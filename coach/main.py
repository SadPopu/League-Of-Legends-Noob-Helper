"""Servidor web local: liga o cliente do LoL, a API da Riot e a interface no browser."""
import asyncio
import json
import os
import logging
import time
from contextlib import asynccontextmanager

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .analysis import my_champion_stats, recommend_build, recommend_counters, recommend_picks
from .champselect import ChampSelectState, mock_session, parse_session
from .data import crawl_meta
from .history import QUEUE_GROUPS, QUEUE_NAMES, store_match, sync_history
from .db import DB
from .ddragon import DDragon
from .lcu import LCU
from .mains import crawl_mastery, ensure_names, games_stale, mains_report, refresh_main_games, top_mains
from .postgame import analyse_match
from .riot_api import RiotAPI, RiotAPIError

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
log = logging.getLogger("coach")

ARAM_QUEUES = {450, 2400}


class Coach:
    def __init__(self):
        self.dd = DDragon()
        self.db = DB()
        self.riot = RiotAPI(config.RIOT_API_KEY, config.PLATFORM, config.REGION)
        self.meta_apis: dict[str, RiotAPI] = {config.PLATFORM: self.riot}
        self.lcu = LCU(self.on_lcu_event, self.on_lcu_status)
        self.clients: set[WebSocket] = set()
        self.me: dict = {}
        self.phase = "None"
        self.last: dict[str, dict] = {}      # última mensagem de cada tipo (para clientes novos)
        self.picks_sig = None
        self.build_sig = None
        self.jobs: dict[str, dict] = {}
        self.postgame_task: asyncio.Task | None = None

    # ---------------------------------------------------------------- broadcast
    async def send(self, msg: dict, remember: bool = True):
        if remember:
            self.last[msg["type"]] = msg
        dead = []
        for ws in self.clients:
            try:
                await ws.send_json(msg)
            except Exception:
                dead.append(ws)
        for ws in dead:
            self.clients.discard(ws)

    def status(self) -> dict:
        return {"type": "status", "lcu": self.lcu.connected, "me": self.me, "phase": self.phase,
                "riot_key": bool(config.RIOT_API_KEY), "jobs": self.jobs, "version": self.dd.version}

    async def push_status(self):
        await self.send(self.status())

    # ---------------------------------------------------------------- identidade
    async def resolve_me(self):
        # O PUUID do cliente (LCU) não serve para a API da Riot, que usa PUUIDs encriptados
        # por chave. Do cliente tiramos só o Riot ID e pedimos o PUUID à API.
        riot_id = config.RIOT_ID
        if self.lcu.connected:
            s = await self.lcu.get("/lol-summoner/v1/current-summoner")
            if s and s.get("gameName"):
                riot_id = f"{s['gameName']}#{s.get('tagLine', '')}"
        if not riot_id or "#" not in riot_id or self.me.get("name", "").lower() == riot_id.lower():
            return
        try:
            name, tag = riot_id.split("#", 1)
            acc = await self.riot.account_by_riot_id(name, tag)
            if acc:
                self.me = {"puuid": acc["puuid"], "name": f"{acc['gameName']}#{acc['tagLine']}"}
        except RiotAPIError as e:
            log.warning("Could not fetch the account: %s", e)

    # ---------------------------------------------------------------- LCU
    async def on_lcu_status(self, connected: bool):
        log.info("LoL client %s", "connected" if connected else "disconnected")
        if connected:
            await self.resolve_me()
            phase = await self.lcu.get("/lol-gameflow/v1/gameflow-phase")
            if phase:
                self.phase = phase
            session = await self.lcu.get("/lol-champ-select/v1/session")
            if session and session.get("myTeam"):
                await self.handle_session(session)
        await self.push_status()

    async def on_lcu_event(self, uri: str, data):
        if uri == "/lol-champ-select/v1/session":
            if data and data.get("myTeam"):
                await self.handle_session(data)
            else:
                self.picks_sig = self.build_sig = None
                for k in ("champselect", "picks", "counters", "build", "mains"):
                    self.last.pop(k, None)
                await self.send({"type": "champselect_end"}, remember=False)
        elif uri == "/lol-gameflow/v1/gameflow-phase" and isinstance(data, str):
            self.phase = data
            await self.push_status()
            if data in ("PreEndOfGame", "EndOfGame"):
                self.schedule_postgame()

    # ---------------------------------------------------------------- champ select
    async def handle_session(self, session: dict):
        st = parse_session(session)
        await self.send(self.champselect_payload(st))

        if st.picks_signature() != self.picks_sig and not st.locked:
            self.picks_sig = st.picks_signature()
            picks = await asyncio.to_thread(recommend_picks, st, self.db, self.dd)
            await self.send({"type": "picks", **picks})
            counters = await asyncio.to_thread(recommend_counters, st, self.db, self.dd)
            await self.send({"type": "counters", **counters})

        if st.my_champion and st.build_signature() != self.build_sig:
            self.build_sig = st.build_signature()
            build = await asyncio.to_thread(recommend_build, st, self.db, self.dd)
            await self.send({"type": "build", "locked": st.locked, **build})
            await self.send_mains(st.my_champion)

    async def send_mains(self, champ: int):
        mains = top_mains(self.db, champ)
        report = await asyncio.to_thread(mains_report, self.db, self.dd, champ, mains)
        await self.send({"type": "mains", **report})
        if any(not m.get("name") for m in mains):
            # os nomes vêm da API (pode demorar se houver uma recolha a correr): envia de novo quando chegarem
            async def names_later():
                await ensure_names(self.apis_for(mains), self.db, mains)
                if self.build_sig and self.build_sig[0] == champ:
                    rep2 = await asyncio.to_thread(mains_report, self.db, self.dd, champ, mains)
                    await self.send({"type": "mains", **rep2})
            asyncio.create_task(names_later())

    def apis_for(self, mains: list[dict]) -> dict[str, RiotAPI]:
        return {m["platform"]: self.api_for(m["platform"]) for m in mains
                if m["platform"] in config.META_PLATFORMS}

    def champselect_payload(self, st: ChampSelectState) -> dict:
        champ = lambda cid: {"id": cid, "name": self.dd.champ_name(cid), "icon": self.dd.champ_icon(cid)} if cid else None
        return {
            "type": "champselect",
            "phase": st.phase,
            "my_position": st.my_position,
            "locked": st.locked,
            "allies": [{**a, "champion": champ(a["champion"])} for a in st.allies],
            "enemies": [champ(c) for c in st.enemies],
            "bans": [champ(c) for c in st.bans],
        }

    # ---------------------------------------------------------------- pós-jogo
    def schedule_postgame(self):
        if self.postgame_task and not self.postgame_task.done():
            return
        self.postgame_task = asyncio.create_task(self.wait_and_analyse(time.time()))

    async def wait_and_analyse(self, ended_at: float):
        """A Riot API demora uns minutos a publicar o jogo depois de acabar."""
        await self.resolve_me()
        if not self.me:
            return
        await self.send({"type": "postgame_pending"}, remember=False)
        for _ in range(30):
            try:
                ids = await self.riot.match_ids(self.me["puuid"], count=1)
                if ids and not self.db.postgame(ids[0]):
                    match = await self.riot.match(ids[0])
                    if match and match["info"].get("gameEndTimestamp", 0) / 1000 > ended_at - 1800:
                        await self.analyse(ids[0], match)
                        return
            except RiotAPIError as e:
                log.warning("Post-game: %s", e)
                return
            await asyncio.sleep(30)

    async def analyse(self, match_id: str, match: dict | None = None) -> dict | None:
        match = match or await self.riot.match(match_id)
        if not match or self.me["puuid"] not in match["metadata"]["participants"]:
            return None
        store_match(self.db, match, self.me["puuid"])  # histórico (todas as filas)
        await self.send({"type": "history_updated"}, remember=False)
        if match["info"].get("queueId") in ARAM_QUEUES:
            return None
        timeline = await self.riot.timeline(match_id)
        report = await asyncio.to_thread(analyse_match, match, timeline, self.me["puuid"], self.db, self.dd)
        self.db.save_postgame(match_id, report)
        await self.send({"type": "postgame", "report": report}, remember=False)
        return report

    def api_for(self, platform: str) -> RiotAPI:
        """Um cliente por região: cada região tem o seu próprio limite de pedidos."""
        if platform not in self.meta_apis:
            self.meta_apis[platform] = RiotAPI(config.RIOT_API_KEY, platform, config.META_PLATFORMS[platform])
        return self.meta_apis[platform]

    # ---------------------------------------------------------------- jobs
    def start_job(self, name: str, coro_factory, unit: str = "new games"):
        if self.jobs.get(name, {}).get("running"):
            return False

        async def progress(*args):
            self.jobs[name]["progress"] = " ".join(str(a) for a in args[:-2]) + f" {args[-2]}/{args[-1]}"
            await self.push_status()

        async def runner():
            self.jobs[name] = {"running": True, "progress": "starting", "result": ""}
            await self.push_status()
            try:
                result = await coro_factory(progress)
                self.jobs[name].update(running=False, result=f"Done: {result} {unit}")
            except Exception as e:
                log.exception("Job %s falhou", name)
                self.jobs[name].update(running=False, result=f"Error: {e}")
            await self.push_status()

        asyncio.create_task(runner())
        return True


coach = Coach()

DAILY_TASKS = ("meta", "mastery", "sync")
DAY = 86400


def start_meta_job(regions=("euw1", "kr"), players=100, per_player=20, days=14, include_master=False,
                   timelines=True, backfill=300) -> bool:
    tiers = ("challenger", "grandmaster", "master") if include_master else ("challenger", "grandmaster")
    regions = [r for r in regions if r in config.META_PLATFORMS] or [config.PLATFORM]

    async def run(progress):
        # cada região tem o seu limite de pedidos, por isso correm em paralelo
        totals = await asyncio.gather(*(crawl_meta(
            coach.api_for(r), coach.db, tiers=tiers, players=players, per_player=per_player, days=days,
            timelines=timelines, backfill=backfill, progress=progress, label=r.upper()) for r in regions))
        return sum(totals)

    return coach.start_job("meta", run)


def start_mastery_job(regions=("euw1", "kr")) -> bool:
    regions = [r for r in regions if r in config.META_PLATFORMS] or [config.PLATFORM]

    async def run(progress):
        totals = await asyncio.gather(*(crawl_mastery(coach.api_for(r), coach.db, progress=progress, label=r.upper())
                                        for r in regions))
        return sum(totals)

    return coach.start_job("mastery", run, unit="players checked")


def start_sync_job() -> bool:
    if not coach.me:
        return False
    return coach.start_job("sync", lambda p: sync_history(coach.riot, coach.db, coach.me["puuid"], p))


def start_mains_games_job(champ: int, mains: list[dict]) -> bool:
    return coach.start_job("mains_games", lambda p: refresh_main_games(
        coach.apis_for(mains), coach.db, champ, mains, progress=p))


async def daily_loop():
    """Uma vez por dia (enquanto a app estiver aberta): recolhe jogos high elo, revê as
    maestrias/top 5 dos mains e sincroniza os teus jogos."""
    await asyncio.sleep(60)
    while True:
        if coach.db.kv_get("auto_enabled") != "0" and config.RIOT_API_KEY:
            for task in DAILY_TASKS:
                last = float(coach.db.kv_get(f"auto:{task}") or 0)
                if time.time() - last < DAY or coach.jobs.get(task, {}).get("running"):
                    continue
                if task == "sync":
                    await coach.resolve_me()
                started = {"meta": start_meta_job, "mastery": start_mastery_job, "sync": start_sync_job}[task]()
                if started:
                    log.info("Daily task: %s", task)
                    coach.db.kv_set(f"auto:{task}", str(time.time()))
        await asyncio.sleep(1800)


@asynccontextmanager
async def lifespan(_: FastAPI):
    await coach.dd.load()
    lcu_task = asyncio.create_task(coach.lcu.run())
    # NOOB_NO_AUTO=1 desliga as tarefas diárias (útil para testes)
    daily_task = asyncio.create_task(daily_loop() if not os.getenv("NOOB_NO_AUTO") else asyncio.sleep(0))
    await coach.resolve_me()
    log.info("Open http://127.0.0.1:%s in your browser", config.PORT)
    yield
    lcu_task.cancel()
    daily_task.cancel()
    for api in coach.meta_apis.values():
        await api.close()


app = FastAPI(title="Noob Helper", lifespan=lifespan)
app.mount("/static", StaticFiles(directory=config.STATIC_DIR), name="static")


@app.get("/")
async def index():
    return FileResponse(config.STATIC_DIR / "index.html")


@app.websocket("/ws")
async def ws_endpoint(ws: WebSocket):
    await ws.accept()
    coach.clients.add(ws)
    await ws.send_json(coach.status())
    for msg in list(coach.last.values()):
        if msg["type"] != "status":
            await ws.send_json(msg)
    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        coach.clients.discard(ws)


@app.get("/api/stats")
async def stats(position: str | None = None):
    rows = my_champion_stats(coach.db, position or None)
    for r in rows:
        r["name"] = coach.dd.champ_name(r["champion_id"])
        r["icon"] = coach.dd.champ_icon(r["champion_id"])
    meta = coach.db.query("SELECT COUNT(*) n, SUM(has_timeline = 1) t FROM matches WHERE kind='meta'")[0]
    regions = coach.db.query(
        """SELECT substr(match_id, 1, instr(match_id, '_') - 1) region, COUNT(*) n
           FROM matches WHERE kind='meta' GROUP BY region""")
    return {"champions": rows, "meta_games": meta["n"], "meta_timelines": meta["t"] or 0,
            "meta_regions": {r["region"]: r["n"] for r in regions}}


@app.post("/api/sync")
async def sync():
    """Sincroniza todos os teus jogos da season (histórico + estatísticas ranked)."""
    await coach.resolve_me()
    if not coach.me:
        return {"ok": False, "error": "Unknown account: open the LoL client or set RIOT_ID in .env"}
    return {"ok": start_sync_job()}


# ---------------------------------------------------------------- histórico
def _history_where(group: str, champion: int | None):
    where, args = ["remake=0"], []
    if group in QUEUE_GROUPS or group == "other":
        where.append("grp=?")
        args.append(group)
    if champion:
        where.append("champion_id=?")
        args.append(champion)
    return " AND ".join(where), args


def _player_view(p: dict) -> dict:
    dd = coach.dd
    return {
        **p,
        "champion": {"name": dd.champ_name(p["champion_id"]), "icon": dd.champ_icon(p["champion_id"])},
        "items": [{"id": i, "name": dd.item_name(i), "icon": dd.item_icon(i)} if i else None for i in p["items"]],
        "spells": [{"name": dd.spells.get(s, ""), "icon": dd.spell_icons.get(s, "")} for s in p["spells"]],
        "keystone": {"name": dd.perks.get(p["keystone"], ""), "icon": dd.perk_icons.get(p["keystone"], "")}
                    if p.get("keystone") else None,
        "sub_style": {"icon": dd.perk_icons.get(p["sub_style"], "")} if p.get("sub_style") else None,
        "is_me": p["puuid"] == coach.me.get("puuid"),
    }


def _teams_compact(players: list[dict]) -> list[list[dict]]:
    teams: dict = {}
    for p in players:
        teams.setdefault(p["team"], []).append(
            {"name": p["name"].split("#")[0], "icon": coach.dd.champ_icon(p["champion_id"]),
             "is_me": p["puuid"] == coach.me.get("puuid")})
    return list(teams.values())


@app.get("/api/history")
async def history(group: str = "all", offset: int = 0, limit: int = 20, champion: int | None = None):
    where, args = _history_where(group, champion)
    rows = coach.db.query(
        f"SELECT data FROM history WHERE {where} ORDER BY game_creation DESC LIMIT ? OFFSET ?",
        (*args, limit, offset))
    games = []
    for r in rows:
        d = json.loads(r["data"])
        me = next((p for p in d["players"] if p["puuid"] == coach.me.get("puuid")), None)
        if not me:
            continue
        games.append({
            "match_id": d["match_id"], "queue": QUEUE_NAMES.get(d["queue_id"], f"Fila {d['queue_id']}"),
            "game_creation": d["game_creation"], "duration": d["duration"],
            "me": _player_view(me), "teams": _teams_compact(d["players"]),
        })
    summary = coach.db.query(
        f"""SELECT COUNT(*) games, SUM(win) wins, AVG(kills) k, AVG(deaths) d, AVG(assists) a,
                   AVG(cs*60.0/duration) cspm, AVG(damage) dmg, AVG(gold) gold, AVG(vision) vision,
                   AVG(kp) kp, AVG(score) score, SUM(badge='MVP') mvp, SUM(badge='ACE') ace
            FROM history WHERE {where}""", args)[0]
    counts = {r["grp"]: r["n"] for r in coach.db.query(
        "SELECT grp, COUNT(*) n FROM history WHERE remake=0 GROUP BY grp")}
    top = coach.db.query(
        f"""SELECT champion_id, COUNT(*) g, SUM(win) w, AVG(kills) k, AVG(deaths) d, AVG(assists) a
            FROM history WHERE {where} GROUP BY champion_id ORDER BY g DESC LIMIT 5""", args)
    for t in top:
        t["name"], t["icon"] = coach.dd.champ_name(t["champion_id"]), coach.dd.champ_icon(t["champion_id"])
    return {"games": games, "summary": summary, "counts": counts, "top_champions": top}


@app.get("/api/history/{match_id}")
async def history_detail(match_id: str):
    d = coach.db.history_detail(match_id)
    if not d:
        return {"ok": False}
    teams: dict = {}
    for p in d["players"]:
        teams.setdefault(p["team"], []).append(_player_view(p))
    return {
        "ok": True, "match_id": match_id, "queue": QUEUE_NAMES.get(d["queue_id"], ""),
        "duration": d["duration"],
        "max_damage": max((p["damage"] for p in d["players"]), default=1) or 1,
        "teams": [{"team": k, "win": v[0]["win"], "players": v,
                   "kills": sum(p["kills"] for p in v), "gold": sum(p["gold"] for p in v)}
                  for k, v in teams.items()],
    }


class CrawlParams(BaseModel):
    regions: list[str] = ["euw1"]
    players: int = 60
    per_player: int = 15
    days: int = 14
    include_master: bool = False
    timelines: bool = True
    backfill: int = 300


@app.post("/api/meta/crawl")
async def meta_crawl(params: CrawlParams):
    return {"ok": start_meta_job(params.regions, params.players, params.per_player, params.days,
                                 params.include_master, params.timelines, params.backfill)}


class MasteryParams(BaseModel):
    regions: list[str] = ["euw1", "kr"]


@app.post("/api/mains/crawl")
async def mains_crawl(params: MasteryParams):
    return {"ok": start_mastery_job(params.regions)}


@app.get("/api/mains")
async def mains_list():
    counts = {r["champion_id"]: r for r in coach.db.query(
        """SELECT champion_id, COUNT(*) n, MAX(points) top FROM mains
           WHERE champion_id > 0 GROUP BY champion_id""")}
    players = coach.db.query("SELECT COUNT(DISTINCT puuid) n FROM mains")[0]["n"]
    champs = sorted(({"id": cid, "name": c["name"], "icon": coach.dd.champ_icon(cid),
                      "mains": counts.get(cid, {}).get("n", 0), "top": counts.get(cid, {}).get("top", 0)}
                     for cid, c in coach.dd.champs.items()), key=lambda c: c["name"])
    return {"champions": champs, "players": players}


@app.get("/api/mains/{champ}")
async def mains_champ(champ: int, sort: str = "points"):
    # responde logo com o que já está na base de dados; o que vem da API corre em segundo plano
    mains = top_mains(coach.db, champ, sort=sort)
    report = await asyncio.to_thread(mains_report, coach.db, coach.dd, champ, mains)
    if any(not m.get("name") for m in mains):
        async def names_later():
            await ensure_names(coach.apis_for(mains), coach.db, mains)
            await coach.send({"type": "mains_updated", "champion": champ}, remember=False)
        asyncio.create_task(names_later())
        report["names_pending"] = True
    # atualiza sozinho os jogos recentes dos mains (no máximo 2x por dia por boneco)
    report["auto_refresh"] = bool(mains) and games_stale(coach.db, champ) and start_mains_games_job(champ, mains)
    return report


@app.post("/api/mains/{champ}/refresh")
async def mains_refresh(champ: int):
    mains = top_mains(coach.db, champ)
    if not mains:
        return {"ok": False, "error": "No masteries collected yet."}
    return {"ok": start_mains_games_job(champ, mains)}


@app.get("/api/auto")
async def auto_status():
    return {"enabled": coach.db.kv_get("auto_enabled") != "0",
            "last": {t: float(coach.db.kv_get(f"auto:{t}") or 0) for t in DAILY_TASKS}}


@app.post("/api/auto")
async def auto_set(enabled: bool):
    coach.db.kv_set("auto_enabled", "1" if enabled else "0")
    return await auto_status()


@app.get("/api/runes")
async def runes():
    return coach.dd.rune_tree()


@app.get("/api/postgame")
async def postgame_list():
    return coach.db.postgames()


@app.post("/api/postgame/analyse")
async def postgame_analyse(match_id: str | None = None):
    await coach.resolve_me()
    if not coach.me:
        return {"ok": False, "error": "Unknown account: open the LoL client or set RIOT_ID in .env"}
    try:
        if not match_id:
            ids = await coach.riot.match_ids(coach.me["puuid"], count=5)
            for mid in ids:
                match = await coach.riot.match(mid)
                if match and match["info"].get("queueId") not in ARAM_QUEUES:
                    match_id = mid
                    break
        if not match_id:
            return {"ok": False, "error": "No recent games found"}
        report = await coach.analyse(match_id)
    except RiotAPIError as e:
        return {"ok": False, "error": str(e)}
    return {"ok": bool(report), "report": report}


@app.get("/api/champions")
async def champions():
    return sorted(c["name"] for c in coach.dd.champs.values())


class SimAlly(BaseModel):
    position: str
    champion: str = ""


class SimParams(BaseModel):
    position: str = "MIDDLE"
    champion: str = ""
    locked: bool = False
    allies: list[SimAlly] = []
    enemies: list[str] = []
    bans: list[str] = []


@app.post("/api/simulate")
async def simulate(p: SimParams):
    """Simula um champ select (para testar sem estar em fila)."""
    look = lambda n: coach.dd.champ_lookup(n) or 0 if n else 0
    session = mock_session(
        p.position, look(p.champion),
        [(a.position, look(a.champion)) for a in p.allies],
        [c for c in (look(e) for e in p.enemies) if c],
        [c for c in (look(b) for b in p.bans) if c],
        p.locked,
    )
    await coach.handle_session(session)
    return {"ok": True}


@app.post("/api/simulate/end")
async def simulate_end():
    await coach.on_lcu_event("/lol-champ-select/v1/session", None)
    return {"ok": True}

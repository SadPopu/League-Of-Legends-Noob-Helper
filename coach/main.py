"""Servidor web local: liga o cliente do LoL, a API da Riot e a interface no browser."""
import asyncio
import hashlib
import json
import os
import logging
import time
from contextlib import asynccontextmanager

import httpx
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from . import config
from .analysis import my_champion_stats, recommend_build, recommend_counters, recommend_picks
from .champselect import ChampSelectState, mock_session, parse_session
from .data import collect_new_games, crawl_meta
from .ingame import (RIFT_MAP, assign_roles, fetch_basics, fetch_performance, live_positions, players_from_game,
                     public_player)
from .history import QUEUE_GROUPS, QUEUE_NAMES, store_match, sync_history
from .db import DB
from .ddragon import DDragon
from .lcu import LCU
from .mains import crawl_mastery, ensure_names, games_stale, mains_report, refresh_main_games, top_mains
from .postgame import analyse_match
from .profile import QUEUES, game_lp, lp_series, rank_view
from .riot_api import RiotAPI, RiotAPIError, background_job

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s: %(message)s")
log = logging.getLogger("coach")

FAVORITE_KEY = "favorite_account"
GAME_START_PHASES = {"GameStart", "InProgress", "Reconnect"}
GAME_LOOKUP_TRIES, GAME_LOOKUP_WAIT = 30, 10  # até 5 minutos à espera que a Riot publique o jogo
LIVE_POSITION_TRIES, LIVE_POSITION_WAIT = 30, 15  # o jogo só responde depois do ecrã de loading
ARAM_QUEUES = {450, 2400}
# fases do cliente em que o jogo está a decorrer (a build do champ select fica no ecrã)
IN_GAME_PHASES = {"GameStart", "InProgress", "Reconnect", "WaitingForStats"}
# fases em que a build do champ select anterior deixa de interessar
CLEAR_PHASES = {"None", "Lobby", "Matchmaking", "ReadyCheck", "PreEndOfGame", "EndOfGame", "TerminatedInError"}
# muda quando a página muda: um separador que ficou aberto com a versão antiga recarrega-se sozinho
APP_BUILD = hashlib.sha1(b"".join(f.read_bytes() for f in sorted(config.STATIC_DIR.iterdir()) if f.is_file())).hexdigest()[:12]


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
        self.job_tasks: dict[str, asyncio.Task] = {}
        self.postgame_task: asyncio.Task | None = None
        self.cs_ended = False  # champ select acabou mas a build continua no ecrã (jogo a decorrer)
        self.profile: dict = {}  # nome, ícone e rank (guardado uns minutos)
        self.cs_state: ChampSelectState | None = None  # último estado do champ select
        self.game_task: asyncio.Task | None = None

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
                "riot_key": bool(config.RIOT_API_KEY), "jobs": self.jobs, "version": self.dd.version,
                "build": APP_BUILD}

    async def push_status(self):
        await self.send(self.status())

    # ---------------------------------------------------------------- identidade
    async def resolve_me(self):
        # A conta é a favorita escolhida na app; sem favorita, a do RIOT_ID no .env; sem ele, a aberta no cliente.
        # O PUUID do cliente (LCU) não serve para a API da Riot, que usa PUUIDs encriptados
        # por chave. Do cliente tiramos só o Riot ID e pedimos o PUUID à API.
        riot_id = self.db.kv_get(FAVORITE_KEY) or config.RIOT_ID
        if not riot_id and self.lcu.connected:
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
                self.switch_account()
        except RiotAPIError as e:
            log.warning("Could not fetch the account: %s", e)

    def switch_account(self):
        """Os dados de cada conta ficam guardados à parte; a app mostra só os da conta ativa.
        Uma conta sem jogos guardados (nova na app) vai buscá-los logo."""
        puuid = self.me["puuid"]
        old = self.db.adopt_puuid(self.me["name"], puuid)
        if old:
            log.info("%s: games moved to the PUUID of the new API key", self.me["name"])
        self.db.me_puuid = puuid
        self.profile = {}
        if not self.db.query("SELECT 1 FROM history WHERE puuid=? LIMIT 1", (puuid,)):
            log.info("%s has no games saved yet: fetching them", self.me["name"])
            start_sync_job()

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
            if self.phase in GAME_START_PHASES:
                self.start_game_tracking()
        await self.push_status()

    async def on_lcu_event(self, uri: str, data):
        if uri == "/lol-champ-select/v1/session":
            if data and data.get("myTeam"):
                # a sessão ainda pode atualizar-se com o jogo a começar: isso não é um champ select novo
                if self.phase not in GAME_START_PHASES:
                    await self.handle_session(data)
            elif not self.cs_ended:
                # o champ select acabou: se foi porque o jogo começou, a build fica visível até ao fim do jogo
                self.cs_ended = True
                asyncio.create_task(self.after_champselect())
        elif uri == "/lol-gameflow/v1/gameflow-phase" and isinstance(data, str):
            self.phase = data
            await self.push_status()
            if self.cs_ended and data in CLEAR_PHASES:
                await self.clear_champselect()
            if data in GAME_START_PHASES:
                self.start_game_tracking()
            if data in ("PreEndOfGame", "EndOfGame"):
                self.schedule_postgame()

    async def after_champselect(self):
        """Espera a fase do jogo assentar: jogo a começar → mantém; dodge/saída → limpa."""
        await asyncio.sleep(2)
        if not self.cs_ended:
            return  # entretanto começou outro champ select
        phase = await self.lcu.get("/lol-gameflow/v1/gameflow-phase") if self.lcu.connected else "None"
        if isinstance(phase, str):
            self.phase = phase
        if self.phase in IN_GAME_PHASES:
            await self.send({"type": "ingame", "phase": self.phase})
        else:
            await self.clear_champselect()

    async def clear_champselect(self):
        self.cs_ended = False
        self.picks_sig = self.build_sig = None
        self.cs_state = None
        self.stop_game_tracking()
        for k in ("champselect", "picks", "counters", "build", "mains", "ingame", "game"):
            self.last.pop(k, None)
        await self.send({"type": "champselect_end"}, remember=False)

    # ---------------------------------------------------------------- champ select
    async def handle_session(self, session: dict):
        if self.cs_ended:  # novo champ select: esquece o anterior
            self.cs_ended = False
            self.stop_game_tracking()
            self.last.pop("ingame", None)
            self.last.pop("game", None)
        st = parse_session(session)
        self.cs_state = st
        await self.send(self.champselect_payload(st))

        # build/mains primeiro (rápidos); sugestões em segundo plano para não atrasar os eventos seguintes
        if st.my_champion and st.build_signature() != self.build_sig:
            self.build_sig = st.build_signature()
            build = await asyncio.to_thread(recommend_build, st, self.db, self.dd)
            await self.send({"type": "build", "locked": st.locked, **build})
            await self.send_mains(st.my_champion, st.my_position or None)

        if st.picks_signature() != self.picks_sig and not st.locked:
            self.picks_sig = st.picks_signature()
            asyncio.create_task(self.send_picks(st, self.picks_sig))

    async def send_picks(self, st: ChampSelectState, sig):
        """Calcula sugestões e counters; se entretanto houve outro pick, o resultado antigo é descartado."""
        picks = await asyncio.to_thread(recommend_picks, st, self.db, self.dd)
        if sig != self.picks_sig:
            return
        await self.send({"type": "picks", **picks})
        counters = await asyncio.to_thread(recommend_counters, st, self.db, self.dd)
        if sig == self.picks_sig:
            await self.send({"type": "counters", **counters})

    async def send_mains(self, champ: int, position: str | None = None):
        mains = top_mains(self.db, champ)
        report = await asyncio.to_thread(mains_report, self.db, self.dd, champ, mains, position)
        await self.send({"type": "mains", **report})
        if any(not m.get("name") for m in mains):
            # os nomes vêm da API (pode demorar se houver uma recolha a correr): envia de novo quando chegarem
            async def names_later():
                await ensure_names(self.apis_for(mains), self.db, mains)
                if self.build_sig and self.build_sig[0] == champ:
                    rep2 = await asyncio.to_thread(mains_report, self.db, self.dd, champ, mains, position)
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
            "ally_bans": [champ(c) for c in st.ally_bans],
            "enemy_bans": [champ(c) for c in st.enemy_bans],
        }

    # ---------------------------------------------------------------- jogo a começar
    def start_game_tracking(self):
        if self.game_task and not self.game_task.done():
            return
        self.cs_ended = True  # a app pode ter aberto já com o jogo a decorrer
        self.game_task = asyncio.create_task(self.track_game())

    def stop_game_tracking(self):
        if self.game_task and not self.game_task.done():
            self.game_task.cancel()
        self.game_task = None

    async def client_puuid(self) -> str | None:
        """A conta que está mesmo a jogar é a do cliente, que pode não ser a favorita."""
        s = await self.lcu.get("/lol-summoner/v1/current-summoner")
        if not s or not s.get("gameName"):
            return None
        acc = await self.riot.account_by_riot_id(s["gameName"], s.get("tagLine", ""))
        return acc["puuid"] if acc else None

    async def track_game(self):
        try:
            puuid = await self.client_puuid()
            if not puuid:
                return
            # a Riot só publica o jogo uns segundos depois de começar
            for _ in range(GAME_LOOKUP_TRIES):
                try:
                    game = await self.riot.active_game(puuid)
                except (RiotAPIError, httpx.HTTPStatusError) as e:  # uma falha da Riot não desiste do jogo
                    log.warning("Live game: %s", e)
                    game = None
                if game:
                    await self.follow_game(game, puuid)
                    return
                if self.phase not in IN_GAME_PHASES:
                    return
                await asyncio.sleep(GAME_LOOKUP_WAIT)
        except RiotAPIError as e:
            log.warning("Live game: %s", e)
        except Exception:
            log.exception("Could not read the live game")

    async def follow_game(self, game: dict, puuid: str | None, watch_live: bool = True):
        """Mostra logo os 10 jogadores; os dados de cada um e as roles vão sendo acrescentados."""
        players = players_from_game(game, puuid, self.dd)
        is_rift = game.get("mapId") == RIFT_MAP and len(players) == 10
        known: dict = {}
        me = next((p for p in players if p["me"]), None)
        if self.cs_state and me:
            known = {(me["team"], a["champion"]): (a["position"], "team")
                     for a in self.cs_state.allies if a["champion"] and a["position"]}

        async def publish():
            await self.send({"type": "game", "game_id": game.get("gameId"), "is_rift": is_rift,
                             "players": [public_player(p) for p in players]})

        async def update_roles():
            if is_rift:
                await asyncio.to_thread(assign_roles, self.db, self.dd, players, known)
                await self.update_game_build(players)

        async def enrich():
            background_job.set(True)  # o que pedires na app passa à frente destes pedidos
            await asyncio.gather(*(fetch_basics(self.riot, p) for p in players))
            await publish()
            # o adversário de lane primeiro, a seguir os inimigos, a tua equipa no fim
            team = me["team"] if me else None
            for p in sorted(players, key=lambda x: (x["team"] == team, not (me and x["role"] == me["role"]))):
                await fetch_performance(self.riot, self.db, p)
                await publish()
            await update_roles()  # os últimos jogos de cada um ajudam a acertar as roles
            await publish()

        async def follow_live():
            for _ in range(LIVE_POSITION_TRIES):
                found = await live_positions(self.dd)
                if found:
                    known.update(found)
                    await update_roles()
                    await publish()
                    return
                await asyncio.sleep(LIVE_POSITION_WAIT)

        await update_roles()
        await publish()
        await asyncio.gather(enrich(), *([follow_live()] if watch_live and is_rift else []))

    async def update_game_build(self, players: list[dict]):
        """Refaz a build com a role e o adversário reais; se nada mudou, não faz nada."""
        me = next((p for p in players if p["me"]), None)
        if not me or not me["role"]:
            return
        side = [p for p in players if p["team"] == me["team"]]
        enemies = [p for p in players if p["team"] != me["team"]]
        st = ChampSelectState(
            my_position=me["role"], my_champion=me["champion_id"], locked=True,
            allies=[{"champion": p["champion_id"], "position": p["role"], "me": p["me"], "locked": True} for p in side],
            enemies=[p["champion_id"] for p in enemies],
            enemy_roles={p["role"]: p["champion_id"] for p in enemies},
            bans=self.cs_state.bans if self.cs_state else [])
        if st.build_signature() == self.build_sig:
            return
        self.build_sig = st.build_signature()
        build = await asyncio.to_thread(recommend_build, st, self.db, self.dd)
        await self.send({"type": "build", "locked": True, "from_game": True, **build})
        await self.send_mains(me["champion_id"], me["role"])

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
        known = self.db.has_history(match_id, self.me["puuid"])
        store_match(self.db, match, self.me["puuid"])  # histórico (todas as filas)
        if not known:
            await self.refresh_profile(force=True)
            await self.send({"type": "history_updated"}, remember=False)
        if match["info"].get("queueId") in ARAM_QUEUES:
            return None
        timeline = await self.riot.timeline(match_id)
        report = await asyncio.to_thread(analyse_match, match, timeline, self.me["puuid"], self.db, self.dd)
        self.db.save_postgame(match_id, self.me["puuid"], report)
        await self.send({"type": "postgame", "report": report}, remember=False)
        return report

    async def refresh_profile(self, force: bool = False) -> dict:
        """Nome, nível, ícone e rank (solo/flex); cada vez que o rank muda fica registado para o gráfico de LP."""
        await self.resolve_me()
        if not self.me:
            return {}
        p = self.profile
        if force or p.get("puuid") != self.me["puuid"] or time.time() - p.get("ts", 0) > 300:
            summ = await self.riot.summoner_by_puuid(self.me["puuid"]) or {}
            entries = await self.riot.league_entries_by_puuid(self.me["puuid"])
            now = int(time.time() * 1000)
            for e in entries:
                if e.get("queueType") in QUEUES.values():
                    self.db.save_rank(self.me["puuid"], e, now)
            by_queue = {e.get("queueType"): e for e in entries}
            self.profile = {
                "puuid": self.me["puuid"], "ts": time.time(), "name": self.me["name"],
                "level": summ.get("summonerLevel"),
                "icon": f"https://ddragon.leagueoflegends.com/cdn/{self.dd.version}/img/profileicon/{summ['profileIconId']}.png"
                        if summ.get("profileIconId") is not None else None,
                "ranks": {k: rank_view(by_queue.get(q)) for k, q in QUEUES.items()},
            }
        return self.profile

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
            # (região, passo, feito, total) quando há várias regiões em paralelo; (passo, feito, total) senão
            key = str(args[0]) if len(args) > 3 else "_"
            step = " ".join(str(a) for a in (args[1:-2] if len(args) > 3 else args[:-2]))
            self.jobs[name]["parts"][key] = {"step": step, "done": args[-2], "total": args[-1]}
            self.jobs[name]["progress"] = " ".join(str(a) for a in args[:-2]) + f" {args[-2]}/{args[-1]}"
            await self.push_status()

        async def runner():
            self.jobs[name] = {"running": True, "progress": "starting", "result": "", "parts": {}, "count": None}
            await self.push_status()
            try:
                result = await coro_factory(progress)
                self.jobs[name].update(running=False, result=f"Done: {result} {unit}", count=result, ok=True)
                self.db.kv_set(f"last_job:{name}", str(time.time()))
            except asyncio.CancelledError:
                # parado pelo utilizador: o que já foi descarregado fica guardado
                done = sum(p["done"] for p in self.jobs[name]["parts"].values())
                self.jobs[name].update(running=False, result="Stopped", count=done, ok=True, stopped=True)
            except Exception as e:
                log.exception("Job %s falhou", name)
                self.jobs[name].update(running=False, result=f"Error: {e}", ok=False)
            await self.push_status()

        self.job_tasks[name] = asyncio.create_task(runner())
        return True

    def stop_job(self, name: str) -> bool:
        task = self.job_tasks.get(name)
        if not task or task.done():
            return False
        task.cancel()
        return True


coach = Coach()

DAILY_TASKS = ("meta", "mastery", "sync")
DAY = 86400


async def run_regions(coros) -> list:
    """Corre uma tarefa por região em paralelo; se uma falhar, as outras param também."""
    tasks = [asyncio.create_task(c) for c in coros]
    try:
        return await asyncio.gather(*tasks)
    except Exception:
        for t in tasks:
            t.cancel()
        raise


def start_meta_job(regions=("euw1", "kr"), players=100, per_player=20, days=14, include_master=False,
                   timelines=True, backfill=300) -> bool:
    tiers = ("challenger", "grandmaster", "master") if include_master else ("challenger", "grandmaster")
    regions = [r for r in regions if r in config.META_PLATFORMS] or [config.PLATFORM]

    async def run(progress):
        # cada região tem o seu limite de pedidos, por isso correm em paralelo
        totals = await run_regions(crawl_meta(
            coach.api_for(r), coach.db, tiers=tiers, players=players, per_player=per_player, days=days,
            timelines=timelines, backfill=backfill, progress=progress, label=r.upper()) for r in regions)
        return sum(totals)

    return coach.start_job("meta", run)


def start_collect_job(target: int, regions=("euw1", "kr"), days=30, timelines=True) -> bool:
    """Vai buscar `target` jogos high elo novos, divididos pelas regiões escolhidas (em paralelo)."""
    regions = [r for r in regions if r in config.META_PLATFORMS] or [config.PLATFORM]
    share = [target // len(regions) + (i < target % len(regions)) for i in range(len(regions))]

    async def run(progress):
        totals = await run_regions(collect_new_games(
            coach.api_for(r), coach.db, n, days=days, timelines=timelines, progress=progress, label=r.upper())
            for r, n in zip(regions, share))
        return sum(totals)

    return coach.start_job("meta", run)


def start_mastery_job(regions=("euw1", "kr")) -> bool:
    regions = [r for r in regions if r in config.META_PLATFORMS] or [config.PLATFORM]

    async def run(progress):
        totals = await run_regions(crawl_mastery(coach.api_for(r), coach.db, progress=progress, label=r.upper())
                                   for r in regions)
        return sum(totals)

    return coach.start_job("mastery", run, unit="players checked")


def start_sync_job() -> bool:
    if not coach.me:
        return False
    async def run(progress):
        background_job.set(True)
        n = await sync_history(coach.riot, coach.db, coach.me["puuid"], progress)
        try:
            await coach.refresh_profile(force=True)
        except RiotAPIError as e:
            log.warning("Rank: %s", e)
        return n

    return coach.start_job("sync", run)


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
    asyncio.create_task(my_mastery())  # guarda as tuas maestrias para as sugestões do champ select
    asyncio.create_task(profile())     # regista o rank atual (gráfico de LP)
    log.info("Open http://127.0.0.1:%s in your browser", config.PORT)
    yield
    lcu_task.cancel()
    daily_task.cancel()
    for api in coach.meta_apis.values():
        await api.close()


app = FastAPI(title="Noob Helper", lifespan=lifespan)


@app.middleware("http")
async def never_cache_page(request, call_next):
    """O browser não pode reabrir a página de uma cópia antiga depois de a app mudar."""
    response = await call_next(request)
    if request.url.path == "/" or request.url.path.startswith("/static/"):
        response.headers["Cache-Control"] = "no-store"
    return response

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
        return {"ok": False, "error": "Unknown account: search for your Riot ID at the top of the page"}
    return {"ok": start_sync_job()}


# ---------------------------------------------------------------- pesquisa de contas e favorita
async def lookup_account(riot_id: str) -> dict:
    """Procura um Riot ID no servidor da app. Devolve {"ok": False, "error": ...} se não der para o usar."""
    name, _, tag = riot_id.strip().partition("#")
    name, tag = name.strip(), tag.strip()
    if not name or not tag:
        return {"ok": False, "error": "Type the full Riot ID, like Name#TAG"}
    if not tag.isalnum():  # a Riot responde com erro de servidor em vez de "não existe"
        return {"ok": False, "error": f"No account called {name}#{tag}"}
    try:
        acc = await coach.riot.account_by_riot_id(name, tag)
        if not acc:
            return {"ok": False, "error": f"No account called {name}#{tag}"}
        full_name = f"{acc['gameName']}#{acc['tagLine']}"
        summ = await coach.riot.summoner_by_puuid(acc["puuid"])
        if not summ:  # a conta existe mas joga noutro servidor: os jogos dela não se encontram com esta região
            return {"ok": False, "error": f"{full_name} doesn't play on this server ({config.PLATFORM.upper()})"}
        entries = await coach.riot.league_entries_by_puuid(acc["puuid"])
    except RiotAPIError as e:
        return {"ok": False, "error": str(e)}
    except httpx.HTTPStatusError:  # a Riot recusa nomes com caracteres inválidos
        return {"ok": False, "error": f"No account called {name}#{tag}"}
    solo = next((e for e in entries if e.get("queueType") == QUEUES["solo"]), None)
    return {"ok": True, "puuid": acc["puuid"], "name": full_name, "level": summ.get("summonerLevel"),
            "icon": f"https://ddragon.leagueoflegends.com/cdn/{coach.dd.version}/img/profileicon/{summ['profileIconId']}.png"
                    if summ.get("profileIconId") is not None else None,
            "rank": rank_view(solo), "is_favorite": full_name.lower() == (coach.db.kv_get(FAVORITE_KEY) or "").lower()}


class FavoriteParams(BaseModel):
    riot_id: str


@app.get("/api/account/search")
async def account_search(riot_id: str):
    found = await lookup_account(riot_id)
    found.pop("puuid", None)
    return found


@app.post("/api/account/favorite")
async def account_favorite(params: FavoriteParams):
    """A conta favorita passa a ser a conta da app, e continua a sê-lo depois de reiniciar."""
    found = await lookup_account(params.riot_id)
    if not found["ok"]:
        return found
    coach.db.kv_set(FAVORITE_KEY, found["name"])
    if coach.me.get("puuid") != found["puuid"]:
        # uma sincronização a meio é da conta anterior: tem de acabar antes de começar a da nova
        if coach.stop_job("sync"):
            await coach.job_tasks["sync"]
        coach.me = {"puuid": found["puuid"], "name": found["name"]}
        coach.switch_account()
        start_sync_job()  # mesmo uma conta já conhecida pode ter jogos novos
        asyncio.create_task(my_mastery())
    await coach.push_status()
    return {"ok": True, "name": found["name"]}


# ---------------------------------------------------------------- histórico
def _history_where(group: str, champion: int | None):
    where, args = ["puuid=?", "remake=0"], [coach.me.get("puuid")]
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
    lp_by_game = game_lp(coach.db, coach.me.get("puuid"))
    games = []
    for r in rows:
        d = json.loads(r["data"])
        me = next((p for p in d["players"] if p["puuid"] == coach.me.get("puuid")), None)
        if not me:
            continue
        games.append({
            "match_id": d["match_id"], "queue": QUEUE_NAMES.get(d["queue_id"], f"Fila {d['queue_id']}"),
            "game_creation": d["game_creation"], "duration": d["duration"],
            # a análise pós-jogo (mapa, farm, lane...) só faz sentido no Summoner's Rift
            "analysable": not d.get("remake") and any(d["queue_id"] in QUEUE_GROUPS[g] for g in ("solo", "flex", "normal")),
            "has_report": coach.db.postgame(d["match_id"]) is not None,
            "lp": lp_by_game.get(d["match_id"]),
            "me": _player_view(me), "teams": _teams_compact(d["players"]),
        })
    summary = coach.db.query(
        f"""SELECT COUNT(*) games, SUM(win) wins, AVG(kills) k, AVG(deaths) d, AVG(assists) a,
                   AVG(cs*60.0/duration) cspm, AVG(damage) dmg, AVG(gold) gold, AVG(vision) vision,
                   AVG(kp) kp, AVG(score) score, SUM(badge='MVP') mvp, SUM(badge='ACE') ace
            FROM history WHERE {where}""", args)[0]
    counts = {r["grp"]: r["n"] for r in coach.db.query(
        "SELECT grp, COUNT(*) n FROM history WHERE puuid=? AND remake=0 GROUP BY grp", (coach.me.get("puuid"),))}
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


class CollectParams(BaseModel):
    target: int = 1000
    regions: list[str] = ["euw1", "kr"]
    days: int = 30
    timelines: bool = True


@app.post("/api/meta/collect")
async def meta_collect(params: CollectParams):
    if coach.jobs.get("meta", {}).get("running"):
        return {"ok": False, "error": "A collection is already running."}
    return {"ok": start_collect_job(max(1, min(params.target, 50000)), params.regions, params.days, params.timelines)}


@app.post("/api/jobs/{name}/stop")
async def job_stop(name: str):
    return {"ok": coach.stop_job(name)}


@app.post("/api/meta/crawl")
async def meta_crawl(params: CrawlParams):
    return {"ok": start_meta_job(params.regions, params.players, params.per_player, params.days,
                                 params.include_master, params.timelines, params.backfill)}


class MasteryParams(BaseModel):
    regions: list[str] = ["euw1", "kr"]


@app.post("/api/mains/crawl")
async def mains_crawl(params: MasteryParams):
    return {"ok": start_mastery_job(params.regions)}


@app.get("/api/my-mastery")
async def my_mastery(refresh: bool = False):
    """Os teus 10 bonecos com mais pontos de maestria (guardados 6 h para não gastar pedidos)."""
    await coach.resolve_me()
    if not coach.me:
        return {"ok": False, "error": "Unknown account: search for your Riot ID at the top of the page", "champions": []}
    cached = json.loads(coach.db.kv_get("my_mastery") or "{}")
    if refresh or cached.get("puuid") != coach.me["puuid"] or time.time() - cached.get("ts", 0) > 6 * 3600:
        try:
            masteries = await coach.riot.mastery_top(coach.me["puuid"], 10)
        except RiotAPIError as e:
            if not cached.get("list"):
                return {"ok": False, "error": str(e), "champions": []}
            masteries = cached["list"]
        else:
            cached = {"puuid": coach.me["puuid"], "ts": time.time(), "list": masteries}
            coach.db.kv_set("my_mastery", json.dumps(cached))
    dd = coach.dd
    return {"ok": True, "updated": cached.get("ts"), "champions": [
        {"id": m["championId"], "name": dd.champ_name(m["championId"]), "icon": dd.champ_icon(m["championId"]),
         "points": m["championPoints"], "level": m.get("championLevel")}
        for m in cached.get("list", [])]}


@app.get("/api/mains")
async def mains_list():
    counts = {r["champion_id"]: r for r in coach.db.query(
        """SELECT champion_id, COUNT(*) n, MAX(points) top FROM mains
           WHERE champion_id > 0 GROUP BY champion_id""")}
    players = coach.db.query("SELECT COUNT(DISTINCT puuid) n FROM mains")[0]["n"]
    champs = sorted(({"id": cid, "name": c["name"], "icon": coach.dd.champ_icon(cid),
                      "mains": counts.get(cid, {}).get("n", 0), "top": counts.get(cid, {}).get("top", 0)}
                     for cid, c in coach.dd.champs.items()), key=lambda c: c["name"])
    return {"champions": champs, "players": players,
            "regions": [r["platform"] for r in coach.db.query("SELECT DISTINCT platform FROM mains")],
            "updated": float(coach.db.kv_get("last_job:mastery") or coach.db.kv_get("auto:mastery") or 0) or None}


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
        return {"ok": False, "error": "Unknown account: search for your Riot ID at the top of the page"}
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


@app.get("/api/postgame/{match_id}")
async def postgame_get(match_id: str):
    return {"ok": True, "report": coach.db.postgame(match_id)}


# ---------------------------------------------------------------- perfil
@app.get("/api/profile")
async def profile(refresh: bool = False):
    try:
        p = await coach.refresh_profile(force=refresh)
    except RiotAPIError as e:
        p = coach.profile or {}
        if not p:
            return {"ok": False, "error": str(e)}
    if not p:
        return {"ok": False, "error": "Unknown account: search for your Riot ID at the top of the page"}
    return {"ok": True, **{k: v for k, v in p.items() if k != "puuid"}}


@app.get("/api/lp")
async def lp(queue: str = "solo"):
    if queue not in QUEUES or not coach.me:
        return {"points": []}
    data = lp_series(coach.db, coach.me["puuid"], queue)
    for pt in data["points"]:
        if pt.get("champion_id"):
            pt["icon"] = coach.dd.champ_icon(pt["champion_id"])
            pt["champion"] = coach.dd.champ_name(pt.pop("champion_id"))
    return data


# ---------------------------------------------------------------- explorar os dados guardados
def _last(name: str) -> float | None:
    return float(coach.db.kv_get(f"last_job:{name}") or coach.db.kv_get(f"auto:{name}") or 0) or None


@app.get("/api/data/overview")
async def data_overview():
    db = coach.db
    meta = db.query("""SELECT COUNT(*) n, SUM(has_timeline = 1) t, MIN(game_creation) first, MAX(game_creation) last
                       FROM matches WHERE kind='meta'""")[0]
    regions = db.query("""SELECT lower(substr(match_id, 1, instr(match_id, '_') - 1)) region, COUNT(*) n
                          FROM matches WHERE kind='meta' GROUP BY region ORDER BY n DESC""")
    patches = db.query("""SELECT patch, COUNT(*) n FROM matches WHERE kind='meta' AND patch IS NOT NULL
                          GROUP BY patch ORDER BY n DESC LIMIT 8""")
    patches.sort(key=lambda r: [int(x) for x in r["patch"].split(".") if x.isdigit()], reverse=True)
    me = (coach.me.get("puuid"),)
    mine = db.query("SELECT grp, COUNT(*) n FROM history WHERE puuid=? AND remake=0 GROUP BY grp", me)
    last_game = db.query("SELECT MAX(game_creation) t FROM history WHERE puuid=?", me)[0]["t"]
    return {
        "meta": {"games": meta["n"], "with_details": meta["t"] or 0, "first": meta["first"], "last": meta["last"],
                 "regions": regions, "patches": patches, "updated": _last("meta")},
        "mine": {"games": sum(r["n"] for r in mine), "by_queue": {r["grp"]: r["n"] for r in mine},
                 "last_game": last_game, "updated": _last("sync"), "season_start": config.SEASON_START},
        "auto": coach.db.kv_get("auto_enabled") != "0",
        "regions": list(config.META_PLATFORMS),
    }


def _data_where(source: str, region: str, role: str, patch: str):
    where, args = ["source=?"], ["me" if source == "me" else "meta"]
    if source == "me":  # só os jogos da conta ativa
        where.append("puuid=?")
        args.append(coach.me.get("puuid"))
    if region:
        where.append("lower(substr(match_id, 1, instr(match_id, '_') - 1))=?")
        args.append(region.lower())
    if role:
        where.append("position=?")
        args.append(role)
    if patch:
        where.append("patch=?")
        args.append(patch)
    return " AND ".join(where), args


@app.get("/api/data/champions")
async def data_champions(source: str = "meta", region: str = "", role: str = "", patch: str = ""):
    where, args = _data_where(source, region, role, patch)
    total = coach.db.query(f"SELECT COUNT(DISTINCT match_id) n FROM participants INDEXED BY idx_part_explore WHERE {where}", args)[0]["n"]
    rows = coach.db.query(
        f"""SELECT champion_id, COUNT(*) games, SUM(win) wins, AVG(kills) k, AVG(deaths) d, AVG(assists) a,
                   AVG(cs * 60.0 / duration) cspm, AVG(damage_share) dmg, AVG(kp) kp
            FROM participants INDEXED BY idx_part_explore WHERE {where} GROUP BY champion_id""", args)
    for r in rows:
        r["name"], r["icon"] = coach.dd.champ_name(r["champion_id"]), coach.dd.champ_icon(r["champion_id"])
        r["winrate"] = r["wins"] / r["games"]
        r["pickrate"] = r["games"] / total if total else 0
        r["kda"] = (r["k"] + r["a"]) / max(r["d"], 0.1)
    return {"total_games": total, "champions": rows}


@app.get("/api/data/champion/{cid}")
async def data_champion(cid: int, source: str = "meta", region: str = "", role: str = "", patch: str = ""):
    dd = coach.dd
    where, args = _data_where(source, region, role, patch)
    rwhere, rargs = _data_where(source, region, "", patch)  # todas as posições
    roles = coach.db.query(f"""SELECT position, COUNT(*) n, SUM(win) w FROM participants INDEXED BY idx_part_explore
                               WHERE {rwhere} AND champion_id=? GROUP BY position ORDER BY n DESC""", rargs + [cid])
    ups = coach.db.query(f"""SELECT opponent_id, COUNT(*) g, SUM(win) w FROM participants INDEXED BY idx_part_explore
                             WHERE {where} AND champion_id=? AND opponent_id > 0
                             GROUP BY opponent_id HAVING g >= 5""", args + [cid])
    for u in ups:
        u["name"], u["icon"], u["winrate"] = dd.champ_name(u["opponent_id"]), dd.champ_icon(u["opponent_id"]), u["w"] / u["g"]
    smooth = lambda u: (u["w"] + 5) / (u["g"] + 10)  # suavizado: poucas amostras ficam perto de 50%
    by_wr = sorted(ups, key=smooth)
    return {
        "champion": {"id": cid, "name": dd.champ_name(cid), "icon": dd.champ_icon(cid)},
        "roles": [{"position": r["position"], "games": r["n"], "winrate": r["w"] / r["n"]} for r in roles if r["position"]],
        "best": [u for u in by_wr[::-1] if smooth(u) > 0.5][:6], "worst": [u for u in by_wr if smooth(u) < 0.5][:6],
        "common": sorted(ups, key=lambda u: -u["g"])[:8],
    }


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
    enemy_bans: list[str] = []


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
        [c for c in (look(b) for b in p.enemy_bans) if c],
    )
    await coach.handle_session(session)
    return {"ok": True}


@app.post("/api/simulate/end")
async def simulate_end():
    await coach.on_lcu_event("/lol-champ-select/v1/session", None)
    return {"ok": True}

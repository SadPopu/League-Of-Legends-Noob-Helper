"""O jogo a começar: quem está lá, em que role joga cada um e como tem jogado.

A API da Riot diz os jogadores, bonecos, feitiços e runas de um jogo a decorrer, mas não diz a role.
A role vem, por esta ordem, do próprio jogo (Live Client Data, quando o jogo já abriu), do champ select
(só para a tua equipa) ou é calculada com os feitiços, as runas e os últimos jogos de cada jogador.
"""
import asyncio
import logging
from collections import Counter
from itertools import permutations
from math import log

import httpx

from .analysis import role_probabilities
from .config import POSITIONS
from .db import DB
from .ddragon import DDragon
from .profile import QUEUES, rank_view
from .riot_api import RiotAPI, RiotAPIError

log_ = logging.getLogger("ingame")

SMITE = 11
RIFT_MAP = 11
RECENT_GAMES = 20          # últimos jogos ranked vistos de cada jogador
MAX_NEW_MATCHES = 7        # 10 jogadores x 7 jogos cabe no limite de pedidos de uma personal key
MIN_GAME_SECONDS = 300     # abaixo disto foi remake
LIVE_URL = "https://127.0.0.1:2999/liveclientdata/playerlist"
LIVE_TEAMS = {"ORDER": 100, "CHAOS": 200}


def players_from_game(game: dict, me_puuid: str | None, dd: DDragon) -> list[dict]:
    players = []
    for p in game.get("participants", []):
        cid = p["championId"]
        key = dd.champs.get(cid, {}).get("key", "")
        keystone = ((p.get("perks") or {}).get("perkIds") or [0])[0]
        spells = [s for s in (p.get("spell1Id"), p.get("spell2Id")) if s]
        puuid = p.get("puuid")
        players.append({
            "puuid": puuid, "team": p.get("teamId"), "me": bool(puuid) and puuid == me_puuid,
            "name": p.get("riotId") if puuid else "Hidden player", "bot": bool(p.get("bot")),
            "champion_id": cid, "champion": {
                "id": cid, "name": dd.champ_name(cid), "icon": dd.champ_icon(cid),
                "art": f"https://ddragon.leagueoflegends.com/cdn/img/champion/loading/{key}_0.jpg" if key else None},
            "spell_ids": spells,
            "spells": [{"name": dd.spells.get(s, ""), "icon": dd.spell_icons.get(s, "")} for s in spells],
            "keystone_id": keystone,
            "keystone": {"name": dd.perks.get(keystone, ""), "icon": dd.perk_icons.get(keystone, "")} if keystone else None,
            "role": None, "role_source": None, "rank": None, "mastery": None, "perf": None,
            "loading": bool(puuid) and not p.get("bot"),
        })
    return players


def public_player(p: dict) -> dict:
    return {k: v for k, v in p.items() if k not in ("puuid", "spell_ids", "keystone_id")}


# ---------------------------------------------------------------- roles
def _role_scores(db: DB, dd: DDragon, p: dict) -> dict[str, float]:
    """Pontuação (log) de cada role para este jogador, com o boneco, os feitiços, a keystone e os últimos jogos."""
    cid = p["champion_id"]
    prior = role_probabilities(db, dd, [cid])[cid]
    spells = frozenset(p["spell_ids"])
    games, same_spells, same_keystone = Counter(), Counter(), Counter()
    for r in db.query("""SELECT position, spells, perks FROM participants WHERE source='meta' AND champion_id=?
                         ORDER BY game_creation DESC LIMIT 3000""", (cid,)):
        games[r["position"]] += 1
        same_spells[r["position"]] += frozenset(r["spells"]) == spells
        same_keystone[r["position"]] += bool(r["perks"]) and r["perks"][0] == p["keystone_id"]
    scores = {}
    for pos in POSITIONS:
        s = log(prior[pos])
        s += log((same_spells[pos] + 1) / (games[pos] + 10))
        s += log((same_keystone[pos] + 1) / (games[pos] + 10))
        perf = p.get("perf") or {}
        # o que esta pessoa costuma jogar, sobretudo com este boneco
        for counts, weight in ((perf.get("roles") or {}, 1), (perf.get("champ_roles") or {}, 2)):
            total = sum(counts.values())
            if total:
                s += weight * log((counts.get(pos, 0) + 1) / (total + 5))
        if (SMITE in spells) != (pos == "JUNGLE"):
            s -= 30  # o Smite decide o jungler
        scores[pos] = s
    return scores


def assign_roles(db: DB, dd: DDragon, players: list[dict], known: dict[tuple[int, int], tuple[str, str]]):
    """Dá uma role a cada jogador. `known` = {(equipa, boneco): (role, origem)} para as roles que já se sabem."""
    for team in {p["team"] for p in players}:
        side = [p for p in players if p["team"] == team]
        if len(side) != len(POSITIONS):
            continue
        scores = [_role_scores(db, dd, p) for p in side]
        fixed = [known.get((team, p["champion_id"])) for p in side]

        def total(perm):
            return sum((1000 if f and f[0] == pos else -1000 if f else s[pos])
                       for s, f, pos in zip(scores, fixed, perm))

        best = max(permutations(POSITIONS), key=total)
        for p, f, pos in zip(side, fixed, best):
            p["role"], p["role_source"] = pos, f[1] if f and f[0] == pos else "likely"


async def live_data(dd: DDragon) -> tuple[dict[tuple[int, int], tuple[str, str]], dict[tuple[int, int], int]]:
    """O que o próprio jogo diz: (roles, skins). Para as roles só conta uma equipa com as 5 preenchidas e diferentes."""
    try:
        async with httpx.AsyncClient(verify=False, timeout=3) as c:  # certificado da Riot, só em localhost
            r = await c.get(LIVE_URL)
        rows = r.json() if r.status_code == 200 else []
    except (httpx.HTTPError, ValueError):
        return {}, {}
    if not isinstance(rows, list):
        return {}, {}
    skins = {}
    for p in rows:
        cid = dd.champ_lookup(p.get("championName", ""))
        if cid and p.get("team") in LIVE_TEAMS:
            skins[(LIVE_TEAMS[p["team"]], cid)] = p.get("skinID") or 0
    known = {}
    for side_name, team in LIVE_TEAMS.items():
        side = [p for p in rows if p.get("team") == side_name]
        if {p.get("position") for p in side} != set(POSITIONS) or len(side) != len(POSITIONS):
            continue
        for p in side:
            cid = dd.champ_lookup(p.get("championName", ""))
            if cid:
                known[(team, cid)] = (p["position"], "game")
    return known, skins


async def apply_skins(dd: DDragon, players: list[dict], skins: dict[tuple[int, int], int]):
    async def one(p: dict):
        skin = skins.get((p["team"], p["champion_id"]))
        art = await dd.skin_art(p["champion_id"], skin) if skin else None
        if art:
            p["champion"]["art"] = art

    await asyncio.gather(*(one(p) for p in players))


# ---------------------------------------------------------------- dados de cada jogador
async def fetch_basics(riot: RiotAPI, p: dict):
    """Rank a solo e maestria com o boneco que está a jogar."""
    if not p["loading"]:
        return
    try:
        entries = await riot.league_entries_by_puuid(p["puuid"])
        solo = next((e for e in entries if e.get("queueType") == QUEUES["solo"]), None)
        p["rank"] = rank_view(solo)
        m = await riot.mastery_champion(p["puuid"], p["champion_id"])
        if m:
            p["mastery"] = {"level": m.get("championLevel"), "points": m.get("championPoints")}
    except (RiotAPIError, httpx.HTTPStatusError) as e:
        log_.warning("Player data: %s", e)


async def fetch_performance(riot: RiotAPI, db: DB, p: dict):
    """Os últimos jogos ranked: os que já estão na base de dados não gastam pedidos à Riot."""
    if not p["loading"]:
        return
    games = []
    try:
        ids = await riot.match_ids(p["puuid"], queue=QUEUES_RANKED_SOLO, count=RECENT_GAMES)
        saved = {r["match_id"]: r for r in db.query(
            f"""SELECT match_id, champion_id, position, win, kills, deaths, assists, duration FROM participants
                WHERE puuid=? AND match_id IN ({','.join('?' * len(ids))})""", (p["puuid"], *ids))} if ids else {}
        games = [r for r in saved.values()]
        for mid in [i for i in ids if i not in saved][:MAX_NEW_MATCHES]:
            match = await riot.match(mid)
            part = next((x for x in (match or {}).get("info", {}).get("participants", [])
                         if x.get("puuid") == p["puuid"]), None)
            if part:
                games.append({"champion_id": part["championId"], "position": part.get("teamPosition") or "",
                              "win": int(bool(part.get("win"))), "kills": part["kills"], "deaths": part["deaths"],
                              "assists": part["assists"], "duration": match["info"].get("gameDuration", 0)})
    except (RiotAPIError, httpx.HTTPStatusError) as e:
        log_.warning("Player games: %s", e)
    p["perf"] = summarize_games(games, p["champion_id"])
    p["loading"] = False


QUEUES_RANKED_SOLO = 420


def summarize_games(games: list[dict], champion_id: int) -> dict:
    games = [g for g in games if g["duration"] > MIN_GAME_SECONDS]
    on_champ = [g for g in games if g["champion_id"] == champion_id]
    champ = None
    if on_champ:
        wins = sum(g["win"] for g in on_champ)
        kills, deaths, assists = (sum(g[k] for g in on_champ) for k in ("kills", "deaths", "assists"))
        champ = {"games": len(on_champ), "wins": wins, "winrate": wins / len(on_champ),
                 "kda": (kills + assists) / max(deaths, 1)}
    return {"games": len(games), "wins": sum(g["win"] for g in games), "champ": champ,
            "roles": dict(Counter(g["position"] for g in games if g["position"])),
            "champ_roles": dict(Counter(g["position"] for g in on_champ if g["position"]))}

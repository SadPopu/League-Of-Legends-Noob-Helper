"""Histórico de jogos da season: guarda um resumo de cada jogo (os 10 jogadores) e
calcula um score de performance de 0 a 10 para cada jogador."""
import logging
from datetime import datetime

from .config import RANKED_QUEUES, SEASON_START
from .data import participant_rows, patch_of
from .db import DB
from .riot_api import RiotAPI

log = logging.getLogger("history")

QUEUE_NAMES = {
    420: "Ranked Solo/Duo", 440: "Ranked Flex", 400: "Normal Draft", 430: "Normal Blind",
    480: "Swiftplay", 490: "Quickplay", 450: "ARAM", 2400: "ARAM: Mayhem", 100: "ARAM",
    1700: "Arena", 1710: "Arena", 900: "URF", 1900: "URF", 1020: "One for All", 0: "Custom",
}
QUEUE_GROUPS = {
    "solo": {420},
    "flex": {440},
    "normal": {400, 430, 480, 490},
    "aram": {450, 2400, 100},
}
ARENA = {1700, 1710}

# pesos do score: cada métrica é comparada com o melhor valor dessa métrica no jogo
SCORE_WEIGHTS = {"kda": 0.25, "kp": 0.15, "damage": 0.20, "gold": 0.10, "vision": 0.10, "taken": 0.10, "objectives": 0.10}


def queue_group(queue_id: int) -> str:
    return next((g for g, qs in QUEUE_GROUPS.items() if queue_id in qs), "other")


def season_start_ts() -> int:
    return int(datetime.strptime(SEASON_START, "%Y-%m-%d").timestamp())


def summarise(match: dict) -> dict:
    info = match["info"]
    queue = info.get("queueId", 0)
    minutes = max(info["gameDuration"] / 60, 1)
    parts = info["participants"]
    team_key = (lambda p: p.get("playerSubteamId") or p["teamId"]) if queue in ARENA else (lambda p: p["teamId"])

    teams: dict = {}
    for p in parts:
        t = teams.setdefault(team_key(p), {"kills": 0, "damage": 0, "gold": 0, "taken": 0, "objectives": 0})
        t["kills"] += p["kills"]
        t["damage"] += p["totalDamageDealtToChampions"]
        t["gold"] += p["goldEarned"]
        t["taken"] += p["totalDamageTaken"]
        t["objectives"] += p.get("damageDealtToObjectives", 0)

    players = []
    for p in parts:
        t = teams[team_key(p)]
        perks = p.get("perks", {}).get("styles", [])
        players.append({
            "puuid": p["puuid"],
            "name": f"{p.get('riotIdGameName') or p.get('summonerName', '?')}#{p.get('riotIdTagline', '')}",
            "team": team_key(p), "win": p["win"], "position": p.get("teamPosition") or "",
            "placement": p.get("placement"),
            "champion_id": p["championId"], "level": p["champLevel"],
            "kills": p["kills"], "deaths": p["deaths"], "assists": p["assists"],
            "cs": p["totalMinionsKilled"] + p["neutralMinionsKilled"],
            "gold": p["goldEarned"], "damage": p["totalDamageDealtToChampions"],
            "taken": p["totalDamageTaken"], "vision": p["visionScore"],
            "wards": p.get("wardsPlaced", 0), "control_wards": p.get("visionWardsBoughtInGame", 0),
            "objectives": p.get("damageDealtToObjectives", 0),
            "kp": (p["kills"] + p["assists"]) / max(t["kills"], 1),
            "damage_share": p["totalDamageDealtToChampions"] / max(t["damage"], 1),
            "items": [p.get(f"item{i}", 0) for i in range(7)],
            "spells": [p["summoner1Id"], p["summoner2Id"]],
            "keystone": perks[0]["selections"][0]["perk"] if perks else None,
            "sub_style": perks[1]["style"] if len(perks) > 1 else None,
            "multikill": p.get("largestMultiKill", 0),
            "_shares": {
                "gold": p["goldEarned"] / max(t["gold"], 1),
                "taken": p["totalDamageTaken"] / max(t["taken"], 1),
                "objectives": p.get("damageDealtToObjectives", 0) / max(t["objectives"], 1),
            },
        })

    remake = info["gameDuration"] < 300 or any(p.get("gameEndedInEarlySurrender") for p in parts)
    if not remake:
        _score(players, minutes)
    for pl in players:
        pl.pop("_shares", None)

    return {
        "match_id": match["metadata"]["matchId"], "queue_id": queue, "patch": patch_of(match),
        "game_creation": info["gameCreation"], "duration": info["gameDuration"], "remake": remake,
        "players": players,
    }


def _score(players: list[dict], minutes: float):
    feats = []
    for p in players:
        feats.append({
            "kda": min((p["kills"] + p["assists"]) / max(p["deaths"], 1), 15),
            "kp": p["kp"], "damage": p["damage_share"], "gold": p["_shares"]["gold"],
            "vision": p["vision"] / minutes, "taken": p["_shares"]["taken"],
            "objectives": p["_shares"]["objectives"],
        })
    best = {k: max(f[k] for f in feats) or 1 for k in SCORE_WEIGHTS}
    raws = [sum(w * f[k] / best[k] for k, w in SCORE_WEIGHTS.items()) for f in feats]
    top = max(raws) or 1
    for p, raw in zip(players, raws):
        p["score"] = round(10 * raw / top, 1)
        p["badge"] = None
    # MVP = melhor da equipa que ganhou; ACE = melhor da equipa que perdeu
    for won, badge in ((True, "MVP"), (False, "ACE")):
        group = [p for p in players if p["win"] == won]
        if group:
            max(group, key=lambda p: p["score"])["badge"] = badge


async def sync_history(riot: RiotAPI, db: DB, puuid: str, progress=None) -> int:
    """Vai buscar todos os teus jogos desde o início da season (todas as filas)."""
    since = season_start_ts()
    ids: list[str] = []
    start = 0
    while True:
        batch = await riot.match_ids(puuid, start=start, count=100, start_time=since)
        ids += batch
        if progress:
            await progress("listing games", len(ids), len(ids))
        if len(batch) < 100:
            break
        start += 100
    new = [m for m in ids if not db.has_history(m, puuid)]
    for i, mid in enumerate(new, 1):
        match = await riot.match(mid)
        if match:
            store_match(db, match, puuid)
        if progress:
            await progress("games", i, len(new))
    return len(new)


def store_match(db: DB, match: dict, puuid: str):
    """Guarda o jogo no histórico e, se for ranked, também nas tuas estatísticas por boneco."""
    summary = summarise(match)
    me = next((p for p in summary["players"] if p["puuid"] == puuid), None)
    if not me:
        return
    db.save_history(summary, me, queue_group(summary["queue_id"]))
    info = match["info"]
    mid = summary["match_id"]
    known = db.query("SELECT 1 FROM participants WHERE match_id=? AND puuid=? AND source='me'", (mid, puuid))
    if info.get("queueId") in RANKED_QUEUES and not summary["remake"] and not known:
        db.save_match(mid, "me", info["gameCreation"], patch_of(match),
                      participant_rows(match, "me", only_puuid=puuid))

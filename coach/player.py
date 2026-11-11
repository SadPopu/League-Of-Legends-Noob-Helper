"""Examinar outro jogador: os jogos vêm da Riot à medida que se pedem, 20 de cada vez.

Ficam numa tabela à parte (viewed_history) para não se misturarem com os jogos e as estatísticas da tua conta.
"""
import asyncio
import json
import logging

import httpx

from .db import DB
from .history import QUEUE_GROUPS, queue_group, season_start_ts, summarise
from .riot_api import RiotAPI, RiotAPIError

log = logging.getLogger("player")

OTHER_QUEUES = {1700, 1710, 900, 1900, 1020}
MAX_IDS = 100  # o máximo que a Riot devolve de uma vez
STATS_GAMES = 40


def _queues(group: str) -> set[int] | None:
    return OTHER_QUEUES if group == "other" else QUEUE_GROUPS.get(group)


async def match_page(api: RiotAPI, puuid: str, group: str, offset: int, limit: int) -> list[str]:
    """Os ids de uma página do histórico, do mais recente para o mais antigo."""
    since = season_start_ts()
    queues = _queues(group)
    if not queues:
        return await api.match_ids(puuid, start=offset, count=limit, start_time=since)
    if len(queues) == 1:
        return await api.match_ids(puuid, queue=next(iter(queues)), start=offset, count=limit, start_time=since)
    # a Riot só filtra por uma fila de cada vez: junta as várias e ordena (o número do jogo cresce com o tempo)
    wanted = min(offset + limit, MAX_IDS)
    lists = await asyncio.gather(*(api.match_ids(puuid, queue=q, count=wanted, start_time=since) for q in queues))
    merged = sorted({m for ids in lists for m in ids}, key=lambda m: int(m.rsplit("_", 1)[1]), reverse=True)
    return merged[offset:offset + limit]


def _player_in(summary: dict | None, puuid: str) -> dict | None:
    return next((p for p in summary["players"] if p["puuid"] == puuid), None) if summary else None


async def store_games(api: RiotAPI, db: DB, puuid: str, ids: list[str]):
    """Guarda os jogos que faltam. Um jogo que falhe não impede de mostrar os outros."""
    async def one(match_id: str):
        if db.has_viewed(match_id, puuid):
            return
        summary = db.history_detail(match_id)
        try:
            if not _player_in(summary, puuid):  # jogos guardados com outra chave da Riot têm outros PUUIDs
                match = await api.match(match_id)
                summary = summarise(match) if match else None
        except (RiotAPIError, httpx.HTTPStatusError, KeyError, IndexError) as e:
            log.warning("Game %s: %r", match_id, e)
            return
        player = _player_in(summary, puuid)
        if player:
            db.save_history(summary, player, queue_group(summary["queue_id"]), table="viewed_history")

    await asyncio.gather(*(one(m) for m in ids))


def viewed_stats(db: DB, puuid: str, position: str | None) -> tuple[list[dict], int]:
    """Estatísticas por boneco dos jogos ranked já carregados: (linhas, nº de jogos)."""
    by_champ: dict[int, list[tuple[dict, int]]] = {}
    for r in db.query("SELECT data FROM viewed_history WHERE puuid=? AND grp IN ('solo', 'flex') AND remake=0", (puuid,)):
        d = json.loads(r["data"])
        p = _player_in(d, puuid)
        if p and (not position or p["position"] == position):
            by_champ.setdefault(p["champion_id"], []).append((p, d["duration"]))
    rows = []
    for cid, games in by_champ.items():
        n = len(games)
        avg = lambda f: sum(f(p, secs) for p, secs in games) / n
        wins = sum(p["win"] for p, _ in games)
        k, d, a = (avg(lambda p, _s, key=key: p[key]) for key in ("kills", "deaths", "assists"))
        rows.append({"champion_id": cid, "games": n, "wins": wins, "winrate": wins / n, "kda": (k + a) / max(d, 1),
                     "cspm": avg(lambda p, s: p["cs"] * 60 / s), "vspm": avg(lambda p, s: p["vision"] * 60 / s),
                     "kp": avg(lambda p, _s: p["kp"]), "dmg_share": avg(lambda p, _s: p["damage_share"])})
    rows.sort(key=lambda r: r["games"], reverse=True)
    return rows, sum(r["games"] for r in rows)

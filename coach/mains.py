"""Mains: os jogadores Challenger/Grandmaster com mais pontos de maestria em cada boneco
(EUW + KR), e as runas/builds que usam nos seus jogos."""
import logging
import time
from collections import Counter

from .analysis import aggregate_build, wr
from .data import participant_rows, patch_of, timeline_extras
from .db import DB
from .ddragon import DDragon
from .riot_api import RiotAPI, RiotAPIError

log = logging.getLogger("mains")

REFRESH_DAYS = 1          # maestrias de cada jogador são revistas uma vez por dia
GAMES_REFRESH_HOURS = 12  # jogos dos mains de um boneco são atualizados no máximo 2x por dia
REGION_NAMES = {"euw1": "EUW", "kr": "KR", "eun1": "EUNE", "na1": "NA"}


async def crawl_mastery(riot: RiotAPI, db: DB, tiers=("challenger", "grandmaster"), top: int = 10,
                        progress=None, label: str = "", max_players: int | None = None) -> int:
    """Revê a lista de mains da região.

    1. Lê a lista atual de Challenger/GM: atualiza tier e LP de todos e remove quem já
       não está lá (assim o top 5 de cada boneco só tem jogadores que ainda são apex).
    2. Para cada jogador não revisto há REFRESH_DAYS dias, guarda os `top` bonecos com
       mais maestria (1 pedido por jogador).
    """
    players = []
    for tier in tiers:
        league = await riot.apex_league(tier)
        players += [(e["puuid"], tier.upper(), e.get("leaguePoints", 0))
                    for e in (league or {}).get("entries", []) if e.get("puuid")]
    if not players:
        return 0
    db.sync_apex(riot.platform, players)

    fresh = time.time() - REFRESH_DAYS * 86400
    recent = {r["puuid"] for r in db.query("SELECT DISTINCT puuid FROM mains WHERE updated > ?", (fresh,))}
    todo = [p for p in players if p[0] not in recent][:max_players]
    for i, (puuid, tier, lp) in enumerate(todo, 1):
        masteries = await riot.mastery_top(puuid, top)
        db.save_mains(puuid, riot.platform, tier, lp, [(m["championId"], m["championPoints"]) for m in masteries])
        if progress:
            await progress(label, "masteries", i, len(todo))
    return len(todo)


async def ensure_names(apis: dict[str, RiotAPI], db: DB, mains: list[dict]):
    """Vai buscar o Riot ID (Nome#TAG) dos mains que ainda não tem nome guardado."""
    for m in mains:
        if m.get("name"):
            continue
        api = apis.get(m["platform"])
        if not api:
            continue
        try:
            acc = await api.account_by_puuid(m["puuid"])
        except RiotAPIError:
            continue
        if acc:
            m["name"] = f"{acc.get('gameName', '?')}#{acc.get('tagLine', '')}"
            db.save_account(m["puuid"], m["name"])


def top_mains(db: DB, champ: int, limit: int = 5, sort: str = "points") -> list[dict]:
    order = "m.lp DESC" if sort == "lp" else "m.points DESC"
    return db.query(
        f"""SELECT m.puuid, m.platform, m.tier, m.lp, m.points, a.name
            FROM mains m LEFT JOIN accounts a ON a.puuid = m.puuid
            WHERE m.champion_id=? ORDER BY {order} LIMIT ?""", (champ, limit))


def mains_report(db: DB, dd: DDragon, champ: int, mains: list[dict], position: str | None = None) -> dict:
    """Build agregada dos jogos dos mains com este boneco, e a build de cada um.
    As runas e itens são sempre de uma só role: a pedida ou, sem ela, a mais jogada (de todos ou de cada main)."""
    puuids = [m["puuid"] for m in mains]
    rows = db.query(
        f"""SELECT * FROM participants WHERE source='meta' AND champion_id=?
            AND puuid IN ({','.join('?' * len(puuids))})""", (champ, *puuids)) if puuids else []
    pos = position or (Counter(r["position"] for r in rows).most_common(1)[0][0] if rows else None)

    players = []
    for m in mains:
        mine = [r for r in rows if r["puuid"] == m["puuid"]]
        items = Counter(i for r in mine for i in set(r["items"]) if dd.is_completed_item(i) and not dd.is_boots(i))
        k, d, a = (sum(r[x] for r in mine) for x in ("kills", "deaths", "assists"))
        positions = Counter(r["position"] for r in mine)
        own_pos = position or (positions.most_common(1)[0][0] if positions else None)
        build = aggregate_build([r for r in mine if r["position"] == own_pos], dd, own_pos)
        players.append({
            **m,
            "region": REGION_NAMES.get(m["platform"], m["platform"]),
            "games": len(mine), "winrate": wr(sum(r["win"] for r in mine), len(mine)) if mine else None,
            "kda": (k + a) / max(d, 1) if mine else None,
            "position": positions.most_common(1)[0][0] if positions else None,
            "keystone": ({"name": dd.perks.get(build["runes"]["perk_ids"][0], ""),
                          "icon": dd.perk_icons.get(build["runes"]["perk_ids"][0], "")} if build["runes"] else None),
            "items": [{"name": dd.item_name(i), "icon": dd.item_icon(i)} for i, _ in items.most_common(4)],
            "build": build,
        })

    return {"champion": {"id": champ, "name": dd.champ_name(champ), "icon": dd.champ_icon(champ)},
            "mains": players, "last_refresh": db.kv_get(f"mains_games:{champ}"), "position": pos,
            **aggregate_build([r for r in rows if r["position"] == pos], dd, pos)}


def games_stale(db: DB, champ: int) -> bool:
    last = db.kv_get(f"mains_games:{champ}")
    return not last or time.time() - float(last) > GAMES_REFRESH_HOURS * 3600


async def refresh_main_games(apis: dict[str, RiotAPI], db: DB, champ: int, mains: list[dict],
                             per_player: int = 20, progress=None) -> int:
    """Descarrega os jogos ranked recentes dos mains (com timeline quando jogaram o boneco)."""
    db.kv_set(f"mains_games:{champ}", str(time.time()))
    saved = 0
    for i, m in enumerate(mains, 1):
        api = apis.get(m["platform"])
        if not api:
            continue
        ids = await api.match_ids(m["puuid"], queue=420, count=per_player)
        new = [mid for mid in ids if not db.has_match(mid)]
        for j, mid in enumerate(new, 1):
            match = await api.match(mid)
            if not match:
                continue
            if match["info"].get("gameDuration", 0) <= 900:
                db.save_match(mid, "skip", match["info"]["gameCreation"], patch_of(match), [])
                continue
            on_champ = any(p["puuid"] == m["puuid"] and p["championId"] == champ for p in match["info"]["participants"])
            timeline = await api.timeline(mid) if on_champ else None
            extras = timeline_extras(match, timeline) if timeline else None
            db.save_match(mid, "meta", match["info"]["gameCreation"], patch_of(match),
                          participant_rows(match, "meta", extras=extras), has_timeline=bool(extras))
            saved += 1
            if progress:
                await progress(f"main {i}/{len(mains)} games", j, len(new))
    return saved

"""Recolha de dados: os teus jogos ranked e jogos de jogadores high elo (meta)."""
import logging
import time

from .db import DB
from .riot_api import RiotAPI

log = logging.getLogger("data")

SKILLS = {1: "Q", 2: "W", 3: "E", 4: "R"}

# Campos de jungle no Summoner's Rift (coordenadas aproximadas). Os da equipa azul (100)
# ficam em baixo/esquerda; os da vermelha (200) em cima/direita.
CAMPS = [
    ("Blue buff", 100, 3821, 8101), ("Gromp", 100, 2288, 8448), ("Wolves", 100, 3783, 6495),
    ("Raptors", 100, 7061, 5325), ("Red buff", 100, 7762, 4011), ("Krugs", 100, 8394, 2641),
    ("Blue buff", 200, 11032, 6990), ("Gromp", 200, 12703, 6443), ("Wolves", 200, 11008, 8387),
    ("Raptors", 200, 7852, 9471), ("Red buff", 200, 7101, 10903), ("Krugs", 200, 6317, 12146),
    ("Scuttle top", 0, 4400, 9600), ("Scuttle bot", 0, 10500, 5000),
]


def patch_of(match: dict) -> str:
    return ".".join(match["info"].get("gameVersion", "0.0").split(".")[:2])


def nearest_camp(pos: dict, team: int, max_dist: float = 2000) -> str | None:
    best, best_d = None, max_dist
    for name, side, x, y in CAMPS:
        d = ((pos["x"] - x) ** 2 + (pos["y"] - y) ** 2) ** 0.5
        if d < best_d:
            best_d = d
            best = name if side in (0, team) else f"{name} (enemy)"
    return best


def map_side(pos: dict) -> str:
    """Divide o mapa pela diagonal: acima = lado top, abaixo = lado bot."""
    d = pos["y"] - pos["x"]
    return "top" if d > 2500 else "bot" if d < -2500 else "mid"


def timeline_extras(match: dict, timeline: dict) -> dict[str, dict]:
    """Por jogador: itens comprados por ordem, ordem das skills e pathing (junglers)."""
    parts = {p["participantId"]: p for p in match["info"]["participants"]}
    purchases = {pid: [] for pid in parts}
    skills = {pid: "" for pid in parts}
    first_gank = {}
    frames = timeline["info"]["frames"]

    for frame in frames:
        for ev in frame["events"]:
            pid, t = ev.get("participantId"), ev["timestamp"]
            if ev["type"] == "ITEM_PURCHASED" and pid in purchases:
                purchases[pid].append([ev["itemId"], round(t / 60000, 1)])
            elif ev["type"] == "ITEM_UNDO" and pid in purchases and ev.get("beforeId"):
                for i in range(len(purchases[pid]) - 1, -1, -1):
                    if purchases[pid][i][0] == ev["beforeId"]:
                        purchases[pid].pop(i)
                        break
            elif ev["type"] == "SKILL_LEVEL_UP" and pid in skills and ev.get("levelUpType") == "NORMAL":
                skills[pid] += SKILLS.get(ev["skillSlot"], "")
            elif ev["type"] == "CHAMPION_KILL" and t < 10 * 60000 and "position" in ev:
                for p in [ev.get("killerId"), *ev.get("assistingParticipantIds", [])]:
                    if p in parts and p not in first_gank:
                        first_gank[p] = {"lane": map_side(ev["position"]), "minute": round(t / 60000, 1)}

    out = {}
    for pid, p in parts.items():
        jungle = None
        if p.get("teamPosition") == "JUNGLE":
            pos = lambda m: frames[m]["participantFrames"][str(pid)]["position"] if m < len(frames) else None
            team = p["teamId"]
            jungle = {
                "start": nearest_camp(pos(1), team) if pos(1) else None,       # 1:00 = à espera do 1.º campo
                "at2": nearest_camp(pos(2), team, 2500) if pos(2) else None,  # 2:00
                "side3": map_side(pos(3)) if pos(3) else None,                 # 3:00 ~ fim do full clear
                "first_gank": first_gank.get(pid),
                "team": team,
                "path": [[pos(m)["x"], pos(m)["y"]] for m in range(0, 8) if pos(m)],
            }
        out[p["puuid"]] = {"purchases": purchases[pid], "skill_order": skills[pid][:18], "jungle": jungle}
    return out


def participant_rows(match: dict, source: str, only_puuid: str | None = None,
                     extras: dict[str, dict] | None = None) -> list[dict]:
    info = match["info"]
    parts = info["participants"]
    duration = info["gameDuration"]
    team_kills = {t: sum(p["kills"] for p in parts if p["teamId"] == t) for t in (100, 200)}
    team_dmg = {t: sum(p["totalDamageDealtToChampions"] for p in parts if p["teamId"] == t) for t in (100, 200)}
    rows = []
    for p in parts:
        if only_puuid and p["puuid"] != only_puuid:
            continue
        pos = p.get("teamPosition") or ""
        if not pos:
            continue  # remakes / posições inválidas
        opp = next((o for o in parts if o["teamId"] != p["teamId"] and o.get("teamPosition") == pos), None)
        styles = p.get("perks", {}).get("styles", [])
        stat = p.get("perks", {}).get("statPerks", {})
        perks = [s["perk"] for st in styles for s in st["selections"]]
        perks += [stat.get("offense"), stat.get("flex"), stat.get("defense")]
        row = {
            "match_id": match["metadata"]["matchId"], "puuid": p["puuid"], "source": source,
            "champion_id": p["championId"], "position": pos,
            "opponent_id": opp["championId"] if opp else None, "win": int(p["win"]),
            "kills": p["kills"], "deaths": p["deaths"], "assists": p["assists"],
            "cs": p["totalMinionsKilled"] + p["neutralMinionsKilled"], "duration": duration,
            "gold": p["goldEarned"], "damage": p["totalDamageDealtToChampions"], "vision": p["visionScore"],
            "damage_share": p["totalDamageDealtToChampions"] / max(team_dmg[p["teamId"]], 1),
            "kp": (p["kills"] + p["assists"]) / max(team_kills[p["teamId"]], 1),
            "game_creation": info["gameCreation"], "patch": patch_of(match),
            "primary_style": styles[0]["style"] if styles else None,
            "sub_style": styles[1]["style"] if len(styles) > 1 else None,
            "perks": perks,
            "spells": sorted([p["summoner1Id"], p["summoner2Id"]]),
            "items": [p[f"item{i}"] for i in range(6) if p[f"item{i}"]],
        }
        if extras and p["puuid"] in extras:
            row.update(extras[p["puuid"]])
        rows.append(row)
    return rows


async def crawl_meta(riot: RiotAPI, db: DB, tiers=("challenger", "grandmaster"), players: int = 60,
                     per_player: int = 15, days: int = 14, timelines: bool = True, backfill: int = 300,
                     progress=None, label: str = "") -> int:
    """Recolhe jogos Solo/Duo recentes dos melhores jogadores do servidor.

    Cada jogo dá 10 linhas (campeão, posição, adversário de lane, runas, itens).
    Com timelines=True também guarda a ordem de compra, a ordem das skills e o pathing
    (custa 1 pedido extra por jogo) e completa até `backfill` jogos antigos sem timeline.
    """
    entries = []
    for tier in tiers:
        league = await riot.apex_league(tier)
        entries += (league or {}).get("entries", [])
    entries.sort(key=lambda e: e.get("leaguePoints", 0), reverse=True)
    puuids = [e["puuid"] for e in entries if e.get("puuid")][:players]

    since = int(time.time()) - days * 86400
    match_ids: list[str] = []
    for i, puuid in enumerate(puuids, 1):
        match_ids += await riot.match_ids(puuid, queue=420, count=per_player, start_time=since)
        if progress:
            await progress(label, "players", i, len(puuids))
    new = [m for m in dict.fromkeys(match_ids) if not db.has_match(m)]
    saved = 0
    for i, mid in enumerate(new, 1):
        match = await riot.match(mid)
        if match and match["info"].get("gameDuration", 0) > 900:
            timeline = await riot.timeline(mid) if timelines else None
            extras = timeline_extras(match, timeline) if timeline else None
            db.save_match(mid, "meta", match["info"]["gameCreation"], patch_of(match),
                          participant_rows(match, "meta", extras=extras), has_timeline=bool(extras))
            saved += 1
        elif match:
            # jogo curto (remake/FF15): regista-o para não o voltar a descarregar
            db.save_match(mid, "skip", match["info"]["gameCreation"], patch_of(match), [])
        if progress:
            await progress(label, "games", i, len(new))

    if timelines and backfill:
        # só jogos desta região (o ID do jogo começa pela plataforma, p.ex. "KR_" ou "EUW1_")
        prefix = riot.platform.upper() + "_"
        old = [m for m in db.matches_without_timeline("meta", 100000) if m.startswith(prefix)][:backfill]
        for i, mid in enumerate(old, 1):
            match, timeline = await riot.match(mid), await riot.timeline(mid)
            if match and timeline:
                db.save_timeline_extras(mid, timeline_extras(match, timeline))
            else:
                db.mark_timeline_unavailable(mid)
            if progress:
                await progress(label, "old game timelines", i, len(old))
    return saved

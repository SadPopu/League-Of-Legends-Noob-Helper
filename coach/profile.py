"""O teu rank e a evolução dos LP ao longo da season.

A Riot não guarda o histórico de LP: só dá o rank atual. A app regista o rank sempre que o vê
mudar (tabela lp_history) e, para trás disso, estima os LP jogo a jogo a partir das vitórias e
derrotas do histórico, com o ganho/perda médio observado entre registos.
"""
from statistics import median

from .db import DB
from .history import season_start_ts

QUEUES = {"solo": "RANKED_SOLO_5x5", "flex": "RANKED_FLEX_SR"}
TIERS = ["IRON", "BRONZE", "SILVER", "GOLD", "PLATINUM", "EMERALD", "DIAMOND", "MASTER", "GRANDMASTER", "CHALLENGER"]
DIVISIONS = {"IV": 0, "III": 1, "II": 2, "I": 3}
APEX = TIERS.index("MASTER")
DEFAULT_GAIN, DEFAULT_LOSS = 20, -20


def total_lp(tier: str, division: str, lp: int) -> int:
    """LP numa escala única (Iron IV 0 LP = 0; cada divisão = 100; Master+ partilham a mesma escala)."""
    i = TIERS.index(tier) if tier in TIERS else 0
    if i >= APEX:
        return APEX * 400 + lp
    return i * 400 + DIVISIONS.get(division, 0) * 100 + lp


def rank_view(entry: dict | None) -> dict | None:
    if not entry:
        return None
    games = entry["wins"] + entry["losses"]
    return {"tier": entry["tier"], "division": entry["rank"], "lp": entry["leaguePoints"],
            "wins": entry["wins"], "losses": entry["losses"], "winrate": entry["wins"] / games if games else None,
            "total": total_lp(entry["tier"], entry["rank"], entry["leaguePoints"])}


def _one_game_gaps(snaps: list[dict]):
    """Pares de registos seguidos separados por um único jogo: (registo antes, registo depois, ganhou, LP)."""
    for a, b in zip(snaps, snaps[1:]):
        dw, dl = b["wins"] - a["wins"], b["losses"] - a["losses"]
        if dw + dl == 1 and abs(b["v"] - a["v"]) < 60:  # mais do que isto é um dodge pelo meio
            yield a, b, bool(dw), b["v"] - a["v"]


def _typical_lp(snaps: list[dict]) -> tuple[int | None, int | None]:
    """Ganho e perda típicos observados nos registos (None se ainda não houver nenhum)."""
    gains = [lp for *_, won, lp in _one_game_gaps(snaps) if won]
    losses = [lp for *_, won, lp in _one_game_gaps(snaps) if not won]
    return round(median(gains)) if gains else None, round(median(losses)) if losses else None


def game_lp(db: DB, puuid: str) -> dict[str, dict]:
    """LP ganhos/perdidos por jogo: {match_id: {"lp", "exact"}}.

    Exato quando há um registo do rank antes e depois desse jogo; os outros jogos ficam com o ganho/perda
    típico da fila. Sem nada observado na fila, os jogos ficam de fora em vez de mostrarem um valor inventado.
    """
    result = {}
    for queue, queue_type in QUEUES.items():
        snaps = db.query(
            "SELECT ts, tier, division, lp, wins, losses FROM lp_history WHERE puuid=? AND queue=? ORDER BY ts",
            (puuid, queue_type))
        for s in snaps:
            s["v"] = total_lp(s["tier"], s["division"], s["lp"])
        games = db.query(
            """SELECT match_id, game_creation + duration * 1000 AS t, win FROM history
               WHERE puuid=? AND grp=? AND remake=0 AND game_creation >= ?""",
            (puuid, queue, season_start_ts() * 1000))
        gain, loss = _typical_lp(snaps)
        for g in games:
            typical = gain if g["win"] else loss
            if typical is not None:
                result[g["match_id"]] = {"lp": typical, "exact": False}
        for a, b, won, lp in _one_game_gaps(snaps):
            between = [g for g in games if a["ts"] < g["t"] <= b["ts"]]
            if len(between) == 1 and bool(between[0]["win"]) == won:
                result[between[0]["match_id"]] = {"lp": lp, "exact": True}
    return result


def lp_series(db: DB, puuid: str, queue: str) -> dict:
    """Pontos (tempo, LP total) desde o início da season: exatos onde há registos, estimados no resto."""
    snaps = db.query(
        "SELECT ts, tier, division, lp, wins, losses FROM lp_history WHERE puuid=? AND queue=? ORDER BY ts",
        (puuid, QUEUES[queue]))
    if not snaps:
        return {"points": [], "gain": None, "loss": None}
    for s in snaps:
        s["v"] = total_lp(s["tier"], s["division"], s["lp"])
    games = db.query(
        """SELECT game_creation + duration * 1000 AS t, win, champion_id FROM history
           WHERE puuid=? AND grp=? AND remake=0 AND game_creation >= ? ORDER BY t""",
        (puuid, queue, season_start_ts() * 1000))

    gain, loss = _typical_lp(snaps)
    gain = DEFAULT_GAIN if gain is None else gain
    loss = DEFAULT_LOSS if loss is None else loss
    delta = lambda g: gain if g["win"] else loss

    last = snaps[-1]
    points = [{"t": s["ts"], "v": s["v"], "exact": True} for s in snaps]
    # jogos depois do último registo: para a frente
    v = last["v"]
    for g in (g for g in games if g["t"] > last["ts"]):
        v += delta(g)
        points.append({"t": g["t"], "v": v, "exact": False, "win": g["win"], "champion_id": g["champion_id"]})
    # jogos antes: para trás, a recomeçar no valor exato de cada registo que se encontra
    older = [g for g in games if g["t"] <= last["ts"]][::-1]
    snaps_desc = snaps[::-1]
    si, v = 0, last["v"]
    for g in older:
        reset = False
        while si + 1 < len(snaps_desc) and snaps_desc[si + 1]["ts"] >= g["t"]:
            si += 1
            v, reset = snaps_desc[si]["v"], True
        points.append({"t": g["t"], "v": v, "exact": reset, "win": g["win"], "champion_id": g["champion_id"]})
        v -= delta(g)
    if older:  # LP antes do primeiro jogo da season
        points.append({"t": older[-1]["t"] - 1800_000, "v": v, "exact": False})
    points.sort(key=lambda p: p["t"])
    return {"points": points, "gain": gain, "loss": loss}

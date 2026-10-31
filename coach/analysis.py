"""Motor de regras do coach: sugestões de pick e de build (runas, feitiços, itens)."""
import json
from collections import Counter
from itertools import permutations

from .champselect import ChampSelectState
from .config import POSITIONS
from .db import DB
from .ddragon import DDragon

# Campeões com muita cura/regeneração (chave do Data Dragon) -> recomenda anti-heal
HEALERS = {
    "Soraka", "Yuumi", "Aatrox", "Vladimir", "Sylas", "Warwick", "DrMundo", "Fiddlesticks",
    "Swain", "Briar", "Nami", "Sona", "Seraphine", "Illaoi", "Olaf", "Volibear", "Milio",
    "Kayn", "Mordekaiser", "Trundle", "Maokai", "Zac", "Nidalee", "Irelia", "Yone",
}

MIN_MATCHUP_GAMES = 15

# Role "provável" pela classe do campeão, usada quando há poucos dados meta
TAG_ROLE = {"Marksman": "BOTTOM", "Support": "UTILITY", "Mage": "MIDDLE",
            "Assassin": "MIDDLE", "Fighter": "TOP", "Tank": "TOP"}


def wr(wins: float, games: float) -> float:
    return wins / games if games else 0.0


# ---------------------------------------------------------------- estatísticas
def my_champion_stats(db: DB, position: str | None = None) -> list[dict]:
    where = "source='me'" + (" AND position=?" if position else "")
    rows = db.query(
        f"""SELECT champion_id, COUNT(*) games, SUM(win) wins,
                   AVG(kills) k, AVG(deaths) d, AVG(assists) a,
                   AVG(cs*60.0/duration) cspm, AVG(vision*60.0/duration) vspm,
                   AVG(damage_share) dmg_share, AVG(kp) kp, MAX(game_creation) last_played
            FROM participants WHERE {where} GROUP BY champion_id ORDER BY games DESC""",
        (position,) if position else (),
    )
    for r in rows:
        r["winrate"] = wr(r["wins"], r["games"])
        r["kda"] = (r["k"] + r["a"]) / max(r["d"], 1)
    return rows


def matchup(db: DB, source: str, champ: int, opp: int, position: str | None = None) -> tuple[int, float]:
    sql = "SELECT COUNT(*) g, SUM(win) w FROM participants WHERE source=? AND champion_id=? AND opponent_id=?"
    args = [source, champ, opp]
    if position:
        sql += " AND position=?"
        args.append(position)
    r = db.query(sql, args)[0]
    return r["g"], wr(r["w"] or 0, r["g"])


def role_probabilities(db: DB, dd: DDragon, champ_ids: list[int]) -> dict[int, dict[str, float]]:
    out = {}
    for cid in champ_ids:
        counts = {r["position"]: r["g"] for r in db.query(
            "SELECT position, COUNT(*) g FROM participants WHERE champion_id=? GROUP BY position", (cid,))}
        prior = {p: 1.0 for p in POSITIONS}
        for i, tag in enumerate(dd.champs.get(cid, {}).get("tags", [])):
            if tag in TAG_ROLE:
                prior[TAG_ROLE[tag]] += 3 if i == 0 else 1
        total = sum(counts.values()) + sum(prior.values())
        out[cid] = {p: (counts.get(p, 0) + prior[p]) / total for p in POSITIONS}
    return out


def guess_enemy_roles(db: DB, dd: DDragon, enemies: list[int]) -> dict[str, int]:
    """Em ranked não vês a posição dos inimigos: adivinha pela distribuição de roles no meta."""
    if not enemies:
        return {}
    probs = role_probabilities(db, dd, enemies)
    best, best_score = None, -1.0
    for perm in permutations(POSITIONS, len(enemies)):
        score = 1.0
        for cid, pos in zip(enemies, perm):
            score *= probs[cid][pos]
        if score > best_score:
            best, best_score = perm, score
    return {pos: cid for cid, pos in zip(enemies, best)}


# ---------------------------------------------------------------- picks
def recommend_picks(state: ChampSelectState, db: DB, dd: DDragon, limit: int = 5) -> dict:
    pos = state.my_position or None
    enemy_roles = guess_enemy_roles(db, dd, state.enemies)
    lane_opp = enemy_roles.get(pos) if pos else None
    unavailable = state.unavailable()

    ally_champs = [a["champion"] for a in state.allies if a["champion"] and not a["me"]]
    ally_ad = sum(dd.champs.get(c, {}).get("damage") == "AD" for c in ally_champs)
    ally_ap = len(ally_champs) - ally_ad
    ally_has_tank = any("Tank" in dd.champs.get(c, {}).get("tags", []) for c in ally_champs)

    pool = my_champion_stats(db, pos) or my_champion_stats(db)
    # bonecos com 1–2 jogos não dizem nada: só entram se não houver alternativas suficientes
    played = [c for c in pool if c["games"] >= MIN_POOL_GAMES and c["champion_id"] not in unavailable]
    # os teus 10 bonecos com mais maestria também contam como "teus" se se jogam nesta posição
    mastery = my_mastery_ranks(db)
    if mastery:
        have = {c["champion_id"] for c in played}
        overall = {c["champion_id"]: c for c in my_champion_stats(db)}
        probs = role_probabilities(db, dd, [cid for cid in mastery if cid not in have])
        for cid in mastery:
            if cid in have or cid in unavailable or (pos and probs[cid][pos] < 0.15):
                continue
            o = overall.get(cid)
            played.append(o or {"champion_id": cid, "games": 0, "wins": 0, "winrate": None, "kda": None})
    if len(played) >= 3:
        pool = played
    from_pool = True
    if not pool:
        # sem histórico: usa os bonecos mais fortes na posição em high elo
        from_pool = False
        pool = db.query(
            """SELECT champion_id, COUNT(*) games, SUM(win) wins FROM participants
               WHERE source='meta' AND (? IS NULL OR position=?) GROUP BY champion_id
               HAVING games >= 20 ORDER BY games DESC LIMIT 40""", (pos, pos))
        for r in pool:
            r["winrate"] = wr(r["wins"], r["games"])

    vs = enemy_matchups(db, dd, pos, lane_opp, state.enemies)
    candidates = []
    for c in pool:
        cid = c["champion_id"]
        if cid in unavailable:
            continue
        info = dd.champs.get(cid, {})
        m = vs.get(cid, {"lane": None, "vs": []})
        reasons = []
        # Média ponderada de winrates "suavizados" (cada amostra começa com jogos fictícios a 50%):
        # poucos jogos ficam perto de 50%, por isso 2 jogos a 100% não passam à frente de 30 jogos a 60%.
        parts = []
        if from_pool:
            parts.append((smooth(c["wins"], c["games"], PRIOR_MINE), 1.5))
            if c["games"]:
                reasons.append(f"your WR: {c['winrate']:.0%} in {c['games']} games"
                               + (" (small sample)" if c["games"] < 5 else ""))
            if cid in mastery:
                rank, points = mastery[cid]
                reasons.append(f"★ your #{rank} mastery ({points / 1000:.0f}k pts)")
        else:
            parts.append((smooth(c["wins"], c["games"], PRIOR_META), 1.5))
            reasons.append(f"{c['winrate']:.0%} WR in high elo ({c['games']} games)")

        if lane_opp:
            # o matchup de lane é o que mais pesa; sem dados conta como 50%
            lane = m["lane"]
            parts.append((smooth(lane["w"], lane["g"], PRIOR_META) if lane else 0.5, 3))
            if lane and lane["g"] >= 10:
                tone = "favourable" if lane["wr"] >= 0.52 else "hard" if lane["wr"] <= 0.48 else "even"
                reasons.append(f"{tone} matchup vs {lane['enemy']}: {lane['wr']:.0%} in high elo ({lane['g']} games)")
            elif lane:
                reasons.append(f"few high elo games vs {lane['enemy']}: {lane['wr']:.0%} in {lane['g']}")
            else:
                reasons.append(f"no high elo data vs {dd.champ_name(lane_opp)} yet")
            mg, mw = matchup(db, "me", cid, lane_opp)
            if mg:
                parts.append((smooth(round(mw * mg), mg, PRIOR_MY_MATCHUP), 1))
                reasons.append(f"you vs {dd.champ_name(lane_opp)}: {round(mw * mg)}W {mg - round(mw * mg)}L")
        for v in m["vs"]:
            parts.append((smooth(v["w"], v["g"], PRIOR_META), 0.5))
        good = [v for v in m["vs"] if v["g"] >= 10 and v["wr"] >= 0.55]
        bad = [v for v in m["vs"] if v["g"] >= 10 and v["wr"] <= 0.45]
        if good:
            reasons.append("strong vs " + ", ".join(f"{v['enemy']} ({v['wr']:.0%})" for v in good))
        if bad:
            reasons.append("weak vs " + ", ".join(f"{v['enemy']} ({v['wr']:.0%})" for v in bad))
        score = sum(x * w for x, w in parts) / sum(w for _, w in parts)

        if len(ally_champs) >= 2:
            if ally_ad >= 3 and info.get("damage") == "AP":
                score += 0.01
                reasons.append("balances damage (your team is already very AD)")
            elif ally_ap >= 3 and info.get("damage") == "AD":
                score += 0.01
                reasons.append("balances damage (your team is already very AP)")
            if not ally_has_tank and "Tank" in info.get("tags", []):
                score += 0.0075
                reasons.append("your team has no frontline yet")

        candidates.append({
            "champion_id": cid, "name": dd.champ_name(cid), "icon": dd.champ_icon(cid),
            "score": round(score, 4), "games": c["games"], "winrate": c["winrate"],
            "kda": c.get("kda"), "reasons": reasons, "from_pool": from_pool,
            # "confortável" = 3+ jogos teus ou top 10 de maestria; estes aparecem sempre primeiro
            "comfort": not from_pool or c["games"] >= MIN_POOL_GAMES or cid in mastery,
        })
    candidates.sort(key=lambda x: (x["comfort"], x["score"]), reverse=True)

    warning = None
    if state.my_champion and lane_opp and not state.locked:
        g, w = matchup(db, "meta", state.my_champion, lane_opp, pos)
        if g >= MIN_MATCHUP_GAMES and w < 0.47:
            warning = (f"Careful: {dd.champ_name(state.my_champion)} vs {dd.champ_name(lane_opp)} "
                       f"only wins {w:.0%} of games in high elo ({g} games).")

    return {
        "position": pos,
        "lane_opponent": {"id": lane_opp, "name": dd.champ_name(lane_opp), "icon": dd.champ_icon(lane_opp)} if lane_opp else None,
        "enemy_roles": {p: dd.champ_name(c) for p, c in enemy_roles.items()},
        "candidates": candidates[:limit],
        "warning": warning,
        "from_pool": from_pool,
    }


# ---------------------------------------------------------------- build
def _most_common(rows: list[dict], key) -> tuple | None:
    groups: dict = {}
    for r in rows:
        k = key(r)
        if k is None:
            continue
        g = groups.setdefault(k, [0, 0])
        g[0] += 1
        g[1] += r["win"]
    if not groups:
        return None
    k, (games, wins) = max(groups.items(), key=lambda kv: kv[1][0])
    return k, games, wr(wins, games)


def recommend_build(state: ChampSelectState, db: DB, dd: DDragon) -> dict:
    champ, pos = state.my_champion, state.my_position or None
    enemy_roles = guess_enemy_roles(db, dd, state.enemies)
    opp = enemy_roles.get(pos) if pos else None

    base = "SELECT * FROM participants WHERE source='meta' AND champion_id=?"
    rows, scope = [], ""
    if opp:
        rows = db.query(base + " AND opponent_id=?" + (" AND position=?" if pos else ""),
                        (champ, opp, pos) if pos else (champ, opp))
        scope = f"vs {dd.champ_name(opp)}"
    if len(rows) < 8 and pos:
        rows = db.query(base + " AND position=?", (champ, pos))
        scope = f"overall in {pos}"
    if len(rows) < 5:
        rows = db.query(base, (champ,))
        scope = "overall"

    result = {
        "champion": {"id": champ, "name": dd.champ_name(champ), "icon": dd.champ_icon(champ)},
        "opponent": {"id": opp, "name": dd.champ_name(opp), "icon": dd.champ_icon(opp)} if opp else None,
        "scope": scope, "sample": len(rows), "runes": None, "shards": None, "spells": None,
        "core_items": [], "boots": None, "situational": [], "matchup": None,
    }

    if rows:
        page = _most_common(rows, lambda r: (r["primary_style"], r["sub_style"], tuple(r["perks"][:6])))
        if page:
            (prim, sub, perks), g, w = page
            result["runes"] = {
                "primary": dd.styles.get(prim, "?"), "secondary": dd.styles.get(sub, "?"),
                "primary_id": prim, "secondary_id": sub, "perk_ids": list(perks), "shard_ids": [],
                "keystone": dd.perks.get(perks[0], "?"),
                "perks": [dd.perks.get(p, str(p)) for p in perks], "games": g, "winrate": w,
            }
            same_page = [r for r in rows if tuple(r["perks"][:6]) == perks]
            shards = _most_common(same_page, lambda r: tuple(r["perks"][6:9]))
            if shards:
                result["shards"] = [dd.perks.get(p, str(p)) for p in shards[0]]
                result["runes"]["shard_ids"] = list(shards[0])
        spells = _most_common(rows, lambda r: tuple(r["spells"]))
        if spells:
            result["spells"] = spell_info(spells, dd)

        item_counts, item_wins, boots = Counter(), Counter(), Counter()
        for r in rows:
            for iid in set(r["items"]):
                if not dd.is_completed_item(iid):
                    continue
                if dd.is_boots(iid):
                    boots[iid] += 1
                else:
                    item_counts[iid] += 1
                    item_wins[iid] += r["win"]
        result["core_items"] = [
            {"id": iid, "name": dd.item_name(iid), "icon": dd.item_icon(iid),
             "pick_rate": n / len(rows), "winrate": wr(item_wins[iid], n)}
            for iid, n in item_counts.most_common(6)
        ]
        if boots:
            iid, n = boots.most_common(1)[0]
            result["boots"] = {"id": iid, "name": dd.item_name(iid), "icon": dd.item_icon(iid), "pick_rate": n / len(rows)}

    # Ordem de itens / skills / pathing vêm da timeline; se o matchup tiver poucos jogos
    # com timeline, usa todos os jogos do boneco nessa posição.
    tl_rows = [r for r in rows if r.get("purchases")]
    tl_scope = scope
    if len(tl_rows) < 8:
        broader = db.query(base + " AND purchases IS NOT NULL" + (" AND position=?" if pos else ""),
                           (champ, pos) if pos else (champ,))
        if len(broader) > len(tl_rows):
            tl_rows, tl_scope = broader, f"overall in {pos}" if pos else "overall"
    result["timeline"] = timeline_build(tl_rows, dd, pos) if tl_rows else None
    if result["timeline"]:
        result["timeline"]["scope"] = tl_scope

    if opp:
        g, w = matchup(db, "meta", champ, opp, pos)
        mg, mw = matchup(db, "me", champ, opp)
        result["matchup"] = {"meta_games": g, "meta_winrate": w, "my_games": mg, "my_winrate": mw}

    result["situational"] = situational_tips(state, dd, champ)
    return result


def spell_info(spells: tuple, dd: DDragon) -> dict:
    (ids, games, winrate) = spells
    return {"names": [dd.spells.get(s, str(s)) for s in ids],
            "icons": [{"name": dd.spells.get(s, ""), "icon": dd.spell_icons.get(s, "")} for s in ids],
            "games": games, "winrate": winrate}


def aggregate_build(rows: list[dict], dd: DDragon, pos: str | None = None) -> dict:
    """Runas, feitiços, skills e ordem de itens mais comuns num conjunto de jogos."""
    out = {"sample": len(rows), "runes": None, "spells": None, "timeline": None}
    if not rows:
        return out
    page = _most_common(rows, lambda r: (r["primary_style"], r["sub_style"], tuple(r["perks"][:6])))
    if page:
        (prim, sub, perks), g, w = page
        same = [r for r in rows if tuple(r["perks"][:6]) == perks]
        shards = _most_common(same, lambda r: tuple(r["perks"][6:9]))
        out["runes"] = {"primary_id": prim, "secondary_id": sub, "perk_ids": list(perks),
                        "shard_ids": list(shards[0]) if shards else [],
                        "primary": dd.styles.get(prim, "?"), "secondary": dd.styles.get(sub, "?"),
                        "keystone": dd.perks.get(perks[0], "?"),
                        "perks": [dd.perks.get(p, str(p)) for p in perks], "games": g, "winrate": w}
    spells = _most_common(rows, lambda r: tuple(r["spells"]))
    if spells:
        out["spells"] = spell_info(spells, dd)
    tl = [r for r in rows if r.get("purchases")]
    if tl:
        pos = pos or Counter(r["position"] for r in rows).most_common(1)[0][0]
        out["timeline"] = timeline_build(tl, dd, pos)
        out["timeline"]["scope"] = "games"
    return out


def _first_completed(purchases: list, dd: DDragon, n: int = 3) -> list[tuple[int, float]]:
    seen, out = set(), []
    for iid, minute in purchases:
        if iid in seen or not dd.is_completed_item(iid) or dd.is_boots(iid):
            continue
        seen.add(iid)
        out.append((iid, minute))
        if len(out) == n:
            break
    return out


def _skill_priority(order: str) -> str | None:
    """Ordem em que Q/W/E chegam ao nível máximo (5), p.ex. 'Q > E > W'."""
    counts, maxed = Counter(), []
    for s in order:
        counts[s] += 1
        if s in "QWE" and counts[s] == 5:
            maxed.append(s)
    if len(maxed) < 2:
        return None
    maxed += [s for s in "QWE" if s not in maxed]
    return " > ".join(maxed[:3])


def timeline_build(rows: list[dict], dd: DDragon, pos: str | None) -> dict:
    n = len(rows)
    out = {"sample": n}
    item_info = lambda iid: {"id": iid, "name": dd.item_name(iid), "icon": dd.item_icon(iid)}

    start = _most_common(rows, lambda r: tuple(sorted(
        i for i, m in r["purchases"] if m <= 1.0 and "Trinket" not in dd.items.get(i, {}).get("tags", [])
    )) or None)
    if start:
        out["start_items"] = {"items": [item_info(i) for i in start[0]], "games": start[1], "winrate": start[2]}

    path = _most_common(rows, lambda r: tuple(i for i, _ in _first_completed(r["purchases"], dd)) or None)
    if path and len(path[0]) >= 2:
        same = [r for r in rows if tuple(i for i, _ in _first_completed(r["purchases"], dd)) == path[0]]
        minutes = [[m for _, m in _first_completed(r["purchases"], dd)] for r in same]
        avg = [round(sum(ms[k] for ms in minutes if len(ms) > k) / max(1, sum(len(ms) > k for ms in minutes)), 1)
               for k in range(len(path[0]))]
        out["item_path"] = {"items": [{**item_info(i), "minute": avg[k]} for k, i in enumerate(path[0])],
                            "games": path[1], "winrate": path[2]}

    prio = _most_common(rows, lambda r: _skill_priority(r.get("skill_order") or ""))
    first3 = _most_common(rows, lambda r: (r.get("skill_order") or "")[:3] or None)
    if prio:
        out["skills"] = {"priority": prio[0], "first3": first3[0] if first3 else "",
                         "games": prio[1], "winrate": prio[2]}

    if pos == "JUNGLE":
        jg = [r["jungle"] for r in rows if r.get("jungle")]
        if jg:
            def dist(key, sub=None):
                c = Counter((j.get(key) or {}).get(sub) if sub else j.get(key) for j in jg)
                c.pop(None, None)
                total = sum(c.values())
                return [{"name": k, "pct": v / total} for k, v in c.most_common(3)] if total else []
            gank_minutes = [j["first_gank"]["minute"] for j in jg if j.get("first_gank")]
            # rotas reais (posição minuto a minuto) por lado do mapa, para desenhar no minimapa
            paths = {"100": [], "200": []}
            for j in jg:
                if j.get("path") and str(j.get("team")) in paths and len(paths[str(j["team"])]) < 30:
                    paths[str(j["team"])].append(j["path"])
            out["jungle"] = {
                "sample": len(jg),
                "start": dist("start"),
                "at2": dist("at2"),
                "side3": dist("side3"),
                "first_gank": dist("first_gank", "lane"),
                "first_gank_minute": round(sum(gank_minutes) / len(gank_minutes), 1) if gank_minutes else None,
                "paths": paths,
            }
    return out


# ---------------------------------------------------------------- matchups contra a equipa inimiga
MIN_POOL_GAMES = 3     # mínimo de jogos teus para um boneco entrar no "My champs"
PRIOR_MINE = 10        # jogos fictícios a 50% no teu winrate com o boneco
PRIOR_META = 20        # idem para amostras high elo
PRIOR_MY_MATCHUP = 4   # idem para os teus jogos contra o adversário de lane


def my_mastery_ranks(db: DB) -> dict[int, tuple[int, int]]:
    """Os teus 10 bonecos com mais maestria (guardados pela aba Mains): {id: (posição, pontos)}."""
    cached = json.loads(db.kv_get("my_mastery") or "{}")
    return {m["championId"]: (i + 1, m["championPoints"]) for i, m in enumerate(cached.get("list", []))}


def smooth(wins: float, games: float, prior: float) -> float:
    return (wins + prior / 2) / (games + prior)


def enemy_matchups(db: DB, dd: DDragon, pos: str | None, lane_opp: int | None,
                   enemies: list[int]) -> dict[int, dict]:
    """Para cada boneco (na tua posição, em high elo): resultado contra o adversário de lane
    e contra cada um dos outros inimigos (jogos em que esse inimigo estava na equipa adversária)."""
    out: dict[int, dict] = {}
    for enemy in enemies:
        is_lane = enemy == lane_opp
        if is_lane:
            rows = db.query(
                """SELECT champion_id, COUNT(*) g, SUM(win) w FROM participants
                   WHERE source='meta' AND opponent_id=? AND (? IS NULL OR position=?) GROUP BY champion_id""",
                (enemy, pos, pos))
        else:
            rows = db.query(
                """SELECT a.champion_id, COUNT(*) g, SUM(a.win) w FROM participants a
                   JOIN participants b ON b.match_id = a.match_id AND b.win != a.win
                   WHERE a.source='meta' AND b.champion_id=? AND (? IS NULL OR a.position=?)
                   GROUP BY a.champion_id""", (enemy, pos, pos))
        for r in rows:
            entry = {"enemy": dd.champ_name(enemy), "g": r["g"], "w": r["w"], "wr": wr(r["w"], r["g"])}
            d = out.setdefault(r["champion_id"], {"lane": None, "vs": []})
            if is_lane:
                d["lane"] = entry
            else:
                d["vs"].append(entry)
    return out


# ---------------------------------------------------------------- counters (high elo)
def recommend_counters(state: ChampSelectState, db: DB, dd: DDragon, limit: int = 8) -> dict:
    """Bonecos com melhor winrate em high elo contra a equipa inimiga atual (na tua posição).

    Conta muito o matchup de lane e um pouco o winrate contra cada um dos outros inimigos
    (jogos em que esse inimigo estava na equipa adversária, em qualquer posição).
    """
    pos = state.my_position or None
    enemy_roles = guess_enemy_roles(db, dd, state.enemies)
    lane_opp = enemy_roles.get(pos) if pos else None
    unavailable = state.unavailable()

    pool = db.query(
        """SELECT champion_id, COUNT(*) g, SUM(win) w FROM participants
           WHERE source='meta' AND (? IS NULL OR position=?) GROUP BY champion_id HAVING g >= 15""", (pos, pos))
    if not pool:
        return {"position": pos, "lane_opponent": None, "candidates": []}

    vs = enemy_matchups(db, dd, pos, lane_opp, state.enemies)
    stats = {r["champion_id"]: {"g": r["g"], "w": r["w"], **vs.get(r["champion_id"], {"lane": None, "vs": []})}
             for r in pool if r["champion_id"] not in unavailable}

    candidates = []
    prior = 20  # jogos "fictícios" a 50% para suavizar amostras pequenas
    for cid, s in stats.items():
        base = (s["w"] + prior / 2) / (s["g"] + prior)
        score, weight = base, 1.0
        reasons = [f"{wr(s['w'], s['g']):.0%} overall WR in high elo ({s['g']} games)"]
        if s["lane"]:
            lane_wr = (s["lane"]["wr"] * s["lane"]["g"] + prior / 2) / (s["lane"]["g"] + prior)
            score += lane_wr * 3
            weight += 3
            reasons.append(f"vs {s['lane']['enemy']} in lane: {s['lane']['wr']:.0%} ({s['lane']['g']} games)")
        for v in s["vs"]:
            score += (v["wr"] * v["g"] + prior / 2) / (v["g"] + prior)
            weight += 1
        good = [v for v in s["vs"] if v["g"] >= 10 and v["wr"] >= 0.55]
        if good:
            reasons.append("strong vs " + ", ".join(f"{v['enemy']} ({v['wr']:.0%})" for v in good))
        candidates.append({
            "champion_id": cid, "name": dd.champ_name(cid), "icon": dd.champ_icon(cid),
            "score": round(score / weight, 4), "games": s["g"], "winrate": wr(s["w"], s["g"]),
            "reasons": reasons, "in_pool": False,
        })

    mine = {r["champion_id"] for r in my_champion_stats(db)}
    for c in candidates:
        c["in_pool"] = c["champion_id"] in mine
    candidates.sort(key=lambda c: c["score"], reverse=True)
    return {
        "position": pos,
        "lane_opponent": {"id": lane_opp, "name": dd.champ_name(lane_opp), "icon": dd.champ_icon(lane_opp)} if lane_opp else None,
        "candidates": candidates[:limit],
    }


def situational_tips(state: ChampSelectState, dd: DDragon, champ: int) -> list[str]:
    tips = []
    info = dd.champs.get(champ, {})
    is_tank = "Tank" in info.get("tags", [])
    is_marksman = "Marksman" in info.get("tags", [])
    enemies = [dd.champs.get(c, {}) for c in state.enemies]
    ap = sum(e.get("damage") == "AP" for e in enemies)
    ad = len(enemies) - ap

    if ap >= 3:
        tips.append(f"Enemy team has a lot of magic damage ({ap} AP): consider Mercury's Treads and early magic resist.")
    if ad >= 4:
        tips.append(f"Enemy team is almost all AD ({ad}): Plated Steelcaps and armor are very efficient.")
    healers = [e["name"] for e in enemies if e.get("key") in HEALERS]
    if healers:
        if is_tank:
            item = "Bramble Vest → Thornmail"
        elif info.get("damage") == "AP":
            item = "Oblivion Orb → Morellonomicon"
        elif is_marksman:
            item = "Executioner's Calling → Mortal Reminder"
        else:
            item = "Executioner's Calling → Chempunk Chainsword"
        tips.append(f"Strong enemy healing ({', '.join(healers)}): buy anti-heal ({item}).")
    tanks = sum("Tank" in e.get("tags", []) for e in enemies)
    if tanks >= 2 and not is_tank:
        tips.append(f"{tanks} enemy tanks: include penetration (Lord Dominik's / Void Staff / Black Cleaver depending on your champion).")
    assassins = sum("Assassin" in e.get("tags", []) for e in enemies)
    if assassins >= 2 and not is_tank:
        tips.append(f"{assassins} enemy assassins: consider Zhonya's / Guardian Angel / Maw and play your positioning carefully.")
    return tips

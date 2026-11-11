"""Análise pós-jogo: métricas do jogo + timeline, comparadas com objetivos por posição,
com a tua média no boneco e com a média high elo."""
from .analysis import my_champion_stats
from .db import DB
from .ddragon import DDragon

# Objetivos de referência por posição (aprox. Diamante+)
TARGETS = {
    "TOP":     {"cspm": 7.0, "vspm": 0.7, "kp": 0.45, "cs10": 70},
    "JUNGLE":  {"cspm": 5.5, "vspm": 1.0, "kp": 0.60, "cs10": 55},
    "MIDDLE":  {"cspm": 7.5, "vspm": 0.8, "kp": 0.55, "cs10": 75},
    "BOTTOM":  {"cspm": 8.0, "vspm": 0.7, "kp": 0.55, "cs10": 80},
    "UTILITY": {"cspm": 0.0, "vspm": 2.0, "kp": 0.60, "cs10": 0},
}


def _frame(timeline: dict, minute: int) -> dict | None:
    frames = timeline["info"]["frames"]
    return frames[minute]["participantFrames"] if minute < len(frames) else None


def analyse_match(match: dict, timeline: dict | None, puuid: str, db: DB, dd: DDragon) -> dict:
    info = match["info"]
    parts = info["participants"]
    me = next(p for p in parts if p["puuid"] == puuid)
    pos = me.get("teamPosition") or ""
    opp = next((p for p in parts if p["teamId"] != me["teamId"] and p.get("teamPosition") == pos), None)
    team = [p for p in parts if p["teamId"] == me["teamId"]]
    minutes = info["gameDuration"] / 60
    team_kills = sum(p["kills"] for p in team)
    team_dmg = sum(p["totalDamageDealtToChampions"] for p in team)
    ch = me.get("challenges", {})

    m = {
        "kills": me["kills"], "deaths": me["deaths"], "assists": me["assists"],
        "kda": (me["kills"] + me["assists"]) / max(me["deaths"], 1),
        "cs": me["totalMinionsKilled"] + me["neutralMinionsKilled"],
        "cspm": (me["totalMinionsKilled"] + me["neutralMinionsKilled"]) / minutes,
        "vspm": me["visionScore"] / minutes,
        "kp": (me["kills"] + me["assists"]) / max(team_kills, 1),
        "damage_share": me["totalDamageDealtToChampions"] / max(team_dmg, 1),
        "gold_pm": me["goldEarned"] / minutes,
        "control_wards": me.get("visionWardsBoughtInGame", 0),
        "wards_placed": me.get("wardsPlaced", 0),
        "solo_kills": ch.get("soloKills"),
        "plates": ch.get("turretPlatesTaken"),
    }

    deaths = []
    if timeline:
        pid = me["participantId"]
        opp_pid = opp["participantId"] if opp else None
        for minute in (10, 15):
            f = _frame(timeline, minute)
            if not f:
                continue
            mine = f[str(pid)]
            m[f"cs{minute}"] = mine["minionsKilled"] + mine["jungleMinionsKilled"]
            if opp_pid:
                theirs = f[str(opp_pid)]
                m[f"gold_diff{minute}"] = mine["totalGold"] - theirs["totalGold"]
                m[f"xp_diff{minute}"] = mine["xp"] - theirs["xp"]
                m[f"cs_diff{minute}"] = m[f"cs{minute}"] - (theirs["minionsKilled"] + theirs["jungleMinionsKilled"])
        champ_by_pid = {p["participantId"]: p["championId"] for p in parts}
        first_item = None
        for frame in timeline["info"]["frames"]:
            for ev in frame["events"]:
                if ev["type"] == "CHAMPION_KILL" and ev.get("victimId") == pid:
                    deaths.append({"minute": round(ev["timestamp"] / 60000, 1),
                                   "killer": dd.champ_name(champ_by_pid.get(ev.get("killerId"))),
                                   "assists": len(ev.get("assistingParticipantIds", []))})
                elif (ev["type"] == "ITEM_PURCHASED" and ev.get("participantId") == pid and first_item is None
                      and dd.is_completed_item(ev["itemId"]) and not dd.is_boots(ev["itemId"])):
                    first_item = {"name": dd.item_name(ev["itemId"]), "minute": round(ev["timestamp"] / 60000, 1)}
        m["first_item"] = first_item
        m["early_deaths"] = sum(d["minute"] < 14 for d in deaths)

    feedback = _feedback(m, pos, me, db, deaths)
    good = sum(f["type"] == "good" for f in feedback)
    bad = sum(f["type"] == "bad" for f in feedback)
    score = round(100 * (good + 1) / (good + bad + 2))

    return {
        "match_id": match["metadata"]["matchId"],
        "date": info["gameCreation"],
        "duration_min": round(minutes, 1),
        "win": me["win"],
        "position": pos,
        "champion": {"id": me["championId"], "name": dd.champ_name(me["championId"]), "icon": dd.champ_icon(me["championId"])},
        "opponent": {"name": dd.champ_name(opp["championId"]), "icon": dd.champ_icon(opp["championId"])} if opp else None,
        "items": [{"name": dd.item_name(me[f"item{i}"]), "icon": dd.item_icon(me[f"item{i}"])} for i in range(6) if me[f"item{i}"]],
        "metrics": m,
        "deaths": deaths,
        "feedback": feedback,
        "score": score,
        "detail": build_detail(match, timeline, me, opp, dd) if timeline else None,
        "map_icon": f"https://ddragon.leagueoflegends.com/cdn/{dd.version}/img/map/map11.png",
    }


def build_detail(match: dict, timeline: dict, me: dict, opp: dict | None, dd: DDragon) -> dict:
    """Tudo o que a timeline da Riot dá para "rever" o jogo: posições de todos a cada minuto,
    kills/mortes e objetivos com local, compras, skills e ouro."""
    parts = match["info"]["participants"]
    frames = timeline["info"]["frames"]
    my_pid = me["participantId"]
    enemy_jg = next((p for p in parts if p["teamId"] != me["teamId"] and p.get("teamPosition") == "JUNGLE"), None)
    ally_jg = next((p for p in parts if p["teamId"] == me["teamId"] and p.get("teamPosition") == "JUNGLE"), None)

    players = [{
        "pid": p["participantId"], "team": p["teamId"], "position": p.get("teamPosition") or "",
        "name": p.get("riotIdGameName") or "?", "champion": dd.champ_name(p["championId"]),
        "icon": dd.champ_icon(p["championId"]), "is_me": p["participantId"] == my_pid,
    } for p in parts]

    frame_rows = []
    for i, f in enumerate(frames):
        pf = f["participantFrames"]
        frame_rows.append({
            "m": i,
            "pos": {k: [v["position"]["x"], v["position"]["y"]] for k, v in pf.items() if "position" in v},
            "gold": {k: v["totalGold"] for k, v in pf.items()},
            "level": {k: v["level"] for k, v in pf.items()},
        })

    events, purchases, skills = [], [], ""
    for f in frames:
        for ev in f["events"]:
            t = round(ev["timestamp"] / 60000, 2)
            typ = ev["type"]
            if typ == "CHAMPION_KILL":
                pos = ev.get("position", {})
                events.append({"t": t, "type": "kill", "x": pos.get("x"), "y": pos.get("y"),
                               "killer": ev.get("killerId"), "victim": ev.get("victimId"),
                               "assists": ev.get("assistingParticipantIds", [])})
            elif typ == "ELITE_MONSTER_KILL":
                pos = ev.get("position", {})
                events.append({"t": t, "type": "objective", "x": pos.get("x"), "y": pos.get("y"),
                               "killer": ev.get("killerId"), "team": ev.get("killerTeamId"),
                               "monster": ev.get("monsterSubType") or ev.get("monsterType")})
            elif typ == "BUILDING_KILL":
                pos = ev.get("position", {})
                events.append({"t": t, "type": "building", "x": pos.get("x"), "y": pos.get("y"),
                               "team": ev.get("teamId"), "building": ev.get("towerType") or ev.get("buildingType"),
                               "lane": ev.get("laneType")})
            elif ev.get("participantId") == my_pid:
                if typ == "ITEM_PURCHASED":
                    purchases.append({"t": t, "id": ev["itemId"], "name": dd.item_name(ev["itemId"]),
                                      "icon": dd.item_icon(ev["itemId"]),
                                      "completed": dd.is_completed_item(ev["itemId"])})
                elif typ == "ITEM_UNDO" and ev.get("beforeId"):
                    for k in range(len(purchases) - 1, -1, -1):
                        if purchases[k]["id"] == ev["beforeId"]:
                            purchases.pop(k)
                            break
                elif typ == "SKILL_LEVEL_UP" and ev.get("levelUpType") == "NORMAL":
                    skills += {1: "Q", 2: "W", 3: "E", 4: "R"}.get(ev["skillSlot"], "")

    return {
        "my_pid": my_pid,
        "opp_pid": opp["participantId"] if opp else None,
        "ally_jg_pid": ally_jg["participantId"] if ally_jg else None,
        "enemy_jg_pid": enemy_jg["participantId"] if enemy_jg else None,
        "players": players,
        "frames": frame_rows,
        "events": events,
        "purchases": purchases,
        "skills": skills,
    }


def _feedback(m: dict, pos: str, me: dict, db: DB, deaths: list[dict]) -> list[dict]:
    fb = []
    t = TARGETS.get(pos, TARGETS["MIDDLE"])

    cat = {"v": "farm"}

    def section(name):
        cat["v"] = name

    def add(kind, text):
        fb.append({"type": kind, "text": text, "cat": cat["v"]})

    section("farm")
    if pos != "UTILITY":
        if m["cspm"] >= t["cspm"]:
            add("good", f"Good farming: {m['cspm']:.1f} CS/min (target {t['cspm']}).")
        elif m["cspm"] < t["cspm"] - 1.5:
            add("bad", f"Low farm: {m['cspm']:.1f} CS/min (target {t['cspm']}). Focus on last hits and don't lose waves when you roam/recall.")
        if "cs10" in m and t["cs10"] and m["cs10"] < t["cs10"] * 0.8:
            add("bad", f"Only {m['cs10']} CS at 10 min (target ~{t['cs10']}). The early laning phase is where the easiest gold is.")

    section("lane")
    gd = m.get("gold_diff15")
    if gd is not None:
        if gd >= 1000:
            add("good", f"You won lane: +{gd} gold at 15 min vs your direct opponent.")
        elif gd <= -1000:
            add("bad", f"You lost lane: {gd} gold at 15 min. Review your early deaths and the trades you took.")
    if m.get("cs_diff10") is not None and m["cs_diff10"] <= -15 and pos != "UTILITY":
        add("bad", f"{-m['cs_diff10']} CS behind your opponent at 10 min.")

    section("deaths")
    early = m.get("early_deaths")
    if early is not None:
        if early == 0:
            add("good", "No deaths before 14 min: safe laning phase.")
        elif early >= 3:
            add("bad", f"{early} deaths before 14 min. Play around the map: check the minimap, ward the river, respect the enemy jungler.")
    if m["deaths"] >= 8:
        add("bad", f"{m['deaths']} deaths in total. Mid/late game deaths give the enemy objectives; avoid walking alone without vision.")
    late = [d for d in deaths if d["minute"] >= 25]
    if len(late) >= 3:
        add("bad", f"{len(late)} deaths after 25 min, when respawn timers are long and cost objectives.")
    solo = [d for d in deaths if d["assists"] == 0]
    if len(solo) >= 3:
        add("info", f"{len(solo)} of your deaths were 1v1 (no assists). Check whether those trades were favourable.")

    section("vision")
    if m["vspm"] >= t["vspm"]:
        add("good", f"Good vision: {m['vspm']:.2f} vision score/min.")
    elif m["vspm"] < t["vspm"] * 0.6:
        add("bad", f"Weak vision: {m['vspm']:.2f}/min (target {t['vspm']}). Use your trinket whenever it is up.")
    section("wards")
    if m["control_wards"] == 0:
        add("bad", "You didn't buy a single control ward. Buy one on almost every recall.")
    elif m["control_wards"] >= 4:
        add("good", f"{m['control_wards']} control wards bought.")

    section("fights")
    if m["kp"] >= t["kp"]:
        add("good", f"{m['kp']:.0%} kill participation: you were there for the fights.")
    elif m["kp"] < t["kp"] - 0.15:
        add("bad", f"Only {m['kp']:.0%} kill participation (target {t['kp']:.0%}). Group more with your team for objective fights.")
    if pos in ("MIDDLE", "BOTTOM") and m["damage_share"] < 0.2:
        add("bad", f"Only {m['damage_share']:.0%} of your team's damage. As a carry you need to be in more fights (with safe positioning).")
    elif m["damage_share"] >= 0.3:
        add("good", f"{m['damage_share']:.0%} of your team's damage.")

    section("items")
    fi = m.get("first_item")
    if fi and pos != "UTILITY" and fi["minute"] > 14:
        add("info", f"First completed item ({fi['name']}) only at {fi['minute']} min. More farm or more efficient recalls get you there sooner.")

    section("compare")
    # as médias guardadas são só as da conta ativa: não servem para comparar outro jogador
    mine = next((s for s in my_champion_stats(db, pos or None) if s["champion_id"] == me["championId"]), None)
    if mine and mine["games"] >= 5 and me["puuid"] == db.me_puuid:
        diff = m["cspm"] - mine["cspm"]
        if abs(diff) >= 1 and pos != "UTILITY":
            add("info", f"CS/min {'above' if diff > 0 else 'below'} your average on this champion ({mine['cspm']:.1f}).")
        if m["kda"] < mine["kda"] * 0.6:
            add("info", f"KDA well below your average on this champion ({mine['kda']:.1f}).")

    section("compare")
    meta = db.query(
        """SELECT COUNT(*) g, AVG(cs*60.0/duration) cspm, AVG(vision*60.0/duration) vspm, AVG(deaths) d
           FROM participants WHERE source='meta' AND champion_id=? AND position=?""",
        (me["championId"], pos))[0]
    if meta["g"] and meta["g"] >= 20:
        add("info", f"High elo average on this champion/role: {meta['cspm']:.1f} CS/min, {meta['vspm']:.2f} vision/min, "
                    f"{meta['d']:.1f} deaths (based on {meta['g']} games).")
    return fb

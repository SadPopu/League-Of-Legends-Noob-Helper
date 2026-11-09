"""Converte a sessão de champ select do LCU num estado simples."""
from dataclasses import dataclass, field


@dataclass
class ChampSelectState:
    my_position: str = ""             # TOP / JUNGLE / MIDDLE / BOTTOM / UTILITY ('' em normals sem posição)
    my_champion: int = 0              # pick final ou intenção (hover)
    locked: bool = False
    allies: list[dict] = field(default_factory=list)   # [{"champion": id, "position": "TOP", "me": bool, "locked": bool}]
    enemies: list[int] = field(default_factory=list)   # ids já escolhidos pela equipa inimiga
    bans: list[int] = field(default_factory=list)
    ally_bans: list[int] = field(default_factory=list)
    enemy_bans: list[int] = field(default_factory=list)
    enemy_roles: dict[str, int] = field(default_factory=dict)  # roles inimigas já conhecidas (jogo a decorrer)
    phase: str = ""

    def unavailable(self) -> set[int]:
        taken = {a["champion"] for a in self.allies if a["champion"] and not a["me"]}
        return taken | set(self.enemies) | set(self.bans)

    def picks_signature(self):
        return (self.my_position, tuple(sorted(self.unavailable())),
                tuple((a["position"], a["champion"]) for a in self.allies if not a["me"]))

    def build_signature(self):
        return (self.my_champion, self.my_position, tuple(sorted(self.enemies)), tuple(sorted(self.enemy_roles.items())))


def parse_session(s: dict) -> ChampSelectState:
    local = s.get("localPlayerCellId")
    actions = [a for group in s.get("actions", []) for a in group]
    completed_pick_cells = {a["actorCellId"] for a in actions if a.get("type") == "pick" and a.get("completed")}

    st = ChampSelectState(phase=(s.get("timer") or {}).get("phase", ""))
    for p in s.get("myTeam", []):
        me = p.get("cellId") == local
        champ = p.get("championId") or p.get("championPickIntent") or 0
        locked = p.get("cellId") in completed_pick_cells
        pos = (p.get("assignedPosition") or "").upper()
        st.allies.append({"champion": champ, "position": pos, "me": me, "locked": locked})
        if me:
            st.my_position, st.my_champion, st.locked = pos, champ, locked

    st.enemies = [p["championId"] for p in s.get("theirTeam", []) if p.get("championId")]

    # bans por equipa: pela lista de bans da sessão ou por quem fez cada ação de ban
    my_cells = {p.get("cellId") for p in s.get("myTeam", [])}
    ally = list((s.get("bans") or {}).get("myTeamBans", []))
    enemy = list((s.get("bans") or {}).get("theirTeamBans", []))
    for a in actions:
        if a.get("type") == "ban" and a.get("completed") and a.get("championId"):
            (ally if a.get("actorCellId") in my_cells else enemy).append(a["championId"])
    st.ally_bans = list(dict.fromkeys(b for b in ally if b))
    st.enemy_bans = list(dict.fromkeys(b for b in enemy if b))
    st.bans = sorted(set(st.ally_bans) | set(st.enemy_bans))
    return st


def mock_session(my_position: str, my_champion: int, allies: list[tuple[str, int]], enemies: list[int],
                 bans: list[int], locked: bool, enemy_bans: list[int] | None = None) -> dict:
    """Sessão falsa no formato do LCU, para testar a app sem estar em fila."""
    my_team = [{"cellId": 0, "championId": my_champion if locked else 0,
                "championPickIntent": my_champion, "assignedPosition": my_position.lower()}]
    for i, (pos, cid) in enumerate(allies, start=1):
        my_team.append({"cellId": i, "championId": cid, "assignedPosition": pos.lower()})
    their = [{"cellId": 5 + i, "championId": cid} for i, cid in enumerate(enemies)]
    actions = [[{"actorCellId": 0, "type": "pick", "championId": my_champion, "completed": locked}]]
    actions += [[{"actorCellId": i, "type": "pick", "championId": cid, "completed": True}]
                for i, (_, cid) in enumerate(allies, start=1) if cid]
    return {"localPlayerCellId": 0, "myTeam": my_team, "theirTeam": their, "actions": actions,
            "bans": {"myTeamBans": bans, "theirTeamBans": enemy_bans or []}, "timer": {"phase": "BAN_PICK"}}

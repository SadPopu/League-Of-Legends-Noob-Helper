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
    phase: str = ""

    def unavailable(self) -> set[int]:
        taken = {a["champion"] for a in self.allies if a["champion"] and not a["me"]}
        return taken | set(self.enemies) | set(self.bans)

    def picks_signature(self):
        return (self.my_position, tuple(sorted(self.unavailable())),
                tuple((a["position"], a["champion"]) for a in self.allies if not a["me"]))

    def build_signature(self):
        return (self.my_champion, self.my_position, tuple(sorted(self.enemies)))


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

    bans = set((s.get("bans") or {}).get("myTeamBans", [])) | set((s.get("bans") or {}).get("theirTeamBans", []))
    bans |= {a["championId"] for a in actions if a.get("type") == "ban" and a.get("completed")}
    st.bans = sorted(b for b in bans if b)
    return st


def mock_session(my_position: str, my_champion: int, allies: list[tuple[str, int]], enemies: list[int],
                 bans: list[int], locked: bool) -> dict:
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
            "bans": {"myTeamBans": bans, "theirTeamBans": []}, "timer": {"phase": "BAN_PICK"}}

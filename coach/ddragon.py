"""Dados estáticos do jogo (Data Dragon): campeões, runas, itens, feitiços."""
import json

import httpx

from .config import DATA_DIR

BASE = "https://ddragon.leagueoflegends.com"

# Linhas de fragmentos tal como aparecem no cliente (ataque / flex / defesa)
SHARD_ROWS = [[5008, 5005, 5007], [5008, 5010, 5001], [5011, 5013, 5001]]
SHARD_ICONS = {
    5008: "StatModsAdaptiveForceIcon", 5005: "StatModsAttackSpeedIcon", 5007: "StatModsCDRScalingIcon",
    5010: "StatModsMovementSpeedIcon", 5001: "StatModsHealthScalingIcon", 5011: "StatModsHealthPlusIcon",
    5013: "StatModsTenacityIcon",
}

STAT_SHARDS = {
    5008: "Adaptive Force", 5005: "Attack Speed", 5007: "Ability Haste",
    5001: "Health Scaling", 5010: "Move Speed", 5011: "Health",
    5013: "Tenacity & Slow Resist", 5002: "Armor", 5003: "Magic Resist",
}


class DDragon:
    def __init__(self):
        self.version = ""
        self.champs: dict[int, dict] = {}      # 266 -> {"key": "Aatrox", "name": "Aatrox", "tags": [...], "damage": "AD"}
        self.by_name: dict[str, int] = {}      # "aatrox" -> 266
        self.perks: dict[int, str] = dict(STAT_SHARDS)
        self.styles: dict[int, str] = {}
        self.items: dict[int, dict] = {}
        self.spells: dict[int, str] = {}
        self.rune_styles: list[dict] = []
        self.spell_icons: dict[int, str] = {}
        self.perk_icons: dict[int, str] = {}

    async def load(self):
        cache = DATA_DIR / "ddragon.json"
        try:
            async with httpx.AsyncClient(timeout=20) as c:
                self.version = (await c.get(f"{BASE}/api/versions.json")).json()[0]
                if cache.exists():
                    cached = json.loads(cache.read_text(encoding="utf-8"))
                    if cached.get("version") == self.version:
                        return self._parse(cached)
                cdn = f"{BASE}/cdn/{self.version}/data/en_US"
                raw = {"version": self.version}
                for name in ("champion", "runesReforged", "item", "summoner"):
                    raw[name] = (await c.get(f"{cdn}/{name}.json")).json()
            cache.write_text(json.dumps(raw), encoding="utf-8")
            self._parse(raw)
        except httpx.HTTPError:
            if not cache.exists():
                raise
            self._parse(json.loads(cache.read_text(encoding="utf-8")))

    def _parse(self, raw):
        self.version = raw["version"]
        for c in raw["champion"]["data"].values():
            cid = int(c["key"])
            info = c.get("info", {})
            damage = "AP" if info.get("magic", 0) > info.get("attack", 0) else "AD"
            self.champs[cid] = {"key": c["id"], "name": c["name"], "tags": c["tags"], "damage": damage}
            self.by_name[c["name"].lower()] = cid
            self.by_name[c["id"].lower()] = cid
        self.rune_styles = []
        for style in raw["runesReforged"]:
            self.rune_styles.append({
                "id": style["id"], "name": style["name"], "icon": f"{BASE}/cdn/img/{style['icon']}",
                "slots": [[{"id": r["id"], "name": r["name"], "icon": f"{BASE}/cdn/img/{r['icon']}"}
                           for r in slot["runes"]] for slot in style["slots"]],
            })
            self.styles[style["id"]] = style["name"]
            for slot in style["slots"]:
                for rune in slot["runes"]:
                    self.perks[rune["id"]] = rune["name"]
                    self.perk_icons[rune["id"]] = f"{BASE}/cdn/img/{rune['icon']}"
            self.perk_icons[style["id"]] = f"{BASE}/cdn/img/{style['icon']}"
        for iid, it in raw["item"]["data"].items():
            self.items[int(iid)] = {
                "name": it["name"],
                "gold": it["gold"]["total"],
                "into": it.get("into", []),
                "from": it.get("from", []),
                "tags": it.get("tags", []),
            }
        for s in raw["summoner"]["data"].values():
            self.spells[int(s["key"])] = s["name"]
            self.spell_icons[int(s["key"])] = f"{BASE}/cdn/{self.version}/img/spell/{s['image']['full']}"

    def rune_tree(self) -> dict:
        shard = lambda i: {"id": i, "name": STAT_SHARDS[i],
                           "icon": f"{BASE}/cdn/img/perk-images/StatMods/{SHARD_ICONS[i]}.png"}
        return {"styles": self.rune_styles, "shards": [[shard(i) for i in row] for row in SHARD_ROWS]}

    # --- helpers ---
    def champ_name(self, cid: int | None) -> str:
        return self.champs.get(cid or 0, {}).get("name", "?")

    def champ_icon(self, cid: int | None) -> str:
        key = self.champs.get(cid or 0, {}).get("key")
        return f"{BASE}/cdn/{self.version}/img/champion/{key}.png" if key else ""

    def item_icon(self, iid: int) -> str:
        return f"{BASE}/cdn/{self.version}/img/item/{iid}.png"

    def item_name(self, iid: int) -> str:
        return self.items.get(iid, {}).get("name", str(iid))

    def is_completed_item(self, iid: int) -> bool:
        it = self.items.get(iid)
        if not it or "Consumable" in it["tags"] or "Trinket" in it["tags"]:
            return False
        if "Boots" in it["tags"]:
            return it["gold"] >= 900
        return not it["into"] and it["gold"] >= 1000

    def is_quest_item(self, iid: int) -> bool:
        """Item de suporte final (Celestial Opposition, Bloodsong...): vem da quest, não se compra."""
        return "3867" in self.items.get(iid, {}).get("from", [])  # 3867 = Bounty of Worlds

    def is_boots(self, iid: int) -> bool:
        return "Boots" in self.items.get(iid, {}).get("tags", [])

    def champ_lookup(self, text: str) -> int | None:
        t = text.strip().lower()
        return self.by_name.get(t) or self.by_name.get(t.replace(" ", "").replace("'", "").replace(".", ""))

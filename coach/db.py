"""Base de dados SQLite: jogos teus, jogos high-elo (meta) e relatórios pós-jogo."""
import json
import sqlite3
import time

from .config import DB_PATH

SCHEMA = """
CREATE TABLE IF NOT EXISTS matches (
    match_id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,              -- 'me' ou 'meta'
    game_creation INTEGER,
    patch TEXT,
    has_timeline INTEGER DEFAULT 0
);
CREATE TABLE IF NOT EXISTS participants (
    match_id TEXT NOT NULL,
    puuid TEXT NOT NULL,
    source TEXT NOT NULL,            -- 'me' (os teus jogos) ou 'meta' (high elo)
    champion_id INTEGER, position TEXT, opponent_id INTEGER, win INTEGER,
    kills INTEGER, deaths INTEGER, assists INTEGER, cs INTEGER, duration INTEGER,
    gold INTEGER, damage INTEGER, vision INTEGER, damage_share REAL, kp REAL,
    game_creation INTEGER, patch TEXT,
    primary_style INTEGER, sub_style INTEGER, perks TEXT, spells TEXT, items TEXT,
    purchases TEXT,                  -- timeline: [[item_id, minuto], ...] por ordem
    skill_order TEXT,                -- timeline: "QWEQQRQ..."
    jungle TEXT,                     -- timeline: pathing do jungler (json)
    PRIMARY KEY (match_id, puuid)
);
CREATE INDEX IF NOT EXISTS idx_part_champ ON participants(source, champion_id, position);
CREATE INDEX IF NOT EXISTS idx_part_matchup ON participants(source, champion_id, opponent_id);
-- índice "de cobertura" para o explorador de dados: evita ler as colunas JSON grandes de cada linha
CREATE INDEX IF NOT EXISTS idx_part_explore ON participants(source, position, patch, champion_id,
    win, kills, deaths, assists, cs, duration, damage_share, kp, match_id, opponent_id);
-- para "boneco X contra cada inimigo": começa nos jogos do inimigo e salta para os adversários pelo match_id
CREATE INDEX IF NOT EXISTS idx_part_bychamp ON participants(champion_id, source, position, match_id, win);
CREATE INDEX IF NOT EXISTS idx_part_bymatch ON participants(match_id, win, source, position, champion_id);
CREATE TABLE IF NOT EXISTS history (
    match_id TEXT PRIMARY KEY,
    grp TEXT,                        -- solo / flex / normal / aram / other
    queue_id INTEGER, game_creation INTEGER, duration INTEGER, remake INTEGER,
    win INTEGER, champion_id INTEGER, kills INTEGER, deaths INTEGER, assists INTEGER,
    cs INTEGER, gold INTEGER, damage INTEGER, vision INTEGER, kp REAL, score REAL, badge TEXT,
    data TEXT                        -- resumo do jogo com os 10 jogadores (json)
);
CREATE INDEX IF NOT EXISTS idx_history_grp ON history(grp, game_creation);
CREATE TABLE IF NOT EXISTS mains (
    puuid TEXT NOT NULL,
    champion_id INTEGER NOT NULL,
    platform TEXT, tier TEXT, lp INTEGER, points INTEGER, updated INTEGER,
    PRIMARY KEY (puuid, champion_id)
);
CREATE INDEX IF NOT EXISTS idx_mains_champ ON mains(champion_id, points);
CREATE TABLE IF NOT EXISTS kv (
    key TEXT PRIMARY KEY,
    value TEXT
);
CREATE TABLE IF NOT EXISTS accounts (
    puuid TEXT PRIMARY KEY,
    name TEXT
);
CREATE TABLE IF NOT EXISTS lp_history (   -- o teu rank sempre que a app o vê mudar
    puuid TEXT NOT NULL,
    queue TEXT NOT NULL,             -- RANKED_SOLO_5x5 / RANKED_FLEX_SR
    ts INTEGER NOT NULL,             -- ms
    tier TEXT, division TEXT, lp INTEGER, wins INTEGER, losses INTEGER,
    PRIMARY KEY (puuid, queue, ts)
);
CREATE TABLE IF NOT EXISTS postgame (
    match_id TEXT PRIMARY KEY,
    created INTEGER,
    report TEXT
);
"""

JSON_COLS = ("perks", "spells", "items", "purchases", "jungle")

# colunas acrescentadas depois da primeira versão (bases de dados antigas)
MIGRATIONS = {
    "matches": {"has_timeline": "INTEGER DEFAULT 0"},
    "participants": {"purchases": "TEXT", "skill_order": "TEXT", "jungle": "TEXT"},
}


class DB:
    def __init__(self, path=DB_PATH):
        self.conn = sqlite3.connect(path, check_same_thread=False)
        self.conn.row_factory = sqlite3.Row
        self.conn.executescript(SCHEMA)
        for table, cols in MIGRATIONS.items():
            existing = {r["name"] for r in self.conn.execute(f"PRAGMA table_info({table})")}
            for col, decl in cols.items():
                if col not in existing:
                    self.conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")
        self.conn.commit()

    def has_match(self, match_id: str, kind: str | None = None) -> bool:
        q = "SELECT 1 FROM matches WHERE match_id=?" + (" AND kind=?" if kind else "")
        args = (match_id, kind) if kind else (match_id,)
        return self.conn.execute(q, args).fetchone() is not None

    def save_match(self, match_id: str, kind: str, game_creation: int, patch: str, rows: list[dict],
                   has_timeline: bool = False):
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO matches (match_id, kind, game_creation, patch, has_timeline) VALUES (?,?,?,?,?)",
                (match_id, kind, game_creation, patch, int(has_timeline)),
            )
            for r in rows:
                r = {**r, **{k: json.dumps(r[k]) for k in JSON_COLS if k in r}}
                cols = ",".join(r)
                self.conn.execute(
                    f"INSERT OR REPLACE INTO participants ({cols}) VALUES ({','.join('?' * len(r))})",
                    tuple(r.values()),
                )

    def save_timeline_extras(self, match_id: str, extras: dict[str, dict]):
        """Acrescenta dados da timeline a um jogo já guardado."""
        with self.conn:
            for puuid, e in extras.items():
                self.conn.execute(
                    "UPDATE participants SET purchases=?, skill_order=?, jungle=? WHERE match_id=? AND puuid=?",
                    (json.dumps(e["purchases"]), e["skill_order"], json.dumps(e["jungle"]), match_id, puuid),
                )
            self.conn.execute("UPDATE matches SET has_timeline=1 WHERE match_id=?", (match_id,))

    def mark_timeline_unavailable(self, match_id: str):
        """A Riot não devolveu a timeline: marca com -1 para não tentar outra vez."""
        with self.conn:
            self.conn.execute("UPDATE matches SET has_timeline=-1 WHERE match_id=?", (match_id,))

    def matches_without_timeline(self, kind: str, limit: int) -> list[str]:
        rows = self.conn.execute(
            "SELECT match_id FROM matches WHERE kind=? AND has_timeline=0 ORDER BY game_creation DESC LIMIT ?",
            (kind, limit))
        return [r["match_id"] for r in rows]

    def query(self, sql: str, args=()) -> list[dict]:
        out = []
        for row in self.conn.execute(sql, args):
            d = dict(row)
            for k in JSON_COLS:
                if k in d and isinstance(d[k], str):
                    d[k] = json.loads(d[k])
            out.append(d)
        return out

    # --- mains ---
    def save_mains(self, puuid: str, platform: str, tier: str, lp: int, masteries: list[tuple[int, int]]):
        now = int(time.time())
        with self.conn:
            for champ, points in masteries:
                self.conn.execute("INSERT OR REPLACE INTO mains VALUES (?,?,?,?,?,?,?)",
                                  (puuid, champ, platform, tier, lp, points, now))
            if not masteries:  # marca como visto mesmo sem dados, para não repetir
                self.conn.execute("INSERT OR REPLACE INTO mains VALUES (?,?,?,?,?,?,?)",
                                  (puuid, 0, platform, tier, lp, 0, now))

    def sync_apex(self, platform: str, players: list[tuple[str, str, int]]):
        """Atualiza tier/LP dos mains e remove quem saiu de Challenger/GM nesta região."""
        current = {p[0]: p for p in players}
        with self.conn:
            for (puuid,) in self.conn.execute("SELECT DISTINCT puuid FROM mains WHERE platform=?", (platform,)).fetchall():
                if puuid in current:
                    _, tier, lp = current[puuid]
                    self.conn.execute("UPDATE mains SET tier=?, lp=? WHERE puuid=?", (tier, lp, puuid))
                else:
                    self.conn.execute("DELETE FROM mains WHERE puuid=?", (puuid,))

    # --- chave/valor (datas das tarefas automáticas, etc.) ---
    def kv_get(self, key: str) -> str | None:
        r = self.conn.execute("SELECT value FROM kv WHERE key=?", (key,)).fetchone()
        return r["value"] if r else None

    def kv_set(self, key: str, value: str):
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO kv VALUES (?,?)", (key, value))

    def save_account(self, puuid: str, name: str):
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO accounts VALUES (?,?)", (puuid, name))

    # --- histórico ---
    def has_history(self, match_id: str) -> bool:
        return self.conn.execute("SELECT 1 FROM history WHERE match_id=?", (match_id,)).fetchone() is not None

    def save_history(self, summary: dict, me: dict, grp: str):
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO history VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (summary["match_id"], grp, summary["queue_id"], summary["game_creation"], summary["duration"],
                 int(summary["remake"]), int(me["win"]), me["champion_id"], me["kills"], me["deaths"],
                 me["assists"], me["cs"], me["gold"], me["damage"], me["vision"], me["kp"],
                 me.get("score"), me.get("badge"), json.dumps(summary)),
            )

    def history_detail(self, match_id: str) -> dict | None:
        r = self.conn.execute("SELECT data FROM history WHERE match_id=?", (match_id,)).fetchone()
        return json.loads(r["data"]) if r else None

    def save_rank(self, puuid: str, entry: dict, ts: int):
        """Guarda o rank de uma fila só se mudou desde o último registo (mesmo W/L = nada de novo)."""
        last = self.conn.execute(
            "SELECT tier, division, lp, wins, losses FROM lp_history WHERE puuid=? AND queue=? ORDER BY ts DESC LIMIT 1",
            (puuid, entry["queueType"])).fetchone()
        row = (entry["tier"], entry["rank"], entry["leaguePoints"], entry["wins"], entry["losses"])
        if last and tuple(last) == row:
            return
        with self.conn:
            self.conn.execute("INSERT OR REPLACE INTO lp_history VALUES (?,?,?,?,?,?,?,?)",
                              (puuid, entry["queueType"], ts, *row))

    def save_postgame(self, match_id: str, report: dict):
        with self.conn:
            self.conn.execute(
                "INSERT OR REPLACE INTO postgame VALUES (?,?,?)", (match_id, int(time.time()), json.dumps(report))
            )

    def postgames(self, limit: int = 20) -> list[dict]:
        rows = self.conn.execute("SELECT report FROM postgame ORDER BY created DESC LIMIT ?", (limit,))
        return [json.loads(r["report"]) for r in rows]

    def postgame(self, match_id: str) -> dict | None:
        r = self.conn.execute("SELECT report FROM postgame WHERE match_id=?", (match_id,)).fetchone()
        return json.loads(r["report"]) if r else None

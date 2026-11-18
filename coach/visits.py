"""Registo de utilização do modo online (só para o admin ver quem usa a app).

Guarda o mínimo: um id anónimo por browser, o email da conta do portfolio (se houver),
o que fez (abrir, pesquisar, escolher favorita), o país indicado pela Cloudflare e o tipo
de dispositivo. Não guarda endereços IP. Os registos com mais de 90 dias são apagados.
"""
import re
import sqlite3
import threading
import time

from . import config

DB_PATH = config.DATA_DIR / "visits.db"
RETENTION_DAYS = 90
_lock = threading.Lock()


def _conn() -> sqlite3.Connection:
    c = sqlite3.connect(DB_PATH, timeout=5)
    c.row_factory = sqlite3.Row
    return c


def init() -> None:
    with _lock, _conn() as c:
        c.execute("""CREATE TABLE IF NOT EXISTS events (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            ts REAL NOT NULL,
            visitor TEXT NOT NULL,
            user TEXT,
            action TEXT NOT NULL,
            detail TEXT,
            country TEXT,
            device TEXT
        )""")
        c.execute("CREATE INDEX IF NOT EXISTS events_ts ON events (ts)")
        c.execute("CREATE INDEX IF NOT EXISTS events_visitor ON events (visitor)")


def device_of(user_agent: str) -> str:
    ua = user_agent or ""
    kind = "Mobile" if re.search(r"Mobile|Android|iPhone|iPad", ua) else "Desktop"
    for name, pat in (("Edge", r"Edg/"), ("Opera", r"OPR/"), ("Chrome", r"Chrome/"), ("Firefox", r"Firefox/"), ("Safari", r"Safari/")):
        if re.search(pat, ua):
            return f"{kind} · {name}"
    return kind


def log(visitor: str, action: str, detail: str | None, user: str | None, country: str | None, user_agent: str) -> None:
    visitor = (visitor or "")[:64] or "unknown"
    user = None if not user or user == "-" else user[:120]
    with _lock, _conn() as c:
        c.execute("INSERT INTO events (ts, visitor, user, action, detail, country, device) VALUES (?, ?, ?, ?, ?, ?, ?)",
                  (time.time(), visitor, user, action, (detail or "")[:80] or None, (country or "")[:2] or None,
                   device_of(user_agent)))
        # limpeza ocasional (1 em cada ~50 registos)
        if int(time.time() * 1000) % 50 == 0:
            c.execute("DELETE FROM events WHERE ts < ?", (time.time() - RETENTION_DAYS * 86400,))


def report(online_now: int) -> dict:
    now = time.time()
    with _lock, _conn() as c:
        def unique_since(seconds: float) -> int:
            return c.execute("SELECT COUNT(DISTINCT visitor) FROM events WHERE ts >= ?", (now - seconds,)).fetchone()[0]

        visitors = [dict(r) for r in c.execute("""
            SELECT visitor,
                   MIN(ts) AS first_seen,
                   MAX(ts) AS last_seen,
                   MAX(user) AS user,
                   COUNT(*) AS events,
                   SUM(action = 'search') AS searches,
                   (SELECT detail FROM events f WHERE f.visitor = e.visitor AND f.action = 'favorite' ORDER BY ts DESC LIMIT 1) AS favorite,
                   (SELECT country FROM events f WHERE f.visitor = e.visitor AND f.country IS NOT NULL ORDER BY ts DESC LIMIT 1) AS country,
                   (SELECT device FROM events f WHERE f.visitor = e.visitor ORDER BY ts DESC LIMIT 1) AS device
            FROM events e
            GROUP BY visitor
            ORDER BY last_seen DESC
            LIMIT 50""")]
        recent = [dict(r) for r in c.execute(
            "SELECT ts, visitor, user, action, detail, country, device FROM events ORDER BY ts DESC LIMIT 40")]
        return {
            "ok": True,
            "online_now": online_now,
            "today": unique_since(86400),
            "week": unique_since(7 * 86400),
            "total": c.execute("SELECT COUNT(DISTINCT visitor) FROM events").fetchone()[0],
            "visitors": visitors,
            "recent": recent,
        }

"""Ligação à API local do cliente do LoL (LCU).

O cliente expõe um servidor HTTPS em 127.0.0.1 com uma porta e password que mudam
sempre que abre. Lemo-las da linha de comandos do processo LeagueClientUx.exe
(ou do ficheiro lockfile) e subscrevemos os eventos pelo websocket (protocolo WAMP).
Só lemos dados: a app nunca faz picks/bans nem lock in por ti.
"""
import asyncio
import base64
import json
import logging
import re
import ssl
from pathlib import Path

import httpx
import psutil
from websockets.asyncio.client import connect

from .config import LOL_PATH

log = logging.getLogger("lcu")

EVENTS = (
    "OnJsonApiEvent_lol-champ-select_v1_session",
    "OnJsonApiEvent_lol-gameflow_v1_gameflow-phase",
)


def find_credentials() -> tuple[int, str] | None:
    for proc in psutil.process_iter(["name", "cmdline"]):
        try:
            if proc.info["name"] != "LeagueClientUx.exe":
                continue
            cmd = " ".join(proc.info["cmdline"] or [])
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            continue
        port = re.search(r"--app-port=(\d+)", cmd)
        token = re.search(r"--remoting-auth-token=([\w-]+)", cmd)
        if port and token:
            return int(port.group(1)), token.group(1)
    lockfile = Path(LOL_PATH) / "lockfile"
    if lockfile.exists():
        # formato: LeagueClient:pid:port:password:https
        parts = lockfile.read_text().split(":")
        if len(parts) >= 5:
            return int(parts[2]), parts[3]
    return None


class LCU:
    def __init__(self, on_event, on_status):
        self.on_event = on_event      # async (uri, data)
        self.on_status = on_status    # async (connected: bool)
        self.port: int | None = None
        self.auth = ""
        self.connected = False

    def _ssl(self):
        ctx = ssl.create_default_context()
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE  # certificado self-signed da Riot, só em localhost
        return ctx

    async def get(self, path: str):
        if not self.port:
            return None
        async with httpx.AsyncClient(verify=False, timeout=5) as c:
            r = await c.get(f"https://127.0.0.1:{self.port}{path}", headers={"Authorization": self.auth})
            return r.json() if r.status_code == 200 else None

    async def run(self):
        while True:
            creds = await asyncio.to_thread(find_credentials)
            if not creds:
                await self._set_connected(False)
                await asyncio.sleep(3)
                continue
            self.port, token = creds
            self.auth = "Basic " + base64.b64encode(f"riot:{token}".encode()).decode()
            try:
                async with connect(
                    f"wss://127.0.0.1:{self.port}/",
                    ssl=self._ssl(),
                    additional_headers={"Authorization": self.auth},
                    subprotocols=["wamp"],
                    max_size=None,
                ) as ws:
                    for ev in EVENTS:
                        await ws.send(json.dumps([5, ev]))
                    await self._set_connected(True)
                    async for raw in ws:
                        if not raw:
                            continue
                        msg = json.loads(raw)
                        if msg[0] == 8 and isinstance(msg[2], dict):
                            payload = msg[2]
                            data = None if payload.get("eventType") == "Delete" else payload.get("data")
                            await self.on_event(payload.get("uri", ""), data)
            except Exception as e:  # cliente fechou, a reiniciar, etc.
                log.info("LCU desligado: %s", e)
            await self._set_connected(False)
            await asyncio.sleep(3)

    async def _set_connected(self, value: bool):
        if value != self.connected:
            self.connected = value
            await self.on_status(value)

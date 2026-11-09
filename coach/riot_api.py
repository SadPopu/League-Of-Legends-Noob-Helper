"""Cliente mínimo da API pública da Riot, com rate limiting."""
import asyncio
import time
from collections import deque
from contextvars import ContextVar
from urllib.parse import quote

import httpx


class RiotAPIError(Exception):
    pass


# Tarefas longas (sincronizar jogos) gastariam o limite todo de uma vez e o que o utilizador pede
# ficaria à espera até 2 minutos. Com isto ligado, a tarefa deixa uma parte do limite livre.
background_job: ContextVar[bool] = ContextVar("background_job", default=False)
BACKGROUND_SHARE = 0.85


class RateLimiter:
    """Janelas deslizantes, p.ex. [(20, 1), (100, 120)] = 20/s e 100/2min (limites da dev/personal key)."""

    def __init__(self, windows):
        self.windows = [(limit, period, deque()) for limit, period in windows]
        self.lock = asyncio.Lock()

    async def acquire(self):
        share = BACKGROUND_SHARE if background_job.get() else 1
        while True:
            async with self.lock:
                now = time.monotonic()
                wait = 0.0
                for limit, period, stamps in self.windows:
                    while stamps and now - stamps[0] >= period:
                        stamps.popleft()
                    cap = int(limit * share)
                    if len(stamps) >= cap:
                        wait = max(wait, period - (now - stamps[len(stamps) - cap]) + 0.05)
                if wait <= 0:
                    for _, _, stamps in self.windows:
                        stamps.append(now)
                    return
            await asyncio.sleep(wait)  # fora do lock: um pedido do utilizador pode passar à frente


class RiotAPI:
    def __init__(self, api_key: str, platform: str, region: str):
        self.platform = platform
        self.platform_url = f"https://{platform}.api.riotgames.com"
        self.region_url = f"https://{region}.api.riotgames.com"
        self.client = httpx.AsyncClient(timeout=20, headers={"X-Riot-Token": api_key})
        self.limiter = RateLimiter([(20, 1), (100, 120)])
        self.enabled = bool(api_key)

    async def close(self):
        await self.client.aclose()

    async def _get(self, url: str, params: dict | None = None):
        if not self.enabled:
            raise RiotAPIError("RIOT_API_KEY is not set in .env")
        for attempt in range(5):
            await self.limiter.acquire()
            try:
                r = await self.client.get(url, params=params)
            except httpx.TransportError:
                # ligação caiu ou a meio da resposta: espera (2, 4, 8, 16, 32 s) e tenta outra vez
                await asyncio.sleep(2 ** (attempt + 1))
                continue
            if r.status_code == 429:
                await asyncio.sleep(int(r.headers.get("Retry-After", "5")))
                continue
            if r.status_code == 404:
                return None
            if r.status_code in (401, 403):
                raise RiotAPIError("Invalid or expired Riot API key (a dev key lasts 24h)")
            if r.status_code >= 500:
                await asyncio.sleep(2)
                continue
            r.raise_for_status()
            return r.json()
        raise RiotAPIError("The Riot API did not respond after several attempts: check your internet connection or try again later")

    # --- account / summoner / league ---
    async def account_by_riot_id(self, game_name: str, tag_line: str):
        return await self._get(
            f"{self.region_url}/riot/account/v1/accounts/by-riot-id/{quote(game_name)}/{quote(tag_line)}"
        )

    async def account_by_puuid(self, puuid: str):
        return await self._get(f"{self.region_url}/riot/account/v1/accounts/by-puuid/{puuid}")

    async def mastery_top(self, puuid: str, count: int = 10):
        return await self._get(
            f"{self.platform_url}/lol/champion-mastery/v4/champion-masteries/by-puuid/{puuid}/top",
            {"count": count}) or []

    async def summoner_by_puuid(self, puuid: str):
        return await self._get(f"{self.platform_url}/lol/summoner/v4/summoners/by-puuid/{puuid}")

    async def league_entries_by_puuid(self, puuid: str):
        return await self._get(f"{self.platform_url}/lol/league/v4/entries/by-puuid/{puuid}") or []

    async def apex_league(self, tier: str, queue: str = "RANKED_SOLO_5x5"):
        """tier: challenger | grandmaster | master"""
        return await self._get(f"{self.platform_url}/lol/league/v4/{tier}leagues/by-queue/{queue}")

    async def mastery_champion(self, puuid: str, champion_id: int):
        return await self._get(
            f"{self.platform_url}/lol/champion-mastery/v4/champion-masteries/by-puuid/{puuid}/by-champion/{champion_id}")

    async def active_game(self, puuid: str):
        """O jogo a decorrer desta conta (None se não estiver em jogo)."""
        return await self._get(f"{self.platform_url}/lol/spectator/v5/active-games/by-summoner/{puuid}")

    # --- match-v5 ---
    async def match_ids(self, puuid: str, queue: int | None = None, start: int = 0, count: int = 20,
                        start_time: int | None = None) -> list[str]:
        params = {"start": start, "count": count}
        if queue is not None:
            params["queue"] = queue
        if start_time is not None:
            params["startTime"] = start_time
        return await self._get(f"{self.region_url}/lol/match/v5/matches/by-puuid/{puuid}/ids", params) or []

    async def match(self, match_id: str):
        return await self._get(f"{self.region_url}/lol/match/v5/matches/{match_id}")

    async def timeline(self, match_id: str):
        return await self._get(f"{self.region_url}/lol/match/v5/matches/{match_id}/timeline")

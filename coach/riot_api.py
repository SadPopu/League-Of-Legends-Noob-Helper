"""Cliente mínimo da API pública da Riot, com rate limiting."""
import asyncio
import time
from collections import deque
from urllib.parse import quote

import httpx


class RiotAPIError(Exception):
    pass


class RateLimiter:
    """Janelas deslizantes, p.ex. [(20, 1), (100, 120)] = 20/s e 100/2min (limites da dev/personal key)."""

    def __init__(self, windows):
        self.windows = [(limit, period, deque()) for limit, period in windows]
        self.lock = asyncio.Lock()

    async def acquire(self):
        async with self.lock:
            while True:
                now = time.monotonic()
                wait = 0.0
                for limit, period, stamps in self.windows:
                    while stamps and now - stamps[0] >= period:
                        stamps.popleft()
                    if len(stamps) >= limit:
                        wait = max(wait, period - (now - stamps[0]) + 0.05)
                if wait <= 0:
                    for _, _, stamps in self.windows:
                        stamps.append(now)
                    return
                await asyncio.sleep(wait)


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
        for _ in range(5):
            await self.limiter.acquire()
            r = await self.client.get(url, params=params)
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
        raise RiotAPIError(f"Failed after several attempts: {url}")

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

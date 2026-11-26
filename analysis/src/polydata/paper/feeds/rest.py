"""Read-only public REST (Gamma, Data API) with the recorder's rate-limit handling (polymarket.ts getJson,
noteError, backoff). Nothing here authenticates."""

from __future__ import annotations

import asyncio
import json
import urllib.error
import urllib.request
from typing import Callable, Literal

GAMMA_URL = "https://gamma-api.polymarket.com"  # config.ts gammaUrl
DATA_URL = "https://data-api.polymarket.com"  # config.ts dataApiUrl
TIMEOUT_S = 15

Api = Literal["gamma", "data"]


class HttpError(Exception):
    def __init__(self, status: int, message: str, retry_after_s: float | None = None):
        super().__init__(message)
        self.status = status
        self.retry_after_s = retry_after_s


class RateLimited(HttpError):
    """HTTP 429, or a call made while that API is paused."""


def backoff_ms(failures: int) -> int:
    """Per-market backoff after `failures` consecutive failures: 30 s, 60 s, ... capped at 10 min."""
    return min(30_000 * 2 ** (max(failures, 1) - 1), 600_000)


def _fetch(url: str) -> object:
    req = urllib.request.Request(url, headers={"User-Agent": "polydata-paper/0.1", "Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as r:
            return json.loads(r.read())
    except urllib.error.HTTPError as e:
        ra = None
        try:
            ra = float(e.headers.get("Retry-After"))
            ra = ra if ra > 0 else None
        except (TypeError, ValueError):
            pass
        raise HttpError(e.code, f"{e.code} {url[:160]}", ra) from None


class Rest:
    def __init__(self, clock: Callable[[], int], event: Callable[[str, str, str], None]):
        self.clock = clock
        self.event = event
        self.paused_until: dict[str, int] = {"gamma": 0, "data": 0}

    def is_paused(self, api: str) -> bool:
        return self.clock() < self.paused_until[api]

    async def get_json(self, api: Api, url: str) -> object:
        if self.is_paused(api):
            raise RateLimited(429, f"{api} paused", None)
        try:
            return await asyncio.to_thread(_fetch, url)
        except HttpError as e:
            if e.status == 429:
                pause_s = max(e.retry_after_s or 60, 10)  # Polymarket sends Retry-After: 1, too short to help
                self.paused_until[api] = self.clock() + int(pause_s * 1000)
                self.event("rate_limited", f"pm_{api}", f"429, pausing {pause_s:g} s: {str(e)[4:120]}")
                raise RateLimited(429, str(e), e.retry_after_s) from None
            raise

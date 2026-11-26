"""Binance USD-M futures feed (src/recorder/binance.ts:69,73): bookTicker and aggTrade only."""

from __future__ import annotations

import asyncio
import json
from typing import Callable

from .polymarket import txt
from .ws import ReconnectingWs

FUT_PUBLIC_WS = "wss://fstream.binance.com/public/stream?streams="  # config.ts binanceFutPublicWs
FUT_MARKET_WS = "wss://fstream.binance.com/market/stream?streams="  # config.ts binanceFutMarketWs
STALE_MS = 30_000
MAX_LIFETIME_MS = 23 * 3_600_000


def parse_fut(raw: str) -> list[tuple[str, tuple]]:
    msg = json.loads(raw)
    stream, d = msg.get("stream", ""), msg.get("data")
    kind = stream[stream.find("@") + 1:]
    if kind == "bookTicker":
        return [("bn_fut_bbo", tuple(txt(d.get(k)) for k in ("s", "u", "E", "T", "b", "B", "a", "A")))]
    if kind == "aggTrade":
        return [("bn_fut_trade", tuple(txt(d.get(k)) for k in ("s", "E", "T", "a", "f", "l", "p", "q")) + ("1" if d.get("m") else "0",))]
    return []


class BinanceFeed:
    def __init__(self, coins: list[str], emit: Callable[[list[tuple[str, object]]], None],
                 event: Callable[[str, str, str], None], clock: Callable[[], int]):
        syms = [f"{c.lower()}usdt" for c in coins]

        def on_message(raw: str, recv_ms: int) -> None:
            rows = parse_fut(raw)
            if rows:
                self.emit(rows)

        self.emit = emit
        common = dict(stale_ms=STALE_MS, max_lifetime_ms=MAX_LIFETIME_MS, clock=clock)
        bbo_url = FUT_PUBLIC_WS + "/".join(f"{s}@bookTicker" for s in syms)
        trade_url = FUT_MARKET_WS + "/".join(f"{s}@aggTrade" for s in syms)
        self.conns = [
            ReconnectingWs("binance-fut-bbo", lambda: bbo_url, on_message, event, **common),
            ReconnectingWs("binance-fut-market", lambda: trade_url, on_message, event, **common),
        ]

    async def run(self) -> None:
        await asyncio.gather(*(c.run() for c in self.conns))

    def stop(self) -> None:
        for c in self.conns:
            c.stop()

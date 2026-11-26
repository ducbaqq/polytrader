"""Polymarket real-time data socket: Chainlink prices (src/recorder/rtds.ts). Two redundant connections,
deduplicated by (topic, symbol) timestamp."""

from __future__ import annotations

import asyncio
import json
from typing import Callable

from .polymarket import txt
from .ws import ReconnectingWs

RTDS_WS_URL = "wss://ws-live-data.polymarket.com"  # config.ts rtdsWsUrl
SUBSCRIBE = json.dumps({
    "action": "subscribe",
    "subscriptions": [
        {"topic": "crypto_prices_chainlink", "type": "*", "filters": ""},
        {"topic": "crypto_prices", "type": "update"},
    ],
})


def parse_rtds(raw: str, conn: str, last_ts: dict) -> list[tuple[str, object]]:
    """rtds_price only if ts > last_ts[topic|symbol] (updated here); non-updates go to rtds_other with conn."""
    if not raw or raw == "PONG":
        return []
    m = json.loads(raw)
    if not isinstance(m, dict):
        return [("rtds_other", {"conn": conn, "raw": m})]
    p = m.get("payload")
    # recorder: `p.symbol !== undefined && p.value !== undefined` (JSON null still counts as present)
    if m.get("type") == "update" and isinstance(p, dict) and "symbol" in p and "value" in p:
        k = f"{m.get('topic')}|{p['symbol']}"
        try:
            ts = float(p["timestamp"])
        except (KeyError, TypeError, ValueError):
            ts = float("nan")  # JS Number(undefined) is NaN: `NaN <= x` is false, so the row is written
        if ts <= last_ts.get(k, 0):
            return []  # already written by the other connection
        last_ts[k] = ts
        return [("rtds_price", (txt(m.get("topic")), txt(p["symbol"]), txt(p.get("timestamp")), txt(p["value"]),
                                txt(p.get("full_accuracy_value")), txt(m.get("timestamp")), conn))]
    return [("rtds_other", {"conn": conn, **m})]


class RtdsFeed:
    def __init__(self, emit: Callable[[list[tuple[str, object]]], None], event: Callable[[str, str, str], None], clock: Callable[[], int]):
        self.emit, self.event, self.clock = emit, event, clock
        self.last_ts: dict[str, float] = {}
        self.conns = {cid: self._conn(cid) for cid in ("a", "b")}

    def _conn(self, cid: str) -> ReconnectingWs:
        def on_message(raw: str, recv_ms: int) -> None:
            rows = parse_rtds(raw, cid, self.last_ts)
            if rows:
                self.emit(rows)

        return ReconnectingWs(
            f"polymarket-rtds-{cid}", lambda: RTDS_WS_URL, on_message, self.event,
            on_open=lambda ws: ws.send(SUBSCRIBE), text_ping=("PING", 5_000), stale_ms=5_000, clock=self.clock,
        )

    async def run(self) -> None:
        async def second():
            await asyncio.sleep(2)  # stagger so they don't reconnect in lockstep
            await self.conns["b"].run()

        await asyncio.gather(self.conns["a"].run(), second())

    def stop(self) -> None:
        for c in self.conns.values():
            c.stop()

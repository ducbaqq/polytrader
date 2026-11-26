"""Reachability smoke test for the paper-runner feeds. Public read-only endpoints; writes nothing.

    uv run python tools/feed_smoke.py --minutes 10 --coins btc eth --timeframes 5m
"""

from __future__ import annotations

import argparse
import asyncio
import collections
import time

from polydata.paper.feeds.binance import BinanceFeed
from polydata.paper.feeds.polymarket import PolymarketFeed
from polydata.paper.feeds.rest import Rest
from polydata.paper.feeds.rtds import RtdsFeed
from polydata.paper.feeds.series import series_for


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--minutes", type=float, default=10)
    ap.add_argument("--coins", nargs="+", default=["btc", "eth"])
    ap.add_argument("--timeframes", nargs="+", default=["5m"])
    a = ap.parse_args()
    clock = lambda: int(time.time() * 1000)  # noqa: E731
    counts: collections.Counter = collections.Counter()
    kinds: collections.Counter = collections.Counter()
    t0 = time.time()

    def emit(rows):
        for stream, _ in rows:
            counts[stream] += 1

    def event(kind, source, detail):
        kinds[kind] += 1
        print(f"[{time.time() - t0:7.1f}s] {kind} {source} {detail}", flush=True)

    pm = PolymarketFeed(series_for(a.coins, a.timeframes), emit, event, Rest(clock, event), clock)
    feeds = [pm, RtdsFeed(emit, event, clock), BinanceFeed(a.coins, emit, event, clock)]
    tasks = [asyncio.ensure_future(f.run()) for f in feeds]
    max_lag = [0.0]

    async def lag_monitor():
        while True:
            t = time.perf_counter()
            await asyncio.sleep(0.1)
            max_lag[0] = max(max_lag[0], time.perf_counter() - t - 0.1)

    lag_task = asyncio.ensure_future(lag_monitor())
    prev: collections.Counter = collections.Counter()
    end = t0 + a.minutes * 60
    while time.time() < end:
        await asyncio.sleep(min(60, max(0.0, end - time.time())))
        d = {k: counts[k] - prev[k] for k in counts}
        prev = collections.Counter(counts)
        print(f"[{time.time() - t0:7.1f}s] last-interval rows: {dict(sorted(d.items()))} markets known: {len(pm.markets())}", flush=True)
    for f in feeds:
        f.stop()
    await asyncio.gather(*tasks, return_exceptions=True)
    el = time.time() - t0
    print(f"\nTOTAL after {el:.0f}s")
    for k, v in sorted(counts.items()):
        print(f"  {k:18s} {v:9d} rows  {v / el:8.2f}/s")
    print("  events:", dict(kinds))
    lag_task.cancel()
    print(f"  max event-loop lag: {max_lag[0] * 1000:.0f} ms")


if __name__ == "__main__":
    asyncio.run(main())

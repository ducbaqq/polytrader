"""Test helpers: feed recorder-shaped Polymarket rows to `GrowingMarketData` the way the live runner releases them,
and compare it with a finished `MarketData` (every query on a dense time grid, both clocks, plus every array)."""

from __future__ import annotations

import dataclasses
import heapq
import math
from collections import defaultdict
from typing import NamedTuple

import numpy as np
import pandas as pd

from polydata.bt.marketdata import SIDES, MarketData

STREAM_ORDER = {"pm_book": 0, "pm_price_change": 1, "pm_trade": 2, "tick": 3}


class Row(NamedTuple):
    """One recorder row: `values` are the columns after recv_ms as text (for "tick": (ts_ms, new_tick_size))."""
    stream: str
    recv_ms: int
    seq: int
    values: tuple


def frames(rows: list[Row]) -> tuple[pd.DataFrame, pd.DataFrame, pd.DataFrame, pd.DataFrame]:
    """The same rows as `MarketData.from_frames` input (text columns, NULL for an empty field, as the build reads
    the raw TSV)."""
    def nz(x):
        return None if x == "" else x

    c, b, t, k = [], [], [], []
    for r in rows:
        v = r.values
        if r.stream == "pm_price_change":
            c.append((v[0], r.recv_ms, "Down" if v[1].endswith("|D") else "Up", v[2], v[3], v[4], nz(v[5]), nz(v[6]),
                      r.seq))
        elif r.stream == "pm_book":
            b.append((v[0], r.recv_ms, "Down" if v[1].endswith("|D") else "Up", nz(v[2]), nz(v[3]), r.seq))
        elif r.stream == "pm_trade":
            t.append((v[0], r.recv_ms, "Down" if v[1].endswith("|D") else "Up", v[2], v[3], v[4], r.seq))
        else:
            k.append((int(v[0]), r.recv_ms, float(v[1])))
    obj = dict(dtype=object)
    return (pd.DataFrame(c, columns=["ts_ms", "recv_ms", "outcome", "side", "price", "size", "best_bid", "best_ask",
                                     "seq"], **obj),
            pd.DataFrame(b, columns=["ts_ms", "recv_ms", "outcome", "bids", "asks", "seq"], **obj),
            pd.DataFrame(t, columns=["ts_ms", "recv_ms", "outcome", "side", "price", "size", "seq"], **obj),
            pd.DataFrame(k, columns=["ts_ms", "recv_ms", "new_tick_size"]))


class LiveFeed:
    """Simulates the live release: rows are ingested at their recv_ms (wall clock), `H = wall - hold_back`
    releases every buffered row with ts_ms <= H (whole ts_ms at once, increasing ts_ms) via `add_ts`, and the
    strategy clock `T = H - skew` releases the recv side. A row arriving with ts_ms <= H is late (an error here)."""

    def __init__(self, md, rows: list[Row], hold_back: int = 3000, skew: int = 1000):
        self.md, self.hold, self.skew = md, hold_back, skew
        self.rows = sorted(rows, key=lambda r: (r.recv_ms, STREAM_ORDER[r.stream], r.seq))
        self.i = 0
        self.buf: dict[int, dict[str, list]] = defaultdict(lambda: defaultdict(list))
        self.heap: list[int] = []  # buffered ts_ms
        self.H = self.T = -(2**62)
        self.trades_added = 0

    def ingest(self, wall: int) -> None:
        while self.i < len(self.rows) and self.rows[self.i].recv_ms <= wall:
            r = self.rows[self.i]
            self.i += 1
            ts = int(r.values[0])
            if ts <= self.H:
                raise AssertionError(f"late row {r} (H={self.H})")
            if ts > r.recv_ms + self.skew:
                raise AssertionError(f"future-stamped row {r}")
            if ts not in self.buf:
                heapq.heappush(self.heap, ts)
            if r.stream == "tick":
                self.buf[ts]["tick"].append((r.recv_ms, float(r.values[1])))
            else:
                self.buf[ts][r.stream].append((r.recv_ms, r.seq, r.values))

    def step(self, wall: int) -> tuple[int, int, int, int]:
        """Advance the wall clock; returns (H_prev, H, T_prev, T)."""
        self.ingest(wall)
        H_prev, T_prev = self.H, self.T
        H = wall - self.hold
        while self.heap and self.heap[0] <= H:
            ts = heapq.heappop(self.heap)
            g = self.buf.pop(ts)
            self.trades_added += self.md.add_ts(ts, g.get("pm_price_change", []), g.get("pm_book", []),
                                                g.get("pm_trade", []), g.get("tick", []))
        self.H, self.T = H, H - self.skew
        self.md.release_recv(self.T)
        return H_prev, H, T_prev, self.T

    def end_wall(self) -> int:
        return max(r.recv_ms for r in self.rows) + self.hold + self.skew + 1

    def run(self, step_ms: int = 20, on_step=None) -> None:
        wall = self.rows[0].recv_ms
        end = self.end_wall()
        while True:
            span = self.step(wall)
            if on_step is not None:
                on_step(*span)
            if wall >= end:
                break
            wall = min(wall + step_ms, end)
        assert not self.buf and self.md.pending_recv() == 0


def _same(a, b) -> bool:
    if isinstance(a, float) and isinstance(b, float) and math.isnan(a) and math.isnan(b):
        return True
    return a == b


def _top_eq(a, b) -> bool:
    x, y = dataclasses.astuple(a), dataclasses.astuple(b)
    return len(x) == len(y) and all(_same(u, v) for u, v in zip(x, y))


def grid(t_lo: int, t_hi: int, step: int, origin: int = 0) -> range:
    """Times in (t_lo, t_hi] on the grid origin + step * i."""
    first = t_lo + 1 + (-(t_lo + 1 - origin)) % step
    return range(first, t_hi + 1, step)


def compare_queries(live, ref: MarketData, times, clock: str, levels=None, depth: int | None = None,
                    level_changes_from: int | None = None) -> int:
    """Every query at every time in `times` on `clock`; returns the number of comparisons."""
    levels = sorted(set(ref.levels()) | set(live.levels())) if levels is None else levels
    n = 0
    prev = level_changes_from
    for t in times:
        assert live.book_at(t, clock, depth) == ref.book_at(t, clock, depth), (t, clock)
        a, b = live.top_at(t, clock), ref.top_at(t, clock)
        assert _top_eq(a, b), (t, clock, a, b)
        assert live.tick_at(t, clock) == ref.tick_at(t, clock), (t, clock)
        for side, px in levels:
            assert live.size_at(side, px, t, clock) == ref.size_at(side, px, t, clock), (side, px, t, clock)
            if prev is not None:
                la, lb = live.level_changes(side, px, prev, t, clock), ref.level_changes(side, px, prev, t, clock)
                assert all(np.array_equal(x, y) for x, y in zip(la, lb)), (side, px, prev, t, clock)
        n += 1
        prev = t
    return n


def compare_arrays(live, ref: MarketData) -> None:
    """Every array (values and dtypes) and the level keys."""
    assert sorted(live.levels()) == sorted(ref.levels())
    for side in SIDES:
        assert live.prices(side) == ref.prices(side), side
    for key in ref.levels():
        a, b = live._levels[key], ref._levels[key]
        for f in ("ts_ms", "recv_ms", "recv_eff", "size", "last_pos"):
            x, y = getattr(a, f), getattr(b, f)
            assert np.array_equal(x, y), (key, f, x, y)
            assert x.dtype == y.dtype, (key, f, x.dtype, y.dtype)
    for name in ("tops", "tops_recv", "trades", "trades_recv", "ticks", "_ticks_recv"):
        x, y = getattr(live, name), getattr(ref, name)
        assert type(x) is type(y)
        for f, u, v in zip(x._fields, x, y):
            assert np.array_equal(u, v), (name, f, u, v)
            assert u.dtype == v.dtype, (name, f, u.dtype, v.dtype)
    assert np.array_equal(live.snaps_recv, ref.snaps_recv)
    assert live.snaps_recv.dtype == ref.snaps_recv.dtype

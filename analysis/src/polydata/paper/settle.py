"""Chainlink settlement (spec "Settlement"): a paper market's outcome from the Chainlink ticks the router keeps.

`ChainlinkSettler` implements the router's settler protocol (`bt.live.LiveRouter`): `admit(info, admitted_ms)` for
every admitted market, `on_tick(coin, recv_ms, ts_ms, price)` for every kept Chainlink tick (first receipt per
`ts_ms`, received at or after `rs`, in receive order). On a market's first tick with `ts_ms >= window_end_ms` (received
at `r_end`) it decides:

- `close_twap` = mean of the coin's ticks with `ts_ms` in `[window_end - 60 s, window_end)` received so far;
- `ptb` = mean of the ticks with `ts_ms` in `[window_start - 60 s, window_start)` received up to the first tick with
  `ts_ms >= window_start` (the revealing tick): the value `Core.scan_ptb` shows the strategy;
- both `float(np.mean(...))` over the prices in receive order; outcome `Up` iff `close_twap >= ptb`.

If either window holds no tick, or `window_start - 60 s < rs` (the start window was not watched), the market is listed
in `gave_up` and gets no resolution here: it waits for Gamma (Task 9), or is voided "no outcome" at the end.
"""

from __future__ import annotations

import copy
from bisect import bisect_left

import numpy as np

from polydata.bt.live import Resolution
from polydata.bt.types import MarketInfo

WINDOW_MS = 60_000
SOURCE = "paper-chainlink"


class _Ticks:
    """One coin's kept ticks in receive order, with the running max of `ts_ms` (nondecreasing) for a bisect start."""
    __slots__ = ("recv", "ts", "px", "runmax")

    def __init__(self):
        self.recv: list[int] = []
        self.ts: list[int] = []
        self.px: list[float] = []
        self.runmax: list[int] = []

    def add(self, recv: int, ts: int, px: float) -> None:
        self.recv.append(recv)
        self.ts.append(ts)
        self.px.append(px)
        self.runmax.append(ts if not self.runmax or ts > self.runmax[-1] else self.runmax[-1])

    def window(self, lo: int, hi: int, recv_max: int | None = None) -> list[float]:
        """Prices of ticks with lo <= ts < hi (and recv <= recv_max), in receive order."""
        i = bisect_left(self.runmax, lo)  # every earlier tick has ts < lo
        ts, recv, px = self.ts, self.recv, self.px
        out = []
        for j in range(i, len(ts)):
            if recv_max is not None and recv[j] > recv_max:
                break
            if lo <= ts[j] < hi:
                out.append(px[j])
        return out

    def first_at_or_after(self, t: int) -> int | None:
        """Receive time of the first tick (receive order) with ts >= t."""
        i = bisect_left(self.runmax, t)  # the first tick whose running max reaches t is that tick
        return self.recv[i] if i < len(self.recv) else None


class ChainlinkSettler:
    def __init__(self, rs: int):
        self.rs = int(rs)
        self.ticks: dict[str, _Ticks] = {}
        self.pending: dict[str, dict[str, MarketInfo]] = {}  # coin -> slug -> info, admitted and not decided
        self.gave_up: dict[str, str] = {}  # slug -> reason (no Chainlink outcome: waits for Gamma)
        self.decided: dict[str, Resolution] = {}

    def admit(self, info: MarketInfo, admitted_ms: int) -> None:
        self.pending.setdefault(info.coin, {})[info.slug] = info

    def on_tick(self, coin: str, recv_ms: int, ts_ms: int, price: float) -> list[Resolution]:
        t = self.ticks.get(coin)
        if t is None:
            t = self.ticks[coin] = _Ticks()
        t.add(int(recv_ms), int(ts_ms), float(price))
        pend = self.pending.get(coin)
        if not pend:
            return []
        out = []
        for slug in [s for s, m in pend.items() if ts_ms >= m.window_end_ms]:
            info = pend.pop(slug)
            res = self._decide(t, info, int(recv_ms))
            if res is not None:
                self.decided[slug] = res
                out.append(res)
        return out

    def _decide(self, t: _Ticks, info: MarketInfo, r_end: int) -> Resolution | None:
        ws, we = int(info.window_start_ms), int(info.window_end_ms)
        if ws - WINDOW_MS < self.rs:
            self.gave_up[info.slug] = "start window before the session start"
            return None
        r_reveal = t.first_at_or_after(ws)
        start = t.window(ws - WINDOW_MS, ws, r_reveal)
        close = t.window(we - WINDOW_MS, we)
        if not start or not close:
            self.gave_up[info.slug] = "no Chainlink tick in the " + ("start" if not start else "end") + " window"
            return None
        ptb = float(np.mean(np.array(start, dtype=np.float64)))
        twap = float(np.mean(np.array(close, dtype=np.float64)))
        return Resolution(info.slug, "Up" if twap >= ptb else "Down", r_end, SOURCE, ptb, twap)


def resolution_row(res: Resolution, discovery_event: dict | None) -> dict:
    """The `pm_resolution` row (`d`) for a settler (or Gamma fallback) resolution: the market's discovery event with
    `eventMetadata.priceToBeat = ptb` and `finalPrice = close_twap`, read by `views.markets_sql` as the recorder's."""
    ev = copy.deepcopy(discovery_event) if discovery_event else {"slug": res.slug}
    md = ev.get("eventMetadata")
    md = dict(md) if isinstance(md, dict) else {}
    md["priceToBeat"] = res.ptb
    md["finalPrice"] = res.close_twap
    ev["eventMetadata"] = md
    return {"slug": res.slug, "outcome": res.outcome, "event": ev, "source": res.source}


__all__ = ["ChainlinkSettler", "SOURCE", "resolution_row"]

"""`mm-side-pull` (family `market-making`): `mm-binance-pull` plus a one-sided, more sensitive pull.

Research note: docs/strategies/mm-side-pull.md. On the baseline's first-half fills (2026-11-24 < 17:15
UTC), fills made right after Binance moved AGAINST the filled side (we bought Up while the coin fell over
the previous 2 s) lost 2.6-4.0 cents per share to adverse selection, while fills after a move WITH the
filled side gained about 1 cent. Most of those moves are 1-2 bp, below the symmetric 2-sigma pull.

So, on top of the symmetric pull (all quotes, `pull_k`), this strategy watches the signed Binance return
over `side_window_ms`:

- r < -side_k x sigma_1s x sqrt(side_window_s): the coin fell, the Up bid is the stale side. Bids
  (Up BUY) on that coin's markets are cancelled and not re-placed for `side_cooldown_ms`.
- r > +side_k x ...: the coin rose; asks (Up SELL / Down BUY) are pulled the same way.

The other side keeps quoting: it is the side the informed flow is not trading against. sigma_1s is the
parent's 15-minute realised volatility. A new move restarts the side's pause. Everything else (centre,
skew, inventory cap, time band, book-spread filter, churn rules) is the parent's.
"""

from __future__ import annotations

import math
from collections import deque

from ..strategy import Ctx, register
from ..types import Bbo
from .mm_binance_pull import VOL_WINDOW_S, MmBinancePull, _Mkt


@register
class MmSidePull(MmBinancePull):
    name = "mm-side-pull"
    family = "market-making"
    default_params = {
        **MmBinancePull.default_params,
        "side_k": 0.5,  # one-sided pull threshold, in sigma_1s x sqrt(side_window_s)
        "side_window_ms": 2000,
        "side_cooldown_ms": 3000,
    }

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        p = self.params
        self.side_k = float(p["side_k"])
        self.side_win_ms = int(p["side_window_ms"])
        self.side_cool_ms = int(p["side_cooldown_ms"])
        self.swin: dict[str, deque[tuple[int, float]]] = {}  # by coin: (recv ms, mid) over side_window_ms
        self.bid_paused: dict[str, int] = {}  # coin -> pause end (ms)
        self.ask_paused: dict[str, int] = {}

    def on_binance(self, ctx: Ctx, sym: str, bbo: Bbo) -> None:
        super().on_binance(ctx, sym, bbo)
        coin = self.coin_of[sym]
        v = self.vol[coin]
        t, mid = bbo.recv_ms, (bbo.bid + bbo.ask) / 2
        w = self.swin.get(coin)
        if w is None:
            w = self.swin[coin] = deque()
        w.append((t, mid))
        cut = t - self.side_win_ms
        while len(w) > 1 and w[1][0] <= cut:
            w.popleft()
        if not v.ready or w[0][0] > cut:
            return
        sigma = math.sqrt(max(v.sum, 0.0) / VOL_WINDOW_S)
        thr = self.side_k * sigma * math.sqrt(self.side_win_ms / 1000)
        r = math.log(mid / w[0][1])
        if r < -thr:
            side, paused = "bid", self.bid_paused
        elif r > thr:
            side, paused = "ask", self.ask_paused
        else:
            return
        if paused.get(coin, 0) <= t:
            ctx.log("side-pull", coin, side, f"{r:+.6f}")
        paused[coin] = t + self.side_cool_ms
        ctx.set_timer(f"resume:{coin}", self.side_cool_ms)  # the parent requotes the coin's markets
        for m in self.by_coin.get(coin, ()):
            self._cancel_side(ctx, getattr(m, side))

    def _targets(self, ctx: Ctx, m: _Mkt) -> tuple[int | None, int | None]:
        bid, ask = super()._targets(ctx, m)
        coin, now = m.info.coin, ctx.now
        if self.bid_paused.get(coin, 0) > now:
            bid = None
        if self.ask_paused.get(coin, 0) > now:
            ask = None
        return bid, ask


__all__ = ["MmSidePull"]

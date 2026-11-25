"""`fv-taker` (family `fair-value`): take a token whose model fair value beats its ask by a margin.

Simulator version of the `fair-value-deviation` screen (docs/strategies/fair-value-deviation.md). For each
15m/1h market (param `timeframes`) with time left in [`min_tau_s`, `max_tau_s`], on every book change and
every `check_ms`: p = `ctx.fair_value` (Up). For each token with fair f (Up: p, Down: 1 - p):

- limit = largest tick price L with L + fee_ps(L) <= f - margin (`fee_aware`, the screen's rule
  fair - ask - fee > margin), or L = f - margin rounded down to the tick when `fee_aware` is false
- the token's book spread must be <= `max_spread`
- the visible ask size at prices <= L (book depth 10) is the take size, capped at `size`; below
  the market's minimum order size there is no trade

The best-edge token is taken as an IOC order at limit L. A market allows `max_entries` entries (an entry
counts once an order of it fills); an order that fills nothing may be retried, at most `max_tries` orders per
market, `retry_ms` apart. Held to resolution (no exit). Inputs come from `ctx` only.
"""

from __future__ import annotations

import math

from ..strategy import Ctx, OrderView, Strategy, register
from ..types import MarketInfo

DEPTH = 10


def fee_ps(price: float, rate: float, exponent: float) -> float:
    return rate * (price * (1.0 - price)) ** exponent


def limit_price(fair: float, margin: float, tick: float, rate: float, exponent: float, fee_aware: bool) -> float | None:
    """Highest on-tick limit with limit (+ fee) <= fair - margin, or None when it is outside (0, 1)."""
    target = fair - margin
    n = math.floor(target / tick + 1e-9)
    if fee_aware:
        while n > 0 and n * tick + fee_ps(n * tick, rate, exponent) > target + 1e-12:
            n -= 1
    p = round(n * tick, 6)
    return p if 0 < p < 1 else None


@register
class FvTaker(Strategy):
    name = "fv-taker"
    family = "fair-value"
    default_params = {
        "margin": 0.02,
        "min_tau_s": 300,
        "max_tau_s": 1200,
        "timeframes": ["15m", "1h"],
        "size": 10.0,
        "max_entries": 1,
        "max_tries": 5,
        "retry_ms": 1000,
        "check_ms": 1000,
        "max_spread": 0.10,
        "fee_aware": True,
    }

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self.entries: dict[str, int] = {}
        self.tries: dict[str, int] = {}
        self.pending: dict[str, int] = {}  # slug -> order id in flight
        self.last_try: dict[str, int] = {}
        self.mkts: dict[str, MarketInfo] = {}

    def on_market_open(self, ctx: Ctx, market: MarketInfo) -> None:
        if market.timeframe not in self.params["timeframes"]:
            return
        self.mkts[market.slug] = market
        self.entries[market.slug] = self.tries[market.slug] = 0
        ctx.set_timer(f"chk:{market.slug}", self.params["check_ms"])

    def on_book(self, ctx: Ctx, market: MarketInfo, book) -> None:
        if market.slug in self.mkts:
            self._decide(ctx, market)

    def on_timer(self, ctx: Ctx, name: str) -> None:
        m = self.mkts.get(name[4:]) if name.startswith("chk:") else None
        if m is None:
            return
        if self._tau(ctx, m) <= self.params["min_tau_s"] or self.entries[m.slug] >= self.params["max_entries"]:
            return  # window left the band (or done): stop polling
        self._decide(ctx, m)
        ctx.set_timer(name, self.params["check_ms"])

    def on_order(self, ctx: Ctx, order: OrderView) -> None:
        if self.pending.get(order.slug) != order.id or order.is_open:
            return
        del self.pending[order.slug]
        if order.filled > 0:
            self.entries[order.slug] += 1

    def on_market_close(self, ctx: Ctx, market: MarketInfo) -> None:
        for d in (self.mkts, self.entries, self.tries, self.pending, self.last_try):
            d.pop(market.slug, None)

    # ------------------------------------------------------------------------------------------
    @staticmethod
    def _tau(ctx: Ctx, m: MarketInfo) -> float:
        return (m.window_end_ms - ctx.now) / 1000.0

    def _decide(self, ctx: Ctx, m: MarketInfo) -> None:
        p, slug = self.params, m.slug
        if slug in self.pending or self.entries[slug] >= p["max_entries"] or self.tries[slug] >= p["max_tries"]:
            return
        tau = self._tau(ctx, m)
        if not p["min_tau_s"] <= tau <= p["max_tau_s"]:
            return
        if ctx.now - self.last_try.get(slug, -10**12) < p["retry_ms"]:
            return
        fair = ctx.fair_value(slug)
        if fair is None:
            return
        book = ctx.book(slug, DEPTH)
        if book is None:
            return
        tick = m.tick_px / 1000.0
        best = None  # (edge, token, limit, size)
        for token, f in (("Up", fair), ("Down", 1.0 - fair)):
            v = book.for_token(token)
            if not v.asks or not v.bids or v.spread is None or v.spread > p["max_spread"] + 1e-9:
                continue
            lim = limit_price(f, p["margin"], tick, m.fee_rate, m.fee_exponent, p["fee_aware"])
            if lim is None or v.asks[0][0] > lim + 1e-9:
                continue
            avail = sum(s for px, s in v.asks if px <= lim + 1e-9)
            n = min(p["size"], avail)
            if n < m.min_order_size:
                continue
            edge = f - v.asks[0][0] - fee_ps(v.asks[0][0], m.fee_rate, m.fee_exponent)
            if best is None or edge > best[0]:
                best = (edge, token, lim, n)
        if best is None:
            return
        _, token, lim, n = best
        self.tries[slug] += 1
        self.last_try[slug] = ctx.now
        self.pending[slug] = ctx.take(slug, token, "BUY", n, lim)


__all__ = ["FvTaker", "fee_ps", "limit_price"]

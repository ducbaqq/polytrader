"""`xtf-arb` (family `cross-timeframe`): buy Up of the lower-strike and Down of the higher-strike market
when two end-aligned markets of one coin cost less than 1 together (docs/strategies/xtf-arb.md).

Markets of one coin with different timeframes that end at the same time settle on the same final price F,
Up iff F >= K, with different strikes K. With K_A < K_B, one Up_A + one Down_B pays 1 if F >= K_B or
F < K_A and 2 if K_A <= F < K_B: never less than 1. So when

    ask(Up_A) + ask(Down_B) + fee(Up_A) + fee(Down_B) < 1 - margin      (ask(Down_B) = 1 - bid(Up_B))

the pair is a locked profit plus a free option. On every book change of a market in `timeframes`, every
other open market of the coin with the same `window_end_ms` and a different timeframe is checked (both
strikes known, both books' spread <= `max_spread`). Up_A's asks and Up_B's bids are walked together while
the marginal pair cost stays below 1 - margin, up to `size` pairs; below the larger minimum order size
there is no trade. Two IOC buys are sent at once, each limited to the worst level used.

Leg risk: the legs can fill differently (latency, other takers). The excess of one leg is held to
resolution (no hedge). A market pair waits until both orders are done, then may trade again `retry_ms`
later, at most `max_entries` times. Every finished pair is logged (`pair ...`) for the leg-risk analysis.
The strikes are the ones the strategy sees (`price_to_beat` derived from Chainlink, median 0.15 bp from the
official one); pairs closer than `min_kgap_bp` are skipped. Inputs come from `ctx` only.
"""

from __future__ import annotations

import math

from ..strategy import Ctx, OrderView, Strategy, register
from ..types import MarketInfo

DEPTH = 10


def fee_ps(price: float, rate: float, exponent: float) -> float:
    return rate * (price * (1.0 - price)) ** exponent


def walk(asks_a, bids_b, fee_a, fee_b, margin: float, cap: float):
    """Walk Up_A asks and Up_B bids (best first, (price, size)) while ask_a + (1 - bid_b) + fees < 1 - margin.
    `fee_a(p)` / `fee_b(p)` give the fee per share at token price p. Returns (pairs, worst ask_a, worst bid_b,
    cost of the first pair) or None when the best pair does not qualify."""
    i = j = 0
    ra = [s for _, s in asks_a]
    rb = [s for _, s in bids_b]
    n = 0.0
    worst_a = worst_b = first = None
    while i < len(asks_a) and j < len(bids_b) and n < cap - 1e-9:
        ua, db = asks_a[i][0], 1.0 - bids_b[j][0]
        c = ua + db + fee_a(ua) + fee_b(db)
        if c >= 1.0 - margin - 1e-12:
            break
        if first is None:
            first = c
        q = min(ra[i], rb[j], cap - n)
        n += q
        worst_a, worst_b = asks_a[i][0], bids_b[j][0]
        ra[i] -= q
        rb[j] -= q
        if ra[i] <= 1e-9:
            i += 1
        if rb[j] <= 1e-9:
            j += 1
    if first is None:
        return None
    return n, worst_a, worst_b, first


@register
class XtfArb(Strategy):
    name = "xtf-arb"
    family = "cross-timeframe"
    default_params = {
        "margin": 0.0,          # required locked edge per pair after fees
        "size": 50.0,           # max pairs per entry
        "max_spread": 0.10,     # both books (wide-book trap)
        "timeframes": ["5m", "15m", "1h", "4h"],
        "max_entries": 10,      # entries per market pair
        "retry_ms": 1000,
        "min_tau_s": 1.0,       # no new pair in the last second
        "min_kgap_bp": 0.0,     # skip pairs whose strikes are closer than this (strike-estimate error)
    }

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self.mk: dict[str, MarketInfo] = {}
        self.by_end: dict[int, set[str]] = {}
        self.inflight: dict[tuple[str, str], dict] = {}
        self.by_order: dict[int, tuple[str, str]] = {}
        self.entries: dict[tuple[str, str], int] = {}
        self.last_done: dict[tuple[str, str], int] = {}

    def on_market_open(self, ctx: Ctx, market: MarketInfo) -> None:
        if market.timeframe not in self.params["timeframes"]:
            return
        self.mk[market.slug] = market
        self.by_end.setdefault(market.window_end_ms, set()).add(market.slug)

    def on_book(self, ctx: Ctx, market: MarketInfo, book) -> None:
        if market.slug not in self.mk:
            return
        self.mk[market.slug] = market  # the latest public info (price_to_beat appears at the window start)
        if (market.window_end_ms - ctx.now) / 1000.0 < self.params["min_tau_s"]:
            return
        for other in list(self.by_end.get(market.window_end_ms, ())):
            o = self.mk.get(other)
            if o is None or other == market.slug or o.timeframe == market.timeframe:
                continue
            self._check(ctx, market, o)

    def on_order(self, ctx: Ctx, order: OrderView) -> None:
        key = self.by_order.get(order.id)
        if key is None:
            return
        st = self.inflight[key]
        st[order.id] = order
        if order.is_open or not all(o is not None and not o.is_open for o in (st[st["ida"]], st[st["idb"]])):
            return
        oa, ob = st[st["ida"]], st[st["idb"]]
        ctx.log("pair", key[0], key[1], round(oa.filled, 4), round(oa.notional, 6), round(oa.fee, 6),
                round(ob.filled, 4), round(ob.notional, 6), round(ob.fee, 6), st["n"], round(st["cost"], 6))
        for i in (st["ida"], st["idb"]):
            self.by_order.pop(i, None)
        del self.inflight[key]
        self.last_done[key] = ctx.now
        if oa.filled > 0 or ob.filled > 0:
            self.entries[key] = self.entries.get(key, 0) + 1

    def on_market_close(self, ctx: Ctx, market: MarketInfo) -> None:
        self.mk.pop(market.slug, None)
        s = self.by_end.get(market.window_end_ms)
        if s is not None:
            s.discard(market.slug)
            if not s:
                del self.by_end[market.window_end_ms]

    # ------------------------------------------------------------------------------------------
    def _check(self, ctx: Ctx, m1: MarketInfo, m2: MarketInfo) -> None:
        p = self.params
        if m1.price_to_beat is None or m2.price_to_beat is None:
            return
        a, b = (m1, m2) if m1.price_to_beat <= m2.price_to_beat else (m2, m1)
        if (b.price_to_beat / a.price_to_beat - 1.0) * 1e4 < p["min_kgap_bp"]:
            return
        key = (a.slug, b.slug)
        if key in self.inflight or self.entries.get(key, 0) >= p["max_entries"]:
            return
        if ctx.now - self.last_done.get(key, -10**12) < p["retry_ms"]:
            return
        ba, bb = ctx.book(a.slug, DEPTH), ctx.book(b.slug, DEPTH)
        if ba is None or bb is None:
            return
        for bk in (ba, bb):
            if bk.spread is None or bk.spread > p["max_spread"] + 1e-9:
                return
        # cheap pre-check on the tops before walking
        ua, db = ba.best_ask, 1.0 - bb.best_bid
        if ua + db >= 1.0 - p["margin"]:
            return
        res = walk(ba.asks, bb.bids, lambda x: fee_ps(x, a.fee_rate, a.fee_exponent),
                   lambda x: fee_ps(x, b.fee_rate, b.fee_exponent), p["margin"], p["size"])
        if res is None:
            return
        n, worst_a, worst_b, first = res
        n = math.floor(n * 100 + 1e-9) / 100
        if n < max(a.min_order_size, b.min_order_size):
            return
        ida = ctx.take(a.slug, "Up", "BUY", n, worst_a)
        idb = ctx.take(b.slug, "Down", "BUY", n, round(1.0 - worst_b, 6))
        self.inflight[key] = {"ida": ida, "idb": idb, ida: None, idb: None, "n": n, "cost": first}
        self.by_order[ida] = self.by_order[idb] = key


__all__ = ["XtfArb", "fee_ps", "walk"]

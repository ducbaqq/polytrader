"""Sanity strategies (family `sanity`): trivial behaviour whose results can be computed independently, so
`polydata.bt.validate` can check the simulator against them.

- `noop`: does nothing. P&L must be exactly 0.
- `buy-and-hold-up`: at market open, takes Up for `min_order_size` shares (limit 0.99) and holds to
  resolution.
- `always-cross`: at open, takes Up (5 shares, limit 0.99); once the buy is done, sells what it got
  with a take at limit 0.01. Loses the spread plus two fees.
- `join-best`: keeps a 5-share bid at the Up best bid and a 5-share ask at the Up best ask (selling Up
  if it holds 5, else buying Down at 1 - best ask). It cancels only to rejoin when the best moves away
  from its price (any difference is >= 1 tick) and stops 30 s before the window end; orders still
  resting then expire at the end.

  **One order per side per market at a time** (Up terms): a side gets a new order only after the
  previous one's terminal report (filled / cancelled / rejected). Exchange lifetimes of a side's
  orders therefore never overlap, which lets the validation re-derive each order's fills on its own.
"""

from __future__ import annotations

from ..strategy import Ctx, OrderView, Strategy, register
from ..types import MarketInfo


@register
class Noop(Strategy):
    name = "noop"
    family = "sanity"


@register
class BuyAndHoldUp(Strategy):
    name = "buy-and-hold-up"
    family = "sanity"
    default_params = {"limit": 0.99}

    def on_market_open(self, ctx: Ctx, market: MarketInfo) -> None:
        ctx.take(market.slug, "Up", "BUY", market.min_order_size, self.params["limit"])


@register
class AlwaysCross(Strategy):
    name = "always-cross"
    family = "sanity"
    default_params = {"size": 5.0, "buy_limit": 0.99, "sell_limit": 0.01}

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self.buys: set[int] = set()

    def on_market_open(self, ctx: Ctx, market: MarketInfo) -> None:
        p = self.params
        self.buys.add(ctx.take(market.slug, "Up", "BUY", p["size"], p["buy_limit"]))

    def on_order(self, ctx: Ctx, order: OrderView) -> None:
        if order.id in self.buys and not order.is_open:  # done: filled, or an IOC remainder cancelled
            self.buys.discard(order.id)
            if order.filled > 0:
                ctx.take(order.slug, "Up", "SELL", order.filled, self.params["sell_limit"])


@register
class JoinBest(Strategy):
    name = "join-best"
    family = "sanity"
    default_params = {"size": 5.0, "stop_s": 30}

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self.mkts: dict[str, MarketInfo] = {}
        # slug -> {"bid"/"ask": (order id, Up-terms px)}; a side is free once its order is done
        self.live: dict[str, dict[str, tuple[int, int]]] = {}
        self.cancelling: set[int] = set()
        self.side_of: dict[int, tuple[str, str]] = {}  # order id -> (slug, "bid"/"ask")

    def on_market_open(self, ctx: Ctx, market: MarketInfo) -> None:
        self.mkts[market.slug] = market
        self.live[market.slug] = {}
        self._quote(ctx, market)

    def on_book(self, ctx: Ctx, market: MarketInfo, book) -> None:
        if market.slug in self.mkts:
            self._quote(ctx, market)

    def on_order(self, ctx: Ctx, order: OrderView) -> None:
        if order.is_open:
            return
        key = self.side_of.pop(order.id, None)
        self.cancelling.discard(order.id)
        if key is None:
            return
        slug, side = key
        sides = self.live.get(slug)
        if sides is not None and sides.get(side, (None,))[0] == order.id:
            del sides[side]
        # rejoin at once after a fill or our own cancel; after a rejection wait for the next book change
        # (a local rejection is reported in the same ms: requoting here could loop)
        if order.status in ("filled", "cancelled") and slug in self.mkts:
            self._quote(ctx, self.mkts[slug])

    def on_market_close(self, ctx: Ctx, market: MarketInfo) -> None:
        self.mkts.pop(market.slug, None)
        self.live.pop(market.slug, None)

    def _quote(self, ctx: Ctx, m: MarketInfo) -> None:
        if m.window_end_ms - ctx.now < self.params["stop_s"] * 1000:
            return
        top = ctx.top(m.slug)
        if top is None:
            return
        sides, size = self.live[m.slug], self.params["size"]
        for side, target in (("bid", top.bid_px), ("ask", top.ask_px)):
            if not 0 < target < 1000:
                continue  # that side of the book is empty
            cur = sides.get(side)
            if cur is not None:
                oid, px = cur
                if px != target and oid not in self.cancelling:  # the best moved away: rejoin
                    self.cancelling.add(oid)
                    ctx.cancel(oid)
                continue
            if side == "bid":
                oid = ctx.place(m.slug, "Up", "BUY", target / 1000, size)
            elif ctx.position(m.slug)[0] >= size:
                oid = ctx.place(m.slug, "Up", "SELL", target / 1000, size)
            else:
                oid = ctx.place(m.slug, "Down", "BUY", (1000 - target) / 1000, size)
            sides[side] = (oid, target)
            self.side_of[oid] = (m.slug, side)


__all__ = ["AlwaysCross", "BuyAndHoldUp", "JoinBest", "Noop"]

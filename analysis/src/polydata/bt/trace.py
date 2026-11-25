"""Optional call tracing: every strategy handler call and every `ctx` call, in order, as rows of `DECISION_COLS`.

A paper run and its replay must make the same calls at the same `now` with the same arguments and get the
same answers; the first differing row names the event where they diverge. Tracing never changes what the
strategy sees or does: the wrappers record, then delegate (handlers) or record the answer (ctx calls).
`book` is recorded as "BookView"/"None" so tracing never loads levels the strategy did not ask for.
"""

from __future__ import annotations

import math

from .core import SimCtx

DECISION_COLS = ["n", "now", "call", "key", "token", "side", "price", "size", "ret"]
_NAN = math.nan


class TracedStrategy:
    """Wraps a Strategy: each handler appends a row (`call` = handler name, `key` = slug / sym / coin / timer
    name / order id), then delegates. The engine reads `Strategy.overrides` on the wrapped class."""

    __slots__ = ("_s", "_rows")

    def __init__(self, strategy, rows: list):
        self._s, self._rows = strategy, rows

    def _row(self, ctx, call: str, key) -> None:
        rows = self._rows
        rows.append((len(rows), ctx.now, call, key, None, None, _NAN, _NAN, None))

    def on_start(self, ctx) -> None:
        self._row(ctx, "on_start", None)
        self._s.on_start(ctx)

    def on_market_open(self, ctx, market) -> None:
        self._row(ctx, "on_market_open", market.slug)
        self._s.on_market_open(ctx, market)

    def on_book(self, ctx, market, book) -> None:
        self._row(ctx, "on_book", market.slug)
        self._s.on_book(ctx, market, book)

    def on_trade(self, ctx, market, trade) -> None:
        self._row(ctx, "on_trade", market.slug)
        self._s.on_trade(ctx, market, trade)

    def on_binance(self, ctx, sym, bbo) -> None:
        self._row(ctx, "on_binance", sym)
        self._s.on_binance(ctx, sym, bbo)

    def on_chainlink(self, ctx, coin, price) -> None:
        self._row(ctx, "on_chainlink", coin)
        self._s.on_chainlink(ctx, coin, price)

    def on_order(self, ctx, order) -> None:
        self._row(ctx, "on_order", str(order.id))
        self._s.on_order(ctx, order)

    def on_timer(self, ctx, name) -> None:
        self._row(ctx, "on_timer", name)
        self._s.on_timer(ctx, name)

    def on_market_close(self, ctx, market) -> None:
        self._row(ctx, "on_market_close", market.slug)
        self._s.on_market_close(ctx, market)


class TracedCtx(SimCtx):
    """`SimCtx` that records every call after it returns: `ret` is `repr` of the answer (`book`: "BookView" /
    "None"; `orders` / `markets`: the ids / slugs). token/side/price/size are filled for `place` and `take`."""

    __slots__ = ("_rows",)

    def __init__(self, core, rows: list):
        super().__init__(core)
        self._rows = rows

    def _rec(self, call: str, key, ret: str, token=None, side=None, price=_NAN, size=_NAN) -> None:
        rows = self._rows
        rows.append((len(rows), self.now, call, key, token, side, price, size, ret))

    def place(self, slug, token, side, price, size, post_only=True) -> int:
        oid = super().place(slug, token, side, price, size, post_only)
        self._rec("place", slug, repr(oid), token, side, float(price), float(size))
        return oid

    def take(self, slug, token, side, size, limit_price) -> int:
        oid = super().take(slug, token, side, size, limit_price)
        self._rec("take", slug, repr(oid), token, side, float(limit_price), float(size))
        return oid

    def cancel(self, order_id) -> None:
        super().cancel(order_id)
        self._rec("cancel", str(order_id), repr(None))

    def cancel_all(self, slug=None) -> None:
        super().cancel_all(slug)
        self._rec("cancel_all", slug, repr(None))

    def book(self, slug, depth=5):
        b = super().book(slug, depth)
        self._rec("book", slug, "None" if b is None else "BookView")
        return b

    def top(self, slug):
        v = super().top(slug)
        self._rec("top", slug, repr(v))
        return v

    def position(self, slug):
        v = super().position(slug)
        self._rec("position", slug, repr(v))
        return v

    def cash(self, slug=None):
        v = super().cash(slug)
        self._rec("cash", slug, repr(v))
        return v

    def orders(self, slug=None):
        v = super().orders(slug)
        self._rec("orders", slug, repr([o.id for o in v]))
        return v

    def set_timer(self, name, ms) -> None:
        super().set_timer(name, ms)
        self._rec("set_timer", str(name), repr(None))

    def markets(self):
        v = super().markets()
        self._rec("markets", None, repr([m.slug for m in v]))
        return v

    def fair_value(self, slug):
        v = super().fair_value(slug)
        self._rec("fair_value", slug, repr(v))
        return v

    def binance(self, sym):
        v = super().binance(sym)
        self._rec("binance", sym, repr(v))
        return v

    def chainlink(self, coin):
        v = super().chainlink(coin)
        self._rec("chainlink", coin, repr(v))
        return v

    def log(self, *args) -> None:
        super().log(*args)
        self._rec("log", None, repr(None))


__all__ = ["DECISION_COLS", "TracedCtx", "TracedStrategy"]

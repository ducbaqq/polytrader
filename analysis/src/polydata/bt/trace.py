"""Optional call tracing: every strategy handler call and every `ctx` call, in order, as rows of `DECISION_COLS`.

A paper run and its replay must make the same calls at the same `now` with the same arguments and get the
same answers; the first differing row names the event where they diverge. Tracing never changes what the
strategy sees or does: the wrappers record, then delegate (handlers) or record the answer (ctx calls).

Columns: `n` row number, `now` strategy clock, `call` handler or ctx method name, `key` slug / sym / coin / timer
name / order id, `token`/`side`/`price`/`size` the order arguments of `place`/`take` (`size` is the delay of
`set_timer`), `ret` the answer of a ctx call (`repr`, or "raise:<Exception>") or the input of a handler (`repr`),
`flag` `post_only` of `place`. `book` is recorded as "BookView"/"None" and `on_book` without its levels, so tracing
never loads levels the strategy did not ask for.
"""

from __future__ import annotations

import math

from .core import SimCtx

DECISION_COLS = ["n", "now", "call", "key", "token", "side", "price", "size", "ret", "flag"]
_NAN = math.nan


def _num(x) -> float:
    try:
        return float(x)
    except (TypeError, ValueError):
        return _NAN


class TracedStrategy:
    """Wraps a Strategy: each handler appends a row (`call` = handler name, `key` = slug / sym / coin / timer
    name / order id, `ret` = the handler's input where the key doesn't identify it), then delegates. The engine
    reads `Strategy.overrides` on the wrapped class; other attributes (`name`, `params`, ...) are forwarded."""

    __slots__ = ("_s", "_rows")

    def __init__(self, strategy, rows: list):
        self._s, self._rows = strategy, rows

    def __getattr__(self, name):
        return getattr(self._s, name)

    def _row(self, ctx, call: str, key, ret=None) -> None:
        rows = self._rows
        rows.append((len(rows), ctx.now, call, key, None, None, _NAN, _NAN, ret, None))

    def on_start(self, ctx) -> None:
        self._row(ctx, "on_start", None)
        self._s.on_start(ctx)

    def on_market_open(self, ctx, market) -> None:
        self._row(ctx, "on_market_open", market.slug)
        self._s.on_market_open(ctx, market)

    def on_book(self, ctx, market, book) -> None:
        self._row(ctx, "on_book", market.slug)  # the levels are lazy: recording them would load them
        self._s.on_book(ctx, market, book)

    def on_trade(self, ctx, market, trade) -> None:
        self._row(ctx, "on_trade", market.slug, repr(trade))
        self._s.on_trade(ctx, market, trade)

    def on_binance(self, ctx, sym, bbo) -> None:
        self._row(ctx, "on_binance", sym, repr(bbo))
        self._s.on_binance(ctx, sym, bbo)

    def on_chainlink(self, ctx, coin, price) -> None:
        self._row(ctx, "on_chainlink", coin, repr(price))
        self._s.on_chainlink(ctx, coin, price)

    def on_order(self, ctx, order) -> None:
        self._row(ctx, "on_order", str(order.id),
                  repr((order.event, order.status, order.filled, order.remaining, order.reason, order.last_fill)))
        self._s.on_order(ctx, order)

    def on_timer(self, ctx, name) -> None:
        self._row(ctx, "on_timer", name)
        self._s.on_timer(ctx, name)

    def on_market_close(self, ctx, market) -> None:
        self._row(ctx, "on_market_close", market.slug)
        self._s.on_market_close(ctx, market)


class TracedCtx(SimCtx):
    """`SimCtx` that records every call when it returns or raises: `ret` is `repr` of the answer (`book`:
    "BookView"/"None"; `orders`/`markets`: the ids/slugs), or "raise:<type>" if the call raised."""

    __slots__ = ("_rows",)

    def __init__(self, core, rows: list):
        super().__init__(core)
        self._rows = rows

    def _call(self, call: str, key, fn, fmt=repr, token=None, side=None, price=_NAN, size=_NAN, flag=None):
        ret = None
        try:
            v = fn()
            ret = fmt(v)
            return v
        except BaseException as e:
            ret = "raise:" + type(e).__name__
            raise
        finally:
            rows = self._rows
            rows.append((len(rows), self.now, call, key, token, side, price, size, ret, flag))

    def place(self, slug, token, side, price, size, post_only=True) -> int:
        return self._call("place", slug, lambda: SimCtx.place(self, slug, token, side, price, size, post_only),
                          token=token, side=side, price=_num(price), size=_num(size), flag=bool(post_only))

    def take(self, slug, token, side, size, limit_price) -> int:
        return self._call("take", slug, lambda: SimCtx.take(self, slug, token, side, size, limit_price),
                          token=token, side=side, price=_num(limit_price), size=_num(size))

    def cancel(self, order_id) -> None:
        return self._call("cancel", str(order_id), lambda: SimCtx.cancel(self, order_id))

    def cancel_all(self, slug=None) -> None:
        return self._call("cancel_all", slug, lambda: SimCtx.cancel_all(self, slug))

    def book(self, slug, depth=5):
        return self._call("book", slug, lambda: SimCtx.book(self, slug, depth),
                          lambda b: "None" if b is None else "BookView")

    def top(self, slug):
        return self._call("top", slug, lambda: SimCtx.top(self, slug))

    def position(self, slug):
        return self._call("position", slug, lambda: SimCtx.position(self, slug))

    def cash(self, slug=None):
        return self._call("cash", slug, lambda: SimCtx.cash(self, slug))

    def orders(self, slug=None):
        return self._call("orders", slug, lambda: SimCtx.orders(self, slug), lambda v: repr([o.id for o in v]))

    def set_timer(self, name, ms) -> None:
        return self._call("set_timer", str(name), lambda: SimCtx.set_timer(self, name, ms), size=_num(ms))

    def markets(self):
        return self._call("markets", None, lambda: SimCtx.markets(self), lambda v: repr([m.slug for m in v]))

    def fair_value(self, slug):
        return self._call("fair_value", slug, lambda: SimCtx.fair_value(self, slug))

    def binance(self, sym):
        return self._call("binance", sym, lambda: SimCtx.binance(self, sym))

    def chainlink(self, coin):
        return self._call("chainlink", coin, lambda: SimCtx.chainlink(self, coin))

    def log(self, *args) -> None:
        return self._call("log", None, lambda: SimCtx.log(self, *args))


__all__ = ["DECISION_COLS", "TracedCtx", "TracedStrategy"]

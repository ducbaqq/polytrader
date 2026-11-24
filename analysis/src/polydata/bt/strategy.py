"""The strategy API: the `Strategy` base class, the `Ctx` protocol and the registry.

The same Strategy class runs in the backtester (`engine.SimCtx`) and, later, in the live runner, so
this module imports nothing from the engine. Prices at this boundary are floats in token terms
(0.48), sizes are shares. `book()` and `top()` are in Up terms; `BookView.for_token("Down")` mirrors.

Everything a strategy sees through `ctx` is what it would have received by `ctx.now`: market data at
its `recv_ms`, order events `fill_report_latency_ms` after the exchange produced them, and the
market's `outcome` / `resolution_recv_ms` are always None.
"""

from __future__ import annotations

import importlib
import re
from collections.abc import Callable
from dataclasses import dataclass
from typing import Literal, Protocol

from .types import Bbo, ChainlinkTick, MarketInfo, Side, Token, Top

Level = tuple[float, float]  # (price, size)


class BookView:
    """Book levels, best first. Built from lists, or lazily from `loader() -> (bids, asks)` (the
    engine only reads the levels when the strategy asks)."""

    __slots__ = ("_bids", "_asks", "_loader")

    def __init__(self, bids: list[Level] | None = None, asks: list[Level] | None = None,
                 loader: Callable[[], tuple[list[Level], list[Level]]] | None = None):
        if loader is None and (bids is None or asks is None):
            raise ValueError("BookView needs bids and asks, or a loader")
        self._bids, self._asks, self._loader = bids, asks, loader

    def _load(self) -> None:
        if self._bids is None:
            self._bids, self._asks = self._loader()

    @property
    def bids(self) -> list[Level]:
        self._load()
        return self._bids

    @property
    def asks(self) -> list[Level]:
        self._load()
        return self._asks

    @property
    def best_bid(self) -> float | None:
        b = self.bids
        return b[0][0] if b else None

    @property
    def best_ask(self) -> float | None:
        a = self.asks
        return a[0][0] if a else None

    @property
    def mid(self) -> float | None:
        b, a = self.best_bid, self.best_ask
        return None if b is None or a is None else (b + a) / 2

    @property
    def spread(self) -> float | None:
        b, a = self.best_bid, self.best_ask
        return None if b is None or a is None else round(a - b, 6)

    def for_token(self, token: Token) -> BookView:
        """This book in `token` terms: Down bids are 1 - Up asks, Down asks 1 - Up bids."""
        if token == "Up":
            return self
        if token != "Down":
            raise ValueError(f"unknown token {token!r}")
        return BookView([(round(1 - p, 6), s) for p, s in self.asks], [(round(1 - p, 6), s) for p, s in self.bids])

    def __repr__(self) -> str:
        return f"BookView(bids={self.bids[:3]}, asks={self.asks[:3]})"


@dataclass
class FillInfo:
    ts_ms: int  # exchange time of the fill
    price: float  # token terms
    size: float
    fee: float
    liquidity: Literal["maker", "taker"]


@dataclass
class OrderView:
    """An order as the strategy knows it (updated only when an order event is delivered)."""
    id: int
    slug: str
    token: Token
    side: Side
    price: float  # limit, token terms
    size: float
    kind: Literal["limit", "take"]
    post_only: bool
    placed_ms: int
    status: Literal["pending", "open", "filled", "cancelled", "expired", "rejected"] = "pending"
    filled: float = 0.0
    notional: float = 0.0  # sum of fill price x size
    fee: float = 0.0
    reason: str = ""
    event: str = "placed"  # the last delivered event: accepted / rejected / fill / cancelled / cancel_rejected / expired
    last_fill: FillInfo | None = None  # the fill this event reports (event == "fill")

    @property
    def remaining(self) -> float:
        return self.size - self.filled

    @property
    def avg_price(self) -> float | None:
        return self.notional / self.filled if self.filled else None

    @property
    def is_open(self) -> bool:
        return self.status in ("pending", "open")


class Ctx(Protocol):
    """What a strategy can see and do. `now` is the strategy clock (ms)."""

    now: int

    def place(self, slug: str, token: Token, side: Side, price: float, size: float, post_only: bool = True) -> int: ...
    def take(self, slug: str, token: Token, side: Side, size: float, limit_price: float) -> int: ...
    def cancel(self, order_id: int) -> None: ...
    def cancel_all(self, slug: str | None = None) -> None: ...
    def book(self, slug: str, depth: int = 5) -> BookView | None: ...
    def top(self, slug: str) -> Top | None: ...
    def position(self, slug: str) -> tuple[float, float]: ...
    def cash(self, slug: str | None = None) -> float: ...
    def orders(self, slug: str | None = None) -> list[OrderView]: ...
    def set_timer(self, name: str, ms: int) -> None: ...
    def markets(self) -> list[MarketInfo]: ...
    def fair_value(self, slug: str) -> float | None: ...
    def binance(self, sym: str) -> Bbo | None: ...
    def chainlink(self, coin: str) -> ChainlinkTick | None: ...
    def log(self, *args) -> None: ...


class Strategy:
    """Base class: override the handlers you need (all default to no-ops). Handlers that are not
    overridden are not called, which the engine uses to skip whole streams."""

    name: str = ""  # kebab-case registry name
    family: str = ""  # from docs/strategies/README.md
    default_params: dict = {}

    def __init__(self, params: dict | None = None):
        self.params = {**self.default_params, **(params or {})}

    def on_start(self, ctx: Ctx) -> None: ...
    def on_market_open(self, ctx: Ctx, market: MarketInfo) -> None: ...  # a market enters the tradable set
    def on_book(self, ctx: Ctx, market: MarketInfo, book: BookView) -> None: ...  # the top of book changed
    def on_trade(self, ctx: Ctx, market: MarketInfo, trade) -> None: ...  # a public trade (types.Trade)
    def on_binance(self, ctx: Ctx, sym: str, bbo: Bbo) -> None: ...  # Binance futures top, 10 ms steps
    def on_chainlink(self, ctx: Ctx, coin: str, price: float) -> None: ...
    def on_order(self, ctx: Ctx, order: OrderView) -> None: ...  # an order event, after the report delay
    def on_timer(self, ctx: Ctx, name: str) -> None: ...
    def on_market_close(self, ctx: Ctx, market: MarketInfo) -> None: ...  # window ended

    @classmethod
    def overrides(cls, handler: str) -> bool:
        return getattr(cls, handler) is not getattr(Strategy, handler)


REGISTRY: dict[str, type[Strategy]] = {}
_NAME = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")


def register(cls: type[Strategy]) -> type[Strategy]:
    """Class decorator: add a strategy to REGISTRY under its kebab-case `name`."""
    if not _NAME.match(cls.name or ""):
        raise ValueError(f"strategy name must be kebab-case, got {cls.name!r}")
    old = REGISTRY.get(cls.name)
    if old is not None and (old.__module__, old.__qualname__) != (cls.__module__, cls.__qualname__):
        raise ValueError(f"strategy {cls.name!r} is already registered by {old.__module__}.{old.__qualname__}")
    REGISTRY[cls.name] = cls
    return cls


def get_strategy(name: str) -> type[Strategy]:
    """Look a strategy up by name, importing the bundled `polydata.bt.strategies` package first."""
    if name not in REGISTRY:
        try:
            importlib.import_module("polydata.bt.strategies")
        except ModuleNotFoundError as e:
            if e.name != "polydata.bt.strategies":
                raise
    try:
        return REGISTRY[name]
    except KeyError:
        raise KeyError(f"unknown strategy {name!r}; registered: {sorted(REGISTRY)}") from None


__all__ = ["BookView", "Ctx", "FillInfo", "OrderView", "REGISTRY", "Strategy", "get_strategy", "register"]

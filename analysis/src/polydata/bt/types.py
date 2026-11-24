"""Shared backtester types and the integer price / Up-terms conventions.

Prices inside the simulator are ints in units of 1/1000 (`px`), so mirroring a Down price into
Up terms (`1000 - px`) is exact. Each market has one combined book, kept in Up terms: a Down BUY
at q is an Up SELL at 1 - q.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Literal

Token = Literal["Up", "Down"]
Side = Literal["BUY", "SELL"]

PX_SCALE = 1000
NO_BID_PX = 0  # Top.bid_px when the bid side is empty
NO_ASK_PX = PX_SCALE  # Top.ask_px when the ask side is empty


def to_px(p: float) -> int:
    return int(round(p * PX_SCALE))


def from_px(px: int) -> float:
    return px / PX_SCALE


def _flip(side: Side) -> Side:
    return "SELL" if side == "BUY" else "BUY"


def to_up(token: Token, side: Side, px: int) -> tuple[Side, int]:
    """(side, px) on `token` -> (side, px) in Up terms. Up is unchanged; Down flips both."""
    if token == "Up":
        return side, int(px)
    if token == "Down":
        return _flip(side), PX_SCALE - int(px)
    raise ValueError(f"unknown token {token!r}")


def from_up(token: Token, up_side: Side, up_px: int) -> tuple[Side, int]:
    """Inverse of `to_up` (the mapping is its own inverse)."""
    return to_up(token, up_side, up_px)


@dataclass(frozen=True)
class MarketInfo:
    slug: str
    coin: str
    timeframe: str
    series: str
    condition_id: str
    window_start_ms: int
    window_end_ms: int
    price_to_beat: float | None
    fee_rate: float
    fee_exponent: float
    tick_px: int  # initial tick in px units: 10 (0.01) or 1 (0.001)
    min_order_size: float
    outcome: str | None
    resolution_recv_ms: int | None


@dataclass(frozen=True)
class Top:
    """Top of the combined book in Up terms. A missing side is bid_px = 0 / ask_px = 1000, size 0."""
    recv_ms: int
    ts_ms: int
    bid_px: int
    bid_size: float
    ask_px: int
    ask_size: float


@dataclass(frozen=True)
class Trade:
    """A public trade. `side` is the taker side on `token`; `up_side`/`up_px` are the taker's side and
    price in Up terms (an Up-terms taker BUY consumes asks)."""
    recv_ms: int
    ts_ms: int
    token: Token
    side: Side
    price: float
    size: float
    up_side: Side
    up_px: int


@dataclass(frozen=True)
class Bbo:
    recv_ms: int
    sym: str
    bid: float
    ask: float
    bid_qty: float
    ask_qty: float


@dataclass(frozen=True)
class ChainlinkTick:
    recv_ms: int
    ts_ms: int
    coin: str
    price: float

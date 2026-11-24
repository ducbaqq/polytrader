"""The replay engine: feeds recorded data to a strategy on its own clock and routes its orders
through the simulated exchange.

**What is replayed.** Each selected day contributes `[max(day start, REPLAY_START_MS), day end)`,
cut short where the recording ends; adjacent days merge into one range. A market is replayed only
if its whole window lies inside one range (so a market running into a sealed or unselected day is
never touched) and it isn't voided (`gaps.voided_markets`: data gaps, or no outcome).

**Two clocks.** The strategy sees data at `recv_ms`; the exchange matches on `ts_ms`.

- Data events (Polymarket tops and trades of open markets, Binance top of book bundled to the last
  update per `bn_step_ms` bucket and delivered at the bucket's last ms, Chainlink deduplicated on
  `ts_ms`) form one sorted array per hour: `np.lexsort` by recv_ms, then stream (Polymarket <
  Chainlink < Binance), then kind, market and row order.
- Internal events (order reports, timers, market close, settlement, redemption, equity samples) sit
  in a heap; at an equal ms they come after data events.
- Before anything is delivered at time T, the exchanges with live orders are advanced up to
  `T - fill_report_latency_ms`, one exchange event time at a time, delivering any report that falls
  in between first. So each exchange event reaches the strategy (as `on_order`) exactly
  `fill_report_latency_ms` after it happened, and an order placed in reaction (arriving
  `order_latency_ms + U(0, latency_jitter_ms)` later, RNG seeded from `seed` and the coin) never
  lands in time the exchange has already processed. (Advancing straight to T would find fills
  whose report time had already passed.)
- Fills hit the portfolio when they are reported, so `ctx.cash` / `ctx.position` never run ahead of
  what the strategy knows; the cash for an order is locked when it is placed.

**Markets.** A market opens (`on_market_open`; orders accepted from then on) once its first
`pm_book` snapshot inside the replay range has been received: before that its book is untrusted.
Markets are subscribed ~2 windows before they start and are tradable then. Its `price_to_beat`
is hidden until the strategy could know it (see `_schedule_ptb`). At `window_end_ms` the exchange is
closed (resting orders expire, orders in flight are cancelled as "never arrived"), then
`on_market_close`; the market's data is freed. Settlement happens once the resolution was received
and every report is in; the payout is credited `redeem_delay_s` after the resolution.

**Memory.** Markets are loaded in chunks (by `window_start` hour) just before they are needed and
freed at their close. Binance and Chainlink are loaded one hour at a time.
"""

from __future__ import annotations

import heapq
import logging
import math
import multiprocessing
import os
import tempfile
import time
import zlib
from collections.abc import Callable
from dataclasses import dataclass, field, replace
from datetime import date, datetime, timezone
from typing import NamedTuple

import numpy as np
import pandas as pd

from .config import RunConfig
from .exchange import EPS, Exchange, ExchangeConfig, ExEvent, Order
from .fairvalue import K_BEST
from .fairvalue import fair_value as _fair_value
from .gaps import gap_table, voided_markets
from .marketdata import LOOKBACK_WINDOWS, SIDES, TOKENS, MarketData, load_coin
from .portfolio import Portfolio
from .splits import REPLAY_START_MS, day_kind
from .strategy import BookView, FillInfo, OrderView, Strategy, get_strategy
from .types import NO_ASK_PX, NO_BID_PX, PX_SCALE, Bbo, ChainlinkTick, MarketInfo, Top, Trade

log = logging.getLogger(__name__)

DAY_MS, HOUR_MS = 86_400_000, 3_600_000
INF = float("inf")
SIG_WINDOW_S = 900  # sig15: 15 min of 1 s Binance returns
DATA_END_SLACK_MS = 60_000  # a day whose data reaches this close to midnight counts as complete

# Data event kinds, in delivery order at an equal recv_ms. The first three are Polymarket.
K_OPEN, K_TOP, K_TRADE, K_CL, K_BN = 0, 1, 2, 3, 4
_RANK = np.array([0, 0, 0, 1, 2], np.int8)  # stream rank: Polymarket 0, Chainlink 1, Binance 2
# Internal events at an equal ms
P_REPORT, P_CLOSE, P_TIMER, P_SETTLE, P_REDEEM, P_EQUITY = range(6)

ORDER_COLS = ["ms", "report_ms", "order_id", "slug", "event", "token", "side", "kind", "post_only", "px", "size",
              "reason", "queue_ahead"]
FILL_COLS = ["slug", "coin", "order_id", "ts_ms", "report_ms", "token", "side", "kind", "px", "size", "fee",
             "liquidity", "queue_ahead_at_fill", "trade_ts_ms", "mid_at_fill", "mid_10s", "settle_value",
             "bn_move_1s"]
# cash = total unlocked cash, realised profits included (not capped at the budget like ctx.cash)
EQUITY_COLS = ["ms", "cash", "locked", "pending", "mtm", "equity"]
MARKET_COLS = ["slug", "coin", "timeframe", "window_start_ms", "window_end_ms", "voided", "reason", "pnl",
               "settle_ms", "outcome"]


def sym_of(coin: str) -> str:
    return f"{coin.upper()}USDT"


def _day_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def _date(ms: int) -> date:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).date()


def _up_mid(top: Top) -> float:
    if top.bid_px == NO_BID_PX and top.ask_px == NO_ASK_PX:
        return math.nan
    return (top.bid_px + top.ask_px) / (2 * PX_SCALE)


def default_con_factory():
    """A `polydata.connect()` connection (sealed days hidden). The engine caps its memory."""
    from ..views import connect

    return connect()


# ---------------------------------------------------------------------------------------- results

@dataclass
class CoinResult:
    orders: pd.DataFrame  # one row per order event (ORDER_COLS)
    fills: pd.DataFrame  # one row per fill (FILL_COLS)
    equity: pd.DataFrame  # EQUITY_COLS, every 1 s while positions exist (+ first and last)
    markets: pd.DataFrame  # MARKET_COLS: every market considered, voided ones with a reason and NaN P&L
    log: list[str]
    stats: dict = field(default_factory=dict)


@dataclass
class RunResult:
    cfg: RunConfig
    coins: dict[str, CoinResult]  # budget key (coin, or "*" in global mode) -> result
    elapsed_s: float = 0.0

    def _cat(self, name: str) -> pd.DataFrame:
        parts = [getattr(r, name).assign(budget=k) for k, r in self.coins.items()]
        return pd.concat(parts, ignore_index=True) if parts else pd.DataFrame()

    @property
    def orders(self) -> pd.DataFrame:
        return self._cat("orders")

    @property
    def fills(self) -> pd.DataFrame:
        return self._cat("fills")

    @property
    def equity(self) -> pd.DataFrame:
        return self._cat("equity")

    @property
    def markets(self) -> pd.DataFrame:
        return self._cat("markets")

    @property
    def log(self) -> list[str]:
        return [line for r in self.coins.values() for line in r.log]


# ---------------------------------------------------------------------------------------- state

class _Bn(NamedTuple):
    """Bundled Binance rows of one symbol for one hour, in recv order (recv = bucket's last ms)."""
    recv: np.ndarray
    bid: np.ndarray
    ask: np.ndarray
    bid_qty: np.ndarray
    ask_qty: np.ndarray


_EMPTY_BN = _Bn(*(np.empty(0, t) for t in (np.int64, float, float, float, float)))


class _Grid:
    """1 s Binance mids (last update received in each second, forward-filled) and the running sum of
    squared log returns, for sig15. Restarted at each replay range."""

    def __init__(self):
        self.sec0: int | None = None
        self.mids = np.empty(0)
        self.csum = np.empty(0)  # csum[j] = sum of r^2 over returns 1..j (r_j = ln(mid_j / mid_{j-1}))

    def append(self, sec_from: int, sec_to: int, bn: _Bn) -> None:
        """Add the seconds [sec_from, sec_to) from this hour's bundles."""
        n = sec_to - sec_from
        vals = np.full(n, np.nan)
        if len(bn.recv):
            sec = bn.recv // 1000 - sec_from
            last = np.r_[sec[1:] != sec[:-1], True]
            vals[sec[last]] = ((bn.bid + bn.ask) / 2)[last]
        if self.sec0 is None:
            ok = np.flatnonzero(~np.isnan(vals))
            if not len(ok):
                return
            self.sec0, vals = sec_from + int(ok[0]), vals[ok[0]:]
            prev = vals[0]
        else:
            if self.sec0 + len(self.mids) != sec_from:
                raise RuntimeError("Binance grid is not contiguous")
            prev = self.mids[-1]
        idx = np.where(~np.isnan(vals), np.arange(len(vals)), -1)
        idx = np.maximum.accumulate(idx)
        filled = np.where(idx >= 0, vals[np.maximum(idx, 0)], prev)
        r2 = np.log(filled / np.r_[prev, filled[:-1]]) ** 2
        base = self.csum[-1] if len(self.csum) else 0.0
        if not len(self.mids):
            r2[0] = 0.0  # the first second has no return
        self.mids = np.r_[self.mids, filled]
        self.csum = np.r_[self.csum, base + np.cumsum(r2)]

    def sig15(self, t_ms: int) -> float | None:
        """sqrt(mean r^2) over the 900 returns ending at the last complete second before t_ms."""
        if self.sec0 is None:
            return None
        j = t_ms // 1000 - 1 - self.sec0
        if j < SIG_WINDOW_S or j >= len(self.csum):
            return None
        return math.sqrt(max(self.csum[j] - self.csum[j - SIG_WINDOW_S], 0.0) / SIG_WINDOW_S)


class _Mkt:
    __slots__ = ("info", "pub", "mi", "md", "ex", "rs", "open_ms", "opened", "closed", "last_mid", "raw_fills",
                 "settle_ms", "ptb_sched")

    def __init__(self, info: MarketInfo, mi: int, md: MarketData, ex: Exchange, rs: int):
        self.info = info
        # What the strategy may see: no outcome, and price_to_beat only once computed from received
        # Chainlink ticks (see _Replay._schedule_ptb).
        self.pub = replace(info, outcome=None, resolution_recv_ms=None, price_to_beat=None)
        self.ptb_sched = False
        self.mi, self.md, self.ex, self.rs = mi, md, ex, rs
        self.open_ms: int | None = None
        self.opened = self.closed = False
        self.last_mid = math.nan
        self.raw_fills: list[tuple] = []
        self.settle_ms: int | None = None

    def reveal_ptb(self, value: float) -> None:
        self.pub = replace(self.pub, price_to_beat=value)


@dataclass
class _Chunk:
    need_ms: int
    rs: int  # start of the replay range it belongs to
    rows: list  # market rows (namedtuples from the markets query)
    loaded: bool = False


# ---------------------------------------------------------------------------------------- context

class SimCtx:
    """`strategy.Ctx` for the backtester. Every query answers as of `now` on the strategy clock."""

    __slots__ = ("now", "_r")

    def __init__(self, replay: _Replay):
        self._r = replay
        self.now = 0

    def place(self, slug, token, side, price, size, post_only=True) -> int:
        return self._r.submit(slug, token, side, int(round(float(price) * PX_SCALE)), float(size), "limit",
                              bool(post_only))

    def take(self, slug, token, side, size, limit_price) -> int:
        return self._r.submit(slug, token, side, int(round(float(limit_price) * PX_SCALE)), float(size), "take",
                              False)

    def cancel(self, order_id) -> None:
        self._r.cancel(int(order_id))

    def cancel_all(self, slug=None) -> None:
        r = self._r
        for v in list(r.views.values()):
            if v.is_open and (slug is None or v.slug == slug) and v.id not in r.cancel_sent:
                r.cancel(v.id)

    def book(self, slug, depth=5) -> BookView | None:
        m = self._r.open.get(slug)
        if m is None:
            return None
        md, t = m.md, self.now
        return BookView(loader=lambda: _levels(md, t, depth))

    def top(self, slug) -> Top | None:
        m = self._r.open.get(slug)
        return None if m is None else m.md.top_at(self.now, "recv")

    def position(self, slug) -> tuple[float, float]:
        return self._r.pf.positions.get(slug, (0.0, 0.0))

    def cash(self, slug=None) -> float:
        r = self._r
        if slug is None:
            return sum(r.pf.free_cash(k) for k in r.pf.cash)
        return r.pf.free_cash(r.pf.key(slug))

    def orders(self, slug=None) -> list[OrderView]:
        return [v for v in self._r.views.values() if v.is_open and (slug is None or v.slug == slug)]

    def set_timer(self, name, ms) -> None:
        self._r.set_timer(str(name), int(ms))

    def markets(self) -> list[MarketInfo]:
        return [m.pub for m in self._r.open.values()]

    def fair_value(self, slug) -> float | None:
        return self._r.fair_value(slug)

    def binance(self, sym) -> Bbo | None:
        return self._r.bbo(sym, self.now)

    def chainlink(self, coin) -> ChainlinkTick | None:
        return self._r.cl_tick(coin, self.now)

    def log(self, *args) -> None:
        self._r.log.append(f"{self.now} " + " ".join(str(a) for a in args))


def _levels(md: MarketData, t: int, depth: int):
    bids, asks = md.book_at(t, "recv", depth)
    return [(p / PX_SCALE, s) for p, s in bids], [(p / PX_SCALE, s) for p, s in asks]


# ---------------------------------------------------------------------------------------- replay

class _Replay:
    def __init__(self, cfg: RunConfig, coins: list[str], budgets: dict[str, float], strategy: Strategy,
                 loader: Callable, rec_events: pd.DataFrame | None, clip: tuple[int, int] | None = None):
        self.cfg, self.coins, self.strategy, self.loader, self.rec_events = cfg, list(coins), strategy, loader, rec_events
        self.clip = clip
        self.R = int(cfg.fill_report_latency_ms)
        self.lat, self.jitter = int(cfg.order_latency_ms), int(cfg.latency_jitter_ms)
        self.rng = np.random.default_rng([int(cfg.seed), zlib.crc32(",".join(self.coins).encode())])
        self.exc = ExchangeConfig(cancel_model=cfg.cancel_model, impact_reset_ms=cfg.impact_reset_ms)
        self.pf = Portfolio(budgets, redeem_delay_ms=cfg.redeem_delay_s * 1000)
        self.ctx = SimCtx(self)
        self.heap: list[tuple] = []
        self._seq = 0
        self.next_ex = INF  # lower bound on the next exchange event over the live markets
        self.live: dict[int, _Mkt] = {}  # markets whose exchange has something pending
        self.mkts: list[_Mkt] = []  # by mi
        self.by_slug: dict[str, _Mkt] = {}
        self.loaded: list[_Mkt] = []  # loaded and not closed
        self.open: dict[str, _Mkt] = {}  # opened and not closed (the strategy's tradable set)
        # Orders the strategy still sees as open (pending/open); dropped at their terminal report.
        self.orders: dict[int, Order] = {}
        self.views: dict[int, OrderView] = {}
        self.cancel_sent: set[int] = set()
        self._oid = 0
        self.timer_ver: dict[str, int] = {}
        self.ending = False
        self.eq_sched = False
        self.order_rows: list[tuple] = []
        self.fill_rows: list[tuple] = []
        self.equity_rows: list[tuple] = []
        self.market_rows: dict[str, dict] = {}
        self.log: list[str] = []
        self.syms = [sym_of(c) for c in self.coins]
        self.bn_cur: dict[str, _Bn] = {}
        self.bn_prev: dict[str, _Bn] = {}
        self.grid: dict[str, _Grid] = {}
        self.cl: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]] = {}
        self.stats = {"events": 0, "segments": 0, "load_s": 0.0, "data_s": 0.0, "markets": 0}
        self.want = {h: type(strategy).overrides(h) for h in ("on_book", "on_trade", "on_binance", "on_chainlink")}

    # ------------------------------------------------------------------------------- scheduling
    def push(self, t: int, prio: int, payload: tuple) -> None:
        self._seq += 1
        heapq.heappush(self.heap, (int(t), prio, self._seq, payload))

    def _advance(self, T: int) -> None:
        nxt = INF
        for mi in list(self.live):
            m = self.live[mi]
            ex = m.ex
            if ex.next_event_ms() <= T:
                for ev in ex.advance(T):
                    self._on_ex(m, ev)
            n = ex.next_event_ms()
            if n == INF:
                del self.live[mi]
            elif n < nxt:
                nxt = n
        self.next_ex = nxt

    def _drain(self, limit: float) -> None:
        """Deliver internal events with time < limit (advancing the exchanges before each)."""
        heap, R = self.heap, self.R
        while heap:
            h = heap[0][0]
            if h >= limit:
                return
            if h - R >= self.next_ex:
                self._advance(self.next_ex)  # one exchange time at a time: its reports may come first
                continue
            self._handle(heapq.heappop(heap))

    def _catch_up(self, t: int) -> None:
        """Everything that must happen before a data event at t."""
        heap, R = self.heap, self.R
        while True:
            if heap and heap[0][0] < t:
                self._drain(t)
            elif t - R >= self.next_ex:
                self._advance(self.next_ex)
            else:
                return

    def _mark_live(self, m: _Mkt) -> None:
        self.live[m.mi] = m
        n = m.ex.next_event_ms()
        if n < self.next_ex:
            self.next_ex = n

    # ------------------------------------------------------------------------------- orders
    def _arrival(self) -> int:
        j = int(self.rng.integers(0, self.jitter + 1)) if self.jitter > 0 else 0
        return self.ctx.now + self.lat + j

    def submit(self, slug: str, token: str, side: str, px: int, size: float, kind: str, post_only: bool) -> int:
        if token not in ("Up", "Down") or side not in ("BUY", "SELL"):
            raise ValueError(f"bad token/side {token!r}/{side!r}")
        now = self.ctx.now
        self._oid += 1
        oid = self._oid
        o = Order(id=oid, slug=slug, token=token, side=side, px=px, size=size, kind=kind, post_only=post_only,
                  placed_ms=now, arrival_ms=now)
        self.orders[oid] = o
        self.views[oid] = OrderView(oid, slug, token, side, px / PX_SCALE, size, kind, post_only, now)
        self.order_rows.append((now, now, oid, slug, "placed", token, side, kind, post_only, px / PX_SCALE, size, "",
                                math.nan))
        m = self.by_slug.get(slug)
        reason = ""
        if m is None or m.closed or not m.opened:
            reason = "market not open"
        elif side == "BUY" and not self.pf.reserve_buy(o):
            reason = "insufficient cash"
        elif side == "SELL" and not self.pf.reserve_sell(o):
            reason = "insufficient shares"
        if reason:
            o.status, o.reason = "rejected", reason
            self.order_rows.append((now, now, oid, slug, "rejected", token, side, kind, post_only, px / PX_SCALE, size,
                                    reason, math.nan))
            self.push(now, P_REPORT, ("lrej", self.views.pop(oid), reason))
            del self.orders[oid]
            return oid
        o.arrival_ms = self._arrival()
        m.ex.submit(o)
        self._mark_live(m)
        return oid

    def cancel(self, oid: int) -> None:
        o = self.orders.get(oid)
        if o is None:
            return  # unknown, or done as far as the strategy knows
        m = self.by_slug.get(o.slug)
        if m is None or m.closed or m.ex is None:  # its expiry report may still be in flight
            now = self.ctx.now
            self.order_rows.append((now, now, oid, o.slug, "cancel_rejected", o.token, o.side, o.kind, o.post_only,
                                    o.px / PX_SCALE, o.size, "market closed", math.nan))
            self.push(now, P_REPORT, ("lcrej", oid))
            return
        self.cancel_sent.add(oid)
        # a cancel never reaches the exchange before its own order (jitter could reorder them)
        m.ex.request_cancel(oid, max(self._arrival(), o.arrival_ms))
        self._mark_live(m)

    def set_timer(self, name: str, ms: int) -> None:
        v = self.timer_ver.get(name, 0) + 1
        self.timer_ver[name] = v
        self.push(self.ctx.now + max(0, ms), P_TIMER, ("timer", name, v))

    # ------------------------------------------------------------------------------- exchange events
    def _on_ex(self, m: _Mkt, ev: ExEvent) -> None:
        o = self.orders.get(ev.order_id)
        rep = ev.ts_ms + self.R
        if o is None:  # e.g. a late cancel of an order whose terminal report was delivered
            self.order_rows.append((ev.ts_ms, rep, ev.order_id, m.info.slug, ev.kind, None, None, None, None, math.nan,
                                    math.nan, ev.reason, math.nan))
            return
        qa = math.nan
        px, size = o.px / PX_SCALE, o.size
        if ev.kind == "fill":
            f = ev.fill
            px, size, qa = f.px / PX_SCALE, f.size, f.queue_ahead_at_fill
            m.raw_fills.append((f, o, rep, self._bn_move(sym_of(m.info.coin), f.ts_ms)))
        elif ev.kind == "accepted" and o.kind == "limit":
            qa = m.md.size_at(o.up_side, o.up_px, ev.ts_ms, "ts")  # the level we join behind
        self.order_rows.append((ev.ts_ms, rep, o.id, o.slug, ev.kind, o.token, o.side, o.kind, o.post_only, px, size,
                                ev.reason, qa))
        self.push(rep, P_REPORT, ("rep", m, ev))

    def _done(self, oid: int) -> None:
        self.views.pop(oid, None)
        self.orders.pop(oid, None)
        self.cancel_sent.discard(oid)

    def _report(self, ev: ExEvent) -> None:
        v, o = self.views.get(ev.order_id), self.orders.get(ev.order_id)
        if v is None:
            return
        k = ev.kind
        v.event, v.last_fill = k, None
        if k == "accepted":
            if v.status == "pending":
                v.status = "open"
        elif k == "fill":
            f = ev.fill
            px = f.px / PX_SCALE
            self.pf.apply_fill(o, f)
            self.pf.merge(o.slug)
            v.filled += f.size
            v.notional += px * f.size
            v.fee += f.fee
            v.last_fill = FillInfo(f.ts_ms, px, f.size, f.fee, f.liquidity)
            if v.filled >= v.size - EPS:
                v.status = "filled"
                self.pf.release(o)
                self._done(v.id)
            else:
                v.status = "open"
            self._want_equity()
        elif k in ("cancelled", "expired", "rejected"):
            v.status, v.reason = k, ev.reason
            self.pf.release(o)
            self._done(v.id)
        else:  # cancel_rejected: the order is unaffected; a later cancel_all may try again
            v.reason = ev.reason
            self.cancel_sent.discard(v.id)
        self.strategy.on_order(self.ctx, v)

    # ------------------------------------------------------------------------------- internal events
    def _handle(self, item: tuple) -> None:
        t, _, _, p = item
        self.ctx.now = t
        kind = p[0]
        if kind == "rep":
            self._report(p[2])
        elif kind == "lrej":
            v = p[1]
            v.status, v.reason, v.event, v.last_fill = "rejected", p[2], "rejected", None
            self.strategy.on_order(self.ctx, v)
        elif kind == "timer":
            if not self.ending and self.timer_ver.get(p[1]) == p[2]:
                self.strategy.on_timer(self.ctx, p[1])
        elif kind == "close":
            self._close(p[1], t)
        elif kind == "settle":
            self._settle(p[1], t)
        elif kind == "redeem":
            self.pf.redeem(t)
        elif kind == "ptb":
            p[1].reveal_ptb(p[2])
        elif kind == "lcrej":
            v = self.views.get(p[1])
            if v is not None:
                v.event, v.reason, v.last_fill = "cancel_rejected", "market closed", None
                self.strategy.on_order(self.ctx, v)
        elif kind == "eq":
            self._equity_sample(t)
            if self.pf.has_exposure() and not (self.ending and not self.heap):  # nothing left to change it
                self.push(t + 1000, P_EQUITY, ("eq",))
            else:
                self.eq_sched = False

    def _want_equity(self) -> None:
        if not self.eq_sched:
            self.eq_sched = True
            self.push((self.ctx.now // 1000 + 1) * 1000, P_EQUITY, ("eq",))

    def _equity_sample(self, t: int) -> None:
        marks = {}
        for slug in self.pf.held_slugs():
            m = self.by_slug[slug]
            marks[slug] = _up_mid(m.md.top_at(t, "recv")) if m.md is not None else m.last_mid
        locked = sum(self.pf.locked.values())
        free = sum(self.pf.cash.values()) - locked  # unlocked cash, profits above the budget included
        pend = self.pf.pending()
        eq = self.pf.equity(marks)
        self.equity_rows.append((t, free, locked, pend, eq - free - locked - pend, eq))

    def _close(self, m: _Mkt, t: int) -> None:
        for ev in m.ex.close(t):
            self._on_ex(m, ev)
        self.live.pop(m.mi, None)
        m.last_mid = _up_mid(m.md.top_at(t, "recv"))
        m.closed = True
        self.open.pop(m.info.slug, None)
        if m in self.loaded:
            self.loaded.remove(m)
        if m.opened:
            self.strategy.on_market_close(self.ctx, m.pub)
        self._annotate(m)
        m.md = m.ex = None  # free the market data
        res = m.info.resolution_recv_ms
        self.push(max(res if res is not None else t, t + self.R), P_SETTLE, ("settle", m))

    def _settle(self, m: _Mkt, t: int) -> None:
        info, row = m.info, self.market_rows[m.info.slug]
        if info.outcome not in ("Up", "Down"):  # cannot happen for a replayed market; never settle at 0
            row.update(voided=True, reason="no outcome at settlement", pnl=math.nan)
            return
        res = info.resolution_recv_ms if info.resolution_recv_ms is not None else info.window_end_ms
        credit = self.pf.settle(info.slug, info.outcome, res)
        m.settle_ms = credit
        row.update(pnl=self.pf.pnl[info.slug], settle_ms=credit)
        if self.pf.pending():
            self.push(max(credit, t), P_REDEEM, ("redeem",))
            self._want_equity()

    def _annotate(self, m: _Mkt) -> None:
        md, info = m.md, m.info
        end = info.window_end_ms
        for f, o, rep, bn_move in m.raw_fills:
            up = _up_mid(md.top_at(f.ts_ms, "ts"))
            up10 = _up_mid(md.top_at(f.ts_ms + 10_000, "ts")) if f.ts_ms + 10_000 < end else math.nan
            dn = o.token == "Down"
            self.fill_rows.append((
                info.slug, info.coin, o.id, f.ts_ms, rep, o.token, o.side, o.kind, f.px / PX_SCALE, f.size, f.fee,
                f.liquidity, f.queue_ahead_at_fill, f.trade_ts_ms, 1 - up if dn else up, 1 - up10 if dn else up10,
                math.nan if info.outcome is None else float(info.outcome == o.token), bn_move))
        m.raw_fills = []

    # ------------------------------------------------------------------------------- strategy queries
    def _bn_find(self, sym: str, t: int) -> tuple[_Bn, int] | None:
        for bn in (self.bn_cur.get(sym), self.bn_prev.get(sym)):
            if bn is not None and len(bn.recv) and bn.recv[0] <= t:
                return bn, int(np.searchsorted(bn.recv, t, side="right")) - 1
        return None

    def bbo(self, sym: str, t: int) -> Bbo | None:
        f = self._bn_find(sym, t)
        if f is None:
            return None
        bn, i = f
        return Bbo(int(bn.recv[i]), sym, float(bn.bid[i]), float(bn.ask[i]), float(bn.bid_qty[i]), float(bn.ask_qty[i]))

    def _bn_mid(self, sym: str, t: int) -> float | None:
        f = self._bn_find(sym, t)
        if f is None:
            return None
        bn, i = f
        return float(bn.bid[i] + bn.ask[i]) / 2

    def _bn_move(self, sym: str, t: int) -> bool:
        """|ln(mid_t / mid_{t-1s})| > 2 sig15, with the Binance mid as received (recv clock) at the
        fill's exchange time t and 1 s before."""
        g = self.grid.get(sym)
        sig = g.sig15(t) if g is not None else None
        a, b = self._bn_mid(sym, t), self._bn_mid(sym, t - 1000)
        if sig is None or a is None or b is None:
            return False
        return abs(math.log(a / b)) > 2 * sig

    def cl_tick(self, coin: str, t: int) -> ChainlinkTick | None:
        c = self.cl.get(coin)
        if c is None:
            return None
        i = int(np.searchsorted(c[0], t, side="right")) - 1
        if i < 0:
            return None
        return ChainlinkTick(int(c[0][i]), int(c[1][i]), coin, float(c[2][i]))

    def fair_value(self, slug: str) -> float | None:
        m = self.open.get(slug)
        if m is None or m.pub.price_to_beat is None:  # not yet known to the strategy
            return None
        now, info = self.ctx.now, m.info
        tau = (info.window_end_ms - now) / 1000
        if tau <= 0:
            return None
        sym = sym_of(info.coin)
        g = self.grid.get(sym)
        sig = g.sig15(now) if g is not None else None
        if sig is None:
            return None
        c = self.cl.get(info.coin)
        if c is None:
            return None
        i = int(np.searchsorted(c[0], now, side="right")) - 1
        if i < 0:
            return None
        cl_ts, cl_px = int(c[1][i]), float(c[2][i])
        n, avg = 0, None
        if tau <= 60:  # Chainlink prices already observed in the settlement window [end - 60 s, now)
            lo, tot, j = info.window_end_ms - 60_000, 0.0, i
            while j >= 0 and c[0][j] >= lo:
                if lo <= c[1][j] < now:
                    n += 1
                    tot += float(c[2][j])
                j -= 1
            avg = tot / n if n else None
        return _fair_value(cl_px, self._bn_mid(sym, now), self._bn_mid(sym, cl_ts), m.pub.price_to_beat, sig, tau, n,
                           avg, k=K_BEST)

    # ------------------------------------------------------------------------------- setup
    def _ranges(self, con) -> list[tuple[int, int]]:
        cfg = self.cfg
        if cfg.days is None:
            days = [r[0] for r in con.execute("SELECT DISTINCT date FROM bn_fut_bbo").fetchall()]
        else:
            days = [date.fromisoformat(str(d)) for d in cfg.days]
        kinds = set(cfg.day_kinds)
        days = sorted({d for d in days if day_kind(d) in kinds})
        if not days:
            return []
        ends = dict(con.execute(
            "SELECT date, max(recv_ms) FROM bn_fut_bbo WHERE date IN (SELECT unnest(?)) AND sym IN (SELECT unnest(?)) "
            "GROUP BY date", [days, self.syms]).fetchall())
        if "pm_trade" in {r[0] for r in con.execute("SHOW TABLES").fetchall()}:
            for d, mx in con.execute("SELECT date, max(recv_ms) FROM pm_trade WHERE date IN (SELECT unnest(?)) "
                                     "GROUP BY date", [days]).fetchall():
                ends[d] = min(ends.get(d, mx), mx)
        out: list[list[int]] = []
        for d in days:
            if ends.get(d) is None:
                continue
            s, e = max(_day_ms(d), REPLAY_START_MS), _day_ms(d) + DAY_MS
            if ends[d] < e - DATA_END_SLACK_MS:  # the recording stops early that day
                e = int(ends[d]) + 1
            if e <= s:
                continue
            if out and out[-1][1] == s:
                out[-1][1] = e
            else:
                out.append([s, e])
        if self.clip is not None:
            lo, hi = self.clip
            out = [[max(a, lo), min(b, hi)] for a, b in out if min(b, hi) > max(a, lo)]
        return [(a, b) for a, b in out]

    def _select_markets(self, con, ranges) -> list[_Chunk]:
        cfg = self.cfg
        chunks: dict[int, _Chunk] = {}
        for rs, re in ranges:
            mk = con.execute(
                """SELECT slug, coin, timeframe, series, window_start_ms, window_end_ms, outcome
                   FROM markets WHERE list_contains(?, coin) AND list_contains(?, timeframe)
                     AND window_start_ms >= ? AND window_end_ms <= ?
                   ORDER BY window_start_ms, slug""", [self.coins, list(cfg.timeframes), rs, re]).df()
            if mk.empty:
                continue
            gaps = gap_table(con, rs, re, events=self.rec_events, max_gap_ms_pm=cfg.max_gap_ms_pm,
                             max_gap_ms_bn=cfg.max_gap_ms_bn)
            void = dict(voided_markets(mk, gaps, cfg.max_gap_ms_pm, cfg.max_gap_ms_bn).itertuples(index=False))
            for r in mk.itertuples(index=False):
                outcome = None if pd.isna(r.outcome) else r.outcome
                self.market_rows[r.slug] = dict(
                    slug=r.slug, coin=r.coin, timeframe=r.timeframe, window_start_ms=int(r.window_start_ms),
                    window_end_ms=int(r.window_end_ms), voided=r.slug in void, reason=void.get(r.slug, ""),
                    pnl=math.nan if r.slug in void else 0.0, settle_ms=math.nan, outcome=outcome)
                if r.slug in void:
                    continue
                h = int(r.window_start_ms) // HOUR_MS
                length = int(r.window_end_ms) - int(r.window_start_ms)
                need = max(rs, int(r.window_start_ms) - LOOKBACK_WINDOWS * length)
                c = chunks.setdefault(h, _Chunk(need, rs, []))
                c.need_ms = min(c.need_ms, need)
                c.rows.append(r)
        return sorted(chunks.values(), key=lambda c: (c.need_ms, c.rows[0].window_start_ms))

    def _load_chunk(self, con, c: _Chunk, seg_start: int) -> None:
        t0 = time.perf_counter()
        c.loaded = True
        for coin in self.coins:
            rows = [r for r in c.rows if r.coin == coin]
            if not rows:
                continue
            mds = self.loader(con, coin, list(self.cfg.timeframes), min(int(r.window_start_ms) for r in rows),
                              max(int(r.window_end_ms) for r in rows), slugs={r.slug for r in rows})
            for r in rows:
                md = mds.get(r.slug)
                if md is None:
                    self.market_rows[r.slug].update(voided=True, reason="no data", pnl=math.nan)
                    continue
                info = md.info
                m = _Mkt(info, len(self.mkts), md, Exchange(md, self.exc), c.rs)
                sn = md.snaps_recv
                i = int(np.searchsorted(sn, max(c.rs, seg_start)))
                if i >= len(sn) or sn[i] >= info.window_end_ms:
                    self.market_rows[r.slug].update(voided=True, reason="no book snapshot", pnl=math.nan)
                    continue
                m.open_ms = int(sn[i])  # the first snapshot in range: the book is trusted from here
                self.mkts.append(m)
                self.by_slug[info.slug] = m
                self.loaded.append(m)
                self.pf.add_market(info)
                self.push(info.window_end_ms, P_CLOSE, ("close", m))
                self.stats["markets"] += 1
        self.stats["load_s"] += time.perf_counter() - t0

    # ------------------------------------------------------------------------------- data per hour
    def _load_bn(self, con, s: int, e: int) -> None:
        step = self.cfg.bn_step_ms
        d = con.execute(
            f"""SELECT sym, recv_ms // {step} AS b,
                       arg_max(bid, (recv_ms, update_id)) AS bid, arg_max(ask, (recv_ms, update_id)) AS ask,
                       arg_max(bid_qty, (recv_ms, update_id)) AS bid_qty, arg_max(ask_qty, (recv_ms, update_id)) AS ask_qty
                FROM bn_fut_bbo WHERE date = ? AND sym IN (SELECT unnest(?)) AND recv_ms >= ? AND recv_ms < ?
                GROUP BY ALL ORDER BY sym, b""", [_date(s), self.syms, s, e]).fetchnumpy()
        sym = np.asarray(d["sym"]).astype(str) if len(d["sym"]) else np.empty(0, str)
        self.bn_prev, self.bn_cur = self.bn_cur, {}
        for x in self.syms:
            sel = sym == x
            bn = _Bn(np.asarray(d["b"])[sel].astype(np.int64) * step + step - 1, np.asarray(d["bid"])[sel].astype(float),
                     np.asarray(d["ask"])[sel].astype(float), np.asarray(d["bid_qty"])[sel].astype(float),
                     np.asarray(d["ask_qty"])[sel].astype(float)) if len(sym) else _EMPTY_BN
            self.bn_cur[x] = bn
            self.grid.setdefault(x, _Grid()).append(s // 1000, -(-e // 1000), bn)

    def _load_cl(self, con, s: int, e: int) -> dict[str, tuple]:
        d = con.execute(
            """SELECT coin, ts_ms, min(recv_ms) AS recv_ms, arg_min(price, recv_ms) AS price
               FROM chainlink WHERE date BETWEEN ? AND ? AND coin IN (SELECT unnest(?))
                 AND recv_ms >= ? AND recv_ms < ?
               GROUP BY coin, ts_ms HAVING min(recv_ms) >= ?
               ORDER BY recv_ms, coin, ts_ms""", [_date(s - 60_000), _date(e - 1), self.coins, s - 60_000, e, s]).fetchnumpy()
        coin = np.asarray(d["coin"]).astype(str) if len(d["coin"]) else np.empty(0, str)
        out = {}
        for c in self.coins:
            sel = coin == c
            seg = (np.asarray(d["recv_ms"])[sel].astype(np.int64), np.asarray(d["ts_ms"])[sel].astype(np.int64),
                   np.asarray(d["price"])[sel].astype(float))
            out[c] = seg
            old = self.cl.get(c)
            self.cl[c] = seg if old is None else tuple(np.r_[a, b] for a, b in zip(old, seg))
        return out

    def _segment(self, con, s: int, e: int) -> None:
        t0 = time.perf_counter()
        self._load_bn(con, s, e)
        cl = self._load_cl(con, s, e)
        self._schedule_ptb(s, e)
        recv, kind, mi, idx = [], [], [], []

        def add(r, k, m, i):
            recv.append(r)
            kind.append(np.full(len(r), k, np.int8) if np.ndim(k) == 0 else k)
            mi.append(np.full(len(r), m, np.int32))
            idx.append(i)

        want = self.want
        for m in self.loaded:
            E = m.info.window_end_ms
            lo, hi = max(s, m.open_ms), min(e, E)
            if lo >= hi:
                continue
            tr = m.md.tops_recv.recv_ms
            a, b = (int(x) for x in np.searchsorted(tr, [lo, hi]))
            if not m.opened and s <= m.open_ms < e:
                add(np.array([m.open_ms], np.int64), K_OPEN, m.mi, np.array([-1]))
            if want["on_book"] and b > a:
                add(tr[a:b], K_TOP, m.mi, np.arange(a, b))
            if want["on_trade"]:
                rr = m.md.trades_recv.recv_ms
                a, b = (int(x) for x in np.searchsorted(rr, [lo, hi]))
                if b > a:
                    add(rr[a:b], K_TRADE, m.mi, np.arange(a, b))
        bn_lists = []
        if want["on_binance"]:
            for j, x in enumerate(self.syms):
                bn = self.bn_cur[x]
                bn_lists.append((x, bn.bid.tolist(), bn.ask.tolist(), bn.bid_qty.tolist(), bn.ask_qty.tolist()))
                if len(bn.recv):
                    add(bn.recv, K_BN, j, np.arange(len(bn.recv)))
        cl_lists = []
        if want["on_chainlink"]:
            for j, c in enumerate(self.coins):
                seg = cl[c]
                cl_lists.append((c, seg[2].tolist()))
                if len(seg[0]):
                    add(seg[0], K_CL, j, np.arange(len(seg[0])))
        self.stats["data_s"] += time.perf_counter() - t0
        if recv:
            R_ = np.concatenate(recv).astype(np.int64)
            K_ = np.concatenate(kind)
            M_ = np.concatenate(mi)
            I_ = np.concatenate(idx).astype(np.int64)
            o = np.lexsort((I_, M_, K_, _RANK[K_], R_))
            self._dispatch(R_[o].tolist(), K_[o].tolist(), M_[o].tolist(), I_[o].tolist(), bn_lists, cl_lists)
        self._drain(e)
        self.stats["segments"] += 1

    def _schedule_ptb(self, s: int, e: int) -> None:
        """The strategy's price_to_beat is the mean of the received Chainlink ticks with ts in
        [window_start - 60 s, window_start) (the settlement rule, docs/RECORDER.md), revealed when the
        first tick with ts >= window_start is received. It stays None when no tick fell in that span
        or the span starts before the replay range (the live runner computes it the same way)."""
        for m in self.loaded:
            ws = m.info.window_start_ms
            if m.ptb_sched or ws >= e:
                continue
            c = self.cl.get(m.info.coin)
            hit = np.flatnonzero(c[1] >= ws) if c is not None else []
            if not len(hit):
                continue  # not revealed yet: look again next hour
            m.ptb_sched = True
            r = int(c[0][hit[0]])
            sel = (c[1] >= ws - 60_000) & (c[1] < ws) & (c[0] <= r)
            if ws - 60_000 < m.rs or not sel.any():
                continue
            self.push(max(r, s), P_TIMER, ("ptb", m, float(c[2][sel].mean())))

    def _dispatch(self, recv, kind, mi, idx, bn_lists, cl_lists) -> None:
        """The hot loop: one iteration per data event."""
        heap, R, ctx, S, mkts, open_ = self.heap, self.R, self.ctx, self.strategy, self.mkts, self.open
        on_book, on_trade, on_bn, on_cl = S.on_book, S.on_trade, S.on_binance, S.on_chainlink
        catch_up = self._catch_up
        self.stats["events"] += len(recv)
        for t, k, j, i in zip(recv, kind, mi, idx):
            if (heap and heap[0][0] < t) or t - R >= self.next_ex:
                catch_up(t)
            ctx.now = t
            if k == K_BN:
                a = bn_lists[j]
                on_bn(ctx, a[0], Bbo(t, a[0], a[1][i], a[2][i], a[3][i], a[4][i]))
            elif k == K_TOP:
                m = mkts[j]
                if not m.closed:
                    on_book(ctx, m.pub, BookView(loader=_book_loader(m.md, t)))
            elif k == K_TRADE:
                m = mkts[j]
                if not m.closed:
                    tr = m.md.trades_recv
                    on_trade(ctx, m.pub, Trade(t, int(tr.ts_ms[i]), TOKENS[tr.token[i]], SIDES[tr.side[i]],
                                               float(tr.price[i]), float(tr.size[i]), SIDES[tr.up_side[i]],
                                               int(tr.up_px[i])))
            elif k == K_CL:
                a = cl_lists[j]
                on_cl(ctx, a[0], a[1][i])
            else:  # K_OPEN
                m = mkts[j]
                if m.closed:
                    continue
                m.opened = True
                open_[m.info.slug] = m
                S.on_market_open(ctx, m.pub)

    # ------------------------------------------------------------------------------- run
    def run(self, con) -> CoinResult:
        t_start = time.perf_counter()
        ranges = self._ranges(con)
        chunks = self._select_markets(con, ranges) if ranges else []
        if ranges:
            self.ctx.now = ranges[0][0]
            self._equity_sample(ranges[0][0])
            self.strategy.on_start(self.ctx)
        ci = 0
        for ri, (rs, re) in enumerate(ranges):
            self.grid, self.bn_cur, self.bn_prev, self.cl = {}, {}, {}, {}  # vol history restarts per range
            s = rs
            while s < re:
                e = min((s // HOUR_MS + 1) * HOUR_MS, re)
                while ci < len(chunks) and chunks[ci].need_ms < e:
                    self._load_chunk(con, chunks[ci], s)
                    ci += 1
                self._segment(con, s, e)
                s = e
            if ri + 1 < len(ranges):
                self._drain(ranges[ri + 1][0])
        self.ending = True
        self._drain(INF)
        if ranges:
            self._equity_sample(max(self.ctx.now, ranges[-1][1]))
        self.stats["elapsed_s"] = time.perf_counter() - t_start
        self.stats["ranges"] = ranges
        return self._result()

    def _result(self) -> CoinResult:
        orders = pd.DataFrame(self.order_rows, columns=ORDER_COLS)
        fills = pd.DataFrame(self.fill_rows, columns=FILL_COLS)
        if not fills.empty:
            fills = fills.sort_values(["ts_ms", "order_id"], kind="stable").reset_index(drop=True)
        fills["bn_move_1s"] = fills["bn_move_1s"].astype(bool)
        equity = pd.DataFrame(self.equity_rows, columns=EQUITY_COLS)
        rows = list(self.market_rows.values())
        markets = pd.DataFrame({c: pd.Series([r[c] for r in rows], dtype=object) for c in MARKET_COLS})
        for c in ("window_start_ms", "window_end_ms"):
            markets[c] = markets[c].astype("int64")
        markets["voided"] = markets["voided"].astype(bool)
        for c in ("pnl", "settle_ms"):
            markets[c] = markets[c].astype(float)
        markets["reason"] = markets["reason"].astype(str)
        return CoinResult(orders, fills, equity, markets, self.log, self.stats)


def _book_loader(md: MarketData, t: int):
    return lambda: _levels(md, t, 5)


# ---------------------------------------------------------------------------------------- entry points

def _replay(cfg: RunConfig, coins: list[str], budgets: dict[str, float], con_factory: Callable,
            loader: Callable | None = None, strategy: Strategy | None = None,
            rec_events: pd.DataFrame | None = None, clip: tuple[int, int] | None = None) -> CoinResult:
    strategy = strategy if strategy is not None else get_strategy(cfg.strategy)(cfg.params)
    r = _Replay(cfg, coins, budgets, strategy, loader or load_coin, rec_events, clip)
    con = con_factory()
    try:
        con.execute("SET memory_limit='4GB'")  # load_coin's peak is DuckDB's buffer pool
        spill = os.path.join(tempfile.gettempdir(), "polydata-duckdb")  # not the cwd (`.tmp` in the repo)
        con.execute(f"SET temp_directory='{spill}'")
        return r.run(con)
    finally:
        con.close()


def run_coin(cfg: RunConfig, coin: str, con_factory: Callable = default_con_factory, *, loader: Callable | None = None,
             strategy: Strategy | None = None, budget: float | None = None,
             rec_events: pd.DataFrame | None = None, clip: tuple[int, int] | None = None) -> CoinResult:
    """Replay one coin's markets with its own budget (default `capital / len(coins)`).

    `loader(con, coin, timeframes, start_ms, end_ms, slugs=...)` defaults to `marketdata.load_coin`;
    `strategy` (an instance) defaults to the registry's `cfg.strategy` built with `cfg.params`;
    `rec_events` defaults to the daily `rec_events` catalog (see `gaps.gap_table`); `clip=(start_ms,
    end_ms)` narrows the replay ranges (smoke runs and tests)."""
    b = cfg.capital / len(cfg.coins) if budget is None else budget
    return _replay(cfg, [coin], {coin: b}, con_factory, loader, strategy, rec_events, clip)


def _worker(args) -> tuple[str, CoinResult]:
    cfg_d, coin, budget, con_factory = args
    return coin, run_coin(RunConfig.from_dict(cfg_d), coin, con_factory, budget=budget)


def run(cfg: RunConfig, con_factory: Callable = default_con_factory, workers: int | None = None) -> RunResult:
    """Run the config. Per-coin mode: one process per coin (at most `workers`, default
    min(coins, 4): each process can use ~4 GB), budgets `capital / len(coins)`. Global mode: every
    coin in one replay with one shared balance."""
    t0 = time.perf_counter()
    if cfg.capital_mode == "global":
        res = {"*": _replay(cfg, list(cfg.coins), {"*": cfg.capital}, con_factory)}
        return RunResult(cfg, res, time.perf_counter() - t0)
    budget = cfg.capital / len(cfg.coins)
    args = [(cfg.to_dict(), c, budget, con_factory) for c in cfg.coins]
    n = min(len(args), workers or min(4, os.cpu_count() or 1))
    if n <= 1:
        out = [_worker(a) for a in args]
    else:
        with multiprocessing.get_context("spawn").Pool(n) as pool:
            out = pool.map(_worker, args, chunksize=1)
    return RunResult(cfg, dict(out), time.perf_counter() - t0)


__all__ = ["CoinResult", "RunResult", "SimCtx", "default_con_factory", "run", "run_coin", "sym_of"]

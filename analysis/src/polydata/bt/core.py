"""The simulation core: everything in a backtest that is not loading data.

`Core` owns the strategy clock, the internal event heap, order routing through the per-market `Exchange`,
report delivery, the portfolio, settlement and the strategy's queries. A driver feeds it: `engine.ReplayDriver`
loads recorded data per hour and calls `dispatch` with the sorted data events. The event-order rules are
documented in `engine`'s module docstring.
"""

from __future__ import annotations

import heapq
import logging
import math
import zlib
from dataclasses import dataclass, field, replace
from typing import NamedTuple

import numpy as np
import pandas as pd

from .config import RunConfig
from .exchange import EPS, Exchange, ExchangeConfig, ExEvent, Order
from .fairvalue import K_BEST
from .fairvalue import fair_value as _fair_value
from .marketdata import SIDES, TOKENS, MarketData
from .portfolio import Portfolio
from .strategy import BookView, FillInfo, OrderView, Strategy
from .types import NO_ASK_PX, NO_BID_PX, PX_SCALE, Bbo, ChainlinkTick, MarketInfo, Top, Trade

log = logging.getLogger(__name__)

HOUR_MS = 3_600_000
INF = float("inf")
SIG_WINDOW_S = 900  # sig15: 15 min of 1 s Binance returns

# Data event kinds, in delivery order at an equal recv_ms. The first four are Polymarket. K_ADMIT (a market joins the
# run at that recv_ms) is used by the live and session drivers only; the replay admits markets at their chunk load.
K_ADMIT, K_OPEN, K_TOP, K_TRADE, K_CL, K_BN = 0, 1, 2, 3, 4, 5
_RANK = np.array([0, 0, 0, 0, 1, 2], np.int8)  # stream rank: Polymarket 0, Chainlink 1, Binance 2
# Internal events at an equal ms
P_REPORT, P_CLOSE, P_TIMER, P_SETTLE, P_REDEEM, P_EQUITY = range(6)
SETTLE_KEY_SHIFT = 2**20  # settle heap key: window_end_ms * 2**20 + load index

ORDER_COLS = ["ms", "report_ms", "order_id", "slug", "event", "token", "side", "kind", "post_only", "px", "size",
              "reason", "queue_ahead"]
FILL_COLS = ["slug", "coin", "order_id", "ts_ms", "report_ms", "token", "side", "kind", "px", "size", "fee",
             "liquidity", "queue_ahead_at_fill", "trade_ts_ms", "mid_at_fill", "mid_10s", "settle_value",
             "bn_move_1s", "mid_before"]
# mid_before: token-terms mid at ts_ms - 200 on the exchange clock. The recorded level change that our fill's trade
# caused lands up to ~150 ms before the trade's ts, so mid_at_fill is already the post-trade mid. Post-hoc, metrics only.
MID_BEFORE_MS = 200
_TOKEN, _SETTLE_VALUE = FILL_COLS.index("token"), FILL_COLS.index("settle_value")
# cash = total unlocked cash, realised profits included (not capped at the budget like ctx.cash)
EQUITY_COLS = ["ms", "cash", "locked", "pending", "mtm", "equity"]
MARKET_COLS = ["slug", "coin", "timeframe", "window_start_ms", "window_end_ms", "voided", "reason", "pnl",
               "settle_ms", "outcome"]


def sym_of(coin: str) -> str:
    return f"{coin.upper()}USDT"


def _up_mid(top: Top) -> float:
    if top.bid_px == NO_BID_PX or top.ask_px == NO_ASK_PX:  # a one-sided book has no mid
        return math.nan
    return (top.bid_px + top.ask_px) / (2 * PX_SCALE)


# ---------------------------------------------------------------------------------------- results

@dataclass
class CoinResult:
    orders: pd.DataFrame  # one row per order event (ORDER_COLS)
    fills: pd.DataFrame  # one row per fill (FILL_COLS)
    equity: pd.DataFrame  # EQUITY_COLS, every 1 s while positions exist (+ first and last)
    markets: pd.DataFrame  # MARKET_COLS: every market considered, voided ones with a reason and NaN P&L
    log: list[str]
    stats: dict = field(default_factory=dict)
    decisions: pd.DataFrame = field(default_factory=lambda: _decisions_frame(None))  # trace.DECISION_COLS


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
    def decisions(self) -> pd.DataFrame:
        return self._cat("decisions")

    @property
    def log(self) -> list[str]:
        return [line for r in self.coins.values() for line in r.log]


def _decisions_frame(rows: list[tuple] | None) -> pd.DataFrame:
    from .trace import DECISION_COLS  # trace imports SimCtx from here

    return pd.DataFrame(rows or [], columns=DECISION_COLS)


# ---------------------------------------------------------------------------------------- state

class _Bn(NamedTuple):
    """Bundled Binance rows of one symbol for one hour, in recv order (recv = bucket's last ms)."""
    recv: np.ndarray
    bid: np.ndarray
    ask: np.ndarray
    bid_qty: np.ndarray
    ask_qty: np.ndarray


_EMPTY_BN = _Bn(*(np.empty(0, t) for t in (np.int64, float, float, float, float)))


def _grid_segment(sec_from: int, sec_to: int, bn, prev: float | None, first: bool, base: float
                  ) -> tuple[int, np.ndarray, np.ndarray] | None:
    """The 1 s grid of the seconds [sec_from, sec_to) from bundles `bn` (every bundle in that span, recv order):
    (sec0_offset, filled, csum). `first`: nothing precedes the segment, so it starts at its first second with data
    (`sec0_offset`; None if there is none), and that second has no return. Otherwise `prev` is the previous second's
    mid and `base` the running sum before it. Pure, so the live grid can recompute a segment's prefix each second and
    get the same bits (a prefix of a cumsum is the cumsum of the prefix)."""
    n = sec_to - sec_from
    vals = np.full(n, np.nan)
    if len(bn.recv):
        sec = bn.recv // 1000 - sec_from
        last = np.r_[sec[1:] != sec[:-1], True]
        vals[sec[last]] = ((bn.bid + bn.ask) / 2)[last]
    off = 0
    if first:
        ok = np.flatnonzero(~np.isnan(vals))
        if not len(ok):
            return None
        off, vals = int(ok[0]), vals[ok[0]:]
        prev = vals[0]
    idx = np.where(~np.isnan(vals), np.arange(len(vals)), -1)
    idx = np.maximum.accumulate(idx)
    filled = np.where(idx >= 0, vals[np.maximum(idx, 0)], prev)
    r2 = np.log(filled / np.r_[prev, filled[:-1]]) ** 2
    if first:
        r2[0] = 0.0  # the first second has no return
    return off, filled, base + np.cumsum(r2)


class _Grid:
    """1 s Binance mids (last update received in each second, forward-filled) and the running sum of
    squared log returns, for sig15. Restarted at each replay range."""

    def __init__(self):
        self.sec0: int | None = None
        self.mids = np.empty(0)
        self.csum = np.empty(0)  # csum[j] = sum of r^2 over returns 1..j (r_j = ln(mid_j / mid_{j-1}))

    def append(self, sec_from: int, sec_to: int, bn: _Bn) -> None:
        """Add the seconds [sec_from, sec_to) from this hour's bundles."""
        first = self.sec0 is None
        if not first and self.sec0 + len(self.mids) != sec_from:
            raise RuntimeError("Binance grid is not contiguous")
        seg = _grid_segment(sec_from, sec_to, bn, None if first else self.mids[-1], first,
                            self.csum[-1] if len(self.csum) else 0.0)
        if seg is None:
            return
        off, filled, csum = seg
        if first:
            self.sec0 = sec_from + off
        self.mids = np.r_[self.mids, filled]
        self.csum = np.r_[self.csum, csum]

    def sig15(self, t_ms: int) -> float | None:
        """sqrt(mean r^2) over the 900 returns ending at the last complete second before t_ms."""
        if self.sec0 is None:
            return None
        j = t_ms // 1000 - 1 - self.sec0
        if j < SIG_WINDOW_S or j >= len(self.csum):
            return None
        return math.sqrt(max(self.csum[j] - self.csum[j - SIG_WINDOW_S], 0.0) / SIG_WINDOW_S)


class _Mkt:
    __slots__ = ("info", "pub", "mi", "md", "ex", "rs", "load_ms", "resolved", "open_ms", "opened", "closed",
                 "close_ms", "last_mid", "raw_fills", "settle_ms", "settle_pushed", "fill_idx", "ptb_sched")

    def __init__(self, info: MarketInfo, mi: int, md: MarketData, ex: Exchange, rs: int, load_ms: int):
        self.info = info
        self.load_ms = load_ms  # when the driver staged it (the load index `mi` is its position in Core.mkts)
        self.resolved = info.outcome is not None
        # What the strategy may see: no outcome, and price_to_beat only once computed from received
        # Chainlink ticks (see Core.scan_ptb).
        self.pub = replace(info, outcome=None, resolution_recv_ms=None, price_to_beat=None)
        self.ptb_sched = False
        self.mi, self.md, self.ex, self.rs = mi, md, ex, rs
        self.open_ms: int | None = None
        self.opened = self.closed = False
        self.close_ms: int | None = None
        self.last_mid = math.nan
        self.raw_fills: list[tuple] = []
        self.settle_ms: int | None = None
        self.settle_pushed = False
        self.fill_idx: list[int] = []  # fill rows annotated before the outcome was known (settle_value NaN)

    def reveal_ptb(self, value: float) -> None:
        self.pub = replace(self.pub, price_to_beat=value)


# ---------------------------------------------------------------------------------------- context

class SimCtx:
    """`strategy.Ctx` for the backtester. Every query answers as of `now` on the strategy clock."""

    __slots__ = ("now", "_r")

    def __init__(self, core: Core):
        self._r = core
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


# ---------------------------------------------------------------------------------------- core

class Core:
    def __init__(self, cfg: RunConfig, coins: list[str], budgets: dict[str, float], strategy: Strategy, *,
                 trace: bool = False, defer_settle: bool = False):
        self.cfg, self.coins, self.strategy = cfg, list(coins), strategy
        # False (replay): every outcome is known at load, the settle is pushed at the close. True (live and session
        # replay): only once the market is resolved (set_resolution), or voided by finish_unresolved.
        self.defer_settle = defer_settle
        self._void_unresolved = False
        self.R = int(cfg.fill_report_latency_ms)
        self.lat, self.jitter = int(cfg.order_latency_ms), int(cfg.latency_jitter_ms)
        self.rng = np.random.default_rng([int(cfg.seed), zlib.crc32(",".join(self.coins).encode())])
        self.exc = ExchangeConfig(cancel_model=cfg.cancel_model, impact_reset_ms=cfg.impact_reset_ms)
        self.pf = Portfolio(budgets, redeem_delay_ms=cfg.redeem_delay_s * 1000)
        self.ctx = SimCtx(self)
        self.heap: list[tuple] = []
        self._seq = 0
        self.hour_seq: dict[int, int] = {}  # strategy-clock hour (and range start) -> _seq when it was marked
        self._ptb_wait: dict[int, list[tuple]] = {}  # unmarked hour -> reveals (m, r, value) waiting for it
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
        # Call tracing (trace.py): every handler and ctx call as a row. `want` above stays the wrapped class's.
        self.decisions: list[tuple] | None = None
        if trace:
            from .trace import TracedCtx, TracedStrategy

            self.decisions = []
            self.strategy = TracedStrategy(strategy, self.decisions)
            self.ctx = TracedCtx(self, self.decisions)

    # ------------------------------------------------------------------------------- scheduling
    # Heap entries are (t, prio, k, tie, payload). An ordinary event has k = push sequence and tie = 0, so events at
    # an equal (t, prio) come in push order. Two kinds get a key that does not depend on when they are pushed (the
    # live driver pushes them at other moments than the replay), chosen to equal the replay's push order:
    # - ptb reveal: k = hour_seq[s*] + 0.5, tie = load index (`_push_ptb`);
    # - settle: k = window_end_ms * 2**20 + load index, tie = 0 (`_push_settle`): the replay pushes settles at the
    #   close, and closes run in (window_end_ms, load order).
    def push(self, t: int, prio: int, payload: tuple) -> None:
        self._seq += 1
        heapq.heappush(self.heap, (int(t), prio, self._seq, 0, payload))

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
            # the exchange's own queue position at the join (no trade can have touched it yet: trades
            # within trade_echo_ms of the arrival never do, and the exchange is advanced one time at a time)
            qa = o.queue_ahead
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
        t, _, _, _, p = item
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
            if m.md is not None:
                mid = _up_mid(m.md.top_at(t, "recv"))
                if mid == mid:  # a one-sided book has no mid: keep the last finite one
                    m.last_mid = mid
            marks[slug] = m.last_mid
        locked = sum(self.pf.locked.values())
        free = sum(self.pf.cash.values()) - locked  # unlocked cash, profits above the budget included
        pend = self.pf.pending()
        eq = self.pf.equity(marks)
        self.equity_rows.append((t, free, locked, pend, eq - free - locked - pend, eq))

    def _close(self, m: _Mkt, t: int) -> None:
        for ev in m.ex.close(t):
            self._on_ex(m, ev)
        self.live.pop(m.mi, None)
        mid = _up_mid(m.md.top_at(t, "recv"))
        if mid == mid:
            m.last_mid = mid
        m.closed = True
        self.open.pop(m.info.slug, None)
        if m in self.loaded:
            self.loaded.remove(m)
        if m.opened:
            self.strategy.on_market_close(self.ctx, m.pub)
        self._annotate(m)
        m.md = m.ex = None  # free the market data
        m.close_ms = t
        if not self.defer_settle or m.resolved or self._void_unresolved:
            self._push_settle(m)

    def _push_settle(self, m: _Mkt) -> None:
        """Settle once the resolution was received and every report is in: max(resolution, close + R)."""
        t, res = m.close_ms, m.info.resolution_recv_ms
        at = int(max(res if res is not None else t, t + self.R))
        # only the void of an unresolved market after finish_unresolved may lie before now (Decision 3)
        if at < self.ctx.now and not (self._void_unresolved and not m.resolved):
            raise RuntimeError(f"{m.info.slug}: settle pushed into the past (at {at} < now {self.ctx.now})")
        m.settle_pushed = True
        heapq.heappush(self.heap, (at, P_SETTLE, m.info.window_end_ms * SETTLE_KEY_SHIFT + m.mi, 0, ("settle", m)))

    def _settle(self, m: _Mkt, t: int) -> None:
        info, row = m.info, self.market_rows[m.info.slug]
        if info.outcome not in ("Up", "Down"):  # cannot happen for a replayed market; never settle at 0
            row.update(voided=True, reason="no outcome at settlement", pnl=math.nan)
            return
        if m.fill_idx:  # fills annotated while the outcome was unknown
            rows = self.fill_rows
            for i in m.fill_idx:
                r = rows[i]
                rows[i] = r[:_SETTLE_VALUE] + (float(info.outcome == r[_TOKEN]),) + r[_SETTLE_VALUE + 1:]
            m.fill_idx = []
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
        first = len(self.fill_rows)
        for f, o, rep, bn_move in m.raw_fills:
            up = _up_mid(md.top_at(f.ts_ms, "ts"))
            up10 = _up_mid(md.top_at(f.ts_ms + 10_000, "ts")) if f.ts_ms + 10_000 < end else math.nan
            ub = _up_mid(md.top_at(f.ts_ms - MID_BEFORE_MS, "ts"))
            dn = o.token == "Down"
            self.fill_rows.append((
                info.slug, info.coin, o.id, f.ts_ms, rep, o.token, o.side, o.kind, f.px / PX_SCALE, f.size, f.fee,
                f.liquidity, f.queue_ahead_at_fill, f.trade_ts_ms, 1 - up if dn else up, 1 - up10 if dn else up10,
                math.nan if info.outcome is None else float(info.outcome == o.token), bn_move, 1 - ub if dn else ub))
        if info.outcome is None:
            m.fill_idx.extend(range(first, len(self.fill_rows)))  # _settle fills in settle_value
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

    # ------------------------------------------------------------------------------- data events
    def dispatch(self, recv, kind, mi, idx, bn_lists, cl_lists) -> None:
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
            elif k == K_OPEN:
                m = mkts[j]
                if m.closed:
                    continue
                m.opened = True
                open_[m.info.slug] = m
                S.on_market_open(ctx, m.pub)
            else:  # K_ADMIT: no strategy call
                self.admit(j)

    # ------------------------------------------------------------------------------- driver entry points
    def stage(self, info: MarketInfo, md, rs: int, load_ms: int) -> int:
        """Create a market's state (exchange included) and give it the next load index. Nothing is scheduled
        and the market is not live until `admit`."""
        mi = len(self.mkts)
        m = _Mkt(info, mi, md, Exchange(md, self.exc), rs, load_ms)
        self.mkts.append(m)
        self.by_slug[info.slug] = m
        return mi

    def admit(self, mi: int, row: dict | None = None) -> None:
        """A staged market joins the run: the portfolio knows it, its close is scheduled at window_end_ms and it
        gets a result row (`row`, else the one the driver already made, else a default)."""
        m = self.mkts[mi]
        info = m.info
        self.loaded.append(m)
        self.pf.add_market(info)
        self.push(info.window_end_ms, P_CLOSE, ("close", m))
        if row is not None:
            self.market_rows[info.slug] = row
        elif info.slug not in self.market_rows:
            self.market_rows[info.slug] = dict(
                slug=info.slug, coin=info.coin, timeframe=info.timeframe, window_start_ms=int(info.window_start_ms),
                window_end_ms=int(info.window_end_ms), voided=False, reason="", pnl=0.0, settle_ms=math.nan,
                outcome=info.outcome)
        self.stats["markets"] += 1
        self.scan_ptb()

    def add_market(self, info: MarketInfo, md, rs: int, load_ms: int, open_ms: int) -> _Mkt:
        """`stage` + `admit`, with the book trusted from `open_ms` (the replay's chunk load)."""
        m = self.mkts[self.stage(info, md, rs, load_ms)]
        self.admit(m.mi)
        m.open_ms = open_ms
        return m

    def set_resolution(self, slug: str, outcome: str, recv_ms: int) -> None:
        """The market's outcome, received at recv_ms. A closed market's settle is pushed now; otherwise `_close`
        pushes it."""
        m = self.by_slug[slug]
        if m.settle_pushed:  # a duplicate or corrected resolution, or one after finish_unresolved: too late
            self.stats["late_resolutions"] = self.stats.get("late_resolutions", 0) + 1
            log.warning("%s: resolution %s at %d ignored: its settle is already scheduled", slug, outcome, recv_ms)
            return
        m.info = replace(m.info, outcome=outcome, resolution_recv_ms=int(recv_ms))
        m.resolved = True
        row = self.market_rows.get(slug)
        if row is not None:
            row["outcome"] = outcome
        if m.closed and not m.settle_pushed:
            self._push_settle(m)

    def finish_unresolved(self) -> None:
        """No more resolutions will come (Decision 3): every closed, unresolved market settles at close + R, as the
        replay does, and is voided "no outcome at settlement". Markets still open settle that way at their close.
        Call it before `finish`. A settle may lie before ctx.now; it calls no strategy handler."""
        self._void_unresolved = True
        for m in self.mkts:
            if m.closed and not m.settle_pushed:
                self._push_settle(m)

    def on_trades_appended(self, mi: int) -> None:
        """A growing MarketData gained trades: the market's exchange may have an earlier next event."""
        if mi in self.live:
            n = self.mkts[mi].ex.next_event_ms()
            if n < self.next_ex:
                self.next_ex = n

    # ------------------------------------------------------------------------------- price to beat
    # The strategy's price_to_beat is the mean of the received Chainlink ticks with ts in [window_start - 60 s,
    # window_start) (the settlement rule, docs/RECORDER.md), revealed when the first tick with ts >= window_start is
    # received (at r). It stays None when no tick fell in that span or the span starts before the replay range.
    # The reveal is a P_TIMER event at max(r, s*, load_ms), s* = max(rs, floor_hour(max(r, window_start, load_ms))),
    # keyed (hour_seq[s*] + 0.5, load index): after every event pushed before hour s* was marked, before every event
    # pushed after. That is where the replay used to push it, at the start of the hour segment in which it found the
    # tick, whenever the reveal is found (the live driver finds it when the tick is released).
    def mark_hour(self, s: int) -> None:
        """Hour boundary (or range start) s of the strategy clock: everything before s is drained and no data event
        at or after s has been dispatched. Reveals waiting for s are pushed."""
        self.hour_seq[s] = self._seq
        for m, r, v in self._ptb_wait.pop(s, []):  # in scan order; the heap tie (load index) orders them
            self._push_ptb(m, r, v, s)

    def scan_ptb(self) -> None:
        """Look for the revealing tick of every admitted, open market whose reveal is not scheduled yet."""
        top: dict[str, float] = {}
        for m in self.loaded:
            if m.ptb_sched:
                continue
            coin, ws = m.info.coin, m.info.window_start_ms
            c = self.cl.get(coin)
            if c is None or not len(c[1]):
                continue
            if coin not in top:
                top[coin] = c[1].max()
            if top[coin] < ws:
                continue  # no tick with ts >= window_start received yet
            hit = np.flatnonzero(c[1] >= ws)
            m.ptb_sched = True
            r = int(c[0][hit[0]])
            sel = (c[1] >= ws - 60_000) & (c[1] < ws) & (c[0] <= r)
            if ws - 60_000 < m.rs or not sel.any():
                continue
            self.schedule_ptb(m, r, float(c[2][sel].mean()))

    def schedule_ptb(self, m: _Mkt, r: int, value: float | None) -> None:
        s_star = max(m.rs, (max(r, m.info.window_start_ms, m.load_ms) // HOUR_MS) * HOUR_MS)
        if s_star in self.hour_seq:
            self._push_ptb(m, r, value, s_star)
        else:
            self._ptb_wait.setdefault(s_star, []).append((m, r, value))

    def _push_ptb(self, m: _Mkt, r: int, value: float | None, s_star: int) -> None:
        at = int(max(r, s_star, m.load_ms))
        if at < self.ctx.now:
            raise RuntimeError(f"{m.info.slug}: ptb reveal pushed into the past (at {at} < now {self.ctx.now})")
        heapq.heappush(self.heap, (at, P_TIMER, self.hour_seq[s_star] + 0.5, m.mi, ("ptb", m, value)))

    def reset_range(self) -> None:
        """A new replay range: the Binance/Chainlink history (and so sig15) restarts."""
        self.grid, self.bn_cur, self.bn_prev, self.cl = {}, {}, {}, {}

    def add_bn_hour(self, s: int, e: int, bns: dict[str, _Bn]) -> None:
        """The Binance bundles of the hour [s, e), one per symbol."""
        self.bn_prev, self.bn_cur = self.bn_cur, {}
        for x in self.syms:
            bn = bns[x]
            self.bn_cur[x] = bn
            self.grid.setdefault(x, _Grid()).append(s // 1000, -(-e // 1000), bn)

    def add_cl(self, seg: dict[str, tuple[np.ndarray, np.ndarray, np.ndarray]]) -> None:
        """Received Chainlink ticks (recv_ms, ts_ms, price) per coin, appended in recv order."""
        for c, part in seg.items():
            old = self.cl.get(c)
            self.cl[c] = part if old is None else tuple(np.r_[a, b] for a, b in zip(old, part))
        self.scan_ptb()

    # ------------------------------------------------------------------------------- run
    def start(self, t: int) -> None:
        self.ctx.now = t
        self._equity_sample(t)
        self.strategy.on_start(self.ctx)

    def finish(self, end_ms: int) -> None:
        if self.defer_settle and not self._void_unresolved:
            raise RuntimeError("finish with defer_settle needs finish_unresolved() first")
        self.ending = True
        self._drain(INF)
        self._equity_sample(max(self.ctx.now, end_ms))

    def result(self) -> CoinResult:
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
        return CoinResult(orders, fills, equity, markets, self.log, self.stats, _decisions_frame(self.decisions))


def _book_loader(md: MarketData, t: int):
    return lambda: _levels(md, t, 5)

"""Simulated Polymarket exchange for one market: queue position, trade-through fills, takers, impact.

Everything runs on the exchange clock (`ts_ms`) against the combined Up-terms book of `MarketData`.
The model is deliberately pessimistic (spec: "Queue and fill model"):

- **Join.** A resting order joins at the back: `queue_ahead` = the recorded level size at arrival.
  A later order of ours at the same level never queues ahead of an earlier one (its `queue_ahead`
  is at least theirs).
- **Recorded trades** (taker side T, Up price P, size S) match our resting orders on the other side,
  in price priority for the taker, then arrival order:
  - at a price b strictly better for the taker than P (trade-through): we fill
    min(remaining, max(0, x - B - queue_ahead)). A sweep is printed as ONE row at its worst price P
    with the total size x, so before reaching us the taker used B, the recorded decrease (same
    window and per-ms rule as the at-price cap below) of every level strictly better than b on that
    side, and the queue ahead of us at b. Never more than the old min(remaining, x);
  - at P: the trade first consumes `queue_ahead`, then fills us. `x`, the trade volume left for
    our later orders, shrinks by every fill of ours (trade-throughs included), never by the
    volume that consumed `queue_ahead` (each order's `queue_ahead` counts other traders only).
  - **At-price cap.** At P the usable volume is also capped by the recorded decrease of level
    (resting side, P) over the exchange-time window [ts - trade_echo_ms, ts]: the sum over the ms in
    that window of max(0, size before the ms - size after it). A taker that sweeps several levels is
    usually printed as ONE row at its worst price with the total size (2026-11-24 data), so `x` can
    include volume executed at better levels that never reached P; the level decrease bounds what
    really traded at P. Uses no data after the trade. Trade-throughs are not capped this way.
  - **Trade echo:** trades with `ts_ms <= book_ms + trade_echo_ms` (book_ms: when the order joined) never touch the order (no queue
    reduction, no fill): a trade's `ts_ms` lags the level change it caused (median 21 ms, p99 134 ms), so its
    volume may already be missing from the level we joined behind.
- **Cancels by others.** `"behind"`: ignored. `"proportional"`: a level decrease not explained by
  trade volume at that level within `trade_explain_ms` after it removes `decrease * ahead / size_before`
  (at most `decrease`) from each of our orders that joined before it.
- **Takers** (and the crossing part of a non-post-only limit) walk `book_at(arrival, "ts")` up to the
  limit, minus our own earlier impact; one fee per order on its average price; a `take` remainder
  is cancelled (IOC), a limit remainder rests.
- **Taker delay** (`taker_delay_ms` > 0, Polymarket's hold on some crypto markets): a non-post-only order that
  crosses the recorded top at arrival stays pending for the hold (a cancel of it is rejected, "taker delay"),
  then is validated again and processed as an arrival at the hold's end: accepted, matched against the book
  of that time, the rest cancelled (take) or resting (limit, joining then). Others' cancels during the hold
  are in that book. A hold ending at or after the window end is rejected ("after window end").
- **Impact** (liquidity we took) per (level side, px) stays removed until the recorded level reaches
  0 after our last take there, or more than `impact_reset_ms` after it.

**Window end.** When the clock reaches `window_end_ms`, every open order expires there, whether or
not the engine calls `close()`: trades with `ts_ms >= window_end_ms` never match.

Same-ms order: recorded trades, then our cancels, then our arrivals; except that a cancel never
overtakes its own order: an order arriving in the same ms as its cancel is processed first. `advance` returns `ExEvent`s in
exchange-time order; the engine adds report latency. Order ids are assigned by the caller.
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass, field
from typing import Literal

import numpy as np

from ..fees import taker_fee
from .marketdata import SIDES, MarketData
from .types import PX_SCALE, Side, Token, from_up, to_up

EPS = 1e-9
_INF = float("inf")


@dataclass(eq=False)  # identity: orders are tracked by object in the queues
class Order:
    id: int
    slug: str
    token: Token
    side: Side
    px: int  # token terms, 1/1000
    size: float
    kind: Literal["limit", "take"]
    post_only: bool
    placed_ms: int
    arrival_ms: int
    status: Literal["pending", "open", "filled", "cancelled", "rejected"] = "pending"
    filled: float = 0.0
    cost: float = 0.0  # USDC notional of the fills (token price x size), excluding fees
    fee: float = 0.0
    queue_ahead: float = 0.0
    reason: str = ""
    book_ms: int = 0  # when it joined our book (its arrival, or the end of a taker hold)

    @property
    def up_side(self) -> Side:
        return to_up(self.token, self.side, self.px)[0]

    @property
    def up_px(self) -> int:
        return to_up(self.token, self.side, self.px)[1]

    @property
    def remaining(self) -> float:
        return self.size - self.filled


@dataclass(frozen=True)
class Fill:
    order_id: int
    ts_ms: int
    px: int  # token terms
    size: float
    fee: float
    queue_ahead_at_fill: float
    liquidity: Literal["maker", "taker"]
    trade_ts_ms: int | None  # the supporting recorded trade; None for taker fills


@dataclass(frozen=True)
class ExEvent:
    ts_ms: int
    order_id: int
    kind: Literal["accepted", "rejected", "cancelled", "cancel_rejected", "fill", "expired"]
    fill: Fill | None = None
    reason: str = ""


@dataclass(frozen=True)
class ExchangeConfig:
    cancel_model: Literal["behind", "proportional"] = "behind"
    impact_reset_ms: int = 5000
    # Trade ts minus the ts of the level change it caused (BTC 5m): median 21 ms, p95 40 ms, p99 134 ms.
    trade_echo_ms: int = 150
    trade_explain_ms: int = 150  # proportional: a decrease followed by trade volume this soon is not a cancel
    taker_delay_ms: int = 0  # Polymarket's hold of marketable orders on this market (0: none)


@dataclass
class _LevelState:
    """Proportional cancel model bookkeeping for a level we rest at."""
    t: int  # level changes up to here are applied
    used: dict[int, float] = field(default_factory=dict)  # trade index -> volume already explaining decreases


class Exchange:
    def __init__(self, md: MarketData, cfg: ExchangeConfig = ExchangeConfig()):
        if cfg.cancel_model not in ("behind", "proportional"):
            raise ValueError(f"unknown cancel_model {cfg.cancel_model!r}")
        self.md, self.cfg = md, cfg  # md.trades is read at each call: a growing MarketData may append trades
        self._cur = 0  # next recorded trade (ts order)
        self._now = -(2**62)  # everything <= _now is processed
        self.orders: dict[int, Order] = {}
        self._arrivals: list[tuple[int, int, Order]] = []  # heap (arrival_ms, seq, order)
        self._cancels: list[tuple[int, int, int]] = []  # heap (arrival_ms, seq, order_id)
        self._seq = 0
        self._book: dict[str, dict[int, list[Order]]] = {"BUY": {}, "SELL": {}}  # our resting orders, Up terms
        self._n_open = 0
        self._impact: dict[tuple[str, int], list] = {}  # (level side, px) -> [shares taken, last take ms]
        self._lvl: dict[tuple[str, int], _LevelState] = {}
        self._end = int(md.info.window_end_ms)
        self._expired = False  # open orders were expired at the window end
        self._held: set[int] = set()  # marketable orders in the taker hold (back in `_arrivals` at its end)

    # ------------------------------------------------------------------------------------ requests
    def submit(self, order: Order) -> None:
        if order.arrival_ms < self._now:
            raise ValueError(f"order {order.id} arrives at {order.arrival_ms}, before {self._now} (already processed)")
        if order.id in self.orders:
            raise ValueError(f"duplicate order id {order.id}")
        order.status = "pending"
        self.orders[order.id] = order
        heapq.heappush(self._arrivals, (int(order.arrival_ms), self._next_seq(), order))

    def request_cancel(self, order_id: int, arrival_ms: int) -> None:
        if arrival_ms < self._now:
            raise ValueError(f"cancel arrives at {arrival_ms}, before {self._now} (already processed)")
        heapq.heappush(self._cancels, (int(arrival_ms), self._next_seq(), order_id))

    def _next_seq(self) -> int:
        self._seq += 1
        return self._seq

    # ------------------------------------------------------------------------------------ clock
    def advance(self, t_ms: int) -> list[ExEvent]:
        """Process recorded trades, cancel arrivals and order arrivals with time <= t_ms."""
        out: list[ExEvent] = []
        tr = self.md.trades
        ts, n = tr.ts_ms, len(tr.ts_ms)
        while True:
            ta = self._arrivals[0][0] if self._arrivals else _INF
            tc = self._cancels[0][0] if self._cancels else _INF
            nxt = min(ta, tc)
            if not self._n_open and self._cur < n:
                # nothing rests: trades up to the next arrival/cancel (same-ms trades come first) are moot
                lim = min(t_ms, nxt)
                if ts[self._cur] <= lim:
                    self._cur = int(np.searchsorted(ts, lim, side="right"))
            tt = int(ts[self._cur]) if self._cur < n else _INF
            if not self._expired and t_ms >= self._end and min(tt, nxt) >= self._end:
                self._expire_open(self._end, out)  # before anything at or after the window end
                self._expired = True
                continue
            if min(tt, nxt) > t_ms:
                break
            if tt <= nxt:
                self._on_trade(self._cur, out)
                self._cur += 1
            elif tc <= ta:
                oid = self._cancels[0][2]
                o = self.orders.get(oid)
                if o is not None and o.status == "pending" and o.arrival_ms <= tc and oid not in self._held:
                    # its own order arrives at the same ms: the order (sent first) is processed first
                    i = next(i for i, x in enumerate(self._arrivals) if x[2] is o)
                    self._arrivals[i] = self._arrivals[-1]
                    self._arrivals.pop()
                    heapq.heapify(self._arrivals)
                    self._on_arrival(o, out)
                    continue
                t, _, oid = heapq.heappop(self._cancels)
                self._on_cancel(oid, t, out)
            else:
                t, _, o = heapq.heappop(self._arrivals)
                if o.id in self._held:
                    self._on_release(o, t, out)
                else:
                    self._on_arrival(o, out)
        self._now = max(self._now, t_ms)
        return out

    def next_event_ms(self) -> float:
        """A lower bound on the exchange time of the next event `advance` could produce (inf if none):
        the next order or cancel arrival, and while orders rest, the next recorded trade and the
        window end. The engine skips `advance` calls before it."""
        t = min(self._arrivals[0][0] if self._arrivals else _INF, self._cancels[0][0] if self._cancels else _INF)
        if self._n_open:
            ts = self.md.trades.ts_ms
            if self._cur < len(ts):
                t = min(t, int(ts[self._cur]))
            if not self._expired:
                t = min(t, self._end)
        return t

    def close(self, t_ms: int) -> list[ExEvent]:
        """Advance to `t_ms` (normally the window end), expire what is still open and cancel orders
        that have not arrived yet. Idempotent."""
        out = self.advance(t_ms)
        self._expire_open(t_ms, out)
        for _, _, o in sorted(self._arrivals):
            if o.id in self._held:  # its re-validation would come after the window end
                o.status, o.reason = "rejected", "after window end"
                out.append(ExEvent(t_ms, o.id, "rejected", None, "after window end"))
                continue
            o.status, o.reason = "cancelled", "never arrived"
            out.append(ExEvent(t_ms, o.id, "cancelled", None, "never arrived"))
        self._held.clear()
        self._arrivals.clear()
        self._cancels.clear()
        return out

    def _expire_open(self, t: int, out: list[ExEvent]) -> None:
        left = [o for lv in self._book.values() for q in lv.values() for o in q]
        for o in sorted(left, key=lambda o: (o.arrival_ms, o.id)):
            self._remove(o)
            o.status, o.reason = "cancelled", "window end"
            out.append(ExEvent(t, o.id, "expired", None, "window end"))

    # ------------------------------------------------------------------------------------ arrivals
    def _reject_reason(self, o: Order, t: int) -> str:
        info = self.md.info
        if o.kind not in ("limit", "take"):
            return "bad kind"
        if o.kind == "take" and o.post_only:
            return "post-only take"
        if t >= info.window_end_ms:
            return "after window end"
        tick = self.md.tick_at(t, "ts")
        if int(o.px) != o.px or o.px % tick:
            return "off tick"
        if not o.size >= info.min_order_size:
            return "size below minimum"
        if not tick <= o.px <= PX_SCALE - tick:
            return "price out of range"
        if o.post_only:
            bids, asks = self.md.book_at(t, "ts", depth=1)  # the raw recording, impact not removed
            if o.up_side == "BUY" and asks and asks[0][0] <= o.up_px:
                return "would cross"
            if o.up_side == "SELL" and bids and bids[0][0] >= o.up_px:
                return "would cross"
        return ""

    def _on_arrival(self, o: Order, out: list[ExEvent]) -> None:
        t = o.arrival_ms
        reason = self._reject_reason(o, t)
        if reason:
            o.status, o.reason = "rejected", reason
            out.append(ExEvent(t, o.id, "rejected", None, reason))
            return
        if self.cfg.taker_delay_ms and not o.post_only and self._crosses(o, t):
            # Polymarket's taker delay: the marketable order stays pending (not cancellable) for the hold, then is
            # re-validated and matched against the book of that time
            self._held.add(o.id)
            heapq.heappush(self._arrivals, (t + int(self.cfg.taker_delay_ms), self._next_seq(), o))
            return
        self._execute(o, t, out)

    def _on_release(self, o: Order, t: int, out: list[ExEvent]) -> None:
        """The end of a taker hold: validated again, then matched (or rested) as if it arrived now."""
        self._held.discard(o.id)
        reason = self._reject_reason(o, t)
        if reason:
            o.status, o.reason = "rejected", reason
            out.append(ExEvent(t, o.id, "rejected", None, reason))
            return
        self._execute(o, t, out)

    def _crosses(self, o: Order, t: int) -> bool:
        """Marketable against the recorded top of book at t (the same test `_take` starts with)."""
        bids, asks = self.md.book_at(t, "ts", depth=1)
        if o.up_side == "BUY":
            return bool(asks) and asks[0][0] <= o.up_px
        return bool(bids) and bids[0][0] >= o.up_px

    def _execute(self, o: Order, t: int, out: list[ExEvent]) -> None:
        o.status = "open"
        out.append(ExEvent(t, o.id, "accepted"))
        if not o.post_only:
            self._take(o, t, out)
        if o.status == "filled":
            return
        if o.kind == "take":
            o.status, o.reason = "cancelled", "ioc remainder"
            out.append(ExEvent(t, o.id, "cancelled", None, "ioc remainder"))
            return
        self._join(o, t)

    def _join(self, o: Order, t: int) -> None:
        key = (o.up_side, o.up_px)
        if self.cfg.cancel_model == "proportional" and key in self._lvl:
            self._apply_cancels(key, t)
        ahead = self.md.size_at(key[0], key[1], t, "ts")
        q = self._book[key[0]].setdefault(key[1], [])
        if q:  # never ahead of our own earlier orders at this level
            ahead = max(ahead, max(x.queue_ahead for x in q))
        o.queue_ahead = ahead
        o.book_ms = t
        q.append(o)
        self._n_open += 1
        self._lvl.setdefault(key, _LevelState(t))

    def _remove(self, o: Order) -> None:
        lv = self._book[o.up_side]
        q = lv[o.up_px]
        q.remove(o)
        self._n_open -= 1
        if not q:
            del lv[o.up_px]
            self._lvl.pop((o.up_side, o.up_px), None)

    def _on_cancel(self, oid: int, t: int, out: list[ExEvent]) -> None:
        o = self.orders.get(oid)
        if o is None or o.status != "open":
            why = "unknown order" if o is None else "taker delay" if oid in self._held else o.status
            out.append(ExEvent(t, oid, "cancel_rejected", None, why))
            return
        self._remove(o)
        o.status, o.reason = "cancelled", "cancel request"
        out.append(ExEvent(t, oid, "cancelled", None, "cancel request"))

    # ------------------------------------------------------------------------------------ fills
    def _fill(self, o: Order, ts: int, up_px: int, q: float, liq: str, trade_ts: int | None, fee: float,
              out: list[ExEvent]) -> None:
        px = from_up(o.token, o.up_side, up_px)[1]
        o.filled += q
        o.cost += px / PX_SCALE * q
        o.fee += fee
        out.append(ExEvent(ts, o.id, "fill", Fill(o.id, ts, px, q, fee, o.queue_ahead, liq, trade_ts)))
        if o.remaining <= EPS:
            o.filled = o.size
            if o.status == "open" and o in self._book[o.up_side].get(o.up_px, ()):
                self._remove(o)
            o.status = "filled"

    def _take(self, o: Order, t: int, out: list[ExEvent]) -> None:
        """Walk the recorded book at arrival (minus our impact) up to the limit."""
        opp: Side = "SELL" if o.up_side == "BUY" else "BUY"
        bids, asks = self.md.book_at(t, "ts", depth=1)  # most limits don't cross: check the top first
        top = asks if opp == "SELL" else bids
        if not top or ((top[0][0] > o.up_px) if opp == "SELL" else (top[0][0] < o.up_px)):
            return
        bids, asks = self.md.book_at(t, "ts")
        took: list[tuple[int, float]] = []
        rem = o.remaining
        for px, size in (asks if opp == "SELL" else bids):
            if (px > o.up_px) if opp == "SELL" else (px < o.up_px):
                break
            avail = size - self._impact_at(opp, px, t)
            if avail <= EPS:
                continue
            q = min(avail, rem)
            e = self._impact.setdefault((opp, px), [0.0, t])
            e[0] += q
            e[1] = t
            took.append((px, q))
            rem -= q
            if rem <= EPS:
                break
        if not took:
            return
        shares = sum(q for _, q in took)
        notional = sum(from_up(o.token, o.up_side, px)[1] * q for px, q in took) / PX_SCALE
        # the average price rounded to 9 decimals: float noise (2.35 / 5 = 0.47000000000000003) must not
        # flip the fee's 5-decimal rounding on a tie
        fee = taker_fee(shares, round(notional / shares, 9), self.md.info.fee_rate, self.md.info.fee_exponent)
        for i, (px, q) in enumerate(took):
            self._fill(o, t, px, q, "taker", None, fee if i == len(took) - 1 else 0.0, out)

    def _impact_at(self, side: str, px: int, t: int) -> float:
        e = self._impact.get((side, px))
        if e is None:
            return 0.0
        amt, last = e
        if t - last > self.cfg.impact_reset_ms or (self.md.level_changes(side, px, last, t)[2] <= 0).any():
            del self._impact[(side, px)]
            return 0.0
        return amt

    # ------------------------------------------------------------------------------------ trades
    def _on_trade(self, i: int, out: list[ExEvent]) -> None:
        tr = self.md.trades
        rest: Side = "SELL" if tr.up_side[i] == 0 else "BUY"  # a taker BUY (0) hits resting SELLs
        lv = self._book[rest]
        if not lv:
            return
        ts, P, x = int(tr.ts_ms[i]), int(tr.up_px[i]), float(tr.size[i])
        if ts >= self._end:  # unreachable (orders expire first); never match after the window
            return
        echo = self.cfg.trade_echo_ms
        for px in sorted(lv, reverse=(rest == "BUY")):  # price priority for the taker
            if px == P:
                if self.cfg.cancel_model == "proportional":
                    self._apply_cancels((rest, px), ts)
                xp = None  # the at-price volume: min(x, recorded level decrease), computed when first needed
                for o in list(lv.get(px, ())):
                    if ts <= o.book_ms + echo:
                        continue
                    if xp is None:
                        xp = min(x, self._level_decrease(rest, px, ts - echo, ts))
                    d = min(o.queue_ahead, xp)
                    o.queue_ahead -= d
                    q = min(o.remaining, xp - d)
                    if q > EPS:
                        x -= q
                        xp -= q
                        self._fill(o, ts, px, q, "maker", ts, 0.0, out)
            elif (px < P) if rest == "SELL" else (px > P):
                # Trade-through: the volume left after the better levels (B) and our queue (Q) were used up;
                # x already excludes our own earlier fills (deliberately pessimistic).
                better = None  # B: what the taker took at levels strictly better than px, computed when needed
                for o in list(lv.get(px, ())):
                    if ts <= o.book_ms + echo:
                        continue
                    if better is None:
                        better = self._better_decrease(rest, px, ts - echo, ts)
                    q = min(o.remaining, self._through_volume(x, better, o.queue_ahead))
                    if q > EPS:
                        x -= q
                        self._fill(o, ts, px, q, "maker", ts, 0.0, out)
            else:
                break  # worse than P for the taker: untouched, and so is everything after

    def _better_decrease(self, side: str, px: int, t0: int, t1: int) -> float:
        """B: the summed `_level_decrease` of every recorded level of `side` strictly better than px for the taker."""
        return sum(self._level_decrease(side, p, t0, t1) for p in self.md.prices(side)
                   if ((p < px) if side == "SELL" else (p > px)))

    @staticmethod
    def _through_volume(x: float, better: float, ahead: float) -> float:
        """Trade-through volume that reaches our order: what is left after B and the queue ahead of us."""
        return max(0.0, x - better - ahead)

    def _level_decrease(self, side: str, px: int, t0: int, t1: int) -> float:
        """Sum over the ms t in [t0, t1] of max(0, size at t - 1 - size at t) of level (side, px), ts clock."""
        ts_arr, _, sizes = self.md.level_changes(side, px, t0 - 1, t1)
        if not len(ts_arr):
            return 0.0
        last = np.r_[ts_arr[1:] != ts_arr[:-1], True]  # the state at the end of each ms
        s = sizes[last]
        prev = np.r_[self.md.size_at(side, px, t0 - 1, "ts"), s[:-1]]
        return float(np.clip(prev - s, 0.0, None).sum())

    def _apply_cancels(self, key: tuple[str, int], t: int) -> None:
        """Proportional model: apply the level's unexplained decreases in (state.t, t] to our queue."""
        st = self._lvl[key]
        if t <= st.t:
            return
        side, px = key
        ts_arr, _, sizes = self.md.level_changes(side, px, st.t, t)
        prev = self.md.size_at(side, px, st.t, "ts")
        q = self._book[side].get(px, [])
        for tc, s in zip(ts_arr.tolist(), sizes.tolist()):
            if s < prev:
                dec = prev - s
                unexplained = dec - self._explained(side, px, tc, dec, st.used)
                if unexplained > EPS:
                    for o in q:
                        if o.book_ms < tc:
                            o.queue_ahead = max(0.0, o.queue_ahead - min(unexplained, unexplained * o.queue_ahead / prev))
            prev = s
        st.t = t

    def _explained(self, side: str, px: int, tc: int, dec: float, used: dict[int, float]) -> float:
        """Recorded trade volume consuming this level within [tc, tc + trade_explain_ms], not yet used."""
        taker = 1 if side == "BUY" else 0  # bids are consumed by taker SELLs
        tr = self.md.trades
        a = int(np.searchsorted(tr.ts_ms, tc, side="left"))
        b = int(np.searchsorted(tr.ts_ms, tc + self.cfg.trade_explain_ms, side="right"))
        got = 0.0
        for j in range(a, b):
            if tr.up_side[j] == taker and tr.up_px[j] == px:
                avail = float(tr.size[j]) - used.get(j, 0.0)
                if avail > EPS:
                    take = min(avail, dec - got)
                    used[j] = used.get(j, 0.0) + take
                    got += take
                    if got >= dec - EPS:
                        break
        return got


__all__ = ["Exchange", "ExchangeConfig", "ExEvent", "Fill", "Order", "SIDES"]

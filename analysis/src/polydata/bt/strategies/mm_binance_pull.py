"""`mm-binance-pull` (family `market-making`): the backtester's baseline market maker.

The calibration study (docs/research/calibration-study.md, sections 3-4) found that Polymarket makers
earn the spread on quiet flow and lose it to takers who trade in the direction Binance just moved.
This strategy quotes around the mid and steps back whenever Binance moves.

**Quotes.** One bid and one ask per market (in Up terms), `size` shares each:

- centre = the Up mid (`center: mid`) or `ctx.fair_value` (`center: fair`)
- bid = centre - half_spread - skew_per_share x inventory, rounded down to the tick
- ask = centre + half_spread - skew_per_share x inventory, rounded up to the tick
- post-only, so clipped to bid <= best ask - tick and ask >= best bid + tick
- inventory = Up shares - Down shares held in the market; a side whose fill would take |inventory|
  beyond `max_inventory` is not quoted
- the ask is a SELL Up when at least `size` Up is held, else a BUY Down at 1 - ask

A market is quoted only while its book is two-sided with spread <= `max_book_spread`, time left
is in [max(t_min_s, stop_before_end_s), t_max_s] and, unless `quote_before_start`, its window has
started (before the start the strike is unknown, so the Binance link does not hold yet; `t_max_s`
still counts from the end). At the stop time its quotes are cancelled, and so are they whenever the
conditions stop holding.

**Known simplifications.** An ask resting as a BUY Down is not converted to a SELL Up when Up is
later held (it stays until it is filled or replaced; only a new ask picks the form). The inventory
cap uses the reported position, so with fills still in their report latency (and the other side
resting) |inventory| can overshoot `max_inventory` by up to one `size`.

**Pulls.** On every Binance step: if |ln(mid / mid pull_window_ms ago)| > pull_k x sigma_1s x
sqrt(pull_window_ms / 1000), all quotes on that coin's markets are cancelled and quoting pauses for
`cooldown_ms` (a new move restarts the pause). sigma_1s is the realised volatility of 1 s Binance
mids over the last 15 min (900 returns: the last mid received in each second, forward-filled),
computed here from `on_binance`. A coin is not quoted until it has 15 min of history.

**Churn.** A resting quote is replaced (cancel, then place once the cancel is reported) only when its
target moved >= `requote_threshold_ticks` ticks. Requotes happen on book changes, after a fill or a
cancel report, when a pause ends and, with `center: fair`, every 100 ms of Binance. A side never has
a second order while a cancel for it is in flight. Rejected orders are re-placed on the next book
change (a rejection reported in the same ms could otherwise loop).

Everything comes from `ctx` and the handler arguments: nothing about the future.
"""

from __future__ import annotations

import math
from collections import deque

from ..strategy import Ctx, OrderView, Strategy, register
from ..types import Bbo, MarketInfo

VOL_WINDOW_S = 900  # 15 min of 1 s returns
MAX_BN_GAP_S = 60  # a longer silence restarts the volatility history
FAIR_REQUOTE_MS = 100  # with center = fair: requote a coin's markets at most this often on Binance steps


class _Vol:
    """Per symbol: 1 s realised volatility over the last 15 min, and the pull detector."""

    __slots__ = ("sec", "last", "sec_mid", "r2", "sum", "pushes", "win", "hi", "lo")

    def __init__(self):
        self.sec: int | None = None  # the current (incomplete) second
        self.last = 0.0  # latest mid
        self.sec_mid: float | None = None  # mid at the end of the last complete second
        self.r2: deque[float] = deque()
        self.sum = 0.0
        self.pushes = 0
        self.win: deque[tuple[int, float]] = deque()  # (recv ms, mid) covering the pull window
        self.hi = self.lo = 1.0  # pull when mid / ref > hi or < lo

    @property
    def ready(self) -> bool:
        return len(self.r2) >= VOL_WINDOW_S

    def _push(self, r2: float) -> None:
        q = self.r2
        if len(q) == VOL_WINDOW_S:
            self.sum -= q.popleft()
        q.append(r2)
        self.sum += r2
        self.pushes += 1
        if self.pushes % VOL_WINDOW_S == 0:
            self.sum = math.fsum(q)  # no drift from the running sum

    def _reset(self) -> None:
        self.sec_mid = None
        self.r2.clear()
        self.sum = 0.0
        self.win.clear()

    def update(self, t: int, mid: float, k: float, window_ms: int) -> bool:
        """Add a mid received at t; True when it completes a second (the thresholds were refreshed)."""
        sec = t // 1000
        rolled = False
        if self.sec is None:
            self.sec = sec
        elif sec != self.sec:
            gap = sec - self.sec
            if gap > MAX_BN_GAP_S:
                self._reset()
            else:
                if self.sec_mid is not None:
                    self._push(math.log(self.last / self.sec_mid) ** 2)
                for _ in range(min(gap - 1, VOL_WINDOW_S)):  # silent seconds: forward-filled, zero return
                    self._push(0.0)
            self.sec_mid = self.last
            self.sec = sec
            rolled = True
            if self.ready:
                x = k * math.sqrt(max(self.sum, 0.0) / VOL_WINDOW_S) * math.sqrt(window_ms / 1000)
                self.hi, self.lo = math.exp(x), math.exp(-x)
        self.last = mid
        w = self.win
        w.append((t, mid))
        cut = t - window_ms
        while len(w) > 1 and w[1][0] <= cut:
            w.popleft()
        return rolled

    def moved(self, t: int, mid: float, window_ms: int) -> bool:
        """|ln(mid / mid at t - window)| above the threshold (the reference is the last mid received
        at or before t - window)."""
        if not self.ready:
            return False
        t0, ref = self.win[0]
        if t0 > t - window_ms:
            return False
        r = mid / ref
        return r > self.hi or r < self.lo


class _Side:
    __slots__ = ("oid", "px", "cancelling")

    def __init__(self, oid: int, px: int):
        self.oid, self.px, self.cancelling = oid, px, False


class _Mkt:
    __slots__ = ("info", "bid", "ask", "stopped")

    def __init__(self, info: MarketInfo):
        self.info = info
        self.bid: _Side | None = None
        self.ask: _Side | None = None
        self.stopped = False


@register
class MmBinancePull(Strategy):
    name = "mm-binance-pull"
    family = "market-making"
    default_params = {
        "half_spread": 0.01,
        "size": 10,
        "max_book_spread": 0.04,
        "t_min_s": 30,
        "t_max_s": 3600,
        "pull_k": 2.0,
        "pull_window_ms": 1000,
        "cooldown_ms": 2000,
        "skew_per_share": 0.0005,
        "max_inventory": 50,
        "stop_before_end_s": 20,
        "center": "mid",
        "requote_threshold_ticks": 1,
        "quote_before_start": False,
    }

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        p = self.params
        if p["center"] not in ("mid", "fair"):
            raise ValueError(f"center must be mid or fair, not {p['center']!r}")
        self.size = float(p["size"])
        self.hs_px = float(p["half_spread"]) * 1000
        self.skew_px = float(p["skew_per_share"]) * 1000
        self.max_inv = float(p["max_inventory"])
        self.max_spread_px = int(round(float(p["max_book_spread"]) * 1000))
        self.stop_ms = int(max(p["t_min_s"], p["stop_before_end_s"]) * 1000)
        self.start_ms = int(p["t_max_s"] * 1000)
        self.k = float(p["pull_k"])
        self.win_ms = int(p["pull_window_ms"])
        self.cool_ms = int(p["cooldown_ms"])
        self.thr = int(p["requote_threshold_ticks"])
        self.fair = p["center"] == "fair"
        self.pre_start = bool(p["quote_before_start"])
        self.mkts: dict[str, _Mkt] = {}
        self.by_coin: dict[str, list[_Mkt]] = {}
        self.vol: dict[str, _Vol] = {}  # by coin
        self.paused_until: dict[str, int] = {}
        self.last_fair_quote: dict[str, int] = {}
        self.side_of: dict[int, tuple[str, str]] = {}  # order id -> (slug, "bid"/"ask")
        self.coin_of: dict[str, str] = {}  # Binance symbol -> coin

    # ------------------------------------------------------------------------------- handlers
    def on_market_open(self, ctx: Ctx, market: MarketInfo) -> None:
        m = _Mkt(market)
        self.mkts[market.slug] = m
        self.by_coin.setdefault(market.coin, []).append(m)
        end, now = market.window_end_ms, ctx.now
        start = end - self.start_ms if self.pre_start else max(end - self.start_ms, market.window_start_ms)
        if start > now:
            ctx.set_timer(f"start:{market.slug}", start - now)
        if end - self.stop_ms > now:
            ctx.set_timer(f"stop:{market.slug}", end - self.stop_ms - now)
        else:
            m.stopped = True
        self._quote(ctx, m)

    def on_market_close(self, ctx: Ctx, market: MarketInfo) -> None:
        m = self.mkts.pop(market.slug, None)
        if m is not None:
            self.by_coin[market.coin].remove(m)

    def on_book(self, ctx: Ctx, market: MarketInfo, book) -> None:
        m = self.mkts.get(market.slug)
        if m is not None:
            self._quote(ctx, m)

    def on_binance(self, ctx: Ctx, sym: str, bbo: Bbo) -> None:
        coin = self.coin_of.get(sym)
        if coin is None:
            coin = self.coin_of[sym] = sym.removesuffix("USDT").lower()
        v = self.vol.get(coin)
        if v is None:
            v = self.vol[coin] = _Vol()
        t, mid = bbo.recv_ms, (bbo.bid + bbo.ask) / 2
        v.update(t, mid, self.k, self.win_ms)
        if v.moved(t, mid, self.win_ms):
            if self.paused_until.get(coin, 0) <= t:
                ctx.log("pull", coin, f"{math.log(mid / v.win[0][1]):+.6f}")
            self.paused_until[coin] = t + self.cool_ms
            ctx.set_timer(f"resume:{coin}", self.cool_ms)
            for m in self.by_coin.get(coin, ()):
                self._cancel_side(ctx, m.bid)
                self._cancel_side(ctx, m.ask)
        elif self.fair and t - self.last_fair_quote.get(coin, -FAIR_REQUOTE_MS) >= FAIR_REQUOTE_MS:
            self.last_fair_quote[coin] = t
            for m in self.by_coin.get(coin, ()):
                self._quote(ctx, m)

    def on_timer(self, ctx: Ctx, name: str) -> None:
        kind, _, key = name.partition(":")
        if kind == "resume":
            for m in list(self.by_coin.get(key, ())):
                self._quote(ctx, m)
            return
        m = self.mkts.get(key)
        if m is None:
            return
        if kind == "stop":
            m.stopped = True
        self._quote(ctx, m)  # "start", or "stop" (cancels everything)

    def on_order(self, ctx: Ctx, order: OrderView) -> None:
        key = self.side_of.get(order.id)
        if key is None:
            return
        slug, side = key
        m = self.mkts.get(slug)
        s = None if m is None else getattr(m, side)
        mine = s is not None and s.oid == order.id
        if order.event == "cancel_rejected":
            if mine:
                s.cancelling = False  # still resting; the next requote may cancel it again
            return
        if order.is_open:  # accepted, or a partial fill
            if order.event == "fill" and m is not None:
                self._quote(ctx, m)  # the inventory (skew) changed
            return
        del self.side_of[order.id]
        if mine:
            setattr(m, side, None)
        if m is not None and order.status in ("filled", "cancelled"):
            self._quote(ctx, m)

    # ------------------------------------------------------------------------------- quoting
    def _cancel_side(self, ctx: Ctx, s: _Side | None) -> None:
        if s is not None and not s.cancelling:
            s.cancelling = True
            ctx.cancel(s.oid)

    def _targets(self, ctx: Ctx, m: _Mkt) -> tuple[int | None, int | None]:
        """(bid, ask) in Up px, None for a side not to quote."""
        info, now = m.info, ctx.now
        if m.stopped:
            return None, None
        left = info.window_end_ms - now
        if left < self.stop_ms or left > self.start_ms or (now < info.window_start_ms and not self.pre_start):
            return None, None
        v = self.vol.get(info.coin)
        if v is None or not v.ready or self.paused_until.get(info.coin, 0) > now:
            return None, None
        top = ctx.top(info.slug)
        if top is None:
            return None, None
        bb, ba = top.bid_px, top.ask_px
        if bb <= 0 or ba >= 1000 or ba - bb > self.max_spread_px:
            return None, None
        if self.fair:
            f = ctx.fair_value(info.slug)
            if f is None:
                return None, None
            c = f * 1000
        else:
            c = (bb + ba) / 2
        up, down = ctx.position(info.slug)
        inv = up - down
        c -= self.skew_px * inv
        tick = info.tick_px
        bid = math.floor((c - self.hs_px) / tick + 1e-9) * tick
        ask = math.ceil((c + self.hs_px) / tick - 1e-9) * tick
        bid = min(bid, ba - tick)
        ask = max(ask, bb + tick)
        if ask <= bid:  # half_spread 0 on a mid that sits on the tick
            ask = bid + tick
        if bid < tick or inv + self.size > self.max_inv:
            bid = None
        if ask > 1000 - tick or inv - self.size < -self.max_inv:
            ask = None
        return bid, ask

    def _quote(self, ctx: Ctx, m: _Mkt) -> None:
        bid, ask = self._targets(ctx, m)
        tol = self.thr * m.info.tick_px
        for side, target in (("bid", bid), ("ask", ask)):
            s = getattr(m, side)
            if s is not None:
                if s.cancelling:
                    continue  # wait for the cancel's report
                if target is None or abs(target - s.px) >= tol:
                    self._cancel_side(ctx, s)
                continue
            if target is None:
                continue
            slug = m.info.slug
            if side == "bid":
                oid = ctx.place(slug, "Up", "BUY", target / 1000, self.size)
            elif ctx.position(slug)[0] >= self.size:
                oid = ctx.place(slug, "Up", "SELL", target / 1000, self.size)
            else:
                oid = ctx.place(slug, "Down", "BUY", (1000 - target) / 1000, self.size)
            setattr(m, side, _Side(oid, target))
            self.side_of[oid] = (slug, side)


__all__ = ["MmBinancePull"]

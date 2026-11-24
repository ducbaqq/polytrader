"""`mm-binance-pull` on synthetic inputs: the strategy driven through a fake ctx (exact control over
books, Binance, positions and order reports), plus one end-to-end run through the engine."""

from __future__ import annotations

import math

import pytest
from test_engine import NO_EVENTS, T0, cfg, make_con, minfo

from polydata.bt import engine
from polydata.bt.strategy import OrderView, get_strategy
from polydata.bt.types import Bbo, Top

SYM = "BTCUSDT"
BASE = {"btc": 60000.0, "eth": 3000.0}
HIST_S = 902  # 900 one-second returns need 901 complete seconds, plus the current one


class FakeCtx:
    """The parts of `Ctx` the strategy uses. Orders stay pending until the test reports on them."""

    def __init__(self, markets):
        self.now = 0
        self._markets = {m.slug: m for m in markets}
        self.tops: dict[str, Top] = {}
        self.pos: dict[str, tuple[float, float]] = {}
        self.fair: dict[str, float] = {}
        self.views: dict[int, OrderView] = {}
        self.placed: list[OrderView] = []
        self.cancels: list[int] = []
        self.timers: dict[str, int] = {}
        self.logs: list[str] = []
        self._oid = 0

    # actions
    def place(self, slug, token, side, price, size, post_only=True):
        self._oid += 1
        v = OrderView(self._oid, slug, token, side, price, size, "limit", post_only, self.now)
        self.views[v.id] = v
        self.placed.append(v)
        return v.id

    def take(self, *a, **k):
        raise AssertionError("mm-binance-pull never takes")

    def cancel(self, oid):
        self.cancels.append(oid)

    def cancel_all(self, slug=None):
        raise AssertionError("not used")

    def set_timer(self, name, ms):
        self.timers[name] = self.now + ms

    def log(self, *args):
        self.logs.append(" ".join(map(str, args)))

    # queries
    def top(self, slug):
        return self.tops.get(slug)

    def book(self, slug, depth=5):
        raise AssertionError("not used")

    def position(self, slug):
        return self.pos.get(slug, (0.0, 0.0))

    def cash(self, slug=None):
        return 1000.0

    def orders(self, slug=None):
        return [v for v in self.views.values() if v.is_open and (slug is None or v.slug == slug)]

    def markets(self):
        return list(self._markets.values())

    def fair_value(self, slug):
        return self.fair.get(slug)

    def binance(self, sym):
        return None

    def chainlink(self, coin):
        return None


# ------------------------------------------------------------------------------------------ helpers

def make(params=None, markets=None, t_start=None):
    """A strategy with 15 min of quiet Binance history (one update per second, +-1 bp wiggle) and its
    markets opened. Returns (strategy, ctx)."""
    markets = markets or [minfo(T0)]
    s = get_strategy("mm-binance-pull")(params)
    ctx = FakeCtx(markets)
    t0 = (markets[0].window_start_ms if t_start is None else t_start) - HIST_S * 1000
    base = {f"{m.coin.upper()}USDT": BASE.get(m.coin, 100.0) for m in markets}
    for k in range(HIST_S):
        for sym, b in base.items():
            bn(s, ctx, t0 + k * 1000, b * (1.0001 if k % 2 else 1.0), sym=sym)
    for m in markets:
        ctx.tops.setdefault(m.slug, top(480, 520))
        s.on_market_open(ctx, m)
    return s, ctx


def bn(s, ctx, t, mid, sym=SYM):
    ctx.now = t
    s.on_binance(ctx, sym, Bbo(t, sym, mid - 0.05, mid + 0.05, 1.0, 1.0))


def top(bid_px, ask_px, t=0):
    return Top(t, t, bid_px, 100.0, ask_px, 100.0)


def book(s, ctx, m, bid_px, ask_px, t=None):
    if t is not None:
        ctx.now = t
    ctx.tops[m.slug] = top(bid_px, ask_px, ctx.now)
    s.on_book(ctx, m, None)


def reset(s, ctx):
    """Report every open order as cancelled by the exchange (each report may requote)."""
    for v in [v for v in ctx.views.values() if v.is_open]:
        report(s, ctx, v.id, "cancelled")


def report(s, ctx, oid, status, event=None, filled=None):
    v = ctx.views[oid]
    v.status = status
    v.event = event or status
    if filled is not None:
        v.filled = filled
    s.on_order(ctx, v)


def live(ctx):
    """(token, side, price, size) of the orders placed and not reported done."""
    return sorted((v.token, v.side, v.price, v.size) for v in ctx.views.values() if v.is_open)


def fire(s, ctx, name):
    ctx.now = ctx.timers.pop(name)
    s.on_timer(ctx, name)


# ------------------------------------------------------------------------------------------ tests

def test_registered():
    cls = get_strategy("mm-binance-pull")
    assert cls.family == "market-making"
    assert cls.default_params["half_spread"] == 0.01 and cls.default_params["center"] == "mid"


def test_no_quotes_without_15_min_of_binance_history():
    m = minfo(T0)
    s = get_strategy("mm-binance-pull")()
    ctx = FakeCtx([m])
    for k in range(600):  # 10 minutes only
        bn(s, ctx, T0 - 600_000 + k * 1000, 60000.0)
    ctx.tops[m.slug] = top(480, 520)
    s.on_market_open(ctx, m)
    book(s, ctx, m, 480, 520)
    assert ctx.placed == []


def test_quotes_inside_tight_book():
    m = minfo(T0)
    s, ctx = make()
    # mid 0.50: bid 0.49 (Up BUY); ask 0.51 as Down BUY 0.49 (no Up held)
    assert live(ctx) == [("Down", "BUY", 0.49, 10.0), ("Up", "BUY", 0.49, 10.0)]
    assert all(v.post_only for v in ctx.placed)
    # holding Up: the ask is a SELL Up (and the skew moves both quotes down 0.005 -> rounds outward)
    s2, ctx2 = make()
    ctx2.pos[m.slug] = (10.0, 0.0)
    reset(s2, ctx2)
    assert live(ctx2) == [("Up", "BUY", 0.48, 10.0), ("Up", "SELL", 0.51, 10.0)]
    # a one-tick book (0.49/0.50, mid 0.495): rounded outward to 0.48 / 0.51, never inside the target
    s3, ctx3 = make()
    ctx3.tops[m.slug] = top(490, 500)
    reset(s3, ctx3)
    assert live(ctx3) == [("Down", "BUY", 0.49, 10.0), ("Up", "BUY", 0.48, 10.0)]


def test_quotes_never_cross_the_book():
    m = minfo(T0)
    # heavy skew pushes the bid target above the best ask: clipped to ask - tick
    s, ctx = make({"half_spread": 0.005, "skew_per_share": 0.002, "max_inventory": 1000})
    ctx.pos[m.slug] = (0.0, 20.0)  # short Up 20: both targets move up 0.04 -> bid 0.535 > best ask 0.52
    reset(s, ctx)
    assert live(ctx) == [("Down", "BUY", 0.45, 10.0), ("Up", "BUY", 0.51, 10.0)]  # ask 0.545 -> 0.55
    # half_spread 0 on a mid that sits on the tick: bid and ask never meet
    s, ctx = make({"half_spread": 0.0})
    assert live(ctx) == [("Down", "BUY", 0.49, 10.0), ("Up", "BUY", 0.5, 10.0)]


def test_no_quotes_in_wide_book():
    m = minfo(T0)
    s, ctx = make()
    assert len(live(ctx)) == 2
    book(s, ctx, m, 400, 500)  # spread 0.10 > 0.04: pull the quotes, place nothing new
    assert sorted(ctx.cancels) == sorted(v.id for v in ctx.placed)
    for oid in list(ctx.cancels):
        report(s, ctx, oid, "cancelled")
    assert live(ctx) == []
    book(s, ctx, m, 400, 500)
    assert live(ctx) == [] and len(ctx.placed) == 2
    book(s, ctx, m, 480, 520)  # tight again
    assert len(live(ctx)) == 2


def test_one_sided_book_not_quoted():
    m = minfo(T0)
    s, ctx = make()
    book(s, ctx, m, 0, 520)
    assert sorted(ctx.cancels) == sorted(v.id for v in ctx.placed)
    reset(s, ctx)
    assert live(ctx) == [] and len(ctx.placed) == 2


def test_pull_on_binance_jump_then_requote_after_cooldown():
    m = minfo(T0)
    s, ctx = make()
    t = ctx.now
    assert len(live(ctx)) == 2
    # small moves within 2 sigma: nothing happens
    bn(s, ctx, t + 10, 60003.0)
    assert ctx.cancels == []
    # a 0.2% jump within 1 s: everything on the coin is pulled
    bn(s, ctx, t + 500, 60120.0)
    assert sorted(ctx.cancels) == sorted(v.id for v in ctx.placed)
    assert ctx.timers["resume:btc"] == t + 500 + 2000
    for oid in list(ctx.cancels):
        report(s, ctx, oid, "cancelled")
    assert live(ctx) == []  # paused: the cancel reports do not requote
    book(s, ctx, m, 490, 530, t=t + 1000)
    assert live(ctx) == []
    fire(s, ctx, "resume:btc")
    assert ctx.now == t + 2500
    assert live(ctx) == [("Down", "BUY", 0.48, 10.0), ("Up", "BUY", 0.5, 10.0)]


def test_pull_cancels_every_market_of_the_coin_only():
    a, b, e = minfo(T0), minfo(T0, tf="15m"), minfo(T0, coin="eth")
    s, ctx = make(markets=[a, b, e])
    assert len(live(ctx)) == 6
    bn(s, ctx, ctx.now + 200, 60200.0)
    assert set(ctx.cancels) == {v.id for v in ctx.placed if v.slug != e.slug} and len(ctx.cancels) == 4


def test_requote_threshold_and_cancel_in_flight():
    m = minfo(T0)
    s, ctx = make({"requote_threshold_ticks": 2})
    bid = next(v for v in ctx.placed if v.token == "Up")
    book(s, ctx, m, 490, 520)  # mid 0.505 -> bid target 0.49: unchanged
    book(s, ctx, m, 490, 530)  # mid 0.51 -> bid 0.50: one tick, below the threshold
    assert ctx.cancels == []
    book(s, ctx, m, 500, 540)  # mid 0.52 -> bid 0.51: two ticks: cancel
    assert bid.id in ctx.cancels
    n = len(ctx.placed)
    book(s, ctx, m, 510, 550)  # the cancel is in flight: no new bid, no second cancel
    assert len(ctx.placed) == n and ctx.cancels.count(bid.id) == 1
    report(s, ctx, bid.id, "cancelled")
    assert ctx.placed[-1].token == "Up" and ctx.placed[-1].price == 0.52


def test_requote_after_fill():
    m = minfo(T0)
    s, ctx = make()
    bid = next(v for v in ctx.placed if v.token == "Up")
    ctx.pos[m.slug] = (10.0, 0.0)
    report(s, ctx, bid.id, "filled", event="fill", filled=10.0)
    # the new bid is skewed down by 10 x 0.0005 = 0.005 (0.485 -> 0.48)
    assert ctx.placed[-1].token == "Up" and ctx.placed[-1].side == "BUY" and ctx.placed[-1].price == 0.48


def test_inventory_cap_stops_one_side():
    m = minfo(T0)
    s, ctx = make()
    # long 45 Up: another 10 would make 55 > 50, so no bid; the ask is a SELL Up
    ctx.pos[m.slug] = (45.0, 0.0)
    book(s, ctx, m, 480, 520)
    for oid in list(ctx.cancels):
        report(s, ctx, oid, "cancelled")
    assert live(ctx) == [("Up", "SELL", 0.49, 10.0)]  # ask 0.51 - 0.0225 -> 0.49 (>= best bid + tick)
    # short 45 (Down held): no ask, the bid is skewed up
    ctx.pos[m.slug] = (0.0, 45.0)
    book(s, ctx, m, 480, 520)
    for oid in [i for i in ctx.cancels if ctx.views[i].is_open]:
        report(s, ctx, oid, "cancelled")
    assert live(ctx) == [("Up", "BUY", 0.51, 10.0)]
    # short 30: the ask is allowed again
    ctx.pos[m.slug] = (0.0, 30.0)
    book(s, ctx, m, 480, 520)
    assert ("Down", "BUY", 0.47, 10.0) in live(ctx)


def test_inventory_cap_cancels_resting_order():
    m = minfo(T0)
    s, ctx = make()
    bid = next(v for v in ctx.placed if v.token == "Up")
    ctx.pos[m.slug] = (45.0, 0.0)
    book(s, ctx, m, 480, 520)
    assert bid.id in ctx.cancels


def test_stops_before_end():
    m = minfo(T0)  # 5m window
    s, ctx = make()
    stop = m.window_end_ms - 30_000  # max(t_min_s 30, stop_before_end_s 20)
    assert ctx.timers[f"stop:{m.slug}"] == stop
    ids = {v.id for v in ctx.placed}
    fire(s, ctx, f"stop:{m.slug}")
    assert set(ctx.cancels) == ids
    for oid in ids:
        report(s, ctx, oid, "cancelled")
    book(s, ctx, m, 480, 520, t=stop + 1000)
    assert live(ctx) == [] and {v.id for v in ctx.placed} == ids


def test_market_opened_late_never_quotes():
    m = minfo(T0)
    s, ctx = make(t_start=m.window_end_ms - 10_000)  # 10 s left at open
    assert ctx.placed == []


def test_waits_until_t_max():
    m = minfo(T0, tf="1h")
    start = m.window_end_ms - 3_600_000
    s, ctx = make(markets=[m], t_start=start - 60_000)  # opened 61 min before the end
    assert ctx.placed == []
    assert ctx.timers[f"start:{m.slug}"] == start
    book(s, ctx, m, 480, 520)
    assert ctx.placed == []
    for k in range(60):  # Binance keeps ticking up to the start
        bn(s, ctx, start - 60_000 + k * 1000, 60000.0)
    fire(s, ctx, f"start:{m.slug}")
    assert len(live(ctx)) == 2


def test_fair_center():
    m = minfo(T0)
    s, ctx = make({"center": "fair"})
    assert ctx.placed == []  # no fair value yet
    ctx.fair[m.slug] = 0.47
    book(s, ctx, m, 480, 520)
    # bid 0.46; ask 0.48 -> clipped to best bid + tick 0.49, as Down BUY 0.51
    assert live(ctx) == [("Down", "BUY", 0.51, 10.0), ("Up", "BUY", 0.46, 10.0)]


def test_bad_center_rejected():
    with pytest.raises(ValueError):
        get_strategy("mm-binance-pull")({"center": "middle"})


# ------------------------------------------------------------------------------------------ engine

def test_runs_through_engine():
    from test_engine import loader_for, mdata

    m = minfo(T0)
    md = mdata(m, changes=[(T0 + k * 5_000, "BUY", 0.49, 100 if k % 2 else 0, 0.49 if k % 2 else 0.48, 0.52)
                           for k in range(1, 50)],
               trades=[(T0 + 20_000, "Up", "SELL", 0.48, 150), (T0 + 40_000, "Up", "BUY", 0.52, 130)])
    con = lambda: make_con([m], T0 - 1_200_000, T0 + 1_300_000)  # noqa: E731
    c = cfg(strategy="mm-binance-pull", timeframes=["5m"], params={})
    res = engine.run_coin(c, "btc", con, loader=loader_for({m.slug: md}), rec_events=NO_EVENTS)
    o = res.orders
    placed = o[o.event == "placed"]
    assert len(placed) >= 2 and (placed["size"] == 10).all() and placed.post_only.all()
    assert (o.event == "rejected").sum() == 0
    assert (placed.ms <= m.window_end_ms - 30_000).all()
    pnl = res.markets.set_index("slug").pnl[m.slug]
    assert not math.isnan(pnl)

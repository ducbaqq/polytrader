"""`fv-taker` decision logic through a fake ctx (synthetic books, no engine)."""

from __future__ import annotations

from test_engine import T0, minfo

from polydata.bt.strategies.fv_taker import fee_ps, limit_price
from polydata.bt.strategy import BookView, OrderView, get_strategy

M15 = minfo(T0, "15m")  # fee 0.05, exponent 1, tick 0.01, min size 5


class Ctx:
    def __init__(self, m=M15, fair=0.60, bids=((0.45, 50),), asks=((0.50, 30),), tau=600):
        self.m, self.fairv, self.bids, self.asks = m, fair, list(bids), list(asks)
        self.now = m.window_end_ms - tau * 1000
        self.takes, self.timers, self._id = [], {}, 0

    def fair_value(self, slug):
        return self.fairv

    def book(self, slug, depth=5):
        return BookView(self.bids, self.asks)

    def take(self, slug, token, side, size, limit_price):
        self._id += 1
        self.takes.append((token, side, size, limit_price))
        return self._id

    def set_timer(self, name, ms):
        self.timers[name] = self.now + ms


def mk(params=None, **kw):
    s = get_strategy("fv-taker")(params)
    c = Ctx(**kw)
    s.on_market_open(c, c.m)
    return s, c


def test_registered_and_defaults():
    cls = get_strategy("fv-taker")
    assert cls.family == "fair-value"
    assert cls.default_params["margin"] == 0.02 and cls.default_params["max_entries"] == 1


def test_limit_price_fee_aware():
    # fair 0.60, margin 0.02, fee 0.05*p*(1-p) ~ 0.012: limit + fee <= 0.58 -> 0.56
    assert limit_price(0.60, 0.02, 0.01, 0.05, 1.0, True) == 0.56
    assert limit_price(0.60, 0.02, 0.01, 0.05, 1.0, False) == 0.58
    assert limit_price(0.01, 0.02, 0.01, 0.05, 1.0, True) is None
    p = limit_price(0.60, 0.02, 0.01, 0.05, 1.0, True)
    assert p + fee_ps(p, 0.05, 1.0) <= 0.58


def test_takes_up_when_ask_below_limit_sized_to_depth():
    s, c = mk()  # ask 0.50 <= 0.56; 30 visible, size 10
    s.on_book(c, c.m, None)
    assert c.takes == [("Up", "BUY", 10.0, 0.56)]
    s2, c2 = mk(asks=((0.50, 7), (0.55, 100), (0.58, 100)))  # only levels <= 0.56 count: 107, capped at 10
    s2.on_book(c2, c2.m, None)
    assert c2.takes[0][2] == 10.0
    s3, c3 = mk(asks=((0.50, 7), (0.58, 100)))  # 7 visible at the limit
    s3.on_book(c3, c3.m, None)
    assert c3.takes[0][2] == 7.0


def test_thin_ask_below_min_size_skips():
    s, c = mk(asks=((0.50, 4),))
    s.on_book(c, c.m, None)
    assert c.takes == []


def test_ask_above_limit_no_trade():
    s, c = mk(asks=((0.57, 100),))
    s.on_book(c, c.m, None)
    assert c.takes == []


def test_takes_down_when_fair_low():
    # fair Up 0.40 -> Down 0.60; Up bids 0.45/asks 0.52 -> Down asks at 1-0.45 = 0.55 (not <= 0.56? yes) take Down
    s, c = mk(fair=0.40, bids=((0.45, 50),), asks=((0.52, 50),))
    s.on_book(c, c.m, None)
    assert c.takes == [("Down", "BUY", 10.0, 0.56)]


def test_spread_filter():
    s, c = mk(bids=((0.30, 50),), asks=((0.50, 50),))  # spread 0.20 > 0.10
    s.on_book(c, c.m, None)
    assert c.takes == []


def test_time_band():
    for tau in (200, 1500):
        s, c = mk(tau=tau)
        s.on_book(c, c.m, None)
        assert c.takes == [], tau
    s, c = mk(tau=1200)
    s.on_book(c, c.m, None)
    assert len(c.takes) == 1


def test_timeframes_filter_and_no_fair():
    s, c = mk(m=minfo(T0, "5m"))
    s.on_book(c, c.m, None)
    assert c.takes == []  # 5m not tradable by default
    s, c = mk(params={"timeframes": ["5m", "15m"]}, m=minfo(T0, "5m"), tau=250)
    s.params["min_tau_s"] = 100
    s.on_book(c, c.m, None)
    assert len(c.takes) == 1
    s, c = mk(fair=None)
    s.on_book(c, c.m, None)
    assert c.takes == []


def test_one_entry_per_market_and_retry_on_no_fill():
    s, c = mk()
    s.on_book(c, c.m, None)
    s.on_book(c, c.m, None)
    assert len(c.takes) == 1  # pending order blocks a second
    v = OrderView(1, c.m.slug, "Up", "BUY", 0.56, 10.0, "take", False, c.now, status="cancelled", filled=0.0)
    s.on_order(c, v)  # nothing filled: may retry after retry_ms
    s.on_book(c, c.m, None)
    assert len(c.takes) == 1
    c.now += 1000
    s.on_book(c, c.m, None)
    assert len(c.takes) == 2
    v2 = OrderView(2, c.m.slug, "Up", "BUY", 0.56, 10.0, "take", False, c.now, status="filled", filled=10.0)
    s.on_order(c, v2)  # entered: done
    c.now += 5000
    s.on_book(c, c.m, None)
    assert len(c.takes) == 2


def test_max_entries_two():
    s, c = mk({"max_entries": 2})
    for i in (1, 2):
        s.on_book(c, c.m, None)
        s.on_order(c, OrderView(i, c.m.slug, "Up", "BUY", 0.56, 10.0, "take", False, c.now, status="filled", filled=10.0))
        c.now += 1000
    s.on_book(c, c.m, None)
    assert len(c.takes) == 2


def test_timer_polls_then_stops():
    s, c = mk()
    c.now = c.m.window_end_ms - 600_000
    s.on_timer(c, f"chk:{c.m.slug}")
    assert len(c.takes) == 1 and c.timers  # decided and rescheduled
    c.timers.clear()
    c.now = c.m.window_end_ms - 250_000
    s.on_timer(c, f"chk:{c.m.slug}")
    assert not c.timers  # below min_tau: stops polling

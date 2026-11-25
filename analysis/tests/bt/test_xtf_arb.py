"""`xtf-arb` decision logic through a fake ctx (synthetic books, no engine)."""

from __future__ import annotations

from dataclasses import replace

from test_engine import T0, minfo

from polydata.bt.strategies.xtf_arb import fee_ps, walk
from polydata.bt.strategy import BookView, OrderView, get_strategy

# 5m and 15m ending at the same time; ETH-like fee 0.01
M15 = replace(minfo(T0, "15m", outcome=None, coin="eth", price_to_beat=3000.0), fee_rate=0.01)
M5 = replace(minfo(T0 + 600_000, "5m", outcome=None, coin="eth", price_to_beat=3001.0), fee_rate=0.01)
assert M5.window_end_ms == M15.window_end_ms


class Ctx:
    def __init__(self, books):
        self.books = books  # slug -> (bids, asks) in Up terms
        self.now = M5.window_end_ms - 120_000
        self.takes, self._id, self.logs = [], 0, []

    def book(self, slug, depth=5):
        b, a = self.books[slug]
        return BookView(list(b), list(a))

    def take(self, slug, token, side, size, limit_price):
        self._id += 1
        self.takes.append((slug, token, side, size, limit_price))
        return self._id

    def log(self, *a):
        self.logs.append(a)


def mk(books, params=None):
    s = get_strategy("xtf-arb")(params)
    c = Ctx(books)
    for m in (M15, M5):
        s.on_market_open(c, m)
    return s, c


F = lambda p: fee_ps(p, 0.01, 1.0)  # noqa: E731


def test_registered_and_defaults():
    cls = get_strategy("xtf-arb")
    assert cls.family == "cross-timeframe"
    assert cls.default_params["margin"] == 0.0 and cls.default_params["max_spread"] == 0.10


def test_walk_stops_at_cost_one():
    # Up_A asks 0.40 (10), 0.45 (10); Up_B bids 0.50 (5), 0.43 (100)
    n, wa, wb, first = walk([(0.40, 10), (0.45, 10)], [(0.50, 5), (0.43, 100)], F, F, 0.0, 100)
    # pair 1: 0.40 + 0.50 + fees < 1 (5); pair 2: 0.40 + 0.57 + fees = 0.97 + ~0.0049 < 1 (5);
    # pair 3: 0.45 + 0.57 + fees > 1 -> stop
    assert n == 10 and wa == 0.40 and wb == 0.43
    assert abs(first - (0.90 + F(0.40) + F(0.50))) < 1e-12
    assert walk([(0.50, 10)], [(0.49, 10)], F, F, 0.0, 100) is None  # gross 1.01


def test_walk_caps_size():
    n, *_ = walk([(0.40, 100)], [(0.50, 100)], F, F, 0.0, 30)
    assert n == 30


def test_buys_up_lower_strike_and_down_higher_strike():
    # A = 15m (K 3000, lower), B = 5m (K 3001). Up_A ask 0.40, Up_B bid 0.45 -> cost 0.95 + fees
    books = {M15.slug: ([(0.35, 50)], [(0.40, 20)]), M5.slug: ([(0.45, 8)], [(0.50, 50)])}
    s, c = mk(books)
    s.on_book(c, M5, None)
    assert c.takes == [(M15.slug, "Up", "BUY", 8.0, 0.40), (M5.slug, "Down", "BUY", 8.0, 0.55)]


def test_no_trade_when_monotone():
    books = {M15.slug: ([(0.50, 50)], [(0.52, 20)]), M5.slug: ([(0.45, 50)], [(0.47, 50)])}
    s, c = mk(books)
    s.on_book(c, M5, None)
    assert c.takes == []


def test_direction_follows_strike_not_timeframe():
    # Same crossed prices but the 5m has the LOWER strike now: Up_5m ask must be below Up_15m bid
    m5 = replace(M5, price_to_beat=2999.0)
    books = {M15.slug: ([(0.45, 8)], [(0.50, 50)]), m5.slug: ([(0.35, 50)], [(0.40, 20)])}
    s, c = mk(books)
    s.on_book(c, m5, None)
    assert c.takes == [(m5.slug, "Up", "BUY", 8.0, 0.40), (M15.slug, "Down", "BUY", 8.0, 0.55)]


def test_wide_book_thin_size_unknown_strike_skip():
    wide = {M15.slug: ([(0.25, 50)], [(0.40, 20)]), M5.slug: ([(0.45, 8)], [(0.50, 50)])}
    s, c = mk(wide)
    s.on_book(c, M5, None)
    assert c.takes == []
    thin = {M15.slug: ([(0.35, 50)], [(0.40, 4)]), M5.slug: ([(0.45, 8)], [(0.50, 50)])}
    s, c = mk(thin)
    s.on_book(c, M5, None)
    assert c.takes == []
    ok = {M15.slug: ([(0.35, 50)], [(0.40, 20)]), M5.slug: ([(0.45, 8)], [(0.50, 50)])}
    s, c = mk(ok)
    s.on_book(c, replace(M5, price_to_beat=None), None)
    assert c.takes == []


def test_one_pair_in_flight_then_logs_and_cooldown():
    books = {M15.slug: ([(0.35, 50)], [(0.40, 20)]), M5.slug: ([(0.45, 8)], [(0.50, 50)])}
    s, c = mk(books, {"retry_ms": 1000})
    s.on_book(c, M5, None)
    s.on_book(c, M5, None)
    assert len(c.takes) == 2  # still in flight: no second pair
    oa = OrderView(1, M15.slug, "Up", "BUY", 0.40, 8.0, "take", False, c.now, status="filled", filled=8.0,
                   notional=3.2, event="fill")
    ob = OrderView(2, M5.slug, "Down", "BUY", 0.55, 8.0, "take", False, c.now, status="expired", filled=0.0,
                   event="expired")
    s.on_order(c, oa)
    assert not c.logs
    s.on_order(c, ob)
    assert c.logs and c.logs[0][0] == "pair" and c.logs[0][3] == 8.0 and c.logs[0][6] == 0.0
    s.on_book(c, M5, None)
    assert len(c.takes) == 2  # cooldown
    c.now += 1000
    s.on_book(c, M5, None)
    assert len(c.takes) == 4

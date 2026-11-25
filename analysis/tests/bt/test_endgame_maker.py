"""`endgame-maker` decision logic through a fake ctx (synthetic books, no engine)."""

from __future__ import annotations

from test_engine import T0, minfo

from polydata.bt.strategy import BookView, FillInfo, OrderView, get_strategy

M5 = minfo(T0, "5m")


class Ctx:
    def __init__(self, m=M5, bids=((0.84, 50),), asks=((0.86, 30),), tau=40, fair=0.9):
        self.m, self.bids, self.asks, self.fairv = m, list(bids), list(asks), fair
        self.now = m.window_end_ms - tau * 1000
        self.places, self.cancels, self.timers, self._id = [], [], {}, 0

    def book(self, slug, depth=5):
        return BookView(self.bids, self.asks)

    def fair_value(self, slug):
        return self.fairv

    def place(self, slug, token, side, price, size, post_only=True):
        self._id += 1
        self.places.append((token, side, price, size))
        return self._id

    def cancel(self, oid):
        self.cancels.append(oid)

    def set_timer(self, name, ms):
        self.timers[name] = self.now + ms


def mk(params=None, **kw):
    s = get_strategy("endgame-maker")(params)
    c = Ctx(**kw)
    s.on_market_open(c, c.m)
    return s, c


def ov(oid, event, status, size=0.0):
    o = OrderView(oid, M5.slug, "Up", "BUY", 0.84, 10.0, "limit", True, 0, status=status, event=event)
    if size:
        o.last_fill = FillInfo(0, 0.84, size, 0.0, "maker")
    return o


def test_registered():
    cls = get_strategy("endgame-maker")
    assert cls.family == "endgame" and cls.default_params["start_tau_s"] == 60


def test_joins_best_bid_of_up_favourite():
    s, c = mk()  # mid 0.85, spread 0.02
    s.on_book(c, c.m, None)
    assert c.places == [("Up", "BUY", 0.84, 10.0)]
    s.on_book(c, c.m, None)  # already resting: no second order
    assert len(c.places) == 1


def test_down_favourite_uses_down_terms():
    # Up book 0.14/0.16 -> Down bid 0.84, ask 0.86
    s, c = mk(bids=((0.14, 50),), asks=((0.16, 30),))
    s.on_book(c, c.m, None)
    assert c.places == [("Down", "BUY", 0.84, 10.0)]


def test_no_quote_outside_time_band_band_or_wide_book():
    for kw in (dict(tau=90), dict(tau=3), dict(bids=((0.60, 5),), asks=((0.62, 5),)),
               dict(bids=((0.96, 5),), asks=((0.98, 5),)), dict(bids=((0.80, 5),), asks=((0.90, 5),)),
               dict(bids=((0.84, 5),), asks=())):
        s, c = mk(**kw)
        s.on_book(c, c.m, None)
        assert c.places == [], kw


def test_cancel_when_conditions_stop_then_no_double_cancel():
    s, c = mk()
    s.on_book(c, c.m, None)
    c.bids, c.asks = [(0.60, 5)], [(0.62, 5)]  # favourite fell out of the band
    s.on_book(c, c.m, None)
    s.on_book(c, c.m, None)
    assert c.cancels == [1]
    s.on_order(c, ov(1, "cancelled", "cancelled"))
    c.bids, c.asks = [(0.84, 5)], [(0.86, 5)]
    s.on_book(c, c.m, None)  # may quote again (nothing filled yet)
    assert len(c.places) == 2


def test_cancel_at_stop_time_via_timer():
    s, c = mk()
    s.on_book(c, c.m, None)
    c.now = c.m.window_end_ms - 4000
    c.timers.clear()
    s.on_timer(c, f"chk:{c.m.slug}")
    assert c.cancels == [1] and f"chk:{c.m.slug}" not in c.timers


def test_one_position_per_market_after_a_fill():
    s, c = mk()
    s.on_book(c, c.m, None)
    s.on_order(c, ov(1, "fill", "open", 4.0))  # partial: order still rests
    s.on_order(c, ov(1, "cancelled", "cancelled"))
    s.on_book(c, c.m, None)
    assert len(c.places) == 1


def test_fair_guard():
    s, c = mk({"fair_guard": True}, fair=0.80)  # fair 0.80 < bid 0.84
    s.on_book(c, c.m, None)
    assert c.places == []
    s2, c2 = mk({"fair_guard": True}, fair=0.90)
    s2.on_book(c2, c2.m, None)
    assert len(c2.places) == 1


def test_other_timeframe_ignored():
    s = get_strategy("endgame-maker")()
    c = Ctx()
    c.m = minfo(T0, "15m")
    s.on_market_open(c, c.m)
    s.on_book(c, c.m, None)
    assert c.places == []

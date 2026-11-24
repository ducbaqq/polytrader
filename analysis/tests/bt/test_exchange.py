from __future__ import annotations

import pandas as pd
import pytest

from polydata.bt.exchange import Exchange, ExchangeConfig, Order
from polydata.bt.marketdata import MarketData
from polydata.bt.types import MarketInfo
from polydata.fees import taker_fee

T0 = 1_795_520_000_000
SLUG = "btc-updown-5m-1795520000"


def info(tick_px: int = 10) -> MarketInfo:
    return MarketInfo(slug=SLUG, coin="btc", timeframe="5m", series="btc-up-or-down-5m", condition_id="0xabc",
                      window_start_ms=T0, window_end_ms=T0 + 300_000, price_to_beat=60000.0, fee_rate=0.05,
                      fee_exponent=1.0, tick_px=tick_px, min_order_size=5.0, outcome="Up",
                      resolution_recv_ms=T0 + 360_000)


def market(bids: str = "", asks: str = "", changes=(), trades=(), book_ts: int = T0 - 10_000) -> MarketData:
    """An Up snapshot at `book_ts`, Up change rows (ts_ms, side, price, size), trades
    (ts_ms, outcome, side, price, size)."""
    b = pd.DataFrame([(book_ts, book_ts + 100, "Up", bids, asks)],
                     columns=["ts_ms", "recv_ms", "outcome", "bids", "asks"])
    c = pd.DataFrame([(ts, ts + 100, "Up", side, price, size, 0.0, 1.0) for ts, side, price, size in changes],
                     columns=["ts_ms", "recv_ms", "outcome", "side", "price", "size", "best_bid", "best_ask"])
    t = pd.DataFrame([(ts, ts + 120, o, s, p, q) for ts, o, s, p, q in trades],
                     columns=["ts_ms", "recv_ms", "outcome", "side", "price", "size"])
    k = pd.DataFrame([], columns=["ts_ms", "recv_ms", "new_tick_size"])
    return MarketData.from_frames(info(), c, b, t, k)


_ids = iter(range(1, 10**6))


def order(token="Up", side="BUY", px=480, size=10.0, kind="limit", post_only=False, arrival=T0 + 1000) -> Order:
    return Order(id=next(_ids), slug=SLUG, token=token, side=side, px=px, size=size, kind=kind,
                 post_only=post_only, placed_ms=arrival - 200, arrival_ms=arrival)


def fills(events):
    return [e.fill for e in events if e.kind == "fill"]


# ------------------------------------------------------------------------------------------ makers

def test_join_back_of_queue():
    md = market("0.48:100", "0.52:100", trades=[(T0 + 2000, "Up", "SELL", 0.48, 60),
                                                (T0 + 3000, "Up", "SELL", 0.48, 50)])
    ex = Exchange(md, ExchangeConfig())
    o = order(px=480, size=20)
    ex.submit(o)
    ev = ex.advance(T0 + 1000)
    assert [e.kind for e in ev] == ["accepted"] and o.status == "open" and o.queue_ahead == 100
    assert fills(ex.advance(T0 + 2000)) == [] and o.queue_ahead == 40
    f = fills(ex.advance(T0 + 3000))
    assert len(f) == 1 and f[0].size == 10 and f[0].px == 480 and f[0].liquidity == "maker"
    assert f[0].fee == 0 and f[0].trade_ts_ms == T0 + 3000 and f[0].ts_ms == T0 + 3000
    assert o.filled == 10 and o.status == "open" and o.cost == pytest.approx(4.8)


def test_trade_through_fills_fully():
    md = market("0.48:100", "0.52:100", trades=[(T0 + 2000, "Up", "SELL", 0.47, 1)])
    ex = Exchange(md, ExchangeConfig())
    o = order(px=480, size=20)
    ex.submit(o)
    f = fills(ex.advance(T0 + 5000))
    assert [(x.px, x.size, x.queue_ahead_at_fill) for x in f] == [(480, 20, 100)]
    assert o.status == "filled" and o.filled == 20


def test_down_order_filled_by_up_trade():
    md = market("0.60:50", "0.65:50", trades=[(T0 + 2000, "Up", "BUY", 0.62, 10)])
    ex = Exchange(md, ExchangeConfig())
    o = order(token="Down", side="BUY", px=380, size=20)
    assert (o.up_side, o.up_px) == ("SELL", 620)
    ex.submit(o)
    ex.advance(T0 + 1000)
    assert o.queue_ahead == 0
    f = fills(ex.advance(T0 + 5000))
    assert [(x.px, x.size) for x in f] == [(380, 10)]
    assert o.cost == pytest.approx(3.8) and o.status == "open"


def test_trade_on_same_side_never_fills():
    # A taker SELL hits bids; our resting SELL (ask) must not fill from it.
    md = market("0.48:100", "0.52:100", trades=[(T0 + 2000, "Up", "SELL", 0.48, 500)])
    ex = Exchange(md, ExchangeConfig())
    o = order(side="SELL", px=520)
    ex.submit(o)
    assert fills(ex.advance(T0 + 5000)) == []


@pytest.mark.parametrize("model, ahead", [("behind", 70), ("proportional", 20)])
def test_cancel_model_behind_vs_proportional(model, ahead):
    md = market("0.48:100", "0.52:100", changes=[(T0 + 1500, "BUY", 0.48, 50)],
                trades=[(T0 + 2000, "Up", "SELL", 0.48, 30)])
    ex = Exchange(md, ExchangeConfig(cancel_model=model))
    o = order(px=480, size=10)
    ex.submit(o)
    assert fills(ex.advance(T0 + 5000)) == []
    assert o.queue_ahead == pytest.approx(ahead)


def test_proportional_trade_decrease_not_double_counted():
    # The level drops 100 -> 70 a few ms before the 30-share trade that caused it: not a cancel.
    md = market("0.48:100", "0.52:100", changes=[(T0 + 1996, "BUY", 0.48, 70)],
                trades=[(T0 + 2000, "Up", "SELL", 0.48, 30)])
    ex = Exchange(md, ExchangeConfig(cancel_model="proportional"))
    o = order(px=480, size=10)
    ex.submit(o)
    ex.advance(T0 + 5000)
    assert o.queue_ahead == pytest.approx(70)


def test_trade_echo_not_double_counted():
    md = market("0.48:100", "0.52:100", trades=[(T0 + 1010, "Up", "SELL", 0.48, 30),
                                                (T0 + 1020, "Up", "SELL", 0.47, 30)])
    for model in ("behind", "proportional"):
        ex = Exchange(md, ExchangeConfig(cancel_model=model))
        o = order(px=480, size=10, arrival=T0 + 1000)
        ex.submit(o)
        assert fills(ex.advance(T0 + 5000)) == []
        assert o.queue_ahead == 100 and o.filled == 0


def test_own_orders_fifo_and_share_trade_volume():
    # Two of our bids at 0.48 (10 each), nobody else ahead. A 15-share trade fills the first one fully
    # and only the 5 left over of the second: our earlier fills reduce what is left of the trade.
    md = market("", "0.52:100", trades=[(T0 + 2000, "Up", "SELL", 0.48, 15)])
    ex = Exchange(md, ExchangeConfig())
    a, b = order(px=480, size=10, arrival=T0 + 1000), order(px=480, size=10, arrival=T0 + 1100)
    ex.submit(a)
    ex.submit(b)
    f = fills(ex.advance(T0 + 5000))
    assert [(x.order_id, x.size) for x in f] == [(a.id, 10), (b.id, 5)]


def test_later_own_order_never_ahead_of_earlier():
    # a joins behind 100; the level then drops to 20 (cancels, "behind" model keeps a's ahead at 100).
    # b joins later at the same price and must still queue behind a.
    md = market("0.48:100", "0.52:100", changes=[(T0 + 1500, "BUY", 0.48, 20)],
                trades=[(T0 + 3000, "Up", "SELL", 0.48, 30)])
    ex = Exchange(md, ExchangeConfig())
    a, b = order(px=480, size=10, arrival=T0 + 1000), order(px=480, size=10, arrival=T0 + 2000)
    ex.submit(a)
    ex.submit(b)
    assert fills(ex.advance(T0 + 5000)) == []
    assert a.queue_ahead == 70 and b.queue_ahead >= a.queue_ahead


def test_trade_through_reduces_volume_left_for_worse_own_orders():
    # Our asks at 0.50 (10) and 0.52 (10, nothing ahead). A taker BUY of 10 at 0.52 would have hit our
    # 0.50 first: it fills (trade-through), and nothing is left for our 0.52.
    md = market("0.40:100", "", trades=[(T0 + 2000, "Up", "BUY", 0.52, 10)])
    ex = Exchange(md, ExchangeConfig())
    a, b = order(side="SELL", px=520, size=10), order(side="SELL", px=500, size=10)
    ex.submit(a)
    ex.submit(b)
    f = fills(ex.advance(T0 + 5000))
    assert [(x.order_id, x.size) for x in f] == [(b.id, 10)]


def test_partial_fill_then_cancel():
    md = market("", "0.52:100", trades=[(T0 + 2000, "Up", "SELL", 0.48, 4)])
    ex = Exchange(md, ExchangeConfig())
    o = order(px=480, size=10)
    ex.submit(o)
    ex.request_cancel(o.id, T0 + 3000)
    ev = ex.advance(T0 + 5000)
    assert [e.kind for e in ev] == ["accepted", "fill", "cancelled"]
    assert o.status == "cancelled" and o.filled == 4 and ev[-1].ts_ms == T0 + 3000


def test_ordering_equal_ms_pessimistic():
    md = market("0.48:100", "0.52:100", trades=[(T0 + 2000, "Up", "SELL", 0.47, 1)])
    ex = Exchange(md, ExchangeConfig())
    o = order(px=480, size=10)
    ex.submit(o)
    ex.request_cancel(o.id, T0 + 2000)
    ev = ex.advance(T0 + 5000)
    assert [e.kind for e in ev] == ["accepted", "fill", "rejected"]
    assert ev[-1].reason.startswith("cancel rejected") and o.status == "filled"


def test_arrival_after_same_ms_trade():
    # Trades at the arrival ms are processed before the arrival (and are echoes anyway).
    md = market("0.48:100", "0.52:100", trades=[(T0 + 1000, "Up", "SELL", 0.47, 1)])
    ex = Exchange(md, ExchangeConfig())
    o = order(px=480, size=10, arrival=T0 + 1000)
    ex.submit(o)
    assert fills(ex.advance(T0 + 5000)) == [] and o.status == "open"


def test_cancel_before_arrival_is_rejected_and_order_rests():
    md = market("0.48:100", "0.52:100")
    ex = Exchange(md, ExchangeConfig())
    o = order(px=480, size=10, arrival=T0 + 1000)
    ex.submit(o)
    ex.request_cancel(o.id, T0 + 1000)  # same ms: the cancel is processed first
    ev = ex.advance(T0 + 5000)
    assert [e.kind for e in ev] == ["rejected", "accepted"] and o.status == "open"


def test_close_expires_open_orders():
    md = market("0.48:100", "0.52:100")
    ex = Exchange(md, ExchangeConfig())
    o = order(px=480, size=10)
    ex.submit(o)
    ex.advance(T0 + 2000)
    ev = ex.close(T0 + 300_000)
    assert [(e.kind, e.order_id, e.ts_ms) for e in ev] == [("expired", o.id, T0 + 300_000)]
    assert o.status == "cancelled"
    assert ex.advance(T0 + 400_000) == []


# ------------------------------------------------------------------------------------------ takers

def test_taker_walks_and_one_fee():
    md = market("0.48:100", "0.52:10|0.53:10")
    ex = Exchange(md, ExchangeConfig())
    o = order(px=530, size=15, kind="take")
    ex.submit(o)
    ev = ex.advance(T0 + 1000)
    f = fills(ev)
    assert [(x.px, x.size, x.liquidity, x.trade_ts_ms) for x in f] == [(520, 10, "taker", None), (530, 5, "taker", None)]
    avg = (0.52 * 10 + 0.53 * 5) / 15
    assert avg == pytest.approx(0.5233333333)
    fee = taker_fee(15, avg, 0.05, 1.0)
    assert o.fee == fee and sum(x.fee for x in f) == fee and [x.fee for x in f] == [0.0, fee]
    assert o.status == "filled" and o.cost == pytest.approx(7.85)


def test_taker_ioc_remainder_cancelled():
    md = market("0.48:100", "0.52:10|0.53:10|0.55:100")
    ex = Exchange(md, ExchangeConfig())
    o = order(px=530, size=30, kind="take")
    ex.submit(o)
    ev = ex.advance(T0 + 1000)
    assert [e.kind for e in ev] == ["accepted", "fill", "fill", "cancelled"]
    assert o.filled == 20 and o.status == "cancelled" and o.fee == taker_fee(20, 0.525, 0.05)


def test_taker_down_sell_walks_up_asks():
    # SELL Down at >= 0.45 is a BUY Up at <= 0.55: it takes the Up asks; fill prices in Down terms.
    md = market("0.40:100", "0.52:10|0.53:10")
    ex = Exchange(md, ExchangeConfig())
    o = order(token="Down", side="SELL", px=470, size=12, kind="take")
    ex.submit(o)
    f = fills(ex.advance(T0 + 1000))
    assert [(x.px, x.size) for x in f] == [(480, 10), (470, 2)]
    assert o.fee == taker_fee(12, (0.48 * 10 + 0.47 * 2) / 12, 0.05)


def test_taker_impact_persists_then_resets():
    md = market("0.48:100", "0.52:10|0.53:10")
    ex = Exchange(md, ExchangeConfig())
    a = order(px=530, size=10, kind="take", arrival=T0 + 1000)
    b = order(px=530, size=5, kind="take", arrival=T0 + 3000)
    c = order(px=530, size=5, kind="take", arrival=T0 + 3000 + 5001)
    for o in (a, b, c):
        ex.submit(o)
    f = fills(ex.advance(T0 + 20_000))
    assert [(x.order_id, x.px, x.size) for x in f] == [(a.id, 520, 10), (b.id, 530, 5), (c.id, 520, 5)]


def test_taker_impact_resets_when_level_empties():
    md = market("0.48:100", "0.52:10|0.53:10", changes=[(T0 + 1500, "SELL", 0.52, 0), (T0 + 1600, "SELL", 0.52, 8)])
    ex = Exchange(md, ExchangeConfig())
    a = order(px=530, size=10, kind="take", arrival=T0 + 1000)
    b = order(px=530, size=5, kind="take", arrival=T0 + 2000)
    ex.submit(a)
    ex.submit(b)
    f = fills(ex.advance(T0 + 5000))
    assert [(x.order_id, x.px, x.size) for x in f] == [(a.id, 520, 10), (b.id, 520, 5)]


def test_taker_impact_partial_level():
    # Level refilled 10 -> 30 in the recording after we took 10: 20 left for us.
    md = market("0.48:100", "0.52:10|0.53:100", changes=[(T0 + 1500, "SELL", 0.52, 30)])
    ex = Exchange(md, ExchangeConfig())
    a = order(px=530, size=10, kind="take", arrival=T0 + 1000)
    b = order(px=530, size=25, kind="take", arrival=T0 + 2000)
    ex.submit(a)
    ex.submit(b)
    f = fills(ex.advance(T0 + 5000))
    assert [(x.order_id, x.px, x.size) for x in f] == [(a.id, 520, 10), (b.id, 520, 20), (b.id, 530, 5)]


def test_crossing_limit_takes_then_rests():
    md = market("0.48:100", "0.52:10|0.54:10", trades=[(T0 + 2000, "Up", "SELL", 0.52, 3)])
    ex = Exchange(md, ExchangeConfig())
    o = order(px=530, size=15)
    ex.submit(o)
    ev = ex.advance(T0 + 1000)
    assert [e.kind for e in ev] == ["accepted", "fill"]
    assert ev[1].fill.liquidity == "taker" and ev[1].fill.size == 10 and ev[1].fill.fee == taker_fee(10, 0.52, 0.05)
    assert o.status == "open" and o.queue_ahead == 0 and o.fee == ev[1].fill.fee
    f = fills(ex.advance(T0 + 5000))  # a taker SELL at 0.52 trades through our resting 0.53 bid
    assert [(x.px, x.size, x.liquidity, x.fee) for x in f] == [(530, 5, "maker", 0.0)]
    assert o.status == "filled" and o.fee == taker_fee(10, 0.52, 0.05)


# ------------------------------------------------------------------------------------------ rejections

def test_rejections():
    md = market("0.48:100", "0.52:100")
    ex = Exchange(md, ExchangeConfig())
    cases = [
        (order(px=485), "off tick"),
        (order(size=4), "size below minimum"),
        (order(px=0), "price out of range"),
        (order(px=1000, side="SELL"), "price out of range"),
        (order(arrival=T0 + 300_001), "after window end"),
        (order(px=520, post_only=True), "would cross"),
        (order(token="Down", px=530, post_only=True), "would cross"),  # = Up SELL 0.47, crosses the 0.48 bid
    ]
    for o, _ in cases:
        ex.submit(o)
    ev = ex.advance(T0 + 400_000)
    assert {e.order_id: (e.kind, e.reason) for e in ev} == {o.id: ("rejected", r) for o, r in cases}
    assert [e.ts_ms for e in ev] == sorted(e.ts_ms for e in ev)  # exchange-time order
    assert all(o.status == "rejected" and o.reason == r for o, r in cases)


def test_post_only_not_crossing_accepted():
    md = market("0.48:100", "0.52:100")
    ex = Exchange(md, ExchangeConfig())
    o = order(px=510, post_only=True)
    ex.submit(o)
    assert [e.kind for e in ex.advance(T0 + 2000)] == ["accepted"]


def test_tick_change_rejects_off_new_tick():
    md = market("0.48:100", "0.52:100")
    k = pd.DataFrame([(T0 + 500, T0 + 600, 0.001)], columns=["ts_ms", "recv_ms", "new_tick_size"])
    md2 = MarketData.from_frames(info(tick_px=1), pd.DataFrame(columns=["ts_ms", "recv_ms", "outcome", "side", "price",
                                                                       "size", "best_bid", "best_ask"]),
                                 pd.DataFrame([(T0 - 10_000, T0 - 9_900, "Up", "0.48:100", "0.52:100")],
                                              columns=["ts_ms", "recv_ms", "outcome", "bids", "asks"]),
                                 pd.DataFrame(columns=["ts_ms", "recv_ms", "outcome", "side", "price", "size"]), k)
    ex = Exchange(md, ExchangeConfig())
    o = order(px=485)
    ex.submit(o)
    assert ex.advance(T0 + 2000)[0].reason == "off tick"
    ex2 = Exchange(md2, ExchangeConfig())
    o2 = order(px=485)
    ex2.submit(o2)
    assert ex2.advance(T0 + 2000)[0].kind == "accepted"


# ------------------------------------------------------------------------------------------ misc

def test_submit_in_the_past_raises():
    md = market("0.48:100", "0.52:100")
    ex = Exchange(md, ExchangeConfig())
    ex.advance(T0 + 2000)
    with pytest.raises(ValueError):
        ex.submit(order(arrival=T0 + 1000))


def test_no_open_orders_skips_trades():
    md = market("0.48:100", "0.52:100", trades=[(T0 + i, "Up", "SELL", 0.47, 1) for i in range(100, 1100)])
    ex = Exchange(md, ExchangeConfig())
    assert ex.advance(T0 + 2000) == []
    assert ex._cur == 1000
    o = order(px=480, size=10, arrival=T0 + 2500)
    ex.submit(o)
    assert fills(ex.advance(T0 + 5000)) == []  # all trades are in the past

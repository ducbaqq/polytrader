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
    # each trade at our price is echoed by the recorded level decrease it caused (a few ms earlier)
    md = market("0.48:100", "0.52:100", changes=[(T0 + 1990, "BUY", 0.48, 40), (T0 + 2500, "BUY", 0.48, 60),
                                                 (T0 + 2990, "BUY", 0.48, 10)],
                trades=[(T0 + 2000, "Up", "SELL", 0.48, 60), (T0 + 3000, "Up", "SELL", 0.48, 50)])
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


def test_sweep_printed_at_worst_price_does_not_fill_behind_queue():
    # A taker SELL sweeps 50 at 0.49 and 30 at 0.48; Polymarket prints it as ONE row at 0.48, size 80.
    # Our 0.48 bid sits behind 60 others: only the 30 that really traded at 0.48 eats that queue.
    md = market("0.49:50|0.48:60", "0.52:100",
                changes=[(T0 + 1980, "BUY", 0.49, 0), (T0 + 1980, "BUY", 0.48, 30)],
                trades=[(T0 + 2000, "Up", "SELL", 0.48, 80)])
    ex = Exchange(md, ExchangeConfig())
    o = order(px=480, size=20)
    ex.submit(o)
    assert fills(ex.advance(T0 + 5000)) == []
    assert o.queue_ahead == pytest.approx(30) and o.filled == 0


def test_at_price_volume_capped_by_level_decrease_in_echo_window():
    # The cap counts decreases of our level with ts in [trade ts - 150 ms, trade ts], per ms (the state at
    # the end of each ms); increases do not offset them; a decrease before the window does not count.
    md = market("0.48:100", "0.52:100",
                changes=[(T0 + 1700, "BUY", 0.48, 95),  # 300 ms before the trade: outside the window
                         (T0 + 1900, "BUY", 0.48, 55), (T0 + 1900, "BUY", 0.48, 85),  # same ms: net 95 -> 85
                         (T0 + 1950, "BUY", 0.48, 125), (T0 + 1990, "BUY", 0.48, 105)],
                trades=[(T0 + 2000, "Up", "SELL", 0.48, 500)])
    ex = Exchange(md, ExchangeConfig())
    o = order(px=480, size=10)
    ex.submit(o)
    assert fills(ex.advance(T0 + 5000)) == []
    assert o.queue_ahead == pytest.approx(100 - 10 - 20)


def test_at_price_cap_shared_by_own_orders():
    # Two of our bids at 0.48 with nobody ahead; 12 shares join behind us. A 15-share trade whose level only
    # dropped by 12 fills 10 + 2.
    md = market("", "0.52:100", changes=[(T0 + 1200, "BUY", 0.48, 12), (T0 + 1995, "BUY", 0.48, 0)],
                trades=[(T0 + 2000, "Up", "SELL", 0.48, 15)])
    ex = Exchange(md, ExchangeConfig())
    a, b = order(px=480, size=10, arrival=T0 + 1000), order(px=480, size=10, arrival=T0 + 1100)
    ex.submit(a)
    ex.submit(b)
    f = fills(ex.advance(T0 + 5000))
    assert [(x.order_id, x.size) for x in f] == [(a.id, 10), (b.id, 2)]


def test_trade_through_sweep_fills_only_what_reached_us():
    # Others bid 20 at 0.50 and 30 at 0.49; our 0.49 bid (20) joins behind those 30. A taker SELL sweeps 0.50,
    # 0.49 and 15 at 0.48, printed as ONE row at 0.48 for 65. Before reaching us it used B = 20 (better level)
    # and Q = 30 (our queue): only 65 - 20 - 30 = 15 is left for us.
    md = market("0.50:20|0.49:30|0.48:100", "0.52:100",
                changes=[(T0 + 1990, "BUY", 0.50, 0), (T0 + 1990, "BUY", 0.49, 0), (T0 + 1990, "BUY", 0.48, 85)],
                trades=[(T0 + 2000, "Up", "SELL", 0.48, 65)])
    ex = Exchange(md, ExchangeConfig())
    o = order(px=490, size=20)
    ex.submit(o)
    f = fills(ex.advance(T0 + 5000))
    assert [(x.px, x.size) for x in f] == [(490, 15)] and o.status == "open"


def test_trade_through_fills_fully():
    # x = 150 covers our queue (100) and our whole order: the old full fill
    md = market("0.48:100", "0.52:100", trades=[(T0 + 2000, "Up", "SELL", 0.47, 150)])
    ex = Exchange(md, ExchangeConfig())
    o = order(px=480, size=20)
    ex.submit(o)
    f = fills(ex.advance(T0 + 5000))
    assert [(x.px, x.size, x.queue_ahead_at_fill) for x in f] == [(480, 20, 100)]
    assert o.status == "filled" and o.filled == 20


def test_trade_through_capped_by_trade_size():
    # A 15-share taker SELL at 0.47 first takes the 10 ahead of us at 0.48, then 5 from us and stops: 5, not 20.
    md = market("0.48:10", "0.52:100", trades=[(T0 + 2000, "Up", "SELL", 0.47, 15),
                                               (T0 + 3000, "Up", "SELL", 0.46, 40)])
    ex = Exchange(md, ExchangeConfig())
    o = order(px=480, size=20)
    ex.submit(o)
    f = fills(ex.advance(T0 + 2000))
    assert [(x.px, x.size) for x in f] == [(480, 5)] and o.status == "open" and o.queue_ahead == 10
    f = fills(ex.advance(T0 + 5000))
    assert [(x.px, x.size) for x in f] == [(480, 15)] and o.status == "filled"


def test_down_order_filled_by_up_trade():
    md = market("0.60:50", "0.65:50", changes=[(T0 + 1500, "SELL", 0.62, 10), (T0 + 1995, "SELL", 0.62, 0)],
                trades=[(T0 + 2000, "Up", "BUY", 0.62, 10)])
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
    md = market("0.48:100", "0.52:100", changes=[(T0 + 1500, "BUY", 0.48, 50), (T0 + 1995, "BUY", 0.48, 20)],
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
                                                (T0 + 1150, "Up", "SELL", 0.47, 30)])
    for model in ("behind", "proportional"):
        ex = Exchange(md, ExchangeConfig(cancel_model=model))
        o = order(px=480, size=10, arrival=T0 + 1000)
        ex.submit(o)
        assert fills(ex.advance(T0 + 5000)) == []
        assert o.queue_ahead == 100 and o.filled == 0


def test_echo_and_explain_defaults():
    cfg = ExchangeConfig()
    assert cfg.trade_echo_ms == 150 and cfg.trade_explain_ms == 150


def test_proportional_decrease_explained_by_lagging_trade():
    # Trade ts lags its level change by up to ~134 ms (p99): a drop 120 ms before the trade is the trade.
    md = market("0.48:100", "0.52:100", changes=[(T0 + 1880, "BUY", 0.48, 70)],
                trades=[(T0 + 2000, "Up", "SELL", 0.48, 30)])
    ex = Exchange(md, ExchangeConfig(cancel_model="proportional"))
    o = order(px=480, size=10)
    ex.submit(o)
    ex.advance(T0 + 5000)
    assert o.queue_ahead == pytest.approx(70)


def test_own_orders_fifo_and_share_trade_volume():
    # Two of our bids at 0.48 (10 each), nobody else ahead. A 15-share trade fills the first one fully
    # and only the 5 left over of the second: our earlier fills reduce what is left of the trade.
    md = market("", "0.52:100", changes=[(T0 + 1500, "BUY", 0.48, 15), (T0 + 1995, "BUY", 0.48, 0)],
                trades=[(T0 + 2000, "Up", "SELL", 0.48, 15)])
    ex = Exchange(md, ExchangeConfig())
    a, b = order(px=480, size=10, arrival=T0 + 1000), order(px=480, size=10, arrival=T0 + 1100)
    ex.submit(a)
    ex.submit(b)
    f = fills(ex.advance(T0 + 5000))
    assert [(x.order_id, x.size) for x in f] == [(a.id, 10), (b.id, 5)]


def test_later_own_order_never_ahead_of_earlier():
    # a joins behind 100; the level then drops to 20 (cancels, "behind" model keeps a's ahead at 100).
    # b joins later at the same price and must still queue behind a.
    md = market("0.48:100", "0.52:100", changes=[(T0 + 1500, "BUY", 0.48, 20), (T0 + 2500, "BUY", 0.48, 40),
                                                 (T0 + 2995, "BUY", 0.48, 10)],
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
    md = market("", "0.52:100", changes=[(T0 + 1500, "BUY", 0.48, 4), (T0 + 1995, "BUY", 0.48, 0)],
                trades=[(T0 + 2000, "Up", "SELL", 0.48, 4)])
    ex = Exchange(md, ExchangeConfig())
    o = order(px=480, size=10)
    ex.submit(o)
    ex.request_cancel(o.id, T0 + 3000)
    ev = ex.advance(T0 + 5000)
    assert [e.kind for e in ev] == ["accepted", "fill", "cancelled"]
    assert o.status == "cancelled" and o.filled == 4 and ev[-1].ts_ms == T0 + 3000


def test_ordering_equal_ms_pessimistic():
    md = market("0.48:5", "0.52:100", trades=[(T0 + 2000, "Up", "SELL", 0.47, 15)])
    ex = Exchange(md, ExchangeConfig())
    o = order(px=480, size=10)
    ex.submit(o)
    ex.request_cancel(o.id, T0 + 2000)
    ev = ex.advance(T0 + 5000)
    assert [e.kind for e in ev] == ["accepted", "fill", "cancel_rejected"]
    assert ev[-1].reason == "filled" and o.status == "filled"


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
    ex.request_cancel(o.id, T0 + 999)  # strictly before the order: nothing to cancel yet
    ev = ex.advance(T0 + 5000)
    assert [e.kind for e in ev] == ["cancel_rejected", "accepted"] and o.status == "open"
    assert ev[0].reason == "pending"


def test_cancel_same_ms_as_its_order_cancels_it():
    md = market("0.48:100", "0.52:100")
    ex = Exchange(md, ExchangeConfig())
    other = order(px=470, size=10, arrival=T0 + 1000)
    o = order(px=480, size=10, arrival=T0 + 1000)
    ex.submit(other)
    ex.submit(o)
    ex.request_cancel(o.id, T0 + 1000)  # same ms: its order arrives first, then the cancel
    ev = ex.advance(T0 + 5000)
    assert [(e.kind, e.order_id) for e in ev] == [("accepted", o.id), ("cancelled", o.id), ("accepted", other.id)]
    assert o.status == "cancelled" and other.status == "open"


def test_close_expires_open_orders():
    md = market("0.48:100", "0.52:100")
    ex = Exchange(md, ExchangeConfig())
    o = order(px=480, size=10)
    ex.submit(o)
    ex.advance(T0 + 2000)
    ev = ex.close(T0 + 300_000)
    assert [(e.kind, e.order_id, e.ts_ms) for e in ev] == [("expired", o.id, T0 + 300_000)]
    assert o.status == "cancelled"
    assert ex.advance(T0 + 400_000) == [] and ex.close(T0 + 300_000) == []  # idempotent


def test_no_fills_after_window_end():
    end = T0 + 300_000
    md = market("0.48:100", "0.52:100", trades=[(end + 5000, "Up", "SELL", 0.40, 100)])
    ex = Exchange(md, ExchangeConfig())
    o = order(px=480, size=10)
    ex.submit(o)
    ev = ex.advance(end + 10_000)  # no close()
    assert [(e.kind, e.ts_ms, e.reason) for e in ev] == [("accepted", T0 + 1000, ""), ("expired", end, "window end")]
    assert o.status == "cancelled" and o.filled == 0
    assert ex.close(end + 10_000) == []


def test_trade_at_window_end_not_matched():
    end = T0 + 300_000
    md = market("0.48:100", "0.52:100", trades=[(end - 1, "Up", "SELL", 0.47, 120), (end, "Up", "SELL", 0.40, 100)])
    ex = Exchange(md, ExchangeConfig())
    a, b = order(px=480, size=10), order(px=470, size=10)
    ex.submit(a)
    ex.submit(b)
    ev = ex.close(end)
    assert [(e.kind, e.order_id) for e in ev if e.kind in ("fill", "expired")] == [("fill", a.id), ("expired", b.id)]


def test_close_cancels_orders_that_never_arrived():
    md = market("0.48:100", "0.52:100")
    ex = Exchange(md, ExchangeConfig())
    o = order(px=480, size=10, arrival=T0 + 290_000)
    ex.submit(o)
    ev = ex.close(T0 + 200_000)
    assert [(e.kind, e.reason) for e in ev] == [("cancelled", "never arrived")]
    assert o.status == "cancelled" and o.reason == "never arrived"


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
    md = market("0.48:100", "0.52:10|0.54:10", trades=[(T0 + 2000, "Up", "SELL", 0.52, 5)])
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


# ------------------------------------------------------------------------------------------ taker delay

DELAY = ExchangeConfig(taker_delay_ms=250)


def test_taker_delay_fills_at_book_after_hold():
    # the ask improves 100 ms after arrival and worsens 300 ms after: the take sees the book at arrival + 250
    md = market("0.48:100", "0.52:100", changes=[(T0 + 1100, "SELL", 0.51, 50), (T0 + 1300, "SELL", 0.51, 0)])
    ex = Exchange(md, DELAY)
    o = order(px=550, size=10, kind="take")
    ex.submit(o)
    assert ex.advance(T0 + 1249) == [] and o.status == "pending"
    ev = ex.advance(T0 + 1250)
    assert [e.kind for e in ev] == ["accepted", "fill"] and ev[0].ts_ms == T0 + 1250
    f = fills(ev)
    assert f[0].px == 510 and f[0].ts_ms == T0 + 1250 and f[0].liquidity == "taker"
    assert f[0].fee == pytest.approx(taker_fee(10, 0.51, 0.05, 1.0)) and o.status == "filled"


def test_taker_delay_off_fills_at_arrival():
    md = market("0.48:100", "0.52:100", changes=[(T0 + 1100, "SELL", 0.51, 50)])
    ex = Exchange(md, ExchangeConfig())
    o = order(px=550, size=10, kind="take")
    ex.submit(o)
    f = fills(ex.advance(T0 + 1000))
    assert f[0].px == 520 and f[0].ts_ms == T0 + 1000


def test_maker_cancel_inside_hold_beats_taker():
    # the 0.52 ask is cancelled 100 ms into the hold: the take walks to 0.53, or gets nothing under its limit
    chg = [(T0 + 1100, "SELL", 0.52, 0)]
    md = market("0.48:100", "0.52:100|0.53:100", changes=chg)
    ex = Exchange(md, DELAY)
    o = order(px=550, size=10, kind="take")
    ex.submit(o)
    f = fills(ex.advance(T0 + 2000))
    assert [x.px for x in f] == [530] and f[0].ts_ms == T0 + 1250
    md = market("0.48:100", "0.52:100|0.53:100", changes=chg)
    ex = Exchange(md, DELAY)
    o = order(px=520, size=10, kind="take")
    ex.submit(o)
    ev = ex.advance(T0 + 2000)
    assert [e.kind for e in ev] == ["accepted", "cancelled"] and ev[1].reason == "ioc remainder"
    assert o.filled == 0 and ev[1].ts_ms == T0 + 1250


def test_cancel_of_held_taker_rejected():
    md = market("0.48:100", "0.52:100")
    ex = Exchange(md, DELAY)
    o = order(px=520, size=130, kind="limit")
    ex.submit(o)
    ex.request_cancel(o.id, T0 + 1000)  # the same ms as its arrival: the order is processed first, then held
    ex.request_cancel(o.id, T0 + 1200)
    ev = ex.advance(T0 + 1250)
    assert [(e.kind, e.ts_ms, e.reason) for e in ev[:2]] == [("cancel_rejected", T0 + 1000, "taker delay"),
                                                             ("cancel_rejected", T0 + 1200, "taker delay")]
    assert [e.kind for e in ev[2:]] == ["accepted", "fill"]
    # the crossing limit's remainder rests from the hold's end; a cancel now works
    assert o.status == "open" and o.filled == 100 and o.book_ms == T0 + 1250
    ex.request_cancel(o.id, T0 + 1300)
    assert [e.kind for e in ex.advance(T0 + 1300)] == ["cancelled"]


def test_held_limit_rests_if_no_longer_crossing():
    # the ask moves away during the hold: the limit is placed on the book at the hold's end, behind the bid level
    md = market("0.50:40", "0.52:100", changes=[(T0 + 1100, "SELL", 0.52, 0), (T0 + 1100, "SELL", 0.55, 100),
                                                (T0 + 1150, "BUY", 0.52, 70)])
    ex = Exchange(md, DELAY)
    o = order(px=520, size=10)
    ex.submit(o)
    ev = ex.advance(T0 + 1250)
    assert [e.kind for e in ev] == ["accepted"] and ev[0].ts_ms == T0 + 1250
    assert o.status == "open" and o.queue_ahead == 70 and o.book_ms == T0 + 1250


def test_non_crossing_and_post_only_not_delayed():
    md = market("0.48:100", "0.52:100")
    ex = Exchange(md, DELAY)
    a, b = order(px=480), order(token="Up", side="SELL", px=530, post_only=True)
    ex.submit(a)
    ex.submit(b)
    ev = ex.advance(T0 + 1000)
    assert [e.kind for e in ev] == ["accepted", "accepted"] and a.book_ms == T0 + 1000


def test_hold_past_window_end_rejected():
    md = market("0.48:100", "0.52:100")
    ex = Exchange(md, DELAY)
    o = order(px=550, size=10, kind="take", arrival=T0 + 299_900)
    ex.submit(o)
    ev = ex.close(T0 + 300_000)
    assert [(e.kind, e.reason) for e in ev] == [("rejected", "after window end")] and o.status == "rejected"

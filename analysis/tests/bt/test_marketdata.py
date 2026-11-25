from __future__ import annotations

import math

import pandas as pd
import pytest

from polydata.bt.marketdata import MarketData
from polydata.bt.types import MarketInfo, from_up, to_px, to_up

T0 = 1_795_520_000_000


def info(tick_px: int = 10) -> MarketInfo:
    return MarketInfo(slug="btc-updown-5m-1795520000", coin="btc", timeframe="5m", series="btc-up-or-down-5m",
                      condition_id="0xabc", window_start_ms=T0, window_end_ms=T0 + 300_000, price_to_beat=60000.0,
                      fee_rate=0.072, fee_exponent=1.0, tick_px=tick_px, min_order_size=5.0, outcome="Up",
                      resolution_recv_ms=T0 + 360_000)


def changes(rows) -> pd.DataFrame:
    """rows: (ts_ms, outcome, side, price, size, best_bid, best_ask); recv_ms = ts_ms + 100."""
    df = pd.DataFrame(rows, columns=["ts_ms", "outcome", "side", "price", "size", "best_bid", "best_ask"])
    df.insert(0, "recv_ms", df.ts_ms + 100)
    return df


def mirror(rows):
    """Up change rows plus their Down mirrors (as the exchange publishes them)."""
    out = []
    for ts, side, price, size, bb, ba in rows:
        out.append((ts, "Up", side, price, size, bb, ba))
        out.append((ts, "Down", "SELL" if side == "BUY" else "BUY", round(1 - price, 3), size,
                    round(1 - ba, 3) if ba < 1 else 0.0, round(1 - bb, 3) if bb > 0 else 1.0))
    return out


def books(rows) -> pd.DataFrame:
    """rows: (ts_ms, outcome, bids, asks)."""
    df = pd.DataFrame(rows, columns=["ts_ms", "outcome", "bids", "asks"])
    df.insert(0, "recv_ms", df.ts_ms + 100)
    return df


def trades(rows) -> pd.DataFrame:
    """rows: (ts_ms, outcome, side, price, size)."""
    df = pd.DataFrame(rows, columns=["ts_ms", "outcome", "side", "price", "size"])
    df.insert(0, "recv_ms", df.ts_ms + 120)
    return df


def ticks(rows=()) -> pd.DataFrame:
    df = pd.DataFrame(list(rows), columns=["ts_ms", "new_tick_size"])
    df.insert(0, "recv_ms", df.ts_ms + 100)
    return df


EMPTY_C, EMPTY_B, EMPTY_T = changes([]), books([]), trades([])


def test_mirror_roundtrip_extremes():
    for px in [1, 10, 500, 990, 999]:
        for token in ("Up", "Down"):
            for s in ("BUY", "SELL"):
                assert from_up(token, *to_up(token, s, px)) == (s, px)
    assert to_up("Down", "BUY", 380) == ("SELL", 620)
    assert to_up("Up", "SELL", 999) == ("SELL", 999)
    assert to_px(0.001) == 1 and to_px(0.999) == 999 and to_px(1 - 0.38) == 620
    assert isinstance(to_up("Down", "SELL", 1)[1], int)


def test_level_series_and_snapshot_reset():
    b = books([(T0, "Up", "0.48:100|0.47:50", "0.52:80"),
               (T0 + 2000, "Up", "0.48:30", "0.52:80")])
    c = changes(mirror([(T0 + 1000, "BUY", 0.48, 30.0, 0.48, 0.52)]))
    md = MarketData.from_frames(info(), c, b, EMPTY_T, ticks())
    assert md.size_at("BUY", 470, T0 + 1500) == 50
    assert md.size_at("BUY", 470, T0 + 2000) == 0
    assert md.size_at("BUY", 470, T0 + 9999) == 0
    assert md.size_at("BUY", 480, T0 + 999) == 100
    assert md.size_at("BUY", 480, T0 + 1000) == 30
    assert md.size_at("BUY", 470, T0 - 1) == 0
    assert md.book_at(T0 + 1500) == ([(480, 30), (470, 50)], [(520, 80)])
    assert md.book_at(T0 + 2500) == ([(480, 30)], [(520, 80)])
    assert md.book_at(T0 + 1500, depth=1) == ([(480, 30)], [(520, 80)])
    # recv clock: the change at ts T0+1000 is received at T0+1100
    assert md.size_at("BUY", 480, T0 + 1050, clock="recv") == 100
    assert md.size_at("BUY", 480, T0 + 1100, clock="recv") == 30
    ts, recv, size = md.level_changes("BUY", 470, T0, T0 + 5000)
    assert list(ts) == [T0 + 2000] and list(size) == [0]
    ts, recv, size = md.level_changes("BUY", 470, T0 - 1, T0 + 5000)
    assert list(ts) == [T0, T0 + 2000] and list(size) == [50, 0]


def test_snapshot_after_same_ts_changes():
    # A snapshot at the same ts_ms as a change reflects the state after it.
    b = books([(T0, "Up", "0.48:100", "0.52:80"), (T0 + 1000, "Up", "0.48:100", "0.52:80")])
    c = changes(mirror([(T0 + 1000, "BUY", 0.48, 30.0, 0.48, 0.52)]))
    md = MarketData.from_frames(info(), c, b, EMPTY_T, ticks())
    assert md.size_at("BUY", 480, T0 + 1000) == 100


def test_down_changes_ignored_when_mirrored():
    b = books([(T0, "Up", "0.48:100", "0.52:80")])
    c = changes(mirror([(T0 + 1000, "SELL", 0.52, 20.0, 0.48, 0.52)]))
    md = MarketData.from_frames(info(), c, b, EMPTY_T, ticks())
    assert md.size_at("SELL", 520, T0 + 1000) == 20
    # The Down BUY at 0.48 (mirror of Up SELL 0.52) must not appear as an Up level at 480/520 again.
    assert md.book_at(T0 + 1000) == ([(480, 100)], [(520, 20)])
    assert len(md.level_changes("SELL", 520, T0, T0 + 5000)[0]) == 1


def test_down_only_change_applied():
    b = books([(T0, "Up", "0.48:100", "0.52:80")])
    # Down BUY at 0.47 size 40 with no Up mirror -> Up SELL at 0.53 size 40
    c = changes([(T0 + 1000, "Down", "BUY", 0.47, 40.0, 0.48, 0.52)])
    md = MarketData.from_frames(info(), c, b, EMPTY_T, ticks())
    assert md.size_at("SELL", 530, T0 + 1000) == 40
    assert md.book_at(T0 + 1000) == ([(480, 100)], [(520, 80), (530, 40)])


def test_down_only_snapshot_applied():
    b = books([(T0, "Down", "0.52:100", "0.48:80")])  # Down bids 0.52 -> Up asks 0.48 ...
    md = MarketData.from_frames(info(), EMPTY_C, b, EMPTY_T, ticks())
    assert md.book_at(T0) == ([(520, 80)], [(480, 100)])


def test_top_changes_only_on_change():
    b = books([(T0, "Up", "0.48:100", "0.52:80")])
    c = changes(mirror([
        (T0 + 1000, "BUY", 0.47, 10.0, 0.48, 0.52),   # deep change: top unchanged
        (T0 + 2000, "BUY", 0.49, 15.0, 0.49, 0.52),   # new best bid
        (T0 + 3000, "BUY", 0.46, 10.0, 0.49, 0.52),   # unchanged
        (T0 + 4000, "SELL", 0.52, 0.0, 0.49, 1.0),    # ask side empty
    ]))
    md = MarketData.from_frames(info(), c, b, EMPTY_T, ticks())
    assert list(md.tops.ts_ms) == [T0, T0 + 2000, T0 + 4000]
    assert list(md.tops.bid_px) == [480, 490, 490] and list(md.tops.ask_px) == [520, 520, 1000]
    assert list(md.tops_recv.recv_ms) == [T0 + 100, T0 + 2100, T0 + 4100]
    top = md.top_at(T0 + 2100)  # recv clock by default
    assert (top.bid_px, top.bid_size, top.ask_px, top.ask_size) == (490, 15, 520, 80)
    assert (top.recv_ms, top.ts_ms) == (T0 + 2100, T0 + 2000)
    assert md.top_at(T0 + 2099).bid_px == 480
    empty = md.top_at(T0 + 4100)
    assert (empty.ask_px, empty.ask_size) == (1000, 0)
    before = md.top_at(T0)
    assert (before.bid_px, before.ask_px, before.bid_size) == (0, 1000, 0)
    assert md.top_at(T0 + 2000, clock="ts").bid_px == 490


def test_trade_up_terms():
    t = trades([(T0 + 500, "Down", "SELL", 0.38, 10.0), (T0 + 400, "Up", "BUY", 0.52, 5.0)])
    md = MarketData.from_frames(info(), EMPTY_C, EMPTY_B, t, ticks())
    assert list(md.trades.ts_ms) == [T0 + 400, T0 + 500]  # ts order
    tr = md.trade(1)
    assert (tr.token, tr.side, tr.price, tr.size) == ("Down", "SELL", 0.38, 10.0)
    assert (tr.up_side, tr.up_px) == ("BUY", 620)
    assert md.trade(0).up_side == "BUY" and md.trade(0).up_px == 520
    assert tr.recv_ms == T0 + 620


def test_tick_size_change():
    b = books([(T0, "Up", "0.99:10", "0.995:5")])
    md = MarketData.from_frames(info(tick_px=10), EMPTY_C, b, EMPTY_T, ticks([(T0 + 100, 0.001)]))
    assert md.tick_at(T0) == 10
    assert md.tick_at(T0 + 100) == 1
    assert md.tick_at(T0 + 150, clock="recv") == 10 and md.tick_at(T0 + 200, clock="recv") == 1
    assert md.size_at("SELL", 995, T0 + 200) == 5
    assert md.book_at(T0 + 200) == ([(990, 10)], [(995, 5)])


def test_empty_market():
    md = MarketData.from_frames(info(), EMPTY_C, EMPTY_B, EMPTY_T, ticks())
    assert md.book_at(T0) == ([], [])
    assert md.size_at("BUY", 500, T0) == 0
    assert md.top_at(T0).bid_px == 0
    assert len(md.trades.ts_ms) == 0
    with pytest.raises(IndexError):
        md.trade(0)


def test_same_ms_rows_follow_seq():
    # Rows sharing ts_ms and recv_ms apply in `seq` order (the raw arrival order kept by the
    # Parquet build), not in frame order.
    b = books([(T0, "Up", "0.58:50|0.57:20", "0.59:10")])
    rows = [(T0 + 1000, "BUY", 0.58, 0.0, 0.57, 0.59),      # seq 0: the 0.58 bid is taken
            (T0 + 1000, "SELL", 0.58, 10.0, 0.57, 0.58),    # seq 1: the rest of the sell order rests
            (T0 + 2000, "BUY", 0.47, 30.0, 0.57, 0.58),     # seq 2..4: 30 -> 15 -> 0 at one ms
            (T0 + 2000, "BUY", 0.47, 15.0, 0.57, 0.58),
            (T0 + 2000, "BUY", 0.47, 0.0, 0.57, 0.58)]
    c = changes(mirror(rows))
    c["seq"] = [i for i in range(len(rows)) for _ in (0, 1)]
    c = c.iloc[::-1].reset_index(drop=True)  # frame order reversed
    md = MarketData.from_frames(info(), c, b, EMPTY_T, ticks())
    assert md.book_at(T0 + 1000) == ([(570, 20)], [(580, 10), (590, 10)])
    top = md.top_at(T0 + 1000, clock="ts")
    assert (top.bid_px, top.ask_px, top.ask_size) == (570, 580, 10)
    assert list(zip(md.tops.bid_px.tolist(), md.tops.ask_px.tolist())) == [(580, 590), (570, 590), (570, 580)]
    assert md.size_at("BUY", 470, T0 + 2000) == 0


def test_top_at_recv_never_zero_for_present_price():
    # The bid at 0.49 is emptied (received on time) and refilled, but the refill arrives late. A
    # later change reports best bid 0.49: on the recv clock the level still reads 0, so top_at
    # reports the last positive size known on that clock (100); with none known, NaN.
    b = books([(T0, "Up", "0.49:100|0.48:10", "0.52:80")])
    c = changes(mirror([(T0 + 1000, "BUY", 0.49, 0.0, 0.48, 0.52),
                        (T0 + 1500, "BUY", 0.49, 20.0, 0.49, 0.52),
                        (T0 + 2000, "SELL", 0.52, 70.0, 0.49, 0.52),
                        (T0 + 3000, "BUY", 0.50, 5.0, 0.50, 0.52),
                        (T0 + 3500, "SELL", 0.52, 60.0, 0.50, 0.52)]))
    c.loc[c.ts_ms == T0 + 1500, "recv_ms"] = T0 + 9000
    c.loc[c.ts_ms == T0 + 3000, "recv_ms"] = T0 + 9500
    md = MarketData.from_frames(info(), c, b, EMPTY_T, ticks())
    top = md.top_at(T0 + 2100)
    assert (top.bid_px, top.bid_size) == (490, 100)
    assert md.size_at("BUY", 490, T0 + 2100, clock="recv") == 0
    top = md.top_at(T0 + 3600)  # bid 0.50 reported, its only change not yet received
    assert top.bid_px == 500 and math.isnan(top.bid_size)
    assert md.top_at(T0 + 9600).bid_size == 5


def test_trades_recv_order():
    t = trades([(T0 + 500, "Up", "BUY", 0.52, 5.0), (T0 + 400, "Down", "SELL", 0.38, 10.0)])
    t.loc[0, "recv_ms"] = T0 + 450  # received before the ts-earlier trade (recv T0+520)
    md = MarketData.from_frames(info(), EMPTY_C, EMPTY_B, t, ticks())
    assert list(md.trades.ts_ms) == [T0 + 400, T0 + 500]
    assert list(md.trades_recv.recv_ms) == [T0 + 450, T0 + 520]
    assert list(md.trades_recv.ts_ms) == [T0 + 500, T0 + 400]

"""GrowingMarketData fed as the live runner feeds it must answer every query exactly as MarketData.from_frames over the
same rows (and the live Binance grid as _Grid): that is what makes paper/replay parity possible."""

from __future__ import annotations

import dataclasses
import math

import duckdb
import numpy as np
import pandas as pd
import pytest
from live_feed import LiveFeed, Row, compare_arrays, compare_queries, frames, grid

from polydata.bt.core import _Bn, _Grid
from polydata.bt.marketdata import MarketData
from polydata.bt.marketdata_live import GrowingArray, GrowingMarketData, LiveBn, LiveCl, LiveGrid, info_from_meta
from polydata.bt.types import MarketInfo

WS = 1_795_520_100_000  # a 5m window start
SLUG = f"btc-updown-5m-{WS // 1000}"
UP, DN = f"{SLUG}|U", f"{SLUG}|D"
INFO = MarketInfo(slug=SLUG, coin="btc", timeframe="5m", series="btc-up-or-down-5m", condition_id="0xabc",
                  window_start_ms=WS, window_end_ms=WS + 300_000, price_to_beat=None, fee_rate=0.072,
                  fee_exponent=1.0, tick_px=10, min_order_size=5.0, outcome=None, resolution_recv_ms=None)
STEP = 7  # query grid (ms)


def p3(px: int) -> str:
    return f"{px / 1000:.3f}"


def chg(recv, seq, ts, asset, side, px, size, bb, ba) -> Row:
    """bb/ba: px or None (empty field)."""
    return Row("pm_price_change", recv, seq, (str(ts), asset, side, p3(px), str(size),
                                              "" if bb is None else p3(bb), "" if ba is None else p3(ba)))


def book(recv, seq, ts, asset, bids, asks) -> Row:
    """bids/asks: [(px, size)]."""
    return Row("pm_book", recv, seq, (str(ts), asset, "|".join(f"{p3(p)}:{s}" for p, s in bids),
                                      "|".join(f"{p3(p)}:{s}" for p, s in asks), "h"))


def trd(recv, seq, ts, asset, side, px, size) -> Row:
    return Row("pm_trade", recv, seq, (str(ts), asset, side, p3(px), str(size), "0", "0xtx"))


def tick(recv, ts, size) -> Row:
    return Row("tick", recv, 0, (str(ts), str(size)))


def check(rows: list[Row], hold_back: int = 3000, skew: int = 1000, step_ms: int = 20) -> GrowingMarketData:
    """Feed `rows` live and compare with MarketData.from_frames: at every step, every query at the newly visible
    times (recv clock up to T, ts clock up to H_ts) on the 7 ms grid; at the end every array and every query."""
    ref = MarketData.from_frames(INFO, *frames(rows))
    live = GrowingMarketData(INFO)
    feed = LiveFeed(live, rows, hold_back, skew)
    lo = min(min(int(r.values[0]), r.recv_ms) for r in rows) - 50
    n = [0]

    def on_step(H_prev, H, T_prev, T):
        n[0] += compare_queries(live, ref, grid(max(T_prev, lo), T, STEP), "recv", level_changes_from=max(T_prev, lo))
        n[0] += compare_queries(live, ref, grid(max(H_prev, lo), H, STEP), "ts", level_changes_from=max(H_prev, lo))

    feed.run(step_ms, on_step)
    assert n[0] > 0
    compare_arrays(live, ref)
    hi = feed.end_wall()
    for clock in ("ts", "recv"):
        compare_queries(live, ref, grid(lo, hi, STEP), clock, level_changes_from=lo)
    assert feed.trades_added == len(ref.trades.ts_ms)
    return live


# ---------------------------------------------------------------------------------------- synthetic generator

def synth(seed: int, n_ts: int = 300) -> list[Row]:
    """Random recorder-shaped rows of one market: Up changes, their Down mirrors (often in a separate message with
    another recv_ms), unmirrored Down and Up rows, one-sided tops, Up/Down/duplicate snapshots, trades on both
    tokens, tick size changes on both tokens. Feed delays from -400 ms (exchange clock ahead) to 2500 ms."""
    rng = np.random.default_rng(seed)
    raw: list[tuple] = []  # (stream, recv, tiebreak, values-builder args)
    ts = WS - 30_000
    pxs = list(range(400, 620, 10)) + [455, 505, 515]

    def delay():
        return int(rng.integers(-400, 2500))

    def side_px():
        return ("BUY" if rng.random() < 0.5 else "SELL"), int(rng.choice(pxs))

    def top():
        bb = None if rng.random() < 0.08 else int(rng.choice(pxs[:12]))
        ba = None if rng.random() < 0.08 else int(rng.choice(pxs[10:]))
        return bb, ba

    for _ in range(n_ts):
        ts += int(rng.integers(1, 40))
        d_up = delay()
        for _ in range(int(rng.integers(0, 5))):
            side, px = side_px()
            size = 0 if rng.random() < 0.25 else round(float(rng.uniform(1, 300)), 2)
            bb, ba = top()
            u = rng.random()
            if u < 0.85:  # Up row
                raw.append(("c", ts + d_up, (ts, UP, side, px, size, bb, ba)))
            if u < 0.75 or u >= 0.85:  # Down mirror (or a Down-only row)
                d_dn = d_up + int(rng.integers(-5, 6)) if rng.random() < 0.5 else delay()
                dside = "SELL" if side == "BUY" else "BUY"
                dbb = None if ba is None else 1000 - ba
                dba = None if bb is None else 1000 - bb
                dpx = 1000 - px if u < 0.75 or rng.random() < 0.5 else int(rng.choice(pxs))
                raw.append(("c", ts + d_dn, (ts, DN, dside, dpx, size, dbb, dba)))
        if rng.random() < 0.12:
            for _ in range(int(rng.integers(1, 3))):
                lv = sorted({int(x) for x in rng.choice(pxs[:12], size=int(rng.integers(0, 4)))}, reverse=True)
                la = sorted({int(x) for x in rng.choice(pxs[10:], size=int(rng.integers(0, 4)))})
                bids = [(p, 0 if rng.random() < 0.1 else round(float(rng.uniform(1, 99)), 1)) for p in lv]
                asks = [(p, round(float(rng.uniform(1, 99)), 1)) for p in la]
                if rng.random() < 0.6:
                    raw.append(("b", ts + delay(), (ts, UP, bids, asks)))
                if rng.random() < 0.5:  # Down snapshot (book mirrored)
                    raw.append(("b", ts + delay(), (ts, DN, [(1000 - p, s) for p, s in asks],
                                                     [(1000 - p, s) for p, s in bids])))
        if rng.random() < 0.2:
            for _ in range(int(rng.integers(1, 3))):
                side, px = side_px()
                raw.append(("t", ts + delay(), (ts, UP if rng.random() < 0.5 else DN, side, px,
                                                 round(float(rng.uniform(1, 50)), 2))))
        if rng.random() < 0.02:
            size = float(rng.choice([0.01, 0.001]))
            r0 = ts + delay()
            raw.append(("k", r0, (ts, size)))
            raw.append(("k", r0 + int(rng.integers(0, 3)), (ts, size)))  # the other token's message
    # seq: arrival order per stream (ties at one recv_ms in random order)
    order = sorted(range(len(raw)), key=lambda i: (raw[i][1], rng.random()))
    seqs: dict[str, int] = {}
    rows = []
    for i in order:
        kind, recv, a = raw[i]
        s = seqs[kind] = seqs.get(kind, -1) + 1
        if kind == "c":
            rows.append(chg(recv, s, *a))
        elif kind == "b":
            rows.append(book(recv, s, *a))
        elif kind == "t":
            rows.append(trd(recv, s, *a))
        else:
            rows.append(tick(recv, *a))
    return rows


# ---------------------------------------------------------------------------------------- equivalence

@pytest.mark.parametrize("seed", range(6))
def test_equivalence_synthetic_basic(seed):
    rows = synth(seed)
    assert sum(r.stream == "pm_book" for r in rows) and sum(r.stream == "pm_trade" for r in rows)
    live = check(rows)
    assert len(live.tops.ts_ms) > 10 and len(live.trades.ts_ms) > 5


def test_equivalence_synthetic_coarse_ticks():
    """A coarser wall tick (one release step covering many ts_ms and recv_ms) gives the same data."""
    check(synth(11), step_ms=137)


def test_down_mirror_split_across_messages():
    """Review Focus 1: a Down row received 5 ms before its Up mirror (same ts_ms, separate messages). The mirror
    check runs on the complete ts_ms, so the Down row is dropped: one level event, not two."""
    t = WS - 5000
    rows = [
        book(t - 900, 0, t - 1000, UP, [(480, 100)], [(520, 50)]),
        chg(t + 100, 0, t, DN, "BUY", 500, 30, 480, 500),  # mirror of the Up SELL 500 below
        chg(t + 105, 1, t, UP, "SELL", 500, 30, 500, 520),
        chg(t + 110, 2, t + 1, UP, "SELL", 510, 7, 500, 510),
        chg(t + 108, 3, t + 1, DN, "BUY", 490, 7, 490, 500),  # mirror received first, different ts than the above
    ]
    live = check(rows)
    assert list(live.level_changes("SELL", 500, t - 1, t + 10, "ts")[0]) == [t]
    assert list(live.level_changes("SELL", 510, t - 1, t + 10, "ts")[0]) == [t + 1]
    assert live.size_at("SELL", 500, t + 104, "recv") == 0 and live.size_at("SELL", 500, t + 105, "recv") == 30
    # the Up top (500, 520) is first seen at the Up row's recv, not the Down row's
    assert live.top_at(t + 104).bid_px == 480 and live.top_at(t + 105).bid_px == 500


def test_unmirrored_down_used():
    t = WS - 5000
    rows = [
        book(t - 900, 0, t - 1000, UP, [(480, 100)], [(520, 50)]),
        chg(t + 100, 0, t, DN, "BUY", 470, 12, 470, 500),  # Up SELL 530, no Up mirror
        chg(t + 101, 1, t, UP, "BUY", 470, 9, 480, 520),  # unrelated Up row of the same ts
    ]
    live = check(rows)
    assert live.size_at("SELL", 530, t, "ts") == 12
    assert live.size_at("BUY", 470, t, "ts") == 9


def test_snapshot_zeroes_unlisted_levels():
    t = WS - 5000
    rows = [
        book(t + 100, 0, t, UP, [(480, 100), (470, 50)], [(520, 80), (530, 5)]),
        chg(t + 1100, 0, t + 1000, UP, "BUY", 460, 20, 480, 520),
        book(t + 2100, 1, t + 2000, UP, [(480, 30)], [(530, 0)]),  # 470, 460, 520 unlisted; 530 listed at 0
        book(t + 2105, 2, t + 2000, DN, [(500, 1)], []),  # ignored: an Up snapshot has this ts_ms
        book(t + 3200, 3, t + 3000, DN, [(500, 4)], [(560, 6)]),  # Down-only snapshot: Up ask 500, bid 440
    ]
    live = check(rows)
    for side, px in (("BUY", 470), ("BUY", 460), ("SELL", 520), ("SELL", 530)):
        assert live.size_at(side, px, t + 2000, "ts") == 0
    assert list(live.level_changes("BUY", 470, t, t + 5000, "ts")[2]) == [0.0]
    assert live.book_at(t + 3000, "ts") == ([(440, 6.0)], [(500, 4.0)])


def test_snapshot_after_same_ts_changes():
    t = WS - 5000
    rows = [
        book(t + 100, 0, t, UP, [(480, 100)], [(520, 80)]),
        chg(t + 1300, 0, t + 1000, UP, "BUY", 490, 15, 490, 520),  # received after the snapshot below
        book(t + 1200, 1, t + 1000, UP, [(480, 100)], [(520, 80)]),  # same ts_ms: applied after the change
        chg(t + 1250, 1, t + 1000, UP, "SELL", 520, 40, 480, 520),
    ]
    live = check(rows)
    assert live.size_at("BUY", 490, t + 1000, "ts") == 0  # the snapshot (after the change) does not list it
    assert live.size_at("SELL", 520, t + 1000, "ts") == 80
    assert live.top_at(t + 1000, "ts").bid_px == 480


def test_one_sided_top():
    t = WS - 5000
    rows = [
        book(t + 100, 0, t, UP, [(480, 100)], []),
        chg(t + 1100, 0, t + 1000, UP, "BUY", 490, 15, 490, None),  # empty best_ask -> 1000
        chg(t + 2100, 1, t + 2000, DN, "SELL", 300, 5, None, 300),  # unmirrored Down, empty best_bid -> ask 1000
        chg(t + 3100, 2, t + 3000, UP, "SELL", 700, 5, None, 700),  # empty best_bid -> 0
    ]
    live = check(rows)
    assert (live.top_at(t + 1000, "ts").bid_px, live.top_at(t + 1000, "ts").ask_px) == (490, 1000)
    assert (live.top_at(t + 2000, "ts").bid_px, live.top_at(t + 2000, "ts").ask_px) == (700, 1000)
    assert (live.top_at(t + 3000, "ts").bid_px, live.top_at(t + 3000, "ts").ask_px) == (0, 700)
    assert live.top_at(t + 100, "recv").ask_px == 1000


def test_recv_eff_out_of_order():
    """A later-ts change received earlier: on the recv clock the level is known only once every earlier change of
    it has arrived (running max)."""
    t = WS - 5000
    rows = [
        book(t + 100, 0, t, UP, [(480, 100)], [(520, 80)]),
        chg(t + 2400, 0, t + 1000, UP, "BUY", 480, 50, 480, 520),
        chg(t + 1200, 1, t + 1100, UP, "BUY", 480, 70, 480, 520),
        chg(t + 1300, 2, t + 1200, UP, "BUY", 480, 0, 470, 520),
    ]
    live = check(rows)
    lv = live._levels[("BUY", 480)]
    assert list(lv.recv_eff) == [t + 100, t + 2400, t + 2400, t + 2400]
    assert live.size_at("BUY", 480, t + 2399, "recv") == 100
    assert live.size_at("BUY", 480, t + 2400, "recv") == 0


def test_tick_change_dedup():
    t = WS - 5000
    rows = [
        book(t + 100, 0, t, UP, [(480, 100)], [(520, 80)]),
        tick(t + 1200, t + 1000, 0.001), tick(t + 1150, t + 1000, 0.001),  # both tokens: earliest recv wins
        tick(t + 1160, t + 1000, 0.01),  # same ts, another tick: kept
        tick(t + 3100, t + 3000, 0.01),
    ]
    live = check(rows)
    assert list(live.ticks.recv_ms) == [t + 1150, t + 1160, t + 3100]
    assert list(live.ticks.tick_px) == [1, 10, 10]
    assert live.tick_at(t + 1155, "recv") == 1 and live.tick_at(t + 1160, "recv") == 10
    assert live.tick_at(t + 1000, "ts") == 10


def test_ticks_not_range_filtered():
    """load_coin reads every tick size change of the condition (no ts range), so the live side keeps them too."""
    live = GrowingMarketData(INFO)
    old = WS - 5_000_000
    live.add_ts(old, [(old + 1, 0, (str(old), UP, "BUY", "0.5", "10", "0.5", "0.6"))], [], [], [(old + 5, 0.001)])
    live.release_recv(old + 10)
    assert not live.levels() and list(live.ticks.ts_ms) == [old] and live.tick_at(old + 5, "recv") == 1


def test_appending_never_changes_visible_answers():
    rows = synth(3)
    live = GrowingMarketData(INFO)
    feed = LiveFeed(live, rows)
    wall = rows[0].recv_ms
    mid = sorted(int(r.values[0]) for r in rows)[len(rows) // 2]
    while feed.T < mid:
        feed.step(wall)
        wall += 20
    lo = min(int(r.values[0]) for r in rows) - 50
    T, H = feed.T, feed.H
    levels = live.levels()

    def answers(t, clock):
        return (live.book_at(t, clock), dataclasses.astuple(live.top_at(t, clock)), live.tick_at(t, clock),
                [live.size_at(s, p, t, clock) for s, p in levels],
                [tuple(map(tuple, live.level_changes(s, p, lo, t, clock))) for s, p in levels])

    before = {(t, c): answers(t, c) for c, hi in (("recv", T), ("ts", H)) for t in grid(lo, hi, STEP)}
    bn = (len(live.trades.ts_ms), len(live.trades_recv.ts_ms))
    trades_before = [a.copy() for a in live.trades_recv]
    feed.run()
    assert len(live.trades.ts_ms) > bn[0]  # more data was appended
    after = {k: answers(*k) for k in before}
    for k in before:
        assert repr(before[k]) == repr(after[k]), k
    for a, b in zip(trades_before, live.trades_recv):
        assert np.array_equal(a, b[:len(a)])


def test_row_window_and_order_guards():
    live = GrowingMarketData(INFO)
    lo = WS - 2 * 300_000 - 600_000
    assert live.add_ts(lo - 1, [(lo + 100, 0, (str(lo - 1), UP, "BUY", "0.5", "10", "0.5", "0.6"))], [], [], []) == 0
    assert not live.levels()
    assert live.add_ts(lo, [], [], [(lo + 100, 0, (str(lo), UP, "BUY", "0.5", "10", "0", "0x"))], []) == 1
    hi = WS + 300_000 + 600_000
    assert live.add_ts(hi + 1, [], [], [(hi + 100, 1, (str(hi + 1), UP, "BUY", "0.5", "10", "0", "0x"))], []) == 0
    with pytest.raises(ValueError):
        live.add_ts(lo + 5, [], [], [], [])  # ts_ms going back
    with pytest.raises(ValueError):
        GrowingMarketData(INFO).add_ts(WS, [(WS, 0, (str(WS), "eth-updown-5m-1|U", "BUY", "0.5", "1", "", ""))],
                                       [], [], [])
    with pytest.raises(ValueError):
        GrowingMarketData(INFO).add_ts(WS, [(WS, 0, (str(WS + 1), UP, "BUY", "0.5", "1", "", ""))], [], [], [])
    live.release_recv(hi + 1000)
    with pytest.raises(ValueError):
        live.add_recv([("snap", lo, lo, 0, -1)])  # recv going back


def test_price_rounding_is_duckdbs():
    """round() half away from zero, as DuckDB (Python's round() is half to even)."""
    from polydata.bt.marketdata_live import _rnd

    xs = [0.0125 * 1000, 2.5, 3.5, 0.5, 1.4999999999999998, 514.9999999, 0.0, 999.5]
    got = duckdb.sql("SELECT " + ", ".join(f"round({x!r}::DOUBLE)::BIGINT" for x in xs)).fetchall()[0]
    assert [_rnd(x) for x in xs] == list(got)
    assert _rnd(-2.5) == -3


def test_growing_array():
    a = GrowingArray(np.int64, capacity=1)
    v0 = a.view
    for i in range(100):
        a.push(i)
    a.append([100, 101])
    a.append(102)
    assert list(a.view) == list(range(103)) and len(v0) == 0 and len(a) == 103


# ---------------------------------------------------------------------------------------- market info

def _meta_rows(slug: str, end: str, **over):
    market = {
        "slug": slug, "conditionId": "0xabc", "endDate": end,
        "outcomes": '["Up", "Down"]', "clobTokenIds": '["111", "222"]',
        "resolutionSource": "https://data.chain.link/streams/btc-usd-twap-60s-streams",
        "cryptoMarketConfig": {"asset": "btc", "twapLookbackSeconds": 60},
        "feeSchedule": {"rate": 0.072, "exponent": 1, "takerOnly": True, "rebateRate": 0.2},
        "orderPriceMinTickSize": 0.01, "orderMinSize": 5, **over,
    }
    return {"series": "btc-up-or-down-5m", "event": {"slug": slug, "markets": [market]}}, market


@pytest.mark.parametrize("resolved", [False, True])
def test_info_from_meta_matches_markets_view(tmp_path, monkeypatch, resolved):
    from dataclasses import asdict

    from test_pipeline import write_jsonl, write_tsv

    from polydata.build import build
    from polydata.bt.marketdata import load_coin
    from polydata.views import connect

    monkeypatch.setenv("POLYDATA_DIR", str(tmp_path))
    ws = 1795518000000  # 2026-11-24 11:00 UTC
    slug = f"btc-updown-5m-{ws // 1000}"
    raw = tmp_path / "raw" / "2026-11-24" / "11"
    d, market = _meta_rows(slug, "2026-11-24T11:05:00Z", orderPriceMinTickSize=0.001, orderMinSize=2.5)
    d2, _ = _meta_rows(slug, "2026-11-24T11:05:00Z", orderMinSize=9)  # an older discovery row: the latest wins
    write_jsonl(raw / f"pm_market_meta.{ws}.jsonl.zst", [{"recv_ms": ws - 600_000, "d": d2},
                                                        {"recv_ms": ws - 500_000, "d": d}])
    # build_markets needs a pm_resolution file: unresolved, it holds another market's resolution only
    rslug = slug if resolved else "eth-updown-5m-1795517700"
    _, rmarket = _meta_rows(rslug, "2026-11-24T11:00:00Z")
    write_jsonl(raw / f"pm_resolution.{ws}.jsonl.zst", [{"recv_ms": ws + 400_000, "d": {
        "slug": rslug, "outcome": "Up", "event": {"slug": rslug, "eventMetadata": {"priceToBeat": 60000.0},
                                                  "markets": [market if resolved else rmarket]}}}])
    write_tsv(raw / f"pm_book.{ws}.tsv.zst", ["recv_ms", "ts_ms", "asset", "bids", "asks", "hash"],
              [[ws + 500, ws + 400, f"{slug}|U", "0.48:100", "0.52:80", "h1"]])
    write_tsv(raw / f"pm_price_change.{ws}.tsv.zst",
              ["recv_ms", "ts_ms", "asset", "side", "price", "size", "best_bid", "best_ask"],
              [[ws + 600, ws + 500, f"{slug}|U", "BUY", "0.48", "10", "0.48", "0.52"]])
    write_tsv(raw / f"pm_trade.{ws}.tsv.zst",
              ["recv_ms", "ts_ms", "asset", "side", "price", "size", "fee_rate_bps", "tx_hash"],
              [[ws + 700, ws + 600, f"{slug}|U", "BUY", "0.52", "5", "0", "0x1"]])
    build()
    con = connect()
    want = asdict(load_coin(con, "btc", ["5m"], ws, ws + 300_000)[slug].info)
    got = asdict(info_from_meta(d, ws - 500_000))
    assert got["outcome"] is None and got["resolution_recv_ms"] is None and got["price_to_beat"] is None
    skip = {"outcome", "resolution_recv_ms", "price_to_beat"} if resolved else set()
    assert {k: v for k, v in got.items() if k not in skip} == {k: v for k, v in want.items() if k not in skip}
    assert got["tick_px"] == 1 and got["min_order_size"] == 2.5 and got["window_end_ms"] == ws + 300_000
    if resolved:
        assert want["outcome"] == "Up" and want["price_to_beat"] == 60000.0


def test_info_from_meta_rejects_incomplete():
    d, _ = _meta_rows("btc-updown-5m-1795518000", None)
    with pytest.raises(Exception):
        info_from_meta(d, 1)


# ---------------------------------------------------------------------------------------- Binance / Chainlink

def _bn_rows(rng, start_ms: int, end_ms: int, gaps=(), spacing=(1, 7)):
    """Raw bn_fut_bbo rows (recv_ms, update_id, bid, bid_qty, ask, ask_qty): ~every 3 ms, with silent gaps,
    shared recv_ms and update ids out of order at one recv_ms."""
    rows, t, uid, mid = [], start_ms, 1000, 60000.0
    while t < end_ms:
        if not any(a <= t < b for a, b in gaps):
            for _ in range(int(rng.integers(1, 3))):
                mid *= math.exp(rng.normal(0, 2e-5))
                uid += int(rng.integers(1, 4))
                rows.append((t, uid if rng.random() < 0.9 else uid - 5, round(mid - 0.05, 2), float(rng.uniform(0, 5)),
                             round(mid + 0.05, 2), float(rng.uniform(0, 5))))
        t += int(rng.integers(*spacing))
    return rows


def _sql_bundles(rows, s, e, step=10) -> _Bn:
    """engine._load_bn over the same rows."""
    con = duckdb.connect()
    df = pd.DataFrame(rows, columns=["recv_ms", "update_id", "bid", "bid_qty", "ask", "ask_qty"])
    con.execute("CREATE TABLE b AS SELECT * FROM df")
    d = con.execute(f"""SELECT recv_ms // {step} AS b,
               arg_max(bid, (recv_ms, update_id)) AS bid, arg_max(ask, (recv_ms, update_id)) AS ask,
               arg_max(bid_qty, (recv_ms, update_id)) AS bid_qty, arg_max(ask_qty, (recv_ms, update_id)) AS ask_qty
        FROM b WHERE recv_ms >= ? AND recv_ms < ? GROUP BY ALL ORDER BY b""", [s, e]).fetchnumpy()
    return _Bn(d["b"].astype(np.int64) * step + step - 1, d["bid"].astype(float), d["ask"].astype(float),
               d["bid_qty"].astype(float), d["ask_qty"].astype(float))


def test_live_bn_matches_sql_bundles():
    rng = np.random.default_rng(5)
    s = 1795518000000
    rows = _bn_rows(rng, s, s + 20_000)
    bn = LiveBn(10)
    T = s - 1
    for r in rows:
        while T + 7 < r[0]:  # the strategy clock moves in steps; buckets are final once T reaches their last ms
            T += 7
            bn.flush(T)
            assert len(bn.recv) == 0 or bn.recv[-1] <= T
        bn.add(*r)
    bn.flush(s + 20_000)
    want = _sql_bundles(rows, s, s + 20_000)
    for f in _Bn._fields:
        assert np.array_equal(getattr(bn, f), getattr(want, f)), f


def test_live_cl_first_receipt():
    rs = 10_000
    cl = LiveCl(rs)
    assert cl.add([(9_990, 9_000, 1.0), (9_995, 9_500, 2.0)]) == 0  # first received before rs: never visible
    assert cl.add([(10_000, 9_500, 2.5), (10_000, 9_800, 3.0), (10_000, 9_700, 4.0)]) == 2
    assert cl.add([(10_050, 9_800, 9.0), (10_060, 10_000, 5.0)]) == 1
    r, t, p = cl.arrays
    assert list(r) == [10_000, 10_000, 10_060] and list(t) == [9_700, 9_800, 10_000] and list(p) == [4.0, 3.0, 5.0]


def test_live_grid_bitwise():
    """LiveGrid fed second by second equals _Grid fed whole hours, bit for bit, at every second (data starts late
    in the first hour, gaps of minutes, three hour segments starting mid-hour)."""
    rng = np.random.default_rng(1)
    rs = 1795515000000  # 10:10 UTC: the first segment is [rs, 11:00)
    ends = [1795518000000, 1795521600000, 1795523400000]
    rows = _bn_rows(rng, rs + 37_400, ends[-1], gaps=((1795516000000, 1795516200000),
                                                      (1795517999000, 1795518061000)), spacing=(20, 700))
    ref, live = _Grid(), LiveGrid()
    s, i, T = rs, 0, rs - 1
    live.start_segment(s // 1000)
    bn = LiveBn(10)
    for e in ends:
        ref.append(s // 1000, -(-e // 1000), _sql_bundles(rows, s, e))
        while T < e - 1:
            T = min(T + 1000, e - 1)  # one completed second per step (s is a whole second)
            while i < len(rows) and rows[i][0] <= T:
                bn.add(*rows[i])
                i += 1
            bn.flush(T)
            live.set_bundles(bn, (T + 1) // 1000)
            for t in (T, T - 1, T - 999):
                assert live.sig15(t) == ref.sig15(t), t  # ref already holds the whole hour
        s = e
        bn = LiveBn(10)
        live.start_segment(s // 1000)
    vals = 0
    for t in range(rs, ends[-1] + 2000, 1000):
        for dt in (0, 1, 999):
            a, b = live.sig15(t + dt), ref.sig15(t + dt)
            assert a == b, t + dt
            vals += a is not None
    assert vals > 4000
    assert live.sec0 == ref.sec0 and np.array_equal(live.mids, ref.mids) and np.array_equal(live.csum, ref.csum)

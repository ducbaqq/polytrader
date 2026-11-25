"""Real-data equivalence of the live market data (run with POLYDATA_REALDATA=1 and POLYDATA_DIR set): raw recorder
rows of 2026-11-24 10:00-13:00, fed in receive order the way the live runner releases them, give exactly
`load_coin`'s MarketData (every array, and the strategy queries every 50 ms as they become visible), and the live
Binance grid gives `_Grid`'s sig15 bit for bit."""

from __future__ import annotations

import os
import sys
from pathlib import Path

import numpy as np
import pytest

pytestmark = pytest.mark.skipif(os.environ.get("POLYDATA_REALDATA") != "1", reason="needs POLYDATA_REALDATA=1")
sys.path.insert(0, str(Path(__file__).parents[1]))  # tests/bt helpers

START_MS, END_MS = 1795518000000, 1795521600000  # 2026-11-24 11:00-12:00 UTC
HOURS = ("10", "11", "12")
STREAMS = {  # recorder columns after recv_ms
    "pm_price_change": ["ts_ms", "asset", "side", "price", "size", "best_bid", "best_ask"],
    "pm_book": ["ts_ms", "asset", "bids", "asks", "hash"],
    "pm_trade": ["ts_ms", "asset", "side", "price", "size", "fee_rate_bps", "tx_hash"],
}
HOLD_BACK, SKEW, QUERY_STEP = 3000, 1000, 50


@pytest.fixture(scope="module")
def con():
    import polydata

    return polydata.connect()


def _hour_files(stream: str, hour: str) -> list[Path]:
    from polydata.build import _has_rows
    from polydata.config import paths

    d = paths().raw / "2026-11-24" / hour
    return [f for f in sorted(d.glob(f"{stream}.*.zst")) if _has_rows(f)]


def _raw_rows(stream: str, prefix: str):
    """(recv_ms, seq, values) of every raw row whose asset starts with `prefix`; seq as the build numbers it
    (raw order within the stream-hour)."""
    import pyarrow.compute as pc

    from polydata import schema
    from polydata.build import _read_tsv

    cols = STREAMS[stream]
    out = []
    for hour in HOURS:
        offset = 0
        for f in _hour_files(stream, hour):
            tbl = _read_tsv(f, schema.TSV[stream])
            n = tbl.num_rows
            seq = np.arange(offset, offset + n)
            offset += n
            keep = pc.starts_with(tbl["asset"], prefix)
            idx = np.flatnonzero(keep.to_numpy(zero_copy_only=False))
            if not len(idx):
                continue
            sub = tbl.take(idx)
            recv = [int(x) for x in sub["recv_ms"].to_pylist()]
            vals = list(zip(*(sub[c].to_pylist() if c in sub.column_names else [None] * len(idx) for c in cols)))
            out.extend(zip(recv, seq[idx].tolist(), vals))
    return out


def _raw_ticks(condition_id: str):
    import duckdb

    files = [f.as_posix() for h in HOURS for f in _hour_files("pm_other", h)]
    return duckdb.execute(
        """SELECT recv_ms, (d->>'timestamp')::BIGINT, (d->>'new_tick_size')::DOUBLE
           FROM read_json(?, format='newline_delimited', columns={'recv_ms': 'BIGINT', 'd': 'JSON'})
           WHERE d->>'event_type' = 'tick_size_change' AND d->>'market' = ?""", [files, condition_id]).fetchall()


@pytest.mark.parametrize("coin", ["btc", "sol"])
def test_real_hour_equivalence(con, coin):
    from live_feed import LiveFeed, Row, compare_arrays, compare_queries, grid

    from polydata.bt.marketdata import LOOKBACK_WINDOWS, MARGIN_MS, load_coin
    from polydata.bt.marketdata_live import GrowingMarketData

    slugs = sorted(load_coin(con, coin, ["5m"], START_MS, END_MS))
    assert len(slugs) >= 12
    raw = {s: _raw_rows(s, f"{coin}-updown-5m-") for s in STREAMS}
    checked = 0
    for slug in slugs:
        ref = load_coin(con, coin, ["5m"], START_MS, END_MS, slugs={slug})[slug]  # this market's own row range
        info = ref.info
        length = info.window_end_ms - info.window_start_ms
        lo, hi = info.window_start_ms - LOOKBACK_WINDOWS * length - MARGIN_MS, info.window_end_ms + MARGIN_MS
        rows = [Row(s, r, q, v) for s in STREAMS for r, q, v in raw[s] if v[1].startswith(f"{slug}|")]
        rows += [Row("tick", r, 0, (str(ts), str(size))) for r, ts, size in _raw_ticks(info.condition_id)]
        # The live runner drops a row delayed by more than the hold-back (and voids the market): a market with
        # such rows is fed with a longer hold-back here, so every market is still compared.
        delay = max(r.recv_ms - int(r.values[0]) for r in rows)
        hold = HOLD_BACK if delay < HOLD_BACK - 20 else delay + 100
        live = GrowingMarketData(info)
        feed = LiveFeed(live, rows, hold, SKEW)
        t_lo = lo - 1
        n = [0]

        def on_step(H_prev, H, T_prev, T):
            for clock, a, b in (("recv", T_prev, T), ("ts", H_prev, H)):
                ts = grid(max(a, t_lo), b, QUERY_STEP)
                if len(ts):
                    n[0] += compare_queries(live, ref, ts, clock, levels=[], depth=5)

        feed.run(50, on_step)
        compare_arrays(live, ref)
        assert feed.trades_added == len(ref.trades.ts_ms)
        print(f"{slug}: {len(rows)} rows, hold-back {hold} ms, {n[0]} query times, "
              f"{len(ref.levels())} levels, {len(ref.trades.ts_ms)} trades, {len(ref.ticks.ts_ms)} ticks")
        checked += 1
    assert checked == len(slugs)


def test_real_grid_bitwise(con):
    """bn_fut_bbo BTCUSDT 10:00-12:00 (two hour segments): LiveBn from the raw rows + LiveGrid second by second
    against the replay's SQL bundles + _Grid."""
    from polydata import schema
    from polydata.build import _read_tsv
    from polydata.bt.core import _Bn, _Grid
    from polydata.bt.marketdata_live import LiveBn, LiveGrid

    sym, step, rs = "BTCUSDT", 10, START_MS - 3_600_000
    rows = []
    for hour in HOURS[:2]:
        for f in _hour_files("bn_fut_bbo", hour):
            t = _read_tsv(f, schema.TSV["bn_fut_bbo"]).to_pydict()
            rows += [(int(t["recv_ms"][i]), int(t["update_id"][i]), float(t["bid"][i]), float(t["bid_qty"][i]),
                      float(t["ask"][i]), float(t["ask_qty"][i])) for i in range(len(t["sym"])) if t["sym"][i] == sym]
    rows.sort(key=lambda r: r[0])  # stable: raw order within a recv_ms
    ref, live = _Grid(), LiveGrid()
    i, s, T = 0, rs, rs - 1
    live.start_segment(s // 1000)
    for e in (START_MS, END_MS):
        d = con.execute(
            f"""SELECT recv_ms // {step} AS b,
                       arg_max(bid, (recv_ms, update_id)) AS bid, arg_max(ask, (recv_ms, update_id)) AS ask,
                       arg_max(bid_qty, (recv_ms, update_id)) AS bid_qty, arg_max(ask_qty, (recv_ms, update_id)) AS ask_qty
                FROM bn_fut_bbo WHERE date = '2026-11-24' AND sym = ? AND recv_ms >= ? AND recv_ms < ?
                GROUP BY ALL ORDER BY b""", [sym, s, e]).fetchnumpy()
        want = _Bn(d["b"].astype(np.int64) * step + step - 1, d["bid"].astype(float), d["ask"].astype(float),
                   d["bid_qty"].astype(float), d["ask_qty"].astype(float))
        ref.append(s // 1000, -(-e // 1000), want)
        bn = LiveBn(step)
        while T < e - 1:
            T += 1000
            while i < len(rows) and rows[i][0] <= T:
                bn.add(*rows[i])
                i += 1
            bn.flush(T)
            live.set_bundles(bn, (T + 1) // 1000)
            assert live.sig15(T) == ref.sig15(T), T
        for f in _Bn._fields:
            assert np.array_equal(getattr(bn, f), getattr(want, f)), f
        s = e
        live.start_segment(s // 1000)
    n = 0
    for t in range(rs, END_MS + 1000, 1000):
        a = live.sig15(t)
        assert a == ref.sig15(t), t
        n += a is not None
    assert n > 6000
    assert np.array_equal(live.csum, ref.csum) and np.array_equal(live.mids, ref.mids)

"""Real-data checks of the combined Up-terms book (run with POLYDATA_REALDATA=1 and POLYDATA_DIR set)."""

from __future__ import annotations

import os
import time

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("POLYDATA_REALDATA") != "1", reason="needs POLYDATA_REALDATA=1")

START_MS, END_MS = 1795518000000, 1795521600000  # 2026-11-24 11:00-12:00 UTC


@pytest.fixture(scope="module")
def con():
    import polydata

    return polydata.connect()


@pytest.fixture(scope="module")
def markets(con):
    from polydata.bt.marketdata import load_coin

    out = {}
    for coin in ("btc", "sol"):
        out.update({s: md for s, md in load_coin(con, coin, ["5m"], START_MS, END_MS).items()
                    if START_MS <= md.info.window_start_ms < END_MS})
    return out


def test_down_rows_have_up_mirror(con):
    n, mirrored = con.execute("""
        WITH c AS (SELECT slug, outcome, ts_ms, side, round(price * 1000)::INT AS px, size FROM pm_price_change
                   WHERE (asset LIKE 'btc-updown-5m-%' OR asset LIKE 'sol-updown-5m-%') AND ts_ms BETWEEN ? AND ?)
        SELECT count(*), count(*) FILTER (WHERE EXISTS (
            SELECT 1 FROM c u WHERE u.outcome = 'Up' AND u.slug = d.slug AND u.ts_ms = d.ts_ms
              AND u.px = 1000 - d.px AND u.side <> d.side AND u.size = d.size))
        FROM c d WHERE d.outcome = 'Down'""", [START_MS, END_MS]).fetchall()[0]
    print(f"Down rows with an Up mirror: {mirrored}/{n} = {mirrored / n:.5f}")
    assert n > 0 and mirrored / n >= 0.999


# Gate: the default (lenient) rate >= 99.5 %: a row also matches a book state within the same ts_ms,
# because pm_best_bid_ask is published per change and can show the book halfway through a message
# that changes several levels at once (or before same-ms changes). The strict rate is printed too.
# 1000 rows per market rather than 200: at 200 a single miss is 0.5 %, so the 99.5 % gate would
# flip on sampling noise (seed 0 at n=200 gives 199/200 on one market; every market is >= 99.7 % at 1000).
N_ROWS = 1000


def test_book_reconstruction(con, markets):
    from polydata.bt.marketdata import reconstruction_rate

    assert len(markets) == 24
    bad = {}
    for slug, md in sorted(markets.items()):
        rate = reconstruction_rate(con, md, n=N_ROWS, seed=0)
        strict = reconstruction_rate(con, md, n=N_ROWS, seed=0, strict=True)
        print(f"{slug}: {rate:.3f} (strict {strict:.3f})")
        if rate < 0.995:
            bad[slug] = rate
    assert not bad


def test_trades_are_taker_side(markets):
    """An Up-terms taker BUY at p consumes asks: the Up SELL level at p shrinks within +-50 ms."""
    near, other, n = 0, 0, 0
    for md in markets.values():
        for i in range(0, len(md.trades.ts_ms), 3):
            t = md.trade(i)
            consumed = "SELL" if t.up_side == "BUY" else "BUY"
            for side in (consumed, "BUY" if consumed == "SELL" else "SELL"):
                _, _, size = md.level_changes(side, t.up_px, t.ts_ms - 50, t.ts_ms + 50)
                drop = len(size) > 0 and size.min() < md.size_at(side, t.up_px, t.ts_ms - 51)
                if side == consumed:
                    near += drop
                else:
                    other += drop
            n += 1
    print(f"trades {n}: consumed-side level drops {near / n:.3f}, opposite side {other / n:.3f}")
    assert near / n > 0.7 and other / n < 0.05


def test_load_coin_speed(con):
    from polydata.bt.marketdata import load_coin

    t = time.perf_counter()
    mds = load_coin(con, "btc", ["5m", "15m"], START_MS, END_MS)
    dt = time.perf_counter() - t
    print(f"load_coin(btc, 5m+15m, 1 h): {dt:.1f} s, {len(mds)} markets")
    assert mds and dt <= 25

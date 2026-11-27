"""Thin-market screen: price the 1h / 4h / daily market off the liquid market ending at the same moment
(docs/strategies/thin-markets.md, pre-registered rule).

usage: POLYDATA_DIR=... uv run --project analysis python research/thin_markets/screen.py DAY OUT_DIR
  DAY = 2026-11-24 (choose) or 2026-11-27 (report). Writes OUT_DIR/grid_DAY.parquet (one row per pair x 5 s point).
  Then `score.py` reads the grid files.
"""

from __future__ import annotations

import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from polydata.views import connect

DAYS = {
    # day: (first end, last end, earliest read)
    "2026-11-24": ("2026-11-24 10:40", "2026-11-25 00:00", "2026-11-24 00:00"),
    "2026-11-27": ("2026-11-27 00:05", "2026-11-27 09:50", "2026-11-27 00:00"),
}
LAT_MS = 200
HOLD_MS = {"1h": 250}  # taker hold on 1h; none on 4h / 1d (docs/FEES.md, sim config)


def ms(s: str) -> int:
    return int(datetime.strptime(s, "%Y-%m-%d %H:%M").replace(tzinfo=timezone.utc).timestamp() * 1000)


def build(day: str, out: Path) -> None:
    lo_end, hi_end, read_lo = (ms(x) for x in DAYS[day])
    con = connect()
    con.execute("SET preserve_insertion_order = false")
    t0 = time.time()

    def step(msg: str, sql: str) -> None:
        t = time.time()
        con.execute(sql)
        print(f"  {msg:<40} {time.time() - t:5.1f}s", flush=True)

    # thin market starts on the report day itself for 11-27 (no read of 11-26)
    start_ok = f"AND (timeframe IN ('5m','15m') OR window_start_ms >= {read_lo})" if day == "2026-11-27" else ""
    step("markets", f"""
        CREATE TEMP TABLE mk AS
        SELECT slug, coin, timeframe, window_start_ms, window_end_ms, price_to_beat, fee_rate,
               (outcome = 'Up')::INT AS outcome_up
        FROM markets
        WHERE outcome IS NOT NULL AND timeframe IN ('5m','15m','1h','4h','1d')
          AND window_end_ms BETWEEN {lo_end} AND {hi_end} AND window_end_ms % 3600000 = 0 {start_ok}
    """)
    # 4h strike: mean of Chainlink ticks in the 60 s before the window start
    step("4h strikes", f"""
        UPDATE mk SET price_to_beat = k.kk FROM (
            SELECT m.slug, avg(c.price) AS kk FROM mk m JOIN chainlink c ON c.coin = m.coin
              AND c.ts_ms >= m.window_start_ms - 60000 AND c.ts_ms < m.window_start_ms
            WHERE m.timeframe = '4h' GROUP BY 1) k
        WHERE mk.slug = k.slug AND mk.price_to_beat IS NULL
    """)
    step("pairs + grid", """
        CREATE TEMP TABLE grid AS
        WITH r AS (SELECT * FROM (VALUES ('5m',1),('15m',2),('1h',3),('4h',4),('1d',5)) v(tf, rk))
        SELECT t.slug AS t_slug, t.coin, t.timeframe AS t_tf, l.slug AS l_slug, l.timeframe AS l_tf,
               t.window_end_ms AS end_ms, t.price_to_beat AS k_t, l.price_to_beat AS k_l, t.fee_rate AS t_fee,
               t.outcome_up AS t_out, l.outcome_up AS l_out,
               t.window_end_ms - 1000 * s AS t_ms, s AS tau_s, upper(t.coin) || 'USDT' AS sym
        FROM mk t JOIN mk l ON t.coin = l.coin AND t.window_end_ms = l.window_end_ms
        JOIN r rt ON rt.tf = t.timeframe JOIN r rl ON rl.tf = l.timeframe,
        range(5, 301, 5) g(s)
        WHERE t.timeframe IN ('1h','4h','1d') AND rl.rk < rt.rk
          AND t.price_to_beat IS NOT NULL AND l.price_to_beat IS NOT NULL
    """)
    step("pm tops", f"""
        CREATE TEMP TABLE pm AS
        SELECT slug, recv_ms, best_bid, best_ask FROM pm_best_bid_ask
        WHERE outcome = 'Up' AND recv_ms >= {read_lo} AND recv_ms <= {hi_end}
          AND slug IN (SELECT slug FROM mk)
    """)
    lo_bn = lo_end - 20 * 60000
    step("binance 1 s mids (last 20 min of hours)", f"""
        CREATE TEMP TABLE bn_sec AS
        SELECT sym, recv_ms // 1000 AS sec, arg_max((bid + ask) / 2, recv_ms) AS mid
        FROM bn_fut_bbo
        WHERE recv_ms BETWEEN {max(lo_bn, read_lo)} AND {hi_end} AND recv_ms % 3600000 >= 3600000 - 20 * 60000
          AND sym IN (SELECT DISTINCT sym FROM grid)
        GROUP BY ALL
    """)
    step("sig15 at grid points", """
        CREATE TEMP TABLE vol AS
        WITH pts AS (SELECT DISTINCT sym, t_ms FROM grid),
        w AS (SELECT p.sym, p.t_ms, b.sec, b.mid FROM pts p JOIN bn_sec b ON b.sym = p.sym
              AND b.sec >= p.t_ms // 1000 - 901 AND b.sec < p.t_ms // 1000),
        r AS (SELECT sym, t_ms, ln(mid / lag(mid) OVER (PARTITION BY sym, t_ms ORDER BY sec)) AS r,
                     sec - lag(sec) OVER (PARTITION BY sym, t_ms ORDER BY sec) AS dt FROM w)
        SELECT sym, t_ms, sqrt(sum(r * r) / 900.0) AS sig15, count(r) AS n_ret FROM r GROUP BY 1, 2
    """)
    step("as-of joins", f"""
        CREATE TEMP TABLE out AS
        SELECT g.*, v.sig15, v.n_ret,
               l.best_bid AS l_bid, l.best_ask AS l_ask,
               t.best_bid AS t_bid, t.best_ask AS t_ask, t.recv_ms AS t_recv_ms,
               x.best_bid AS x_bid, x.best_ask AS x_ask
        FROM (SELECT *, t_ms + {LAT_MS} + CASE WHEN t_tf = '1h' THEN {HOLD_MS['1h']} ELSE 0 END AS x_ms FROM grid) g
        LEFT JOIN vol v USING (sym, t_ms)
        ASOF LEFT JOIN pm l ON g.l_slug = l.slug AND g.t_ms >= l.recv_ms
        ASOF LEFT JOIN pm t ON g.t_slug = t.slug AND g.t_ms >= t.recv_ms
        ASOF LEFT JOIN pm x ON g.t_slug = x.slug AND g.x_ms >= x.recv_ms
    """)
    out.mkdir(parents=True, exist_ok=True)
    f = out / f"grid_{day}.parquet"
    con.execute(f"COPY out TO '{f}' (FORMAT parquet)")
    n = con.execute("SELECT count(*), count(DISTINCT t_slug) FROM out").fetchone()
    print(f"{day}: {n[0]} rows, {n[1]} thin markets -> {f} ({time.time() - t0:.0f}s)")


if __name__ == "__main__":
    build(sys.argv[1], Path(sys.argv[2]))

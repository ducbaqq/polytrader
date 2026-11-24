"""Point-in-time snapshots of every resolved Up/Down market, for calibration and fair-value research.

One row per (market, decision time t), every STEP_S seconds from window start to just before the end.
Every feature uses only data we had *received* by t (as-of joins on `recv_ms`), so nothing looks ahead:

  up_bid/up_ask/dn_bid/dn_ask   Polymarket top of book of the Up and Down tokens
  cl_px, cl_ts_ms               last Chainlink price (the resolution source) and its timestamp
  cl_obs_avg, cl_obs_n          average of the Chainlink prices so far inside the settlement window
                                [end - 60 s, t) (NULL before it starts)
  bn_mid, bn_mid_at_cl          Binance USDⓈ-M futures mid at t, and at the Chainlink price's timestamp
  sig15/sig60                   realised vol of Binance 1 s log returns over the last 15/60 min (per √s)
  up_ask_300/dn_ask_300 (_1000)  asks 300 ms / 1 s after t: what a taker order sent at t would meet
                                after that much latency
  outcome_up                    1 if the market resolved Up

Output: data/research/snapshots.parquet (rebuilt by `build()`).
"""

from __future__ import annotations

import time

import duckdb

from ..config import paths
from ..views import connect

STEP_S = 5
DEV_END_MS = 1795456000000  # 2026-11-23 17:46:40 UTC: end of the development period (see data quality log)
TIMEFRAMES = ("5m", "15m", "1h")


def build(step_s: int = STEP_S) -> str:
    out = paths().root / "research" / "snapshots.parquet"
    out.parent.mkdir(parents=True, exist_ok=True)
    con = connect()
    con.execute("SET preserve_insertion_order = false")
    t0 = time.time()

    def step(msg: str, sql: str) -> None:
        t = time.time()
        con.execute(sql)
        print(f"  {msg:<45} {time.time() - t:5.1f}s")

    tf = ", ".join(f"'{x}'" for x in TIMEFRAMES)
    step("markets", f"""
        CREATE TEMP TABLE mk AS
        SELECT slug, coin, timeframe, window_start_ms, window_end_ms, price_to_beat, final_price, fee_rate,
               (outcome = 'Up')::INT AS outcome_up, upper(coin) || 'USDT' AS sym
        FROM markets
        WHERE outcome IS NOT NULL AND price_to_beat IS NOT NULL AND timeframe IN ({tf})
          AND window_start_ms - 3600000 >= {DEV_END_MS}  -- need 60 min of history for sig60
    """)
    lo, hi = con.execute("SELECT min(window_start_ms) - 3700000, max(window_end_ms) + 60000 FROM mk").fetchall()[0]

    # Binance futures mid per second (last update received in that second), then on a full second grid.
    step("binance 1s mids", f"""
        CREATE TEMP TABLE bn_sec AS
        SELECT sym, recv_ms // 1000 AS sec, arg_max((bid + ask) / 2, recv_ms) AS mid
        FROM bn_fut_bbo WHERE recv_ms BETWEEN {lo} AND {hi} AND sym IN (SELECT DISTINCT sym FROM mk)
        GROUP BY ALL
    """)
    step("binance 1s grid + returns", f"""
        CREATE TEMP TABLE bn_grid AS
        WITH g AS (SELECT s.sym, sec FROM (SELECT DISTINCT sym FROM mk) s,
                   range({lo // 1000}, {hi // 1000}) t(sec)),
        f AS (SELECT g.sym, g.sec, b.mid FROM g ASOF LEFT JOIN bn_sec b ON g.sym = b.sym AND g.sec >= b.sec)
        SELECT sym, sec, mid, ln(mid / lag(mid) OVER (PARTITION BY sym ORDER BY sec)) AS r FROM f
    """)
    step("realised vol (15 / 60 min)", """
        CREATE TEMP TABLE bn_vol AS
        SELECT sym, sec, mid,
               sqrt(avg(r * r) OVER (PARTITION BY sym ORDER BY sec ROWS BETWEEN 899 PRECEDING AND CURRENT ROW)) AS sig15,
               sqrt(avg(r * r) OVER (PARTITION BY sym ORDER BY sec ROWS BETWEEN 3599 PRECEDING AND CURRENT ROW)) AS sig60
        FROM bn_grid
    """)
    step("chainlink (dedup)", f"""
        CREATE TEMP TABLE cl AS
        SELECT coin, ts_ms, min(recv_ms) AS recv_ms, any_value(price) AS price
        FROM chainlink WHERE ts_ms BETWEEN {lo} AND {hi} GROUP BY ALL
    """)
    step("polymarket top of book", f"""
        CREATE TEMP TABLE pm AS
        SELECT slug, outcome, recv_ms, best_bid, best_ask FROM pm_best_bid_ask
        WHERE slug IN (SELECT slug FROM mk) AND recv_ms BETWEEN {lo} AND {hi}
    """)
    step("decision-time grid", f"""
        CREATE TEMP TABLE grid AS
        SELECT mk.*, t AS t_ms, (window_end_ms - t) / 1000.0 AS tau_s
        FROM mk, range(window_start_ms, window_end_ms, {step_s * 1000}) r(t)
    """)
    step("as-of joins", """
        CREATE TEMP TABLE snap AS
        SELECT g.*,
               u.best_bid AS up_bid, u.best_ask AS up_ask, u.recv_ms AS up_recv_ms,
               d.best_bid AS dn_bid, d.best_ask AS dn_ask,
               c.price AS cl_px, c.ts_ms AS cl_ts_ms,
               v.mid AS bn_mid, v.sig15, v.sig60
        FROM grid g
        ASOF LEFT JOIN (SELECT * FROM pm WHERE outcome = 'Up') u ON g.slug = u.slug AND g.t_ms >= u.recv_ms
        ASOF LEFT JOIN (SELECT * FROM pm WHERE outcome = 'Down') d ON g.slug = d.slug AND g.t_ms >= d.recv_ms
        ASOF LEFT JOIN cl c ON g.coin = c.coin AND g.t_ms >= c.recv_ms
        ASOF LEFT JOIN bn_vol v ON g.sym = v.sym AND g.t_ms // 1000 - 1 >= v.sec   -- last *complete* second
    """)
    step("binance mid at chainlink timestamp", """
        CREATE TEMP TABLE snap2 AS
        SELECT s.*, b.mid AS bn_mid_at_cl
        FROM snap s ASOF LEFT JOIN bn_grid b ON s.sym = b.sym AND s.cl_ts_ms // 1000 >= b.sec
    """)
    # Average of Chainlink prices already observed inside the settlement window [end - 60 s, t).
    step("observed settlement average", """
        CREATE TEMP TABLE snap3 AS
        SELECT s.*, o.cl_obs_avg, coalesce(o.cl_obs_n, 0) AS cl_obs_n
        FROM snap2 s LEFT JOIN (
            SELECT s.slug, s.t_ms, avg(c.price) AS cl_obs_avg, count(*) AS cl_obs_n
            FROM snap2 s JOIN cl c ON c.coin = s.coin
             AND c.ts_ms >= s.window_end_ms - 60000 AND c.ts_ms < s.t_ms AND c.recv_ms <= s.t_ms
            WHERE s.tau_s <= 60
            GROUP BY ALL
        ) o USING (slug, t_ms)
    """)
    step("execution prices after latency", """
        CREATE TEMP TABLE snap4 AS
        SELECT s.*, u3.best_ask AS up_ask_300, d3.best_ask AS dn_ask_300, u1.best_ask AS up_ask_1000, d1.best_ask AS dn_ask_1000
        FROM snap3 s
        ASOF LEFT JOIN (SELECT * FROM pm WHERE outcome = 'Up') u3 ON s.slug = u3.slug AND s.t_ms + 300 >= u3.recv_ms
        ASOF LEFT JOIN (SELECT * FROM pm WHERE outcome = 'Down') d3 ON s.slug = d3.slug AND s.t_ms + 300 >= d3.recv_ms
        ASOF LEFT JOIN (SELECT * FROM pm WHERE outcome = 'Up') u1 ON s.slug = u1.slug AND s.t_ms + 1000 >= u1.recv_ms
        ASOF LEFT JOIN (SELECT * FROM pm WHERE outcome = 'Down') d1 ON s.slug = d1.slug AND s.t_ms + 1000 >= d1.recv_ms
    """)
    con.execute(f"COPY (SELECT * FROM snap4 ORDER BY window_start_ms, slug, t_ms) TO '{out}' (FORMAT parquet)")
    n, m = con.execute(f"SELECT count(*), count(DISTINCT slug) FROM '{out}'").fetchall()[0]
    print(f"snapshots: {n:,} rows, {m:,} markets -> {out} ({time.time() - t0:.0f}s)")
    return str(out)


if __name__ == "__main__":
    build()

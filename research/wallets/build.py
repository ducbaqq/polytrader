"""Per-trade table for the wallet study (docs/research/wallets.md).

usage: POLYDATA_DIR=... uv run --project analysis python research/wallets/build.py OUT.parquet

One row per Data API trade (taker order) of the 5m/15m/1h markets with window_start in
[2026-11-24 10:36, 2026-11-25 00:00) UTC: wallet, side, token, price, size, t_tr (WS receive time of the
matched pm_trade, else block time), settlement edge, combined-book mids at t_tr + 10 s / 60 s (capped at the
window end), and the copy entry (combined ask of the token we would buy, spread) at T = t_tr + L + 200 ms for
L = 0 and L = block time + 1 s - t_tr (>= 0), plus the mid 60 s after each entry. Every book value is an ASOF
join on recv_ms. Outcome is only used for the label columns (`won`, `edge_settle`).
"""

from __future__ import annotations

import sys
import time

from polydata import connect

LO_MS = 1795516560000  # 2026-11-24 10:36 UTC
HI_MS = 1795564800000  # 2026-11-25 00:00 UTC (sealed day starts; never read)
HALF_MS = 1795540500000  # 2026-11-24 17:15 UTC
ORDER_LAT_MS = 200


def main(out: str) -> None:
    con = connect()
    con.execute("SET preserve_insertion_order = false")

    def step(msg: str, sql: str) -> None:
        t = time.time()
        con.execute(sql)
        print(f"  {msg:<40} {time.time() - t:5.1f}s", flush=True)

    step("markets", f"""
        CREATE TEMP TABLE mk AS
        SELECT slug, coin, timeframe, window_start_ms, window_end_ms, fee_rate, outcome
        FROM markets
        WHERE window_start_ms >= {LO_MS} AND window_start_ms < {HI_MS} AND timeframe IN ('5m', '15m', '1h')
          AND outcome IS NOT NULL""")
    step("trades + WS match", f"""
        CREATE TEMP TABLE tr AS
        WITH ws AS (SELECT tx_hash, min(recv_ms) AS ws_recv_ms FROM pm_trade
                    WHERE recv_ms BETWEEN {LO_MS - 3_600_000} AND {HI_MS + 3_600_000} GROUP BY 1)
        SELECT a.slug, a.proxy_wallet AS wallet, a.side, a.outcome AS token, a.price, a.size, a.ts_ms, a.tx_hash,
               ws.ws_recv_ms, coalesce(ws.ws_recv_ms, a.ts_ms) AS t_tr,
               m.coin, m.timeframe, m.window_start_ms, m.window_end_ms, m.fee_rate, m.outcome AS mkt_outcome,
               (m.window_start_ms < {HALF_MS}) AS h1
        FROM pm_api_trades a JOIN mk m USING (slug) LEFT JOIN ws USING (tx_hash)""")
    step("combined book (Up terms)", f"""
        CREATE TEMP TABLE bbo AS
        WITH u AS (
            SELECT slug, recv_ms, seq,
                   CASE WHEN outcome = 'Up' THEN best_bid END AS ub, CASE WHEN outcome = 'Up' THEN best_ask END AS ua,
                   CASE WHEN outcome = 'Down' THEN best_bid END AS db, CASE WHEN outcome = 'Down' THEN best_ask END AS da
            FROM pm_best_bid_ask
            WHERE slug IN (SELECT slug FROM mk) AND recv_ms BETWEEN {LO_MS - 1_200_000} AND {HI_MS + 600_000}),
        f AS (
            SELECT slug, recv_ms, seq,
                   last_value(ub IGNORE NULLS) OVER w AS ub, last_value(ua IGNORE NULLS) OVER w AS ua,
                   last_value(db IGNORE NULLS) OVER w AS db, last_value(da IGNORE NULLS) OVER w AS da
            FROM u WINDOW w AS (PARTITION BY slug ORDER BY recv_ms, seq ROWS UNBOUNDED PRECEDING))
        SELECT slug, recv_ms,
               greatest(coalesce(nullif(ub, 0), 0), coalesce(1 - nullif(da, 0), 0)) AS bid,
               least(coalesce(nullif(ua, 0), 1), coalesce(1 - nullif(db, 0), 1)) AS ask
        FROM f
        QUALIFY row_number() OVER (PARTITION BY slug, recv_ms ORDER BY seq DESC) = 1""")
    # bid 0 / ask 1 mean "no quote on that side" (Polymarket sends 0 / empty for an empty side).
    step("probe times", f"""
        CREATE TEMP TABLE pr AS
        SELECT *,
               least(t_tr + 10_000, window_end_ms) AS t10, least(t_tr + 60_000, window_end_ms) AS t60,
               t_tr + {ORDER_LAT_MS} AS e0,
               t_tr + greatest(0, ts_ms + 1000 - t_tr) + {ORDER_LAT_MS} AS e1
        FROM tr""")
    for col in ("t10", "t60", "e0", "e1"):
        step(f"asof {col}", f"""
            CREATE OR REPLACE TEMP TABLE pr AS
            SELECT p.*, b.bid AS {col}_bid, b.ask AS {col}_ask
            FROM pr p ASOF LEFT JOIN bbo b ON p.slug = b.slug AND p.{col} >= b.recv_ms""")
    for col in ("e0", "e1"):
        step(f"asof {col}+60s", f"""
            CREATE OR REPLACE TEMP TABLE pr AS
            SELECT p.*, b.bid AS {col}m_bid, b.ask AS {col}m_ask
            FROM pr p ASOF LEFT JOIN bbo b ON p.slug = b.slug AND least(p.{col} + 60_000, p.window_end_ms) >= b.recv_ms""")
    step("labels + write", f"""
        COPY (
        SELECT *,
               CASE WHEN side = 'BUY' THEN 1 ELSE -1 END AS sgn,
               (token = mkt_outcome)::INT AS won,
               -- token-terms mid of the traded token at a probe (Up terms -> token terms)
               CASE WHEN token = 'Up' THEN (t10_bid + t10_ask) / 2 ELSE 1 - (t10_bid + t10_ask) / 2 END AS mid10,
               CASE WHEN token = 'Up' THEN (t60_bid + t60_ask) / 2 ELSE 1 - (t60_bid + t60_ask) / 2 END AS mid60,
               -- the token a copier buys: X if the wallet bought X, the other token if it sold X
               CASE WHEN (side = 'BUY') = (token = 'Up') THEN 'Up' ELSE 'Down' END AS copy_token
        FROM pr) TO '{out}' (FORMAT parquet)""")
    con.execute(f"""
        CREATE OR REPLACE VIEW t AS SELECT * FROM read_parquet('{out}')""")
    print(con.sql("SELECT count(*) n, count(ws_recv_ms) ws, count(mid60) m60, count(e0_ask) e0, count(DISTINCT wallet) w FROM t"))


if __name__ == "__main__":
    main(sys.argv[1])

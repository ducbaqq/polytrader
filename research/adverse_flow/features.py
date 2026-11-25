"""Point-in-time features for maker fills (adverse-flow study, docs/research/adverse-flow.md).

usage (from the repo root, POLYDATA_DIR set):
    uv run --project analysis python research/adverse_flow/features.py FILLS_H1.parquet FILLS_H2.parquet OUT.parquet

The fill frames come from a backtest run's fills.parquet, enriched with dir / spr / adv / inv (cents per
share, Up terms, + = good for us) and the market's timeframe / window. Every feature reads only data with
recv_ms <= fill ts_ms - CUT_MS, joined as-of on recv_ms. Signed features are multiplied by `dir` (+1 = the
fill made us long Up), so + means "pressure in favour of our new position".
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

from polydata.bt.marketdata import load_coin
from polydata.views import connect

CUT_MS = 200
DAY = "2026-11-24"


def asof(con, left: pd.DataFrame, right_sql: str, key: str, cols: list[str], t: str = "t") -> pd.DataFrame:
    """As-of join `left` (idx, key, t) to `right_sql` (key, recv_ms, cols): last row with recv_ms <= t."""
    con.register("_l", left[["idx", key, t]].rename(columns={t: "t"}))
    sel = ", ".join(f"r.{c}" for c in cols)
    out = con.execute(f"""SELECT l.idx, {sel} FROM _l l ASOF LEFT JOIN ({right_sql}) r
                          ON l.{key} = r.{key} AND l.t >= r.recv_ms ORDER BY l.idx""").df()
    con.unregister("_l")
    return out.set_index("idx").reindex(left.idx)


def windowed(con, f: pd.DataFrame, cum_sql: str, key: str, cols: list[str], spans: list[int]) -> dict:
    """Differences of cumulative columns between t and t - span, per span."""
    now = asof(con, f, cum_sql, key, cols)
    res = {}
    for s in spans:
        g = f.assign(t2=f.t - s)
        before = asof(con, g, cum_sql, key, cols, t="t2")
        for c in cols:
            res[(c, s)] = (now[c].fillna(0) - before[c].fillna(0)).to_numpy()
    return res


def main(h1: str, h2: str, out: str) -> None:
    f = pd.concat([pd.read_parquet(h1).assign(half="H1"), pd.read_parquet(h2).assign(half="H2")], ignore_index=True)
    f["idx"] = np.arange(len(f))
    f["t"] = f.ts_ms - CUT_MS
    f["mko"] = f.spr + f.adv
    con = connect()
    d = f.dir.to_numpy()

    # ---- A. Polymarket taker flow, same market (Up terms: taker buys Up or sells Down = +)
    tr = f"""SELECT slug, recv_ms,
                sum(CASE WHEN (outcome = 'Up') = (side = 'BUY') THEN size ELSE -size END)
                    OVER (PARTITION BY slug ORDER BY recv_ms, seq ROWS UNBOUNDED PRECEDING) AS cv,
                count(*) OVER (PARTITION BY slug ORDER BY recv_ms, seq ROWS UNBOUNDED PRECEDING) AS cn
             FROM pm_trade WHERE date = '{DAY}' AND slug IN (SELECT DISTINCT slug FROM _fs)"""
    con.register("_fs", f[["slug"]].drop_duplicates())
    w = windowed(con, f, tr, "slug", ["cv", "cn"], [1000, 5000, 30000])
    f["A1_flow1s"] = w[("cv", 1000)] * d
    f["A2_flow5s"] = w[("cv", 5000)] * d
    f["A3_flow30s"] = w[("cv", 30000)] * d
    f["A4_ntrades5s"] = w[("cn", 5000)]

    # ---- B. Book, strategy view (recv clock)
    rows = []
    for coin, g in f.groupby("coin"):
        lo, hi = int(g.window_start_ms.min()), int(g.window_end_ms.max())
        mds = load_coin(con, coin, sorted(g.timeframe.unique()), lo, hi, slugs=set(g.slug))
        for r in g.itertuples():
            md = mds[r.slug]
            top = md.top_at(r.t, "recv")
            top2 = md.top_at(r.t - 2000, "recv")
            bids, asks = md.book_at(r.t, "recv", depth=3)
            bids0, asks0 = md.book_at(r.t - 2000, "recv", depth=3)
            ok = top.bid_px > 0 and top.ask_px < 1000
            mid = (top.bid_px + top.ask_px) / 2 if ok else np.nan
            mid2 = (top2.bid_px + top2.ask_px) / 2 if (top2.bid_px > 0 and top2.ask_px < 1000) else np.nan
            bs, as_ = top.bid_size, top.ask_size
            # our resting side: dir +1 = we bought Up = our order was a bid
            ours, ours0 = (bids, bids0) if r.dir > 0 else (asks, asks0)
            dep, dep0 = sum(s for _, s in ours), sum(s for _, s in ours0)
            rows.append(dict(
                idx=r.idx,
                B1_imbalance=((bs - as_) / (bs + as_) * r.dir) if ok and bs + as_ > 0 else np.nan,
                B2_spread_ticks=(top.ask_px - top.bid_px) / 10 if ok else np.nan,
                B3_our_touch=(bs if r.dir > 0 else as_) if ok else np.nan,
                B4_our_depth_chg2s=(dep - dep0) / dep0 if dep0 > 0 else np.nan,
                B5_pm_mid2s=(mid - mid2) / 10 * r.dir,
                pm_mid=mid / 1000,
            ))
        del mds
    f = f.merge(pd.DataFrame(rows), on="idx", how="left")
    f["B6_queue_at_fill"] = f.queue_ahead_at_fill

    # ---- C/D. Binance futures mids (strategy's ctx.binance), spot, Chainlink
    bbo = f"SELECT sym, recv_ms, (bid + ask) / 2 AS m FROM bn_fut_bbo WHERE date = '{DAY}'"
    spot = f"SELECT sym, recv_ms, (bid + ask) / 2 AS s FROM bn_spot_bbo WHERE date = '{DAY}'"
    fm = {s: asof(con, f.assign(t2=f.t - s), bbo, "sym", ["m"], t="t2")["m"].to_numpy()
          for s in (0, 2000, 10000, 60000)}
    sm = {s: asof(con, f.assign(t2=f.t - s), spot, "sym", ["s"], t="t2")["s"].to_numpy() for s in (0, 2000)}
    f["D1_bn_ret2s"] = np.log(fm[0] / fm[2000]) * 1e4 * d
    f["D2_bn_ret10s"] = np.log(fm[0] / fm[10000]) * 1e4 * d
    f["D3_bn_ret60s"] = np.log(fm[0] / fm[60000]) * 1e4 * d
    basis0, basis2 = np.log(fm[0] / sm[0]) * 1e4, np.log(fm[2000] / sm[2000]) * 1e4
    f["D4_basis_chg2s"] = (basis0 - basis2) * d
    ft = f"""SELECT sym, recv_ms,
                sum(CASE WHEN buyer_is_maker THEN -qty ELSE qty END) OVER w AS cs,
                sum(qty) OVER w AS cq
             FROM bn_fut_trade WHERE date = '{DAY}'
             WINDOW w AS (PARTITION BY sym ORDER BY recv_ms, seq ROWS UNBOUNDED PRECEDING)"""
    w = windowed(con, f, ft, "sym", ["cs", "cq"], [5000])
    f["D5_bn_takerimb5s"] = np.where(w[("cq", 5000)] > 0, w[("cs", 5000)] / np.maximum(w[("cq", 5000)], 1e-12), 0) * d
    lq = f"""SELECT sym, recv_ms, sum(qty * price) OVER (PARTITION BY sym ORDER BY recv_ms, seq
                ROWS UNBOUNDED PRECEDING) AS cl FROM bn_fut_liquidation WHERE date = '{DAY}'"""
    w = windowed(con, f, lq, "sym", ["cl"], [30000])
    f["D6_liq30s"] = w[("cl", 30000)]

    f["cl_coin"] = f.coin
    cl = asof(con, f, f"SELECT coin AS cl_coin, recv_ms, price FROM chainlink WHERE date = '{DAY}'",
              "cl_coin", ["price", "recv_ms"])
    f["C1_bn_vs_cl"] = np.log(fm[0] / cl.price.to_numpy()) * 1e4 * d
    ptb = con.execute(f"SELECT slug, price_to_beat FROM markets").df()
    f = f.merge(ptb, on="slug", how="left")
    f["C2_cl_vs_strike"] = np.log(cl.price.to_numpy() / f.price_to_beat) * 1e4 * d
    f["C3_cl_age"] = (f.t - cl.recv_ms.to_numpy()) / 1000

    # ---- E. Context
    f["E1_tau_s"] = (f.window_end_ms - f.ts_ms) / 1000
    f["E2_abs_moneyness"] = (f.pm_mid - 0.5).abs()
    f["E3_signed_moneyness"] = (f.pm_mid - 0.5) * d
    f["E4_timeframe"] = f.timeframe
    f["E5_coin"] = f.coin

    # ---- F. Cross-timeframe: the same coin's other open markets (Up mid change over 5 s, taker flow 5 s)
    mk = con.execute("SELECT slug, coin, timeframe, window_start_ms, window_end_ms FROM markets").df()
    pairs = f[["idx", "coin", "slug", "t", "dir"]].merge(mk, on="coin", suffixes=("", "_o"))
    pairs = pairs[(pairs.slug_o != pairs.slug) & (pairs.window_start_ms <= pairs.t) & (pairs.window_end_ms > pairs.t)]
    pairs = pairs[pairs.slug_o.str.split("-").str[2] != pairs.slug.str.split("-").str[2]]  # other timeframe
    pairs = pairs.reset_index(drop=True)
    pairs["pid"] = np.arange(len(pairs))
    p = pairs.rename(columns={"idx": "fidx", "pid": "idx", "slug": "slug_f", "slug_o": "slug"})
    con.register("_fs", p[["slug"]].drop_duplicates())
    bba = f"""SELECT slug, recv_ms, (best_bid + best_ask) / 2 AS m FROM pm_best_bid_ask
              WHERE date = '{DAY}' AND outcome = 'Up' AND best_bid > 0 AND best_ask < 1 AND best_ask - best_bid <= 0.05
                AND slug IN (SELECT slug FROM _fs)"""
    m0 = asof(con, p, bba, "slug", ["m"])["m"].to_numpy()
    m5 = asof(con, p.assign(t2=p.t - 5000), bba, "slug", ["m"], t="t2")["m"].to_numpy()
    w = windowed(con, p, tr, "slug", ["cv"], [5000])
    p["dm"] = (m0 - m5) * 100 * p.dir
    p["fl"] = w[("cv", 5000)] * p.dir
    agg = p.groupby("fidx").agg(F1_sib_mid5s=("dm", "mean"), F2_sib_flow5s=("fl", "sum"))
    f = f.merge(agg, left_on="idx", right_index=True, how="left")
    f.to_parquet(out)
    print(f.filter(regex="^[A-F][0-9]_").describe().T.round(3).to_string())


if __name__ == "__main__":
    main(*sys.argv[1:4])

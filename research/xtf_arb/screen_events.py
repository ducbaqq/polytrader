"""Cross-timeframe arb screen at event resolution (docs/strategies/xtf-arb.md).

usage: POLYDATA_DIR=... uv run --project analysis python research/xtf_arb/screen_events.py [OUT_DIR]

End-aligned market pairs (same coin, same window_end, different timeframes among 5m/15m/1h/4h, both
windows starting at or after REPLAY_START). A = the lower strike. At every Up-token `pm_best_bid_ask`
update of either market (recv_ms, inside the overlap) the pair cost is

    cost = ask(Up_A) + ask(Down_B) + fee(Up_A) + fee(Down_B),   ask(Down_B) = 1 - bid(Up_B)

(fee per share = rate * p * (1 - p)). A signal is cost < 1 with both books' spread <= 10 c. Execution:
both tops ASOF at recv_ms + latency (200 ms, robustness 400 ms). Payoff per pair: 1 + [A Up and B Down].
Halves by the overlap start (the shorter market's window_start) vs 17:15 UTC. Only 2026-11-24 (train).
Writes events.parquet (every signal row) and pairs.parquet to OUT_DIR and prints the tables.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from polydata.bt.splits import REPLAY_START_MS
from polydata.views import connect

HALF_MS = 1795540500000  # 2026-11-24 17:15 UTC
DAY_END_MS = 1795564800000  # 2026-11-25 00:00 UTC: nothing at or after (sealed day)
MAX_SPREAD = 0.10
LATENCIES = (200, 400)
BLOCK_MS = 15 * 60_000

PAIRS_SQL = f"""
CREATE OR REPLACE TEMP TABLE mk AS
SELECT m.slug, m.coin, m.timeframe, m.window_start_ms, m.window_end_ms, m.fee_rate, m.outcome,
       coalesce(m.price_to_beat, (SELECT avg(price) FROM chainlink c WHERE c.coin = m.coin
                AND c.ts_ms >= m.window_start_ms - 60000 AND c.ts_ms < m.window_start_ms)) AS K
FROM markets m
WHERE m.timeframe IN ('5m', '15m', '1h', '4h') AND m.window_start_ms >= {REPLAY_START_MS}
  AND m.window_end_ms <= {DAY_END_MS} AND m.outcome IS NOT NULL;
CREATE OR REPLACE TEMP TABLE pairs AS
SELECT row_number() OVER (ORDER BY a.window_end_ms, a.coin, a.timeframe, b.timeframe) AS pid,
       a.coin, a.window_end_ms AS end_ms, greatest(a.window_start_ms, b.window_start_ms) AS ov_start,
       a.slug AS slug_a, a.timeframe AS tf_a, a.K AS k_a, a.fee_rate AS rate_a, a.outcome AS out_a,
       b.slug AS slug_b, b.timeframe AS tf_b, b.K AS k_b, b.fee_rate AS rate_b, b.outcome AS out_b
FROM mk a JOIN mk b ON a.coin = b.coin AND a.window_end_ms = b.window_end_ms AND a.timeframe <> b.timeframe
WHERE a.K < b.K;
"""

EVENTS_SQL = """
CREATE OR REPLACE TEMP TABLE q AS
SELECT slug, recv_ms, seq, best_bid AS bid, best_ask AS ask
FROM pm_best_bid_ask
WHERE outcome = 'Up' AND recv_ms >= {lo} AND recv_ms < {hi}
  AND slug IN (SELECT slug_a FROM pairs UNION SELECT slug_b FROM pairs);
CREATE OR REPLACE TEMP TABLE qa AS SELECT p.pid, q.* FROM pairs p JOIN q ON q.slug = p.slug_a;
CREATE OR REPLACE TEMP TABLE qb AS SELECT p.pid, q.* FROM pairs p JOIN q ON q.slug = p.slug_b;
-- event clock: every update of either market inside the overlap
CREATE OR REPLACE TEMP TABLE ev AS
SELECT DISTINCT p.pid, x.recv_ms AS t FROM pairs p
JOIN (SELECT pid, recv_ms FROM qa UNION ALL SELECT pid, recv_ms FROM qb) x ON x.pid = p.pid
WHERE x.recv_ms >= p.ov_start AND x.recv_ms < p.end_ms;
"""


def state_sql(shift: int) -> str:
    """Both tops ASOF at t + shift (last row received at or before; ties broken by seq via arg_max is not
    possible in ASOF, so rows with equal recv_ms are collapsed to the highest seq first)."""
    return f"""
    WITH a1 AS (SELECT pid, recv_ms, arg_max(bid, seq) bid, arg_max(ask, seq) ask FROM qa GROUP BY ALL),
         b1 AS (SELECT pid, recv_ms, arg_max(bid, seq) bid, arg_max(ask, seq) ask FROM qb GROUP BY ALL),
         e AS (SELECT pid, t, t + {shift} AS tt FROM ev)
    SELECT e.pid, e.t, a.bid AS bid_a, a.ask AS ask_a, b.bid AS bid_b, b.ask AS ask_b
    FROM e ASOF LEFT JOIN a1 a ON e.pid = a.pid AND e.tt >= a.recv_ms
           ASOF LEFT JOIN b1 b ON e.pid = b.pid AND e.tt >= b.recv_ms
    """


def cost(df: pd.DataFrame, pairs: pd.DataFrame, sfx: str = "") -> pd.DataFrame:
    d = df.merge(pairs[["pid", "rate_a", "rate_b"]], on="pid", how="left")
    ua = d[f"ask_a{sfx}"].to_numpy(float)
    db = 1.0 - d[f"bid_b{sfx}"].to_numpy(float)
    fee = d.rate_a * ua * (1 - ua) + d.rate_b * db * (1 - db)
    df[f"gross{sfx}"] = ua + db
    df[f"cost{sfx}"] = (ua + db + fee).to_numpy()
    df[f"wide{sfx}"] = ((d[f"ask_a{sfx}"] - d[f"bid_a{sfx}"] > MAX_SPREAD + 1e-9)
                        | (d[f"ask_b{sfx}"] - d[f"bid_b{sfx}"] > MAX_SPREAD + 1e-9)).to_numpy()
    return df


def boot(blocks: pd.DataFrame, n: int = 2000, seed: int = 0):
    """95% CI of sum(pnl)/sum(n) over 15-minute blocks (all blocks with pairs, traded or not)."""
    if blocks.n.sum() == 0:
        return (np.nan, np.nan)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(blocks), (n, len(blocks)))
    p, c = blocks.pnl.to_numpy()[idx].sum(1), blocks.n.to_numpy()[idx].sum(1)
    r = np.where(c > 0, p / np.where(c > 0, c, 1), np.nan) * 100
    return tuple(np.nanpercentile(r, [2.5, 97.5]))


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    con = connect()
    con.execute(PAIRS_SQL)
    pairs = con.execute("SELECT * FROM pairs").df()
    lo, hi = int(pairs.ov_start.min()), int(pairs.end_ms.max()) + 1000
    con.execute(EVENTS_SQL.format(lo=lo, hi=hi))
    n_ev = con.execute("SELECT count(*) FROM ev").fetchone()[0]
    s0 = cost(con.execute(state_sql(0)).df(), pairs)
    s0 = s0.dropna(subset=["cost"])
    s0["half"] = np.where(s0.pid.map(pairs.set_index("pid").ov_start) < HALF_MS, "H1", "H2")
    print(f"pairs {len(pairs)}  events {n_ev}  rows with both books {len(s0)}")
    print("min gross (ask Up_A + ask Down_B) per half:", s0.groupby("half").gross.min().to_dict())
    print("min cost per half:", s0.groupby("half").cost.min().to_dict())
    print("rows gross<1:", int((s0.gross < 1).sum()), " cost<1:", int((s0.cost < 1).sum()),
          " cost<1 & narrow:", int(((s0.cost < 1) & ~s0.wide).sum()))
    q = s0[~s0.wide].groupby("half").cost.quantile([0, 0.001, 0.01, 0.1, 0.5]).unstack()
    print("cost quantiles (narrow books):\n", q.round(4).to_string())
    s0 = s0.sort_values(["pid", "t"]).reset_index(drop=True)
    sig = (s0.cost < 1) & ~s0.wide
    # episodes: consecutive event rows of a pair with the signal; duration until the first row without it
    nxt_t = s0.groupby("pid").t.shift(-1)
    start = sig & ~(sig.groupby(s0.pid).shift(1, fill_value=False))
    ep_id = start.cumsum().where(sig)
    s0["ep"] = ep_id
    eps = s0[sig].groupby("ep").agg(pid=("pid", "first"), t=("t", "first"), t_last=("t", "last"),
                                    min_cost=("cost", "min"), half=("half", "first"))
    end_t = nxt_t[sig].groupby(ep_id[sig]).last()
    eps["dur_ms"] = (end_t - eps.t).to_numpy()
    print(f"episodes: {len(eps)}")
    if len(eps):
        print(eps.groupby("half").agg(n=("pid", "size"), pairs=("pid", "nunique"),
                                      med_dur=("dur_ms", "median"), max_dur=("dur_ms", "max"),
                                      best=("min_cost", "min")).to_string())
    # execution at +latency for every episode start
    res = {}
    for lat in LATENCIES:
        sl = cost(con.execute(state_sql(lat)).df().rename(columns=lambda c: c if c in ("pid", "t") else c + f"_{lat}"),
                  pairs, f"_{lat}")
        e = eps.reset_index().merge(sl, on=["pid", "t"], how="left")
        e["ok"] = (e[f"cost_{lat}"] < 1) & ~e[f"wide_{lat}"]
        res[lat] = e
        print(f"latency {lat} ms: episodes still < 1: {int(e.ok.sum())} of {len(e)}")
    eps.to_parquet(out / "episodes.parquet")
    pairs.to_parquet(out / "pairs.parquet")
    for lat, e in res.items():
        e.to_parquet(out / f"episodes_{lat}.parquet")
    # pair-level P&L: first executable episode per pair, 1 share (size measured separately)
    pairs["half"] = np.where(pairs.ov_start < HALF_MS, "H1", "H2")
    pairs["block"] = pairs.ov_start // BLOCK_MS
    pairs["payoff"] = 1.0 + ((pairs.out_a == "Up") & (pairs.out_b == "Down")).astype(float)
    for lat, e in res.items():
        ok = e[e.ok].sort_values("t").drop_duplicates("pid")[["pid", f"cost_{lat}"]]
        p = pairs.merge(ok, on="pid", how="left")
        p["n"] = p[f"cost_{lat}"].notna().astype(int)
        p["pnl"] = np.where(p.n > 0, p.payoff - p[f"cost_{lat}"], 0.0)
        p["locked"] = np.where(p.n > 0, 1 - p[f"cost_{lat}"], 0.0)
        for h, g in p.groupby("half"):
            b = g.groupby("block").agg(pnl=("pnl", "sum"), n=("n", "sum"))
            ci = boot(b)
            n = int(g.n.sum())
            c = g.pnl.sum() / n * 100 if n else float("nan")
            lk = g.locked.sum() / n * 100 if n else float("nan")
            print(f"lat {lat} {h}: pairs traded {n}/{len(g)}  c/pair {c:+.2f} [{ci[0]:+.2f},{ci[1]:+.2f}]  locked c/pair {lk:+.2f}")
    con.close()


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("xtf_out"))

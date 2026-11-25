"""Pair-level results and leg risk of an `xtf-arb` simulator run, from its log.txt (`pair ...` lines).

usage: POLYDATA_DIR=... uv run --project analysis python research/xtf_arb/sim_pairs.py RUN_DIR

Each finished pair entry: filled Up_A (n_a, notional, fee) and Down_B (n_b, notional, fee). The matched part
(min(n_a, n_b) shares of each leg) is the arb; the excess of one leg is an unhedged position held to
resolution (leg risk). P&L uses the recorded outcomes (scoring only). Halves by the shorter market's
window_start vs 17:15 UTC; CI: 15-minute-block bootstrap over entries' blocks, per pair-share.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from polydata.views import connect

HALF_MS = 1795540500000


def main(run: Path) -> None:
    rows = []
    for line in (run / "log.txt").read_text().splitlines():
        f = line.split()
        if len(f) == 12 and f[1] == "pair":
            rows.append(dict(t=int(f[0]), a=f[2], b=f[3], n_a=float(f[4]), not_a=float(f[5]), fee_a=float(f[6]),
                             n_b=float(f[7]), not_b=float(f[8]), fee_b=float(f[9]), n=float(f[10]), cost=float(f[11])))
    d = pd.DataFrame(rows)
    print(f"pair entries sent: {len(d)}")
    if d.empty:
        return
    con = connect()
    mk = con.execute("SELECT slug, window_start_ms, outcome FROM markets").df().set_index("slug")
    con.close()
    up_a = (d.a.map(mk.outcome) == "Up").astype(float)
    dn_b = (d.b.map(mk.outcome) == "Down").astype(float)
    d["ov_start"] = np.maximum(d.a.map(mk.window_start_ms), d.b.map(mk.window_start_ms))
    d["half"] = np.where(d.ov_start < HALF_MS, "H1", "H2")
    d["block"] = d.ov_start // 900_000
    d["m"] = np.minimum(d.n_a, d.n_b)
    pa = np.where(d.n_a > 0, d.not_a / d.n_a.where(d.n_a > 0, 1), 0)
    pb = np.where(d.n_b > 0, d.not_b / d.n_b.where(d.n_b > 0, 1), 0)
    fa = np.where(d.n_a > 0, d.fee_a / d.n_a.where(d.n_a > 0, 1), 0)
    fb = np.where(d.n_b > 0, d.fee_b / d.n_b.where(d.n_b > 0, 1), 0)
    d["pnl_matched"] = d.m * (up_a + dn_b - pa - pb - fa - fb)
    d["locked_matched"] = d.m * (1 - pa - pb - fa - fb)
    d["pnl_excess"] = (d.n_a - d.m) * (up_a - pa - fa) + (d.n_b - d.m) * (dn_b - pb - fb)
    d["excess_cost"] = (d.n_a - d.m) * (pa + fa) + (d.n_b - d.m) * (pb + fb)
    d["pnl"] = d.pnl_matched + d.pnl_excess
    d["status"] = np.select([(d.n_a == 0) & (d.n_b == 0), d.n_a == d.n_b, d.n_a == 0, d.n_b == 0],
                            ["none filled", "both equal", "only B", "only A"], "partial mismatch")
    pd.set_option("display.width", 250)
    print(d.groupby(["half", "status"]).agg(n=("t", "size"), pairs_matched=("m", "sum"),
                                            pnl=("pnl", "sum"), pnl_excess=("pnl_excess", "sum")).round(3).to_string())
    for h, g in d.groupby("half"):
        b = g.groupby("block").agg(pnl=("pnl", "sum"), n=("m", "sum"))
        tot_n = max(b.n.sum(), 1e-9)
        rng = np.random.default_rng(0)
        idx = rng.integers(0, len(b), (2000, len(b)))
        p, c = b.pnl.to_numpy()[idx].sum(1), b.n.to_numpy()[idx].sum(1)
        ci = np.nanpercentile(np.where(c > 0, p / np.where(c > 0, c, 1), np.nan) * 100, [2.5, 97.5])
        print(f"{h}: entries {len(g)} (market pairs {g[['a','b']].drop_duplicates().shape[0]}), matched pairs {g.m.sum():.1f}, "
              f"P&L ${g.pnl.sum():+.2f} = matched ${g.pnl_matched.sum():+.2f} (locked ${g.locked_matched.sum():+.2f}) "
              f"+ unhedged ${g.pnl_excess.sum():+.2f}; c/pair {g.pnl.sum() / tot_n * 100:+.2f} [{ci[0]:+.2f},{ci[1]:+.2f}]; "
              f"legs mismatched {int((g.status != 'both equal').sum())}/{len(g)}, unhedged cost ${g.excess_cost.sum():.2f}")
    print(d[["t", "a", "b", "n", "cost", "n_a", "n_b", "pnl_matched", "pnl_excess", "half"]].round(4).to_string(index=False))


if __name__ == "__main__":
    main(Path(sys.argv[1]))

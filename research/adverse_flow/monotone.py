"""Post-hoc (not pre-registered) checks for the adverse-flow study:

1. Spearman rank correlation of each numeric feature with per-fill adverse, per half, with a market-clustered
   bootstrap 95% CI. A feature is "monotone-stable" if both CIs exclude 0 with the same sign.
2. One combined linear score: OLS of winsorised adverse on the clipped, standardised numeric features, fit on
   H1 only, scored on H2 (quintiles of the score with H1 edges).

usage: uv run --project analysis python research/adverse_flow/monotone.py FEATURES.parquet
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd
from scipy.stats import spearmanr

NBOOT = 500


def clustered_spearman(g: pd.DataFrame, feat: str, seed: int = 0) -> tuple[float, float, float]:
    g = g[[feat, "adv", "slug"]].dropna()
    slugs = g.slug.unique()
    by = {s: ix for s, ix in g.groupby("slug").indices.items()}
    x, y = g[feat].to_numpy(), g.adv.to_numpy()
    r0 = spearmanr(x, y).statistic
    rng = np.random.default_rng(seed)
    bs = []
    for _ in range(NBOOT):
        ix = np.concatenate([by[s] for s in rng.choice(slugs, len(slugs))])
        bs.append(spearmanr(x[ix], y[ix]).statistic)
    return r0, float(np.nanquantile(bs, 0.025)), float(np.nanquantile(bs, 0.975))


def main(path: str) -> None:
    f = pd.read_parquet(path)
    feats = [c for c in f.columns if len(c) > 2 and c[0] in "ABCDF" and c[1].isdigit() and c[2] == "_"]
    feats += ["E1_tau_s", "E2_abs_moneyness", "E3_signed_moneyness"]
    rows = []
    for feat in feats:
        r = {"feature": feat}
        for h in ("H1", "H2"):
            rho, lo, hi = clustered_spearman(f[f.half == h], feat)
            r[f"{h} rho"] = f"{rho:+.3f} [{lo:+.3f}, {hi:+.3f}]"
            r[f"{h} sig"] = np.sign(rho) if lo > 0 or hi < 0 else 0
        r["stable"] = r["H1 sig"] != 0 and r["H1 sig"] == r["H2 sig"]
        rows.append(r)
    print(pd.DataFrame(rows).to_string(index=False))

    # combined score, fit on H1 only (B6 excluded: simulator state at the fill, not known before it)
    use = [c for c in feats if c != "B6_queue_at_fill"]
    h1 = f[f.half == "H1"]
    lo, hi = h1[use].quantile(0.01), h1[use].quantile(0.99)
    mu, sd = h1[use].clip(lo, hi, axis=1).mean(), h1[use].clip(lo, hi, axis=1).std()

    def X(d):
        z = ((d[use].clip(lo, hi, axis=1) - mu) / sd).fillna(0)
        return np.column_stack([np.ones(len(d)), z.to_numpy()])

    y = h1.adv.clip(-20, 20).to_numpy()
    beta = np.linalg.lstsq(X(h1), y, rcond=None)[0]
    f["score"] = X(f) @ beta
    edges = np.quantile(f.loc[f.half == "H1", "score"], [0.2, 0.4, 0.6, 0.8])
    f["sq"] = np.searchsorted(edges, f.score) + 1
    t = f.groupby(["half", "sq"]).apply(lambda g: pd.Series({
        "n": len(g), "adv": np.average(g.adv, weights=g["size"]), "mko": np.average(g.spr + g.adv, weights=g["size"])}),
        include_groups=False).unstack("half").round(2)
    print("\ncombined linear score (fit on H1), quintiles with H1 edges (q1 = predicted worst)")
    print(t.to_string())
    for h in ("H1", "H2"):
        g = f[f.half == h]
        print(h, "spearman(score, adv) =", round(spearmanr(g.score, g.adv).statistic, 3))
    print("\nlargest |beta| (per 1 sd):")
    print(pd.Series(beta[1:], index=use).sort_values(key=np.abs, ascending=False).head(8).round(3).to_string())


if __name__ == "__main__":
    main(sys.argv[1])

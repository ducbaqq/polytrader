"""Quintile tables and the H1 -> H2 stability test for the adverse-flow features.

usage: uv run --project analysis python research/adverse_flow/analyze.py FEATURES.parquet [OUT.md]

Edges are H1 quintiles, applied unchanged to H2. The H1-worst bucket (lowest size-weighted adverse) is the
candidate; it is "stable" if it is >= 1.0 c/share worse than the rest on H1 and >= 0.5 c/share worse on H2
with a market-clustered bootstrap 95% CI excluding 0 (see docs/research/adverse-flow.md).
"""

from __future__ import annotations

import sys

import numpy as np
import pandas as pd

CATEGORICAL = {"E4_timeframe", "E5_coin"}
H1_GAP, H2_GAP, NBOOT = -1.0, -0.5, 1000


def md(t: pd.DataFrame, index: bool = True) -> str:
    """Markdown table without the optional tabulate dependency."""
    t = t.reset_index() if index else t
    rows = [[str(c) for c in t.columns]] + [[f"{v:.2f}" if isinstance(v, float) else str(v) for v in r]
                                             for r in t.itertuples(index=False)]
    return "\n".join(["| " + " | ".join(rows[0]) + " |", "|" + "---|" * len(rows[0])]
                     + ["| " + " | ".join(r) + " |" for r in rows[1:]])


def wmean(x, w):
    return float(np.sum(x * w) / np.sum(w)) if np.sum(w) > 0 else np.nan


def buckets(f: pd.DataFrame, feat: str) -> pd.Series:
    if feat in CATEGORICAL:
        return f[feat].astype(str)
    h1 = f.loc[f.half == "H1", feat].dropna()
    edges = np.unique(np.quantile(h1, [0.2, 0.4, 0.6, 0.8]))
    b = pd.Series(np.searchsorted(edges, f[feat].to_numpy(), side="right"), index=f.index)
    labels = [f"<={edges[0]:.3g}"] + [f"({a:.3g},{c:.3g}]" for a, c in zip(edges[:-1], edges[1:])] + [f">{edges[-1]:.3g}"]
    out = b.map(lambda i: f"q{i + 1} {labels[i]}")
    return out.where(f[feat].notna())


def table(g: pd.DataFrame) -> pd.Series:
    w = g["size"].to_numpy()
    return pd.Series({"n": len(g), "spr": wmean(g.spr, w), "adv": wmean(g.adv, w), "mko": wmean(g.mko, w)})


def diff_boot(g: pd.DataFrame, flag: pd.Series, seed: int = 0) -> tuple[float, float, float]:
    """adverse(flagged) - adverse(rest), with a market-clustered bootstrap 95% CI."""
    x = g.assign(fl=flag.to_numpy(), wa=g.adv * g["size"])
    per = x.groupby(["slug", "fl"]).agg(wa=("wa", "sum"), w=("size", "sum")).unstack(fill_value=0)
    wa, w = per["wa"].to_numpy(), per["w"].to_numpy()
    if wa.shape[1] < 2:
        return np.nan, np.nan, np.nan

    def d(ix):
        s, n = wa[ix].sum(0), w[ix].sum(0)
        return s[1] / n[1] - s[0] / n[0] if n[0] > 0 and n[1] > 0 else np.nan

    rng = np.random.default_rng(seed)
    bs = np.array([d(rng.integers(0, len(wa), len(wa))) for _ in range(NBOOT)])
    return d(np.arange(len(wa))), float(np.nanquantile(bs, 0.025)), float(np.nanquantile(bs, 0.975))


def main(path: str, out: str | None) -> None:
    f = pd.read_parquet(path)
    feats = [c for c in f.columns if len(c) > 2 and c[0] in "ABCDEF" and c[1].isdigit() and c[2] == "_"]
    lines, summary = [], []
    for feat in feats:
        b = buckets(f, feat)
        x = f.assign(b=b).dropna(subset=["b"])
        t = x.groupby(["half", "b"]).apply(table, include_groups=False).unstack("half")
        t.columns = [f"{h} {c}" for c, h in t.columns]
        t = t[[f"{h} {c}" for h in ("H1", "H2") for c in ("n", "spr", "adv", "mko")]]
        h1 = x[x.half == "H1"]
        h1t = h1.groupby("b").apply(table, include_groups=False)
        h1t = h1t[h1t.n >= 30]
        worst = h1t.adv.idxmin()
        g1 = wmean(h1.adv[h1.b == worst], h1["size"][h1.b == worst]) - wmean(h1.adv[h1.b != worst], h1["size"][h1.b != worst])
        h2 = x[x.half == "H2"]
        g2, lo, hi = diff_boot(h2, h2.b == worst)
        stable = g1 <= H1_GAP and g2 <= H2_GAP and hi < 0
        h2keep = h2[h2.b != worst]
        summary.append(dict(feature=feat, worst_bucket_h1=worst, h1_gap=round(g1, 2), h2_gap=round(g2, 2),
                            h2_ci=f"[{lo:+.2f}, {hi:+.2f}]", h2_flagged_share=round((h2.b == worst).mean(), 3),
                            h2_mko_all=round(wmean(h2.mko, h2["size"]), 2),
                            h2_mko_kept=round(wmean(h2keep.mko, h2keep["size"]), 2), stable=stable))
        lines.append(f"### {feat}\n\n" + md(t) + "\n")
    s = pd.DataFrame(summary)
    txt = "## Summary (gap = adverse of the H1-worst bucket minus the rest, c/share)\n\n" + md(s, index=False) \
        + "\n\n## Quintile tables (edges from H1)\n\n" + "\n".join(lines)
    print(s.to_string())
    if out:
        open(out, "w").write(txt)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)

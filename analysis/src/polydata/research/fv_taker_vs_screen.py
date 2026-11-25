"""Per-market comparison of an `fv-taker` simulator run with the `fair-value-deviation` screen (300 ms, 10 shares).

    uv run python -m polydata.research.fv_taker_vs_screen <run_dir>

Prints how many markets both trade, how often the token agrees, the entry time and price gaps, and the P&L per
share of the matched, screen-only and simulator-only trades, per half.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from .. import screen, screen_ideas
from .screen_halves import HALF_MS


def main(run: Path) -> None:
    snap, _ = screen.load_snapshots(rebuild=False)
    voided = set(screen.voided_slugs(snap))
    snap = snap[~snap.slug.isin(voided)].reset_index(drop=True)
    sig = screen_ideas.fair_value_deviation(snap.drop(columns=[c for c in screen.FORBIDDEN_RULE_COLUMNS if c in snap.columns]))
    f = screen.fills(snap, sig, 300, "hold", 10.0)
    f = f.rename(columns={"token": "c_tok", "price": "c_px", "pnl": "c_pnl", "t_ms": "c_t"})[["slug", "c_tok", "c_px", "c_pnl", "c_t"]]
    fl = pd.read_parquet(run / "fills.parquet")
    mk = pd.read_parquet(run / "markets.parquet")
    mk = mk[~mk.voided.astype(bool)]
    rows = []
    for slug, d in fl.groupby("slug"):
        rows.append({"slug": slug, "s_tok": d.token.iloc[0], "s_t": d.ts_ms.min(), "s_px": np.average(d.px, weights=d["size"]),
                     "s_sz": d["size"].sum()})
    g = pd.DataFrame(rows).merge(mk[["slug", "pnl", "window_start_ms", "outcome"]], on="slug", how="right")
    m = f.merge(g, on="slug", how="outer", indicator=True)
    ws = snap.drop_duplicates("slug").set_index("slug").window_start_ms
    m["ws"] = m.slug.map(ws)
    m = m[m.c_tok.notna() | m.s_tok.notna()]
    m["half"] = np.where(m.ws < HALF_MS, "h1", "h2")
    print("screen trades", int(m.c_tok.notna().sum()), "sim trades", int(m.s_tok.notna().sum()))
    for h, d in m.groupby("half"):
        both = d[d.c_tok.notna() & d.s_tok.notna()]
        same = both[both.c_tok == both.s_tok]
        print(f"\n[{h}] screen {int(d.c_tok.notna().sum())}, sim {int(d.s_tok.notna().sum())}, both {len(both)}, same token {len(same)}")
        diff = both[both.c_tok != both.s_tok]
        groups = (("both", both), ("same token", same), ("diff token", diff), ("screen only", d[d.s_tok.isna()]), ("sim only", d[d.c_tok.isna()]))
        for name, x in groups:
            if not len(x):
                continue
            cs = x.c_pnl.sum() / (x.c_pnl.notna().sum() * 10) * 100 if x.c_pnl.notna().any() else float("nan")
            ss = x.pnl[x.s_tok.notna()].sum() / x.s_sz.sum() * 100 if x.s_sz.notna().any() else float("nan")
            print(f"  {name:12s} n={len(x):3d}  screen {cs:+6.2f}c/sh   sim {ss:+6.2f}c/sh")
        if len(diff):
            dd = (diff.s_t - diff.c_t) / 1000
            print(f"  diff-token entry time sim-screen: median {dd.median():+.1f}s, sim earlier in {(dd < 0).mean():.2f} of them")
        if len(same):
            dt = (same.s_t - same.c_t) / 1000
            print(f"  same-token entry time sim-screen: median {dt.median():+.1f}s, quartiles [{dt.quantile(.25):+.1f}, {dt.quantile(.75):+.1f}]")
            print(f"  fill px - screen px: mean {np.mean(same.s_px - same.c_px) * 100:+.2f}c, median {np.median(same.s_px - same.c_px) * 100:+.2f}c")
            print(f"  sim size mean {same.s_sz.mean():.2f} of 10; share with size < 10: {(same.s_sz < 10 - 1e-9).mean():.2f}")
            win = np.where(same.s_tok == "Up", same.outcome == "Up", same.outcome == "Down")
            print(f"  win rate {win.mean():.3f}; mean px screen {same.c_px.mean():.3f}, sim {same.s_px.mean():.3f}")


if __name__ == "__main__":
    main(Path(sys.argv[1]))

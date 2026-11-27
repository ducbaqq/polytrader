"""Cents per share of a btc-shock-alts run folder, with a 15-minute block bootstrap CI and a per-coin split.

usage: uv run --project analysis python research/btc_shock/run_ci.py RUN_DIR [RUN_DIR ...]
Per fill: settlement P&L = (settle_value - px) * size - fee; 10 s markout = (mid_10s - px) * size - fee.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

BLOCK_MS = 15 * 60_000


def ci(f: pd.DataFrame, col: str, n: int = 2000) -> str:
    x = f.dropna(subset=[col])
    blk = x.groupby(x.ts_ms // BLOCK_MS).agg(p=(col, "sum"), s=("size", "sum"))
    if len(blk) < 2:
        return "n/a"
    idx = np.random.default_rng(0).integers(0, len(blk), (n, len(blk)))
    est = 100 * blk.p.values[idx].sum(1) / blk.s.values[idx].sum(1)
    return f"{100 * blk.p.sum() / blk.s.sum():+.2f} [{np.quantile(est, .025):+.1f}, {np.quantile(est, .975):+.1f}]"


for d in sys.argv[1:]:
    f = pd.read_parquet(Path(d) / "fills.parquet")
    f["settle"] = (f.settle_value - f.px) * f["size"] - f.fee
    f["mk10"] = (f.mid_10s - f.px) * f["size"] - f.fee
    coins = ", ".join(f"{c} {len(g)}/{100 * g.settle.sum() / g['size'].sum():+.1f}" for c, g in f.groupby("coin"))
    print(f"{Path(d).name}: fills {len(f)}, shares {f['size'].sum():.0f}, settle c/sh {ci(f, 'settle')}, "
          f"10 s c/sh {ci(f, 'mk10')}; by coin fills/settle c: {coins}")

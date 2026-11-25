"""Halves report for a finished simulator run (train day): cents per share with a 15-minute block bootstrap,
first half (window_start < 2026-11-24 17:15 UTC) and second half separately, as the research rules ask.

    uv run python -m polydata.research.bt_halves <run_dir> [by]

cents per share = market P&L (fees included) / shares bought. Blocks are all non-voided markets' 15-minute
blocks of window start (traded or not), 2000 resamples, seed 0, the same as `screen._stats`.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from .screen_halves import HALF_MS

BLOCK_MS, N_BOOT = 900_000, 2000


def stats(mk: pd.DataFrame, size: pd.Series) -> dict:
    mk = mk[~mk.voided.astype(bool)].copy()
    mk["pnl"] = mk.pnl.astype(float).fillna(0.0)
    mk["size"] = mk.slug.map(size).fillna(0.0)
    mk["n"] = (mk["size"] > 0).astype(int)
    mk["block"] = mk.window_start_ms.astype("int64") // BLOCK_MS
    b = mk.groupby("block").agg(pnl=("pnl", "sum"), size=("size", "sum"), n=("n", "sum"))
    pnl, sh, n = float(b.pnl.sum()), float(b["size"].sum()), int(b.n.sum())
    out = {"n": n, "shares": sh, "pnl": pnl, "cents": pnl / sh * 100 if sh else None, "ci": None}
    if n:
        idx = np.random.default_rng(0).integers(0, len(b), (N_BOOT, len(b)))
        p, s = b.pnl.to_numpy()[idx].sum(1), b["size"].to_numpy()[idx].sum(1)
        c = np.where(s > 0, p / np.where(s > 0, s, 1) * 100, np.nan)
        out["ci"] = [float(x) for x in np.nanpercentile(c, [2.5, 97.5])]
    return out


def fmt(s: dict) -> str:
    if not s["n"]:
        return "n=0"
    return f"n={s['n']} sh={s['shares']:.0f} {s['cents']:+.2f}c [{s['ci'][0]:+.1f},{s['ci'][1]:+.1f}] ${s['pnl']:+.2f}"


def report(run: Path, by: bool = False) -> None:
    mk = pd.read_parquet(run / "markets.parquet")
    fl = pd.read_parquet(run / "fills.parquet")
    size = fl.groupby("slug")["size"].sum()
    print(run.name, "markets", len(mk), "voided", int(mk.voided.astype(bool).sum()), "fills", len(fl))
    if len(fl):
        print(f"  mean fill px {np.average(fl.px, weights=fl['size']):.4f}, mean size/market {size.mean():.2f}")
    for name, sel in (("h1", mk.window_start_ms < HALF_MS), ("h2", mk.window_start_ms >= HALF_MS), ("all", mk.window_start_ms > 0)):
        print(f"  {name}: {fmt(stats(mk[sel], size))}")
        if by:
            for key in ("timeframe", "coin"):
                for v, g in mk[sel].groupby(key):
                    s = stats(g, size)
                    if s["n"]:
                        print(f"      {key}={v}: {fmt(s)}")


if __name__ == "__main__":
    report(Path(sys.argv[1]), len(sys.argv) > 2 and sys.argv[2] == "by")

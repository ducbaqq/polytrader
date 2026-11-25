"""Positive-EV cross-timeframe pair screen on the 5 s snapshots (docs/strategies/xtf-arb.md, variant EV).

usage: POLYDATA_DIR=... uv run --project analysis python research/xtf_arb/screen_ev.py [OUT_DIR]

Pairs of end-aligned 5m/15m/1h markets of one coin (A = lower strike). At each common snapshot time t:
cost = up_ask_A + dn_ask_B + fees; value = 1 + max(0, fair_A - fair_B) (calibration model, k = 1.2).
Signal: value - cost > margin, both books' spread <= 10 c, first signal per pair. Fill: up_ask_300 of A and
dn_ask_300 of B (+ fees); payoff 1 + [A Up and B Down]; 1 pair per signal. Margin chosen on H1 from
{0, 0.02, 0.05}; H2 reported unchanged; 1000 ms robustness. Voided windows (data gaps) are skipped.
Also prints the riskless subset (cost < 1 at t) for comparison with screen_events.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from polydata import screen
from polydata.research.calibration import fair_value

HALF_MS = 1795540500000
BLOCK_MS = 15 * 60_000
MAX_SPREAD = 0.10
MARGINS = (0.0, 0.02, 0.05)


def fee(p, r):
    return r * p * (1 - p)


def pair_frame(snap: pd.DataFrame) -> pd.DataFrame:
    s = snap.copy()
    s["fair"] = fair_value(s, k=1.2)
    keep = ["slug", "coin", "timeframe", "window_start_ms", "window_end_ms", "price_to_beat", "fee_rate", "outcome_up",
            "t_ms", "tau_s", "up_bid", "up_ask", "dn_bid", "dn_ask", "up_ask_300", "dn_ask_300", "up_ask_1000",
            "dn_ask_1000", "fair"]
    s = s[keep]
    j = s.merge(s, on=["coin", "window_end_ms", "t_ms"], suffixes=("_a", "_b"))
    j = j[(j.timeframe_a != j.timeframe_b) & (j.price_to_beat_a < j.price_to_beat_b)].copy()
    j["ov_start"] = np.maximum(j.window_start_ms_a, j.window_start_ms_b)
    j["half"] = np.where(j.ov_start < HALF_MS, "H1", "H2")
    j["block"] = j.ov_start // BLOCK_MS
    j["pid"] = j.slug_a + "|" + j.slug_b
    ua, db = j.up_ask_a, j.dn_ask_b
    j["cost"] = ua + db + fee(ua, j.fee_rate_a) + fee(db, j.fee_rate_b)
    j["value"] = 1 + np.clip(j.fair_a - j.fair_b, 0, None)
    j["narrow"] = ((j.up_ask_a - j.up_bid_a) <= MAX_SPREAD + 1e-9) & ((j.dn_ask_b - j.dn_bid_b) <= MAX_SPREAD + 1e-9)
    j["payoff"] = 1.0 + ((j.outcome_up_a == 1) & (j.outcome_up_b == 0)).astype(float)
    return j


def score(j: pd.DataFrame, sig: pd.Series, lat: int) -> dict:
    s = j[sig].sort_values(["pid", "t_ms"]).drop_duplicates("pid")
    ua, db = s[f"up_ask_{lat}_a"], s[f"dn_ask_{lat}_b"]
    ok = ua.between(0, 1, inclusive="neither") & db.between(0, 1, inclusive="neither")
    s = s[ok]
    s = s.assign(pnl=s.payoff - (ua + db + fee(ua, s.fee_rate_a) + fee(db, s.fee_rate_b))[ok], n=1)
    allb = j.drop_duplicates("pid")[["pid", "block"]]
    b = allb.merge(s[["pid", "pnl", "n"]], on="pid", how="left").fillna({"pnl": 0.0, "n": 0}).groupby("block")[["pnl", "n"]].sum()
    n = int(b.n.sum())
    out = {"n": n, "pairs": len(allb), "c_per_pair": b.pnl.sum() / n * 100 if n else np.nan, "ci": (np.nan, np.nan),
           "usd_10": b.pnl.sum() * 10}
    if n:
        rng = np.random.default_rng(0)
        idx = rng.integers(0, len(b), (2000, len(b)))
        p, c = b.pnl.to_numpy()[idx].sum(1), b.n.to_numpy()[idx].sum(1)
        out["ci"] = tuple(np.nanpercentile(np.where(c > 0, p / np.where(c > 0, c, 1), np.nan) * 100, [2.5, 97.5]))
    return out


def fmt(r: dict) -> str:
    return (f"n={r['n']:4d}/{r['pairs']}  {r['c_per_pair']:+7.2f} c/pair [{r['ci'][0]:+.2f},{r['ci'][1]:+.2f}]"
            f"  $ at 10 pairs {r['usd_10']:+.2f}")


def main(out: Path) -> None:
    out.mkdir(parents=True, exist_ok=True)
    snap, _ = screen.load_snapshots(rebuild=False)
    snap = snap[snap.window_end_ms <= 1795564800000]
    voided = set(screen.voided_slugs(snap))
    snap = snap[~snap.slug.isin(voided)].reset_index(drop=True)
    j = pair_frame(snap)
    print(f"voided markets {len(voided)}; pair-rows {len(j)}; pairs {j.pid.nunique()}")
    for h in ("H1", "H2"):
        jh = j[j.half == h]
        rl = (jh.cost < 1) & jh.narrow
        print(f"{h} riskless subset (cost<1 at t): rows {int(rl.sum())}  ->", fmt(score(jh, rl, 300)))
        for m in MARGINS:
            sig = (jh.value - jh.cost > m) & jh.narrow
            print(f"{h} margin {m:.2f}  300ms", fmt(score(jh, sig, 300)), "| 1000ms", fmt(score(jh, sig, 1000)))
    j.to_parquet(out / "ev_pairs.parquet")


if __name__ == "__main__":
    main(Path(sys.argv[1]) if len(sys.argv) > 1 else Path("xtf_out"))

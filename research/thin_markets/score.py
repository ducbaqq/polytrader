"""Score the thin-market screen grids (docs/strategies/thin-markets.md).

usage: uv run --project analysis python research/thin_markets/score.py OUT_DIR
Chooses the margin on 2026-11-24, reports every margin on both days (the chosen one is the result).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd
from scipy.special import ndtr, ndtri

K_VOL = 1.2
MAX_SPREAD = 0.10
MARGINS = (0.01, 0.02, 0.04, 0.07)
SIZE = 10.0
CHOOSE, REPORT = "2026-11-24", "2026-11-27"


def fee(p, r):
    return r * p * (1 - p)


def var_units(tau):
    tau = np.asarray(tau, float)
    return np.where(tau > 60, tau - 40, (tau / 60) ** 2 * tau / 3)


def thin_fair(p_l, k_l, k_t, sig, tau):
    """P(F >= K_T) given P(F >= K_L) = p_l, F normal with sd K_L * K_VOL * sig * sqrt(v(tau))."""
    sd = k_l * K_VOL * sig * np.sqrt(var_units(tau))
    return ndtr(ndtri(p_l) + (k_l - k_t) / sd)


def prepare(g: pd.DataFrame) -> pd.DataFrame:
    g = g.copy()
    g["pair"] = g.t_tf + "~" + g.l_tf
    g["l_mid"] = (g.l_bid + g.l_ask) / 2
    ok = (g.l_ask - g.l_bid <= MAX_SPREAD + 1e-9) & (g.t_ask - g.t_bid <= MAX_SPREAD + 1e-9)
    ok &= g.l_mid.between(0.03, 0.97) & (g.t_bid > 0) & (g.t_ask < 1) & (g.n_ret >= 600) & g.sig15.gt(0)
    g = g[ok].copy()
    g["fair"] = thin_fair(g.l_mid, g.k_l, g.k_t, g.sig15, g.tau_s)
    g["t_mid"] = (g.t_bid + g.t_ask) / 2
    g["edge_up"] = g.fair - g.t_ask - fee(g.t_ask, g.t_fee)
    g["edge_dn"] = g.t_bid - g.fair - fee(1 - g.t_bid, g.t_fee)
    return g


def trades(g: pd.DataFrame, m: float) -> tuple[pd.DataFrame, pd.DataFrame]:
    s = g[(g.edge_up > m) | (g.edge_dn > m)].copy()
    up = s.edge_up >= s.edge_dn
    s["side"] = np.where(up, "Up", "Down")
    s["limit"] = np.where(up, s.t_ask, 1 - s.t_bid)
    s["px"] = np.where(up, s.x_ask, 1 - s.x_bid)
    s["filled"] = s.px.notna() & (s.px <= s.limit + 1e-9) & (s.px > 0) & (s.px < 1)
    s["payoff"] = np.where(up, s.t_out, 1 - s.t_out)
    s["pnl"] = s.payoff - s.px - fee(s.px, s.t_fee)
    f = s[s.filled].sort_values("t_ms").drop_duplicates(["t_slug", "l_tf"])
    return s, f


def boot(blocks_all: np.ndarray, f: pd.DataFrame, n_boot: int = 2000) -> tuple[float, float]:
    b = pd.DataFrame({"end_ms": blocks_all}).merge(
        f.groupby("end_ms").agg(p=("pnl", "sum"), n=("pnl", "size")).reset_index(), how="left").fillna(0)
    if b.n.sum() == 0:
        return (np.nan, np.nan)
    rng = np.random.default_rng(0)
    idx = rng.integers(0, len(b), (n_boot, len(b)))
    p, n = b.p.to_numpy()[idx].sum(1), b.n.to_numpy()[idx].sum(1)
    r = np.where(n > 0, p / np.where(n > 0, n, 1), np.nan) * 100
    return tuple(np.nanpercentile(r, [2.5, 97.5]))


def episodes(s: pd.DataFrame) -> pd.Series:
    if s.empty:
        return pd.Series(dtype=float)
    s = s.sort_values(["t_slug", "l_tf", "t_ms"])
    new = (s.t_slug != s.t_slug.shift()) | (s.l_tf != s.l_tf.shift()) | (s.t_ms - s.t_ms.shift() != 5000)
    return s.groupby(new.cumsum()).size() * 5


def summarize(g: pd.DataFrame, m: float, key: str) -> dict:
    s, f = trades(g, m)
    ep = episodes(s)
    ci = boot(np.unique(g.end_ms), f)
    return {
        "key": key, "margin": m, "thin_mkts": g.t_slug.nunique(), "grid_pts": len(g), "sig_pts": len(s),
        "episodes": len(ep), "ep_med_s": float(ep.median()) if len(ep) else np.nan,
        "sig_mkts": s.drop_duplicates(["t_slug", "l_tf"]).shape[0],
        "fill_rate": float(s.filled.mean()) if len(s) else np.nan, "fills": len(f),
        "c_sh": f.pnl.mean() * 100 if len(f) else np.nan, "ci_lo": ci[0], "ci_hi": ci[1],
        "usd": f.pnl.sum() * SIZE, "won": int((f.payoff == 1).sum()),
    }


def main(out: Path) -> None:
    grids = {d: prepare(pd.read_parquet(out / f"grid_{d}.parquet")) for d in (CHOOSE, REPORT)}
    pd.set_option("display.width", 220)
    pd.set_option("display.max_columns", 30)
    # diagnostic: is the L-implied fair a better forecast of T's outcome than T's own mid?
    for d, g in grids.items():
        b_fair = ((g.fair - g.t_out) ** 2).groupby(g.pair).mean()
        b_mid = ((g.t_mid - g.t_out) ** 2).groupby(g.pair).mean()
        print(f"{d} Brier fair vs T mid:\n", pd.DataFrame({"fair": b_fair, "t_mid": b_mid,
              "rows": g.groupby("pair").size(), "mkts": g.groupby("pair").t_slug.nunique()}).round(4))
    rows = []
    for d, g in grids.items():
        for m in MARGINS:
            rows.append(summarize(g, m, f"{d} ALL"))
            for p, gp in g.groupby("pair"):
                rows.append(summarize(gp, m, f"{d} {p}"))
    res = pd.DataFrame(rows)
    res.to_csv(out / "summary.csv", index=False)
    print(res.round(3).to_string(index=False))
    c = res[(res.key == f"{CHOOSE} ALL") & (res.fills >= 10)]
    m = float(c.sort_values("c_sh").margin.iloc[-1]) if len(c) else MARGINS[-1]
    print(f"\nchosen margin on {CHOOSE}: {m}")
    print(res[res.margin == m].round(3).to_string(index=False))
    _, f = trades(grids[REPORT], m)
    f.to_parquet(out / "fills_report.parquet")
    _, f = trades(grids[CHOOSE], m)
    f.to_parquet(out / "fills_choose.parquet")


if __name__ == "__main__":
    main(Path(sys.argv[1]))

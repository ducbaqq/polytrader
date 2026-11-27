"""Score the BTC-shock event tables (docs/strategies/btc-shock-alts.md).

usage: POLYDATA_DIR=... uv run --project analysis python research/btc_shock/score.py OUT_DIR
Reads OUT_DIR/events_<day>.parquet (from events.py). Chooses (k, m) on 2026-11-24 by the 60 s mid markout at
L = 50 ms (signal beta, alts), reports every variant and the chosen one on both days, plus the descriptive tables.
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from polydata.views import connect

CHOOSE, REPORT = "2026-11-24", "2026-11-27"
KS, MS = (3.0, 4.0), (0.01, 0.03)
LATS = {50: 300, 200: 450}  # latency -> fill offset (latency + 250 ms taker hold)
MAX_SPREAD = 0.03
BLOCK_MS = 15 * 60_000
ALTS = ("eth", "sol", "xrp", "doge", "bnb", "hype", "zec")


def fee(p, r):
    return r * p * (1 - p)


def boot(df: pd.DataFrame, col: str, n: int = 2000, seed: int = 0) -> tuple[float, float, float, int]:
    x = df[["t0", col]].dropna()
    if len(x) < 2:
        return (float(x[col].mean()) * 100 if len(x) else np.nan, np.nan, np.nan, len(x))
    blk = x.groupby(x.t0 // BLOCK_MS)[col].agg(["sum", "count"])
    s, c = blk["sum"].values, blk["count"].values
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(blk), (n, len(blk)))
    est = s[idx].sum(1) / np.maximum(c[idx].sum(1), 1)
    return (100 * s.sum() / c.sum(), 100 * np.quantile(est, 0.025), 100 * np.quantile(est, 0.975), len(x))


def eligible(ev: pd.DataFrame) -> pd.DataFrame:
    sp = ev.up_ask_0 - ev.up_bid_0
    mid = (ev.up_ask_0 + ev.up_bid_0) / 2
    return ev[(sp <= MAX_SPREAD + 1e-9) & mid.between(0.05, 0.95)].copy()


def side(ev: pd.DataFrame, f: str, d) -> pd.Series:
    """Favoured-side value of field f (bid/ask/mid) at offset d."""
    if f == "mid":
        return (side(ev, "bid", d) + side(ev, "ask", d)) / 2
    return pd.Series(np.where(ev.dir > 0, ev[f"up_{f}_{d}"], ev[f"dn_{f}_{d}"]), index=ev.index)


def fair_side(ev: pd.DataFrame, sig: str, d=0) -> pd.Series:
    fv = ev[f"fv_{sig}_{d}"]
    return pd.Series(np.where(ev.dir > 0, fv, 1 - fv), index=ev.index)


def trades(ev: pd.DataFrame, k: float, m: float, sig: str, lat: int) -> pd.DataFrame:
    e = ev[ev.k == k].copy()
    f = fair_side(e, sig)
    a0 = side(e, "ask", 0)
    e = e[(f - a0 - fee(a0, e.fee_rate) > m) & (a0 < 1)].copy()
    off = LATS[lat]
    f = fair_side(e, sig)
    px = side(e, "ask", off)
    e["px"] = px
    e["filled"] = px.notna() & (px < 1) & (f - px - fee(px, e.fee_rate) >= m - 1e-9)
    e["signals"] = 1
    e = e[e.filled].copy()
    c = fee(e.px, e.fee_rate)
    e["mk10"] = side(e, "mid", 10000 + off) - e.px - c
    e["mk60"] = side(e, "mid", 60000 + off) - e.px - c
    won = np.where(e.dir > 0, e.outcome == "Up", e.outcome == "Down").astype(float)
    e["settle"] = np.where(e.outcome.notna(), won - e.px - c, np.nan)
    return e


def fmt(b):
    v, lo, hi, n = b
    return f"{v:+.2f} [{lo:+.1f}, {hi:+.1f}] n={n}" if np.isfinite(lo) else f"{v:+.2f} n={n}"


def describe(ev: pd.DataFrame, day: str) -> None:
    print(f"\n### {day} descriptive")
    for k in KS:
        e = ev[ev.k == k]
        print(f"k={k:g}: shocks {e.t0.nunique()}, |r_btc| median {1e4 * e.drop_duplicates('t0').r_btc.abs().median():.1f} bp")
    e = eligible(ev[ev.k == 3.0])
    e["grp"] = np.where(e.coin == "btc", "btc", "alts")
    rows = []
    for g, x in e.groupby("grp"):
        r = {"grp": g, "rows": len(x)}
        for d in (0, 150, 500, 2000):
            ed = fair_side(x, "beta", d) - side(x, "ask", d) - fee(side(x, "ask", d), x.fee_rate)
            r[f"edge_beta_{d} med/p90 c"] = f"{100 * ed.median():+.1f}/{100 * ed.quantile(.9):+.1f}"
        ed = fair_side(x, "own", 0) - side(x, "ask", 0) - fee(side(x, "ask", 0), x.fee_rate)
        r["edge_own_0 med/p90"] = f"{100 * ed.median():+.1f}/{100 * ed.quantile(.9):+.1f}"
        rp = x.reprice_ms
        r["repriced<=10s %"] = f"{100 * rp.notna().mean():.0f}"
        r["reprice ms p25/50/75"] = "/".join(f"{v:.0f}" for v in rp.quantile([.25, .5, .75]))
        rows.append(r)
    print(pd.DataFrame(rows).to_string(index=False))
    # catch-up: fraction of the (t0 - 1 s -> t0 + 10 s) move done by t0 + d
    alts = e[e.coin != "btc"].drop_duplicates(["t0", "coin"])
    big_b = alts[np.abs(np.log(alts.bn_10000 / alts.bn_pre)) > 0]
    pm = e[np.abs(e.pm_mid_10000 - e.pm_pre) >= 0.02]
    out = []
    for d in (0, 150, 500, 2000):
        fb = np.log(big_b[f"bn_{d}"] / big_b.bn_pre) / np.log(big_b.bn_10000 / big_b.bn_pre)
        bt = np.log(big_b[f"btcm_{d}"] / big_b.btc_pre) / np.log(big_b.btcm_10000 / big_b.btc_pre)
        fpa = (pm[f"pm_mid_{d}"] - pm.pm_pre) / (pm.pm_mid_10000 - pm.pm_pre)
        out.append({"d_ms": d, "BTC bn": f"{bt.median():.2f}", "alt bn": f"{fb.median():.2f}",
                    "alt PM mid": f"{fpa[pm.coin != 'btc'].median():.2f}",
                    "BTC PM mid": f"{fpa[pm.coin == 'btc'].median():.2f}"})
    print("median fraction of the t0-1s -> t0+10s move done by t0+d "
          f"(alt-coin pairs {len(big_b)}, PM rows moving >=2c: alt {int((pm.coin != 'btc').sum())}, btc {int((pm.coin == 'btc').sum())})")
    print(pd.DataFrame(out).to_string(index=False))
    rr = np.log(alts.bn_10000 / alts.bn_pre) / (alts.beta * np.log(alts.btcm_10000 / alts.btc_pre))
    print(f"alt bn 10 s move / (beta * BTC move): median {rr.median():.2f}, beta median {alts.beta.median():.2f}")


def ask_sizes(tr: pd.DataFrame, lat: int) -> pd.Series:
    """Shares at the fill price on the favoured token, from the last price_change at that level."""
    if tr.empty:
        return pd.Series(dtype=float)
    q = tr[["slug", "t0", "dir", "px"]].copy()
    q["outcome"] = np.where(q.dir > 0, "Up", "Down")
    q["tf"] = q.t0 + LATS[lat]
    q["i"] = range(len(q))
    con = connect()
    con.register("q", q)
    days = ",".join(f"'{d}'" for d in (CHOOSE, REPORT))
    r = con.execute(f"""
        SELECT q.i, p.size FROM q ASOF LEFT JOIN
          (SELECT slug, outcome, price, recv_ms, size FROM pm_price_change WHERE date IN ({days}) AND side = 'SELL'
             AND slug IN (SELECT slug FROM q)) p
          ON q.slug = p.slug AND q.outcome = p.outcome AND round(q.px, 3) = round(p.price, 3) AND q.tf >= p.recv_ms
        ORDER BY q.i""").df()
    return r["size"]


def main(out: Path) -> None:
    evs = {d: pd.read_parquet(out / f"events_{d}.parquet") for d in (CHOOSE, REPORT) if (out / f"events_{d}.parquet").exists()}
    for d, ev in evs.items():
        describe(ev, d)
    res = []
    for d, ev in evs.items():
        e = eligible(ev)
        for grp, x in (("alts", e[e.coin.isin(ALTS)]), ("btc", e[e.coin == "btc"])):
            for sig in ("beta", "own"):
                for k in KS:
                    for m in MS:
                        for lat in LATS:
                            tr = trades(x, k, m, sig, lat)
                            res.append({"day": d, "grp": grp, "sig": sig, "k": k, "m": m, "lat": lat,
                                        "fills": len(tr), "shocks": tr.t0.nunique(),
                                        "mk10": fmt(boot(tr, "mk10")), "mk60": fmt(boot(tr, "mk60")),
                                        "settle": fmt(boot(tr, "settle")), "_mk60": boot(tr, "mk60")[0]})
    r = pd.DataFrame(res)
    pick = r[(r.day == CHOOSE) & (r.grp == "alts") & (r.sig == "beta") & (r.lat == 50) & (r.fills >= 5)]
    if pick.empty:
        print("\nno variant with >= 5 fills on the choice day")
        best = (3.0, 0.01)
    else:
        b = pick.sort_values("_mk60").iloc[-1]
        best = (b.k, b.m)
    print(f"\nchosen on {CHOOSE}: k={best[0]:g}, m={best[1]:g}")
    print(r.drop(columns="_mk60").to_string(index=False))
    for d, ev in evs.items():
        e = eligible(ev)
        for lat in LATS:
            tr = trades(e[e.coin.isin(ALTS)], best[0], best[1], "beta", lat)
            if len(tr):
                sz = ask_sizes(tr, lat)
                print(f"{d} chosen alts L={lat}: by coin n/mk60c " + ", ".join(
                    f"{c}:{len(g)}/{100 * g.mk60.mean():+.1f}" for c, g in tr.groupby("coin"))
                    + f"; shares at fill ask median {sz.median():.0f} (known {sz.notna().sum()}/{len(sz)})")


if __name__ == "__main__":
    main(Path(sys.argv[1]))

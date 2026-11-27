"""Tables for docs/strategies/prestart.md from the CSV that build.py writes.

    cd analysis && uv run python ../research/prestart/report.py CSV [A|B]
A: describe the pre-start books, taker and maker screens (all cells). B: the frozen rules (see RULES).
"""
import sys
import numpy as np, pandas as pd
sys.path.insert(0, __file__.rsplit("/", 1)[0])
from build import boot

pd.set_option("display.width", 250); pd.set_option("display.max_columns", 40)
THR = [0.49, 0.48, 0.47]


def long(df):
    """One row per (market, tmin, lat, side)."""
    out = []
    for s in ("u", "d"):
        c = {k: df[f"{s}_{k}"] for k in ("win", "fair", "spread", "tk_pnl", "tk_fairedge")}
        x = df[["slug", "coin", "timeframe", "tm", "lat", "block", "s"]].copy()
        x["side"] = s
        x["ask"] = df["ua" if s == "u" else "da"]
        x["ask_a"] = df["ua_a" if s == "u" else "da_a"]
        x["asz"] = np.where(df[f"{s}_ap"] == x.ask, df[f"{s}_asz"], np.nan)
        x["bsz"] = np.where(df[f"{s}_bp"] == df["ub" if s == "u" else "db"], df[f"{s}_bsz"], np.nan)
        for k, v in c.items():
            x[k] = v
        for b in (48, 49, 50):
            for k in ("placed", "filled", "mk_pnl", "mk_fairedge"):
                x[f"{k}{b}"] = df[f"{s}_{k}{b}"]
        out.append(x)
    return pd.concat(out, ignore_index=True)


def ci(x, col):
    lo, hi = boot(x, col)
    return f"{x[col].mean()*100:+.1f} [{lo*100:+.1f}, {hi*100:+.1f}]"


def describe(df, L):
    base = df[df.lat == 200]
    print("== books by minutes before start (lat 200): n windows quoted, up-mid, spreads, depth, share of windows where a side's ask <thr ==")
    rows = []
    for tf in ("5m", "15m"):
        for tm in sorted(base.tm.unique(), reverse=True):
            x = base[(base.timeframe == tf) & (base.tm == tm)]
            l = L[(L.lat == 200) & (L.timeframe == tf) & (L.tm == tm)]
            if len(x) < 10:
                continue
            r = dict(tf=tf, tmin=tm, n=len(x), mid_absdev_c=(x.upmid - .5).abs().mean() * 100,
                     mid_sd_c=x.upmid.std() * 100, spread_c=l.spread.mean() * 100,
                     ask_sz=l.asz.median(), bid_sz=l.bsz.median(), ask_sum=((x.ua + x.da)).mean())
            for t in THR:
                r[f"any<{t}"] = (((x.ua < t) | (x.da < t))).mean()
            rows.append(r)
    print(pd.DataFrame(rows).round(3).to_string(index=False))


def taker(df, L):
    print("== taker: buy a side whose ask(at decision) < thr; settle c/sh [CI] incl fee, hold, latency; model edge = fair_tilt - ask - fee; and vs plain 0.5 ==")
    rows = []
    for lat in (200, 50):
        for tf in ("5m", "15m"):
            for tm in sorted(df.tm.unique(), reverse=True):
                for t in THR:
                    x = L[(L.lat == lat) & (L.timeframe == tf) & (L.tm == tm) & (L.ask < t) & L.ask_a.notna()]
                    if len(x) < 8:
                        continue
                    x = x.assign(p05=x.win - x.ask_a - 0)
                    rows.append((lat, tf, tm, t, len(x), x.win.mean(), ci(x, "tk_pnl"), x.tk_fairedge.mean() * 100,
                                 (x.win - 0.5).mean() * 100))
    print(pd.DataFrame(rows, columns=["lat", "tf", "tmin", "ask<", "n", "winrate", "settle_c [CI]", "fair_edge_c", "win-0.5_c"]).round(3).to_string(index=False))


def maker(df, L):
    print("== maker: bid b on each side at T-tmin (pessimistic trade-through fill). placed = not crossing; fill = fills/placed; fill_pnl = win-b on fills; all_pnl = win-b on all placed ==")
    rows = []
    for lat in (200, 50):
        for tf in ("5m", "15m"):
            for tm in (3, 1):
                for b in (48, 49, 50):
                    x = L[(L.lat == lat) & (L.timeframe == tf) & (L.tm == tm) & L[f"placed{b}"]]
                    if len(x) < 8:
                        continue
                    f = x[x[f"filled{b}"]]
                    if len(f) < 5:
                        rows.append((lat, tf, tm, b / 100, len(x), len(f), len(f) / len(x), "", "", np.nan, np.nan, np.nan))
                        continue
                    rows.append((lat, tf, tm, b / 100, len(x), len(f), len(f) / len(x), ci(f, f"mk_pnl{b}"),
                                 ci(x, f"mk_pnl{b}"), f.win.mean(), x.win.mean(), f[f"mk_fairedge{b}"].mean() * 100))
    print(pd.DataFrame(rows, columns=["lat", "tf", "tmin", "bid", "placed", "fills", "fill_rate", "fill_pnl_c [CI]",
                                      "all_pnl_c [CI]", "win|fill", "win|all", "fair_edge|fill_c"]).round(3).to_string(index=False))


# Frozen on 2026-11-24 (see journal); evaluated unchanged on 2026-11-27.
def rules(df, L):
    print("== frozen rules (5m+15m, all coins); c/sh [CI, 15-min block bootstrap] ==")
    rows = []
    for lat in (200, 50):
        l = L[L.lat == lat]
        sel = {
            "R1 taker ask<0.49 @T-3": l[(l.tm == 3) & (l.ask < 0.49) & l.ask_a.notna()],
            "R2 taker fair_tilt-ask-fee>3c, spread<=5c @T-0.5": l[(l.tm == 0.5) & (l.tk_fairedge > 0.03) & (l.spread <= 0.05) & l.ask_a.notna()],
        }
        for name, x in sel.items():
            if len(x) >= 3:
                rows.append((name, lat, len(x), x.win.mean(), ci(x, "tk_pnl"), x.tk_fairedge.mean() * 100))
        for name, tf, tm in (("R3 maker bid 0.48 5m @T-3", "5m", 3), ("R4 maker bid 0.48 15m @T-1", "15m", 1)):
            x = l[(l.timeframe == tf) & (l.tm == tm) & l.placed48]
            f = x[x.filled48]
            if len(f) >= 3:
                rows.append((name + f" (fills of {len(x)} placed)", lat, len(f), f.win.mean(), ci(f, "mk_pnl48"),
                             f.mk_fairedge48.mean() * 100))
    print(pd.DataFrame(rows, columns=["rule", "lat", "n", "win", "c/sh [CI]", "fair_edge_c"]).round(3).to_string(index=False))


if __name__ == "__main__":
    df = pd.read_csv(sys.argv[1])
    L = long(df)
    mode = sys.argv[2] if len(sys.argv) > 2 else "A"
    if mode == "A":
        describe(df, L); taker(df, L); maker(df, L)
    else:
        rules(df, L)

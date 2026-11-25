"""Pre-registered wallet study (docs/research/wallets.md): H1 skill -> sharp sets -> H2 persistence ->
copy-trade P&L -> maker adverse selection by top-k wallets.

usage: uv run --project analysis python research/wallets/analyze.py TRADES.parquet [OUT.json]
(TRADES.parquet from build.py). Wallets are printed as their first 6 characters only.
"""

from __future__ import annotations

import json
import sys

import numpy as np
import pandas as pd

BLOCK_MS = 15 * 60_000
MIN_TRADES, MIN_MARKETS, T_MIN = 20, 5, 2.0
MAX_SPREAD = 0.10
N_BOOT, N_RAND = 2000, 500
RNG = np.random.default_rng(20261124)


def load(path: str) -> pd.DataFrame:
    t = pd.read_parquet(path)
    t["edge_settle"] = t.sgn * (t.won - t.price)
    t["mk10"] = t.sgn * (t.mid10 - t.price)
    t["mk60"] = t.sgn * (t.mid60 - t.price)
    t["block"] = t.window_start_ms // BLOCK_MS
    return t


def wallet_scores(t: pd.DataFrame, col: str) -> pd.DataFrame:
    """Share-weighted mean edge per wallet and its t-stat with a market-clustered standard error."""
    d = t[t[col].notna()].assign(se_=lambda x: x[col] * x["size"])
    wm = d.groupby(["wallet", "slug"]).agg(s=("size", "sum"), e=("se_", "sum"), n=("size", "size")).reset_index()
    w = wm.groupby("wallet").agg(S=("s", "sum"), E=("e", "sum"), n=("n", "sum"), G=("slug", "nunique"))
    w["mean"] = w.E / w.S
    wm = wm.join(w["mean"], on="wallet")
    wm["r2"] = (wm.e - wm.s * wm["mean"]) ** 2
    v = wm.groupby("wallet").r2.sum()
    w["se"] = np.sqrt(w.G / (w.G - 1).clip(lower=1) * v / w.S**2)
    w["t"] = w["mean"] / w.se.replace(0, np.nan)
    return w


def wmean(d: pd.DataFrame, col: str, weight: str | None = "size") -> float:
    d = d[d[col].notna()]
    if weight is None:
        return float(d[col].mean()) if len(d) else float("nan")
    return float((d[col] * d[weight]).sum() / d[weight].sum()) if len(d) else float("nan")


def boot_ci(d: pd.DataFrame, col: str, weight: str | None = "size") -> tuple[float, float, float]:
    d = d[d[col].notna()]
    if d.empty:
        return (float("nan"),) * 3
    w = d[weight] if weight else pd.Series(1.0, index=d.index)
    g = pd.DataFrame({"b": d.block, "x": d[col] * w, "w": w}).groupby("b").sum()
    x, ww = g.x.to_numpy(), g.w.to_numpy()
    idx = RNG.integers(0, len(g), size=(N_BOOT, len(g)))
    m = x[idx].sum(1) / ww[idx].sum(1)
    return float(x.sum() / ww.sum()), float(np.quantile(m, 0.025)), float(np.quantile(m, 0.975))


def c(x: float) -> float:
    return round(100 * x, 2)  # dollars per share -> cents


def copy_frame(d: pd.DataFrame, e: str) -> pd.DataFrame:
    """One copied share per trade, entered at probe `e` (e0 / e1)."""
    bid, ask, mbid, mask = d[f"{e}_bid"], d[f"{e}_ask"], d[f"{e}m_bid"], d[f"{e}m_ask"]
    up = d.copy_token == "Up"
    px = np.where(up, ask, 1 - bid)
    mid_after = np.where(up, (mbid + mask) / 2, 1 - (mbid + mask) / 2)
    o = d.assign(px=px, spread=ask - bid, fee=d.fee_rate * px * (1 - px))
    o["pay"] = (o.copy_token == o.mkt_outcome).astype(float)
    o["pnl_settle"] = o.pay - o.px - o.fee
    o["pnl_mk60"] = mid_after - o.px - o.fee
    ok = (o.spread <= MAX_SPREAD + 1e-9) & (o.px > 0) & (o.px < 1)
    return o[ok]


def main(path: str, out: str | None) -> None:
    t = load(path)
    res: dict = {}
    h1, h2 = t[t.h1], t[~t.h1]
    print(f"trades {len(t)}  H1 {len(h1)}  H2 {len(h2)}  wallets {t.wallet.nunique()}")

    # sanity: the copy price right after a trade vs the trade price (BUY X -> we buy X at about p)
    s0 = copy_frame(t[t.side == "BUY"], "e0")
    print(f"sanity: BUY trades, copy ask at +200 ms minus trade price: median {c((s0.px - s0.price).median())}c")

    # all takers
    res["all"] = {h: {col: c(wmean(d, col)) for col in ("edge_settle", "mk10", "mk60")} for h, d in (("H1", h1), ("H2", h2))}
    print("all takers (c/share, size-weighted):", res["all"])

    # H1 skill
    sc = {col: wallet_scores(h1, col) for col in ("mk60", "edge_settle", "mk10")}
    base = sc["mk60"]
    elig = base[(base.n >= MIN_TRADES) & (base.G >= MIN_MARKETS)].index
    sets = {
        "S60": sorted(sc["mk60"].loc[elig].query("t >= @T_MIN").index),
        "Sset": sorted(sc["edge_settle"].reindex(elig).query("t >= @T_MIN").index),
    }
    n1 = h1.groupby("wallet").size().reindex(elig)
    dec = pd.qcut(n1.rank(method="first"), 10, labels=False)
    print(f"eligible wallets {len(elig)};", {k: len(v) for k, v in sets.items()},
          f"overlap {len(set(sets['S60']) & set(sets['Sset']))}")
    res["eligible"] = len(elig)

    for name, ws in sets.items():
        r: dict = {"n_wallets": len(ws)}
        a1, a2 = h1[h1.wallet.isin(ws)], h2[h2.wallet.isin(ws)]
        r["n_active_H2"] = int(a2.wallet.nunique())
        r["trades"] = {"H1": len(a1), "H2": len(a2)}
        r["share_of_trades"] = {"H1": round(len(a1) / len(h1), 3), "H2": round(len(a2) / len(h2), 3)}
        for col in ("edge_settle", "mk60", "mk10"):
            r[f"H1_{col}"] = c(wmean(a1, col))
            m, lo, hi = boot_ci(a2, col)
            r[f"H2_{col}"] = [c(m), c(lo), c(hi)]
            r[f"H2_{col}_eqw"] = c(wmean(a2, col, None))
        # random comparators: same size per H1 trade-count decile
        need = dec.loc[ws].value_counts()
        pools = {k: dec[dec == k].index.to_numpy() for k in need.index}
        h2g = h2.assign(ys=h2.edge_settle * h2["size"], x6=(h2.mk60 * h2["size"]).fillna(0),
                        s6=h2["size"].where(h2.mk60.notna(), 0))
        agg = h2g.groupby("wallet").agg(ys=("ys", "sum"), x6=("x6", "sum"), s=("size", "sum"), s6=("s6", "sum"))
        rs, r6 = [], []
        for _ in range(N_RAND):
            pick = np.concatenate([RNG.choice(pools[k], size=n, replace=False) for k, n in need.items()])
            g = agg.reindex(pick).dropna()
            rs.append(g.ys.sum() / g.s.sum())
            r6.append(g.x6.sum() / g.s6.sum())
        r["random_H2_edge_settle_p50_p95"] = [c(np.quantile(rs, 0.5)), c(np.quantile(rs, 0.95))]
        r["random_H2_mk60_p50_p95"] = [c(np.quantile(r6, 0.5)), c(np.quantile(r6, 0.95))]
        r["pctile_vs_random_settle"] = round(float((np.array(rs) < r["H2_edge_settle"][0] / 100).mean()), 3)
        r["pctile_vs_random_mk60"] = round(float((np.array(r6) < r["H2_mk60"][0] / 100).mean()), 3)
        # rank stability among eligible wallets active in H2
        sc2 = wallet_scores(h2, "mk60" if name == "S60" else "edge_settle")
        col = "mk60" if name == "S60" else "edge_settle"
        both = sc[col].reindex(elig)[["t", "mean"]].join(sc2[["mean", "n"]], rsuffix="_h2", how="inner")
        both = both[both.n >= MIN_TRADES]
        r["spearman_H1t_vs_H2mean"] = round(float(both.t.rank().corr(both.mean_h2.rank())), 3)
        r["n_rank_pairs"] = len(both)
        # actionability
        for e, lab in (("e0", "L0"), ("e1", "Lblock")):
            for h, d in (("H1", a1), ("H2", a2)):
                cf = copy_frame(d, e)
                m, lo, hi = boot_ci(cf, "pnl_settle", None)
                mm, mlo, mhi = boot_ci(cf, "pnl_mk60", None)
                r[f"copy_{lab}_{h}"] = {"n": len(cf), "settle": [c(m), c(lo), c(hi)], "mk60": [c(mm), c(mlo), c(mhi)],
                                        "avg_px": round(float(cf.px.mean()), 3), "avg_fee_c": c(cf.fee.mean()),
                                        "avg_cost_vs_trade_px_c": c((cf.px - np.where(cf.side == "BUY", cf.price, 1 - cf.price)).mean())}
        top = sc[col].loc[ws].sort_values("t", ascending=False).head(10)
        r["top10_H1"] = [{"w": w[:6], "n": int(x.n), "t": round(x.t, 2), "mean_c": c(x["mean"])} for w, x in top.iterrows()]
        res[name] = r
        print(f"\n== {name}\n" + json.dumps(r, indent=1))

    # by-product: maker adverse selection by top-k takers (ranked on H1 $ 60 s markout)
    usd1 = (h1.mk60 * h1["size"]).groupby(h1.wallet).sum().sort_values(ascending=False)
    bp: dict = {}
    for lab, col in (("mk60", "mk60"), ("settle", "edge_settle")):
        d = h2[h2[col].notna()]
        maker = -(d[col] * d["size"])
        loss = maker.clip(upper=0)
        row = {"H2_maker_net_usd": round(float(maker.sum())), "H2_maker_losses_usd": round(float(loss.sum()))}
        for k in (10, 50, 100):
            m = d.wallet.isin(usd1.index[:k])
            row[f"top{k}"] = {"share_of_losses": round(float(loss[m].sum() / loss.sum()), 3),
                              "net_usd": round(float(maker[m].sum())), "share_of_volume": round(float(d.loc[m, 'size'].sum() / d['size'].sum()), 3)}
        bp[lab] = row
    # descriptive, same-period ranking (not out of sample)
    usd2 = (h2.mk60 * h2["size"]).groupby(h2.wallet).sum().sort_values(ascending=False)
    maker2 = -(h2.mk60 * h2["size"])
    bp["mk60_insample_H2_top"] = {k: round(float(maker2[h2.wallet.isin(usd2.index[:k])].clip(upper=0).sum() / maker2.clip(upper=0).sum()), 3)
                                  for k in (10, 50, 100)}
    res["byproduct"] = bp
    print("\n== maker adverse selection by top-k takers (ranked on H1)\n" + json.dumps(bp, indent=1))
    if out:
        with open(out, "w") as f:
            json.dump(res, f, indent=1)


if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2] if len(sys.argv) > 2 else None)

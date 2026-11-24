"""Calibration & fair-value study: recompute every number and chart in docs/research/calibration-study.md.

    cd analysis && uv run python -m polydata.research.study [--out ../docs/research/calibration-study]

Steps: build snapshots -> market calibration -> fair-value model (train/test) -> taker tests ->
maker/taker P&L from all trades -> lead-lag -> latency trade on exchange time -> toxic-flow split.
Writes figures (PNG) and results.json to --out.
"""

from __future__ import annotations

import argparse
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

from ..views import connect
from . import calibration as C
from . import leadlag as L
from .snapshots import DEV_END_MS, build

plt.rcParams.update({"figure.dpi": 130, "axes.grid": True, "grid.alpha": 0.3, "axes.spines.top": False,
                     "axes.spines.right": False, "font.size": 9})


def market_calibration(df: pd.DataFrame, out: Path, res: dict) -> None:
    liq = df[df.spread <= C.MAX_SPREAD].copy()
    rows = []
    for (tf, tb), g in liq.groupby(["timeframe", "tb"], observed=True):
        if len(g) < 500:
            continue
        b = C.fit_logistic(np.c_[np.ones(len(g)), C.logit(g.mid.to_numpy())], g.outcome_up.to_numpy())
        lo, hi = C.block_bootstrap(g, lambda x: C.fit_logistic(np.c_[np.ones(len(x)), C.logit(x.mid.to_numpy())],
                                                               x.outcome_up.to_numpy())[1], n=200)
        rows.append({"timeframe": tf, "time_left": str(tb), "snapshots": len(g), "markets": g.slug.nunique(),
                     "slope": round(b[1], 2), "slope_ci": [round(lo, 2), round(hi, 2)]})
    res["calibration"] = rows

    fig, axes = plt.subplots(1, 3, figsize=(11, 3.6), sharey=True)
    for ax, tf in zip(axes, ("5m", "15m", "1h")):
        g = liq[liq.timeframe == tf]
        for tb in g.tb.cat.categories:
            h = g[g.tb == tb]
            if len(h) < 2000:
                continue
            bins = pd.cut(h.mid, np.linspace(0, 1, 11))
            r = h.groupby(bins, observed=True).agg(p=("mid", "mean"), y=("outcome_up", "mean"), n=("mid", "size"))
            r = r[r.n >= 100]
            ax.plot(r.p, r.y, "o-", ms=3, label=str(tb))
        ax.plot([0, 1], [0, 1], "k--", lw=0.8)
        ax.set_title(f"{tf} markets")
        ax.set_xlabel("Polymarket Up mid")
        ax.legend(title="time left", fontsize=7, title_fontsize=7)
    axes[0].set_ylabel("share that resolved Up")
    fig.suptitle("Calibration: market price vs. outcome (books with spread <= 10c)")
    fig.tight_layout()
    fig.savefig(out / "calibration.png")
    plt.close(fig)


def model_vs_market(df: pd.DataFrame, res: dict) -> tuple[C.Fit, pd.DataFrame]:
    train, test = df[df.split == "train"], df[df.split == "test"]
    fit = C.fit_model(train)
    tl = test[test.spread <= C.MAX_SPREAD]
    y = tl.outcome_up.to_numpy()
    pm = C.fair_value(tl, fit.k, fit.vol, fit.nowcast)
    trl = train[train.spread <= C.MAX_SPREAD]

    def feats(d):
        return np.c_[np.ones(len(d)), C.logit(d.mid.to_numpy()), C.logit(C.fair_value(d, fit.k, fit.vol, fit.nowcast))]

    b = C.fit_logistic(feats(trl), trl.outcome_up.to_numpy())
    p_comb = 1 / (1 + np.exp(-feats(tl) @ b))
    res["model"] = {"fit": fit.__dict__, "test_brier_model": C.brier(pm, y), "test_brier_market": C.brier(tl.mid.to_numpy(), y),
                    "test_logloss_model": C.logloss(pm, y), "test_logloss_market": C.logloss(tl.mid.to_numpy(), y),
                    "test_logloss_market_plus_model": C.logloss(p_comb, y), "combined_weights": [round(x, 3) for x in b]}
    trades = []
    p = C.fair_value(test, fit.k, fit.vol, fit.nowcast)
    for lat in ("300", "1000"):
        for margin in (0.0, 0.02, 0.05, 0.10):
            s = C.summarize_trades(C.taker_signals(test, p, margin, latency=lat))
            trades.append({"latency_ms": int(lat), "margin": margin, **{k: (v if not isinstance(v, float) else round(v, 4))
                                                                         for k, v in s.items()}})
    res["model_taker_test"] = trades
    return fit, test


def maker_pnl(out: Path, res: dict) -> pd.DataFrame:
    con = connect()
    t = con.sql(f"""
        SELECT t.recv_ms, t.ts_ms, t.slug, t.outcome AS token, t.side AS taker_side, t.price, t.size,
               m.coin, m.timeframe, m.fee_rate, m.window_start_ms, (t.outcome = m.outcome)::INT AS token_won
        FROM pm_trade t JOIN markets m USING (slug)
        WHERE m.outcome IS NOT NULL AND m.window_start_ms >= {DEV_END_MS} AND t.ts_ms < m.window_end_ms
    """).df()
    sgn = np.where(t.taker_side == "BUY", 1, -1)
    t["maker_pnl"] = sgn * (t["price"] - t.token_won)
    t["fee"] = C.fee_per_share(t["price"], t.fee_rate)
    t["usd"] = t["price"] * t["size"]
    t["block"] = t.window_start_ms // 900_000
    w = t["size"]
    lo, hi = C.block_bootstrap(t, lambda x: 100 * np.average(x.maker_pnl, weights=x["size"]), n=300)
    res["maker"] = {"trades": len(t), "shares": float(w.sum()), "notional_usd": float(t.usd.sum()),
                    "maker_pnl_usd": float((t.maker_pnl * w).sum()), "taker_fees_usd": float((t.fee * w).sum()),
                    "maker_c_per_share": float(100 * np.average(t.maker_pnl, weights=w)), "maker_ci95": [lo, hi],
                    "by_coin": {c: round(float(100 * np.average(g.maker_pnl, weights=g["size"])), 3) for c, g in t.groupby("coin")},
                    "by_fee_rate": {str(c): round(float(100 * np.average(g.maker_pnl, weights=g["size"])), 3) for c, g in t.groupby("fee_rate")}}
    t["period"] = pd.to_datetime(t.window_start_ms, unit="ms").dt.floor("4h")
    per = t.groupby("period").apply(lambda g: 100 * np.average(g.maker_pnl, weights=g["size"]), include_groups=False)
    fig, ax = plt.subplots(figsize=(6, 3))
    ax.bar(per.index.strftime("%d %H:%M"), per.values, color="#2a7")
    ax.axhline(0, color="k", lw=0.8)
    ax.set_ylabel("maker P&L, cents / share")
    ax.set_title("Liquidity providers' P&L to resolution, per 4 h (all Up/Down markets)")
    fig.tight_layout()
    fig.savefig(out / "maker_pnl_by_period.png")
    plt.close(fig)
    return t


def toxic_flow(t: pd.DataFrame, out: Path, res: dict) -> None:
    t = t[t.timeframe.eq("5m") & t.coin.isin(["btc", "eth"])].copy()
    con = connect()
    con.register("tr", t[["ts_ms", "coin"]].drop_duplicates())
    lo, hi = int(t.ts_ms.min()) - 5000, int(t.ts_ms.max()) + 1000
    mv = con.sql(f"""
        WITH bn AS (SELECT lower(replace(sym, 'USDT', '')) AS coin, recv_ms, (bid + ask) / 2 AS mid FROM bn_fut_bbo
                    WHERE recv_ms BETWEEN {lo} AND {hi} AND sym IN ('BTCUSDT', 'ETHUSDT'))
        SELECT tr.ts_ms, tr.coin, ln(a.mid / b.mid) AS r1s FROM tr
        ASOF LEFT JOIN bn a ON tr.coin = a.coin AND tr.ts_ms + 126 >= a.recv_ms
        ASOF LEFT JOIN bn b ON tr.coin = b.coin AND tr.ts_ms + 126 - 1000 >= b.recv_ms
    """).df()
    t = t.merge(mv, on=["ts_ms", "coin"], how="left")
    pos = np.where((t.token == "Up") == (t.taker_side == "BUY"), 1, -1)
    med = t.groupby("coin").r1s.transform(lambda x: x.abs().median())
    size = t.r1s.abs() / med
    withm = np.sign(t.r1s) * pos > 0
    t["bucket"] = np.select([size < 1, withm & (size >= 3), withm, size >= 3],
                            ["quiet", "with big move", "with small move", "against big move"], "against small move")
    order = ["quiet", "against small move", "against big move", "with small move", "with big move"]
    tab = {}
    fig, ax = plt.subplots(figsize=(7, 3.2))
    for i, coin in enumerate(("btc", "eth")):
        g = t[t.coin == coin]
        a = g.groupby("bucket").apply(lambda x: pd.Series({"share": x["size"].sum(), "maker_c": 100 * np.average(x.maker_pnl, weights=x["size"])}),
                                      include_groups=False).reindex(order)
        a["share"] = a.share / a.share.sum()
        tab[coin] = a.round(3).to_dict(orient="index")
        ax.bar(np.arange(len(order)) + (i - 0.5) * 0.38, a.maker_c, 0.38, label=coin.upper())
    ax.set_xticks(range(len(order)), [o.replace(" ", "\n", 1) for o in order])
    ax.axhline(0, color="k", lw=0.8)
    ax.set_ylabel("maker P&L, cents / share")
    ax.set_title("5m markets: maker P&L by what Binance did in the second before the taker's trade")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out / "toxic_flow.png")
    plt.close(fig)
    res["toxic_flow"] = tab


def lead_lag(out: Path, res: dict) -> None:
    fig, ax = plt.subplots(figsize=(6, 3.4))
    res["leadlag"] = {}
    for coin in ("btc", "eth", "sol", "xrp"):
        r = L.response(L.series(coin))
        r = r[r.lag >= 0]
        ax.plot(r.lag * L.BUCKET_MS, r.cum_from_0, "o-", ms=2.5, label=coin.upper())
        res["leadlag"][coin] = {int(l * L.BUCKET_MS): round(c, 3) for l, c in zip(r.lag, r.cum_from_0) if l in (0, 1, 2, 3, 5, 10, 15)}
    ax.axhline(1, color="k", ls="--", lw=0.8)
    ax.set_xlim(0, 3000)
    ax.set_xlabel("ms after a Binance move (our receive clock)")
    ax.set_ylabel("share of fair-value move\nin Polymarket mid")
    ax.set_title("How fast 5m markets price in Binance moves")
    ax.legend()
    fig.tight_layout()
    fig.savefig(out / "leadlag.png")
    plt.close(fig)


def latency_trade(out: Path, res: dict) -> None:
    fig, ax = plt.subplots(figsize=(6, 3.2))
    res["latency_trade"] = {}
    arrivals = (0, 100, 150, 200, 250, 300, 400, 600)
    for coin, fee in (("btc", 0.05), ("eth", 0.01)):
        s, book = L.series(coin), L.exchange_book(coin)
        vals, ses = [], []
        for a in arrivals:
            t = L.latency_trade_exchange(s, book, a, 2000, fee)
            t = t.groupby(t.t // 2000).first()
            vals.append(100 * t.pnl.mean())
            ses.append(100 * t.pnl.std() / np.sqrt(len(t)))
        res["latency_trade"][coin] = {"arrival_ms": list(arrivals), "pnl_c": [round(v, 3) for v in vals], "n": int(len(t))}
        ax.errorbar(arrivals, vals, yerr=1.96 * np.array(ses), fmt="o-", ms=3, capsize=2, label=f"{coin.upper()} 5m")
    ax.axhline(0, color="k", lw=0.8)
    ax.axvspan(200, 300, color="orange", alpha=0.15, label="realistic from Singapore")
    ax.set_xlabel("order arrival at exchange after we receive the Binance move (ms)")
    ax.set_ylabel("P&L, cents / share\n(after taker fee)")
    ax.set_title("Latency trade: buy after a Binance jump, mark 2 s later")
    ax.legend(fontsize=7)
    fig.tight_layout()
    fig.savefig(out / "latency_trade.png")
    plt.close(fig)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default=str(Path(__file__).resolve().parents[4] / "docs" / "research" / "calibration-study"))
    ap.add_argument("--skip-snapshots", action="store_true")
    a = ap.parse_args()
    out = Path(a.out)
    out.mkdir(parents=True, exist_ok=True)
    if not a.skip_snapshots:
        build()
    df = C.load()
    df = df[df.cl_px.notna() & df.sig15.notna() & df.up_bid.notna()].copy()
    df["tb"] = C.tau_bucket(df)
    res = {"period": [str(pd.to_datetime(df.window_start_ms.min(), unit="ms")), str(pd.to_datetime(df.window_end_ms.max(), unit="ms"))],
           "markets": int(df.slug.nunique()), "snapshots": len(df)}
    for name, fn in (("calibration", lambda: market_calibration(df, out, res)), ("model", lambda: model_vs_market(df, res)),
                     ("lead-lag", lambda: lead_lag(out, res)), ("latency trade", lambda: latency_trade(out, res))):
        print(f"- {name}")
        fn()
    print("- maker P&L")
    t = maker_pnl(out, res)
    print("- toxic flow")
    toxic_flow(t, out, res)
    (out / "results.json").write_text(json.dumps(res, indent=1, default=str))
    print(f"wrote {out}")


if __name__ == "__main__":
    main()

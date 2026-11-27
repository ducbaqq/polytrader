"""BTC-shock event table for alt (and BTC) Up/Down books (docs/strategies/btc-shock-alts.md).

usage: POLYDATA_DIR=... uv run --project analysis python research/btc_shock/events.py DAY OUT_DIR

DAY is 2026-11-24 (train; events from 10:36 UTC) or 2026-11-27 (held out; events from 01:00 UTC so that beta
has an hour of history). Writes OUT_DIR/events_DAY.parquet: one row per (k, shock, open 5m/15m market) with
the Binance and Polymarket state at t0 + fixed offsets. Everything at time t is the last row received at or
before t (Binance on a 100 ms grid: the last update of the last complete 100 ms bucket).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from polydata.bt.fairvalue import fair_value
from polydata.bt.splits import REPLAY_START_MS
from polydata.views import connect

KS = (3.0, 4.0)
DECLUSTER_MS = 30_000
SIG_S, BETA_S, BETA_STEP_S = 900, 3600, 10
COINS = ("btc", "eth", "sol", "xrp", "doge", "bnb", "hype", "zec")
OFFSETS = (0, 150, 300, 450, 500, 2000, 10000, 10300, 10450, 60300, 60450)
FV_OFFSETS = (0, 150, 500, 2000)
CATCH = (0, 150, 500, 2000, 10000)
DAY0 = {"2026-11-24": 1795478400000, "2026-11-27": 1795737600000}
START = {"2026-11-24": REPLAY_START_MS, "2026-11-27": 1795741200000}


def grids(con, day: str):
    """100 ms grid of futures mids per coin: value at bucket b = last update received in [100b, 100b+100)."""
    d0 = DAY0[day]
    n = 864_000
    df = con.execute(f"""
        SELECT lower(replace(sym, 'USDT', '')) coin, recv_ms // 100 - {d0 // 100} b,
               arg_max((bid + ask) / 2, seq) mid, max(recv_ms) r
        FROM bn_fut_bbo WHERE date = '{day}' GROUP BY ALL""").df()
    g, last = {}, {}
    for c, x in df.groupby("coin"):
        a = np.full(n, np.nan)
        rr = np.full(n, -1, dtype=np.int64)
        a[x.b.values] = x.mid.values
        rr[x.b.values] = x.r.values
        a = pd.Series(a).ffill().values
        g[c], last[c] = a, rr
    return d0, g, last


def at(g, d0, t):
    """Mid known at time t (ms): last complete 100 ms bucket."""
    i = (np.asarray(t, dtype=np.int64) - d0) // 100 - 1
    return g[np.clip(i, 0, len(g) - 1)]


def sec_grid(g):
    return g[9::10]  # value at the end of each second


def sig15(s1, sec):
    """sqrt(mean r^2) over the 900 1 s returns ending at second sec - 1 (complete)."""
    r = np.diff(np.log(s1[sec - SIG_S - 1:sec]))
    return float(np.sqrt(np.nanmean(r ** 2)))


def beta(sa, sb, sec):
    lo = sec - BETA_S
    a = np.diff(np.log(sa[lo:sec:BETA_STEP_S]))
    b = np.diff(np.log(sb[lo:sec:BETA_STEP_S]))
    ok = np.isfinite(a) & np.isfinite(b)
    if ok.sum() < 100 or np.var(b[ok]) == 0:
        return np.nan
    return float(np.cov(a[ok], b[ok])[0, 1] / np.var(b[ok], ddof=1))


def shocks(d0, g, last, k, lo, hi):
    btc = g["btc"]
    r = np.log(btc[10:] / btc[:-10])  # bucket b vs b - 10
    s1 = sec_grid(btc)
    r2 = np.r_[0.0, np.diff(np.log(s1)) ** 2]
    r2 = np.nan_to_num(r2)
    cs = np.cumsum(r2)
    b = np.arange(10, len(btc))
    t_b = d0 + (b + 1) * 100
    sec = (t_b - d0) // 1000  # current (incomplete) second; complete returns end at sec - 1
    j = sec - 1
    sig = np.sqrt(np.maximum(cs[np.clip(j, 0, len(cs) - 1)] - cs[np.clip(j - SIG_S, 0, len(cs) - 1)], 0) / SIG_S)
    hit = (np.abs(r) > k * sig) & (t_b >= lo) & (t_b < hi) & (j >= SIG_S) & np.isfinite(r)
    out, nxt = [], -1
    for i in np.flatnonzero(hit):
        if t_b[i] < nxt:
            continue
        bb = b[i]
        t0 = int(last["btc"][bb]) if last["btc"][bb] > 0 else int(t_b[i])
        out.append((t0, int(np.sign(r[i])), float(r[i]), float(sig[i])))
        nxt = t_b[i] + DECLUSTER_MS
    return pd.DataFrame(out, columns=["t0", "dir", "r_btc", "sig_btc"])


def asof(times, rt, vals):
    i = np.searchsorted(rt, times, side="right") - 1
    v = vals[np.clip(i, 0, None)].astype(float)
    v[i < 0] = np.nan
    return v


def main(day: str, out: Path) -> None:
    con = connect()
    d0, g, last = grids(con, day)
    hi = int(con.execute(f"SELECT max(recv_ms) FROM bn_fut_bbo WHERE date = '{day}'").fetchone()[0]) - 70_000
    lo = START[day]
    sh = pd.concat([shocks(d0, g, last, k, lo, hi).assign(k=k) for k in KS], ignore_index=True)
    print(f"{day}: shocks k=3 {int((sh.k == 3).sum())}, k=4 {int((sh.k == 4).sum())}", flush=True)

    mk = con.execute(f"""
        SELECT m.slug, m.coin, m.timeframe tf, m.window_start_ms ws, m.window_end_ms we, m.fee_rate,
               m.outcome, coalesce(m.price_to_beat, (SELECT avg(price) FROM chainlink c WHERE c.coin = m.coin
                 AND c.ts_ms >= m.window_start_ms - 60000 AND c.ts_ms < m.window_start_ms)) AS K
        FROM markets m WHERE m.timeframe IN ('5m', '15m') AND m.window_end_ms > {lo}
          AND m.window_start_ms < {hi + 70_000}""").df()
    ev = sh.merge(mk, how="cross")
    ev = ev[(ev.ws <= ev.t0) & (ev.we - ev.t0 > 60_000) & ev.K.notna()].reset_index(drop=True)
    ev["tau_s"] = (ev.we - ev.t0) / 1000

    cl = con.execute(f"""SELECT coin, recv_ms, price FROM chainlink WHERE recv_ms >= {lo - 600_000}
                         AND recv_ms < {hi + 70_000} ORDER BY coin, recv_ms""").df()
    s1 = {c: sec_grid(g[c]) for c in COINS}
    for c in COINS:
        m = ev.coin == c
        if not m.any():
            continue
        e = ev[m]
        sec = ((e.t0.values - d0) // 1000).astype(int)
        ev.loc[m, "sig"] = [sig15(s1[c], s) for s in sec]
        ev.loc[m, "beta"] = 1.0 if c == "btc" else [beta(s1[c], s1["btc"], s) for s in sec]
        x = cl[cl.coin == c]
        rt, px = x.recv_ms.values, x.price.values
        t0 = e.t0.values
        ev.loc[m, "cl_px"] = asof(t0, rt, px)
        cl_t = rt[np.clip(np.searchsorted(rt, t0, side="right") - 1, 0, None)]
        ev.loc[m, "bn_at_cl"] = at(g[c], d0, cl_t)
        ev.loc[m, "bn_pre"] = at(g[c], d0, t0 - 1000)
        ev.loc[m, "btc_pre"] = at(g["btc"], d0, t0 - 1000)
        for d in sorted(set(FV_OFFSETS) | set(CATCH)):
            ev.loc[m, f"bn_{d}"] = at(g[c], d0, t0 + d)
            ev.loc[m, f"btcm_{d}"] = at(g["btc"], d0, t0 + d)

    # fair values (tau > 60 s branch; tau at t0 used for all offsets: <= 2 s difference)
    for d in FV_OFFSETS:
        own, bet = [], []
        for row in ev.itertuples():
            bn = getattr(row, f"bn_{d}")
            r_btc = np.log(getattr(row, f"btcm_{d}") / row.btc_pre)
            r_alt = np.log(bn / row.bn_pre)
            bn_b = bn * np.exp(row.beta * r_btc - r_alt) if np.isfinite(row.beta) else np.nan
            args = (row.cl_px, None, None, row.K, row.sig, row.tau_s - d / 1000, 0, None)
            ok = np.isfinite(row.cl_px) and np.isfinite(row.sig) and row.sig > 0
            own.append(fair_value(row.cl_px, bn, row.bn_at_cl, *args[3:]) if ok else np.nan)
            bet.append(fair_value(row.cl_px, bn_b, row.bn_at_cl, *args[3:]) if ok and np.isfinite(bn_b) else np.nan)
        ev[f"fv_own_{d}"], ev[f"fv_beta_{d}"] = own, bet

    # Polymarket tops of both tokens around each event
    iv = ev[["slug", "t0"]].drop_duplicates().assign(a=lambda x: x.t0 - 30_000, b=lambda x: x.t0 + 62_000)
    con.register("iv", iv)
    bk = con.execute(f"""
        SELECT q.slug, q.outcome, q.recv_ms, q.seq, q.best_bid bid, q.best_ask ask
        FROM pm_best_bid_ask q
        WHERE q.date IN ('{day}') AND q.slug IN (SELECT DISTINCT slug FROM iv)
          AND EXISTS (SELECT 1 FROM iv WHERE iv.slug = q.slug AND q.recv_ms BETWEEN iv.a AND iv.b)
        ORDER BY q.slug, q.outcome, q.recv_ms, q.seq""").df()
    print(f"book rows {len(bk)}", flush=True)
    books = {key: (x.recv_ms.values, x.bid.values, x.ask.values) for key, x in bk.groupby(["slug", "outcome"])}
    cols = {f"{s}_{f}_{d}": np.full(len(ev), np.nan) for s in ("up", "dn") for f in ("bid", "ask") for d in OFFSETS}
    rep = np.full(len(ev), np.nan)
    pm_catch = {d: np.full(len(ev), np.nan) for d in CATCH}
    pm_pre = np.full(len(ev), np.nan)
    for i, row in enumerate(ev.itertuples()):
        for s, o in (("up", "Up"), ("dn", "Down")):
            b = books.get((row.slug, o))
            if b is None:
                continue
            rt, bid, ask = b
            ts = row.t0 + np.array(OFFSETS)
            for f, v in (("bid", bid), ("ask", ask)):
                vals = asof(ts, rt, v)
                for d, x in zip(OFFSETS, vals):
                    cols[f"{s}_{f}_{d}"][i] = x
            if s == "up":
                mid = (bid + ask) / 2
                m0 = asof([row.t0], rt, mid)[0]
                pm_pre[i] = asof([row.t0 - 1000], rt, mid)[0]
                for d in CATCH:
                    pm_catch[d][i] = asof([row.t0 + d], rt, mid)[0]
                w = (rt > row.t0) & (rt <= row.t0 + 10_000)
                hit = np.flatnonzero(w & (row.dir * (mid - m0) >= 0.01 - 1e-9))
                if len(hit):
                    rep[i] = rt[hit[0]] - row.t0
    for c, v in cols.items():
        ev[c] = v
    ev["reprice_ms"], ev["pm_pre"] = rep, pm_pre
    for d in CATCH:
        ev[f"pm_mid_{d}"] = pm_catch[d]
    out.mkdir(parents=True, exist_ok=True)
    ev.to_parquet(out / f"events_{day}.parquet")
    print(f"events {len(ev)} -> {out}", flush=True)


if __name__ == "__main__":
    main(sys.argv[1], Path(sys.argv[2]))

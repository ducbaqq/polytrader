"""How fast does the Polymarket Up price react to Binance moves?

For each coin, on a grid of BUCKET_MS buckets (by our receive time), take the Binance futures mid
return in each bucket and the change of the live 5m market's Up mid. The fair-value change implied by
a Binance return r is  dP = phi(z) / (sigma * sqrt(tau_eff)) * r,  where z = Phi^-1(mid) and tau_eff is
the remaining variance time (tau - 40 s before the settlement window). Regressing the Polymarket mid
change at lag L on the implied change gives the fraction of a fair-value move that shows up in the
Polymarket price after L buckets. In an instantly efficient market the fractions sum to ~1, all at lag 0.
"""

from __future__ import annotations

import numpy as np
import pandas as pd
from scipy.special import ndtri
from scipy.stats import norm

from ..views import connect

BUCKET_MS = 200
DEV_END_MS = 1795456000000


def series(coin: str, timeframe: str = "5m", bucket_ms: int = BUCKET_MS, max_spread: float = 0.03) -> pd.DataFrame:
    con = connect()
    sym = coin.upper() + "USDT"
    lo, hi = con.execute(f"""SELECT min(window_start_ms), max(window_end_ms) FROM markets
                             WHERE coin = '{coin}' AND timeframe = '{timeframe}' AND outcome IS NOT NULL
                               AND window_start_ms >= {DEV_END_MS} + 3600000""").fetchall()[0]
    return con.sql(f"""
        WITH g AS (SELECT b * {bucket_ms} AS t FROM range({lo // bucket_ms}, {hi // bucket_ms}) r(b)),
        bn AS (SELECT recv_ms, (bid + ask) / 2 AS mid FROM bn_fut_bbo WHERE sym = '{sym}' AND recv_ms BETWEEN {lo - 900000} AND {hi}),
        vol AS (  -- 15-min realised vol per second (log, per sqrt s)
            SELECT sec, sqrt(avg(r * r) OVER (ORDER BY sec ROWS BETWEEN 899 PRECEDING AND CURRENT ROW)) AS sig
            FROM (SELECT sec, ln(mid / lag(mid) OVER (ORDER BY sec)) AS r
                  FROM (SELECT recv_ms // 1000 AS sec, arg_max(mid, recv_ms) AS mid FROM bn GROUP BY 1))),
        mk AS (SELECT slug, window_start_ms, window_end_ms FROM markets WHERE coin = '{coin}' AND timeframe = '{timeframe}'),
        pm AS (SELECT slug, recv_ms, (best_bid + best_ask) / 2 AS mid, best_ask - best_bid AS spread
               FROM pm_best_bid_ask WHERE outcome = 'Up' AND slug IN (SELECT slug FROM mk) AND recv_ms BETWEEN {lo} AND {hi}),
        gm AS (SELECT g.t, mk.slug, (mk.window_end_ms - g.t) / 1000.0 AS tau FROM g JOIN mk
               ON g.t >= mk.window_start_ms AND g.t < mk.window_end_ms)
        SELECT gm.t, gm.slug, gm.tau, b.mid AS bn_mid, p.mid AS pm_mid, p.spread, v.sig
        FROM gm
        ASOF LEFT JOIN bn b ON gm.t >= b.recv_ms
        ASOF LEFT JOIN pm p ON gm.slug = p.slug AND gm.t >= p.recv_ms
        ASOF LEFT JOIN vol v ON gm.t // 1000 - 1 >= v.sec
        ORDER BY gm.t
    """).df().assign(max_spread=max_spread)


def response(df: pd.DataFrame, lags: range = range(-5, 31)) -> pd.DataFrame:
    """Fraction of the implied fair-value change seen in the Polymarket mid after each lag (buckets)."""
    d = df.copy()
    d["ret"] = np.log(d.bn_mid / d.bn_mid.shift(1))
    d["dpm"] = d.pm_mid - d.pm_mid.shift(1)
    same = d.slug == d.slug.shift(1)
    tau_eff = np.where(d.tau > 60, d.tau - 40, np.maximum(d.tau, 1) / 3)
    z = ndtri(np.clip(d.pm_mid, 0.01, 0.99))
    d["implied"] = norm.pdf(z) / (d.sig * np.sqrt(tau_eff)) * d.ret
    ok = same & (d.spread <= d.max_spread) & d.tau.between(30, 270) & d.pm_mid.between(0.1, 0.9) & d.implied.notna()
    x = d.implied.where(ok)
    out = []
    for L in lags:
        y = d.dpm.shift(-L).where(d.slug.shift(-L) == d.slug)  # mid change L buckets later, same market
        m = x.notna() & y.notna()
        xx, yy = x[m].to_numpy(), y[m].to_numpy()
        beta = float((xx * yy).sum() / (xx * xx).sum())
        resid = yy - beta * xx
        se = float(np.sqrt((resid ** 2).sum() / (len(xx) - 1) / (xx * xx).sum()))
        out.append({"lag": L, "beta": beta, "se": se, "n": int(m.sum())})
    r = pd.DataFrame(out)
    r["cum_from_0"] = r.beta.where(r.lag >= 0, 0).cumsum()
    return r


def latency_trade(df: pd.DataFrame, latency_buckets: int, hold_buckets: int, fee_rate: float,
                  min_edge: float = 0.0) -> pd.DataFrame:
    """Pure latency arbitrage: when the implied fair-value change of the last bucket exceeds the cost
    of crossing (half spread + taker fee) by min_edge, buy that side at the ask `latency_buckets`
    later and mark it at the mid `hold_buckets` after the fill. Returns one row per signal with the
    markout P&L per share (fee charged on entry only; holding to resolution avoids an exit fee).
    """
    from ..fees import fee_per_share

    d = df.copy()
    d["ret"] = np.log(d.bn_mid / d.bn_mid.shift(1))
    tau_eff = np.where(d.tau > 60, d.tau - 40, np.maximum(d.tau, 1) / 3)
    z = ndtri(np.clip(d.pm_mid, 0.01, 0.99))
    d["implied"] = norm.pdf(z) / (d.sig * np.sqrt(tau_eff)) * d.ret
    half = d.spread / 2
    cost = half + fee_per_share(d.pm_mid + half, fee_rate)
    sig_up = d.implied > cost + min_edge
    sig_dn = -d.implied > cost + min_edge
    ok = (d.slug == d.slug.shift(-(latency_buckets + hold_buckets))) & d.tau.between(30, 270) & (d.spread <= 0.03)
    L, H = latency_buckets, hold_buckets
    ask_up = (d.pm_mid + d.spread / 2).shift(-L)
    bid_up = (d.pm_mid - d.spread / 2).shift(-L)
    mid_later = d.pm_mid.shift(-(L + H))
    up = pd.DataFrame({"t": d.t, "slug": d.slug, "side": "Up", "price": ask_up,
                       "pnl": mid_later - ask_up - fee_per_share(ask_up, fee_rate)})[sig_up & ok]
    # buying Down at its ask = 1 - Up bid; its value later = 1 - Up mid
    dn = pd.DataFrame({"t": d.t, "slug": d.slug, "side": "Down", "price": 1 - bid_up,
                       "pnl": (1 - mid_later) - (1 - bid_up) - fee_per_share(1 - bid_up, fee_rate)})[sig_dn & ok]
    return pd.concat([up, dn]).dropna().sort_values("t")


def exchange_book(coin: str, timeframe: str = "5m", bucket_ms: int = 50) -> pd.DataFrame:
    """Up top of book on a grid of *exchange* time (Polymarket ts_ms), to price fills at the moment
    an order would arrive at the exchange rather than when we would have seen it."""
    con = connect()
    lo, hi = con.execute(f"""SELECT min(window_start_ms), max(window_end_ms) FROM markets
                             WHERE coin = '{coin}' AND timeframe = '{timeframe}' AND outcome IS NOT NULL
                               AND window_start_ms >= {DEV_END_MS} + 3600000""").fetchall()[0]
    return con.sql(f"""
        WITH g AS (SELECT b * {bucket_ms} AS x FROM range({lo // bucket_ms}, {hi // bucket_ms}) r(b)),
        mk AS (SELECT slug, window_start_ms, window_end_ms FROM markets WHERE coin = '{coin}' AND timeframe = '{timeframe}'),
        pm AS (SELECT slug, ts_ms, best_bid, best_ask FROM pm_best_bid_ask
               WHERE outcome = 'Up' AND slug IN (SELECT slug FROM mk) AND recv_ms BETWEEN {lo} AND {hi} + 60000),
        gm AS (SELECT g.x, mk.slug FROM g JOIN mk ON g.x >= mk.window_start_ms AND g.x < mk.window_end_ms)
        SELECT gm.x, gm.slug, p.best_bid, p.best_ask FROM gm ASOF LEFT JOIN pm p ON gm.slug = p.slug AND gm.x >= p.ts_ms
        ORDER BY gm.x
    """).df()


def latency_trade_exchange(df: pd.DataFrame, book: pd.DataFrame, arrive_ms: int, hold_ms: int, fee_rate: float,
                           min_edge: float = 0.0) -> pd.DataFrame:
    """Like latency_trade, but the decision uses what we had received by t (Binance + Polymarket by
    recv time) and the fill uses the exchange's book at t + arrive_ms (by Polymarket's ts_ms), marked
    at the exchange mid hold_ms after the fill. Fee on entry only."""
    from ..fees import fee_per_share

    d = df.copy()
    d["ret"] = np.log(d.bn_mid / d.bn_mid.shift(1))
    tau_eff = np.where(d.tau > 60, d.tau - 40, np.maximum(d.tau, 1) / 3)
    z = ndtri(np.clip(d.pm_mid, 0.01, 0.99))
    d["implied"] = norm.pdf(z) / (d.sig * np.sqrt(tau_eff)) * d.ret
    half = d.spread / 2
    cost = half + fee_per_share(d.pm_mid + half, fee_rate)
    d = d[d.tau.between(30, 270) & (d.spread <= 0.03) & ((d.implied.abs()) > cost + min_edge)].copy()
    d["side"] = np.where(d.implied > 0, "Up", "Down")
    b = book.set_index(["slug", "x"]).sort_index()
    step = int(book.x.iloc[1] - book.x.iloc[0])

    def at(slug, ms):
        key = (slug, int(ms // step * step))
        return b.loc[key] if key in b.index else None

    rows = []
    for r in d.itertuples():
        f, m = at(r.slug, r.t + arrive_ms), at(r.slug, r.t + arrive_ms + hold_ms)
        if f is None or m is None or pd.isna(f.best_ask) or pd.isna(m.best_ask):
            continue
        mid_later = (m.best_bid + m.best_ask) / 2
        if r.side == "Up":
            price, value = f.best_ask, mid_later
        else:
            price, value = 1 - f.best_bid, 1 - mid_later
        rows.append((r.t, r.slug, r.side, price, value - price - fee_per_share(price, fee_rate)))
    return pd.DataFrame(rows, columns=["t", "slug", "side", "price", "pnl"])

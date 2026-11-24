"""Calibration of Polymarket Up/Down prices and a fair-value model.

Uses data/research/snapshots.parquet (see snapshots.py). Everything is evaluated out of sample:
parameters are fitted on the first half of the windows (by start time) and scored on the second
half. Uncertainty comes from a block bootstrap over 15-minute blocks of window start times, because
outcomes of different coins (and overlapping timeframes) in the same period are strongly correlated.
"""

from __future__ import annotations

import math
from dataclasses import dataclass

import numpy as np
import pandas as pd
from scipy.special import ndtr  # standard normal CDF

from ..config import paths
from ..fees import fee_per_share

EPS = 1e-4
MAX_SPREAD = 0.10  # mids from wider books are not a usable market probability


# ---------------------------------------------------------------------------------------------
# data

def load(path: str | None = None) -> pd.DataFrame:
    df = pd.read_parquet(path or paths().root / "research" / "snapshots.parquet")
    df["block"] = df.window_start_ms // (15 * 60_000)  # bootstrap cluster
    df["frac_left"] = df.tau_s / ((df.window_end_ms - df.window_start_ms) / 1000)
    df["mid"] = (df.up_bid + df.up_ask) / 2
    df["spread"] = df.up_ask - df.up_bid
    cut = df.window_start_ms.quantile(0.5)
    df["split"] = np.where(df.window_start_ms < cut, "train", "test")
    return df


def tau_bucket(df: pd.DataFrame) -> pd.Series:
    edges = [-1, 10, 30, 60, 120, 300, 900, 3600]
    labels = ["0-10s", "10-30s", "30-60s", "1-2m", "2-5m", "5-15m", "15-60m"]
    return pd.cut(df.tau_s, edges, labels=labels, right=True)


# ---------------------------------------------------------------------------------------------
# fair-value model

def fair_value(df: pd.DataFrame, k: float = 1.0, vol: str = "sig15", nowcast: bool = True,
               fat_tail_df: float | None = None) -> np.ndarray:
    """P(settlement average >= price_to_beat) under a driftless random walk.

    Settlement = mean of Chainlink prices over the last 60 s. With tau seconds left and per-second
    log vol sigma (Binance realised, scaled by k):
      tau > 60:  log(avg) ~ N(log S, sigma^2 (tau - 40))     [(tau - 60) before the window + 60/3 inside]
      tau <= 60: avg = w * observed_avg + (1 - w) * future_avg, w = n_obs / (n_obs + tau),
                 future_avg ~ N(S, (S sigma)^2 tau / 3)
    S = last Chainlink price, moved by the Binance return since that Chainlink tick (nowcast), because
    Chainlink arrives ~2 s late and Binance leads it.
    """
    S = df.cl_px.to_numpy(float)
    if nowcast:
        ratio = (df.bn_mid / df.bn_mid_at_cl).to_numpy(float)
        S = S * np.where(np.isfinite(ratio), ratio, 1.0)
    K = df.price_to_beat.to_numpy(float)
    sig = k * df[vol].to_numpy(float)
    tau = df.tau_s.to_numpy(float)
    n_obs = df.cl_obs_n.to_numpy(float)
    obs = df.cl_obs_avg.to_numpy(float)

    z = np.empty_like(S)
    pre = tau > 60
    z[pre] = np.log(S[pre] / K[pre]) / (sig[pre] * np.sqrt(tau[pre] - 40))
    inw = ~pre
    w = np.where(n_obs[inw] > 0, n_obs[inw] / (n_obs[inw] + np.maximum(tau[inw], 0.5)), 0.0)
    obs_i = np.where(n_obs[inw] > 0, obs[inw], S[inw])
    mean = w * obs_i + (1 - w) * S[inw]
    sd = (1 - w) * S[inw] * sig[inw] * np.sqrt(np.maximum(tau[inw], 0.5) / 3)
    z[inw] = (mean - K[inw]) / sd
    if fat_tail_df:
        from scipy.stats import t as student_t
        p = student_t.cdf(z, fat_tail_df)
    else:
        p = ndtr(z)
    return np.clip(p, EPS, 1 - EPS)


# ---------------------------------------------------------------------------------------------
# scoring

def logloss(p: np.ndarray, y: np.ndarray) -> float:
    p = np.clip(p, EPS, 1 - EPS)
    return float(-np.mean(y * np.log(p) + (1 - y) * np.log(1 - p)))


def brier(p: np.ndarray, y: np.ndarray) -> float:
    return float(np.mean((p - y) ** 2))


def logit(p):
    p = np.clip(p, EPS, 1 - EPS)
    return np.log(p / (1 - p))


def fit_logistic(X: np.ndarray, y: np.ndarray, iters: int = 50) -> np.ndarray:
    """Plain Newton-Raphson logistic regression (X includes the intercept column)."""
    b = np.zeros(X.shape[1])
    for _ in range(iters):
        p = 1 / (1 + np.exp(-X @ b))
        W = p * (1 - p) + 1e-9
        H = X.T @ (X * W[:, None]) + 1e-6 * np.eye(X.shape[1])
        step = np.linalg.solve(H, X.T @ (y - p))
        b += step
        if np.max(np.abs(step)) < 1e-8:
            break
    return b


def block_bootstrap(df: pd.DataFrame, stat, n: int = 300, seed: int = 0) -> tuple[float, float]:
    """95% interval of stat(df) resampling 15-minute blocks with replacement."""
    rng = np.random.default_rng(seed)
    groups = [g for _, g in df.groupby("block")]
    vals = []
    for _ in range(n):
        pick = rng.integers(0, len(groups), len(groups))
        vals.append(stat(pd.concat([groups[i] for i in pick])))
    return float(np.nanpercentile(vals, 2.5)), float(np.nanpercentile(vals, 97.5))


@dataclass
class Fit:
    k: float
    vol: str
    nowcast: bool
    train_logloss: float


def fit_model(train: pd.DataFrame) -> Fit:
    best = None
    for vol in ("sig15", "sig60"):
        for nowcast in (False, True):
            for k in np.arange(0.6, 2.01, 0.05):
                ll = logloss(fair_value(train, k, vol, nowcast), train.outcome_up.to_numpy())
                if best is None or ll < best.train_logloss:
                    best = Fit(round(float(k), 2), vol, nowcast, ll)
    return best


# ---------------------------------------------------------------------------------------------
# simple taker strategy: buy the side the model thinks is cheap, hold to resolution

def taker_signals(df: pd.DataFrame, p_model: np.ndarray, margin: float, latency: str = "300",
                  first_only: bool = True) -> pd.DataFrame:
    """One row per trade: buy Up (or Down) at the ask seen after `latency` ms when
    model value - ask(at decision) > fee + margin. 1 share; PnL = payout - price - fee."""
    d = df.assign(p=p_model)
    rows = []
    for side, ask_now, ask_exec, val in (("Up", "up_ask", f"up_ask_{latency}", d.p),
                                         ("Down", "dn_ask", f"dn_ask_{latency}", 1 - d.p)):
        a = d[ask_now]
        edge = val - a - fee_per_share(a, d.fee_rate)
        sel = d[(edge > margin) & a.notna() & d[ask_exec].notna() & (d[ask_exec] < 0.995)].copy()
        sel["side"] = side
        sel["price"] = sel[ask_exec]
        sel["edge_at_decision"] = edge[sel.index]
        sel["payout"] = sel.outcome_up if side == "Up" else 1 - sel.outcome_up
        rows.append(sel)
    t = pd.concat(rows)
    t["fee"] = fee_per_share(t.price, t.fee_rate)
    t["pnl"] = t.payout - t.price - t.fee
    if first_only:  # one trade per market and side: avoids counting the same mispricing many times
        t = t.sort_values("t_ms").groupby(["slug", "side"], as_index=False).first()
    return t


def summarize_trades(t: pd.DataFrame) -> dict:
    if t.empty:
        return {"trades": 0}
    lo, hi = block_bootstrap(t, lambda x: x.pnl.mean()) if len(t) > 20 else (math.nan, math.nan)
    return {"trades": len(t), "markets": t.slug.nunique(), "win_rate": t.payout.mean(),
            "avg_price": t.price.mean(), "avg_fee": t.fee.mean(), "pnl_per_share": t.pnl.mean(),
            "pnl_ci95": (lo, hi), "total_pnl_1_share_each": t.pnl.sum()}

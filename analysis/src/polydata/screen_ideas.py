"""Taker-style screens from the 2026-11 research round (journals in docs/strategies/). Rules only use columns
known at t (see screen.FORBIDDEN_RULE_COLUMNS). Parameters default to the variant chosen on the first half of
the train day; the research script sweeps them by calling the rule functions directly.

Token `Both` (complete set) is understood by `screen.fills`: one Up + one Down share, payoff 1, fee per leg.
"""

from __future__ import annotations

import numpy as np
import pandas as pd

from .research.calibration import fair_value
from .screen import Screen, register

MAX_SPREAD = 0.10  # the ask of a wide book is not a real price (calibration study)


def _first(s: pd.DataFrame) -> pd.DataFrame:
    s = s.sort_values(["slug", "t_ms"], kind="stable").drop_duplicates("slug")
    return s[["slug", "t_ms", "token"]].reset_index(drop=True)


def _fee_ps(price: np.ndarray, rate: np.ndarray) -> np.ndarray:
    return rate * price * (1.0 - price)  # fees.fee_per_share, vectorised (exponent 1)


# ------------------------------------------------------------------------------------ 1. complete set

def set_edge(snap: pd.DataFrame) -> np.ndarray:
    """1 - (up_ask + dn_ask) - fee per set, at the signal time (before latency)."""
    ua, da = snap.up_ask.to_numpy(float), snap.dn_ask.to_numpy(float)
    rate = snap.fee_rate.to_numpy(float)
    return 1 - ua - da - _fee_ps(ua, rate) - _fee_ps(da, rate)


def complete_set_arb(snap: pd.DataFrame, margin: float = 0.0, first_only: bool = True) -> pd.DataFrame:
    """Buy one Up and one Down share when 1 - up_ask - dn_ask - fees > margin. Depth is not checked."""
    ok = set_edge(snap) > margin
    s = snap[ok].assign(token="Both")
    return _first(s) if first_only else s[["slug", "t_ms", "token"]].reset_index(drop=True)


register(Screen("complete-set-arb", "complete-set-arb", complete_set_arb))


# ------------------------------------------------------------------------------------ fair value helpers

def _fair_edges(snap: pd.DataFrame, k: float):
    """(edge_up, edge_dn, spread_up, spread_dn): fair - ask - fee per share for each token (NaN -> -1)."""
    p = fair_value(snap, k=k)
    rate = snap.fee_rate.to_numpy(float)
    ua, da = snap.up_ask.to_numpy(float), snap.dn_ask.to_numpy(float)
    eu = np.nan_to_num(p - ua - _fee_ps(ua, rate), nan=-1.0)
    ed = np.nan_to_num((1 - p) - da - _fee_ps(da, rate), nan=-1.0)
    return eu, ed, (snap.up_ask - snap.up_bid).to_numpy(float), (snap.dn_ask - snap.dn_bid).to_numpy(float)


def _pick(snap, eu, ed, su, sd, ask_cap, margin, ok):
    """Best-edge side per row that passes `ok`, margin, spread and the ask cap."""
    ua, da = snap.up_ask.to_numpy(float), snap.dn_ask.to_numpy(float)
    gu = ok & (eu > margin) & (su <= MAX_SPREAD) & (ua <= ask_cap)
    gd = ok & (ed > margin) & (sd <= MAX_SPREAD) & (da <= ask_cap)
    up = gu & (~gd | (eu >= ed))
    keep = gu | gd
    return snap[keep].assign(token=np.where(up[keep], "Up", "Down"))


# ------------------------------------------------------------------------------------ 2. settlement lock

def settlement_lock(snap: pd.DataFrame, min_obs: int = 40, margin: float = 0.01, ask_cap: float = 0.97,
                    k: float = 1.2, dist_sigmas: float = 0.0) -> pd.DataFrame:
    """Last 60 s: with >= min_obs Chainlink ticks already observed in the settlement window, buy the token whose
    fair value (model with the observed average) beats its ask by margin after the fee, ask <= ask_cap.
    dist_sigmas > 0 also needs |obs_avg - ptb| / ptb > dist_sigmas * sig15 * sqrt(tau + 1).
    NB: snapshots carry the official price_to_beat; the simulator derives it from Chainlink (screen-only caveat)."""
    eu, ed, su, sd = _fair_edges(snap, k)
    ok = (snap.tau_s <= 60).to_numpy() & (snap.cl_obs_n >= min_obs).to_numpy()
    if dist_sigmas > 0:
        d = (snap.cl_obs_avg - snap.price_to_beat).abs() / snap.price_to_beat
        ok &= (d > dist_sigmas * snap.sig15 * np.sqrt(snap.tau_s + 1)).to_numpy()
        # only the side the observed average points to (else the cheap loser passes a negative margin)
        locked_up = (snap.cl_obs_avg > snap.price_to_beat).to_numpy()
        eu, ed = np.where(locked_up, eu, -1.0), np.where(~locked_up, ed, -1.0)
    return _first(_pick(snap, eu, ed, su, sd, ask_cap, margin, ok))


register(Screen("settlement-lock", "endgame", settlement_lock))


# ------------------------------------------------------------------------------------ 3. fair value deviation

def fair_value_deviation(snap: pd.DataFrame, margin: float = 0.02, min_tau: float = 300, max_tau: float = 1200,
                         k: float = 1.2, timeframes: tuple = ("15m", "1h")) -> pd.DataFrame:
    """Buy when fair_value - ask - fee per share > margin, time left in [min_tau, max_tau], 15m/1h only."""
    eu, ed, su, sd = _fair_edges(snap, k)
    ok = snap.timeframe.isin(timeframes).to_numpy() & (snap.tau_s >= min_tau).to_numpy() & (snap.tau_s <= max_tau).to_numpy()
    return _first(_pick(snap, eu, ed, su, sd, 1.0, margin, ok))


register(Screen("fair-value-deviation", "fair-value", fair_value_deviation))


# ------------------------------------------------------------------------------------ 4. binance momentum

def binance_momentum(snap: pd.DataFrame, n_s: int = 30, c: float = 2.5, margin: float = 0.02, k: float = 1.2,
                     step_s: int = 5) -> pd.DataFrame:
    """Non-BTC coins. Binance mid moved more than c * sig15 * sqrt(n_s) in the last n_s seconds (against the
    snapshot n_s ago) and the market still lags: fair - ask - fee > margin on the side of the move."""
    s = snap[snap.coin != "btc"].sort_values(["slug", "t_ms"], kind="stable").reset_index(drop=True)
    lag = n_s // step_s
    prev = s.groupby("slug").bn_mid.shift(lag)
    prev_t = s.groupby("slug").t_ms.shift(lag)
    r = np.log(s.bn_mid / prev)
    strong = ((r.abs() > c * s.sig15 * np.sqrt(n_s)) & ((s.t_ms - prev_t) == n_s * 1000)).to_numpy()
    eu, ed, su, sd = _fair_edges(s, k)
    up_move = (r > 0).to_numpy()
    eu = np.where(strong & up_move, eu, -1.0)
    ed = np.where(strong & ~up_move, ed, -1.0)
    return _first(_pick(s, eu, ed, su, sd, 1.0, margin, strong))


register(Screen("binance-momentum", "momentum", binance_momentum))

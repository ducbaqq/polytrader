"""Scalar fair value: `research.calibration.fair_value` for one decision time, over online state.

P(settlement average >= price_to_beat) under a driftless random walk. The settlement is the mean of
the Chainlink prices over the last 60 s of the window. With `tau_s` seconds left and per-second log
vol sigma = k * sig15 (Binance 1 s returns over 15 min):

- tau > 60: log(avg) ~ N(log S, sigma^2 (tau - 40))
- tau <= 60: avg = w * observed_avg + (1 - w) * future_avg, w = n_obs / (n_obs + tau),
  future_avg ~ N(S, (S sigma)^2 tau / 3)

S = the last Chainlink price, moved by the Binance return since that Chainlink tick (nowcast).
k = 1.2 was the study's best fit. No scipy: this runs on every call from a strategy.
"""

from __future__ import annotations

import math

K_BEST = 1.2
EPS = 1e-4


def _ratio(z_num: float, z_den: float) -> float:
    if z_den > 0:
        return z_num / z_den
    return math.copysign(math.inf, z_num) if z_num else 0.0


def fair_value(cl_px: float, bn_mid: float | None, bn_mid_at_cl: float | None, price_to_beat: float,
               sig15: float, tau_s: float, n_obs: float, obs_avg: float | None, k: float = K_BEST) -> float:
    S = float(cl_px)
    if bn_mid is not None and bn_mid_at_cl is not None and bn_mid_at_cl != 0:
        r = bn_mid / bn_mid_at_cl
        if math.isfinite(r):
            S *= r
    K, sig, tau = float(price_to_beat), k * float(sig15), float(tau_s)
    if tau > 60:
        z = _ratio(math.log(S / K), sig * math.sqrt(tau - 40))
    else:
        t = max(tau, 0.5)
        if n_obs > 0 and obs_avg is not None:
            w, obs = n_obs / (n_obs + t), float(obs_avg)
        else:
            w, obs = 0.0, S
        mean = w * obs + (1 - w) * S
        z = _ratio(mean - K, (1 - w) * S * sig * math.sqrt(t / 3))
    p = 0.5 * math.erfc(-z / math.sqrt(2))
    return min(max(p, EPS), 1 - EPS)

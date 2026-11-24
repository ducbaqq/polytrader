"""Sanity checks of the fair-value model used in the calibration study."""

import numpy as np
import pandas as pd

from polydata.research.calibration import fair_value


def snap(**kw):
    base = dict(cl_px=100.0, bn_mid=100.5, bn_mid_at_cl=100.5, price_to_beat=100.0, sig15=1e-4,
                tau_s=200.0, cl_obs_n=0, cl_obs_avg=np.nan)
    base.update(kw)
    return pd.DataFrame([base])


def test_at_the_money_is_half():
    assert abs(fair_value(snap())[0] - 0.5) < 1e-9


def test_monotone_in_price_and_shrinks_with_time():
    p_far = fair_value(snap(cl_px=100.05, tau_s=280))[0]
    p_near = fair_value(snap(cl_px=100.05, tau_s=70))[0]
    assert 0.5 < p_far < p_near < 1  # same lead is worth more with less time left


def test_settlement_window_uses_observed_average():
    # 55 s of the 60 s average already observed well above the strike; spot just dipped below it
    p = fair_value(snap(tau_s=5, cl_obs_n=55, cl_obs_avg=100.2, cl_px=99.95, bn_mid=1, bn_mid_at_cl=1))[0]
    assert p > 0.99


def test_nowcast_moves_price_with_binance():
    up = fair_value(snap(bn_mid=100.6, bn_mid_at_cl=100.5))[0]   # Binance up 10 bp since the Chainlink tick
    flat = fair_value(snap(bn_mid=100.6, bn_mid_at_cl=100.5), nowcast=False)[0]
    assert up > 0.5 and abs(flat - 0.5) < 1e-9

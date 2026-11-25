"""Decision logic of the research-round screens (synthetic frames)."""

from __future__ import annotations

import numpy as np
import pandas as pd
import pytest

from polydata import screen, screen_ideas as si
from polydata.fees import taker_fee

WS = int(pd.Timestamp("2026-11-27", tz="UTC").timestamp() * 1000)  # a train day


def row(slug="m1", coin="eth", tf="5m", tau=20.0, up_ask=0.60, up_bid=0.58, dn_ask=0.42, dn_bid=0.40, rate=0.05,
        ptb=100.0, cl_px=100.0, obs_avg=100.0, obs_n=40, sig15=1e-4, bn=100.0, bn_cl=100.0, outcome_up=1, t_ms=None,
        length_s=300):
    end = WS + length_s * 1000
    return dict(slug=slug, coin=coin, timeframe=tf, window_start_ms=WS, window_end_ms=end,
                t_ms=end - int(tau * 1000) if t_ms is None else t_ms, tau_s=tau, fee_rate=rate, price_to_beat=ptb,
                cl_px=cl_px, cl_obs_avg=obs_avg, cl_obs_n=obs_n, sig15=sig15, sig60=sig15, bn_mid=bn, bn_mid_at_cl=bn_cl,
                up_bid=up_bid, up_ask=up_ask, dn_bid=dn_bid, dn_ask=dn_ask, outcome_up=outcome_up,
                up_ask_300=up_ask, dn_ask_300=dn_ask, up_ask_1000=up_ask, dn_ask_1000=dn_ask)


def frame(*rows):
    return pd.DataFrame(list(rows))


def test_complete_set_arb_needs_edge_after_both_fees():
    # asks sum to 0.97, fees ~ 0.05*(.5*.5) * 2 = 0.025 -> edge 0.005 > 0
    arb = row(up_ask=0.50, up_bid=0.49, dn_ask=0.47, dn_bid=0.46)
    # asks sum to 0.99: fees eat it
    no = row(slug="m2", up_ask=0.50, up_bid=0.49, dn_ask=0.49, dn_bid=0.48)
    s = si.complete_set_arb(frame(arb, no))
    assert list(s.slug) == ["m1"] and list(s.token) == ["Both"]


def test_both_fill_pays_one_per_set_whatever_the_outcome():
    for outcome in (0, 1):
        snap = frame(row(up_ask=0.50, dn_ask=0.47, outcome_up=outcome, up_bid=0.49, dn_bid=0.46))
        sig = pd.DataFrame([dict(slug="m1", t_ms=int(snap.t_ms[0]), token="Both")])
        r = screen.evaluate(snap, sig, 300, "hold", 10.0)
        want = 10 * (1 - 0.97) - taker_fee(10, 0.50, 0.05) - taker_fee(10, 0.47, 0.05)
        assert r["overall"]["pnl_usd"] == pytest.approx(want)
        r = screen.evaluate(snap, sig, 300, "bid", 10.0)  # a set is always held
        assert r["overall"]["pnl_usd"] == pytest.approx(want)


def test_settlement_lock_buys_only_the_locked_side():
    # observed average well above ptb: Up is locked. Up ask 0.93 (cheap enough), the loser Down at 0.03
    locked = row(obs_avg=101.0, cl_px=101.0, bn=101.0, bn_cl=101.0, up_ask=0.93, up_bid=0.92, dn_ask=0.08, dn_bid=0.07)
    s = si.settlement_lock(frame(locked), margin=0.01, ask_cap=0.97)
    assert list(s.token) == ["Up"]
    s = si.settlement_lock(frame(locked), margin=-0.5, ask_cap=0.97, dist_sigmas=2.0)
    assert list(s.token) == ["Up"]  # never the cheap loser, even with a permissive margin
    # locked favourite already priced at 0.995: nothing to buy
    priced = row(obs_avg=101.0, cl_px=101.0, bn=101.0, bn_cl=101.0, up_ask=0.995, up_bid=0.99, dn_ask=0.01, dn_bid=0.005)
    assert si.settlement_lock(frame(priced), margin=-0.5, ask_cap=0.97, dist_sigmas=2.0).empty
    # too few observed ticks, or too early
    assert si.settlement_lock(frame(row(obs_avg=101.0, cl_px=101.0, up_ask=0.5, up_bid=0.49, obs_n=10))).empty
    assert si.settlement_lock(frame(row(tau=90.0, obs_avg=101.0, cl_px=101.0, up_ask=0.5, up_bid=0.49))).empty


def test_settlement_lock_skips_wide_books():
    wide = row(obs_avg=101.0, cl_px=101.0, bn=101.0, bn_cl=101.0, up_ask=0.93, up_bid=0.60)
    assert si.settlement_lock(frame(wide), margin=0.01).empty


def test_fair_value_deviation_timeframes_band_and_margin():
    kw = dict(tau=600.0, up_ask=0.40, up_bid=0.39, dn_ask=0.62, dn_bid=0.61, cl_px=100.5, bn=100.5, bn_cl=100.5,
              obs_n=0, obs_avg=np.nan, sig15=5e-5)
    ok = row(tf="15m", length_s=900, **kw)
    assert list(si.fair_value_deviation(frame(ok), margin=0.04).token) == ["Up"]
    assert si.fair_value_deviation(frame(row(slug="m2", tf="5m", **kw)), margin=0.04).empty  # 5m excluded
    assert si.fair_value_deviation(frame(ok), margin=0.04, min_tau=700, max_tau=1200).empty  # outside the band
    assert si.fair_value_deviation(frame(ok), margin=0.9).empty  # margin too high


def test_binance_momentum_skips_btc_and_needs_strong_recent_move():
    def two(coin, bn_now):
        old = row(coin=coin, tau=40.0, bn=100.0, cl_px=100.0, bn_cl=100.0, up_ask=0.50, up_bid=0.49, dn_ask=0.52, dn_bid=0.51,
                  obs_n=0, obs_avg=np.nan, sig15=1e-4)
        new = row(coin=coin, tau=30.0, bn=bn_now, bn_cl=100.0, cl_px=100.0, up_ask=0.50, up_bid=0.49, dn_ask=0.52, dn_bid=0.51,
                  obs_n=0, obs_avg=np.nan, sig15=1e-4)
        mid = dict(old, tau_s=35.0, t_ms=int(old["t_ms"]) + 5000)
        return frame(old, mid, new)

    s = si.binance_momentum(two("eth", 100.5), n_s=10, c=2.0, margin=0.02)  # +50 bp > 2 * 1e-4 * sqrt(10)
    assert list(s.token) == ["Up"] and int(s.t_ms[0]) == int(two("eth", 100.5).t_ms[2])
    assert si.binance_momentum(two("btc", 100.5), n_s=10, c=2.0, margin=0.02).empty
    assert si.binance_momentum(two("eth", 100.001), n_s=10, c=2.0, margin=0.02).empty  # small move
    s = si.binance_momentum(two("eth", 99.5), n_s=10, c=2.0, margin=0.02)
    assert list(s.token) == ["Down"]


def test_registered():
    for n in ("complete-set-arb", "settlement-lock", "fair-value-deviation", "binance-momentum"):
        assert n in screen.REGISTRY

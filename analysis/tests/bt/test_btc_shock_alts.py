"""`btc-shock-alts` decision logic through a fake ctx (synthetic Binance paths and books, no engine)."""

from __future__ import annotations

import math
import random

import pytest
from test_engine import T0, minfo

from polydata.bt.strategies.btc_shock_alts import SymState, beta
from polydata.bt.strategy import BookView, get_strategy
from polydata.bt.types import Bbo, ChainlinkTick

ETH = minfo(T0, "15m", coin="eth", price_to_beat=3000.0)
BTC = minfo(T0, "15m", coin="btc", price_to_beat=60000.0)


class Ctx:
    def __init__(self, markets=(ETH,), bids=((0.49, 50),), asks=((0.51, 50),)):
        self.ms, self.bids, self.asks = list(markets), list(bids), list(asks)
        self.now, self.takes, self.cl = T0, [], {}

    def markets(self):
        return self.ms

    def book(self, slug, depth=5):
        return BookView(self.bids, self.asks)

    def chainlink(self, coin):
        return self.cl.get(coin)

    def take(self, slug, token, side, size, limit_price):
        self.takes.append((slug, token, side, size, limit_price))
        return len(self.takes)


def feed(s, c, sym, t, mid):
    c.now = t
    s.on_binance(c, sym, Bbo(t, sym, mid - 0.005, mid + 0.005, 1.0, 1.0))


def warm(s, c, secs=1100, btc=60000.0, eth=3000.0, vol=1e-4):
    """secs seconds of a shared random walk (BTC and ETH together: beta 1, sigma ~1 bp/s) ending at the
    starting prices, with a Chainlink tick at the end."""
    rng = random.Random(0)
    steps = [rng.gauss(0, vol) for _ in range(secs)]
    x, path = 0.0, []
    for z in steps:
        x += z
        path.append(x)
    t0 = T0 - secs * 1000
    for i, x in enumerate(path):
        f = math.exp(x - path[-1])
        feed(s, c, "BTCUSDT", t0 + i * 1000, btc * f)
        feed(s, c, "ETHUSDT", t0 + i * 1000 + 1, eth * f)
    c.now = T0 - 200
    c.cl["eth"] = ChainlinkTick(c.now, c.now, "eth", eth)
    s.on_chainlink(c, "eth", eth)
    return T0


def mk(params=None, **kw):
    s = get_strategy("btc-shock-alts")(params)
    return s, Ctx(**kw)


def test_registered_and_defaults():
    cls = get_strategy("btc-shock-alts")
    assert cls.family == "latency-arb"
    assert cls.default_params["k"] == 4.0 and cls.default_params["margin"] == 0.03
    assert "btc" not in cls.default_params["trade_coins"]


def test_symstate_grid_and_sig15():
    st = SymState()
    for i in range(1000):
        st.update(T0 + i * 1000, 100.0 * (1.001 if i % 2 else 1.0))
    assert st.sig15(T0 + 1000 * 1000) == pytest.approx(math.log(1.001), rel=1e-6)
    assert st.mid_at(T0 + 999_000) == pytest.approx(100.1)
    st2 = SymState()
    st2.update(T0, 1.0)
    st2.update(T0 + 3500, 2.0)  # gap: seconds forward-filled
    assert st2.secs == [1.0, 1.0, 1.0, 2.0]
    assert st2.sig15(T0 + 4000) is None


def test_beta_slope():
    b = [100 * math.exp(0.001 * (i % 3)) for i in range(200)]
    a = [50 * math.exp(0.002 * (i % 3)) for i in range(200)]
    assert beta(a, b, 100) == pytest.approx(2.0, rel=1e-6)
    assert beta(a[:50], b[:50], 100) is None


def test_btc_shock_takes_favoured_side_in_alt():
    s, c = mk()
    t = warm(s, c)
    feed(s, c, "BTCUSDT", t + 10, 60000.0 * 1.004)  # +40 bp in 1 s: far above 4 sigma (sigma ~1 bp)
    assert s.shocks == 1
    assert len(c.takes) == 1
    slug, token, side, size, lim = c.takes[0]
    assert (slug, token, side, size) == (ETH.slug, "Up", "BUY", 10.0)
    assert 0.51 <= lim < 1


def test_down_shock_buys_down_and_decluster():
    s, c = mk()
    t = warm(s, c)
    feed(s, c, "BTCUSDT", t + 10, 60000.0 * 0.996)
    assert [x[1] for x in c.takes] == ["Down"]
    feed(s, c, "BTCUSDT", t + 2000, 60000.0 * 0.992)  # inside the 30 s decluster window
    assert s.shocks == 1 and len(c.takes) == 1


def test_small_move_no_shock():
    s, c = mk()
    t = warm(s, c)
    feed(s, c, "BTCUSDT", t + 10, 60000.0 * 1.0002)
    assert s.shocks == 0 and c.takes == []


def test_filters_spread_coin_and_signal_own():
    s, c = mk(bids=((0.40, 50),), asks=((0.51, 50),))  # spread 11c > 3c
    t = warm(s, c)
    feed(s, c, "BTCUSDT", t + 10, 60000.0 * 1.004)
    assert s.shocks == 1 and c.takes == []
    s, c = mk(markets=(BTC,))  # btc not in trade_coins
    t = warm(s, c)
    feed(s, c, "BTCUSDT", t + 10, 60000.0 * 1.004)
    assert c.takes == []
    s, c = mk({"signal": "own"})  # ETH has not moved yet: own fair ~0.5, no edge over 0.51
    t = warm(s, c)
    feed(s, c, "BTCUSDT", t + 10, 60000.0 * 1.004)
    assert s.shocks == 1 and c.takes == []

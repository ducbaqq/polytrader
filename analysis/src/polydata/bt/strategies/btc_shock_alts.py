"""`btc-shock-alts` (family `latency-arb`): after a sharp BTC move, take the favoured side of alt Up/Down books
before they reprice (docs/strategies/btc-shock-alts.md).

State is built from `ctx` only (`on_binance`, `on_chainlink`):

- per Binance symbol: the recent mids (back to the last one at or before 1 s ago) and a 1 s grid (last mid of each second, forward-filled);
  sig15 = sqrt(mean r^2) over the 900 1 s returns ending at the last complete second (as the simulator's);
- shock: on a BTCUSDT update, r = ln(mid_now / mid_1s_ago) and |r| > `k` * sig15(BTC); then no new shock for
  `decluster_ms`;
- beta of each alt on BTC: OLS slope of 10 s returns over the trailing hour (at least `min_beta_n` returns),
  refreshed every 60 s;
- fair (signal `beta`): `fairvalue.fair_value` with S = the last Chainlink price moved by the alt's Binance return
  since that tick, and the alt mid moved further by beta * BTC return over the last second minus the alt's own
  return over it (the alt is assumed to end up with beta times BTC's move). Signal `own` skips that step.

On a shock, for every open market of `trade_coins` x `timeframes` with more than `min_tau_s` left, a price to beat,
Up spread <= `max_spread` and Up mid in [`min_mid`, 1 - `min_mid`]: buy the side the shock favours with an IOC at
the highest tick L with L + fee(L) <= fair_side - `margin`, sized to the visible ask at <= L (capped at `size`).
One order per market per shock. Held to resolution.
"""

from __future__ import annotations

import math
from collections import deque

from ..fairvalue import fair_value
from ..strategy import Ctx, Strategy, register
from ..types import Bbo, MarketInfo
from .fv_taker import limit_price

SIG_S, BETA_S, BETA_STEP_S, KEEP_S = 900, 3600, 10, 3700
DEPTH = 10


class SymState:
    """Recent mids and a 1 s grid of one Binance symbol."""

    def __init__(self):
        self.recent: deque[tuple[int, float]] = deque()
        self.sec0: int | None = None
        self.secs: list[float] = []  # secs[i] = last mid received in second sec0 + i (forward-filled)
        self._sig: tuple[int, float | None] = (-1, None)

    def update(self, t: int, mid: float) -> None:
        self.recent.append((t, mid))
        while len(self.recent) > 1 and self.recent[1][0] <= t - 1000:  # recent[0]: last at or before t - 1 s
            self.recent.popleft()
        s = t // 1000
        if self.sec0 is None:
            self.sec0, self.secs = s, [mid]
            return
        last = self.sec0 + len(self.secs) - 1
        if s > last:
            self.secs.extend([self.secs[-1]] * (s - last - 1))
            self.secs.append(mid)
        else:
            self.secs[-1] = mid
        if len(self.secs) > KEEP_S + 600:
            cut = len(self.secs) - KEEP_S
            del self.secs[:cut]
            self.sec0 += cut

    @property
    def mid(self) -> float | None:
        return self.recent[-1][1] if self.recent else None

    def mid_at(self, t: int) -> float | None:
        """Last mid received at or before t (t >= the last update - 1 s)."""
        out = None
        for ts, m in self.recent:
            if ts > t:
                break
            out = m
        return out

    def _complete(self, now: int) -> list[float]:
        """Grid values of the complete seconds before now."""
        if self.sec0 is None:
            return []
        n = now // 1000 - self.sec0  # seconds sec0 .. now//1000 - 1 are complete
        return self.secs[:max(0, min(n, len(self.secs)))]

    def sig15(self, now: int) -> float | None:
        if self._sig[0] == now // 1000:
            return self._sig[1]
        g = self._complete(now)
        out = None
        if len(g) >= SIG_S + 1:
            w = g[-SIG_S - 1:]
            out = math.sqrt(sum(math.log(b / a) ** 2 for a, b in zip(w, w[1:])) / SIG_S)
        self._sig = (now // 1000, out)
        return out


def beta(alt: list[float], btc: list[float], min_n: int) -> float | None:
    """OLS slope of alt on BTC log returns (aligned grids, same step)."""
    n = min(len(alt), len(btc))
    a, b = alt[len(alt) - n:], btc[len(btc) - n:]
    ra = [math.log(y / x) for x, y in zip(a, a[1:])]
    rb = [math.log(y / x) for x, y in zip(b, b[1:])]
    if len(rb) < min_n:
        return None
    ma, mb = sum(ra) / len(ra), sum(rb) / len(rb)
    vb = sum((x - mb) ** 2 for x in rb)
    if vb <= 0:
        return None
    return sum((x - mb) * (y - ma) for x, y in zip(rb, ra)) / vb


@register
class BtcShockAlts(Strategy):
    name = "btc-shock-alts"
    family = "latency-arb"
    default_params = {
        "k": 4.0,
        "margin": 0.03,
        "signal": "beta",  # beta | own
        "trade_coins": ["eth", "sol", "xrp", "doge", "bnb", "hype", "zec"],
        "timeframes": ["5m", "15m"],
        "decluster_ms": 30000,
        "min_tau_s": 60,
        "max_spread": 0.03,
        "min_mid": 0.05,
        "size": 10.0,
        "min_beta_n": 100,
        "kfv": 1.2,
    }

    def __init__(self, params: dict | None = None):
        super().__init__(params)
        self.sym: dict[str, SymState] = {}
        self.cl_mid: dict[str, tuple[int, float]] = {}  # coin -> (chainlink recv_ms, Binance mid then)
        self.betas: dict[str, tuple[int, float | None]] = {}
        self.next_shock = -1
        self.shocks = 0

    # ------------------------------------------------------------------------------------------
    def on_binance(self, ctx: Ctx, sym: str, bbo: Bbo) -> None:
        if not (bbo.bid > 0 and bbo.ask > 0):
            return
        st = self.sym.setdefault(sym, SymState())
        st.update(ctx.now, (bbo.bid + bbo.ask) / 2)
        if sym == "BTCUSDT":
            self._check(ctx, st)

    def on_chainlink(self, ctx: Ctx, coin: str, price: float) -> None:
        st = self.sym.get(coin.upper() + "USDT")
        if st is not None and st.mid is not None:
            self.cl_mid[coin] = (ctx.now, st.mid)

    # ------------------------------------------------------------------------------------------
    def _check(self, ctx: Ctx, btc: SymState) -> None:
        now = ctx.now
        if now < self.next_shock:
            return
        pre = btc.mid_at(now - 1000)
        sig = btc.sig15(now)
        if pre is None or not sig:
            return
        r = math.log(btc.mid / pre)
        if abs(r) <= self.params["k"] * sig:
            return
        self.next_shock = now + self.params["decluster_ms"]
        self.shocks += 1
        self.on_shock(ctx, 1 if r > 0 else -1, r, pre)

    def _beta(self, now: int, coin: str) -> float | None:
        if coin == "btc":
            return 1.0
        t, b = self.betas.get(coin, (-10**12, None))
        if now - t < 60_000:
            return b
        alt, btc = self.sym.get(coin.upper() + "USDT"), self.sym.get("BTCUSDT")
        b = None
        if alt is not None and btc is not None:
            ga, gb = alt._complete(now)[-BETA_S:], btc._complete(now)[-BETA_S:]
            n = min(len(ga), len(gb))
            b = beta(ga[len(ga) - n::BETA_STEP_S], gb[len(gb) - n::BETA_STEP_S], self.params["min_beta_n"])
        self.betas[coin] = (now, b)
        return b

    def fair(self, ctx: Ctx, m: MarketInfo, r_btc: float) -> float | None:
        """Up fair value at ctx.now for market m (signal `beta` or `own`)."""
        st = self.sym.get(m.coin.upper() + "USDT")
        cl = ctx.chainlink(m.coin)
        sig = st.sig15(ctx.now) if st is not None else None
        if st is None or st.mid is None or cl is None or not sig or m.price_to_beat is None:
            return None
        mid = st.mid
        at_cl = self.cl_mid.get(m.coin)
        bn_at_cl = at_cl[1] if at_cl is not None and at_cl[0] >= cl.recv_ms else None
        if bn_at_cl is None:
            return None
        if self.params["signal"] == "beta":
            b, pre = self._beta(ctx.now, m.coin), st.mid_at(ctx.now - 1000)
            if b is None or pre is None:
                return None
            mid *= math.exp(b * r_btc - math.log(mid / pre))
        tau = (m.window_end_ms - ctx.now) / 1000.0
        return fair_value(cl.price, mid, bn_at_cl, m.price_to_beat, sig, tau, 0, None, k=self.params["kfv"])

    def on_shock(self, ctx: Ctx, d: int, r_btc: float, btc_pre: float) -> None:
        p = self.params
        for m in ctx.markets():
            if m.coin not in p["trade_coins"] or m.timeframe not in p["timeframes"]:
                continue
            if ctx.now < m.window_start_ms or (m.window_end_ms - ctx.now) / 1000.0 <= p["min_tau_s"]:
                continue
            book = ctx.book(m.slug, DEPTH)
            if book is None or book.spread is None or book.spread > p["max_spread"] + 1e-9:
                continue
            if not p["min_mid"] <= book.mid <= 1 - p["min_mid"]:
                continue
            f = self.fair(ctx, m, r_btc)
            if f is None:
                continue
            token = "Up" if d > 0 else "Down"
            fs = f if d > 0 else 1.0 - f
            v = book.for_token(token)
            lim = limit_price(fs, p["margin"], m.tick_px / 1000.0, m.fee_rate, m.fee_exponent, True)
            if lim is None or not v.asks or v.asks[0][0] > lim + 1e-9:
                continue
            n = min(p["size"], sum(s for px, s in v.asks if px <= lim + 1e-9))
            if n < m.min_order_size:
                continue
            ctx.take(m.slug, token, "BUY", n, lim)


__all__ = ["BtcShockAlts", "SymState", "beta"]

"""Real-data smoke runs of the replay engine (POLYDATA_REALDATA=1 and POLYDATA_DIR set)."""

from __future__ import annotations

import os
import time

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("POLYDATA_REALDATA") != "1", reason="needs POLYDATA_REALDATA=1")

START_MS = 1795518000000  # 2026-11-24 11:00 UTC
HOUR = 3_600_000


def _cfg(**kw):
    from polydata.bt.config import RunConfig

    return RunConfig(**{"strategy": "smoke", "coins": ["btc"], "timeframes": ["5m"], "days": ["2026-11-24"], **kw})


def test_noop_btc_5m_one_hour():
    from polydata.bt import engine
    from polydata.bt.strategy import Strategy

    class Noop(Strategy):
        name = "noop-smoke"

    t = time.perf_counter()
    res = engine.run_coin(_cfg(), "btc", strategy=Noop(), clip=(START_MS, START_MS + HOUR))
    el = time.perf_counter() - t
    print(f"\nnoop BTC 5m 1h: {el:.1f}s, {res.stats}")
    m = res.markets
    assert len(m) == 12 and res.orders.empty and m.pnl.fillna(0).sum() == 0.0
    assert el < 120


def test_join_best_btc_5m_one_hour_no_lookahead():
    """Quote at the best bid on every top change; check what the strategy saw against ctx.now."""
    from polydata.bt import engine
    from polydata.bt.strategy import Strategy

    class Join(Strategy):
        name = "join-smoke"

        def __init__(self):
            super().__init__({})
            self.bad: list = []
            self.reports: list = []

        def on_book(self, ctx, market, book):
            top = ctx.top(market.slug)
            if top.recv_ms > ctx.now:
                self.bad.append(("top", ctx.now, top.recv_ms))
            if (market.window_end_ms - ctx.now) < 30_000 or top.bid_px <= 0:
                return
            mine = ctx.orders(market.slug)
            if mine and abs(mine[0].price - top.bid_px / 1000) < 1e-9:
                return
            ctx.cancel_all(market.slug)
            ctx.place(market.slug, "Up", "BUY", top.bid_px / 1000, 5)

        def on_order(self, ctx, order):
            if order.last_fill is not None:
                self.reports.append((ctx.now, order.last_fill.ts_ms))

    s = Join()
    res = engine.run_coin(_cfg(capital=50_000.0), "btc", strategy=s, clip=(START_MS, START_MS + HOUR))
    print(f"\njoin-best BTC 5m 1h: {res.stats['elapsed_s']:.1f}s, {len(res.orders)} order events, "
          f"{len(res.fills)} fills, pnl {res.markets.pnl.sum():.2f}")
    assert not s.bad
    assert s.reports and all(now == ts + 126 for now, ts in s.reports)
    f = res.fills
    assert len(f) and f.settle_value.isin([0.0, 1.0]).all()
    # a one-sided book has no mid (NaN by design); anything else must have one
    nan = f[f.mid_at_fill.isna()]
    if len(nan):
        from polydata.bt.marketdata import load_coin
        from polydata.bt.types import NO_ASK_PX, NO_BID_PX
        from polydata.views import connect

        con = connect()
        try:
            mds = load_coin(con, "btc", ["5m"], START_MS - HOUR, START_MS + 2 * HOUR, slugs=set(nan.slug))
        finally:
            con.close()
        for r in nan.itertuples():
            top = mds[r.slug].top_at(int(r.ts_ms), "ts")
            assert top.bid_px == NO_BID_PX or top.ask_px == NO_ASK_PX, (r.slug, r.ts_ms, top)
    print(f"  fills without a mid (one-sided book): {len(nan)} of {len(f)}")

"""`mm-side-pull` on synthetic inputs (the fake ctx and helpers of test_mm_binance_pull)."""

from __future__ import annotations

from test_engine import T0, minfo
from test_mm_binance_pull import HIST_S, FakeCtx, bn, fire, live, report, top

from polydata.bt.strategy import get_strategy


def make(params=None):
    """15 min of history (1 bp wiggle every second, so sigma_1s = 1 bp), one market opened 1 s in."""
    m = minfo(T0)
    s = get_strategy("mm-side-pull")(params)
    ctx = FakeCtx([m])
    t0 = m.window_start_ms + 2000 - HIST_S * 1000
    for k in range(HIST_S):
        bn(s, ctx, t0 + k * 1000, 60000.0 * (1.0001 if k % 2 else 1.0))
    ctx.tops[m.slug] = top(480, 520)
    s.on_market_open(ctx, m)
    return s, ctx, m


def test_registered_with_parent_defaults():
    cls = get_strategy("mm-side-pull")
    assert cls.family == "market-making"
    assert cls.default_params["side_k"] == 0.5 and cls.default_params["pull_k"] == 2.0


def test_quiet_history_quotes_both_sides():
    s, ctx, m = make()
    assert ctx.cancels == []
    assert live(ctx) == [("Down", "BUY", 0.49, 10.0), ("Up", "BUY", 0.49, 10.0)]


def test_small_drop_pulls_only_the_bid_until_the_pause_ends():
    s, ctx, m = make()
    t = ctx.now  # last history mid 60006 (odd second); 2 s ago also 60006, 1 s ago 60000
    bid = next(v for v in ctx.placed if v.token == "Up")
    ask = next(v for v in ctx.placed if v.token == "Down")
    # -1.5 bp vs 2 s ago (threshold 0.5 x 1 bp x sqrt 2 = 0.71 bp); -0.5 bp vs 1 s ago: no symmetric pull
    bn(s, ctx, t + 500, 60006.0 * (1 - 0.00015))
    assert ctx.cancels == [bid.id]
    assert ctx.timers["resume:btc"] == t + 500 + 3000
    report(s, ctx, bid.id, "cancelled")
    assert live(ctx) == [("Down", "BUY", 0.49, 10.0)]  # the ask stays; no new bid while paused
    assert ask.id not in ctx.cancels
    fire(s, ctx, "resume:btc")
    assert live(ctx) == [("Down", "BUY", 0.49, 10.0), ("Up", "BUY", 0.49, 10.0)]


def test_small_rise_pulls_only_the_ask():
    s, ctx, m = make()
    t = ctx.now
    ask = next(v for v in ctx.placed if v.token == "Down")
    # +1.5 bp vs 2 s ago, but +2.5 bp vs 1 s ago: the symmetric pull (> 2 bp) fires too, both sides go
    bn(s, ctx, t + 500, 60006.0 * (1 + 0.00015))
    assert ask.id in ctx.cancels
    s, ctx, m = make({"pull_k": 10.0})  # symmetric pull out of the way
    t = ctx.now
    ask = next(v for v in ctx.placed if v.token == "Down")
    bn(s, ctx, t + 500, 60006.0 * (1 + 0.00015))
    assert ctx.cancels == [ask.id]


def test_side_k_large_disables_side_pull():
    s, ctx, m = make({"side_k": 100.0})
    bn(s, ctx, ctx.now + 500, 60006.0 * (1 - 0.00015))
    assert ctx.cancels == []

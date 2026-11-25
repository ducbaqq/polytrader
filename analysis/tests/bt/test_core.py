"""The Core / ReplayDriver split (engine refactor for the paper runner)."""

from __future__ import annotations

import pytest


def test_engine_public_api_unchanged():
    from polydata.bt import core, engine
    from polydata.bt.engine import (  # noqa: F401
        EQUITY_COLS, FILL_COLS, ORDER_COLS, CoinResult, RunResult, SimCtx, default_con_factory, run, run_coin, sym_of,
    )

    assert isinstance(engine.ReplayDriver, type) and isinstance(core.Core, type)
    assert engine.CoinResult is core.CoinResult and engine.RunResult is core.RunResult and engine.SimCtx is core.SimCtx
    assert sym_of("btc") == "BTCUSDT"


def test_exchange_reads_trades_lazily():
    from test_exchange import T0, fills, market, order

    from polydata.bt.exchange import Exchange, ExchangeConfig

    # the level our bid joins (empty at arrival) grows to 15 and is consumed by a trade 5 ms later
    changes = [(T0 + 1500, "BUY", 0.48, 15), (T0 + 1995, "BUY", 0.48, 0)]
    md = market("", "0.52:100", changes=changes)
    ex = Exchange(md, ExchangeConfig())
    o = order(px=480, size=10, arrival=T0 + 1000)
    ex.submit(o)
    assert [e.kind for e in ex.advance(T0 + 1000)] == ["accepted"]
    md.trades = market("", "0.52:100", changes=changes, trades=[(T0 + 2000, "Up", "SELL", 0.48, 15)]).trades
    assert ex.next_event_ms() == T0 + 2000
    f = fills(ex.advance(T0 + 5000))
    assert [(x.order_id, x.size, x.ts_ms) for x in f] == [(o.id, 10, T0 + 2000)]


def _run_traced(trace: bool, strategy=None):
    from test_engine import M1, NO_EVENTS, T0, Recorder, cfg, loader_for, make_con, market_one

    from polydata.bt import engine

    s = strategy if strategy is not None else Recorder()
    return engine.run_coin(cfg(), "btc", lambda: make_con([M1], T0 - 1_200_000, T0 + 500_000),
                           loader=loader_for({M1.slug: market_one()}), strategy=s, rec_events=NO_EVENTS, trace=trace)


def test_trace_off_by_default_and_identical():
    from polydata.bt.trace import DECISION_COLS

    off, on = _run_traced(False), _run_traced(True)
    for name in ("orders", "fills", "equity", "markets"):
        a, b = getattr(off, name), getattr(on, name)
        assert a.equals(b), name
        assert list(a.dtypes) == list(b.dtypes), name
    assert off.log == on.log
    assert off.decisions.empty and list(off.decisions.columns) == DECISION_COLS
    assert len(on.decisions) > 0 and list(on.decisions.columns) == DECISION_COLS


def test_trace_rows():
    import math

    d = _run_traced(True).decisions
    assert d.iloc[0]["call"] == "on_start"
    assert d["n"].tolist() == list(range(len(d)))
    assert (d["now"].diff().dropna() >= 0).all()
    places = d[d["call"] == "place"]
    assert len(places) == 1
    p = places.iloc[0]
    assert isinstance(p["price"], float) and not math.isnan(p["price"]) and p["price"] == 0.48 and p["size"] == 10.0
    assert p["token"] == "Up" and p["side"] == "BUY"
    takes = d[d["call"] == "take"]
    assert len(takes) == 1 and takes.iloc[0]["price"] == 0.99
    # order ids are 1, 2 in placement order, and the on_order rows name them
    assert p["ret"] == repr(1) and takes.iloc[0]["ret"] == repr(2)
    assert set(d.loc[d["call"] == "on_order", "key"]) <= {"1", "2"}
    # book is never loaded by the trace; other calls record the answer
    assert set(d.loc[d["call"] == "book", "ret"]) <= {"BookView", "None"}
    calls = set(d["call"])
    assert {"on_start", "on_market_open", "on_book", "on_order", "on_timer", "on_market_close", "markets", "top",
            "fair_value", "binance", "chainlink", "set_timer"} <= calls


def test_trace_keeps_stream_skipping():
    # Strategy.overrides is read on the wrapped class: a strategy without on_binance gets no Binance rows
    from polydata.bt.strategy import Strategy

    class Quiet(Strategy):
        name = "quiet"

        def on_book(self, ctx, market, book):
            pass

    d = _run_traced(True, Quiet()).decisions
    assert "on_book" in set(d["call"]) and not {"on_binance", "on_chainlink", "on_trade"} & set(d["call"])


def test_run_result_decisions_concat():
    from polydata.bt.engine import RunResult

    r = _run_traced(True)
    rr = RunResult(None, {"btc": r})
    assert len(rr.decisions) == len(r.decisions) and set(rr.decisions["budget"]) == {"btc"}


def test_trace_records_inputs_ms_and_post_only():
    d = _run_traced(True).decisions
    p = d[d["call"] == "place"].iloc[0]
    assert p["flag"] is True or p["flag"] == True  # noqa: E712 (Recorder places post_only=True by default)
    t = d[d["call"] == "take"].iloc[0]
    assert t["flag"] is None or t["flag"] != t["flag"]
    timers = d[d["call"] == "set_timer"]
    assert timers.iloc[0]["size"] == 1_000.0 and set(timers["size"]) == {1_000.0, 10_000.0}
    cl = d[d["call"] == "on_chainlink"]
    assert len(cl) and all(isinstance(float(r), float) for r in cl["ret"])  # repr(price) round-trips
    assert d[d["call"] == "on_binance"]["ret"].str.startswith("Bbo(").all()
    assert d[d["call"] == "on_trade"]["ret"].str.startswith("Trade(").all()
    od = d[d["call"] == "on_order"]
    assert od["ret"].str.startswith("('accepted'").any() and od["ret"].str.contains("'fill'").any()
    assert d.loc[d["call"].isin(["on_book", "on_market_open", "on_market_close"]), "ret"].isna().all()


def test_traced_ctx_records_raise():
    import pytest

    from polydata.bt.strategy import Strategy

    class Bad(Strategy):
        name = "bad"

        def on_market_open(self, ctx, market):
            ctx.place(market.slug, "Sideways", "BUY", 0.5, 5)

    rows = []

    class Spy(Bad):
        def on_market_open(self, ctx, market):
            rows.append(ctx._rows)
            super().on_market_open(ctx, market)

    with pytest.raises(ValueError):
        _run_traced(True, Spy())
    last = rows[0][-1]
    assert last[2] == "place" and last[8] == "raise:ValueError" and last[4] == "Sideways" and last[6] == 0.5


def test_traced_strategy_forwards_attributes():
    from polydata.bt.config import RunConfig
    from polydata.bt.core import Core
    from test_engine import Recorder

    s = Recorder({"x": 1})
    c = Core(RunConfig(strategy="recorder", coins=["btc"], timeframes=["5m"], days=["2026-11-24"]), ["btc"],
             {"btc": 100.0}, s, trace=True)
    assert c.strategy is not s and c.strategy.name == "recorder" and c.strategy.params == s.params
    assert c.strategy.events is s.events


def test_traced_strategy_getattr_no_recursion():
    import copy

    import pytest

    from polydata.bt.trace import TracedStrategy

    bare = TracedStrategy.__new__(TracedStrategy)
    with pytest.raises(AttributeError):
        bare.foo  # noqa: B018
    with pytest.raises(AttributeError):
        bare._s  # noqa: B018
    inner = object()
    c = copy.copy(TracedStrategy(inner, []))
    assert c._s is inner
    with pytest.raises(AttributeError):
        c.missing  # noqa: B018


def test_traced_ctx_formatter_error_is_not_a_raise():
    import pytest

    from polydata.bt.trace import TracedCtx

    rows = []
    ctx = TracedCtx.__new__(TracedCtx)
    ctx._rows, ctx.now = rows, 5

    def bad_fmt(v):
        raise RuntimeError("fmt")

    with pytest.raises(RuntimeError):
        ctx._call("top", "s", lambda: 1, bad_fmt)
    assert rows[-1][2] == "top" and rows[-1][8] is None
    with pytest.raises(KeyError):
        ctx._call("top", "s", lambda: {}["x"])
    assert rows[-1][8] == "raise:KeyError"
    assert ctx._call("top", "s", lambda: 2.5) == 2.5 and rows[-1][8] == "2.5"


# ------------------------------------------------------------------------------------------ entry points

def _core(strategy=None, **kw):
    from test_engine import Recorder, cfg

    from polydata.bt.core import Core

    return Core(cfg(), ["btc"], {"btc": 1000.0}, strategy if strategy is not None else Recorder(), **kw)


def test_kinds_keep_their_relative_order():
    from polydata.bt.core import _RANK, K_ADMIT, K_BN, K_CL, K_OPEN, K_TOP, K_TRADE

    assert (K_ADMIT, K_OPEN, K_TOP, K_TRADE, K_CL, K_BN) == (0, 1, 2, 3, 4, 5)
    assert _RANK.tolist() == [0, 0, 0, 0, 1, 2]


def test_stage_then_admit():
    import math

    from test_engine import M1, market_one

    from polydata.bt.core import P_CLOSE

    c = _core()
    mi = c.stage(M1, market_one(), 1, 2)
    m = c.mkts[mi]
    assert mi == 0 and c.by_slug[M1.slug] is m and m.load_ms == 2 and m.rs == 1 and m.resolved
    assert not c.loaded and not c.heap and M1.slug not in c.market_rows and M1.slug not in c.pf.positions
    c.admit(mi)
    assert c.loaded == [m] and M1.slug in c.pf.positions
    assert [(h[0], h[1], h[3], h[4][0]) for h in c.heap] == [(M1.window_end_ms, P_CLOSE, 0, "close")]
    row = c.market_rows[M1.slug]
    assert row["voided"] is False and row["reason"] == "" and row["pnl"] == 0.0 and math.isnan(row["settle_ms"])
    assert row["outcome"] == "Up" and row["window_end_ms"] == M1.window_end_ms


def test_admit_keeps_driver_row_and_takes_given_row():
    from test_engine import M1, market_one, mdata, minfo

    c = _core()
    pre = {"slug": M1.slug, "marker": 1}
    c.market_rows[M1.slug] = pre
    c.admit(c.stage(M1, market_one(), 0, 0))
    assert c.market_rows[M1.slug] is pre
    m2 = minfo(M1.window_end_ms)
    given = {"slug": m2.slug, "marker": 2}
    c.admit(c.stage(m2, mdata(m2), 0, 0), given)
    assert c.market_rows[m2.slug] is given


def test_dispatch_admit_calls_no_strategy():
    from test_engine import M1, market_one

    from polydata.bt.core import K_ADMIT

    c = _core()
    s = c.strategy
    mi = c.stage(M1, market_one(), 0, 0)
    c.dispatch([M1.window_start_ms - 100_000], [K_ADMIT], [mi], [-1], [], [])
    assert c.loaded == [c.mkts[mi]] and c.ctx.now == M1.window_start_ms - 100_000 and s.events == []


# ------------------------------------------------------------------------------------------ ordering rules

HOUR = 3_600_000


def _probe_cls():
    from polydata.bt.strategy import Strategy

    class TimerProbe(Strategy):
        """Two timers at the same ms: one set before the hour of `at` was marked, one after (at the open)."""
        name = "timer-probe"
        family = "test"

        def __init__(self, at, params=None):
            super().__init__(params)
            self.at, self.obs = at, []

        def on_start(self, ctx):
            ctx.set_timer("before", self.at - ctx.now)

        def on_market_open(self, ctx, market):
            ctx.set_timer("after", self.at - ctx.now)

        def on_timer(self, ctx, name):
            ms = ctx.markets()
            self.obs.append((ctx.now, name, [m.price_to_beat for m in ms],
                             [ctx.fair_value(m.slug) is not None for m in ms]))

    return TimerProbe


def test_ptb_key_matches_replay_push():
    from test_engine import M1, NO_EVENTS, T0, cfg, loader_for, make_con, market_one

    from polydata.bt import engine
    from polydata.bt.core import K_OPEN, Core

    at = T0 + 2_000  # the reveal ms: the tick with ts T0 + 0.5 s is received at T0 + 2 s (make_con)
    Probe = _probe_cls()

    def con():
        return make_con([M1], T0 - 1_200_000, T0 + 500_000)

    rep = Probe(at)
    engine.run_coin(cfg(), "btc", con, loader=loader_for({M1.slug: market_one()}), strategy=rep, rec_events=NO_EVENTS)
    assert [o[:2] for o in rep.obs] == [(at, "before"), (at, "after")]
    assert rep.obs[0][2:] == ([None], [False]) and rep.obs[1][2][0] is not None and rep.obs[1][3] == [True]

    # By hand, as the live driver would: the market is admitted after the hour is marked, and the revealing tick
    # is released (scan_ptb) only after the "after" timer was set. The key still puts the reveal between them.
    live = Probe(at)
    c = Core(cfg(), ["btc"], {"btc": 1000.0}, live)
    d = engine.ReplayDriver(c, None, NO_EVENTS)
    h = T0 // HOUR * HOUR
    rs = h - HOUR
    k = con()
    c.reset_range()
    c.start(rs)
    c.mark_hour(rs)
    d._load_bn(k, rs, h)
    d._load_cl(k, rs, h)
    c._drain(h)
    d._load_bn(k, h, h + HOUR)
    c.mark_hour(h)
    md = market_one()
    mi = c.stage(M1, md, rs, h)
    c.admit(mi)
    c.dispatch([int(md.snaps_recv[0])], [K_OPEN], [mi], [-1], [], [])
    assert not [e for e in c.heap if e[4][0] == "ptb"]
    d._load_cl(k, h, h + HOUR)
    ptb = [e for e in c.heap if e[4][0] == "ptb"]
    assert len(ptb) == 1 and ptb[0][:4] == (at, 2, c.hour_seq[h] + 0.5, mi)
    c._drain(T0 + 500_000)  # (finish would stop timers)
    assert live.obs == rep.obs


def test_ptb_deferred_until_hour_marked():
    from test_engine import M1, T0, market_one, mdata, minfo

    from polydata.bt.core import P_TIMER

    c = _core()
    h = T0 // HOUR * HOUR
    c.mark_hour(h)
    m = c.mkts[c.stage(M1, market_one(), h, h)]
    c.schedule_ptb(m, h + HOUR + 5, 60000.0)  # revealed in the next hour, not marked yet
    assert not c.heap
    m2 = c.mkts[c.stage(minfo(T0 + 300_000), mdata(minfo(T0 + 300_000)), h, h)]
    c.schedule_ptb(m2, T0 + 300_500, 61000.0)  # its hour is marked: pushed now
    assert c.heap == [(T0 + 300_500, P_TIMER, c.hour_seq[h] + 0.5, m2.mi, ("ptb", m2, 61000.0))]
    c.push(h + HOUR + 5, P_TIMER, ("timer", "x", 1))  # pushed before the mark: ahead of the reveal
    c.mark_hour(h + HOUR)
    e = [x for x in c.heap if x[4][0] == "ptb" and x[4][1] is m]
    assert e == [(h + HOUR + 5, P_TIMER, c.hour_seq[h + HOUR] + 0.5, m.mi, ("ptb", m, 60000.0))]
    assert not c._ptb_wait
    c.push(h + HOUR + 5, P_TIMER, ("timer", "y", 1))  # pushed after the mark: behind it
    c._drain(h + HOUR + 6)
    assert m.pub.price_to_beat == 60000.0 and m2.pub.price_to_beat == 61000.0


def test_ptb_reveal_time_respects_load_ms():
    from test_engine import M1, T0, market_one

    c = _core()
    h = T0 // HOUR * HOUR
    c.mark_hour(h)
    m = c.mkts[c.stage(M1, market_one(), h, T0 + 10_000)]  # admitted after its window start and its reveal tick
    c.schedule_ptb(m, T0 + 2_000, 60000.0)
    assert [x[:4] for x in c.heap] == [(T0 + 10_000, 2, c.hour_seq[h] + 0.5, m.mi)]


def _unresolved_markets(c):
    from test_engine import T0, mdata, minfo

    infos = [minfo(T0 + 600_000, outcome=None), minfo(T0 + 300_000, outcome=None), minfo(T0, "15m", outcome=None)]
    for i in infos:
        c.admit(c.stage(i, mdata(i), T0 - HOUR, T0 - HOUR))
    return infos


def test_settle_order_by_window_end_then_load():
    from test_engine import T0

    c = _core(defer_settle=True)
    a, b, cc = _unresolved_markets(c)  # window ends T0 + 900 s, T0 + 600 s, T0 + 900 s
    assert a.window_end_ms == cc.window_end_ms > b.window_end_ms
    c.start(T0 - HOUR)
    c._drain(T0 + 1_000_000)
    assert all(m.closed for m in c.mkts) and not [e for e in c.heap if e[4][0] == "settle"]
    seen, pf_seen = [], []
    settle, pf_settle = c._settle, c.pf.settle

    def spy(m, t):
        seen.append((m.info.slug, t))
        settle(m, t)

    def pf_spy(slug, outcome, t):
        pf_seen.append(slug)
        return pf_settle(slug, outcome, t)

    c._settle, c.pf.settle = spy, pf_spy
    res = T0 + 2_000_000
    for i in (cc, b, a):  # reverse load order, all received at the same ms
        c.set_resolution(i.slug, "Up", res)
    c.finish(T0 + 2_100_000)
    assert seen == [(b.slug, res), (a.slug, res), (cc.slug, res)] and pf_seen == [b.slug, a.slug, cc.slug]
    for i in (a, b, cc):
        row = c.market_rows[i.slug]
        assert row["outcome"] == "Up" and not row["voided"] and row["settle_ms"] == res + 60_000 and row["pnl"] == 0.0


def _taker_cls():
    from polydata.bt.strategy import Strategy

    class Taker(Strategy):
        name = "taker"
        family = "test"

        def on_market_open(self, ctx, market):
            ctx.take(market.slug, "Up", "BUY", 5, 0.99)

    return Taker


@pytest.mark.parametrize("when,dt,outcome", [("before", 10, "Up"), ("before", 60_000, "Down"),
                                             ("after", 10, "Down"), ("after", 60_000, "Up")])
def test_set_resolution_before_and_after_close(when, dt, outcome):
    import math

    from test_engine import T0, mdata, minfo

    from polydata.bt.core import K_OPEN

    c = _core(_taker_cls()(), defer_settle=True)
    info = minfo(T0, outcome=None)
    md = mdata(info)
    mi = c.stage(info, md, T0 - HOUR, T0 - HOUR)
    c.admit(mi)
    c.start(T0 - HOUR)
    c.dispatch([int(md.snaps_recv[0])], [K_OPEN], [mi], [-1], [], [])
    end, res = info.window_end_ms, info.window_end_ms + dt
    seen = []
    settle = c._settle
    c._settle = lambda m, t: (seen.append(t), settle(m, t))
    if when == "before":
        c._drain(end - 1)
        c.set_resolution(info.slug, outcome, res)
        assert c.mkts[mi].resolved and not c.mkts[mi].closed
    else:
        c._drain(end + 1)
        assert c.mkts[mi].closed and not [e for e in c.heap if e[4][0] == "settle"]
        assert len(c.fill_rows) == 1 and math.isnan(c.fill_rows[0][16])  # outcome unknown at the close
        c.set_resolution(info.slug, outcome, res)
    c.finish(end + 200_000)
    assert seen == [max(res, end + 126)]
    f = c.result().fills
    assert len(f) == 1 and f["settle_value"].iloc[0] == (1.0 if outcome == "Up" else 0.0)
    row = c.market_rows[info.slug]
    assert row["outcome"] == outcome and not row["voided"] and row["settle_ms"] == res + 60_000
    assert row["pnl"] == pytest.approx((5.0 if outcome == "Up" else 0.0) - f["px"].iloc[0] * 5 - f["fee"].iloc[0])


def test_finish_unresolved_voids():
    import math

    from test_engine import T0

    c = _core(defer_settle=True)
    a, b, cc = _unresolved_markets(c)
    c.start(T0 - HOUR)
    c._drain(b.window_end_ms + 1)  # b is closed; a and cc close during the final drain
    c.set_resolution(a.slug, "Down", T0 + 950_000)
    seen = []
    settle = c._settle
    c._settle = lambda m, t: (seen.append((m.info.slug, t)), settle(m, t))
    c.finish_unresolved()
    c.finish(T0 + 2_000_000)
    assert seen == [(b.slug, b.window_end_ms + 126), (cc.slug, cc.window_end_ms + 126), (a.slug, T0 + 950_000)]
    for i in (b, cc):
        row = c.market_rows[i.slug]
        assert row["voided"] is True and row["reason"] == "no outcome at settlement" and math.isnan(row["pnl"])
    assert c.market_rows[a.slug]["voided"] is False


def test_on_trades_appended_lowers_next_ex():
    from test_engine import M1, market_one

    c = _core()
    mi = c.stage(M1, market_one(), 0, 0)
    c.on_trades_appended(mi)  # not live: nothing to do
    assert c.next_ex == float("inf")

    class Ex:
        def next_event_ms(self):
            return 123

    c.mkts[mi].ex = Ex()
    c.live[mi] = c.mkts[mi]
    c.on_trades_appended(mi)
    assert c.next_ex == 123

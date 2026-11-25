"""The Core / ReplayDriver split (engine refactor for the paper runner)."""

from __future__ import annotations


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

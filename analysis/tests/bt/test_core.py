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

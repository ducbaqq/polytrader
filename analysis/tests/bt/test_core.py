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

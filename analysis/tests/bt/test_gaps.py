from __future__ import annotations

import duckdb
import pandas as pd

from polydata.bt import gaps as G

T0 = 1_795_520_000_000


def ev(rows):
    return pd.DataFrame(rows, columns=["ts_ms", "kind", "source", "detail"])


def markets(rows):
    return pd.DataFrame(rows, columns=["slug", "series", "coin", "window_start_ms", "window_end_ms", "outcome"])


def mkt(slug="btc-updown-5m-1", series="btc-up-or-down-5m", coin="btc", start=T0, outcome="Up"):
    return (slug, series, coin, start, start + 300_000, outcome)


def test_ws_close_to_open_is_gap():
    e = ev([
        (T0, "ws_close", "pm:btc-up-or-down-5m", ""), (T0 + 6000, "ws_open", "pm:btc-up-or-down-5m", ""),
        (T0 + 10_000, "ws_stale", "polymarket", ""), (T0 + 10_500, "ws_close", "polymarket", ""),
        (T0 + 12_000, "ws_open", "polymarket", ""),
        (T0 + 20_000, "pm_subscriptions", "polymarket", ""),
        (T0 + 30_000, "ws_close", "polymarket-rtds", ""), (T0 + 31_000, "ws_open", "polymarket-rtds", ""),
        (T0 + 40_000, "stop", "recorder", "SIGTERM"), (T0 + 45_000, "start", "recorder", "pid=1"),
    ])
    g = G.event_gaps(e, end_ms=T0 + 100_000, bn_syms=["BTCUSDT", "ETHUSDT"])
    rows = {(r.stream, r.key, r.from_ms, r.to_ms) for r in g.itertuples()}
    assert ("pm", "btc-up-or-down-5m", T0, T0 + 6000) in rows
    assert ("pm", "*", T0 + 10_000, T0 + 12_000) in rows  # stale then close: one gap from the first
    assert ("cl", "*", T0 + 30_000, T0 + 31_000) in rows
    assert ("pm", "*", T0 + 40_000, T0 + 45_000) in rows and ("cl", "*", T0 + 40_000, T0 + 45_000) in rows
    assert ("bn", "BTCUSDT", T0 + 40_000, T0 + 45_000) in rows
    assert not any(r.from_ms == T0 + 20_000 for r in g.itertuples())  # pm_subscriptions is no gap


def test_open_ended_gap_runs_to_end():
    g = G.event_gaps(ev([(T0, "ws_close", "binance-fut-public", "")]), end_ms=T0 + 9000, bn_syms=["BTCUSDT"])
    assert g.iloc[0].to_ms == T0 + 9000


def gap_df(rows):
    return pd.DataFrame(rows, columns=["stream", "key", "from_ms", "to_ms", "source"])


def test_short_gap_not_voiding():
    m = markets([mkt()])
    short = gap_df([("pm", "btc-up-or-down-5m", T0 + 1000, T0 + 5000, "x")])  # 4000 ms
    long_ = gap_df([("pm", "btc-up-or-down-5m", T0 + 1000, T0 + 7000, "x")])  # 6000 ms
    assert G.voided_markets(m, short).empty
    v = G.voided_markets(m, long_)
    assert list(v.slug) == ["btc-updown-5m-1"] and "pm" in v.reason.iloc[0]
    star = gap_df([("pm", "*", T0 + 1000, T0 + 7000, "x")])
    assert len(G.voided_markets(m, star)) == 1
    outside = gap_df([("pm", "btc-up-or-down-5m", T0 + 300_000, T0 + 400_000, "x")])  # starts at window_end
    assert G.voided_markets(m, outside).empty


def test_binance_silence_voids_coin_only():
    m = markets([mkt(), mkt("eth-updown-5m-1", "eth-up-or-down-5m", "eth")])
    g = gap_df([("bn", "BTCUSDT", T0 + 100, T0 + 1500, "x")])
    v = G.voided_markets(m, g)
    assert list(v.slug) == ["btc-updown-5m-1"]
    assert G.voided_markets(m, gap_df([("bn", "BTCUSDT", T0 + 100, T0 + 900, "x")])).empty


def test_unresolved_market_voided():
    m = markets([mkt(outcome=None)])
    v = G.voided_markets(m, gap_df([]))
    assert list(v.reason) == ["no outcome"]


def test_bn_silences_query():
    con = duckdb.connect()
    con.execute("CREATE TABLE bn_fut_bbo(recv_ms BIGINT, sym VARCHAR)")
    con.execute("INSERT INTO bn_fut_bbo VALUES (?,'BTCUSDT'),(?,'BTCUSDT'),(?,'BTCUSDT'),(?,'ETHUSDT'),(?,'ETHUSDT')",
                [T0, T0 + 500, T0 + 2500, T0, T0 + 900])
    g = G.bn_silences(con, T0, T0 + 10_000, max_gap_ms_bn=1000)
    assert [(r.key, r.from_ms, r.to_ms) for r in g.itertuples()] == [("BTCUSDT", T0 + 500, T0 + 2500)]

from __future__ import annotations

import duckdb
import pandas as pd
import pytest

from polydata.bt import gaps as G

T0 = 1_795_520_000_000


@pytest.fixture(autouse=True)
def _fresh_health_looks(monkeypatch):
    monkeypatch.setattr(G, "_HEALTH_LOOKED", set())  # one health look per (sealed day, process): reset per test


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


def bn_con(bbo, trades=()):
    con = duckdb.connect()
    con.execute("CREATE TABLE bn_fut_bbo(recv_ms BIGINT, sym VARCHAR)")
    con.execute("CREATE TABLE bn_fut_trade(recv_ms BIGINT, sym VARCHAR)")
    for sym, ts in bbo:
        con.executemany("INSERT INTO bn_fut_bbo VALUES (?,?)", [(t, sym) for t in ts])
    if trades:
        con.executemany("INSERT INTO bn_fut_trade VALUES (?,?)", [(t, s) for s, t in trades])
    return con


def test_slow_symbol_silence_without_trades_is_not_a_gap():
    con = bn_con([("BNBUSDT", [T0, T0 + 2500]), ("BTCUSDT", [T0, T0 + 500, T0 + 1000, T0 + 1500, T0 + 2000, T0 + 2500])])
    assert G.bn_silences(con, T0, T0 + 10_000).empty


def test_silence_with_trade_inside_is_a_gap():
    con = bn_con([("BNBUSDT", [T0, T0 + 2500])], trades=[("BNBUSDT", T0 + 1200)])
    g = G.bn_silences(con, T0, T0 + 10_000)
    assert [(r.key, r.from_ms, r.to_ms) for r in g.itertuples()] == [("BNBUSDT", T0, T0 + 2500)]
    assert "trades inside" in g.source.iloc[0]
    # a trade exactly at a boundary is not inside
    con = bn_con([("BNBUSDT", [T0, T0 + 2500])], trades=[("BNBUSDT", T0), ("ETHUSDT", T0 + 1200)])
    assert G.bn_silences(con, T0, T0 + 10_000).empty


def test_three_symbols_silent_together_is_a_gap():
    con = bn_con([(s, [T0, T0 + 2000 + i * 100] + [T0 + 2500 + 500 * k for k in range(15)]) for i, s in enumerate(["A", "B", "C"])])
    g = G.bn_silences(con, T0, T0 + 10_000)
    assert sorted(g.key) == ["A", "B", "C"]  # 9500 vs end 10000: no tail gap
    assert all("multi-symbol" in x for x in g.source)
    con = bn_con([(s, [T0, T0 + 2000] + [T0 + 2500 + 500 * k for k in range(15)]) for s in ["A", "B"]])
    assert G.bn_silences(con, T0, T0 + 10_000).empty


def test_bn_silence_straddling_start_and_dead_feed():
    # silence T0-3000 .. T0+500 straddles start (trade inside); BNB feed dies at T0+1000, trade later
    con = bn_con([("BNBUSDT", [T0 - 3000, T0 + 500, T0 + 1000]), ("BTCUSDT", [T0 - 100, T0 + 9500])],
                 trades=[("BNBUSDT", T0 - 1000), ("BNBUSDT", T0 + 5000)])
    g = G.bn_silences(con, T0, T0 + 10_000)
    rows = [(r.key, r.from_ms, r.to_ms) for r in g.itertuples()]
    assert ("BNBUSDT", T0, T0 + 500) in rows  # clipped to the range
    assert ("BNBUSDT", T0 + 1000, T0 + 10_000) in rows  # never came back: runs to end_ms


def test_pm_silence_not_spurious_at_range_edges():
    con = duckdb.connect()
    con.execute("CREATE TABLE markets(slug VARCHAR, series VARCHAR, window_start_ms BIGINT, window_end_ms BIGINT)")
    con.execute("CREATE TABLE pm_book(slug VARCHAR, recv_ms BIGINT)")
    con.execute("CREATE TABLE pm_api_trades(slug VARCHAR, ts_ms BIGINT)")
    con.execute("INSERT INTO markets VALUES ('h','btc-up-or-down-hourly',?,?)", [T0 - 1_800_000, T0 + 1_800_000])
    # events every second from window start, including before start_ms
    con.execute("INSERT INTO pm_book SELECT 'h', ? + i*1000 FROM range(0, 3600) t(i)", [T0 - 1_800_000])
    con.execute("INSERT INTO pm_api_trades VALUES ('h', ?), ('h', ?)", [T0 - 500_000, T0 + 500_000])
    assert G.pm_silences(con, T0, T0 + 600_000).empty


# ------------------------------------------------------------------------------- sealed catalog days

def _day_ms(d: str) -> int:
    from datetime import date, datetime, timezone

    x = date.fromisoformat(d)
    return int(datetime(x.year, x.month, x.day, tzinfo=timezone.utc).timestamp() * 1000)


def _write_events(root, day: str, rows) -> None:
    from datetime import datetime, timezone

    d = root / "catalog" / day
    d.mkdir(parents=True, exist_ok=True)
    df = pd.DataFrame([(datetime.fromtimestamp(t / 1000, timezone.utc).isoformat(), k, s, "")
                       for t, k, s in rows], columns=["ts", "kind", "source", "detail"])
    con = duckdb.connect()
    con.register("df", df)
    con.execute(f"COPY df TO '{(d / 'rec_events.csv.zst').as_posix()}' (HEADER, COMPRESSION zstd)")


def _looks(root) -> list[dict]:
    import json

    f = root / "runs" / "looks.jsonl"
    return [json.loads(x) for x in f.read_text().splitlines()] if f.exists() else []


def test_sealed_day_connection_gap_voids_market_in_final_replay(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYDATA_DIR", str(tmp_path))
    s = _day_ms("2026-11-25")  # sealed
    _write_events(tmp_path, "2026-11-25", [(s + 3_600_000, "ws_close", "polymarket"),
                                           (s + 3_610_000, "ws_open", "polymarket")])
    con = bn_con([("BTCUSDT", [s + 1000])])
    g = G.gap_table(con, s, s + 86_400_000)
    assert [(r.stream, r.key, r.from_ms, r.to_ms) for r in g.itertuples() if r.stream == "pm"] == \
        [("pm", "*", s + 3_600_000, s + 3_610_000)]
    v = G.voided_markets(markets([mkt(start=s + 3_600_000 - 100_000)]), g)
    assert len(v) == 1 and "pm gap" in v.reason.iloc[0]
    looks = _looks(tmp_path)
    assert [x["purpose"] for x in looks] == ["health"]  # once per call
    assert "2026-11-25" in str(looks[0]["detail"])


def test_close_on_sealed_day_open_next_day_voids_next_day_start(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYDATA_DIR", str(tmp_path))
    d1 = _day_ms("2026-11-26")  # validation; the day before is sealed
    _write_events(tmp_path, "2026-11-25", [(d1 - 30_000, "ws_close", "polymarket")])
    _write_events(tmp_path, "2026-11-26", [(d1 + 20_000, "ws_open", "polymarket")])
    con = bn_con([("BTCUSDT", [d1 + 1000])])
    g = G.gap_table(con, d1, d1 + 86_400_000)
    pm = g[g.stream == "pm"]
    assert [(r.from_ms, r.to_ms) for r in pm.itertuples()] == [(d1 - 30_000, d1 + 20_000)]
    v = G.voided_markets(markets([mkt(start=d1)]), g)
    assert list(v.slug) == ["btc-updown-5m-1"]
    assert [x["purpose"] for x in _looks(tmp_path)] == ["health"]


def test_no_look_recorded_without_sealed_catalog(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYDATA_DIR", str(tmp_path))
    d = _day_ms("2026-11-27")  # train; the day before (11-26) is validation
    _write_events(tmp_path, "2026-11-27", [(d + 1000, "ws_close", "polymarket"), (d + 9000, "ws_open", "polymarket")])
    con = bn_con([("BTCUSDT", [d + 1000])])
    assert len(G.gap_table(con, d, d + 86_400_000)) >= 1
    assert _looks(tmp_path) == []


def test_health_look_once_per_sealed_day_per_process(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYDATA_DIR", str(tmp_path))
    s, s2 = _day_ms("2026-11-25"), _day_ms("2026-11-28")  # both sealed
    for d in ("2026-11-25", "2026-11-28"):
        _write_events(tmp_path, d, [(_day_ms(d) + 1000, "ws_close", "polymarket"), (_day_ms(d) + 9000, "ws_open", "polymarket")])
    con = bn_con([("BTCUSDT", [s + 1000])])
    G.gap_table(con, s, s + 86_400_000)
    G.gap_table(con, s, s + 86_400_000)
    assert len(_looks(tmp_path)) == 1
    G.gap_table(con, s2, s2 + 86_400_000)  # a new sealed day: one more look, for that day only
    looks = _looks(tmp_path)
    assert len(looks) == 2 and looks[1]["detail"]["rec_events_days"] == ["2026-11-28"]

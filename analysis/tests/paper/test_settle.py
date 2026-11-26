"""Chainlink settler and the pm_resolution row it produces."""

from __future__ import annotations

import sys
from pathlib import Path

import duckdb
import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bt"))

from synth import meta_row  # noqa: E402

from polydata.bt.live import LiveRow, Resolution  # noqa: E402
from polydata.bt.types import MarketInfo  # noqa: E402
from polydata.build import build  # noqa: E402
from polydata.paper.session import SessionWriter  # noqa: E402
from polydata.paper.settle import ChainlinkSettler, resolution_row  # noqa: E402
from polydata.views import create_views  # noqa: E402

H11 = 1_795_518_000_000  # 2026-11-24 11:00 UTC (train day)
WS = H11
WE = H11 + 300_000


def info(slug="btc-updown-5m-x", coin="btc", ws=WS, we=WE) -> MarketInfo:
    return MarketInfo(slug, coin, "5m", "btc-up-or-down-5m", "0xc", ws, we, None, 0.05, 1.0, 10, 5.0, None, None)


def feed(s: ChainlinkSettler, ticks, coin="btc"):
    out = []
    for recv, ts, px in ticks:
        out += s.on_tick(coin, recv, ts, px)
    return out


def test_settler_up_down_and_tie():
    rs = WS - 120_000
    start = [(WS - 50_000 + 100, WS - 50_000, 100.0), (WS - 20_000 + 100, WS - 20_000, 102.0)]
    reveal = [(WS + 300, WS + 1, 999.0)]
    late_start = [(WS + 2_000, WS - 10_000, 500.0)]  # a start-window tick received after the reveal: not shown
    ptb = float(np.mean([100.0, 102.0]))
    for close, want in (([101.0, 103.0], "Up"), ([100.0, 100.5], "Down")):
        s = ChainlinkSettler(rs)
        s.admit(info(), rs)
        ticks = start + reveal + late_start + [(WE - 30_000 + 50 + i, WE - 30_000 + i, p) for i, p in enumerate(close)]
        assert feed(s, ticks) == []
        assert feed(s, [(WE + 400, WE - 1, close[-1])]) == []  # still inside the close window: no decision
        res = feed(s, [(WE + 900, WE + 5, 1.0), (WE + 950, WE + 6, 1.0)])  # decided on the first tick >= we only
        twap = float(np.mean(close + [close[-1]]))
        assert [(r.slug, r.outcome, r.recv_ms, r.source, r.ptb, r.close_twap) for r in res] == [
            ("btc-updown-5m-x", want, WE + 900, "paper-chainlink", ptb, twap)]
    # tie: close_twap == ptb -> Up
    s = ChainlinkSettler(rs)
    s.admit(info(), rs)
    res = feed(s, [(WS - 1000, WS - 30_000, 5.0), (WS + 10, WS, 5.0), (WE - 100, WE - 1000, 5.0), (WE + 10, WE, 9.0)])
    assert [(r.outcome, r.ptb, r.close_twap, r.recv_ms) for r in res] == [("Up", 5.0, 5.0, WE + 10)]


def test_settler_waits_without_ticks():
    rs = WS - 120_000
    s = ChainlinkSettler(rs)
    s.admit(info(), rs)  # empty start window
    assert feed(s, [(WS + 10, WS + 1, 1.0), (WE - 10, WE - 100, 1.0), (WE + 10, WE, 1.0)]) == []
    assert "start" in s.gave_up["btc-updown-5m-x"]
    s = ChainlinkSettler(rs)
    s.admit(info(), rs)  # empty close window
    assert feed(s, [(WS - 100, WS - 1000, 1.0), (WS + 10, WS + 1, 1.0), (WE + 10, WE, 1.0)]) == []
    assert "end" in s.gave_up["btc-updown-5m-x"]
    s = ChainlinkSettler(WS - 59_999)  # ws - 60 s < rs
    s.admit(info(), WS - 59_999)
    assert feed(s, [(WS - 100, WS - 1000, 1.0), (WS + 10, WS + 1, 1.0), (WE - 10, WE - 100, 1.0),
                    (WE + 10, WE, 1.0)]) == []
    assert "session start" in s.gave_up["btc-updown-5m-x"]
    # other coins' ticks never decide a market
    s = ChainlinkSettler(rs)
    s.admit(info(), rs)
    assert feed(s, [(WE + 10, WE + 1, 1.0)], coin="eth") == []
    assert not s.gave_up


def test_resolution_row_markets_sql(tmp_path, monkeypatch):
    meta = meta_row("btc", "5m", WS, WS - 100_000)
    slug = meta.values["event"]["slug"]
    r_end = WE + 1234
    res = Resolution(slug, "Down", r_end, "paper-chainlink", 61_000.125, 60_999.5)
    d = resolution_row(res, meta.values["event"])
    assert d["event"]["markets"] == meta.values["event"]["markets"] and "eventMetadata" not in meta.values["event"]
    root = tmp_path / "s"
    w = SessionWriter(root)
    w.write(meta)
    w.write(LiveRow("pm_resolution", r_end, d))
    w.close()
    monkeypatch.setenv("POLYDATA_DIR", str(root))
    build()
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    create_views(con, parquet=root / "parquet", include_sealed=True)
    row = con.execute("SELECT outcome, price_to_beat, final_price, epoch_ms(resolution_seen_at), window_end_ms "
                      "FROM markets WHERE slug = ?", [slug]).fetchall()
    assert row == [("Down", 61_000.125, 60_999.5, r_end, WE)]

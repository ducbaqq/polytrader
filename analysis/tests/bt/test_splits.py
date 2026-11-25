from __future__ import annotations

import json
from datetime import date

import pytest

from test_pipeline import HOUR, MARKET, data, write_jsonl, write_tsv  # noqa: F401  (data is a fixture)

TRADE_HEADER = ["recv_ms", "ts_ms", "asset", "side", "price", "size", "fee_rate_bps", "tx_hash"]


def test_day_kind():
    from polydata.bt.splits import day_kind

    assert day_kind(date(2026, 11, 25)) == "sealed"
    assert day_kind(date(2026, 11, 26)) == "validation"
    assert day_kind(date(2026, 11, 27)) == "train"
    assert day_kind(date(2026, 11, 28)) == "sealed"
    assert day_kind(date(2026, 11, 24)) == "train"
    assert day_kind(date(2026, 11, 23)) == "excluded"


def test_days_of_kind_and_filter_consistent():
    import duckdb

    from polydata.bt.splits import SEALED_SQL_FILTER, day_kind, days_of_kind

    days = [date(2026, 11, 20) + __import__("datetime").timedelta(days=i) for i in range(20)]
    assert days_of_kind(days, {"sealed"}) == [d for d in days if day_kind(d) == "sealed"]
    con = duckdb.connect()
    for d in days:
        ok = con.execute(f"SELECT {SEALED_SQL_FILTER.replace('date', 'DATE ' + repr(str(d)))}").fetchall()[0][0]
        assert ok == (day_kind(d) != "sealed"), d


def _trade(path_hour, recv_ms):
    return [[recv_ms, recv_ms, "slug|U", "BUY", 0.5, 10, 0, "0x1"]]


@pytest.fixture()
def two_days(data):
    write_tsv(data / "raw" / "2026-11-24/11" / "pm_trade.1795518000000.tsv.zst", TRADE_HEADER, _trade(0, 1795518000000))
    write_tsv(data / "raw" / "2026-11-25/11" / "pm_trade.1795604400000.tsv.zst", TRADE_HEADER, _trade(0, 1795604400000))
    from polydata.build import build

    build()
    return data


def test_connect_hides_sealed(two_days):
    from polydata.views import connect

    q = "SELECT DISTINCT date FROM pm_trade ORDER BY 1"
    assert [str(r[0]) for r in connect().sql(q).fetchall()] == ["2026-11-24"]

    con = connect(include_sealed=True, purpose="test")
    assert [str(r[0]) for r in con.sql(q).fetchall()] == ["2026-11-24", "2026-11-25"]
    lines = (two_days / "runs" / "looks.jsonl").read_text().splitlines()
    assert len(lines) == 1
    rec = json.loads(lines[0])
    assert rec["purpose"] == "test" and {"utc", "detail", "git_commit"} <= set(rec)

    with pytest.raises(ValueError):
        connect(include_sealed=True)


def test_derived_views_hide_sealed(two_days):
    from polydata.views import connect

    write_tsv(two_days / "raw" / "2026-11-25/11" / "rtds_price.1795604400000.tsv.zst",
              ["recv_ms", "topic", "symbol", "ts_ms", "value", "full_accuracy_value", "server_ts_ms", "conn"],
              [[1795604400000, "crypto_prices_chainlink", "btc/usd", 1795604400000, 1.0, 1.0, 1795604400000, "a"]])
    from polydata.build import build

    build()
    assert connect().sql("SELECT count(*) FROM chainlink").fetchall()[0][0] == 2
    assert connect(include_sealed=True, purpose="health").sql("SELECT count(*) FROM chainlink").fetchall()[0][0] == 3


def test_markets_crossing_into_sealed_day_hidden(data):
    crossing = dict(MARKET, slug="btc-updown-1h-1795563000", endDate="2026-11-25T00:30:00Z")
    write_jsonl(data / "raw" / HOUR / "pm_market_meta.1795518000001.jsonl.zst",
                [{"recv_ms": 1795517000000, "d": {"series": "btc-up-or-down-1h",
                                                   "event": {"slug": crossing["slug"], "markets": [crossing]}}}])
    from polydata.build import build
    from polydata.views import connect

    build()
    slugs = lambda con: {r[0] for r in con.sql("SELECT slug FROM markets").fetchall()}  # noqa: E731
    assert slugs(connect()) == {"btc-updown-5m-1795518000"}
    assert slugs(connect(include_sealed=True, purpose="test")) == {"btc-updown-5m-1795518000", crossing["slug"]}

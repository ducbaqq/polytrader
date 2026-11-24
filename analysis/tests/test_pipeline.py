"""End-to-end test of build + views on small synthetic recorder files (no network, no credentials)."""

from __future__ import annotations

import json

import duckdb
import pytest

SLUG = "btc-updown-5m-1795518000"  # window 2026-11-24 11:00-11:05 UTC
HOUR = "2026-11-24/11"


def write_tsv(path, header, rows):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix("")  # .tsv
    tmp.write_text("\t".join(header) + "\n" + "".join("\t".join(map(str, r)) + "\n" for r in rows))
    duckdb.sql(
        f"COPY (SELECT * FROM read_csv('{tmp}', delim='\t', header=true, all_varchar=true)) "
        f"TO '{path}' (FORMAT csv, DELIMITER '\t', HEADER, QUOTE '', COMPRESSION zstd)"
    )
    tmp.unlink()


def write_jsonl(path, objs):
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_suffix("")
    tmp.write_text("".join(json.dumps(o) + "\n" for o in objs))
    duckdb.sql(f"COPY (SELECT line FROM read_csv('{tmp}', columns={{'line': 'VARCHAR'}}, delim=chr(1), header=false, quote='', escape='')) "
               f"TO '{path}' (FORMAT csv, HEADER false, QUOTE '', ESCAPE '', DELIMITER chr(1), COMPRESSION zstd)")
    tmp.unlink()


MARKET = {
    "slug": SLUG, "conditionId": "0xabc", "endDate": "2026-11-24T11:05:00Z",
    "outcomes": '["Up", "Down"]', "clobTokenIds": '["111", "222"]',
    "resolutionSource": "https://data.chain.link/streams/btc-usd-twap-60s-streams",
    "cryptoMarketConfig": {"asset": "btc", "twapLookbackSeconds": 60},
    "feeSchedule": {"rate": 0.05, "exponent": 1, "takerOnly": True, "rebateRate": 0.2},
    "orderPriceMinTickSize": 0.01, "orderMinSize": 5,
}


@pytest.fixture()
def data(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYDATA_DIR", str(tmp_path))
    raw = tmp_path / "raw" / HOUR
    # rtds_price: an old file without `conn`, a new one with it
    write_tsv(raw / "rtds_price.1795518000000.tsv.zst",
              ["recv_ms", "topic", "symbol", "ts_ms", "value", "full_accuracy_value", "server_ts_ms"],
              [[1795518000100, "crypto_prices_chainlink", "btc/usd", 1795518000000, 60000.5, 60000.5, 1795518000050]])
    write_tsv(raw / "rtds_price.1795518300000.tsv.zst",
              ["recv_ms", "topic", "symbol", "ts_ms", "value", "full_accuracy_value", "server_ts_ms", "conn"],
              [[1795518300100, "crypto_prices_chainlink", "btc/usd", 1795518300000, 60010.0, 60010.0, 1795518300050, "a"],
               [1795518300200, "crypto_prices", "btcusdt", 1795518300000, 60011.0, 60011.0, 1795518300060, "b"]])
    write_tsv(raw / "pm_book.1795518000000.tsv.zst", ["recv_ms", "ts_ms", "asset", "bids", "asks", "hash"],
              [[1795518000500, 1795518000400, f"{SLUG}|U", "0.48:100|0.47:50", "0.52:80", "h1"],
               [1795518000500, 1795518000400, f"{SLUG}|D", "0.48:80", "", "h2"]])
    write_tsv(raw / "bn_fut_bbo.1795518000000.tsv.zst",
              ["recv_ms", "sym", "update_id", "event_ms", "txn_ms", "bid", "bid_qty", "ask", "ask_qty"],
              [[1795518000000 + i, "BTCUSDT", 100 + i, 1795517999990 + i, 1795517999990 + i, 60000.1, 1.5, 60000.2, 2.0] for i in range(5)])
    write_jsonl(raw / "pm_market_meta.1795518000000.jsonl.zst",
                [{"recv_ms": 1795517000000, "d": {"series": "btc-up-or-down-5m", "event": {"slug": SLUG, "markets": [MARKET]}}}])
    write_jsonl(raw / "pm_resolution.1795518300000.jsonl.zst",
                [{"recv_ms": 1795518400000, "d": {"slug": SLUG, "outcome": "Up", "event": {
                    "slug": SLUG, "volume": 1234.5, "eventMetadata": {"priceToBeat": 60000.0, "finalPrice": 60010.0},
                    "markets": [MARKET]}}}])
    write_jsonl(raw / "bn_depth_snapshot.1795518000000.jsonl.zst",
                [{"recv_ms": 1795518001000, "d": {"market": "fut", "sym": "BTCUSDT", "lastUpdateId": 99,
                                                   "bids": [["60000.1", "1.5"], ["60000.0", "3"]], "asks": [["60000.2", "2"]]}}])
    return tmp_path


def test_build_and_views(data):
    from polydata.build import build
    from polydata.views import connect

    build()
    con = connect()

    assert con.sql("SELECT count(*) FROM bn_fut_bbo").fetchall()[0][0] == 5
    rows = con.sql("SELECT conn FROM rtds_price ORDER BY recv_ms").fetchall()
    assert [r[0] for r in rows] == [None, "a", "b"]  # old file gets NULL conn
    assert con.sql("SELECT count(*) FROM chainlink WHERE coin = 'btc'").fetchall()[0][0] == 2
    assert con.sql("SELECT coin FROM pm_binance_relay").fetchall() == [("btc",)]

    book = con.sql("SELECT slug, outcome, levels(bids), levels(asks) FROM pm_book ORDER BY outcome DESC").fetchall()
    assert book[0][:2] == (SLUG, "Up")
    assert book[0][2] == [{"price": 0.48, "size": 100.0}, {"price": 0.47, "size": 50.0}]
    assert book[1][3] == []  # empty side

    m = con.sql("SELECT coin, timeframe, window_start_ms, window_end_ms, up_token_id, down_token_id, fee_rate, "
                "twap_lookback_s, outcome, price_to_beat, final_price FROM markets").fetchall()
    assert m == [("btc", "5m", 1795518000000, 1795518300000, "111", "222", 0.05, 60, "Up", 60000.0, 60010.0)]

    snap = con.sql("SELECT len(bids), bids[1].price, asks[1].qty FROM bn_depth_snapshot").fetchall()
    assert snap == [(2, 60000.1, 2.0)]

    date = con.sql("SELECT DISTINCT date FROM bn_fut_bbo").fetchall()[0][0]
    assert str(date) == "2026-11-24"


def test_incremental(data, capsys):
    from polydata.build import build

    build()
    capsys.readouterr()
    build()
    assert "built 0," in capsys.readouterr().out  # nothing changed -> nothing rebuilt

    # A newly synced file for the same hour triggers a rebuild of that stream-hour only.
    write_tsv(data / "raw" / HOUR / "bn_fut_bbo.1795518300000.tsv.zst",
              ["recv_ms", "sym", "update_id", "event_ms", "txn_ms", "bid", "bid_qty", "ask", "ask_qty"],
              [[1795518300000, "ETHUSDT", 1, 1795518300000, 1795518300000, 1700.0, 1.0, 1700.1, 1.0]])
    build()
    assert "built 1," in capsys.readouterr().out
    from polydata.views import connect

    assert connect().sql("SELECT count(*) FROM bn_fut_bbo").fetchall()[0][0] == 6

import json
from pathlib import Path

from polydata.paper.feeds.binance import parse_fut
from polydata.paper.feeds.polymarket import parse_clob
from polydata.paper.feeds.rtds import parse_rtds

G = Path(__file__).parent / "golden"
load = lambda n: json.load(open(G / f"{n}.json"))  # noqa: E731
KEYS = {"TUP": "s|U", "TDN": "s|D"}
key = lambda t: KEYS.get(t, t)  # noqa: E731


def norm(rows):
    return [[s, list(v) if isinstance(v, tuple) else v] for s, v in rows]


def clob(name):
    g = load(name)
    assert norm(parse_clob(g["raw"], key)) == g["expected"]


def test_clob_book():
    clob("clob_book")


def test_clob_price_change_many_rows_same_recv_order():
    clob("clob_price_change")


def test_clob_best_bid_ask():
    clob("clob_best_bid_ask")


def test_clob_last_trade_price():
    clob("clob_last_trade_price")


def test_clob_new_market_and_other():
    clob("clob_new_market_and_other")
    g = load("clob_new_market_and_other")
    assert parse_clob(g["raw"], key)[1][1] == json.loads(g["raw"])[1]  # tick_size_change unchanged


def test_clob_array_message():
    clob("clob_array")


def test_clob_pong_ignored():
    assert parse_clob("PONG", key) == [] and parse_clob("", key) == []


def test_rtds_dedup_across_connections():
    g = load("rtds_dedup")
    last = {}
    for (conn, raw), exp in zip(g["messages"], g["expected"]):
        assert norm(parse_rtds(raw, conn, last)) == exp


def test_binance_book_ticker():
    g = load("binance_book_ticker")
    assert norm(parse_fut(g["raw"])) == g["expected"]


def test_binance_agg_trade_maker_flag():
    for raw, exp in load("binance_agg_trade")["messages"]:
        assert norm(parse_fut(raw)) == exp


def test_number_text_roundtrip():
    x = 0.1 + 0.2
    raw = json.dumps({"event_type": "last_trade_price", "asset_id": "TUP", "timestamp": 1, "side": "BUY",
                      "price": x, "size": 1e-7, "fee_rate_bps": None, "transaction_hash": "h"})
    (_, v), = parse_clob(raw, key)
    assert float(v[3]) == x and float(v[4]) == 1e-7 and v[5] == ""


def test_polymarket_discovery_emits_meta_then_registers():
    import asyncio

    from polydata.paper.feeds.polymarket import PolymarketFeed
    from polydata.paper.feeds.series import series_for

    ev = {"slug": "btc-updown-5m-1764000300", "startTime": "2025-11-24T16:00:00Z", "endDate": "2025-11-24T16:05:00Z",
          "markets": [{"clobTokenIds": '["TUP","TDN"]', "outcomes": '["Up","Down"]', "conditionId": "0xc",
                       "eventStartTime": "2025-11-24T16:00:00Z", "endDate": "2025-11-24T16:05:00Z"}]}

    class FakeRest:
        def is_paused(self, api):
            return False

        async def get_json(self, api, url):
            assert "series_id=10684" in url and "limit=4" in url
            return [ev, ev]  # a repeat must not emit twice

    out = []
    feed = PolymarketFeed(series_for(["btc"], ["5m"]), out.extend, lambda *a: None, FakeRest(), lambda: 0)
    asyncio.run(feed.discover())
    assert out == [("pm_market_meta", {"series": "btc-up-or-down-5m", "event": ev})]
    (m,) = feed.markets()
    assert (m.slug, m.condition_id, m.up_token, m.start, m.end) == ("btc-updown-5m-1764000300", "0xc", "TUP", 1764000000000, 1764000300000)
    assert feed._key("TDN") == "btc-updown-5m-1764000300|D"


def test_rtds_non_dict_null_value_and_missing_timestamp():
    last = {}
    assert norm(parse_rtds("[1, 2]", "a", last)) == [["rtds_other", {"conn": "a", "raw": [1, 2]}]]
    null_val = json.dumps({"topic": "t", "type": "update", "timestamp": 9, "payload": {"symbol": "btc/usd", "timestamp": 5, "value": None}})
    assert norm(parse_rtds(null_val, "a", last)) == [["rtds_price", ["t", "btc/usd", "5", "", "", "9", "a"]]]
    no_ts = json.dumps({"topic": "t", "type": "update", "payload": {"symbol": "eth/usd", "value": 3.5}})
    assert norm(parse_rtds(no_ts, "a", last)) == [["rtds_price", ["t", "eth/usd", "", "3.5", "", "", "a"]]]


def test_polymarket_malformed_market_skipped():
    import asyncio

    from polydata.paper.feeds.polymarket import PolymarketFeed
    from polydata.paper.feeds.series import series_for

    good = {"slug": "g", "startTime": "2025-11-24T16:00:00Z", "endDate": "2025-11-24T16:05:00Z",
            "markets": [{"clobTokenIds": '["A","B"]', "outcomes": '["Up","Down"]', "conditionId": "0x"}]}
    bad = {"slug": "b", "markets": [{"clobTokenIds": '["C","D"]', "outcomes": '["Up","Down"]', "conditionId": "0y"}]}

    class FakeRest:
        def is_paused(self, api):
            return False

        async def get_json(self, api, url):
            return [bad, good]

    out, evs = [], []
    feed = PolymarketFeed(series_for(["btc"], ["5m"]), out.extend, lambda *a: evs.append(a), FakeRest(), lambda: 0)
    asyncio.run(feed.discover())
    assert [m.slug for m in feed.markets()] == ["g"] and [r[0] for r in out] == ["pm_market_meta"]
    assert any(e[0] == "rest_error" for e in evs)

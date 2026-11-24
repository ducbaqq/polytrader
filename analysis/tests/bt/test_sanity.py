"""Sanity strategies and the validation suite on synthetic data.

The engine-level fixtures come from test_engine (`minfo`, `make_con`, ...). For the validation checks the
same data is also written as raw-shaped `pm_price_change` / `pm_book` / `pm_trade` / `pm_best_bid_ask`
tables, so the engine loads it through the real `load_coin` and the independent SQL reads the same rows.
"""

from __future__ import annotations

import json
import math
from datetime import datetime, timezone

import pandas as pd
import pytest
from test_engine import NO_EVENTS, T0, cfg, make_con, minfo

from polydata.bt import engine, validate
from polydata.bt.strategy import get_strategy
from polydata.fees import taker_fee
from polydata.views import MACROS

RATE = 0.05


# ------------------------------------------------------------------------------------------ raw tables

def _date(ms: int):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).date()


class Raw:
    """Raw-shaped Polymarket rows for synthetic markets (Up token only; Down trades allowed)."""

    def __init__(self):
        self.pc, self.bk, self.tr, self.infos = [], [], [], []

    def market(self, info, bids="0.48:100", asks="0.52:100", book_ts=None, changes=(), trades=(), snaps=(),
               down_snaps=()):
        """Up snapshot at `book_ts` (default 60 s before the window) plus extra `snaps` (ts, bids, asks);
        Up change rows (ts, side, price, size, best_bid, best_ask), recv = ts + 100; trades
        (ts, outcome, side, price, size), recv = ts + 120."""
        self.infos.append(info)
        s = info.slug
        bt = info.window_start_ms - 60_000 if book_ts is None else book_ts
        for o, ts, b, a in [("Up", bt, bids, asks), *(("Up", *x) for x in snaps), *(("Down", *x) for x in down_snaps)]:
            self.bk.append(dict(recv_ms=ts + 100, ts_ms=ts, asset=f"{s}|{o[0]}", bids=b, asks=a, hash="h",
                                seq=len(self.bk), date=_date(ts), slug=s, outcome=o))
        for ts, side, p, q, bb, ba in changes:
            self.pc.append(dict(recv_ms=ts + 100, ts_ms=ts, asset=f"{s}|U", side=side, price=p, size=float(q),
                                best_bid=bb, best_ask=ba, seq=len(self.pc), date=_date(ts), slug=s, outcome="Up"))
        for ts, o, side, p, q in trades:
            self.tr.append(dict(recv_ms=ts + 120, ts_ms=ts, asset=f"{s}|{o[0]}", side=side, price=p, size=float(q),
                                fee_rate_bps=0.0, tx_hash="0x", seq=len(self.tr), date=_date(ts), slug=s, outcome=o))
        return self

    def con(self):
        con = make_con(self.infos, T0 - 1_200_000, T0 + 1_300_000)
        con.execute(MACROS)
        cols = {
            "pm_price_change": ("recv_ms BIGINT, ts_ms BIGINT, asset VARCHAR, side VARCHAR, price DOUBLE, size DOUBLE, "
                                "best_bid DOUBLE, best_ask DOUBLE, seq BIGINT, date DATE, slug VARCHAR, outcome VARCHAR"),
            "pm_book": ("recv_ms BIGINT, ts_ms BIGINT, asset VARCHAR, bids VARCHAR, asks VARCHAR, hash VARCHAR, seq BIGINT, "
                        "date DATE, slug VARCHAR, outcome VARCHAR"),
            "pm_trade": ("recv_ms BIGINT, ts_ms BIGINT, asset VARCHAR, side VARCHAR, price DOUBLE, size DOUBLE, "
                         "fee_rate_bps DOUBLE, tx_hash VARCHAR, seq BIGINT, date DATE, slug VARCHAR, outcome VARCHAR"),
        }
        # a late trade elsewhere: the engine ends the replay range at the last recorded pm_trade
        sentinel = dict(recv_ms=T0 + 1_250_000, ts_ms=T0 + 1_250_000, asset="sentinel|U", side="BUY", price=0.5,
                        size=1.0, fee_rate_bps=0.0, tx_hash="0x", seq=len(self.tr), date=_date(T0), slug="sentinel",
                        outcome="Up")
        for name, rows in (("pm_price_change", self.pc), ("pm_book", self.bk), ("pm_trade", [*self.tr, sentinel])):
            con.execute(f"CREATE TABLE {name} ({cols[name]})")
            if rows:
                con.register("_rows", pd.DataFrame(rows))
                con.execute(f"INSERT INTO {name} BY NAME SELECT * FROM _rows")
                con.unregister("_rows")
        # pm_best_bid_ask as Polymarket publishes it: one row per change, from the change rows' top columns
        con.execute("""CREATE TABLE pm_best_bid_ask AS
            SELECT recv_ms, ts_ms, asset, best_bid, best_ask, best_ask - best_bid AS spread, seq, date, slug, outcome
            FROM pm_price_change""")
        return con


def run(raw: Raw, strategy: str, **kw):
    return engine.run_coin(cfg(strategy=strategy, timeframes=["5m"], **kw), "btc", raw.con, rec_events=NO_EVENTS)


M1 = minfo(T0)
M2 = minfo(T0 + 300_000, outcome="Down")


def two_markets() -> Raw:
    r = Raw()
    r.market(M1, trades=[(T0 + 20_000, "Up", "SELL", 0.48, 150), (T0 + 40_000, "Down", "SELL", 0.48, 30)])
    r.market(M2, bids="0.40:100", asks="0.45:3|0.47:100")  # ask size 3 < 5: skipped by the reference
    return r


# ------------------------------------------------------------------------------------------ strategies

def test_sanity_strategies_registered():
    for name in ("noop", "buy-and-hold-up", "always-cross", "join-best"):
        cls = get_strategy(name)
        assert cls.name == name and cls.family == "sanity"


def test_noop_pnl_zero():
    res = run(two_markets(), "noop")
    assert res.orders.empty and res.fills.empty
    m = res.markets[~res.markets.voided]
    assert len(m) == 2 and (m.pnl == 0.0).all()


def test_buy_and_hold_up_hand_computed():
    r = Raw().market(M1)
    res = run(r, "buy-and-hold-up")
    pnl = res.markets.set_index("slug").pnl[M1.slug]
    assert pnl == pytest.approx(5 * (1 - 0.52) - taker_fee(5, 0.52, RATE), abs=1e-9)
    f = res.fills
    assert len(f) == 1 and f.liquidity.iloc[0] == "taker" and f["size"].iloc[0] == 5
    placed = res.orders[res.orders.event == "placed"]
    assert placed.side.tolist() == ["BUY"] and placed.px.tolist() == [0.99]


def test_buy_and_hold_up_losing_side():
    r = Raw().market(M2, asks="0.47:100")
    pnl = run(r, "buy-and-hold-up").markets.set_index("slug").pnl[M2.slug]
    # the fee is a rounding tie (5 * 0.05 * 0.47 * 0.53 = 0.062275): the simulator's average price
    # 2.35 / 5 is one ulp above 0.47, so its fee rounds up by 0.00001
    assert pnl == pytest.approx(-5 * 0.47 - taker_fee(5, 0.47, RATE), abs=2e-5)


def test_always_cross_loses_spread_plus_fees():
    r = Raw().market(M1, bids="0.48:100", asks="0.52:100")
    res = run(r, "always-cross")
    pnl = res.markets.set_index("slug").pnl[M1.slug]
    loss = (0.52 - 0.48) * 5 + taker_fee(5, 0.52, RATE) + taker_fee(5, 0.48, RATE)
    assert pnl == pytest.approx(-loss, abs=1e-9)
    f = res.fills.sort_values("ts_ms")
    assert f.side.tolist() == ["BUY", "SELL"] and (f.liquidity == "taker").all()
    # the sell is placed when the buy's fill report arrives (126 ms), and arrives 200 ms later
    assert f.ts_ms.iloc[1] - f.ts_ms.iloc[0] == 126 + 200


def test_join_best_quotes_both_sides_and_settles():
    r = Raw().market(M1, trades=[(T0 + 20_000, "Up", "SELL", 0.48, 150), (T0 + 40_000, "Up", "BUY", 0.52, 130)])
    res = run(r, "join-best")
    o = res.orders
    placed = o[o.event == "placed"]
    # bid: Up BUY 0.48; ask: Down BUY 0.48 (= Up SELL 0.52) while no Up is held, then Up SELL 0.52
    assert {("Up", "BUY", 0.48), ("Down", "BUY", 0.48)} <= set(zip(placed.token, placed.side, placed.px))
    assert (placed["size"] == 5).all() and (placed.post_only == True).all()  # noqa: E712
    f = res.fills
    assert (f.liquidity == "maker").all() and len(f) >= 2
    # orders still resting at the window end expire there (review focus 4)
    exp = o[o.event == "expired"]
    assert len(exp) >= 1 and (exp.ms == M1.window_end_ms).all() and (exp.reason == "window end").all()
    m = res.markets.set_index("slug").loc[M1.slug]
    assert not math.isnan(m.pnl) and m.settle_ms == M1.resolution_recv_ms + 60_000
    eq = res.equity
    assert eq.equity.iloc[-1] == pytest.approx(1000 + m.pnl) and eq.locked.iloc[-1] == pytest.approx(0)


def test_join_best_stops_30s_before_end_and_one_order_per_side():
    # the best bid moves every 10 s, so the bid is cancelled and rejoined
    ch = [(T0 + k * 10_000, "BUY", 0.48, 100 if k % 2 else 0, 0.48 if k % 2 else 0.47, 0.52) for k in range(1, 30)]
    r = Raw().market(M1, bids="0.48:100|0.47:100", changes=ch)
    res = run(r, "join-best")
    o = res.orders
    placed = o[o.event == "placed"]
    assert (placed.ms < M1.window_end_ms - 30_000).all()
    assert (o.event == "cancelled").sum() >= 5
    # never more than one live order per (Up-terms) side on the exchange
    assert validate.one_order_per_side(o, order_latency_ms=200)


def test_unresolved_market_voided_for_sanity_strategies():
    bad = minfo(T0 + 300_000, outcome=None)
    r = Raw().market(M1).market(bad)
    res = run(r, "buy-and-hold-up")
    m = res.markets.set_index("slug")
    assert bool(m.loc[bad.slug].voided) and math.isnan(m.loc[bad.slug].pnl)
    assert bad.slug not in set(res.orders.slug)


# ------------------------------------------------------------------------------------------ validation checks

def test_buy_and_hold_reference_matches_and_skips_thin_asks():
    r = two_markets()
    res = run(r, "buy-and-hold-up")
    chk = validate.check_buy_and_hold(r.con(), res, order_latency_ms=200)
    assert chk["passed"], chk
    assert chk["compared"] == 1 and chk["skipped"] == 1 and chk["max_abs_diff"] < 1e-9


def test_buy_and_hold_reference_detects_a_wrong_pnl():
    r = two_markets()
    res = run(r, "buy-and-hold-up")
    res.markets.loc[res.markets.slug == M1.slug, "pnl"] += 0.01
    assert not validate.check_buy_and_hold(r.con(), res, order_latency_ms=200)["passed"]


def test_reference_book_uses_changes_after_the_snapshot():
    # the market opens at the snapshot (recv ws - 59.9 s), the order arrives at ws - 59.7 s: by then 0.52 was
    # removed by a change after the snapshot, so the best ask is 0.53
    r = Raw().market(M1, asks="0.52:100|0.53:100", changes=[(M1.window_start_ms - 59_800, "SELL", 0.52, 0, 0.48, 0.53)])
    res = run(r, "buy-and-hold-up")
    chk = validate.check_buy_and_hold(r.con(), res, order_latency_ms=200)
    assert chk["passed"] and chk["compared"] == 1
    assert res.fills.px.iloc[0] == pytest.approx(0.53)


def test_reference_book_uses_a_later_down_snapshot():
    # At subscription the Up snapshot says Up bids 0.49:100; a Down snapshot 1 ms later says Down asks 0.53:30,
    # i.e. Up bid 0.47:30. The later one is the book (as on 2026-11-24): the sell hits 0.47.
    ws = M1.window_start_ms
    r = Raw().market(M1, bids="0.49:100", asks="0.51:100", book_ts=ws - 60_000,
                     down_snaps=[(ws - 59_999, "0.49:100", "0.53:30")])
    res = run(r, "always-cross")
    assert res.fills.sort_values("ts_ms").px.tolist() == [0.51, 0.47]
    chk = validate.check_always_cross(r.con(), res, order_latency_ms=200)
    assert chk["passed"] and chk["compared"] == 1, chk


def test_always_cross_reference_matches():
    r = two_markets()
    res = run(r, "always-cross")
    chk = validate.check_always_cross(r.con(), res, order_latency_ms=200)
    assert chk["passed"], chk
    assert chk["compared"] == 1 and chk["skipped"] == 1


def _jb_raw() -> Raw:
    trades = [(T0 + 20_000, "Up", "SELL", 0.48, 60),  # eats 60 of the 100 ahead
              (T0 + 25_000, "Down", "BUY", 0.52, 50),  # = Up SELL 0.48: 40 ahead, then 10 -> our 5
              (T0 + 30_000, "Up", "BUY", 0.53, 3),  # trade-through of our Up-terms ask 0.52: 3
              (T0 + 31_000, "Down", "SELL", 0.47, 40),  # = Up BUY 0.53: through again, the rest 2
              (T0 + 80_000, "Up", "SELL", 0.46, 2),  # through our new bid (rejoined at 0.48): 2
              ]
    ch = [(T0 + 50_000, "BUY", 0.48, 30, 0.48, 0.52)]
    return Raw().market(M1, trades=trades, changes=ch)


def test_fill_support_rederivation_matches_exchange():
    r = _jb_raw()
    res = run(r, "join-best")
    assert len(res.fills) >= 3
    chk = validate.check_fill_support(r.con(), res, order_latency_ms=200)
    assert chk["passed"], chk
    assert chk["sim_fills"] == chk["ref_fills"] and chk["mismatches"] == 0 and chk["ahead_mismatches"] == 0


def test_fill_support_detects_unsupported_fill():
    r = _jb_raw()
    res = run(r, "join-best")
    f = res.fills.copy()
    f.loc[f.index[0], "size"] += 1.0
    res.fills = f
    chk = validate.check_fill_support(r.con(), res, order_latency_ms=200)
    assert not chk["passed"] and chk["mismatches"] >= 1


def test_average_maker_and_join_best_c_per_share():
    r = two_markets()
    con = r.con()
    # M1 resolves Up: a taker SELL Up at 0.48 (150) -> maker bought Up at 0.48: +0.52; a taker SELL Down at 0.48
    # (30): maker bought Down at 0.48, Down lost: -0.48
    want = 100 * (150 * 0.52 + 30 * -0.48) / 180
    assert validate.average_maker_c(con, [M1.slug, M2.slug]) == pytest.approx(want)
    fills = pd.DataFrame(dict(slug=[M1.slug, M1.slug], token=["Up", "Down"], side=["BUY", "SELL"], px=[0.48, 0.40],
                              size=[5.0, 5.0]))
    mk = pd.DataFrame(dict(slug=[M1.slug], outcome=["Up"]))
    assert validate.fills_c_per_share(fills, mk) == pytest.approx(100 * (5 * 0.52 + 5 * 0.40) / 10)


def test_determinism_digest_is_stable_and_sensitive():
    r = _jb_raw()
    a, b = run(r, "join-best"), run(r, "join-best")
    assert validate.summary_bytes(a) == validate.summary_bytes(b)
    b.fills = b.fills.assign(size=b.fills["size"] + 1e-6)
    assert validate.summary_bytes(a) != validate.summary_bytes(b)


def test_sim_version_and_is_validated(tmp_path, monkeypatch):
    v = validate.sim_version()
    assert len(v) == 12 and int(v, 16) >= 0 and v == validate.sim_version()
    monkeypatch.setenv("POLYDATA_DIR", str(tmp_path))
    assert not validate.is_validated(v)
    p = tmp_path / "runs" / "validation" / f"{v}.json"
    p.parent.mkdir(parents=True)
    p.write_text(json.dumps({"sim_version": v, "passed": False, "checks": {}}))
    assert not validate.is_validated(v)
    p.write_text(json.dumps({"sim_version": v, "passed": True, "checks": {}}))
    assert validate.is_validated(v)
    assert not validate.is_validated("000000000000")


def test_run_suite_synthetic(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYDATA_DIR", str(tmp_path))
    r = Raw()
    trades = [(T0 + 20_000, "Up", "SELL", 0.48, 150), (T0 + 25_000, "Down", "BUY", 0.52, 50),
              (T0 + 30_000, "Up", "BUY", 0.53, 30)]
    r.market(M1, trades=trades, changes=[(T0 + 5_000, "BUY", 0.48, 90, 0.48, 0.52)])
    r.market(M2, bids="0.40:100", asks="0.45:100", trades=[(M2.window_start_ms + 20_000, "Up", "BUY", 0.45, 100)],
             changes=[(M2.window_start_ms + 5_000, "BUY", 0.40, 90, 0.40, 0.45)])
    out = validate.run_suite(["2026-11-24"], coins=("btc",), timeframes=("5m",), con_factory=r.con,
                             recon_rows=50)
    p = tmp_path / "runs" / "validation" / f"{out['sim_version']}.json"
    assert json.loads(p.read_text()) == json.loads(json.dumps(out))
    c = out["checks"]
    assert set(c) == {"book_reconstruction", "noop", "buy_and_hold_up", "always_cross", "fill_support",
                      "maker_edge", "determinism"}
    for k in ("book_reconstruction", "noop", "buy_and_hold_up", "always_cross", "fill_support", "determinism"):
        assert c[k]["passed"], (k, c[k])
    assert out["passed"] == all(x["passed"] for x in c.values())
    assert validate.is_validated(out["sim_version"]) == out["passed"]

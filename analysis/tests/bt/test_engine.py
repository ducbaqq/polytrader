"""Replay engine on synthetic data: an in-memory DuckDB with `markets`, `bn_fut_bbo` and `chainlink`
tables, and MarketData built with `from_frames`, injected through `run_coin(loader=...)`."""

from __future__ import annotations

import math
from datetime import datetime, timezone

import duckdb
import numpy as np
import pandas as pd
import pytest

from polydata.bt import engine
from polydata.bt.config import RunConfig
from polydata.bt.marketdata import MarketData
from polydata.bt.strategy import REGISTRY, BookView, Strategy, register
from polydata.bt.types import MarketInfo

T0 = 1_795_519_800_000  # 2026-11-24 11:30:00 UTC, a train day after REPLAY_START_MS
DAY = "2026-11-24"
LEN = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000}
NO_EVENTS = pd.DataFrame({"ts_ms": pd.Series([], dtype="int64"), "kind": [], "source": [], "detail": []})


def minfo(ws: int, tf: str = "5m", outcome: str | None = "Up", coin: str = "btc",
          price_to_beat: float | None = 60000.0) -> MarketInfo:
    return MarketInfo(slug=f"{coin}-updown-{tf}-{ws // 1000}", coin=coin, timeframe=tf,
                      series=f"{coin}-up-or-down-{tf}", condition_id=f"0x{ws}", window_start_ms=ws,
                      window_end_ms=ws + LEN[tf], price_to_beat=price_to_beat, fee_rate=0.05, fee_exponent=1.0,
                      tick_px=10, min_order_size=5.0, outcome=outcome,
                      resolution_recv_ms=None if outcome is None else ws + LEN[tf] + 30_000)


def mdata(info: MarketInfo, bids="0.48:100", asks="0.52:100", changes=(), trades=(), book_ts=None) -> MarketData:
    """Up snapshot at `book_ts` (default 60 s before the window); Up change rows (ts, side, price, size,
    best_bid, best_ask), recv = ts + 100; trades (ts, outcome, side, price, size), recv = ts + 120."""
    bt = info.window_start_ms - 60_000 if book_ts is None else book_ts
    b = pd.DataFrame([(bt, bt + 100, "Up", bids, asks)], columns=["ts_ms", "recv_ms", "outcome", "bids", "asks"])
    c = pd.DataFrame([(ts, ts + 100, "Up", s, p, q, bb, ba) for ts, s, p, q, bb, ba in changes],
                     columns=["ts_ms", "recv_ms", "outcome", "side", "price", "size", "best_bid", "best_ask"])
    t = pd.DataFrame([(ts, ts + 120, o, s, p, q) for ts, o, s, p, q in trades],
                     columns=["ts_ms", "recv_ms", "outcome", "side", "price", "size"])
    k = pd.DataFrame([], columns=["ts_ms", "recv_ms", "new_tick_size"])
    return MarketData.from_frames(info, c, b, t, k)


def make_con(infos: list[MarketInfo], bn_from: int, bn_to: int, coin: str = "btc", step: int = 100):
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    mk = pd.DataFrame([dict(slug=i.slug, coin=i.coin, timeframe=i.timeframe, series=i.series,
                            condition_id=i.condition_id, window_start_ms=i.window_start_ms,
                            window_end_ms=i.window_end_ms, price_to_beat=i.price_to_beat, fee_rate=i.fee_rate,
                            fee_exponent=i.fee_exponent, tick_size_at_discovery=i.tick_px / 1000,
                            min_order_size=i.min_order_size, outcome=i.outcome,
                            res_ms=i.resolution_recv_ms) for i in infos])
    mk["res_ms"] = mk["res_ms"].astype("Int64")
    mk["outcome"] = mk["outcome"].astype(object)
    con.register("_mk", mk)
    con.execute("CREATE TABLE markets AS SELECT * EXCLUDE (res_ms), make_timestamp(res_ms * 1000) AS resolution_seen_at FROM _mk")
    recv = np.arange(bn_from, bn_to, step, dtype=np.int64)
    mid = 60000.0 + 5.0 * np.sin(np.arange(len(recv)) / 50.0)
    date = [datetime.fromtimestamp(r / 1000, timezone.utc).date() for r in recv[::1]]
    bn = pd.DataFrame(dict(recv_ms=recv, sym=f"{coin.upper()}USDT", update_id=np.arange(len(recv)), event_ms=recv - 5,
                           txn_ms=recv - 6, bid=mid - 0.05, bid_qty=1.0, ask=mid + 0.05, ask_qty=2.0, date=date))
    con.register("_bn", bn)
    con.execute("CREATE TABLE bn_fut_bbo AS SELECT * FROM _bn")
    cr = np.arange(bn_from, bn_to, 1000, dtype=np.int64)
    cl = pd.DataFrame(dict(recv_ms=np.repeat(cr, 2) + np.tile([0, 7], len(cr)), ts_ms=np.repeat(cr - 1500, 2),
                           coin=coin, price=np.repeat(60000.0 + np.arange(len(cr)) % 7, 2),
                           date=np.repeat([datetime.fromtimestamp(r / 1000, timezone.utc).date() for r in cr], 2)))
    con.register("_cl", cl)
    con.execute("CREATE TABLE chainlink AS SELECT * FROM _cl")
    for n in ("_mk", "_bn", "_cl"):
        con.unregister(n)
    return con


def loader_for(mds: dict[str, MarketData]):
    calls = []

    def loader(con, coin, timeframes, start_ms, end_ms, slugs=None):
        calls.append(sorted(slugs) if slugs is not None else None)
        return {s: md for s, md in mds.items() if (slugs is None or s in slugs) and md.info.coin == coin}

    loader.calls = calls
    return loader


def cfg(**kw) -> RunConfig:
    base = dict(strategy="test", params={}, coins=["btc"], timeframes=["5m", "15m", "1h"], days=[DAY], capital=1000.0)
    base.update(kw)
    return RunConfig(**base)


# ------------------------------------------------------------------------------------------ fixtures

M1 = minfo(T0)


def market_one() -> MarketData:
    # each trade at a level price is echoed by the level decrease it caused, 10 ms or less before it
    return mdata(M1, changes=[(T0 + 1_995, "SELL", 0.52, 97, 0.48, 0.52),
                              (T0 + 5_000, "BUY", 0.49, 50, 0.49, 0.52),
                              (T0 + 9_000, "BUY", 0.49, 0, 0.48, 0.52),
                              (T0 + 15_000, "BUY", 0.48, 200, 0.48, 0.52),
                              (T0 + 19_990, "BUY", 0.48, 50, 0.48, 0.52),
                              (T0 + 30_000, "SELL", 0.51, 40, 0.48, 0.51),
                              (T0 + 39_995, "BUY", 0.48, 20, 0.48, 0.51)],
                 trades=[(T0 + 2_000, "Up", "BUY", 0.52, 3),
                         (T0 + 20_000, "Up", "SELL", 0.48, 150),
                         (T0 + 40_000, "Down", "BUY", 0.52, 30)])


class Recorder(Strategy):
    """Records everything it sees, checking each value against ctx.now."""
    name = "recorder"
    family = "test"

    def __init__(self, params=None):
        super().__init__(params)
        self.events: list[tuple] = []
        self.tops: list[tuple[int, int]] = []  # (now, top.recv_ms)
        self.seen: list[tuple[int, int]] = []  # (now, recv_ms of a delivered or queried value)
        self.fill_reports: list[tuple[int, int]] = []  # (now, fill ts_ms)
        self.infos: list[MarketInfo] = []
        self.ptb: list[tuple[int, float]] = []  # (now, price_to_beat) whenever it was visible
        self.done = False

    def _look(self, ctx):
        for m in ctx.markets():
            if m.price_to_beat is not None:
                self.ptb.append((ctx.now, m.price_to_beat))
            top = ctx.top(m.slug)
            self.tops.append((ctx.now, top.recv_ms))
            ctx.book(m.slug).bids  # noqa: B018 (forces the level lookup)
            ctx.fair_value(m.slug)
        for v in (ctx.binance("BTCUSDT"), ctx.chainlink("btc")):
            if v is not None:
                self.seen.append((ctx.now, v.recv_ms))

    def on_start(self, ctx):
        self.events.append((ctx.now, "start"))
        ctx.set_timer("tick", 1_000)

    def on_market_open(self, ctx, market):
        self.infos.append(market)
        self.events.append((ctx.now, "open", market.slug))
        if not self.done:
            self.done = True
            ctx.place(market.slug, "Up", "BUY", 0.48, 10)  # joins behind 100 at 0.48
            ctx.take(market.slug, "Up", "BUY", 5, 0.99)

    def on_book(self, ctx, market, book):
        self.events.append((ctx.now, "book", market.slug, book.best_bid, book.best_ask))
        self._look(ctx)

    def on_trade(self, ctx, market, trade):
        self.seen.append((ctx.now, trade.recv_ms))
        self.events.append((ctx.now, "trade", market.slug, trade.up_px, trade.size))
        self._look(ctx)

    def on_binance(self, ctx, sym, bbo):
        self.seen.append((ctx.now, bbo.recv_ms))
        if ctx.now % 5_000 < 100:
            self.events.append((ctx.now, "bn", sym, bbo.bid))
            self._look(ctx)

    def on_chainlink(self, ctx, coin, price):
        self.events.append((ctx.now, "cl", coin, price))
        self._look(ctx)

    def on_order(self, ctx, order):
        f = order.last_fill
        if order.event == "fill":
            self.fill_reports.append((ctx.now, f.ts_ms))
        self.events.append((ctx.now, "order", order.id, order.event, order.status, order.filled,
                            None if f is None else (f.price, f.size)))
        self._look(ctx)

    def on_timer(self, ctx, name):
        self.events.append((ctx.now, "timer", name))
        self._look(ctx)
        if ctx.now < T0 + 400_000:
            ctx.set_timer("tick", 10_000)

    def on_market_close(self, ctx, market):
        self.events.append((ctx.now, "close", market.slug))


def run_recorder(**kw):
    s = Recorder()
    res = engine.run_coin(cfg(**kw), "btc", lambda: make_con([M1], T0 - 1_200_000, T0 + 500_000),
                          loader=loader_for({M1.slug: market_one()}), strategy=s, rec_events=NO_EVENTS)
    return s, res


# --------------------------------------------------------------------------------------------- tests

def test_engine_no_lookahead():
    s, res = run_recorder()
    assert s.tops and all(top_recv <= now for now, top_recv in s.tops)
    assert s.seen and all(recv <= now for now, recv in s.seen)
    assert len(s.fill_reports) >= 2  # the take and the resting bid
    assert all(now >= ts + 126 for now, ts in s.fill_reports)
    # the strategy never sees the outcome or the resolution time
    assert s.infos and all(i.outcome is None and i.resolution_recv_ms is None for i in s.infos)
    # price_to_beat = mean of the received Chainlink ticks with ts in [start - 60 s, start), revealed
    # when the first tick with ts >= start is received: unknown when the market opens before that
    assert all(i.price_to_beat is None for i in s.infos)
    bn_from = T0 - 1_200_000  # make_con: tick k is received at bn_from + k s, ts 1.5 s earlier, price 60000 + k % 7
    ks = [k for k in range(2000) if T0 - 60_000 <= bn_from + k * 1000 - 1500 < T0]
    want = sum(60000.0 + k % 7 for k in ks) / len(ks)
    # revealed at T0 + 2 s (the tick with ts T0 + 0.5 s), after the data events of that ms: first seen just after
    assert len(ks) == 60 and s.ptb and T0 + 2_000 < min(t for t, _ in s.ptb) < T0 + 2_200
    assert all(v == pytest.approx(want) for _, v in s.ptb)
    # events reach the strategy in time order
    times = [e[0] for e in s.events]
    assert times == sorted(times)


def test_engine_fills_and_frames():
    s, res = run_recorder()
    f = res.fills
    for c in ("slug", "order_id", "ts_ms", "report_ms", "token", "side", "px", "size", "fee", "liquidity",
              "queue_ahead_at_fill", "trade_ts_ms", "mid_at_fill", "mid_10s", "settle_value", "bn_move_1s"):
        assert c in f.columns, c
    taker = f[f.liquidity == "taker"].iloc[0]
    assert taker.px == pytest.approx(0.52) and taker["size"] == 5 and taker.fee > 0
    assert taker.mid_at_fill == pytest.approx(0.50) and taker.settle_value == 1.0
    maker = f[f.liquidity == "maker"].iloc[0]
    # 150 sold at 0.48: 100 ahead of us, then our 10
    assert maker.px == pytest.approx(0.48) and maker["size"] == 10 and maker.queue_ahead_at_fill == 0
    assert maker.trade_ts_ms == T0 + 20_000 and maker.report_ms == T0 + 20_126
    assert maker.mid_10s == pytest.approx(0.495)  # at T0 + 30 s the ask moved to 0.51 (ts clock, inclusive)
    assert (f.report_ms == f.ts_ms + 126).all()
    o = res.orders
    assert {"ms", "order_id", "slug", "token", "side", "px", "size", "reason", "queue_ahead", "event"} <= set(o.columns)
    assert {"placed", "accepted", "fill"} <= set(o.event)
    m = res.markets.set_index("slug").loc[M1.slug]
    assert not m.voided and m.outcome == "Up"
    # P&L: 15 Up shares bought (5 @ 0.52 + fee, 10 @ 0.48), Up wins.
    fee = taker.fee
    assert m.pnl == pytest.approx(15 - 5 * 0.52 - fee - 10 * 0.48)
    assert m.settle_ms == M1.resolution_recv_ms + 60_000
    eq = res.equity
    assert len(eq) > 10 and eq.ms.is_monotonic_increasing
    assert eq.equity.iloc[-1] == pytest.approx(1000 + m.pnl)
    assert eq.cash.iloc[-1] == pytest.approx(1000 + m.pnl)


def test_engine_ordering_deterministic():
    s1, r1 = run_recorder(latency_jitter_ms=50, seed=7)
    s2, r2 = run_recorder(latency_jitter_ms=50, seed=7)
    assert s1.events == s2.events
    pd.testing.assert_frame_equal(r1.orders, r2.orders)
    pd.testing.assert_frame_equal(r1.fills, r2.fills)
    s3, _ = run_recorder(latency_jitter_ms=50, seed=8)
    assert s3.events != s1.events  # the seed matters


def test_latency_applies_to_orders():
    s, res = run_recorder(order_latency_ms=300)
    o = res.orders
    placed = o[o.event == "placed"].set_index("order_id").ms
    acc = o[o.event == "accepted"].set_index("order_id").ms
    assert ((acc - placed.loc[acc.index]) == 300).all()


def test_data_events_before_internal_events_at_same_ms():
    s, _ = run_recorder()
    # timer at a whole second coincides with Binance bundle ends only off by -1 ms, so look at order:
    # no internal event is ever delivered before a data event with the same timestamp
    kinds_at = {}
    for e in s.events:
        kinds_at.setdefault(e[0], []).append(e[1])
    for t, ks in kinds_at.items():
        internal = [i for i, k in enumerate(ks) if k in ("order", "timer", "close")]
        data = [i for i, k in enumerate(ks) if k in ("book", "trade", "bn", "cl")]
        if internal and data:
            assert max(data) < min(internal), (t, ks)


class Bidder(Strategy):
    name = "bidder"
    family = "test"

    def __init__(self, params=None):
        super().__init__(params)
        self.cash_seen: list[tuple[str, float]] = []
        self.opened: list[str] = []

    def on_market_open(self, ctx, market):
        self.opened.append(market.slug)
        self.cash_seen.append(("open", ctx.cash(market.slug)))
        ctx.place(market.slug, "Up", "BUY", 0.30, 10)  # far below the book: never fills
        ctx.place(market.slug, "Up", "BUY", 0.30, 10_000)  # more than the budget: refused locally

    def on_order(self, ctx, order):
        self.cash_seen.append((order.event, ctx.cash()))


def test_resting_order_cancelled_at_window_end_releases_cash():
    s = Bidder()
    res = engine.run_coin(cfg(capital=100.0), "btc", lambda: make_con([M1], T0 - 1_200_000, T0 + 500_000),
                          loader=loader_for({M1.slug: market_one()}), strategy=s, rec_events=NO_EVENTS)
    ev = dict(s.cash_seen)
    assert ev["open"] == pytest.approx(100.0)
    assert ev["rejected"] == pytest.approx(97.0)  # the big one: 3.00 locked by the small one
    assert ev["accepted"] == pytest.approx(97.0)
    assert ev["expired"] == pytest.approx(100.0)
    o = res.orders
    exp = o[o.event == "expired"]
    assert len(exp) == 1 and exp.ms.iloc[0] == M1.window_end_ms and exp.reason.iloc[0] == "window end"
    rej = o[o.event == "rejected"]
    assert rej.reason.iloc[0] == "insufficient cash"
    assert res.fills.empty and res.markets.set_index("slug").loc[M1.slug].pnl == 0.0


def test_market_crossing_into_sealed_day_not_replayed():
    # 2026-11-27 is a train day, 2026-11-28 is sealed.
    d27 = 1_795_737_600_000  # 2026-11-27 00:00 UTC
    assert datetime.fromtimestamp(d27 / 1000, timezone.utc).isoformat() == "2026-11-27T00:00:00+00:00"
    ok, cross = minfo(d27 + 22 * 3_600_000, "1h"), minfo(d27 + 23 * 3_600_000 + 1_800_000, "1h")
    mds = {i.slug: mdata(i) for i in (ok, cross)}
    for days in (None, ["2026-11-27"], ["2026-11-27", "2026-11-28"]):
        s = Bidder()
        ld = loader_for(mds)
        res = engine.run_coin(cfg(days=days, capital=100.0), "btc",
                              lambda: make_con([ok, cross], d27 + 21 * 3_600_000, d27 + 25 * 3_600_000, step=1000),
                              loader=ld, strategy=s, rec_events=NO_EVENTS)
        assert s.opened == [ok.slug], days
        assert all(cross.slug not in (c or []) for c in ld.calls)
        assert cross.slug not in set(res.markets.slug)
        assert ok.slug in set(res.markets.slug)


def test_unresolved_market_voided_not_settled_at_zero():
    bad = minfo(T0 + 300_000, outcome=None)
    mds = {M1.slug: market_one(), bad.slug: mdata(bad)}
    s = Bidder()
    res = engine.run_coin(cfg(capital=100.0), "btc", lambda: make_con([M1, bad], T0 - 1_200_000, T0 + 800_000),
                          loader=loader_for(mds), strategy=s, rec_events=NO_EVENTS)
    m = res.markets.set_index("slug")
    assert bool(m.loc[bad.slug].voided) and "no outcome" in m.loc[bad.slug].reason
    assert math.isnan(m.loc[bad.slug].pnl) and m.loc[bad.slug].outcome is None
    assert not m.loc[M1.slug].voided and m.loc[M1.slug].pnl == 0.0
    assert bad.slug not in s.opened


def test_market_after_data_end_not_replayed():
    late = minfo(T0 + 600_000)  # ends after the last recorded Binance row
    s = Bidder()
    res = engine.run_coin(cfg(), "btc", lambda: make_con([M1, late], T0 - 1_200_000, T0 + 700_000),
                          loader=loader_for({M1.slug: market_one(), late.slug: mdata(late)}), strategy=s,
                          rec_events=NO_EVENTS)
    assert s.opened == [M1.slug] and late.slug not in set(res.markets.slug)


def test_noop_pnl_zero_and_no_orders():
    class Noop(Strategy):
        name = "noop-test"

    res = engine.run_coin(cfg(), "btc", lambda: make_con([M1], T0 - 1_200_000, T0 + 500_000),
                          loader=loader_for({M1.slug: market_one()}), strategy=Noop(), rec_events=NO_EVENTS)
    assert res.orders.empty and res.fills.empty
    assert res.markets.pnl.sum() == 0.0
    assert res.equity.equity.iloc[-1] == 1000.0


def test_fair_value_none_before_15_min_of_binance_then_a_probability():
    class FV(Strategy):
        name = "fv-test"

        def __init__(self):
            super().__init__({})
            self.vals: list[tuple[int, float | None]] = []

        def on_chainlink(self, ctx, coin, price):
            for m in ctx.markets():
                self.vals.append((ctx.now, ctx.fair_value(m.slug)))

    s = FV()
    start = T0 - 800_000
    engine.run_coin(cfg(), "btc", lambda: make_con([M1], start, T0 + 500_000),
                    loader=loader_for({M1.slug: market_one()}), strategy=s, rec_events=NO_EVENTS)
    early = [v for t, v in s.vals if t < start + 900_000]
    late = [v for t, v in s.vals if t > start + 905_000 and t < M1.window_end_ms]
    assert early and all(v is None for v in early)
    assert late and all(v is not None and 0 < v < 1 for v in late)


def test_fair_value_scalar_matches_study():
    from polydata.bt.fairvalue import fair_value as fv_scalar
    from polydata.research.calibration import fair_value as fv_vec

    rng = np.random.default_rng(0)
    n = 400
    df = pd.DataFrame(dict(cl_px=60000 + rng.normal(0, 50, n), bn_mid=60000 + rng.normal(0, 50, n),
                           bn_mid_at_cl=60000 + rng.normal(0, 50, n), price_to_beat=60000 + rng.normal(0, 50, n),
                           sig15=rng.uniform(1e-5, 1e-4, n), tau_s=rng.uniform(0.2, 400, n),
                           cl_obs_n=rng.integers(0, 50, n).astype(float), cl_obs_avg=60000 + rng.normal(0, 50, n)))
    want = fv_vec(df, k=1.2, vol="sig15", nowcast=True)
    got = [fv_scalar(r.cl_px, r.bn_mid, r.bn_mid_at_cl, r.price_to_beat, r.sig15, r.tau_s, r.cl_obs_n,
                     r.cl_obs_avg, k=1.2) for r in df.itertuples()]
    np.testing.assert_allclose(got, want, rtol=1e-9, atol=1e-12)


def test_book_view_token_mapping():
    b = BookView([(0.48, 100.0), (0.47, 5.0)], [(0.52, 50.0)])
    assert b.best_bid == 0.48 and b.best_ask == 0.52 and b.mid == pytest.approx(0.50)
    assert b.spread == pytest.approx(0.04)
    d = b.for_token("Down")
    assert d.bids == [(0.48, 50.0)] and d.asks == [(0.52, 100.0), (0.53, 5.0)]
    assert BookView([], []).mid is None


def test_registry_and_config_roundtrip():
    @register
    class Foo(Strategy):
        name = "foo-bar-test"
        family = "test"

    assert REGISTRY["foo-bar-test"] is Foo
    with pytest.raises(ValueError):
        @register
        class Bad(Strategy):
            name = "Bad_Name"
    c = RunConfig.from_dict({"strategy": "foo-bar-test", "params": {"a": 1}, "coins": ["btc"]})
    assert c.order_latency_ms == 200 and c.fill_report_latency_ms == 126 and c.capital == 5000.0
    assert c.timeframes == ["5m", "15m", "1h"] and c.day_kinds == ["train", "validation"]
    assert RunConfig.from_dict(c.to_dict()) == c
    assert RunConfig.from_dict({"strategy": "x"}).coins == ["btc", "eth", "sol", "xrp", "bnb", "doge", "hype", "zec"]
    with pytest.raises(ValueError):
        RunConfig.from_dict({"strategy": "x", "nope": 1})
    with pytest.raises(ValueError):
        RunConfig.from_dict({"strategy": "x", "capital_mode": "weird"})


def test_reports_exact_without_other_events():
    """A strategy with no data handlers gets no data events: order reports must still arrive exactly
    fill_report_latency_ms after the exchange event (the engine advances exchanges to T - 126)."""

    class Quiet(Strategy):
        name = "quiet-test"

        def __init__(self):
            super().__init__({})
            self.reports: list[tuple[int, str, int]] = []

        def on_market_open(self, ctx, market):
            ctx.place(market.slug, "Up", "BUY", 0.48, 10)

        def on_order(self, ctx, order):
            ts = order.last_fill.ts_ms if order.last_fill else None
            self.reports.append((ctx.now, order.event, ts))
            if order.event == "fill":  # react: the exchange must not have run past now + latency
                ctx.place(order.slug, "Up", "SELL", 0.55, 10)

    s = Quiet()
    engine.run_coin(cfg(), "btc", lambda: make_con([M1], T0 - 1_200_000, T0 + 500_000),
                    loader=loader_for({M1.slug: market_one()}), strategy=s, rec_events=NO_EVENTS)
    open_ms = M1.window_start_ms - 60_000 + 100
    assert s.reports == [(open_ms + 200 + 126, "accepted", None), (T0 + 20_126, "fill", T0 + 20_000),
                         (T0 + 20_126 + 200 + 126, "accepted", None), (M1.window_end_ms + 126, "expired", None)]


def test_market_opens_only_after_first_in_range_snapshot():
    """Price changes before any in-range book snapshot leave the book untrusted: the market opens (and
    takes orders) only once the first snapshot in the replay range has been received."""
    snap = T0 + 10_000
    md = mdata(M1, book_ts=snap, changes=[(T0 - 50_000, "BUY", 0.48, 100, 0.48, 0.52),
                                           (T0 - 40_000, "SELL", 0.52, 100, 0.48, 0.52)])

    class Early(Strategy):
        name = "early-test"

        def __init__(self):
            super().__init__({})
            self.seen: list[tuple[int, str]] = []

        def on_start(self, ctx):
            ctx.set_timer("try", T0 - 30_000 - ctx.now)

        def on_timer(self, ctx, name):
            ctx.place(M1.slug, "Up", "BUY", 0.48, 10)

        def on_market_open(self, ctx, market):
            self.seen.append((ctx.now, "open"))

        def on_book(self, ctx, market, book):
            self.seen.append((ctx.now, "book"))

        def on_order(self, ctx, order):
            self.seen.append((ctx.now, f"{order.event}:{order.reason}"))

    s = Early()
    engine.run_coin(cfg(), "btc", lambda: make_con([M1], T0 - 1_200_000, T0 + 500_000), loader=loader_for({M1.slug: md}),
                    strategy=s, rec_events=NO_EVENTS)
    assert s.seen[0] == (T0 - 30_000, "rejected:market not open")
    assert s.seen[1] == (snap + 100, "open")
    assert all(t >= snap + 100 for t, _ in s.seen[1:])


class PlaceCancel(Strategy):
    name = "place-cancel-test"

    def __init__(self):
        super().__init__({})
        self.events: list[tuple[int, str, str]] = []

    def on_market_open(self, ctx, market):
        for _ in range(20):
            oid = ctx.place(market.slug, "Up", "BUY", 0.30, 10)
            ctx.cancel(oid)  # same handler, sent after the order

    def on_order(self, ctx, order):
        self.events.append((order.id, order.event, order.reason))


@pytest.mark.parametrize("jitter", [0, 50])
def test_place_then_cancel_in_one_handler_cancels(jitter):
    s = PlaceCancel()
    res = engine.run_coin(cfg(latency_jitter_ms=jitter, seed=3), "btc",
                          lambda: make_con([M1], T0 - 1_200_000, T0 + 500_000),
                          loader=loader_for({M1.slug: market_one()}), strategy=s, rec_events=NO_EVENTS)
    by = {}
    for oid, ev, reason in s.events:
        by.setdefault(oid, []).append((ev, reason))
    assert len(by) == 20
    for evs in by.values():
        assert evs == [("accepted", ""), ("cancelled", "cancel request")], evs
    assert "expired" not in set(res.orders.event)


def test_cancel_on_closed_market_reports_cancel_rejected():
    class Late(Strategy):
        name = "late-cancel-test"

        def __init__(self):
            super().__init__({})
            self.events: list[tuple[int, str, str]] = []

        def on_market_open(self, ctx, market):
            ctx.place(market.slug, "Up", "BUY", 0.30, 10)

        def on_market_close(self, ctx, market):
            ctx.cancel_all(market.slug)

        def on_order(self, ctx, order):
            self.events.append((ctx.now, order.event, order.reason))

    s = Late()
    engine.run_coin(cfg(), "btc", lambda: make_con([M1], T0 - 1_200_000, T0 + 500_000),
                    loader=loader_for({M1.slug: market_one()}), strategy=s, rec_events=NO_EVENTS)
    E = M1.window_end_ms
    assert s.events[1:] == [(E, "cancel_rejected", "market closed"), (E + 126, "expired", "window end")]


def test_cash_capped_at_budget_after_a_win():
    class Win(Strategy):
        name = "win-test"

        def __init__(self):
            super().__init__({})
            self.cash: list[float] = []

        def on_market_open(self, ctx, market):
            ctx.take(market.slug, "Up", "BUY", 50, 0.99)  # Up wins

        def on_timer(self, ctx, name):
            self.cash.append(ctx.cash())

        def on_order(self, ctx, order):
            ctx.set_timer("later", M1.resolution_recv_ms + 61_000 - ctx.now)

    s = Win()
    res = engine.run_coin(cfg(capital=100.0), "btc", lambda: make_con([M1], T0 - 1_200_000, T0 + 500_000),
                          loader=loader_for({M1.slug: market_one()}), strategy=s, rec_events=NO_EVENTS)
    pnl = res.markets.set_index("slug").loc[M1.slug].pnl
    assert pnl > 20
    assert s.cash and s.cash[-1] == pytest.approx(100.0)  # profits don't grow the budget
    assert res.equity.equity.iloc[-1] == pytest.approx(100 + pnl)


def test_mid_10s_nan_within_10s_of_window_end():
    end = M1.window_end_ms
    md = mdata(M1, changes=[(end - 6_000, "BUY", 0.48, 600, 0.48, 0.52), (end - 5_010, "BUY", 0.48, 100, 0.48, 0.52)],
               trades=[(end - 5_000, "Up", "SELL", 0.48, 500)])

    class Bid(Strategy):
        name = "bid-late-test"

        def on_market_open(self, ctx, market):
            ctx.place(market.slug, "Up", "BUY", 0.48, 10)

    res = engine.run_coin(cfg(), "btc", lambda: make_con([M1], T0 - 1_200_000, T0 + 500_000),
                          loader=loader_for({M1.slug: md}), strategy=Bid(), rec_events=NO_EVENTS)
    f = res.fills
    assert len(f) == 1 and math.isnan(f.mid_10s.iloc[0]) and f.mid_at_fill.iloc[0] == pytest.approx(0.5)


def test_fair_value_uses_computed_price_to_beat_when_official_is_none():
    """Like a 4h market: no official price_to_beat; the engine's computed (revealed) one is used."""
    m = minfo(T0, price_to_beat=None)

    class FV(Strategy):
        name = "fv-none-test"

        def __init__(self):
            super().__init__({})
            self.vals: list[tuple[int, float | None, float | None]] = []

        def on_chainlink(self, ctx, coin, price):
            for x in ctx.markets():
                self.vals.append((ctx.now, x.price_to_beat, ctx.fair_value(x.slug)))

    s = FV()
    engine.run_coin(cfg(), "btc", lambda: make_con([m], T0 - 800_000, T0 + 500_000),
                    loader=loader_for({m.slug: mdata(m)}), strategy=s, rec_events=NO_EVENTS)
    late = [(k, v) for t, k, v in s.vals if T0 + 105_000 < t < m.window_end_ms]
    assert late and all(k is not None and v is not None and 0 < v < 1 for k, v in late)
    assert all(v is None for t, k, v in s.vals if k is None)


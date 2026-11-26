"""Live driver (`bt.live`) vs session-mode replay (`engine.run_session`) over the same synthetic rows.

`run_both` runs the live side (`LiveRouter.ingest` each row at its recv_ms, `tick(wall)` every 20 ms of virtual wall
time, `request_stop`, the remaining rows and resolutions, `finish()`), writes the rows the router accepted (plus the
settler's `pm_resolution` rows) as a recorder store, builds it, and replays it with `run_session` over the router's
admitted markets. Parity: decisions, orders, fills, equity and markets[MARKET_COLS] `DataFrame.equals` per budget,
and the same log lines.
"""

from __future__ import annotations

from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import pytest
import yaml
from synth import make_session, meta_row, resolution_row, slug_of, sort_rows, write_store

from polydata.bt.config import RunConfig
from polydata.bt.core import MARKET_COLS, Core, _Bn, _Grid
from polydata.bt.engine import SessionPlan, run_session
from polydata.bt.exchange import Exchange
from polydata.bt.live import LiveRouter, LiveRow, Resolution, is_late_candidate
from polydata.bt.marketdata_live import GrowingMarketData
from polydata.bt.strategy import Strategy, register

H11 = 1_795_518_000_000  # 2026-11-24 11:00 UTC
H12 = H11 + 3_600_000
CONFIGS = Path(__file__).resolve().parents[2] / "configs"


# ---------------------------------------------------------------------------------------- strategies

@register
class ParityProbe(Strategy):
    """Uses every ctx query and trades a little, from ctx only: timers at absolute times (`at`), a periodic check,
    a post-only bid at the best bid and a small take now and then."""
    name = "parity-probe"
    family = "test"
    default_params = {"at": [], "every_ms": 700, "take_every": 7}

    def __init__(self, params=None):
        super().__init__(params)
        self.n = 0
        self.bid: dict[str, int] = {}

    def on_start(self, ctx):
        ctx.log("start", ctx.markets(), ctx.cash())
        for t in self.params["at"]:
            ctx.set_timer(f"at:{t}", t - ctx.now)
        ctx.set_timer("chk", self.params["every_ms"])

    def on_market_open(self, ctx, market):
        ctx.log("open", market.slug, market.price_to_beat, ctx.fair_value(market.slug))

    def on_book(self, ctx, market, book):
        ctx.fair_value(market.slug)
        ctx.top(market.slug)

    def on_trade(self, ctx, market, trade):
        ctx.position(market.slug)

    def on_chainlink(self, ctx, coin, price):
        ctx.chainlink(coin)

    def on_order(self, ctx, order):
        ctx.log("order", order.id, order.event, order.status, order.filled, order.last_fill)
        if not order.is_open and self.bid.get(order.slug) == order.id:
            del self.bid[order.slug]

    def on_market_close(self, ctx, market):
        ctx.log("close", market.slug, ctx.position(market.slug))

    def on_timer(self, ctx, name):
        if name.startswith("at:"):
            ctx.log(name, [(m.slug, m.price_to_beat) for m in ctx.markets()])
            ctx.set_timer("again:" + name, 0)  # after anything keyed after the timer (a ptb reveal at this ms)
            return
        if name.startswith("again:"):
            ctx.log(name, [(m.slug, m.price_to_beat, ctx.fair_value(m.slug)) for m in ctx.markets()])
            return
        self.n += 1
        for m in sorted(ctx.markets(), key=lambda m: m.slug):
            ctx.fair_value(m.slug)
            ctx.binance(f"{m.coin.upper()}USDT")
            ctx.chainlink(m.coin)
            book = ctx.book(m.slug)
            top = ctx.top(m.slug)
            if top is None or not 0 < top.bid_px < top.ask_px < 1000:
                continue
            if m.window_end_ms - ctx.now < 20_000:
                continue
            oid = self.bid.get(m.slug)
            if oid is None:
                self.bid[m.slug] = ctx.place(m.slug, "Up", "BUY", top.bid_px / 1000, 5)
            elif self.n % 3 == 0:
                ctx.cancel(oid)
            if self.n % self.params["take_every"] == 0 and book is not None and book.best_ask is not None:
                ctx.take(m.slug, "Down", "BUY", 5, min(0.99, 1 - top.bid_px / 1000 + 0.02))
        ctx.set_timer("chk", self.params["every_ms"])


@register
class CapProbe(ParityProbe):
    """`parity-probe` that logs, at every close, the Up top of each market still open (a sibling's close sees them)."""
    name = "cap-probe"

    def on_market_close(self, ctx, market):
        ctx.log("close-top", market.slug, [(m.slug, ctx.top(m.slug)) for m in ctx.markets() if m.slug != market.slug])


@register
class ParityProbeBn(ParityProbe):
    """`parity-probe` plus `on_binance`."""
    name = "parity-probe-bn"

    def on_binance(self, ctx, sym, bbo):
        if bbo.recv_ms % 97 == 0:
            ctx.binance(sym)
            for m in ctx.markets():
                ctx.fair_value(m.slug)


@register
class RestOneBid(Strategy):
    """Rests one post-only Up bid per market at open and never touches it again; watches Chainlink. Nothing but newly
    released trades can then advance its exchange (`Core.on_trades_appended`)."""
    name = "rest-one-bid"
    family = "test"

    def on_book(self, ctx, market, book):
        if market.slug not in self.__dict__.setdefault("placed", set()):
            top = ctx.top(market.slug)
            if top is not None and 0 < top.bid_px < top.ask_px < 1000 and market.window_end_ms - ctx.now > 60_000:
                self.placed.add(market.slug)
                ctx.place(market.slug, "Up", "BUY", top.bid_px / 1000, 5)

    def on_chainlink(self, ctx, coin, price):
        for m in ctx.markets():
            ctx.position(m.slug)

    def on_order(self, ctx, order):
        ctx.log("order", order.id, order.event, order.filled)


class StubSettler:
    """Task 8's Chainlink settler, minimal: outcome Up iff mean(ticks in [we-60s, we)) >= mean(ticks in
    [ws-60s, ws)), on the first kept tick with ts >= window end."""

    def __init__(self, rs: int):
        self.rs, self.mk, self.ticks = rs, {}, {}

    def admit(self, info, admitted_ms):
        self.mk[info.slug] = info

    def on_tick(self, coin, recv_ms, ts_ms, price):
        self.ticks.setdefault(coin, []).append((ts_ms, price))
        out = []
        for slug, m in list(self.mk.items()):
            if m.coin != coin or ts_ms < m.window_end_ms:
                continue
            del self.mk[slug]
            ws, we = m.window_start_ms, m.window_end_ms
            a = [p for t, p in self.ticks[coin] if ws - 60_000 <= t < ws]
            b = [p for t, p in self.ticks[coin] if we - 60_000 <= t < we]
            if ws - 60_000 < self.rs or not a or not b:
                continue
            ptb, tw = float(np.mean(a)), float(np.mean(b))
            out.append(Resolution(slug, "Up" if tw >= ptb else "Down", recv_ms, "paper-chainlink", ptb, tw))
        return out


# ---------------------------------------------------------------------------------------- harness

def cfg_of(strategy: str, coins=("btc",), timeframes=("5m",), **kw) -> RunConfig:
    return RunConfig(strategy=strategy, coins=list(coins), timeframes=list(timeframes), **kw)


def run_live(rows, cfg, rs, stop_ms, *, tick_ms=20, on_tick=None, hard_stop_ms=None, **router_kw):
    """The live side. Returns (router, RunResult, rows for the session store). `hard_stop_ms`: finish as soon as
    T reaches it (after the stop request), whatever is still pending."""
    router = LiveRouter(cfg, rs, **router_kw)
    ok, i, n = [], 0, len(rows)
    wall = rows[0].recv_ms
    last = rows[-1].recv_ms
    while True:
        while i < n and rows[i].recv_ms <= wall:
            if router.ingest(rows[i]) == "ok":
                ok.append(rows[i])
            i += 1
        router.tick(wall)
        if on_tick is not None:
            on_tick(router, wall)
        if router.end_ms is None and router.T >= stop_ms:
            router.request_stop(router.T)
        if router.end_ms is not None and i >= n and router.T >= max(router.end_ms, last):
            break
        if hard_stop_ms is not None and router.end_ms is not None and router.T >= hard_stop_ms:
            break
        wall += tick_ms
    res = [LiveRow("pm_resolution", r.recv_ms, {"slug": r.slug, "outcome": r.outcome, "source": r.source,
                                                 "event": {"slug": r.slug, "eventMetadata": {
                                                     "priceToBeat": r.ptb, "finalPrice": r.close_twap}}})
           for r in router.new_resolutions]
    return router, router.finish(), sort_rows(ok + res)


def run_replay(store_rows, cfg, router, root: Path, monkeypatch):
    from polydata.build import build
    from polydata.views import create_views

    write_store(store_rows, root)
    monkeypatch.setenv("POLYDATA_DIR", str(root))
    build()

    def con_factory():
        con = duckdb.connect()
        con.execute("SET TimeZone = 'UTC'")
        create_views(con, parquet=root / "parquet", include_sealed=True)
        return con

    plan = SessionPlan(router.rs, router.end_ms, [(a["slug"], a["admitted_ms"]) for a in router.admitted])
    return run_session(cfg, con_factory, plan)


def _diff(name, a: pd.DataFrame, b: pd.DataFrame) -> str:
    n = min(len(a), len(b))
    for i in range(n):
        x, y = a.iloc[i], b.iloc[i]
        if not all((u == v) or (pd.isna(u) and pd.isna(v)) for u, v in zip(x.tolist(), y.tolist())):
            lo = max(0, i - 5)
            return f"{name}: first difference at row {i}\nlive:\n{a.iloc[lo:i + 1]}\nreplay:\n{b.iloc[lo:i + 1]}"
    return f"{name}: lengths {len(a)} vs {len(b)}; dtypes {dict(a.dtypes)} vs {dict(b.dtypes)}"


def assert_parity(live, rep) -> None:
    assert list(live.coins) == list(rep.coins)
    for k in live.coins:
        a, b = live.coins[k], rep.coins[k]
        # decisions first: the first divergence names the event
        for name in ("decisions", "orders", "fills", "equity"):
            x, y = getattr(a, name), getattr(b, name)
            assert x.equals(y), f"budget {k}: " + _diff(name, x, y)
        x, y = a.markets[MARKET_COLS], b.markets[MARKET_COLS]
        assert x.equals(y), f"budget {k}: " + _diff("markets", x, y)
        assert a.log == b.log, f"budget {k}: logs differ"


def run_both(rows, cfg, rs, stop_ms, tmp_path, monkeypatch, **kw):
    router, live, store = run_live(rows, cfg, rs, stop_ms, **kw)
    rep = run_replay(store, cfg, router, tmp_path / "session", monkeypatch)
    assert_parity(live, rep)
    return router, live, rep, store


# ---------------------------------------------------------------------------------------- unit

def _cfg(**kw):
    return cfg_of("noop", **kw)


def test_ingest_classification():
    r = LiveRouter(_cfg(), H11, hold_back_ms=3000, skew_ms=1000)
    assert r.hold_back_ms == 3000
    assert LiveRouter(_cfg(), H11).hold_back_ms == 10_000  # controller ruling
    slug = slug_of("btc", "5m", H11)
    pc = lambda recv, ts: LiveRow("pm_price_change", recv, (str(ts), f"{slug}|U", "BUY", "0.50", "10", "0.50", "0.51"))
    assert r.ingest(meta_row("btc", "5m", H11, H11 - 60_000)) == "ok"
    assert r.ingest(pc(H11 - 50_000, H11 - 50_000 + 1000)) == "ok"  # Polymarket clock ahead by exactly the skew
    assert r.ingest(pc(H11 - 50_000, H11 - 50_000 + 1001)) == "future"
    r.tick(H11 - 40_000)  # H = H11 - 43000
    assert r.H_ts == H11 - 43_000 and r.T == H11 - 44_000
    # ts <= H: late; the market has not opened (no snapshot yet), so the row is accepted (fix round 1 ruling A)
    assert r.ingest(pc(H11 - 39_000, H11 - 43_000)) == "ok"
    assert r.ingest(pc(H11 - 39_000, H11 - 42_999)) == "ok"
    tick = LiveRow("pm_other", H11 - 38_000, {"event_type": "tick_size_change", "market": f"0xbtc5m{H11 // 1000}",
                                              "new_tick_size": "0.001", "timestamp": str(H11 - 44_000)})
    assert is_late_candidate(tick) and r.ingest(tick) == "ok"
    other = LiveRow("pm_other", H11 - 38_000, {"event_type": "market_resolved", "timestamp": "1"})
    assert not is_late_candidate(other) and r.ingest(other) == "ok"
    bbo = LiveRow("pm_best_bid_ask", H11 - 38_000, (str(H11 - 44_000), f"{slug}|U", "0.5", "0.51", "0.01"))
    assert is_late_candidate(bbo) and r.ingest(bbo) == "ok"
    # a late row of a market this run cannot admit (discovered with its window before rs) is still dropped
    old_slug = slug_of("btc", "5m", H11 - 600_000)
    r.ingest(meta_row("btc", "5m", H11 - 600_000, H11 - 38_000))
    assert r.ingest(LiveRow("pm_price_change", H11 - 38_000, (str(H11 - 44_000), f"{old_slug}|U", "BUY", "0.5", "1",
                                                              "0.5", "0.51"))) == "late"
    assert r.stats["late"] == 1 and r.stats["late_accepted"] == 3 and r.stats["future"] == 1
    assert [x[1] for x in r.late] == [1, 1000]  # future: beyond recv + skew; late: behind H
    with pytest.raises(ValueError, match="went back"):
        r.ingest(pc(H11 - 38_001, H11 - 38_000))
    # a Chainlink row received after the released horizon is used, never late
    cl = lambda recv: LiveRow("rtds_price", recv, ("crypto_prices_chainlink", "btc/usd", "1", "1", "1", "1", "a"))
    assert r.ingest(cl(H11 - 38_000)) == "ok"
    # unknown market (no discovery before its rows): kept, never admitted
    assert r.ingest(LiveRow("pm_trade", H11 - 38_000, (str(H11 - 37_000), "btc-updown-5m-1|U", "BUY", "0.5", "1", "0",
                                                       "x"))) == "ok"
    assert r.stats["orphan_rows"] == 1
    # stamped at or behind the released horizon: a Polymarket exchange-clock row is late; any other row is a
    # stamping bug and raises (Binance and Chainlink rows are never late)
    r2 = LiveRouter(_cfg(), H11, hold_back_ms=3000)
    r2.tick(H11)  # H = H11 - 3000
    assert r2.ingest(pc(H11 - 3000, H11 - 3500)) == "late"
    for row in (cl(H11 - 3000), LiveRow("bn_fut_bbo", H11 - 3000, ("BTCUSDT", "1", "1", "1", "1", "1", "1", "1")),
                meta_row("btc", "5m", H11, H11 - 3000)):
        with pytest.raises(ValueError, match="released horizon"):
            r2.ingest(row)
    assert r2.ingest(cl(H11 - 2999)) == "ok"
    with pytest.raises(ValueError, match="hold_back_ms"):
        LiveRouter(_cfg(), H11, hold_back_ms=0)


def test_router_rejects_small_skew():
    with pytest.raises(ValueError, match="trade_explain_ms"):
        LiveRouter(_cfg(fill_report_latency_ms=100), H11, skew_ms=49)
    LiveRouter(_cfg(fill_report_latency_ms=100), H11, skew_ms=50)


def test_request_stop_end_ms():
    rows = make_session(3, H11 - 10 * 60_000, 12, bn_ms=50)
    rs = H11 - 9 * 60_000
    router, res, _ = run_live(rows, _cfg(), rs, H11 + 30_000 + 7, hold_back_ms=3000)
    assert router.end_ms % 1000 == 0 and router.end_ms >= H11 + 30_000
    assert router.end_ms == H11 + 5 * 60_000  # the last admitted window end (the stop came before it)
    # both discovered before rs: admitted at rs, in the order their discovery rows were received
    metas = [r.values["event"]["slug"] for r in rows if r.stream == "pm_market_meta"]
    want = [slug_of("btc", "5m", H11 - 5 * 60_000), slug_of("btc", "5m", H11)]
    assert [a["slug"] for a in router.admitted] == [m for m in metas if m in want]
    assert [a["admitted_ms"] for a in router.admitted] == [rs, rs]
    assert all(a["budget"] == "btc" and a["load_index"] == i for i, a in enumerate(router.admitted))
    assert router.stopping and router.unresolved() == [want[1]]  # the session ends before its resolution
    assert res.coins["btc"].markets.set_index("slug").loc[want[1], "reason"] == "no outcome at settlement"


# ---------------------------------------------------------------------------------------- parity

def _assert_active(res, fills=True, decisions=100):
    d = res.decisions
    assert len(d) > decisions
    assert (d.call == "on_market_open").sum() >= 2
    if fills:
        assert len(res.fills) > 0
    assert len(res.markets) >= 2


def test_parity_noop(tmp_path, monkeypatch):
    rows = make_session(1, H11 - 20 * 60_000, 30, bn_ms=10)
    rs = H11 - 19 * 60_000
    router, live, rep, _ = run_both(rows, cfg_of("noop"), rs, H11 + 5 * 60_000, tmp_path, monkeypatch)
    m = live.markets
    assert len(m) >= 4 and not m.voided.any() and (m.pnl == 0).all()
    assert len(live.equity) >= 2


def test_parity_join_best(tmp_path, monkeypatch):
    rows = make_session(2, H11 - 25 * 60_000, 32, bn_ms=10)
    rs = H11 - 24 * 60_000
    router, live, rep, _ = run_both(rows, cfg_of("join-best"), rs, H11 + 2 * 60_000, tmp_path, monkeypatch)
    _assert_active(live)
    assert (live.fills.liquidity == "maker").any()


def test_parity_always_cross_with_settler(tmp_path, monkeypatch):
    rs = H11 - 24 * 60_000
    rows = make_session(4, H11 - 25 * 60_000, 32, bn_ms=10, resolutions=False)
    router, live, rep, store = run_both(rows, cfg_of("always-cross"), rs, H11 + 60_000, tmp_path, monkeypatch,
                                        settler=StubSettler(rs))
    _assert_active(live, decisions=30)
    assert router.new_resolutions and all(r.source == "paper-chainlink" for r in router.new_resolutions)
    m = live.markets
    assert m.settle_ms.notna().sum() >= 3  # settled through the settler's resolutions


def test_parity_endgame_maker(tmp_path, monkeypatch):
    params = yaml.safe_load((CONFIGS / "validation" / "endgame-maker-v2.yaml").read_text())["params"]
    cfg = cfg_of("endgame-maker", coins=("btc", "eth"), params=params)
    rows = make_session(5, H11 - 25 * 60_000, 40, coins=("btc", "eth"), bn_ms=10)
    router, live, rep, _ = run_both(rows, cfg, H11 - 24 * 60_000, H11 + 10 * 60_000, tmp_path, monkeypatch)
    assert set(live.coins) == {"btc", "eth"}
    assert (live.decisions.call == "place").sum() > 0
    assert len(live.orders) > 0


def test_parity_across_hour_boundary(tmp_path, monkeypatch):
    """Review Focus 4: data from 11:58, a timer at exactly 12:00:00.000, `on_binance`, ticks straddling 12:00, rows
    received at 12:00:00.000 exactly, a market starting at 12:00 whose ptb is revealed by a tick received at 12:00."""
    start = H12 - 2 * 60_000
    rows = make_session(6, start, 12, bn_ms=3)
    slug = slug_of("btc", "5m", H12)
    extra = [
        LiveRow("rtds_price", H12, ("crypto_prices_chainlink", "btc/usd", str(H12 - 1), "60000.5", "60000.5", "0", "a")),
        LiveRow("pm_trade", H12, (str(H12 - 150), f"{slug}|U", "BUY", "0.55", "3", "0", "0xhb")),
    ]
    rows = sort_rows([r for r in rows if not (r.stream == "rtds_price" and r.values[2] == str(H12))] + extra
                     + [LiveRow("rtds_price", H12, ("crypto_prices_chainlink", "btc/usd", str(H12), "60001", "60001",
                                                    "0", "a"))])
    rs = start + 30_000
    cfg = cfg_of("parity-probe-bn", params={"at": [H12, H12 + 1, H12 + 5 * 60_000]})
    router, live, rep, _ = run_both(rows, cfg, rs, H12 + 6 * 60_000, tmp_path, monkeypatch, tick_ms=20)
    d = live.decisions
    assert (d.call == "on_binance").sum() > 1000
    at = d[(d.call == "on_timer") & (d.key == f"at:{H12}")]
    assert list(at.now) == [H12]
    assert any(f"at:{H12}" in s and "None" in s for s in live.log)  # before the reveal keyed after the timer
    assert any(f"again:at:{H12}" in s and "60000" in s for s in live.log)  # the reveal at 12:00:00.000
    on12 = d[(d.now == H12) & d.call.str.startswith("on_")]
    assert {"on_trade", "on_chainlink", "on_timer"} <= set(on12.call)


def test_parity_per_coin_and_global(tmp_path, monkeypatch):
    rows = make_session(7, H11 - 22 * 60_000, 30, coins=("btc", "sol"), bn_ms=10)
    rs = H11 - 21 * 60_000
    for mode in ("per_coin", "global"):
        cfg = cfg_of("parity-probe", coins=("btc", "sol"), capital_mode=mode)
        router, live, rep, _ = run_both(rows, cfg, rs, H11 + 3 * 60_000, tmp_path / mode, monkeypatch)
        assert list(live.coins) == (["btc", "sol"] if mode == "per_coin" else ["*"])
        assert set(live.markets.coin) == {"btc", "sol"}
        assert len(live.fills) > 0


def test_parity_mixed_timeframes(tmp_path, monkeypatch):
    """5m and 15m markets in one session, discovered long before they start: each market's rows start before its own
    row window (Task 4 obligation 7: the session replay loads each market with its own window)."""
    rows = make_session(8, H11 - 40 * 60_000, 50, timeframes=("5m", "15m"), bn_ms=20, meta_lead_ms=35 * 60_000)
    rs = H11 - 39 * 60_000
    router, live, rep, _ = run_both(rows, cfg_of("join-best", timeframes=("5m", "15m")), rs, H11 + 5 * 60_000,
                                    tmp_path, monkeypatch)
    assert set(live.markets.timeframe) == {"5m", "15m"}
    _assert_active(live)


def test_late_row_dropped(tmp_path, monkeypatch):
    rows = make_session(9, H11 - 20 * 60_000, 30, bn_ms=10)
    rs = H11 - 19 * 60_000
    slug = slug_of("btc", "5m", H11 - 10 * 60_000)
    i = next(i for i, r in enumerate(rows) if r.stream == "pm_price_change" and r.values[1] == f"{slug}|U"
             and int(r.values[0]) > H11 - 8 * 60_000)
    late = rows[i]
    late = late._replace(recv_ms=int(late.values[0]) + 3000 + 500)  # delayed beyond the 3 s hold-back
    rows = sort_rows(rows[:i] + rows[i + 1:] + [late])
    router, live, rep, store = run_both(rows, cfg_of("join-best"), rs, H11 + 3 * 60_000, tmp_path, monkeypatch,
                                        hold_back_ms=3000)
    assert [x[0] for x in router.late] == [late] and router.stats["late"] == 1
    assert 0 < router.late[0][1] <= 500
    assert late not in store
    _assert_active(live)


def test_future_stamped_row_dropped(tmp_path, monkeypatch):
    """Review Focus 2: a row stamped beyond recv + skew is dropped (never reaches either engine); one stamped exactly
    at recv + skew is used."""
    rows = make_session(10, H11 - 20 * 60_000, 30, bn_ms=10)
    rs = H11 - 19 * 60_000
    slug = slug_of("btc", "5m", H11 - 10 * 60_000)
    cand = [i for i, r in enumerate(rows) if r.stream == "pm_price_change" and r.values[1] == f"{slug}|U"
            and int(r.values[0]) > H11 - 8 * 60_000]
    i, j = cand[0], cand[40]
    fut = rows[i]._replace(values=(str(rows[i].recv_ms + 1001),) + rows[i].values[1:])
    edge = rows[j]._replace(values=(str(rows[j].recv_ms + 1000),) + rows[j].values[1:])
    rows = [fut if k == i else edge if k == j else r for k, r in enumerate(rows)]
    router, live, rep, store = run_both(rows, cfg_of("join-best"), rs, H11 + 3 * 60_000, tmp_path, monkeypatch)
    assert [x[0] for x in router.late] == [fut] and router.stats["future"] == 1 and router.late[0][1] == 1
    assert fut not in store and edge in store
    _assert_active(live)


def test_ptb_reveal_admitted_late(tmp_path, monkeypatch):
    """Review Focus 3: the market is discovered after its window start and after the Chainlink tick that reveals its
    price to beat. Both sides reveal it at admission; fair_value turns non-None at the same `now`."""
    start = H11 - 25 * 60_000
    rows = make_session(11, start, 35, bn_ms=10)
    ws = H11
    slug = slug_of("btc", "5m", ws)
    meta = next(r for r in rows if r.stream == "pm_market_meta" and r.values["event"]["slug"] == slug)
    reveal = min(r.recv_ms for r in rows if r.stream == "rtds_price" and int(r.values[2]) >= ws)
    late_meta = meta._replace(recv_ms=ws + 25_000)
    assert late_meta.recv_ms > reveal > ws
    # its rows before the late discovery would make it an orphan: the market's subscription starts with it
    rows = sort_rows([r for r in rows if r is not meta and not (
        r.stream.startswith("pm_") and isinstance(r.values, tuple) and r.values[1].startswith(slug)
        and r.recv_ms <= late_meta.recv_ms)] + [late_meta])
    rs = start + 60_000
    cfg = cfg_of("parity-probe", params={"every_ms": 250})
    router, live, rep, _ = run_both(rows, cfg, rs, H11 + 6 * 60_000, tmp_path, monkeypatch)
    adm = next(a for a in router.admitted if a["slug"] == slug)
    assert adm["admitted_ms"] == ws + 25_000
    for res in (live, rep):
        d = res.decisions
        fv = d[(d.call == "fair_value") & (d.key == slug)]
        first = fv[fv.ret != "None"].now.min()
        assert first >= adm["admitted_ms"]
        assert not np.isnan(first)
    a, b = live.decisions, rep.decisions
    fa = a[(a.call == "fair_value") & (a.key == slug) & (a.ret != "None")].now.min()
    fb = b[(b.call == "fair_value") & (b.key == slug) & (b.ret != "None")].now.min()
    assert fa == fb
    assert any(s.split()[1:3] == ["open", slug] and s.split()[3] != "None" for s in live.log)  # ptb known at open


def test_resolution_after_end_and_never(tmp_path, monkeypatch):
    """Review Focus 5: one market's pm_resolution is received after end_ms, another market never gets one."""
    start = H11 - 20 * 60_000
    rows = make_session(12, start, 26, bn_ms=10, meta_lead_ms=4 * 60_000)
    last = slug_of("btc", "5m", H11)
    never = slug_of("btc", "5m", H11 - 5 * 60_000)
    res = {r.values["slug"]: r for r in rows if r.stream == "pm_resolution"}
    rows = [r for r in rows if not (r.stream == "pm_resolution" and r.values["slug"] in (last, never))]
    rows = sort_rows(rows + [res[last]._replace(recv_ms=H11 + 5 * 60_000 + 90_000)])
    cfg = cfg_of("buy-and-hold-up")
    router, live, rep, _ = run_both(rows, cfg, start + 60_000, H11 + 30_000, tmp_path, monkeypatch)
    assert router.end_ms == H11 + 5 * 60_000
    m = live.markets.set_index("slug")
    assert m.loc[never, "voided"] and m.loc[never, "reason"] == "no outcome at settlement"
    assert not m.loc[last, "voided"] and m.loc[last, "settle_ms"] > router.end_ms
    assert m.loc[last, "outcome"] == res[last].values["outcome"]
    assert never in router.unresolved() and last not in router.unresolved()


def test_hold_back_invariant(tmp_path, monkeypatch):
    """Every core query during the live run is at <= T (strategy clock and exchange clock), every exchange advance
    is at <= T - R < H_ts; and skew_ms + R < 150 is refused."""
    with pytest.raises(ValueError):
        LiveRouter(cfg_of("noop", fill_report_latency_ms=100), H11, skew_ms=40)
    rows = make_session(13, H11 - 20 * 60_000, 28, bn_ms=10)
    rs = H11 - 19 * 60_000
    cfg = cfg_of("parity-probe", params={"every_ms": 300, "take_every": 2})
    state = {"router": None, "n": 0, "adv": 0}
    R = cfg.fill_report_latency_ms

    def check(t, limit, what):
        r = state["router"]
        if r is not None and r._in_tick:
            state["n"] += 1
            assert t <= limit(r), (what, t, r.T, r.H_ts)

    def wrap(cls, name, clock_arg, limit):
        orig = getattr(cls, name)

        def f(self, *a, **k):
            check(clock_arg(a, k), limit, name)
            return orig(self, *a, **k)
        monkeypatch.setattr(cls, name, f)

    T = lambda r: r.T
    wrap(GrowingMarketData, "top_at", lambda a, k: a[0], T)
    wrap(GrowingMarketData, "book_at", lambda a, k: a[0], T)
    wrap(GrowingMarketData, "size_at", lambda a, k: a[2], T)
    wrap(GrowingMarketData, "tick_at", lambda a, k: a[0], T)
    wrap(GrowingMarketData, "level_changes", lambda a, k: a[3], T)
    wrap(Core, "_bn_find", lambda a, k: a[1], T)
    wrap(Core, "cl_tick", lambda a, k: a[1], T)
    wrap(Exchange, "advance", lambda a, k: a[0], T)  # the close advances to the window end (<= T)
    orig_adv = Core._advance

    def adv(self, t):
        r = state["router"]
        if r is not None and r._in_tick:
            state["adv"] += 1
            assert t <= r.T - R < r.H_ts
        return orig_adv(self, t)
    monkeypatch.setattr(Core, "_advance", adv)

    orig_tick = LiveRouter.tick

    def tick(self, wall):
        state["router"] = self
        self._in_tick = True
        try:
            orig_tick(self, wall)
        finally:
            self._in_tick = False
    monkeypatch.setattr(LiveRouter, "tick", tick)
    router, live, store = run_live(rows, cfg, rs, H11 + 2 * 60_000)
    state["router"] = None
    assert state["n"] > 1000 and state["adv"] > 10
    assert len(live.fills) > 0
    monkeypatch.undo()
    rep = run_replay(store, cfg, router, tmp_path / "session", monkeypatch)
    assert_parity(live, rep)


def test_binance_lookups_complete_to_horizon(tmp_path, monkeypatch):
    """Binance silent for 4 s. At every (7th) tick, the core's Binance mid at every 10 ms of (T - 1 s, H_ts] (beyond T:
    where a fill's ts_ms could look) and the grid's sig15 up to T equal those of the replay's whole-hour bundles:
    the bucket in progress before the silence is visible as soon as the horizon passes its last ms."""
    start = H11 - 20 * 60_000
    silence = (H11 - 4 * 60_000 + 3, H11 - 4 * 60_000 + 4_003)
    rows = make_session(14, start, 26, bn_ms=3, bn_silence=silence)
    rs = start + 60_000
    cfg = cfg_of("parity-probe", params={"every_ms": 200, "take_every": 2})
    # the replay's bundles (_load_bn): last by (recv, update_id) per 10 ms bucket, delivered at the bucket's last ms
    best = {}
    for r in rows:
        if r.stream == "bn_fut_bbo" and r.recv_ms >= rs:
            b = r.recv_ms // 10
            key = (r.recv_ms, int(r.values[1]))
            if b not in best or key >= best[b][0]:
                best[b] = (key, float(r.values[4]), float(r.values[6]))
    ref_recv = np.array([b * 10 + 9 for b in sorted(best)], np.int64)
    ref_mid = np.array([(best[b][1] + best[b][2]) / 2 for b in sorted(best)])
    ref = _Grid()
    for s, e in ((rs, H11), (H11, H12)):
        sel = (ref_recv >= s) & (ref_recv < e)
        ref.append(s // 1000, e // 1000, _Bn(ref_recv[sel], ref_mid[sel], ref_mid[sel], ref_mid[sel], ref_mid[sel]))
    seen = {"n": 0, "beyond_T": 0, "silent": 0}

    def on_tick(router, wall):
        if not router._started or router.end_ms is not None or wall % 7:
            return
        core = router.sources["btc"].core
        hour = router.T // 3_600_000 * 3_600_000
        for t in range(router.T - 1000, min(router.H_ts, hour + 3_599_999) + 1, 10):
            i = int(np.searchsorted(ref_recv, t, side="right")) - 1
            want = float(ref_mid[i]) if i >= 0 and ref_recv[i] >= hour - 3_600_000 else None
            assert core._bn_mid("BTCUSDT", t) == want, (t, router.T, router.H_ts)
            seen["n"] += 1
            seen["beyond_T"] += t > router.T
            seen["silent"] += silence[0] <= t < silence[1]
        for t in range(router.T - 2000, router.T + 1, 100):
            assert core.grid["BTCUSDT"].sig15(t) == ref.sig15(t), t

    router, live, rep, _ = run_both(rows, cfg, rs, H11 + 2 * 60_000, tmp_path, monkeypatch, on_tick=on_tick)
    assert seen["n"] > 10_000 and seen["beyond_T"] > 5_000 and seen["silent"] > 100
    assert len(live.fills) > 0


def test_trades_released_after_a_resting_order(tmp_path, monkeypatch):
    """A resting order and no other activity: the fill comes from trades released (ts side) after the order rests.
    Without lowering next_ex when trades are appended, live would advance the exchange too late."""
    rows = make_session(15, H11 - 20 * 60_000, 30, bn_ms=20)
    router, live, rep, _ = run_both(rows, cfg_of("rest-one-bid"), H11 - 19 * 60_000, H11 + 3 * 60_000, tmp_path,
                                    monkeypatch)
    assert len(live.fills) > 0 and (live.fills.liquidity == "maker").all()


def test_resolution_before_admission_ignored(tmp_path, monkeypatch):
    """A resolution received before its market's admission counts on neither side; one received exactly at the
    admission time counts on both, even when its row arrives before the discovery row."""
    start = H11 - 20 * 60_000
    rows = make_session(16, start, 32, bn_ms=10)
    rs = start + 60_000
    a_slug, b_slug = slug_of("btc", "5m", H11), slug_of("btc", "5m", H11 - 5 * 60_000)
    meta = {r.values["event"]["slug"]: r for r in rows if r.stream == "pm_market_meta"}
    a_meta = meta[a_slug]._replace(recv_ms=H11 + 25_000)  # A discovered after its start
    b_adm = meta[b_slug].recv_ms
    assert b_adm > rs
    rows = [r for r in rows if r is not meta[a_slug] and not (r.stream == "pm_resolution"
            and r.values["slug"] in (a_slug, b_slug)) and not (
        r.stream.startswith("pm_") and isinstance(r.values, tuple) and r.values[1].startswith(a_slug)
        and r.recv_ms <= a_meta.recv_ms)]
    i = rows.index(meta[b_slug])
    rows = rows[:i] + [resolution_row("btc", "5m", H11 - 5 * 60_000, b_adm, "Up")] + rows[i:]  # same recv, first
    rows = sort_rows(rows + [a_meta, resolution_row("btc", "5m", H11, H11 + 10_000, "Down")])
    router, live, rep, _ = run_both(rows, cfg_of("buy-and-hold-up"), rs, H11 + 60_000, tmp_path, monkeypatch)
    adm = {a["slug"]: a["admitted_ms"] for a in router.admitted}
    assert adm[a_slug] == H11 + 25_000 and adm[b_slug] == b_adm
    m = live.markets.set_index("slug")
    assert m.loc[a_slug, "reason"] == "no outcome at settlement" and m.loc[a_slug, "outcome"] is None
    assert not m.loc[b_slug, "voided"] and m.loc[b_slug, "outcome"] == "Up"
    assert router.stats["ignored_resolutions"] >= 1
    assert a_slug in router.unresolved() and b_slug not in router.unresolved()


def test_hard_stop_is_a_prefix(tmp_path, monkeypatch):
    """finish() before T reaches end_ms: nothing past T is processed (no drain, close or settle beyond it),
    clean_stop is False, and the run is the prefix of the session replay's (decisions and orders)."""
    start = H11 - 20 * 60_000
    rows = make_session(17, start, 30, bn_ms=10)
    rs = start + 60_000
    cfg = cfg_of("parity-probe", params={"every_ms": 300, "take_every": 2})
    stop = H11 + 60_000
    router, live, store = run_live(rows, cfg, rs, stop, hard_stop_ms=stop + 30_000)
    T = router.T
    assert router.end_ms - 1 > T and router.clean_stop is False
    r = live.coins["btc"]
    assert r.stats["clean_stop"] is False and r.stats["flushed_ms"] == T
    assert (r.decisions.now <= T).all() and (r.equity.ms <= T).all() and (r.orders.ms <= T).all()
    closes = set(r.decisions[r.decisions.call == "on_market_close"].key)
    m = r.markets.set_index("slug")
    for slug, row in m.iterrows():
        if row.window_end_ms > T:
            assert slug not in closes and np.isnan(row.settle_ms)
    assert any(row.window_end_ms > T for _, row in m.iterrows())
    rep = run_replay(store, cfg, router, tmp_path / "session", monkeypatch)
    b = rep.coins["btc"]
    for name in ("decisions", "orders"):
        x, y = getattr(r, name), getattr(b, name)
        assert len(y) > len(x) > 100
        assert x.equals(y.iloc[:len(x)].reset_index(drop=True)), _diff(name, x, y)


def test_no_row_received_at_end_ms_is_visible(tmp_path, monkeypatch):
    """A Polymarket row received exactly at end_ms (midnight into a sealed day) is never visible, even to the close of a
    sibling market at end_ms; the session replay sees the same (recv_cap on the market data)."""
    E = H11
    rows = make_session(21, E - 20 * 60_000, 22, timeframes=("5m", "15m"), bn_ms=20, meta_lead_ms=25 * 60_000)
    b = slug_of("btc", "15m", E - 900_000)
    dn, up = f"{b}|D", f"{b}|U"
    late = [LiveRow("pm_price_change", E, (str(E - 20), up, "BUY", "0.991", "100", "0.991", "0.993")),
            LiveRow("pm_price_change", E, (str(E - 20), dn, "SELL", "0.009", "100", "0.007", "0.009"))]
    rows = sort_rows(rows + late)
    cfg = cfg_of("cap-probe", timeframes=("5m", "15m"))
    rs = E - 19 * 60_000
    router, live, store = run_live(rows, cfg, rs, E - 30_000, admit_filter=lambda info, t: info.window_end_ms <= E)
    assert router.end_ms == E and all(r in store for r in late)  # accepted, written to the session
    rep = run_replay(store, cfg, router, tmp_path / "session", monkeypatch)
    assert_parity(live, rep)
    tops = [str(x) for x in live.coins["btc"].log if " close-top " in str(x)]
    assert any("Top(" in t for t in tops) and not any("bid_px=991" in t for t in tops), tops
    md = router.sources["btc"].mds[b]
    assert md.recv_cap == E and md.top_at(E, "recv").bid_px != 991 and md.top_at(E + 5, "recv").bid_px != 991
    assert md.tops_recv.recv_ms.max() < E  # never released into the recv arrays

"""The fake feed (`paper.fakefeed`): a synthetic store replayed through the real ingest under a virtual clock."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import duckdb
import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bt"))

from synth import make_session, write_store  # noqa: E402
from test_live import H11, cfg_of  # noqa: E402,F401  (registers the parity-probe strategies)

from polydata.build import build  # noqa: E402
from polydata.paper import parity  # noqa: E402
from polydata.paper.fakefeed import FakeFeed, VirtualClock  # noqa: E402
from polydata.paper.runner import PaperOpts, PaperRun  # noqa: E402

START = H11 - 20 * 60_000  # 2026-11-24 10:40 UTC (train day)
COMPARED = ["pm_market_meta", "pm_book", "pm_price_change", "pm_best_bid_ask", "pm_trade", "pm_other", "rtds_price",
            "bn_fut_bbo"]


def make_store(root: Path, *, coins=("btc",), minutes=22, seed=5) -> list:
    rows = make_session(seed, START, minutes, coins=coins, bn_ms=20, resolutions=True, meta_lead_ms=4 * 60_000)
    write_store(rows, root)
    return rows


def fake_run(tmp_path, monkeypatch, opts=None, *, strategy="parity-probe", coins=("btc",), store_coins=None,
             feed_fn=None, speed=None, real_sleep=None, out=None, extra=None, store=None):
    """A paper run fed from a synthetic store at `START` (`feed_fn(feed)` adds faults). Returns (cfg, run dir, run,
    feed, clock)."""
    store = store or tmp_path / "store"
    monkeypatch.setenv("POLYDATA_DIR", str(store))
    monkeypatch.setenv("POLYDATA_RUNS_CSV", str(tmp_path / "runs-index.csv"))
    if not (store / "raw").exists():
        make_store(store, coins=store_coins or coins)
    cfg = cfg_of(strategy)
    if tuple(coins) != tuple(cfg.coins):
        cfg = type(cfg).from_dict({**cfg.to_dict(), "coins": list(coins)})
    opts = opts or PaperOpts(tick_ms=50, duration_s=20 * 60)
    feed = FakeFeed.from_store(store, START, START + 3_600_000, cfg.coins, cfg.timeframes)
    if feed_fn is not None:
        feed_fn(feed)
    vc = VirtualClock(START, speed, real_sleep=real_sleep)
    out = out or tmp_path / "run"
    lines: list[str] = []
    factory = feed.factory(vc)
    if extra is not None:
        base = factory
        factory = lambda ingest, clock: base(ingest, clock) + extra(vc)  # noqa: E731
    run = PaperRun(cfg, out, opts, factory, clock=vc, sleep=vc.sleep, echo=lines.append,
                   rest_factory=feed.rest_factory())
    d = asyncio.run(run.run())
    return cfg, d, run, feed, vc


def _rows(parquet: Path, stream: str, lo: int, hi: int) -> pd.DataFrame:
    con = duckdb.connect()
    df = con.execute(f"SELECT * EXCLUDE (date) FROM read_parquet('{parquet.as_posix()}/{stream}/*/*.parquet') "
                     "WHERE recv_ms >= ? AND recv_ms <= ?", [lo, hi]).df()
    df = df.drop(columns=[c for c in ("seq",) if c in df.columns]).astype(str)  # seq: the build's row number
    return df.sort_values(list(df.columns)).reset_index(drop=True)


def test_fake_session_builds_same_rows_as_source(tmp_path, monkeypatch):
    """Store with btc and eth, config btc: the session's rows are the source's btc rows of the run window, with their
    recorded recv_ms."""
    cfg, d, run, feed, vc = fake_run(tmp_path, monkeypatch, store_coins=("btc", "eth"))
    store, sess = tmp_path / "store", d / "session"
    build()  # the source store (POLYDATA_DIR)
    parity.build_session(sess)
    last = feed.last_recv
    assert feed.delivered > 10_000 and last is not None
    for stream in COMPARED:
        a = _rows(sess / "parquet", stream, START, last)
        b = _rows(store / "parquet", stream, START, last)
        if stream in ("pm_book", "pm_price_change", "pm_best_bid_ask", "pm_trade"):
            b = b[b.asset.str.startswith("btc-")].reset_index(drop=True)
        elif stream == "pm_market_meta":
            b = b[b.d.str.contains('"series":"btc-')].reset_index(drop=True)
        elif stream == "pm_other":
            b = b[b.d.str.contains('"market":"0xbtc')].reset_index(drop=True)
        elif stream == "rtds_price":
            b = b[b.symbol.str.startswith("btc")].reset_index(drop=True)
        elif stream == "bn_fut_bbo":
            b = b[b.sym == "BTCUSDT"].reset_index(drop=True)
        assert len(a) > 0, stream
        pd.testing.assert_frame_equal(a, b, check_dtype=False, obj=stream)
    # the recorded resolutions were Gamma answers, not rows: the session's pm_resolution rows are the settler's
    res = json.loads((d / "results.json").read_text())
    assert res["resolutions"] == {"paper-chainlink": 3} and res["gamma_answers"] == 3
    assert res["gamma_fallbacks"] == 0 and res["api_trades"]["markets"] == 3


def test_fake_speed_max_and_n():
    """speed=2: virtual time advances 2x the (patched) real sleep; max: no real sleep, the clock jumps. Rows are
    delivered with the clock at their recv_ms."""
    rows = [(1_000 + 7 * k, ("x", k)) for k in range(100)]

    class Ing:
        def __init__(self, clock):
            self.clock, self.seen = clock, []

        def on_rows(self, group):
            self.seen.append((self.clock(), [v for _, v in group]))

        def on_event(self, *a):
            pass

    def drive(speed):
        slept: list[float] = []

        async def real_sleep(dt):
            slept.append(dt)

        vc = VirtualClock(1_000, speed, real_sleep=real_sleep)
        feed = FakeFeed([("pm_book", lambda: iter(rows))], 1_000)
        ing = Ing(vc)

        async def main():
            task = asyncio.ensure_future(feed.run(ing, vc))
            await asyncio.sleep(0)
            for _ in range(40):
                await vc.sleep(0.02)
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)

        asyncio.run(main())
        return vc, ing, slept

    vc, ing, slept = drive(2.0)
    assert vc.now() == 1_000 + 40 * 20
    assert abs(sum(slept) * 2 - 40 * 0.02) < 1e-9 and len(slept) == 40
    assert [t for t, _ in ing.seen] == [r for r, _ in rows if r <= 1_800]
    vc, ing, slept = drive(None)
    assert slept == [] and vc.now() == 1_800 and len(ing.seen) == len([r for r, _ in rows if r <= 1_800])
    # a stall: the clock leaps when the runner's sleep crosses it
    vc = VirtualClock(0)
    vc.stall(50, 5_000)

    async def main2():
        for _ in range(5):
            await vc.sleep(0.02)

    asyncio.run(main2())
    assert vc.now() == 100 + 5_000


def test_fake_session_replays_a_session(tmp_path, monkeypatch):
    """`from_session`: a paper session fed again (rows, connection events, Gamma answers) makes the same calls."""
    cfg, d, run, feed, vc = fake_run(tmp_path, monkeypatch)
    feed2 = FakeFeed.from_session(d / "session")
    assert feed2.start_ms == START and len(feed2.answers) == 3
    vc2 = VirtualClock(feed2.start_ms)
    run2 = PaperRun(cfg, tmp_path / "run2", PaperOpts(tick_ms=50, duration_s=20 * 60), feed2.factory(vc2), clock=vc2,
                    sleep=vc2.sleep, echo=lambda s: None, rest_factory=feed2.rest_factory())
    d2 = asyncio.run(run2.run())
    for name in ("decisions", "orders", "fills", "equity"):
        assert pd.read_parquet(d / f"{name}.parquet").equals(pd.read_parquet(d2 / f"{name}.parquet")), name
    a, b = pd.read_parquet(d / "markets.parquet"), pd.read_parquet(d2 / "markets.parquet")
    assert a.drop(columns=["gamma_seen_ms"]).equals(b.drop(columns=["gamma_seen_ms"]))
    assert parity.compare(d2, parity.replay(d2))["passed"]

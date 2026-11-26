"""Performance benchmarks for the paper runner (offline; no network, no orders).

    uv run python tools/paper_bench.py parsers [--n 20000]
    POLYDATA_DIR=... uv run python tools/paper_bench.py fake --from "2026-11-24 12:00" --to "2026-11-24 13:00"

`parsers` prints messages/s per feed parser (one core). `fake` runs the full ingest path over the recorder store at
max speed and prints the speed x real time, CPU, RSS and the processing / loop lag percentiles.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import resource
import shutil
import sys
import tempfile
import time
from pathlib import Path

GOLDEN = Path(__file__).resolve().parents[1] / "tests" / "paper" / "golden"
KEYS = {"TUP": "s|U", "TDN": "s|D"}


def _key(t: str) -> str:
    return KEYS.get(t, t)


def _raws(*names: str) -> list[str]:
    out = []
    for n in names:
        g = json.loads((GOLDEN / f"{n}.json").read_text())
        out += [m[0] for m in g["messages"]] if "messages" in g else [g["raw"]]
    return out


def bench_parsers(n: int = 20000) -> dict[str, float]:
    """Messages per second of each parser, over its golden messages repeated up to `n` messages."""
    from polydata.paper.feeds.binance import parse_fut
    from polydata.paper.feeds.polymarket import parse_clob
    from polydata.paper.feeds.rtds import parse_rtds

    clob = _raws("clob_book", "clob_price_change", "clob_best_bid_ask", "clob_last_trade_price", "clob_array")
    fut = _raws("binance_agg_trade", "binance_book_ticker")
    g = json.loads((GOLDEN / "rtds_dedup.json").read_text())
    rtds = [raw for _, raw in g["messages"]]
    cases = {"clob": (clob, lambda r: parse_clob(r, _key)), "binance_fut": (fut, parse_fut)}
    cases["rtds"] = (rtds, lambda r: parse_rtds(r, "a", {}))
    out = {}
    for name, (raws, fn) in cases.items():
        msgs = [raws[i % len(raws)] for i in range(n)]
        t0 = time.perf_counter()
        for m in msgs:
            fn(m)
        out[name] = n / max(time.perf_counter() - t0, 1e-9)
    return out


def bench_fake(frm: int, to: int, coins: list[str] | None = None, config: str | None = None) -> dict:
    """The full ingest path over the recorder store at max speed (PaperRun + FakeFeed + virtual clock)."""
    from polydata.bt.cli import _fail  # noqa: F401
    from polydata.paper import cli as pc
    from polydata.paper.fakefeed import FakeFeed  # noqa: F401
    from polydata.paper.runner import PaperRun

    cfg_path = config or str(Path(__file__).resolve().parents[1] / "configs" / "validation" / "endgame-maker-v2.yaml")
    cfg, paper = pc.load_paper_config(cfg_path, coins)
    ns = argparse.Namespace(fake_session=None, fake_from=frm, fake_to=to, speed=None, hold_back_ms=None, skew_ms=None,
                            warmup_s=None, max_drain=None, confirm_wait=None, duration=None, allow_sealed=False,
                            purpose=None)
    opts = pc.build_opts(ns, paper)
    feed, vc = pc.fake_setup(ns, cfg, opts)
    out = Path(tempfile.mkdtemp(prefix="paper_bench_"))
    try:
        c0, t0 = time.process_time(), time.perf_counter()
        run = PaperRun(cfg, out, opts, feed.factory(vc), clock=vc, sleep=vc.sleep, update_docs_index=False,
                       rest_factory=feed.rest_factory())
        asyncio.run(run.run())
        wall, cpu = time.perf_counter() - t0, time.process_time() - c0
        span = (feed.last_recv or feed.start_ms) - feed.start_ms
        stats = json.loads((out / "results.json").read_text())
    finally:
        shutil.rmtree(out, ignore_errors=True)
    rss = resource.getrusage(resource.RUSAGE_SELF).ru_maxrss
    rss_mb = rss / 1e6 if sys.platform == "darwin" else rss / 1e3
    return {"rows": feed.delivered, "wall_s": wall, "span_s": span / 1000, "speed_x": span / 1000 / max(wall, 1e-9),
            "cpu_pct": 100 * cpu / max(wall, 1e-9), "rss_mb": rss_mb,
            "processing_lag_ms": stats.get("processing_lag_ms"), "loop_lag_ms": stats.get("loop_lag_ms")}


def main(argv: list[str] | None = None) -> dict:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("parsers")
    p.add_argument("--n", type=int, default=20000)
    f = sub.add_parser("fake")
    f.add_argument("--from", dest="frm", required=True)
    f.add_argument("--to", required=True)
    f.add_argument("--coins", nargs="+")
    a = ap.parse_args(argv)
    if a.cmd == "parsers":
        res = bench_parsers(a.n)
        for k, v in res.items():
            print(f"{k:12s} {v:12,.0f} msg/s")
        return res
    from polydata.paper.cli import parse_time

    res = bench_fake(parse_time(a.frm), parse_time(a.to), a.coins)
    print(json.dumps(res, indent=2))
    return res


if __name__ == "__main__":
    main()

"""`polydata paper run CONFIG.yaml ...`: a paper session against the live public feeds (no orders are ever sent).

The command re-executes itself with `PYTHONHASHSEED=0` when that is not already set (spec "Parity check": both the
paper run and its replay run with the same string hashing).
"""

from __future__ import annotations

import argparse
import asyncio
import os
import re
import shutil
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

from polydata.bt.config import RunConfig

_DUR = re.compile(r"^(\d+(?:\.\d+)?)([smhd]?)$")
_UNIT = {"": 1, "s": 1, "m": 60, "h": 3600, "d": 86400}


def parse_duration(text: str) -> int:
    """'90', '90s', '15m', '6h', '1d' -> seconds."""
    m = _DUR.match(str(text).strip())
    if not m:
        raise argparse.ArgumentTypeError(f"bad duration {text!r} (use e.g. 90s, 15m, 6h)")
    return int(round(float(m.group(1)) * _UNIT[m.group(2)]))


def ensure_hashseed(argv: list[str]) -> None:
    """Re-exec `python -m polydata.cli *argv` with PYTHONHASHSEED=0 unless it is already set to 0."""
    if os.environ.get("PYTHONHASHSEED") == "0":
        return
    env = {**os.environ, "PYTHONHASHSEED": "0"}
    sys.stdout.flush()
    sys.stderr.flush()
    os.execvpe(sys.executable, [sys.executable, "-m", "polydata.cli", *argv], env)


def main(argv: list[str] | None = None) -> None:
    """Entry point for `polydata paper ...` with the hash seed fixed (re-execs if needed)."""
    argv = list(sys.argv[1:] if argv is None else argv)
    ensure_hashseed(argv)
    from polydata.cli import main as polydata_main

    polydata_main(argv)


def load_paper_config(path: str, coins=None, sets=()) -> tuple[RunConfig, dict]:
    """A bt config (its `days` are ignored) and its optional `paper:` block."""
    from polydata.bt.cli import _fail, _kv, apply_set

    d = yaml.safe_load(Path(path).read_text()) or {}
    paper = d.pop("paper", None) or {}
    d.pop("days", None)
    if coins:
        d["coins"] = coins
    try:
        for s in sets:
            k, v = _kv(s)
            apply_set(d, k, yaml.safe_load(v))
        return RunConfig.from_dict(d), dict(paper)
    except (ValueError, TypeError) as e:
        _fail(str(e))


def live_feeds(cfg: RunConfig):
    """The live feeds factory: Polymarket discovery + CLOB (built once per session), RTDS Chainlink, Binance."""
    from polydata.paper.feeds.binance import BinanceFeed
    from polydata.paper.feeds.polymarket import PolymarketFeed
    from polydata.paper.feeds.rest import Rest
    from polydata.paper.feeds.rtds import RtdsFeed
    from polydata.paper.feeds.series import series_for

    def factory(ingest, clock):
        emit, event = ingest.on_rows, ingest.on_event
        feeds = [PolymarketFeed(series_for(cfg.coins, cfg.timeframes), emit, event, Rest(clock, event), clock),
                 RtdsFeed(emit, event, clock), BinanceFeed(cfg.coins, emit, event, clock)]

        async def run(f):
            try:
                await f.run()
            finally:
                f.stop()

        return [run(f) for f in feeds]

    return factory


def build_opts(a, paper: dict):
    from polydata.paper.runner import PaperOpts

    o = PaperOpts()
    known = set(o.__dict__)
    for k, v in paper.items():
        k = k.replace("-", "_")
        if k in ("max_drain", "confirm_wait", "duration"):
            k, v = k + "_s", parse_duration(v)
        if k not in known:
            raise SystemExit(f"error: unknown paper setting {k!r}")
        setattr(o, k, v)
    for k in ("hold_back_ms", "skew_ms", "warmup_s"):
        if getattr(a, k) is not None:
            setattr(o, k, getattr(a, k))
    for k in ("max_drain", "confirm_wait", "duration"):
        if getattr(a, k) is not None:
            setattr(o, k + "_s", getattr(a, k))
    o.allow_sealed, o.purpose = bool(a.allow_sealed), a.purpose
    if o.allow_sealed and not o.purpose:
        raise SystemExit("error: --allow-sealed needs --purpose TEXT")
    return o


def cmd_run(a) -> None:
    ensure_hashseed(sys.argv[1:])
    from polydata.bt import runs
    from polydata.bt.strategy import get_strategy
    from polydata.paper.runner import PaperRefused, PaperRun

    cfg, paper = load_paper_config(a.config, a.coins, a.set)
    opts = build_opts(a, paper)
    created = a.out is None
    if a.out:
        out = Path(a.out)
    else:
        cls = get_strategy(cfg.strategy)
        out = runs.new_run_dir(cls.family or "unfiled", cls.name, datetime.now(timezone.utc))
    print(f"# paper run {cfg.strategy} coins {','.join(cfg.coins)} timeframes {','.join(cfg.timeframes)} -> {out}",
          flush=True)
    try:
        asyncio.run(PaperRun(cfg, out, opts, live_feeds(cfg), update_docs_index=a.update_docs_index).run())
    except PaperRefused:
        if created:
            shutil.rmtree(out, ignore_errors=True)
        raise


def register(sub) -> None:
    paper = sub.add_parser("paper", help="paper trading against the live feeds (no orders are ever sent)")
    ps = paper.add_subparsers(dest="paper_cmd", required=True)
    sp = ps.add_parser("run", help="run a strategy on the live feeds and write a run folder with its session")
    sp.add_argument("config", metavar="CONFIG.yaml")
    sp.add_argument("--out", metavar="DIR", help="run folder (default: data/runs/<family>/<strategy>/<time>-<id>)")
    sp.add_argument("--duration", type=parse_duration, help="e.g. 30m, 6h (default: until Ctrl-C)")
    sp.add_argument("--coins", nargs="+")
    sp.add_argument("--set", nargs="+", default=[], metavar="KEY=VALUE")
    sp.add_argument("--hold-back-ms", type=int, dest="hold_back_ms")
    sp.add_argument("--skew-ms", type=int, dest="skew_ms")
    sp.add_argument("--warmup-s", type=int, dest="warmup_s")
    sp.add_argument("--max-drain", type=parse_duration, dest="max_drain")
    sp.add_argument("--confirm-wait", type=parse_duration, dest="confirm_wait")
    sp.add_argument("--allow-sealed", action="store_true", help="run on a sealed day (records one look)")
    sp.add_argument("--purpose", help="why a sealed day is read (with --allow-sealed)")
    sp.add_argument("--update-docs-index", action="store_true",
                    help="also rewrite the committed docs/runs-index.csv (default: data/runs/index.csv only)")
    sp.set_defaults(fn=cmd_run)


if __name__ == "__main__":
    main()

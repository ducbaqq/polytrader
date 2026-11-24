"""`polydata bt ...`: run, sweep, validate, runs, push, pull. Registered by `polydata.cli`."""

from __future__ import annotations

import argparse
import itertools
import os
import secrets
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from . import metrics as bt_metrics
from . import report, runs
from .config import RunConfig
from .splits import day_kind

_FIELDS = set(RunConfig.__dataclass_fields__)


def _fail(msg: str):
    sys.exit(f"error: {msg}")


# ---------------------------------------------------------------------------------------- parsing

def parse_days(tokens: list[str] | None) -> list[str] | None:
    """ISO dates and `A..B` ranges (inclusive) to a sorted list of ISO dates."""
    if not tokens:
        return None
    out: set[str] = set()
    for t in tokens:
        a, _, b = t.partition("..")
        d0, d1 = date.fromisoformat(a), date.fromisoformat(b or a)
        out |= {(d0 + timedelta(days=i)).isoformat() for i in range((d1 - d0).days + 1)}
    return sorted(out)


def apply_set(d: dict, key: str, value) -> None:
    """`params.x=v` or an unknown name sets a strategy param; a RunConfig field name sets that field."""
    if key.startswith("params."):
        d.setdefault("params", {})[key[7:]] = value
    elif key in _FIELDS:
        d[key] = value
    else:
        d.setdefault("params", {})[key] = value


def _kv(s: str) -> tuple[str, str]:
    if "=" not in s:
        _fail(f"expected key=value, got {s!r}")
    k, _, v = s.partition("=")
    return k.strip(), v


def load_config(path: str, days=None, coins=None, sets=()) -> RunConfig:
    d = yaml.safe_load(Path(path).read_text()) or {}
    if days:
        d["days"] = parse_days(days)
    if coins:
        d["coins"] = coins
    for s in sets:
        k, v = _kv(s)
        apply_set(d, k, yaml.safe_load(v))
    try:
        return RunConfig.from_dict(d)
    except (ValueError, TypeError) as e:
        _fail(str(e))


# ---------------------------------------------------------------------------------------- running

def sealed_con():
    """A connection with sealed days visible, for `--final` workers. The look is recorded once by the
    caller (`polydata.connect(include_sealed=True, purpose="final")`), not once per worker process."""
    import duckdb

    from ..views import create_views

    con = duckdb.connect(":memory:")
    con.execute("SET TimeZone = 'UTC'")
    create_views(con, include_sealed=True)
    return con


def _brief(s: dict) -> dict:
    return {k: s[k] for k in ("pnl_usd", "usd_per_day", "fills", "orders", "voided", "n_days")}


def execute(cfg: RunConfig, kind: str = "run", latency_sweep: tuple[int, ...] | None = None, workers: int | None = None,
            con_factory=None, extra_prov: dict | None = None, rebuild: bool = True) -> tuple[Path, dict]:
    """Run `cfg`, optionally re-run it at other order latencies, write the run folder and refresh the index."""
    from . import engine
    from .strategy import get_strategy

    cls = get_strategy(cfg.strategy)
    con_factory = con_factory or engine.default_con_factory
    res = engine.run(cfg, con_factory, workers)
    metrics = runs.summary(res, cfg)
    dk = lambda d: day_kind(date.fromisoformat(d))  # noqa: E731
    if latency_sweep:
        sweep = {str(cfg.order_latency_ms): {**_brief(metrics), "eval_pnl_usd": bt_metrics.eval_pnl(res, dk)}}
        for ms in latency_sweep:
            if ms != cfg.order_latency_ms:
                c2 = replace(cfg, order_latency_ms=ms)
                r2 = engine.run(c2, con_factory, workers)
                sweep[str(ms)] = {**_brief(runs.summary(r2, c2)), "eval_pnl_usd": bt_metrics.eval_pnl(r2, dk)}
        metrics["latency_sweep"] = dict(sorted(sweep.items(), key=lambda kv: int(kv[0])))
    days = metrics["days"]
    m = bt_metrics.compute(res, cfg, dk)
    m["latency_sweep"] = metrics.get("latency_sweep")
    v = bt_metrics.verdict(m, "final" if kind == "final" else "provisional", (extra_prov or {}).get("final_looks", 0),
                           cfg.min_usd_per_day)
    e = m["eval"] or m
    metrics.update(bt_metrics.clean({"metrics": m, "verdict": v["label"], "verdict_detail": v, "t_block": e["block_pnl_t"]}))
    prov = runs.provenance(cls, cfg.params, days, kind, elapsed_s=round(res.elapsed_s, 1), **(extra_prov or {}))
    d = runs.new_run_dir(cls.family or "unfiled", cls.name, datetime.now(timezone.utc))
    runs.write_run(d, cfg, res, metrics, prov)
    if rebuild:
        runs.rebuild_index()
        report.render_run(d)
    return d, metrics


def _print_run(d: Path, m: dict) -> None:
    print(f"run folder: {d}")
    print(f"  verdict: {m['verdict']}; report: {d / 'report.html'}")
    print(f"  P&L ${m['pnl_usd']:.2f} over {m['n_days']} days (${m['usd_per_day']:.2f}/day), {m['orders']} orders, "
          f"{m['fills']} fills, {m['voided']}/{m['markets']} markets voided")
    for ms, s in m.get("latency_sweep", {}).items():
        print(f"  latency {ms} ms: P&L ${s['pnl_usd']:.2f}, {s['fills']} fills")


def cmd_run(a) -> None:
    cfg = load_config(a.config, a.days, a.coins, a.set)
    sweep = runs.LATENCY_SWEEP_MS if a.latency_sweep else None
    if not a.final:
        d, m = execute(cfg, "run", sweep)
        return _print_run(d, m)
    if a.days:
        _fail("--final always evaluates every sealed day; do not pass --days")
    from .strategy import get_strategy

    cls = get_strategy(cfg.strategy)
    days = runs.days_of_kinds({"sealed"})
    try:
        earlier = runs.final_preflight(cls, cfg.params, days)
    except runs.FinalRefused as e:
        _fail(f"--final refused: {e}")
    from ..views import connect

    connect(include_sealed=True, purpose="final").close()  # the recorded look
    n = runs.final_looks()
    cfg = replace(cfg, day_kinds=["sealed"], days=days, tags=[*cfg.tags, "final"], order_latency_ms=200)
    d, m = execute(cfg, "final", runs.LATENCY_SWEEP_MS, con_factory=sealed_con,
                   extra_prov={"earlier_finals": list(earlier.run_id), "final_looks": n})
    _print_run(d, m)
    print(f"  final looks so far (N): {n}")
    print(f"  earlier finals of this version: {', '.join(earlier.run_id) if len(earlier) else 'none'}")


def _sweep_point(args) -> tuple[str, dict]:
    cfg_d, workers = args
    d, m = execute(RunConfig.from_dict(cfg_d), "sweep", None, workers, rebuild=False)
    return str(d), m


def cmd_sweep(a) -> None:
    base = load_config(a.config, a.days, a.coins, a.set).to_dict()
    grid = []
    for g in a.grid:
        k, v = _kv(g)
        grid.append([(k, yaml.safe_load(x)) for x in v.split(",")])
    sweep_id = f"{datetime.now(timezone.utc):%m%d%H%M}-{secrets.token_hex(2)}"
    points = []
    for combo in itertools.product(*grid):
        d = {**base, "params": dict(base.get("params") or {}), "tags": [*base.get("tags", []), f"sweep:{sweep_id}"]}
        for k, v in combo:
            apply_set(d, k, v)
        points.append((combo, d))
    workers = max(1, (os.cpu_count() or 4) // a.jobs)
    print(f"sweep {sweep_id}: {len(points)} runs, {a.jobs} at a time")
    jobs = [(d, workers) for _, d in points]
    if a.jobs <= 1:
        results = [_sweep_point(j) for j in jobs]
    else:
        with ProcessPoolExecutor(a.jobs) as ex:
            results = list(ex.map(_sweep_point, jobs))
    runs.rebuild_index()
    for path, _ in results:  # now the siblings are in the index: render each run's robustness grid
        report.render_run(Path(path))
    rows = sorted(zip(points, results), key=lambda r: -r[1][1]["usd_per_day"])
    for (combo, _), (path, m) in rows:
        label = " ".join(f"{k}={v}" for k, v in combo)
        print(f"  {label:<40} ${m['usd_per_day']:>9.2f}/day  P&L ${m['pnl_usd']:>9.2f}  {Path(path).name}")
    print(f"find them again: polydata bt runs --tag sweep:{sweep_id}")


def cmd_validate(a) -> None:
    from . import validate

    days = parse_days(a.days) or runs.days_of_kinds({"train", "validation"})
    if not days:
        _fail("no non-sealed days on disk: run `polydata update`")
    out = validate.run_suite(days)
    for name, c in out["checks"].items():
        print(f"  {'pass' if c['passed'] else 'FAIL'}  {name}")
    print(f"sim_version {out['sim_version']}: {'PASSED' if out['passed'] else 'FAILED'} ({out['elapsed_s']}s)")
    sys.exit(0 if out["passed"] else 1)


def cmd_runs(a) -> None:
    import pandas as pd

    df = runs.load_index()
    if a.strategy:
        df = df[df.strategy == a.strategy]
    if a.family:
        df = df[df.family == a.family]
    if a.tag:
        df = df[df.tags.fillna("").str.split(",").map(lambda t: a.tag in t)]
    if a.search:
        hay = df[["run_id", "family", "strategy", "version", "kind", "tags", "coins", "days", "verdict"]].fillna("")
        text = hay.agg(" ".join, axis=1).str.lower()
        df = df[text.str.contains(a.search.lower(), regex=False)]
    if a.passed:
        df = df[df.verdict.fillna("").str.lower().str.startswith("pass")]
    cols = ["run_id", "family", "strategy", "version", "kind", "n_days", "pnl_usd", "usd_per_day", "verdict", "tags"]
    with pd.option_context("display.max_columns", None, "display.width", 250, "display.max_colwidth", 60):
        print(df[cols].to_string(index=False) if len(df) else "no runs")
    print(f"({len(df)} runs; folders under {runs.runs_dir()})", file=sys.stderr)


def cmd_screen(a) -> None:
    from .. import screen

    if a.name not in screen.REGISTRY:
        _fail(f"unknown screen {a.name!r}; known: {', '.join(sorted(screen.REGISTRY))}")
    d, res = screen.run(a.name, a.latency_ms, a.exit, a.size, a.coins, a.timeframes)
    o = res["metrics"]["overall"]
    print(f"run folder: {d}")
    print(f"  verdict: {res['verdict']} ({res['verdict_note']})")
    c = o["cents_per_share"]
    print(f"  overall: {o['n_trades']} trades over {o['n_days']} days, " + (f"{c:.2f} c/share CI {o['ci_cents']}, ${o['usd_per_day']:.2f}/day" if c is not None else "no trades"))
    for k, v in res["metrics"]["by_day_kind"].items():
        print(f"  {k}: {v['n_trades']} trades, " + (f"{v['cents_per_share']:.2f} c/share CI {v['ci_cents']}" if v["cents_per_share"] is not None else "none"))
    print(f"  note: {res['metrics']['note']}")


def cmd_push(a) -> None:
    print(f"uploaded {runs.push()} files")


def cmd_pull(a) -> None:
    print(f"downloaded {runs.pull()} files")


def register(bt: argparse.ArgumentParser) -> None:
    sub = bt.add_subparsers(dest="bt_cmd", required=True)

    def common(sp):
        sp.add_argument("config", metavar="CONFIG.yaml")
        sp.add_argument("--days", nargs="+", metavar="DAY", help="ISO dates or A..B ranges (default: every day of the config's day kinds)")
        sp.add_argument("--coins", nargs="+")
        sp.add_argument("--set", nargs="+", default=[], metavar="KEY=VALUE",
                        help="override a config field or strategy param (params.x=1 or x=1)")

    sp = sub.add_parser("run", help="replay a strategy and write a run folder")
    common(sp)
    sp.add_argument("--final", action="store_true", help="evaluate on all sealed days (once per version/sim/data)")
    sp.add_argument("--latency-sweep", action="store_true", help="also run at 100/200/400 ms order latency")
    sp.set_defaults(fn=cmd_run)

    sp = sub.add_parser("sweep", help="one run per grid point, sharing a sweep:<id> tag")
    common(sp)
    sp.add_argument("--grid", action="append", required=True, metavar="PARAM=V1,V2")
    sp.add_argument("--jobs", type=int, default=1, help="runs in parallel (each also parallelises over coins)")
    sp.set_defaults(fn=cmd_sweep)

    sp = sub.add_parser("validate", help="run the simulator validation suite")
    sp.add_argument("--days", nargs="+", metavar="DAY")
    sp.set_defaults(fn=cmd_validate)

    sp = sub.add_parser("runs", help="list and filter past runs")
    sp.add_argument("--strategy")
    sp.add_argument("--family")
    sp.add_argument("--tag")
    sp.add_argument("--search", metavar="TEXT")
    sp.add_argument("--passed", action="store_true")
    sp.set_defaults(fn=cmd_runs)

    sp = sub.add_parser("screen", help="screen a taker idea over snapshots in seconds (reject or promote)")
    sp.add_argument("name", metavar="NAME")
    sp.add_argument("--latency-ms", type=int, default=300, choices=[300, 1000])
    sp.add_argument("--exit", choices=["hold", "bid"], default="hold")
    sp.add_argument("--size", type=float, default=10.0, help="shares per signal (depth is not checked)")
    sp.add_argument("--coins", nargs="+")
    sp.add_argument("--timeframes", nargs="+")
    sp.set_defaults(fn=cmd_screen)

    sub.add_parser("push", help="upload data/runs to the bucket, rebuild the index").set_defaults(fn=cmd_push)
    sub.add_parser("pull", help="download runs from the bucket, rebuild the index").set_defaults(fn=cmd_pull)

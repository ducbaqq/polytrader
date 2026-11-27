"""Replay identity harness: proves engine refactors leave backtest outputs byte-identical.

    cd analysis && POLYDATA_DIR=$PD uv run python tools/replay_identity.py save --out DIR [--only NAME ...]
    cd analysis && POLYDATA_DIR=$PD uv run python tools/replay_identity.py check --ref DIR [--only NAME ...]

`--set k=v` (repeatable) overrides a RunConfig field in every case, e.g. `--set taker_delay_ms=0` checks a
reference saved before the taker delay existed (`ref-gaps`); `ref-taker-on` is saved with the defaults.

The CLI re-execs itself with PYTHONHASHSEED=0 when it is unset, so spawned workers share one hash seed.

Each case writes `<DIR>/<case>/results.json` (summary + metrics, canonical JSON) and `hashes.json` (sha256 per
result frame, row order kept, plus the log). Timings (`stats`, `elapsed_s`) are deliberately not part of it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import sys
import time
from dataclasses import replace
from datetime import date
from pathlib import Path

DAY = "2026-11-24"
VALIDATION = ("endgame-maker-v2", "fv-taker-m3", "mm-side-pull")
SANITY = ("noop", "buy-and-hold-up", "always-cross", "join-best")
FRAMES = ("orders", "fills", "equity", "markets", "decisions")
ANALYSIS = Path(__file__).resolve().parents[1]


def _cases():
    from polydata.bt.cli import load_config
    from polydata.bt.config import RunConfig

    out = []
    for name in VALIDATION:
        cfg = load_config(str(ANALYSIS / "configs" / "validation" / f"{name}.yaml"), days=[DAY], coins=["btc", "sol", "xrp"])
        out.append((name, replace(cfg, day_kinds=["train"])))
    for name in SANITY:
        out.append((name, RunConfig(strategy=name, coins=["btc", "sol"], timeframes=["5m"], days=[DAY])))
    return out


CASES = _cases()


def _sha(b: bytes) -> str:
    return hashlib.sha256(b).hexdigest()


def _frame_hash(df) -> str:
    """sha256 of dtypes plus CSV; NaN, None and empty string render distinctly, row order kept."""
    df = df.copy()
    for c in df.columns:
        if df[c].dtype == object:
            df[c] = [
                "<None>" if v is None else "<NaN>" if isinstance(v, float) and v != v else v for v in df[c]
            ]
    head = json.dumps([[str(c), str(t)] for c, t in df.dtypes.items()])
    body = df.to_csv(index=False, lineterminator="\n", na_rep="<NA>")  # float NaN -> <NA>; inf/-inf print as inf
    return _sha((head + "\0" + body).encode())


def fingerprint(res, cfg) -> dict[str, bytes]:
    from polydata.bt import metrics, runs
    from polydata.bt.splits import day_kind

    def dk(d: str) -> str:
        return day_kind(date.fromisoformat(d))

    doc = metrics.clean({**runs.summary(res, cfg), "metrics": metrics.compute(res, cfg, dk)})
    results = json.dumps(doc, sort_keys=True, indent=1, allow_nan=False).encode()
    hashes: dict[str, str] = {}
    for name in FRAMES:
        df = getattr(res, name, None)
        if df is None or (name == "decisions" and df.empty):  # untraced runs have no decisions
            continue
        hashes[name] = _frame_hash(df)
    hashes["log"] = _sha(json.dumps(list(res.log)).encode())
    return {"results.json": results, "hashes.json": json.dumps(hashes, indent=1).encode()}


def _overrides(sets) -> dict:
    import yaml

    out = {}
    for kv in sets or ():
        k, _, v = kv.partition("=")
        out[k] = yaml.safe_load(v)
    return out


def _select(only, sets=None):
    ov = _overrides(sets)
    cases = [(n, replace(c, **ov) if ov else c) for n, c in CASES if not only or n in only]
    if only and (bad := set(only) - {c[0] for c in CASES}):
        sys.exit(f"unknown case(s): {sorted(bad)}")
    return cases


def _run(cfg):
    from polydata.bt import engine

    return engine.run(cfg, engine.default_con_factory, None)


def cmd_save(a) -> int:
    t0 = time.perf_counter()
    for name, cfg in _select(a.only, a.set):
        t = time.perf_counter()
        fp = fingerprint(_run(cfg), cfg)
        d = Path(a.out) / name
        d.mkdir(parents=True, exist_ok=True)
        for fn, b in fp.items():
            (d / fn).write_bytes(b)
        print(f"saved {name} ({time.perf_counter() - t:.1f}s)", flush=True)
    print(f"total {time.perf_counter() - t0:.1f}s")
    return 0


def cmd_check(a) -> int:
    same = total = 0
    for name, cfg in _select(a.only, a.set):
        total += 1
        fp = fingerprint(_run(cfg), cfg)
        diff = None
        for fn, b in fp.items():
            p = Path(a.ref) / name / fn
            if not p.exists():
                diff = f"{fn}: missing reference"
            elif p.read_bytes() != b:
                old, new = json.loads(p.read_bytes()), json.loads(b)
                k = next((k for k in sorted(set(old) | set(new)) if old.get(k) != new.get(k)), "?")
                diff = f"{fn}: {k}"
            if diff:
                break
        if diff:
            print(f"DIFF {name}: {diff}")
        else:
            same += 1
            print(f"ok   {name}")
    print(f"identical: {same}/{total}")
    if total < len(CASES):
        print(f"PARTIAL ({total} of {len(CASES)} cases): not the full gate")
    return 0 if same == total else 1


def main(argv=None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    sub = p.add_subparsers(dest="cmd", required=True)
    s = sub.add_parser("save")
    s.add_argument("--out", required=True)
    c = sub.add_parser("check")
    c.add_argument("--ref", required=True)
    for x in (s, c):
        x.add_argument("--only", nargs="+", metavar="NAME")
        x.add_argument("--set", action="append", metavar="K=V", help="override a RunConfig field in every case")
    a = p.parse_args(argv)
    if os.environ.get("PYTHONHASHSEED") != "0":
        os.execve(sys.executable, [sys.executable, *sys.argv], {**os.environ, "PYTHONHASHSEED": "0"})
    return cmd_save(a) if a.cmd == "save" else cmd_check(a)


if __name__ == "__main__":
    sys.exit(main())

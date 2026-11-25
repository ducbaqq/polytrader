"""Replay identity harness: proves engine refactors leave backtest outputs byte-identical.

    cd analysis && POLYDATA_DIR=$PD uv run python tools/replay_identity.py save --out DIR [--only NAME ...]
    cd analysis && POLYDATA_DIR=$PD uv run python tools/replay_identity.py check --ref DIR [--only NAME ...]

Each case writes `<DIR>/<case>/results.json` (summary + metrics, canonical JSON) and `hashes.json` (sha256 per
result frame, row order kept, plus the log). Timings (`stats`, `elapsed_s`) are deliberately not part of it.
"""

from __future__ import annotations

import argparse
import hashlib
import json
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
        if df is None:
            continue
        hashes[name] = _sha(df.to_csv(index=False, lineterminator="\n").encode())
    hashes["log"] = _sha("\n".join(res.log).encode())
    return {"results.json": results, "hashes.json": json.dumps(hashes, indent=1).encode()}


def _select(only):
    cases = [c for c in CASES if not only or c[0] in only]
    if only and (bad := set(only) - {c[0] for c in CASES}):
        sys.exit(f"unknown case(s): {sorted(bad)}")
    return cases


def _run(cfg):
    from polydata.bt import engine

    return engine.run(cfg, engine.default_con_factory, None)


def cmd_save(a) -> int:
    t0 = time.perf_counter()
    for name, cfg in _select(a.only):
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
    for name, cfg in _select(a.only):
        total += 1
        fp = fingerprint(_run(cfg), cfg)
        diff = None
        for fn, b in fp.items():
            p = Path(a.ref) / name / fn
            if not p.exists():
                diff = f"{fn}: missing reference"
            elif p.read_bytes() != b:
                if fn == "hashes.json":
                    old, new = json.loads(p.read_bytes()), json.loads(b)
                    k = next((k for k in sorted(set(old) | set(new)) if old.get(k) != new.get(k)), "?")
                    diff = f"hashes.json: {k}"
                else:
                    old, new = json.loads(p.read_bytes()), json.loads(b)
                    k = next((k for k in sorted(set(old) | set(new)) if old.get(k) != new.get(k)), "?")
                    diff = f"results.json: {k}"
            if diff:
                break
        if diff:
            print(f"DIFF {name}: {diff}")
        else:
            same += 1
            print(f"ok   {name}")
    print(f"identical: {same}/{total}")
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
    a = p.parse_args(argv)
    return cmd_save(a) if a.cmd == "save" else cmd_check(a)


if __name__ == "__main__":
    sys.exit(main())

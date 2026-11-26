"""Real-data parity (spec "Fake feed", real data): the three validation configs, all 8 coins, a fake run from the
recorder store over 2026-11-24 11:00-13:00 UTC at `max` (rs = 11:01:00), then `paper replay --vs-recorder`.

Run: `POLYDATA_REALDATA=1 POLYDATA_DIR=$PD nice -n 15 uv run pytest tests/paper/realdata/test_parity_realdata.py -q -s`
(heavy: one fake run and two replays per config). The run folders go to a temporary directory.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("POLYDATA_REALDATA") != "1", reason="real data (POLYDATA_REALDATA=1)")

ROOT = Path(__file__).resolve().parents[3]  # analysis/
CONFIGS = ["endgame-maker-v2", "fv-taker-m3", "mm-side-pull"]
A, B = "2026-11-24T11:00", "2026-11-24T13:00"
SPAN_S = 2 * 3600
# Known differences against the recorder replay (`--vs-recorder`), measured on this window: the inputs are the same
# rows, but (1) the hold-back drops a quiet book's opening `pm_book` snapshot (its `timestamp` is the book's last
# change, ~60 s before receipt on bnb/doge/hype/xrp/zec), so the paper market opens at a later snapshot or never, while
# the recorder replay opens it at that snapshot (spec "Parity check": late rows are dropped before use); (2) bt voids
# markets with recorder gaps before replaying them (never opened), the paper run trades them and voids them afterwards
# (spec "Gaps"). Every decision difference must sit in a market with one of these causes (`explained.all_explained`:
# order ids renumbered per market, nothing unexplained); btc/eth are identical.
IDENTICAL_BUDGETS = ("btc", "eth")


def _cli(*args: str, env: dict) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, "-m", "polydata.cli", *args], cwd=ROOT, env=env, capture_output=True,
                          text=True)


@pytest.mark.parametrize("name", CONFIGS)
def test_parity_realdata(name, tmp_path):
    env = {**os.environ, "PYTHONHASHSEED": "0", "POLYDATA_RUNS_CSV": str(tmp_path / "runs-index.csv")}
    out = tmp_path / name
    t0 = time.perf_counter()
    r = _cli("paper", "run", f"configs/validation/{name}.yaml", "--fake-from", A, "--fake-to", B, "--speed", "max",
             "--out", str(out), env=env)
    run_s = time.perf_counter() - t0
    assert r.returncode == 0, r.stdout[-3000:] + r.stderr[-3000:]
    man = json.loads((out / "session" / "manifest.json").read_text())
    tfs = sorted({a["slug"].split("-")[2] for a in man["admitted"]})
    t1 = time.perf_counter()
    r2 = _cli("paper", "replay", str(out), "--vs-recorder", env=env)
    rep_s = time.perf_counter() - t1
    p = json.loads((out / "parity.json").read_text())
    v = json.loads((out / "parity" / "vs_recorder.json").read_text())
    print(f"\n{name}: fake run {run_s:.0f} s ({SPAN_S / run_s:.1f}x real time), replay + vs-recorder {rep_s:.0f} s; "
          f"admitted {len(man['admitted'])} ({', '.join(tfs)}); parity {p['verdict']} "
          + json.dumps({k: t["mismatches"] for k, t in p["tables"].items()})
          + f"; vs recorder: {v['decision_differences']} decision differences, fills {v['fills']}, "
          f"pnl {v['pnl_usd']}")
    print([x for x in r.stdout.splitlines() if x.startswith("# fake feed") or x.startswith("# P&L")])
    assert r2.returncode == 0 and p["passed"] and p["verdict"] == "passed", json.dumps(p, indent=1)[:4000]
    assert man["clean_stop"] is True and len(man["admitted"]) > 0
    e = v["explained"]
    print(f"vs recorder explained: {len(e['open_differs'])} markets open at another time ({len(e['late_snapshot'])} "
          f"late opening snapshot, {len(e['recorder_prevoided'])} voided by bt up front), unexplained "
          f"{e['unexplained']}, all explained {e['all_explained']}")
    assert e["all_explained"] and e["unexplained"] == 0, json.dumps(e)[:3000]
    for b in IDENTICAL_BUDGETS:
        d = v["decisions"].get(b)
        assert d is None or d["only_paper"] == d["only_recorder"] == 0, (b, d)
    if not e["open_differs"]:
        assert v["decision_differences"] == 0

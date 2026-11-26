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
# --vs-recorder (a bt replay of the same window from the recorder store): the inputs are the same rows, and since fix
# round 1 (ruling A: late rows of a market that has not opened are accepted, so a quiet book's stale opening snapshot
# opens the market as in bt) the only accepted cause of decision differences is bt voiding a market for a gap before
# the replay (never opened), while the paper run trades it and voids it afterwards (spec "Gaps"; ruling B). Pinned per
# config on this window: open-differing markets by cause; everything else is decision-identical.
EXPECTED_CAUSES = {
    "endgame-maker-v2": {"late_snapshot": 0, "recorder_prevoided": 1, "no_cause": 0},
    "fv-taker-m3": {"late_snapshot": 0, "recorder_prevoided": 4, "no_cause": 0},
    "mm-side-pull": {"late_snapshot": 0, "recorder_prevoided": 5, "no_cause": 0},
}


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
    causes = {k: len(e[k]) for k in ("late_snapshot", "recorder_prevoided", "no_cause")}
    print(f"vs recorder explained: {len(e['open_differs'])} markets open at another time {causes}, unexplained "
          f"{e['unexplained']}, all explained {e['all_explained']}; bt pre-voided: {e['recorder_prevoided']}")
    assert e["all_explained"] and e["unexplained"] == 0, json.dumps(e)[:3000]
    assert causes == EXPECTED_CAUSES[name], causes
    for b, d in v["decisions"].items():  # a budget without an open-differing market is decision-identical
        if not e["budgets"].get(b, {}).get("open_differs"):
            assert d["only_paper"] == d["only_recorder"] == 0, (b, d)

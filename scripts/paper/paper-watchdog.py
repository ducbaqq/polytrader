#!/usr/bin/env python3
"""Paper-run watchdog: every 5 minutes, alerts on Telegram ONLY for actionable states. Standard library only.

Alerts (each repeats at most once per REMIND_MIN while it lasts, then one "recovered" note):
  - a run's service is failed/inactive when it should run (between 00:05 and 23:55 UTC, not a sealed or
    late-skipped day);
  - the run's log.txt has no new line for > STALE_MIN minutes (status lines are written every minute);
  - the loop/processing lag p99 in the status lines stayed above LAG_MS for the last LAG_MIN minutes;
  - disk usage of the runs directory above DISK_PCT percent;
  - a finished run whose parity is not passed, whose clean_stop is false, or which has no parity.json 3 h
    after results.json (reported once per run).
Self-healing blips (a restart that comes back, a single lag spike) never alert.

Run configs are the enabled polytrader-paper@<stem>.timer units. Env (from /opt/polytrader/.env):
TELEGRAM_BOT_TOKEN, TELEGRAM_CHAT_ID. Optional: PAPER_RUNS_DIR, PAPER_STATE_DIR.

  paper-watchdog.py            run the checks once and send alerts (what the timer does)
  paper-watchdog.py --dry-run  print what would be sent; no sending, no state
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

RUNS = Path(os.environ.get("PAPER_RUNS_DIR", "/opt/polytrader/paper/runs"))
STATE_DIR = Path(os.environ.get("PAPER_STATE_DIR", "/opt/polytrader/paper/state"))
STATE_FILE = STATE_DIR / "watchdog.json"
STALE_MIN = 10
LAG_MS = 2000
LAG_MIN = 10
DISK_PCT = 85
REMIND_MIN = 60
PARITY_WAIT_S = 3 * 3600
LAG_RE = re.compile(r"lag p99 (\d+(?:\.\d+)?) ms")
DRY = "--dry-run" in sys.argv


def sh(*cmd: str) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=20).stdout


def send(text: str) -> None:
    if DRY:
        print("WOULD SEND:", text)
        return
    data = urllib.parse.urlencode({"chat_id": os.environ["TELEGRAM_CHAT_ID"], "text": text[:4000]}).encode()
    url = f"https://api.telegram.org/bot{os.environ['TELEGRAM_BOT_TOKEN']}/sendMessage"
    with urllib.request.urlopen(url, data=data, timeout=20):
        pass


def stems() -> list[str]:
    out = sh("systemctl", "list-unit-files", "polytrader-paper@*.timer", "--state=enabled", "--no-legend")
    return sorted({m.group(1) for m in re.finditer(r"polytrader-paper@(.+?)\.timer", out)})


def read_json(p: Path) -> dict:
    try:
        return json.loads(p.read_text())
    except (OSError, ValueError):
        return {}


def current_run(stem: str) -> Path | None:
    """The run folder the status file points to, else the newest folder (with a paper.lock if any)."""
    st = STATE_DIR / f"{stem}.status"
    try:
        m = re.search(r"\brunning (\S+)", st.read_text())
        if m and Path(m.group(1)).is_dir():
            return Path(m.group(1))
    except OSError:
        pass
    runs = [d for d in (RUNS / stem).glob("*") if d.is_dir()] if (RUNS / stem).is_dir() else []
    locked = [d for d in runs if (d / "paper.lock").exists()]
    pool = locked or runs
    return max(pool, key=lambda d: d.stat().st_mtime) if pool else None


def check_running(stem: str, now: datetime) -> dict[str, str]:
    out: dict[str, str] = {}
    minutes = now.hour * 60 + now.minute
    if not 5 <= minutes <= 23 * 60 + 55:
        return out
    try:
        status = (STATE_DIR / f"{stem}.status").read_text()
    except OSError:
        status = ""
    today = now.strftime("%Y-%m-%d")
    if f" {stem} {today} skipped-" in status:
        return out  # sealed day (or too late to start): nothing should run
    unit = f"polytrader-paper@{stem}.service"
    props = dict(l.split("=", 1) for l in sh("systemctl", "show", unit, "-p", "ActiveState", "-p", "SubState",
                                               "-p", "Result").splitlines() if "=" in l)
    done_today = f" {stem} {today} done " in status
    if props.get("ActiveState") != "active" and not done_today:
        out[f"{stem}:service"] = (f"Paper run {stem} is not running: {props.get('ActiveState')}/"
                                  f"{props.get('SubState')} (result {props.get('Result')}). "
                                  f"journalctl -u {unit} -n 50; restart: systemctl start {unit}")
        return out
    if props.get("ActiveState") != "active":
        return out
    run = current_run(stem)
    log = run / "log.txt" if run else None
    if log and log.exists():
        age_min = (time.time() - log.stat().st_mtime) / 60
        # the first status line comes after the warm-up, so allow a little longer right after a start
        if age_min > STALE_MIN:
            out[f"{stem}:stale"] = f"Paper run {stem}: log.txt has had no new line for {age_min:.0f} min ({run})"
        lags = [float(m.group(1)) for line in log.read_text(errors="replace").splitlines()[-LAG_MIN:]
                if (m := LAG_RE.search(line))]
        if len(lags) >= LAG_MIN and min(lags) > LAG_MS:
            out[f"{stem}:lag"] = (f"Paper run {stem}: processing lag p99 above {LAG_MS} ms for {LAG_MIN}+ min "
                                  f"(now {lags[-1]:.0f} ms)")
    return out


def check_disk() -> dict[str, str]:
    path = RUNS if RUNS.exists() else Path("/")
    u = shutil.disk_usage(path)
    pct = 100 * u.used / u.total
    if pct > DISK_PCT:
        return {"disk": f"Paper droplet disk {pct:.0f}% full ({u.free / 1e9:.1f} GB free); is the archive timer working?"}
    return {}


def check_finished(state: dict) -> dict[str, str]:
    """Finished runs with bad parity / unclean stop: reported once per run (key remembered in state)."""
    out: dict[str, str] = {}
    seen = state.setdefault("reported_runs", {})
    for results in RUNS.glob("*/*/results.json"):
        run = results.parent
        rel = f"{run.parent.name}/{run.name}"
        if rel in seen or time.time() - results.stat().st_mtime > 3 * 86400:
            continue
        res, par = read_json(results), read_json(run / "parity.json")
        why = []
        if par:
            if not par.get("passed"):
                why.append(f"parity {par.get('verdict', 'not passed')}")
            if par.get("clean_stop") is False or res.get("clean_stop") is False:
                why.append("clean_stop false")
        elif time.time() - results.stat().st_mtime > PARITY_WAIT_S:
            why.append("no parity.json (replay missing or failed)")
        if why:
            seen[rel] = time.time()
            out[f"run:{rel}"] = f"Paper run {rel}: " + ", ".join(why)
    return out


def main() -> int:
    now = datetime.now(timezone.utc)
    try:
        state = json.loads(STATE_FILE.read_text())
    except (OSError, ValueError):
        state = {}
    active: dict[str, str] = {}
    for stem in stems():
        active.update(check_running(stem, now))
    active.update(check_disk())
    active.update(check_finished(state))

    sent = state.setdefault("sent", {})
    t = time.time()
    for key, msg in active.items():
        last = sent.get(key)
        if key.startswith("run:"):
            send(msg)  # one-shot, remembered in reported_runs
            sent[key] = t
        elif last is None:
            send("ALERT " + msg)
            sent[key] = t
        elif t - last >= REMIND_MIN * 60:
            send("STILL FAILING " + msg)
            sent[key] = t
    for key in [k for k in sent if k not in active and not k.startswith("run:")]:
        send(f"RECOVERED {key}")
        del sent[key]
    state["reported_runs"] = {k: v for k, v in state.get("reported_runs", {}).items() if t - v < 14 * 86400}
    state["sent"] = {k: v for k, v in sent.items() if not k.startswith("run:") or t - v < 14 * 86400}
    if not DRY:
        STATE_DIR.mkdir(parents=True, exist_ok=True)
        STATE_FILE.write_text(json.dumps(state))
    return 0


if __name__ == "__main__":
    try:
        sys.exit(main())
    except Exception as e:  # noqa: BLE001 - a broken watchdog must be visible in journald, not silent
        print(f"paper-watchdog failed: {type(e).__name__}: {e}", file=sys.stderr)
        sys.exit(1)

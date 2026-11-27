#!/usr/bin/env bash
# One paper-run day: `polydata paper run` until 23:59:30 UTC, then `polydata paper replay` (parity), then a one-line
# status file. Skips sealed days (exit 0). Run by polytrader-paper@<config-stem>.service.
#
# Usage: paper-daily.sh <config.yaml>   (relative paths are relative to $PAPER_APP_DIR/analysis)
# Env (optional; PAPER_ENV_FILE, default /opt/polytrader/.env, is loaded if present):
#   PAPER_APP_DIR (/opt/polytrader/app), PAPER_RUNS_DIR (/opt/polytrader/paper/runs),
#   PAPER_STATE_DIR (/opt/polytrader/paper/state), POLYDATA_DIR (/opt/polytrader/paper/pd),
#   UV (~/.local/bin/uv).
set -euo pipefail

CONFIG="${1:?usage: paper-daily.sh <config.yaml>}"
APP="${PAPER_APP_DIR:-/opt/polytrader/app}"
RUNS="${PAPER_RUNS_DIR:-/opt/polytrader/paper/runs}"
STATE="${PAPER_STATE_DIR:-/opt/polytrader/paper/state}"
UV="${UV:-$HOME/.local/bin/uv}"
ENV_FILE="${PAPER_ENV_FILE:-/opt/polytrader/.env}"
if [ -f "$ENV_FILE" ]; then
  set -a
  # shellcheck disable=SC1090
  . "$ENV_FILE"
  set +a
fi
export POLYDATA_DIR="${POLYDATA_DIR:-/opt/polytrader/paper/pd}"
mkdir -p "$RUNS" "$STATE" "$POLYDATA_DIR"

cd "$APP/analysis"
stem=$(basename "$CONFIG" .yaml)
if [ ! -f "$CONFIG" ]; then   # a bare stem (the systemd instance name) or a path relative to analysis/
  for cand in "$CONFIG" "configs/paper/$stem.yaml" "configs/validation/$stem.yaml" "configs/$stem.yaml"; do
    if [ -f "$cand" ]; then CONFIG="$cand"; break; fi
  done
fi
[ -f "$CONFIG" ] || { echo "[paper-daily] config not found: $CONFIG"; exit 1; }
today=$(date -u +%Y-%m-%d)
status="$STATE/$stem.status"
say() { echo "[paper-daily] $*"; }
write_status() { echo "$(date -u +%FT%TZ) $stem $today $*" > "$status"; }

kind=$("$UV" run python -c "import sys, datetime as d; from polydata.bt.splits import day_kind; print(day_kind(d.date.fromisoformat(sys.argv[1])))" "$today")
if [ "$kind" = "sealed" ]; then
  say "$today is a sealed day: not running"
  write_status "skipped-sealed"
  exit 0
fi

secs=$(( $(date -u -d "$today 23:59:30" +%s) - $(date -u +%s) ))
if [ "$secs" -lt 120 ]; then
  say "only ${secs}s left today: not running"
  write_status "skipped-late"
  exit 0
fi

out="$RUNS/$stem/$today"
if [ -e "$out" ]; then
  # an earlier start today (restart after a failure): keep it, put this run beside it
  out="$RUNS/$stem/$today-$(date -u +%H%M%S)"
fi
mkdir -p "$RUNS/$stem"
say "run $stem -> $out for ${secs}s (day kind: $kind)"
write_status "running $out"
rc=0
"$UV" run polydata paper run "$CONFIG" --duration "${secs}s" --out "$out" || rc=$?
if [ ! -f "$out/results.json" ]; then
  say "paper run failed (rc=$rc), no results.json"
  write_status "run-failed rc=$rc"
  exit 1
fi
say "run finished (rc=$rc); replaying for parity"
rrc=0
"$UV" run polydata paper replay "$out" || rrc=$?
verdict=$(python3 -c "import json, sys; print(json.load(open(sys.argv[1])).get('verdict', 'unknown'))" "$out/parity.json" 2>/dev/null || echo none)
write_status "done run_rc=$rc replay_rc=$rrc parity=$verdict $out"
say "done: parity=$verdict"
# a run that produced results is a finished day even if it exited non-zero: no restart (it would double-run today)
exit 0

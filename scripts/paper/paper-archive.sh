#!/usr/bin/env bash
# Archive finished paper-run folders to DigitalOcean Spaces (spaces:$SPACES_BUCKET/paper/...). Hourly via
# polytrader-paper-archive.timer. A run folder is finished when it has parity.json or is older than 1 day.
# Uploads, verifies, then deletes local session/ folders older than PAPER_KEEP_DAYS (default 2); results, parity
# and markets files stay local. No database. Logs to journald.
#
# Env (from /opt/polytrader/.env): SPACES_BUCKET, SPACES_REGION, SPACES_KEY, SPACES_SECRET.
# Optional: PAPER_RUNS_DIR (/opt/polytrader/paper/runs), PAPER_KEEP_DAYS (2).
set -euo pipefail

RUNS="${PAPER_RUNS_DIR:-/opt/polytrader/paper/runs}"
KEEP_DAYS="${PAPER_KEEP_DAYS:-2}"
REMOTE="spaces:${SPACES_BUCKET}/paper"

export RCLONE_CONFIG_SPACES_TYPE=s3
export RCLONE_CONFIG_SPACES_PROVIDER=DigitalOcean
export RCLONE_CONFIG_SPACES_ACCESS_KEY_ID="$SPACES_KEY"
export RCLONE_CONFIG_SPACES_SECRET_ACCESS_KEY="$SPACES_SECRET"
export RCLONE_CONFIG_SPACES_ENDPOINT="${SPACES_REGION}.digitaloceanspaces.com"
export RCLONE_CONFIG_SPACES_ACL=private
export RCLONE_CONFIG=/dev/null   # everything comes from env

say() { echo "[paper-archive] $*"; }
trap 'say "FAILED at line $LINENO"' ERR

exec 9>/tmp/polytrader-paper-archive.lock
flock -n 9 || { say "previous run still active, skipping"; exit 0; }

[ -d "$RUNS" ] || { say "no runs dir"; exit 0; }
start=$(date +%s)
verified=0
pruned=0
failed=0
now=$start
cutoff=$(date -u -d "-${KEEP_DAYS} days" +%s)

for run in "$RUNS"/*/*/; do
  [ -d "$run" ] || continue
  run="${run%/}"
  [ -f "$run/results.json" ] || continue             # still running, or crashed before writing results
  rel="${run#"$RUNS"/}"
  mtime=$(stat -c %Y "$run/results.json")
  if [ ! -f "$run/parity.json" ] && [ $((now - mtime)) -lt 86400 ]; then
    continue                                         # not finished yet (no parity, younger than 1 day)
  fi
  if [ -n "$(find "$run" -type f -mmin -5 -print -quit)" ]; then
    continue                                         # files still being written (replay)
  fi
  if ! rclone copy "$run" "$REMOTE/$rel" --transfers 4 --checkers 8 --s3-no-check-bucket -q; then
    say "upload failed: $rel"
    failed=$((failed + 1))
    continue
  fi
  if ! rclone check "$run" "$REMOTE/$rel" --one-way --s3-no-check-bucket -q 2>/tmp/paper-archive-check.log; then
    say "verify failed: $rel: $(head -c 300 /tmp/paper-archive-check.log)"
    failed=$((failed + 1))
    continue
  fi
  verified=$((verified + 1))
  # verified in the bucket: drop the bulky local session once the run is older than KEEP_DAYS
  if [ -d "$run/session" ] && [ "$mtime" -lt "$cutoff" ]; then
    rm -rf "$run/session"
    pruned=$((pruned + 1))
    say "pruned local session: $rel"
  fi
done

free_gb=$(df --output=avail -B1 "$RUNS" | tail -1 | awk '{printf "%.1f", $1/1e9}')
say "ok: verified=$verified pruned=$pruned failed=$failed local free=${free_gb} GB, took $(( $(date +%s) - start ))s"
[ "$failed" -eq 0 ] || exit 1

#!/usr/bin/env bash
# Archive recorder data to DigitalOcean Spaces. Run hourly by polytrader-archive.timer.
#
# 1. Upload every closed data file (older than 15 min) to spaces:<bucket>/raw/...
# 2. For day folders older than KEEP_DAYS: verify the bucket copy matches (size + MD5),
#    then delete the local copy. Nothing is deleted unless the check passes.
# 3. Once a day, export the Postgres catalog tables to spaces:<bucket>/catalog/.
#
# Env (from /opt/polytrader/.env): SPACES_BUCKET, SPACES_REGION, SPACES_KEY, SPACES_SECRET,
#   DATABASE_URL, DATABASE_CA_CERT. Optional: RECORDER_DATA_DIR, ARCHIVE_KEEP_DAYS (default 3).
set -euo pipefail

DATA_DIR="${RECORDER_DATA_DIR:-/opt/polytrader/data}"
KEEP_DAYS="${ARCHIVE_KEEP_DAYS:-3}"
REMOTE="spaces:${SPACES_BUCKET}"

export RCLONE_CONFIG_SPACES_TYPE=s3
export RCLONE_CONFIG_SPACES_PROVIDER=DigitalOcean
export RCLONE_CONFIG_SPACES_ACCESS_KEY_ID="$SPACES_KEY"
export RCLONE_CONFIG_SPACES_SECRET_ACCESS_KEY="$SPACES_SECRET"
export RCLONE_CONFIG_SPACES_ENDPOINT="${SPACES_REGION}.digitaloceanspaces.com"
export RCLONE_CONFIG_SPACES_ACL=private
export RCLONE_CONFIG=/dev/null   # everything comes from env; silences the "config not found" notice

PSQL_URL="${DATABASE_URL}&sslrootcert=${DATABASE_CA_CERT}"
event() {  # kind, detail -> rec_events (best effort)
  psql "$PSQL_URL" -X -q -v ON_ERROR_STOP=1 -c "INSERT INTO rec_events (kind, source, detail) VALUES ('$1', 'archive', \$\$${2}\$\$)" >/dev/null 2>&1 || true
  echo "[archive] $1: $2"
}
trap 'event archive_error "failed at line $LINENO"' ERR

exec 9>/tmp/polytrader-archive.lock
flock -n 9 || { echo "[archive] previous run still active, skipping"; exit 0; }

start=$(date +%s)

# 1. Upload closed files. rclone verifies each upload's MD5 against the bucket.
rclone copy "$DATA_DIR/raw" "$REMOTE/raw" --min-age 15m --transfers 4 --checkers 8 --s3-no-check-bucket --stats-one-line --stats 0 -q
uploaded_gb=$(rclone size "$REMOTE/raw" --json | python3 -c 'import sys,json;print(round(json.load(sys.stdin)["bytes"]/1e9,2))')

# 2. Prune verified old days.
cutoff=$(date -u -d "-${KEEP_DAYS} days" +%Y-%m-%d)
pruned=()
for dir in "$DATA_DIR"/raw/????-??-??; do
  [ -d "$dir" ] || continue
  day=$(basename "$dir")
  [[ "$day" < "$cutoff" ]] || continue
  if rclone check "$dir" "$REMOTE/raw/$day" --one-way --s3-no-check-bucket -q 2>/tmp/archive-check.log; then
    rm -rf "$dir"
    pruned+=("$day")
  else
    event archive_check_failed "$day: $(head -c 300 /tmp/archive-check.log)"
  fi
done

# 3. Daily catalog export (first run after midnight UTC).
today=$(date -u +%Y-%m-%d)
if ! rclone lsf "$REMOTE/catalog/$today/" --s3-no-check-bucket -q 2>/dev/null | grep -q rec_markets; then
  tmp=$(mktemp -d)
  for t in rec_markets rec_events rec_stats; do
    psql "$PSQL_URL" -X -q -v ON_ERROR_STOP=1 -c "\copy $t TO STDOUT WITH (FORMAT csv, HEADER)" | zstd -q -9 > "$tmp/$t.csv.zst"
  done
  rclone copy "$tmp" "$REMOTE/catalog/$today" --s3-no-check-bucket -q
  rm -rf "$tmp"
fi

free_gb=$(df --output=avail -B1 "$DATA_DIR" | tail -1 | awk '{printf "%.1f", $1/1e9}')
event archive_ok "bucket raw=${uploaded_gb} GB, pruned=[${pruned[*]:-}], local free=${free_gb} GB, took $(( $(date +%s) - start ))s"

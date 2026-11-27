# Paper runner on an always-on Linux host

Runs `polydata paper run` one UTC day at a time under systemd, replays it for parity, uploads finished runs to
Spaces and alerts on Telegram. Paths below are the defaults; override them with env vars (see the script headers).
Design and run folder: [docs/PAPER.md](../../docs/PAPER.md).

## Install (as root, once)

```
apt install -y rclone
# repo at /opt/polytrader/app, uv in ~polytrader/.local/bin, /opt/polytrader/.env with TELEGRAM_BOT_TOKEN,
# TELEGRAM_CHAT_ID, SPACES_BUCKET, SPACES_REGION, SPACES_KEY, SPACES_SECRET (same file as the recorder)
install -d -o polytrader -g polytrader /opt/polytrader/paper/{runs,state,pd}
cp /opt/polytrader/app/scripts/paper/systemd/* /etc/systemd/system/
systemctl daemon-reload
systemctl enable --now polytrader-paper@record-btc-eth.timer polytrader-paper@endgame-maker-v2.timer
systemctl enable --now polytrader-paper-archive.timer polytrader-paper-watchdog.timer
systemctl start polytrader-paper@record-btc-eth polytrader-paper@endgame-maker-v2   # start today now (mid-day ok)
```

The instance name is a config stem: `paper-daily.sh` finds `analysis/configs/paper/<stem>.yaml`, then
`configs/validation/<stem>.yaml`, then `configs/<stem>.yaml`. Configs to run: `endgame-maker-v2` (frozen validation
config) and `record-btc-eth` (noop strategy, records BTC/ETH 5m + 15m). Both need only the `analysis/` uv env
(`cd /opt/polytrader/app/analysis && uv sync`). Trading uses 0.6 to 1.4 GB per run. The end-of-run session build runs in a subprocess (measured peak about 3 GB
for a 1 h, 8-coin session: Arrow reads one hour of the busiest stream outside DuckDB's cap) under a host-wide lock,
so two runs never build at once. The unit has `MemoryMax=4G` for the run plus that build; swap covers the rest.

## Units

| Unit | What it does |
|---|---|
| `polytrader-paper@<stem>.timer` | starts the service daily at 00:00:30 UTC (`Persistent=true`: a missed start runs on boot) |
| `polytrader-paper@<stem>.service` | `paper-daily.sh`: skips sealed days (logs, exit 0), otherwise runs until 23:59:30 UTC (a late start just runs a shorter day), then `paper replay`, then writes `state/<stem>.status`. `Restart=on-failure` (60 s, at most 5 starts per hour) restarts a crashed run beside the earlier folder |
| `polytrader-paper-archive.timer/.service` | hourly `paper-archive.sh`: uploads finished runs (`parity.json` present, or older than 1 day) to `spaces:$SPACES_BUCKET/paper/<stem>/<day>/`, verifies with `rclone check`, deletes local `session/` of runs older than `PAPER_KEEP_DAYS` (2). Results, parity and markets files stay. Under `flock` |
| `polytrader-paper-watchdog.timer/.service` | every 5 min: `paper-watchdog.py` alerts only for a run that is not running when it should (00:05 to 23:55 UTC, not sealed), a log with no line for 10 min, processing lag p99 above 2 s for 10 min, disk above 85%, a finished run with parity not passed / `clean_stop` false / no parity after 3 h. Repeats hourly ("STILL FAILING"), then "RECOVERED" |

## Outputs

- `/opt/polytrader/paper/runs/<stem>/<YYYY-MM-DD>/`: the run folder (a restart the same day adds `<day>-<HHMMSS>`).
- `/opt/polytrader/paper/state/<stem>.status`: one line, e.g. `... done run_rc=0 replay_rc=0 parity=passed <dir>`.
- Logs: `journalctl -u polytrader-paper@<stem>`, `-u polytrader-paper-archive`, `-u polytrader-paper-watchdog`.
- Fetch on a workstation: `rclone copy spaces:$SPACES_BUCKET/paper/<stem>/<day> data/runs/...`.

## Operate

```
systemctl stop polytrader-paper@<stem>              # clean stop (SIGINT): drains, builds, writes results (up to 15 min)
systemctl disable --now polytrader-paper@<stem>.timer   # stop it from starting tomorrow
cat /opt/polytrader/paper/state/*.status
sudo -u polytrader python3 /opt/polytrader/app/scripts/paper/paper-watchdog.py --dry-run
```

A stop mid-day gives a run with `clean_stop` true; the timer starts a new run the next UTC day (or start one by hand
with `systemctl start`, which runs until 23:59:30). Never run on a sealed day: the script skips it.

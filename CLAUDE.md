# CLAUDE.md

Guidelines for AI assistants working on this codebase.

## Layout

- `src/recorder/`: market data recorder (TypeScript). Runs as the `polytrader-recorder` systemd service on
  the droplet. Every change affects live data collection, so keep it backward compatible and record format
  changes in `docs/RECORDER.md` (streams/columns + the data quality log).
- `analysis/`: `polydata` Python package (uv). Mirrors the Spaces bucket, builds Parquet and exposes
  DuckDB views. Column types live in `analysis/src/polydata/schema.py`; update it whenever the recorder
  adds a stream or column.
- `scripts/recorder/`: droplet scripts (`verify.py` data checks, `archive.sh` hourly Spaces upload).

## Rules

- **The repo is public.** Never commit secrets, IPs, hostnames or bucket credentials. They belong in
  `.env` (see `.env.example`).
- **Backtests must not look ahead.** Join on `recv_ms` (our receive time) with `ASOF JOIN`, not on
  exchange timestamps, and check the data quality log in `docs/RECORDER.md` for affected periods.
- Recorder TSV columns may only be **appended** (the analysis build fills missing columns with NULL for
  older files). Never reorder or rename them.
- Test SQL against real data before committing, e.g. `cd analysis && uv run polydata sql "..."`.
- After changing `analysis/`, run `cd analysis && uv run pytest`.
- After changing `src/recorder/`, run `npm run build`. Deploy steps are in `docs/RECORDER.md#operations`.

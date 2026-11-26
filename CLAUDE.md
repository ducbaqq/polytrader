# CLAUDE.md

Guidelines for AI assistants working on this codebase.

## Layout

- `src/recorder/`: market data recorder (TypeScript). Runs as the `polytrader-recorder` systemd service on
  the droplet. Every change affects live data collection, so keep it backward compatible and record format
  changes in `docs/RECORDER.md` (streams/columns + the data quality log).
- `analysis/`: `polydata` Python package (uv). Mirrors the Spaces bucket, builds Parquet and exposes
  DuckDB views. Column types live in `analysis/src/polydata/schema.py`; update it whenever the recorder
  adds a stream or column.
- `analysis/src/polydata/paper/`: paper runner (live feeds, simulated fills; guide `docs/PAPER.md`).
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

- **The paper runner never places real orders** (no keys, signing or order endpoints). Trust a paper result only after `polydata paper replay` parity passes.

## Backtester (`analysis/src/polydata/bt/`, `screen.py`)

Guide: `docs/BACKTESTER.md`. Journals: `docs/strategies/`.

- **Never tune on, or read, sealed days** (every third day from 2026-11-25; hidden in the views). Reading them
  needs `include_sealed=True` with a purpose and is logged as a look.
- **`--final` only after `bt validate` passes for the current `sim_version`**, and at most once per
  (strategy version, sim version, data). Each final run is a look that raises the bar for every strategy.
- **Every run that matters gets a line** in its strategy journal (`docs/strategies/<strategy>.md`), including
  failures and the variant count.
- **Run folders live under `data/runs`** (gitignored) and are shared with `polydata bt push`. Commit only
  journals, the glossary and `docs/runs-index.csv`.
- **A change to `bt/` simulator files, `bt/strategies/sanity.py`, `fees.py` or the data layer (`views.py`,
  `build.py`, `schema.py`) changes `sim_version`**, so re-run `bt validate` before trusting results or using
  `--final`.
- **Strategies use only `ctx`** and the handler arguments: no DuckDB, files, clocks or global state, so the same
  class can run in the live runner.
- A screen can only reject or promote; only a simulator run can pass.

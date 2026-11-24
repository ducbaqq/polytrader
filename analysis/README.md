# polydata: analysis toolkit

DuckDB over the recorder's data. `polydata` mirrors the Spaces bucket locally, converts it to typed
Parquet, and exposes every stream as a SQL view, from Python, Jupyter, the DuckDB web UI or the DuckDB
CLI.

## Setup on a new machine

Requirements: [uv](https://docs.astral.sh/uv/) (`brew install uv`). It installs the right Python and
every dependency; nothing else is needed. The `duckdb` CLI is optional (`brew install duckdb`).

```bash
git clone git@github.com:ducbaqq/polytrader.git && cd polytrader
cp .env.example .env          # fill in SPACES_BUCKET / SPACES_KEY / SPACES_SECRET
cd analysis
uv sync                       # creates .venv with duckdb, pandas, pyarrow, jupyter, ...
uv run polydata status --remote   # what is in the bucket vs local
uv run polydata update        # download everything + build Parquet (incremental, re-run any time)
uv run pytest                 # optional: checks the pipeline on synthetic data
```

Data goes to `<repo>/data/` (gitignored; override with `POLYDATA_DIR`). Expect about 9–11 GB/day raw
plus about 7–8 GB/day Parquet, and about 13 MB/s download from Singapore. To fetch only part of it:

```bash
uv run polydata update --days 2026-11-24                 # one day
uv run polydata update --days '2026-11-2*' --streams 'pm_*' rtds_price bn_fut_bbo
```

## Querying

```bash
uv run polydata sql "SELECT timeframe, count(*) FROM markets GROUP BY 1"
uv run polydata ui          # DuckDB web UI at http://localhost:4213 with all views loaded
uv run polydata notebook    # Jupyter Lab in analysis/notebooks (start with 00_quickstart.ipynb)
duckdb data/polytrader.duckdb   # DuckDB CLI; the file only holds views (rewritten by `build` / `polydata db`)
```

```python
import polydata
con = polydata.connect()
df = con.sql("SELECT * FROM pm_trade WHERE slug = 'btc-updown-5m-1795509000'").df()
```

Session time zone is UTC. Filter on `date` (a partition column, e.g. `WHERE date = '2026-11-24'`) or on
the instrument (`sym`, `asset`/`slug`, `symbol`) to read less data. Parquet files are sorted by
instrument, then `recv_ms`.

## Views

**Raw streams**, one per recorder stream, with the columns documented in [docs/RECORDER.md](../docs/RECORDER.md),
plus `date`:

| View | Notes |
|---|---|
| `bn_spot_trade`, `bn_spot_bbo`, `bn_spot_depth`, `bn_fut_trade`, `bn_fut_bbo`, `bn_fut_depth`, `bn_fut_mark`, `bn_fut_liquidation` | Typed TSV columns. Depth `bids`/`asks` are `price:qty\|…` strings; use `levels()` |
| `pm_book`, `pm_price_change`, `pm_best_bid_ask`, `pm_trade` | Plus `slug` and `outcome` (`Up`/`Down`) parsed from `asset` |
| `rtds_price` | `conn` is NULL before 2026-11-24 10:36 (single connection then) |
| `pm_api_trades` | Typed Data API trades: `slug, token_id, outcome, side, price, size, ts_ms, proxy_wallet, pseudonym, tx_hash, backfill` |
| `bn_depth_snapshot` | `market, sym, last_update_id, bids, asks` as `LIST(STRUCT(price, qty))` (1000 levels) |
| `pm_market_meta`, `pm_resolution`, `pm_new_market`, `pm_other`, `bn_fut_stats` | `recv_ms, d` (JSON); query with `d->>'field'` |

**Derived**

| View | What |
|---|---|
| `markets` | One row per Up/Down market: `coin, timeframe, window_start/end(_ms), up/down_token_id, condition_id, fee_*, twap_lookback_s, tick_size_at_discovery, outcome, price_to_beat, final_price, volume` |
| `chainlink` | Chainlink prices (the resolution source): `coin, ts_ms, price, recv_ms` |
| `pm_binance_relay` | Polymarket's relay of Binance prices: `coin, ts_ms, price` |
| `pm_tick_size_change` | Tick size changes (0.01 → 0.001 when the price nears 0 or 1) |
| `pm_market_resolved` | CLOB `market_resolved` broadcasts |
| `bn_open_interest` | Open interest per minute |
| `bn_fut_ratios` | Long/short account and position ratios, taker buy/sell, OI history (5-min) |

**Macros:** `levels(s)` turns a book string into `LIST(STRUCT(price, size))`, `ms(x)` converts ms to
TIMESTAMP, and `asset_slug(a)` / `asset_outcome(a)` parse an asset key.

## Rules for backtests

- **Avoid lookahead:** decide on what had arrived by then. Join on `recv_ms` (our receive time), not on
  exchange timestamps, and use `ASOF JOIN` (see the quickstart notebook).
- Check [the data quality log](../docs/RECORDER.md#data-quality-log) for periods with gaps or skewed
  `recv_ms`.
- `markets.price_to_beat` is NULL for 4h markets. Rebuild it from `chainlink` (mean over the 60 s
  before `window_start`), as in the notebook.

## How it works

`polydata update` = `sync` + `build`.

- `sync` downloads `raw/` and `catalog/` from the bucket. It skips files already present with the same
  size and writes through a temp file, so interrupted runs are safe.
- `build` converts each stream-hour to `data/parquet/<stream>/date=YYYY-MM-DD/HH.parquet`, recording
  the source files in `HH.src`. An hour is rebuilt only when its raw file list changes.
- `build` then writes `data/parquet/markets.parquet` and the views into `data/polytrader.duckdb`.
- Column types live in `src/polydata/schema.py`. When the recorder adds a stream or column, add it
  there; `build` refuses unknown columns.

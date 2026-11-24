# polytrader

Data and research platform for short-horizon trading on Polymarket's crypto **Up/Down** markets and
the underlying coins on Binance.

A recorder on a DigitalOcean droplet captures everything about these markets 24/7. An analysis
toolkit turns that data into DuckDB tables on any machine. Strategies and backtests will be built on
top of it.

```
 Polymarket CLOB / Gamma / Data API ─┐
 Polymarket RTDS (Chainlink prices)  ├─► recorder (droplet, Singapore) ─► 5-min zstd files ─► Spaces bucket
 Binance spot + USDⓈ-M futures      ─┘          │                                            │
                                                └─► Postgres (catalog, health)               ▼
                                                                         polydata: sync ─► Parquet ─► DuckDB views
```

## Repository

| Path | What |
|---|---|
| `src/recorder/` | Recorder (TypeScript, Node 20), run as the `polytrader-recorder` systemd service |
| `scripts/recorder/` | `archive.sh` (hourly upload to Spaces), `verify.py` (data-quality checks) |
| `analysis/` | `polydata` (Python, DuckDB): sync, Parquet build, SQL views, notebooks, tests |
| `docs/RECORDER.md` | Every stream and column, operations, data quality log |
| `analysis/README.md` | Views, query examples, backtest rules |

## What is recorded

- **Markets:** 37 series, BTC, ETH, SOL, XRP, DOGE, BNB, HYPE and ZEC × 5m / 15m / 1h / 4h / daily.
  Each market is recorded from before it opens until after it resolves.
- **Polymarket:**
  - Full order books, every price-level change, best bid/ask, trades.
  - Market metadata, resolutions with `priceToBeat`.
  - Every trade from the Data API, including the trader's wallet.
- **Chainlink:** about 1 price per second per coin. These are the prices the markets resolve on: the
  60-second TWAP, where `priceToBeat` is the mean over the 60 s before the window opens.
- **Binance spot and perpetual futures:**
  - Every trade, every top-of-book change, 100 ms depth diffs, 1000-level snapshots each minute.
  - Mark/index/funding prices.
  - All liquidations, open interest, long/short and taker ratios.

The recorder produces about 11 GB/day compressed. Files are kept on the droplet for 3 days and kept
permanently in the Spaces bucket.

## Analysis setup

Needs [uv](https://docs.astral.sh/uv/) (`brew install uv`); it installs Python and all dependencies.

```bash
git clone git@github.com:ducbaqq/polytrader.git && cd polytrader
cp .env.example .env               # fill in SPACES_BUCKET, SPACES_KEY, SPACES_SECRET
cd analysis
uv sync                            # Python env with DuckDB, pandas, pyarrow, Jupyter
uv run polydata status --remote    # what's in the bucket vs. local
uv run polydata update             # download + convert to Parquet (incremental, re-run any time)
```

Then query:

```bash
uv run polydata sql "SELECT timeframe, count(*), avg((outcome = 'Up')::INT) FROM markets GROUP BY 1"
uv run polydata notebook           # Jupyter, start with notebooks/00_quickstart.ipynb
uv run polydata ui                 # DuckDB web UI at http://localhost:4213
```

```python
import polydata
con = polydata.connect()           # every stream as a view + markets, chainlink, ...
con.sql("SELECT * FROM pm_trade WHERE slug = 'btc-updown-5m-1795509000'").df()
```

- **Storage:** about 9–11 GB/day raw plus 7–8 GB/day Parquet in `data/` (gitignored). Use
  `--days 2026-11-24` or `--streams 'pm_*' rtds_price` to fetch less.
- **Speed:** queries over hundreds of millions of rows take about a second.
- **Tests:** `uv run pytest` checks the pipeline on synthetic data, with no credentials needed.

## Backtesting rules

- **No lookahead:** join on `recv_ms` (when we received the data) with `ASOF JOIN`, never on exchange
  timestamps.
- Check the [data quality log](docs/RECORDER.md#data-quality-log) for affected periods. Data from
  2026-11-23 16:50–17:46 UTC is development data.
- Taker fees apply on 5m/15m markets (`markets.fee_rate`); makers get rebates.
- 4h markets have no `price_to_beat`; rebuild it from `chainlink`.

## Recorder

```bash
npm ci && npm run build            # needs Node 20 and zstd on PATH
npm run recorder                   # DATABASE_URL optional (catalog/health tables)
```

Deployment, the systemd units and day-to-day operations are covered in
[docs/RECORDER.md](docs/RECORDER.md#operations). Check the live recorder with
`python3 /opt/polytrader/app/verify.py /opt/polytrader/data --since-minutes 60` on the droplet.

## Infrastructure

| Piece | Purpose |
|---|---|
| Droplet (Singapore, 1 vCPU / 2 GB / 70 GB) | Runs the recorder and the hourly archive timer |
| Spaces bucket (sgp1, private) | Permanent archive: `raw/YYYY-MM-DD/HH/…`, daily `catalog/` exports |
| Managed Postgres | `rec_markets`, `rec_stats`, `rec_events`: market catalog and recorder health |

Credentials live only in `.env` (see `.env.example`). This repository is public, so never commit
secrets, IPs or hostnames.

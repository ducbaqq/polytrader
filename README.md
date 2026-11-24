# polytrader

Research infrastructure for short-horizon strategies on Polymarket's crypto **Up/Down** markets
(5m / 15m / 1h / 4h / daily) and the underlying coins on Binance.

| Part | Where | What |
|---|---|---|
| Recorder | `src/recorder/` (TypeScript, Node 20) | Runs 24/7 on a droplet. Records the Polymarket order books, trades, metadata and resolutions; the Chainlink prices markets resolve on; and Binance spot and futures trades, top of book, depth, mark price, liquidations and stats. Writes 5-minute zstd files and archives them hourly to DigitalOcean Spaces. |
| Analysis | `analysis/` (Python, DuckDB) | `polydata` syncs the bucket, converts it to Parquet and exposes every stream as a SQL view; notebooks |
| Ops scripts | `scripts/recorder/` | `verify.py` (data-quality checks on the droplet), `archive.sh` (hourly upload to Spaces) |

Docs: [docs/RECORDER.md](docs/RECORDER.md) covers every stream and column, operations and the data
quality log. [analysis/README.md](analysis/README.md) covers analysis setup, views and backtest rules.

## Analysis quick start

```bash
cp .env.example .env            # SPACES_* credentials
cd analysis && uv sync
uv run polydata update          # sync + build Parquet
uv run polydata notebook        # or: uv run polydata ui / uv run polydata sql "..."
```

## Recorder

```bash
npm ci && npm run build
npm run recorder                # needs zstd on PATH; DATABASE_URL optional
```

Deployment and operations are covered in [docs/RECORDER.md](docs/RECORDER.md#operations).

# Paper runner (`polydata paper`)

Runs a `Strategy` class against the **live** Polymarket, Binance and Chainlink feeds with simulated fills. No
real orders: nothing authenticates, signs or calls an order endpoint; feeds are public WebSocket/REST only.
Design: [the spec](superpowers/specs/2026-11-25-paper-runner-design.md) (see its "Changes during
implementation"). Engine and strategy interface: [BACKTESTER.md](BACKTESTER.md).

## Run

```
cd analysis
caffeinate -i uv run polydata paper run path/to/config.yaml --duration 6h      # Ctrl-C stops cleanly
uv run polydata paper replay data/runs/<family>/<strategy>/<run>               # parity check
```

`paper run CONFIG.yaml` flags (see `--help`):

| Flag | Meaning |
|---|---|
| `--out DIR` | run folder (default `data/runs/<family>/<strategy>/<time>-<id>`) |
| `--duration` | e.g. `30m`, `6h`; default until Ctrl-C |
| `--coins`, `--set KEY=VALUE` | override the config |
| `--hold-back-ms` (10000), `--skew-ms` (1000), `--warmup-s`, `--max-drain`, `--confirm-wait` | timing knobs |
| `--allow-sealed --purpose "..."` | allow a sealed day; records a look only if one is reachable |
| `--update-docs-index` | also rewrite the committed `docs/runs-index.csv` (default: `data/runs/index.csv` only) |
| `--fake-from TIME [--fake-to TIME]`, `--fake-session DIR`, `--speed N\|max` | offline fake feed (recorder store or a session) |

Polymarket may be unreachable from some networks; the runner then just reports connection errors. Keep the
machine awake (`caffeinate -i`) for long runs. Sealed days are refused by default.

## Hold-back

The strategy clock runs `hold_back_ms + skew_ms` (default 10 s + 1 s) behind wall time. Rows with exchange
`ts <= wall - hold_back_ms` are released in order, so the live core sees exactly what a replay of the session
sees and parity is exact. Being late costs nothing in paper (no real orders), but it means a paper run cannot test
reaction speed. The 10 s default comes from measurement: with 3 s, a quarter of BTC 5m markets had late rows.

## Run folder

`data/runs/<family>/<strategy>/<time>-<id>/`:

| File | Contents |
|---|---|
| `config.yaml`, `provenance.json` | config plus `paper:` block; commit, `sim_version`, `paper_version`, `validated`, host hash |
| `results.json` | bt summary keys plus feed delay, lag, gaps, voids, outcome mismatches, `pnl_usd_gamma`, `parity` |
| `orders.parquet`, `fills.parquet`, `equity.parquet`, `markets.parquet` | bt schemas (markets add outcome source and Gamma columns) |
| `decisions.parquet` | trace of every strategy call (`now`, handler, args, return) |
| `log.txt` | strategy `ctx.log` lines and runner status lines (prefixed `#`) |
| `session/` | `POLYDATA_DIR`-shaped store: `manifest.json`, `raw/` (recorder layout), `catalog/rec_events.csv.zst`, `late/` (dropped rows), `paper/gamma_resolution.jsonl.zst`, `parquet/` (built on demand) |
| `parity/`, `parity.json` | written by `paper replay` |

Sessions run to about 130 to 230 MB/hour, so `bt push` skips `session/` unless `--with-sessions`.

## Parity and `paper replay`

`paper replay RUN_DIR` builds `session/parquet` from the raw session rows and re-runs the simulator over it in
session mode (the same admitted markets at their admission times, the run's config and seed). Parity holds when,
per budget, decisions, orders, fills, equity, market rows and strategy log lines are identical (no tolerance).
Verdicts: `passed`; `passed-truncated` (hard stop or crash: compared up to the last flushed row); `informational`
(replay on a different CPU architecture, tolerance reported, not a pass). `--keep-parquet` keeps the built
Parquet. **A paper result is not to be trusted until parity passes.**

`--vs-recorder` also replays the same window on the recorder's store (the recorder's receive times) and reports the
differences in decisions, fills and P&L. It measures location and feed effects and is never a pass/fail.

## Voids

A market is voided (excluded from P&L reporting; cash it already moved still counts) when a Binance gap (> 1 s) or
Polymarket gap (> 5 s) overlaps its window, or a late row (exchange `ts < window_end`, `recv >= open`) arrives
after the market opened. Late rows for a market not yet opened are accepted. Voids and late rows are listed in
`results.json`; check them before reading P&L.

## Fake feed

`--fake-from` / `--fake-session` replay stored rows through the live code path (hold-back, writer, voids), for
testing without a network. Offline lag figures are not meaningful.

## Settlement

Markets settle on the Chainlink price, then are confirmed against Polymarket's outcome (Gamma) for up to
`--confirm-wait`. Near-tie disagreements are kept as Chainlink outcomes (as in bt) and reported:
`pnl_usd` is Chainlink-settled, `pnl_usd_gamma` is restated with Polymarket's outcome. Compare both.

## Limitations

- **Fills are simulated** with the bt fill model, which is unverified against real orders.
- **Timing differs from the recorder's host.** The paper host receives feeds at different times than the host that
  recorded the backtest data; `--vs-recorder` quantifies it.
- **250 ms taker delay** that Polymarket applies on some markets is not modelled (Polymarket docs, clob-markets
  `itode`). Taker results are optimistic and market-making fill assumptions may change.
- **Chainlink prices** come from the legacy RTDS feed. The PolyBolt key requirement from 2026-11-30 means
  the feed needs replacing (see [TODO](TODO.md)).
- **End-of-run memory spike:** the in-process session build at the end of a 6 h run peaked near 7 GB RSS (trading
  itself stays at 0.6 to 1.4 GB). Planned: build in a subprocess.
- **Binance-gap voids hit strategies that do not read Binance** (quiet-symbol silences of 1 to 1.6 s void their
  markets). Pending the `gaps.py` change.
- **Voided markets still move cash**; the headline P&L includes them unless you filter.

## First runs

A 1 h and a 6 h paper run of `endgame-maker-v2` (journal: [strategies/endgame-maker.md](strategies/endgame-maker.md)).
The 6 h session passed parity (2.43M decisions, 0 mismatches). That shows the runner reproduces its own session;
it is **not evidence of edge** (6 h P&L: -$6.64 Chainlink-settled, +$13.36 with Polymarket outcomes).

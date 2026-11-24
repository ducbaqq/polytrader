# To do

Path from the data we're collecting to tested strategies. Update this file as items are done.

## Done

- [x] Recorder live 24/7 on the droplet, archived hourly to Spaces (2026-11-23)
- [x] `polydata`: sync, Parquet, DuckDB views, notebooks (2026-11-24)
- [x] Telegram alerting, actionable problems only (2026-11-24)
- [x] Fee formula verified on-chain: [FEES.md](FEES.md) (2026-11-24)
- [x] Calibration & fair-value study: [research/calibration-study.md](research/calibration-study.md) (2026-11-24).
  Conclusions: market making is the lead strategy; makers must react to Binance moves within
  ~100–200 ms; taker mispricing and the latency trade (from Singapore) are not worth pursuing.

## Next

### 1. Backtester, market making first (done 2026-11-25)
- [x] Design: strategy interface, configuration, reports (2026-11-25): [the spec](superpowers/specs/2026-11-24-backtester-design.md)
- [x] Event replay ordered by `recv_ms` across Polymarket, Binance and Chainlink (no lookahead) (2026-11-25)
- [x] Order book reconstruction from `pm_book` + `pm_price_change`, validated against `pm_best_bid_ask` (2026-11-25)
- [x] Fill model: back-of-queue makers, trade-through capped, 150 ms trade echo, taker walks with impact,
  minimum size, tick changes (2026-11-25)
- [x] Costs: `taker_fee()`, no maker fee, 0% rebates, settlement at resolution (2026-11-25)
- [x] Reports: verdict, attribution, by market, realism, latency sensitivity (2026-11-25)
- [x] Run folders, runs index, journals, `bt push` / `pull`, sealed day splits (2026-11-25)
- [x] Simulator validation suite and the screening tier `polydata.screen` (2026-11-25)
- [x] Baseline `mm-binance-pull` and the sanity strategies (2026-11-25)
- [x] `gaps` handling: windows with recorder gaps are voided (2026-11-25)

Guide: [BACKTESTER.md](BACKTESTER.md).

### 1a. Strategy research loop (next)
- [ ] Run `mm-binance-pull` on train and validation days as data accumulates; sweep with `bt sweep`; log every
  variant in [strategies/mm-binance-pull.md](strategies/mm-binance-pull.md).
- [ ] Screen taker families (endgame, momentum, order-flow, cross-timeframe) with `bt screen`; promote the
  survivors to strategy classes.
- [ ] First `--final` run once at least 5 sealed days exist and `bt validate` passes.
- [ ] Rerun the calibration study at 7 and 14 days.

### 1b. Data and servers (from the backtester design review, 2026-11-24)
In this order:
- [ ] Locate Polymarket's matching engine: run short probes in a few regions and compare the minimum
  `recv_ms − ts_ms` on the Polymarket feed (no orders needed). Unverified assumption: Binance
  matches in Tokyo, Polymarket's order book in London.
- [ ] Second recorder in the region we'd trade from: at least the Polymarket streams and Binance top
  of book. One box in Singapore is a single point of failure, gaps are permanent, and every backtest
  is tied to Singapore's receive times.
- [ ] More feeds that can't be fetched later, by value:
  - Coinbase and one more perp venue: top of book and trades
  - Deribit implied volatility, for the fair-value model
  - Kalshi's short-dated crypto markets, for a cross-venue strategy family
  - On-chain fills (maker and taker wallets) can be fetched from the chain later; not urgent.
- [x] `gaps` from `rec_events` / `rec_stats` / the data quality log (2026-11-25)

### 2. Strategy research (when there are about 7–10 days of data)
- [ ] Market making (lead candidate): quote in tight books, pull or reprice quotes on Binance moves,
  inventory limits. Measure what *we* would earn with a back-of-queue fill model.
- [ ] Rerun the calibration study at 7 and 14 days: `uv run python -m polydata.research.study`.
- [ ] Find where Polymarket's matching engine is hosted (round-trip ~200 ms from Singapore). A
  server near it would speed up maker cancels and could revive the BTC 5m latency trade.
- [ ] Wallet study (`pm_api_trades`): which wallets consistently profit, and what they do (timing,
  prices, side, size).
- [ ] Set pass criteria before testing:
  - positive after fees out of sample (tune on early days, test on later days)
  - holds across most coins and days
  - t-statistic above about 3
  - enough fillable size

### 3. Paper trading (next after a candidate passes)
- [ ] Live/paper runner that runs the same `Strategy` classes against the live feeds; `orders.parquet` has the same schema as the simulator's.
- [ ] Minimum-size (5 share) real orders to verify the fill model (removes "Fill model: unverified").
- [ ] Run the best one or two candidates live on the droplet, simulated against the live book.
- [ ] Compare paper fills with what the backtest predicted for the same period.

## Small / optional
- [ ] healthchecks.io dead-man's switch (`HEALTHCHECK_URL`), so the droplet dying alerts Telegram.
- [ ] `polydata build --prune-raw` to keep only Parquet locally (saves about 250 GB per two weeks).
- [ ] Reconnect logic: don't reconnect a single-market Polymarket connection after its market
  resolved. Cosmetic; it only adds code=1000 noise.

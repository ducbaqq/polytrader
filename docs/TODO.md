# To do

Path from the data we're collecting to tested strategies. Update this file as items are done.

## Done

- [x] Recorder live 24/7 on the droplet, archived hourly to Spaces (2026-11-23)
- [x] `polydata`: sync, Parquet, DuckDB views, notebooks (2026-11-24)
- [x] Telegram alerting, actionable problems only (2026-11-24)
- [x] Fee formula verified on-chain: [FEES.md](FEES.md) (2026-11-24)

## Next

### 1. Fair-value and calibration study (about 1 day; start on the first 2–3 days of data)
- [ ] Fair-value model for P(Up), from:
  - distance to `priceToBeat` (in volatility units)
  - time left in the window
  - realized and implied Binance volatility
  - the 60-second TWAP settlement: the closing average dampens the last minute's moves
- [ ] Calibration: bucket Polymarket mid prices by time-to-end and compare with actual outcomes.
  Is the market over- or under-confident, and where?
- [ ] Mispricing map: where does |model − market| exceed fee plus half the spread? How often, how
  long does it last, and how much size is available?
- [ ] Use the result to choose which strategy to build first: lead-lag, market making, or mispricing.

### 2. Backtester (a few days; design discussion with you first)
- [ ] Design: the strategy interface (events in, orders out), the configuration, and the reports
  wanted.
- [ ] Event replay ordered by `recv_ms` across Polymarket, Binance and Chainlink streams (no
  lookahead).
- [ ] Polymarket order book reconstruction from `pm_book` + `pm_price_change`, validated against
  `pm_best_bid_ask`.
- [ ] Fill model:
  - taker orders walk the book after a latency delay
  - maker orders fill only when a later trade trades through our price (conservative; queue
    position is unknown)
  - minimum order size of 5 shares
  - tick-size changes
- [ ] Costs: `taker_fee()`, no maker fee, 0% rebates. Settlement at resolution.
- [ ] Reports:
  - P&L per trade, win rate and drawdown
  - results per coin, timeframe and day
  - capacity: size actually fillable
  - sensitivity to latency and fill assumptions

### 3. Strategy research (when there are about 7–10 days of data)
- [ ] Lead-lag: how fast the Polymarket book reacts to Binance moves, and whether the delay is worth
  more than fee plus spread.
- [ ] Market making: quote around fair value, widen or pull quotes on volatility, track inventory.
  Makers pay no fees, which is a large advantage on BTC 5m/15m.
- [ ] Wallet study (`pm_api_trades`): which wallets consistently profit, and what they do (timing,
  prices, side, size).
- [ ] Set pass criteria before testing:
  - positive after fees out of sample (tune on early days, test on later days)
  - holds across most coins and days
  - t-statistic above about 3
  - enough fillable size

### 4. Paper trading
- [ ] Run the best one or two candidates live on the droplet, simulated against the live book.
- [ ] Compare paper fills with what the backtest predicted for the same period.

## Small / optional
- [ ] healthchecks.io dead-man's switch (`HEALTHCHECK_URL`), so the droplet dying alerts Telegram.
- [ ] `polydata build --prune-raw` to keep only Parquet locally (saves about 250 GB per two weeks).
- [ ] Reconnect logic: don't reconnect a single-market Polymarket connection after its market
  resolved. Cosmetic; it only adds code=1000 noise.

# mm-location (measurement): does the recording/trading location change the market maker?

**Family:** market-making | **Kind:** location measurement (not a strategy) | **Status:** done
Strategies: [mm-binance-pull](mm-binance-pull.md) and [mm-side-pull](mm-side-pull.md), default params, unchanged code.

## Pre-registration (written before any run)

**Hypothesis.** The market maker's adverse selection (about -1.6 c/share at the touch in the Singapore-timed
backtests, ~2/3 of it inside the last 200 ms before the fill, see [adverse-flow](../research/adverse-flow.md)) is
largely a latency artefact of the Singapore location. Polymarket's matching engine is near London: the Polymarket
feed arrives ~9 ms after the exchange in London against ~126 ms in Singapore, and London order round trips are
short. With London feed timing and London order latency, adverse selection should shrink enough that the spread
capture (spread + 10 s adverse, "markout") is positive.

**Design.** Same strategy code and default params, same markets (BTC and ETH, 5m and 15m), same window
[00:01, 06:06) UTC on train day 2026-11-27, two data sources:

| Source | Store | recv_ms | order_latency_ms | fill_report_latency_ms |
|---|---|---|---|---|
| Singapore | recorder store (`$POLYDATA_DIR`), clipped to the window | Singapore receive times | 200 (as all past runs), 50 | 126 (default) |
| London | the paper session recorded in London with a noop strategy (`<session>/parquet`), replayed with `engine.run_session` (the session's admitted markets and times) | London receive times | 20 (optimistic), 50, 100 (pessimistic) | 10 (London feed delay) |

Binance arrives later in London (~120 ms vs ~35 ms in Singapore); that is part of the realism and is kept.
`fill_report_latency_ms` follows the location's Polymarket feed delay (it is the same channel); it is fixed per
location, not a tuned knob.

**Primary metric.** Markout per share (spread + 10 s adverse, `bt.metrics.attribution`, c/share) and adverse
c/share, with 15-minute-block bootstrap CIs, compared across source x latency on the markets that are not voided in
either source. Also reported: fills, P&L, full attribution, and the adverse split at the fill (mid at the fill -
mid 200 ms before) vs after (mid 10 s later - mid at the fill).

**Variants.** 2 strategies x 5 (source, latency) = 10 runs. No parameter is tuned.

**Honesty.** One train day, ~6 h, 2 coins. This is a location measurement, not a strategy pass.

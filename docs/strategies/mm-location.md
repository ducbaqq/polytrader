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

## Method as run
- Data: Singapore = recorder store after `polydata update --days 2026-11-27`; London = `<session>/parquet` of the
  London noop paper session. Window [00:01, 06:06) UTC from the session manifest (`rs`, `end_ms`), 188 markets
  in both sources.
- Singapore: `engine.run_coin(..., clip=(rs, end_ms))` per coin (what `paper replay --vs-recorder` does).
  London: `engine.run_session` with the manifest's admitted markets. sim_version is the current main's; no
  simulator file was touched. Script and voids list were scratch files (not committed); run folders are under
  `$POLYDATA_DIR/runs/market-making/<strategy>/`.
- Comparison set: the 178 markets voided in neither source (Singapore engine voids: 4, Binance gaps 1.5 s;
  London `paper.voids.session_voids`: 10, Binance gaps 1.5-2.3 s, one Polymarket gap 11.8 s, late rows).
- Attribution as `bt.metrics.attribution` (mid_before = mid 200 ms before the fill, the engine's definition).
  Adverse is split into "at the fill" (mid at the fill - mid 200 ms before) and "after" (mid 10 s later - mid at
  the fill). CIs: 15-minute blocks by market `window_start_ms`, 2000 resamples, seed 0 (ratio of sums for the
  per-share figures; `metrics.block_stats` for P&L). 22-23 blocks.
- Data completeness: the engine reads `pm_*`, `bn_fut_bbo`, `bn_fut_trade` and `rtds_price`; the London session
  has all of them (and `pm_api_trades`). It lacks Binance spot and depth streams, which neither strategy uses.
  Nothing the comparison needs is missing.

## Results (2026-11-27 00:01-06:06 UTC, BTC+ETH 5m/15m, 178 common markets)
c/sh = cents per share. Markout = spread + adverse (10 s).

| Strategy | Source | Order latency | Run | Fills | P&L [95% CI] | Spread | Adverse [95% CI] | at fill / after | Inventory | Markout [95% CI] |
|---|---|---|---|---|---|---|---|---|---|---|
| mm-binance-pull | Singapore | 200 | `2026-11-27T0723-f60ff8` | 746 | -$125.7 [-228, -31] | +0.50 | -1.95 [-2.96, -0.92] | -1.34 / -0.61 | -0.82 | -1.45 [-2.42, -0.47] |
| mm-binance-pull | Singapore | 50 | `2026-11-27T0724-7b80c0` | 667 | -$125.4 [-212, -51] | +0.91 | -2.52 [-3.60, -1.30] | -1.40 / -1.12 | -0.96 | -1.61 [-2.63, -0.41] |
| mm-binance-pull | London | 20 | `2026-11-27T0718-14b1f7` | 491 | +$37.2 [-16, 90] | +1.09 | -1.30 [-2.14, -0.34] | -0.73 / -0.56 | +1.20 | -0.20 [-1.04, +0.78] |
| mm-binance-pull | London | 50 | `2026-11-27T0717-400031` | 596 | -$12.4 [-50, 25] | +1.10 | -1.88 [-2.70, -1.02] | -0.92 / -0.96 | +0.52 | -0.78 [-1.62, +0.09] |
| mm-binance-pull | London | 100 | `2026-11-27T0719-f2dd51` | 615 | -$2.6 [-43, 43] | +1.08 | -1.98 [-2.68, -1.26] | -1.14 / -0.84 | +0.84 | -0.90 [-1.61, -0.19] |
| mm-side-pull | Singapore | 200 | `2026-11-27T0724-35a8b8` | 238 | -$9.3 [-79, 65] | +0.65 | -0.47 [-2.61, +2.01] | -1.13 / +0.66 | -0.67 | +0.18 [-2.04, +2.66] |
| mm-side-pull | Singapore | 50 | `2026-11-27T0725-48b3f1` | 219 | -$10.9 [-82, 65] | +0.90 | -1.33 [-3.14, +0.60] | -1.16 / -0.18 | -0.21 | -0.43 [-2.28, +1.53] |
| mm-side-pull | London | 20 | `2026-11-27T0721-aa7fee` | 229 | -$4.9 [-53, 39] | +1.07 | -1.27 [-2.57, +0.08] | -0.67 / -0.60 | -0.07 | -0.20 [-1.49, +1.13] |
| mm-side-pull | London | 50 | `2026-11-27T0722-2399cb` | 263 | +$13.5 [-34, 62] | +1.11 | -0.94 [-2.37, +0.60] | -0.88 / -0.06 | +0.50 | +0.17 [-1.25, +1.70] |
| mm-side-pull | London | 100 | `2026-11-27T0723-43eefe` | 258 | +$27.2 [-20, 82] | +1.08 | -0.84 [-2.56, +0.67] | -1.20 / +0.35 | +1.11 | +0.23 [-1.49, +1.76] |

Variants: 10 runs (2 strategies x 5 source/latency cells), all pre-registered, none tuned. Fees are 0 (maker).

## Reading
- **Location helps mm-binance-pull's markout, but it does not turn positive.** At the same 50 ms order latency,
  London timing moves markout from -1.61 to -0.78 c/sh: the spread earned per fill rises (+0.91 to +1.10 c; the
  quote sits on a fresher book) and adverse shrinks (-2.52 to -1.88 c). Only the optimistic 20 ms cell is near
  zero (-0.20 c, CI [-1.04, +0.78]); 50 and 100 ms stay negative (100 ms CI excludes 0).
- **The "at the fill" part is where the latency shows.** In London it grows with order latency (-0.73, -0.92,
  -1.14 c at 20/50/100 ms); in Singapore it is about -1.35 c at both 50 and 200 ms. With a 126 ms-old Polymarket
  feed, faster orders do not help: the information is stale before the order leaves. That supports the mechanism
  (the jump at the fill is partly a latency effect) but not its size: even at 20 ms, -0.73 c is lost at the fill
  instant, and adverse (-1.30 c) still exceeds spread (+1.09 c).
- **mm-side-pull:** ~230 fills, every CI is about +-1.5 c wide and spans 0 in all five cells. London cells are
  slightly better (markout -0.20 / +0.17 / +0.23 vs +0.18 / -0.43), with no latency gradient. Uninformative at this
  sample size.
- **P&L is mostly inventory (settlement coin flips):** the Singapore-London P&L gap (~$110-160 for
  mm-binance-pull) is mainly the inventory term (-0.8 c vs +0.5 to +1.2 c per share), not spread capture. Do not read
  the London P&L as an edge.

## Verdict
Hypothesis **partly supported, not confirmed**: London timing removes about half of mm-binance-pull's negative
markout (-1.6 to -0.8 c/sh at equal 50 ms latency, -0.2 c at an optimistic 20 ms), but spread capture is not
positive at any realistic latency and no CI is above 0. Market making at the touch stays a loser in simulation;
location is a necessary condition for any MM idea, not a sufficient one. Not a strategy pass (train day,
~6 h, 2 coins).

## Caveats
- One train day, ~6 h overnight UTC (Asia hours), 2 coins, 178 markets, 22-23 blocks; 200-750 fills per cell.
- The London cells differ from Singapore in two ways at once: feed timing (Polymarket earlier, Binance ~85 ms
  later) and the data itself (a separately recorded stream: different message arrival, gaps and late rows; session
  mode admits markets at their recorded admission times). The 50 ms pair isolates order latency but not these.
- `fill_report_latency_ms` is 10 in London vs 126 in Singapore (fixed per location before running).
- The fill model is unverified against real orders, and queue position uses the pessimistic `behind` model.
  Polymarket's 250 ms taker delay on some markets is not modelled; it would delay takers hitting us, likely
  helping a fast maker, so the London numbers could be optimistic or pessimistic.
- London order latency (20-100 ms) is assumed, not measured.

## Next
- Measure the real London order round trip (a single post/cancel against the API, no fills) before trusting any
  latency cell.
- Repeat on more train days recorded in London (only one London session exists).

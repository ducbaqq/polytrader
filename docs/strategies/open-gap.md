# open-gap (stale strike at window open)

**Family:** fair-value | **Status:** abandoned (screen)
**Current version:** screen only (no simulator strategy) | **sim_version:** n/a (no sim run; the screen reads books as of `recv_ms`)

## Pre-registration (written before any number was looked at)

**Mechanism.** A window's strike K is the Chainlink 60 s TWAP of the minute before the window starts, so after a
trend into the open the current price S already differs from K and the fair P(Up) at t = 0 is not 0.5. K is only
knowable once the first Chainlink tick with ts at or after the window start has arrived (the simulator's
`price_to_beat` reveal rule, `docs/BACKTESTER.md`). Hypothesis: in the first seconds the book still sits near 50c
(makers anchored on 0.5 or not yet requoted), so the favoured token's ask is below fair. Who loses: slow makers.

**Pricing rule (fixed, not tuned).** K = mean of the Chainlink ticks (deduplicated on `ts_ms`) with ts in the 60 s
before the window start, at least 20 ticks; reveal = `recv_ms` of the first tick with ts >= window start. At
t = reveal + offset, offset in {0, 2, 5, 10, 20, 30} s: fair P(Up) is the tau > 60 branch of `bt/fairvalue.py`
(S = last Chainlink price nowcast with the Binance futures return since that tick, sigma = 1.2 x sig15, tau - 40).
Books are the last `pm_best_bid_ask` with `recv_ms` <= t; "tight" means both tokens have spread <= 3c.

**Screen.** All coins, 5m + 15m, every window. Signal: buy the favoured token (fair >= 0.5 -> Up) if
fair - ask - fee(ask) > margin. Execution: the order arrives at t + latency (200 ms, and 50 ms for Amsterdam),
is held 250 ms, and fills at the ask of that time (the arrival ask, not the signal ask; fee at the arrival ask).
Taker fee rate per market (0.05 p(1-p) BTC 5m/15m, 0.01 elsewhere). Margin in {0, 2, 4, 6}c.
Sizes at the ask are not in `pm_best_bid_ask`; they would be read from the simulator if the screen passed.

**Primary metric.** Settlement markout (outcome - arrival ask - fee) in c/share on 2026-11-27 for the (offset,
margin) chosen on 2026-11-24, 95% CI from a bootstrap over 15-minute blocks. Secondary: mid at +60 s minus fill
price and fee, and the model edge (fair - arrival ask - fee). Gate to build the strategy: held-out 50 ms
markout positive and not clearly negative.

**Variants.** 1 rule x 6 offsets x 4 margins x 2 latencies = 24 reported cells per day (latency changes almost
nothing). No sealed or validation (11-26) day read.

## Results

Train 2026-11-24 (windows from 10:36 UTC) and 2026-11-27 (held out, data through ~10:00 UTC). Script:
`research/open_gap/screen.py`.

**Is the open mispriced? No: the book is already past the model.** At the reveal, windows with tight books
(372 on 11-24, 350 on 11-27, of about 1,700 and 1,300 5m+15m windows; the rest lack tight books or K at the open):

| | 11-24 | 11-27 |
|---|---|---|
| mean abs(fair - 0.5) at reveal | 0.058 | 0.057 |
| mean abs(Up mid - 0.5) at reveal | 0.079 | 0.082 |
| mean fair - ask - fee, favoured token | -2.8c | -3.0c |

By |fair - 0.5| bucket the gap stays negative (-2.3c, -3.3c, -3.6c for 0-5, 5-10, 10-20c on 11-24; 11-27 alike
except the 6-window 20-30c bucket). The mid is already ~2c beyond the model's favoured side: makers do see the new
strike (they quote off the same Chainlink stream) and the favoured ask is above fair by roughly the fee plus the
half spread. Reprice speed: there is nothing to reprice; the book/model gap does not change from +0 to +30 s
(-2.6c to -2.9c on 11-24), only the deviations grow as the price moves.

**Executable taker (settlement markout, c/share, [CI], n; the model edge in the last column is what the signal
promises).** Most cells are negative or zero; margin 6c is positive only at +0 s and +2 s on 11-24 (n 11 and 29, CIs of about
+-25c) and negative at +10 to +30 s. The cell chosen on 11-24 by best settlement and +60 s markout together:
offset +2 s, margin 6c.

| Day | Latency | Cell | n | Settlement c/sh [95% CI] | +60 s mid c/sh | Model edge c/sh |
|---|---|---|---|---|---|---|
| 11-24 (chosen) | 200 ms | +2 s, 6c | 29 | +12.5 [-7.1, 30.9] | +9.1 | 6.6 |
| 11-24 | 50 ms | +2 s, 6c | 29 | +12.4 [-7.3, 30.9] | +9.2 | 6.5 |
| 11-27 (held out) | 200 ms | +2 s, 6c | 26 | +11.6 [-13.9, 32.4] | -0.1 | 9.5 |
| 11-27 (held out) | 50 ms | +2 s, 6c | 26 | +11.7 [-14.0, 32.5] | +0.2 | 9.7 |

Unfiltered, the same signal at margin 0 loses on both days (offsets 0-30 s: -9 to -1c/sh on 11-24, -2c/sh on
11-27 at 0-5 s) with +60 s markouts around 0. Other 11-24 cells at margin 4-6c flip sign between offsets (e.g.
+20 s: -27c [-45, -7] at 200 ms).

## Verdict

**Abandoned; no strategy or simulator run.** The mechanism is not there: books are not stale at the open. The one
cell that is positive on both days (+2 s, 6c) is the best of 24 cells picked on 29 trades; its CI spans 0 by about
+-20c on both days, the +60 s mark on the held-out day is flat (-0.1c, so the book does not move towards the
model after we buy), and the signal that selects it has a mean gap of -3c over all tight windows. The
literal gate (50 ms held-out markout positive and not clearly negative) is met on settlement only through 26
trades whose win rate is noise-sized, so it was not treated as a pass.

## Lessons

- Makers re-anchor to the new strike within the first Chainlink tick: at reveal the book mid is already further
  from 0.5 than the model fair.
- At the open the Polymarket books are often wide (a large share of windows fail the 3c tight-book filter), so
  the executable set is small even before the effect: 26-29 trades per cell per day.
- Sizes at the ask are unmeasured here; irrelevant given the verdict.
- A re-test would need more days (n ~ 500 trades for a +-3c CI) and a trigger not chosen from the 24-cell grid.

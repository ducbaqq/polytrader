# prestart (are Up/Down markets mispriced before their window starts?)

**Family:** fair-value | **Status:** abandoned (screen)
**Current version:** screen only (no simulator strategy) | **sim_version:** n/a (books read as of `recv_ms`; no sim run)

## Pre-registration (written before any number was looked at)

**Mechanism.** A market is quoted about two windows ahead (5m: first book a median 5.7 min before the start, range
2-10 min; 15m: median 14.9 min, range 6-28). Its strike K is the Chainlink 60 s TWAP of the minute before the start,
unknown until then, so before the start fair P(Up) is about 0.5, tilted only by the part of K already seen and the
current price. Hypothesis: pre-start books sometimes offer a side below fair (ask < 0.50 - fee - margin), so a taker
buy, or a resting bid at 0.48-0.50, earns the gap. Who loses: makers who have not yet quoted tightly.

**Screen.** All coins, 5m + 15m, every window. At t = start - tmin, tmin in {8, 5, 3, 2, 1, 0.5} min, books are the
last `pm_best_bid_ask` with `recv_ms <= t`. Taker: buy a side whose ask is < 0.49 / 0.48 / 0.47 at t; the order
lands at t + latency (200 ms, and 50 ms), is held 250 ms and fills at that time's ask, fee `rate*p*(1-p)` (0.05 BTC
5m/15m, 0.01 elsewhere). Maker: a bid at 0.48 / 0.49 / 0.50 on each side placed at T-3 and T-1 min, live until the
start; filled only if a recorded SELL trade strictly **below** the bid occurs after arrival (trade-through,
pessimistic; no queue), no fee; skipped if the ask has fallen to or below the bid on arrival. Fair for the edge
columns: 0.5, and a tilted fair (price nowcast vs K_est = seen K ticks plus current price for the unseen share,
sigma = 1.2 x sig15, horizon window + time to start). Depth is the size at the best price in the last `pm_book`
snapshot at or before t, only when its best price equals the quote.

**Primary metric.** Settlement markout (outcome - fill price - fee) in c/share of the frozen rules on 2026-11-27,
rules chosen on 2026-11-24, 95% CI from a bootstrap over 15-minute blocks. Train days 11-24 (windows from 10:46 UTC)
and 11-27 (partial day, resolved windows only); 11-25 sealed and 11-26 (validation) not read.

**Frozen rules (on 11-24, before 11-27 was built).** R1 taker, ask < 0.49 at T-3 min. R2 taker, tilted-fair - ask -
fee > 3c with spread <= 5c at T-0.5 min. R3 maker bid 0.48, 5m, T-3 min. R4 maker bid 0.48, 15m, T-1 min.

**Variants.** 1 mechanism; about 36 taker cells (tf x tmin x 3 thresholds, reported where n >= 8) and 12 maker cells
(tf x 2 times x 3 bids) per day, x 2 latencies (they differ by < 1c), plus the 4 frozen rules. Scripts:
`research/prestart/build.py`, `report.py`.

## Results

### A. Pre-start books (11-24 / 11-27; lat 200 ms; mean over quoted windows)

| tf | min before | n (11-24) | spread c (11-24 / 11-27) | median ask size, sh | share windows with a side's ask < 0.49 / 0.48 / 0.47 (11-24) |
|---|---|---|---|---|---|
| 5m | 8 | 288 | 3.6 / 3.0 | 40 | 0 / 0 / 0 |
| 5m | 5 | 377 | 3.4 / 3.0 | 64 | 0 / 0 / 0 |
| 5m | 3 | 1230 | 12.8 / 5.9 | 102 | 8% / 0.8% / 0.2% |
| 5m | 1 | 1234 | 12.9 / 9.3 | 67 | 9.7% / 1.6% / 0.3% |
| 5m | 0.5 | 1237 | 23.2 / 10.5 | 35 | 34% / 24% / 17% |
| 15m | 5 | 265 | 24.4 / 16.8 | 96 | 6% / 2.6% / 0.4% |
| 15m | 1 | 406 | 18.1 / 15.1 | 97 | 9% / 3.7% / 1% |
| 15m | 0.5 | 406 | 27.0 / 16.3 | 143 | 20% / 13% / 7% |

Early on only the liquid coins are quoted (3c spread, 40-64 sh at the touch); the average spread jumps at T-3 min
when thin coins' markets get their first (wide) quotes, and the Up mid stays within 0.6-1.2c of 0.50 (mean absolute
deviation) until the minute before the start, then moves to 4-6c at T-0.5 min as the strike forms (K ticks arriving).
Ask < 0.49 before T-1 min is mostly wide-spread thin-coin books (Up ask + Down ask = 1.03-1.25), not a cheap side of
a tight book.

### Taker (11-24 / 11-27, 200 ms; 50 ms is within 0.1c everywhere)

Buying a side with ask < 0.49 loses in nearly every cell on 11-24: 5m T-3 -5.3c [-16.2, +5.4] (n 98), T-2 -11.2c
(92), T-1 -9.8c (120), T-0.5 -8.1c [-15.2, -0.6] (414); 15m mostly -1 to -19c with CIs of +-25c (n 14-81). Win rates
are 37-43% for a "cheap" side priced about 0.47-0.49: the cheap side is the one the market already sees as the
underdog, and it wins less than 50%. The tilted-fair model edge on those trades is only +1.4 to +4c (fee and spread
eat the 1-3c below 0.50). 11-27 is milder (5m T-0.5 +0.7c [-6.1, +7.1], n 390; 15m +1.6c [-14.3, +15.9], n 87).

### Maker (bid at 0.48 / 0.49 / 0.50, trade-through fills, no fee)

| Cell (lat 200) | placed | fill rate 11-24 / 11-27 | markout on fills c/sh 11-24 | 11-27 | markout of all placed bids (unfilled too) 11-24 |
|---|---|---|---|---|---|
| 5m T-3, 0.48 | 2362 / 1958 | 6.1% / 10.9% | -8.1 [-15.4, -1.0] (143 fills) | -2.2 [-9.6, +4.8] (214) | +2.3 [+1.8, +2.7] |
| 5m T-3, 0.49 | 2238 / 1820 | 6.7% / 12.4% | -5.4 [-13.4, +3.0] | -1.2 [-8.5, +5.8] | +1.7 |
| 5m T-1, 0.48 | 2351 / 2086 | 5.8% / 9.7% | -7.9 [-15.2, -0.1] | -0.2 [-8.3, +7.4] | +2.7 |
| 15m T-3, 0.48 | 735 / 648 | 1.6% / 4.8% | +18.7 [-14.7, +44.9] (12) | +3.6 [-14.7, +22.4] (31) | +2.1 |
| 15m T-1, 0.48 | 773 / 701 | 1.4% / 3.7% | +24.7 [-3.6, +52.0] (11) | -1.8 [-20.0, +18.7] (26) | +3.1 |

Adverse selection is clear on 5m: a resting bid at 0.48-0.50 would earn +1 to +3c per share if it filled at random
(win rate 50-51% for a 0.48 price), but it fills 5-12% of the time, only when a seller trades through it, and then the
side wins 40-48% of the time (-8c on 11-24). The fill itself tells you the side is losing. 15m fills are 7-31 shares
and noise.

### B. Frozen rules, 11-27 unchanged (c/sh [95% CI], n = trades / fills)

| Rule | 11-24 (200 ms) | 11-27 (200 ms) | 11-27 (50 ms) |
|---|---|---|---|
| R1 taker ask < 0.49 @T-3 | -4.5 [-14.6, +4.8] (117) | -2.1 [-15.2, +9.5] (118) | -2.1 [-15.2, +9.5] |
| R2 taker model edge > 3c @T-0.5 | -5.5 [-20.4, +8.2] (62) | +0.7 [-11.1, +11.0] (126) | +1.4 [-11.5, +12.1] |
| R3 maker 0.48 5m @T-3 | -8.1 [-15.4, -1.0] (143) | -2.2 [-9.6, +4.8] (214) | -2.2 [-9.6, +4.8] |
| R4 maker 0.48 15m @T-1 | +24.7 [-3.6, +52.0] (11) | -1.8 [-20.0, +18.7] (26) | -1.8 [-20.0, +18.7] |

R2's model edge promises +4.1 to +4.4c on both days and realises -5.5c / +0.7c: the fair model is not reliable this
early either.

## Verdict

**Abandoned; no strategy or simulator run.** Pre-start books are not mispriced in a way that pays: cheap asks are
wide-spread thin-coin books or the market's underdog, taker buys lose 2-11c per share on 11-24 and are flat on
11-27, and resting bids fill mainly when the side is losing (5m: 40-48% win rate against a 0.48 price). No frozen rule
is positive on both days with a CI above 0 (R4 is 11 and 26 fills). Neither latency matters.

## Note for the streak ladder ([streak-ladder.md](streak-ladder.md))

The ladder pays about 0.61-0.66 at the open (books) for the side it bets on. Resting a bid before the start gets a
much lower price but almost never fills: 5m bids at 0.48-0.50 placed at T-3 or T-1 min fill 5-12% of the time
(15m 1-6%), and those fills have a 40-48% win rate. So a pre-start bid cannot replace the open entry: filling at about
0.48 is a rarity that selects losers, and the ~0.65 paid at the open is the price after the strike and trend are
known, not an overcharge relative to a stable 0.50. Also, a streak is only known when the previous window has
settled, which is the moment the next one opens: a bid placed earlier would have to be conditioned on an incomplete
streak.

## Lessons

- Pre-start the Up mid sits within about 1c of 0.50 and spreads are wide for thin coins; the information arrives in
  the last minute (mid moves 4-6c from T-1 to T-0.5 min).
- "Cheap vs 0.50" is not an edge: after the fee, the cheap side is the underdog and wins less than its price.
- Maker pessimistic fills show adverse selection at the 6-8c level on 5m; unconditional bid markouts (+2c) are
  not attainable because fills are not random.
- Not tested: queue-priority fills at exactly the bid price (we required trade-through), `pm_book` depth is only
  as fresh as the last snapshot.

## Next

None planned. Re-check with validation days only if a maker venue with better queue modelling appears.

# xtf-arb

**Family:** cross-timeframe | **Status:** abandoned
**Current version:** f74eb721846f | **sim_version:** 2127ec99eafd

## Pre-registration (written before any opportunity was measured)

**Mechanism.** Markets of one coin with different timeframes (5m, 15m, 1h, 4h) that end at the same
timestamp settle on the same final price F (mean of Chainlink over the last 60 s); Up iff F ≥ K, and the
strikes K differ by timeframe. For two such markets with K_A < K_B, Up_A pays whenever Up_B pays, so
P(Up_A) ≥ P(Up_B). Buying Up_A + Down_B pays 1 if F ≥ K_B or F < K_A and 2 if K_A ≤ F < K_B: never
below 1. Down_B's ask is 1 − bid(Up_B) in the combined book.

**Hypothesis.** (R, riskless) Moments exist where ask(Up_A) + ask(Down_B) + both taker fees < 1, and
they are still there 200 ms later with size. Who loses: whoever leaves one of the two books stale
while the other reprices. Expectation: rare (the complete-set screen found nothing within one
market), but the two books are run by different makers and may lag each other.
(EV) Even when the cost is above 1, the pair's value is 1 + P(K_A ≤ F < K_B); with P ≈ fair_A − fair_B,
EV − cost = (fair_A − ask Up_A − fee) + (1 − fair_B − ask Down_B − fee), i.e. the sum of the two
legs' fair-value edges (the fv-taker idea, with the loss bounded at cost − 1).

**Settlement check (done before anything else).** On every end-aligned pair with K_A < K_B, B resolved
Up ⇒ A resolved Up. Any violation is investigated before going further.

**Primary metric.** Second half (pair overlap start = the shorter market's window_start ≥ 2026-11-24
17:15 UTC), riskless variant: ¢ per pair = payoff − cost − fees, entry at both tops of book
received 200 ms after the signal (ASOF on recv_ms + 200 ms), first signal per market pair, held to
resolution, with a 15-minute-block bootstrap 95% CI; plus the count of opportunity episodes and the
visible size at the two tops at +200 ms. H1 is reported alongside; no parameter is tuned for R
(margin 0).

**Defaults.** Pairs: same coin, same window_end, different timeframes among 5m/15m/1h/4h, both windows
starting at or after 10:36 UTC (replayable). K: official `price_to_beat` (5m/15m/1h); 4h has none
recorded, so its K is the mean of Chainlink ticks with ts in the 60 s before the window start (as the
simulator derives it). Equal strikes are counted separately. Books: Up-token `pm_best_bid_ask`, both
books' spread ≤ 10¢ (wide-book trap). Robustness: 400 ms latency.
EV variant: 5 s snapshots (5m/15m/1h only), fair value = calibration model k = 1.2, signal when
1 + max(0, fair_A − fair_B) − cost > margin, margin chosen on H1 from {0, 0.02, 0.05}, execution at the
300 ms asks, H2 reported unchanged.

## Variants tried
Count: **4** (riskless rule R at margin 0, screened and simulated as one version, 200 + 400 ms; EV rule at margins 0 / 0.02 / 0.05).

| Version | What changed | Why |
|---|---|---|
| R screen | event-level `pm_best_bid_ask`, ASOF recv_ms + 200 / 400 ms, sizes from the simulator's book reconstruction | pre-registered |
| f74eb721846f | `xtf-arb` in the simulator (defaults in `configs/xtf-arb.yaml`), 4h included | R had executable episodes net of latency |
| EV m ∈ {0, .02, .05} | 5 s snapshots, value 1 + max(0, fair_A − fair_B) | pre-registered second variant |

## Results
Train day 2026-11-24 only (10:36–24:00 UTC). H1 = pair overlap start < 17:15 UTC. Code: `research/xtf_arb/`
(`screen_events.py`, `sizes.py`, `screen_ev.py`, `sim_pairs.py`).

**Settlement check: 0 violations.** 626 end-aligned pairs (5m/15m/1h/4h, 4h strike derived from Chainlink): never
"B Up and A not Up" with K_A < K_B; no equal strikes; where both final prices are recorded they are identical. 180
pairs (29%) settled between the strikes (A Up, B Down). Derived strikes are 0.15 bp (median) / 1.1 bp (p99) from the
official ones; ordering flipped on 1 of 580 pairs.

**Riskless screen (R).** 2.28 M pair events. Narrow-book cost quantiles: 0.1% = 1.002 (H1) / 1.000 (H2), median 1.36 / 1.34.

| | H1 | H2 |
|---|---|---|
| episodes with cost < 1 (narrow books) | 55 in 10 market pairs | 293 in 8 market pairs |
| median / max duration | 77 ms / 3.1 s | 36 ms / 3.6 s |
| still < 1 at +200 ms (all episodes) | 164 of 348 (400 ms: 160) | |
| market pairs traded (first executable episode, 1 share) | 8 of 315 | 5 of 311 |
| ¢ per pair (incl. the between-strikes bonus), +200 ms | +15.3 [+0.5, +39.5] | **+1.90 [+0.51, +3.37]** |
| locked ¢ per pair (1 − cost) | +2.8 | +1.9 |
| visible pairs at +200 ms: first episode / largest episode, sum | 224 / 428 | 73 / 124 |
| locked $ at that size: first / largest episode | $3.6 / $10.4 | $1.0 / $3.9 |

Primary metric (H2 ¢/pair, +200 ms): **+1.90 [+0.51, +3.37] on 5 pairs**, worth about $1–4 for the half at the visible
size. The opportunities cluster in pairs whose strikes are within a few bp (DOGE/SOL/XRP/BNB 5m-15m), mostly
in the middle of the window; none is in a voided window.

**Simulator** (`xtf-arb` f74eb721846f, all coins, 5m/15m/1h/4h, margin 0, size ≤ 50). Leg risk: the two IOC legs filled
equally in only 13 of 76 entries (200 ms) and 8 of 72 (400 ms); most entries filled one leg only.

| Run | Latency | Entries | Legs equal | Matched pairs | Locked $ (matched) | Unhedged-leg $ | Total $ | H2 ¢/pair [CI] |
|---|---|---|---|---|---|---|---|---|
| `data/runs/cross-timeframe/xtf-arb/2026-11-25T0554-b3294d` | 200 ms | 30 H1 / 46 H2 | 3 / 10 | 67 / 166 | +0.95 / +2.68 | +85.1 / +14.3 | **+109.2** (H1 +92.2, H2 +16.9) | +10.2 [−30.1, +128.6] |
| `data/runs/cross-timeframe/xtf-arb/2026-11-25T0554-c9b8f9` | 400 ms | 30 H1 / 42 H2 | 2 / 6 | 68 / 128 | +0.48 / +3.26 | +57.7 / +13.7 | +93.1 (H1 +76.2, H2 +16.9) | +13.2 [−32.6, +145.1] |

The run's $/day is not the arb: the locked part is $3.6/day (200 ms). The rest is unhedged single legs
($243 of cost at 200 ms) held to resolution, i.e. directional bets on whichever leg filled, with H2 CIs far across 0
and a block t of 1.5. One ETH 5m/15m/1h cluster at 11:55 UTC (Down legs of the 1h/15m that won) is most of H1.

**EV variant** (5 s snapshots, 466 pairs, 130 voided markets skipped, 300 ms fills, 1 pair per signal):

| Margin | H1 ¢/pair [CI] (n) | H2 ¢/pair [CI] (n) |
|---|---|---|
| 0 (chosen on H1) | −6.9 [−17.0, +4.5] (128) | **−4.5 [−14.4, +6.7]** (156) |
| 0.02 | −7.5 [−17.7, +3.3] (105) | −3.0 [−13.9, +10.2] (128) |
| 0.05 | −8.0 [−18.6, +6.2] (64) | −5.0 [−15.5, +8.1] (82) |

1000 ms is about the same. The EV variant is the sum of the two legs' fair-value edges (fv-taker), with the loss bounded; it
inherits fv-taker's lack of edge.

Verdict: **abandoned.** The riskless crossing is real (0 settlement violations; H2 locked +1.9 ¢/pair with CI > 0) but tiny:
about $1–4 per half at visible size, far below the $10/day bar, and in the simulator the two legs rarely fill together, so
a live version would mostly be taking unhedged directional positions.

## Lessons
- End-aligned markets really do share one settlement: 626 pairs, no monotonicity violation. Cross-timeframe ideas can rely on it.
- Monotonicity crossings (ask Up_A < bid Up_B) happen almost only when the two strikes are within a few bp: the two
  markets are then nearly the same bet, and each book's top is a few thin quotes (5–30 shares) that drift a tick apart.
- A two-leg taker arb on thin tops has leg risk that dwarfs the edge: by the time the IOCs arrive, the stale side has
  usually been taken. 13/76 equal fills at 200 ms. A live version would need to fire the thinner leg first and
  only complete on its fill (or rest the second leg as a maker), which turns it into a different strategy.
- The sim run's +$109/day is an example of a P&L that looks good and is not the hypothesis: always split P&L into
  the part the mechanism explains (locked) and the rest.
- The simulator strategy sees Chainlink-derived strikes (0.15 bp off); for pairs within ~2 bp the order can flip (1/580).

## Next
- Nothing at this size. If revisited: leg-first execution (take the leg whose top is thinner, then complete), and only
  pairs with strikes ≥ 2 bp apart; re-check capacity on validation days.

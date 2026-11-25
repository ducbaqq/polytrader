# Adverse flow: what predicts a toxic maker fill?

**Status:** done (the pre-registration below was committed before any feature was computed) | **Data:** train day 2026-11-24 from
10:36 UTC only | **sim_version of the fills:** 2127ec99eafd

## Question
The baseline market maker (`mm-binance-pull` defaults, run `2026-11-25T0056-0abdaa`) earns about +0.6 c/share of
spread and loses about -1.4 to -1.7 c/share to adverse selection. Pulling on Binance moves helped in the first
half of the day only; in the second half 46% of fills had no Binance move in the 2 s before them and still lost
~1.5 c/share. Which features, observable *before* the fill, predict the adverse fills, and is any of them stable
across both halves? Offline only: the fills of the baseline run are scored, no new simulator run is needed for
the diagnosis.

## Pre-registration
- **Fills.** Baseline fills, settled, split per market: H1 = `window_start` < 2026-11-24 17:15 UTC (1,448
  fills), H2 = the rest (1,561 fills).
- **Targets.** Per fill, in cents per share, in our direction (Up terms, + = good for us):
  adverse = mid 10 s after the fill - mid 200 ms before (primary); markout = spread + adverse (secondary).
  Means are size-weighted.
- **Features are point-in-time.** Every feature uses only data with `recv_ms <= fill ts_ms - 200 ms` (the
  latest moment a cancel could still beat the fill at the default 200 ms latency), joined as-of on `recv_ms`;
  never exchange timestamps, never the outcome or anything after the fill. Signed features are multiplied by
  the fill direction so that + means "pressure in favour of the position we just got".
- **Feature list (fixed now; any later addition is counted).**
  - A. Polymarket flow, same market: A1/A2/A3 signed taker volume over 1/5/30 s; A4 trade count over 5 s.
  - B. Book (strategy view, recv clock): B1 signed top-of-book size imbalance; B2 spread (ticks); B3 size of
    the touch on the side we rest on; B4 change of our side's depth (top 3 levels) over the last 2 s; B5
    signed Polymarket mid change over the last 2 s; B6 queue ahead at the fill (simulator state, diagnostic).
  - C. Chainlink: C1 signed Binance-futures mid minus last Chainlink price (bp); C2 signed Chainlink price
    minus `price_to_beat` (bp); C3 age of the last Chainlink tick.
  - D. Binance: D1/D2/D3 signed futures mid return over 2/10/60 s; D4 signed change of the futures-spot
    basis over 2 s; D5 signed futures taker imbalance over 5 s; D6 liquidation notional over 30 s.
  - E. Context: E1 time left in the window; E2 |mid - 0.5|; E3 signed moneyness (mid - 0.5 in our
    direction: are we buying the favourite or the longshot); E4 timeframe; E5 coin.
  - F. Cross-timeframe: F1 signed mid change over 5 s of the same coin's other open markets; F2 their
    signed taker volume over 5 s.
  - 27 features in total.
- **Method.** Quintile edges are computed on H1 and applied unchanged to H2. For each feature: per-quintile
  fills, spread, adverse and markout on H1 and on H2.
- **Stable** = the H1-worst quintile (lowest mean adverse) is worse than the other four quintiles together by
  at least 1.0 c/share on H1, AND the same bucket (H1 edges) is worse than the rest on H2 by at least 0.5
  c/share with a market-clustered bootstrap 95% CI that excludes 0. Categorical features (E4, E5) use their
  categories instead of quintiles.
- **Avoidance bound.** For stable features, the H2 markout after dropping the flagged bucket (chosen on H1) is
  reported. It is an upper bound: it assumes the other fills stay as they are, and ignores that a pull also
  loses queue position and some good fills.
- Multiple comparisons: with 27 features and 5 buckets, a few spurious H1 "worst buckets" are expected; the
  H2 confirmation is the filter.

## Results

Code: `research/adverse_flow/features.py` (point-in-time features per fill), `analyze.py` (pre-registered quintile
test), `monotone.py` (post-hoc rank and combined-score checks). Inputs: the baseline run's settled fills, H1 1,448
and H2 1,561 (sizes mostly 10 shares). Baseline: H1 spread +0.54 / adverse -1.43 / markout -0.89 c/share;
H2 +0.64 / -1.73 / -1.08.

**Comparisons made (multiple-comparison count): 53.** 26 pre-registered features (the pre-registration says 27;
the list has 26: A1-A4, B1-B6, C1-C3, D1-D6, E1-E5, F1-F2), 24 post-hoc rank correlations (numeric features),
1 combined H1-fit linear score, 2 avoidance thresholds on D1.

### Pre-registered test: no stable feature
None of the 26 features passes. Full per-half quintile tables (H1 edges) are in the appendix. In short:

| feature | H1-worst bucket | H1 gap | H2 gap [95% CI] | H2 markout all -> without bucket |
|---|---|---|---|---|
| A1 PM signed flow 1 s | q2 (-30.6, 0] | -0.89 | -0.36 [-2.48, +1.43] | -1.08 -> -1.02 |
| A2 PM signed flow 5 s | q3 (-5.24, 0] | -0.98 | -1.63 [-3.98, +0.74] | -1.08 -> -1.03 |
| A3 PM signed flow 30 s | q1 <= -430 | -1.03 | +1.35 [-0.64, +3.46] | -1.08 -> -1.30 |
| A4 PM trade count 5 s | q1 <= 1 | -0.42 | +1.31 [+0.02, +2.59] | -1.08 -> -1.38 |
| B1 top imbalance | q4 (-0.33, 0.17] | -1.74 | -0.62 [-1.91, +0.72] | -1.08 -> -0.99 |
| B2 spread (ticks) | q2 (1, 3] | -0.07 | +0.21 [-1.14, +1.44] | -1.08 -> -1.02 |
| B3 our touch size | q4 (40, 116] | -0.36 | +0.87 [-0.52, +2.41] | -1.08 -> -1.25 |
| B4 our depth change 2 s | q5 > +55% | -1.72 | +1.49 [-0.17, +3.16] | -1.08 -> -1.36 |
| B5 PM mid change 2 s | q2 (-2, 0] | -2.04 | +0.89 [-0.60, +2.53] | -1.08 -> -1.28 |
| B6 queue at fill (diag.) | q2 (0, 6] | -0.79 | +2.04 [+0.40, +3.57] | -1.08 -> -2.61 |
| D1 Binance return 2 s | q1 <= -1.81 bp | -1.77 | -2.51 [-6.82, +1.90] (47 fills) | -1.08 -> -0.99 |
| D2 Binance return 10 s | q4 (0.15, 2.77] | -1.05 | -0.15 [-1.59, +1.43] | -1.08 -> -1.05 |
| D3 Binance return 60 s | q3 (-3.84, 1.15] | -0.67 | +0.21 [-1.13, +1.39] | -1.08 -> -1.15 |
| D4 fut-spot basis change 2 s | q1 <= -0.70 bp | -1.84 | +0.31 [-1.51, +2.30] | -1.08 -> -1.09 |
| D5 fut taker imbalance 5 s | q3 (-0.25, 0.08] | -0.92 | +1.31 [-1.13, +3.86] | -1.08 -> -1.23 |
| D6 liquidations 30 s | too sparse (only HYPE has any) | -2.03 | +0.60 [-1.50, +2.51] | - |
| C1 Binance vs Chainlink | q1 <= -8.3 bp | -2.61 | +0.04 [-1.18, +1.46] | -1.08 -> -1.07 |
| C2 Chainlink vs strike | q3 (-2.2, 1.7] bp | -0.86 | -0.41 [-2.14, +1.13] | -1.08 -> -0.95 |
| C3 Chainlink tick age | q4 (0.58, 0.87] s | -0.71 | +0.11 [-1.67, +1.96] | -1.08 -> -1.10 |
| E1 time left | q1 <= 107 s | -1.03 | -0.38 [-2.00, +1.25] | -1.08 -> -0.95 |
| E2 abs moneyness | q3 (0.145, 0.225] | -1.95 | -0.33 [-2.25, +1.43] | -1.08 -> -1.02 |
| E3 signed moneyness | q5 > 0.215 | -1.11 | -0.35 [-1.90, +1.16] | -1.08 -> -1.00 |
| E4 timeframe | 15m | -1.30 | -0.24 [-1.78, +1.14] | -1.08 -> -1.07 |
| E5 coin | sol | -2.19 | -0.35 [-2.98, +1.91] | -1.08 -> -1.05 |
| F1 sibling mid change 5 s | q4 (0.05, 0.75] c | -1.58 | -0.47 [-2.04, +1.18] | -1.08 -> -1.03 |
| F2 sibling flow 5 s | q4 > 27 | -0.41 | -0.26 [-2.12, +1.42] | -1.08 -> -1.06 |

Gap = adverse of the H1-worst bucket minus the other fills, c/share; the H2 CI is market-clustered. Eleven
features have an H1 gap <= -1.0 c (the pre-registered bar); on H2 the same bucket is either not worse or not
significantly so. The "H1-worst bucket" is often a middle quintile, the signature of noise: with ~290 fills a
bucket, one bucket in five is 1-2 c worse than the rest by chance (fills are heavy-tailed: 6-7% of them move
more than 20 c in 10 s).

### Post-hoc (not pre-registered): rank correlations and a combined score
Spearman correlation of each numeric feature with per-fill adverse, market-clustered 95% CI:

| feature | H1 rho | H2 rho |
|---|---|---|
| **D1 Binance return 2 s** | **+0.135 [+0.077, +0.198]** | **+0.057 [+0.003, +0.116]** |
| D6 liquidations 30 s | +0.076 [+0.017, +0.135] | +0.019 [-0.032, +0.071] |
| D5 fut taker imbalance 5 s | +0.018 [-0.043, +0.071] | +0.065 [+0.013, +0.120] |
| E2 abs moneyness | +0.038 [-0.009, +0.093] | +0.063 [+0.019, +0.118] |
| B6 queue at fill (diag.) | -0.026 [-0.083, +0.028] | -0.066 [-0.121, -0.009] |
| the other 19 | CIs include 0 in both halves | |

Only **D1 (signed Binance futures mid return over the 2 s before the cancel deadline)** is significant with the
same sign in both halves, and it is monotone across quintiles in both (adverse by H1 quintile, q1 -> q5: H1 -2.87,
-2.66, -1.89, -0.48, +0.54; H2 -4.17, -2.36, -2.04, -1.60, -0.35). Its strength falls by more than half in H2,
because 46% of H2 fills (13% in H1) had no Binance mid change at all in those 2 s, and those fills lose -1.62 c
(H1 -1.61) anyway. This is the signal `mm-side-pull` already uses, so it is not new.

A combined OLS score over the 25 pre-fill features (fit on H1 only, winsorised target, scored on H2) ranks H2
fills weakly (Spearman 0.20 on H1, 0.06 on H2). H2 adverse by score quintile (H1 edges): -2.86, -2.22, -2.46,
-1.15, -0.51. Its largest weight is D1.

### Where the adverse selection comes from
Split adverse = (mid at the fill - mid 200 ms before) + (mid 10 s later - mid at the fill):

| | adverse | at the fill (last 200 ms) | after the fill (10 s) |
|---|---|---|---|
| H1 | -1.43 | **-1.16** | -0.27 |
| H2 | -1.73 | **-1.11** | -0.62 |
| H2 fills with no 2 s Binance move | -1.62 | -0.88 | -0.74 |

About two thirds of the adverse selection has already happened at the fill instant: the mid moved against us
inside the last 200 ms, after the latest moment a cancel could still arrive. Measured against the mid at the
fill, the spread we earn is negative (H1 -0.62, H2 -0.46 c). That is the fill mechanism (we quote one tick inside
the touch and get filled when a taker runs through the level), not a slow drift that a feature computed 200 ms
earlier could see.

### Offline markout bounds on H2 (upper bounds)
These drop the H2 fills flagged by a rule chosen on H1 and keep the rest unchanged. They ignore that a pull
also loses queue position and some good fills, so the real effect would be smaller.

| rule (chosen on H1) | H2 fills dropped | H2 markout of kept fills (all: -1.08) |
|---|---|---|
| pre-registered: none stable | - | - |
| D1 <= -1.81 bp (H1 q1) | 47 | -0.99 |
| D1 <= -1.06 bp (H1 q1+q2; on H1 the kept markout is +0.03) | 262 | -0.84 |
| combined score q1+q2 (H1 edges) | 416 | about -0.8 |

Even the best bound leaves the markout clearly negative: avoiding the flagged fills perfectly would not make
the baseline profitable on H2.

## Conclusions
- **No stable predictor of adverse fills beyond the 2 s Binance return**, and that one weakens by half out of
  sample and leaves a negative markout after perfect avoidance. Polymarket's own flow, book imbalance, depth
  thinning, Chainlink divergence, futures-spot lead, futures taker imbalance, time left, moneyness, timeframe,
  coin and sibling-market moves did not survive H2.
- **Most of the loss is not predictable 200 ms ahead**: it is the mid jump at the fill instant. A 1-tick-inside
  quote is filled mostly when a level is run over. Better triggers cannot fix that; only the quote placement
  and the fill mechanics can.
- About 1,500 fills per half, heavy-tailed: effects under ~1 c/share per bucket are not detectable on this day.
  Re-run `features.py` + `analyze.py` on new train/validation days before trusting any of this either way.

## ctx extensions that would be needed
Not needed now (nothing new was stable); recorded for later. `bn_spot_bbo` (D4 basis), `bn_fut_trade` taker flow
(D5) and liquidations (D6) are not exposed through `ctx` (it has the futures top of book only), and neither is
our queue position (B6: `OrderView` has no queue estimate; a strategy could only track it itself from book sizes
and trades). Polymarket flow and books of sibling markets (A, B, F) and Chainlink (C) are already observable via
`on_trade`, `ctx.book` and `ctx.chainlink`.

## Recommendation for the next MM strategy
No new pull trigger: the only stable feature is already in `mm-side-pull`, and its offline ceiling is negative.
Change what gets filled instead: (1) score the at-fill jump by quote placement (inside the touch vs joining the
touch vs one tick behind) on the existing baseline and join-best fills before writing code; (2) quote only where
the visible spread covers the ~1.1 c at-fill jump plus ~0.5 c of drift; (3) above all, get real minimum-size
fills to check the queue/fill model, since the at-fill jump is exactly what an unverified queue model is most
likely to get wrong.

## Appendix: per-half quintile tables (edges from H1; c/share, size-weighted)
Generated by `analyze.py`.
### A1_flow1s

| b | H1 n | H1 spr | H1 adv | H1 mko | H2 n | H2 spr | H2 adv | H2 mko |
|---|---|---|---|---|---|---|---|---|
| q1 <=-30.6 | 290.00 | 0.40 | -1.44 | -1.05 | 224.00 | 0.52 | -2.78 | -2.26 |
| q2 (-30.6,0] | 198.00 | 0.45 | -2.20 | -1.75 | 275.00 | 0.65 | -2.03 | -1.38 |
| q3 (0,24.1] | 670.00 | 0.73 | -1.29 | -0.57 | 863.00 | 0.66 | -1.63 | -0.96 |
| q4 >24.1 | 290.00 | 0.33 | -1.20 | -0.87 | 199.00 | 0.71 | -0.61 | 0.11 |

### A2_flow5s

| b | H1 n | H1 spr | H1 adv | H1 mko | H2 n | H2 spr | H2 adv | H2 mko |
|---|---|---|---|---|---|---|---|---|
| q1 <=-149 | 290.00 | 0.41 | -0.99 | -0.58 | 230.00 | 0.57 | -1.70 | -1.13 |
| q2 (-149,-5.24] | 289.00 | 0.52 | -1.34 | -0.82 | 362.00 | 0.56 | -1.80 | -1.24 |
| q3 (-5.24,0] | 47.00 | 0.52 | -2.38 | -1.85 | 69.00 | 0.68 | -3.30 | -2.62 |
| q4 (0,122] | 532.00 | 0.71 | -1.61 | -0.89 | 666.00 | 0.74 | -1.36 | -0.62 |
| q5 >122 | 290.00 | 0.39 | -1.50 | -1.11 | 234.00 | 0.59 | -2.26 | -1.67 |

### A3_flow30s

| b | H1 n | H1 spr | H1 adv | H1 mko | H2 n | H2 spr | H2 adv | H2 mko |
|---|---|---|---|---|---|---|---|---|
| q1 <=-430 | 290.00 | 0.43 | -2.24 | -1.82 | 243.00 | 0.59 | -0.61 | -0.01 |
| q2 (-430,-17.9] | 289.00 | 0.52 | -0.45 | 0.07 | 362.00 | 0.62 | -0.58 | 0.04 |
| q3 (-17.9,23.1] | 290.00 | 0.81 | -1.71 | -0.90 | 366.00 | 0.84 | -1.86 | -1.02 |
| q4 (23.1,428] | 289.00 | 0.59 | -1.65 | -1.06 | 323.00 | 0.62 | -2.59 | -1.97 |
| q5 >428 | 290.00 | 0.38 | -1.08 | -0.70 | 267.00 | 0.54 | -3.03 | -2.50 |

### A4_ntrades5s

| b | H1 n | H1 spr | H1 adv | H1 mko | H2 n | H2 spr | H2 adv | H2 mko |
|---|---|---|---|---|---|---|---|---|
| q1 <=1 | 259.00 | 0.83 | -1.78 | -0.95 | 326.00 | 0.88 | -0.67 | 0.21 |
| q2 (1,10] | 313.00 | 0.72 | -1.76 | -1.04 | 487.00 | 0.67 | -2.22 | -1.55 |
| q3 (10,25] | 278.00 | 0.46 | -0.56 | -0.10 | 351.00 | 0.62 | -2.42 | -1.79 |
| q4 (25,39] | 307.00 | 0.40 | -1.75 | -1.35 | 237.00 | 0.51 | -1.64 | -1.13 |
| q5 >39 | 291.00 | 0.34 | -1.29 | -0.95 | 160.00 | 0.40 | -0.80 | -0.39 |

### B1_imbalance

| b | H1 n | H1 spr | H1 adv | H1 mko | H2 n | H2 spr | H2 adv | H2 mko |
|---|---|---|---|---|---|---|---|---|
| q1 <=-0.937 | 290.00 | 0.27 | -1.55 | -1.28 | 251.00 | 0.42 | -2.01 | -1.60 |
| q2 (-0.937,-0.769] | 289.00 | 0.38 | -0.35 | 0.03 | 323.00 | 0.65 | -1.74 | -1.09 |
| q3 (-0.769,-0.333] | 289.00 | 0.65 | -1.01 | -0.36 | 334.00 | 0.63 | -2.20 | -1.57 |
| q4 (-0.333,0.167] | 287.00 | 0.72 | -2.82 | -2.10 | 308.00 | 0.75 | -2.24 | -1.49 |
| q5 >0.167 | 293.00 | 0.70 | -1.41 | -0.71 | 345.00 | 0.74 | -0.60 | 0.14 |

### B2_spread_ticks

| b | H1 n | H1 spr | H1 adv | H1 mko | H2 n | H2 spr | H2 adv | H2 mko |
|---|---|---|---|---|---|---|---|---|
| q1 <=1 | 8.00 | 0.36 | -9.49 | -9.13 | 4.00 | 0.33 | 1.73 | 2.06 |
| q2 (1,3] | 1118.00 | 0.46 | -1.44 | -0.98 | 1115.00 | 0.57 | -1.68 | -1.11 |
| q3 >3 | 322.00 | 0.84 | -1.15 | -0.31 | 442.00 | 0.88 | -1.93 | -1.06 |

### B3_our_touch

| b | H1 n | H1 spr | H1 adv | H1 mko | H2 n | H2 spr | H2 adv | H2 mko |
|---|---|---|---|---|---|---|---|---|
| q1 <=6.28 | 290.00 | 0.54 | -1.29 | -0.75 | 390.00 | 0.73 | -1.82 | -1.09 |
| q2 (6.28,16] | 289.00 | 0.55 | -1.51 | -0.96 | 294.00 | 0.66 | -2.51 | -1.85 |
| q3 (16,40] | 289.00 | 0.48 | -1.04 | -0.56 | 318.00 | 0.65 | -2.13 | -1.47 |
| q4 (40,116] | 290.00 | 0.58 | -1.71 | -1.13 | 314.00 | 0.59 | -1.04 | -0.45 |
| q5 >116 | 290.00 | 0.55 | -1.58 | -1.03 | 245.00 | 0.57 | -1.14 | -0.56 |

### B4_our_depth_chg2s

| b | H1 n | H1 spr | H1 adv | H1 mko | H2 n | H2 spr | H2 adv | H2 mko |
|---|---|---|---|---|---|---|---|---|
| q1 <=-0.747 | 290.00 | 0.29 | -1.28 | -1.00 | 245.00 | 0.58 | -1.93 | -1.35 |
| q2 (-0.747,-0.461] | 289.00 | 0.57 | -1.04 | -0.48 | 263.00 | 0.61 | -1.88 | -1.26 |
| q3 (-0.461,-0.0599] | 290.00 | 0.51 | -0.74 | -0.23 | 325.00 | 0.64 | -1.93 | -1.28 |
| q4 (-0.0599,0.546] | 289.00 | 0.76 | -1.26 | -0.50 | 408.00 | 0.77 | -2.26 | -1.49 |
| q5 >0.546 | 290.00 | 0.58 | -2.81 | -2.22 | 320.00 | 0.56 | -0.54 | 0.01 |

### B5_pm_mid2s

| b | H1 n | H1 spr | H1 adv | H1 mko | H2 n | H2 spr | H2 adv | H2 mko |
|---|---|---|---|---|---|---|---|---|
| q1 <=-2 | 257.00 | 0.45 | 0.59 | 1.05 | 277.00 | 0.44 | -1.70 | -1.27 |
| q2 (-2,-0] | 311.00 | 0.51 | -3.03 | -2.52 | 317.00 | 0.72 | -1.02 | -0.30 |
| q3 (-0,0.5] | 229.00 | 0.80 | -1.29 | -0.49 | 407.00 | 0.75 | -2.16 | -1.41 |
| q4 (0.5,2.5] | 335.00 | 0.51 | -1.90 | -1.39 | 291.00 | 0.65 | -1.39 | -0.74 |
| q5 >2.5 | 316.00 | 0.47 | -1.10 | -0.62 | 268.00 | 0.61 | -2.25 | -1.64 |

### B6_queue_at_fill

| b | H1 n | H1 spr | H1 adv | H1 mko | H2 n | H2 spr | H2 adv | H2 mko |
|---|---|---|---|---|---|---|---|---|
| q2 (0,6] | 1155.00 | 0.51 | -1.63 | -1.11 | 1240.00 | 0.65 | -1.21 | -0.56 |
| q3 >6 | 293.00 | 0.62 | -0.83 | -0.21 | 321.00 | 0.64 | -3.25 | -2.61 |

### D1_bn_ret2s

| b | H1 n | H1 spr | H1 adv | H1 mko | H2 n | H2 spr | H2 adv | H2 mko |
|---|---|---|---|---|---|---|---|---|
| q1 <=-1.81 | 290.00 | 0.28 | -2.87 | -2.58 | 47.00 | -0.28 | -4.17 | -4.45 |
| q2 (-1.81,-1.06] | 289.00 | 0.50 | -2.66 | -2.16 | 215.00 | 0.40 | -2.36 | -1.96 |
| q3 (-1.06,-0.0331] | 289.00 | 0.51 | -1.89 | -1.38 | 332.00 | 0.44 | -2.04 | -1.60 |
| q4 (-0.0331,0.467] | 290.00 | 0.65 | -0.48 | 0.17 | 806.00 | 0.81 | -1.60 | -0.79 |
| q5 >0.467 | 290.00 | 0.73 | 0.54 | 1.27 | 161.00 | 0.75 | -0.35 | 0.39 |

### D2_bn_ret10s

| b | H1 n | H1 spr | H1 adv | H1 mko | H2 n | H2 spr | H2 adv | H2 mko |
|---|---|---|---|---|---|---|---|---|
| q1 <=-4.45 | 290.00 | 0.55 | 0.07 | 0.62 | 96.00 | 0.33 | -1.23 | -0.90 |
| q2 (-4.45,-1.87] | 289.00 | 0.44 | -2.21 | -1.77 | 259.00 | 0.60 | -1.13 | -0.53 |
| q3 (-1.87,0.15] | 290.00 | 0.54 | -1.38 | -0.83 | 623.00 | 0.65 | -2.10 | -1.45 |
| q4 (0.15,2.77] | 289.00 | 0.54 | -2.26 | -1.72 | 394.00 | 0.65 | -1.84 | -1.19 |
| q5 >2.77 | 290.00 | 0.63 | -1.37 | -0.75 | 189.00 | 0.82 | -1.26 | -0.44 |

### D3_bn_ret60s

| b | H1 n | H1 spr | H1 adv | H1 mko | H2 n | H2 spr | H2 adv | H2 mko |
|---|---|---|---|---|---|---|---|---|
| q1 <=-9.16 | 290.00 | 0.56 | -0.70 | -0.14 | 119.00 | 0.37 | -0.81 | -0.43 |
| q2 (-9.16,-3.84] | 289.00 | 0.41 | -1.11 | -0.70 | 331.00 | 0.59 | -1.86 | -1.28 |
| q3 (-3.84,1.15] | 290.00 | 0.50 | -1.97 | -1.46 | 532.00 | 0.63 | -1.60 | -0.96 |
| q4 (1.15,7.75] | 289.00 | 0.64 | -1.93 | -1.29 | 433.00 | 0.76 | -1.86 | -1.10 |
| q5 >7.75 | 290.00 | 0.58 | -1.42 | -0.84 | 146.00 | 0.66 | -2.16 | -1.50 |

### D4_basis_chg2s

| b | H1 n | H1 spr | H1 adv | H1 mko | H2 n | H2 spr | H2 adv | H2 mko |
|---|---|---|---|---|---|---|---|---|
| q1 <=-0.699 | 290.00 | 0.37 | -2.90 | -2.53 | 170.00 | 0.38 | -1.46 | -1.07 |
| q2 (-0.699,-0.213] | 289.00 | 0.41 | -1.42 | -1.01 | 240.00 | 0.49 | -1.20 | -0.71 |
| q3 (-0.213,0] | 218.00 | 0.50 | -2.45 | -1.95 | 164.00 | 0.50 | -1.85 | -1.36 |
| q4 (0,0.378] | 361.00 | 0.72 | -0.43 | 0.28 | 811.00 | 0.76 | -1.96 | -1.19 |
| q5 >0.378 | 290.00 | 0.64 | -0.49 | 0.16 | 176.00 | 0.68 | -1.54 | -0.86 |

### D5_bn_takerimb5s

| b | H1 n | H1 spr | H1 adv | H1 mko | H2 n | H2 spr | H2 adv | H2 mko |
|---|---|---|---|---|---|---|---|---|
| q1 <=-0.607 | 290.00 | 0.42 | -1.00 | -0.58 | 513.00 | 0.56 | -2.29 | -1.74 |
| q2 (-0.607,-0.25] | 289.00 | 0.51 | -0.82 | -0.32 | 237.00 | 0.64 | -2.77 | -2.13 |
| q3 (-0.25,0.0754] | 290.00 | 0.58 | -2.16 | -1.58 | 178.00 | 0.57 | -0.58 | -0.00 |
| q4 (0.0754,0.445] | 289.00 | 0.50 | -1.90 | -1.40 | 182.00 | 0.57 | -2.04 | -1.47 |
| q5 >0.445 | 290.00 | 0.69 | -1.24 | -0.55 | 451.00 | 0.80 | -1.00 | -0.21 |

### D6_liq30s

| b | H1 n | H1 spr | H1 adv | H1 mko | H2 n | H2 spr | H2 adv | H2 mko |
|---|---|---|---|---|---|---|---|---|
| q2 (0,596] | 1158.00 | 0.54 | -1.84 | -1.30 | 1460.00 | 0.65 | -1.69 | -1.05 |
| q3 >596 | 290.00 | 0.55 | 0.18 | 0.73 | 101.00 | 0.65 | -2.30 | -1.65 |

### C1_bn_vs_cl

| b | H1 n | H1 spr | H1 adv | H1 mko | H2 n | H2 spr | H2 adv | H2 mko |
|---|---|---|---|---|---|---|---|---|
| q1 <=-8.32 | 290.00 | 0.37 | -3.51 | -3.13 | 459.00 | 0.58 | -1.71 | -1.13 |
| q2 (-8.32,-5.73] | 289.00 | 0.64 | -1.73 | -1.09 | 287.00 | 0.64 | -1.04 | -0.40 |
| q3 (-5.73,4.74] | 290.00 | 0.71 | -0.69 | 0.02 | 47.00 | 0.86 | -3.17 | -2.31 |
| q4 (4.74,7.03] | 289.00 | 0.42 | -0.70 | -0.28 | 140.00 | 0.46 | -2.15 | -1.69 |
| q5 >7.03 | 290.00 | 0.55 | -0.45 | 0.10 | 628.00 | 0.72 | -1.87 | -1.15 |

### C2_cl_vs_strike

| b | H1 n | H1 spr | H1 adv | H1 mko | H2 n | H2 spr | H2 adv | H2 mko |
|---|---|---|---|---|---|---|---|---|
| q1 <=-8.89 | 290.00 | 0.74 | -0.46 | 0.28 | 165.00 | 0.71 | -0.22 | 0.48 |
| q2 (-8.89,-2.2] | 288.00 | 0.47 | -1.24 | -0.77 | 421.00 | 0.63 | -1.63 | -1.00 |
| q3 (-2.2,1.71] | 291.00 | 0.39 | -2.12 | -1.72 | 439.00 | 0.59 | -2.02 | -1.43 |
| q4 (1.71,8.29] | 289.00 | 0.47 | -1.85 | -1.38 | 384.00 | 0.65 | -2.36 | -1.71 |
| q5 >8.29 | 290.00 | 0.64 | -1.40 | -0.76 | 152.00 | 0.76 | -0.98 | -0.22 |

### C3_cl_age

| b | H1 n | H1 spr | H1 adv | H1 mko | H2 n | H2 spr | H2 adv | H2 mko |
|---|---|---|---|---|---|---|---|---|
| q1 <=0.174 | 290.00 | 0.48 | -1.29 | -0.81 | 287.00 | 0.60 | -1.63 | -1.03 |
| q2 (0.174,0.359] | 289.00 | 0.52 | -0.65 | -0.14 | 266.00 | 0.62 | -1.92 | -1.30 |
| q3 (0.359,0.577] | 290.00 | 0.58 | -1.62 | -1.04 | 365.00 | 0.63 | -1.98 | -1.35 |
| q4 (0.577,0.865] | 289.00 | 0.51 | -2.00 | -1.48 | 305.00 | 0.63 | -1.64 | -1.01 |
| q5 >0.865 | 290.00 | 0.62 | -1.58 | -0.96 | 338.00 | 0.73 | -1.47 | -0.74 |

### E1_tau_s

| b | H1 n | H1 spr | H1 adv | H1 mko | H2 n | H2 spr | H2 adv | H2 mko |
|---|---|---|---|---|---|---|---|---|
| q1 <=107 | 290.00 | 0.35 | -2.26 | -1.91 | 405.00 | 0.54 | -2.01 | -1.48 |
| q2 (107,175] | 289.00 | 0.43 | -1.17 | -0.74 | 355.00 | 0.55 | -2.44 | -1.89 |
| q3 (175,231] | 290.00 | 0.57 | -0.79 | -0.22 | 270.00 | 0.65 | -0.95 | -0.30 |
| q4 (231,280] | 289.00 | 0.56 | -1.68 | -1.12 | 242.00 | 0.66 | -1.24 | -0.58 |
| q5 >280 | 290.00 | 0.78 | -1.26 | -0.49 | 289.00 | 0.89 | -1.59 | -0.71 |

### E2_abs_moneyness

| b | H1 n | H1 spr | H1 adv | H1 mko | H2 n | H2 spr | H2 adv | H2 mko |
|---|---|---|---|---|---|---|---|---|
| q1 <=0.055 | 287.00 | 0.48 | -1.97 | -1.49 | 233.00 | 0.66 | -1.91 | -1.25 |
| q2 (0.055,0.145] | 291.00 | 0.50 | -0.56 | -0.05 | 345.00 | 0.63 | -1.86 | -1.23 |
| q3 (0.145,0.225] | 288.00 | 0.51 | -3.00 | -2.50 | 279.00 | 0.62 | -2.01 | -1.38 |
| q4 (0.225,0.33] | 285.00 | 0.55 | -0.50 | 0.05 | 339.00 | 0.62 | -2.20 | -1.59 |
| q5 >0.33 | 297.00 | 0.65 | -1.21 | -0.56 | 365.00 | 0.70 | -0.89 | -0.19 |

### E3_signed_moneyness

| b | H1 n | H1 spr | H1 adv | H1 mko | H2 n | H2 spr | H2 adv | H2 mko |
|---|---|---|---|---|---|---|---|---|
| q1 <=-0.23 | 287.00 | 0.66 | 0.29 | 0.95 | 375.00 | 0.66 | -1.05 | -0.39 |
| q2 (-0.23,-0.06] | 292.00 | 0.49 | -1.65 | -1.16 | 323.00 | 0.53 | -0.77 | -0.24 |
| q3 (-0.06,0.051] | 290.00 | 0.48 | -1.94 | -1.46 | 236.00 | 0.67 | -1.82 | -1.14 |
| q4 (0.051,0.215] | 288.00 | 0.54 | -1.43 | -0.89 | 310.00 | 0.73 | -3.00 | -2.28 |
| q5 >0.215 | 291.00 | 0.54 | -2.30 | -1.76 | 317.00 | 0.64 | -2.00 | -1.37 |

### E4_timeframe

| b | H1 n | H1 spr | H1 adv | H1 mko | H2 n | H2 spr | H2 adv | H2 mko |
|---|---|---|---|---|---|---|---|---|
| 15m | 253.00 | 0.80 | -2.50 | -1.69 | 315.00 | 0.77 | -1.92 | -1.15 |
| 1h | 49.00 | 0.88 | -1.61 | -0.73 | 41.00 | 1.11 | -1.15 | -0.04 |
| 5m | 1146.00 | 0.46 | -1.17 | -0.71 | 1205.00 | 0.59 | -1.70 | -1.10 |

### E5_coin

| b | H1 n | H1 spr | H1 adv | H1 mko | H2 n | H2 spr | H2 adv | H2 mko |
|---|---|---|---|---|---|---|---|---|
| bnb | 47.00 | 0.97 | -2.45 | -1.48 | 45.00 | 0.72 | -3.49 | -2.77 |
| btc | 926.00 | 0.43 | -1.18 | -0.75 | 830.00 | 0.60 | -1.91 | -1.31 |
| doge | 51.00 | 0.94 | -1.81 | -0.87 | 132.00 | 0.90 | -1.01 | -0.10 |
| eth | 180.00 | 0.60 | -1.43 | -0.83 | 198.00 | 0.43 | -0.78 | -0.35 |
| hype | 17.00 | 1.16 | -2.07 | -0.91 | 33.00 | 0.77 | -1.22 | -0.45 |
| sol | 124.00 | 0.71 | -3.42 | -2.71 | 155.00 | 0.67 | -2.04 | -1.38 |
| xrp | 103.00 | 0.84 | -0.49 | 0.35 | 168.00 | 0.95 | -2.00 | -1.04 |

### F1_sib_mid5s

| b | H1 n | H1 spr | H1 adv | H1 mko | H2 n | H2 spr | H2 adv | H2 mko |
|---|---|---|---|---|---|---|---|---|
| q1 <=-1.33 | 290.00 | 0.48 | -0.87 | -0.39 | 362.00 | 0.52 | -1.94 | -1.43 |
| q2 (-1.33,-0.375] | 288.00 | 0.45 | -1.75 | -1.30 | 255.00 | 0.65 | -1.03 | -0.37 |
| q3 (-0.375,0.045] | 290.00 | 0.51 | -0.10 | 0.41 | 335.00 | 0.65 | -2.44 | -1.79 |
| q4 (0.045,0.75] | 282.00 | 0.61 | -2.69 | -2.08 | 220.00 | 0.69 | -2.13 | -1.44 |
| q5 >0.75 | 297.00 | 0.66 | -1.80 | -1.14 | 389.00 | 0.72 | -1.17 | -0.45 |

### F2_sib_flow5s

| b | H1 n | H1 spr | H1 adv | H1 mko | H2 n | H2 spr | H2 adv | H2 mko |
|---|---|---|---|---|---|---|---|---|
| q1 <=-36 | 289.00 | 0.49 | -1.09 | -0.60 | 232.00 | 0.57 | -1.04 | -0.47 |
| q2 (-36,0] | 281.00 | 0.41 | -0.90 | -0.49 | 298.00 | 0.53 | -2.77 | -2.24 |
| q3 (0,27] | 587.00 | 0.64 | -1.70 | -1.06 | 773.00 | 0.68 | -1.42 | -0.74 |
| q4 >27 | 291.00 | 0.53 | -1.75 | -1.23 | 258.00 | 0.74 | -1.95 | -1.21 |

# endgame-maker-tf (endgame-maker on 15m / 1h / 4h / 1d)

**Family:** endgame | **Status:** testing | **Strategy code:** `endgame-maker` (frozen, params only) | **sim_version:** see runs

## Hypothesis
`endgame-maker` (5m, quote in the last 120 s) earns its small edge at settlement: favourites late in a window are
underpriced. Does the same hold on the longer markets, scaled to the timeframe? Step 1 is a no-simulator
calibration check; the simulator is only used where it shows a plausible gap.

## Pre-registration (written before any simulator run)
- **Data split:** choose on 2026-11-24 only; report the chosen variants unchanged on 2026-11-27 (held out, synced
  through 09:59 UTC). Sealed days and the already-looked-at validation day 2026-11-26 are not used.
- **Step 1 (calibration, 11-24):** per timeframe and time-to-end (30, 60, 120, 300, 600, 1800 s where they fit), the
  favourite (higher mid) in tight books (spread <= 2c), mid 0.75-0.95, tops as-of `recv_ms`, official outcome.
  Gap = win rate - mean mid (cents), 15-minute block bootstrap CI.
- **Step 2 variants (at most 6, params only, `configs/research/`):** V0 5m last 120 s (reference); V1 15m last
  300 s; V2 15m last 120 s; V3 1h last 600 s; V4 4h+1d last 1800 s. All other params as V2 of endgame-maker
  (band 0.75-0.95, spread <= 2c, 10 shares, stop 5 s). The windows were fixed from the market's length, not from
  step 1's gaps (which are noisy: see below).
- **Primary metric:** cents per share bought, fees included, 15-minute block CI; plus fills and markets. A
  timeframe is "promising" only if the held-out day is positive with the CI lower bound > 0 (or the sample is
  stated as too small).
- 4h has 6 markets per day and 1d about one per coin: said plainly where the sample can't support a conclusion.

## Step 1: late-window calibration of the favourite (2026-11-24, no simulator)
`research/endgame_tf/calibration.py`. Gap = realised win rate - mean mid, in cents, over favourite-in-band,
spread <= 2c snapshots (one per market and time-to-end), 15-minute block bootstrap 95% CI. Markets are those ending
on 11-24 with a top of book (data starts 10:36 UTC). "n" is markets, not independent: coins move together.
| Timeframe | tau (s) | n | win % | mid % | gap (c) [CI] |
|---|---|---|---|---|---|
| 5m | 30 / 60 / 120 | 63 / 188 / 376 | 88.9 / 87.8 / 88.6 | 87.8 / 88.1 / 87.2 | +1.1 [-7.3, 8.7] / -0.3 [-5.3, 4.5] / +1.3 [-2.9, 5.0] |
| 15m | 30 | 7 | 100 | 89.0 | +11.0 (7 markets) |
| 15m | 60 / 120 | 22 / 63 | 86.4 / 88.9 | 89.2 / 86.7 | -2.9 [-20, 11] / +2.2 [-5.7, 9.3] |
| 15m | 300 / 600 | 176 / 140 | 85.8 / 82.1 | 85.4 / 83.6 | +0.4 [-6.8, 6.7] / -1.5 [-11.5, 7.1] |
| 1h | 60 / 120 / 300 / 600 / 1800 | 5 / 4 / 13 / 17 / 45 | 100 / 75 / 92 / 88 / 89 | 87.5 / 90.8 / 88.9 / 89.5 / 85.8 | +12.5 / -15.8 / +3.4 / -1.2 / +3.1 (all CIs span 0, n <= 45) |
| 4h | 1800 | 1 | 100 | 85 | one market |
| 1d | any | 0 | | | no tight-book favourite observed |

Reading: **no timeframe x time-to-end cell shows a gap above 1-2c whose CI excludes 0** (the CIs are +-5 to +-10c).
The two "significant" cells (15m at 30 s, 1h at 60 s) are 7 and 5 markets. The 5m gap at 120 s is +1.3c, which is the
same size as the earlier calibration slope result and not distinguishable from 0 either. Step 1 therefore does not
pick a timeframe; step 2 runs the pre-registered windows anyway (the simulator is the real test, and a maker's edge
is not only the calibration gap).

## Variants tried
Count: 5 (all params-only configs in `analysis/configs/research/`, each run on 11-24 then, unchanged, on 11-27).
| Variant | Config | Timeframe, quoting window |
|---|---|---|
| V0 | endgame-tf-5m-120 | 5m, last 120 s (reference = endgame-maker V2) |
| V1 | endgame-tf-15m-300 | 15m, last 300 s |
| V2 | endgame-tf-15m-120 | 15m, last 120 s |
| V3 | endgame-tf-1h-600 | 1h, last 600 s |
| V4 | endgame-tf-4h1d-1800 | 4h + 1d, last 1800 s |

## Results
c/share is market P&L (fees included, makers pay none) over shares bought, 15-minute block CI. Attribution per share
(c): spread = mid_before - fill px; adverse = mid 10 s after - mid_before; settlement = settle value - mid 10 s after
(fills without a 10 s mid are excluded from the attribution). Run folders `data/runs/endgame/endgame-maker/<id>`;
all 8 coins.

**Train day 2026-11-24 (choose):**
| Variant | Run | Markets (voided) | Fills (traded mkts) | Shares | P&L | c/share [CI] | Win % vs mean fill px | spread / adverse / settlement |
|---|---|---|---|---|---|---|---|---|
| V0 5m | 2026-11-27T1024-c9b3f6 | 1199 (57) | 193 (158) | 1327 | +$41.91 | +3.16 [-1.9, 8.1] | 88.3 vs 85.2 | -0.07 / -1.99 / +4.55 |
| V1 15m 300 s | 2026-11-27T1031-988feb | 371 (45) | 77 (69) | 625 | +$44.03 | +7.05 [-1.2, 13.9] | 90.7 vs 83.6 | +0.01 / -1.73 / +8.45 |
| V2 15m 120 s | 2026-11-27T1035-20d399 | 371 (45) | 32 (30) | 276 | +$18.25 | +6.62 [-3.9, 14.5] | 91.5 vs 84.9 | -0.08 / -4.27 / +9.42 |
| V3 1h 600 s | 2026-11-27T1039-f27a07 | 49 (35) | 3 (3) | 30 | -$5.00 | -16.7 [-31.5, 13.0] | 66.7 vs 83.3 | 3 fills: no meaning |
| V4 4h+1d | 2026-11-27T1042-625cb0 | 3 (13) | 0 | 0 | $0 | n/a | | no fills |

**Chosen on 11-24: V1 (15m, last 300 s)**: the highest P&L and c/share with a useful number of fills; V0 is the
reference. V2 has half the fills; V3 and V4 never traded. (All five were also run on 11-27, so the held-out table
below is not filtered by the choice.)

**Held-out day 2026-11-27 (synced through 09:59 UTC, so ~10 h; unchanged configs):**
| Variant | Run | Markets (voided) | Fills (traded mkts) | Shares | P&L | c/share [CI] | Win % vs mean fill px | spread / adverse / settlement |
|---|---|---|---|---|---|---|---|---|
| V0 5m | 2026-11-27T1059-7aa527 | 887 (49) | 121 (96) | 816 | +$24.47 | +3.00 [-2.7, 8.3] | 87.8 vs 84.8 | -0.07 / -3.15 / +5.56 |
| **V1 15m 300 s** | 2026-11-27T1052-d75d64 | 272 (40) | 53 (51) | 428 | +$22.16 | **+5.18 [-3.0, 12.1]** | 92.0 vs 86.8 | +0.03 / -3.07 / +8.19 |
| V2 15m 120 s | 2026-11-27T1101-061a9c | 272 (40) | 19 (19) | 172 | +$23.69 | +13.7 [+10.4, 16.8] | 100 vs 86.3 | -0.50 / -4.47 / +19.19 |
| V3 1h 600 s | 2026-11-27T1103-55813a | 36 (27) | 8 (6) | 55 | -$12.13 | -22.0 [-62.8, 17.4] | 63.8 vs 85.7 | -0.17 / -8.68 / -13.12 |
| V4 4h+1d | 2026-11-27T1104-a12779 | 2 (14) | 0 | 0 | $0 | n/a | | no fills |

Latency sweep on the held-out day (order latency 100 / 200 (default) / 400 ms), P&L:
| Variant | 100 ms | 200 ms | 400 ms |
|---|---|---|---|
| V1 15m 300 s | +$9.34 (66 fills) | +$22.16 (53) | **-$11.35 (47)** |
| V0 5m | +$1.43 (144) | +$24.47 (121) | +$11.24 (115) |

Caveats: V2's CI is degenerate (19 of 19 fills won, so every resample is positive); it is 19 fills and one afternoon,
not evidence. Most markets of the longer timeframes are voided (the simulator's data-gap rule voids 42% of the 1h and 81% of the 4h markets on 11-24), which
is why V3 and V4 see only 3 to 8 fills.

## Verdict
- **15m (last 300 s): the same sign and size as 5m, not better established.** +7.1c on the train day, +5.2c [-3.0, +12.1]
  held out (51 markets, 428 shares): positive on both days, CI spans 0 on both, and the latency sweep is unstable
  (+$9 / +$22 / -$11 at 100 / 200 / 400 ms; 5m V0 is +$1 / +$24 / +$11). Same story as the 5m: edge is settlement
  (+8c), adverse -3c, spread zero. The step 1 calibration gap at 15m (+0.4c at 300 s) does not explain +5-7c, so some of
  this is likely luck in who wins; call it "plausibly like 5m, unproven". **Status: needs more days, not promising yet.**
- **15m last 120 s:** 30 and 19 fills; too few to say anything.
- **1h (last 600 s):** 3 and 8 fills, one losing favourite each day makes it -$5 / -$12. **Sample cannot support a
  conclusion.** Favourites in tight books are rare and most 1h markets are voided.
- **4h / 1d:** zero fills on both days. **Sample cannot support a conclusion** (6 4h markets per day, 8 coins' 1d markets
  mostly never quoted tight and voided). Not testable with this data volume.
- Overall: nothing here beats 5m V0; the edge does not obviously extend, and where it might (15m) it is as unproven as
  at 5m. Next step if wanted: more days of 5m+15m together (the 15m adds roughly 50% more fills/day), judged with the
  validation protocol, not tuned.

## Lessons
- Calibration cells at 1h/4h are too thin to measure a 1-2c gap on one or two days (CI +-10c or n < 10).
- Block bootstrap CIs collapse when every fill wins (V2 15m 120 s): report the win/loss counts next to them.
- Longer timeframes lose most markets to the data-gap void rule, which also cuts the fills.

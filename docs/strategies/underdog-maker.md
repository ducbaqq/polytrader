# underdog-maker

**Family:** fair-value (the edge is market calibration, not a model) | **Status:** testing
**Current version:** (see runs) | **sim_version:** 2127ec99eafd

## Hypothesis
The calibration study found 5m prices too extreme with 2-5 minutes left (slope 0.86 [0.75, 0.98]): favourites are
overpriced and underdogs underpriced. `endgame-maker` exploits the opposite late underconfidence; its edge was
entirely settlement (+5.3c/sh), with ~0 spread and small 10 s adverse. This mirrors it: with 300 to 120 s left in a 5m
market, when the underdog's mid is in a band and its spread <= 2c, join the underdog's best bid for 10 shares, one
position per market, hold to settlement, cancel when the conditions stop holding. Who loses to us: sellers of the
underdog who dump it into our bid (and holders of the overpriced favourite on the other side of the same bet).
The family is `fair-value` only because the glossary has no calibration family; there is no model here.

**Same-data caveat.** The calibration study that gave the idea used the same 2026-11-24 data, so the second half is an
execution check (does the simulated maker keep the edge), not independent evidence. The first real test is validation
day 2026-11-26.

Payoffs are lottery-like (win ~75c per share, lose ~25c, win rate near the fill price), so counts of winning markets are
reported next to the CI.

## Pre-registration (written before any run)
- **Primary metric:** second-half (window_start >= 2026-11-24 17:15 UTC) cents per share bought, fees included, with a
  15-minute block bootstrap 95% CI (`polydata.research.bt_halves`), plus fill and market counts.
- **Defaults:** 5m only, start_tau_s 300, stop_tau_s 120, band 0.15-0.35 (underdog mid), max_spread 0.02, size 10.
- **Tuning:** first half only, at most 5 variants (V1 defaults; V2 band 0.25-0.45; V3 tau 300-60; V4 band 0.25-0.45 +
  tau 300-60; V5 reserved). The chosen variant is written here before H2 is read, then reported unchanged, plus a 400 ms run.
- **Promising** only if H2 is positive with CI lower bound > 0 (or clearly positive, said so).
- Attribution per share: spread, 10 s adverse, settlement.

## Variants tried
Count so far: 4 (all tuned on the first half; V5 never used). One extra run (V1 at 400 ms) is a robustness check, not a variant.
| Variant | What changed | Run |
|---|---|---|
| V1 | defaults: 5m, tau 300-120 s, band 0.15-0.35, spread <= 2c | 2026-11-25T0644-22a442 |
| V2 | band 0.25-0.45 | 2026-11-25T0644-b115e2 |
| V3 | tau 300-60 s | 2026-11-25T0645-a4c2b1 |
| V4 | band 0.25-0.45 and tau 300-60 s | 2026-11-25T0645-10b5e2 |

**H1 results (window_start < 17:15 UTC), chosen on these only** (c/share, 15-min block bootstrap CI):
| Variant | markets with fills (winning) | fills | shares | avg fill px | c/share [CI] |
|---|---|---|---|---|---|
| V1 | 164 (50) | 222 | 1265 | 0.281 | +4.20 [-5.5, +13.8] |
| V2 | 167 (58) | 221 | 1250 | 0.368 | +1.00 [-8.9, +10.2] |
| V3 | 182 (55) | 242 | 1412 | 0.276 | +4.12 [-4.4, +12.9] |
| V4 | 172 (60) | 227 | 1287 | 0.368 | +1.25 [-8.3, +10.0] |

**Chosen variant (recorded before H2 was read): V1** (the pre-registered defaults). It has the best H1 point estimate,
V3 is indistinguishable (+4.12) with a longer window, and the 0.25-0.45 band is worse (+1.0, +1.25). H2 is reported
unchanged, plus a 400 ms latency run of V1.

## Results
Run folders `data/runs/fair-value/underdog-maker/<id>` (day 2026-11-24, train, all 8 coins, 5m only; one `--set` per run).
| Run | Kind | Variant | Notes |
|---|---|---|---|
| 2026-11-25T0644-22a442 | train | V1 chosen | **H1 +4.20c [-5.5, +13.8] (164 mkts, 50 won, 1265 sh, 222 fills); H2 -1.78c [-11.7, +7.4] (176 mkts, 42 won, 1342 sh, 231 fills, -$23.92); all +1.12c [-6.0, +8.3] (340 mkts, 92 won, +$29.27)** |
| 2026-11-25T0650-371ccc | train | V1 at 400 ms | H1 -0.68c [-8.5, +8.2]; **H2 -0.65c [-9.3, +7.1]** (171 mkts, 44 won, 1355 sh, 229 fills) |
| 2026-11-25T0644-b115e2 / 0645-a4c2b1 / 0645-10b5e2 | train | V2 / V3 / V4 | H1 only was read, see the table above |

Attribution per share, V1 (c/share; spread = mid_before minus fill price, adverse = mid 10 s after minus mid_before,
settlement = settle value minus mid 10 s after):
| Half | fill px | spread | adverse (10 s) | settlement | total | win rate (share-wtd) |
|---|---|---|---|---|---|---|
| H1 | 28.11 | -0.27 | -0.53 | +5.00 | +4.20 | 0.323 |
| H2 | 27.66 | -0.11 | -1.76 | +0.08 | -1.78 | 0.259 |
Makers pay no fee, so total = settle value minus fill price. The H1 edge was a win rate of 0.32 against a fill price of
0.28 (the calibration study's direction); in H2 the win rate fell to 0.26 against 0.28, i.e. no edge, and the 10 s adverse
drift (-1.8c) was larger.

By coin, V1 H2 (c/share, markets with fills, winning markets): BTC -2.3 (73, 19), ETH +2.7 (53, 14), SOL -1.6 (27, 6),
XRP -7.8 (15, 2), DOGE -22 (4, 1), BNB -22 (4, 0). H1: BTC +2.3 (72, 22), ETH +6.7 (46, 15), SOL +7.1 (28, 9), XRP -12.3 (9, 1).
The coins agree on nothing beyond noise; no coin has a CI away from 0.

## Verdict
**Abandoned (not promising).** H1 +4.2c was not distinguishable from 0 (CI [-5.5, +13.8]); H2 is -1.78c [-11.7, +7.4],
the sign flips, and the 400 ms run is about -0.7c in both halves. About 170 filled markets per half with ~30% winners gives
a CI of roughly +-10c, so only edges above 10c per share could show up; the calibration slope (0.86) implies a few cents at
most. No frozen validation config was written (H2 is not positive). Variants tried: 4.

## Lessons
- The calibration study's "favourites are overpriced 2-5 min out" bias (slope 0.86) is too small to detect with one day of
  makers in the 0.15-0.35 band: an underdog bought at 0.28 has a standard error of about 4.5c per market, so 170 markets
  give ~+-7c before block effects.
- Joined underdog bids fill at about the mid (spread ~0): the maker earns no spread; any edge must come from settlement.
- Unlike endgame-maker, there was no persistent edge to keep: the settlement term went from +5.0c to +0.1c between halves.
- Multiple settings behind one `--set` (a second `--set` flag overrides the first).

## Next
Nothing queued. Revisit only if a longer sample (several validation days) is available, e.g. as a cheap extra run on
2026-11-26 with the unchanged defaults; do not tune further.

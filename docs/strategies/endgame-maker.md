# endgame-maker

**Family:** endgame | **Status:** testing
**Current version:** (see runs) | **sim_version:** 2127ec99eafd

## Hypothesis
The calibration study found 5m prices too timid near the end (slope 1.11 [1.02, 1.23] at 30-60 s left, 1.28
[1.16, 1.62] at 10-30 s). Taking the favourite at the ask was not tradeable (-2.2 to +1.6 c/sh, CIs span 0): the
taker pays spread and fee. A maker resting a bid on the favourite collects the spread and pays no fee, so the
underconfidence edge might survive. Risk: the fills that happen are adverse (the favourite is sold to us just
before it flips). Who loses to us: impatient sellers of the favourite. That adverse-fill risk is what we measure.

Rule (see the strategy docstring): 5m markets, last `start_tau_s` seconds until `stop_tau_s`, favourite's spread
<= 2 c and mid in [band_lo, band_hi]; rest one post-only bid at the favourite's best bid, `size` shares, hold to
settlement, cancel when the conditions stop holding; one position per market.

## Pre-registration (written before any run)
- **Primary metric:** second-half (window_start >= 2026-11-24 17:15 UTC) cents per share bought, fees included,
  with a 15-minute block bootstrap 95% CI (`polydata.research.bt_halves`), plus the fill count.
- **Defaults (chosen before any run):** 5m only, start_tau_s 60, stop_tau_s 5, band 0.75-0.95, max_spread 0.02,
  size 10, no fair guard.
- **Tuning:** on the first half only, at most 6 variants in total (T 30/60/120 s, band, 5m vs 5m+15m, fair guard).
  The chosen variant is written here before the second half is read, then reported unchanged, plus a 400 ms
  latency run.
- **Promising** only if H2 is positive with CI lower bound > 0 (or clearly positive, said so).
- Attribution reported per share: spread, adverse, settlement (an end-of-window "adverse" move is settlement).

## Variants tried
Count so far: 6 (all on the first half; one of them, V5, was void, see below).
| Variant | What changed | Why |
|---|---|---|
| V1 | defaults: 5m, T=60 s, band 0.75-0.95, spread <= 2c | pre-registered default |
| V2 | T=120 s | more time to get filled |
| V3 | T=30 s | where the study's slope is largest |
| V4 | V1 + fair guard (fair >= bid) | skip bids the model thinks are too rich |
| V5 | meant 5m+15m; the second `--set` overrode the first, so the strategy still ignored 15m: identical to V1 (void, still counted) | |
| V6 | T=120 s, 5m+15m | does 15m add fills? |

**H1 results (window_start < 17:15 UTC), chosen on these only** (c/share, 15-min block bootstrap CI):
| Variant | fills (markets) | shares | c/share [CI] | spread / adverse(10 s) / settlement-after-10 s |
|---|---|---|---|---|
| V1 | 28 (23) | 215 | +0.17 [-17.7, +12.7] | -1.21 / -6.38 / +7.75 |
| V2 | 90 (78) | 649 | +2.00 [-5.9, +9.1] | -0.07 / -2.85 / +4.91 |
| V3 | 5 (5) | 50 | +12.76 [+8.8, +16.0] | +1.86 / -2.16 / +13.06 (5 markets only) |
| V4 | 1 (1) | 6 | +8.00 (one market) | |
| V6 | 103 (91) | 762 | +1.00 [-7.2, +8.1] | -0.11 / -2.89 / +4.00 |

**Chosen variant (recorded before H2 was read): V2** = 5m only, start_tau_s 120, everything else default. It has
the most fills with a positive point estimate; V3 and V4 have too few fills to judge, and adding 15m (V6) did not
help. H2 will be reported unchanged, plus a 400 ms latency run of V2.

## Results
| Run | Days | Kind | $/day | Verdict | Notes |
|---|---|---|---|---|---|

## Lessons

## Next

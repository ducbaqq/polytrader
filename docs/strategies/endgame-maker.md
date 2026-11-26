# endgame-maker

**Family:** endgame | **Status:** testing (needs validation days)
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
Run folders `data/runs/endgame/endgame-maker/<id>` (day 2026-11-24, train, all 8 coins, 5m unless stated).
| Run | Days | Kind | $/day | Verdict | Notes |
|---|---|---|---|---|---|
| 2026-11-25T0514-2241c4 | 11-24 | train | | V1 | H1 only looked at: +0.17c [-17.7, +12.7], 23 markets |
| 2026-11-25T0523-73c265 | 11-24 | train | | V5 (void) | identical to V1 |
| 2026-11-25T0520-216332 | 11-24 | train | | V3 | H1: 5 markets |
| 2026-11-25T0523-a2cadf | 11-24 | train | | V4 | H1: 1 market |
| 2026-11-25T0548-5db57d | 11-24 | train | | V6 | H1: +1.00c [-7.2, +8.1], 91 markets |
| 2026-11-25T0523-2414d7 | 11-24 | train | +41.9 (whole day, all 5m) | V2 chosen | **H1 +2.00c [-5.9, +9.1] (78 mkts, 649 sh, 90 fills); H2 +4.27c [-3.0, +10.4] (80 mkts, 678 sh, 103 fills, +$28.92); all +3.16c [-1.9, +8.1]** |
| 2026-11-25T0546-712a7a | 11-24 | train | | V2 at 400 ms | H1 +3.06c [-5.3, +10.6]; **H2 +2.26c [-5.5, +8.8]** (80 mkts, 642 sh, 99 fills) |

**Paper runs** (`polydata paper run configs/validation/endgame-maker-v2.yaml`, live public feeds, simulated fills, the user's Mac via a
London VPN; validation day 2026-11-26, so each run is one validation look; not evidence of edge on its own):
| Run | UTC span | Markets (voided) | Orders / fills | P&L (Chainlink settle) | P&L (Polymarket outcomes) | Parity | Notes |
|---|---|---|---|---|---|---|---|
| live_task11 run1h | 13:25–14:31 | 88 (6) | 817 / 20 | +$12.04 | +$12.04 | (not replayed) | voids: bn gap 1.0–1.6 s on BNB/HYPE; 2 ZEC never opened; loop lag p99 14 ms |
| live_task11 run6h | 14:31–20:37 | 568 (72) | 5,469 / 98 | −$6.64 | **+$13.36** | **passed** (2.43M decisions, 0 mismatches) | voids: 58 bn gap, 10 pm gap, 4 late data (one ~30 min network burst, 85k late rows); 3 near-tie outcome mismatches (Chainlink reconstruction vs Polymarket) swing P&L by $20; loop lag p99 11 ms |

Resource measurement (1 h run, sampled every 30 s): runner CPU avg 29 % / max 47 % of one core, RSS max 825 MB, processing lag
p99 16 ms, Polymarket feed delay p50 7 ms / p99 ~250 ms, session ~230 MB/h (6 h run: ~130 MB/h; trading RSS 0.6–1.4 GB, but the
end-of-run in-process session build peaked at 7.2 GB, to be moved to a subprocess).

Lessons from the paper runs: the sign of the 6 h result is unresolved (−$6.64 with the simulator's own Chainlink reconstruction,
+$13.36 with Polymarket's official outcomes; 3 near-tie markets decide it). Until we record the settlement TWAP stream itself,
report both and treat the official-outcome figure as the reference;
the Binance-gap void rule voids 10% of markets for a strategy that never reads Binance (to be fixed with the gaps.py work).

Attribution per share, V2 (c/share; mid_before = mid 200 ms before the fill):
| Half | fill px | spread | adverse (10 s after) | settlement (after the 10 s) | total | win rate |
|---|---|---|---|---|---|---|
| H1 | 87.10 | -0.07 | -2.85 | +4.91 | +2.00 | 0.891 |
| H2 | 83.33 | -0.08 | -0.98 | +5.33 | +4.27 | 0.876 |
Settlement P&L per share (settle value minus fill price) is the total, +2.0 and +4.3c, since makers pay no fee.

By coin (V2, whole day): BTC +3.2c (68 mkts), ETH +5.2c (55), SOL +2.4c (23), DOGE +16.5c (2), XRP -11.8c (10, wins are
rare and losses are 85c). Only BTC, ETH, SOL, XRP, DOGE ever passed the 2c-spread filter; BNB/HYPE/ZEC never quoted.

## Verdict
Not promising by the pre-registered rule: H2 is positive (+4.27c) but the CI [-3.0, +10.4] spans 0; H1 is also
positive with a CI spanning 0, and the 400 ms run stays positive (+2.3c in H2). Positive in both halves and robust to
latency, but about 80 filled markets per half cannot separate +3c from 0. Needs validation days (first 2026-11-26).
Variants tried: 6. Not a candidate for the final run.

## Lessons
- The "spread" earned is about zero: a joined bid fills when the mid has come down onto it (fill price ~ mid), so the
  maker does not collect the half-spread; the whole result is the settlement edge, +5c per share after the first 10 s.
  That is the calibration study's underconfidence (win rate 0.88-0.89 against a fill price of 0.83-0.87), obtained
  without taker costs. The adverse selection at the 10 s horizon is small (-1 to -3c) compared with it.
- Tight books (<= 2c) restrict this to BTC, ETH, SOL, XRP, DOGE; the fair guard and T=30 s leave too few fills to judge.
- Wins cost 13-17c, losses 83-87c: XRP's 10 markets show how one flip erases several fills, and the CI is wide.
- A `--set` flag given twice keeps only the last (V5 void): put several settings behind one `--set`.

## Next
Re-run V2 unchanged on validation day 2026-11-26 and later; the test is whether H2-like +3-4c per share repeats
with a CI above 0. A larger size or the 15m market would only add fills, not edge (V6 showed no gain).

## Controller note (2026-11-25)
The hypothesis came from the calibration study, which used the same recorded data (including 2026-11-24). So the second half here
is not independent of how the idea was found: it is a check that the simulated execution keeps the edge, not out-of-sample evidence
for the edge itself. The first real test is validation day 2026-11-26, with V2 run unchanged (strategy version as merged).

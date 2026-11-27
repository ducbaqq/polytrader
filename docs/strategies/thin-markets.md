# thin-markets (thin-from-liquid)

**Family:** cross-timeframe | **Status:** abandoned (screen)
**Current version:** screen only (no simulator strategy) | **sim_version:** a44b1ed9e51f

## Pre-registration (written before any opportunity was measured)

**Mechanism.** All crypto Up/Down markets settle on the same Chainlink 60 s TWAP at the window end, so markets of
one coin that END at the same moment share the final value F and differ only in the strike K (the TWAP at their own
window start). The 5m market trades ~25× the 1h volume (strategy-ideas idea #2); the 1h / 4h / daily books are
thin and probably quoted by fewer, slower makers. In the last minutes of the thin market T, the liquid market L
that ends at the same moment prices the same F, so L's mid implies a fair price for T.

**Hypothesis.** The thin market's best bid/ask sometimes sits beyond the fair price implied by L by more than the
taker fee plus a margin, and taking it earns positive settlement markout. Who loses: thin-market makers who
update slowly when the price moves in the last minutes.

**Pricing rule (fixed here, not tuned).** F ~ N(μ, σ²) with σ the remaining uncertainty of the settlement TWAP:
σ = K_L · k · sig15 · √v(τ), sig15 = realised vol of Binance futures 1 s log returns over the last 15 min, k = 1.2
(calibration study), v(τ) = τ − 40 for τ > 60 s, (τ/60)² · τ/3 inside the 60 s settlement window (only the
unobserved part of the average is uncertain). From L's Up mid p_L = P(F ≥ K_L):
μ = K_L + σ Φ⁻¹(p_L), so **fair_T = Φ(Φ⁻¹(p_L) + (K_L − K_T) / σ)**. Only the strike gap in σ units matters; when it is
large fair_T is ~0 or ~1.

**Inputs.** Strikes: official `price_to_beat` (5m/15m/1h/1d); 4h has none recorded, so K = mean of Chainlink ticks
with ts in the 60 s before the window start (as in xtf-arb and the simulator). Books: Up-token `pm_best_bid_ask`
as-of `recv_ms ≤ t` (Down ask = 1 − Up bid). L's mid must be in [0.03, 0.97] and both spreads ≤ 10¢.

**Screen.** Pairs (T, L): same coin, same window_end, T ∈ {1h, 4h, 1d}, L shorter (5m, 15m, 1h). Grid: every 5 s with
τ ∈ [5, 300] s. Signal: buy Up_T if fair − ask_T − fee(ask_T) > m; buy Down_T if bid_T − fair − fee(1 − bid_T) > m
(fee = rate · p(1 − p), T's own rate, 0.01 for 1h/4h/1d). Execution: IOC at the signal price, matched against T's
book at t + 200 ms order latency, + 250 ms taker hold when T is 1h (none for 4h/1d); fills at the then ask if it is
still at or below the limit. 10 shares, held to settlement. One trade per (T market, L timeframe): the first fill.
Visible size is read from `pm_book` at the fill time where available.

**Primary metric.** Settlement markout ¢/share (outcome − price − fee) of the first fills, per pair type and pooled,
on 2026-11-27 with the margin chosen on 2026-11-24, 95% CI from a block bootstrap over end hours (all end hours of
the group are blocks, with or without trades), 2000 resamples. Margin m chosen on 2026-11-24 from
{0.01, 0.02, 0.04, 0.07} (highest pooled ¢/share with ≥ 10 fills), reported unchanged on 2026-11-27.
Also reported: opportunity grid points, episodes (consecutive grid points), markets with a signal, fill rate,
visible size.

**Days.** Choose on 2026-11-24 (windows ending 10:40–24:00 UTC). Report on 2026-11-27 (thin markets whose window
starts on 11-27, data to ~09:50 UTC; book reads from 11-27 00:00). 2026-11-25 / 11-28 are sealed and 11-26
(validation) is not used. The daily market ends at 17:00 UTC, so 11-27 has no daily pair.

**Promotion.** A simulator strategy (`thin-from-liquid`) only if the pooled 11-27 markout is positive (CI lower
bound > 0, or clearly positive with the CI noted).

## Variants tried
Count: **4** (one pricing rule, margin m ∈ {0.01, 0.02, 0.04, 0.07}). Pair types are reporting groups, not variants.
No simulator strategy was built: the screen did not pass its promotion rule.

| Version | What changed | Why |
|---|---|---|
| screen m = 0.01 / 0.02 / 0.04 / 0.07 | taker on the thin market at the L-implied fair ± m | pre-registered grid |

## Results
Code: `research/thin_markets/` (`screen.py` builds the 5 s grid per day, `score.py` scores it, `sizes.py` reads
visible size from `pm_book` snapshots). Screen only, no runs folder. Valid grid points: both spreads ≤ 10¢ and
L's mid in [0.03, 0.97] (most points in the last 300 s fail the mid filter because the 5m market is decided).

**Mechanism check first: the thin market's own mid is as good a forecast as the L-implied fair.** Brier score of
each against T's outcome on the valid grid points (lower is better):

| Pair (T~L) | 11-24 fair | 11-24 T mid | 11-24 mkts | 11-27 fair | 11-27 T mid | 11-27 mkts |
|---|---|---|---|---|---|---|
| 1h~5m | 0.0367 | **0.0337** | 68 | 0.0661 | **0.0623** | 54 |
| 1h~15m | 0.0475 | **0.0465** | 59 | **0.0628** | 0.0632 | 46 |
| 4h~5m | 0.0006 | **0.0001** | 17 | 0.0052 | **0.0031** | 6 |
| 4h~15m | 0.0005 | **0.0001** | 15 | 0.0076 | **0.0044** | 6 |
| 4h~1h | 0.0001 | 0.0000 | 11 | 0.0001 | 0.0001 | 5 |
| 1d~5m / 15m / 1h | 0.0000 | 0.0003 | 2 | — | — | 0 |

The thin books are not stale: they track the same F as the 5m book (4h and daily are usually decided in the last
5 minutes, so there is nothing to price).

**Taker screen** (10 shares, first fill per thin market and L timeframe, 200 ms latency + 250 ms hold on 1h,
¢/share net of fee, 95% CI over end-hour blocks). Pooled over pair types:

| Margin | 11-24 signal pts / episodes / mkts | 11-24 fills | 11-24 ¢/sh [CI] | 11-27 fills | 11-27 ¢/sh [CI] |
|---|---|---|---|---|---|
| 0.01 | 992 / 202 / 62 | 61 | −1.6 [−12.2, 15.2] | 53 | +1.8 [−4.4, 12.6] |
| 0.02 | 837 / 151 / 54 | 53 | −1.9 [−14.2, 17.6] | 38 | +4.9 [−5.7, 17.8] |
| 0.04 | 691 / 138 / 41 | 39 | +1.0 [−14.6, 20.3] | 32 | −4.5 [−14.3, 4.6] |
| **0.07 (chosen on 11-24)** | 502 / 129 / 31 | 30 | +4.0 [−16.5, 31.1] | **18** | **−1.3 [−17.5, 12.6]** |

Chosen margin by pair type (m = 0.07):

| Pair | 11-24 fills | 11-24 ¢/sh [CI] | 11-27 fills | 11-27 ¢/sh [CI] |
|---|---|---|---|---|
| 1h~5m | 14 | +3.9 [−15.8, 39.1] | 11 | −7.4 [−28.2, 8.5] |
| 1h~15m | 13 | +5.8 [−17.9, 44.5] | 7 | +8.1 [−16.1, 34.8] |
| 4h~5m | 2 | −4.5 | 0 (1 signal, unfilled) | — |
| 4h~15m | 1 | −2.0 | 0 (1 signal, unfilled) | — |
| 4h~1h | 0 | — | 0 | — |
| 1d~any | 0 | — | (no daily end in the 11-27 data) | — |

Opportunities/day at m = 0.07: ~130 episodes (median 10 s) on 31 thin markets on 11-24 (13.5 h), 60 episodes on 21
markets on 11-27 (~9 h). Fill rate 74–79% (the quote often moves within the 450 ms). Visible size at the limit at
the fill (last `pm_book` snapshot, median 5 s old): median 10 shares on 11-24, 56 on 11-27, a quarter of fills 0.
Primary metric (11-27, pooled, m = 0.07): **−1.3 ¢/share [−17.5, +12.6] on 18 fills** (−$2.4 at 10 shares). The
margin ranking also flips between days. Not promoted; no simulator run.

## Lessons
- End-aligned markets of one coin share F (xtf-arb), but the thin 1h book prices it as well as the 5m book does in
  the last 5 minutes: whoever quotes the 1h book is not slow at the end. The 25× volume gap does not mean a stale
  price.
- 4h and daily markets are almost always decided by their last 5 minutes (Brier ~0.0001–0.005), so pricing them off
  the 5m market has nothing to trade; an earlier horizon (e.g. the 1h ending inside a 4h window) would need a
  different, non-end-aligned model.
- Disagreements > 7¢ exist (~10 per hour) but taking the thin side of them earns ~0 at settlement (CIs ±15¢ wide).

## Next
Nothing on this rule. If revisited: test the maker side (rest on the thin book at fair ± m) only after a
simulator check of queue position, since the taker screen shows no information advantage to start from.

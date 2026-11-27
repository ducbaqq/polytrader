# thin-markets (thin-from-liquid)

**Family:** cross-timeframe | **Status:** testing
**Current version:** screen only | **sim_version:** a44b1ed9e51f

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
Count: (filled in below)

## Results

## Lessons

## Next

# binance-momentum

**Family:** momentum | **Status:** abandoned
**Current version:** see results | **sim_version:** n/a (screening tier, no simulator)

## Pre-registration (written before any run)
**Hypothesis:** after a strong Binance move in the last N seconds, the Polymarket ask on non-BTC coins may still lag the fair value. The lead-lag study says the response is mostly complete within 1 s, so expect nothing at 5 s snapshot resolution. Rule: |log return of bn_mid over N s| > c * sig15 * sqrt(N), and fair - ask - fee > margin on the side of the move. Variants: (N=10 s, c=2) and (N=30 s, c=2.5), margin 2c. Primary metric: second-half cents per share.

Common protocol: snapshots of the 2026-11-24 train day (one day, about 13 h). Tune on markets with window_start < 17:15 UTC
(first half), report the chosen variant unchanged on the second half. Fills: full ask at +300 ms (robustness +1000 ms),
taker fee, hold to resolution, 10 shares. CIs: 15-minute block bootstrap. Primary metric: cents per share on the second
half at 300 ms. Promising only if second half > 0 with CI lower bound > 0 (or clearly positive, said explicitly).
Depth is not checked (snapshots carry no sizes).

## Variants tried
Count: **2**.
(N=10 s, c=2) and (N=30 s, c=2.5), margin 2c, k=1.2, all non-BTC coins and timeframes, first signal per market.

## Results
Halves: first = markets with window_start < 2026-11-24 17:15 UTC, second = the rest (one train day, no validation days yet).
All numbers are cents per share (a complete set for complete-set-arb), 300 ms latency unless noted, with the 95% 15-minute-block bootstrap CI.
Reproduce: `cd analysis && uv run polydata bt screen binance-momentum` for the whole day; the halves come from
`uv run python -m polydata.research.screen_halves <rule> "<kwargs>"` (the registered defaults are the variant chosen on the first half).

| Variant | First half | Second half | 1000 ms (first / second) |
|---|---|---|---|
| N=10 s, c=2 | n=117, -7.51c [-19.95, +7.51] | n=151, -0.79c [-9.60, +7.69] | -7.91c / -1.51c |
| **N=30 s, c=2.5 (chosen: least bad on the first half)** | n=52, -1.18c [-22.46, +19.05] | n=70, **-1.78c [-13.65, +10.55]** | -1.72c / -1.88c |

By coin x timeframe (N=30 s, second half): every cell has fewer than 14 trades and every interval spans 0 except tiny cells (xrp/1h n=3 -29.9c). Nothing to report beyond noise.

## Lessons
- Point estimates are negative in all four half-variants but every CI spans 0 (no evidence of an edge, none of harm). Consistent with the lead-lag study: when Binance has moved by 2-2.5 sigma over the last 10-30 s, the Polymarket book has
  already repriced (fair - ask is large only when the model is wrong or the book is wide).
- At 5 s snapshots a 1 s response cannot be tested properly; a faster-resolution test belongs to the simulator (on_binance handler) and the study already says
  the pure latency trade is marginal on BTC 5m and negative elsewhere from Singapore.

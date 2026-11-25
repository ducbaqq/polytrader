# fair-value-deviation

**Family:** fair-value | **Status:** needs validation days (screen: positive both halves, CI includes 0)
**Current version:** see results | **sim_version:** n/a (screening tier, no simulator)

## Pre-registration (written before any run)
**Hypothesis:** the calibration study found no edge at 5 s resolution on 5m. Slower markets (15m, 1h) may misprice relative to the fair value model at large margins. Rule: fair - ask - fee > margin, time left in a band, spread <= 10c, 15m and 1h only, first signal per market. Primary metric: second-half cents per share. Grid on first half: margin {2,4,6}c x band {60-300 s, 300-1200 s} (6 variants). Only 44 15m and 10 1h markets per coin on the day: expect low power.

Common protocol: snapshots of the 2026-11-24 train day (one day, about 13 h). Tune on markets with window_start < 17:15 UTC
(first half), report the chosen variant unchanged on the second half. Fills: full ask at +300 ms (robustness +1000 ms),
taker fee, hold to resolution, 10 shares. CIs: 15-minute block bootstrap. Primary metric: cents per share on the second
half at 300 ms. Promising only if second half > 0 with CI lower bound > 0 (or clearly positive, said explicitly).
Depth is not checked (snapshots carry no sizes).

## Variants tried
Count: **6**.
margin {2,4,6}c x time-left band {60-300 s, 300-1200 s}, 15m and 1h only, first signal per market, spread <= 10c, k=1.2.

## Results
Halves: first = markets with window_start < 2026-11-24 17:15 UTC, second = the rest (one train day, no validation days yet).
All numbers are cents per share (a complete set for complete-set-arb), 300 ms latency unless noted, with the 95% 15-minute-block bootstrap CI.
Reproduce: `cd analysis && uv run polydata bt screen fair-value-deviation` for the whole day; the halves come from
`uv run python -m polydata.research.screen_halves <rule> "<kwargs>"` (the registered defaults are the variant chosen on the first half).

First-half grid (300 ms):

| Band | margin 2c | margin 4c | margin 6c |
|---|---|---|---|
| 60-300 s | n=147, +0.06c [-9.7, +11.0] | n=128, -0.11c [-10.3, +10.8] | n=110, -4.02c [-13.3, +5.9] |
| 300-1200 s | **n=182, +5.99c [-6.1, +18.0]** | n=172, -1.26c [-11.4, +9.5] | n=148, -0.02c [-10.6, +10.5] |

Chosen (best first half): margin 2c, band 300-1200 s.

| | n | cents/share [CI] | $/day (10 shares) |
|---|---|---|---|
| First half, 300 ms | 182 | +5.99 [-6.07, +17.99] | +108.93 |
| **Second half, 300 ms (unchanged)** | 179 | **+5.07 [-4.27, +14.23]** | +90.78 |
| First half, 1000 ms | 182 | +5.11 [-7.31, +17.34] | +92.98 |
| Second half, 1000 ms | 179 | +4.92 [-4.37, +14.10] | +87.99 |

Second half by coin, 15m (n, c): eth 24 +13.8 [-6.3, +31.9] · doge 26 +13.2 [-5.5, +30.9] · xrp 26 +9.8 [-8.9, +28.3] · hype 21 +5.0 [-16.0, +26.3] ·
btc 24 +2.3 · bnb 13 +1.7 · sol 26 -4.4 [-22.5, +14.2] · zec 2 -14.6. 1h cells have 1-4 trades each (17 in the second half: xrp 4 -31.2, doge 4 -14.7, eth 4 +23.6, others 1-3).
Every cell's interval spans 0. The 15m markets carry the result; 1h adds little (the 300-1200 s band is a small part of an hour).

## Lessons
- Positive in both halves and robust to 1 s latency, but the second-half CI lower bound is -4.3c: **not promotable** by the screen rule, and the winning
  grid cell was one of six noisy ones (the other five centre on 0, the 60-300 s band and larger margins show nothing).
- Large ¢/share with wide CIs is typical of buying cheap tokens (the standard error of a few hundred binary outcomes is ~10c); one day cannot separate
  +6c from 0. The mechanism is not established; it is also one trending day with strongly correlated coins.
- Next: re-run on the first validation days (2026-11-26+) with exactly these parameters (margin 2c, band 300-1200 s, k=1.2, 15m). If the validation lower bound > 0, write it as a strategy class
  and run it through the simulator (depth matters: 15m books are thinner than BTC 5m).

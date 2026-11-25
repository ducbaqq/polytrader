# settlement-lock

**Family:** endgame | **Status:** abandoned
**Current version:** see results | **sim_version:** n/a (screening tier, no simulator)

## Pre-registration (written before any run)
**Hypothesis:** in the last 60 s, with at least 40 Chainlink ticks already observed in the settlement window, the observed average plus the current price nearly fixes the outcome. The market may still ask less than the model fair value (research.calibration.fair_value, k=1.2). Rule: buy the side with fair - ask - fee > margin, ask <= cap, spread <= 10c, first signal per market. Primary metric: second-half cents per share. Grid on first half: margin {1,2,4}c x cap {0.95,0.97} (6 variants, plus one distance-in-sigma filter variant). Caveat: snapshots carry the official price_to_beat; the simulator derives it from Chainlink (fine for a screen).

Common protocol: snapshots of the 2026-11-24 train day (one day, about 13 h). Tune on markets with window_start < 17:15 UTC
(first half), report the chosen variant unchanged on the second half. Fills: full ask at +300 ms (robustness +1000 ms),
taker fee, hold to resolution, 10 shares. CIs: 15-minute block bootstrap. Primary metric: cents per share on the second
half at 300 ms. Promising only if second half > 0 with CI lower bound > 0 (or clearly positive, said explicitly).
Depth is not checked (snapshots carry no sizes).

## Variants tried
Count: **18**.
Grid on the first half (fair-value rule, `margin` x `ask_cap`): margin {1,2,4}c x cap {0.95,0.97} = 6, plus a distance-in-sigma filter (2 sigma)
= 7. Then the user's stated form (distance >= k sigma, buy if ask <= cap, margin disabled): k in {0.5,1,2,3} = 4, cap {0.98, 0.99} x k {1,2} = 4,
margin {1,2,4}c at cap 0.99 = 3. The first run of the distance form had a bug (see lessons); the counts include the buggy runs.

## Results
Halves: first = markets with window_start < 2026-11-24 17:15 UTC, second = the rest (one train day, no validation days yet).
All numbers are cents per share (a complete set for complete-set-arb), 300 ms latency unless noted, with the 95% 15-minute-block bootstrap CI.
Reproduce: `cd analysis && uv run polydata bt screen settlement-lock` for the whole day; the halves come from
`uv run python -m polydata.research.screen_halves <rule> "<kwargs>"` (the registered defaults are the variant chosen on the first half).

Chosen on the first half: fair-value rule, margin 1c, ask cap 0.97 (least bad of the grid).

| Variant | First half | Second half | 1000 ms (first / second) |
|---|---|---|---|
| margin 1c, cap 0.97 (registered default) | n=40, -3.68c [-7.78, +0.81] | n=39, **-3.40c [-9.78, +3.83]** | -3.37c [-6.52, -0.04] / -3.02c [-9.00, +4.09] |
| margin 2c, cap 0.97 | n=36, -3.98c [-8.46, +1.05] | not run | |
| margin 4c, cap 0.97 | n=29, -7.29c [-10.22, -4.81] | not run | |
| distance >= 0.5 / 1 / 2 / 3 sigma, cap 0.97 or 0.98/0.99 | n = 1 / 0 / 0 / 0 | n = 0 | |

Locked states in the last 60 s with >= 40 observed ticks (first half, 4,388 rows): when |avg - ptb| is below 1 sigma the favourite wins 93% (n=470);
at 1 sigma and beyond it wins **100%** (n=2,178) but the favourite's ask is already 0.999-1.000 in every one of those rows (never <= 0.99).

## Lessons
- When the settlement average locks the outcome the book already shows it: there is nothing to buy below 0.99, and at 0.999+ the fee exceeds the 0.1c left.
  The market is efficient inside the lock window; that is the answer to the "buy at <= 0.97" form.
- The model-fair-value form (fair - ask - fee > margin) finds only 40 trades (39 in the second half) and they lose (-3.7c and -7.4c per share), so the model's edge
  signal here is noise, not information.
- **Bug caught:** with a permissive margin the first distance form picked the cheap *losing* token whenever the locked favourite's ask was above the cap
  (a 1c ticket, all losers, -1.01c/share; looked like a stable -0.5c edge). Fixed by restricting to the side the observed average points to (test added).
- Screen-only caveat: snapshots carry the official `price_to_beat`; the simulator's strategy-visible one comes from Chainlink. Irrelevant for the rejection.

# complete-set-arb

**Family:** complete-set-arb | **Status:** abandoned
**Current version:** see results | **sim_version:** n/a (screening tier, no simulator)

## Pre-registration (written before any run)
**Hypothesis:** buying one Up and one Down share when up_ask + dn_ask + both taker fees < 1 locks in 1 - cost whatever the outcome. Mechanism: transient book dislocations between the two tokens. Expectation: rare, and gone after 300 ms latency and fees. Primary metric: second-half cents per set (token `Both`, fee per leg), first signal per market. Also reported: how often the condition occurs and the edge per set at signal time. Variants: margin 0 only (plus latency robustness).

Common protocol: snapshots of the 2026-11-24 train day (one day, about 13 h). Tune on markets with window_start < 17:15 UTC
(first half), report the chosen variant unchanged on the second half. Fills: full ask at +300 ms (robustness +1000 ms),
taker fee, hold to resolution, 10 shares. CIs: 15-minute block bootstrap. Primary metric: cents per share on the second
half at 300 ms. Promising only if second half > 0 with CI lower bound > 0 (or clearly positive, said explicitly).
Depth is not checked (snapshots carry no sizes).

## Variants tried
Count: **1**.
Margin 0 (any edge after both fees); rows with `Both` signals; also checked the gross condition (no fees) and the mirror (sell both: bids sum > 1).

## Results
Halves: first = markets with window_start < 2026-11-24 17:15 UTC, second = the rest (one train day, no validation days yet).
All numbers are cents per share (a complete set for complete-set-arb), 300 ms latency unless noted, with the 95% 15-minute-block bootstrap CI.
Reproduce: `cd analysis && uv run polydata bt screen complete-set-arb` for the whole day; the halves come from
`uv run python -m polydata.research.screen_halves <rule> "<kwargs>"` (the registered defaults are the variant chosen on the first half).

| Check | Result |
|---|---|
| Snapshot rows (210,180, 1,751 markets, whole day) with up_ask + dn_ask < 1 (gross, no fee) | **0** |
| Rows with up_ask + dn_ask <= 1.000 | 2 |
| Minimum of up_ask + dn_ask, any coin/timeframe | 1.000 (1.001 for most); median 1.03 |
| Asks sum at +300 ms below 1 | 0 |
| Mirror: up_bid + dn_bid > 1 (sell both) | 1 row |
| Trades after fees, either half, 300 or 1000 ms | none |

Depth was not checked (not needed: there is no signal).

## Lessons
- The two asks never sum to less than 1 in 5-second snapshots; with fees the break-even sum is about 0.97 to 0.99. Neither side of the pair
  is ever mispriced against the other at that resolution. A sub-second dislocation could exist between snapshots; the recorder's
  event stream (`pm_best_bid_ask`) would show it, but the 300 ms order latency would kill it anyway.
- Token `Both` is now supported by `screen.fills` (payoff 1, a fee per leg) for any later set-based idea.

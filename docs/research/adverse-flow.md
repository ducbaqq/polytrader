# Adverse flow: what predicts a toxic maker fill?

**Status:** pre-registered (written before any feature was computed) | **Data:** train day 2026-11-24 from
10:36 UTC only | **sim_version of the fills:** 2127ec99eafd

## Question
The baseline market maker (`mm-binance-pull` defaults, run `2026-11-25T0056-0abdaa`) earns about +0.6 c/share of
spread and loses about -1.4 to -1.7 c/share to adverse selection. Pulling on Binance moves helped in the first
half of the day only; in the second half 46% of fills had no Binance move in the 2 s before them and still lost
~1.5 c/share. Which features, observable *before* the fill, predict the adverse fills, and is any of them stable
across both halves? Offline only: the fills of the baseline run are scored, no new simulator run is needed for
the diagnosis.

## Pre-registration
- **Fills.** Baseline fills, settled, split per market: H1 = `window_start` < 2026-11-24 17:15 UTC (1,448
  fills), H2 = the rest (1,561 fills).
- **Targets.** Per fill, in cents per share, in our direction (Up terms, + = good for us):
  adverse = mid 10 s after the fill - mid 200 ms before (primary); markout = spread + adverse (secondary).
  Means are size-weighted.
- **Features are point-in-time.** Every feature uses only data with `recv_ms <= fill ts_ms - 200 ms` (the
  latest moment a cancel could still beat the fill at the default 200 ms latency), joined as-of on `recv_ms`;
  never exchange timestamps, never the outcome or anything after the fill. Signed features are multiplied by
  the fill direction so that + means "pressure in favour of the position we just got".
- **Feature list (fixed now; any later addition is counted).**
  - A. Polymarket flow, same market: A1/A2/A3 signed taker volume over 1/5/30 s; A4 trade count over 5 s.
  - B. Book (strategy view, recv clock): B1 signed top-of-book size imbalance; B2 spread (ticks); B3 size of
    the touch on the side we rest on; B4 change of our side's depth (top 3 levels) over the last 2 s; B5
    signed Polymarket mid change over the last 2 s; B6 queue ahead at the fill (simulator state, diagnostic).
  - C. Chainlink: C1 signed Binance-futures mid minus last Chainlink price (bp); C2 signed Chainlink price
    minus `price_to_beat` (bp); C3 age of the last Chainlink tick.
  - D. Binance: D1/D2/D3 signed futures mid return over 2/10/60 s; D4 signed change of the futures-spot
    basis over 2 s; D5 signed futures taker imbalance over 5 s; D6 liquidation notional over 30 s.
  - E. Context: E1 time left in the window; E2 |mid - 0.5|; E3 signed moneyness (mid - 0.5 in our
    direction: are we buying the favourite or the longshot); E4 timeframe; E5 coin.
  - F. Cross-timeframe: F1 signed mid change over 5 s of the same coin's other open markets; F2 their
    signed taker volume over 5 s.
  - 27 features in total.
- **Method.** Quintile edges are computed on H1 and applied unchanged to H2. For each feature: per-quintile
  fills, spread, adverse and markout on H1 and on H2.
- **Stable** = the H1-worst quintile (lowest mean adverse) is worse than the other four quintiles together by
  at least 1.0 c/share on H1, AND the same bucket (H1 edges) is worse than the rest on H2 by at least 0.5
  c/share with a market-clustered bootstrap 95% CI that excludes 0. Categorical features (E4, E5) use their
  categories instead of quintiles.
- **Avoidance bound.** For stable features, the H2 markout after dropping the flagged bucket (chosen on H1) is
  reported. It is an upper bound: it assumes the other fills stay as they are, and ignores that a pull also
  loses queue position and some good fills.
- Multiple comparisons: with 27 features and 5 buckets, a few spurious H1 "worst buckets" are expected; the
  H2 confirmation is the filter.

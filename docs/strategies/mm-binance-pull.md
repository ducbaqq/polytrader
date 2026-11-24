# mm-binance-pull

**Family:** market-making | **Status:** testing
**Current version:** not yet implemented (baseline strategy of the backtester; see the design spec)

## Hypothesis
Providing liquidity is profitable on average: makers' losses come mostly from trades that follow
Binance moves. A market maker that quotes bid and ask around the mid and pulls all quotes within
~100-200 ms of a Binance move larger than `k x sigma` should keep the spread and avoid most of the
informed flow. Quotes are skewed against inventory, inventory is capped per market, and quoting stops
`stop_before_end_s` before the window ends. See
[the design spec](../superpowers/specs/2026-11-24-backtester-design.md) (baseline strategy) and the
[calibration study](../research/calibration-study.md).

## Variants tried
Count: 0.
| Version | What changed | Why |
|---|---|---|

## Results
No results yet (the sanity strategy `join-best` exists to test the fill model; its runs are under the
`sanity` family). Placeholder: results will be listed here, with links to run folders, once the
strategy has been implemented and run.

## Lessons
None yet.

## Next
Implement the strategy, run it on train days, then sweep `half_spread`, `k`, `pull_window_ms` and
`cooldown_ms` on the validation days.

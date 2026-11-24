# mm-binance-pull

**Family:** market-making | **Status:** testing
**Current version:** 1f2544856f6c (`analysis/src/polydata/bt/strategies/mm_binance_pull.py`, config
`analysis/configs/mm-binance-pull.yaml`)

## Hypothesis
Providing liquidity is profitable on average: makers' losses come mostly from trades that follow
Binance moves. A market maker that quotes bid and ask around the mid and pulls all quotes within
~100-200 ms of a Binance move larger than `k x sigma` should keep the spread and avoid most of the
informed flow. Quotes are skewed against inventory, inventory is capped per market, and quoting stops
`stop_before_end_s` before the window ends. See
[the design spec](../superpowers/specs/2026-11-24-backtester-design.md) (baseline strategy) and the
[calibration study](../research/calibration-study.md).

## Variants tried
Count: 2 (the defaults before and after the pre-window gate). The `pull_k=1e6` and
`quote_before_start=true` runs below are diagnostic ablations, not candidates.
| Version | What changed | Why |
|---|---|---|
| bca155741816 | first implementation, default params | baseline |
| 1f2544856f6c | `quote_before_start: false` (new default): no quotes before `window_start_ms`; `t_max_s` still counts from the end | before the start the strike is unknown, so the Binance link does not hold (review ruling, Task 9) |

## Results
All on train data only: 2026-11-24 10:36-17:50 UTC (~7.2 h, one train day), sim_version a0f42186e4cc.
Attribution per share is over settled fills (spread / adverse / inventory, metrics.attribution).
"After move" = share of fills with a > 2 sigma Binance move in the second before the fill.

| Run | Scope | Kind | P&L | Fills (shares) | Spread / adverse / inventory, cents per share | After move | Notes |
|---|---|---|---|---|---|---|---|
| `data/runs/market-making/mm-binance-pull/2026-11-24T2233-c82aee` | 8 coins x 5m/15m/1h | train | -$351 | 5,336 (36.3k) | +0.67 / -1.87 / +0.23 | 2.7% | defaults; 85/946 markets voided |
| `data/runs/market-making/mm-binance-pull/2026-11-24T2241-b1a1de` | same | train | -$763 | 8,040 (54.3k) | +0.68 / -2.32 / +0.23 | 13.0% | ablation: pulls off (`pull_k=1e6`) |
| `data/runs/sanity/join-best/2026-11-24T2237-fcfc8b` | same | train | -$637 | 13,533 (59.0k) | +0.36 / -1.77 / +0.32 | 11.1% | join-best (size 5), for reference |
| `data/runs/market-making/mm-binance-pull/2026-11-24T2229-674f91` | btc+eth x 5m/15m | train | -$266 | 4,421 | totals +$196 / -$564 / +$102 | 2.6% btc, 3.6% eth | speed check: 76.6 s for 7.2 h |

**After the pre-window gate (1f2544856f6c).** The data store had grown by then (10:36-21:50 UTC, ~11.2 h,
still one train day), so the gated defaults were run next to the ungated variant on the same range:

| Run | Scope | Kind | P&L | Fills (shares) | Spread / adverse / inventory, $ | After move | By timeframe 5m / 15m / 1h |
|---|---|---|---|---|---|---|---|
| `data/runs/market-making/mm-binance-pull/2026-11-24T2252-342179` | 8 coins x 3 tf, 10:36-21:50 | train | -$427.91 | 7,959 (54.7k) | +371 / -1,030 / +231 | 2.6% | -396 / -61 / +29 |
| `data/runs/market-making/mm-binance-pull/2026-11-24T2258-3c9ae4` | same, `quote_before_start=true` | train | -$457.28 | 8,526 | +403 / -1,145 / +285 | 2.7% | -425 / -62 / +29 |

On the markets ending by 17:45 (the first runs' range): gated -$322 vs ungated -$341 (the first run had
-$351 there; a few more markets are unvoided in the refreshed data). The gate saves ~$20-30 and ~7% of
fills; per-share adverse selection is unchanged (-1.80 vs -1.85 cents). 1h is identical (with
`t_max_s` 3600 a 1h market is only quoted after its start anyway).

By timeframe (first defaults run): 5m -$377, 15m +$9, 1h +$17. BTC carries most of the fills (3,855 of 5,336).

## Lessons
- **Pulling works on the flow it targets.** Against the same strategy with pulls off, it cuts the
  fills that follow a Binance move from 13% to 2.7%, adverse selection from -2.32 to -1.87 cents
  per share, and the loss from -$763 to -$351.
- **It is not enough.** The remaining fills are still adversely selected (-1.8 cents per share on
  fills with no 2 sigma move before them) against +0.67 cents of spread. Quiet-looking flow is
  informed too, at least at a 1-tick-inside-the-touch quote on 5m markets; 15m and 1h are about
  flat. One train day only: no conclusion about the edge yet.
- Quoting before the window start was a small net loss, not the main problem: 5m markets lose
  either way.
- Churn is high: ~515k orders in 7.2 h over 8 coins (requote at every 1-tick target move, plus
  pulls). Fine in the simulator; the live runner will need a rate budget.

## Next
More train days first. Then sweep `half_spread`, `k`, `pull_window_ms` and
`cooldown_ms` on the validation days.

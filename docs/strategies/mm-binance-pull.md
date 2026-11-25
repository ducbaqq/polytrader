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
All on train data only: 2026-11-24 10:36-17:50 UTC (~7.2 h, one train day), sim_version a0f42186e4cc unless noted.
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

**After the at-price volume cap (sim_version a3cf78718e4d, final review fix).** A recorded trade now fills or
advances our queue at its own price only up to the level's recorded drop in the 150 ms before it (sweeps are
printed as one row at the worst price). Same strategy version and range as `342179`:

| Run | Scope | Kind | P&L | Fills (shares) | Spread / adverse / inventory, $ | After move | By timeframe 5m / 15m / 1h |
|---|---|---|---|---|---|---|---|
| `data/runs/market-making/mm-binance-pull/2026-11-25T0010-83d538` | 8 coins x 3 tf, 10:36-21:50 | train | -$448.22 | 7,739 (53.3k) | +353 / -1,037 / +236 | 2.5% | -405 / -70 / +27 |

The cap removes 2.8% of fills (2.5% of shares), mostly cheap ones: spread earned drops by $18 while adverse
selection is unchanged, so the loss grows by $20. The earlier numbers were slightly optimistic; the conclusion
(5m loses, 15m/1h about flat) does not change.

**After the trade-through cap (sim_version 2127ec99eafd).** A trade printed at a price worse than our quote
now fills us only with `max(0, x - B - Q)`: what is left of the (one-row) sweep after the better levels' recorded
drops and the queue ahead of us. Same strategy version and range:

| Run | Scope | Kind | P&L | Fills (shares) | Spread / adverse / inventory, $ | After move | By timeframe 5m / 15m / 1h |
|---|---|---|---|---|---|---|---|
| `data/runs/market-making/mm-binance-pull/2026-11-25T0044-2b7f3e` | 8 coins x 3 tf, 10:36-21:50 | train | -$201.24 | 2,636 (18.2k) | +106 / -300 / -7 | 2.7% | -135 / -80 / +13 |

Two thirds of the fills are gone (7,739 to 2,636): most earlier fills were trade-throughs that our queue, not
us, would have absorbed. The loss shrinks with volume, but per share the picture is the same: spread +0.58 c
against adverse -1.65 c (before: +0.66 / -1.94). Still a loser; 5m remains the problem. The join-best
validation run drops the same way (12,601 to 5,234 fills).

On the markets ending by 17:45 (the first runs' range): gated -$322 vs ungated -$341 (the first run had
-$351 there; a few more markets are unvoided in the refreshed data). The gate saves ~$20-30 and ~7% of
fills; per-share adverse selection is unchanged (-1.80 vs -1.85 cents). 1h is identical (with
`t_max_s` 3600 a 1h market is only quoted after its start anyway).

By timeframe (first defaults run): 5m -$377, 15m +$9, 1h +$17. BTC carries most of the fills (3,855 of 5,336).

## Research round 2026-11-25 (branch `research-mm`): pre-registration
Written before any run of this round.

- **Hypothesis.** The loss is adverse selection (-1.65 c/share against +0.58 c of spread). Variants that
  (a) avoid the markets where flow is most informed (BTC, 5m), (b) demand more edge per fill (wider
  `half_spread`, quote off the fair value instead of the mid), (c) pull harder, or (d) avoid the end of the
  window and wide books should cut adverse selection per share faster than they cut spread.
- **Primary metric.** P&L ($) on the first half of 2026-11-24 (markets with `window_start` < 17:15 UTC),
  read with the per-share attribution. Variants are chosen on the first half only; the chosen one is then
  reported on the second half (>= 17:15) unchanged, with a 15-minute-block bootstrap CI, and at 400 ms.
- Each run covers the whole day; the halves are split per market afterwards
  (`window_start_ms` from `markets.parquet`). Second-half numbers are read only for the chosen variants.

### Round 1 results (config only; whole day 2026-11-24 replayed, sim_version 2127ec99eafd)
Run folders are `data/runs/market-making/mm-binance-pull/<id>` (scratch data dir of the research session).
"Markout" = spread + adverse (the P&L of a fill marked to the mid 10 s later); the inventory part is
mostly the coin flip of settlement and dominates P&L at these volumes (first-half block CIs are about
+-$100 wide for every variant). Variants tried in this round: 10 config variants (plus the baseline rerun).

| Variant | Run | First half P&L | 95% CI | Fills | Spread / adverse / inventory, c/share | Markout c/share |
|---|---|---|---|---|---|---|
| baseline (defaults) | `2026-11-25T0056-0abdaa` | -$130.78 | [-298, 27] | 1,452 | +0.54 / -1.42 / -0.41 | -0.88 |
| (a) 15m + 1h only | `2026-11-25T0114-263382` | -$39.70 | [-118, 40] | 302 | +0.82 / -2.34 / -0.27 | -1.52 |
| (a) 1h only | `2026-11-25T0108-a60bc0` | +$6.85 | [-9, 19] | 49 | +0.88 / -1.61 / +2.43 | -0.73 |
| (a) no BTC | `2026-11-25T0124-13d704` | -$47.89 | [-169, 70] | 526 | +0.75 / -1.89 / -0.23 | -1.14 |
| (b) `half_spread` 0.02 | `2026-11-25T0131-784837` | -$18.22 | [-148, 110] | 626 | +0.81 / -2.04 / +0.81 | -1.23 |
| (b) `half_spread` 0.03 | `2026-11-25T0147-f07337` | -$1.97 | [-87, 78] | 346 | +1.24 / -2.29 / +0.97 | -1.05 |
| (c) `center` fair | `2026-11-25T0213-d82d46` | -$5.70 | [-475, 460] | 2,323 | +0.10 / +0.58 / -0.72 | +0.68 |
| (d) `pull_k` 1.0 | `2026-11-25T0226-869942` | -$13.74 | [-122, 91] | 551 | +0.53 / -0.39 / -0.50 | **+0.14** |
| (d) `cooldown_ms` 5000 | `2026-11-25T0258-8d2c16` | +$19.02 | [-130, 155] | 1,002 | +0.55 / -1.16 / +0.88 | -0.61 |
| (e) `max_book_spread` 0.02 | `2026-11-25T0319-0e4644` | -$92.43 | [-219, 18] | 1,180 | +0.47 / -1.51 / -0.07 | -1.04 |
| (e) `t_min_s` 150 (5m: first half of the window only) | `2026-11-25T0331-cd5d80` | -$119.99 | [-260, 6] | 986 | +0.62 / -1.19 / -1.16 | -0.57 |

Second half (>= 17:15 UTC), unchanged, for the variant carried forward and the baseline:

| Variant | Second half P&L | 95% CI | Fills | Spread / adverse / inventory, c/share | Markout |
|---|---|---|---|---|---|
| baseline | -$93.46 | [-230, 45] | 1,562 | +0.64 / -1.73 / +0.23 | -1.09 |
| `pull_k` 1.0 | -$105.56 | [-235, 36] | 890 | +0.79 / -1.55 / -0.88 | -0.76 |

Reading:
- Timeframe/coin restrictions just select subsets of the baseline's per-market results (the strategy is per
  market); they need no run. 1h is the only positive slice but has 49 fills in 6.5 h.
- Wider spreads raise spread earned per fill but adverse grows with it: we are filled only when the price
  runs through us. Markout stays about -1 c.
- `center: fair` turns the market maker into a directional trader: markout positive but no spread and a
  huge inventory variance (CI +-$470). Not a market maker any more; the calibration study says the model
  does not beat the mid.
- A stricter symmetric pull (`pull_k` 1) is the only config that brought the first-half markout to about
  zero; it did not hold in the second half (-0.76 c).
- Tight-books-only and early-window-only did not help.
- Round 2 (one-sided pull) is in [mm-side-pull](mm-side-pull.md).

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
Round 1 and 2 (2026-11-25) found no config or one-sided pull that survives the second half of the one train
day. See [mm-side-pull](mm-side-pull.md#next) for what to test next. More train days first.

## Queue-model sensitivity (controller, 2026-11-25)
Question: is the adverse selection caused by our pessimistic queue model (`cancel_model: behind`, so a resting quote only
fills after the whole queue ahead trades, mostly on sweeps)? Same config and day (2026-11-24) with `cancel_model: proportional`
(others' cancels also shrink the queue ahead of us), sim_version 2127ec99eafd, run `2026-11-25T0519-a0c1a9`:

| Cancel model | Fills | P&L | Spread ¢/sh | Adverse ¢/sh | Inventory ¢/sh | Markout ¢/sh |
|---|---|---|---|---|---|---|
| behind (default), run `2026-11-25T0056-0abdaa` | 3,014 | −$224 | +0.59 | −1.58 | −0.08 | −0.99 |
| proportional | 9,182 | −$353 | +0.81 | −1.66 | +0.26 | −0.85 |

A better queue position triples the fills and earns more spread, but adverse selection per share is unchanged. The loss is not an artefact
of the queue model: symmetric quoting at the touch loses ~1.6¢/share to the next 10 s under either model. Market making in this form is
dead in simulation; the next MM idea needs a different source of edge (where we quote, or when), not a better pull trigger.

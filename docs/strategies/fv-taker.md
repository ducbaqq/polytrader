# fv-taker

**Family:** fair-value | **Status:** abandoned as pre-registered (primary metric failed); margin 3-4c variants are post hoc, see Next
**Current version:** 423d85963783 (`analysis/src/polydata/bt/strategies/fv_taker.py`, config `analysis/configs/fv-taker.yaml`) | **sim_version:** 2127ec99eafd

## Pre-registration (written before any run)
**Hypothesis:** the `fair-value-deviation` screen (15m/1h, margin 2c, 300-1200 s left) earned +6.0c/share (first half) and
+5.1c (second half) on 5 s snapshots, with CIs spanning 0. If a real edge sits behind it, it should survive the simulator, which walks the real book
(depth, impact), adds order and report latency, derives `fair_value` online and takes `price_to_beat` from received Chainlink ticks. Who loses: takers who
quote 15m books from a model that ignores the Binance-Chainlink nowcast.

**Strategy:** for each 15m/1h market with 300-1200 s left, on every book change and every second: p = `ctx.fair_value`; for each token, limit = largest tick price
with limit + taker fee <= fair - margin (the screen's rule fair - ask - fee > margin); spread <= 10c; size = min(10, visible ask size at prices <= limit), at
least the minimum order size; IOC take of the best-edge token; one entry per market (`max_entries`), a zero-fill order may be retried (<= 5 orders, 1 s apart);
hold to resolution.

**Primary metric:** second-half cents per share (market P&L including fees / shares bought) at the default 200 ms order latency, margin 2c, size 10, with a 15-minute
block bootstrap CI. Promising only if the second half is positive with CI lower bound > 0 (or clearly positive, said explicitly). Halves: first = markets with
window_start < 2026-11-24 17:15 UTC, second = the rest. No tuning: the screen's chosen cell is run as is. Robustness (few variants, all reported): margin 3c and 4c,
size 5 and 20, 5m markets (expected to fail), `--latency-sweep` (100/200/400 ms).

## Variants tried
Count: **6** strategy variants (the pre-registered default, margin 3c, margin 4c, size 5, size 20, 5m), run at 200 ms, plus the 100/400 ms latency runs of the default (a simulator
setting, not a variant). One more run was discarded as a no-op (below). The screen's own 6-cell grid (upstream) chose margin 2c and the 300-1200 s band, so the
selection pressure behind the default is larger than 6.
| Version | What changed | Why |
|---|---|---|
| 423d85963783 | defaults: margin 0.02, band 300-1200 s, 15m+1h, size 10, fee-aware limit | pre-registered mirror of the screen |
| margin 0.03 / 0.04 | `params.margin` | robustness |
| size 5 / 20 | `params.size` | robustness (depth) |
| 5m only, band 60-290 s | `timeframes=[5m]`, `min_tau_s=60`, `max_tau_s=290` | robustness (expected to fail) |

A first 5m attempt used `timeframes=[5m,15m,1h]` with the unchanged band 300-1200 s. A 5m window is only 300 s long, so it never has more than 300 s left: it added no trades
(identical P&L to the default, 383 fills) and is not counted as a result. The 5m variant above uses the band 60-290 s instead (the screen's other band was 60-300 s).

## Results
All on the one train day 2026-11-24 (10:36-24:00 UTC), 8 coins, sim_version 2127ec99eafd, 80 of 500 15m/1h markets voided (gaps; 57 of 1256 for 5m).
Cents per share with the 95% 15-minute-block bootstrap CI (all non-voided markets' blocks, traded or not; same as the screens).
Reproduce a run's halves: `cd analysis && uv run python -m polydata.research.bt_halves <run_dir> [by]`.

| Run (data/runs/fair-value/fv-taker/...) | Variant | First half | Second half | Day $ |
|---|---|---|---|---|
| `2026-11-25T0229-3629fa` | **default, 200 ms (primary)** | n=182 +9.56c [-2.2, +21.1] | **n=179 -4.88c [-13.1, +4.0]** | +76.91 |
| `2026-11-25T0402-57ea3a` | default, 100 ms | n=183 +10.99c [-1.4, +22.8] | n=182 -2.66c [-11.6, +6.6] | +129.72 |
| `2026-11-25T0406-fdd0f9` | default, 400 ms | n=179 +9.72c [-2.0, +21.8] | n=176 -2.62c [-11.7, +7.2] | +110.72 |
| `2026-11-25T0258-407bb4` | margin 3c | n=172 +7.40c [-3.4, +19.4] | n=171 +4.17c [-6.0, +14.3] | +168.56 |
| `2026-11-25T0329-33644d` | margin 4c | n=163 +7.22c [-3.0, +18.5] | n=170 +3.68c [-6.8, +15.1] | +156.99 |
| `2026-11-25T0339-094ccf` | size 5 | n=182 +8.13c [-3.1, +19.6] | n=179 -5.24c [-13.1, +3.9] | +27.50 |
| `2026-11-25T0348-c70f4a` | size 20 (mean fill 13.1 shares) | n=182 +11.03c [-0.8, +22.3] | n=179 -3.47c [-11.9, +6.1] | +195.93 |
| `2026-11-25T0409-b95805` | 5m only, band 60-290 s | n=499 -0.37c [-7.5, +7.3] | n=508 +2.26c [-3.7, +8.1] | +80.57 |
| screen (upstream, 300 ms, 10 shares, full ask) | fair-value-deviation | n=182 +5.99c [-6.1, +18.0] | n=179 +5.07c [-4.3, +14.2] | |

Day total of the default, whole-day: +2.52c/share [-5.2, +10.0] (n=361, 3,050 shares, $+76.91), 15m +3.0c, 1h -2.0c (34 trades). Second half by coin (default): sol -23.6c [-37.8, -6.5]
(n=29), btc -8.8c, xrp -4.9c, zec -19c (n=4), hype -0.2c, doge +1.2c, eth +5.5c, bnb +4.9c; every other cell spans 0.

Primary metric: **second half -4.88c, CI [-13.1, +4.0]: fails** (not positive). The first half (+9.56c, CI lower bound -2.2) is not tuned either, but it is the half the screen was tuned on
and the point estimate is not reproduced out of sample.

### Why the simulator differs from the screen
`bt run` 200 ms vs the screen's 300 ms; both take the first signal per market, so the trade counts match (182 + 179 each), but they are not the same trades.
Per-market join (`uv run python -m polydata.research.fv_taker_vs_screen <run_dir>`):

| | First half | Second half |
|---|---|---|
| markets both trade | 180 | 175 |
| same token | 143 (screen +7.22c, sim +8.52c) | 146 (screen -0.46c, sim +0.45c) |
| different token | 37 (screen +1.66c, sim +15.06c) | 29 (screen +37.26c, sim -33.06c) |
| only one of the two trades | 2 + 2 | 4 + 4 |
| fill price minus screen price | +0.15c mean, 0.00c median | -0.68c mean, 0.00c median |
| fill size (of 10) | mean 8.57, 30% partial | mean 8.45, 33% partial |

- **Depth: not the explanation.** The simulator fills at the same price as the screen's full-ask assumption (median difference 0.0c) and gets 8.5 of 10 shares on average
  (30% of the trades are partial, because the take is sized to the visible ask at the limit). On the same-token trades the two give the same answer in both halves
  (-0.46c vs +0.45c in the second half). Size 5 and 20 give the same sign by half as size 10.
- **Latency: not the explanation.** 100 / 200 / 400 ms give +129.72 / +76.91 / +110.72 dollars on the day and second halves of -2.7c / -4.9c / -2.6c: no trend, and the
  differences are trade-selection noise, not decay (the take hits the ask; the ask rarely moves in 100-400 ms on these markets).
- **The whole gap is trade selection on about 20% of markets.** In 66 markets (37 + 29) the simulator picks the opposite token to the screen, and median 23-28 s earlier (it evaluates on every
  book change and every second, the screen every 5 s on snapshot fair value). In the first half that group favours the simulator (+15c vs +1.7c), in the second half the screen
  (+37c vs -33c). In those markets the model's signal flips between the sides, and the first signal is a coin flip about which side wins. Pooled over the day the screen's picks in those 66 markets average about +17c/share
  and the simulator's about -5c/share; with a per-trade standard deviation near 50c that is roughly 2-3 standard errors, so not clearly noise, not proven systematic. I did not
  isolate whether the cause is the online `fair_value` (Chainlink last price nowcast with Binance, sigma from the online 15-minute window) vs the snapshot's, or `price_to_beat`
  computed from Chainlink ticks (the recorder doc says it reproduces Gamma's to ~0.2 bp median, small next to a 2c margin) vs the official one in the snapshots; the simulator cannot export either
  without a strategy log. That would be the next diagnostic.
- Sensitivity to selection is itself the finding: moving the margin from 2c to 3c keeps 171 of 179 second-half trades but turns -4.9c into +4.2c, which means ~8 trades (or the entry time shifted by
  the larger margin) move the result by 9c. A result that swings that much on 8 trades is not an edge estimate.

## Lessons
- The screen's +5c in the second half does not survive the simulator (-4.9c [-13.1, +4.0]); the first half is higher (+9.6c). Both CIs span 0 and the halves disagree in sign: one day cannot show an edge here.
- Depth and latency cost almost nothing for a taker of this size on 15m books (same fill price as the screen, 85% of the size, no latency trend). That is useful to know for later taker strategies: sizes of 10-20 shares are executable at the visible ask.
- The difference between "first signal in a 5 s snapshot" and "first signal on a live book" is large when the signal flips: with first-signal-per-market entries the outcome depends on which side
  crosses the margin first. A future version should consider requiring persistence (the edge held for N seconds) or taking the larger of the two edges only.
- Margin 3c/4c are positive in both halves (+7.4/+4.2c and +7.2/+3.7c) but they are 2 of 6 cells, picked after seeing both halves, and their intervals also span 0: no evidence. 5m is flat (-0.4c, +2.3c) rather than clearly
  negative: no edge, no loss, consistent with the calibration study.
- Per-coin second-half results are dominated by SOL (-23.6c, n=29): single-coin cells have no power.

## Next
- No validation days exist yet (first: 2026-11-26). Do not tune further on 2026-11-24. If the first validation day is looked at, run exactly the default and, at most, margin 3c as a second pre-registered cell named now
  (post hoc here, so on validation it counts as a fresh trial). The default is expected to fail again.
- Cheap diagnostic if this idea is revisited: log fair, ask, tau and `price_to_beat` at each entry (`ctx.log`) and compare to the snapshot's `p` at the nearest 5 s to see whether online `fair_value` or Chainlink `price_to_beat` differs systematically.

## Follow-up: is `price_to_beat` the cause of the opposite-token picks? (controller, 2026-11-25)
No. For every 2026-11-24 market, the mean of the Chainlink ticks over [window_start − 60 s, window_start), the rule the simulator uses,
matches the official `priceToBeat` within 0.10–0.22 bp median (p90 0.36–0.86 bp) for every coin. Using the last tick instead would be
off by 2–5 bp. The selection gap is timing: the simulator evaluates on every event and takes the first trigger (`max_entries` 1), while the screen
looks every 5 s, so they enter at different moments and sometimes on different sides. The simulator is the authority.

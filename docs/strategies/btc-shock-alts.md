# btc-shock-alts

**Family:** latency-arb | **Status:** testing (needs validation days) | **Strategy code:** `btc-shock-alts` | **sim_version:** a44b1ed9e51f

## Hypothesis
After a sharp BTC move, alt Up/Down books (ETH, SOL, XRP, DOGE, BNB, HYPE, ZEC) on Polymarket may stay stale for a
few hundred ms, because single-feed alt bots wait for their own coin's Binance price, and alt Binance itself may lag
BTC. A taker who reads the BTC move and buys the favoured side of the alt market before it reprices would be paid
by the stale alt makers. Idea 7 in [strategy ideas](../research/strategy-ideas.md). Prior evidence against:
[binance-momentum](binance-momentum.md) found books already repriced at 10-30 s; the calibration study found alt 5m
books reprice to their own Binance in 200-400 ms. Our handicap: a 250 ms Polymarket taker hold, plus 200 ms order
latency from Singapore (about 50 ms from Amsterdam).

## Pre-registration (written before any number was computed)
- **Data:** choose on 2026-11-24 (from 10:36 UTC) only; report the chosen rule unchanged on 2026-11-27 (held out,
  synced through about 11:50 UTC). Sealed days and the validation day 2026-11-26 are not used.
- **Shock:** BTCUSDT futures mid (as received, `recv_ms`) on a 100 ms grid; r = ln(mid_t / mid_{t-1 s});
  shock when |r| > k * sig15_BTC (1 s returns, trailing 900 s, as in the simulator); k in {3, 4}. After a shock,
  no new shock for 30 s (declustering). t0 = the grid time the shock is first visible.
- **Markets:** every alt (and BTC, the control) 5m/15m market open at t0 with more than 60 s left, Polymarket
  spread <= 3c on the Up token at t0, Up mid in 0.05-0.95.
- **Fair value:** `bt/fairvalue.fair_value` (k = 1.2, tau > 60 s branch) with S = last Chainlink price moved by the
  alt Binance return since that Chainlink tick, sig15 of the alt. Two signals:
  - `own`: the alt's Binance mid at the decision time (what the alt feed shows now);
  - `beta` (primary): the alt mid additionally moved by beta * BTC return since t0 - 1 s minus the alt's own return
    since t0 - 1 s, i.e. assumes the alt ends up with beta times BTC's move. beta = OLS slope of alt on BTC 10 s
    returns over the trailing hour (known at t0).
- **Rule:** at t0 buy the side the shock favours (Up if BTC rose) if fair_side - ask_side - fee(ask) > m, with a
  limit price fair_side - fee - m; m in {1c, 3c}. The order reaches Polymarket at t0 + L and is matched against the
  book at t0 + L + 250 ms (taker hold), filling at that ask if it is <= the limit. L = 200 ms (Singapore) and
  50 ms (Amsterdam-like). One order per market per shock.
- **Markouts:** Up/Down mid at fill + 10 s and + 60 s minus price paid minus taker fee; also settlement payoff.
  Sizes: shares at the best ask at the fill time (from `pm_book` + `pm_price_change`), reported, not used.
- **Variants:** k x m = 4 (signal `beta`). The `own` signal and the BTC-control are reported for the chosen (k, m)
  but not chosen on.
- **Primary metric:** executable 60 s mid markout, cents per share, at L = 50 ms, on 11-27, with a 15-minute block
  bootstrap 95% CI (shocks in one block are not independent; alts in one shock even less). Choose (k, m) on
  11-24 by the same metric. Step 2 (simulator strategy) only if 11-27 is positive with a CI not clearly negative.
- **Descriptive:** fair - ask at t0 + 0/150/500/2000 ms; reprice time = first Polymarket update of the alt's Up
  mid in the shock's direction by >= 1c (capped at 10 s); catch-up of the alt Binance mid vs beta * BTC move at
  0/150/500/2000/10000 ms.

## Variants tried
Count: **4** event-study variants (k in {3, 4} x m in {1c, 3c}, signal `beta`, alts) + **2** simulator
configs (the frozen alt rule, and the same rule on BTC's own books as the control). Total 6. The `own` signal and the BTC control were computed for every (k, m) but never chosen on.
| Version | What changed | Why |
|---|---|---|
| event study k=4, m=3c, beta (chosen on 11-24) | best 60 s markout at 50 ms on 11-24 | pre-registered choice rule |
| sim `configs/research/btc-shock-alts.yaml` | frozen rule, alts, held to resolution | step 2 (held-out step 1 passed) |
| sim `configs/research/btc-shock-alts-btc.yaml` | same rule, BTC's own 5m/15m books | pre-registered control |

## Results

### Step 1: event study (no simulator)
`research/btc_shock/events.py DAY OUT` builds the event table (one row per shock x open 5m/15m market),
`research/btc_shock/score.py OUT` scores it. Markouts in cents per share after the taker fee, 15-minute block
bootstrap 95% CI, n = fills. Fill = the favoured token's ask at t0 + L + 250 ms if it still clears the margin.

**Shocks.** 11-24 (10:36-24:00): 659 at k=3 (|r| median 3.1 bp in 1 s), 363 at k=4. 11-27 (01:00-11:49): 550 /
344. That is about 50 / 27 per hour: a "4 sigma" 1 s BTC move is common because 1 s returns are fat-tailed.

**How fast books reprice** (k=3, eligible books: Up spread <= 3c, mid 0.05-0.95). Reprice = first Up-mid update
of >= 1c in the shock's direction.
| Day | Group | rows | fair-ask-fee at t0 (median / p90, c) | at +150 ms | at +500 ms | at +2 s | reprice ms p25/50/75 |
|---|---|---|---|---|---|---|---|
| 11-24 | alts | 2954 | -2.0 / +4.7 | -1.4 / +6.8 | -3.0 / +4.6 | -2.9 / +4.2 | 77 / 151 / 328 |
| 11-24 | BTC (control) | 972 | -0.7 / +5.2 | +1.0 / +9.2 | -0.6 / +8.7 | -3.2 / +2.7 | 160 / 337 / 620 |
| 11-27 | alts | 3192 | -1.7 / +4.7 | -0.4 / +7.0 | -2.5 / +4.8 | -2.2 / +4.7 | 87 / 169 / 326 |
| 11-27 | BTC (control) | 833 | -0.5 / +6.5 | +2.2 / +10.8 | +0.7 / +9.7 | -2.2 / +3.7 | 180 / 366 / 662 |

Median fraction of the (t0 - 1 s -> t0 + 10 s) move already done by t0 + d (11-24; 11-27 is similar):
| d | BTC Binance | alt Binance | alt PM mid | BTC PM mid |
|---|---|---|---|---|
| 0 | 0.40 | 0.30 | 0.16 | 0.05 |
| 150 ms | 0.71 | 0.50 | 0.30 | 0.11 |
| 500 ms | 0.78 | 0.59 | 0.52 | 0.30 |
| 2 s | 0.81 | 0.67 | 0.67 | 0.71 |

Alt Binance does lag BTC (half of its 10 s move by 150 ms, 2/3 by 2 s; 10 s alt move = 0.92 x beta x BTC move,
beta median 0.97). Alt Polymarket books follow their own coin's Binance closely (alt PM is about 150-300 ms behind
alt Binance). The BTC book is the slowest of all relative to its feed: 5% of the move at t0, 30% by 500 ms.

**Executable markout, signal `beta`, alts** (all four variants, both days; chosen on 11-24: k=4, m=3c):
| k | m | L | 11-24 fills | 11-24 60 s | 11-27 fills | 11-27 10 s | 11-27 60 s | 11-27 settlement |
|---|---|---|---|---|---|---|---|---|
| 3 | 1c | 50 | 450 | +2.19 [-0.3, +4.9] | 526 | +0.78 [-0.3, +1.8] | +1.27 [-1.1, +4.0] | +2.00 [-4.1, +8.4] |
| 3 | 1c | 200 | 398 | +1.75 [-0.8, +4.5] | 445 | -0.71 [-1.9, +0.5] | -0.49 [-3.2, +2.5] | -0.33 [-6.9, +6.6] |
| 3 | 3c | 50 | 261 | +2.38 [-1.1, +5.6] | 256 | +0.91 [-0.7, +2.4] | +0.43 [-2.9, +4.2] | -1.38 [-8.4, +6.4] |
| 3 | 3c | 200 | 233 | +2.07 [-1.4, +5.7] | 206 | -0.31 [-2.2, +1.4] | -2.49 [-5.6, +1.0] | -4.83 [-12.4, +3.9] |
| 4 | 1c | 50 | 268 | +4.50 [+0.5, +8.5] | 287 | +3.06 [+1.1, +5.1] | +6.95 [+3.4, +10.4] | +10.03 [+4.0, +16.2] |
| 4 | 1c | 200 | 231 | +4.66 [-0.4, +9.4] | 230 | +1.58 [-0.4, +3.6] | +4.69 [+1.4, +8.1] | +9.01 [+2.8, +15.7] |
| **4** | **3c** | **50** | 170 | **+6.26 [+1.2, +11.2]** | 150 | +3.90 [+1.0, +6.6] | **+9.24 [+5.1, +13.4]** | +9.31 [+2.1, +17.2] |
| **4** | **3c** | **200** | 137 | +7.35 [+1.1, +13.0] | 113 | +2.13 [-0.9, +4.8] | +6.94 [+3.1, +10.7] | +6.03 [-1.3, +13.1] |

Chosen rule on 11-24 at 50 ms: 10 s +2.75 [-0.0, +5.0], settlement +13.4 [+4.5, +22.3]. By coin (11-27, 50 ms,
n / 60 s c): ETH 60 / +15.4, SOL 31 / +1.7, XRP 21 / +4.9, DOGE 17 / +12.9, BNB 13 / +7.6, HYPE 8 / -1.7, ZEC 0
(ZEC books are rarely <= 3c wide). Median shares at the fill ask: 15-17 (the size is small: 10 shares/order).
Signal `own` (alt Binance only) at k=4, m=3c, 50 ms: 11-24 +7.85 [+2.0, +13.1], 11-27 +9.02 [+4.4, +13.4]
(fewer fills): most of the edge does not need the beta step.

**Control: BTC's own books** (k=4, m=3c): 11-24 60 s +7.52 [+2.4, +12.8] (50 ms, n=120), +7.80 (200 ms);
11-27 +15.20 [+9.9, +20.3] (n=115), +16.68 [+11.0, +21.7] (200 ms, n=92); 10 s markouts +8 to +12c with CIs
far from 0, after the 5% taker fee rate. BTC's book lags its feed more than the alt books lag theirs.

**Reading.** The edge is concentrated in the largest shocks (k=3 is about 0 on 11-27, k=4 clearly positive) and
grows from 10 s to 60 s: it is under-reaction of the books to a large BTC move over the following minute, not
only a few hundred ms of staleness. Step 1 passes the pre-registered bar (11-27 at 50 ms: +9.2c, CI [+5.1, +13.4]).

### Step 2: simulator (sim a44b1ed9e51f, 250 ms taker hold, `capital_mode: global`)
Config `analysis/configs/research/btc-shock-alts.yaml` (k=4, m=3c, beta, alts, 10 shares, held to resolution),
frozen from step 1. Cents per share settle = (settlement - price) - fee over all fills, 15-minute block bootstrap
(`research/btc_shock/run_ci.py`); 10 s = mid 10 s after the fill. One train day each, so no pass verdict exists.
| Run | Day | Latency | $/day | Fills | settle c/sh [CI] | 10 s c/sh [CI] | block t |
|---|---|---|---|---|---|---|---|
| `data/runs/latency-arb/btc-shock-alts/2026-11-27T1245-9be2db` | 11-24 (choice) | 200 ms | +236.41 | 228 | +13.0 [+2.0, +24.0] | +2.8 [+0.3, +5.0] | 2.15 |
| (sweep) | 11-24 | 100 / 400 ms | +237.17 / +208.14 | 244 / 194 | | | |
| `data/runs/latency-arb/btc-shock-alts/2026-11-27T1248-2484e4` | 11-24 | 50 ms | +252.10 | 259 | +12.4 [+2.8, +22.4] | +3.7 [+1.3, +5.8] | 2.27 |
| `data/runs/latency-arb/btc-shock-alts/2026-11-27T1254-2b5451` | **11-27 (held out)** | 200 ms | +86.56 | 217 | +4.9 [-4.0, +14.0] | +1.9 [-0.8, +4.6] | 1.05 |
| (sweep) | 11-27 | 100 / 400 ms | +175.13 / +44.79 | 256 / 205 | | | |
| `data/runs/latency-arb/btc-shock-alts/2026-11-27T1255-69fa5a` | **11-27 (held out)** | 50 ms | +162.27 | 275 | **+7.1 [-0.3, +15.1]** | +3.1 [+0.5, +5.6] | 1.78 |

Attribution (11-27, 50 ms): adverse (10 s markout) +$122, inventory (settlement minus the 10 s mid) +$92, spread
-$47, fees -$4. By coin (11-27, 50 ms, fills / settle c): ETH 102 / +12.7, SOL 69 / +4.8, XRP 53 / -3.7,
DOGE 30 / +9.6, BNB 12 / +18.2, HYPE 9 / -2.3. On 11-24 XRP was the best coin (+28c): coin ranks do not repeat.

**BTC control in the simulator** (`configs/research/btc-shock-alts-btc.yaml`, same frozen params, BTC 5m/15m only,
fee rate 0.05):
| Run | Day | Latency | $/day | Fills | settle c/sh [CI] | 10 s c/sh [CI] | block t |
|---|---|---|---|---|---|---|---|
| `data/runs/latency-arb/btc-shock-alts/2026-11-27T1306-574c73` | 11-24 | 200 ms | +123.53 | 136 | +10.7 [+1.4, +20.9] | +10.3 [+7.7, +12.9] | 2.27 |
| (sweep) | 11-24 | 100 / 400 ms | +157.80 / +93.20 | 157 / 107 | | | |
| `data/runs/latency-arb/btc-shock-alts/2026-11-27T1307-5db81c` | 11-24 | 50 ms | +177.52 | 165 | +12.4 [+3.8, +21.4] | +10.2 [+7.7, +12.5] | 2.83 |
| `data/runs/latency-arb/btc-shock-alts/2026-11-27T1310-40106a` | 11-27 | 200 ms | +211.61 | 164 | +14.8 [+6.2, +23.3] | +10.6 [+8.4, +12.7] | 3.19 |
| (sweep) | 11-27 | 100 / 400 ms | +239.28 / +173.05 | 192 / 134 | | | |
| `data/runs/latency-arb/btc-shock-alts/2026-11-27T1310-d9f70b` | 11-27 | 50 ms | +237.71 | 203 | +13.4 [+5.4, +21.2] | +10.1 [+8.2, +11.9] | 3.15 |

## Lessons
- Alt books are not stale relative to their own coin: they follow alt Binance within ~150-300 ms, and alt Binance
  itself lags BTC (half of its move by 150 ms). The extra information in BTC's move (the `beta` step) adds little
  over the alt's own feed; the edge comes from the size of the shock, not from cross-coin staleness.
- Small 3-sigma shocks have no edge after the 250 ms hold (11-27 about 0 or negative at 200 ms). 4-sigma shocks do,
  and the markout keeps growing from 10 s to 60 s and to settlement: books under-react to large BTC moves over the
  next minute. That is closer to short-horizon momentum than to latency arbitrage, and it is why 400 ms still pays.
- The alt simulator result held out is positive but weaker than in the event study (+7.1c/sh at 50 ms, CI touches
  0; +4.9c at 200 ms, CI spans 0). Coin ranks flip between days (XRP best on 11-24, negative on 11-27).
- The control is the stronger finding: BTC's own 5m/15m books lag a BTC shock more than alt books do (5% of the
  10 s move at t0, 30% by 500 ms), and the same rule earns about +10c/sh at 10 s with tight CIs on both days and
  every latency, after the 5% fee. Two days, ~150-200 fills/day at 10 shares: capacity and day-to-day stability
  are untested. Timing is Singapore-recv for both feeds; Binance (Tokyo) reaches Singapore sooner than Polymarket
  (London), which may flatter a Binance-led signal in recorded data.
- Variant count is 6 (4 event-study, 2 sim). The BTC control was pre-registered as a control, not chosen on.

## Next
- Queue both configs unchanged for the next validation day (with `--latency-sweep`); BTC control first.
- Measure the timing caveat: compare the shock's lead over the Polymarket book with London-timed data (mm-location).
- Size: what the visible ask at the limit allows beyond 10 shares, and whether fills are within the first
  150 ms after the shock (competition) or later (under-reaction).

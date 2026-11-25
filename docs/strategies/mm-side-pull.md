# mm-side-pull

**Family:** market-making | **Status:** abandoned
**Current version:** 99cfab239f07 (defaults; other variants differ by params) | **sim_version:** 2127ec99eafd

Code: `analysis/src/polydata/bt/strategies/mm_side_pull.py` (subclass of `mm-binance-pull`), config
`analysis/configs/mm-side-pull.yaml`, tests `analysis/tests/bt/test_mm_side_pull.py`.

## Hypothesis
(Pre-registered before the first run.) The baseline market maker loses to adverse selection, and the
informed fills are mostly ones where Binance had just moved *against the side that got filled*, by less
than the symmetric 2-sigma pull threshold. On the baseline's first-half fills (2026-11-24, windows starting
before 17:15 UTC, run `2026-11-25T0056-0abdaa`), sorted by the signed Binance return over the 2 s before
the fill (+ = in the direction of our fill):

| Signed 2 s Binance return (quintile, bp) | Fills | Spread | Adverse | c/share |
|---|---|---|---|---|
| -8.2 to -1.8 | 290 | +0.28 | -2.63 | |
| -1.8 to -1.1 | 289 | +0.55 | **-3.99** | |
| -1.1 to 0.0 | 290 | +0.45 | -0.86 | |
| 0.0 to +0.5 | 289 | +0.70 | -1.02 | |
| +0.5 to +7.2 | 290 | +0.71 | **+1.08** | |

So: when the coin falls by even 1-2 bp, the Up bid is the stale quote that fast takers hit, and the ask is
the side that earns. Pulling **only the exposed side**, with a much lower threshold than the symmetric
pull, should remove the worst fills while keeping the good ones (a symmetric low-threshold pull would also
throw away the +1 c fills).

**Who loses to us:** the uninformed flow on the side that Binance moved towards; we stop selling options to
the fast takers on the other side.

**Primary metric:** first-half P&L ($), read with per-share adverse selection; the chosen parameter set is
then reported on the second half (>= 17:15 UTC) unchanged with a 15-minute-block bootstrap CI, and at 400 ms.

## Variants tried
Count: 6 parameter sets of this class (plus 10 config variants of the parent in the same research round, see
[mm-binance-pull](mm-binance-pull.md#round-1-results-config-only-whole-day-2026-11-24-replayed-sim_version-2127ec99eafd)).
| Variant | What changed | Why |
|---|---|---|
| s1 (defaults) | `side_k` 0.5, `side_window_ms` 2000, `side_cooldown_ms` 3000 | pre-registered |
| s2 | `side_k` 1.0 | less sensitive |
| s3 | `side_k` 0.25 | more sensitive |
| s4 | `side_window_ms` 5000 | slower moves too |
| s5 | `side_cooldown_ms` 10000 | longer pause |
| s6 | defaults + parent `pull_k` 1.0 | combine with the best Round 1 config |

## Selection (written before reading any second-half number)
Chosen on the first half: **the defaults (`side_k` 0.5, `side_window_ms` 2000, `side_cooldown_ms` 3000), all
coins and timeframes** (version of run `2026-11-25T0203-39761e`). Reasons: the pre-registered parameters,
first-half P&L positive (+$18), and spread + adverse (the 10 s markout) positive per share for the first
time (+0.06 c against -0.88 c for the baseline). `side_k` 0.25 had a slightly better markout (+0.11 c) on
half the fills and less P&L; the combination with `pull_k` 1 had more P&L (+$31) but a worse markout
(-0.47 c), i.e. luckier inventory, so it was not chosen. Comparator carried to the second half: the parent
with `pull_k` 1.0 (best markout among the Round 1 config variants, +0.14 c on 551 fills).

## Results
All runs: 8 coins x 5m/15m/1h, the whole of 2026-11-24 (train) replayed, sim_version 2127ec99eafd, split per
market by `window_start` (first half < 17:15 UTC). Run folders `data/runs/market-making/mm-side-pull/<id>`.
Markout = spread + adverse per share.

| Variant | Run | Half | P&L | 95% CI | Fills | Spread / adverse / inventory, c/share | Markout |
|---|---|---|---|---|---|---|---|
| s1 defaults | `2026-11-25T0203-39761e` | first | +$18.21 | [-88, 129] | 361 | +0.60 / -0.54 / +0.65 | +0.06 |
| s2 `side_k` 1.0 | `2026-11-25T0255-939494` | first | -$39.47 | [-180, 100] | 770 | +0.56 / -1.01 / -0.27 | -0.45 |
| s3 `side_k` 0.25 | `2026-11-25T0254-e0dcb4` | first | +$5.93 | [-91, 105] | 207 | +0.66 / -0.55 / +0.28 | +0.11 |
| s4 `side_window_ms` 5000 | `2026-11-25T0324-fa67dd` | first | -$84.46 | [-209, 37] | 606 | +0.55 / -1.32 / -1.22 | -0.77 |
| s5 `side_cooldown_ms` 10000 | `2026-11-25T0338-64d105` | first | -$15.88 | [-74, 45] | 71 | +0.90 / -1.69 / -2.15 | -0.79 |
| s6 + `pull_k` 1.0 | `2026-11-25T0330-6b49ac` | first | +$30.82 | [-44, 116] | 195 | +0.68 / -1.15 / +2.73 | -0.47 |
| **s1 defaults (chosen)** | `2026-11-25T0203-39761e` | **second** | **-$116.20** | [-240, 6] | 658 | +0.81 / -2.05 / -1.17 | **-1.24** |
| parent baseline, for reference | `mm-binance-pull/2026-11-25T0056-0abdaa` | second | -$93.46 | [-230, 45] | 1,562 | +0.64 / -1.73 / +0.23 | -1.09 |

Latency (s1, whole day): LATENCY_ROWS

On the first half the one-sided pull did what it was built for: adverse selection per share fell from -1.42
to -0.54 c (BTC 5m from -1.2 to +0.3 c) and the markout turned slightly positive. It cost 75% of the fills.
On the second half, unchanged, it lost $116 (block-bootstrap CI [-240, 6]), worse per share than the
baseline. **Verdict: abandoned** (not promising: the second half is negative).

**Post-mortem (diagnostic only, nothing re-tuned).** The same 2 s signed-Binance-return breakdown on the
baseline's second-half fills: 717 of 1,561 fills had *no* Binance mid change at all in the 2 s before them
(the evening was quiet), and they still lost -1.49 c/share to adverse selection; fills after a move in our
favour lost -0.37 c instead of gaining +1.08 c. In the second half the informed flow did not come after
Binance moves that we can see, so a Binance-triggered pull (one- or two-sided) has nothing to act on.

## Lessons
- The first-half mechanism was real in that half (a clean monotone pattern over 1,452 fills) but it is a
  regime, not a constant: in the quiet second half adverse selection came without any visible Binance
  move. One train day cannot separate the two.
- First-half P&L is the wrong selection metric at these volumes: the inventory term (settlement luck) is
  +-$100 per half for every variant, larger than any spread or adverse difference. Select on markout
  (spread + adverse) and fill count, and treat P&L only as a sanity check.
- Every pull variant trades volume for adverse selection roughly one for one: fewer fills, the same edge
  per fill out of sample. Quoting one tick inside the touch at the back of the queue is filled mostly when
  the level is being run over.
- `side_k` is monotone on the first half (1.0 < 0.5 ~ 0.25) and longer windows / cooldowns hurt; nothing here
  is worth further tuning on this day.

## Next
- Find what leads the second-half flow: Binance spot (`bn_spot_bbo`), Coinbase/Chainlink (`rtds_price`,
  `chainlink`), and Polymarket's own trade flow (the last few taker trades' direction in the same market and
  in the sibling timeframes). A pull is only as good as its trigger.
- Score fills by queue position: fills with queue ahead 0 (we were the last at the level) vs fills from
  trades at our price with queue left; if the former carry the adverse selection, quote only where we would
  be early in the queue (join new levels, do not requote into old ones).
- Re-run s1 and the parent with `pull_k` 1.0 on every new train/validation day before believing either way.

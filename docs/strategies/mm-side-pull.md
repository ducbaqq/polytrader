# mm-side-pull

**Family:** market-making | **Status:** testing
**Current version:** see Results | **sim_version:** 2127ec99eafd

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
Count: see the table (each parameter set is one variant).
| Version | What changed | Why |
|---|---|---|

## Selection (written before reading any second-half number)
Chosen on the first half: **the defaults (`side_k` 0.5, `side_window_ms` 2000, `side_cooldown_ms` 3000), all
coins and timeframes** (version of run `2026-11-25T0203-39761e`). Reasons: the pre-registered parameters,
first-half P&L positive (+$18), and spread + adverse (the 10 s markout) positive per share for the first
time (+0.06 c against -0.88 c for the baseline). `side_k` 0.25 had a slightly better markout (+0.11 c) on
half the fills and less P&L; the combination with `pull_k` 1 had more P&L (+$31) but a worse markout
(-0.47 c), i.e. luckier inventory, so it was not chosen. Comparator carried to the second half: the parent
with `pull_k` 1.0 (best markout among the Round 1 config variants, +0.14 c on 551 fills).

| Run | Scope | Half | P&L | Fills (shares) | Spread / adverse / inventory, c/share | Notes |
|---|---|---|---|---|---|---|

## Lessons

## Next

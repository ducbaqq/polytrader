# Strategy journals

One file per strategy (`docs/strategies/<strategy>.md`, named like the strategy's registry name) holds
its hypothesis, the variants tried, results with links to run folders, lessons and status. Run
artifacts themselves are not committed (they live in `data/runs/`, shared through `polydata bt
push` / `pull`); only the journals, this glossary and `docs/runs-index.csv` are.

Research catalogue of strategy ideas: [research/strategy-ideas.md](../research/strategy-ideas.md).

Status: `idea` / `testing` / `candidate` / `paper` / `live` / `abandoned`.

## Where each strategy stands

Train data so far: 2026-11-24 (10:36–24:00 UTC). Chosen on the first half (windows before 17:15 UTC),
reported on the second half unchanged. No validation day yet (first: 2026-11-26).

| Strategy | Family | Tier | Status | Result in one line |
|---|---|---|---|---|
| [mm-binance-pull](mm-binance-pull.md) | market-making | sim | abandoned (baseline kept) | −$201/day; earns +0.6¢/sh spread, loses −1.6¢/sh to adverse selection |
| [mm-side-pull](mm-side-pull.md) | market-making | sim | abandoned | H1 +$18, H2 −$116 [−240, 6]; H2 adverse flow isn't preceded by Binance moves |
| [fair-value-deviation](fair-value-deviation.md) | fair-value | screen | promoted to sim | +6.0 / +5.1¢/sh per half, CIs span 0 |
| [fv-taker](fv-taker.md) | fair-value | sim | abandoned | H1 +9.6¢/sh, H2 −4.9¢/sh [−13, 4]; margin 3–4¢ positive in both halves (post hoc, re-test on validation days) |
| [endgame-maker](endgame-maker.md) | endgame | sim | testing (needs validation days) | V2 (5m, last 120 s): H1 +2.0c/sh [-5.9, 9.1], H2 +4.3c/sh [-3.0, 10.4] (80 mkts), +2.3c at 400 ms; edge is settlement, not spread |
| [underdog-maker](underdog-maker.md) | fair-value (calibration) | sim | abandoned | V1 (5m, 300-120 s left, underdog mid 0.15-0.35): H1 +4.2c/sh [-5.5, 13.8], H2 -1.8c/sh [-11.7, 7.4] (176 mkts, 42 won); about -0.7c at 400 ms; same-data idea, no persistent edge |
| [complete-set-arb](complete-set-arb.md) | complete-set-arb | screen | abandoned | the two asks never sum below 1 in 5 s snapshots (break-even ≈ 0.97–0.99 after fees) |
| [xtf-arb](xtf-arb.md) | cross-timeframe | sim | abandoned | shared settlement holds (0/626 violations); Up_A + Down_B < 1 is real but tiny (H2 +1.9¢/pair [0.5, 3.4], ≈$1–4/half at visible size); in sim the legs fill together in 13/76 entries, P&L is unhedged legs |
| [settlement-lock](settlement-lock.md) | endgame | screen | abandoned | once the average locks the outcome, nothing is offered below 0.99 |
| [binance-momentum](binance-momentum.md) | momentum | screen | abandoned | negative in all four half-variants, CIs span 0; the book has already repriced |
| [martingale-streak-reversal](martingale-streak-reversal.md) | martingale | screen | abandoned | outcomes are close to independent; escalation only reshapes losses |
| [sharp-wallet copy (study)](../research/wallets.md) | copy-trading | study | abandoned | wallets chosen on H1 at the 49th pct of random sets on H2; copy +0.3–0.5¢/sh, CIs span 0 |
| [adverse-flow (study)](../research/adverse-flow.md) | market-making | study | done | 26 pre-fill features, none stable H1→H2; ~2/3 of adverse is inside the 200 ms cancel latency |

## Validation queue

**2026-11-26 (first validation day), run 2026-11-27:** endgame-maker-v2 +$41.35/day (t 0.88, FAIL, positive again: keep testing); fv-taker-m3 −$66.86 (FAIL, abandoned); mm-side-pull +$137 at 200 ms but −$140 / −$138 at 100 / 400 ms (FAIL, luck, abandoned). Details in each journal.


Fixed on 2026-11-25, before any validation data exists. Each is run **unchanged** on every validation day
(first: 2026-11-26, then every third day) from the frozen configs in `analysis/configs/validation/`, with
`--latency-sweep`. Each run is one look and goes into the strategy's journal, pass or fail.

| Config | Why it's queued | Pass on a validation day means |
|---|---|---|
| `endgame-maker-v2.yaml` | positive in both train halves (+2.0 / +4.3 ¢/sh), CIs span 0 | the run's verdict passes; at minimum ¢/sh > 0 at 200 and 400 ms |
| `fv-taker-m3.yaml` | margin 3¢ positive in both halves, but chosen after seeing both (post hoc) | as above |
| `mm-side-pull.yaml` | best per-share markout on H1; checks whether H2's quiet-evening failure repeats | as above |

```bash
cd analysis && uv run polydata update --days 2026-11-26
for c in endgame-maker-v2 fv-taker-m3 mm-side-pull; do
  uv run polydata bt run configs/validation/$c.yaml --days 2026-11-26 --latency-sweep
done
```

## Family glossary

A strategy's `family` (a class attribute) names the run folder: `data/runs/<family>/<strategy>/...`.

| Family | What it is |
|---|---|
| `market-making` | Rests bids and asks inside the book and earns the spread; pulls quotes when informed flow (Binance moves) arrives. |
| `latency-arb` | Takes stale quotes right after a Binance or Chainlink move, before the book reprices. |
| `fair-value` | Trades the gap between the market price and a model price (Binance, Chainlink, time left, volatility). |
| `endgame` | Trades the last seconds of a window, when the outcome is nearly decided. |
| `complete-set-arb` | Buys or sells Up + Down together when the pair costs less or more than $1. |
| `cross-timeframe` | Trades inconsistencies between the 5m, 15m and 1h markets of one coin. |
| `momentum` | Bets that a recent move in price or probability continues. |
| `mean-reversion` | Bets that a recent move reverses. |
| `order-flow` | Trades on public trade flow and book imbalance. |
| `copy-trading` | Follows the trades of selected wallets. |
| `martingale` | Sizes up after losses (stake escalation schemes). |
| `sanity` | Trivial strategies that test the simulator (noop, buy-and-hold-up, always-cross, join-best); no edge expected. |

## Finding runs

`data/runs/index.parquet` has one row per run (derived; rebuilt from the run folders). A mirror of
its summary columns is committed as `docs/runs-index.csv`, so runs can be found from git alone.

```bash
cd analysis
uv run polydata bt runs                          # all runs
uv run polydata bt runs --strategy mm-binance-pull --passed
uv run polydata bt runs --family market-making --tag sweep:1124-ab12
uv run polydata bt runs --search "btc 5m"        # text over id, strategy, tags, coins, days, verdict
uv run polydata bt pull                          # fetch runs made on other machines, rebuild the index
uv run polydata sql "SELECT * FROM read_parquet('../data/runs/index.parquet') ORDER BY usd_per_day DESC LIMIT 20"
```

A run folder holds `config.yaml`, `provenance.json` (git commit, simulator and strategy versions,
data fingerprint), `results.json`, `orders.parquet`, `fills.parquet`, `equity.parquet`,
`markets.parquet` and `log.txt`. The same config, strategy version, simulator version and data give
the same `results.json` bytes.

## Journal template

```markdown
# <strategy-name>

**Family:** <family> | **Status:** idea | testing | candidate | paper | live | abandoned
**Current version:** <strategy_version from the latest run's provenance.json> | **sim_version:** <from the same file>

## Hypothesis
What edge, why it should exist, and who loses money to us.

## Variants tried
Count of strategy versions and parameter sets tried so far (the trial budget): <n>.
| Version | What changed | Why |
|---|---|---|

## Results
| Run | Days | Kind | $/day | Verdict | Notes |
|---|---|---|---|---|---|
Link run folders as `data/runs/<family>/<strategy>/<run_id>`. One line per run that matters, failures included.
Never list sealed-day results except from a `--final` run (give N, the number of final looks, and the verdict).

## Lessons
What the runs taught us, including what did not work.

## Next
```

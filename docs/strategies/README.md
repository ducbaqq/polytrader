# Strategy journals

One file per strategy (`docs/strategies/<strategy>.md`, named like the strategy's registry name) holds
its hypothesis, the variants tried, results with links to run folders, lessons and status. Run
artifacts themselves are not committed (they live in `data/runs/`, shared through `polydata bt
push` / `pull`); only the journals, this glossary and `docs/runs-index.csv` are.

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
| [complete-set-arb](complete-set-arb.md) | complete-set-arb | screen | abandoned | the two asks never sum below 1 in 5 s snapshots (break-even ≈ 0.97–0.99 after fees) |
| [settlement-lock](settlement-lock.md) | endgame | screen | abandoned | once the average locks the outcome, nothing is offered below 0.99 |
| [binance-momentum](binance-momentum.md) | momentum | screen | abandoned | negative in all four half-variants, CIs span 0; the book has already repriced |
| [martingale-streak-reversal](martingale-streak-reversal.md) | martingale | screen | abandoned | outcomes are close to independent; escalation only reshapes losses |

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

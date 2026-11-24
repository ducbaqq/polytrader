# Strategy journals

One file per strategy (`docs/strategies/<strategy>.md`, named like the strategy's registry name) holds
its hypothesis, the variants tried, results with links to run folders, lessons and status. Run
artifacts themselves are not committed (they live in `data/runs/`, shared through `polydata bt
push` / `pull`); only the journals, this glossary and `docs/runs-index.csv` are.

Status: `idea` / `testing` / `candidate` / `paper` / `live` / `abandoned`.

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
**Current version:** <strategy_version from the latest run's provenance.json>

## Hypothesis
What edge, why it should exist, and who loses money to us.

## Variants tried
Count of strategy versions and parameter sets tried so far (the trial budget): <n>.
| Version | What changed | Why |
|---|---|---|

## Results
| Run | Days | Kind | $/day | Verdict | Notes |
|---|---|---|---|---|---|
Link run folders as `data/runs/<family>/<strategy>/<run_id>`.

## Lessons
What the runs taught us, including what did not work.

## Next
```

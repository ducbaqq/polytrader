# Backtester design (polydata.bt)

Status: draft for approval · 2026-11-24 (builder check done)

## Intent

Tell us honestly what a strategy would have earned on Polymarket's crypto Up/Down markets, starting
with a **market maker on BTC/ETH 5m/15m**. "Honestly" means:
- fills at the back of the queue
- our measured delays
- cancels that race takers chasing Binance moves
- exact fees, settlement at resolution
- a $5,000 capital limit

The results decide which strategies go to paper trading and then to real money (the user's $5k).

Why market making first: the calibration study (`docs/research/calibration-study.md`) found:
- Taking liquidity has no edge.
- Liquidity providers earn +0.47¢ per share on average.
- Their losses are concentrated in trades that follow Binance moves.

Whether *we* would earn that, as a new, slower, back-of-queue maker, can only be answered by
simulating our own orders in the real book.

Many strategies will be tested over months and across many sessions, including future Claude
sessions with no memory of earlier ones. Every run must therefore be **findable, inspectable and
reproducible**.

## Goals

1. **Event replay.** Recorded Polymarket, Binance and Chainlink events reach the strategy in the order
   we received them (`recv_ms`). The strategy never sees anything it couldn't have known.
2. **Exchange simulator** per market:
   - one combined Up/Down book, rebuilt from the recording
   - our resting orders with queue position
   - order, cancel and fill-report delays
   - taker fills against the book
   - verified fees (`polydata.fees`), settlement at resolution, capital limits
3. **Strategy interface.** Strategies are Python classes; the same class must later run unchanged in a
   live/paper runner.
4. **Baseline strategy** `mm-binance-pull` (see below), plus trivial sanity strategies.
5. **Reports:** a one-page verdict report per run and a runs index to compare runs.
6. **Experiment record:**
   - a run folder per run (named by family / strategy / time)
   - per-strategy journals, a family glossary
   - sync of run artifacts to Spaces
   - a sealed test period
7. **Simulator validation** before anyone trusts a result.

## Non-goals (this project)

- The live/paper trading runner. It's the next project; the interface must not block it.
- Placing real orders, or any order routing.
- Strategy optimisation beyond simple parameter sweeps.
- A GUI beyond the static HTML report.
- Perfect speed. The target is below.

## Anti-goals (failure even if it "works")

- **Lookahead:** any use of data the strategy couldn't have had at that moment.
- **Optimistic fills:** front-of-queue fills, ignoring delays, fills that the recorded trades don't
  support.
- **Results that can't be reproduced** from the run folder (code version, config, data range).
- **Tuning on the sealed test period**, or quietly re-running a strategy on it until it passes.

## Constraints

- Python 3.12, inside the existing `analysis/` uv package as `polydata.bt`.
- Data comes from the `polydata` Parquet store and views (`analysis/README.md`, `docs/RECORDER.md`).
- The repo is **public**: no secrets, IPs or hostnames in committed files.
- Fees come only from `polydata.fees.taker_fee` (verified on-chain; `docs/FEES.md`). Makers pay 0.
  Rebates are 0%.
- Default capital: **$5,000**.
- Must run on a laptop. Target: **≤ 10 minutes per day of data** for BTC+ETH 5m/15m with one strategy.

## Approaches considered

1. **Our own engine in Python (chosen by the user).** Replay from `polydata`, with our own book,
   queue and matching simulator built for Polymarket's mechanics. Full control over what decides a
   market maker's profit: queue position, latency, the combined Up/Down book. It fits DuckDB and
   notebooks. Cost: we build and validate the simulator, and pure Python is slower; events are
   bundled into 10 ms steps, and the core can be accelerated later.
2. **NautilusTrader.** Fast, mature, with a Polymarket live connector. But it treats Up and Down as
   separate instruments, while Polymarket matches them as one book, so fills would be wrong where it
   matters most. It would need custom fill models, data conversion and a large framework to learn.
3. **hftbacktest.** The best queue and latency models off the shelf. But strategies must be written
   in a restricted compiled-Python style, and there's no Polymarket live trading, so strategies would
   be written twice. The user ruled that out: backtest and live must share code.
4. **Vectorized (pandas/DuckDB).** Fast, but it can't model resting quotes, queues or cancels, so it's
   unusable for market making.

## Decided

### How Polymarket's book works (verified on our data, 2026-11-24)

- **Level changes are published for both tokens, mirrored.** Up BUY at p with size s ⇔ Down SELL at
  1−p with size s, in the same message. Each market therefore has **one combined book**; the
  simulator keeps it in Up terms (bids and asks on Up).
- **Each trade is published once**, on the token the taker traded. A trade on Down at price q is,
  in Up terms, the opposite side at 1−q. The queue at a level is eaten by trades on either token.
- Our own order "buy Down at q" is placed in the combined book as "sell Up at 1−q". The same
  applies to cash and inventory accounting (below).
- Tick size can change mid-market (0.01 → 0.001 near the extremes); `pm_other` / the
  `pm_tick_size_change` view. The minimum order size is 5 shares (`markets.min_order_size`).

### Time and delays

Three clocks, all in milliseconds:

| Clock | Meaning | Source |
|---|---|---|
| Strategy time | When the strategy would have seen an event | `recv_ms` (Singapore recorder) |
| Exchange time | When the exchange's book changed or a trade happened | Polymarket `ts_ms` |
| Order arrival | When our order or cancel reaches the exchange | strategy time + `order_latency_ms` |

- Defaults: `order_latency_ms = 200` (the same for cancels), plus `fill_report_latency_ms = 126`
  before the strategy learns of a fill. Both are configurable, with optional jitter.
- Defaults come from the measured feed delays (Polymarket 126 ms median, Binance 38 ms) and the
  ~200 ms round trip to Polymarket.
- **The simulator matches our orders against the book on exchange time.** The strategy only sees
  events at their `recv_ms`. This is the method used for the latency test in the calibration study.

### Queue and fill model (conservative by default)

**Joining.** When our limit order arrives at price p, the shares ahead of us = the level's size in the
exchange book at arrival time. We join at the back.

**Ahead of us shrinks:**
- **Trades** at our price (on either token, mapped to Up terms) reduce "ahead" first. Once "ahead"
  is zero, further trade volume at our price fills us, partially or fully.
- **Size drops not explained by trades** are cancels by others. The default `cancel_model =
  "behind"` assumes they were behind us, so "ahead" doesn't change: pessimistic. An option
  `"proportional"` removes them pro rata. Reports state which model was used.

**Trade-through.** A trade at a price worse than ours, for the side that rests at our price, fills us
fully. The book can't trade through us without filling us.

**No self-impact.** Our orders don't change the recorded book or other traders' behaviour. Reports
show our order sizes against the level sizes, so oversized orders are visible.

**Taker orders** (IOC):
- Walk the recorded book at arrival time, limited to the visible depth.
- Fee = `taker_fee(total shares, average price, rate)`: one fee per order on its average price, as
  verified.
- Our taking doesn't remove liquidity from the recorded book; the impact is ignored and flagged in
  the report when large.

**Validation rules:** orders off-tick, below the minimum size, or outside the allowed price range are
rejected with a reason, as the real exchange would.

**Market end:** no new orders after the window ends. Resting orders are cancelled at the end.

### Capital and positions

- Cash starts at $5,000 (configurable).
- **Buying Up at p locks p × size; buying Down at q locks q × size.**
- Selling shares we hold locks those shares. Selling shares we don't hold isn't allowed; the
  equivalent is buying the other token.
- **Holding equal Up and Down in a market is merged back into $1 each** (Polymarket allows merging
  for free; assume instantly).
- **At resolution** winning shares pay $1, after a configurable `redeem_delay_s`. Default 60 s after
  the outcome was recorded (`pm_resolution` `recv_ms`). Cash is locked until then.
- Orders that would exceed free cash are rejected, so a backtest never trades money we wouldn't
  have had.

### Strategy interface

A strategy is a Python class with plain handlers. The same class must later run in the live runner.

```python
class Strategy:
    name = "mm-binance-pull"          # strategy name (kebab-case)
    family = "market-making"          # from docs/strategies/README.md
    params: dict                      # from the run config

    def on_start(self, ctx): ...
    def on_market_open(self, ctx, market): ...        # a market enters the tradable set
    def on_book(self, ctx, market, book): ...          # top-of-book or levels changed (strategy's view)
    def on_trade(self, ctx, market, trade): ...        # a public trade (Up terms + original token)
    def on_binance(self, ctx, sym, bbo): ...           # Binance futures top of book (10 ms steps)
    def on_chainlink(self, ctx, coin, price): ...
    def on_order(self, ctx, order): ...                # accepted / rejected / cancelled / filled (after delay)
    def on_timer(self, ctx, name): ...
    def on_market_close(self, ctx, market): ...        # window ended
```

`ctx` provides:
- `now`
- `place(market, token, side, price, size, post_only=True)` → order id
- `take(market, token, side, size, limit_price)`
- `cancel(order_id)`, `cancel_all(market=None)`
- `book(market)`: the strategy's delayed view
- `position(market)`, `cash`, `orders(market)`
- `set_timer(name, ms)`
- `markets()`: open markets with `price_to_beat`, window, fee rate, tick and min size
- `fair_value(market)`: reuses the study's model
- `log(...)`

### Baseline strategy `mm-binance-pull` (family `market-making`)

Quote bid and ask around the market mid (or the fair value, if configured) on each selected market:
- half-spread and size from params
- only when the book's spread ≤ `max_book_spread` and the time left is within `[t_min, t_max]`
- **pull all quotes when Binance moved more than `k × σ` in the last `pull_window_ms`**, re-quote
  after `cooldown_ms`
- skew quotes against inventory; cap inventory per market
- stop quoting `stop_before_end_s` before the end

The parameters live in the config, so sweeps can vary them.

Sanity strategies (also the simulator's tests):
- `noop` (P&L exactly 0)
- `buy-and-hold-up` (P&L = outcome − price − fee per market)
- `always-cross` (pays spread + fee every time)

### Runs, naming and records

**Run folder:** `data/runs/<family>/<strategy>/<YYYY-MM-DDTHHMM>-<shortid>/`

| File | Contents |
|---|---|
| `config.yaml` | strategy, params, markets, date range, delays, fill/cancel models, capital |
| `provenance.json` | git commit + diff if uncommitted, fill-model version, data fingerprint (raw-file manifest hash for the range), Python/package versions, host |
| `results.json` | every metric and the verdict |
| `report.html` | self-contained one-page report |
| `orders.parquet` | every order event: placed, accepted, rejected, cancelled, filled (with queue-ahead at fill) |
| `equity.parquet` | cash, positions and marked P&L over time |
| `log.txt` | the strategy's log |

**Runs index:** `data/runs/index.parquet`, one row per run with key metrics, verdict and tags.
- `polydata bt runs [--strategy|--family|--tag|--search|--passed]` lists and filters runs.
- A committed `docs/runs-index.csv` mirrors the summary columns, so runs are findable from git alone.

**Journals:**
- `docs/strategies/README.md`: the family glossary (market-making, latency-arb, fair-value, endgame,
  complete-set-arb, cross-timeframe, momentum, mean-reversion, order-flow, copy-trading,
  martingale) with one-line definitions.
- `docs/strategies/<strategy>.md`: hypothesis, variants tried (count), results with links to run
  folders, lessons, status (idea / testing / candidate / paper / live / abandoned).
- Seed it with `martingale-streak-reversal`: rejected by simulation in our first conversation.

**Sharing:** `polydata bt push` / `pull` sync `data/runs/` with `spaces:<bucket>/runs/`, using the
same credentials and code as `polydata sync`.

### Data splits and the sealed test period

Every recorded day is permanently one of three kinds. The assignment is deterministic by date, so it
never changes and every session agrees on it:

| Kind | Days | Used for |
|---|---|---|
| **Sealed** | every 3rd calendar day (day number since 2026-11-23 divisible by 3) | `--final` runs only. Sweeps and ordinary runs never load them; the replay refuses. |
| **Validation** | the most recent 30% of the remaining days | ordinary runs' **provisional verdict**; sweeps rank on it |
| **Train** | the rest | tuning, exploration |

- **Why interleave sealed days instead of waiting for future data:** a final verdict doesn't need
  new days to be recorded first, and the sealed days cover all market conditions seen so far.
- **A strategy version** = hash of (strategy source file + parameter set). Picking new parameters
  from a sweep is a new version.
- **`--final` evaluates a version on all sealed days** (at least `final_min_days`, default 5;
  refused before then) **once per (version, simulator version, data fingerprint)**:
  - A simulator fix or re-synced data allows another final run.
  - The report then lists all earlier final runs of that version.
  - Changing the strategy creates a new version.
- **The journal and report show how many versions were tried** before a final run. Each final run on
  the sealed days counts as one look.
- **`--final` is refused until the simulator validation suite passes** (see below), so a simulator
  bug can't burn the sealed data.

### Report (one page, verdict first)

Header with the verdict, then:
- equity curve, train vs test
- daily P&L, drawdown
- P&L attribution: spread earned, losses to informed takers (markout after fills), inventory, fees
- by market
- realism: fill rate, share of fills within 1 s after a Binance move, average queue-ahead,
  cancel-model used
- latency sensitivity (100 / 200 / 400 ms)
- robustness grid (neighbouring parameter values, when a sweep exists)

**Pass criteria** (defaults, configurable per strategy, recorded in the run). They apply to the
validation days in ordinary runs (provisional verdict) and to the sealed days in `--final` runs (the
verdict):
- **Profit > 0, and significant.** The significance unit is P&L per 15-minute block, with a block
  bootstrap as in the calibration study: the 95% lower bound must be > 0, and t > 3 on block P&L.
  Daily P&L is too few points with only a handful of days.
- ≥ 70% of days positive, **checked only when there are ≥ 7 days**; otherwise shown, not judged.
- maximum drawdown < 10% of capital
- profitable at 400 ms order latency
- ≥ 200 fills per day

### Simulator validation (must pass before results are trusted)

1. **Book reconstruction:** the combined book's best bid/ask matches `pm_best_bid_ask` in ≥ 99.5% of
   sampled moments, per market.
2. **Sanity strategies:**
   - `noop` = 0
   - `buy-and-hold-up` matches a direct DuckDB computation to the cent
   - `always-cross` loses exactly spread + fee
3. **Fill realism:**
   - A strategy that joins the best bid/ask with small size and never cancels should earn less per
     share than the average maker (+0.47¢ in the study), because it's back-of-queue and slow.
   - Fills must only happen when recorded trades or trade-throughs support them; checked by
     re-deriving every fill from the trades.
4. **Determinism:** the same config and data give byte-identical `results.json`.

`polydata bt validate` runs 1–4 and records the result with the simulator version. `--final` is
refused until it passes for the current simulator version.

## My calls (the user can override)

- The defaults above: delays, `cancel_model = "behind"`, pass criteria, `redeem_delay_s = 60`, the
  10 ms Binance step.
- **Sealed days:** every 3rd calendar day, permanently. Validation is the latest 30% of the other days.
  Versions = source + params. Final runs: once per version, simulator version and data fingerprint,
  and only after the validation suite passes.
- **Significance on 15-minute block P&L** (bootstrap), not daily P&L, which has too few points.
- **Binance events are bundled into 10 ms steps** (last top of book per step) to keep replay fast. A
  10 ms difference is far below our 126–200 ms delays.
- **Merging Up+Down pairs is assumed free and instant**; it's a gasless operation via Polymarket's
  relayer, to be confirmed in the live project.
- **Run artifacts are not committed.** Only journals, the glossary and the index CSV are.

## Proposed additions (review 2026-11-24, not yet approved)

From a review of this draft. Each item is a proposal; where it conflicts with a section above, the
section above stands until the user approves the change.

### Fixes to the design above

- **Enforce the sealed period in the data layer, not only in the replay.** The DuckDB views,
  notebooks and `polydata.research.study` reruns would otherwise read sealed days. The views exclude
  sealed days by default; only `--final` runs open them.
- **Start the day count at the first clean day.** 2026-11-23 is day 0, so it's sealed, and it's the
  development day (restarts, gaps; see the data quality log). Proposed epoch: 2026-11-25. Days
  before it are train only.
- **Make the validation split truly permanent.** "The most recent 30% of the remaining days" moves
  as data grows, so validation days turn into train days. Proposed: a fixed pattern by day number,
  like the sealed days (e.g. day number mod 3 = 1 is validation, mod 3 = 2 is train).
- **Data gaps.** A dropped Polymarket connection leaves a stale book, which gives fills that could
  never have happened.
  - Build a machine-readable gap table from `rec_events`, `rec_stats` and the data quality log.
  - The simulator cancels our orders and voids a market window that overlaps a gap; the report
    counts the voided windows.
  - Until 2026-11-24 10:36, `recv_ms` is up to 4 s late at every 5-minute boundary, i.e. at the
    open and close of 5m markets. Exclude that period from runs.
- **Minimal self-impact.** Median ask depth on BTC 5m is 121 shares, so our orders are a meaningful
  part of a level. Liquidity we take stays removed until that level's next recorded update.
- **Capital per market.** A single global cash limit forces one serial replay of all markets.
  Proposed default: a fixed budget per market (summing to at most the $5,000), so markets are
  independent, run in parallel and can be cached. The global limit stays as an option.
- **Event order.** Events from different streams with the same `recv_ms` have no defined order.
  Define a tie-break (stream priority, then row number), or append a `recv_seq` column in the
  recorder. Needed for the determinism check.

### Additions

1. **Fast screening tier (`polydata.screen`).** At 10 minutes per day of data, a 50-point sweep over
   30 days takes about 250 hours. Most families (endgame, momentum, wallet-following,
   cross-timeframe) need no queue model.
   - Screen them in DuckDB in seconds: taker fills at the ask after the order delay, with the fee.
   - Only survivors go to the event simulator.
   - Screens are indexed and counted like runs, and obey the same day splits.
2. **Trial budget.** With hundreds of strategies, some pass t > 3 on the sealed days by luck.
   - Count every final look across all strategies in the runs index.
   - Raise the bar as the count N grows (e.g. the t threshold for a Bonferroni-corrected 5% level
     over N looks). The report shows N and the threshold used.
3. **Recency test.** The latency trade's edge decayed within 20 hours; interleaved sealed days can't
   detect decay.
   - The report plots edge per day.
   - Pass criterion: profit > 0 on the most recent third of the evaluated days as well.
4. **Ground truth for fills.** Every check in the validation suite is internal consistency, and
   paper trading can't validate queue position either.
   - At the live stage, place minimum-size real orders (5 shares) and compare each fill, and its
     timing, with the simulator's prediction for the same order.
   - `orders.parquet` uses the same schema in the simulator and live, so the comparison is a join.
   - Until this is done, reports label the fill model as unverified.
5. **Dollars per day.** All makers together earned about $84k in 20 hours, and a back-of-queue
   newcomer gets a small slice of that.
   - The report shows expected $ per day and return on capital, against running costs.
   - Pass criterion: $ per day above a configured minimum.
6. **Wider baseline.** BTC/ETH 5m is a speed contest. In the study, makers earned 1–2.6¢ per share
   on SOL, DOGE, BNB and HYPE, on small volume, which suits $5k. Run `mm-binance-pull` on all coins
   and timeframes, with the `max_book_spread` filter (wide books are a trap).

### Data and servers (recorder work, outside `polydata.bt`)

- **Locate the matching engine.** Run short probes in a few regions and compare the minimum
  `recv_ms − ts_ms` on the Polymarket feed; no orders needed. Unverified assumption: Binance
  matches in Tokyo, Polymarket's order book in London.
- **Second recorder** in the region we'd trade from: at least the Polymarket streams and Binance
  top of book. One box in Singapore is a single point of failure, gaps are permanent, and every
  backtest is tied to Singapore's `recv_ms`.
- **More feeds that can't be fetched later** (order by value):
  - Coinbase and one more perp venue: top of book and trades
  - Deribit implied volatility, for the fair-value model
  - Kalshi's short-dated crypto markets, for a cross-venue family
  - On-chain fills (maker and taker wallets) can be fetched from the chain later; not urgent.

### Open risks

- Everything so far rests on about 20 hours of data in one down-trend.
- Whether the user can trade on Polymarket from their location decides if any result can be used.
  Settle it before the live project.

## Left to the builder

- Internal data structures, chunking of the replay (e.g. per hour), and any acceleration needed to
  hit the speed target.
- The HTML report's styling and charting library.
- Exact CLI flag names beyond those mentioned.
- How sweeps parallelise.

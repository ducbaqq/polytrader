# Backtester (`polydata.bt`) and screening tier (`polydata.screen`)

An event-replay simulator for Polymarket's crypto Up/Down markets. It replays recorded Polymarket,
Binance and Chainlink events to a strategy in the order we received them, simulates our orders against
the recorded book (queue position, delays, fees, capital) and writes a run folder with a one-page
report and a verdict. Design and reasoning: [the spec](superpowers/specs/2026-11-24-backtester-design.md).
Strategy journals and the family glossary: [strategies/README.md](strategies/README.md).

Everything below is run from `analysis/` (`cd analysis`).

## Quick start

```bash
uv sync
uv run polydata update                         # sync + build Parquet (incremental)
uv run polydata bt validate                    # simulator validation suite; records the result per sim_version
uv run polydata bt run configs/mm-binance-pull.yaml
open ../data/runs/market-making/mm-binance-pull/<run_id>/report.html    # the path is printed at the end of the run
uv run polydata bt runs                        # all runs; filter: --strategy --family --tag --search --passed
```

| Command | What |
|---|---|
| `bt run CONFIG.yaml [--days A B..C] [--coins ...] [--set k=v ...] [--latency-sweep] [--final]` | Replay one strategy. `--days` takes ISO dates and `A..B` ranges; a sealed day (or `day_kinds` with `sealed`) without `--final` is an error ("sealed days need --final"). `--set params.x=1` sets a strategy param; `x=1` works only if `x` is a `RunConfig` field or one of the strategy's `default_params` (anything else is an error, so a typo cannot become a silent param). `--latency-sweep` also runs at 100/200/400 ms. |
| `bt sweep CONFIG.yaml --grid PARAM=V1,V2 [--grid ...] [--jobs N]` | One run per grid point, all tagged `sweep:<id>`. Ranked by $/day on the evaluation (validation) days when the runs have them, else on all days. |
| `bt validate [--days ...]` | Validation suite (see below). Exit code 1 on failure. |
| `bt runs [filters]` | List runs from `data/runs/index.parquet`. |
| `bt screen NAME [--latency-ms 300\|1000] [--exit hold\|bid] [--size N] [--coins ...] [--timeframes ...]` | Run a screen. |
| `bt push` / `bt pull` | Sync `data/runs/` with the bucket (same credentials as `polydata sync`) and rebuild the index. |

Run folders live in `data/runs/<family>/<strategy>/<YYYY-MM-DDTHHMM>-<shortid>/` (gitignored):

| File | Contents |
|---|---|
| `config.yaml` | The resolved `RunConfig` |
| `provenance.json` | Git commit (+ diff if the tree was dirty), `sim_version`, strategy version, data fingerprint, package versions, hashed host |
| `results.json` | Every metric and the verdict (byte-identical for the same config, versions and data) |
| `report.html` | Self-contained one-page report |
| `orders.parquet`, `fills.parquet`, `equity.parquet`, `markets.parquet` | Order events, fills, equity over time, per-market results |
| `log.txt` | The strategy's `ctx.log` output |

`docs/runs-index.csv` is the committed mirror of the index's summary columns. Run artifacts are not
committed.

## How to write a strategy

A strategy is a class in `analysis/src/polydata/bt/strategies/`. The base class and `Ctx` protocol are
in `bt/strategy.py`. Override only the handlers you need; handlers you do not override are not called
(the engine skips whole streams that nobody listens to).

| Handler | Called when |
|---|---|
| `on_start(ctx)` | Once per coin replay |
| `on_market_open(ctx, market)` | A market becomes tradable (after its first in-range book snapshot) |
| `on_book(ctx, market, book)` | The top of the book changed (the strategy's delayed view) |
| `on_trade(ctx, market, trade)` | A public trade (`types.Trade`: token, side, price, size, plus `up_side`/`up_px` in Up terms) |
| `on_binance(ctx, sym, bbo)` | Binance futures top of book, bundled into `bn_step_ms` (10 ms) steps |
| `on_chainlink(ctx, coin, price)` | A Chainlink tick |
| `on_order(ctx, order)` | An order event (accepted, rejected, fill, cancelled, expired), `fill_report_latency_ms` after it happened |
| `on_timer(ctx, name)` | A timer set with `ctx.set_timer` |
| `on_market_close(ctx, market)` | The window ended |

`market` is a `MarketInfo`: `slug, coin, timeframe, window_start_ms, window_end_ms, price_to_beat,
fee_rate, tick_px, min_order_size, ...`. Prices at the strategy boundary are floats in token terms
(0.48); sizes are shares.

`ctx` API:

| Call | Meaning |
|---|---|
| `ctx.now` | Strategy time, ms |
| `place(slug, token, side, price, size, post_only=True) -> id` | Limit order. `token` is `"Up"`/`"Down"`, `side` is `"BUY"`/`"SELL"` |
| `take(slug, token, side, size, limit_price) -> id` | IOC taker order: walks the book up to the limit |
| `cancel(id)`, `cancel_all(slug=None)` | Cancels (also delayed by `order_latency_ms`) |
| `book(slug, depth=5) -> BookView`, `top(slug)` | The strategy's view, in Up terms; `BookView.for_token("Down")` mirrors it |
| `position(slug) -> (up, down)`, `cash(slug=None)`, `orders(slug=None)` | Reported state; never ahead of what the strategy could know |
| `markets()` | Open markets |
| `fair_value(slug)` | The calibration study's model (None while `price_to_beat` is unknown) |
| `binance(sym)`, `chainlink(coin)` | Latest received tick |
| `set_timer(name, ms)`, `log(...)` | |

Rules for strategies: use only `ctx` and the handler arguments (no DuckDB, files or clocks), so the same
class can later run unchanged in the live runner. Orders must be on tick, at least `min_order_size`, and
in range, or they are rejected with a reason. You cannot sell shares you do not hold; buy the other token
instead.

Minimal example (`bt/strategies/join_one.py`):

```python
from ..strategy import Ctx, OrderView, Strategy, register
from ..types import MarketInfo


@register
class JoinOne(Strategy):
    name = "join-one"                 # kebab-case registry name
    family = "market-making"          # from docs/strategies/README.md
    default_params = {"size": 10.0}

    def on_book(self, ctx: Ctx, market: MarketInfo, book) -> None:
        if ctx.orders(market.slug) or book.best_bid is None:
            return                                    # one resting bid at a time
        ctx.place(market.slug, "Up", "BUY", book.best_bid, self.params["size"])

    def on_order(self, ctx: Ctx, order: OrderView) -> None:
        if order.event == "fill":
            ctx.log("filled", order.slug, order.last_fill.size, order.last_fill.price)

    def on_market_close(self, ctx: Ctx, market: MarketInfo) -> None:
        ctx.cancel_all(market.slug)
```

Steps to add a strategy:

1. Write the class with `@register` and import the module in `bt/strategies/__init__.py` (that is what
   `get_strategy` imports).
2. Add `analysis/configs/<name>.yaml` (see the config reference). Params you do not list take the class's
   `default_params`.
3. Add a unit test next to `tests/bt/test_mm_binance_pull.py`.
4. Add `docs/strategies/<name>.md` from the template in [strategies/README.md](strategies/README.md) and
   keep it current: every run that matters gets a line in its Results table.
5. `uv run polydata bt run configs/<name>.yaml --days <a train day>` to smoke-test, then run on train and
   validation days.

The strategy version is a hash of the strategy's source file, its registry name and the merged params. Picking
new parameters is a new version.

## Config reference

`RunConfig` (`bt/config.py`). Unknown keys are an error.

| Field | Default | Meaning |
|---|---|---|
| `strategy` | required | Registry name |
| `params` | `{}` | Strategy params, merged over the class `default_params` |
| `coins` | all 8 (btc eth sol xrp bnb doge hype zec) | |
| `timeframes` | `[5m, 15m, 1h]` | |
| `days` | `null` | Explicit ISO dates; `null` means every available day of `day_kinds` |
| `day_kinds` | `[train, validation]` | `train` / `validation` / `sealed`. Sealed needs `--final` |
| `order_latency_ms` | 200 | Order and cancel arrival delay |
| `latency_jitter_ms` | 0 | Adds U(0, jitter), seeded |
| `fill_report_latency_ms` | 126 | Exchange event to the strategy's `on_order` |
| `cancel_model` | `behind` | `behind` (others' cancels are behind us, pessimistic) or `proportional` |
| `impact_reset_ms` | 5000 | How long liquidity we took stays removed from a level |
| `capital` | 5000.0 | Total, split into equal fixed per-coin budgets |
| `capital_mode` | `per_coin` | `global` replays all coins in one pass with one shared balance |
| `redeem_delay_s` | 60 | Delay from the recorded resolution to the payout |
| `max_gap_ms_pm` | 5000 | A longer Polymarket gap voids the window |
| `max_gap_ms_bn` | 1000 | A longer Binance top-of-book gap voids the window |
| `bn_step_ms` | 10 | Binance bundling step (must divide 1 s) |
| `seed` | 0 | Jitter RNG |
| `min_usd_per_day` | 10.0 | Pass criterion, recorded with the run |
| `tags` | `[]` | Free text, searchable with `bt runs --tag/--search` |

Not config: `trade_echo_ms` (150) and `trade_explain_ms` (150) are `ExchangeConfig` constants
(`bt/exchange.py`), not settable with `--set`; `final_min_days` is the constant `runs.FINAL_MIN_DAYS = 5`.

## The fill model in plain words

Conservative by default: when in doubt, we do not get filled.

- **Back of the queue.** A limit order joins behind the level's whole recorded size at the moment it
  arrives (`order_latency_ms` after the strategy sent it). Matching runs on exchange time (`ts_ms`); the
  strategy only sees events at `recv_ms`.
- **One combined book.** Up and Down mirror each other: Up BUY at p is Down SELL at 1-p. Each market has one
  book kept in Up terms. Buying Down at q rests as selling Up at 1-q.
- **Trades on either token** eat the queue at our price (Down trades are mapped to Up terms). Once the
  queue ahead is zero, further volume at our price fills us.
- **Trade-through, capped.** A trade at a price worse than ours, on the side that rests at our price, fills
  us, but only up to that trade's size, because a taker smaller than our order would have stopped at our
  price.
- **At-price cap.** At our price, the volume that can eat the queue ahead and fill us is also capped by the
  recorded decrease of that level over [trade `ts_ms` - 150 ms, trade `ts_ms`] (the sum over those ms of the
  drops in its size; increases do not offset them). A taker that sweeps several levels is usually printed as
  ONE `pm_trade` row at its worst price with the total size, so without the cap volume executed at better
  levels would eat our queue at the worst one. Trade-throughs are not capped this way (already capped by the
  trade's size). Deliberately pessimistic: a trade whose level change was recorded earlier than 150 ms before
  its `ts_ms` fills nobody at its price.
- **150 ms trade echo.** A trade's `ts_ms` lands after the level change it caused (median about 21 ms on BTC
  5m, p99 134 ms). Trades within `trade_echo_ms` = 150 of our arrival are never matched against our order, so
  the same volume is not counted twice.
- **Cancel model.** A size drop at a level that no trade explains is someone else's cancel. `behind` assumes
  it was behind us, so our queue does not shrink. `proportional` removes it pro rata. Reports state which was
  used.
- **No self-impact.** Our orders do not change the recorded book or other traders. Reports show our sizes
  against level sizes.
- **Taker orders** walk the recorded book at arrival, limited to the visible depth. One fee per order, on the
  average price (`taker_fee`, verified on-chain; makers pay 0, rebates 0%). Liquidity we take stays removed
  from the level until it empties in the recording or `impact_reset_ms` passes.
- **Latency.** Defaults: 200 ms for orders and cancels, 126 ms for fill reports (measured feed delay).
  `--latency-sweep` shows 100/200/400 ms.
- **Window end.** No new orders after `window_end_ms`; resting orders expire and no fills happen at or after
  it.
- **Markets open late on purpose.** A market opens (`on_market_open`, orders accepted) only after its first
  `pm_book` snapshot inside the replay range has been received, because the book is untrusted before that.
- **Validation of orders.** Off-tick, below-minimum or out-of-range orders are rejected with a reason.
- **Gaps.** A window with a Polymarket gap over `max_gap_ms_pm` or a Binance gap over `max_gap_ms_bn` is
  voided: not run, no P&L, counted in the report. Data before 2026-11-24 10:36 UTC is never replayed. Gaps
  come from `gaps.gap_table()` / `gaps.voided_markets()` (there is no `gaps` SQL view): connection events in the
  daily `rec_events` catalog (sealed days included: they are data-health information, and a reconnect on a
  sealed day can leave the next day's start stale; each read is a `health` look) plus data silences (Binance
  top of book, Polymarket streams while Data API trades happened).
- **Ordering.** Same-`recv_ms` events are ordered by stream (Polymarket, Chainlink, Binance), then by `seq`
  (the raw arrival order kept in Parquet), so runs are deterministic.

## What the strategy can see

- Market data at its `recv_ms`; order events `fill_report_latency_ms` after the exchange produced them.
- `price_to_beat` is not read from the recording. It is computed from the received Chainlink ticks (mean of
  ticks with `ts` in the 60 s before `window_start`) and revealed when the first tick at or after the window
  start arrives, the same way a live runner would. It stays `None` when no tick fell in that span or the span
  starts before the replay range.
- `outcome` and `resolution_recv_ms` are always `None`. The outcome only reaches the portfolio, at
  settlement.
- Position and cash change only when a fill is *reported*, never ahead of what the strategy could know.

## Capital

- `capital` ($5,000) is split into equal fixed budgets per coin. All of a coin's markets share its budget (so
  cross-timeframe strategies work); coins replay in parallel. Budgets do not grow with profits.
  `capital_mode: global` uses one shared balance for strategies that trade across coins (single pass, slower).
- Buying Up at p locks p x size; buying Down at q locks q x size; a buy that can take liquidity also locks a
  fee bound. Selling locks shares held. Orders that exceed free cash are rejected.
- Equal Up and Down shares in a market are merged back into $1 each, immediately and free (to be confirmed
  in the live project).
- Winning shares pay $1 `redeem_delay_s` after the resolution was received (`pm_resolution` `recv_ms`).
  Until then the payout is pending: it counts in equity but not in free cash.

## Data splits and the sealed rule

Every recorded day has one permanent kind, set by its date: day number = days since 2026-11-25.

| Kind | Days | Used for |
|---|---|---|
| Sealed | day number mod 3 = 0 | `--final` only |
| Validation | mod 3 = 1 | The provisional verdict of ordinary runs; sweeps rank on it |
| Train | mod 3 = 2, plus 2026-11-24 from 10:36 UTC | Tuning and exploration |

- Sealed days are hidden in the data layer: the `polydata` views leave them out, so notebooks and screens
  cannot read them by accident. Reading them needs `include_sealed=True` with a `purpose`, and each read is
  appended to `data/runs/looks.jsonl`. **Do not tune on, or look at, sealed days.**
- `bt run --final` evaluates the strategy version on all sealed days (at least `runs.FINAL_MIN_DAYS` = 5, a
  constant; refused before then, and you cannot pass `--days`). It is refused unless `bt validate` has passed
  for the current `sim_version`, and refused (listing the fields) if the config changes any simulator,
  capital or pass setting from its default: `order_latency_ms`, `latency_jitter_ms`, `fill_report_latency_ms`,
  `cancel_model`, `impact_reset_ms`, `capital`, `capital_mode`, `redeem_delay_s`, `max_gap_ms_pm`,
  `max_gap_ms_bn`, `bn_step_ms`, `min_usd_per_day`. Strategy params, coins, timeframes, tags and seed are free.
- `--final` first runs `bt pull` so earlier finals and looks of other machines count. Offline use is allowed:
  if the pull fails it warns and records `pulled: false` in `provenance.json`.
- One final run per (strategy version, `sim_version`, data fingerprint). A simulator fix or re-synced data
  allows another; changing the strategy makes a new version. The report lists the earlier finals.
- **Trial budget N.** N counts the final runs (in the index, across all strategies) plus every other look at
  sealed data that is neither `health` nor `final` (a final run's own look is counted once, as the run). The
  pass threshold on block t is max(3, z(1 - 0.05/N)): 3 up to 37 looks, 3.29 at N = 100, 3.89 at N = 1,000.
  Validation runs and `health` looks (gap detection reading sealed-day `rec_events`) do not count.
- `sim_version` is a hash of `bt/*.py` (excluding `report.py`, `runs.py`, `cli.py`), `bt/strategies/sanity.py`
  (the strategies the suite runs), `fees.py` and the data layer (`views.py`, `build.py`, `schema.py`). Any
  change to them changes it and means re-running `bt validate`.

## How to read a report

The header shows the verdict, the fill-model status ("Fill model: unverified"), the cancel model, and for
final runs N and the threshold used.

| Label | Meaning |
|---|---|
| `PASS (provisional)` / `FAIL (provisional)` | Criteria judged on the validation days of an ordinary run |
| `PASS` / `FAIL` | A `--final` run on the sealed days: the verdict |
| `INCOMPLETE (no 400 ms sweep)` | Nothing failed but the 400 ms latency check was not run; rerun with `--latency-sweep` |
| `no validation days yet` | The run had no validation days (train-only), so there is nothing to judge |
| `no sealed-day data` | A final run found no sealed data |

Criteria (all must hold; defaults, recorded in the run):

- P&L > 0, 95% block-bootstrap lower bound > 0 and t > threshold on 15-minute block P&L (not judged below 30
  blocks, which counts as a fail, not a pass)
- P&L > 0 on the most recent third of the evaluated days
- at least `min_usd_per_day` ($10) per day
- at least 200 fills per day
- realised max drawdown < 10% of capital
- profitable at 400 ms order latency
- at least 70% of days positive, judged only with 7 or more days (otherwise shown as "not judged")

P&L attribution, per share and in total, over settled fills:

| Part | Meaning |
|---|---|
| Spread | (mid 200 ms before the fill - fill price) in our favour: what the spread earned |
| Adverse | The mid's move over the 10 s after the fill: losses to informed takers |
| Inventory | Settlement value minus the mid 10 s after: what holding to resolution did |
| Fees | Taker fees paid |

They sum to the run's P&L. The report also shows equity (train vs validation), daily P&L, drawdown, results
by coin and timeframe, realism (fill rate, share of fills within 1 s after a Binance move, queue ahead,
our size vs level size, voided windows), latency sensitivity, and a robustness grid when a sweep exists.

## The screening tier

For ideas that take liquidity (endgame, momentum, order-flow, cross-timeframe, ...), `polydata.screen` tests
a rule in seconds over the point-in-time snapshots (`polydata.research.snapshots`) instead of running the
simulator.

- Fills are pessimistic: the full ask (or bid) `latency_ms` after the signal (300 or 1000), plus the taker fee.
  Exit is resolution (`hold`) or a taker sale at the bid about 2 s later (`bid`).
- Output: cents per share and $ per day with a 15-minute-block bootstrap CI, by day kind, coin and timeframe.
  Screens never read sealed days, skip voided windows, and are recorded in the runs index.
- **A screen can only reject or promote** (promote = lower CI bound on validation days > 0). A promoted idea
  becomes a strategy class and goes through the simulator; only that can pass.

To add a screen, in `analysis/src/polydata/screen.py` write a function taking the snapshot frame and returning
rows `slug, t_ms, token ('Up'|'Down')` and optionally `size`, then register it:

```python
def my_rule(snap): ...
register(Screen("my-screen", "momentum", my_rule))   # name, family, rule
```

Signal inputs must be columns known at `t_ms` (`up_bid`, `up_ask`, `tau_s`, ...). The `*_300` and `*_1000`
columns are execution prices that look ahead of `t_ms`, and `outcome_up` is the result: `bt screen` passes the
rule a frame without them (`screen.FORBIDDEN_RULE_COLUMNS`). Voided windows come from the gap table, computed
per contiguous range of non-sealed days (a hidden sealed day would otherwise look like a 24 h silence). Add a test in
`tests/bt/test_screen.py`, then `uv run polydata bt screen my-screen`.

## Validation suite

`bt validate` writes `data/runs/validation/<sim_version>.json`. `--final` checks it. It compares the simulator
with references computed independently in DuckDB SQL over the raw views:

1. **Book reconstruction:** the combined book's best bid and ask match `pm_best_bid_ask` in at least 99.5% of
   sampled moments, per market.
2. **Sanity strategies:** `noop` earns exactly 0; `buy-and-hold-up` and `always-cross` match a direct DuckDB
   computation to the cent.
3. **Fill realism:** every `join-best` maker fill is re-derived from the raw trades with the fill model's
   rules (checked order by order); and `join-best` earns less per share than the average maker in the
   calibration study.
4. **Determinism:** the same config and data give byte-identical results.

It is internal consistency only. It cannot say whether the queue and delay model matches the real exchange.

## Known limitations

- **Fill model: unverified.** Until our own real fills (minimum-size orders in the live project) are compared
  with the simulator's prediction, every report says so. Queue position in particular is an assumption.
- **At-price volume cap.** A recorded trade fills or advances us at its own price only up to the recorded
  drop of that level in the 150 ms before it (see the fill model). It guards against sweeps printed as one row
  at the worst price (2026-11-24: for isolated BTC 5m trades with a better level cleared just before, only 44%
  of the printed volume shows up as a drop at the printed price, against 94% otherwise). It is pessimistic
  when a level change lags its trade by more than 150 ms, and the same drop can bound two trades in the window.
- **Current-day connection gaps are unknown.** `rec_events` is exported to the catalog once a day, so a run
  on today's data sees only data silences, not reconnects. Re-run once the day's catalog exists.
- **Singapore receive times.** Every backtest is tied to the Singapore recorder's `recv_ms`. A strategy
  running elsewhere sees a different world and different latencies. Gaps are permanent.
- **Screens use a fixed size and no depth.** Snapshots have no sizes, so every screen signal fills in full at
  its size (default 10 shares). Capacity is not measured.
- **Eval drawdown is realised-only.** It is computed from settled markets in settle order, not marked to
  market, so open-position swings inside a market are not in it.
- **One-sided books.** A book with an empty side has no mid (the strategy's `mid` is `None`, and fair-value
  centring waits); mid-based attribution falls back to the fill price.
- **Backfilled resolutions delay payout cash.** The payout is credited `redeem_delay_s` after the
  resolution's `recv_ms`. A resolution recorded late (backfilled) delays the cash, which can hold up a
  coin's budget in the replay longer than it would live.
- **Little data.** Results so far rest on a few hours to days in one market regime. Re-run the calibration
  study at 7 and 14 days before trusting any edge.
- **Merging and redemption are assumed instant and free** (merging is a relayer operation; to be confirmed).
- **Python speed.** The target is about 10 minutes per day of data for BTC+ETH 5m/15m with one strategy; all
  coins and 1h markets take longer, which is why sweeps should be screened or run on few days first.

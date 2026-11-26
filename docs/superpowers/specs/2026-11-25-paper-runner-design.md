# Paper runner design (polydata.paper)

Status: approved by the controller · 2026-11-25 (the user delegated the design calls, recorded under "My calls"; the sealed-day call was changed in review).
Extends [the backtester design](2026-11-24-backtester-design.md); read that first.

## Intent

Run a registered `Strategy` class, unchanged, on live public market data, with fills simulated by the
backtester's own fill model (`bt/exchange.py`). No real orders exist anywhere in this project.

The point is not a second simulator. It is a test that **the backtester predicts what the strategy does
live**: the paper runner records every input it used, and replaying that recording through `polydata.bt`
must reproduce the paper run's decisions and fills exactly. Once parity holds, a paper run is just a
backtest of data we recorded ourselves, so every later difference between paper and real trading is the
fill model's or the exchange's, not ours.

First user: `endgame-maker` (8 coins × 5m, `configs/validation/endgame-maker-v2.yaml`).

## Goals

1. **Same strategy, same simulator.** `Strategy` subclasses run unchanged against a live `Ctx`. Fills come
   from `Exchange` on exchange time (`ts_ms`) with the configured order and report delays.
2. **Parity.** Every input is recorded in the recorder's file formats; `polydata paper replay` replays the
   session through `polydata.bt` and must reproduce every strategy call, order event and fill exactly.
3. **Live feeds, read-only.** Polymarket CLOB market WebSocket (the config's coins and timeframes only),
   Polymarket RTDS Chainlink prices, Binance USD-M futures book ticker (plus aggTrade, see gaps). Gamma and
   Data API REST for discovery, confirmation and gap checks. Nothing authenticates or signs.
4. **Robust.** Reconnects; disconnects, silences and late data void the affected markets with the
   `bt/gaps.py` rules.
5. **Same records.** A run folder shaped like a bt run folder (`kind: "paper"`), indexed with the other runs.
6. **Testable offline.** A fake feed plays recorded rows through the live path at real or accelerated
   speed, so parity is a unit test.

## Non-goals

- Real orders, API keys, wallets, signing, order routing. Merging/redeeming on-chain.
- Telegram or other alerts. Multi-strategy portfolios (one strategy per process; per-coin or global budgets
  as in bt).
- Running on the droplet by default (single vCPU, runs the recorder). No new servers.
- Measuring real queue position. Paper fills are simulator fills; "Fill model: unverified" stays.
- Making the paper strategy react in real time. The strategy clock runs a few seconds behind the wall clock
  (see the hold-back); that is harmless without real orders and is what makes parity exact.

## Anti-goals (failure even if it "works")

- A paper result that the replay of its own session doesn't reproduce.
- A live-only code path that changes what the strategy sees (a second book builder with its own rules, a
  different event order, a different `price_to_beat` rule).
- Any input the replay can't see (wall-clock reads, REST answers not written to the session).

## Architecture and data flow

```
 WS/REST feeds ──parse──> rows (recorder columns) ──stamp recv_ms──> Ingest
                                                                       │ every row → SessionWriter (raw/…tsv.zst, jsonl.zst)
                                                                       │ late rows → session/late/ (not used)
                                                                       ▼
                                                     LiveSource per budget (coin, or "*")
                                                       reorder buffer, release by horizon
                                                       ├─ GrowingMarketData per market (ts side ≤ H_ts, recv side ≤ T)
                                                       ├─ Binance hour buckets, Chainlink ticks (≤ T)
                                                       └─ event batches (recv ≤ T) ──> Core (bt engine core)
                                                                                         Strategy ← LiveCtx (= SimCtx)
                                                                                         Exchange (fill model), Portfolio
                                                                                         └─> orders/fills/equity/markets/decisions
```

Single Python process, one asyncio loop. A tick runs every `tick_ms` (20) of wall time.

**Clocks.** As in bt:

| Clock | Live meaning |
|---|---|
| Strategy time `ctx.now` | `recv_ms`: our wall-clock receive time of the event (monotone, see below) |
| Exchange time | the event's `ts_ms` from Polymarket; the exchange is advanced to `now − fill_report_latency_ms` |
| Order arrival | `now + order_latency_ms (+ jitter)` |

**The hold-back** (the key idea). The replay engine advances the exchange to `T − R` (R =
`fill_report_latency_ms`) using every recorded trade with `ts ≤ T − R`, including trades *received* after T
(Polymarket's feed delay is ~126 ms median, so about half of them). Live, those trades haven't arrived yet.
So the live core runs behind the wall clock:

- `H_ts = wall − hold_back_ms` (default 3,000): every Polymarket row with `ts_ms ≤ H_ts` is released, in
  `(ts_ms, snapshot-after-change, recv_ms, seq)` order, into the ts-ordered structures (levels, ts-order tops
  and trades, tick sizes).
- `T = H_ts − skew_ms` (default 1,000): the strategy clock. Events with `recv_ms ≤ T` are released into the
  recv-ordered structures and dispatched.
- A row is **late** if, when it arrives, `ts_ms ≤` the last released `H_ts` (feed delay > hold-back). A row is
  **future-stamped** if `ts_ms > recv_ms + skew_ms` (Polymarket clock ahead of ours by more than the skew). Both
  are dropped from the engine and from the session's stream files, written to `session/late/`, counted, and
  void their market (see gaps). Binance and Chainlink rows are only used on the recv clock and are never late.

With these two horizons every query the core makes is answered from complete data: exchange queries are at
`≤ T − R < H_ts`, strategy queries at `≤ T`, and every row with `recv ≤ T` has `ts ≤ T + skew = H_ts`. The core
therefore sees exactly what the replay sees, in the same order. The strategy decides 4 s late in wall time;
it never learns this (all its times are `recv_ms`).

Each tick: (1) release Polymarket rows with `ts_ms ≤ H_ts` (and lower `next_ex` for appended trades);
(2) release rows with `recv_ms ≤ T` and dispatch them, sorted by the replay's key; (3) drain internal events
with time ≤ T (`_drain(T + 1)`). The replay drains lazily before its next data event; the result is the same
because no data event ≤ T is left.

**Monotone receive time.** `recv_ms = max(previous recv_ms, wall_ms)` across all feeds (a backward NTP step
must not reorder events). Steps and event-loop stalls > 2 s are logged as `recorder` stop/start events, which
void everything they overlap (gaps).

**Per-budget cores.** Per-coin mode runs one Core per coin, each with its own Strategy instance, RNG seed,
Portfolio and budget, exactly like `engine.run` (one process per coin there, one process total here).
Global mode runs one Core. The router sends each row to its coin's LiveSource.

**Market admission.** A market is admitted (handed to its Core) when its `pm_market_meta` row is processed on
the strategy clock, at `max(meta recv_ms, rs)`, if its coin and timeframe are in the config,
`window_start_ms ≥ rs` (session start) and the runner is not draining. Its `MarketInfo` is derived by running
`views.MARKETS_SQL` in an in-memory DuckDB over that one meta row, so live and replay parse Gamma identically.
It opens (`on_market_open`) at the first `pm_book` snapshot received at or after admission, and closes at
`window_end_ms` (as in bt). The admitted list and admission times go into the session manifest.

## Engine refactor

Goal: one event-driven core, two drivers. No change in behaviour for ordinary backtests.

### What moves where

- `bt/engine.py` keeps the public API (`run`, `run_coin`, `RunResult`, `CoinResult`, `SimCtx`).
- `_Replay` splits into:
  - **`Core`** (`bt/core.py`): everything that is not loading. Scheduling (`push`, `_drain`, `_catch_up`,
    `_advance`), orders (`submit`, `cancel`, `set_timer`), exchange/report handling (`_on_ex`, `_report`),
    internal events (`_handle`, `_close`, `_settle`, equity), strategy queries (`bbo`, `cl_tick`,
    `fair_value`, `_bn_move`), the hot loop `dispatch(recv, kind, mi, idx, bn_lists, cl_lists)`, and new
    entry points: `add_market(info, md, open_after_ms)`, `add_bn_hour(...)` / `add_cl(...)` (the hour
    buffers the replay already keeps), `set_resolution(slug, outcome, recv_ms)`.
  - **`ReplayDriver`** (`bt/engine.py`): `_ranges`, `_select_markets`, `_load_chunk`, `_load_bn`, `_load_cl`,
    `_segment`, `_schedule_ptb`, `run`. It calls Core exactly as `_Replay` calls itself today.
  - **`LiveSource`** (`bt/live.py`): the incremental driver (horizons, reorder buffer, batching). It lives in
    `bt/` because it decides event order, i.e. simulation semantics, so it is covered by `sim_version`.
- `Exchange` stops caching the trade arrays at construction (`_ts`, `_tside`, `_tpx`, `_tsize`, `_n`): it
  reads them from `md.trades` at each `advance`/`next_event_ms`, which is free for the static `MarketData`
  and picks up appended trades for the growing one.
- When trades are appended to a market with resting orders, Core lowers `next_ex` to the new trade's
  `ts_ms` (always ≥ the exchange's processed time, by the hold-back).

### Ordering rules made explicit

Today some orders are implicit in when `_Replay` pushes things. The refactor names them so the live driver
can reproduce them; in replay mode they evaluate to today's order.

- **Data events** at equal `recv_ms`: stream rank (Polymarket < Chainlink < Binance), then kind
  (`K_ADMIT` (new, session/live only) < `K_OPEN` < `K_TOP` < `K_TRADE`), then **load order** of the market
  (today: `mi`), then row index. Unchanged `np.lexsort`.
- **Load order** is chosen by the driver: ReplayDriver = chunk order (unchanged); live and session replay =
  admission order (manifest). Load order also fixes `Portfolio.add_market` order, i.e. dict iteration order
  in equity sums, so it must be identical in a paper run and its replay.
- **Internal events** at equal `(t, prio)`: push order (`seq`), as today. Three cases need a rule because live
  pushes them at a different moment than the replay:
  - *Close* is pushed when the market is added (both drivers).
  - *Settle*: replay knows the outcome at load and pushes at close; live learns it later. Settles at equal
    `t` are ordered by `(window_end_ms, load order)`, which is today's push order (closes run in that order).
  - *ptb reveal*: the replay pushes it at the start of the hour segment in which it is found, at
    `max(r, s)`. Live pushes it when the revealing Chainlink tick is released, with the sequence number the
    Core had at `s* = max(rs, floor_hour(max(r, window_start)))` (Core records `seq` at every hour boundary
    of the strategy clock), at time `max(r, s*)`. Several reveals in one push are ordered by load order.
    This reproduces the replay exactly, including a strategy timer at the reveal millisecond.
- **Settlement annotation.** `_annotate` writes `settle_value` at close in replay (outcome known); live
  writes it at settlement. The final fills table is identical.

### Proof that replay is unchanged

1. Before the refactor (current `main`), run on 2026-11-24 (a train day; override `day_kinds` accordingly)
   the three `configs/validation/*.yaml` and the four validation strategies (`noop`, `buy-and-hold-up`,
   `always-cross`, `join-best`). Save `results.json` and a content hash of each Parquet output (row-wise, so
   Parquet metadata doesn't matter).
2. After each refactor step: same runs, compare bytes of `results.json` and the hashes. Any difference
   blocks the step.
3. `polydata bt validate` passes for the new `sim_version` (it will change; expected).
4. The script lives in `analysis/tools/replay_identity.py` so later simulator changes can reuse it.

## Live MarketData

`GrowingMarketData` (`bt/marketdata_live.py`) implements what `Exchange`, Core and `SimCtx` read from
`MarketData`: `info`, `size_at`, `book_at`, `level_changes`, `prices`, `top_at`, `tick_at`, `trades`,
`trades_recv`, `tops_recv`, `snaps_recv`. Semantics are the SQL pipeline's (`marketdata.py`), applied
incrementally:

- **Ts side** (appended up to `H_ts`, in `(ts_ms, change before snapshot, recv_ms, seq)` order):
  - level changes from Up `pm_price_change` rows; a Down row only when its Up mirror (same `ts_ms`,
    opposite side, `1000 − px`) is absent. Same-ts completeness is guaranteed because a whole `ts_ms` is
    released at once.
  - snapshots: one per `ts_ms` (Up preferred, then latest `recv_ms`, `seq`); every listed level is set and
    every other positive level gets a size-0 entry at the snapshot.
  - per level: `ts_ms`, `recv_ms`, `recv_eff` (running max of `recv_ms`, prefix-stable), `size`,
    `last_pos`; price keys kept sorted (`bisect`).
  - ts-order tops (only rows where `(bid, ask)` changes), ts-order trades (with Up-terms side/price), tick
    sizes (`tick_size_change` deduplicated on `(ts_ms, tick)`, earliest `recv_ms`).
- **Recv side** (appended up to `T`, in `(recv_ms, ts key, seq)` order): `tops_recv`, `trades_recv`,
  recv-order ticks, `snaps_recv`.
- Arrays are capacity-doubling numpy buffers exposed as views of the filled part; queries are the same
  `searchsorted` calls as `MarketData`. Appending never changes an already-visible answer, so a lazy
  `BookView` built at time t stays correct.
- Prices are parsed as `round(float(text) × 1000)` (valid ticks are exact multiples, so no rounding-rule
  difference from DuckDB), empty `best_bid`/`best_ask` map to 0 / 1000 as `_BID`/`_ASK` do.
- **Equivalence test:** for synthetic frames and for real hours (BTC, SOL 5m, 2026-11-24 11:00–12:00,
  `POLYDATA_REALDATA=1`), build `MarketData.from_frames` and `GrowingMarketData` fed row by row, and compare
  every query on a dense time grid on both clocks.

Binance: rows bucketed by `recv_ms // bn_step_ms`; a bucket is final when `T ≥` its last ms; the last row by
`(recv_ms, update_id)` wins, delivered at the bucket's last ms, kept in per-hour arrays (current + previous
hour, as `bn_cur`/`bn_prev`), feeding the same `_Grid`. Chainlink: first receipt of each `(coin, ts_ms)` wins;
only ticks first received at or after `rs` are visible (as `_load_cl`).

## Feeds (`polydata/paper/feeds/`, asyncio)

Ported from the recorder; behaviour and row shapes must match it (cited by file and line). Every message
yields rows in the recorder's columns; all rows of one message share one `recv_ms` and keep message order
(`seq` = arrival order per stream). Numbers are written as received (string fields verbatim; JSON numbers as
Python `repr`, which round-trips), so DuckDB's casts in `polydata build` give the same doubles the live
engine parsed. One WebSocket client class (`websockets` library) does what `src/recorder/wsClient.ts` does:
backoff 1 s doubling to 30 s, text keepalive, stale timeout, max lifetime, and `ws_open` / `ws_close` /
`ws_stale` events with the recorder's source names.

### Polymarket discovery and CLOB (`src/recorder/polymarket.ts`)

- **Series table**: a Python copy of `SERIES`, `LOOKAHEAD`, `subscribeLeadWindows`, `minSubscribeLeadMs`,
  `unsubscribeAfterEndMs` (`config.ts`, `polymarket.ts:121`), filtered to the config's coins and timeframes
  (`hourly` = `1h`, `daily` = `1d`). A test parses `config.ts` and fails if the copies drift.
- **Discovery** every 30 s (`polymarket.ts:197`): `GET {gamma}/events?series_id=…&closed=false&end_date_min=
  now&order=endDate&ascending=true&limit=LOOKAHEAD`. A new event → `pm_market_meta` row `{series, event}`
  (`registerMarket`, `:159`) and token → `slug|U` / `slug|D` keys.
- **Subscriptions** every 5 s (`updateSubscriptions`, `:215`): subscribe at `start − max(10 min, 2 windows)`,
  unsubscribe at `end + 3 min`. Connections grouped as the recorder does: one per series, one per market for
  `PER_MARKET_SERIES` (`:84`); subscribe message `{assets_ids, type: "market", custom_feature_enabled: true}`,
  `PING` every 10 s, stale after 25 s, source name `pm:<group>`.
- **Message → rows** (`onMessage`, `:50`), arrays iterated in order:

| `event_type` | Stream | Columns after `recv_ms` |
|---|---|---|
| `book` | `pm_book` | timestamp, key(asset_id), bids, asks (`price:size\|…` in message order), hash |
| `price_change` | `pm_price_change` | one row per `price_changes[]` entry: m.timestamp, key(c.asset_id), side, price, size, best_bid, best_ask |
| `best_bid_ask` | `pm_best_bid_ask` | timestamp, key, best_bid, best_ask, spread |
| `last_trade_price` | `pm_trade` | timestamp, key, side, price, size, fee_rate_bps, transaction_hash |
| `new_market` | `pm_new_market` (JSONL) | whole message |
| anything else (`tick_size_change`, `market_resolved`, …) | `pm_other` (JSONL) | whole message |

  The engine uses `pm_book`, `pm_price_change`, `pm_trade` and `tick_size_change`; the rest is recorded
  because it is free and lets `reconstruction_rate` run on a session.

### Chainlink (`src/recorder/rtds.ts`)

Two connections (`a`, `b`, second started 2 s later), subscribe to `crypto_prices_chainlink` (`*`) and
`crypto_prices` (`update`); `PING` every 5 s, stale after 5 s; sources `polymarket-rtds-a|b`. Updates →
`rtds_price` rows `topic, symbol, payload.timestamp, value, full_accuracy_value, m.timestamp, conn`, written
only if `ts > last ts` for that `topic|symbol` (`rtds.ts:36–47`); everything else → `rtds_other`. The engine
uses the Chainlink topic for the configured coins (`symbol` = `<coin>/usd`).

### Binance USD-M futures (`src/recorder/binance.ts`)

- `binance-fut-bbo`: `{fapi}/public/stream?streams=<sym>@bookTicker/…` → `bn_fut_bbo` rows
  `s, u, E, T, b, B, a, A` (`binance.ts:69`).
- `binance-fut-market`: `{fapi}/market/stream?streams=<sym>@aggTrade/…` → `bn_fut_trade` rows
  `s, E, T, a, f, l, p, q, m?1:0` (`:73`). Not used by strategies; the gap rule needs it (a Binance silence
  only counts if the symbol traded inside it).
- Stale after 30 s, reconnect before Binance's 24 h cut (`maxLifetimeMs` 23 h, `:141`).
- **Session thinning:** per `(sym, 10 ms bucket)` only the first-received row and the last row by
  `(recv_ms, update_id)` are written. The replay reads exactly these (the bundle is the last row; silences are
  gaps between consecutive `recv_ms`, which the first row preserves). Saves most of the ~5k rows/s.
  Valid for any `bn_step_ms` that is a multiple of 10 (the default); the manifest records it.

### REST (read-only, public)

Gamma discovery and resolution polls, Data API v2 trades (`fetchTrades`, `:254`) after `end + 120 s`. Rate
limiting as the recorder: on 429 pause that API ≥ 10 s; per-market backoff 30 s → 10 min. Every answer that
the run uses is written to the session (`pm_market_meta`, `pm_api_trades`, `paper/gamma_resolution`).

## Gaps

A paper run cannot skip a market before it happens, so gaps don't hide markets from the strategy (bt's
`voided_markets` runs before the replay; here it runs after). **Voiding is post-hoc and only affects
accounting**: the strategy trades the market as it saw it; the market's P&L is reported separately and left
out of every metric, as in bt. Rules, all computed from session files only, so the replay derives the same
voids:

1. **Connection events.** Every WS client writes `ws_open` / `ws_close` / `ws_stale` to
   `session/catalog/<day>/rec_events.csv.zst` (columns `ts, kind, source, detail`, as the daily catalog).
   `gaps.event_gaps` turns them into gaps.
2. **Silences.** `gaps.bn_silences` (needs `bn_fut_trade`) and `gaps.pm_silences` (needs `pm_api_trades`,
   fetched after each market ends) over the session store.
3. **Stalls and clock steps.** An event-loop stall or backward clock step > 2 s writes a `recorder`
   `stop`/`start` pair, which `event_gaps` turns into a gap on every stream.
4. **Late / future-stamped Polymarket rows** (see the hold-back): the market is voided, reason
   `late data (n rows, max x ms)`, from `session/late/`. Only rows received in `[open, window end)` count, where `open` is the receive time of the market's
   first `pm_book` row at or after its admission: the opening snapshot of a quiet book carries the book's
   last-change time (older than the hold-back), so it arrives late before the market is open without any data
   missing. Rows of other streams in `late/` never void.
5. **No outcome** at the end (no Chainlink settlement and no Gamma answer).

Thresholds are bt's: `max_gap_ms_pm` 5,000, `max_gap_ms_bn` 1,000. `voided_markets` is applied unchanged to
the session's markets and gap table. A voided market keeps its raw P&L in `pnl_raw`; `pnl` is NaN.

**gaps.py fix needed first (found while reading):** `event_gaps` maps the sources `binance-fut-public` and
`polymarket-rtds`, but the recorder logs `binance-fut-bbo` and `polymarket-rtds-a|b`; and per-market Polymarket
connections log `pm:<market slug>`, which `voided_markets` never matches (it matches the series or `*`). So
today a Binance disconnect and a disconnect of the BTC 5m/15m or ETH 5m market connections void nothing in bt
(only the silence rules catch them). Fix: map `binance-fut-bbo` to every symbol, and match a `pm` gap key
against the market's slug as well as its series. This changes bt results on days with such events, so it is
its own PR with `bt validate`, a data-quality-log line and journal notes.

## Settlement

- **Outcome (primary): Chainlink.** `close_twap` = mean of the market's coin's first-received Chainlink
  ticks with `ts_ms` in `[window_end − 60 s, window_end)`; `ptb` = the same over
  `[window_start − 60 s, window_start)` (the value the strategy was shown). Outcome Up iff
  `close_twap ≥ ptb` (verified 635/635 against Gamma). It is known when the first tick with
  `ts_ms ≥ window_end` is released (strategy time `r_end`).
- The runner then writes a **`pm_resolution` row** at `recv_ms = r_end`:
  `{slug, outcome, event: <discovery event with eventMetadata.priceToBeat = ptb, finalPrice = close_twap>,
  source: "paper-chainlink"}`. `views.MARKETS_SQL` reads it as it reads the recorder's, so the replay settles
  at the same time with the same outcome. Core is told via `set_resolution`.
- **Fallback:** if either 60 s window has no tick, or `window_start − 60 s < rs`, the outcome waits for Gamma;
  the `pm_resolution` row is then written at the Gamma answer's `recv_ms` with `source: "paper-gamma"`.
- **Confirmation:** Gamma `/events?slug=` polled from `end + 30 s` every 30 s (as `resolveEnded`), up to
  `--confirm-wait` (default 15 min) after the last market; answers go to `session/paper/gamma_resolution
  .jsonl.zst` (not a recorder stream, so `build` ignores it). `markets.parquet` gets `outcome_source`,
  `gamma_outcome`, `gamma_seen_ms`, `outcome_mismatch`. A mismatch is an error in `results.json`; P&L keeps the
  Chainlink outcome (parity), and `pnl_usd_gamma` restates it.
- **Cash:** credited `redeem_delay_s` (60) after `r_end`, as bt credits it after the resolution's `recv_ms`.
  This is earlier than bt on recorder data (Gamma seen ≈ end + 30–60 s); see My calls.
- **Stop:** `end_ms` = max(strategy time of the stop request or `--duration` end, last admitted
  `window_end_ms`). Data events are dispatched up to `end_ms`, as the replay's range ends there. After it the
  runner keeps ingesting only to obtain the missing `pm_resolution` rows (bounded by `--max-drain`, default
  15 min); resolutions learned after `end_ms` are set without dispatching anything; then `ending = True`,
  `drain(∞)`, exactly as `_Replay.run` ends. `end_ms` goes into the manifest.

## Run folder and session format

`data/runs/<family>/<strategy>/<YYYY-MM-DDTHHMM>-<shortid>/` (`runs.new_run_dir`), or `--out DIR`:

| File | Contents |
|---|---|
| `config.yaml` | the RunConfig (as bt) plus a `paper:` block (hold-back, skew, tick, warm-up, drain, confirm-wait, duration) |
| `provenance.json` | `kind: "paper"`, git commit + diff, `sim_version`, `validated` (bt validate passed for it), `strategy_version`, `paper_version` (hash of `polydata/paper/*.py`), host hash, Python/package versions, session fingerprint (manifest hash), `PYTHONHASHSEED` |
| `results.json` | bt's summary keys (`pnl_usd`, `usd_per_day`, `fills`, `orders`, `markets`, `voided`, `days`, …; `verdict: null`) plus paper stats: rows per stream, late/future-stamped rows, gaps, processing lag p50/p99/max, feed delay (`recv − ts`) p50/p99 per stream, outcome mismatches, `pnl_usd_gamma`, `parity` (set by `paper replay`) |
| `orders.parquet`, `fills.parquet`, `equity.parquet` | bt schemas, unchanged |
| `markets.parquet` | bt `MARKET_COLS` plus `pnl_raw`, `outcome_source`, `gamma_outcome`, `gamma_seen_ms`, `outcome_mismatch`, `admitted_ms` |
| `decisions.parquet` | the call trace: budget, n, `now`, handler or ctx call, key (slug / sym / timer / order id), args (price, size, token, side), return value |
| `log.txt` | the strategy's `ctx.log` lines (as bt) and runner status lines (prefixed `#`) |
| `session/` | below |
| `parity/`, `parity.json` | written by `paper replay` |

`session/` is a `POLYDATA_DIR`-shaped store, so `POLYDATA_DIR=<run>/session polydata build` works unchanged:

```
session/manifest.json      rs, end_ms, clean_stop, coins, timeframes, hold_back_ms, skew_ms, bn_step_ms,
                           bn thinning, admitted [{slug, admitted_ms, load_index}], files [{path, rows, sha256}]
session/raw/YYYY-MM-DD/HH/<stream>.<slotStartMs>.{tsv,jsonl}.zst   recorder layout, 5-minute slots, TSV header row
session/catalog/YYYY-MM-DD/rec_events.csv.zst                       connection/stall events
session/late/<stream>.tsv.zst                                       dropped rows (+ late_by_ms), never built
session/paper/gamma_resolution.jsonl.zst                            confirmations, never built
session/parquet/                                                    built on demand by `paper replay`
```

Streams written: `pm_book`, `pm_price_change`, `pm_best_bid_ask`, `pm_trade`, `pm_market_meta`,
`pm_resolution`, `pm_api_trades`, `pm_new_market`, `pm_other`, `rtds_price`, `rtds_other`, `bn_fut_bbo`
(thinned), `bn_fut_trade`. Column sets are exactly `schema.TSV` (no new columns, so `schema.py` doesn't
change). Files are zstd frames flushed every 10 s (a crash loses ≤ 10 s, and every file stays readable).
Writing runs in a thread (`zstandard` releases the GIL), level 3.

Sessions are large (Binance), so `bt push` skips `session/` unless `--with-sessions`.

## Parity check (`polydata paper replay RUN`)

1. Build `session/parquet` from `session/raw` (`build` with `POLYDATA_DIR` = session).
2. Run `polydata.bt` over it in **session mode**: the ReplayDriver with explicit range `[rs, end_ms)` (not
   `_ranges`), market load = the manifest's admitted markets at their `admitted_ms` (as `K_ADMIT` events, in
   manifest order), open threshold = admission, no pre-voiding, the run's config (same seed, delays, models),
   tracing on. Views over the session include sealed days (see My calls on looks).
3. Apply the paper void rules to the replay's markets.
4. Compare with the paper run's files and write `parity/` (the replay's outputs) and `parity.json`.

**Definition.** Parity holds when, per budget:

| Output | Requirement |
|---|---|
| `decisions.parquet` | identical rows, in order (every strategy call at the same `now`, same arguments, same returns) |
| `orders.parquet`, `fills.parquet` | identical (all columns, NaN = NaN) |
| `equity.parquet` | identical |
| `markets.parquet` | identical on bt's columns |
| `log.txt` | identical strategy lines |

No numeric tolerance: the same code computes the same doubles from the same inputs in the same order, so any
difference is a bug. `parity.json`: `passed`, per-table row counts and mismatch counts, and the first
divergence (budget, index, both rows, the 10 decisions before it).

**Where exact parity is not possible, and what we do:**

| Case | Treatment |
|---|---|
| Rows later than the hold-back, or future-stamped | dropped before use, so parity still holds; fidelity loss, the market is voided; rate reported |
| Hard stop or crash (no drain) | `clean_stop: false`; compare decisions with `now <` last flushed `recv_ms` and markets closed before it; verdict `passed-truncated` |
| Different host or CPU architecture for the replay | libm may differ in the last bit (`log`, `sqrt` in sig15 / fair value): verdict `informational` (1e-9 relative tolerance reported, not a pass) |
| A strategy reading clocks, randomness or global state | not tolerated: parity fails, and that is a strategy bug (CLAUDE.md rule) |
| Hash-order dependence (sets of strings in a strategy) | both sides run with `PYTHONHASHSEED=0` (the CLI re-execs itself if unset) |
| Equal-`recv_ms` ordering, Binance 10 ms bundles, `price_to_beat` reveal, settlement order | exact by construction (same rules, see the engine refactor), not tolerances |

**Fidelity is a separate question.** `paper replay --vs-recorder` additionally runs a normal bt replay of
the same window on the recorder's store (Singapore `recv_ms`) and reports the differences (decisions, fills,
P&L). That measures location and feed effects; it is never a pass/fail.

## Fake feed (offline testing)

`polydata/paper/fakefeed.py` replaces the WebSocket and REST clients, not the ingest: it reads recorded rows
(from a store range `--fake-from A --fake-to B`, or a session dir) and injects them into the router as parsed
rows with their recorded `recv_ms`, under a **virtual clock** (wall time = the clock the ingest reads). Speed:
`N×` real time (sleeps scaled) or `max` (jump to the next row). It also replays `rec_events` and REST answers
(`pm_market_meta`, `pm_resolution`, `pm_api_trades`) at their `recv_ms`. Fault injection: delay chosen rows
beyond the hold-back (late path), drop a connection's rows for a span with matching `rec_events` (gap path),
stall the clock.

Tests:
- **CI (synthetic):** generated markets with level changes, snapshots, trades, mirrored Down rows, a tick
  change, Binance and Chainlink rows; run sanity strategies and `endgame-maker` through the fake feed and
  through session-mode replay; assert parity. Plus `GrowingMarketData` equivalence, parser golden tests
  (captured raw messages → expected rows), and that a fake-fed session builds to the same rows as its source.
- **Real data (`POLYDATA_REALDATA=1`):** 2026-11-24 11:00–13:00 UTC, all 8 coins, the three validation
  configs: fake feed at `max` → session → replay → parity. Also `--vs-recorder` there: the inputs are the
  same rows, so every difference must come from the two documented schedule differences (load order and
  open threshold = admission instead of chunk). Expected: none for these configs. This ties the live path to
  ordinary backtests.

## CLI

```
polydata paper run CONFIG.yaml [--out DIR] [--duration 6h] [--coins ...] [--set k=v ...]
                               [--hold-back-ms 3000] [--skew-ms 1000] [--warmup-s 60]
                               [--max-drain 15m] [--confirm-wait 15m]
                               [--fake-from A --fake-to B | --fake-session DIR] [--speed N|max]
polydata paper replay RUN_DIR [--vs-recorder] [--keep-parquet]     # exit code 1 if parity fails
```

- `CONFIG.yaml` is a bt config (validation configs work unchanged; `days` / `day_kinds` are ignored) with an
  optional `paper:` block; flags override it.
- `rs` = feeds connected + warm-up, rounded up to the second. `--duration` sets `end`: markets ending after
  it are not admitted. Without it the run lasts until SIGINT.
- SIGINT → drain: no new admissions, finish open markets, wait for resolutions, write outputs. Second
  SIGINT → hard stop (truncated session).
- A status line every 60 s to stdout and `log.txt` (`#`): open markets, orders, fills, P&L, processing lag,
  late rows, connections.
- Refuses to start if another paper run holds the same output dir. Warns (doesn't refuse) when `bt validate`
  hasn't passed for the current `sim_version`. On macOS it suggests `caffeinate -i`.
- Refuses sealed days unless `--allow-sealed --purpose` (then records one look); see My calls.

## Performance budget

Live needs 1× real time; the replay already runs at ~144× (10 min per day for BTC+ETH 5m/15m), so the core is
not the risk. Ingest is: Python per-message cost (WebSocket frame + JSON + row write + buffer) ≈ 20–40 µs.

| Load | Messages/s (estimate) | Ingest CPU | Core events/s |
|---|---|---|---|
| 8 coins × 5m (endgame-maker) | Binance bookTicker ~5,000, aggTrade ~300, Polymarket ~200–500, RTDS ~30 | ~0.15–0.25 core | ≤ 1,000 (book tops + timers; Binance only if `on_binance`, ≤ 100 bundles/s/symbol) |
| 8 coins, all timeframes in bt (5m/15m/1h) | Polymarket ~3× | ~0.25–0.35 core | ≤ 3,000 |
| + 4h/1d | small increment | — | — |

- **Budget:** ≤ 50% of one core on an M-series Mac for 8 coins × 5m/15m/1h; event-loop lag p99 ≤ 20 ms.
  Processing lag (wall − (T + hold-back + skew)) p99 ≤ 200 ms; a backlog that grows for 60 s is logged as a
  stall (and voids, rule 3).
- **Memory:** ≤ 1.5 GB: Binance hour arrays (current + previous hour), open markets only (freed at close),
  reorder buffer ≈ hold-back × rate (~20k rows).
- **Disk:** thinned session ≈ 0.5–1.5 GB/day for 8 coins × 5m (Binance dominates; measured in task 10).
- **Measure, don't trust these numbers:** task 10 counts real rates for one hour and runs the fake feed on the
  busiest recorded hour at `max`; it must reach ≥ 5× real time through the full ingest path, and the parser
  micro-benchmark ≥ 20k messages/s on one core. If not: `orjson`, then `uvloop`, then a separate ingest
  process.

## Risks

1. **The engine refactor breaks byte-identical replay** (ordering made explicit, Exchange no longer caching
   arrays). Mitigation: the identity script runs after every step; refactor in small commits; no behaviour
   change in the same commit as a move.
2. **`GrowingMarketData` diverges from the SQL pipeline** in a corner (same-ms Down mirrors, snapshot zeroing,
   one-sided tops, `recv_eff`). Mitigation: row-by-row equivalence tests on real hours; parity on real data
   catches anything left.
3. **Hold-back too short for a laptop on Wi-Fi.** Late Polymarket rows void markets. Mitigation: measured
   rate in `results.json`; raise `--hold-back-ms` (costs nothing for paper). Mac sleep or Wi-Fi drops show up
   as gaps and voids, not as wrong results.
4. **Paper results are tied to this host's location and latency**, like backtests are to Singapore's.
   `--vs-recorder` quantifies it. A paper run on a Mac far from Polymarket says little about a server near it.
5. **Feeds unreachable from the host** (geo-restrictions on Polymarket or Binance futures WebSockets).
   Check in task 5 before building more.
6. **Polymarket message format changes**: parse errors are counted and logged (`parse_error` events);
   unknown types go to `pm_other` as the recorder does.
7. **Voided markets still move cash.** A market traded on a stale book (during a disconnect) is voided for
   P&L, but its cash and positions stayed in the budget and may have blocked or allowed other orders.
   Bounded by how rare gaps are; the report lists voided markets with orders.
8. **Gamma or Chainlink disagree** (a near tie, an RTDS gap in the TWAP window). Mismatches are reported and
   restated; repeated ones would mean the 635/635 check doesn't hold.

## My calls (the user can override)

| Decision | Reason | Cost if wrong |
|---|---|---|
| **Hold-back: the strategy clock runs `hold_back_ms + skew_ms` (3 s + 1 s) behind wall time** | Makes the live core see exactly what the replay sees, so parity is exact rather than a tolerance; paper has no real orders, so being late costs nothing | Late rows void markets if the host's feed delay often exceeds 3 s; the paper run can't test reaction speed (the live project must) |
| **Late and future-stamped rows are dropped and void their market**, not inserted | Inserting would change answers the strategy already got; dropping keeps the session = what was used | Fewer markets on a bad network |
| **One core, two drivers; live order rules = replay order rules** (made explicit, not re-invented) | The only way the same class gives the same result; a separate live engine would drift | Refactor effort; byte-identity must be proven |
| **Live driver and `GrowingMarketData` live in `bt/` (in `sim_version`); feeds, session, CLI in `polydata/paper/` (`paper_version`)** | They decide what the strategy sees; feeds only produce rows | Every live-driver tweak changes `sim_version` and needs `bt validate` |
| **Parity replay uses session mode** (admission schedule from the manifest, explicit range, post-hoc voids) | A live run can't know future markets (bt's chunk load order and pre-voiding need them); the difference to normal bt is measured by `--vs-recorder` | If normal bt and session mode differ materially, backtests and paper aren't directly comparable until fixed |
| **Voids are post-hoc and accounting-only** (bt rules on the session, plus late data, stalls, no outcome) | The strategy can't un-trade a market; post-hoc rules are a pure function of the session | Cash contamination from voided markets (risk 7) |
| **Parity tolerance is zero** on the same host | Same code, same inputs, same order | A harmless float-order bug blocks a pass until found |
| **Settlement from the Chainlink TWAP at `r_end`**, Gamma only confirms (and falls back) | User's decision; deterministic and fast | Cash is credited ~30–60 s earlier than in bt on recorder data (Gamma polling): slightly optimistic when the budget binds |
| **Record recorder formats, all messages of the subscribed sockets**, with Binance bookTicker thinned to first+last row per 10 ms bucket | `polydata build` and every view work on a session; thinning keeps everything the replay and gap rules read | A future replay with `bn_step_ms` < 10 can't use a session |
| **Subscribe Binance aggTrade too** | `bn_silences` needs it to tell a quiet book from a dead feed | ~300 more messages/s |
| **Single process, single asyncio loop**, per-coin cores inside it | Simple; the load fits one core | A slow strategy handler delays `recv_ms` stamping (visible in loop lag) |
| **Sealed days: refused by default.** `paper run` refuses to start, and stops admitting markets, when the strategy clock is in a sealed day; `--allow-sealed --purpose "..."` overrides and records one look per session (the parity replay of that session is part of it) | (Controller override of the draft, which allowed paper anywhere.) A paper P&L on a sealed day is a read of held-out data; paper still runs 2 days in 3 | Paper results arrive a third slower; markets spanning midnight into a sealed day are not admitted |
| **`PYTHONHASHSEED=0`** for paper and parity replay | Removes the one source of process-level nondeterminism Python has | None |
| **Defaults**: tick 20 ms, warm-up 60 s, drain ≤ 15 min, confirm-wait 15 min, zstd level 3, 10 s flush | Small and safe | None material |
| **`websockets` + stdlib REST (in a thread), `orjson` only if the budget needs it** | Fewest new dependencies | Slightly slower parsing |

## Tasks (for the plan)

1. **Identity harness.** `analysis/tools/replay_identity.py`: reference outputs on current `main` for the
   validation configs and the four validation strategies on 2026-11-24.
2. **Core split.** `bt/core.py` + `ReplayDriver`; explicit ordering rules (load order, settle key, ptb
   sequence); `Exchange` reads `md.trades` lazily; call tracing (off by default). Identity harness and
   `bt validate` pass.
3. **`GrowingMarketData`** + Binance/Chainlink hour buffers, with equivalence tests (synthetic + real hours).
4. **`LiveSource`**: horizons, reorder buffer, late/future-stamped handling, monotone `recv_ms`, batching
   into `Core.dispatch`, admission (`K_ADMIT`), ptb emulation, settlement via `set_resolution`; session-mode
   ReplayDriver. CI parity test on synthetic rows through a minimal row-level fake source.
5. **Feeds.** Series table (+ drift test against `config.ts`), discovery, PM CLOB, RTDS, Binance, WS client
   with recorder event names, REST with rate limiting; parser golden tests. First smoke: connect from the Mac
   for 10 minutes and print rates (checks reachability, risk 5).
6. **Session writer.** Recorder layout, thinning, `rec_events` catalog, `late/`, manifest, 10 s flushes;
   test: a written session builds and loads into the same `MarketData`.
7. **Settlement and voids.** Chainlink outcome + `pm_resolution` row, Gamma confirmation and fallback, Data
   API trades, post-hoc void rules (gaps.py applied to the session).
8. **`polydata paper run`.** Config/flags, run folder, provenance, `results.json`, status lines, drain and
   signals, sealed-day looks, runs index (`kind: "paper"`), `bt push` skipping sessions.
9. **`polydata paper replay`** (+ `--vs-recorder`) and the full fake feed (store or session, speed, fault
   injection); real-data parity test on 2026-11-24 11:00–13:00.
10. **Performance and first live run.** Rates and CPU on the Mac for 1 h of `endgame-maker-v2`; fake feed at
    `max` on the busiest hour; then a 6 h paper run on a non-sealed day and its parity check. Journal line in
    `docs/strategies/endgame-maker.md`.
11. **gaps.py source-name fix** (separate PR; changes bt results on affected days; `bt validate`; data
    quality log; journal notes for strategies whose runs change).
12. **Docs.** `docs/PAPER.md` (how to run, what the files are, parity, limitations), links from
    `BACKTESTER.md` and `CLAUDE.md` layout, `docs/TODO.md` item 3 updated.

**Minimum for a first live dry run** (plumbing, not results): 1, 2, 3, 4, 5, 6, 8, with settlement from
Chainlink only (the first half of 7). It writes a complete session, so parity (9), Gamma confirmation, voids
and the gaps.py fix (11) can be applied to that session afterwards without re-running it. Run it on a
non-sealed day.

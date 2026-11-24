# Backtester (polydata.bt) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** An honest event-replay backtester for Polymarket crypto Up/Down markets (market making first), with run records, reports, a validation suite and a screening tier.

**Architecture:** Market data for one coin is pre-indexed with DuckDB + numpy: a per-price-level
size series of the combined Up-terms book, the trades, and top-of-book changes. A Python loop then
replays only the strategy-visible events (top-of-book changes, trades, 10 ms Binance steps,
Chainlink), ordered by `recv_ms`. A per-market simulated exchange runs on exchange time (`ts_ms`).
Our resting orders sit in a queue, and fills are driven by the recorded trades. Coins replay in
parallel with separate cash budgets.

**Tech Stack:** Python 3.12, uv, DuckDB 2.0.1, numpy, pandas, pyarrow, matplotlib, PyYAML (new dependency), pytest.

**Spec:** `docs/superpowers/specs/2026-11-24-backtester-design.md`. Read it in full before your task. It is the authority; this plan records the decisions it leaves open.

## Global Constraints

- **Package layout:** code lives in `analysis/src/polydata/bt/` (and `polydata/screen.py`); tests go in `analysis/tests/bt/`.
- **Commands:**
  - Run tests with `cd analysis && uv run pytest`.
  - Every test must pass offline on synthetic data. Real-data checks go in `analysis/tests/bt/realdata/` and are skipped unless `POLYDATA_REALDATA=1`.
  - Real data: `POLYDATA_DIR=/private/tmp/claude-501/-Users-ducba--paradev-worktrees-polytrader-claude-scan-repository-mine-e4497e50/f2da7beb-7e9c-42f6-a2f6-e09637f48bd4/scratchpad/pd`. Parquet covers 2026-11-23 18:50 – 2026-11-24 ~14:45 UTC. Fetch more with `uv run polydata update --days 2026-11-24` (credentials in the repo-root `.env`; never print them).
- **The repo is public:** never commit secrets, IPs, hostnames or bucket names. `data/` is gitignored, and run folders live under it.
- **DuckDB 2.0:** use `lambda x: …` (not `x ->`), use `fetchall()[0]` (not `fetchone()`), and run `SET TimeZone='UTC'` (`polydata.connect()` already does).
- **Fees only via `polydata.fees.taker_fee(shares, avg_price, rate, exponent)`:**
  - one fee per taker order, on its average price
  - makers pay 0; rebates are 0
  - a buy pays the fee on top; a sell has it deducted from proceeds
- **No lookahead:**
  - The strategy sees an event only at its `recv_ms`.
  - The exchange matches on `ts_ms`.
  - Data before `REPLAY_START_MS = 1795516560000` (2026-11-24 10:36:00 UTC) is never replayed.
- **Prices are integers.** Inside the simulator, a price is an `int` in units of 1/1000 (`px = round(p * 1000)`), so Down↔Up mirroring (`1000 - px`) is exact. Convert back only at the strategy boundary and in output files.
- **Defaults:**

  | Setting | Default |
  |---|---|
  | `order_latency_ms` | 200 (cancels too) |
  | `fill_report_latency_ms` | 126 |
  | `cancel_model` | `"behind"` |
  | `impact_reset_ms` | 5000 |
  | `redeem_delay_s` | 60 |
  | `max_gap_ms_pm` | 5000 |
  | `max_gap_ms_bn` | 1000 |
  | `capital` | 5000 |
  | `capital_mode` | `"per_coin"` |
  | Binance step | 10 ms |
  | `final_min_days` | 5 |
  | `min_usd_per_day` | 10 |

- **Same-`recv_ms` tie order:** Polymarket (0) < Chainlink (1) < Binance (2), then file row order.
- **Speed target:** ≤ 10 min per day of data for BTC+ETH 5m/15m with one strategy, on a laptop.

## Review Focus

1. **A market with no outcome** (unresolved, or `outcome IS NULL`) is voided with a reason. It must not crash the run, and it must not settle at 0. Test in Task 6.
2. **A tick-size change mid-market:** after the change, orders off the new tick are rejected and level prices at 0.001 resolve. Tests in Tasks 3 and 5.
3. **Down-token orders at extreme prices** (0.001, 0.999) round-trip exactly through the Up-terms mapping, with no float drift in level keys. Test in Task 3.
4. **Orders resting at window end** are cancelled, their locked cash is released, and positions settle after `redeem_delay_s`. Test in Task 6.
5. **Sealed-day leakage:** a 1h market starting 23:30 on a train day must not be replayed if the next day is sealed. `polydata.research.snapshots` must exclude sealed days. Tests in Tasks 1 and 7.

---

### Task 1: Day splits and sealed-day closing in the data layer  (model: sonnet)

**Files:**
- Create: `analysis/src/polydata/bt/__init__.py`, `analysis/src/polydata/bt/splits.py`
- Modify: `analysis/src/polydata/views.py` (`_stream_view`, `create_views`, `connect`)
- Test: `analysis/tests/bt/test_splits.py`

**Interfaces:**
- Produces:
  - `splits.SPLIT_EPOCH = date(2026, 11, 25)`
  - `splits.REPLAY_START_MS = 1795516560000`
  - `splits.day_kind(d: date) -> Literal["sealed", "validation", "train", "excluded"]`:
    - before 2026-11-24 → `"excluded"`
    - 2026-11-24 → `"train"`. Replay additionally clips at `REPLAY_START_MS`.
    - otherwise by `(d - SPLIT_EPOCH).days % 3`: 0 sealed, 1 validation, 2 train
  - `splits.days_of_kind(days: Iterable[date], kinds: set[str]) -> list[date]`
  - `splits.SEALED_SQL_FILTER: str`: a SQL boolean over a `date` column that is true for **non-sealed** days. For example: `(date < DATE '2026-11-25' OR (date - DATE '2026-11-25') % 3 <> 0)`.
  - `splits.record_look(purpose: str, detail: dict) -> None`: appends a JSON line `{utc, purpose, detail, git_commit}` to `paths().root / "runs" / "looks.jsonl"`.
  - `views.connect(database=":memory:", read_only=False, include_sealed: bool = False, purpose: str | None = None, **config)`:
    - With `include_sealed=False`, every stream view and every derived view excludes sealed `date` partitions, and the `markets` view excludes markets whose `window_start` **or** `window_end` falls on a sealed day.
    - `include_sealed=True` requires a `purpose` (raise `ValueError` otherwise) and calls `record_look`. `purpose="health"` is recorded too, but later tasks don't count it as a look.
    - `create_views(con, parquet=None, include_sealed=False)` likewise.

- [ ] **Step 1: Write failing tests**
  - `test_day_kind`:
    - `day_kind(date(2026,11,25)) == "sealed"`, `(…26) == "validation"`, `(…27) == "train"`, `(…28) == "sealed"`
    - `day_kind(date(2026,11,24)) == "train"`, `day_kind(date(2026,11,23)) == "excluded"`
  - `test_connect_hides_sealed`: reuse the synthetic writer helpers from `tests/test_pipeline.py` (import them).
    - Write a `pm_trade` file on 2026-11-24/11 and another on 2026-11-25/11, and build.
    - `connect()` sees only 11-24 rows.
    - `connect(include_sealed=True, purpose="test")` sees both and appends one line to `runs/looks.jsonl`.
    - `connect(include_sealed=True)` raises `ValueError`.
  - `test_markets_crossing_into_sealed_day_hidden`: a market with `window_start` 2026-11-24 23:30 and end 2026-11-25 00:30 is absent from `markets` by default.
- [ ] **Step 2:** Run `uv run pytest tests/bt/test_splits.py -v`. Expect FAIL (import error).
- [ ] **Step 3:** Implement. The stream views add `WHERE <SEALED_SQL_FILTER>` on the hive `date` column. `markets.parquet` has no `date`, so filter it with the two window dates.
- [ ] **Step 4:** Run the new tests and the full suite (`uv run pytest`). Expect PASS. Check that `polydata.research.snapshots.build()` still uses `connect()` (sealed days are then excluded automatically).
- [ ] **Step 5:** Commit `bt: day splits; hide sealed days in the data layer`.

---

### Task 2: Gap table and voided windows  (model: sonnet)

**Files:**
- Create: `analysis/src/polydata/bt/gaps.py`
- Test: `analysis/tests/bt/test_gaps.py`

**Interfaces:**
- Consumes: `views.connect`, `paths().catalog` (daily `rec_events.csv.zst`, columns `ts,kind,source,detail`).
- Produces:
  - `gaps.gap_table(con, start_ms: int, end_ms: int) -> pandas.DataFrame` with columns `stream` (`'pm'|'bn'|'cl'`), `key` (`series` such as `btc-up-or-down-5m`, `'*'` for all PM series, a Binance `sym`, or a coin for `cl`), `from_ms`, `to_ms` and `source`. It is built from:
    1. **Recorder connection events:** `rec_events` `ws_close`/`ws_stale` → the next `ws_open` of the same `source` (`pm:<series>` → that series; `polymarket` or `polymarket-clob` → `'*'`; `binance-fut-public` → every Binance sym; `polymarket-rtds` → cl `'*'`).
    2. **Binance silences:** gaps between consecutive `bn_fut_bbo.recv_ms` per sym longer than `max_gap_ms_bn`.
    3. **Logged incidents:** a hard-coded `KNOWN_GAPS` list for data-quality-log periods that affect replay. Restart truncations end at 2026-11-24 10:12:39, so `REPLAY_START_MS` already excludes all of them; keep the list for future entries.
  - `gaps.voided_markets(markets: DataFrame, gaps: DataFrame, max_gap_ms_pm=5000, max_gap_ms_bn=1000) -> DataFrame[slug, reason]`. A market is voided when, within `[window_start_ms, window_end_ms)`:
    - a pm gap of its series (or `'*'`) longer than `max_gap_ms_pm` overlaps the window, or
    - a bn gap for its coin's `<COIN>USDT` longer than `max_gap_ms_bn` overlaps the window, or
    - `outcome IS NULL`.
  - The series name comes from `markets.series`.
  - When the catalog for a day is missing (the current day), emit one warning and rely on source 2 plus PM silences. For PM silences, a market with no PM event of any kind for > `max_gap_ms_pm` while trades for the same series in the Data API (`pm_api_trades`) occurred inside that silence counts as a gap.

- [ ] **Step 1: Failing tests** (synthetic DataFrames, no files):
  - `test_ws_close_to_open_is_gap`
  - `test_short_gap_not_voiding` (a 4,000 ms pm gap doesn't void; 6,000 ms does)
  - `test_binance_silence_voids_coin_only`
  - `test_unresolved_market_voided` (reason `"no outcome"`)
- [ ] **Step 2:** Run them and see them fail.
- [ ] **Step 3:** Implement. Put the rec_events parsing in its own function `load_rec_events(days) -> DataFrame` so the tests can pass DataFrames directly.
- [ ] **Step 4:** Tests pass. With real data, `gap_table` for 2026-11-24 10:36–14:45 must run in < 30 s, and the number of voided markets must be printed (record it in the commit message).
- [ ] **Step 5:** Commit `bt: gap table and voided windows`.

---

### Task 3: Market data and the combined book  (model: opus)

**Files:**
- Create: `analysis/src/polydata/bt/types.py`, `analysis/src/polydata/bt/marketdata.py`
- Test: `analysis/tests/bt/test_marketdata.py`, `analysis/tests/bt/realdata/test_book_reconstruction.py`

**Interfaces:**
- Produces in `types.py`:
  - `Token = Literal["Up", "Down"]`, `Side = Literal["BUY", "SELL"]`
  - `to_px(p: float) -> int` (= `round(p * 1000)`), `from_px(px: int) -> float`
  - `to_up(token: Token, side: Side, px: int) -> tuple[Side, int]`: Up keeps side and px. Down flips the side, and px becomes `1000 - px`.
  - `from_up(token: Token, up_side: Side, up_px: int) -> tuple[Side, int]`: the inverse of `to_up`.
  - `@dataclass(frozen=True) MarketInfo`, with fields:
    `slug, coin, timeframe, series, condition_id, window_start_ms, window_end_ms, price_to_beat, fee_rate, fee_exponent, tick_px (int, initial tick in px units: 10 or 1), min_order_size, outcome (str|None), resolution_recv_ms (int|None)`
  - `@dataclass(frozen=True) Top`: `recv_ms, ts_ms, bid_px, bid_size, ask_px, ask_size`, in Up terms. A missing side is `bid_px = 0` / `ask_px = 1000`, size 0.
  - `@dataclass(frozen=True) Trade`: `recv_ms, ts_ms, token, side, price (float, token terms), size, up_side, up_px`. `side` is the **taker** side on `token`.
  - `@dataclass(frozen=True) Bbo`: `recv_ms, sym, bid, ask, bid_qty, ask_qty`
  - `@dataclass(frozen=True) ChainlinkTick`: `recv_ms, ts_ms, coin, price`
- Produces in `marketdata.py`:
  - `class MarketData` for one market, holding numpy arrays:
    - **levels:** for each `(up_side, px)`, sorted arrays `ts_ms`, `recv_ms` and `size` (the new total size after each change, Up terms).
    - **trades:** arrays in ts order, plus `recv_ms`.
    - **tops:** Up top-of-book changes, in `recv_ms` order and in `ts_ms` order.
    - **ticks:** `(ts_ms, recv_ms, tick_px)` tick-size changes.

    Methods:
    - `size_at(up_side, px, t, clock="ts") -> float`
    - `book_at(t, clock="ts", depth=None) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]`: bids descending, asks ascending, Up terms, levels with size > 0.
    - `top_at(t, clock="recv") -> Top`
    - `tick_at(t, clock="ts") -> int`
    - `level_changes(up_side, px, t0, t1, clock="ts")`: arrays of the changes in `(t0, t1]`.
    - `MarketData.from_frames(info, changes: DataFrame, books: DataFrame, trades: DataFrame, ticks: DataFrame)` builds it from raw-shaped frames: the `pm_price_change` / `pm_book` / `pm_trade` / `pm_tick_size_change` view columns, plus `outcome`. Tests use it.
  - `load_coin(con, coin: str, timeframes: list[str], start_ms: int, end_ms: int, slugs: set[str] | None = None) -> dict[str, MarketData]`: one DuckDB pass per stream for all of the coin's markets in range.
- **Rules:**
  - **Build the combined book from Up-token rows only.** `pm_price_change` mirrors every change onto Down. Assert in a real-data test that ≥ 99.9% of Down rows have an Up mirror in the same `ts_ms`. If a Down row lacks its mirror, apply it mapped through `to_up`.
  - **Price-change rows** set a level to `size` (0 removes it).
  - **A `pm_book` snapshot** (Up token; or Down mapped through `to_up` when no Up snapshot has the same `ts_ms`) replaces the whole book. Every level previously > 0 and absent from the snapshot gets a size-0 entry at the snapshot's time. Do this in DuckDB with set operations, not in Python loops.
  - **Strategy-visible tops** come from the `best_bid`/`best_ask` columns of Up rows, plus the Up snapshot tops. Emit a row only when the top changed. Sizes come from the level series.
  - **Trades:** `to_up(token, side, px)` gives the taker's side and price in Up terms. A taker `BUY` in Up terms consumes asks.

- [ ] **Step 1: Failing unit tests**
  - `test_mirror_roundtrip_extremes`: for px in `[1, 10, 500, 990, 999]` and both tokens and sides, `from_up(token, *to_up(token, s, px)) == (s, px)`, and `to_up("Down","BUY",380) == ("SELL",620)`.
  - `test_level_series_and_snapshot_reset`: a snapshot with bids `0.48:100|0.47:50`, asks `0.52:80`; then change Up BUY 0.48 → 30; then a snapshot without 0.47.
    - `size_at("BUY",470,t_after_second_snapshot) == 0`
    - `book_at` between the two snapshots is `([(480,30),(470,50)], [(520,80)])`
  - `test_down_changes_ignored_when_mirrored` and `test_down_only_change_applied`
  - `test_top_changes_only_on_change`
  - `test_trade_up_terms`: a Down `SELL` at 0.38 gives `up_side="BUY", up_px=620`.
  - `test_tick_size_change`: `tick_at` before is 10, after is 1. A level at px 995 is representable.
- [ ] **Step 2:** Run them and see them fail.
- [ ] **Step 3:** Implement. Keep the per-level arrays in a dict keyed by `(side, px)`. `book_at` iterates at most 999 keys per side using `np.searchsorted`; that's fine because it's called rarely.
- [ ] **Step 4:** The unit tests pass.
- [ ] **Step 5: Real-data test** `test_book_reconstruction` (`POLYDATA_REALDATA=1`):
  - Data: BTC and SOL 5m markets, 2026-11-24 11:00–12:00.
  - Take 200 random `pm_best_bid_ask` Up rows per market.
  - The reconstructed `book_at(ts_ms, "ts")` best bid/ask must equal the row's values in ≥ 99.5% of rows for each market.
  - Expose the check as `marketdata.reconstruction_rate(con, md: MarketData, n=200, seed=0) -> float`, for reuse by `bt validate`.
  - Also time `load_coin("btc", ["5m","15m"], 1 h)` and print it. It must be ≤ 25 s per hour of data; otherwise optimise before moving on.
- [ ] **Step 6:** Commit `bt: market data loader and combined Up-terms book`.

---

### Task 4: Simulated exchange  (model: opus)

**Files:**
- Create: `analysis/src/polydata/bt/exchange.py`
- Test: `analysis/tests/bt/test_exchange.py`

**Interfaces:**
- Consumes: `MarketData`, `types.*`, `polydata.fees.taker_fee`.
- Produces:
  - `@dataclass Order`, with fields:
    - `id: int, slug, token, side, px: int, size: float, kind: Literal["limit","take"], post_only: bool`
    - `placed_ms, arrival_ms`
    - `status: Literal["pending","open","filled","cancelled","rejected"]`
    - `filled: float = 0, cost: float = 0` (USDC excluding fees), `fee: float = 0`
    - `queue_ahead: float = 0, reason: str = ""`
    - derived: `up_side`, `up_px`
  - `@dataclass(frozen=True) Fill`: `order_id, ts_ms, px (token terms), size, fee, queue_ahead_at_fill, liquidity: Literal["maker","taker"], trade_ts_ms (supporting trade, None for takers)`
  - `@dataclass(frozen=True) ExEvent`: `ts_ms, order_id, kind: Literal["accepted","rejected","cancelled","fill","expired"], fill: Fill|None, reason: str`. This is exchange-time output; the engine adds the report delay.
  - `class Exchange(md: MarketData, cfg: ExchangeConfig)`, with methods:
    - `submit(order)`
    - `request_cancel(order_id, arrival_ms)`
    - `advance(t_ms) -> list[ExEvent]`: processes order arrivals, cancel arrivals and recorded trades with `ts_ms ≤ t_ms` in time order. At equal ms the order is recorded trades, then our cancels, then our arrivals (pessimistic).
    - `close(t_ms) -> list[ExEvent]`: expires all open orders at window end.
  - `@dataclass(frozen=True) ExchangeConfig`: `cancel_model: Literal["behind","proportional"]="behind", impact_reset_ms: int = 5000`
  - **Pre-trade checks** live in the engine (Task 6). The exchange itself rejects:
    - off-tick (`px % tick_at(arrival) != 0`)
    - `size < min_order_size`
    - `px` outside `[tick, 1000 - tick]`
    - arrival after `window_end_ms`
    - post-only that would cross the book at arrival (reason `"would cross"`)
- **Fill rules** (spec section "Queue and fill model"):
  - **Join:** `queue_ahead = size_at(up_side, up_px, arrival_ms)`.
  - **Each recorded trade, in Up terms** (taker `T`, price `P`, size `S`), against each open order of ours on the opposite side to `T`, in our queue order (earlier arrival first). A taker BUY hits our resting SELLs. Let `x = remaining size of the trade`:
    - If our px is strictly better than `P` for the taker (our SELL px < `P`, or our BUY px > `P`), it's a trade-through: fill our full remainder. `x` is unchanged (no self-impact).
    - If our px == `P`, consume `ahead` first, `d = min(ahead, x)`, then fill `min(remaining, x − d)`.
  - **Cancels by others:** with `"behind"` they're ignored. With `"proportional"`, before applying each trade at our level, read `level_changes` since the last check. For each size decrease not explained by trade volume at that level in the same `ts_ms`, set `ahead -= decrease × ahead / level_size_before`.
  - **Taker orders** at arrival:
    - Walk `book_at(arrival)` on the opposite side up to the limit, minus the `impact` already consumed at each level. Impact is keyed by `(up_side, px)`; it resets when the recorded level size reaches 0 after we took, or after `impact_reset_ms`.
    - Fee = `taker_fee(filled, avg_token_price, md.info.fee_rate, md.info.fee_exponent)`.
    - The remainder is cancelled (IOC).
    - A non-post-only limit that crosses executes the crossing part as a taker and rests the rest.
  - Fill prices are in **token terms**. For Down: `from_up`.

- [ ] **Step 1: Failing tests** (synthetic `MarketData.from_frames`):
  - `test_join_back_of_queue`: the level has 100 at our bid 0.48. A taker SELL of 60 at 0.48 → no fill, ahead 40. Then a taker SELL of 50 at 0.48 → we fill 10.
  - `test_trade_through_fills_fully`: our bid 0.48 with ahead 100. A taker SELL at 0.47 → full fill.
  - `test_down_order_filled_by_up_trade`: our BUY Down 0.38 (= SELL Up 0.62) with ahead 0. A taker BUY Up 0.62, size 10 → our fill is in token terms: Down px 0.38, size min(10, ours).
  - `test_cancel_model_behind_vs_proportional`: the level 100 → 50 with no trades, then a taker trade of 30 at our level. Behind: no fill, ahead 70. Proportional: ahead 50 → after the trade 20, no fill.
  - `test_taker_walks_and_one_fee`: asks 0.52:10, 0.53:10; take BUY 15 → avg 0.52333…, fee `taker_fee(15, avg, 0.05)` exactly.
  - `test_taker_impact_persists_then_resets`: take all 10 at 0.52; a second take within 5 s sees 0.53 as best; after `impact_reset_ms` it sees 0.52 again (if the recording still has it).
  - `test_rejections`: off-tick (0.485 at tick 0.01), size 4, px 0, after window end, post-only crossing, each with its reason.
  - `test_ordering_equal_ms_pessimistic`: a cancel arriving at the same ms as a trade-through still fills.
- [ ] **Step 2:** Run them and see them fail.
- [ ] **Step 3:** Implement `Exchange`. Iterate trades with a cursor over the md trade arrays; only markets with open orders need trade processing.
- [ ] **Step 4:** Tests pass.
- [ ] **Step 5:** Commit `bt: simulated exchange (queue, trade-through, takers, impact)`.

---

### Task 5: Strategy API, portfolio and the replay engine  (model: opus)

**Files:**
- Create: `analysis/src/polydata/bt/strategy.py`, `analysis/src/polydata/bt/portfolio.py`, `analysis/src/polydata/bt/engine.py`, `analysis/src/polydata/bt/config.py`
- Test: `analysis/tests/bt/test_portfolio.py`, `analysis/tests/bt/test_engine.py`

**Interfaces:**
- Consumes: Tasks 1–4.
- Produces:
  - `config.RunConfig` (dataclass, loadable from a YAML dict through `RunConfig.from_dict`, saved through `to_dict`), with fields:
    - `strategy: str` (a registry name), `params: dict`
    - `coins: list[str]` (default all 8: btc eth sol xrp bnb doge hype zec), `timeframes` (default `["5m","15m","1h"]`)
    - `days: list[str] | None` (ISO dates; None = all available non-sealed days), `day_kinds: list[str] = ["train","validation"]`
    - `order_latency_ms=200, latency_jitter_ms=0, fill_report_latency_ms=126`
    - `cancel_model="behind", impact_reset_ms=5000`
    - `capital=5000.0, capital_mode="per_coin"`
    - `redeem_delay_s=60, max_gap_ms_pm=5000, max_gap_ms_bn=1000, bn_step_ms=10`
    - `seed=0, tags: list[str] = []`
  - `strategy.Strategy` base class: attributes `name`, `family`, `params`, and every handler from the spec's Strategy interface (default no-op). `strategy.REGISTRY: dict[str, type[Strategy]]`, filled by the `@register` decorator.
  - `strategy.Ctx` protocol, implemented by the engine (`engine.SimCtx`) and later by the live runner:
    - `now`
    - `place(slug, token, side, price: float, size, post_only=True) -> int`
    - `take(slug, token, side, size, limit_price: float) -> int`
    - `cancel(order_id)`, `cancel_all(slug=None)`
    - `book(slug, depth=5) -> BookView`: delayed (`clock="recv"`), Up terms. Methods: `.bids`, `.asks` (lists of `(price float, size)`), `.best_bid`, `.best_ask`, `.mid`, `.spread`, `.for_token(token)`.
    - `top(slug) -> Top`
    - `position(slug) -> tuple[float, float]` (Up, Down shares)
    - `cash(slug=None) -> float` (free cash of that coin's budget)
    - `orders(slug=None) -> list[Order]` (the strategy's view: open as far as it knows)
    - `set_timer(name, ms)`
    - `markets() -> list[MarketInfo]` (open markets)
    - `fair_value(slug) -> float | None`
    - `binance(sym) -> Bbo | None`, `chainlink(coin) -> ChainlinkTick | None`
    - `log(*args)`
  - `portfolio.Portfolio(budgets: dict[str, float])`, per coin (`"*"` in global mode):
    - `free_cash(coin)`
    - `reserve_buy(order) -> bool` (locks `px × size`; takes also lock a fee bound of `taker_fee(size, limit, rate)`)
    - `reserve_sell(order) -> bool` (locks shares)
    - `release(order)`
    - `apply_fill(order, fill)`
    - `merge(slug)`: equal Up+Down → $1 each, instantly, after every fill
    - `settle(slug, outcome, t_ms)`: winning shares pay $1, credited to free cash at `resolution_recv_ms + redeem_delay_s*1000`
    - `positions`, `equity(marks)`
  - `engine.run_coin(cfg: RunConfig, coin: str, con_factory) -> CoinResult`, with `CoinResult` = `orders: DataFrame`, `fills: DataFrame`, `equity: DataFrame`, `markets: DataFrame` (slug, voided, reason, pnl, settle_ms, outcome) and `log: list[str]`.
  - `engine.run(cfg) -> RunResult`: coins in a `multiprocessing` pool with per-coin budgets `capital / len(coins)`. Global mode runs one process.
- **Engine rules:**
  - **Replay range:** `max(day start, REPLAY_START_MS)` → day end, for each selected day. A market is replayed only if its whole window lies in selected days and it isn't voided (Task 2).
  - **Strategy events:** merge Polymarket tops/trades (recv order), Binance bbo bundled to the last update per `bn_step_ms` bucket (recv time = bucket end), and Chainlink (`chainlink` view, deduplicated on `ts_ms`, first `recv_ms`). Combine them into one sorted structured numpy array (`np.lexsort` by `(row, stream_rank, recv_ms)`). Internal events (order reports, timers, market open/close, redemptions) go into a `heapq`, and at equal ms they come after data events.
  - **Before delivering any event at time T:** call `exchange.advance(T)` for every market with live orders. Turn `ExEvent`s into strategy `on_order` calls at `ts_ms + fill_report_latency_ms`.
  - **Markets:**
    - `on_market_open` fires at the market's first top event (≥ replay start).
    - `on_market_close` fires at `window_end_ms`; the exchange `close()` runs then, and no orders are accepted after it.
    - Settlement follows the resolution.
  - **Latency:** `ctx.place` and `ctx.cancel` arrive at `now + order_latency_ms + U(0, latency_jitter_ms)` (seeded RNG).
  - **Equity samples:** every 1 s of replay time while positions exist. Marks are Up mid (Down = 1 − mid).
  - **`fair_value`:** port `research.calibration.fair_value` to a scalar function over online state:
    - the last Chainlink price
    - the Binance mid now and at the Chainlink `ts_ms`
    - `sig15` from 1 s Binance mids over 15 min
    - the observed settlement average
    - k = 1.2 (the study's best). Returns `None` until 15 min of Binance history exists.
- [ ] **Step 1: Failing tests**:
  - `test_portfolio_locks_and_merge`: buy Up 10 @ 0.40 and Down 10 @ 0.55 → merge gives cash +10, positions 0, P&L = 10 − 9.5 = +0.5.
  - `test_reject_insufficient_cash`
  - `test_sell_unheld_rejected`
  - `test_settlement_after_redeem_delay`
  - `test_resting_order_cancelled_at_window_end_releases_cash`
  - `test_engine_no_lookahead`: a strategy that records `ctx.now` and `ctx.top()` on every event. Assert every `top.recv_ms <= now`, and that fills are reported no earlier than `fill.ts_ms + 126`.
  - `test_engine_ordering_deterministic`: two runs give identical event logs.
  - `test_market_crossing_into_sealed_day_not_replayed` (Review Focus 5)
  - `test_unresolved_market_voided_not_settled_at_zero` (Review Focus 1)
- [ ] **Step 2:** Run them and see them fail.
- [ ] **Step 3:** Implement `config.py`, `portfolio.py`, `strategy.py` and `engine.py`. Synthetic engine tests build `MarketData` with `from_frames` and inject it through a `loader` argument to `run_coin` (default: `marketdata.load_coin`).
- [ ] **Step 4:** Tests pass.
- [ ] **Step 5: Real-data smoke check:** a `noop` run on BTC 5m for 1 h prints the elapsed time.
- [ ] **Step 6:** Commit `bt: strategy API, portfolio and replay engine`.

---

### Task 6: Sanity strategies and the validation suite  (model: opus)

**Files:**
- Create: `analysis/src/polydata/bt/strategies/__init__.py`, `.../strategies/sanity.py` (`noop`, `buy-and-hold-up`, `always-cross`, `join-best`), `analysis/src/polydata/bt/validate.py`
- Test: `analysis/tests/bt/test_sanity.py`, `analysis/tests/bt/realdata/test_validate.py`

**Interfaces:**
- Produces:
  - **The strategies** (family `sanity`):
    - `noop`
    - `buy-and-hold-up`: at market open, `take` Up for `min_order_size` shares with limit 0.99; hold to resolution.
    - `always-cross`: at open, take-buy Up 5 shares; when filled, take-sell 5 at limit 0.01.
    - `join-best`: keep a 5-share bid at the Up best bid and a 5-share ask (sell Up if held, else buy Down at 1 − best ask). Never cancel except to rejoin when the best moves away by ≥ 1 tick. Stop at 30 s before the end.
  - `validate.run_suite(days: list[str], coins=("btc","sol"), timeframes=("5m",)) -> dict`. It runs the checks below, writes `data/runs/validation/<sim_version>.json` with `{sim_version, passed, checks: {...}}` and returns it.
    1. **Book reconstruction:** `reconstruction_rate` ≥ 0.995 for every non-voided market.
    2. **Sanity strategies:**
       - noop P&L == 0.0 exactly
       - `buy-and-hold-up` per-market P&L equals an independent DuckDB computation to the cent:
         - Use the Up best ask as of `ts_ms ≤ open_recv + 200` from the `pm_price_change` Up rows, with ask size ≥ 5 (skip the market otherwise, in both).
         - P&L = `5*(outcome_up − ask) − taker_fee(5, ask, rate)`.
       - `always-cross` loses `(ask_t1 − bid_t2)*5 + fee1 + fee2` to the cent, computed independently the same way.
    3. **Fill support:**
       - Every maker fill in `join-best` has supporting trades: from `orders.parquet`/fills, recompute ahead and fill from the raw `pm_trade` rows with independent code (DuckDB SQL, not the exchange class). They must match.
       - `join-best` ¢/share < the average maker ¢/share on the same markets (computed from `pm_trade` marked to outcome).
    4. **Determinism:** run `join-best` twice; the `results.json` bytes are identical.
  - `validate.sim_version() -> str`: the sha256 of the contents of every `polydata/bt/*.py` except `strategies/`, `report.py`, `runs.py` and `cli.py`, truncated to 12 hex characters.
  - `validate.is_validated(sim_version) -> bool`
- [ ] **Step 1: Synthetic failing tests:**
  - noop P&L 0
  - buy-and-hold-up on a synthetic market equals the hand-computed `5*(1-0.52) - taker_fee(5,0.52,0.05)`
  - always-cross equals the hand-computed loss
- [ ] **Step 2:** Run them and see them fail.
- [ ] **Step 3:** Implement the strategies and `validate.py`.
- [ ] **Step 4:** Synthetic tests pass. With `POLYDATA_REALDATA=1`, `run_suite(["2026-11-24"])` passes. **If a check fails, stop and report the numbers. Do not loosen a threshold.**
- [ ] **Step 5:** Commit `bt: sanity strategies and validation suite`.

---

### Task 7: Run folders, versions, index, CLI and journals  (model: sonnet)

**Files:**
- Create: `analysis/src/polydata/bt/runs.py`, `analysis/src/polydata/bt/cli.py`
- Create: `docs/strategies/README.md`, `docs/strategies/martingale-streak-reversal.md`, `docs/strategies/mm-binance-pull.md`, `docs/runs-index.csv` (header only)
- Modify: `analysis/src/polydata/cli.py` (add `bt` subcommand group), `analysis/pyproject.toml` (add `pyyaml`)
- Test: `analysis/tests/bt/test_runs.py`

**Interfaces:**
- Consumes: `engine.run`, `validate.sim_version` / `is_validated`, `splits`, `sync` (`spaces_client`, `bucket`).
- Produces:
  - `runs.strategy_version(strategy_cls, params) -> str`: the sha256 of the strategy's source file bytes plus `json.dumps(params, sort_keys=True)`, truncated to 12 hex characters.
  - `runs.data_fingerprint(days) -> str`: the sha256 of the sorted `(raw path, size)` list for those days, from `.src` manifests or the raw dir.
  - `runs.new_run_dir(family, strategy, now_utc) -> Path`: `data/runs/<family>/<strategy>/<YYYY-MM-DDTHHMM>-<6 hex>/`.
  - `runs.write_run(dir, cfg, result: RunResult, metrics: dict, provenance: dict)` writes:
    - `config.yaml`
    - `provenance.json` (git commit, `git diff` if dirty, sim_version, strategy_version, data_fingerprint, python/package versions, host = `platform.node()` hashed to 8 hex, never raw)
    - `results.json` (deterministic: `sort_keys`, no timestamps or paths)
    - `orders.parquet`, `equity.parquet`, `log.txt`
  - `runs.rebuild_index() -> DataFrame`: scans `data/runs/**/results.json`, writes `data/runs/index.parquet` and `docs/runs-index.csv`. Columns:
    - `run_id, created_utc, family, strategy, version, kind (run|final|sweep|screen), days, n_days, coins, timeframes, order_latency_ms, pnl_usd, usd_per_day, t_block, verdict, tags, path` (path relative to `data/runs`)
  - `runs.final_looks() -> int`: the count of `kind == "final"` runs plus non-`health` lines in `looks.jsonl`.
  - **CLI** (`polydata bt …`):
    - `run CONFIG.yaml [--days …] [--coins …] [--set key=value …] [--final] [--latency-sweep]`
    - `sweep CONFIG.yaml --grid param=v1,v2 [--grid …] [--jobs N]`
    - `validate [--days …]`
    - `runs [--strategy] [--family] [--tag] [--search TEXT] [--passed]`
    - `push`, `pull` (sync `data/runs/` ↔ `runs/` in the bucket, then `rebuild_index`)
  - **`--final` rules:**
    - refused unless `validate.is_validated(sim_version())`
    - refused unless ≥ `final_min_days` sealed days exist
    - refused if the index already has a final for the same `(strategy_version, sim_version, data_fingerprint)`
    - uses `connect(include_sealed=True, purpose="final")`, day_kinds `["sealed"]`, and the latency sweep 100/200/400
    - the report lists earlier finals of that version
- [ ] **Step 1: Failing tests** (`tmp_path` as `POLYDATA_DIR`; a fake `RunResult`):
  - `test_version_changes_with_params`
  - `test_run_dir_name_format`
  - `test_results_json_deterministic`
  - `test_index_rebuild_and_csv`
  - `test_final_refused_without_validation`
  - `test_final_refused_when_repeated`
  - `test_final_looks_counts_looks_jsonl_excluding_health`
- [ ] **Step 2:** Run them and see them fail.
- [ ] **Step 3:** Implement. Write the docs:
  - `docs/strategies/README.md`: the family glossary from the spec (one line each, plus `sanity`), how to search runs, and the journal template.
  - `martingale-streak-reversal.md`: status abandoned. Hypothesis: double the stake after losses betting on streak reversal. Rejected by simulation in the first session; the outcomes are close to independent, so it has no edge and a ruin risk.
  - `mm-binance-pull.md`: status testing.
- [ ] **Step 4:** Tests pass. `uv run polydata bt run` on a sanity config works end to end and creates a run folder.
- [ ] **Step 5:** Commit `bt: run records, index, CLI, strategy journals`.

---

### Task 8: Metrics, pass criteria and the HTML report  (model: sonnet)

**Files:**
- Create: `analysis/src/polydata/bt/metrics.py`, `analysis/src/polydata/bt/report.py`
- Modify: `analysis/src/polydata/bt/runs.py` / `cli.py` (call them)
- Test: `analysis/tests/bt/test_metrics.py`

**Interfaces:**
- Produces:
  - `metrics.compute(result: RunResult, cfg: RunConfig, day_kind_of: Callable[[str], str]) -> dict`, with keys:
    - `pnl_usd, usd_per_day, return_on_capital`
    - `days: {date: {kind, pnl}}`
    - `block_pnl_t, block_ci95: [lo, hi]` (15-minute blocks by market `window_start_ms // 900000`; per-market P&L summed per block; 2000 bootstrap resamples over blocks with seed 0; reuse the shape of `research.calibration.block_bootstrap`)
    - `max_drawdown_usd, max_drawdown_pct`
    - `fills, fills_per_day, cents_per_share`
    - `attribution: {spread, adverse, inventory, fees}`. For each maker fill with direction `dir` (+1 buy, −1 sell, in token terms): spread = `(mid_fill − px)·dir·size`, adverse = `(mid_10s − mid_fill)·dir·size`, inventory = `(settle − mid_10s)·dir·size`; fees = −Σfee. **The four sum to `pnl_usd` within $0.01** (assert in a test). Mids are token-terms mids from the recorded tops at `ts_ms`.
    - `realism: {fill_rate, share_fills_1s_after_bn_move, avg_queue_ahead, cancel_model, order_vs_level_size_p50, voided_windows, voided_share}`. A Binance move means |Δmid| > 2σ (1 s) within the second before the fill.
    - `by_market: list`, `by_coin_timeframe: list`, `per_day_edge: list`
  - `metrics.verdict(m: dict, kind: "provisional"|"final", n_looks: int) -> dict(passed: bool, checks: {name: {value, threshold, passed}})`. Checks (spec "Pass criteria"):
    - `pnl > 0`
    - `ci95_lo > 0`
    - `t > max(3, z(1-0.05/N))`, with `N = max(n_looks,1)` only when final; else 3
    - recent third of days P&L > 0
    - `usd_per_day ≥ 10`
    - positive-day share ≥ 0.7 only if `n_days ≥ 7`, else `"not judged"`
    - `max_drawdown_pct < 10`
    - profitable at 400 ms (only when a latency sweep exists, else `"not judged"`)
    - `fills_per_day ≥ 200`

    The provisional verdict uses validation days only. With no validation days, the verdict is `"no validation days yet"` and `passed=False`.
  - `report.render(run_dir: Path, metrics: dict, verdict: dict, cfg, extras: dict) -> Path`: a self-contained `report.html` with matplotlib PNGs as base64 and no external requests. Layout:
    - header: strategy, version, verdict, **"Fill model: unverified"**, and for finals N and the threshold
    - then: equity (train vs validation shaded), daily P&L, drawdown, edge per day, attribution bar, by-market table (top/bottom 10), realism table, latency sensitivity table (if present), robustness grid (if the run belongs to a sweep), config and provenance at the bottom
- [ ] **Step 1: Failing tests:**
  - `test_attribution_sums_to_pnl`
  - `test_bonferroni_threshold`: N=1 → 3; N=100 → 3.29 ±0.01; N=1000 → 3.89 ±0.01
  - `test_positive_days_not_judged_below_7`
  - `test_no_validation_days_verdict`
  - `test_report_is_self_contained`: no `http` in `src=` or `href=`
- [ ] **Step 2:** Run them and see them fail.
- [ ] **Step 3:** Implement, and wire it into `bt run`, which writes `results.json` with the metrics and verdict.
- [ ] **Step 4:** Tests pass. Render a real `join-best` run report and open it in a browser to check it reads well.
- [ ] **Step 5:** Commit `bt: metrics, pass criteria, HTML report`.

---

### Task 9: Baseline strategy `mm-binance-pull`  (model: opus)

**Files:**
- Create: `analysis/src/polydata/bt/strategies/mm_binance_pull.py`, `analysis/configs/mm-binance-pull.yaml`
- Test: `analysis/tests/bt/test_mm_binance_pull.py`

**Interfaces:**
- Consumes: `Strategy`, `Ctx`.
- Params (defaults in the YAML):

  | Param | Default |
  |---|---|
  | `half_spread` | 0.01 |
  | `size` | 10 |
  | `max_book_spread` | 0.04 |
  | `t_min_s` | 30 |
  | `t_max_s` | 3600 |
  | `pull_k` | 2.0 |
  | `pull_window_ms` | 1000 |
  | `cooldown_ms` | 2000 |
  | `skew_per_share` | 0.0005 |
  | `max_inventory` | 50 |
  | `stop_before_end_s` | 20 |
  | `center` | `"mid"` (or `"fair"`) |
  | `requote_threshold_ticks` | 1 |

- **Behaviour:**
  - The quote centre is the Up mid (or `ctx.fair_value`). Bid = centre − half_spread − skew·inventory and ask = centre + half_spread − skew·inventory, rounded outward to the tick and clipped inside the book (post-only).
  - Asks are placed as SELL Up when Up is held, otherwise as BUY Down at 1 − ask.
  - Quote only when book spread ≤ `max_book_spread` and time left is in `[max(t_min_s, stop_before_end_s), t_max_s]`.
  - **Pull:** on each Binance step, if |log mid change over `pull_window_ms`| > `pull_k × σ₁ₛ × sqrt(pull_window_ms/1000)` (σ₁ₛ = the 15-min realized 1 s vol), cancel all quotes on that coin's markets and pause for `cooldown_ms`.
  - Requote when the target moves ≥ `requote_threshold_ticks`.
  - Inventory cap per market: stop quoting the side that would increase |net Up − Down| beyond `max_inventory`.
- [ ] **Step 1: Failing synthetic tests:**
  - `test_quotes_inside_tight_book`
  - `test_no_quotes_in_wide_book`
  - `test_pull_on_binance_jump_then_requote_after_cooldown`
  - `test_inventory_cap_stops_one_side`
  - `test_stops_before_end`
- [ ] **Step 2:** Run them and see them fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Tests pass.
- [ ] **Step 5: Real-data speed check:** run BTC+ETH 5m/15m over the available data. Elapsed time per data-day must be ≤ 10 min (scale linearly). If it's slower, profile (`python -m cProfile`) and optimise the engine or loader hot spots before committing; record the timing in the commit message.
- [ ] **Step 6:** Commit `bt: mm-binance-pull baseline`.

---

### Task 10: Screening tier  (model: sonnet)

**Files:**
- Create: `analysis/src/polydata/screen.py`
- Modify: `analysis/src/polydata/bt/cli.py` (`polydata bt screen NAME`)
- Test: `analysis/tests/bt/test_screen.py`

**Interfaces:**
- Consumes: `research.snapshots` (now with sealed days excluded), `splits`, `gaps.voided_markets`, `fees.taker_fee`, `runs` (index entries of kind `screen`).
- Produces:
  - `screen.Screen` = `(name, family, rule: Callable[[DataFrame], DataFrame])`. The rule returns rows `slug, t_ms, token, size?` meaning "buy this token now".
  - `screen.evaluate(snap: DataFrame, signals: DataFrame, latency_ms=300, exit="hold"|"bid") -> dict`:
    - Fill at the ask as of `t_ms + latency` (`up_ask_300` / `up_ask_1000` columns, or an as-of join on `pm_best_bid_ask`), size limited to the visible size, plus `taker_fee`.
    - Exit at resolution or at the bid.
    - Output: ¢/share, $/day, and a 15-minute block-bootstrap CI, by coin × timeframe and by day kind.
    - Voided windows are skipped.
  - `screen.run(name, …)` records a run folder (kind `screen`, verdict `reject|promote`: promote iff the CI lower bound > 0 on validation days).
  - **Built-in example screen `endgame-favourite`:** buy the favourite when ask ∈ [0.80, 0.99], τ ≤ 60 s and spread ≤ 0.10.
- [ ] **Step 1: Failing tests:**
  - `test_fill_uses_latency_ask_and_fee`
  - `test_voided_skipped`
  - `test_promote_requires_ci_above_zero_on_validation`
- [ ] **Step 2:** Run them and see them fail.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Tests pass. `polydata bt screen endgame-favourite` runs on real data in < 60 s.
- [ ] **Step 5:** Commit `screen: pessimistic taker screening tier`.

---

### Task 11: Docs and end-to-end verification  (model: sonnet for docs; the verification is a separate agent)

**Files:**
- Modify: `README.md` (backtester section), `analysis/README.md` (`polydata bt` usage), `CLAUDE.md` (rules: sealed days, `--final` only after `bt validate`, strategy journals updated with every run that matters, run folders under `data/runs`), `docs/TODO.md` (mark backtester items done)
- Create: `docs/BACKTESTER.md`: how to write a strategy, the config reference, the fill model in plain words, and how to read a report.

- [ ] **Step 1:** Write the docs.
- [ ] **Step 2: Verification agent** (independent, not the implementer), on real data:
  - `uv run pytest`
  - `POLYDATA_REALDATA=1 uv run pytest tests/bt/realdata`
  - `polydata bt validate --days 2026-11-24`
  - an `mm-binance-pull` run on all coins, with its timing
  - a lookahead audit: read `engine.py` and `exchange.py` looking for any use of data at a time later than the strategy's `now` (or the exchange's `t`), and try to break the fill model with adversarial synthetic cases
  - it reports findings with evidence
- [ ] **Step 3:** Fix the findings (send them back to the implementer), then rerun the verification.
- [ ] **Step 4:** Commit, open a PR to `main`, merge it (standing permission), and update `docs/strategies/mm-binance-pull.md` with the first results.

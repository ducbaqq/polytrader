# Paper Runner (polydata.paper) Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Run a registered `Strategy` class unchanged on live public market data, with fills from the backtester's own simulator. Record every input in recorder formats, and prove parity: replaying the session through `polydata.bt` reproduces every decision and fill exactly.

**Architecture:** `bt/engine.py`'s `_Replay` splits into a `Core` (scheduling, orders, exchange, settlement, strategy queries, the hot dispatch loop) and two drivers. `ReplayDriver` loads Parquet as today. `LiveSource`/`LiveRouter` (`bt/live.py`) feed rows that grow incrementally (`bt/marketdata_live.py`) under two horizons, so the live core sees exactly what a replay sees. Feeds, the session writer, settlement and the CLI live in `polydata/paper/` and only produce rows.

**Tech Stack:** Python 3.12, uv, DuckDB 2.0, numpy, pandas, pyarrow, zstandard, `websockets` (new dependency), stdlib `urllib` for REST (run in a thread), asyncio, pytest.

**Spec:** `docs/superpowers/specs/2026-11-25-paper-runner-design.md`. Read it in full, and the backtester spec it extends (`docs/superpowers/specs/2026-11-24-backtester-design.md`), before your task. The spec is the authority; this plan records the decisions it leaves open. Where they differ, the spec wins unless the plan names the difference under "Decisions beyond the spec".

## Global Constraints

- **The repo is PUBLIC.** Never commit secrets, IPs, hostnames, bucket names or credentials, not even in test fixtures, docs or log lines. Host identity is `runs.host_hash()` only. `data/` (run folders, sessions, identity references) is gitignored.
- **No real orders, ever.** Nothing in this plan authenticates, signs, holds keys or wallets, or calls an order endpoint. Feeds use public WebSocket and REST endpoints only.
- **Sealed days are never read.** 2026-11-25 (today) is sealed (every third day from 2026-11-25). Tests use 2026-11-24 data only. `paper run` refuses sealed strategy-clock days unless `--allow-sealed --purpose` is given (spec "My calls"). Never pass `include_sealed=True` outside the session-replay path the spec allows.
- **Test data:** offline tests use synthetic data. Real-data tests live under `analysis/tests/**/realdata/` and are skipped unless `POLYDATA_REALDATA=1`. Run them with `POLYDATA_DIR=/private/tmp/claude-501/-Users-ducba--paradev-worktrees-polytrader-claude-scan-repository-mine-e4497e50/f2da7beb-7e9c-42f6-a2f6-e09637f48bd4/scratchpad/pd` (below: `$PD`). It holds raw + Parquet for 2026-11-23 18:50 to 2026-11-24 ~14:45 UTC.
- **Strategies use only `ctx`** and handler arguments: no clocks, files, DuckDB or global state. The paper runner must not give them anything else.
- **Byte-identical replay.** After every commit that touches a `sim_version` file (`bt/*.py` except `report.py`, `runs.py`, `cli.py`; `fees.py`, `views.py`, `build.py`, `schema.py`, `bt/strategies/sanity.py`), run the identity harness (Task 1) against the reference saved in Task 1. Any difference blocks the commit. Moves and behaviour changes go in separate commits.
- **Suite:** `cd analysis && uv run pytest -q` is green after every task.
- **Validation:** `cd analysis && POLYDATA_DIR=$PD uv run polydata bt validate` passes at the final `sim_version` before the plan is complete (Final verification). Task 13 is a separate PR with its own validate.
- **DuckDB 2.0 idioms:** `lambda x: …`, `fetchall()[0]`, `SET TimeZone='UTC'`.
- **Prices are integer px** (`round(float(text) * 1000)`) inside the simulator. An empty `best_bid` maps to 0 and an empty `best_ask` to 1000, as `marketdata._BID`/`_ASK` do.
- **Paper defaults** (spec): `hold_back_ms` 3000, `skew_ms` 1000, tick 20 ms, warm-up 60 s, `--max-drain` 15 min, `--confirm-wait` 15 min, zstd level 3, flush every 10 s, Binance thinning at 10 ms. Require `skew_ms + fill_report_latency_ms ≥ trade_explain_ms` (150), because `Exchange._explained` looks 150 ms ahead on the ts clock.
- **`PYTHONHASHSEED=0`** for `paper run` and `paper replay`: the CLI re-execs itself if it is unset.
- **Model tiers** per task: *cheap* (mechanical, fully specified), *standard* (integration and judgment within one area), *most capable* (simulation semantics where an ordering slip silently breaks parity).

## Decisions beyond the spec (ambiguities resolved here)

1. **ptb reveal key.** The spec's `s* = max(rs, floor_hour(max(r, window_start)))` does not cover a market admitted after its reveal tick. The plan uses `s* = max(rs, floor_hour(max(r, ws, load_ms)))` and push time `max(r, s*, load_ms)`. In normal replay, `load_ms ≤ ws`, so this is today's rule. If `s*`'s hour has not been marked yet, the push is deferred until `mark_hour(s*)` (Task 3).
2. **Heap tie keys.** Heap entries become `(t, prio, k, tie, payload)`. Ordinary events have `k = seq`, `tie = 0`. A ptb reveal has `k = hour_seq[s*] + 0.5`, `tie = load_index`. A settle has `k = window_end_ms * 2**20 + load_index`, `tie = 0`.
3. **Unresolved markets at the end.** After the drain, `Core.finish_unresolved()` pushes each closed, unresolved market's settle at `close_t + R`, as the replay does. `_settle` then voids it with "no outcome at settlement". ctx.now may step back for this one non-strategy event; nothing strategy-visible happens.
4. **Binance 1 s grid live.** `_Grid.append`'s arithmetic is factored into a pure `_grid_segment(...)`. `LiveGrid` recomputes the current segment's prefix with it each completed second, so values are bit-identical (a prefix of `np.cumsum` is the cumsum of the prefix).
5. **The late rule covers** `pm_book`, `pm_price_change`, `pm_best_bid_ask`, `pm_trade` and `pm_other` messages with `event_type == "tick_size_change"`. Other `pm_other`/`pm_new_market` messages are never late.
6. **Market info parsing.** `views.MARKETS_SQL` becomes `views.markets_sql(meta_rel, res_rel)`. `MARKETS_SQL` stays as the Parquet instance with identical text semantics. The live path runs it over in-memory relations.
7. **Fake feed and recorded `pm_resolution` rows.** In a store-fed fake run, recorder `pm_resolution` rows (Gamma answers) are delivered as Gamma confirmation answers, not as `pm_resolution` rows. The Chainlink settler still decides outcomes.
8. **Live md row window.** `GrowingMarketData` ignores Polymarket rows with `ts_ms < window_start − 2·len − MARGIN_MS`, the per-market lower bound `load_coin` applies. Rows after the close are never queried.
9. **Task split.** The spec's task 2 (core split) is split into a pure move (Task 2) and the explicit ordering rules plus entry points (Task 3). The minimum dry-run set is Tasks 1–8; Tasks 9–12 follow; Task 13 (gaps.py) is a separate PR.

## Review Focus

1. **A Down `pm_price_change` received before its Up mirror** (same `ts_ms`, different message, later `recv_ms`). The live book must equal the SQL book: the mirror check runs once per complete `ts_ms`. Test in Task 4 (`test_down_mirror_split_across_messages`).
2. **A Polymarket clock ahead of ours.** A row with `ts_ms ≤ recv_ms + skew_ms` is used. One beyond the skew is "future-stamped": dropped, written to `late/`, and its market voided. Neither case may reach the engine. Test in Task 5 (`test_future_stamped_row_dropped`).
3. **A market admitted after its window start, or whose ptb-revealing Chainlink tick arrived before admission.** Live and session replay must reveal at the same time and key. Test in Task 5 (`test_ptb_reveal_admitted_late`).
4. **A 20 ms tick that straddles an hour boundary.** Drain order, Binance hour rotation, grid segments and `mark_hour` must match the replay's segment start. Test in Task 5 (`test_parity_across_hour_boundary`).
5. **A resolution that arrives after `end_ms`, or never** (Chainlink gap and Gamma timeout). The market settles without any dispatch, or is voided "no outcome". The ending drain matches the replay. Test in Task 5 (`test_resolution_after_end_and_never`) and Task 9 (`test_gamma_fallback`).

---

### Task 1: Identity harness and reference outputs  (tier: standard)

**Files:**
- Create: `analysis/tools/replay_identity.py`
- Test: `analysis/tests/bt/test_replay_identity.py`

**Interfaces:**
- Consumes: `engine.run(cfg, con_factory, workers)`, `runs.summary(res, cfg)`, `bt.metrics.compute(res, cfg, day_kind_of)`, `bt.metrics.clean`.
- Produces:
  - `replay_identity.CASES: list[tuple[str, RunConfig]]`, seven cases:
    - `configs/validation/{endgame-maker-v2,fv-taker-m3,mm-side-pull}.yaml`, each loaded with `days=["2026-11-24"], day_kinds=["train"]`
    - `noop`, `buy-and-hold-up`, `always-cross`, `join-best`, each on `coins=["btc","sol"], timeframes=["5m"], days=["2026-11-24"]` (as `validate.run_suite`)
  - `replay_identity.fingerprint(res: RunResult, cfg: RunConfig) -> dict[str, bytes]` with keys `results.json` and `hashes.json`:
    - `results.json`: `json.dumps(clean({**runs.summary(res, cfg), "metrics": compute(res, cfg, dk)}), sort_keys=True, indent=1, allow_nan=False)`
    - `hashes.json`: an ordered sha256 per frame (`orders`, `fills`, `equity`, `markets`, and `decisions` when present) of `df.to_csv(index=False, lineterminator="\n")` with row order kept, plus a sha256 of `"\n".join(res.log)`.
  - CLI: `python tools/replay_identity.py save --out DIR [--only NAME…]` and `python tools/replay_identity.py check --ref DIR [--only NAME…]`. `check` exits 1 on any difference and prints the first differing key per case.

- [ ] **Step 1: Write the failing tests** (`test_replay_identity.py`, synthetic, using `tests/bt/test_engine.py` helpers `make_con`, `loader_for`, `market_one`, `cfg`):
  - `test_fingerprint_stable`: two `engine.run_coin` runs of the same config give equal `fingerprint` dicts.
  - `test_fingerprint_detects_change`: changing `order_latency_ms` changes `hashes.json`.
  - `test_fingerprint_order_sensitive`: reversing `res.coins["btc"].orders` rows changes `hashes.json`.
- [ ] **Step 2:** Run `cd analysis && uv run pytest tests/bt/test_replay_identity.py -q`. Expect FAIL (module missing).
- [ ] **Step 3:** Implement `analysis/tools/replay_identity.py`. Import it in tests via `sys.path.insert(0, <analysis>/tools)`. Do not import `stats` timings.
- [ ] **Step 4:** Run the tests. Expect PASS.
- [ ] **Step 5:** Save the reference on the unchanged engine. This is the reference for every later task:
  `cd analysis && POLYDATA_DIR=$PD uv run python tools/replay_identity.py save --out $PD/identity/ref`
  Then run `… check --ref $PD/identity/ref`. Expected: `identical: 7/7`, exit 0. Record the wall time of the save in the commit message.
- [ ] **Step 6: Commit** `tools: replay identity harness` (test + tool only; the reference lives under `$PD`, never in git).

---

### Task 2: Core split — pure move, lazy trade arrays, call tracing  (tier: most capable)

**Files:**
- Create: `analysis/src/polydata/bt/core.py`, `analysis/src/polydata/bt/trace.py`
- Modify: `analysis/src/polydata/bt/engine.py`, `analysis/src/polydata/bt/exchange.py:130-220,459-470`
- Test: `analysis/tests/bt/test_core.py`

**Interfaces:**
- Consumes: everything `_Replay` uses today.
- Produces:
  - `bt/core.py`:
    - The constants `K_OPEN…K_BN`, `_RANK`, `P_*`, `ORDER_COLS`, `FILL_COLS`, `EQUITY_COLS`, `MARKET_COLS`, `MID_BEFORE_MS`, `SIG_WINDOW_S`, `HOUR_MS`, `INF`.
    - `sym_of`, `_up_mid`, `_Bn`, `_EMPTY_BN`, `_Grid`, `_Mkt`, `SimCtx`, `_levels`, `_book_loader`.
    - `class Core`, with `Core(cfg: RunConfig, coins: list[str], budgets: dict[str, float], strategy: Strategy, *, trace: bool = False)`. It owns every non-loading method of today's `_Replay` with unchanged bodies: `push`, `_advance`, `_drain`, `_catch_up`, `_mark_live`, `_arrival`, `submit`, `cancel`, `set_timer`, `_on_ex`, `_done`, `_report`, `_handle`, `_want_equity`, `_equity_sample`, `_close`, `_settle`, `_annotate`, `_bn_find`, `bbo`, `_bn_mid`, `_bn_move`, `cl_tick`, `fair_value`.
    - Renamed: `dispatch(recv, kind, mi, idx, bn_lists, cl_lists)` (was `_dispatch`) and `result() -> CoinResult` (was `_result`).
    - New wrappers around today's inline code in `run`: `start(t: int) -> None` (`ctx.now = t`, `_equity_sample(t)`, `strategy.on_start`) and `finish(end_ms: int) -> None` (`ending = True`, `_drain(INF)`, `_equity_sample(max(ctx.now, end_ms))`).
    - `CoinResult` and `RunResult` move here too and are re-exported from `engine`.
  - `bt/engine.py`:
    - `class ReplayDriver`, with `ReplayDriver(core: Core, loader: Callable, rec_events: DataFrame | None, clip: tuple[int, int] | None)`. Methods `_ranges`, `_select_markets`, `_load_chunk`, `_load_bn`, `_load_cl`, `_segment`, `_schedule_ptb`, `run(con) -> CoinResult`. They reach Core state through `self.core` and call Core exactly as `_Replay` called itself.
    - Unchanged public API: `run`, `run_coin`, `_replay`, `default_con_factory`, `RunResult`, `CoinResult`, `SimCtx`, `sym_of`, `ORDER_COLS`, `FILL_COLS`, `EQUITY_COLS` (re-exported).
    - `run_coin(…, trace: bool = False)` and `run(…, trace: bool = False)` thread `trace` through to Core.
  - `bt/exchange.py`: `Exchange` no longer stores `_ts/_tside/_tpx/_tsize/_n`. `advance`, `next_event_ms`, `_on_trade`, `_explained` read `tr = self.md.trades` and `len(tr.ts_ms)` at each call.
  - `bt/trace.py`:
    - `DECISION_COLS = ["n", "now", "call", "key", "token", "side", "price", "size", "ret"]`
    - `class TracedStrategy` wraps a Strategy. Each handler appends a row with `call` = the handler name (`on_start`, `on_market_open`, …) and `key` = slug for market handlers, sym or coin for `on_binance`/`on_chainlink`, the timer name, or `str(order.id)` for `on_order`. Then it delegates.
    - `class TracedCtx(SimCtx)` records every ctx method call. `token`/`side`/`price`/`size` are filled for `place`/`take` and NaN/None otherwise. `ret` is `repr(value)` for scalars, tuples, `Top`, `Bbo`, `ChainlinkTick`, `MarketInfo` and None. For `book` it is `"BookView"`/`"None"` (no load); for `orders` and `markets` it is `repr([v.id …])`/`repr([m.slug …])`.
    - `n` is the running row number. `CoinResult` gains `decisions: pd.DataFrame` (default: empty with `DECISION_COLS`), and `RunResult.decisions` concatenates it with `budget`.

- [ ] **Step 1: Write failing tests** (`test_core.py`):
  - `test_engine_public_api_unchanged`: `from polydata.bt.engine import run, run_coin, RunResult, CoinResult, SimCtx, ORDER_COLS, FILL_COLS, EQUITY_COLS, sym_of, default_con_factory` works, and `engine.ReplayDriver` and `core.Core` exist.
  - `test_exchange_reads_trades_lazily`: build `Exchange(md)`, then swap `md.trades` for a `Trades` with one extra trade at a resting order's price. `advance` fills against it (proves there is no cached copy).
  - `test_trace_off_by_default_and_identical`: `run_coin(..., trace=True)` and `trace=False` on the `test_engine` fixtures give equal orders/fills/equity/markets/log, and `decisions` is non-empty only with `trace=True`.
  - `test_trace_rows`: with the `test_engine.Recorder` strategy, the first decision row is `call == "on_start"`. Every `place` row has a numeric `price` and `ret == repr(order_id)`. Rows are in `n` order with nondecreasing `now`.
- [ ] **Step 2:** Run `uv run pytest tests/bt/test_core.py -q`. Expect FAIL.
- [ ] **Step 3 (commit A, move only):** Move the code into `core.py`/`ReplayDriver` with no logic change. Run `uv run pytest -q` and `POLYDATA_DIR=$PD uv run python tools/replay_identity.py check --ref $PD/identity/ref`. Expect all pass and `identical: 7/7`. Commit `bt: split _Replay into Core and ReplayDriver (move only)`.
- [ ] **Step 4 (commit B):** Make `Exchange` read trades lazily. Same two checks. Commit `bt: Exchange reads md.trades lazily`.
- [ ] **Step 5 (commit C):** Add tracing (`trace.py`; Core uses `TracedStrategy`/`TracedCtx` when `trace=True`, and `Strategy.overrides` is still read on the wrapped class). Run the tests and the identity check (trace off): `identical: 7/7`. Commit `bt: optional call tracing (decisions)`.

---

### Task 3: Core entry points and explicit ordering rules  (tier: most capable)

**Files:**
- Modify: `analysis/src/polydata/bt/core.py`, `analysis/src/polydata/bt/engine.py`
- Test: `analysis/tests/bt/test_core.py` (extend)

**Interfaces:**
- Consumes: Task 2's `Core`, `ReplayDriver`.
- Produces:
  - Kinds renumbered with the same relative order: `K_ADMIT, K_OPEN, K_TOP, K_TRADE, K_CL, K_BN = 0, 1, 2, 3, 4, 5`; `_RANK = np.array([0, 0, 0, 0, 1, 2], np.int8)`.
  - Heap entries are `(t, prio, k, tie, payload)` (Decision 2). `push(t, prio, payload)` keeps its signature (`k = seq`, `tie = 0`), and `_handle` unpacks 5-tuples.
  - `_Mkt` gains `load_ms: int` and `resolved: bool`. `mi` is the load index.
  - `Core.stage(info: MarketInfo, md, rs: int, load_ms: int) -> int`: creates the `_Mkt`, appends it to `mkts`/`by_slug`, returns `mi`. It does not push and is not yet in `loaded`.
  - `Core.admit(mi: int, row: dict | None = None) -> None`: appends to `loaded`, `pf.add_market(info)`, pushes the close at `window_end_ms`, creates `market_rows[slug]` from `row` or a default (`voided=False, reason="", pnl=0.0, settle_ms=NaN, outcome=info.outcome`), then `scan_ptb()`.
  - `Core.add_market(info, md, rs: int, load_ms: int, open_ms: int) -> _Mkt` = `stage` + `admit` + set `open_ms`. `_load_chunk` uses it.
  - `Core.reset_range() -> None` (grid, bn_cur, bn_prev, cl = {}).
  - `Core.add_bn_hour(s: int, e: int, bns: dict[str, _Bn]) -> None` (rotation + `_Grid.append`, moved out of `_load_bn`).
  - `Core.add_cl(seg: dict[str, tuple[ndarray, ndarray, ndarray]]) -> None` (concatenate, then `scan_ptb()`).
  - `Core.mark_hour(s: int) -> None`, `Core.scan_ptb() -> None`, `Core.schedule_ptb(m: _Mkt, r: int, value: float | None) -> None` (algorithm below). `ReplayDriver._schedule_ptb` is replaced by `core.mark_hour(s)` at segment start (after chunk loads and `add_bn_hour`, before `add_cl`).
  - `Core.set_resolution(slug: str, outcome: str, recv_ms: int) -> None`: sets `info` (`outcome`, `resolution_recv_ms`) and `resolved = True`. If the market is already closed, it pushes the settle now; otherwise `_close` will.
  - `Core(…, defer_settle: bool = False)`. In `stage`, `resolved` is set from `info.outcome is not None`.
  - `Core._close`, with `defer_settle=False` (normal replay): pushes the settle exactly as today, at `max(res or t, t + R)`. With `defer_settle=True` (live, and session-mode replay, Task 5): it pushes only if `m.resolved`. The settle always carries key `window_end_ms * 2**20 + mi`.
  - `Core._annotate`: rows get `settle_value = NaN` while the outcome is unknown, and their indices are kept on `m`. `_settle` patches them.
  - `Core.finish_unresolved() -> None` (Decision 3). Only meaningful with `defer_settle=True`; LiveRouter.finish and session-mode `run_session` call it before `Core.finish`.
  - `Core.on_trades_appended(mi: int) -> None`: if `mi in live`, `next_ex = min(next_ex, mkts[mi].ex.next_event_ms())`.
  - `Core.dispatch` handles `K_ADMIT` by calling `admit(j)` (no strategy call).

**ptb algorithm** (not determined by the signatures):

```python
def mark_hour(self, s):                 # after everything < s is drained, before any data event >= s
    self.hour_seq[s] = self._seq
    for m, r, v in self._ptb_wait.pop(s, []):   # in load order
        self._push_ptb(m, r, v, s)

def scan_ptb(self):                     # admitted, not closed, not yet scheduled; in load order
    for m in self.loaded:
        if m.ptb_sched: continue
        c = self.cl.get(m.info.coin)
        hit = np.flatnonzero(c[1] >= m.info.window_start_ms) if c is not None else []
        if not len(hit): continue
        m.ptb_sched = True
        r, ws = int(c[0][hit[0]]), m.info.window_start_ms
        sel = (c[1] >= ws - 60_000) & (c[1] < ws) & (c[0] <= r)
        value = None if ws - 60_000 < m.rs or not sel.any() else float(c[2][sel].mean())
        if value is not None:
            self.schedule_ptb(m, r, value)

def schedule_ptb(self, m, r, value):
    s_star = max(m.rs, (max(r, m.info.window_start_ms, m.load_ms) // HOUR_MS) * HOUR_MS)
    if s_star in self.hour_seq: self._push_ptb(m, r, value, s_star)
    else: self._ptb_wait.setdefault(s_star, []).append((m, r, value))

def _push_ptb(self, m, r, value, s_star):
    heapq.heappush(self.heap, (max(r, s_star, m.load_ms), P_TIMER, self.hour_seq[s_star] + 0.5, m.mi, ("ptb", m, value)))
```

In replay, `scan_ptb` runs in `add_cl` (segment start, after `mark_hour`) and in `admit`. `hour_seq` keys include each range start `rs`.

- [ ] **Step 1: Write failing tests** (extend `test_core.py`; synthetic via `test_engine` helpers):
  - `test_settle_order_by_window_end_then_load`: three markets, two sharing a `window_end_ms`, all with equal `resolution_recv_ms`. Call `set_resolution` in reverse order after their closes, through a hand-driven Core (`stage`/`admit`/`dispatch`/`_drain`). The settle order in `market_rows` `settle_ms` assignment and in `pf` equals (window_end, mi).
  - `test_ptb_key_matches_replay_push`: a Core driven by hand with `mark_hour(h)`, a strategy timer set to fire at the reveal millisecond before `h`, and one after. The order of `on_timer` vs ptb visibility (`ctx.fair_value` None or not inside `on_timer`) matches `engine.run_coin` on the same data.
  - `test_ptb_deferred_until_hour_marked`: `schedule_ptb` with `s* > last marked hour` pushes nothing until `mark_hour(s*)`.
  - `test_set_resolution_before_and_after_close`: the settle time is `max(res, close + R)` in both cases, and fills get `settle_value` 1.0/0.0 after settlement (NaN before).
  - `test_finish_unresolved_voids`: a closed, unresolved market ends `voided=True`, `reason="no outcome at settlement"`, `pnl` NaN.
- [ ] **Step 2:** Run `uv run pytest tests/bt/test_core.py -q`. Expect FAIL.
- [ ] **Step 3:** Implement, in two commits: (a) kinds, the heap tuple, `stage`/`admit`/`add_market`/`reset_range`/`add_bn_hour`/`add_cl`, each followed by the identity check; then (b) the ptb rule, settle key, `set_resolution`, deferred annotation and `finish_unresolved`, followed by the identity check.
- [ ] **Step 4:** Run `uv run pytest -q` and `POLYDATA_DIR=$PD uv run python tools/replay_identity.py check --ref $PD/identity/ref`. Expected: all pass, `identical: 7/7`.
- [ ] **Step 5: Commits** `bt: Core entry points (stage/admit, hour buffers)` and `bt: explicit ordering rules (settle key, ptb hour sequence, late resolution)`.

---

### Task 4: GrowingMarketData, live hour buffers, market info from meta  (tier: most capable)

**Files:**
- Create: `analysis/src/polydata/bt/marketdata_live.py`
- Modify: `analysis/src/polydata/views.py` (`markets_sql`), `analysis/src/polydata/bt/core.py` (`_grid_segment`, used by `_Grid.append`)
- Test: `analysis/tests/bt/test_marketdata_live.py`, `analysis/tests/bt/realdata/test_marketdata_live_realdata.py`

**Interfaces:**
- Consumes: `marketdata.MarketData` (query contract), `marketdata._info`, `marketdata.LOOKBACK_WINDOWS`, `MARGIN_MS`, `Tops`, `Trades`, `Ticks`, `core._Grid`, `core._Bn`.
- Produces:
  - `views.markets_sql(meta_rel: str, res_rel: str) -> str`. `MARKETS_SQL = markets_sql("read_parquet('{P}/pm_market_meta/*/*.parquet')", "read_parquet('{P}/pm_resolution/*/*.parquet')")`, so `build.build_markets` is unchanged in behaviour.
  - `marketdata_live.info_from_meta(meta: dict, recv_ms: int) -> MarketInfo`: runs `markets_sql("_meta", "_res")` in an in-memory DuckDB with `_meta(recv_ms BIGINT, d JSON)` holding the row and an empty `_res`, selects the columns `marketdata._MARKETS_SQL` selects, and applies `_info`.
  - `class GrowingArray`: a capacity-doubling numpy buffer. `append(values)`; `.view` is the filled part.
  - `class GrowingMarketData`, with `GrowingMarketData(info: MarketInfo)`, implementing everything Core, `Exchange` and `SimCtx` read: `info`, `size_at`, `book_at`, `level_changes`, `prices`, `levels`, `top_at`, `tick_at`, `trade`, and attributes `trades`, `trades_recv`, `tops`, `tops_recv`, `ticks`, `snaps_recv` (NamedTuples of `GrowingArray` views, rebuilt only when lengths change).
  - Feed methods (rows are recorder-column tuples of text, as in `LiveRow.values`, Task 5):
    - `add_ts(ts_ms: int, changes: list[tuple[int, int, tuple]], books: list[tuple[int, int, tuple]], trades: list[tuple[int, int, tuple]], ticks: list[tuple[int, float]]) -> int` takes one complete `ts_ms` worth of rows as `(recv_ms, seq, values)`. It returns the number of trades appended.
    - `add_recv(rows: list[tuple[str, int, int, int, int]]) -> None` takes `(kind, recv_ms, ts_ms, seq, ref)` in `(recv_ms, k, seq)` order. `kind` is one of `"top"`, `"trade"`, `"tick"`, `"snap"`; `ref` indexes the row's ts-side entry.
  - `class LiveBn` (`recv`, `bid`, `ask`, `bid_qty`, `ask_qty` as views, so it duck-types `_Bn`).
  - `class LiveGrid`, with `LiveGrid()`, `start_segment(sec_from: int)`, `set_bundles(bn: LiveBn, sec_done: int)`, `sig15(t_ms) -> float | None`.
  - `core._grid_segment(sec_from: int, sec_to: int, bn, prev: float | None, first: bool, base: float) -> tuple[int, ndarray, ndarray] | None` returns `(sec0_offset, filled, csum)`. `_Grid.append` is rewritten on top of it (identity check).

**Ts-side rules** for one `ts_ms`, so the result is the SQL pipeline's:
1. Parse `price`/`size`/`best_bid`/`best_ask` with `round(float(x) * 1000)`. An empty best side becomes 0/1000.
2. Up change rows are used. A Down row is used, mapped to Up terms, only if no Up row in this `ts_ms` has the opposite token side and `px == 1000 − down_px` (`_SQL_CHANGES`).
3. Snapshot: keep one `pm_book` row per `ts_ms`. Prefer Up; then take the max `(recv_ms, seq)`. Its listed levels are the snapshot entries; size-0 entries go to every level that is currently positive and not listed (`_zero`).
4. Level events sort by `(k, recv_ms, seq, src, size)`, with `k = 2·ts` for changes (`src` 0) and `2·ts + 1` for snapshot rows (`src` 1 listed, 2 zeroed). Append per level: `ts_ms`, `recv_ms`, `recv_eff` (running max), `size`, `last_pos`. Keep price keys sorted (BUY descending, SELL ascending) with `bisect`.
5. ts-order tops: change rows' `(bb, ba)` and the snapshot top (max listed positive bid, min listed positive ask, default 0/1000), in `(k, recv, seq)` order. Append only where `(bid, ask)` differs from the previous ts-order top.
6. Trades are appended in `(ts, recv, seq)` order with token, side, price, size, `up_side`, `up_px` as `_SQL_TRADES_OUT`. Ticks are deduplicated on `(ts_ms, tick_px)`, keeping the earliest `recv_ms`.

The recv side applies the same "changed" filter for `tops_recv` against the previous recv-order top. Rows with `ts_ms < ws − LOOKBACK_WINDOWS·len − MARGIN_MS` are ignored (Decision 8).

Binance live bundles: per symbol, the last row by `(recv_ms, update_id)` of each `recv_ms // bn_step_ms` bucket, delivered at `b·step + step − 1` once `T ≥` that ms. Chainlink: first receipt of each `(coin, ts_ms)` wins, and the tick is visible only if that first receipt is `≥ rs`. Arrays are kept in `(recv_ms, ts_ms)` order.

- [ ] **Step 1: Write failing tests** (`test_marketdata_live.py`). Feed the same synthetic rows to `MarketData.from_frames` and, row by row, to `GrowingMarketData`. Compare `size_at`/`book_at`/`top_at`/`tick_at`/`level_changes` for every level on both clocks, at every ms in `[t0, t1]` with step 7 ms, plus `trades`, `trades_recv`, `tops`, `tops_recv`, `snaps_recv` arrays (`np.array_equal`).
  - `test_equivalence_synthetic_basic` (changes, snapshot, trades)
  - `test_down_mirror_split_across_messages` (Review Focus 1: Down row recv 5 ms before its Up mirror, same `ts_ms`)
  - `test_unmirrored_down_used`
  - `test_snapshot_zeroes_unlisted_levels`
  - `test_snapshot_after_same_ts_changes`
  - `test_one_sided_top` (empty `best_ask` → 1000)
  - `test_recv_eff_out_of_order` (a later-ts change received earlier)
  - `test_tick_change_dedup`
  - `test_appending_never_changes_visible_answers`: record all answers up to t, append more rows with `ts > t`, re-query → unchanged.
  - `test_info_from_meta_matches_markets_view`: write a `pm_market_meta` JSONL with `test_pipeline.write_jsonl`, build, and compare `marketdata.load_coin`'s `MarketInfo` with `info_from_meta` (all fields except outcome/resolution).
  - `test_live_grid_bitwise`: feed one hour of synthetic bundles second by second to `LiveGrid` and whole to `_Grid`. `sig15(t)` is `==` (not approx) at every second.
- [ ] **Step 2:** Run `uv run pytest tests/bt/test_marketdata_live.py -q`. Expect FAIL.
- [ ] **Step 3:** Implement. Factor out `_grid_segment` and `markets_sql` first, in a separate commit, then run the identity check (`identical: 7/7`). Then add `marketdata_live.py`.
- [ ] **Step 4: Real-data equivalence** (`test_marketdata_live_realdata.py`, `POLYDATA_REALDATA=1`):
  - `test_real_hour_equivalence[btc|sol]`: for BTC and SOL 5m markets overlapping 2026-11-24 11:00–12:00, read the raw rows (`pm_price_change`, `pm_book`, `pm_trade`, tick changes from `pm_other`) via DuckDB `read_csv` of `$PD/raw/2026-11-24/{10,11,12}/*.tsv.zst`, keeping file order as `seq`. Feed them in `recv` order to `GrowingMarketData`, releasing `ts ≤ recv − 3000` as the live path would. Compare with `load_coin`'s `MarketData` on both clocks every 50 ms.
  - `test_real_grid_bitwise`: run `LiveGrid` and `_Grid` on `bn_fut_bbo` BTCUSDT for the same hour; `sig15` is equal at every second.

  Run: `POLYDATA_REALDATA=1 POLYDATA_DIR=$PD uv run pytest tests/bt/realdata/test_marketdata_live_realdata.py -q`. Expect PASS.
- [ ] **Step 5:** Run `uv run pytest -q` and the identity check (`identical: 7/7`).
- [ ] **Step 6: Commits** `views: markets_sql(meta, res); bt: _grid_segment` and `bt: GrowingMarketData and live hour buffers`.

---

### Task 5: LiveSource, LiveRouter and session-mode replay  (tier: most capable)

**Files:**
- Create: `analysis/src/polydata/bt/live.py`
- Modify: `analysis/src/polydata/bt/engine.py` (session mode)
- Test: `analysis/tests/bt/test_live.py`, `analysis/tests/bt/synth.py` (synthetic session generator)

**Interfaces:**
- Consumes: Tasks 3–4 (`Core.stage/admit/dispatch/_drain/mark_hour/scan_ptb/set_resolution/on_trades_appended/start/finish/finish_unresolved`, `GrowingMarketData`, `LiveBn`, `LiveGrid`, `info_from_meta`).
- Produces (`bt/live.py`):
  - `class LiveRow(NamedTuple)`: `stream: str`, `recv_ms: int`, `values: tuple[str, ...] | dict`. TSV streams carry the recorder columns after `recv_ms` as text. JSONL streams carry the object (for `pm_other`, the message).
  - `PM_TS_STREAMS = ("pm_book", "pm_price_change", "pm_best_bid_ask", "pm_trade")`.
  - `is_late_candidate(row) -> bool` (the streams above, plus `pm_other` with `event_type == "tick_size_change"`).
  - `class Resolution(NamedTuple)`: `slug`, `outcome`, `recv_ms`, `source`, `ptb: float | None`, `close_twap: float | None`.
  - `class LiveRouter`, with `LiveRouter(cfg: RunConfig, rs: int, *, hold_back_ms: int = 3000, skew_ms: int = 1000, trace: bool = True, admit_filter: Callable[[MarketInfo, int], bool] | None = None, settler=None)`:
    - `ingest(row: LiveRow) -> Literal["ok", "late", "future"]`: classifies (Decision 5), assigns per-stream `seq` to ok rows and buffers them.
    - `tick(wall_ms: int) -> None`
    - `request_stop(t_ms: int) -> None` sets `end_ms = max(t_ms, last admitted window_end_ms)`, stops admissions, and stops dispatch at `end_ms`.
    - `apply_resolution(res: Resolution) -> None`
    - `unresolved() -> list[str]`
    - `finish() -> RunResult` (`finish_unresolved`, `Core.finish(end_ms)` per budget)
    - Attributes: `T`, `H_ts`, `end_ms`, `admitted: list[dict]` (`{slug, admitted_ms, load_index, budget}`), `late: list[tuple[LiveRow, int]]` (row, `late_by_ms`), `new_resolutions: list[Resolution]` (drained by the caller), `stats: dict` (rows per stream, late/future counts).
    - Per-coin mode gives one `LiveSource` per coin (budget `capital/len(coins)`, its own strategy instance from the registry, the Core RNG seeded as `engine.run_coin`). Global mode gives one `LiveSource("*")`.
    - `settler` protocol (implemented in Task 8): `admit(info, admitted_ms)` and `on_tick(coin, recv_ms, ts_ms, price) -> list[Resolution]`.
  - `class LiveSource`, with `LiveSource(core: Core, coins: list[str], rs: int)`: the per-budget state (markets by slug and condition id, `LiveBn` per symbol, `LiveGrid`, Chainlink arrays, `T_prev`).
  - `bt/engine.py`:
    - `SessionPlan(NamedTuple)`: `rs: int`, `end_ms: int`, `admitted: list[tuple[str, int]]` (slug, `admitted_ms`, in manifest order).
    - `run_session(cfg, con_factory, plan: SessionPlan, *, trace: bool = True, loader=None) -> RunResult`: runs `ReplayDriver(..., session=plan)` per budget. Session mode means: range `[rs, end_ms)`; no `_ranges`/`_select_markets`/pre-voiding; markets are staged in manifest order at the segment start of their admission hour; `K_ADMIT` events sit at `admitted_ms`; `K_OPEN` at the first `snaps_recv ≥ admitted_ms` (if `< window_end`); `K_TOP`/`K_TRADE` from `max(s, open_ms)`.

**Tick algorithm** (`LiveRouter.tick`; not determined by the signatures):

```
H_new = wall − hold_back_ms; T_new = H_new − skew_ms
1. ts release: take buffered PM rows with ts_ms ≤ H_new, group by (slug, ts_ms), and in (ts_ms, slug) order
   call md.add_ts(...). If a md gained trades and its market is admitted, call core.on_trades_appended(mi).
   Set H_ts = H_new; from now on a PM row with ts_ms ≤ H_ts is late.
2. Recv release: if T < rs, only buffer (warm-up). The first time T_new ≥ rs, call core.start(rs) per budget,
   then core.mark_hour(rs). Split (T_prev, T_new] at each hour boundary h into pieces; for each piece [a, b]:
   a. Pop rows with recv in [a, b] in (recv_ms, stream order, seq) order. PM rows → md.add_recv;
      pm_market_meta → info_from_meta, then admission if admit_filter allows (K_ADMIT at max(recv, rs));
      pm_resolution (external) → apply_resolution at its recv; rtds_price Chainlink rows for config coins
      → first-receipt dedup → cl arrays (recv ≥ rs) → settler.on_tick → apply_resolution for each result
      (recv = r_end); bn_fut_bbo rows with recv ≥ rs → buckets.
   b. Finalise Binance buckets with last ms ≤ b. LiveGrid.set_bundles(sec_done = (b + 1) // 1000).
      core.cl[coin] = views; core.scan_ptb().
   c. Build data events with recv in [a, b] (and < end_ms): K_ADMIT, K_OPEN, K_TOP (if want on_book,
      recv ≥ open_ms, < window_end), K_TRADE (if want on_trade), K_CL, K_BN (if wanted; idx into this
      piece's lists). np.lexsort((idx, mi, kind, _RANK[kind], recv)) → core.dispatch(...).
   d. If b + 1 == h (an hour boundary): core._drain(h); rotate bn_prev/bn_cur to new LiveBn,
      grid.start_segment(h // 1000); core.mark_hour(h).
3. core._drain(min(T_new, end_ms − 1) + 1). T_prev = T_new.
```

Monotone `recv_ms` stamping is the caller's job (Task 7). The router assumes nondecreasing `recv_ms` across all `ingest` calls and raises if one goes back. A future-stamped row has `ts_ms > recv_ms + skew_ms`; a late row has `ts_ms ≤ H_ts` at ingest. Both go to `late` and are never buffered. Admission also requires coin and timeframe in the config, `window_start_ms ≥ rs`, not stopping, and `admit_filter` (sealed days and duration, Task 8).

After `T ≥ end_ms`, nothing is dispatched. Rows are still released (resolutions apply via `apply_resolution`; settles are pushed). `finish()` is then `finish_unresolved()` followed by `core.finish(end_ms)` for each budget.

- [ ] **Step 1: Write the synthetic generator** `tests/bt/synth.py`: `make_session(seed: int, start_ms: int, minutes: int, coins=("btc",), timeframes=("5m",)) -> list[LiveRow]`.
  - Recorder-shaped rows in nondecreasing `recv_ms`: meta rows (event JSON shaped like Gamma's: `slug`, `markets[0]` with `conditionId`, `clobTokenIds`, `outcomes`, `endDate`, `feeSchedule`, `orderPriceMinTickSize`, `orderMinSize`, `cryptoMarketConfig.asset`; `series`); book snapshots; Up price changes with mirrored Down rows (some unmirrored); trades on both tokens; one `tick_size_change`; `rtds_price` Chainlink rows every second per coin; `bn_fut_bbo` rows every 3 ms per symbol.
  - Feed delay `recv − ts` is drawn from 20–400 ms; some rows share a `recv_ms`; rows cross at least one hour boundary.
  - Also `write_store(rows, root: Path) -> None`: writes rows in recorder layout with `tests/test_pipeline.write_tsv/write_jsonl` (5-minute slot files by `recv_ms`, TSV header row) so `polydata build` works.
- [ ] **Step 2: Write failing tests** (`test_live.py`). The harness `run_both(rows, cfg, rs, end_ms)` runs:
  - **live**: `LiveRouter.ingest` each row at its `recv_ms`, then `tick(wall)` every 20 ms of virtual wall time, with wall ≥ `recv_ms` of the rows ingested so far. Then `request_stop`, resolutions, `finish()`.
  - **replay**: `write_store` → `build` (`monkeypatch.setenv("POLYDATA_DIR", tmp)`) → `run_session(cfg, con_factory over create_views(include_sealed=True), SessionPlan(rs, end_ms, router.admitted))`.

  Assert `decisions`, `orders`, `fills`, `equity` and `markets[MARKET_COLS]` are `DataFrame.equals` per budget (NaN = NaN), and log lines are equal.
  - `test_parity_noop`, `test_parity_join_best`, `test_parity_always_cross`, `test_parity_endgame_maker` (`get_strategy("endgame-maker")` with `configs/validation/endgame-maker-v2.yaml` params, 2 coins)
  - `test_parity_across_hour_boundary` (Review Focus 4: data starts 11:58, a strategy timer at exactly 12:00:00.000, an `on_binance` strategy)
  - `test_parity_per_coin_and_global`
  - `test_late_row_dropped` (a row whose delay exceeds the hold-back → `late`, not in the session store, the run still at parity)
  - `test_future_stamped_row_dropped` (Review Focus 2)
  - `test_ptb_reveal_admitted_late` (Review Focus 3: meta `recv` after `window_start` and after the reveal tick; parity, and `fair_value` becomes non-None at the same `now` on both sides)
  - `test_resolution_after_end_and_never` (Review Focus 5: one market's `pm_resolution` row has `recv > end_ms`, another has none; parity, and the second is voided "no outcome at settlement")
  - `test_hold_back_invariant`: during the live run, assert every core query time `≤ T` and exchange advance `≤ T − R < H_ts`; and `skew_ms + R < 150` raises `ValueError` at construction.
- [ ] **Step 3:** Run `uv run pytest tests/bt/test_live.py -q`. Expect FAIL.
- [ ] **Step 4:** Implement `live.py`, then session mode in `engine.py`. When a parity test fails, compare `decisions` first (the first divergence names the event).
- [ ] **Step 5:** Run `uv run pytest -q` and the identity check (`identical: 7/7`; session mode must not touch normal replay).
- [ ] **Step 6: Commit** `bt: LiveSource/LiveRouter and session-mode replay (synthetic parity)`.

---

### Task 6: Feeds (series table, WebSocket client, CLOB, RTDS, Binance, REST)  (tier: standard)

**Files:**
- Create: `analysis/src/polydata/paper/__init__.py`, `paper/feeds/__init__.py`, `paper/feeds/series.py`, `paper/feeds/ws.py`, `paper/feeds/polymarket.py`, `paper/feeds/rtds.py`, `paper/feeds/binance.py`, `paper/feeds/rest.py`, `analysis/tools/feed_smoke.py`
- Modify: `analysis/pyproject.toml` (`uv add websockets`)
- Test: `analysis/tests/paper/test_series.py`, `analysis/tests/paper/test_parsers.py`, `analysis/tests/paper/golden/*.json`

**Interfaces:**
- Consumes: `LiveRow` (Task 5). Recorder behaviour as cited in the spec (`src/recorder/{config,polymarket,rtds,binance,wsClient}.ts`).
- Produces:
  - `series.SERIES: list[Series]`, where `Series(NamedTuple)` has `id`, `slug`, `coin`, `recurrence`; `RECURRENCE_MS`, `LOOKAHEAD`, `SUBSCRIBE_LEAD_WINDOWS = 2`, `MIN_SUBSCRIBE_LEAD_MS = 600_000`, `UNSUBSCRIBE_AFTER_END_MS = 180_000`, `PER_MARKET_SERIES`, and `TF_OF = {"5m": "5m", "15m": "15m", "hourly": "1h", "4h": "4h", "daily": "1d"}`. `series_for(coins, timeframes) -> list[Series]`.
  - `ws.ReconnectingWs`, with `ReconnectingWs(name: str, url: Callable[[], str], on_message: Callable[[str, int], None], on_event: Callable[[str, str, str], None], *, on_open=None, text_ping: tuple[str, int] | None = None, stale_ms: int | None = None, max_lifetime_ms: int | None = None, clock: Callable[[], int])`.
    - `async run()`, `send(text)`, `stop()`, `is_open()`.
    - Backoff 1 s doubling to 30 s. Events `ws_open`, `ws_close` (`code=…, reconnect in … ms`), `ws_stale`, `ws_rotate`, `parse_error`, with the recorder's source names.
  - `polymarket.parse_clob(raw: str, key: Callable[[str], str]) -> list[tuple[str, tuple | dict]]`: `(stream, values)` per the spec table, in message order. `values` are text exactly as received. JSON numbers are written as Python `repr`, None as `""`.
  - `class PolymarketFeed`, with `PolymarketFeed(series: list[Series], emit: Callable[[list[tuple[str, object]]], None], event: Callable[[str, str, str], None], http: Rest, clock)`: discovery every 30 s, subscriptions every 5 s, connection grouping, `pm_market_meta` rows `{series, event}`. `markets()` returns the known markets (slug, condition id, tokens, start, end).
  - `rtds.parse_rtds(raw: str, conn: str, last_ts: dict) -> list[tuple[str, object]]` (`rtds_price` only if `ts > last_ts[topic|symbol]`, else nothing; non-updates go to `rtds_other` with `conn`). `class RtdsFeed`: two connections `a`, `b`, the second started 2 s later.
  - `binance.parse_fut(raw: str) -> list[tuple[str, tuple]]` (`bn_fut_bbo`: `s,u,E,T,b,B,a,A`; `bn_fut_trade`: `s,E,T,a,f,l,p,q,m?1:0`). `class BinanceFeed` on `binance-fut-bbo` (bookTicker) and `binance-fut-market` (aggTrade), stale 30 s, `max_lifetime` 23 h.
  - `rest.Rest`, with `Rest(clock, event)` and `async get_json(api: Literal["gamma", "data"], url: str) -> object` (stdlib `urllib` in `asyncio.to_thread`, timeout 15 s). On 429 it pauses that API for `max(retry_after or 60, 10)` s and raises `RateLimited`. `backoff_ms(failures: int) -> int` gives 30 s doubling to 10 min.
  - Endpoints are constants copied from `config.ts` (public URLs, already in the public repo).

- [ ] **Step 1: Write failing tests:**
  - `test_series_matches_recorder_config`: regex-parse `src/recorder/config.ts` (`SERIES` tuples, `subscribeLeadWindows`, `minSubscribeLeadMs`, `unsubscribeAfterEndMs`) and `polymarket.ts` (`LOOKAHEAD`, `PER_MARKET_SERIES`). Assert they equal the Python copies.
  - `test_series_for_filters`: `series_for(["btc"], ["5m", "1h"])` gives `btc-up-or-down-5m` and `btc-up-or-down-hourly`.
  - Golden parser tests (`golden/clob_*.json`, `rtds_*.json`, `binance_*.json`: hand-written messages in the documented shapes, each with its expected rows):
    - `test_clob_book`, `test_clob_price_change_many_rows_same_recv_order`, `test_clob_best_bid_ask`, `test_clob_last_trade_price`, `test_clob_new_market_and_other` (`tick_size_change` → `pm_other` object unchanged), `test_clob_array_message`, `test_clob_pong_ignored`
    - `test_rtds_dedup_across_connections`
    - `test_binance_book_ticker`, `test_binance_agg_trade_maker_flag`
    - `test_number_text_roundtrip`: a JSON number such as `0.1 + 0.2` written via `repr` reparses to the same float.
  - `test_ws_backoff_and_events`: a local `websockets.serve` server that closes the first two connections. Events are `ws_open, ws_close, ws_open, ws_close, ws_open`, and the reconnect delays are 1 s, 2 s (the clock and sleep are patched).
  - `test_rest_429_pauses_api`: a local `http.server` returning 429 with `Retry-After: 1` → pause ≥ 10 s and a `rate_limited` event.
- [ ] **Step 2:** Run `uv run pytest tests/paper -q`. Expect FAIL.
- [ ] **Step 3:** Implement. Discovery URL: `{gamma}/events?series_id={id}&closed=false&end_date_min={iso}&order=endDate&ascending=true&limit={LOOKAHEAD}`. Subscribe at `start − max(10 min, 2·len)` and unsubscribe at `end + 3 min`. The subscribe message is `{assets_ids, type: "market", custom_feature_enabled: true}`, and later additions use `operation: "subscribe"`. Ping `PING` every 10 s, stale after 25 s.
- [ ] **Step 4:** Run the tests. Expect PASS.
- [ ] **Step 5: Reachability smoke** (risk 5): `cd analysis && uv run python tools/feed_smoke.py --minutes 10 --coins btc eth --timeframes 5m`. It prints rows/s per stream and connection events and writes nothing. Expected: non-zero rates for `pm_book`, `pm_price_change`, `pm_trade`, `rtds_price`, `bn_fut_bbo`, `bn_fut_trade`, and no repeated `ws_close`. If a feed is unreachable, stop and report: do not build further tasks on it. Optionally capture one real message per type into `tests/paper/golden/` (public market data only; strip nothing else; no headers or IPs).
- [ ] **Step 6: Commit** `paper: feeds (series, ws client, CLOB, RTDS, Binance, REST)`.

---

### Task 7: Session writer and ingest  (tier: standard)

**Files:**
- Create: `analysis/src/polydata/paper/session.py`, `analysis/src/polydata/paper/ingest.py`
- Test: `analysis/tests/paper/test_session.py`

**Interfaces:**
- Consumes: `LiveRow`, `LiveRouter.ingest/late`, `schema.TSV`/`schema.JSONL` column orders, `build.build`.
- Produces:
  - `session.SessionWriter`, with `SessionWriter(root: Path, *, bn_step_ms: int = 10, level: int = 3, flush_s: int = 10)`:
    - `write(row: LiveRow) -> None`: slot = `recv_ms // 300_000 · 300_000`, path `raw/YYYY-MM-DD/HH/<stream>.<slotStartMs>.{tsv,jsonl}.zst`. A TSV header row (`recv_ms` + `schema.TSV` columns) is written only when the file is created. JSONL lines are `{"recv_ms": r, "d": obj}`.
    - `write_late(row: LiveRow, late_by_ms: int) -> None` writes to `late/<stream>.tsv.zst` with an extra `late_by_ms` column (JSONL rows as one `d` text column).
    - `event(ts_ms: int, kind: str, source: str, detail: str) -> None` writes to `catalog/YYYY-MM-DD/rec_events.csv.zst` (header `ts,kind,source,detail`; `ts` as `YYYY-MM-DD HH:MM:SS.ffffff+00`; CSV-quoted detail).
    - `write_paper(name: str, obj: dict) -> None` writes `paper/<name>.jsonl.zst`.
    - `flush() -> None`: one zstd frame appended per touched file, in a writer thread; files are opened in append mode and closed after each frame.
    - `close() -> None`; `manifest(**fields) -> None` writes `manifest.json` with `files [{path, rows, sha256}]`.
  - Binance thinning inside `write`: for `bn_fut_bbo`, per `(sym, recv_ms // 10)`, write the first-received row immediately. Hold the latest row by `(recv_ms, update_id)` as the bucket's candidate and write it when a later bucket for that symbol arrives (or at flush/close), unless it is the first row itself.
  - `ingest.Ingest`, with `Ingest(router: LiveRouter, writer: SessionWriter, clock: Callable[[], int])`:
    - `stamp() -> int` (`recv = max(prev, clock())`; a backward step > 2 s writes `recorder stop` at the previous recv and `start` at the new recv).
    - `on_rows(rows: list[tuple[str, object]]) -> None`: one `recv` per call, rows in message order. Each row goes through `router.ingest`; ok → `writer.write`, else → `writer.write_late`. Router's `new_resolutions` → `pm_resolution` rows `{slug, outcome, event, source}` written at their `recv_ms`.
    - `on_event(kind, source, detail) -> None` (stamped, written to the catalog).
    - `note_loop(wall_ms: int) -> None` (an event-loop stall > 2 s since the last call writes a `recorder stop/start` pair).

- [ ] **Step 1: Write failing tests:**
  - `test_session_builds_to_same_rows`: synthetic rows (`tests/bt/synth.make_session`) → `SessionWriter` (with 3 flushes mid-way, so files are multi-frame) → `build` with `POLYDATA_DIR=<session>`. For every TSV stream except `bn_fut_bbo`, the Parquet rows (all columns incl. `seq` order) equal the same rows written by `synth.write_store` and built.
  - `test_multi_frame_files_readable`: Arrow (`build._read_tsv`) and DuckDB `read_json` read a 3-frame file completely.
  - `test_bn_thinning_keeps_first_and_last`: per (sym, 10 ms) bucket the written rows are exactly {first received, last by `(recv, update_id)`}. `engine._load_bn`'s bundles over the thinned store equal those over the full store for `bn_step_ms` 10 and 100. `gaps.bn_silences` gives the same table.
  - `test_late_rows_not_in_raw`: late rows appear only in `late/` with `late_by_ms`, and `build` ignores `late/`.
  - `test_rec_events_catalog_readable`: `gaps.event_gaps` over the session catalog (read with DuckDB as `load_rec_events` does) gives the expected gap for a `ws_close`/`ws_open` pair.
  - `test_monotone_stamp_and_clock_step`: a clock that steps back 3 s → stamps never decrease, and one `recorder stop/start` pair is written.
  - `test_session_replay_equals_live`: the Task 5 parity harness, with the store written by `Ingest` + `SessionWriter` instead of `synth.write_store` → parity holds.
- [ ] **Step 2:** Run `uv run pytest tests/paper/test_session.py -q`. Expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `uv run pytest -q`. Expect PASS.
- [ ] **Step 5: Commit** `paper: session writer and ingest`.

---

### Task 8: `polydata paper run` with Chainlink settlement  (tier: standard)

**Files:**
- Create: `analysis/src/polydata/paper/settle.py`, `analysis/src/polydata/paper/runner.py`, `analysis/src/polydata/paper/cli.py`, `analysis/src/polydata/paper/results.py`
- Modify: `analysis/src/polydata/cli.py` (register `paper`), `analysis/src/polydata/bt/runs.py` (`push(with_sessions=False)`, skip `*/session/**`), `analysis/src/polydata/bt/cli.py` (`bt push --with-sessions`)
- Test: `analysis/tests/paper/test_settle.py`, `analysis/tests/paper/test_run.py`

**Interfaces:**
- Consumes: feeds (Task 6), `Ingest`/`SessionWriter` (Task 7), `LiveRouter` (Task 5), `runs.new_run_dir/provenance/host_hash/rebuild_index`, `validate.is_validated`, `splits.day_kind/record_look`, `bt.cli.load_config`.
- Produces:
  - `settle.ChainlinkSettler`, with `ChainlinkSettler(rs: int)` implementing the router's settler protocol, plus `admit(info, admitted_ms)`.
    - `on_tick(coin, recv_ms, ts_ms, price) -> list[Resolution]` returns, for each admitted market of that coin, a resolution on its first tick with `ts_ms ≥ window_end_ms`.
    - `ptb` = mean of first-received ticks with ts in `[ws − 60 s, ws)`; `close_twap` = mean over `[we − 60 s, we)`. Both are `float(np.mean(...))` in recv order.
    - `outcome = "Up" if close_twap >= ptb else "Down"`, `source = "paper-chainlink"`, `recv_ms = r_end`.
    - It returns nothing (the market waits for Gamma, Task 9) if either window is empty or `ws − 60 s < rs`.
  - `settle.resolution_row(res: Resolution, discovery_event: dict) -> dict`: `{slug, outcome, event: deepcopy(event) with eventMetadata.priceToBeat = ptb and finalPrice = close_twap, source}`.
  - `runner.PaperRun`, with `PaperRun(cfg: RunConfig, out: Path, opts: PaperOpts, feeds_factory)`:
    - `async run() -> Path` (the run folder).
    - `PaperOpts` dataclass: `hold_back_ms=3000, skew_ms=1000, tick_ms=20, warmup_s=60, max_drain_s=900, confirm_wait_s=900, duration_s: int | None = None, allow_sealed=False, purpose: str | None = None`.
    - `feeds_factory(ingest, clock) -> list[awaitable]` builds the live feeds (Task 6) or the fake feed (Task 10).
  - `results.write_paper_run(d: Path, cfg, opts, res: RunResult, router, manifest: dict) -> dict` writes `config.yaml` (cfg + `paper:` block), `provenance.json`, `results.json`, the parquet files, `decisions.parquet` and `log.txt`.
    - `provenance.json`: `runs.provenance(cls, params, [], "paper", validated=…, paper_version=…, session_fingerprint=sha256(manifest.json), pythonhashseed=os.environ["PYTHONHASHSEED"])`.
    - `paper_version()`: sha256 over `polydata/paper/**/*.py`, 12 hex.
    - `results.json`: `runs.summary` keys with `verdict: None`, plus paper stats (rows per stream, late/future counts, processing lag p50/p99/max, feed delay p50/p99 per PM stream, `parity: None`).
    - `markets.parquet`: bt `MARKET_COLS` plus `pnl_raw` (= `pnl` until voids, Task 9), `outcome_source`, `admitted_ms`.
  - `paper.cli.register(sub)`: `polydata paper run CONFIG.yaml [--out DIR] [--duration 6h] [--coins …] [--set k=v …] [--hold-back-ms N] [--skew-ms N] [--warmup-s N] [--max-drain 15m] [--confirm-wait 15m] [--allow-sealed --purpose TEXT]`. Fake flags come in Task 10.

**Run sequence** (`PaperRun.run`):
1. Refuse if `out` has a `paper.lock` held by a live PID. Warn if `not is_validated()`. On macOS, print a `caffeinate -i` hint.
2. Start the feeds. `rs` = (time all feeds first report `ws_open`) + warm-up, rounded up to the second. Refuse if `rs` falls on a sealed day without `--allow-sealed` (`record_look("paper", {...})` once with it).
3. Every `tick_ms`: `ingest.note_loop(now)`, `router.tick(now)`. Every 60 s, a status line to stdout and `log.txt` (`# open N orders N fills N pnl $x lag p99 N ms late N conns up/total`). Every `flush_s`, `writer.flush()`.
4. `admit_filter`: the market must not touch a sealed day (unless allowed) and must end at or before `rs + duration` when given.
5. Stop on duration end or the first SIGINT: `router.request_stop(router.T)`, keep ticking until `T ≥ end_ms` and all markets are resolved, or `max_drain_s` passes. Then `router.finish()`, write the manifest (`clean_stop: true`) and outputs. A second SIGINT is a hard stop: flush, manifest `clean_stop: false`, write what exists.
6. `runs.rebuild_index()`.

`PYTHONHASHSEED`: `paper.cli` re-execs `[sys.executable, "-m", "polydata.cli", *argv]` with `PYTHONHASHSEED=0` if it is unset or different.

- [ ] **Step 1: Write failing tests:**
  - `test_settler_up_down_and_tie`: `close_twap == ptb` → Up; `close_twap < ptb` → Down; `recv_ms` is the first tick with `ts ≥ we`.
  - `test_settler_waits_without_ticks`: an empty start window, or `ws − 60 s < rs` → no resolution.
  - `test_resolution_row_markets_sql`: write the row with `SessionWriter`, build → `markets.outcome`, `price_to_beat == ptb`, `final_price == close_twap`, `resolution_seen_at` = `r_end`.
  - `test_paper_run_fake_rows_end_to_end`: a `feeds_factory` that plays `synth.make_session` rows through `Ingest` under a virtual clock (minimal, before Task 10's full fake feed) with `--duration 20m`. The run folder has every file in the spec table. `provenance.kind == "paper"`. `session/manifest.json` has `rs`, `end_ms`, `clean_stop: true` and `admitted`. `runs.rebuild_index()` lists the run with `kind == "paper"`. `run_session` over the session gives parity with the run's files (the Task 5 comparison).
  - `test_refuses_sealed_day`: a virtual clock on 2026-11-25 → exits with "sealed". With `--allow-sealed --purpose x`, one `looks.jsonl` line.
  - `test_push_skips_sessions`: `runs._shared_files()` excludes `session/**` unless `with_sessions=True`.
  - `test_reexec_sets_hashseed`: `paper.cli.main` with `PYTHONHASHSEED` unset calls the patched `os.execvpe` with `PYTHONHASHSEED=0`.
- [ ] **Step 2:** Run `uv run pytest tests/paper/test_settle.py tests/paper/test_run.py -q`. Expect FAIL.
- [ ] **Step 3:** Implement.
- [ ] **Step 4:** Run `uv run pytest -q`. Expect PASS.
- [ ] **Step 5: Live dry run** (plumbing only, non-sealed day; 2026-11-26 is the next one, so if today is sealed, wait and record that in the commit message instead): `cd analysis && uv run polydata paper run configs/validation/endgame-maker-v2.yaml --duration 30m --coins btc eth`. Expected: the run folder exists, `results.json` shows `late` ≈ 0 and markets admitted, and `POLYDATA_DIR=<run>/session uv run polydata build` succeeds.
- [ ] **Step 6: Commit** `paper: polydata paper run (Chainlink settlement)`.

---

### Task 9: Gamma confirmation and fallback, Data API trades, post-hoc voids  (tier: standard)

**Files:**
- Create: `analysis/src/polydata/paper/confirm.py`, `analysis/src/polydata/paper/voids.py`
- Modify: `analysis/src/polydata/paper/runner.py`, `analysis/src/polydata/paper/results.py`
- Test: `analysis/tests/paper/test_confirm.py`, `analysis/tests/paper/test_voids.py`

**Interfaces:**
- Consumes: `Rest`, `Ingest`, `LiveRouter.unresolved/admitted`, `gaps.event_gaps/bn_silences/pm_silences/voided_markets`, `views.create_views`.
- Produces:
  - `confirm.Confirmer`, with `Confirmer(rest: Rest, ingest: Ingest, settler: ChainlinkSettler, clock)`:
    - `async poll() -> None`, run every 30 s. From `end + 30 s` it polls `/events?slug=` per market (rule as `resolveEnded`: closed, and `outcomePrices` has a 1), until answered or `confirm_wait` after the last market. Each answer → `paper/gamma_resolution` `{slug, outcome, recv_ms, event}`. If the market has no Chainlink outcome, a `pm_resolution` row with `source: "paper-gamma"` goes through `ingest.on_rows` (applied by the router at release).
    - From `end + 120 s` it fetches `{data}/v2/trades?condition_id=…&limit=1000&cursor=…` (pages buffered, written only when complete) → `pm_api_trades` rows `{slug, …trade}`.
  - `voids.session_voids(session: Path, markets: DataFrame, cfg: RunConfig, end_ms: int) -> DataFrame[slug, reason]`: builds the session store if needed, then `gap_table(con, rs, end_ms, events=<session catalog>)` → `voided_markets(...)`. It adds `late data (n rows, max x ms)` from `late/` (slug from `asset`, or condition id → slug) and `no outcome`. Stalls come through the `recorder` events.
  - `voids.apply(markets: DataFrame, voids: DataFrame) -> DataFrame` sets `pnl_raw = pnl`, then for voided slugs `voided = True`, `reason`, `pnl = NaN`.
  - `results` adds `gamma_outcome`, `gamma_seen_ms`, `outcome_mismatch`, `pnl_usd_gamma` and `errors` (a mismatch is an error) to `markets.parquet`/`results.json`. P&L keeps the Chainlink outcome.

- [ ] **Step 1: Write failing tests:**
  - `test_confirm_records_answers` (a stub `Rest` returning Gamma-shaped events)
  - `test_gamma_fallback` (Review Focus 5: no Chainlink outcome → a `paper-gamma` `pm_resolution` row at the answer's recv, settled through the router, and parity with the session replay)
  - `test_mismatch_reported` (Gamma Down vs Chainlink Up → `outcome_mismatch`, `pnl_usd_gamma` restated, an error in `results.json`)
  - `test_api_trades_written_once_after_complete_download`
  - `test_voids_late_rows`, `test_voids_ws_gap_pm_slug_and_series`, `test_voids_stall_recorder_events`, `test_voids_no_outcome`, `test_void_keeps_pnl_raw`
- [ ] **Step 2:** Run `uv run pytest tests/paper -q`. Expect FAIL on the new tests.
- [ ] **Step 3:** Implement, and wire `Confirmer` and the voids into `PaperRun`. Voids are applied after `finish()`, before writing.
- [ ] **Step 4:** Run `uv run pytest -q`. Expect PASS.
- [ ] **Step 5: Commit** `paper: Gamma confirmation/fallback, Data API trades, post-hoc voids`.

---

### Task 10: `polydata paper replay`, full fake feed, real-data parity  (tier: most capable)

**Files:**
- Create: `analysis/src/polydata/paper/fakefeed.py`, `analysis/src/polydata/paper/parity.py`
- Modify: `analysis/src/polydata/paper/cli.py`, `analysis/src/polydata/paper/runner.py`
- Test: `analysis/tests/paper/test_fakefeed.py`, `analysis/tests/paper/test_parity.py`, `analysis/tests/paper/realdata/test_parity_realdata.py`

**Interfaces:**
- Consumes: `run_session`, `SessionPlan`, `build`, `voids`, `create_views(include_sealed=True)`, `engine.run`.
- Produces:
  - `fakefeed.VirtualClock` with `now()`, `async sleep_until(ms)`, `speed: float | None` (None = `max`).
  - `fakefeed.FakeFeed`:
    - `FakeFeed.from_store(root: Path, start_ms: int, end_ms: int, coins, timeframes) -> FakeFeed` reads `raw/` TSV/JSONL files whose slot overlaps the range, as text in file order, and filters PM rows to config series (asset prefix or meta series), RTDS rows to config coins and Binance rows to config symbols. It also reads `catalog/*/rec_events.csv.zst` for events. Recorded `pm_resolution` rows are delivered to the `Confirmer` stub as Gamma answers (Decision 7).
    - `FakeFeed.from_session(session: Path) -> FakeFeed`.
    - `async run(ingest: Ingest, clock: VirtualClock) -> None` delivers rows grouped by recorded `recv_ms`, setting the clock to it before `on_rows`.
    - Fault injection: `delay(stream, predicate, ms)` (late path), `drop(source, from_ms, to_ms)` (drops that connection's rows and emits `ws_close`/`ws_open`), `stall(at_ms, ms)`.
  - `parity.compare(run_dir: Path, replay: RunResult) -> dict`: `{passed, verdict: "passed"|"failed"|"passed-truncated"|"informational", tables: {name: {rows_paper, rows_replay, mismatches}}, first_divergence: {budget, index, paper, replay, before: [10 decisions]}}`.
    - The comparison is exact per spec ("Parity check"): decisions, orders, fills, equity, `markets[MARKET_COLS]`, strategy log lines (lines not starting with `#`).
    - Truncated sessions (`clean_stop: false`) compare decisions with `now <` the last flushed recv and markets closed before it.
    - A different `host_hash` gives `informational`, with a 1e-9 relative tolerance count.
  - `paper.cli`:
    - `paper run … [--fake-from A --fake-to B | --fake-session DIR] [--speed N|max]`
    - `paper replay RUN_DIR [--vs-recorder] [--keep-parquet]`: build `session/parquet`, run `run_session` with the run's config, `trace=True`, apply `voids`, write `parity/` and `parity.json`, set `results.json["parity"]`, exit 1 if not passed.
    - `--vs-recorder` adds a normal `engine.run` over the default store clipped to `[rs, end_ms)` and writes `parity/vs_recorder.json` (decision/fill/P&L differences; never pass/fail).

- [ ] **Step 1: Write failing tests:**
  - `test_fake_session_builds_same_rows_as_source`: a fake run from a synthetic store → its session's Parquet rows equal the source store's rows for the run window and config filter.
  - `test_fake_speed_max_and_n`: at `speed=2`, virtual time advances 2× wall time (patched sleep); at `max`, it jumps.
  - `test_parity_cli_pass_and_fail`: `paper replay` on a fake run → `parity.json.passed` and exit 0. Tamper one fill in `fills.parquet` → exit 1, and `first_divergence.index` points at it.
  - `test_fault_late_voids_market`, `test_fault_drop_voids_market`, `test_fault_stall_voids_all` (parity still passes; voids as expected)
  - `test_truncated_session_passed_truncated` (a hard stop mid-run)
- [ ] **Step 2:** Run `uv run pytest tests/paper -q`. Expect FAIL on the new tests.
- [ ] **Step 3:** Implement.
- [ ] **Step 4: Real-data parity** (`test_parity_realdata.py`, `POLYDATA_REALDATA=1`). For each of the three validation configs with all 8 coins, a fake run from `$PD` 2026-11-24 11:00–13:00 at `max` (`rs` = 11:00 + warm-up), then `paper replay`, must give `passed`. `--vs-recorder` must report zero decision differences for these configs; any difference must be explained by load order or the open threshold and recorded in the test as a known-difference assertion with its cause.
  Run: `POLYDATA_REALDATA=1 POLYDATA_DIR=$PD uv run pytest tests/paper/realdata/test_parity_realdata.py -q -s`. Expect PASS. Print elapsed and speed.
- [ ] **Step 5:** Run `uv run pytest -q` and the identity check (`identical: 7/7`).
- [ ] **Step 6: Commit** `paper: paper replay, fake feed, real-data parity`.

---

### Task 11: Performance and first live run  (tier: standard)

**Files:**
- Create: `analysis/tools/paper_bench.py`
- Modify: `docs/strategies/endgame-maker.md` (journal line), `docs/runs-index.csv` (via `rebuild_index`)
- Test: `analysis/tests/paper/test_bench.py` (smoke only)

**Interfaces:**
- Consumes: `FakeFeed`, `PaperRun`, parsers.
- Produces: `paper_bench.py parsers` prints messages/s per parser. `paper_bench.py fake --from A --to B` prints the speed × real time through the full ingest path, CPU %, RSS and processing lag p50/p99.

- [ ] **Step 1:** `test_bench_smoke`: `paper_bench.main(["parsers", "--n", "1000"])` returns a dict with every parser > 0 msg/s.
- [ ] **Step 2:** Measure:
  - `uv run python tools/paper_bench.py parsers`. Expected: ≥ 20,000 msg/s each on one core.
  - `POLYDATA_DIR=$PD uv run python tools/paper_bench.py fake --from "2026-11-24 12:00" --to "2026-11-24 13:00"` (busiest recorded hour; pick it from `bn_fut_bbo` row counts first). Expected: ≥ 5× real time.
  - If either misses: `orjson`, then `uvloop`, then a separate ingest process, each as its own commit with the numbers in the message.
- [ ] **Step 3: One-hour live measurement** on a non-sealed day: `uv run polydata paper run configs/validation/endgame-maker-v2.yaml --duration 1h`. Record rates per stream, CPU (≤ 50 % of one core), event-loop lag p99 (≤ 20 ms), processing lag p99 (≤ 200 ms), RSS (≤ 1.5 GB) and session size per hour.
- [ ] **Step 4: First real paper run:** a 6 h `paper run` of `endgame-maker-v2` on a non-sealed day, then `uv run polydata paper replay <run>`. Expected: `parity.json.passed == true`. Add one journal line to `docs/strategies/endgame-maker.md` (run id, hours, markets, voided, P&L, parity, late rate). Run `uv run polydata bt runs` to refresh `docs/runs-index.csv`.
- [ ] **Step 5: Commit** `paper: benchmarks; first paper run of endgame-maker-v2`.

---

### Task 12: Docs  (tier: cheap)

**Files:**
- Create: `docs/PAPER.md`
- Modify: `docs/BACKTESTER.md` (link + one paragraph on session-mode replay and `run_session`), `CLAUDE.md` (Layout: `analysis/src/polydata/paper/`; Rules: no real orders; parity before trusting a paper result), `docs/TODO.md` (item 3: mark the runner done, keep "compare paper fills with the backtest" open with the `--vs-recorder` pointer)

- [ ] **Step 1:** Write `docs/PAPER.md`: how to run (`paper run`, flags, `caffeinate -i`), the hold-back and why the strategy is 4 s late, the run folder and session layout (copy the spec table), parity (what passes, truncated, informational), voids, the fake feed, limitations (host location, fill model unverified, voided markets still move cash), and sealed-day rules. No hostnames or IPs.
- [ ] **Step 2:** Check that every command in `PAPER.md` matches `uv run polydata paper run --help` / `replay --help`.
- [ ] **Step 3: Commit** `docs: paper runner`.

---

### Task 13: gaps.py source-name fix — **separate PR**  (tier: standard)

Branch `gaps-source-names` from `main` after the paper runner merges. Never mix it into the paper-runner PR.

**Files:**
- Modify: `analysis/src/polydata/bt/gaps.py` (`event_gaps.emit`, `voided_markets`), `docs/RECORDER.md` (data quality log line), the journals of strategies whose runs change
- Test: `analysis/tests/bt/test_gaps.py`

**Interfaces:**
- Produces:
  - `event_gaps` maps `binance-fut-bbo` (and keeps `binance-fut-public`) to every symbol; `polymarket-rtds-a`/`-b` (and `polymarket-rtds`) to `("cl", "*")`.
  - A `pm:<x>` gap keeps key `x`. `voided_markets` matches a pm gap when `key ∈ {series, slug, "*"}`.

- [ ] **Step 1: Failing tests:** `test_binance_fut_bbo_close_voids_all_syms`, `test_rtds_a_close_is_cl_gap`, `test_pm_slug_gap_voids_that_market_only` (a `pm:<slug>` source voids that market and not its series siblings), `test_legacy_names_still_map`.
- [ ] **Step 2:** Run `uv run pytest tests/bt/test_gaps.py -q`. Expect FAIL. Implement. Expect PASS, and `uv run pytest -q` green.
- [ ] **Step 3:** Run `POLYDATA_DIR=$PD uv run python tools/replay_identity.py check --ref $PD/identity/ref`. Differences are expected only where such events exist. List each changed case and its newly voided markets in the PR description, then save a new reference (`save --out $PD/identity/ref-gaps`).
- [ ] **Step 4:** Run `POLYDATA_DIR=$PD uv run polydata bt validate`. Expected: `PASSED` at the new `sim_version`.
- [ ] **Step 5:** Add a data quality log line to `docs/RECORDER.md` (which source names were unmatched and since when) and journal notes for affected strategies.
- [ ] **Step 6: Commit** `bt: gaps maps recorder source names (binance-fut-bbo, rtds-a/b, pm:<slug>)`, then open its own PR.

---

## Final verification (before the plan is complete)

- [ ] `cd analysis && uv run pytest -q` → all pass.
- [ ] `POLYDATA_REALDATA=1 POLYDATA_DIR=$PD uv run pytest tests/bt/realdata tests/paper/realdata -q` → all pass.
- [ ] `POLYDATA_DIR=$PD uv run python tools/replay_identity.py check --ref $PD/identity/ref` → `identical: 7/7` (paper-runner branch, before Task 13).
- [ ] `POLYDATA_DIR=$PD uv run polydata bt validate` → `PASSED` for the current `sim_version`.
- [ ] `git grep -nE '([0-9]{1,3}\.){3}[0-9]{1,3}|SPACES_(KEY|SECRET)|BEGIN .*PRIVATE'` over the diff → nothing new.

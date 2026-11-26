"""The live driver: feeds a `Core` per budget from rows as they arrive, so that it makes exactly the calls the
session-mode replay (`engine.run_session`) makes over the same rows (spec "The hold-back", "Market admission").

**Rows.** `LiveRouter.ingest` takes one `LiveRow` at a time, in nondecreasing `recv_ms` (stamping is the caller's
job). It classifies the row ("ok", "late", "future"), numbers "ok" rows per stream (`seq`, their arrival order, as
the Parquet build numbers them) and buffers what the core needs:

- Polymarket rows of the `PM_TS_STREAMS` and `tick_size_change` messages are released on the **ts clock**: at
  `tick(wall)`, `H = wall - hold_back_ms`, every buffered row with `ts_ms <= H` goes into its market's
  `GrowingMarketData` (`add_ts`, one whole `ts_ms` at a time, in `(ts_ms, slug)` order). A row is *late* if its
  `ts_ms <= H` when it arrives, *future-stamped* if `ts_ms > recv_ms + skew_ms`; both are dropped (listed in
  `late`), never buffered; so is one of them received at or before `H` (a broken stamp). Any other row received at
  or before `H` raises: only these rows can be late.
- Binance rows are bucketed (`LiveBn`, one per hour and symbol) up to `H` as well, so every Binance lookup the core
  can make (fill times included) is answered from complete buckets; `K_BN` events are still dispatched on the
  strategy clock.
- Everything else (market discovery, resolutions, Chainlink) is processed on the **strategy clock**
  `T = H - skew_ms`, together with the recv side of every market (`md.release_recv`).

**A tick** (`tick(wall)`) releases the ts side, then the strategy clock interval `(T_prev, T]`, split at every hour
boundary into pieces `[a, b]`. For each piece, in order: the markets' recv sides up to `b`; the rows received in
`[a, b]` in arrival order (admissions, resolutions, Chainlink ticks: first receipt per `ts_ms`, kept from `rs`); the
settler; `scan_ptb`; then the piece's data events (`K_ADMIT`, `K_OPEN`, `K_TOP`, `K_TRADE`, `K_CL`, `K_BN`, recv
`< end_ms`), sorted with the replay's key and dispatched. At an hour boundary `h`: drain to `h`, rotate the Binance
hour, start a grid segment and `mark_hour(h)`, exactly where the replay ends one segment and starts the next.
Finally the core drains to `T + 1`. Everything the core does in between (partial drains, pushes with
push-time-independent keys, trades appended above `T`) gives the same event sequence as the replay's hour-at-once
processing; see `docs/superpowers/plans/2026-11-25-paper-runner.md` ("Decisions beyond the spec") and
`engine.run_session`.

**Admission.** A `pm_market_meta` row is parsed when it is ingested (`info_from_meta`), so the market's
`GrowingMarketData` exists before its first Polymarket row is released. It is admitted when the row is processed on
the strategy clock, at `max(recv_ms, rs)`, if its coin and timeframe are in the config, `window_start_ms >= rs`, its
window has not ended, the router is not stopping, no row of it arrived before its discovery, and `admit_filter`
allows it. The admission order is the load order (`Core.stage`), recorded in `admitted` for the session manifest.

**Stop.** `request_stop(t)` sets `end_ms` (rounded up to the second, never before what was already dispatched, never
before the last admitted window end) and stops admissions. Data events at or after `end_ms` are never dispatched and
the core never drains past `end_ms`; rows keep being released so resolutions (and the settler) still apply.
`finish()` = `finish_unresolved()` + `Core.finish(end_ms)` per budget once `T >= end_ms - 1`; before that (a hard
stop) it processes nothing beyond `T` and sets `clean_stop = False`. A resolution counts only if received at or after
its market's admission.
"""

from __future__ import annotations

import heapq
import logging
import time
from collections import deque
from collections.abc import Callable
from typing import Literal, NamedTuple

import numpy as np

from .config import RunConfig
from .core import _RANK, HOUR_MS, K_ADMIT, K_BN, K_CL, K_OPEN, K_TOP, K_TRADE, Core, RunResult, _Bn
from .exchange import ExchangeConfig
from .marketdata import MARGIN_MS
from .marketdata_live import GrowingMarketData, LiveBn, LiveCl, LiveGrid, info_from_meta
from .strategy import get_strategy
from .types import MarketInfo

log = logging.getLogger(__name__)

PM_TS_STREAMS = ("pm_book", "pm_price_change", "pm_best_bid_ask", "pm_trade")
_MD_INDEX = {"pm_price_change": 0, "pm_book": 1, "pm_trade": 2}  # GrowingMarketData.add_ts argument; ticks: 3
_NEVER = -1  # open_ms of a market whose first snapshot after admission is at or after its window end
_FAR = -(2**62)


class LiveRow(NamedTuple):
    """One recorder row as received. TSV streams: `values` are the recorder columns after `recv_ms`, as text
    (`schema.TSV` order). JSONL streams: the object (the recorder's `d`; for `pm_other`, the message)."""
    stream: str
    recv_ms: int
    values: tuple | dict


class Resolution(NamedTuple):
    slug: str
    outcome: str
    recv_ms: int
    source: str
    ptb: float | None = None
    close_twap: float | None = None


def _is_tick_change(row: LiveRow) -> bool:
    return row.stream == "pm_other" and isinstance(row.values, dict) and row.values.get("event_type") == "tick_size_change"


def is_late_candidate(row: LiveRow) -> bool:
    """Rows used on the exchange clock (plan Decision 5): they can be late or future-stamped."""
    return row.stream in PM_TS_STREAMS or _is_tick_change(row)


def _ts_of(row: LiveRow) -> int:
    return int(row.values["timestamp"]) if row.stream == "pm_other" else int(row.values[0])


def _ceil(x: int, step: int) -> int:
    return -(-int(x) // step) * step


def _truncate(bn, end_ms: int) -> _Bn:
    """A static copy of the bundles received before end_ms."""
    b = bn.bn if isinstance(bn, LiveBn) else bn
    k = int(np.searchsorted(b.recv, end_ms))
    return _Bn(*(a[:k].copy() for a in b))


class _Adm:
    """An admitted market of a LiveSource."""
    __slots__ = ("slug", "mi", "md", "admitted_ms", "end", "open_ms")

    def __init__(self, slug: str, mi: int, md: GrowingMarketData, admitted_ms: int):
        self.slug, self.mi, self.md, self.admitted_ms = slug, mi, md, admitted_ms
        self.end = int(md.info.window_end_ms)
        self.open_ms: int | None = None  # None: no snapshot at or after admission yet; _NEVER: none before the end


# ---------------------------------------------------------------------------------------- per budget

class LiveSource:
    """One budget's live state: its Core, the markets of its coins (by slug and condition id), Binance buckets per hour
    and symbol, the 1 s grid, Chainlink ticks, and the strategy clock position `T_prev`."""

    def __init__(self, core: Core, coins: list[str], rs: int):
        self.core, self.coins, self.rs = core, list(coins), int(rs)
        self.syms = list(core.syms)
        self.mds: dict[str, GrowingMarketData] = {}  # candidate markets (discovered), by slug
        self.by_cond: dict[str, str] = {}
        self.adm: list[_Adm] = []  # admitted, in load order
        self.adm_by_slug: dict[str, _Adm] = {}
        self._new_admits: list[_Adm] = []  # admitted in the current piece (K_ADMIT events)
        self.cl = {c: LiveCl(rs) for c in self.coins}
        self.bn: dict[int, dict[str, LiveBn]] = {}  # hour start -> sym -> buckets
        self.grid = {x: LiveGrid() for x in self.syms}
        self.hour: int | None = None  # start of the current hour segment (strategy clock)
        self.seg_from: int | None = None
        self._grid_key = None
        self._sealed = False  # bn_cur replaced by bundles before end_ms
        self.started = False
        self.T_prev = self.rs - 1

    # -- Binance (horizon H)
    def bns(self, h: int) -> dict[str, LiveBn]:
        d = self.bn.get(h)
        if d is None:
            d = self.bn[h] = {x: LiveBn(self.core.cfg.bn_step_ms) for x in self.syms}
        return d

    def add_bn(self, recv: int, sym: str, v: tuple) -> None:
        self.bns(recv // HOUR_MS * HOUR_MS)[sym].add(recv, int(v[1]), float(v[4]), float(v[5]), float(v[6]),
                                                      float(v[7]))

    def flush_bn(self, H: int) -> None:
        for d in self.bn.values():
            for b in d.values():
                b.flush(H)

    def refresh_grid(self, H: int, end_ms: int | None) -> None:
        """Recompute the current grid segment from the complete seconds (all of them once every bundle of the segment
        is in), as `_Grid.append` would over the segment `[seg_from, min(next hour, end_ms))`."""
        if self.hour is None or self._sealed:
            return
        e = self.hour + HOUR_MS if end_ms is None else min(self.hour + HOUR_MS, end_ms)
        final = H >= e - 1
        sec_done = -(-e // 1000) if final else min((H + 1) // 1000, -(-e // 1000))
        key = (self.hour, sec_done, final, end_ms)
        if key == self._grid_key:
            return
        self._grid_key = key
        cur = self.bns(self.hour)
        seal = final and end_ms is not None and e == end_ms
        for x in self.syms:
            bn = cur[x]
            if seal:  # the replay's last segment holds the bundles received before end_ms only
                bn = _truncate(bn, end_ms)
                self.core.bn_cur[x] = bn
            self.grid[x].set_bundles(bn, sec_done)
        self._sealed = seal

    # -- strategy clock
    def start(self, H: int, end_ms: int | None) -> None:
        """`Core.start(rs)` (before any data is visible, as in the replay), then the first hour: Binance, grid,
        `mark_hour(rs)`."""
        core, rs = self.core, self.rs
        core.start(rs)
        core.bn_prev, core.bn_cur = {}, dict(self.bns(rs // HOUR_MS * HOUR_MS))
        core.grid = dict(self.grid)
        self.hour, self.seg_from = rs // HOUR_MS * HOUR_MS, rs
        for g in self.grid.values():
            g.start_segment(rs // 1000)
        self.refresh_grid(H, end_ms)
        core.mark_hour(rs)
        self.started = True

    def rotate(self, h: int, H: int, end_ms: int | None) -> None:
        """Hour boundary h (the core is drained to h): next Binance hour, grid segment, `mark_hour(h)`."""
        core = self.core
        self.refresh_grid(H, end_ms)  # completes the old segment (H >= h - 1)
        core.bn_prev, core.bn_cur = core.bn_cur, dict(self.bns(h))
        for k in [k for k in self.bn if k < h]:
            del self.bn[k]  # core.bn_prev keeps the previous hour
        self.hour, self.seg_from, self._grid_key = h, h, None
        for g in self.grid.values():
            g.start_segment(h // 1000)
        self.refresh_grid(H, end_ms)
        core.mark_hour(h)

    def release_md(self, t: int, end_ms: int | None = None) -> None:
        if end_ms is not None:
            t = min(t, end_ms - 1)  # nothing received at or after end_ms reaches the strategy's arrays
        for md in self.mds.values():
            md.release_recv(t)

    def set_cl(self, end_ms: int | None) -> None:
        for c, lc in self.cl.items():
            arr = lc.arrays
            if end_ms is not None and len(arr[0]) and arr[0][-1] >= end_ms:
                k = int(np.searchsorted(arr[0], end_ms))
                arr = tuple(a[:k] for a in arr)
            self.core.cl[c] = arr

    def stage(self, slug: str, md: GrowingMarketData, t: int) -> _Adm:
        mi = self.core.stage(md.info, md, self.rs, t)
        a = _Adm(slug, mi, md, t)
        self.adm.append(a)
        self.adm_by_slug[slug] = a
        self._new_admits.append(a)
        return a

    def dispatch_piece(self, a: int, b: int, end_ms: int | None) -> None:
        """Dispatch the data events with recv in [a, b] (and < end_ms), sorted as `ReplayDriver._segment` sorts them."""
        core = self.core
        lim = b + 1 if end_ms is None else min(b + 1, end_ms)
        new, self._new_admits = self._new_admits, []
        if lim <= a:
            return
        recv, kind, mi, idx = [], [], [], []

        def add(r, k, m, i):
            recv.append(r)
            kind.append(np.full(len(r), k, np.int8))
            mi.append(np.full(len(r), m, np.int32))
            idx.append(i)

        for x in new:
            add(np.array([x.admitted_ms], np.int64), K_ADMIT, x.mi, np.array([-1]))
        want = core.want
        for x in self.adm:
            m = core.mkts[x.mi]
            if m.closed or x.open_ms == _NEVER:
                continue
            md = x.md
            if x.open_ms is None:
                sn = md.snaps_recv
                i = int(np.searchsorted(sn, x.admitted_ms))
                if i >= len(sn):
                    continue
                x.open_ms = int(sn[i]) if sn[i] < x.end else _NEVER
                if x.open_ms == _NEVER:
                    continue
            if not m.opened and a <= x.open_ms < lim:
                add(np.array([x.open_ms], np.int64), K_OPEN, x.mi, np.array([-1]))
            lo, hi = max(a, x.open_ms), min(lim, x.end)
            if lo >= hi:
                continue
            if want["on_book"]:
                tr = md.tops_recv.recv_ms
                i0, i1 = (int(v) for v in np.searchsorted(tr, [lo, hi]))
                if i1 > i0:
                    add(tr[i0:i1], K_TOP, x.mi, np.arange(i0, i1))
            if want["on_trade"]:
                rr = md.trades_recv.recv_ms
                i0, i1 = (int(v) for v in np.searchsorted(rr, [lo, hi]))
                if i1 > i0:
                    add(rr[i0:i1], K_TRADE, x.mi, np.arange(i0, i1))
        bn_lists = []
        if want["on_binance"]:
            for j, s in enumerate(self.syms):
                bn = core.bn_cur[s]
                r = bn.recv
                i0, i1 = (int(v) for v in np.searchsorted(r, [a, lim]))
                bn_lists.append((s, bn.bid[i0:i1].tolist(), bn.ask[i0:i1].tolist(), bn.bid_qty[i0:i1].tolist(),
                                 bn.ask_qty[i0:i1].tolist()))
                if i1 > i0:
                    add(r[i0:i1], K_BN, j, np.arange(i1 - i0))
        cl_lists = []
        if want["on_chainlink"]:
            for j, c in enumerate(self.coins):
                r, _, px = self.cl[c].arrays
                i0, i1 = (int(v) for v in np.searchsorted(r, [a, lim]))
                cl_lists.append((c, px[i0:i1].tolist()))
                if i1 > i0:
                    add(r[i0:i1], K_CL, j, np.arange(i1 - i0))
        if recv:
            R_ = np.concatenate(recv).astype(np.int64)
            K_ = np.concatenate(kind)
            M_ = np.concatenate(mi)
            I_ = np.concatenate(idx).astype(np.int64)
            o = np.lexsort((I_, M_, K_, _RANK[K_], R_))
            core.dispatch(R_[o].tolist(), K_[o].tolist(), M_[o].tolist(), I_[o].tolist(), bn_lists, cl_lists)

    def forget_before(self, t: int) -> None:
        """Drop markets that can no longer be queried (window end + margin before t; admitted ones are closed)."""
        for slug in [s for s, md in self.mds.items() if int(md.info.window_end_ms) + MARGIN_MS < t]:
            md = self.mds.pop(slug)
            self.by_cond.pop(md.info.condition_id, None)
            a = self.adm_by_slug.get(slug)
            if a is not None:
                a.md = None
        self.adm = [x for x in self.adm if x.md is not None]


# ---------------------------------------------------------------------------------------- router

class LiveRouter:
    """Classifies, buffers and releases rows for every budget (module docstring). Per-coin mode: one `LiveSource` per
    coin (budget `capital / len(coins)`, its own strategy instance and RNG, as `engine.run_coin`); global mode: one
    `LiveSource` "*". `settler` (Task 8): `admit(info, admitted_ms)` and `on_tick(coin, recv_ms, ts_ms, price) ->
    list[Resolution]`, called with every Chainlink tick the core keeps (first receipt, received at or after rs)."""

    def __init__(self, cfg: RunConfig, rs: int, *, hold_back_ms: int = 10_000, skew_ms: int = 1000, trace: bool = True,
                 admit_filter: Callable[[MarketInfo, int], bool] | None = None, settler=None):
        hold_back_ms, skew_ms = int(hold_back_ms), int(skew_ms)
        if hold_back_ms <= 0 or skew_ms < 0:
            raise ValueError("hold_back_ms must be > 0 and skew_ms >= 0")
        explain = ExchangeConfig().trade_explain_ms
        if skew_ms + int(cfg.fill_report_latency_ms) < explain:
            # Exchange._explained reads trades up to T - R + trade_explain_ms on the ts clock: that must be <= H_ts
            raise ValueError(f"skew_ms + fill_report_latency_ms must be >= {explain} (trade_explain_ms)")
        self.cfg, self.rs = cfg, int(rs)
        self.hold_back_ms, self.skew_ms = hold_back_ms, skew_ms
        self.admit_filter, self.settler = admit_filter, settler
        self.timeframes = set(cfg.timeframes)
        self.sources: dict[str, LiveSource] = {}
        if cfg.capital_mode == "global":
            core = Core(cfg, list(cfg.coins), {"*": cfg.capital}, get_strategy(cfg.strategy)(cfg.params), trace=trace,
                        defer_settle=True)
            self.sources["*"] = LiveSource(core, list(cfg.coins), self.rs)
        else:
            budget = cfg.capital / len(cfg.coins)
            for c in cfg.coins:
                core = Core(cfg, [c], {c: budget}, get_strategy(cfg.strategy)(cfg.params), trace=trace,
                            defer_settle=True)
                self.sources[c] = LiveSource(core, [c], self.rs)
        self._src_of_coin = {c: s for s in self.sources.values() for c in s.coins}
        self._src_of_sym = {x: s for s in self.sources.values() for x in s.syms}

        self.H_ts = _FAR
        self.T = _FAR - skew_ms
        self.end_ms: int | None = None
        self.stopping = False
        self.clean_stop: bool | None = None  # set by finish()
        self.admitted: list[dict] = []
        self.late: list[tuple[LiveRow, int]] = []
        self.new_resolutions: list[Resolution] = []
        self.stats: dict = {"rows": {}, "late": 0, "future": 0, "orphan_rows": 0, "ignored_resolutions": 0,
                            "duplicate_resolutions": 0, "meta_changed": 0, "bad_meta": 0}
        self._last_recv = _FAR
        self._done = _FAR  # strategy-clock rows processed up to here
        self._seq: dict[str, int] = {}
        self._known: dict[str, MarketInfo] = {}  # every discovered market (first meta row)
        self._md_src: dict[str, LiveSource] = {}  # candidate slug -> its source
        self._cond: dict[str, str] = {}  # condition id -> slug
        self._orphans: set[str] = set()  # slugs (and condition ids) with a row before their discovery
        self._resolved: set[str] = set()
        self._ts_heap: list[int] = []
        self._ts_buf: dict[int, list[tuple]] = {}
        self._fifo: deque[LiveRow] = deque()  # strategy-clock rows, in arrival order
        self._bn_fifo: deque[LiveRow] = deque()  # Binance rows (released at H)
        self._pre_admit: list[tuple[MarketInfo, int]] = []  # discovered before rs: admitted at rs
        self._started = False
        self._t0 = time.perf_counter()

    # ------------------------------------------------------------------------------- ingest
    def ingest(self, row: LiveRow) -> Literal["ok", "late", "future"]:
        recv = int(row.recv_ms)
        if recv < self._last_recv:
            raise ValueError(f"recv_ms went back: {recv} after {self._last_recv} ({row.stream})")
        self._last_recv = recv
        rows = self.stats["rows"]
        rows[row.stream] = rows.get(row.stream, 0) + 1
        if recv <= self.H_ts and not is_late_candidate(row):
            # Only Polymarket exchange-clock rows can be late; anything else stamped at or behind the released horizon
            # is a stamping bug (stamp recv_ms = max(previous, clock, last tick's wall)).
            raise ValueError(f"{row.stream} row received at {recv}, at or before the released horizon {self.H_ts}")
        if recv <= self.H_ts:  # stamped behind what was already released: nothing may use it
            return self._drop(row, self.H_ts - recv, "late")
        if is_late_candidate(row):
            ts = _ts_of(row)
            if ts > recv + self.skew_ms:
                return self._drop(row, ts - recv - self.skew_ms, "future")
            if ts <= self.H_ts:
                return self._drop(row, self.H_ts - ts, "late")
            seq = self._next_seq(row.stream)
            if row.stream == "pm_best_bid_ask":
                return "ok"  # recorded, not used by the simulator
            self._buffer_pm(row, ts, seq)
            return "ok"
        self._next_seq(row.stream)
        st = row.stream
        if st == "bn_fut_bbo":
            if row.values[0] in self._src_of_sym:
                self._bn_fifo.append(row)
        elif st == "rtds_price":
            v = row.values
            if v[0] == "crypto_prices_chainlink" and v[1].split("/")[0] in self._src_of_coin:
                self._fifo.append(row)
        elif st == "pm_market_meta":
            if self._discover(row):
                self._fifo.append(row)
        elif st == "pm_resolution":
            self._fifo.append(row)
        return "ok"

    def _next_seq(self, stream: str) -> int:
        n = self._seq.get(stream, 0)
        self._seq[stream] = n + 1
        return n

    def _drop(self, row: LiveRow, by: int, kind: str) -> str:
        self.late.append((row, int(by)))
        self.stats[kind] += 1
        return kind

    def _buffer_pm(self, row: LiveRow, ts: int, seq: int) -> None:
        if row.stream == "pm_other":
            cond = row.values.get("market")
            slug = self._cond.get(cond)
            if slug is None:
                self._orphans.add(cond)
                self.stats["orphan_rows"] += 1
                return
            entry = (slug, 3, (int(row.recv_ms), float(row.values["new_tick_size"])))
        else:
            slug = row.values[1].split("|", 1)[0]
            if slug not in self._known:
                self._orphans.add(slug)
                self.stats["orphan_rows"] += 1
                return
            entry = (slug, _MD_INDEX[row.stream], (int(row.recv_ms), seq, row.values))
        if slug not in self._md_src:
            return  # a market this run never admits
        b = self._ts_buf.get(ts)
        if b is None:
            b = self._ts_buf[ts] = []
            heapq.heappush(self._ts_heap, ts)
        b.append(entry)

    def _discover(self, row: LiveRow) -> bool:
        """Parse a discovery row; create the market's GrowingMarketData if this run could admit it. True if the row
        must be processed on the strategy clock (admission)."""
        try:
            info = info_from_meta(row.values, row.recv_ms)
        except (ValueError, KeyError, TypeError) as e:
            self.stats["bad_meta"] += 1
            log.warning("pm_market_meta at %d ignored: %s", row.recv_ms, e)
            return False
        old = self._known.get(info.slug)
        if old is not None:
            if old != info:
                self.stats["meta_changed"] += 1
                log.warning("%s: a later discovery row differs from the first (kept): %s", info.slug, info)
            return False
        self._known[info.slug] = info
        self._cond[info.condition_id] = info.slug
        if info.condition_id in self._orphans:
            self._orphans.add(info.slug)
        src = self._src_of_coin.get(info.coin)
        if src is None or info.timeframe not in self.timeframes or info.window_start_ms < self.rs:
            return False
        src.mds[info.slug] = GrowingMarketData(info)
        src.by_cond[info.condition_id] = info.slug
        self._md_src[info.slug] = src
        return True

    # ------------------------------------------------------------------------------- tick
    def tick(self, wall_ms: int) -> None:
        """Advance the horizons to `wall_ms` (module docstring). `H_ts` and `T` are the new ones from the start."""
        H = max(self.H_ts, int(wall_ms) - self.hold_back_ms)
        T = H - self.skew_ms
        self.H_ts, self.T = H, T
        self._release_ts(H)
        self._release_bn(H)
        self._release_recv(T)
        if self._started:
            lim = T + 1 if self.end_ms is None else min(T + 1, self.end_ms)
            for s in self.sources.values():
                s.core._drain(lim)

    def _release_ts(self, H: int) -> None:
        heap, buf = self._ts_heap, self._ts_buf
        while heap and heap[0] <= H:
            ts = heapq.heappop(heap)
            groups: dict[str, list[list]] = {}
            for slug, k, r in buf.pop(ts):
                g = groups.get(slug)
                if g is None:
                    g = groups[slug] = [[], [], [], []]
                g[k].append(r)
            for slug in sorted(groups):
                src = self._md_src.get(slug)
                md = None if src is None else src.mds.get(slug)
                if md is None:
                    continue  # forgotten (window long over)
                g = groups[slug]
                if md.add_ts(ts, g[0], g[1], g[2], g[3]):
                    a = src.adm_by_slug.get(slug)
                    if a is not None:
                        src.core.on_trades_appended(a.mi)

    def _release_bn(self, H: int) -> None:
        q = self._bn_fifo
        rs, end = self.rs, self.end_ms
        while q and q[0].recv_ms <= H:
            row = q.popleft()
            r = int(row.recv_ms)
            if r < rs or (end is not None and r >= end):
                continue
            self._src_of_sym[row.values[0]].add_bn(r, row.values[0], row.values)
        for s in self.sources.values():
            s.flush_bn(H)
            if s.started:
                s.refresh_grid(H, end)

    def _release_recv(self, T: int) -> None:
        while self._done < T:
            if not self._started:
                if T < self.rs:
                    self._warm(T)
                    return
                self._warm(self.rs - 1)
                self._start()
                continue
            a = self._done + 1
            h = (a // HOUR_MS + 1) * HOUR_MS
            b = min(T, h - 1)
            self._piece(a, b)
            if b + 1 == h:
                self._boundary(h)

    def _warm(self, t: int) -> None:
        """Before rs: the markets' recv sides, discoveries (admitted at rs) and Chainlink first receipts."""
        for s in self.sources.values():
            s.release_md(t, self.end_ms)
        cl: dict[LiveSource, dict[str, list]] = {}
        q = self._fifo
        while q and q[0].recv_ms <= t:
            row = q.popleft()
            if row.stream == "pm_market_meta":
                self._pre_admit.append((self._known[row.values["event"]["slug"]], int(row.recv_ms)))
            elif row.stream == "rtds_price":
                self._cl_row(cl, row)
            elif row.stream == "pm_resolution":
                self._resolution_row(row)
        for src, d in cl.items():
            for c, rows in d.items():
                src.cl[c].add(rows)  # only marks them seen: kept from rs
        self._done = max(self._done, t)

    def _start(self) -> None:
        for s in self.sources.values():
            s.start(self.H_ts, self.end_ms)
        self._started = True

    def _cl_row(self, cl: dict, row: LiveRow) -> None:
        v = row.values
        c = v[1].split("/")[0]
        src = self._src_of_coin[c]
        cl.setdefault(src, {}).setdefault(c, []).append((int(row.recv_ms), int(v[2]), float(v[3])))

    def _resolution_row(self, row: LiveRow) -> None:
        d = row.values
        if not isinstance(d, dict) or d.get("outcome") is None or d.get("slug") is None:
            self.stats["ignored_resolutions"] += 1
            return
        self.apply_resolution(Resolution(str(d["slug"]), str(d["outcome"]), int(row.recv_ms),
                                         str(d.get("source") or "pm_resolution")))

    def _piece(self, a: int, b: int) -> None:
        """Strategy-clock rows received in [a, b] (one hour at most), then the piece's data events."""
        end = self.end_ms
        for s in self.sources.values():
            s.release_md(b, end)
        if self._pre_admit:
            for info, recv in self._pre_admit:
                self._admit(info, recv)
            self._pre_admit = []
        cl: dict[LiveSource, dict[str, list]] = {}
        res_rows = []
        q = self._fifo
        while q and q[0].recv_ms <= b:
            row = q.popleft()
            st = row.stream
            if st == "rtds_price":
                self._cl_row(cl, row)
            elif st == "pm_market_meta":
                self._admit(self._known[row.values["event"]["slug"]], int(row.recv_ms))
            elif st == "pm_resolution":
                res_rows.append(row)
        for row in res_rows:  # after the piece's admissions: the rule is recv >= admitted_ms, whatever the row order
            self._resolution_row(row)
        live = end is None or a < end
        for src, d in cl.items():
            kept = False
            for c, rows in d.items():
                n = src.cl[c].add(rows)
                if not n:
                    continue
                kept = True
                if self.settler is not None:
                    r_, ts_, px_ = (x[-n:] for x in src.cl[c].arrays)
                    for r, ts, px in zip(r_.tolist(), ts_.tolist(), px_.tolist()):
                        for res in self.settler.on_tick(c, r, ts, px) or ():
                            self.new_resolutions.append(res)
                            self.apply_resolution(res)
            if kept and live:
                src.set_cl(end)
                src.core.scan_ptb()
        self._done = b  # rows of [a, b] are processed: a resolution must come after this
        if live:
            for s in self.sources.values():
                s.dispatch_piece(a, b, end)
        for s in self.sources.values():
            s.T_prev = b

    def _boundary(self, h: int) -> None:
        end = self.end_ms
        lim = h if end is None else min(h, end)
        for s in self.sources.values():
            s.core._drain(lim)
            if end is None or h < end:
                s.rotate(h, self.H_ts, end)
            s.forget_before(h)
        for slug in [k for k, s in self._md_src.items() if k not in s.mds]:
            del self._md_src[slug]

    def _admit(self, info: MarketInfo, recv: int) -> None:
        t = max(int(recv), self.rs)
        slug = info.slug
        src = self._md_src.get(slug)
        if src is None or slug in src.adm_by_slug:
            return
        if (self.stopping or slug in self._orphans or info.window_end_ms <= t
                or (self.admit_filter is not None and not self.admit_filter(info, t))):
            return
        a = src.stage(slug, src.mds[slug], t)
        key = next(k for k, s in self.sources.items() if s is src)
        self.admitted.append({"slug": slug, "admitted_ms": t, "load_index": a.mi, "budget": key})
        if self.settler is not None:
            self.settler.admit(info, t)

    # ------------------------------------------------------------------------------- resolutions / stop
    def apply_resolution(self, res: Resolution) -> None:
        """Tell the market's core its outcome, received at `res.recv_ms` (which must lie after what was processed).
        A resolution counts only for an admitted market and if received at or after its admission (the session replay
        applies the same rule); only the first one counts (Core.set_resolution would overwrite)."""
        if res.recv_ms <= self._done:
            raise ValueError(f"{res.slug}: resolution at {res.recv_ms} is behind the processed time {self._done}")
        src = self._md_src_admitted(res.slug)
        if src is None or res.recv_ms < src.adm_by_slug[res.slug].admitted_ms:
            self.stats["ignored_resolutions"] += 1
            return
        if res.slug in self._resolved:
            self.stats["duplicate_resolutions"] += 1
            log.warning("%s: second resolution %s at %d ignored", res.slug, res.outcome, res.recv_ms)
            return
        self._resolved.add(res.slug)
        src.core.set_resolution(res.slug, res.outcome, int(res.recv_ms))

    def _md_src_admitted(self, slug: str) -> LiveSource | None:
        for s in self.sources.values():
            if slug in s.adm_by_slug:
                return s
        return None

    def unresolved(self) -> list[str]:
        return [a["slug"] for a in self.admitted if a["slug"] not in self._resolved]

    def request_stop(self, t_ms: int) -> None:
        """Stop admissions; `end_ms` = max(t_ms, what was dispatched, last admitted window end), rounded up to the
        second (so the last Binance bucket and grid second are whole, as in the replay's last segment)."""
        if self.stopping:
            return
        self.stopping = True
        self._pre_admit = []
        end = max(int(t_ms), self.T + 1, self.rs)
        for a in self.admitted:
            end = max(end, int(self._known[a["slug"]].window_end_ms))
        self.end_ms = _ceil(end, 1000)
        for s in self.sources.values():
            for md in s.mds.values():
                md.recv_cap = self.end_ms  # receive-clock queries never see a row received at or after end_ms
            if s.started:
                s.refresh_grid(self.H_ts, self.end_ms)
                s.set_cl(self.end_ms)

    def finish(self) -> RunResult:
        """End the run (calls `request_stop(T)` first if needed).

        Clean stop (`T >= end_ms - 1`: everything up to end_ms was dispatched): `finish_unresolved()` and
        `Core.finish(end_ms)` per budget, as the session replay ends. Hard stop (`T < end_ms - 1`): nothing beyond `T`
        is processed (the markets' data is only complete up to the horizons): no further drain, close or settle; the
        result holds what happened up to `T` (`clean_stop` False, `stats["clean_stop"]`, `stats["flushed_ms"]`). It is
        the prefix of the session replay's run (spec "Parity check": passed-truncated)."""
        if self.end_ms is None:
            self.request_stop(self.T)
        self.clean_stop = self.T >= self.end_ms - 1
        if self.clean_stop and not self._started:
            self._start()
        out = {}
        for k, s in self.sources.items():
            if self.clean_stop:
                s.refresh_grid(self.H_ts, self.end_ms)
                s.core.finish_unresolved()
                s.core.finish(self.end_ms)
            r = s.core.result()
            r.stats["clean_stop"] = self.clean_stop
            r.stats["flushed_ms"] = int(min(self.T, self.end_ms - 1))
            out[k] = r
        return RunResult(self.cfg, out, time.perf_counter() - self._t0)


__all__ = ["LiveRouter", "LiveRow", "LiveSource", "PM_TS_STREAMS", "Resolution", "is_late_candidate"]

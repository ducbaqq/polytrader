"""The replay engine: feeds recorded data to a strategy on its own clock and routes its orders
through the simulated exchange.

**What is replayed.** Each selected day contributes `[max(day start, REPLAY_START_MS), day end)`,
cut short where the recording ends; adjacent days merge into one range. A market is replayed only
if its whole window lies inside one range (so a market running into a sealed or unselected day is
never touched) and it isn't voided (`gaps.voided_markets`: data gaps, or no outcome).

**Two clocks.** The strategy sees data at `recv_ms`; the exchange matches on `ts_ms`.

- Data events (Polymarket tops and trades of open markets, Binance top of book bundled to the last
  update per `bn_step_ms` bucket and delivered at the bucket's last ms, Chainlink deduplicated on
  `ts_ms`) form one sorted array per hour: `np.lexsort` by recv_ms, then stream (Polymarket <
  Chainlink < Binance), then kind, market and row order.
- Internal events (order reports, timers, market close, settlement, redemption, equity samples) sit
  in a heap; at an equal ms they come after data events.
- Before anything is delivered at time T, the exchanges with live orders are advanced up to
  `T - fill_report_latency_ms`, one exchange event time at a time, delivering any report that falls
  in between first. So each exchange event reaches the strategy (as `on_order`) exactly
  `fill_report_latency_ms` after it happened, and an order placed in reaction (arriving
  `order_latency_ms + U(0, latency_jitter_ms)` later, RNG seeded from `seed` and the coin) never
  lands in time the exchange has already processed. (Advancing straight to T would find fills
  whose report time had already passed.)
- Fills hit the portfolio when they are reported, so `ctx.cash` / `ctx.position` never run ahead of
  what the strategy knows; the cash for an order is locked when it is placed.

**Markets.** A market opens (`on_market_open`; orders accepted from then on) once its first
`pm_book` snapshot inside the replay range has been received: before that its book is untrusted.
Markets are subscribed ~2 windows before they start and are tradable then. Its `price_to_beat`
is hidden until the strategy could know it (see `ReplayDriver._schedule_ptb`). At `window_end_ms` the exchange is
closed (resting orders expire, orders in flight are cancelled as "never arrived"), then
`on_market_close`; the market's data is freed. Settlement happens once the resolution was received
and every report is in; the payout is credited `redeem_delay_s` after the resolution.

**Memory.** Markets are loaded in chunks (by `window_start` hour) just before they are needed and
freed at their close. Binance and Chainlink are loaded one hour at a time.
"""

from __future__ import annotations

import logging
import math
import multiprocessing
import os
import tempfile
import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone

import numpy as np
import pandas as pd

from .config import RunConfig
from .core import (  # noqa: F401 (re-exported: the engine's public API)
    _EMPTY_BN, _RANK, EQUITY_COLS, FILL_COLS, HOUR_MS, INF, K_BN, K_CL, K_OPEN, K_TOP, K_TRADE, MARKET_COLS,
    ORDER_COLS, P_CLOSE, P_TIMER, CoinResult, Core, RunResult, SimCtx, _Bn, _Grid, _Mkt, sym_of,
)
from .exchange import Exchange
from .gaps import gap_table, voided_markets
from .marketdata import LOOKBACK_WINDOWS, load_coin
from .splits import REPLAY_START_MS, day_kind
from .strategy import Strategy, get_strategy

log = logging.getLogger(__name__)

DAY_MS = 86_400_000
DATA_END_SLACK_MS = 60_000  # a day whose data reaches this close to midnight counts as complete


def _day_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def _date(ms: int) -> date:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).date()


def default_con_factory():
    """A `polydata.connect()` connection (sealed days hidden). The engine caps its memory."""
    from ..views import connect

    return connect()


# ---------------------------------------------------------------------------------------- replay

@dataclass
class _Chunk:
    need_ms: int
    rs: int  # start of the replay range it belongs to
    rows: list  # market rows (namedtuples from the markets query)
    loaded: bool = False


class ReplayDriver:
    """Feeds a `Core` from the recorded store: picks the replay ranges and markets, loads markets in chunks
    and Binance/Chainlink one hour at a time, and dispatches each hour's sorted data events."""

    def __init__(self, core: Core, loader: Callable, rec_events: pd.DataFrame | None,
                 clip: tuple[int, int] | None = None):
        self.core, self.loader, self.rec_events, self.clip = core, loader, rec_events, clip

    # ------------------------------------------------------------------------------- setup
    def _ranges(self, con) -> list[tuple[int, int]]:
        cfg = self.core.cfg
        if cfg.days is None:
            days = [r[0] for r in con.execute("SELECT DISTINCT date FROM bn_fut_bbo").fetchall()]
        else:
            days = [date.fromisoformat(str(d)) for d in cfg.days]
        kinds = set(cfg.day_kinds)
        days = sorted({d for d in days if day_kind(d) in kinds})
        if not days:
            return []
        ends = dict(con.execute(
            "SELECT date, max(recv_ms) FROM bn_fut_bbo WHERE date IN (SELECT unnest(?)) AND sym IN (SELECT unnest(?)) "
            "GROUP BY date", [days, self.core.syms]).fetchall())
        if "pm_trade" in {r[0] for r in con.execute("SHOW TABLES").fetchall()}:
            for d, mx in con.execute("SELECT date, max(recv_ms) FROM pm_trade WHERE date IN (SELECT unnest(?)) "
                                     "GROUP BY date", [days]).fetchall():
                ends[d] = min(ends.get(d, mx), mx)
        out: list[list[int]] = []
        for d in days:
            if ends.get(d) is None:
                continue
            s, e = max(_day_ms(d), REPLAY_START_MS), _day_ms(d) + DAY_MS
            if ends[d] < e - DATA_END_SLACK_MS:  # the recording stops early that day
                e = int(ends[d]) + 1
            if e <= s:
                continue
            if out and out[-1][1] == s:
                out[-1][1] = e
            else:
                out.append([s, e])
        if self.clip is not None:
            lo, hi = self.clip
            out = [[max(a, lo), min(b, hi)] for a, b in out if min(b, hi) > max(a, lo)]
        return [(a, b) for a, b in out]

    def _select_markets(self, con, ranges) -> list[_Chunk]:
        cfg = self.core.cfg
        chunks: dict[int, _Chunk] = {}
        for rs, re in ranges:
            mk = con.execute(
                """SELECT slug, coin, timeframe, series, window_start_ms, window_end_ms, outcome
                   FROM markets WHERE list_contains(?, coin) AND list_contains(?, timeframe)
                     AND window_start_ms >= ? AND window_end_ms <= ?
                   ORDER BY window_start_ms, slug""", [self.core.coins, list(cfg.timeframes), rs, re]).df()
            if mk.empty:
                continue
            gaps = gap_table(con, rs, re, events=self.rec_events, max_gap_ms_pm=cfg.max_gap_ms_pm,
                             max_gap_ms_bn=cfg.max_gap_ms_bn)
            void = dict(voided_markets(mk, gaps, cfg.max_gap_ms_pm, cfg.max_gap_ms_bn).itertuples(index=False))
            for r in mk.itertuples(index=False):
                outcome = None if pd.isna(r.outcome) else r.outcome
                self.core.market_rows[r.slug] = dict(
                    slug=r.slug, coin=r.coin, timeframe=r.timeframe, window_start_ms=int(r.window_start_ms),
                    window_end_ms=int(r.window_end_ms), voided=r.slug in void, reason=void.get(r.slug, ""),
                    pnl=math.nan if r.slug in void else 0.0, settle_ms=math.nan, outcome=outcome)
                if r.slug in void:
                    continue
                h = int(r.window_start_ms) // HOUR_MS
                length = int(r.window_end_ms) - int(r.window_start_ms)
                need = max(rs, int(r.window_start_ms) - LOOKBACK_WINDOWS * length)
                c = chunks.setdefault(h, _Chunk(need, rs, []))
                c.need_ms = min(c.need_ms, need)
                c.rows.append(r)
        return sorted(chunks.values(), key=lambda c: (c.need_ms, c.rows[0].window_start_ms))

    def _load_chunk(self, con, c: _Chunk, seg_start: int) -> None:
        t0 = time.perf_counter()
        c.loaded = True
        for coin in self.core.coins:
            rows = [r for r in c.rows if r.coin == coin]
            if not rows:
                continue
            mds = self.loader(con, coin, list(self.core.cfg.timeframes), min(int(r.window_start_ms) for r in rows),
                              max(int(r.window_end_ms) for r in rows), slugs={r.slug for r in rows})
            for r in rows:
                md = mds.get(r.slug)
                if md is None:
                    self.core.market_rows[r.slug].update(voided=True, reason="no data", pnl=math.nan)
                    continue
                info = md.info
                m = _Mkt(info, len(self.core.mkts), md, Exchange(md, self.core.exc), c.rs)
                sn = md.snaps_recv
                i = int(np.searchsorted(sn, max(c.rs, seg_start)))
                if i >= len(sn) or sn[i] >= info.window_end_ms:
                    self.core.market_rows[r.slug].update(voided=True, reason="no book snapshot", pnl=math.nan)
                    continue
                m.open_ms = int(sn[i])  # the first snapshot in range: the book is trusted from here
                self.core.mkts.append(m)
                self.core.by_slug[info.slug] = m
                self.core.loaded.append(m)
                self.core.pf.add_market(info)
                self.core.push(info.window_end_ms, P_CLOSE, ("close", m))
                self.core.stats["markets"] += 1
        self.core.stats["load_s"] += time.perf_counter() - t0

    # ------------------------------------------------------------------------------- data per hour
    def _load_bn(self, con, s: int, e: int) -> None:
        step = self.core.cfg.bn_step_ms
        d = con.execute(
            f"""SELECT sym, recv_ms // {step} AS b,
                       arg_max(bid, (recv_ms, update_id)) AS bid, arg_max(ask, (recv_ms, update_id)) AS ask,
                       arg_max(bid_qty, (recv_ms, update_id)) AS bid_qty, arg_max(ask_qty, (recv_ms, update_id)) AS ask_qty
                FROM bn_fut_bbo WHERE date = ? AND sym IN (SELECT unnest(?)) AND recv_ms >= ? AND recv_ms < ?
                GROUP BY ALL ORDER BY sym, b""", [_date(s), self.core.syms, s, e]).fetchnumpy()
        sym = np.asarray(d["sym"]).astype(str) if len(d["sym"]) else np.empty(0, str)
        self.core.bn_prev, self.core.bn_cur = self.core.bn_cur, {}
        for x in self.core.syms:
            sel = sym == x
            bn = _Bn(np.asarray(d["b"])[sel].astype(np.int64) * step + step - 1, np.asarray(d["bid"])[sel].astype(float),
                     np.asarray(d["ask"])[sel].astype(float), np.asarray(d["bid_qty"])[sel].astype(float),
                     np.asarray(d["ask_qty"])[sel].astype(float)) if len(sym) else _EMPTY_BN
            self.core.bn_cur[x] = bn
            self.core.grid.setdefault(x, _Grid()).append(s // 1000, -(-e // 1000), bn)

    def _load_cl(self, con, s: int, e: int) -> dict[str, tuple]:
        d = con.execute(
            """SELECT coin, ts_ms, min(recv_ms) AS recv_ms, arg_min(price, recv_ms) AS price
               FROM chainlink WHERE date BETWEEN ? AND ? AND coin IN (SELECT unnest(?))
                 AND recv_ms >= ? AND recv_ms < ?
               GROUP BY coin, ts_ms HAVING min(recv_ms) >= ?
               ORDER BY recv_ms, coin, ts_ms""", [_date(s - 60_000), _date(e - 1), self.core.coins, s - 60_000, e, s]).fetchnumpy()
        coin = np.asarray(d["coin"]).astype(str) if len(d["coin"]) else np.empty(0, str)
        out = {}
        for c in self.core.coins:
            sel = coin == c
            seg = (np.asarray(d["recv_ms"])[sel].astype(np.int64), np.asarray(d["ts_ms"])[sel].astype(np.int64),
                   np.asarray(d["price"])[sel].astype(float))
            out[c] = seg
            old = self.core.cl.get(c)
            self.core.cl[c] = seg if old is None else tuple(np.r_[a, b] for a, b in zip(old, seg))
        return out

    def _segment(self, con, s: int, e: int) -> None:
        t0 = time.perf_counter()
        self._load_bn(con, s, e)
        cl = self._load_cl(con, s, e)
        self._schedule_ptb(s, e)
        recv, kind, mi, idx = [], [], [], []

        def add(r, k, m, i):
            recv.append(r)
            kind.append(np.full(len(r), k, np.int8) if np.ndim(k) == 0 else k)
            mi.append(np.full(len(r), m, np.int32))
            idx.append(i)

        want = self.core.want
        for m in self.core.loaded:
            E = m.info.window_end_ms
            lo, hi = max(s, m.open_ms), min(e, E)
            if lo >= hi:
                continue
            tr = m.md.tops_recv.recv_ms
            a, b = (int(x) for x in np.searchsorted(tr, [lo, hi]))
            if not m.opened and s <= m.open_ms < e:
                add(np.array([m.open_ms], np.int64), K_OPEN, m.mi, np.array([-1]))
            if want["on_book"] and b > a:
                add(tr[a:b], K_TOP, m.mi, np.arange(a, b))
            if want["on_trade"]:
                rr = m.md.trades_recv.recv_ms
                a, b = (int(x) for x in np.searchsorted(rr, [lo, hi]))
                if b > a:
                    add(rr[a:b], K_TRADE, m.mi, np.arange(a, b))
        bn_lists = []
        if want["on_binance"]:
            for j, x in enumerate(self.core.syms):
                bn = self.core.bn_cur[x]
                bn_lists.append((x, bn.bid.tolist(), bn.ask.tolist(), bn.bid_qty.tolist(), bn.ask_qty.tolist()))
                if len(bn.recv):
                    add(bn.recv, K_BN, j, np.arange(len(bn.recv)))
        cl_lists = []
        if want["on_chainlink"]:
            for j, c in enumerate(self.core.coins):
                seg = cl[c]
                cl_lists.append((c, seg[2].tolist()))
                if len(seg[0]):
                    add(seg[0], K_CL, j, np.arange(len(seg[0])))
        self.core.stats["data_s"] += time.perf_counter() - t0
        if recv:
            R_ = np.concatenate(recv).astype(np.int64)
            K_ = np.concatenate(kind)
            M_ = np.concatenate(mi)
            I_ = np.concatenate(idx).astype(np.int64)
            o = np.lexsort((I_, M_, K_, _RANK[K_], R_))
            self.core.dispatch(R_[o].tolist(), K_[o].tolist(), M_[o].tolist(), I_[o].tolist(), bn_lists, cl_lists)
        self.core._drain(e)
        self.core.stats["segments"] += 1

    def _schedule_ptb(self, s: int, e: int) -> None:
        """The strategy's price_to_beat is the mean of the received Chainlink ticks with ts in
        [window_start - 60 s, window_start) (the settlement rule, docs/RECORDER.md), revealed when the
        first tick with ts >= window_start is received. It stays None when no tick fell in that span
        or the span starts before the replay range (the live runner computes it the same way)."""
        for m in self.core.loaded:
            ws = m.info.window_start_ms
            if m.ptb_sched or ws >= e:
                continue
            c = self.core.cl.get(m.info.coin)
            hit = np.flatnonzero(c[1] >= ws) if c is not None else []
            if not len(hit):
                continue  # not revealed yet: look again next hour
            m.ptb_sched = True
            r = int(c[0][hit[0]])
            sel = (c[1] >= ws - 60_000) & (c[1] < ws) & (c[0] <= r)
            if ws - 60_000 < m.rs or not sel.any():
                continue
            self.core.push(max(r, s), P_TIMER, ("ptb", m, float(c[2][sel].mean())))

    # ------------------------------------------------------------------------------- run
    def run(self, con) -> CoinResult:
        t_start = time.perf_counter()
        ranges = self._ranges(con)
        chunks = self._select_markets(con, ranges) if ranges else []
        if ranges:
            self.core.start(ranges[0][0])
        ci = 0
        for ri, (rs, re) in enumerate(ranges):
            self.core.grid, self.core.bn_cur, self.core.bn_prev, self.core.cl = {}, {}, {}, {}  # vol history restarts per range
            s = rs
            while s < re:
                e = min((s // HOUR_MS + 1) * HOUR_MS, re)
                while ci < len(chunks) and chunks[ci].need_ms < e:
                    self._load_chunk(con, chunks[ci], s)
                    ci += 1
                self._segment(con, s, e)
                s = e
            if ri + 1 < len(ranges):
                self.core._drain(ranges[ri + 1][0])
        if ranges:
            self.core.finish(ranges[-1][1])
        else:
            self.core.ending = True
            self.core._drain(INF)
        self.core.stats["elapsed_s"] = time.perf_counter() - t_start
        self.core.stats["ranges"] = ranges
        return self.core.result()


# ---------------------------------------------------------------------------------------- entry points

def _replay(cfg: RunConfig, coins: list[str], budgets: dict[str, float], con_factory: Callable,
            loader: Callable | None = None, strategy: Strategy | None = None,
            rec_events: pd.DataFrame | None = None, clip: tuple[int, int] | None = None) -> CoinResult:
    strategy = strategy if strategy is not None else get_strategy(cfg.strategy)(cfg.params)
    r = ReplayDriver(Core(cfg, coins, budgets, strategy), loader or load_coin, rec_events, clip)
    con = con_factory()
    try:
        con.execute("SET memory_limit='4GB'")  # load_coin's peak is DuckDB's buffer pool
        spill = os.path.join(tempfile.gettempdir(), "polydata-duckdb")  # not the cwd (`.tmp` in the repo)
        con.execute(f"SET temp_directory='{spill}'")
        return r.run(con)
    finally:
        con.close()


def run_coin(cfg: RunConfig, coin: str, con_factory: Callable = default_con_factory, *, loader: Callable | None = None,
             strategy: Strategy | None = None, budget: float | None = None,
             rec_events: pd.DataFrame | None = None, clip: tuple[int, int] | None = None) -> CoinResult:
    """Replay one coin's markets with its own budget (default `capital / len(coins)`).

    `loader(con, coin, timeframes, start_ms, end_ms, slugs=...)` defaults to `marketdata.load_coin`;
    `strategy` (an instance) defaults to the registry's `cfg.strategy` built with `cfg.params`;
    `rec_events` defaults to the daily `rec_events` catalog (see `gaps.gap_table`); `clip=(start_ms,
    end_ms)` narrows the replay ranges (smoke runs and tests)."""
    b = cfg.capital / len(cfg.coins) if budget is None else budget
    return _replay(cfg, [coin], {coin: b}, con_factory, loader, strategy, rec_events, clip)


def _worker(args) -> tuple[str, CoinResult]:
    cfg_d, coin, budget, con_factory = args
    return coin, run_coin(RunConfig.from_dict(cfg_d), coin, con_factory, budget=budget)


def run(cfg: RunConfig, con_factory: Callable = default_con_factory, workers: int | None = None) -> RunResult:
    """Run the config. Per-coin mode: one process per coin (at most `workers`, default
    min(coins, 4): each process can use ~4 GB), budgets `capital / len(coins)`. Global mode: every
    coin in one replay with one shared balance."""
    t0 = time.perf_counter()
    if cfg.capital_mode == "global":
        res = {"*": _replay(cfg, list(cfg.coins), {"*": cfg.capital}, con_factory)}
        return RunResult(cfg, res, time.perf_counter() - t0)
    budget = cfg.capital / len(cfg.coins)
    args = [(cfg.to_dict(), c, budget, con_factory) for c in cfg.coins]
    n = min(len(args), workers or min(4, os.cpu_count() or 1))
    if n <= 1:
        out = [_worker(a) for a in args]
    else:
        with multiprocessing.get_context("spawn").Pool(n) as pool:
            out = pool.map(_worker, args, chunksize=1)
    return RunResult(cfg, dict(out), time.perf_counter() - t0)


__all__ = ["CoinResult", "RunResult", "SimCtx", "default_con_factory", "run", "run_coin", "sym_of"]

"""`PaperRun`: one paper session (spec "CLI", "Settlement", "Run folder and session format").

Sequence (`run()`):
1. Refuse if the run folder holds a `paper.lock` of a live process. Warn if `bt validate` has not passed for the
   current `sim_version`; on macOS suggest `caffeinate -i`.
2. Start the feeds (`feeds_factory(ingest, clock)`). Until the session start is known, `Ingest` holds the stamped rows.
   `rs` = the time every required connection reported `ws_open`, plus the warm-up, rounded up to the second. A sealed
   `rs` day is refused unless `allow_sealed` (then one look is recorded). The router (with the Chainlink settler) is
   built and the held rows are routed.
3. Every `tick_ms`: `router.tick(wall)`, `ingest.note_tick(wall)` (writes the settler's resolutions, notes stalls,
   flushes every 10 s). Every 60 s a status line (`# ...`) to stdout and `log.txt`.
4. Admission: a market must not touch a sealed day (unless allowed) and must end by `rs + duration` when given.
5. Stop at the duration end, at the start of the next sealed day (unless allowed) or on the first SIGINT:
   `router.request_stop`, then keep ticking until `T >= end_ms` and no admitted market still waits for its Chainlink
   resolution, or `max_drain_s` passes. A second SIGINT is a hard stop (`clean_stop: false`).
6. Feeds stopped. `Confirmer` (when `rest_factory` is given): polled every 30 s during the run (Gamma answers, the
   fallback for markets without a Chainlink outcome, Data API trades); the drain waits for the markets the settler gave
   up on while Gamma can still answer (bounded by `--confirm-wait` per market and `--max-drain`). After the drain the
   Gamma fallback is closed and, with only the REST polling left, the run waits until every market has its Gamma answer
   and trades or `--confirm-wait` after the last market's end. Then `router.finish()`, `ingest.finish()`,
   `writer.close()`, the manifest, the post-hoc voids (`voids.session_voids` over the finished session), the run files
   (`results.write_paper_run`), `runs.rebuild_index()`.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
import time
from collections import Counter
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from polydata.bt import runs, validate
from polydata.bt.config import RunConfig
from polydata.bt.live import LiveRouter
from polydata.bt.splits import day_kind, record_look
from polydata.bt.types import MarketInfo
from polydata.paper.confirm import POLL_MS, Confirmer
from polydata.paper.feeds.rest import Rest
from polydata.paper.ingest import Ingest
from polydata.paper.session import SessionWriter
from polydata.paper.settle import ChainlinkSettler

BN_THIN_MS = 10  # session writer's Binance thinning step
STATUS_EVERY_MS = 60_000
STALL_WARN_MS = 2_000
# Connection groups that must have opened before the session can start (source name prefixes).
REQUIRED_SOURCES = ("polymarket-rtds", "binance-fut-bbo", "binance-fut-market", "pm:")


class PaperRefused(SystemExit):
    """The run may not start (or go on): the message says why."""


@dataclass
class PaperOpts:
    hold_back_ms: int = 10_000  # controller ruling (the spec said 3000)
    skew_ms: int = 1000
    tick_ms: int = 20
    warmup_s: int = 60
    max_drain_s: int = 900
    confirm_wait_s: int = 900
    duration_s: int | None = None
    allow_sealed: bool = False
    purpose: str | None = None


def _utc_day(ms: int) -> date:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).date()


def _day_ms(d: date) -> int:
    return int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)


def next_sealed_start(ms: int, horizon_days: int = 4) -> int | None:
    """Start (ms) of the first sealed UTC day after the day of `ms`."""
    d = _utc_day(ms)
    for i in range(1, horizon_days + 1):
        if day_kind(d + timedelta(days=i)) == "sealed":
            return _day_ms(d + timedelta(days=i))
    return None


def touches_sealed(info: MarketInfo) -> bool:
    a, b = _utc_day(int(info.window_start_ms)), _utc_day(int(info.window_end_ms) - 1)
    return any(day_kind(a + timedelta(days=i)) == "sealed" for i in range((b - a).days + 1))


class _Hist:
    """Whole-ms histogram (bounded memory) with percentiles."""

    def __init__(self):
        self.c: Counter = Counter()
        self.n = 0

    def add(self, v: float) -> None:
        self.c[max(0, int(v))] += 1
        self.n += 1

    def pct(self, q: float) -> int | None:
        return hist_pct(self.c, q)

    def max(self) -> int | None:
        return max(self.c) if self.c else None


def hist_pct(c: Counter, q: float) -> int | None:
    n = sum(c.values())
    if not n:
        return None
    k, acc = q * n, 0
    for v in sorted(c):
        acc += c[v]
        if acc >= k:
            return int(v)
    return int(max(c))


def _pid_alive(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    return True


def _real_clock() -> int:
    return int(time.time() * 1000)


class PaperRun:
    def __init__(self, cfg: RunConfig, out: Path, opts: PaperOpts,
                 feeds_factory: Callable[[Ingest, Callable[[], int]], list[Awaitable]], *,
                 clock: Callable[[], int] | None = None, sleep: Callable[[float], Awaitable] | None = None,
                 required_sources: tuple[str, ...] = REQUIRED_SOURCES, ready_timeout_s: float = 180.0,
                 echo: Callable[[str], None] | None = None, update_docs_index: bool = False,
                 rest_factory: Callable[[Callable[[], int], Callable[[str, str, str], None]], Rest] | None = None):
        if int(cfg.bn_step_ms) % BN_THIN_MS:
            raise ValueError(f"bn_step_ms {cfg.bn_step_ms} must be a multiple of the session thinning "
                             f"({BN_THIN_MS} ms)")
        if opts.allow_sealed and not opts.purpose:
            raise ValueError("--allow-sealed needs --purpose")
        self.cfg, self.out, self.opts, self.feeds_factory = cfg, Path(out), opts, feeds_factory
        self.clock = clock or _real_clock
        self.sleep = sleep or asyncio.sleep
        self.required = tuple(required_sources)
        self.ready_timeout_s = ready_timeout_s
        self.echo = echo or (lambda s: print(s, flush=True))
        self.sigints = 0
        self.rest_factory = rest_factory  # None: no Gamma confirmation or Data API trades (offline tests)
        self.confirmer: Confirmer | None = None
        self._poll_task: asyncio.Task | None = None
        self.void_error: str | None = None
        self.update_docs_index = update_docs_index
        self.feed_failures = 0
        self._feeds_closing = False
        self.router: LiveRouter | None = None
        self.settler: ChainlinkSettler | None = None
        self.ingest: Ingest | None = None
        self.writer: SessionWriter | None = None
        self.conns: dict[str, bool] = {}
        self.first_open: dict[str, int] = {}
        self.proc_lag = _Hist()
        self.loop_lag = _Hist()
        self.status_lines: list[str] = []
        self.warnings: list[str] = []
        self.rs: int | None = None
        self.stop_at: int | None = None
        self.stop_reason: str | None = None

    # ------------------------------------------------------------------------------------------- helpers
    def say(self, line: str, status: bool = False) -> None:
        self.echo(line)
        if status:
            self.status_lines.append(line)
            try:
                with open(self.out / "log.txt", "a") as f:
                    f.write(line + "\n")
            except OSError:
                pass

    def warn(self, msg: str) -> None:
        self.warnings.append(msg)
        self.say(f"# warning: {msg}", status=True)

    def request_stop(self) -> None:
        """First call: drain (no new admissions, finish open markets). Second call: hard stop."""
        self.sigints += 1
        if self.sigints == 1:
            self.say("# stop requested: draining (press Ctrl-C again for a hard stop)", status=True)
        else:
            self.say("# hard stop", status=True)

    def _on_event(self, kind: str, source: str, detail: str, ts: int) -> None:
        if kind == "ws_open":
            self.conns[source] = True
            self.first_open.setdefault(source, ts)
        elif kind in ("ws_close", "ws_stale", "ws_error", "ws_rotate"):
            if source in self.conns:
                self.conns[source] = False

    def _ready(self) -> bool:
        return all(any(s.startswith(p) for s in self.first_open) for p in self.required)

    def _lock(self) -> Path:
        self.out.mkdir(parents=True, exist_ok=True)
        lock = self.out / "paper.lock"
        if lock.exists():
            try:
                pid = int(lock.read_text().strip() or 0)
            except ValueError:
                pid = 0
            if pid and pid != os.getpid() and _pid_alive(pid):
                raise PaperRefused(f"error: {self.out} is used by another paper run (pid {pid})")
        if (self.out / "session" / "manifest.json").exists():
            raise PaperRefused(f"error: {self.out} already holds a finished session")
        lock.write_text(str(os.getpid()))
        return lock

    def can_touch_sealed(self, rs: int) -> bool:
        """The session can reach a sealed day: rs is sealed, or the run (until SIGINT, or rs + duration) crosses the
        start of one."""
        if day_kind(_utc_day(rs)) == "sealed":
            return True
        s = next_sealed_start(rs)
        return s is not None and (self.opts.duration_s is None or rs + int(self.opts.duration_s) * 1000 > s)

    def admit_filter(self, info: MarketInfo, t: int) -> bool:
        if not self.opts.allow_sealed and touches_sealed(info):
            return False
        if self.opts.duration_s is not None and int(info.window_end_ms) > self.rs + int(self.opts.duration_s) * 1000:
            return False
        return True

    # ------------------------------------------------------------------------------------------- run
    async def run(self) -> Path:
        from polydata.paper.results import write_paper_run

        opts, cfg = self.opts, self.cfg
        lock = self._lock()
        try:
            if not validate.is_validated():
                self.warn(f"`polydata bt validate` has not passed for sim_version {validate.sim_version()}")
            if sys.platform == "darwin" and self.clock is _real_clock:
                self.say("# tip: keep the Mac awake with `caffeinate -i` (sleep shows up as gaps and voids)")
            now = self.clock()
            if not opts.allow_sealed and day_kind(_utc_day(now + opts.warmup_s * 1000)) == "sealed":
                raise PaperRefused(f"error: {_utc_day(now + opts.warmup_s * 1000)} is a sealed day: paper runs refuse "
                                   "sealed days (use --allow-sealed --purpose TEXT to record a look)")
            self.writer = SessionWriter(self.out / "session", bn_step_ms=BN_THIN_MS)
            self.ingest = Ingest(None, self.writer, self.clock, listener=self._on_event)
            tasks = [asyncio.ensure_future(a) for a in self.feeds_factory(self.ingest, self.clock)]
            for t in tasks:
                t.add_done_callback(self._feed_done)
            self._install_signals()
            try:
                await self._start()
                await self._loop()
            finally:
                self._feeds_closing = True
                for t in tasks:
                    t.cancel()
                for r in await asyncio.gather(*tasks, return_exceptions=True):
                    if isinstance(r, BaseException) and not isinstance(r, asyncio.CancelledError):
                        self.say(f"# warning: feed task raised while stopping: {type(r).__name__}: {r}", status=True)
                self._remove_signals()
            await self._confirm_phase()
            router = self.router
            res = router.finish()
            self.ingest.finish()
            self.writer.close()
            manifest = self._manifest()
            self.writer.manifest(**manifest)
            voids = self._voids(res, manifest)
            stats = self._stats()
            summary = write_paper_run(self.out, cfg, opts, res, router, manifest, stats=stats,
                                      status_lines=self.status_lines, voids=voids, confirm=self.confirmer,
                                      errors=[self.void_error] if self.void_error else None)
            try:
                # the committed docs/runs-index.csv only on request: a paper run must not rewrite it from a partial
                # local runs folder (`bt run` keeps its own behaviour)
                runs.rebuild_index(None if self.update_docs_index else runs.runs_dir() / "index.csv")
            except Exception as e:  # noqa: BLE001 - the run folder is complete; the index can be rebuilt later
                self.say(f"# warning: could not rebuild the runs index: {e}")
            self.say(f"# run folder: {self.out}")
            self.say(f"# P&L ${summary['pnl_usd']:.2f}, {summary['orders']} orders, {summary['fills']} fills, "
                     f"{summary['markets']} markets, late {summary['late']}, future {summary['future']}, "
                     f"clean_stop {manifest['clean_stop']}")
            if summary.get("voided_with_orders"):
                self.say(f"# voided markets with orders (their P&L is left out): {len(summary['voided_with_orders'])}: "
                         + ", ".join(f"{k} ({v})" for k, v in summary["voided_with_orders"].items()))
            return self.out
        except PaperRefused:
            if self.writer is not None:
                self.writer.close()
            raise
        except Exception as e:
            self._error_manifest(e)
            raise
        finally:
            try:
                lock.unlink()
            except OSError:
                pass

    async def _start(self) -> None:
        """Wait for the connections, fix rs, build the router and route the held rows."""
        opts = self.opts
        t0 = self.clock()
        while not self._ready():
            if self.sigints:
                raise PaperRefused("error: stopped before the session started")
            if self.clock() - t0 > self.ready_timeout_s * 1000:
                missing = [p for p in self.required if not any(s.startswith(p) for s in self.first_open)]
                raise PaperRefused(f"error: no connection opened within {self.ready_timeout_s:.0f} s for {missing}")
            await self.sleep(opts.tick_ms / 1000)
        ready = max(self.first_open.values())
        rs = -(-(ready + opts.warmup_s * 1000) // 1000) * 1000
        if day_kind(_utc_day(rs)) == "sealed":
            if not opts.allow_sealed:
                raise PaperRefused(f"error: the session would start on {_utc_day(rs)}, a sealed day: paper runs refuse "
                                   "sealed days (use --allow-sealed --purpose TEXT to record a look)")
        if opts.allow_sealed and self.can_touch_sealed(rs):
            record_look("paper", {"purpose": opts.purpose, "run": self.out.name, "rs": rs,
                                  "strategy": self.cfg.strategy, "coins": list(self.cfg.coins),
                                  "duration_s": opts.duration_s})
        self.rs = rs
        stops = []
        if opts.duration_s is not None:
            stops.append((rs + int(opts.duration_s) * 1000, "duration"))
        if not opts.allow_sealed and (s := next_sealed_start(rs)) is not None:
            stops.append((s, "sealed day"))
        if stops:
            self.stop_at, self.stop_reason = min(stops)
        self.settler = ChainlinkSettler(rs)
        if self.rest_factory is not None:
            self.confirmer = Confirmer(self.rest_factory(self.clock, self.ingest.on_event), self.ingest, self.settler,
                                       self.clock, confirm_wait_ms=int(opts.confirm_wait_s) * 1000)
        self.router = LiveRouter(self.cfg, rs, hold_back_ms=opts.hold_back_ms, skew_ms=opts.skew_ms, trace=True,
                                 admit_filter=self.admit_filter, settler=self.settler)
        self.ingest.attach(self.router)
        iso = lambda ms: datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%d %H:%M:%S")  # noqa: E731
        self.say(f"# session start rs {iso(rs)} UTC (connections up, + {opts.warmup_s} s warm-up); strategy clock "
                 f"runs {opts.hold_back_ms + opts.skew_ms} ms behind wall time"
                 + (f"; stops at {iso(self.stop_at)} UTC ({self.stop_reason})" if self.stop_at else ""), status=True)

    def _feed_done(self, t: asyncio.Task) -> None:
        """A feed task ended before the stop: its rows are missing from here on (gaps follow from the session)."""
        if self._feeds_closing:
            return
        self.feed_failures += 1
        why = "cancelled" if t.cancelled() else (repr(t.exception()) if t.exception() else "returned")
        self.warn(f"a feed task ended before the stop ({why})")

    def _error_manifest(self, e: BaseException) -> None:
        """An unexpected error: close the session and mark it unclean, so what was written stays readable."""
        try:
            if self.ingest is not None and self.router is not None:
                self.ingest.finish()
            if self.writer is None:
                return
            self.writer.close()
            if self.router is not None:
                m = self._manifest()
            else:
                m = {"rs": None, "end_ms": None, "coins": list(self.cfg.coins), "timeframes": list(self.cfg.timeframes),
                     "admitted": []}
            m.update(clean_stop=False, stop_reason=f"error: {type(e).__name__}: {e}"[:500])
            self.writer.manifest(**m)
        except Exception as e2:  # noqa: BLE001 - never hide the original error
            self.say(f"# warning: could not write the manifest after an error: {e2}")

    def _waiting(self) -> list[str]:
        """Admitted markets still waiting for a resolution this run can produce: from the Chainlink settler, or (the
        settler gave up on them) from Gamma while the Confirmer can still get it (or has it and the router has not
        applied it yet)."""
        gave_up, c = self.settler.gave_up, self.confirmer
        return [s for s in self.router.unresolved() if s not in gave_up or (c is not None and c.holds(s))]

    def _poll_done(self, t: asyncio.Task) -> None:
        if not t.cancelled() and t.exception() is not None:
            self.warn(f"Gamma/Data API polling failed: {t.exception()!r}")

    def _maybe_poll(self, now: int) -> None:
        c = self.confirmer
        if c is not None and now >= self._next_poll and (self._poll_task is None or self._poll_task.done()):
            self._next_poll = now + POLL_MS
            self._poll_task = asyncio.ensure_future(c.poll())
            self._poll_task.add_done_callback(self._poll_done)

    async def _confirm_phase(self) -> None:
        """After the drain: no more Gamma fallbacks (a `pm_resolution` row now would make the replay settle a market
        the run left unresolved); keep polling until every admitted market has its Gamma answer and trades, or
        `confirm_wait` after the last market's end (a second SIGINT skips it)."""
        c = self.confirmer
        if c is None:
            return
        c.close_fallback()
        if self._poll_task is not None:
            await asyncio.wait([self._poll_task], timeout=30)
        last_end = max([int(i.window_end_ms) for i in self.settler.infos.values()] or [0])
        deadline = last_end + int(self.opts.confirm_wait_s) * 1000 + 2 * POLL_MS
        if self.sigints < 2 and c.outstanding():
            self.say(f"# waiting for Gamma answers and Data API trades (up to {self.opts.confirm_wait_s} s after the "
                     "last market ended; Ctrl-C to skip)", status=True)
        nxt = 0
        while self.sigints < 2 and c.outstanding() and self.clock() < deadline:
            if self.clock() >= nxt:
                nxt = self.clock() + POLL_MS
                await c.poll()
            await self.sleep(1.0)
        if c.outstanding():
            self.warn("confirmation ended with Gamma answers or Data API trades missing")

    def _voids(self, res, manifest):
        """Post-hoc voids over the finished session (`voids.session_voids`); a failure is reported as an error and
        leaves every market unvoided."""
        from polydata.paper import voids

        try:
            return voids.session_voids(self.out / "session", res.markets, self.cfg, int(manifest["end_ms"]))
        except Exception as e:  # noqa: BLE001 - the run folder must still be written
            self.void_error = f"post-hoc voids failed: {type(e).__name__}: {e}"[:400]
            self.warn(self.void_error)
            return None

    async def _loop(self) -> None:
        router, ingest, opts = self.router, self.ingest, self.opts
        tick_s = opts.tick_ms / 1000
        next_status = self.clock() + STATUS_EVERY_MS
        stop_wall: int | None = None
        expected = None
        self._next_poll = 0
        while True:
            wall = self.clock()
            if expected is not None:
                lag = wall - expected
                self.loop_lag.add(lag)
                if lag > STALL_WARN_MS:
                    self.warn(f"event loop stalled {lag} ms (markets overlapping a stall > 2 s are voided)")
            if (not router.stopping and not self.sigints and self.stop_at is not None
                    and wall - opts.hold_back_ms - opts.skew_ms >= self.stop_at - 1000):
                # before the tick: a jump of the wall clock must not dispatch anything at or after stop_at
                router.request_stop(self.stop_at)
                self.say(f"# stopping ({self.stop_reason}): draining", status=True)
                stop_wall = self.clock()
            router.tick(wall)
            ingest.note_tick(wall)
            self._maybe_poll(wall)
            self.proc_lag.add(self.clock() - (wall if expected is None else min(wall, expected)))
            if self.sigints >= 2:
                if self.confirmer is not None:
                    self.confirmer.close_fallback()
                break
            if not router.stopping:
                if self.sigints:
                    router.request_stop(router.T)
                if router.stopping:
                    stop_wall = self.clock()
            if router.stopping:
                if (self.confirmer is not None and self.confirmer.fallback_open
                        and self.clock() - stop_wall
                        > opts.max_drain_s * 1000 - opts.hold_back_ms - opts.skew_ms - 1000):
                    # a Gamma row written now could not be applied before the drain limit
                    self.confirmer.close_fallback()
                if router.T >= router.end_ms and not self._waiting():
                    break
                if self.clock() - stop_wall > opts.max_drain_s * 1000:
                    self.warn(f"drain limit reached: {len(self._waiting())} markets unresolved")
                    break
            now = self.clock()
            if now >= next_status:
                self.say(self.status_line(), status=True)
                next_status = now + STATUS_EVERY_MS
            expected = self.clock() + opts.tick_ms
            await self.sleep(tick_s)

    def status_line(self) -> str:
        r = self.router
        n_open = n_orders = n_fills = 0
        pnl = 0.0
        for s in r.sources.values():
            c = s.core
            n_open += len(c.open)
            n_orders += c._oid
            n_fills += len(c.fill_rows)
            for row in c.market_rows.values():
                p = row.get("pnl")
                if p is not None and p == p:
                    pnl += float(p)
        up = sum(self.conns.values())
        p99 = self.proc_lag.pct(0.99)
        return (f"# open {n_open} orders {n_orders} fills {n_fills} pnl ${pnl:.2f} lag p99 {p99} ms "
                f"late {r.stats['late'] + r.stats['future']} conns {up}/{len(self.conns)} admitted {len(r.admitted)}")

    def _install_signals(self) -> None:
        self._sig = False
        try:
            asyncio.get_running_loop().add_signal_handler(signal.SIGINT, self.request_stop)
            self._sig = True
        except (NotImplementedError, RuntimeError, ValueError):
            pass

    def _remove_signals(self) -> None:
        if getattr(self, "_sig", False):
            try:
                asyncio.get_running_loop().remove_signal_handler(signal.SIGINT)
            except (NotImplementedError, RuntimeError, ValueError):
                pass

    # ------------------------------------------------------------------------------------------- outputs
    def _manifest(self) -> dict:
        r, cfg = self.router, self.cfg
        return {"rs": r.rs, "end_ms": r.end_ms, "clean_stop": bool(r.clean_stop), "coins": list(cfg.coins),
                "timeframes": list(cfg.timeframes), "hold_back_ms": r.hold_back_ms, "skew_ms": r.skew_ms,
                "bn_step_ms": int(cfg.bn_step_ms), "bn_thinning_ms": BN_THIN_MS, "tick_ms": self.opts.tick_ms,
                "warmup_s": self.opts.warmup_s, "stop_reason": "sigint" if self.sigints else self.stop_reason,
                "flushed_ms": int(min(r.T, r.end_ms - 1)) if r.end_ms is not None else None,
                "admitted": list(r.admitted)}

    def _stats(self) -> dict:
        ing = self.ingest
        return {
            "processing_lag_ms": {"p50": self.proc_lag.pct(0.5), "p99": self.proc_lag.pct(0.99),
                                  "max": self.proc_lag.max()},
            "loop_lag_ms": {"p50": self.loop_lag.pct(0.5), "p99": self.loop_lag.pct(0.99), "max": self.loop_lag.max()},
            "feed_delay_ms": {s: {"p50": hist_pct(c, 0.5), "p99": hist_pct(c, 0.99), "n": sum(c.values())}
                              for s, c in ing.delays.items() if c},
            "gave_up": dict(self.settler.gave_up),
            "connections": {"up": sum(self.conns.values()), "seen": len(self.conns)},
            "warnings": list(self.warnings),
            "feed_failures": self.feed_failures,
            "opts": asdict(self.opts),
        }


__all__ = ["PaperOpts", "PaperRefused", "PaperRun", "REQUIRED_SOURCES", "next_sealed_start", "touches_sealed"]

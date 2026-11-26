"""Ingest: stamps `recv_ms`, feeds the `LiveRouter`, and writes what it accepted to the `SessionWriter`.

Feeds emit `(stream, values)` rows without `recv_ms`. One stamp per emit, `recv = max(previous recv, clock(),
last tick wall)`, so receive time never goes back (the router raises otherwise, and a non-Polymarket row stamped at or
before the released horizon raises too). Emits are processed in order and synchronously (no await between the stamp
and the write). A clock that steps back by more than 2 s, or an event loop that stalled for more than 2 s, is a gap in
every stream: a `recorder stop`/`start` pair goes to the catalog, as the recorder writes on a restart.

Before the session start is known the runner has no router yet (`router=None`): rows are stamped and held, and
`attach(router)` routes them in order before anything else (connection events are written at once). The discovery
event of every market is kept, so the settler's resolutions are written as `settle.resolution_row` (the discovery event
with the Chainlink prices). `listener(kind, source, detail, ts)` sees every connection event; `delays` counts the
feed delay (`recv_ms - ts_ms`, whole ms) per Polymarket exchange-clock stream.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Callable

from polydata.bt.live import PM_TS_STREAMS, LiveRouter, LiveRow
from polydata.paper.session import SessionWriter
from polydata.paper.settle import resolution_row

STEP_MS = 2000  # a clock step back or an event-loop stall longer than this is a recorder gap


class Ingest:
    def __init__(self, router: LiveRouter | None, writer: SessionWriter, clock: Callable[[], int], *,
                 listener: Callable[[str, str, str, int], None] | None = None):
        self.router, self.writer, self.clock = router, writer, clock
        self.listener = listener
        self.held: list[LiveRow] | None = [] if router is None else None
        self.events: dict[str, dict] = {}  # slug -> discovery event (first one)
        self.delays: dict[str, Counter] = {s: Counter() for s in PM_TS_STREAMS}
        self.kinds: Counter = Counter()  # ok / late / future
        self.recv = -(2**62)
        self._tick_wall = -(2**62)
        self._step_end: int | None = None  # inside a clock step back: `stop` written, `start` pending at this ms
        self._loop_ms: int | None = None
        self._n_res = 0  # router.new_resolutions already written

    def stamp(self) -> int:
        now = int(self.clock())
        prev = self.recv
        if prev > -(2**61) and now < prev - STEP_MS:
            # recv stays frozen at prev until the clock catches up, about prev - now later: everything stamped at prev
            # lies inside the gap [prev, prev + (prev - now)]. A deeper step extends it.
            end = prev + (prev - now)
            if self._step_end is None:
                self.writer.event(prev, "stop", "recorder", f"clock stepped back {prev - now} ms")
            self._step_end = end if self._step_end is None else max(self._step_end, end)
        self.recv = max(prev, now, self._tick_wall)
        if self._step_end is not None and now >= prev:
            self._close_step()
        return self.recv

    def _close_step(self) -> None:
        """Write the pending `start` of a clock step (one stop/start pair per episode, at the estimated catch-up)."""
        if self._step_end is not None:
            self.writer.event(self._step_end, "start", "recorder", "clock step")
            self._step_end = None

    def note_tick(self, wall_ms: int) -> None:
        """Call after `router.tick(wall_ms)`: later stamps are at least this wall, and the settler's resolutions the
        tick produced go to the session."""
        self._tick_wall = max(self._tick_wall, int(wall_ms))
        self.note_loop(wall_ms)
        self._write_resolutions()
        self.writer.maybe_flush()

    def _write_resolutions(self) -> None:
        if self.router is None:
            return
        res = self.router.new_resolutions
        while self._n_res < len(res):
            r = res[self._n_res]
            self._n_res += 1
            self.writer.write(LiveRow("pm_resolution", int(r.recv_ms), resolution_row(r, self.events.get(r.slug))))

    def on_rows(self, rows: list[tuple[str, object]]) -> None:
        recv = self.stamp()
        if self.held is not None:
            self.held.extend(LiveRow(stream, recv, values) for stream, values in rows)
            return
        self._write_resolutions()
        for stream, values in rows:
            self._route(LiveRow(stream, recv, values))

    def _route(self, row: LiveRow) -> None:
        stream = row.stream
        kind = self.router.ingest(row)
        self.kinds[kind] += 1
        if kind == "ok":
            self.writer.write(row)
            d = self.delays.get(stream)
            if d is not None:
                d[row.recv_ms - int(row.values[0])] += 1
            elif stream == "pm_market_meta":
                ev = row.values.get("event") if isinstance(row.values, dict) else None
                if isinstance(ev, dict) and ev.get("slug") is not None:
                    self.events.setdefault(str(ev["slug"]), ev)
        else:
            self.writer.write_late(row, self.router.late[-1][1])

    def attach(self, router: LiveRouter) -> None:
        """Start routing: the held rows go to `router` (and the session) in arrival order, then every later emit."""
        held, self.held = self.held or [], None
        self.router = router
        for row in held:
            self._route(row)

    def on_event(self, kind: str, source: str, detail: str) -> None:
        ts = self.stamp()
        self.writer.event(ts, kind, source, detail)
        if self.listener is not None:
            self.listener(kind, source, detail, ts)

    def note_loop(self, wall_ms: int) -> None:
        """A wall jump of more than 2 s since the previous `note_loop`/`note_tick` is an event-loop stall: a
        `recorder stop` at the previous call and `start` at this wall (a gap in every stream, judged by the
        unchanged gaps.py thresholds). `note_tick` calls this, so a stall is always seen."""
        last, self._loop_ms = self._loop_ms, int(wall_ms)
        if last is not None and wall_ms - last > STEP_MS:
            self.writer.event(last, "stop", "recorder", f"event loop stalled {wall_ms - last} ms")
            self.writer.event(int(wall_ms), "start", "recorder", "event loop stall")

    def finish(self) -> None:
        """After `router.finish()`: write the remaining resolutions and any pending clock-step `start`."""
        self._close_step()
        self._write_resolutions()

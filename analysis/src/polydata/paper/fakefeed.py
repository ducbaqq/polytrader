"""Fake feed (spec "Fake feed (offline testing)"): recorded rows injected into the ingest under a virtual clock, in
place of the WebSocket and REST clients (the ingest, router, settler, Confirmer and session writer are the real ones).

`VirtualClock` is the wall clock the runner and the ingest read. The runner's `sleep(dt)` raises the clock's target
by `dt`, lets the feeds deliver every row received up to the target (`sleep_until(ms)` blocks a feed until the target
reaches `ms`, then sets the clock to `ms`), and leaves the clock at the target. Each row group is therefore stamped
with its recorded `recv_ms` (the ingest stamps `max(previous, clock, last tick)` and the last tick is below it).
`speed=None` (`max`) jumps; `speed=N` also sleeps `dt / N` of real time per runner sleep, so virtual time runs at
about N times wall time.

`FakeFeed` rows come from a recorder store (`from_store`) or a paper session (`from_session`), merged across streams
by `recv_ms` (file order within a stream; at an equal `recv_ms`: connection events, then the streams in `STREAMS`
order) and delivered one `ingest.on_rows` call per `recv_ms`.

- `from_store(root, start_ms, end_ms, coins, timeframes)` reads `raw/` files whose 5-minute slot overlaps the range
  (rows with `recv_ms` in it), as text, in file order, filtered: Polymarket rows to the config's series (the `asset`
  slug, the meta `series`, the condition id of `pm_other` / `pm_new_market` messages), `rtds_price` to the config
  coins (`crypto_prices_chainlink` `<coin>/usd`, `crypto_prices` `<coin>usdt`), Binance (`bn_fut_bbo`,
  `bn_fut_trade`) to the config symbols. At `start_ms` it emits `ws_open` for every connection group the runner
  waits for and delivers the discovery (`pm_market_meta`) rows of config markets found before `start_ms` that have not
  ended (a live feed's first discovery poll), then the recorded connection events (`rec_events` catalog: `ws_*`,
  recorder `start`/`stop`) of the range at their time. Recorded `pm_resolution` rows are not rows here: they are the
  Gamma answers of the REST stub (plan Decision 7), available from their `recv_ms`; recorded `pm_api_trades` rows are
  its Data API answers. Files of sealed days are never opened (unless `allow_sealed`).
- `from_session(session)` replays a paper session's rows and connection events; its Gamma answers
  (`paper/gamma_resolution.jsonl.zst`) and `pm_api_trades` rows go to the REST stub; its `pm_resolution` rows (the
  settler's, or Gamma fallbacks) are not replayed (the settler and the Confirmer produce them again).

Fault injection (before `run`):
- `delay(stream, predicate, ms)`: rows of `stream` with `predicate(values)` arrive `ms` later (the late path when `ms`
  exceeds the hold-back: dropped by the router, written to `late/`, the market voided);
- `drop(source, from_ms, to_ms)`: that connection's rows received in `[from_ms, to_ms)` are lost, with `ws_close` at
  `from_ms` and `ws_open` at `to_ms` (a gap). Sources: `pm:<series or slug>`, `polymarket-rtds`, `binance-fut-bbo`
  (`bn_fut_bbo`), `binance-fut-market` (`bn_fut_trade`);
- `stall(at_ms, ms)`: the wall clock leaps `ms` when the runner's sleep crosses `at_ms` (an event-loop stall: a
  recorder gap in every stream).
"""

from __future__ import annotations

import asyncio
import csv
import heapq
import io
import json
import urllib.parse
from collections.abc import Callable, Iterator
from datetime import date, datetime, timezone
from pathlib import Path

import zstandard

from polydata import schema
from polydata.bt.core import sym_of
from polydata.bt.splits import day_kind
from polydata.paper.feeds.rest import RateLimited
from polydata.paper.feeds.series import LOOKAHEAD, RECURRENCE_MS, series_for

SLOT_MS = 300_000
HOUR_MS = 3_600_000
# Rows delivered at an equal recv_ms in this stream order (connection events first).
STREAMS = ["pm_market_meta", "pm_book", "pm_price_change", "pm_best_bid_ask", "pm_trade", "pm_other", "pm_new_market",
           "rtds_price", "bn_fut_bbo", "bn_fut_trade"]
_RANK = {s: i for i, s in enumerate(STREAMS)}
PM_TSV = ("pm_book", "pm_price_change", "pm_best_bid_ask", "pm_trade")
CONN_KINDS = ("ws_open", "ws_close", "ws_stale", "ws_error", "ws_rotate", "start", "stop")
REQUIRED_OPEN = ("polymarket-rtds", "binance-fut-bbo", "binance-fut-market")
_INF = 1 << 62


# ------------------------------------------------------------------------------------------- clock

class VirtualClock:
    def __init__(self, t0: int, speed: float | None = None, *, real_sleep: Callable | None = None):
        if speed is not None and speed <= 0:
            raise ValueError("speed must be > 0 (or None for max)")
        self.t = self.target = int(t0)
        self.speed = speed
        self.real_sleep = real_sleep or asyncio.sleep
        self.stalls: list[tuple[int, int]] = []  # (at_ms, ms)
        self._waiters: list[tuple[int, asyncio.Future]] = []
        self._active = 0  # feeds that may still deliver rows at or below the target

    def now(self) -> int:
        return self.t

    __call__ = now

    def stall(self, at_ms: int, ms: int) -> None:
        self.stalls.append((int(at_ms), int(ms)))

    # --- feed side
    def register(self) -> None:
        self._active += 1

    def unregister(self) -> None:
        self._active -= 1

    async def sleep_until(self, ms: int) -> None:
        """A feed: wait until the target reaches `ms`, then the clock is (at least) `ms`."""
        ms = int(ms)
        if ms > self.target:
            fut = asyncio.get_running_loop().create_future()
            self._waiters.append((ms, fut))
            self._active -= 1
            try:
                await fut  # _release counted this feed active again
            except asyncio.CancelledError:
                if not fut.done() or fut.cancelled():
                    self._active += 1
                raise
        if ms > self.t:
            self.t = ms

    def _release(self) -> None:
        keep = []
        for ms, fut in self._waiters:
            if ms <= self.target and not fut.done():
                self._active += 1
                fut.set_result(None)
            elif not fut.done():
                keep.append((ms, fut))
        self._waiters = keep

    # --- runner side
    async def sleep(self, dt_s: float) -> None:
        """The runner's sleep: virtual time advances `dt_s` (plus a stall it crosses); every row up to there is
        delivered first."""
        target = self.t + int(round(dt_s * 1000))
        for at, ms in self.stalls:
            if self.t < at <= target:
                target += ms
        if self.speed is not None:
            await self.real_sleep(dt_s / self.speed)
        self.target = max(self.target, target)
        self._release()
        await asyncio.sleep(0)
        while self._active > 0:
            await asyncio.sleep(0)
        if target > self.t:
            self.t = target


# ------------------------------------------------------------------------------------------- files

def _day_of(ms: int) -> date:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).date()


def _lines(path: Path) -> Iterator[str]:
    with open(path, "rb") as fh:
        reader = zstandard.ZstdDecompressor().stream_reader(fh, read_across_frames=True)
        for line in io.TextIOWrapper(reader, encoding="utf-8", newline="\n"):
            if line.endswith("\n"):
                line = line[:-1]
            if line:
                yield line


def _slot_of(path: Path) -> int:
    return int(path.name.split(".")[1])


def _files(raw: Path, stream: str, start_ms: int, end_ms: int, allow_sealed: bool) -> list[Path]:
    """`raw/<day>/<hour>/<stream>.<slot>.*.zst` with a slot overlapping [start_ms, end_ms), in slot order."""
    out = []
    h = start_ms // HOUR_MS * HOUR_MS - HOUR_MS
    while h < end_ms:
        t = datetime.fromtimestamp(h / 1000, timezone.utc)
        if allow_sealed or day_kind(t.date()) != "sealed":
            d = raw / t.strftime("%Y-%m-%d") / t.strftime("%H")
            if d.is_dir():
                for f in d.glob(f"{stream}.*.zst"):
                    s = _slot_of(f)
                    if s < end_ms and s + SLOT_MS > start_ms:
                        out.append(f)
        h += HOUR_MS
    return sorted(out, key=_slot_of)


def _tsv_rows(files: list[Path], stream: str, start_ms: int, end_ms: int,
              keep: Callable[[list[str]], bool] | None) -> Iterator[tuple[int, tuple]]:
    n = len(schema.TSV[stream]) - 1
    for f in files:
        for line in _lines(f):
            if line.startswith("recv_ms\t"):
                continue  # header (a file written in several runs may repeat it)
            parts = line.split("\t")
            recv = int(parts[0])
            if recv < start_ms or recv >= end_ms:
                continue
            v = parts[1:]
            if len(v) < n:
                v += [""] * (n - len(v))  # an older file without the appended columns
            elif len(v) > n:
                v = v[:n]
            if keep is None or keep(v):
                yield recv, tuple(v)


def _jsonl_rows(files: list[Path], start_ms: int, end_ms: int,
                keep: Callable[[object], bool] | None) -> Iterator[tuple[int, object]]:
    for f in files:
        for line in _lines(f):
            r = json.loads(line)
            recv = int(r["recv_ms"])
            if recv < start_ms or recv >= end_ms:
                continue
            d = r.get("d")
            if keep is None or keep(d):
                yield recv, d


def _events(catalog: Path, start_ms: int, end_ms: int, allow_sealed: bool) -> list[tuple[int, str, str, str]]:
    """Connection events (`CONN_KINDS`) of the `rec_events` catalog with ts in [start_ms, end_ms), in file order."""
    out = []
    for f in sorted(catalog.glob("*/rec_events.csv.zst")):
        d = date.fromisoformat(f.parent.name)
        if d < _day_of(start_ms) or (end_ms < _INF and d > _day_of(end_ms - 1)):
            continue
        if allow_sealed or day_kind(d) != "sealed":
            rows = csv.reader(io.StringIO("\n".join(_lines(f))))
            for r in rows:
                if len(r) < 4 or r[0] == "ts" or r[1] not in CONN_KINDS:
                    continue
                ts = _parse_ts(r[0])
                if start_ms <= ts < end_ms:
                    out.append((ts, r[1], r[2], r[3]))
    out.sort(key=lambda e: e[0])
    return out


def _parse_ts(s: str) -> int:
    """'YYYY-MM-DD HH:MM:SS[.ffffff]+00' (the catalog's timestamps) -> epoch ms."""
    s = s.strip()
    if s.endswith("+00"):
        s = s[:-3] + "+00:00"
    t = datetime.fromisoformat(s.replace(" ", "T"))
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return int(round(t.timestamp() * 1000))


def _source_rows(source: str, series_of: dict[str, str]) -> Callable[[str, object], bool]:
    """Which rows a connection carries (for `drop`)."""
    if source.startswith("pm:"):
        key = source[3:]

        def pm(stream, v):
            if stream in PM_TSV:
                slug = v[1].partition("|")[0]
                return slug == key or series_of.get(slug) == key
            return False

        return pm
    if source.startswith("polymarket-rtds"):
        return lambda stream, v: stream == "rtds_price"
    if source == "binance-fut-bbo":
        return lambda stream, v: stream == "bn_fut_bbo"
    if source == "binance-fut-market":
        return lambda stream, v: stream == "bn_fut_trade"
    raise ValueError(f"unknown connection {source!r}")


# ------------------------------------------------------------------------------------------- REST stub

def gamma_event(d: dict) -> dict:
    """The Gamma `/events` answer of a recorded `pm_resolution` row: its event, with the first market closed and
    `outcomePrices` saying the recorded outcome (what `confirm.gamma_outcome` reads)."""
    ev = dict(d["event"]) if isinstance(d.get("event"), dict) else {"slug": d["slug"]}
    mkts = [dict(m) if isinstance(m, dict) else {} for m in (ev.get("markets") or [{}])]
    want = '["1", "0"]' if d["outcome"] == "Up" else '["0", "1"]'
    from polydata.paper.confirm import gamma_outcome

    if gamma_outcome([{**ev, "markets": mkts}]) != d["outcome"]:
        mkts[0] = {**mkts[0], "closed": True, "outcomePrices": want}
    ev["markets"] = mkts
    return ev


class FakeRest:
    """The Confirmer's `Rest`: Gamma (`/events?slug=`) answers a market's recorded resolution event once the clock
    reaches its `recv_ms` (an open market before that); the Data API (`/v2/trades?condition_id=`) answers its recorded
    trades in one page once the clock reaches their `recv_ms` (rate limited before that, so the Confirmer retries
    without backing off), or no trades if none were recorded."""

    def __init__(self, clock: Callable[[], int], answers: dict[str, tuple[int, dict]],
                 trades: dict[str, tuple[int, list[dict]]], slug_of_cond: dict[str, str]):
        self.clock, self.answers, self.trades, self.slug_of_cond = clock, answers, trades, slug_of_cond
        self.calls: list[tuple[str, str]] = []

    def is_paused(self, api: str) -> bool:
        return False

    async def get_json(self, api: str, url: str) -> object:
        self.calls.append((api, url))
        q = urllib.parse.parse_qs(urllib.parse.urlparse(url).query)
        now = self.clock()
        if api == "gamma":
            slug = q.get("slug", [""])[0]
            a = self.answers.get(slug)
            if a is None or a[0] > now:
                return [{"slug": slug, "markets": [{"closed": False, "outcomePrices": "[\"0.5\", \"0.5\"]"}]}]
            return [a[1]]
        slug = self.slug_of_cond.get(q.get("condition_id", [""])[0])
        t = self.trades.get(slug) if slug is not None else None
        if t is None:
            return {"data": [], "pagination": {"has_more": False}}
        if t[0] > now:
            raise RateLimited(429, "not recorded yet")
        return {"data": [{k: v for k, v in r.items() if k != "slug"} for r in t[1]],
                "pagination": {"has_more": False}}


# ------------------------------------------------------------------------------------------- feed

class FakeFeed:
    def __init__(self, sources: list[tuple[str, Callable[[], Iterator[tuple[int, object]]]]], start_ms: int, *,
                 events: list[tuple[int, str, str, str]] = (), start_rows: list[tuple[str, object]] = (),
                 answers: dict[str, tuple[int, dict]] | None = None,
                 trades: dict[str, tuple[int, list[dict]]] | None = None,
                 slug_of_cond: dict[str, str] | None = None, series_of: dict[str, str] | None = None):
        self.sources = sources  # (stream, () -> iterator of (recv_ms, values)), each in file order
        self.start_ms = int(start_ms)
        self.events = list(events)
        self.start_rows = list(start_rows)
        self.answers = dict(answers or {})
        self.trades = dict(trades or {})
        self.slug_of_cond = dict(slug_of_cond or {})
        self.series_of = dict(series_of or {})
        self._delays: list[tuple[str, Callable, int]] = []
        self._drops: list[tuple[str, int, int, Callable]] = []
        self._stalls: list[tuple[int, int]] = []
        self.delivered = 0  # rows
        self.last_recv: int | None = None
        self.done = False

    # --- construction
    @classmethod
    def from_store(cls, root: Path, start_ms: int, end_ms: int, coins, timeframes, *,
                   allow_sealed: bool = False) -> "FakeFeed":
        root, start_ms, end_ms = Path(root), int(start_ms), int(end_ms)
        raw = root / "raw"
        series = series_for(coins, timeframes)
        names = {s.slug for s in series}
        coins_l = {c.lower() for c in coins}
        syms = {sym_of(c) for c in coins_l}
        cl_syms = {f"{c}/usd" for c in coins_l}
        px_syms = {f"{c}usdt" for c in coins_l}
        lookback = max([(LOOKAHEAD[s.recurrence] + 1) * RECURRENCE_MS[s.recurrence] for s in series] or [0])

        # discovery rows of config markets: before the start (still running ones are delivered at the start) and in
        # the range (their slugs and condition ids filter the Polymarket rows)
        from polydata.bt.marketdata_live import info_from_meta

        metas = list(_jsonl_rows(_files(raw, "pm_market_meta", start_ms - lookback, end_ms, allow_sealed),
                                 start_ms - lookback, end_ms,
                                 lambda d: isinstance(d, dict) and d.get("series") in names))
        slugs: set[str] = set()
        conds: set[str] = set()
        series_of: dict[str, str] = {}
        slug_of_cond: dict[str, str] = {}
        start_rows, seen = [], set()
        for recv, d in metas:
            ev = d.get("event") if isinstance(d.get("event"), dict) else {}
            slug = ev.get("slug")
            if slug is None:
                continue
            m = (ev.get("markets") or [{}])[0] or {}
            slugs.add(slug)
            series_of[slug] = d["series"]
            if m.get("conditionId"):
                conds.add(m["conditionId"])
                slug_of_cond[m["conditionId"]] = slug
            if recv < start_ms and slug not in seen:
                seen.add(slug)
                try:
                    info = info_from_meta(d, recv)
                except (ValueError, KeyError, TypeError):
                    continue
                if int(info.window_end_ms) > start_ms:
                    start_rows.append(("pm_market_meta", d))

        def files(stream):
            return _files(raw, stream, start_ms, end_ms, allow_sealed)

        def pm_keep(v):
            return v[1].partition("|")[0] in slugs

        def tsv(stream, keep):
            return lambda: _tsv_rows(files(stream), stream, start_ms, end_ms, keep)

        def jsonl(stream, keep):
            return lambda: _jsonl_rows(files(stream), start_ms, end_ms, keep)

        def rtds_keep(v):
            return v[1] in cl_syms if v[0] == "crypto_prices_chainlink" else v[1] in px_syms

        sources = [
            ("pm_market_meta", jsonl("pm_market_meta", lambda d: isinstance(d, dict) and d.get("series") in names)),
            *[(s, tsv(s, pm_keep)) for s in PM_TSV],
            ("pm_other", jsonl("pm_other", lambda d: isinstance(d, dict) and d.get("market") in conds)),
            ("pm_new_market", jsonl("pm_new_market", lambda d: isinstance(d, dict) and (
                d.get("market") in conds or d.get("slug") in slugs))),
            ("rtds_price", tsv("rtds_price", rtds_keep)),
            ("bn_fut_bbo", tsv("bn_fut_bbo", lambda v: v[0] in syms)),
            ("bn_fut_trade", tsv("bn_fut_trade", lambda v: v[0] in syms)),
        ]
        sources = [(s, f) for s, f in sources if s in schema.TSV or s in schema.JSONL]

        answers: dict[str, tuple[int, dict]] = {}
        for recv, d in _jsonl_rows(files("pm_resolution"), start_ms, end_ms,
                                   lambda d: isinstance(d, dict) and d.get("slug") in slugs):
            if d.get("outcome") in ("Up", "Down") and d["slug"] not in answers:
                answers[d["slug"]] = (recv, gamma_event(d))
        trades: dict[str, tuple[int, list[dict]]] = {}
        for recv, d in _jsonl_rows(files("pm_api_trades"), start_ms, end_ms,
                                   lambda d: isinstance(d, dict) and d.get("slug") in slugs):
            r, rows = trades.get(d["slug"], (recv, []))
            rows.append(d)
            trades[d["slug"]] = (max(r, recv), rows)

        events = [(start_ms, "ws_open", s, "fake feed") for s in REQUIRED_OPEN]
        events += [(start_ms, "ws_open", f"pm:{s.slug}", "fake feed") for s in series]
        events += _events(root / "catalog", start_ms, end_ms, allow_sealed)
        return cls(sources, start_ms, events=events, start_rows=start_rows, answers=answers, trades=trades,
                   slug_of_cond=slug_of_cond, series_of=series_of)

    @classmethod
    def from_session(cls, session: Path) -> "FakeFeed":
        session = Path(session)
        man = json.loads((session / "manifest.json").read_text())
        raw = session / "raw"
        start = None
        evs = _events(session / "catalog", 0, _INF, True) if (session / "catalog").exists() else []
        if evs:
            start = evs[0][0]
        lo, hi = 0, _INF

        def files(stream):
            return sorted(raw.rglob(f"{stream}.*.zst"), key=_slot_of)

        sources = []
        for s in STREAMS:
            if s in schema.TSV:
                sources.append((s, (lambda s=s: _tsv_rows(files(s), s, lo, hi, None))))
            else:
                sources.append((s, (lambda s=s: _jsonl_rows(files(s), lo, hi, None))))
        slug_of_cond, series_of = {}, {}
        for recv, d in _jsonl_rows(files("pm_market_meta"), lo, hi, None):
            ev = d.get("event") or {}
            m = (ev.get("markets") or [{}])[0] or {}
            if ev.get("slug") and m.get("conditionId"):
                slug_of_cond[m["conditionId"]] = ev["slug"]
                series_of[ev["slug"]] = d.get("series")
            if start is None or recv < start:
                start = recv
        from polydata.paper.results import load_gamma_answers

        answers = {s: (int(a["recv_ms"]), a["event"]) for s, a in load_gamma_answers(session).items()}
        trades: dict[str, tuple[int, list[dict]]] = {}
        for recv, d in _jsonl_rows(files("pm_api_trades"), lo, hi, None):
            r, rows = trades.get(d["slug"], (recv, []))
            rows.append(d)
            trades[d["slug"]] = (max(r, recv), rows)
        start = int(start if start is not None else man.get("rs") or 0)
        return cls(sources, start, events=evs, answers=answers, trades=trades, slug_of_cond=slug_of_cond,
                   series_of=series_of)

    # --- faults
    def delay(self, stream: str, predicate: Callable[[object], bool], ms: int) -> "FakeFeed":
        self._delays.append((stream, predicate, int(ms)))
        return self

    def drop(self, source: str, from_ms: int, to_ms: int) -> "FakeFeed":
        self._drops.append((source, int(from_ms), int(to_ms), _source_rows(source, self.series_of)))
        self.events += [(int(from_ms), "ws_close", source, "fake drop"), (int(to_ms), "ws_open", source, "fake drop")]
        self.events.sort(key=lambda e: e[0])
        return self

    def stall(self, at_ms: int, ms: int) -> "FakeFeed":
        self._stalls.append((int(at_ms), int(ms)))
        return self

    # --- delivery
    def rest(self, clock: Callable[[], int]) -> FakeRest:
        return FakeRest(clock, self.answers, self.trades, self.slug_of_cond)

    def rest_factory(self) -> Callable:
        """For `PaperRun(rest_factory=...)`."""
        return lambda clock, event: self.rest(clock)

    def _merged(self) -> Iterator[tuple]:
        """(recv, rank, k, kind, a, b): kind 'row' (stream, values) or 'event' (kind, (source, detail))."""
        def tag(stream, it):
            r = _RANK[stream]
            for k, (recv, v) in enumerate(it):
                yield recv, r, k, "row", stream, v

        def evs():
            for k, (ts, kind, source, detail) in enumerate(self.events):
                yield ts, -2, k, "event", kind, (source, detail)

        start = [(self.start_ms, -1, k, "row", s, v) for k, (s, v) in enumerate(self.start_rows)]
        its = [iter(start), evs()] + [tag(s, f()) for s, f in self.sources]
        merged = heapq.merge(*its)
        if not self._delays and not self._drops:
            yield from merged
            return
        held: list[tuple] = []
        n = 0
        for item in merged:
            while held and held[0][0] <= item[0]:
                yield heapq.heappop(held)
            if item[3] == "row":
                recv, stream, v = item[0], item[4], item[5]
                if any(a <= recv < b and f(stream, v) for _, a, b, f in self._drops):
                    continue
                d = next((ms for s, p, ms in self._delays if s == stream and p(v)), None)
                if d:
                    n += 1
                    heapq.heappush(held, (recv + d, 99, n, "row", stream, v))
                    continue
            yield item
        while held:
            yield heapq.heappop(held)

    async def run(self, ingest, clock: VirtualClock) -> None:
        """Deliver every row and event at its recorded time (rows of one `recv_ms` in one `on_rows` call), then idle
        until cancelled."""
        for at, ms in self._stalls:
            clock.stall(at, ms)
        clock.register()
        try:
            group: list = []
            g_recv = None
            for recv, _, _, kind, a, b in self._merged():
                if group and recv != g_recv:
                    self._emit(ingest, group, g_recv)
                    group = []
                if recv > clock.target:
                    await clock.sleep_until(recv)
                elif recv > clock.t:
                    clock.t = recv
                g_recv = recv
                if kind == "event":
                    if group:
                        self._emit(ingest, group, g_recv)
                        group = []
                    ingest.on_event(a, b[0], b[1])
                else:
                    group.append((a, b))
            if group:
                self._emit(ingest, group, g_recv)
            self.done = True
            await clock.sleep_until(_INF)  # nothing more: idle until the runner cancels the feed
        finally:
            clock.unregister()

    def _emit(self, ingest, group: list, recv: int) -> None:
        ingest.on_rows(group)
        self.delivered += len(group)
        self.last_recv = recv

    def factory(self, clock: VirtualClock) -> Callable:
        """For `PaperRun(feeds_factory=...)`."""
        return lambda ingest, _clock: [self.run(ingest, clock)]


__all__ = ["FakeFeed", "FakeRest", "STREAMS", "VirtualClock"]

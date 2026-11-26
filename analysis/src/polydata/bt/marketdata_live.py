"""Live (incrementally built) market data for the paper runner: `GrowingMarketData`, Binance and Chainlink hour
buffers, the live 1 s Binance grid, and `MarketInfo` from a discovery row.

`GrowingMarketData` answers every query `MarketData` answers (it inherits the query code) and gives the same
answers as `MarketData.from_frames` / `load_coin` over the same rows, as long as rows are fed the way the live
runner releases them (spec "The hold-back"):

- **Ts side** (`add_ts`): one complete `ts_ms` at a time, in increasing `ts_ms`. The SQL pipeline's rules
  (`marketdata`: `_SQL_CHANGES`, `_SQL_SNAPSHOTS`, `_SQL_LEVELS`, `_SQL_TOPS`, `_SQL_TRADES_OUT`, `_SQL_TICKS_OUT`)
  are evaluated per `ts_ms`, which is enough because every rule only compares rows of the same `ts_ms` (the Down
  mirror check, the snapshot choice) or depends on the state before it (zeroed levels, `recv_eff`, `last_pos`,
  the "top changed" filter):
  1. prices are `round(x * 1000)` (half away from zero, as DuckDB's `round`); an empty best bid is 0, an empty
     or out-of-range best ask 1000 (`_BID` / `_ASK`);
  2. Up change rows are used; a Down row is used (mapped to Up terms) only if no Up row of this `ts_ms` has the
     opposite token side and `px == 1000 - down_px`;
  3. one snapshot per `ts_ms`: Up preferred, then the largest `(recv_ms, seq)`. Its listed levels are set, and
     every level that is positive (after this `ts_ms`'s changes) and not listed gets a size-0 entry;
  4. level events in `(k, recv_ms, seq, src, size)` order, `k = 2 ts` for changes (`src` 0) and `2 ts + 1` for the
     snapshot (`src` 1 listed, 2 zeroed); per level `ts_ms`, `recv_ms`, `recv_eff` (running max of `recv_ms`),
     `size`, `last_pos`;
  5. ts-order tops: change rows' `(bb, ba)` and the snapshot top in `(k, recv_ms, seq)` order, kept where
     `(bid, ask)` differs from the previous candidate;
  6. trades in `(ts_ms, recv_ms, seq)` order; tick sizes deduplicated on `(ts_ms, tick_px)`, earliest `recv_ms`.
  Polymarket rows outside `load_coin`'s per-market range `[ws - LOOKBACK_WINDOWS * len - MARGIN_MS,
  we + MARGIN_MS]` are ignored (plan Decision 8); tick size changes are not range-filtered (neither are they in
  `load_coin`).
- **Recv side** (`take_recv` / `add_recv`): the entries the ts side produced, in `(recv_ms, k, seq)` order, up to the
  strategy clock T: `tops_recv` (with the same "changed" filter against the previous candidate in recv order),
  `trades_recv`, recv-order ticks, `snaps_recv`.

Every row with `recv_ms <= T` has `ts_ms <= T + skew = H_ts`, so with the hold-back every query the core makes
(ts clock `<= H_ts`, recv clock `<= T`) is answered from complete data, and appending never changes an answer
that was already visible: arrays only grow at the end (capacity-doubling buffers, exposed as views).

**Late rows before the open** (`insert_late`): until the router decides the market's open (`drop_history`), every
`add_ts` call is kept. A late row of a market that has not opened (spec "Gaps", quiet book) is inserted at its
`ts_ms` and the market data is rebuilt in place from that history (ts side in `ts_ms` order, then the recv side up
to where it was released), so it equals what `load_coin` builds from the session's rows. Nothing of a market is
visible to the strategy before its open, and the late row's recv entries lie after the strategy clock.
"""

from __future__ import annotations

import bisect
import heapq
import json
import math
from typing import NamedTuple

import numpy as np

from .core import SIG_WINDOW_S, _Bn, _grid_segment
from .marketdata import LOOKBACK_WINDOWS, MARGIN_MS, MarketData, Ticks, Tops, Trades, _info
from .types import MarketInfo

# ---------------------------------------------------------------------------------------- buffers


class GrowingArray:
    """A capacity-doubling 1-D numpy buffer. `view` is the filled part (a view: it does not see later appends)."""

    __slots__ = ("_buf", "n")

    def __init__(self, dtype, capacity: int = 16):
        self._buf = np.empty(max(int(capacity), 1), dtype)
        self.n = 0

    def _grow(self, need: int) -> None:
        cap = len(self._buf)
        while cap < need:
            cap *= 2
        buf = np.empty(cap, self._buf.dtype)
        buf[:self.n] = self._buf[:self.n]
        self._buf = buf

    def push(self, value) -> None:
        """Append one value."""
        if self.n == len(self._buf):
            self._grow(self.n + 1)
        self._buf[self.n] = value
        self.n += 1

    def append(self, values) -> None:
        """Append a scalar or a 1-D sequence."""
        a = np.asarray(values, self._buf.dtype)
        if a.ndim == 0:
            self.push(a)
            return
        m = self.n + len(a)
        if m > len(self._buf):
            self._grow(m)
        self._buf[self.n:m] = a
        self.n = m

    @property
    def view(self) -> np.ndarray:
        return self._buf[:self.n]

    def __len__(self) -> int:
        return self.n


class _Group:
    """GrowingArrays exposed together as a NamedTuple of views, rebuilt only when the length changed."""

    __slots__ = ("cls", "arrays", "_n", "_tuple")

    def __init__(self, cls, dtypes):
        self.cls = cls
        self.arrays = [GrowingArray(d) for d in dtypes]
        self._n = -1
        self._tuple = None

    def push(self, *values) -> None:
        for a, v in zip(self.arrays, values):
            a.push(v)

    def get(self):
        n = self.arrays[0].n
        if n != self._n:
            self._tuple = self.cls(*(a.view for a in self.arrays))
            self._n = n
        return self._tuple

    def row(self, i: int) -> tuple:
        return tuple(a._buf[i] for a in self.arrays)

    def __len__(self) -> int:
        return self.arrays[0].n


_TOPS_T = (np.int64, np.int64, np.int32, np.int32)
_TRADES_T = (np.int64, np.int64, np.int8, np.int8, np.float64, np.float64, np.int8, np.int32)
_TICKS_T = (np.int64, np.int64, np.int32)


class _GLevel:
    """One level's event series, growing; the attributes `MarketData` queries read (`marketdata._Level`) are views."""

    __slots__ = ("_ts", "_recv", "_eff", "_size", "_lp", "cur", "eff", "lp")

    def __init__(self):
        self._ts, self._recv, self._eff = GrowingArray(np.int64), GrowingArray(np.int64), GrowingArray(np.int64)
        self._size, self._lp = GrowingArray(np.float64), GrowingArray(np.int64)
        self.cur = 0.0  # size after the last event
        self.eff = None  # running max of recv_ms
        self.lp = -1  # index of the last event with size > 0

    def add(self, ts: int, recv: int, size: float) -> None:
        i = self._ts.n
        self.eff = recv if self.eff is None or recv > self.eff else self.eff
        if size > 0:
            self.lp = i
        self.cur = size
        self._ts.push(ts)
        self._recv.push(recv)
        self._eff.push(self.eff)
        self._size.push(size)
        self._lp.push(self.lp)

    ts_ms = property(lambda self: self._ts.view)
    recv_ms = property(lambda self: self._recv.view)
    recv_eff = property(lambda self: self._eff.view)
    size = property(lambda self: self._size.view)
    last_pos = property(lambda self: self._lp.view)


# ---------------------------------------------------------------------------------------- parsing

def _rnd(x: float) -> int:
    """DuckDB's round() of a DOUBLE: half away from zero (Python's round() is half to even)."""
    if x < 0:
        return -_rnd(-x)
    r = math.floor(x)
    return int(r + 1 if x - r >= 0.5 else r)


def _num(s) -> float | None:
    """A recorder text field as DOUBLE; an empty field (NULL in Parquet) is None."""
    if s is None or s == "":
        return None
    return float(s)


def _bid(s) -> int:  # marketdata._BID
    b = _num(s)
    return _rnd(b * 1000) if b is not None and b > 0 else 0


def _ask(s) -> int:  # marketdata._ASK
    a = _num(s)
    return _rnd(a * 1000) if a is not None and 0 < a < 1 else 1000


def _book_levels(s) -> list[tuple[float, float]]:
    """views.levels(): 'price:size|price:size' -> [(price, size)], recorded order; empty -> []."""
    if not s:
        return []
    out = []
    for x in s.split("|"):
        if x != "":
            p, _, q = x.partition(":")
            out.append((float(p), float(q.split(":")[0])))
    return out


# Recv-side entry kinds (heap tie-break between kinds only; each kind's own order is (recv_ms, k, seq)).
_KINDS = ("top", "trade", "tick", "snap")
_K_TOP, _K_TRADE, _K_TICK, _K_SNAP = range(4)


# ---------------------------------------------------------------------------------------- market data

class GrowingMarketData(MarketData):
    """One market's Polymarket data, appended as the live runner releases it (module docstring). The query
    methods are `MarketData`'s, over views of the growing arrays."""

    def __init__(self, info: MarketInfo):  # noqa: D107 (MarketData.__init__ builds from finished arrays)
        self.info = info
        length = int(info.window_end_ms) - int(info.window_start_ms)
        self._lo = int(info.window_start_ms) - LOOKBACK_WINDOWS * length - MARGIN_MS
        self._hi = int(info.window_end_ms) + MARGIN_MS
        self._prefix = f"{info.slug}|"
        self._levels: dict[tuple[str, int], _GLevel] = {}
        self._keys: dict[str, list[int]] = {"BUY": [], "SELL": []}
        self._pos: set[tuple[str, int]] = set()  # levels whose current size is > 0
        self._tops, self._tops_recv = _Group(Tops, _TOPS_T), _Group(Tops, _TOPS_T)
        self._trades, self._trades_recv = _Group(Trades, _TRADES_T), _Group(Trades, _TRADES_T)
        self._ticks, self._ticks_r = _Group(Ticks, _TICKS_T), _Group(Ticks, _TICKS_T)
        self._snaps = GrowingArray(np.int64)
        self._top_ts: tuple[int, int] | None = None  # last ts-order top candidate
        self._top_recv: tuple[int, int] | None = None  # last recv-order top candidate
        self._cand: dict[int, tuple[int, int]] = {}  # top candidates waiting for the recv side
        self._ncand = 0
        self._pending: list[tuple] = []  # heap of recv-side entries: (recv_ms, k, seq, kind, ts_ms, ref)
        self.last_ts: int | None = None  # last ts_ms released
        self.last_recv: int | None = None  # last recv_ms released on the recv side
        self._hist: list[tuple] | None = []  # every add_ts call until drop_history (for insert_late)
        self._released: int | None = None  # the recv side is released up to here

    # -- views
    tops = property(lambda self: self._tops.get())
    tops_recv = property(lambda self: self._tops_recv.get())
    trades = property(lambda self: self._trades.get())
    trades_recv = property(lambda self: self._trades_recv.get())
    ticks = property(lambda self: self._ticks.get())
    _ticks_recv = property(lambda self: self._ticks_r.get())
    snaps_recv = property(lambda self: self._snaps.view)

    @classmethod
    def from_frames(cls, *args, **kwargs):  # pragma: no cover
        raise TypeError("GrowingMarketData is fed with add_ts / add_recv; use MarketData.from_frames")

    # -- ts side
    def _level(self, key: tuple[str, int]) -> _GLevel:
        lv = self._levels.get(key)
        if lv is None:
            lv = self._levels[key] = _GLevel()
            side, px = key
            keys = self._keys[side]
            if side == "BUY":  # descending
                keys.insert(bisect.bisect_left(keys, -px, key=lambda p: -p), px)
            else:
                keys.insert(bisect.bisect_left(keys, px), px)
        return lv

    def _set(self, key: tuple[str, int], ts: int, recv: int, size: float) -> None:
        self._level(key).add(ts, recv, size)
        if size > 0:
            self._pos.add(key)
        else:
            self._pos.discard(key)

    def _top(self, k: int, ts: int, recv: int, seq: int, bid: int, ask: int) -> None:
        if self._top_ts != (bid, ask):
            self._tops.push(ts, recv, bid, ask)
            self._top_ts = (bid, ask)
        ref = self._ncand
        self._ncand += 1
        self._cand[ref] = (bid, ask)
        heapq.heappush(self._pending, (recv, k, seq, _K_TOP, ts, ref))

    def _asset(self, asset: str, ts_text, ts: int) -> bool:
        """True for the Down token. Checks the row belongs to this market and this ts_ms."""
        if int(ts_text) != ts:
            raise ValueError(f"{self.info.slug}: row ts_ms {ts_text} released with ts_ms {ts}")
        if not asset.startswith(self._prefix):
            raise ValueError(f"{self.info.slug}: row of another market ({asset})")
        o = asset[len(self._prefix):]
        if o not in ("U", "D"):
            raise ValueError(f"{self.info.slug}: bad asset {asset!r}")
        return o == "D"

    def add_ts(self, ts_ms: int, changes: list[tuple[int, int, tuple]], books: list[tuple[int, int, tuple]],
               trades: list[tuple[int, int, tuple]], ticks: list[tuple[int, float]]) -> int:
        """Every row with this `ts_ms` (call once per `ts_ms`, in increasing `ts_ms`): `changes`, `books`, `trades`
        as `(recv_ms, seq, values)` with `values` the recorder columns after `recv_ms` (text) of `pm_price_change`,
        `pm_book`, `pm_trade`; `ticks` as `(recv_ms, new_tick_size)`. `seq` is the row's arrival order in its
        stream. Returns the number of trades appended."""
        ts = int(ts_ms)
        if self.last_ts is not None and ts <= self.last_ts:
            raise ValueError(f"{self.info.slug}: ts_ms {ts} released after {self.last_ts}")
        self.last_ts = ts
        if self._hist is not None:
            self._hist.append((ts, list(changes), list(books), list(trades), list(ticks)))
        if ticks:
            self._add_ticks(ts, ticks)
        if not self._lo <= ts <= self._hi:
            return 0
        k = 2 * ts

        # 1-2. change rows in Up terms (_SQL_CHANGES)
        up, down = [], []
        for recv, seq, v in changes:
            dn = self._asset(v[1], v[0], ts)
            row = (int(recv), int(seq), v[2] == "SELL", _rnd(float(v[3]) * 1000), float(v[4]), _bid(v[5]), _ask(v[6]))
            (down if dn else up).append(row)
        if down:
            mirrored = {(px, sell) for _, _, sell, px, _, _, _ in up}
            for recv, seq, sell, px, size, bb, ba in down:
                if (1000 - px, not sell) not in mirrored:
                    up.append((recv, seq, not sell, 1000 - px, size, 1000 - ba if ba < 1000 else 0,
                               1000 - bb if bb > 0 else 1000))
        up.sort(key=lambda r: (r[0], r[1], r[4]))  # (recv_ms, seq, size): _ORDER within k
        for recv, seq, sell, px, size, bb, ba in up:
            self._set(("SELL" if sell else "BUY", px), ts, recv, size)
            self._top(k, ts, recv, seq, bb, ba)

        # 3. the snapshot (_SQL_SNAPSHOTS, _zero)
        if books:
            best = None
            for recv, seq, v in books:
                is_up = not self._asset(v[1], v[0], ts)
                key = (is_up, int(recv), int(seq))
                if best is None or key > best[0]:
                    best = (key, v)
            (is_up, srecv, sseq), v = best
            listed: list[tuple[bool, int, float]] = []  # (sell in Up terms, px, size)
            for tside, text in (("BUY", v[2]), ("SELL", v[3])):
                sell = is_up == (tside == "SELL")
                for p, q in _book_levels(text):
                    px = _rnd(p * 1000)
                    listed.append((sell, px if is_up else 1000 - px, q))
            keys = {("SELL" if s else "BUY", px) for s, px, _ in listed}
            zero = [key for key in self._pos if key not in keys]
            listed.sort(key=lambda r: r[2])  # same level listed twice: _ORDER ends with size
            for sell, px, q in listed:
                self._set(("SELL" if sell else "BUY", px), ts, srecv, q)
            for key in zero:
                self._set(key, ts, srecv, 0.0)
            bid = max((px for s, px, q in listed if not s and q > 0), default=0)
            ask = min((px for s, px, q in listed if s and q > 0), default=1000)
            self._top(k + 1, ts, srecv, sseq, bid, ask)
            heapq.heappush(self._pending, (srecv, k + 1, sseq, _K_SNAP, ts, -1))

        # 6. trades (_SQL_TRADES_OUT)
        rows = []
        for recv, seq, v in trades:
            dn = self._asset(v[1], v[0], ts)
            sell, price = v[2] == "SELL", float(v[3])
            px = _rnd(price * 1000)
            rows.append((int(recv), int(seq), int(dn), int(sell), price, float(v[4]), int(dn != sell),
                         1000 - px if dn else px))
        rows.sort(key=lambda r: (r[0], r[1]))
        for recv, seq, *rest in rows:
            ref = len(self._trades)
            self._trades.push(ts, recv, *rest)
            heapq.heappush(self._pending, (recv, k, seq, _K_TRADE, ts, ref))
        return len(rows)

    def _add_ticks(self, ts: int, ticks: list[tuple[int, float]]) -> None:
        """_SQL_TICKS_OUT: one row per (ts_ms, tick_px), earliest recv_ms; (ts_ms, recv_ms) order."""
        first: dict[int, int] = {}
        for recv, size in ticks:
            px, recv = _rnd(float(size) * 1000), int(recv)
            if px not in first or recv < first[px]:
                first[px] = recv
        for recv, px in sorted((r, p) for p, r in first.items()):
            ref = len(self._ticks)
            self._ticks.push(ts, recv, px)
            heapq.heappush(self._pending, (recv, 2 * ts, ref, _K_TICK, ts, ref))

    # -- recv side
    def take_recv(self, t: int) -> list[tuple[str, int, int, int, int]]:
        """The released ts-side entries with `recv_ms <= t` not yet on the recv side, as `add_recv` rows
        `(kind, recv_ms, ts_ms, seq, ref)` in `(recv_ms, k, seq)` order."""
        out, heap = [], self._pending
        while heap and heap[0][0] <= t:
            recv, _, seq, kind, ts, ref = heapq.heappop(heap)
            out.append((_KINDS[kind], recv, ts, seq, ref))
        return out

    def add_recv(self, rows: list[tuple[str, int, int, int, int]]) -> None:
        """Recv-side entries `(kind, recv_ms, ts_ms, seq, ref)` in `(recv_ms, k, seq)` order (as `take_recv` gives
        them): `ref` is the entry's top candidate (`top`), ts-order trade (`trade`) or tick (`tick`) index."""
        for kind, recv, ts, seq, ref in rows:
            if self.last_recv is not None and recv < self.last_recv:
                raise ValueError(f"{self.info.slug}: recv_ms {recv} released after {self.last_recv}")
            self.last_recv = recv
            if kind == "top":
                bid, ask = self._cand.pop(ref)
                if self._top_recv != (bid, ask):
                    self._tops_recv.push(ts, recv, bid, ask)
                    self._top_recv = (bid, ask)
            elif kind == "trade":
                self._trades_recv.push(*self._trades.row(ref))
            elif kind == "tick":
                self._ticks_r.push(*self._ticks.row(ref))
            elif kind == "snap":
                self._snaps.push(recv)
            else:
                raise ValueError(f"unknown recv-side kind {kind!r}")

    def release_recv(self, t: int) -> int:
        """`add_recv(take_recv(t))`; returns the number of entries released."""
        rows = self.take_recv(t)
        self.add_recv(rows)
        if self._released is None or t > self._released:
            self._released = int(t)
        return len(rows)

    # -- late rows before the open
    @property
    def has_history(self) -> bool:
        return self._hist is not None

    def drop_history(self) -> None:
        """The market's open is decided: no late row is inserted any more."""
        self._hist = None

    def insert_late(self, ts_ms: int, k: int, entry: tuple) -> None:
        """Insert one late row (`k`: 0 change, 1 book, 2 trade as `(recv_ms, seq, values)`; 3 tick size as
        `(recv_ms, new_tick_size)`) at its `ts_ms` (already released), and rebuild in place from the history: the
        ts side in `ts_ms` order, then the recv side up to where it was released."""
        if self._hist is None:
            raise ValueError(f"{self.info.slug}: no history (the market's open is decided)")
        ts = int(ts_ms)
        hist = self._hist
        i = bisect.bisect_left(hist, ts, key=lambda h: h[0])
        if i < len(hist) and hist[i][0] == ts:
            hist[i][1 + k].append(entry)
        else:
            g = (ts, [], [], [], [])
            g[1 + k].append(entry)
            hist.insert(i, g)
        cap = self.__dict__.get("recv_cap")
        released = self._released
        self.__init__(self.info)
        if cap is not None:
            self.recv_cap = cap
        for h in hist:
            self.add_ts(*h)
        if released is not None:
            self.release_recv(released)

    def pending_recv(self) -> int:
        return len(self._pending)


# ---------------------------------------------------------------------------------------- Binance / Chainlink

class LiveBn:
    """One symbol's Binance bundles for one hour, growing (duck-types `core._Bn`): the last row by
    `(recv_ms, update_id)` of each `recv_ms // step_ms` bucket, delivered at the bucket's last ms
    (`engine._load_bn`). Feed rows in nondecreasing `recv_ms` with `add`; `flush(T)` appends the bucket in
    progress once T reaches its last ms (a bucket is also appended when a row of a later bucket arrives)."""

    def __init__(self, step_ms: int):
        self.step = int(step_ms)
        self._g = _Group(_Bn, (np.int64, np.float64, np.float64, np.float64, np.float64))
        self._b: int | None = None  # bucket in progress
        self._best: tuple | None = None  # ((recv_ms, update_id), bid, ask, bid_qty, ask_qty)

    def add(self, recv_ms: int, update_id: int, bid: float, bid_qty: float, ask: float, ask_qty: float) -> None:
        b = int(recv_ms) // self.step
        if self._b is not None and b != self._b:
            if b < self._b:
                raise ValueError(f"Binance row at {recv_ms} after bucket {self._b}")
            self._emit()
        key = (int(recv_ms), int(update_id))
        if self._best is None or key >= self._best[0]:
            self._best = (key, float(bid), float(ask), float(bid_qty), float(ask_qty))
        self._b = b

    def _emit(self) -> None:
        _, bid, ask, bq, aq = self._best
        self._g.push(self._b * self.step + self.step - 1, bid, ask, bq, aq)
        self._b = self._best = None

    def flush(self, t: int) -> None:
        if self._b is not None and self._b * self.step + self.step - 1 <= t:
            self._emit()

    @property
    def bn(self) -> _Bn:
        return self._g.get()

    recv = property(lambda self: self._g.get().recv)
    bid = property(lambda self: self._g.get().bid)
    ask = property(lambda self: self._g.get().ask)
    bid_qty = property(lambda self: self._g.get().bid_qty)
    ask_qty = property(lambda self: self._g.get().ask_qty)

    def __len__(self) -> int:
        return len(self._g)


class LiveCl:
    """One coin's received Chainlink ticks (recv_ms, ts_ms, price), growing: the first receipt of each `ts_ms` wins
    and a tick is kept only if that first receipt is at or after `rs` (`engine._load_cl`)."""

    def __init__(self, rs: int):
        self.rs = int(rs)
        self._seen: set[int] = set()
        self._g = [GrowingArray(np.int64), GrowingArray(np.int64), GrowingArray(np.float64)]
        self._n = -1
        self._t = None

    def add(self, rows: list[tuple[int, int, float]]) -> int:
        """Rows `(recv_ms, ts_ms, price)` received together (every row of each recv_ms in one call, in nondecreasing
        recv_ms across calls). Returns the number of ticks kept."""
        n = 0
        for recv, ts, price in sorted(rows, key=lambda r: (r[0], r[1])):
            ts = int(ts)
            if ts in self._seen:
                continue
            self._seen.add(ts)
            if recv >= self.rs:
                for a, v in zip(self._g, (recv, ts, price)):
                    a.push(v)
                n += 1
        return n

    @property
    def arrays(self) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(recv_ms, ts_ms, price) views, the layout of `Core.cl[coin]`."""
        if self._g[0].n != self._n:
            self._t = tuple(a.view for a in self._g)
            self._n = self._g[0].n
        return self._t


class LiveGrid:
    """`core._Grid` built second by second: `start_segment(sec_from)` at the range start and at each hour boundary
    (as `_Grid.append` is called per hour), `set_bundles(bn, sec_done)` once the seconds before `sec_done` are
    complete (every bundle in them is in `bn`). The current segment's prefix is recomputed with `_grid_segment`, so
    `sig15` is bit-identical to `_Grid`'s after the whole hour."""

    def __init__(self):
        self.sec0: int | None = None
        self.mids = np.empty(0)  # committed segments
        self.csum = np.empty(0)
        self._seg: int | None = None  # current segment start
        self._cur: tuple[int, np.ndarray, np.ndarray] | None = None  # its prefix (sec0_offset, filled, csum)

    def start_segment(self, sec_from: int) -> None:
        if self._cur is not None:
            off, filled, csum = self._cur
            if self.sec0 is None:
                self.sec0 = self._seg + off
            self.mids = np.r_[self.mids, filled]
            self.csum = np.r_[self.csum, csum]
            self._cur = None
        if self.sec0 is not None and self.sec0 + len(self.mids) != sec_from:
            raise RuntimeError("Binance grid is not contiguous")
        self._seg = int(sec_from)

    def set_bundles(self, bn, sec_done: int) -> None:
        if self._seg is None:
            raise RuntimeError("LiveGrid.set_bundles before start_segment")
        if sec_done <= self._seg:
            self._cur = None
            return
        recv = bn.recv
        n = int(np.searchsorted(recv, sec_done * 1000))  # bundles of the complete seconds
        sub = _Bn(recv[:n], bn.bid[:n], bn.ask[:n], bn.bid_qty[:n], bn.ask_qty[:n])
        first = self.sec0 is None
        self._cur = _grid_segment(self._seg, int(sec_done), sub, None if first else self.mids[-1], first,
                                  self.csum[-1] if len(self.csum) else 0.0)

    def _at(self, j: int):
        n = len(self.csum)
        return self.csum[j] if j < n else self._cur[2][j - n]

    def sig15(self, t_ms: int) -> float | None:
        """`_Grid.sig15`."""
        sec0, total = self.sec0, len(self.csum)
        if self._cur is not None:
            if sec0 is None:
                sec0 = self._seg + self._cur[0]
            total += len(self._cur[2])
        if sec0 is None:
            return None
        j = t_ms // 1000 - 1 - sec0
        if j < SIG_WINDOW_S or j >= total:
            return None
        return math.sqrt(max(self._at(j) - self._at(j - SIG_WINDOW_S), 0.0) / SIG_WINDOW_S)


# ---------------------------------------------------------------------------------------- market info

def _info_select() -> str:
    from .marketdata import _MARKETS_SQL

    head = _MARKETS_SQL.split("FROM markets")[0]
    if not head.strip().startswith("SELECT"):
        raise RuntimeError("marketdata._MARKETS_SQL changed shape")
    return head


def info_from_meta(meta: dict, recv_ms: int) -> MarketInfo:
    """The `MarketInfo` of one `pm_market_meta` row (`meta` = its `d` object, received at `recv_ms`): `views.markets_sql`
    over in-memory relations holding that row and no resolution, the columns `load_coin` selects, `marketdata._info`.
    So live and replay parse Gamma identically. Outcome and resolution are None."""
    import duckdb

    from ..views import markets_sql

    con = duckdb.connect()
    try:
        con.execute("SET TimeZone = 'UTC'")
        con.execute("CREATE TEMP TABLE _meta (recv_ms BIGINT, d JSON)")
        con.execute("CREATE TEMP TABLE _res (recv_ms BIGINT, d JSON)")
        con.execute("INSERT INTO _meta VALUES (?, ?)", [int(recv_ms), json.dumps(meta)])
        df = con.execute(f"{_info_select()} FROM ({markets_sql('_meta', '_res')})").df()
    finally:
        con.close()
    if len(df) != 1:
        raise ValueError(f"pm_market_meta row gives {len(df)} markets")
    r = next(df.itertuples())
    if r.slug is None or r.window_end_ms is None or (isinstance(r.window_end_ms, float) and np.isnan(r.window_end_ms)):
        raise ValueError(f"pm_market_meta row without slug or endDate: {r.slug!r}")
    return _info(r)


__all__ = ["GrowingArray", "GrowingMarketData", "LiveBn", "LiveCl", "LiveGrid", "info_from_meta"]

"""Session writer: a paper session in the recorder's raw layout, so `POLYDATA_DIR=<session> polydata build` works.

Layout under `root`:
- `raw/YYYY-MM-DD/HH/<stream>.<slotStartMs>.{tsv,jsonl}.zst`: 5-minute slots by `recv_ms`, a TSV header row only when a
  file is created, JSONL lines `{"recv_ms": r, "d": obj}`. Rows the router accepted only.
- `late/<stream>.tsv.zst`: rows the router dropped (late, future-stamped), with a `late_by_ms` column. `build` never
  reads this folder.
- `catalog/YYYY-MM-DD/rec_events.csv.zst`: the `rec_events` catalog (`ts,kind,source,detail`).
- `paper/<name>.jsonl.zst`: runner records (decisions, status).
- `manifest.json`: files with row counts and sha256.

Every file is a sequence of zstd frames: `flush()` appends one frame per touched file (compressed and written by a
writer thread, the file opened in append mode and closed again), so a crash loses at most `flush_s` of rows and a
file written in several flushes stays readable (Arrow and DuckDB read across frames).

Binance thinning (`bn_fut_bbo` only): per `(sym, recv_ms // bn_step_ms)` the first received row is written at once and
the bucket's last row by `(recv_ms, update_id)` when a later bucket of that symbol arrives (or at flush/close once no
more rows can land in the bucket). `engine._load_bn` takes `arg_max(.., (recv_ms, update_id))` per bucket of the
configured step, and `gaps.bn_silences` looks at gaps between rows, so both give the same answer over the thinned
store as long as the configured step is a multiple of `bn_step_ms`.
"""

from __future__ import annotations

import csv
import hashlib
import io
import json
import math
import time
from concurrent.futures import Future, ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import zstandard

from polydata import schema
from polydata.bt.live import LiveRow

SLOT_MS = 300_000


def _plain(x):
    """JSON.stringify number text: an integral float prints as an integer (1.0 -> 1); non-finite -> null."""
    if isinstance(x, float):
        if not math.isfinite(x):
            return None
        return int(x) if x.is_integer() and abs(x) < 1e21 else x
    if isinstance(x, dict):
        return {k: _plain(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_plain(v) for v in x]
    return x


def dumps(obj) -> str:
    """Compact JSON like the recorder's `JSON.stringify` (same separators, same number text)."""
    return json.dumps(_plain(obj), separators=(",", ":"), ensure_ascii=False)


def _fmt_ts(ts_ms: int) -> str:
    sec, ms = divmod(int(ts_ms), 1000)
    return datetime.fromtimestamp(sec, timezone.utc).strftime("%Y-%m-%d %H:%M:%S") + f".{ms:03d}000+00"


class _Bn:
    """Thinning state of one symbol: the current bucket, its best row by key, and whether that row still has to be
    written (it is not the first row of the bucket)."""
    __slots__ = ("bucket", "key", "row", "pending")

    def __init__(self, bucket: int, key: tuple, row: LiveRow):
        self.bucket, self.key, self.row, self.pending = bucket, key, row, False


class SessionWriter:
    def __init__(self, root: Path, *, bn_step_ms: int = 10, level: int = 3, flush_s: int = 10):
        self.root = Path(root)
        self.bn_step_ms = int(bn_step_ms)
        self.level = level
        self.flush_s = flush_s
        self.root.mkdir(parents=True, exist_ok=True)
        self._buf: dict[Path, list[str]] = {}
        self._rows: dict[Path, int] = {}  # every file touched, rows written by this writer (no header)
        self._dir_cache: dict[int, Path] = {}
        self._bn: dict[str, _Bn] = {}
        self._max_recv = -(2**62)
        self._last_flush = time.monotonic()
        self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="session-writer")
        self._futs: list[Future] = []
        self._closed = False

    # ------------------------------------------------------------------------------------------- files
    def _slot_dir(self, slot: int) -> Path:
        d = self._dir_cache.get(slot)
        if d is None:
            t = datetime.fromtimestamp(slot / 1000, timezone.utc)
            d = self.root / "raw" / t.strftime("%Y-%m-%d") / t.strftime("%H")
            if len(self._dir_cache) > 4096:
                self._dir_cache.clear()
            self._dir_cache[slot] = d
        return d

    def _put(self, path: Path, line: str, header: str | None) -> None:
        buf = self._buf.get(path)
        if buf is None:
            buf = self._buf[path] = []
            if path not in self._rows:
                self._rows[path] = 0
                if header is not None and not path.exists():
                    buf.append(header)
        buf.append(line)
        self._rows[path] += 1

    # ------------------------------------------------------------------------------------------- rows
    def write(self, row: LiveRow) -> None:
        if self._closed:
            raise ValueError("session writer is closed")
        recv = int(row.recv_ms)
        if recv > self._max_recv:
            self._max_recv = recv
        if row.stream == "bn_fut_bbo":
            self._bn_row(row)
        else:
            self._write_raw(row)

    def _write_raw(self, row: LiveRow) -> None:
        stream, recv = row.stream, int(row.recv_ms)
        slot = recv // SLOT_MS * SLOT_MS
        base = self._slot_dir(slot)
        if stream in schema.TSV:
            cols = schema.TSV[stream]
            v = row.values
            if len(v) != len(cols) - 1:
                raise ValueError(f"{stream}: {len(v)} values, expected {len(cols) - 1}")
            self._put(base / f"{stream}.{slot}.tsv.zst", f"{recv}\t" + "\t".join(v) + "\n",
                      "\t".join(cols) + "\n")
        elif stream in schema.JSONL:
            self._put(base / f"{stream}.{slot}.jsonl.zst", dumps({"recv_ms": recv, "d": row.values}) + "\n", None)
        else:
            raise ValueError(f"unknown stream {stream!r}")

    def _bn_row(self, row: LiveRow) -> None:
        sym, recv = row.values[0], int(row.recv_ms)
        b = recv // self.bn_step_ms
        key = (recv, int(row.values[1]))
        s = self._bn.get(sym)
        if s is None or s.bucket != b:
            if s is not None and s.pending:
                self._write_raw(s.row)
            self._bn[sym] = _Bn(b, key, row)
            self._write_raw(row)  # the first row of a bucket is written at once
            return
        if key > s.key:
            s.key, s.row, s.pending = key, row, True

    def _settle_bn(self, upto_bucket: int | None) -> None:
        """Write the held last row of every bucket below `upto_bucket` (all of them if None)."""
        for s in self._bn.values():
            if s.pending and (upto_bucket is None or s.bucket < upto_bucket):
                self._write_raw(s.row)
                s.pending = False

    def write_late(self, row: LiveRow, late_by_ms: int) -> None:
        if self._closed:
            raise ValueError("session writer is closed")
        path = self.root / "late" / f"{row.stream}.tsv.zst"
        if row.stream in schema.TSV:
            cols = list(schema.TSV[row.stream])
            line = f"{int(row.recv_ms)}\t" + "\t".join(row.values) + f"\t{int(late_by_ms)}\n"
        else:
            cols = ["recv_ms", "d"]
            line = f"{int(row.recv_ms)}\t{dumps(row.values)}\t{int(late_by_ms)}\n"
        self._put(path, line, "\t".join(cols + ["late_by_ms"]) + "\n")

    def event(self, ts_ms: int, kind: str, source: str, detail: str) -> None:
        """One `rec_events` row (the recorder's columns) in the catalog of the UTC day of `ts_ms`."""
        if self._closed:
            raise ValueError("session writer is closed")
        day = datetime.fromtimestamp(int(ts_ms) // 1000, timezone.utc).strftime("%Y-%m-%d")
        out = io.StringIO()
        csv.writer(out, lineterminator="\n").writerow([_fmt_ts(ts_ms), kind, source, detail])
        self._put(self.root / "catalog" / day / "rec_events.csv.zst", out.getvalue(), "ts,kind,source,detail\n")

    def write_paper(self, name: str, obj: dict) -> None:
        if self._closed:
            raise ValueError("session writer is closed")
        self._put(self.root / "paper" / f"{name}.jsonl.zst", dumps(obj) + "\n", None)

    # ------------------------------------------------------------------------------------------- flushing
    def maybe_flush(self) -> None:
        """Flush when `flush_s` of wall time passed since the last flush."""
        if time.monotonic() - self._last_flush >= self.flush_s:
            self.flush()

    def flush(self) -> None:
        """Append one zstd frame per touched file. Compression and I/O run in the writer thread."""
        self._last_flush = time.monotonic()
        self._settle_bn(self._max_recv // self.bn_step_ms)  # buckets that can no longer receive rows
        buf, self._buf = self._buf, {}
        if buf:
            self._futs.append(self._pool.submit(self._write_frames, buf))
        self._raise_done()

    def _write_frames(self, buf: dict[Path, list[str]]) -> None:
        comp = zstandard.ZstdCompressor(level=self.level)
        for path, lines in buf.items():
            frame = comp.compress("".join(lines).encode("utf-8", "replace"))
            path.parent.mkdir(parents=True, exist_ok=True)
            with open(path, "ab") as f:
                f.write(frame)

    def _raise_done(self, wait: bool = False) -> None:
        keep = []
        for f in self._futs:
            if wait or f.done():
                f.result()
            else:
                keep.append(f)
        self._futs = keep

    def close(self) -> None:
        if self._closed:
            return
        self._settle_bn(None)
        self.flush()
        self._closed = True
        self._pool.shutdown(wait=True)
        self._raise_done(wait=True)

    def manifest(self, **fields) -> None:
        """`manifest.json`: `fields` plus `files [{path, rows, sha256}]` of every file this writer touched."""
        if not self._closed:
            self.flush()
            self._raise_done(wait=True)  # frames are on disk
            self._pool.shutdown(wait=True)
            self._pool = ThreadPoolExecutor(max_workers=1, thread_name_prefix="session-writer")
        files = []
        for path in sorted(self._rows):
            h = hashlib.sha256()
            if path.exists():
                with open(path, "rb") as f:
                    for chunk in iter(lambda: f.read(1 << 20), b""):
                        h.update(chunk)
            files.append({"path": path.relative_to(self.root).as_posix(), "rows": self._rows[path],
                          "sha256": h.hexdigest()})
        (self.root / "manifest.json").write_text(json.dumps({**fields, "files": files}, indent=1, default=str))

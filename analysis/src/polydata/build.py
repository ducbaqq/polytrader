"""Convert the raw mirror (zstd TSV/JSONL) into typed Parquet:

    data/parquet/<stream>/date=YYYY-MM-DD/HH.parquet     one file per stream per hour
    data/parquet/markets.parquet                          one row per Up/Down market

Incremental: each Parquet file has a sidecar `HH.src` listing the raw files (name + size) it was
built from; an hour is rebuilt only when that list changes (e.g. more files were synced).
"""

from __future__ import annotations

import fnmatch
import time
from pathlib import Path

import duckdb

from . import schema
from .config import paths
from .views import MARKETS_SQL


def _sources(files: list[Path]) -> str:
    return "\n".join(f"{f.name}:{f.stat().st_size}" for f in files)


def _sql_list(files: list[Path]) -> str:
    return "[" + ", ".join(f"'{f.as_posix()}'" for f in files) + "]"


def _convert(con: duckdb.DuckDBPyConnection, stream: str, files: list[Path], out: Path) -> int:
    src = _sql_list(files)
    if stream in schema.TSV:
        opts = "delim='\t', header=true, quote='', escape='', union_by_name=true"
        # Columns can be added over time (rtds_price gained `conn` on 2026-11-24): type the columns
        # the files have, fill the others with NULL, so every hour has the same schema.
        present = {r[0] for r in con.execute(f"DESCRIBE SELECT * FROM read_csv({src}, {opts}, all_varchar=true)").fetchall()}
        types = schema.TSV[stream]
        unknown = present - set(types)
        if unknown:
            raise duckdb.Error(f"columns not in schema.py: {sorted(unknown)}")
        cols = "{" + ", ".join(f"'{k}': '{v}'" for k, v in types.items() if k in present) + "}"
        select = ", ".join(k if k in present else f"NULL::{v} AS {k}" for k, v in types.items())
        rel = f"SELECT {select} FROM read_csv({src}, {opts}, types={cols}) ORDER BY {schema.SORT[stream]}, recv_ms"
    else:
        con.execute(
            f"CREATE OR REPLACE TEMP VIEW src AS SELECT * FROM read_json({src}, format='newline_delimited', "
            f"columns={{'recv_ms': 'BIGINT', 'd': 'JSON'}})"
        )
        rel = schema.JSONL_SELECT.get(stream, "SELECT recv_ms, d FROM src ORDER BY recv_ms")
    tmp = out.with_suffix(".tmp")
    con.execute(f"COPY ({rel}) TO '{tmp.as_posix()}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 250000)")
    tmp.replace(out)
    return con.execute(f"SELECT count(*) FROM '{out.as_posix()}'").fetchone()[0]


def build(days: list[str] | None = None, streams: list[str] | None = None, force: bool = False) -> None:
    p = paths()
    all_streams = list(schema.TSV) + schema.JSONL
    con = duckdb.connect()
    built = skipped = failed = 0
    start = time.time()

    for day_dir in sorted(p.raw.glob("????-??-??")):
        if days and not any(fnmatch.fnmatch(day_dir.name, d) for d in days):
            continue
        for hour_dir in sorted(day_dir.glob("??")):
            for stream in all_streams:
                if streams and not any(fnmatch.fnmatch(stream, s) for s in streams):
                    continue
                files = sorted(hour_dir.glob(f"{stream}.*.zst"))
                if not files:
                    continue
                out_dir = p.parquet / stream / f"date={day_dir.name}"
                out = out_dir / f"{hour_dir.name}.parquet"
                manifest = out.with_suffix(".src")
                srcs = _sources(files)
                if not force and out.exists() and manifest.exists() and manifest.read_text() == srcs:
                    skipped += 1
                    continue
                out_dir.mkdir(parents=True, exist_ok=True)
                t = time.time()
                try:
                    rows = _convert(con, stream, files, out)
                except duckdb.Error as e:
                    failed += 1
                    print(f"  FAILED {day_dir.name}/{hour_dir.name} {stream}: {str(e).splitlines()[0][:300]}")
                    continue
                manifest.write_text(srcs)
                built += 1
                print(f"  {day_dir.name}/{hour_dir.name} {stream:<20} {len(files):>3} files {rows:>11,} rows  {time.time() - t:5.1f}s")

    if built or force or not (p.parquet / "markets.parquet").exists():
        build_markets(con)
    print(f"built {built}, up to date {skipped}, failed {failed} in {time.time() - start:.0f}s")


def build_markets(con: duckdb.DuckDBPyConnection | None = None) -> None:
    """One row per market from discovery metadata + resolution (small; rebuilt every time)."""
    p = paths()
    if not list((p.parquet / "pm_market_meta").glob("*/*.parquet")):
        return
    con = con or duckdb.connect()
    out = p.parquet / "markets.parquet"
    tmp = out.with_suffix(".tmp")
    con.execute(f"COPY ({MARKETS_SQL.format(P=p.parquet.as_posix())}) TO '{tmp.as_posix()}' (FORMAT parquet)")
    tmp.replace(out)
    n = con.execute(f"SELECT count(*) FROM '{out.as_posix()}'").fetchone()[0]
    print(f"  markets.parquet {n:,} markets")

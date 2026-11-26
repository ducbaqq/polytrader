"""Convert the raw mirror (zstd TSV/JSONL) into typed Parquet:

    data/parquet/<stream>/date=YYYY-MM-DD/HH.parquet     one file per stream per hour
    data/parquet/markets.parquet                          one row per Up/Down market

Incremental: each Parquet file has a sidecar `HH.src` listing the raw files (name + size) it was
built from, plus the build format; an hour is rebuilt only when that changes (e.g. more files were
synced, or `FORMAT` was bumped).

TSV streams get a derived `seq` column: the raw arrival order within the stream-hour (5-minute
files in name order, then row order within each file). Files are sorted by (instrument, recv_ms,
seq), so rows that share a recv_ms keep their recorded order.
"""

from __future__ import annotations

import fnmatch
import time
from pathlib import Path

import duckdb
import numpy as np
import pyarrow as pa

from . import schema
from .config import paths
from .views import MARKETS_SQL, markets_sql


def _has_rows(f: Path) -> bool:
    """False for files without data rows (empty zstd frames left by restarts during development)."""
    if f.stat().st_size > 1024:
        return True
    import zstandard

    with zstandard.ZstdDecompressor().stream_reader(f.open("rb")) as r:
        lines = r.read().splitlines()
    return len(lines) > (1 if ".tsv." in f.name else 0)


# Bump when the Parquet layout changes, so `build` rebuilds existing hours.
# 2: TSV streams gain `seq` and are sorted by (instrument, recv_ms, seq).
FORMAT = 2


def _sources(files: list[Path]) -> str:
    return f"# format {FORMAT}\n" + "\n".join(f"{f.name}:{f.stat().st_size}" for f in files)


def _read_tsv(path: Path, columns) -> pa.Table:
    """One raw TSV file as an Arrow table of strings, in file order (empty field -> NULL, like
    DuckDB's read_csv). Arrow's CSV reader keeps row order, which a row number over DuckDB's
    parallel read_csv does not guarantee."""
    import pyarrow.csv as pacsv

    return pacsv.read_csv(
        pa.input_stream(path.as_posix(), compression="zstd"),
        read_options=pacsv.ReadOptions(block_size=64 << 20),
        parse_options=pacsv.ParseOptions(delimiter="\t", quote_char=False, double_quote=False,
                                         escape_char=False, newlines_in_values=False),
        convert_options=pacsv.ConvertOptions(column_types={c: pa.string() for c in columns},
                                             strings_can_be_null=True, null_values=[""]),
    )


def _sql_list(files: list[Path]) -> str:
    return "[" + ", ".join(f"'{f.as_posix()}'" for f in files) + "]"


def _convert(con: duckdb.DuckDBPyConnection, stream: str, files: list[Path], out: Path) -> int:
    src = _sql_list(files)
    if stream in schema.TSV:
        # Columns can be added over time (rtds_price gained `conn` on 2026-11-24): type the columns
        # each file has, fill the others with NULL, so every hour has the same schema.
        types = schema.TSV[stream]
        con.execute("DROP TABLE IF EXISTS _tsv")
        con.execute("CREATE TEMP TABLE _tsv (" + ", ".join(f"{k} {v}" for k, v in types.items()) + f", {schema.SEQ_COLUMN} BIGINT)")
        offset = 0
        for f in files:
            tbl = _read_tsv(f, types)
            unknown = set(tbl.column_names) - set(types)
            if unknown:
                raise duckdb.Error(f"columns not in schema.py: {sorted(unknown)}")
            tbl = tbl.append_column(schema.SEQ_COLUMN, pa.array(np.arange(offset, offset + tbl.num_rows, dtype=np.int64)))
            offset += tbl.num_rows
            select = ", ".join(f"CAST({k} AS {v})" if k in tbl.column_names else f"NULL::{v}" for k, v in types.items())
            con.register("_arrow", tbl)
            con.execute(f"INSERT INTO _tsv SELECT {select}, {schema.SEQ_COLUMN} FROM _arrow")
            con.unregister("_arrow")
        rel = f"SELECT * FROM _tsv ORDER BY {schema.SORT[stream]}, recv_ms, {schema.SEQ_COLUMN}"
    else:
        con.execute(
            f"CREATE OR REPLACE TEMP VIEW src AS SELECT * FROM read_json({src}, format='newline_delimited', "
            f"columns={{'recv_ms': 'BIGINT', 'd': 'JSON'}})"
        )
        rel = schema.JSONL_SELECT.get(stream, "SELECT recv_ms, d FROM src ORDER BY recv_ms")
    tmp = out.with_suffix(".tmp")
    con.execute(f"COPY ({rel}) TO '{tmp.as_posix()}' (FORMAT parquet, COMPRESSION zstd, ROW_GROUP_SIZE 250000)")
    tmp.replace(out)
    con.execute("DROP TABLE IF EXISTS _tsv")
    return con.execute(f"SELECT count(*) FROM '{out.as_posix()}'").fetchall()[0][0]


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
                files = [f for f in sorted(hour_dir.glob(f"{stream}.*.zst")) if _has_rows(f)]
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
    sql = MARKETS_SQL.format(P=p.parquet.as_posix())
    if not list((p.parquet / "pm_resolution").glob("*/*.parquet")):
        # no market resolved yet (a short paper session): an empty resolution relation instead of a failing glob
        sql = markets_sql(f"read_parquet('{p.parquet.as_posix()}/pm_market_meta/*/*.parquet')",
                          "(SELECT NULL::BIGINT AS recv_ms, NULL::JSON AS d WHERE false)")
    con.execute(f"COPY ({sql}) TO '{tmp.as_posix()}' (FORMAT parquet)")
    tmp.replace(out)
    n = con.execute(f"SELECT count(*) FROM '{out.as_posix()}'").fetchall()[0][0]
    print(f"  markets.parquet {n:,} markets")

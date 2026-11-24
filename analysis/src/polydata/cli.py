"""`polydata` command line. Run `uv run polydata --help` from analysis/."""

from __future__ import annotations

import argparse
import sys
import time
from collections import defaultdict

from .config import REPO_ROOT, paths


def _write_db() -> None:
    """Persist the views in data/polytrader.duckdb so the DuckDB CLI / any SQL tool can open it."""
    import duckdb

    from .views import create_views

    p = paths()
    p.root.mkdir(parents=True, exist_ok=True)
    try:
        con = duckdb.connect(str(p.db))
    except duckdb.IOException as e:  # open in another process (e.g. the UI)
        print(f"  could not update {p.db} ({str(e).splitlines()[0]}); views there may be stale")
        return
    names = create_views(con)
    con.close()
    print(f"  {p.db}: {len(names)} views")


def cmd_sync(a) -> None:
    from .sync import sync

    sync(days=a.days, streams=a.streams, include_catalog=not a.no_catalog, dry_run=a.dry_run)


def cmd_build(a) -> None:
    from .build import build

    build(days=a.days, streams=a.streams, force=a.force)
    _write_db()


def cmd_update(a) -> None:
    cmd_sync(a)
    if not a.dry_run:
        cmd_build(a)


def cmd_status(a) -> None:
    p = paths()
    print(f"data dir: {p.root}")
    raw = defaultdict(lambda: [0, 0])
    for f in p.raw.glob("*/*/*.zst"):
        d = f.parts[-3]
        raw[d][0] += 1
        raw[d][1] += f.stat().st_size
    pq = defaultdict(lambda: [set(), 0])
    for f in p.parquet.glob("*/date=*/*.parquet"):
        d = f.parent.name[5:]
        pq[d][0].add(f.stem)
        pq[d][1] += f.stat().st_size
    remote: dict[str, list[int]] = {}
    if a.remote:
        from .sync import list_remote

        for f in list_remote("raw/"):
            remote.setdefault(f.day, [0, 0])
            remote[f.day][0] += 1
            remote[f.day][1] += f.size
    days = sorted(set(raw) | set(pq) | set(remote))
    hdr = f"{'day':<12}{'raw files':>10}{'raw GB':>9}{'parquet h':>11}{'parquet GB':>12}"
    print(hdr + (f"{'bucket files':>14}{'bucket GB':>11}" if a.remote else ""))
    for d in days:
        line = f"{d:<12}{raw[d][0]:>10}{raw[d][1] / 1e9:>9.2f}{len(pq[d][0]):>11}{pq[d][1] / 1e9:>12.2f}"
        if a.remote:
            r = remote.get(d, [0, 0])
            line += f"{r[0]:>14}{r[1] / 1e9:>11.2f}"
        print(line)
    if not days:
        print("no data yet: run `uv run polydata update`")


def cmd_sql(a) -> None:
    from .views import connect

    con = connect()
    t = time.time()
    import duckdb

    try:
        rel = con.sql(a.query)
    except duckdb.Error as e:
        sys.exit(str(e))
    if rel is not None:
        rel = rel.limit(a.rows)
        cols, rows = rel.columns, rel.fetchall()
        import pandas as pd

        with pd.option_context("display.max_columns", None, "display.width", 250, "display.max_colwidth", 80):
            print(pd.DataFrame(rows, columns=cols).to_string(index=False))
    print(f"({time.time() - t:.2f}s)", file=sys.stderr)


def cmd_ui(a) -> None:
    from .views import connect

    con = connect()
    con.execute("CALL start_ui()")
    print("DuckDB UI running at http://localhost:4213 with all views loaded. Ctrl-C to stop.")
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass


def cmd_db(a) -> None:
    _write_db()


def cmd_notebook(a) -> None:
    import subprocess

    subprocess.run([sys.executable, "-m", "jupyter", "lab", str(REPO_ROOT / "analysis" / "notebooks")])


def main(argv: list[str] | None = None) -> None:
    ap = argparse.ArgumentParser(prog="polydata", description="Polytrader recorder data: sync from Spaces, build Parquet, query with DuckDB.")
    sub = ap.add_subparsers(dest="cmd", required=True)

    def selectors(sp):
        sp.add_argument("--days", nargs="+", metavar="DAY", help="day globs, e.g. 2026-11-24 '2026-11-2*' (default: all)")
        sp.add_argument("--streams", nargs="+", metavar="STREAM", help="stream globs, e.g. 'pm_*' bn_fut_bbo (default: all)")

    sp = sub.add_parser("update", help="sync + build (the usual command)")
    selectors(sp)
    sp.add_argument("--no-catalog", action="store_true")
    sp.add_argument("--dry-run", action="store_true")
    sp.add_argument("--force", action="store_true")
    sp.set_defaults(fn=cmd_update)

    sp = sub.add_parser("sync", help="download raw files from the Spaces bucket")
    selectors(sp)
    sp.add_argument("--no-catalog", action="store_true", help="skip the daily Postgres catalog exports")
    sp.add_argument("--dry-run", action="store_true", help="only show what would be downloaded")
    sp.set_defaults(fn=cmd_sync)

    sp = sub.add_parser("build", help="convert raw files to Parquet (incremental)")
    selectors(sp)
    sp.add_argument("--force", action="store_true", help="rebuild even if up to date")
    sp.set_defaults(fn=cmd_build)

    sp = sub.add_parser("status", help="local (and with --remote, bucket) coverage per day")
    sp.add_argument("--remote", action="store_true")
    sp.set_defaults(fn=cmd_status)

    sp = sub.add_parser("sql", help="run one query against the views")
    sp.add_argument("query")
    sp.add_argument("--rows", type=int, default=100, help="max rows to print")
    sp.set_defaults(fn=cmd_sql)

    sub.add_parser("ui", help="open the DuckDB web UI with all views").set_defaults(fn=cmd_ui)
    sub.add_parser("db", help=f"(re)write views into data/polytrader.duckdb").set_defaults(fn=cmd_db)
    sub.add_parser("notebook", help="start Jupyter Lab in analysis/notebooks").set_defaults(fn=cmd_notebook)

    a = ap.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    main()

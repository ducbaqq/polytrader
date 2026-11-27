"""Out-of-process `polydata.build.build()` for a paper session (`voids.build_session_store` runs this module).

`python -m polydata.paper.buildproc <session> [memory_limit] [threads]` sets `POLYDATA_DIR=<session>`, makes every DuckDB
connection `build` opens use `SET memory_limit/threads` (DuckDB then spills instead of growing; `build.py` itself is
not touched, it is part of `sim_version`), and runs the normal build. Keeping it in its own process keeps the paper
runner's RSS flat: the in-process build at the end of a 6 h session peaked near 7 GB.
"""

from __future__ import annotations

import os
import sys


def main(argv: list[str]) -> int:
    session, memory_limit, threads = argv[0], argv[1] if len(argv) > 1 else "1.5GB", int(argv[2]) if len(argv) > 2 else 2
    os.environ["POLYDATA_DIR"] = session
    import duckdb

    real_connect = duckdb.connect

    def connect(*a, **k):
        con = real_connect(*a, **k)
        con.execute(f"SET memory_limit='{memory_limit}'")
        con.execute(f"SET threads={threads}")
        return con

    duckdb.connect = connect
    import gc

    import pyarrow as pa

    from polydata import build as build_mod

    real_convert = build_mod._convert

    def convert(*a, **k):
        try:
            return real_convert(*a, **k)
        finally:  # hand each (hour, stream) conversion's Arrow buffers back before the next one
            gc.collect()
            pa.default_memory_pool().release_unused()

    build_mod._convert = convert
    build_mod.build()
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

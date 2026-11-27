"""Visible size at the signal limit for the screen's fills: the token's last `pm_book` snapshot received before the
fill time (a snapshot, not a reconstruction with price changes, so it is approximate).

usage: POLYDATA_DIR=... uv run --project analysis python research/thin_markets/sizes.py OUT_DIR
"""

from __future__ import annotations

import sys
from pathlib import Path

import pandas as pd

from polydata.views import connect


def main(out: Path) -> None:
    con = connect()
    for name in ("fills_choose", "fills_report"):
        f = pd.read_parquet(out / f"{name}.parquet")[["t_slug", "pair", "side", "limit", "x_ms", "px"]]
        if f.empty:
            continue
        con.register("f", f)
        r = con.execute("""
            WITH b AS (SELECT slug, outcome, recv_ms, asks FROM pm_book
                       WHERE slug IN (SELECT t_slug FROM f) AND recv_ms <= (SELECT max(x_ms) FROM f)
                         AND recv_ms >= (SELECT min(x_ms) FROM f) - 3600000)
            SELECT f.*, b.recv_ms AS book_ms, b.asks FROM f ASOF LEFT JOIN b
              ON f.t_slug = b.slug AND f.side = b.outcome AND f.x_ms >= b.recv_ms
        """).df()

        def size_at(row) -> float:
            if not isinstance(row.asks, str) or not row.asks:
                return float("nan")
            tot = 0.0
            for lvl in row.asks.split("|"):
                p, s = lvl.split(":")
                if float(p) <= row.limit + 1e-9:
                    tot += float(s)
            return tot

        r["size"] = r.apply(size_at, axis=1)
        r["age_s"] = (r.x_ms - r.book_ms) / 1000
        print(name, "fills", len(r), "| visible shares at limit: median", r["size"].median(), "p25",
              r["size"].quantile(0.25), "sum", round(r["size"].sum()), "| book age s median", r.age_s.median())
        print(r.groupby("pair")["size"].agg(["count", "median", "sum"]).round(0).to_string())


if __name__ == "__main__":
    main(Path(sys.argv[1]))

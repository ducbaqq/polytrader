"""Visible size behind the executable xtf-arb episodes (run after screen_events.py).

usage: POLYDATA_DIR=... uv run --project analysis python research/xtf_arb/sizes.py OUT_DIR [LATENCY]

For every episode still below cost 1 at +LATENCY (default 200 ms), the simulator's book reconstruction
(`marketdata.load_coin`, read only) gives both books at recv_ms + LATENCY. Walking Up_A's asks and
Up_B's bids together, a pair share is added while ask_A + (1 - bid_B) + both fees < 1. Prints the
executable pairs and the locked profit per episode (first episode per market pair and the largest).
"""

from __future__ import annotations

import sys
from pathlib import Path

import numpy as np
import pandas as pd

from polydata.bt.marketdata import load_coin
from polydata.views import connect

PX = 1000.0


def walk(asks_a, bids_b, rate_a, rate_b):
    """(pairs, locked $) taking levels while the marginal pair cost after fees is < 1."""
    asks_a = [[p / PX, s] for p, s in asks_a]
    bids_b = [[p / PX, s] for p, s in bids_b]
    i = j = 0
    n = locked = 0.0
    while i < len(asks_a) and j < len(bids_b):
        ua, db = asks_a[i][0], 1 - bids_b[j][0]
        c = ua + db + rate_a * ua * (1 - ua) + rate_b * db * (1 - db)
        if c >= 1:
            break
        q = min(asks_a[i][1], bids_b[j][1])
        n += q
        locked += q * (1 - c)
        asks_a[i][1] -= q
        bids_b[j][1] -= q
        if asks_a[i][1] <= 1e-9:
            i += 1
        if bids_b[j][1] <= 1e-9:
            j += 1
    return n, locked


def main(out: Path, lat: int = 200) -> None:
    pairs = pd.read_parquet(out / "pairs.parquet")
    e = pd.read_parquet(out / f"episodes_{lat}.parquet")
    e = e[e.ok].merge(pairs, on="pid", suffixes=("", "_p"))
    con = connect()
    rows = []
    for coin, g in e.groupby("coin"):
        slugs = set(g.slug_a) | set(g.slug_b)
        md = load_coin(con, coin, ["5m", "15m", "1h", "4h"], int(g.t.min()) - 1, int(g.t.max()) + 1000, slugs=slugs)
        for r in g.itertuples():
            _, asks_a = md[r.slug_a].book_at(int(r.t) + lat, "recv", 10)
            bids_b, _ = md[r.slug_b].book_at(int(r.t) + lat, "recv", 10)
            n, lk = walk(asks_a, bids_b, r.rate_a, r.rate_b)
            top_a = asks_a[0] if asks_a else (None, 0)
            top_b = bids_b[0] if bids_b else (None, 0)
            rows.append(dict(pid=r.pid, coin=coin, tf_a=r.tf_a, tf_b=r.tf_b, half=r.half, t=r.t,
                             tau_s=(r.end_ms - r.t) / 1000, cost=getattr(r, f"cost_{lat}"),
                             ask_a=top_a[0], ask_a_sz=top_a[1], bid_b=top_b[0], bid_b_sz=top_b[1],
                             pairs=n, locked_usd=lk))
    con.close()
    d = pd.DataFrame(rows)
    d.to_parquet(out / f"sizes_{lat}.parquet")
    pd.set_option("display.width", 250)
    print(f"episodes executable at +{lat} ms: {len(d)}; with book size > 0: {(d.pairs > 0).sum()}")
    print("pairs per episode quantiles:", d.pairs.quantile([0.1, 0.5, 0.9, 1]).round(1).to_dict())
    first = d.sort_values("t").drop_duplicates("pid")
    big = d.sort_values("locked_usd").drop_duplicates("pid", keep="last")
    for name, x in (("first episode per pair", first), ("largest episode per pair", big)):
        print(f"\n{name}:")
        print(x.groupby("half").agg(pairs_traded=("pid", "size"), shares=("pairs", "sum"), med_shares=("pairs", "median"),
                                    locked_usd=("locked_usd", "sum")).round(2).to_string())
    print("\nper market pair (largest episode):")
    print(big.sort_values(["half", "pid"]).round(3).to_string(index=False))


if __name__ == "__main__":
    main(Path(sys.argv[1]), int(sys.argv[2]) if len(sys.argv) > 2 else 200)

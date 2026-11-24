"""Simulator validation suite (spec: "Simulator validation"). `--final` runs are refused until it has
passed for the current `sim_version()`.

Every reference here is computed **independently** of the simulator: DuckDB SQL over the raw
`pm_price_change` / `pm_book` / `pm_trade` / `markets` views, never `MarketData` or `Exchange`. The
only inputs taken from a run are what the strategy did (when it placed, cancelled) and the results
being checked (P&L, fills).

Reference book (`_size_at`, `_best`): the combined book in Up terms from Up `pm_price_change` rows
and Up `pm_book` snapshots, plus Down rows / snapshots mirrored into Up terms where no Up row / snapshot
shares their ts (each level change is published for both tokens; see `_stage`). The state at exchange time t is the last snapshot with ts <= t (the latest
received when several share a ts), then for each level the last change with snapshot ts < ts <= t
(same-ts changes: last by recv_ms, seq; a snapshot reflects the changes of its own ts).

Checks:
1. **book_reconstruction**: `marketdata.reconstruction_rate` >= 0.995 for every non-voided market.
2. **noop** P&L exactly 0; **buy_and_hold_up** and **always_cross** P&L per market equal the
   reference to the cent (|diff| < 0.005). The reference open time is the recv_ms of the market's first
   in-range snapshot (`ref_open_ms`), and the strategy must have placed exactly then. Markets whose
   reference top level holds fewer than `min_order_size` shares are skipped (the taker would walk several
   levels); anything else that prevents a comparison (no sell, wrong open time) fails.
3. **fill_support**: every `join-best` maker fill re-derived from the raw trades with the fill model's
   rules (join behind the recorded level size at arrival; only trades with ts > arrival + 150 ms;
   at our price the trade eats the queue ahead first; a trade-through fills up to the trade's size;
   nothing at or after the window end or after our cancel; cancel model "behind"). `join-best`
   keeps at most one order per side per market at a time, so each order is re-derived on its own
   (checked: `one_order_per_side`). Compared per (order, trade ts): filled size, fill price (the order's
   own limit in its token's terms) and the queue still ahead at the fill; per order, the queue joined
   behind (the accepted row's queue_ahead) and the arrival time. **maker_edge**: `join-best` earns less per share than the average
   maker on the same markets (every `pm_trade` marked to the outcome, as in the calibration study).
4. **determinism**: `join-best` run twice gives byte-identical summaries (canonical JSON of the
   per-market P&L plus SHA-256 of the sorted CSV of the orders and fills frames).
"""

from __future__ import annotations

import hashlib
import json
import math
import time
from collections.abc import Callable, Iterable
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd

from ..config import paths
from ..fees import taker_fee

BT_DIR = Path(__file__).resolve().parent
_VERSION_EXCLUDE = {"report.py", "runs.py", "cli.py"}
CENT = 0.005  # "to the cent": |sim - ref| below half a cent
SIZE_EPS = 1e-6
TRADE_ECHO_MS = 150  # spec "Trade echo"; restated here, not imported from the exchange
RECON_MIN = 0.995
STRATEGIES = ("noop", "buy-and-hold-up", "always-cross", "join-best")


# ---------------------------------------------------------------------------------------- version

def sim_version() -> str:
    """sha256 over every `polydata/bt/*.py` (not `strategies/`, report.py, runs.py, cli.py) and
    `polydata/fees.py`, 12 hex."""
    h = hashlib.sha256()
    files = [f for f in sorted(BT_DIR.glob("*.py")) if f.name not in _VERSION_EXCLUDE] + [BT_DIR.parent / "fees.py"]
    for f in files:
        h.update(f.relative_to(BT_DIR.parent).as_posix().encode() + b"\0" + f.read_bytes() + b"\0")
    return h.hexdigest()[:12]


def validation_path(version: str) -> Path:
    return paths().root / "runs" / "validation" / f"{version}.json"


def is_validated(version: str | None = None) -> bool:
    p = validation_path(version or sim_version())
    try:
        return json.loads(p.read_text()).get("passed") is True
    except (OSError, ValueError):
        return False


# ---------------------------------------------------------------------------------------- staging

def _day(ms: int):
    return datetime.fromtimestamp(ms / 1000, timezone.utc).date()


def _stage(con, slugs: Iterable[str]) -> None:
    """Temp tables with the raw rows of `slugs`: `_v_chg` (Up level changes, one row per level and ts),
    `_v_snap` / `_v_slv` (Up snapshots and their levels, one per ts), `_v_tr` (trades in Up terms),
    `_v_mk` (markets)."""
    slugs = sorted(set(slugs))
    con.execute("CREATE OR REPLACE TEMP TABLE _v_mk AS SELECT * FROM markets WHERE list_contains(?, slug)", [slugs])
    lo, hi = con.execute("SELECT min(window_start_ms), max(window_end_ms) FROM _v_mk").fetchall()[0]
    if lo is None:
        lo = hi = 0
    d0, d1 = _day(int(lo) - 3 * 3_600_000), _day(int(hi) + 3_600_000)  # markets are listed ~2 windows early
    flt = "list_contains(?, slug) AND date BETWEEN ? AND ?"
    args = [slugs, d0, d1]
    # Level changes in Up terms: Up rows, plus Down rows (mirrored) that have no Up mirror in the same ts.
    con.execute(f"""CREATE OR REPLACE TEMP TABLE _v_pc AS
        SELECT slug, outcome, side, round(price * 1000)::INTEGER AS px, size, ts_ms, recv_ms, seq
        FROM pm_price_change WHERE {flt}""", args)
    con.execute("""CREATE OR REPLACE TEMP TABLE _v_chg AS
        WITH u AS (SELECT slug, side, px, ts_ms, recv_ms, seq, size FROM _v_pc WHERE outcome = 'Up'),
        d AS (SELECT slug, CASE side WHEN 'BUY' THEN 'SELL' ELSE 'BUY' END AS side, 1000 - px AS px, ts_ms, recv_ms,
                     seq, size
              FROM _v_pc p WHERE outcome = 'Down' AND NOT EXISTS (
                  SELECT 1 FROM _v_pc m WHERE m.outcome = 'Up' AND m.slug = p.slug AND m.ts_ms = p.ts_ms
                     AND m.px = 1000 - p.px AND m.side <> p.side))
        SELECT slug, side, px, ts_ms, arg_max(size, (recv_ms, seq)) AS size
        FROM (SELECT * FROM u UNION ALL SELECT * FROM d) GROUP BY ALL""")
    con.execute("DROP TABLE _v_pc")
    # Snapshots: Up, plus Down ones (mirrored) at a ts without an Up snapshot. At subscription the two
    # can be 1 ms apart and disagree; the later one is the book from then on (verified on 2026-11-24).
    con.execute(f"""CREATE OR REPLACE TEMP TABLE _v_snap AS
        WITH b AS (SELECT slug, outcome, ts_ms, arg_max(bids, (recv_ms, seq)) AS bids,
                          arg_max(asks, (recv_ms, seq)) AS asks, max(recv_ms) AS recv_ms
                   FROM pm_book WHERE {flt} GROUP BY ALL)
        SELECT * FROM b WHERE outcome = 'Up'
        UNION ALL
        SELECT * FROM b d WHERE outcome = 'Down'
          AND NOT EXISTS (SELECT 1 FROM b u WHERE u.outcome = 'Up' AND u.slug = d.slug AND u.ts_ms = d.ts_ms)""", args)
    con.execute("""CREATE OR REPLACE TEMP TABLE _v_slv AS
        WITH l AS (SELECT slug, ts_ms, outcome, 'BUY' AS side, unnest(levels(bids)) AS l FROM _v_snap
                   UNION ALL SELECT slug, ts_ms, outcome, 'SELL', unnest(levels(asks)) FROM _v_snap)
        SELECT slug, ts_ms,
               CASE WHEN outcome = 'Up' THEN side WHEN side = 'BUY' THEN 'SELL' ELSE 'BUY' END AS side,
               (CASE WHEN outcome = 'Up' THEN round(l.price * 1000) ELSE 1000 - round(l.price * 1000) END)::INTEGER AS px,
               max(l.size) AS size
        FROM l GROUP BY ALL""")
    con.execute(f"""CREATE OR REPLACE TEMP TABLE _v_tr AS
        SELECT slug, ts_ms, recv_ms, seq, size,
               CASE WHEN (outcome = 'Down') <> (side = 'SELL') THEN 'SELL' ELSE 'BUY' END AS taker_up_side,
               (CASE WHEN outcome = 'Up' THEN round(price * 1000) ELSE 1000 - round(price * 1000) END)::INTEGER AS up_px,
               price, side, outcome
        FROM pm_trade WHERE {flt}""", args)


def _size_at(con, q: pd.DataFrame) -> pd.Series:
    """q[qid, slug, side, px, t] -> recorded level size (Up terms) at exchange time t, by qid."""
    if q.empty:
        return pd.Series(dtype=float)
    con.register("_q_df", q[["qid", "slug", "side", "px", "t"]].astype({"qid": "int64", "px": "int64", "t": "int64"}))
    try:
        d = con.execute("""
            WITH s AS (SELECT q.*, s.ts_ms AS snap_ts FROM _q_df q ASOF LEFT JOIN _v_snap s
                       ON q.slug = s.slug AND q.t >= s.ts_ms),
            c AS (SELECT s.*, c.ts_ms AS chg_ts, c.size AS chg_size FROM s ASOF LEFT JOIN _v_chg c
                  ON s.slug = c.slug AND s.side = c.side AND s.px = c.px AND s.t >= c.ts_ms)
            SELECT c.qid, CASE WHEN c.chg_ts IS NOT NULL AND (c.snap_ts IS NULL OR c.chg_ts > c.snap_ts) THEN c.chg_size
                               WHEN c.snap_ts IS NOT NULL THEN coalesce(l.size, 0) ELSE 0 END AS size
            FROM c LEFT JOIN _v_slv l ON l.slug = c.slug AND l.ts_ms = c.snap_ts AND l.side = c.side AND l.px = c.px
        """).df()
    finally:
        con.unregister("_q_df")
    return d.set_index("qid")["size"].astype(float)


def _best(con, q: pd.DataFrame, side: str) -> pd.DataFrame:
    """q[qid, slug, t] -> best level of `side` ('SELL' = asks, 'BUY' = bids) at t: [qid, px, size]
    (px NaN when the side is empty)."""
    con.register("_q_df", q[["qid", "slug", "t"]].astype({"qid": "int64", "t": "int64"}))
    try:
        cand = con.execute("""
            WITH s AS (SELECT q.*, s.ts_ms AS snap_ts FROM _q_df q ASOF LEFT JOIN _v_snap s
                       ON q.slug = s.slug AND q.t >= s.ts_ms)
            SELECT DISTINCT s.qid, s.slug, s.t, l.px FROM s JOIN _v_slv l
                ON l.slug = s.slug AND l.ts_ms = s.snap_ts AND l.side = ?
            UNION
            SELECT DISTINCT s.qid, s.slug, s.t, c.px FROM s JOIN _v_chg c
                ON c.slug = s.slug AND c.side = ? AND c.ts_ms <= s.t AND (s.snap_ts IS NULL OR c.ts_ms > s.snap_ts)
        """, [side, side]).df()
    finally:
        con.unregister("_q_df")
    out = pd.DataFrame({"qid": q.qid.astype("int64"), "px": np.nan, "size": np.nan}).set_index("qid")
    if cand.empty:
        return out.reset_index()
    cand = cand.reset_index(drop=True)
    cand["cid"] = np.arange(len(cand))
    sz = _size_at(con, cand.assign(side=side).rename(columns={"qid": "oq", "cid": "qid"}))
    cand["size"] = cand.cid.map(sz)
    cand = cand[cand["size"] > 0]
    best = cand.sort_values(["qid", "px"], ascending=[True, side == "SELL"]).groupby("qid").head(1).set_index("qid")
    out.loc[best.index, "px"] = best.px.astype(float)
    out.loc[best.index, "size"] = best["size"]
    return out.reset_index()


# ---------------------------------------------------------------------------------------- helpers

def _live(res) -> pd.DataFrame:
    m = res.markets
    return m[~m.voided.astype(bool)]


def _mk(con) -> pd.DataFrame:
    return con.execute("SELECT slug, outcome, fee_rate, fee_exponent, min_order_size, window_end_ms FROM _v_mk").df()\
        .set_index("slug")


def _placed(res, side: str) -> pd.DataFrame:
    o = res.orders
    return o[(o.event == "placed") & (o.kind == "take") & (o.token == "Up") & (o.side == side)]


def _range_start(days: Iterable[str] | None, ws: int) -> int:
    """Start of the replay range holding a market that starts at `ws`: the first day of its run of
    consecutive selected days (default: only its own day), never before REPLAY_START_MS."""
    from datetime import date, timedelta

    from .splits import REPLAY_START_MS

    d = _day(ws)
    sel = {date.fromisoformat(str(x)) for x in days} if days else {d}
    while d - timedelta(days=1) in sel:
        d -= timedelta(days=1)
    start = int(datetime(d.year, d.month, d.day, tzinfo=timezone.utc).timestamp() * 1000)
    return max(start, REPLAY_START_MS)


def ref_open_ms(con, slugs: Iterable[str], days: Iterable[str] | None = None) -> pd.Series:
    """When each market opens, from the raw data: the recv_ms of its first book snapshot (Up, or a
    Down one at a ts without an Up one) received inside the replay range. Needs `_stage` first."""
    mk = con.execute("SELECT slug, window_start_ms FROM _v_mk WHERE list_contains(?, slug)", [sorted(set(slugs))]).df()
    lo = pd.DataFrame({"slug": mk.slug, "lo": [_range_start(days, int(w)) for w in mk.window_start_ms]})
    con.register("_lo_df", lo)
    try:
        d = con.execute("""SELECT l.slug, min(s.recv_ms) AS t FROM _lo_df l JOIN _v_snap s
                           ON s.slug = l.slug AND s.recv_ms >= l.lo GROUP BY l.slug""").df()
    finally:
        con.unregister("_lo_df")
    return d.set_index("slug").t.astype("int64")


def _fnum(x) -> float | None:
    return None if x is None or (isinstance(x, float) and math.isnan(x)) else float(x)


def _compare(rows: list[dict], skipped: list[dict], missing: list[str], failures: list[dict]) -> dict:
    """`skipped`: only markets whose reference top level is too thin. `missing`: markets with no order;
    `failures`: anything else that cannot be compared (wrong open time, no sell, ...). Both fail."""
    diffs = [abs(r["sim"] - r["ref"]) for r in rows]
    bad = [r for r in rows if abs(r["sim"] - r["ref"]) >= CENT]
    return {"passed": bool(rows) and not bad and not missing and not failures, "compared": len(rows),
            "skipped": len(skipped), "missing_orders": missing, "failures": failures, "mismatches": bad,
            "max_abs_diff": max(diffs) if diffs else None,
            "sim_pnl": sum(r["sim"] for r in rows), "ref_pnl": sum(r["ref"] for r in rows),
            "skipped_markets": skipped}


def _opens(con, res, live, days) -> tuple[pd.Series, pd.Series, list[dict]]:
    """(sim buy placed ms, reference open ms, open-time failures) for the live markets."""
    pl = _placed(res, "BUY").groupby("slug").ms.min()
    pl = pl[pl.index.isin(live.slug)]
    ref = ref_open_ms(con, live.slug, days)
    bad = [{"slug": s, "why": "open time", "sim_placed_ms": int(pl[s]), "ref_open_ms": _fnum(ref.get(s))}
           for s in pl.index if ref.get(s) != pl[s]]
    return pl, ref, bad


# ---------------------------------------------------------------------------------------- checks

def check_reconstruction(con, res, timeframes: Iterable[str], rows: int = 1000) -> dict:
    from .marketdata import load_coin, reconstruction_rate

    live = _live(res)
    rates: dict[str, float] = {}
    for (coin, h), g in live.groupby([live.coin, live.window_start_ms // 3_600_000]):
        mds = load_coin(con, coin, list(timeframes), int(g.window_start_ms.min()), int(g.window_end_ms.max()),
                        slugs=set(g.slug))
        for slug in g.slug:
            md = mds.get(slug)
            rates[slug] = float("nan") if md is None else reconstruction_rate(con, md, n=rows, seed=0)
        del mds
    bad = {s: _fnum(r) for s, r in rates.items() if not r >= RECON_MIN}
    vals = [r for r in rates.values() if not math.isnan(r)]
    return {"passed": bool(rates) and not bad, "markets": len(rates), "rows_per_market": rows,
            "min_rate": min(vals) if vals else None, "mean_rate": float(np.mean(vals)) if vals else None,
            "failures": bad}


def check_noop(res) -> dict:
    live = _live(res)
    pnl = live.pnl.astype(float)
    return {"passed": bool(len(live)) and res.orders.empty and res.fills.empty and bool((pnl == 0.0).all()),
            "markets": len(live), "orders": len(res.orders), "pnl_total": float(pnl.sum())}


def check_buy_and_hold(con, res, order_latency_ms: int = 200, days: Iterable[str] | None = None) -> dict:
    """P&L = size * (outcome_up - ask) - taker_fee(size, ask) with the Up best ask when the order arrives:
    the market's open (its first in-range snapshot received, `ref_open_ms`) + latency."""
    live = _live(res)
    _stage(con, live.slug)
    mk = _mk(con)
    pl, opened, failures = _opens(con, res, live, days)
    missing = sorted(set(live.slug) - set(pl.index))
    slugs = [s for s in pl.index if s in opened.index]
    q = pd.DataFrame({"slug": slugs, "t": [int(opened[s]) + order_latency_ms for s in slugs]})
    q["qid"] = np.arange(len(q))
    ask = _best(con, q, "SELL").set_index("qid")
    sim = live.set_index("slug").pnl.astype(float)
    rows, skipped = [], []
    for r in q.itertuples():
        info = mk.loc[r.slug]
        n = float(info.min_order_size)
        a_px, a_sz = ask.loc[r.qid, "px"], ask.loc[r.qid, "size"]
        if not a_sz >= n:
            skipped.append({"slug": r.slug, "ask": _fnum(a_px / 1000), "ask_size": _fnum(a_sz)})
            continue
        a = a_px / 1000
        ref = n * (float(info.outcome == "Up") - a) - taker_fee(n, a, float(info.fee_rate), float(info.fee_exponent))
        rows.append({"slug": r.slug, "t": int(r.t), "ask": a, "sim": float(sim[r.slug]), "ref": ref})
    return _compare(rows, skipped, missing, failures)


def check_always_cross(con, res, order_latency_ms: int = 200, days: Iterable[str] | None = None) -> dict:
    """Loss = (ask_t1 - bid_t2) * size + fee(ask) + fee(bid): ask at the buy's arrival (reference open +
    latency), bid at the sell's. Only a thin top level (< min_order_size) is a skip; a market with a
    fillable buy but no sell, or a sell arriving after the window end, is a failure."""
    live = _live(res)
    _stage(con, live.slug)
    mk = _mk(con)
    buy, opened, failures = _opens(con, res, live, days)
    sell = _placed(res, "SELL").groupby("slug").ms.min()
    missing = sorted(set(live.slug) - set(buy.index))
    slugs = [s for s in buy.index if s in opened.index]
    q = pd.DataFrame({"slug": slugs, "t": [int(opened[s]) + order_latency_ms for s in slugs]})
    q["qid"] = np.arange(len(q))
    ask = _best(con, q, "SELL").set_index("qid")
    q2 = q[q.slug.isin(sell.index)].copy()
    q2["t"] = [int(sell[s]) + order_latency_ms for s in q2.slug]
    bid = _best(con, q2, "BUY").set_index("qid") if len(q2) else pd.DataFrame(columns=["px", "size"])
    sim = live.set_index("slug").pnl.astype(float)
    rows, skipped = [], []
    for r in q.itertuples():
        info = mk.loc[r.slug]
        n = float(info.min_order_size)
        a_px, a_sz = ask.loc[r.qid, "px"], ask.loc[r.qid, "size"]
        if not a_sz >= n:
            skipped.append({"slug": r.slug, "ask_size": _fnum(a_sz)})
            continue
        if r.qid not in bid.index:
            failures.append({"slug": r.slug, "why": "the buy could fill but no sell was placed"})
            continue
        t2 = int(q2.set_index("qid").loc[r.qid, "t"])
        if t2 >= int(info.window_end_ms):
            failures.append({"slug": r.slug, "why": "the sell arrives after the window end", "t2": t2})
            continue
        b_px, b_sz = bid.loc[r.qid, "px"], bid.loc[r.qid, "size"]
        if not b_sz >= n:
            skipped.append({"slug": r.slug, "ask_size": _fnum(a_sz), "bid_size": _fnum(b_sz)})
            continue
        a, b = a_px / 1000, b_px / 1000
        rate, ex = float(info.fee_rate), float(info.fee_exponent)
        ref = n * (b - a) - taker_fee(n, a, rate, ex) - taker_fee(n, b, rate, ex)
        rows.append({"slug": r.slug, "ask": a, "bid": b, "sim": float(sim[r.slug]), "ref": ref})
    return _compare(rows, skipped, missing, failures)


def _lifetimes(orders: pd.DataFrame, order_latency_ms: int) -> pd.DataFrame:
    """Accepted limit orders: slug, order_id, Up-terms side/px, size, arrival (placed + latency),
    accepted_ms and sim queue_ahead, end_ms (cancel/expiry, NaN if it filled), last fill ms."""
    o = orders
    pl = o[(o.event == "placed") & (o.kind == "limit")].drop_duplicates(["slug", "order_id"])
    pl = pl[["slug", "order_id", "ms", "token", "side", "px", "size"]].rename(columns={"ms": "placed_ms"})
    acc = o[o.event == "accepted"].groupby(["slug", "order_id"]).agg(accepted_ms=("ms", "min"),
                                                                      sim_ahead=("queue_ahead", "first"))
    end = o[o.event.isin(["cancelled", "expired"])].groupby(["slug", "order_id"]).ms.min().rename("end_ms")
    fl = o[o.event == "fill"].groupby(["slug", "order_id"]).ms.max().rename("last_fill_ms")
    d = pl.join(acc, on=["slug", "order_id"], how="inner").join(end, on=["slug", "order_id"])\
        .join(fl, on=["slug", "order_id"]).reset_index(drop=True)
    tpx = (d.px.astype(float) * 1000).round().astype("int64")
    dn = d.token == "Down"
    d["up_side"] = np.where(dn, np.where(d.side == "BUY", "SELL", "BUY"), d.side)
    d["up_px"] = np.where(dn, 1000 - tpx, tpx)
    d["arrival"] = d.placed_ms.astype("int64") + order_latency_ms
    return d


def one_order_per_side(orders: pd.DataFrame, order_latency_ms: int = 200) -> bool:
    """True if no two accepted limit orders of a market rest on the same Up-terms side at once."""
    d = _lifetimes(orders, order_latency_ms)
    if d.empty:
        return True
    d["stop"] = d.end_ms.fillna(d.last_fill_ms).fillna(np.inf)
    for _, g in d.sort_values("arrival").groupby(["slug", "up_side"]):
        if (g.arrival.to_numpy()[1:] <= g.stop.to_numpy()[:-1]).any():
            return False
    return True


_FILL_SQL = f"""
WITH m AS (
    SELECT o.slug, o.order_id, t.ts_ms, t.recv_ms, t.seq, t.size AS x, t.up_px = o.up_px AS at_px
    FROM _o o JOIN _v_tr t ON t.slug = o.slug AND t.taker_up_side <> o.up_side
     AND t.ts_ms > o.arrival + {TRADE_ECHO_MS} AND t.ts_ms < o.window_end_ms
     AND t.ts_ms <= coalesce(o.end_ms, o.window_end_ms)
     AND ((o.up_side = 'BUY' AND t.up_px <= o.up_px) OR (o.up_side = 'SELL' AND t.up_px >= o.up_px))),
c AS (
    SELECT *, sum(CASE WHEN at_px THEN x ELSE 0 END) OVER w AS at_cum,
              sum(CASE WHEN at_px THEN 0 ELSE x END) OVER w AS thr_cum
    FROM m WINDOW w AS (PARTITION BY slug, order_id ORDER BY ts_ms, recv_ms, seq ROWS UNBOUNDED PRECEDING)),
f AS (
    -- at our price a trade first eats the queue ahead; a trade-through fills up to its size
    SELECT c.*, o.ahead, least(o.size, c.thr_cum + greatest(0, c.at_cum - o.ahead)) AS filled_cum
    FROM c JOIN _o o USING (slug, order_id)),
g AS (
    SELECT slug, order_id, ts_ms, greatest(0, ahead - at_cum) AS ahead_left,
           filled_cum - coalesce(lag(filled_cum) OVER (PARTITION BY slug, order_id ORDER BY ts_ms, recv_ms, seq), 0) AS q
    FROM f)
-- ahead_left: the queue still ahead of us after the last trade that filled us in that ms
SELECT slug, order_id, ts_ms, sum(q) AS size, min(ahead_left) FILTER (WHERE q > {SIZE_EPS}) AS ahead_left
FROM g GROUP BY ALL HAVING sum(q) > {SIZE_EPS}
"""


def rederive_fills(con, res, order_latency_ms: int = 200) -> tuple[pd.DataFrame, pd.DataFrame]:
    """(orders with the reference `ahead`, reference fills [slug, order_id, ts_ms, size]). Assumes one
    order per side per market at a time (see `one_order_per_side`)."""
    d = _lifetimes(res.orders, order_latency_ms)
    _stage(con, d.slug.unique() if len(d) else [])
    if d.empty:
        return d, pd.DataFrame(columns=["slug", "order_id", "ts_ms", "size", "ahead_left", "px"])
    we = con.execute("SELECT slug, window_end_ms FROM _v_mk").df().set_index("slug").window_end_ms
    d["window_end_ms"] = d.slug.map(we).astype("int64")
    d["qid"] = np.arange(len(d))
    d["ahead"] = d.qid.map(_size_at(con, d.assign(side=d.up_side, px=d.up_px, t=d.arrival))).astype(float)
    cols = ["slug", "order_id", "up_side", "up_px", "size", "arrival", "end_ms", "window_end_ms", "ahead"]
    con.register("_o_df", d[cols].astype({"order_id": "int64", "up_px": "int64", "size": float, "end_ms": float}))
    try:
        con.execute("CREATE OR REPLACE TEMP TABLE _o AS SELECT * EXCLUDE (end_ms), end_ms::BIGINT AS end_ms FROM _o_df")
    finally:
        con.unregister("_o_df")
    ref = con.execute(_FILL_SQL).df()
    # a maker fill is at the order's own limit price, in its own token's terms
    ref["px"] = ref.set_index(["slug", "order_id"]).index.map(d.set_index(["slug", "order_id"]).px).astype(float)
    return d, ref


def check_fill_support(con, res, order_latency_ms: int = 200) -> dict:
    """Per (order, trade ts): the filled size, the fill price (the order's own limit, token terms) and the
    queue still ahead at the fill must equal the re-derivation; the queue joined behind at arrival
    (the accepted row's queue_ahead) must equal the recorded level size then."""
    if not one_order_per_side(res.orders, order_latency_ms):
        return {"passed": False, "error": "more than one order per side at a time: the re-derivation does not apply"}
    d, ref = rederive_fills(con, res, order_latency_ms)
    f = res.fills
    key = ["slug", "order_id", "ts_ms"]
    taker = int((f.liquidity != "maker").sum()) if len(f) else 0
    if len(f):
        sim = f.groupby(key).agg(sim=("size", "sum"), sim_px_min=("px", "min"), sim_px_max=("px", "max"),
                                 sim_ahead=("queue_ahead_at_fill", "min"))
    else:
        sim = pd.DataFrame(columns=["sim", "sim_px_min", "sim_px_max", "sim_ahead"])
    refs = ref.set_index(key).rename(columns={"size": "ref", "px": "ref_px", "ahead_left": "ref_ahead"})
    j = sim.join(refs, how="outer")
    j[["sim", "ref"]] = j[["sim", "ref"]].fillna(0.0)
    both = j.ref_px.notna() & j.sim_px_min.notna()
    bad = j[(j.sim - j.ref).abs() > SIZE_EPS]
    px_bad = j[(j.ref_px.isna() & j.sim_px_min.notna())
               | (both & (((j.sim_px_min - j.ref_px).abs() > 1e-9) | ((j.sim_px_max - j.ref_px).abs() > 1e-9)))]
    fill_ahead_bad = j[both & ((j.sim_ahead - j.ref_ahead).abs() > SIZE_EPS)]
    acc_bad = d[d.accepted_ms != d.arrival] if len(d) else d
    ahead_bad = d[(d.sim_ahead - d.ahead).abs() > SIZE_EPS] if len(d) else d

    def ex(frame, cols):
        return [{"slug": s, "order_id": int(o), "ts_ms": int(t), **{c: _fnum(r[c]) for c in cols}}
                for (s, o, t), r in frame.head(10).iterrows()]

    return {"passed": len(sim) > 0 and bad.empty and px_bad.empty and fill_ahead_bad.empty and acc_bad.empty
                      and ahead_bad.empty and taker == 0,
            "orders": len(d), "sim_fills": int(len(sim)), "ref_fills": int(len(refs)),
            "sim_shares": float(f["size"].sum()) if len(f) else 0.0, "ref_shares": float(ref["size"].sum()) if len(ref) else 0.0,
            "mismatches": int(len(bad)), "px_mismatches": int(len(px_bad)),
            "fill_ahead_mismatches": int(len(fill_ahead_bad)), "ahead_mismatches": int(len(ahead_bad)),
            "arrival_mismatches": int(len(acc_bad)), "taker_fills": taker,
            "examples": ex(bad, ["sim", "ref"]) + ex(px_bad, ["sim_px_min", "sim_px_max", "ref_px"])
                        + ex(fill_ahead_bad, ["sim_ahead", "ref_ahead"])}


def average_maker_c(con, slugs: Iterable[str]) -> float:
    """Size-weighted maker P&L (cents/share) of every recorded trade of `slugs` before the window end,
    marked to the outcome (makers pay no fee), as in the calibration study."""
    return float(con.execute("""
        SELECT 100 * sum(t.size * CASE WHEN t.side = 'BUY' THEN 1 ELSE -1 END
                         * (t.price - (t.outcome = m.outcome)::INT)) / sum(t.size)
        FROM pm_trade t JOIN markets m USING (slug)
        WHERE list_contains(?, t.slug) AND m.outcome IS NOT NULL AND t.ts_ms < m.window_end_ms""",
                             [sorted(set(slugs))]).fetchall()[0][0])


def fills_c_per_share(fills: pd.DataFrame, markets: pd.DataFrame) -> float:
    """Size-weighted P&L of our fills (cents/share) marked to the outcome, fees included."""
    won = fills.slug.map(markets.set_index("slug").outcome) == fills.token
    sgn = np.where(fills.side == "BUY", 1.0, -1.0)
    fee = fills["fee"] if "fee" in fills else 0.0
    pnl = sgn * (won.astype(float) - fills.px.astype(float)) * fills["size"] - fee
    return float(100 * pnl.sum() / fills["size"].sum())


def check_maker_edge(con, res) -> dict:
    slugs = list(_live(res).slug)
    avg = average_maker_c(con, slugs)
    f = res.fills
    ours = fills_c_per_share(f, res.markets) if len(f) else float("nan")
    return {"passed": bool(len(f)) and ours < avg, "join_best_c_per_share": _fnum(ours),
            "average_maker_c_per_share": avg, "markets": len(slugs), "join_best_shares": float(f["size"].sum()) if len(f) else 0.0}


# ---------------------------------------------------------------------------------------- determinism

def _frame_sha(df: pd.DataFrame) -> str:
    lines = df.to_csv(index=False, lineterminator="\n").split("\n")
    body = sorted(x for x in lines[1:] if x)
    return hashlib.sha256(("\n".join([lines[0], *body]) + "\n").encode()).hexdigest()


def summary(res) -> dict:
    m = res.markets.sort_values("slug")
    return {"markets": {s: _fnum(p) for s, p in zip(m.slug, m.pnl.astype(float))},
            "pnl_total": float(m.pnl.astype(float).fillna(0).sum()), "orders": len(res.orders),
            "fills": len(res.fills), "orders_sha256": _frame_sha(res.orders), "fills_sha256": _frame_sha(res.fills)}


def summary_bytes(res) -> bytes:
    return json.dumps(summary(res), sort_keys=True, separators=(",", ":"), allow_nan=False).encode()


def check_determinism(a, b) -> dict:
    x, y = summary_bytes(a), summary_bytes(b)
    return {"passed": x == y, "sha256": [hashlib.sha256(x).hexdigest(), hashlib.sha256(y).hexdigest()]}


# ---------------------------------------------------------------------------------------- suite

def _clean(x):
    if isinstance(x, dict):
        return {str(k): _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    if isinstance(x, (np.integer,)):
        return int(x)
    if isinstance(x, (float, np.floating)):
        return None if math.isnan(x) else float(x)
    if isinstance(x, np.bool_):
        return bool(x)
    return x


def run_suite(days: list[str], coins=("btc", "sol"), timeframes=("5m",), *, con_factory: Callable | None = None,
              recon_rows: int = 1000, workers: int | None = None, write: bool = True) -> dict:
    """Run checks 1-4, write `<data>/runs/validation/<sim_version>.json` and return it."""
    from . import engine
    from .config import RunConfig

    con_factory = con_factory or engine.default_con_factory
    base = dict(coins=list(coins), timeframes=list(timeframes), days=[str(d) for d in days])
    t0 = time.perf_counter()
    runs, timing = {}, {}
    for name in (*STRATEGIES, "join-best#2"):
        t = time.perf_counter()
        cfg = RunConfig(strategy=name.split("#")[0], **base)
        runs[name] = engine.run(cfg, con_factory, workers)
        timing[name] = round(time.perf_counter() - t, 1)
    lat = RunConfig(strategy="noop", **base).order_latency_ms
    con = con_factory()
    try:
        checks = {
            "book_reconstruction": check_reconstruction(con, runs["noop"], timeframes, recon_rows),
            "noop": check_noop(runs["noop"]),
            "buy_and_hold_up": check_buy_and_hold(con, runs["buy-and-hold-up"], lat, base["days"]),
            "always_cross": check_always_cross(con, runs["always-cross"], lat, base["days"]),
            "fill_support": check_fill_support(con, runs["join-best"], lat),
            "maker_edge": check_maker_edge(con, runs["join-best"]),
            "determinism": check_determinism(runs["join-best"], runs["join-best#2"]),
        }
    finally:
        con.close()
    v = sim_version()
    out = _clean({"sim_version": v, "passed": all(c["passed"] for c in checks.values()),
                  "created_utc": datetime.now(timezone.utc).isoformat(timespec="seconds"), "days": base["days"],
                  "coins": base["coins"], "timeframes": base["timeframes"], "run_seconds": timing,
                  "elapsed_s": round(time.perf_counter() - t0, 1), "checks": checks})
    if write:
        p = validation_path(v)
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(out, indent=1, sort_keys=True))
    return out


__all__ = ["average_maker_c", "check_always_cross", "check_buy_and_hold", "check_determinism", "check_fill_support",
           "check_maker_edge", "check_noop", "check_reconstruction", "fills_c_per_share", "is_validated",
           "one_order_per_side", "rederive_fills", "run_suite", "sim_version", "summary", "summary_bytes",
           "validation_path"]

"""Per-market Polymarket data for the backtester: the combined Up-terms book, tops, trades, ticks.

Polymarket publishes every level change twice (Up and its Down mirror) and every trade once, on the
token the taker traded. `MarketData` keeps one combined book in Up terms (see `types.to_up`):

- **Levels.** Built from Up `pm_price_change` rows only; a Down row is used (mapped to Up terms)
  only when its Up mirror (same `ts_ms`, opposite side, `1000 - px`) is missing. A row sets the
  level to `size` (0 removes it).
- **Snapshots.** A `pm_book` snapshot (Up; a Down one mapped when no Up snapshot has the same
  `ts_ms`) replaces the book: every level it lists is set, every other level > 0 gets a size-0
  entry at the snapshot's time. A snapshot reflects the state *after* the price changes with the
  same `ts_ms` (verified on 2026-11-24 data), so it is ordered after them.
- **Tops** (strategy-visible) come from the `best_bid`/`best_ask` columns of the change rows (Up,
  plus unmirrored Down rows mapped) and the snapshot tops, one row per change of (bid_px, ask_px).
  Sizes are looked up in the level series.
- **Trades** keep the token and the taker side, plus the taker side/price in Up terms. Verified on
  2026-11-24 data: the consumed-side level drops within +-50 ms of a trade for ~82 % of trades,
  the opposite side for < 1 %. Trade `ts_ms` lags the level change by a few ms (median ~4 ms).
- **Same-ms order.** Rows are ordered by (ts_ms, recv_ms, seq): `seq` is the raw arrival order the
  Parquet build keeps (`polydata.build`), so changes sharing a millisecond apply in recorded order.

Everything is built in DuckDB (one query per stream for all of a coin's markets) and sliced into
numpy arrays; nothing iterates over price-change rows in Python. Time `t` is inclusive: the state
at `t` includes every event with time <= t. `clock="ts"` is exchange time, `clock="recv"` our
receive time (a level change counts as received once it and every earlier change of that level
have arrived, i.e. the running max of `recv_ms` in exchange order).
"""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Literal, NamedTuple

import numpy as np
import pandas as pd

from .types import NO_ASK_PX, NO_BID_PX, PX_SCALE, MarketInfo, Side, Top, Trade, to_px

Clock = Literal["ts", "recv"]
SIDES: tuple[Side, Side] = ("BUY", "SELL")
TOKENS = ("Up", "Down")


class Tops(NamedTuple):
    """Top-of-book changes; prices are int32 px."""
    ts_ms: np.ndarray
    recv_ms: np.ndarray
    bid_px: np.ndarray
    ask_px: np.ndarray


class Trades(NamedTuple):
    """Public trades as arrays. `MarketData.trades` is in ts_ms order (the exchange clock, for
    matching); `MarketData.trades_recv` holds the same trades in recv_ms order (what the strategy sees)."""
    ts_ms: np.ndarray
    recv_ms: np.ndarray
    token: np.ndarray  # 0 Up, 1 Down
    side: np.ndarray  # taker side on token: 0 BUY, 1 SELL
    price: np.ndarray  # token terms
    size: np.ndarray
    up_side: np.ndarray  # taker side in Up terms: 0 BUY, 1 SELL
    up_px: np.ndarray


class Ticks(NamedTuple):
    ts_ms: np.ndarray
    recv_ms: np.ndarray
    tick_px: np.ndarray


class _Level(NamedTuple):
    ts_ms: np.ndarray
    recv_ms: np.ndarray  # raw receive times, in exchange order
    recv_eff: np.ndarray  # running max of recv_ms: when the state is known at our end
    size: np.ndarray
    last_pos: np.ndarray  # index of the last event at or before i with size > 0, or -1


# --------------------------------------------------------------------------------------------- SQL
# Inputs (temp tables): _pc(mi, ts_ms, recv_ms, seq, dn, sell, px, size, bb, ba) — compact: see _PC_SELECT,
# _bk(mi, ts_ms, recv_ms, seq, outcome, bids, asks), _tr(mi, ts_ms, recv_ms, seq, outcome, side, price, size),
# _tk(mi, ts_ms, recv_ms, new_tick_size). `mi` is the market index, `seq` the row order inside a stream.
# Event order key k = ts_ms * 2 (+1 for snapshots, so they follow same-ts changes).

_BID = "CASE WHEN {b} > 0 THEN round({b} * 1000)::INTEGER ELSE 0 END"
_ASK = "CASE WHEN {a} > 0 AND {a} < 1 THEN round({a} * 1000)::INTEGER ELSE 1000 END"

# Compact form of pm_price_change rows (token-terms side/px; bb/ba: 0 / 1000 for an empty side).
_PC_SELECT = f"""outcome = 'Down' AS dn, side = 'SELL' AS sell, round(price * 1000)::SMALLINT AS px, size,
    ({_BID.format(b='best_bid')})::SMALLINT AS bb, ({_ASK.format(a='best_ask')})::SMALLINT AS ba"""

# Level changes in Up terms; `sell` = the Up-terms side is SELL (an ask).
_SQL_CHANGES = """
CREATE OR REPLACE TEMP TABLE _chg AS
SELECT mi, ts_ms, recv_ms, seq, sell, px, size, bb, ba FROM _pc WHERE NOT dn
UNION ALL
SELECT mi, ts_ms, recv_ms, seq, NOT sell, (1000 - px)::SMALLINT, size,
       (CASE WHEN ba < 1000 THEN 1000 - ba ELSE 0 END)::SMALLINT, (CASE WHEN bb > 0 THEN 1000 - bb ELSE 1000 END)::SMALLINT
FROM _pc d WHERE dn AND NOT EXISTS (
    SELECT 1 FROM _pc u WHERE NOT u.dn AND u.mi = d.mi AND u.ts_ms = d.ts_ms AND u.px = 1000 - d.px AND u.sell <> d.sell)
"""

_SQL_SNAPSHOTS = """
CREATE OR REPLACE TEMP TABLE _snap AS
WITH up AS (SELECT * FROM _bk WHERE outcome = 'Up'),
eff AS (
    SELECT * FROM up
    UNION ALL
    SELECT * FROM _bk d WHERE outcome = 'Down' AND NOT EXISTS (
        SELECT 1 FROM up u WHERE u.mi = d.mi AND u.ts_ms = d.ts_ms))
SELECT mi, ts_ms, recv_ms, seq, outcome, bids, asks, ts_ms * 2 + 1 AS k
FROM eff
QUALIFY row_number() OVER (PARTITION BY mi, ts_ms ORDER BY outcome DESC, recv_ms DESC, seq DESC) = 1;

CREATE OR REPLACE TEMP TABLE _sl AS
WITH l AS (
    SELECT mi, ts_ms, recv_ms, seq, k, outcome, 'BUY' AS tside, unnest(levels(bids)) AS l FROM _snap
    UNION ALL
    SELECT mi, ts_ms, recv_ms, seq, k, outcome, 'SELL', unnest(levels(asks)) FROM _snap)
SELECT mi, ts_ms, recv_ms, seq, k,
       (outcome = 'Up') = (tside = 'SELL') AS sell,
       (CASE WHEN outcome = 'Up' THEN round(l.price * 1000) ELSE 1000 - round(l.price * 1000) END)::SMALLINT AS px,
       l.size AS size
FROM l;
"""

_ORDER = "k, recv_ms, seq, src, size"

_SQL_LEVELS = f"""
CREATE OR REPLACE TEMP TABLE _ev AS
SELECT mi, sell, px, ts_ms * 2 AS k, ts_ms, recv_ms, seq, 0 AS src, size FROM _chg
UNION ALL
SELECT mi, sell, px, k, ts_ms, recv_ms, seq, 1, size FROM _sl;

-- A level > 0 after key k whose next event at a later key comes after the next snapshot (or never)
-- is absent from that snapshot: it gets a size-0 entry at the snapshot.
CREATE OR REPLACE TEMP TABLE _zero AS
WITH g AS (SELECT mi, sell, px, k, arg_max(size, (recv_ms, seq, src)) > 0 AS pos FROM _ev GROUP BY ALL),
e AS (SELECT *, lead(k) OVER (PARTITION BY mi, sell, px ORDER BY k) AS nk FROM g),
s AS (SELECT mi, k, ts_ms, recv_ms, seq FROM _snap)
SELECT e.mi, e.sell, e.px, s.k, s.ts_ms, s.recv_ms, s.seq, 2 AS src, 0.0 AS size
FROM e ASOF JOIN s ON e.mi = s.mi AND e.k < s.k
WHERE e.pos AND (e.nk IS NULL OR e.nk > s.k);
"""

_SQL_LEVELS_OUT = f"""
SELECT mi, sell::TINYINT AS side, px, ts_ms, recv_ms, size
FROM (SELECT * FROM _ev UNION ALL SELECT mi, sell, px, k, ts_ms, recv_ms, seq, src, size FROM _zero)
ORDER BY mi, side, px, {_ORDER}
"""

_SQL_TOPS = """
CREATE OR REPLACE TEMP TABLE _top AS
SELECT mi, ts_ms * 2 AS k, ts_ms, recv_ms, seq, bb, ba FROM _chg
UNION ALL
SELECT s.mi, s.k, s.ts_ms, s.recv_ms, s.seq,
       coalesce(max(l.px) FILTER (WHERE NOT l.sell AND l.size > 0), 0),
       coalesce(min(l.px) FILTER (WHERE l.sell AND l.size > 0), 1000)
FROM _snap s LEFT JOIN _sl l ON l.mi = s.mi AND l.k = s.k
GROUP BY s.mi, s.k, s.ts_ms, s.recv_ms, s.seq;
"""


def _sql_tops_out(order: str) -> str:
    """Top rows in `order`, keeping a row only where (bid, ask) changes."""
    return f"""
    SELECT mi, ts_ms, recv_ms, bb::INTEGER AS bid_px, ba::INTEGER AS ask_px FROM (
        SELECT *, lag(bb) OVER w AS pb, lag(ba) OVER w AS pa FROM _top
        WINDOW w AS (PARTITION BY mi ORDER BY {order}))
    WHERE pb IS NULL OR pb <> bb OR pa <> ba
    ORDER BY mi, {order}"""


_SQL_TRADES_OUT = """
SELECT mi, ts_ms, recv_ms, (outcome = 'Down')::TINYINT AS token, (side = 'SELL')::TINYINT AS side, price, size,
       ((outcome = 'Down') <> (side = 'SELL'))::TINYINT AS up_side,
       (CASE WHEN outcome = 'Up' THEN round(price * 1000) ELSE 1000 - round(price * 1000) END)::INTEGER AS up_px
FROM _tr ORDER BY mi, ts_ms, recv_ms, seq
"""

_SQL_TICKS_OUT = """
SELECT mi, ts_ms, min(recv_ms) AS recv_ms, round(new_tick_size * 1000)::INTEGER AS tick_px
FROM _tk GROUP BY mi, ts_ms, tick_px ORDER BY mi, ts_ms, recv_ms
"""


def _build(con, n_markets: int) -> list[dict]:
    """Run the pipeline over the _pc/_bk/_tr/_tk temp tables; return one dict of arrays per market."""
    con.execute(_SQL_CHANGES)
    con.execute("DROP TABLE IF EXISTS _pc")
    con.execute(_SQL_SNAPSHOTS)
    con.execute("DROP TABLE IF EXISTS _bk")
    con.execute(_SQL_LEVELS)
    con.execute(_SQL_TOPS)
    con.execute("DROP TABLE IF EXISTS _chg; DROP TABLE IF EXISTS _sl")
    parts = {
        "levels": con.execute(_SQL_LEVELS_OUT).fetchnumpy(),
        "tops": con.execute(_sql_tops_out("k, recv_ms, seq")).fetchnumpy(),
        "tops_recv": con.execute(_sql_tops_out("recv_ms, k, seq")).fetchnumpy(),
        "trades": con.execute(_SQL_TRADES_OUT).fetchnumpy(),
        "ticks": con.execute(_SQL_TICKS_OUT).fetchnumpy(),
        "snaps": con.execute("SELECT mi, recv_ms FROM _snap ORDER BY mi, recv_ms").fetchnumpy(),
    }
    for t in ("_snap", "_ev", "_zero", "_top"):
        con.execute(f"DROP TABLE IF EXISTS {t}")
    out: list[dict] = [{} for _ in range(n_markets)]
    for name, cols in parts.items():
        cols = {c: np.asarray(v) for c, v in cols.items()}
        mi = cols.pop("mi")
        bounds = np.searchsorted(mi, np.arange(n_markets + 1))
        for m in range(n_markets):
            a, b = bounds[m], bounds[m + 1]
            out[m][name] = {c: v[a:b] for c, v in cols.items()}
    return out


# ------------------------------------------------------------------------------------- MarketData

def _search(arr: np.ndarray, t: int) -> int:
    """Index of the last element <= t, or -1."""
    return int(np.searchsorted(arr, t, side="right")) - 1


class MarketData:
    """One market's Polymarket data in Up terms. Build with `from_frames` or `load_coin`."""

    def __init__(self, info: MarketInfo, arrays: dict):
        self.info = info
        lv = arrays["levels"]
        # Arrays arrive in their final dtypes from SQL; astype(copy=False) only guards that.
        side, px = lv["side"].astype(np.int8, copy=False), lv["px"].astype(np.int16, copy=False)
        ts, recv = lv["ts_ms"].astype(np.int64, copy=False), lv["recv_ms"].astype(np.int64, copy=False)
        size = lv["size"].astype(np.float64, copy=False)
        n = len(ts)
        self._levels: dict[tuple[str, int], _Level] = {}
        self._keys: dict[str, list[int]] = {"BUY": [], "SELL": []}
        if n:
            new = np.r_[True, (side[1:] != side[:-1]) | (px[1:] != px[:-1])]
            start = np.flatnonzero(new)
            group = np.cumsum(new) - 1
            base = int(recv.min())
            span = int(recv.max()) - base + 1
            recv_eff = np.maximum.accumulate((recv - base) + group * span) - group * span + base
            del group
            # last index with size > 0 at or before each event (global index; < group start -> none)
            pos = np.maximum.accumulate(np.where(size > 0, np.arange(n), -1))
            ends = np.r_[start[1:], n]
            for a, b in zip(start.tolist(), ends.tolist()):
                key = (SIDES[side[a]], int(px[a]))
                lp = pos[a:b] - a
                lp[lp < 0] = -1
                self._levels[key] = _Level(ts[a:b], recv[a:b], recv_eff[a:b], size[a:b], lp)
                self._keys[key[0]].append(key[1])
        self._keys["BUY"].sort(reverse=True)
        self._keys["SELL"].sort()

        self.tops = self._tops(arrays["tops"])  # ts_ms order
        self.tops_recv = self._tops(arrays["tops_recv"])  # recv_ms order
        tr = arrays["trades"]
        self.trades = Trades(tr["ts_ms"].astype(np.int64, copy=False), tr["recv_ms"].astype(np.int64, copy=False),
                             tr["token"].astype(np.int8, copy=False), tr["side"].astype(np.int8, copy=False),
                             tr["price"].astype(np.float64, copy=False), tr["size"].astype(np.float64, copy=False),
                             tr["up_side"].astype(np.int8, copy=False), tr["up_px"].astype(np.int32, copy=False))
        o = np.lexsort((np.arange(len(self.trades.ts_ms)), self.trades.recv_ms))  # recv, then ts order
        self.trades_recv = Trades(*(a[o] for a in self.trades))
        tk = arrays["ticks"]
        tk_ts, tk_recv = tk["ts_ms"].astype(np.int64, copy=False), tk["recv_ms"].astype(np.int64, copy=False)
        tk_px = tk["tick_px"].astype(np.int32, copy=False)
        self.ticks = Ticks(tk_ts, tk_recv, tk_px)
        o = np.argsort(tk_recv, kind="stable")
        self._ticks_recv = Ticks(tk_ts[o], tk_recv[o], tk_px[o])
        # receive times of the book snapshots (sorted): the book is trusted from the first one on
        sn = arrays.get("snaps")
        self.snaps_recv = (np.sort(sn["recv_ms"].astype(np.int64)) if sn is not None
                           else np.empty(0, np.int64))

    @staticmethod
    def _tops(d: dict) -> Tops:
        return Tops(d["ts_ms"].astype(np.int64, copy=False), d["recv_ms"].astype(np.int64, copy=False),
                    d["bid_px"].astype(np.int32, copy=False), d["ask_px"].astype(np.int32, copy=False))

    def __repr__(self) -> str:
        return (f"MarketData({self.info.slug}: {len(self._levels)} levels, "
                f"{sum(len(v.ts_ms) for v in self._levels.values())} level events, "
                f"{len(self.tops.ts_ms)} tops, {len(self.trades.ts_ms)} trades)")

    # -- levels
    def _times(self, lv: _Level, clock: Clock) -> np.ndarray:
        if clock == "ts":
            return lv.ts_ms
        if clock == "recv":
            return lv.recv_eff
        raise ValueError(f"unknown clock {clock!r}")

    def size_at(self, up_side: Side, px: int, t: int, clock: Clock = "ts") -> float:
        lv = self._levels.get((up_side, int(px)))
        if lv is None:
            return 0.0
        i = _search(self._times(lv, clock), t)
        return float(lv.size[i]) if i >= 0 else 0.0

    def book_at(self, t: int, clock: Clock = "ts", depth: int | None = None
                ) -> tuple[list[tuple[int, float]], list[tuple[int, float]]]:
        """(bids descending, asks ascending), Up terms, levels with size > 0."""
        out: tuple[list, list] = ([], [])
        for side, lst in zip(SIDES, out):
            for px in self._keys[side]:
                s = self.size_at(side, px, t, clock)
                if s > 0:
                    lst.append((px, s))
                    if depth is not None and len(lst) >= depth:
                        break
        return out

    def level_changes(self, up_side: Side, px: int, t0: int, t1: int, clock: Clock = "ts"
                      ) -> tuple[np.ndarray, np.ndarray, np.ndarray]:
        """(ts_ms, recv_ms, size) of the level's changes with time in (t0, t1]."""
        lv = self._levels.get((up_side, int(px)))
        if lv is None:
            e = np.empty(0, np.int64)
            return e, e, np.empty(0, np.float64)
        times = self._times(lv, clock)
        a, b = np.searchsorted(times, [t0, t1], side="right")
        return lv.ts_ms[a:b], lv.recv_ms[a:b], lv.size[a:b]

    def prices(self, up_side: Side) -> list[int]:
        """Every recorded price of `up_side`, best first (bids descending, asks ascending)."""
        return self._keys[up_side]

    def levels(self) -> list[tuple[str, int]]:
        return list(self._levels)

    # -- tops
    def top_at(self, t: int, clock: Clock = "recv") -> Top:
        """Top of book at t on `clock` (default: what the strategy has seen). Prices come from the
        reported tops; sizes from the level series on the same clock (see `_top_size`: a present
        price never has size 0, NaN when no positive size is known yet)."""
        tops = self.tops_recv if clock == "recv" else self.tops if clock == "ts" else None
        if tops is None:
            raise ValueError(f"unknown clock {clock!r}")
        i = _search(tops.recv_ms if clock == "recv" else tops.ts_ms, t)
        if i < 0:
            return Top(0, 0, NO_BID_PX, 0.0, NO_ASK_PX, 0.0)
        bid, ask = int(tops.bid_px[i]), int(tops.ask_px[i])
        return Top(int(tops.recv_ms[i]), int(tops.ts_ms[i]),
                   bid, self._top_size("BUY", bid, t, clock) if bid != NO_BID_PX else 0.0,
                   ask, self._top_size("SELL", ask, t, clock) if ask != NO_ASK_PX else 0.0)

    def _top_size(self, side: Side, px: int, t: int, clock: Clock) -> float:
        """Size of a price the top reports as present. If the level reads 0 at t on this clock (a
        change filling it was published earlier but arrives late), the last positive size known
        on this clock; NaN if none is known. Never 0 for a present price."""
        lv = self._levels.get((side, px))
        if lv is None:
            return float("nan")
        i = _search(self._times(lv, clock), t)
        if i < 0:
            return float("nan")
        j = int(lv.last_pos[i])
        return float(lv.size[j]) if j >= 0 else float("nan")

    # -- trades / ticks
    def trade(self, i: int) -> Trade:
        tr = self.trades
        if not 0 <= i < len(tr.ts_ms):
            raise IndexError(i)
        return Trade(int(tr.recv_ms[i]), int(tr.ts_ms[i]), TOKENS[tr.token[i]], SIDES[tr.side[i]],
                     float(tr.price[i]), float(tr.size[i]), SIDES[tr.up_side[i]], int(tr.up_px[i]))

    def tick_at(self, t: int, clock: Clock = "ts") -> int:
        tk = self.ticks if clock == "ts" else self._ticks_recv if clock == "recv" else None
        if tk is None:
            raise ValueError(f"unknown clock {clock!r}")
        i = _search(tk.ts_ms if clock == "ts" else tk.recv_ms, t)
        return int(tk.tick_px[i]) if i >= 0 else self.info.tick_px

    # -- construction
    @classmethod
    def from_frames(cls, info: MarketInfo, changes: pd.DataFrame, books: pd.DataFrame, trades: pd.DataFrame,
                    ticks: pd.DataFrame) -> MarketData:
        """Build from raw-shaped frames (view columns of pm_price_change / pm_book / pm_trade /
        pm_tick_size_change, with `outcome`). Same-ms rows apply in the frame's `seq` column
        order if it has one (as in the Parquet files), else in row order."""
        import duckdb

        from ..views import MACROS

        con = duckdb.connect()
        con.execute("SET TimeZone = 'UTC'")
        con.execute(MACROS)
        specs = {
            "_pc": (changes, ["ts_ms", "recv_ms", "outcome", "side", "price", "size", "best_bid", "best_ask"],
                    "BIGINT, BIGINT, VARCHAR, VARCHAR, DOUBLE, DOUBLE, DOUBLE, DOUBLE"),
            "_bk": (books, ["ts_ms", "recv_ms", "outcome", "bids", "asks"], "BIGINT, BIGINT, VARCHAR, VARCHAR, VARCHAR"),
            "_tr": (trades, ["ts_ms", "recv_ms", "outcome", "side", "price", "size"],
                    "BIGINT, BIGINT, VARCHAR, VARCHAR, DOUBLE, DOUBLE"),
            "_tk": (ticks, ["ts_ms", "recv_ms", "new_tick_size"], "BIGINT, BIGINT, DOUBLE"),
        }
        for name, (df, cols, types) in specs.items():
            seq = df["seq"].to_numpy(np.int64) if "seq" in df.columns else np.arange(len(df), dtype=np.int64)
            df = df[cols].reset_index(drop=True).assign(seq=seq)
            casts = ", ".join(f"CAST({c} AS {t.strip()}) AS {c}" for c, t in zip(cols, types.split(",")))
            con.register("_src", df)
            con.execute(f"CREATE TEMP TABLE {name} AS SELECT 0 AS mi, {casts}, seq FROM _src")
            if name == "_pc":
                con.execute(f"CREATE OR REPLACE TEMP TABLE _pc AS SELECT mi, ts_ms, recv_ms, seq, {_PC_SELECT} FROM _pc")
            con.unregister("_src")
        try:
            return cls(info, _build(con, 1)[0])
        finally:
            con.close()


# ---------------------------------------------------------------------------------------- loading

_MARKETS_SQL = """
SELECT slug, coin, timeframe, series, condition_id, window_start_ms, window_end_ms, price_to_beat,
       fee_rate, fee_exponent, tick_size_at_discovery, min_order_size, outcome,
       epoch_ms(resolution_seen_at) AS resolution_recv_ms
FROM markets
WHERE coin = ? AND list_contains(?, timeframe) AND window_start_ms < ? AND window_end_ms > ?
ORDER BY window_start_ms, slug
"""

LOOKBACK_WINDOWS = 2  # markets are subscribed up to ~2 windows before they open
MARGIN_MS = 600_000  # extra data loaded before the lookback and after the window end


def _none(x):
    return None if x is None or (isinstance(x, float) and np.isnan(x)) or x is pd.NA else x


def _info(r) -> MarketInfo:
    return MarketInfo(
        slug=r.slug, coin=r.coin, timeframe=r.timeframe, series=r.series, condition_id=r.condition_id,
        window_start_ms=int(r.window_start_ms), window_end_ms=int(r.window_end_ms),
        price_to_beat=_none(r.price_to_beat), fee_rate=float(r.fee_rate), fee_exponent=float(r.fee_exponent),
        tick_px=to_px(r.tick_size_at_discovery), min_order_size=float(r.min_order_size), outcome=_none(r.outcome),
        resolution_recv_ms=None if _none(r.resolution_recv_ms) is None else int(r.resolution_recv_ms))


def load_coin(con, coin: str, timeframes: list[str], start_ms: int, end_ms: int,
              slugs: set[str] | None = None) -> dict[str, MarketData]:
    """Every market of `coin` in `timeframes` whose window overlaps [start_ms, end_ms), with all its
    Polymarket data (from subscription, ~2 windows before the open, to the end of the recording).
    One DuckDB pass per stream for all the markets. Returns {slug: MarketData}."""
    for v in ("pm_price_change", "pm_book", "pm_trade"):
        if "seq" not in {r[0] for r in con.execute(f"DESCRIBE {v}").fetchall()}:
            raise RuntimeError(f"{v} has no `seq` column: the Parquet predates build format 2; run `polydata build --force`")
    mk = con.execute(_MARKETS_SQL, [coin, list(timeframes), end_ms, start_ms]).df()
    if slugs is not None:
        mk = mk[mk.slug.isin(slugs)]
    mk = mk.reset_index(drop=True)
    if mk.empty:
        return {}
    lo = int((mk.window_start_ms - LOOKBACK_WINDOWS * (mk.window_end_ms - mk.window_start_ms)).min()) - MARGIN_MS
    hi = int(mk.window_end_ms.max()) + MARGIN_MS
    d0, d1 = (datetime.fromtimestamp(ms / 1000, timezone.utc).date() for ms in (lo - 3_600_000, hi + 3_600_000))
    con.register("_mk_df", pd.DataFrame({"mi": np.arange(len(mk)), "slug": mk.slug}))
    con.execute("CREATE OR REPLACE TEMP TABLE _mk AS SELECT * FROM _mk_df")
    con.unregister("_mk_df")
    assets = [f"{s}|{o}" for s in mk.slug for o in ("U", "D")]
    where = "s.asset IN (SELECT unnest(?)) AND s.date BETWEEN ? AND ? AND s.ts_ms BETWEEN ? AND ?"
    params = [assets, d0, d1, lo, hi]
    con.execute(f"""CREATE OR REPLACE TEMP TABLE _pc AS
        SELECT m.mi, s.ts_ms, s.recv_ms, s.seq, {_PC_SELECT}
        FROM pm_price_change s JOIN _mk m USING (slug) WHERE {where}""", params)
    con.execute(f"""CREATE OR REPLACE TEMP TABLE _bk AS
        SELECT m.mi, s.ts_ms, s.recv_ms, s.seq, s.outcome, s.bids, s.asks
        FROM pm_book s JOIN _mk m USING (slug) WHERE {where}""", params)
    con.execute(f"""CREATE OR REPLACE TEMP TABLE _tr AS
        SELECT m.mi, s.ts_ms, s.recv_ms, s.seq,
               s.outcome, s.side, s.price, s.size
        FROM pm_trade s JOIN _mk m USING (slug) WHERE {where}""", params)
    has_ticks = "pm_tick_size_change" in {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    if has_ticks:
        con.register("_cid_df", pd.DataFrame({"mi": np.arange(len(mk)), "condition_id": mk.condition_id}))
        con.execute("""CREATE OR REPLACE TEMP TABLE _tk AS
            SELECT c.mi, s.ts_ms, s.recv_ms, s.new_tick_size FROM pm_tick_size_change s JOIN _cid_df c USING (condition_id)""")
        con.unregister("_cid_df")
    else:
        con.execute("CREATE OR REPLACE TEMP TABLE _tk (mi INTEGER, ts_ms BIGINT, recv_ms BIGINT, new_tick_size DOUBLE)")
    for t_, stream in (("_pc", "pm_price_change"), ("_bk", "pm_book"), ("_tr", "pm_trade")):
        if con.execute(f"SELECT count(*) FROM {t_} WHERE seq IS NULL").fetchall()[0][0]:
            raise RuntimeError(f"{stream}: rows without `seq` (hours built before build format 2); "
                               "rebuild them with `polydata build --force`")
    arrays = _build(con, len(mk))
    for t_ in ("_pc", "_bk", "_tr", "_tk", "_mk"):
        con.execute(f"DROP TABLE IF EXISTS {t_}")
    return {r.slug: MarketData(_info(r), arrays[i]) for i, r in enumerate(mk.itertuples())}


# ------------------------------------------------------------------------------------- validation

def reconstruction_rate(con, md: MarketData, n: int = 200, seed: int = 0, strict: bool = False) -> float:
    """Share of `n` random `pm_best_bid_ask` Up rows of the market whose best bid and ask match the
    reconstructed book (a missing side compares as bid 0 / ask 1000).

    `strict`: the row must equal the top of `book_at(ts_ms, "ts")`. Otherwise (default) a row also
    matches when it shows a state *within* that millisecond: the book before the same-`ts_ms`
    changes (`book_at(ts_ms - 1)`), or an intermediate top that the Up change rows of that `ts_ms`
    report in their best_bid/best_ask columns, provided our end-of-ms top is also one of those.
    `pm_best_bid_ask` is published per change, so a row can show the state halfway through a
    message that changes several levels at once.
    """
    slug = md.info.slug
    rows = con.execute(
        "SELECT ts_ms, best_bid, best_ask FROM pm_best_bid_ask WHERE asset = ? ORDER BY ts_ms, recv_ms, seq",
        [f"{slug}|U"]).df()
    if rows.empty:
        return float("nan")
    rows = rows.iloc[np.sort(np.random.default_rng(seed).choice(len(rows), size=min(n, len(rows)), replace=False))]
    reported: dict[int, set[tuple[int, int]]] = {}
    if not strict:
        for t, bb, ba in con.execute(
                f"""SELECT DISTINCT ts_ms, {_BID.format(b='best_bid')}, {_ASK.format(a='best_ask')} FROM pm_price_change
                    WHERE asset = ? AND list_contains(?, ts_ms)""", [f"{slug}|U", rows.ts_ms.astype(int).tolist()]).fetchall():
            reported.setdefault(int(t), set()).add((int(bb), int(ba)))

    def top(t: int) -> tuple[int, int]:
        bids, asks = md.book_at(t, "ts", depth=1)
        return (bids[0][0] if bids else NO_BID_PX, asks[0][0] if asks else NO_ASK_PX)

    ok = 0
    for r in rows.itertuples():
        t = int(r.ts_ms)
        want = (to_px(r.best_bid) if r.best_bid > 0 else NO_BID_PX, to_px(r.best_ask) if 0 < r.best_ask < 1 else NO_ASK_PX)
        got = top(t)
        if got == want:
            ok += 1
        elif not strict:
            rep = reported.get(t, set())
            ok += want == top(t - 1) or (want in rep and got in rep)
    return ok / len(rows)


__all__ = ["MarketData", "Tops", "Trades", "Ticks", "load_coin", "reconstruction_rate", "PX_SCALE"]

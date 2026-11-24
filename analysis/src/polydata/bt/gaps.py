"""Data gaps (dropped connections, silent feeds) and the market windows they void.

A dropped Polymarket connection leaves a stale book, which gives fills that could never have
happened. `gap_table` lists the gaps; `voided_markets` says which market windows must not be replayed.
"""

from __future__ import annotations

import logging
from collections.abc import Iterable
from datetime import date

import pandas as pd

from ..config import paths

log = logging.getLogger(__name__)

GAP_COLS = ["stream", "key", "from_ms", "to_ms", "source"]
EVENT_COLS = ["ts_ms", "kind", "source", "detail"]

# Logged incidents that affect replay: (stream, key, from_ms, to_ms, source). Restart truncations
# end at 2026-11-24 10:12:39, so REPLAY_START_MS already excludes all of them; add future ones here.
KNOWN_GAPS: list[tuple[str, str, int, int, str]] = []

_PM_STREAMS = ("pm_book", "pm_price_change", "pm_best_bid_ask", "pm_trade")


def _empty() -> pd.DataFrame:
    return pd.DataFrame({"stream": [], "key": [], "from_ms": [], "to_ms": [], "source": []}).astype(
        {"stream": object, "key": object, "from_ms": "int64", "to_ms": "int64", "source": object})


def load_rec_events(days: Iterable[date]) -> pd.DataFrame:
    """Read the daily `rec_events.csv.zst` catalog exports. Missing days are skipped with one warning."""
    import duckdb

    files, missing = [], []
    for d in days:
        f = paths().catalog / d.isoformat() / "rec_events.csv.zst"
        (files if f.exists() else missing).append(f.as_posix() if f.exists() else d.isoformat())
    if missing:
        log.warning("no rec_events catalog for %s; relying on Binance and Polymarket silences only", ", ".join(missing))
    if not files:
        return pd.DataFrame(columns=EVENT_COLS).astype({"ts_ms": "int64"})
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    return con.execute(
        "SELECT epoch_ms(ts::TIMESTAMPTZ)::BIGINT AS ts_ms, kind, source, detail "
        "FROM read_csv(?, header=true, columns={'ts':'VARCHAR','kind':'VARCHAR','source':'VARCHAR','detail':'VARCHAR'}) "
        "ORDER BY ts_ms", [files]).df()


def event_gaps(events: pd.DataFrame, end_ms: int, bn_syms: Iterable[str] = ()) -> pd.DataFrame:
    """Gaps from connection events: ws_close/ws_stale -> next ws_open of the same source, and
    recorder stop -> start (a gap for every stream). An unmatched start runs to `end_ms`."""
    syms = list(bn_syms)
    out: list[tuple] = []

    def emit(source: str, a: int, b: int) -> None:
        if source.startswith("pm:"):
            out.append(("pm", source[3:], a, b, source))
        elif source in ("polymarket", "polymarket-clob"):
            out.append(("pm", "*", a, b, source))
        elif source == "polymarket-rtds":
            out.append(("cl", "*", a, b, source))
        elif source == "binance-fut-public":
            out.extend(("bn", s, a, b, source) for s in syms)
        elif source == "recorder":
            out.append(("pm", "*", a, b, source))
            out.append(("cl", "*", a, b, source))
            out.extend(("bn", s, a, b, source) for s in syms)

    pending: dict[str, int] = {}
    for r in events.sort_values("ts_ms", kind="stable").itertuples():
        src, k, t = r.source, r.kind, int(r.ts_ms)
        if k in ("ws_close", "ws_stale"):
            pending.setdefault(src, t)  # stale then close: the gap starts at the first
        elif k == "ws_open" and src in pending:
            emit(src, pending.pop(src), t)
        elif src == "recorder" and k == "stop":
            pending.setdefault("recorder", t)
        elif src == "recorder" and k == "start" and "recorder" in pending:
            emit("recorder", pending.pop("recorder"), t)
    for src, a in pending.items():
        emit(src, a, max(end_ms, a))
    return pd.DataFrame(out, columns=GAP_COLS) if out else _empty()


def bn_silences(con, start_ms: int, end_ms: int, max_gap_ms_bn: int = 1000) -> pd.DataFrame:
    """Gaps between consecutive `bn_fut_bbo.recv_ms` per sym longer than `max_gap_ms_bn`."""
    return con.execute(
        """
        SELECT 'bn' AS stream, sym AS key, prev AS from_ms, recv_ms AS to_ms, 'bn_fut_bbo silence' AS source
        FROM (SELECT sym, recv_ms, lag(recv_ms) OVER (PARTITION BY sym ORDER BY recv_ms) AS prev
              FROM bn_fut_bbo WHERE recv_ms BETWEEN ? AND ?)
        WHERE recv_ms - prev > ?
        ORDER BY sym, from_ms""", [start_ms, end_ms, max_gap_ms_bn]).df()


def pm_silences(con, start_ms: int, end_ms: int, max_gap_ms_pm: int = 5000) -> pd.DataFrame:
    """Per market: no PM event of any kind for > `max_gap_ms_pm` inside its window while Data API
    trades for the same market happened in the silence. Keyed by the market's series."""
    names = {r[0] for r in con.execute("SHOW TABLES").fetchall()}
    if "markets" not in names or "pm_api_trades" not in names:
        return _empty()
    streams = [s for s in _PM_STREAMS if s in names]
    ev = " UNION ALL ".join(f"SELECT slug, recv_ms FROM {s} WHERE recv_ms BETWEEN {start_ms} AND {end_ms}"
                            for s in streams)
    ev = f"{ev} UNION ALL " if ev else ""
    return con.execute(
        f"""
        WITH m AS (SELECT slug, series, window_start_ms, window_end_ms FROM markets
                   WHERE window_end_ms > ? AND window_start_ms < ?),
        e AS ({ev}SELECT slug, window_start_ms AS recv_ms FROM m UNION ALL SELECT slug, window_end_ms FROM m),
        e2 AS (SELECT e.slug, e.recv_ms FROM e JOIN m USING (slug)
               WHERE e.recv_ms BETWEEN m.window_start_ms AND m.window_end_ms),
        s AS (SELECT slug, lag(recv_ms) OVER (PARTITION BY slug ORDER BY recv_ms) AS from_ms, recv_ms AS to_ms FROM e2)
        SELECT 'pm' AS stream, m.series AS key, s.from_ms, s.to_ms, 'pm silence' AS source
        FROM s JOIN m USING (slug)
        WHERE s.to_ms - s.from_ms > ?
          AND EXISTS (SELECT 1 FROM pm_api_trades t WHERE t.slug = s.slug AND t.ts_ms > s.from_ms AND t.ts_ms < s.to_ms)
        """, [start_ms, end_ms, max_gap_ms_pm]).df()


def gap_table(con, start_ms: int, end_ms: int, events: pd.DataFrame | None = None,
              max_gap_ms_pm: int = 5000, max_gap_ms_bn: int = 1000) -> pd.DataFrame:
    """All known gaps overlapping [start_ms, end_ms): columns stream ('pm'|'bn'|'cl'), key (series,
    '*', Binance sym or coin), from_ms, to_ms, source. `events` defaults to the daily catalog."""
    from datetime import datetime, timedelta, timezone

    if events is None:
        d0 = datetime.fromtimestamp(start_ms / 1000, timezone.utc).date()
        d1 = datetime.fromtimestamp(end_ms / 1000, timezone.utc).date()
        events = load_rec_events([d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)])
    syms = [r[0] for r in con.execute(
        "SELECT DISTINCT sym FROM bn_fut_bbo WHERE recv_ms BETWEEN ? AND ?", [start_ms, end_ms]).fetchall()]
    parts = [event_gaps(events, end_ms, syms), bn_silences(con, start_ms, end_ms, max_gap_ms_bn),
             pm_silences(con, start_ms, end_ms, max_gap_ms_pm),
             pd.DataFrame(KNOWN_GAPS, columns=GAP_COLS) if KNOWN_GAPS else _empty()]
    g = pd.concat([p for p in parts if len(p)], ignore_index=True) if any(len(p) for p in parts) else _empty()
    g = g[(g.from_ms < end_ms) & (g.to_ms > start_ms)]
    return g.sort_values(["stream", "key", "from_ms"], kind="stable").reset_index(drop=True)


def voided_markets(markets: pd.DataFrame, gaps: pd.DataFrame, max_gap_ms_pm: int = 5000,
                   max_gap_ms_bn: int = 1000) -> pd.DataFrame:
    """Markets that must not be replayed: a long enough gap overlaps [window_start_ms, window_end_ms),
    or the market has no outcome. Returns DataFrame[slug, reason]."""
    g = gaps.assign(dur=gaps.to_ms - gaps.from_ms) if len(gaps) else gaps.assign(dur=[])
    pm = g[(g.stream == "pm") & (g.dur > max_gap_ms_pm)]
    bn = g[(g.stream == "bn") & (g.dur > max_gap_ms_bn)]
    out = []
    for m in markets.itertuples():
        ws, we = m.window_start_ms, m.window_end_ms
        reasons = []
        if pd.isna(m.outcome):
            reasons.append("no outcome")
        sel = pm[(pm.key.isin([m.series, "*"])) & (pm.from_ms < we) & (pm.to_ms > ws)]
        if len(sel):
            reasons.append(f"pm gap {int(sel.dur.max())} ms")
        sym = f"{str(m.coin).upper()}USDT"
        sel = bn[(bn.key.isin([sym, "*"])) & (bn.from_ms < we) & (bn.to_ms > ws)]
        if len(sel):
            reasons.append(f"bn gap {int(sel.dur.max())} ms")
        if reasons:
            out.append((m.slug, "; ".join(reasons)))
    return pd.DataFrame(out, columns=["slug", "reason"])

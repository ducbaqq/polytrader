"""Post-hoc voids of a paper session (spec "Gaps"): `bt.gaps` runs over the session store after the run, so the strategy
traded every market as it saw it and only the accounting changes (`apply`: `pnl_raw` keeps the P&L, `pnl` is NaN).

`session_voids(session, markets, cfg, end_ms)` builds the session store if needed and gives `DataFrame[slug, reason]`:

- connection events, silences and stalls: `gaps.gap_table(con, rs, end_ms, events=<session catalog>)` and
  `gaps.voided_markets` with bt's thresholds (`cfg.max_gap_ms_pm` / `max_gap_ms_bn`); the catalog is read from the
  session directly (no `health` look). The recorder logs per-market connections as `pm:<slug>` (series gap keys are the
  series), which `voided_markets` does not match, so a `pm` gap keyed by a market's slug is also checked against that
  market only (a second `voided_markets` call over the markets with `series = slug`: never sibling markets);
- `no outcome` (`voided_markets`: the market's outcome is missing);
- `late data (n rows, max x ms)` from `late/`, **only Polymarket rows** (`pm_book`, `pm_price_change`,
  `pm_best_bid_ask`, `pm_trade`, `pm_other` tick-size messages; the slug from `asset`, or the condition id of the
  message). A late or future-stamped row counts against a market only if it was received at or after `open` with an exchange
  timestamp before the window end (`ts_ms < window_end`; `values[0]` / the message `timestamp`: with the hold-back, a
  row received just after the end can carry an in-window ts the ts-clock fill simulation never saw). `open` is the
  receive time of the market's first `pm_book` row at or after its admission (the manifest's `admitted_ms`; the engine
  opens at the chosen snapshot's recv, so this approximation can only over-void: rows in [first book recv, engine
  open) count): the strategy is not called before it opens, and the opening snapshot of a quiet book carries the
  book's last-change time (often older than the hold-back), so it arrives "late" without any data being missed. Rows
  received before the open, with a ts at or after the window end, or of a market that never opened (no snapshot) never void it. Rows of
  `bn_fut_bbo`, `bn_fut_trade` and `rtds_price` in `late/` never void (the router cannot produce them).
"""

from __future__ import annotations

import contextlib
import io
import json
import math
import os
from collections.abc import Iterator
from pathlib import Path

import duckdb
import pandas as pd

from polydata.bt import gaps as gaps_mod
from polydata.bt.config import RunConfig

PM_LATE_STREAMS = ("pm_book", "pm_price_change", "pm_best_bid_ask", "pm_trade", "pm_other")


@contextlib.contextmanager
def _env_dir(root: Path) -> Iterator[None]:
    old = os.environ.get("POLYDATA_DIR")
    os.environ["POLYDATA_DIR"] = str(root)
    try:
        yield
    finally:
        if old is None:
            os.environ.pop("POLYDATA_DIR", None)
        else:
            os.environ["POLYDATA_DIR"] = old


def _catalog_events(session: Path) -> pd.DataFrame:
    """The session's `rec_events` (every day's catalog file), as `gaps.load_rec_events` reads the daily exports."""
    files = sorted((session / "catalog").glob("*/rec_events.csv.zst"))
    if not files:
        return pd.DataFrame(columns=gaps_mod.EVENT_COLS).astype({"ts_ms": "int64"})
    con = duckdb.connect()
    con.execute("SET TimeZone='UTC'")
    cols = "{'ts':'VARCHAR','kind':'VARCHAR','source':'VARCHAR','detail':'VARCHAR'}"
    return con.execute(
        "SELECT epoch_ms(ts::TIMESTAMPTZ)::BIGINT AS ts_ms, kind, source, detail "
        f"FROM read_csv(?, header=true, columns={cols}) "
        "ORDER BY ts_ms", [[f.as_posix() for f in files]]).df()


def _late_rows(session: Path, cond_slug: dict[str, str]) -> pd.DataFrame:
    """Polymarket rows of `late/`: DataFrame[slug, recv_ms, late_by_ms]."""
    parts = []
    con = duckdb.connect()
    for stream in PM_LATE_STREAMS:
        f = session / "late" / f"{stream}.tsv.zst"
        if not f.exists():
            continue
        df = con.execute("SELECT * FROM read_csv(?, delim='\t', header=true, quote='', escape='', all_varchar=true)",
                         [f.as_posix()]).df()
        if not len(df):
            continue
        if stream == "pm_other":
            slugs, tss = [], []
            for d in df["d"]:
                try:
                    msg = json.loads(d)
                    tick = msg.get("event_type") == "tick_size_change"
                    slug = cond_slug.get(msg.get("market")) if tick else None
                    ts = int(msg.get("timestamp"))
                except (TypeError, ValueError, AttributeError):
                    slug, ts = None, 0  # no usable timestamp: the row cannot be placed against a window
                slugs.append(slug)
                tss.append(ts)
        else:
            slugs = [str(a).split("|", 1)[0] for a in df["asset"]]
            tss = df["ts_ms"].astype("int64").tolist()
        parts.append(pd.DataFrame({"slug": slugs, "ts_ms": tss, "recv_ms": df["recv_ms"].astype("int64"),
                                   "late_by_ms": df["late_by_ms"].astype("int64")}))
    if not parts:
        return pd.DataFrame({"slug": [], "ts_ms": [], "recv_ms": [], "late_by_ms": []})
    out = pd.concat(parts, ignore_index=True)
    return out[out.slug.notna()].reset_index(drop=True)


def session_voids(session: Path, markets: pd.DataFrame, cfg: RunConfig, end_ms: int) -> pd.DataFrame:
    """Voided markets of the session (module docstring). `markets`: the run's markets (`slug`, `coin`,
    `window_start_ms`, `window_end_ms`, `outcome`)."""
    from polydata.build import build
    from polydata.views import create_views

    session = Path(session)
    manifest = json.loads((session / "manifest.json").read_text())
    rs = int(manifest["rs"])
    admitted = {a["slug"]: int(a["admitted_ms"]) for a in manifest.get("admitted", [])}
    if not len(markets):
        return pd.DataFrame(columns=["slug", "reason"])
    with _env_dir(session):
        with contextlib.redirect_stdout(io.StringIO()):
            build()
        con = duckdb.connect()
        con.execute("SET TimeZone='UTC'")
        names = create_views(con, parquet=session / "parquet", include_sealed=True)  # the run's own session store
        info = (con.execute("SELECT slug, series, condition_id FROM markets").df() if "markets" in names
                else pd.DataFrame({"slug": [], "series": [], "condition_id": []}))
        mk = markets[["slug", "coin", "window_start_ms", "window_end_ms", "outcome"]].copy()
        mk["series"] = mk["slug"].map(dict(zip(info.slug, info.series)))
        mk["series"] = [s if isinstance(s, str) else f"{c}-up-or-down-{slug.split('-')[2]}"
                        for s, c, slug in zip(mk.series, mk.coin, mk.slug)]
        mk["outcome"] = mk["outcome"].where(mk["outcome"].notna(), None)
        gaps = gaps_mod.gap_table(con, rs, int(end_ms), events=_catalog_events(session),
                                  max_gap_ms_pm=cfg.max_gap_ms_pm, max_gap_ms_bn=cfg.max_gap_ms_bn)
        opens: dict[str, int] = {}
        if "pm_book" in names and admitted:
            adm = pd.DataFrame({"slug": list(admitted), "adm": list(admitted.values())})
            con.register("_adm", adm)
            opens = dict(con.execute(
                "SELECT b.slug, min(b.recv_ms) FROM pm_book b JOIN _adm a ON a.slug = b.slug AND b.recv_ms >= a.adm "
                "GROUP BY b.slug").fetchall())
    base = gaps_mod.voided_markets(mk, gaps, cfg.max_gap_ms_pm, cfg.max_gap_ms_bn)
    reasons: dict[str, list[str]] = {s: r.split("; ") for s, r in zip(base.slug, base.reason)}
    # TODO(Task 13): goes away when gaps.py matches `pm:<slug>` keys itself, so pm gaps are never counted twice.
    # a pm gap keyed by a market slug (`pm:<slug>` connections) voids that market only
    pm_slug = gaps[(gaps.stream == "pm") & gaps.key.isin(set(mk.slug))] if len(gaps) else gaps
    if len(pm_slug):
        by_slug = gaps_mod.voided_markets(mk.assign(series=mk.slug), pm_slug, cfg.max_gap_ms_pm, cfg.max_gap_ms_bn)
        for s, r in zip(by_slug.slug, by_slug.reason):
            for x in r.split("; "):
                if x.startswith("pm gap") and not any(y.startswith("pm gap") for y in reasons.get(s, [])):
                    reasons.setdefault(s, []).append(x)
    late = _late_rows(session, dict(zip(info.condition_id, info.slug)))
    if len(late):
        we = dict(zip(mk.slug, mk.window_end_ms))
        for slug, g in late.groupby("slug"):
            o = opens.get(slug)
            if o is None or slug not in we:
                continue
            g = g[(g.recv_ms >= o) & (g.ts_ms < we[slug])]
            if len(g):
                reasons.setdefault(slug, []).append(f"late data ({len(g)} rows, max {int(g.late_by_ms.max())} ms)")
    order = {s: i for i, s in enumerate(mk.slug)}
    out = [(s, "; ".join(r)) for s, r in sorted(reasons.items(), key=lambda kv: order.get(kv[0], 1 << 30))]
    return pd.DataFrame(out, columns=["slug", "reason"])


def apply(markets: pd.DataFrame, voids: pd.DataFrame) -> pd.DataFrame:
    """`pnl_raw = pnl`, then for voided slugs `voided = True`, `reason` (joined to a reason the core already set) and
    `pnl = NaN`."""
    mk = markets.copy()
    mk["pnl_raw"] = mk["pnl"].astype(float) if len(mk) else pd.Series(dtype=float)
    if voids is None or not len(voids):
        return mk
    why = dict(zip(voids.slug, voids.reason))
    hit = mk["slug"].isin(why)
    mk["voided"] = mk["voided"].astype(bool) | hit
    old = mk["reason"].where(mk["reason"].notna(), "").astype(str)
    new = pd.Series([why.get(s, "") for s in mk["slug"]], index=mk.index)
    mk["reason"] = [n if not o else (o if (not n or n in o) else f"{o}; {n}") for o, n in zip(old, new)]
    mk.loc[hit, "pnl"] = math.nan
    return mk


__all__ = ["PM_LATE_STREAMS", "apply", "session_voids"]

"""`polydata paper replay RUN_DIR`: the parity check (spec "Parity check").

`replay(run_dir)` builds `session/parquet` (`build` with `POLYDATA_DIR` = the session), runs `engine.run_session` over
it with the run's config (`config.yaml` without its `paper:` block), the manifest's range and admitted markets, and
tracing on; the replay's markets get the paper run's post-processing (`results.paper_markets`: `voids.session_voids`
over the session, `voids.apply`, `results.gamma_columns` with `load_gamma_answers` / `load_outcome_sources`,
`admitted_ms` from the manifest). `compare` writes the replay's outputs to `parity/` and `parity.json`, and sets
`results.json["parity"]`.

**Definition (exact, NaN = NaN, per budget in row order):**

| table | compared |
|---|---|
| `decisions` | every column of `DECISION_COLS` + `budget` |
| `orders`, `fills`, `equity` | every column (bt's schemas + `budget`) |
| `markets` | `MARKET_COLS` + `budget` + `PAPER_MARKET_COLS` (`pnl_raw`, `outcome_source`, `gamma_outcome`, ...) |
| `log` | the strategy's lines of `log.txt` (lines not starting with `#`) |
| `results` | `results.json` fields derived again from the replay and the session files (`RESULT_KEYS`) |

`results.json` fields left out of `results`, and why: `rows`, `late`, `future`, `late_rows`, `router` (router
counters: the replay does not ingest rows), `processing_lag_ms`, `loop_lag_ms`, `feed_delay_ms`, `connections`,
`warnings`, `feed_failures`, `settler_gave_up` (live process state), `api_trades.markets` (a complete download of zero
trades leaves no row in the session), `errors` (runner errors; outcome mismatches are compared through
`outcome_mismatches`). `gamma_answers` / `gamma_fallbacks` / `resolutions` come from the Confirmer's state in the run
and are compared with their session-file derivation (`paper/gamma_resolution.jsonl.zst`, the first `pm_resolution`
row's `source` per market), skipped when the run had no Confirmer (`null`). `admitted_ms` and `admitted` come from
the router in the run and from the manifest in the replay (the manifest is the router's list).

`pnl` is NaN for a voided market on both sides (the replay's markets are voided by the same rules), so `pnl_raw`
carries the P&L of voided markets.

**Truncated sessions** (`clean_stop: false`): decisions with `now < cut` and markets settled before `cut` (`settle_ms <
cut`, as in the run), `cut = manifest.flushed_ms + 1` (the strategy clock reached `flushed_ms`; older manifests: the
run's last decision `now` + 1); other tables are reported, not judged. Verdict `passed-truncated`.

**Another host** (`provenance.host` differs from this host's `runs.host_hash()`): libm may differ in the last bit;
verdict `informational` (never a pass), with the mismatches that remain under a 1e-9 relative tolerance counted.

**Checks** (reported, not judged): `cl_rereceipt_gt_60s`: Chainlink `(symbol, ts_ms)` received again more than 60 s
after its first receipt (the live router deduplicates globally, the replay's `_load_cl` over a 60 s lookback).

`--vs-recorder` (`vs_recorder`): a normal `engine` replay of the same window `[rs, end_ms)` and days (whatever the
config's `day_kinds`) over the default store (sealed days hidden, the recorder's gaps), traced, against the paper run:
decision, fill and P&L differences in `parity/vs_recorder.json`; never a pass or fail (fidelity, not parity).
`explained` attributes decision differences to markets that opened at another time (`explain_differences`): on
2026-11-24 every difference comes from the hold-back dropping a quiet book's opening snapshot (its `timestamp` is the
book's last change, ~60 s old on bnb/doge/hype/xrp/zec), so the paper market opens at a later snapshot or never.
"""

from __future__ import annotations

import contextlib
import io
import json
import math
import time
from collections import Counter
from pathlib import Path

import duckdb
import numpy as np
import pandas as pd
import yaml

from polydata.bt import runs
from polydata.bt.config import RunConfig
from polydata.bt.core import MARKET_COLS
from polydata.bt.engine import RunResult, SessionPlan, run_session

TABLES = ["decisions", "orders", "fills", "equity", "markets", "log", "results"]
PAPER_MARKET_COLS = ["pnl_raw", "outcome_source", "gamma_outcome", "gamma_seen_ms", "outcome_mismatch", "pnl_gamma",
                     "admitted_ms"]
MARKET_CMP = MARKET_COLS + ["budget"] + PAPER_MARKET_COLS
RESULT_KEYS = ["pnl_usd", "voided", "outcome_mismatches", "pnl_usd_gamma", "admitted", "unresolved", "resolutions",
               "gamma_answers", "gamma_fallbacks", "api_trades.rows", "void_reasons", "voided_with_orders",
               "never_opened"]
TOL = 1e-9
BEFORE_N = 10


# ------------------------------------------------------------------------------------------- loading

def load_run(run_dir: Path) -> tuple[RunConfig, dict]:
    d = yaml.safe_load((Path(run_dir) / "config.yaml").read_text()) or {}
    d.pop("paper", None)
    man = json.loads((Path(run_dir) / "session" / "manifest.json").read_text())
    return RunConfig.from_dict(d), man


def session_con_factory(session: Path):
    def f():
        from polydata.views import create_views

        con = duckdb.connect()
        con.execute("SET TimeZone = 'UTC'")
        create_views(con, parquet=Path(session) / "parquet", include_sealed=True)  # the run's own session store
        return con

    return f


def build_session(session: Path, quiet: bool = True) -> None:
    from polydata.build import build
    from polydata.paper.voids import _env_dir

    with _env_dir(Path(session)):
        if quiet:
            with contextlib.redirect_stdout(io.StringIO()):
                build()
        else:
            build()


def replay(run_dir: Path) -> RunResult:
    """The session-mode replay of a paper run (builds `session/parquet` first)."""
    run_dir = Path(run_dir)
    cfg, man = load_run(run_dir)
    sess = run_dir / "session"
    build_session(sess)
    adm = [(a["slug"], int(a["admitted_ms"])) for a in man["admitted"]]
    plan = SessionPlan(int(man["rs"]), int(man["end_ms"]), adm)
    return run_session(cfg, session_con_factory(sess), plan, trace=True)


def replay_markets(run_dir: Path, rep: RunResult) -> tuple[pd.DataFrame, list[str] | None]:
    """The replay's markets after the paper run's post-processing, and the never-opened markets."""
    from polydata.paper import voids as voids_mod
    from polydata.paper.results import paper_markets

    run_dir = Path(run_dir)
    _, man = load_run(run_dir)
    sess = run_dir / "session"
    v = voids_mod.session_voids(sess, rep.markets, rep.cfg, int(man["end_ms"]))
    mk = paper_markets(rep, man["admitted"], v, sess)
    return mk, list(v.attrs.get("never_opened", []))


def derived_results(mk: pd.DataFrame, orders: pd.DataFrame, session: Path, man: dict,
                    never_opened: list[str] | None) -> dict:
    """`results.json` fields (`RESULT_KEYS`) from the replay's markets/orders and the session files."""
    from polydata.paper.results import load_gamma_answers, load_outcome_sources, voided_with_orders

    adm = [a["slug"] for a in man["admitted"]]
    sources = load_outcome_sources(session)
    answers = load_gamma_answers(session)
    res_src = Counter(sources[s] for s in adm if s in sources)
    voided = mk["voided"].astype(bool) if len(mk) else pd.Series(dtype=bool)
    api_rows = 0
    con = duckdb.connect()
    for f in sorted((Path(session) / "raw").rglob("pm_api_trades.*.jsonl.zst")):
        api_rows += con.execute("SELECT count(*) FROM read_json_auto(?, format='newline_delimited')",
                                [f.as_posix()]).fetchall()[0][0]
    return {
        "pnl_usd": float(mk["pnl"].astype(float).fillna(0).sum()) if len(mk) else 0.0,
        "voided": int(voided.sum()),
        "outcome_mismatches": int(mk["outcome_mismatch"].sum()) if len(mk) else 0,
        "pnl_usd_gamma": float(mk["pnl_gamma"].where(~voided, 0.0).fillna(0).sum()) if len(mk) else 0.0,
        "admitted": len(adm),
        "unresolved": len([s for s in adm if s not in sources]),
        "resolutions": dict(res_src),
        "gamma_answers": len(answers),
        "gamma_fallbacks": int(sum(1 for s in adm if sources.get(s) == "paper-gamma")),
        "api_trades.rows": int(api_rows),
        "void_reasons": dict(zip(mk.slug[voided], mk.reason[voided])) if len(mk) else {},
        "voided_with_orders": voided_with_orders(mk, orders),
        "never_opened": never_opened,
    }


def _get(d: dict, key: str):
    for k in key.split("."):
        if not isinstance(d, dict):
            return None
        d = d.get(k)
    return d


# ------------------------------------------------------------------------------------------- comparison

def _col_eq(a: pd.Series, b: pd.Series, tol: float | None = None) -> np.ndarray:
    """Element-wise equality with NaN/None/NA equal to each other; `tol`: relative tolerance for numbers."""
    an, bn = a.isna().to_numpy(), b.isna().to_numpy()
    num = (pd.api.types.is_numeric_dtype(a) and pd.api.types.is_numeric_dtype(b)
           and not pd.api.types.is_bool_dtype(a) and not pd.api.types.is_bool_dtype(b))
    if num:
        x, y = a.to_numpy(dtype=float, na_value=np.nan), b.to_numpy(dtype=float, na_value=np.nan)
        with np.errstate(invalid="ignore"):
            eq = x == y
            if tol is not None:
                eq |= np.abs(x - y) <= tol * np.maximum(np.abs(x), np.abs(y))
    else:
        x = a.astype(object).to_numpy().copy()
        y = b.astype(object).to_numpy().copy()
        x[an] = y[bn] = "\x00NA"
        eq = np.asarray(x == y, dtype=bool)
    return (an & bn) | (~an & ~bn & eq)


def _rows_eq(a: pd.DataFrame, b: pd.DataFrame, cols: list[str], tol: float | None = None) -> np.ndarray:
    n = min(len(a), len(b))
    ok = np.ones(n, dtype=bool)
    for c in cols:
        if c not in a.columns or c not in b.columns:
            return np.zeros(n, dtype=bool)
        ok &= _col_eq(a[c].iloc[:n].reset_index(drop=True), b[c].iloc[:n].reset_index(drop=True), tol)
    return ok


def _jsonable(row: pd.Series | dict | None):
    if row is None:
        return None
    d = row.to_dict() if isinstance(row, pd.Series) else dict(row)
    out = {}
    for k, v in d.items():
        if hasattr(v, "item") and not isinstance(v, (str, bytes)):
            v = v.item()
        if isinstance(v, float) and not math.isfinite(v):
            v = None
        if v is pd.NA or v is pd.NaT:
            v = None
        out[str(k)] = v
    return out


def _table(name: str, a: pd.DataFrame, b: pd.DataFrame, cols: list[str], tol: float | None) -> tuple[dict, dict | None]:
    """Per budget, row by row: {rows_paper, rows_replay, mismatches}, and the first divergence."""
    mism = 0
    first = None
    keys = list(dict.fromkeys(list(a["budget"].astype(str)) + list(b["budget"].astype(str)))) \
        if "budget" in a.columns and "budget" in b.columns else [None]
    for k in keys:
        x = a[a["budget"].astype(str) == k].reset_index(drop=True) if k is not None else a.reset_index(drop=True)
        y = b[b["budget"].astype(str) == k].reset_index(drop=True) if k is not None else b.reset_index(drop=True)
        ok = _rows_eq(x, y, cols, tol)
        bad = np.flatnonzero(~ok)
        mism += len(bad) + abs(len(x) - len(y))
        idx = int(bad[0]) if len(bad) else (min(len(x), len(y)) if len(x) != len(y) else None)
        if idx is not None and first is None:
            first = {"table": name, "budget": k, "index": idx,
                     "paper": _jsonable(x.iloc[idx]) if idx < len(x) else None,
                     "replay": _jsonable(y.iloc[idx]) if idx < len(y) else None}
    return {"rows_paper": int(len(a)), "rows_replay": int(len(b)), "mismatches": int(mism)}, first


def _time_of(row: dict | None, name: str) -> int | None:
    if not row:
        return None
    for c in {"decisions": "now", "orders": "ms", "fills": "report_ms", "equity": "ms",
              "markets": "window_end_ms"}.get(name, "").split():
        v = row.get(c)
        if v is not None:
            return int(v)
    return None


def _before(dec: pd.DataFrame, first: dict) -> list[dict]:
    """The 10 paper decisions of the budget before the divergence (by index for decisions, by time otherwise)."""
    if dec is None or not len(dec):
        return []
    x = dec[dec["budget"].astype(str) == str(first["budget"])].reset_index(drop=True) \
        if first.get("budget") is not None and "budget" in dec.columns else dec.reset_index(drop=True)
    if first["table"] == "decisions":
        i = int(first["index"])
    else:
        t = _time_of(first.get("paper") or first.get("replay"), first["table"])
        if t is None:
            return []
        i = int(np.searchsorted(x["now"].to_numpy(), t, side="right"))
    return [_jsonable(r) for _, r in x.iloc[max(0, i - BEFORE_N):i].iterrows()]


def _read(d: Path, name: str) -> pd.DataFrame:
    f = d / f"{name}.parquet"
    return pd.read_parquet(f) if f.exists() else pd.DataFrame()


def compare(run_dir: Path, replay: RunResult, *, host: str | None = None) -> dict:
    """Write the replay's outputs to `run_dir/parity/` and compare them with the run's files (module docstring).
    Returns the `parity.json` content (also written)."""
    run_dir = Path(run_dir)
    t0 = time.perf_counter()
    sess = run_dir / "session"
    _, man = load_run(run_dir)
    out = run_dir / "parity"
    out.mkdir(exist_ok=True)
    mk, never = replay_markets(run_dir, replay)
    replay.decisions.to_parquet(out / "decisions.parquet", index=False)
    replay.orders.to_parquet(out / "orders.parquet", index=False)
    replay.fills.to_parquet(out / "fills.parquet", index=False)
    replay.equity.to_parquet(out / "equity.parquet", index=False)
    mk.to_parquet(out / "markets.parquet", index=False)
    rep_log = "\n".join(map(str, replay.log))
    (out / "log.txt").write_text(rep_log + ("\n" if rep_log else ""))

    prov_f = run_dir / "provenance.json"
    prov = json.loads(prov_f.read_text()) if prov_f.exists() else {}
    here = host or runs.host_hash()
    other_host = prov.get("host") is not None and prov.get("host") != here
    clean = bool(man.get("clean_stop"))
    cut = None
    if not clean:
        if man.get("flushed_ms") is not None:
            cut = int(man["flushed_ms"]) + 1
        else:
            dp = _read(run_dir, "decisions")
            cut = int(dp["now"].max()) + 1 if len(dp) else int(man["rs"])

    tables: dict[str, dict] = {}
    firsts: list[dict] = []
    tol_counts: dict[str, int] = {}
    dec_paper = _read(run_dir, "decisions")
    for name in ("decisions", "orders", "fills", "equity", "markets"):
        a, b = _read(run_dir, name), _read(out, name)
        if name == "markets":
            cols = list(MARKET_CMP)
        else:
            cols = list(dict.fromkeys(list(a.columns) + list(b.columns)))
        judged = True
        if cut is not None:
            if name == "decisions":
                a = a[a["now"] < cut] if len(a) else a
                b = b[b["now"] < cut] if len(b) else b
            elif name == "markets":
                a = a[a["settle_ms"].notna() & (a["settle_ms"] < cut)] if len(a) else a
                b = b[b["slug"].isin(set(a["slug"]))] if len(b) else b
            else:
                judged = False
        if not len(a) and not len(b):
            tables[name] = {"rows_paper": 0, "rows_replay": 0, "mismatches": 0}
            continue
        if "budget" not in a.columns:
            a = a.assign(budget="")
        if "budget" not in b.columns:
            b = b.assign(budget="")
        t, first = _table(name, a, b, cols, None)
        if other_host:
            t2, _ = _table(name, a, b, cols, TOL)
            tol_counts[name] = t2["mismatches"]
        if not judged:
            t["judged"] = False
            first = None
        tables[name] = t
        if first is not None:
            firsts.append(first)

    paper_lines = [x for x in (run_dir / "log.txt").read_text().splitlines() if not x.startswith("#")] \
        if (run_dir / "log.txt").exists() else []
    rep_lines = [x for x in rep_log.splitlines() if not x.startswith("#")]
    n = min(len(paper_lines), len(rep_lines))
    bad = [i for i in range(n) if paper_lines[i] != rep_lines[i]]
    lm = len(bad) + abs(len(paper_lines) - len(rep_lines))
    tables["log"] = {"rows_paper": len(paper_lines), "rows_replay": len(rep_lines), "mismatches": lm}
    if cut is not None:
        tables["log"]["judged"] = False
    elif lm:
        i = bad[0] if bad else n
        firsts.append({"table": "log", "budget": None, "index": i,
                       "paper": paper_lines[i] if i < len(paper_lines) else None,
                       "replay": rep_lines[i] if i < len(rep_lines) else None})

    res_f = run_dir / "results.json"
    paper_res = json.loads(res_f.read_text()) if res_f.exists() else {}
    derived = derived_results(mk, _read(out, "orders"), sess, man, never)
    diffs = {}
    for k in RESULT_KEYS:
        pv = _get(paper_res, k)
        if pv is None and k in ("gamma_answers", "gamma_fallbacks", "never_opened"):
            continue  # the run had no Confirmer / no voids
        rv = derived[k]
        if k == "resolutions":
            pv = {s: n for s, n in (pv or {}).items() if n}
        same = (math.isclose(pv, rv, rel_tol=0, abs_tol=1e-9) if isinstance(pv, float) and isinstance(rv, float)
                else json.loads(json.dumps(pv)) == json.loads(json.dumps(rv)))
        if not same:
            diffs[k] = {"paper": pv, "replay": rv}
    tables["results"] = {"rows_paper": len(RESULT_KEYS), "rows_replay": len(RESULT_KEYS), "mismatches": len(diffs),
                         "differences": diffs}
    if cut is not None:
        tables["results"]["judged"] = False

    failed = any(t["mismatches"] and t.get("judged", True) for t in tables.values())
    if other_host:
        verdict = "informational"
    elif failed:
        verdict = "failed"
    else:
        verdict = "passed" if cut is None else "passed-truncated"
    first = None
    if firsts:
        order = {n: i for i, n in enumerate(TABLES)}
        first = sorted(firsts, key=lambda f: order[f["table"]])[0]
        first["before"] = _before(dec_paper, first) if first["table"] != "results" else []
    out_d = {
        "passed": verdict in ("passed", "passed-truncated"),
        "verdict": verdict,
        "tables": tables,
        "first_divergence": first,
        "cut_ms": cut,
        "clean_stop": clean,
        "host": {"run": prov.get("host"), "replay": here},
        "tolerance_mismatches": tol_counts if other_host else None,
        "checks": checks(sess),
        "excluded": ["rows", "late", "future", "late_rows", "router", "processing_lag_ms", "loop_lag_ms",
                     "feed_delay_ms", "connections", "warnings", "feed_failures", "settler_gave_up",
                     "api_trades.markets", "errors"],
        "replay_elapsed_s": round(float(replay.elapsed_s), 1),
        "compare_elapsed_s": round(time.perf_counter() - t0, 1),
    }
    out_d = json.loads(json.dumps(out_d, default=_default))
    (run_dir / "parity.json").write_text(json.dumps(out_d, indent=1, sort_keys=True) + "\n")
    if res_f.exists():
        paper_res["parity"] = {"passed": out_d["passed"], "verdict": verdict,
                               "mismatches": {k: v["mismatches"] for k, v in tables.items()}}
        res_f.write_text(json.dumps(paper_res, indent=1, sort_keys=True, allow_nan=False) + "\n")
    return out_d


def _default(x):
    if hasattr(x, "item"):
        return x.item()
    return str(x)


def checks(session: Path) -> dict:
    """Reported, never judged (module docstring)."""
    out = {}
    pq = Path(session) / "parquet" / "rtds_price"
    if list(pq.glob("*/*.parquet")):
        con = duckdb.connect()
        out["cl_rereceipt_gt_60s"] = int(con.execute(
            "SELECT count(*) FROM (SELECT symbol, ts_ms, max(recv_ms) - min(recv_ms) AS span "
            f"FROM read_parquet('{pq.as_posix()}/*/*.parquet') WHERE topic = 'crypto_prices_chainlink' "
            "GROUP BY 1, 2) WHERE span > 60000").fetchall()[0][0])
    return out


# ------------------------------------------------------------------------------------------- vs recorder

def recorder_run(cfg: RunConfig, rs: int, end_ms: int, con_factory=None) -> RunResult:
    """A normal replay over the default store, clipped to [rs, end_ms), traced (one budget after the other)."""
    from dataclasses import replace
    from datetime import datetime, timedelta, timezone

    from polydata.bt import engine
    from polydata.bt.splits import day_kind

    con_factory = con_factory or engine.default_con_factory
    # the window's own days, whatever the config's `days` / `day_kinds` (a validation config run on a train day);
    # sealed days stay hidden by the default connection
    d0 = datetime.fromtimestamp(int(rs) / 1000, timezone.utc).date()
    d1 = datetime.fromtimestamp((int(end_ms) - 1) / 1000, timezone.utc).date()
    days = [d0 + timedelta(days=i) for i in range((d1 - d0).days + 1)]
    cfg = replace(cfg, days=[d.isoformat() for d in days], day_kinds=sorted({day_kind(d) for d in days}))
    t0 = time.perf_counter()
    clip = (int(rs), int(end_ms))
    if cfg.capital_mode == "global":
        out = {"*": engine._replay(cfg, list(cfg.coins), {"*": cfg.capital}, con_factory, clip=clip, trace=True)}
    else:
        b = cfg.capital / len(cfg.coins)
        out = {c: engine.run_coin(cfg, c, con_factory, budget=b, clip=clip, trace=True) for c in cfg.coins}
    return RunResult(cfg, out, time.perf_counter() - t0)


DEC_KEY = ["now", "call", "key", "token", "side", "price", "size", "ret", "flag"]


def _keyed(df: pd.DataFrame, cols: list[str]) -> Counter:
    if not len(df):
        return Counter()
    x = df[cols].astype(object).where(df[cols].notna(), None)
    return Counter(map(tuple, x.itertuples(index=False, name=None)))


def vs_recorder(run_dir: Path, rec: RunResult) -> dict:
    """Differences between the paper run and a recorder replay of its window (never pass/fail)."""
    run_dir = Path(run_dir)
    dp, dr = _read(run_dir, "decisions"), rec.decisions
    out: dict = {"decisions": {}, "fills": {}, "markets": {}}
    tot = 0
    first = None
    for k in sorted(set(dp.get("budget", pd.Series(dtype=str)).astype(str)) | set(dr.get("budget", pd.Series(
            dtype=str)).astype(str))):
        x = dp[dp["budget"].astype(str) == k].reset_index(drop=True) if len(dp) else dp
        y = dr[dr["budget"].astype(str) == k].reset_index(drop=True) if len(dr) else dr
        ca, cb = _keyed(x, DEC_KEY), _keyed(y, DEC_KEY)
        only_p, only_r = sum((ca - cb).values()), sum((cb - ca).values())
        ok = _rows_eq(x, y, DEC_KEY)
        bad = np.flatnonzero(~ok)
        idx = int(bad[0]) if len(bad) else (min(len(x), len(y)) if len(x) != len(y) else None)
        out["decisions"][k] = {"paper": int(len(x)), "recorder": int(len(y)), "only_paper": int(only_p),
                               "only_recorder": int(only_r), "first_index": idx}
        tot += only_p + only_r
        if idx is not None and first is None:
            first = {"budget": k, "index": idx, "paper": _jsonable(x.iloc[idx]) if idx < len(x) else None,
                     "recorder": _jsonable(y.iloc[idx]) if idx < len(y) else None,
                     "before": [_jsonable(r) for _, r in x.iloc[max(0, idx - BEFORE_N):idx].iterrows()]}
    out["decision_differences"] = int(tot)
    out["first_difference"] = first
    out["explained"] = explain_differences(run_dir, dp, _read(run_dir, "orders"), dr, rec.orders)
    fp, fr = _read(run_dir, "fills"), rec.fills
    fcols = [c for c in ("budget", "slug", "token", "side", "ts_ms", "report_ms", "px", "size", "fee", "liquidity")
             if c in fp.columns and c in fr.columns]
    ca, cb = _keyed(fp, fcols), _keyed(fr, fcols)
    out["fills"] = {"paper": int(len(fp)), "recorder": int(len(fr)), "only_paper": int(sum((ca - cb).values())),
                    "only_recorder": int(sum((cb - ca).values()))}
    mp, mr = _read(run_dir, "markets"), rec.markets
    pp = dict(zip(mp.slug, mp.pnl_raw)) if len(mp) else {}
    pr = dict(zip(mr.slug, mr.pnl)) if len(mr) else {}
    diff = {s: {"paper": _num(pp.get(s)), "recorder": _num(pr.get(s))} for s in sorted(set(pp) | set(pr))
            if not _same(pp.get(s), pr.get(s))}
    out["markets"] = {"paper": len(pp), "recorder": len(pr), "only_paper": sorted(set(pp) - set(pr)),
                      "only_recorder": sorted(set(pr) - set(pp)), "pnl_differences": diff,
                      "recorder_voided": dict(zip(mr.slug[mr.voided.astype(bool)], mr.reason[mr.voided.astype(bool)]))
                      if len(mr) else {}}
    out["pnl_usd"] = {"paper_raw": float(np.nansum(mp.pnl_raw)) if len(mp) else 0.0,
                      "recorder": float(np.nansum(mr.pnl.astype(float))) if len(mr) else 0.0}
    out["recorder_elapsed_s"] = round(float(rec.elapsed_s), 1)
    out = json.loads(json.dumps(out, default=_default))
    (run_dir / "parity").mkdir(exist_ok=True)
    (run_dir / "parity" / "vs_recorder.json").write_text(json.dumps(out, indent=1, sort_keys=True) + "\n")
    return out


_SLUG_RX = r"([a-z]+-updown-[0-9a-z]+-[0-9]+)"


def _norm_orders(dec: pd.DataFrame, orders: pd.DataFrame, budget: str) -> pd.DataFrame:
    """The budget's decisions with order ids (keys of `cancel` / `on_order` / `replace`, `ret` of `place`) renamed
    `<slug>#<k>` (the market's k-th order): ids are numbered per budget, so one market trading differently shifts
    every later id."""
    o = orders[orders["budget"].astype(str) == budget].drop_duplicates("order_id") if len(orders) else orders
    name, count = {}, Counter()
    for oid, slug in zip(o.get("order_id", []), o.get("slug", [])):
        name[int(oid)] = f"{slug}#{count[slug]}"
        count[slug] += 1

    def m(v):
        try:
            return name.get(int(float(v)), v)
        except (TypeError, ValueError):
            return v

    x = dec[dec["budget"].astype(str) == budget].copy()
    oc = x["call"].isin(["cancel", "on_order", "replace"])
    x.loc[oc, "key"] = x.loc[oc, "key"].map(m)
    pl = x["call"] == "place"
    x.loc[pl, "ret"] = x.loc[pl, "ret"].map(m)
    return x


def explain_differences(run_dir: Path, dp: pd.DataFrame, op: pd.DataFrame, dr: pd.DataFrame,
                        orr: pd.DataFrame) -> dict:
    """Attribute paper/recorder decision differences to markets that opened at a different time (`on_market_open`
    `now` differs or only one side opened). Those markets' own calls (key, token or returned order id naming them) are
    set aside; `unexplained` counts what remains. `late_snapshot` lists the open-differing markets with a `pm_book`
    row in the session's `late/` (the hold-back dropped the opening snapshot of a quiet book: it carries the book's
    last-change time). Every difference is explained when `unexplained == 0` and every open-differing market has a
    late snapshot."""
    late_f = Path(run_dir) / "session" / "late" / "pm_book.tsv.zst"
    late = set()
    if late_f.exists():
        late = {r[0] for r in duckdb.connect().execute(
            "SELECT DISTINCT split_part(asset, '|', 1) FROM read_csv(?, delim='\t', header=true, all_varchar=true)",
            [late_f.as_posix()]).fetchall()}
    out: dict = {"budgets": {}, "unexplained": 0, "open_differs": [], "late_snapshot": []}
    if not len(dp) and not len(dr):
        return out
    for b in sorted(set(dp["budget"].astype(str)) | set(dr["budget"].astype(str))):
        x, y = _norm_orders(dp, op, b), _norm_orders(dr, orr, b)
        ox = dict(zip(x.key[x.call == "on_market_open"], x.now[x.call == "on_market_open"]))
        oy = dict(zip(y.key[y.call == "on_market_open"], y.now[y.call == "on_market_open"]))
        diff = {k for k in set(ox) | set(oy) if ox.get(k) != oy.get(k)}

        def keep(df):
            k = pd.Series([str(v) for v in df.key], index=df.index).str.extract(_SLUG_RX)[0]
            r = pd.Series([str(v) for v in df.ret], index=df.index).str.extract(_SLUG_RX)[0]
            return df[~k.isin(diff) & ~r.isin(diff)]

        cx, cy = _keyed(keep(x), DEC_KEY), _keyed(keep(y), DEC_KEY)
        rest = int(sum((cx - cy).values()) + sum((cy - cx).values()))
        out["budgets"][b] = {"open_differs": len(diff), "opened_only_recorder": len([k for k in diff if k not in ox]),
                             "opened_only_paper": len([k for k in diff if k not in oy]), "unexplained": rest}
        out["unexplained"] += rest
        out["open_differs"] += sorted(diff)
        out["late_snapshot"] += sorted(diff & late)
    out["all_late_snapshot"] = set(out["open_differs"]) <= set(out["late_snapshot"])
    return out


def _num(x):
    if x is None:
        return None
    x = float(x)
    return None if not math.isfinite(x) else x


def _same(a, b) -> bool:
    a, b = _num(a), _num(b)
    return a == b


# ------------------------------------------------------------------------------------------- command

def replay_command(run_dir: Path, *, vs_rec: bool = False, keep_parquet: bool = False,
                   echo=print) -> dict:
    """`paper replay`: replay, compare, optionally `--vs-recorder`; removes `session/parquet` unless
    `keep_parquet`. Returns parity.json's content."""
    import shutil

    run_dir = Path(run_dir)
    t0 = time.perf_counter()
    rep = replay(run_dir)
    res = compare(run_dir, rep)
    echo(f"# parity {res['verdict']}: " + ", ".join(f"{k} {v['mismatches']}" for k, v in res["tables"].items())
         + f" (replay {rep.elapsed_s:.1f} s, total {time.perf_counter() - t0:.1f} s)")
    if res["first_divergence"]:
        f = res["first_divergence"]
        echo(f"# first divergence: {f['table']} budget {f['budget']} index {f['index']}")
    if vs_rec:
        cfg, man = load_run(run_dir)
        rec = recorder_run(cfg, int(man["rs"]), int(man["end_ms"]))
        v = vs_recorder(run_dir, rec)
        echo(f"# vs recorder: {v['decision_differences']} decision differences, fills paper {v['fills']['paper']} "
             f"recorder {v['fills']['recorder']}, P&L paper ${v['pnl_usd']['paper_raw']:.2f} recorder "
             f"${v['pnl_usd']['recorder']:.2f}")
    if not keep_parquet:
        shutil.rmtree(run_dir / "session" / "parquet", ignore_errors=True)
    return res


__all__ = ["DEC_KEY", "MARKET_CMP", "RESULT_KEYS", "checks", "compare", "derived_results", "explain_differences",
           "load_run", "recorder_run", "replay", "replay_command", "replay_markets", "vs_recorder"]

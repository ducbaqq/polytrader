"""The paper run folder's files (spec "Run folder and session format"), next to `session/`.

`config.yaml` (the RunConfig plus a `paper:` block), `provenance.json` (`kind: "paper"`), `results.json` (bt's summary
keys with `verdict: null` plus paper stats), `orders/fills/equity.parquet` (bt schemas), `markets.parquet` (bt's
columns plus `pnl_raw`, `outcome_source`, `admitted_ms`, `gamma_outcome`, `gamma_seen_ms`, `outcome_mismatch`, `pnl_gamma`), `decisions.parquet` (the call trace) and `log.txt` (the
strategy's lines, then the runner's `#` status lines).
"""

from __future__ import annotations

import hashlib
import json
import math
import os
from dataclasses import asdict
from pathlib import Path

import pandas as pd
import yaml

from polydata.bt import runs, validate
from polydata.bt.engine import RunResult
from polydata.bt.strategy import get_strategy

PAPER_DIR = Path(__file__).resolve().parent


def paper_version() -> str:
    """sha256 over `polydata/paper/**/*.py` (path and bytes, sorted), 12 hex."""
    h = hashlib.sha256()
    for f in sorted(PAPER_DIR.rglob("*.py")):
        h.update(f.relative_to(PAPER_DIR).as_posix().encode())
        h.update(b"\0")
        h.update(f.read_bytes())
    return h.hexdigest()[:12]


def _clean(x):
    if isinstance(x, float):
        return None if not math.isfinite(x) else x
    if isinstance(x, dict):
        return {str(k): _clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [_clean(v) for v in x]
    if hasattr(x, "item") and not isinstance(x, (str, bytes)):  # numpy scalars
        return _clean(x.item())
    return x


def paper_markets(res: RunResult, router, voids: pd.DataFrame | None = None, confirm=None) -> pd.DataFrame:
    """bt's market columns plus `pnl_raw` (`voids.apply`), `outcome_source`, `admitted_ms`, and Gamma's answer:
    `gamma_outcome`, `gamma_seen_ms`, `outcome_mismatch` (both outcomes known and different) and `pnl_gamma`
    (`pnl_raw` restated with Gamma's outcome; the position's value changes by `pos[gamma] - pos[chainlink]`).
    P&L keeps the outcome the run settled with."""
    from polydata.paper import voids as voids_mod

    mk = voids_mod.apply(res.markets.copy(), voids)
    sources = {}
    for r in router.new_resolutions:
        sources.setdefault(r.slug, r.source)
    for s in (confirm.fallbacks if confirm is not None else ()):
        sources.setdefault(s, "paper-gamma")
    adm = {a["slug"]: int(a["admitted_ms"]) for a in router.admitted}
    mk["outcome_source"] = pd.Series([sources.get(s) for s in mk["slug"]], index=mk.index, dtype=object)
    mk["admitted_ms"] = pd.Series([adm.get(s) for s in mk["slug"]], index=mk.index, dtype="Int64")
    ans = confirm.answers if confirm is not None else {}
    mk["gamma_outcome"] = pd.Series([ans[s]["outcome"] if s in ans else None for s in mk["slug"]], index=mk.index,
                                    dtype=object)
    mk["gamma_seen_ms"] = pd.Series([ans[s]["recv_ms"] if s in ans else None for s in mk["slug"]], index=mk.index,
                                    dtype="Int64")
    outc = mk["outcome"].where(mk["outcome"].notna(), None)
    mk["outcome_mismatch"] = pd.Series([g is not None and o is not None and g != o
                                        for g, o in zip(mk["gamma_outcome"], outc)], index=mk.index, dtype=bool)
    pos: dict[tuple[str, str], float] = {}
    f = res.fills
    for slug, tok, side, size in zip(f["slug"], f["token"], f["side"], f["size"]) if len(f) else ():
        pos[(slug, tok)] = pos.get((slug, tok), 0.0) + (float(size) if side == "BUY" else -float(size))
    pg = []
    for s, raw, g, o, bad in zip(mk["slug"], mk["pnl_raw"], mk["gamma_outcome"], outc, mk["outcome_mismatch"]):
        pg.append(float(raw) + pos.get((s, g), 0.0) - pos.get((s, o), 0.0) if bad else float(raw))
    mk["pnl_gamma"] = pd.Series(pg, index=mk.index, dtype=float)
    return mk


def write_paper_run(d: Path, cfg, opts, res: RunResult, router, manifest: dict, *, stats: dict | None = None,
                    status_lines: list[str] | None = None, voids: pd.DataFrame | None = None, confirm=None,
                    errors: list[str] | None = None) -> dict:
    d = Path(d)
    d.mkdir(parents=True, exist_ok=True)
    stats = stats or {}
    cfg_d = cfg.to_dict()
    cfg_d["paper"] = {k: v for k, v in asdict(opts).items() if k not in ("allow_sealed", "purpose")}
    if opts.allow_sealed:
        cfg_d["paper"].update(allow_sealed=True, purpose=opts.purpose)
    (d / "config.yaml").write_text(yaml.safe_dump(cfg_d, sort_keys=True))

    mf = d / "session" / "manifest.json"
    fingerprint = hashlib.sha256(mf.read_bytes()).hexdigest() if mf.exists() else None
    cls = get_strategy(cfg.strategy)
    prov = runs.provenance(cls, cfg.params, [], "paper", validated=validate.is_validated(),
                           paper_version=paper_version(), session_fingerprint=fingerprint,
                           pythonhashseed=os.environ.get("PYTHONHASHSEED"), elapsed_s=round(res.elapsed_s, 1))
    (d / "provenance.json").write_text(json.dumps(prov, indent=1, sort_keys=True, default=str))

    mk = paper_markets(res, router, voids, confirm)
    summary = runs.summary(res, cfg)
    summary["verdict"] = None
    pnl = float(mk["pnl"].astype(float).fillna(0).sum()) if len(mk) else 0.0
    ndays = summary["n_days"]
    summary.update(pnl_usd=pnl, usd_per_day=pnl / ndays if ndays else 0.0,
                   voided=int(mk["voided"].sum()) if len(mk) else 0)  # voided markets leave the P&L (pnl_raw keeps it)
    bad = mk[mk["outcome_mismatch"]]
    errs = list(errors or []) + [f"outcome mismatch {r.slug}: run settled {r.outcome} ({r.outcome_source}), Gamma says "
                                 f"{r.gamma_outcome}" for r in bad.itertuples()]
    api = dict(confirm.api_trades) if confirm is not None else {}
    st = router.stats
    res_src: dict[str, int] = {}
    for r in router.new_resolutions:
        res_src[r.source] = res_src.get(r.source, 0) + 1
    if confirm is not None and confirm.fallbacks:
        res_src["paper-gamma"] = len(confirm.fallbacks)
    summary.update({
        "rows": dict(sorted(st["rows"].items())),
        "late": int(st["late"]), "future": int(st["future"]),
        "late_rows": int(st["late"]) + int(st["future"]),
        "router": {k: v for k, v in st.items() if k not in ("rows",)},
        "rs": manifest.get("rs"), "end_ms": manifest.get("end_ms"), "clean_stop": manifest.get("clean_stop"),
        "admitted": len(router.admitted), "resolutions": res_src, "unresolved": len(router.unresolved()),
        "processing_lag_ms": stats.get("processing_lag_ms"), "loop_lag_ms": stats.get("loop_lag_ms"),
        "feed_delay_ms": stats.get("feed_delay_ms"), "settler_gave_up": stats.get("gave_up"),
        "connections": stats.get("connections"), "warnings": stats.get("warnings", []),
        "feed_failures": stats.get("feed_failures", 0),
        "outcome_mismatches": int(len(bad)), "parity": None, "errors": errs,
        "pnl_usd_gamma": float(mk["pnl_gamma"].where(~mk["voided"].astype(bool), 0.0).fillna(0).sum()),
        "gamma_answers": len(confirm.answers) if confirm is not None else None,
        "gamma_fallbacks": len(confirm.fallbacks) if confirm is not None else None,
        "api_trades": {"markets": len(api), "rows": sum(api.values())},
        "void_reasons": dict(zip(voids.slug, voids.reason)) if voids is not None and len(voids) else {},
    })
    summary = _clean(summary)
    (d / "results.json").write_text(json.dumps(summary, indent=1, sort_keys=True, allow_nan=False) + "\n")

    res.orders.to_parquet(d / "orders.parquet", index=False)
    res.fills.to_parquet(d / "fills.parquet", index=False)
    res.equity.to_parquet(d / "equity.parquet", index=False)
    mk.to_parquet(d / "markets.parquet", index=False)
    res.decisions.to_parquet(d / "decisions.parquet", index=False)
    lines = [*map(str, res.log), *(status_lines or [])]
    (d / "log.txt").write_text("\n".join(lines) + ("\n" if lines else ""))
    return summary


__all__ = ["paper_markets", "paper_version", "write_paper_run"]

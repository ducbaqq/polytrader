"""The paper run folder's files (spec "Run folder and session format"), next to `session/`.

`config.yaml` (the RunConfig plus a `paper:` block), `provenance.json` (`kind: "paper"`), `results.json` (bt's summary
keys with `verdict: null` plus paper stats), `orders/fills/equity.parquet` (bt schemas), `markets.parquet` (bt's
columns plus `pnl_raw`, `outcome_source`, `admitted_ms`), `decisions.parquet` (the call trace) and `log.txt` (the
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


def paper_markets(res: RunResult, router) -> pd.DataFrame:
    mk = res.markets.copy()
    sources = {}
    for r in router.new_resolutions:
        sources.setdefault(r.slug, r.source)
    adm = {a["slug"]: int(a["admitted_ms"]) for a in router.admitted}
    mk["pnl_raw"] = mk["pnl"].astype(float) if len(mk) else pd.Series(dtype=float)
    mk["outcome_source"] = pd.Series([sources.get(s) for s in mk["slug"]], index=mk.index, dtype=object)
    mk["admitted_ms"] = pd.Series([adm.get(s) for s in mk["slug"]], index=mk.index, dtype="Int64")
    return mk


def write_paper_run(d: Path, cfg, opts, res: RunResult, router, manifest: dict, *, stats: dict | None = None,
                    status_lines: list[str] | None = None) -> dict:
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

    summary = runs.summary(res, cfg)
    summary["verdict"] = None
    st = router.stats
    res_src: dict[str, int] = {}
    for r in router.new_resolutions:
        res_src[r.source] = res_src.get(r.source, 0) + 1
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
        "outcome_mismatches": None, "pnl_usd_gamma": None, "parity": None,
    })
    summary = _clean(summary)
    (d / "results.json").write_text(json.dumps(summary, indent=1, sort_keys=True, allow_nan=False) + "\n")

    res.orders.to_parquet(d / "orders.parquet", index=False)
    res.fills.to_parquet(d / "fills.parquet", index=False)
    res.equity.to_parquet(d / "equity.parquet", index=False)
    paper_markets(res, router).to_parquet(d / "markets.parquet", index=False)
    res.decisions.to_parquet(d / "decisions.parquet", index=False)
    lines = [*map(str, res.log), *(status_lines or [])]
    (d / "log.txt").write_text("\n".join(lines) + ("\n" if lines else ""))
    return summary


__all__ = ["paper_markets", "paper_version", "write_paper_run"]

"""Run folders, strategy versions, the runs index and sharing runs through the Spaces bucket.

A run folder is `<data>/runs/<family>/<strategy>/<YYYY-MM-DDTHHMM>-<6 hex>/`. The runs index
(`<data>/runs/index.parquet` and the committed `docs/runs-index.csv`) is derived: `rebuild_index`
scans the folders, so it can always be rebuilt from them.

Run files that get pushed never contain secrets, hostnames, IPs or bucket names (the host is a
hash). `results.json` is deterministic: the same run twice gives the same bytes.
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import platform
import secrets
import subprocess
import sys
from datetime import datetime
from importlib import metadata
from pathlib import Path

import pandas as pd
import yaml

from ..config import REPO_ROOT, paths
from . import validate
from .config import RunConfig
from .engine import RunResult
from .splits import day_kind

FINAL_MIN_DAYS = 5
MIN_USD_PER_DAY = 10
LATENCY_SWEEP_MS = (100, 200, 400)
KINDS = ("run", "final", "sweep", "screen")
INDEX_COLUMNS = ["run_id", "created_utc", "family", "strategy", "version", "kind", "days", "n_days", "coins",
                 "timeframes", "order_latency_ms", "pnl_usd", "usd_per_day", "t_block", "verdict", "tags", "path"]
_NOT_SHARED = {"index.parquet", "looks.jsonl"}  # derived / local audit trail


class FinalRefused(Exception):
    """A `--final` run that the rules do not allow."""


def runs_dir() -> Path:
    return paths().root / "runs"


def csv_default() -> Path:
    return REPO_ROOT / "docs" / "runs-index.csv"


# ---------------------------------------------------------------------------------------- versions

def effective_params(strategy_cls: type, params: dict) -> dict:
    return {**strategy_cls.default_params, **(params or {})}


def strategy_version(strategy_cls: type, params: dict) -> str:
    """sha256 of the strategy's source file bytes, its registry name and the sorted-JSON params, 12 hex."""
    h = hashlib.sha256(Path(inspect.getsourcefile(strategy_cls)).read_bytes())
    h.update(b"\0" + strategy_cls.name.encode() + b"\0")  # several strategies can share one file
    h.update(json.dumps(params, sort_keys=True, default=str).encode())
    return h.hexdigest()[:12]


def data_fingerprint(days: list[str]) -> str:
    """sha256 of the sorted (raw path, size) list of `days`: from the raw dir, else the Parquet `.src` manifests."""
    p = paths()
    items: list[str] = []
    for day in sorted(set(days)):
        files = sorted(p.raw.glob(f"{day}/*/*.zst"))
        if files:
            items += [f"{f.relative_to(p.raw).as_posix()}\t{f.stat().st_size}" for f in files]
            continue
        for src in sorted(p.parquet.glob(f"*/date={day}/*.src")):
            rel = src.relative_to(p.parquet).with_suffix("").as_posix()
            items += [f"{rel}/{line}" for line in src.read_text().splitlines() if line and not line.startswith("#")]
    return hashlib.sha256("\n".join(sorted(items)).encode()).hexdigest()


def available_days() -> list[str]:
    """Days with raw files or Parquet on disk, ISO strings, sorted."""
    p = paths()
    days = {d.name for d in p.raw.glob("*") if d.is_dir()} | {d.name[5:] for d in p.parquet.glob("*/date=*")}
    return sorted(days)


def days_of_kinds(kinds: set[str]) -> list[str]:
    from datetime import date

    return [d for d in available_days() if day_kind(date.fromisoformat(d)) in kinds]


def new_run_dir(family: str, strategy: str, now_utc: datetime) -> Path:
    d = runs_dir() / family / strategy / f"{now_utc:%Y-%m-%dT%H%M}-{secrets.token_hex(3)}"
    d.mkdir(parents=True, exist_ok=False)
    return d


# ---------------------------------------------------------------------------------------- records

def host_hash() -> str:
    """The host as 8 hex of sha256(platform.node()), never the hostname itself (the repo is public)."""
    return hashlib.sha256(platform.node().encode()).hexdigest()[:8]


def _git(*args: str) -> str | None:
    try:
        out = subprocess.run(["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, timeout=30)
        return out.stdout if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def provenance(strategy_cls: type, params: dict, days: list[str], kind: str, **extra) -> dict:
    commit = (_git("rev-parse", "HEAD") or "").strip() or None
    diff = _git("diff", "HEAD") or ""
    pk = {}
    for name in ("polydata", "duckdb", "pandas", "numpy", "pyarrow", "pyyaml"):
        try:
            pk[name] = metadata.version(name)
        except metadata.PackageNotFoundError:
            pass
    return {"kind": kind, "family": strategy_cls.family, "strategy": strategy_cls.name,
            "git_commit": commit, "git_diff": diff[:200_000] if diff else None, "git_dirty": bool(diff),
            "sim_version": validate.sim_version(), "strategy_version": strategy_version(strategy_cls, effective_params(strategy_cls, params)),
            "data_fingerprint": data_fingerprint(days), "python": sys.version.split()[0], "packages": pk,
            "host": host_hash(), **extra}


def _num(x) -> float | None:
    x = float(x)
    return None if math.isnan(x) else x


def result_days(result: RunResult) -> list[str]:
    mk = result.markets
    if mk.empty:
        return []
    ms = mk["window_start_ms"].astype("int64")
    return sorted(set(pd.to_datetime(ms, unit="ms", utc=True).dt.strftime("%Y-%m-%d")))


def summary(result: RunResult, cfg: RunConfig) -> dict:
    """The minimal deterministic summary written to results.json. Task 8 extends it with metrics and verdicts."""
    mk = result.markets.sort_values(["slug"], kind="stable") if len(result.markets) else result.markets
    days = result_days(result)
    kinds: dict[str, int] = {}
    from datetime import date

    for d in days:
        k = day_kind(date.fromisoformat(d))
        kinds[k] = kinds.get(k, 0) + 1
    pnl = float(mk["pnl"].astype(float).fillna(0).sum()) if len(mk) else 0.0
    o = result.orders
    n_orders = len(o[["budget", "order_id"]].drop_duplicates()) if len(o) else 0
    return {"pnl_usd": pnl, "usd_per_day": pnl / len(days) if days else 0.0, "fills": len(result.fills),
            "orders": n_orders, "markets": len(mk), "voided": int(mk["voided"].sum()) if len(mk) else 0,
            "n_days": len(days), "days": days, "day_kinds": kinds, "order_latency_ms": cfg.order_latency_ms,
            "t_block": None, "verdict": None}


def write_run(d: Path, cfg: RunConfig, result: RunResult, metrics: dict, prov: dict) -> None:
    d.mkdir(parents=True, exist_ok=True)
    (d / "config.yaml").write_text(yaml.safe_dump(cfg.to_dict(), sort_keys=True))
    (d / "provenance.json").write_text(json.dumps(prov, indent=1, sort_keys=True, default=str))
    (d / "results.json").write_text(json.dumps(metrics, indent=1, sort_keys=True, allow_nan=False) + "\n")
    result.orders.to_parquet(d / "orders.parquet", index=False)
    result.equity.to_parquet(d / "equity.parquet", index=False)
    result.fills.to_parquet(d / "fills.parquet", index=False)
    result.markets.to_parquet(d / "markets.parquet", index=False)
    (d / "log.txt").write_text("\n".join(map(str, result.log)) + ("\n" if result.log else ""))


# ---------------------------------------------------------------------------------------- the index

def _row(d: Path) -> dict | None:
    try:
        cfg = yaml.safe_load((d / "config.yaml").read_text()) or {}
        res = json.loads((d / "results.json").read_text())
        prov = json.loads((d / "provenance.json").read_text()) if (d / "provenance.json").exists() else {}
    except (OSError, ValueError, yaml.YAMLError):
        return None
    rid = d.name
    try:
        created = datetime.strptime(rid[:15], "%Y-%m-%dT%H%M").strftime("%Y-%m-%dT%H:%M:00Z")
    except ValueError:
        created = None
    days = res.get("days") or []
    return {"run_id": rid, "created_utc": created, "family": d.parent.parent.name, "strategy": d.parent.name,
            "version": prov.get("strategy_version"), "kind": prov.get("kind", "run"),
            "days": f"{days[0]}..{days[-1]}" if len(days) > 1 else (days[0] if days else ""),
            "n_days": res.get("n_days", len(days)), "coins": ",".join(cfg.get("coins") or []),
            "timeframes": ",".join(cfg.get("timeframes") or []), "order_latency_ms": cfg.get("order_latency_ms"),
            "pnl_usd": res.get("pnl_usd"), "usd_per_day": res.get("usd_per_day"), "t_block": res.get("t_block"),
            "verdict": res.get("verdict"), "tags": ",".join(cfg.get("tags") or []),
            "path": d.relative_to(runs_dir()).as_posix()}


def rebuild_index(csv_path: Path | None = None) -> pd.DataFrame:
    """Scan `runs/*/*/*/results.json`, write `runs/index.parquet` and the CSV mirror, return the frame."""
    rows = [r for f in sorted(runs_dir().glob("*/*/*/results.json")) if (r := _row(f.parent))]
    df = pd.DataFrame(rows, columns=INDEX_COLUMNS)
    df = df.astype({"run_id": "object", "created_utc": "object", "family": "object", "strategy": "object",
                    "version": "object", "kind": "object", "days": "object", "n_days": "int64", "coins": "object",
                    "timeframes": "object", "order_latency_ms": "Int64", "pnl_usd": "float64",
                    "usd_per_day": "float64", "t_block": "float64", "verdict": "object", "tags": "object",
                    "path": "object"})
    df = df.sort_values(["created_utc", "run_id"], kind="stable").reset_index(drop=True)
    runs_dir().mkdir(parents=True, exist_ok=True)
    df.to_parquet(runs_dir() / "index.parquet", index=False)
    out = csv_path or csv_default()
    out.parent.mkdir(parents=True, exist_ok=True)
    df.to_csv(out, index=False, lineterminator="\n")
    return df


def load_index() -> pd.DataFrame:
    f = runs_dir() / "index.parquet"
    return pd.read_parquet(f) if f.exists() else rebuild_index()


def final_looks() -> int:
    """Final runs in the index plus the non-`health` lines of looks.jsonl: N for the trial-budget threshold."""
    idx = runs_dir() / "index.parquet"
    n = int((pd.read_parquet(idx).kind == "final").sum()) if idx.exists() else 0
    f = runs_dir() / "looks.jsonl"
    if f.exists():
        for line in f.read_text().splitlines():
            if line.strip():
                try:
                    purpose = json.loads(line).get("purpose")
                except ValueError:
                    purpose = None
                n += purpose != "health"
    return n


def final_preflight(strategy_cls: type, params: dict, days: list[str], sim_ver: str | None = None) -> pd.DataFrame:
    """Raise FinalRefused unless a final run is allowed; return the index rows of earlier finals of this version."""
    sim = sim_ver or validate.sim_version()
    if not validate.is_validated(sim):
        raise FinalRefused(f"the simulator validation has not passed for sim_version {sim}: run `polydata bt validate`")
    if len(days) < FINAL_MIN_DAYS:
        raise FinalRefused(f"only {len(days)} sealed days available, --final needs at least {FINAL_MIN_DAYS}")
    version, fp = strategy_version(strategy_cls, effective_params(strategy_cls, params)), data_fingerprint(days)
    idx = load_index()
    finals = idx[(idx.kind == "final") & (idx.version == version)]
    same = []
    for _, r in finals.iterrows():
        try:
            prov = json.loads((runs_dir() / r.path / "provenance.json").read_text())
        except (OSError, ValueError):
            continue
        if prov.get("sim_version") == sim and prov.get("data_fingerprint") == fp:
            same.append(r.run_id)
    if same:
        raise FinalRefused(f"a final run of version {version} already exists for this simulator version and data: "
                           f"{', '.join(same)}. Change the strategy, or fix the simulator / re-sync the data.")
    return finals.reset_index(drop=True)


# ---------------------------------------------------------------------------------------- sharing

def _shared_files() -> list[Path]:
    r = runs_dir()
    return sorted(f for f in r.rglob("*") if f.is_file() and f.name not in _NOT_SHARED and not f.name.endswith(".part")
                  and ".tmp" not in f.parts)


def _remote(client, bucket_name: str) -> dict[str, int]:
    out = {}
    for page in client.get_paginator("list_objects_v2").paginate(Bucket=bucket_name, Prefix="runs/"):
        out.update({o["Key"]: o["Size"] for o in page.get("Contents", [])})
    return out


def _client(client, bucket_name):
    if client is None:
        from ..config import bucket, spaces_client

        return spaces_client(), bucket()
    return client, bucket_name


def push(client=None, bucket_name: str | None = None, csv_path: Path | None = None) -> int:
    """Upload run files that are missing in the bucket (or differ in size), then rebuild the index."""
    client, bucket_name = _client(client, bucket_name)
    remote = _remote(client, bucket_name)
    n = 0
    for f in _shared_files():
        key = "runs/" + f.relative_to(runs_dir()).as_posix()
        if remote.get(key) != f.stat().st_size:
            client.upload_file(str(f), bucket_name, key)
            n += 1
    rebuild_index(csv_path)
    return n


def pull(client=None, bucket_name: str | None = None, csv_path: Path | None = None) -> int:
    """Download run files that are missing locally (or differ in size), then rebuild the index."""
    client, bucket_name = _client(client, bucket_name)
    n = 0
    for key, size in sorted(_remote(client, bucket_name).items()):
        rel = key[len("runs/"):]
        if not rel or Path(rel).name in _NOT_SHARED:
            continue
        local = runs_dir() / rel
        if local.exists() and local.stat().st_size == size:
            continue
        local.parent.mkdir(parents=True, exist_ok=True)
        tmp = local.with_name(local.name + ".part")
        client.download_file(bucket_name, key, str(tmp))
        tmp.replace(local)
        n += 1
    rebuild_index(csv_path)
    return n

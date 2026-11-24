"""Run folders, strategy versions, the runs index and sharing runs through the Spaces bucket.

A run folder is `<data>/runs/<family>/<strategy>/<YYYY-MM-DDTHHMM>-<6 hex>/`. The runs index
(`<data>/runs/index.parquet` and the committed `docs/runs-index.csv`) is derived: `rebuild_index`
scans the folders, so it can always be rebuilt from them.

Run files that get pushed never contain secrets, hostnames, IPs or bucket names (the host is a
hash). `results.json` is deterministic: the same run twice gives the same bytes.

Pulled validation files (`runs/validation/<sim_version>.json`) are trusted, keyed by `sim_version`.
Looks are pushed per host (`runs/looks/<host8>.jsonl`, a copy of the local `looks.jsonl`).

Versions: `strategy_version` hashes the strategy's source file bytes, its registry name (several
strategies can share one file) and the params merged over the class `default_params`.
Provenance: `provenance.json` carries `git diff HEAD` (tracked files only, capped at 200 kB) when the
tree is dirty; it is pushed with the run, so it must stay free of secrets (the repo is public).
"""

from __future__ import annotations

import hashlib
import inspect
import json
import math
import os
import platform
import secrets
import shutil
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
    return Path(os.environ["POLYDATA_RUNS_CSV"]) if os.environ.get("POLYDATA_RUNS_CSV") else REPO_ROOT / "docs" / "runs-index.csv"


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
    """sha256 over the `.src` manifests (raw file names and sizes) of the built Parquet of `days`.

    They describe what is actually replayed and survive pruning of the raw files. Raises if a day has none."""
    p = paths()
    h = hashlib.sha256()
    for day in sorted(set(days)):
        srcs = sorted(p.parquet.glob(f"*/date={day}/*.src"))
        if not srcs:
            raise ValueError(f"day {day} not built: run polydata build")
        for src in srcs:
            h.update(src.relative_to(p.parquet).as_posix().encode() + b"\0" + src.read_bytes() + b"\0")
    return h.hexdigest()


def available_days() -> list[str]:
    """Days with raw files or Parquet on disk, ISO strings, sorted."""
    p = paths()
    days = {d.name for d in p.raw.glob("*") if d.is_dir()} | {d.name[5:] for d in p.parquet.glob("*/date=*")}
    return sorted(days)


def days_of_kinds(kinds: set[str]) -> list[str]:
    from datetime import date

    return [d for d in available_days() if day_kind(date.fromisoformat(d)) in kinds]


def new_run_dir(family: str, strategy: str, now_utc: datetime) -> Path:
    for _ in range(100):
        d = runs_dir() / family / strategy / f"{now_utc:%Y-%m-%dT%H%M}-{secrets.token_hex(3)}"
        try:
            d.mkdir(parents=True, exist_ok=False)
            return d
        except FileExistsError:
            continue
    raise RuntimeError("could not find a free run folder name")


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
    try:
        fingerprint = data_fingerprint(days)
    except ValueError:
        if kind == "final":
            raise
        fingerprint = None  # an ordinary run must not abort after its replay
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
            "data_fingerprint": fingerprint, "python": sys.version.split()[0], "packages": pk,
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
    out = csv_path or csv_default()
    out.parent.mkdir(parents=True, exist_ok=True)
    tag = f".{os.getpid()}.{secrets.token_hex(2)}.tmp"
    for target, write in ((runs_dir() / "index.parquet", lambda f: df.to_parquet(f, index=False)),
                          (out, lambda f: df.to_csv(f, index=False, lineterminator="\n"))):
        tmp = target.with_name(target.name + tag)
        write(tmp)
        os.replace(tmp, target)  # atomic: concurrent readers and writers never see a partial file
    return df


def load_index() -> pd.DataFrame:
    f = runs_dir() / "index.parquet"
    return pd.read_parquet(f) if f.exists() else rebuild_index()


def _look_lines() -> list[dict]:
    """Looks from the local looks.jsonl and every pulled `runs/looks/*.jsonl`, deduplicated by (utc, purpose, detail)."""
    seen, out = set(), []
    for f in [runs_dir() / "looks.jsonl", *sorted((runs_dir() / "looks").glob("*.jsonl"))]:
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            key = (rec.get("utc"), rec.get("purpose"), json.dumps(rec.get("detail"), sort_keys=True))
            if key not in seen:
                seen.add(key)
                out.append(rec)
    return out


def final_looks() -> int:
    """Final runs in the (freshly rebuilt) index plus the look lines (local and pulled) that are neither
    `health` nor `final` (a final run records a `final` look but is counted once, as a run): N for the
    trial-budget threshold."""
    n = int((rebuild_index().kind == "final").sum())
    return n + sum(r.get("purpose") not in ("health", "final") for r in _look_lines())


def final_preflight(strategy_cls: type, params: dict, days: list[str], sim_ver: str | None = None) -> pd.DataFrame:
    """Raise FinalRefused unless a final run is allowed; return the index rows of earlier finals of this version."""
    sim = sim_ver or validate.sim_version()
    if not validate.is_validated(sim):
        raise FinalRefused(f"the simulator validation has not passed for sim_version {sim}: run `polydata bt validate`")
    if len(days) < FINAL_MIN_DAYS:
        raise FinalRefused(f"only {len(days)} sealed days available, --final needs at least {FINAL_MIN_DAYS}")
    try:
        fp = data_fingerprint(days)
    except ValueError as e:
        raise FinalRefused(str(e)) from None
    version = strategy_version(strategy_cls, effective_params(strategy_cls, params))
    idx = rebuild_index()  # never trust a cached index
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
    return sorted(f for f in r.rglob("*") if f.is_file() and f.name not in _NOT_SHARED and not f.name.endswith((".part", ".tmp")))


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
    local_looks = runs_dir() / "looks.jsonl"
    if local_looks.exists():  # shared under a per-host name so machines never overwrite each other's looks
        (runs_dir() / "looks").mkdir(exist_ok=True)
        shutil.copyfile(local_looks, runs_dir() / "looks" / f"{host_hash()}.jsonl")
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
        local = (runs_dir() / rel).resolve()
        if not local.is_relative_to(runs_dir().resolve()):
            continue  # a key that would leave runs/
        if local.exists() and local.stat().st_size == size:
            continue
        local.parent.mkdir(parents=True, exist_ok=True)
        tmp = local.with_name(local.name + ".part")
        client.download_file(bucket_name, key, str(tmp))
        tmp.replace(local)
        n += 1
    rebuild_index(csv_path)
    return n

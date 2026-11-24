"""Download recorder files from the Spaces bucket into the local mirror (data/raw, data/catalog).

Only files that are missing locally or have a different size are downloaded, so re-running is
cheap. Files are written to a temp name and renamed, so an interrupted sync never leaves a partial
file behind.
"""

from __future__ import annotations

import fnmatch
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from pathlib import Path

from .config import bucket, paths, spaces_client


@dataclass
class RemoteFile:
    key: str
    size: int

    @property
    def day(self) -> str:  # raw/YYYY-MM-DD/HH/... or catalog/YYYY-MM-DD/...
        return self.key.split("/")[1]

    @property
    def stream(self) -> str:
        return self.key.rsplit("/", 1)[-1].split(".", 1)[0]


def list_remote(prefix: str) -> list[RemoteFile]:
    s3 = spaces_client()
    out: list[RemoteFile] = []
    for page in s3.get_paginator("list_objects_v2").paginate(Bucket=bucket(), Prefix=prefix):
        out += [RemoteFile(o["Key"], o["Size"]) for o in page.get("Contents", [])]
    return out


def sync(
    days: list[str] | None = None,
    streams: list[str] | None = None,
    include_catalog: bool = True,
    workers: int = 16,
    dry_run: bool = False,
) -> dict:
    """Mirror raw (and catalog) files. `days`/`streams` accept glob patterns, e.g. '2026-11-2*', 'pm_*'."""
    root = paths().root
    remote = list_remote("raw/") + (list_remote("catalog/") if include_catalog else [])

    def wanted(f: RemoteFile) -> bool:
        if days and not any(fnmatch.fnmatch(f.day, d) for d in days):
            return False
        if streams and f.key.startswith("raw/") and not any(fnmatch.fnmatch(f.stream, s) for s in streams):
            return False
        return True

    todo = []
    for f in filter(wanted, remote):
        local = root / f.key
        if not local.exists() or local.stat().st_size != f.size:
            todo.append(f)

    total = sum(f.size for f in todo)
    print(f"{len(todo)} files to download ({total / 1e9:.2f} GB)")
    if dry_run or not todo:
        return {"files": len(todo), "bytes": total}

    s3 = spaces_client()  # boto3 clients are thread-safe
    done_bytes = 0
    start = time.time()

    def fetch(f: RemoteFile) -> int:
        local = root / f.key
        local.parent.mkdir(parents=True, exist_ok=True)
        tmp = local.with_name(local.name + ".part")
        s3.download_file(bucket(), f.key, str(tmp))
        if tmp.stat().st_size != f.size:
            tmp.unlink()
            raise IOError(f"size mismatch for {f.key}")
        tmp.replace(local)
        return f.size

    errors = 0
    with ThreadPoolExecutor(workers) as pool:
        futures = {pool.submit(fetch, f): f for f in todo}
        for i, fut in enumerate(as_completed(futures), 1):
            try:
                done_bytes += fut.result()
            except Exception as e:  # keep going; a re-run picks up whatever failed
                errors += 1
                print(f"  failed: {futures[fut].key}: {e}")
            if i % 200 == 0 or i == len(todo):
                rate = done_bytes / max(time.time() - start, 1e-9) / 1e6
                print(f"  {i}/{len(todo)} files, {done_bytes / 1e9:.2f} GB, {rate:.0f} MB/s")
    for part in Path(root).rglob("*.part"):
        part.unlink()
    return {"files": len(todo) - errors, "bytes": done_bytes, "errors": errors}

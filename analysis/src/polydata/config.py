"""Paths and credentials. Everything is configurable via environment / the repo-root .env."""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path

from dotenv import load_dotenv

REPO_ROOT = Path(__file__).resolve().parents[3]
load_dotenv(REPO_ROOT / ".env")


@dataclass(frozen=True)
class Paths:
    root: Path

    @property
    def raw(self) -> Path:  # mirror of spaces:<bucket>/raw (zstd TSV/JSONL, as recorded)
        return self.root / "raw"

    @property
    def catalog(self) -> Path:  # mirror of spaces:<bucket>/catalog (daily Postgres exports)
        return self.root / "catalog"

    @property
    def parquet(self) -> Path:  # typed Parquet built from raw, one file per stream per hour
        return self.root / "parquet"

    @property
    def db(self) -> Path:  # DuckDB file holding the views (no data; views point at parquet)
        return self.root / "polytrader.duckdb"


def paths() -> Paths:
    return Paths(Path(os.environ.get("POLYDATA_DIR", REPO_ROOT / "data")).expanduser().resolve())


def spaces_client():
    """boto3 S3 client for the DigitalOcean Spaces bucket (credentials from .env)."""
    import boto3

    missing = [k for k in ("SPACES_BUCKET", "SPACES_REGION", "SPACES_KEY", "SPACES_SECRET") if not os.environ.get(k)]
    if missing:
        raise SystemExit(f"Missing {', '.join(missing)} in {REPO_ROOT / '.env'} (see .env.example)")
    # A globally set AWS_PROFILE would make boto3 look up an unrelated AWS profile; hide it while
    # creating the session.
    saved = os.environ.pop("AWS_PROFILE", None)
    try:
        return boto3.session.Session(
            aws_access_key_id=os.environ["SPACES_KEY"],
            aws_secret_access_key=os.environ["SPACES_SECRET"],
            region_name=os.environ["SPACES_REGION"],
        ).client("s3", endpoint_url=f"https://{os.environ['SPACES_REGION']}.digitaloceanspaces.com")
    finally:
        if saved is not None:
            os.environ["AWS_PROFILE"] = saved


def bucket() -> str:
    return os.environ["SPACES_BUCKET"]

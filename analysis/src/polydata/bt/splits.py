"""Permanent day splits (sealed / validation / train) and the audit trail of looks at sealed data.

The kind of a day depends only on its date. Sealed days are hidden in the data layer
(`polydata.views`), so ordinary analysis code cannot read them by accident.
"""

from __future__ import annotations

import json
import subprocess
from collections.abc import Iterable
from datetime import date, datetime, timezone
from typing import Literal

from ..config import REPO_ROOT, paths

SPLIT_EPOCH = date(2026, 11, 25)  # first full day after the recorder fixes; day number 0 (sealed)
FIRST_DAY = date(2026, 11, 24)  # earlier days are excluded; this one is train from REPLAY_START_MS on
REPLAY_START_MS = 1795516560000  # 2026-11-24 10:36:00 UTC; nothing earlier is ever replayed

DayKind = Literal["sealed", "validation", "train", "excluded"]


def day_kind(d: date) -> DayKind:
    if d < FIRST_DAY:
        return "excluded"
    if d < SPLIT_EPOCH:
        return "train"
    return ("sealed", "validation", "train")[(d - SPLIT_EPOCH).days % 3]


def days_of_kind(days: Iterable[date], kinds: set[str]) -> list[date]:
    return [d for d in days if day_kind(d) in kinds]


def sealed_filter(expr: str) -> str:
    """SQL boolean over a DATE expression: true for non-sealed days."""
    return f"({expr} < DATE '{SPLIT_EPOCH.isoformat()}' OR ({expr} - DATE '{SPLIT_EPOCH.isoformat()}') % 3 <> 0)"


SEALED_SQL_FILTER = sealed_filter("date")


def _git_commit() -> str | None:
    try:
        out = subprocess.run(["git", "rev-parse", "HEAD"], cwd=REPO_ROOT, capture_output=True, text=True, timeout=10)
        return out.stdout.strip() or None if out.returncode == 0 else None
    except (OSError, subprocess.SubprocessError):
        return None


def record_look(purpose: str, detail: dict) -> None:
    """Append one line to runs/looks.jsonl. Every read of sealed data goes through here."""
    f = paths().root / "runs" / "looks.jsonl"
    f.parent.mkdir(parents=True, exist_ok=True)
    rec = {"utc": datetime.now(timezone.utc).isoformat(), "purpose": purpose, "detail": detail,
           "git_commit": _git_commit()}
    with f.open("a") as fh:
        fh.write(json.dumps(rec, default=str) + "\n")

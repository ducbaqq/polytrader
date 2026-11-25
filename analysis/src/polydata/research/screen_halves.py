"""Crude out-of-sample check for a screen rule: evaluate it on each half of the train day (split at the
HALF cut) at 300 and 1000 ms, optionally by coin x timeframe.

    uv run python -m polydata.research.screen_halves settlement_lock "dict(margin=0.01)" [by]

Rules are the functions of `polydata.screen_ideas`. Tune on `h1` (markets starting before 2026-11-24 17:15 UTC),
then report `h2` unchanged. Never reads sealed days (`screen.load_snapshots` drops them).
"""

from __future__ import annotations

import sys
from datetime import datetime, timezone

from .. import screen, screen_ideas

HALF_MS = int(datetime(2026, 11, 24, 17, 15, tzinfo=timezone.utc).timestamp() * 1000)


def fmt(st: dict) -> str:
    if not st["n_trades"]:
        return "n=0"
    c = st["ci_cents"]
    return f"n={st['n_trades']} {st['cents_per_share']:+.2f}c [{c[0]:+.2f},{c[1]:+.2f}] ${st['usd_per_day']:+.2f}/d"


def run_half(rule, snap, voided, half: str, latency_ms: int = 300, **kw):
    s = snap[snap.window_start_ms < HALF_MS] if half == "h1" else snap[snap.window_start_ms >= HALF_MS]
    s = s.reset_index(drop=True)
    sig = rule(s.drop(columns=[c for c in screen.FORBIDDEN_RULE_COLUMNS if c in s.columns]), **kw)
    return screen.evaluate(s, sig, latency_ms, "hold", screen.DEFAULT_SIZE, voided), sig


def main(argv: list[str]) -> None:
    rule = getattr(screen_ideas, argv[0])
    kw = eval(argv[1]) if len(argv) > 1 else {}  # trusted local use only
    by = len(argv) > 2 and argv[2] == "by"
    snap, _ = screen.load_snapshots(rebuild=False)
    voided = screen.voided_slugs(snap)
    for half in ("h1", "h2"):
        for lat in (300, 1000):
            r, sig = run_half(rule, snap, voided, half, lat, **kw)
            print(half, lat, fmt(r["overall"]), flush=True)
            if by and lat == 300:
                for k, x in r["by_coin_timeframe"].items():
                    if x["n_trades"]:
                        print("   ", k, fmt(x))


if __name__ == "__main__":
    main(sys.argv[1:])

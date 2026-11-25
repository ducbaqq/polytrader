"""Replay identity harness on synthetic data."""

from __future__ import annotations

import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import replay_identity as ri  # noqa: E402
from test_engine import M1, T0, cfg, loader_for, make_con, market_one  # noqa: E402

from polydata.bt import engine  # noqa: E402


def _run(c):
    con = make_con([M1], T0 - 120_000, T0 + 400_000)
    return engine.run_coin(c, "btc", lambda: con, loader=loader_for({M1.slug: market_one()}))


def _res(c):
    r = _run(c)
    return engine.RunResult(c, {"btc": r})


def _cfg(**kw):
    return cfg(strategy="join-best", timeframes=["5m"], **kw)


def test_fingerprint_stable():
    c = _cfg()
    a, b = ri.fingerprint(_res(c), c), ri.fingerprint(_res(c), c)
    assert a == b and set(a) == {"results.json", "hashes.json"}


def test_fingerprint_detects_change():
    c = _cfg()
    c2 = replace(c, order_latency_ms=c.order_latency_ms + 50)
    assert ri.fingerprint(_res(c), c)["hashes.json"] != ri.fingerprint(_res(c2), c2)["hashes.json"]


def test_fingerprint_order_sensitive():
    c = _cfg()
    res = _res(c)
    base = ri.fingerprint(res, c)["hashes.json"]
    o = res.coins["btc"].orders
    assert len(o) > 1
    res.coins["btc"].orders = o.iloc[::-1].reset_index(drop=True)
    assert ri.fingerprint(res, c)["hashes.json"] != base

"""The simulator validation suite on real data (POLYDATA_REALDATA=1 and POLYDATA_DIR set).

Runs `validate.run_suite(["2026-11-24"])` (BTC + SOL 5m, ~7 h of replayable data: five replays per coin),
which writes `<data>/runs/validation/<sim_version>.json`. Thresholds are the spec's; never loosen them.
"""

from __future__ import annotations

import json
import os

import pytest

pytestmark = pytest.mark.skipif(os.environ.get("POLYDATA_REALDATA") != "1", reason="needs POLYDATA_REALDATA=1")


@pytest.fixture(scope="module")
def suite():
    from polydata.bt import validate

    return validate.run_suite(["2026-11-24"])


def _show(name, c):
    print(f"\n{name}: " + json.dumps({k: v for k, v in c.items() if k not in ("skipped_markets", "failures")})[:1500])


@pytest.mark.parametrize("check", ["book_reconstruction", "noop", "buy_and_hold_up", "always_cross", "fill_support",
                                   "maker_edge", "determinism"])
def test_check_passes(suite, check):
    c = suite["checks"][check]
    _show(check, c)
    assert c["passed"]


def test_suite_recorded(suite):
    from polydata.bt import validate

    assert suite["passed"] and validate.is_validated(suite["sim_version"])
    assert suite["checks"]["buy_and_hold_up"]["compared"] > 100
    assert suite["checks"]["fill_support"]["sim_fills"] > 100

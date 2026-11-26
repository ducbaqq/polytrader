import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[2] / "tools"))

import paper_bench  # noqa: E402


def test_bench_smoke():
    res = paper_bench.main(["parsers", "--n", "1000"])
    assert set(res) == {"clob", "binance_fut", "rtds"}
    assert all(v > 0 for v in res.values())

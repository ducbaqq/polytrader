import re
from pathlib import Path

from polydata.paper.feeds import series as S

ROOT = Path(__file__).resolve().parents[3]
CONFIG_TS = (ROOT / "src/recorder/config.ts").read_text()
POLY_TS = (ROOT / "src/recorder/polymarket.ts").read_text()


def test_series_matches_recorder_config():
    rows = re.findall(r"\['(\d+)', '([\w-]+)', '(\w+)', '(\w+)'\]", CONFIG_TS)
    assert rows and [tuple(s) for s in S.SERIES] == [tuple(r) for r in rows]
    assert S.SUBSCRIBE_LEAD_WINDOWS == int(re.search(r"subscribeLeadWindows:\s*(\d+)", CONFIG_TS).group(1))
    num = lambda e: eval(e.replace("_", ""))  # noqa: E731  (e.g. "10 * 60_000")
    assert S.MIN_SUBSCRIBE_LEAD_MS == num(re.search(r"minSubscribeLeadMs:\s*([\d_ *]+),", CONFIG_TS).group(1))
    assert S.UNSUBSCRIBE_AFTER_END_MS == num(re.search(r"unsubscribeAfterEndMs:\s*([\d_ *]+),", CONFIG_TS).group(1))
    rec = re.search(r"const RECURRENCE_MS[^{]*\{(.*?)\};", CONFIG_TS, re.S).group(1)
    got = {k.strip("' "): num(v) for k, v in (p.split(":") for p in rec.replace("\n", "").split(",") if p.strip())}
    assert got == S.RECURRENCE_MS
    la = re.search(r"const LOOKAHEAD[^{]*\{(.*?)\}", POLY_TS).group(1)
    assert {k.strip("' "): int(v) for k, v in (p.split(":") for p in la.split(","))} == S.LOOKAHEAD
    pm = re.search(r"PER_MARKET_SERIES = new Set\(\[(.*?)\]\)", POLY_TS).group(1)
    assert set(re.findall(r"'([\w-]+)'", pm)) == set(S.PER_MARKET_SERIES)


def test_series_for_filters():
    got = [s.slug for s in S.series_for(["btc"], ["5m", "1h"])]
    assert got == ["btc-up-or-down-5m", "btc-up-or-down-hourly"]
    assert S.TF_OF == {"5m": "5m", "15m": "15m", "hourly": "1h", "4h": "4h", "daily": "1d"}

"""Post-hoc voids of a paper session (`paper.voids`): gaps from the session catalog, late rows, no outcome."""

from __future__ import annotations

import json
import math
import sys
from pathlib import Path

import pandas as pd

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bt"))

from synth import make_session, market_obj, slug_of  # noqa: E402
from test_live import H11, cfg_of  # noqa: E402

from polydata.bt.live import LiveRow  # noqa: E402
from polydata.paper import voids  # noqa: E402
from polydata.paper.session import SessionWriter  # noqa: E402

START = H11 - 20 * 60_000
RS = START + 60_000
END = H11
WS = [H11 - 15 * 60_000, H11 - 10 * 60_000, H11 - 5 * 60_000]  # admitted at rs, each opens within seconds
SLUGS = [slug_of("btc", "5m", ws) for ws in WS]


def make(tmp_path, *, events=(), late=(), api_trades=(), outcomes=("Up", "Up", "Up"), drop=None):
    """A session of three admitted 5m markets (rows from `make_session`), plus extra catalog events (`(ts, kind,
    source)`), late rows (`(LiveRow, late_by_ms)`) and Data API trades."""
    rows = make_session(31, START, 22, bn_ms=20, meta_lead_ms=4 * 60_000)
    if drop is not None:  # no Polymarket row of the slug in [lo, hi] (a silent feed)
        slug, lo, hi = drop
        rows = [r for r in rows if not (r.stream in ("pm_book", "pm_price_change", "pm_best_bid_ask", "pm_trade")
                                        and f"{slug}|" in r.values[1] and lo <= r.recv_ms <= hi)]
    root = tmp_path / "session"
    w = SessionWriter(root)
    for r in rows:
        w.write(r)
    for ts, kind, source in events:
        w.event(ts, kind, source, "test")
    for r, by in late:
        w.write_late(r, by)
    for r in api_trades:
        w.write(r)
    w.close()
    w.manifest(rs=RS, end_ms=END, clean_stop=True, admitted=[{"slug": s, "admitted_ms": RS} for s in SLUGS])
    mk = pd.DataFrame({"slug": SLUGS, "coin": "btc", "window_start_ms": WS, "window_end_ms": [x + 300_000 for x in WS],
                       "outcome": list(outcomes)}, dtype=object)
    return root, mk


def voided(root, mk):
    return dict(voids.session_voids(root, mk, cfg_of("noop"), END).itertuples(index=False))


def a_row(stream, slug):
    rows = make_session(31, START, 22, bn_ms=20, meta_lead_ms=4 * 60_000)
    return next(r for r in rows if r.stream == stream and (slug is None or f"{slug}|" in str(r.values[1])))


def test_clean_session_voids_nothing(tmp_path):
    root, mk = make(tmp_path)
    assert voided(root, mk) == {}


def test_voids_late_rows(tmp_path):
    """Late and future rows void a market only if received at or after the open with a ts before the window end (also
    when received after the end); non-Polymarket rows never."""
    s1, s2, s3 = SLUGS
    pc2 = a_row("pm_price_change", s2)
    pc3 = a_row("pm_price_change", s3)
    t2 = WS[1] + 60_000
    tick = LiveRow("pm_other", WS[2] + 90_000, {"event_type": "tick_size_change", "market": market_obj("btc", "5m", WS[2])[
        "conditionId"], "new_tick_size": "0.001", "timestamp": str(WS[2] + 80_000)})
    late = [
        (pc2._replace(recv_ms=t2), 3_000), (pc2._replace(recv_ms=t2 + 5), 4_500),  # s2: two rows, max 4500
        (tick, 1_200),  # s3 by its condition id
        (a_row("pm_book", s1)._replace(recv_ms=RS - 5_000), 13_000),  # s1: before admission/open: harmless
        (pc3._replace(recv_ms=WS[2] + 300_000 + 5), 900),  # s3: received after its end with an in-window ts: counts
        (pc2._replace(recv_ms=WS[1] + 300_000 + 5, values=(str(WS[1] + 300_000 + 1),) + tuple(pc2.values[1:])), 800),
        # ^ s2: a ts at or after its window end: harmless
        (a_row("bn_fut_bbo", None)._replace(recv_ms=t2), 50_000),  # never void (the router cannot produce them)
        (a_row("rtds_price", None)._replace(recv_ms=t2), 50_000),
    ]
    root, mk = make(tmp_path, late=late)
    v = voided(root, mk)
    assert v == {s2: "late data (2 rows, max 4500 ms)", s3: "late data (2 rows, max 1200 ms)"}


def test_late_opening_snapshot_before_open_does_not_void(tmp_path):
    """The Task 8 live case: the opening snapshot's old ts makes it late, at admission, before any row counts."""
    s1 = SLUGS[0]
    root, mk = make(tmp_path, late=[(a_row("pm_book", s1)._replace(recv_ms=RS + 1), 3_015)])
    # received at RS + 1, i.e. at or after admission but before the first accepted snapshot (the market's open)
    assert voided(root, mk) == {}
    root2, mk2 = make(tmp_path / "b", late=[(a_row("pm_book", s1)._replace(recv_ms=WS[0] + 120_000), 3_015)])
    assert list(voided(root2, mk2)) == [s1]  # an ordinary late row after the open voids


def test_voids_ws_gap_pm_slug_and_series(tmp_path):
    s1, s2, s3 = SLUGS
    # a per-market connection (`pm:<slug>`) closed for 10 s inside s2's window: only s2
    ev = [(WS[1] + 100_000, "ws_close", f"pm:{s2}"), (WS[1] + 110_000, "ws_open", f"pm:{s2}")]
    root, mk = make(tmp_path / "a", events=ev)
    assert voided(root, mk) == {s2: "pm gap 10000 ms"}
    # a gap of 4 s is below bt's threshold
    ev = [(WS[1] + 100_000, "ws_close", f"pm:{s2}"), (WS[1] + 104_000, "ws_open", f"pm:{s2}")]
    root, mk = make(tmp_path / "b", events=ev)
    assert voided(root, mk) == {}
    # a series connection: every market of the series overlapping the gap (the gap spans the s2/s3 boundary)
    ev = [(WS[2] - 3_000, "ws_close", "pm:btc-up-or-down-5m"), (WS[2] + 4_000, "ws_open", "pm:btc-up-or-down-5m")]
    root, mk = make(tmp_path / "c", events=ev)
    assert voided(root, mk) == {s2: "pm gap 7000 ms", s3: "pm gap 7000 ms"}
    # the whole Polymarket feed
    ev = [(WS[0] + 50_000, "ws_close", "polymarket"), (WS[0] + 60_000, "ws_open", "polymarket")]
    root, mk = make(tmp_path / "d", events=ev)
    assert voided(root, mk) == {s1: "pm gap 10000 ms"}


def test_voids_stall_recorder_events(tmp_path):
    """An event-loop stall (a recorder stop/start pair) is a gap on every stream."""
    ev = [(WS[1] + 100_000, "stop", "recorder"), (WS[1] + 107_000, "start", "recorder")]
    root, mk = make(tmp_path, events=ev)
    v = voided(root, mk)
    assert list(v) == [SLUGS[1]] and "pm gap 7000 ms" in v[SLUGS[1]] and "bn gap 7000 ms" in v[SLUGS[1]]


def test_voids_no_outcome(tmp_path):
    root, mk = make(tmp_path, outcomes=("Up", None, "Down"))
    assert voided(root, mk) == {SLUGS[1]: "no outcome"}


def test_voids_pm_silence_with_api_trades(tmp_path):
    """`gaps.pm_silences` over the session: no Polymarket row for > 5 s of a market's window while Data API trades
    happened in the silence (the rule needs `pm_api_trades`, fetched after the market ends)."""
    s2 = SLUGS[1]
    lo, hi = WS[1] + 100_000, WS[1] + 120_000
    mid = (lo + hi) // 2

    def trade(ts_ms):
        return LiveRow("pm_api_trades", WS[1] + 400_000, {
            "slug": s2, "condition_id": "c", "token_id": "t", "outcome": "Up", "outcome_index": 0, "side": "BUY",
            "price": 0.5, "size": 5, "timestamp": ts_ms // 1000, "proxy_wallet": "w", "transaction_hash": "h"})

    root, mk = make(tmp_path / "a", drop=(s2, lo, hi), api_trades=[trade(mid)])
    v = voided(root, mk)
    assert list(v) == [s2] and v[s2].startswith("pm gap")
    root, mk = make(tmp_path / "b", drop=(s2, lo, hi))  # the silence alone (no Data API trade inside) voids nothing
    assert voided(root, mk) == {}


def test_void_keeps_pnl_raw():
    mk = pd.DataFrame({"slug": ["a", "b", "c"], "voided": [False, False, True],
                       "reason": ["", "", "no outcome at settlement"], "pnl": [1.5, -2.0, math.nan]}, dtype=object)
    v = pd.DataFrame({"slug": ["b", "c"], "reason": ["pm gap 6000 ms", "bn gap 2000 ms"]})
    out = voids.apply(mk, v)
    assert list(out.pnl_raw.astype(float).fillna(99)) == [1.5, -2.0, 99.0]
    assert list(out.voided) == [False, True, True]
    assert list(out.reason) == ["", "pm gap 6000 ms", "no outcome at settlement; bn gap 2000 ms"]
    assert out.pnl.astype(float).isna().tolist() == [False, True, True]
    assert voids.apply(mk, pd.DataFrame(columns=["slug", "reason"])).pnl_raw.astype(float).tolist()[:2] == [1.5, -2.0]


def test_run_voids_after_a_stall(tmp_path, monkeypatch):
    """In a run: an event-loop stall (the wall clock leaps 7 s) voids the markets around it after the run: `voided`,
    `reason`, `pnl` NaN and `pnl_raw` kept in markets.parquet; results.json's P&L leaves them out."""
    from test_run import VClock, paper_run

    from polydata.paper.runner import PaperOpts

    start = H11 - 20 * 60_000
    vc = VClock(start, jump_at=H11 - 7 * 60_000, jump_ms=7_000)
    cfg, d, run, lines = paper_run(tmp_path, monkeypatch, start, 22, PaperOpts(tick_ms=50, duration_s=20 * 60), vc=vc)
    mk = pd.read_parquet(d / "markets.parquet").set_index("slug")
    stalled = mk[mk.voided]
    assert list(stalled.index) == [SLUGS[1]]  # [H11 - 10 min, H11 - 5 min) holds the stall
    assert "pm gap" in stalled.reason.iloc[0] and stalled.pnl.isna().all() and stalled.pnl_raw.notna().all()
    res = json.loads((d / "results.json").read_text())
    assert res["voided"] == 1 and res["void_reasons"].keys() == {SLUGS[1]}
    assert abs(res["pnl_usd"] - mk.loc[~mk.voided, "pnl"].astype(float).sum()) < 1e-9

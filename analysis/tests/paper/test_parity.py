"""`polydata paper replay` (`paper.parity`): pass/fail, faults through the fake feed, truncated sessions."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bt"))

from synth import slug_of  # noqa: E402
from test_fakefeed import START, fake_run  # noqa: E402
from test_live import H11  # noqa: E402

from polydata import cli as polydata_cli  # noqa: E402
from polydata.paper import parity  # noqa: E402

M1050 = H11 - 10 * 60_000  # the 10:50 market (admitted: 10:45, 10:50, 10:55)


def _replay_cli(d: Path, monkeypatch) -> int:
    monkeypatch.setenv("PYTHONHASHSEED", "0")  # no re-exec
    try:
        polydata_cli.main(["paper", "replay", str(d)])
    except SystemExit as e:
        return int(e.code or 0)
    return 0


def _markets(d: Path) -> pd.DataFrame:
    return pd.read_parquet(d / "markets.parquet").set_index("slug")


def test_parity_cli_pass_and_fail(tmp_path, monkeypatch):
    cfg, d, run, feed, vc = fake_run(tmp_path, monkeypatch)
    assert _replay_cli(d, monkeypatch) == 0
    p = json.loads((d / "parity.json").read_text())
    assert p["passed"] and p["verdict"] == "passed" and p["first_divergence"] is None
    assert p["tables"]["decisions"]["rows_paper"] > 1000 and p["tables"]["fills"]["rows_paper"] > 5
    assert json.loads((d / "results.json").read_text())["parity"]["verdict"] == "passed"
    assert (d / "parity" / "decisions.parquet").exists() and not (d / "session" / "parquet").exists()
    fills = pd.read_parquet(d / "fills.parquet")
    k = 3
    fills.loc[k, "px"] = fills.loc[k, "px"] + 0.001
    fills.to_parquet(d / "fills.parquet", index=False)
    assert _replay_cli(d, monkeypatch) == 1
    p = json.loads((d / "parity.json").read_text())
    assert not p["passed"] and p["verdict"] == "failed"
    assert p["tables"]["fills"]["mismatches"] == 1 and p["tables"]["decisions"]["mismatches"] == 0
    f = p["first_divergence"]
    assert (f["table"], f["budget"], f["index"]) == ("fills", "btc", k) and len(f["before"]) == 10
    assert json.loads((d / "results.json").read_text())["parity"]["passed"] is False


def test_fault_late_voids_market(tmp_path, monkeypatch):
    slug = slug_of("btc", "5m", M1050)
    lo = M1050 + 120_000

    def delay(feed):
        feed.delay("pm_price_change", lambda v: v[1].startswith(slug) and lo <= int(v[0]) < lo + 5_000, 15_000)

    cfg, d, run, feed, vc = fake_run(tmp_path, monkeypatch, feed_fn=delay)
    mk = _markets(d)
    assert "late data" in mk.loc[slug, "reason"] and mk.loc[slug, "voided"]
    assert not mk.drop(index=slug).voided.any()
    assert json.loads((d / "results.json").read_text())["late"] > 0
    assert parity.compare(d, parity.replay(d))["passed"]


def test_fault_drop_voids_market(tmp_path, monkeypatch):
    slug = slug_of("btc", "5m", M1050)
    a = M1050 + 120_000
    cfg, d, run, feed, vc = fake_run(tmp_path, monkeypatch, feed_fn=lambda f: f.drop(f"pm:{slug}", a, a + 10_000))
    mk = _markets(d)
    assert "pm gap" in mk.loc[slug, "reason"] and mk.loc[slug, "voided"]
    assert not mk.drop(index=slug).voided.any()
    assert parity.compare(d, parity.replay(d))["passed"]


def test_fault_stall_voids_all(tmp_path, monkeypatch):
    """A 5 s event-loop stall at 10:52: every market open then (btc and eth 10:50) is voided, the others are not."""
    at = M1050 + 120_000
    cfg, d, run, feed, vc = fake_run(tmp_path, monkeypatch, coins=("btc", "eth"), feed_fn=lambda f: f.stall(at, 5_000))
    mk = _markets(d)
    hit = {slug_of(c, "5m", M1050) for c in ("btc", "eth")}
    assert set(mk.index[mk.voided]) == hit and all("gap" in mk.loc[s, "reason"] for s in hit)
    assert any("stalled" in w for w in run.warnings)
    assert parity.compare(d, parity.replay(d))["passed"]


def test_truncated_session_passed_truncated(tmp_path, monkeypatch):
    """A hard stop (two SIGINTs) at 10:53: the session is truncated; decisions before the cut and the markets settled
    before it match the replay."""
    holder = {}
    stop_at = START + 13 * 60_000

    def extra(vc):
        async def stopper():
            vc.register()
            try:
                await vc.sleep_until(stop_at)
                holder["run"].request_stop()
                holder["run"].request_stop()
            finally:
                vc.unregister()
            await asyncio.Event().wait()

        return [stopper()]

    import polydata.paper.runner as runner_mod

    orig = runner_mod.PaperRun.__init__

    def init(self, *a, **k):
        orig(self, *a, **k)
        holder["run"] = self

    monkeypatch.setattr(runner_mod.PaperRun, "__init__", init)
    cfg, d, run, feed, vc = fake_run(tmp_path, monkeypatch, extra=extra)
    man = json.loads((d / "session" / "manifest.json").read_text())
    assert man["clean_stop"] is False and man["flushed_ms"] < stop_at
    p = parity.compare(d, parity.replay(d))
    assert p["passed"] and p["verdict"] == "passed-truncated" and p["cut_ms"] == man["flushed_ms"] + 1
    assert p["tables"]["decisions"]["rows_paper"] > 100 and p["tables"]["decisions"]["mismatches"] == 0
    assert p["tables"]["markets"]["rows_paper"] >= 1  # the 10:45 market settled before the stop
    assert p["tables"]["fills"].get("judged") is False
    assert p["tables"]["markets"]["replay_only_settled_before_cut"] >= 0
    assert p["tables"]["markets"]["rows_replay"] == p["tables"]["markets"]["rows_paper"]


def test_other_host_is_informational(tmp_path, monkeypatch):
    cfg, d, run, feed, vc = fake_run(tmp_path, monkeypatch)
    p = parity.compare(d, parity.replay(d), host="00000000")
    assert p["verdict"] == "informational" and not p["passed"]
    assert p["tolerance_mismatches"]["decisions"] == 0
    monkeypatch.setattr(parity.runs, "host_hash", lambda: "00000000")
    assert _replay_cli(d, monkeypatch) == 2  # informational: neither a pass nor a failure


def test_stale_opening_snapshot_opens_market(tmp_path, monkeypatch):
    """Ruling A (fix round 1): a quiet book. The 10:50 market's opening snapshot (Up and Down) is received 60 s after
    its timestamp and no other snapshot comes for 65 s: the late row is accepted (the market has not opened), written
    to the session's pm_book stream, and opens the market at its receipt; the session replay matches."""
    from synth import LiveRow, make_session, sort_rows, write_store

    slug = slug_of("btc", "5m", M1050)
    rows = make_session(5, START, 22, bn_ms=20, resolutions=True, meta_lead_ms=4 * 60_000)
    books = [r for r in rows if r.stream == "pm_book" and r.values[1].startswith(slug)]
    ts0 = min(int(r.values[0]) for r in books)
    first = [r for r in books if int(r.values[0]) == ts0]
    r0 = max(r.recv_ms for r in first)
    # the snapshot of a ts_ms is the Up row if there is one (else the Down row): it opens the market at its receipt
    snap = max(first, key=lambda r: (r.values[1].endswith("|U"), r.recv_ms))
    out = []
    for r in rows:
        if r.stream == "pm_book" and r.values[1].startswith(slug):
            if int(r.values[0]) == ts0:
                r = LiveRow(r.stream, r.recv_ms + 60_000, r.values)  # stale: ts ~60 s old at receipt
            elif r0 < r.recv_ms <= r0 + 65_000:
                continue  # a quiet book: no other snapshot meanwhile
        out.append(r)
    write_store(sort_rows(out), tmp_path / "store")
    cfg, d, run, feed, vc = fake_run(tmp_path, monkeypatch)
    res = json.loads((d / "results.json").read_text())
    assert res["router"]["late_accepted"] >= 1 and slug not in (res["never_opened"] or [])
    dec = pd.read_parquet(d / "decisions.parquet")
    opened = dec[(dec.call == "on_market_open") & (dec.key == slug)]
    assert list(opened.now) == [snap.recv_ms + 60_000]
    mk = _markets(d)
    assert not mk.loc[slug, "voided"]
    parity.build_session(d / "session")
    import duckdb

    n = duckdb.connect().execute(
        f"SELECT count(*) FROM read_parquet('{(d / 'session' / 'parquet').as_posix()}/pm_book/*/*.parquet') "
        "WHERE asset LIKE ? AND ts_ms = ?", [slug + "|%", ts0]).fetchall()[0][0]
    assert n == len(first)  # in the session's pm_book stream, not late/
    assert parity.compare(d, parity.replay(d))["passed"]


def test_late_row_after_open_still_voids(tmp_path, monkeypatch):
    """A late row received after the market opened is dropped to late/ and voids it (unchanged by ruling A)."""
    slug = slug_of("btc", "5m", M1050)
    lo = M1050 + 120_000  # long after the 10:50 market opened (~10:46)

    def delay(feed):
        feed.delay("pm_book", lambda v: v[1].startswith(slug) and lo <= int(v[0]) < lo + 10_000, 15_000)

    cfg, d, run, feed, vc = fake_run(tmp_path, monkeypatch, feed_fn=delay)
    mk = _markets(d)
    assert "late data" in mk.loc[slug, "reason"] and mk.loc[slug, "voided"]
    res = json.loads((d / "results.json").read_text())
    assert res["late"] > 0
    assert parity.compare(d, parity.replay(d))["passed"]


def test_vs_recorder_synthetic(tmp_path, monkeypatch):
    """The same rows replayed by bt from the store: no decision differences (no stale snapshots, no gaps); and order
    ids renumbered per market for the attribution."""
    from polydata.build import build

    cfg, d, run, feed, vc = fake_run(tmp_path, monkeypatch)
    build()  # the source store
    man = json.loads((d / "session" / "manifest.json").read_text())
    rec = parity.recorder_run(cfg, man["rs"], man["end_ms"])
    v = parity.vs_recorder(d, rec)
    assert v["decision_differences"] == 0 and v["fills"]["only_paper"] == v["fills"]["only_recorder"] == 0
    assert v["explained"]["all_explained"] and v["explained"]["open_differs"] == []
    assert (d / "parity" / "vs_recorder.json").exists()
    dec = pd.read_parquet(d / "decisions.parquet")
    n = parity._norm_orders(dec, pd.read_parquet(d / "orders.parquet"), "btc")
    placed = n[n.call.isin(["place", "take"])].ret
    assert len(placed) and placed.astype(str).str.match(r"btc-updown-5m-\d+#\d+$").all()


if __name__ == "__main__":
    sys.exit(pytest.main([__file__, "-q"]))

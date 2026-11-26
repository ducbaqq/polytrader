"""`polydata paper run` end to end on synthetic rows under a virtual clock (no network)."""

from __future__ import annotations

import asyncio
import json
import os
import sys
from pathlib import Path

import duckdb
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bt"))

from synth import make_session  # noqa: E402
from test_live import H11, cfg_of  # noqa: E402,F401  (registers the parity-probe strategies)

from polydata.bt import runs  # noqa: E402
from polydata.bt.core import MARKET_COLS  # noqa: E402
from polydata.bt.engine import SessionPlan, run_session  # noqa: E402
from polydata.build import build  # noqa: E402
from polydata.paper import cli as paper_cli  # noqa: E402
from polydata.paper.runner import PaperOpts, PaperRefused, PaperRun  # noqa: E402
from polydata.views import create_views  # noqa: E402

RUN_FILES = ["config.yaml", "provenance.json", "results.json", "orders.parquet", "fills.parquet", "equity.parquet",
             "markets.parquet", "decisions.parquet", "log.txt", "session/manifest.json"]


class VClock:
    """Virtual wall clock: the runner's sleep moves the target, the fake feed emits every row up to it at the row's
    recv_ms (so stamps equal the recorded recv_ms), then the clock jumps to the target."""

    def __init__(self, t0: int):
        self.now = self.target = int(t0)

    def __call__(self) -> int:
        return self.now

    async def sleep(self, dt: float) -> None:
        self.target = self.now + int(round(dt * 1000))
        await asyncio.sleep(0)
        self.now = max(self.now, self.target)


def fake_feeds(rows, vc: VClock):
    def factory(ingest, clock):
        async def run():
            for src in ("polymarket-rtds-a", "binance-fut-bbo", "binance-fut-market", "pm:btc-up-or-down-5m"):
                ingest.on_event("ws_open", src, "")
            i, n = 0, len(rows)
            while True:
                while i < n and rows[i].recv_ms <= vc.target:
                    vc.now = max(vc.now, rows[i].recv_ms)
                    ingest.on_rows([(rows[i].stream, rows[i].values)])
                    i += 1
                await asyncio.sleep(0)

        return [run()]

    return factory


def paper_run(tmp_path, monkeypatch, start, minutes, opts, *, strategy="parity-probe", out=None, rows=None, seed=5):
    monkeypatch.setenv("POLYDATA_DIR", str(tmp_path / "data"))
    if rows is None:
        rows = make_session(seed, start, minutes, bn_ms=20, resolutions=False, meta_lead_ms=4 * 60_000)
    cfg = cfg_of(strategy)
    vc = VClock(start)
    if out is None:
        out = runs.new_run_dir("test", strategy, pd.Timestamp(start, unit="ms", tz="UTC").to_pydatetime())
    lines = []
    run = PaperRun(cfg, out, opts, fake_feeds(rows, vc), clock=vc, sleep=vc.sleep, echo=lines.append)
    d = asyncio.run(run.run())
    return cfg, d, run, lines


def replay_and_compare(cfg, d: Path, monkeypatch):
    sess = d / "session"
    monkeypatch.setenv("POLYDATA_DIR", str(sess))
    build()
    man = json.loads((sess / "manifest.json").read_text())

    def con_factory():
        con = duckdb.connect()
        con.execute("SET TimeZone = 'UTC'")
        create_views(con, parquet=sess / "parquet", include_sealed=True)
        return con

    plan = SessionPlan(man["rs"], man["end_ms"], [(a["slug"], a["admitted_ms"]) for a in man["admitted"]])
    rep = run_session(cfg, con_factory, plan)
    tmp = d.parent / (d.name + "-replay")
    tmp.mkdir()
    for name in ("decisions", "orders", "fills", "equity"):
        getattr(rep, name).to_parquet(tmp / f"{name}.parquet", index=False)
        a, b = pd.read_parquet(d / f"{name}.parquet"), pd.read_parquet(tmp / f"{name}.parquet")
        assert a.equals(b), name
    a = pd.read_parquet(d / "markets.parquet")[MARKET_COLS + ["budget"]]
    rep.markets.to_parquet(tmp / "markets.parquet", index=False)
    b = pd.read_parquet(tmp / "markets.parquet")[MARKET_COLS + ["budget"]]
    assert a.equals(b)
    strategy_lines = [x for x in (d / "log.txt").read_text().splitlines() if not x.startswith("#")]
    assert strategy_lines == list(map(str, rep.log))
    return rep, man


def test_paper_run_fake_rows_end_to_end(tmp_path, monkeypatch):
    start = H11 - 20 * 60_000  # 10:40 UTC on 2026-11-24 (train)
    opts = PaperOpts(tick_ms=50, duration_s=20 * 60)
    cfg, d, run, lines = paper_run(tmp_path, monkeypatch, start, 22, opts)
    for f in RUN_FILES:
        assert (d / f).exists(), f
    assert not (d / "paper.lock").exists()
    prov = json.loads((d / "provenance.json").read_text())
    assert prov["kind"] == "paper" and len(prov["paper_version"]) == 12 and prov["session_fingerprint"]
    man = json.loads((d / "session" / "manifest.json").read_text())
    rs = start + 60_000
    assert man["rs"] == rs and man["end_ms"] == rs + 20 * 60_000 and man["clean_stop"] is True
    want = [f"btc-updown-5m-{(H11 + k * 300_000) // 1000}" for k in (-3, -2, -1)]
    assert [a["slug"] for a in man["admitted"]] == want
    res = json.loads((d / "results.json").read_text())
    assert res["verdict"] is None and res["parity"] is None and res["late"] == 0 and res["future"] >= 0
    assert res["markets"] == 3 and res["resolutions"] == {"paper-chainlink": 3} and res["unresolved"] == 0
    assert res["rows"]["bn_fut_bbo"] > 0 and "pm_price_change" in res["feed_delay_ms"]
    assert res["processing_lag_ms"]["p99"] is not None
    mk = pd.read_parquet(d / "markets.parquet")
    assert set(mk.outcome_source) == {"paper-chainlink"} and mk.admitted_ms.notna().all()
    assert mk.pnl_raw.equals(mk.pnl) and not mk.voided.any()
    assert len(pd.read_parquet(d / "decisions.parquet")) > 100
    assert any(x.startswith("# open ") for x in (d / "log.txt").read_text().splitlines())
    idx = runs.rebuild_index()
    assert list(idx[idx.run_id == d.name].kind) == ["paper"]
    rep, _ = replay_and_compare(cfg, d, monkeypatch)
    assert len(rep.fills) > 0


def test_session_without_resolutions(tmp_path, monkeypatch):
    """Obligations 3/15: the only admitted market has its start window before rs, so the settler gives up, the
    session has no pm_resolution file, and build, the markets view and the session replay still work."""
    start = H11 - 16 * 60_000 - 30_000  # 10:43:30 -> rs 10:44:30
    opts = PaperOpts(tick_ms=50, duration_s=6 * 60)
    cfg, d, run, _ = paper_run(tmp_path, monkeypatch, start, 8, opts, strategy="buy-and-hold-up")
    assert not list((d / "session" / "raw").rglob("pm_resolution.*"))
    man = json.loads((d / "session" / "manifest.json").read_text())
    assert [a["slug"] for a in man["admitted"]] == [f"btc-updown-5m-{(H11 - 900_000) // 1000}"]
    rep, _ = replay_and_compare(cfg, d, monkeypatch)
    mk = pd.read_parquet(d / "markets.parquet")
    assert list(mk.reason) == ["no outcome at settlement"] and mk.outcome_source.isna().all()
    assert "session start" in next(iter(run.settler.gave_up.values()))


def test_refuses_sealed_day(tmp_path, monkeypatch):
    sealed = 1_795_600_800_000  # 2026-11-25 10:00 UTC (sealed)
    out = tmp_path / "run"
    with pytest.raises(PaperRefused, match="sealed"):
        paper_run(tmp_path, monkeypatch, sealed, 1, PaperOpts(duration_s=60), out=out, rows=[])
    looks = tmp_path / "data" / "runs" / "looks.jsonl"
    assert not looks.exists()
    opts = PaperOpts(tick_ms=100, duration_s=60, allow_sealed=True, purpose="x")
    _, d, run, _ = paper_run(tmp_path, monkeypatch, sealed, 1, opts, out=tmp_path / "run2", rows=[])
    recs = [json.loads(x) for x in looks.read_text().splitlines()]
    assert len(recs) == 1 and recs[0]["purpose"] == "paper" and recs[0]["detail"]["purpose"] == "x"
    assert json.loads((d / "session" / "manifest.json").read_text())["clean_stop"] is True


def test_admit_filter_and_sealed_stop():
    from polydata.paper.runner import next_sealed_start, touches_sealed
    from test_settle import info

    day25 = 1_795_564_800_000  # 2026-11-25 00:00 UTC
    assert next_sealed_start(day25 - 3_600_000) == day25
    assert touches_sealed(info(ws=day25 - 300_000, we=day25 + 1)) and not touches_sealed(info(ws=day25 - 300_000,
                                                                                             we=day25))
    run = PaperRun(cfg_of("noop"), Path("/nonexistent"), PaperOpts(duration_s=600), lambda i, c: [])
    run.rs = day25 - 3_600_000
    assert run.admit_filter(info(ws=run.rs, we=run.rs + 600_000), run.rs)
    assert not run.admit_filter(info(ws=run.rs, we=run.rs + 600_001), run.rs)


def test_refuses_locked_out_dir(tmp_path, monkeypatch):
    out = tmp_path / "run"
    out.mkdir()
    (out / "paper.lock").write_text(str(os.getppid()))  # a live process
    with pytest.raises(PaperRefused, match="another paper run"):
        paper_run(tmp_path, monkeypatch, H11, 1, PaperOpts(duration_s=60), out=out, rows=[])


def test_push_skips_sessions(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYDATA_DIR", str(tmp_path))
    r = runs.runs_dir() / "fam" / "strat" / "2026-11-26T1000-abcdef"
    for rel in ("results.json", "markets.parquet", "session/manifest.json", "session/raw/2026-11-26/10/x.tsv.zst",
                "paper.lock"):
        (r / rel).parent.mkdir(parents=True, exist_ok=True)
        (r / rel).write_text("x")
    names = lambda fs: sorted(f.relative_to(r).as_posix() for f in fs)  # noqa: E731
    assert names(runs._shared_files()) == ["markets.parquet", "results.json"]
    assert names(runs._shared_files(with_sessions=True)) == ["markets.parquet", "results.json", "session/manifest.json",
                                                             "session/raw/2026-11-26/10/x.tsv.zst"]


def test_reexec_sets_hashseed(monkeypatch):
    calls = []

    class Exec(Exception):
        pass

    def fake(path, args, env):
        calls.append((path, args, env))
        raise Exec

    monkeypatch.delenv("PYTHONHASHSEED", raising=False)
    monkeypatch.setattr(paper_cli.os, "execvpe", fake)
    with pytest.raises(Exec):
        paper_cli.main(["paper", "run", "x.yaml", "--duration", "30m"])
    (path, args, env), = calls
    assert path == sys.executable and args == [sys.executable, "-m", "polydata.cli", "paper", "run", "x.yaml",
                                               "--duration", "30m"]
    assert env["PYTHONHASHSEED"] == "0"
    calls.clear()
    monkeypatch.setenv("PYTHONHASHSEED", "0")
    paper_cli.ensure_hashseed(["paper"])
    assert calls == []


def test_cli_parses_flags(tmp_path):
    cfgf = tmp_path / "c.yaml"
    cfgf.write_text("strategy: noop\ncoins: [btc]\ndays: ['2026-11-24']\n"
                    "paper:\n  hold_back_ms: 12000\n  max_drain: 5m\n")
    cfg, paper = paper_cli.load_paper_config(str(cfgf), ["btc", "eth"], ["seed=3"])
    assert cfg.coins == ["btc", "eth"] and cfg.seed == 3 and cfg.days is None
    import argparse

    ap = argparse.ArgumentParser()
    paper_cli.register(ap.add_subparsers())
    a = ap.parse_args(["paper", "run", str(cfgf), "--duration", "30m", "--skew-ms", "900"])
    o = paper_cli.build_opts(a, paper)
    assert (o.hold_back_ms, o.skew_ms, o.max_drain_s, o.duration_s, o.allow_sealed) == (12000, 900, 300, 1800, False)

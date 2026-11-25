"""Run folders, versions, the runs index, final-run refusals and push/pull (mocked client)."""

from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path

import pandas as pd
import pytest
from test_engine import M1, T0, cfg, loader_for, make_con, market_one, NO_EVENTS

from polydata.bt import engine, runs, validate
from polydata.bt.config import RunConfig
from polydata.bt.engine import CoinResult, RunResult
from polydata.bt.strategy import get_strategy


@pytest.fixture
def pdir(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYDATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYDATA_RUNS_CSV", str(tmp_path / "runs-index.csv"))  # never touch docs/runs-index.csv
    return tmp_path


def build_manifests(pdir, days, body="pm_trade.tsv.zst:10"):
    for d in days:
        f = pdir / "parquet" / "pm_trade" / f"date={d}"
        f.mkdir(parents=True, exist_ok=True)
        (f / "10.src").write_text("# format 2\n" + body)


def fake_result(c: RunConfig, pnls=(1.5, -0.25, None)) -> RunResult:
    day = 1_795_519_800_000
    mk = pd.DataFrame([dict(slug=f"m{i}", coin="btc", timeframe="5m", window_start_ms=day + i * 300_000,
                            window_end_ms=day + (i + 1) * 300_000, voided=p is None, reason="" if p is not None else "gap",
                            pnl=float("nan") if p is None else p, settle_ms=float("nan"), outcome="Up")
                       for i, p in enumerate(pnls)])
    orders = pd.DataFrame({c_: [] for c_ in engine.ORDER_COLS})
    fills = pd.DataFrame({c_: [] for c_ in engine.FILL_COLS})
    equity = pd.DataFrame({c_: [] for c_ in engine.EQUITY_COLS})
    return RunResult(c, {"btc": CoinResult(orders, fills, equity, mk, ["hello"])})


def synthetic_run() -> RunResult:
    c = cfg(strategy="join-best")
    res = engine.run_coin(c, "btc", lambda: make_con([M1], T0 - 1_200_000, T0 + 500_000),
                          loader=loader_for({M1.slug: market_one()}), rec_events=NO_EVENTS)
    return RunResult(c, {"btc": res})


def put_run(root: Path, family="sanity", strategy="noop", rid="2026-11-24T1100-aaaaaa", *, kind="run", version="v1",
            sim="s1", fp="f1", pnl=2.0, tags=(), days=("2026-11-24",), verdict=None) -> Path:
    d = root / "runs" / family / strategy / rid
    d.mkdir(parents=True)
    (d / "config.yaml").write_text(
        "strategy: %s\ncoins: [btc]\ntimeframes: [5m]\norder_latency_ms: 200\ntags: %s\n" % (strategy, json.dumps(list(tags))))
    (d / "provenance.json").write_text(json.dumps({"kind": kind, "strategy_version": version, "sim_version": sim,
                                                   "data_fingerprint": fp, "family": family}))
    (d / "results.json").write_text(json.dumps({"pnl_usd": pnl, "usd_per_day": pnl / len(days), "days": list(days),
                                                 "n_days": len(days), "verdict": verdict, "t_block": None}))
    return d


# ---------------------------------------------------------------------------------------------------

def test_version_changes_with_params():
    cls = get_strategy("join-best")
    a, b = runs.strategy_version(cls, {"size": 5}), runs.strategy_version(cls, {"size": 6})
    assert a != b and len(a) == 12
    assert a == runs.strategy_version(cls, {"size": 5})
    assert runs.strategy_version(cls, {"a": 1, "b": 2}) == runs.strategy_version(cls, {"b": 2, "a": 1})
    assert runs.strategy_version(get_strategy("noop"), {}) != runs.strategy_version(cls, {})


def test_run_dir_name_format(pdir):
    now = datetime(2026, 11, 24, 13, 5, 59, tzinfo=timezone.utc)
    d = runs.new_run_dir("sanity", "noop", now)
    assert d.parent == pdir / "runs" / "sanity" / "noop" and d.is_dir()
    assert d.name[:15] == "2026-11-24T1305" and d.name[15] == "-" and len(d.name) == 22
    int(d.name[16:], 16)
    assert runs.new_run_dir("sanity", "noop", now) != d


def test_results_json_deterministic(pdir):
    blobs = []
    for i in range(2):
        res = synthetic_run()
        d = runs.new_run_dir("sanity", "join-best", datetime(2026, 11, 24, 13, 5, tzinfo=timezone.utc))
        runs.write_run(d, res.cfg, res, runs.summary(res, res.cfg), {"kind": "run", "host": runs.host_hash()})
        blobs.append((d / "results.json").read_bytes())
        for f in ("config.yaml", "provenance.json", "orders.parquet", "equity.parquet", "log.txt"):
            assert (d / f).exists()
    assert blobs[0] == blobs[1]
    s = json.loads(blobs[0])
    assert s["orders"] > 0 and s["fills"] > 0 and s["n_days"] == 1 and s["day_kinds"] == {"train": 1}
    assert b"/Users" not in blobs[0] and b"utc" not in blobs[0].lower()


def test_summary_counts(pdir):
    res = fake_result(cfg())
    s = runs.summary(res, res.cfg)
    assert s["pnl_usd"] == 1.25 and s["voided"] == 1 and s["markets"] == 3 and s["fills"] == 0
    assert s["days"] == ["2026-11-24"] and s["usd_per_day"] == 1.25


def test_provenance_host_is_hashed(pdir, monkeypatch):
    monkeypatch.setattr("platform.node", lambda: "my-secret-host.example")
    build_manifests(pdir, ["2026-11-24"])
    h = runs.host_hash()
    assert len(h) == 8 and "secret" not in h
    p = runs.provenance(get_strategy("noop"), {}, ["2026-11-24"], "run")
    assert p["host"] == h and "my-secret-host" not in json.dumps({k: v for k, v in p.items() if k != "git_diff"})  # the diff may quote this test
    assert {"git_commit", "sim_version", "strategy_version", "data_fingerprint", "python", "packages"} <= set(p)


def test_data_fingerprint(pdir):
    with pytest.raises(ValueError, match="2026-11-24"):
        runs.data_fingerprint(["2026-11-24"])
    (pdir / "raw" / "2026-11-24" / "10").mkdir(parents=True)  # raw files are not used
    (pdir / "raw" / "2026-11-24" / "10" / "x.zst").write_bytes(b"x")
    with pytest.raises(ValueError):
        runs.data_fingerprint(["2026-11-24"])
    build_manifests(pdir, ["2026-11-24"])
    a = runs.data_fingerprint(["2026-11-24"])
    assert a == runs.data_fingerprint(["2026-11-24"]) and len(a) == 64
    build_manifests(pdir, ["2026-11-24"], "pm_trade.tsv.zst:11")
    assert runs.data_fingerprint(["2026-11-24"]) != a


def test_index_rebuild_and_csv(pdir):
    put_run(pdir, rid="2026-11-24T1100-aaaaaa", tags=["a", "b"], pnl=2.0)
    put_run(pdir, "mm", "x", "2026-11-24T1200-bbbbbb", kind="sweep", pnl=-1.0, days=("2026-11-24", "2026-11-26"))
    (pdir / "runs" / "validation").mkdir()
    (pdir / "runs" / "validation" / "abc.json").write_text("{}")
    csv = pdir / "runs-index.csv"
    df = runs.rebuild_index(csv_path=csv)
    assert list(df.columns) == runs.INDEX_COLUMNS
    assert list(df.run_id) == ["2026-11-24T1100-aaaaaa", "2026-11-24T1200-bbbbbb"]
    r = df.iloc[0]
    assert r.created_utc == "2026-11-24T11:00:00Z" and r.tags == "a,b" and r.path == "sanity/noop/2026-11-24T1100-aaaaaa"
    assert df.iloc[1].kind == "sweep" and df.iloc[1].n_days == 2
    assert (pdir / "runs" / "index.parquet").exists()
    back = pd.read_csv(csv)
    assert list(back.columns) == runs.INDEX_COLUMNS and len(back) == 2
    pd.testing.assert_frame_equal(pd.read_parquet(pdir / "runs" / "index.parquet"), df, check_dtype=False)
    # derived: rebuilding again gives the same rows
    pd.testing.assert_frame_equal(runs.rebuild_index(csv_path=csv), df)


def test_index_empty_has_header(pdir):
    csv = pdir / "i.csv"
    df = runs.rebuild_index(csv_path=csv)
    assert df.empty and csv.read_text().strip() == ",".join(runs.INDEX_COLUMNS)


def sealed_days(n=5):
    return [d.isoformat() for d in pd.date_range("2026-11-25", periods=3 * n, freq="D").date if
            __import__("polydata.bt.splits", fromlist=["x"]).day_kind(d) == "sealed"][:n]


def mark_validated(pdir, ok=True):
    p = validate.validation_path(validate.sim_version())
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps({"passed": ok}))


def test_final_refused_without_validation(pdir):
    cls = get_strategy("noop")
    with pytest.raises(runs.FinalRefused, match="validation"):
        runs.final_preflight(cls, {}, sealed_days())
    mark_validated(pdir, ok=False)
    with pytest.raises(runs.FinalRefused, match="validation"):
        runs.final_preflight(cls, {}, sealed_days())
    mark_validated(pdir)
    build_manifests(pdir, sealed_days())
    runs.final_preflight(cls, {}, sealed_days())
    with pytest.raises(runs.FinalRefused, match="sealed days"):
        runs.final_preflight(cls, {}, sealed_days(4))


def test_final_refused_when_repeated(pdir):
    mark_validated(pdir)
    cls, days = get_strategy("noop"), sealed_days()
    build_manifests(pdir, days)
    v, fp, sim = runs.strategy_version(cls, {}), runs.data_fingerprint(days), validate.sim_version()
    assert runs.final_preflight(cls, {}, days).empty
    put_run(pdir, kind="final", version=v, sim=sim, fp=fp, rid="2026-11-30T1000-cccccc")
    with pytest.raises(runs.FinalRefused, match="already"):  # no manual rebuild: preflight refreshes the index
        runs.final_preflight(cls, {}, days)
    # a different simulator version or fingerprint allows another final, and lists the earlier one
    idx = pd.read_parquet(pdir / "runs" / "index.parquet")
    validate.validation_path("other").write_text(json.dumps({"passed": True}))
    other = runs.final_preflight(cls, {}, days, sim_ver="other")
    assert list(other["run_id"]) == ["2026-11-30T1000-cccccc"]
    assert len(idx) == 1


def test_final_looks_counts_looks_jsonl_excluding_health(pdir):
    assert runs.final_looks() == 0
    put_run(pdir, kind="final", rid="2026-11-30T1000-cccccc")  # no rebuild here: final_looks must not trust a cache
    put_run(pdir, kind="run", rid="2026-11-30T1100-dddddd")
    (pdir / "runs" / "looks.jsonl").write_text("\n".join(json.dumps({"purpose": p}) for p in
                                                           ["health", "health", "inspect", "final"]) + "\n\n")
    assert runs.final_looks() == 1 + 1


class FakeS3:
    def __init__(self, objs=None):
        self.objs = dict(objs or {})
        self.uploaded, self.downloaded = [], []

    def get_paginator(self, name):
        outer = self

        class P:
            def paginate(self, Bucket, Prefix):
                yield {"Contents": [{"Key": k, "Size": len(v)} for k, v in outer.objs.items() if k.startswith(Prefix)]}
        return P()

    def upload_file(self, path, bucket, key):
        self.uploaded.append(key)
        self.objs[key] = Path(path).read_bytes()

    def download_file(self, bucket, key, path):
        self.downloaded.append(key)
        Path(path).write_bytes(self.objs[key])


def test_push_pull_roundtrip(pdir, tmp_path_factory, monkeypatch):
    put_run(pdir, rid="2026-11-24T1100-aaaaaa")
    (pdir / "runs" / "looks.jsonl").write_text("{}\n")
    (pdir / "runs" / "index.parquet").write_bytes(b"x")
    s3 = FakeS3()
    runs.push(client=s3, bucket_name="b", csv_path=pdir / "i.csv")
    assert "runs/sanity/noop/2026-11-24T1100-aaaaaa/results.json" in s3.objs
    assert not any(k.endswith("looks.jsonl") or k.endswith("index.parquet") for k in s3.objs)
    n = len(s3.uploaded)
    runs.push(client=s3, bucket_name="b", csv_path=pdir / "i.csv")
    assert len(s3.uploaded) == n  # nothing new to upload
    # another machine
    other = tmp_path_factory.mktemp("other")
    monkeypatch.setenv("POLYDATA_DIR", str(other))
    runs.pull(client=s3, bucket_name="b", csv_path=other / "i.csv")
    assert (other / "runs" / "sanity" / "noop" / "2026-11-24T1100-aaaaaa" / "results.json").exists()
    assert list(pd.read_parquet(other / "runs" / "index.parquet").run_id) == ["2026-11-24T1100-aaaaaa"]


# ------------------------------------------------------------------------------------------------ CLI

def test_cli_parsing(tmp_path):
    from polydata.bt import cli

    assert cli.parse_days(["2026-11-24", "2026-11-26..2026-11-28"]) == ["2026-11-24", "2026-11-26", "2026-11-27", "2026-11-28"]
    d = {"strategy": "join-best"}
    cli.apply_set(d, "capital", 10)
    cli.apply_set(d, "size", 7)
    cli.apply_set(d, "params.stop_s", 9)
    assert d == {"strategy": "join-best", "capital": 10, "params": {"size": 7, "stop_s": 9}}
    f = tmp_path / "c.yaml"
    f.write_text("strategy: join-best\ncoins: [btc]\ndays: [2026-11-24]\nparams: {size: 5}\n")
    c = cli.load_config(str(f), None, ["eth"], ["size=8", "capital=100"])
    assert c.coins == ["eth"] and c.params == {"size": 8} and c.capital == 100 and c.days == ["2026-11-24"]


def test_cli_final_refused_end_to_end(pdir, monkeypatch):
    from polydata.cli import main

    monkeypatch.setattr(runs, "pull", lambda *a, **k: 0)  # never touch the real bucket from a test

    (pdir / "c.yaml").write_text("strategy: noop\ncoins: [btc]\n")
    with pytest.raises(SystemExit, match="--final refused"):
        main(["bt", "run", str(pdir / "c.yaml"), "--final"])
    assert not (pdir / "runs" / "looks.jsonl").exists()  # a refused final records no look


def test_cli_runs_filters(pdir, capsys):
    from polydata.cli import main

    put_run(pdir, rid="2026-11-24T1100-aaaaaa", tags=["sweep:x"], pnl=2.0)
    put_run(pdir, "mm", "x", "2026-11-24T1200-bbbbbb", pnl=-1.0, verdict="PASS")
    runs.rebuild_index(csv_path=pdir / "i.csv")
    for args, expect in [(["--strategy", "x"], "bbbbbb"), (["--family", "sanity"], "aaaaaa"), (["--tag", "sweep:x"], "aaaaaa"),
                         (["--search", "NOOP"], "aaaaaa"), (["--passed"], "bbbbbb")]:
        main(["bt", "runs", *args])
        out = capsys.readouterr().out
        assert expect in out and ("bbbbbb" if expect == "aaaaaa" else "aaaaaa") not in out


def test_one_final_run_counts_once(pdir):
    put_run(pdir, kind="final", rid="2026-11-30T1000-cccccc")
    runs.rebuild_index(csv_path=pdir / "i.csv")
    (pdir / "runs" / "looks.jsonl").write_text(json.dumps({"purpose": "final"}) + "\n")
    assert runs.final_looks() == 1


def test_looks_merge_across_hosts_and_dedupe(pdir):
    own = {"utc": "u1", "purpose": "inspect", "detail": {"a": 1}}
    (pdir / "runs").mkdir()
    (pdir / "runs" / "looks.jsonl").write_text(json.dumps(own) + "\n")
    (pdir / "runs" / "looks").mkdir()
    (pdir / "runs" / "looks" / "aaaaaaaa.jsonl").write_text(
        json.dumps(own) + "\n" + json.dumps({"utc": "u2", "purpose": "inspect", "detail": {}}) + "\n"
        + json.dumps({"utc": "u3", "purpose": "health", "detail": {}}) + "\n")
    assert runs.final_looks() == 2


def test_push_shares_looks_per_host(pdir):
    (pdir / "runs").mkdir()
    (pdir / "runs" / "looks.jsonl").write_text("{}\n")
    s3 = FakeS3()
    runs.push(client=s3, bucket_name="b")
    assert f"runs/looks/{runs.host_hash()}.jsonl" in s3.objs and "runs/looks.jsonl" not in s3.objs


def test_pull_rejects_escaping_keys(pdir):
    s3 = FakeS3({"runs/../evil.txt": b"x", "runs/a/b/c/results.json": b"{}"})
    runs.pull(client=s3, bucket_name="b")
    assert not (pdir / "evil.txt").exists() and "runs/../evil.txt" not in s3.downloaded


def test_new_run_dir_retries_on_collision(pdir, monkeypatch):
    toks = iter(["aaaaaa", "aaaaaa", "bbbbbb"])
    monkeypatch.setattr(runs.secrets, "token_hex", lambda n: next(toks))
    now = datetime(2026, 11, 24, 13, 5, tzinfo=timezone.utc)
    assert runs.new_run_dir("f", "s", now).name.endswith("aaaaaa")
    assert runs.new_run_dir("f", "s", now).name.endswith("bbbbbb")


def test_final_refused_when_day_not_built(pdir):
    mark_validated(pdir)
    with pytest.raises(runs.FinalRefused, match="not built: run polydata build"):
        runs.final_preflight(get_strategy("noop"), {}, sealed_days())


def test_provenance_without_manifests_records_null_fingerprint(pdir):
    p = runs.provenance(get_strategy("noop"), {}, ["2026-11-24"], "run")
    assert p["data_fingerprint"] is None
    with pytest.raises(ValueError):
        runs.provenance(get_strategy("noop"), {}, ["2026-11-24"], "final")


def test_push_skips_tmp_files(pdir):
    d = put_run(pdir)
    (d / "results.json.123.tmp").write_text("x")
    s3 = FakeS3()
    runs.push(client=s3, bucket_name="b")
    assert not any(k.endswith(".tmp") for k in s3.objs) and any(k.endswith("results.json") for k in s3.objs)


def test_apply_set_rejects_unknown_keys(tmp_path):
    from polydata.bt import cli

    d = {"strategy": "join-best"}
    with pytest.raises(ValueError, match="sizee"):
        cli.apply_set(d, "sizee", 7)  # a typo is neither a RunConfig field nor a join-best param
    cli.apply_set(d, "params.anything", 1)  # explicit params.x is always allowed
    assert d["params"] == {"anything": 1}
    f = tmp_path / "c.yaml"
    f.write_text("strategy: join-best\ncoins: [btc]\n")
    with pytest.raises(SystemExit, match="sizee"):
        cli.load_config(str(f), None, None, ["sizee=8"])


@pytest.mark.parametrize("field, value", [("order_latency_ms", 100), ("latency_jitter_ms", 50),
                                          ("fill_report_latency_ms", 50), ("cancel_model", "proportional"),
                                          ("impact_reset_ms", 1000), ("capital", 1e6), ("capital_mode", "global"),
                                          ("redeem_delay_s", 0), ("max_gap_ms_pm", 60_000), ("max_gap_ms_bn", 60_000),
                                          ("bn_step_ms", 100), ("min_usd_per_day", 0.0)])
def test_final_refuses_loosened_settings(field, value):
    c = RunConfig(strategy="join-best", **{field: value})
    with pytest.raises(runs.FinalRefused, match=field):
        runs.check_final_config(c)


def test_final_allows_params_coins_timeframes_tags_seed():
    runs.check_final_config(RunConfig(strategy="join-best", params={"size": 9}, coins=["btc"], timeframes=["5m"],
                                      tags=["x"], seed=3))
    with pytest.raises(runs.FinalRefused, match="capital.*order_latency_ms|order_latency_ms.*capital"):
        runs.check_final_config(RunConfig(strategy="join-best", capital=1.0, order_latency_ms=1))


def test_cli_final_refuses_loosened_config_without_a_look(pdir):
    from polydata.cli import main

    (pdir / "c.yaml").write_text("strategy: noop\ncoins: [btc]\nmax_gap_ms_pm: 600000\n")
    with pytest.raises(SystemExit, match="max_gap_ms_pm"):
        main(["bt", "run", str(pdir / "c.yaml"), "--final"])
    assert not (pdir / "runs" / "looks.jsonl").exists()


@pytest.mark.parametrize("pull_ok", [True, False])
def test_cli_final_pulls_first_and_records_it(pdir, monkeypatch, capsys, pull_ok):
    from polydata.bt import cli
    from polydata.cli import main

    mark_validated(pdir)
    build_manifests(pdir, sealed_days())
    calls, seen = [], {}

    def pull(*a, **k):
        calls.append("pull")
        if not pull_ok:
            raise RuntimeError("no credentials")
        return 0

    def execute(cfg, kind="run", latency_sweep=None, workers=None, con_factory=None, extra_prov=None, rebuild=True):
        seen.update(extra_prov or {})
        return pdir, {}

    monkeypatch.setattr(runs, "pull", pull)
    monkeypatch.setattr(cli, "execute", execute)
    monkeypatch.setattr(cli, "_print_run", lambda d, m: None)
    (pdir / "c.yaml").write_text("strategy: noop\ncoins: [btc]\n")
    main(["bt", "run", str(pdir / "c.yaml"), "--final"])
    assert calls == ["pull"] and seen["pulled"] is pull_ok
    err = capsys.readouterr().err
    assert ("could not pull" in err) is (not pull_ok)


@pytest.mark.parametrize("extra", [["--days", "2026-11-24", "2026-11-25"], ["--set", "day_kinds=[sealed]"]])
def test_cli_non_final_run_refuses_sealed_days(pdir, extra):
    from polydata.cli import main

    (pdir / "c.yaml").write_text("strategy: noop\ncoins: [btc]\n")
    with pytest.raises(SystemExit, match="sealed days need --final"):
        main(["bt", "run", str(pdir / "c.yaml"), *extra])
    with pytest.raises(SystemExit, match="sealed days need --final"):
        main(["bt", "sweep", str(pdir / "c.yaml"), "--grid", "seed=1,2", *extra])


@pytest.mark.parametrize("evals, order", [((-5.0, 7.0, 1.0), ["seed=2", "seed=3", "seed=1"]),
                                           ((None, None, None), ["seed=1", "seed=3", "seed=2"])])
def test_sweep_ranks_on_eval_usd_per_day(pdir, monkeypatch, capsys, evals, order):
    from polydata.bt import cli, report
    from polydata.cli import main

    vals = {1: (50.0, evals[0]), 2: (10.0, evals[1]), 3: (30.0, evals[2])}  # seed -> (usd_per_day, eval usd_per_day)

    def point(args):
        d, _ = args
        u, e = vals[d["seed"]]
        return f"/x/seed{d['seed']}", {"usd_per_day": u, "pnl_usd": u,
                                       "metrics": {"eval": None if e is None else {"usd_per_day": e}}}

    monkeypatch.setattr(cli, "_sweep_point", point)
    monkeypatch.setattr(report, "render_run", lambda d: None)
    (pdir / "c.yaml").write_text("strategy: noop\ncoins: [btc]\n")
    main(["bt", "sweep", str(pdir / "c.yaml"), "--grid", "seed=1,2,3"])
    out = [x for x in capsys.readouterr().out.splitlines() if "seed=" in x and "/day" in x]
    assert [x.split()[0] for x in out] == order

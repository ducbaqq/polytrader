"""Metrics, verdict and report on synthetic results (offline)."""

from __future__ import annotations

import json
import re
from datetime import date

import numpy as np
import pandas as pd
import pytest

from polydata.bt import engine, metrics, report
from polydata.bt.config import RunConfig
from polydata.bt.engine import CoinResult, RunResult
from polydata.bt.splits import day_kind

D0 = 1_795_519_800_000  # 2026-11-24 11:10 UTC
DAY = 86_400_000


def kind_of(d: str) -> str:
    return day_kind(date.fromisoformat(d))


def make_result(n_days=1, per_day=6, seed=1, pnl_bias=0.0, with_sweep_days=False) -> tuple[RunResult, RunConfig]:
    """Markets with 1 buy and 1 sell fill each. P&L is computed with the same cash-flow identity the engine uses."""
    rng = np.random.default_rng(seed)
    cfg = RunConfig(strategy="join-best", coins=["btc"], timeframes=["5m"])
    mk, fl, orders = [], [], []
    for d in range(n_days):
        for i in range(per_day):
            ws = D0 + d * DAY + i * 300_000
            slug = f"m{d}-{i}"
            settle = float(rng.integers(0, 2))
            # buy at 0.45 (mid 0.5, 10 s later 0.52), sell at 0.60 (mid 0.58, 10 s later 0.55); settle 0/1
            fills = [("buy", 0.45, 5.0, 0.50, 0.52), ("sell", 0.60, 3.0, 0.58, 0.55)]
            pnl = 0.0
            for j, (side, px, sz, m0, m10) in enumerate(fills):
                fee = 0.01 if side == "buy" else 0.0
                dr = 1 if side == "buy" else -1
                pnl += dr * sz * (settle - px) - fee
                fl.append(dict(slug=slug, mid_before=m0, coin="btc", order_id=100 * i + j, ts_ms=ws + 1000 + j, report_ms=ws + 1100 + j,
                               token="Up", side=side, kind="limit", px=px, size=sz, fee=fee, liquidity="maker",
                               queue_ahead_at_fill=1.0, trade_ts_ms=ws + 1000, mid_at_fill=m0, mid_10s=m10,
                               settle_value=settle, bn_move_1s=bool(j)))
                orders.append(dict(ms=ws, report_ms=ws, order_id=100 * i + j, slug=slug, event="placed", token="Up",
                                   side=side, kind="limit", post_only=True, px=px, size=sz, reason="", queue_ahead=2.0))
                orders.append(dict(ms=ws + 1, report_ms=ws + 1, order_id=100 * i + j, slug=slug, event="accepted",
                                   token="Up", side=side, kind="limit", post_only=True, px=px, size=sz, reason="",
                                   queue_ahead=2.0))
            pnl += pnl_bias
            mk.append(dict(slug=slug, coin="btc", timeframe="5m", window_start_ms=ws, window_end_ms=ws + 300_000,
                           voided=False, reason="", pnl=pnl, settle_ms=ws + 360_000, outcome="Up"))
    mk.append(dict(slug="void", coin="btc", timeframe="5m", window_start_ms=D0, window_end_ms=D0 + 300_000, voided=True,
                   reason="gap", pnl=float("nan"), settle_ms=float("nan"), outcome=None))
    ts = np.arange(D0, D0 + n_days * DAY, 3_600_000)
    eq = pd.DataFrame({"ms": ts, "cash": 5000.0, "locked": 0.0, "pending": 0.0, "mtm": 0.0,
                       "equity": 5000.0 + np.cumsum(rng.normal(0, 1, len(ts)))})
    cr = CoinResult(pd.DataFrame(orders), pd.DataFrame(fl)[engine.FILL_COLS], eq, pd.DataFrame(mk), [])
    return RunResult(cfg, {"btc": cr}), cfg


def test_attribution_sums_to_pnl():
    res, cfg = make_result(n_days=2)
    m = metrics.compute(res, cfg, kind_of)
    a = m["attribution"]
    assert abs(sum(a.values()) - m["pnl_usd"]) < 0.01
    assert a["fees"] == pytest.approx(-0.01 * 12)
    assert m["pnl_usd"] == pytest.approx(float(res.markets.pnl.sum()))
    assert m["fills"] == 24 and m["realism"]["fill_rate"] == 1.0
    assert m["realism"]["voided_windows"] == 1
    assert m["realism"]["share_fills_1s_after_bn_move"] == 0.5
    assert m["realism"]["avg_queue_ahead"] == 2.0


def test_bonferroni_threshold():
    assert metrics.t_threshold("final", 1) == 3.0
    assert metrics.t_threshold("final", 100) == pytest.approx(3.29, abs=0.01)
    assert metrics.t_threshold("final", 1000) == pytest.approx(3.89, abs=0.01)
    assert metrics.t_threshold("provisional", 1000) == 3.0


def test_no_validation_days_verdict():
    res, cfg = make_result(n_days=1)  # 2026-11-24 is a train day
    m = metrics.compute(res, cfg, kind_of)
    assert m["eval"] is None
    v = metrics.verdict(m, "provisional", 0)
    assert v["label"] == "no validation days yet" and v["passed"] is False


def test_positive_days_not_judged_below_7():
    res, cfg = make_result(n_days=3, pnl_bias=5.0)
    m = metrics.compute(res, cfg, lambda d: "validation")  # treat every day as validation
    assert m["eval"]["n_days"] == 3
    v = metrics.verdict(m, "provisional", 0)
    c = v["checks"]["positive days >= 70%"]
    assert c["passed"] is None and "not judged" in c["note"]
    # judged at 7+ days
    res, cfg = make_result(n_days=7, pnl_bias=5.0)
    m = metrics.compute(res, cfg, lambda d: "validation")
    assert metrics.verdict(m)["checks"]["positive days >= 70%"]["passed"] is True


def test_verdict_pass_and_fail_paths():
    res, cfg = make_result(n_days=3, per_day=300, pnl_bias=2.0)
    m = metrics.compute(res, cfg, lambda d: "validation")
    v = metrics.verdict(m)
    assert v["checks"]["fills_per_day >= 200"]["passed"] is True
    assert v["checks"]["profitable at 400 ms"]["passed"] is None
    assert v["checks"]["usd_per_day >= min"]["passed"] is True
    m["latency_sweep"] = {"400": {"pnl_usd": 9.0, "eval_pnl_usd": -1.0}}
    v = metrics.verdict(m)
    assert v["checks"]["profitable at 400 ms"]["passed"] is False and v["passed"] is False and v["label"].startswith("FAIL")
    bad = make_result(n_days=3, per_day=2, pnl_bias=-9.0)
    mb = metrics.compute(*bad, lambda d: "validation")
    assert metrics.verdict(mb)["passed"] is False


def test_block_bootstrap_is_deterministic():
    b = np.array([1.0, 2.0, -0.5, 3.0, 2.5, 1.5])
    t1, ci1 = metrics.block_stats(b)
    t2, ci2 = metrics.block_stats(b)
    assert (t1, ci1) == (t2, ci2) and ci1[0] < b.sum() < ci1[1]
    assert metrics.block_stats(np.array([1.0])) == (None, [None, None])


def test_clean_maps_nan_and_numpy():
    out = metrics.clean({"a": float("nan"), "b": np.float64(1.5), "c": [np.int64(2), float("inf")], "d": np.bool_(True)})
    assert out == {"a": None, "b": 1.5, "c": [2, None], "d": True}
    json.dumps(out, allow_nan=False)


def test_report_is_self_contained(tmp_path):
    res, cfg = make_result(n_days=3, per_day=20, pnl_bias=1.0)
    m = metrics.clean(metrics.compute(res, cfg, lambda d: "validation" if d != "2026-11-24" else "train"))
    m["latency_sweep"] = {"100": {"pnl_usd": 5.0, "usd_per_day": 1.0, "fills": 10}, "400": {"pnl_usd": 2.0, "usd_per_day": 0.5, "fills": 9}}
    v = metrics.clean(metrics.verdict(m, "provisional", 0))
    res.equity.to_parquet(tmp_path / "equity.parquet", index=False)
    extras = {"provenance": {"strategy_version": "abc123abc123", "git_commit": "deadbeef"}, "latency_sweep": m["latency_sweep"],
              "sweep_runs": [{"run_id": "r1", "label": "size=5", "pnl_usd": 1.0, "usd_per_day": 0.3, "t_block": 1.2, "verdict": "FAIL", "self": True}]}
    out = report.render(tmp_path, m, v, cfg, extras)
    html = out.read_text()
    assert out.name == "report.html" and len(html) > 5000
    for url in re.findall(r'(?:src|href)\s*=\s*"([^"]*)"', html):
        assert not url.startswith(("http", "//")), url
    assert "http://" not in html and "https://" not in html
    assert html.count("data:image/png;base64,") >= 5
    for s in ("Fill model: unverified", "join-best", "abc123abc123", "Attribution", "Realism", "Latency sensitivity",
              "Robustness", "By market", "Configuration", "Equity", "Daily P&amp;L", "Drawdown", "Edge per day"):
        assert s in html, s


def test_report_final_header_and_no_eval(tmp_path):
    res, cfg = make_result(n_days=1)
    m = metrics.clean(metrics.compute(res, cfg, kind_of))
    v = metrics.clean(metrics.verdict(m, "final", 100))
    res.equity.to_parquet(tmp_path / "equity.parquet", index=False)
    html = report.render(tmp_path, m, v, cfg, {}).read_text()
    assert "no sealed-day data" in html and "Fill model: unverified" in html
    assert "Latency sensitivity" not in html
    m2 = metrics.clean(metrics.compute(res, cfg, lambda d: "sealed"))
    v2 = metrics.clean(metrics.verdict(m2, "final", 99))
    html = report.render(tmp_path, m2, v2, cfg, {}).read_text()
    assert "N = 100" in html and "3.29" in html


def test_execute_writes_metrics_verdict_and_report(tmp_path, monkeypatch):
    from polydata.bt import cli, runs

    monkeypatch.setenv("POLYDATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYDATA_RUNS_CSV", str(tmp_path / "idx.csv"))
    res, cfg = make_result(n_days=2, per_day=5)
    monkeypatch.setattr(engine, "run", lambda c, f, w=None: res)
    d, m = cli.execute(cfg, "run", (100, 400), con_factory=lambda: None)
    r = json.loads((d / "results.json").read_text())
    assert r["verdict"] == "no validation days yet" and r["metrics"]["attribution"]
    assert set(r["latency_sweep"]) == {"100", "200", "400"}
    assert r["latency_sweep"]["400"]["eval_pnl_usd"] is None  # train days only
    assert (d / "report.html").exists()
    idx = runs.load_index()
    assert idx.verdict.iloc[0] == "no validation days yet"
    # the same run twice gives the same results.json bytes (no wall clock)
    d2, _ = cli.execute(cfg, "run", (100, 400), con_factory=lambda: None)
    assert (d / "results.json").read_text() == (d2 / "results.json").read_text()


def test_eval_pnl_uses_validation_days_only():
    res, cfg = make_result(n_days=2, per_day=3, pnl_bias=1.0)
    mk = res.markets
    d1 = metrics._day(D0 + DAY)
    v = metrics.eval_pnl(res, lambda d: "validation" if d == d1 else "train")
    assert v == pytest.approx(float(mk[(mk.window_start_ms >= D0 + DAY) & ~mk.voided].pnl.sum()))
    assert metrics.eval_pnl(res, lambda d: "train") is None


def test_spread_uses_mid_before():
    res, cfg = make_result(n_days=1)
    f = res.coins["btc"].fills.copy()
    f["mid_before"] = f["mid_at_fill"] + 0.01 * np.where(f.side == "buy", 1, -1)
    a = metrics.attribution(f)
    b = metrics.attribution(f.assign(mid_before=f.mid_at_fill))
    assert a["spread"] > b["spread"] and sum(a.values()) == pytest.approx(sum(b.values()))


def _flat_two_coin():
    cfg = RunConfig(strategy="join-best", coins=["btc", "eth"], timeframes=["5m"])  # capital 5000 -> 2500 each

    def cr(coin, t0):
        eq = pd.DataFrame({"ms": [t0, t0 + 1000], "cash": 2500.0, "locked": 0.0, "pending": 0.0, "mtm": 0.0,
                           "equity": [2500.0, 2500.0]})
        mk = pd.DataFrame([dict(slug=coin, coin=coin, timeframe="5m", window_start_ms=D0, window_end_ms=D0 + 300000,
                                voided=False, reason="", pnl=0.0, settle_ms=D0, outcome="Up")])
        return CoinResult(pd.DataFrame(), pd.DataFrame(columns=engine.FILL_COLS), eq, mk, [])
    return RunResult(cfg, {"btc": cr("btc", D0), "eth": cr("eth", D0 + 5000)}), cfg


def test_capital_denominator_two_coins_flat_equity():
    res, cfg = _flat_two_coin()
    m = metrics.compute(res, cfg, lambda d: "validation")
    assert m["capital_total"] == 5000.0 and m["max_drawdown_usd"] == 0 and m["max_drawdown_pct"] == 0
    assert metrics.capitals(cfg) == (5000.0, 2500.0)
    assert metrics.capitals(RunConfig(strategy="x", coins=["btc", "eth"], capital_mode="global")) == (5000.0, 5000.0)


def test_eval_drawdown_ignores_train_days():
    res, cfg = make_result(n_days=2, per_day=3)
    mk = res.coins["btc"].markets  # the RunResult.markets property is a copy
    mk.loc[mk.window_start_ms < D0 + DAY, "pnl"] = -500.0  # a big train-day loss
    mk.loc[mk.window_start_ms >= D0 + DAY, "pnl"] = 1.0
    d1 = metrics._day(D0 + DAY)
    m = metrics.compute(res, cfg, lambda d: "validation" if d == d1 else "train")
    assert m["max_drawdown_usd"] >= 0 and m["eval"]["max_drawdown_usd"] == 0
    assert m["eval"]["n_days"] == 1


def test_missing_400_sweep_is_incomplete_not_pass():
    res, cfg = make_result(n_days=3, per_day=300, pnl_bias=2.0)
    m = metrics.compute(res, cfg, lambda d: "validation")
    v = metrics.verdict(m)
    assert v["passed"] is False and v["label"] == "INCOMPLETE (no 400 ms sweep)"
    m["latency_sweep"] = {"400": {"eval_pnl_usd": 3.0}}
    assert metrics.verdict(m)["passed"] is True


def test_too_few_blocks_fails_t_and_ci():
    res, cfg = make_result(n_days=3, per_day=6, pnl_bias=5.0)
    m = metrics.compute(res, cfg, lambda d: "validation")
    assert m["eval"]["n_blocks"] < metrics.MIN_BLOCKS
    m["latency_sweep"] = {"400": {"eval_pnl_usd": 3.0}}
    v = metrics.verdict(m)
    for k in ("ci95_lo > 0", "t > threshold"):
        assert v["checks"][k]["passed"] is False and "too few blocks" in v["checks"][k]["note"]
    assert v["passed"] is False


def test_bonferroni_counts_current_look():
    res, cfg = make_result(n_days=1)
    m = metrics.compute(res, cfg, lambda d: "sealed")
    v = metrics.verdict(m, "final", 99)
    assert v["n_looks"] == 100 and v["t_threshold"] == pytest.approx(3.29, abs=0.01)


def test_days_and_positive_share_use_nonvoided_days_and_min_usd_in_config():
    res, cfg = make_result(n_days=2, per_day=3)
    mk = res.coins["btc"].markets
    mk.loc[len(mk)] = dict(slug="v2", coin="btc", timeframe="5m", window_start_ms=D0 + 5 * DAY, window_end_ms=D0 + 5 * DAY + 1,
                           voided=True, reason="gap", pnl=float("nan"), settle_ms=float("nan"), outcome=None)
    m = metrics.compute(res, cfg, lambda d: "validation")
    assert m["n_days"] == 2 and len(m["days"]) == 2 and m["eval"]["n_days"] == 2
    assert cfg.min_usd_per_day == 10 and "min_usd_per_day" in cfg.to_dict()

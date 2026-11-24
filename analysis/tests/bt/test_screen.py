"""Screening tier: pessimistic taker fills over snapshots, bootstrap CI, promote/reject."""

from __future__ import annotations

import json
from datetime import date

import numpy as np
import pandas as pd
import pytest

from polydata import screen
from polydata.bt.splits import REPLAY_START_MS
from polydata.fees import taker_fee

DAY_MS = 86_400_000
D_TRAIN = int(pd.Timestamp("2026-11-27", tz="UTC").timestamp() * 1000)   # train
D_VALID = int(pd.Timestamp("2026-11-26", tz="UTC").timestamp() * 1000)   # validation
D_SEALED = int(pd.Timestamp("2026-11-25", tz="UTC").timestamp() * 1000)  # sealed


def snap_rows(slug, ws, *, coin="btc", tf="5m", outcome_up=1, ask=0.80, ask300=0.85, ask1000=0.90, bid=0.78,
              fee_rate=0.05, taus=(50, 40, 30, 20, 10)):
    end = ws + 300_000
    rows = []
    for tau in taus:
        t = end - tau * 1000
        rows.append(dict(slug=slug, coin=coin, timeframe=tf, window_start_ms=ws, window_end_ms=end, t_ms=t,
                         tau_s=float(tau), fee_rate=fee_rate, outcome_up=outcome_up, up_bid=bid, up_ask=ask,
                         dn_bid=1 - ask, dn_ask=1 - bid, up_ask_300=ask300, dn_ask_300=1 - bid,
                         up_ask_1000=ask1000, dn_ask_1000=1 - bid))
    return rows


def sig(slug, t, token="Up", size=None):
    d = dict(slug=slug, t_ms=t, token=token)
    if size is not None:
        d["size"] = size
    return d


def test_fill_uses_latency_ask_and_fee():
    s = pd.DataFrame(snap_rows("m1", D_TRAIN + 1000))
    t = int(s.t_ms.iloc[0])
    sg = pd.DataFrame([sig("m1", t)])
    r = screen.evaluate(s, sg, latency_ms=300)
    expected = 10 * (1 - 0.85) - taker_fee(10, 0.85, 0.05)  # fills at up_ask_300, not at the 0.80 ask at t
    assert r["overall"]["pnl_usd"] == pytest.approx(expected)
    assert r["overall"]["cents_per_share"] == pytest.approx(expected / 10 * 100)
    r1 = screen.evaluate(s, sg, latency_ms=1000)
    assert r1["overall"]["pnl_usd"] == pytest.approx(10 * (1 - 0.90) - taker_fee(10, 0.90, 0.05))
    with pytest.raises(ValueError):
        screen.evaluate(s, sg, latency_ms=500)
    assert "depth" in r["note"]


def test_signal_size_and_down_token_and_loss():
    s = pd.DataFrame(snap_rows("m1", D_TRAIN + 1000, outcome_up=1))
    t = int(s.t_ms.iloc[0])
    r = screen.evaluate(s, pd.DataFrame([sig("m1", t, "Down", size=4)]))
    ask = 1 - 0.78
    assert r["overall"]["pnl_usd"] == pytest.approx(-4 * ask - taker_fee(4, ask, 0.05))


def test_exit_bid_takes_second_fee_at_grid_row_after_2s():
    s = pd.DataFrame(snap_rows("m1", D_TRAIN + 1000, taus=(50, 45, 40, 30)))
    s.loc[s.tau_s == 45, "up_bid"] = 0.10  # 5 s later than the 50 s row: must not be used (>= 2 s: first is 45 s row)
    s.loc[s.tau_s == 40, "up_bid"] = 0.99
    t = int(s.t_ms.iloc[0])
    r = screen.evaluate(s, pd.DataFrame([sig("m1", t)]), exit="bid")
    # the first grid row with t' >= t + 2000 is the tau=45 row (5 s later)
    exp = 10 * (0.10 - 0.85) - taker_fee(10, 0.85, 0.05) - taker_fee(10, 0.10, 0.05)
    assert r["overall"]["pnl_usd"] == pytest.approx(exp)


def test_voided_skipped():
    s = pd.DataFrame(snap_rows("m1", D_TRAIN + 1000) + snap_rows("m2", D_TRAIN + 1000 + 300_000))
    sg = pd.DataFrame([sig("m1", int(s.t_ms.iloc[0])), sig("m2", int(s[s.slug == "m2"].t_ms.iloc[0]))])
    both = screen.evaluate(s, sg)
    one = screen.evaluate(s, sg, voided={"m1"})
    assert both["overall"]["n_trades"] == 2 and one["overall"]["n_trades"] == 1
    assert one["n_voided"] == 1


def many_markets(ws0, n, win_ask300, *, outcome_for):
    rows, sigs = [], []
    for i in range(n):
        ws = ws0 + i * 900_000
        rs = snap_rows(f"m{ws}", ws, ask300=win_ask300, outcome_up=outcome_for(i), taus=(30,))
        rows += rs
        sigs.append(sig(f"m{ws}", rs[0]["t_ms"]))
    return rows, sigs


def test_promote_requires_ci_above_zero_on_validation():
    # validation: always wins at 0.50 -> CI far above 0
    vr, vs = many_markets(D_VALID, 40, 0.50, outcome_for=lambda i: 1)
    tr, ts = many_markets(D_TRAIN, 40, 0.50, outcome_for=lambda i: 1)
    s = pd.DataFrame(vr + tr)
    v = screen.verdict(screen.evaluate(s, pd.DataFrame(vs + ts)))
    assert v["verdict"] == "promote"
    # validation loses half the time at 0.90: CI includes/below 0 even if train is great
    vr, vs = many_markets(D_VALID, 40, 0.90, outcome_for=lambda i: i % 2)
    s = pd.DataFrame(vr + tr)
    v = screen.verdict(screen.evaluate(s, pd.DataFrame(vs + ts)))
    assert v["verdict"] == "reject" and "lower bound" in v["note"]
    # no validation days at all
    v = screen.verdict(screen.evaluate(pd.DataFrame(tr), pd.DataFrame(ts)))
    assert v["verdict"] == "reject" and v["note"] == "no validation days yet"


def test_group_breakdowns():
    r1, s1 = many_markets(D_TRAIN, 10, 0.5, outcome_for=lambda i: 1)
    s = pd.DataFrame(r1)
    r = screen.evaluate(s, pd.DataFrame(s1))
    assert set(r["by_coin_timeframe"]) == {"btc/5m"} and set(r["by_day_kind"]) == {"train"}
    ci = r["overall"]["ci_cents"]
    assert ci[0] <= r["overall"]["cents_per_share"] <= ci[1]
    assert r["overall"]["n_days"] == 1


def test_prepare_drops_before_replay_start_and_sealed_end():
    early = snap_rows("early", REPLAY_START_MS - 600_000)
    ok = snap_rows("ok", D_TRAIN + 1000)
    spill = snap_rows("spill", D_SEALED - 100_000, tf="1h")  # starts on a train day, ends on a sealed one
    spill = [{**r, "window_end_ms": D_SEALED + 100_000} for r in spill]
    s = screen.prepare(pd.DataFrame(early + ok + spill))
    assert set(s.slug) == {"ok"}


def test_endgame_favourite_rule():
    rows = (snap_rows("a", D_TRAIN, ask=0.85, bid=0.83, taus=(90, 55, 40))        # favourite Up, first <=60 s row
            + snap_rows("b", D_TRAIN + 300_000, ask=0.50, bid=0.48, taus=(30,))   # no favourite
            + snap_rows("c", D_TRAIN + 600_000, ask=0.995, bid=0.99, taus=(30,))  # too expensive
            + snap_rows("d", D_TRAIN + 900_000, ask=0.90, bid=0.70, taus=(30,))   # spread 0.20
            + snap_rows("e", D_TRAIN + 1_200_000, ask=0.15, bid=0.13, taus=(30,)))  # Down is the favourite (ask 0.87)
    s = pd.DataFrame(rows)
    s.loc[s.slug == "e", ["dn_ask", "dn_bid"]] = [0.87, 0.85]
    out = screen.REGISTRY["endgame-favourite"].rule(s)
    got = {r.slug: (r.token, r.t_ms) for r in out.itertuples()}
    assert set(got) == {"a", "e"}
    a_t = int(s[(s.slug == "a") & (s.tau_s == 55)].t_ms.iloc[0])
    assert got["a"] == ("Up", a_t) and got["e"][0] == "Down"
    assert len(out) == 2  # one entry per market


def test_rule_ignores_execution_columns():
    s = pd.DataFrame(snap_rows("a", D_TRAIN, ask=0.85, bid=0.83, taus=(30,)))
    base = screen.REGISTRY["endgame-favourite"].rule(s)
    s2 = s.assign(up_ask_300=0.01, up_ask_1000=0.01, dn_ask_300=0.01, dn_ask_1000=0.01, outcome_up=0)
    assert screen.REGISTRY["endgame-favourite"].rule(s2).equals(base)


def test_run_records_screen_folder(tmp_path, monkeypatch):
    monkeypatch.setenv("POLYDATA_DIR", str(tmp_path))
    monkeypatch.setenv("POLYDATA_RUNS_CSV", str(tmp_path / "idx.csv"))
    vr, _ = many_markets(D_VALID, 20, 0.50, outcome_for=lambda i: 1)
    s = pd.DataFrame(vr).assign(up_ask=0.85, up_bid=0.83, tau_s=30.0)
    d, res = screen.run("endgame-favourite", snap=s, voided=set())
    r = json.loads((d / "results.json").read_text())
    assert r["verdict"] == "promote"
    assert (d / "config.yaml").exists()
    idx = pd.read_parquet(tmp_path / "runs" / "index.parquet")
    assert list(idx.kind) == ["screen"] and list(idx.verdict) == ["promote"]
    with pytest.raises(KeyError):
        screen.run("nope", snap=s, voided=set())


def test_exit_bid_many_trades_keep_row_alignment():
    a = pd.DataFrame(snap_rows("a", D_TRAIN + 1000, taus=(50, 40, 30)))
    b = pd.DataFrame(snap_rows("b", D_TRAIN + 1000, taus=(50, 40, 30)))
    a["up_bid"] = 0.20
    b["up_bid"] = 0.60
    s = pd.concat([a, b], ignore_index=True)
    t = int(a.t_ms.iloc[0])
    r = screen.fills(s, pd.DataFrame([sig("b", t), sig("a", t)]), exit="bid").set_index("slug")
    assert r.loc["a", "exit_px"] == pytest.approx(0.20) and r.loc["b", "exit_px"] == pytest.approx(0.60)


def test_spread_gate_uses_favourites_own_token():
    # Down is the favourite (ask 0.87): wide Down spread must block even though the Up spread is narrow
    s = pd.DataFrame(snap_rows("w", D_TRAIN, taus=(30,)) + snap_rows("n", D_TRAIN + 300_000, taus=(30,)))
    s["up_ask"], s["up_bid"], s["dn_ask"], s["dn_bid"] = 0.15, 0.13, 0.87, 0.85
    s.loc[s.slug == "w", "dn_bid"] = 0.60  # Down spread 0.27
    out = screen.REGISTRY["endgame-favourite"].rule(s)
    assert list(out.slug) == ["n"]
    # wide Up spread but a tight Down favourite still signals
    s.loc[s.slug == "w", ["dn_bid", "up_bid"]] = [0.85, 0.01]
    assert set(screen.REGISTRY["endgame-favourite"].rule(s).slug) == {"w", "n"}


def test_exit_bid_capped_at_10s_holds_to_resolution():
    s = pd.DataFrame(snap_rows("m1", D_TRAIN + 1000, taus=(100, 80, 70)))  # next row is 20 s later
    s["up_bid"] = 0.30
    r = screen.fills(s, pd.DataFrame([sig("m1", int(s.t_ms.iloc[0]))]), exit="bid")
    assert r.exit_px.iloc[0] == 1.0 and r.exit_fee.iloc[0] == 0.0


def test_staleness_ignores_sealed_day_parquet(tmp_path, monkeypatch):
    import os
    monkeypatch.setenv("POLYDATA_DIR", str(tmp_path))
    f = tmp_path / "research" / "snapshots.parquet"
    f.parent.mkdir(parents=True)
    pd.DataFrame(snap_rows("a", D_TRAIN)).to_parquet(f)
    old = tmp_path / "parquet" / "x" / "date=2026-11-27"
    new = tmp_path / "parquet" / "x" / "date=2026-11-25"  # sealed
    old.mkdir(parents=True), new.mkdir(parents=True)
    (old / "1.parquet").write_text("")
    (new / "1.parquet").write_text("")
    os.utime(old / "1.parquet", (1, 1))
    os.utime(f, (100, 100))
    # the sealed-day file is newer than the snapshots but must not trigger a rebuild
    _, secs = screen.load_snapshots()
    assert secs is None

"""Gamma confirmation / fallback and Data API trades (`paper.confirm`), with stub REST (no network)."""

from __future__ import annotations

import asyncio
import json
import sys
from pathlib import Path

import duckdb
import pandas as pd
import zstandard

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bt"))

from synth import slug_of  # noqa: E402
from test_live import H11  # noqa: E402
from test_run import paper_run, replay_and_compare  # noqa: E402

from polydata.bt.types import MarketInfo  # noqa: E402
from polydata.paper.confirm import Confirmer, gamma_outcome  # noqa: E402
from polydata.paper.feeds.rest import HttpError  # noqa: E402
from polydata.paper.ingest import Ingest  # noqa: E402
from polydata.paper.runner import PaperOpts  # noqa: E402
from polydata.paper.session import SessionWriter  # noqa: E402
from polydata.paper.settle import ChainlinkSettler  # noqa: E402

WS = H11
WE = H11 + 300_000
SLUG = slug_of("btc", "5m", WS)


def gamma_event(slug, up=True, closed=True, prices=None):
    p = prices or (["1", "0"] if up else ["0", "1"])
    return [{"slug": slug, "eventMetadata": {"priceToBeat": 1.0, "finalPrice": 2.0},
             "markets": [{"closed": closed, "outcomePrices": json.dumps(p), "conditionId": "0xc"}]}]


class StubRest:
    """`get_json(api, url)` answers from callables: `gamma(slug)` / `data(cid, cursor)` (raise to fail)."""

    def __init__(self, gamma=None, data=None):
        self.gamma, self.data = gamma, data
        self.calls: list[tuple[str, str]] = []

    def is_paused(self, api):
        return False

    async def get_json(self, api, url):
        self.calls.append((api, url))
        if api == "gamma":
            return self.gamma(url.split("slug=")[1])
        cid = url.split("condition_id=")[1].split("&")[0]
        cur = url.split("cursor=")[1] if "cursor=" in url else None
        return self.data(cid, cur)


def info(slug=SLUG, ws=WS, we=WE) -> MarketInfo:
    return MarketInfo(slug, "btc", "5m", "btc-up-or-down-5m", "0xc", ws, we, None, 0.05, 1.0, 10, 5.0, None, None)


class Env:
    def __init__(self, tmp_path, rest, *, confirm_wait_ms=900_000):
        self.now = [WE]
        self.w = SessionWriter(tmp_path / "session")
        self.ing = Ingest(None, self.w, lambda: self.now[0])  # rows are held: nothing is routed
        self.settler = ChainlinkSettler(WS - 120_000)
        self.settler.admit(info(), WS)
        self.c = Confirmer(rest, self.ing, self.settler, lambda: self.now[0], confirm_wait_ms=confirm_wait_ms)

    def paper_records(self, name="gamma_resolution"):
        self.w.close()
        f = next((self.w.root / "paper").glob(f"{name}.jsonl.zst"), None)
        if f is None:
            return []
        text = zstandard.ZstdDecompressor().stream_reader(f.open("rb")).read().decode()
        return [json.loads(x) for x in text.splitlines()]


def test_gamma_outcome_rule():
    assert gamma_outcome(gamma_event("s")) == "Up" and gamma_outcome(gamma_event("s", up=False)) == "Down"
    assert gamma_outcome(gamma_event("s", closed=False)) is None
    assert gamma_outcome(gamma_event("s", prices=["0.6", "0.4"])) is None
    assert gamma_outcome([]) is None and gamma_outcome({"x": 1}) is None
    assert gamma_outcome([{"markets": [{"closed": True, "outcomePrices": "oops"}]}]) is None


def test_confirm_records_answers(tmp_path):
    answers = {"n": 0}

    def gamma(slug):
        answers["n"] += 1
        return gamma_event(slug, closed=answers["n"] > 1)  # the first answer: not settled yet

    rest = StubRest(gamma=gamma, data=lambda cid, cur: {"data": []})
    e = Env(tmp_path, rest)
    e.now[0] = WE + 29_000
    asyncio.run(e.c.poll())
    assert rest.calls == [] and e.c.outstanding()  # before end + 30 s nothing is asked
    e.now[0] = WE + 30_000
    asyncio.run(e.c.poll())
    assert len(rest.calls) == 1 and e.c.answers == {}  # not closed yet: asked again next poll
    e.now[0] = WE + 60_000
    asyncio.run(e.c.poll())
    assert e.c.answers[SLUG]["outcome"] == "Up" and e.c.answers[SLUG]["recv_ms"] == WE + 60_000
    n = len(rest.calls)
    e.now[0] = WE + 90_000
    asyncio.run(e.c.poll())
    assert [c for c in rest.calls[n:] if c[0] == "gamma"] == []  # answered: not asked again
    rec = e.paper_records()
    assert [(r["slug"], r["outcome"], r["recv_ms"]) for r in rec] == [(SLUG, "Up", WE + 60_000)]
    assert rec[0]["event"]["slug"] == SLUG
    # no Chainlink outcome (the settler never decided): the answer is also the market's resolution row
    assert [(r.stream, r.recv_ms, r.values["source"]) for r in e.ing.held] == [
        ("pm_resolution", WE + 60_000, "paper-gamma")]
    assert e.c.fallbacks == {SLUG} and SLUG in e.settler.gave_up


def test_gamma_giveup_after_confirm_wait(tmp_path):
    rest = StubRest(gamma=lambda s: gamma_event(s, closed=False), data=lambda c, cur: {"data": []})
    e = Env(tmp_path, rest, confirm_wait_ms=120_000)
    e.now[0] = WE + 30_000
    asyncio.run(e.c.poll())
    assert e.c.holds(SLUG) and e.c.outstanding()
    e.now[0] = WE + 121_000
    asyncio.run(e.c.poll())
    assert not e.c.holds(SLUG) and not e.c.outstanding()  # given up (gamma) and trades done


def test_gamma_errors_back_off(tmp_path):
    def boom(slug):
        raise HttpError(500, "500 boom")

    rest = StubRest(gamma=boom, data=lambda c, cur: {"data": []})
    e = Env(tmp_path, rest)
    e.now[0] = WE + 30_000
    asyncio.run(e.c.poll())
    asyncio.run(e.c.poll())  # inside the 30 s backoff: no second call
    assert len([c for c in rest.calls if c[0] == "gamma"]) == 1
    e.now[0] = WE + 61_000
    asyncio.run(e.c.poll())
    assert len([c for c in rest.calls if c[0] == "gamma"]) == 2
    e.w.close()
    ev = pd.read_csv(next((e.w.root / "catalog").glob("*/rec_events.csv.zst")))
    assert set(ev.kind) == {"rest_error"} and ev.source.str.startswith("pm_resolve:").all()


def test_api_trades_written_once_after_complete_download(tmp_path):
    seen = []
    fail_once = {"left": 1}

    def data(cid, cur):
        seen.append(cur)
        if cur is None:
            return {"data": [{"transaction_hash": f"h{i}", "timestamp": 1} for i in range(3)],
                    "pagination": {"has_more": True, "next_cursor": "c1/=="}}
        if fail_once["left"]:
            fail_once["left"] -= 1
            raise HttpError(502, "502 bad gateway")
        return {"data": [{"transaction_hash": "h3", "timestamp": 2}], "pagination": {"has_more": False}}

    rest = StubRest(gamma=lambda s: gamma_event(s, closed=False), data=data)  # no Gamma row among the trades
    e = Env(tmp_path, rest)
    e.now[0] = WE + 119_000
    asyncio.run(e.c.poll())
    assert [x for x in seen] == []  # trades start at end + 120 s
    e.now[0] = WE + 120_000
    asyncio.run(e.c.poll())
    assert seen == [None, "c1%2F%3D%3D"]  # page 2 failed
    assert e.ing.held == [] and not e.c.api_trades  # nothing written for a partial download
    e.now[0] = WE + 150_000  # past the 30 s backoff: resumes from the cursor, no page 1 again
    asyncio.run(e.c.poll())
    assert seen == [None, "c1%2F%3D%3D", "c1%2F%3D%3D"]
    assert [(r.stream, r.values["slug"], r.values["transaction_hash"]) for r in e.ing.held] == [
        ("pm_api_trades", SLUG, f"h{i}") for i in range(4)]
    assert len({r.recv_ms for r in e.ing.held}) == 1 and e.c.api_trades == {SLUG: 4}
    e.now[0] = WE + 400_000
    asyncio.run(e.c.poll())
    assert len(seen) == 3 and len(e.ing.held) == 4  # done: never downloaded or written again


def assert_columns_from_session(d, run):
    """`gamma_columns` over the in-memory Confirmer/router state and over the session files give the markets.parquet
    columns exactly (what `paper replay` will compute)."""
    from polydata.paper.results import gamma_columns, load_gamma_answers, load_outcome_sources

    written = pd.read_parquet(d / "markets.parquet")
    drop = ["outcome_source", "gamma_outcome", "gamma_seen_ms", "outcome_mismatch", "pnl_gamma", "admitted_ms"]
    base = written.drop(columns=drop)
    base["reason"] = base["reason"].str.replace(" (Gamma answered after the drain)", "", regex=False)
    fills = pd.read_parquet(d / "fills.parquet")
    mem_src = {}
    for r in run.router.new_resolutions:
        mem_src.setdefault(r.slug, r.source)
    for s in run.confirmer.fallbacks:
        mem_src.setdefault(s, "paper-gamma")
    mem = gamma_columns(base, fills, run.confirmer.answers, mem_src)
    ses = gamma_columns(base, fills, load_gamma_answers(d / "session"), load_outcome_sources(d / "session"))
    pd.testing.assert_frame_equal(mem, ses)
    pd.testing.assert_frame_equal(ses, written.drop(columns=["admitted_ms"]), check_dtype=False)  # parquet round trip


# ------------------------------------------------------------------------------------------- end to end

def run_start(first_gave_up: bool):
    """first_gave_up: the 11:00 market starts 30 s after rs, so its start window precedes rs (the settler gives up)."""
    return (H11 - 90_000, 8, PaperOpts(tick_ms=50, duration_s=360)) if first_gave_up else (
        H11 - 20 * 60_000, 22, PaperOpts(tick_ms=50, duration_s=20 * 60))


def factory(rest):
    return lambda clock, event: rest


def test_gamma_fallback(tmp_path, monkeypatch):
    """No Chainlink outcome (start window before rs): the Gamma answer is the pm_resolution row (`paper-gamma`, at the
    answer's recv), the market settles through the router, and the session replay matches."""
    start, minutes, opts = run_start(True)
    rest = StubRest(gamma=lambda s: gamma_event(s, up=True), data=lambda c, cur: {"data": []})
    cfg, d, run, lines = paper_run(tmp_path, monkeypatch, start, minutes, opts, rest_factory=factory(rest))
    assert run.settler.gave_up and list(run.settler.gave_up) == [SLUG]
    res = json.loads((d / "results.json").read_text())
    assert res["resolutions"] == {"paper-gamma": 1} and res["gamma_fallbacks"] == 1 and res["unresolved"] == 0
    mk = pd.read_parquet(d / "markets.parquet").set_index("slug")
    assert mk.loc[SLUG, "outcome"] == "Up" and mk.loc[SLUG, "outcome_source"] == "paper-gamma"
    assert mk.loc[SLUG, "gamma_outcome"] == "Up" and not mk.loc[SLUG, "outcome_mismatch"]
    assert not mk.loc[SLUG, "voided"] and mk.loc[SLUG, "settle_ms"] > WE
    # the session holds exactly one pm_resolution row, from Gamma, stamped at the answer's recv
    sess = d / "session"
    files = [f.as_posix() for f in (sess / "raw").rglob("pm_resolution.*")]
    rows = duckdb.sql(f"SELECT recv_ms, d->>'source', d->>'outcome' FROM read_json({files}, format='newline_delimited', "
                      "columns={'recv_ms': 'BIGINT', 'd': 'JSON'})").fetchall()
    assert len(rows) == 1 and rows[0][1:] == ("paper-gamma", "Up")
    g = [json.loads(x) for x in zstandard.ZstdDecompressor().stream_reader(
        next((sess / "paper").glob("gamma_resolution.*")).open("rb")).read().decode().splitlines()]
    assert len(g) == 1 and g[0]["recv_ms"] == rows[0][0] and g[0]["recv_ms"] >= WE + 30_000
    assert_columns_from_session(d, run)
    rep, man = replay_and_compare(cfg, d, monkeypatch)
    assert man["clean_stop"] is True
    assert rep.markets.set_index("slug").loc[SLUG, "outcome"] == "Up"


def test_drain_waits_for_gamma(tmp_path, monkeypatch):
    """Obligation 17: the drain waits for a market the settler gave up on, while Gamma can still answer: here Gamma
    answers only 5 min after the end, long after the strategy clock reached end_ms."""
    start, minutes, opts = run_start(True)
    calls = {"n": 0}

    def gamma(slug):
        calls["n"] += 1
        return gamma_event(slug, closed=calls["n"] >= 10)

    rest = StubRest(gamma=gamma, data=lambda c, cur: {"data": []})
    cfg, d, run, lines = paper_run(tmp_path, monkeypatch, start, minutes, opts, rest_factory=factory(rest))
    assert calls["n"] >= 10
    res = json.loads((d / "results.json").read_text())
    assert res["gamma_fallbacks"] == 1 and res["unresolved"] == 0
    replay_and_compare(cfg, d, monkeypatch)
    # and bounded: Gamma never answers within confirm_wait -> the market is voided "no outcome", the run still ends
    rest2 = StubRest(gamma=lambda s: gamma_event(s, closed=False), data=lambda c, cur: {"data": []})
    opts2 = PaperOpts(tick_ms=50, duration_s=360, confirm_wait_s=120)
    cfg, d2, run2, _ = paper_run(tmp_path / "b", monkeypatch, start, minutes, opts2, rest_factory=factory(rest2))
    mk = pd.read_parquet(d2 / "markets.parquet").set_index("slug")
    assert mk.loc[SLUG, "voided"] and "no outcome" in mk.loc[SLUG, "reason"]
    assert json.loads((d2 / "results.json").read_text())["unresolved"] == 1
    replay_and_compare(cfg, d2, monkeypatch)


def test_mismatch_reported(tmp_path, monkeypatch):
    """Gamma disagrees with Chainlink: reported (outcome_mismatch, an error), P&L keeps Chainlink's outcome and
    `pnl_usd_gamma` restates it; parity holds."""
    start, minutes, opts = run_start(False)
    rest = StubRest(gamma=lambda s: gamma_event(s, up=False), data=lambda c, cur: {"data": []})  # always Down
    cfg, d, run, lines = paper_run(tmp_path, monkeypatch, start, minutes, opts, rest_factory=factory(rest))
    res = json.loads((d / "results.json").read_text())
    mk = pd.read_parquet(d / "markets.parquet")
    assert (mk.outcome_source == "paper-chainlink").all() and (mk.gamma_outcome == "Down").all()
    assert (mk.outcome_mismatch == (mk.outcome == "Up")).all() and mk.outcome_mismatch.any()
    assert res["outcome_mismatches"] == int(mk.outcome_mismatch.sum())
    assert len(res["errors"]) == res["outcome_mismatches"] and "outcome mismatch" in res["errors"][0]
    assert res["gamma_fallbacks"] == 0
    f = pd.read_parquet(d / "fills.parquet")
    for r in mk.itertuples():
        net = {t: float(f[(f.slug == r.slug) & (f.token == t) & (f.side == "BUY")]["size"].sum()
                        - f[(f.slug == r.slug) & (f.token == t) & (f.side == "SELL")]["size"].sum())
               for t in ("Up", "Down")}
        want = r.pnl_raw + (net["Down"] - net[r.outcome] if r.outcome_mismatch else 0.0)
        assert abs(r.pnl_gamma - want) < 1e-9
    assert abs(res["pnl_usd_gamma"] - mk.loc[~mk.voided, "pnl_gamma"].sum()) < 1e-6
    assert abs(res["pnl_usd"] - mk.loc[~mk.voided, "pnl"].astype(float).sum()) < 1e-6
    assert_columns_from_session(d, run)
    replay_and_compare(cfg, d, monkeypatch)


def test_gamma_fallback_mid_run_parity(tmp_path, monkeypatch):
    """A paper-gamma row received well before end_ms (a later market keeps the run going): the market settles on the
    strategy clock mid-run, as the session replay does."""
    start = H11 - 90_000
    opts = PaperOpts(tick_ms=50, duration_s=660)
    rest = StubRest(gamma=lambda s: gamma_event(s, up=True), data=lambda c, cur: {"data": []})
    cfg, d, run, lines = paper_run(tmp_path, monkeypatch, start, 12, opts, rest_factory=factory(rest))
    man = json.loads((d / "session" / "manifest.json").read_text())
    assert [a["slug"] for a in man["admitted"]] == [SLUG, slug_of("btc", "5m", WE)] and man["end_ms"] == H11 - 30_000 + 660_000
    mk = pd.read_parquet(d / "markets.parquet").set_index("slug")
    assert mk.loc[SLUG, "outcome_source"] == "paper-gamma" and mk.loc[SLUG, "gamma_seen_ms"] < man["end_ms"]
    assert mk.loc[SLUG, "settle_ms"] < man["end_ms"]
    assert mk.loc[slug_of("btc", "5m", WE), "outcome_source"] == "paper-chainlink"
    replay_and_compare(cfg, d, monkeypatch)


def test_late_gamma_void_reason(tmp_path, monkeypatch):
    """Gamma answers only after the drain limit: no pm_resolution row (parity), voided, and the reason says why."""
    start, minutes, _ = run_start(True)
    calls = {"n": 0}

    def gamma(slug):
        calls["n"] += 1
        return gamma_event(slug, closed=calls["n"] >= 6)

    rest = StubRest(gamma=gamma, data=lambda c, cur: {"data": []})
    opts = PaperOpts(tick_ms=50, duration_s=360, max_drain_s=60)
    cfg, d, run, _ = paper_run(tmp_path, monkeypatch, start, minutes, opts, rest_factory=factory(rest))
    mk = pd.read_parquet(d / "markets.parquet").set_index("slug")
    assert mk.loc[SLUG, "gamma_outcome"] == "Up" and mk.loc[SLUG, "outcome_source"] is None
    assert mk.loc[SLUG, "voided"] and mk.loc[SLUG, "reason"].endswith("(Gamma answered after the drain)")
    assert "no outcome" in json.loads((d / "results.json").read_text())["void_reasons"][SLUG]
    assert_columns_from_session(d, run)
    assert not list((d / "session" / "raw").rglob("pm_resolution.*"))  # no row: replay parity as in the bounded case

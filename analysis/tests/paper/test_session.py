"""Session writer and ingest: a session builds to the same rows as the recorder layout the backtests use."""

from __future__ import annotations

import sys
from pathlib import Path
from types import SimpleNamespace

import duckdb
import pandas as pd
import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "bt"))

from synth import make_session, meta_row, resolution_row, slug_of, sort_rows, write_store  # noqa: E402
from test_live import H11, StubSettler, assert_parity, cfg_of  # noqa: E402

from polydata import schema  # noqa: E402
from polydata.bt import gaps as G  # noqa: E402
from polydata.bt.engine import ReplayDriver, SessionPlan, run_session  # noqa: E402
from polydata.bt.live import LiveRouter, LiveRow  # noqa: E402
from polydata.build import _read_tsv, build  # noqa: E402
from polydata.paper.ingest import Ingest  # noqa: E402
from polydata.paper.session import SessionWriter, dumps  # noqa: E402
from polydata.views import create_views  # noqa: E402


def build_at(root: Path, monkeypatch) -> None:
    monkeypatch.setenv("POLYDATA_DIR", str(root))
    build()


def stream_df(root: Path, stream: str) -> pd.DataFrame | None:
    files = sorted((root / "parquet" / stream).glob("*/*.parquet"))
    if not files:
        return None
    con = duckdb.connect()
    cols = list(schema.TSV[stream]) + ["seq"] if stream in schema.TSV else ["recv_ms", "d"]
    sel = ", ".join(f"{c}::VARCHAR AS {c}" if c == "d" else c for c in cols)
    order = ", ".join(["recv_ms", "seq"] if stream in schema.TSV else ["recv_ms", "d"])
    return con.execute(f"SELECT {sel} FROM read_parquet(?) ORDER BY {order}", [[f.as_posix() for f in files]]).df()


def frames(path: Path) -> int:
    """Number of zstd frames in a file."""
    import zstandard

    data, n = path.read_bytes(), 0
    while data:
        d = zstandard.ZstdDecompressor().decompressobj()
        d.decompress(data)
        data = d.unused_data
        n += 1
    return n


def write_chunks(w: SessionWriter, rows, flushes: int) -> None:
    n = len(rows)
    cuts = {n * (i + 1) // (flushes + 1) for i in range(flushes)}
    for i, r in enumerate(rows):
        if i in cuts:
            w.flush()
        w.write(r)
    w.close()


def test_session_builds_to_same_rows(tmp_path, monkeypatch):
    rows = make_session(3, H11, 12, coins=("btc", "eth"), timeframes=("5m",), bn_ms=10)
    write_store(rows, tmp_path / "ref")
    w = SessionWriter(tmp_path / "sess")
    write_chunks(w, rows, 3)
    build_at(tmp_path / "ref", monkeypatch)
    build_at(tmp_path / "sess", monkeypatch)
    seen = 0
    for stream in list(schema.TSV) + schema.JSONL:
        a, b = stream_df(tmp_path / "ref", stream), stream_df(tmp_path / "sess", stream)
        assert (a is None) == (b is None), stream
        if a is None:
            continue
        seen += 1
        if stream == "bn_fut_bbo":
            assert 0 < len(b) <= len(a)
            continue
        pd.testing.assert_frame_equal(a, b, obj=stream)
    assert seen >= 7
    # files really are multi-frame
    assert max(frames(f) for f in (tmp_path / "sess" / "raw").rglob("*.zst")) >= 2


def test_multi_frame_files_readable(tmp_path):
    w = SessionWriter(tmp_path)
    r = [LiveRow("pm_trade", H11 + i, (str(H11 + i), "a|U", "BUY", "0.50", "1", "0", f"0x{i}")) for i in range(9)]
    m = [LiveRow("pm_other", H11 + i, {"event_type": "x", "i": i}) for i in range(9)]
    for k in range(3):
        for row in r[3 * k:3 * k + 3] + m[3 * k:3 * k + 3]:
            w.write(row)
        w.flush()
    w.close()
    (tsv,) = (tmp_path / "raw").rglob("pm_trade.*.tsv.zst")
    (js,) = (tmp_path / "raw").rglob("pm_other.*.jsonl.zst")
    t = _read_tsv(tsv, schema.TSV["pm_trade"])
    assert t.num_rows == 9 and t.column("tx_hash").to_pylist() == [f"0x{i}" for i in range(9)]
    assert t.column_names == list(schema.TSV["pm_trade"])  # one header only
    assert frames(tsv) == 3 and frames(js) == 3
    d = duckdb.sql(f"SELECT recv_ms, d->>'i' AS i FROM read_json('{js}', format='newline_delimited', "
                   f"columns={{'recv_ms': 'BIGINT', 'd': 'JSON'}}) ORDER BY recv_ms").fetchall()
    assert [int(x[1]) for x in d] == list(range(9))


def bbo(sym, recv, uid, bid="1.0"):
    return LiveRow("bn_fut_bbo", recv, (sym, str(uid), str(recv - 2), str(recv - 3), bid, "1", "2.0", "1"))


def test_bn_thinning_keeps_first_and_last(tmp_path, monkeypatch):
    rows = make_session(5, H11, 6, coins=("btc", "eth", "sol"), bn_ms=3, bn_silence=(H11 + 2 * 60_000, H11 + 2 * 60_000 + 3_000))
    # out-of-order update ids inside buckets, bursts sharing a recv_ms
    extra = [bbo("BTCUSDT", H11 + 10_000 + k, u) for k, u in [(1, 50), (2, 40), (3, 60), (4, 10), (11, 5), (11, 6)]]
    rows = sort_rows(rows + extra)
    write_store(rows, tmp_path / "full")
    w = SessionWriter(tmp_path / "thin")
    write_chunks(w, rows, 3)

    by: dict = {}
    for r in rows:
        if r.stream == "bn_fut_bbo":
            by.setdefault((r.values[0], r.recv_ms // 10), []).append(r)
    want = []
    for rs in by.values():
        want.append(rs[0])
        best = max(rs, key=lambda r: (r.recv_ms, int(r.values[1])))
        if best is not rs[0] and (best.recv_ms, int(best.values[1])) > (rs[0].recv_ms, int(rs[0].values[1])):
            want.append(best)
    got = []
    for f in (tmp_path / "thin" / "raw").rglob("bn_fut_bbo.*.tsv.zst"):
        t = _read_tsv(f, schema.TSV["bn_fut_bbo"]).to_pandas()
        got += [tuple(str(x) for x in rec) for rec in t.itertuples(index=False)]
    expect = [(str(r.recv_ms), *r.values) for r in want]
    assert sorted(got) == sorted(expect)
    assert len(got) < sum(len(v) for v in by.values())

    build_at(tmp_path / "full", monkeypatch)
    build_at(tmp_path / "thin", monkeypatch)

    def con_of(root):
        con = duckdb.connect()
        con.execute("SET TimeZone='UTC'")
        create_views(con, parquet=root / "parquet", include_sealed=True)
        return con

    full, thin = con_of(tmp_path / "full"), con_of(tmp_path / "thin")
    hour = H11 // 3_600_000 * 3_600_000
    for step in (10, 100):
        out = []
        for con in (full, thin):
            seen = {}
            drv = object.__new__(ReplayDriver)
            drv.core = SimpleNamespace(cfg=SimpleNamespace(bn_step_ms=step), syms=["BTCUSDT", "ETHUSDT", "SOLUSDT"],
                                       add_bn_hour=lambda s, e, bns, seen=seen: seen.update(bns))
            drv._load_bn(con, hour, hour + 3_600_000)
            out.append(seen)
        for sym in out[0]:
            for a, b in zip(out[0][sym], out[1][sym]):
                assert (a == b).all(), (step, sym)
            assert len(out[0][sym].recv) > 0
    a = G.bn_silences(full, H11, H11 + 6 * 60_000)
    b = G.bn_silences(thin, H11, H11 + 6 * 60_000)
    assert len(a) > 0
    pd.testing.assert_frame_equal(a, b)


def test_late_rows_not_in_raw(tmp_path, monkeypatch):
    rows = make_session(7, H11, 6, bn_ms=20)
    w = SessionWriter(tmp_path)
    late = LiveRow("pm_book", H11 + 1000, (str(H11 + 500), "zzz-late|U", "0.40:5", "0.60:5", "hlate"))
    late_other = LiveRow("pm_other", H11 + 1000, {"event_type": "tick_size_change", "market": "0xzz", "timestamp": "1"})
    for r in rows:
        w.write(r)
    w.write_late(late, 700)
    w.write_late(late_other, 5)
    w.close()
    (f,) = (tmp_path / "late").glob("pm_book.tsv.zst")
    t = _read_tsv(f, [*schema.TSV["pm_book"], "late_by_ms"]).to_pylist()
    assert t == [{"recv_ms": str(H11 + 1000), "ts_ms": str(H11 + 500), "asset": "zzz-late|U", "bids": "0.40:5",
                  "asks": "0.60:5", "hash": "hlate", "late_by_ms": "700"}]
    (g,) = (tmp_path / "late").glob("pm_other.tsv.zst")
    t2 = _read_tsv(g, ["recv_ms", "d", "late_by_ms"]).to_pylist()
    assert len(t2) == 1 and t2[0]["late_by_ms"] == "5" and '"tick_size_change"' in t2[0]["d"]
    build_at(tmp_path, monkeypatch)
    a = stream_df(tmp_path, "pm_book")
    assert not a.asset.str.contains("zzz-late").any()
    assert not (tmp_path / "parquet" / "late").exists()


def test_rec_events_catalog_readable(tmp_path, monkeypatch):
    w = SessionWriter(tmp_path)
    w.event(H11 + 1_000, "ws_close", "polymarket", "code=1006, reconnect in 1000 ms")
    w.flush()
    w.event(H11 + 6_000, "ws_open", "polymarket", 'a "quoted", comma\nand newline')
    w.event(H11 + 7_000, "ws_close", "polymarket-rtds", "")
    w.close()
    monkeypatch.setenv("POLYDATA_DIR", str(tmp_path))
    from datetime import date
    ev = G.load_rec_events([date(2026, 11, 24)], include_sealed=True)
    assert list(ev.kind) == ["ws_close", "ws_open", "ws_close"]
    assert ev.detail.iloc[1] == 'a "quoted", comma\nand newline'
    assert list(ev.ts_ms) == [H11 + 1_000, H11 + 6_000, H11 + 7_000]
    g = G.event_gaps(ev, end_ms=H11 + 60_000)
    rows = {(r.stream, r.key, r.from_ms, r.to_ms) for r in g.itertuples()}
    assert ("pm", "*", H11 + 1_000, H11 + 6_000) in rows
    assert ("cl", "*", H11 + 7_000, H11 + 60_000) in rows  # unmatched close runs to the end


def _events(root: Path, monkeypatch) -> pd.DataFrame:
    from datetime import date

    monkeypatch.setenv("POLYDATA_DIR", str(root))
    return G.load_rec_events([date(2026, 11, 24)], include_sealed=True)


def _market(ws: int) -> pd.DataFrame:
    return pd.DataFrame([(slug_of("btc", "5m", ws), "btc-up-or-down-5m", "btc", ws, ws + 300_000, "Up")],
                        columns=["slug", "series", "coin", "window_start_ms", "window_end_ms", "outcome"])


def test_monotone_stamp_and_clock_step(tmp_path, monkeypatch):
    now = [H11]
    w = SessionWriter(tmp_path)
    ing = Ingest(None, w, lambda: now[0])  # type: ignore[arg-type]
    stamps = []
    for t in [0, 10, 20, 5_000, 5_010, 2_010, 2_500, 4_000, 11_000, 11_100]:  # back by 3 s, caught up at +11 s
        now[0] = H11 + t
        stamps.append(ing.stamp())
    assert stamps == sorted(stamps) and stamps[-1] == H11 + 11_100
    assert stamps[5:8] == [H11 + 5_010] * 3  # frozen while the clock is behind
    w.close()
    ev = _events(tmp_path, monkeypatch)
    assert list(zip(ev.kind, ev.source)) == [("stop", "recorder"), ("start", "recorder")]  # one pair
    # the gap runs until the first stamp with the clock caught up (not zero length)
    assert (ev.ts_ms.iloc[0], ev.ts_ms.iloc[1]) == (H11 + 5_010, H11 + 11_000)
    g = G.event_gaps(ev, end_ms=H11 + 300_000, bn_syms=["BTCUSDT"])
    pm = g[(g.stream == "pm") & (g.key == "*")].iloc[0]
    assert pm.to_ms - pm.from_ms == 5_990 > 2_000
    v = G.voided_markets(_market(H11), g)
    assert list(v.slug) == [slug_of("btc", "5m", H11)] and "pm gap 5990" in v.reason[0]
    assert G.voided_markets(_market(H11 + 600_000), g).empty


def test_stall_gap_voids_market(tmp_path, monkeypatch):
    ws = H11 + 300_000
    w = SessionWriter(tmp_path)
    ing = Ingest(LiveRouter(cfg_of("noop"), H11), w, lambda: ws)
    for t in [ws + 10_000, ws + 10_020, ws + 10_040]:  # ticks every 20 ms
        ing.note_tick(t)
    ing.note_tick(ws + 10_040 + 7_000)  # stalled 7 s: gap [last, wall]
    ing.note_tick(ws + 10_040 + 7_020)
    ing.note_loop(ws + 10_040 + 9_000)  # a 2 s step is not a stall
    w.close()
    ev = _events(tmp_path, monkeypatch)
    assert list(zip(ev.kind, ev.ts_ms)) == [("stop", ws + 10_040), ("start", ws + 17_040)]
    g = G.event_gaps(ev, end_ms=ws + 400_000, bn_syms=["BTCUSDT"])
    v = G.voided_markets(_market(ws), g)
    assert list(v.slug) == [slug_of("btc", "5m", ws)] and "pm gap 7000" in v.reason[0]
    # a stall that does not overlap the window voids nothing
    assert G.voided_markets(_market(ws + 600_000), g).empty


def test_tsv_value_with_separator_raises(tmp_path):
    w = SessionWriter(tmp_path)
    for bad in ["a\tb", "a\nb", "a\rb"]:
        with pytest.raises(ValueError):
            w.write(LiveRow("pm_book", H11, (str(H11), bad, "", "", "h")))
    with pytest.raises(ValueError):
        w.write_late(LiveRow("pm_book", H11, (str(H11), "x\ty", "", "", "h")), 5)
    w.write(LiveRow("pm_book", H11, (str(H11), "ok|U", "", "", "h")))  # empty fields are fine
    w.close()
    assert sum(1 for _ in (tmp_path / "raw").rglob("pm_book.*")) == 1


def test_json_number_text_and_markets_view(tmp_path, monkeypatch):
    from test_pipeline import write_jsonl

    assert dumps({"a": 1.0, "b": 0.05, "c": [2.0, {"d": -0.0}], "e": float("nan"), "f": "é"}) == \
        '{"a":1,"b":0.05,"c":[2,{"d":0}],"e":null,"f":"é"}'
    ws = H11 + 600_000
    m = meta_row("btc", "5m", ws, ws - 120_000)
    mk = m.values["event"]["markets"][0]
    mk["orderMinSize"] = 5.0  # a float with an integer value, as Python parses `5.0`
    mk["feeSchedule"]["exponent"] = 1.0
    res = resolution_row("btc", "5m", ws, ws + 330_000, "Up", ptb=100.0, final=101.5, source="x")
    rows = [m, res, LiveRow("pm_other", ws, {"event_type": "tick_size_change", "market": mk["conditionId"], "n": 2.0})]
    w = SessionWriter(tmp_path / "mine")
    for r in rows:
        w.write(r)
    w.close()
    # the recorder's text: JSON.stringify prints 5.0 as 5
    ref = tmp_path / "ref"
    base = next((tmp_path / "mine" / "raw").glob("*/*"))
    for f in base.glob("*.jsonl.zst"):
        txt = duckdb.sql(f"SELECT line FROM read_csv('{f}', columns={{'line': 'VARCHAR'}}, delim=chr(1), header=false, quote='', escape='')").fetchall()
        assert all("5.0" not in x[0] and "100.0" not in x[0] and '"n":2.0' not in x[0] for x in txt)
    write_store(rows, ref)
    build_at(tmp_path / "mine", monkeypatch)
    build_at(ref, monkeypatch)

    def markets(root):
        con = duckdb.connect()
        con.execute("SET TimeZone='UTC'")
        create_views(con, parquet=root / "parquet", include_sealed=True)
        return con.execute("SELECT * FROM markets").df()

    a, b = markets(tmp_path / "mine"), markets(ref)
    assert len(a) == 1
    pd.testing.assert_frame_equal(a, b)


# --------------------------------------------------------------------------------------------- parity

@pytest.mark.parametrize("settler", [False, True])
def test_session_replay_equals_live(tmp_path, monkeypatch, settler):
    """The Task 5 parity harness with the store written by Ingest + SessionWriter. With `settler`, the feed's
    pm_resolution rows are dropped and the settler's resolutions (`router.new_resolutions`) reach the session only
    through Ingest."""
    start = H11 - 20 * 60_000
    rows = make_session(11, start, 28, bn_ms=10, meta_lead_ms=4 * 60_000)
    if settler:
        rows = [r for r in rows if r.stream != "pm_resolution"]
    cfg = cfg_of("buy-and-hold-up")
    rs = start + 60_000
    router = LiveRouter(cfg, rs, settler=StubSettler(rs) if settler else None)
    wall = [rows[0].recv_ms]
    w = SessionWriter(tmp_path / "session", flush_s=10)
    ing = Ingest(router, w, lambda: wall[0])
    stop_ms = H11 + 2 * 60_000
    i, n, last = 0, len(rows), rows[-1].recv_ms
    cur = wall[0]
    while True:
        while i < n and rows[i].recv_ms <= cur:
            wall[0] = rows[i].recv_ms
            ing.on_rows([(rows[i].stream, rows[i].values)])
            i += 1
        wall[0] = cur
        router.tick(cur)
        ing.note_tick(cur)
        if i % 500 == 0:
            w.flush()
        if router.end_ms is None and router.T >= stop_ms:
            router.request_stop(router.T)
        if router.end_ms is not None and i >= n and router.T >= max(router.end_ms, last):
            break
        cur += 20
    live = router.finish()
    ing.finish()
    w.close()
    assert len(router.admitted) > 0
    if settler:
        assert len(router.new_resolutions) >= 3
    sess = duckdb.sql(f"SELECT count(*) FROM read_json({[f.as_posix() for f in (tmp_path / 'session' / 'raw').rglob('pm_resolution.*')]}, "
                      "format='newline_delimited', columns={'recv_ms': 'BIGINT', 'd': 'JSON'})").fetchall()[0][0]
    assert sess == (len(router.new_resolutions) if settler else sum(1 for r in rows if r.stream == "pm_resolution"))
    assert sess >= 3
    build_at(tmp_path / "session", monkeypatch)
    root = tmp_path / "session"

    def con_factory():
        con = duckdb.connect()
        con.execute("SET TimeZone = 'UTC'")
        create_views(con, parquet=root / "parquet", include_sealed=True)
        return con

    plan = SessionPlan(router.rs, router.end_ms, [(a["slug"], a["admitted_ms"]) for a in router.admitted])
    rep = run_session(cfg, con_factory, plan)
    assert_parity(live, rep)

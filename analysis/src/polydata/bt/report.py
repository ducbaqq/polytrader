"""The one-page HTML report of a run: verdict first, charts inlined as base64 PNGs, no external requests."""

from __future__ import annotations

import base64
import html
import io
import json
from pathlib import Path

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import pandas as pd  # noqa: E402
import yaml  # noqa: E402

from . import metrics as M  # noqa: E402

KIND_COLOR = {"train": "#9aa5b1", "validation": "#2f7ed8", "sealed": "#d9822b"}
GOOD, BAD = "#2e8b57", "#c0392b"
CSS = """
body{font:14px/1.45 -apple-system,Helvetica,Arial,sans-serif;max-width:980px;margin:24px auto;padding:0 16px;color:#222}
h1{font-size:22px;margin:0 0 4px}h2{font-size:16px;margin:28px 0 6px;border-bottom:1px solid #ddd;padding-bottom:3px}
.verdict{display:inline-block;padding:3px 12px;border-radius:4px;color:#fff;font-weight:600}
.pass{background:#2e8b57}.fail{background:#c0392b}.none{background:#7a7a7a}
.meta{color:#555;margin:6px 0}.warn{color:#9a5b00;font-weight:600}
table{border-collapse:collapse;margin:6px 0}td,th{border:1px solid #ddd;padding:3px 9px;text-align:right}
th{background:#f4f4f4}td:first-child,th:first-child{text-align:left}tr.self td{background:#fff6d6}
img{max-width:100%}pre{background:#f6f6f6;padding:8px;overflow:auto;font-size:12px}
.ok{color:#2e8b57}.no{color:#c0392b}.nj{color:#777}
"""


def _png(fig) -> str:
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=90, bbox_inches="tight")
    plt.close(fig)
    return '<img alt="chart" src="data:image/png;base64,' + base64.b64encode(buf.getvalue()).decode() + '">'


def _fmt(x, f="{:,.2f}") -> str:
    return "n/a" if x is None else f.format(x)


def _table(head: list[str], rows: list[list], cls: list[str] | None = None) -> str:
    h = "<tr>" + "".join(f"<th>{html.escape(c)}</th>" for c in head) + "</tr>"
    b = "".join(f'<tr{" class=" + repr(cls[i]) if cls and cls[i] else ""}>' + "".join(f"<td>{html.escape(str(c))}</td>" for c in r) + "</tr>"
                for i, r in enumerate(rows))
    return f"<table>{h}{b}</table>"


def _shade(ax, days: dict) -> None:
    for d, v in days.items():
        t = pd.Timestamp(d, tz="UTC")
        ax.axvspan(t, t + pd.Timedelta(days=1), color=KIND_COLOR.get(v["kind"], "#ccc"), alpha=0.12, lw=0)


def _equity_series(run_dir: Path, m: dict, cfg) -> pd.Series:
    f = run_dir / "equity.parquet"
    if not f.exists():
        return pd.Series(dtype="float64")
    eq = pd.read_parquet(f)
    total, each = M.capitals(cfg)
    s = M.total_equity(eq, total, each) if len(eq) else pd.Series(dtype="float64")
    if len(s) > 3000:
        s = s.iloc[:: len(s) // 3000 + 1]
    s.index = pd.to_datetime(s.index, unit="ms", utc=True)
    return s


def _empty(msg="no data") -> str:
    fig, ax = plt.subplots(figsize=(8, 1.2))
    ax.text(0.5, 0.5, msg, ha="center", va="center")
    ax.axis("off")
    return _png(fig)


def chart_equity(s: pd.Series, m: dict) -> str:
    if len(s) == 0:
        return _empty()
    fig, ax = plt.subplots(figsize=(9, 3))
    ax.plot(s.index, s.values, lw=1, color="#222")
    _shade(ax, m["days"])
    ax.set_ylabel("equity, $")
    ax.legend(handles=[plt.Rectangle((0, 0), 1, 1, color=c, alpha=0.3) for c in KIND_COLOR.values()],
              labels=list(KIND_COLOR), loc="upper left", fontsize=8, ncol=3)
    fig.autofmt_xdate()
    return _png(fig)


def chart_drawdown(s: pd.Series) -> str:
    if len(s) == 0:
        return _empty()
    fig, ax = plt.subplots(figsize=(9, 2.2))
    ax.fill_between(s.index, (s - s.cummax()).values, 0, color=BAD, alpha=0.5)
    ax.set_ylabel("drawdown, $")
    fig.autofmt_xdate()
    return _png(fig)


def chart_bars(rows: list[dict], key: str, ylabel: str) -> str:
    rows = [r for r in rows if r.get(key) is not None]
    if not rows:
        return _empty()
    fig, ax = plt.subplots(figsize=(9, 2.4))
    ax.bar([r["day"] for r in rows], [r[key] for r in rows], color=[KIND_COLOR.get(r["kind"], "#ccc") for r in rows])
    ax.axhline(0, color="#444", lw=0.6)
    ax.set_ylabel(ylabel)
    fig.autofmt_xdate()
    return _png(fig)


def chart_attribution(a: dict) -> str:
    names = ["spread", "adverse", "inventory", "fees"]
    vals = [a[n] for n in names]
    fig, ax = plt.subplots(figsize=(6, 2.6))
    ax.bar(names + ["total"], vals + [sum(vals)], color=[GOOD if v >= 0 else BAD for v in vals] + ["#444"])
    ax.axhline(0, color="#444", lw=0.6)
    ax.set_ylabel("$")
    return _png(fig)


def _verdict_block(v: dict, m: dict) -> str:
    label = v.get("label", "")
    cls = "none" if label in (M.NO_VALIDATION, "no sealed-day data") or label.startswith("INCOMPLETE") else ("pass" if v.get("passed") else "fail")
    out = [f'<span class="verdict {cls}">{html.escape(label)}</span>']
    if v.get("checks"):
        rows, cl = [], []
        for name, c in v["checks"].items():
            mark = "not judged" if c["passed"] is None else ("pass" if c["passed"] else "FAIL")
            rows.append([name, _fmt(c["value"], "{:,.3f}") if isinstance(c["value"], (int, float)) else "n/a",
                         _fmt(c["threshold"], "{:,.3f}"), mark])
        out.append(_table(["check", "value", "threshold", "result"], rows, cl))
    return "".join(out)


def render(run_dir: Path, metrics: dict, verdict: dict, cfg, extras: dict) -> Path:
    """Write `<run_dir>/report.html` and return its path. `extras`: provenance, latency_sweep, sweep_runs."""
    run_dir = Path(run_dir)
    m, v = metrics, verdict
    prov = extras.get("provenance") or {}
    e = m.get("eval")
    sections = []
    final = v.get("kind") == "final"
    head = [f"<h1>{html.escape(cfg.strategy)} <small>version {html.escape(str(prov.get('strategy_version') or 'n/a'))}</small></h1>",
            _verdict_block(v, m),
            '<div class="meta warn">Fill model: unverified</div>']
    if final:
        head.append(f'<div class="meta">Final run: N = {v.get("n_looks")} looks so far, t threshold {_fmt(v.get("t_threshold"), "{:.2f}")}</div>')
    else:
        head.append(f'<div class="meta">Provisional verdict on {html.escape(e["kind"] if e else "no")} days only; '
                    f't threshold {_fmt(v.get("t_threshold"), "{:.2f}")}</div>')
    kinds = {}
    for d in m["days"].values():
        kinds[d["kind"]] = kinds.get(d["kind"], 0) + 1
    head.append(f'<div class="meta">All days: {m["n_days"]} days ({", ".join(f"{n} {k}" for k, n in sorted(kinds.items()))}); '
                f'P&amp;L ${_fmt(m["pnl_usd"])} (${_fmt(m["usd_per_day"])}/day, return on capital {_fmt(100 * m["return_on_capital"])}%); '
                f'{m["fills"]} fills ({_fmt(m["cents_per_share"])} cents/share); '
                f'block t {_fmt(m["block_pnl_t"])}, 95% CI [{_fmt(m["block_ci95"][0])}, {_fmt(m["block_ci95"][1])}]; '
                f'max drawdown ${_fmt(m["max_drawdown_usd"])} ({_fmt(m["max_drawdown_pct"])}% of capital)</div>')
    if e:
        head.append(f'<div class="meta">Evaluated ({html.escape(e["kind"])} days): {e["n_days"]} days; P&amp;L ${_fmt(e["pnl_usd"])} '
                    f'(${_fmt(e["usd_per_day"])}/day); {e["fills"]} fills; {e["n_blocks"]} blocks, block t {_fmt(e["block_pnl_t"])}, '
                    f'95% CI [{_fmt(e["block_ci95"][0])}, {_fmt(e["block_ci95"][1])}]; realised max drawdown '
                    f'${_fmt(e["max_drawdown_usd"])} ({_fmt(e["max_drawdown_pct"])}%)</div>')
    sections += head
    s = _equity_series(run_dir, m, cfg)
    day_rows = [{"day": d, "kind": x["kind"], "pnl": x["pnl"]} for d, x in sorted(m["days"].items())]
    sections += ["<h2>Equity (train, validation, sealed shaded)</h2>", chart_equity(s, m),
                 "<h2>Daily P&amp;L</h2>", chart_bars(day_rows, "pnl", "$ per day"),
                 "<h2>Drawdown</h2>", chart_drawdown(s),
                 "<h2>Edge per day (cents per share)</h2>", chart_bars(m["per_day_edge"], "cents_per_share", "cents/share"),
                 "<h2>Attribution</h2>", chart_attribution(m["attribution"]),
                 _table(["component", "$"], [[k, _fmt(x)] for k, x in m["attribution"].items()]
                        + [["total", _fmt(sum(m["attribution"].values()))]])]
    bm = sorted((r for r in m["by_market"] if r["pnl"] is not None), key=lambda r: r["pnl"])
    pick = bm[:10] + [r for r in bm[-10:] if r not in bm[:10]]
    sections += [f"<h2>By market (worst and best 10 of {len(bm)})</h2>",
                 _table(["market", "day", "fills", "P&L $"], [[r["slug"], r["day"], r["fills"], _fmt(r["pnl"])] for r in pick]),
                 "<h2>By coin and timeframe</h2>",
                 _table(["coin", "timeframe", "markets", "fills", "P&L $"],
                        [[r["coin"], r["timeframe"], r["markets"], r["fills"], _fmt(r["pnl"])] for r in m["by_coin_timeframe"]])]
    r = m["realism"]
    sections += ["<h2>Realism</h2>", _table(["measure", "value"], [
        ["fill rate (limit orders with a fill)", _fmt(r["fill_rate"], "{:.1%}")],
        ["share of fills within 1 s after a Binance move", _fmt(r["share_fills_1s_after_bn_move"], "{:.1%}")],
        ["average queue ahead (shares)", _fmt(r["avg_queue_ahead"])],
        ["cancel model", r["cancel_model"]],
        ["our size / level size, median", _fmt(r["order_vs_level_size_p50"], "{:.2f}")],
        ["voided windows", f'{r["voided_windows"]} ({_fmt(100 * r["voided_share"], "{:.1f}")}%)']])]
    ls = extras.get("latency_sweep")
    if ls:
        sections += ["<h2>Latency sensitivity</h2>",
                     _table(["order latency ms", "P&L $", "$/day", "fills"],
                            [[k, _fmt(x.get("pnl_usd")), _fmt(x.get("usd_per_day")), x.get("fills", "")] for k, x in ls.items()])]
    sw = extras.get("sweep_runs")
    if sw:
        sections += ["<h2>Robustness grid (runs sharing this sweep)</h2>",
                     _table(["params", "P&L $", "$/day", "block t", "verdict", "run"],
                            [[x["label"], _fmt(x["pnl_usd"]), _fmt(x["usd_per_day"]), _fmt(x["t_block"]), x.get("verdict") or "",
                              x["run_id"]] for x in sw], ["self" if x.get("self") else "" for x in sw])]
    sections += ["<h2>Configuration and provenance</h2>",
                 "<pre>" + html.escape(yaml.safe_dump(cfg.to_dict(), sort_keys=True)) + "</pre>",
                 "<pre>" + html.escape(json.dumps({k: prov.get(k) for k in ("kind", "git_commit", "git_dirty", "sim_version",
                                                                             "strategy_version", "data_fingerprint", "python", "packages")},
                                                   indent=1, sort_keys=True, default=str)) + "</pre>"]
    page = (f'<!doctype html><html><head><meta charset="utf-8"><title>{html.escape(cfg.strategy)} report</title>'
            f"<style>{CSS}</style></head><body>" + "\n".join(sections) + "</body></html>")
    out = run_dir / "report.html"
    out.write_text(page)
    return out


def sweep_runs(run_dir: Path, cfg) -> list[dict]:
    """The other runs sharing this run's `sweep:` tag (found through the index), with this run marked."""
    from . import runs

    tags = [t for t in cfg.tags if t.startswith("sweep:")]
    if not tags:
        return []
    idx = runs.load_index()
    idx = idx[idx.tags.fillna("").str.split(",").map(lambda t: tags[0] in t)]
    base = dict(cfg.params)
    out = []
    for r in idx.itertuples():
        try:
            c = yaml.safe_load((runs.runs_dir() / r.path / "config.yaml").read_text()) or {}
        except (OSError, yaml.YAMLError):
            continue
        diff = {k: v for k, v in (c.get("params") or {}).items() if base.get(k) != v}
        other = {k: v for k, v in c.items() if k not in ("params", "tags") and c.get(k) != getattr(cfg, k, None)}
        label = " ".join(f"{k}={v}" for k, v in sorted({**diff, **other}.items())) or "(this run's params)"
        out.append({"run_id": r.run_id, "label": label, "pnl_usd": r.pnl_usd, "usd_per_day": r.usd_per_day,
                    "t_block": None if pd.isna(r.t_block) else r.t_block, "verdict": r.verdict, "self": r.run_id == Path(run_dir).name})
    return out if len(out) > 1 else []


def render_run(run_dir: Path) -> Path:
    """(Re)build `report.html` from a run folder's files. Needs the run in the index to find its sweep siblings."""
    from .config import RunConfig

    run_dir = Path(run_dir)
    cfg = RunConfig.from_dict(yaml.safe_load((run_dir / "config.yaml").read_text()))
    res = json.loads((run_dir / "results.json").read_text())
    prov_f = run_dir / "provenance.json"
    prov = json.loads(prov_f.read_text()) if prov_f.exists() else {}
    extras = {"provenance": prov, "latency_sweep": res.get("latency_sweep"), "sweep_runs": sweep_runs(run_dir, cfg)}
    return render(run_dir, res["metrics"], res["verdict_detail"], cfg, extras)

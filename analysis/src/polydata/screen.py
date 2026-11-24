"""Screening tier: test taker-style ideas in seconds over point-in-time snapshots.

A screen can only reject or promote, never pass: promotion means "worth a full simulator run".

A screen is a rule over the snapshot frame (`research/snapshots.py`) that returns "buy this token now"
rows `slug, t_ms, token ('Up'|'Down'), size?`. Signal inputs must be columns known at `t_ms`
(`up_bid`, `up_ask`, `tau_s`, ...). The `up_ask_300`/`dn_ask_300` (and `_1000`) columns are execution
prices only, they look 300 ms / 1 s past t, and must never feed a rule.

Pessimistic fills: a market buy at the ask `latency_ms` after t (latency 300 or 1000 only, the horizons
the snapshots carry), plus `taker_fee` on entry. Visible depth is NOT checked (snapshots have no sizes):
every signal fills in full at `size` shares (default 10) unless the rule gives a size. Exit is resolution
(`exit="hold"`) or a taker sale at the bid on the first snapshot row at or after t + 2 s (`exit="bid"`,
second fee; held to resolution if that row is later than t + 10 s, or there is no bid). The fee exponent is 1, as for every market today.

Uncertainty: block bootstrap over 15-minute blocks of window start (`window_start_ms // 900000`), 2000
resamples, seed 0. Blocks are all blocks of the group's (non-voided) markets, with or without trades.
Verdict: promote iff the lower bound of the 95% CI of cents/share on validation days is > 0.
"""

from __future__ import annotations

import hashlib
import json
import os
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date, datetime, timezone
from pathlib import Path

import numpy as np
import pandas as pd
import yaml

from .bt import runs
from .bt.splits import REPLAY_START_MS, day_kind
from .config import paths
from .fees import taker_fee

LATENCIES = (300, 1000)
BLOCK_MS = 15 * 60_000
N_BOOT = 2000
EXIT_DELAY_MS = 2000
MAX_EXIT_WAIT_MS = 10_000  # a bid exit whose first row is later than t + 10 s is held to resolution instead
DEFAULT_SIZE = 10.0
NOTE_DEPTH = "visible depth is not checked: every signal fills in full at its size"


@dataclass(frozen=True)
class Screen:
    name: str
    family: str
    rule: Callable[[pd.DataFrame], pd.DataFrame]


REGISTRY: dict[str, Screen] = {}


def register(s: Screen) -> Screen:
    REGISTRY[s.name] = s
    return s


# ---------------------------------------------------------------------------------------- rules

def endgame_favourite(snap: pd.DataFrame, lo: float = 0.80, hi: float = 0.99, max_tau_s: float = 60,
                      max_spread: float = 0.10) -> pd.DataFrame:
    """Buy the favourite (the token with the higher ask) when its ask is in [lo, hi], tau <= max_tau_s and the
    spread <= max_spread on the favourite's own token. The first signal per market only. Uses only columns known at t."""
    fav_up = snap.up_ask >= snap.dn_ask
    fav_ask = np.where(fav_up, snap.up_ask, snap.dn_ask)
    spread = np.where(fav_up, snap.up_ask - snap.up_bid, snap.dn_ask - snap.dn_bid)  # the favourite's own token
    ok = ((fav_ask >= lo) & (fav_ask <= hi) & (snap.tau_s <= max_tau_s) & (spread <= max_spread))
    s = snap[ok].assign(token=np.where(fav_up[ok], "Up", "Down"))
    s = s.sort_values(["slug", "t_ms"], kind="stable").drop_duplicates("slug")
    return s[["slug", "t_ms", "token"]].reset_index(drop=True)


register(Screen("endgame-favourite", "taker", endgame_favourite))


# ---------------------------------------------------------------------------------------- snapshots

def _day(ms) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, timezone.utc).strftime("%Y-%m-%d")


def _kind(day: str) -> str:
    return day_kind(date.fromisoformat(day))


def prepare(snap: pd.DataFrame) -> pd.DataFrame:
    """Drop everything before REPLAY_START_MS (rows and windows starting earlier) and any window that ends on
    a sealed day (its tail would sit in data that polydata.connect() hides)."""
    s = snap[(snap.t_ms >= REPLAY_START_MS) & (snap.window_start_ms >= REPLAY_START_MS)]
    end_kind = s.window_end_ms.map(lambda ms: _kind(_day(ms - 1)))
    return s[end_kind.to_numpy() != "sealed"].reset_index(drop=True)


def snapshots_path() -> Path:
    return paths().root / "research" / "snapshots.parquet"


def load_snapshots(rebuild: bool | None = None) -> tuple[pd.DataFrame, float | None]:
    """The prepared snapshots; rebuilds the file first when missing or older than the newest Parquet.
    Returns (frame, rebuild seconds or None)."""
    import time

    f = snapshots_path()
    newest = max((p.stat().st_mtime for p in paths().parquet.glob("**/*.parquet")
                 if p.parent.name.startswith("date=") and _kind(p.parent.name[5:]) != "sealed"), default=0.0)
    secs = None
    if rebuild or (rebuild is None and (not f.exists() or f.stat().st_mtime < newest)):
        from .research import snapshots

        t = time.time()
        snapshots.build()
        secs = time.time() - t
    return prepare(pd.read_parquet(f)), secs


def voided_slugs(snap: pd.DataFrame, max_gap_ms_pm: int = 5000, max_gap_ms_bn: int = 1000) -> dict[str, str]:
    """Markets of `snap` that overlap a data gap (or have no outcome), as {slug: reason}."""
    from .bt import gaps
    from .views import connect

    mk = snap.drop_duplicates("slug")[["slug", "window_start_ms", "window_end_ms"]]
    con = connect()
    try:
        meta = con.execute("SELECT slug, series, coin, outcome FROM markets").df()
        mk = mk.merge(meta, on="slug", how="left")
        g = gaps.gap_table(con, int(mk.window_start_ms.min()), int(mk.window_end_ms.max()),
                           max_gap_ms_pm=max_gap_ms_pm, max_gap_ms_bn=max_gap_ms_bn)
    finally:
        con.close()
    v = gaps.voided_markets(mk, g, max_gap_ms_pm, max_gap_ms_bn)
    return dict(zip(v.slug, v.reason))


# ---------------------------------------------------------------------------------------- evaluation

def fills(snap: pd.DataFrame, signals: pd.DataFrame, latency_ms: int = 300, exit: str = "hold",
          size: float = DEFAULT_SIZE) -> pd.DataFrame:
    """One row per executed signal: slug, t_ms, token, size, price, fee, exit_px, pnl."""
    if latency_ms not in LATENCIES:
        raise ValueError(f"latency_ms must be one of {LATENCIES} (the snapshots carry asks at those horizons), not {latency_ms}")
    if exit not in ("hold", "bid"):
        raise ValueError(f"exit must be 'hold' or 'bid', not {exit!r}")
    cols = ["slug", "t_ms", "token"] + (["size"] if "size" in signals else [])
    sg = signals[cols].drop_duplicates(["slug", "t_ms"])
    need = ["slug", "t_ms", "fee_rate", "outcome_up", "up_bid", "dn_bid", f"up_ask_{latency_ms}", f"dn_ask_{latency_ms}"]
    f = sg.merge(snap[need], on=["slug", "t_ms"], how="inner")
    up = f.token == "Up"
    f["price"] = np.where(up, f[f"up_ask_{latency_ms}"], f[f"dn_ask_{latency_ms}"]).astype(float)
    f["size"] = f["size"].fillna(size).astype(float) if "size" in f else float(size)
    f = f[(f.price > 0) & (f.price < 1) & (f["size"] > 0)].copy()
    f["fee"] = [taker_fee(n, p, r) for n, p, r in zip(f["size"], f.price, f.fee_rate)]
    won = np.where(up[f.index], f.outcome_up, 1 - f.outcome_up).astype(float)
    f["exit_px"] = np.where(won > 0, 1.0, 0.0)  # settlement payoff per share
    f["exit_fee"] = 0.0
    if exit == "bid" and len(f):
        grid = snap[["slug", "t_ms", "up_bid", "dn_bid"]].rename(columns={"t_ms": "exit_t", "up_bid": "xu", "dn_bid": "xd"})
        left = f.assign(exit_t=f.t_ms + EXIT_DELAY_MS, _i=np.arange(len(f))).sort_values("exit_t")
        m = pd.merge_asof(left, grid.sort_values("exit_t"), on="exit_t", by="slug", direction="forward", tolerance=MAX_EXIT_WAIT_MS - EXIT_DELAY_MS)
        m = m.sort_values("_i")
        bid = np.where(m.token == "Up", m.xu, m.xd).astype(float)
        ok = np.isfinite(bid) & (bid > 0) & (bid < 1)
        f["exit_px"] = np.where(ok, bid, f.exit_px)
        f["exit_fee"] = [taker_fee(n, b, r) if o else 0.0 for n, b, r, o in zip(f["size"], bid, f.fee_rate, ok)]
    f["pnl"] = f["size"] * (f.exit_px - f.price) - f.fee - f.exit_fee
    return f.reset_index(drop=True)


def _stats(mk: pd.DataFrame) -> dict:
    """Stats of a market frame with columns block, pnl, size, n (every market, traded or not) and day."""
    n_days = int(mk.day.nunique())
    b = mk.groupby("block").agg(pnl=("pnl", "sum"), size=("size", "sum"), n=("n", "sum"))
    pnl, size, n = float(b.pnl.sum()), float(b["size"].sum()), int(b.n.sum())
    out = {"n_trades": n, "shares": size, "pnl_usd": pnl, "n_days": n_days, "n_blocks": len(b),
           "cents_per_share": pnl / size * 100 if size else None,
           "usd_per_day": pnl / n_days if n_days else None, "ci_cents": None, "ci_usd_day": None}
    if n and len(b):
        rng = np.random.default_rng(0)
        idx = rng.integers(0, len(b), (N_BOOT, len(b)))
        p, s = b.pnl.to_numpy()[idx].sum(1), b["size"].to_numpy()[idx].sum(1)
        cents = np.where(s > 0, p / np.where(s > 0, s, 1) * 100, np.nan)
        out["ci_cents"] = [float(x) for x in np.nanpercentile(cents, [2.5, 97.5])]
        out["ci_usd_day"] = [float(x) for x in np.percentile(p / n_days, [2.5, 97.5])]
    return out


def evaluate(snap: pd.DataFrame, signals: pd.DataFrame, latency_ms: int = 300, exit: str = "hold",
             size: float = DEFAULT_SIZE, voided=()) -> dict:
    """Score `signals` on `snap`. `voided` (slugs, or a {slug: reason} dict) are skipped entirely."""
    voided = set(voided)
    n_voided = len(voided & set(snap.slug))
    snap = snap[~snap.slug.isin(voided)]
    f = fills(snap, signals[~signals.slug.isin(voided)], latency_ms, exit, size)
    mk = snap.drop_duplicates("slug")[["slug", "coin", "timeframe", "window_start_ms"]].copy()
    mk["block"] = mk.window_start_ms // BLOCK_MS
    mk["day"] = mk.window_start_ms.map(_day)
    mk["kind"] = mk.day.map(_kind)
    per = f.groupby("slug").agg(pnl=("pnl", "sum"), size=("size", "sum"), n=("pnl", "size"))
    mk = mk.join(per, on="slug").fillna({"pnl": 0.0, "size": 0.0, "n": 0})
    by_ct = {f"{c}/{t}": _stats(g) for (c, t), g in mk.groupby(["coin", "timeframe"])}
    by_kind = {k: _stats(g) for k, g in mk.groupby("kind")}
    return {"latency_ms": latency_ms, "exit": exit, "default_size": size, "note": NOTE_DEPTH,
            "n_voided": n_voided, "overall": _stats(mk),
            "by_coin_timeframe": by_ct, "by_day_kind": by_kind}


def verdict(result: dict) -> dict:
    """promote iff the 95% CI lower bound of cents/share on validation days is > 0, else reject."""
    v = result["by_day_kind"].get("validation")
    if not v:
        return {"verdict": "reject", "note": "no validation days yet"}
    if not v["n_trades"] or not v["ci_cents"]:
        return {"verdict": "reject", "note": "no trades on validation days"}
    lo = v["ci_cents"][0]
    if lo > 0:
        return {"verdict": "promote", "note": f"validation CI lower bound {lo:.2f} c/share > 0"}
    return {"verdict": "reject", "note": f"validation CI lower bound {lo:.2f} c/share is not above 0"}


# ---------------------------------------------------------------------------------------- run folder

def _version(name: str, config: dict) -> str:
    h = hashlib.sha256(Path(__file__).read_bytes())
    h.update(b"\0" + name.encode() + b"\0" + json.dumps(config, sort_keys=True, default=str).encode())
    return h.hexdigest()[:12]


def run(name: str, latency_ms: int = 300, exit: str = "hold", size: float = DEFAULT_SIZE, coins: list[str] | None = None,
        timeframes: list[str] | None = None, tags: list[str] | None = None, snap: pd.DataFrame | None = None,
        voided=None, rebuild_index: bool = True) -> tuple[Path, dict]:
    """Evaluate the registered screen `name` and record a run folder of kind `screen`. Returns (folder, results).
    `snap`/`voided` are for tests; by default the snapshots are loaded (rebuilt if stale) and gaps computed."""
    scr = REGISTRY[name]
    rebuild_s = None
    if snap is None:
        snap, rebuild_s = load_snapshots()
    else:
        snap = prepare(snap)
    if coins:
        snap = snap[snap.coin.isin(coins)]
    if timeframes:
        snap = snap[snap.timeframe.isin(timeframes)]
    snap = snap[snap.window_start_ms.map(lambda ms: _kind(_day(ms)) != "sealed")].reset_index(drop=True)
    if voided is None:
        voided = voided_slugs(snap) if len(snap) else {}
    sigs = scr.rule(snap)
    res = evaluate(snap, sigs, latency_ms, exit, size, voided)
    v = verdict(res)
    days = sorted(set(snap.window_start_ms.map(_day)))
    kinds: dict[str, int] = {}
    for d in days:
        kinds[_kind(d)] = kinds.get(_kind(d), 0) + 1
    o = res["overall"]
    results = {"screen": name, "pnl_usd": o["pnl_usd"], "usd_per_day": o["usd_per_day"] or 0.0, "n_days": len(days),
               "days": days, "day_kinds": kinds, "t_block": None, "verdict": v["verdict"], "verdict_note": v["note"],
               "metrics": res}
    from .bt.metrics import clean

    config = {"screen": name, "strategy": name, "latency_ms": latency_ms, "order_latency_ms": latency_ms, "exit": exit,
              "size": size, "fee_exponent": 1, "coins": sorted(set(snap.coin)), "timeframes": sorted(set(snap.timeframe)),
              "tags": list(tags or []), "block_ms": BLOCK_MS, "n_boot": N_BOOT, "seed": 0}
    try:
        fp = runs.data_fingerprint(days)
    except ValueError:
        fp = None
    prov = {"kind": "screen", "family": scr.family, "strategy": name, "strategy_version": _version(name, config),
            "sim_version": None, "data_fingerprint": fp, "git_commit": (runs._git("rev-parse", "HEAD") or "").strip() or None,
            "host": runs.host_hash(), "snapshot_rebuild_s": rebuild_s}
    d = runs.new_run_dir(scr.family, name, datetime.now(timezone.utc))
    (d / "config.yaml").write_text(yaml.safe_dump(config, sort_keys=True))
    (d / "provenance.json").write_text(json.dumps(prov, indent=1, sort_keys=True, default=str))
    (d / "results.json").write_text(json.dumps(clean(results), indent=1, sort_keys=True, allow_nan=False) + "\n")
    if rebuild_index:
        runs.rebuild_index()
    return d, results

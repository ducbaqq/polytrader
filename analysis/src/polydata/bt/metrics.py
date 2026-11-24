"""Run metrics and the pass criteria (spec "Report" and "Pass criteria").

`compute` reads a `RunResult` and returns a JSON-safe dict. Everything is attributed to the day of the market's
`window_start`. The `eval` block holds the numbers the verdict is judged on: the validation days of an ordinary
run (provisional verdict) or all days of a run that has only sealed days (the final verdict). `verdict` applies the
criteria to it. `realism.order_vs_level_size_p50` is our size / (queue ahead + our size) at acceptance (the orders
frame has no level size). The 400 ms check uses per-day P&L of the latency-sweep runs on the evaluated days only. All values are deterministic (no wall clock) and finite (NaN -> None).
"""

from __future__ import annotations

import math
from collections.abc import Callable
from datetime import datetime, timezone

import numpy as np
import pandas as pd
from scipy.stats import norm

from .config import RunConfig
from .engine import RunResult

BLOCK_MS = 900_000
N_BOOT = 2000
MIN_T = 3.0
MIN_BLOCKS = 30  # t and the bootstrap CI are not trusted below this many evaluated 15-minute blocks
MIN_DAYS_POS_SHARE = 7
MAX_DD_PCT = 10.0
MIN_FILLS_PER_DAY = 200
POS_DAY_SHARE = 0.7
NOT_JUDGED = "not judged"
NO_VALIDATION = "no validation days yet"


def _day(ms) -> str:
    return datetime.fromtimestamp(int(ms) / 1000, tz=timezone.utc).strftime("%Y-%m-%d")


def clean(x):
    """Recursively turn numpy scalars into Python numbers and NaN/inf into None (results.json is allow_nan=False)."""
    if isinstance(x, dict):
        return {str(k): clean(v) for k, v in x.items()}
    if isinstance(x, (list, tuple)):
        return [clean(v) for v in x]
    if isinstance(x, (np.bool_, bool)):
        return bool(x)
    if isinstance(x, (np.integer, int)):
        return int(x)
    if isinstance(x, (np.floating, float)):
        return None if not math.isfinite(float(x)) else float(x)
    return x


# ---------------------------------------------------------------------------------------- equity

def total_equity(equity: pd.DataFrame, capital_total: float, capital_each: float) -> pd.Series:
    """Total equity over time, indexed by ms: each budget's last value carried forward on the union of
    timestamps (before its first sample a budget holds its starting capital), summed, with a starting point."""
    if equity is None or len(equity) == 0:
        return pd.Series(dtype="float64")
    cols = {}
    for b, g in equity.groupby("budget"):
        cols[b] = g.drop_duplicates("ms", keep="last").set_index("ms")["equity"].astype(float)
    df = pd.DataFrame(cols).sort_index()
    df = df.ffill().fillna(capital_each)
    s = df.sum(axis=1)
    s.loc[int(s.index[0]) - 1] = capital_total
    return s.sort_index()


def capitals(cfg: RunConfig) -> tuple[float, float]:
    """(total capital, capital per budget): the engine gives each coin capital / len(coins) in per-coin mode."""
    total = float(cfg.capital)
    return total, (total / len(cfg.coins) if cfg.capital_mode == "per_coin" else total)


def realised_drawdown(mk: pd.DataFrame, capital_total: float) -> float:
    """Drawdown of capital_total + cumulative P&L of settled markets in settle order (realised only)."""
    mk = mk[~mk["voided"].astype(bool)]
    if len(mk) == 0:
        return 0.0
    order = mk["settle_ms"].astype(float).fillna(mk["window_end_ms"].astype(float)).to_numpy().argsort(kind="stable")
    c = np.concatenate([[0.0], np.cumsum(mk["pnl"].astype(float).fillna(0.0).to_numpy()[order])]) + capital_total
    return float((np.maximum.accumulate(c) - c).max())


def max_drawdown(s: pd.Series) -> float:
    if len(s) == 0:
        return 0.0
    return float((s.cummax() - s).max())


# ---------------------------------------------------------------------------------------- stats

def block_stats(blocks: np.ndarray) -> tuple[float | None, list[float | None]]:
    """t = mean/se of the block P&L, and the 95% bootstrap interval of the total (2000 resamples of blocks, seed 0)."""
    n = len(blocks)
    if n < 2:
        return None, [None, None]
    sd = blocks.std(ddof=1)
    t = float(blocks.mean() / (sd / math.sqrt(n))) if sd > 0 else None
    rng = np.random.default_rng(0)
    pick = rng.integers(0, n, (N_BOOT, n))
    tot = blocks[pick].sum(axis=1)
    return t, [float(np.percentile(tot, 2.5)), float(np.percentile(tot, 97.5))]


def _stats(mk: pd.DataFrame, fills: pd.DataFrame, eq: pd.Series, capital_total: float) -> dict:
    mk = mk[~mk["voided"].astype(bool)]
    pnl = mk["pnl"].astype(float).fillna(0.0)
    days = sorted({_day(m) for m in mk["window_start_ms"]}) if len(mk) else []
    block = (mk["window_start_ms"].astype("int64") // BLOCK_MS)
    bl = pnl.groupby(block).sum().to_numpy(dtype=float) if len(mk) else np.array([])
    t, ci = block_stats(bl)
    mdd = max_drawdown(eq)
    shares = float(fills["size"].sum()) if len(fills) else 0.0
    total = float(pnl.sum())
    return {"pnl_usd": total, "n_days": len(days), "usd_per_day": total / len(days) if days else 0.0,
            "return_on_capital": total / capital_total if capital_total else 0.0,
            "n_blocks": len(bl), "block_pnl_t": t, "block_ci95": ci,
            "max_drawdown_usd": mdd, "max_drawdown_pct": 100 * mdd / capital_total if capital_total else 0.0,
            "fills": len(fills), "fills_per_day": len(fills) / len(days) if days else 0.0,
            "cents_per_share": 100 * total / shares if shares else None}


def attribution(fills: pd.DataFrame) -> dict:
    """P&L split over all fills (maker and taker), in token terms; sums to the run's P&L.

    spread = (mid_before - px)*dir*size; adverse = (mid_10s - mid_before)*dir*size (mid_10s := mid_before when
    unknown); mid_before is the mid 200 ms before the fill (mid_at_fill already includes the impact of the fill's own
    trade, which would book adverse selection as negative spread); inventory = (settle - mid_10s)*dir*size; fees = -sum(fee). Fills without a settlement (voided) are skipped."""
    f = fills[fills["settle_value"].notna()] if len(fills) else fills
    if len(f) == 0:
        return {"spread": 0.0, "adverse": 0.0, "inventory": 0.0, "fees": 0.0}
    d = np.where(f["side"].astype(str).str.lower().str.startswith("b"), 1.0, -1.0)
    px, sz = f["px"].astype(float).to_numpy(), f["size"].astype(float).to_numpy()
    mb = f["mid_before"] if "mid_before" in f else f["mid_at_fill"]
    m0 = mb.astype(float).fillna(f["px"].astype(float)).to_numpy()
    m10 = f["mid_10s"].astype(float).to_numpy()
    m10 = np.where(np.isnan(m10), m0, m10)
    s = f["settle_value"].astype(float).to_numpy()
    return {"spread": float(((m0 - px) * d * sz).sum()), "adverse": float(((m10 - m0) * d * sz).sum()),
            "inventory": float(((s - m10) * d * sz).sum()), "fees": -float(f["fee"].astype(float).sum())}


def realism(result: RunResult, cfg: RunConfig, mk: pd.DataFrame) -> dict:
    o, f = result.orders, result.fills
    out = {"cancel_model": cfg.cancel_model, "voided_windows": int(mk["voided"].sum()) if len(mk) else 0,
           "voided_share": float(mk["voided"].mean()) if len(mk) else 0.0,
           "fill_rate": None, "share_fills_1s_after_bn_move": None, "avg_queue_ahead": None,
           "order_vs_level_size_p50": None}
    if len(o):
        lim = o[(o["event"] == "placed") & (o["kind"] == "limit")]
        keys = set(zip(lim["budget"], lim["order_id"]))
        filled = set(zip(f["budget"], f["order_id"])) if len(f) else set()
        out["fill_rate"] = len(keys & filled) / len(keys) if keys else None
        acc = o[(o["event"] == "accepted") & (o["kind"] == "limit")]
        if len(acc):
            q = acc["queue_ahead"].astype(float)
            out["avg_queue_ahead"] = float(q.mean())
            # our size against the size of the level we joined (queue ahead plus ourselves)
            out["order_vs_level_size_p50"] = float((acc["size"].astype(float) / (q + acc["size"].astype(float))).median())
    if len(f):
        out["share_fills_1s_after_bn_move"] = float(f["bn_move_1s"].astype(bool).mean())
    return out


def eval_pnl(result: RunResult, day_kind_of: Callable[[str], str]) -> float | None:
    """P&L on the evaluated days only (validation days, or all days of an all-sealed run); None if there are none."""
    mk = result.markets
    if len(mk) == 0:
        return None
    mk = mk[~mk["voided"].astype(bool)]
    kinds = [day_kind_of(_day(x)) for x in mk["window_start_ms"]]
    sel = [k == "validation" for k in kinds]
    if not any(sel):
        if not kinds or set(kinds) != {"sealed"}:
            return None
        sel = [True] * len(kinds)
    return float(mk["pnl"].astype(float).fillna(0.0)[sel].sum())


def compute(result: RunResult, cfg: RunConfig, day_kind_of: Callable[[str], str]) -> dict:
    mk = result.markets
    fills = result.fills
    cap_total, cap_each = capitals(cfg)
    eq = total_equity(result.equity, cap_total, cap_each)

    if len(mk):
        mk = mk.copy()
        mk["day"] = [_day(m) for m in mk["window_start_ms"]]
        mk["kind"] = [day_kind_of(d) for d in mk["day"]]
    else:
        mk = mk.assign(day=[], kind=[])
    if len(fills):
        fills = fills.merge(mk[["slug", "day"]].drop_duplicates("slug"), on="slug", how="left")
    else:
        fills = fills.assign(day=[])

    m = _stats(mk, fills, eq, cap_total)
    nv = mk[~mk["voided"].astype(bool)] if len(mk) else mk
    daily = nv.groupby("day")["pnl"].apply(lambda s: float(s.astype(float).fillna(0).sum())) if len(nv) else {}
    kind_of = dict(zip(nv["day"], nv["kind"])) if len(nv) else {}
    all_days = sorted(set(nv["day"])) if len(nv) else []  # days with at least one non-voided market
    m["days"] = {d: {"kind": kind_of[d], "pnl": float(daily.get(d, 0.0))} for d in all_days}
    m["attribution"] = attribution(fills)
    m["realism"] = realism(result, cfg, mk)
    m["capital_total"] = cap_total

    fpm = fills.groupby("slug").size() if len(fills) else pd.Series(dtype=int)
    bym = []
    for r in mk.sort_values("slug", kind="stable").itertuples():
        bym.append({"slug": r.slug, "coin": r.coin, "timeframe": r.timeframe, "day": r.day, "voided": bool(r.voided),
                    "pnl": None if r.voided else float(r.pnl), "fills": int(fpm.get(r.slug, 0))})
    m["by_market"] = bym
    bct = []
    if len(nv):
        for (coin, tf), g in nv.groupby(["coin", "timeframe"]):
            ss = fills[fills["slug"].isin(g["slug"])] if len(fills) else fills
            bct.append({"coin": coin, "timeframe": tf, "markets": len(g), "pnl": float(g["pnl"].astype(float).sum()),
                        "fills": len(ss)})
    m["by_coin_timeframe"] = bct
    pde = []
    for d in all_days:
        fd = fills[fills["day"] == d] if len(fills) else fills
        sh = float(fd["size"].sum()) if len(fd) else 0.0
        pde.append({"day": d, "kind": kind_of[d], "pnl": m["days"][d]["pnl"], "shares": sh,
                    "cents_per_share": 100 * m["days"][d]["pnl"] / sh if sh else None})
    m["per_day_edge"] = pde

    # the numbers the verdict is judged on
    kinds = set(mk["kind"]) if len(mk) else set()
    if "validation" in kinds:
        ev_kind = "validation"
    elif kinds == {"sealed"}:
        ev_kind = "sealed"
    else:
        ev_kind = None
    m["eval"] = None
    if ev_kind:
        sel = mk[mk["kind"] == ev_kind]
        days = set(sel[~sel["voided"].astype(bool)]["day"])
        efills = fills[fills["day"].isin(days)] if len(fills) else fills
        e = _stats(sel, efills, pd.Series(dtype="float64"), cap_total)
        e["max_drawdown_usd"] = realised_drawdown(sel, cap_total)  # realised only; never touches train days
        e["max_drawdown_pct"] = 100 * e["max_drawdown_usd"] / cap_total if cap_total else 0.0
        dp = [m["days"][d]["pnl"] for d in sorted(days)]
        k = max(1, round(len(dp) / 3))
        e["kind"] = ev_kind
        e["recent_third_pnl"] = float(sum(dp[-k:])) if dp else 0.0
        e["positive_day_share"] = float(np.mean([p > 0 for p in dp])) if dp else None
        m["eval"] = e
    return m


# ---------------------------------------------------------------------------------------- verdict

def t_threshold(kind: str, n_looks: int) -> float:
    if kind != "final":
        return MIN_T
    return float(max(MIN_T, norm.ppf(1 - 0.05 / max(n_looks, 1))))


def _chk(value, threshold, passed) -> dict:
    return {"value": value, "threshold": threshold, "passed": passed}


def verdict(m: dict, kind: str = "provisional", n_looks: int = 0, min_usd_per_day: float = 10.0) -> dict:
    """Apply the spec's pass criteria to `m["eval"]`. `passed` is False with a note when there is nothing to judge.
    A check that cannot be judged has `passed` None and a `value` of "not judged"; it does not fail the verdict."""
    e = m.get("eval")
    if not e:
        label = "no sealed-day data" if kind == "final" else NO_VALIDATION
        return {"passed": False, "label": label, "kind": kind, "checks": {}, "t_threshold": t_threshold(kind, n_looks + 1)}
    n_eff = n_looks + 1  # the look being judged counts
    thr = t_threshold(kind, n_eff)
    lo = (e["block_ci95"] or [None])[0]
    t = e["block_pnl_t"]
    few = e["n_blocks"] < MIN_BLOCKS
    c = {
        "pnl > 0": _chk(e["pnl_usd"], 0, e["pnl_usd"] > 0),
        "ci95_lo > 0": _chk(lo, 0, not few and lo is not None and lo > 0),
        "t > threshold": _chk(t, thr, not few and t is not None and t > thr),
        "recent third of days > 0": _chk(e["recent_third_pnl"], 0, e["recent_third_pnl"] > 0),
        "usd_per_day >= min": _chk(e["usd_per_day"], min_usd_per_day, e["usd_per_day"] >= min_usd_per_day),
        "fills_per_day >= 200": _chk(e["fills_per_day"], MIN_FILLS_PER_DAY, e["fills_per_day"] >= MIN_FILLS_PER_DAY),
        "max_drawdown_pct < 10": _chk(e["max_drawdown_pct"], MAX_DD_PCT, e["max_drawdown_pct"] < MAX_DD_PCT),
    }
    if few:
        for k in ("ci95_lo > 0", "t > threshold"):
            c[k]["note"] = f"too few blocks ({e['n_blocks']} < {MIN_BLOCKS})"
    if e["n_days"] >= MIN_DAYS_POS_SHARE:
        s = e["positive_day_share"]
        c["positive days >= 70%"] = _chk(s, POS_DAY_SHARE, s is not None and s >= POS_DAY_SHARE)
    else:
        c["positive days >= 70%"] = _chk(e["positive_day_share"], POS_DAY_SHARE, None)
        c["positive days >= 70%"]["note"] = f"{NOT_JUDGED}: needs {MIN_DAYS_POS_SHARE}+ days"
    sw = m.get("latency_sweep") or {}
    p400 = (sw.get("400") or {}).get("eval_pnl_usd")
    if p400 is None:
        c["profitable at 400 ms"] = _chk(None, 0, None)
        c["profitable at 400 ms"]["note"] = f"{NOT_JUDGED}: no latency sweep"
    else:
        c["profitable at 400 ms"] = _chk(p400, 0, p400 > 0)
    # only the positive-days check may be unjudged and still pass; a missing 400 ms sweep is incomplete
    passed = all(v["passed"] is True for k, v in c.items() if k != "positive days >= 70%")
    passed = passed and c["positive days >= 70%"]["passed"] is not False
    suffix = " (provisional)" if kind != "final" else ""
    if not passed and not any(v["passed"] is False for v in c.values()) and c["profitable at 400 ms"]["passed"] is None:
        label = "INCOMPLETE (no 400 ms sweep)"
    else:
        label = ("PASS" if passed else "FAIL") + suffix
    return {"passed": passed, "label": label, "kind": kind, "t_threshold": thr, "n_looks": n_eff if kind == "final" else None,
            "checks": c}

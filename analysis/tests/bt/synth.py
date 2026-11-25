"""Synthetic recorder-shaped sessions (live/replay parity tests): `make_session` gives `LiveRow`s in nondecreasing
`recv_ms`, `write_store` writes rows in the recorder's raw layout so `polydata build` turns them into a store.

What a session holds, per coin and timeframe:
- `pm_market_meta` discovery rows (Gamma-shaped event: `slug`, `markets[0]` with `conditionId`, `clobTokenIds`,
  `outcomes`, `endDate`, `feeSchedule`, `orderPriceMinTickSize`, `orderMinSize`, `cryptoMarketConfig.asset`; `series`),
  `meta_lead_ms` before each window start (never before the session start).
- Per market from its subscription to just after its end: book snapshots (Up and Down, sometimes one only), Up level
  changes with Down mirrors (mostly in the same message, some in a later message, some missing), trades on both
  tokens, `pm_best_bid_ask` rows; one `tick_size_change` (`pm_other`) per session.
- `rtds_price` Chainlink rows every second per coin (some received twice, some relay rows), `bn_fut_bbo` rows every
  `bn_ms` per symbol (some sharing a `recv_ms`), `pm_resolution` rows 20-60 s after each window end (`resolutions`).
Polymarket feed delays (`recv - ts`) are 20-400 ms; message rows share a `recv_ms`.
"""

from __future__ import annotations

import random
from datetime import datetime, timezone
from pathlib import Path

from polydata import schema
from polydata.bt.live import LiveRow

TF_MS = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000}
BASE = {"btc": 60_000.0, "eth": 3_000.0, "sol": 150.0, "xrp": 0.6}
STREAM_RANK = {s: i for i, s in enumerate(["pm_market_meta", "pm_book", "pm_price_change", "pm_best_bid_ask",
                                           "pm_trade", "pm_other", "rtds_price", "bn_fut_bbo", "pm_resolution"])}


def slug_of(coin: str, tf: str, ws: int) -> str:
    return f"{coin}-updown-{tf}-{ws // 1000}"


def _iso(ms: int) -> str:
    return datetime.fromtimestamp(ms / 1000, timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def market_obj(coin: str, tf: str, ws: int) -> dict:
    we = ws + TF_MS[tf]
    n = ws // 1000
    return {
        "slug": slug_of(coin, tf, ws), "conditionId": f"0x{coin}{tf}{n}", "endDate": _iso(we),
        "outcomes": '["Up", "Down"]', "clobTokenIds": f'["{n}1", "{n}2"]',
        "resolutionSource": f"https://data.chain.link/streams/{coin}-usd",
        "cryptoMarketConfig": {"asset": coin, "twapLookbackSeconds": 60},
        "feeSchedule": {"rate": 0.05, "exponent": 1, "takerOnly": True, "rebateRate": 0.2},
        "orderPriceMinTickSize": 0.01, "orderMinSize": 5,
    }


def meta_row(coin: str, tf: str, ws: int, recv: int) -> LiveRow:
    m = market_obj(coin, tf, ws)
    return LiveRow("pm_market_meta", recv, {"series": f"{coin}-up-or-down-{tf}", "event": {"slug": m["slug"], "markets": [m]}})


def resolution_row(coin: str, tf: str, ws: int, recv: int, outcome: str, ptb=None, final=None, source=None) -> LiveRow:
    m = market_obj(coin, tf, ws)
    d = {"slug": m["slug"], "outcome": outcome,
         "event": {"slug": m["slug"], "eventMetadata": {"priceToBeat": ptb, "finalPrice": final}, "markets": [m]}}
    if source is not None:
        d["source"] = source
    return LiveRow("pm_resolution", recv, d)


def _c(x: int) -> str:  # cents -> price text
    return f"{x / 100:.2f}"


class _Book:
    """One market's Up book in cents: bids/asks {cents: size}."""

    def __init__(self, rng: random.Random, c: int):
        self.rng = rng
        self.bids = {c - i: float(rng.randint(10, 200)) for i in range(5)}
        self.asks = {c + 1 + i: float(rng.randint(10, 200)) for i in range(5)}

    def best(self) -> tuple[int | None, int | None]:
        b = max((p for p, s in self.bids.items() if s > 0), default=None)
        a = min((p for p, s in self.asks.items() if s > 0), default=None)
        return b, a

    def recenter(self, c: int) -> list[tuple[str, int, float]]:
        """Changes (Up side, cents, size) moving the book's centre to c."""
        out = []
        for p in [p for p, s in self.bids.items() if p > c and s > 0]:
            self.bids[p] = 0.0
            out.append(("BUY", p, 0.0))
        for p in [p for p, s in self.asks.items() if p <= c and s > 0]:
            self.asks[p] = 0.0
            out.append(("SELL", p, 0.0))
        for i in range(3):
            p = c - i
            if 1 <= p <= 99 and self.bids.get(p, 0.0) <= 0:
                self.bids[p] = float(self.rng.randint(10, 200))
                out.append(("BUY", p, self.bids[p]))
            q = c + 1 + i
            if 1 <= q <= 99 and self.asks.get(q, 0.0) <= 0:
                self.asks[q] = float(self.rng.randint(10, 200))
                out.append(("SELL", q, self.asks[q]))
        return out

    def snapshot(self) -> tuple[str, str]:
        bids = "|".join(f"{_c(p)}:{s:g}" for p, s in sorted(self.bids.items(), reverse=True) if s > 0)
        asks = "|".join(f"{_c(p)}:{s:g}" for p, s in sorted(self.asks.items()) if s > 0)
        return bids, asks

    def down_snapshot(self) -> tuple[str, str]:
        bids = "|".join(f"{_c(100 - p)}:{s:g}" for p, s in sorted(self.asks.items()) if s > 0)
        asks = "|".join(f"{_c(100 - p)}:{s:g}" for p, s in sorted(self.bids.items(), reverse=True) if s > 0)
        return bids, asks


def _market_rows(rng: random.Random, coin: str, tf: str, ws: int, sub: int, stop: int, out: list, tick_change: bool,
                 skew_ahead: bool) -> str:
    """Polymarket rows of one market from `sub` to `stop`; returns the outcome its book drifted to."""
    slug = slug_of(coin, tf, ws)
    we = ws + TF_MS[tf]
    up, dn = f"{slug}|U", f"{slug}|D"
    target = rng.choice([85, 15])
    p = 50.0
    book = _Book(rng, 50)
    c = 50
    ts = sub + 50
    next_snap = ts
    ticked = not tick_change
    hashes = 0

    def recv_of(t: int) -> int:
        return t + rng.randint(20, 400)

    def bb_ba(b, a, down=False):
        if not down:
            return ("" if b is None else _c(b)), ("" if a is None else _c(a))
        return ("" if a is None else _c(100 - a)), ("" if b is None else _c(100 - b))

    def change(t: int, r: int, side: str, px: int, size: float):
        b, a = book.best()
        ub, ua = bb_ba(b, a)
        out.append(LiveRow("pm_price_change", r, (str(t), up, side, _c(px), f"{size:g}", ub, ua)))
        u = rng.random()
        if u < 0.05:
            return  # the Down mirror is missing
        db, da = bb_ba(b, a, True)
        r2 = r if u < 0.85 else r + rng.randint(1, 60)  # mirrored in a later message
        out.append(LiveRow("pm_price_change", r2, (str(t), dn, "SELL" if side == "BUY" else "BUY", _c(100 - px),
                                                    f"{size:g}", db, da)))

    while ts < stop:
        frac = min(max((ts - ws) / (we - ws), 0.0), 1.0)
        p += rng.gauss(0, 0.6) + (target - p) * 0.004 * (1 + 6 * frac)
        p = min(max(p, 3.0), 97.0)
        nc = int(round(p))
        r = recv_of(ts)
        if skew_ahead and rng.random() < 0.01:
            r = max(ts - 900, sub + 1)  # Polymarket's clock ahead of ours, within the skew
        if ts >= next_snap:
            hashes += 1
            which = rng.random()
            if which < 0.9:
                bids, asks = book.snapshot()
                out.append(LiveRow("pm_book", r, (str(ts), up, bids, asks, f"h{hashes}u")))
            if which > 0.1:
                bids, asks = book.down_snapshot()
                out.append(LiveRow("pm_book", r + (0 if rng.random() < 0.7 else rng.randint(1, 30)),
                                   (str(ts), dn, bids, asks, f"h{hashes}d")))
            next_snap = ts + rng.randint(3000, 9000)
        elif nc != c:
            for side, px, size in book.recenter(nc):
                change(ts, r, side, px, size)
            c = nc
        else:
            side = rng.choice(["BUY", "SELL"])
            px = c - rng.randint(0, 4) if side == "BUY" else c + 1 + rng.randint(0, 4)
            if 1 <= px <= 99:
                lv = book.bids if side == "BUY" else book.asks
                size = 0.0 if rng.random() < 0.25 else float(rng.randint(5, 250))
                lv[px] = size
                change(ts, r, side, px, size)
        if rng.random() < 0.3:
            b, a = book.best()
            tt = ts + rng.randint(1, 6)
            taker = rng.choice(["BUY", "SELL"])
            lvpx = a if taker == "BUY" else b
            if lvpx is not None:
                lv = book.asks if taker == "BUY" else book.bids
                q = min(lv[lvpx], float(rng.randint(1, 40)))
                if q > 0:
                    lv[lvpx] = max(lv[lvpx] - q, 0.0)
                    change(tt - 3 if tt - 3 > ts else ts, r, "SELL" if taker == "BUY" else "BUY", lvpx, lv[lvpx])
                    tr = recv_of(tt)
                    if rng.random() < 0.5:
                        out.append(LiveRow("pm_trade", tr, (str(tt), up, taker, _c(lvpx), f"{q:g}", "0", f"0x{tt:x}")))
                    else:  # the same trade on the Down token
                        out.append(LiveRow("pm_trade", tr, (str(tt), dn, "SELL" if taker == "BUY" else "BUY",
                                                            _c(100 - lvpx), f"{q:g}", "0", f"0x{tt:x}")))
        if rng.random() < 0.1:
            b, a = book.best()
            ub, ua = bb_ba(b, a)
            sp = "" if b is None or a is None else _c(a - b)
            out.append(LiveRow("pm_best_bid_ask", r + 1, (str(ts), up, ub, ua, sp)))
        if not ticked and ts > ws + 30_000:
            ticked = True
            m = market_obj(coin, tf, ws)
            out.append(LiveRow("pm_other", recv_of(ts), {
                "event_type": "tick_size_change", "asset_id": f"{ws // 1000}1", "market": m["conditionId"],
                "old_tick_size": "0.01", "new_tick_size": "0.001", "timestamp": str(ts)}))
        ts += rng.randint(60, 420)
    return "Up" if p >= 50 else "Down"


def make_session(seed: int, start_ms: int, minutes: int, coins=("btc",), timeframes=("5m",), *, bn_ms: int = 3,
                 resolutions: bool = True, meta_lead_ms: int | None = None, tick_change: bool = True,
                 bn_silence: tuple[int, int] | None = None, cl_every_ms: int = 1000) -> list[LiveRow]:
    """Recorder-shaped rows of `minutes` from `start_ms` (module docstring), sorted by recv_ms (ties keep
    generation order)."""
    rng = random.Random(seed)
    end = start_ms + minutes * 60_000
    out: list[LiveRow] = []
    first = True
    for coin in coins:
        for tf in timeframes:
            L = TF_MS[tf]
            lead = 2 * L + 30_000 if meta_lead_ms is None else meta_lead_ms
            ws = (start_ms // L - 1) * L
            while ws < end:
                we = ws + L
                meta_recv = max(start_ms + rng.randint(1, 500), ws - lead + rng.randint(0, 2000))
                if meta_recv < end:
                    out.append(meta_row(coin, tf, ws, meta_recv))
                    sub = meta_recv + rng.randint(50, 300)
                    tick = first and tick_change and ws >= start_ms
                    first = first and not tick
                    outcome = _market_rows(rng, coin, tf, ws, sub, min(end, we + 2_000), out, tick, True)
                    if resolutions and we <= end + 60_000:
                        out.append(resolution_row(coin, tf, ws, we + rng.randint(20_000, 60_000), outcome))
                ws += L
    for coin in coins:
        px = BASE[coin]
        t = (start_ms // cl_every_ms + 1) * cl_every_ms
        while t < end:
            px *= 1 + rng.gauss(0, 0.0004)
            v = f"{px:.6f}"
            r = t + rng.randint(80, 900)
            out.append(LiveRow("rtds_price", r, ("crypto_prices_chainlink", f"{coin}/usd", str(t), v, v, str(t + 30), "a")))
            if rng.random() < 0.1:  # received again on the other connection
                out.append(LiveRow("rtds_price", r + rng.randint(0, 3000),
                                   ("crypto_prices_chainlink", f"{coin}/usd", str(t), v, v, str(t + 30), "b")))
            if rng.random() < 0.05:
                out.append(LiveRow("rtds_price", r + 5, ("crypto_prices", f"{coin}usdt", str(t), v, v, str(t + 30), "a")))
            t += cl_every_ms
    for coin in coins:
        sym = f"{coin.upper()}USDT"
        px = BASE[coin]
        dec = 2 if px > 100 else 5
        t = start_ms + rng.randint(0, bn_ms)
        uid = 1_000_000
        while t < end:
            if bn_silence is None or not bn_silence[0] <= t < bn_silence[1]:
                for _ in range(2 if rng.random() < 0.1 else 1):  # some rows share a recv_ms
                    px *= 1 + rng.gauss(0, 0.00003)
                    uid += rng.randint(1, 5)
                    half = px * 0.00001
                    out.append(LiveRow("bn_fut_bbo", t, (sym, str(uid), str(t - 2), str(t - 3), f"{px - half:.{dec}f}",
                                                         f"{rng.uniform(0.1, 9):.3f}", f"{px + half:.{dec}f}",
                                                         f"{rng.uniform(0.1, 9):.3f}")))
            t += bn_ms
    return sort_rows(out)


def sort_rows(rows: list[LiveRow]) -> list[LiveRow]:
    """By recv_ms; equal recv_ms keep their order (as received)."""
    return [r for _, _, r in sorted(((r.recv_ms, i, r) for i, r in enumerate(rows)), key=lambda x: (x[0], x[1]))]


def write_store(rows: list[LiveRow], root: Path) -> None:
    """Rows in the recorder's raw layout under `root/raw`: `YYYY-MM-DD/HH/<stream>.<slotStartMs>.{tsv,jsonl}.zst`,
    5-minute slots by recv_ms, TSV header row, JSONL lines `{"recv_ms", "d"}`. Rows keep their order."""
    from test_pipeline import write_jsonl, write_tsv

    groups: dict[tuple[str, int], list[LiveRow]] = {}
    for r in rows:
        groups.setdefault((r.stream, r.recv_ms // 300_000 * 300_000), []).append(r)
    for (stream, slot), rs in groups.items():
        d = datetime.fromtimestamp(slot / 1000, timezone.utc)
        base = Path(root) / "raw" / d.strftime("%Y-%m-%d") / d.strftime("%H")
        if stream in schema.TSV:
            write_tsv(base / f"{stream}.{slot}.tsv.zst", list(schema.TSV[stream]),
                      [[r.recv_ms, *r.values] for r in rs])
        else:
            write_jsonl(base / f"{stream}.{slot}.jsonl.zst", [{"recv_ms": r.recv_ms, "d": r.values} for r in rs])


__all__ = ["make_session", "meta_row", "resolution_row", "slug_of", "sort_rows", "write_store"]

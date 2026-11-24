#!/usr/bin/env python3
"""
Verify recorder output: structure, completeness, latency and cross-source consistency.

Usage (on the droplet):
  python3 verify.py /opt/polytrader/data [--since-minutes 30]

Streams through closed files with the `zstd` CLI in a single pass per stream,
keeping only aggregates in memory (the droplet has 2 GB RAM shared with the
recorder). Standard library only. Exit code 1 if any problem is found.
"""
import argparse
import collections
import glob
import json
import os
import random
import subprocess
import sys
import time

p = argparse.ArgumentParser()
p.add_argument("data_dir")
p.add_argument("--since-minutes", type=int, default=30)
args = p.parse_args()

NOW_MS = time.time() * 1000
# Files of the current 5-minute slot are still open and only partly readable (zstd buffers output,
# more for quiet streams), so only check up to the last closed slot.
UNTIL_MS = (NOW_MS // 300_000) * 300_000
SINCE_MS = UNTIL_MS - args.since_minutes * 60_000
problems = []


def problem(msg):
    problems.append(msg)
    print("  !! " + msg)


def files(stream, since_ms=None):
    """Files whose 5-minute slot overlaps the window. Open files (still being written) read as partial."""
    since_ms = SINCE_MS if since_ms is None else since_ms
    out = []
    for f in glob.glob(os.path.join(args.data_dir, "raw", "*", "*", f"{stream}.*.zst")):
        start_ms = int(os.path.basename(f).split(".")[1])
        if since_ms - 5 * 60_000 <= start_ms < UNTIL_MS:
            out.append((start_ms, f))
    return [f for _, f in sorted(out)]


def lines(stream, since_ms=None):
    for f in files(stream, since_ms):
        proc = subprocess.Popen(["zstd", "-dcq", f], stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True, bufsize=1 << 20)
        for ln in proc.stdout:
            if ln.endswith("\n"):  # skip a partial last line
                yield ln[:-1]
        proc.wait()


def tsv(stream, since_ms=None):
    """Yield (header, row) for rows since since_ms (default: the window); checks column counts."""
    since_ms = SINCE_MS if since_ms is None else since_ms
    header = None
    bad = 0
    for ln in lines(stream, since_ms):
        if ln.startswith("recv_ms\t"):
            h = ln.split("\t")
            if header and h[: len(header)] != header[: len(h)]:
                problem(f"{stream}: incompatible header change between files")
            header = h  # columns may be appended over time (e.g. rtds_price gained `conn`)
            continue
        r = ln.split("\t")
        if header and len(r) != len(header):
            bad += 1
            continue
        if since_ms <= int(r[0]) < UNTIL_MS:
            yield header, r
    if bad:
        problem(f"{stream}: {bad} rows with wrong column count")


def jsonl(stream):
    for ln in lines(stream):
        try:
            j = json.loads(ln)
        except json.JSONDecodeError:
            problem(f"{stream}: invalid JSON line")
            continue
        if SINCE_MS <= j["recv_ms"] < UNTIL_MS:
            yield j


class Reservoir:
    """Fixed-size random sample, for percentiles without storing everything."""

    def __init__(self, k=20000):
        self.k, self.n, self.v = k, 0, []

    def add(self, x):
        self.n += 1
        if len(self.v) < self.k:
            self.v.append(x)
        else:
            j = random.randrange(self.n)
            if j < self.k:
                self.v[j] = x

    def pct(self, q):
        if not self.v:
            return float("nan")
        s = sorted(self.v)
        return s[min(len(s) - 1, int(q * len(s)))]

    def summary(self):
        return f"p50={self.pct(.5):.0f} p99={self.pct(.99):.0f} max={max(self.v) if self.v else float('nan'):.0f}"


class StreamCheck:
    """Per-stream aggregates: rows, span, per-key counts, longest silence per key, recv-event lag."""

    def __init__(self, key_i, evt_i=None):
        self.key_i, self.evt_i = key_i, evt_i
        self.rows = 0
        self.first = self.last = None
        self.per_key = collections.Counter()
        self.last_seen = {}
        self.max_gap = collections.Counter()
        self.lag = Reservoir()
        self.backwards = 0

    def add(self, r):
        t = int(r[0])
        self.rows += 1
        if self.first is None:
            self.first = t
        if self.last is not None and t < self.last:
            self.backwards += 1
        self.last = t
        k = r[self.key_i]
        self.per_key[k] += 1
        if k in self.last_seen:
            g = t - self.last_seen[k]
            if g > self.max_gap[k]:
                self.max_gap[k] = g
        self.last_seen[k] = t
        if self.evt_i is not None and r[self.evt_i]:
            self.lag.add(t - int(r[self.evt_i]))

    def span_min(self):
        return (self.last - self.first) / 60000 if self.rows else 0

    def silent(self, limit_ms):
        return {k: v for k, v in self.max_gap.items() if v > limit_ms}


fmt = lambda ms: time.strftime("%H:%M", time.gmtime(ms / 1000))
print(f"== Recorder verification {fmt(SINCE_MS)}-{fmt(UNTIL_MS)} UTC ({args.since_minutes} min of closed files), data dir {args.data_dir}")

# ================================================================ Binance
print("\n## Binance")
last_mid = {}
crossed = 0
seq_breaks = collections.Counter()
for stream, evt_col, max_gap in [
    ("bn_spot_trade", "event_ms", 120_000),
    ("bn_spot_bbo", None, 60_000),
    ("bn_spot_depth", "event_ms", 5_000),
    ("bn_fut_trade", "event_ms", 120_000),
    ("bn_fut_bbo", "event_ms", 60_000),
    ("bn_fut_depth", "event_ms", 5_000),
    ("bn_fut_mark", "event_ms", 5_000),
    ("bn_fut_liquidation", "event_ms", None),
]:
    chk = None
    seq_last = {}
    for h, r in tsv(stream):
        if chk is None:
            chk = StreamCheck(1, h.index(evt_col) if evt_col else None)
        chk.add(r)
        if stream.endswith("_bbo"):
            # spot: recv, sym, update_id, bid, bid_qty, ask, ask_qty | fut: recv, sym, update_id, event, txn, bid, bid_qty, ask, ask_qty
            bid, ask = (float(r[3]), float(r[5])) if stream == "bn_spot_bbo" else (float(r[5]), float(r[7]))
            if bid > ask:
                crossed += 1
            last_mid[(stream, r[1])] = (bid + ask) / 2
        if stream == "bn_spot_depth":
            s, f, l = r[1], int(r[3]), int(r[4])
            if s in seq_last and not (f <= seq_last[s] + 1 <= l):
                seq_breaks[f"spot:{s}"] += 1
            seq_last[s] = l
        elif stream == "bn_fut_depth":
            s, l, pu = r[1], int(r[5]), int(r[6])
            if s in seq_last and pu != seq_last[s]:
                seq_breaks[f"fut:{s}"] += 1
            seq_last[s] = l
    if chk is None:
        if stream != "bn_fut_liquidation":
            problem(f"{stream}: no rows")
        else:
            print(f"  {stream:20} no rows (liquidations can be quiet)")
        continue
    line = f"  {stream:20} rows={chk.rows:>9,} over {chk.span_min():5.1f} min | symbols={len(chk.per_key)}"
    if evt_col:
        line += f" | lag ms {chk.lag.summary()}"
    print(line)
    if stream != "bn_fut_liquidation" and len(chk.per_key) < 8:
        problem(f"{stream}: only {len(chk.per_key)} symbols: {sorted(chk.per_key)}")
    if max_gap and chk.silent(max_gap):
        problem(f"{stream}: silent > {max_gap/1000:.0f}s: {', '.join(f'{k}={v/1000:.0f}s' for k, v in chk.silent(max_gap).items())}")
    if chk.backwards:
        problem(f"{stream}: recv_ms went backwards {chk.backwards} times")
print(f"  depth sequence breaks (expected only at reconnects): {dict(seq_breaks) or 'none'}")
if sum(seq_breaks.values()) > 16:
    problem(f"{sum(seq_breaks.values())} depth sequence breaks")
if crossed:
    problem(f"{crossed} crossed Binance BBO rows (bid > ask)")
s_mid, f_mid = last_mid.get(("bn_spot_bbo", "BTCUSDT")), last_mid.get(("bn_fut_bbo", "BTCUSDT"))
if s_mid and f_mid:
    print(f"  BTC last mid: spot {s_mid:.2f} / perp {f_mid:.2f} (basis {(f_mid/s_mid-1)*1e4:+.1f} bp)")
    if abs(f_mid / s_mid - 1) > 0.005:
        problem("BTC spot vs perp mid differ by > 0.5%")

snap_keys, min_levels, n_snaps = collections.Counter(), None, 0
for j in jsonl("bn_depth_snapshot"):
    d = j["d"]
    n_snaps += 1
    snap_keys[(d["market"], d["sym"])] += 1
    lv = min(len(d.get("bids", [])), len(d.get("asks", [])))
    min_levels = lv if min_levels is None else min(min_levels, lv)
print(f"  bn_depth_snapshot     {n_snaps} snapshots, {len(snap_keys)} market/symbol pairs, min levels per side {min_levels}")
if len(snap_keys) < 16:
    problem(f"bn_depth_snapshot: only {len(snap_keys)} of 16 market/symbol pairs")
eps = collections.Counter(j["d"]["endpoint"] for j in jsonl("bn_fut_stats"))
print(f"  bn_fut_stats          {dict(eps)}")
if not eps.get("open_interest"):
    problem("bn_fut_stats: no open interest")
n_other = sum(1 for _ in jsonl("bn_other"))
if n_other:
    problem(f"bn_other: {n_other} unrecognised Binance messages")

# ================================================================ RTDS
print("\n## Polymarket RTDS (Chainlink + Binance relay)")
rt_chk = StreamCheck(2)
chainlink = collections.defaultdict(list)  # coin -> [(ts_ms, value)]
rt_counts = collections.Counter()
for h, r in tsv("rtds_price"):
    rt_chk.add(r)
    rt_counts[(r[1], r[2])] += 1
    if r[1] == "crypto_prices_chainlink":
        chainlink[r[2].split("/")[0]].append((int(r[3]), float(r[4])))
for (topic, sym), n in sorted(rt_counts.items()):
    print(f"  {topic:24} {sym:10} {n:>7,} updates")
if not chainlink:
    problem("rtds: no Chainlink prices")
# Chainlink must be continuous: priceToBeat is a 60 s TWAP of it.
for coin, v in chainlink.items():
    ts = sorted(t for t, _ in v)
    g = [(b - a) / 1000 for a, b in zip(ts, ts[1:]) if b - a > 5000]
    if g:
        problem(f"chainlink {coin}: {len(g)} gaps > 5s, total {sum(g):.0f}s, longest {max(g):.0f}s")
for v in chainlink.values():
    v.sort()

# ================================================================ Polymarket CLOB
print("\n## Polymarket CLOB")
meta_series = collections.Counter(j["d"]["series"] for j in jsonl("pm_market_meta"))
print(f"  markets discovered in window: {sum(meta_series.values())} across {len(meta_series)} series")
books = collections.Counter()
first_book = {}
first_update = {}
assets_seen = set()
last_bba = {}
bad_bba = 0
ws_trades = collections.Counter()
for stream in ["pm_book", "pm_price_change", "pm_best_bid_ask", "pm_trade"]:
    chk = StreamCheck(2, 1)
    unmapped = 0
    for h, r in tsv(stream):
        chk.add(r)
        a = r[2]
        if "|" not in a:
            unmapped += 1
        assets_seen.add(a)
        t = int(r[0])
        if stream == "pm_book":
            books[a] += 1
            first_book.setdefault(a, t)
        else:
            first_update[a] = min(first_update.get(a, t), t)
        if stream == "pm_best_bid_ask":
            if r[3] and r[4]:
                b, k = float(r[3]), float(r[4])
                if not (0 <= b < k <= 1):
                    bad_bba += 1
                last_bba[a] = (b, k)
        elif stream == "pm_trade":
            ws_trades[a.split("|")[0]] += 1
    print(f"  {stream:18} rows={chk.rows:>9,} | assets={len(chk.per_key):>4} | lag ms {chk.lag.summary()}")
    if chk.rows == 0:
        problem(f"{stream}: no rows")
    if unmapped:
        problem(f"{stream}: {unmapped} rows with unmapped asset ids")
# Markets are subscribed up to 2 windows before they open, so look back further for their snapshot.
for h, r in tsv("pm_book", SINCE_MS - 60 * 60_000):
    if int(r[0]) < SINCE_MS:
        first_book[r[2]] = min(first_book.get(r[2], int(r[0])), int(r[0]))
# Assets first seen well inside the window were subscribed during it: their first message must be a book snapshot.
newly = [a for a, t in first_update.items() if t > SINCE_MS + 90_000]
no_book = [a for a in newly if first_book.get(a, float("inf")) > first_update[a] + 5_000]
# Also: every asset with updates in the window must have had a snapshot at some point.
never = [a for a in first_update if a not in first_book]
if never:
    problem(f"{len(never)} assets never got a book snapshot, e.g. {never[:3]}")
print(f"  assets subscribed during window: {len(newly)}, without a book snapshot first: {len(no_book)}")
if no_book:
    problem(f"{len(no_book)} newly subscribed assets got updates before any book snapshot, e.g. {no_book[:3]}")
if bad_bba:
    problem(f"pm_best_bid_ask: {bad_bba} rows with bid >= ask or outside [0,1]")
mirror = Reservoir()
for a, (b, k) in last_bba.items():
    slug, side = a.split("|")
    if side == "U" and f"{slug}|D" in last_bba:
        bd, kd = last_bba[f"{slug}|D"]
        if b and kd:
            mirror.add(abs(b + kd - 1) * 100)
print(f"  Up/Down mirror |bid_up + ask_down - 1| in cents: {mirror.summary()} over {mirror.n} markets")
if mirror.n and mirror.pct(.99) > 1:
    problem("Up and Down books do not mirror each other")
series_with_data = {a.split("|")[0].rsplit("-", 1)[0] for a in assets_seen}
print(f"  slug prefixes with data: {len(series_with_data)}")
for s in ["pm_new_market", "pm_other"]:
    kinds = collections.Counter(j["d"].get("event_type") for j in jsonl(s))
    print(f"  {s}: {sum(kinds.values())} {dict(kinds)}")

# ================================================================ Resolutions
print("\n## Resolutions")
res = list(jsonl("pm_resolution"))
api_by = collections.Counter(j["d"]["slug"] for j in jsonl("pm_api_trades"))
print(f"  resolved markets: {len(res)} | API trades: {sum(api_by.values())} across {len(api_by)} markets")
print(f"  outcomes: {dict(collections.Counter(r['d']['outcome'] for r in res))}")
missing_ptb = [r["d"]["slug"] for r in res if not (r["d"]["event"].get("eventMetadata") or {}).get("priceToBeat")]
if missing_ptb:
    problem(f"{len(missing_ptb)} resolved markets without priceToBeat, e.g. {missing_ptb[:3]}")
diffs = Reservoir()
examples = []
for r in res:
    ev = r["d"]["event"]
    ptb = (ev.get("eventMetadata") or {}).get("priceToBeat")
    m = ev["markets"][0]
    coin = (m.get("cryptoMarketConfig") or {}).get("asset") or r["d"]["slug"].split("-")[0]
    start = int(time.mktime(time.strptime(m["eventStartTime"], "%Y-%m-%dT%H:%M:%SZ")) - time.timezone) * 1000
    series = chainlink.get(coin, [])
    win = [v for t, v in series if start - 60_000 <= t < start]
    if ptb and series and series[0][0] <= start - 60_000 and win:
        twap = sum(win) / len(win)
        d = (twap / ptb - 1) * 1e4
        diffs.add(abs(d) * 100)  # hundredths of a bp, so the reservoir summary keeps precision
        if len(examples) < 3:
            examples.append(f"    {r['d']['slug']:30} priceToBeat={ptb:.5f} chainlink 60s TWAP={twap:.5f} diff={d:+.3f}bp")
if diffs.n:
    print(f"  60s Chainlink TWAP before window start vs priceToBeat, |diff| in 0.01 bp: {diffs.summary()} over {diffs.n} markets")
    print("\n".join(examples))
    if diffs.pct(.99) > 200:
        problem("priceToBeat no longer matches the 60s Chainlink TWAP (|diff| > 2 bp)")
# Trade completeness for markets recorded from before their window opened until after it closed.
DUR = {"5m": 300, "15m": 900, "1h": 3600, "4h": 14400, "1d": 86400}
full = []
for slug, n in api_by.items():
    parts = slug.split("-")
    start_s, dur = int(parts[-1]), DUR.get(parts[-2])
    if dur and start_s * 1000 > SINCE_MS + 60_000:
        full.append((slug, n, ws_trades.get(slug, 0)))
for slug, n, w in full[:6]:
    print(f"  {slug:30} API trades={n:>5}  WS trade events={w:>5}")
missing = [f for f in full if f[1] > 0 and f[2] == 0]
if missing:
    problem(f"{len(missing)} fully recorded markets have API trades but no WebSocket trades, e.g. {missing[:2]}")

# ================================================================ Disk
print("\n## Disk (whole data dir)")
tot = collections.Counter()
oldest = None
for f in glob.glob(os.path.join(args.data_dir, "raw", "*", "*", "*.zst")):
    tot[os.path.basename(f).split(".")[0]] += os.path.getsize(f)
    st_ms = int(os.path.basename(f).split(".")[1])
    oldest = st_ms if oldest is None else min(oldest, st_ms)
total = sum(tot.values())
hours = max((NOW_MS - oldest) / 3_600_000, 1e-9) if oldest else 0
for k, v in tot.most_common(8):
    print(f"  {k:22} {v/1e6:8.1f} MB")
st = os.statvfs(args.data_dir)
free = st.f_bavail * st.f_frsize
rate = total / hours * 24 if hours else 0
print(f"  total {total/1e9:.2f} GB over {hours:.2f} h => ~{rate/1e9:.1f} GB/day; {free/1e9:.1f} GB free => ~{free/rate:.1f} days left" if rate else "")

print(f"\n== {len(problems)} problem(s)")
for p_ in problems:
    print("  - " + p_)
sys.exit(1 if problems else 0)

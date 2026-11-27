"""Screen for docs/strategies/open-gap.md: is the book mispriced in the first seconds of a window?

    cd analysis && POLYDATA_DIR=... uv run python ../research/open_gap/screen.py 2026-11-24 [--out OUT.csv]

Per 5m/15m window: K = mean of Chainlink ticks with ts in the 60 s before window start; revealed at the recv
time of the first tick with ts >= window start (the sim's rule). At t = reveal + offset the fair P(Up) comes from
the calibration model (tau > 60 branch of bt/fairvalue.py, S nowcast with Binance) and the books are read as of t.
A taker order decided at t lands at t + latency, is held 250 ms, then hits the book of that time.
Writes one row per (window, offset, latency) to a CSV and prints the summary tables.
"""
import sys, math, datetime as dt
import numpy as np, pandas as pd
from polydata.views import connect

OFFS = [0, 2, 5, 10, 20, 30]
LATS = [200, 50]
HOLD = 250
MARGINS = [0.0, 0.02, 0.04, 0.06]
MAX_SPREAD = 0.03
START_MS = int(dt.datetime(2026, 11, 24, 10, 36, tzinfo=dt.timezone.utc).timestamp() * 1000)
K_VOL = 1.2


def build(day: str) -> pd.DataFrame:
    con = connect()
    con.execute("SET preserve_insertion_order = false")
    con.execute(f"""create temp table mk as select slug,coin,timeframe,window_start_ms,window_end_ms,
        (outcome='Up')::int up, fee_rate, fee_exponent, price_to_beat official_k, upper(coin)||'USDT' sym
        from markets where outcome is not null and timeframe in ('5m','15m')
        and strftime(window_start,'%Y-%m-%d')='{day}' and window_start_ms >= {START_MS} + 60000""")
    lo, hi = con.execute("select min(window_start_ms)-3700000, max(window_end_ms)+120000 from mk").fetchone()
    con.execute(f"""create temp table cl as select coin, ts_ms, min(recv_ms) recv_ms, any_value(price) price
        from chainlink where ts_ms between {lo} and {hi} group by all""")
    con.execute("""create temp table k as select mk.slug, avg(c.price) K, count(*) n_k from mk join cl c
        on c.coin=mk.coin and c.ts_ms>=mk.window_start_ms-60000 and c.ts_ms<mk.window_start_ms group by 1""")
    con.execute("""create temp table rev as select mk.slug, min(c.recv_ms) rev_ms from mk join cl c
        on c.coin=mk.coin and c.ts_ms>=mk.window_start_ms and c.recv_ms < mk.window_end_ms group by 1""")
    con.execute(f"""create temp table bsec as select sym, recv_ms//1000 sec, arg_max((bid+ask)/2, recv_ms) mid
        from bn_fut_bbo where recv_ms between {lo} and {hi} and sym in (select distinct sym from mk) group by all""")
    con.execute(f"""create temp table bg as with g as (select s.sym, sec from (select distinct sym from mk) s,
        range({lo//1000},{hi//1000}) t(sec)), f as (select g.sym,g.sec,b.mid from g asof left join bsec b
        on g.sym=b.sym and g.sec>=b.sec)
        select sym,sec,mid, ln(mid/lag(mid) over (partition by sym order by sec)) r from f""")
    con.execute("""create temp table bv as select sym,sec,mid,
        sqrt(avg(r*r) over (partition by sym order by sec rows between 899 preceding and current row)) sig15 from bg""")
    con.execute(f"""create temp table pm as select slug,outcome,recv_ms,best_bid,best_ask from pm_best_bid_ask
        where slug in (select slug from mk) and recv_ms between {lo} and {hi}""")
    offs = ",".join(map(str, OFFS)); lats = ",".join(map(str, LATS))
    con.execute(f"""create temp table g as select mk.*, k.K, k.n_k, rev.rev_ms, o off, l lat,
        rev.rev_ms + o*1000 t, rev.rev_ms + o*1000 + l + {HOLD} a
        from mk join k using(slug) join rev using(slug), (select unnest([{offs}]) o), (select unnest([{lats}]) l)
        where k.n_k >= 20""")
    con.execute("""create temp table snap as select g.*,
        u0.best_bid ub, u0.best_ask ua, d0.best_bid db, d0.best_ask da,
        u1.best_bid ub_a, u1.best_ask ua_a, d1.best_bid db_a, d1.best_ask da_a,
        u2.best_bid ub_m, u2.best_ask ua_m, d2.best_bid db_m, d2.best_ask da_m,
        c.price cl_px, c.ts_ms cl_ts, v.mid bn_mid, v.sig15
        from g
        asof left join (select * from pm where outcome='Up') u0 on g.slug=u0.slug and g.t>=u0.recv_ms
        asof left join (select * from pm where outcome='Down') d0 on g.slug=d0.slug and g.t>=d0.recv_ms
        asof left join (select * from pm where outcome='Up') u1 on g.slug=u1.slug and g.a>=u1.recv_ms
        asof left join (select * from pm where outcome='Down') d1 on g.slug=d1.slug and g.a>=d1.recv_ms
        asof left join (select * from pm where outcome='Up') u2 on g.slug=u2.slug and g.a+60000>=u2.recv_ms
        asof left join (select * from pm where outcome='Down') d2 on g.slug=d2.slug and g.a+60000>=d2.recv_ms
        asof left join cl c on g.coin=c.coin and g.t>=c.recv_ms
        asof left join bv v on g.sym=v.sym and g.t//1000-1>=v.sec""")
    con.execute("""create temp table snap2 as select s.*, b.mid bn_at_cl from snap s
        asof left join bg b on s.sym=b.sym and s.cl_ts//1000>=b.sec""")
    return con.execute("select * from snap2").df()


def fair_up(df: pd.DataFrame) -> np.ndarray:
    S = df.cl_px * (df.bn_mid / df.bn_at_cl)
    S = S.where(S.notna() & np.isfinite(S), df.cl_px)
    sig = K_VOL * df.sig15
    tau = (df.window_end_ms - df.t) / 1000.0
    z = np.log(S / df.K) / (sig * np.sqrt(tau - 40))
    return np.clip(0.5 * np.vectorize(math.erfc)(-z / math.sqrt(2)), 1e-4, 1 - 1e-4)


def derive(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna(subset=["ub", "ua", "db", "da", "cl_px", "sig15", "K"]).copy()
    df["fair"] = fair_up(df)
    df["tight"] = ((df.ua - df.ub) <= MAX_SPREAD) & ((df.da - df.db) <= MAX_SPREAD)
    df["upmid"] = (df.ub + df.ua) / 2
    up = df.fair >= 0.5
    df["side_up"] = up
    df["f"] = np.where(up, df.fair, 1 - df.fair)
    df["ask0"] = np.where(up, df.ua, df.da)
    df["ask_a"] = np.where(up, df.ua_a, df.da_a)
    df["mid_m"] = np.where(up, (df.ub_m + df.ua_m) / 2, (df.db_m + df.da_m) / 2)
    df["win"] = np.where(up, df.up, 1 - df.up)
    fee = lambda p: df.fee_rate * (p * (1 - p)) ** df.fee_exponent
    df["fee0"] = fee(df.ask0)
    df["fee_a"] = fee(df.ask_a)
    df["gap0"] = df.f - df.ask0 - df.fee0            # model edge at decision
    df["pnl_settle"] = df.win - df.ask_a - df.fee_a   # executed at the arrival ask, held to settlement
    df["pnl_mark"] = df.mid_m - df.ask_a - df.fee_a   # same, marked at the mid 60 s later
    df["pnl_fair"] = df.f - df.ask_a - df.fee_a
    df["block"] = df.window_start_ms // 900000
    df["absdev"] = (df.fair - 0.5).abs()
    return df


def boot(x: pd.DataFrame, col: str, n=2000, seed=0):
    b = x.groupby("block")[col].agg(["sum", "count"])
    if len(b) < 2:
        return (np.nan, np.nan)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(b), (n, len(b)))
    m = b["sum"].to_numpy()[idx].sum(1) / b["count"].to_numpy()[idx].sum(1)
    return tuple(np.percentile(m, [2.5, 97.5]))


def summarise(df: pd.DataFrame) -> None:
    pd.set_option("display.width", 220)
    base = df[df.tight & (df.lat == 200)]
    print(f"windows with K and books: {base[base.off == 0].slug.nunique()}")
    print("== books vs model at the open (tight books, 5m+15m, all coins) ==")
    r = base.groupby("off").agg(n=("fair", "size"), absdev=("absdev", "mean"),
                                mid_dev=("upmid", lambda s: (s - 0.5).abs().mean()),
                                gap0_c=("gap0", lambda s: s.mean() * 100)).round(3)
    print(r.to_string())
    b = base.assign(bucket=pd.cut(base.absdev, [0, .05, .1, .2, .3, .5]))
    print("-- |fair-0.5| buckets at off=0: n, mean fair-ask-fee (c), mean ask0")
    print(b[b.off == 0].groupby("bucket", observed=True).agg(
        n=("f", "size"), gap_c=("gap0", lambda s: s.mean() * 100), ask=("ask0", "mean")).round(3).to_string())
    print("== executable taker: n, settle c/sh [95% CI, 15-min block bootstrap], mark60 c/sh, model edge c/sh ==")
    rows = []
    for lat in LATS:
        for off in OFFS:
            for m in MARGINS:
                x = df[(df.lat == lat) & (df.off == off) & df.tight & (df.gap0 > m)].dropna(subset=["ask_a", "mid_m"])
                if len(x) < 5:
                    continue
                lo, hi = boot(x, "pnl_settle")
                rows.append((lat, off, m, len(x), x.pnl_settle.mean() * 100, lo * 100, hi * 100,
                             x.pnl_mark.mean() * 100, x.pnl_fair.mean() * 100))
    out = pd.DataFrame(rows, columns=["lat", "off", "margin", "n", "settle_c", "lo", "hi", "mark60_c", "fair_edge_c"])
    print(out.round(2).to_string(index=False))


if __name__ == "__main__":
    day = sys.argv[1]
    out = sys.argv[sys.argv.index("--out") + 1] if "--out" in sys.argv else None
    d = derive(build(day))
    if out:
        d.to_csv(out, index=False)
    summarise(d)

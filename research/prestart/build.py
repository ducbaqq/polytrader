"""Pre-start screen (docs/strategies/prestart.md): build one row per (5m/15m window, minutes-before-start, latency).

    cd analysis && POLYDATA_DIR=... nice -n 15 uv run python ../research/prestart/build.py 2026-11-24 OUT.csv

At t = window_start - tmin: books are the last pm_best_bid_ask with recv_ms <= t (no lookahead). A taker order
decided at t arrives at t+lat, is held 250 ms, hits the ask of that time. A resting bid placed at t arrives at
t+lat and is live until the window start; it counts as filled only if a recorded SELL trade strictly below its price
occurs after arrival (trade-through; the pessimistic rule). Fair price: 0.5, and a tilted fair that uses the part of the
strike TWAP already known at t plus the current price (see fair_tilt). Depth is the size at the best price in the last
pm_book snapshot at or before t, only when that snapshot's best price equals the quote.
"""
import sys, math, datetime as dt
import numpy as np, pandas as pd
from polydata.views import connect

TMINS = [8, 5, 3, 2, 1, 0.5]
LATS = [200, 50]
HOLD = 250
BIDS = [0.48, 0.49, 0.50]
START_MS = int(dt.datetime(2026, 11, 24, 10, 36, tzinfo=dt.timezone.utc).timestamp() * 1000)
K_VOL = 1.2


def build(day: str) -> pd.DataFrame:
    con = connect()
    con.execute("SET preserve_insertion_order = false")
    lo_clip = f"and window_start_ms >= {START_MS + 600000}" if day == "2026-11-24" else ""
    con.execute(f"""create temp table mk as select slug,coin,timeframe,window_start_ms s,window_end_ms e,
        (outcome='Up')::int up, fee_rate, fee_exponent, upper(coin)||'USDT' sym
        from markets where outcome is not null and timeframe in ('5m','15m')
        and strftime(window_start,'%Y-%m-%d')='{day}' {lo_clip}""")
    lo, hi = con.execute("select min(s)-3700000, max(e)+120000 from mk").fetchone()
    con.execute(f"""create temp table cl as select coin, ts_ms, min(recv_ms) recv_ms, any_value(price) price
        from chainlink where ts_ms between {lo} and {hi} group by all""")
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
    con.execute(f"""create temp table bk as select slug, case when asset like '%|D' then 'Down' else 'Up' end outcome, recv_ms,
        try_cast(split_part(str_split(bids,'|')[-1],':',1) as double) bp, try_cast(split_part(str_split(bids,'|')[-1],':',2) as double) bsz,
        try_cast(split_part(str_split(asks,'|')[-1],':',1) as double) ap, try_cast(split_part(str_split(asks,'|')[-1],':',2) as double) asz
        from pm_book where slug in (select slug from mk) and recv_ms between {lo} and {hi}""")
    tm = ",".join(map(str, TMINS)); lt = ",".join(map(str, LATS))
    con.execute(f"""create temp table g as select mk.*, tm, l lat, s - cast(tm*60000 as bigint) t,
        s - cast(tm*60000 as bigint) + l a, s - cast(tm*60000 as bigint) + l + {HOLD} h
        from mk, (select unnest([{tm}]) tm), (select unnest([{lt}]) l)""")
    con.execute("""create temp table snap as select g.*,
        u0.best_bid ub, u0.best_ask ua, d0.best_bid db, d0.best_ask da,
        u1.best_ask ua_a, d1.best_ask da_a, u1.best_bid ub_a, d1.best_bid db_a,
        u2.best_ask ua_h, d2.best_ask da_h,
        ku.asz u_asz, ku.ap u_ap, ku.bsz u_bsz, ku.bp u_bp, kd.asz d_asz, kd.ap d_ap, kd.bsz d_bsz, kd.bp d_bp,
        c.price cl_px, c.ts_ms cl_ts, v.mid bn_mid, v.sig15
        from g
        asof left join (select * from pm where outcome='Up') u0 on g.slug=u0.slug and g.t>=u0.recv_ms
        asof left join (select * from pm where outcome='Down') d0 on g.slug=d0.slug and g.t>=d0.recv_ms
        asof left join (select * from pm where outcome='Up') u1 on g.slug=u1.slug and g.a>=u1.recv_ms
        asof left join (select * from pm where outcome='Down') d1 on g.slug=d1.slug and g.a>=d1.recv_ms
        asof left join (select * from pm where outcome='Up') u2 on g.slug=u2.slug and g.h>=u2.recv_ms
        asof left join (select * from pm where outcome='Down') d2 on g.slug=d2.slug and g.h>=d2.recv_ms
        asof left join (select * from bk where outcome='Up') ku on g.slug=ku.slug and g.t>=ku.recv_ms
        asof left join (select * from bk where outcome='Down') kd on g.slug=kd.slug and g.t>=kd.recv_ms
        asof left join cl c on g.coin=c.coin and g.t>=c.recv_ms
        asof left join bv v on g.sym=v.sym and g.t//1000-1>=v.sec""")
    con.execute("""create temp table snap2 as select s.*, b.mid bn_at_cl from snap s
        asof left join bg b on s.sym=b.sym and s.cl_ts//1000>=b.sec""")
    # strike ticks already seen at t (ts in the minute before the start, received by t)
    con.execute("""create temp table kn as select g.slug, g.tm, g.lat, sum(c.price) ksum, count(*) kn
        from g join cl c on c.coin=g.coin and c.ts_ms>=g.s-60000 and c.ts_ms<g.s and c.recv_ms<=g.t group by all""")
    # maker fills: first SELL trade strictly below the bid price after arrival, up to the window start
    cases = ",".join(f"min(tr.recv_ms) filter (where tr.price < {b}) fill_{int(round(b*100))}" for b in BIDS)
    con.execute(f"""create temp table tr as select slug, outcome, recv_ms, price from pm_trade
        where side='SELL' and slug in (select slug from mk) and recv_ms between {lo} and {hi}""")
    for o in ("Up", "Down"):
        con.execute(f"""create temp table fl_{o} as select g.slug, g.tm, g.lat, {cases}
            from g join tr on tr.slug=g.slug and tr.outcome='{o}' and tr.recv_ms > g.a and tr.recv_ms <= g.s group by all""")
    df = con.execute("""select s.*, kn.ksum, kn.kn,
        fu.fill_48 uf48, fu.fill_49 uf49, fu.fill_50 uf50, fd.fill_48 df48, fd.fill_49 df49, fd.fill_50 df50
        from snap2 s left join kn using(slug,tm,lat) left join fl_Up fu using(slug,tm,lat)
        left join fl_Down fd using(slug,tm,lat)""").df()
    return df


def fair_tilt(df: pd.DataFrame) -> np.ndarray:
    """P(Up) given the current price and the part of the strike TWAP already seen (K unknown before T-60 s).
    K_est = (sum of ticks seen + S * unseen share) / 60 ticks; sd of ln(S_end/K_est) ~ sig * sqrt(W + (T - t))."""
    S = df.cl_px * (df.bn_mid / df.bn_at_cl)
    S = S.where(S.notna() & np.isfinite(S), df.cl_px)
    kn = df.kn.fillna(0); ks = df.ksum.fillna(0)
    nk = np.minimum(kn, 60)
    K = (ks + S * (60 - nk)) / 60.0
    W = (df.e - df.s) / 1000.0
    pre = df.tm * 60.0
    sig = K_VOL * df.sig15
    z = np.log(S / K) / (sig * np.sqrt(W + pre))
    return np.clip(0.5 * np.vectorize(math.erfc)(-z / math.sqrt(2)), 1e-4, 1 - 1e-4)


def derive(df: pd.DataFrame) -> pd.DataFrame:
    df = df.dropna(subset=["ub", "ua", "db", "da", "ua_a", "da_a"]).copy()
    df["fair"] = fair_tilt(df)
    df["fair"] = df.fair.fillna(0.5)
    fee = lambda p: df.fee_rate * (p * (1 - p)) ** df.fee_exponent
    df["block"] = df.s // 900000
    df["upmid"] = (df.ub + df.ua) / 2
    for side, ask, bid, ask_a, bid_a in (("u", "ua", "ub", "ua_a", "ub_a"), ("d", "da", "db", "da_a", "db_a")):
        win = df.up if side == "u" else 1 - df.up
        fair = df.fair if side == "u" else 1 - df.fair
        df[f"{side}_win"] = win
        df[f"{side}_fair"] = fair
        df[f"{side}_spread"] = df[ask] - df[bid]
        a = df[ask_a]
        df[f"{side}_tk_pnl"] = win - a - fee(a)          # taker at arrival ask, to settlement
        df[f"{side}_tk_fairedge"] = fair - a - fee(a)    # model edge
        for b in (48, 49, 50):
            p = b / 100
            f = df[f"{side}f{b}"]
            df[f"{side}_placed{b}"] = df[ask_a] > p + 1e-9   # does not cross on arrival
            df[f"{side}_filled{b}"] = df[f"{side}_placed{b}"] & f.notna()
            df[f"{side}_mk_pnl{b}"] = win - p                 # makers pay no fee
            df[f"{side}_mk_fairedge{b}"] = fair - p
    return df


def boot(x: pd.DataFrame, col: str, n=2000, seed=0):
    b = x.groupby("block")[col].agg(["sum", "count"])
    if len(b) < 2:
        return (np.nan, np.nan)
    rng = np.random.default_rng(seed)
    idx = rng.integers(0, len(b), (n, len(b)))
    m = b["sum"].to_numpy()[idx].sum(1) / b["count"].to_numpy()[idx].sum(1)
    return tuple(np.percentile(m, [2.5, 97.5]))


if __name__ == "__main__":
    d = derive(build(sys.argv[1]))
    d.to_csv(sys.argv[2], index=False)
    print(len(d), "rows", d.slug.nunique(), "markets")

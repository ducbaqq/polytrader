"""Backtest of the streak ladder (docs/strategies/streak-ladder.md). No free parameters.

    cd analysis && POLYDATA_DIR=... uv run python ../research/streak_ladder/streak_ladder.py A 2026-11-24 primary [bets.csv]
    cd analysis && POLYDATA_DIR=... uv run python ../research/streak_ladder/streak_ladder.py B secondary [bets.csv]

A <day>: books-based (entry = best ask of the opposite token at decision time + 200 ms latency (+250 ms hold on
5m/15m/1h), walked through the last recorded snapshot depth, taker fee included). B: every non-sealed resolved
market, entry price 0.50 + fee, nominal.

primary: the bot learns each outcome from Chainlink (paper/settle.py rule: Up iff mean of first-received ticks with
ts in [end-60 s, end) >= mean of the ticks in [start-60 s, start) received by the first tick with ts >= start),
at the recv time of the first tick with ts >= end; the window starting at that boundary is bet at its open + 1 s
(or the knowledge time, if later). secondary: waits for the official Polymarket resolution recv.
P&L always settles on the official outcome. The views hide sealed days (2026-11-25, 2026-11-28); this script never
sets include_sealed (a window ending at midnight before a sealed day has no inferable outcome there and falls back
to the official resolution).
"""
import sys, json, pathlib
import pandas as pd
from polydata.views import connect

DUR = {"5m": 300_000, "15m": 900_000, "1h": 3_600_000, "4h": 14_400_000, "1d": 86_400_000}
HOLD_TF = ("5m", "15m", "1h")
LAT, HOLD, DELAY = 200, 250, 1000
N_STREAK, STEP = 3, 2.0
BOOK_START = {"2026-11-24": "2026-11-24 10:36:00+00"}  # first replayable hour


def fps(p, rate, exp):
    return rate * (p * (1 - p)) ** exp


def parse(s):
    out = []
    for lv in s.split("|"):
        a, b = lv.split(":")
        out.append((float(a), float(b)))
    return sorted(out)


def walk(asks, best_ask, budget, rate, exp):
    """Spend up to `budget` (fee included) buying from `asks` (price, size). Returns (spent, shares) or None."""
    lv = [(p, s) for p, s in asks if p >= best_ask - 1e-9]
    if not lv:
        return None
    if lv[0][0] > best_ask + 1e-9:  # snapshot older than the best ask: assume the top size
        lv.insert(0, (best_ask, lv[0][1]))
    spent = shares = 0.0
    for p, s in lv:
        c = p + fps(p, rate, exp)
        take = min(s, (budget - spent) / c)
        if take <= 0:
            break
        spent += take * c
        shares += take
        if budget - spent < 1e-9:
            break
    return (spent, shares) if shares > 0 else None


def run_series(ws, entry_fn, primary):
    """ws: windows of one series sorted by start. The bot's view of a window is `bout` known at `known`; P&L settles
    on the official `outcome`. SECONDARY: a window is enterable once start > known. PRIMARY: the window starting at
    the boundary is enterable (decision at dec_ms < window end)."""
    ladders, bets, skips = [], [], {"unresolved": 0, "no_fill": 0, "waited": 0}
    n = len(ws)
    last_with_outcome = max((i for i, w in enumerate(ws) if w["outcome"]), default=-1)
    i = 0
    run_dir, run_len, prev = None, 0, None
    while i < n:
        w = ws[i]
        if not w["bout"]:
            run_dir, run_len, prev = None, 0, None
            i += 1
            continue
        if prev is not None and w["start"] - prev["start"] == w["dur"] and w["bout"] == run_dir:
            run_len += 1
        else:
            run_dir, run_len = w["bout"], 1
        prev = w
        if run_len < N_STREAK:
            i += 1
            continue
        opp = "Down" if run_dir == "Up" else "Up"
        true_streak = all(ws[j]["outcome"] == run_dir for j in range(i - N_STREAK + 1, i + 1))
        lad = dict(series=w["series"], trigger=w["slug"], dir=run_dir, bets=0, max_stake=0.0, pnl=0.0,
                   status="open", losses=0, false_trigger=not true_streak, misread_bets=0)
        ready, stake, k, ended_win = w["known"], STEP, i + 1, False
        while k < n:
            wk = ws[k]
            if (wk["dec_ms"] >= wk["end"]) if primary else (wk["start"] <= ready):
                skips["waited"] += 1
                k += 1
                continue
            if not wk["outcome"]:
                if k <= last_with_outcome:
                    skips["unresolved"] += 1
                    k += 1
                    continue
                break  # unresolved at the end of the data: ladder stays open
            ent = entry_fn(wk, opp, stake)
            if ent is None:
                skips["no_fill"] += 1
                k += 1
                continue
            spent, shares, ask, t_in, spread = ent
            real_win = wk["outcome"] == opp
            bot_win = wk["bout"] == opp
            pnl = (shares - spent) if real_win else -spent
            bets.append(dict(series=w["series"], slug=wk["slug"], n=lad["bets"] + 1, stake=stake, spent=spent,
                             ask=ask, price_paid=spent / shares, win=real_win, pnl=pnl, t_in=t_in, t_out=wk["res_ms"],
                             spread=spread, opp=opp, misread=real_win != bot_win))
            lad["bets"] += 1
            lad["misread_bets"] += real_win != bot_win
            lad["max_stake"] = max(lad["max_stake"], stake)
            lad["pnl"] += pnl
            ready = wk["known"]
            if bot_win:
                lad["status"] = "won" if real_win else "stopped_on_misread_loss"
                ended_win = True
                break
            lad["losses"] += 1
            stake += STEP
            k += 1
        ladders.append(lad)
        if ended_win:
            run_dir, run_len, prev = None, 0, None  # fresh streak from windows after the winning one
            i = k + 1
        else:
            break
    return ladders, bets, skips


def exposure_peak(bets):
    ev = []
    for b in bets:
        ev.append((b["t_in"], b["spent"]))
        ev.append((b["t_out"], -b["spent"]))
    ev.sort(key=lambda x: (x[0], x[1]))  # releases before opens at equal times
    cur = peak = 0.0
    when = None
    for t, d in ev:
        cur += d
        if cur > peak:
            peak, when = cur, t
    return peak, when


def drawdown(bets):
    cum = peak = dd = 0.0
    for b in sorted(bets, key=lambda b: b["t_out"]):
        cum += b["pnl"]
        peak = max(peak, cum)
        dd = max(dd, peak - cum)
    return dd


def summarize(label, ladders, bets):
    df, lad = pd.DataFrame(bets), pd.DataFrame(ladders)
    if df.empty:
        return dict(series=label, triggers=len(lad), bets=0)
    first = df[df.n == 1]
    return dict(series=label, triggers=len(lad), completed=int((lad.status != "open").sum()),
                open=int((lad.status == "open").sum()), false_triggers=int(lad.false_trigger.sum()),
                misread_bets=int(df.misread.sum()), stopped_on_misread=int((lad.status == "stopped_on_misread_loss").sum()),
                bets=len(df), first_bets=len(first),
                first_win=round(first.win.mean(), 4), first_price=round(first.price_paid.mean(), 4),
                all_win=round(df.win.mean(), 4), all_price=round(df.price_paid.mean(), 4),
                longest_loss_run=int(lad.losses.max()), max_stake=float(lad.max_stake.max()),
                staked=round(df.spent.sum(), 2), pnl=round(df.pnl.sum(), 2),
                roi_staked=round(df.pnl.sum() / df.spent.sum(), 4), max_dd=round(drawdown(bets), 2),
                worst_ladder=round(lad.pnl.min(), 2))


def report(tag, all_ladders, all_bets, by_series, skips):
    rows = [summarize(s, l, b) for s, (l, b) in sorted(by_series.items())]
    tot = summarize("TOTAL", all_ladders, all_bets)
    pk, when = exposure_peak(all_bets)
    lad = pd.DataFrame(all_ladders)
    reach = {f">=${x}": [int((lad.max_stake >= x).sum()), round(float((lad.max_stake >= x).mean()), 4),
                         round(0.5 ** (x / STEP - 1), 4)] for x in (6, 10, 20, 30, 50)}
    out = dict(tag=tag, total=tot, series=rows, peak_exposure=round(pk, 2), peak_exposure_ms=when,
               reach_stake_count_frac_fair_coin=reach, skips=skips)
    print(json.dumps(out, indent=1, default=str))
    return out


def load_windows(con, where):
    return con.execute(f"""select slug, coin, timeframe tf, window_start_ms start, window_end_ms "end", outcome,
        epoch_ms(resolution_seen_at) res_ms, fee_rate rate, coalesce(fee_exponent,1) exp, up_token_id, down_token_id
        from markets where {where} order by coin, timeframe, window_start_ms""").df()


def add_inferred(con, df):
    """Chainlink-inferred outcome (paper/settle.py rule) and its receipt time, from the (non-sealed) chainlink view."""
    con.register("wi", df[["slug", "coin", "start", "end"]])
    con.execute("create or replace temp table cl as select coin, ts_ms, min(recv_ms) recv_ms, any_value(price) price "
                "from chainlink group by 1, 2")
    q = con.execute("""
        with e as (select wi.slug, wi.coin, wi.start, wi."end", min(c.recv_ms) r_end from wi join cl c
                   on c.coin = wi.coin and c.ts_ms >= wi."end" and c.ts_ms < wi."end" + 120000 group by all),
        s as (select wi.slug, min(c.recv_ms) r_start from wi join cl c
                   on c.coin = wi.coin and c.ts_ms >= wi.start and c.ts_ms < wi.start + 120000 group by all),
        tw as (select e.slug, avg(c.price) close_twap, count(*) n_c from e join cl c on c.coin = e.coin
                   and c.ts_ms >= e."end" - 60000 and c.ts_ms < e."end" and c.recv_ms <= e.r_end group by all),
        pt as (select wi.slug, avg(c.price) ptb, count(*) n_p from wi join s on s.slug = wi.slug join cl c
                   on c.coin = wi.coin and c.ts_ms >= wi.start - 60000 and c.ts_ms < wi.start and c.recv_ms <= s.r_start
                   group by all)
        select e.slug, e.r_end inf_ms, tw.close_twap, pt.ptb from e join tw using (slug) join pt using (slug)
        where tw.n_c >= 20 and pt.n_p >= 20""").df()
    q["inf"] = (q.close_twap >= q.ptb).map({True: "Up", False: "Down"})
    return df.merge(q[["slug", "inf", "inf_ms", "close_twap", "ptb"]], on="slug", how="left")


def series_of(df, primary):
    out = {}
    for (coin, tf), g in df.groupby(["coin", "tf"]):
        ws = []
        for r in g.itertuples():
            off = r.outcome if isinstance(r.outcome, str) else None
            inf = r.inf if isinstance(r.inf, str) else None
            res = int(r.res_ms) if pd.notna(r.res_ms) else 0
            if primary and inf:
                bout, known = inf, int(r.inf_ms)
            else:
                bout, known = off, res
            ws.append(dict(slug=r.slug, series=f"{coin}-{tf}", start=int(r.start), dur=DUR[tf], end=int(r.end),
                           outcome=off, inf=inf, bout=bout, known=known, res_ms=res, rate=float(r.rate),
                           exp=float(r.exp), tf=tf))
        for j, w in enumerate(ws):  # entry decision: not before the previous window's outcome is known
            p = ws[j - 1] if j else None
            kn = p["known"] if p and p["end"] == w["start"] else 0
            w["dec_ms"] = max(w["start"] + DELAY, kn) if primary else w["start"] + DELAY
        out[f"{coin}-{tf}"] = ws
    return out


def run_all(df, entry, primary):
    by, AL, AB = {}, [], []
    skips = {"unresolved": 0, "no_fill": 0, "waited": 0}
    for s, ws in series_of(df, primary).items():
        l, b, sk = run_series(ws, entry, primary)
        by[s] = (l, b)
        AL += l
        AB += b
        for k in skips:
            skips[k] += sk[k]
    return AL, AB, by, skips


def inference_report(df):
    d = df[df.outcome.notna()]
    have = d[d.inf.notna()]
    out = dict(resolved=len(d), inferred=len(have), differs=int((have.inf != have.outcome).sum()),
               lag_ms_median=float((have.inf_ms - have["end"]).median()) if len(have) else None,
               by_tf={tf: [int(len(g)), int((g.inf != g.outcome).sum())] for tf, g in have.groupby("tf")})
    print("inference", json.dumps(out), file=sys.stderr)


def mode_b(primary):
    con = connect()
    df = add_inferred(con, load_windows(con, "timeframe in ('5m','15m','1h','4h','1d')"))
    print("windows", len(df), "resolved", int(df.outcome.notna().sum()), "days",
          sorted(set(pd.to_datetime(df.start, unit="ms").dt.strftime("%Y-%m-%d"))), file=sys.stderr)
    inference_report(df)

    def entry(w, opp, stake):
        c = 0.5 + fps(0.5, w["rate"], w["exp"])
        return stake, stake / c, 0.5, w["dec_ms"] + LAT, 0.0

    AL, AB, by, skips = run_all(df, entry, primary)
    return report("B " + ("primary" if primary else "secondary"), AL, AB, by, skips), AB


def mode_a(day, primary):
    con = connect()
    con.execute("SET preserve_insertion_order = false")
    lo = BOOK_START.get(day, f"{day} 00:00:00+00")
    df = load_windows(con, f"strftime(window_start,'%Y-%m-%d')='{day}' and window_start >= TIMESTAMPTZ '{lo}'")
    df = add_inferred(con, df)
    inference_report(df)
    sers = series_of(df, primary)
    tt = pd.DataFrame([(w["slug"], w["dec_ms"], w["tf"]) for ws in sers.values() for w in ws],
                      columns=["slug", "dec", "tf"])
    m = df[["slug", "up_token_id", "down_token_id"]].merge(tt, on="slug")
    m["t"] = m.dec + LAT + m.tf.isin(HOLD_TF) * HOLD
    con.register("m", m)
    con.execute("""create temp table g as select slug, 'Up' tok, slug || '|U' asset, t from m
        union all select slug, 'Down', slug || '|D', t from m""")
    lo_ms, hi_ms = int(df.start.min()) - 3_600_000, int(df["end"].max()) + 120_000
    con.execute(f"""create temp table bba as select asset, recv_ms, best_bid, best_ask from pm_best_bid_ask
        where recv_ms between {lo_ms} and {hi_ms} and asset in (select asset from g)""")
    con.execute(f"""create temp table bk as select asset, recv_ms, asks from pm_book
        where recv_ms between {lo_ms} and {hi_ms} and asset in (select asset from g)""")
    snap = con.execute("""select g.slug, g.tok, g.t, b.best_bid, b.best_ask, k.asks, b.recv_ms bba_ms from g
        asof left join bba b on g.asset=b.asset and g.t>=b.recv_ms
        asof left join bk k on g.asset=k.asset and g.t>=k.recv_ms""").df()
    snaps = {(r.slug, r.tok): r for r in snap.itertuples()}
    stats = dict(bets_attempted=0, wide_gt_10c=0, empty_or_no_fill=0, bba_stale_gt_60s=0)

    def entry(w, opp, stake):
        r = snaps.get((w["slug"], opp))
        stats["bets_attempted"] += 1
        if r is None or pd.isna(r.best_ask) or not isinstance(r.asks, str) or not r.asks:
            stats["empty_or_no_fill"] += 1
            return None
        sp = r.best_ask - (r.best_bid if pd.notna(r.best_bid) else 0.0)
        stats["wide_gt_10c"] += sp > 0.10
        stats["bba_stale_gt_60s"] += (r.t - r.bba_ms) > 60_000
        f = walk(parse(r.asks), r.best_ask, stake, w["rate"], w["exp"])
        if f is None:
            stats["empty_or_no_fill"] += 1
            return None
        return f[0], f[1], r.best_ask, int(r.t), sp

    AL, AB, by, skips = run_all(df, entry, primary)
    out = report(f"A {day} " + ("primary" if primary else "secondary"), AL, AB, by, skips)
    print("book_stats", {k: int(v) for k, v in stats.items()})
    return out, AB


if __name__ == "__main__":
    # usage: A <day> <primary|secondary> [bets.csv]   |   B <primary|secondary> [bets.csv]
    if sys.argv[1] == "B":
        out, bets = mode_b(sys.argv[2] == "primary")
    else:
        out, bets = mode_a(sys.argv[2], sys.argv[3] == "primary")
    if sys.argv[-1].endswith(".csv"):
        pd.DataFrame(bets).to_csv(pathlib.Path(sys.argv[-1]), index=False)

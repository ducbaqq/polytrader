"""Sizing / entry variants of the streak ladder (docs/strategies/streak-ladder.md, section "Variants").

    cd analysis && POLYDATA_DIR=... uv run python ../research/streak_ladder/variants.py OUT.json

Same trigger and PRIMARY timing as streak_ladder.py (Chainlink-inferred outcomes, next window at its open); only the
stake rule, the entry filter, the entry delay and (control) the side change. Variants are pre-declared, nothing tuned:
  rec        recovery sizing: first bet $2, then shares = (L + 2) / (1 - c_eff), L = money lost so far in the ladder
  f1         +$2 ladder, bet only if ask <= 0.55 and spread <= 3c (else skip the window, keep the ladder)
  f5         same as f1 but entering at open + 5 s
  rec_f1     recovery sizing with the f1 filter
  coin_f1    negative control: random side (seeded per window), +$2 ladder, f1 filter
A = books on 11-24, 11-27, 11-26 (validation, same single look); B = outcome only at 0.50 + fee (no filter possible).
"""
import sys, json, zlib, random
import pandas as pd
import streak_ladder as sl
from streak_ladder import fps, parse
from polydata.views import connect

MAX_ASK, MAX_SPREAD, BANKROLL = 0.55, 0.03, 5000.0


def levels(asks, best_ask):
    lv = [(p, s) for p, s in asks if p >= best_ask - 1e-9]
    if lv and lv[0][0] > best_ask + 1e-9:
        lv.insert(0, (best_ask, lv[0][1]))
    return lv


def take_shares(lv, target, rate, exp):
    spent = got = 0.0
    for p, s in lv:
        q = min(s, target - got)
        spent += q * (p + fps(p, rate, exp))
        got += q
        if target - got < 1e-9:
            break
    return spent, got


def fill(lv, rate, exp, budget=None, need=None):
    """-> (spent, shares, shortfall_shares) or None. budget: spend that much. need: shares s with s (1 - c_eff) = need."""
    if not lv:
        return None
    if budget is not None:
        r = sl.walk(lv, lv[0][0], budget, rate, exp)
        return None if r is None else (r[0], r[1], 0.0)
    c = lv[0][0] + fps(lv[0][0], rate, exp)
    for _ in range(30):
        if c >= 0.995:
            return None
        target = need / (1 - c)
        spent, got = take_shares(lv, target, rate, exp)
        if got <= 0:
            return None
        c2 = spent / got
        if abs(c2 - c) < 1e-9:
            break
        c = c2
    return spent, got, max(0.0, target - got)


def run_series(ws, entry_fn, primary, sizing, control, policy="A"):
    ladders, bets = [], []
    skips = {"unresolved": 0, "no_fill": 0, "waited": 0}
    n = len(ws)
    last_with_outcome = max((i for i, w in enumerate(ws) if w["outcome"]), default=-1)
    i, run_dir, run_len, prev = 0, None, 0, None
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
        if run_len < sl.N_STREAK:
            i += 1
            continue
        base_opp = "Down" if run_dir == "Up" else "Up"
        lad = dict(series=w["series"], trigger=w["slug"], bets=0, max_stake=0.0, pnl=0.0, status="open", losses=0,
                   maxL=0.0, path=[], shortfall=0.0)
        k, L, ended = i + 1, 0.0, False
        while k < n:
            wk = ws[k]
            if wk["dec_ms"] >= wk["end"]:
                skips["waited"] += 1
                k += 1
                continue
            if not wk["outcome"]:
                if k <= last_with_outcome:
                    skips["unresolved"] += 1
                    k += 1
                    continue
                break
            opp = base_opp
            if control:
                opp = random.Random(zlib.crc32(wk["slug"].encode()) ^ 20261127).choice(["Up", "Down"])
            if sizing == "rec" and L > 0:
                ent = entry_fn(wk, opp, None, L + sl.STEP)
            else:
                ent = entry_fn(wk, opp, sl.STEP * (lad["losses"] + 1) if sizing == "plus2" else sl.STEP, None)
            if ent is None:
                skips["no_fill"] += 1
                if policy == "C":  # abandon: the skip ends the ladder, wait for a fresh streak
                    lad["status"] = "abandoned"
                    ended = True
                    break
                if policy == "B":  # shadow: tracked as if bet, no money
                    if wk["bout"] == opp:
                        lad["status"] = "shadow_end"
                        ended = True
                        break
                    sh = (entry_fn(wk, opp, sl.STEP * (lad["losses"] + 1), None, True) if sizing == "plus2" or L == 0
                          else entry_fn(wk, opp, None, L + sl.STEP, True))
                    sh_spent = sh[0] if sh else sl.STEP * (lad["losses"] + 1)
                    lad["losses"] += 1
                    L += sh_spent
                    lad["maxL"] = max(lad["maxL"], L)
                    lad["shadow_losses"] = lad.get("shadow_losses", 0) + 1
                k += 1
                continue
            spent, shares, short, ask, t_in, spread = ent
            real_win, bot_win = wk["outcome"] == opp, wk["bout"] == opp
            pnl = (shares - spent) if real_win else -spent
            bets.append(dict(series=w["series"], slug=wk["slug"], n=lad["bets"] + 1, spent=spent, price_paid=spent / shares,
                             win=real_win, pnl=pnl, t_in=t_in, t_out=wk["res_ms"], spread=spread))
            lad["bets"] += 1
            lad["path"].append(round(spent, 2))
            lad["max_stake"] = max(lad["max_stake"], spent)
            lad["pnl"] += pnl
            lad["shortfall"] += short
            if bot_win:
                lad["status"] = "won" if real_win else "stopped_on_misread_loss"
                ended = True
                break
            lad["losses"] += 1
            L += spent
            lad["maxL"] = max(lad["maxL"], L)
            k += 1
        ladders.append(lad)
        if ended:
            run_dir, run_len, prev = None, 0, None
            i = k + 1
        else:
            break
    return ladders, bets, skips


def cash_path(bets):
    ev = []
    for b in bets:
        ev.append((b["t_in"], 1, -b["spent"], b["slug"]))
        ev.append((b["t_out"], 0, b["spent"] + b["pnl"], b["slug"]))
    ev.sort(key=lambda e: (e[0], e[1]))
    cash = low = 0.0
    bust = None
    for t, _, d, slug in ev:
        cash += d
        low = min(low, cash)
        if bust is None and cash < -BANKROLL:
            bust = (pd.Timestamp(t, unit="ms", tz="UTC").strftime("%H:%M"), slug)
    return -low, bust


def summarize(ladders, bets, skips, stats=None):
    df, lad = pd.DataFrame(bets), pd.DataFrame(ladders)
    if df.empty:
        return dict(bets=0, triggers=len(lad), skips=skips)
    need, bust = cash_path(bets)
    first = df[df.n == 1]
    return dict(ladders=len(lad), completed=int((lad.status != "open").sum()), open=int((lad.status == "open").sum()),
                bets=len(df), first_win=round(first.win.mean(), 3), first_price=round(first.price_paid.mean(), 3),
                staked=round(df.spent.sum(), 1), pnl=round(df.pnl.sum(), 2), roi=round(df.pnl.sum() / df.spent.sum(), 4),
                max_stake=round(lad.max_stake.max(), 1), max_ladder_loss=round(lad.maxL.max(), 1),
                worst_ladder=round(lad.pnl.min(), 1), longest_loss_run=int(lad.losses.max()),
                peak_exposure=round(sl.exposure_peak(bets)[0], 1), bankroll_needed=round(need, 1),
                bust_5k=bust, shortfall_shares=round(lad.shortfall.sum(), 2),
                biggest_stake_path=max(lad.path, key=lambda p: max(p) if p else 0), skips=skips, book_stats=stats)


def run_all(df, entry, primary, sizing, control, policy="A"):
    AL, AB = [], []
    skips = {"unresolved": 0, "no_fill": 0, "waited": 0}
    for s, ws in sl.series_of(df, primary).items():
        l, b, sk = run_series(ws, entry, primary, sizing, control, policy)
        AL += l
        AB += b
        for k in skips:
            skips[k] += sk[k]
    return AL, AB, skips


def day_setup(con, day, delay):
    sl.DELAY = delay
    lo = sl.BOOK_START.get(day, f"{day} 00:00:00+00")
    df = sl.add_inferred(con, sl.load_windows(con, f"strftime(window_start,'%Y-%m-%d')='{day}' and window_start >= TIMESTAMPTZ '{lo}'"))
    sers = sl.series_of(df, True)
    tt = pd.DataFrame([(w["slug"], w["dec_ms"], w["tf"]) for ws in sers.values() for w in ws], columns=["slug", "dec", "tf"])
    m = tt.copy()
    m["t"] = m.dec + sl.LAT + m.tf.isin(sl.HOLD_TF) * sl.HOLD
    con.register("m", m)
    con.execute("""create or replace temp table g as select slug, 'Up' tok, slug || '|U' asset, t from m
        union all select slug, 'Down', slug || '|D', t from m""")
    lo_ms, hi_ms = int(df.start.min()) - 3_600_000, int(df["end"].max()) + 120_000
    con.execute(f"""create or replace temp table bba as select asset, recv_ms, best_bid, best_ask from pm_best_bid_ask
        where recv_ms between {lo_ms} and {hi_ms} and asset in (select asset from g)""")
    con.execute(f"""create or replace temp table bk as select asset, recv_ms, asks from pm_book
        where recv_ms between {lo_ms} and {hi_ms} and asset in (select asset from g)""")
    snap = con.execute("""select g.slug, g.tok, g.t, b.best_bid, b.best_ask, k.asks from g
        asof left join bba b on g.asset=b.asset and g.t>=b.recv_ms
        asof left join bk k on g.asset=k.asset and g.t>=k.recv_ms""").df()
    return df, {(r.slug, r.tok): r for r in snap.itertuples()}


def books_entry(snaps, filt, stats):
    def entry(w, opp, budget, need, force=False):
        r = snaps.get((w["slug"], opp))
        if r is None or pd.isna(r.best_ask) or not isinstance(r.asks, str) or not r.asks:
            stats["empty"] += 1
            return None
        sp = r.best_ask - (r.best_bid if pd.notna(r.best_bid) else 0.0)
        if filt and not force and (r.best_ask > MAX_ASK + 1e-9 or sp > MAX_SPREAD + 1e-9):
            stats["filtered"] += 1
            return None
        lv = levels(parse(r.asks), r.best_ask)
        f = fill(lv, w["rate"], w["exp"], budget, need)
        if f is None:
            stats["empty"] += 1
            return None
        stats["placed"] += 1
        return f[0], f[1], f[2], r.best_ask, int(r.t), sp
    return entry


def b_entry(w, opp, budget, need, force=False):
    f = fill([(0.5, 1e12)], w["rate"], w["exp"], budget, need)
    return None if f is None else (f[0], f[1], f[2], 0.5, w["dec_ms"] + sl.LAT, 0.0)


VARIANTS = {  # name: (sizing, filter, delay_ms, control)
    "rec": ("rec", False, 1000, False),
    "f1": ("plus2", True, 1000, False),
    "f5": ("plus2", True, 5000, False),
    "rec_f1": ("rec", True, 1000, False),
    "coin_f1": ("plus2", True, 1000, True),
}

if __name__ == "__main__":
    # usage: variants.py OUT.json [policies=A] [variants=all] [B=yes|no]
    policies = (sys.argv[2] if len(sys.argv) > 2 else "A").split(",")
    names = (sys.argv[3] if len(sys.argv) > 3 else ",".join(VARIANTS)).split(",")
    do_b = (sys.argv[4] if len(sys.argv) > 4 else "yes") == "yes"
    con = connect()
    con.execute("SET preserve_insertion_order = false")
    out = {}
    for name in names:
        sizing, filt, delay, control = VARIANTS[name]
        for pol in policies:
            for day in ("2026-11-24", "2026-11-27", "2026-11-26"):
                df, snaps = day_setup(con, day, delay)
                stats = dict(placed=0, filtered=0, empty=0)
                AL, AB, sk = run_all(df, books_entry(snaps, filt, stats), True, sizing, control, pol)
                key = f"{name}|{pol}|A {day}"
                out[key] = summarize(AL, AB, sk, stats)
                print(key, {k: out[key].get(k) for k in ("bets", "pnl", "roi", "max_stake", "bankroll_needed", "bust_5k")}, flush=True)
    if do_b:
        sl.DELAY = 1000
        dfb = sl.add_inferred(con, sl.load_windows(con, "timeframe in ('5m','15m','1h','4h','1d')"))
        for name, sizing, control in (("rec", "rec", False), ("plus2", "plus2", False), ("coin", "plus2", True)):
            AL, AB, skp = run_all(dfb, b_entry, True, sizing, control)
            out[f"{name}|A|B"] = summarize(AL, AB, skp)
            print(name, "B", {k: out[f"{name}|A|B"].get(k) for k in ("bets", "pnl", "roi", "max_stake", "bankroll_needed", "bust_5k")}, flush=True)
    json.dump(out, open(sys.argv[1], "w"), indent=1, default=str)

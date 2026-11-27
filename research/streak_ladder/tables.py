"""Markdown tables from the streak_ladder.py outputs.  python tables.py OUTDIR  (OUTDIR holds A_<day>_<v>.out/.csv, B_<v>.json)."""
import json, sys, pathlib
import pandas as pd

d = pathlib.Path(sys.argv[1])
COLS = ["series", "triggers", "completed", "open", "bets", "first_win", "first_price", "all_win", "longest_loss_run",
        "max_stake", "staked", "pnl", "roi_staked", "max_dd"]


def load(p):
    t = p.read_text()
    i = t.rfind("\nbook_stats")
    return json.loads(t[:i] if i >= 0 else t), (t[i:].strip() if i >= 0 else "")


def table(o):
    rows = [r for r in o["series"] if r.get("bets")] + [o["total"]]
    df = pd.DataFrame(rows)[COLS]
    print("| " + " | ".join(COLS) + " |")
    print("|" + "---|" * len(COLS))
    for r in df.itertuples(index=False):
        print("| " + " | ".join("" if pd.isna(x) else str(x) for x in r) + " |")


for name in ["B_primary.json", "B_secondary.json"] + [f"A_{day}_{v}.out" for v in ("primary", "secondary")
                                                      for day in ("2026-11-24", "2026-11-27", "2026-11-26")]:
    o, bs = load(d / name)
    print(f"\n#### {name.rsplit('.', 1)[0]}  (peak simultaneous exposure ${o['peak_exposure']}; skips {o['skips']}; {bs})\n")
    table(o)

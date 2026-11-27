"""Markdown table of the variants.py outputs.  python vtable.py v1.json v2.json ..."""
import json, sys

d = {}
for f in sys.argv[1:]:
    d.update(json.load(open(f)))
print("| variant | policy | day | ladders | bets | skips | first-bet win @ price | P&L | on staked | max stake | max ladder loss "
      "| worst ladder | longest loss run | peak exposure | bankroll needed | bust at $5k | depth shortfall (sh) |")
print("|" + "---|" * 17)
for k, v in d.items():
    name, pol, day = k.split("|")
    if not v.get("bets"):
        print(f"| {name} | {pol} | {day} | {v.get('triggers')} | 0 | | | | | | | | | | | | |")
        continue
    bust = v["bust_5k"]
    bust = f"{bust[0]} UTC" if isinstance(bust, (list, tuple)) else (bust or "no")
    if isinstance(v["bust_5k"], str) and v["bust_5k"] != "None":
        bust = v["bust_5k"][2:7] + " UTC"
    print(f"| {name} | {pol} | {day} | {v['ladders']} | {v['bets']} | {v['skips']['no_fill']} | {v['first_win']:.1%} @ {v['first_price']} "
          f"| {v['pnl']:+.0f} | {v['roi']:+.1%} | ${v['max_stake']:.0f} | ${v['max_ladder_loss']:.0f} | ${v['worst_ladder']:.0f} "
          f"| {v['longest_loss_run']} | ${v['peak_exposure']:.0f} | ${v['bankroll_needed']:.0f} | {bust} | {v['shortfall_shares']} |")

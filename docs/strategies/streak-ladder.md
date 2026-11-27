# streak-ladder (bet against a 3-in-a-row, add $2 after every loss)

**Family:** martingale / streak-reversal | **Status:** abandoned (no edge shown; execution cost makes it negative)
**Current version:** research script only (`research/streak_ladder/`), not a `bt` strategy (the strategy API hides
outcomes, so the simulator cannot run it; books are read directly) | **sim_version:** n/a (no simulator run)
Related: [martingale-streak-reversal](martingale-streak-reversal.md) (an earlier informal doubling variant).

## Hypothesis (the user's rule, no free parameters)

Per series (coin x timeframe, each an independent sequence of consecutive windows):
1. Wait for 3 consecutive windows with the same outcome (Up, Up, Up).
2. Bet $2 on the opposite outcome (Down) in the next window.
3. If it loses (the streak direction came again), bet again on the same opposite outcome in the next window with
   stake + $2 (2, 4, 6, 8, ...).
4. On the first win stop, reset, and wait for a fresh 3-in-a-row counted only from windows after the winning one.

"Bet $X" = spend $X buying the opposite token as a taker at the ask: shares = X / (ask + fee per share), fee per share
= rate p (1 - p) (0.05 BTC 5m/15m, 0.01 elsewhere). Taker hold 250 ms on 5m/15m/1h, order latency 200 ms.

Primary metric: win rate of the first bet after a streak against the price paid, with P&L and return on money staked;
plus the ladder risk (stake reached, drawdown, simultaneous exposure vs a $5,000 bankroll).
Variants tried: 1 rule x 2 timing variants x 2 evaluations (nothing tuned). Sealed days (2026-11-25, 2026-11-28) never
read. 2026-11-26 is the validation day: one look, reported separately.

## Method

Code: `research/streak_ladder/streak_ladder.py` (tables: `tables.py`). Windows come from `markets` (non-sealed days);
a series' windows are "consecutive" only if their starts differ by exactly the timeframe; a window without an
outcome or a gap in the series breaks the streak. Each A day is run separately from a fresh state (ladders still
open at the end of the day are counted as open and their realised losses are included).

**Timing, PRIMARY (the user's override).** The bot infers each outcome from the Chainlink ticks it has received
(paper `ChainlinkSettler` rule: Up iff mean of first-received ticks with ts in [end - 60 s, end) >= mean of the ticks
in [start - 60 s, start) received by the first tick with ts >= start), at the `recv_ms` of the first tick with
ts >= window end (median about 1.4-1.5 s after the end). The streak counter and the ladder run on the inferred
outcome; the next window (starting at that boundary) is bet at open + 1 s (or the knowledge time if later) + 200 ms
latency + 250 ms hold. For 5m the immediately next window is bet, nothing is skipped. P&L settles on the OFFICIAL
outcome. A window ending right before a sealed day has no inferable outcome (the next ticks are hidden) and falls
back to the official resolution.

**Timing, SECONDARY (my first reading).** Wait for the official `pm_resolution` recv; the first window whose start is
after it is bet. This skips the windows already running: 316 / 732 / 333 windows on A days 11-24 / 11-26 / 11-27
(3,433 in B), so about 37% fewer triggers (658 vs 1,044 in B) and fewer bets.

**A (execution-realistic).** Entry = best ask of the opposite token (`pm_best_bid_ask`, ASOF on `recv_ms`) at the entry
time, walked through the last `pm_book` snapshot at or before it (levels at or above the best ask; if the snapshot is
older than the best ask its top size is assumed), fee included. Days 11-24 (windows from 10:36 UTC), 11-27 (data to
about 10:00 UTC; open ladders at the end), 11-26 validation. No bet had an empty book or a failed fill; 4-9 per day
used a best bid/ask older than 60 s. **Wide books:** the rule bets regardless, so the primary numbers include them:
167 / 177 / 337 of 298 / 320 / 710 bets (11-24 / 11-27 / 11-26) had a spread above 10c; those bets pay about 0.75.
The book at window open is wide and the opposite token's ask is high.

**B (outcome only).** Every resolved non-sealed market (11-22, 11-23, 11-24, 11-26, 11-27; 9,151 resolved windows over
5m/15m/1h/4h/1d, 8 coins), price = 0.50 + fee, entry timing as in the variant. B overlaps the A days; it adds 11-23 and
the windows before 10:36 on 11-24.

Peak simultaneous exposure = the largest sum of open stakes (entry to official resolution recv) across all series.

## Results

### Inference quality (Chainlink vs official outcome)

54 of 9,051 inferred windows differ from the official outcome (0.6%: 41 of 6,362 on 5m, 12 of 2,115 on 15m, 1 of 448
on 1h, 0 on 4h/1d), all near-ties. Effect on the ladders in B (primary): 17 of 1,044 triggers were false (the
official outcomes were not a 3-streak), 13 bets were misread (the bot's win/loss differs from the real one), and 5
ladders stopped after a real loss that the bot read as a win (the loss is booked and the ladder is not continued).
On the A days: 2-5 false triggers and 2-3 misread bets per day. The effect on P&L is a few dollars of noise.

### A: books, primary timing (inferred outcomes, next window at its open)

| day | triggers | bets | first-bet win | price paid | all-bet win | P&L | on staked | max drawdown | longest loss run | max stake | peak exposure |
|---|---|---|---|---|---|---|---|---|---|---|---|
| 2026-11-24 (train) | 181 | 298 | 58.0% (n 174) | 0.659 | 58.7% | -$72.88 | -7.1% | $110.94 | 6 | $14 | $98 |
| 2026-11-27 (train, partial) | 183 | 320 | 51.9% (n 181) | 0.606 | 56.3% | -$51.97 | -4.8% | $82.20 | 5 | $12 | $100 |
| 2026-11-26 (validation, 1 look) | 381 | 710 | 56.8% (n 380) | 0.649 | 53.7% | -$430.48 | -15.6% | $469.87 | 10 | $22 | $160 |

The first bet wins 52-58%, above 50% but below the 0.61-0.66 it costs. Splitting the first bets by spread (pooled
over the 3 days): narrow books (spread <= 10c, n 375) win 57.6% at a price of about 0.54 (+3.6c per share, standard
error about 2.6c); wide books (n 360) win 54.0% at about 0.75 (-21c). The rule cannot avoid the wide books (that is where
it enters); a narrow-book filter would be a new, tuned variant and was not run.

Probability that a ladder reaches a stake >= $6 / $10 / $20 (B primary): 22.3% / 5.6% / 0.19%, against a fair coin
25% / 6.25% / 0.20% (stake 2k needs k - 1 straight losses); 0 of 1,044 reached $30. A: 11-26 had one $22 ladder (10
losses in a row, -$90.76).

### A: books, secondary timing (official resolution recv; windows skipped)

| day | triggers | bets | first-bet win | price paid | P&L | on staked | max drawdown | longest loss run | max stake | peak exposure |
|---|---|---|---|---|---|---|---|---|---|---|
| 2026-11-24 | 152 | 248 | 60.1% (n 143) | 0.659 | -$79.63 | -9.1% | $115.16 | 6 | $14 | $68 |
| 2026-11-27 | 135 | 256 | 43.8% (n 128) | 0.615 | -$187.17 | -18.2% | $191.54 | 11 | $24 | $70 |
| 2026-11-26 | 309 | 576 | 53.2% (n 308) | 0.633 | -$260.01 | -12.2% | $283.87 | 7 | $16 | $60 |

### B: outcome only, price 0.50 + fee (optimistic entry)

| variant | triggers | completed / open | bets | first-bet win (n) | price | all-bet win | P&L | on staked | max drawdown | longest loss run | max stake | worst ladder | peak exposure |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| primary | 1,044 | 1,040 / 4 | 1,985 | 53.0% (1,041) | 0.5036 | 52.5% | +$302.58 | +4.0% | $286.78 | 10 | $22 | -$84.24 | $196 |
| secondary | 658 | 648 / 10 | 1,274 | 50.3% (652) | 0.5035 | 50.9% | +$159.74 | +3.3% | $114.81 | 10 | $22 | -$88.22 | $88 |

First-bet win rate: 53.0% vs 0.504 paid (primary: +2.5 points, standard error 1.5, about 1.7 sigma, not significant;
the secondary variant on a subset of the same windows gives 50.3%). A ladder that ends in a win nets about $2 less
fees, so any win rate near 50% gives a small positive result at a 0.50 price, with the cost carried by rare deep
ladders. Real entry prices at the open are about 0.65 (A), which turns the result negative.

The per-series tables (generated by `research/streak_ladder/tables.py`, at the end) show 8-95 triggers per series and
first-bet win rates scattered widely around 50% on small samples (the 1h/4h/1d series have 0-8 triggers). The deepest
ladder (10 losses, -$84) was bnb-5m. Same-window bets across coins are correlated (a market-wide move makes several
series streak and lose together), so peak exposure ($60-$196) and drawdowns come in clusters.

## Verdict

Abandoned. (1) No demonstrated edge: consecutive outcomes are close to independent; the first bet after a streak wins
50.3-53% in B and 52-58% in the books, against a price of 0.50 (stand-in) or 0.61-0.66 (real asks at the open). All
three book days lose money; the validation day 11-26 is the worst (-$430, -15.6% on money staked). (2) The execution
cost decides: at the window open the opposite token is quoted wide and high (about 0.75 in half of the bets), so a taker
pays about 15c over fair on average. Only narrow-book bets look near break-even (+3.6c per share, one standard error
2.6c) and selecting them is a tuned variant. (3) Risk: small gains against rare deep losses. Observed worst ladders
cost $84-$91 on $2 base stakes (10 losses in a row, max stake $22); peak simultaneous exposure was $196 (B primary) or
$60-$160 (A) against a $5,000 bankroll, so ruin is not the issue at this size, but the expected value is not
positive. (4) The 0.6% of Chainlink misreads do not change the conclusion. Samples are small (3 book days), so a
small edge could be hidden; nothing here supports trading it.

## Lessons
- A streak-reversal bet is judged by its entry price, not its win rate: the first-bet win rate is near 50-56% but the
  taker price at the window open is 0.61-0.66, so the rule loses.
- Betting at the very open means paying the opening spread; wide books are not avoidable within the rule.
- Waiting for Polymarket's resolution (about 1 min) skips about 37% of the entries; Chainlink inference is right
  99.4% of the time and available about 1.4 s after the window end.

## Per-series tables (generated)

#### B_primary  (peak simultaneous exposure $196.0; skips {'unresolved': 5, 'no_fill': 0, 'waited': 2}; )

| series | triggers | completed | open | bets | first_win | first_price | all_win | longest_loss_run | max_stake | staked | pnl | roi_staked | max_dd |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| bnb-15m | 31 | 31 | 0 | 65 | 0.4839 | 0.5025 | 0.4923 | 5 | 12.0 | 260.0 | 10.65 | 0.0409 | 32.3 |
| bnb-1h | 8 | 8 | 0 | 23 | 0.375 | 0.5025 | 0.3478 | 5 | 12.0 | 116.0 | -24.46 | -0.2108 | 40.22 |
| bnb-4h | 2 | 1 | 1 | 1 | 1.0 | 0.5025 | 1.0 | 0 | 2.0 | 2.0 | 1.98 | 0.99 | 0.0 |
| bnb-5m | 95 | 95 | 0 | 176 | 0.5474 | 0.5025 | 0.5455 | 10 | 22.0 | 750.0 | -45.52 | -0.0607 | 135.87 |
| btc-15m | 28 | 28 | 0 | 48 | 0.5714 | 0.5125 | 0.5833 | 4 | 10.0 | 158.0 | 29.32 | 0.1856 | 20.0 |
| btc-1h | 8 | 8 | 0 | 18 | 0.375 | 0.5025 | 0.4444 | 4 | 10.0 | 72.0 | -0.36 | -0.005 | 20.0 |
| btc-4h | 1 | 1 | 0 | 1 | 1.0 | 0.5025 | 1.0 | 0 | 2.0 | 2.0 | 1.98 | 0.99 | 0.0 |
| btc-5m | 83 | 83 | 0 | 156 | 0.5542 | 0.5125 | 0.5321 | 5 | 12.0 | 598.0 | 10.78 | 0.018 | 56.34 |
| doge-15m | 28 | 27 | 1 | 47 | 0.5185 | 0.5025 | 0.5745 | 3 | 8.0 | 152.0 | 35.06 | 0.2307 | 12.0 |
| doge-1h | 8 | 7 | 1 | 19 | 0.25 | 0.5025 | 0.3684 | 4 | 10.0 | 76.0 | -12.32 | -0.1621 | 20.0 |
| doge-4h | 1 | 1 | 0 | 4 | 0.0 | 0.5025 | 0.25 | 3 | 8.0 | 20.0 | -4.08 | -0.204 | 12.0 |
| doge-5m | 92 | 92 | 0 | 167 | 0.5435 | 0.5025 | 0.5449 | 5 | 12.0 | 600.0 | 56.72 | 0.0945 | 42.64 |
| eth-15m | 28 | 28 | 0 | 44 | 0.5714 | 0.5025 | 0.6591 | 2 | 6.0 | 128.0 | 55.08 | 0.4303 | 6.06 |
| eth-1h | 7 | 7 | 0 | 18 | 0.1429 | 0.5025 | 0.3889 | 3 | 8.0 | 70.0 | 1.64 | 0.0235 | 12.0 |
| eth-4h | 1 | 1 | 0 | 2 | 0.0 | 0.5025 | 0.5 | 1 | 4.0 | 6.0 | 1.96 | 0.3267 | 2.0 |
| eth-5m | 87 | 87 | 0 | 162 | 0.5747 | 0.5025 | 0.5432 | 6 | 14.0 | 628.0 | 20.76 | 0.0331 | 72.6 |
| hype-15m | 33 | 33 | 0 | 62 | 0.6364 | 0.5025 | 0.5323 | 4 | 10.0 | 236.0 | 10.77 | 0.0456 | 36.28 |
| hype-1h | 8 | 7 | 1 | 12 | 0.5714 | 0.5025 | 0.5833 | 2 | 6.0 | 38.0 | 9.76 | 0.2569 | 6.0 |
| hype-4h | 3 | 3 | 0 | 3 | 1.0 | 0.5025 | 1.0 | 0 | 2.0 | 6.0 | 5.94 | 0.99 | 0.0 |
| hype-5m | 92 | 92 | 0 | 180 | 0.5326 | 0.5025 | 0.5056 | 7 | 16.0 | 716.0 | -3.56 | -0.005 | 58.78 |
| sol-15m | 29 | 29 | 0 | 42 | 0.6552 | 0.5025 | 0.6905 | 3 | 8.0 | 118.0 | 49.16 | 0.4166 | 12.0 |
| sol-1h | 8 | 8 | 0 | 19 | 0.5 | 0.5025 | 0.4211 | 4 | 10.0 | 84.0 | -8.38 | -0.0997 | 20.0 |
| sol-4h | 1 | 1 | 0 | 4 | 0.0 | 0.5025 | 0.25 | 3 | 8.0 | 20.0 | -4.08 | -0.204 | 12.0 |
| sol-5m | 95 | 95 | 0 | 170 | 0.5474 | 0.5025 | 0.5647 | 6 | 14.0 | 594.0 | 86.6 | 0.1458 | 42.0 |
| xrp-15m | 33 | 33 | 0 | 66 | 0.4242 | 0.5025 | 0.5152 | 4 | 10.0 | 236.0 | 34.65 | 0.1468 | 20.0 |
| xrp-1h | 8 | 8 | 0 | 18 | 0.5 | 0.5025 | 0.4444 | 6 | 14.0 | 88.0 | -16.36 | -0.1859 | 42.0 |
| xrp-4h | 2 | 2 | 0 | 2 | 1.0 | 0.5025 | 1.0 | 0 | 2.0 | 4.0 | 3.96 | 0.99 | 0.0 |
| xrp-5m | 91 | 91 | 0 | 184 | 0.5275 | 0.5025 | 0.4891 | 6 | 14.0 | 748.0 | -19.64 | -0.0263 | 72.63 |
| zec-15m | 37 | 37 | 0 | 70 | 0.5405 | 0.5025 | 0.5143 | 4 | 10.0 | 254.0 | 20.63 | 0.0812 | 20.0 |
| zec-4h | 2 | 2 | 0 | 6 | 0.0 | 0.5025 | 0.3333 | 3 | 8.0 | 26.0 | -2.12 | -0.0815 | 12.0 |
| zec-5m | 94 | 94 | 0 | 196 | 0.4468 | 0.5025 | 0.4847 | 7 | 16.0 | 792.0 | -3.94 | -0.005 | 59.51 |
| TOTAL | 1044 | 1040 | 4 | 1985 | 0.5303 | 0.5036 | 0.5254 | 10 | 22.0 | 7598.0 | 302.58 | 0.0398 | 286.78 |

#### B_secondary  (peak simultaneous exposure $88.0; skips {'unresolved': 3, 'no_fill': 0, 'waited': 3433}; )

| series | triggers | completed | open | bets | first_win | first_price | all_win | longest_loss_run | max_stake | staked | pnl | roi_staked | max_dd |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| bnb-15m | 17 | 17 | 0 | 38 | 0.5294 | 0.5025 | 0.4474 | 5 | 12.0 | 174.0 | -22.76 | -0.1308 | 40.22 |
| bnb-1h | 7 | 7 | 0 | 16 | 0.2857 | 0.5025 | 0.4375 | 3 | 8.0 | 60.0 | 3.68 | 0.0614 | 12.0 |
| bnb-4h | 1 | 1 | 0 | 1 | 1.0 | 0.5025 | 1.0 | 0 | 2.0 | 2.0 | 1.98 | 0.99 | 0.0 |
| bnb-5m | 75 | 75 | 0 | 139 | 0.5733 | 0.5025 | 0.5396 | 5 | 12.0 | 514.0 | 39.23 | 0.0763 | 50.52 |
| btc-15m | 17 | 17 | 0 | 36 | 0.4706 | 0.5125 | 0.4722 | 5 | 12.0 | 146.0 | -5.51 | -0.0378 | 30.0 |
| btc-1h | 7 | 7 | 0 | 14 | 0.4286 | 0.5025 | 0.5 | 2 | 6.0 | 48.0 | 7.72 | 0.1609 | 6.06 |
| btc-4h | 1 | 1 | 0 | 2 | 0.0 | 0.5025 | 0.5 | 1 | 4.0 | 6.0 | 1.96 | 0.3267 | 2.0 |
| btc-5m | 52 | 51 | 1 | 80 | 0.6275 | 0.5125 | 0.6375 | 3 | 8.0 | 244.0 | 68.2 | 0.2795 | 12.0 |
| doge-15m | 18 | 17 | 1 | 37 | 0.5294 | 0.5025 | 0.4595 | 4 | 10.0 | 158.0 | -10.74 | -0.068 | 34.18 |
| doge-1h | 7 | 6 | 1 | 13 | 0.4286 | 0.5025 | 0.4615 | 2 | 6.0 | 44.0 | 3.76 | 0.0855 | 6.06 |
| doge-4h | 1 | 1 | 0 | 2 | 0.0 | 0.5025 | 0.5 | 1 | 4.0 | 6.0 | 1.96 | 0.3267 | 2.0 |
| doge-5m | 75 | 75 | 0 | 139 | 0.52 | 0.5025 | 0.5396 | 7 | 16.0 | 522.0 | 31.23 | 0.0598 | 56.0 |
| eth-15m | 16 | 16 | 0 | 36 | 0.4375 | 0.5025 | 0.4444 | 3 | 8.0 | 138.0 | 5.28 | 0.0383 | 12.0 |
| eth-1h | 6 | 6 | 0 | 13 | 0.3333 | 0.5025 | 0.4615 | 4 | 10.0 | 52.0 | -0.26 | -0.005 | 20.0 |
| eth-4h | 1 | 1 | 0 | 1 | 1.0 | 0.5025 | 1.0 | 0 | 2.0 | 2.0 | 1.98 | 0.99 | 0.0 |
| eth-5m | 48 | 47 | 1 | 88 | 0.4894 | 0.5025 | 0.5341 | 4 | 10.0 | 306.0 | 44.25 | 0.1446 | 28.28 |
| hype-15m | 16 | 16 | 0 | 36 | 0.375 | 0.5025 | 0.4444 | 5 | 12.0 | 146.0 | -2.72 | -0.0186 | 30.06 |
| hype-1h | 6 | 5 | 1 | 12 | 0.6 | 0.5025 | 0.4167 | 5 | 12.0 | 60.0 | -12.24 | -0.204 | 30.0 |
| hype-4h | 2 | 1 | 1 | 5 | 0.0 | 0.5025 | 0.2 | 2 | 6.0 | 18.0 | -6.06 | -0.3367 | 6.06 |
| hype-5m | 67 | 66 | 1 | 128 | 0.4627 | 0.5025 | 0.5156 | 6 | 14.0 | 474.0 | 31.47 | 0.0664 | 46.14 |
| sol-15m | 17 | 16 | 1 | 29 | 0.5 | 0.5025 | 0.5517 | 2 | 6.0 | 94.0 | 21.42 | 0.2279 | 6.06 |
| sol-1h | 7 | 6 | 1 | 15 | 0.1667 | 0.5025 | 0.4 | 3 | 8.0 | 58.0 | 1.7 | 0.0293 | 12.0 |
| sol-4h | 1 | 1 | 0 | 2 | 0.0 | 0.5025 | 0.5 | 1 | 4.0 | 6.0 | 1.96 | 0.3267 | 2.0 |
| sol-5m | 47 | 47 | 0 | 80 | 0.617 | 0.5025 | 0.5875 | 4 | 10.0 | 276.0 | 42.41 | 0.1537 | 26.56 |
| xrp-15m | 18 | 18 | 0 | 42 | 0.4444 | 0.5025 | 0.4286 | 5 | 12.0 | 186.0 | -18.84 | -0.1013 | 40.24 |
| xrp-1h | 7 | 6 | 1 | 13 | 0.4286 | 0.5025 | 0.4615 | 3 | 8.0 | 46.0 | 1.76 | 0.0383 | 12.0 |
| xrp-4h | 2 | 2 | 0 | 3 | 0.5 | 0.5025 | 0.6667 | 1 | 4.0 | 8.0 | 3.94 | 0.4925 | 2.0 |
| xrp-5m | 47 | 47 | 0 | 90 | 0.5106 | 0.5025 | 0.5222 | 4 | 10.0 | 326.0 | 32.21 | 0.0988 | 22.6 |
| zec-15m | 16 | 16 | 0 | 46 | 0.4375 | 0.5025 | 0.3478 | 10 | 22.0 | 282.0 | -98.92 | -0.3508 | 122.62 |
| zec-4h | 2 | 2 | 0 | 3 | 0.5 | 0.5025 | 0.6667 | 1 | 4.0 | 8.0 | 3.94 | 0.4925 | 2.0 |
| zec-5m | 54 | 54 | 0 | 115 | 0.4444 | 0.5025 | 0.4696 | 6 | 14.0 | 472.0 | -14.29 | -0.0303 | 42.0 |
| TOTAL | 658 | 648 | 10 | 1274 | 0.5031 | 0.5035 | 0.5086 | 10 | 22.0 | 4882.0 | 159.74 | 0.0327 | 114.81 |

#### A_2026-11-24_primary  (peak simultaneous exposure $98.0; skips {'unresolved': 0, 'no_fill': 0, 'waited': 0}; book_stats {'bets_attempted': 298, 'wide_gt_10c': 167, 'empty_or_no_fill': 0, 'bba_stale_gt_60s': 4})

| series | triggers | completed | open | bets | first_win | first_price | all_win | longest_loss_run | max_stake | staked | pnl | roi_staked | max_dd |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| bnb-15m | 6 | 5 | 1 | 11 | 0.5 | 0.6919 | 0.5455 | 4 | 10.0 | 44.0 | -4.46 | -0.1013 | 9.4 |
| bnb-5m | 18 | 18 | 0 | 28 | 0.5556 | 0.5907 | 0.6429 | 3 | 8.0 | 82.0 | 7.65 | 0.0932 | 12.0 |
| btc-15m | 6 | 6 | 0 | 10 | 0.5 | 0.5291 | 0.6 | 2 | 6.0 | 30.0 | 7.74 | 0.2581 | 6.0 |
| btc-5m | 13 | 12 | 1 | 18 | 0.5385 | 0.5608 | 0.6667 | 1 | 4.0 | 46.0 | 12.94 | 0.2812 | 2.0 |
| doge-15m | 3 | 3 | 0 | 5 | 0.6667 | 0.589 | 0.6 | 2 | 6.0 | 16.0 | 2.44 | 0.1526 | 6.0 |
| doge-5m | 15 | 15 | 0 | 22 | 0.6667 | 0.6208 | 0.6818 | 2 | 6.0 | 62.0 | 14.69 | 0.237 | 9.19 |
| eth-15m | 6 | 6 | 0 | 10 | 0.5 | 0.5293 | 0.7 | 2 | 6.0 | 30.0 | 15.24 | 0.5079 | 2.0 |
| eth-5m | 17 | 16 | 1 | 27 | 0.625 | 0.5692 | 0.5926 | 6 | 14.0 | 106.0 | -6.07 | -0.0573 | 42.0 |
| hype-15m | 5 | 5 | 0 | 9 | 0.6 | 0.5644 | 0.5556 | 3 | 8.0 | 32.0 | -4.85 | -0.1516 | 12.0 |
| hype-1h | 2 | 2 | 0 | 4 | 0.5 | 0.9604 | 0.5 | 2 | 6.0 | 14.0 | -5.76 | -0.4111 | 6.0 |
| hype-5m | 19 | 19 | 0 | 33 | 0.6842 | 0.8124 | 0.5758 | 4 | 10.0 | 124.0 | -45.15 | -0.3641 | 53.49 |
| sol-15m | 5 | 5 | 0 | 7 | 0.6 | 0.5404 | 0.7143 | 1 | 4.0 | 18.0 | 6.5 | 0.361 | 2.0 |
| sol-5m | 16 | 16 | 0 | 24 | 0.6875 | 0.5714 | 0.6667 | 3 | 8.0 | 72.0 | 16.96 | 0.2356 | 12.14 |
| xrp-15m | 6 | 6 | 0 | 12 | 0.3333 | 0.5591 | 0.5833 | 2 | 6.0 | 40.0 | 15.11 | 0.3778 | 6.0 |
| xrp-5m | 17 | 17 | 0 | 30 | 0.7059 | 0.6485 | 0.5667 | 4 | 10.0 | 112.0 | -6.91 | -0.0617 | 23.29 |
| zec-15m | 6 | 6 | 0 | 13 | 0.5 | 0.9323 | 0.4615 | 3 | 8.0 | 52.0 | -24.18 | -0.4649 | 24.9 |
| zec-5m | 15 | 15 | 0 | 35 | 0.3333 | 0.9326 | 0.4286 | 5 | 12.0 | 150.0 | -74.78 | -0.4986 | 75.21 |
| TOTAL | 181 | 172 | 9 | 298 | 0.5805 | 0.6586 | 0.5872 | 6 | 14.0 | 1030.0 | -72.88 | -0.0708 | 110.94 |

#### A_2026-11-27_primary  (peak simultaneous exposure $100.0; skips {'unresolved': 0, 'no_fill': 0, 'waited': 0}; book_stats {'bets_attempted': 320, 'wide_gt_10c': 177, 'empty_or_no_fill': 0, 'bba_stale_gt_60s': 6})

| series | triggers | completed | open | bets | first_win | first_price | all_win | longest_loss_run | max_stake | staked | pnl | roi_staked | max_dd |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| bnb-15m | 4 | 4 | 0 | 13 | 0.0 | 0.7867 | 0.3077 | 5 | 12.0 | 66.0 | -35.11 | -0.532 | 37.07 |
| bnb-1h | 2 | 2 | 0 | 5 | 0.5 | 0.5175 | 0.4 | 3 | 8.0 | 22.0 | -3.44 | -0.1562 | 12.0 |
| bnb-5m | 15 | 15 | 0 | 24 | 0.5333 | 0.5569 | 0.625 | 2 | 6.0 | 70.0 | 12.86 | 0.1837 | 6.0 |
| btc-15m | 4 | 4 | 0 | 7 | 0.5 | 0.5249 | 0.5714 | 2 | 6.0 | 22.0 | 2.11 | 0.0957 | 6.0 |
| btc-1h | 1 | 1 | 0 | 4 | 0.0 | 0.5325 | 0.25 | 3 | 8.0 | 20.0 | -5.25 | -0.2627 | 12.0 |
| btc-5m | 16 | 16 | 0 | 19 | 0.8125 | 0.5044 | 0.8421 | 1 | 4.0 | 44.0 | 30.48 | 0.6927 | 2.0 |
| doge-15m | 7 | 6 | 1 | 11 | 0.3333 | 0.6321 | 0.5455 | 2 | 6.0 | 34.0 | 0.7 | 0.0206 | 6.46 |
| doge-1h | 2 | 1 | 1 | 5 | 0.0 | 0.6223 | 0.2 | 3 | 6.0 | 18.0 | -11.47 | -0.6371 | 12.0 |
| doge-5m | 18 | 18 | 0 | 28 | 0.5 | 0.5463 | 0.6429 | 2 | 6.0 | 78.0 | 17.35 | 0.2224 | 6.0 |
| eth-15m | 4 | 4 | 0 | 7 | 0.5 | 0.5225 | 0.5714 | 2 | 6.0 | 22.0 | 3.56 | 0.1616 | 6.0 |
| eth-1h | 1 | 1 | 0 | 4 | 0.0 | 0.5325 | 0.25 | 3 | 8.0 | 20.0 | -4.98 | -0.2488 | 12.0 |
| eth-5m | 15 | 15 | 0 | 21 | 0.6 | 0.529 | 0.7143 | 1 | 4.0 | 54.0 | 25.49 | 0.472 | 2.0 |
| hype-15m | 6 | 6 | 0 | 11 | 0.6667 | 0.6654 | 0.5455 | 3 | 8.0 | 40.0 | -7.49 | -0.1872 | 15.65 |
| hype-5m | 18 | 18 | 0 | 33 | 0.5556 | 0.6745 | 0.5455 | 5 | 12.0 | 122.0 | -30.82 | -0.2526 | 45.81 |
| sol-15m | 4 | 4 | 0 | 7 | 0.5 | 0.515 | 0.5714 | 2 | 6.0 | 22.0 | 3.12 | 0.142 | 6.0 |
| sol-1h | 2 | 2 | 0 | 5 | 0.5 | 0.5425 | 0.4 | 3 | 8.0 | 22.0 | -10.08 | -0.4584 | 12.0 |
| sol-5m | 13 | 13 | 0 | 20 | 0.6154 | 0.5261 | 0.7 | 2 | 6.0 | 56.0 | 31.13 | 0.5559 | 6.0 |
| xrp-15m | 7 | 7 | 0 | 14 | 0.4286 | 0.5053 | 0.5 | 2 | 6.0 | 48.0 | 0.85 | 0.0177 | 8.52 |
| xrp-1h | 2 | 2 | 0 | 3 | 0.5 | 0.6123 | 0.6667 | 1 | 4.0 | 8.0 | 2.18 | 0.2724 | 2.0 |
| xrp-5m | 17 | 17 | 0 | 29 | 0.4706 | 0.5047 | 0.5517 | 2 | 6.0 | 90.0 | 14.9 | 0.1656 | 6.0 |
| zec-15m | 7 | 7 | 0 | 14 | 0.5714 | 0.9349 | 0.5 | 4 | 10.0 | 56.0 | -26.12 | -0.4664 | 26.5 |
| zec-5m | 17 | 17 | 0 | 36 | 0.4118 | 0.8862 | 0.4722 | 5 | 12.0 | 142.0 | -61.94 | -0.4362 | 65.71 |
| TOTAL | 183 | 180 | 3 | 320 | 0.5193 | 0.6063 | 0.5625 | 5 | 12.0 | 1076.0 | -51.97 | -0.0483 | 82.2 |

#### A_2026-11-26_primary  (peak simultaneous exposure $160.0; skips {'unresolved': 0, 'no_fill': 0, 'waited': 0}; book_stats {'bets_attempted': 710, 'wide_gt_10c': 337, 'empty_or_no_fill': 0, 'bba_stale_gt_60s': 9})

| series | triggers | completed | open | bets | first_win | first_price | all_win | longest_loss_run | max_stake | staked | pnl | roi_staked | max_dd |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| bnb-15m | 11 | 11 | 0 | 22 | 0.4545 | 0.6322 | 0.5 | 4 | 10.0 | 82.0 | -16.32 | -0.199 | 21.77 |
| bnb-1h | 3 | 3 | 0 | 8 | 0.3333 | 0.7184 | 0.375 | 3 | 8.0 | 34.0 | -6.17 | -0.1814 | 12.0 |
| bnb-4h | 1 | 1 | 0 | 1 | 1.0 | 0.5325 | 1.0 | 0 | 2.0 | 2.0 | 1.76 | 0.878 | 0.0 |
| bnb-5m | 32 | 32 | 0 | 65 | 0.625 | 0.5961 | 0.5077 | 10 | 22.0 | 334.0 | -117.47 | -0.3517 | 134.83 |
| btc-15m | 12 | 12 | 0 | 20 | 0.5833 | 0.5307 | 0.6 | 2 | 6.0 | 62.0 | 14.12 | 0.2277 | 6.0 |
| btc-1h | 3 | 3 | 0 | 4 | 0.6667 | 0.5258 | 0.75 | 1 | 4.0 | 10.0 | 5.46 | 0.546 | 2.0 |
| btc-4h | 1 | 1 | 0 | 1 | 1.0 | 0.5325 | 1.0 | 0 | 2.0 | 2.0 | 1.76 | 0.878 | 0.0 |
| btc-5m | 33 | 32 | 1 | 64 | 0.5625 | 0.5834 | 0.5 | 5 | 12.0 | 258.0 | -28.95 | -0.1122 | 47.83 |
| doge-15m | 11 | 11 | 0 | 19 | 0.5455 | 0.5969 | 0.5789 | 3 | 8.0 | 62.0 | -3.04 | -0.0491 | 14.33 |
| doge-1h | 3 | 3 | 0 | 5 | 0.3333 | 0.7649 | 0.6 | 1 | 4.0 | 14.0 | -2.54 | -0.1815 | 2.66 |
| doge-5m | 32 | 32 | 0 | 62 | 0.4688 | 0.6041 | 0.5 | 5 | 12.0 | 226.0 | -22.57 | -0.0999 | 41.54 |
| eth-15m | 10 | 10 | 0 | 17 | 0.5 | 0.5345 | 0.5882 | 2 | 6.0 | 52.0 | 10.32 | 0.1985 | 6.0 |
| eth-1h | 2 | 2 | 0 | 3 | 0.5 | 0.5375 | 0.6667 | 1 | 4.0 | 8.0 | 4.05 | 0.5058 | 2.0 |
| eth-5m | 30 | 30 | 0 | 59 | 0.5667 | 0.5443 | 0.5254 | 6 | 14.0 | 234.0 | -0.61 | -0.0026 | 42.0 |
| hype-15m | 12 | 12 | 0 | 21 | 0.75 | 0.7475 | 0.5714 | 4 | 10.0 | 80.0 | -10.53 | -0.1317 | 26.16 |
| hype-1h | 2 | 2 | 0 | 2 | 1.0 | 0.9703 | 1.0 | 0 | 2.0 | 4.0 | 0.12 | 0.0306 | 0.0 |
| hype-4h | 1 | 1 | 0 | 1 | 1.0 | 0.9703 | 1.0 | 0 | 2.0 | 2.0 | 0.06 | 0.0306 | 0.0 |
| hype-5m | 29 | 29 | 0 | 60 | 0.5172 | 0.8245 | 0.4833 | 7 | 16.0 | 262.0 | -104.8 | -0.4 | 118.61 |
| sol-15m | 13 | 13 | 0 | 14 | 0.9231 | 0.5429 | 0.9286 | 1 | 4.0 | 30.0 | 21.72 | 0.7241 | 2.0 |
| sol-1h | 3 | 3 | 0 | 3 | 1.0 | 0.5758 | 1.0 | 0 | 2.0 | 6.0 | 4.48 | 0.7459 | 0.0 |
| sol-5m | 38 | 38 | 0 | 69 | 0.5789 | 0.5609 | 0.5507 | 6 | 14.0 | 254.0 | 4.16 | 0.0164 | 42.0 |
| xrp-15m | 11 | 11 | 0 | 19 | 0.5455 | 0.5016 | 0.5789 | 3 | 8.0 | 62.0 | 11.02 | 0.1778 | 12.0 |
| xrp-1h | 2 | 2 | 0 | 5 | 0.5 | 0.5624 | 0.4 | 3 | 8.0 | 22.0 | -2.44 | -0.1107 | 12.0 |
| xrp-4h | 1 | 1 | 0 | 1 | 1.0 | 0.5725 | 1.0 | 0 | 2.0 | 2.0 | 1.49 | 0.7469 | 0.0 |
| xrp-5m | 34 | 34 | 0 | 67 | 0.5294 | 0.5573 | 0.5075 | 6 | 14.0 | 268.0 | -24.01 | -0.0896 | 56.36 |
| zec-15m | 14 | 14 | 0 | 24 | 0.6429 | 0.9363 | 0.5833 | 3 | 8.0 | 80.0 | -28.46 | -0.3558 | 29.34 |
| zec-4h | 1 | 1 | 0 | 2 | 0.0 | 0.9703 | 0.5 | 1 | 4.0 | 6.0 | -1.88 | -0.3129 | 2.0 |
| zec-5m | 36 | 36 | 0 | 72 | 0.4722 | 0.932 | 0.5 | 7 | 16.0 | 296.0 | -141.22 | -0.4771 | 141.52 |
| TOTAL | 381 | 380 | 1 | 710 | 0.5684 | 0.6487 | 0.5366 | 10 | 22.0 | 2754.0 | -430.48 | -0.1563 | 469.87 |

#### A_2026-11-24_secondary  (peak simultaneous exposure $68.0; skips {'unresolved': 0, 'no_fill': 0, 'waited': 316}; book_stats {'bets_attempted': 248, 'wide_gt_10c': 156, 'empty_or_no_fill': 0, 'bba_stale_gt_60s': 1})

| series | triggers | completed | open | bets | first_win | first_price | all_win | longest_loss_run | max_stake | staked | pnl | roi_staked | max_dd |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| bnb-15m | 6 | 5 | 1 | 9 | 0.6 | 0.676 | 0.5556 | 3 | 8.0 | 32.0 | -5.37 | -0.1679 | 12.0 |
| bnb-5m | 15 | 15 | 0 | 22 | 0.6667 | 0.6514 | 0.6818 | 2 | 6.0 | 62.0 | 0.4 | 0.0065 | 6.0 |
| btc-15m | 5 | 5 | 0 | 10 | 0.4 | 0.5543 | 0.5 | 3 | 8.0 | 36.0 | 4.29 | 0.1191 | 12.0 |
| btc-5m | 12 | 11 | 1 | 18 | 0.8182 | 0.5459 | 0.6111 | 4 | 10.0 | 68.0 | 0.31 | 0.0045 | 20.83 |
| doge-15m | 3 | 3 | 0 | 7 | 0.3333 | 0.6323 | 0.4286 | 3 | 8.0 | 28.0 | -5.54 | -0.1977 | 12.0 |
| doge-5m | 15 | 15 | 0 | 21 | 0.6667 | 0.5674 | 0.7143 | 2 | 6.0 | 56.0 | 19.66 | 0.351 | 6.61 |
| eth-15m | 5 | 5 | 0 | 9 | 0.6 | 0.5525 | 0.5556 | 3 | 8.0 | 32.0 | 0.94 | 0.0293 | 12.0 |
| eth-5m | 12 | 11 | 1 | 23 | 0.4545 | 0.5287 | 0.4783 | 4 | 10.0 | 88.0 | 12.21 | 0.1388 | 20.0 |
| hype-15m | 4 | 4 | 0 | 10 | 0.5 | 0.7618 | 0.4 | 5 | 12.0 | 52.0 | -24.66 | -0.4743 | 30.36 |
| hype-1h | 1 | 1 | 0 | 4 | 0.0 | 0.9703 | 0.25 | 3 | 8.0 | 20.0 | -11.76 | -0.5878 | 12.0 |
| hype-5m | 14 | 14 | 0 | 25 | 0.5714 | 0.8135 | 0.56 | 5 | 12.0 | 94.0 | -35.28 | -0.3753 | 37.56 |
| sol-15m | 5 | 5 | 0 | 7 | 0.6 | 0.5864 | 0.7143 | 1 | 4.0 | 18.0 | 6.82 | 0.379 | 2.0 |
| sol-5m | 11 | 11 | 0 | 15 | 0.6364 | 0.6133 | 0.7333 | 1 | 4.0 | 38.0 | 17.35 | 0.4567 | 2.0 |
| xrp-15m | 5 | 5 | 0 | 9 | 0.6 | 0.5267 | 0.5556 | 3 | 8.0 | 32.0 | 2.1 | 0.0657 | 12.0 |
| xrp-5m | 15 | 15 | 0 | 24 | 0.6667 | 0.589 | 0.625 | 3 | 8.0 | 76.0 | 7.77 | 0.1022 | 12.0 |
| zec-15m | 6 | 6 | 0 | 8 | 0.6667 | 0.9373 | 0.75 | 1 | 4.0 | 20.0 | -2.9 | -0.1449 | 3.49 |
| zec-5m | 12 | 12 | 0 | 27 | 0.5 | 0.9307 | 0.4444 | 6 | 14.0 | 124.0 | -65.98 | -0.5321 | 66.42 |
| TOTAL | 152 | 143 | 9 | 248 | 0.6014 | 0.6591 | 0.5766 | 6 | 14.0 | 876.0 | -79.63 | -0.0909 | 115.16 |

#### A_2026-11-27_secondary  (peak simultaneous exposure $70.0; skips {'unresolved': 0, 'no_fill': 0, 'waited': 333}; book_stats {'bets_attempted': 256, 'wide_gt_10c': 144, 'empty_or_no_fill': 0, 'bba_stale_gt_60s': 7})

| series | triggers | completed | open | bets | first_win | first_price | all_win | longest_loss_run | max_stake | staked | pnl | roi_staked | max_dd |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| bnb-15m | 3 | 3 | 0 | 6 | 0.3333 | 0.6554 | 0.5 | 2 | 6.0 | 20.0 | -5.47 | -0.2733 | 7.01 |
| bnb-1h | 2 | 1 | 1 | 2 | 0.0 | 0.4925 | 0.5 | 1 | 4.0 | 6.0 | 1.31 | 0.2178 | 2.0 |
| bnb-5m | 13 | 13 | 0 | 22 | 0.5385 | 0.6053 | 0.5909 | 2 | 6.0 | 68.0 | 5.02 | 0.0739 | 8.48 |
| btc-15m | 3 | 3 | 0 | 6 | 0.3333 | 0.5024 | 0.5 | 2 | 6.0 | 20.0 | 3.4 | 0.1701 | 6.0 |
| btc-1h | 1 | 1 | 0 | 2 | 0.0 | 0.5125 | 0.5 | 1 | 4.0 | 6.0 | 1.37 | 0.2289 | 2.0 |
| btc-5m | 13 | 12 | 1 | 19 | 0.6667 | 0.5309 | 0.6316 | 3 | 8.0 | 60.0 | 5.78 | 0.0964 | 12.0 |
| doge-15m | 6 | 5 | 1 | 10 | 0.6 | 0.6064 | 0.5 | 4 | 10.0 | 42.0 | -14.03 | -0.334 | 20.0 |
| doge-1h | 2 | 1 | 1 | 2 | 0.5 | 0.5824 | 0.5 | 1 | 2.0 | 4.0 | -0.73 | -0.1835 | 2.0 |
| doge-5m | 14 | 14 | 0 | 28 | 0.4286 | 0.5934 | 0.5 | 4 | 10.0 | 104.0 | 0.19 | 0.0018 | 20.0 |
| eth-15m | 3 | 3 | 0 | 10 | 0.0 | 0.5291 | 0.3 | 3 | 8.0 | 44.0 | -3.63 | -0.0826 | 12.0 |
| eth-1h | 1 | 1 | 0 | 2 | 0.0 | 0.5325 | 0.5 | 1 | 4.0 | 6.0 | 1.51 | 0.252 | 2.0 |
| eth-5m | 11 | 10 | 1 | 14 | 0.6 | 0.5593 | 0.7143 | 1 | 4.0 | 36.0 | 19.13 | 0.5313 | 2.0 |
| hype-15m | 4 | 4 | 0 | 8 | 0.25 | 0.6049 | 0.5 | 2 | 6.0 | 26.0 | 3.78 | 0.1453 | 6.0 |
| hype-5m | 13 | 12 | 1 | 21 | 0.4615 | 0.7709 | 0.5714 | 3 | 8.0 | 64.0 | -7.15 | -0.1117 | 12.0 |
| sol-15m | 4 | 3 | 1 | 4 | 0.6667 | 0.5658 | 0.75 | 1 | 4.0 | 10.0 | 4.03 | 0.403 | 2.0 |
| sol-1h | 2 | 1 | 1 | 2 | 0.0 | 0.5025 | 0.5 | 1 | 4.0 | 6.0 | 1.31 | 0.2188 | 2.0 |
| sol-5m | 11 | 11 | 0 | 16 | 0.6364 | 0.5387 | 0.6875 | 2 | 6.0 | 44.0 | 14.81 | 0.3366 | 6.0 |
| xrp-15m | 5 | 5 | 0 | 14 | 0.2 | 0.5544 | 0.3571 | 3 | 8.0 | 58.0 | -3.13 | -0.0539 | 12.14 |
| xrp-1h | 2 | 1 | 1 | 2 | 0.5 | 0.5924 | 0.5 | 1 | 2.0 | 4.0 | -0.31 | -0.0783 | 2.0 |
| xrp-5m | 9 | 9 | 0 | 23 | 0.3333 | 0.4827 | 0.3913 | 4 | 10.0 | 100.0 | -16.13 | -0.1613 | 20.73 |
| zec-15m | 3 | 3 | 0 | 16 | 0.3333 | 0.9373 | 0.1875 | 11 | 24.0 | 170.0 | -135.98 | -0.7999 | 136.48 |
| zec-5m | 9 | 9 | 0 | 27 | 0.1111 | 0.9008 | 0.3333 | 5 | 12.0 | 130.0 | -62.25 | -0.4788 | 62.63 |
| TOTAL | 135 | 125 | 10 | 256 | 0.4375 | 0.615 | 0.4883 | 11 | 24.0 | 1028.0 | -187.17 | -0.1821 | 191.54 |

#### A_2026-11-26_secondary  (peak simultaneous exposure $60.0; skips {'unresolved': 0, 'no_fill': 0, 'waited': 732}; book_stats {'bets_attempted': 576, 'wide_gt_10c': 313, 'empty_or_no_fill': 0, 'bba_stale_gt_60s': 5})

| series | triggers | completed | open | bets | first_win | first_price | all_win | longest_loss_run | max_stake | staked | pnl | roi_staked | max_dd |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| bnb-15m | 9 | 9 | 0 | 21 | 0.5556 | 0.6855 | 0.4286 | 5 | 12.0 | 100.0 | -35.65 | -0.3565 | 41.24 |
| bnb-1h | 3 | 3 | 0 | 5 | 0.3333 | 0.5658 | 0.6 | 1 | 4.0 | 14.0 | 3.82 | 0.2729 | 2.0 |
| bnb-4h | 1 | 1 | 0 | 1 | 1.0 | 0.5425 | 1.0 | 0 | 2.0 | 2.0 | 1.69 | 0.8434 | 0.0 |
| bnb-5m | 25 | 25 | 0 | 53 | 0.6 | 0.5983 | 0.4717 | 5 | 12.0 | 230.0 | -54.95 | -0.2389 | 70.69 |
| btc-15m | 10 | 10 | 0 | 20 | 0.5 | 0.5603 | 0.5 | 3 | 8.0 | 74.0 | 0.51 | 0.0069 | 12.52 |
| btc-1h | 3 | 3 | 0 | 3 | 1.0 | 0.5025 | 1.0 | 0 | 2.0 | 6.0 | 5.97 | 0.9948 | 0.0 |
| btc-4h | 1 | 0 | 1 | 1 | 0.0 | 0.5025 | 0.0 | 1 | 2.0 | 2.0 | -2.0 | -1.0 | 2.0 |
| btc-5m | 29 | 29 | 0 | 46 | 0.5862 | 0.5343 | 0.6304 | 3 | 8.0 | 138.0 | 42.12 | 0.3052 | 12.0 |
| doge-15m | 10 | 10 | 0 | 18 | 0.6 | 0.5944 | 0.5556 | 4 | 10.0 | 66.0 | -8.89 | -0.1347 | 20.0 |
| doge-1h | 3 | 3 | 0 | 5 | 0.6667 | 0.7516 | 0.6 | 2 | 6.0 | 16.0 | -2.18 | -0.1362 | 6.0 |
| doge-5m | 27 | 27 | 0 | 48 | 0.5185 | 0.5933 | 0.5625 | 7 | 16.0 | 184.0 | -28.68 | -0.1559 | 56.0 |
| eth-15m | 9 | 8 | 1 | 16 | 0.5556 | 0.5724 | 0.5 | 3 | 8.0 | 56.0 | 0.7 | 0.0126 | 12.0 |
| eth-1h | 2 | 2 | 0 | 3 | 0.5 | 0.5275 | 0.6667 | 1 | 4.0 | 8.0 | 3.01 | 0.3768 | 2.0 |
| eth-5m | 23 | 23 | 0 | 44 | 0.5652 | 0.5571 | 0.5227 | 4 | 10.0 | 164.0 | 6.34 | 0.0387 | 26.18 |
| hype-15m | 9 | 9 | 0 | 18 | 0.4444 | 0.6688 | 0.5 | 3 | 8.0 | 64.0 | -3.08 | -0.0481 | 13.51 |
| hype-1h | 2 | 2 | 0 | 2 | 1.0 | 0.9703 | 1.0 | 0 | 2.0 | 4.0 | 0.12 | 0.0306 | 0.0 |
| hype-4h | 1 | 0 | 1 | 1 | 0.0 | 0.9207 | 0.0 | 1 | 2.0 | 2.0 | -2.0 | -1.0 | 2.0 |
| hype-5m | 24 | 24 | 0 | 48 | 0.4167 | 0.7832 | 0.5 | 6 | 14.0 | 186.0 | -55.69 | -0.2994 | 62.7 |
| sol-15m | 9 | 9 | 0 | 18 | 0.4444 | 0.5544 | 0.5 | 2 | 6.0 | 62.0 | 2.47 | 0.0399 | 7.87 |
| sol-1h | 2 | 2 | 0 | 5 | 0.0 | 0.5075 | 0.4 | 2 | 6.0 | 18.0 | 1.95 | 0.1083 | 6.0 |
| sol-5m | 27 | 27 | 0 | 52 | 0.5926 | 0.5505 | 0.5192 | 4 | 10.0 | 202.0 | -17.73 | -0.0878 | 35.97 |
| xrp-15m | 9 | 9 | 0 | 18 | 0.5556 | 0.5225 | 0.5 | 5 | 12.0 | 76.0 | -10.57 | -0.1391 | 30.0 |
| xrp-1h | 2 | 2 | 0 | 3 | 0.5 | 0.7863 | 0.6667 | 1 | 4.0 | 8.0 | 2.18 | 0.2729 | 2.0 |
| xrp-4h | 1 | 0 | 1 | 1 | 0.0 | 0.5025 | 0.0 | 1 | 2.0 | 2.0 | -2.0 | -1.0 | 2.0 |
| xrp-5m | 27 | 27 | 0 | 47 | 0.5556 | 0.5095 | 0.5745 | 4 | 10.0 | 158.0 | 13.4 | 0.0848 | 25.7 |
| zec-15m | 9 | 8 | 1 | 21 | 0.3333 | 0.934 | 0.381 | 3 | 8.0 | 80.0 | -41.57 | -0.5196 | 41.72 |
| zec-4h | 1 | 1 | 0 | 1 | 1.0 | 0.9703 | 1.0 | 0 | 2.0 | 2.0 | 0.06 | 0.0306 | 0.0 |
| zec-5m | 31 | 30 | 1 | 57 | 0.5 | 0.8922 | 0.5263 | 5 | 12.0 | 208.0 | -79.38 | -0.3816 | 80.91 |
| TOTAL | 309 | 303 | 6 | 576 | 0.5325 | 0.6329 | 0.526 | 7 | 16.0 | 2132.0 | -260.01 | -0.122 | 283.87 |

## Variants: recovery sizing, price filter, skip policies (follow-up)

Pre-declared, nothing tuned; same trigger and PRIMARY timing (Chainlink-inferred outcomes, next window at its open),
same days (A: 11-24, 11-27, 11-26 = the same single validation look; B: outcome only at 0.50 + fee). Code:
`research/streak_ladder/variants.py` (table: `vtable.py`). Variants tried in this follow-up: 5 (rec, f1, f5, rec_f1,
coin_f1) x skip policies A/B/C for the three filtered ones.

- **rec** (user's recovery sizing): first bet $2; after losses totalling L in the ladder the next bet has
  shares = (L + 2) / (1 - c_eff), c_eff = average entry price + fee per share walked through the visible ask depth (if
  the depth is short, take what is there and record the shortfall). No cap. `bankroll needed` = minus the lowest point
  of the shared-cash path (every stake out at entry, payout in at resolution recv, all series together, start 0).
  `bust at $5k` = first time that path goes below -$5,000.
- **f1**: the +$2 ladder, but a bet (first or continuation) is placed only if the opposite token's ask <= 0.55 and the
  spread <= 3c at entry. **f5**: same filter, entering at open + 5 s instead of + 1 s. **rec_f1**: filter + recovery
  sizing. **coin_f1**: negative control, a seeded coin-flip side per window, +$2 ladder, f1 filter, policy A.
- **Skip policies** (for a window skipped by the filter or an empty book): **A keep waiting** (skip counts as neither
  win nor loss; stake and direction kept, next window tried); **B shadow** (the skipped window is tracked as if bet
  without money: if its outcome is our side the ladder ends with no P&L; if it is the streak side the stake level
  advances as if lost, and for recovery sizing L grows by the shadow stake from the book as of that moment); **C abandon**
  (a skip ends the ladder, wait for a fresh 3-in-a-row). B/C are for the filtered variants only: the outcome-only B
  evaluation has no book to filter, so its rows are the unfiltered rec, plus2 and a coin flip.
- "skips" counts window attempts rejected by the filter (large: most windows open wide or high). Under shadow policy
  `max ladder loss` includes shadow losses (not real money). `ladders` is larger for C because every skip restarts the wait.

Result table (P&L in $; `A <day>` rows are books, `B` is outcome-only; the "policy" column of the B rows is not used):

| variant | policy | day | ladders | bets | skips | first-bet win @ price | P&L | on staked | max stake | max ladder loss | worst ladder | longest loss run | peak exposure | bankroll needed | bust at $5k | depth shortfall (sh) |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| f1 | A | A 2026-11-24 | 76 | 107 | 1010 | 48.3% @ 0.486 | +58 | +15.7% | $14 | $42 | $-22 | 6 | $30 | $18 | no | 0.0 |
| f1 | A | A 2026-11-27 | 82 | 114 | 814 | 50.0% @ 0.46 | +127 | +34.0% | $10 | $20 | $-12 | 4 | $32 | $8 | no | 0.0 |
| f1 | A | A 2026-11-26 | 169 | 286 | 1919 | 53.2% @ 0.473 | +151 | +14.3% | $14 | $42 | $-28 | 6 | $48 | $40 | no | 0.0 |
| f1 | B | A 2026-11-24 | 181 | 49 | 249 | 48.7% @ 0.491 | +55 | +35.5% | $12 | $42 | $-16 | 6 | $24 | $27 | no | 0.0 |
| f1 | B | A 2026-11-27 | 183 | 74 | 246 | 53.4% @ 0.457 | +65 | +31.4% | $8 | $30 | $-6 | 5 | $34 | $8 | no | 0.0 |
| f1 | B | A 2026-11-26 | 381 | 158 | 552 | 52.4% @ 0.484 | -6 | -0.9% | $14 | $110 | $-24 | 10 | $76 | $93 | no | 0.0 |
| f1 | C | A 2026-11-24 | 198 | 43 | 167 | 42.9% @ 0.487 | +26 | +25.8% | $4 | $2 | $-2 | 1 | $14 | $10 | no | 0.0 |
| f1 | C | A 2026-11-27 | 192 | 67 | 153 | 51.9% @ 0.456 | +45 | +25.9% | $8 | $12 | $-6 | 3 | $22 | $8 | no | 0.0 |
| f1 | C | A 2026-11-26 | 427 | 109 | 366 | 56.6% @ 0.485 | +23 | +7.9% | $10 | $20 | $-12 | 4 | $20 | $18 | no | 0.0 |
| f5 | A | A 2026-11-24 | 101 | 169 | 722 | 46.1% @ 0.473 | +87 | +14.4% | $14 | $42 | $-30 | 6 | $44 | $22 | no | 0.0 |
| f5 | A | A 2026-11-27 | 112 | 194 | 535 | 51.9% @ 0.44 | +137 | +19.6% | $12 | $30 | $-30 | 5 | $66 | $52 | no | 0.0 |
| f5 | A | A 2026-11-26 | 213 | 427 | 1439 | 46.0% @ 0.455 | +102 | +5.8% | $20 | $90 | $-55 | 9 | $72 | $117 | no | 0.0 |
| f5 | B | A 2026-11-24 | 181 | 74 | 224 | 54.4% @ 0.484 | +86 | +36.7% | $12 | $42 | $-18 | 6 | $28 | $28 | no | 0.0 |
| f5 | B | A 2026-11-27 | 183 | 106 | 214 | 52.6% @ 0.446 | +77 | +24.7% | $8 | $30 | $-8 | 5 | $58 | $6 | no | 0.0 |
| f5 | B | A 2026-11-26 | 381 | 225 | 485 | 48.6% @ 0.457 | -2 | -0.2% | $16 | $110 | $-30 | 10 | $116 | $156 | no | 0.0 |
| f5 | C | A 2026-11-24 | 196 | 63 | 152 | 49.0% @ 0.478 | +53 | +34.9% | $6 | $6 | $-6 | 2 | $18 | $9 | no | 0.0 |
| f5 | C | A 2026-11-27 | 192 | 93 | 140 | 50.7% @ 0.446 | +53 | +21.2% | $8 | $12 | $-6 | 3 | $42 | $12 | no | 0.0 |
| f5 | C | A 2026-11-26 | 416 | 177 | 328 | 47.2% @ 0.456 | +9 | +1.8% | $12 | $42 | $-42 | 6 | $44 | $59 | no | 0.0 |
| rec_f1 | A | A 2026-11-24 | 76 | 107 | 1010 | 48.3% @ 0.486 | +100 | +4.4% | $1610 | $303 | $-14 | 6 | $1624 | $1835 | no | 0.0 |
| rec_f1 | A | A 2026-11-27 | 82 | 114 | 814 | 50.0% @ 0.46 | +142 | +32.5% | $33 | $32 | $-2 | 4 | $67 | $16 | no | 0.0 |
| rec_f1 | A | A 2026-11-26 | 169 | 286 | 1919 | 53.2% @ 0.473 | +320 | +19.7% | $124 | $120 | $-7 | 6 | $199 | $164 | no | 0.0 |
| rec_f1 | B | A 2026-11-24 | 181 | 49 | 249 | 48.7% @ 0.492 | +7 | +1.5% | $169 | $4291 | $-173 | 6 | $177 | $270 | no | 0.0 |
| rec_f1 | B | A 2026-11-27 | 183 | 74 | 246 | 53.4% @ 0.456 | +89 | +38.2% | $20 | $17148 | $-5 | 5 | $60 | $22 | no | 0.0 |
| rec_f1 | B | A 2026-11-26 | 381 | 158 | 552 | 52.4% @ 0.486 | +615 | +14.0% | $3238 | $212929 | $-45 | 10 | $3451 | $3452 | no | 0.0 |
| rec_f1 | C | A 2026-11-24 | 198 | 43 | 167 | 42.9% @ 0.487 | +25 | +24.4% | $5 | $2 | $-2 | 1 | $14 | $10 | no | 0.0 |
| rec_f1 | C | A 2026-11-27 | 192 | 67 | 153 | 51.9% @ 0.456 | +54 | +28.9% | $19 | $14 | $-5 | 3 | $35 | $16 | no | 0.0 |
| rec_f1 | C | A 2026-11-26 | 427 | 109 | 366 | 56.6% @ 0.485 | +51 | +14.1% | $44 | $36 | $-7 | 4 | $66 | $48 | no | 0.0 |
| rec | A | A 2026-11-24 | 181 | 298 | 0 | 58.0% @ 0.659 | -10105 | -15.9% | $16565 | $4291 | $-3802 | 6 | $21010 | $27240 | 15:20 UTC | 543877.18 |
| rec | A | A 2026-11-27 | 183 | 320 | 0 | 51.9% @ 0.606 | -19162 | -24.1% | $36905 | $17148 | $-16646 | 5 | $37639 | $40206 | 10:15 UTC | 827170.13 |
| rec | A | A 2026-11-26 | 381 | 710 | 0 | 56.8% @ 0.649 | -323363 | -67.1% | $45308 | $212929 | $-211518 | 10 | $125601 | $348399 | 06:05 UTC | 31170436.57 |
| coin_f1 | A | A 2026-11-24 | 75 | 134 | 1011 | 44.1% @ 0.467 | -186 | -27.1% | $26 | $156 | $-122 | 12 | $58 | $251 | no | 0.0 |
| coin_f1 | A | A 2026-11-27 | 76 | 130 | 851 | 42.9% @ 0.454 | +36 | +6.9% | $14 | $42 | $-31 | 6 | $42 | $27 | no | 0.0 |
| coin_f1 | A | A 2026-11-26 | 166 | 320 | 1890 | 46.4% @ 0.456 | -111 | -7.7% | $30 | $210 | $-181 | 14 | $84 | $252 | no | 0.0 |
| rec | A | B | 1044 | 1985 | 0 | 53.0% @ 0.504 | +2082 | +10.6% | $2164 | $2140 | $-14 | 10 | $3403 | $3213 | no | 0.0 |
| plus2 | A | B | 1044 | 1985 | 0 | 53.0% @ 0.504 | +303 | +4.0% | $22 | $110 | $-84 | 10 | $196 | $278 | no | 0.0 |
| coin | A | B | 1049 | 2066 | 0 | 51.4% @ 0.504 | +6 | +0.1% | $22 | $110 | $-88 | 10 | $114 | $161 | no | 0.0 |

Reading (A = books):
- **Recovery sizing, unfiltered (rec): not survivable.** The unfiltered book entries cost 0.61-0.66 (about 0.75 in the
  wide books), so winning back L + $2 needs stakes of $16k / $37k / $45k from a $2 base on three ordinary days, far beyond
  the visible depth (shortfall column), and a $5,000 bankroll would have gone bust on all three days (15:20, 10:15 and
  06:05 UTC). The P&L (-$10k / -$19k / -$323k) is itself optimistic because the fills ignore the depth shortfall. At the
  0.50 stand-in (B) the same sizing makes +$2,082 (+10.6%) but needs $3,213 of bankroll and a $2,164 stake: the tail is
  the cost.
- **Filter (f1, policy A): positive on all three book days**: +$58 (+15.7%, 107 bets), +$127 (+34.0%, 114), +$151
  (+14.3%, 286, validation), max stake $10-14, bankroll needed $8-$40. But the first-bet win rate is only 48-53% at a
  price of 0.46-0.49 (pooled first bets, n 327: 51.3% vs 47.3%, +3.9c, standard error about 2.8c, 1.4 sigma), and the
  coin-flip control with the identical filter, timing and ladder gives -$186 / +$36 / -$111 (-27% / +7% / -8%, with 12-
  and 14-loss ladders): the swing of this ladder on 100-300 bets (+-25% on staked) is as large as the measured result.
  Not significant. The filter does remove the opening spread cost that made the unfiltered rule negative.
- **f5 (open + 5 s):** +$87 / +$137 / +$102 (+14% / +20% / +5.8%), more bets (169 / 194 / 427) but a deeper tail (max
  stake $20, bankroll $117 on 11-26): no better than f1.
- **Skip policies:** A keeps the most bets and the highest P&L on 11-24 and 11-27; C (abandon) has the smallest stakes
  (max $4-$12, bankroll $8-$59) and similar positive returns (+2% to +35%); B (shadow) is flat on 11-26 (-$6 / -$2 for
  f1 / f5) while A and C are positive. The differences between policies are inside the noise of one day of 100-400 bets.
- **rec_f1 (recovery + filter):** +$100 / +$142 / +$320 (policy A), stakes small most of the time, but the tail is large:
  one 11-24 ladder reached $1,610 (bankroll needed $1,835) and under the shadow policy one 11-26 ladder reached $3,238
  (bankroll needed $3,452, no bust at $5,000 but 69% of it). Under policy C the stakes stay small ($5 / $19 / $44) with
  +$25 / +$54 / +$51.
- **Controls:** the outcome-only coin flip (+$5.72, +0.1%) shows the zero baseline; the filtered coin flip (A) is
  negative on two of three days.

**Verdict of the follow-up.** Unfiltered recovery sizing is rejected (bankroll bust on every day, infeasible stakes).
The price filter is the one thing that helps: it avoids the opening spread and turns the A results from -5% to -16% into
+6% to +34% on all three book days including the validation day, with small stakes. It stays unproven: 100-430 bets per
day, a first-bet edge of +3.9c at 1.4 sigma, a control that swings +-27%, one validation day. The filtered +$2 ladder
(policy A or C) moves to `watchlist`: re-test on new days before any trading. Recovery sizing adds tail risk (up to
$3.4k of bankroll in one ladder) without changing the edge.

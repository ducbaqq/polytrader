# Wallets: are some takers persistently informed, and can we follow them?

**Status:** done, abandoned (the pre-registration below was committed before any wallet skill, markout or settlement edge was
computed) | **Family:** `copy-trading` | **Data:** train day 2026-11-24 from 10:36 UTC only (2026-11-25 is
sealed and never read) | Code: `research/wallets/`

## Question
`pm_api_trades` (Polymarket Data API v2) carries a `proxy_wallet` per trade. Are some wallets persistently
informed (their trades are followed by the price moving their way, and they win at settlement more than the
price implies), and can we profit by copying their trades *after we could have learned about them*, paying
the spread and the taker fee?

## Data semantics (checked before the pre-registration; no outcome or price path was looked at)
Markets with `window_start` in [2026-11-24 10:36, 2026-11-25 00:00) UTC, 5m / 15m / 1h (the 14 4h markets are
left out): 391,688 rows, 6,825 wallets, 1,582 markets, all resolved.

- **One row per taker order, from the taker's point of view.** `tx_hash` is unique per row (392,696 rows,
  392,696 hashes). 99.6% of rows (390,978) match a CLOB WebSocket trade (`pm_trade`) by `tx_hash`, and every
  match has exactly one WS trade with the same token, the same `side` and the same size. The WS `side` is the
  taker's side, so `side` here is the taker's side and `proxy_wallet` is the **taker's** wallet. Makers are
  not in this stream at all (no maker wallets, no maker rows per fill).
- **`ts_ms` is the block time** (whole seconds): median 1.5 s *after* our WS receive time of the same trade
  (p1 −2.9 s, p99 +1.2 s; a few rows are > 1 s before the WS message because of the whole-second rounding and
  occasional WS delays).
- **`recv_ms` is useless for timing**: the recorder downloads a market's whole trade list once, ≥ 120 s after
  the window ends (`resolveEnded` in `src/recorder/polymarket.ts`). `recv_ms − ts_ms` is 175 s at p1, 435 s
  median, 52 min at p99. So "follow from the Data API row's `recv_ms`" is impossible in this data; the live
  equivalent would be polling the Data API (or Polymarket's live activity feed, or Polygon `OrderFilled`
  logs) while the market is open. That latency was never measured.
- **Duplicates / backfill:** the `pm_api_trades` view drops exact duplicate rows (a pre-fix pagination
  retry). No `backfill` rows fall in this period (the 1,366 backfilled rows are before 10:36).
- 414 rows have a block time after the window end (fills in the last second) and 21,858 are before the
  window start (pre-open trading); both are kept.

## Pre-registration
**Timing of a trade.** `t_tr` = the WS `pm_trade` `recv_ms` of the matched trade (our earliest knowledge
that the trade happened); for the 0.4% unmatched rows, `ts_ms` (block time). All markouts and copy entries
are measured from `t_tr`. Prices are token terms; the book is the **combined** book in token terms:
`up_ask = min(Up ask, 1 − Down bid)`, `up_bid = max(Up bid, 1 − Down ask)`, Down = 1 − Up, from
`pm_best_bid_ask` joined `ASOF` on `recv_ms`.

**Per-trade edge (the label, per share, from the wallet's side).** BUY token X at p: settlement edge
`1{X won} − p`, markout `mid_X(t_tr + h) − p`; SELL: the negatives. h = 10 s and 60 s. Gross of fees (the
wallet's own fee/builder fee is unknown). Outcome is only ever the label.

**Halves.** H1 = markets with `window_start` < 2026-11-24 17:15 UTC, H2 = the rest. Everything is chosen on
H1 and applied unchanged to H2.

**Wallet skill on H1.** Eligible: ≥ 20 H1 trades in ≥ 5 H1 markets. Score = share-weighted mean edge, with a
standard error clustered by market (wallet-market sums); t = mean / se. Ranking by t, not by the mean, so
small samples are shrunk toward zero.

**Sharp sets (2 selection rules, both reported):**
- **S60 (primary):** eligible wallets with H1 t(markout 60 s) ≥ 2.
- **Sset:** eligible wallets with H1 t(settlement edge) ≥ 2.

**Persistence on H2 (step 3).** For each set: share-weighted H2 settlement edge and 60 s / 10 s markout per
share of the set's H2 trades, with a 95% block-bootstrap CI (blocks = 15 min of `window_start`, 2,000
draws). Comparator: 500 random sets of the same size drawn from eligible wallets, stratified by H1
trade-count decile; report where the sharp set falls in that distribution, and the all-takers mean.
**Primary metric: S60's H2 settlement edge (¢/share).** "Persistent" = H2 CI lower bound > 0 *and* above the
random-set 95th percentile. If neither set persists, the study stops at step 3 (steps 4's numbers are still
printed for completeness, but no strategy is built).

**Actionability (step 4).** For each sharp-set trade on token X, from `T = t_tr + L + 200 ms` (200 ms order
latency): copy = buy X if the wallet bought X, buy the other token if it sold X (no inventory needed). Pay the
combined ask of the token we buy at T plus the taker fee `rate·p(1−p)`. Skip if the combined spread at T is
> 10¢ or the ask is not in (0, 1). One share per copied trade (no depth model). P&L per share: settlement
(payoff − ask − fee) and 60 s markout (mid at T + 60 s − ask − fee). Attribution delay L (how long after the
WS trade we would learn the wallet):
- **L = 0** (optimistic bound: a wallet-tagged live feed as fast as the CLOB WS; primary for this step),
- **L = block time + 1 s − t_tr, at least 0** (learning it from the chain / Data API right after the block).
Reported on H1 (biased, the set was chosen there) and H2 (honest), with 15-min block-bootstrap CIs.
"Promising" needs H2 settlement P&L CI lower bound > 0 at L = 0 and a positive mean at the block-time delay.

**By-product (market making).** Maker P&L per trade = − taker edge × size (makers pay no fee). Rank wallets
by their H1 total $ taker 60 s markout; for the top k = 10 / 50 / 100, report on H2 their share of all
H2 maker $ losses (sum of negative maker markouts) and their net maker $ P&L, for 60 s markout and settlement.

**Variants counted:** 2 selection rules × 2 attribution delays = 4 (plus the by-product, which is descriptive).
Committed docs show wallets only as their first 6 characters.

## Results

Reproduce: `uv run --project analysis python research/wallets/build.py OUT.parquet` (per-trade table, ~5 s),
then `research/wallets/analyze.py OUT.parquet [results.json]`. ¢/share, size-weighted unless "eqw" (equal
weight per trade). CIs: 95%, 15-min block bootstrap.

**Sanity.** Copying a BUY 200 ms after the WS trade costs the trade price at the median (0.0¢), so the
combined-book join lines up with the trades.

**All takers** (the baseline every set is compared with): settlement edge H1 −0.37 / H2 −0.55, 60 s markout
H1 −0.45 / H2 −0.14. Takers lose on average, as the calibration study found.

**H1 selection.** 1,598 eligible wallets (≥ 20 trades in ≥ 5 markets; 89% of H1 trades). S60: 124 wallets
(4.2% of H1 trades), Sset: 171 (6.8%), overlap 80.

### Persistence H1 → H2 (step 3): none

| | S60 (primary) | Sset |
|---|---|---|
| H1 settlement edge (in-sample) | +3.41 | +5.98 |
| H1 60 s markout (in-sample) | +3.17 | +1.85 |
| **H2 settlement edge** | **−0.10 [−1.94, +1.42]** | −1.00 [−4.02, +1.69] |
| H2 60 s markout | −0.12 [−1.23, +0.90] | +0.10 [−1.13, +1.30] |
| H2 10 s markout | −0.09 [−0.51, +0.35] | −0.26 [−0.84, +0.27] |
| H2 settlement / 60 s, eqw | +1.51 / +0.91 | +0.87 / +0.45 |
| Random sets (500, same size by activity decile), H2 settlement p50 / p95 | −0.06 / +1.72 | −0.07 / +1.40 |
| Percentile of the sharp set among random sets (settlement / 60 s) | 49% / 38% | 19% / 47% |
| Spearman, H1 t vs H2 mean, 905 wallets with ≥ 20 trades in both | 0.08 | 0.13 |

The sharp sets are exactly as good as random wallets of the same activity on H2. The primary metric fails
(CI spans 0, 49th percentile of the random sets). The rank correlation between halves is slightly positive,
but too weak to pick a followable set. Per the pre-registration the study stops here; step 4 is printed for
completeness only.

### Copying (step 4, for completeness): not actionable

One share per copied trade, combined ask + taker fee, spread ≤ 10¢; settlement P&L [CI] and 60 s markout.

| Set, attribution delay | H1 (biased) settle / 60 s | H2 (honest) settle | H2 60 s | H2 n |
|---|---|---|---|---|
| S60, L = 0 | +3.13 / +2.61 | +0.53 [−0.90, +1.82] | −0.12 [−1.05, +0.76] | 7,586 |
| S60, block + 1 s | +2.97 / +2.52 | +0.27 [−1.20, +1.70] | −0.38 [−1.34, +0.51] | 7,200 |
| Sset, L = 0 | +4.27 / +1.01 | −0.04 [−1.53, +1.31] | −0.47 [−1.27, +0.27] | 11,037 |
| Sset, block + 1 s | +4.19 / +0.89 | −0.33 [−1.84, +0.98] | −0.75 [−1.63, +0.01] | 10,670 |

The H1 rows show the selection bias (+3 to +4¢ on the half the set was chosen on). On H2 nothing is
distinguishable from zero even with an instant wallet feed. Copying costs 0.45–0.6¢ more than the
wallet paid at L = 0 and 0.7–0.85¢ at block time (the book has already moved), plus a ~0.45¢ fee.

### By-product: maker adverse selection by taker wallet

Maker P&L per trade = −taker edge × size. Wallets ranked by H1 total taker $ 60 s markout; shares measured
on H2 (out of sample). H2 maker losses (sum of negative maker 60 s markouts): $318k; net maker 60 s P&L +$6.9k.

| H1 top-k takers | Share of H2 volume | Share of H2 maker 60 s losses | Net maker $ vs them (60 s / settlement) |
|---|---|---|---|
| 10 | 8.1% | 10.2% | −3.6k / −1.2k |
| 50 | 16.9% | 20.0% | −7.1k / +6.4k |
| 100 | 23.9% | 27.4% | −5.2k / +8.1k |

Ranked on H2 itself (in-sample, hindsight) the top 10 / 50 / 100 take 20 / 35 / 43% of the losses. Out of
sample, the H1-top wallets take only slightly more than their volume share of the losses. **A wallet
blacklist would not help a market maker much**; adverse flow is better predicted by market state (see
[adverse-flow.md](adverse-flow.md)) than by who is trading. (Makers can't see the taker before the fill
anyway; the use would be pulling quotes after a known-toxic wallet starts trading a market.)

### Caveats and lessons
- **Degenerate t-stats.** A few wallets have a market-clustered standard error of ~0 (e.g. they only buy
  0.98 favourites that all won, so every market's edge is identical); their t is astronomically large.
  The pre-registered rule includes them. They are a small share of the set's trades and don't change the
  verdict, but a future version should floor the standard error (e.g. at the binomial one).
- **One day, one regime.** ~13 h of data, 905 wallets active in both halves. Persistence over days (a
  wallet sharp on day 1 and day 2) is untested; re-run on validation days once they exist (the scripts are
  date-agnostic apart from the constants at the top of `build.py`).
- **Timing of a live feed is unknown.** The recorder fetches Data API trades only after resolution. L = 0
  assumes a wallet-tagged feed as fast as the CLOB WebSocket (~130 ms median after the match); the
  block-time variant is the realistic chain/Data-API bound. Since even L = 0 fails, measuring it isn't
  worth it now.
- **What a live strategy would need** (not built): a `ctx` stream of wallet-tagged trades (Polymarket's
  live activity feed or Polygon `OrderFilled` logs) with its own receive time, recorded by the recorder
  first so it can be replayed. Today `ctx`/`on_trade` only sees anonymous CLOB trades.

**Variants tried: 4** (2 selection rules × 2 attribution delays), plus the descriptive by-product and one
in-sample comparison. **Verdict: abandoned** — no wallet set chosen on H1 kept an edge on H2, and copying
it is ~0 after costs.

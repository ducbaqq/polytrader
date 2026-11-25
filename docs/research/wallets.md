# Wallets: are some takers persistently informed, and can we follow them?

**Status:** pre-registered (this section was committed before any wallet skill, markout or settlement edge was
computed) | **Family:** `copy-trading` | **Data:** train day 2026-11-24 from 10:36 UTC only (2026-11-25 is
sealed and never read) | Code: `research/wallets/`

## Question
`pm_api_trades` (Polymarket Data API v2) carries a `proxy_wallet` per trade. Are some wallets persistently
informed (their trades are followed by the price moving their way, and they win at settlement more than the
price implies), and can we profit by copying their trades *after we could have learned about them*, paying
the spread and the taker fee?

## Data semantics (checked before the pre-registration; no outcome or price path was looked at)
Markets with `window_start` in [2026-11-24 10:36, 2026-11-25 00:00) UTC, 5m / 15m / 1h (the 14 4h markets are
left out): 391,688 rows, ~6.9k wallets, 1,582 markets, all resolved.

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

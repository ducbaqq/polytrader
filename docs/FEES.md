# Polymarket fees

Verified 2026-11-24 against [Polymarket's fee docs](https://docs.polymarket.com/trading/fees.md) and
against on-chain fills (`analysis/tools/verify_fees.py`). Code: `polydata.fees.taker_fee()` in Python,
`taker_fee(shares, price, rate)` in SQL.

## Formula

```
fee (USDC) = shares × rate × (p × (1 − p)) ^ exponent      rounded to 5 decimals
```

- **Takers only.** Makers pay nothing. Settlement and redemption of winning shares are free.
- **Paid in USDC on top of the trade.** Example: buying 15.625 shares at 0.32 transferred 5.17
  USDC, 5.00 for the shares and a 0.17 fee (tx `0x17cbc1ed…992f`).
- `rate` and `exponent` come from each market's `feeSchedule`, in `markets.fee_rate` and
  `markets.fee_exponent`. Today `exponent` is always 1:

| Markets | rate | Fee per share at p = 0.5 | Fee as % of cost when buying at p |
|---|---|---|---|
| BTC 5m and 15m Up/Down | 0.05 | 1.25¢ | 5% × (1 − p): 2.5% at 0.50, 1% at 0.80 |
| All other crypto Up/Down (ETH, SOL, …; BTC 1h/4h/daily) | 0.01 | 0.25¢ | 1% × (1 − p): 0.5% at 0.50 |

The fee in USDC is symmetric around 0.5. It's largest at 0.5 and tends to 0 near 0 and 1; fees below
0.000005 round to zero.

## What this means for strategies

- **A taker trade needs an edge greater than `rate × p(1 − p)` per share**, in probability points,
  plus the spread it crosses. On BTC 5m at 0.5 that's 1.25 points; on ETH 5m it's 0.25 points.
- **A round trip (buy, then sell before resolution) pays twice.** Holding to resolution pays once.
- **Maker orders pay no fee.** That's the main structural advantage of market making on BTC 5m/15m.
- In the first ~20 hours of recording, takers paid about 1.35% of notional on BTC 5m/15m and about
  0.3% everywhere else. That's roughly $82k on $6M of BTC 5m volume.

## Rebates: assume zero

- **Maker rebates** pay a share of the taker fees your fills generate: 3% / 7% / 10% / 15% by tier.
  It starts only at **$200k of 30-day maker weighted volume**, where weighted volume = shares × p(1−p),
  i.e. about $800k of notional at p = 0.5.
- **Taker rebates** pay 10–60% by tier, also starting at **$200k of 30-day weighted volume**, where
  weighted volume = cost × (1 − entry price).
- The market's `feeSchedule.rebateRate` (0.2) is **not** our rebate. Per the docs, rebates depend on
  the account's tier, not on the market's configuration.
- Backtests therefore use **0% rebates**. Treat any rebate as upside once volume justifies it.

## Gotchas

- `fee_rate_bps` in the CLOB `last_trade_price` messages (`pm_trade`) is always 0. Ignore it.
- Fills routed through third-party apps carry the app's **builder fee** on top: observed 1 bp, 50 bp
  and 400 bp of notional. That's why some fees in on-chain data or `pm_api_trades` look too high.
  Orders we place directly don't pay it.
- Fees are set by the protocol at match time and can change. Polymarket has run fee holidays and
  per-category changes. Re-run `uv run python tools/verify_fees.py` after any announcement, and keep
  `fee_rate` per market (from `markets`) rather than hard-coding it.

"""Polymarket trading fees (verified on-chain 2026-11-24, see docs/FEES.md).

    fee (USDC) = shares * rate * (p * (1 - p)) ** exponent,  rounded to 5 decimals

- Taker only; makers pay nothing. Settlement/redemption is free.
- Paid in USDC (pUSD) on top of the trade: buying 15.625 shares at 0.32 costs 5.00 + 0.17 fee.
- rate/exponent per market: `markets.fee_rate`, `markets.fee_exponent` (BTC 5m/15m 0.05, other crypto 0.01).
- Rebates: 0% for us until 30-day weighted volume reaches $200k (maker or taker tier). The market's
  `rebateRate` field is NOT our rebate.
"""

from __future__ import annotations

TICK = 0.00001  # fees are rounded to 5 decimals; smaller fees round to 0


def taker_fee(shares: float, price: float, rate: float, exponent: float = 1.0) -> float:
    """Fee in USDC for a taker fill of `shares` at `price` (0 < price < 1)."""
    return round(shares * rate * (price * (1.0 - price)) ** exponent, 5)


def fee_per_share(price: float, rate: float, exponent: float = 1.0) -> float:
    """Unrounded fee per share in USDC: the edge (in probability points) a taker trade must beat."""
    return rate * (price * (1.0 - price)) ** exponent


def fee_fraction_of_cost(price: float, rate: float, exponent: float = 1.0) -> float:
    """Fee as a fraction of the money spent buying at `price`: rate * (1 - p) when exponent = 1."""
    return fee_per_share(price, rate, exponent) / price


# DuckDB macro, registered by polydata.connect(): taker_fee(shares, price, rate[, exponent])
SQL_MACRO = """
CREATE OR REPLACE MACRO taker_fee(shares, price, rate, exponent := 1) AS
    round(shares * rate * pow(price * (1 - price), exponent), 5);
"""

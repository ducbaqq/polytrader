"""Series table: a copy of src/recorder/config.ts SERIES and the polymarket.ts constants.

tests/paper/test_series.py parses the TypeScript and fails when these drift.
"""

from __future__ import annotations

from typing import NamedTuple


class Series(NamedTuple):
    id: str
    slug: str
    coin: str
    recurrence: str  # 5m | 15m | hourly | 4h | daily


RECURRENCE_MS: dict[str, int] = {
    "5m": 5 * 60_000,
    "15m": 15 * 60_000,
    "hourly": 60 * 60_000,
    "4h": 4 * 60 * 60_000,
    "daily": 24 * 60 * 60_000,
}

# Recorder recurrence names -> strategy timeframe names.
TF_OF: dict[str, str] = {"5m": "5m", "15m": "15m", "hourly": "1h", "4h": "4h", "daily": "1d"}

SERIES: list[Series] = [Series(*t) for t in [
    ('10684', 'btc-up-or-down-5m', 'btc', '5m'),
    ('10192', 'btc-up-or-down-15m', 'btc', '15m'),
    ('10114', 'btc-up-or-down-hourly', 'btc', 'hourly'),
    ('10331', 'btc-up-or-down-4h', 'btc', '4h'),
    ('41', 'btc-up-or-down-daily', 'btc', 'daily'),
    ('10683', 'eth-up-or-down-5m', 'eth', '5m'),
    ('10191', 'eth-up-or-down-15m', 'eth', '15m'),
    ('10117', 'eth-up-or-down-hourly', 'eth', 'hourly'),
    ('10332', 'eth-up-or-down-4h', 'eth', '4h'),
    ('40', 'eth-up-or-down-daily', 'eth', 'daily'),
    ('10686', 'sol-up-or-down-5m', 'sol', '5m'),
    ('10423', 'sol-up-or-down-15m', 'sol', '15m'),
    ('10122', 'solana-up-or-down-hourly', 'sol', 'hourly'),
    ('10333', 'sol-up-or-down-4h', 'sol', '4h'),
    ('10086', 'solana-up-or-down-daily', 'sol', 'daily'),
    ('10685', 'xrp-up-or-down-5m', 'xrp', '5m'),
    ('10422', 'xrp-up-or-down-15m', 'xrp', '15m'),
    ('10123', 'xrp-up-or-down-hourly', 'xrp', 'hourly'),
    ('10327', 'xrp-up-or-down-4h', 'xrp', '4h'),
    ('10100', 'xrp-up-or-down-daily', 'xrp', 'daily'),
    ('11325', 'doge-up-or-down-5m', 'doge', '5m'),
    ('11328', 'doge-up-or-down-15m', 'doge', '15m'),
    ('11287', 'doge-up-or-down-hourly', 'doge', 'hourly'),
    ('11331', 'doge-up-or-down-4h', 'doge', '4h'),
    ('11326', 'bnb-up-or-down-5m', 'bnb', '5m'),
    ('11330', 'bnb-up-or-down-15m', 'bnb', '15m'),
    ('11291', 'bnb-up-or-down-hourly', 'bnb', 'hourly'),
    ('11332', 'bnb-up-or-down-4h', 'bnb', '4h'),
    ('10421', 'bnb-up-or-down-daily', 'bnb', 'daily'),
    ('11327', 'hype-up-or-down-5m', 'hype', '5m'),
    ('11329', 'hype-up-or-down-15m', 'hype', '15m'),
    ('11289', 'hype-up-or-down-hourly', 'hype', 'hourly'),
    ('11333', 'hype-up-or-down-4h', 'hype', '4h'),
    ('11288', 'hype-up-or-down-daily', 'hype', 'daily'),
    ('12517', 'zec-up-or-down-5m', 'zec', '5m'),
    ('12518', 'zec-up-or-down-15m', 'zec', '15m'),
    ('12519', 'zec-up-or-down-4h', 'zec', '4h'),
]]

# polymarket.ts: markets fetched per series per discovery poll.
LOOKAHEAD: dict[str, int] = {"5m": 4, "15m": 3, "hourly": 3, "4h": 2, "daily": 2}

# polymarket.ts: busiest series get one CLOB connection per market (1013 slow consumer otherwise).
PER_MARKET_SERIES: frozenset[str] = frozenset(
    {"btc-up-or-down-5m", "btc-up-or-down-15m", "eth-up-or-down-5m"}
)

# config.ts
SUBSCRIBE_LEAD_WINDOWS = 2
MIN_SUBSCRIBE_LEAD_MS = 600_000
UNSUBSCRIBE_AFTER_END_MS = 180_000


def series_for(coins, timeframes) -> list[Series]:
    """Series of the given coins (lowercase) and timeframes ("5m", "15m", "1h", "4h", "1d")."""
    cs, tfs = {c.lower() for c in coins}, set(timeframes)
    return [s for s in SERIES if s.coin in cs and TF_OF[s.recurrence] in tfs]


def lead_ms(s: Series) -> int:
    """Subscribe this long before a market's window opens (polymarket.ts leadMs)."""
    return max(MIN_SUBSCRIBE_LEAD_MS, SUBSCRIBE_LEAD_WINDOWS * RECURRENCE_MS[s.recurrence])

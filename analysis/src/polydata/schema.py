"""Column types of every recorder stream (see docs/RECORDER.md for meanings).

TSV streams are converted column-for-column with explicit types. JSONL streams become
(recv_ms, d JSON) and are flattened by views in views.sql, except where a typed table is far more
useful (Data API trades, depth snapshots).
"""

from __future__ import annotations

# Prices/sizes are DOUBLE: exact enough for research (15-16 significant digits) and fast.
TSV: dict[str, dict[str, str]] = {
    "bn_spot_trade": {
        "recv_ms": "BIGINT", "sym": "VARCHAR", "event_ms": "BIGINT", "trade_ms": "BIGINT",
        "trade_id": "BIGINT", "price": "DOUBLE", "qty": "DOUBLE", "buyer_is_maker": "BOOLEAN",
    },
    "bn_spot_bbo": {
        "recv_ms": "BIGINT", "sym": "VARCHAR", "update_id": "BIGINT",
        "bid": "DOUBLE", "bid_qty": "DOUBLE", "ask": "DOUBLE", "ask_qty": "DOUBLE",
    },
    "bn_spot_depth": {
        "recv_ms": "BIGINT", "sym": "VARCHAR", "event_ms": "BIGINT",
        "first_update_id": "BIGINT", "last_update_id": "BIGINT", "bids": "VARCHAR", "asks": "VARCHAR",
    },
    "bn_fut_trade": {
        "recv_ms": "BIGINT", "sym": "VARCHAR", "event_ms": "BIGINT", "trade_ms": "BIGINT",
        "agg_id": "BIGINT", "first_id": "BIGINT", "last_id": "BIGINT",
        "price": "DOUBLE", "qty": "DOUBLE", "buyer_is_maker": "BOOLEAN",
    },
    "bn_fut_bbo": {
        "recv_ms": "BIGINT", "sym": "VARCHAR", "update_id": "BIGINT", "event_ms": "BIGINT", "txn_ms": "BIGINT",
        "bid": "DOUBLE", "bid_qty": "DOUBLE", "ask": "DOUBLE", "ask_qty": "DOUBLE",
    },
    "bn_fut_depth": {
        "recv_ms": "BIGINT", "sym": "VARCHAR", "event_ms": "BIGINT", "txn_ms": "BIGINT",
        "first_update_id": "BIGINT", "last_update_id": "BIGINT", "prev_update_id": "BIGINT",
        "bids": "VARCHAR", "asks": "VARCHAR",
    },
    "bn_fut_mark": {
        "recv_ms": "BIGINT", "sym": "VARCHAR", "event_ms": "BIGINT", "mark_price": "DOUBLE",
        "index_price": "DOUBLE", "est_settle_price": "DOUBLE", "funding_rate": "DOUBLE", "next_funding_ms": "BIGINT",
    },
    "bn_fut_liquidation": {
        "recv_ms": "BIGINT", "sym": "VARCHAR", "event_ms": "BIGINT", "trade_ms": "BIGINT",
        "side": "VARCHAR", "order_type": "VARCHAR", "tif": "VARCHAR", "qty": "DOUBLE", "price": "DOUBLE",
        "avg_price": "DOUBLE", "status": "VARCHAR", "last_filled_qty": "DOUBLE", "filled_qty": "DOUBLE",
    },
    "pm_book": {
        "recv_ms": "BIGINT", "ts_ms": "BIGINT", "asset": "VARCHAR", "bids": "VARCHAR", "asks": "VARCHAR", "hash": "VARCHAR",
    },
    "pm_price_change": {
        "recv_ms": "BIGINT", "ts_ms": "BIGINT", "asset": "VARCHAR", "side": "VARCHAR",
        "price": "DOUBLE", "size": "DOUBLE", "best_bid": "DOUBLE", "best_ask": "DOUBLE",
    },
    "pm_best_bid_ask": {
        "recv_ms": "BIGINT", "ts_ms": "BIGINT", "asset": "VARCHAR",
        "best_bid": "DOUBLE", "best_ask": "DOUBLE", "spread": "DOUBLE",
    },
    "pm_trade": {
        "recv_ms": "BIGINT", "ts_ms": "BIGINT", "asset": "VARCHAR", "side": "VARCHAR",
        "price": "DOUBLE", "size": "DOUBLE", "fee_rate_bps": "DOUBLE", "tx_hash": "VARCHAR",
    },
    "rtds_price": {
        "recv_ms": "BIGINT", "topic": "VARCHAR", "symbol": "VARCHAR", "ts_ms": "BIGINT", "value": "DOUBLE",
        "full_accuracy_value": "DOUBLE", "server_ts_ms": "BIGINT", "conn": "VARCHAR",
    },
}

JSONL = [
    "pm_market_meta", "pm_resolution", "pm_api_trades", "pm_new_market", "pm_other",
    "bn_depth_snapshot", "bn_fut_stats", "bn_other", "rtds_other",
]

# Every TSV stream's Parquet also has a derived `seq BIGINT` (raw arrival order within the
# stream-hour, see polydata.build); it is not a recorder column.
SEQ_COLUMN = "seq"

# Sort key inside each Parquet file: groups rows per instrument so filters on it skip row groups.
# Files are sorted by (SORT key, recv_ms, seq).
SORT = {name: ("asset" if name.startswith("pm_") else "symbol" if name == "rtds_price" else "sym") for name in TSV}

# Typed conversions for JSONL streams (SELECT over `src` = read_json(... recv_ms, d)).
JSONL_SELECT: dict[str, str] = {
    "pm_api_trades": """
        SELECT recv_ms,
               d->>'slug'                               AS slug,
               d->>'condition_id'                       AS condition_id,
               d->>'token_id'                           AS token_id,
               d->>'outcome'                            AS outcome,
               (d->>'outcome_index')::INT               AS outcome_index,
               d->>'side'                               AS side,
               (d->>'price')::DOUBLE                    AS price,
               (d->>'size')::DOUBLE                     AS size,
               (d->>'timestamp')::BIGINT * 1000         AS ts_ms,
               d->>'proxy_wallet'                       AS proxy_wallet,
               d->>'name'                               AS name,
               d->>'pseudonym'                          AS pseudonym,
               d->>'transaction_hash'                   AS tx_hash,
               coalesce((d->>'backfill')::BOOLEAN, false) AS backfill
        FROM src
        -- the recorder could write a page twice if a download failed mid-pagination (fixed 2026-11-24)
        QUALIFY row_number() OVER (PARTITION BY slug, tx_hash, proxy_wallet, token_id, side, price, size, ts_ms ORDER BY recv_ms) = 1
        ORDER BY slug, ts_ms""",
    "bn_depth_snapshot": """
        SELECT recv_ms,
               d->>'market'                             AS market,
               d->>'sym'                                AS sym,
               (d->>'lastUpdateId')::BIGINT             AS last_update_id,
               list_transform((d->'bids')::VARCHAR[][], lambda x: {'price': x[1]::DOUBLE, 'qty': x[2]::DOUBLE}) AS bids,
               list_transform((d->'asks')::VARCHAR[][], lambda x: {'price': x[1]::DOUBLE, 'qty': x[2]::DOUBLE}) AS asks
        FROM src ORDER BY market, sym, recv_ms""",
}

"""DuckDB views and macros over the Parquet store. `connect()` is the entry point for analysis."""

from __future__ import annotations

from pathlib import Path

import duckdb

from . import schema
from .config import paths

# One row per market: static fields from discovery (`pm_market_meta`), result from `pm_resolution`.
# Either side may be missing (market discovered but not yet resolved, or vice versa during the
# development period), so both are unioned and the latest row of each kind wins.
MARKETS_SQL = """
WITH meta AS (
    SELECT d->>'$.event.slug' AS slug, d->>'series' AS series, d->'$.event.markets[0]' AS m, recv_ms
    FROM read_parquet('{P}/pm_market_meta/*/*.parquet')
    QUALIFY row_number() OVER (PARTITION BY slug ORDER BY recv_ms DESC) = 1
), res AS (
    SELECT d->>'slug' AS slug, d->>'outcome' AS outcome, d->'event' AS ev, d->'$.event.markets[0]' AS m,
           recv_ms, coalesce((d->>'backfill')::BOOLEAN, false) AS backfill
    FROM read_parquet('{P}/pm_resolution/*/*.parquet')
    QUALIFY row_number() OVER (PARTITION BY slug ORDER BY recv_ms DESC) = 1
), j AS (
    SELECT coalesce(meta.slug, res.slug) AS slug, meta.series, coalesce(meta.m, res.m) AS m,
           meta.recv_ms AS discovered_ms, res.*  EXCLUDE (slug, m)
    FROM meta FULL OUTER JOIN res USING (slug)
)
SELECT
    slug,
    coalesce(series, ev->>'seriesSlug')                                AS series,
    m->>'$.cryptoMarketConfig.asset'                                   AS coin,
    split_part(slug, '-', 3)                                           AS timeframe,      -- 5m 15m 1h 4h 1d
    to_timestamp(split_part(slug, '-', 4)::BIGINT)                     AS window_start,
    (m->>'endDate')::TIMESTAMPTZ                                       AS window_end,
    split_part(slug, '-', 4)::BIGINT * 1000                            AS window_start_ms,
    epoch_ms((m->>'endDate')::TIMESTAMPTZ)                             AS window_end_ms,
    m->>'conditionId'                                                  AS condition_id,
    json_extract_string(m->>'outcomes', '$[0]')                        AS outcome_0,      -- 'Up'
    json_extract_string(m->>'clobTokenIds', '$[0]')                    AS up_token_id,
    json_extract_string(m->>'clobTokenIds', '$[1]')                    AS down_token_id,
    m->>'resolutionSource'                                             AS resolution_source,
    (m->>'$.cryptoMarketConfig.twapLookbackSeconds')::INT              AS twap_lookback_s,
    (m->>'$.feeSchedule.rate')::DOUBLE                                 AS fee_rate,
    (m->>'$.feeSchedule.exponent')::DOUBLE                             AS fee_exponent,
    (m->>'$.feeSchedule.rebateRate')::DOUBLE                           AS fee_rebate_rate,
    (m->>'$.feeSchedule.takerOnly')::BOOLEAN                           AS fee_taker_only,
    (m->>'orderPriceMinTickSize')::DOUBLE                              AS tick_size_at_discovery,
    (m->>'orderMinSize')::DOUBLE                                       AS min_order_size,
    epoch_ms(discovered_ms)                                            AS discovered_at,
    outcome,                                                                              -- 'Up' / 'Down' / NULL
    (ev->>'$.eventMetadata.priceToBeat')::DOUBLE                       AS price_to_beat,  -- NULL for 4h markets
    (ev->>'$.eventMetadata.finalPrice')::DOUBLE                        AS final_price,
    epoch_ms(recv_ms)                                                  AS resolution_seen_at,
    (ev->>'volume')::DOUBLE                                            AS volume,
    backfill                                                           AS resolution_backfilled
FROM j
ORDER BY window_start, slug
"""

MACROS = r"""
-- 'price:size|price:size' (book / depth columns) -> LIST(STRUCT(price, size)), in recorded order;
-- an empty side (stored as NULL) gives [].
CREATE OR REPLACE MACRO levels(s) AS
    list_transform(list_filter(string_split(coalesce(s, ''), '|'), lambda x: x <> ''),
                   lambda x: {'price': split_part(x, ':', 1)::DOUBLE, 'size': split_part(x, ':', 2)::DOUBLE});
-- ms since epoch -> TIMESTAMP (UTC)
CREATE OR REPLACE MACRO ms(x) AS epoch_ms(x::BIGINT);
-- Polymarket asset key 'slug|U' -> slug, outcome
CREATE OR REPLACE MACRO asset_slug(a) AS split_part(a, '|', 1);
CREATE OR REPLACE MACRO asset_outcome(a) AS CASE split_part(a, '|', 2) WHEN 'U' THEN 'Up' WHEN 'D' THEN 'Down' END;
"""

# Derived views; each is created only if the streams it reads exist.
DERIVED: dict[str, tuple[list[str], str]] = {
    "chainlink": (["rtds_price"], """
        SELECT recv_ms, ts_ms, split_part(symbol, '/', 1) AS coin, value AS price, full_accuracy_value,
               server_ts_ms, conn, date
        FROM rtds_price WHERE topic = 'crypto_prices_chainlink'"""),
    "pm_binance_relay": (["rtds_price"], """
        SELECT recv_ms, ts_ms, replace(symbol, 'usdt', '') AS coin, value AS price, server_ts_ms, conn, date
        FROM rtds_price WHERE topic = 'crypto_prices'"""),
    "pm_tick_size_change": (["pm_other"], """
        SELECT recv_ms, (d->>'timestamp')::BIGINT AS ts_ms, d->>'market' AS condition_id, d->>'asset_id' AS token_id,
               (d->>'old_tick_size')::DOUBLE AS old_tick_size, (d->>'new_tick_size')::DOUBLE AS new_tick_size
        FROM pm_other WHERE d->>'event_type' = 'tick_size_change'"""),
    "pm_market_resolved": (["pm_other"], """
        SELECT recv_ms, (d->>'timestamp')::BIGINT AS ts_ms, d->>'market' AS condition_id, d AS raw
        FROM pm_other WHERE d->>'event_type' = 'market_resolved'"""),
    "bn_open_interest": (["bn_fut_stats"], """
        SELECT recv_ms, d->>'sym' AS sym, (d->>'$.data.time')::BIGINT AS ts_ms,
               (d->>'$.data.openInterest')::DOUBLE AS open_interest
        FROM bn_fut_stats WHERE d->>'endpoint' = 'open_interest'"""),
    "bn_fut_ratios": (["bn_fut_stats"], """
        -- 5-minute Binance futures statistics; one row per (endpoint, sym, timestamp), latest fetch wins.
        SELECT d->>'endpoint' AS endpoint, d->>'sym' AS sym, (r->>'timestamp')::BIGINT AS ts_ms,
               (r->>'longShortRatio')::DOUBLE AS long_short_ratio, (r->>'longAccount')::DOUBLE AS long_account,
               (r->>'shortAccount')::DOUBLE AS short_account, (r->>'buySellRatio')::DOUBLE AS buy_sell_ratio,
               (r->>'buyVol')::DOUBLE AS buy_vol, (r->>'sellVol')::DOUBLE AS sell_vol,
               (r->>'sumOpenInterest')::DOUBLE AS sum_open_interest,
               (r->>'sumOpenInterestValue')::DOUBLE AS sum_open_interest_value, recv_ms
        FROM (SELECT recv_ms, d, unnest(d->'$.data[*]') AS r FROM bn_fut_stats WHERE d->>'endpoint' <> 'open_interest')
        QUALIFY row_number() OVER (PARTITION BY endpoint, sym, ts_ms ORDER BY recv_ms DESC) = 1"""),
}


def _stream_view(name: str, glob: str) -> str:
    extra = ""
    if name in ("pm_book", "pm_price_change", "pm_best_bid_ask", "pm_trade"):
        extra = ", asset_slug(asset) AS slug, asset_outcome(asset) AS outcome"
    return (
        f"CREATE OR REPLACE VIEW {name} AS SELECT *{extra} "
        f"FROM read_parquet('{glob}', hive_partitioning=true, hive_types={{'date': DATE}})"
    )


def create_views(con: duckdb.DuckDBPyConnection, parquet: Path | None = None) -> list[str]:
    """(Re)create macros and one view per available stream. Returns the view names."""
    root = (parquet or paths().parquet).as_posix()
    con.execute(MACROS)
    names: list[str] = []
    for stream in list(schema.TSV) + schema.JSONL:
        if list(Path(root, stream).glob("*/*.parquet")):
            con.execute(_stream_view(stream, f"{root}/{stream}/*/*.parquet"))
            names.append(stream)
    if Path(root, "markets.parquet").exists():
        con.execute(f"CREATE OR REPLACE VIEW markets AS SELECT * FROM read_parquet('{root}/markets.parquet')")
        names.append("markets")
    for view, (needs, sql) in DERIVED.items():
        if all(n in names for n in needs):
            con.execute(f"CREATE OR REPLACE VIEW {view} AS {sql}")
            names.append(view)
    return names


def connect(database: str | Path = ":memory:", read_only: bool = False, **config) -> duckdb.DuckDBPyConnection:
    """DuckDB connection with every stream available as a view, e.g.

        import polydata
        con = polydata.connect()
        con.sql("SELECT coin, count(*) FROM markets GROUP BY 1").show()
    """
    con = duckdb.connect(str(database), read_only=read_only, config=config)
    con.execute("SET TimeZone = 'UTC'")
    if not read_only:
        create_views(con)
    return con

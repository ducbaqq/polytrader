/**
 * Repository for order_book_snapshots table operations.
 */

import { PoolClient } from 'pg';
import { queryRows, queryOne } from './index';
import { MarketData, TokenData } from '../types';

export interface DBOrderBookSnapshot {
  id: number;
  market_snapshot_id: number;
  market_id: string;
  scan_timestamp: Date;
  token_side: string;
  best_bid_price: number | null;
  best_bid_size: number | null;
  best_ask_price: number | null;
  best_ask_size: number | null;
  bid_price_2: number | null;
  bid_size_2: number | null;
  ask_price_2: number | null;
  ask_size_2: number | null;
  bid_price_3: number | null;
  bid_size_3: number | null;
  ask_price_3: number | null;
  ask_size_3: number | null;
  spread_percent: number | null;
  mid_price: number | null;
  created_at: Date;
}

/**
 * Insert order book snapshots for a batch of markets.
 */
export async function insertOrderBookSnapshots(
  client: PoolClient,
  markets: MarketData[],
  snapshotIdMap: Map<string, number>,
  scanTimestamp: Date
): Promise<number> {
  const rows: any[][] = [];

  for (const market of markets) {
    const snapshotId = snapshotIdMap.get(market.marketId);
    if (!snapshotId) continue;

    // Insert YES token order book
    if (market.yesToken) {
      rows.push(buildOrderBookRow(market.marketId, snapshotId, scanTimestamp, market.yesToken, 'YES'));
    }

    // Insert NO token order book
    if (market.noToken) {
      rows.push(buildOrderBookRow(market.marketId, snapshotId, scanTimestamp, market.noToken, 'NO'));
    }
  }

  if (rows.length === 0) return 0;

  const columns = [
    'market_snapshot_id',
    'market_id',
    'scan_timestamp',
    'token_side',
    'best_bid_price',
    'best_bid_size',
    'best_ask_price',
    'best_ask_size',
    'bid_price_2',
    'bid_size_2',
    'ask_price_2',
    'ask_size_2',
    'bid_price_3',
    'bid_size_3',
    'ask_price_3',
    'ask_size_3',
    'spread_percent',
    'mid_price',
  ];

  // Batch insert
  const placeholders: string[] = [];
  const values: any[] = [];

  rows.forEach((row, rowIndex) => {
    const rowPlaceholders = columns.map(
      (_, colIndex) => `$${rowIndex * columns.length + colIndex + 1}`
    );
    placeholders.push(`(${rowPlaceholders.join(', ')})`);
    values.push(...row);
  });

  const sql = `
    INSERT INTO order_book_snapshots (${columns.join(', ')})
    VALUES ${placeholders.join(', ')}
  `;

  await client.query(sql, values);
  return rows.length;
}

function buildOrderBookRow(
  marketId: string,
  snapshotId: number,
  scanTimestamp: Date,
  token: TokenData,
  side: 'YES' | 'NO'
): any[] {
  const midPrice =
    token.bestBid && token.bestAsk
      ? (token.bestBid.price + token.bestAsk.price) / 2
      : token.bestBid?.price || token.bestAsk?.price || null;

  return [
    snapshotId,
    marketId,
    scanTimestamp,
    side,
    token.bestBid?.price || null,
    token.bestBid?.size || null,
    token.bestAsk?.price || null,
    token.bestAsk?.size || null,
    null, // bid_price_2 (we don't have depth data currently)
    null, // bid_size_2
    null, // ask_price_2
    null, // ask_size_2
    null, // bid_price_3
    null, // bid_size_3
    null, // ask_price_3
    null, // ask_size_3
    token.spreadPct || null,
    midPrice,
  ];
}

/**
 * Get the latest order book for a market and token side.
 */
export async function getLatestOrderBook(
  marketId: string,
  tokenSide: 'YES' | 'NO'
): Promise<DBOrderBookSnapshot | null> {
  return queryOne<DBOrderBookSnapshot>(
    `SELECT * FROM order_book_snapshots
     WHERE market_id = $1 AND token_side = $2
     ORDER BY scan_timestamp DESC
     LIMIT 1`,
    [marketId, tokenSide]
  );
}

/**
 * Get order book history for a market.
 */
export async function getOrderBookHistory(
  marketId: string,
  tokenSide: 'YES' | 'NO',
  startTime: Date,
  endTime: Date
): Promise<DBOrderBookSnapshot[]> {
  return queryRows<DBOrderBookSnapshot>(
    `SELECT * FROM order_book_snapshots
     WHERE market_id = $1 AND token_side = $2
       AND scan_timestamp BETWEEN $3 AND $4
     ORDER BY scan_timestamp ASC`,
    [marketId, tokenSide, startTime, endTime]
  );
}

/**
 * Get average spread for a market over time.
 */
export async function getAverageSpread(
  marketId: string,
  hours: number = 24
): Promise<{ yes_spread: number; no_spread: number }> {
  const result = await queryOne<{ yes_spread: string; no_spread: string }>(
    `SELECT
       AVG(CASE WHEN token_side = 'YES' THEN spread_percent END) as yes_spread,
       AVG(CASE WHEN token_side = 'NO' THEN spread_percent END) as no_spread
     FROM order_book_snapshots
     WHERE market_id = $1
       AND scan_timestamp > NOW() - INTERVAL '${hours} hours'`,
    [marketId]
  );

  return {
    yes_spread: parseFloat(result?.yes_spread || '0'),
    no_spread: parseFloat(result?.no_spread || '0'),
  };
}

/**
 * Delete old order book snapshots.
 */
export async function deleteOldOrderBooks(daysToKeep: number = 7): Promise<number> {
  const result = await queryOne<{ count: string }>(
    `WITH deleted AS (
       DELETE FROM order_book_snapshots
       WHERE scan_timestamp < NOW() - INTERVAL '${daysToKeep} days'
       RETURNING 1
     )
     SELECT COUNT(*) as count FROM deleted`
  );
  return parseInt(result?.count || '0', 10);
}

/**
 * Get price change percentage over the last N minutes.
 * Returns the percentage change from oldest to newest price.
 * Negative value means price dropped.
 */
export async function getPriceChange(
  marketId: string,
  tokenSide: 'YES' | 'NO',
  minutes: number = 30
): Promise<number | null> {
  const result = await queryOne<{ old_price: string; new_price: string }>(
    `WITH price_data AS (
       SELECT
         mid_price,
         scan_timestamp,
         ROW_NUMBER() OVER (ORDER BY scan_timestamp ASC) as oldest_rank,
         ROW_NUMBER() OVER (ORDER BY scan_timestamp DESC) as newest_rank
       FROM order_book_snapshots
       WHERE market_id = $1
         AND token_side = $2
         AND scan_timestamp > NOW() - INTERVAL '${minutes} minutes'
         AND mid_price IS NOT NULL
     )
     SELECT
       (SELECT mid_price FROM price_data WHERE oldest_rank = 1) as old_price,
       (SELECT mid_price FROM price_data WHERE newest_rank = 1) as new_price`,
    [marketId, tokenSide]
  );

  if (!result?.old_price || !result?.new_price) {
    return null;
  }

  const oldPrice = parseFloat(result.old_price);
  const newPrice = parseFloat(result.new_price);

  if (oldPrice <= 0) return null;

  return (newPrice - oldPrice) / oldPrice;
}

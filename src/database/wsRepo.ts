/**
 * Repository for WebSocket price update persistence.
 * Handles batching and efficient database updates from real-time data.
 */

import { query, getPool } from './index';
import { WSPriceUpdate } from '../wsScanner';

/**
 * Batch insert price updates from WebSocket into order_book_snapshots.
 * Uses upsert to avoid duplicates within the same scan window.
 */
export async function batchInsertWSUpdates(
  updates: WSPriceUpdate[],
  scanTimestamp: Date = new Date()
): Promise<number> {
  if (updates.length === 0) return 0;

  const pool = getPool();
  const client = await pool.connect();

  try {
    // First, insert market snapshots for any new markets
    const marketIds = [...new Set(updates.map(u => u.marketId))];
    const snapshotIdMap = new Map<string, number>();

    // Get or create market snapshots
    for (const marketId of marketIds) {
      const result = await client.query(
        `INSERT INTO market_snapshots (market_id, scan_timestamp, status)
         VALUES ($1, $2, 'active')
         ON CONFLICT (market_id, scan_timestamp) DO UPDATE SET status = 'active'
         RETURNING id`,
        [marketId, scanTimestamp]
      );
      snapshotIdMap.set(marketId, result.rows[0].id);
    }

    // Build order book rows
    const columns = [
      'market_snapshot_id',
      'market_id',
      'scan_timestamp',
      'token_side',
      'best_bid_price',
      'best_bid_size',
      'best_ask_price',
      'best_ask_size',
      'spread_percent',
      'mid_price',
    ];

    const rows: any[][] = [];

    for (const update of updates) {
      const snapshotId = snapshotIdMap.get(update.marketId);
      if (!snapshotId) continue;

      const midPrice = update.bestBid && update.bestAsk
        ? (update.bestBid.price + update.bestAsk.price) / 2
        : update.bestBid?.price || update.bestAsk?.price || null;

      rows.push([
        snapshotId,
        update.marketId,
        scanTimestamp,
        update.outcome,
        update.bestBid?.price || null,
        update.bestBid?.size || null,
        update.bestAsk?.price || null,
        update.bestAsk?.size || null,
        update.spreadPct || null,
        midPrice,
      ]);
    }

    if (rows.length === 0) return 0;

    // Batch insert with conflict handling
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
      ON CONFLICT (market_id, token_side, scan_timestamp)
      DO UPDATE SET
        best_bid_price = EXCLUDED.best_bid_price,
        best_bid_size = EXCLUDED.best_bid_size,
        best_ask_price = EXCLUDED.best_ask_price,
        best_ask_size = EXCLUDED.best_ask_size,
        spread_percent = EXCLUDED.spread_percent,
        mid_price = EXCLUDED.mid_price
    `;

    await client.query(sql, values);
    return rows.length;

  } finally {
    client.release();
  }
}

/**
 * Get latest WebSocket-sourced prices for arbitrage detection.
 */
export async function getLatestWSPrices(marketIds: string[]): Promise<Map<string, {
  yesAsk: number | null;
  noAsk: number | null;
  yesBid: number | null;
  noBid: number | null;
}>> {
  if (marketIds.length === 0) return new Map();

  const result = await query(
    `WITH latest AS (
       SELECT DISTINCT ON (market_id, token_side)
         market_id,
         token_side,
         best_bid_price,
         best_ask_price
       FROM order_book_snapshots
       WHERE market_id = ANY($1)
       ORDER BY market_id, token_side, scan_timestamp DESC
     )
     SELECT
       market_id,
       MAX(CASE WHEN token_side = 'YES' THEN best_ask_price END) as yes_ask,
       MAX(CASE WHEN token_side = 'NO' THEN best_ask_price END) as no_ask,
       MAX(CASE WHEN token_side = 'YES' THEN best_bid_price END) as yes_bid,
       MAX(CASE WHEN token_side = 'NO' THEN best_bid_price END) as no_bid
     FROM latest
     GROUP BY market_id`,
    [marketIds]
  );

  const priceMap = new Map<string, {
    yesAsk: number | null;
    noAsk: number | null;
    yesBid: number | null;
    noBid: number | null;
  }>();

  for (const row of result.rows) {
    priceMap.set(row.market_id, {
      yesAsk: row.yes_ask ? parseFloat(row.yes_ask) : null,
      noAsk: row.no_ask ? parseFloat(row.no_ask) : null,
      yesBid: row.yes_bid ? parseFloat(row.yes_bid) : null,
      noBid: row.no_bid ? parseFloat(row.no_bid) : null,
    });
  }

  return priceMap;
}

/**
 * Detect arbitrage opportunities from latest WebSocket data.
 * Returns markets where YES ask + NO ask < threshold (profitable to buy both).
 */
export async function detectArbitrageFromWS(
  threshold: number = 0.995
): Promise<Array<{
  marketId: string;
  yesAsk: number;
  noAsk: number;
  sum: number;
  profit: number;
}>> {
  const result = await query(
    `WITH latest AS (
       SELECT DISTINCT ON (market_id, token_side)
         market_id,
         token_side,
         best_ask_price,
         best_ask_size
       FROM order_book_snapshots
       WHERE scan_timestamp > NOW() - INTERVAL '5 minutes'
         AND best_ask_price IS NOT NULL
       ORDER BY market_id, token_side, scan_timestamp DESC
     ),
     paired AS (
       SELECT
         market_id,
         MAX(CASE WHEN token_side = 'YES' THEN best_ask_price END) as yes_ask,
         MAX(CASE WHEN token_side = 'NO' THEN best_ask_price END) as no_ask,
         MIN(CASE WHEN token_side = 'YES' THEN best_ask_size END) as yes_size,
         MIN(CASE WHEN token_side = 'NO' THEN best_ask_size END) as no_size
       FROM latest
       GROUP BY market_id
       HAVING COUNT(*) = 2  -- Must have both YES and NO
     )
     SELECT
       market_id,
       yes_ask,
       no_ask,
       (yes_ask + no_ask) as sum,
       (1.0 - yes_ask - no_ask) as profit,
       LEAST(yes_size, no_size) as min_size
     FROM paired
     WHERE (yes_ask + no_ask) < $1
     ORDER BY profit DESC`,
    [threshold]
  );

  return result.rows.map(row => ({
    marketId: row.market_id,
    yesAsk: parseFloat(row.yes_ask),
    noAsk: parseFloat(row.no_ask),
    sum: parseFloat(row.sum),
    profit: parseFloat(row.profit),
  }));
}

/**
 * Get price statistics from WebSocket data for a market.
 */
export async function getWSPriceStats(
  marketId: string,
  minutesBack: number = 60
): Promise<{
  avgYesBid: number | null;
  avgYesAsk: number | null;
  avgNoBid: number | null;
  avgNoAsk: number | null;
  minSpread: number | null;
  maxSpread: number | null;
  updateCount: number;
}> {
  const result = await query(
    `SELECT
       AVG(CASE WHEN token_side = 'YES' THEN best_bid_price END) as avg_yes_bid,
       AVG(CASE WHEN token_side = 'YES' THEN best_ask_price END) as avg_yes_ask,
       AVG(CASE WHEN token_side = 'NO' THEN best_bid_price END) as avg_no_bid,
       AVG(CASE WHEN token_side = 'NO' THEN best_ask_price END) as avg_no_ask,
       MIN(spread_percent) as min_spread,
       MAX(spread_percent) as max_spread,
       COUNT(*) as update_count
     FROM order_book_snapshots
     WHERE market_id = $1
       AND scan_timestamp > NOW() - INTERVAL '${minutesBack} minutes'`,
    [marketId]
  );

  const row = result.rows[0];
  return {
    avgYesBid: row.avg_yes_bid ? parseFloat(row.avg_yes_bid) : null,
    avgYesAsk: row.avg_yes_ask ? parseFloat(row.avg_yes_ask) : null,
    avgNoBid: row.avg_no_bid ? parseFloat(row.avg_no_bid) : null,
    avgNoAsk: row.avg_no_ask ? parseFloat(row.avg_no_ask) : null,
    minSpread: row.min_spread ? parseFloat(row.min_spread) : null,
    maxSpread: row.max_spread ? parseFloat(row.max_spread) : null,
    updateCount: parseInt(row.update_count),
  };
}

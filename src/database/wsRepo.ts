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

/**
 * Detect wide spread opportunities from WebSocket data.
 * Returns markets where spread > threshold (good for market making).
 */
export async function detectWideSpreadFromWS(
  threshold: number = 0.05
): Promise<Array<{
  marketId: string;
  tokenSide: string;
  spreadPct: number;
  bestBid: number;
  bestAsk: number;
  liquidity: number;
}>> {
  const result = await query(
    `WITH latest AS (
       SELECT DISTINCT ON (market_id, token_side)
         market_id,
         token_side,
         best_bid_price,
         best_ask_price,
         best_bid_size,
         best_ask_size,
         spread_percent
       FROM order_book_snapshots
       WHERE scan_timestamp > NOW() - INTERVAL '5 minutes'
         AND best_bid_price IS NOT NULL
         AND best_ask_price IS NOT NULL
         AND spread_percent IS NOT NULL
       ORDER BY market_id, token_side, scan_timestamp DESC
     )
     SELECT
       market_id,
       token_side,
       spread_percent,
       best_bid_price,
       best_ask_price,
       LEAST(best_bid_size, best_ask_size) as liquidity
     FROM latest
     WHERE spread_percent > $1
     ORDER BY spread_percent DESC
     LIMIT 50`,
    [threshold]
  );

  return result.rows.map(row => ({
    marketId: row.market_id,
    tokenSide: row.token_side,
    spreadPct: parseFloat(row.spread_percent),
    bestBid: parseFloat(row.best_bid_price),
    bestAsk: parseFloat(row.best_ask_price),
    liquidity: parseFloat(row.liquidity || 0),
  }));
}

/**
 * Detect volume spikes by comparing recent volume to historical average.
 * Uses market_snapshots volume data.
 */
export async function detectVolumeSpikeFromWS(
  spikeMultiplier: number = 3.0
): Promise<Array<{
  marketId: string;
  currentVolume: number;
  avgVolume: number;
  multiplier: number;
}>> {
  const result = await query(
    `WITH recent_volume AS (
       -- Get latest volume for each market
       SELECT DISTINCT ON (market_id)
         market_id,
         volume_24h as current_volume
       FROM market_snapshots
       WHERE scan_timestamp > NOW() - INTERVAL '1 hour'
         AND volume_24h IS NOT NULL
       ORDER BY market_id, scan_timestamp DESC
     ),
     avg_volume AS (
       -- Get average volume over past 24 hours
       SELECT
         market_id,
         AVG(volume_24h) as avg_volume
       FROM market_snapshots
       WHERE scan_timestamp > NOW() - INTERVAL '24 hours'
         AND volume_24h IS NOT NULL
       GROUP BY market_id
       HAVING AVG(volume_24h) > 0
     )
     SELECT
       r.market_id,
       r.current_volume,
       a.avg_volume,
       (r.current_volume / a.avg_volume) as multiplier
     FROM recent_volume r
     JOIN avg_volume a ON r.market_id = a.market_id
     WHERE (r.current_volume / a.avg_volume) >= $1
     ORDER BY multiplier DESC
     LIMIT 20`,
    [spikeMultiplier]
  );

  return result.rows.map(row => ({
    marketId: row.market_id,
    currentVolume: parseFloat(row.current_volume),
    avgVolume: parseFloat(row.avg_volume),
    multiplier: parseFloat(row.multiplier),
  }));
}

/**
 * Detect thin order books on high-volume markets.
 * Markets with low liquidity relative to volume are opportunities for market makers.
 */
export async function detectThinBookFromWS(
  minVolume: number = 10000,
  maxLiquidity: number = 500
): Promise<Array<{
  marketId: string;
  volume24h: number;
  totalLiquidity: number;
  volumeToLiquidityRatio: number;
}>> {
  const result = await query(
    `WITH latest_volume AS (
       SELECT DISTINCT ON (market_id)
         market_id,
         volume_24h
       FROM market_snapshots
       WHERE scan_timestamp > NOW() - INTERVAL '1 hour'
         AND volume_24h IS NOT NULL
       ORDER BY market_id, scan_timestamp DESC
     ),
     latest_liquidity AS (
       SELECT DISTINCT ON (market_id)
         market_id,
         COALESCE(best_bid_size, 0) + COALESCE(best_ask_size, 0) as total_liquidity
       FROM order_book_snapshots
       WHERE scan_timestamp > NOW() - INTERVAL '5 minutes'
       ORDER BY market_id, scan_timestamp DESC
     )
     SELECT
       v.market_id,
       v.volume_24h,
       COALESCE(l.total_liquidity, 0) as total_liquidity,
       CASE WHEN COALESCE(l.total_liquidity, 0) > 0
            THEN v.volume_24h / l.total_liquidity
            ELSE 9999 END as volume_to_liquidity_ratio
     FROM latest_volume v
     LEFT JOIN latest_liquidity l ON v.market_id = l.market_id
     WHERE v.volume_24h >= $1
       AND COALESCE(l.total_liquidity, 0) < $2
     ORDER BY volume_to_liquidity_ratio DESC
     LIMIT 20`,
    [minVolume, maxLiquidity]
  );

  return result.rows.map(row => ({
    marketId: row.market_id,
    volume24h: parseFloat(row.volume_24h),
    totalLiquidity: parseFloat(row.total_liquidity),
    volumeToLiquidityRatio: parseFloat(row.volume_to_liquidity_ratio),
  }));
}

/**
 * Detect mispricing between related markets.
 * Looks for markets with similar questions but inconsistent prices.
 * Note: This is a simplified version that looks for price anomalies in the same category.
 */
export async function detectMispricingFromWS(): Promise<Array<{
  marketId1: string;
  marketId2: string;
  question1: string;
  question2: string;
  price1: number;
  price2: number;
  priceDifference: number;
}>> {
  // This query looks for markets in the same category with significant YES price differences
  // that might indicate mispricing (e.g., related events priced inconsistently)
  const result = await query(
    `WITH latest_prices AS (
       SELECT DISTINCT ON (o.market_id)
         o.market_id,
         m.question,
         m.category,
         o.mid_price as yes_price
       FROM order_book_snapshots o
       JOIN market_snapshots m ON o.market_id = m.market_id
       WHERE o.scan_timestamp > NOW() - INTERVAL '5 minutes'
         AND o.token_side = 'YES'
         AND o.mid_price IS NOT NULL
         AND m.category IS NOT NULL
       ORDER BY o.market_id, o.scan_timestamp DESC
     )
     SELECT
       p1.market_id as market_id_1,
       p2.market_id as market_id_2,
       p1.question as question_1,
       p2.question as question_2,
       p1.yes_price as price_1,
       p2.yes_price as price_2,
       ABS(p1.yes_price - p2.yes_price) as price_difference
     FROM latest_prices p1
     JOIN latest_prices p2 ON p1.category = p2.category
       AND p1.market_id < p2.market_id
     WHERE ABS(p1.yes_price - p2.yes_price) > 0.1
       AND p1.yes_price BETWEEN 0.2 AND 0.8
       AND p2.yes_price BETWEEN 0.2 AND 0.8
     ORDER BY price_difference DESC
     LIMIT 10`
  );

  return result.rows.map(row => ({
    marketId1: row.market_id_1,
    marketId2: row.market_id_2,
    question1: row.question_1 || '',
    question2: row.question_2 || '',
    price1: parseFloat(row.price_1),
    price2: parseFloat(row.price_2),
    priceDifference: parseFloat(row.price_difference),
  }));
}

/**
 * Run all opportunity detections and return combined results.
 */
export async function detectAllOpportunitiesFromWS(config: {
  arbitrageThreshold?: number;
  wideSpreadThreshold?: number;
  volumeSpikeMultiplier?: number;
  minVolumeForThinBook?: number;
  maxLiquidityForThinBook?: number;
} = {}): Promise<{
  arbitrage: Awaited<ReturnType<typeof detectArbitrageFromWS>>;
  wideSpread: Awaited<ReturnType<typeof detectWideSpreadFromWS>>;
  volumeSpike: Awaited<ReturnType<typeof detectVolumeSpikeFromWS>>;
  thinBook: Awaited<ReturnType<typeof detectThinBookFromWS>>;
  mispricing: Awaited<ReturnType<typeof detectMispricingFromWS>>;
  summary: {
    arbitrageCount: number;
    wideSpreadCount: number;
    volumeSpikeCount: number;
    thinBookCount: number;
    mispricingCount: number;
    totalCount: number;
  };
}> {
  const [arbitrage, wideSpread, volumeSpike, thinBook, mispricing] = await Promise.all([
    detectArbitrageFromWS(config.arbitrageThreshold || 0.995),
    detectWideSpreadFromWS(config.wideSpreadThreshold || 0.05),
    detectVolumeSpikeFromWS(config.volumeSpikeMultiplier || 3.0),
    detectThinBookFromWS(config.minVolumeForThinBook || 10000, config.maxLiquidityForThinBook || 500),
    detectMispricingFromWS(),
  ]);

  return {
    arbitrage,
    wideSpread,
    volumeSpike,
    thinBook,
    mispricing,
    summary: {
      arbitrageCount: arbitrage.length,
      wideSpreadCount: wideSpread.length,
      volumeSpikeCount: volumeSpike.length,
      thinBookCount: thinBook.length,
      mispricingCount: mispricing.length,
      totalCount: arbitrage.length + wideSpread.length + volumeSpike.length + thinBook.length + mispricing.length,
    },
  };
}

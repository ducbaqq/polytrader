/**
 * Repository for market_snapshots table operations.
 */

import { PoolClient } from 'pg';
import { batchInsert, query, queryRows, queryOne } from './index';
import { MarketData } from '../types';

export interface DBMarketSnapshot {
  id: number;
  market_id: string;
  scan_timestamp: Date;
  question: string | null;
  category: string | null;
  end_date: Date | null;
  volume_24h: number | null;
  volume_1h: number | null;
  last_trade_time: Date | null;
  num_makers: number | null;
  status: string | null;
  created_at: Date;
}

/**
 * Insert a batch of market snapshots.
 * Returns the inserted snapshot IDs mapped by market_id.
 */
export async function insertMarketSnapshots(
  client: PoolClient,
  markets: MarketData[],
  scanTimestamp: Date
): Promise<Map<string, number>> {
  if (markets.length === 0) return new Map();

  const columns = [
    'market_id',
    'scan_timestamp',
    'question',
    'category',
    'end_date',
    'volume_24h',
    'volume_1h',
    'last_trade_time',
    'num_makers',
    'status',
  ];

  const rows = markets.map((m) => [
    m.marketId,
    scanTimestamp,
    m.question || null,
    m.category || null,
    m.endDate || null,
    m.volume24h || 0,
    m.volume1h || 0,
    m.timeSinceLastTrade ? new Date(Date.now() - m.timeSinceLastTrade * 1000) : null,
    m.totalActiveMakers || 0,
    'active',
  ]);

  // Insert and get IDs back
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
    INSERT INTO market_snapshots (${columns.join(', ')})
    VALUES ${placeholders.join(', ')}
    RETURNING id, market_id
  `;

  const result = await client.query(sql, values);

  const idMap = new Map<string, number>();
  for (const row of result.rows) {
    idMap.set(row.market_id, row.id);
  }

  return idMap;
}

/**
 * Get the latest snapshot for a market.
 */
export async function getLatestMarketSnapshot(
  marketId: string
): Promise<DBMarketSnapshot | null> {
  return queryOne<DBMarketSnapshot>(
    `SELECT * FROM market_snapshots
     WHERE market_id = $1
     ORDER BY scan_timestamp DESC
     LIMIT 1`,
    [marketId]
  );
}

/**
 * Get market snapshots for a time range.
 */
export async function getMarketSnapshots(
  marketId: string,
  startTime: Date,
  endTime: Date
): Promise<DBMarketSnapshot[]> {
  return queryRows<DBMarketSnapshot>(
    `SELECT * FROM market_snapshots
     WHERE market_id = $1
       AND scan_timestamp BETWEEN $2 AND $3
     ORDER BY scan_timestamp ASC`,
    [marketId, startTime, endTime]
  );
}

/**
 * Get top markets by volume in the last N hours.
 */
export async function getTopMarketsByVolume(
  hours: number = 24,
  limit: number = 100
): Promise<{ market_id: string; avg_volume: number }[]> {
  return queryRows(
    `SELECT market_id, AVG(volume_24h) as avg_volume
     FROM market_snapshots
     WHERE scan_timestamp > NOW() - INTERVAL '${hours} hours'
     GROUP BY market_id
     ORDER BY avg_volume DESC
     LIMIT $1`,
    [limit]
  );
}

/**
 * Get distinct market IDs from snapshots.
 */
export async function getDistinctMarketIds(): Promise<string[]> {
  const rows = await queryRows<{ market_id: string }>(
    `SELECT DISTINCT market_id FROM market_snapshots`
  );
  return rows.map((r) => r.market_id);
}

/**
 * Get scan count.
 */
export async function getScanCount(): Promise<number> {
  const result = await queryOne<{ count: string }>(
    `SELECT COUNT(DISTINCT scan_timestamp) as count FROM market_snapshots`
  );
  return parseInt(result?.count || '0', 10);
}

/**
 * Delete old snapshots (for data retention).
 */
export async function deleteOldSnapshots(daysToKeep: number = 7): Promise<number> {
  const result = await query(
    `DELETE FROM market_snapshots
     WHERE scan_timestamp < NOW() - INTERVAL '${daysToKeep} days'`
  );
  return result.rowCount || 0;
}

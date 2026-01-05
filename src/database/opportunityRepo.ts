/**
 * Repository for opportunities table operations.
 */

import { PoolClient } from 'pg';
import { query, queryRows, queryOne } from './index';
import { Opportunity, OpportunityType, getYesPrice, getNoPrice } from '../types';

export interface DBOpportunity {
  id: number;
  opportunity_type: string;
  market_id: string;
  detected_at: Date;
  expired_at: Date | null;
  duration_seconds: number | null;
  yes_price: number | null;
  no_price: number | null;
  yes_no_sum: number | null;
  spread_percent: number | null;
  available_liquidity: number | null;
  market_volume_24h: number | null;
  market_category: string | null;
  market_age_hours: number | null;
  theoretical_profit_pct: number | null;
  theoretical_profit_usd: number | null;
  still_active: boolean;
  notes: string | null;
}

/**
 * Upsert opportunities - insert new ones, update existing, expire disappeared.
 */
export async function upsertOpportunities(
  client: PoolClient,
  opportunities: Opportunity[],
  scanTimestamp: Date
): Promise<{ inserted: number; updated: number; expired: number }> {
  // Get currently active opportunities
  const activeOps = await client.query<DBOpportunity>(
    `SELECT * FROM opportunities WHERE still_active = TRUE`
  );

  const activeMap = new Map<string, DBOpportunity>();
  for (const op of activeOps.rows) {
    const key = `${op.market_id}:${op.opportunity_type}`;
    activeMap.set(key, op);
  }

  const seenKeys = new Set<string>();
  let inserted = 0;
  let updated = 0;

  // Process current opportunities
  for (const op of opportunities) {
    const key = `${op.marketId}:${op.type}`;
    seenKeys.add(key);

    const existing = activeMap.get(key);

    if (existing) {
      // Update existing - just mark that we saw it (could update prices too)
      updated++;
    } else {
      // Insert new opportunity
      await insertOpportunity(client, op, scanTimestamp);
      inserted++;
    }
  }

  // Expire opportunities that disappeared
  let expired = 0;
  for (const [key, op] of activeMap) {
    if (!seenKeys.has(key)) {
      const duration = Math.floor(
        (scanTimestamp.getTime() - op.detected_at.getTime()) / 1000
      );
      await client.query(
        `UPDATE opportunities
         SET still_active = FALSE, expired_at = $1, duration_seconds = $2
         WHERE id = $3`,
        [scanTimestamp, duration, op.id]
      );
      expired++;
    }
  }

  return { inserted, updated, expired };
}

/**
 * Insert a single opportunity.
 */
export async function insertOpportunity(
  client: PoolClient,
  op: Opportunity,
  detectedAt: Date
): Promise<number> {
  // Map opportunity fields to DB columns
  // The Opportunity type has different field names than our DB schema
  const yesNoSum = op.yesNoSum || null;
  const spreadPct = op.spreadPct || null;
  const availableLiquidity = op.availableLiquidity || null;
  const volume = op.volume || op.currentVolume || null;

  const result = await client.query(
    `INSERT INTO opportunities (
       opportunity_type, market_id, detected_at,
       yes_no_sum, spread_percent,
       available_liquidity, market_volume_24h,
       theoretical_profit_usd,
       still_active, notes
     ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
     RETURNING id`,
    [
      op.type,
      op.marketId,
      detectedAt,
      yesNoSum,
      spreadPct,
      availableLiquidity,
      volume,
      op.potentialProfit || null,
      true,
      op.description || null,
    ]
  );

  return result.rows[0].id;
}

/**
 * Get active opportunities.
 */
export async function getActiveOpportunities(): Promise<DBOpportunity[]> {
  return queryRows<DBOpportunity>(
    `SELECT * FROM opportunities WHERE still_active = TRUE ORDER BY detected_at DESC`
  );
}

/**
 * Get opportunities by type.
 */
export async function getOpportunitiesByType(
  type: OpportunityType,
  limit: number = 100
): Promise<DBOpportunity[]> {
  return queryRows<DBOpportunity>(
    `SELECT * FROM opportunities
     WHERE opportunity_type = $1
     ORDER BY detected_at DESC
     LIMIT $2`,
    [type, limit]
  );
}

/**
 * Get opportunity statistics.
 */
export async function getOpportunityStats(): Promise<{
  total: number;
  by_type: Record<string, number>;
  avg_duration_by_type: Record<string, number>;
}> {
  const total = await queryOne<{ count: string }>(
    `SELECT COUNT(*) as count FROM opportunities`
  );

  const byType = await queryRows<{ opportunity_type: string; count: string }>(
    `SELECT opportunity_type, COUNT(*) as count
     FROM opportunities
     GROUP BY opportunity_type`
  );

  const avgDuration = await queryRows<{ opportunity_type: string; avg_duration: string }>(
    `SELECT opportunity_type, AVG(duration_seconds) as avg_duration
     FROM opportunities
     WHERE duration_seconds IS NOT NULL
     GROUP BY opportunity_type`
  );

  const byTypeMap: Record<string, number> = {};
  for (const row of byType) {
    byTypeMap[row.opportunity_type] = parseInt(row.count, 10);
  }

  const avgDurationMap: Record<string, number> = {};
  for (const row of avgDuration) {
    avgDurationMap[row.opportunity_type] = parseFloat(row.avg_duration);
  }

  return {
    total: parseInt(total?.count || '0', 10),
    by_type: byTypeMap,
    avg_duration_by_type: avgDurationMap,
  };
}

/**
 * Get arbitrage opportunities in a time range.
 */
export async function getArbitrageOpportunities(
  startTime: Date,
  endTime: Date
): Promise<DBOpportunity[]> {
  return queryRows<DBOpportunity>(
    `SELECT * FROM opportunities
     WHERE opportunity_type = 'ARBITRAGE'
       AND detected_at BETWEEN $1 AND $2
     ORDER BY detected_at ASC`,
    [startTime, endTime]
  );
}

/**
 * Get opportunities by hour for pattern analysis.
 */
export async function getOpportunitiesByHour(
  date: Date
): Promise<{ hour: number; type: string; count: number }[]> {
  const startOfDay = new Date(date);
  startOfDay.setHours(0, 0, 0, 0);
  const endOfDay = new Date(date);
  endOfDay.setHours(23, 59, 59, 999);

  return queryRows(
    `SELECT
       EXTRACT(HOUR FROM detected_at)::INTEGER as hour,
       opportunity_type as type,
       COUNT(*) as count
     FROM opportunities
     WHERE detected_at BETWEEN $1 AND $2
     GROUP BY EXTRACT(HOUR FROM detected_at), opportunity_type
     ORDER BY hour, type`,
    [startOfDay, endOfDay]
  );
}

/**
 * Expire all stale opportunities (safety cleanup).
 */
export async function expireStaleOpportunities(
  maxAgeMinutes: number = 60
): Promise<number> {
  const result = await query(
    `UPDATE opportunities
     SET still_active = FALSE,
         expired_at = NOW(),
         duration_seconds = EXTRACT(EPOCH FROM (NOW() - detected_at))::INTEGER
     WHERE still_active = TRUE
       AND detected_at < NOW() - INTERVAL '${maxAgeMinutes} minutes'`
  );
  return result.rowCount || 0;
}

/**
 * Repository for crypto trading tables.
 */

import { PoolClient } from 'pg';
import { query, queryRows, queryOne, withTransaction } from './index';
import {
  CryptoAsset,
  CryptoMarket,
  CryptoOpportunity,
  CryptoPosition,
  CryptoPriceLogRow,
  CryptoMarketsRow,
  CryptoOpportunitiesRow,
  CryptoPositionsRow,
  ThresholdDirection,
} from '../crypto/cryptoTypes';

// ============================================================================
// CRYPTO PRICE LOG
// ============================================================================

export async function logCryptoPrice(
  asset: CryptoAsset,
  price: number,
  change1m: number | null,
  change5m: number | null,
  isSignificantMove: boolean
): Promise<number> {
  const result = await query(
    `INSERT INTO crypto_price_log (asset, price, change_1m, change_5m, is_significant_move)
     VALUES ($1, $2, $3, $4, $5)
     RETURNING id`,
    [asset, price, change1m, change5m, isSignificantMove]
  );
  return result.rows[0].id;
}

export async function getRecentPrices(
  asset: CryptoAsset,
  minutes: number = 5
): Promise<Array<{ price: number; timestamp: Date }>> {
  const rows = await queryRows<CryptoPriceLogRow>(
    `SELECT * FROM crypto_price_log
     WHERE asset = $1 AND timestamp > NOW() - INTERVAL '${minutes} minutes'
     ORDER BY timestamp DESC`,
    [asset]
  );

  return rows.map((r) => ({
    price: parseFloat(String(r.price)),
    timestamp: r.timestamp,
  }));
}

export async function getSignificantMoves(
  minutes: number = 60
): Promise<Array<{ asset: CryptoAsset; price: number; change1m: number; timestamp: Date }>> {
  const rows = await queryRows<CryptoPriceLogRow>(
    `SELECT * FROM crypto_price_log
     WHERE is_significant_move = true
       AND timestamp > NOW() - INTERVAL '${minutes} minutes'
     ORDER BY timestamp DESC`
  );

  return rows.map((r) => ({
    asset: r.asset as CryptoAsset,
    price: parseFloat(String(r.price)),
    change1m: parseFloat(String(r.change_1m || '0')),
    timestamp: r.timestamp,
  }));
}

// ============================================================================
// CRYPTO MARKETS
// ============================================================================

export async function upsertCryptoMarket(market: CryptoMarket): Promise<number> {
  const result = await query(
    `INSERT INTO crypto_markets (
       market_id, question, asset, threshold, direction,
       resolution_date, volume_24h, is_whitelisted, status
     ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
     ON CONFLICT (market_id) DO UPDATE SET
       question = EXCLUDED.question,
       asset = EXCLUDED.asset,
       threshold = EXCLUDED.threshold,
       direction = EXCLUDED.direction,
       resolution_date = EXCLUDED.resolution_date,
       volume_24h = EXCLUDED.volume_24h,
       is_whitelisted = EXCLUDED.is_whitelisted,
       status = EXCLUDED.status
     RETURNING id`,
    [
      market.marketId,
      market.question,
      market.asset,
      market.threshold,
      market.direction,
      market.resolutionDate,
      market.volume24h,
      market.isWhitelisted,
      market.status,
    ]
  );
  return result.rows[0].id;
}

export async function getActiveCryptoMarkets(): Promise<CryptoMarket[]> {
  const rows = await queryRows<CryptoMarketsRow>(
    `SELECT * FROM crypto_markets WHERE status = 'ACTIVE'`
  );

  return rows.map(rowToCryptoMarket);
}

export async function getCryptoMarketsByAsset(asset: CryptoAsset): Promise<CryptoMarket[]> {
  const rows = await queryRows<CryptoMarketsRow>(
    `SELECT * FROM crypto_markets WHERE asset = $1 AND status = 'ACTIVE'`,
    [asset]
  );

  return rows.map(rowToCryptoMarket);
}

export async function getCryptoMarket(marketId: string): Promise<CryptoMarket | null> {
  const row = await queryOne<CryptoMarketsRow>(
    `SELECT * FROM crypto_markets WHERE market_id = $1`,
    [marketId]
  );

  return row ? rowToCryptoMarket(row) : null;
}

export async function updateCryptoMarketStatus(
  marketId: string,
  status: 'ACTIVE' | 'INACTIVE' | 'RESOLVED'
): Promise<void> {
  await query(
    `UPDATE crypto_markets SET status = $1 WHERE market_id = $2`,
    [status, marketId]
  );
}

export async function setCryptoMarketWhitelisted(
  marketId: string,
  isWhitelisted: boolean
): Promise<void> {
  await query(
    `UPDATE crypto_markets SET is_whitelisted = $1 WHERE market_id = $2`,
    [isWhitelisted, marketId]
  );
}

function rowToCryptoMarket(row: CryptoMarketsRow): CryptoMarket {
  return {
    id: row.id,
    marketId: row.market_id,
    question: row.question,
    asset: row.asset as CryptoAsset,
    threshold: parseFloat(String(row.threshold)),
    direction: row.direction as ThresholdDirection,
    resolutionDate: row.resolution_date,
    volume24h: parseFloat(String(row.volume_24h || '0')),
    isWhitelisted: row.is_whitelisted,
    discoveredAt: row.discovered_at,
    status: row.status as 'ACTIVE' | 'INACTIVE' | 'RESOLVED',
  };
}

// ============================================================================
// CRYPTO OPPORTUNITIES
// ============================================================================

export async function insertCryptoOpportunity(opp: CryptoOpportunity): Promise<number> {
  const result = await query(
    `INSERT INTO crypto_opportunities (
       opportunity_id, market_id, asset, threshold, binance_price,
       expected_poly_price, actual_poly_price, gap_percent, side, executed, status
     ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
     RETURNING id`,
    [
      opp.opportunityId,
      opp.marketId,
      opp.asset,
      opp.threshold,
      opp.binancePrice,
      opp.expectedPolyPrice,
      opp.actualPolyPrice,
      opp.gapPercent,
      opp.side,
      opp.executed,
      opp.status,
    ]
  );
  return result.rows[0].id;
}

export async function updateCryptoOpportunityStatus(
  opportunityId: string,
  status: string,
  executed: boolean = false
): Promise<void> {
  await query(
    `UPDATE crypto_opportunities SET status = $1, executed = $2 WHERE opportunity_id = $3`,
    [status, executed, opportunityId]
  );
}

export async function getRecentOpportunities(limit: number = 10): Promise<CryptoOpportunity[]> {
  const rows = await queryRows<CryptoOpportunitiesRow>(
    `SELECT * FROM crypto_opportunities
     ORDER BY detected_at DESC
     LIMIT $1`,
    [limit]
  );

  return rows.map(rowToCryptoOpportunity);
}

export async function getOpportunitiesByStatus(status: string): Promise<CryptoOpportunity[]> {
  const rows = await queryRows<CryptoOpportunitiesRow>(
    `SELECT * FROM crypto_opportunities WHERE status = $1 ORDER BY detected_at DESC`,
    [status]
  );

  return rows.map(rowToCryptoOpportunity);
}

export async function getTodayOpportunityStats(): Promise<{
  total: number;
  executed: number;
  skipped: number;
}> {
  const result = await queryOne<{
    total: string;
    executed: string;
    skipped: string;
  }>(
    `SELECT
       COUNT(*) as total,
       SUM(CASE WHEN executed = true THEN 1 ELSE 0 END) as executed,
       SUM(CASE WHEN status = 'SKIPPED' THEN 1 ELSE 0 END) as skipped
     FROM crypto_opportunities
     WHERE detected_at >= CURRENT_DATE`
  );

  return {
    total: parseInt(result?.total || '0', 10),
    executed: parseInt(result?.executed || '0', 10),
    skipped: parseInt(result?.skipped || '0', 10),
  };
}

function rowToCryptoOpportunity(row: CryptoOpportunitiesRow): CryptoOpportunity {
  return {
    id: row.id,
    opportunityId: row.opportunity_id,
    marketId: row.market_id,
    detectedAt: row.detected_at,
    asset: row.asset as CryptoAsset,
    threshold: parseFloat(String(row.threshold || '0')),
    binancePrice: parseFloat(String(row.binance_price)),
    expectedPolyPrice: parseFloat(String(row.expected_poly_price || '0')),
    actualPolyPrice: parseFloat(String(row.actual_poly_price || '0')),
    gapPercent: parseFloat(String(row.gap_percent || '0')),
    side: (row.side || 'YES') as 'YES' | 'NO',
    executed: row.executed,
    status: row.status as CryptoOpportunity['status'],
  };
}

// ============================================================================
// CRYPTO POSITIONS
// ============================================================================

export async function insertCryptoPosition(
  client: PoolClient,
  pos: CryptoPosition
): Promise<number> {
  const result = await client.query(
    `INSERT INTO crypto_positions (
       position_id, market_id, asset, side, entry_price, quantity,
       entry_time, binance_price_at_entry, status
     ) VALUES ($1, $2, $3, $4, $5, $6, NOW(), $7, $8)
     RETURNING id`,
    [
      pos.positionId,
      pos.marketId,
      pos.asset,
      pos.side,
      pos.entryPrice,
      pos.quantity,
      pos.binancePriceAtEntry,
      pos.status,
    ]
  );
  return result.rows[0].id;
}

export async function getOpenCryptoPositions(): Promise<CryptoPosition[]> {
  const rows = await queryRows<CryptoPositionsRow>(
    `SELECT * FROM crypto_positions WHERE status = 'OPEN' ORDER BY entry_time ASC`
  );

  return rows.map(rowToCryptoPosition);
}

export async function getCryptoPosition(positionId: string): Promise<CryptoPosition | null> {
  const row = await queryOne<CryptoPositionsRow>(
    `SELECT * FROM crypto_positions WHERE position_id = $1`,
    [positionId]
  );

  return row ? rowToCryptoPosition(row) : null;
}

export async function closeCryptoPosition(
  client: PoolClient,
  positionId: string,
  exitPrice: number,
  exitReason: 'PROFIT' | 'STOP' | 'TIME' | 'REVERSAL',
  pnl: number
): Promise<void> {
  await client.query(
    `UPDATE crypto_positions
     SET status = 'CLOSED',
         exit_price = $1,
         exit_time = NOW(),
         exit_reason = $2,
         pnl = $3
     WHERE position_id = $4`,
    [exitPrice, exitReason, pnl, positionId]
  );
}

export async function updateCryptoPositionStatus(
  positionId: string,
  status: 'OPEN' | 'CLOSING' | 'CLOSED'
): Promise<void> {
  await query(
    `UPDATE crypto_positions SET status = $1 WHERE position_id = $2`,
    [status, positionId]
  );
}

export async function getCryptoPositionStats(): Promise<{
  openPositions: number;
  totalExposure: number;
  todayPnl: number;
  todayTrades: number;
}> {
  const result = await queryOne<{
    open_positions: string;
    total_exposure: string;
    today_pnl: string;
    today_trades: string;
  }>(
    `SELECT
       (SELECT COUNT(*) FROM crypto_positions WHERE status = 'OPEN') as open_positions,
       (SELECT COALESCE(SUM(quantity * entry_price), 0) FROM crypto_positions WHERE status = 'OPEN') as total_exposure,
       (SELECT COALESCE(SUM(pnl), 0) FROM crypto_positions WHERE exit_time >= CURRENT_DATE) as today_pnl,
       (SELECT COUNT(*) FROM crypto_positions WHERE entry_time >= CURRENT_DATE) as today_trades`
  );

  return {
    openPositions: parseInt(result?.open_positions || '0', 10),
    totalExposure: parseFloat(result?.total_exposure || '0'),
    todayPnl: parseFloat(result?.today_pnl || '0'),
    todayTrades: parseInt(result?.today_trades || '0', 10),
  };
}

export async function getClosedPositions(limit: number = 50): Promise<CryptoPosition[]> {
  const rows = await queryRows<CryptoPositionsRow>(
    `SELECT * FROM crypto_positions
     WHERE status = 'CLOSED'
     ORDER BY exit_time DESC
     LIMIT $1`,
    [limit]
  );

  return rows.map(rowToCryptoPosition);
}

function rowToCryptoPosition(row: CryptoPositionsRow): CryptoPosition {
  return {
    id: row.id,
    positionId: row.position_id,
    marketId: row.market_id,
    asset: row.asset as CryptoAsset,
    side: row.side as 'YES' | 'NO',
    entryPrice: parseFloat(String(row.entry_price)),
    quantity: parseFloat(String(row.quantity)),
    entryTime: row.entry_time,
    binancePriceAtEntry: parseFloat(String(row.binance_price_at_entry || '0')),
    exitPrice: row.exit_price ? parseFloat(String(row.exit_price)) : undefined,
    exitTime: row.exit_time || undefined,
    exitReason: row.exit_reason as CryptoPosition['exitReason'],
    pnl: row.pnl ? parseFloat(String(row.pnl)) : undefined,
    status: row.status as 'OPEN' | 'CLOSING' | 'CLOSED',
  };
}

// ============================================================================
// AGGREGATE QUERIES
// ============================================================================

export async function getCryptoDailyStats(): Promise<{
  totalPositions: number;
  winRate: number;
  totalPnl: number;
  avgHoldTime: number;
  avgPnlPerTrade: number;
}> {
  const result = await queryOne<{
    total_positions: string;
    wins: string;
    total_pnl: string;
    avg_hold_time: string;
  }>(
    `SELECT
       COUNT(*) as total_positions,
       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
       COALESCE(SUM(pnl), 0) as total_pnl,
       COALESCE(AVG(EXTRACT(EPOCH FROM (exit_time - entry_time))), 0) as avg_hold_time
     FROM crypto_positions
     WHERE status = 'CLOSED'
       AND exit_time >= CURRENT_DATE`
  );

  const total = parseInt(result?.total_positions || '0', 10);
  const wins = parseInt(result?.wins || '0', 10);
  const totalPnl = parseFloat(result?.total_pnl || '0');

  return {
    totalPositions: total,
    winRate: total > 0 ? wins / total : 0,
    totalPnl,
    avgHoldTime: parseFloat(result?.avg_hold_time || '0'),
    avgPnlPerTrade: total > 0 ? totalPnl / total : 0,
  };
}

export async function getCryptoAllTimeStats(): Promise<{
  totalPositions: number;
  winRate: number;
  totalPnl: number;
  bestTrade: number;
  worstTrade: number;
}> {
  const result = await queryOne<{
    total_positions: string;
    wins: string;
    total_pnl: string;
    best_trade: string;
    worst_trade: string;
  }>(
    `SELECT
       COUNT(*) as total_positions,
       SUM(CASE WHEN pnl > 0 THEN 1 ELSE 0 END) as wins,
       COALESCE(SUM(pnl), 0) as total_pnl,
       COALESCE(MAX(pnl), 0) as best_trade,
       COALESCE(MIN(pnl), 0) as worst_trade
     FROM crypto_positions
     WHERE status = 'CLOSED'`
  );

  const total = parseInt(result?.total_positions || '0', 10);
  const wins = parseInt(result?.wins || '0', 10);

  return {
    totalPositions: total,
    winRate: total > 0 ? wins / total : 0,
    totalPnl: parseFloat(result?.total_pnl || '0'),
    bestTrade: parseFloat(result?.best_trade || '0'),
    worstTrade: parseFloat(result?.worst_trade || '0'),
  };
}

// ============================================================================
// CLEANUP / MAINTENANCE
// ============================================================================

export async function pruneOldPriceLogs(daysToKeep: number = 7): Promise<number> {
  const result = await query(
    `DELETE FROM crypto_price_log
     WHERE timestamp < NOW() - INTERVAL '${daysToKeep} days'`
  );
  return result.rowCount || 0;
}

export async function pruneOldOpportunities(daysToKeep: number = 30): Promise<number> {
  const result = await query(
    `DELETE FROM crypto_opportunities
     WHERE detected_at < NOW() - INTERVAL '${daysToKeep} days'`
  );
  return result.rowCount || 0;
}

export async function clearCryptoData(): Promise<void> {
  // Clear in order respecting potential foreign keys
  await query('TRUNCATE TABLE crypto_positions CASCADE');
  await query('TRUNCATE TABLE crypto_opportunities CASCADE');
  await query('TRUNCATE TABLE crypto_price_log CASCADE');
  await query('TRUNCATE TABLE crypto_markets CASCADE');
  console.log('Cleared all crypto tables');
}

/**
 * Repository for paper trading tables.
 */

import { PoolClient } from 'pg';
import { query, queryRows, queryOne, withTransaction } from './index';

// ============ PAPER MARKETS ============

export interface DBPaperMarket {
  id: number;
  market_id: string;
  question: string | null;
  selection_reason: string | null;
  category: string | null;
  volume_24h: number | null;
  selected_at: Date;
  status: string;
  allocated_capital: number | null;
}

export async function insertPaperMarket(
  marketId: string,
  question: string | null,
  selectionReason: string,
  category: string | null,
  volume24h: number,
  allocatedCapital: number
): Promise<number> {
  const result = await query(
    `INSERT INTO paper_markets (market_id, question, selection_reason, category, volume_24h, selected_at, status, allocated_capital)
     VALUES ($1, $2, $3, $4, $5, NOW(), 'ACTIVE', $6)
     ON CONFLICT (market_id) DO UPDATE SET status = 'ACTIVE', selected_at = NOW()
     RETURNING id`,
    [marketId, question, selectionReason, category, volume24h, allocatedCapital]
  );
  return result.rows[0].id;
}

export async function getActivePaperMarkets(): Promise<DBPaperMarket[]> {
  return queryRows<DBPaperMarket>(
    `SELECT * FROM paper_markets WHERE status = 'ACTIVE'`
  );
}

export async function updatePaperMarketStatus(
  marketId: string,
  status: string
): Promise<void> {
  await query(
    `UPDATE paper_markets SET status = $1 WHERE market_id = $2`,
    [status, marketId]
  );
}

// ============ PAPER ORDERS ============

export interface DBPaperOrder {
  id: number;
  market_id: string;
  order_id: string;
  placed_at: Date;
  cancelled_at: Date | null;
  filled_at: Date | null;
  side: string;
  token_side: string;
  order_price: number;
  order_size: number;
  fill_price: number | null;
  fill_size: number | null;
  status: string;
  best_bid_at_order: number | null;
  best_ask_at_order: number | null;
  spread_at_order: number | null;
}

export async function insertPaperOrder(
  client: PoolClient,
  order: {
    marketId: string;
    orderId: string;
    side: 'BUY' | 'SELL';
    tokenSide: 'YES' | 'NO';
    orderPrice: number;
    orderSize: number;
    bestBidAtOrder: number | null;
    bestAskAtOrder: number | null;
    spreadAtOrder: number | null;
  }
): Promise<number> {
  const result = await client.query(
    `INSERT INTO paper_orders (
       market_id, order_id, placed_at, side, token_side,
       order_price, order_size, status,
       best_bid_at_order, best_ask_at_order, spread_at_order
     ) VALUES ($1, $2, NOW(), $3, $4, $5, $6, 'PENDING', $7, $8, $9)
     RETURNING id`,
    [
      order.marketId,
      order.orderId,
      order.side,
      order.tokenSide,
      order.orderPrice,
      order.orderSize,
      order.bestBidAtOrder,
      order.bestAskAtOrder,
      order.spreadAtOrder,
    ]
  );
  return result.rows[0].id;
}

export async function getPendingOrders(): Promise<DBPaperOrder[]> {
  return queryRows<DBPaperOrder>(
    `SELECT * FROM paper_orders WHERE status = 'PENDING' ORDER BY placed_at ASC`
  );
}

export async function fillOrder(
  client: PoolClient,
  orderId: string,
  fillPrice: number,
  fillSize: number
): Promise<void> {
  await client.query(
    `UPDATE paper_orders
     SET status = 'FILLED', filled_at = NOW(), fill_price = $1, fill_size = $2
     WHERE order_id = $3`,
    [fillPrice, fillSize, orderId]
  );
}

export async function cancelOrder(
  client: PoolClient,
  orderId: string
): Promise<void> {
  await client.query(
    `UPDATE paper_orders
     SET status = 'CANCELLED', cancelled_at = NOW()
     WHERE order_id = $1`,
    [orderId]
  );
}

export async function expireOldPendingOrders(
  maxAgeMinutes: number = 5
): Promise<number> {
  const result = await query(
    `UPDATE paper_orders
     SET status = 'EXPIRED', cancelled_at = NOW()
     WHERE status = 'PENDING'
       AND placed_at < NOW() - INTERVAL '${maxAgeMinutes} minutes'`
  );
  return result.rowCount || 0;
}

// ============ PAPER TRADES ============

export interface DBPaperTrade {
  id: number;
  trade_id: string;
  market_id: string;
  order_id: string;
  executed_at: Date;
  side: string;
  token_side: string;
  price: number;
  size: number;
  value: number;
  platform_fee: number;
  gas_cost: number;
  slippage_cost: number;
  total_cost: number;
  net_value: number;
}

export async function insertPaperTrade(
  client: PoolClient,
  trade: {
    tradeId: string;
    marketId: string;
    orderId: string;
    side: 'BUY' | 'SELL';
    tokenSide: 'YES' | 'NO';
    price: number;
    size: number;
    value: number;
    platformFee: number;
    gasCost: number;
    slippageCost: number;
    totalCost: number;
    netValue: number;
  }
): Promise<number> {
  const result = await client.query(
    `INSERT INTO paper_trades (
       trade_id, market_id, order_id, executed_at,
       side, token_side, price, size, value,
       platform_fee, gas_cost, slippage_cost, total_cost, net_value
     ) VALUES ($1, $2, $3, NOW(), $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
     RETURNING id`,
    [
      trade.tradeId,
      trade.marketId,
      trade.orderId,
      trade.side,
      trade.tokenSide,
      trade.price,
      trade.size,
      trade.value,
      trade.platformFee,
      trade.gasCost,
      trade.slippageCost,
      trade.totalCost,
      trade.netValue,
    ]
  );
  return result.rows[0].id;
}

export async function getTradesByMarket(marketId: string): Promise<DBPaperTrade[]> {
  return queryRows<DBPaperTrade>(
    `SELECT * FROM paper_trades WHERE market_id = $1 ORDER BY executed_at ASC`,
    [marketId]
  );
}

export async function getTotalTradeStats(): Promise<{
  total_trades: number;
  total_volume: number;
  total_fees: number;
  net_pnl: number;
}> {
  const result = await queryOne<{
    total_trades: string;
    total_volume: string;
    total_fees: string;
    net_pnl: string;
  }>(
    `SELECT
       COUNT(*) as total_trades,
       SUM(value) as total_volume,
       SUM(total_cost) as total_fees,
       SUM(CASE WHEN side = 'SELL' THEN net_value ELSE -net_value END) as net_pnl
     FROM paper_trades`
  );

  return {
    total_trades: parseInt(result?.total_trades || '0', 10),
    total_volume: parseFloat(result?.total_volume || '0'),
    total_fees: parseFloat(result?.total_fees || '0'),
    net_pnl: parseFloat(result?.net_pnl || '0'),
  };
}

// ============ PAPER POSITIONS ============

export interface DBPaperPosition {
  id: number;
  market_id: string;
  token_side: string;
  updated_at: Date;
  quantity: number;
  average_cost: number;
  cost_basis: number;
  current_price: number | null;
  market_value: number | null;
  unrealized_pnl: number | null;
  unrealized_pnl_pct: number | null;
}

export async function upsertPosition(
  client: PoolClient,
  marketId: string,
  tokenSide: 'YES' | 'NO',
  quantity: number,
  averageCost: number,
  costBasis: number,
  currentPrice: number | null
): Promise<void> {
  const marketValue = currentPrice ? quantity * currentPrice : null;
  const unrealizedPnl = marketValue ? marketValue - costBasis : null;
  // Clamp percentage to prevent numeric overflow (NUMERIC(10,2) max is ~10^8)
  let unrealizedPnlPct: number | null = null;
  if (costBasis > 0.01 && unrealizedPnl !== null) {
    const rawPct = unrealizedPnl / costBasis;
    unrealizedPnlPct = Math.max(-99999999, Math.min(99999999, rawPct));
  }

  await client.query(
    `INSERT INTO paper_positions (
       market_id, token_side, updated_at, quantity, average_cost,
       cost_basis, current_price, market_value, unrealized_pnl, unrealized_pnl_pct
     ) VALUES ($1, $2, NOW(), $3, $4, $5, $6, $7, $8, $9)
     ON CONFLICT (market_id, token_side) DO UPDATE SET
       updated_at = NOW(),
       quantity = $3,
       average_cost = $4,
       cost_basis = $5,
       current_price = $6,
       market_value = $7,
       unrealized_pnl = $8,
       unrealized_pnl_pct = $9`,
    [marketId, tokenSide, quantity, averageCost, costBasis, currentPrice, marketValue, unrealizedPnl, unrealizedPnlPct]
  );
}

export async function getPositions(): Promise<DBPaperPosition[]> {
  return queryRows<DBPaperPosition>(
    `SELECT * FROM paper_positions WHERE quantity != 0 ORDER BY market_id, token_side`
  );
}

export async function getPositionByMarket(
  marketId: string,
  tokenSide: 'YES' | 'NO'
): Promise<DBPaperPosition | null> {
  return queryOne<DBPaperPosition>(
    `SELECT * FROM paper_positions WHERE market_id = $1 AND token_side = $2`,
    [marketId, tokenSide]
  );
}

export async function updatePositionPrices(
  client: PoolClient,
  marketId: string,
  tokenSide: 'YES' | 'NO',
  currentPrice: number
): Promise<void> {
  await client.query(
    `UPDATE paper_positions
     SET current_price = $1,
         market_value = quantity * $1,
         unrealized_pnl = (quantity * $1) - cost_basis,
         unrealized_pnl_pct = CASE WHEN cost_basis > 0 THEN ((quantity * $1) - cost_basis) / cost_basis ELSE NULL END,
         updated_at = NOW()
     WHERE market_id = $2 AND token_side = $3`,
    [currentPrice, marketId, tokenSide]
  );
}

// ============ PAPER P&L ============

export interface DBPaperPnL {
  id: number;
  recorded_at: Date;
  market_id: string | null;
  realized_pnl: number | null;
  realized_pnl_cumulative: number | null;
  unrealized_pnl: number | null;
  total_pnl: number | null;
  cash_balance: number | null;
  position_value: number | null;
  total_equity: number | null;
  trades_today: number | null;
  fill_rate_today: number | null;
  win_rate_today: number | null;
}

export async function recordPnLSnapshot(
  cashBalance: number,
  initialCapital: number = 1000
): Promise<void> {
  // Get position values (parse as floats - PostgreSQL returns numeric as strings)
  const positions = await getPositions();
  const positionValue = positions.reduce((sum, p) => sum + parseFloat(String(p.market_value || 0)), 0);
  const unrealizedPnl = positions.reduce((sum, p) => sum + parseFloat(String(p.unrealized_pnl || 0)), 0);

  // Get realized P&L from trades
  const tradeStats = await getTotalTradeStats();
  const realizedPnl = tradeStats.net_pnl;

  // Calculate totals
  const totalEquity = cashBalance + positionValue;
  const totalPnl = realizedPnl + unrealizedPnl;

  // Get today's stats from orders and trades
  const todayStats = await queryOne<{
    orders_today: string;
    fills_today: string;
    wins_today: string;
  }>(
    `SELECT
       (SELECT COUNT(*) FROM paper_orders WHERE placed_at >= CURRENT_DATE) as orders_today,
       (SELECT COUNT(*) FROM paper_orders WHERE placed_at >= CURRENT_DATE AND status = 'FILLED') as fills_today,
       (SELECT COUNT(*) FROM paper_trades WHERE executed_at >= CURRENT_DATE AND net_value > 0) as wins_today`
  );

  const tradesToday = parseInt(todayStats?.orders_today || '0', 10);
  const fillsToday = parseInt(todayStats?.fills_today || '0', 10);
  const winsToday = parseInt(todayStats?.wins_today || '0', 10);
  const fillRate = tradesToday > 0 ? fillsToday / tradesToday : 0;
  const winRate = fillsToday > 0 ? winsToday / fillsToday : 0;

  await query(
    `INSERT INTO paper_pnl (
       recorded_at, market_id, realized_pnl, realized_pnl_cumulative,
       unrealized_pnl, total_pnl, cash_balance, position_value, total_equity,
       trades_today, fill_rate_today, win_rate_today
     ) VALUES (NOW(), NULL, $1, $1, $2, $3, $4, $5, $6, $7, $8, $9)`,
    [
      realizedPnl,
      unrealizedPnl,
      totalPnl,
      cashBalance,
      positionValue,
      totalEquity,
      tradesToday,
      fillRate,
      winRate,
    ]
  );
}

export async function getLatestPnL(): Promise<DBPaperPnL | null> {
  return queryOne<DBPaperPnL>(
    `SELECT * FROM paper_pnl WHERE market_id IS NULL ORDER BY recorded_at DESC LIMIT 1`
  );
}

export async function getPnLHistory(
  startTime: Date,
  endTime: Date
): Promise<DBPaperPnL[]> {
  return queryRows<DBPaperPnL>(
    `SELECT * FROM paper_pnl
     WHERE market_id IS NULL
       AND recorded_at BETWEEN $1 AND $2
     ORDER BY recorded_at ASC`,
    [startTime, endTime]
  );
}

// ============ MARKET SELECTION QUERIES ============

/**
 * Select a liquid market (highest volume, lowest spread).
 */
export async function selectLiquidMarket(): Promise<{ market_id: string; question: string; avg_volume: number } | null> {
  return queryOne(
    `SELECT ms.market_id, ms.question, AVG(ms.volume_24h) as avg_volume
     FROM market_snapshots ms
     LEFT JOIN paper_markets pm ON ms.market_id = pm.market_id AND pm.status = 'ACTIVE'
     WHERE ms.scan_timestamp > NOW() - INTERVAL '24 hours'
       AND pm.id IS NULL
     GROUP BY ms.market_id, ms.question
     ORDER BY avg_volume DESC
     LIMIT 1`
  );
}

/**
 * Select a medium volume market (good spread opportunities).
 */
export async function selectMediumVolumeMarket(): Promise<{ market_id: string; question: string; avg_volume: number } | null> {
  return queryOne(
    `SELECT ms.market_id, ms.question, AVG(ms.volume_24h) as avg_volume
     FROM market_snapshots ms
     LEFT JOIN order_book_snapshots obs ON ms.id = obs.market_snapshot_id
     LEFT JOIN paper_markets pm ON ms.market_id = pm.market_id AND pm.status = 'ACTIVE'
     WHERE ms.scan_timestamp > NOW() - INTERVAL '24 hours'
       AND ms.volume_24h BETWEEN 20000 AND 50000
       AND pm.id IS NULL
     GROUP BY ms.market_id, ms.question
     ORDER BY AVG(obs.spread_percent) DESC NULLS LAST
     LIMIT 1`
  );
}

/**
 * Select a new market (< 24h old with decent volume).
 */
export async function selectNewMarket(): Promise<{ market_id: string; question: string; volume_24h: number } | null> {
  return queryOne(
    `SELECT market_id, question, volume_24h FROM (
       SELECT DISTINCT ON (ms.market_id) ms.market_id, ms.question, ms.volume_24h, ms.created_at
       FROM market_snapshots ms
       LEFT JOIN paper_markets pm ON ms.market_id = pm.market_id AND pm.status = 'ACTIVE'
       WHERE ms.created_at > NOW() - INTERVAL '24 hours'
         AND ms.volume_24h > 10000
         AND pm.id IS NULL
       ORDER BY ms.market_id, ms.created_at DESC
     ) sub
     ORDER BY created_at DESC
     LIMIT 1`
  );
}

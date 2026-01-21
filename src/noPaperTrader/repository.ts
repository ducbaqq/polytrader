/**
 * Database repository for the No-betting paper trading system.
 * Handles persistence of positions, trades, and portfolio state.
 */

import { query, queryRows, queryOne, withTransaction } from '../database/index';
import { Position, Trade, Portfolio, DailySummary, PositionStatus, TokenSide, StrategyId } from './types';

/**
 * Initialize database tables for No paper trading.
 */
export async function initializeTables(): Promise<void> {
  // Positions table
  await query(`
    CREATE TABLE IF NOT EXISTS no_positions (
      id TEXT PRIMARY KEY,
      strategy_id TEXT NOT NULL DEFAULT 'no-buyer',
      market_id TEXT NOT NULL,
      token_id TEXT NOT NULL,
      token_side TEXT NOT NULL DEFAULT 'NO',
      question TEXT NOT NULL,
      category TEXT NOT NULL,
      entry_price NUMERIC(10, 4) NOT NULL,
      entry_price_after_slippage NUMERIC(10, 4) NOT NULL,
      quantity NUMERIC(10, 4) NOT NULL,
      cost_basis NUMERIC(12, 2) NOT NULL,
      estimated_edge NUMERIC(6, 4) NOT NULL,
      entry_time TIMESTAMP NOT NULL DEFAULT NOW(),
      end_date TIMESTAMP NOT NULL,
      status TEXT NOT NULL DEFAULT 'OPEN',
      exit_price NUMERIC(10, 4),
      exit_time TIMESTAMP,
      exit_reason TEXT,
      realized_pnl NUMERIC(12, 2),
      realized_pnl_percent NUMERIC(8, 4),
      created_at TIMESTAMP NOT NULL DEFAULT NOW()
    )
  `);

  // Add token_side column if it doesn't exist (migration for existing tables)
  await query(`
    DO $$ BEGIN
      ALTER TABLE no_positions ADD COLUMN IF NOT EXISTS token_side TEXT NOT NULL DEFAULT 'NO';
    EXCEPTION WHEN others THEN NULL;
    END $$;
  `);

  // Add strategy_id column if it doesn't exist (migration for existing tables)
  await query(`
    DO $$ BEGIN
      ALTER TABLE no_positions ADD COLUMN IF NOT EXISTS strategy_id TEXT NOT NULL DEFAULT 'no-buyer';
    EXCEPTION WHEN others THEN NULL;
    END $$;
  `);

  // Trades table
  await query(`
    CREATE TABLE IF NOT EXISTS no_trades (
      id TEXT PRIMARY KEY,
      strategy_id TEXT NOT NULL DEFAULT 'no-buyer',
      position_id TEXT NOT NULL,
      market_id TEXT NOT NULL,
      question TEXT NOT NULL,
      category TEXT NOT NULL,
      side TEXT NOT NULL,
      token_side TEXT NOT NULL DEFAULT 'NO',
      price NUMERIC(10, 4) NOT NULL,
      price_after_slippage NUMERIC(10, 4) NOT NULL,
      quantity NUMERIC(10, 4) NOT NULL,
      value NUMERIC(12, 2) NOT NULL,
      slippage_cost NUMERIC(10, 4) NOT NULL,
      timestamp TIMESTAMP NOT NULL DEFAULT NOW(),
      reason TEXT NOT NULL,
      created_at TIMESTAMP NOT NULL DEFAULT NOW()
    )
  `);

  // Add strategy_id column to trades if it doesn't exist (migration)
  await query(`
    DO $$ BEGIN
      ALTER TABLE no_trades ADD COLUMN IF NOT EXISTS strategy_id TEXT NOT NULL DEFAULT 'no-buyer';
    EXCEPTION WHEN others THEN NULL;
    END $$;
  `);

  // Portfolio state table (one row per strategy)
  await query(`
    CREATE TABLE IF NOT EXISTS no_portfolio (
      strategy_id TEXT PRIMARY KEY,
      cash_balance NUMERIC(12, 2) NOT NULL,
      initial_capital NUMERIC(12, 2) NOT NULL,
      realized_pnl NUMERIC(12, 2) NOT NULL DEFAULT 0,
      total_trades INTEGER NOT NULL DEFAULT 0,
      winning_trades INTEGER NOT NULL DEFAULT 0,
      losing_trades INTEGER NOT NULL DEFAULT 0,
      best_trade NUMERIC(12, 2),
      worst_trade NUMERIC(12, 2),
      last_updated TIMESTAMP NOT NULL DEFAULT NOW()
    )
  `);

  // Migration: If old schema exists with id column, migrate to new schema
  // First check if there's data in the old format
  await query(`
    DO $$ BEGIN
      -- Try to add strategy_id if it doesn't exist
      ALTER TABLE no_portfolio ADD COLUMN IF NOT EXISTS strategy_id TEXT;
      -- Migrate old id=1 row to strategy_id='no-buyer' if it exists
      UPDATE no_portfolio SET strategy_id = 'no-buyer' WHERE strategy_id IS NULL;
    EXCEPTION WHEN others THEN NULL;
    END $$;
  `);

  // Daily equity snapshots for equity curve (one row per strategy per day)
  await query(`
    CREATE TABLE IF NOT EXISTS no_daily_snapshots (
      strategy_id TEXT NOT NULL DEFAULT 'no-buyer',
      date DATE NOT NULL,
      starting_equity NUMERIC(12, 2) NOT NULL,
      ending_equity NUMERIC(12, 2) NOT NULL,
      daily_pnl NUMERIC(12, 2) NOT NULL,
      daily_pnl_percent NUMERIC(8, 4) NOT NULL,
      trades_opened INTEGER NOT NULL DEFAULT 0,
      trades_closed INTEGER NOT NULL DEFAULT 0,
      winning_trades INTEGER NOT NULL DEFAULT 0,
      losing_trades INTEGER NOT NULL DEFAULT 0,
      PRIMARY KEY (strategy_id, date)
    )
  `);

  // Migration: add strategy_id to daily_snapshots if it doesn't exist
  await query(`
    DO $$ BEGIN
      ALTER TABLE no_daily_snapshots ADD COLUMN IF NOT EXISTS strategy_id TEXT NOT NULL DEFAULT 'no-buyer';
    EXCEPTION WHEN others THEN NULL;
    END $$;
  `);

  // Scanned markets log (to avoid re-scanning same markets)
  await query(`
    CREATE TABLE IF NOT EXISTS no_scanned_markets (
      market_id TEXT PRIMARY KEY,
      first_scanned_at TIMESTAMP NOT NULL DEFAULT NOW(),
      eligible BOOLEAN NOT NULL,
      rejection_reason TEXT,
      position_opened BOOLEAN NOT NULL DEFAULT FALSE
    )
  `);

  // Create indexes
  await query(`CREATE INDEX IF NOT EXISTS idx_no_positions_status ON no_positions(status)`);
  await query(`CREATE INDEX IF NOT EXISTS idx_no_positions_market ON no_positions(market_id)`);
  await query(`CREATE INDEX IF NOT EXISTS idx_no_trades_position ON no_trades(position_id)`);
  await query(`CREATE INDEX IF NOT EXISTS idx_no_trades_timestamp ON no_trades(timestamp)`);

  console.log('No paper trading tables initialized');
}

/**
 * Initialize portfolio with starting capital for a strategy.
 */
export async function initializePortfolio(initialCapital: number, strategyId: StrategyId): Promise<void> {
  await query(`
    INSERT INTO no_portfolio (strategy_id, cash_balance, initial_capital, realized_pnl, last_updated)
    VALUES ($1, $2, $2, 0, NOW())
    ON CONFLICT (strategy_id) DO NOTHING
  `, [strategyId, initialCapital]);
}

/**
 * Get current portfolio state for a strategy.
 */
export async function getPortfolio(strategyId: StrategyId): Promise<Portfolio | null> {
  const row = await queryOne<any>(`
    SELECT
      strategy_id,
      cash_balance,
      initial_capital,
      realized_pnl,
      total_trades,
      winning_trades,
      losing_trades,
      best_trade,
      worst_trade,
      last_updated
    FROM no_portfolio
    WHERE strategy_id = $1
  `, [strategyId]);

  if (!row) return null;

  // Get open positions for unrealized P&L calculation
  const openPositions = await getOpenPositions(strategyId);
  const openPositionValue = openPositions.reduce((sum, p) => sum + p.costBasis, 0);

  const cashBalance = parseFloat(String(row.cash_balance));
  const initialCapital = parseFloat(String(row.initial_capital)) || 0;
  const totalEquity = cashBalance + openPositionValue;
  const totalTrades = parseInt(String(row.total_trades)) || 0;
  const winningTrades = parseInt(String(row.winning_trades)) || 0;
  const losingTrades = parseInt(String(row.losing_trades)) || 0;
  const realizedPnl = parseFloat(String(row.realized_pnl)) || 0;

  return {
    strategyId,
    cashBalance,
    initialCapital,
    openPositionCount: openPositions.length,
    openPositionValue,
    totalEquity,
    realizedPnl,
    unrealizedPnl: 0, // Calculated when we have current prices
    totalPnl: totalEquity - initialCapital,
    totalPnlPercent: initialCapital > 0 ? ((totalEquity - initialCapital) / initialCapital) * 100 : 0,
    totalTrades,
    winningTrades,
    losingTrades,
    winRate: totalTrades > 0 ? (winningTrades / totalTrades) * 100 : 0,
    avgPnlPerTrade: totalTrades > 0 ? realizedPnl / totalTrades : 0,
    bestTrade: row.best_trade ? parseFloat(String(row.best_trade)) : 0,
    worstTrade: row.worst_trade ? parseFloat(String(row.worst_trade)) : 0,
    lastUpdated: new Date(row.last_updated),
  };
}

/**
 * Get all open positions for a strategy.
 */
export async function getOpenPositions(strategyId: StrategyId): Promise<Position[]> {
  const rows = await queryRows<any>(`
    SELECT *
    FROM no_positions
    WHERE status = 'OPEN' AND strategy_id = $1
    ORDER BY entry_time DESC
  `, [strategyId]);

  return rows.map(rowToPosition);
}

/**
 * Get all positions (open and closed) for a strategy.
 */
export async function getAllPositions(strategyId: StrategyId): Promise<Position[]> {
  const rows = await queryRows<any>(`
    SELECT *
    FROM no_positions
    WHERE strategy_id = $1
    ORDER BY entry_time DESC
  `, [strategyId]);

  return rows.map(rowToPosition);
}

/**
 * Get closed positions only for a strategy.
 */
export async function getClosedPositions(strategyId: StrategyId): Promise<Position[]> {
  const rows = await queryRows<any>(`
    SELECT *
    FROM no_positions
    WHERE status != 'OPEN' AND strategy_id = $1
    ORDER BY exit_time DESC
  `, [strategyId]);

  return rows.map(rowToPosition);
}

/**
 * Get position by ID.
 */
export async function getPosition(positionId: string): Promise<Position | null> {
  const row = await queryOne<any>(`
    SELECT * FROM no_positions WHERE id = $1
  `, [positionId]);

  return row ? rowToPosition(row) : null;
}

/**
 * Check if we already have a position for a market in a specific strategy.
 */
export async function hasPositionForMarket(marketId: string, strategyId: StrategyId): Promise<boolean> {
  const row = await queryOne<any>(`
    SELECT 1 FROM no_positions WHERE market_id = $1 AND status = 'OPEN' AND strategy_id = $2
  `, [marketId, strategyId]);
  return !!row;
}

/**
 * Insert a new position.
 */
export async function insertPosition(position: Position): Promise<void> {
  await query(`
    INSERT INTO no_positions (
      id, strategy_id, market_id, token_id, token_side, question, category,
      entry_price, entry_price_after_slippage, quantity, cost_basis, estimated_edge,
      entry_time, end_date, status
    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
  `, [
    position.id,
    position.strategyId,
    position.marketId,
    position.tokenId,
    position.tokenSide,
    position.question,
    position.category,
    position.entryPrice,
    position.entryPriceAfterSlippage,
    position.quantity,
    position.costBasis,
    position.estimatedEdge,
    position.entryTime,
    position.endDate,
    position.status,
  ]);
}

/**
 * Update a position (for closing).
 */
export async function updatePosition(position: Position): Promise<void> {
  await query(`
    UPDATE no_positions SET
      status = $1,
      exit_price = $2,
      exit_time = $3,
      exit_reason = $4,
      realized_pnl = $5,
      realized_pnl_percent = $6
    WHERE id = $7
  `, [
    position.status,
    position.exitPrice,
    position.exitTime,
    position.exitReason,
    position.realizedPnl,
    position.realizedPnlPercent,
    position.id,
  ]);
}

/**
 * Insert a trade.
 */
export async function insertTrade(trade: Trade): Promise<void> {
  await query(`
    INSERT INTO no_trades (
      id, strategy_id, position_id, market_id, question, category,
      side, token_side, price, price_after_slippage, quantity,
      value, slippage_cost, timestamp, reason
    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15)
  `, [
    trade.id,
    trade.strategyId,
    trade.positionId,
    trade.marketId,
    trade.question,
    trade.category,
    trade.side,
    trade.tokenSide,
    trade.price,
    trade.priceAfterSlippage,
    trade.quantity,
    trade.value,
    trade.slippageCost,
    trade.timestamp,
    trade.reason,
  ]);
}

/**
 * Get all trades.
 */
export async function getTrades(): Promise<Trade[]> {
  const rows = await queryRows<any>(`
    SELECT * FROM no_trades ORDER BY timestamp DESC
  `);
  return rows.map(rowToTrade);
}

/**
 * Get trades for a position.
 */
export async function getTradesForPosition(positionId: string): Promise<Trade[]> {
  const rows = await queryRows<any>(`
    SELECT * FROM no_trades WHERE position_id = $1 ORDER BY timestamp
  `, [positionId]);
  return rows.map(rowToTrade);
}

/**
 * Update portfolio after opening a position.
 */
export async function updatePortfolioOnOpen(costBasis: number, strategyId: StrategyId): Promise<void> {
  await query(`
    UPDATE no_portfolio SET
      cash_balance = cash_balance - $1,
      last_updated = NOW()
    WHERE strategy_id = $2
  `, [costBasis, strategyId]);
}

/**
 * Update portfolio after closing a position.
 */
export async function updatePortfolioOnClose(
  proceeds: number,
  pnl: number,
  isWin: boolean,
  strategyId: StrategyId
): Promise<void> {
  await query(`
    UPDATE no_portfolio SET
      cash_balance = cash_balance + $1,
      realized_pnl = realized_pnl + $2,
      total_trades = total_trades + 1,
      winning_trades = winning_trades + CASE WHEN $3 THEN 1 ELSE 0 END,
      losing_trades = losing_trades + CASE WHEN $3 THEN 0 ELSE 1 END,
      best_trade = GREATEST(COALESCE(best_trade, -999999), $2),
      worst_trade = LEAST(COALESCE(worst_trade, 999999), $2),
      last_updated = NOW()
    WHERE strategy_id = $4
  `, [proceeds, pnl, isWin, strategyId]);
}

/**
 * Check if market was already scanned.
 */
export async function wasMarketScanned(marketId: string): Promise<boolean> {
  const row = await queryOne<any>(`
    SELECT 1 FROM no_scanned_markets WHERE market_id = $1
  `, [marketId]);
  return !!row;
}

/**
 * Record a scanned market.
 */
export async function recordScannedMarket(
  marketId: string,
  eligible: boolean,
  rejectionReason?: string,
  positionOpened: boolean = false
): Promise<void> {
  await query(`
    INSERT INTO no_scanned_markets (market_id, eligible, rejection_reason, position_opened)
    VALUES ($1, $2, $3, $4)
    ON CONFLICT (market_id) DO UPDATE SET
      eligible = $2,
      rejection_reason = $3,
      position_opened = CASE WHEN $4 THEN TRUE ELSE no_scanned_markets.position_opened END
  `, [marketId, eligible, rejectionReason, positionOpened]);
}

/**
 * Record daily snapshot for a strategy.
 */
export async function recordDailySnapshot(
  date: string,
  startingEquity: number,
  endingEquity: number,
  tradesOpened: number,
  tradesClosed: number,
  winningTrades: number,
  losingTrades: number,
  strategyId: StrategyId
): Promise<void> {
  const dailyPnl = endingEquity - startingEquity;
  const dailyPnlPercent = startingEquity > 0 ? (dailyPnl / startingEquity) * 100 : 0;

  await query(`
    INSERT INTO no_daily_snapshots (
      strategy_id, date, starting_equity, ending_equity, daily_pnl, daily_pnl_percent,
      trades_opened, trades_closed, winning_trades, losing_trades
    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10)
    ON CONFLICT (strategy_id, date) DO UPDATE SET
      ending_equity = $4,
      daily_pnl = $5,
      daily_pnl_percent = $6,
      trades_opened = no_daily_snapshots.trades_opened + $7,
      trades_closed = no_daily_snapshots.trades_closed + $8,
      winning_trades = no_daily_snapshots.winning_trades + $9,
      losing_trades = no_daily_snapshots.losing_trades + $10
  `, [strategyId, date, startingEquity, endingEquity, dailyPnl, dailyPnlPercent, tradesOpened, tradesClosed, winningTrades, losingTrades]);
}

/**
 * Get daily snapshots for equity curve for a strategy.
 */
export async function getDailySnapshots(strategyId: StrategyId): Promise<DailySummary[]> {
  const rows = await queryRows<any>(`
    SELECT * FROM no_daily_snapshots WHERE strategy_id = $1 ORDER BY date ASC
  `, [strategyId]);

  return rows.map(row => ({
    date: row.date.toISOString().split('T')[0],
    startingEquity: parseFloat(String(row.starting_equity)),
    endingEquity: parseFloat(String(row.ending_equity)),
    dailyPnl: parseFloat(String(row.daily_pnl)),
    dailyPnlPercent: parseFloat(String(row.daily_pnl_percent)),
    tradesOpened: parseInt(String(row.trades_opened)),
    tradesClosed: parseInt(String(row.trades_closed)),
    winningTrades: parseInt(String(row.winning_trades)),
    losingTrades: parseInt(String(row.losing_trades)),
  }));
}

/**
 * Get lifetime exit statistics (all-time, not just session) for a strategy.
 */
export async function getLifetimeExitStats(strategyId: StrategyId): Promise<{
  takeProfitCount: number;
  stopLossCount: number;
  resolvedCount: number;
}> {
  const row = await queryOne<any>(`
    SELECT
      COUNT(*) FILTER (WHERE status = 'CLOSED_TP') as tp_count,
      COUNT(*) FILTER (WHERE status = 'CLOSED_SL') as sl_count,
      COUNT(*) FILTER (WHERE status = 'CLOSED_RESOLVED') as resolved_count
    FROM no_positions
    WHERE strategy_id = $1
  `, [strategyId]);

  return {
    takeProfitCount: parseInt(String(row?.tp_count || 0)),
    stopLossCount: parseInt(String(row?.sl_count || 0)),
    resolvedCount: parseInt(String(row?.resolved_count || 0)),
  };
}

/**
 * Reset all paper trading data.
 */
export async function resetPaperTrading(): Promise<void> {
  await withTransaction(async (client) => {
    await client.query('DELETE FROM no_trades');
    await client.query('DELETE FROM no_positions');
    await client.query('DELETE FROM no_portfolio');
    await client.query('DELETE FROM no_daily_snapshots');
    await client.query('DELETE FROM no_scanned_markets');
  });
  console.log('No paper trading data reset');
}

// Helper to parse PostgreSQL numeric as number
function num(value: any): number {
  return parseFloat(String(value));
}

function numOrUndefined(value: any): number | undefined {
  return value ? num(value) : undefined;
}

function rowToPosition(row: any): Position {
  return {
    id: row.id,
    strategyId: (row.strategy_id || 'no-buyer') as StrategyId,
    marketId: row.market_id,
    tokenId: row.token_id,
    tokenSide: (row.token_side || 'NO') as TokenSide,
    question: row.question,
    category: row.category,
    entryPrice: num(row.entry_price),
    entryPriceAfterSlippage: num(row.entry_price_after_slippage),
    quantity: num(row.quantity),
    costBasis: num(row.cost_basis),
    estimatedEdge: num(row.estimated_edge),
    entryTime: new Date(row.entry_time),
    endDate: new Date(row.end_date),
    status: row.status as PositionStatus,
    exitPrice: numOrUndefined(row.exit_price),
    exitTime: row.exit_time ? new Date(row.exit_time) : undefined,
    exitReason: row.exit_reason || undefined,
    realizedPnl: numOrUndefined(row.realized_pnl),
    realizedPnlPercent: numOrUndefined(row.realized_pnl_percent),
  };
}

function rowToTrade(row: any): Trade {
  return {
    id: row.id,
    strategyId: (row.strategy_id || 'no-buyer') as StrategyId,
    positionId: row.position_id,
    marketId: row.market_id,
    question: row.question,
    category: row.category,
    side: row.side,
    tokenSide: row.token_side,
    price: num(row.price),
    priceAfterSlippage: num(row.price_after_slippage),
    quantity: num(row.quantity),
    value: num(row.value),
    slippageCost: num(row.slippage_cost),
    timestamp: new Date(row.timestamp),
    reason: row.reason,
  };
}

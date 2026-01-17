/**
 * Database repository for the No-betting paper trading system.
 * Handles persistence of positions, trades, and portfolio state.
 */

import { query, queryRows, queryOne, withTransaction } from '../database/index';
import { Position, Trade, Portfolio, DailySummary, PositionStatus } from './types';

/**
 * Initialize database tables for No paper trading.
 */
export async function initializeTables(): Promise<void> {
  // Positions table
  await query(`
    CREATE TABLE IF NOT EXISTS no_positions (
      id TEXT PRIMARY KEY,
      market_id TEXT NOT NULL,
      token_id TEXT NOT NULL,
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

  // Trades table
  await query(`
    CREATE TABLE IF NOT EXISTS no_trades (
      id TEXT PRIMARY KEY,
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

  // Portfolio state table (single row, updated over time)
  await query(`
    CREATE TABLE IF NOT EXISTS no_portfolio (
      id INTEGER PRIMARY KEY DEFAULT 1,
      cash_balance NUMERIC(12, 2) NOT NULL,
      initial_capital NUMERIC(12, 2) NOT NULL,
      realized_pnl NUMERIC(12, 2) NOT NULL DEFAULT 0,
      total_trades INTEGER NOT NULL DEFAULT 0,
      winning_trades INTEGER NOT NULL DEFAULT 0,
      losing_trades INTEGER NOT NULL DEFAULT 0,
      best_trade NUMERIC(12, 2),
      worst_trade NUMERIC(12, 2),
      last_updated TIMESTAMP NOT NULL DEFAULT NOW(),
      CONSTRAINT single_row CHECK (id = 1)
    )
  `);

  // Daily equity snapshots for equity curve
  await query(`
    CREATE TABLE IF NOT EXISTS no_daily_snapshots (
      date DATE PRIMARY KEY,
      starting_equity NUMERIC(12, 2) NOT NULL,
      ending_equity NUMERIC(12, 2) NOT NULL,
      daily_pnl NUMERIC(12, 2) NOT NULL,
      daily_pnl_percent NUMERIC(8, 4) NOT NULL,
      trades_opened INTEGER NOT NULL DEFAULT 0,
      trades_closed INTEGER NOT NULL DEFAULT 0,
      winning_trades INTEGER NOT NULL DEFAULT 0,
      losing_trades INTEGER NOT NULL DEFAULT 0
    )
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
 * Initialize portfolio with starting capital.
 */
export async function initializePortfolio(initialCapital: number): Promise<void> {
  await query(`
    INSERT INTO no_portfolio (id, cash_balance, initial_capital, realized_pnl, last_updated)
    VALUES (1, $1, $1, 0, NOW())
    ON CONFLICT (id) DO NOTHING
  `, [initialCapital]);
}

/**
 * Get current portfolio state.
 */
export async function getPortfolio(): Promise<Portfolio | null> {
  const row = await queryOne<any>(`
    SELECT
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
    WHERE id = 1
  `);

  if (!row) return null;

  // Get open positions for unrealized P&L calculation
  const openPositions = await getOpenPositions();
  const openPositionValue = openPositions.reduce((sum, p) => sum + p.costBasis, 0);

  const cashBalance = parseFloat(String(row.cash_balance));
  const initialCapital = parseFloat(String(row.initial_capital)) || 0;
  const totalEquity = cashBalance + openPositionValue;
  const totalTrades = parseInt(String(row.total_trades)) || 0;
  const winningTrades = parseInt(String(row.winning_trades)) || 0;
  const losingTrades = parseInt(String(row.losing_trades)) || 0;
  const realizedPnl = parseFloat(String(row.realized_pnl)) || 0;

  return {
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
 * Get all open positions.
 */
export async function getOpenPositions(): Promise<Position[]> {
  const rows = await queryRows<any>(`
    SELECT *
    FROM no_positions
    WHERE status = 'OPEN'
    ORDER BY entry_time DESC
  `);

  return rows.map(rowToPosition);
}

/**
 * Get all positions (open and closed).
 */
export async function getAllPositions(): Promise<Position[]> {
  const rows = await queryRows<any>(`
    SELECT *
    FROM no_positions
    ORDER BY entry_time DESC
  `);

  return rows.map(rowToPosition);
}

/**
 * Get closed positions only.
 */
export async function getClosedPositions(): Promise<Position[]> {
  const rows = await queryRows<any>(`
    SELECT *
    FROM no_positions
    WHERE status != 'OPEN'
    ORDER BY exit_time DESC
  `);

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
 * Check if we already have a position for a market.
 */
export async function hasPositionForMarket(marketId: string): Promise<boolean> {
  const row = await queryOne<any>(`
    SELECT 1 FROM no_positions WHERE market_id = $1 AND status = 'OPEN'
  `, [marketId]);
  return !!row;
}

/**
 * Insert a new position.
 */
export async function insertPosition(position: Position): Promise<void> {
  await query(`
    INSERT INTO no_positions (
      id, market_id, token_id, question, category,
      entry_price, entry_price_after_slippage, quantity, cost_basis, estimated_edge,
      entry_time, end_date, status
    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13)
  `, [
    position.id,
    position.marketId,
    position.tokenId,
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
      id, position_id, market_id, question, category,
      side, token_side, price, price_after_slippage, quantity,
      value, slippage_cost, timestamp, reason
    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
  `, [
    trade.id,
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
export async function updatePortfolioOnOpen(costBasis: number): Promise<void> {
  await query(`
    UPDATE no_portfolio SET
      cash_balance = cash_balance - $1,
      last_updated = NOW()
    WHERE id = 1
  `, [costBasis]);
}

/**
 * Update portfolio after closing a position.
 */
export async function updatePortfolioOnClose(
  proceeds: number,
  pnl: number,
  isWin: boolean
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
    WHERE id = 1
  `, [proceeds, pnl, isWin]);
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
 * Record daily snapshot.
 */
export async function recordDailySnapshot(
  date: string,
  startingEquity: number,
  endingEquity: number,
  tradesOpened: number,
  tradesClosed: number,
  winningTrades: number,
  losingTrades: number
): Promise<void> {
  const dailyPnl = endingEquity - startingEquity;
  const dailyPnlPercent = startingEquity > 0 ? (dailyPnl / startingEquity) * 100 : 0;

  await query(`
    INSERT INTO no_daily_snapshots (
      date, starting_equity, ending_equity, daily_pnl, daily_pnl_percent,
      trades_opened, trades_closed, winning_trades, losing_trades
    ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9)
    ON CONFLICT (date) DO UPDATE SET
      ending_equity = $3,
      daily_pnl = $4,
      daily_pnl_percent = $5,
      trades_opened = no_daily_snapshots.trades_opened + $6,
      trades_closed = no_daily_snapshots.trades_closed + $7,
      winning_trades = no_daily_snapshots.winning_trades + $8,
      losing_trades = no_daily_snapshots.losing_trades + $9
  `, [date, startingEquity, endingEquity, dailyPnl, dailyPnlPercent, tradesOpened, tradesClosed, winningTrades, losingTrades]);
}

/**
 * Get daily snapshots for equity curve.
 */
export async function getDailySnapshots(): Promise<DailySummary[]> {
  const rows = await queryRows<any>(`
    SELECT * FROM no_daily_snapshots ORDER BY date ASC
  `);

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
    marketId: row.market_id,
    tokenId: row.token_id,
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

/**
 * Types for the No-betting paper trading system.
 */

export type PositionStatus = 'OPEN' | 'CLOSED_TP' | 'CLOSED_SL' | 'CLOSED_RESOLVED' | 'CLOSED_MANUAL';
export type TokenSide = 'YES' | 'NO';
export type StrategyId = 'yes-buyer' | 'no-buyer';

/**
 * Definition of a trading strategy.
 */
export interface StrategyDefinition {
  id: StrategyId;
  name: string;
  description: string;
  side: TokenSide;  // Which token side this strategy buys
  minPrice: number;
  maxPrice: number;
  minEdge: number;
  categoryWinRates: Record<string, number>;
}

export interface Position {
  id: string;
  strategyId: StrategyId;
  marketId: string;
  tokenId: string;       // Token ID (YES or NO depending on tokenSide)
  tokenSide: TokenSide;  // Which side we're betting on
  question: string;
  category: string;
  entryPrice: number;
  entryPriceAfterSlippage: number;
  quantity: number;      // Number of contracts
  costBasis: number;     // Total cost including slippage
  estimatedEdge: number;
  entryTime: Date;
  endDate: Date;
  status: PositionStatus;
  exitPrice?: number;
  exitTime?: Date;
  exitReason?: string;
  realizedPnl?: number;
  realizedPnlPercent?: number;
}

export interface Trade {
  id: string;
  strategyId: StrategyId;
  positionId: string;
  marketId: string;
  question: string;
  category: string;
  side: 'BUY' | 'SELL';
  tokenSide: TokenSide;
  price: number;
  priceAfterSlippage: number;
  quantity: number;
  value: number;
  slippageCost: number;
  timestamp: Date;
  reason: string;  // e.g., "Entry", "Take Profit", "Stop Loss", "Resolution"
}

export interface Portfolio {
  strategyId: StrategyId;
  cashBalance: number;
  initialCapital: number;
  openPositionCount: number;
  openPositionValue: number;
  totalEquity: number;
  realizedPnl: number;
  unrealizedPnl: number;
  totalPnl: number;
  totalPnlPercent: number;
  totalTrades: number;
  winningTrades: number;
  losingTrades: number;
  winRate: number;
  avgPnlPerTrade: number;
  bestTrade: number;
  worstTrade: number;
  lastUpdated: Date;
}

export interface DailySummary {
  date: string;           // YYYY-MM-DD
  startingEquity: number;
  endingEquity: number;
  dailyPnl: number;
  dailyPnlPercent: number;
  tradesOpened: number;
  tradesClosed: number;
  winningTrades: number;
  losingTrades: number;
}

export interface CategoryPerformance {
  category: string;
  totalTrades: number;
  winningTrades: number;
  losingTrades: number;
  winRate: number;
  totalPnl: number;
  avgPnlPerTrade: number;
  avgEdge: number;
}

export interface PerformanceReport {
  reportDate: Date;
  periodStart: Date;
  periodEnd: Date;
  daysActive: number;

  // Portfolio summary
  initialCapital: number;
  finalEquity: number;
  totalPnl: number;
  totalPnlPercent: number;

  // Trade statistics
  totalTrades: number;
  winningTrades: number;
  losingTrades: number;
  winRate: number;
  avgPnlPerTrade: number;

  // Best/worst
  bestTrade: { marketId: string; question: string; pnl: number; pnlPercent: number } | null;
  worstTrade: { marketId: string; question: string; pnl: number; pnlPercent: number } | null;

  // By category
  categoryPerformance: CategoryPerformance[];

  // Equity curve (daily snapshots)
  equityCurve: { date: string; equity: number }[];

  // Open positions
  openPositions: Position[];
}

export interface EligibleMarket {
  marketId: string;
  tokenId: string;
  tokenSide: TokenSide;
  question: string;
  category: string;
  price: number;         // Entry price for the token side we're buying
  volume: number;
  createdAt: Date;
  endDate: Date;
  edge: number;
  ageHours: number;
  daysToResolution: number;
}

/**
 * Direction-agnostic scanned market with both YES and NO prices.
 * Monitors decide which side to trade based on their strategy.
 */
export interface ScannedMarket {
  marketId: string;
  question: string;
  category: string;
  yesTokenId: string;
  noTokenId: string;
  yesPrice: number;
  noPrice: number;
  volume24h: number;
  createdAt: Date;
  endDate: Date;
  ageHours: number;
  daysToResolution: number;
}

export interface ScanResult {
  timestamp: Date;
  marketsScanned: number;
  scannedMarkets: ScannedMarket[];  // Direction-agnostic markets for monitors
  eligibleMarkets: EligibleMarket[]; // Legacy - kept for compatibility
  rejectedCount: number;
  rejectionReasons: Record<string, number>;
}

export interface MonitorResult {
  timestamp: Date;
  positionsChecked: number;
  positionsOpened: number;  // New positions opened by the strategy
  takeProfitTriggered: number;
  stopLossTriggered: number;
  resolved: number;
  stillOpen: number;
}

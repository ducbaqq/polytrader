/**
 * Types for the No-betting paper trading system.
 */

export type PositionStatus = 'OPEN' | 'CLOSED_TP' | 'CLOSED_SL' | 'CLOSED_RESOLVED' | 'CLOSED_MANUAL';

export interface Position {
  id: string;
  marketId: string;
  tokenId: string;       // No token ID
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
  positionId: string;
  marketId: string;
  question: string;
  category: string;
  side: 'BUY' | 'SELL';
  tokenSide: 'NO';
  price: number;
  priceAfterSlippage: number;
  quantity: number;
  value: number;
  slippageCost: number;
  timestamp: Date;
  reason: string;  // e.g., "Entry", "Take Profit", "Stop Loss", "Resolution"
}

export interface Portfolio {
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
  question: string;
  category: string;
  noPrice: number;
  volume: number;
  createdAt: Date;
  endDate: Date;
  edge: number;
  ageHours: number;
  daysToResolution: number;
}

export interface ScanResult {
  timestamp: Date;
  marketsScanned: number;
  eligibleMarkets: EligibleMarket[];
  positionsOpened: number;
  rejectedCount: number;
  rejectionReasons: Record<string, number>;
}

export interface MonitorResult {
  timestamp: Date;
  positionsChecked: number;
  takeProfitTriggered: number;
  stopLossTriggered: number;
  resolved: number;
  stillOpen: number;
}

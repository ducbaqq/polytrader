/**
 * Data models for market data, opportunities, and snapshots.
 */

export enum OpportunityType {
  ARBITRAGE = 'arbitrage',
  WIDE_SPREAD = 'wide_spread',
  MISPRICING = 'mispricing',
  VOLUME_SPIKE = 'volume_spike',
  THIN_BOOK = 'thin_book',
}

export interface OrderBookLevel {
  price: number;
  size: number;
}

export interface TokenData {
  tokenId: string;
  outcome: string; // "YES" or "NO"
  bestBid: OrderBookLevel | null;
  bestAsk: OrderBookLevel | null;
  spread: number;
  spreadPct: number;
  activeMakers: number;
}

export interface MarketData {
  marketId: string;
  conditionId: string;
  question: string;
  endDate: Date | null;
  category: string;
  volume24h: number;
  yesToken: TokenData | null;
  noToken: TokenData | null;
  yesNoSum: number;
  totalLiquidityAtBest: number;
  timeSinceLastTrade: number | null;
  createdAt: Date | null;
  lastUpdated: Date;
  totalActiveMakers: number;
  rawData: Record<string, any>;
}

export interface Opportunity {
  type: OpportunityType;
  marketId: string;
  question: string;
  timestamp: Date;
  description: string;
  potentialProfit: number;
  // Arbitrage specific
  yesNoSum: number;
  availableLiquidity: number;
  // Spread specific
  spreadPct: number;
  tokenSide: string;
  // Volume spike specific
  currentVolume: number;
  averageVolume: number;
  spikeMultiplier: number;
  // Thin book specific
  makerCount: number;
  volume: number;
  // Mispricing specific
  relatedMarketId: string;
  priceDifference: number;
}

export interface VolumeDistribution {
  tierUnder1k: number;
  tier1kTo10k: number;
  tier10kTo100k: number;
  tier100kTo1m: number;
  tierOver1m: number;
}

export interface SpreadDistribution {
  tightUnder1pct: number;
  moderate1To3pct: number;
  wide3To5pct: number;
  veryWide5To10pct: number;
  extremeOver10pct: number;
  noSpreadData: number;
}

export interface MarketSnapshot {
  timestamp: Date;
  markets: MarketData[];
  opportunities: Opportunity[];
  volumeDistribution: VolumeDistribution;
  spreadDistribution: SpreadDistribution;
  totalMarkets: number;
  totalVolume24h: number;
  avgSpread: number;
}

export interface ScannerStats {
  totalScans: number;
  failedScans: number;
  lastScanDuration: number;
  lastScanTime: Date | null;
  marketsTracked: number;
  isRunning: boolean;
}

export interface DetectorStats {
  totalOpportunities: number;
  byType: Record<string, number>;
  recent1h: number;
  thresholds: {
    arbitrage: number;
    wideSpread: number;
    volumeSpikeMultiplier: number;
    thinBookMakerCount: number;
  };
}

export interface StorageStats {
  dataDir: string;
  logsDir: string;
  snapshotsCount: number;
  opportunitiesCount: number;
  errorsCount: number;
  totalSizeMb: number;
  compress: boolean;
}

// API response types
export interface GammaMarket {
  id: string;
  question: string;
  conditionId: string;
  slug?: string;
  endDate?: string;
  category?: string;
  outcomes?: string; // JSON string
  outcomePrices?: string; // JSON string
  volume?: string;
  volume24hr?: number;
  volumeNum?: number;
  active?: boolean;
  closed?: boolean;
  clobTokenIds?: string; // JSON string
  tokens?: Array<{ token_id: string; outcome: string }>;
  createdAt?: string;
  liquidityNum?: number;
  bestBid?: number;
  bestAsk?: number;
  spread?: number;
}

export interface OrderBookResponse {
  market?: string;
  asset_id?: string;
  bids: Array<{ price: string; size: string } | OrderBookLevel>;
  asks: Array<{ price: string; size: string } | OrderBookLevel>;
}

// Utility functions for types
export function createEmptyVolumeDistribution(): VolumeDistribution {
  return {
    tierUnder1k: 0,
    tier1kTo10k: 0,
    tier10kTo100k: 0,
    tier100kTo1m: 0,
    tierOver1m: 0,
  };
}

export function createEmptySpreadDistribution(): SpreadDistribution {
  return {
    tightUnder1pct: 0,
    moderate1To3pct: 0,
    wide3To5pct: 0,
    veryWide5To10pct: 0,
    extremeOver10pct: 0,
    noSpreadData: 0,
  };
}

export function calculateVolumeDistribution(markets: MarketData[]): VolumeDistribution {
  const dist = createEmptyVolumeDistribution();
  for (const market of markets) {
    const vol = market.volume24h;
    if (vol < 1000) dist.tierUnder1k++;
    else if (vol < 10000) dist.tier1kTo10k++;
    else if (vol < 100000) dist.tier10kTo100k++;
    else if (vol < 1000000) dist.tier100kTo1m++;
    else dist.tierOver1m++;
  }
  return dist;
}

export function calculateSpreadDistribution(markets: MarketData[]): SpreadDistribution {
  const dist = createEmptySpreadDistribution();
  for (const market of markets) {
    const spreads: number[] = [];
    if (market.yesToken && market.yesToken.spreadPct > 0) {
      spreads.push(market.yesToken.spreadPct);
    }
    if (market.noToken && market.noToken.spreadPct > 0) {
      spreads.push(market.noToken.spreadPct);
    }

    if (spreads.length === 0) {
      dist.noSpreadData++;
      continue;
    }

    const avgSpread = spreads.reduce((a, b) => a + b, 0) / spreads.length;

    if (avgSpread < 0.01) dist.tightUnder1pct++;
    else if (avgSpread < 0.03) dist.moderate1To3pct++;
    else if (avgSpread < 0.05) dist.wide3To5pct++;
    else if (avgSpread < 0.10) dist.veryWide5To10pct++;
    else dist.extremeOver10pct++;
  }
  return dist;
}

export function volumeDistributionToRecord(dist: VolumeDistribution): Record<string, number> {
  return {
    '<$1K': dist.tierUnder1k,
    '$1K-$10K': dist.tier1kTo10k,
    '$10K-$100K': dist.tier10kTo100k,
    '$100K-$1M': dist.tier100kTo1m,
    '>$1M': dist.tierOver1m,
  };
}

export function spreadDistributionToRecord(dist: SpreadDistribution): Record<string, number> {
  return {
    '<1%': dist.tightUnder1pct,
    '1-3%': dist.moderate1To3pct,
    '3-5%': dist.wide3To5pct,
    '5-10%': dist.veryWide5To10pct,
    '>10%': dist.extremeOver10pct,
    'No data': dist.noSpreadData,
  };
}

export function isMarketNew(market: MarketData): boolean {
  if (!market.createdAt) return false;
  const age = Date.now() - market.createdAt.getTime();
  return age < 86400000; // 24 hours in ms
}

export function getYesPrice(market: MarketData): number {
  if (!market.yesToken) return 0;
  if (market.yesToken.bestBid && market.yesToken.bestAsk) {
    return (market.yesToken.bestBid.price + market.yesToken.bestAsk.price) / 2;
  }
  if (market.yesToken.bestBid) return market.yesToken.bestBid.price;
  if (market.yesToken.bestAsk) return market.yesToken.bestAsk.price;
  return 0;
}

export function getNoPrice(market: MarketData): number {
  if (!market.noToken) return 0;
  if (market.noToken.bestBid && market.noToken.bestAsk) {
    return (market.noToken.bestBid.price + market.noToken.bestAsk.price) / 2;
  }
  if (market.noToken.bestBid) return market.noToken.bestBid.price;
  if (market.noToken.bestAsk) return market.noToken.bestAsk.price;
  return 0;
}

// ============ EXTENDED OPPORTUNITY FOR DB ============

export interface ExtendedOpportunity extends Opportunity {
  yesPrice?: number;
  noPrice?: number;
  category?: string;
  marketAgeHours?: number;
  potentialProfitPct?: number;
  potentialProfitUsd?: number;
}

// ============ VALIDATOR CONFIGURATION ============

export interface ValidatorConfig {
  // Scan intervals (seconds)
  fullScanInterval: number;      // All markets
  priorityScanInterval: number;  // Top 100 markets
  priorityMarketCount: number;   // How many top markets

  // Paper trading
  paperTradingEnabled: boolean;
  initialCapital: number;
  marketsToSelect: number;

  // Analysis
  hourlyAnalysisEnabled: boolean;
  dailyAnalysisEnabled: boolean;

  // Data retention
  retentionDays: number;

  // Thresholds (from detector)
  arbitrageThreshold: number;
  wideSpreadThreshold: number;
  volumeSpikeMultiplier: number;
  thinBookMakerCount: number;
}

export const DEFAULT_VALIDATOR_CONFIG: ValidatorConfig = {
  fullScanInterval: 60,
  priorityScanInterval: 15,
  priorityMarketCount: 100,
  paperTradingEnabled: true,
  initialCapital: 1000,
  marketsToSelect: 3,
  hourlyAnalysisEnabled: true,
  dailyAnalysisEnabled: true,
  retentionDays: 7,
  arbitrageThreshold: 0.995,
  wideSpreadThreshold: 0.05,
  volumeSpikeMultiplier: 3.0,
  thinBookMakerCount: 5,
};

// ============ PAPER TRADING TYPES ============

export type OrderSide = 'BUY' | 'SELL';
export type TokenSide = 'YES' | 'NO';
export type OrderStatus = 'PENDING' | 'FILLED' | 'PARTIALLY_FILLED' | 'CANCELLED' | 'EXPIRED';
export type MarketStatus = 'ACTIVE' | 'PAUSED' | 'CLOSED';

export interface PaperOrder {
  orderId: string;
  marketId: string;
  side: OrderSide;
  tokenSide: TokenSide;
  price: number;
  size: number;
  status: OrderStatus;
  placedAt: Date;
  filledAt?: Date;
  fillPrice?: number;
  fillSize?: number;
}

export interface PaperTrade {
  tradeId: string;
  marketId: string;
  orderId: string;
  side: OrderSide;
  tokenSide: TokenSide;
  price: number;
  size: number;
  value: number;
  platformFee: number;
  gasCost: number;
  slippageCost: number;
  totalCost: number;
  netValue: number;
  executedAt: Date;
}

export interface PaperPosition {
  marketId: string;
  tokenSide: TokenSide;
  quantity: number;
  averageCost: number;
  costBasis: number;
  currentPrice: number | null;
  marketValue: number | null;
  unrealizedPnl: number | null;
  unrealizedPnlPct: number | null;
}

export interface PortfolioSummary {
  cashBalance: number;
  positionValue: number;
  totalEquity: number;
  realizedPnl: number;
  unrealizedPnl: number;
  totalPnl: number;
  positions: PaperPosition[];
}

// ============ ANALYSIS TYPES ============

export interface TimeAnalysisRow {
  analysisDate: Date;
  hourOfDay: number;
  arbitrageCount: number;
  avgArbitrageSpread: number;
  wideSpreadCount: number;
  avgWideSpread: number;
  ordersPlaced: number;
  ordersFilled: number;
  fillRate: number;
  tradesExecuted: number;
  grossProfit: number;
  netProfit: number;
  avgVolume: number;
  activeMarkets: number;
}

export interface CategoryAnalysisRow {
  analysisDate: Date;
  category: string;
  opportunitiesFound: number;
  avgSpread: number;
  tradesExecuted: number;
  fillRate: number;
  grossProfit: number;
  netProfit: number;
  roi: number;
  avgVolume: number;
  marketCount: number;
}

// ============ VALIDATION SUMMARY ============

export type Recommendation = 'BUILD_BOT' | 'MARGINAL' | 'DONT_BUILD';

export interface ValidationSummary {
  reportDate: Date;
  daysAnalyzed: number;

  // Discovery results
  totalScans: number;
  arbitrageOpportunities: number;
  arbitrageAvgDurationSec: number;
  arbitrageBestCaseProfit: number;
  arbitrageRealisticProfit: number;
  arbitrageVerdict: string;

  // Paper trading results
  marketsTested: number;
  totalOrders: number;
  totalFills: number;
  overallFillRate: number;

  // P&L
  grossProfit: number;
  platformFees: number;
  gasCosts: number;
  slippageCosts: number;
  netProfit: number;

  // Projections
  roiWeekly: number;
  projectedMonthly: number;

  // Cost breakdown
  feesPctOfGross: number;
  gasPctOfGross: number;
  totalCostsPct: number;

  // Risk metrics
  worstDayLoss: number;
  maxDrawdownPct: number;
  winRate: number;
  dailyPnlStdDev: number;

  // Best performers
  bestMarketCategory: string;
  bestHours: number[];
  worstMarketCategory: string;

  // Decision
  recommendation: Recommendation;
  recommendationReason: string;
  nextSteps: string;
}

// ============ VALIDATOR STATS ============

export interface ValidatorStats {
  startTime: Date;
  uptime: number;
  totalScans: number;
  totalOpportunities: number;
  opportunitiesByType: Record<string, number>;
  paperTradingEnabled: boolean;
  totalPaperTrades: number;
  currentPnl: number;
  lastScanTime: Date | null;
  lastScanDuration: number;
  dbRowCounts: Record<string, number>;
}

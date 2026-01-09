/**
 * Type definitions for Crypto Price Movement Reactive Trader
 */

// ============================================================================
// Binance WebSocket Types
// ============================================================================

export type CryptoAsset = 'BTC' | 'ETH' | 'SOL';

export interface BinanceTickerMessage {
  e: string;           // Event type (24hrTicker)
  E: number;           // Event time
  s: string;           // Symbol (e.g., BTCUSDT)
  c: string;           // Last price
  o: string;           // Open price
  h: string;           // High price
  l: string;           // Low price
  v: string;           // Total traded base asset volume
  q: string;           // Total traded quote asset volume
  P: string;           // Price change percent
}

export interface CryptoPrice {
  asset: CryptoAsset;
  price: number;
  timestamp: Date;
  change1m: number;    // 1-minute price change percentage
  change5m: number;    // 5-minute price change percentage
}

export interface PriceHistory {
  asset: CryptoAsset;
  prices: Array<{ price: number; timestamp: Date }>;
}

export interface SignificantMoveEvent {
  asset: CryptoAsset;
  previousPrice: number;
  currentPrice: number;
  changePercent: number;
  timestamp: Date;
}

// ============================================================================
// Market Discovery Types
// ============================================================================

export type ThresholdDirection = 'ABOVE' | 'BELOW';

export interface CryptoMarket {
  id?: number;
  marketId: string;
  question: string;
  asset: CryptoAsset;
  threshold: number;
  direction: ThresholdDirection;
  resolutionDate: Date | null;
  volume24h: number;
  isWhitelisted: boolean;
  discoveredAt: Date;
  status: 'ACTIVE' | 'INACTIVE' | 'RESOLVED';

  // Runtime data (not persisted)
  currentPolyPrice?: number;
  expectedPrice?: number;
}

export interface ThresholdExtraction {
  asset: CryptoAsset;
  threshold: number;
  direction: ThresholdDirection;
}

export interface MarketDiscoveryResult {
  discovered: number;
  matched: number;
  excluded: number;
  errors: string[];
}

// ============================================================================
// Mispricing Detection Types
// ============================================================================

export interface CryptoOpportunity {
  id?: number;
  opportunityId: string;
  marketId: string;
  detectedAt: Date;
  asset: CryptoAsset;
  threshold: number;
  binancePrice: number;
  expectedPolyPrice: number;
  actualPolyPrice: number;
  gapPercent: number;
  side: 'YES' | 'NO';
  executed: boolean;
  status: 'DETECTED' | 'EXECUTING' | 'EXECUTED' | 'SKIPPED' | 'FAILED';
  skipReason?: string;
}

export interface MispricingResult {
  hasOpportunity: boolean;
  opportunity?: CryptoOpportunity;
  reason?: string;
}

// ============================================================================
// Trading Types
// ============================================================================

export interface CryptoPosition {
  id?: number;
  positionId: string;
  marketId: string;
  asset: CryptoAsset;
  side: 'YES' | 'NO';
  entryPrice: number;
  quantity: number;
  entryTime: Date;
  binancePriceAtEntry: number;
  exitPrice?: number;
  exitTime?: Date;
  exitReason?: 'PROFIT' | 'STOP' | 'TIME' | 'REVERSAL';
  pnl?: number;
  status: 'OPEN' | 'CLOSING' | 'CLOSED';

  // Runtime tracking (not persisted)
  currentPrice?: number;
  unrealizedPnl?: number;
  holdTimeSeconds?: number;
}

export interface OrderRequest {
  marketId: string;
  side: 'YES' | 'NO';
  price: number;
  size: number;
  opportunityId: string;
}

export interface OrderResult {
  success: boolean;
  orderId?: string;
  filledPrice?: number;
  filledQuantity?: number;
  error?: string;
}

// ============================================================================
// Risk Management Types
// ============================================================================

export interface RiskCheck {
  allowed: boolean;
  reason?: string;
}

export interface RiskState {
  totalExposure: number;
  positionCount: number;
  dailyPnl: number;
  dailyTrades: number;
  cooldowns: Map<string, Date>;  // marketId -> cooldown expiry
}

export interface RiskLimits {
  maxTotalExposure: number;
  maxSimultaneousPositions: number;
  dailyLossLimit: number;
  maxDailyTrades: number;
  cooldownMinutes: number;
}

// ============================================================================
// Exit Strategy Types
// ============================================================================

export interface ExitCondition {
  shouldExit: boolean;
  reason?: 'PROFIT' | 'STOP' | 'TIME' | 'REVERSAL';
  currentPrice?: number;
  pnlPercent?: number;
}

export interface ExitConfig {
  profitTargetPct: number;
  stopLossPct: number;
  maxHoldTimeSeconds: number;
}

// ============================================================================
// Configuration Types
// ============================================================================

export interface CryptoTraderConfig {
  // Position sizing
  basePositionSize: number;
  maxPositionSize: number;
  maxTotalExposure: number;
  maxSimultaneousPositions: number;

  // Exit strategy
  maxHoldTimeSeconds: number;
  profitTargetPct: number;
  stopLossPct: number;

  // Risk controls
  cooldownMinutes: number;
  dailyLossLimit: number;
  maxDailyTrades: number;

  // Mispricing thresholds
  minGapPercent: number;
  minVolume: number;
  minResolutionHours: number;

  // Market discovery
  discoveryIntervalMinutes: number;
  assets: CryptoAsset[];
}

// ============================================================================
// Dashboard Types
// ============================================================================

export interface CryptoDashboardData {
  prices: Map<CryptoAsset, CryptoPrice>;
  trackedMarkets: CryptoMarket[];
  activePositions: CryptoPosition[];
  riskState: RiskState;
  recentOpportunities: CryptoOpportunity[];
  isConnected: boolean;
  lastUpdate: Date;
}

// ============================================================================
// Event Types
// ============================================================================

export type CryptoEventType =
  | 'price'
  | 'significantMove'
  | 'opportunity'
  | 'positionOpened'
  | 'positionClosed'
  | 'error';

export interface CryptoEvent {
  type: CryptoEventType;
  timestamp: Date;
  data: unknown;
}

// ============================================================================
// Database Row Types (matching PostgreSQL schema)
// ============================================================================

export interface CryptoPriceLogRow {
  id: number;
  timestamp: Date;
  asset: string;
  price: string;  // numeric comes as string
  change_1m: string | null;
  change_5m: string | null;
  is_significant_move: boolean;
}

export interface CryptoMarketsRow {
  id: number;
  market_id: string;
  question: string;
  asset: string;
  threshold: string;  // numeric comes as string
  direction: string;
  resolution_date: Date | null;
  volume_24h: string | null;
  is_whitelisted: boolean;
  discovered_at: Date;
  status: string;
}

export interface CryptoOpportunitiesRow {
  id: number;
  opportunity_id: string;
  market_id: string;
  detected_at: Date;
  asset: string;
  threshold: string | null;
  binance_price: string;
  expected_poly_price: string | null;
  actual_poly_price: string | null;
  gap_percent: string | null;
  side: string | null;
  executed: boolean;
  status: string;
}

export interface CryptoPositionsRow {
  id: number;
  position_id: string;
  market_id: string;
  asset: string;
  side: string;
  entry_price: string;
  quantity: string;
  entry_time: Date;
  binance_price_at_entry: string | null;
  exit_price: string | null;
  exit_time: Date | null;
  exit_reason: string | null;
  pnl: string | null;
  status: string;
}

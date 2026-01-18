/**
 * Type definitions for Alpha Analysis Pipeline
 */

// ============================================================================
// Input Types (from Gamma API)
// ============================================================================

export interface GammaMarketExtended {
  id: string;
  question: string;
  slug: string;
  endDate: string;
  createdAt?: string;
  closed: boolean;
  active: boolean;
  archived: boolean;
  outcomes: string;          // JSON string: '["Yes","No"]'
  outcomePrices: string;     // JSON string: '["0.75","0.25"]'
  clobTokenIds?: string;     // JSON string: '["token1","token2"]'
  conditionId?: string;
  category?: string;
  tags?: { label: string }[];
  events?: {
    id: string;
    title: string;
    category?: string;
    tags?: { label: string }[];
  }[];
  volume?: string;
  volumeNum?: number;
  liquidity?: string;
  liquidityNum?: number;
  resolvedBy?: string;
  resolutionSource?: string;
}

// ============================================================================
// CLOB Price History Types
// ============================================================================

export interface ClobPricePoint {
  t: number;    // Unix timestamp
  p: string;    // Price as string (0-1)
}

export interface ClobPriceHistoryResponse {
  history: ClobPricePoint[];
}

/**
 * Stored price point with numeric price (for visualization)
 */
export interface StoredPricePoint {
  t: number;    // Unix timestamp
  p: number;    // Price (0-1)
}

// ============================================================================
// Processed Market Types
// ============================================================================

export type DataQualityTier = 'tier1' | 'tier2' | 'tier3';

export interface MarketPrices {
  openingNoPrice: number | null;   // Price at market creation
  finalNoPrice: number;            // Price at resolution
  avgNoPrice: number | null;       // Average price over lifetime
  minNoPrice: number | null;       // Minimum price seen
  maxNoPrice: number | null;       // Maximum price seen
}

export interface MarketEdge {
  impliedNoProb: number;           // Final No price (0-1)
  actualNoOutcome: 0 | 1;          // 1 if resolved No, 0 if Yes
  rawEdge: number;                 // actualNoOutcome - impliedNoProb
}

export interface DataQuality {
  tier: DataQualityTier;
  hasFullHistory: boolean;
  pricePointCount: number;
}

export interface AlphaMarket {
  id: string;
  question: string;
  tags: string[];
  resolution: 'Yes' | 'No';
  resolvedAt: string;
  createdAt: string | null;
  durationDays: number | null;
  volumeNum: number;
  liquidityNum: number;
  yesTokenId: string | null;
  noTokenId: string | null;
  prices: MarketPrices;
  edge: MarketEdge;
  dataQuality: DataQuality;
  /** Raw price history for visualization (tier1/tier2 markets only) */
  priceHistory?: StoredPricePoint[];
}

// ============================================================================
// Aggregation Types
// ============================================================================

export interface ConfidenceInterval {
  lower: number;
  upper: number;
  point: number;
}

export interface CalibrationBucket {
  bucket: string;                  // e.g., "20-30%"
  bucketMin: number;               // e.g., 0.2
  bucketMax: number;               // e.g., 0.3
  marketCount: number;
  actualNoWinRate: number;
  expectedNoWinRate: number;       // Midpoint of bucket
  edge: number;                    // actualNoWinRate - expectedNoWinRate
  confidenceInterval95: ConfidenceInterval;
  isStatisticallySignificant: boolean;
}

export interface CategoryStats {
  category: string;
  marketCount: number;
  avgVolume: number;
  avgLiquidity: number;
  noWinRate: number;
  avgNoPrice: number;
  edge: number;
  confidenceInterval95: ConfidenceInterval;
  verdict: 'positive_edge' | 'negative_edge' | 'no_edge' | 'insufficient_data';
}

export interface LiquidityTierStats {
  tier: string;                    // e.g., "<$1k", "$1k-$10k"
  tierMin: number;
  tierMax: number | null;          // null for unbounded
  marketCount: number;
  noWinRate: number;
  avgNoPrice: number;
  edge: number;
  confidenceInterval95: ConfidenceInterval;
  isStatisticallySignificant: boolean;
}

export interface DurationStats {
  duration: string;                // e.g., "<1 day", "1-7 days"
  durationMinDays: number;
  durationMaxDays: number | null;
  marketCount: number;
  noWinRate: number;
  avgNoPrice: number;
  edge: number;
  confidenceInterval95: ConfidenceInterval;
  isStatisticallySignificant: boolean;
}

// ============================================================================
// Output Types
// ============================================================================

export interface AlphaAnalysisOutput {
  exportDate: string;
  period: string;
  periodDays: number;
  dataSource: {
    gammaApiUrl: string;
    clobApiUrl: string;
  };
  totalMarkets: number;
  markets: AlphaMarket[];
}

export interface OverallStats {
  totalMarkets: number;
  noWinRate: number;
  avgNoPriceAtClose: number;
  averageEdge: number;
  confidenceInterval95: ConfidenceInterval;
}

export interface AlphaSummaryOutput {
  exportDate: string;
  period: string;
  periodDays: number;
  overall: OverallStats;
  byCalibrationBucket: CalibrationBucket[];
  byCategory: CategoryStats[];
  byLiquidity: LiquidityTierStats[];
  byDuration: DurationStats[];
  recommendations: string[];
}

// ============================================================================
// Configuration
// ============================================================================

export interface AlphaAnalysisConfig {
  period: string;
  periodDays: number;
  volumeTiers: {
    tier1MinVolume: number;   // Full history
    tier2MinVolume: number;   // Key points only
  };
  minSampleSize: number;        // For statistical significance
  zScoreThreshold: number;      // For 95% CI (1.96)
  rateLimit: {
    callsPerSecond: number;
    maxRetries: number;
    baseDelayMs: number;
  };
}

export const DEFAULT_CONFIG: AlphaAnalysisConfig = {
  period: '5d',
  periodDays: 5,
  volumeTiers: {
    tier1MinVolume: 10000,    // $10k+ = full history
    tier2MinVolume: 1000,     // $1k-$10k = key points
  },
  minSampleSize: 30,
  zScoreThreshold: 1.96,
  rateLimit: {
    callsPerSecond: 2,
    maxRetries: 3,
    baseDelayMs: 1000,
  },
};

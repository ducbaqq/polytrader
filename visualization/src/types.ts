export interface Market {
  id: string;
  question: string;
  tags: string[];
  resolution: 'Yes' | 'No';
  resolvedAt: string;
}

export interface ExportData {
  exportDate: string;
  monthsBack?: number;     // deprecated
  period?: string;         // e.g., "5d", "2m"
  periodDays?: number;     // actual days
  totalMarkets: number;
  markets: Market[];
}

export interface CategoryData {
  name: string;
  total: number;
  yes: number;
  no: number;
  markets: Market[];
}

// ============================================================================
// Alpha Analysis Types
// ============================================================================

export interface ConfidenceInterval {
  lower: number;
  upper: number;
  point: number;
}

export interface CalibrationBucket {
  bucket: string;
  bucketMin: number;
  bucketMax: number;
  marketCount: number;
  actualNoWinRate: number;
  expectedNoWinRate: number;
  edge: number;
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
  tier: string;
  tierMin: number;
  tierMax: number | null;
  marketCount: number;
  noWinRate: number;
  avgNoPrice: number;
  edge: number;
  confidenceInterval95: ConfidenceInterval;
  isStatisticallySignificant: boolean;
}

export interface DurationStats {
  duration: string;
  durationMinDays: number;
  durationMaxDays: number | null;
  marketCount: number;
  noWinRate: number;
  avgNoPrice: number;
  edge: number;
  confidenceInterval95: ConfidenceInterval;
  isStatisticallySignificant: boolean;
}

export interface OverallStats {
  totalMarkets: number;
  noWinRate: number;
  avgNoPriceAtClose: number;
  averageEdge: number;
  confidenceInterval95: ConfidenceInterval;
}

export interface AlphaSummary {
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

export interface MarketPrices {
  openingNoPrice: number | null;
  finalNoPrice: number;
  avgNoPrice: number | null;
  minNoPrice: number | null;
  maxNoPrice: number | null;
}

export interface MarketEdge {
  impliedNoProb: number;
  actualNoOutcome: 0 | 1;
  rawEdge: number;
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
  dataQuality: {
    tier: 'tier1' | 'tier2' | 'tier3';
    hasFullHistory: boolean;
    pricePointCount: number;
  };
}

export interface AlphaAnalysisData {
  exportDate: string;
  period: string;
  periodDays: number;
  totalMarkets: number;
  markets: AlphaMarket[];
}

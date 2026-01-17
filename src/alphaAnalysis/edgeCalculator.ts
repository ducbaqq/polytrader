/**
 * Statistical edge calculations including Wilson Score confidence intervals
 */

import {
  MarketEdge,
  ConfidenceInterval,
  AlphaAnalysisConfig,
  DEFAULT_CONFIG,
} from './types';

// ============================================================================
// Wilson Score Confidence Interval
// ============================================================================

/**
 * Calculate Wilson Score interval for binary proportions.
 * More accurate than normal approximation for small samples or extreme probabilities.
 *
 * @param successes Number of successes (e.g., No resolutions)
 * @param total Total trials
 * @param z Z-score for desired confidence (1.96 for 95%)
 * @returns [lower, upper] bounds and point estimate
 */
export function wilsonScoreInterval(
  successes: number,
  total: number,
  z: number = 1.96
): ConfidenceInterval {
  if (total === 0) {
    return { lower: 0, upper: 1, point: 0.5 };
  }

  const p = successes / total;
  const n = total;
  const z2 = z * z;

  // Wilson score formula
  const denominator = 1 + z2 / n;
  const center = (p + z2 / (2 * n)) / denominator;
  const margin = (z / denominator) * Math.sqrt((p * (1 - p) + z2 / (4 * n)) / n);

  return {
    lower: Math.max(0, center - margin),
    upper: Math.min(1, center + margin),
    point: p,
  };
}

/**
 * Calculate confidence interval for edge (difference between two proportions).
 * Uses pooled standard error approach.
 *
 * @param actualRate Actual observed rate
 * @param expectedRate Expected rate (e.g., implied probability)
 * @param n Sample size
 * @param z Z-score for desired confidence (1.96 for 95%)
 */
export function edgeConfidenceInterval(
  actualRate: number,
  expectedRate: number,
  n: number,
  z: number = 1.96
): ConfidenceInterval {
  if (n === 0) {
    return { lower: -1, upper: 1, point: 0 };
  }

  const edge = actualRate - expectedRate;

  // Standard error for a proportion
  const se = Math.sqrt((actualRate * (1 - actualRate)) / n);

  return {
    lower: edge - z * se,
    upper: edge + z * se,
    point: edge,
  };
}

/**
 * Check if an edge is statistically significant.
 * Significant if 95% CI doesn't include zero.
 */
export function isStatisticallySignificant(ci: ConfidenceInterval): boolean {
  return ci.lower > 0 || ci.upper < 0;
}

/**
 * Calculate z-score for a proportion difference.
 */
export function calculateZScore(
  observed: number,
  expected: number,
  n: number
): number {
  if (n === 0) return 0;

  const se = Math.sqrt((expected * (1 - expected)) / n);
  if (se === 0) return 0;

  return (observed - expected) / se;
}

// ============================================================================
// Edge Calculation
// ============================================================================

export class EdgeCalculator {
  private config: AlphaAnalysisConfig;

  constructor(config: AlphaAnalysisConfig = DEFAULT_CONFIG) {
    this.config = config;
  }

  /**
   * Calculate edge for a single market.
   *
   * @param resolution Market resolution ('Yes' or 'No')
   * @param finalNoPrice Final price of No token (0-1)
   */
  calculateMarketEdge(
    resolution: 'Yes' | 'No',
    finalNoPrice: number
  ): MarketEdge {
    // Implied probability from price
    const impliedNoProb = finalNoPrice;

    // Actual outcome
    const actualNoOutcome = resolution === 'No' ? 1 : 0;

    // Raw edge: actual - implied
    // Positive means No was underpriced (good bet)
    // Negative means No was overpriced (bad bet)
    const rawEdge = actualNoOutcome - impliedNoProb;

    return {
      impliedNoProb,
      actualNoOutcome: actualNoOutcome as 0 | 1,
      rawEdge,
    };
  }

  /**
   * Calculate aggregate edge statistics for a group of markets.
   */
  calculateAggregateEdge(
    markets: { edge: MarketEdge }[]
  ): {
    avgEdge: number;
    noWinRate: number;
    avgImpliedProb: number;
    confidenceInterval: ConfidenceInterval;
    isSignificant: boolean;
  } {
    if (markets.length === 0) {
      return {
        avgEdge: 0,
        noWinRate: 0,
        avgImpliedProb: 0.5,
        confidenceInterval: { lower: 0, upper: 0, point: 0 },
        isSignificant: false,
      };
    }

    const n = markets.length;
    const noWins = markets.filter((m) => m.edge.actualNoOutcome === 1).length;
    const noWinRate = noWins / n;

    const avgImpliedProb =
      markets.reduce((sum, m) => sum + m.edge.impliedNoProb, 0) / n;

    const avgEdge = noWinRate - avgImpliedProb;

    const confidenceInterval = edgeConfidenceInterval(
      noWinRate,
      avgImpliedProb,
      n,
      this.config.zScoreThreshold
    );

    const isSignificant =
      n >= this.config.minSampleSize && isStatisticallySignificant(confidenceInterval);

    return {
      avgEdge,
      noWinRate,
      avgImpliedProb,
      confidenceInterval,
      isSignificant,
    };
  }

  /**
   * Determine verdict based on edge and significance.
   */
  getVerdict(
    edge: number,
    confidenceInterval: ConfidenceInterval,
    sampleSize: number
  ): 'positive_edge' | 'negative_edge' | 'no_edge' | 'insufficient_data' {
    if (sampleSize < this.config.minSampleSize) {
      return 'insufficient_data';
    }

    if (confidenceInterval.lower > 0) {
      return 'positive_edge';
    } else if (confidenceInterval.upper < 0) {
      return 'negative_edge';
    } else {
      return 'no_edge';
    }
  }

  /**
   * Generate natural language recommendation from statistics.
   */
  generateRecommendation(
    name: string,
    edge: number,
    ci: ConfidenceInterval,
    sampleSize: number,
    noWinRate: number
  ): string | null {
    if (sampleSize < this.config.minSampleSize) {
      return null;
    }

    const edgePct = (edge * 100).toFixed(1);
    const ciLower = (ci.lower * 100).toFixed(1);
    const ciUpper = (ci.upper * 100).toFixed(1);
    const winRatePct = (noWinRate * 100).toFixed(0);

    if (ci.lower > 0.05) {
      // Strong positive edge
      return `${name}: Strong positive edge of ${edgePct}% on No bets (${winRatePct}% win rate, CI: ${ciLower}%-${ciUpper}%, n=${sampleSize})`;
    } else if (ci.lower > 0) {
      // Moderate positive edge
      return `${name}: Moderate positive edge of ${edgePct}% on No bets (${winRatePct}% win rate, n=${sampleSize})`;
    } else if (ci.upper < -0.05) {
      // Strong negative edge
      return `${name}: Markets appear fairly priced - negative edge of ${edgePct}% (${winRatePct}% win rate, n=${sampleSize})`;
    }

    return null;
  }
}

export default EdgeCalculator;

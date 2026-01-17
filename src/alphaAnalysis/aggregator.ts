/**
 * Aggregator for grouping markets by category, liquidity, duration, and calibration buckets
 */

import {
  AlphaMarket,
  CalibrationBucket,
  CategoryStats,
  LiquidityTierStats,
  DurationStats,
  OverallStats,
  AlphaSummaryOutput,
  AlphaAnalysisConfig,
  DEFAULT_CONFIG,
} from './types';
import { EdgeCalculator, wilsonScoreInterval } from './edgeCalculator';

// ============================================================================
// Configuration
// ============================================================================

const CALIBRATION_BUCKETS = [
  { label: '0-10%', min: 0, max: 0.1 },
  { label: '10-20%', min: 0.1, max: 0.2 },
  { label: '20-30%', min: 0.2, max: 0.3 },
  { label: '30-40%', min: 0.3, max: 0.4 },
  { label: '40-50%', min: 0.4, max: 0.5 },
  { label: '50-60%', min: 0.5, max: 0.6 },
  { label: '60-70%', min: 0.6, max: 0.7 },
  { label: '70-80%', min: 0.7, max: 0.8 },
  { label: '80-90%', min: 0.8, max: 0.9 },
  { label: '90-100%', min: 0.9, max: 1.0 },
];

const LIQUIDITY_TIERS = [
  { label: '<$1k', min: 0, max: 1000 },
  { label: '$1k-$10k', min: 1000, max: 10000 },
  { label: '$10k-$100k', min: 10000, max: 100000 },
  { label: '$100k+', min: 100000, max: null },
];

const DURATION_TIERS = [
  { label: '<1 day', minDays: 0, maxDays: 1 },
  { label: '1-7 days', minDays: 1, maxDays: 7 },
  { label: '7-30 days', minDays: 7, maxDays: 30 },
  { label: '30+ days', minDays: 30, maxDays: null },
];

// ============================================================================
// Aggregator Class
// ============================================================================

export class Aggregator {
  private config: AlphaAnalysisConfig;
  private edgeCalculator: EdgeCalculator;

  constructor(config: AlphaAnalysisConfig = DEFAULT_CONFIG) {
    this.config = config;
    this.edgeCalculator = new EdgeCalculator(config);
  }

  /**
   * Calculate overall statistics for all markets.
   */
  calculateOverall(markets: AlphaMarket[]): OverallStats {
    if (markets.length === 0) {
      return {
        totalMarkets: 0,
        noWinRate: 0,
        avgNoPriceAtClose: 0,
        averageEdge: 0,
        confidenceInterval95: { lower: 0, upper: 0, point: 0 },
      };
    }

    const aggStats = this.edgeCalculator.calculateAggregateEdge(markets);

    return {
      totalMarkets: markets.length,
      noWinRate: aggStats.noWinRate,
      avgNoPriceAtClose: aggStats.avgImpliedProb,
      averageEdge: aggStats.avgEdge,
      confidenceInterval95: aggStats.confidenceInterval,
    };
  }

  /**
   * Group markets by calibration bucket (implied probability).
   */
  aggregateByCalibration(markets: AlphaMarket[]): CalibrationBucket[] {
    return CALIBRATION_BUCKETS.map((bucket) => {
      const bucketed = markets.filter(
        (m) =>
          m.prices.finalNoPrice >= bucket.min &&
          m.prices.finalNoPrice < bucket.max
      );

      if (bucketed.length === 0) {
        return {
          bucket: bucket.label,
          bucketMin: bucket.min,
          bucketMax: bucket.max,
          marketCount: 0,
          actualNoWinRate: 0,
          expectedNoWinRate: (bucket.min + bucket.max) / 2,
          edge: 0,
          confidenceInterval95: { lower: 0, upper: 0, point: 0 },
          isStatisticallySignificant: false,
        };
      }

      const noWins = bucketed.filter((m) => m.resolution === 'No').length;
      const actualNoWinRate = noWins / bucketed.length;
      const expectedNoWinRate = (bucket.min + bucket.max) / 2;
      const edge = actualNoWinRate - expectedNoWinRate;

      const ci = wilsonScoreInterval(
        noWins,
        bucketed.length,
        this.config.zScoreThreshold
      );

      // Significant if CI doesn't overlap with expected
      const isStatisticallySignificant =
        bucketed.length >= this.config.minSampleSize &&
        (ci.lower > expectedNoWinRate || ci.upper < expectedNoWinRate);

      return {
        bucket: bucket.label,
        bucketMin: bucket.min,
        bucketMax: bucket.max,
        marketCount: bucketed.length,
        actualNoWinRate,
        expectedNoWinRate,
        edge,
        confidenceInterval95: ci,
        isStatisticallySignificant,
      };
    });
  }

  /**
   * Group markets by category.
   */
  aggregateByCategory(markets: AlphaMarket[]): CategoryStats[] {
    const categoryMap = new Map<string, AlphaMarket[]>();

    for (const market of markets) {
      for (const tag of market.tags) {
        if (!categoryMap.has(tag)) {
          categoryMap.set(tag, []);
        }
        categoryMap.get(tag)!.push(market);
      }
    }

    const stats: CategoryStats[] = [];

    for (const [category, categoryMarkets] of categoryMap) {
      if (categoryMarkets.length === 0) continue;

      const aggStats = this.edgeCalculator.calculateAggregateEdge(categoryMarkets);

      const avgVolume =
        categoryMarkets.reduce((sum, m) => sum + m.volumeNum, 0) /
        categoryMarkets.length;

      const avgLiquidity =
        categoryMarkets.reduce((sum, m) => sum + m.liquidityNum, 0) /
        categoryMarkets.length;

      const verdict = this.edgeCalculator.getVerdict(
        aggStats.avgEdge,
        aggStats.confidenceInterval,
        categoryMarkets.length
      );

      stats.push({
        category,
        marketCount: categoryMarkets.length,
        avgVolume,
        avgLiquidity,
        noWinRate: aggStats.noWinRate,
        avgNoPrice: aggStats.avgImpliedProb,
        edge: aggStats.avgEdge,
        confidenceInterval95: aggStats.confidenceInterval,
        verdict,
      });
    }

    // Sort by edge descending
    return stats.sort((a, b) => b.edge - a.edge);
  }

  /**
   * Group markets by liquidity tier.
   */
  aggregateByLiquidity(markets: AlphaMarket[]): LiquidityTierStats[] {
    return LIQUIDITY_TIERS.map((tier) => {
      const tiered = markets.filter((m) => {
        const liq = m.liquidityNum;
        if (tier.max === null) {
          return liq >= tier.min;
        }
        return liq >= tier.min && liq < tier.max;
      });

      if (tiered.length === 0) {
        return {
          tier: tier.label,
          tierMin: tier.min,
          tierMax: tier.max,
          marketCount: 0,
          noWinRate: 0,
          avgNoPrice: 0,
          edge: 0,
          confidenceInterval95: { lower: 0, upper: 0, point: 0 },
          isStatisticallySignificant: false,
        };
      }

      const aggStats = this.edgeCalculator.calculateAggregateEdge(tiered);

      return {
        tier: tier.label,
        tierMin: tier.min,
        tierMax: tier.max,
        marketCount: tiered.length,
        noWinRate: aggStats.noWinRate,
        avgNoPrice: aggStats.avgImpliedProb,
        edge: aggStats.avgEdge,
        confidenceInterval95: aggStats.confidenceInterval,
        isStatisticallySignificant: aggStats.isSignificant,
      };
    });
  }

  /**
   * Group markets by duration.
   */
  aggregateByDuration(markets: AlphaMarket[]): DurationStats[] {
    return DURATION_TIERS.map((tier) => {
      const tiered = markets.filter((m) => {
        const dur = m.durationDays;
        if (dur === null) return false;
        if (tier.maxDays === null) {
          return dur >= tier.minDays;
        }
        return dur >= tier.minDays && dur < tier.maxDays;
      });

      if (tiered.length === 0) {
        return {
          duration: tier.label,
          durationMinDays: tier.minDays,
          durationMaxDays: tier.maxDays,
          marketCount: 0,
          noWinRate: 0,
          avgNoPrice: 0,
          edge: 0,
          confidenceInterval95: { lower: 0, upper: 0, point: 0 },
          isStatisticallySignificant: false,
        };
      }

      const aggStats = this.edgeCalculator.calculateAggregateEdge(tiered);

      return {
        duration: tier.label,
        durationMinDays: tier.minDays,
        durationMaxDays: tier.maxDays,
        marketCount: tiered.length,
        noWinRate: aggStats.noWinRate,
        avgNoPrice: aggStats.avgImpliedProb,
        edge: aggStats.avgEdge,
        confidenceInterval95: aggStats.confidenceInterval,
        isStatisticallySignificant: aggStats.isSignificant,
      };
    });
  }

  /**
   * Generate recommendations based on analysis.
   */
  generateRecommendations(
    byCategory: CategoryStats[],
    byLiquidity: LiquidityTierStats[],
    byDuration: DurationStats[],
    byCalibration: CalibrationBucket[]
  ): string[] {
    const recommendations: string[] = [];

    // Check categories for positive edge
    for (const cat of byCategory) {
      const rec = this.edgeCalculator.generateRecommendation(
        `Category "${cat.category}"`,
        cat.edge,
        cat.confidenceInterval95,
        cat.marketCount,
        cat.noWinRate
      );
      if (rec) {
        recommendations.push(rec);
      }
    }

    // Check liquidity tiers
    for (const liq of byLiquidity) {
      const rec = this.edgeCalculator.generateRecommendation(
        `Liquidity tier ${liq.tier}`,
        liq.edge,
        liq.confidenceInterval95,
        liq.marketCount,
        liq.noWinRate
      );
      if (rec) {
        recommendations.push(rec);
      }
    }

    // Check duration tiers
    for (const dur of byDuration) {
      const rec = this.edgeCalculator.generateRecommendation(
        `Duration ${dur.duration}`,
        dur.edge,
        dur.confidenceInterval95,
        dur.marketCount,
        dur.noWinRate
      );
      if (rec) {
        recommendations.push(rec);
      }
    }

    // Check calibration - identify miscalibrated buckets
    const miscalibrated = byCalibration.filter(
      (b) => b.isStatisticallySignificant && b.marketCount >= this.config.minSampleSize
    );

    for (const bucket of miscalibrated) {
      const direction = bucket.edge > 0 ? 'underpriced' : 'overpriced';
      const edgePct = (Math.abs(bucket.edge) * 100).toFixed(1);
      recommendations.push(
        `Price bucket ${bucket.bucket}: No bets appear ${direction} by ${edgePct}% (actual win rate: ${(bucket.actualNoWinRate * 100).toFixed(0)}% vs expected: ${(bucket.expectedNoWinRate * 100).toFixed(0)}%, n=${bucket.marketCount})`
      );
    }

    // Limit to top 10 recommendations
    return recommendations.slice(0, 10);
  }

  /**
   * Generate complete summary from markets.
   */
  generateSummary(
    markets: AlphaMarket[],
    period: string,
    periodDays: number
  ): AlphaSummaryOutput {
    const overall = this.calculateOverall(markets);
    const byCalibration = this.aggregateByCalibration(markets);
    const byCategory = this.aggregateByCategory(markets);
    const byLiquidity = this.aggregateByLiquidity(markets);
    const byDuration = this.aggregateByDuration(markets);

    const recommendations = this.generateRecommendations(
      byCategory,
      byLiquidity,
      byDuration,
      byCalibration
    );

    return {
      exportDate: new Date().toISOString().split('T')[0],
      period,
      periodDays,
      overall,
      byCalibrationBucket: byCalibration,
      byCategory,
      byLiquidity,
      byDuration,
      recommendations,
    };
  }
}

export default Aggregator;

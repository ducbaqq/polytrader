/**
 * Opportunity detection module for identifying trading opportunities.
 */

import {
  MarketData,
  MarketSnapshot,
  Opportunity,
  OpportunityType,
  DetectorStats,
  getYesPrice,
  getNoPrice,
} from './types';
import { MarketScanner } from './scanner';

export interface OpportunityDetectorConfig {
  scanner: MarketScanner;
  arbitrageThreshold?: number;
  wideSpreadThreshold?: number;
  volumeSpikeMultiplier?: number;
  thinBookMakerCount?: number;
}

export class OpportunityDetector {
  private scanner: MarketScanner;
  private arbitrageThreshold: number;
  private wideSpreadThreshold: number;
  private volumeSpikeMultiplier: number;
  private thinBookMakerCount: number;

  private opportunityCounts: Map<OpportunityType, number> = new Map();

  // Note: Opportunities are stored in DB, not in memory (to prevent memory leaks)
  getOpportunities(): Opportunity[] {
    return [];
  }

  getRecentOpportunities(hours: number = 1.0, opType?: OpportunityType): Opportunity[] {
    return [];
  }

  constructor(config: OpportunityDetectorConfig) {
    this.scanner = config.scanner;
    this.arbitrageThreshold = config.arbitrageThreshold || 0.995;
    this.wideSpreadThreshold = config.wideSpreadThreshold || 0.05;
    this.volumeSpikeMultiplier = config.volumeSpikeMultiplier || 3.0;
    this.thinBookMakerCount = config.thinBookMakerCount || 5;

    console.log(
      `OpportunityDetector initialized with thresholds: ` +
        `arb=${this.arbitrageThreshold}, spread=${this.wideSpreadThreshold}, ` +
        `volume_spike=${this.volumeSpikeMultiplier}x, thin_book=${this.thinBookMakerCount} makers`
    );
  }

  /**
   * Detect arbitrage opportunity when YES + NO < threshold.
   * Now uses ASK prices (executable cost) instead of mid prices.
   */
  detectArbitrage(market: MarketData): Opportunity | null {
    // yesNoSum is now calculated using ask prices in apiClient
    if (market.yesNoSum <= 0 || market.yesNoSum >= this.arbitrageThreshold) {
      return null;
    }

    // Require both ask prices to exist for valid arbitrage
    if (!market.yesToken?.bestAsk || !market.noToken?.bestAsk) {
      return null;
    }

    // Calculate available liquidity (minimum of both sides)
    const availableLiquidity = Math.min(
      market.yesToken.bestAsk.size,
      market.noToken.bestAsk.size
    );

    // Require minimum liquidity of 100 contracts (~$50) to be worth trading
    const MIN_ARB_LIQUIDITY = 100;
    if (availableLiquidity < MIN_ARB_LIQUIDITY) {
      return null;
    }

    const spreadSize = 1.0 - market.yesNoSum;
    const potentialProfit = spreadSize * availableLiquidity;

    const opportunity: Opportunity = {
      type: OpportunityType.ARBITRAGE,
      marketId: market.marketId,
      question: market.question,
      timestamp: new Date(),
      description: `YES+NO sum = ${market.yesNoSum.toFixed(4)} (threshold: ${this.arbitrageThreshold})`,
      potentialProfit,
      yesNoSum: market.yesNoSum,
      availableLiquidity,
      spreadPct: 0,
      tokenSide: '',
      currentVolume: 0,
      averageVolume: 0,
      spikeMultiplier: 0,
      makerCount: 0,
      volume: 0,
      relatedMarketId: '',
      priceDifference: 0,
    };

    console.log(
      `ARBITRAGE: ${market.question.slice(0, 50)}... ` +
        `sum=${market.yesNoSum.toFixed(4)}, liq=$${availableLiquidity.toFixed(2)}`
    );

    return opportunity;
  }

  /**
   * Detect wide spread opportunities for market making.
   */
  detectWideSpread(market: MarketData): Opportunity[] {
    const opportunities: Opportunity[] = [];

    const tokens = [
      { token: market.yesToken, side: 'YES' },
      { token: market.noToken, side: 'NO' },
    ];

    for (const { token, side } of tokens) {
      if (!token || token.spreadPct <= this.wideSpreadThreshold) {
        continue;
      }

      const opportunity: Opportunity = {
        type: OpportunityType.WIDE_SPREAD,
        marketId: market.marketId,
        question: market.question,
        timestamp: new Date(),
        description: `${side} spread = ${(token.spreadPct * 100).toFixed(2)}% (threshold: ${(this.wideSpreadThreshold * 100).toFixed(0)}%)`,
        potentialProfit: 0,
        yesNoSum: 0,
        availableLiquidity: market.totalLiquidityAtBest,
        spreadPct: token.spreadPct,
        tokenSide: side,
        currentVolume: 0,
        averageVolume: 0,
        spikeMultiplier: 0,
        makerCount: 0,
        volume: 0,
        relatedMarketId: '',
        priceDifference: 0,
      };

      opportunities.push(opportunity);
    }

    return opportunities;
  }

  /**
   * Detect significant volume spikes.
   */
  detectVolumeSpike(market: MarketData): Opportunity | null {
    const avgVolume = this.scanner.get1hVolumeAverage(market.marketId);

    if (avgVolume <= 0) {
      return null;
    }

    const currentVolume = market.volume24h;
    const multiplier = currentVolume / avgVolume;

    if (multiplier < this.volumeSpikeMultiplier) {
      return null;
    }

    const opportunity: Opportunity = {
      type: OpportunityType.VOLUME_SPIKE,
      marketId: market.marketId,
      question: market.question,
      timestamp: new Date(),
      description: `Volume ${multiplier.toFixed(1)}x above 1h average`,
      potentialProfit: 0,
      yesNoSum: 0,
      availableLiquidity: 0,
      spreadPct: 0,
      tokenSide: '',
      currentVolume,
      averageVolume: avgVolume,
      spikeMultiplier: multiplier,
      makerCount: 0,
      volume: 0,
      relatedMarketId: '',
      priceDifference: 0,
    };

    console.log(
      `VOLUME SPIKE: ${market.question.slice(0, 50)}... ` +
        `${multiplier.toFixed(1)}x ($${currentVolume.toLocaleString()} vs $${avgVolume.toLocaleString()} avg)`
    );

    return opportunity;
  }

  /**
   * Detect thin order books on high-volume markets.
   */
  detectThinBook(market: MarketData): Opportunity | null {
    const MIN_VOLUME_FOR_THIN_BOOK = 10000;

    if (market.volume24h < MIN_VOLUME_FOR_THIN_BOOK) {
      return null;
    }

    if (market.totalActiveMakers >= this.thinBookMakerCount) {
      return null;
    }

    const opportunity: Opportunity = {
      type: OpportunityType.THIN_BOOK,
      marketId: market.marketId,
      question: market.question,
      timestamp: new Date(),
      description: `Only ${market.totalActiveMakers} active makers with $${market.volume24h.toLocaleString()} volume`,
      potentialProfit: 0,
      yesNoSum: 0,
      availableLiquidity: 0,
      spreadPct: 0,
      tokenSide: '',
      currentVolume: 0,
      averageVolume: 0,
      spikeMultiplier: 0,
      makerCount: market.totalActiveMakers,
      volume: market.volume24h,
      relatedMarketId: '',
      priceDifference: 0,
    };

    return opportunity;
  }

  /**
   * Extract a key for grouping related markets (for mispricing detection).
   */
  private extractMarketKey(question: string): string | null {
    const patterns = [
      // Price targets
      /(BTC|ETH|SOL|XRP)\s*[><=]+\s*\$?(\d+[KkMm]?)/i,
      // Election/political
      /Will\s+(\w+)\s+(win|be elected|become)/i,
      // Event outcomes
      /(\w+)\s+(happens?|occurs?|passes?)\s+by/i,
    ];

    for (const pattern of patterns) {
      const match = question.match(pattern);
      if (match) {
        return match
          .slice(1)
          .filter(Boolean)
          .map((g) => g.toLowerCase())
          .join('_');
      }
    }

    return null;
  }

  /**
   * Detect mispricing between correlated markets.
   */
  detectMispricing(markets: MarketData[]): Opportunity[] {
    const opportunities: Opportunity[] = [];
    const groups: Map<string, MarketData[]> = new Map();

    // Group markets by their key
    for (const market of markets) {
      const key = this.extractMarketKey(market.question);
      if (key) {
        if (!groups.has(key)) {
          groups.set(key, []);
        }
        groups.get(key)!.push(market);
      }
    }

    // Analyze each group for mispricing
    for (const [key, group] of groups) {
      if (group.length < 2) continue;

      const datedMarkets = group.filter((m) => m.endDate);
      if (datedMarkets.length < 2) continue;

      datedMarkets.sort((a, b) => a.endDate!.getTime() - b.endDate!.getTime());

      for (let i = 0; i < datedMarkets.length - 1; i++) {
        const earlier = datedMarkets[i];
        const later = datedMarkets[i + 1];

        const earlierYes = getYesPrice(earlier);
        const laterYes = getYesPrice(later);

        if (earlierYes > 0 && laterYes > 0) {
          const priceDiff = earlierYes - laterYes;

          if (priceDiff > 0.05) {
            const opportunity: Opportunity = {
              type: OpportunityType.MISPRICING,
              marketId: earlier.marketId,
              question: earlier.question,
              timestamp: new Date(),
              description: `Earlier event priced higher than later: ${(earlierYes * 100).toFixed(2)}% vs ${(laterYes * 100).toFixed(2)}%`,
              potentialProfit: 0,
              yesNoSum: 0,
              availableLiquidity: 0,
              spreadPct: 0,
              tokenSide: '',
              currentVolume: 0,
              averageVolume: 0,
              spikeMultiplier: 0,
              makerCount: 0,
              volume: 0,
              relatedMarketId: later.marketId,
              priceDifference: priceDiff,
            };

            console.log(
              `MISPRICING: ${key} - earlier=${(earlierYes * 100).toFixed(2)}%, later=${(laterYes * 100).toFixed(2)}%`
            );

            opportunities.push(opportunity);
          }
        }
      }
    }

    return opportunities;
  }

  /**
   * Analyze a market snapshot and detect all opportunities.
   */
  analyzeSnapshot(snapshot: MarketSnapshot): Opportunity[] {
    const allOpportunities: Opportunity[] = [];

    for (const market of snapshot.markets) {
      // Arbitrage
      const arb = this.detectArbitrage(market);
      if (arb) allOpportunities.push(arb);

      // Wide spreads
      const spreads = this.detectWideSpread(market);
      allOpportunities.push(...spreads);

      // Volume spikes
      const spike = this.detectVolumeSpike(market);
      if (spike) allOpportunities.push(spike);

      // Thin books
      const thin = this.detectThinBook(market);
      if (thin) allOpportunities.push(thin);
    }

    // Mispricing (needs all markets at once)
    const mispricings = this.detectMispricing(snapshot.markets);
    allOpportunities.push(...mispricings);

    // Update counts (don't accumulate opportunities in memory - they're stored in DB)
    for (const op of allOpportunities) {
      const count = this.opportunityCounts.get(op.type) || 0;
      this.opportunityCounts.set(op.type, count + 1);
    }

    const countByType = {
      arb: allOpportunities.filter((o) => o.type === OpportunityType.ARBITRAGE).length,
      spread: allOpportunities.filter((o) => o.type === OpportunityType.WIDE_SPREAD).length,
      spike: allOpportunities.filter((o) => o.type === OpportunityType.VOLUME_SPIKE).length,
      thin: allOpportunities.filter((o) => o.type === OpportunityType.THIN_BOOK).length,
      misprice: allOpportunities.filter((o) => o.type === OpportunityType.MISPRICING).length,
    };

    console.log(
      `Analysis complete: ${allOpportunities.length} opportunities detected ` +
        `(arb=${countByType.arb}, spread=${countByType.spread}, ` +
        `spike=${countByType.spike}, thin=${countByType.thin}, misprice=${countByType.misprice})`
    );

    // Update snapshot with opportunities
    snapshot.opportunities = allOpportunities;

    return allOpportunities;
  }

  getStats(): DetectorStats {
    const byType: Record<string, number> = {};
    let total = 0;
    for (const [type, count] of this.opportunityCounts) {
      byType[type] = count;
      total += count;
    }

    return {
      totalOpportunities: total,
      byType,
      recent1h: 0, // No longer tracking in memory
      thresholds: {
        arbitrage: this.arbitrageThreshold,
        wideSpread: this.wideSpreadThreshold,
        volumeSpikeMultiplier: this.volumeSpikeMultiplier,
        thinBookMakerCount: this.thinBookMakerCount,
      },
    };
  }

}

/**
 * Create an OpportunityDetector using environment variables.
 */
export function createDetectorFromEnv(scanner: MarketScanner): OpportunityDetector {
  return new OpportunityDetector({
    scanner,
    arbitrageThreshold: parseFloat(process.env.ARBITRAGE_THRESHOLD || '0.995'),
    wideSpreadThreshold: parseFloat(process.env.WIDE_SPREAD_THRESHOLD || '0.05'),
    volumeSpikeMultiplier: parseFloat(process.env.VOLUME_SPIKE_MULTIPLIER || '3.0'),
    thinBookMakerCount: parseInt(process.env.THIN_BOOK_MAKER_COUNT || '5'),
  });
}

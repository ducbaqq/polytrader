/**
 * Market scanner for the No-betting paper trading strategy.
 * Polls Polymarket API for new markets matching basic criteria.
 *
 * The scanner is now direction-agnostic - it returns both YES and NO prices
 * and lets the monitors decide which side to trade based on their strategy.
 */

import { PolymarketClient } from '../apiClient';
import { GammaMarket } from '../types';
import { StrategyConfig, detectCategoryFromQuestion } from './config';
import { ScannedMarket, ScanResult, EligibleMarket } from './types';
import {
  wasMarketScanned,
  recordScannedMarket,
} from './repository';
import { printProgress, clearProgress } from './dashboard';

export type ProgressCallback = (current: number, total: number, rejected: number, eligible: number) => void;

/**
 * Check if a rejection reason is permanent (market won't become eligible).
 * Permanent rejections should be recorded to avoid re-scanning.
 * Temporary rejections (price, time, volume, edge) should NOT be recorded
 * so markets get re-evaluated when conditions change.
 */
function isPermanentRejection(reason: string | undefined): boolean {
  if (!reason) return false;

  // Permanent: structural issues that won't change
  const permanentPatterns = [
    'No market data',
    'No token',
    'Category',  // Wrong category won't change
  ];

  return permanentPatterns.some(pattern => reason.includes(pattern));
}

/**
 * Simple semaphore for concurrency control.
 */
class Semaphore {
  private running = 0;
  private queue: (() => void)[] = [];

  constructor(private limit: number) {}

  async acquire(): Promise<void> {
    if (this.running < this.limit) {
      this.running++;
      return;
    }
    await new Promise<void>(resolve => this.queue.push(resolve));
    this.running++;
  }

  release(): void {
    this.running--;
    const next = this.queue.shift();
    if (next) next();
  }
}

/**
 * Scanner for finding markets that match basic criteria.
 * Direction-agnostic - returns both YES and NO prices for monitors to decide.
 */
export class MarketScanner {
  private client: PolymarketClient;
  private config: StrategyConfig;
  private concurrency: number;

  constructor(client: PolymarketClient, config: StrategyConfig, concurrency: number = 10) {
    this.client = client;
    this.config = config;
    this.concurrency = concurrency;
  }

  /**
   * Scan for markets matching basic criteria (category, volume, duration).
   * Returns direction-agnostic ScannedMarket objects with both YES and NO prices.
   * Does NOT open positions - that's the monitor's job now.
   *
   * @param onProgress - Optional callback for progress updates
   * @param silent - If true, suppress console output (for dashboard mode)
   */
  async scan(onProgress?: ProgressCallback, silent: boolean = false): Promise<ScanResult> {
    const result: ScanResult = {
      timestamp: new Date(),
      marketsScanned: 0,
      scannedMarkets: [],
      eligibleMarkets: [], // Legacy - kept for compatibility
      rejectedCount: 0,
      rejectionReasons: {},
    };

    try {
      // Fetch all active markets
      const markets = await this.client.getAllMarkets(true, undefined, 0);
      result.marketsScanned = markets.length;

      if (!silent) console.log(`Scanning ${markets.length} markets...`);

      // Filter by target categories using keyword detection
      // (API doesn't provide categories for open markets)
      // Also filter by volume early to avoid unnecessary API calls
      const categoryMarkets: Array<{ market: GammaMarket; detectedCategory: string }> = [];

      for (const market of markets) {
        // Early volume filter from Gamma data
        const volume = market.volumeNum || market.volume24hr || 0;
        if (volume < this.config.minVolume || volume > this.config.maxVolume) {
          continue;
        }

        // Try API category first, then keyword detection
        let category = market.category;
        if (!category) {
          category = detectCategoryFromQuestion(market.question) || undefined;
        }

        if (category && this.config.categories.includes(category)) {
          categoryMarkets.push({ market, detectedCategory: category });
        }
      }

      if (!silent) console.log(`Found ${categoryMarkets.length} markets in target categories: ${this.config.categories.join(', ')} (concurrency: ${this.concurrency})`);

      // Process markets concurrently with semaphore
      const semaphore = new Semaphore(this.concurrency);
      let processed = 0;

      const processWithSemaphore = async (market: GammaMarket, detectedCategory: string) => {
        await semaphore.acquire();
        try {
          await this.processMarket(market, result, detectedCategory, silent);
        } finally {
          semaphore.release();
          processed++;

          // Report progress
          if (onProgress) {
            onProgress(processed, categoryMarkets.length, result.rejectedCount, result.scannedMarkets.length);
          } else if (!silent && processed % 50 === 0) {
            printProgress(processed, categoryMarkets.length, result.rejectedCount, result.scannedMarkets.length);
          }
        }
      };

      // Launch all tasks concurrently (semaphore controls actual parallelism)
      await Promise.all(
        categoryMarkets.map(({ market, detectedCategory }) =>
          processWithSemaphore(market, detectedCategory)
        )
      );

      // Clear progress line before final message
      if (!silent && !onProgress) {
        clearProgress();
        console.log(`Scan complete: ${result.scannedMarkets.length} markets found`);
      }
    } catch (error) {
      if (!silent) console.error('Error during scan:', error);
    }

    return result;
  }

  /**
   * Process a single market - direction-agnostic.
   * Returns market data with both YES and NO prices for monitors to evaluate.
   */
  private async processMarket(market: GammaMarket, result: ScanResult, detectedCategory: string, _silent: boolean = false): Promise<void> {
    const marketId = market.id;

    // Skip if already scanned with permanent rejection
    if (await wasMarketScanned(marketId)) {
      return;
    }

    // Get market details
    const marketData = await this.client.buildMarketData(market);
    if (!marketData) {
      await recordScannedMarket(marketId, false, 'No market data');
      result.rejectedCount++;
      result.rejectionReasons['No market data'] = (result.rejectionReasons['No market data'] || 0) + 1;
      return;
    }

    // Need both tokens for direction-agnostic scanning
    const yesToken = marketData.yesToken;
    const noToken = marketData.noToken;

    if (!yesToken || !noToken) {
      await recordScannedMarket(marketId, false, 'Missing tokens');
      result.rejectedCount++;
      result.rejectionReasons['No token'] = (result.rejectionReasons['No token'] || 0) + 1;
      return;
    }

    // Get YES price (use best ask for buying)
    let yesPrice = 0;
    if (yesToken.bestAsk) {
      yesPrice = yesToken.bestAsk.price;
    } else if (yesToken.bestBid) {
      yesPrice = yesToken.bestBid.price;
    }

    // Get NO price (use best ask for buying)
    let noPrice = 0;
    if (noToken.bestAsk) {
      noPrice = noToken.bestAsk.price;
    } else if (noToken.bestBid) {
      noPrice = noToken.bestBid.price;
    }

    // Need at least one price
    if (yesPrice === 0 && noPrice === 0) {
      // Temporary rejection - don't persist, liquidity could appear
      result.rejectedCount++;
      result.rejectionReasons['No price'] = (result.rejectionReasons['No price'] || 0) + 1;
      return;
    }

    // Check basic duration filter (not price/edge - monitors decide that)
    if (marketData.endDate) {
      const timeToEndMs = marketData.endDate.getTime() - Date.now();
      const daysToEnd = timeToEndMs / (1000 * 60 * 60 * 24);

      if (daysToEnd < this.config.minDurationDays) {
        // Temporary rejection - conditions change over time
        result.rejectedCount++;
        result.rejectionReasons['Duration'] = (result.rejectionReasons['Duration'] || 0) + 1;
        return;
      }
      if (daysToEnd > this.config.maxDurationDays) {
        // Temporary rejection
        result.rejectedCount++;
        result.rejectionReasons['Duration'] = (result.rejectionReasons['Duration'] || 0) + 1;
        return;
      }
    } else {
      await recordScannedMarket(marketId, false, 'No end date');
      result.rejectedCount++;
      result.rejectionReasons['No end date'] = (result.rejectionReasons['No end date'] || 0) + 1;
      return;
    }

    // Market passes basic criteria! Create direction-agnostic ScannedMarket
    const ageHours = marketData.createdAt
      ? (Date.now() - marketData.createdAt.getTime()) / (1000 * 60 * 60)
      : 0;

    const daysToResolution = marketData.endDate
      ? (marketData.endDate.getTime() - Date.now()) / (1000 * 60 * 60 * 24)
      : 0;

    const scannedMarket: ScannedMarket = {
      marketId,
      question: marketData.question,
      category: detectedCategory,
      yesTokenId: yesToken.tokenId,
      noTokenId: noToken.tokenId,
      yesPrice,
      noPrice,
      volume24h: marketData.volume24h,
      createdAt: marketData.createdAt!,
      endDate: marketData.endDate!,
      ageHours,
      daysToResolution,
    };

    result.scannedMarkets.push(scannedMarket);

    // Don't persist to scanned_markets - let monitors decide
    // (they will record when they open positions)
  }
}

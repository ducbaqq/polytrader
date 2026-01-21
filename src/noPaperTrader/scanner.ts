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
import { ScannedMarket, ScanResult } from './types';
import {
  wasMarketScanned,
  recordScannedMarket,
} from './repository';
import { printProgress, clearProgress } from './dashboard';
import { WSPriceProvider } from './wsProvider';

export type ProgressCallback = (current: number, total: number, rejected: number, eligible: number) => void;

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
  private wsProvider: WSPriceProvider | null;

  constructor(
    client: PolymarketClient,
    config: StrategyConfig,
    concurrency: number = 10,
    wsProvider?: WSPriceProvider
  ) {
    this.client = client;
    this.config = config;
    this.concurrency = concurrency;
    this.wsProvider = wsProvider || null;
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
   * Uses WebSocket cache first for prices, falls back to REST API.
   */
  private async processMarket(market: GammaMarket, result: ScanResult, detectedCategory: string, _silent: boolean = false): Promise<void> {
    const marketId = market.id;

    // Skip if already scanned with permanent rejection
    if (await wasMarketScanned(marketId)) {
      return;
    }

    // Try WebSocket cache first - can skip REST entirely if we have prices
    let yesPrice = 0;
    let noPrice = 0;
    let yesTokenId = '';
    let noTokenId = '';
    let endDate: Date | null = null;
    let createdAt: Date | null = null;
    let volume24h = 0;
    let usedWSCache = false;

    if (this.wsProvider && this.wsProvider.isConnected()) {
      const wsPrices = this.wsProvider.getMarketPrices(marketId);
      if (wsPrices.yes && wsPrices.no) {
        yesPrice = wsPrices.yes.bestAsk?.price || wsPrices.yes.bestBid?.price || 0;
        noPrice = wsPrices.no.bestAsk?.price || wsPrices.no.bestBid?.price || 0;
        yesTokenId = wsPrices.yes.assetId;
        noTokenId = wsPrices.no.assetId;

        // Get metadata from GammaMarket (no REST call needed)
        if (market.endDate) endDate = new Date(market.endDate);
        if (market.createdAt) createdAt = new Date(market.createdAt);
        volume24h = market.volume24hr || market.volumeNum || 0;
        usedWSCache = true;
      }
    }

    // Only call REST API if we don't have WebSocket data
    if (!usedWSCache) {
      const marketData = await this.client.buildMarketData(market);
      if (!marketData) {
        await recordScannedMarket(marketId, false, 'No market data');
        result.rejectedCount++;
        result.rejectionReasons['No market data'] = (result.rejectionReasons['No market data'] || 0) + 1;
        return;
      }

      const yesToken = marketData.yesToken;
      const noToken = marketData.noToken;

      if (!yesToken || !noToken) {
        await recordScannedMarket(marketId, false, 'Missing tokens');
        result.rejectedCount++;
        result.rejectionReasons['No token'] = (result.rejectionReasons['No token'] || 0) + 1;
        return;
      }

      yesTokenId = yesToken.tokenId;
      noTokenId = noToken.tokenId;
      yesPrice = yesToken.bestAsk?.price || yesToken.bestBid?.price || 0;
      noPrice = noToken.bestAsk?.price || noToken.bestBid?.price || 0;
      endDate = marketData.endDate;
      createdAt = marketData.createdAt;
      volume24h = marketData.volume24h;
    }

    // Need at least one price
    if (yesPrice === 0 && noPrice === 0) {
      result.rejectedCount++;
      result.rejectionReasons['No price'] = (result.rejectionReasons['No price'] || 0) + 1;
      return;
    }

    // Check basic duration filter
    if (!endDate) {
      await recordScannedMarket(marketId, false, 'No end date');
      result.rejectedCount++;
      result.rejectionReasons['No end date'] = (result.rejectionReasons['No end date'] || 0) + 1;
      return;
    }

    const timeToEndMs = endDate.getTime() - Date.now();
    const daysToEnd = timeToEndMs / (1000 * 60 * 60 * 24);

    if (daysToEnd < this.config.minDurationDays || daysToEnd > this.config.maxDurationDays) {
      result.rejectedCount++;
      result.rejectionReasons['Duration'] = (result.rejectionReasons['Duration'] || 0) + 1;
      return;
    }

    // Market passes basic criteria
    const ageHours = createdAt ? (Date.now() - createdAt.getTime()) / (1000 * 60 * 60) : 0;

    const scannedMarket: ScannedMarket = {
      marketId,
      question: market.question,
      category: detectedCategory,
      yesTokenId,
      noTokenId,
      yesPrice,
      noPrice,
      volume24h,
      createdAt: createdAt || new Date(),
      endDate,
      ageHours,
      daysToResolution: daysToEnd,
    };

    result.scannedMarkets.push(scannedMarket);

    // Don't persist to scanned_markets - let monitors decide
    // (they will record when they open positions)
  }
}

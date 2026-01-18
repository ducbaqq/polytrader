/**
 * Market scanner for the No-betting paper trading strategy.
 * Polls Polymarket API for new markets matching entry conditions.
 */

import { randomUUID } from 'crypto';
import { PolymarketClient } from '../apiClient';
import { GammaMarket } from '../types';
import { StrategyConfig, checkMarketEligibility, detectCategoryFromQuestion } from './config';
import { EligibleMarket, ScanResult, Position, Trade } from './types';
import {
  getPortfolio,
  hasPositionForMarket,
  wasMarketScanned,
  recordScannedMarket,
  insertPosition,
  insertTrade,
  updatePortfolioOnOpen,
} from './repository';
import { PriceHistoryFetcher } from '../alphaAnalysis/priceHistoryFetcher';
import { DEFAULT_CONFIG as ALPHA_CONFIG } from '../alphaAnalysis/types';

/**
 * Scanner for finding eligible markets.
 */
export class MarketScanner {
  private client: PolymarketClient;
  private config: StrategyConfig;
  private priceHistoryFetcher: PriceHistoryFetcher;

  constructor(client: PolymarketClient, config: StrategyConfig) {
    this.client = client;
    this.config = config;
    this.priceHistoryFetcher = new PriceHistoryFetcher({
      ...ALPHA_CONFIG,
      volumeTiers: { tier1MinVolume: 0, tier2MinVolume: 0 }, // Always fetch full history
      rateLimit: { callsPerSecond: 5, maxRetries: 3, baseDelayMs: 1000 },
    });
  }

  /**
   * Scan for eligible markets and open positions.
   */
  async scan(): Promise<ScanResult> {
    const result: ScanResult = {
      timestamp: new Date(),
      marketsScanned: 0,
      eligibleMarkets: [],
      positionsOpened: 0,
      rejectedCount: 0,
      rejectionReasons: {},
    };

    try {
      // Fetch all active markets
      const markets = await this.client.getAllMarkets(true, undefined, 0);
      result.marketsScanned = markets.length;

      console.log(`Scanning ${markets.length} markets...`);

      // Filter by target categories using keyword detection
      // (API doesn't provide categories for open markets)
      const categoryMarkets: Array<{ market: GammaMarket; detectedCategory: string }> = [];

      for (const market of markets) {
        // Try API category first, then keyword detection
        let category = market.category;
        if (!category) {
          category = detectCategoryFromQuestion(market.question) || undefined;
        }

        if (category && this.config.categories.includes(category)) {
          categoryMarkets.push({ market, detectedCategory: category });
        }
      }

      console.log(`Found ${categoryMarkets.length} markets in target categories: ${this.config.categories.join(', ')}`);

      // Check each market
      for (const { market, detectedCategory } of categoryMarkets) {
        await this.processMarket(market, result, detectedCategory);
      }

      console.log(`Scan complete: ${result.eligibleMarkets.length} eligible, ${result.positionsOpened} positions opened`);
    } catch (error) {
      console.error('Error during scan:', error);
    }

    return result;
  }

  /**
   * Process a single market.
   */
  private async processMarket(market: GammaMarket, result: ScanResult, detectedCategory: string): Promise<void> {
    const marketId = market.id;

    // Skip if already scanned
    if (await wasMarketScanned(marketId)) {
      return;
    }

    // Skip if we already have a position
    if (await hasPositionForMarket(marketId)) {
      return;
    }

    // Get market details
    const marketData = await this.client.buildMarketData(market);
    if (!marketData || !marketData.noToken) {
      await recordScannedMarket(marketId, false, 'No market data or No token');
      result.rejectedCount++;
      result.rejectionReasons['No market data'] = (result.rejectionReasons['No market data'] || 0) + 1;
      return;
    }

    // Get No price (use best ask for buying)
    let noPrice = 0;
    if (marketData.noToken.bestAsk) {
      noPrice = marketData.noToken.bestAsk.price;
    } else if (marketData.noToken.bestBid) {
      noPrice = marketData.noToken.bestBid.price;
    }

    if (noPrice === 0) {
      await recordScannedMarket(marketId, false, 'No price available');
      result.rejectedCount++;
      result.rejectionReasons['No price'] = (result.rejectionReasons['No price'] || 0) + 1;
      return;
    }

    // Check eligibility using the detected category
    const eligibility = checkMarketEligibility(
      detectedCategory,
      noPrice,
      marketData.volume24h,
      marketData.endDate,
      this.config
    );

    if (!eligibility.eligible) {
      await recordScannedMarket(marketId, false, eligibility.reason);
      result.rejectedCount++;
      const reason = eligibility.reason?.split(' ')[0] || 'Unknown';
      result.rejectionReasons[reason] = (result.rejectionReasons[reason] || 0) + 1;
      return;
    }

    // Brief opportunity window check - skip if price below threshold too long
    const timeBelowRatio = await this.checkTimeBelowThreshold(
      marketData.noToken.tokenId,
      this.config.maxNoPrice
    );

    if (timeBelowRatio > this.config.maxTimeBelowThreshold) {
      const reason = `Price below threshold ${(timeBelowRatio * 100).toFixed(0)}% of time (max ${(this.config.maxTimeBelowThreshold * 100).toFixed(0)}%)`;
      await recordScannedMarket(marketId, false, reason);
      result.rejectedCount++;
      result.rejectionReasons['Price'] = (result.rejectionReasons['Price'] || 0) + 1;
      return;
    }

    // Market is eligible!
    const ageHours = marketData.createdAt
      ? (Date.now() - marketData.createdAt.getTime()) / (1000 * 60 * 60)
      : 0;

    const daysToResolution = marketData.endDate
      ? (marketData.endDate.getTime() - Date.now()) / (1000 * 60 * 60 * 24)
      : 0;

    const eligibleMarket: EligibleMarket = {
      marketId,
      tokenId: marketData.noToken.tokenId,
      question: marketData.question,
      category: detectedCategory,
      noPrice,
      volume: marketData.volume24h,
      createdAt: marketData.createdAt!,
      endDate: marketData.endDate!,
      edge: eligibility.edge!,
      ageHours,
      daysToResolution,
    };

    result.eligibleMarkets.push(eligibleMarket);

    // Try to open position
    const opened = await this.openPosition(eligibleMarket);
    if (opened) {
      result.positionsOpened++;
      await recordScannedMarket(marketId, true, undefined, true);
    } else {
      await recordScannedMarket(marketId, true, 'Insufficient capital');
    }
  }

  /**
   * Open a position for an eligible market.
   */
  private async openPosition(market: EligibleMarket): Promise<boolean> {
    // Check if we have enough capital
    const portfolio = await getPortfolio();
    if (!portfolio) {
      console.log('Portfolio not initialized');
      return false;
    }

    if (portfolio.cashBalance < this.config.positionSize) {
      console.log(`Insufficient capital: $${portfolio.cashBalance.toFixed(2)} < $${this.config.positionSize}`);
      return false;
    }

    // Calculate position details
    const entryPrice = market.noPrice;
    const slippageCost = this.config.positionSize * this.config.slippagePercent;
    const entryPriceAfterSlippage = entryPrice * (1 + this.config.slippagePercent);
    const costBasis = this.config.positionSize + slippageCost;
    const quantity = this.config.positionSize / entryPriceAfterSlippage;

    const positionId = randomUUID();
    const tradeId = randomUUID();

    // Create position
    const position: Position = {
      id: positionId,
      marketId: market.marketId,
      tokenId: market.tokenId,
      question: market.question,
      category: market.category,
      entryPrice,
      entryPriceAfterSlippage,
      quantity,
      costBasis,
      estimatedEdge: market.edge,
      entryTime: new Date(),
      endDate: market.endDate,
      status: 'OPEN',
    };

    // Create trade record
    const trade: Trade = {
      id: tradeId,
      positionId,
      marketId: market.marketId,
      question: market.question,
      category: market.category,
      side: 'BUY',
      tokenSide: 'NO',
      price: entryPrice,
      priceAfterSlippage: entryPriceAfterSlippage,
      quantity,
      value: this.config.positionSize,
      slippageCost,
      timestamp: new Date(),
      reason: 'Entry',
    };

    // Persist
    await insertPosition(position);
    await insertTrade(trade);
    await updatePortfolioOnOpen(costBasis);

    console.log(`\n📈 POSITION OPENED`);
    console.log(`   Market: ${market.question.substring(0, 60)}...`);
    console.log(`   Category: ${market.category}`);
    console.log(`   No Price: ${(entryPrice * 100).toFixed(1)}%`);
    console.log(`   Edge: ${(market.edge * 100).toFixed(1)}%`);
    console.log(`   Size: $${this.config.positionSize}`);
    console.log(`   Quantity: ${quantity.toFixed(2)} contracts`);
    console.log(`   Resolves: ${market.endDate.toISOString().split('T')[0]}`);

    return true;
  }

  /**
   * Calculate what % of price history had No price at/below threshold.
   * Returns 0 on error (permissive - allows market through).
   */
  private async checkTimeBelowThreshold(
    noTokenId: string,
    priceThreshold: number
  ): Promise<number> {
    try {
      const result = await this.priceHistoryFetcher.fetchFullHistory(noTokenId);
      if (!result.success || result.history.length === 0) return 0;

      const belowCount = result.history.filter(p => parseFloat(p.p) <= priceThreshold).length;
      return belowCount / result.history.length;
    } catch {
      return 0;
    }
  }
}

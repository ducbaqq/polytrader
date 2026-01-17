/**
 * Market scanner for the No-betting paper trading strategy.
 * Polls Polymarket API for new markets matching entry conditions.
 */

import { v4 as uuidv4 } from 'uuid';
import { PolymarketClient } from '../apiClient';
import { GammaMarket } from '../types';
import { StrategyConfig, checkMarketEligibility } from './config';
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

/**
 * Scanner for finding eligible markets.
 */
export class MarketScanner {
  private client: PolymarketClient;
  private config: StrategyConfig;

  constructor(client: PolymarketClient, config: StrategyConfig) {
    this.client = client;
    this.config = config;
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

      // Filter by target categories first (performance optimization)
      const categoryMarkets = markets.filter(m =>
        this.config.categories.includes(m.category || '')
      );

      console.log(`Found ${categoryMarkets.length} markets in target categories: ${this.config.categories.join(', ')}`);

      // Check each market
      for (const market of categoryMarkets) {
        await this.processMarket(market, result);
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
  private async processMarket(market: GammaMarket, result: ScanResult): Promise<void> {
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

    // Check eligibility
    const eligibility = checkMarketEligibility(
      marketData.category,
      noPrice,
      marketData.volume24h,
      marketData.createdAt,
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
      category: marketData.category,
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

    const positionId = uuidv4();
    const tradeId = uuidv4();

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
}

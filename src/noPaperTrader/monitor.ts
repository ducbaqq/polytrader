/**
 * Position monitor for the paper trading strategy.
 * Now strategy-aware - each monitor instance handles one strategy.
 *
 * Responsibilities:
 * - Opening new positions based on strategy criteria
 * - Watching open positions for TP/SL triggers
 * - Handling market resolutions
 */

import { randomUUID } from 'crypto';
import axios from 'axios';
import { PolymarketClient } from '../apiClient';
import { StrategyConfig, getStrategy } from './config';
import { Position, Trade, MonitorResult, PositionStatus, ScannedMarket, StrategyId, StrategyDefinition } from './types';
import {
  getPortfolio,
  getOpenPositions,
  hasPositionForMarket,
  updatePosition,
  insertPosition,
  insertTrade,
  updatePortfolioOnOpen,
  updatePortfolioOnClose,
  recordScannedMarket,
} from './repository';
import { WSPriceProvider } from './wsProvider';

const GAMMA_API_URL = 'https://gamma-api.polymarket.com';

/**
 * Strategy-aware position monitor.
 * Each instance handles one strategy (yes-buyer or no-buyer).
 */
export class PositionMonitor {
  private client: PolymarketClient;
  private config: StrategyConfig;
  private strategyId: StrategyId;
  private strategy: StrategyDefinition;
  private wsProvider: WSPriceProvider | null;

  constructor(
    client: PolymarketClient,
    config: StrategyConfig,
    strategyId: StrategyId,
    wsProvider?: WSPriceProvider
  ) {
    this.client = client;
    this.config = config;
    this.strategyId = strategyId;
    this.strategy = getStrategy(strategyId);
    this.wsProvider = wsProvider || null;
  }

  getStrategyId(): StrategyId {
    return this.strategyId;
  }

  getStrategy(): StrategyDefinition {
    return this.strategy;
  }

  /**
   * Monitor all open positions and handle exits.
   * Optionally also check scanned markets for entry opportunities.
   *
   * @param scannedMarkets - Optional markets from scanner to evaluate for entry
   */
  async monitor(scannedMarkets?: ScannedMarket[]): Promise<MonitorResult> {
    const result: MonitorResult = {
      timestamp: new Date(),
      positionsChecked: 0,
      positionsOpened: 0,
      takeProfitTriggered: 0,
      stopLossTriggered: 0,
      resolved: 0,
      stillOpen: 0,
    };

    try {
      // 1. Check for new entry opportunities if markets provided
      if (scannedMarkets && scannedMarkets.length > 0) {
        for (const market of scannedMarkets) {
          const opened = await this.checkForEntry(market);
          if (opened) {
            result.positionsOpened++;
          }
        }
      }

      // 2. Monitor existing positions for this strategy
      const positions = await getOpenPositions(this.strategyId);
      result.positionsChecked = positions.length;

      if (positions.length > 0) {
        console.log(`[${this.strategy.name}] Monitoring ${positions.length} open positions...`);

        for (const position of positions) {
          await this.checkPosition(position, result);
        }
      }

      result.stillOpen = positions.length - result.takeProfitTriggered - result.stopLossTriggered - result.resolved;

      if (result.positionsOpened > 0 || result.takeProfitTriggered > 0 || result.stopLossTriggered > 0 || result.resolved > 0) {
        console.log(`[${this.strategy.name}] Monitor complete: ${result.positionsOpened} opened, ${result.takeProfitTriggered} TP, ${result.stopLossTriggered} SL, ${result.resolved} resolved, ${result.stillOpen} still open`);
      }
    } catch (error) {
      console.error(`[${this.strategy.name}] Error during monitoring:`, error);
    }

    return result;
  }

  /**
   * Check if a scanned market meets entry criteria for this strategy.
   * If so, open a position.
   */
  async checkForEntry(market: ScannedMarket): Promise<boolean> {
    // Skip if we already have a position for this market in this strategy
    if (await hasPositionForMarket(market.marketId, this.strategyId)) {
      return false;
    }

    // Skip if we've reached max positions
    const openPositions = await getOpenPositions(this.strategyId);
    if (openPositions.length >= this.config.maxPositions) {
      return false;
    }

    // Get the price for this strategy's side (ask = buy price, bid = sell price)
    const askPrice = this.strategy.side === 'YES' ? market.yesPrice : market.noPrice;
    const bidPrice = this.strategy.side === 'YES' ? market.yesBidPrice : market.noBidPrice;
    const tokenId = this.strategy.side === 'YES' ? market.yesTokenId : market.noTokenId;

    // Skip if no prices available for our side
    if (askPrice === 0 || bidPrice === 0) {
      return false;
    }

    // Check bid-ask spread - skip if too wide
    // Spread = (ask - bid) / ask = how much we lose immediately after buying
    const spread = (askPrice - bidPrice) / askPrice;
    if (spread > this.config.maxSpread) {
      return false;
    }

    // Check price range against strategy config (use ask price = what we pay)
    const price = askPrice;
    if (price < this.strategy.minPrice || price > this.strategy.maxPrice) {
      return false;
    }

    // Calculate edge using strategy's category win rates
    const winRate = this.strategy.categoryWinRates[market.category];
    if (winRate === undefined) {
      return false; // Category not supported by this strategy
    }

    const edge = winRate - price;
    if (edge < this.strategy.minEdge) {
      return false;
    }

    // Check portfolio has capital
    const portfolio = await getPortfolio(this.strategyId);
    if (!portfolio || portfolio.cashBalance < this.config.positionSize) {
      return false;
    }

    // All checks passed - open position!
    return this.openPosition(market, tokenId, price, edge);
  }

  private async openPosition(
    market: ScannedMarket,
    tokenId: string,
    price: number,
    edge: number
  ): Promise<boolean> {
    const slippageCost = this.config.positionSize * this.config.slippagePercent;
    const entryPriceAfterSlippage = price * (1 + this.config.slippagePercent);
    const costBasis = this.config.positionSize + slippageCost;
    const quantity = this.config.positionSize / entryPriceAfterSlippage;

    const positionId = randomUUID();
    const tradeId = randomUUID();

    // Create position
    const position: Position = {
      id: positionId,
      strategyId: this.strategyId,
      marketId: market.marketId,
      tokenId,
      tokenSide: this.strategy.side,
      question: market.question,
      category: market.category,
      entryPrice: price,
      entryPriceAfterSlippage,
      quantity,
      costBasis,
      estimatedEdge: edge,
      entryTime: new Date(),
      endDate: market.endDate,
      status: 'OPEN',
    };

    // Create trade record
    const trade: Trade = {
      id: tradeId,
      strategyId: this.strategyId,
      positionId,
      marketId: market.marketId,
      question: market.question,
      category: market.category,
      side: 'BUY',
      tokenSide: this.strategy.side,
      price,
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
    await updatePortfolioOnOpen(costBasis, this.strategyId);
    await recordScannedMarket(market.marketId, true, undefined, true);

    console.log(`\n📈 [${this.strategy.name}] POSITION OPENED`);
    console.log(`   Market: ${market.question.substring(0, 60)}...`);
    console.log(`   Category: ${market.category}`);
    console.log(`   Side: ${this.strategy.side}`);
    console.log(`   ${this.strategy.side} Price: ${(price * 100).toFixed(1)}%`);
    console.log(`   Edge: ${(edge * 100).toFixed(1)}%`);
    console.log(`   Size: $${this.config.positionSize}`);
    console.log(`   Quantity: ${quantity.toFixed(2)} contracts`);
    console.log(`   Resolves: ${market.endDate.toISOString().split('T')[0]}`);

    return true;
  }

  /**
   * Check a single position for exit conditions.
   */
  private async checkPosition(position: Position, result: MonitorResult): Promise<void> {
    try {
      // Check if market is resolved
      const marketInfo = await this.getMarketInfo(position.marketId);

      if (marketInfo?.resolved) {
        await this.handleResolution(position, marketInfo);
        result.resolved++;
        return;
      }

      // Get current price for the token we hold (YES or NO)
      const currentPrice = await this.getCurrentPrice(position.tokenId);

      if (currentPrice === null) {
        console.log(`[${this.strategy.name}] Could not get price for position ${position.id}`);
        return;
      }

      // Take profit when price reaches 90¢ (close to $1 payout)
      if (currentPrice >= this.config.takeProfitThreshold) {
        await this.closePosition(position, currentPrice, 'CLOSED_TP', 'Take Profit');
        result.takeProfitTriggered++;
        return;
      }

      // Stop loss when price drops 25% from entry price
      const stopLossPrice = position.entryPriceAfterSlippage * (1 - this.config.stopLossThreshold);
      if (currentPrice <= stopLossPrice) {
        await this.closePosition(position, currentPrice, 'CLOSED_SL', 'Stop Loss');
        result.stopLossTriggered++;
        return;
      }

      // Position still open
    } catch (error) {
      console.error(`[${this.strategy.name}] Error checking position ${position.id}:`, error);
    }
  }

  /**
   * Get market info from Gamma API.
   */
  private async getMarketInfo(marketId: string): Promise<{
    resolved: boolean;
    winningOutcome?: string;
    resolutionPrice?: number;
  } | null> {
    try {
      const response = await axios.get(`${GAMMA_API_URL}/markets/${marketId}`);
      const data = response.data;

      // Parse outcome prices [yesPrice, noPrice]
      const prices = this.parseOutcomePrices(data.outcomePrices);

      // Market is resolved when closed or prices are terminal (0/1)
      const hasTerminalPrices = prices.length >= 2 && (
        (prices[0] >= 0.99 && prices[1] <= 0.01) ||
        (prices[0] <= 0.01 && prices[1] >= 0.99)
      );

      if (!data.closed && !hasTerminalPrices) {
        return { resolved: false };
      }

      if (prices.length < 2) {
        return { resolved: false };
      }

      // Determine winner: NO wins if noPrice >= 0.99 or yesPrice <= 0.01
      const noWins = prices[1] >= 0.99 || prices[0] <= 0.01;
      return {
        resolved: true,
        winningOutcome: noWins ? 'NO' : 'YES',
        resolutionPrice: noWins ? 1 : 0,
      };
    } catch (error: any) {
      if (error?.response?.status === 404) {
        return null;
      }
      console.error(`[${this.strategy.name}] Error getting market info for ${marketId}:`, error);
      return null;
    }
  }

  private parseOutcomePrices(outcomePrices: unknown): number[] {
    if (!outcomePrices) return [];
    if (typeof outcomePrices === 'string') {
      try {
        return JSON.parse(outcomePrices).map((p: unknown) => parseFloat(String(p)));
      } catch {
        return [];
      }
    }
    if (Array.isArray(outcomePrices)) {
      return outcomePrices.map((p: unknown) => parseFloat(String(p)));
    }
    return [];
  }

  /**
   * Get current bid price for selling a token.
   * Uses WebSocket cache first, falls back to REST API.
   */
  private async getCurrentPrice(tokenId: string): Promise<number | null> {
    // Try WebSocket cache first
    if (this.wsProvider?.isConnected()) {
      const cached = this.wsProvider.getPrice(tokenId);
      if (cached && this.wsProvider.isDataFresh(tokenId, 60000)) {
        return cached.bestBid?.price ?? cached.bestAsk?.price ?? null;
      }
    }

    // Fallback to REST API
    return this.getCurrentPriceRest(tokenId);
  }

  private async getCurrentPriceRest(tokenId: string): Promise<number | null> {
    try {
      const orderBook = await this.client.getOrderBook(tokenId);
      if (!orderBook) return null;

      const bids = orderBook.bids || [];
      const asks = orderBook.asks || [];

      if (bids.length > 0) {
        const prices = bids.map((b: any) => parseFloat(String(b.price || 0)));
        return Math.max(...prices);
      }

      if (asks.length > 0) {
        const prices = asks.map((a: any) => parseFloat(String(a.price || 0)));
        return Math.min(...prices);
      }

      return null;
    } catch (error) {
      console.error(`[${this.strategy.name}] Error getting price for token ${tokenId}:`, error);
      return null;
    }
  }

  /**
   * Handle market resolution.
   */
  private async handleResolution(
    position: Position,
    marketInfo: { resolved: boolean; winningOutcome?: string; resolutionPrice?: number }
  ): Promise<void> {
    // Resolution: we win $1 if our side wins, $0 otherwise
    const tokenSide = position.tokenSide || 'NO'; // Default for legacy positions
    const resolutionPrice = marketInfo.winningOutcome === tokenSide ? 1 : 0;

    // Calculate P&L
    const exitValue = position.quantity * resolutionPrice;
    const exitValueAfterSlippage = exitValue; // No slippage on resolution
    const pnl = exitValueAfterSlippage - position.costBasis;
    const pnlPercent = (pnl / position.costBasis) * 100;

    const isWin = pnl > 0;
    const exitReason = `Resolution (${marketInfo.winningOutcome} Won)`;

    // Create exit trade
    const tradeId = randomUUID();
    const trade: Trade = {
      id: tradeId,
      strategyId: position.strategyId,
      positionId: position.id,
      marketId: position.marketId,
      question: position.question,
      category: position.category,
      side: 'SELL',
      tokenSide,
      price: resolutionPrice,
      priceAfterSlippage: resolutionPrice,
      quantity: position.quantity,
      value: exitValue,
      slippageCost: 0,
      timestamp: new Date(),
      reason: exitReason,
    };

    // Update position
    position.status = 'CLOSED_RESOLVED';
    position.exitPrice = resolutionPrice;
    position.exitTime = new Date();
    position.exitReason = exitReason;
    position.realizedPnl = pnl;
    position.realizedPnlPercent = pnlPercent;

    // Persist
    await insertTrade(trade);
    await updatePosition(position);
    await updatePortfolioOnClose(exitValueAfterSlippage, pnl, isWin, position.strategyId);

    const emoji = isWin ? '💰' : '❌';
    console.log(`\n${emoji} [${this.strategy.name}] POSITION RESOLVED`);
    console.log(`   Market: ${position.question.substring(0, 60)}...`);
    console.log(`   Our Side: ${tokenSide}`);
    console.log(`   Outcome: ${marketInfo.winningOutcome}`);
    console.log(`   Entry: ${(position.entryPrice * 100).toFixed(1)}%`);
    console.log(`   Exit: ${(resolutionPrice * 100).toFixed(1)}%`);
    console.log(`   P&L: $${pnl.toFixed(2)} (${pnlPercent.toFixed(1)}%)`);
  }

  /**
   * Close a position (TP/SL).
   */
  private async closePosition(
    position: Position,
    currentPrice: number,
    status: PositionStatus,
    reason: string
  ): Promise<void> {
    const tokenSide = position.tokenSide || 'NO'; // Default to NO for legacy positions

    // Calculate exit with slippage (we're selling)
    const slippageCost = position.quantity * currentPrice * this.config.slippagePercent;
    const exitPriceAfterSlippage = currentPrice * (1 - this.config.slippagePercent);
    const exitValue = position.quantity * exitPriceAfterSlippage;

    // Calculate P&L
    const pnl = exitValue - position.costBasis;
    const pnlPercent = (pnl / position.costBasis) * 100;
    const isWin = pnl > 0;

    // Create exit trade
    const tradeId = randomUUID();
    const trade: Trade = {
      id: tradeId,
      strategyId: position.strategyId,
      positionId: position.id,
      marketId: position.marketId,
      question: position.question,
      category: position.category,
      side: 'SELL',
      tokenSide,
      price: currentPrice,
      priceAfterSlippage: exitPriceAfterSlippage,
      quantity: position.quantity,
      value: exitValue,
      slippageCost,
      timestamp: new Date(),
      reason,
    };

    // Update position
    position.status = status;
    position.exitPrice = exitPriceAfterSlippage;
    position.exitTime = new Date();
    position.exitReason = reason;
    position.realizedPnl = pnl;
    position.realizedPnlPercent = pnlPercent;

    // Persist
    await insertTrade(trade);
    await updatePosition(position);
    await updatePortfolioOnClose(exitValue, pnl, isWin, position.strategyId);

    const emoji = status === 'CLOSED_TP' ? '🎯' : '🛑';
    console.log(`\n${emoji} [${this.strategy.name}] ${reason.toUpperCase()} TRIGGERED`);
    console.log(`   Market: ${position.question.substring(0, 60)}...`);
    console.log(`   Side: ${tokenSide}`);
    console.log(`   Entry: ${(position.entryPrice * 100).toFixed(1)}%`);
    console.log(`   Exit: ${(currentPrice * 100).toFixed(1)}%`);
    console.log(`   P&L: $${pnl.toFixed(2)} (${pnlPercent.toFixed(1)}%)`);
  }
}

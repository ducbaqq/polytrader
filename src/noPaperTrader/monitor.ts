/**
 * Position monitor for the No-betting paper trading strategy.
 * Watches open positions for:
 * - Take profit triggers (No reaches target)
 * - Stop loss triggers (No drops below threshold)
 * - Market resolution
 */

import { v4 as uuidv4 } from 'uuid';
import axios from 'axios';
import { PolymarketClient } from '../apiClient';
import { StrategyConfig } from './config';
import { Position, Trade, MonitorResult, PositionStatus } from './types';
import {
  getOpenPositions,
  updatePosition,
  insertTrade,
  updatePortfolioOnClose,
} from './repository';

const GAMMA_API_URL = 'https://gamma-api.polymarket.com';

/**
 * Position monitor for open positions.
 */
export class PositionMonitor {
  private client: PolymarketClient;
  private config: StrategyConfig;

  constructor(client: PolymarketClient, config: StrategyConfig) {
    this.client = client;
    this.config = config;
  }

  /**
   * Monitor all open positions and handle exits.
   */
  async monitor(): Promise<MonitorResult> {
    const result: MonitorResult = {
      timestamp: new Date(),
      positionsChecked: 0,
      takeProfitTriggered: 0,
      stopLossTriggered: 0,
      resolved: 0,
      stillOpen: 0,
    };

    try {
      const positions = await getOpenPositions();
      result.positionsChecked = positions.length;

      if (positions.length === 0) {
        return result;
      }

      console.log(`Monitoring ${positions.length} open positions...`);

      for (const position of positions) {
        await this.checkPosition(position, result);
      }

      result.stillOpen = positions.length - result.takeProfitTriggered - result.stopLossTriggered - result.resolved;

      console.log(`Monitor complete: ${result.takeProfitTriggered} TP, ${result.stopLossTriggered} SL, ${result.resolved} resolved, ${result.stillOpen} still open`);
    } catch (error) {
      console.error('Error during monitoring:', error);
    }

    return result;
  }

  /**
   * Check a single position.
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

      // Get current No price
      const currentNoPrice = await this.getCurrentNoPrice(position.tokenId);

      if (currentNoPrice === null) {
        console.log(`Could not get price for position ${position.id}`);
        return;
      }

      // Check take profit
      if (currentNoPrice >= this.config.takeProfitThreshold) {
        await this.closePosition(position, currentNoPrice, 'CLOSED_TP', 'Take Profit');
        result.takeProfitTriggered++;
        return;
      }

      // Check stop loss
      if (currentNoPrice <= this.config.stopLossThreshold) {
        await this.closePosition(position, currentNoPrice, 'CLOSED_SL', 'Stop Loss');
        result.stopLossTriggered++;
        return;
      }

      // Position still open
    } catch (error) {
      console.error(`Error checking position ${position.id}:`, error);
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

      // Check various resolution indicators
      const resolved = data.closed === true || data.resolutionSource !== undefined;

      if (resolved) {
        // Try to determine winning outcome
        let winningOutcome: string | undefined;
        let resolutionPrice: number | undefined;

        // Check outcomePrices for resolution
        if (data.outcomePrices) {
          let prices: number[] = [];
          if (typeof data.outcomePrices === 'string') {
            try {
              prices = JSON.parse(data.outcomePrices);
            } catch {
              prices = [];
            }
          } else if (Array.isArray(data.outcomePrices)) {
            prices = data.outcomePrices.map((p: any) => parseFloat(String(p)));
          }

          // In a resolved market, one outcome should be 0 or 1
          if (prices.length >= 2) {
            // Yes price is first, No price is second
            const yesPrice = prices[0];
            const noPrice = prices[1];

            if (noPrice >= 0.99 || yesPrice <= 0.01) {
              winningOutcome = 'NO';
              resolutionPrice = 1;  // No wins = $1 per No contract
            } else if (yesPrice >= 0.99 || noPrice <= 0.01) {
              winningOutcome = 'YES';
              resolutionPrice = 0;  // Yes wins = $0 per No contract
            }
          }
        }

        return { resolved, winningOutcome, resolutionPrice };
      }

      return { resolved: false };
    } catch (error: any) {
      // 404 might mean market doesn't exist or is very old
      if (error?.response?.status === 404) {
        return null;
      }
      console.error(`Error getting market info for ${marketId}:`, error);
      return null;
    }
  }

  /**
   * Get current No price from order book.
   * For selling, we look at bids (what buyers will pay).
   */
  private async getCurrentNoPrice(tokenId: string): Promise<number | null> {
    try {
      const orderBook = await this.client.getOrderBook(tokenId);
      if (!orderBook) return null;

      const bids = orderBook.bids || [];
      const asks = orderBook.asks || [];

      // Best bid (highest price buyers will pay)
      if (bids.length > 0) {
        const prices = bids.map((b: any) => parseFloat(String(b.price || 0)));
        return Math.max(...prices);
      }

      // Fallback to best ask (lowest price sellers want)
      if (asks.length > 0) {
        const prices = asks.map((a: any) => parseFloat(String(a.price || 0)));
        return Math.min(...prices);
      }

      return null;
    } catch (error) {
      console.error(`Error getting price for token ${tokenId}:`, error);
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
    const resolutionPrice = marketInfo.resolutionPrice ?? (marketInfo.winningOutcome === 'NO' ? 1 : 0);

    // Calculate P&L
    // If No wins, each contract is worth $1
    // If Yes wins, each contract is worth $0
    const exitValue = position.quantity * resolutionPrice;
    const exitValueAfterSlippage = exitValue; // No slippage on resolution
    const pnl = exitValueAfterSlippage - position.costBasis;
    const pnlPercent = (pnl / position.costBasis) * 100;

    const isWin = pnl > 0;
    const exitReason = marketInfo.winningOutcome === 'NO' ? 'Resolution (No Won)' : 'Resolution (Yes Won)';

    // Create exit trade
    const tradeId = uuidv4();
    const trade: Trade = {
      id: tradeId,
      positionId: position.id,
      marketId: position.marketId,
      question: position.question,
      category: position.category,
      side: 'SELL',
      tokenSide: 'NO',
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
    await updatePortfolioOnClose(exitValueAfterSlippage, pnl, isWin);

    const emoji = isWin ? '💰' : '❌';
    console.log(`\n${emoji} POSITION RESOLVED`);
    console.log(`   Market: ${position.question.substring(0, 60)}...`);
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
    // Calculate exit with slippage (we're selling)
    const slippageCost = position.quantity * currentPrice * this.config.slippagePercent;
    const exitPriceAfterSlippage = currentPrice * (1 - this.config.slippagePercent);
    const exitValue = position.quantity * exitPriceAfterSlippage;

    // Calculate P&L
    const pnl = exitValue - position.costBasis;
    const pnlPercent = (pnl / position.costBasis) * 100;
    const isWin = pnl > 0;

    // Create exit trade
    const tradeId = uuidv4();
    const trade: Trade = {
      id: tradeId,
      positionId: position.id,
      marketId: position.marketId,
      question: position.question,
      category: position.category,
      side: 'SELL',
      tokenSide: 'NO',
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
    await updatePortfolioOnClose(exitValue, pnl, isWin);

    const emoji = status === 'CLOSED_TP' ? '🎯' : '🛑';
    console.log(`\n${emoji} ${reason.toUpperCase()} TRIGGERED`);
    console.log(`   Market: ${position.question.substring(0, 60)}...`);
    console.log(`   Entry: ${(position.entryPrice * 100).toFixed(1)}%`);
    console.log(`   Exit: ${(currentPrice * 100).toFixed(1)}%`);
    console.log(`   P&L: $${pnl.toFixed(2)} (${pnlPercent.toFixed(1)}%)`);
  }
}

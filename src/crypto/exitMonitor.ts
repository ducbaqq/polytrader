/**
 * Exit Monitor for Crypto Positions
 *
 * Continuously checks open positions for exit conditions:
 * 1. Profit Target: +15% from entry
 * 2. Stop Loss: -5% from entry
 * 3. Time Limit: 2 minutes max hold
 * 4. Reversal: Binance price crosses back threshold
 */

import {
  CryptoPosition,
  CryptoMarket,
  CryptoPrice,
  ExitCondition,
  ExitConfig,
} from './cryptoTypes';
import { DEFAULT_CONFIG } from './cryptoConfig';
import { CryptoTrader, getCryptoTrader } from './cryptoTrader';
import * as cryptoRepo from '../database/cryptoRepo';

export class ExitMonitor {
  private trader: CryptoTrader;
  private config: ExitConfig;
  private isRunning: boolean = false;
  private checkInterval: NodeJS.Timeout | null = null;

  // Cache of market data for reversal detection
  private marketCache: Map<string, CryptoMarket> = new Map();

  // Current prices for each asset
  private prices: Map<string, number> = new Map();

  // Price getter function (injected)
  private getPolymarketPrice:
    | ((marketId: string) => Promise<{ yesPrice: number; noPrice: number } | null>)
    | null = null;

  constructor(trader?: CryptoTrader, config?: Partial<ExitConfig>) {
    this.trader = trader || getCryptoTrader();
    this.config = {
      profitTargetPct: config?.profitTargetPct ?? DEFAULT_CONFIG.profitTargetPct,
      stopLossPct: config?.stopLossPct ?? DEFAULT_CONFIG.stopLossPct,
      maxHoldTimeSeconds: config?.maxHoldTimeSeconds ?? DEFAULT_CONFIG.maxHoldTimeSeconds,
    };
  }

  /**
   * Set the function to get current Polymarket prices.
   */
  setPolymarketPriceGetter(
    getter: (marketId: string) => Promise<{ yesPrice: number; noPrice: number } | null>
  ): void {
    this.getPolymarketPrice = getter;
  }

  /**
   * Update the current price for an asset.
   */
  updatePrice(asset: string, price: number): void {
    this.prices.set(asset, price);
  }

  /**
   * Update market cache for reversal detection.
   */
  async refreshMarketCache(): Promise<void> {
    const markets = await cryptoRepo.getActiveCryptoMarkets();
    this.marketCache.clear();
    for (const market of markets) {
      this.marketCache.set(market.marketId, market);
    }
  }

  /**
   * Check if a position should be exited.
   */
  async checkExitCondition(position: CryptoPosition): Promise<ExitCondition> {
    // Get current Polymarket price
    let currentPrice = position.entryPrice; // Fallback
    if (this.getPolymarketPrice) {
      const prices = await this.getPolymarketPrice(position.marketId);
      if (prices) {
        currentPrice = position.side === 'YES' ? prices.yesPrice : prices.noPrice;
      }
    }

    // Calculate P&L percentage
    const pnlPct = (currentPrice - position.entryPrice) / position.entryPrice;

    // 1. Check PROFIT target
    if (pnlPct >= this.config.profitTargetPct) {
      return {
        shouldExit: true,
        reason: 'PROFIT',
        currentPrice,
        pnlPercent: pnlPct,
      };
    }

    // 2. Check STOP loss
    if (pnlPct <= -this.config.stopLossPct) {
      return {
        shouldExit: true,
        reason: 'STOP',
        currentPrice,
        pnlPercent: pnlPct,
      };
    }

    // 3. Check TIME limit
    const holdTimeSeconds =
      (Date.now() - position.entryTime.getTime()) / 1000;
    if (holdTimeSeconds >= this.config.maxHoldTimeSeconds) {
      return {
        shouldExit: true,
        reason: 'TIME',
        currentPrice,
        pnlPercent: pnlPct,
      };
    }

    // 4. Check REVERSAL (price crosses back threshold)
    const market = this.marketCache.get(position.marketId);
    if (market && position.binancePriceAtEntry) {
      const currentCryptoPrice = this.prices.get(position.asset);
      if (currentCryptoPrice) {
        const entryAboveThreshold = position.binancePriceAtEntry > market.threshold;
        const currentAboveThreshold = currentCryptoPrice > market.threshold;

        // If we entered expecting price to stay above/below and it reversed
        if (entryAboveThreshold !== currentAboveThreshold) {
          return {
            shouldExit: true,
            reason: 'REVERSAL',
            currentPrice,
            pnlPercent: pnlPct,
          };
        }
      }
    }

    return {
      shouldExit: false,
      currentPrice,
      pnlPercent: pnlPct,
    };
  }

  /**
   * Check all open positions and close any that meet exit conditions.
   */
  async checkAllPositions(): Promise<{
    checked: number;
    closed: number;
    closedPositions: Array<{ position: CryptoPosition; reason: string; pnl: number }>;
  }> {
    const positions = await this.trader.getOpenPositions();
    const closedPositions: Array<{
      position: CryptoPosition;
      reason: string;
      pnl: number;
    }> = [];

    for (const position of positions) {
      const exitCondition = await this.checkExitCondition(position);

      if (exitCondition.shouldExit && exitCondition.reason) {
        const result = await this.trader.closePosition(
          position,
          exitCondition.currentPrice || position.entryPrice,
          exitCondition.reason
        );

        if (result.success) {
          closedPositions.push({
            position,
            reason: exitCondition.reason,
            pnl: result.pnl || 0,
          });
        }
      }
    }

    return {
      checked: positions.length,
      closed: closedPositions.length,
      closedPositions,
    };
  }

  /**
   * Start the exit monitor (checks every second).
   */
  start(intervalMs: number = 1000): void {
    if (this.isRunning) return;

    this.isRunning = true;
    console.log('[EXIT-MONITOR] Started');

    // Initial market cache refresh
    this.refreshMarketCache();

    this.checkInterval = setInterval(async () => {
      try {
        const result = await this.checkAllPositions();
        if (result.closed > 0) {
          for (const closed of result.closedPositions) {
            console.log(
              `[EXIT-MONITOR] Closed ${closed.position.side} ${closed.position.asset} ` +
                `| Reason: ${closed.reason} | P&L: $${closed.pnl.toFixed(2)}`
            );
          }
        }
      } catch (error) {
        console.error('[EXIT-MONITOR] Error checking positions:', error);
      }
    }, intervalMs);

    // Refresh market cache every 5 minutes
    setInterval(() => {
      this.refreshMarketCache();
    }, 5 * 60 * 1000);
  }

  /**
   * Stop the exit monitor.
   */
  stop(): void {
    if (this.checkInterval) {
      clearInterval(this.checkInterval);
      this.checkInterval = null;
    }
    this.isRunning = false;
    console.log('[EXIT-MONITOR] Stopped');
  }

  /**
   * Check if monitor is running.
   */
  isMonitorRunning(): boolean {
    return this.isRunning;
  }

  /**
   * Get current exit config.
   */
  getConfig(): ExitConfig {
    return { ...this.config };
  }

  /**
   * Update exit config.
   */
  updateConfig(config: Partial<ExitConfig>): void {
    this.config = { ...this.config, ...config };
    console.log('[EXIT-MONITOR] Config updated:', this.config);
  }

  /**
   * Force check a specific position.
   */
  async forceCheckPosition(
    positionId: string
  ): Promise<ExitCondition | null> {
    const position = await this.trader.getPosition(positionId);
    if (!position) return null;

    return this.checkExitCondition(position);
  }

  /**
   * Get position status summary.
   */
  async getPositionSummary(): Promise<
    Array<{
      position: CryptoPosition;
      holdTime: number;
      pnlPercent: number;
      nearProfit: boolean;
      nearStop: boolean;
      nearTimeout: boolean;
    }>
  > {
    const positions = await this.trader.getOpenPositions();
    const summary = [];

    for (const position of positions) {
      const exitCondition = await this.checkExitCondition(position);
      const holdTime =
        (Date.now() - position.entryTime.getTime()) / 1000;

      summary.push({
        position,
        holdTime,
        pnlPercent: exitCondition.pnlPercent || 0,
        nearProfit:
          (exitCondition.pnlPercent || 0) >= this.config.profitTargetPct * 0.8,
        nearStop:
          (exitCondition.pnlPercent || 0) <= -this.config.stopLossPct * 0.8,
        nearTimeout: holdTime >= this.config.maxHoldTimeSeconds * 0.8,
      });
    }

    return summary;
  }
}

// ============================================================================
// Singleton Instance
// ============================================================================

let instance: ExitMonitor | null = null;

export function getExitMonitor(): ExitMonitor {
  if (!instance) {
    instance = new ExitMonitor();
  }
  return instance;
}

export function resetExitMonitor(): void {
  if (instance) {
    instance.stop();
  }
  instance = null;
}

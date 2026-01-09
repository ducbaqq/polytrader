/**
 * Crypto Trader - Order Execution and Position Management
 *
 * Handles:
 * - Position sizing based on opportunity confidence
 * - Order placement (paper trading mode)
 * - Position tracking
 * - Trade recording
 */

import { v4 as uuidv4 } from 'uuid';
import {
  CryptoOpportunity,
  CryptoPosition,
  OrderRequest,
  OrderResult,
} from './cryptoTypes';
import { DEFAULT_CONFIG, SIZING_CONFIG } from './cryptoConfig';
import { RiskManager, getRiskManager } from './riskManager';
import * as cryptoRepo from '../database/cryptoRepo';
import { withTransaction } from '../database/index';

export class CryptoTrader {
  private riskManager: RiskManager;
  private isPaperTrading: boolean = true;

  constructor(riskManager?: RiskManager) {
    this.riskManager = riskManager || getRiskManager();
  }

  /**
   * Calculate position size based on opportunity confidence.
   *
   * Factors:
   * - Base size from config
   * - Gap percentage (larger gap = more confidence)
   * - Market volume (higher volume = more confidence)
   */
  calculatePositionSize(opportunity: CryptoOpportunity, marketVolume: number): number {
    let size = DEFAULT_CONFIG.basePositionSize;

    // Scale by gap percentage
    for (const tier of SIZING_CONFIG.gapTiers) {
      if (opportunity.gapPercent >= tier.minGap) {
        size *= tier.multiplier;
        break;
      }
    }

    // Scale by volume
    for (const tier of SIZING_CONFIG.volumeTiers) {
      if (marketVolume >= tier.minVolume) {
        size *= tier.multiplier;
        break;
      }
    }

    // Cap at max position size
    return Math.min(size, DEFAULT_CONFIG.maxPositionSize);
  }

  /**
   * Execute a trade for an opportunity.
   */
  async executeTrade(
    opportunity: CryptoOpportunity,
    marketVolume: number = 50000
  ): Promise<{ success: boolean; position?: CryptoPosition; error?: string }> {
    // Calculate position size
    const size = this.calculatePositionSize(opportunity, marketVolume);

    // Check risk limits
    const riskCheck = await this.riskManager.canTrade(opportunity, size);
    if (!riskCheck.allowed) {
      // Update opportunity status
      await cryptoRepo.updateCryptoOpportunityStatus(
        opportunity.opportunityId,
        'SKIPPED',
        false
      );

      return {
        success: false,
        error: riskCheck.reason,
      };
    }

    try {
      // Create position
      const position: CryptoPosition = {
        positionId: uuidv4(),
        marketId: opportunity.marketId,
        asset: opportunity.asset,
        side: opportunity.side,
        entryPrice: opportunity.actualPolyPrice,
        quantity: size / opportunity.actualPolyPrice, // Convert $ to tokens
        entryTime: new Date(),
        binancePriceAtEntry: opportunity.binancePrice,
        status: 'OPEN',
      };

      // In paper trading mode, we simulate the fill
      if (this.isPaperTrading) {
        await withTransaction(async (client) => {
          // Insert position
          await cryptoRepo.insertCryptoPosition(client, position);

          // Update opportunity as executed
          await cryptoRepo.updateCryptoOpportunityStatus(
            opportunity.opportunityId,
            'EXECUTED',
            true
          );
        });

        // Start cooldown for this market
        this.riskManager.startCooldown(opportunity.marketId);

        console.log(
          `[CRYPTO-TRADER] Position opened: ${position.side} ${opportunity.asset} ` +
            `@ $${position.entryPrice.toFixed(4)} | Size: $${size.toFixed(2)} ` +
            `| Qty: ${position.quantity.toFixed(2)}`
        );

        return { success: true, position };
      } else {
        // TODO: Real trading implementation
        return {
          success: false,
          error: 'Real trading not implemented',
        };
      }
    } catch (error: any) {
      console.error('[CRYPTO-TRADER] Trade execution failed:', error);

      await cryptoRepo.updateCryptoOpportunityStatus(
        opportunity.opportunityId,
        'FAILED',
        false
      );

      return {
        success: false,
        error: error.message,
      };
    }
  }

  /**
   * Close a position.
   */
  async closePosition(
    position: CryptoPosition,
    currentPrice: number,
    reason: 'PROFIT' | 'STOP' | 'TIME' | 'REVERSAL'
  ): Promise<{ success: boolean; pnl?: number; error?: string }> {
    try {
      // Calculate P&L
      // For BUY positions: profit when price goes up
      const value = position.quantity * currentPrice;
      const cost = position.quantity * position.entryPrice;
      const pnl = value - cost;

      await withTransaction(async (client) => {
        await cryptoRepo.closeCryptoPosition(
          client,
          position.positionId,
          currentPrice,
          reason,
          pnl
        );
      });

      // Clear cooldown for this market
      this.riskManager.clearCooldown(position.marketId);

      const pnlPct = ((currentPrice - position.entryPrice) / position.entryPrice) * 100;

      console.log(
        `[CRYPTO-TRADER] Position closed: ${position.side} ${position.asset} ` +
          `| Entry: $${position.entryPrice.toFixed(4)} → Exit: $${currentPrice.toFixed(4)} ` +
          `| P&L: $${pnl.toFixed(2)} (${pnl >= 0 ? '+' : ''}${pnlPct.toFixed(1)}%) ` +
          `| Reason: ${reason}`
      );

      return { success: true, pnl };
    } catch (error: any) {
      console.error('[CRYPTO-TRADER] Failed to close position:', error);
      return {
        success: false,
        error: error.message,
      };
    }
  }

  /**
   * Get all open positions.
   */
  async getOpenPositions(): Promise<CryptoPosition[]> {
    return cryptoRepo.getOpenCryptoPositions();
  }

  /**
   * Get position by ID.
   */
  async getPosition(positionId: string): Promise<CryptoPosition | null> {
    return cryptoRepo.getCryptoPosition(positionId);
  }

  /**
   * Enable/disable paper trading mode.
   */
  setPaperTrading(enabled: boolean): void {
    this.isPaperTrading = enabled;
    console.log(`[CRYPTO-TRADER] Paper trading: ${enabled ? 'ON' : 'OFF'}`);
  }

  /**
   * Check if in paper trading mode.
   */
  isInPaperTradingMode(): boolean {
    return this.isPaperTrading;
  }

  /**
   * Get trading statistics.
   */
  async getStats(): Promise<{
    openPositions: number;
    totalExposure: number;
    todayTrades: number;
    todayPnl: number;
    allTimePnl: number;
    winRate: number;
  }> {
    const posStats = await cryptoRepo.getCryptoPositionStats();
    const allTime = await cryptoRepo.getCryptoAllTimeStats();

    return {
      openPositions: posStats.openPositions,
      totalExposure: posStats.totalExposure,
      todayTrades: posStats.todayTrades,
      todayPnl: posStats.todayPnl,
      allTimePnl: allTime.totalPnl,
      winRate: allTime.winRate,
    };
  }
}

// ============================================================================
// Singleton Instance
// ============================================================================

let instance: CryptoTrader | null = null;

export function getCryptoTrader(): CryptoTrader {
  if (!instance) {
    instance = new CryptoTrader();
  }
  return instance;
}

export function resetCryptoTrader(): void {
  instance = null;
}

/**
 * Risk Management for Crypto Reactive Trading
 *
 * Controls:
 * - Maximum total exposure
 * - Maximum simultaneous positions
 * - Daily loss limits
 * - Daily trade limits
 * - Per-market cooldowns
 */

import {
  CryptoOpportunity,
  CryptoPosition,
  RiskCheck,
  RiskState,
  RiskLimits,
} from './cryptoTypes';
import { DEFAULT_CONFIG } from './cryptoConfig';
import * as cryptoRepo from '../database/cryptoRepo';

export class RiskManager {
  private limits: RiskLimits;
  private cooldowns: Map<string, Date> = new Map();

  // In-memory state tracking (refreshed from DB periodically)
  private state: RiskState = {
    totalExposure: 0,
    positionCount: 0,
    dailyPnl: 0,
    dailyTrades: 0,
    cooldowns: new Map(),
  };

  constructor(limits?: Partial<RiskLimits>) {
    this.limits = {
      maxTotalExposure: limits?.maxTotalExposure ?? DEFAULT_CONFIG.maxTotalExposure,
      maxSimultaneousPositions:
        limits?.maxSimultaneousPositions ?? DEFAULT_CONFIG.maxSimultaneousPositions,
      dailyLossLimit: limits?.dailyLossLimit ?? DEFAULT_CONFIG.dailyLossLimit,
      maxDailyTrades: limits?.maxDailyTrades ?? DEFAULT_CONFIG.maxDailyTrades,
      cooldownMinutes: limits?.cooldownMinutes ?? DEFAULT_CONFIG.cooldownMinutes,
    };
  }

  /**
   * Refresh risk state from database.
   */
  async refreshState(): Promise<void> {
    const stats = await cryptoRepo.getCryptoPositionStats();

    this.state.totalExposure = stats.totalExposure;
    this.state.positionCount = stats.openPositions;
    this.state.dailyPnl = stats.todayPnl;
    this.state.dailyTrades = stats.todayTrades;
    this.state.cooldowns = this.cooldowns;
  }

  /**
   * Check if a trade is allowed given current risk state.
   */
  async canTrade(
    opportunity: CryptoOpportunity,
    positionSize: number
  ): Promise<RiskCheck> {
    await this.refreshState();

    // Check daily loss limit
    if (this.state.dailyPnl <= -this.limits.dailyLossLimit) {
      return {
        allowed: false,
        reason: `Daily loss limit reached: $${Math.abs(this.state.dailyPnl).toFixed(2)} / $${this.limits.dailyLossLimit}`,
      };
    }

    // Check daily trade limit
    if (this.state.dailyTrades >= this.limits.maxDailyTrades) {
      return {
        allowed: false,
        reason: `Daily trade limit reached: ${this.state.dailyTrades} / ${this.limits.maxDailyTrades}`,
      };
    }

    // Check max positions
    if (this.state.positionCount >= this.limits.maxSimultaneousPositions) {
      return {
        allowed: false,
        reason: `Max positions reached: ${this.state.positionCount} / ${this.limits.maxSimultaneousPositions}`,
      };
    }

    // Check total exposure
    const newExposure = this.state.totalExposure + positionSize;
    if (newExposure > this.limits.maxTotalExposure) {
      return {
        allowed: false,
        reason: `Max exposure exceeded: $${newExposure.toFixed(2)} > $${this.limits.maxTotalExposure}`,
      };
    }

    // Check market cooldown
    const cooldownExpiry = this.cooldowns.get(opportunity.marketId);
    if (cooldownExpiry && cooldownExpiry > new Date()) {
      const remainingMs = cooldownExpiry.getTime() - Date.now();
      const remainingMin = Math.ceil(remainingMs / 60000);
      return {
        allowed: false,
        reason: `Market on cooldown: ${remainingMin} min remaining`,
      };
    }

    return { allowed: true };
  }

  /**
   * Record that a trade was executed for a market (start cooldown).
   */
  startCooldown(marketId: string): void {
    const expiry = new Date(Date.now() + this.limits.cooldownMinutes * 60 * 1000);
    this.cooldowns.set(marketId, expiry);
    this.state.dailyTrades++;
  }

  /**
   * Clear cooldown for a market (e.g., after position closed).
   */
  clearCooldown(marketId: string): void {
    this.cooldowns.delete(marketId);
  }

  /**
   * Get current risk state summary.
   */
  getState(): RiskState {
    return { ...this.state, cooldowns: new Map(this.cooldowns) };
  }

  /**
   * Get risk limits.
   */
  getLimits(): RiskLimits {
    return { ...this.limits };
  }

  /**
   * Check if we're close to limits (for dashboard warnings).
   */
  getWarnings(): string[] {
    const warnings: string[] = [];

    const exposurePct = this.state.totalExposure / this.limits.maxTotalExposure;
    if (exposurePct > 0.8) {
      warnings.push(
        `High exposure: ${(exposurePct * 100).toFixed(0)}% of limit`
      );
    }

    const lossPct = Math.abs(this.state.dailyPnl) / this.limits.dailyLossLimit;
    if (this.state.dailyPnl < 0 && lossPct > 0.5) {
      warnings.push(`Daily loss: ${(lossPct * 100).toFixed(0)}% of limit`);
    }

    const tradesPct = this.state.dailyTrades / this.limits.maxDailyTrades;
    if (tradesPct > 0.8) {
      warnings.push(
        `Trade limit: ${(tradesPct * 100).toFixed(0)}% used`
      );
    }

    return warnings;
  }

  /**
   * Check if trading should be paused entirely.
   */
  shouldPauseTrading(): { pause: boolean; reason?: string } {
    if (this.state.dailyPnl <= -this.limits.dailyLossLimit) {
      return { pause: true, reason: 'Daily loss limit reached' };
    }

    if (this.state.dailyTrades >= this.limits.maxDailyTrades) {
      return { pause: true, reason: 'Daily trade limit reached' };
    }

    return { pause: false };
  }

  /**
   * Calculate how much additional exposure is available.
   */
  getAvailableExposure(): number {
    return Math.max(0, this.limits.maxTotalExposure - this.state.totalExposure);
  }

  /**
   * Reset daily counters (called at start of day).
   */
  resetDaily(): void {
    // Note: dailyPnl is calculated from DB, not stored here
    this.state.dailyTrades = 0;
    console.log('[RISK] Daily counters reset');
  }

  /**
   * Get number of active cooldowns.
   */
  getActiveCooldownCount(): number {
    const now = new Date();
    let count = 0;
    for (const expiry of this.cooldowns.values()) {
      if (expiry > now) count++;
    }
    return count;
  }
}

// ============================================================================
// Singleton Instance
// ============================================================================

let instance: RiskManager | null = null;

export function getRiskManager(): RiskManager {
  if (!instance) {
    instance = new RiskManager();
  }
  return instance;
}

export function resetRiskManager(): void {
  instance = null;
}

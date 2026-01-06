/**
 * Paper trading engine - orchestrates simulated trading.
 */

import {
  getActivePaperMarkets,
  getPositions,
  getLatestPnL,
  getTotalTradeStats,
  recordPnLSnapshot,
  DBPaperMarket,
} from '../database/paperTradingRepo';
import {
  placeMarketMakingOrders,
  checkFills,
  placeArbitrageOrders,
  handlePartialArbitrageFills,
} from './orderManager';
import { PortfolioSummary, PaperPosition, TokenSide } from '../types';

export interface PaperTraderConfig {
  orderSize: number;           // Size per order
  tickImprovement: number;     // How much to improve on best bid/ask
  maxOrdersPerMarket: number;  // Max concurrent orders per market
  tradingEnabled: boolean;     // Master switch
}

const DEFAULT_CONFIG: PaperTraderConfig = {
  orderSize: 100,  // 100 contracts per order for meaningful trade values
  tickImprovement: 0.01,
  maxOrdersPerMarket: 2,
  tradingEnabled: true,
};

export class PaperTrader {
  private config: PaperTraderConfig;
  private cashBalance: number;
  private initialCapital: number;

  constructor(initialCapital: number, config: Partial<PaperTraderConfig> = {}) {
    this.config = { ...DEFAULT_CONFIG, ...config };
    this.initialCapital = initialCapital;
    this.cashBalance = initialCapital;
  }

  /**
   * Run one cycle of the paper trading engine.
   * Should be called every ~60 seconds.
   */
  async runCycle(): Promise<{
    ordersPlaced: number;
    ordersFilled: number;
    markets: string[];
    arbOrdersPlaced: number;
  }> {
    const cycleStart = Date.now();
    console.log(`[CYCLE] Starting at ${new Date().toISOString()}`);

    if (!this.config.tradingEnabled) {
      console.log(`[CYCLE] Trading disabled, skipping`);
      return { ordersPlaced: 0, ordersFilled: 0, markets: [], arbOrdersPlaced: 0 };
    }

    let ordersPlaced = 0;
    let arbOrdersPlaced = 0;
    const markets: string[] = [];
    const arbMarketIds: string[] = [];

    // Phase 1: Get active paper trading markets
    const marketsStart = Date.now();
    const activeMarkets = await getActivePaperMarkets();
    console.log(`[CYCLE] Fetched ${activeMarkets.length} markets in ${Date.now() - marketsStart}ms`);

    // Separate arbitrage markets from regular markets
    const arbMarkets = activeMarkets.filter(m => m.selection_reason === 'ARBITRAGE');
    const regularMarkets = activeMarkets.filter(m => m.selection_reason !== 'ARBITRAGE');

    // Phase 2a: Handle ARBITRAGE markets (prioritized)
    if (arbMarkets.length > 0) {
      const arbStart = Date.now();
      console.log(`[CYCLE] Processing ${arbMarkets.length} arbitrage markets`);

      for (const market of arbMarkets) {
        try {
          const result = await placeArbitrageOrders(market.market_id, this.config.orderSize);

          if (result.yesOrderId) arbOrdersPlaced++;
          if (result.noOrderId) arbOrdersPlaced++;

          arbMarketIds.push(market.market_id);
          markets.push(market.market_id);
        } catch (error) {
          console.error(`[CYCLE] Error placing arbitrage orders for ${market.market_id}:`, error);
        }
      }

      console.log(`[CYCLE] Arbitrage phase: ${arbOrdersPlaced} orders in ${Date.now() - arbStart}ms`);
    }

    // Phase 2b: Handle regular market-making markets
    const mmStart = Date.now();
    for (const market of regularMarkets) {
      try {
        const marketStart = Date.now();

        // Place YES side orders
        const yesOrders = await placeMarketMakingOrders(
          market.market_id,
          'YES',
          this.config.orderSize,
          this.config.tickImprovement
        );

        if (yesOrders.buyOrderId) ordersPlaced++;
        if (yesOrders.sellOrderId) ordersPlaced++;

        // Place NO side orders
        const noOrders = await placeMarketMakingOrders(
          market.market_id,
          'NO',
          this.config.orderSize,
          this.config.tickImprovement
        );

        if (noOrders.buyOrderId) ordersPlaced++;
        if (noOrders.sellOrderId) ordersPlaced++;

        markets.push(market.market_id);
        console.log(`[CYCLE] Market ${market.market_id}: ${(yesOrders.buyOrderId ? 1 : 0) + (yesOrders.sellOrderId ? 1 : 0) + (noOrders.buyOrderId ? 1 : 0) + (noOrders.sellOrderId ? 1 : 0)} orders in ${Date.now() - marketStart}ms`);
      } catch (error) {
        console.error(`[CYCLE] Error placing orders for ${market.market_id}:`, error);
      }
    }
    console.log(`[CYCLE] Market-making phase: ${ordersPlaced} orders in ${Date.now() - mmStart}ms`);

    // Phase 3: Check for fills on pending orders
    const fillsStart = Date.now();
    const ordersFilled = await checkFills();
    console.log(`[CYCLE] Fill check: ${ordersFilled} fills in ${Date.now() - fillsStart}ms`);

    // Phase 4: Handle partial arbitrage fills (hedge imbalances)
    if (arbMarketIds.length > 0) {
      const hedgeStart = Date.now();
      const hedgeResults = await handlePartialArbitrageFills(arbMarketIds);
      if (hedgeResults.length > 0) {
        console.log(`[CYCLE] Arbitrage hedging: ${hedgeResults.length} markets checked in ${Date.now() - hedgeStart}ms`);
      }
    }

    console.log(`[CYCLE] Complete in ${Date.now() - cycleStart}ms | MM: ${ordersPlaced} | ARB: ${arbOrdersPlaced} | Filled: ${ordersFilled}`);

    return { ordersPlaced, ordersFilled, markets, arbOrdersPlaced };
  }

  /**
   * Get current portfolio summary.
   */
  async getPortfolioSummary(): Promise<PortfolioSummary> {
    const positions = await getPositions();
    const tradeStats = await getTotalTradeStats();

    // Parse as numbers (PostgreSQL returns numeric as strings)
    const positionValue = positions.reduce((sum, p) => sum + parseFloat(String(p.market_value || 0)), 0);
    const unrealizedPnl = positions.reduce((sum, p) => sum + parseFloat(String(p.unrealized_pnl || 0)), 0);

    // Cash balance = initial capital + cash flow from trades
    // net_value is negative for buys (cash out), positive for sells (cash in)
    const totalCashFlow = isNaN(tradeStats.total_cash_flow) ? 0 : tradeStats.total_cash_flow;
    this.cashBalance = this.initialCapital + totalCashFlow;

    const paperPositions: PaperPosition[] = positions.map((p) => ({
      marketId: p.market_id,
      tokenSide: p.token_side as TokenSide,
      quantity: p.quantity,
      averageCost: p.average_cost,
      costBasis: p.cost_basis,
      currentPrice: p.current_price,
      marketValue: p.market_value,
      unrealizedPnl: p.unrealized_pnl,
      unrealizedPnlPct: p.unrealized_pnl_pct,
    }));

    const totalEquity = this.cashBalance + positionValue;
    // Total P&L = current equity - initial capital (includes all fees and costs)
    const totalPnl = totalEquity - this.initialCapital;

    return {
      cashBalance: this.cashBalance,
      positionValue,
      totalEquity,
      realizedPnl: 0,  // Not tracking realized separately
      unrealizedPnl,
      totalPnl,
      positions: paperPositions,
    };
  }

  /**
   * Record a P&L snapshot.
   */
  async recordPnLSnapshot(): Promise<void> {
    await recordPnLSnapshot(this.cashBalance, this.initialCapital);
  }

  /**
   * Enable/disable trading.
   */
  setTradingEnabled(enabled: boolean): void {
    this.config.tradingEnabled = enabled;
    console.log(`Paper trading ${enabled ? 'enabled' : 'disabled'}`);
  }

  /**
   * Get trading status.
   */
  async getStatus(): Promise<{
    tradingEnabled: boolean;
    activeMarkets: number;
    openPositions: number;
    cashBalance: number;
    totalPnl: number;
  }> {
    const markets = await getActivePaperMarkets();
    const positions = await getPositions();
    const summary = await this.getPortfolioSummary();

    return {
      tradingEnabled: this.config.tradingEnabled,
      activeMarkets: markets.length,
      openPositions: positions.filter((p) => p.quantity !== 0).length,
      cashBalance: summary.cashBalance,
      totalPnl: summary.totalPnl,
    };
  }
}

export * from './orderManager';
export * from './costCalculator';

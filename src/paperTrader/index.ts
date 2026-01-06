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
import { placeMarketMakingOrders, checkFills } from './orderManager';
import { PortfolioSummary, PaperPosition, TokenSide } from '../types';

export interface PaperTraderConfig {
  orderSize: number;           // Size per order
  tickImprovement: number;     // How much to improve on best bid/ask
  maxOrdersPerMarket: number;  // Max concurrent orders per market
  tradingEnabled: boolean;     // Master switch
}

const DEFAULT_CONFIG: PaperTraderConfig = {
  orderSize: 30,
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
  }> {
    if (!this.config.tradingEnabled) {
      return { ordersPlaced: 0, ordersFilled: 0, markets: [] };
    }

    let ordersPlaced = 0;
    const markets: string[] = [];

    // Get active paper trading markets
    const activeMarkets = await getActivePaperMarkets();

    // Place orders for each market
    for (const market of activeMarkets) {
      try {
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
      } catch (error) {
        console.error(`Error placing orders for ${market.market_id}:`, error);
      }
    }

    // Check for fills on pending orders
    const ordersFilled = await checkFills();

    return { ordersPlaced, ordersFilled, markets };
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

    // Realized P&L is 0 until positions are closed (we only track unrealized for now)
    const realizedPnl = 0;

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

    return {
      cashBalance: this.cashBalance,
      positionValue,
      totalEquity: this.cashBalance + positionValue,
      realizedPnl,
      unrealizedPnl,
      totalPnl: realizedPnl + unrealizedPnl,
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

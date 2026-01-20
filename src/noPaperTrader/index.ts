/**
 * No-betting Paper Trading System
 *
 * Entry point that orchestrates:
 * - Market scanning for eligible Entertainment/Weather markets
 * - Position monitoring for TP/SL and resolution
 * - Portfolio state management
 */

import { PolymarketClient } from '../apiClient';
import { initDatabase } from '../database/index';
import { StrategyConfig, loadConfig } from './config';
import { MarketScanner } from './scanner';
import { PositionMonitor } from './monitor';
import {
  initializeTables,
  initializePortfolio,
  getPortfolio,
  getOpenPositions,
  recordDailySnapshot,
} from './repository';
import { DashboardState, PositionWithPrice, renderDashboard } from './dashboard';

export interface PaperTraderStats {
  isRunning: boolean;
  startTime: Date | null;
  totalScans: number;
  totalMonitorCycles: number;
  positionsOpened: number;
  positionsClosed: number;
  lastScanTime: Date | null;
  lastMonitorTime: Date | null;
}

/**
 * Main paper trading orchestrator.
 */
export class NoPaperTrader {
  private config: StrategyConfig;
  private client: PolymarketClient;
  private scanner: MarketScanner;
  private monitor: PositionMonitor;
  private stats: PaperTraderStats;
  private running: boolean = false;
  private scanIntervalId: NodeJS.Timeout | null = null;
  private monitorIntervalId: NodeJS.Timeout | null = null;
  private snapshotIntervalId: NodeJS.Timeout | null = null;
  private dashboardIntervalId: NodeJS.Timeout | null = null;
  private currentDate: string = '';
  private dashboardState: DashboardState;
  private useDashboard: boolean = true;

  constructor(config?: StrategyConfig, useDashboard: boolean = true) {
    this.config = config || loadConfig();
    this.client = new PolymarketClient();
    this.scanner = new MarketScanner(this.client, this.config, this.config.scanConcurrency);
    this.monitor = new PositionMonitor(this.client, this.config);
    this.useDashboard = useDashboard;
    this.stats = {
      isRunning: false,
      startTime: null,
      totalScans: 0,
      totalMonitorCycles: 0,
      positionsOpened: 0,
      positionsClosed: 0,
      lastScanTime: null,
      lastMonitorTime: null,
    };
    this.dashboardState = {
      status: 'idle',
      portfolio: null,
      positions: [],
      lastUpdate: new Date(),
      runtime: 0,
      totalScans: 0,
      positionsOpened: 0,
      positionsClosed: 0,
    };
  }

  /**
   * Initialize the paper trader.
   */
  async initialize(): Promise<void> {
    if (!this.useDashboard) {
      console.log('Initializing No Paper Trader...');
      console.log(`  Categories: ${this.config.categories.join(', ')}`);
      console.log(`  Position Size: $${this.config.positionSize}`);
      console.log(`  Min Edge: ${(this.config.minEdge * 100).toFixed(1)}%`);
      console.log(`  Take Profit: ${(this.config.takeProfitThreshold * 100).toFixed(0)}%`);
      console.log(`  Stop Loss: ${(this.config.stopLossThreshold * 100).toFixed(0)}%`);
      console.log(`  Scan Interval: ${this.config.scanIntervalSeconds}s`);
      console.log(`  Monitor Interval: ${this.config.monitorIntervalSeconds}s`);
    }

    // Initialize database
    initDatabase();
    await initializeTables();
    await initializePortfolio(this.config.initialCapital);

    const portfolio = await getPortfolio();
    if (portfolio) {
      this.dashboardState.portfolio = portfolio;
      if (!this.useDashboard) {
        console.log(`\nPortfolio initialized:`);
        console.log(`  Cash Balance: $${portfolio.cashBalance.toFixed(2)}`);
        console.log(`  Open Positions: ${portfolio.openPositionCount}`);
      }
    }
  }

  /**
   * Start the paper trader.
   */
  async start(): Promise<void> {
    if (this.running) {
      if (!this.useDashboard) console.log('Paper trader is already running');
      return;
    }

    this.dashboardState.status = 'starting';
    await this.initialize();

    this.running = true;
    this.stats.isRunning = true;
    this.stats.startTime = new Date();
    this.currentDate = this.getTodayDate();

    if (!this.useDashboard) {
      console.log('\n🚀 Starting No Paper Trader...');
      console.log('─'.repeat(60));
    }

    // Load initial positions
    await this.updatePositionsWithPrices();

    // Run initial scan
    await this.runScan();
    await this.runMonitor();

    // Set up intervals
    this.scanIntervalId = setInterval(
      () => this.runScan(),
      this.config.scanIntervalSeconds * 1000
    );

    this.monitorIntervalId = setInterval(
      () => this.runMonitor(),
      this.config.monitorIntervalSeconds * 1000
    );

    // Daily snapshot at midnight
    this.snapshotIntervalId = setInterval(
      () => this.checkDailySnapshot(),
      60000 // Check every minute
    );

    // Dashboard refresh every 5 seconds
    if (this.useDashboard) {
      this.dashboardIntervalId = setInterval(
        () => this.refreshDashboard(),
        5000
      );
      this.refreshDashboard();
    } else {
      console.log('Paper trader started. Press Ctrl+C to stop.');
    }
  }

  /**
   * Stop the paper trader.
   */
  async stop(): Promise<void> {
    if (!this.running) return;

    if (!this.useDashboard) console.log('\nStopping No Paper Trader...');

    this.running = false;
    this.stats.isRunning = false;

    // Clear all intervals
    const intervals = [this.scanIntervalId, this.monitorIntervalId, this.snapshotIntervalId, this.dashboardIntervalId];
    for (const interval of intervals) {
      if (interval) clearInterval(interval);
    }
    this.scanIntervalId = null;
    this.monitorIntervalId = null;
    this.snapshotIntervalId = null;
    this.dashboardIntervalId = null;

    await this.recordSnapshot();
    if (!this.useDashboard) console.log('Paper trader stopped.');
  }

  /**
   * Run a single scan cycle.
   */
  private async runScan(): Promise<void> {
    if (!this.running) return;

    try {
      this.dashboardState.status = 'scanning';
      if (this.useDashboard) this.refreshDashboard();

      if (!this.useDashboard) {
        console.log(`\n[${new Date().toISOString()}] Running market scan...`);
      }

      // Progress callback for dashboard
      const onProgress = this.useDashboard
        ? (current: number, total: number) => {
            this.dashboardState.scanProgress = { current, total };
            this.refreshDashboard();
          }
        : undefined;

      const result = await this.scanner.scan(onProgress, this.useDashboard);

      this.stats.totalScans++;
      this.stats.positionsOpened += result.positionsOpened;
      this.stats.lastScanTime = new Date();

      // Update dashboard state
      this.dashboardState.totalScans = this.stats.totalScans;
      this.dashboardState.positionsOpened = this.stats.positionsOpened;
      this.dashboardState.scanProgress = undefined;
      this.dashboardState.status = 'idle';

      // Refresh positions after scan
      await this.updatePositionsWithPrices();

      if (!this.useDashboard && (result.eligibleMarkets.length > 0 || result.positionsOpened > 0)) {
        console.log(`Scan result: ${result.eligibleMarkets.length} eligible, ${result.positionsOpened} opened`);
      }
    } catch (error) {
      if (!this.useDashboard) console.error('Error during scan:', error);
      this.dashboardState.status = 'idle';
    }
  }

  /**
   * Run a single monitor cycle.
   */
  private async runMonitor(): Promise<void> {
    if (!this.running) return;

    try {
      this.dashboardState.status = 'monitoring';

      const result = await this.monitor.monitor();

      this.stats.totalMonitorCycles++;
      this.stats.positionsClosed += result.takeProfitTriggered + result.stopLossTriggered + result.resolved;
      this.stats.lastMonitorTime = new Date();

      // Update dashboard state
      this.dashboardState.positionsClosed = this.stats.positionsClosed;
      this.dashboardState.status = 'idle';

      // Refresh positions after monitor
      await this.updatePositionsWithPrices();
    } catch (error) {
      if (!this.useDashboard) console.error('Error during monitor:', error);
      this.dashboardState.status = 'idle';
    }
  }

  /**
   * Check if we need to record a daily snapshot.
   */
  private async checkDailySnapshot(): Promise<void> {
    const today = this.getTodayDate();
    if (today !== this.currentDate) {
      // New day! Record snapshot for previous day
      await this.recordSnapshot();
      this.currentDate = today;
    }
  }

  /**
   * Record a daily snapshot.
   */
  private async recordSnapshot(): Promise<void> {
    try {
      const portfolio = await getPortfolio();
      if (portfolio) {
        await recordDailySnapshot(
          this.currentDate,
          portfolio.initialCapital, // Simplified - would need proper tracking
          portfolio.totalEquity,
          0, // Would need proper tracking
          0,
          0,
          0
        );
      }
    } catch (error) {
      console.error('Error recording snapshot:', error);
    }
  }

  /**
   * Get today's date string.
   */
  private getTodayDate(): string {
    return new Date().toISOString().split('T')[0];
  }

  /**
   * Get current stats.
   */
  getStats(): PaperTraderStats {
    return { ...this.stats };
  }

  /**
   * Get config.
   */
  getConfig(): StrategyConfig {
    return { ...this.config };
  }

  /**
   * Update positions with current market prices.
   */
  private async updatePositionsWithPrices(): Promise<void> {
    try {
      const positions = await getOpenPositions();
      const portfolio = await getPortfolio();

      this.dashboardState.portfolio = portfolio;

      // Fetch current prices for each position
      const positionsWithPrices: PositionWithPrice[] = [];

      for (const pos of positions) {
        const posWithPrice: PositionWithPrice = { ...pos };

        try {
          // Fetch current No price from order book
          // Note: bids are sorted ascending (best bid is LAST), asks descending (best ask is LAST)
          const orderBook = await this.client.getOrderBook(pos.tokenId);

          // Use best bid (what we could sell for) for current value
          if (orderBook && orderBook.bids && orderBook.bids.length > 0) {
            const bestBid = orderBook.bids[orderBook.bids.length - 1];
            posWithPrice.currentPrice = parseFloat(String(bestBid.price));
          } else if (orderBook && orderBook.asks && orderBook.asks.length > 0) {
            // Fallback to best ask if no bids
            const bestAsk = orderBook.asks[orderBook.asks.length - 1];
            posWithPrice.currentPrice = parseFloat(String(bestAsk.price));
          }

          // Calculate unrealized P&L
          if (posWithPrice.currentPrice !== undefined) {
            const currentValue = pos.quantity * posWithPrice.currentPrice;
            posWithPrice.unrealizedPnl = currentValue - pos.costBasis;
            posWithPrice.unrealizedPnlPercent = posWithPrice.unrealizedPnl / pos.costBasis;
          }
        } catch {
          // Ignore price fetch errors
        }

        positionsWithPrices.push(posWithPrice);
      }

      this.dashboardState.positions = positionsWithPrices;
    } catch {
      // Ignore errors
    }
  }

  /**
   * Refresh the dashboard display.
   */
  private refreshDashboard(): void {
    if (!this.useDashboard) return;

    // Update runtime
    if (this.stats.startTime) {
      this.dashboardState.runtime = Date.now() - this.stats.startTime.getTime();
    }
    this.dashboardState.lastUpdate = new Date();

    renderDashboard(this.dashboardState);
  }
}

// Re-export everything
export { StrategyConfig, loadConfig, DEFAULT_STRATEGY_CONFIG } from './config';
export { MarketScanner, ProgressCallback } from './scanner';
export { PositionMonitor } from './monitor';
export { generateReport, printReport, printStatus } from './report';
export { DashboardState, PositionWithPrice, renderDashboard, printProgress, clearProgress } from './dashboard';
export {
  initializeTables,
  initializePortfolio,
  getPortfolio,
  getOpenPositions,
  getClosedPositions,
  getAllPositions,
  getTrades,
  getDailySnapshots,
  getLifetimeExitStats,
  resetPaperTrading,
} from './repository';
export * from './types';

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
  recordDailySnapshot,
} from './repository';

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
  private currentDate: string = '';

  constructor(config?: StrategyConfig) {
    this.config = config || loadConfig();
    this.client = new PolymarketClient();
    this.scanner = new MarketScanner(this.client, this.config);
    this.monitor = new PositionMonitor(this.client, this.config);
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
  }

  /**
   * Initialize the paper trader.
   */
  async initialize(): Promise<void> {
    console.log('Initializing No Paper Trader...');
    console.log(`  Categories: ${this.config.categories.join(', ')}`);
    console.log(`  Position Size: $${this.config.positionSize}`);
    console.log(`  Min Edge: ${(this.config.minEdge * 100).toFixed(1)}%`);
    console.log(`  Take Profit: ${(this.config.takeProfitThreshold * 100).toFixed(0)}%`);
    console.log(`  Stop Loss: ${(this.config.stopLossThreshold * 100).toFixed(0)}%`);
    console.log(`  Scan Interval: ${this.config.scanIntervalSeconds}s`);
    console.log(`  Monitor Interval: ${this.config.monitorIntervalSeconds}s`);

    // Initialize database
    initDatabase();
    await initializeTables();
    await initializePortfolio(this.config.initialCapital);

    const portfolio = await getPortfolio();
    if (portfolio) {
      console.log(`\nPortfolio initialized:`);
      console.log(`  Cash Balance: $${portfolio.cashBalance.toFixed(2)}`);
      console.log(`  Open Positions: ${portfolio.openPositionCount}`);
    }
  }

  /**
   * Start the paper trader.
   */
  async start(): Promise<void> {
    if (this.running) {
      console.log('Paper trader is already running');
      return;
    }

    await this.initialize();

    this.running = true;
    this.stats.isRunning = true;
    this.stats.startTime = new Date();
    this.currentDate = this.getTodayDate();

    console.log('\n🚀 Starting No Paper Trader...');
    console.log('─'.repeat(60));

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

    console.log('Paper trader started. Press Ctrl+C to stop.');
  }

  /**
   * Stop the paper trader.
   */
  async stop(): Promise<void> {
    if (!this.running) return;

    console.log('\nStopping No Paper Trader...');

    this.running = false;
    this.stats.isRunning = false;

    // Clear all intervals
    const intervals = [this.scanIntervalId, this.monitorIntervalId, this.snapshotIntervalId];
    for (const interval of intervals) {
      if (interval) clearInterval(interval);
    }
    this.scanIntervalId = null;
    this.monitorIntervalId = null;
    this.snapshotIntervalId = null;

    await this.recordSnapshot();
    console.log('Paper trader stopped.');
  }

  /**
   * Run a single scan cycle.
   */
  private async runScan(): Promise<void> {
    if (!this.running) return;

    try {
      console.log(`\n[${new Date().toISOString()}] Running market scan...`);
      const result = await this.scanner.scan();

      this.stats.totalScans++;
      this.stats.positionsOpened += result.positionsOpened;
      this.stats.lastScanTime = new Date();

      if (result.eligibleMarkets.length > 0 || result.positionsOpened > 0) {
        console.log(`Scan result: ${result.eligibleMarkets.length} eligible, ${result.positionsOpened} opened`);
      }
    } catch (error) {
      console.error('Error during scan:', error);
    }
  }

  /**
   * Run a single monitor cycle.
   */
  private async runMonitor(): Promise<void> {
    if (!this.running) return;

    try {
      const result = await this.monitor.monitor();

      this.stats.totalMonitorCycles++;
      this.stats.positionsClosed += result.takeProfitTriggered + result.stopLossTriggered + result.resolved;
      this.stats.lastMonitorTime = new Date();
    } catch (error) {
      console.error('Error during monitor:', error);
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
}

// Re-export everything
export { StrategyConfig, loadConfig, DEFAULT_STRATEGY_CONFIG } from './config';
export { MarketScanner } from './scanner';
export { PositionMonitor } from './monitor';
export { generateReport, printReport, printStatus } from './report';
export {
  initializeTables,
  initializePortfolio,
  getPortfolio,
  getOpenPositions,
  getClosedPositions,
  getAllPositions,
  getTrades,
  getDailySnapshots,
  resetPaperTrading,
} from './repository';
export * from './types';

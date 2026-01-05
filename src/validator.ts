/**
 * Main validator orchestrator - coordinates scanning, paper trading, and analysis.
 * Runs 24/7 for market validation.
 */

import { v4 as uuidv4 } from 'uuid';
import {
  MarketData,
  MarketSnapshot,
  ValidatorConfig,
  DEFAULT_VALIDATOR_CONFIG,
  ValidatorStats,
  getYesPrice,
  getNoPrice,
} from './types';
import { PolymarketClient, createClientFromEnv } from './apiClient';
import { OpportunityDetector } from './detector';
import { MarketScanner } from './scanner';
import {
  initDatabase,
  closeDatabase,
  withTransaction,
  getPool,
} from './database/index';
import { verifySchema, getTableCounts } from './database/schema';
import {
  insertMarketSnapshots,
  getTopMarketsByVolume,
  getScanCount,
} from './database/marketRepo';
import { insertOrderBookSnapshots } from './database/orderBookRepo';
import {
  upsertOpportunities,
  getOpportunityStats,
  expireStaleOpportunities,
} from './database/opportunityRepo';
import {
  getActivePaperMarkets,
  insertPaperMarket,
  selectLiquidMarket,
  selectMediumVolumeMarket,
  selectNewMarket,
  recordPnLSnapshot,
  getLatestPnL,
  getTotalTradeStats,
} from './database/paperTradingRepo';
import { PaperTrader } from './paperTrader';

export class MarketValidator {
  private client: PolymarketClient;
  private detector: OpportunityDetector;
  private scanner: MarketScanner;
  private config: ValidatorConfig;

  private isRunning: boolean = false;
  private isStopping: boolean = false;
  private startTime: Date | null = null;
  private totalScans: number = 0;
  private lastScanTime: Date | null = null;
  private lastScanDuration: number = 0;
  private pendingOperations: number = 0;

  private fullScanIntervalId: NodeJS.Timeout | null = null;
  private priorityScanIntervalId: NodeJS.Timeout | null = null;
  private analysisIntervalId: NodeJS.Timeout | null = null;
  private pnlRecordIntervalId: NodeJS.Timeout | null = null;
  private paperTradingIntervalId: NodeJS.Timeout | null = null;
  private dashboardIntervalId: NodeJS.Timeout | null = null;

  private priorityMarketIds: Set<string> = new Set();
  private cashBalance: number;
  private paperTrader: PaperTrader | null = null;

  constructor(config: Partial<ValidatorConfig> = {}) {
    this.config = { ...DEFAULT_VALIDATOR_CONFIG, ...config };
    this.client = createClientFromEnv();

    // Create a minimal scanner for the detector (used for volume history)
    this.scanner = new MarketScanner({
      scanInterval: this.config.fullScanInterval,
      minVolume: parseFloat(process.env.MIN_VOLUME || '1000'),
    });

    this.detector = new OpportunityDetector({
      scanner: this.scanner,
      arbitrageThreshold: this.config.arbitrageThreshold,
      wideSpreadThreshold: this.config.wideSpreadThreshold,
      volumeSpikeMultiplier: this.config.volumeSpikeMultiplier,
      thinBookMakerCount: this.config.thinBookMakerCount,
    });

    this.cashBalance = this.config.initialCapital;

    // Initialize paper trader if enabled
    if (this.config.paperTradingEnabled) {
      this.paperTrader = new PaperTrader(this.config.initialCapital, {
        orderSize: 10,          // $10 per order
        tickImprovement: 0.01,  // Improve by 1 cent
        maxOrdersPerMarket: 2,
        tradingEnabled: true,
      });
    }

    console.log('MarketValidator initialized');
  }

  /**
   * Start the validator system.
   */
  async start(): Promise<void> {
    if (this.isRunning) {
      console.warn('Validator is already running');
      return;
    }

    console.log('Starting Market Validator...');
    this.startTime = new Date();

    // Initialize database
    try {
      initDatabase();
      const schemaCheck = await verifySchema();
      if (!schemaCheck.valid) {
        throw new Error(`Missing tables: ${schemaCheck.missing.join(', ')}`);
      }
      console.log('Database schema verified');
    } catch (error) {
      console.error('Database initialization failed:', error);
      throw error;
    }

    this.isRunning = true;

    // Set up signal handlers
    process.on('SIGINT', () => this.stop());
    process.on('SIGTERM', () => this.stop());

    // Run initial full scan
    console.log('Running initial full scan...');
    await this.runFullScan();

    // Select paper trading markets after first scan
    if (this.config.paperTradingEnabled) {
      await this.selectPaperTradingMarkets();
    }

    // Start intervals
    this.startIntervals();

    // Render initial dashboard
    await this.renderDashboard();
  }

  /**
   * Start all scan and analysis intervals.
   */
  private startIntervals(): void {
    // Full scan every N seconds
    this.fullScanIntervalId = setInterval(
      () => this.runFullScan(),
      this.config.fullScanInterval * 1000
    );

    // Priority scan every N seconds
    this.priorityScanIntervalId = setInterval(
      () => this.runPriorityScan(),
      this.config.priorityScanInterval * 1000
    );

    // Record P&L every 15 minutes
    this.pnlRecordIntervalId = setInterval(
      () => this.recordPnL(),
      15 * 60 * 1000
    );

    // Hourly analysis and cleanup
    this.analysisIntervalId = setInterval(
      () => this.runHourlyTasks(),
      60 * 60 * 1000
    );

    // Paper trading cycle every 60 seconds
    if (this.config.paperTradingEnabled && this.paperTrader) {
      this.paperTradingIntervalId = setInterval(
        () => this.runPaperTradingCycle(),
        60 * 1000
      );
    }

    // Dashboard refresh every 10 seconds
    this.dashboardIntervalId = setInterval(
      () => this.renderDashboard(),
      10 * 1000
    );
  }

  /**
   * Run a full scan of all markets.
   */
  async runFullScan(): Promise<void> {
    if (this.isStopping) return;

    this.pendingOperations++;
    const startTime = Date.now();
    console.log(`[${new Date().toISOString()}] Starting full scan...`);

    try {
      // Fetch all markets
      const rawMarkets = await this.client.getAllMarkets(
        true,
        undefined,
        parseFloat(process.env.MIN_VOLUME || '1000')
      );

      if (!rawMarkets || rawMarkets.length === 0) {
        console.warn('No markets returned from API');
        return;
      }

      // Build market data in batches
      const markets: MarketData[] = [];
      const batchSize = 10;

      for (let i = 0; i < rawMarkets.length; i += batchSize) {
        if (this.isStopping) return;

        const batch = rawMarkets.slice(i, i + batchSize);
        const results = await Promise.allSettled(
          batch.map((m) => this.client.buildMarketData(m))
        );

        for (const result of results) {
          if (result.status === 'fulfilled' && result.value) {
            markets.push(result.value);
          }
        }

        if ((i + batchSize) % 100 === 0) {
          console.log(`  Processed ${Math.min(i + batchSize, rawMarkets.length)}/${rawMarkets.length} markets`);
        }
      }

      if (this.isStopping) return;

      // Store in database
      const scanTimestamp = new Date();
      await withTransaction(async (client) => {
        // Insert market snapshots
        const snapshotIds = await insertMarketSnapshots(client, markets, scanTimestamp);

        // Insert order book snapshots
        await insertOrderBookSnapshots(client, markets, snapshotIds, scanTimestamp);

        // Detect opportunities
        const snapshot: MarketSnapshot = {
          timestamp: scanTimestamp,
          markets,
          opportunities: [],
          volumeDistribution: { tierUnder1k: 0, tier1kTo10k: 0, tier10kTo100k: 0, tier100kTo1m: 0, tierOver1m: 0 },
          spreadDistribution: { tightUnder1pct: 0, moderate1To3pct: 0, wide3To5pct: 0, veryWide5To10pct: 0, extremeOver10pct: 0, noSpreadData: 0 },
          totalMarkets: markets.length,
          totalVolume24h: markets.reduce((sum, m) => sum + m.volume24h, 0),
          avgSpread: 0,
        };

        const opportunities = this.detector.analyzeSnapshot(snapshot);

        // Upsert opportunities
        const opResult = await upsertOpportunities(client, opportunities, scanTimestamp);
        console.log(`  Opportunities: ${opResult.inserted} new, ${opResult.expired} expired`);
      });

      if (this.isStopping) return;

      // Update priority market list
      await this.updatePriorityMarkets();

      this.totalScans++;
      this.lastScanTime = new Date();
      this.lastScanDuration = (Date.now() - startTime) / 1000;

      console.log(
        `[${new Date().toISOString()}] Full scan complete: ${markets.length} markets in ${this.lastScanDuration.toFixed(1)}s`
      );
    } catch (error) {
      console.error('Full scan failed:', error);
    } finally {
      this.pendingOperations--;
    }
  }

  /**
   * Run a priority scan of top markets only.
   */
  async runPriorityScan(): Promise<void> {
    if (this.isStopping || this.priorityMarketIds.size === 0) {
      return;
    }

    this.pendingOperations++;
    const startTime = Date.now();

    try {
      // Fetch only priority markets
      const rawMarkets = await this.client.getAllMarkets(true, undefined, 0);
      const priorityRaw = rawMarkets.filter((m) => this.priorityMarketIds.has(m.id));

      if (priorityRaw.length === 0) return;

      // Build market data
      const markets: MarketData[] = [];
      const results = await Promise.allSettled(
        priorityRaw.map((m) => this.client.buildMarketData(m))
      );

      for (const result of results) {
        if (result.status === 'fulfilled' && result.value) {
          markets.push(result.value);
        }
      }

      // Store in database
      const scanTimestamp = new Date();
      await withTransaction(async (client) => {
        const snapshotIds = await insertMarketSnapshots(client, markets, scanTimestamp);
        await insertOrderBookSnapshots(client, markets, snapshotIds, scanTimestamp);
      });

      const duration = (Date.now() - startTime) / 1000;
      console.log(
        `[${new Date().toISOString()}] Priority scan: ${markets.length} markets in ${duration.toFixed(1)}s`
      );
    } catch (error) {
      console.error('Priority scan failed:', error);
    } finally {
      this.pendingOperations--;
    }
  }

  /**
   * Update the list of priority markets (top by volume).
   */
  private async updatePriorityMarkets(): Promise<void> {
    if (this.isStopping) return;
    try {
      const topMarkets = await getTopMarketsByVolume(24, this.config.priorityMarketCount);
      this.priorityMarketIds = new Set(topMarkets.map((m) => m.market_id));
      console.log(`  Updated priority markets: ${this.priorityMarketIds.size}`);
    } catch (error) {
      console.error('Failed to update priority markets:', error);
    }
  }

  /**
   * Select markets for paper trading.
   */
  private async selectPaperTradingMarkets(): Promise<void> {
    if (this.isStopping) return;
    console.log('Selecting paper trading markets...');

    try {
      const existingMarkets = await getActivePaperMarkets();
      if (existingMarkets.length >= this.config.marketsToSelect) {
        console.log(`Already have ${existingMarkets.length} active paper trading markets`);
        return;
      }

      // Select liquid market
      const liquid = await selectLiquidMarket();
      if (liquid) {
        await insertPaperMarket(
          liquid.market_id,
          liquid.question,
          'LIQUID',
          null,
          liquid.avg_volume,
          this.config.initialCapital / 3
        );
        console.log(`  Selected liquid market: ${liquid.market_id}`);
      }

      // Select medium volume market
      const medium = await selectMediumVolumeMarket();
      if (medium) {
        await insertPaperMarket(
          medium.market_id,
          medium.question,
          'MEDIUM_VOL',
          null,
          medium.avg_volume,
          this.config.initialCapital / 3
        );
        console.log(`  Selected medium volume market: ${medium.market_id}`);
      }

      // Select new market
      const newMarket = await selectNewMarket();
      if (newMarket) {
        await insertPaperMarket(
          newMarket.market_id,
          newMarket.question,
          'NEW_MARKET',
          null,
          newMarket.volume_24h,
          this.config.initialCapital / 3
        );
        console.log(`  Selected new market: ${newMarket.market_id}`);
      }
    } catch (error) {
      console.error('Failed to select paper trading markets:', error);
    }
  }

  /**
   * Record P&L snapshot.
   */
  private async recordPnL(): Promise<void> {
    try {
      await recordPnLSnapshot(this.cashBalance, this.config.initialCapital);
      console.log(`[${new Date().toISOString()}] P&L snapshot recorded`);
    } catch (error) {
      console.error('Failed to record P&L:', error);
    }
  }

  /**
   * Run hourly maintenance tasks.
   */
  private async runHourlyTasks(): Promise<void> {
    if (this.isStopping) return;
    console.log(`[${new Date().toISOString()}] Running hourly tasks...`);

    try {
      // Expire stale opportunities
      const expired = await expireStaleOpportunities(60);
      console.log(`  Expired ${expired} stale opportunities`);

      // Re-select paper trading markets if needed
      if (this.config.paperTradingEnabled) {
        await this.selectPaperTradingMarkets();
      }
    } catch (error) {
      console.error('Hourly tasks failed:', error);
    }
  }

  /**
   * Run one cycle of paper trading.
   */
  private async runPaperTradingCycle(): Promise<void> {
    if (this.isStopping || !this.paperTrader) return;

    this.pendingOperations++;
    try {
      // Check if we have active paper markets - if not, select new ones
      const activeMarkets = await getActivePaperMarkets();
      if (activeMarkets.length === 0) {
        console.log(`[${new Date().toISOString()}] No active paper markets found - selecting new ones...`);
        await this.selectPaperTradingMarkets();
      }

      const result = await this.paperTrader.runCycle();

      if (result.ordersPlaced > 0 || result.ordersFilled > 0) {
        console.log(
          `[${new Date().toISOString()}] Paper trading: ${result.ordersPlaced} orders placed, ${result.ordersFilled} filled`
        );
      }

      // Update cash balance from portfolio
      const portfolio = await this.paperTrader.getPortfolioSummary();
      this.cashBalance = portfolio.cashBalance;
    } catch (error) {
      console.error('Paper trading cycle failed:', error);
    } finally {
      this.pendingOperations--;
    }
  }

  /**
   * Render the live dashboard to the console.
   */
  private async renderDashboard(): Promise<void> {
    if (this.isStopping) return;

    try {
      // Gather all stats
      const opStats = await getOpportunityStats();
      const tradeStats = await getTotalTradeStats();
      const pnl = await getLatestPnL();

      let portfolio = { cashBalance: this.cashBalance, positionValue: 0, totalEquity: this.cashBalance, unrealizedPnl: 0 };
      if (this.paperTrader) {
        portfolio = await this.paperTrader.getPortfolioSummary();
      }

      const runtime = this.startTime ? this.formatDuration(Date.now() - this.startTime.getTime()) : '0s';
      const lastScan = this.lastScanTime ? this.lastScanTime.toLocaleTimeString() : 'Never';

      // Parse numeric values from DB
      const realizedPnl = parseFloat(String(pnl?.realized_pnl || 0));
      const unrealizedPnl = parseFloat(String(portfolio.unrealizedPnl || 0));
      const totalPnl = realizedPnl + unrealizedPnl;
      const cashBalance = parseFloat(String(portfolio.cashBalance || this.cashBalance));
      const positionValue = parseFloat(String(portfolio.positionValue || 0));
      const totalEquity = cashBalance + positionValue;

      // Calculate fill rate
      const totalOrders = tradeStats.total_trades > 0 ? tradeStats.total_trades * 2 : 0; // Rough estimate
      const fillRate = totalOrders > 0 ? ((tradeStats.total_trades / totalOrders) * 100).toFixed(1) : '0.0';

      // Format P&L with color indicators
      const pnlSign = totalPnl >= 0 ? '+' : '';
      const pnlDisplay = `${pnlSign}$${totalPnl.toFixed(2)}`;

      // Clear screen and move cursor to top
      process.stdout.write('\x1B[2J\x1B[0f');

      // Render dashboard
      console.log(`
╔══════════════════════════════════════════════════════════════════════════════╗
║                    POLYMARKET MARKET VALIDATOR                               ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  Runtime: ${runtime.padEnd(20)}                    Last scan: ${lastScan.padEnd(15)}   ║
║  Total scans: ${String(this.totalScans).padEnd(10)}                        Scan duration: ${this.lastScanDuration.toFixed(1).padEnd(6)}s  ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  OPPORTUNITIES DETECTED                                                      ║
║  ├─ Arbitrage:    ${String(opStats.by_type.arbitrage || 0).padEnd(8)}  ├─ Mispricing:   ${String(opStats.by_type.mispricing || 0).padEnd(8)}           ║
║  ├─ Wide Spread:  ${String(opStats.by_type.wide_spread || 0).padEnd(8)}  └─ Thin Book:   ${String(opStats.by_type.thin_book || 0).padEnd(8)}           ║
║  └─ Total:        ${String(opStats.total).padEnd(8)}                                             ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  PAPER TRADING                                                               ║
║  ├─ Trades executed:  ${String(tradeStats.total_trades).padEnd(8)}                                       ║
║  ├─ Total volume:     $${tradeStats.total_volume.toFixed(2).padEnd(12)}                                  ║
║  └─ Total fees:       $${tradeStats.total_fees.toFixed(2).padEnd(12)}                                  ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  PORTFOLIO                                                                   ║
║  ├─ Cash balance:     $${cashBalance.toFixed(2).padEnd(12)}                                  ║
║  ├─ Position value:   $${positionValue.toFixed(2).padEnd(12)}                                  ║
║  ├─ Total equity:     $${totalEquity.toFixed(2).padEnd(12)}                                  ║
║  ├─ Realized P&L:     $${realizedPnl.toFixed(2).padEnd(12)}                                  ║
║  ├─ Unrealized P&L:   $${unrealizedPnl.toFixed(2).padEnd(12)}                                  ║
║  └─ TOTAL P&L:        ${pnlDisplay.padEnd(14)}                                  ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  Config: Full scan ${this.config.fullScanInterval}s | Priority scan ${this.config.priorityScanInterval}s | ${this.priorityMarketIds.size} priority markets        ║
║  Press Ctrl+C to stop                                                        ║
╚══════════════════════════════════════════════════════════════════════════════╝
      `);
    } catch (error) {
      // Silently ignore dashboard errors to avoid spam
    }
  }

  /**
   * Get current validator statistics.
   */
  async getStats(): Promise<ValidatorStats> {
    const opStats = await getOpportunityStats();
    const tableCounts = await getTableCounts();
    const pnl = await getLatestPnL();
    const tradeStats = await getTotalTradeStats();

    return {
      startTime: this.startTime || new Date(),
      uptime: this.startTime ? Date.now() - this.startTime.getTime() : 0,
      totalScans: this.totalScans,
      totalOpportunities: opStats.total,
      opportunitiesByType: opStats.by_type,
      paperTradingEnabled: this.config.paperTradingEnabled,
      totalPaperTrades: tradeStats.total_trades,
      currentPnl: parseFloat(String(pnl?.total_pnl || 0)),
      lastScanTime: this.lastScanTime,
      lastScanDuration: this.lastScanDuration,
      dbRowCounts: tableCounts,
    };
  }

  /**
   * Stop the validator.
   */
  async stop(): Promise<void> {
    if (!this.isRunning || this.isStopping) return;

    console.log('\nStopping Market Validator...');
    this.isStopping = true;
    this.isRunning = false;

    // Clear intervals
    if (this.fullScanIntervalId) clearInterval(this.fullScanIntervalId);
    if (this.priorityScanIntervalId) clearInterval(this.priorityScanIntervalId);
    if (this.analysisIntervalId) clearInterval(this.analysisIntervalId);
    if (this.pnlRecordIntervalId) clearInterval(this.pnlRecordIntervalId);
    if (this.paperTradingIntervalId) clearInterval(this.paperTradingIntervalId);
    if (this.dashboardIntervalId) clearInterval(this.dashboardIntervalId);

    // Wait for pending operations to complete (max 10 seconds)
    let waitTime = 0;
    while (this.pendingOperations > 0 && waitTime < 10000) {
      await new Promise((resolve) => setTimeout(resolve, 500));
      waitTime += 500;
    }

    // Record final P&L
    await this.recordPnL();

    // Print final stats
    const stats = await this.getStats();
    console.log('\n' + '='.repeat(60));
    console.log('MARKET VALIDATOR - FINAL STATISTICS');
    console.log('='.repeat(60));
    console.log(`Runtime: ${this.formatDuration(stats.uptime)}`);
    console.log(`Total scans: ${stats.totalScans}`);
    console.log(`Total opportunities detected: ${stats.totalOpportunities}`);
    console.log('By type:');
    for (const [type, count] of Object.entries(stats.opportunitiesByType)) {
      console.log(`  - ${type}: ${count}`);
    }
    if (stats.paperTradingEnabled) {
      console.log(`Paper trades: ${stats.totalPaperTrades}`);
      console.log(`Current P&L: $${stats.currentPnl.toFixed(2)}`);
    }
    console.log('='.repeat(60));

    // Close database
    await closeDatabase();
    console.log('Validator stopped');

    process.exit(0);
  }

  private formatDuration(ms: number): string {
    const seconds = Math.floor(ms / 1000);
    const minutes = Math.floor(seconds / 60);
    const hours = Math.floor(minutes / 60);
    const days = Math.floor(hours / 24);

    if (days > 0) return `${days}d ${hours % 24}h ${minutes % 60}m`;
    if (hours > 0) return `${hours}h ${minutes % 60}m ${seconds % 60}s`;
    if (minutes > 0) return `${minutes}m ${seconds % 60}s`;
    return `${seconds}s`;
  }
}

/**
 * Create a MarketValidator from environment variables.
 */
export function createValidatorFromEnv(): MarketValidator {
  return new MarketValidator({
    fullScanInterval: parseInt(process.env.FULL_SCAN_INTERVAL || '60'),
    priorityScanInterval: parseInt(process.env.PRIORITY_SCAN_INTERVAL || '15'),
    priorityMarketCount: parseInt(process.env.PRIORITY_MARKET_COUNT || '100'),
    paperTradingEnabled: process.env.PAPER_TRADING_ENABLED !== 'false',
    initialCapital: parseFloat(process.env.INITIAL_CAPITAL || '1000'),
    marketsToSelect: parseInt(process.env.MARKETS_TO_SELECT || '3'),
    retentionDays: parseInt(process.env.RETENTION_DAYS || '7'),
    arbitrageThreshold: parseFloat(process.env.ARBITRAGE_THRESHOLD || '0.995'),
    wideSpreadThreshold: parseFloat(process.env.WIDE_SPREAD_THRESHOLD || '0.05'),
    volumeSpikeMultiplier: parseFloat(process.env.VOLUME_SPIKE_MULTIPLIER || '3.0'),
    thinBookMakerCount: parseInt(process.env.THIN_BOOK_MAKER_COUNT || '5'),
  });
}

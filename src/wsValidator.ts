/**
 * WebSocket-based validator - uses real-time data instead of REST polling.
 * This is an alternative to the REST-based MarketValidator that provides
 * lower latency updates via WebSocket connections.
 */

import {
  ValidatorConfig,
  DEFAULT_VALIDATOR_CONFIG,
  ValidatorStats,
  OpportunityType,
} from './types';
import { WSMarketScanner, WSPriceUpdate, createWSScannerFromEnv } from './wsScanner';
import {
  initDatabase,
  closeDatabase,
  withTransaction,
  getPool,
} from './database/index';
import { verifySchema, getTableCounts } from './database/schema';
import { batchInsertWSUpdates, detectAllOpportunitiesFromWS } from './database/wsRepo';
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

export class WSMarketValidator {
  private wsScanner: WSMarketScanner;
  private config: ValidatorConfig;

  private isRunning: boolean = false;
  private isStopping: boolean = false;
  private startTime: Date | null = null;
  private totalUpdates: number = 0;
  private lastUpdateTime: Date | null = null;

  // Intervals for periodic tasks
  private dbFlushIntervalId: NodeJS.Timeout | null = null;
  private subscriptionRefreshIntervalId: NodeJS.Timeout | null = null;
  private arbDetectionIntervalId: NodeJS.Timeout | null = null;
  private pnlRecordIntervalId: NodeJS.Timeout | null = null;
  private paperTradingIntervalId: NodeJS.Timeout | null = null;
  private dashboardIntervalId: NodeJS.Timeout | null = null;
  private hourlyTasksIntervalId: NodeJS.Timeout | null = null;

  // Buffered updates for batch DB writes
  private updateBuffer: WSPriceUpdate[] = [];
  private lastDbFlushTime: Date = new Date();

  private cashBalance: number;
  private paperTrader: PaperTrader | null = null;

  constructor(config: Partial<ValidatorConfig> = {}) {
    this.config = { ...DEFAULT_VALIDATOR_CONFIG, ...config };

    // Create WebSocket scanner (no REST polling needed)
    this.wsScanner = createWSScannerFromEnv();

    this.cashBalance = this.config.initialCapital;

    // Initialize paper trader if enabled
    if (this.config.paperTradingEnabled) {
      this.paperTrader = new PaperTrader(this.config.initialCapital, {
        orderSize: 100,
        tickImprovement: 0.01,
        maxOrdersPerMarket: 2,
        tradingEnabled: true,
      });
    }

    // Set up WebSocket event handlers
    this.setupWSHandlers();

    console.log('[WS-VALIDATOR] Initialized');
  }

  /**
   * Set up WebSocket event handlers.
   */
  private setupWSHandlers(): void {
    // Handle price updates
    this.wsScanner.on('priceUpdate', (updates: WSPriceUpdate[]) => {
      this.handlePriceUpdates(updates);
    });

    // Handle trades
    this.wsScanner.on('trade', (trade: any) => {
      // Could log or process trades here
    });

    // Handle connection events
    this.wsScanner.on('connected', () => {
      console.log('[WS-VALIDATOR] WebSocket connected');
    });

    this.wsScanner.on('disconnected', () => {
      console.log('[WS-VALIDATOR] WebSocket disconnected');
    });
  }

  /**
   * Handle incoming price updates.
   */
  private handlePriceUpdates(updates: WSPriceUpdate[]): void {
    this.totalUpdates += updates.length;
    this.lastUpdateTime = new Date();

    // Add to buffer for batch DB write
    this.updateBuffer.push(...updates);
  }

  /**
   * Start the WebSocket validator.
   */
  async start(): Promise<void> {
    if (this.isRunning) {
      console.warn('[WS-VALIDATOR] Already running');
      return;
    }

    console.log('[WS-VALIDATOR] Starting...');
    this.startTime = new Date();

    // Initialize database
    try {
      initDatabase();
      const schemaCheck = await verifySchema();
      if (!schemaCheck.valid) {
        throw new Error(`Missing tables: ${schemaCheck.missing.join(', ')}`);
      }
      console.log('[WS-VALIDATOR] Database schema verified');
    } catch (error) {
      console.error('[WS-VALIDATOR] Database initialization failed:', error);
      throw error;
    }

    this.isRunning = true;

    // Set up signal handlers
    process.on('SIGINT', () => this.stop());
    process.on('SIGTERM', () => this.stop());

    // Start WebSocket scanner
    await this.wsScanner.start();

    // Select paper trading markets
    if (this.config.paperTradingEnabled) {
      await this.selectPaperTradingMarkets();
    }

    // Start periodic tasks
    this.startIntervals();

    // Render initial dashboard
    await this.renderDashboard();
  }

  /**
   * Start periodic task intervals.
   */
  private startIntervals(): void {
    // Flush updates to DB every 5 seconds
    this.dbFlushIntervalId = setInterval(
      () => this.flushUpdatesToDb(),
      5000
    );

    // Refresh subscriptions every 5 minutes
    this.subscriptionRefreshIntervalId = setInterval(
      () => this.wsScanner.refreshSubscriptions(),
      5 * 60 * 1000
    );

    // Detect all opportunities every 10 seconds
    this.arbDetectionIntervalId = setInterval(
      () => this.detectOpportunities(),
      10 * 1000
    );

    // Record P&L every 15 minutes
    this.pnlRecordIntervalId = setInterval(
      () => this.recordPnL(),
      15 * 60 * 1000
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

    // Hourly tasks
    this.hourlyTasksIntervalId = setInterval(
      () => this.runHourlyTasks(),
      60 * 60 * 1000
    );
  }

  /**
   * Flush buffered updates to the database.
   */
  private async flushUpdatesToDb(): Promise<void> {
    if (this.isStopping || this.updateBuffer.length === 0) return;

    const updates = this.updateBuffer.splice(0, this.updateBuffer.length);
    const scanTimestamp = new Date();

    try {
      const inserted = await batchInsertWSUpdates(updates, scanTimestamp);
      this.lastDbFlushTime = new Date();
    } catch (error) {
      console.error('[WS-VALIDATOR] Failed to flush updates to DB:', error);
      // Re-add failed updates to buffer (up to a limit)
      if (this.updateBuffer.length < 10000) {
        this.updateBuffer.unshift(...updates);
      }
    }
  }

  /**
   * Detect all opportunity types from WebSocket data.
   */
  private async detectOpportunities(): Promise<void> {
    if (this.isStopping) return;

    try {
      const allOpportunities = await detectAllOpportunitiesFromWS({
        arbitrageThreshold: this.config.arbitrageThreshold,
        wideSpreadThreshold: this.config.wideSpreadThreshold,
        volumeSpikeMultiplier: this.config.volumeSpikeMultiplier,
      });

      const { summary } = allOpportunities;

      if (summary.totalCount > 0) {
        console.log(
          `[WS-VALIDATOR] Found ${summary.totalCount} opportunities: ` +
          `arb=${summary.arbitrageCount}, spread=${summary.wideSpreadCount}, ` +
          `spike=${summary.volumeSpikeCount}, thin=${summary.thinBookCount}, misprice=${summary.mispricingCount}`
        );

        const opportunities: Array<{
          type: OpportunityType;
          marketId: string;
          question: string;
          timestamp: Date;
          description: string;
          potentialProfit: number;
          yesNoSum: number;
          availableLiquidity: number;
          spreadPct: number;
          tokenSide: string;
          currentVolume: number;
          averageVolume: number;
          spikeMultiplier: number;
          makerCount: number;
          volume: number;
          relatedMarketId: string;
          priceDifference: number;
        }> = [];

        // Convert arbitrage opportunities
        for (const arb of allOpportunities.arbitrage) {
          opportunities.push({
            type: OpportunityType.ARBITRAGE,
            marketId: arb.marketId,
            question: '',
            timestamp: new Date(),
            description: `YES+NO=${arb.sum.toFixed(4)} (profit: ${(arb.profit * 100).toFixed(2)}%)`,
            potentialProfit: arb.profit,
            yesNoSum: arb.sum,
            availableLiquidity: 0,
            spreadPct: 0,
            tokenSide: '',
            currentVolume: 0,
            averageVolume: 0,
            spikeMultiplier: 0,
            makerCount: 0,
            volume: 0,
            relatedMarketId: '',
            priceDifference: 0,
          });

          // Auto-add arbitrage to paper trading
          try {
            await insertPaperMarket(arb.marketId, null, 'ARBITRAGE', null, 0, this.config.initialCapital / 10);
          } catch (error) {
            // Market may already exist
          }
        }

        // Convert wide spread opportunities
        for (const spread of allOpportunities.wideSpread) {
          opportunities.push({
            type: OpportunityType.WIDE_SPREAD,
            marketId: spread.marketId,
            question: '',
            timestamp: new Date(),
            description: `${spread.tokenSide} spread=${(spread.spreadPct * 100).toFixed(2)}%`,
            potentialProfit: 0,
            yesNoSum: 0,
            availableLiquidity: spread.liquidity,
            spreadPct: spread.spreadPct,
            tokenSide: spread.tokenSide,
            currentVolume: 0,
            averageVolume: 0,
            spikeMultiplier: 0,
            makerCount: 0,
            volume: 0,
            relatedMarketId: '',
            priceDifference: 0,
          });
        }

        // Convert volume spike opportunities
        for (const spike of allOpportunities.volumeSpike) {
          opportunities.push({
            type: OpportunityType.VOLUME_SPIKE,
            marketId: spike.marketId,
            question: '',
            timestamp: new Date(),
            description: `Volume ${spike.multiplier.toFixed(1)}x above average`,
            potentialProfit: 0,
            yesNoSum: 0,
            availableLiquidity: 0,
            spreadPct: 0,
            tokenSide: '',
            currentVolume: spike.currentVolume,
            averageVolume: spike.avgVolume,
            spikeMultiplier: spike.multiplier,
            makerCount: 0,
            volume: spike.currentVolume,
            relatedMarketId: '',
            priceDifference: 0,
          });
        }

        // Convert thin book opportunities
        for (const thin of allOpportunities.thinBook) {
          opportunities.push({
            type: OpportunityType.THIN_BOOK,
            marketId: thin.marketId,
            question: '',
            timestamp: new Date(),
            description: `Low liquidity ($${thin.totalLiquidity.toFixed(0)}) with $${thin.volume24h.toLocaleString()} volume`,
            potentialProfit: 0,
            yesNoSum: 0,
            availableLiquidity: thin.totalLiquidity,
            spreadPct: 0,
            tokenSide: '',
            currentVolume: 0,
            averageVolume: 0,
            spikeMultiplier: 0,
            makerCount: 0,
            volume: thin.volume24h,
            relatedMarketId: '',
            priceDifference: 0,
          });
        }

        // Convert mispricing opportunities
        for (const misprice of allOpportunities.mispricing) {
          opportunities.push({
            type: OpportunityType.MISPRICING,
            marketId: misprice.marketId1,
            question: misprice.question1,
            timestamp: new Date(),
            description: `Price diff ${(misprice.priceDifference * 100).toFixed(1)}% vs ${misprice.question2.slice(0, 30)}`,
            potentialProfit: 0,
            yesNoSum: 0,
            availableLiquidity: 0,
            spreadPct: 0,
            tokenSide: '',
            currentVolume: 0,
            averageVolume: 0,
            spikeMultiplier: 0,
            makerCount: 0,
            volume: 0,
            relatedMarketId: misprice.marketId2,
            priceDifference: misprice.priceDifference,
          });
        }

        // Store all opportunities
        if (opportunities.length > 0) {
          await withTransaction(async (client) => {
            await upsertOpportunities(client, opportunities, new Date());
          });
        }
      }
    } catch (error) {
      console.error('[WS-VALIDATOR] Opportunity detection failed:', error);
    }
  }

  /**
   * Select markets for paper trading.
   */
  private async selectPaperTradingMarkets(): Promise<void> {
    if (this.isStopping) return;
    console.log('[WS-VALIDATOR] Selecting paper trading markets...');

    try {
      const existingMarkets = await getActivePaperMarkets();
      if (existingMarkets.length >= this.config.marketsToSelect) {
        console.log(`[WS-VALIDATOR] Already have ${existingMarkets.length} active paper trading markets`);
        return;
      }

      // Select markets based on different criteria
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
        console.log(`[WS-VALIDATOR] Selected liquid market: ${liquid.market_id}`);
      }

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
        console.log(`[WS-VALIDATOR] Selected medium volume market: ${medium.market_id}`);
      }

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
        console.log(`[WS-VALIDATOR] Selected new market: ${newMarket.market_id}`);
      }
    } catch (error) {
      console.error('[WS-VALIDATOR] Failed to select paper trading markets:', error);
    }
  }

  /**
   * Record P&L snapshot.
   */
  private async recordPnL(): Promise<void> {
    try {
      await recordPnLSnapshot(this.cashBalance, this.config.initialCapital);
      console.log(`[WS-VALIDATOR] P&L snapshot recorded`);
    } catch (error) {
      console.error('[WS-VALIDATOR] Failed to record P&L:', error);
    }
  }

  /**
   * Run one cycle of paper trading.
   */
  private async runPaperTradingCycle(): Promise<void> {
    if (this.isStopping || !this.paperTrader) return;

    try {
      const activeMarkets = await getActivePaperMarkets();
      if (activeMarkets.length === 0) {
        await this.selectPaperTradingMarkets();
      }

      const result = await this.paperTrader.runCycle();

      if (result.ordersPlaced > 0 || result.ordersFilled > 0) {
        console.log(
          `[WS-VALIDATOR] Paper trading: ${result.ordersPlaced} orders, ${result.ordersFilled} fills`
        );
      }

      const portfolio = await this.paperTrader.getPortfolioSummary();
      this.cashBalance = portfolio.cashBalance;
    } catch (error) {
      console.error('[WS-VALIDATOR] Paper trading cycle failed:', error);
    }
  }

  /**
   * Run hourly maintenance tasks.
   */
  private async runHourlyTasks(): Promise<void> {
    if (this.isStopping) return;
    console.log(`[WS-VALIDATOR] Running hourly tasks...`);

    try {
      // Expire stale opportunities
      const expired = await expireStaleOpportunities(60);
      console.log(`[WS-VALIDATOR] Expired ${expired} stale opportunities`);

      // Re-select paper trading markets if needed
      if (this.config.paperTradingEnabled) {
        await this.selectPaperTradingMarkets();
      }
    } catch (error) {
      console.error('[WS-VALIDATOR] Hourly tasks failed:', error);
    }
  }

  /**
   * Render the live dashboard.
   */
  private async renderDashboard(): Promise<void> {
    if (this.isStopping) return;

    try {
      const wsStats = this.wsScanner.getStats();
      const opStats = await getOpportunityStats();
      const tradeStats = await getTotalTradeStats();
      const pnl = await getLatestPnL();

      let portfolio = { cashBalance: this.cashBalance, positionValue: 0, totalEquity: this.cashBalance, unrealizedPnl: 0 };
      if (this.paperTrader) {
        portfolio = await this.paperTrader.getPortfolioSummary();
      }

      const runtime = this.startTime ? this.formatDuration(Date.now() - this.startTime.getTime()) : '0s';
      const lastUpdate = this.lastUpdateTime ? this.lastUpdateTime.toLocaleTimeString() : 'Never';

      const realizedPnl = parseFloat(String(pnl?.realized_pnl || 0));
      const unrealizedPnl = parseFloat(String(portfolio.unrealizedPnl || 0));
      const totalPnl = realizedPnl + unrealizedPnl;
      const cashBalance = parseFloat(String(portfolio.cashBalance || this.cashBalance));
      const positionValue = parseFloat(String(portfolio.positionValue || 0));
      const totalEquity = cashBalance + positionValue;

      const pnlSign = totalPnl >= 0 ? '+' : '';
      const pnlDisplay = `${pnlSign}$${totalPnl.toFixed(2)}`;

      const connStatus = wsStats.isConnected ? '🟢 CONNECTED' : '🔴 DISCONNECTED';

      // Clear screen
      process.stdout.write('\x1B[2J\x1B[0f');

      console.log(`
╔══════════════════════════════════════════════════════════════════════════════╗
║                POLYMARKET WEBSOCKET VALIDATOR                                ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  Runtime: ${runtime.padEnd(20)}                    Status: ${connStatus.padEnd(16)}  ║
║  Total updates: ${String(this.totalUpdates).padEnd(10)}                       Last update: ${lastUpdate.padEnd(12)} ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  WEBSOCKET CONNECTION                                                        ║
║  ├─ Subscribed assets:  ${String(wsStats.subscribedAssets).padEnd(8)}                                     ║
║  ├─ Subscribed markets: ${String(wsStats.subscribedMarkets).padEnd(8)}                                     ║
║  ├─ Cached prices:      ${String(wsStats.cachedPrices).padEnd(8)}                                     ║
║  ├─ Messages received:  ${String(wsStats.messagesReceived).padEnd(8)}                                     ║
║  └─ Reconnects:         ${String(wsStats.reconnects).padEnd(8)}                                     ║
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
║  └─ TOTAL P&L:        ${pnlDisplay.padEnd(14)}                                  ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  Mode: WebSocket Real-Time | Buffer: ${String(this.updateBuffer.length).padEnd(6)} | Press Ctrl+C to stop     ║
╚══════════════════════════════════════════════════════════════════════════════╝
      `);
    } catch (error) {
      // Silently ignore dashboard errors
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
      totalScans: this.totalUpdates,
      totalOpportunities: opStats.total,
      opportunitiesByType: opStats.by_type,
      paperTradingEnabled: this.config.paperTradingEnabled,
      totalPaperTrades: tradeStats.total_trades,
      currentPnl: parseFloat(String(pnl?.total_pnl || 0)),
      lastScanTime: this.lastUpdateTime,
      lastScanDuration: 0,
      dbRowCounts: tableCounts,
    };
  }

  /**
   * Stop the validator.
   */
  async stop(): Promise<void> {
    if (!this.isRunning || this.isStopping) return;

    console.log('\n[WS-VALIDATOR] Stopping...');
    this.isStopping = true;
    this.isRunning = false;

    // Stop WebSocket scanner
    this.wsScanner.stop();

    // Clear all intervals
    if (this.dbFlushIntervalId) clearInterval(this.dbFlushIntervalId);
    if (this.subscriptionRefreshIntervalId) clearInterval(this.subscriptionRefreshIntervalId);
    if (this.arbDetectionIntervalId) clearInterval(this.arbDetectionIntervalId);
    if (this.pnlRecordIntervalId) clearInterval(this.pnlRecordIntervalId);
    if (this.paperTradingIntervalId) clearInterval(this.paperTradingIntervalId);
    if (this.dashboardIntervalId) clearInterval(this.dashboardIntervalId);
    if (this.hourlyTasksIntervalId) clearInterval(this.hourlyTasksIntervalId);

    // Flush remaining updates
    await this.flushUpdatesToDb();

    // Record final P&L
    await this.recordPnL();

    // Print final stats
    const stats = await this.getStats();
    const wsStats = this.wsScanner.getStats();

    console.log('\n' + '='.repeat(60));
    console.log('WEBSOCKET VALIDATOR - FINAL STATISTICS');
    console.log('='.repeat(60));
    console.log(`Runtime: ${this.formatDuration(stats.uptime)}`);
    console.log(`Total price updates: ${this.totalUpdates}`);
    console.log(`Messages received: ${wsStats.messagesReceived}`);
    console.log(`Reconnects: ${wsStats.reconnects}`);
    console.log(`Total opportunities: ${stats.totalOpportunities}`);
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
    console.log('[WS-VALIDATOR] Stopped');

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
 * Create a WSMarketValidator from environment variables.
 */
export function createWSValidatorFromEnv(): WSMarketValidator {
  return new WSMarketValidator({
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

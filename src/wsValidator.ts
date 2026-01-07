/**
 * WebSocket-based validator - uses real-time data instead of REST polling.
 *
 * CRITICAL: Arbitrage detection runs on EVERY WebSocket price update for
 * instant reaction. Market making runs on a slower 60-second cycle.
 */

import { v4 as uuidv4 } from 'uuid';
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
  insertPaperOrder,
  insertPaperTrade,
  upsertPosition,
} from './database/paperTradingRepo';
import { PaperTrader } from './paperTrader';
import { calculateTradeCosts, calculateNetValue } from './paperTrader/costCalculator';

// ============ FAST-PATH ARBITRAGE TYPES ============

interface MarketPrices {
  yesAsk: number | null;
  yesBid: number | null;
  noAsk: number | null;
  noBid: number | null;
  yesAskSize: number | null;
  noAskSize: number | null;
  lastUpdate: Date;
}

interface ArbitrageExecution {
  marketId: string;
  yesAsk: number;
  noAsk: number;
  sum: number;
  profit: number;
  size: number;
  wssTimestamp: Date;
  detectionTime: number;  // ms from WSS update to detection
  executionTime: number;  // ms from detection to order placement
  totalLatency: number;   // ms from WSS update to order placement
}

interface LatencyMetrics {
  totalExecutions: number;
  avgDetectionTime: number;
  avgExecutionTime: number;
  avgTotalLatency: number;
  minLatency: number;
  maxLatency: number;
  last10Latencies: number[];
}

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

  // ============ FAST-PATH ARBITRAGE STATE ============
  // In-memory price cache for instant arbitrage detection
  private priceCache: Map<string, MarketPrices> = new Map();

  // Rate limiting: track last arbitrage execution per market
  private lastArbExecution: Map<string, number> = new Map();
  private readonly ARB_RATE_LIMIT_MS = 1000; // 1 second between executions per market

  // Latency tracking
  private arbExecutions: ArbitrageExecution[] = [];
  private latencyMetrics: LatencyMetrics = {
    totalExecutions: 0,
    avgDetectionTime: 0,
    avgExecutionTime: 0,
    avgTotalLatency: 0,
    minLatency: Infinity,
    maxLatency: 0,
    last10Latencies: [],
  };

  // Arbitrage config
  private readonly ARB_THRESHOLD = 0.995;  // YES + NO must be < this
  private readonly ARB_ORDER_SIZE = 50;    // Contracts per side

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
   * CRITICAL: This runs on EVERY WebSocket message for real-time arbitrage detection.
   */
  private handlePriceUpdates(updates: WSPriceUpdate[]): void {
    const receiveTime = Date.now();
    this.totalUpdates += updates.length;
    this.lastUpdateTime = new Date();

    // Add to buffer for batch DB write
    this.updateBuffer.push(...updates);

    // FAST PATH: Update price cache and check for arbitrage immediately
    for (const update of updates) {
      this.updatePriceCache(update, receiveTime);
    }

    // Check all updated markets for arbitrage opportunities
    const updatedMarketIds = new Set(updates.map(u => u.marketId));
    for (const marketId of updatedMarketIds) {
      this.checkAndExecuteArbitrage(marketId, receiveTime);
    }
  }

  /**
   * Update in-memory price cache with WebSocket data.
   */
  private updatePriceCache(update: WSPriceUpdate, receiveTime: number): void {
    let prices = this.priceCache.get(update.marketId);
    if (!prices) {
      prices = {
        yesAsk: null,
        yesBid: null,
        noAsk: null,
        noBid: null,
        yesAskSize: null,
        noAskSize: null,
        lastUpdate: new Date(),
      };
      this.priceCache.set(update.marketId, prices);
    }

    if (update.outcome === 'YES') {
      prices.yesAsk = update.bestAsk?.price ?? prices.yesAsk;
      prices.yesBid = update.bestBid?.price ?? prices.yesBid;
      prices.yesAskSize = update.bestAsk?.size ?? prices.yesAskSize;
    } else {
      prices.noAsk = update.bestAsk?.price ?? prices.noAsk;
      prices.noBid = update.bestBid?.price ?? prices.noBid;
      prices.noAskSize = update.bestAsk?.size ?? prices.noAskSize;
    }
    prices.lastUpdate = new Date(receiveTime);
  }

  /**
   * FAST-PATH ARBITRAGE: Check and execute arbitrage instantly.
   * This runs on EVERY price update - must be extremely fast.
   */
  private async checkAndExecuteArbitrage(marketId: string, wssReceiveTime: number): Promise<void> {
    const detectionStart = Date.now();

    // Rate limiting: don't execute more than once per second per market
    const lastExec = this.lastArbExecution.get(marketId) || 0;
    if (detectionStart - lastExec < this.ARB_RATE_LIMIT_MS) {
      return;
    }

    const prices = this.priceCache.get(marketId);
    if (!prices || prices.yesAsk === null || prices.noAsk === null) {
      return;
    }

    const sum = prices.yesAsk + prices.noAsk;

    // Check if arbitrage exists: YES_ask + NO_ask < threshold
    if (sum >= this.ARB_THRESHOLD) {
      return;
    }

    const detectionTime = Date.now() - detectionStart;
    const profit = 1 - sum;

    // Calculate available size (minimum of both sides)
    const availableSize = Math.min(
      prices.yesAskSize || 0,
      prices.noAskSize || 0,
      this.ARB_ORDER_SIZE
    );

    if (availableSize < 10) {
      // Not enough liquidity
      return;
    }

    console.log(
      `[FAST-ARB] OPPORTUNITY DETECTED: ${marketId} ` +
      `YES=${prices.yesAsk.toFixed(4)} NO=${prices.noAsk.toFixed(4)} ` +
      `sum=${sum.toFixed(4)} profit=${(profit * 100).toFixed(2)}% ` +
      `detection=${detectionTime}ms`
    );

    // Execute arbitrage orders
    const executionStart = Date.now();
    try {
      await this.executeArbitrageOrders(marketId, prices, availableSize);
      this.lastArbExecution.set(marketId, Date.now());

      const executionTime = Date.now() - executionStart;
      const totalLatency = Date.now() - wssReceiveTime;

      // Track latency metrics
      this.recordLatencyMetrics({
        marketId,
        yesAsk: prices.yesAsk,
        noAsk: prices.noAsk,
        sum,
        profit,
        size: availableSize,
        wssTimestamp: new Date(wssReceiveTime),
        detectionTime,
        executionTime,
        totalLatency,
      });

      console.log(
        `[FAST-ARB] EXECUTED: ${marketId} ` +
        `size=${availableSize} ` +
        `latency: detect=${detectionTime}ms exec=${executionTime}ms total=${totalLatency}ms`
      );
    } catch (error) {
      console.error(`[FAST-ARB] Execution failed for ${marketId}:`, error);
    }
  }

  /**
   * Execute arbitrage orders atomically (both YES and NO).
   */
  private async executeArbitrageOrders(
    marketId: string,
    prices: MarketPrices,
    size: number
  ): Promise<void> {
    if (prices.yesAsk === null || prices.noAsk === null) return;

    const yesOrderId = uuidv4();
    const noOrderId = uuidv4();
    const yesTradeId = uuidv4();
    const noTradeId = uuidv4();

    // Calculate trade values and costs
    const yesValue = size * prices.yesAsk;
    const noValue = size * prices.noAsk;
    const yesCosts = calculateTradeCosts(yesValue);
    const noCosts = calculateTradeCosts(noValue);
    const yesNetValue = calculateNetValue(yesValue, 'BUY', yesCosts);
    const noNetValue = calculateNetValue(noValue, 'BUY', noCosts);

    await withTransaction(async (client) => {
      // Place YES BUY order
      await insertPaperOrder(client, {
        marketId,
        orderId: yesOrderId,
        side: 'BUY',
        tokenSide: 'YES',
        orderPrice: prices.yesAsk!,
        orderSize: size,
        bestBidAtOrder: prices.yesBid,
        bestAskAtOrder: prices.yesAsk,
        spreadAtOrder: prices.yesAsk! - (prices.yesBid || 0),
      });

      // Place NO BUY order
      await insertPaperOrder(client, {
        marketId,
        orderId: noOrderId,
        side: 'BUY',
        tokenSide: 'NO',
        orderPrice: prices.noAsk!,
        orderSize: size,
        bestBidAtOrder: prices.noBid,
        bestAskAtOrder: prices.noAsk,
        spreadAtOrder: prices.noAsk! - (prices.noBid || 0),
      });

      // Record YES trade
      await insertPaperTrade(client, {
        tradeId: yesTradeId,
        marketId,
        orderId: yesOrderId,
        side: 'BUY',
        tokenSide: 'YES',
        price: prices.yesAsk!,
        size,
        value: yesValue,
        platformFee: yesCosts.platformFee,
        gasCost: yesCosts.gasCost,
        slippageCost: yesCosts.slippageCost,
        totalCost: yesCosts.totalCost,
        netValue: yesNetValue,
      });

      // Record NO trade
      await insertPaperTrade(client, {
        tradeId: noTradeId,
        marketId,
        orderId: noOrderId,
        side: 'BUY',
        tokenSide: 'NO',
        price: prices.noAsk!,
        size,
        value: noValue,
        platformFee: noCosts.platformFee,
        gasCost: noCosts.gasCost,
        slippageCost: noCosts.slippageCost,
        totalCost: noCosts.totalCost,
        netValue: noNetValue,
      });

      // Update positions
      // For arbitrage BUY: cost basis = price * size (what we paid)
      const yesCostBasis = prices.yesAsk! * size;
      const noCostBasis = prices.noAsk! * size;
      await upsertPosition(client, marketId, 'YES', size, prices.yesAsk!, yesCostBasis, prices.yesAsk!);
      await upsertPosition(client, marketId, 'NO', size, prices.noAsk!, noCostBasis, prices.noAsk!);
    });
  }

  /**
   * Record latency metrics for monitoring.
   */
  private recordLatencyMetrics(execution: ArbitrageExecution): void {
    this.arbExecutions.push(execution);

    // Keep only last 100 executions
    if (this.arbExecutions.length > 100) {
      this.arbExecutions.shift();
    }

    // Update metrics
    this.latencyMetrics.totalExecutions++;
    this.latencyMetrics.last10Latencies.push(execution.totalLatency);
    if (this.latencyMetrics.last10Latencies.length > 10) {
      this.latencyMetrics.last10Latencies.shift();
    }

    // Recalculate averages
    const recent = this.arbExecutions.slice(-20);
    this.latencyMetrics.avgDetectionTime = recent.reduce((s, e) => s + e.detectionTime, 0) / recent.length;
    this.latencyMetrics.avgExecutionTime = recent.reduce((s, e) => s + e.executionTime, 0) / recent.length;
    this.latencyMetrics.avgTotalLatency = recent.reduce((s, e) => s + e.totalLatency, 0) / recent.length;
    this.latencyMetrics.minLatency = Math.min(this.latencyMetrics.minLatency, execution.totalLatency);
    this.latencyMetrics.maxLatency = Math.max(this.latencyMetrics.maxLatency, execution.totalLatency);
  }

  /**
   * Get latency metrics for display.
   */
  getLatencyMetrics(): LatencyMetrics {
    return { ...this.latencyMetrics };
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

      // Latency metrics for fast-path arbitrage
      const latency = this.latencyMetrics;
      const avgLatency = latency.totalExecutions > 0 ? `${latency.avgTotalLatency.toFixed(0)}ms` : 'N/A';
      const minLatency = latency.minLatency < Infinity ? `${latency.minLatency}ms` : 'N/A';
      const maxLatency = latency.maxLatency > 0 ? `${latency.maxLatency}ms` : 'N/A';

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
║  ├─ Cached prices:      ${String(this.priceCache.size).padEnd(8)}                                     ║
║  ├─ Messages received:  ${String(wsStats.messagesReceived).padEnd(8)}                                     ║
║  └─ Reconnects:         ${String(wsStats.reconnects).padEnd(8)}                                     ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  FAST-PATH ARBITRAGE (Real-time)                                             ║
║  ├─ Executions:       ${String(latency.totalExecutions).padEnd(8)}  Avg latency: ${avgLatency.padEnd(10)}            ║
║  ├─ Min latency:      ${minLatency.padEnd(8)}  Max latency: ${maxLatency.padEnd(10)}            ║
║  └─ Last 10 (ms):     [${latency.last10Latencies.join(', ').padEnd(40).slice(0, 40)}]  ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  OPPORTUNITIES DETECTED                                                      ║
║  ├─ Arbitrage:    ${String(opStats.by_type.arbitrage || 0).padEnd(8)}  ├─ Mispricing:   ${String(opStats.by_type.mispricing || 0).padEnd(8)}           ║
║  ├─ Wide Spread:  ${String(opStats.by_type.wide_spread || 0).padEnd(8)}  └─ Thin Book:   ${String(opStats.by_type.thin_book || 0).padEnd(8)}           ║
║  └─ Total:        ${String(opStats.total).padEnd(8)}                                             ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  PAPER TRADING (Market Making - 60s cycle)                                   ║
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

    // Fast-path arbitrage latency stats
    const latency = this.latencyMetrics;
    console.log('\nFast-Path Arbitrage:');
    console.log(`  Total executions: ${latency.totalExecutions}`);
    if (latency.totalExecutions > 0) {
      console.log(`  Avg latency: ${latency.avgTotalLatency.toFixed(1)}ms`);
      console.log(`  Min latency: ${latency.minLatency}ms`);
      console.log(`  Max latency: ${latency.maxLatency}ms`);
      console.log(`  Avg detection: ${latency.avgDetectionTime.toFixed(1)}ms`);
      console.log(`  Avg execution: ${latency.avgExecutionTime.toFixed(1)}ms`);
    }

    if (stats.paperTradingEnabled) {
      console.log(`\nPaper trades: ${stats.totalPaperTrades}`);
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

/**
 * Crypto Reactive Trader - Main Orchestrator
 *
 * Coordinates all components:
 * - Binance WebSocket price feeds
 * - Market discovery and tracking
 * - Mispricing detection
 * - Trade execution
 * - Exit monitoring
 * - Dashboard display
 */

import { EventEmitter } from 'events';
import {
  CryptoAsset,
  CryptoPrice,
  CryptoMarket,
  CryptoOpportunity,
  CryptoPosition,
  CryptoDashboardData,
  SignificantMoveEvent,
} from './cryptoTypes';
import { DEFAULT_CONFIG, DASHBOARD_CONFIG, getConfig } from './cryptoConfig';
import { BinanceWSClient, getBinanceWSClient, resetBinanceWSClient } from './binanceWS';
import {
  discoverCryptoMarkets,
  getTrackedMarkets,
  refreshMarketDiscovery,
} from './marketDiscovery';
import { detectMispricing, scanForOpportunities } from './mispricingDetector';
import { RiskManager, getRiskManager, resetRiskManager } from './riskManager';
import { CryptoTrader, getCryptoTrader, resetCryptoTrader } from './cryptoTrader';
import { ExitMonitor, getExitMonitor, resetExitMonitor } from './exitMonitor';
import { createClientFromEnv, PolymarketClient } from '../apiClient';
import * as cryptoRepo from '../database/cryptoRepo';

export class CryptoReactiveTrader extends EventEmitter {
  private binanceWS: BinanceWSClient;
  private riskManager: RiskManager;
  private trader: CryptoTrader;
  private exitMonitor: ExitMonitor;
  private polyClient: PolymarketClient;

  private isRunning: boolean = false;
  private discoveryInterval: NodeJS.Timeout | null = null;

  // Cache of tracked markets
  private trackedMarkets: Map<string, CryptoMarket> = new Map();

  // Cache of Polymarket prices (refreshed every 10 seconds)
  private polyPriceCache: Map<string, { yesPrice: number; noPrice: number; timestamp: number }> = new Map();
  private readonly POLY_PRICE_CACHE_TTL_MS = 10 * 1000; // 10 seconds
  private lastPolyPriceRefresh = 0;
  private isRefreshingPolyPrices = false;

  // Recent opportunities for dashboard
  private recentOpportunities: CryptoOpportunity[] = [];

  constructor() {
    super();

    this.binanceWS = getBinanceWSClient();
    this.riskManager = getRiskManager();
    this.trader = getCryptoTrader();
    this.exitMonitor = getExitMonitor();
    this.polyClient = createClientFromEnv();
  }

  /**
   * Initialize and start the crypto trader.
   */
  async start(): Promise<void> {
    if (this.isRunning) {
      console.log('[CRYPTO-TRADER] Already running');
      return;
    }

    console.log('\n====================================');
    console.log('  CRYPTO REACTIVE TRADER STARTING');
    console.log('====================================\n');

    try {
      // 1. Discover markets
      console.log('[STARTUP] Discovering crypto markets...');
      await discoverCryptoMarkets(this.polyClient);
      await this.refreshMarketCache();
      console.log(`[STARTUP] Tracking ${this.trackedMarkets.size} markets`);

      // 2. Initial Polymarket price fetch
      console.log('[STARTUP] Fetching initial Polymarket prices...');
      await this.triggerPolyPriceRefresh();
      console.log(`[STARTUP] Cached prices for ${this.polyPriceCache.size} markets`);

      // 2. Setup Polymarket price getter for exit monitor
      this.exitMonitor.setPolymarketPriceGetter(async (marketId: string) => {
        return this.getMarketPrices(marketId);
      });

      // 3. Connect to Binance
      console.log('[STARTUP] Connecting to Binance WebSocket...');
      await this.binanceWS.connect();

      // 4. Setup event handlers
      this.setupEventHandlers();

      // 5. Start exit monitor
      this.exitMonitor.start(1000);

      // 6. Start market discovery refresh interval
      this.startDiscoveryRefresh();

      this.isRunning = true;
      console.log('\n[CRYPTO-TRADER] Started successfully\n');

      this.emit('started');
    } catch (error) {
      console.error('[CRYPTO-TRADER] Startup failed:', error);
      throw error;
    }
  }

  /**
   * Stop the crypto trader.
   */
  async stop(): Promise<void> {
    if (!this.isRunning) return;

    console.log('\n[CRYPTO-TRADER] Stopping...');

    // Stop components
    this.binanceWS.disconnect();
    this.exitMonitor.stop();

    if (this.discoveryInterval) {
      clearInterval(this.discoveryInterval);
      this.discoveryInterval = null;
    }

    this.isRunning = false;
    console.log('[CRYPTO-TRADER] Stopped');

    this.emit('stopped');
  }

  /**
   * Check if trader is running.
   */
  isTraderRunning(): boolean {
    return this.isRunning;
  }

  /**
   * Get dashboard data.
   */
  async getDashboardData(): Promise<CryptoDashboardData> {
    const prices = this.binanceWS.getAllPrices();
    const positions = await this.trader.getOpenPositions();
    const riskState = this.riskManager.getState();
    const recentOpps = await cryptoRepo.getRecentOpportunities(
      DASHBOARD_CONFIG.maxRecentOpportunities
    );

    // Update market cache with current prices
    const marketsArray = Array.from(this.trackedMarkets.values());
    for (const market of marketsArray) {
      const cryptoPrice = prices.get(market.asset);
      if (cryptoPrice) {
        market.currentPolyPrice = undefined; // Would need to fetch
        // Calculate expected price
        const { calculateExpectedYesPrice } = await import('./mispricingDetector');
        market.expectedPrice = calculateExpectedYesPrice(
          cryptoPrice.price,
          market.threshold,
          market.direction
        );
      }
    }

    return {
      prices,
      trackedMarkets: marketsArray,
      activePositions: positions,
      riskState,
      recentOpportunities: recentOpps,
      isConnected: this.binanceWS.isConnected(),
      lastUpdate: new Date(),
    };
  }

  // ============================================================================
  // Private Methods
  // ============================================================================

  private setupEventHandlers(): void {
    // Handle price updates
    this.binanceWS.on('price', async (price: CryptoPrice) => {
      // Update exit monitor with latest price
      this.exitMonitor.updatePrice(price.asset, price.price);

      // Check for opportunities on every price update
      await this.checkOpportunities(price);
    });

    // Handle significant moves (>1% in 1 min)
    this.binanceWS.on('significantMove', async (event: SignificantMoveEvent) => {
      console.log(
        `\n[SIGNIFICANT MOVE] ${event.asset} moved ${(event.changePercent * 100).toFixed(2)}% ` +
          `($${event.previousPrice.toFixed(2)} → $${event.currentPrice.toFixed(2)})\n`
      );

      // Force check all markets for this asset
      const price: CryptoPrice = {
        asset: event.asset,
        price: event.currentPrice,
        timestamp: event.timestamp,
        change1m: event.changePercent,
        change5m: 0, // Not available in this context
      };

      await this.checkOpportunities(price, true);

      this.emit('significantMove', event);
    });

    // Handle connection events
    this.binanceWS.on('connected', () => {
      console.log('[BINANCE] Connected');
      this.emit('binanceConnected');
    });

    this.binanceWS.on('disconnected', (data: any) => {
      console.log('[BINANCE] Disconnected:', data);
      this.emit('binanceDisconnected', data);
    });

    this.binanceWS.on('error', (error: Error) => {
      console.error('[BINANCE] Error:', error);
      this.emit('error', error);
    });
  }

  private async checkOpportunities(
    price: CryptoPrice,
    forceCheck: boolean = false
  ): Promise<void> {
    // Get markets for this asset
    const markets = await cryptoRepo.getCryptoMarketsByAsset(price.asset);

    for (const market of markets) {
      try {
        // Get current Polymarket prices
        const polyPrices = await this.getMarketPrices(market.marketId);
        if (!polyPrices) continue;

        // Detect mispricing
        const result = detectMispricing(
          market,
          price,
          polyPrices.yesPrice,
          polyPrices.noPrice
        );

        if (result.hasOpportunity && result.opportunity) {
          // Store in recent opportunities
          this.recentOpportunities.unshift(result.opportunity);
          if (this.recentOpportunities.length > DASHBOARD_CONFIG.maxRecentOpportunities) {
            this.recentOpportunities.pop();
          }

          // Emit opportunity event
          this.emit('opportunity', result.opportunity);

          // Try to execute trade
          const tradeResult = await this.trader.executeTrade(
            result.opportunity,
            market.volume24h
          );

          if (tradeResult.success && tradeResult.position) {
            this.emit('positionOpened', tradeResult.position);
          }
        }
      } catch (error) {
        console.error(`[CRYPTO-TRADER] Error checking market ${market.marketId}:`, error);
      }
    }
  }

  private async getMarketPrices(
    marketId: string
  ): Promise<{ yesPrice: number; noPrice: number } | null> {
    // Check cache first
    const cached = this.polyPriceCache.get(marketId);
    if (cached && Date.now() - cached.timestamp < this.POLY_PRICE_CACHE_TTL_MS) {
      return { yesPrice: cached.yesPrice, noPrice: cached.noPrice };
    }

    // Trigger a batch refresh if needed (don't block)
    this.triggerPolyPriceRefresh();

    // Return stale cache if available, null otherwise
    if (cached) {
      return { yesPrice: cached.yesPrice, noPrice: cached.noPrice };
    }
    return null;
  }

  /**
   * Batch refresh all Polymarket prices (runs every 10 seconds max)
   */
  private async triggerPolyPriceRefresh(): Promise<void> {
    const now = Date.now();

    // Skip if recently refreshed or already refreshing
    if (this.isRefreshingPolyPrices || now - this.lastPolyPriceRefresh < this.POLY_PRICE_CACHE_TTL_MS) {
      return;
    }

    this.isRefreshingPolyPrices = true;
    this.lastPolyPriceRefresh = now;

    try {
      // Fetch all markets once
      const gammaMarkets = await this.polyClient.getAllMarkets(true, 1000, 0);

      // Update cache for all tracked markets
      for (const [marketId, market] of this.trackedMarkets) {
        const gammaMarket = gammaMarkets.find((m) => m.id === marketId);
        if (!gammaMarket) continue;

        try {
          const marketData = await this.polyClient.buildMarketData(gammaMarket);
          if (marketData) {
            const yesPrice = marketData.yesToken?.bestAsk?.price || 0.5;
            const noPrice = marketData.noToken?.bestAsk?.price || 0.5;

            this.polyPriceCache.set(marketId, {
              yesPrice,
              noPrice,
              timestamp: now,
            });
          }
        } catch (err) {
          // Skip individual market errors
        }
      }

      console.log(`[CRYPTO-TRADER] Refreshed Polymarket prices for ${this.polyPriceCache.size} markets`);
    } catch (error) {
      console.error('[CRYPTO-TRADER] Error refreshing Polymarket prices:', error);
    } finally {
      this.isRefreshingPolyPrices = false;
    }
  }

  private async refreshMarketCache(): Promise<void> {
    const markets = await getTrackedMarkets();
    this.trackedMarkets.clear();
    for (const market of markets) {
      this.trackedMarkets.set(market.marketId, market);
    }
  }

  private startDiscoveryRefresh(): void {
    const intervalMs = DEFAULT_CONFIG.discoveryIntervalMinutes * 60 * 1000;

    this.discoveryInterval = setInterval(async () => {
      console.log('[CRYPTO-TRADER] Refreshing market discovery...');
      await refreshMarketDiscovery();
      await this.refreshMarketCache();
      await this.exitMonitor.refreshMarketCache();
    }, intervalMs);
  }
}

// ============================================================================
// Factory and Cleanup Functions
// ============================================================================

let traderInstance: CryptoReactiveTrader | null = null;

export function getCryptoReactiveTrader(): CryptoReactiveTrader {
  if (!traderInstance) {
    traderInstance = new CryptoReactiveTrader();
  }
  return traderInstance;
}

export function resetCryptoReactiveTrader(): void {
  if (traderInstance) {
    traderInstance.stop();
  }
  traderInstance = null;

  // Reset all singletons
  resetBinanceWSClient();
  resetRiskManager();
  resetCryptoTrader();
  resetExitMonitor();
}

// ============================================================================
// Re-exports for convenience
// ============================================================================

export { BinanceWSClient, getBinanceWSClient } from './binanceWS';
export { RiskManager, getRiskManager } from './riskManager';
export { CryptoTrader, getCryptoTrader } from './cryptoTrader';
export { ExitMonitor, getExitMonitor } from './exitMonitor';
export * from './cryptoTypes';
export * from './cryptoConfig';
export * from './marketDiscovery';
export * from './mispricingDetector';

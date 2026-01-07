/**
 * WebSocket-based market scanner for real-time Polymarket data.
 * Replaces REST polling with WebSocket subscriptions for low-latency updates.
 */

import WebSocket from 'ws';
import { EventEmitter } from 'events';
import {
  MarketData,
  MarketSnapshot,
  TokenData,
  OrderBookLevel,
  calculateVolumeDistribution,
  calculateSpreadDistribution,
  createEmptyVolumeDistribution,
  createEmptySpreadDistribution,
} from './types';
import { PolymarketClient, createClientFromEnv } from './apiClient';

// Polymarket WebSocket endpoint for market data
const WS_MARKET_URL = 'wss://ws-subscriptions-clob.polymarket.com/ws/market';

// WebSocket message types
interface WSBookMessage {
  event_type: 'book';
  asset_id: string;
  market: string;
  timestamp: string;
  bids: Array<{ price: string; size: string }>;
  asks: Array<{ price: string; size: string }>;
  hash: string;
}

interface WSPriceChangeMessage {
  event_type: 'price_change';
  asset_id: string;
  market?: string;
  price: string;
  size: string;
  side: 'BUY' | 'SELL';
  timestamp: string;
  // New format (Sept 2025+)
  price_changes?: Array<{
    asset_id: string;
    best_bid: string;
    best_ask: string;
  }>;
}

interface WSLastTradePriceMessage {
  event_type: 'last_trade_price';
  asset_id: string;
  market?: string;
  price: string;
  size: string;
  side: 'BUY' | 'SELL';
  timestamp: string;
  fee_rate_bps?: string;
}

interface WSTickSizeChangeMessage {
  event_type: 'tick_size_change';
  asset_id: string;
  old_tick_size: string;
  new_tick_size: string;
  timestamp: string;
}

type WSMessage = WSBookMessage | WSPriceChangeMessage | WSLastTradePriceMessage | WSTickSizeChangeMessage;

export interface AssetInfo {
  tokenId: string;
  marketId: string;
  outcome: 'YES' | 'NO';
  question: string;
  category: string;
  volume24h: number;
}

export interface WSPriceUpdate {
  assetId: string;
  marketId: string;
  outcome: 'YES' | 'NO';
  bestBid: OrderBookLevel | null;
  bestAsk: OrderBookLevel | null;
  spread: number;
  spreadPct: number;
  timestamp: Date;
}

export interface WSScannerConfig {
  maxSubscriptions?: number;      // Max markets to subscribe to (default: 100)
  reconnectIntervalMs?: number;   // Reconnect delay (default: 5000)
  heartbeatIntervalMs?: number;   // Ping interval (default: 30000)
  staleDataThresholdMs?: number;  // Consider data stale after (default: 60000)
  minVolume?: number;             // Minimum 24h volume for subscription
}

export type PriceUpdateCallback = (updates: WSPriceUpdate[]) => void;

const DEFAULT_CONFIG: Required<WSScannerConfig> = {
  maxSubscriptions: 100,
  reconnectIntervalMs: 5000,
  heartbeatIntervalMs: 30000,
  staleDataThresholdMs: 60000,
  minVolume: 10000,
};

export class WSMarketScanner extends EventEmitter {
  private config: Required<WSScannerConfig>;
  private ws: WebSocket | null = null;
  private client: PolymarketClient;

  // Asset tracking
  private subscribedAssets: Map<string, AssetInfo> = new Map();  // assetId -> info
  private marketAssets: Map<string, Set<string>> = new Map();    // marketId -> assetIds

  // Price data cache
  private priceCache: Map<string, WSPriceUpdate> = new Map();    // assetId -> latest price

  // Connection state
  private isConnected: boolean = false;
  private isConnecting: boolean = false;
  private shouldReconnect: boolean = false;
  private reconnectTimer: NodeJS.Timeout | null = null;
  private heartbeatTimer: NodeJS.Timeout | null = null;
  private lastPongTime: number = 0;

  // Stats
  private stats = {
    messagesReceived: 0,
    priceUpdates: 0,
    reconnects: 0,
    errors: 0,
    lastMessageTime: null as Date | null,
  };

  constructor(config: WSScannerConfig = {}) {
    super();
    this.config = { ...DEFAULT_CONFIG, ...config };
    this.client = createClientFromEnv();
    console.log(`[WS] Scanner initialized with max ${this.config.maxSubscriptions} subscriptions`);
  }

  /**
   * Start the WebSocket scanner.
   * Fetches top markets and establishes WebSocket connection.
   */
  async start(): Promise<void> {
    console.log('[WS] Starting WebSocket scanner...');
    this.shouldReconnect = true;

    // Fetch initial market list
    await this.refreshSubscriptions();

    // Connect to WebSocket
    await this.connect();
  }

  /**
   * Stop the WebSocket scanner.
   */
  stop(): void {
    console.log('[WS] Stopping WebSocket scanner...');
    this.shouldReconnect = false;
    this.disconnect();
  }

  /**
   * Refresh market subscriptions (call periodically to update market list).
   */
  async refreshSubscriptions(): Promise<void> {
    console.log('[WS] Refreshing market subscriptions...');

    try {
      // Fetch top markets by volume
      const markets = await this.client.getAllMarkets(
        true,
        this.config.maxSubscriptions * 2,  // Fetch more to filter
        this.config.minVolume
      );

      // Build market data to get token IDs
      const newAssets = new Map<string, AssetInfo>();
      const newMarketAssets = new Map<string, Set<string>>();

      let processedCount = 0;

      for (const rawMarket of markets) {
        if (processedCount >= this.config.maxSubscriptions) break;

        const marketData = await this.client.buildMarketData(rawMarket);
        if (!marketData) continue;

        const assetSet = new Set<string>();

        // Add YES token
        if (marketData.yesToken?.tokenId) {
          newAssets.set(marketData.yesToken.tokenId, {
            tokenId: marketData.yesToken.tokenId,
            marketId: marketData.marketId,
            outcome: 'YES',
            question: marketData.question,
            category: marketData.category,
            volume24h: marketData.volume24h,
          });
          assetSet.add(marketData.yesToken.tokenId);
        }

        // Add NO token
        if (marketData.noToken?.tokenId) {
          newAssets.set(marketData.noToken.tokenId, {
            tokenId: marketData.noToken.tokenId,
            marketId: marketData.marketId,
            outcome: 'NO',
            question: marketData.question,
            category: marketData.category,
            volume24h: marketData.volume24h,
          });
          assetSet.add(marketData.noToken.tokenId);
        }

        if (assetSet.size > 0) {
          newMarketAssets.set(marketData.marketId, assetSet);
          processedCount++;
        }
      }

      // Update subscription maps
      const oldAssetIds = new Set(this.subscribedAssets.keys());
      const newAssetIds = new Set(newAssets.keys());

      // Find assets to add and remove
      const toAdd = [...newAssetIds].filter(id => !oldAssetIds.has(id));
      const toRemove = [...oldAssetIds].filter(id => !newAssetIds.has(id));

      this.subscribedAssets = newAssets;
      this.marketAssets = newMarketAssets;

      console.log(`[WS] Subscriptions updated: ${newAssets.size} assets across ${newMarketAssets.size} markets`);
      console.log(`[WS] Added ${toAdd.length}, removed ${toRemove.length} subscriptions`);

      // If connected, update subscriptions
      if (this.isConnected && this.ws) {
        if (toRemove.length > 0) {
          this.sendUnsubscribe(toRemove);
        }
        if (toAdd.length > 0) {
          this.sendSubscribe(toAdd);
        }
      }
    } catch (error) {
      console.error('[WS] Error refreshing subscriptions:', error);
      this.stats.errors++;
    }
  }

  /**
   * Connect to WebSocket server.
   */
  private async connect(): Promise<void> {
    if (this.isConnecting || this.isConnected) {
      return;
    }

    this.isConnecting = true;
    console.log('[WS] Connecting to', WS_MARKET_URL);

    return new Promise((resolve, reject) => {
      try {
        this.ws = new WebSocket(WS_MARKET_URL);

        this.ws.on('open', () => {
          console.log('[WS] Connected successfully');
          this.isConnected = true;
          this.isConnecting = false;
          this.lastPongTime = Date.now();

          // Subscribe to all assets
          const assetIds = [...this.subscribedAssets.keys()];
          if (assetIds.length > 0) {
            this.sendSubscribe(assetIds);
          }

          // Start heartbeat
          this.startHeartbeat();

          this.emit('connected');
          resolve();
        });

        this.ws.on('message', (data: Buffer) => {
          this.handleMessage(data);
        });

        this.ws.on('pong', () => {
          this.lastPongTime = Date.now();
        });

        this.ws.on('close', (code, reason) => {
          console.log(`[WS] Connection closed: ${code} - ${reason.toString()}`);
          this.handleDisconnect();
        });

        this.ws.on('error', (error) => {
          console.error('[WS] Connection error:', error.message);
          this.stats.errors++;
          this.isConnecting = false;
          reject(error);
        });

      } catch (error) {
        this.isConnecting = false;
        reject(error);
      }
    });
  }

  /**
   * Disconnect from WebSocket server.
   */
  private disconnect(): void {
    this.stopHeartbeat();

    if (this.reconnectTimer) {
      clearTimeout(this.reconnectTimer);
      this.reconnectTimer = null;
    }

    if (this.ws) {
      this.ws.removeAllListeners();
      if (this.ws.readyState === WebSocket.OPEN) {
        this.ws.close();
      }
      this.ws = null;
    }

    this.isConnected = false;
    this.isConnecting = false;
  }

  /**
   * Handle disconnection and schedule reconnect.
   */
  private handleDisconnect(): void {
    this.isConnected = false;
    this.isConnecting = false;
    this.stopHeartbeat();

    this.emit('disconnected');

    if (this.shouldReconnect) {
      console.log(`[WS] Scheduling reconnect in ${this.config.reconnectIntervalMs}ms...`);
      this.stats.reconnects++;

      this.reconnectTimer = setTimeout(async () => {
        try {
          await this.connect();
        } catch (error) {
          console.error('[WS] Reconnect failed:', error);
          this.handleDisconnect();  // Schedule another reconnect
        }
      }, this.config.reconnectIntervalMs);
    }
  }

  /**
   * Send subscription message.
   */
  private sendSubscribe(assetIds: string[]): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      return;
    }

    // Polymarket expects subscription messages in specific format
    // Based on documentation: {"type": "subscribe", "assets_ids": [...]}
    const message = {
      type: 'subscribe',
      assets_ids: assetIds,
    };

    console.log(`[WS] Subscribing to ${assetIds.length} assets`);
    this.ws.send(JSON.stringify(message));
  }

  /**
   * Send unsubscribe message.
   */
  private sendUnsubscribe(assetIds: string[]): void {
    if (!this.ws || this.ws.readyState !== WebSocket.OPEN) {
      return;
    }

    const message = {
      type: 'unsubscribe',
      assets_ids: assetIds,
    };

    console.log(`[WS] Unsubscribing from ${assetIds.length} assets`);
    this.ws.send(JSON.stringify(message));
  }

  /**
   * Start heartbeat/ping mechanism.
   */
  private startHeartbeat(): void {
    this.stopHeartbeat();

    this.heartbeatTimer = setInterval(() => {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        // Check if we received a pong recently
        const timeSinceLastPong = Date.now() - this.lastPongTime;
        if (timeSinceLastPong > this.config.heartbeatIntervalMs * 2) {
          console.warn('[WS] No pong received, connection may be stale');
          this.ws.terminate();
          return;
        }

        this.ws.ping();
      }
    }, this.config.heartbeatIntervalMs);
  }

  /**
   * Stop heartbeat mechanism.
   */
  private stopHeartbeat(): void {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }

  /**
   * Handle incoming WebSocket message.
   */
  private handleMessage(data: Buffer): void {
    try {
      const message = JSON.parse(data.toString()) as WSMessage | WSMessage[];
      this.stats.messagesReceived++;
      this.stats.lastMessageTime = new Date();

      // Handle array of messages
      const messages = Array.isArray(message) ? message : [message];

      const priceUpdates: WSPriceUpdate[] = [];

      for (const msg of messages) {
        const update = this.processMessage(msg);
        if (update) {
          priceUpdates.push(update);
        }
      }

      if (priceUpdates.length > 0) {
        this.stats.priceUpdates += priceUpdates.length;
        this.emit('priceUpdate', priceUpdates);
      }

    } catch (error) {
      console.error('[WS] Error parsing message:', error);
      this.stats.errors++;
    }
  }

  /**
   * Process a single WebSocket message.
   */
  private processMessage(msg: WSMessage): WSPriceUpdate | null {
    const assetInfo = this.subscribedAssets.get(msg.asset_id);
    if (!assetInfo) {
      return null;  // Ignore messages for unsubscribed assets
    }

    switch (msg.event_type) {
      case 'book':
        return this.processBookMessage(msg, assetInfo);

      case 'price_change':
        return this.processPriceChangeMessage(msg, assetInfo);

      case 'last_trade_price':
        // Trade happened - we could emit a separate event for this
        this.emit('trade', {
          assetId: msg.asset_id,
          marketId: assetInfo.marketId,
          price: parseFloat(msg.price),
          size: parseFloat(msg.size),
          side: msg.side,
          timestamp: new Date(msg.timestamp),
        });
        return null;

      case 'tick_size_change':
        // Tick size changed - log but don't emit price update
        console.log(`[WS] Tick size changed for ${msg.asset_id}: ${msg.old_tick_size} -> ${msg.new_tick_size}`);
        return null;

      default:
        return null;
    }
  }

  /**
   * Process orderbook snapshot message.
   */
  private processBookMessage(msg: WSBookMessage, assetInfo: AssetInfo): WSPriceUpdate {
    let bestBid: OrderBookLevel | null = null;
    let bestAsk: OrderBookLevel | null = null;

    // Get best bid (highest)
    if (msg.bids && msg.bids.length > 0) {
      let maxBidPrice = -1;
      let maxBidOrder: { price: string; size: string } | null = null;

      for (const bid of msg.bids) {
        const price = parseFloat(bid.price);
        if (price > maxBidPrice) {
          maxBidPrice = price;
          maxBidOrder = bid;
        }
      }

      if (maxBidOrder) {
        bestBid = {
          price: parseFloat(maxBidOrder.price),
          size: parseFloat(maxBidOrder.size),
        };
      }
    }

    // Get best ask (lowest)
    if (msg.asks && msg.asks.length > 0) {
      let minAskPrice = Infinity;
      let minAskOrder: { price: string; size: string } | null = null;

      for (const ask of msg.asks) {
        const price = parseFloat(ask.price);
        if (price < minAskPrice) {
          minAskPrice = price;
          minAskOrder = ask;
        }
      }

      if (minAskOrder) {
        bestAsk = {
          price: parseFloat(minAskOrder.price),
          size: parseFloat(minAskOrder.size),
        };
      }
    }

    // Calculate spread
    let spread = 0;
    let spreadPct = 0;
    if (bestBid && bestAsk && bestBid.price > 0) {
      spread = bestAsk.price - bestBid.price;
      const midPrice = (bestBid.price + bestAsk.price) / 2;
      spreadPct = midPrice > 0 ? spread / midPrice : 0;
    }

    const update: WSPriceUpdate = {
      assetId: msg.asset_id,
      marketId: assetInfo.marketId,
      outcome: assetInfo.outcome,
      bestBid,
      bestAsk,
      spread,
      spreadPct,
      timestamp: new Date(msg.timestamp),
    };

    // Cache the update
    this.priceCache.set(msg.asset_id, update);

    return update;
  }

  /**
   * Process price change message.
   */
  private processPriceChangeMessage(msg: WSPriceChangeMessage, assetInfo: AssetInfo): WSPriceUpdate | null {
    // Get cached price data and update it
    let cached = this.priceCache.get(msg.asset_id);

    if (!cached) {
      cached = {
        assetId: msg.asset_id,
        marketId: assetInfo.marketId,
        outcome: assetInfo.outcome,
        bestBid: null,
        bestAsk: null,
        spread: 0,
        spreadPct: 0,
        timestamp: new Date(),
      };
    }

    // Handle new format (Sept 2025+) with price_changes array
    if (msg.price_changes && msg.price_changes.length > 0) {
      for (const change of msg.price_changes) {
        if (change.asset_id === msg.asset_id) {
          if (change.best_bid) {
            cached.bestBid = {
              price: parseFloat(change.best_bid),
              size: cached.bestBid?.size || 0,
            };
          }
          if (change.best_ask) {
            cached.bestAsk = {
              price: parseFloat(change.best_ask),
              size: cached.bestAsk?.size || 0,
            };
          }
        }
      }
    } else {
      // Old format - update based on side
      const price = parseFloat(msg.price);
      const size = parseFloat(msg.size);

      if (msg.side === 'BUY') {
        // Buy order - this affects bid side
        if (!cached.bestBid || price >= cached.bestBid.price) {
          cached.bestBid = { price, size };
        }
      } else {
        // Sell order - this affects ask side
        if (!cached.bestAsk || price <= cached.bestAsk.price) {
          cached.bestAsk = { price, size };
        }
      }
    }

    // Recalculate spread
    if (cached.bestBid && cached.bestAsk && cached.bestBid.price > 0) {
      cached.spread = cached.bestAsk.price - cached.bestBid.price;
      const midPrice = (cached.bestBid.price + cached.bestAsk.price) / 2;
      cached.spreadPct = midPrice > 0 ? cached.spread / midPrice : 0;
    }

    cached.timestamp = new Date(msg.timestamp);

    // Update cache
    this.priceCache.set(msg.asset_id, cached);

    return cached;
  }

  /**
   * Get current price data for a market.
   */
  getMarketPrices(marketId: string): { yes: WSPriceUpdate | null; no: WSPriceUpdate | null } {
    const assetIds = this.marketAssets.get(marketId);
    if (!assetIds) {
      return { yes: null, no: null };
    }

    let yes: WSPriceUpdate | null = null;
    let no: WSPriceUpdate | null = null;

    for (const assetId of assetIds) {
      const cached = this.priceCache.get(assetId);
      if (cached) {
        if (cached.outcome === 'YES') {
          yes = cached;
        } else {
          no = cached;
        }
      }
    }

    return { yes, no };
  }

  /**
   * Get all cached price updates.
   */
  getAllPrices(): WSPriceUpdate[] {
    return [...this.priceCache.values()];
  }

  /**
   * Build a MarketSnapshot from WebSocket data (for compatibility with existing code).
   */
  buildSnapshot(): MarketSnapshot {
    const marketDataMap = new Map<string, MarketData>();

    // Group price updates by market
    for (const [assetId, update] of this.priceCache) {
      const assetInfo = this.subscribedAssets.get(assetId);
      if (!assetInfo) continue;

      let marketData = marketDataMap.get(assetInfo.marketId);
      if (!marketData) {
        marketData = {
          marketId: assetInfo.marketId,
          conditionId: '',
          question: assetInfo.question,
          endDate: null,
          category: assetInfo.category,
          volume24h: assetInfo.volume24h,
          yesToken: null,
          noToken: null,
          yesNoSum: 0,
          totalLiquidityAtBest: 0,
          timeSinceLastTrade: null,
          createdAt: null,
          lastUpdated: update.timestamp,
          totalActiveMakers: 0,
          rawData: {},
        };
        marketDataMap.set(assetInfo.marketId, marketData);
      }

      const tokenData: TokenData = {
        tokenId: assetId,
        outcome: update.outcome,
        bestBid: update.bestBid,
        bestAsk: update.bestAsk,
        spread: update.spread,
        spreadPct: update.spreadPct,
        activeMakers: 0,
      };

      if (update.outcome === 'YES') {
        marketData.yesToken = tokenData;
      } else {
        marketData.noToken = tokenData;
      }

      // Update lastUpdated to most recent
      if (update.timestamp > marketData.lastUpdated) {
        marketData.lastUpdated = update.timestamp;
      }
    }

    // Calculate yesNoSum and liquidity for each market
    for (const marketData of marketDataMap.values()) {
      if (marketData.yesToken?.bestAsk && marketData.noToken?.bestAsk) {
        marketData.yesNoSum = marketData.yesToken.bestAsk.price + marketData.noToken.bestAsk.price;
      }

      let liquidity = 0;
      if (marketData.yesToken) {
        if (marketData.yesToken.bestBid) liquidity += marketData.yesToken.bestBid.size;
        if (marketData.yesToken.bestAsk) liquidity += marketData.yesToken.bestAsk.size;
      }
      if (marketData.noToken) {
        if (marketData.noToken.bestBid) liquidity += marketData.noToken.bestBid.size;
        if (marketData.noToken.bestAsk) liquidity += marketData.noToken.bestAsk.size;
      }
      marketData.totalLiquidityAtBest = liquidity;
    }

    const markets = [...marketDataMap.values()];

    // Calculate distributions
    const volumeDistribution = markets.length > 0
      ? calculateVolumeDistribution(markets)
      : createEmptyVolumeDistribution();
    const spreadDistribution = markets.length > 0
      ? calculateSpreadDistribution(markets)
      : createEmptySpreadDistribution();

    // Calculate average spread
    const spreads: number[] = [];
    for (const market of markets) {
      if (market.yesToken && market.yesToken.spreadPct > 0) {
        spreads.push(market.yesToken.spreadPct);
      }
      if (market.noToken && market.noToken.spreadPct > 0) {
        spreads.push(market.noToken.spreadPct);
      }
    }
    const avgSpread = spreads.length > 0
      ? spreads.reduce((a, b) => a + b, 0) / spreads.length
      : 0;

    return {
      timestamp: new Date(),
      markets,
      opportunities: [],  // Opportunities are detected separately
      volumeDistribution,
      spreadDistribution,
      totalMarkets: markets.length,
      totalVolume24h: markets.reduce((sum, m) => sum + m.volume24h, 0),
      avgSpread,
    };
  }

  /**
   * Get scanner statistics.
   */
  getStats(): {
    isConnected: boolean;
    subscribedAssets: number;
    subscribedMarkets: number;
    cachedPrices: number;
    messagesReceived: number;
    priceUpdates: number;
    reconnects: number;
    errors: number;
    lastMessageTime: Date | null;
  } {
    return {
      isConnected: this.isConnected,
      subscribedAssets: this.subscribedAssets.size,
      subscribedMarkets: this.marketAssets.size,
      cachedPrices: this.priceCache.size,
      ...this.stats,
    };
  }

  /**
   * Check if data is stale (no updates for too long).
   */
  isDataStale(): boolean {
    if (!this.stats.lastMessageTime) return true;
    return Date.now() - this.stats.lastMessageTime.getTime() > this.config.staleDataThresholdMs;
  }
}

/**
 * Create a WSMarketScanner using environment variables.
 */
export function createWSScannerFromEnv(): WSMarketScanner {
  const maxSubscriptions = parseInt(process.env.WS_MAX_SUBSCRIPTIONS || '100');
  const minVolume = parseFloat(process.env.MIN_VOLUME || '10000');
  const heartbeatIntervalMs = parseInt(process.env.WS_HEARTBEAT_MS || '30000');

  return new WSMarketScanner({
    maxSubscriptions,
    minVolume,
    heartbeatIntervalMs,
  });
}

/**
 * Binance WebSocket client for real-time crypto price tracking.
 *
 * Connects to Binance ticker streams for BTC, ETH, SOL and tracks:
 * - Current price
 * - 1-minute and 5-minute price changes
 * - Significant moves (>1% in 1 minute)
 */

import WebSocket from 'ws';
import { EventEmitter } from 'events';
import {
  CryptoAsset,
  CryptoPrice,
  BinanceTickerMessage,
  SignificantMoveEvent,
} from './cryptoTypes';
import { BINANCE_CONFIG } from './cryptoConfig';

interface PricePoint {
  price: number;
  timestamp: number;
}

export class BinanceWSClient extends EventEmitter {
  private ws: WebSocket | null = null;
  private reconnectAttempts = 0;
  private reconnectTimeout: NodeJS.Timeout | null = null;
  private heartbeatInterval: NodeJS.Timeout | null = null;
  private isConnecting = false;
  private shouldReconnect = true;

  // Current prices for each asset
  private prices: Map<CryptoAsset, number> = new Map();

  // Price history for change calculations
  // Only store 1 sample per second, max 300 points (5 minutes)
  private priceHistory: Map<CryptoAsset, PricePoint[]> = new Map();
  private lastSampleTime: Map<CryptoAsset, number> = new Map();
  private readonly HISTORY_DURATION_MS = 5 * 60 * 1000; // 5 minutes
  private readonly SAMPLE_INTERVAL_MS = 1000; // 1 sample per second
  private readonly MAX_HISTORY_POINTS = 300; // 5 min at 1/sec

  // Asset mapping from Binance symbols
  private readonly symbolToAsset: Map<string, CryptoAsset> = new Map([
    ['BTCUSDT', 'BTC'],
    ['ETHUSDT', 'ETH'],
    ['SOLUSDT', 'SOL'],
  ]);

  constructor() {
    super();
    // Initialize price history for each asset
    for (const asset of ['BTC', 'ETH', 'SOL'] as CryptoAsset[]) {
      this.priceHistory.set(asset, []);
    }
  }

  /**
   * Connect to Binance WebSocket and start receiving prices.
   */
  async connect(): Promise<void> {
    if (this.ws?.readyState === WebSocket.OPEN || this.isConnecting) {
      return;
    }

    this.isConnecting = true;
    this.shouldReconnect = true;

    return new Promise((resolve, reject) => {
      try {
        // Build combined stream URL
        const streams = Object.values(BINANCE_CONFIG.streams).join('/');
        const url = `${BINANCE_CONFIG.wsUrl}/${streams}`;

        console.log(`[BINANCE-WS] Connecting to ${url}`);
        this.ws = new WebSocket(url);

        this.ws.on('open', () => {
          console.log('[BINANCE-WS] Connected');
          this.isConnecting = false;
          this.reconnectAttempts = 0;
          this.startHeartbeat();
          this.emit('connected');
          resolve();
        });

        this.ws.on('message', (data: WebSocket.RawData) => {
          this.handleMessage(data);
        });

        this.ws.on('close', (code, reason) => {
          console.log(`[BINANCE-WS] Disconnected: ${code} - ${reason.toString()}`);
          this.isConnecting = false;
          this.stopHeartbeat();
          this.emit('disconnected', { code, reason: reason.toString() });

          if (this.shouldReconnect) {
            this.scheduleReconnect();
          }
        });

        this.ws.on('error', (error) => {
          console.error('[BINANCE-WS] Error:', error.message);
          this.isConnecting = false;
          this.emit('error', error);

          if (!this.ws || this.ws.readyState === WebSocket.CLOSED) {
            reject(error);
          }
        });

        // Timeout for initial connection
        setTimeout(() => {
          if (this.isConnecting) {
            this.isConnecting = false;
            reject(new Error('Connection timeout'));
          }
        }, 10000);
      } catch (error) {
        this.isConnecting = false;
        reject(error);
      }
    });
  }

  /**
   * Disconnect from Binance WebSocket.
   */
  disconnect(): void {
    this.shouldReconnect = false;
    this.stopHeartbeat();

    if (this.reconnectTimeout) {
      clearTimeout(this.reconnectTimeout);
      this.reconnectTimeout = null;
    }

    if (this.ws) {
      this.ws.close();
      this.ws = null;
    }

    console.log('[BINANCE-WS] Disconnected');
  }

  /**
   * Get current price for an asset.
   */
  getPrice(asset: CryptoAsset): number | undefined {
    return this.prices.get(asset);
  }

  /**
   * Get all current prices.
   */
  getAllPrices(): Map<CryptoAsset, CryptoPrice> {
    const result = new Map<CryptoAsset, CryptoPrice>();

    for (const [asset, price] of this.prices) {
      const change1m = this.calculateChange(asset, 1);
      const change5m = this.calculateChange(asset, 5);

      result.set(asset, {
        asset,
        price,
        timestamp: new Date(),
        change1m,
        change5m,
      });
    }

    return result;
  }

  /**
   * Check if connected to Binance.
   */
  isConnected(): boolean {
    return this.ws?.readyState === WebSocket.OPEN;
  }

  // ============================================================================
  // Private Methods
  // ============================================================================

  private handleMessage(data: WebSocket.RawData): void {
    try {
      const message = JSON.parse(data.toString());

      // Handle combined stream format
      if (message.stream && message.data) {
        this.processTickerMessage(message.data);
      } else if (message.e === '24hrTicker') {
        // Direct ticker message
        this.processTickerMessage(message);
      }
    } catch (error) {
      console.error('[BINANCE-WS] Error parsing message:', error);
    }
  }

  private processTickerMessage(ticker: BinanceTickerMessage): void {
    const asset = this.symbolToAsset.get(ticker.s);
    if (!asset) return;

    const price = parseFloat(ticker.c);
    const previousPrice = this.prices.get(asset);
    const now = Date.now();

    // Update current price
    this.prices.set(asset, price);

    // Add to history
    this.addToHistory(asset, price, now);

    // Calculate changes
    const change1m = this.calculateChange(asset, 1);
    const change5m = this.calculateChange(asset, 5);

    // Create price event
    const cryptoPrice: CryptoPrice = {
      asset,
      price,
      timestamp: new Date(now),
      change1m,
      change5m,
    };

    // Emit price update
    this.emit('price', cryptoPrice);

    // Check for significant move
    if (
      previousPrice &&
      Math.abs(change1m) >= BINANCE_CONFIG.significantMovePercent
    ) {
      const significantMove: SignificantMoveEvent = {
        asset,
        previousPrice,
        currentPrice: price,
        changePercent: change1m,
        timestamp: new Date(now),
      };

      console.log(
        `[BINANCE-WS] Significant move: ${asset} ${change1m > 0 ? '+' : ''}${(
          change1m * 100
        ).toFixed(2)}% ($${previousPrice.toFixed(2)} → $${price.toFixed(2)})`
      );

      this.emit('significantMove', significantMove);
    }
  }

  private addToHistory(asset: CryptoAsset, price: number, timestamp: number): void {
    const history = this.priceHistory.get(asset);
    if (!history) return;

    // Only sample once per second to avoid memory bloat
    const lastSample = this.lastSampleTime.get(asset) || 0;
    if (timestamp - lastSample < this.SAMPLE_INTERVAL_MS) {
      return; // Skip this tick, too soon after last sample
    }
    this.lastSampleTime.set(asset, timestamp);

    // Add new price point
    history.push({ price, timestamp });

    // Efficient cleanup: remove old entries in one operation
    const cutoff = timestamp - this.HISTORY_DURATION_MS;
    if (history.length > 0 && history[0].timestamp < cutoff) {
      // Find first index that's within our time window
      let firstValidIndex = 0;
      for (let i = 0; i < history.length; i++) {
        if (history[i].timestamp >= cutoff) {
          firstValidIndex = i;
          break;
        }
        firstValidIndex = i + 1;
      }
      // Remove all old entries at once (more efficient than multiple shifts)
      if (firstValidIndex > 0) {
        history.splice(0, firstValidIndex);
      }
    }

    // Hard cap to prevent unbounded growth
    if (history.length > this.MAX_HISTORY_POINTS) {
      history.splice(0, history.length - this.MAX_HISTORY_POINTS);
    }
  }

  private calculateChange(asset: CryptoAsset, minutes: number): number {
    const history = this.priceHistory.get(asset);
    if (!history || history.length === 0) return 0;

    const currentPrice = this.prices.get(asset);
    if (!currentPrice) return 0;

    const targetTime = Date.now() - minutes * 60 * 1000;

    // Find the price closest to target time
    let oldPrice: number | null = null;
    for (let i = 0; i < history.length; i++) {
      if (history[i].timestamp <= targetTime) {
        oldPrice = history[i].price;
      } else {
        break;
      }
    }

    // If no historical price, use oldest available
    if (oldPrice === null && history.length > 0) {
      oldPrice = history[0].price;
    }

    if (oldPrice === null || oldPrice === 0) return 0;

    return (currentPrice - oldPrice) / oldPrice;
  }

  private startHeartbeat(): void {
    this.stopHeartbeat();

    this.heartbeatInterval = setInterval(() => {
      if (this.ws?.readyState === WebSocket.OPEN) {
        this.ws.ping();
      }
    }, BINANCE_CONFIG.heartbeatIntervalMs);
  }

  private stopHeartbeat(): void {
    if (this.heartbeatInterval) {
      clearInterval(this.heartbeatInterval);
      this.heartbeatInterval = null;
    }
  }

  private scheduleReconnect(): void {
    if (this.reconnectAttempts >= BINANCE_CONFIG.maxReconnectAttempts) {
      console.error('[BINANCE-WS] Max reconnect attempts reached');
      this.emit('maxReconnectsReached');
      return;
    }

    const delay = BINANCE_CONFIG.reconnectDelayMs * Math.pow(1.5, this.reconnectAttempts);
    this.reconnectAttempts++;

    console.log(
      `[BINANCE-WS] Reconnecting in ${delay / 1000}s (attempt ${this.reconnectAttempts}/${BINANCE_CONFIG.maxReconnectAttempts})`
    );

    this.reconnectTimeout = setTimeout(async () => {
      try {
        await this.connect();
      } catch (error) {
        console.error('[BINANCE-WS] Reconnection failed:', error);
      }
    }, delay);
  }
}

// ============================================================================
// Singleton Instance
// ============================================================================

let instance: BinanceWSClient | null = null;

export function getBinanceWSClient(): BinanceWSClient {
  if (!instance) {
    instance = new BinanceWSClient();
  }
  return instance;
}

export function resetBinanceWSClient(): void {
  if (instance) {
    instance.disconnect();
    instance = null;
  }
}

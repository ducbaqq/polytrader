/**
 * WebSocket Price Provider for noPaperTrader.
 * Wraps WSMarketScanner to provide real-time prices. Uses singleton pattern.
 */

import { WSMarketScanner, WSScannerConfig, WSPriceUpdate } from '../wsScanner';

export interface WSProviderStats {
  connected: boolean;
  cachedPrices: number;
  subscribedAssets: number;
  subscribedMarkets: number;
  lastUpdate: Date | null;
  messagesReceived: number;
  reconnects: number;
}

export class WSPriceProvider {
  private wsScanner: WSMarketScanner;
  private lastUpdateTimes: Map<string, Date> = new Map();
  private started: boolean = false;

  constructor(config?: WSScannerConfig) {
    this.wsScanner = new WSMarketScanner(config);
    this.wsScanner.on('priceUpdate', (updates: WSPriceUpdate[]) => {
      const now = new Date();
      for (const update of updates) {
        this.lastUpdateTimes.set(update.assetId, now);
      }
    });
  }

  async start(): Promise<void> {
    if (this.started) return;
    console.log('[WSProvider] Starting...');
    await this.wsScanner.start();
    this.started = true;
  }

  stop(): void {
    if (!this.started) return;
    console.log('[WSProvider] Stopping...');
    this.wsScanner.stop();
    this.started = false;
    this.lastUpdateTimes.clear();
  }

  isConnected(): boolean {
    return this.started && this.wsScanner.getStats().isConnected;
  }

  /** Get cached price for a token (no API call) */
  getPrice(tokenId: string): WSPriceUpdate | null {
    return this.wsScanner.getAllPrices().find(p => p.assetId === tokenId) || null;
  }

  /** Get cached prices for a market (both YES and NO tokens) */
  getMarketPrices(marketId: string): { yes: WSPriceUpdate | null; no: WSPriceUpdate | null } {
    return this.wsScanner.getMarketPrices(marketId);
  }

  /** Check if data for a token is fresh (default: 60 seconds) */
  isDataFresh(tokenId: string, maxAgeMs: number = 60000): boolean {
    const lastUpdate = this.lastUpdateTimes.get(tokenId);
    if (lastUpdate) {
      return Date.now() - lastUpdate.getTime() < maxAgeMs;
    }
    const cached = this.getPrice(tokenId);
    if (!cached) return false;
    return Date.now() - cached.timestamp.getTime() < maxAgeMs;
  }

  getStats(): WSProviderStats {
    const s = this.wsScanner.getStats();
    return {
      connected: s.isConnected,
      cachedPrices: s.cachedPrices,
      subscribedAssets: s.subscribedAssets,
      subscribedMarkets: s.subscribedMarkets,
      lastUpdate: s.lastMessageTime,
      messagesReceived: s.messagesReceived,
      reconnects: s.reconnects,
    };
  }
}

// Singleton
let instance: WSPriceProvider | null = null;

export function getWSProvider(): WSPriceProvider | null {
  return instance;
}

export async function initWSProvider(config?: WSScannerConfig): Promise<WSPriceProvider> {
  if (instance) return instance;
  instance = new WSPriceProvider(config);
  await instance.start();
  return instance;
}

export function stopWSProvider(): void {
  if (instance) {
    instance.stop();
    instance = null;
  }
}

export { WSPriceUpdate, WSScannerConfig };

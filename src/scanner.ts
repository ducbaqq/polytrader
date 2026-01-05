/**
 * Market scanner module for continuous market data collection.
 */

import {
  MarketData,
  MarketSnapshot,
  ScannerStats,
  calculateVolumeDistribution,
  calculateSpreadDistribution,
  createEmptyVolumeDistribution,
  createEmptySpreadDistribution,
} from './types';
import { PolymarketClient, createClientFromEnv } from './apiClient';

interface VolumeHistory {
  marketId: string;
  hourlyVolumes: number[];
  timestamps: Date[];
  maxHistoryHours: number;
}

function createVolumeHistory(marketId: string): VolumeHistory {
  return {
    marketId,
    hourlyVolumes: [],
    timestamps: [],
    maxHistoryHours: 24,
  };
}

function addVolumeSample(history: VolumeHistory, volume: number, timestamp?: Date): void {
  const ts = timestamp || new Date();
  history.hourlyVolumes.push(volume);
  history.timestamps.push(ts);

  // Trim old data
  const cutoff = new Date(Date.now() - history.maxHistoryHours * 60 * 60 * 1000);
  while (history.timestamps.length > 0 && history.timestamps[0] < cutoff) {
    history.timestamps.shift();
    history.hourlyVolumes.shift();
  }
}

function get1hAverage(history: VolumeHistory): number {
  if (history.hourlyVolumes.length === 0) return 0;

  const oneHourAgo = new Date(Date.now() - 60 * 60 * 1000);
  const recentVolumes = history.hourlyVolumes.filter(
    (_, i) => history.timestamps[i] >= oneHourAgo
  );

  return recentVolumes.length > 0
    ? recentVolumes.reduce((a, b) => a + b, 0) / recentVolumes.length
    : 0;
}

export type ScanCompleteCallback = (snapshot: MarketSnapshot) => void;

export interface MarketScannerConfig {
  client?: PolymarketClient;
  scanInterval?: number;
  maxMarkets?: number;
  minVolume?: number;
  onScanComplete?: ScanCompleteCallback;
}

export class MarketScanner {
  private client: PolymarketClient;
  private scanInterval: number;
  private maxMarkets?: number;
  private minVolume: number;
  private onScanComplete?: ScanCompleteCallback;

  private currentSnapshot: MarketSnapshot | null = null;
  private markets: Map<string, MarketData> = new Map();
  private volumeHistory: Map<string, VolumeHistory> = new Map();

  private totalScans: number = 0;
  private failedScans: number = 0;
  private lastScanDuration: number = 0;
  private lastScanTime: Date | null = null;

  private intervalId: NodeJS.Timeout | null = null;
  private isRunning: boolean = false;

  constructor(config: MarketScannerConfig = {}) {
    this.client = config.client || createClientFromEnv();
    this.scanInterval = config.scanInterval || 30;
    this.maxMarkets = config.maxMarkets;
    this.minVolume = config.minVolume || 0;
    this.onScanComplete = config.onScanComplete;

    console.log(`MarketScanner initialized with ${this.scanInterval}s interval`);
  }

  getCurrentSnapshot(): MarketSnapshot | null {
    return this.currentSnapshot;
  }

  getMarkets(): Map<string, MarketData> {
    return new Map(this.markets);
  }

  getVolumeHistory(marketId: string): VolumeHistory {
    if (!this.volumeHistory.has(marketId)) {
      this.volumeHistory.set(marketId, createVolumeHistory(marketId));
    }
    return this.volumeHistory.get(marketId)!;
  }

  get1hVolumeAverage(marketId: string): number {
    return get1hAverage(this.getVolumeHistory(marketId));
  }

  async scanOnce(): Promise<MarketSnapshot | null> {
    const startTime = Date.now();
    console.log('Starting market scan...');

    try {
      // Fetch all markets with filters
      const rawMarkets = await this.client.getAllMarkets(
        true,
        this.maxMarkets,
        this.minVolume
      );

      if (!rawMarkets || rawMarkets.length === 0) {
        console.warn('No markets returned from API');
        return null;
      }

      // Process each market
      const processedMarkets: MarketData[] = [];
      let failedCount = 0;

      for (let i = 0; i < rawMarkets.length; i++) {
        try {
          const marketData = await this.client.buildMarketData(rawMarkets[i]);

          if (marketData) {
            processedMarkets.push(marketData);

            // Update volume history
            this.markets.set(marketData.marketId, marketData);
            const history = this.getVolumeHistory(marketData.marketId);
            addVolumeSample(history, marketData.volume24h);
          }
        } catch (error) {
          failedCount++;
          continue;
        }

        // Log progress every 50 markets
        if ((i + 1) % 50 === 0) {
          console.log(`Processed ${i + 1}/${rawMarkets.length} markets`);
        }
      }

      // Calculate distributions
      const volumeDistribution = calculateVolumeDistribution(processedMarkets);
      const spreadDistribution = calculateSpreadDistribution(processedMarkets);

      // Calculate average spread
      const spreads: number[] = [];
      for (const market of processedMarkets) {
        if (market.yesToken && market.yesToken.spreadPct > 0) {
          spreads.push(market.yesToken.spreadPct);
        }
        if (market.noToken && market.noToken.spreadPct > 0) {
          spreads.push(market.noToken.spreadPct);
        }
      }
      const avgSpread =
        spreads.length > 0 ? spreads.reduce((a, b) => a + b, 0) / spreads.length : 0;

      // Create snapshot
      const snapshot: MarketSnapshot = {
        timestamp: new Date(),
        markets: processedMarkets,
        opportunities: [],
        volumeDistribution,
        spreadDistribution,
        totalMarkets: processedMarkets.length,
        totalVolume24h: processedMarkets.reduce((sum, m) => sum + m.volume24h, 0),
        avgSpread,
      };

      // Update internal state
      this.currentSnapshot = snapshot;
      this.totalScans++;
      this.lastScanDuration = (Date.now() - startTime) / 1000;
      this.lastScanTime = new Date();

      console.log(
        `Scan complete: ${processedMarkets.length} markets processed ` +
          `(${failedCount} failed) in ${this.lastScanDuration.toFixed(1)}s`
      );

      return snapshot;
    } catch (error) {
      console.error('Scan failed:', error);
      this.failedScans++;
      return null;
    }
  }

  private async scanLoop(): Promise<void> {
    while (this.isRunning) {
      try {
        const snapshot = await this.scanOnce();

        if (snapshot && this.onScanComplete) {
          try {
            this.onScanComplete(snapshot);
          } catch (error) {
            console.error('Callback error:', error);
          }
        }
      } catch (error) {
        console.error('Scan loop error:', error);
      }

      // Wait for next scan
      await new Promise((resolve) => setTimeout(resolve, this.scanInterval * 1000));
    }
  }

  start(): void {
    if (this.isRunning) {
      console.warn('Scanner already running');
      return;
    }

    this.isRunning = true;
    console.log('Scanner started');
    this.scanLoop();
  }

  stop(): void {
    console.log('Stopping scanner...');
    this.isRunning = false;
    console.log('Scanner stopped');
  }

  getStats(): ScannerStats {
    return {
      totalScans: this.totalScans,
      failedScans: this.failedScans,
      lastScanDuration: this.lastScanDuration,
      lastScanTime: this.lastScanTime,
      marketsTracked: this.markets.size,
      isRunning: this.isRunning,
    };
  }
}

/**
 * Create a MarketScanner using environment variables.
 */
export function createScannerFromEnv(onScanComplete?: ScanCompleteCallback): MarketScanner {
  const scanInterval = parseFloat(process.env.SCAN_INTERVAL_SECONDS || '30');
  const maxMarketsStr = process.env.MAX_MARKETS || '';
  const maxMarkets = maxMarketsStr ? parseInt(maxMarketsStr) : undefined;
  const minVolume = parseFloat(process.env.MIN_VOLUME || '1000');

  return new MarketScanner({
    scanInterval,
    maxMarkets,
    minVolume,
    onScanComplete,
  });
}

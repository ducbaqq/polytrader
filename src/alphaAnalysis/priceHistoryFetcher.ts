/**
 * Fetch price history from CLOB API with rate limiting
 */

import axios, { AxiosInstance } from 'axios';
import {
  ClobPriceHistoryResponse,
  ClobPricePoint,
  AlphaAnalysisConfig,
  DEFAULT_CONFIG,
  DataQualityTier,
  MarketPrices,
} from './types';
import { RateLimiter, withRetry, ConcurrentRateLimiter, BatchProgress } from '../utils';

const CLOB_API_URL = 'https://clob.polymarket.com';

// ============================================================================
// Price History Fetcher
// ============================================================================

export interface PriceHistoryResult {
  tokenId: string;
  history: ClobPricePoint[];
  tier: DataQualityTier;
  success: boolean;
  error?: string;
}

export interface BatchFetchRequest {
  tokenId: string;
  volumeNum: number;
}

export class PriceHistoryFetcher {
  private rateLimiter: RateLimiter;
  private axiosInstance: AxiosInstance;
  private config: AlphaAnalysisConfig;

  constructor(config: AlphaAnalysisConfig = DEFAULT_CONFIG) {
    this.config = config;
    this.rateLimiter = new RateLimiter(config.rateLimit.callsPerSecond);
    this.axiosInstance = axios.create({
      baseURL: CLOB_API_URL,
      timeout: 30000,
      headers: { 'Content-Type': 'application/json' },
    });
  }

  getTier(volumeNum: number): DataQualityTier {
    if (volumeNum >= this.config.volumeTiers.tier1MinVolume) {
      return 'tier1';
    }
    if (volumeNum >= this.config.volumeTiers.tier2MinVolume) {
      return 'tier2';
    }
    return 'tier3';
  }

  private async fetchPriceHistory(
    tokenId: string,
    tier: 'tier1' | 'tier2',
    useRateLimiter: boolean = true
  ): Promise<PriceHistoryResult> {
    if (useRateLimiter) {
      await this.rateLimiter.wait();
    }

    const params = tier === 'tier1'
      ? { market: tokenId, interval: '1d', fidelity: 30 }
      : { market: tokenId, interval: 'max', fidelity: 2 };

    try {
      const response = await withRetry(
        () => this.axiosInstance.get('/prices-history', { params }),
        this.config.rateLimit.maxRetries,
        this.config.rateLimit.baseDelayMs
      );

      const data = response.data as ClobPriceHistoryResponse;
      return {
        tokenId,
        history: data.history || [],
        tier,
        success: true,
      };
    } catch (error: unknown) {
      return {
        tokenId,
        history: [],
        tier,
        success: false,
        error: (error as Error).message,
      };
    }
  }

  async fetchFullHistory(tokenId: string): Promise<PriceHistoryResult> {
    return this.fetchPriceHistory(tokenId, 'tier1', true);
  }

  async fetchKeyPoints(tokenId: string): Promise<PriceHistoryResult> {
    return this.fetchPriceHistory(tokenId, 'tier2', true);
  }

  async fetchForTier(tokenId: string, volumeNum: number): Promise<PriceHistoryResult> {
    const tier = this.getTier(volumeNum);

    if (tier === 'tier3') {
      return { tokenId, history: [], tier: 'tier3', success: true };
    }

    return this.fetchPriceHistory(tokenId, tier, true);
  }

  calculatePrices(history: ClobPricePoint[], finalPriceFromGamma: number): MarketPrices {
    if (history.length === 0) {
      return {
        openingNoPrice: null,
        finalNoPrice: finalPriceFromGamma,
        avgNoPrice: null,
        minNoPrice: null,
        maxNoPrice: null,
      };
    }

    const sorted = [...history].sort((a, b) => a.t - b.t);
    const prices = sorted.map((p) => parseFloat(p.p));

    const openingNoPrice = prices[0];
    const finalNoPrice = prices[prices.length - 1] || finalPriceFromGamma;
    const avgNoPrice = prices.reduce((a, b) => a + b, 0) / prices.length;
    const minNoPrice = Math.min(...prices);
    const maxNoPrice = Math.max(...prices);

    return {
      openingNoPrice,
      finalNoPrice,
      avgNoPrice,
      minNoPrice,
      maxNoPrice,
    };
  }

  /**
   * Fetch price histories for multiple tokens in parallel with rate limiting
   */
  async fetchBatch(
    requests: BatchFetchRequest[],
    concurrency: number = 10,
    onProgress?: (progress: BatchProgress) => void
  ): Promise<Map<string, PriceHistoryResult>> {
    const results = new Map<string, PriceHistoryResult>();

    if (requests.length === 0) {
      return results;
    }

    // Separate tier3 requests (no API call needed) from API requests
    const apiRequests: Array<{ request: BatchFetchRequest; tier: 'tier1' | 'tier2' }> = [];

    for (const request of requests) {
      const tier = this.getTier(request.volumeNum);
      if (tier === 'tier3') {
        results.set(request.tokenId, {
          tokenId: request.tokenId,
          history: [],
          tier: 'tier3',
          success: true,
        });
      } else {
        apiRequests.push({ request, tier });
      }
    }

    if (apiRequests.length === 0) {
      return results;
    }

    const concurrentLimiter = new ConcurrentRateLimiter({
      maxConcurrent: concurrency,
    });

    const tasks = apiRequests.map(({ request, tier }) => async (): Promise<void> => {
      const result = await this.fetchPriceHistory(request.tokenId, tier, false);
      results.set(request.tokenId, result);
    });

    await concurrentLimiter.executeInChunks(tasks, 5000, onProgress);

    return results;
  }
}

export default PriceHistoryFetcher;

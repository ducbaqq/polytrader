/**
 * Polymarket API client with rate limiting and retry logic.
 */

import axios, { AxiosInstance, AxiosError } from 'axios';
import {
  MarketData,
  TokenData,
  OrderBookLevel,
  GammaMarket,
  OrderBookResponse,
} from './types';

const GAMMA_API_URL = 'https://gamma-api.polymarket.com';
const CLOB_API_URL = 'https://clob.polymarket.com';

/**
 * Simple rate limiter to prevent API throttling.
 */
class RateLimiter {
  private minInterval: number;
  private lastCallTime: number = 0;

  constructor(callsPerSecond: number = 5.0) {
    this.minInterval = 1000 / callsPerSecond; // Convert to ms
  }

  async wait(): Promise<void> {
    const elapsed = Date.now() - this.lastCallTime;
    if (elapsed < this.minInterval) {
      await new Promise((resolve) => setTimeout(resolve, this.minInterval - elapsed));
    }
    this.lastCallTime = Date.now();
  }
}

/**
 * Sleep utility for retry delays.
 */
function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

/**
 * Retry wrapper with exponential backoff.
 * Does not retry on 404 errors (expected for missing order books).
 */
async function withRetry<T>(
  fn: () => Promise<T>,
  maxRetries: number = 3,
  baseDelay: number = 1000
): Promise<T> {
  let lastError: Error | null = null;

  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      return await fn();
    } catch (error: any) {
      lastError = error as Error;

      // Don't retry on 404 errors - they're expected for tokens without order books
      if (error?.response?.status === 404) {
        throw error;
      }

      if (attempt < maxRetries) {
        const delay = baseDelay * Math.pow(2, attempt);
        console.warn(
          `Request failed (attempt ${attempt + 1}/${maxRetries + 1}): ${lastError.message}. Retrying in ${delay}ms...`
        );
        await sleep(delay);
      }
    }
  }

  throw lastError;
}

export interface PolymarketClientConfig {
  privateKey?: string;
  apiKey?: string;
  apiSecret?: string;
  apiPassphrase?: string;
  callsPerSecond?: number;
}

export class PolymarketClient {
  private rateLimiter: RateLimiter;
  private axiosInstance: AxiosInstance;
  private config: PolymarketClientConfig;

  constructor(config: PolymarketClientConfig = {}) {
    this.config = config;
    this.rateLimiter = new RateLimiter(config.callsPerSecond || 5.0);

    this.axiosInstance = axios.create({
      timeout: 30000,
      headers: {
        'Content-Type': 'application/json',
      },
    });

    console.log('PolymarketClient initialized');
  }

  /**
   * Make a GET request with rate limiting and retry logic.
   */
  private async get<T>(url: string, params?: Record<string, any>): Promise<T> {
    await this.rateLimiter.wait();
    return withRetry(async () => {
      const response = await this.axiosInstance.get<T>(url, { params });
      return response.data;
    });
  }

  /**
   * Fetch all markets from the Gamma API.
   */
  async getAllMarkets(
    activeOnly: boolean = true,
    maxMarkets?: number,
    minVolume: number = 0
  ): Promise<GammaMarket[]> {
    const allMarkets: GammaMarket[] = [];
    let offset = 0;
    const limit = 500;

    while (true) {
      const params: Record<string, any> = {
        limit,
        offset,
      };

      if (activeOnly) {
        params.closed = 'false';
        params.active = 'true';
      }

      try {
        const response = await this.get<GammaMarket[] | { data: GammaMarket[] }>(
          `${GAMMA_API_URL}/markets`,
          params
        );

        const markets = Array.isArray(response) ? response : response.data || [];

        // Filter by minimum volume if specified
        const filteredMarkets =
          minVolume > 0
            ? markets.filter((m) => (m.volume24hr || 0) >= minVolume)
            : markets;

        allMarkets.push(...filteredMarkets);

        // Check if we've reached max_markets limit
        if (maxMarkets && allMarkets.length >= maxMarkets) {
          return allMarkets.slice(0, maxMarkets);
        }

        // If we got fewer markets than the limit, we've reached the end
        if (markets.length < limit) {
          break;
        }

        offset += limit;
      } catch (error) {
        console.error(`Error fetching markets page at offset ${offset}:`, error);
        break;
      }
    }

    console.log(`Fetched ${allMarkets.length} markets from Gamma API`);
    return allMarkets;
  }

  /**
   * Fetch order book for a specific token.
   */
  async getOrderBook(tokenId: string): Promise<OrderBookResponse | null> {
    try {
      const response = await this.get<OrderBookResponse>(`${CLOB_API_URL}/book`, {
        token_id: tokenId,
      });
      return response;
    } catch (error: any) {
      // 404 errors are expected for tokens without order books - handle silently
      if (error?.response?.status === 404) {
        return null;
      }
      console.error(`Error fetching order book for token ${tokenId}:`, error);
      return null;
    }
  }

  /**
   * Build a complete MarketData object from raw API response.
   */
  async buildMarketData(rawMarket: GammaMarket): Promise<MarketData | null> {
    try {
      const marketId = rawMarket.id || '';
      const conditionId = rawMarket.conditionId || '';
      const question = rawMarket.question || 'Unknown';
      const category = rawMarket.category || 'Unknown';

      // Parse dates
      let endDate: Date | null = null;
      if (rawMarket.endDate) {
        try {
          endDate = new Date(rawMarket.endDate);
        } catch {
          // ignore
        }
      }

      let createdAt: Date | null = null;
      if (rawMarket.createdAt) {
        try {
          createdAt = new Date(rawMarket.createdAt);
        } catch {
          // ignore
        }
      }

      // Parse clobTokenIds - it's a JSON string like "[\"id1\", \"id2\"]"
      let clobTokenIds: string[] = [];
      if (rawMarket.clobTokenIds) {
        if (typeof rawMarket.clobTokenIds === 'string') {
          try {
            clobTokenIds = JSON.parse(rawMarket.clobTokenIds);
          } catch {
            clobTokenIds = [];
          }
        } else if (Array.isArray(rawMarket.clobTokenIds)) {
          clobTokenIds = rawMarket.clobTokenIds;
        }
      }

      // Parse outcomes - it's a JSON string like "[\"Yes\", \"No\"]"
      let outcomes: string[] = ['Yes', 'No'];
      if (rawMarket.outcomes) {
        if (typeof rawMarket.outcomes === 'string') {
          try {
            outcomes = JSON.parse(rawMarket.outcomes);
          } catch {
            outcomes = ['Yes', 'No'];
          }
        } else if (Array.isArray(rawMarket.outcomes)) {
          outcomes = rawMarket.outcomes;
        }
      }

      // Build tokens list
      let tokens: Array<{ token_id: string; outcome: string }> = rawMarket.tokens || [];
      if (tokens.length === 0 && clobTokenIds.length > 0) {
        tokens = clobTokenIds.map((tokenId, i) => ({
          token_id: tokenId,
          outcome: outcomes[i] || (i === 0 ? 'Yes' : 'No'),
        }));
      }

      let yesToken: TokenData | null = null;
      let noToken: TokenData | null = null;

      // Filter valid tokens first
      const validTokens = tokens.filter((t) => t.token_id);

      // Fetch all order books in parallel for better performance
      const orderBooks = await Promise.all(
        validTokens.map((token) => this.getOrderBook(token.token_id))
      );

      // Process each token with its order book
      for (let i = 0; i < validTokens.length; i++) {
        const token = validTokens[i];
        const tokenId = token.token_id;
        const outcome = (token.outcome || '').toUpperCase();
        const orderBook = orderBooks[i];

        let bestBid: OrderBookLevel | null = null;
        let bestAsk: OrderBookLevel | null = null;
        let activeMakers = 0;

        if (orderBook) {
          const bids = orderBook.bids || [];
          const asks = orderBook.asks || [];

          // Count unique makers (if available in response)
          const makerIds = new Set<string>();
          for (const order of [...bids, ...asks]) {
            const maker = (order as any).maker || (order as any).makerAddress;
            if (maker) makerIds.add(maker);
          }
          activeMakers = makerIds.size;

          // Get best bid (HIGHEST price)
          if (bids.length > 0) {
            let bestBidPrice = -1;
            let bestBidOrder: any = null;

            for (const bid of bids) {
              const price = parseFloat(String((bid as any).price || 0));
              if (price > bestBidPrice) {
                bestBidPrice = price;
                bestBidOrder = bid;
              }
            }

            if (bestBidOrder) {
              bestBid = {
                price: parseFloat(String(bestBidOrder.price)),
                size: parseFloat(String(bestBidOrder.size)),
              };
            }
          }

          // Get best ask (LOWEST price)
          if (asks.length > 0) {
            let bestAskPrice = Infinity;
            let bestAskOrder: any = null;

            for (const ask of asks) {
              const price = parseFloat(String((ask as any).price || 0));
              if (price < bestAskPrice) {
                bestAskPrice = price;
                bestAskOrder = ask;
              }
            }

            if (bestAskOrder) {
              bestAsk = {
                price: parseFloat(String(bestAskOrder.price)),
                size: parseFloat(String(bestAskOrder.size)),
              };
            }
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

        const tokenData: TokenData = {
          tokenId,
          outcome,
          bestBid,
          bestAsk,
          spread,
          spreadPct,
          activeMakers,
        };

        if (outcome === 'YES') {
          yesToken = tokenData;
        } else if (outcome === 'NO') {
          noToken = tokenData;
        }
      }

      // Calculate YES + NO sum using ASK prices (executable cost for arbitrage)
      // This is what you'd actually PAY to buy both sides
      let yesNoSum = 0;
      let minArbLiquidity = 0;
      if (yesToken && noToken) {
        // For arbitrage detection, we MUST use ask prices (what we pay to buy)
        // Only flag arbitrage if both ask prices exist
        if (yesToken.bestAsk && noToken.bestAsk) {
          yesNoSum = yesToken.bestAsk.price + noToken.bestAsk.price;
          // Minimum liquidity available for arbitrage (limited by smaller side)
          minArbLiquidity = Math.min(yesToken.bestAsk.size, noToken.bestAsk.size);
        } else {
          // Fallback for display purposes only (not valid for arbitrage)
          let yesPrice = 0;
          let noPrice = 0;

          if (yesToken.bestBid && yesToken.bestAsk) {
            yesPrice = (yesToken.bestBid.price + yesToken.bestAsk.price) / 2;
          } else if (yesToken.bestBid) {
            yesPrice = yesToken.bestBid.price;
          } else if (yesToken.bestAsk) {
            yesPrice = yesToken.bestAsk.price;
          }

          if (noToken.bestBid && noToken.bestAsk) {
            noPrice = (noToken.bestBid.price + noToken.bestAsk.price) / 2;
          } else if (noToken.bestBid) {
            noPrice = noToken.bestBid.price;
          } else if (noToken.bestAsk) {
            noPrice = noToken.bestAsk.price;
          }

          yesNoSum = yesPrice + noPrice;
        }
      }

      // Calculate total liquidity at best prices
      let totalLiquidity = 0;
      if (yesToken) {
        if (yesToken.bestBid) totalLiquidity += yesToken.bestBid.size;
        if (yesToken.bestAsk) totalLiquidity += yesToken.bestAsk.size;
      }
      if (noToken) {
        if (noToken.bestBid) totalLiquidity += noToken.bestBid.size;
        if (noToken.bestAsk) totalLiquidity += noToken.bestAsk.size;
      }

      // Total active makers
      let totalMakers = 0;
      if (yesToken) totalMakers += yesToken.activeMakers;
      if (noToken) totalMakers += noToken.activeMakers;

      // Get volume
      const volume24h =
        rawMarket.volume24hr ||
        parseFloat(rawMarket.volume || '0') ||
        rawMarket.volumeNum ||
        0;

      return {
        marketId,
        conditionId,
        question,
        endDate,
        category,
        volume24h,
        yesToken,
        noToken,
        yesNoSum,
        totalLiquidityAtBest: totalLiquidity,
        timeSinceLastTrade: null,
        createdAt,
        lastUpdated: new Date(),
        totalActiveMakers: totalMakers,
        rawData: rawMarket,
      };
    } catch (error) {
      console.error('Error building market data:', error);
      return null;
    }
  }
}

/**
 * Create a PolymarketClient using environment variables.
 */
export function createClientFromEnv(): PolymarketClient {
  return new PolymarketClient({
    privateKey: process.env.POLYMARKET_PRIVATE_KEY,
    apiKey: process.env.POLYMARKET_API_KEY,
    apiSecret: process.env.POLYMARKET_API_SECRET,
    apiPassphrase: process.env.POLYMARKET_API_PASSPHRASE,
    callsPerSecond: parseFloat(process.env.POLYMARKET_RATE_LIMIT || '5.0'),
  });
}

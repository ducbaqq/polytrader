/**
 * Export settled Polymarket markets to JSON.
 *
 * Usage:
 *   npx ts-node src/exportSettledMarkets.ts --months 2
 *   npm run export-markets -- --months 2
 */

import axios, { AxiosInstance } from 'axios';
import { Command } from 'commander';
import * as fs from 'fs';

const GAMMA_API_URL = 'https://gamma-api.polymarket.com';

// ============================================================================
// Types
// ============================================================================

interface GammaEvent {
  id: string;
  title: string;
  category?: string;
  tags?: { label: string }[];
}

interface GammaMarketResponse {
  id: string;
  question: string;
  slug: string;
  endDate: string;
  closed: boolean;
  active: boolean;
  archived: boolean;
  outcomes: string;          // JSON string: '["Yes","No"]'
  outcomePrices: string;     // JSON string: '["0.75","0.25"]'
  category?: string;         // Direct category field (older markets)
  tags?: { label: string }[];
  events?: GammaEvent[];     // Events array with category info
  resolvedBy?: string;
  resolutionSource?: string;
}

interface ExportedMarket {
  id: string;
  question: string;
  tags: string[];
  resolution: 'Yes' | 'No';
  resolvedAt: string;
}

interface ExportResult {
  exportDate: string;
  monthsBack: number;
  totalMarkets: number;
  markets: ExportedMarket[];
}

// ============================================================================
// Rate Limiter (from apiClient.ts pattern)
// ============================================================================

class RateLimiter {
  private minInterval: number;
  private lastCallTime: number = 0;

  constructor(callsPerSecond: number = 5.0) {
    this.minInterval = 1000 / callsPerSecond;
  }

  async wait(): Promise<void> {
    const elapsed = Date.now() - this.lastCallTime;
    if (elapsed < this.minInterval) {
      await new Promise((resolve) => setTimeout(resolve, this.minInterval - elapsed));
    }
    this.lastCallTime = Date.now();
  }
}

function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

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

// ============================================================================
// API Client
// ============================================================================

class SettledMarketsExporter {
  private rateLimiter: RateLimiter;
  private axiosInstance: AxiosInstance;

  constructor() {
    this.rateLimiter = new RateLimiter(5.0);
    this.axiosInstance = axios.create({
      baseURL: GAMMA_API_URL,
      timeout: 30000,
      headers: {
        'Content-Type': 'application/json',
      },
    });
  }

  /**
   * Fetch all closed markets with pagination.
   */
  async fetchClosedMarkets(startDate: Date, endDate: Date): Promise<GammaMarketResponse[]> {
    const allMarkets: GammaMarketResponse[] = [];
    const limit = 500;
    let offset = 0;
    let hasMore = true;

    console.log(`Fetching closed markets from ${startDate.toISOString().split('T')[0]} to ${endDate.toISOString().split('T')[0]}...`);

    while (hasMore) {
      await this.rateLimiter.wait();

      const markets = await withRetry(async () => {
        const response = await this.axiosInstance.get('/markets', {
          params: {
            closed: true,
            limit,
            offset,
            // Sort by end date descending to get most recent first
            order: 'endDate',
            ascending: false,
          },
        });
        return response.data as GammaMarketResponse[];
      });

      if (markets.length === 0) {
        hasMore = false;
        break;
      }

      // Filter by date range
      const startTimestamp = startDate.getTime();
      const endTimestamp = endDate.getTime();

      for (const market of markets) {
        const marketEndDate = new Date(market.endDate).getTime();

        // If market is before our start date, we can stop (sorted descending)
        if (marketEndDate < startTimestamp) {
          hasMore = false;
          break;
        }

        // Include if within date range
        if (marketEndDate >= startTimestamp && marketEndDate <= endTimestamp) {
          allMarkets.push(market);
        }
      }

      console.log(`  Fetched ${markets.length} markets (offset: ${offset}), total matching: ${allMarkets.length}`);

      offset += limit;

      // Safety check: if we got fewer than limit, we've reached the end
      if (markets.length < limit) {
        hasMore = false;
      }
    }

    return allMarkets;
  }

  /**
   * Check if a market is binary (Yes/No outcomes only).
   */
  private isBinaryMarket(market: GammaMarketResponse): boolean {
    try {
      const outcomes = JSON.parse(market.outcomes);
      if (!Array.isArray(outcomes) || outcomes.length !== 2) {
        return false;
      }
      const normalized = outcomes.map((o: string) => o.toLowerCase());
      return normalized.includes('yes') && normalized.includes('no');
    } catch {
      return false;
    }
  }

  /**
   * Determine the resolution of a binary market.
   */
  private getResolution(market: GammaMarketResponse): 'Yes' | 'No' | null {
    try {
      const outcomes = JSON.parse(market.outcomes) as string[];
      const prices = JSON.parse(market.outcomePrices) as string[];

      // Find which outcome has price close to 1.0 (resolved to that outcome)
      for (let i = 0; i < outcomes.length; i++) {
        const price = parseFloat(prices[i]);
        if (price >= 0.99) {
          const outcome = outcomes[i].toLowerCase();
          if (outcome === 'yes') return 'Yes';
          if (outcome === 'no') return 'No';
        }
      }

      return null;
    } catch {
      return null;
    }
  }

  /**
   * Extract categories/tags from market.
   * Priority: market.category > events[0].category > events[0].tags > 'Uncategorized'
   */
  private getTags(market: GammaMarketResponse): string[] {
    const categories: string[] = [];

    // 1. Check direct category field (older markets)
    if (market.category && typeof market.category === 'string') {
      categories.push(this.formatCategory(market.category));
    }

    // 2. Check events array for category
    if (market.events && Array.isArray(market.events)) {
      for (const event of market.events) {
        if (event.category && typeof event.category === 'string') {
          const formatted = this.formatCategory(event.category);
          if (!categories.includes(formatted)) {
            categories.push(formatted);
          }
        }
        // Also check event tags
        if (event.tags && Array.isArray(event.tags)) {
          for (const tag of event.tags) {
            if (tag.label && tag.label !== 'All') {
              if (!categories.includes(tag.label)) {
                categories.push(tag.label);
              }
            }
          }
        }
      }
    }

    // 3. Check market-level tags
    if (market.tags && Array.isArray(market.tags)) {
      for (const tag of market.tags) {
        if (tag.label && tag.label !== 'All') {
          if (!categories.includes(tag.label)) {
            categories.push(tag.label);
          }
        }
      }
    }

    // 4. Try to infer category from question keywords
    if (categories.length === 0) {
      const inferredCategory = this.inferCategory(market.question);
      if (inferredCategory) {
        categories.push(inferredCategory);
      }
    }

    return categories.length > 0 ? categories : ['Uncategorized'];
  }

  /**
   * Format category string (e.g., "US-current-affairs" -> "US Current Affairs")
   */
  private formatCategory(category: string): string {
    return category
      .split(/[-_]/)
      .map(word => word.charAt(0).toUpperCase() + word.slice(1).toLowerCase())
      .join(' ')
      .trim();
  }

  /**
   * Infer category from question text using keyword matching.
   */
  private inferCategory(question: string): string | null {
    const q = question.toLowerCase();

    // Crypto/Finance keywords
    if (/(bitcoin|btc|ethereum|eth|solana|crypto|token|blockchain|defi|nft)/i.test(q)) {
      return 'Crypto';
    }
    if (/(stock|nasdaq|s&p|dow|market|trading|price|above|below|nvda|aapl|tsla|msft|amzn|googl)/i.test(q)) {
      return 'Finance';
    }

    // Politics
    if (/(trump|biden|election|president|congress|senate|vote|political|democrat|republican|governor)/i.test(q)) {
      return 'Politics';
    }

    // Sports
    if (/(nfl|nba|mlb|nhl|soccer|football|basketball|baseball|hockey|game|win|score|playoffs|super bowl|championship)/i.test(q)) {
      return 'Sports';
    }

    // Tech
    if (/(ai|artificial intelligence|openai|chatgpt|google|apple|microsoft|tech|software|hardware|launch)/i.test(q)) {
      return 'Tech';
    }

    // Entertainment
    if (/(movie|film|tv|show|netflix|disney|spotify|youtube|streaming|box office|celebrity)/i.test(q)) {
      return 'Entertainment';
    }

    // Weather
    if (/(weather|temperature|hurricane|tornado|snow|rain|climate)/i.test(q)) {
      return 'Weather';
    }

    return null;
  }

  /**
   * Process and filter markets for export.
   */
  processMarkets(markets: GammaMarketResponse[]): ExportedMarket[] {
    const exportedMarkets: ExportedMarket[] = [];

    for (const market of markets) {
      // Skip non-binary markets
      if (!this.isBinaryMarket(market)) {
        continue;
      }

      // Get resolution
      const resolution = this.getResolution(market);
      if (!resolution) {
        continue;
      }

      exportedMarkets.push({
        id: market.id,
        question: market.question,
        tags: this.getTags(market),
        resolution,
        resolvedAt: market.endDate.split('T')[0], // Just the date part
      });
    }

    return exportedMarkets;
  }
}

// ============================================================================
// Main
// ============================================================================

async function main() {
  const program = new Command();

  program
    .name('export-markets')
    .description('Export settled Polymarket markets to JSON')
    .requiredOption('-m, --months <number>', 'Number of months back to fetch', parseInt)
    .option('-o, --output <path>', 'Output file path', 'settled_markets.json')
    .parse(process.argv);

  const options = program.opts<{ months: number; output: string }>();

  if (isNaN(options.months) || options.months < 1) {
    console.error('Error: --months must be a positive integer');
    process.exit(1);
  }

  console.log(`\n=== Polymarket Settled Markets Export ===`);
  console.log(`Months back: ${options.months}`);
  console.log(`Output file: ${options.output}\n`);

  const exporter = new SettledMarketsExporter();

  // Calculate date range
  const endDate = new Date();
  const startDate = new Date();
  startDate.setMonth(startDate.getMonth() - options.months);

  // Fetch markets
  const rawMarkets = await exporter.fetchClosedMarkets(startDate, endDate);
  console.log(`\nFetched ${rawMarkets.length} total closed markets in date range`);

  // Process and filter
  const exportedMarkets = exporter.processMarkets(rawMarkets);
  console.log(`Filtered to ${exportedMarkets.length} binary markets with clear resolutions`);

  // Build export result
  const result: ExportResult = {
    exportDate: new Date().toISOString().split('T')[0],
    monthsBack: options.months,
    totalMarkets: exportedMarkets.length,
    markets: exportedMarkets,
  };

  // Count resolutions
  const yesCount = exportedMarkets.filter(m => m.resolution === 'Yes').length;
  const noCount = exportedMarkets.filter(m => m.resolution === 'No').length;
  console.log(`\nResolutions: ${yesCount} Yes, ${noCount} No`);

  // Count categories
  const categoryCount = new Map<string, number>();
  for (const market of exportedMarkets) {
    for (const tag of market.tags) {
      categoryCount.set(tag, (categoryCount.get(tag) || 0) + 1);
    }
  }
  console.log(`\nCategories (${categoryCount.size} total):`);
  const sortedCategories = [...categoryCount.entries()].sort((a, b) => b[1] - a[1]);
  for (const [category, count] of sortedCategories.slice(0, 10)) {
    console.log(`  ${category}: ${count}`);
  }
  if (sortedCategories.length > 10) {
    console.log(`  ... and ${sortedCategories.length - 10} more`);
  }

  // Write to file
  fs.writeFileSync(options.output, JSON.stringify(result, null, 2));
  console.log(`\nExported to ${options.output}`);
}

main().catch((error) => {
  console.error('Error:', error.message);
  process.exit(1);
});

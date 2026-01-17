/**
 * Export settled Polymarket markets to JSON.
 *
 * Usage:
 *   npx ts-node src/exportSettledMarkets.ts --period 5d
 *   npx ts-node src/exportSettledMarkets.ts --period 2m
 *   npm run export-markets -- --period 5d
 *
 * Period format:
 *   - Days: 1d, 5d, 14d
 *   - Months: 1m, 3m, 6m
 */

import axios, { AxiosInstance } from 'axios';
import { Command } from 'commander';
import * as fs from 'fs';

import { RateLimiter, withRetry, parsePeriod, extractTags } from './utils';

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
  outcomes: string;
  outcomePrices: string;
  category?: string;
  tags?: { label: string }[];
  events?: GammaEvent[];
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
  period: string;
  periodDays: number;
  totalMarkets: number;
  markets: ExportedMarket[];
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
      headers: { 'Content-Type': 'application/json' },
    });
  }

  async fetchClosedMarkets(startDate: Date, endDate: Date): Promise<GammaMarketResponse[]> {
    const allMarkets: GammaMarketResponse[] = [];
    const limit = 500;
    let offset = 0;
    let hasMore = true;

    console.log(
      `Fetching closed markets from ${startDate.toISOString().split('T')[0]} to ${endDate.toISOString().split('T')[0]}...`
    );

    const startTimestamp = startDate.getTime();
    const endTimestamp = endDate.getTime();

    while (hasMore) {
      await this.rateLimiter.wait();

      const markets = await withRetry(async () => {
        const response = await this.axiosInstance.get('/markets', {
          params: {
            closed: true,
            limit,
            offset,
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

      for (const market of markets) {
        const marketEndDate = new Date(market.endDate).getTime();

        if (marketEndDate < startTimestamp) {
          hasMore = false;
          break;
        }

        if (marketEndDate >= startTimestamp && marketEndDate <= endTimestamp) {
          allMarkets.push(market);
        }
      }

      console.log(`  Fetched ${markets.length} markets (offset: ${offset}), total matching: ${allMarkets.length}`);
      offset += limit;

      if (markets.length < limit) {
        hasMore = false;
      }
    }

    return allMarkets;
  }

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

  private getResolution(market: GammaMarketResponse): 'Yes' | 'No' | null {
    try {
      const outcomes = JSON.parse(market.outcomes) as string[];
      const prices = JSON.parse(market.outcomePrices) as string[];

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

  private getTags(market: GammaMarketResponse): string[] {
    return extractTags(market, market.question);
  }

  processMarkets(markets: GammaMarketResponse[]): ExportedMarket[] {
    const exportedMarkets: ExportedMarket[] = [];

    for (const market of markets) {
      if (!this.isBinaryMarket(market)) {
        continue;
      }

      const resolution = this.getResolution(market);
      if (!resolution) {
        continue;
      }

      exportedMarkets.push({
        id: market.id,
        question: market.question,
        tags: this.getTags(market),
        resolution,
        resolvedAt: market.endDate.split('T')[0],
      });
    }

    return exportedMarkets;
  }
}

// ============================================================================
// Main
// ============================================================================

async function main(): Promise<void> {
  const program = new Command();

  program
    .name('export-markets')
    .description('Export settled Polymarket markets to JSON')
    .option('-p, --period <period>', 'Time period (e.g., 5d, 1m, 3m)')
    .option('-m, --months <number>', '[DEPRECATED] Use --period instead', parseInt)
    .option('-o, --output <path>', 'Output file path', 'settled_markets.json')
    .parse(process.argv);

  const options = program.opts<{ period?: string; months?: number; output: string }>();

  // Parse period from arguments
  let periodDays: number;
  let periodString: string;

  if (options.period) {
    try {
      const parsed = parsePeriod(options.period);
      periodDays = parsed.days;
      periodString = parsed.original;
    } catch (err: unknown) {
      const error = err as Error;
      console.error(`Error: ${error.message}`);
      process.exit(1);
    }
  } else if (options.months) {
    if (isNaN(options.months) || options.months < 1) {
      console.error('Error: --months must be a positive integer');
      process.exit(1);
    }
    console.warn('Warning: --months is deprecated. Use --period instead (e.g., --period 2m)');
    periodDays = options.months * 30;
    periodString = `${options.months}m`;
  } else {
    console.error('Error: --period is required (e.g., --period 5d or --period 2m)');
    process.exit(1);
  }

  console.log(`\n=== Polymarket Settled Markets Export ===`);
  console.log(`Period: ${periodString} (${periodDays} days)`);
  console.log(`Output file: ${options.output}\n`);

  const exporter = new SettledMarketsExporter();

  const endDate = new Date();
  const startDate = new Date();
  startDate.setDate(startDate.getDate() - periodDays);

  const rawMarkets = await exporter.fetchClosedMarkets(startDate, endDate);
  console.log(`\nFetched ${rawMarkets.length} total closed markets in date range`);

  const exportedMarkets = exporter.processMarkets(rawMarkets);
  console.log(`Filtered to ${exportedMarkets.length} binary markets with clear resolutions`);

  const result: ExportResult = {
    exportDate: new Date().toISOString().split('T')[0],
    period: periodString,
    periodDays,
    totalMarkets: exportedMarkets.length,
    markets: exportedMarkets,
  };

  const yesCount = exportedMarkets.filter((m) => m.resolution === 'Yes').length;
  const noCount = exportedMarkets.filter((m) => m.resolution === 'No').length;
  console.log(`\nResolutions: ${yesCount} Yes, ${noCount} No`);

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

  fs.writeFileSync(options.output, JSON.stringify(result, null, 2));
  console.log(`\nExported to ${options.output}`);
}

main().catch((error: Error) => {
  console.error('Error:', error.message);
  process.exit(1);
});

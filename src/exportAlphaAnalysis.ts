/**
 * Alpha Analysis Pipeline for Polymarket Trading Edge
 *
 * Fetches settled markets, calculates edge metrics, and generates statistical analysis.
 *
 * Usage:
 *   npx ts-node src/exportAlphaAnalysis.ts --period 5d
 *   npx ts-node src/exportAlphaAnalysis.ts --period 2m
 *   npx ts-node src/exportAlphaAnalysis.ts --period 2m --concurrency 20
 *   npm run alpha-analysis -- --period 5d
 *   npm run alpha-analysis -- --period 2m --concurrency 15
 */

import axios, { AxiosInstance } from 'axios';
import { Command } from 'commander';
import * as fs from 'fs';

import {
  GammaMarketExtended,
  AlphaMarket,
  AlphaAnalysisOutput,
  AlphaSummaryOutput,
  AlphaAnalysisConfig,
  DEFAULT_CONFIG,
  MarketPrices,
  DataQualityTier,
} from './alphaAnalysis/types.js';
import { PriceHistoryFetcher, BatchFetchRequest } from './alphaAnalysis/priceHistoryFetcher.js';
import { EdgeCalculator } from './alphaAnalysis/edgeCalculator.js';
import { Aggregator } from './alphaAnalysis/aggregator.js';
import { RateLimiter, withRetry, parsePeriod, extractTags } from './utils/index.js';

const GAMMA_API_URL = 'https://gamma-api.polymarket.com';
const CLOB_API_URL = 'https://clob.polymarket.com';

// ============================================================================
// Alpha Analysis Exporter
// ============================================================================

class AlphaAnalysisExporter {
  private rateLimiter: RateLimiter;
  private axiosInstance: AxiosInstance;
  private config: AlphaAnalysisConfig;
  private priceHistoryFetcher: PriceHistoryFetcher;
  private edgeCalculator: EdgeCalculator;
  private aggregator: Aggregator;

  constructor(config: AlphaAnalysisConfig = DEFAULT_CONFIG) {
    this.config = config;
    this.rateLimiter = new RateLimiter(5.0);
    this.axiosInstance = axios.create({
      baseURL: GAMMA_API_URL,
      timeout: 30000,
      headers: { 'Content-Type': 'application/json' },
    });
    this.priceHistoryFetcher = new PriceHistoryFetcher(config);
    this.edgeCalculator = new EdgeCalculator(config);
    this.aggregator = new Aggregator(config);
  }

  async fetchClosedMarkets(startDate: Date, endDate: Date): Promise<GammaMarketExtended[]> {
    const allMarkets: GammaMarketExtended[] = [];
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
        return response.data as GammaMarketExtended[];
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

  private isBinaryMarket(market: GammaMarketExtended): boolean {
    try {
      const outcomes = JSON.parse(market.outcomes) as string[];
      if (outcomes.length !== 2) return false;

      const normalized = outcomes.map((o) => o.toLowerCase());
      return normalized.includes('yes') && normalized.includes('no');
    } catch {
      return false;
    }
  }

  private getResolution(market: GammaMarketExtended): 'Yes' | 'No' | null {
    try {
      const outcomes = JSON.parse(market.outcomes) as string[];
      const prices = JSON.parse(market.outcomePrices) as string[];

      for (let i = 0; i < outcomes.length; i++) {
        if (parseFloat(prices[i]) >= 0.99) {
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

  private getFinalNoPrice(market: GammaMarketExtended): number | null {
    try {
      const outcomes = JSON.parse(market.outcomes) as string[];
      const prices = JSON.parse(market.outcomePrices) as string[];

      const noIndex = outcomes.findIndex((o) => o.toLowerCase() === 'no');
      return noIndex >= 0 ? parseFloat(prices[noIndex]) : null;
    } catch {
      return null;
    }
  }

  private parseTokenIds(market: GammaMarketExtended): { yesTokenId: string | null; noTokenId: string | null } {
    if (!market.clobTokenIds) {
      return { yesTokenId: null, noTokenId: null };
    }

    try {
      const tokenIds = JSON.parse(market.clobTokenIds) as string[];
      const outcomes = JSON.parse(market.outcomes) as string[];

      let yesTokenId: string | null = null;
      let noTokenId: string | null = null;

      for (let i = 0; i < outcomes.length; i++) {
        const outcomeLower = outcomes[i].toLowerCase();
        if (outcomeLower === 'yes') {
          yesTokenId = tokenIds[i] || null;
        } else if (outcomeLower === 'no') {
          noTokenId = tokenIds[i] || null;
        }
      }
      return { yesTokenId, noTokenId };
    } catch {
      return { yesTokenId: null, noTokenId: null };
    }
  }

  private getTags(market: GammaMarketExtended): string[] {
    return extractTags(market, market.question);
  }

  private calculateDuration(createdAt: string | null, resolvedAt: string): number | null {
    if (!createdAt) return null;

    const created = new Date(createdAt).getTime();
    const resolved = new Date(resolvedAt).getTime();
    const days = (resolved - created) / (1000 * 60 * 60 * 24);
    return days >= 0 ? days : null;
  }

  /**
   * Process markets with parallel price history fetching
   *
   * Three-phase processing for optimal performance:
   * 1. Filter phase (sync): Identify valid binary markets
   * 2. Fetch phase (parallel): Batch fetch all price histories with controlled concurrency
   * 3. Transform phase (sync): Build final AlphaMarket objects
   *
   * @param markets Raw markets from Gamma API
   * @param fetchPriceHistory Whether to fetch price history from CLOB API
   * @param concurrency Number of concurrent API requests (default: 10)
   * @returns Array of processed AlphaMarket objects
   */
  async processMarkets(
    markets: GammaMarketExtended[],
    fetchPriceHistory: boolean = true,
    concurrency: number = 10
  ): Promise<AlphaMarket[]> {
    console.log(`\nProcessing ${markets.length} markets (concurrency: ${concurrency})...`);

    // ========================================================================
    // Phase 1: Filter - Identify valid binary markets
    // ========================================================================
    console.log('\n--- Phase 1: Filtering valid binary markets ---');

    interface ValidatedMarket {
      market: GammaMarketExtended;
      resolution: 'Yes' | 'No';
      finalNoPrice: number;
      yesTokenId: string | null;
      noTokenId: string | null;
      volumeNum: number;
      liquidityNum: number;
      tier: DataQualityTier;
    }

    const validMarkets: ValidatedMarket[] = [];
    let skipped = 0;

    for (const market of markets) {
      if (!this.isBinaryMarket(market)) {
        skipped++;
        continue;
      }

      const resolution = this.getResolution(market);
      if (!resolution) {
        skipped++;
        continue;
      }

      const finalNoPrice = this.getFinalNoPrice(market);
      if (finalNoPrice === null) {
        skipped++;
        continue;
      }

      const { yesTokenId, noTokenId } = this.parseTokenIds(market);
      const volumeNum = market.volumeNum ?? (parseFloat(market.volume || '0') || 0);
      const liquidityNum = market.liquidityNum ?? (parseFloat(market.liquidity || '0') || 0);
      const tier = this.priceHistoryFetcher.getTier(volumeNum);

      validMarkets.push({
        market,
        resolution,
        finalNoPrice,
        yesTokenId,
        noTokenId,
        volumeNum,
        liquidityNum,
        tier,
      });
    }

    console.log(`  Found ${validMarkets.length} valid binary markets (skipped ${skipped} non-binary/invalid)`);

    // ========================================================================
    // Phase 2: Fetch - Batch fetch all price histories in parallel
    // ========================================================================
    console.log('\n--- Phase 2: Fetching price histories ---');

    const priceHistoryMap = new Map<string, { history: { t: number; p: string }[]; success: boolean }>();

    if (fetchPriceHistory) {
      // Build batch requests for markets that need API calls
      const batchRequests: BatchFetchRequest[] = [];
      for (const vm of validMarkets) {
        if (vm.noTokenId && vm.tier !== 'tier3') {
          batchRequests.push({
            tokenId: vm.noTokenId,
            volumeNum: vm.volumeNum,
          });
        }
      }

      console.log(`  ${batchRequests.length} markets need price history API calls`);
      console.log(`  ${validMarkets.length - batchRequests.length} markets skipped (tier3 or no tokenId)`);

      if (batchRequests.length > 0) {
        const startTime = Date.now();
        let lastLogTime = startTime;

        const results = await this.priceHistoryFetcher.fetchBatch(
          batchRequests,
          concurrency,
          (progress) => {
            const now = Date.now();
            // Log progress every 5 seconds
            if (now - lastLogTime >= 5000 || progress.completed === progress.total) {
              const pct = ((progress.completed / progress.total) * 100).toFixed(1);
              const rate = progress.ratePerSecond.toFixed(1);
              const eta = ((progress.total - progress.completed) / progress.ratePerSecond).toFixed(0);
              console.log(`  Progress: ${progress.completed}/${progress.total} (${pct}%) @ ${rate}/sec, ETA: ${eta}s`);
              lastLogTime = now;
            }
          }
        );

        // Copy results to our map
        for (const [tokenId, result] of results) {
          priceHistoryMap.set(tokenId, {
            history: result.history,
            success: result.success,
          });
        }

        const elapsed = ((Date.now() - startTime) / 1000).toFixed(1);
        const rate = (batchRequests.length / parseFloat(elapsed)).toFixed(1);
        console.log(`  Completed ${batchRequests.length} API calls in ${elapsed}s (${rate}/sec)`);
      }
    } else {
      console.log('  Skipping price history fetch (--no-price-history flag)');
    }

    // ========================================================================
    // Phase 3: Transform - Build final AlphaMarket objects
    // ========================================================================
    console.log('\n--- Phase 3: Building AlphaMarket objects ---');

    const alphaMarkets: AlphaMarket[] = [];

    for (const vm of validMarkets) {
      let prices: MarketPrices = {
        openingNoPrice: null,
        finalNoPrice: vm.finalNoPrice,
        avgNoPrice: null,
        minNoPrice: null,
        maxNoPrice: null,
      };

      let hasFullHistory = false;
      let pricePointCount = 0;

      // Apply price history if available
      if (vm.noTokenId && priceHistoryMap.has(vm.noTokenId)) {
        const historyData = priceHistoryMap.get(vm.noTokenId)!;
        if (historyData.success && historyData.history.length > 0) {
          prices = this.priceHistoryFetcher.calculatePrices(historyData.history, vm.finalNoPrice);
          hasFullHistory = vm.tier === 'tier1';
          pricePointCount = historyData.history.length;
        }
      }

      const edge = this.edgeCalculator.calculateMarketEdge(vm.resolution, vm.finalNoPrice);
      const durationDays = this.calculateDuration(vm.market.createdAt || null, vm.market.endDate);

      alphaMarkets.push({
        id: vm.market.id,
        question: vm.market.question,
        tags: this.getTags(vm.market),
        resolution: vm.resolution,
        resolvedAt: vm.market.endDate.split('T')[0],
        createdAt: vm.market.createdAt?.split('T')[0] || null,
        durationDays,
        volumeNum: vm.volumeNum,
        liquidityNum: vm.liquidityNum,
        yesTokenId: vm.yesTokenId,
        noTokenId: vm.noTokenId,
        prices,
        edge,
        dataQuality: {
          tier: vm.tier,
          hasFullHistory,
          pricePointCount,
        },
      });
    }

    console.log(`\nProcessed ${alphaMarkets.length} binary markets total`);
    return alphaMarkets;
  }

  generateSummary(markets: AlphaMarket[], period: string, periodDays: number): AlphaSummaryOutput {
    return this.aggregator.generateSummary(markets, period, periodDays);
  }
}

// ============================================================================
// Main
// ============================================================================

async function main(): Promise<void> {
  const program = new Command();

  program
    .name('alpha-analysis')
    .description('Alpha analysis pipeline for Polymarket trading edge')
    .requiredOption('-p, --period <period>', 'Time period (e.g., 5d, 1m, 3m)')
    .option('-o, --output <path>', 'Output file path for market data', 'alpha_analysis.json')
    .option('-s, --summary <path>', 'Output file path for summary', 'alpha_summary.json')
    .option('--no-price-history', 'Skip fetching price history from CLOB API')
    .option('-c, --concurrency <number>', 'Number of concurrent API requests', '10')
    .parse(process.argv);

  const options = program.opts<{
    period: string;
    output: string;
    summary: string;
    priceHistory: boolean;
    concurrency: string;
  }>();

  const concurrency = parseInt(options.concurrency, 10);
  if (isNaN(concurrency) || concurrency < 1 || concurrency > 50) {
    console.error('Error: Concurrency must be a number between 1 and 50');
    process.exit(1);
  }

  let periodDays: number;
  let periodString: string;

  try {
    const parsed = parsePeriod(options.period);
    periodDays = parsed.days;
    periodString = parsed.original;
  } catch (err: unknown) {
    const error = err as Error;
    console.error(`Error: ${error.message}`);
    process.exit(1);
  }

  console.log(`\n=== Polymarket Alpha Analysis Pipeline ===`);
  console.log(`Period: ${periodString} (${periodDays} days)`);
  console.log(`Output: ${options.output}`);
  console.log(`Summary: ${options.summary}`);
  console.log(`Fetch price history: ${options.priceHistory}`);
  console.log(`Concurrency: ${concurrency}`);
  console.log('');

  const config: AlphaAnalysisConfig = {
    ...DEFAULT_CONFIG,
    period: periodString,
    periodDays,
  };

  const exporter = new AlphaAnalysisExporter(config);

  const endDate = new Date();
  const startDate = new Date();
  startDate.setDate(startDate.getDate() - periodDays);

  const rawMarkets = await exporter.fetchClosedMarkets(startDate, endDate);
  console.log(`\nFetched ${rawMarkets.length} total closed markets in date range`);

  const alphaMarkets = await exporter.processMarkets(rawMarkets, options.priceHistory, concurrency);

  const output: AlphaAnalysisOutput = {
    exportDate: new Date().toISOString().split('T')[0],
    period: periodString,
    periodDays,
    dataSource: {
      gammaApiUrl: GAMMA_API_URL,
      clobApiUrl: CLOB_API_URL,
    },
    totalMarkets: alphaMarkets.length,
    markets: alphaMarkets,
  };

  const summary = exporter.generateSummary(alphaMarkets, periodString, periodDays);

  console.log(`\n=== Summary ===`);
  console.log(`Total markets: ${summary.overall.totalMarkets}`);
  console.log(`No win rate: ${(summary.overall.noWinRate * 100).toFixed(1)}%`);
  console.log(`Average No price at close: ${(summary.overall.avgNoPriceAtClose * 100).toFixed(1)}%`);
  console.log(`Average edge: ${(summary.overall.averageEdge * 100).toFixed(2)}%`);
  console.log(
    `95% CI: [${(summary.overall.confidenceInterval95.lower * 100).toFixed(2)}%, ${(summary.overall.confidenceInterval95.upper * 100).toFixed(2)}%]`
  );

  console.log(`\n--- Top Categories by Edge ---`);
  for (const cat of summary.byCategory.slice(0, 5)) {
    const edgePct = (cat.edge * 100).toFixed(1);
    const ciStr = `[${(cat.confidenceInterval95.lower * 100).toFixed(1)}%, ${(cat.confidenceInterval95.upper * 100).toFixed(1)}%]`;
    console.log(`  ${cat.category}: ${edgePct}% edge (${cat.marketCount} markets, CI: ${ciStr})`);
  }

  console.log(`\n--- Recommendations ---`);
  for (const rec of summary.recommendations.slice(0, 5)) {
    console.log(`  - ${rec}`);
  }

  fs.writeFileSync(options.output, JSON.stringify(output, null, 2));
  console.log(`\nWrote ${options.output}`);

  fs.writeFileSync(options.summary, JSON.stringify(summary, null, 2));
  console.log(`Wrote ${options.summary}`);
}

main().catch((error: Error) => {
  console.error('Error:', error.message);
  process.exit(1);
});

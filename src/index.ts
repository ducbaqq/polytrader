/**
 * Polymarket Market Discovery Bot - Main Entry Point
 */

import * as dotenv from 'dotenv';
import { Command } from 'commander';
import {
  MarketSnapshot,
  OpportunityType,
  volumeDistributionToRecord,
  spreadDistributionToRecord,
} from './types';
import { MarketScanner, createScannerFromEnv } from './scanner';
import { OpportunityDetector, createDetectorFromEnv } from './detector';
import { DataStorage, createStorageFromEnv } from './storage';
import { Dashboard, createDashboardFromEnv } from './dashboard';
import { MarketValidator, createValidatorFromEnv } from './validator';
import { initDatabase, closeDatabase } from './database/index';
import { verifySchema, clearAllData, getTableCounts } from './database/schema';
import { generateValidationReport } from './analyzer/reportGenerator';

// Load environment variables (override shell env vars with .env file)
dotenv.config({ override: true });

class PolymarketBot {
  private scanner: MarketScanner | null = null;
  private detector: OpportunityDetector | null = null;
  private storage: DataStorage | null = null;
  private dashboard: Dashboard | null = null;

  private scanInterval: number;
  private dashboardInterval: number;
  private saveInterval: number;

  private isRunning: boolean = false;
  private startTime: Date | null = null;
  private saveIntervalId: NodeJS.Timeout | null = null;
  private totalOpportunities: number = 0;

  constructor(
    scanInterval: number = 30,
    dashboardInterval: number = 60,
    saveInterval: number = 3600
  ) {
    this.scanInterval = scanInterval;
    this.dashboardInterval = dashboardInterval;
    this.saveInterval = saveInterval;
    console.log('PolymarketBot initialized');
  }

  private onScanComplete(snapshot: MarketSnapshot): void {
    if (this.detector) {
      const opportunities = this.detector.analyzeSnapshot(snapshot);
      this.totalOpportunities += opportunities.length;

      // Save significant opportunities immediately
      if (this.storage && opportunities.length > 0) {
        const significant = opportunities.filter(
          (op) =>
            op.type === OpportunityType.ARBITRAGE || op.type === OpportunityType.MISPRICING
        );
        if (significant.length > 0) {
          this.storage.saveOpportunities(significant);
        }
      }
    }
  }

  private saveData(): void {
    if (!this.storage || !this.scanner) return;

    const snapshot = this.scanner.getCurrentSnapshot();
    if (!snapshot) {
      console.warn('No snapshot to save');
      return;
    }

    try {
      this.storage.saveSnapshot(snapshot);

      if (this.detector) {
        const recentOps = this.detector.getRecentOpportunities(1.0);
        if (recentOps.length > 0) {
          this.storage.saveOpportunities(recentOps);
        }
      }

      console.log('Data saved successfully');
    } catch (error) {
      console.error('Error saving data:', error);
      if (this.storage && error instanceof Error) {
        this.storage.logError(error, 'saveData');
      }
    }
  }

  async start(): Promise<void> {
    if (this.isRunning) {
      console.warn('Bot is already running');
      return;
    }

    console.log('Starting Polymarket Discovery Bot...');
    this.startTime = new Date();

    // Initialize components
    console.log('Initializing components...');

    this.storage = createStorageFromEnv();
    console.log('Storage initialized');

    this.scanner = new MarketScanner({
      scanInterval: this.scanInterval,
      minVolume: parseFloat(process.env.MIN_VOLUME || '1000'),
      maxMarkets: process.env.MAX_MARKETS ? parseInt(process.env.MAX_MARKETS) : undefined,
      onScanComplete: (snapshot) => this.onScanComplete(snapshot),
    });
    console.log('Scanner initialized');

    this.detector = createDetectorFromEnv(this.scanner);
    console.log('Detector initialized');

    this.dashboard = createDashboardFromEnv(this.scanner, this.detector);
    console.log('Dashboard initialized');

    // Setup signal handlers
    process.on('SIGINT', () => this.stop());
    process.on('SIGTERM', () => this.stop());

    // Start save interval
    this.saveIntervalId = setInterval(() => this.saveData(), this.saveInterval * 1000);

    // Start scanner
    this.scanner.start();

    this.isRunning = true;
    console.log('Bot started successfully!');

    // Run dashboard (blocking)
    await this.dashboard.runBlocking();
  }

  stop(): void {
    if (!this.isRunning) return;

    console.log('\nStopping bot...');
    this.isRunning = false;

    // Stop save interval
    if (this.saveIntervalId) {
      clearInterval(this.saveIntervalId);
      this.saveIntervalId = null;
    }

    // Stop scanner
    if (this.scanner) {
      this.scanner.stop();
    }

    // Stop dashboard
    if (this.dashboard) {
      this.dashboard.stop();
    }

    // Save final snapshot
    console.log('Saving final snapshot...');
    this.saveData();

    // Cleanup old files
    if (this.storage) {
      this.storage.cleanupOldFiles(30);
    }

    // Print final stats
    this.printFinalStats();

    process.exit(0);
  }

  private printFinalStats(): void {
    if (!this.startTime) return;

    const runtime = Date.now() - this.startTime.getTime();
    const runtimeStr = this.formatDuration(runtime);

    console.log('\n' + '='.repeat(60));
    console.log('POLYMARKET DISCOVERY BOT - FINAL STATS');
    console.log('='.repeat(60));
    console.log(`Runtime: ${runtimeStr}`);

    if (this.scanner) {
      const stats = this.scanner.getStats();
      console.log(`Total scans: ${stats.totalScans}`);
      console.log(`Failed scans: ${stats.failedScans}`);
      console.log(`Markets tracked: ${stats.marketsTracked}`);
    }

    if (this.detector) {
      const detStats = this.detector.getStats();
      console.log(`Total opportunities detected: ${detStats.totalOpportunities}`);
      console.log('By type:');
      for (const [type, count] of Object.entries(detStats.byType)) {
        console.log(`  - ${type}: ${count}`);
      }
    }

    if (this.storage) {
      const storStats = this.storage.getStats();
      console.log(`Snapshots saved: ${storStats.snapshotsCount}`);
      console.log(`Total storage: ${storStats.totalSizeMb.toFixed(2)} MB`);
    }

    console.log('='.repeat(60));
    console.log('Data saved to ./data/');
    console.log('Logs saved to ./logs/');
    console.log('='.repeat(60) + '\n');
  }

  private formatDuration(ms: number): string {
    const seconds = Math.floor(ms / 1000);
    const minutes = Math.floor(seconds / 60);
    const hours = Math.floor(minutes / 60);
    const days = Math.floor(hours / 24);

    if (days > 0) {
      return `${days}d ${hours % 24}h ${minutes % 60}m ${seconds % 60}s`;
    } else if (hours > 0) {
      return `${hours}h ${minutes % 60}m ${seconds % 60}s`;
    } else if (minutes > 0) {
      return `${minutes}m ${seconds % 60}s`;
    } else {
      return `${seconds}s`;
    }
  }
}

async function runScanOnce(): Promise<number> {
  console.log('Running single scan...');

  const scanner = createScannerFromEnv();
  const detector = createDetectorFromEnv(scanner);

  const snapshot = await scanner.scanOnce();

  if (!snapshot) {
    console.log('Scan failed!');
    return 1;
  }

  console.log('\n=== SCAN RESULTS ===');
  console.log(`Total markets: ${snapshot.totalMarkets}`);
  console.log(`Total 24h volume: $${snapshot.totalVolume24h.toLocaleString()}`);
  console.log(`Average spread: ${(snapshot.avgSpread * 100).toFixed(2)}%`);

  console.log('\n=== VOLUME DISTRIBUTION ===');
  const volDist = volumeDistributionToRecord(snapshot.volumeDistribution);
  for (const [tier, count] of Object.entries(volDist)) {
    console.log(`  ${tier}: ${count}`);
  }

  console.log('\n=== SPREAD DISTRIBUTION ===');
  const spreadDist = spreadDistributionToRecord(snapshot.spreadDistribution);
  for (const [spread, count] of Object.entries(spreadDist)) {
    console.log(`  ${spread}: ${count}`);
  }

  // Detect opportunities
  const opportunities = detector.analyzeSnapshot(snapshot);

  console.log(`\n=== OPPORTUNITIES (${opportunities.length} found) ===`);
  for (const op of opportunities.slice(0, 10)) {
    console.log(`  [${op.type.toUpperCase()}] ${op.question.slice(0, 60)}...`);
    console.log(`    ${op.description}`);
  }

  if (opportunities.length > 10) {
    console.log(`  ... and ${opportunities.length - 10} more`);
  }

  console.log('\n=== TOP 5 LIQUID MARKETS ===');
  const sortedMarkets = [...snapshot.markets]
    .sort((a, b) => b.volume24h - a.volume24h)
    .slice(0, 5);

  for (const market of sortedMarkets) {
    console.log(`  ${market.question.slice(0, 60)}...`);
    console.log(`    Volume: $${market.volume24h.toLocaleString()}`);
    if (market.yesToken) {
      console.log(`    YES spread: ${(market.yesToken.spreadPct * 100).toFixed(2)}%`);
    }
  }

  return 0;
}

async function runValidator(): Promise<void> {
  const validator = createValidatorFromEnv();
  await validator.start();
}

async function runReport(): Promise<void> {
  console.log('Generating validation report...');
  initDatabase();

  const schemaCheck = await verifySchema();
  if (!schemaCheck.valid) {
    console.error('Database schema invalid. Missing tables:', schemaCheck.missing);
    process.exit(1);
  }

  try {
    const summary = await generateValidationReport(7, './reports');
    console.log('\n=== VALIDATION SUMMARY ===');
    console.log(`Recommendation: ${summary.recommendation}`);
    console.log(`Net Profit: $${summary.netProfit.toFixed(2)}`);
    console.log(`Win Rate: ${(summary.winRate * 100).toFixed(1)}%`);
    console.log(`Fill Rate: ${(summary.overallFillRate * 100).toFixed(1)}%`);
    console.log('\n' + summary.recommendationReason);
  } finally {
    await closeDatabase();
  }
}

async function runReset(): Promise<void> {
  console.log('Resetting database...');
  initDatabase();

  const schemaCheck = await verifySchema();
  if (!schemaCheck.valid) {
    console.error('Database schema invalid. Missing tables:', schemaCheck.missing);
    process.exit(1);
  }

  console.log('Clearing all data...');
  await clearAllData();
  console.log('Database reset complete');
  await closeDatabase();
}

async function runDbStatus(): Promise<void> {
  console.log('Checking database status...');
  initDatabase();

  const schemaCheck = await verifySchema();
  console.log(`Schema valid: ${schemaCheck.valid}`);
  if (!schemaCheck.valid) {
    console.log('Missing tables:', schemaCheck.missing);
  }

  const counts = await getTableCounts();
  console.log('\nTable row counts:');
  for (const [table, count] of Object.entries(counts)) {
    console.log(`  ${table}: ${count}`);
  }

  await closeDatabase();
}

async function main(): Promise<void> {
  const program = new Command();

  program
    .name('polymarket-bot')
    .description('Polymarket Market Discovery Bot & Validator')
    .version('1.0.0');

  // Default command - original discovery bot
  program
    .command('discover', { isDefault: true })
    .description('Run the market discovery bot with terminal dashboard')
    .option('--scan-once', 'Run a single scan and exit')
    .action(async (options) => {
      if (options.scanOnce) {
        const exitCode = await runScanOnce();
        process.exit(exitCode);
      } else {
        const scanInterval = parseFloat(process.env.SCAN_INTERVAL_SECONDS || '30');
        const dashboardInterval = parseFloat(process.env.DASHBOARD_UPDATE_SECONDS || '60');
        const saveInterval = parseFloat(process.env.DATA_SAVE_INTERVAL_MINUTES || '60') * 60;

        console.log(`
    ╔═══════════════════════════════════════════════════════════════╗
    ║         POLYMARKET MARKET DISCOVERY BOT                       ║
    ║                                                               ║
    ║  Scanning markets every ${scanInterval}s                                ║
    ║  Dashboard updates every ${dashboardInterval}s                              ║
    ║  Data saves every ${saveInterval / 60} minutes                              ║
    ║                                                               ║
    ║  Press Ctrl+C to stop (saves final snapshot)                  ║
    ╚═══════════════════════════════════════════════════════════════╝
        `);

        const bot = new PolymarketBot(scanInterval, dashboardInterval, saveInterval);
        await bot.start();
      }
    });

  // Validate command - full validation system
  program
    .command('validate')
    .description('Run the market validation system (scans + paper trading + analysis)')
    .action(async () => {
      await runValidator();
    });

  // Report command - generate report from existing data
  program
    .command('report')
    .description('Generate validation report from existing data')
    .action(async () => {
      await runReport();
    });

  // Reset command - clear all data
  program
    .command('reset')
    .description('Reset database (clear all data)')
    .action(async () => {
      await runReset();
    });

  // DB status command
  program
    .command('db-status')
    .description('Check database status and table counts')
    .action(async () => {
      await runDbStatus();
    });

  // Keep --scan-once as top-level option for backwards compatibility
  program
    .option('--scan-once', 'Run a single scan and exit')
    .action(async (options) => {
      if (options.scanOnce) {
        const exitCode = await runScanOnce();
        process.exit(exitCode);
      }
    });

  await program.parseAsync(process.argv);
}

main().catch((error) => {
  console.error('Fatal error:', error);
  process.exit(1);
});

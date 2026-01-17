#!/usr/bin/env node
/**
 * CLI entry point for the No-betting Paper Trading System.
 *
 * Commands:
 *   no-trader start   - Start the paper trader (runs continuously)
 *   no-trader status  - Check current portfolio status
 *   no-trader report  - Generate full performance report
 *   no-trader reset   - Reset all paper trading data
 */

import 'dotenv/config';
import { Command } from 'commander';
import {
  NoPaperTrader,
  loadConfig,
  printStatus,
  generateReport,
  printReport,
  resetPaperTrading,
  initializeTables,
  initializePortfolio,
} from './noPaperTrader/index';
import { initDatabase, closeDatabase } from './database/index';

const program = new Command();

program
  .name('no-trader')
  .description('No-betting paper trading system for Polymarket')
  .version('1.0.0');

/**
 * Start command - runs the paper trader continuously.
 */
program
  .command('start')
  .description('Start the paper trader (runs scanner and monitor continuously)')
  .option('--capital <amount>', 'Initial capital (default: $2500)', '2500')
  .option('--size <amount>', 'Position size per trade (default: $50)', '50')
  .option('--min-edge <percent>', 'Minimum edge required (default: 5%)', '5')
  .option('--take-profit <percent>', 'Take profit threshold (default: 90%)', '90')
  .option('--stop-loss <percent>', 'Stop loss threshold (default: 25%)', '25')
  .option('--scan-interval <seconds>', 'Scan interval in seconds (default: 60)', '60')
  .option('--monitor-interval <seconds>', 'Monitor interval in seconds (default: 30)', '30')
  .action(async (options) => {
    try {
      const config = loadConfig();

      // Override config from CLI options
      if (options.capital) config.initialCapital = parseFloat(options.capital);
      if (options.size) config.positionSize = parseFloat(options.size);
      if (options.minEdge) config.minEdge = parseFloat(options.minEdge) / 100;
      if (options.takeProfit) config.takeProfitThreshold = parseFloat(options.takeProfit) / 100;
      if (options.stopLoss) config.stopLossThreshold = parseFloat(options.stopLoss) / 100;
      if (options.scanInterval) config.scanIntervalSeconds = parseInt(options.scanInterval);
      if (options.monitorInterval) config.monitorIntervalSeconds = parseInt(options.monitorInterval);

      const trader = new NoPaperTrader(config);

      // Handle graceful shutdown
      const shutdown = async (signal: string) => {
        console.log(`\nReceived ${signal}, shutting down...`);
        await trader.stop();
        await closeDatabase();
        process.exit(0);
      };
      process.on('SIGINT', () => shutdown('SIGINT'));
      process.on('SIGTERM', () => shutdown('SIGTERM'));

      await trader.start();

      // Keep the process running
      await new Promise(() => {});
    } catch (error) {
      console.error('Error starting paper trader:', error);
      process.exit(1);
    }
  });

/**
 * Status command - shows current portfolio status.
 */
program
  .command('status')
  .description('Check current portfolio status')
  .action(async () => {
    try {
      initDatabase();
      await initializeTables();
      await printStatus();
      await closeDatabase();
    } catch (error) {
      console.error('Error getting status:', error);
      process.exit(1);
    }
  });

/**
 * Report command - generates full performance report.
 */
program
  .command('report')
  .description('Generate full performance report')
  .action(async () => {
    try {
      initDatabase();
      await initializeTables();

      const report = await generateReport();
      if (report) {
        printReport(report);
      } else {
        console.log('No data to report. Start the paper trader first.');
      }

      await closeDatabase();
    } catch (error) {
      console.error('Error generating report:', error);
      process.exit(1);
    }
  });

/**
 * Reset command - clears all paper trading data.
 */
program
  .command('reset')
  .description('Reset all paper trading data (WARNING: This cannot be undone)')
  .option('--force', 'Skip confirmation prompt')
  .action(async (options) => {
    try {
      if (!options.force) {
        console.log('WARNING: This will delete all paper trading data including:');
        console.log('  - All positions (open and closed)');
        console.log('  - All trades');
        console.log('  - Portfolio history');
        console.log('  - Daily snapshots');
        console.log('  - Scanned markets log');
        console.log('\nRun with --force to confirm.');
        process.exit(0);
      }

      initDatabase();
      await initializeTables();
      await resetPaperTrading();

      console.log('Paper trading data reset successfully.');

      await closeDatabase();
    } catch (error) {
      console.error('Error resetting data:', error);
      process.exit(1);
    }
  });

/**
 * Scan-once command - runs a single scan without starting the full trader.
 */
program
  .command('scan-once')
  .description('Run a single market scan without starting the full trader')
  .action(async () => {
    try {
      initDatabase();
      await initializeTables();

      const config = loadConfig();
      const { PolymarketClient } = await import('./apiClient');
      const { MarketScanner } = await import('./noPaperTrader/scanner');

      await initializePortfolio(config.initialCapital);

      const client = new PolymarketClient();
      const scanner = new MarketScanner(client, config);

      console.log('Running single scan...');
      const result = await scanner.scan();

      console.log('\nScan Results:');
      console.log(`  Markets scanned: ${result.marketsScanned}`);
      console.log(`  Eligible markets: ${result.eligibleMarkets.length}`);
      console.log(`  Positions opened: ${result.positionsOpened}`);
      console.log(`  Rejected: ${result.rejectedCount}`);

      if (result.eligibleMarkets.length > 0) {
        console.log('\nEligible Markets:');
        for (const market of result.eligibleMarkets) {
          console.log(`  - ${market.question.substring(0, 50)}...`);
          console.log(`    Category: ${market.category}, No: ${(market.noPrice * 100).toFixed(1)}%, Edge: ${(market.edge * 100).toFixed(1)}%`);
        }
      }

      if (Object.keys(result.rejectionReasons).length > 0) {
        console.log('\nRejection Reasons:');
        for (const [reason, count] of Object.entries(result.rejectionReasons)) {
          console.log(`  ${reason}: ${count}`);
        }
      }

      await closeDatabase();
    } catch (error) {
      console.error('Error during scan:', error);
      process.exit(1);
    }
  });

/**
 * Monitor-once command - runs a single monitor cycle.
 */
program
  .command('monitor-once')
  .description('Run a single position monitor cycle')
  .action(async () => {
    try {
      initDatabase();
      await initializeTables();

      const config = loadConfig();
      const { PolymarketClient } = await import('./apiClient');
      const { PositionMonitor } = await import('./noPaperTrader/monitor');

      const client = new PolymarketClient();
      const monitor = new PositionMonitor(client, config);

      console.log('Running single monitor cycle...');
      const result = await monitor.monitor();

      console.log('\nMonitor Results:');
      console.log(`  Positions checked: ${result.positionsChecked}`);
      console.log(`  Take profit triggered: ${result.takeProfitTriggered}`);
      console.log(`  Stop loss triggered: ${result.stopLossTriggered}`);
      console.log(`  Resolved: ${result.resolved}`);
      console.log(`  Still open: ${result.stillOpen}`);

      await closeDatabase();
    } catch (error) {
      console.error('Error during monitor:', error);
      process.exit(1);
    }
  });

program.parse();

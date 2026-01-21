#!/usr/bin/env node
/**
 * CLI entry point for the No-betting Paper Trading System.
 *
 * Commands:
 *   no-trader scan      - Run scanner (direction-agnostic, shared by all strategies)
 *   no-trader monitor   - Run monitor for a specific strategy (--strategy required)
 *   no-trader status    - Check current portfolio status for a strategy
 *   no-trader report    - Generate full performance report for a strategy
 *   no-trader reset     - Reset all paper trading data
 */

import 'dotenv/config';
import { Command } from 'commander';
import {
  loadConfig,
  printStatus,
  generateReport,
  printReport,
  resetPaperTrading,
  initializeTables,
  initializePortfolio,
  getPortfolio,
  getOpenPositions,
  getLifetimeExitStats,
  getStrategy,
  getAvailableStrategies,
  isValidStrategy,
  STRATEGY_REGISTRY,
} from './noPaperTrader/index';
import { StrategyId } from './noPaperTrader/types';
import { initDatabase, closeDatabase } from './database/index';
import {
  ScannerDashboardState,
  MonitorDashboardState,
  PositionWithPrice,
  renderScannerDashboard,
  renderMonitorDashboard,
} from './noPaperTrader/dashboard';
import { PolymarketClient } from './apiClient';
import { MarketScanner } from './noPaperTrader/scanner';
import { PositionMonitor } from './noPaperTrader/monitor';

const program = new Command();

program
  .name('no-trader')
  .description('Multi-strategy paper trading system for Polymarket')
  .version('2.0.0');

/**
 * Validate and return a strategy ID.
 * Exits with error if strategy is invalid.
 */
function validateStrategy(name: string): StrategyId {
  if (!isValidStrategy(name)) {
    console.error(`Error: Strategy '${name}' not found.`);
    console.error(`Available strategies: ${getAvailableStrategies().join(', ')}`);
    process.exit(1);
  }
  return name as StrategyId;
}

/**
 * Scan command - runs scanner continuously (direction-agnostic).
 * Scanner is shared across all strategies.
 */
program
  .command('scan')
  .description('Run scanner continuously (direction-agnostic, shared by monitors)')
  .option('--interval <seconds>', 'Scan interval in seconds (default: 60)', '60')
  .option('--concurrency <num>', 'Number of markets to process in parallel (default: 20)', '20')
  .option('--no-dashboard', 'Disable live dashboard')
  .action(async (options) => {
    try {
      initDatabase();
      await initializeTables();

      const config = loadConfig();
      if (options.interval) config.scanIntervalSeconds = parseInt(options.interval);
      if (options.concurrency) config.scanConcurrency = parseInt(options.concurrency);

      const useDashboard = options.dashboard !== false;

      const client = new PolymarketClient();
      const scanner = new MarketScanner(client, config, config.scanConcurrency);

      const startTime = Date.now();
      let scanCount = 0;

      // Dashboard state
      const dashState: ScannerDashboardState = {
        status: 'idle',
        runtime: 0,
        totalScans: 0,
        positionsOpened: 0,
        cashBalance: 0,
        openPositionCount: 0,
        lastUpdate: new Date(),
        recentOpened: [],
      };

      const refreshDashboard = async () => {
        if (!useDashboard) return;
        dashState.runtime = Date.now() - startTime;
        dashState.lastUpdate = new Date();
        renderScannerDashboard(dashState);
      };

      if (!useDashboard) {
        console.log(`🔍 Scanner started (interval: ${config.scanIntervalSeconds}s, concurrency: ${config.scanConcurrency})`);
        console.log(`   Categories: ${config.categories.join(', ')}`);
        console.log('   Press Ctrl+C to stop\n');
      }

      const runScan = async () => {
        scanCount++;
        dashState.status = 'scanning';
        dashState.totalScans = scanCount;

        const onProgress = useDashboard
          ? (current: number, total: number) => {
              dashState.scanProgress = { current, total };
              refreshDashboard();
            }
          : undefined;

        if (!useDashboard) console.log(`[${new Date().toISOString()}] Scan #${scanCount} starting...`);

        const result = await scanner.scan(onProgress, useDashboard);

        dashState.status = 'idle';
        dashState.scanProgress = undefined;

        if (!useDashboard) {
          console.log(`   Scanned: ${result.marketsScanned}, Found: ${result.scannedMarkets.length}`);
        }

        await refreshDashboard();
      };

      let running = true;
      const dashIntervalId = useDashboard ? setInterval(refreshDashboard, 5000) : null;

      const shutdown = async () => {
        if (!useDashboard) console.log('\nShutting down scanner...');
        running = false;
        if (dashIntervalId) clearInterval(dashIntervalId);
        await closeDatabase();
        process.exit(0);
      };
      process.on('SIGINT', shutdown);
      process.on('SIGTERM', shutdown);

      // Run scan loop - wait interval AFTER scan completes
      while (running) {
        await runScan();
        if (running) {
          await new Promise(resolve => setTimeout(resolve, config.scanIntervalSeconds * 1000));
        }
      }
    } catch (error) {
      console.error('Error in scanner:', error);
      process.exit(1);
    }
  });

/**
 * Monitor command - runs monitor continuously for a specific strategy.
 */
program
  .command('monitor')
  .description('Run monitor continuously for a strategy (opens positions and manages exits)')
  .requiredOption('--strategy <name>', 'Strategy to use (yes-buyer, no-buyer)')
  .option('--interval <seconds>', 'Monitor interval in seconds (default: 30)', '30')
  .option('--capital <amount>', 'Initial capital (default: $2500)', '2500')
  .option('--size <amount>', 'Position size per trade (default: $50)', '50')
  .option('--take-profit <percent>', 'Take profit threshold (default: 90%)', '90')
  .option('--stop-loss <percent>', 'Stop loss threshold (default: 25%)', '25')
  .option('--no-dashboard', 'Disable live dashboard')
  .action(async (options) => {
    try {
      const strategyId = validateStrategy(options.strategy);
      const strategy = getStrategy(strategyId);

      initDatabase();
      await initializeTables();

      const config = loadConfig();
      if (options.interval) config.monitorIntervalSeconds = parseInt(options.interval);
      if (options.capital) config.initialCapital = parseFloat(options.capital);
      if (options.size) config.positionSize = parseFloat(options.size);
      if (options.takeProfit) config.takeProfitThreshold = parseFloat(options.takeProfit) / 100;
      if (options.stopLoss) config.stopLossThreshold = parseFloat(options.stopLoss) / 100;

      const useDashboard = options.dashboard !== false;

      // Initialize portfolio for this strategy
      await initializePortfolio(config.initialCapital, strategyId);

      const client = new PolymarketClient();
      const scanner = new MarketScanner(client, config, config.scanConcurrency);
      const monitor = new PositionMonitor(client, config, strategyId);

      const startTime = Date.now();

      const dashState: MonitorDashboardState = {
        status: 'idle',
        runtime: 0,
        takeProfitCount: 0,
        stopLossCount: 0,
        resolvedCount: 0,
        positions: [],
        portfolio: null,
        lastUpdate: new Date(),
      };

      const updatePositionsWithPrices = async (): Promise<PositionWithPrice[]> => {
        const positions = await getOpenPositions(strategyId);
        const result: PositionWithPrice[] = [];

        for (const pos of positions) {
          const posWithPrice: PositionWithPrice = { ...pos };
          try {
            const orderBook = await client.getOrderBook(pos.tokenId);
            if (orderBook && orderBook.bids && orderBook.bids.length > 0) {
              const bestBid = orderBook.bids[orderBook.bids.length - 1];
              posWithPrice.currentPrice = parseFloat(String(bestBid.price));
            } else if (orderBook && orderBook.asks && orderBook.asks.length > 0) {
              const bestAsk = orderBook.asks[orderBook.asks.length - 1];
              posWithPrice.currentPrice = parseFloat(String(bestAsk.price));
            }
            if (posWithPrice.currentPrice !== undefined) {
              const currentValue = pos.quantity * posWithPrice.currentPrice;
              posWithPrice.unrealizedPnl = currentValue - pos.costBasis;
            }
          } catch { /* ignore */ }
          result.push(posWithPrice);
        }
        return result;
      };

      const refreshDashboard = async () => {
        if (!useDashboard) return;
        dashState.runtime = Date.now() - startTime;
        dashState.portfolio = await getPortfolio(strategyId);
        dashState.positions = await updatePositionsWithPrices();
        // Fetch lifetime stats from database (not session stats)
        const lifetimeStats = await getLifetimeExitStats(strategyId);
        dashState.takeProfitCount = lifetimeStats.takeProfitCount;
        dashState.stopLossCount = lifetimeStats.stopLossCount;
        dashState.resolvedCount = lifetimeStats.resolvedCount;
        dashState.lastUpdate = new Date();
        renderMonitorDashboard(dashState, strategy.name);
      };

      if (!useDashboard) {
        console.log(`👁️  Monitor started for ${strategy.name} (interval: ${config.monitorIntervalSeconds}s)`);
        console.log(`   Side: ${strategy.side}`);
        console.log(`   Take profit: ${(config.takeProfitThreshold * 100).toFixed(0)}%`);
        console.log(`   Stop loss: ${(config.stopLossThreshold * 100).toFixed(0)}%`);
        console.log('   Press Ctrl+C to stop\n');
      }

      let cycleCount = 0;
      const runMonitor = async () => {
        cycleCount++;
        dashState.status = 'checking';
        if (useDashboard) await refreshDashboard();

        // First scan for new markets
        const scanResult = await scanner.scan(undefined, true);

        // Then monitor - pass scanned markets for entry evaluation
        const result = await monitor.monitor(scanResult.scannedMarkets);
        dashState.status = 'idle';

        const actions = result.positionsOpened + result.takeProfitTriggered + result.stopLossTriggered + result.resolved;
        if (!useDashboard && (actions > 0 || cycleCount % 10 === 1)) {
          console.log(`[${new Date().toISOString()}] [${strategy.name}] ${result.positionsChecked} positions, Opened:${result.positionsOpened} TP:${result.takeProfitTriggered} SL:${result.stopLossTriggered} Resolved:${result.resolved}`);
        }

        await refreshDashboard();
      };

      let running = true;
      const dashIntervalId = useDashboard ? setInterval(refreshDashboard, 5000) : null;

      const shutdown = async () => {
        if (!useDashboard) console.log(`\nShutting down ${strategy.name} monitor...`);
        running = false;
        if (dashIntervalId) clearInterval(dashIntervalId);
        await closeDatabase();
        process.exit(0);
      };
      process.on('SIGINT', shutdown);
      process.on('SIGTERM', shutdown);

      // Run monitor loop - wait interval AFTER cycle completes
      while (running) {
        await runMonitor();
        if (running) {
          await new Promise(resolve => setTimeout(resolve, config.monitorIntervalSeconds * 1000));
        }
      }
    } catch (error) {
      console.error('Error in monitor:', error);
      process.exit(1);
    }
  });

/**
 * Status command - shows current portfolio status for a strategy.
 */
program
  .command('status')
  .description('Check current portfolio status for a strategy')
  .requiredOption('--strategy <name>', 'Strategy to check (yes-buyer, no-buyer)')
  .action(async (options) => {
    try {
      const strategyId = validateStrategy(options.strategy);

      initDatabase();
      await initializeTables();
      await printStatus(strategyId);
      await closeDatabase();
    } catch (error) {
      console.error('Error getting status:', error);
      process.exit(1);
    }
  });

/**
 * Report command - generates full performance report for a strategy.
 */
program
  .command('report')
  .description('Generate full performance report for a strategy')
  .requiredOption('--strategy <name>', 'Strategy to report on (yes-buyer, no-buyer)')
  .action(async (options) => {
    try {
      const strategyId = validateStrategy(options.strategy);

      initDatabase();
      await initializeTables();

      const report = await generateReport(strategyId);
      if (report) {
        printReport(report, strategyId);
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
        console.log('  - All positions (open and closed) for ALL strategies');
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
 * List strategies command - shows available strategies.
 */
program
  .command('strategies')
  .description('List available trading strategies')
  .action(() => {
    console.log('\nAvailable Trading Strategies:\n');
    for (const [id, strategy] of Object.entries(STRATEGY_REGISTRY)) {
      console.log(`  ${id}`);
      console.log(`    Name: ${strategy.name}`);
      console.log(`    Side: ${strategy.side}`);
      console.log(`    Description: ${strategy.description}`);
      console.log(`    Price Range: ${(strategy.minPrice * 100).toFixed(0)}% - ${(strategy.maxPrice * 100).toFixed(0)}%`);
      console.log(`    Min Edge: ${(strategy.minEdge * 100).toFixed(0)}%`);
      console.log(`    Categories: ${Object.keys(strategy.categoryWinRates).join(', ')}`);
      console.log('');
    }
  });

/**
 * Scan-once command - runs a single scan without starting the full trader.
 */
program
  .command('scan-once')
  .description('Run a single market scan (direction-agnostic)')
  .option('--concurrency <num>', 'Number of markets to process in parallel (default: 20)', '20')
  .action(async (options) => {
    try {
      initDatabase();
      await initializeTables();

      const config = loadConfig();
      if (options.concurrency) config.scanConcurrency = parseInt(options.concurrency);

      const client = new PolymarketClient();
      const scanner = new MarketScanner(client, config, config.scanConcurrency);

      console.log('Running single scan...');
      const result = await scanner.scan();

      console.log('\nScan Results:');
      console.log(`  Markets scanned: ${result.marketsScanned}`);
      console.log(`  Markets found: ${result.scannedMarkets.length}`);
      console.log(`  Rejected: ${result.rejectedCount}`);

      if (result.scannedMarkets.length > 0) {
        console.log('\nScanned Markets (with both YES and NO prices):');
        for (const market of result.scannedMarkets.slice(0, 10)) {
          console.log(`  - ${market.question.substring(0, 50)}...`);
          console.log(`    Category: ${market.category}`);
          console.log(`    YES: ${(market.yesPrice * 100).toFixed(1)}%, NO: ${(market.noPrice * 100).toFixed(1)}%`);
        }
        if (result.scannedMarkets.length > 10) {
          console.log(`  ... and ${result.scannedMarkets.length - 10} more`);
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
 * Monitor-once command - runs a single monitor cycle for a strategy.
 */
program
  .command('monitor-once')
  .description('Run a single position monitor cycle for a strategy')
  .requiredOption('--strategy <name>', 'Strategy to monitor (yes-buyer, no-buyer)')
  .action(async (options) => {
    try {
      const strategyId = validateStrategy(options.strategy);
      const strategy = getStrategy(strategyId);

      initDatabase();
      await initializeTables();

      const config = loadConfig();
      const client = new PolymarketClient();
      const scanner = new MarketScanner(client, config, config.scanConcurrency);
      const monitor = new PositionMonitor(client, config, strategyId);

      // Initialize portfolio for this strategy if needed
      await initializePortfolio(config.initialCapital, strategyId);

      console.log(`Running single monitor cycle for ${strategy.name}...`);

      // First scan for new markets
      const scanResult = await scanner.scan(undefined, true);

      // Then monitor
      const result = await monitor.monitor(scanResult.scannedMarkets);

      console.log('\nMonitor Results:');
      console.log(`  Positions opened: ${result.positionsOpened}`);
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

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
  getPortfolio,
  getOpenPositions,
} from './noPaperTrader/index';
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
  .option('--no-dashboard', 'Disable live dashboard (use plain text output)')
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

      const useDashboard = options.dashboard !== false;
      const trader = new NoPaperTrader(config, useDashboard);

      // Handle graceful shutdown
      const shutdown = async (signal: string) => {
        if (!useDashboard) console.log(`\nReceived ${signal}, shutting down...`);
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
 * Scan command - runs scanner continuously as independent process.
 */
program
  .command('scan')
  .description('Run scanner continuously (independent process, shares DB with monitor)')
  .option('--interval <seconds>', 'Scan interval in seconds (default: 60)', '60')
  .option('--capital <amount>', 'Initial capital (default: $2500)', '2500')
  .option('--size <amount>', 'Position size per trade (default: $50)', '50')
  .option('--concurrency <num>', 'Number of markets to process in parallel (default: 20)', '20')
  .option('--no-dashboard', 'Disable live dashboard')
  .action(async (options) => {
    try {
      initDatabase();
      await initializeTables();

      const config = loadConfig();
      if (options.interval) config.scanIntervalSeconds = parseInt(options.interval);
      if (options.capital) config.initialCapital = parseFloat(options.capital);
      if (options.size) config.positionSize = parseFloat(options.size);
      if (options.concurrency) config.scanConcurrency = parseInt(options.concurrency);

      const useDashboard = options.dashboard !== false;

      await initializePortfolio(config.initialCapital);

      const client = new PolymarketClient();
      const scanner = new MarketScanner(client, config, config.scanConcurrency);

      const startTime = Date.now();
      let scanCount = 0;
      let totalOpened = 0;
      const recentOpened: Array<{ question: string; price: number; edge: number }> = [];

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
        const portfolio = await getPortfolio();
        const positions = await getOpenPositions();
        dashState.runtime = Date.now() - startTime;
        dashState.cashBalance = portfolio?.cashBalance || 0;
        dashState.openPositionCount = positions.length;
        dashState.lastUpdate = new Date();
        dashState.recentOpened = recentOpened.slice(-5);
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

        totalOpened += result.positionsOpened;
        dashState.positionsOpened = totalOpened;
        dashState.status = 'idle';
        dashState.scanProgress = undefined;

        // Track recently opened
        for (const m of result.eligibleMarkets.slice(0, result.positionsOpened)) {
          recentOpened.push({ question: m.question, price: m.noPrice, edge: m.edge });
          if (recentOpened.length > 10) recentOpened.shift();
        }

        if (!useDashboard) {
          console.log(`   Scanned: ${result.marketsScanned}, Eligible: ${result.eligibleMarkets.length}, Opened: ${result.positionsOpened}`);
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
 * Monitor command - runs monitor continuously as independent process.
 */
program
  .command('monitor')
  .description('Run monitor continuously (independent process, shares DB with scanner)')
  .option('--interval <seconds>', 'Monitor interval in seconds (default: 30)', '30')
  .option('--take-profit <percent>', 'Take profit threshold (default: 90%)', '90')
  .option('--stop-loss <percent>', 'Stop loss threshold (default: 25%)', '25')
  .option('--no-dashboard', 'Disable live dashboard')
  .action(async (options) => {
    try {
      initDatabase();
      await initializeTables();

      const config = loadConfig();
      if (options.interval) config.monitorIntervalSeconds = parseInt(options.interval);
      if (options.takeProfit) config.takeProfitThreshold = parseFloat(options.takeProfit) / 100;
      if (options.stopLoss) config.stopLossThreshold = parseFloat(options.stopLoss) / 100;

      const useDashboard = options.dashboard !== false;

      const client = new PolymarketClient();
      const monitor = new PositionMonitor(client, config);

      const startTime = Date.now();
      let cycleCount = 0;
      let totalTP = 0;
      let totalSL = 0;
      let totalResolved = 0;

      const dashState: MonitorDashboardState = {
        status: 'idle',
        runtime: 0,
        totalCycles: 0,
        takeProfitCount: 0,
        stopLossCount: 0,
        resolvedCount: 0,
        positions: [],
        portfolio: null,
        lastUpdate: new Date(),
      };

      const updatePositionsWithPrices = async (): Promise<PositionWithPrice[]> => {
        const positions = await getOpenPositions();
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
        dashState.portfolio = await getPortfolio();
        dashState.positions = await updatePositionsWithPrices();
        dashState.lastUpdate = new Date();
        renderMonitorDashboard(dashState);
      };

      if (!useDashboard) {
        console.log(`👁️  Monitor started (interval: ${config.monitorIntervalSeconds}s)`);
        console.log(`   Take profit: ${(config.takeProfitThreshold * 100).toFixed(0)}%`);
        console.log(`   Stop loss: ${(config.stopLossThreshold * 100).toFixed(0)}%`);
        console.log('   Press Ctrl+C to stop\n');
      }

      const runMonitor = async () => {
        cycleCount++;
        dashState.status = 'checking';
        dashState.totalCycles = cycleCount;
        if (useDashboard) await refreshDashboard();

        const result = await monitor.monitor();

        totalTP += result.takeProfitTriggered;
        totalSL += result.stopLossTriggered;
        totalResolved += result.resolved;

        dashState.takeProfitCount = totalTP;
        dashState.stopLossCount = totalSL;
        dashState.resolvedCount = totalResolved;
        dashState.status = 'idle';

        const actions = result.takeProfitTriggered + result.stopLossTriggered + result.resolved;
        if (!useDashboard && (actions > 0 || cycleCount % 10 === 1)) {
          console.log(`[${new Date().toISOString()}] Cycle #${cycleCount}: ${result.positionsChecked} positions, TP:${result.takeProfitTriggered} SL:${result.stopLossTriggered} Resolved:${result.resolved}`);
        }

        await refreshDashboard();
      };

      let running = true;
      const dashIntervalId = useDashboard ? setInterval(refreshDashboard, 5000) : null;

      const shutdown = async () => {
        if (!useDashboard) console.log('\nShutting down monitor...');
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
 * Scan-once command - runs a single scan without starting the full trader.
 */
program
  .command('scan-once')
  .description('Run a single market scan without starting the full trader')
  .option('--concurrency <num>', 'Number of markets to process in parallel (default: 20)', '20')
  .action(async (options) => {
    try {
      initDatabase();
      await initializeTables();

      const config = loadConfig();
      if (options.concurrency) config.scanConcurrency = parseInt(options.concurrency);

      const { PolymarketClient } = await import('./apiClient');
      const { MarketScanner } = await import('./noPaperTrader/scanner');

      await initializePortfolio(config.initialCapital);

      const client = new PolymarketClient();
      const scanner = new MarketScanner(client, config, config.scanConcurrency);

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

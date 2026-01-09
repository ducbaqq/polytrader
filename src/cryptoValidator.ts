/**
 * Crypto Reactive Trader - Entry Point
 *
 * Monitors Binance crypto prices and trades Polymarket crypto threshold markets.
 *
 * Usage:
 *   npm run crypto          - Start the trader with dashboard
 *   npm run crypto -- test  - Run tests only
 */

import * as dotenv from 'dotenv';
dotenv.config();

import chalk from 'chalk';
import {
  getCryptoReactiveTrader,
  resetCryptoReactiveTrader,
  CryptoReactiveTrader,
  CryptoDashboardData,
  CryptoAsset,
  CryptoPosition,
  CryptoOpportunity,
  CryptoMarket,
} from './crypto';
import { formatPrice, formatPolyPrice, formatPercent, DASHBOARD_CONFIG } from './crypto/cryptoConfig';
import { testExpectedPrices, simulateMispricing } from './crypto/mispricingDetector';
import { testThresholdExtraction } from './crypto/marketDiscovery';
import { initDatabase } from './database';
import * as cryptoRepo from './database/cryptoRepo';

// ============================================================================
// Dashboard Display
// ============================================================================

function clearScreen(): void {
  process.stdout.write('\x1B[2J\x1B[0f');
}

function renderDashboard(data: CryptoDashboardData): void {
  clearScreen();

  const width = 80;
  const border = chalk.cyan('║');
  const topLine = chalk.cyan('╔' + '═'.repeat(width - 2) + '╗');
  const midLine = chalk.cyan('╠' + '═'.repeat(width - 2) + '╣');
  const botLine = chalk.cyan('╚' + '═'.repeat(width - 2) + '╝');

  const pad = (text: string, len: number) => text.padEnd(len).substring(0, len);
  const row = (content: string) => border + ' ' + pad(content, width - 4) + ' ' + border;

  // Header
  console.log(topLine);
  console.log(
    row(chalk.bold.white('                CRYPTO REACTIVE TRADER                '))
  );
  console.log(
    row(
      `Status: ${
        data.isConnected ? chalk.green('● CONNECTED') : chalk.red('● DISCONNECTED')
      }    Last Update: ${data.lastUpdate.toLocaleTimeString()}`
    )
  );

  // Binance Prices
  console.log(midLine);
  console.log(row(chalk.bold.yellow('BINANCE PRICES')));

  const assets: CryptoAsset[] = ['BTC', 'ETH', 'SOL'];
  for (const asset of assets) {
    const price = data.prices.get(asset);
    if (price) {
      const change1m = formatPercent(price.change1m);
      const change5m = formatPercent(price.change5m);
      const change1mColor = price.change1m >= 0 ? chalk.green : chalk.red;
      const change5mColor = price.change5m >= 0 ? chalk.green : chalk.red;

      console.log(
        row(
          `├─ ${chalk.bold(asset)}: ${chalk.white(formatPrice(price.price, asset))}    ` +
            `1m: ${change1mColor(change1m)}    5m: ${change5mColor(change5m)}`
        )
      );
    } else {
      console.log(row(`├─ ${asset}: ${chalk.gray('N/A')}`));
    }
  }

  // Tracked Markets
  console.log(midLine);
  console.log(
    row(
      chalk.bold.yellow(`TRACKED MARKETS: ${data.trackedMarkets.length}`)
    )
  );

  const displayMarkets = data.trackedMarkets.slice(0, 5);
  for (const market of displayMarkets) {
    const direction = market.direction === 'ABOVE' ? '>' : '<';
    const threshold = `$${market.threshold.toLocaleString()}`;
    const vol = `$${(market.volume24h / 1000).toFixed(0)}K`;
    const expected = market.expectedPrice
      ? formatPolyPrice(market.expectedPrice)
      : 'N/A';
    const whitelisted = market.isWhitelisted ? chalk.green('✓') : ' ';

    console.log(
      row(
        `${whitelisted} ${chalk.bold(market.asset)} ${direction}${threshold} ` +
          `(Vol: ${vol}) Expected: ${expected}`
      )
    );
  }

  if (data.trackedMarkets.length > 5) {
    console.log(row(chalk.gray(`... and ${data.trackedMarkets.length - 5} more`)));
  }

  // Active Positions
  console.log(midLine);
  const positionLimit = data.riskState.cooldowns.size;
  console.log(
    row(
      chalk.bold.yellow(
        `ACTIVE POSITIONS: ${data.activePositions.length} / ${3}`
      )
    )
  );

  if (data.activePositions.length === 0) {
    console.log(row(chalk.gray('  No open positions')));
  } else {
    for (const pos of data.activePositions) {
      const holdTime = Math.floor(
        (Date.now() - pos.entryTime.getTime()) / 1000
      );
      const pnlPct = pos.unrealizedPnl
        ? ((pos.unrealizedPnl / (pos.quantity * pos.entryPrice)) * 100).toFixed(1)
        : '0.0';
      const pnlColor = parseFloat(pnlPct) >= 0 ? chalk.green : chalk.red;
      const value = (pos.quantity * pos.entryPrice).toFixed(2);

      console.log(
        row(
          `├─ ${pos.asset} ${pos.side} @ ${formatPolyPrice(pos.entryPrice)} ` +
            `Size: $${value}  P&L: ${pnlColor(pnlPct + '%')}  Hold: ${holdTime}s`
        )
      );
    }
  }

  // Risk Status
  console.log(midLine);
  console.log(row(chalk.bold.yellow('RISK STATUS')));

  const exposurePct =
    (data.riskState.totalExposure / 1500) * 100;
  const exposureBar = renderBar(exposurePct, 20);
  console.log(
    row(
      `├─ Exposure: $${data.riskState.totalExposure.toFixed(0)} / $1500  ${exposureBar}`
    )
  );

  const pnlColor = data.riskState.dailyPnl >= 0 ? chalk.green : chalk.red;
  console.log(
    row(`├─ Daily P&L: ${pnlColor('$' + data.riskState.dailyPnl.toFixed(2))}`)
  );

  console.log(
    row(
      `└─ Trades: ${data.riskState.dailyTrades} / 20    Cooldowns: ${data.riskState.cooldowns.size}`
    )
  );

  // Recent Opportunities
  console.log(midLine);
  console.log(
    row(chalk.bold.yellow(`RECENT OPPORTUNITIES (last ${DASHBOARD_CONFIG.maxRecentOpportunities})`))
  );

  if (data.recentOpportunities.length === 0) {
    console.log(row(chalk.gray('  No opportunities detected yet')));
  } else {
    for (const opp of data.recentOpportunities.slice(0, 5)) {
      const time = opp.detectedAt.toLocaleTimeString();
      const gap = (opp.gapPercent * 100).toFixed(1);
      const statusIcon =
        opp.status === 'EXECUTED'
          ? chalk.green('✓')
          : opp.status === 'SKIPPED'
          ? chalk.yellow('○')
          : chalk.gray('·');

      console.log(
        row(
          `${statusIcon} ${time}  ${opp.asset} ${opp.side} Gap: ${gap}%  ${opp.status}`
        )
      );
    }
  }

  console.log(botLine);
  console.log(chalk.gray('Press Ctrl+C to stop'));
}

function renderBar(percent: number, width: number): string {
  const filled = Math.round((percent / 100) * width);
  const empty = width - filled;
  const color = percent > 80 ? chalk.red : percent > 50 ? chalk.yellow : chalk.green;
  return `[${color('█'.repeat(filled))}${chalk.gray('░'.repeat(empty))}]`;
}

// ============================================================================
// Main Runner
// ============================================================================

async function runDashboard(trader: CryptoReactiveTrader): Promise<void> {
  // Refresh dashboard every second
  setInterval(async () => {
    try {
      const data = await trader.getDashboardData();
      renderDashboard(data);
    } catch (error) {
      console.error('Dashboard error:', error);
    }
  }, DASHBOARD_CONFIG.refreshIntervalMs);
}

async function main(): Promise<void> {
  const args = process.argv.slice(2);

  // Handle test mode
  if (args.includes('test')) {
    console.log('\n=== CRYPTO TRADER TESTS ===\n');
    testThresholdExtraction();
    testExpectedPrices();
    simulateMispricing();
    return;
  }

  console.log('\n====================================');
  console.log('  CRYPTO REACTIVE TRADER');
  console.log('====================================\n');

  // Initialize database
  console.log('Initializing database...');
  initDatabase();

  // Create and start trader
  const trader = getCryptoReactiveTrader();

  // Handle shutdown
  process.on('SIGINT', async () => {
    console.log('\n\nShutting down...');
    await trader.stop();
    resetCryptoReactiveTrader();
    process.exit(0);
  });

  process.on('SIGTERM', async () => {
    await trader.stop();
    resetCryptoReactiveTrader();
    process.exit(0);
  });

  // Handle events
  trader.on('opportunity', (opp: CryptoOpportunity) => {
    // Will be shown in dashboard
  });

  trader.on('positionOpened', (pos: CryptoPosition) => {
    console.log(
      chalk.green(
        `\n[TRADE] Opened ${pos.side} ${pos.asset} @ ${formatPolyPrice(pos.entryPrice)}\n`
      )
    );
  });

  trader.on('error', (error: Error) => {
    console.error(chalk.red('[ERROR]'), error.message);
  });

  try {
    // Start trader
    await trader.start();

    // Start dashboard
    await runDashboard(trader);

    // Keep process running
    await new Promise(() => {});
  } catch (error) {
    console.error('Failed to start:', error);
    process.exit(1);
  }
}

// Run
main().catch(console.error);

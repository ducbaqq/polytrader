/**
 * Performance report generator for the No-betting paper trading strategy.
 */

import Table from 'cli-table3';
import chalk from 'chalk';
import { PerformanceReport, CategoryPerformance, Position } from './types';
import {
  getPortfolio,
  getOpenPositions,
  getClosedPositions,
  getDailySnapshots,
} from './repository';

type TradeInfo = { marketId: string; question: string; pnl: number; pnlPercent: number };

/**
 * Generate a full performance report.
 */
export async function generateReport(): Promise<PerformanceReport | null> {
  const portfolio = await getPortfolio();
  if (!portfolio) {
    console.log('No portfolio data found');
    return null;
  }

  const openPositions = await getOpenPositions();
  const closedPositions = await getClosedPositions();
  const dailySnapshots = await getDailySnapshots();

  const allPositions = [...openPositions, ...closedPositions];
  if (allPositions.length === 0) {
    console.log('No positions found');
    return null;
  }

  // Calculate period
  const sortedByEntry = allPositions.sort((a, b) => a.entryTime.getTime() - b.entryTime.getTime());
  const periodStart = sortedByEntry[0].entryTime;
  const periodEnd = new Date();
  const msPerDay = 1000 * 60 * 60 * 24;
  const daysActive = Math.max(1, Math.ceil((periodEnd.getTime() - periodStart.getTime()) / msPerDay));

  // Find best and worst trades
  const positionsWithPnl = closedPositions.filter(p => p.realizedPnl !== undefined);
  const toTradeInfo = (p: Position): TradeInfo => ({
    marketId: p.marketId,
    question: p.question,
    pnl: p.realizedPnl!,
    pnlPercent: p.realizedPnlPercent || 0,
  });

  let bestTrade: TradeInfo | null = null;
  let worstTrade: TradeInfo | null = null;

  if (positionsWithPnl.length > 0) {
    const sorted = [...positionsWithPnl].sort((a, b) => b.realizedPnl! - a.realizedPnl!);
    bestTrade = toTradeInfo(sorted[0]);
    worstTrade = toTradeInfo(sorted[sorted.length - 1]);
  }

  // Calculate category performance
  const categoryPerformance = calculateCategoryPerformance(closedPositions);

  // Equity curve
  const equityCurve = dailySnapshots.map(s => ({
    date: s.date,
    equity: s.endingEquity,
  }));

  const report: PerformanceReport = {
    reportDate: new Date(),
    periodStart,
    periodEnd,
    daysActive,
    initialCapital: portfolio.initialCapital,
    finalEquity: portfolio.totalEquity,
    totalPnl: portfolio.totalPnl,
    totalPnlPercent: portfolio.totalPnlPercent,
    totalTrades: portfolio.totalTrades,
    winningTrades: portfolio.winningTrades,
    losingTrades: portfolio.losingTrades,
    winRate: portfolio.winRate,
    avgPnlPerTrade: portfolio.avgPnlPerTrade,
    bestTrade,
    worstTrade,
    categoryPerformance,
    equityCurve,
    openPositions,
  };

  return report;
}

/**
 * Calculate performance by category.
 */
function calculateCategoryPerformance(positions: Position[]): CategoryPerformance[] {
  const categoryMap: Record<string, {
    totalTrades: number;
    winningTrades: number;
    losingTrades: number;
    totalPnl: number;
    totalEdge: number;
  }> = {};

  for (const position of positions) {
    const cat = position.category;
    if (!categoryMap[cat]) {
      categoryMap[cat] = {
        totalTrades: 0,
        winningTrades: 0,
        losingTrades: 0,
        totalPnl: 0,
        totalEdge: 0,
      };
    }

    categoryMap[cat].totalTrades++;
    categoryMap[cat].totalPnl += position.realizedPnl || 0;
    categoryMap[cat].totalEdge += position.estimatedEdge;

    if ((position.realizedPnl || 0) > 0) {
      categoryMap[cat].winningTrades++;
    } else {
      categoryMap[cat].losingTrades++;
    }
  }

  return Object.entries(categoryMap).map(([category, data]) => ({
    category,
    totalTrades: data.totalTrades,
    winningTrades: data.winningTrades,
    losingTrades: data.losingTrades,
    winRate: data.totalTrades > 0 ? (data.winningTrades / data.totalTrades) * 100 : 0,
    totalPnl: data.totalPnl,
    avgPnlPerTrade: data.totalTrades > 0 ? data.totalPnl / data.totalTrades : 0,
    avgEdge: data.totalTrades > 0 ? (data.totalEdge / data.totalTrades) * 100 : 0,
  }));
}

/**
 * Print the performance report to console.
 */
export function printReport(report: PerformanceReport): void {
  console.log('\n');
  console.log(chalk.bold.blue('═══════════════════════════════════════════════════════════════'));
  console.log(chalk.bold.blue('                    PAPER TRADING REPORT'));
  console.log(chalk.bold.blue('                  No-Betting Strategy'));
  console.log(chalk.bold.blue('═══════════════════════════════════════════════════════════════'));
  console.log('\n');

  // Summary
  console.log(chalk.bold.white('📊 SUMMARY'));
  console.log('─'.repeat(60));

  const summaryTable = new Table({
    chars: { 'mid': '', 'left-mid': '', 'mid-mid': '', 'right-mid': '' },
    style: { 'padding-left': 2 },
  });

  summaryTable.push(
    [chalk.gray('Period'), `${report.periodStart.toISOString().split('T')[0]} → ${report.periodEnd.toISOString().split('T')[0]}`],
    [chalk.gray('Days Active'), `${report.daysActive}`],
    [chalk.gray('Initial Capital'), `$${report.initialCapital.toFixed(2)}`],
    [chalk.gray('Final Equity'), `$${report.finalEquity.toFixed(2)}`],
    [chalk.gray('Total P&L'), colorPnl(report.totalPnl, `$${report.totalPnl.toFixed(2)} (${report.totalPnlPercent.toFixed(2)}%)`)],
  );

  console.log(summaryTable.toString());
  console.log('\n');

  // Trade Statistics
  console.log(chalk.bold.white('📈 TRADE STATISTICS'));
  console.log('─'.repeat(60));

  const tradeTable = new Table({
    chars: { 'mid': '', 'left-mid': '', 'mid-mid': '', 'right-mid': '' },
    style: { 'padding-left': 2 },
  });

  tradeTable.push(
    [chalk.gray('Total Trades'), `${report.totalTrades}`],
    [chalk.gray('Winning Trades'), chalk.green(`${report.winningTrades}`)],
    [chalk.gray('Losing Trades'), chalk.red(`${report.losingTrades}`)],
    [chalk.gray('Win Rate'), `${report.winRate.toFixed(1)}%`],
    [chalk.gray('Avg P&L/Trade'), colorPnl(report.avgPnlPerTrade, `$${report.avgPnlPerTrade.toFixed(2)}`)],
  );

  console.log(tradeTable.toString());
  console.log('\n');

  // Best/Worst Trades
  console.log(chalk.bold.white('🏆 BEST & WORST TRADES'));
  console.log('─'.repeat(60));

  if (report.bestTrade) {
    console.log(chalk.green(`  Best:  $${report.bestTrade.pnl.toFixed(2)} (${report.bestTrade.pnlPercent.toFixed(1)}%)`));
    console.log(chalk.gray(`         ${report.bestTrade.question.substring(0, 55)}...`));
  }
  if (report.worstTrade) {
    console.log(chalk.red(`  Worst: $${report.worstTrade.pnl.toFixed(2)} (${report.worstTrade.pnlPercent.toFixed(1)}%)`));
    console.log(chalk.gray(`         ${report.worstTrade.question.substring(0, 55)}...`));
  }
  console.log('\n');

  // Category Performance
  if (report.categoryPerformance.length > 0) {
    console.log(chalk.bold.white('📁 PERFORMANCE BY CATEGORY'));
    console.log('─'.repeat(60));

    const catTable = new Table({
      head: [
        chalk.white('Category'),
        chalk.white('Trades'),
        chalk.white('Win Rate'),
        chalk.white('Total P&L'),
        chalk.white('Avg P&L'),
        chalk.white('Avg Edge'),
      ],
      colWidths: [15, 8, 10, 12, 10, 10],
    });

    for (const cat of report.categoryPerformance) {
      catTable.push([
        cat.category,
        cat.totalTrades.toString(),
        `${cat.winRate.toFixed(1)}%`,
        colorPnl(cat.totalPnl, `$${cat.totalPnl.toFixed(2)}`),
        colorPnl(cat.avgPnlPerTrade, `$${cat.avgPnlPerTrade.toFixed(2)}`),
        `${cat.avgEdge.toFixed(1)}%`,
      ]);
    }

    console.log(catTable.toString());
    console.log('\n');
  }

  // Open Positions
  if (report.openPositions.length > 0) {
    console.log(chalk.bold.white('📂 OPEN POSITIONS'));
    console.log('─'.repeat(60));

    const posTable = new Table({
      head: [
        chalk.white('Market'),
        chalk.white('Category'),
        chalk.white('Entry'),
        chalk.white('Edge'),
        chalk.white('Size'),
        chalk.white('Resolves'),
      ],
      colWidths: [35, 15, 8, 8, 10, 12],
    });

    for (const pos of report.openPositions) {
      posTable.push([
        pos.question.substring(0, 32) + '...',
        pos.category,
        `${(pos.entryPrice * 100).toFixed(1)}%`,
        `${(pos.estimatedEdge * 100).toFixed(1)}%`,
        `$${pos.costBasis.toFixed(0)}`,
        pos.endDate.toISOString().split('T')[0],
      ]);
    }

    console.log(posTable.toString());
    console.log('\n');
  }

  // Equity Curve (text-based)
  if (report.equityCurve.length > 0) {
    console.log(chalk.bold.white('📉 EQUITY CURVE'));
    console.log('─'.repeat(60));

    const minEquity = Math.min(...report.equityCurve.map(e => e.equity));
    const maxEquity = Math.max(...report.equityCurve.map(e => e.equity));
    const range = maxEquity - minEquity || 1;

    for (const point of report.equityCurve.slice(-14)) { // Last 14 days
      const barLength = Math.round(((point.equity - minEquity) / range) * 30) + 1;
      const bar = '█'.repeat(barLength);
      const color = point.equity >= report.initialCapital ? chalk.green : chalk.red;
      console.log(`  ${point.date} ${color(bar)} $${point.equity.toFixed(0)}`);
    }
    console.log('\n');
  }

  console.log(chalk.bold.blue('═══════════════════════════════════════════════════════════════'));
}

/**
 * Color a P&L value.
 */
function colorPnl(value: number, text: string): string {
  if (value > 0) return chalk.green(text);
  if (value < 0) return chalk.red(text);
  return chalk.gray(text);
}

/**
 * Print current portfolio status (quick view).
 */
export async function printStatus(): Promise<void> {
  const portfolio = await getPortfolio();
  if (!portfolio) {
    console.log('No portfolio data found. Run "no-trader start" first.');
    return;
  }

  const openPositions = await getOpenPositions();

  console.log('\n');
  console.log(chalk.bold.blue('═══════════════════════════════════════════════════════════════'));
  console.log(chalk.bold.blue('                    PORTFOLIO STATUS'));
  console.log(chalk.bold.blue('═══════════════════════════════════════════════════════════════'));
  console.log('\n');

  const table = new Table({
    chars: { 'mid': '', 'left-mid': '', 'mid-mid': '', 'right-mid': '' },
    style: { 'padding-left': 2 },
  });

  table.push(
    [chalk.gray('Cash Balance'), `$${portfolio.cashBalance.toFixed(2)}`],
    [chalk.gray('Open Positions'), `${portfolio.openPositionCount} ($${portfolio.openPositionValue.toFixed(2)})`],
    [chalk.gray('Total Equity'), `$${portfolio.totalEquity.toFixed(2)}`],
    ['', ''],
    [chalk.gray('Realized P&L'), colorPnl(portfolio.realizedPnl, `$${portfolio.realizedPnl.toFixed(2)}`)],
    [chalk.gray('Total P&L'), colorPnl(portfolio.totalPnl, `$${portfolio.totalPnl.toFixed(2)} (${portfolio.totalPnlPercent.toFixed(2)}%)`)],
    ['', ''],
    [chalk.gray('Total Trades'), `${portfolio.totalTrades}`],
    [chalk.gray('Win Rate'), `${portfolio.winRate.toFixed(1)}%`],
  );

  console.log(table.toString());

  if (openPositions.length > 0) {
    console.log('\n');
    console.log(chalk.bold.white('Open Positions:'));
    for (const pos of openPositions) {
      const daysLeft = Math.ceil((pos.endDate.getTime() - Date.now()) / (1000 * 60 * 60 * 24));
      console.log(chalk.gray(`  • ${pos.question.substring(0, 50)}... (${pos.category}, ${daysLeft}d left)`));
    }
  }

  console.log('\n');
}

/**
 * Report generator - creates validation summary and human-readable reports.
 */

import * as fs from 'fs';
import * as path from 'path';
import { query, queryRows, queryOne } from '../database/index';
import { getOpportunityStats } from '../database/opportunityRepo';
import { getTotalTradeStats, getLatestPnL, getPnLHistory } from '../database/paperTradingRepo';
import { getScanCount } from '../database/marketRepo';
import { getBestTradingHours } from './timeAnalyzer';
import { getBestCategories, getWorstCategories } from './categoryAnalyzer';
import { ValidationSummary, Recommendation } from '../types';

/**
 * Generate the final validation report.
 */
export async function generateValidationReport(
  daysAnalyzed: number = 7,
  outputDir: string = './reports'
): Promise<ValidationSummary> {
  console.log('Generating validation report...');

  // Ensure output directory exists
  if (!fs.existsSync(outputDir)) {
    fs.mkdirSync(outputDir, { recursive: true });
  }

  // Gather all metrics
  const scanCount = await getScanCount();
  const opStats = await getOpportunityStats();
  const tradeStats = await getTotalTradeStats();
  const latestPnL = await getLatestPnL();
  const bestHours = await getBestTradingHours(daysAnalyzed);
  const bestCategories = await getBestCategories(daysAnalyzed);
  const worstCategories = await getWorstCategories(daysAnalyzed);

  // Get arbitrage-specific stats
  const arbStats = await queryOne<{
    count: string;
    avg_duration: string;
    best_profit: string;
  }>(
    `SELECT
       COUNT(*) as count,
       AVG(duration_seconds) as avg_duration,
       MAX(theoretical_profit_usd) as best_profit
     FROM opportunities
     WHERE opportunity_type = 'ARBITRAGE'`
  );

  // Get order stats
  const orderStats = await queryOne<{
    total_orders: string;
    total_fills: string;
  }>(
    `SELECT
       COUNT(*) as total_orders,
       SUM(CASE WHEN status = 'FILLED' THEN 1 ELSE 0 END) as total_fills
     FROM paper_orders`
  );

  // Get cost breakdown
  const costBreakdown = await queryOne<{
    platform_fees: string;
    gas_costs: string;
    slippage_costs: string;
  }>(
    `SELECT
       SUM(platform_fee) as platform_fees,
       SUM(gas_cost) as gas_costs,
       SUM(slippage_cost) as slippage_costs
     FROM paper_trades`
  );

  // Get daily P&L for risk metrics
  const dailyPnL = await queryRows<{
    date: string;
    total_pnl: string;
  }>(
    `SELECT
       DATE(recorded_at) as date,
       SUM(total_pnl) as total_pnl
     FROM paper_pnl
     WHERE market_id IS NULL
     GROUP BY DATE(recorded_at)
     ORDER BY date`
  );

  // Calculate risk metrics
  const pnlValues = dailyPnL.map((d) => parseFloat(d.total_pnl || '0'));
  const worstDayLoss = Math.min(...pnlValues, 0);
  const dailyPnlStdDev = calculateStdDev(pnlValues);

  // Calculate max drawdown
  let maxDrawdown = 0;
  let peak = 0;
  let cumulative = 0;
  for (const pnl of pnlValues) {
    cumulative += pnl;
    if (cumulative > peak) peak = cumulative;
    const drawdown = peak > 0 ? (peak - cumulative) / peak : 0;
    if (drawdown > maxDrawdown) maxDrawdown = drawdown;
  }

  // Get win rate
  const winStats = await queryOne<{
    wins: string;
    total: string;
  }>(
    `SELECT
       SUM(CASE WHEN net_value > 0 THEN 1 ELSE 0 END) as wins,
       COUNT(*) as total
     FROM paper_trades`
  );

  const wins = parseInt(winStats?.wins || '0', 10);
  const totalTrades = parseInt(winStats?.total || '0', 10);
  const winRate = totalTrades > 0 ? wins / totalTrades : 0;

  // Calculate metrics
  const totalOrders = parseInt(orderStats?.total_orders || '0', 10);
  const totalFills = parseInt(orderStats?.total_fills || '0', 10);
  const overallFillRate = totalOrders > 0 ? totalFills / totalOrders : 0;

  const platformFees = parseFloat(costBreakdown?.platform_fees || '0');
  const gasCosts = parseFloat(costBreakdown?.gas_costs || '0');
  const slippageCosts = parseFloat(costBreakdown?.slippage_costs || '0');

  const grossProfit = tradeStats.total_volume;
  const netProfit = tradeStats.total_cash_flow;  // Sum of all trade net_values

  const feesPctOfGross = grossProfit > 0 ? platformFees / grossProfit : 0;
  const gasPctOfGross = grossProfit > 0 ? gasCosts / grossProfit : 0;
  const totalCostsPct = grossProfit > 0 ? (platformFees + gasCosts + slippageCosts) / grossProfit : 0;

  const roiWeekly = 1000 > 0 ? netProfit / 1000 : 0;  // Assuming $1000 initial capital
  const projectedMonthly = netProfit * (30 / daysAnalyzed);

  // Determine recommendation
  let recommendation: Recommendation;
  let recommendationReason: string;
  let nextSteps: string;

  if (netProfit > 50 && overallFillRate > 0.3 && winRate > 0.4) {
    recommendation = 'BUILD_BOT';
    recommendationReason = `Net profit of $${netProfit.toFixed(2)} with ${(winRate * 100).toFixed(1)}% win rate and ${(overallFillRate * 100).toFixed(1)}% fill rate indicates viable trading strategy.`;
    nextSteps = '1. Build production trading bot\n2. Start with small capital ($100-500)\n3. Monitor for 1 week before scaling\n4. Focus on best performing hours and categories';
  } else if (netProfit > 20 || (netProfit > 0 && overallFillRate > 0.2)) {
    recommendation = 'MARGINAL';
    recommendationReason = `Net profit of $${netProfit.toFixed(2)} is marginally profitable. Consider optimizing strategy before building bot.`;
    nextSteps = '1. Analyze losing trades for patterns\n2. Adjust order pricing strategy\n3. Consider focusing on specific categories\n4. Run another week of paper trading with adjustments';
  } else {
    recommendation = 'DONT_BUILD';
    recommendationReason = `Net profit of $${netProfit.toFixed(2)} with ${(overallFillRate * 100).toFixed(1)}% fill rate does not justify building a trading bot.`;
    nextSteps = '1. Review opportunity detection thresholds\n2. Consider different market types\n3. Analyze why fills are low\n4. May need fundamentally different approach';
  }

  // Determine arbitrage verdict
  const arbCount = parseInt(arbStats?.count || '0', 10);
  const arbAvgDuration = parseFloat(arbStats?.avg_duration || '0');
  const arbBestProfit = parseFloat(arbStats?.best_profit || '0');
  const arbRealisticProfit = arbBestProfit * 0.3; // Assume 30% capture rate

  let arbitrageVerdict: string;
  if (arbCount > 10 && arbAvgDuration > 60) {
    arbitrageVerdict = 'VIABLE - Sufficient opportunities with reasonable duration';
  } else if (arbCount > 5) {
    arbitrageVerdict = 'MARGINAL - Some opportunities but may be difficult to capture';
  } else {
    arbitrageVerdict = 'NOT_VIABLE - Insufficient arbitrage opportunities detected';
  }

  // Build summary
  const summary: ValidationSummary = {
    reportDate: new Date(),
    daysAnalyzed,
    totalScans: scanCount,
    arbitrageOpportunities: arbCount,
    arbitrageAvgDurationSec: Math.round(arbAvgDuration),
    arbitrageBestCaseProfit: arbBestProfit,
    arbitrageRealisticProfit: arbRealisticProfit,
    arbitrageVerdict,
    marketsTested: 3, // From paper trading selection
    totalOrders,
    totalFills,
    overallFillRate,
    grossProfit,
    platformFees,
    gasCosts,
    slippageCosts,
    netProfit,
    roiWeekly,
    projectedMonthly,
    feesPctOfGross,
    gasPctOfGross,
    totalCostsPct,
    worstDayLoss,
    maxDrawdownPct: maxDrawdown,
    winRate,
    dailyPnlStdDev,
    bestMarketCategory: bestCategories[0]?.category || 'N/A',
    bestHours: bestHours.slice(0, 5).map((h) => h.hour),
    worstMarketCategory: worstCategories[0]?.category || 'N/A',
    recommendation,
    recommendationReason,
    nextSteps,
  };

  // Save to database
  await saveValidationSummary(summary);

  // Generate markdown report
  const markdown = generateMarkdownReport(summary, bestHours, bestCategories);
  const reportPath = path.join(outputDir, `validation_report_${new Date().toISOString().split('T')[0]}.md`);
  fs.writeFileSync(reportPath, markdown);
  console.log(`Report saved to: ${reportPath}`);

  return summary;
}

/**
 * Save validation summary to database.
 */
async function saveValidationSummary(summary: ValidationSummary): Promise<void> {
  await query(
    `INSERT INTO validation_summary (
       report_date, days_analyzed, total_scans,
       arbitrage_opportunities, arbitrage_avg_duration_sec,
       arbitrage_best_case_profit, arbitrage_realistic_profit, arbitrage_verdict,
       markets_tested, total_orders, total_fills, overall_fill_rate,
       gross_profit, platform_fees, gas_costs, slippage_costs, net_profit,
       roi_weekly, projected_monthly,
       fees_pct_of_gross, gas_pct_of_gross, total_costs_pct,
       worst_day_loss, max_drawdown_pct, win_rate, daily_pnl_std_dev,
       best_market_category, best_hours, worst_market_category,
       recommendation, recommendation_reason, next_steps
     ) VALUES (
       $1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14, $15, $16, $17, $18, $19, $20, $21, $22, $23, $24, $25, $26, $27, $28, $29, $30, $31, $32
     )`,
    [
      summary.reportDate,
      summary.daysAnalyzed,
      summary.totalScans,
      summary.arbitrageOpportunities,
      summary.arbitrageAvgDurationSec,
      summary.arbitrageBestCaseProfit,
      summary.arbitrageRealisticProfit,
      summary.arbitrageVerdict,
      summary.marketsTested,
      summary.totalOrders,
      summary.totalFills,
      summary.overallFillRate,
      summary.grossProfit,
      summary.platformFees,
      summary.gasCosts,
      summary.slippageCosts,
      summary.netProfit,
      summary.roiWeekly,
      summary.projectedMonthly,
      summary.feesPctOfGross,
      summary.gasPctOfGross,
      summary.totalCostsPct,
      summary.worstDayLoss,
      summary.maxDrawdownPct,
      summary.winRate,
      summary.dailyPnlStdDev,
      summary.bestMarketCategory,
      JSON.stringify(summary.bestHours),
      summary.worstMarketCategory,
      summary.recommendation,
      summary.recommendationReason,
      summary.nextSteps,
    ]
  );
}

/**
 * Generate markdown report.
 */
function generateMarkdownReport(
  summary: ValidationSummary,
  bestHours: { hour: number; avgNetProfit: number }[],
  bestCategories: { category: string; totalNetProfit: number }[]
): string {
  const recommendationEmoji =
    summary.recommendation === 'BUILD_BOT' ? '🚀' :
    summary.recommendation === 'MARGINAL' ? '⚠️' : '❌';

  return `# Polymarket Trading Validation Report

**Generated:** ${summary.reportDate.toISOString()}
**Analysis Period:** ${summary.daysAnalyzed} days

---

## ${recommendationEmoji} Recommendation: ${summary.recommendation}

${summary.recommendationReason}

### Next Steps
${summary.nextSteps}

---

## Executive Summary

| Metric | Value |
|--------|-------|
| Total Scans | ${summary.totalScans.toLocaleString()} |
| Net Profit | $${summary.netProfit.toFixed(2)} |
| ROI (Weekly) | ${(summary.roiWeekly * 100).toFixed(2)}% |
| Projected Monthly | $${summary.projectedMonthly.toFixed(2)} |
| Win Rate | ${(summary.winRate * 100).toFixed(1)}% |
| Fill Rate | ${(summary.overallFillRate * 100).toFixed(1)}% |

---

## Arbitrage Analysis

| Metric | Value |
|--------|-------|
| Opportunities Detected | ${summary.arbitrageOpportunities} |
| Avg Duration | ${summary.arbitrageAvgDurationSec}s |
| Best Case Profit | $${summary.arbitrageBestCaseProfit.toFixed(2)} |
| Realistic Profit (30%) | $${summary.arbitrageRealisticProfit.toFixed(2)} |
| **Verdict** | ${summary.arbitrageVerdict} |

---

## Paper Trading Results

### Orders & Fills

| Metric | Value |
|--------|-------|
| Total Orders | ${summary.totalOrders} |
| Total Fills | ${summary.totalFills} |
| Fill Rate | ${(summary.overallFillRate * 100).toFixed(1)}% |
| Markets Tested | ${summary.marketsTested} |

### Profit & Loss

| Metric | Value |
|--------|-------|
| Gross Volume | $${summary.grossProfit.toFixed(2)} |
| Platform Fees | $${summary.platformFees.toFixed(2)} (${(summary.feesPctOfGross * 100).toFixed(1)}%) |
| Gas Costs | $${summary.gasCosts.toFixed(2)} (${(summary.gasPctOfGross * 100).toFixed(1)}%) |
| Slippage Costs | $${summary.slippageCosts.toFixed(2)} |
| **Net Profit** | **$${summary.netProfit.toFixed(2)}** |

---

## Risk Metrics

| Metric | Value |
|--------|-------|
| Worst Day Loss | $${summary.worstDayLoss.toFixed(2)} |
| Max Drawdown | ${(summary.maxDrawdownPct * 100).toFixed(1)}% |
| Daily P&L Std Dev | $${summary.dailyPnlStdDev.toFixed(2)} |

---

## Best Trading Hours

${bestHours.slice(0, 5).map((h, i) =>
  `${i + 1}. **${h.hour}:00 UTC** - Avg Net Profit: $${h.avgNetProfit.toFixed(2)}`
).join('\n')}

---

## Best Categories

${bestCategories.slice(0, 5).map((c, i) =>
  `${i + 1}. **${c.category}** - Total Net Profit: $${c.totalNetProfit.toFixed(2)}`
).join('\n')}

---

## Worst Categories

- ${summary.worstMarketCategory}

---

*Report generated by Polymarket Market Validator*
`;
}

/**
 * Calculate standard deviation.
 */
function calculateStdDev(values: number[]): number {
  if (values.length === 0) return 0;
  const mean = values.reduce((a, b) => a + b, 0) / values.length;
  const squaredDiffs = values.map((v) => Math.pow(v - mean, 2));
  const variance = squaredDiffs.reduce((a, b) => a + b, 0) / values.length;
  return Math.sqrt(variance);
}

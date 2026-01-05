/**
 * Category-based pattern analyzer - analyzes performance by market category.
 */

import { query, queryRows, queryOne } from '../database/index';
import { CategoryAnalysisRow } from '../types';

/**
 * Run daily category analysis.
 */
export async function runDailyCategoryAnalysis(): Promise<void> {
  const today = new Date().toISOString().split('T')[0];

  console.log(`Running daily category analysis for ${today}...`);

  try {
    // Get categories from market snapshots
    const categories = await queryRows<{ category: string }>(
      `SELECT DISTINCT category FROM market_snapshots
       WHERE category IS NOT NULL
         AND scan_timestamp >= NOW() - INTERVAL '24 hours'`
    );

    for (const { category } of categories) {
      if (!category) continue;

      // Get opportunity stats for this category
      const opStats = await queryOne<{
        opportunities_found: string;
        avg_spread: string;
      }>(
        `SELECT
           COUNT(*) as opportunities_found,
           AVG(spread_percent) as avg_spread
         FROM opportunities o
         JOIN market_snapshots ms ON o.market_id = ms.market_id
         WHERE ms.category = $1
           AND o.detected_at >= NOW() - INTERVAL '24 hours'`,
        [category]
      );

      // Get trading stats for this category
      const tradeStats = await queryOne<{
        trades_executed: string;
        fills: string;
        orders: string;
        gross_profit: string;
        net_profit: string;
      }>(
        `SELECT
           COUNT(DISTINCT pt.id) as trades_executed,
           SUM(CASE WHEN po.status = 'FILLED' THEN 1 ELSE 0 END) as fills,
           COUNT(DISTINCT po.id) as orders,
           COALESCE(SUM(pt.value), 0) as gross_profit,
           COALESCE(SUM(pt.net_value), 0) as net_profit
         FROM paper_orders po
         LEFT JOIN paper_trades pt ON po.order_id = pt.order_id
         JOIN paper_markets pm ON po.market_id = pm.market_id
         WHERE pm.category = $1
           AND po.placed_at >= NOW() - INTERVAL '24 hours'`,
        [category]
      );

      // Get market stats for this category
      const marketStats = await queryOne<{
        avg_volume: string;
        market_count: string;
      }>(
        `SELECT
           AVG(volume_24h) as avg_volume,
           COUNT(DISTINCT market_id) as market_count
         FROM market_snapshots
         WHERE category = $1
           AND scan_timestamp >= NOW() - INTERVAL '24 hours'`,
        [category]
      );

      const opportunitiesFound = parseInt(opStats?.opportunities_found || '0', 10);
      const avgSpread = parseFloat(opStats?.avg_spread || '0');
      const tradesExecuted = parseInt(tradeStats?.trades_executed || '0', 10);
      const fills = parseInt(tradeStats?.fills || '0', 10);
      const orders = parseInt(tradeStats?.orders || '0', 10);
      const grossProfit = parseFloat(tradeStats?.gross_profit || '0');
      const netProfit = parseFloat(tradeStats?.net_profit || '0');
      const avgVolume = parseFloat(marketStats?.avg_volume || '0');
      const marketCount = parseInt(marketStats?.market_count || '0', 10);

      const fillRate = orders > 0 ? fills / orders : 0;
      const roi = grossProfit > 0 ? (netProfit / grossProfit) : 0;

      // Insert or update category analysis row
      await query(
        `INSERT INTO category_analysis (
           analysis_date, category,
           opportunities_found, avg_spread,
           trades_executed, fill_rate, gross_profit, net_profit, roi,
           avg_volume, market_count
         ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11)
         ON CONFLICT (analysis_date, category) DO UPDATE SET
           opportunities_found = $3,
           avg_spread = $4,
           trades_executed = $5,
           fill_rate = $6,
           gross_profit = $7,
           net_profit = $8,
           roi = $9,
           avg_volume = $10,
           market_count = $11`,
        [
          today,
          category,
          opportunitiesFound,
          avgSpread,
          tradesExecuted,
          fillRate,
          grossProfit,
          netProfit,
          roi,
          avgVolume,
          marketCount,
        ]
      );
    }

    console.log(`Category analysis complete: ${categories.length} categories analyzed`);
  } catch (error) {
    console.error('Daily category analysis failed:', error);
    throw error;
  }
}

/**
 * Get category analysis for a date range.
 */
export async function getCategoryAnalysis(
  startDate: Date,
  endDate: Date
): Promise<CategoryAnalysisRow[]> {
  const rows = await queryRows(
    `SELECT * FROM category_analysis
     WHERE analysis_date BETWEEN $1 AND $2
     ORDER BY analysis_date, category`,
    [startDate.toISOString().split('T')[0], endDate.toISOString().split('T')[0]]
  );

  return rows.map((r) => ({
    analysisDate: new Date(r.analysis_date),
    category: r.category,
    opportunitiesFound: r.opportunities_found || 0,
    avgSpread: parseFloat(r.avg_spread || '0'),
    tradesExecuted: r.trades_executed || 0,
    fillRate: parseFloat(r.fill_rate || '0'),
    grossProfit: parseFloat(r.gross_profit || '0'),
    netProfit: parseFloat(r.net_profit || '0'),
    roi: parseFloat(r.roi || '0'),
    avgVolume: parseFloat(r.avg_volume || '0'),
    marketCount: r.market_count || 0,
  }));
}

/**
 * Get best performing categories.
 */
export async function getBestCategories(
  daysToAnalyze: number = 7
): Promise<{ category: string; totalNetProfit: number; avgRoi: number; avgFillRate: number }[]> {
  const rows = await queryRows<{
    category: string;
    total_net_profit: string;
    avg_roi: string;
    avg_fill_rate: string;
  }>(
    `SELECT
       category,
       SUM(net_profit) as total_net_profit,
       AVG(roi) as avg_roi,
       AVG(fill_rate) as avg_fill_rate
     FROM category_analysis
     WHERE analysis_date >= CURRENT_DATE - INTERVAL '${daysToAnalyze} days'
     GROUP BY category
     ORDER BY total_net_profit DESC`
  );

  return rows.map((r) => ({
    category: r.category,
    totalNetProfit: parseFloat(r.total_net_profit || '0'),
    avgRoi: parseFloat(r.avg_roi || '0'),
    avgFillRate: parseFloat(r.avg_fill_rate || '0'),
  }));
}

/**
 * Get worst performing categories.
 */
export async function getWorstCategories(
  daysToAnalyze: number = 7
): Promise<{ category: string; totalNetProfit: number; avgRoi: number }[]> {
  const rows = await queryRows<{
    category: string;
    total_net_profit: string;
    avg_roi: string;
  }>(
    `SELECT
       category,
       SUM(net_profit) as total_net_profit,
       AVG(roi) as avg_roi
     FROM category_analysis
     WHERE analysis_date >= CURRENT_DATE - INTERVAL '${daysToAnalyze} days'
     GROUP BY category
     ORDER BY total_net_profit ASC
     LIMIT 5`
  );

  return rows.map((r) => ({
    category: r.category,
    totalNetProfit: parseFloat(r.total_net_profit || '0'),
    avgRoi: parseFloat(r.avg_roi || '0'),
  }));
}

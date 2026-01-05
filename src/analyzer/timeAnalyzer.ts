/**
 * Time-based pattern analyzer - analyzes opportunity patterns by hour of day.
 */

import { query, queryRows, queryOne } from '../database/index';
import { TimeAnalysisRow } from '../types';

/**
 * Run hourly analysis for the past hour.
 */
export async function runHourlyAnalysis(): Promise<void> {
  const now = new Date();
  const hour = now.getHours();
  const today = now.toISOString().split('T')[0];

  console.log(`Running hourly analysis for ${today} hour ${hour}...`);

  try {
    // Get opportunity counts by type for the past hour
    const opCounts = await queryRows<{
      opportunity_type: string;
      count: string;
      avg_spread: string;
    }>(
      `SELECT
         opportunity_type,
         COUNT(*) as count,
         AVG(spread_percent) as avg_spread
       FROM opportunities
       WHERE detected_at >= NOW() - INTERVAL '1 hour'
       GROUP BY opportunity_type`
    );

    let arbitrageCount = 0;
    let avgArbitrageSpread = 0;
    let wideSpreadCount = 0;
    let avgWideSpread = 0;

    for (const row of opCounts) {
      if (row.opportunity_type === 'ARBITRAGE') {
        arbitrageCount = parseInt(row.count, 10);
        avgArbitrageSpread = parseFloat(row.avg_spread || '0');
      } else if (row.opportunity_type === 'WIDE_SPREAD') {
        wideSpreadCount = parseInt(row.count, 10);
        avgWideSpread = parseFloat(row.avg_spread || '0');
      }
    }

    // Get paper trading stats for the past hour
    const orderStats = await queryOne<{
      orders_placed: string;
      orders_filled: string;
      trades_executed: string;
      gross_profit: string;
      net_profit: string;
    }>(
      `SELECT
         COUNT(DISTINCT po.id) as orders_placed,
         SUM(CASE WHEN po.status = 'FILLED' THEN 1 ELSE 0 END) as orders_filled,
         COUNT(DISTINCT pt.id) as trades_executed,
         COALESCE(SUM(pt.value), 0) as gross_profit,
         COALESCE(SUM(pt.net_value), 0) as net_profit
       FROM paper_orders po
       LEFT JOIN paper_trades pt ON po.order_id = pt.order_id
       WHERE po.placed_at >= NOW() - INTERVAL '1 hour'`
    );

    const ordersPlaced = parseInt(orderStats?.orders_placed || '0', 10);
    const ordersFilled = parseInt(orderStats?.orders_filled || '0', 10);
    const tradesExecuted = parseInt(orderStats?.trades_executed || '0', 10);
    const grossProfit = parseFloat(orderStats?.gross_profit || '0');
    const netProfit = parseFloat(orderStats?.net_profit || '0');
    const fillRate = ordersPlaced > 0 ? ordersFilled / ordersPlaced : 0;

    // Get market activity stats
    const marketStats = await queryOne<{
      avg_volume: string;
      active_markets: string;
    }>(
      `SELECT
         AVG(volume_24h) as avg_volume,
         COUNT(DISTINCT market_id) as active_markets
       FROM market_snapshots
       WHERE scan_timestamp >= NOW() - INTERVAL '1 hour'`
    );

    const avgVolume = parseFloat(marketStats?.avg_volume || '0');
    const activeMarkets = parseInt(marketStats?.active_markets || '0', 10);

    // Insert or update time analysis row
    await query(
      `INSERT INTO time_analysis (
         analysis_date, hour_of_day,
         arbitrage_count, avg_arbitrage_spread,
         wide_spread_count, avg_wide_spread,
         orders_placed, orders_filled, fill_rate,
         trades_executed, gross_profit, net_profit,
         avg_volume, active_markets
       ) VALUES ($1, $2, $3, $4, $5, $6, $7, $8, $9, $10, $11, $12, $13, $14)
       ON CONFLICT (analysis_date, hour_of_day) DO UPDATE SET
         arbitrage_count = $3,
         avg_arbitrage_spread = $4,
         wide_spread_count = $5,
         avg_wide_spread = $6,
         orders_placed = $7,
         orders_filled = $8,
         fill_rate = $9,
         trades_executed = $10,
         gross_profit = $11,
         net_profit = $12,
         avg_volume = $13,
         active_markets = $14`,
      [
        today,
        hour,
        arbitrageCount,
        avgArbitrageSpread,
        wideSpreadCount,
        avgWideSpread,
        ordersPlaced,
        ordersFilled,
        fillRate,
        tradesExecuted,
        grossProfit,
        netProfit,
        avgVolume,
        activeMarkets,
      ]
    );

    console.log(`Hourly analysis complete: ${arbitrageCount} arb, ${wideSpreadCount} wide spread`);
  } catch (error) {
    console.error('Hourly analysis failed:', error);
    throw error;
  }
}

/**
 * Get time analysis for a date range.
 */
export async function getTimeAnalysis(
  startDate: Date,
  endDate: Date
): Promise<TimeAnalysisRow[]> {
  const rows = await queryRows(
    `SELECT * FROM time_analysis
     WHERE analysis_date BETWEEN $1 AND $2
     ORDER BY analysis_date, hour_of_day`,
    [startDate.toISOString().split('T')[0], endDate.toISOString().split('T')[0]]
  );

  return rows.map((r) => ({
    analysisDate: new Date(r.analysis_date),
    hourOfDay: r.hour_of_day,
    arbitrageCount: r.arbitrage_count || 0,
    avgArbitrageSpread: parseFloat(r.avg_arbitrage_spread || '0'),
    wideSpreadCount: r.wide_spread_count || 0,
    avgWideSpread: parseFloat(r.avg_wide_spread || '0'),
    ordersPlaced: r.orders_placed || 0,
    ordersFilled: r.orders_filled || 0,
    fillRate: parseFloat(r.fill_rate || '0'),
    tradesExecuted: r.trades_executed || 0,
    grossProfit: parseFloat(r.gross_profit || '0'),
    netProfit: parseFloat(r.net_profit || '0'),
    avgVolume: parseFloat(r.avg_volume || '0'),
    activeMarkets: r.active_markets || 0,
  }));
}

/**
 * Get best trading hours based on historical analysis.
 */
export async function getBestTradingHours(
  daysToAnalyze: number = 7
): Promise<{ hour: number; avgNetProfit: number; avgFillRate: number }[]> {
  const rows = await queryRows<{
    hour_of_day: number;
    avg_net_profit: string;
    avg_fill_rate: string;
  }>(
    `SELECT
       hour_of_day,
       AVG(net_profit) as avg_net_profit,
       AVG(fill_rate) as avg_fill_rate
     FROM time_analysis
     WHERE analysis_date >= CURRENT_DATE - INTERVAL '${daysToAnalyze} days'
     GROUP BY hour_of_day
     ORDER BY avg_net_profit DESC`
  );

  return rows.map((r) => ({
    hour: r.hour_of_day,
    avgNetProfit: parseFloat(r.avg_net_profit || '0'),
    avgFillRate: parseFloat(r.avg_fill_rate || '0'),
  }));
}

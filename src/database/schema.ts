/**
 * Database schema validation and table existence checks.
 */

import { query, queryRows } from './index';

const REQUIRED_TABLES = [
  'market_snapshots',
  'order_book_snapshots',
  'opportunities',
  'paper_markets',
  'paper_orders',
  'paper_trades',
  'paper_positions',
  'paper_pnl',
  'time_analysis',
  'category_analysis',
  'validation_summary',
];

/**
 * Verify all required tables exist.
 */
export async function verifySchema(): Promise<{ valid: boolean; missing: string[] }> {
  const result = await queryRows<{ tablename: string }>(
    `SELECT tablename FROM pg_tables WHERE schemaname = 'public'`
  );

  const existingTables = new Set(result.map((r) => r.tablename));
  const missing = REQUIRED_TABLES.filter((t) => !existingTables.has(t));

  return {
    valid: missing.length === 0,
    missing,
  };
}

/**
 * Get table row counts for monitoring.
 */
export async function getTableCounts(): Promise<Record<string, number>> {
  const counts: Record<string, number> = {};

  for (const table of REQUIRED_TABLES) {
    try {
      const result = await query(`SELECT COUNT(*) as count FROM ${table}`);
      counts[table] = parseInt(result.rows[0].count, 10);
    } catch {
      counts[table] = -1; // Table doesn't exist
    }
  }

  return counts;
}

/**
 * Clear all data from tables (for reset).
 */
export async function clearAllData(): Promise<void> {
  // Clear in order respecting foreign keys
  const clearOrder = [
    'paper_pnl',
    'paper_trades',
    'paper_positions',
    'paper_orders',
    'paper_markets',
    'validation_summary',
    'category_analysis',
    'time_analysis',
    'opportunities',
    'order_book_snapshots',
    'market_snapshots',
  ];

  for (const table of clearOrder) {
    try {
      await query(`TRUNCATE TABLE ${table} CASCADE`);
      console.log(`Cleared table: ${table}`);
    } catch (error) {
      console.error(`Error clearing ${table}:`, error);
    }
  }
}

/**
 * Get database size info.
 */
export async function getDatabaseSize(): Promise<{
  total_size: string;
  table_sizes: Record<string, string>;
}> {
  const totalSize = await query(
    `SELECT pg_size_pretty(pg_database_size(current_database())) as size`
  );

  const tableSizes = await queryRows<{ table_name: string; size: string }>(
    `SELECT
       relname as table_name,
       pg_size_pretty(pg_total_relation_size(relid)) as size
     FROM pg_catalog.pg_statio_user_tables
     ORDER BY pg_total_relation_size(relid) DESC`
  );

  const sizes: Record<string, string> = {};
  for (const row of tableSizes) {
    sizes[row.table_name] = row.size;
  }

  return {
    total_size: totalSize.rows[0].size,
    table_sizes: sizes,
  };
}

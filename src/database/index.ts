/**
 * Database connection pool and utilities for PostgreSQL.
 */

import { Pool, PoolClient, QueryResult } from 'pg';

let pool: Pool | null = null;

/**
 * Initialize the database connection pool.
 */
export function initDatabase(): Pool {
  if (pool) {
    return pool;
  }

  const connectionString = process.env.DATABASE_URL;
  if (!connectionString) {
    throw new Error('DATABASE_URL environment variable is not set');
  }

  pool = new Pool({
    connectionString,
    max: 20,
    idleTimeoutMillis: 30000,
    connectionTimeoutMillis: 10000,
    ssl: {
      rejectUnauthorized: false,
    },
  });

  pool.on('error', (err) => {
    console.error('Unexpected database pool error:', err);
  });

  console.log('Database pool initialized');
  return pool;
}

/**
 * Get the database pool.
 */
export function getPool(): Pool {
  if (!pool) {
    return initDatabase();
  }
  return pool;
}

/**
 * Execute a query with automatic connection handling.
 */
export async function query(
  text: string,
  params?: any[]
): Promise<QueryResult<any>> {
  const p = getPool();
  return p.query(text, params);
}

/**
 * Execute a query and return rows.
 */
export async function queryRows<T = any>(
  text: string,
  params?: any[]
): Promise<T[]> {
  const result = await query(text, params);
  return result.rows as T[];
}

/**
 * Execute a query and return first row or null.
 */
export async function queryOne<T = any>(
  text: string,
  params?: any[]
): Promise<T | null> {
  const result = await query(text, params);
  return (result.rows[0] as T) || null;
}

/**
 * Execute multiple queries in a transaction.
 */
export async function withTransaction<T>(
  fn: (client: PoolClient) => Promise<T>
): Promise<T> {
  const p = getPool();
  const client = await p.connect();

  try {
    await client.query('BEGIN');
    const result = await fn(client);
    await client.query('COMMIT');
    return result;
  } catch (error) {
    await client.query('ROLLBACK');
    throw error;
  } finally {
    client.release();
  }
}

/**
 * Batch insert helper for efficient bulk inserts.
 */
export async function batchInsert(
  client: PoolClient,
  table: string,
  columns: string[],
  rows: any[][],
  batchSize: number = 100
): Promise<number> {
  if (rows.length === 0) return 0;

  let inserted = 0;

  for (let i = 0; i < rows.length; i += batchSize) {
    const batch = rows.slice(i, i + batchSize);
    const placeholders: string[] = [];
    const values: any[] = [];

    batch.forEach((row, rowIndex) => {
      const rowPlaceholders = columns.map(
        (_, colIndex) => `$${rowIndex * columns.length + colIndex + 1}`
      );
      placeholders.push(`(${rowPlaceholders.join(', ')})`);
      values.push(...row);
    });

    const sql = `
      INSERT INTO ${table} (${columns.join(', ')})
      VALUES ${placeholders.join(', ')}
    `;

    await client.query(sql, values);
    inserted += batch.length;
  }

  return inserted;
}

/**
 * Close the database pool.
 */
export async function closeDatabase(): Promise<void> {
  if (pool) {
    await pool.end();
    pool = null;
    console.log('Database pool closed');
  }
}

export { Pool, PoolClient };

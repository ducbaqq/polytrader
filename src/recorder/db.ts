/**
 * Small Postgres layer for the recorder: market catalog, per-minute stats, events.
 * Tick data does NOT go to Postgres; it goes to compressed files (see sink.ts).
 */

import * as fs from 'fs';
import { URL } from 'url';
import { Pool } from 'pg';

let pool: Pool | null = null;
let disabled = false;

export function getPool(): Pool | null {
  if (pool || disabled) return pool;
  const raw = process.env.DATABASE_URL;
  if (!raw) {
    console.warn('[db] DATABASE_URL not set; running without Postgres (files only)');
    disabled = true;
    return null;
  }
  // Configure TLS explicitly: strip ssl params from the URL so they can't override the CA settings.
  const url = new URL(raw);
  ['sslmode', 'sslrootcert', 'sslcert', 'sslkey'].forEach((k) => url.searchParams.delete(k));
  const caPath = process.env.DATABASE_CA_CERT;
  pool = new Pool({
    connectionString: url.toString(),
    max: 3,
    idleTimeoutMillis: 30_000,
    connectionTimeoutMillis: 10_000,
    ssl: caPath ? { ca: fs.readFileSync(caPath, 'utf8'), rejectUnauthorized: true } : { rejectUnauthorized: true },
  });
  pool.on('error', (e) => console.error('[db] pool error:', e.message));
  return pool;
}

export async function migrate(): Promise<void> {
  const p = getPool();
  if (!p) return;
  await p.query(`
    CREATE TABLE IF NOT EXISTS rec_markets (
      slug            text PRIMARY KEY,
      series_slug     text NOT NULL,
      coin            text NOT NULL,
      recurrence      text NOT NULL,
      condition_id    text NOT NULL,
      up_token_id     text NOT NULL,
      down_token_id   text NOT NULL,
      window_start    timestamptz NOT NULL,
      window_end      timestamptz NOT NULL,
      resolution_source text,
      twap_lookback_s integer,
      fee_schedule    jsonb,
      tick_size       numeric,
      min_order_size  numeric,
      subscribed_at   timestamptz,
      unsubscribed_at timestamptz,
      price_to_beat   numeric,
      outcome         text,
      resolved_at     timestamptz,
      final_volume    numeric,
      api_trades      integer,
      discovered_at   timestamptz NOT NULL DEFAULT now(),
      updated_at      timestamptz NOT NULL DEFAULT now()
    );
    CREATE INDEX IF NOT EXISTS rec_markets_window_end ON rec_markets (window_end);
    CREATE INDEX IF NOT EXISTS rec_markets_series ON rec_markets (series_slug, window_start);

    CREATE TABLE IF NOT EXISTS rec_stats (
      minute   timestamptz NOT NULL,
      stream   text NOT NULL,
      rows     integer NOT NULL,
      bytes    bigint NOT NULL,
      pending  bigint NOT NULL DEFAULT 0,
      PRIMARY KEY (minute, stream)
    );

    CREATE TABLE IF NOT EXISTS rec_events (
      ts      timestamptz NOT NULL DEFAULT now(),
      kind    text NOT NULL,
      source  text NOT NULL,
      detail  text
    );
    CREATE INDEX IF NOT EXISTS rec_events_ts ON rec_events (ts);
  `);
}

/** Fire-and-forget query; failures are logged, never thrown into the data path. */
export function bg(sql: string, params: unknown[]): void {
  const p = getPool();
  if (!p) return;
  p.query(sql, params).catch((e) => console.error('[db]', e.message, '|', sql.slice(0, 80)));
}

export async function closeDb(): Promise<void> {
  await pool?.end();
}

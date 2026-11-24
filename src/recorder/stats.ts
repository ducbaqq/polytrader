/**
 * Health reporting: per-minute row counts per stream, plus notable events
 * (reconnects, sequence gaps, errors), to Postgres and stdout.
 */

import { bg } from './db';
import { drainCounters } from './sink';

export function logEvent(kind: string, source: string, detail: string): void {
  console.log(`[${new Date().toISOString()}] ${kind} ${source} ${detail}`);
  bg('INSERT INTO rec_events (kind, source, detail) VALUES ($1, $2, $3)', [kind, source, detail]);
}

let periodStart = Date.now();

/** Rows are attributed to the minute in which the reporting period started. */
export function flushStats(): void {
  const minute = new Date(Math.floor(periodStart / 60_000) * 60_000).toISOString();
  periodStart = Date.now();
  const rows = drainCounters();
  const total = rows.reduce((a, r) => a + r.rows, 0);
  const bytes = rows.reduce((a, r) => a + r.bytes, 0);
  const mem = process.memoryUsage();
  console.log(
    `[stats] ${minute} rows=${total} uncompressed=${(bytes / 1e6).toFixed(1)}MB rss=${(mem.rss / 1e6).toFixed(0)}MB ` +
      rows.filter((r) => r.rows > 0).map((r) => `${r.stream}=${r.rows}`).join(' '),
  );
  for (const r of rows) {
    bg(
      `INSERT INTO rec_stats (minute, stream, rows, bytes, pending) VALUES ($1, $2, $3, $4, $5)
       ON CONFLICT (minute, stream) DO UPDATE SET rows = rec_stats.rows + EXCLUDED.rows, bytes = rec_stats.bytes + EXCLUDED.bytes, pending = EXCLUDED.pending`,
      [minute, r.stream, r.rows, r.bytes, r.pending],
    );
  }
  bg(
    `INSERT INTO rec_stats (minute, stream, rows, bytes, pending) VALUES ($1, '_process_rss', 0, $2, 0)
     ON CONFLICT (minute, stream) DO UPDATE SET bytes = EXCLUDED.bytes`,
    [minute, mem.rss],
  );
}

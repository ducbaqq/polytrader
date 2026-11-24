/**
 * Market data recorder entry point.
 *
 *   node dist/recorder/index.js
 *
 * Env: DATABASE_URL, DATABASE_CA_CERT (optional, for the catalog/stats tables),
 *      RECORDER_DATA_DIR (default ./data/recorder)
 */

import 'dotenv/config';
import { execSync } from 'child_process';
import * as fs from 'fs';
import { CONFIG } from './config';
import { migrate, closeDb } from './db';
import { closeAll } from './sink';
import { flushStats, logEvent } from './stats';
import { startBinance } from './binance';
import { startPolymarket } from './polymarket';
import { startRtds } from './rtds';

async function main(): Promise<void> {
  execSync('zstd --version'); // fail fast if the compressor is missing
  console.log(`[recorder] data dir: ${CONFIG.dataDir}`);
  await migrate();
  logEvent('start', 'recorder', `pid=${process.pid}`);

  const conns = [...startBinance(), startRtds(), await startPolymarket()];
  // Align stats flushes to wall-clock minutes so each rec_stats row covers one minute.
  setTimeout(() => {
    flushStats();
    setInterval(flushStats, CONFIG.statsIntervalMs);
  }, 60_000 - (Date.now() % 60_000));

  let stopping = false;

  // Disk guard: stop cleanly before the system disk fills. Exit code 75 tells systemd not to restart.
  const MIN_FREE = parseInt(process.env.RECORDER_MIN_FREE_GB || '3', 10) * 1e9;
  setInterval(() => {
    fs.mkdirSync(CONFIG.dataDir, { recursive: true });
    const st = fs.statfsSync(CONFIG.dataDir);
    const free = st.bavail * st.bsize;
    if (free < MIN_FREE) {
      logEvent('disk_full', 'recorder', `${(free / 1e9).toFixed(1)} GB free, stopping`);
      stop('disk_full', 75);
    } else if (free < MIN_FREE * 3) {
      logEvent('disk_low', 'recorder', `${(free / 1e9).toFixed(1)} GB free`);
    }
  }, 10 * 60_000);

  const stop = async (sig: string, code = 0) => {
    if (stopping) return;
    stopping = true;
    console.log(`[recorder] ${sig}: closing files`);
    conns.forEach((c) => c.stop());
    flushStats();
    logEvent('stop', 'recorder', sig);
    await closeAll();
    await new Promise((r) => setTimeout(r, 500)); // let the last DB writes go out
    await closeDb();
    process.exit(code);
  };
  process.on('SIGINT', () => stop('SIGINT'));
  process.on('SIGTERM', () => stop('SIGTERM'));
}

process.on('unhandledRejection', (e) => logEvent('unhandled_rejection', 'recorder', String(e).slice(0, 500)));

main().catch((e) => {
  console.error('[recorder] fatal:', e);
  process.exit(1);
});

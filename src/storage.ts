/**
 * JSON data storage module for persisting market snapshots.
 */

import * as fs from 'fs';
import * as path from 'path';
import * as zlib from 'zlib';
import { MarketSnapshot, Opportunity, StorageStats } from './types';

export interface DataStorageConfig {
  dataDir?: string;
  logsDir?: string;
  compress?: boolean;
}

export class DataStorage {
  private dataDir: string;
  private logsDir: string;
  private compress: boolean;
  private snapshotsDir: string;
  private opportunitiesDir: string;
  private errorsDir: string;

  constructor(config: DataStorageConfig = {}) {
    this.dataDir = config.dataDir || 'data';
    this.logsDir = config.logsDir || 'logs';
    this.compress = config.compress !== false;

    this.snapshotsDir = path.join(this.dataDir, 'snapshots');
    this.opportunitiesDir = path.join(this.dataDir, 'opportunities');
    this.errorsDir = path.join(this.logsDir, 'errors');

    // Create directories
    for (const dir of [
      this.snapshotsDir,
      this.opportunitiesDir,
      this.errorsDir,
    ]) {
      fs.mkdirSync(dir, { recursive: true });
    }

    console.log(`DataStorage initialized: data=${this.dataDir}, logs=${this.logsDir}`);
  }

  private getTimestampStr(timestamp?: Date): string {
    const ts = timestamp || new Date();
    const year = ts.getUTCFullYear();
    const month = String(ts.getUTCMonth() + 1).padStart(2, '0');
    const day = String(ts.getUTCDate()).padStart(2, '0');
    const hour = String(ts.getUTCHours()).padStart(2, '0');
    return `${year}${month}${day}_${hour}0000`;
  }

  private getSnapshotPath(timestamp?: Date): string {
    const tsStr = this.getTimestampStr(timestamp);
    const ext = this.compress ? '.json.gz' : '.json';
    return path.join(this.snapshotsDir, `snapshot_${tsStr}${ext}`);
  }

  private getOpportunitiesPath(timestamp?: Date): string {
    const tsStr = this.getTimestampStr(timestamp);
    const ext = this.compress ? '.json.gz' : '.json';
    return path.join(this.opportunitiesDir, `opportunities_${tsStr}${ext}`);
  }

  private writeJson(filePath: string, data: any): void {
    const jsonStr = JSON.stringify(data, null, 2);

    if (this.compress) {
      const compressed = zlib.gzipSync(jsonStr);
      fs.writeFileSync(filePath, compressed);
    } else {
      fs.writeFileSync(filePath, jsonStr);
    }
  }

  private readJson(filePath: string): any | null {
    try {
      if (!fs.existsSync(filePath)) {
        return null;
      }

      if (filePath.endsWith('.gz')) {
        const compressed = fs.readFileSync(filePath);
        const decompressed = zlib.gunzipSync(compressed);
        return JSON.parse(decompressed.toString());
      } else {
        const content = fs.readFileSync(filePath, 'utf-8');
        return JSON.parse(content);
      }
    } catch (error) {
      console.error(`Error reading ${filePath}:`, error);
      return null;
    }
  }

  /**
   * Convert snapshot to serializable format.
   */
  private snapshotToJson(snapshot: MarketSnapshot): any {
    return {
      timestamp: snapshot.timestamp.toISOString(),
      totalMarkets: snapshot.totalMarkets,
      totalVolume24h: snapshot.totalVolume24h,
      avgSpread: snapshot.avgSpread,
      markets: snapshot.markets.map((m) => ({
        marketId: m.marketId,
        conditionId: m.conditionId,
        question: m.question,
        endDate: m.endDate?.toISOString() || null,
        category: m.category,
        volume24h: m.volume24h,
        yesToken: m.yesToken,
        noToken: m.noToken,
        yesNoSum: m.yesNoSum,
        totalLiquidityAtBest: m.totalLiquidityAtBest,
        timeSinceLastTrade: m.timeSinceLastTrade,
        createdAt: m.createdAt?.toISOString() || null,
        lastUpdated: m.lastUpdated.toISOString(),
        totalActiveMakers: m.totalActiveMakers,
      })),
      opportunities: snapshot.opportunities.map((o) => ({
        ...o,
        timestamp: o.timestamp.toISOString(),
      })),
      volumeDistribution: snapshot.volumeDistribution,
      spreadDistribution: snapshot.spreadDistribution,
    };
  }

  /**
   * Save a market snapshot to disk.
   */
  saveSnapshot(snapshot: MarketSnapshot): string {
    const filePath = this.getSnapshotPath(snapshot.timestamp);

    // Check if file exists (same hour) - merge data
    const existing = this.readJson(filePath);

    let data: any;
    if (existing) {
      const existingMarkets: Map<string, any> = new Map();
      for (const m of existing.markets || []) {
        existingMarkets.set(m.marketId, m);
      }

      const snapshotJson = this.snapshotToJson(snapshot);
      for (const m of snapshotJson.markets) {
        existingMarkets.set(m.marketId, m);
      }

      data = {
        ...snapshotJson,
        markets: Array.from(existingMarkets.values()),
        updatesInHour: (existing.updatesInHour || 1) + 1,
      };
    } else {
      data = {
        ...this.snapshotToJson(snapshot),
        updatesInHour: 1,
      };
    }

    this.writeJson(filePath, data);
    console.log(`Saved snapshot to ${filePath}`);

    return filePath;
  }

  /**
   * Save opportunities to disk.
   */
  saveOpportunities(opportunities: Opportunity[], timestamp?: Date): string {
    const ts = timestamp || new Date();
    const filePath = this.getOpportunitiesPath(ts);

    // Append to existing file if same hour
    const existing = this.readJson(filePath);

    let allOps: any[];
    if (existing) {
      const existingOps = existing.opportunities || [];
      const existingKeys = new Set<string>();

      for (const op of existingOps) {
        const key = `${op.marketId}_${op.type}_${op.timestamp?.slice(0, 16)}`;
        existingKeys.add(key);
      }

      allOps = [...existingOps];
      for (const op of opportunities) {
        const key = `${op.marketId}_${op.type}_${op.timestamp.toISOString().slice(0, 16)}`;
        if (!existingKeys.has(key)) {
          allOps.push({
            ...op,
            timestamp: op.timestamp.toISOString(),
          });
        }
      }
    } else {
      allOps = opportunities.map((op) => ({
        ...op,
        timestamp: op.timestamp.toISOString(),
      }));
    }

    const data = {
      timestamp: ts.toISOString(),
      opportunities: allOps,
      totalCount: allOps.length,
    };

    this.writeJson(filePath, data);

    return filePath;
  }

  /**
   * Log an error to the error log file.
   */
  logError(error: Error, context: string = ''): void {
    const timestamp = new Date();
    const dateStr = timestamp.toISOString().slice(0, 10).replace(/-/g, '');
    const filePath = path.join(this.errorsDir, `errors_${dateStr}.json`);

    const existing = this.readJson(filePath) || { errors: [] };

    existing.errors.push({
      timestamp: timestamp.toISOString(),
      type: error.name,
      message: error.message,
      context,
    });

    // Don't compress error logs for easy reading
    fs.writeFileSync(filePath, JSON.stringify(existing, null, 2));
  }

  /**
   * Load a snapshot from disk.
   */
  loadSnapshot(timestamp: Date): any | null {
    const filePath = this.getSnapshotPath(timestamp);
    return this.readJson(filePath);
  }

  /**
   * Load all snapshots from the last N hours.
   */
  loadRecentSnapshots(hours: number = 24): any[] {
    const snapshots: any[] = [];
    const now = new Date();

    for (let h = 0; h < hours; h++) {
      const ts = new Date(now.getTime() - h * 60 * 60 * 1000);
      const snapshot = this.loadSnapshot(ts);
      if (snapshot) {
        snapshots.push(snapshot);
      }
    }

    return snapshots;
  }

  /**
   * Load all opportunities from the last N hours.
   */
  loadRecentOpportunities(hours: number = 24): any[] {
    const allOpportunities: any[] = [];
    const now = new Date();

    for (let h = 0; h < hours; h++) {
      const ts = new Date(now.getTime() - h * 60 * 60 * 1000);
      const filePath = this.getOpportunitiesPath(ts);
      const data = this.readJson(filePath);

      if (data) {
        allOpportunities.push(...(data.opportunities || []));
      }
    }

    return allOpportunities;
  }

  /**
   * Remove files older than specified days.
   */
  cleanupOldFiles(days: number = 30): void {
    const cutoff = new Date(Date.now() - days * 24 * 60 * 60 * 1000);
    let removedCount = 0;

    for (const directory of [this.snapshotsDir, this.opportunitiesDir, this.errorsDir]) {
      if (!fs.existsSync(directory)) continue;

      const files = fs.readdirSync(directory);
      for (const file of files) {
        try {
          // Extract date from filename like "snapshot_20240115_120000.json.gz"
          const match = file.match(/_(\d{8})_/);
          if (match) {
            const dateStr = match[1];
            const fileDate = new Date(
              parseInt(dateStr.slice(0, 4)),
              parseInt(dateStr.slice(4, 6)) - 1,
              parseInt(dateStr.slice(6, 8))
            );

            if (fileDate < cutoff) {
              fs.unlinkSync(path.join(directory, file));
              removedCount++;
            }
          }
        } catch {
          continue;
        }
      }
    }

    if (removedCount > 0) {
      console.log(`Cleaned up ${removedCount} old files`);
    }
  }

  /**
   * Get storage statistics.
   */
  getStats(): StorageStats {
    const getDirSize = (dir: string): number => {
      if (!fs.existsSync(dir)) return 0;
      let size = 0;
      const files = fs.readdirSync(dir);
      for (const file of files) {
        const filePath = path.join(dir, file);
        const stat = fs.statSync(filePath);
        if (stat.isFile()) {
          size += stat.size;
        }
      }
      return size;
    };

    const getFileCount = (dir: string): number => {
      if (!fs.existsSync(dir)) return 0;
      return fs.readdirSync(dir).filter((f) => fs.statSync(path.join(dir, f)).isFile())
        .length;
    };

    return {
      dataDir: this.dataDir,
      logsDir: this.logsDir,
      snapshotsCount: getFileCount(this.snapshotsDir),
      opportunitiesCount: getFileCount(this.opportunitiesDir),
      errorsCount: getFileCount(this.errorsDir),
      totalSizeMb: (getDirSize(this.dataDir) + getDirSize(this.logsDir)) / (1024 * 1024),
      compress: this.compress,
    };
  }
}

/**
 * Create a DataStorage using environment variables.
 */
export function createStorageFromEnv(): DataStorage {
  return new DataStorage({
    dataDir: process.env.DATA_DIR || 'data',
    logsDir: process.env.LOGS_DIR || 'logs',
    compress: (process.env.COMPRESS_DATA || 'true').toLowerCase() === 'true',
  });
}

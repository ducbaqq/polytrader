/**
 * Concurrent rate limiter for parallel API requests with controlled throughput
 *
 * Allows N concurrent requests while respecting overall rate limits.
 * Much more efficient than sequential processing for I/O bound tasks.
 */

import { sleep } from './rateLimiter.js';

export interface ConcurrentRateLimiterOptions {
  maxConcurrent: number;
  callsPerSecond: number;
}

export interface BatchProgress {
  completed: number;
  total: number;
  startTime: number;
  elapsedMs: number;
  ratePerSecond: number;
}

const DEFAULT_OPTIONS: ConcurrentRateLimiterOptions = {
  maxConcurrent: 10,
  callsPerSecond: 20,
};

export class ConcurrentRateLimiter {
  private maxConcurrent: number;
  private activeCount: number = 0;
  private queue: Array<{ resolve: () => void }> = [];

  constructor(options: Partial<ConcurrentRateLimiterOptions> = {}) {
    const opts = { ...DEFAULT_OPTIONS, ...options };
    this.maxConcurrent = opts.maxConcurrent;
  }

  /**
   * Execute a single task with rate limiting and concurrency control
   */
  private async acquire(): Promise<void> {
    if (this.activeCount < this.maxConcurrent) {
      this.activeCount++;
      return;
    }

    // Wait for a slot to be available
    return new Promise((resolve) => {
      this.queue.push({ resolve });
    });
  }

  private release(): void {
    this.activeCount--;
    const next = this.queue.shift();
    if (next) {
      this.activeCount++;
      next.resolve();
    }
  }

  /**
   * Execute all tasks with controlled concurrency and rate limiting
   *
   * @param tasks Array of async functions to execute
   * @param onProgress Optional callback for progress updates
   * @returns Array of results in the same order as input tasks
   */
  async executeAll<T>(
    tasks: Array<() => Promise<T>>,
    onProgress?: (progress: BatchProgress) => void
  ): Promise<T[]> {
    if (tasks.length === 0) {
      return [];
    }

    const results: T[] = new Array(tasks.length);
    let completed = 0;
    const startTime = Date.now();

    const wrappedTasks = tasks.map((task, index) => async () => {
      await this.acquire();

      try {
        results[index] = await task();
      } finally {
        this.release();
        completed++;

        if (onProgress) {
          const elapsedMs = Date.now() - startTime;
          onProgress({
            completed,
            total: tasks.length,
            startTime,
            elapsedMs,
            ratePerSecond: completed / (elapsedMs / 1000),
          });
        }
      }
    });

    const settled = await Promise.allSettled(wrappedTasks.map((t) => t()));

    const failureCount = settled.filter((r) => r.status === 'rejected').length;
    if (failureCount > 0) {
      console.warn(`${failureCount} tasks failed during batch execution`);
    }

    return results;
  }

  /**
   * Execute tasks in chunks with rate limiting between chunks
   * Better for very large datasets to prevent memory issues
   *
   * @param tasks Array of async functions to execute
   * @param chunkSize Number of tasks per chunk
   * @param onProgress Optional callback for progress updates
   * @returns Array of results in the same order as input tasks
   */
  async executeInChunks<T>(
    tasks: Array<() => Promise<T>>,
    chunkSize: number = 1000,
    onProgress?: (progress: BatchProgress) => void
  ): Promise<T[]> {
    if (tasks.length === 0) {
      return [];
    }

    const results: T[] = [];
    const startTime = Date.now();

    for (let i = 0; i < tasks.length; i += chunkSize) {
      const chunk = tasks.slice(i, i + chunkSize);
      const chunkOffset = i;

      const chunkResults = await this.executeAll(chunk, (progress) => {
        if (onProgress) {
          const overallCompleted = chunkOffset + progress.completed;
          const elapsedMs = Date.now() - startTime;
          onProgress({
            completed: overallCompleted,
            total: tasks.length,
            startTime,
            elapsedMs,
            ratePerSecond: overallCompleted / (elapsedMs / 1000),
          });
        }
      });

      results.push(...chunkResults);

      // Small delay between chunks to allow GC
      if (i + chunkSize < tasks.length) {
        await sleep(100);
      }
    }

    return results;
  }
}

export default ConcurrentRateLimiter;

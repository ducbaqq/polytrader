/**
 * Rate limiting utilities for API calls
 */

export class RateLimiter {
  private minInterval: number;
  private lastCallTime: number = 0;

  constructor(callsPerSecond: number = 5.0) {
    this.minInterval = 1000 / callsPerSecond;
  }

  async wait(): Promise<void> {
    const elapsed = Date.now() - this.lastCallTime;
    if (elapsed < this.minInterval) {
      await sleep(this.minInterval - elapsed);
    }
    this.lastCallTime = Date.now();
  }
}

export function sleep(ms: number): Promise<void> {
  return new Promise((resolve) => setTimeout(resolve, ms));
}

export async function withRetry<T>(
  fn: () => Promise<T>,
  maxRetries: number = 3,
  baseDelay: number = 1000
): Promise<T> {
  let lastError: Error | null = null;

  for (let attempt = 0; attempt <= maxRetries; attempt++) {
    try {
      return await fn();
    } catch (error: unknown) {
      lastError = error as Error;

      // Don't retry on 404 (no data)
      const axiosError = error as { response?: { status?: number } };
      if (axiosError?.response?.status === 404) {
        throw error;
      }

      if (attempt < maxRetries) {
        const delay = baseDelay * Math.pow(2, attempt);
        console.warn(
          `Request failed (attempt ${attempt + 1}/${maxRetries + 1}): ${lastError.message}. Retrying in ${delay}ms...`
        );
        await sleep(delay);
      }
    }
  }

  throw lastError;
}

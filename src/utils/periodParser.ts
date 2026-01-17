/**
 * Period parsing utilities for CLI arguments
 *
 * Period format:
 *   - Days: 1d, 5d, 14d
 *   - Months: 1m, 3m, 6m (approximated as 30 days each)
 */

export interface ParsedPeriod {
  days: number;
  original: string;
}

export function parsePeriod(value: string): ParsedPeriod {
  const match = value.match(/^(\d+)(d|m)$/i);
  if (!match) {
    throw new Error('Invalid period format. Use format like: 1d, 5d, 1m, 4m');
  }

  const num = parseInt(match[1], 10);
  const unit = match[2].toLowerCase();

  if (num <= 0) {
    throw new Error('Period value must be a positive integer');
  }

  const days = unit === 'd' ? num : num * 30;
  return { days, original: value.toLowerCase() };
}

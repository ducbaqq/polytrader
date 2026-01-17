/**
 * Configuration for the No-betting paper trading strategy.
 *
 * Hypothesis: Entertainment and Weather markets show historically high No win rates
 * due to retail bettors emotionally overbuying Yes on exciting/fearful outcomes.
 */

export interface StrategyConfig {
  // Capital management
  initialCapital: number;          // Starting balance ($)
  positionSize: number;            // Amount per trade ($)
  side: 'NO';                      // Always bet No

  // Entry conditions
  categories: string[];            // Target categories
  maxMarketAgeHours: number;       // Max age since creation
  minDurationDays: number;         // Min time until resolution
  maxDurationDays: number;         // Max time until resolution
  minNoPrice: number;              // Min No price (0-1)
  maxNoPrice: number;              // Max No price (0-1)
  minVolume: number;               // Min market volume ($)
  maxVolume: number;               // Max market volume ($)
  minEdge: number;                 // Min estimated edge (e.g., 0.05 = 5%)

  // Historical No win rates by category (for edge calculation)
  categoryWinRates: Record<string, number>;

  // Exit conditions
  holdToResolution: boolean;       // Default exit strategy
  takeProfitThreshold: number;     // Sell if No reaches this (e.g., 0.90)
  stopLossThreshold: number;       // Sell if No drops to this (e.g., 0.25)

  // Costs
  slippagePercent: number;         // Slippage assumption (0.005 = 0.5%)

  // Polling
  scanIntervalSeconds: number;     // How often to scan for new markets
  monitorIntervalSeconds: number;  // How often to check positions
}

/**
 * Default configuration based on the hypothesis.
 */
export const DEFAULT_STRATEGY_CONFIG: StrategyConfig = {
  // Capital
  initialCapital: 2500,
  positionSize: 50,
  side: 'NO',

  // Entry conditions
  categories: ['Entertainment', 'Weather'],
  maxMarketAgeHours: 48,
  minDurationDays: 1,
  maxDurationDays: 7,
  minNoPrice: 0.50,
  maxNoPrice: 0.75,
  minVolume: 200,
  maxVolume: 50000,
  minEdge: 0.05,  // 5% edge required

  // Historical win rates from alpha analysis
  categoryWinRates: {
    'Entertainment': 0.858,  // 85.8%
    'Weather': 0.831,        // 83.1%
  },

  // Exit conditions
  holdToResolution: true,
  takeProfitThreshold: 0.90,
  stopLossThreshold: 0.25,

  // Costs
  slippagePercent: 0.005,  // 0.5%

  // Polling
  scanIntervalSeconds: 60,    // Check for new markets every minute
  monitorIntervalSeconds: 30, // Monitor positions every 30 seconds
};

/**
 * Load configuration from environment variables or use defaults.
 */
export function loadConfig(): StrategyConfig {
  const config = { ...DEFAULT_STRATEGY_CONFIG };
  const env = process.env;

  // Map environment variables to config properties
  const floatMappings: [string, keyof StrategyConfig][] = [
    ['NO_TRADER_INITIAL_CAPITAL', 'initialCapital'],
    ['NO_TRADER_POSITION_SIZE', 'positionSize'],
    ['NO_TRADER_MIN_EDGE', 'minEdge'],
    ['NO_TRADER_TAKE_PROFIT', 'takeProfitThreshold'],
    ['NO_TRADER_STOP_LOSS', 'stopLossThreshold'],
  ];

  for (const [envKey, configKey] of floatMappings) {
    if (env[envKey]) {
      (config as any)[configKey] = parseFloat(env[envKey]!);
    }
  }

  if (env.NO_TRADER_SCAN_INTERVAL) {
    config.scanIntervalSeconds = parseInt(env.NO_TRADER_SCAN_INTERVAL);
  }

  return config;
}

/**
 * Calculate estimated edge for a market.
 * Edge = Historical No win rate - Current No price
 */
export function calculateEdge(
  category: string,
  noPrice: number,
  config: StrategyConfig
): number {
  const winRate = config.categoryWinRates[category];
  if (winRate === undefined) {
    return 0;  // Unknown category = no edge
  }
  return winRate - noPrice;
}

/**
 * Check if a market meets all entry conditions.
 */
export interface MarketEligibility {
  eligible: boolean;
  reason?: string;
  edge?: number;
}

export function checkMarketEligibility(
  category: string,
  noPrice: number,
  volume: number,
  createdAt: Date | null,
  endDate: Date | null,
  config: StrategyConfig
): MarketEligibility {
  // Check category
  if (!config.categories.includes(category)) {
    return { eligible: false, reason: `Category ${category} not in target list` };
  }

  // Check No price range
  if (noPrice < config.minNoPrice) {
    return { eligible: false, reason: `No price ${noPrice.toFixed(2)} below min ${config.minNoPrice}` };
  }
  if (noPrice > config.maxNoPrice) {
    return { eligible: false, reason: `No price ${noPrice.toFixed(2)} above max ${config.maxNoPrice}` };
  }

  // Check volume
  if (volume < config.minVolume) {
    return { eligible: false, reason: `Volume $${volume} below min $${config.minVolume}` };
  }
  if (volume > config.maxVolume) {
    return { eligible: false, reason: `Volume $${volume} above max $${config.maxVolume}` };
  }

  // Check market age
  if (createdAt) {
    const ageMs = Date.now() - createdAt.getTime();
    const ageHours = ageMs / (1000 * 60 * 60);
    if (ageHours > config.maxMarketAgeHours) {
      return { eligible: false, reason: `Market age ${ageHours.toFixed(1)}h exceeds max ${config.maxMarketAgeHours}h` };
    }
  }

  // Check time until resolution
  if (endDate) {
    const timeToEndMs = endDate.getTime() - Date.now();
    const daysToEnd = timeToEndMs / (1000 * 60 * 60 * 24);

    if (daysToEnd < config.minDurationDays) {
      return { eligible: false, reason: `Resolution in ${daysToEnd.toFixed(1)} days, below min ${config.minDurationDays}` };
    }
    if (daysToEnd > config.maxDurationDays) {
      return { eligible: false, reason: `Resolution in ${daysToEnd.toFixed(1)} days, above max ${config.maxDurationDays}` };
    }
  } else {
    return { eligible: false, reason: 'No end date specified' };
  }

  // Calculate edge
  const edge = calculateEdge(category, noPrice, config);
  if (edge < config.minEdge) {
    return {
      eligible: false,
      reason: `Edge ${(edge * 100).toFixed(1)}% below min ${(config.minEdge * 100).toFixed(1)}%`,
      edge,
    };
  }

  return { eligible: true, edge };
}

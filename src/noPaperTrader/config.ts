/**
 * Configuration for the No-betting paper trading strategy.
 *
 * Hypothesis: Entertainment and Weather markets show historically high No win rates
 * due to retail bettors emotionally overbuying Yes on exciting/fearful outcomes.
 */

import { StrategyId, StrategyDefinition, TokenSide } from './types';

// ============================================================================
// Strategy Registry - defines all available trading strategies
// ============================================================================

/**
 * Registry of all available trading strategies.
 * Each strategy has its own configuration for side, price range, and edge calculation.
 */
export const STRATEGY_REGISTRY: Record<StrategyId, StrategyDefinition> = {
  'yes-buyer': {
    id: 'yes-buyer',
    name: 'YES Buyer',
    description: 'Buys YES tokens when price is below expected win rate',
    side: 'YES',
    minPrice: 0.10,
    maxPrice: 0.60,
    minEdge: 0.02,
    categoryWinRates: {
      'Crypto': 0.70,        // Crypto price targets tend to be hit
      'Entertainment': 0.65, // Entertainment YES events often occur
      'Finance': 0.60,       // Finance YES predictions sometimes hit
      'Weather': 0.55,       // Weather YES events sometimes occur
      'Tech': 0.65,          // Tech announcements often happen
    },
  },
  'no-buyer': {
    id: 'no-buyer',
    name: 'NO Buyer',
    description: 'Buys NO tokens when YES is overpriced by retail bettors',
    side: 'NO',
    minPrice: 0.10,
    maxPrice: 0.60,
    minEdge: 0.02,
    categoryWinRates: {
      'Crypto': 0.30,        // Crypto NO - 30% (inverse of YES)
      'Entertainment': 1.00, // Entertainment NO win rate
      'Finance': 0.986,      // Finance NO win rate
      'Weather': 0.985,      // Weather NO win rate
      'Tech': 0.982,         // Tech NO win rate
    },
  },
};

/**
 * Get a strategy definition by ID.
 * @throws Error if strategy ID is not found
 */
export function getStrategy(id: string): StrategyDefinition {
  const strategy = STRATEGY_REGISTRY[id as StrategyId];
  if (!strategy) {
    throw new Error(`Unknown strategy: ${id}. Available: ${getAvailableStrategies().join(', ')}`);
  }
  return strategy;
}

export function getAvailableStrategies(): StrategyId[] {
  return Object.keys(STRATEGY_REGISTRY) as StrategyId[];
}

export function isValidStrategy(id: string): id is StrategyId {
  return id in STRATEGY_REGISTRY;
}

// ============================================================================
// Strategy Configuration
// ============================================================================

export interface StrategyConfig {
  // Capital management
  initialCapital: number;          // Starting balance ($)
  positionSize: number;            // Amount per trade ($)
  maxPositions: number;            // Max open positions at once

  // Entry conditions
  categories: string[];            // Target categories
  yesCategories: string[];         // Categories where we buy YES instead of NO
  minDurationDays: number;         // Min time until resolution
  maxDurationDays: number;         // Max time until resolution
  minPrice: number;                // Min entry price (0-1)
  maxPrice: number;                // Max entry price (0-1)
  minVolume: number;               // Min market volume ($)
  maxVolume: number;               // Max market volume ($)
  minEdge: number;                 // Min estimated edge (e.g., 0.05 = 5%)
  maxSpread: number;               // Max bid-ask spread (e.g., 0.10 = 10%)
  maxTimeBelowThreshold: number;   // Max % of market lifetime price was below maxPrice (0-1)

  // Historical win rates by category (for edge calculation)
  // For NO categories: this is NO win rate
  // For YES categories: this is YES win rate
  categoryWinRates: Record<string, number>;

  // Exit conditions
  holdToResolution: boolean;       // Default exit strategy
  takeProfitThreshold: number;     // Sell if price reaches this (e.g., 0.90)
  stopLossThreshold: number;       // Sell if price drops to this (e.g., 0.25)

  // Costs
  slippagePercent: number;         // Slippage assumption (0.005 = 0.5%)

  // Polling
  scanIntervalSeconds: number;     // How often to scan for new markets
  monitorIntervalSeconds: number;  // How often to check positions

  // Performance
  scanConcurrency: number;         // Number of markets to process in parallel
}

/**
 * Default configuration based on the hypothesis.
 *
 * Categories selected based on alpha analysis:
 * - Crypto: Buy YES (price prediction markets tend to resolve YES)
 * - Entertainment, Finance, Weather, Tech: Buy NO (high NO win rates)
 * - Excluded: Sports and Politics (lower win rates)
 *
 * Note: The Polymarket API doesn't provide category fields for open markets.
 * We use keyword-based category detection via detectCategoryFromQuestion().
 */
export const DEFAULT_STRATEGY_CONFIG: StrategyConfig = {
  // Capital
  initialCapital: 2500,
  positionSize: 50,
  maxPositions: 10,  // Limit to 10 open positions

  // Entry conditions - categories detected via keywords in question text
  categories: ['Crypto', 'Entertainment', 'Finance', 'Weather', 'Tech'],
  yesCategories: ['Crypto', 'Entertainment', 'Finance', 'Weather', 'Tech'],   // All categories buy YES
  minDurationDays: 1,
  maxDurationDays: 7,
  minPrice: 0,
  maxPrice: 0.60,              // Max 60¢ - looking for underpriced tokens
  minVolume: 1000,             // Min $1K volume
  maxVolume: Infinity,         // No max cap
  minEdge: 0.02,               // 2% minimum edge
  maxSpread: 0.10,             // Max 10% bid-ask spread
  maxTimeBelowThreshold: 0.75, // Skip if price was low >75% of lifetime

  // Historical win rates from alpha/observation
  // For NO categories: NO win rate
  // For YES categories (Crypto): YES win rate
  categoryWinRates: {
    'Crypto': 1.00,        // 100% YES win rate (price predictions tend to happen)
    'Entertainment': 1.00, // 100% NO win rate
    'Finance': 0.986,      // 98.6% NO win rate
    'Weather': 0.985,      // 98.5% NO win rate
    'Tech': 0.982,         // 98.2% NO win rate
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

  // Performance
  scanConcurrency: 10,        // Process 10 markets in parallel (conservative to avoid rate limits)
};

/**
 * Load configuration from environment variables or use defaults.
 */
export function loadConfig(): StrategyConfig {
  const config = { ...DEFAULT_STRATEGY_CONFIG };
  const env = process.env;

  const floatMappings: [string, keyof StrategyConfig][] = [
    ['NO_TRADER_INITIAL_CAPITAL', 'initialCapital'],
    ['NO_TRADER_POSITION_SIZE', 'positionSize'],
    ['NO_TRADER_MIN_EDGE', 'minEdge'],
    ['NO_TRADER_MAX_SPREAD', 'maxSpread'],
    ['NO_TRADER_TAKE_PROFIT', 'takeProfitThreshold'],
    ['NO_TRADER_STOP_LOSS', 'stopLossThreshold'],
    ['NO_TRADER_MAX_PRICE', 'maxPrice'],
    ['NO_TRADER_MIN_VOLUME', 'minVolume'],
    ['NO_TRADER_MAX_VOLUME', 'maxVolume'],
    ['NO_TRADER_MAX_TIME_BELOW_THRESHOLD', 'maxTimeBelowThreshold'],
  ];

  const intMappings: [string, keyof StrategyConfig][] = [
    ['NO_TRADER_SCAN_INTERVAL', 'scanIntervalSeconds'],
    ['NO_TRADER_SCAN_CONCURRENCY', 'scanConcurrency'],
    ['NO_TRADER_MAX_POSITIONS', 'maxPositions'],
  ];

  for (const [envKey, configKey] of floatMappings) {
    if (env[envKey]) {
      (config as any)[configKey] = parseFloat(env[envKey]!);
    }
  }

  for (const [envKey, configKey] of intMappings) {
    if (env[envKey]) {
      (config as any)[configKey] = parseInt(env[envKey]!);
    }
  }

  return config;
}

/**
 * Check if a category should buy YES instead of NO.
 */
export function isYesCategory(category: string, config: StrategyConfig): boolean {
  return config.yesCategories.includes(category);
}

/**
 * Calculate estimated edge for a market.
 * Edge = Historical win rate - Current price
 * Works for both YES and NO positions.
 */
export function calculateEdge(
  category: string,
  price: number,
  config: StrategyConfig
): number {
  const winRate = config.categoryWinRates[category];
  if (winRate === undefined) {
    return 0;  // Unknown category = no edge
  }
  return winRate - price;
}

/**
 * Check if a market meets all entry conditions.
 */
export interface MarketEligibility {
  eligible: boolean;
  reason?: string;
  edge?: number;
  tokenSide?: 'YES' | 'NO';
}

export function checkMarketEligibility(
  category: string,
  price: number,
  volume: number,
  endDate: Date | null,
  config: StrategyConfig
): MarketEligibility {
  // Check category
  if (!config.categories.includes(category)) {
    return { eligible: false, reason: `Category ${category} not in target list` };
  }

  // Determine which side we're buying
  const tokenSide = isYesCategory(category, config) ? 'YES' : 'NO';

  // Check price range
  if (price < config.minPrice) {
    return { eligible: false, reason: `${tokenSide} price ${price.toFixed(2)} below min ${config.minPrice}` };
  }
  if (price > config.maxPrice) {
    return { eligible: false, reason: `${tokenSide} price ${price.toFixed(2)} above max ${config.maxPrice}` };
  }

  // Check volume
  if (volume < config.minVolume) {
    return { eligible: false, reason: `Volume $${volume} below min $${config.minVolume}` };
  }
  if (volume > config.maxVolume) {
    return { eligible: false, reason: `Volume $${volume} above max $${config.maxVolume}` };
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
  const edge = calculateEdge(category, price, config);
  if (edge < config.minEdge) {
    return {
      eligible: false,
      reason: `Edge ${(edge * 100).toFixed(1)}% below min ${(config.minEdge * 100).toFixed(1)}%`,
      edge,
      tokenSide,
    };
  }

  return { eligible: true, edge, tokenSide };
}

/**
 * Keyword patterns for detecting category from market question.
 * Since Polymarket API doesn't provide categories for open markets,
 * we use keyword matching to categorize markets.
 */
const CATEGORY_KEYWORDS: Record<string, string[]> = {
  'Crypto': [
    'bitcoin', 'btc', 'ethereum', 'eth', 'crypto', 'solana', 'sol',
    'dogecoin', 'doge', 'xrp', 'ripple', 'cardano', 'ada', 'polkadot',
    'avalanche', 'polygon', 'matic', 'chainlink', 'link', 'uniswap',
    'binance', 'coinbase', 'kraken', 'defi', 'nft', 'blockchain',
    'altcoin', 'stablecoin', 'usdc', 'usdt', 'tether', 'microstrategy',
  ],
  'Weather': [
    'weather', 'temperature', 'hurricane', 'tornado', 'storm',
    'rain', 'snow', 'flood', 'drought', 'heatwave', 'cold wave',
    'climate', 'wildfire', 'el nino', 'la nina',
  ],
  'Entertainment': [
    'movie', 'film', 'oscar', 'grammy', 'emmy', 'golden globe',
    'box office', 'netflix', 'spotify', 'taylor swift', 'beyonce',
    'album', 'song', 'concert', 'tour', 'celebrity', 'kardashian',
    'super bowl halftime', 'streaming', 'disney', 'marvel', 'dc',
  ],
  'Finance': [
    'stock', 's&p', 'dow', 'nasdaq', 'fed', 'interest rate',
    'inflation', 'gdp', 'unemployment', 'recession', 'ipo',
    'earnings', 'revenue', 'market cap', 'treasury', 'bond',
    'forex', 'gold price', 'oil price', 'commodity',
  ],
  'Tech': [
    'apple', 'google', 'microsoft', 'meta', 'amazon', 'nvidia',
    'tesla', 'openai', 'chatgpt', 'ai ', 'artificial intelligence',
    'spacex', 'starship', 'rocket', 'launch', 'iphone', 'android',
    'tiktok', 'twitter', 'x.com', 'elon musk', 'zuckerberg',
  ],
};

/**
 * Detect category from market question using keyword matching.
 */
export function detectCategoryFromQuestion(question: string): string | null {
  const q = question.toLowerCase();
  for (const [category, keywords] of Object.entries(CATEGORY_KEYWORDS)) {
    if (keywords.some(kw => q.includes(kw))) return category;
  }
  return null;
}

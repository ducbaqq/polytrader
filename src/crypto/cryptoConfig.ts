/**
 * Configuration for Crypto Price Movement Reactive Trader
 */

import { CryptoTraderConfig, CryptoAsset } from './cryptoTypes';

// ============================================================================
// Default Configuration
// ============================================================================

export const DEFAULT_CONFIG: CryptoTraderConfig = {
  // Position sizing
  basePositionSize: 200,        // $200 per trade
  maxPositionSize: 500,         // $500 max per trade
  maxTotalExposure: 1500,       // $1500 max total exposure
  maxSimultaneousPositions: 3,  // Max 3 concurrent positions

  // Exit strategy
  maxHoldTimeSeconds: 120,      // 2 minutes max hold
  profitTargetPct: 0.15,        // +15% profit target
  stopLossPct: 0.05,            // -5% stop loss

  // Risk controls
  cooldownMinutes: 5,           // 5 min cooldown per market after trade
  dailyLossLimit: 100,          // $100 daily loss limit
  maxDailyTrades: 20,           // Max 20 trades per day

  // Mispricing thresholds
  minGapPercent: 0.20,          // 20% gap required to trade
  minVolume: 50000,             // $50K minimum volume
  minResolutionHours: 24,       // Min 24h until resolution

  // Market discovery
  discoveryIntervalMinutes: 5,  // Refresh markets every 5 min
  assets: ['BTC', 'ETH', 'SOL'] as CryptoAsset[],
};

// ============================================================================
// Binance WebSocket Configuration
// ============================================================================

export const BINANCE_CONFIG = {
  wsUrl: 'wss://stream.binance.com:9443/ws',

  // Ticker streams for each asset
  streams: {
    BTC: 'btcusdt@ticker',
    ETH: 'ethusdt@ticker',
    SOL: 'solusdt@ticker',
  } as Record<CryptoAsset, string>,

  // Reconnection settings
  reconnectDelayMs: 5000,
  maxReconnectAttempts: 10,
  heartbeatIntervalMs: 30000,

  // Significant move threshold (triggers immediate evaluation)
  significantMovePercent: 0.01,  // 1% move in 1 minute
};

// ============================================================================
// Market Discovery Configuration
// ============================================================================

export const DISCOVERY_CONFIG = {
  // Inclusion patterns - market question must contain asset name
  assetPatterns: {
    BTC: /\b(BTC|Bitcoin)\b/i,
    ETH: /\b(ETH|Ethereum|Ether)\b/i,
    SOL: /\b(SOL|Solana)\b/i,
  } as Record<CryptoAsset, RegExp>,

  // Threshold extraction patterns
  thresholdPatterns: [
    /\$(\d{1,3}(?:,\d{3})*(?:\.\d{2})?)/,  // $100,000 or $100,000.00
    /\$(\d+(?:\.\d+)?)\s*[kK]/,             // $100K or $100k
    /\$(\d+(?:\.\d+)?)\s*[mM]/,             // $1M or $1m (millions)
    /(\d{1,3}(?:,\d{3})+)/,                  // 100,000 (no $ sign)
  ],

  // Direction patterns
  directionPatterns: {
    ABOVE: /\b(above|over|exceed|reach|hit|break|surpass)\b/i,
    BELOW: /\b(below|under|fall|drop|dip)\b/i,
  },

  // Exclusion patterns - markets to skip
  exclusionPatterns: [
    /tweet/i,
    /hack/i,
    /\bsec\b/i,           // SEC (regulatory)
    /\belon\b/i,          // Elon Musk
    /\btrump\b/i,
    /\bmusk\b/i,
    /\bban\b/i,
    /\bregulat/i,
    /\bwhale\b/i,
    /\bpump\b/i,
    /\bdump\b/i,
    /\bsay\b/i,           // "Will X say..."
    /\bannounce/i,
    /\bconfirm/i,
    /\breport/i,
    /\bclaim/i,
    /\blaunch/i,
    /\blist/i,            // Exchange listings
    /\bapprove/i,         // ETF approvals etc
    /\betf\b/i,           // ETF related
    /\bhalving\b/i,       // Bitcoin halving
  ],

  // Whitelist patterns - high-confidence price threshold markets
  whitelistPatterns: [
    /will\s+(?:BTC|Bitcoin|ETH|Ethereum|SOL|Solana)\s+(?:be\s+)?(?:above|below|reach|hit)/i,
    /(?:BTC|Bitcoin|ETH|Ethereum|SOL|Solana)\s+(?:price\s+)?(?:above|below|over|under)\s+\$/i,
  ],
};

// ============================================================================
// Expected Price Calculation Parameters
// ============================================================================

export const PRICING_CONFIG = {
  // When price is above threshold (for ABOVE markets)
  aboveThreshold: {
    basePrice: 0.85,          // Start at 85% when just above
    maxPrice: 0.98,           // Cap at 98%
    distanceMultiplier: 2,    // How fast price increases with distance
    maxDistanceBonus: 0.13,   // Max bonus from distance
  },

  // When price is below threshold (for ABOVE markets)
  belowThreshold: {
    basePrice: 0.50,          // Start at 50% at threshold
    minPrice: 0.05,           // Floor at 5%
    distanceMultiplier: 4,    // How fast price decreases with distance
    maxDistancePenalty: 0.45, // Max penalty from distance
  },
};

// ============================================================================
// Position Sizing Parameters
// ============================================================================

export const SIZING_CONFIG = {
  // Gap-based confidence scaling
  gapTiers: [
    { minGap: 0.40, multiplier: 1.5 },   // 40%+ gap = max size
    { minGap: 0.30, multiplier: 1.25 },  // 30-40% gap
    { minGap: 0.20, multiplier: 1.0 },   // 20-30% gap = base size
  ],

  // Volume-based confidence scaling
  volumeTiers: [
    { minVolume: 200000, multiplier: 1.2 },  // $200K+ volume
    { minVolume: 100000, multiplier: 1.1 },  // $100K+ volume
    { minVolume: 50000, multiplier: 1.0 },   // $50K+ volume = base
  ],
};

// ============================================================================
// Dashboard Configuration
// ============================================================================

export const DASHBOARD_CONFIG = {
  refreshIntervalMs: 1000,    // Update every second
  maxRecentOpportunities: 10, // Show last 10 opportunities
  priceDecimalPlaces: 2,      // For crypto prices
  polyPriceDecimalPlaces: 4,  // For Polymarket prices
};

// ============================================================================
// Logging Configuration
// ============================================================================

export const LOGGING_CONFIG = {
  logSignificantMoves: true,
  logOpportunities: true,
  logTrades: true,
  logExits: true,
  logPriceUpdates: false,  // Too noisy
};

// ============================================================================
// Helper Functions
// ============================================================================

export function getConfig(): CryptoTraderConfig {
  // In future, could load from env or database
  return { ...DEFAULT_CONFIG };
}

export function getAssetSymbol(asset: CryptoAsset): string {
  const symbols: Record<CryptoAsset, string> = {
    BTC: 'BTCUSDT',
    ETH: 'ETHUSDT',
    SOL: 'SOLUSDT',
  };
  return symbols[asset];
}

export function formatPrice(price: number, asset: CryptoAsset): string {
  if (asset === 'BTC') {
    return `$${price.toLocaleString('en-US', { minimumFractionDigits: 2, maximumFractionDigits: 2 })}`;
  }
  return `$${price.toFixed(2)}`;
}

export function formatPolyPrice(price: number): string {
  return `$${price.toFixed(4)}`;
}

export function formatPercent(pct: number): string {
  const sign = pct >= 0 ? '+' : '';
  return `${sign}${(pct * 100).toFixed(2)}%`;
}

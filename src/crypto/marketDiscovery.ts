/**
 * Market Discovery for Crypto Price Threshold Markets
 *
 * Finds Polymarket markets that have crypto price thresholds like:
 * - "Will BTC be above $100,000 by January 2025?"
 * - "Will ETH reach $4,000?"
 * - "SOL below $200?"
 */

import { PolymarketClient, createClientFromEnv } from '../apiClient';
import { GammaMarket } from '../types';
import {
  CryptoAsset,
  CryptoMarket,
  ThresholdExtraction,
  ThresholdDirection,
  MarketDiscoveryResult,
} from './cryptoTypes';
import { DISCOVERY_CONFIG, DEFAULT_CONFIG } from './cryptoConfig';
import * as cryptoRepo from '../database/cryptoRepo';

// ============================================================================
// Threshold Extraction
// ============================================================================

/**
 * Extract crypto asset, threshold price, and direction from a market question.
 */
export function extractThreshold(question: string): ThresholdExtraction | null {
  // First, identify which asset this is about
  let asset: CryptoAsset | null = null;

  for (const [assetKey, pattern] of Object.entries(DISCOVERY_CONFIG.assetPatterns)) {
    if (pattern.test(question)) {
      asset = assetKey as CryptoAsset;
      break;
    }
  }

  if (!asset) return null;

  // Next, extract the threshold price
  let threshold: number | null = null;

  for (const pattern of DISCOVERY_CONFIG.thresholdPatterns) {
    const match = question.match(pattern);
    if (match) {
      let value = match[1];

      // Remove commas
      value = value.replace(/,/g, '');

      // Handle K/M multipliers
      const originalQuestion = question.toLowerCase();
      const matchIndex = match.index || 0;
      const afterMatch = originalQuestion.substring(matchIndex + match[0].length, matchIndex + match[0].length + 2);

      if (afterMatch.includes('k') || match[0].toLowerCase().includes('k')) {
        threshold = parseFloat(value) * 1000;
      } else if (afterMatch.includes('m') || match[0].toLowerCase().includes('m')) {
        threshold = parseFloat(value) * 1000000;
      } else {
        threshold = parseFloat(value);
      }

      break;
    }
  }

  if (!threshold || threshold <= 0 || !isFinite(threshold)) return null;

  // Sanity check thresholds based on asset
  if (!isThresholdReasonable(asset, threshold)) return null;

  // Determine direction (ABOVE or BELOW)
  let direction: ThresholdDirection = 'ABOVE'; // Default

  if (DISCOVERY_CONFIG.directionPatterns.BELOW.test(question)) {
    direction = 'BELOW';
  } else if (DISCOVERY_CONFIG.directionPatterns.ABOVE.test(question)) {
    direction = 'ABOVE';
  }

  return { asset, threshold, direction };
}

/**
 * Check if threshold is reasonable for the asset.
 */
function isThresholdReasonable(asset: CryptoAsset, threshold: number): boolean {
  const ranges: Record<CryptoAsset, { min: number; max: number }> = {
    BTC: { min: 10000, max: 1000000 },    // $10K - $1M
    ETH: { min: 500, max: 50000 },        // $500 - $50K
    SOL: { min: 10, max: 5000 },          // $10 - $5K
  };

  const range = ranges[asset];
  return threshold >= range.min && threshold <= range.max;
}

// ============================================================================
// Exclusion Filters
// ============================================================================

/**
 * Check if a market question should be excluded.
 */
export function shouldExclude(question: string): boolean {
  for (const pattern of DISCOVERY_CONFIG.exclusionPatterns) {
    if (pattern.test(question)) {
      return true;
    }
  }
  return false;
}

/**
 * Check if a market question matches whitelist patterns (high confidence).
 */
export function isWhitelisted(question: string): boolean {
  for (const pattern of DISCOVERY_CONFIG.whitelistPatterns) {
    if (pattern.test(question)) {
      return true;
    }
  }
  return false;
}

// ============================================================================
// Market Validation
// ============================================================================

/**
 * Validate a market meets minimum requirements.
 */
export function validateMarket(
  market: GammaMarket,
  extraction: ThresholdExtraction
): { valid: boolean; reason?: string } {
  // Check volume
  const volume = market.volume24hr || 0;
  if (volume < DEFAULT_CONFIG.minVolume) {
    return { valid: false, reason: `Volume too low: $${volume.toFixed(0)}` };
  }

  // Check resolution date (must be in the future)
  if (market.endDate) {
    const endDate = new Date(market.endDate);
    const now = new Date();
    const hoursUntilResolution = (endDate.getTime() - now.getTime()) / (1000 * 60 * 60);

    if (hoursUntilResolution < DEFAULT_CONFIG.minResolutionHours) {
      return {
        valid: false,
        reason: `Resolves too soon: ${hoursUntilResolution.toFixed(0)} hours`,
      };
    }
  }

  // Check market is active
  if (market.closed || !market.active) {
    return { valid: false, reason: 'Market is closed or inactive' };
  }

  return { valid: true };
}

// ============================================================================
// Market Discovery
// ============================================================================

/**
 * Discover crypto threshold markets from Polymarket.
 */
export async function discoverCryptoMarkets(
  client?: PolymarketClient
): Promise<MarketDiscoveryResult> {
  const apiClient = client || createClientFromEnv();

  const result: MarketDiscoveryResult = {
    discovered: 0,
    matched: 0,
    excluded: 0,
    errors: [],
  };

  try {
    // Fetch all active markets
    console.log('[CRYPTO-DISCOVERY] Fetching markets from Polymarket...');
    const markets = await apiClient.getAllMarkets(true, undefined, 0);
    result.discovered = markets.length;
    console.log(`[CRYPTO-DISCOVERY] Found ${markets.length} active markets`);

    let processed = 0;
    for (const market of markets) {
      const question = market.question || '';

      // Try to extract threshold
      const extraction = extractThreshold(question);
      if (!extraction) continue;

      processed++;

      // Check exclusion patterns
      if (shouldExclude(question)) {
        result.excluded++;
        continue;
      }

      // Validate market
      const validation = validateMarket(market, extraction);
      if (!validation.valid) {
        result.excluded++;
        continue;
      }

      // Create crypto market record
      const cryptoMarket: CryptoMarket = {
        marketId: market.id || '',
        question: question,
        asset: extraction.asset,
        threshold: extraction.threshold,
        direction: extraction.direction,
        resolutionDate: market.endDate ? new Date(market.endDate) : null,
        volume24h: market.volume24hr || 0,
        isWhitelisted: isWhitelisted(question),
        discoveredAt: new Date(),
        status: 'ACTIVE',
      };

      // Save to database
      try {
        await cryptoRepo.upsertCryptoMarket(cryptoMarket);
        result.matched++;

        console.log(
          `[CRYPTO-DISCOVERY] Matched: ${extraction.asset} ${extraction.direction} $${extraction.threshold.toLocaleString()} - "${question.substring(0, 60)}..."`
        );
      } catch (error: any) {
        result.errors.push(`Failed to save market ${market.id}: ${error.message}`);
      }
    }

    console.log(`[CRYPTO-DISCOVERY] Processed ${processed} crypto-related markets`);
    console.log(
      `[CRYPTO-DISCOVERY] Result: ${result.matched} matched, ${result.excluded} excluded`
    );
  } catch (error: any) {
    result.errors.push(`Discovery failed: ${error.message}`);
    console.error('[CRYPTO-DISCOVERY] Error:', error);
  }

  return result;
}

/**
 * Get current tracked markets from database, optionally filtered by asset.
 */
export async function getTrackedMarkets(asset?: CryptoAsset): Promise<CryptoMarket[]> {
  if (asset) {
    return cryptoRepo.getCryptoMarketsByAsset(asset);
  }
  return cryptoRepo.getActiveCryptoMarkets();
}

/**
 * Refresh market discovery (called periodically).
 */
export async function refreshMarketDiscovery(): Promise<MarketDiscoveryResult> {
  console.log('[CRYPTO-DISCOVERY] Starting market refresh...');

  // Mark existing markets as potentially stale
  // (We'll update them if they're still active)

  // Run discovery
  const result = await discoverCryptoMarkets();

  // TODO: Mark markets that weren't found as INACTIVE

  return result;
}

// ============================================================================
// Testing Utilities
// ============================================================================

/**
 * Test threshold extraction on sample questions.
 */
export function testThresholdExtraction(): void {
  const testCases = [
    'Will BTC be above $100,000 by January 2025?',
    'Will Bitcoin reach $100K?',
    'ETH above $4,000 by end of year?',
    'Will Ethereum hit $5K?',
    'SOL below $200 by March?',
    'Solana price over $300?',
    'Will BTC dip below $90,000?',
    'Bitcoin to $150K this year?',
    'Will ETH be under $3,000?',
    // Edge cases
    'BTC price 100000',
    'Will BTC tweet about $100K?', // Should be excluded
    'Elon Musk says BTC to $200K', // Should be excluded
  ];

  console.log('\n=== Threshold Extraction Tests ===\n');

  for (const question of testCases) {
    const extraction = extractThreshold(question);
    const excluded = shouldExclude(question);
    const whitelisted = isWhitelisted(question);

    console.log(`Q: "${question}"`);
    if (extraction) {
      console.log(
        `   → ${extraction.asset} ${extraction.direction} $${extraction.threshold.toLocaleString()}`
      );
      console.log(`   → Excluded: ${excluded}, Whitelisted: ${whitelisted}`);
    } else {
      console.log('   → No extraction (excluded or no match)');
    }
    console.log('');
  }
}

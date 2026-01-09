/**
 * Mispricing Detection for Crypto Price Threshold Markets
 *
 * Calculates expected Polymarket prices based on Binance prices,
 * compares to actual prices, and detects trading opportunities.
 */

import { v4 as uuidv4 } from 'uuid';
import {
  CryptoAsset,
  CryptoMarket,
  CryptoOpportunity,
  CryptoPrice,
  MispricingResult,
  ThresholdDirection,
} from './cryptoTypes';
import { PRICING_CONFIG, DEFAULT_CONFIG } from './cryptoConfig';
import * as cryptoRepo from '../database/cryptoRepo';

// ============================================================================
// Expected Price Calculation
// ============================================================================

/**
 * Calculate expected YES price based on current crypto price vs threshold.
 *
 * Logic:
 * - For "ABOVE $X" markets:
 *   - If crypto > threshold: YES should be high (0.85-0.98)
 *   - If crypto < threshold: YES should be low (0.05-0.50)
 *
 * - For "BELOW $X" markets:
 *   - If crypto < threshold: YES should be high
 *   - If crypto > threshold: YES should be low
 */
export function calculateExpectedYesPrice(
  cryptoPrice: number,
  threshold: number,
  direction: ThresholdDirection
): number {
  // Calculate distance as percentage of threshold
  const distance = (cryptoPrice - threshold) / threshold;

  if (direction === 'ABOVE') {
    if (cryptoPrice > threshold) {
      // Price is above threshold - YES is likely
      const { basePrice, maxPrice, distanceMultiplier, maxDistanceBonus } =
        PRICING_CONFIG.aboveThreshold;
      const bonus = Math.min(maxDistanceBonus, distance * distanceMultiplier);
      return Math.min(maxPrice, basePrice + bonus);
    } else {
      // Price is below threshold - YES is unlikely
      const { basePrice, minPrice, distanceMultiplier, maxDistancePenalty } =
        PRICING_CONFIG.belowThreshold;
      const penalty = Math.min(maxDistancePenalty, Math.abs(distance) * distanceMultiplier);
      return Math.max(minPrice, basePrice - penalty);
    }
  } else {
    // BELOW direction - inverse logic
    if (cryptoPrice < threshold) {
      // Price is below threshold - YES (for BELOW market) is likely
      const { basePrice, maxPrice, distanceMultiplier, maxDistanceBonus } =
        PRICING_CONFIG.aboveThreshold;
      const bonus = Math.min(maxDistanceBonus, Math.abs(distance) * distanceMultiplier);
      return Math.min(maxPrice, basePrice + bonus);
    } else {
      // Price is above threshold - YES (for BELOW market) is unlikely
      const { basePrice, minPrice, distanceMultiplier, maxDistancePenalty } =
        PRICING_CONFIG.belowThreshold;
      const penalty = Math.min(maxDistancePenalty, distance * distanceMultiplier);
      return Math.max(minPrice, basePrice - penalty);
    }
  }
}

/**
 * Calculate the expected NO price (complement of YES).
 */
export function calculateExpectedNoPrice(
  cryptoPrice: number,
  threshold: number,
  direction: ThresholdDirection
): number {
  return 1 - calculateExpectedYesPrice(cryptoPrice, threshold, direction);
}

// ============================================================================
// Threshold Crossing Detection
// ============================================================================

/**
 * Check if price has crossed a threshold.
 */
export function checkThresholdCrossing(
  previousPrice: number,
  currentPrice: number,
  threshold: number
): { crossed: boolean; direction: 'UP' | 'DOWN' | null } {
  if (previousPrice <= threshold && currentPrice > threshold) {
    return { crossed: true, direction: 'UP' };
  }
  if (previousPrice >= threshold && currentPrice < threshold) {
    return { crossed: true, direction: 'DOWN' };
  }
  return { crossed: false, direction: null };
}

/**
 * Calculate proximity to threshold (for prioritization).
 * Returns a value 0-1 where 1 = at threshold, 0 = far from threshold.
 */
export function calculateThresholdProximity(
  cryptoPrice: number,
  threshold: number
): number {
  const distance = Math.abs(cryptoPrice - threshold) / threshold;
  // Exponential decay - closer = higher value
  return Math.exp(-distance * 10);
}

// ============================================================================
// Mispricing Detection
// ============================================================================

/**
 * Detect mispricing opportunity for a single market.
 */
export function detectMispricing(
  market: CryptoMarket,
  cryptoPrice: CryptoPrice,
  actualYesPrice: number,
  actualNoPrice: number
): MispricingResult {
  // Calculate expected prices
  const expectedYes = calculateExpectedYesPrice(
    cryptoPrice.price,
    market.threshold,
    market.direction
  );
  const expectedNo = 1 - expectedYes;

  // Calculate gaps
  const yesGap = (expectedYes - actualYesPrice) / actualYesPrice;
  const noGap = (expectedNo - actualNoPrice) / actualNoPrice;

  // Find the larger opportunity
  const absYesGap = Math.abs(yesGap);
  const absNoGap = Math.abs(noGap);

  let side: 'YES' | 'NO';
  let gapPercent: number;
  let expectedPrice: number;
  let actualPrice: number;

  if (absYesGap >= absNoGap) {
    side = 'YES';
    gapPercent = yesGap;
    expectedPrice = expectedYes;
    actualPrice = actualYesPrice;
  } else {
    side = 'NO';
    gapPercent = noGap;
    expectedPrice = expectedNo;
    actualPrice = actualNoPrice;
  }

  // Check if gap is significant enough
  const absGap = Math.abs(gapPercent);
  if (absGap < DEFAULT_CONFIG.minGapPercent) {
    return {
      hasOpportunity: false,
      reason: `Gap too small: ${(absGap * 100).toFixed(1)}% < ${(DEFAULT_CONFIG.minGapPercent * 100).toFixed(0)}%`,
    };
  }

  // Determine trade direction
  // Positive gap = expected > actual = BUY (underpriced)
  // Negative gap = expected < actual = SELL (overpriced)
  // We want to BUY underpriced assets
  if (gapPercent < 0) {
    return {
      hasOpportunity: false,
      reason: `${side} is overpriced, not underpriced`,
    };
  }

  // Create opportunity
  const opportunity: CryptoOpportunity = {
    opportunityId: uuidv4(),
    marketId: market.marketId,
    detectedAt: new Date(),
    asset: cryptoPrice.asset,
    threshold: market.threshold,
    binancePrice: cryptoPrice.price,
    expectedPolyPrice: expectedPrice,
    actualPolyPrice: actualPrice,
    gapPercent: gapPercent,
    side: side,
    executed: false,
    status: 'DETECTED',
  };

  return {
    hasOpportunity: true,
    opportunity,
  };
}

/**
 * Scan all tracked markets for a given asset and detect opportunities.
 */
export async function scanForOpportunities(
  cryptoPrice: CryptoPrice,
  getMarketPrice: (marketId: string) => Promise<{ yesPrice: number; noPrice: number } | null>
): Promise<CryptoOpportunity[]> {
  const opportunities: CryptoOpportunity[] = [];

  // Get markets for this asset
  const markets = await cryptoRepo.getCryptoMarketsByAsset(cryptoPrice.asset);

  for (const market of markets) {
    // Get current Polymarket prices
    const prices = await getMarketPrice(market.marketId);
    if (!prices) continue;

    // Detect mispricing
    const result = detectMispricing(
      market,
      cryptoPrice,
      prices.yesPrice,
      prices.noPrice
    );

    if (result.hasOpportunity && result.opportunity) {
      opportunities.push(result.opportunity);

      // Log to database
      try {
        await cryptoRepo.insertCryptoOpportunity(result.opportunity);
      } catch (error) {
        console.error('[MISPRICING] Failed to log opportunity:', error);
      }

      console.log(
        `[MISPRICING] Opportunity: ${market.asset} ${market.direction} $${market.threshold.toLocaleString()} ` +
          `| ${result.opportunity.side} @ $${prices.yesPrice.toFixed(4)} → expected $${result.opportunity.expectedPolyPrice.toFixed(4)} ` +
          `| Gap: ${(result.opportunity.gapPercent * 100).toFixed(1)}%`
      );
    }
  }

  return opportunities;
}

/**
 * Quick check if a price update might create an opportunity.
 * Used for early filtering before full analysis.
 */
export function mightCreateOpportunity(
  market: CryptoMarket,
  previousPrice: number,
  currentPrice: number
): boolean {
  // Check threshold crossing
  const crossing = checkThresholdCrossing(previousPrice, currentPrice, market.threshold);
  if (crossing.crossed) return true;

  // Check proximity to threshold (within 5%)
  const proximity = Math.abs(currentPrice - market.threshold) / market.threshold;
  if (proximity < 0.05) return true;

  // Check significant price move (>1%)
  const priceChange = Math.abs(currentPrice - previousPrice) / previousPrice;
  if (priceChange > 0.01) return true;

  return false;
}

// ============================================================================
// Testing / Debug Utilities
// ============================================================================

/**
 * Test expected price calculation.
 */
export function testExpectedPrices(): void {
  console.log('\n=== Expected Price Tests ===\n');

  const testCases = [
    { asset: 'BTC', threshold: 100000, prices: [95000, 98000, 100000, 102000, 105000] },
    { asset: 'ETH', threshold: 4000, prices: [3500, 3800, 4000, 4200, 4500] },
    { asset: 'SOL', threshold: 200, prices: [180, 190, 200, 210, 220] },
  ];

  for (const tc of testCases) {
    console.log(`${tc.asset} ${tc.threshold.toLocaleString()} threshold (ABOVE):`);
    for (const price of tc.prices) {
      const expectedYes = calculateExpectedYesPrice(price, tc.threshold, 'ABOVE');
      const expectedNo = 1 - expectedYes;
      const status =
        price > tc.threshold
          ? '✓ ABOVE'
          : price < tc.threshold
          ? '✗ BELOW'
          : '= AT';
      console.log(
        `  $${price.toLocaleString().padStart(7)} ${status} → YES: $${expectedYes.toFixed(2)} | NO: $${expectedNo.toFixed(2)}`
      );
    }
    console.log('');
  }
}

/**
 * Simulate a mispricing scenario for testing.
 */
export function simulateMispricing(): void {
  console.log('\n=== Mispricing Simulation ===\n');

  const market: CryptoMarket = {
    marketId: 'test-btc-100k',
    question: 'Will BTC be above $100,000?',
    asset: 'BTC',
    threshold: 100000,
    direction: 'ABOVE',
    resolutionDate: new Date(Date.now() + 7 * 24 * 60 * 60 * 1000), // 7 days
    volume24h: 150000,
    isWhitelisted: true,
    discoveredAt: new Date(),
    status: 'ACTIVE',
  };

  const scenarios = [
    { btcPrice: 102000, polyYes: 0.72, polyNo: 0.28, desc: 'BTC above, Poly lagging' },
    { btcPrice: 98000, polyYes: 0.65, polyNo: 0.35, desc: 'BTC below, Poly still high' },
    { btcPrice: 100500, polyYes: 0.80, polyNo: 0.20, desc: 'BTC just above, Poly correct' },
    { btcPrice: 99500, polyYes: 0.48, polyNo: 0.52, desc: 'BTC just below, Poly correct' },
  ];

  for (const scenario of scenarios) {
    const cryptoPrice: CryptoPrice = {
      asset: 'BTC',
      price: scenario.btcPrice,
      timestamp: new Date(),
      change1m: 0.005,
      change5m: 0.01,
    };

    const result = detectMispricing(
      market,
      cryptoPrice,
      scenario.polyYes,
      scenario.polyNo
    );

    console.log(`Scenario: ${scenario.desc}`);
    console.log(`  BTC: $${scenario.btcPrice.toLocaleString()}`);
    console.log(`  Poly: YES $${scenario.polyYes} / NO $${scenario.polyNo}`);

    if (result.hasOpportunity && result.opportunity) {
      console.log(
        `  → OPPORTUNITY: ${result.opportunity.side} | Gap: ${(result.opportunity.gapPercent * 100).toFixed(1)}%`
      );
    } else {
      console.log(`  → No opportunity: ${result.reason}`);
    }
    console.log('');
  }
}

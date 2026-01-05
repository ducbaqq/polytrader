/**
 * Cost calculator for paper trading - simulates real trading costs.
 */

export interface TradeCosts {
  platformFee: number;
  gasCost: number;
  slippageCost: number;
  totalCost: number;
}

export interface CostConfig {
  platformFeePct: number;  // 2% default
  gasCostFixed: number;    // $0.10 per trade
  slippagePct: number;     // 0.5% default
}

const DEFAULT_COST_CONFIG: CostConfig = {
  platformFeePct: 0.02,
  gasCostFixed: 0.10,
  slippagePct: 0.005,
};

/**
 * Calculate trading costs for a given trade value.
 */
export function calculateTradeCosts(
  tradeValue: number,
  config: CostConfig = DEFAULT_COST_CONFIG
): TradeCosts {
  const platformFee = tradeValue * config.platformFeePct;
  const gasCost = config.gasCostFixed;
  const slippageCost = tradeValue * config.slippagePct;
  const totalCost = platformFee + gasCost + slippageCost;

  return {
    platformFee,
    gasCost,
    slippageCost,
    totalCost,
  };
}

/**
 * Calculate net value after costs.
 * For BUY: netValue = -(value + totalCost) (money out)
 * For SELL: netValue = value - totalCost (money in)
 */
export function calculateNetValue(
  tradeValue: number,
  side: 'BUY' | 'SELL',
  costs: TradeCosts
): number {
  if (side === 'BUY') {
    return -(tradeValue + costs.totalCost);
  } else {
    return tradeValue - costs.totalCost;
  }
}

/**
 * Check if a trade is profitable given costs.
 */
export function isTradeWorthwhile(
  entryPrice: number,
  exitPrice: number,
  size: number,
  config: CostConfig = DEFAULT_COST_CONFIG
): { profitable: boolean; netProfit: number; breakEvenPrice: number } {
  const entryValue = entryPrice * size;
  const exitValue = exitPrice * size;

  const entryCosts = calculateTradeCosts(entryValue, config);
  const exitCosts = calculateTradeCosts(exitValue, config);

  const totalCosts = entryCosts.totalCost + exitCosts.totalCost;
  const grossProfit = exitValue - entryValue;
  const netProfit = grossProfit - totalCosts;

  // Calculate break-even exit price
  const breakEvenPrice = (entryValue + entryCosts.totalCost + exitCosts.totalCost) / size;

  return {
    profitable: netProfit > 0,
    netProfit,
    breakEvenPrice,
  };
}

/**
 * Estimate slippage based on order size and available liquidity.
 */
export function estimateSlippage(
  orderSize: number,
  availableLiquidity: number,
  baseSlippage: number = 0.005
): number {
  if (availableLiquidity <= 0) return baseSlippage * 2;

  // Slippage increases as order size approaches available liquidity
  const sizeRatio = orderSize / availableLiquidity;

  if (sizeRatio < 0.1) return baseSlippage;
  if (sizeRatio < 0.25) return baseSlippage * 1.5;
  if (sizeRatio < 0.5) return baseSlippage * 2;
  return baseSlippage * 3;
}

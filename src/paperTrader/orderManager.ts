/**
 * Order manager for paper trading - handles order placement, fills, and cancellations.
 */

import { v4 as uuidv4 } from 'uuid';
import { PoolClient } from 'pg';
import { withTransaction, getPool } from '../database/index';
import {
  insertPaperOrder,
  getPendingOrders,
  fillOrder,
  cancelOrder,
  expireOldPendingOrders,
  insertPaperTrade,
  upsertPosition,
  getPositionByMarket,
  hasRecentSell,
  hasExistingPosition,
  DBPaperOrder,
} from '../database/paperTradingRepo';
import { getLatestOrderBook, getPriceChange, DBOrderBookSnapshot } from '../database/orderBookRepo';

import { calculateTradeCosts, calculateNetValue } from './costCalculator';
import { OrderSide, TokenSide } from '../types';

// ============ RISK MANAGEMENT CONSTANTS ============

// A: Position limit - max contracts per token to prevent concentration
const MAX_POSITION = 300;

// B: Stop loss - skip buying if unrealized P&L below this threshold
const STOP_LOSS_PCT = -0.05;

// C: Balanced trading - require sell in last N minutes (unless no position)
const BALANCED_TRADE_WINDOW_MINUTES = 10;

// E: Trend detection - skip buying if price dropped more than this in last 30 mins
const TREND_DROP_THRESHOLD = -0.05;
const TREND_LOOKBACK_MINUTES = 30;

export interface OrderRequest {
  marketId: string;
  side: OrderSide;
  tokenSide: TokenSide;
  price: number;
  size: number;
}

/**
 * Place a new paper order.
 */
export async function placeOrder(
  request: OrderRequest,
  bestBid: number | null,
  bestAsk: number | null,
  spread: number | null
): Promise<string> {
  const orderId = uuidv4();

  await withTransaction(async (client) => {
    await insertPaperOrder(client, {
      marketId: request.marketId,
      orderId,
      side: request.side,
      tokenSide: request.tokenSide,
      orderPrice: request.price,
      orderSize: request.size,
      bestBidAtOrder: bestBid,
      bestAskAtOrder: bestAsk,
      spreadAtOrder: spread,
    });
  });

  return orderId;
}

/**
 * Check pending orders for fills based on current market data.
 */
export async function checkFills(): Promise<number> {
  const pendingOrders = await getPendingOrders();
  let fillCount = 0;

  for (const order of pendingOrders) {
    // Get latest order book for this market
    const orderBook = await getLatestOrderBook(order.market_id, order.token_side as TokenSide);

    if (!orderBook) continue;

    const wouldFill = checkIfWouldFill(order, orderBook);

    if (wouldFill) {
      // For SELL orders, check if we have enough position to sell (no short selling allowed)
      if (order.side === 'SELL') {
        const position = await getPositionByMarket(order.market_id, order.token_side as TokenSide);
        const currentQty = position ? parseFloat(String(position.quantity)) : 0;
        const orderSize = parseFloat(String(order.order_size));

        if (currentQty < orderSize) {
          // Not enough position to sell - skip this order (it will expire)
          continue;
        }
      }

      await executeFill(order, orderBook);
      fillCount++;
    }
  }

  // Expire old pending orders (30 seconds instead of 5 minutes)
  await expireOldPendingOrders(0.5);

  return fillCount;
}

/**
 * Check if an order would have filled given current order book.
 */
function checkIfWouldFill(order: DBPaperOrder, orderBook: DBOrderBookSnapshot): boolean {
  // Parse as numbers (PostgreSQL returns numeric as strings)
  const orderPrice = parseFloat(String(order.order_price));
  const bestAsk = orderBook.best_ask_price !== null ? parseFloat(String(orderBook.best_ask_price)) : null;
  const bestBid = orderBook.best_bid_price !== null ? parseFloat(String(orderBook.best_bid_price)) : null;

  if (order.side === 'BUY') {
    // BUY order fills if market ask <= our bid price
    return bestAsk !== null && bestAsk <= orderPrice;
  } else {
    // SELL order fills if market bid >= our ask price
    return bestBid !== null && bestBid >= orderPrice;
  }
}

/**
 * Execute a fill for an order.
 */
async function executeFill(
  order: DBPaperOrder,
  orderBook: DBOrderBookSnapshot
): Promise<void> {
  await withTransaction(async (client) => {
    // Parse DB values as numbers (PostgreSQL returns numeric as strings)
    const orderPrice = parseFloat(String(order.order_price));
    const orderSize = parseFloat(String(order.order_size));
    const bestAsk = orderBook.best_ask_price !== null ? parseFloat(String(orderBook.best_ask_price)) : null;
    const bestBid = orderBook.best_bid_price !== null ? parseFloat(String(orderBook.best_bid_price)) : null;

    // Determine fill price (conservative estimate)
    let fillPrice: number;
    if (order.side === 'BUY') {
      // For buys, we pay the ask price (or our limit price, whichever is lower)
      fillPrice = Math.min(orderPrice, bestAsk ?? orderPrice);
    } else {
      // For sells, we get the bid price (or our limit price, whichever is higher)
      fillPrice = Math.max(orderPrice, bestBid ?? orderPrice);
    }

    const fillSize = orderSize;
    const tradeValue = fillPrice * fillSize;

    // Calculate costs
    const costs = calculateTradeCosts(tradeValue);
    const netValue = calculateNetValue(tradeValue, order.side as OrderSide, costs);

    // Update order status
    await fillOrder(client, order.order_id, fillPrice, fillSize);

    // Insert trade record
    const tradeId = uuidv4();
    await insertPaperTrade(client, {
      tradeId,
      marketId: order.market_id,
      orderId: order.order_id,
      side: order.side as OrderSide,
      tokenSide: order.token_side as TokenSide,
      price: fillPrice,
      size: fillSize,
      value: tradeValue,
      platformFee: costs.platformFee,
      gasCost: costs.gasCost,
      slippageCost: costs.slippageCost,
      totalCost: costs.totalCost,
      netValue,
    });

    // Update position
    const midPrice = orderBook.mid_price !== null ? parseFloat(String(orderBook.mid_price)) : null;
    await updatePositionAfterTrade(
      client,
      order.market_id,
      order.token_side as TokenSide,
      order.side as OrderSide,
      fillPrice,
      fillSize,
      midPrice
    );
  });
}

/**
 * Update position after a trade.
 */
async function updatePositionAfterTrade(
  client: PoolClient,
  marketId: string,
  tokenSide: TokenSide,
  side: OrderSide,
  price: number,
  size: number,
  currentMarketPrice: number | null
): Promise<void> {
  const existingPosition = await getPositionByMarket(marketId, tokenSide);

  let newQuantity: number;
  let newAverageCost: number;
  let newCostBasis: number;

  if (!existingPosition || existingPosition.quantity === 0) {
    // New position
    newQuantity = side === 'BUY' ? size : -size;
    newAverageCost = price;
    newCostBasis = price * size;
  } else {
    // Parse as numbers (PostgreSQL returns numeric as strings)
    const oldQty = parseFloat(String(existingPosition.quantity));
    const oldCost = parseFloat(String(existingPosition.cost_basis));

    if (side === 'BUY') {
      // Adding to position
      newQuantity = oldQty + size;
      if (newQuantity > 0) {
        newCostBasis = oldCost + (price * size);
        newAverageCost = newCostBasis / newQuantity;
      } else {
        // Position flipped
        newCostBasis = Math.abs(newQuantity) * price;
        newAverageCost = price;
      }
    } else {
      // Reducing/closing position
      newQuantity = oldQty - size;
      if (newQuantity > 0) {
        // Still long, reduce cost basis proportionally
        const reductionRatio = size / oldQty;
        newCostBasis = oldCost * (1 - reductionRatio);
        newAverageCost = parseFloat(String(existingPosition.average_cost));
      } else if (newQuantity < 0) {
        // Position flipped to short
        newCostBasis = Math.abs(newQuantity) * price;
        newAverageCost = price;
      } else {
        // Position closed
        newCostBasis = 0;
        newAverageCost = 0;
      }
    }
  }

  await upsertPosition(
    client,
    marketId,
    tokenSide,
    newQuantity,
    newAverageCost,
    newCostBasis,
    currentMarketPrice
  );
}

/**
 * Place market making orders for a given market.
 * Places both a buy order slightly above best bid and a sell order slightly below best ask.
 * Includes risk management checks to prevent accumulation in falling markets.
 */
export async function placeMarketMakingOrders(
  marketId: string,
  tokenSide: TokenSide,
  orderSize: number,
  tickImprovement: number = 0.01
): Promise<{ buyOrderId: string | null; sellOrderId: string | null }> {
  const orderBook = await getLatestOrderBook(marketId, tokenSide);

  if (!orderBook || !orderBook.best_bid_price || !orderBook.best_ask_price) {
    return { buyOrderId: null, sellOrderId: null };
  }

  // Parse as numbers (PostgreSQL returns numeric as strings)
  const bestBid = parseFloat(String(orderBook.best_bid_price));
  const bestAsk = parseFloat(String(orderBook.best_ask_price));

  if (isNaN(bestBid) || isNaN(bestAsk)) {
    return { buyOrderId: null, sellOrderId: null };
  }

  // Calculate spread and determine market-making approach
  const spread = bestAsk - bestBid;
  const minTick = 0.001;

  let buyPrice: number;
  let sellPrice: number;

  if (spread <= minTick) {
    // Minimum tick spread - can't improve, join the queue at best bid/ask
    buyPrice = bestBid;
    sellPrice = bestAsk;
  } else if (spread < 0.005) {
    // Tight spread (0.1-0.5 cents) - improve by minimum tick only
    buyPrice = Math.min(bestBid + minTick, 0.99);
    sellPrice = Math.max(bestAsk - minTick, 0.01);
  } else {
    // Wider spread - use percentage-based improvement (max 40% of spread)
    const maxTickFromSpread = spread * 0.4;
    const adjustedTick = Math.min(tickImprovement, Math.max(minTick, maxTickFromSpread));
    buyPrice = Math.min(bestBid + adjustedTick, 0.99);
    sellPrice = Math.max(bestAsk - adjustedTick, 0.01);
  }

  // Validate prices are sensible
  if (buyPrice <= 0 || buyPrice >= 1 || sellPrice <= 0 || sellPrice >= 1) {
    return { buyOrderId: null, sellOrderId: null };
  }

  // Safety check: ensure we haven't crossed prices (should never happen with above logic)
  if (sellPrice <= buyPrice) {
    console.log(`[MM] Price inversion for ${marketId}/${tokenSide}: buy=$${buyPrice.toFixed(4)} sell=$${sellPrice.toFixed(4)}`);
    return { buyOrderId: null, sellOrderId: null };
  }

  // Minimum trade value check - skip if trade value too small relative to gas costs
  // At $0.10 gas per trade, need at least $5 trade value for fees to be < 2%
  const tradeValue = orderSize * bestBid;
  if (tradeValue < 5.0) {
    return { buyOrderId: null, sellOrderId: null };
  }

  let buyOrderId: string | null = null;
  let sellOrderId: string | null = null;

  const spreadPercent = orderBook.spread_percent !== null ? parseFloat(String(orderBook.spread_percent)) : null;

  // ============ RISK MANAGEMENT CHECKS FOR BUY ORDERS ============
  let skipBuy = false;
  let skipReason = '';

  // Get current position for risk checks
  const position = await getPositionByMarket(marketId, tokenSide);
  const currentQty = position ? parseFloat(String(position.quantity)) : 0;
  const unrealizedPnlPct = position?.unrealized_pnl_pct
    ? parseFloat(String(position.unrealized_pnl_pct))
    : 0;

  // A: Position limit check
  if (currentQty >= MAX_POSITION) {
    skipBuy = true;
    skipReason = `Position limit reached (${currentQty}/${MAX_POSITION})`;
  }

  // B: Stop loss check
  if (!skipBuy && currentQty > 0 && unrealizedPnlPct < STOP_LOSS_PCT) {
    skipBuy = true;
    skipReason = `Stop loss triggered (${(unrealizedPnlPct * 100).toFixed(1)}% < ${(STOP_LOSS_PCT * 100).toFixed(1)}%)`;
  }

  // C: Balanced trading check (only if we have a position)
  if (!skipBuy && currentQty > 0) {
    const hasPosition = await hasExistingPosition(marketId, tokenSide);
    const recentlySold = await hasRecentSell(marketId, tokenSide, BALANCED_TRADE_WINDOW_MINUTES);

    if (hasPosition && !recentlySold) {
      skipBuy = true;
      skipReason = `Balanced trading: no sell in last ${BALANCED_TRADE_WINDOW_MINUTES} mins`;
    }
  }

  // E: Trend detection check
  if (!skipBuy) {
    const priceChange = await getPriceChange(marketId, tokenSide, TREND_LOOKBACK_MINUTES);
    if (priceChange !== null && priceChange < TREND_DROP_THRESHOLD) {
      skipBuy = true;
      skipReason = `Price dropping (${(priceChange * 100).toFixed(1)}% in last ${TREND_LOOKBACK_MINUTES} mins)`;
    }
  }

  if (skipBuy) {
    console.log(`[RISK] Skipping BUY for ${marketId}/${tokenSide}: ${skipReason}`);
  }

  // Place BUY order (if not skipped by risk management)
  if (!skipBuy) {
    try {
      buyOrderId = await placeOrder(
        {
          marketId,
          side: 'BUY',
          tokenSide,
          price: buyPrice,
          size: orderSize,
        },
        bestBid,
        bestAsk,
        spreadPercent
      );
    } catch (error) {
      console.error('Failed to place buy order:', error);
    }
  }

  // Place SELL order (always try, no risk management needed for sells)
  try {
    sellOrderId = await placeOrder(
      {
        marketId,
        side: 'SELL',
        tokenSide,
        price: sellPrice,
        size: orderSize,
      },
      bestBid,
      bestAsk,
      spreadPercent
    );
  } catch (error) {
    console.error('Failed to place sell order:', error);
  }

  return { buyOrderId, sellOrderId };
}

// ============ ARBITRAGE TRADING ============

export interface ArbitrageOrderResult {
  yesOrderId: string | null;
  noOrderId: string | null;
  hedged: boolean;
  fetchTimeMs: number;
  orderTimeMs: number;
  yesAsk: number | null;
  noAsk: number | null;
  sum: number | null;
  theoreticalProfit: number | null;
}

/**
 * Place arbitrage orders for a market - BUY both YES and NO tokens.
 * Only places orders if arbitrage opportunity still exists.
 */
export async function placeArbitrageOrders(
  marketId: string,
  orderSize: number
): Promise<ArbitrageOrderResult> {
  const startTime = Date.now();

  // Fetch order books for both sides
  const [yesBook, noBook] = await Promise.all([
    getLatestOrderBook(marketId, 'YES'),
    getLatestOrderBook(marketId, 'NO'),
  ]);
  const fetchTimeMs = Date.now() - startTime;

  // Check if we have valid order book data
  if (!yesBook?.best_ask_price || !noBook?.best_ask_price) {
    console.log(`[ARB] ${marketId}: No order book data (fetch: ${fetchTimeMs}ms)`);
    return {
      yesOrderId: null,
      noOrderId: null,
      hedged: false,
      fetchTimeMs,
      orderTimeMs: 0,
      yesAsk: null,
      noAsk: null,
      sum: null,
      theoreticalProfit: null,
    };
  }

  const yesAsk = parseFloat(String(yesBook.best_ask_price));
  const noAsk = parseFloat(String(noBook.best_ask_price));
  const sum = yesAsk + noAsk;

  // Verify arbitrage still exists (sum < $0.995)
  if (sum >= 0.995) {
    console.log(`[ARB] ${marketId}: Opportunity gone. YES=${yesAsk.toFixed(4)} NO=${noAsk.toFixed(4)} sum=${sum.toFixed(4)} (fetch: ${fetchTimeMs}ms)`);
    return {
      yesOrderId: null,
      noOrderId: null,
      hedged: false,
      fetchTimeMs,
      orderTimeMs: 0,
      yesAsk,
      noAsk,
      sum,
      theoreticalProfit: null,
    };
  }

  // Check available liquidity
  const yesAskSize = parseFloat(String(yesBook.best_ask_size)) || 0;
  const noAskSize = parseFloat(String(noBook.best_ask_size)) || 0;
  const availableLiquidity = Math.min(yesAskSize, noAskSize);

  // Adjust order size to available liquidity
  const actualOrderSize = Math.min(orderSize, availableLiquidity);
  if (actualOrderSize < 10) {
    console.log(`[ARB] ${marketId}: Insufficient liquidity. Available=${availableLiquidity.toFixed(0)} (fetch: ${fetchTimeMs}ms)`);
    return {
      yesOrderId: null,
      noOrderId: null,
      hedged: false,
      fetchTimeMs,
      orderTimeMs: 0,
      yesAsk,
      noAsk,
      sum,
      theoreticalProfit: null,
    };
  }

  const theoreticalProfit = (1 - sum) * actualOrderSize;
  console.log(`[ARB] ${marketId}: Placing orders. YES=${yesAsk.toFixed(4)} NO=${noAsk.toFixed(4)} sum=${sum.toFixed(4)} size=${actualOrderSize} profit=$${theoreticalProfit.toFixed(2)}`);

  // Place both orders as close together as possible
  const orderStartTime = Date.now();
  let yesOrderId: string | null = null;
  let noOrderId: string | null = null;

  try {
    // Place YES order at ask price (to get filled immediately)
    yesOrderId = await placeOrder(
      {
        marketId,
        side: 'BUY',
        tokenSide: 'YES',
        price: yesAsk,
        size: actualOrderSize,
      },
      yesBook.best_bid_price ? parseFloat(String(yesBook.best_bid_price)) : null,
      yesAsk,
      null
    );
  } catch (error) {
    console.error(`[ARB] ${marketId}: Failed to place YES order:`, error);
  }

  try {
    // Place NO order at ask price (to get filled immediately)
    noOrderId = await placeOrder(
      {
        marketId,
        side: 'BUY',
        tokenSide: 'NO',
        price: noAsk,
        size: actualOrderSize,
      },
      noBook.best_bid_price ? parseFloat(String(noBook.best_bid_price)) : null,
      noAsk,
      null
    );
  } catch (error) {
    console.error(`[ARB] ${marketId}: Failed to place NO order:`, error);
  }

  const orderTimeMs = Date.now() - orderStartTime;
  console.log(`[ARB] ${marketId}: Orders placed in ${orderTimeMs}ms. YES=${yesOrderId?.slice(0, 8) || 'FAILED'} NO=${noOrderId?.slice(0, 8) || 'FAILED'}`);

  return {
    yesOrderId,
    noOrderId,
    hedged: false,
    fetchTimeMs,
    orderTimeMs,
    yesAsk,
    noAsk,
    sum,
    theoreticalProfit,
  };
}

/**
 * Check arbitrage positions for partial fills and hedge if needed.
 * Called after checkFills() to handle one-sided fills.
 */
export async function handlePartialArbitrageFills(
  arbMarketIds: string[]
): Promise<{ marketId: string; action: string; hedgeOrderId: string | null }[]> {
  const results: { marketId: string; action: string; hedgeOrderId: string | null }[] = [];

  for (const marketId of arbMarketIds) {
    const yesPos = await getPositionByMarket(marketId, 'YES');
    const noPos = await getPositionByMarket(marketId, 'NO');

    const yesQty = yesPos ? parseFloat(String(yesPos.quantity)) : 0;
    const noQty = noPos ? parseFloat(String(noPos.quantity)) : 0;

    // If balanced, nothing to do
    if (Math.abs(yesQty - noQty) < 0.01) {
      continue;
    }

    // If imbalanced, we have a partial fill problem
    console.log(`[ARB-HEDGE] ${marketId}: Imbalanced! YES=${yesQty.toFixed(0)} NO=${noQty.toFixed(0)}`);

    let hedgeOrderId: string | null = null;
    let action: string;

    if (yesQty > noQty) {
      // Sell excess YES tokens at best bid
      const excess = yesQty - noQty;
      const yesBook = await getLatestOrderBook(marketId, 'YES');
      if (yesBook?.best_bid_price) {
        const bestBid = parseFloat(String(yesBook.best_bid_price));
        try {
          hedgeOrderId = await placeOrder(
            {
              marketId,
              side: 'SELL',
              tokenSide: 'YES',
              price: bestBid,
              size: excess,
            },
            bestBid,
            yesBook.best_ask_price ? parseFloat(String(yesBook.best_ask_price)) : null,
            null
          );
          action = `Selling ${excess.toFixed(0)} excess YES at ${bestBid.toFixed(4)}`;
          console.log(`[ARB-HEDGE] ${marketId}: ${action}`);
        } catch (error) {
          action = `Failed to sell excess YES: ${error}`;
          console.error(`[ARB-HEDGE] ${marketId}: ${action}`);
        }
      } else {
        action = 'No YES bid to hedge against';
      }
    } else {
      // Sell excess NO tokens at best bid
      const excess = noQty - yesQty;
      const noBook = await getLatestOrderBook(marketId, 'NO');
      if (noBook?.best_bid_price) {
        const bestBid = parseFloat(String(noBook.best_bid_price));
        try {
          hedgeOrderId = await placeOrder(
            {
              marketId,
              side: 'SELL',
              tokenSide: 'NO',
              price: bestBid,
              size: excess,
            },
            bestBid,
            noBook.best_ask_price ? parseFloat(String(noBook.best_ask_price)) : null,
            null
          );
          action = `Selling ${excess.toFixed(0)} excess NO at ${bestBid.toFixed(4)}`;
          console.log(`[ARB-HEDGE] ${marketId}: ${action}`);
        } catch (error) {
          action = `Failed to sell excess NO: ${error}`;
          console.error(`[ARB-HEDGE] ${marketId}: ${action}`);
        }
      } else {
        action = 'No NO bid to hedge against';
      }
    }

    results.push({ marketId, action, hedgeOrderId });
  }

  return results;
}

// ============ LOGGING FOR AUTOPSY ============

/**
 * Log why an order expired (didn't fill).
 */
export function logOrderExpiredReason(order: DBPaperOrder): string {
  const orderPrice = parseFloat(String(order.order_price));
  const bestBid = order.best_bid_at_order !== null ? parseFloat(String(order.best_bid_at_order)) : null;
  const bestAsk = order.best_ask_at_order !== null ? parseFloat(String(order.best_ask_at_order)) : null;
  const spread = (bestAsk !== null && bestBid !== null) ? bestAsk - bestBid : null;
  const orderAge = Date.now() - new Date(order.placed_at).getTime();

  let reason = 'Unknown';

  if (order.side === 'BUY') {
    if (bestAsk !== null && orderPrice < bestAsk) {
      reason = `BUY price ${orderPrice.toFixed(4)} below ask ${bestAsk.toFixed(4)} (gap: ${(bestAsk - orderPrice).toFixed(4)})`;
    } else if (bestAsk === null) {
      reason = 'No ask available in order book';
    }
  } else {
    if (bestBid !== null && orderPrice > bestBid) {
      reason = `SELL price ${orderPrice.toFixed(4)} above bid ${bestBid.toFixed(4)} (gap: ${(orderPrice - bestBid).toFixed(4)})`;
    } else if (bestBid === null) {
      reason = 'No bid available in order book';
    }
  }

  // Additional context
  if (spread !== null && spread > 0.10) {
    reason += ` | Wide spread (${(spread * 100).toFixed(1)}%)`;
  }

  console.log(`[ORDER-EXPIRED] ${order.market_id}/${order.token_side} ${order.side}: ${reason} (age: ${orderAge}ms)`);

  return reason;
}

/**
 * Log order fill details.
 */
export function logOrderFilled(
  order: DBPaperOrder,
  fillPrice: number,
  fillSize: number
): void {
  const orderPrice = parseFloat(String(order.order_price));
  const slippage = fillPrice - orderPrice;
  const orderAge = Date.now() - new Date(order.placed_at).getTime();
  const value = fillPrice * fillSize;

  console.log(
    `[ORDER-FILLED] ${order.market_id}/${order.token_side} ${order.side}: ` +
    `order=${orderPrice.toFixed(4)} fill=${fillPrice.toFixed(4)} ` +
    `slippage=${slippage >= 0 ? '+' : ''}${slippage.toFixed(4)} ` +
    `size=${fillSize} value=$${value.toFixed(2)} (age: ${orderAge}ms)`
  );
}

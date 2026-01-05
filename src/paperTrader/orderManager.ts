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
  DBPaperOrder,
} from '../database/paperTradingRepo';
import { getLatestOrderBook, DBOrderBookSnapshot } from '../database/orderBookRepo';
import { calculateTradeCosts, calculateNetValue } from './costCalculator';
import { OrderSide, TokenSide } from '../types';

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
      await executeFill(order, orderBook);
      fillCount++;
    }
  }

  // Expire old pending orders
  await expireOldPendingOrders(5);

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

  // Use percentage-based tick improvement for low-priced tokens
  // Minimum tick is 0.001 (0.1 cent), or 1% of price for very low prices
  const minTick = 0.001;
  const adjustedTick = Math.max(minTick, Math.min(tickImprovement, orderBook.best_bid_price * 0.1));

  const buyPrice = Math.min(orderBook.best_bid_price + adjustedTick, 0.99);  // Cap at 99 cents
  const sellPrice = Math.max(orderBook.best_ask_price - adjustedTick, 0.01); // Floor at 1 cent

  // Validate prices are sensible
  if (buyPrice <= 0 || buyPrice >= 1 || sellPrice <= 0 || sellPrice >= 1) {
    return { buyOrderId: null, sellOrderId: null };
  }

  // Only place if there's still a positive spread after our improvement
  if (sellPrice <= buyPrice) {
    return { buyOrderId: null, sellOrderId: null };
  }

  let buyOrderId: string | null = null;
  let sellOrderId: string | null = null;

  try {
    buyOrderId = await placeOrder(
      {
        marketId,
        side: 'BUY',
        tokenSide,
        price: buyPrice,
        size: orderSize,
      },
      orderBook.best_bid_price,
      orderBook.best_ask_price,
      orderBook.spread_percent
    );
  } catch (error) {
    console.error('Failed to place buy order:', error);
  }

  try {
    sellOrderId = await placeOrder(
      {
        marketId,
        side: 'SELL',
        tokenSide,
        price: sellPrice,
        size: orderSize,
      },
      orderBook.best_bid_price,
      orderBook.best_ask_price,
      orderBook.spread_percent
    );
  } catch (error) {
    console.error('Failed to place sell order:', error);
  }

  return { buyOrderId, sellOrderId };
}

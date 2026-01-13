/**
 * Price-triggered sell worker for Polymarket.
 * Monitors a specific market via WebSocket and executes a market sell
 * when the best bid reaches the target price.
 *
 * Usage:
 *   npm run sell-trigger                    # Live mode
 *   npm run sell-trigger -- --dry-run       # Dry run (no execution)
 */

import WebSocket from 'ws';
import dotenv from 'dotenv';
import {
  createClobClient,
  findMarketByQuestion,
  getPositionSize,
  marketSell,
  getOrderBook,
} from './clobClient';
import { ClobClient } from '@polymarket/clob-client';

dotenv.config();

// Configuration
const CONFIG = {
  // Market to monitor
  MARKET_SEARCH: 'Khamenei out as Supreme Leader of Iran by January 31',

  // Trigger price (best bid must be >= this to sell)
  TRIGGER_PRICE: 0.24,

  // WebSocket endpoint
  WS_URL: 'wss://ws-subscriptions-clob.polymarket.com/ws/market',

  // Heartbeat interval (ms)
  HEARTBEAT_MS: 30000,

  // Delay before execution to allow abort (ms)
  EXECUTION_DELAY_MS: 2000,

  // Reconnect delay (ms)
  RECONNECT_DELAY_MS: 5000,
};

// Parse command line arguments
const args = process.argv.slice(2);
const DRY_RUN = args.includes('--dry-run');

interface WSBookMessage {
  event_type: 'book';
  asset_id: string;
  market: string;
  timestamp: string;
  bids: Array<{ price: string; size: string }>;
  asks: Array<{ price: string; size: string }>;
}

interface WSPriceChangeMessage {
  event_type: 'price_change';
  asset_id: string;
  price_changes?: Array<{
    asset_id: string;
    best_bid: string;
    best_ask: string;
  }>;
}

type WSMessage = WSBookMessage | WSPriceChangeMessage | { event_type: string };

class PriceTriggerWorker {
  private client: ClobClient;
  private ws: WebSocket | null = null;
  private yesTokenId: string = '';
  private marketQuestion: string = '';
  private positionSize: number = 0;
  private triggered: boolean = false;
  private shouldReconnect: boolean = true;
  private heartbeatTimer: NodeJS.Timeout | null = null;
  private lastPongTime: number = 0;
  private currentBestBid: number | null = null;

  constructor() {
    this.client = createClobClient();
  }

  /**
   * Start the worker.
   */
  async start(): Promise<void> {
    console.log('═══════════════════════════════════════════════════════════');
    console.log('       POLYMARKET PRICE-TRIGGERED SELL WORKER');
    console.log('═══════════════════════════════════════════════════════════');
    console.log(`Mode: ${DRY_RUN ? '🧪 DRY RUN (no execution)' : '🔴 LIVE MODE'}`);
    console.log(`Trigger price: $${CONFIG.TRIGGER_PRICE}`);
    console.log('');

    // Step 1: Find the market
    console.log('[1/4] Searching for market...');
    const market = await findMarketByQuestion(CONFIG.MARKET_SEARCH);
    if (!market) {
      throw new Error(`Market not found: ${CONFIG.MARKET_SEARCH}`);
    }
    if (!market.yesTokenId) {
      throw new Error('YES token ID not found for market');
    }

    this.yesTokenId = market.yesTokenId;
    this.marketQuestion = market.question;
    console.log(`      ✓ Found: "${market.question}"`);
    console.log(`      ✓ YES Token: ${this.yesTokenId}`);
    console.log('');

    // Step 2: Query position size
    console.log('[2/4] Querying position size...');
    this.positionSize = await getPositionSize(this.client, this.yesTokenId);
    if (this.positionSize <= 0) {
      throw new Error(`No position found for token ${this.yesTokenId}`);
    }
    console.log(`      ✓ Position: ${this.positionSize} YES shares`);
    console.log('');

    // Step 3: Get current price
    console.log('[3/4] Fetching current orderbook...');
    const book = await getOrderBook(this.client, this.yesTokenId);
    console.log(`      ✓ Best Bid: $${book.bestBid?.toFixed(4) || 'N/A'} (${book.bidSize} shares)`);
    console.log(`      ✓ Best Ask: $${book.bestAsk?.toFixed(4) || 'N/A'} (${book.askSize} shares)`);
    this.currentBestBid = book.bestBid;
    console.log('');

    // Step 4: Connect WebSocket
    console.log('[4/4] Connecting to WebSocket...');
    await this.connect();

    console.log('');
    console.log('═══════════════════════════════════════════════════════════');
    console.log(`  Monitoring for best bid >= $${CONFIG.TRIGGER_PRICE}`);
    console.log(`  Will sell ALL ${this.positionSize} shares when triggered`);
    console.log('  Press Ctrl+C to stop');
    console.log('═══════════════════════════════════════════════════════════');
    console.log('');

    // Handle graceful shutdown
    process.on('SIGINT', () => this.shutdown('SIGINT'));
    process.on('SIGTERM', () => this.shutdown('SIGTERM'));
  }

  /**
   * Connect to WebSocket.
   */
  private async connect(): Promise<void> {
    return new Promise((resolve, reject) => {
      try {
        this.ws = new WebSocket(CONFIG.WS_URL);

        this.ws.on('open', () => {
          console.log('      ✓ WebSocket connected');

          // Subscribe to the YES token
          const subscribeMsg = {
            type: 'subscribe',
            assets_ids: [this.yesTokenId],
          };
          this.ws!.send(JSON.stringify(subscribeMsg));
          console.log('      ✓ Subscribed to YES token');

          this.lastPongTime = Date.now();
          this.startHeartbeat();

          resolve();
        });

        this.ws.on('message', (data: Buffer) => {
          this.handleMessage(data);
        });

        this.ws.on('pong', () => {
          this.lastPongTime = Date.now();
        });

        this.ws.on('close', (code, reason) => {
          console.log(`[WS] Connection closed: ${code} - ${reason.toString()}`);
          this.handleDisconnect();
        });

        this.ws.on('error', (error) => {
          console.error('[WS] Error:', error.message);
          reject(error);
        });

      } catch (error) {
        reject(error);
      }
    });
  }

  /**
   * Start heartbeat mechanism.
   */
  private startHeartbeat(): void {
    this.stopHeartbeat();

    this.heartbeatTimer = setInterval(() => {
      if (this.ws && this.ws.readyState === WebSocket.OPEN) {
        const timeSinceLastPong = Date.now() - this.lastPongTime;
        if (timeSinceLastPong > CONFIG.HEARTBEAT_MS * 2) {
          console.warn('[WS] No pong received, reconnecting...');
          this.ws.terminate();
          return;
        }
        this.ws.ping();
      }
    }, CONFIG.HEARTBEAT_MS);
  }

  /**
   * Stop heartbeat.
   */
  private stopHeartbeat(): void {
    if (this.heartbeatTimer) {
      clearInterval(this.heartbeatTimer);
      this.heartbeatTimer = null;
    }
  }

  /**
   * Handle WebSocket disconnection.
   */
  private handleDisconnect(): void {
    this.stopHeartbeat();

    if (this.shouldReconnect && !this.triggered) {
      console.log(`[WS] Reconnecting in ${CONFIG.RECONNECT_DELAY_MS}ms...`);
      setTimeout(async () => {
        try {
          await this.connect();
        } catch (error) {
          console.error('[WS] Reconnect failed:', error);
          this.handleDisconnect();
        }
      }, CONFIG.RECONNECT_DELAY_MS);
    }
  }

  /**
   * Handle incoming WebSocket message.
   */
  private handleMessage(data: Buffer): void {
    try {
      const message = JSON.parse(data.toString()) as WSMessage | WSMessage[];
      const messages = Array.isArray(message) ? message : [message];

      for (const msg of messages) {
        if (msg.event_type === 'book') {
          this.processBookMessage(msg as WSBookMessage);
        } else if (msg.event_type === 'price_change') {
          this.processPriceChangeMessage(msg as WSPriceChangeMessage);
        }
      }
    } catch (error) {
      // Ignore parse errors
    }
  }

  /**
   * Process orderbook snapshot.
   */
  private processBookMessage(msg: WSBookMessage): void {
    if (msg.asset_id !== this.yesTokenId) return;

    let bestBid: number | null = null;

    if (msg.bids && msg.bids.length > 0) {
      // Find highest bid
      let maxBidPrice = -1;
      for (const bid of msg.bids) {
        const price = parseFloat(bid.price);
        if (price > maxBidPrice) {
          maxBidPrice = price;
        }
      }
      if (maxBidPrice > 0) {
        bestBid = maxBidPrice;
      }
    }

    this.updateBestBid(bestBid);
  }

  /**
   * Process price change message.
   */
  private processPriceChangeMessage(msg: WSPriceChangeMessage): void {
    if (msg.price_changes) {
      for (const change of msg.price_changes) {
        if (change.asset_id === this.yesTokenId && change.best_bid) {
          this.updateBestBid(parseFloat(change.best_bid));
        }
      }
    }
  }

  /**
   * Update best bid and check trigger.
   */
  private updateBestBid(bestBid: number | null): void {
    if (bestBid === null) return;

    const previousBid = this.currentBestBid;
    this.currentBestBid = bestBid;

    // Log significant changes
    if (previousBid === null || Math.abs(bestBid - previousBid) >= 0.001) {
      const timestamp = new Date().toISOString().substring(11, 19);
      const status = bestBid >= CONFIG.TRIGGER_PRICE ? '⚡ TRIGGER' : '';
      console.log(`[${timestamp}] Best bid: $${bestBid.toFixed(4)} ${status}`);
    }

    // Check trigger
    if (!this.triggered && bestBid >= CONFIG.TRIGGER_PRICE) {
      this.triggerSell(bestBid);
    }
  }

  /**
   * Trigger the sell execution.
   */
  private async triggerSell(triggerPrice: number): Promise<void> {
    if (this.triggered) return;
    this.triggered = true;

    console.log('');
    console.log('═══════════════════════════════════════════════════════════');
    console.log('  🚨 TRIGGER ACTIVATED 🚨');
    console.log('═══════════════════════════════════════════════════════════');
    console.log(`  Best bid: $${triggerPrice.toFixed(4)} >= $${CONFIG.TRIGGER_PRICE}`);
    console.log(`  Selling: ${this.positionSize} shares`);
    console.log('');

    if (DRY_RUN) {
      console.log('  🧪 DRY RUN - Order NOT executed');
      console.log('═══════════════════════════════════════════════════════════');
      this.shutdown('DRY_RUN_COMPLETE');
      return;
    }

    // Re-query position to get current size
    console.log('  Confirming position size...');
    const currentPosition = await getPositionSize(this.client, this.yesTokenId);
    if (currentPosition <= 0) {
      console.log('  ❌ No position found - aborting');
      this.shutdown('NO_POSITION');
      return;
    }

    console.log(`  Position confirmed: ${currentPosition} shares`);
    console.log('');
    console.log(`  ⏳ Executing in ${CONFIG.EXECUTION_DELAY_MS / 1000}s... (Ctrl+C to abort)`);

    // Delay before execution
    await this.sleep(CONFIG.EXECUTION_DELAY_MS);

    if (!this.shouldReconnect) {
      console.log('  ❌ Aborted by user');
      return;
    }

    console.log('');
    console.log('  📤 Executing market sell order...');

    try {
      const result = await marketSell(this.client, this.yesTokenId, currentPosition);

      console.log('');
      console.log('  ✅ ORDER EXECUTED SUCCESSFULLY');
      console.log('═══════════════════════════════════════════════════════════');
      console.log(`  Order ID: ${result?.orderID || 'N/A'}`);
      console.log(`  Status: ${result?.status || 'N/A'}`);
      console.log(`  Transaction: ${result?.transactionsHashes?.[0] || 'N/A'}`);
      console.log('═══════════════════════════════════════════════════════════');

    } catch (error: any) {
      console.error('');
      console.error('  ❌ ORDER FAILED');
      console.error('═══════════════════════════════════════════════════════════');
      console.error(`  Error: ${error.message || error}`);
      console.error('═══════════════════════════════════════════════════════════');
    }

    this.shutdown('EXECUTION_COMPLETE');
  }

  /**
   * Shutdown the worker.
   */
  private shutdown(reason: string): void {
    console.log('');
    console.log(`[Worker] Shutting down... (${reason})`);

    this.shouldReconnect = false;
    this.stopHeartbeat();

    if (this.ws) {
      this.ws.removeAllListeners();
      if (this.ws.readyState === WebSocket.OPEN) {
        this.ws.close();
      }
      this.ws = null;
    }

    // Exit after a brief delay to allow logs to flush
    setTimeout(() => {
      process.exit(0);
    }, 500);
  }

  /**
   * Sleep helper.
   */
  private sleep(ms: number): Promise<void> {
    return new Promise(resolve => setTimeout(resolve, ms));
  }
}

// Main entry point
async function main(): Promise<void> {
  try {
    const worker = new PriceTriggerWorker();
    await worker.start();
  } catch (error: any) {
    console.error('');
    console.error('❌ Fatal error:', error.message || error);
    console.error('');
    process.exit(1);
  }
}

main();

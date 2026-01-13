/**
 * Authenticated CLOB client wrapper for Polymarket order execution.
 * Uses L2 API keys for authentication.
 */

import { ClobClient, Chain, Side, OrderType, AssetType } from '@polymarket/clob-client';
import { Wallet } from '@ethersproject/wallet';
import dotenv from 'dotenv';

dotenv.config();

const CLOB_HOST = 'https://clob.polymarket.com';
const GAMMA_API_HOST = 'https://gamma-api.polymarket.com';

export interface ClobConfig {
  privateKey: string;
  apiKey: string;
  apiSecret: string;
  apiPassphrase: string;
}

/**
 * Get CLOB config from environment variables.
 */
export function getClobConfigFromEnv(): ClobConfig {
  const privateKey = process.env.POLYMARKET_PRIVATE_KEY;
  const apiKey = process.env.POLYMARKET_API_KEY;
  const apiSecret = process.env.POLYMARKET_API_SECRET;
  const apiPassphrase = process.env.POLYMARKET_API_PASSPHRASE;

  if (!privateKey) throw new Error('POLYMARKET_PRIVATE_KEY not set in environment');
  if (!apiKey) throw new Error('POLYMARKET_API_KEY not set in environment');
  if (!apiSecret) throw new Error('POLYMARKET_API_SECRET not set in environment');
  if (!apiPassphrase) throw new Error('POLYMARKET_API_PASSPHRASE not set in environment');

  return { privateKey, apiKey, apiSecret, apiPassphrase };
}

/**
 * Create an authenticated CLOB client.
 */
export function createClobClient(config?: ClobConfig): ClobClient {
  const cfg = config || getClobConfigFromEnv();

  // Create wallet from private key
  const wallet = new Wallet(cfg.privateKey);

  // Create credentials object
  const creds = {
    key: cfg.apiKey,
    secret: cfg.apiSecret,
    passphrase: cfg.apiPassphrase,
  };

  console.log(`[CLOB] Initializing client for address: ${wallet.address}`);

  // Create ClobClient with L2 auth (Polygon mainnet = 137)
  const client = new ClobClient(
    CLOB_HOST,
    Chain.POLYGON,
    wallet,
    creds
  );

  return client;
}

/**
 * Get position size for a specific token.
 * First tries getBalanceAllowance, then falls back to calculating from trade history.
 */
export async function getPositionSize(client: ClobClient, tokenId: string): Promise<number> {
  // Method 1: Try direct balance query
  try {
    const balance = await client.getBalanceAllowance({
      asset_type: AssetType.CONDITIONAL,
      token_id: tokenId,
    });

    const shares = parseFloat(balance.balance);
    if (shares > 0) {
      console.log(`[CLOB] Position for token ${tokenId}: ${shares} shares (from allowance)`);
      return shares;
    }
    console.log(`[CLOB] Balance allowance returned 0, checking trade history...`);
  } catch (error: any) {
    console.log(`[CLOB] Balance query failed: ${error.message}, checking trade history...`);
  }

  // Method 2: Fall back to calculating from trade history
  try {
    const trades = await client.getTrades();
    let netPosition = 0;

    for (const trade of trades) {
      if (trade.asset_id === tokenId) {
        const size = parseFloat(trade.size);
        const side = trade.side?.toUpperCase();
        if (side === 'BUY') {
          netPosition += size;
        } else if (side === 'SELL') {
          netPosition -= size;
        }
      }
    }

    console.log(`[CLOB] Position for token ${tokenId}: ${netPosition} shares (from trade history)`);
    return netPosition;
  } catch (error) {
    console.error('[CLOB] Error fetching trade history:', error);
    throw error;
  }
}

/**
 * Execute a market sell order (FOK - Fill or Kill).
 */
export async function marketSell(
  client: ClobClient,
  tokenId: string,
  shares: number
): Promise<any> {
  console.log(`[CLOB] Executing market sell: ${shares} shares of token ${tokenId}`);

  const order = {
    tokenID: tokenId,
    amount: shares,
    side: Side.SELL,
  };

  try {
    const result = await client.createAndPostMarketOrder(order, {}, OrderType.FOK);
    console.log('[CLOB] Market sell result:', JSON.stringify(result, null, 2));
    return result;
  } catch (error) {
    console.error('[CLOB] Market sell failed:', error);
    throw error;
  }
}

/**
 * Find market by searching question text using Gamma API.
 * First tries the events endpoint (more reliable), then falls back to markets.
 */
export async function findMarketByQuestion(searchText: string): Promise<{
  conditionId: string;
  question: string;
  yesTokenId: string;
  noTokenId: string;
} | null> {
  const axios = (await import('axios')).default;

  try {
    console.log(`[CLOB] Searching for market: "${searchText}"`);

    // Convert search text to potential slug format
    const slugSearch = searchText.toLowerCase().replace(/[^a-z0-9]+/g, '-').replace(/^-|-$/g, '');

    // Try events endpoint first (more reliable for finding specific markets)
    try {
      const eventResponse = await axios.get(`${GAMMA_API_HOST}/events`, {
        params: { slug: slugSearch },
      });

      if (eventResponse.data && eventResponse.data.length > 0) {
        const event = eventResponse.data[0];
        if (event.markets && event.markets.length > 0) {
          const market = event.markets[0];
          return parseMarketData(market);
        }
      }
    } catch (e) {
      // Event search failed, continue to markets search
    }

    // Fallback: Search all markets
    const response = await axios.get(`${GAMMA_API_HOST}/markets`, {
      params: {
        closed: false,
        limit: 1000,
      },
    });

    const markets = response.data;

    // Search for matching question
    const searchLower = searchText.toLowerCase();
    const match = markets.find((m: any) =>
      m.question && m.question.toLowerCase().includes(searchLower)
    );

    if (!match) {
      console.log('[CLOB] No matching market found');
      return null;
    }

    return parseMarketData(match);
  } catch (error) {
    console.error('[CLOB] Error searching for market:', error);
    throw error;
  }
}

/**
 * Parse market data from Gamma API response.
 */
function parseMarketData(match: any): {
  conditionId: string;
  question: string;
  yesTokenId: string;
  noTokenId: string;
} | null {
  // Parse token IDs from clobTokenIds JSON string
  let tokenIds: string[] = [];
  if (match.clobTokenIds) {
    try {
      tokenIds = JSON.parse(match.clobTokenIds);
    } catch {
      tokenIds = [];
    }
  }

  // Parse outcomes to map token IDs to YES/NO
  let outcomes: string[] = [];
  if (match.outcomes) {
    try {
      outcomes = JSON.parse(match.outcomes);
    } catch {
      outcomes = ['Yes', 'No'];
    }
  }

  let yesTokenId = '';
  let noTokenId = '';

  for (let i = 0; i < outcomes.length; i++) {
    const outcome = outcomes[i].toLowerCase();
    if (outcome === 'yes' && tokenIds[i]) {
      yesTokenId = tokenIds[i];
    } else if (outcome === 'no' && tokenIds[i]) {
      noTokenId = tokenIds[i];
    }
  }

  console.log(`[CLOB] Found market: "${match.question}"`);
  console.log(`[CLOB] Condition ID: ${match.conditionId}`);
  console.log(`[CLOB] YES token: ${yesTokenId}`);
  console.log(`[CLOB] NO token: ${noTokenId}`);

  return {
    conditionId: match.conditionId,
    question: match.question,
    yesTokenId,
    noTokenId,
  };
}

/**
 * Get orderbook for a token.
 */
export async function getOrderBook(client: ClobClient, tokenId: string): Promise<{
  bestBid: number | null;
  bestAsk: number | null;
  bidSize: number;
  askSize: number;
}> {
  try {
    const book = await client.getOrderBook(tokenId);

    let bestBid: number | null = null;
    let bestAsk: number | null = null;
    let bidSize = 0;
    let askSize = 0;

    if (book.bids && book.bids.length > 0) {
      // Bids are sorted highest first
      bestBid = parseFloat(book.bids[0].price);
      bidSize = parseFloat(book.bids[0].size);
    }

    if (book.asks && book.asks.length > 0) {
      // Asks are sorted lowest first
      bestAsk = parseFloat(book.asks[0].price);
      askSize = parseFloat(book.asks[0].size);
    }

    return { bestBid, bestAsk, bidSize, askSize };
  } catch (error) {
    console.error('[CLOB] Error fetching orderbook:', error);
    throw error;
  }
}

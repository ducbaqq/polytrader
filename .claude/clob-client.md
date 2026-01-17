# CLOB Client

## Purpose

Authenticated wrapper for Polymarket's Central Limit Order Book (CLOB) API. Used for real order execution, position queries, and trade history.

---

## Entry Point

`src/clobClient.ts`

---

## Key Files

| File | Purpose |
|------|---------|
| `src/clobClient.ts` | CLOB client wrapper functions |
| `src/deriveApiKey.ts` | Utility to derive API keys from private key |

---

## How to Use

```typescript
import { createClobClient, getPositionSize, marketSell } from './clobClient';

// Create authenticated client
const client = createClobClient();

// Get position for a token
const size = await getPositionSize(client, tokenId);

// Execute market sell
await marketSell(client, tokenId, size);
```

---

## Key Functions

### createClobClient(config?)

Creates authenticated CLOB client using L2 API keys.

```typescript
const client = createClobClient();
// Uses env vars: POLYMARKET_PRIVATE_KEY, POLYMARKET_API_KEY,
// POLYMARKET_API_SECRET, POLYMARKET_API_PASSPHRASE
```

### getPositionSize(client, tokenId)

Gets current position for a token:
1. Tries `getBalanceAllowance` (direct balance query)
2. Falls back to calculating from trade history

### findMarketByQuestion(searchText)

Searches Polymarket for a market by question text.

### marketSell(client, tokenId, size)

Executes a market sell order.

### getOrderBook(client, tokenId)

Fetches current order book for a token.

---

## Authentication

Uses L2 API keys derived from private key:

```bash
# Derive keys from private key
npm run derive-key
```

This creates/retrieves API credentials and outputs:
```
POLYMARKET_API_KEY=...
POLYMARKET_API_SECRET=...
POLYMARKET_API_PASSPHRASE=...
```

---

## API Endpoints

| Endpoint | Purpose |
|----------|---------|
| `https://clob.polymarket.com` | CLOB API (orders, trades) |
| `https://gamma-api.polymarket.com` | Gamma API (market discovery) |

---

## Environment Variables

```bash
# Required for authenticated operations
POLYMARKET_PRIVATE_KEY=your_private_key
POLYMARKET_API_KEY=your_api_key
POLYMARKET_API_SECRET=your_api_secret
POLYMARKET_API_PASSPHRASE=your_passphrase
```

---

## Dependencies

- `@polymarket/clob-client` - Official Polymarket SDK
- `@ethersproject/wallet` - Wallet signing
- `ethers` - Ethereum utilities

---

## Order Types

From `@polymarket/clob-client`:
- `Side.BUY` / `Side.SELL`
- `OrderType.GTC` (Good Till Cancelled)
- `OrderType.FOK` (Fill Or Kill)
- `OrderType.GTD` (Good Till Date)

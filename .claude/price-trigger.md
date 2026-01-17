# Price Trigger Worker

## Purpose

Monitors a specific Polymarket market via WebSocket and automatically executes a market sell when the best bid reaches a target price. Useful for setting price alerts with automatic execution.

---

## Entry Point

`src/priceTriggerWorker.ts`

---

## How to Run

```bash
# Live mode
npm run sell-trigger

# Dry run (no execution)
npm run sell-trigger -- --dry-run
```

---

## Key Files

| File | Purpose |
|------|---------|
| `src/priceTriggerWorker.ts` | Main worker class `PriceTriggerWorker` |
| `src/clobClient.ts` | CLOB client for order execution |

---

## Configuration

Hardcoded in `CONFIG` object at top of file:

```typescript
const CONFIG = {
  // Market to monitor (search by question)
  MARKET_SEARCH: 'Khamenei out as Supreme Leader of Iran by January 31',

  // Trigger price (best bid must be >= this)
  TRIGGER_PRICE: 0.24,

  // WebSocket endpoint
  WS_URL: 'wss://ws-subscriptions-clob.polymarket.com/ws/market',

  // Timing
  HEARTBEAT_MS: 30000,
  EXECUTION_DELAY_MS: 2000,  // Delay before sell (abort window)
  RECONNECT_DELAY_MS: 5000,
};
```

---

## Flow

```
1. Search for market by question
2. Find YES token ID
3. Get current position size
4. Connect to WebSocket
5. Subscribe to market book updates
6. On each price_change event:
   - Check if best_bid >= TRIGGER_PRICE
   - If yes, wait EXECUTION_DELAY_MS (abort window)
   - Execute market sell
   - Disconnect and exit
```

---

## WebSocket Messages

Subscribes to both `book` and `price_change` events:

```json
{
  "event_type": "price_change",
  "asset_id": "...",
  "price_changes": [
    { "asset_id": "...", "best_bid": "0.24", "best_ask": "0.26" }
  ]
}
```

---

## Dependencies

- **External**: Polymarket WebSocket, Polymarket CLOB API
- **Internal**: `clobClient.ts`
- **Environment**: Requires full API credentials for order execution

---

## Safety Features

- Dry run mode (`--dry-run`) for testing
- 2-second delay before execution to allow abort
- Logs every price update and decision
- Only triggers once, then exits

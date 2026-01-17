# WebSocket Market Validator

## Purpose

Real-time market scanner and paper trading engine. Connects to Polymarket WebSocket for instant price updates, detects arbitrage opportunities, and runs simulated trades.

---

## Entry Point

`src/index.ts` → `src/wsValidator.ts`

---

## How to Run

```bash
# Main validator (runs 24/7)
npm run validate

# Generate report from existing data
npm run report

# Check database status
npm run db-status

# Reset database
npm run reset
```

---

## Key Files

| File | Purpose |
|------|---------|
| `src/index.ts` | CLI entry point, parses commands |
| `src/wsValidator.ts` | Main orchestrator class `WSMarketValidator` |
| `src/wsScanner.ts` | WebSocket connection manager `WSMarketScanner` |
| `src/apiClient.ts` | REST API client for market discovery |

---

## Architecture

```
Polymarket WebSocket
        │
        ▼
┌─────────────────┐
│ WSMarketScanner │ ──► Emits 'price' events
└─────────────────┘
        │
        ▼
┌─────────────────┐
│ WSMarketValidator│
│ - Buffer updates │
│ - Detect arb     │
│ - Run paper trade│
└─────────────────┘
        │
        ▼
    PostgreSQL
```

---

## Main Components

### WSMarketScanner (`wsScanner.ts`)
- Connects to `wss://ws-subscriptions-clob.polymarket.com/ws/market`
- Subscribes to market book updates
- Maintains in-memory market prices
- Emits events: `price`, `book`, `connected`, `disconnected`

### WSMarketValidator (`wsValidator.ts`)
- Buffers WebSocket updates for batch DB writes
- Runs arbitrage detection on every price update
- Executes paper trading cycle every 60 seconds
- Manages market selection (hourly refresh)

---

## Intervals

| Interval | Task |
|----------|------|
| 5 seconds | Flush buffered updates to DB |
| 60 seconds | Paper trading cycle |
| 15 minutes | Record P&L snapshot |
| 1 hour | Re-select trading markets, expire stale opportunities |

---

## Arbitrage Detection (Fast Path)

On every WebSocket price update:
1. Check if `yesAsk + noAsk < 0.995` (arbitrage threshold)
2. If found, immediately insert into `paper_markets` with reason='ARBITRAGE'
3. Track latency metrics (detection time, execution time)

---

## Paper Trading Flow

Each 60-second cycle:
1. Fetch active markets from `paper_markets` table
2. For ARBITRAGE markets: Place buy orders for both YES and NO
3. For regular markets: Place market-making orders (BUY low, SELL high)
4. Check pending orders for fills (compare against current best bid/ask)
5. Handle partial arbitrage fills (hedge imbalances)

---

## Dependencies

- **External**: Polymarket WebSocket, Polymarket REST API
- **Internal**: `database/`, `paperTrader/`
- **Database tables**: `market_snapshots`, `order_book_snapshots`, `opportunities`, `paper_*`

---

## Configuration

Environment variables:
- `DATABASE_URL` - PostgreSQL connection string
- `POLYMARKET_PRIVATE_KEY` - For API client (optional, read-only works without)
- `ARBITRAGE_THRESHOLD` - Sum threshold for arbitrage detection (default: 0.995)

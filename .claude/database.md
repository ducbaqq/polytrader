# Database Module

## Purpose

PostgreSQL connection pool and repository modules for data persistence. All market data, opportunities, paper trades, and crypto positions are stored here.

---

## Entry Point

`src/database/index.ts`

---

## Key Files

| File | Purpose |
|------|---------|
| `src/database/index.ts` | Connection pool, query helpers, transactions |
| `src/database/schema.ts` | Table verification, row counts, reset |
| `src/database/marketRepo.ts` | Market snapshot CRUD |
| `src/database/orderBookRepo.ts` | Order book snapshots |
| `src/database/opportunityRepo.ts` | Opportunity detection results |
| `src/database/paperTradingRepo.ts` | Paper trading tables |
| `src/database/wsRepo.ts` | WebSocket update batch inserts |
| `src/database/cryptoRepo.ts` | Crypto trading tables |

---

## Connection

Uses `pg` library with connection pool:

```typescript
const pool = new Pool({
  connectionString: process.env.DATABASE_URL,
  max: 20,
  idleTimeoutMillis: 30000,
  connectionTimeoutMillis: 10000,
  ssl: { rejectUnauthorized: false },
});
```

---

## Query Helpers

```typescript
// Execute query, return QueryResult
query(text: string, params?: any[]): Promise<QueryResult>

// Execute query, return rows array
queryRows<T>(text: string, params?: any[]): Promise<T[]>

// Execute query, return first row or null
queryOne<T>(text: string, params?: any[]): Promise<T | null>

// Execute in transaction
withTransaction<T>(fn: (client) => Promise<T>): Promise<T>

// Batch insert
batchInsert(client, table, columns, rows, batchSize)
```

---

## Required Tables

Verified by `schema.ts`:

**Scanning Tables:**
- `market_snapshots` - Market data per scan
- `order_book_snapshots` - YES/NO order books per scan
- `opportunities` - Detected opportunities (active/expired)

**Paper Trading Tables:**
- `paper_markets` - Selected markets for simulation
- `paper_orders` - Orders (PENDING/FILLED/EXPIRED)
- `paper_trades` - Executed trades with costs
- `paper_positions` - Current holdings
- `paper_pnl` - P&L history

**Analysis Tables:**
- `time_analysis` - Hourly pattern stats
- `category_analysis` - Performance by category
- `validation_summary` - Final report data

**Crypto Tables:**
- `crypto_markets` - Tracked crypto threshold markets
- `crypto_positions` - Open crypto positions
- `crypto_opportunities` - Detected mispricings
- `crypto_price_log` - Binance price history

---

## Common Operations

### Paper Trading Repo

```typescript
// Get active markets for trading
getActivePaperMarkets(): Promise<DBPaperMarket[]>

// Insert new paper market
insertPaperMarket(marketId, reason, question?)

// Get all positions
getPositions(): Promise<DBPosition[]>

// Record P&L snapshot
recordPnLSnapshot(cashBalance, initialCapital)

// Get trade statistics
getTotalTradeStats(): Promise<{ total_trades, total_fees, total_cash_flow }>
```

### Crypto Repo

```typescript
// Get markets by asset (BTC/ETH/SOL)
getCryptoMarketsByAsset(asset): Promise<CryptoMarket[]>

// Upsert crypto market
upsertCryptoMarket(market)

// Get open positions
getCryptoPositions(): Promise<CryptoPosition[]>

// Log opportunity
insertCryptoOpportunity(opportunity)

// Log price
insertPriceLog(asset, price)
```

---

## Environment Variables

```bash
DATABASE_URL=postgresql://user:password@host:port/database
```

---

## Schema Management

```bash
# Check table existence and row counts
npm run db-status

# Clear all data (fresh start)
npm run reset
```

# Paper Trading Engine

## Purpose

Simulated trading engine that places orders, tracks fills, and calculates P&L without risking real money. Includes realistic cost modeling (platform fees, gas, slippage).

---

## Entry Point

`src/paperTrader/index.ts`

---

## Key Files

| File | Purpose |
|------|---------|
| `src/paperTrader/index.ts` | `PaperTrader` class - orchestrates trading cycles |
| `src/paperTrader/orderManager.ts` | Order placement, fill detection, arbitrage orders |
| `src/paperTrader/costCalculator.ts` | Fee and slippage calculations |

---

## How It Works

### Trading Cycle (60 seconds)

```
1. Fetch active markets from paper_markets table
2. Separate ARBITRAGE markets from regular markets

3a. ARBITRAGE markets (prioritized):
    - Buy YES at best ask
    - Buy NO at best ask
    - Goal: Lock in guaranteed profit when YES + NO < 0.995

3b. Regular markets (market-making):
    - Place BUY order at best_bid + tick_improvement
    - Place SELL order at best_ask - tick_improvement
    - For both YES and NO tokens

4. Check pending orders for fills:
    - BUY fills if current best_ask <= order price
    - SELL fills if current best_bid >= order price

5. Handle partial arbitrage fills:
    - If only YES filled, sell YES at best bid
    - If only NO filled, sell NO at best bid
```

---

## Cost Model

Every trade incurs realistic costs:

| Cost Type | Amount | Notes |
|-----------|--------|-------|
| Platform Fee | 2% of trade value | Polymarket's actual fee |
| Gas Cost | $0.10 per trade | Polygon transaction |
| Slippage | 0.5% of trade value | Price impact estimate |

**Example**: $10 trade = $0.20 platform + $0.10 gas + $0.05 slippage = **$0.35 total**

---

## Configuration

```typescript
const DEFAULT_CONFIG: PaperTraderConfig = {
  orderSize: 100,           // 100 contracts per order
  tickImprovement: 0.01,    // Improve best bid/ask by 1 cent
  maxOrdersPerMarket: 2,    // Max concurrent orders
  tradingEnabled: true,     // Master switch
};
```

---

## Database Tables

| Table | Purpose |
|-------|---------|
| `paper_markets` | Markets selected for paper trading |
| `paper_orders` | Orders placed (PENDING/FILLED/EXPIRED) |
| `paper_trades` | Executed trades with costs |
| `paper_positions` | Current holdings per market/token |
| `paper_pnl` | P&L snapshots over time |

---

## Key Methods

### PaperTrader class

```typescript
// Run one trading cycle
async runCycle(): Promise<{
  ordersPlaced: number;
  ordersFilled: number;
  markets: string[];
  arbOrdersPlaced: number;
}>

// Get portfolio summary
async getPortfolioSummary(): Promise<PortfolioSummary>

// Record P&L snapshot
async recordPnLSnapshot(): Promise<void>
```

### Order Manager

```typescript
// Place market-making orders
placeMarketMakingOrders(marketId, tokenSide, size, tickImprovement)

// Place arbitrage orders (buy both YES and NO)
placeArbitrageOrders(marketId, size)

// Check pending orders for fills
checkFills(): Promise<number>

// Handle partial arb fills (hedge imbalances)
handlePartialArbitrageFills(marketIds)
```

---

## Dependencies

- **Internal**: `database/paperTradingRepo.ts`, `database/wsRepo.ts`
- **Database**: PostgreSQL with paper trading tables

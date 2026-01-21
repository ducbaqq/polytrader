# No-Betting Paper Trading System

## Purpose

Multi-strategy simulated trading system that supports multiple trading strategies running in parallel:

- **YES Buyer** (`yes-buyer`): Buys YES tokens when price is below expected win rate
- **NO Buyer** (`no-buyer`): Buys NO tokens when YES is overpriced by retail bettors

Each strategy can be run independently with its own portfolio, isolated via `strategy_id` in the database.

---

## Entry Point

`src/noPaperTraderCli.ts`

---

## Key Files

| File | Purpose |
|------|---------|
| `src/noPaperTrader/config.ts` | Strategy registry + config + keyword-based category detection |
| `src/noPaperTrader/types.ts` | Type definitions (Position, Trade, Portfolio, StrategyId, ScannedMarket) |
| `src/noPaperTrader/repository.ts` | Database operations with strategy isolation |
| `src/noPaperTrader/scanner.ts` | Direction-agnostic market scanner (returns both YES and NO prices) |
| `src/noPaperTrader/monitor.ts` | Strategy-aware position monitor (opens AND closes positions) |
| `src/noPaperTrader/report.ts` | Performance report generation per strategy |
| `src/noPaperTrader/dashboard.ts` | Live terminal dashboard with portfolio/position display |
| `src/noPaperTrader/wsProvider.ts` | WebSocket price provider singleton for real-time prices |
| `src/noPaperTrader/index.ts` | Main orchestrator class |

---

## Architecture

### Multi-Strategy Design with WebSocket

```
WSMarketScanner (WebSocket)
    │
    │  Maintains real-time price cache for all subscribed tokens
    │
    ├──────────────────────────────────────────┤
    │                                          │
    ▼                                          ▼
Monitor (yes-buyer)                    Monitor (no-buyer)
    │                                          │
    │ Gets prices from WebSocket cache         │ Gets prices from WebSocket cache
    │ Only calls Gamma API for resolution      │ Only calls Gamma API for resolution
    │                                          │
    ▼                                          ▼
Database (strategy_id isolation)       Database (strategy_id isolation)
```

**Key Points:**
- WebSocket provides real-time prices (eliminates 90%+ of REST API calls)
- Scanner and monitor share a singleton `WSPriceProvider`
- Falls back to REST API if WebSocket data is stale (>60s)
- Each monitor instance handles one strategy
- Database tables use `strategy_id` for isolation

---

## CLI Commands

All commands that operate on positions require `--strategy`:

| Command | Description |
|---------|-------------|
| `npm run no-trader -- scan` | Run scanner continuously (shared by all strategies) |
| `npm run no-trader -- monitor --strategy=<id>` | Run monitor for a specific strategy |
| `npm run no-trader -- status --strategy=<id>` | Check portfolio status for a strategy |
| `npm run no-trader -- report --strategy=<id>` | Generate performance report for a strategy |
| `npm run no-trader -- reset --force` | Reset ALL paper trading data |
| `npm run no-trader -- strategies` | List available strategies |
| `npm run no-trader -- scan-once` | Run single market scan |
| `npm run no-trader -- monitor-once --strategy=<id>` | Run single monitor cycle |

### Running Multiple Strategies

Run strategies in parallel in separate terminals:

```bash
# Terminal 1 - YES Buyer strategy
npm run no-trader -- monitor --strategy=yes-buyer

# Terminal 2 - NO Buyer strategy
npm run no-trader -- monitor --strategy=no-buyer
```

Each monitor internally runs its own scanner and evaluates markets against its strategy criteria.

### Monitor Options

```bash
npm run no-trader -- monitor --strategy=<id> \
  --interval 30 \         # Monitor interval in seconds
  --scan-interval 120 \   # How often to scan for new markets
  --capital 2500 \        # Initial capital
  --size 50 \             # Position size per trade
  --take-profit 90 \      # Take profit threshold (%)
  --stop-loss 25 \        # Stop loss threshold (%)
  --no-dashboard \        # Disable live dashboard
  --no-websocket          # Disable WebSocket (use REST API only)
```

---

## Strategy Registry

Strategies are defined in `config.ts`:

```typescript
export const STRATEGY_REGISTRY: Record<StrategyId, StrategyDefinition> = {
  'yes-buyer': {
    id: 'yes-buyer',
    name: 'YES Buyer',
    side: 'YES',
    minPrice: 0.10,
    maxPrice: 0.60,
    minEdge: 0.02,
    categoryWinRates: {
      'Crypto': 0.70,
      'Entertainment': 0.65,
      // ...
    },
  },
  'no-buyer': {
    id: 'no-buyer',
    name: 'NO Buyer',
    side: 'NO',
    minPrice: 0.10,
    maxPrice: 0.60,
    minEdge: 0.02,
    categoryWinRates: {
      'Crypto': 0.30,
      'Entertainment': 1.00,
      // ...
    },
  },
};
```

Helper functions:
- `getStrategy(id)` - Get strategy definition (throws if invalid)
- `getAvailableStrategies()` - List all strategy IDs
- `isValidStrategy(id)` - Check if strategy ID is valid

---

## Edge Calculation

Each strategy has its own category win rates:

```
Edge = Strategy's Category Win Rate - Current Price for Strategy's Side

YES Buyer Example (Crypto):
  "Will Bitcoin reach $100K?" YES priced at 55%
  Edge = 70% - 55% = 15%

NO Buyer Example (Entertainment):
  "Will Taylor Swift win Grammy?" NO priced at 30%
  Edge = 100% - 30% = 70%

Only enter if Edge >= strategy.minEdge (default 2%)
```

---

## Database Schema Changes

All tables now support multi-strategy via `strategy_id`:

### no_portfolio

| Column | Type | Description |
|--------|------|-------------|
| **strategy_id** | text | **PK** - Strategy identifier (yes-buyer, no-buyer) |
| cash_balance | numeric | Available cash for trading |
| initial_capital | numeric | Starting capital amount |
| ... | | |

### no_positions

| Column | Type | Description |
|--------|------|-------------|
| id | text | **PK** - Unique position ID |
| **strategy_id** | text | Strategy that owns this position |
| token_side | text | Token type: YES or NO |
| ... | | |

### no_trades

| Column | Type | Description |
|--------|------|-------------|
| id | text | **PK** - Unique trade ID |
| **strategy_id** | text | Strategy that made this trade |
| ... | | |

### no_daily_snapshots

| Column | Type | Description |
|--------|------|-------------|
| **strategy_id** | text | **PK (composite)** - Strategy identifier |
| date | date | **PK (composite)** - Snapshot date |
| ... | | |

---

## ScannedMarket Type

The scanner returns direction-agnostic market data:

```typescript
interface ScannedMarket {
  marketId: string;
  question: string;
  category: string;
  yesTokenId: string;
  noTokenId: string;
  yesPrice: number;    // Both prices returned
  noPrice: number;
  volume24h: number;
  createdAt: Date;
  endDate: Date;
  ageHours: number;
  daysToResolution: number;
}
```

Monitors use the price for their strategy's side to evaluate entry.

---

## Monitor Flow

1. **Scan for markets**: Runs scanner to get ScannedMarket list
2. **Evaluate entries**: For each market:
   - Skip if already have position for this market in this strategy
   - Get price for strategy's side (YES or NO)
   - Check price range against strategy config
   - Calculate edge using strategy's categoryWinRates
   - Check portfolio has capital
   - Open position if all checks pass
3. **Check existing positions**: For each open position:
   - Check if market resolved → close at resolution price
   - Check take profit → sell if price >= 90%
   - Check stop loss → sell if price <= 25%

---

## Position Resolution

```
YES position wins $1 if YES wins, $0 if NO wins
NO position wins $1 if NO wins, $0 if YES wins
```

---

## Environment Variables

```bash
# Optional overrides (apply to all strategies)
NO_TRADER_INITIAL_CAPITAL=2500
NO_TRADER_POSITION_SIZE=50
NO_TRADER_MIN_EDGE=0.02
NO_TRADER_TAKE_PROFIT=0.90
NO_TRADER_STOP_LOSS=0.25
NO_TRADER_SCAN_INTERVAL=60
NO_TRADER_MAX_PRICE=0.60
NO_TRADER_MIN_VOLUME=1000
NO_TRADER_SCAN_CONCURRENCY=10
```

---

## WebSocket Integration

The monitor uses WebSocket for real-time price updates to avoid REST API rate limits (429 errors).

### How It Works

1. **On startup**: `initWSProvider()` creates singleton WebSocket connection
2. **Price lookups**: Check `wsProvider.getPrice(tokenId)` first (instant, no API call)
3. **Freshness check**: If data is >60s old, fall back to REST API
4. **Dashboard**: Shows WebSocket connection status and cached prices count

### WebSocket vs REST

| Operation | With WebSocket | Without (--no-websocket) |
|-----------|---------------|--------------------------|
| Price check (TP/SL) | Cache lookup | REST API call |
| Scanner prices | Cache lookup | REST API call |
| Resolution check | REST API (no WS alternative) | REST API |
| API calls/min | ~N (resolution only) | ~150+ |

### Fallback Behavior

- WebSocket disconnection → auto-reconnect with backoff
- Stale data (>60s) → REST API fallback
- WS initialization failure → runs in REST-only mode

---

## Rate Limiting

To prevent 429 errors from Polymarket API:

| Setting | Default | Description |
|---------|---------|-------------|
| WebSocket | enabled | Real-time prices via wss://ws-subscriptions-clob.polymarket.com |
| Scan concurrency | 10 | Markets processed in parallel per scan |
| API rate limit | 3/sec | Requests per second (in apiClient.ts) |
| Monitor scan interval | 120s | How often monitor rescans for new markets |
| Scanner interval | 60s | How often standalone scanner runs |

Override via CLI:
```bash
npm run no-trader -- monitor --strategy=no-buyer --scan-interval 180
npm run no-trader -- monitor --strategy=no-buyer --no-websocket  # Force REST-only
npm run no-trader -- scan --concurrency 5
```

---

## Live Dashboard

When running `npm run no-trader -- monitor --strategy=<id>`, a live terminal dashboard displays:
- Strategy name in header
- WebSocket connection status (when enabled)
- Portfolio stats for that strategy
- Open positions with entry/current prices and P&L
- Lifetime exit stats (TP/SL/Resolved counts)

Use `--no-dashboard` for plain text output mode.

---

## Dependencies

- **Internal**: `apiClient.ts`, `database/index.ts`, `wsScanner.ts`
- **External**: axios, cli-table3, chalk, commander, ws
- **Database**: PostgreSQL with no_* tables

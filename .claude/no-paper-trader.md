# No-Betting Paper Trading System

## Purpose

Simulated trading system that bets on market outcomes based on category analysis:

- **Crypto**: Buys **YES** (price prediction markets tend to resolve YES)
- **Entertainment, Finance, Weather, Tech**: Buys **NO** (high NO win rates due to retail overbuying YES)

---

## Entry Point

`src/noPaperTraderCli.ts`

---

## Key Files

| File | Purpose |
|------|---------|
| `src/noPaperTrader/config.ts` | Strategy config + keyword-based category detection |
| `src/noPaperTrader/types.ts` | Type definitions (Position, Trade, Portfolio, TokenSide) |
| `src/noPaperTrader/repository.ts` | Database operations for positions, trades, portfolio |
| `src/noPaperTrader/scanner.ts` | Market scanner - selects YES or NO based on category |
| `src/noPaperTrader/monitor.ts` | Position monitor for TP/SL and resolution |
| `src/noPaperTrader/report.ts` | Performance report generation |
| `src/noPaperTrader/dashboard.ts` | Live terminal dashboard with portfolio/position display |
| `src/noPaperTrader/index.ts` | Main orchestrator class |

---

## CLI Commands

| Command | Description |
|---------|-------------|
| `npm run no-trader:start` | Start paper trader with dashboard (scanner + monitor together) |
| `npm run no-trader:scan` | Run scanner continuously (independent process) |
| `npm run no-trader:monitor` | Run monitor continuously (independent process) |
| `npm run no-trader:status` | Check current portfolio status |
| `npm run no-trader:report` | Generate full performance report |
| `npm run no-trader:reset` | Reset all paper trading data |
| `npm run no-trader:scan-once` | Run single market scan |
| `npm run no-trader:monitor-once` | Run single monitor cycle |

### Separate Processes Mode

Run scanner and monitor as independent processes (they share the database):

```bash
# Terminal 1 - Scanner finds and opens positions
npm run no-trader:scan -- --interval 60 --concurrency 20

# Terminal 2 - Monitor manages TP/SL/resolution
npm run no-trader:monitor -- --interval 30
```

Scanner options: `--interval`, `--capital`, `--size`, `--concurrency`
Monitor options: `--interval`, `--take-profit`, `--stop-loss`

### Start Options (Combined Mode)

```bash
npm run no-trader -- start \
  --capital 2500 \
  --size 50 \
  --min-edge 5 \
  --take-profit 90 \
  --stop-loss 25 \
  --scan-interval 60 \
  --monitor-interval 30 \
  --no-dashboard          # Optional: disable live dashboard
```

---

## Strategy Configuration

```typescript
const DEFAULT_STRATEGY_CONFIG = {
  // Capital
  initialCapital: 2500,          // $2,500 starting balance
  positionSize: 50,              // $50 per position

  // Entry conditions
  categories: ['Crypto', 'Entertainment', 'Finance', 'Weather', 'Tech'],
  yesCategories: ['Crypto'],     // These buy YES, others buy NO
  minDurationDays: 1,            // Resolves in 1+ days
  maxDurationDays: 7,            // Resolves in 7 or fewer days
  minPrice: 0,                   // No minimum price
  maxPrice: 0.60,                // Max 60¢ (looking for underpriced tokens)
  minVolume: 1000,               // Min $1K volume
  maxVolume: Infinity,           // No max cap
  minEdge: 0.02,                 // 2% minimum edge
  maxTimeBelowThreshold: 0.75,   // Skip if price low >75% of lifetime

  // Historical win rates
  // For yesCategories: YES win rate; for others: NO win rate
  categoryWinRates: {
    'Crypto': 1.00,              // 100% YES win rate
    'Entertainment': 1.00,       // 100% NO win rate
    'Finance': 0.986,            // 98.6% NO win rate
    'Weather': 0.985,            // 98.5% NO win rate
    'Tech': 0.982,               // 98.2% NO win rate
  },

  // Exit conditions
  takeProfitThreshold: 0.90,     // Sell if price reaches 90%
  stopLossThreshold: 0.25,       // Sell if price drops to 25%

  // Costs
  slippagePercent: 0.005,        // 0.5% slippage

  // Polling
  scanIntervalSeconds: 60,
  monitorIntervalSeconds: 30,

  // Performance
  scanConcurrency: 20,           // Process 20 markets in parallel
};
```

---

## YES vs NO Logic

The system determines which side to buy based on category:

| Category | Token Side | Rationale |
|----------|------------|-----------|
| Crypto | **YES** | Price prediction markets (dips, above $X) tend to resolve YES |
| Entertainment | NO | Retail overbuys YES on exciting outcomes |
| Finance | NO | Same bias pattern |
| Weather | NO | Same bias pattern |
| Tech | NO | Same bias pattern |

**Position Resolution:**
- YES position wins $1 if YES wins, $0 if NO wins
- NO position wins $1 if NO wins, $0 if YES wins

---

## Category Detection

Since the Polymarket API doesn't provide categories for open markets, we use **keyword-based detection** from the question text:

| Category | Keywords |
|----------|----------|
| Crypto | bitcoin, btc, ethereum, eth, crypto, solana, coinbase, etc. |
| Weather | weather, temperature, hurricane, tornado, storm, etc. |
| Entertainment | movie, oscar, grammy, netflix, taylor swift, etc. |
| Finance | stock, s&p, fed, interest rate, inflation, ipo, etc. |
| Tech | apple, google, openai, chatgpt, spacex, nvidia, etc. |

**Excluded**: Sports and Politics (lower win rates in alpha analysis)

---

## Edge Calculation

```
Edge = Historical Win Rate - Current Price

Example (Crypto - YES side):
  "Will Bitcoin dip to $88K?" YES priced at 55%
  Edge = 100% - 55% = 45%

Example (Finance - NO side):
  "Will Fed raise rates?" NO priced at 55%
  Edge = 98.6% - 55% = 43.6%

Only enter if Edge >= 2%
```

---

## Brief Opportunity Window Rule

Markets are rejected if the price has been at/below the entry threshold (60¢) for more than 75% of the market's lifetime. This filters out "stale" opportunities.

Implementation: Fetches price history from CLOB API and calculates `(points below threshold) / (total points)`.

---

## How It Works

### Scanning Cycle

1. Fetch all active markets from Polymarket Gamma API
2. Detect category using keyword matching on question text
3. Filter to target categories
4. For each market:
   - Determine token side (YES for Crypto, NO for others)
   - Get price for that token side
   - Validate entry conditions (duration, price ≤60¢, volume ≥$1K)
   - Calculate edge vs historical win rate
   - Check brief opportunity window (price history)
   - If eligible and sufficient capital, open position

### Monitoring Cycle

1. Get all open positions
2. For each position:
   - Check if market resolved → close at resolution price
   - Get current price for the position's token side
   - Check take profit (price >= 90%) → sell
   - Check stop loss (price <= 25%) → sell
   - Otherwise, hold

### Position Lifecycle

```
OPEN → CLOSED_TP (take profit)
     → CLOSED_SL (stop loss)
     → CLOSED_RESOLVED (market resolved)
```

---

## Database Tables

| Table | Purpose |
|-------|---------|
| `no_positions` | Open and closed positions (includes `token_side` column) |
| `no_trades` | Entry and exit trades |
| `no_portfolio` | Current portfolio state |
| `no_daily_snapshots` | Daily equity snapshots |
| `no_scanned_markets` | Market scan history |

---

## Environment Variables

```bash
# Optional overrides
NO_TRADER_INITIAL_CAPITAL=2500
NO_TRADER_POSITION_SIZE=50
NO_TRADER_MIN_EDGE=0.02
NO_TRADER_TAKE_PROFIT=0.90
NO_TRADER_STOP_LOSS=0.25
NO_TRADER_SCAN_INTERVAL=60
NO_TRADER_MAX_PRICE=0.60
NO_TRADER_MIN_VOLUME=1000
NO_TRADER_MAX_VOLUME=<infinity>           # Optional, defaults to no cap
NO_TRADER_MAX_TIME_BELOW_THRESHOLD=0.75
NO_TRADER_SCAN_CONCURRENCY=20
```

---

## Performance Report

The report shows:

- **Summary**: Period, capital, equity, total P&L
- **Trade Statistics**: Total trades, win rate, avg P&L per trade
- **Best/Worst Trades**: Highest and lowest P&L trades
- **Category Performance**: Breakdown by category
- **Equity Curve**: Daily equity snapshots
- **Open Positions**: Current active positions

---

## Live Dashboard

When running `npm run no-trader -- start`, a live terminal dashboard displays portfolio and positions with entry/current prices and P&L.

Use `--no-dashboard` for plain text output mode.

---

## Dependencies

- **Internal**: `apiClient.ts`, `database/index.ts`, `alphaAnalysis/priceHistoryFetcher.ts`
- **External**: axios, cli-table3, chalk, commander
- **Database**: PostgreSQL with no_* tables

---

## Market Re-scanning Behavior

The scanner tracks markets in `no_scanned_markets` to avoid redundant processing. However, **only permanent rejections are persisted**:

| Rejection Type | Persisted? | Reason |
|----------------|------------|--------|
| No market data / No token | Yes | Structural issue, won't change |
| No end date specified | Yes | Market structure |
| Category not in target | Yes | Category won't change |
| Price above max | No | Prices change constantly |
| Duration out of range | No | Time passes |
| Volume below min | No | Volume can increase |
| Edge below min | No | Depends on price |
| No liquidity | No | Liquidity could appear |

Markets rejected for temporary reasons are re-evaluated on each scan cycle.

Implementation: `isPermanentRejection()` in `scanner.ts`.

# No-Betting Paper Trading System

## Purpose

Simulated trading system that bets **No** on high win-rate categories based on alpha analysis:

> Crypto, Entertainment, Finance, Weather, and Tech markets show historically high No win rates (98-100%) due to retail bettors emotionally overbuying Yes on exciting/fearful outcomes. If No is priced below its historical win rate, there may be edge.

---

## Entry Point

`src/noPaperTraderCli.ts`

---

## Key Files

| File | Purpose |
|------|---------|
| `src/noPaperTrader/config.ts` | Strategy config + keyword-based category detection |
| `src/noPaperTrader/types.ts` | Type definitions (Position, Trade, Portfolio, etc.) |
| `src/noPaperTrader/repository.ts` | Database operations for positions, trades, portfolio |
| `src/noPaperTrader/scanner.ts` | Market scanner with price history checks |
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
  side: 'NO',                    // Always bet No

  // Entry conditions
  categories: ['Crypto', 'Entertainment', 'Finance', 'Weather', 'Tech'],
  minDurationDays: 1,            // Resolves in 1+ days
  maxDurationDays: 7,            // Resolves in 7 or fewer days
  minNoPrice: 0,                 // No minimum price
  maxNoPrice: 0.60,              // Max 60¢ (looking for underpriced No)
  minVolume: 1000,               // Min $1K volume
  maxVolume: Infinity,           // No max cap (was $50K)
  minEdge: 0.02,                 // 2% minimum edge (was 5%)
  maxTimeBelowThreshold: 0.75,   // Skip if price low >75% of lifetime (was 25%)

  // Historical win rates from alpha analysis
  categoryWinRates: {
    'Crypto': 1.00,              // 100%
    'Entertainment': 1.00,       // 100%
    'Finance': 0.986,            // 98.6%
    'Weather': 0.985,            // 98.5%
    'Tech': 0.982,               // 98.2%
  },

  // Exit conditions
  takeProfitThreshold: 0.90,     // Sell if No reaches 90%
  stopLossThreshold: 0.25,       // Sell if No drops to 25%

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
Edge = Historical Category No Win Rate - Current No Price

Example:
  Finance market, No priced at 55%
  Edge = 98.6% - 55% = 43.6%

  Only enter if Edge >= 2%
```

---

## Brief Opportunity Window Rule

Markets are rejected if the No price has been at/below the entry threshold (60¢) for more than 75% of the market's lifetime. This filters out "stale" opportunities that everyone already knows about.

Implementation: Fetches price history from CLOB API and calculates `(points below threshold) / (total points)`.

---

## How It Works

### Scanning Cycle

1. Fetch all active markets from Polymarket Gamma API
2. Detect category using keyword matching on question text
3. Filter to target categories (Crypto, Entertainment, Finance, Weather, Tech)
4. For each market:
   - Check if already scanned or have position
   - Validate entry conditions (duration, price ≤60¢, volume ≥$1K)
   - Calculate edge vs historical win rate
   - Check brief opportunity window (price history)
   - If eligible and sufficient capital, open position

### Monitoring Cycle

1. Get all open positions
2. For each position:
   - Check if market resolved → close at resolution price
   - Get current No price
   - Check take profit (No >= 90%) → sell
   - Check stop loss (No <= 25%) → sell
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
| `no_positions` | Open and closed positions |
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
NO_TRADER_MAX_NO_PRICE=0.60
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

When running `npm run no-trader -- start`, a live terminal dashboard displays:

```
╔══════════════════════════════════════════════════════════════════════════════╗
║  NO PAPER TRADER                                                             ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  🔍 Scanning: 342/2613                   Runtime: 18s  10:19:23 PM           ║
║  Scans: 0  Opened: 0  Closed: 0                                              ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  PORTFOLIO                                                                   ║
║  ├─ Initial Capital:   $2500.00                                              ║
║  ├─ Cash Balance:      $2449.75                                              ║
║  ├─ Position Value:    $84.92                                                ║
║  ├─ Total Equity:      $2534.67                                              ║
║  ├─ Unrealized P&L:    +$34.67                                               ║
║  └─ Total P&L:         +$34.67                                               ║
╠══════════════════════════════════════════════════════════════════════════════╣
║  OPEN POSITIONS (1)                                                          ║
║  │ Will the price of Bitcoin be above $9... │ 58.0%  │ 99.0%  │ +$34.67      │
╚══════════════════════════════════════════════════════════════════════════════╝
```

- **Status**: Shows scanning progress (e.g., `342/2613`), monitoring, or idle
- **Portfolio**: Initial capital, cash, position value, equity, unrealized/total P&L
- **Positions**: Entry price, current price, unrealized P&L per position
- **Refresh**: Updates every 5 seconds + on scan progress

Use `--no-dashboard` for plain text output mode.

---

## Dependencies

- **Internal**: `apiClient.ts`, `database/index.ts`, `alphaAnalysis/priceHistoryFetcher.ts`
- **External**: axios, cli-table3, chalk, commander
- **Database**: PostgreSQL with no_* tables

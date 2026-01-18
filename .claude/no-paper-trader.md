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
| `src/noPaperTrader/index.ts` | Main orchestrator class |

---

## CLI Commands

| Command | Description |
|---------|-------------|
| `npm run no-trader:start` | Start paper trader (runs continuously) |
| `npm run no-trader:status` | Check current portfolio status |
| `npm run no-trader:report` | Generate full performance report |
| `npm run no-trader:reset` | Reset all paper trading data |
| `npm run no-trader:scan` | Run single market scan |
| `npm run no-trader:monitor` | Run single monitor cycle |

### Start Options

```bash
npm run no-trader -- start \
  --capital 2500 \
  --size 50 \
  --min-edge 5 \
  --take-profit 90 \
  --stop-loss 25 \
  --scan-interval 60 \
  --monitor-interval 30
```

---

## Strategy Configuration

```typescript
const DEFAULT_STRATEGY_CONFIG = {
  // Capital
  initialCapital: 2500,          // $2,500 starting balance
  positionSize: 50,              // $50 per position
  side: 'NO',                    // Always bet No

  // Entry conditions (updated from alpha analysis)
  categories: ['Crypto', 'Entertainment', 'Finance', 'Weather', 'Tech'],
  minDurationDays: 1,            // Resolves in 1+ days
  maxDurationDays: 7,            // Resolves in 7 or fewer days
  minNoPrice: 0,                 // No minimum price
  maxNoPrice: 0.60,              // Max 60¢ (brief opportunity window)
  minVolume: 1000,               // Min $1,000 volume
  maxVolume: 50000,
  minEdge: 0.05,                 // 5% minimum edge
  maxTimeBelowThreshold: 0.25,   // Skip if price was ≤60¢ for >25% of lifetime

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

  Only enter if Edge >= 5%
```

---

## Brief Opportunity Window Rule

Markets are rejected if the No price has been at/below the entry threshold (60¢) for more than 25% of the market's lifetime. This filters out "stale" opportunities that everyone already knows about.

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
NO_TRADER_MIN_EDGE=0.05
NO_TRADER_TAKE_PROFIT=0.90
NO_TRADER_STOP_LOSS=0.25
NO_TRADER_SCAN_INTERVAL=60
NO_TRADER_MAX_NO_PRICE=0.60
NO_TRADER_MIN_VOLUME=1000
NO_TRADER_MAX_TIME_BELOW_THRESHOLD=0.25
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

## Dependencies

- **Internal**: `apiClient.ts`, `database/index.ts`, `alphaAnalysis/priceHistoryFetcher.ts`
- **External**: axios, cli-table3, chalk, commander
- **Database**: PostgreSQL with no_* tables

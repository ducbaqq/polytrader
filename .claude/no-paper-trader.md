# No-Betting Paper Trading System

## Purpose

Simulated trading system that bets **No** on Entertainment and Weather markets to validate a strategy hypothesis:

> Entertainment and Weather markets show historically high No win rates (85.8% and 83.1% respectively) due to retail bettors emotionally overbuying Yes on exciting/fearful outcomes. If No is priced below its historical win rate, there may be edge.

---

## Entry Point

`src/noPaperTraderCli.ts`

---

## Key Files

| File | Purpose |
|------|---------|
| `src/noPaperTrader/config.ts` | Strategy configuration (capital, edge thresholds, TP/SL) |
| `src/noPaperTrader/types.ts` | Type definitions (Position, Trade, Portfolio, etc.) |
| `src/noPaperTrader/repository.ts` | Database operations for positions, trades, portfolio |
| `src/noPaperTrader/scanner.ts` | Market scanner to find eligible markets |
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

  // Entry conditions
  categories: ['Entertainment', 'Weather'],
  maxMarketAgeHours: 48,         // Max 48 hours old
  minDurationDays: 1,            // Resolves in 1+ days
  maxDurationDays: 7,            // Resolves in 7 or fewer days
  minNoPrice: 0.50,              // No price between 50-75%
  maxNoPrice: 0.75,
  minVolume: 200,                // Volume $200-$50,000
  maxVolume: 50000,
  minEdge: 0.05,                 // 5% minimum edge

  // Historical win rates
  categoryWinRates: {
    'Entertainment': 0.858,      // 85.8%
    'Weather': 0.831,            // 83.1%
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

## Edge Calculation

```
Edge = Historical Category No Win Rate - Current No Price

Example:
  Entertainment market, No priced at 65%
  Edge = 85.8% - 65% = 20.8%

  Only enter if Edge >= 5%
```

---

## How It Works

### Scanning Cycle

1. Fetch all active markets from Polymarket Gamma API
2. Filter to Entertainment and Weather categories
3. For each market:
   - Check if already scanned
   - Check if already have position
   - Validate entry conditions (age, duration, price, volume)
   - Calculate edge
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
```

---

## Performance Report

The report shows:

- **Summary**: Period, capital, equity, total P&L
- **Trade Statistics**: Total trades, win rate, avg P&L per trade
- **Best/Worst Trades**: Highest and lowest P&L trades
- **Category Performance**: Breakdown by Entertainment/Weather
- **Equity Curve**: Daily equity snapshots
- **Open Positions**: Current active positions

---

## Dependencies

- **Internal**: `apiClient.ts`, `database/index.ts`
- **External**: axios, uuid, cli-table3, chalk, commander
- **Database**: PostgreSQL with no_* tables

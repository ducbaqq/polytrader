# Polymarket Validation System - Architecture Overview

## System Purpose
A 24/7 market validation system that scans Polymarket, simulates paper trading, and generates a GO/NO-GO recommendation for building a real trading bot.

---

## NPM Scripts (package.json)

| Script | Command | Purpose |
|--------|---------|---------|
| `npm run validate` | Main command | Runs the full validation system 24/7 |
| `npm run db-status` | Utility | Shows table row counts and schema status |
| `npm run report` | Analysis | Generates validation report from collected data |
| `npm run reset` | Utility | Clears all database tables (fresh start) |
| `npm run scan-once` | Debug | Runs a single market scan and exits |
| `npm run dev` | Legacy | Runs the original discovery bot with terminal dashboard |
| `npm run build` | Build | Compiles TypeScript to JavaScript |
| `npm run start` | Production | Runs compiled JavaScript |

---

## What Happens When You Run `npm run validate`

### Startup Sequence
```
1. Load .env configuration
2. Initialize PostgreSQL connection pool
3. Verify database schema (11 tables)
4. Run initial full market scan
5. Select paper trading markets (1 liquid, 1 medium volume)
6. Start all interval timers
```

### Continuous Operations (Running 24/7)

| Interval | Action | Details |
|----------|--------|---------|
| **60 seconds** | Full Scan | Fetches ~1000 markets from Polymarket API, stores snapshots, detects opportunities |
| **15 seconds** | Priority Scan | Scans only top 100 markets by volume (faster updates) |
| **60 seconds** | Paper Trading | Places market-making orders, checks for fills, updates positions |
| **15 minutes** | P&L Snapshot | Records current portfolio value to database |
| **1 hour** | Hourly Tasks | Expires stale opportunities, re-selects paper markets if needed |

---

## Data Flow

```
┌─────────────────────────────────────────────────────────────────┐
│                        VALIDATOR                                 │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │                    FULL SCAN (60s)                        │  │
│  │  1. Fetch markets from Polymarket Gamma API               │  │
│  │  2. Build order books (YES/NO tokens)                     │  │
│  │  3. Store in market_snapshots + order_book_snapshots      │  │
│  │  4. Run OpportunityDetector                               │  │
│  │  5. Upsert opportunities (new/expired)                    │  │
│  │  6. Update priority market list                           │  │
│  └──────────────────────────────────────────────────────────┘  │
│                                                                  │
│  ┌──────────────────────────────────────────────────────────┐  │
│  │                 PAPER TRADING (60s)                       │  │
│  │  1. Get active paper markets from DB                      │  │
│  │  2. For each market + token (YES/NO):                     │  │
│  │     - Place BUY order at best_bid + tick                  │  │
│  │     - Place SELL order at best_ask - tick                 │  │
│  │  3. Check pending orders for fills                        │  │
│  │  4. Record trades, update positions                       │  │
│  │  5. Expire old unfilled orders (>5 min)                   │  │
│  └──────────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────────┘
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────┐
│                      POSTGRESQL DATABASE                         │
├─────────────────────────────────────────────────────────────────┤
│  SCANNING TABLES:                                                │
│  • market_snapshots     - Market data per scan                  │
│  • order_book_snapshots - YES/NO order books per scan           │
│  • opportunities        - Detected opportunities (active/expired)│
├─────────────────────────────────────────────────────────────────┤
│  PAPER TRADING TABLES:                                           │
│  • paper_markets   - Selected markets for simulation            │
│  • paper_orders    - Placed orders (PENDING/FILLED/EXPIRED)     │
│  • paper_trades    - Executed trades with costs                 │
│  • paper_positions - Current holdings per market/token          │
│  • paper_pnl       - P&L snapshots over time                    │
├─────────────────────────────────────────────────────────────────┤
│  ANALYSIS TABLES:                                                │
│  • time_analysis       - Hourly pattern stats                   │
│  • category_analysis   - Performance by market category         │
│  • validation_summary  - Final report data                      │
└─────────────────────────────────────────────────────────────────┘
```

---

## Opportunity Detection

The `OpportunityDetector` analyzes each market for:

| Type | Condition | What It Means |
|------|-----------|---------------|
| **arbitrage** | YES + NO prices > 99.5% | Guaranteed profit if you buy both sides |
| **mispricing** | Earlier event priced higher than later | Temporal inconsistency |
| **wide_spread** | Spread > 10% | Market making opportunity |
| **thin_book** | < 5 makers | Low competition for market making |

---

## Paper Trading Cost Model

Every simulated trade includes realistic costs:

| Cost Type | Amount | Purpose |
|-----------|--------|---------|
| Platform Fee | 2% of trade value | Polymarket's actual fee |
| Gas Cost | $0.10 per trade | Polygon transaction cost |
| Slippage | 0.5% of trade value | Price impact estimate |

**Example**: $10 trade → $0.20 platform + $0.10 gas + $0.05 slippage = **$0.35 total cost**

---

## File Structure

```
src/
├── index.ts           # CLI entry point (commander.js)
├── validator.ts       # Main orchestrator (intervals, coordination)
├── apiClient.ts       # Polymarket API wrapper
├── scanner.ts         # Market scanning logic
├── detector.ts        # Opportunity detection algorithms
├── database/
│   ├── index.ts       # Connection pool, query helpers
│   ├── schema.ts      # Table verification
│   ├── marketRepo.ts  # Market snapshot CRUD
│   ├── orderBookRepo.ts
│   ├── opportunityRepo.ts
│   └── paperTradingRepo.ts
├── paperTrader/
│   ├── index.ts       # Paper trading orchestrator
│   ├── orderManager.ts # Order placement, fills
│   └── costCalculator.ts
└── analyzer/
    ├── timeAnalyzer.ts
    ├── categoryAnalyzer.ts
    └── reportGenerator.ts
```

---

## End Goal

After running for 7 days:
1. Run `npm run report`
2. System analyzes all collected data
3. Generates recommendation: **BUILD_BOT** / **MARGINAL** / **DONT_BUILD**
4. Based on: fill rates, P&L, opportunity frequency, cost coverage

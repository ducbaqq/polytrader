# CLAUDE.md

Guidelines for AI assistants working on this codebase.

## Project Overview

Polymarket trading bot with:
- Market scanner and opportunity detector
- Paper trading engine for strategy validation
- PostgreSQL database for persistence

## Code Quality Rules

### SQL Queries

**Always test complex SQL queries against the database before committing.**

When writing or modifying SQL queries with GROUP BY:
1. Every non-aggregated column in SELECT must be in GROUP BY
2. Every non-aggregated column in ORDER BY must be in GROUP BY
3. Columns from LEFT JOINed tables need aggregation (MAX, MIN, SUM, etc.) if not in GROUP BY

Example of the bug pattern to avoid:
```sql
-- BAD: perf.past_trades is not in GROUP BY and not aggregated
SELECT mp.market_id, mp.question
FROM market_prices mp
LEFT JOIN market_performance perf ON mp.market_id = perf.market_id
GROUP BY mp.market_id, mp.question
ORDER BY perf.past_trades DESC  -- ERROR!

-- GOOD: Use MAX() for columns from LEFT JOINed tables
ORDER BY MAX(perf.past_trades) DESC
```

**Validation step:** Run the actual query against the database with a simple test before committing:
```bash
PGPASSWORD='...' psql "postgresql://..." -c "YOUR_QUERY LIMIT 1"
```

### TypeScript

- Parse PostgreSQL numeric columns as numbers: `parseFloat(String(value))`
- PostgreSQL returns numeric types as strings in node-pg

## Testing Checklist

Before committing changes to:
- [ ] **Database queries**: Run the query manually to verify it executes without errors
- [ ] **Paper trading logic**: Check that orders are placed and fills are processed
- [ ] **API client changes**: Verify market data is fetched correctly

## Architecture Notes

- `validator.ts` - Main orchestrator, runs scans and paper trading cycles
- `paperTrader/` - Paper trading engine (orders, fills, positions)
- `database/` - PostgreSQL repositories
- Market selection happens at startup and hourly via `selectPaperTradingMarkets()`

## Database Schema: NO Trader Tables

The `no_*` tables store data for multi-strategy paper trading (YES-buyer and NO-buyer strategies).
All tables use `strategy_id` to isolate data between strategies.

### no_portfolio

One row per strategy tracking portfolio state.

| Column | Type | Default | Description |
|--------|------|---------|-------------|
| strategy_id | text | - | **PK** - Strategy identifier (yes-buyer, no-buyer) |
| cash_balance | numeric | - | Available cash for trading |
| initial_capital | numeric | - | Starting capital amount |
| realized_pnl | numeric | 0 | Total realized profit/loss |
| total_trades | integer | 0 | Count of all closed trades |
| winning_trades | integer | 0 | Count of profitable trades |
| losing_trades | integer | 0 | Count of losing trades |
| best_trade | numeric | NULL | Highest single trade PnL |
| worst_trade | numeric | NULL | Lowest single trade PnL |
| last_updated | timestamp | now() | Last modification time |

### no_positions

Tracks open and closed trading positions per strategy.

| Column | Type | Default | Description |
|--------|------|---------|-------------|
| id | text | - | **PK** - Unique position ID |
| strategy_id | text | 'no-buyer' | Strategy that owns this position |
| market_id | text | - | Polymarket market identifier |
| token_id | text | - | Specific token being traded |
| token_side | text | 'NO' | Token type: YES or NO |
| question | text | - | Market question text |
| category | text | - | Market category |
| entry_price | numeric | - | Price at order placement |
| entry_price_after_slippage | numeric | - | Actual fill price including slippage |
| quantity | numeric | - | Number of tokens held |
| cost_basis | numeric | - | Total cost (price × quantity) |
| estimated_edge | numeric | - | Expected edge at entry |
| entry_time | timestamp | now() | When position was opened |
| end_date | timestamp | - | Market end/expiry date |
| status | text | 'OPEN' | Position status: OPEN, CLOSED_TP, CLOSED_SL, CLOSED_RESOLVED |
| exit_price | numeric | NULL | Price at exit (if closed) |
| exit_time | timestamp | NULL | When position was closed |
| exit_reason | text | NULL | Why closed: Take Profit, Stop Loss, Resolution |
| realized_pnl | numeric | NULL | Actual profit/loss on close |
| realized_pnl_percent | numeric | NULL | PnL as percentage of cost |
| created_at | timestamp | now() | Record creation time |

**Indexes:** `idx_no_positions_market` (market_id), `idx_no_positions_status` (status)

### no_trades

Individual trade records (entries and exits) per strategy.

| Column | Type | Default | Description |
|--------|------|---------|-------------|
| id | text | - | **PK** - Unique trade ID |
| strategy_id | text | 'no-buyer' | Strategy that made this trade |
| position_id | text | - | Links to no_positions.id |
| market_id | text | - | Polymarket market identifier |
| question | text | - | Market question text |
| category | text | - | Market category |
| side | text | - | Trade direction: BUY or SELL |
| token_side | text | 'NO' | Token type: YES or NO |
| price | numeric | - | Order price |
| price_after_slippage | numeric | - | Actual fill price |
| quantity | numeric | - | Tokens traded |
| value | numeric | - | Trade value (price × quantity) |
| slippage_cost | numeric | - | Cost due to slippage |
| timestamp | timestamp | now() | Trade execution time |
| reason | text | - | Why trade occurred: Entry, Take Profit, Stop Loss, etc. |
| created_at | timestamp | now() | Record creation time |

**Indexes:** `idx_no_trades_position` (position_id), `idx_no_trades_timestamp` (timestamp)

### no_scanned_markets

Tracks markets that have been evaluated for trading (shared across strategies).

| Column | Type | Default | Description |
|--------|------|---------|-------------|
| market_id | text | - | **PK** - Polymarket market identifier |
| first_scanned_at | timestamp | now() | When first seen by scanner |
| eligible | boolean | - | Whether market passed basic filters |
| rejection_reason | text | NULL | Why market was rejected (if not eligible) |
| position_opened | boolean | false | Whether any position was opened |

### no_daily_snapshots

Daily performance tracking per strategy for equity curve and metrics.

| Column | Type | Default | Description |
|--------|------|---------|-------------|
| strategy_id | text | 'no-buyer' | **PK (composite)** - Strategy identifier |
| date | date | - | **PK (composite)** - Snapshot date |
| starting_equity | numeric | - | Portfolio value at day start |
| ending_equity | numeric | - | Portfolio value at day end |
| daily_pnl | numeric | - | Day's profit/loss |
| daily_pnl_percent | numeric | - | Day's PnL as percentage |
| trades_opened | integer | 0 | Positions opened that day |
| trades_closed | integer | 0 | Positions closed that day |
| winning_trades | integer | 0 | Winning trades closed |
| losing_trades | integer | 0 | Losing trades closed |

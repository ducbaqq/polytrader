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

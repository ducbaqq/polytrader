# Analyzer Module

## Purpose

Generates validation reports from collected data. Analyzes trading patterns by time of day and market category to determine if building a real trading bot is worthwhile.

---

## Entry Point

`src/analyzer/index.ts`

---

## How to Run

```bash
npm run report
```

---

## Key Files

| File | Purpose |
|------|---------|
| `src/analyzer/index.ts` | Re-exports all analyzers |
| `src/analyzer/reportGenerator.ts` | Main report generation |
| `src/analyzer/timeAnalyzer.ts` | Hourly pattern analysis |
| `src/analyzer/categoryAnalyzer.ts` | Performance by market category |

---

## Report Generation

### generateValidationReport(daysAnalyzed, outputDir)

Collects metrics from database and generates recommendation:

1. **Scan Count**: Total market scans performed
2. **Opportunity Stats**: Arbitrage, wide spread, thin book counts
3. **Trade Stats**: Total trades, fees, cash flow
4. **P&L History**: Daily profit/loss
5. **Best Hours**: Most profitable trading times
6. **Best/Worst Categories**: Performance by market type

---

## Metrics Collected

### From `opportunities` table:
- Total arbitrage opportunities
- Average duration
- Best theoretical profit

### From `paper_orders` table:
- Total orders placed
- Fill rate

### From `paper_trades` table:
- Platform fees
- Gas costs
- Slippage costs

### From `paper_pnl` table:
- Daily P&L
- Total return

---

## Recommendations

Based on collected data:

| Recommendation | Criteria |
|----------------|----------|
| **BUILD_BOT** | Positive P&L, high fill rate, consistent opportunities |
| **MARGINAL** | Breakeven or slightly positive, moderate fill rate |
| **DONT_BUILD** | Negative P&L, low fill rate, insufficient opportunities |

---

## Time Analysis

`getBestTradingHours(daysAnalyzed)`:
- Groups opportunities by hour of day
- Calculates success rate per hour
- Returns top performing hours

---

## Category Analysis

`getBestCategories(daysAnalyzed)` / `getWorstCategories(daysAnalyzed)`:
- Groups paper trades by market category
- Calculates P&L per category
- Identifies best and worst performers

---

## Output

Report saved to `./reports/` directory (JSON format).

Dashboard displays:
- Net Profit
- Win Rate
- Fill Rate
- Recommendation with reasoning

# Alpha Analysis Pipeline

## 1. Overview

Statistical analysis pipeline that determines if Polymarket's ~80% No resolution rate represents actual trading edge or is already priced in by the market. Fetches settled markets, retrieves price history from CLOB API, calculates implied vs actual probabilities, computes Wilson Score confidence intervals, and visualizes edge by category/liquidity/duration.

## 2. Files

### Core Files
| File | Purpose |
|------|---------|
| `src/exportAlphaAnalysis.ts` | Main CLI entry point with 3-phase processing |
| `src/alphaAnalysis/types.ts` | Type definitions (AlphaMarket, CalibrationBucket, etc.) |
| `src/alphaAnalysis/priceHistoryFetcher.ts` | CLOB API wrapper with batch fetching |
| `src/alphaAnalysis/edgeCalculator.ts` | Wilson Score CI calculations |
| `src/alphaAnalysis/aggregator.ts` | Grouping by calibration bucket, category, etc. |

### Shared Utilities (`src/utils/`)
| File | Purpose |
|------|---------|
| `concurrentRateLimiter.ts` | Parallel request execution with controlled concurrency |
| `rateLimiter.ts` | Sequential rate limiting, retry logic |
| `periodParser.ts` | Parse period strings (5d, 2m) to days |
| `categoryInference.ts` | Infer market categories from question text |

### Visualization Components
| File | Purpose |
|------|---------|
| `visualization/src/components/EdgeAnalysisView.tsx` | Combined edge analysis tab |
| `visualization/src/components/CalibrationChart.tsx` | Implied vs actual probability chart |
| `visualization/src/components/EdgeByCategoryChart.tsx` | Horizontal bar chart with 95% CIs |
| `visualization/src/components/EdgeScatterPlot.tsx` | Market scatter with category filter |

## 3. Architecture

### Data Flow
```
Gamma API (closed markets) → Filter binary markets → CLOB API (price history)
    ↓
Edge Calculator (implied vs actual) → Aggregator (by dimension)
    ↓
alpha_analysis.json + alpha_summary.json → React visualization
```

### Three-Phase Processing (in `processMarkets`)
The pipeline uses a 3-phase approach for optimal performance:

1. **Filter Phase** (sync): Validate markets, extract token IDs, determine tier
2. **Fetch Phase** (parallel): Batch fetch all price histories with `ConcurrentRateLimiter`
3. **Transform Phase** (sync): Build final `AlphaMarket` objects with edge calculations

### Parallel Batch Fetching
The `ConcurrentRateLimiter` class enables parallel API requests:

```typescript
// Usage in priceHistoryFetcher.ts
const concurrentLimiter = new ConcurrentRateLimiter({
  maxConcurrent: concurrency,  // e.g., 10-20 parallel requests
});

await concurrentLimiter.executeInChunks(tasks, 5000, onProgress);
```

**Performance improvement:** ~98 req/sec vs ~1.5 req/sec sequential (65x faster)

| Concurrency | Throughput | Time for 70k calls |
|-------------|------------|-------------------|
| 1 (old)     | ~1.5/sec   | ~13 hours |
| 10          | ~15-20/sec | ~1 hour |
| 15          | ~25-30/sec | ~40 minutes |

### Tiered Price History Fetching
- **Tier 1** (volume > $10k): Full daily history (`interval=1d, fidelity=30`)
- **Tier 2** ($1k-$10k): Key points only (`interval=max, fidelity=2`)
- **Tier 3** (< $1k): Use `outcomePrices` only, skip API call

## 4. External Dependencies

**APIs:**
- Gamma API: `https://gamma-api.polymarket.com/markets` (no auth, ~5/sec)
- CLOB API: `https://clob.polymarket.com/prices-history` (no auth, ~2/sec per client)

**No database required** - outputs to JSON files.

## 5. How to Run

```bash
# Run alpha analysis (5 days, default concurrency=10)
npm run alpha-analysis -- --period 5d

# Faster with higher concurrency (use 15-20 for large datasets)
npm run alpha-analysis -- --period 2m --concurrency 15

# Skip price history fetch (faster, less accurate)
npm run alpha-analysis -- --period 5d --no-price-history

# Custom output paths
npm run alpha-analysis -- --period 1m -o data/alpha.json -s data/summary.json

# View in visualization
cp alpha_summary.json alpha_analysis.json visualization/public/
cd visualization && npm run dev
# Then click "Edge Analysis" tab
```

**CLI Options:**
| Flag | Description | Default |
|------|-------------|---------|
| `-p, --period <period>` | Time period (5d, 2m, etc.) | Required |
| `-c, --concurrency <n>` | Parallel requests (1-50) | 10 |
| `-o, --output <path>` | Output file for market data | alpha_analysis.json |
| `-s, --summary <path>` | Output file for summary | alpha_summary.json |
| `--no-price-history` | Skip CLOB API calls | false |

## 6. Key Decisions

- **Wilson Score CI** chosen over normal approximation - more accurate for small samples
- **Parallel batch fetching** with `ConcurrentRateLimiter` - 65x faster than sequential
- **3-phase processing** separates filtering, fetching, and transformation for clarity
- **Tiered fetching** to manage CLOB API load for different volume levels
- **30-market minimum** for statistical significance
- **Edge = actualNoWinRate - avgImpliedNoProb** - positive means No was underpriced

## 7. Known Limitations

- CLOB price history may not exist for very old/low-volume markets
- No caching of price history - re-fetches on every run
- Duration stats require `createdAt` field which some markets lack
- Very high concurrency (>20) may trigger API rate limits

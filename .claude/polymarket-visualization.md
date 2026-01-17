# Polymarket Settled Markets Visualization

## Overview

React visualization app for Polymarket market analysis with two main views:

1. **Overview Tab**: Interactive bubble chart showing market categories, sized by count, with embedded pie charts for Yes/No resolution ratios
2. **Edge Analysis Tab**: Statistical edge analysis with calibration charts, category edge comparisons, and scatter plots

The app loads data from JSON files exported by the alpha analysis pipeline.

---

## Files Created/Modified

### Export Script
| File | Purpose |
|------|---------|
| `src/exportSettledMarkets.ts` | CLI script to fetch and export settled markets |
| `package.json` | Added `export-markets` npm script |

### Visualization App (`visualization/`)
| File | Purpose |
|------|---------|
| `package.json` | Dependencies: React 18, D3 v7, Vite 5 |
| `src/App.tsx` | Main component - tab navigation, data loading |
| `src/App.css` | Dark theme styles |
| `src/types.ts` | TypeScript interfaces for all data types |

### Overview Tab Components
| File | Purpose |
|------|---------|
| `src/components/SummaryHeader.tsx` | Total/Yes/No statistics |
| `src/components/BubbleChart.tsx` | D3 circle pack with embedded pie charts |
| `src/components/CategoryDetail.tsx` | Modal showing markets in category |

### Edge Analysis Tab Components
| File | Purpose |
|------|---------|
| `src/components/EdgeAnalysisView.tsx` | Combined view with all edge charts |
| `src/components/CalibrationChart.tsx` | Implied vs actual probability (diagonal = perfect calibration) |
| `src/components/EdgeByCategoryChart.tsx` | Horizontal bar chart with 95% confidence intervals |
| `src/components/EdgeScatterPlot.tsx` | Individual market scatter, filterable by category |

### Data Files (in `public/`)
| File | Source |
|------|--------|
| `settled_markets.json` | From `npm run export-markets` |
| `alpha_summary.json` | From `npm run alpha-analysis` |
| `alpha_analysis.json` | From `npm run alpha-analysis` (optional, for scatter plot) |

---

## Data Pipeline

### API Endpoint
```
GET https://gamma-api.polymarket.com/markets
```

### Query Parameters
- `closed=true` - Only fetch closed/settled markets
- `limit=500` - Max items per request
- `offset=N` - Pagination offset
- `order=endDate` - Sort by end date
- `ascending=false` - Most recent first

### Category Detection
The API doesn't reliably provide categories, so we:
1. Check `market.category` field (older markets)
2. Check `market.events[].category` field
3. Infer from question keywords (Finance, Crypto, Sports, Politics, Tech, Entertainment, Weather)
4. Default to "Uncategorized"

### Output File: `settled_markets.json`
```json
{
  "exportDate": "2026-01-16",
  "monthsBack": 2,
  "totalMarkets": 41439,
  "markets": [
    {
      "id": "1146621",
      "question": "Will Tesla (TSLA) finish week of January 12 above $455?",
      "tags": ["Finance"],
      "resolution": "Yes" | "No",
      "resolvedAt": "2026-01-16"
    }
  ]
}
```

### Filtering
- Only binary markets (outcomes = `["Yes", "No"]`)
- Only markets with clear resolution (price >= 0.99 for winning outcome)

---

## Visualization Architecture

### Libraries
- **D3.js v7** - Circle pack layout, pie charts, bar charts, scatter plots
- **React 18** - UI components, state management
- **Vite 5** - Build tool

### Component Structure
```
App.tsx (tab navigation, data loading)
├── Overview Tab
│   ├── SummaryHeader.tsx     (stats bar)
│   ├── BubbleChart.tsx       (D3 circle pack)
│   └── CategoryDetail.tsx    (modal overlay)
│
└── Edge Analysis Tab
    └── EdgeAnalysisView.tsx  (container)
        ├── CalibrationChart.tsx      (implied vs actual probability)
        ├── EdgeByCategoryChart.tsx   (bar chart with 95% CIs)
        └── EdgeScatterPlot.tsx       (individual markets)
```

### BubbleChart Implementation
1. **Layout**: `d3.pack()` creates circle packing based on market count
2. **Bubbles**: Each category is a `<g>` group with:
   - Outer circle (dark background, border)
   - Donut pie chart (`d3.pie()` + `d3.arc()`)
   - Category name label
   - Market count label
3. **Sizing**: Bubble radius = sqrt(market count) via pack layout
4. **Colors**: Yes = `#22c55e` (green), No = `#ef4444` (red)

### Click Interaction
1. Click bubble → `onCategoryClick(categoryData)`
2. App sets `selectedCategory` state
3. `CategoryDetail` modal renders with:
   - Category name and stats
   - Scrollable list of markets (sorted by date desc)
   - Resolution badges (color-coded)
4. Close via: X button, click outside, or Escape key

---

## How to Run

### Export Data
```bash
# Export last 5 days of settled markets (for Overview tab)
npm run export-markets -- --period 5d

# Run alpha analysis (for Edge Analysis tab)
npm run alpha-analysis -- --period 5d --concurrency 15

# Copy all data to visualization
cp settled_markets.json alpha_summary.json alpha_analysis.json visualization/public/
```

### Run Visualization
```bash
cd visualization
npm install
npm run dev

# Opens at http://localhost:5173
# - Overview tab: Bubble chart of categories
# - Edge Analysis tab: Statistical edge charts (requires alpha_summary.json)
```

---

## Known Limitations / TODOs

1. **Category accuracy**: ~34% of markets fall into "Uncategorized" because keyword inference is imperfect. Could improve with:
   - More keyword patterns
   - ML-based classification
   - Manual category mapping for common market types

2. **Performance**: With 40k+ markets, the modal can be slow to render. Consider:
   - Virtualized list (react-window)
   - Pagination in detail view

3. **Data freshness**: Export must be re-run manually. Could add:
   - Scheduled job
   - Incremental updates

4. **Multi-tag handling**: Markets appear in multiple categories if they match multiple keywords. This inflates category totals vs unique market count.

5. **Missing features**:
   - Date range filter in UI
   - Search/filter within category detail
   - Export detail view to CSV

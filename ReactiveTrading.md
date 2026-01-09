# Crypto Reactive Trading System

A real-time trading system that monitors Binance crypto prices and trades Polymarket crypto threshold markets when mispricings occur.

---

## Overview

The system detects opportunities when Polymarket prices lag behind real crypto price movements. For example:

- BTC crosses above $100,000 on Binance
- Polymarket "BTC above $100K" YES token is still at $0.72 (should be ~$0.95)
- System buys YES tokens, profits when market catches up

---

## Architecture

```
Binance WebSocket (BTC/ETH/SOL)
        │
        ▼
┌─────────────────┐
│ BinanceWSClient │ ──► Price updates every ~1 second
│ - Track 1m/5m   │     Emits 'price' and 'significantMove' events
└─────────────────┘
        │
        ▼
┌─────────────────┐     ┌──────────────────┐
│ MispricingDetect│ ◄── │ CryptoMarkets    │
│ - Expected price│     │ (from discovery) │
│ - Compare actual│     └──────────────────┘
└─────────────────┘
        │
        │ if gap > 20%
        ▼
┌─────────────────┐
│ RiskManager     │
│ - Exposure check│
│ - Cooldown check│
│ - Daily limits  │
└─────────────────┘
        │
        │ if allowed
        ▼
┌─────────────────┐
│ CryptoTrader    │ ──► Opens position (paper trading)
└─────────────────┘
        │
        ▼
┌─────────────────┐
│ ExitMonitor     │ ──► Checks every 1 second
│ - Profit +15%   │     Closes on first trigger
│ - Stop -5%      │
│ - Time 2min     │
│ - Reversal      │
└─────────────────┘
```

---

## File Structure

```
src/crypto/
├── cryptoTypes.ts       # Type definitions
├── cryptoConfig.ts      # Configuration constants
├── binanceWS.ts         # Binance WebSocket client
├── marketDiscovery.ts   # Find & parse crypto threshold markets
├── mispricingDetector.ts# Calculate expected vs actual prices
├── cryptoTrader.ts      # Order execution & position management
├── exitMonitor.ts       # Exit strategy monitoring
├── riskManager.ts       # Risk controls & limits
└── index.ts             # CryptoReactiveTrader orchestrator

src/database/
└── cryptoRepo.ts        # Database operations for crypto tables

src/cryptoValidator.ts   # Entry point with dashboard
```

---

## Components

### 1. Binance WebSocket Client (`binanceWS.ts`)

Connects to Binance's real-time price stream for BTC, ETH, and SOL.

**Features:**
- Subscribes to `btcusdt@ticker`, `ethusdt@ticker`, `solusdt@ticker`
- Tracks 1-minute and 5-minute price changes in memory
- Emits `'price'` event on every update (~1/second)
- Emits `'significantMove'` when price moves >1% in 1 minute
- Auto-reconnects on disconnect with exponential backoff

**Usage:**
```typescript
const ws = getBinanceWSClient();
await ws.connect();

ws.on('price', (price: CryptoPrice) => {
  console.log(`${price.asset}: $${price.price} (1m: ${price.change1m}%)`);
});

ws.on('significantMove', (event) => {
  console.log(`${event.asset} moved ${event.changePercent}%!`);
});
```

---

### 2. Market Discovery (`marketDiscovery.ts`)

Finds Polymarket markets with crypto price thresholds.

**Threshold Extraction:**
Parses questions like:
- "Will BTC be above $100,000?" → BTC, ABOVE, $100,000
- "Will Bitcoin reach $100K?" → BTC, ABOVE, $100,000
- "ETH below $4,000 by March?" → ETH, BELOW, $4,000

**Patterns matched:**
```typescript
/\$(\d{1,3}(?:,\d{3})*)/     // $100,000
/\$(\d+(?:\.\d+)?)[kK]/      // $100K, $100k
/(\d{1,3}(?:,\d{3})+)/       // 100,000 (without $)
```

**Exclusion Filters:**
Markets with these keywords are excluded:
- tweet, hack, SEC, Elon, Trump, Musk, ban, regulate, ETF, halving

**Whitelisted Markets:**
Only these threshold ranges are traded:
- BTC: $90,000 - $150,000
- ETH: $3,000 - $6,000
- SOL: $150 - $400

---

### 3. Mispricing Detector (`mispricingDetector.ts`)

Calculates what Polymarket price SHOULD be based on Binance price.

**Expected Price Algorithm:**

For "ABOVE" direction:
```typescript
function calculateExpectedYesPrice(binancePrice, threshold, direction) {
  const distance = (binancePrice - threshold) / threshold;

  if (direction === 'ABOVE') {
    if (binancePrice > threshold) {
      // Price is above threshold: YES should be high
      // Base 0.85, add up to 0.13 based on distance
      return Math.min(0.98, 0.85 + Math.min(0.13, distance * 2));
    } else {
      // Price is below threshold: YES should be low
      // Base 0.50, subtract based on distance
      return Math.max(0.05, 0.50 - Math.min(0.45, Math.abs(distance) * 4));
    }
  }
  // Mirror logic for 'BELOW'
}
```

**Example Expected Prices:**

| BTC Price | Threshold | Direction | Expected YES |
|-----------|-----------|-----------|--------------|
| $95,000   | $100,000  | ABOVE     | $0.30        |
| $98,000   | $100,000  | ABOVE     | $0.42        |
| $100,000  | $100,000  | ABOVE     | $0.50        |
| $102,000  | $100,000  | ABOVE     | $0.89        |
| $105,000  | $100,000  | ABOVE     | $0.95        |

**Mispricing Detection:**

An opportunity is detected when:
1. Gap between expected and actual price > 20%
2. The token is UNDERPRICED (we can buy cheap)

```typescript
const gap = (expectedPrice - actualPrice) / expectedPrice;
if (gap >= 0.20) {
  // Opportunity: buy this token
}
```

---

### 4. Risk Manager (`riskManager.ts`)

Controls exposure and prevents overtrading.

**Limits:**
| Parameter | Default | Description |
|-----------|---------|-------------|
| maxTotalExposure | $1,500 | Maximum $ at risk |
| maxPositions | 3 | Maximum simultaneous positions |
| dailyLossLimit | $100 | Stop trading if daily loss exceeds |
| maxDailyTrades | 20 | Maximum trades per day |
| cooldownMinutes | 5 | Wait time after trading a market |

**Risk Checks:**
```typescript
const result = await riskManager.canTrade(opportunity, size);
// { allowed: false, reason: "Max exposure reached" }
// { allowed: false, reason: "Market on cooldown" }
// { allowed: true }
```

---

### 5. Crypto Trader (`cryptoTrader.ts`)

Executes trades and manages positions.

**Position Sizing:**

Base size: $200, scaled by:
- Gap percentage (larger gap = more confidence)
- Market volume (higher volume = more confidence)

| Gap % | Multiplier |
|-------|------------|
| > 40% | 1.5x       |
| > 30% | 1.3x       |
| > 20% | 1.0x       |

| Volume | Multiplier |
|--------|------------|
| > $200K | 1.3x      |
| > $100K | 1.2x      |
| > $50K  | 1.0x      |

Maximum position: $500

**Trade Execution:**
```typescript
const result = await trader.executeTrade(opportunity, marketVolume);
// { success: true, position: { positionId, entryPrice, quantity, ... } }
// { success: false, error: "Max exposure reached" }
```

---

### 6. Exit Monitor (`exitMonitor.ts`)

Monitors positions every second and closes on exit conditions.

**Exit Conditions (first to trigger wins):**

| Condition | Trigger | Action |
|-----------|---------|--------|
| PROFIT | P&L >= +15% | Close for profit |
| STOP | P&L <= -5% | Close to limit loss |
| TIME | Hold time >= 2 minutes | Close to avoid decay |
| REVERSAL | Binance price crosses back threshold | Close to avoid loss |

**Example:**
- Entry: YES @ $0.72 when BTC = $102,000
- BTC drops to $99,000 (below $100K threshold)
- REVERSAL triggered: close position immediately

---

### 7. Orchestrator (`index.ts`)

The `CryptoReactiveTrader` class coordinates all components.

**Startup Sequence:**
1. Discover crypto threshold markets from Polymarket
2. Connect to Binance WebSocket
3. Setup event handlers for price updates
4. Start exit monitor (checks every 1 second)
5. Start market discovery refresh (every 5 minutes)

**Event Flow:**
```
BinanceWS 'price' event
    │
    ▼
checkOpportunities()
    │
    ├── Get markets for this asset
    ├── Fetch current Polymarket prices
    ├── detectMispricing()
    │       │
    │       ▼ (if opportunity)
    └── trader.executeTrade()
            │
            ▼ (if success)
        exitMonitor tracks position
```

---

## Database Schema

### crypto_price_log
Stores significant price movements for analysis.
```sql
CREATE TABLE crypto_price_log (
  id SERIAL PRIMARY KEY,
  timestamp TIMESTAMPTZ DEFAULT NOW(),
  asset VARCHAR(10) NOT NULL,
  price NUMERIC(20, 8) NOT NULL,
  change_1m NUMERIC(10, 6),
  change_5m NUMERIC(10, 6),
  is_significant_move BOOLEAN DEFAULT FALSE
);
```

### crypto_markets
Discovered crypto threshold markets.
```sql
CREATE TABLE crypto_markets (
  id SERIAL PRIMARY KEY,
  market_id VARCHAR(100) UNIQUE NOT NULL,
  question TEXT,
  asset VARCHAR(10) NOT NULL,
  threshold NUMERIC(20, 2) NOT NULL,
  direction VARCHAR(10) NOT NULL,  -- 'ABOVE' or 'BELOW'
  resolution_date TIMESTAMPTZ,
  volume_24h NUMERIC(20, 2),
  is_whitelisted BOOLEAN DEFAULT FALSE,
  status VARCHAR(20) DEFAULT 'ACTIVE'
);
```

### crypto_opportunities
Detected trading opportunities.
```sql
CREATE TABLE crypto_opportunities (
  id SERIAL PRIMARY KEY,
  opportunity_id VARCHAR(100) UNIQUE NOT NULL,
  market_id VARCHAR(100) NOT NULL,
  detected_at TIMESTAMPTZ DEFAULT NOW(),
  asset VARCHAR(10) NOT NULL,
  threshold NUMERIC(20, 2),
  binance_price NUMERIC(20, 8) NOT NULL,
  expected_poly_price NUMERIC(10, 4),
  actual_poly_price NUMERIC(10, 4),
  gap_percent NUMERIC(10, 4),
  side VARCHAR(5),  -- 'YES' or 'NO'
  executed BOOLEAN DEFAULT FALSE,
  status VARCHAR(20) DEFAULT 'DETECTED'
);
```

### crypto_positions
Open and closed positions.
```sql
CREATE TABLE crypto_positions (
  id SERIAL PRIMARY KEY,
  position_id VARCHAR(100) UNIQUE NOT NULL,
  market_id VARCHAR(100) NOT NULL,
  asset VARCHAR(10) NOT NULL,
  side VARCHAR(5) NOT NULL,
  entry_price NUMERIC(10, 4) NOT NULL,
  quantity NUMERIC(20, 2) NOT NULL,
  entry_time TIMESTAMPTZ DEFAULT NOW(),
  binance_price_at_entry NUMERIC(20, 8),
  exit_price NUMERIC(10, 4),
  exit_time TIMESTAMPTZ,
  exit_reason VARCHAR(20),  -- 'PROFIT', 'STOP', 'TIME', 'REVERSAL'
  pnl NUMERIC(20, 2),
  status VARCHAR(20) DEFAULT 'OPEN'
);
```

---

## Configuration

All defaults in `cryptoConfig.ts`:

```typescript
const DEFAULT_CONFIG = {
  // Position sizing
  basePositionSize: 200,      // $200 per trade
  maxPositionSize: 500,       // $500 max per trade
  maxTotalExposure: 1500,     // $1500 max total
  maxSimultaneousPositions: 3,

  // Exit strategy
  maxHoldTimeSeconds: 120,    // 2 minutes
  profitTargetPct: 0.15,      // 15%
  stopLossPct: 0.05,          // 5%

  // Risk controls
  cooldownMinutes: 5,
  dailyLossLimit: 100,        // $100
  maxDailyTrades: 20,

  // Mispricing thresholds
  minGapPercent: 0.20,        // 20% gap required
  minVolume: 50000,           // $50K volume
  minResolutionHours: 24,

  // Market discovery
  discoveryIntervalMinutes: 5,
};
```

---

## Usage

### Run Tests
```bash
npm run crypto-test
```

Tests:
- Threshold extraction from various question formats
- Expected price calculations
- Mispricing detection scenarios

### Start Trader
```bash
npm run crypto
```

Shows live dashboard with:
- Binance prices (BTC, ETH, SOL)
- Tracked markets with expected prices
- Active positions with P&L
- Risk status (exposure, daily P&L, trades)
- Recent opportunities

---

## Dashboard

```
╔══════════════════════════════════════════════════════════════════════════════╗
║                    CRYPTO REACTIVE TRADER                                    ║
║ Status: ● CONNECTED    Last Update: 10:45:32 AM                             ║
╠══════════════════════════════════════════════════════════════════════════════╣
║ BINANCE PRICES                                                               ║
║ ├─ BTC: $102,345.67    1m: +0.3%    5m: +1.2%                               ║
║ ├─ ETH: $3,456.78      1m: +0.1%    5m: +0.8%                               ║
║ └─ SOL: $234.56        1m: -0.2%    5m: +0.5%                               ║
╠══════════════════════════════════════════════════════════════════════════════╣
║ TRACKED MARKETS: 12                                                          ║
║ ✓ BTC >$100K (Vol: $156K) Expected: $0.95                                   ║
║   ETH >$4K (Vol: $89K) Expected: $0.35                                      ║
╠══════════════════════════════════════════════════════════════════════════════╣
║ ACTIVE POSITIONS: 1 / 3                                                      ║
║ ├─ BTC YES @ $0.72  Size: $300  P&L: +4.0%  Hold: 45s                       ║
╠══════════════════════════════════════════════════════════════════════════════╣
║ RISK STATUS                                                                  ║
║ ├─ Exposure: $300 / $1500  [████░░░░░░░░░░░░░░░░]                           ║
║ ├─ Daily P&L: +$45.00                                                        ║
║ └─ Trades: 5 / 20    Cooldowns: 2                                           ║
╠══════════════════════════════════════════════════════════════════════════════╣
║ RECENT OPPORTUNITIES (last 10)                                               ║
║ ✓ 10:45:32  BTC YES Gap: 23%  EXECUTED                                      ║
║ ○ 10:42:15  ETH NO Gap: 8%   SKIPPED                                        ║
╚══════════════════════════════════════════════════════════════════════════════╝
```

---

## Trading Logic Example

**Scenario: BTC crosses $100K**

1. **10:45:30** - BTC at $99,500 on Binance
   - Expected YES price: $0.48
   - Actual Polymarket YES: $0.50
   - Gap: 4% - no opportunity

2. **10:45:35** - BTC jumps to $101,000 on Binance
   - Expected YES price: $0.87
   - Actual Polymarket YES: $0.55 (hasn't caught up)
   - Gap: 37% - **OPPORTUNITY DETECTED**

3. **10:45:36** - Risk check passes
   - Exposure: $0 < $1,500 ✓
   - Positions: 0 < 3 ✓
   - Market not on cooldown ✓

4. **10:45:37** - Trade executed
   - Buy YES @ $0.55
   - Position size: $300 (base $200 × 1.5 for 37% gap)
   - Quantity: 545 tokens

5. **10:46:15** - Exit monitor check
   - Current YES price: $0.68
   - P&L: +23.6%
   - **PROFIT TARGET HIT** (+15%)

6. **10:46:16** - Position closed
   - Sell YES @ $0.68
   - P&L: +$71 (23.6%)
   - Cooldown started for this market

---

## Risk Management Details

### Why These Limits?

| Limit | Reasoning |
|-------|-----------|
| $1,500 max exposure | Limits total capital at risk |
| 3 positions max | Prevents over-diversification |
| $100 daily loss limit | Stops trading on bad days |
| 20 trades/day max | Prevents overtrading |
| 5-min cooldown | Avoids chasing same market |
| 2-min max hold | Crypto prices move fast |

### Exit Priority

1. **REVERSAL** - Most important, price thesis invalidated
2. **STOP** - Limit losses early
3. **PROFIT** - Take gains
4. **TIME** - Avoid holding too long

---

## Future Improvements

1. **Real Trading** - Currently paper trading only
2. **More Assets** - Add DOGE, AVAX, etc.
3. **Volatility Adjustment** - Scale position size by volatility
4. **Order Book Analysis** - Check liquidity before trading
5. **Machine Learning** - Predict optimal entry/exit points

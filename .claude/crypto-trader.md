# Crypto Reactive Trader

## Purpose

Monitors Binance crypto prices (BTC, ETH, SOL) in real-time and trades Polymarket crypto threshold markets when mispricings occur. For example, if BTC crosses $100K on Binance but the Polymarket "BTC above $100K" market hasn't caught up, it buys the underpriced YES tokens.

---

## Entry Point

`src/cryptoValidator.ts`

---

## How to Run

```bash
# Start with dashboard
npm run crypto

# Run tests only
npm run crypto -- test
```

---

## Key Files

| File | Purpose |
|------|---------|
| `src/cryptoValidator.ts` | Entry point with terminal dashboard |
| `src/crypto/index.ts` | `CryptoReactiveTrader` orchestrator |
| `src/crypto/binanceWS.ts` | Binance WebSocket client |
| `src/crypto/marketDiscovery.ts` | Find crypto threshold markets on Polymarket |
| `src/crypto/mispricingDetector.ts` | Calculate expected vs actual prices |
| `src/crypto/cryptoTrader.ts` | Position management |
| `src/crypto/exitMonitor.ts` | Exit strategy (profit/stop/time/reversal) |
| `src/crypto/riskManager.ts` | Exposure and cooldown controls |
| `src/crypto/cryptoConfig.ts` | Configuration constants |
| `src/crypto/cryptoTypes.ts` | Type definitions |

---

## Architecture

```
Binance WebSocket (BTC/ETH/SOL)
        │
        ▼
┌─────────────────┐
│ BinanceWSClient │ ──► Price updates ~1/second
└─────────────────┘
        │
        ▼
┌─────────────────┐     ┌──────────────────┐
│MispricingDetect │ ◄── │ Market Discovery │
│ - Expected price│     │ (Polymarket)     │
└─────────────────┘     └──────────────────┘
        │
        │ if gap > 20%
        ▼
┌─────────────────┐
│ RiskManager     │
│ - Max exposure  │
│ - Cooldown      │
│ - Daily limits  │
└─────────────────┘
        │
        ▼
┌─────────────────┐
│ CryptoTrader    │ ──► Opens position
└─────────────────┘
        │
        ▼
┌─────────────────┐
│ ExitMonitor     │ ──► Closes on:
│ - Profit +15%   │     - Profit target
│ - Stop -5%      │     - Stop loss
│ - Time 2min     │     - Time limit
│ - Reversal      │     - Price reversal
└─────────────────┘
```

---

## Components

### BinanceWSClient
- Connects to Binance ticker streams
- Tracks 1-minute and 5-minute price changes
- Emits `'price'` and `'significantMove'` events
- Auto-reconnects with exponential backoff

### Market Discovery
- Searches Polymarket for crypto threshold markets
- Parses market questions like "Will BTC be above $100,000 by Jan 31?"
- Extracts: asset (BTC/ETH/SOL), threshold, direction (ABOVE/BELOW), expiry

### Mispricing Detection
- Calculates expected YES price based on current crypto price vs threshold
- Uses distance-to-threshold for probability estimation
- Triggers opportunity when gap > 20%

### Exit Monitor
- Checks positions every 1 second
- Exit triggers: profit target (+15%), stop loss (-5%), time limit (2 min), price reversal

---

## Dependencies

- **External**: Binance WebSocket, Polymarket REST API
- **Internal**: `database/cryptoRepo.ts`, `apiClient.ts`
- **Database tables**: `crypto_markets`, `crypto_positions`, `crypto_opportunities`, `crypto_price_log`

---

## Configuration

Key settings in `src/crypto/cryptoConfig.ts`:
- `mispricingThreshold`: 0.20 (20% gap to trigger)
- `profitTarget`: 0.15 (15% profit to exit)
- `stopLoss`: 0.05 (5% loss to exit)
- `maxHoldTime`: 120 seconds
- `maxExposure`: $1000
- `cooldownMinutes`: 5

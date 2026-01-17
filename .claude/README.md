# Polymarket Trading Bot - Codebase Documentation

## Project Overview

A comprehensive trading bot system for Polymarket that includes:
- Real-time market scanning via WebSocket
- Paper trading engine for strategy validation
- Crypto-reactive trading (Binance → Polymarket)
- Price-triggered sell orders
- Settled markets visualization

---

## Tech Stack

| Category | Technology |
|----------|------------|
| Language | TypeScript |
| Runtime | Node.js 18+ |
| Database | PostgreSQL |
| WebSocket | Polymarket CLOB WS, Binance WS |
| API Client | @polymarket/clob-client |
| CLI | Commander.js |
| Visualization | React 18 + D3.js + Vite |

---

## Directory Structure

```
polymarket/
├── src/
│   ├── index.ts              # Main CLI entry point
│   ├── wsValidator.ts        # WebSocket-based trading validator
│   ├── wsScanner.ts          # Real-time market scanner
│   ├── cryptoValidator.ts    # Crypto reactive trader entry
│   ├── priceTriggerWorker.ts # Price-triggered sell worker
│   ├── clobClient.ts         # Authenticated CLOB client
│   ├── apiClient.ts          # Polymarket REST API wrapper
│   ├── deriveApiKey.ts       # API key derivation utility
│   ├── exportSettledMarkets.ts # Export settled markets
│   ├── types.ts              # TypeScript interfaces
│   ├── crypto/               # Crypto trading module
│   ├── database/             # PostgreSQL repositories
│   ├── paperTrader/          # Paper trading engine
│   └── analyzer/             # Report generation
├── visualization/            # React app for market visualization
├── docs/                     # Architecture documentation
├── data/                     # JSON snapshots
└── logs/                     # Error logs
```

---

## Applications & Scripts

### Main Applications

| App | Command | Purpose | Documentation |
|-----|---------|---------|---------------|
| **WebSocket Validator** | `npm run validate` | Real-time market scanning, arbitrage detection, paper trading | [wsValidator.md](./wsValidator.md) |
| **Crypto Reactive Trader** | `npm run crypto` | Monitor Binance, trade Polymarket crypto markets | [crypto-trader.md](./crypto-trader.md) |
| **Price Trigger Worker** | `npm run sell-trigger` | Auto-sell when market price reaches target | [price-trigger.md](./price-trigger.md) |
| **Visualization** | `cd visualization && npm run dev` | Interactive bubble chart of settled markets | [polymarket-visualization.md](./polymarket-visualization.md) |

### Utility Scripts

| Script | Command | Purpose |
|--------|---------|---------|
| `report` | `npm run report` | Generate validation report from DB data |
| `reset` | `npm run reset` | Clear all database tables |
| `db-status` | `npm run db-status` | Show table row counts |
| `derive-key` | `npm run derive-key` | Derive API keys from private key |
| `export-markets` | `npm run export-markets` | Export settled markets to JSON |

---

## Core Modules

| Module | Location | Purpose | Documentation |
|--------|----------|---------|---------------|
| Paper Trader | `src/paperTrader/` | Simulated order execution & portfolio tracking | [paper-trader.md](./paper-trader.md) |
| Database | `src/database/` | PostgreSQL connection pool & repositories | [database.md](./database.md) |
| Analyzer | `src/analyzer/` | Time/category analysis, report generation | [analyzer.md](./analyzer.md) |
| CLOB Client | `src/clobClient.ts` | Authenticated order execution | [clob-client.md](./clob-client.md) |
| API Client | `src/apiClient.ts` | REST API wrapper with rate limiting | Part of wsValidator |

---

## Environment Variables

Required in `.env`:

```bash
# Database (required for all apps)
DATABASE_URL=postgresql://user:pass@host:port/db

# Polymarket credentials (required for live trading)
POLYMARKET_PRIVATE_KEY=your_private_key
POLYMARKET_API_KEY=your_api_key
POLYMARKET_API_SECRET=your_api_secret
POLYMARKET_API_PASSPHRASE=your_passphrase

# Optional configuration
SCAN_INTERVAL_SECONDS=30
DASHBOARD_UPDATE_SECONDS=60
MIN_VOLUME=1000
ARBITRAGE_THRESHOLD=0.995
```

---

## Quick Start

```bash
# Install dependencies
npm install

# Setup database tables (run manually or via migration)
# See docs/ARCHITECTURE.md for table structure

# Run the main WebSocket validator
npm run validate

# Or run crypto reactive trader
npm run crypto

# Check status
npm run db-status
```

---

## Related Documentation

- [ARCHITECTURE.md](../docs/ARCHITECTURE.md) - System architecture and data flow
- [ReactiveTrading.md](../ReactiveTrading.md) - Crypto trading system details
- [CLAUDE.md](../CLAUDE.md) - AI assistant guidelines

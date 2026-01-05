# Polymarket Market Discovery Bot (TypeScript)

A comprehensive TypeScript bot for discovering trading opportunities on Polymarket. Continuously scans all active markets, detects arbitrage and market-making opportunities, and displays real-time data in a terminal dashboard.

## Features

- **Continuous Market Scanning**: Fetches all active markets every 30 seconds
- **Opportunity Detection**:
  - Arbitrage (YES + NO < 0.995)
  - Wide spreads (> 5% for market making)
  - Mispricing between correlated markets
  - Volume spikes (3x+ above average)
  - Thin order books (high volume, few makers)
- **Real-time Dashboard**: Terminal-based display updating every 60 seconds
- **Data Persistence**: Hourly JSON snapshots with gzip compression
- **Graceful Shutdown**: Ctrl+C saves final snapshot before exit

## Project Structure

```
polymarket/
├── src/
│   ├── index.ts        # Main entry point and orchestrator
│   ├── types.ts        # TypeScript interfaces and types
│   ├── apiClient.ts    # Polymarket API wrapper with rate limiting
│   ├── scanner.ts      # Continuous market scanner
│   ├── detector.ts     # Opportunity detection algorithms
│   ├── storage.ts      # JSON data persistence
│   └── dashboard.ts    # Terminal dashboard (chalk + cli-table3)
├── data/
│   ├── snapshots/      # Hourly market snapshots
│   └── opportunities/  # Detected opportunities
├── logs/
│   └── errors/         # Error logs
├── package.json
├── tsconfig.json
├── .env.example
└── README.md
```

## Installation

### 1. Prerequisites

- Node.js 18 or higher
- npm or yarn

### 2. Install Dependencies

```bash
npm install
```

### 3. Configure Credentials

Copy the example environment file:

```bash
cp .env.example .env
```

Edit `.env` with your credentials:

```env
# Required for read-only operations (optional - can work without)
POLYMARKET_PRIVATE_KEY=your_private_key_here

# Optional - only needed for authenticated endpoints
POLYMARKET_API_KEY=your_api_key
POLYMARKET_API_SECRET=your_api_secret
POLYMARKET_API_PASSPHRASE=your_passphrase
```

**Note**: The bot works in read-only mode without credentials.

## Running the Bot

### Development Mode

```bash
# Full bot with dashboard
npm run dev

# Single scan
npm run scan-once
```

### Production Mode

```bash
# Build first
npm run build

# Run
npm start
```

## Configuration

All settings can be configured via environment variables in `.env`:

```env
# Scanning intervals
SCAN_INTERVAL_SECONDS=30        # How often to scan markets
DASHBOARD_UPDATE_SECONDS=60     # Dashboard refresh rate
DATA_SAVE_INTERVAL_MINUTES=60   # How often to save data

# Market filtering
MIN_VOLUME=1000                 # Minimum 24h volume to include
MAX_MARKETS=                    # Leave empty for no limit

# Opportunity detection thresholds
ARBITRAGE_THRESHOLD=0.995       # YES+NO sum below this = opportunity
WIDE_SPREAD_THRESHOLD=0.05      # Spread above 5% = opportunity
VOLUME_SPIKE_MULTIPLIER=3.0     # 3x volume = spike detected
THIN_BOOK_MAKER_COUNT=5         # Fewer than 5 makers = thin book

# Storage
DATA_DIR=data                   # Where to save data
LOGS_DIR=logs                   # Where to save logs
COMPRESS_DATA=true              # Gzip compress data files
```

## Interpreting Output

### Dashboard Sections

1. **Volume Distribution**: Markets by 24h volume tier
2. **Spread Distribution**: How tight/wide spreads are
3. **Opportunities Table**: Count and average value by type
4. **Top Liquid Markets**: Highest volume markets with spreads
5. **Newest Markets**: Recently created markets (< 24h old)
6. **Alerts**: Real-time notifications for significant opportunities

### Opportunity Types

- **Arbitrage**: YES + NO < 0.995 (guaranteed profit)
- **Wide Spread**: > 5% spread (market making opportunity)
- **Volume Spike**: 3x+ above 1h average (potential news)
- **Thin Book**: High volume but few makers (liquidity opportunity)
- **Mispricing**: Related markets inconsistently priced

## Stopping the Bot

Press `Ctrl+C` to stop gracefully. The bot will:
1. Save the final market snapshot
2. Save all recent opportunities
3. Print final statistics
4. Clean up old files (> 30 days)

## License

MIT License - Use at your own risk.

## Disclaimer

This bot is for informational purposes only. Trading on prediction markets involves risk.

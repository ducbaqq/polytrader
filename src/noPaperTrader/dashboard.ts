/**
 * Terminal dashboard for the No Paper Trader.
 * Renders a live-updating table with portfolio and position info.
 */

import { Position, Portfolio } from './types';

export interface DashboardState {
  status: 'idle' | 'scanning' | 'monitoring' | 'starting';
  scanProgress?: { current: number; total: number };
  portfolio: Portfolio | null;
  positions: PositionWithPrice[];
  lastUpdate: Date;
  runtime: number; // ms
  totalScans: number;
  positionsOpened: number;
  positionsClosed: number;
}

export interface PositionWithPrice extends Position {
  currentPrice?: number;
  unrealizedPnl?: number;
  unrealizedPnlPercent?: number;
}

/**
 * Format duration in human readable format.
 */
function formatDuration(ms: number): string {
  const seconds = Math.floor(ms / 1000);
  const minutes = Math.floor(seconds / 60);
  const hours = Math.floor(minutes / 60);

  if (hours > 0) return `${hours}h ${minutes % 60}m`;
  if (minutes > 0) return `${minutes}m ${seconds % 60}s`;
  return `${seconds}s`;
}

/**
 * Truncate string to max length with ellipsis.
 */
function truncate(str: string, maxLen: number): string {
  if (str.length <= maxLen) return str;
  return str.substring(0, maxLen - 3) + '...';
}

/**
 * Pad string to fixed width.
 */
function pad(str: string, width: number, align: 'left' | 'right' = 'left'): string {
  if (str.length >= width) return str.substring(0, width);
  const padding = ' '.repeat(width - str.length);
  return align === 'left' ? str + padding : padding + str;
}

/**
 * Format currency value.
 */
function formatCurrency(value: number): string {
  const sign = value >= 0 ? '' : '-';
  return `${sign}$${Math.abs(value).toFixed(2)}`;
}

/**
 * Format percentage value.
 */
function formatPercent(value: number): string {
  return `${(value * 100).toFixed(1)}%`;
}

/**
 * Format P&L with color indicator.
 */
function formatPnl(value: number): string {
  const sign = value >= 0 ? '+' : '';
  return `${sign}$${value.toFixed(2)}`;
}

const BOX_WIDTH = 78;

/**
 * Render the dashboard to the terminal.
 */
export function renderDashboard(state: DashboardState): void {
  // Clear screen and move cursor to top
  process.stdout.write('\x1B[2J\x1B[0f');

  const lines: string[] = [];

  // Header
  lines.push('╔' + '═'.repeat(BOX_WIDTH) + '╗');
  lines.push('║' + pad('  NO PAPER TRADER', BOX_WIDTH) + '║');
  lines.push('╠' + '═'.repeat(BOX_WIDTH) + '╣');

  // Status row
  const statusText = getStatusText(state);
  const runtimeText = `Runtime: ${formatDuration(state.runtime)}`;
  const timeText = state.lastUpdate.toLocaleTimeString();
  lines.push('║  ' + pad(statusText, 40) + pad(`${runtimeText}  ${timeText}`, BOX_WIDTH - 42) + '║');

  // Stats row
  const statsText = `Scans: ${state.totalScans}  Opened: ${state.positionsOpened}  Closed: ${state.positionsClosed}`;
  lines.push('║  ' + pad(statsText, BOX_WIDTH - 2) + '║');

  lines.push('╠' + '═'.repeat(BOX_WIDTH) + '╣');

  // Portfolio section
  lines.push('║  ' + pad('PORTFOLIO', BOX_WIDTH - 2) + '║');

  if (state.portfolio) {
    const p = state.portfolio;
    const totalUnrealized = state.positions.reduce((sum, pos) => sum + (pos.unrealizedPnl || 0), 0);
    const positionValue = state.positions.reduce((sum, pos) => {
      if (pos.currentPrice !== undefined) {
        return sum + pos.quantity * pos.currentPrice;
      }
      return sum + pos.costBasis;
    }, 0);
    const totalEquity = p.cashBalance + positionValue;
    const totalPnl = totalEquity - p.initialCapital;

    lines.push('║  ' + pad(`├─ Initial Capital:   ${formatCurrency(p.initialCapital)}`, BOX_WIDTH - 2) + '║');
    lines.push('║  ' + pad(`├─ Cash Balance:      ${formatCurrency(p.cashBalance)}`, BOX_WIDTH - 2) + '║');
    lines.push('║  ' + pad(`├─ Position Value:    ${formatCurrency(positionValue)}`, BOX_WIDTH - 2) + '║');
    lines.push('║  ' + pad(`├─ Total Equity:      ${formatCurrency(totalEquity)}`, BOX_WIDTH - 2) + '║');
    lines.push('║  ' + pad(`├─ Unrealized P&L:    ${formatPnl(totalUnrealized)}`, BOX_WIDTH - 2) + '║');
    lines.push('║  ' + pad(`└─ Total P&L:         ${formatPnl(totalPnl)}`, BOX_WIDTH - 2) + '║');
  } else {
    lines.push('║  ' + pad('  Loading...', BOX_WIDTH - 2) + '║');
  }

  lines.push('╠' + '═'.repeat(BOX_WIDTH) + '╣');

  // Positions section
  lines.push('║  ' + pad(`OPEN POSITIONS (${state.positions.length})`, BOX_WIDTH - 2) + '║');

  if (state.positions.length === 0) {
    lines.push('║  ' + pad('  No open positions', BOX_WIDTH - 2) + '║');
  } else {
    // Table header
    lines.push('║  ┌' + '─'.repeat(42) + '┬' + '─'.repeat(8) + '┬' + '─'.repeat(8) + '┬' + '─'.repeat(14) + '┐  ║');
    lines.push('║  │' + pad(' Market', 42) + '│' + pad(' Entry', 8) + '│' + pad(' Now', 8) + '│' + pad(' P&L', 14) + '│  ║');
    lines.push('║  ├' + '─'.repeat(42) + '┼' + '─'.repeat(8) + '┼' + '─'.repeat(8) + '┼' + '─'.repeat(14) + '┤  ║');

    // Position rows (max 5 to fit screen)
    const displayPositions = state.positions.slice(0, 5);
    for (const pos of displayPositions) {
      const marketName = truncate(pos.question, 40);
      const entryPrice = formatPercent(pos.entryPrice);
      const currentPrice = pos.currentPrice !== undefined ? formatPercent(pos.currentPrice) : '...';
      const pnl = pos.unrealizedPnl !== undefined ? formatPnl(pos.unrealizedPnl) : '...';

      lines.push('║  │' + pad(` ${marketName}`, 42) + '│' + pad(` ${entryPrice}`, 8) + '│' + pad(` ${currentPrice}`, 8) + '│' + pad(` ${pnl}`, 14) + '│  ║');
    }

    if (state.positions.length > 5) {
      lines.push('║  │' + pad(` ... and ${state.positions.length - 5} more`, 42) + '│' + pad('', 8) + '│' + pad('', 8) + '│' + pad('', 14) + '│  ║');
    }

    lines.push('║  └' + '─'.repeat(42) + '┴' + '─'.repeat(8) + '┴' + '─'.repeat(8) + '┴' + '─'.repeat(14) + '┘  ║');
  }

  // Footer
  lines.push('╠' + '═'.repeat(BOX_WIDTH) + '╣');
  lines.push('║  ' + pad('Press Ctrl+C to stop', BOX_WIDTH - 2) + '║');
  lines.push('╚' + '═'.repeat(BOX_WIDTH) + '╝');

  // Print all lines
  console.log(lines.join('\n'));
}

/**
 * Get status text based on current state.
 */
function getStatusText(state: DashboardState): string {
  switch (state.status) {
    case 'starting':
      return '🔄 Starting...';
    case 'scanning':
      if (state.scanProgress) {
        return `🔍 Scanning: ${state.scanProgress.current}/${state.scanProgress.total}`;
      }
      return '🔍 Scanning...';
    case 'monitoring':
      return '👁️  Monitoring positions';
    case 'idle':
    default:
      return '⏸️  Idle';
  }
}

/**
 * Print progress on the same line (for use during scan).
 */
export function printProgress(current: number, total: number, rejected: number, eligible: number): void {
  const pct = ((current / total) * 100).toFixed(0);
  const text = `  Scanning: ${current}/${total} (${pct}%) | Rejected: ${rejected} | Eligible: ${eligible}`;
  process.stdout.write(`\r${text}${' '.repeat(20)}`);
}

/**
 * Clear the progress line.
 */
export function clearProgress(): void {
  process.stdout.write('\r' + ' '.repeat(80) + '\r');
}

// ============================================================================
// Scanner Dashboard
// ============================================================================

export interface ScannerDashboardState {
  status: 'idle' | 'scanning';
  scanProgress?: { current: number; total: number };
  runtime: number;
  totalScans: number;
  positionsOpened: number;
  cashBalance: number;
  openPositionCount: number;
  lastUpdate: Date;
  recentOpened: Array<{ question: string; price: number; edge: number }>;
}

export function renderScannerDashboard(state: ScannerDashboardState): void {
  process.stdout.write('\x1B[2J\x1B[0f');
  const lines: string[] = [];

  lines.push('╔' + '═'.repeat(BOX_WIDTH) + '╗');
  lines.push('║' + pad('  🔍 SCANNER', BOX_WIDTH) + '║');
  lines.push('╠' + '═'.repeat(BOX_WIDTH) + '╣');

  // Status
  let statusText = '⏸️  Idle - waiting for next scan';
  if (state.status === 'scanning' && state.scanProgress) {
    const pct = ((state.scanProgress.current / state.scanProgress.total) * 100).toFixed(0);
    statusText = `🔍 Scanning: ${state.scanProgress.current}/${state.scanProgress.total} (${pct}%)`;
  }
  const timeText = `Runtime: ${formatDuration(state.runtime)}  ${state.lastUpdate.toLocaleTimeString()}`;
  lines.push('║  ' + pad(statusText, 45) + pad(timeText, BOX_WIDTH - 47) + '║');

  lines.push('╠' + '═'.repeat(BOX_WIDTH) + '╣');

  // Stats
  lines.push('║  ' + pad('STATS', BOX_WIDTH - 2) + '║');
  lines.push('║  ' + pad(`├─ Total Scans:       ${state.totalScans}`, BOX_WIDTH - 2) + '║');
  lines.push('║  ' + pad(`├─ Positions Opened:  ${state.positionsOpened}`, BOX_WIDTH - 2) + '║');
  lines.push('║  ' + pad(`├─ Cash Balance:      ${formatCurrency(state.cashBalance)}`, BOX_WIDTH - 2) + '║');
  lines.push('║  ' + pad(`└─ Open Positions:    ${state.openPositionCount}`, BOX_WIDTH - 2) + '║');

  lines.push('╠' + '═'.repeat(BOX_WIDTH) + '╣');

  // Recent opened
  lines.push('║  ' + pad('RECENTLY OPENED', BOX_WIDTH - 2) + '║');
  if (state.recentOpened.length === 0) {
    lines.push('║  ' + pad('  No positions opened yet', BOX_WIDTH - 2) + '║');
  } else {
    for (const pos of state.recentOpened.slice(0, 5)) {
      const text = `  ${truncate(pos.question, 50)} @ ${formatPercent(pos.price)} (edge: ${formatPercent(pos.edge)})`;
      lines.push('║  ' + pad(text, BOX_WIDTH - 2) + '║');
    }
  }

  lines.push('╠' + '═'.repeat(BOX_WIDTH) + '╣');
  lines.push('║  ' + pad('Press Ctrl+C to stop', BOX_WIDTH - 2) + '║');
  lines.push('╚' + '═'.repeat(BOX_WIDTH) + '╝');

  console.log(lines.join('\n'));
}

// ============================================================================
// Monitor Dashboard
// ============================================================================

export interface MonitorDashboardState {
  status: 'idle' | 'checking';
  runtime: number;
  totalCycles: number;
  takeProfitCount: number;
  stopLossCount: number;
  resolvedCount: number;
  positions: PositionWithPrice[];
  portfolio: Portfolio | null;
  lastUpdate: Date;
}

export function renderMonitorDashboard(state: MonitorDashboardState): void {
  process.stdout.write('\x1B[2J\x1B[0f');
  const lines: string[] = [];

  lines.push('╔' + '═'.repeat(BOX_WIDTH) + '╗');
  lines.push('║' + pad('  👁️  MONITOR', BOX_WIDTH) + '║');
  lines.push('╠' + '═'.repeat(BOX_WIDTH) + '╣');

  // Status
  const statusText = state.status === 'checking' ? '🔄 Checking positions...' : '⏸️  Idle - waiting for next check';
  const timeText = `Runtime: ${formatDuration(state.runtime)}  ${state.lastUpdate.toLocaleTimeString()}`;
  lines.push('║  ' + pad(statusText, 45) + pad(timeText, BOX_WIDTH - 47) + '║');

  lines.push('╠' + '═'.repeat(BOX_WIDTH) + '╣');

  // Stats
  lines.push('║  ' + pad('STATS', BOX_WIDTH - 2) + '║');
  lines.push('║  ' + pad(`├─ Monitor Cycles:    ${state.totalCycles}`, BOX_WIDTH - 2) + '║');
  lines.push('║  ' + pad(`├─ Take Profits:      ${state.takeProfitCount}`, BOX_WIDTH - 2) + '║');
  lines.push('║  ' + pad(`├─ Stop Losses:       ${state.stopLossCount}`, BOX_WIDTH - 2) + '║');
  lines.push('║  ' + pad(`└─ Resolved:          ${state.resolvedCount}`, BOX_WIDTH - 2) + '║');

  lines.push('╠' + '═'.repeat(BOX_WIDTH) + '╣');

  // Portfolio
  if (state.portfolio) {
    const p = state.portfolio;
    const totalUnrealized = state.positions.reduce((sum, pos) => sum + (pos.unrealizedPnl || 0), 0);
    const positionValue = state.positions.reduce((sum, pos) => {
      if (pos.currentPrice !== undefined) return sum + pos.quantity * pos.currentPrice;
      return sum + pos.costBasis;
    }, 0);
    const totalEquity = p.cashBalance + positionValue;
    const totalPnl = totalEquity - p.initialCapital;

    lines.push('║  ' + pad('PORTFOLIO', BOX_WIDTH - 2) + '║');
    lines.push('║  ' + pad(`├─ Cash: ${formatCurrency(p.cashBalance)}  Positions: ${formatCurrency(positionValue)}  Equity: ${formatCurrency(totalEquity)}`, BOX_WIDTH - 2) + '║');
    lines.push('║  ' + pad(`└─ Unrealized: ${formatPnl(totalUnrealized)}  Total P&L: ${formatPnl(totalPnl)}`, BOX_WIDTH - 2) + '║');
  }

  lines.push('╠' + '═'.repeat(BOX_WIDTH) + '╣');

  // Positions table
  lines.push('║  ' + pad(`OPEN POSITIONS (${state.positions.length})`, BOX_WIDTH - 2) + '║');

  if (state.positions.length === 0) {
    lines.push('║  ' + pad('  No open positions', BOX_WIDTH - 2) + '║');
  } else {
    lines.push('║  ┌' + '─'.repeat(42) + '┬' + '─'.repeat(8) + '┬' + '─'.repeat(8) + '┬' + '─'.repeat(14) + '┐  ║');
    lines.push('║  │' + pad(' Market', 42) + '│' + pad(' Entry', 8) + '│' + pad(' Now', 8) + '│' + pad(' P&L', 14) + '│  ║');
    lines.push('║  ├' + '─'.repeat(42) + '┼' + '─'.repeat(8) + '┼' + '─'.repeat(8) + '┼' + '─'.repeat(14) + '┤  ║');

    const displayPositions = state.positions.slice(0, 8);
    for (const pos of displayPositions) {
      const marketName = truncate(pos.question, 40);
      const entryPrice = formatPercent(pos.entryPrice);
      const currentPrice = pos.currentPrice !== undefined ? formatPercent(pos.currentPrice) : '...';
      const pnl = pos.unrealizedPnl !== undefined ? formatPnl(pos.unrealizedPnl) : '...';
      lines.push('║  │' + pad(` ${marketName}`, 42) + '│' + pad(` ${entryPrice}`, 8) + '│' + pad(` ${currentPrice}`, 8) + '│' + pad(` ${pnl}`, 14) + '│  ║');
    }

    if (state.positions.length > 8) {
      lines.push('║  │' + pad(` ... and ${state.positions.length - 8} more`, 42) + '│' + pad('', 8) + '│' + pad('', 8) + '│' + pad('', 14) + '│  ║');
    }

    lines.push('║  └' + '─'.repeat(42) + '┴' + '─'.repeat(8) + '┴' + '─'.repeat(8) + '┴' + '─'.repeat(14) + '┘  ║');
  }

  lines.push('╠' + '═'.repeat(BOX_WIDTH) + '╣');
  lines.push('║  ' + pad('Press Ctrl+C to stop', BOX_WIDTH - 2) + '║');
  lines.push('╚' + '═'.repeat(BOX_WIDTH) + '╝');

  console.log(lines.join('\n'));
}

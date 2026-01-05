/**
 * Terminal dashboard for displaying market data and opportunities.
 */

import chalk from 'chalk';
import Table from 'cli-table3';
import {
  MarketSnapshot,
  Opportunity,
  OpportunityType,
  volumeDistributionToRecord,
  spreadDistributionToRecord,
  isMarketNew,
} from './types';
import { MarketScanner } from './scanner';
import { OpportunityDetector } from './detector';

export interface DashboardConfig {
  scanner: MarketScanner;
  detector: OpportunityDetector;
  updateInterval?: number;
}

export class Dashboard {
  private scanner: MarketScanner;
  private detector: OpportunityDetector;
  private updateInterval: number;
  private startTime: Date;
  private intervalId: NodeJS.Timeout | null = null;

  constructor(config: DashboardConfig) {
    this.scanner = config.scanner;
    this.detector = config.detector;
    this.updateInterval = config.updateInterval || 60;
    this.startTime = new Date();
  }

  private formatVolume(volume: number): string {
    if (volume >= 1_000_000) {
      return `$${(volume / 1_000_000).toFixed(1)}M`;
    } else if (volume >= 1_000) {
      return `$${(volume / 1_000).toFixed(1)}K`;
    } else {
      return `$${volume.toFixed(0)}`;
    }
  }

  private formatPercent(pct: number): string {
    return `${(pct * 100).toFixed(2)}%`;
  }

  private formatUptime(): string {
    const ms = Date.now() - this.startTime.getTime();
    const seconds = Math.floor(ms / 1000);
    const minutes = Math.floor(seconds / 60);
    const hours = Math.floor(minutes / 60);
    const days = Math.floor(hours / 24);

    if (days > 0) {
      return `${days}d ${hours % 24}h ${minutes % 60}m`;
    } else if (hours > 0) {
      return `${hours}h ${minutes % 60}m ${seconds % 60}s`;
    } else if (minutes > 0) {
      return `${minutes}m ${seconds % 60}s`;
    } else {
      return `${seconds}s`;
    }
  }

  private renderHeader(): void {
    const snapshot = this.scanner.getCurrentSnapshot();
    const stats = this.scanner.getStats();

    console.log(chalk.cyan.bold('\n═══════════════════════════════════════════════════════════'));
    console.log(chalk.cyan.bold('          POLYMARKET MARKET DISCOVERY BOT'));
    console.log(chalk.cyan.bold('═══════════════════════════════════════════════════════════'));

    const headerInfo = [
      `Uptime: ${this.formatUptime()}`,
      `Scans: ${stats.totalScans}`,
      `Last scan: ${stats.lastScanDuration.toFixed(1)}s`,
      snapshot ? chalk.green(`Markets: ${snapshot.totalMarkets}`) : chalk.yellow('Waiting...'),
    ];

    console.log(chalk.dim(headerInfo.join(' | ')));
    console.log();
  }

  private renderVolumeDistribution(): void {
    const snapshot = this.scanner.getCurrentSnapshot();
    if (!snapshot) return;

    const table = new Table({
      head: [chalk.cyan('Volume Tier'), chalk.cyan('Count'), chalk.cyan('Bar')],
      colWidths: [15, 10, 35],
    });

    const dist = volumeDistributionToRecord(snapshot.volumeDistribution);
    const total = Object.values(dist).reduce((a, b) => a + b, 0);

    for (const [tier, count] of Object.entries(dist)) {
      const pct = total > 0 ? count / total : 0;
      const bar = '█'.repeat(Math.floor(pct * 30));
      table.push([tier, count.toString(), chalk.green(bar)]);
    }

    console.log(chalk.bold('Volume Distribution (24h)'));
    console.log(table.toString());
    console.log();
  }

  private renderSpreadDistribution(): void {
    const snapshot = this.scanner.getCurrentSnapshot();
    if (!snapshot) return;

    const table = new Table({
      head: [chalk.cyan('Spread'), chalk.cyan('Count'), chalk.cyan('Bar')],
      colWidths: [15, 10, 35],
    });

    const dist = spreadDistributionToRecord(snapshot.spreadDistribution);
    const total = Object.values(dist).reduce((a, b) => a + b, 0);

    for (const [spread, count] of Object.entries(dist)) {
      const pct = total > 0 ? count / total : 0;
      const bar = '█'.repeat(Math.floor(pct * 30));
      table.push([spread, count.toString(), chalk.green(bar)]);
    }

    console.log(chalk.bold('Spread Distribution'));
    console.log(table.toString());
    console.log();
  }

  private renderOpportunities(): void {
    const recent = this.detector.getRecentOpportunities(1.0);

    const table = new Table({
      head: [chalk.cyan('Type'), chalk.cyan('Count'), chalk.cyan('Avg Value')],
      colWidths: [15, 10, 20],
    });

    const typeDisplay: Record<OpportunityType, { name: string; valueField: keyof Opportunity }> = {
      [OpportunityType.ARBITRAGE]: { name: 'Arbitrage', valueField: 'yesNoSum' },
      [OpportunityType.WIDE_SPREAD]: { name: 'Wide Spread', valueField: 'spreadPct' },
      [OpportunityType.VOLUME_SPIKE]: { name: 'Volume Spike', valueField: 'spikeMultiplier' },
      [OpportunityType.THIN_BOOK]: { name: 'Thin Book', valueField: 'makerCount' },
      [OpportunityType.MISPRICING]: { name: 'Mispricing', valueField: 'priceDifference' },
    };

    for (const [opType, { name, valueField }] of Object.entries(typeDisplay)) {
      const ops = recent.filter((op) => op.type === opType);
      if (ops.length > 0) {
        const values = ops.map((op) => op[valueField] as number);
        const avg = values.reduce((a, b) => a + b, 0) / values.length;

        let avgStr: string;
        if (opType === OpportunityType.ARBITRAGE) {
          avgStr = `sum=${avg.toFixed(4)}`;
        } else if (opType === OpportunityType.WIDE_SPREAD) {
          avgStr = this.formatPercent(avg);
        } else if (opType === OpportunityType.VOLUME_SPIKE) {
          avgStr = `${avg.toFixed(1)}x`;
        } else if (opType === OpportunityType.THIN_BOOK) {
          avgStr = `${avg.toFixed(0)} makers`;
        } else {
          avgStr = this.formatPercent(avg);
        }

        table.push([name, ops.length.toString(), avgStr]);
      } else {
        table.push([name, '0', '-']);
      }
    }

    console.log(chalk.bold('Opportunities (Last Hour)'));
    console.log(table.toString());
    console.log();
  }

  private renderTopMarkets(): void {
    const snapshot = this.scanner.getCurrentSnapshot();
    if (!snapshot || snapshot.markets.length === 0) return;

    const table = new Table({
      head: [
        chalk.cyan('Market'),
        chalk.cyan('Volume'),
        chalk.cyan('YES Spread'),
        chalk.cyan('NO Spread'),
      ],
      colWidths: [45, 12, 12, 12],
    });

    const sortedMarkets = [...snapshot.markets]
      .sort((a, b) => b.volume24h - a.volume24h)
      .slice(0, 5);

    for (const market of sortedMarkets) {
      const question =
        market.question.length > 42 ? market.question.slice(0, 42) + '...' : market.question;

      let yesSpread = '-';
      let noSpread = '-';

      if (market.yesToken && market.yesToken.spreadPct > 0) {
        const pct = market.yesToken.spreadPct;
        const color = pct < 0.02 ? chalk.green : pct < 0.05 ? chalk.yellow : chalk.red;
        yesSpread = color(this.formatPercent(pct));
      }

      if (market.noToken && market.noToken.spreadPct > 0) {
        const pct = market.noToken.spreadPct;
        const color = pct < 0.02 ? chalk.green : pct < 0.05 ? chalk.yellow : chalk.red;
        noSpread = color(this.formatPercent(pct));
      }

      table.push([question, this.formatVolume(market.volume24h), yesSpread, noSpread]);
    }

    console.log(chalk.bold('Top 5 Most Liquid Markets'));
    console.log(table.toString());
    console.log();
  }

  private renderNewMarkets(): void {
    const snapshot = this.scanner.getCurrentSnapshot();
    if (!snapshot) return;

    const table = new Table({
      head: [chalk.cyan('Market'), chalk.cyan('Age'), chalk.cyan('Volume')],
      colWidths: [50, 10, 15],
    });

    const newMarkets = snapshot.markets
      .filter((m) => isMarketNew(m) && m.createdAt)
      .sort((a, b) => b.createdAt!.getTime() - a.createdAt!.getTime())
      .slice(0, 5);

    for (const market of newMarkets) {
      const question =
        market.question.length > 47 ? market.question.slice(0, 47) + '...' : market.question;

      const age = Date.now() - market.createdAt!.getTime();
      let ageStr: string;
      if (age < 3600000) {
        ageStr = `${Math.floor(age / 60000)}m`;
      } else {
        ageStr = `${Math.floor(age / 3600000)}h`;
      }

      table.push([question, ageStr, this.formatVolume(market.volume24h)]);
    }

    if (newMarkets.length === 0) {
      table.push(['No new markets found', '-', '-']);
    }

    console.log(chalk.bold('Top 5 Newest Markets (<24h)'));
    console.log(table.toString());
    console.log();
  }

  private renderAlerts(): void {
    const recent = this.detector.getRecentOpportunities(0.5); // Last 30 minutes

    const priorityOps = recent
      .filter(
        (op) =>
          op.type === OpportunityType.ARBITRAGE ||
          (op.type === OpportunityType.WIDE_SPREAD && op.spreadPct > 0.1) ||
          (op.type === OpportunityType.VOLUME_SPIKE && op.spikeMultiplier > 5)
      )
      .slice(0, 5);

    console.log(chalk.bold.red('Recent Alerts'));
    console.log(chalk.red('─'.repeat(60)));

    if (priorityOps.length === 0) {
      console.log(chalk.dim('No significant alerts in the last 30 minutes'));
    } else {
      for (const op of priorityOps) {
        const question = op.question.slice(0, 40) + '...';

        if (op.type === OpportunityType.ARBITRAGE) {
          console.log(
            `${chalk.bold.red('[ARB]')} ${chalk.white(question)} ${chalk.yellow(`sum=${op.yesNoSum.toFixed(4)}`)}`
          );
        } else if (op.type === OpportunityType.WIDE_SPREAD) {
          console.log(
            `${chalk.bold.yellow('[SPREAD]')} ${chalk.white(question)} ${chalk.cyan(`${op.tokenSide} ${this.formatPercent(op.spreadPct)}`)}`
          );
        } else if (op.type === OpportunityType.VOLUME_SPIKE) {
          console.log(
            `${chalk.bold.green('[SPIKE]')} ${chalk.white(question)} ${chalk.cyan(`${op.spikeMultiplier.toFixed(1)}x`)}`
          );
        }
      }
    }

    console.log();
  }

  private renderFooter(): void {
    const now = new Date();
    console.log(chalk.dim('─'.repeat(60)));
    console.log(
      chalk.dim(`Last update: ${now.toISOString()} | Press Ctrl+C to stop`)
    );
  }

  render(): void {
    console.clear();
    this.renderHeader();
    this.renderVolumeDistribution();
    this.renderSpreadDistribution();
    this.renderOpportunities();
    this.renderTopMarkets();
    this.renderNewMarkets();
    this.renderAlerts();
    this.renderFooter();
  }

  start(): void {
    this.render();
    this.intervalId = setInterval(() => {
      this.render();
    }, this.updateInterval * 1000);
  }

  stop(): void {
    if (this.intervalId) {
      clearInterval(this.intervalId);
      this.intervalId = null;
    }
  }

  async runBlocking(): Promise<void> {
    return new Promise((resolve) => {
      this.start();

      process.on('SIGINT', () => {
        this.stop();
        resolve();
      });

      process.on('SIGTERM', () => {
        this.stop();
        resolve();
      });
    });
  }
}

/**
 * Create a Dashboard using environment variables.
 */
export function createDashboardFromEnv(
  scanner: MarketScanner,
  detector: OpportunityDetector
): Dashboard {
  return new Dashboard({
    scanner,
    detector,
    updateInterval: parseFloat(process.env.DASHBOARD_UPDATE_SECONDS || '60'),
  });
}

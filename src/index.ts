/**
 * Polymarket Trading Bot - Main Entry Point
 * Uses WebSocket for real-time market data.
 */

import * as dotenv from 'dotenv';
import { Command } from 'commander';
import { WSMarketValidator, createWSValidatorFromEnv } from './wsValidator';
import { initDatabase, closeDatabase } from './database/index';
import { verifySchema, clearAllData, getTableCounts } from './database/schema';
import { generateValidationReport } from './analyzer/reportGenerator';

// Load environment variables (override shell env vars with .env file)
dotenv.config({ override: true });

async function runReport(): Promise<void> {
  console.log('Generating validation report...');
  initDatabase();

  const schemaCheck = await verifySchema();
  if (!schemaCheck.valid) {
    console.error('Database schema invalid. Missing tables:', schemaCheck.missing);
    process.exit(1);
  }

  try {
    const summary = await generateValidationReport(7, './reports');
    console.log('\n=== VALIDATION SUMMARY ===');
    console.log(`Recommendation: ${summary.recommendation}`);
    console.log(`Net Profit: $${summary.netProfit.toFixed(2)}`);
    console.log(`Win Rate: ${(summary.winRate * 100).toFixed(1)}%`);
    console.log(`Fill Rate: ${(summary.overallFillRate * 100).toFixed(1)}%`);
    console.log('\n' + summary.recommendationReason);
  } finally {
    await closeDatabase();
  }
}

async function runReset(): Promise<void> {
  console.log('Resetting database...');
  initDatabase();

  const schemaCheck = await verifySchema();
  if (!schemaCheck.valid) {
    console.error('Database schema invalid. Missing tables:', schemaCheck.missing);
    process.exit(1);
  }

  console.log('Clearing all data...');
  await clearAllData();
  console.log('Database reset complete');
  await closeDatabase();
}

async function runDbStatus(): Promise<void> {
  console.log('Checking database status...');
  initDatabase();

  const schemaCheck = await verifySchema();
  console.log(`Schema valid: ${schemaCheck.valid}`);
  if (!schemaCheck.valid) {
    console.log('Missing tables:', schemaCheck.missing);
  }

  const counts = await getTableCounts();
  console.log('\nTable row counts:');
  for (const [table, count] of Object.entries(counts)) {
    console.log(`  ${table}: ${count}`);
  }

  await closeDatabase();
}

async function main(): Promise<void> {
  const program = new Command();

  program
    .name('polymarket-bot')
    .description('Polymarket Trading Bot with WebSocket real-time data')
    .version('2.0.0');

  // Default command - WebSocket validator
  program
    .command('validate', { isDefault: true })
    .description('Run the trading bot with WebSocket real-time price updates')
    .action(async () => {
      console.log(`
╔══════════════════════════════════════════════════════════════════╗
║         POLYMARKET WEBSOCKET TRADING BOT                         ║
║                                                                  ║
║  Real-time price updates via WebSocket                           ║
║  Automatic arbitrage detection                                   ║
║  Paper trading with simulated execution                          ║
║                                                                  ║
║  Press Ctrl+C to stop                                            ║
╚══════════════════════════════════════════════════════════════════╝
      `);
      const validator = createWSValidatorFromEnv();
      await validator.start();
    });

  // Report command - generate report from existing data
  program
    .command('report')
    .description('Generate validation report from existing data')
    .action(async () => {
      await runReport();
    });

  // Reset command - clear all data
  program
    .command('reset')
    .description('Reset database (clear all data)')
    .action(async () => {
      await runReset();
    });

  // DB status command
  program
    .command('db-status')
    .description('Check database status and table counts')
    .action(async () => {
      await runDbStatus();
    });

  await program.parseAsync(process.argv);
}

main().catch((error) => {
  console.error('Fatal error:', error);
  process.exit(1);
});

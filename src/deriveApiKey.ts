/**
 * Derive or create API keys for Polymarket CLOB.
 * Run with: npx ts-node src/deriveApiKey.ts
 */

import { ClobClient, Chain } from '@polymarket/clob-client';
import { Wallet } from '@ethersproject/wallet';
import dotenv from 'dotenv';

dotenv.config();

const CLOB_HOST = 'https://clob.polymarket.com';

async function main(): Promise<void> {
  const privateKey = process.env.POLYMARKET_PRIVATE_KEY;

  if (!privateKey) {
    console.error('Error: POLYMARKET_PRIVATE_KEY not set in .env');
    process.exit(1);
  }

  console.log('═══════════════════════════════════════════════════════════');
  console.log('     POLYMARKET API KEY DERIVATION');
  console.log('═══════════════════════════════════════════════════════════');
  console.log('');

  // Create wallet
  const wallet = new Wallet(privateKey);
  console.log(`Wallet address: ${wallet.address}`);
  console.log('');

  // Create unauthenticated client (no creds)
  const client = new ClobClient(CLOB_HOST, Chain.POLYGON, wallet);

  console.log('Deriving API credentials from private key...');
  console.log('(This will create new keys if none exist, or return existing ones)');
  console.log('');

  try {
    // Try to derive existing key first
    const creds = await client.deriveApiKey();

    console.log('✅ SUCCESS! Here are your API credentials:');
    console.log('');
    console.log('═══════════════════════════════════════════════════════════');
    console.log('Add these to your .env file:');
    console.log('═══════════════════════════════════════════════════════════');
    console.log('');
    console.log(`POLYMARKET_API_KEY=${creds.key}`);
    console.log(`POLYMARKET_API_SECRET=${creds.secret}`);
    console.log(`POLYMARKET_API_PASSPHRASE=${creds.passphrase}`);
    console.log('');
    console.log('═══════════════════════════════════════════════════════════');

  } catch (error: any) {
    console.error('');
    console.error('❌ Failed to derive API key:', error.message || error);

    // If derivation fails, try to create new key
    console.log('');
    console.log('Attempting to create new API key...');

    try {
      const newCreds = await client.createApiKey();

      console.log('');
      console.log('✅ SUCCESS! New API credentials created:');
      console.log('');
      console.log('═══════════════════════════════════════════════════════════');
      console.log('Add these to your .env file:');
      console.log('═══════════════════════════════════════════════════════════');
      console.log('');
      console.log(`POLYMARKET_API_KEY=${newCreds.key}`);
      console.log(`POLYMARKET_API_SECRET=${newCreds.secret}`);
      console.log(`POLYMARKET_API_PASSPHRASE=${newCreds.passphrase}`);
      console.log('');
      console.log('═══════════════════════════════════════════════════════════');

    } catch (createError: any) {
      console.error('');
      console.error('❌ Failed to create API key:', createError.message || createError);
      console.error('');
      console.error('Make sure your wallet has funds and has accepted Polymarket terms.');
      process.exit(1);
    }
  }
}

main();

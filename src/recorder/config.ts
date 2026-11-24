/**
 * Recorder configuration.
 *
 * The recorder captures raw market data for later research. It records more
 * than any single strategy needs, so new strategies can be backtested without
 * waiting for new data to accumulate.
 */

export type Recurrence = '5m' | '15m' | 'hourly' | '4h' | 'daily';

export interface SeriesDef {
  id: string;          // Gamma series id
  slug: string;        // Gamma series slug
  coin: string;        // lowercase coin symbol, matches Binance base asset
  recurrence: Recurrence;
}

export const RECURRENCE_MS: Record<Recurrence, number> = {
  '5m': 5 * 60_000,
  '15m': 15 * 60_000,
  hourly: 60 * 60_000,
  '4h': 4 * 60 * 60_000,
  daily: 24 * 60 * 60_000,
};

/**
 * Polymarket recurring crypto Up/Down series (verified against Gamma on 2026-11-23).
 * The recorder also logs every `new_market` broadcast, so series missing here show up in the data.
 */
export const SERIES: SeriesDef[] = [
  ['10684', 'btc-up-or-down-5m', 'btc', '5m'],
  ['10192', 'btc-up-or-down-15m', 'btc', '15m'],
  ['10114', 'btc-up-or-down-hourly', 'btc', 'hourly'],
  ['10331', 'btc-up-or-down-4h', 'btc', '4h'],
  ['41', 'btc-up-or-down-daily', 'btc', 'daily'],
  ['10683', 'eth-up-or-down-5m', 'eth', '5m'],
  ['10191', 'eth-up-or-down-15m', 'eth', '15m'],
  ['10117', 'eth-up-or-down-hourly', 'eth', 'hourly'],
  ['10332', 'eth-up-or-down-4h', 'eth', '4h'],
  ['40', 'eth-up-or-down-daily', 'eth', 'daily'],
  ['10686', 'sol-up-or-down-5m', 'sol', '5m'],
  ['10423', 'sol-up-or-down-15m', 'sol', '15m'],
  ['10122', 'solana-up-or-down-hourly', 'sol', 'hourly'],
  ['10333', 'sol-up-or-down-4h', 'sol', '4h'],
  ['10086', 'solana-up-or-down-daily', 'sol', 'daily'],
  ['10685', 'xrp-up-or-down-5m', 'xrp', '5m'],
  ['10422', 'xrp-up-or-down-15m', 'xrp', '15m'],
  ['10123', 'xrp-up-or-down-hourly', 'xrp', 'hourly'],
  ['10327', 'xrp-up-or-down-4h', 'xrp', '4h'],
  ['10100', 'xrp-up-or-down-daily', 'xrp', 'daily'],
  ['11325', 'doge-up-or-down-5m', 'doge', '5m'],
  ['11328', 'doge-up-or-down-15m', 'doge', '15m'],
  ['11287', 'doge-up-or-down-hourly', 'doge', 'hourly'],
  ['11331', 'doge-up-or-down-4h', 'doge', '4h'],
  ['11326', 'bnb-up-or-down-5m', 'bnb', '5m'],
  ['11330', 'bnb-up-or-down-15m', 'bnb', '15m'],
  ['11291', 'bnb-up-or-down-hourly', 'bnb', 'hourly'],
  ['11332', 'bnb-up-or-down-4h', 'bnb', '4h'],
  ['10421', 'bnb-up-or-down-daily', 'bnb', 'daily'],
  ['11327', 'hype-up-or-down-5m', 'hype', '5m'],
  ['11329', 'hype-up-or-down-15m', 'hype', '15m'],
  ['11289', 'hype-up-or-down-hourly', 'hype', 'hourly'],
  ['11333', 'hype-up-or-down-4h', 'hype', '4h'],
  ['11288', 'hype-up-or-down-daily', 'hype', 'daily'],
  ['12517', 'zec-up-or-down-5m', 'zec', '5m'],
  ['12518', 'zec-up-or-down-15m', 'zec', '15m'],
  ['12519', 'zec-up-or-down-4h', 'zec', '4h'],
].map(([id, slug, coin, recurrence]) => ({ id, slug, coin, recurrence: recurrence as Recurrence }));

/** Binance base assets to record (spot + USDⓈ-M perpetual, quoted in USDT). */
export const COINS = ['btc', 'eth', 'sol', 'xrp', 'doge', 'bnb', 'hype', 'zec'];

export const CONFIG = {
  dataDir: process.env.RECORDER_DATA_DIR || './data/recorder',
  // Level 9: ~25-30% smaller than 3 for ~4% of one CPU; 12+ costs far more CPU for little gain.
  zstdLevel: parseInt(process.env.RECORDER_ZSTD_LEVEL || '9', 10),

  // Polymarket
  gammaUrl: 'https://gamma-api.polymarket.com',
  dataApiUrl: 'https://data-api.polymarket.com',
  clobWsUrl: 'wss://ws-subscriptions-clob.polymarket.com/ws/market',
  rtdsWsUrl: 'wss://ws-live-data.polymarket.com',
  discoveryIntervalMs: 30_000,
  /** Start recording a market this long before its window opens (multiples of its duration, min 10 min). */
  subscribeLeadWindows: 2,
  minSubscribeLeadMs: 10 * 60_000,
  /** Keep recording this long after a window ends (late trades, book teardown). */
  unsubscribeAfterEndMs: 3 * 60_000,
  resolutionPollIntervalMs: 30_000,
  resolutionGiveUpMs: 6 * 60 * 60_000,

  // Binance
  binanceSpotWs: 'wss://stream.binance.com:9443/stream?streams=',
  binanceFutPublicWs: 'wss://fstream.binance.com/public/stream?streams=',
  binanceFutMarketWs: 'wss://fstream.binance.com/market/stream?streams=',
  binanceSpotRest: 'https://api.binance.com',
  binanceFutRest: 'https://fapi.binance.com',
  depthSnapshotIntervalMs: 60_000,
  futStatsIntervalMs: 60_000,

  statsIntervalMs: 60_000,
};

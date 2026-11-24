/**
 * Binance recorder: spot + USDⓈ-M perpetual market data for COINS.
 *
 * WebSocket (lossless, every message):
 *   spot:        <sym>@trade, <sym>@bookTicker, <sym>@depth@100ms
 *   fut /public: <sym>@bookTicker, <sym>@depth@100ms
 *   fut /market: <sym>@aggTrade, <sym>@markPrice@1s, !forceOrder@arr (all symbols)
 * Binance split the futures WebSocket: the legacy fstream URL silently drops
 * the /market streams, so both endpoints are required.
 *
 * REST (polled):
 *   depth snapshots (1000 levels) every minute, so the order book can be rebuilt
 *   from snapshot + diffs; open interest every minute; positioning ratios every 5 min.
 */

import { COINS, CONFIG } from './config';
import { tsv, jsonl } from './sink';
import { ReconnectingWs } from './wsClient';
import { logEvent } from './stats';

const levels = (l: [string, string][]) => l.map(([p, q]) => `${p}:${q}`).join('|');

const spotTrade = tsv('bn_spot_trade', ['sym', 'event_ms', 'trade_ms', 'trade_id', 'price', 'qty', 'buyer_is_maker']);
const spotBbo = tsv('bn_spot_bbo', ['sym', 'update_id', 'bid', 'bid_qty', 'ask', 'ask_qty']);
const spotDepth = tsv('bn_spot_depth', ['sym', 'event_ms', 'first_update_id', 'last_update_id', 'bids', 'asks']);
const futTrade = tsv('bn_fut_trade', ['sym', 'event_ms', 'trade_ms', 'agg_id', 'first_id', 'last_id', 'price', 'qty', 'buyer_is_maker']);
const futBbo = tsv('bn_fut_bbo', ['sym', 'update_id', 'event_ms', 'txn_ms', 'bid', 'bid_qty', 'ask', 'ask_qty']);
const futDepth = tsv('bn_fut_depth', ['sym', 'event_ms', 'txn_ms', 'first_update_id', 'last_update_id', 'prev_update_id', 'bids', 'asks']);
const futMark = tsv('bn_fut_mark', ['sym', 'event_ms', 'mark_price', 'index_price', 'est_settle_price', 'funding_rate', 'next_funding_ms']);
const futLiq = tsv('bn_fut_liquidation', ['sym', 'event_ms', 'trade_ms', 'side', 'order_type', 'tif', 'qty', 'price', 'avg_price', 'status', 'last_filled_qty', 'filled_qty']);
const snapshots = jsonl('bn_depth_snapshot');
const futStats = jsonl('bn_fut_stats');
const unknown = jsonl('bn_other');

/** Depth diff continuity per (market, symbol): detects dropped messages. */
const lastUpdateId = new Map<string, number>();
function checkSeq(key: string, first: number, last: number, prev: number | null): void {
  const before = lastUpdateId.get(key);
  lastUpdateId.set(key, last);
  if (before === undefined) return;
  const ok = prev !== null ? prev === before : first <= before + 1 && last >= before + 1;
  if (!ok) logEvent('seq_gap', key, `expected continuation of ${before}, got first=${first} prev=${prev} last=${last}`);
}

/** After a reconnect the gap is expected (and logged as ws_open); restart continuity tracking. */
function resetSeq(market: 'spot' | 'fut'): void {
  for (const k of [...lastUpdateId.keys()]) if (k.startsWith(market + ':')) lastUpdateId.delete(k);
}

function onSpot(raw: string, now: number): void {
  const { stream, data: d } = JSON.parse(raw);
  const kind = stream.slice(stream.indexOf('@') + 1);
  if (kind === 'trade') spotTrade([d.s, d.E, d.T, d.t, d.p, d.q, d.m ? 1 : 0], now);
  else if (kind === 'bookTicker') spotBbo([d.s, d.u, d.b, d.B, d.a, d.A], now);
  else if (kind === 'depth@100ms') {
    checkSeq(`spot:${d.s}`, d.U, d.u, null);
    spotDepth([d.s, d.E, d.U, d.u, levels(d.b), levels(d.a)], now);
  } else unknown({ market: 'spot', stream, d }, now);
}

function onFut(raw: string, now: number): void {
  const { stream, data: d } = JSON.parse(raw);
  if (stream === '!forceOrder@arr') {
    const o = d.o;
    futLiq([o.s, d.E, o.T, o.S, o.o, o.f, o.q, o.p, o.ap, o.X, o.l, o.z], now);
    return;
  }
  const kind = stream.slice(stream.indexOf('@') + 1);
  if (kind === 'bookTicker') futBbo([d.s, d.u, d.E, d.T, d.b, d.B, d.a, d.A], now);
  else if (kind === 'depth@100ms') {
    checkSeq(`fut:${d.s}`, d.U, d.u, d.pu);
    futDepth([d.s, d.E, d.T, d.U, d.u, d.pu, levels(d.b), levels(d.a)], now);
  } else if (kind === 'aggTrade') futTrade([d.s, d.E, d.T, d.a, d.f, d.l, d.p, d.q, d.m ? 1 : 0], now);
  else if (kind === 'markPrice@1s') futMark([d.s, d.E, d.p, d.i, d.P, d.r, d.T], now);
  else unknown({ market: 'fut', stream, d }, now);
}

async function getJson(url: string): Promise<unknown> {
  const res = await fetch(url, { signal: AbortSignal.timeout(10_000) });
  if (!res.ok) throw new Error(`${res.status} ${url}`);
  return res.json();
}

async function pollSnapshots(markets: ('spot' | 'fut')[] = ['spot', 'fut']): Promise<void> {
  for (const c of COINS) {
    const sym = `${c.toUpperCase()}USDT`;
    const urls: [string, string][] = [
      ['spot', `${CONFIG.binanceSpotRest}/api/v3/depth?symbol=${sym}&limit=1000`],
      ['fut', `${CONFIG.binanceFutRest}/fapi/v1/depth?symbol=${sym}&limit=1000`],
    ];
    for (const [market, url] of urls.filter(([m]) => markets.includes(m as 'spot' | 'fut'))) {
      try {
        const d = await getJson(url);
        snapshots({ market, sym, ...(d as object) }, Date.now());
      } catch (e) {
        logEvent('rest_error', `bn_snapshot:${market}:${sym}`, (e as Error).message);
      }
    }
  }
}

let statsTick = 0;
async function pollFutStats(): Promise<void> {
  const ratios = statsTick++ % 5 === 0; // positioning ratios only change every 5 min
  for (const c of COINS) {
    const sym = `${c.toUpperCase()}USDT`;
    const endpoints: [string, string][] = [['open_interest', `/fapi/v1/openInterest?symbol=${sym}`]];
    if (ratios) {
      for (const ep of ['globalLongShortAccountRatio', 'topLongShortAccountRatio', 'topLongShortPositionRatio', 'takerlongshortRatio', 'openInterestHist']) {
        endpoints.push([ep, `/futures/data/${ep}?symbol=${sym}&period=5m&limit=1`]);
      }
    }
    for (const [name, path] of endpoints) {
      try {
        futStats({ sym, endpoint: name, data: await getJson(CONFIG.binanceFutRest + path) }, Date.now());
      } catch (e) {
        logEvent('rest_error', `bn_fut_stats:${name}:${sym}`, (e as Error).message);
      }
    }
  }
}

/**
 * After a depth stream reconnects, books can't be rebuilt until the next snapshot.
 * Take one right away (after a short delay, so the new diff stream is already buffering).
 */
function depthReopened(market: 'spot' | 'fut'): () => void {
  let first = true;
  return () => {
    resetSeq(market);
    if (first) {
      first = false;
      return;
    }
    setTimeout(() => pollSnapshots([market]), 1500);
  };
}

export function startBinance(): ReconnectingWs[] {
  const s = COINS.map((c) => `${c}usdt`);
  const common = { staleMs: 30_000, maxLifetimeMs: 23 * 3_600_000 };
  const conns = [
    new ReconnectingWs({
      name: 'binance-spot',
      onOpen: depthReopened('spot'),
      url: () => CONFIG.binanceSpotWs + s.flatMap((x) => [`${x}@trade`, `${x}@bookTicker`, `${x}@depth@100ms`]).join('/'),
      onMessage: onSpot,
      ...common,
    }),
    // Futures top-of-book (~5k msg/s) and depth on separate connections: Binance drops connections that fall behind.
    new ReconnectingWs({
      name: 'binance-fut-bbo',
      url: () => CONFIG.binanceFutPublicWs + s.map((x) => `${x}@bookTicker`).join('/'),
      onMessage: onFut,
      ...common,
    }),
    new ReconnectingWs({
      name: 'binance-fut-depth',
      onOpen: depthReopened('fut'),
      url: () => CONFIG.binanceFutPublicWs + s.map((x) => `${x}@depth@100ms`).join('/'),
      onMessage: onFut,
      ...common,
    }),
    new ReconnectingWs({
      name: 'binance-fut-market',
      url: () => CONFIG.binanceFutMarketWs + [...s.flatMap((x) => [`${x}@aggTrade`, `${x}@markPrice@1s`]), '!forceOrder@arr'].join('/'),
      onMessage: onFut,
      ...common,
    }),
  ];
  conns.forEach((c) => c.start());
  pollSnapshots();
  setInterval(pollSnapshots, CONFIG.depthSnapshotIntervalMs);
  pollFutStats();
  setInterval(pollFutStats, CONFIG.futStatsIntervalMs);
  return conns;
}

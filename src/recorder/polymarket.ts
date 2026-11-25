/**
 * Polymarket recorder for recurring crypto Up/Down markets.
 *
 * - Discovery: polls Gamma for upcoming markets in every series, subscribes to
 *   each market from shortly before its window opens until a few minutes after it ends.
 * - CLOB market WebSocket: every book snapshot, price level change, best bid/ask
 *   and trade, plus `new_market` broadcasts and any unknown message type.
 * - Resolution: after a window ends, records the outcome and `priceToBeat`, then
 *   pulls every trade (with trader wallet) from the Data API v2.
 */

import { CONFIG, RECURRENCE_MS, SERIES, SeriesDef } from './config';
import { tsv, jsonl } from './sink';
import { ReconnectingWs } from './wsClient';
import { logEvent } from './stats';
import { bg, getPool } from './db';

interface Market {
  slug: string;
  series: SeriesDef;
  conditionId: string;
  upToken: string;
  downToken: string;
  start: number;
  end: number;
  subscribed: boolean;
  done: boolean;          // unsubscribed after end
  resolved: boolean;
  tradesFetched: boolean;
  inserted?: Promise<void>; // the rec_markets INSERT; row updates are chained after it
  failures?: number;      // consecutive REST failures while resolving (drives backoff)
  nextTry?: number;       // don't retry before this time (ms)
}

const markets = new Map<string, Market>();          // slug -> market
const tokenKey = new Map<string, string>();         // token id -> "slug|U" / "slug|D"
const key = (id: string) => tokenKey.get(id) ?? id;

const book = tsv('pm_book', ['ts_ms', 'asset', 'bids', 'asks', 'hash']);
const priceChange = tsv('pm_price_change', ['ts_ms', 'asset', 'side', 'price', 'size', 'best_bid', 'best_ask']);
const bba = tsv('pm_best_bid_ask', ['ts_ms', 'asset', 'best_bid', 'best_ask', 'spread']);
const trade = tsv('pm_trade', ['ts_ms', 'asset', 'side', 'price', 'size', 'fee_rate_bps', 'tx_hash']);
const marketMeta = jsonl('pm_market_meta');
const resolution = jsonl('pm_resolution');
const apiTrades = jsonl('pm_api_trades');
const newMarket = jsonl('pm_new_market');
const other = jsonl('pm_other');

const lv = (l: { price: string; size: string }[]) => (l || []).map((x) => `${x.price}:${x.size}`).join('|');

function onMessage(raw: string, now: number): void {
  if (raw === 'PONG' || raw === '') return;
  const parsed = JSON.parse(raw);
  for (const m of Array.isArray(parsed) ? parsed : [parsed]) {
    switch (m.event_type) {
      case 'book':
        book([m.timestamp, key(m.asset_id), lv(m.bids), lv(m.asks), m.hash], now);
        break;
      case 'price_change':
        for (const c of m.price_changes || []) {
          priceChange([m.timestamp, key(c.asset_id), c.side, c.price, c.size, c.best_bid, c.best_ask], now);
        }
        break;
      case 'best_bid_ask':
        bba([m.timestamp, key(m.asset_id), m.best_bid, m.best_ask, m.spread], now);
        break;
      case 'last_trade_price':
        trade([m.timestamp, key(m.asset_id), m.side, m.price, m.size, m.fee_rate_bps, m.transaction_hash], now);
        break;
      case 'new_market':
        newMarket(m, now);
        break;
      default:
        other(m, now);
    }
  }
}

/**
 * CLOB connections are grouped by series. A single connection carrying every market is
 * closed by Polymarket ("1013 slow consumer: send buffer full") within seconds, and even
 * one connection per series got occasional 1013s on the busiest series, so those get one
 * connection per market.
 */
const PER_MARKET_SERIES = new Set(['btc-up-or-down-5m', 'btc-up-or-down-15m', 'eth-up-or-down-5m']);
const groupOf = (m: Market) => (PER_MARKET_SERIES.has(m.series.slug) ? m.slug : m.series.slug);
const perMarket = (group: string) => markets.has(group) && PER_MARKET_SERIES.has(markets.get(group)!.series.slug);

const conns = new Map<string, ReconnectingWs>();

function connFor(group: string): ReconnectingWs {
  let c = conns.get(group);
  if (!c) {
    c = new ReconnectingWs({
      name: `pm:${group}`,
      url: () => CONFIG.clobWsUrl,
      onOpen: (sock) => {
        const ids = subscribedTokens(group);
        if (ids.length) sock.send(JSON.stringify({ assets_ids: ids, type: 'market', custom_feature_enabled: true }));
      },
      onMessage,
      textPing: { payload: 'PING', everyMs: 10_000 },
      // PONGs (every 10 s) count as messages, so 25 s of silence means the connection is dead.
      staleMs: 25_000,
    });
    conns.set(group, c);
    c.start();
  }
  return c;
}

function subscribedTokens(group?: string): string[] {
  return [...markets.values()]
    .filter((m) => m.subscribed && !m.done && (!group || groupOf(m) === group))
    .flatMap((m) => [m.upToken, m.downToken]);
}

function leadMs(m: Market): number {
  return Math.max(CONFIG.minSubscribeLeadMs, CONFIG.subscribeLeadWindows * RECURRENCE_MS[m.series.recurrence]);
}

const LOOKAHEAD: Record<string, number> = { '5m': 4, '15m': 3, hourly: 3, '4h': 2, daily: 2 };

class HttpError extends Error {
  constructor(readonly status: number, message: string, readonly retryAfterS?: number) {
    super(message);
  }
}

async function getJson(url: string): Promise<any> {
  const res = await fetch(url, { signal: AbortSignal.timeout(15_000) });
  if (!res.ok) {
    const ra = Number(res.headers.get('retry-after'));
    throw new HttpError(res.status, `${res.status} ${url.slice(0, 160)}`, Number.isFinite(ra) && ra > 0 ? ra : undefined);
  }
  return res.json();
}

/**
 * Rate-limit handling per API host: on HTTP 429 stop calling that host for Retry-After seconds
 * (at least 10, default 60) instead of retrying every market every 30 s, which only prolongs the limiting.
 */
type Api = 'gamma' | 'data';
const pausedUntil: Record<Api, number> = { gamma: 0, data: 0 };
const isPaused = (api: Api) => Date.now() < pausedUntil[api];
function noteError(api: Api, e: unknown): void {
  if (e instanceof HttpError && e.status === 429) {
    const wasPaused = isPaused(api);
    const pauseS = Math.max(e.retryAfterS ?? 60, 10); // Polymarket sends Retry-After: 1, too short to help
    pausedUntil[api] = Date.now() + pauseS * 1000;
    if (!wasPaused) logEvent('rate_limited', `pm_${api}`, `429, pausing ${pauseS} s: ${e.message.slice(4, 120)}`);
  }
}
/** Per-market exponential backoff after a failure: 30 s, 60 s, 2 min ... capped at 10 min. */
function backoff(m: Market): void {
  m.failures = (m.failures ?? 0) + 1;
  m.nextTry = Date.now() + Math.min(30_000 * 2 ** (m.failures - 1), 600_000);
}

function registerMarket(series: SeriesDef, event: any, now: number): void {
  const m = event.markets?.[0];
  if (!m || markets.has(event.slug)) return;
  const [up, down] = JSON.parse(m.clobTokenIds);
  const outcomes = JSON.parse(m.outcomes);
  if (outcomes[0] !== 'Up' || outcomes[1] !== 'Down') {
    logEvent('pm_unexpected_outcomes', event.slug, JSON.stringify(outcomes));
  }
  const mk: Market = {
    slug: event.slug,
    series,
    conditionId: m.conditionId,
    upToken: up,
    downToken: down,
    start: Date.parse(m.eventStartTime || event.startTime),
    end: Date.parse(m.endDate || event.endDate),
    subscribed: false,
    done: false,
    resolved: false,
    tradesFetched: false,
  };
  markets.set(mk.slug, mk);
  tokenKey.set(up, `${mk.slug}|U`);
  tokenKey.set(down, `${mk.slug}|D`);
  marketMeta({ series: series.slug, event }, now);
  mk.inserted = bg(
    `INSERT INTO rec_markets (slug, series_slug, coin, recurrence, condition_id, up_token_id, down_token_id,
       window_start, window_end, resolution_source, twap_lookback_s, fee_schedule, tick_size, min_order_size)
     VALUES ($1,$2,$3,$4,$5,$6,$7,$8,$9,$10,$11,$12,$13,$14) ON CONFLICT (slug) DO NOTHING`,
    [
      mk.slug, series.slug, series.coin, series.recurrence, mk.conditionId, up, down,
      new Date(mk.start), new Date(mk.end), event.resolutionSource ?? m.resolutionSource ?? null,
      m.cryptoMarketConfig?.twapLookbackSeconds ?? null, m.feeSchedule ?? null,
      m.orderPriceMinTickSize ?? null, m.orderMinSize ?? null,
    ],
  );
}

async function discover(): Promise<void> {
  const now = Date.now();
  const iso = new Date(now).toISOString();
  for (const s of SERIES) {
    if (isPaused('gamma')) break;
    try {
      const events = await getJson(
        `${CONFIG.gammaUrl}/events?series_id=${s.id}&closed=false&end_date_min=${iso}&order=endDate&ascending=true&limit=${LOOKAHEAD[s.recurrence]}`,
      );
      for (const e of events) registerMarket(s, e, now);
    } catch (e) {
      noteError('gamma', e);
      logEvent('rest_error', `pm_discover:${s.slug}`, (e as Error).message);
    }
  }
  updateSubscriptions();
}

/** Runs a row update for `m` once its INSERT has finished (now() is evaluated when the UPDATE runs: ms later). */
function afterInsert(m: Market, sql: string): void {
  void (m.inserted ?? Promise.resolve()).then(() => bg(sql, [m.slug]));
}

function updateSubscriptions(): void {
  const now = Date.now();
  const add = new Map<string, string[]>();
  const remove = new Map<string, string[]>();
  const push = (map: Map<string, string[]>, m: Market) => map.set(groupOf(m), [...(map.get(groupOf(m)) || []), m.upToken, m.downToken]);
  for (const m of markets.values()) {
    if (!m.subscribed && now >= m.start - leadMs(m) && now < m.end + CONFIG.unsubscribeAfterEndMs) {
      m.subscribed = true;
      push(add, m);
      // A market discovered in this same tick (4h/daily: lookahead == subscription lead) has its INSERT
      // still in flight; an UPDATE racing ahead of it matched no row and lost subscribed_at.
      afterInsert(m, 'UPDATE rec_markets SET subscribed_at = now(), updated_at = now() WHERE slug = $1');
    } else if (m.subscribed && !m.done && now >= m.end + CONFIG.unsubscribeAfterEndMs) {
      m.done = true;
      push(remove, m);
      afterInsert(m, 'UPDATE rec_markets SET unsubscribed_at = now(), updated_at = now() WHERE slug = $1');
    }
  }
  for (const [group, ids] of add) {
    const existed = conns.has(group);
    const c = connFor(group); // a new connection subscribes everything in onOpen
    if (existed && c.isOpen()) c.send(JSON.stringify({ assets_ids: ids, operation: 'subscribe', custom_feature_enabled: true }));
  }
  for (const [group, ids] of remove) {
    const c = conns.get(group);
    if (!c) continue;
    if (perMarket(group)) {
      c.stop(); // per-market connection: close it entirely
      conns.delete(group);
    } else {
      c.send(JSON.stringify({ assets_ids: ids, operation: 'unsubscribe' }));
    }
  }
  if (add.size || remove.size) {
    const n = (m: Map<string, string[]>) => [...m.values()].reduce((a, x) => a + x.length / 2, 0);
    logEvent('pm_subscriptions', 'polymarket', `+${n(add)} -${n(remove)} markets, ${subscribedTokens().length / 2} active on ${conns.size} connections`);
  }
}

/** All Data API trades of a market. Pages are buffered and written only once the download is
 *  complete, so a retry after a mid-pagination failure can't write trades twice. */
async function fetchTrades(m: Market): Promise<number> {
  let cursor: string | undefined;
  const rows: unknown[] = [];
  for (let page = 0; page < 200; page++) {
    const url = `${CONFIG.dataApiUrl}/v2/trades?condition_id=${m.conditionId}&limit=1000${cursor ? `&cursor=${encodeURIComponent(cursor)}` : ''}`;
    const r = await getJson(url);
    for (const t of r.data || []) rows.push({ slug: m.slug, ...t });
    if (!r.pagination?.has_more || !r.pagination?.next_cursor) break;
    cursor = r.pagination.next_cursor;
  }
  const now = Date.now();
  for (const row of rows) apiTrades(row, now);
  return rows.length;
}

let resolving = false;
async function resolveEnded(): Promise<void> {
  if (resolving) return;
  resolving = true;
  try {
    const now = Date.now();
    for (const m of markets.values()) {
      if (now < m.end + 30_000 || now < (m.nextTry ?? 0)) continue;
      try {
        if (!m.resolved) {
          if (isPaused('gamma')) continue;
          const [e] = await getJson(`${CONFIG.gammaUrl}/events?slug=${m.slug}`);
          const mm = e?.markets?.[0];
          const prices = JSON.parse(mm?.outcomePrices || '[]').map(Number);
          if (!mm?.closed || !(prices[0] === 1 || prices[1] === 1)) {
            // not settled yet; check once more before giving up so resumed old markets still resolve
            if (now > m.end + CONFIG.resolutionGiveUpMs) {
              logEvent('pm_unresolved', m.slug, 'gave up waiting for resolution');
              markets.delete(m.slug);
            }
            continue;
          }
          const outcome = prices[0] === 1 ? 'Up' : 'Down';
          resolution({ slug: m.slug, outcome, event: e }, Date.now());
          bg(
            `UPDATE rec_markets SET outcome = $2, price_to_beat = $3, resolved_at = $4, final_volume = $5, updated_at = now() WHERE slug = $1`,
            [m.slug, outcome, e.eventMetadata?.priceToBeat ?? null, mm.closedTime ? new Date(mm.closedTime.replace(' ', 'T').replace(/\+00$/, 'Z')) : null, mm.volume ?? null],
          );
          m.resolved = true;
        }
        if (m.resolved && !m.tradesFetched && now > m.end + 120_000) {
          if (isPaused('data')) continue;
          const n = await fetchTrades(m);
          bg('UPDATE rec_markets SET api_trades = $2, updated_at = now() WHERE slug = $1', [m.slug, n]);
          m.tradesFetched = true;
        }
        if (m.resolved && m.tradesFetched && m.done) markets.delete(m.slug); // keep tokenKey for late messages
        m.failures = 0;
      } catch (e) {
        noteError(m.resolved ? 'data' : 'gamma', e);
        backoff(m);
        logEvent('rest_error', `pm_resolve:${m.slug}`, `${(e as Error).message} (attempt ${m.failures}, next in ${Math.round(((m.nextTry ?? 0) - Date.now()) / 1000)} s)`);
      }
    }
  } finally {
    resolving = false;
  }
}

/**
 * After a restart, pick up markets that already ended but whose outcome or Data API trades were
 * not recorded yet (discovery only returns markets that haven't ended). Without this, every
 * restart lost the resolutions and trade lists of the markets that ended shortly before it.
 */
async function resumePending(): Promise<void> {
  const p = getPool();
  if (!p) return;
  const r = await p.query(
    `SELECT slug, series_slug, condition_id, up_token_id, down_token_id, window_start, window_end,
            outcome IS NOT NULL AS resolved
     FROM rec_markets
     WHERE window_end <= now() AND window_end > now() - interval '7 days'
       AND (outcome IS NULL OR api_trades IS NULL)`,
  );
  for (const row of r.rows) {
    const series = SERIES.find((s) => s.slug === row.series_slug);
    if (!series || markets.has(row.slug)) continue;
    markets.set(row.slug, {
      slug: row.slug, series, conditionId: row.condition_id, upToken: row.up_token_id, downToken: row.down_token_id,
      start: new Date(row.window_start).getTime(), end: new Date(row.window_end).getTime(),
      subscribed: true, done: true, resolved: row.resolved, tradesFetched: false,
    });
    tokenKey.set(row.up_token_id, `${row.slug}|U`);
    tokenKey.set(row.down_token_id, `${row.slug}|D`);
  }
  if (r.rows.length) logEvent('pm_resume', 'polymarket', `${r.rows.length} ended markets pending resolution/trades`);
}

export async function startPolymarket(): Promise<{ stop: () => void }> {
  await resumePending().catch((e) => logEvent('error', 'pm_resume', (e as Error).message));
  await discover(); // opens one connection per series with markets in range
  setInterval(() => discover().catch((e) => logEvent('error', 'pm_discover', e.message)), CONFIG.discoveryIntervalMs);
  setInterval(updateSubscriptions, 5_000);
  setInterval(() => resolveEnded().catch((e) => logEvent('error', 'pm_resolve', e.message)), CONFIG.resolutionPollIntervalMs);
  return { stop: () => conns.forEach((c) => c.stop()) };
}

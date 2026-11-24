/**
 * Polymarket real-time data socket: Chainlink prices (what the Up/Down markets
 * resolve on) and Polymarket's Binance price relay, for all symbols.
 *
 * Two redundant connections, deduplicated by (topic, symbol, timestamp): over the
 * first 16 hours a single connection stalled 59 times (1.5% of the time), which left
 * gaps in 2% of priceToBeat windows. Data is only lost if both stall at once.
 */

import { CONFIG } from './config';
import { tsv, jsonl } from './sink';
import { ReconnectingWs } from './wsClient';

const price = tsv('rtds_price', ['topic', 'symbol', 'ts_ms', 'value', 'full_accuracy_value', 'server_ts_ms', 'conn']);
const other = jsonl('rtds_other');

/** Latest price timestamp written per topic|symbol. */
const lastTs = new Map<string, number>();
let duplicates = 0;
export const rtdsDuplicates = () => duplicates;

function connection(id: string): ReconnectingWs {
  return new ReconnectingWs({
    name: `polymarket-rtds-${id}`,
    url: () => CONFIG.rtdsWsUrl,
    onOpen: (sock) =>
      sock.send(
        JSON.stringify({
          action: 'subscribe',
          subscriptions: [
            { topic: 'crypto_prices_chainlink', type: '*', filters: '' },
            { topic: 'crypto_prices', type: 'update' },
          ],
        }),
      ),
    onMessage: (raw, now) => {
      if (!raw || raw === 'PONG') return;
      const m = JSON.parse(raw);
      const p = m.payload;
      if (m.type === 'update' && p && p.symbol !== undefined && p.value !== undefined) {
        const k = `${m.topic}|${p.symbol}`;
        const ts = Number(p.timestamp);
        if (ts <= (lastTs.get(k) ?? 0)) {
          duplicates++;
          return; // already written by the other connection
        }
        lastTs.set(k, ts);
        price([m.topic, p.symbol, p.timestamp, p.value, p.full_accuracy_value, m.timestamp, id], now);
      } else {
        other({ conn: id, ...m }, now);
      }
    },
    textPing: { payload: 'PING', everyMs: 5_000 },
    // Prices arrive every second per symbol; 5 s of silence means the connection is dead.
    staleMs: 5_000,
  });
}

export function startRtds(): { stop: () => void } {
  const conns = [connection('a'), connection('b')];
  conns[0].start();
  setTimeout(() => conns[1].start(), 2_000); // stagger so they don't reconnect in lockstep
  return { stop: () => conns.forEach((c) => c.stop()) };
}

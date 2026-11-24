/**
 * Reconnecting WebSocket with optional text keepalive and stale-connection detection.
 */

import WebSocket from 'ws';
import { logEvent } from './stats';

export interface WsOptions {
  name: string;
  url: () => string;
  onOpen?: (ws: WebSocket) => void;
  onMessage: (data: string, recvMs: number) => void;
  /** Text keepalive (e.g. 'PING') sent every pingMs. Binance uses protocol pings, which `ws` answers automatically. */
  textPing?: { payload: string; everyMs: number };
  /** Reconnect if no message arrives for this long. */
  staleMs?: number;
  /** Proactively reconnect after this long (Binance drops connections at 24h). */
  maxLifetimeMs?: number;
}

export class ReconnectingWs {
  private ws: WebSocket | null = null;
  private timers: NodeJS.Timeout[] = [];
  private lastMsg = 0;
  private openedAt = 0;
  private backoffMs = 1000;
  private stopped = false;
  messages = 0;
  reconnects = 0;

  constructor(private readonly o: WsOptions) {}

  start(): void {
    this.connect();
  }

  send(data: string): void {
    if (this.ws && this.ws.readyState === WebSocket.OPEN) this.ws.send(data);
  }

  isOpen(): boolean {
    return !!this.ws && this.ws.readyState === WebSocket.OPEN;
  }

  stop(): void {
    this.stopped = true;
    this.clearTimers();
    this.ws?.terminate();
  }

  private clearTimers(): void {
    this.timers.forEach(clearInterval);
    this.timers = [];
  }

  private connect(): void {
    if (this.stopped) return;
    const ws = new WebSocket(this.o.url(), { perMessageDeflate: false, maxPayload: 64 * 1024 * 1024 });
    this.ws = ws;

    ws.on('open', () => {
      this.openedAt = Date.now();
      this.lastMsg = Date.now();
      this.backoffMs = 1000;
      logEvent('ws_open', this.o.name, '');
      this.o.onOpen?.(ws);
      if (this.o.textPing) {
        const { payload, everyMs } = this.o.textPing;
        this.timers.push(setInterval(() => ws.readyState === WebSocket.OPEN && ws.send(payload), everyMs));
      }
      this.timers.push(
        setInterval(() => {
          const now = Date.now();
          if (this.o.staleMs && now - this.lastMsg > this.o.staleMs) {
            logEvent('ws_stale', this.o.name, `no message for ${now - this.lastMsg} ms`);
            ws.terminate();
          } else if (this.o.maxLifetimeMs && now - this.openedAt > this.o.maxLifetimeMs) {
            logEvent('ws_rotate', this.o.name, 'max lifetime reached');
            ws.terminate();
          }
        }, 1000),
      );
    });

    ws.on('message', (data: WebSocket.RawData) => {
      const now = Date.now();
      this.lastMsg = now;
      this.messages++;
      try {
        this.o.onMessage(data.toString(), now);
      } catch (e) {
        logEvent('parse_error', this.o.name, (e as Error).message.slice(0, 300));
      }
    });

    ws.on('error', (e) => logEvent('ws_error', this.o.name, e.message.slice(0, 300)));

    ws.on('close', (code) => {
      this.clearTimers();
      if (this.stopped) return;
      this.reconnects++;
      logEvent('ws_close', this.o.name, `code=${code}, reconnect in ${this.backoffMs} ms`);
      setTimeout(() => this.connect(), this.backoffMs);
      this.backoffMs = Math.min(this.backoffMs * 2, 30_000);
    });
  }
}

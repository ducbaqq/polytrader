/**
 * Hourly-rotated, zstd-compressed output files.
 *
 * Each stream writes to  <dataDir>/raw/YYYY-MM-DD/HH/<stream>.<slotStartMs>.<ext>.zst,
 * rotating every 5 minutes (zstd buffers output, so a file only becomes readable
 * once it is closed; short files keep data visible and limit loss on power failure).
 * Compression runs in a child `zstd` process: if the recorder crashes, zstd
 * sees EOF and still finalizes a valid file.
 *
 * TSV files start with a header row. DuckDB reads them directly, e.g.
 *   read_csv('raw/2026-11-23/[HH]/bn_fut_bbo.[startMs].tsv.zst', delim='\t', header=true)
 * (use a glob for the hour and startMs parts).
 */

import { spawn, ChildProcessByStdio } from 'child_process';
import { Readable, Writable } from 'stream';
import * as fs from 'fs';
import * as path from 'path';
import { CONFIG } from './config';

const ROTATE_MS = 5 * 60_000;

interface OpenFile {
  slot: number;
  proc: ChildProcessByStdio<Writable, null, Readable>;
  path: string;
}

/**
 * zstd runs at lower CPU priority than the recorder (nice 10 vs -5): on the single vCPU,
 * receiving data must win; compression catches up afterwards.
 */
const ZSTD_NICE = '10';

class Stream {
  private file: OpenFile | null = null;
  /** Compressor for the next slot, started at a random moment in the last minute of the current one. */
  private next: OpenFile | null = null;
  private nextAt = 0;
  private closing = new Map<OpenFile, Promise<void>>();
  rows = 0;
  bytes = 0;

  constructor(
    readonly name: string,
    private readonly ext: 'tsv' | 'jsonl',
    private readonly header: string[] | null,
  ) {}

  write(line: string, nowMs: number): void {
    const slot = Math.floor(nowMs / ROTATE_MS);
    if (!this.file || this.file.slot !== slot) this.switchTo(slot, true);
    this.file!.proc.stdin.write(line + '\n');
    this.rows++;
    this.bytes += line.length + 1;
  }

  /**
   * Called every second. Spreads rotation work over time instead of doing it for every
   * stream at the slot boundary (which stalled the event loop for 4-6 s):
   * - pre-start the next slot's compressor at a random moment in the last 60 s of a slot;
   * - at the boundary only switch the write target;
   * - close the old file a few seconds later, at a random offset (see switchTo).
   */
  tick(nowMs: number): void {
    const slot = Math.floor(nowMs / ROTATE_MS);
    const slotEnd = (slot + 1) * ROTATE_MS;
    if (!this.next && this.file && this.file.slot === slot) {
      if (!this.nextAt) this.nextAt = slotEnd - 60_000 + Math.random() * 55_000;
      if (nowMs >= this.nextAt) this.next = this.open(slot + 1);
    }
    // Quiet streams: switch even without a write, so the old file gets closed.
    if (this.file && this.file.slot !== slot) this.switchTo(slot, false);
  }

  private switchTo(slot: number, needFile: boolean): void {
    const old = this.file;
    this.file = null;
    this.nextAt = 0;
    if (this.next && this.next.slot === slot) {
      this.file = this.next;
      this.next = null;
    } else if (needFile) {
      if (this.next) this.closeLater(this.next, 0); // stale pre-start (e.g. after a long silence)
      this.next = null;
      this.file = this.open(slot);
    }
    if (old) this.closeLater(old, 2_000 + Math.random() * 20_000);
  }

  private open(slot: number): OpenFile {
    const startMs = slot * ROTATE_MS;
    const d = new Date(startMs).toISOString(); // 2026-11-23T16:05:00.000Z
    const dir = path.join(CONFIG.dataDir, 'raw', d.slice(0, 10), d.slice(11, 13));
    fs.mkdirSync(dir, { recursive: true });
    let p = path.join(dir, `${this.name}.${startMs}.${this.ext}.zst`);
    if (fs.existsSync(p)) p = path.join(dir, `${this.name}.${Date.now()}.${this.ext}.zst`); // restarted within the slot
    const proc = spawn('nice', ['-n', ZSTD_NICE, 'zstd', '-q', `-${CONFIG.zstdLevel}`, '-f', '-o', p], { stdio: ['pipe', 'ignore', 'pipe'] });
    proc.stderr.on('data', (b: Buffer) => console.error(`[sink] zstd ${this.name}: ${b}`));
    proc.on('error', (e) => console.error(`[sink] zstd spawn failed for ${this.name}:`, e.message));
    proc.stdin.on('error', (e) => console.error(`[sink] write failed for ${this.name}:`, e.message));
    if (this.header) proc.stdin.write(this.header.join('\t') + '\n');
    return { slot, proc, path: p };
  }

  private closeLater(f: OpenFile, delayMs: number): void {
    if (this.closing.has(f)) return;
    const done = new Promise<void>((resolve) => {
      f.proc.on('close', () => resolve());
      setTimeout(() => f.proc.stdin.writableEnded || f.proc.stdin.end(), delayMs);
    });
    this.closing.set(f, done);
    done.then(() => this.closing.delete(f));
  }

  /** Bytes buffered in memory waiting for zstd (backpressure indicator). */
  pending(): number {
    return this.file ? this.file.proc.stdin.writableLength : 0;
  }

  /** Shutdown: close everything now and wait for zstd to finish each file. */
  async close(): Promise<void> {
    for (const f of [this.file, this.next]) if (f) this.closeLater(f, 0);
    this.file = this.next = null;
    for (const f of this.closing.keys()) if (!f.proc.stdin.writableEnded) f.proc.stdin.end(); // don't wait for delayed closes
    await Promise.all([...this.closing.values()]);
  }
}

const streams = new Map<string, Stream>();

setInterval(() => {
  const now = Date.now();
  for (const s of streams.values()) s.tick(now);
}, 1_000).unref();

/** Get or create a TSV stream. Values must not contain tabs or newlines. */
export function tsv(name: string, header: string[]): (values: (string | number | boolean | null | undefined)[], nowMs: number) => void {
  let s = streams.get(name);
  if (!s) {
    s = new Stream(name, 'tsv', ['recv_ms', ...header]);
    streams.set(name, s);
  }
  const stream = s;
  return (values, nowMs) => {
    stream.write(nowMs + '\t' + values.map((v) => (v === null || v === undefined ? '' : String(v))).join('\t'), nowMs);
  };
}

/** Get or create a JSON-lines stream for nested or rarely-seen payloads. */
export function jsonl(name: string): (obj: unknown, nowMs: number) => void {
  let s = streams.get(name);
  if (!s) {
    s = new Stream(name, 'jsonl', null);
    streams.set(name, s);
  }
  const stream = s;
  return (obj, nowMs) => stream.write(JSON.stringify({ recv_ms: nowMs, d: obj }), nowMs);
}

/** Snapshot of rows written per stream since the last call (and reset). */
export function drainCounters(): { stream: string; rows: number; bytes: number; pending: number }[] {
  const out = [];
  for (const s of streams.values()) {
    out.push({ stream: s.name, rows: s.rows, bytes: s.bytes, pending: s.pending() });
    s.rows = 0;
    s.bytes = 0;
  }
  return out;
}

export async function closeAll(): Promise<void> {
  await Promise.all([...streams.values()].map((s) => s.close()));
}

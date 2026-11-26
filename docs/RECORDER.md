# Market Data Recorder

Records everything we can get about Polymarket's recurring crypto **Up/Down** markets and the
underlying coins on Binance, so strategies can be researched and backtested on our own data.

Code: `src/recorder/` · Verification: `scripts/recorder/verify.py` · Runs on the droplet as the
`polytrader-recorder` systemd service.

## What is recorded

| Source | Coverage |
|---|---|
| Polymarket CLOB WebSocket | 37 series: BTC, ETH, SOL, XRP, DOGE, BNB, HYPE, ZEC × 5m / 15m / hourly / 4h / daily (not every coin has every timeframe). Each market from 2 windows (min 10 min) before it opens until 3 min after it closes. |
| Polymarket Gamma API | Full market/event metadata at discovery and at resolution |
| Polymarket Data API v2 | Every trade of every resolved market, **with trader wallet** |
| Polymarket RTDS | Chainlink prices (the resolution source) and Polymarket's Binance relay, ~1/s per coin |
| Binance spot | Every trade, every top-of-book change, 100 ms depth diffs; 1000-level snapshot each minute |
| Binance USDⓈ-M perps | Every agg trade, every top-of-book change, 100 ms depth diffs, mark/index/funding each second, all-market liquidations; 1000-level snapshot each minute; open interest each minute; long/short + taker ratios every 5 min |

## Files

`/opt/polytrader/data/raw/YYYY-MM-DD/HH/<stream>.<fileStartMs>.<tsv|jsonl>.zst`

- One file per stream per **5 minutes** (UTC), named by slot start (since 2026-11-24 10:36; earlier
  files are named by the time they were opened). A file only becomes readable once closed. Files
  are pre-started at a random moment in the last minute of the previous slot and closed 2–22 s
  after their slot ends, so streams don't all rotate at the same instant.
- zstd level 9. TSV files have a header row; every row starts with `recv_ms` (our receive time,
  ms since epoch, clock synced with chrony). JSONL rows are `{"recv_ms": ..., "d": <payload>}`.
- For analysis use `polydata` ([analysis/README.md](../analysis/README.md)), which syncs these files,
  converts them to Parquet and exposes them as DuckDB views. Raw files can also be read directly:
  `SELECT * FROM read_csv('raw/2026-11-23/*/bn_fut_bbo.*.tsv.zst', delim='\t', header=true)`

### Polymarket

`asset` = `<market slug>|U` (Up token) or `|D` (Down token). Slugs end with the window start in
Unix seconds, e.g. `btc-updown-5m-1795452900` (5m, 15m, 1h, 4h, 1d).

| Stream | Columns / content |
|---|---|
| `pm_book` | ts_ms, asset, bids, asks (`price:size\|price:size…`), hash — full book snapshot (on subscribe and after trades) |
| `pm_price_change` | ts_ms, asset, side, price, size, best_bid, best_ask — every level change (size = new total at that price, 0 = removed) |
| `pm_best_bid_ask` | ts_ms, asset, best_bid, best_ask, spread |
| `pm_trade` | ts_ms, asset, side, price, size, fee_rate_bps, tx_hash |
| `pm_market_meta` | Gamma event JSON at discovery (token ids, fees, tick size, config) |
| `pm_resolution` | Gamma event JSON at resolution + outcome |
| `pm_api_trades` | Data API v2 trades incl. `proxy_wallet`, pseudonym, tx hash |
| `pm_new_market` | Every `new_market` broadcast (all of Polymarket, not just our series) |
| `pm_other` | Anything else, e.g. `tick_size_change`, `market_resolved` |
| `rtds_price` | topic (`crypto_prices_chainlink` / `crypto_prices`), symbol, ts_ms, value, full_accuracy_value, server_ts_ms, conn (`a`/`b`; added 2026-11-24 — two redundant connections, deduplicated by timestamp; read with `union_by_name=true`) |

### Binance

| Stream | Columns |
|---|---|
| `bn_spot_trade` | sym, event_ms, trade_ms, trade_id, price, qty, buyer_is_maker |
| `bn_spot_bbo` | sym, update_id, bid, bid_qty, ask, ask_qty |
| `bn_spot_depth` | sym, event_ms, first_update_id, last_update_id, bids, asks |
| `bn_fut_trade` | sym, event_ms, trade_ms, agg_id, first_id, last_id, price, qty, buyer_is_maker |
| `bn_fut_bbo` | sym, update_id, event_ms, txn_ms, bid, bid_qty, ask, ask_qty |
| `bn_fut_depth` | sym, event_ms, txn_ms, first_update_id, last_update_id, prev_update_id, bids, asks |
| `bn_fut_mark` | sym, event_ms, mark_price, index_price, est_settle_price, funding_rate, next_funding_ms |
| `bn_fut_liquidation` | sym, event_ms, trade_ms, side, order_type, tif, qty, price, avg_price, status, last_filled_qty, filled_qty (all symbols) |
| `bn_depth_snapshot` | JSON: market, sym, lastUpdateId, 1000 bids/asks — rebuild books from snapshot + depth diffs |
| `bn_fut_stats` | JSON: open interest, long/short account & position ratios, taker buy/sell ratio, OI history |

## Archive (DigitalOcean Spaces)

`scripts/recorder/archive.sh`, run hourly at :07 by `polytrader-archive.timer` (as `polytrader`):

- Uploads every closed data file (older than 15 min) to `spaces:polytrader-recorder-sgp1/raw/...`
  (same layout as local; rclone verifies each upload's MD5).
- Day folders older than 3 days (`ARCHIVE_KEEP_DAYS`) are deleted locally **only after** `rclone
  check` confirms the bucket copy is identical.
- Once a day exports `rec_markets`, `rec_events`, `rec_stats` as CSV to `catalog/YYYY-MM-DD/`.
- Logs `archive_ok` / `archive_check_failed` / `archive_error` to `rec_events`.
- Bucket is private (anonymous access returns 403). Credentials: `SPACES_*` in `/opt/polytrader/.env`.

## Postgres tables (catalog and health, not tick data)

- `rec_markets` — one row per market: series, coin, token ids, window, fee schedule, subscribe /
  unsubscribe times, `price_to_beat`, `outcome`, `resolved_at`, `api_trades` count.
- `rec_stats` — rows and bytes written per stream per minute (gaps show up as missing/low minutes).
- `rec_events` — reconnects, sequence gaps, REST errors, starts/stops.

## Things we learned (verified 2026-11-23)

- **Resolution:** markets resolve on Chainlink's 60-second **TWAP** stream, not the spot price.
  `priceToBeat` = mean of Chainlink prices over the 60 s before the window opens (our RTDS data
  reproduces it to ~0.2 bp median; the last spot price is off by ~2.5 bp). The outcome is Up if the
  closing TWAP ≥ priceToBeat.
- **4h markets have no `priceToBeat` in Gamma** (`eventMetadata` is null even after resolution);
  reconstruct it from `rtds_price` the same way.
- **Fees:** taker only, `shares × rate × p(1 − p)`; rate 0.05 on BTC 5m/15m, 0.01 on all other
  Up/Down markets. Verified on-chain; see [FEES.md](FEES.md) (rebates need $200k+ monthly volume).
- **Binance futures WebSocket is split:** `/public` serves bookTicker/depth, `/market` serves
  aggTrade/markPrice/forceOrder. The legacy `fstream.binance.com/stream` URL silently drops the
  `/market` streams.
- **Polymarket drops a single connection carrying ~200 tokens** ("1013 slow consumer: send buffer
  full"); the recorder uses one connection per series.
- Polymarket Data API v1 is retired; v2 `/v2/trades?condition_id=…` paginates with `next_cursor`.
- Volume: ~10 GB/day compressed at level 9; biggest streams are Binance futures top-of-book,
  Polymarket price changes and Binance futures depth.

## Data quality log

Use this when backtesting across these periods. All times UTC.

| Period | Issue | Impact | Status |
|---|---|---|---|
| 2026-11-23 16:50–17:46 | Development: 4 restarts, sharding/compression changes | Gaps of seconds to minutes; treat as test data | — |
| Every restart before 2026-11-24 10:13 | systemd killed zstd with the recorder, truncating the open file of every stream | Up to ~2.5 min lost per stream per restart (last at 10:10:00–10:12:39 on 11-24). 57 truncated files repaired (complete rows kept), 16 empty files removed. Resolutions and Data-API trades re-fetched (13 + 1,366 rows, `"backfill": true`) | Fixed: `KillMode=mixed` |
| 2026-11-23 17:21 – 2026-11-24 10:36 | All streams rotated files at once; the event loop stalled 1.5–4 s at almost every 5-min boundary | **No data lost** (sequence numbers continuous, 1 row/s streams complete), but `recv_ms` is late by up to ~4 s within ~5 s after each :x0/:x5 boundary. Use exchange timestamps there, not `recv_ms` | Fixed: staggered rotation, zstd at lower priority. Lag p99 now ~45 ms |
| 2026-11-23 17:46 – 2026-11-24 10:12 | Single RTDS connection stalled 59× (30 s detection) | Chainlink gaps 1.5% of time; 48 of 2,181 priceToBeat windows incomplete (Gamma's priceToBeat still available for them) | Fixed: 2 redundant connections, 5 s detection. Upstream gaps (both connections, e.g. 8 s at 11-24 10:45:07) remain |
| same | Polymarket CLOB: 4× "1013 slow consumer" (BTC 5m/15m), a few 60 s stale timeouts | Missing book updates and trades for those series for up to ~60 s (e.g. eth-updown-15m 06:45 window: 222 of 277 trades via WS; Data API has all) | Mitigated: per-market connections for BTC 5m/15m and ETH 5m, 25 s stale timeout |
| Every restart before 2026-11-24 14:16 | Markets that ended shortly before a restart were forgotten: no outcome, no Data API trades (66 unresolved, 16 without trades) | Outcomes and trades fetched late on 2026-11-24 14:16–14:20 (`pm_resolution` / `pm_api_trades` rows with late `recv_ms`); no data lost | Fixed: on startup the recorder resumes ended markets missing outcome/trades (7 days back) |
| Parquet built before build format 2 (analysis side, all dates) | `polydata build` sorted each hour by `(asset, recv_ms)` with an unstable sort, so rows sharing `(asset, recv_ms)` lost their arrival order (~38% of `pm_price_change` rows moved; same-ms changes to one level could apply in the wrong order) | Raw TSV files are unaffected. Order same-ms rows by `seq` | Fixed: Parquet format 2 adds `seq` (raw arrival order) and sorts by `(asset, recv_ms, seq)`; run `polydata build` to rebuild old hours |
| Before 2026-11-24 10:36 (backtester) | Late `recv_ms` at 5-minute boundaries (see above) and backfilled resolutions with late `recv_ms` | `polydata.bt` never replays data before 2026-11-24 10:36 UTC. A backfilled resolution delays the simulated payout cash (credited `redeem_delay_s` after its `recv_ms`); see [BACKTESTER.md](BACKTESTER.md#known-limitations) | Handled in the backtester |
| 4h/daily markets discovered in the same tick they were subscribed (until 2026-11-25 19:23) | The `rec_markets` INSERT and the `subscribed_at` UPDATE ran concurrently; when the UPDATE won, `subscribed_at` stayed NULL (e.g. zec-updown-4h 09:00 and 13:00 on 11-25), and the watchdog alerted "Not recording the current market" | **No data lost**: the WebSocket subscription happened and rows were written; only the catalog column is missing | Fixed: the UPDATE waits for the INSERT; deployed 2026-11-25 19:23 UTC; the two zec rows backfilled |
| 2026-11-25 19:23:35–19:23:53 | Recorder restart to deploy the fix above | ~3 s gap on Binance/RTDS, ~18 s on Polymarket (resubscribe after discovery); markets open then are voided by the gap rules (sealed day) | — |
| 2026-11-23 17:46 – 2026-11-26 (backtester) | `bt.gaps` did not map the recorder's source names `binance-fut-bbo` (since 11-23 17:46), `polymarket-rtds-a`/`-b` (since 11-24 11:50 / 14:06) and matched `pm:<slug>` (per-market connections, since 11-24 11:40) only through the paper runner | Only the legacy `binance-fut-public` produced Binance gaps, so a `binance-fut-bbo` close/stale never voided markets; `pm:<slug>` gaps were ignored by `voided_markets`. On 11-24, the identity check (btc/sol/xrp, 7 strategy cases) was byte-identical before and after the fix: no run there changed | Fixed in `bt.gaps` (`sim_version` changed). Backtests run before this fix may have traded through such gaps on other days or coins |

No HTTP 429/418 rate limiting has occurred. One Gamma HTTP 500 (retried on the next discovery cycle).

## Alerting (Telegram)

`scripts/recorder/watchdog.py` runs every minute (`polytrader-watchdog.timer`) and messages Telegram
**only for problems that need action**. A check must fail continuously for its threshold before it
pings. Self-healing blips never alert: dropped connections that reconnect, short upstream gaps,
transient REST errors, depth resyncs and single restarts. They are counted in the daily summary
(07:00 UTC). Each alert repeats every 3 h while it lasts, and a ✅ message is sent when it clears.

| Alert | Fires when |
|---|---|
| Recorder not running | service inactive for 2 min, or ≥ 3 restarts within an hour |
| Data stopped | a core stream (Binance trades/BBO/depth/mark, Polymarket book updates, Chainlink) has no rows for 5 min, or no stats at all for 5 min |
| Feed degraded | Chainlink or mark-price rate below normal for 15 min |
| Connection stuck | a WebSocket hasn't reconnected for 5 min |
| Coverage | a series' current market isn't subscribed for 5 min |
| Rate limited | Binance IP ban (HTTP 418) at once; Binance 429s if 5+ per 10 min for 3 min. Polymarket 429s don't alert: the recorder pauses that API (Retry-After, min 10 s, default 60 s) and backs off per market (30 s → 10 min); lasting effects trigger the coverage/resolution/trades alerts |
| Format change | parse errors or unexpected outcomes (Polymarket changed something) |
| Resolutions | markets without outcome/trades more than 1 h after their end, for 30 min |
| Archive | no successful Spaces upload for 2 h 15 min |
| Host | < 10 GB disk free, clock not synchronized for 30 min, memory > 900 MB for 15 min |

Send `/status` to the bot for a report. Config: `TELEGRAM_BOT_TOKEN`, `TELEGRAM_CHAT_ID` (and
optionally `HEALTHCHECK_URL`, pinged every run, so an external service can alert if the whole droplet
dies) in `/opt/polytrader/.env`. Test with `python3 /opt/polytrader/app/watchdog.py --test`;
`--dry-run` shows what would be sent.

## Operations

```bash
ssh -i ~/.ssh/id_polytrader polytrader@<droplet-ip>
sudo systemctl status polytrader-recorder          # as root
journalctl -u polytrader-recorder -f               # live log (as root)
journalctl -u polytrader-archive -n 20             # archive runs (as root)
systemctl start polytrader-archive                 # archive now (as root)
python3 /opt/polytrader/app/verify.py /opt/polytrader/data --since-minutes 30
```

systemd units: `scripts/recorder/systemd/` (copy to `/etc/systemd/system/`).

Deploy: `npm run build`, rsync `dist/recorder package.json package-lock.json` to
`/opt/polytrader/app/`, `npm ci --omit=dev` if dependencies changed, restart the service.

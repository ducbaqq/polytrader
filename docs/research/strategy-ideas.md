# Strategy idea catalogue (2026-11-26)

Research only: web sources, repo docs, two live public API reads (`gamma-api`, `clob.polymarket.com/clob-markets`)
and one 1.4 s `polydata sql` query. Nothing was backtested. The ideas are ranked by how plausible they are and how
cheaply we can test them. They are not ranked by expected profit, because nobody knows that yet.

## 1. Sources and the facts that matter

**Sources consulted**
- Polymarket docs: [order lifecycle (taker delay, order types)](https://docs.polymarket.com/concepts/order-lifecycle.md),
  [liquidity rewards](https://docs.polymarket.com/market-makers/liquidity-rewards),
  [fees / Nov-2026 fee & rebate changes](https://docs.polymarket.com/programs/fee-and-rebate-changes-november-2026.md),
  [maker rebates](https://docs.polymarket.com/programs/maker-rebates),
  [holding rewards](https://docs.polymarket.com/polymarket-learn/trading/holding-rewards)
- Live market config: `GET clob.polymarket.com/clob-markets/{condition_id}` and Gamma `/markets?slug=` for BTC/ETH 5m,
  BTC/SOL 15m and BTC 1h (2026-11-26), plus `clob.polymarket.com/rewards/markets/{condition_id}`
- Settlement: [Polymart, TWAP resolution](https://polymart.app/blog/polymarket-twap-resolution),
  [CryptoSlate, TWAP reform and Binance end-of-window spikes](https://cryptoslate.com/polymarket-reforms-bitcoin-contracts-and-binance-trading-spikes-immediately-drop/),
  [dev.to, reconstructing the price to beat](https://dev.to/bluewhale-quant-lab/how-polymarket-decides-up-or-down-reconstructing-the-price-to-beat-129d)
  (its pre-TWAP Binance-candle recipe for 1h/1d is **out of date**: live 1h markets name the Chainlink 60 s TWAP stream)
- Chainlink: [Data Streams v3 schema (consensus median mid, LWBA bid/ask)](https://docs.chain.link/data-streams/reference/report-schema-v3),
  [LWBA](https://docs.chain.link/data-streams/concepts/liquidity-weighted-prices)
- Academic: [Le 2026, calibration of 292M Kalshi/Polymarket trades](https://arxiv.org/html/2602.19520v1);
  [Kim & Hansen 2026, quarter-hour effect in crypto perps](https://www.alphaxiv.org/abs/2607.09426);
  [Shanaev et al. 2023, turn-of-the-candle effect](https://www.ncbi.nlm.nih.gov/pmc/articles/PMC10015199/);
  [OpenMarket Polymarket–Binance dataset](https://arxiv.org/abs/2607.26245);
  [SMU/Stanford, settlement manipulation](https://cointelegraph.com/news/stanford-study-5-minute-bitcoin-prediction-markets-enable-settlement-manipulation)
- Other venues: [Kalshi crypto market rules (CF BRTI 60 s averages, 15m up/down)](https://help.kalshi.com/markets/popular-markets/crypto-markets);
  [Deribit expiries (dailies at 08:00 UTC)](https://support.deribit.com/hc/articles/25944688876957)
- Practitioners (hype discounted): [gabigol wallet reverse-engineered](https://dev.to/fatherson/reverse-engineering-gabigol-high-frequency-crypto-updown-farmer-on-polymarket-d0a),
  [gabagool two-sided bot](https://polymarketbot-site.pages.dev/top-bots/gabagool22), Dune's estimate (via search
  snippets) of 55%/62% bot participation on 5m/15m, with 6.2k bot addresses making 56M trades against 166k trades
  from 44k casual addresses

**The most decision-relevant facts**

1. **Crypto Up/Down markets have a 250 ms taker delay, and our simulator does not model it.**
   - Live `clob-markets` returns `"itode": true` for BTC 5m, ETH 5m and BTC 15m. The docs say: "Taker delay: used on
     selected crypto and finance up/down markets. The order is held for 250 ms, then validation runs again and the
     order is matched or placed on the book." A delayed order cannot be cancelled.
   - There is no mention of a taker delay anywhere in `docs/` or `analysis/src`.
   - Effect on takers: every taker result in the simulator (fv-taker, the latency trade) is **optimistic**, because a
     real fill happens about 250 ms later, against a book whose makers had time to cancel.
   - Effect on makers: a maker close to the engine (London, ~10 ms) gets a **250 ms head start** on every sniper.
     That changes how the failed market-making work should be read; see idea 1.
   - The delay also shifts the trade-print clock: a taker trade printed at t was decided at about t − 250 ms − their
     latency. Features in the adverse-flow study that are "inside 200 ms before the fill" are therefore *after* the
     taker's decision.
2. **Liquidity rewards are not active on crypto Up/Down markets.**
   - Gamma shows `rewardsMinSize 50` and `rewardsMaxSpread 4.5`, but `clobRewards` is null and
     `/rewards/markets/{id}` returns an empty list.
   - The $1M TWAP-launch liquidity program for 5m/15m/4h has ended.
   - Holding rewards (4% APY) apply only to long-dated markets (`holdingRewardsEnabled: false`).
   - Maker rebates are tiered and start at $200k of 30-day weighted volume, as `docs/FEES.md` already says. That
     assumption holds.
3. **Every timeframe, including 1h, settles on the same Chainlink TWAP stream.**
   - The live 1h, 15m and 5m market descriptions all name `data.chain.link/streams/<coin>-usd-twap-60s-streams`.
   - Up wins if the end TWAP ≥ the start TWAP. Both benchmarks are the 60 s TWAP.
   - History: 5m used a 30 s TWAP from 2026-08-07 and moved to 60 s on 2026-08-14.
   - So at any shared boundary, the 5m, 15m, 1h, 4h and daily markets are **digitals on one random variable**
     (the TWAP at that boundary) **with different strikes**. 4h windows start on ET hours and the daily window runs
     from noon ET to noon ET, so several timeframes end together at some boundaries.
4. **The Chainlink price is a multi-venue, liquidity-weighted consensus mid (median of DON nodes), not Binance.**
   - Binance is one input, which explains the 6–15 bp basis we saw.
   - Bots that read Data Streams directly see the settlement price about 1 s before our RTDS copy (we lag 1.5 s), and
     venues such as Coinbase can move it when Binance does not.
5. **The literature says calibration is good.**
   - Kalshi crypto contracts have slopes of 0.99–1.01 out to 24 h. Polymarket crypto averages 1.05, mildly
     underconfident.
   - The Polymarket–Binance OpenMarket paper: 43 microstructure features did not beat the book; repricing took a
     median of 347 ms after large Binance moves; spreads are one tick.
   - Both match our own failures. Taker "fair value" alone is a dead end; edges must come from structure, from where
     we sit, or from the markets nobody watches.
6. **Clock effects exist but are small.**
   - In the first 10 s after quarter-hour boundaries, Binance perps show +26% trades, +32% dollar volume and +26%
     absolute returns.
   - Predictable opening returns are worth about 0.5 bp per boundary, roughly a tenth of a taker fee.
   - Turn-of-candle minutes (0/15/30/45) carry a positive drift of about 0.58 bp per minute in older data.
   - This matters for vol and calibration by phase, but probably not as a standalone directional bet.
7. **The volume is in the short timeframes.** Traded notional on 2026-11-24 from our data:

   | Timeframe | Notional | Trades |
   |---|---|---|
   | 5m | $7.8M | 592k |
   | 15m | $1.6M | 106k |
   | 1h | $298k | 20k |
   | 4h | $54k | 3k |
   | 1d | $27k | 0.9k |

   Ideas that target 1h/4h/daily markets have small capacity, maybe tens of dollars a day each, but probably face
   less competition.
8. **Tick size is dynamic.** Near the extremes, a 5m market moved to a 0.001 tick (`mts: 0.001`) while Gamma still
   reported 0.01. Settlement-lock's "nothing below 0.99" should be re-checked at a 0.001 tick (0.991–0.999).
9. **What practitioners actually do.** The documented profitable wallet, "gabigol" (about $308k PnL on $20.6M
   volume, i.e. 1.5%, as the article reports it):
   - takes favourites at 94–99¢ in the last 2 minutes;
   - buys 3–30¢ longshots;
   - almost never sells.

   Two-sided "buy both below $1" bots are temporal pair accumulation, which is economically the same as symmetric
   market making (bid Down = ask Up). Their edge is the maker spread minus adverse selection, the same thing we
   measured.

## 2. Ideas

Notation:
- K = price to beat (strike).
- TWAP_T = the 60 s Chainlink TWAP at boundary T.
- "Screen" = a quick SQL or `screen.py` check.
- "Sim" = the event backtester.

### 1. London maker with a taker-delay head start (re-run market making under the real 250 ms delay)
- **Mechanism:**
  - mm-binance-pull lost 1.6¢/sh to adverse selection, and about 2/3 of the adverse fills fell inside our 200 ms
    cancel latency from Singapore.
  - With a 250 ms taker delay, a sniper reacting to a Binance move still has to wait 250 ms. A maker in London that
    cancels on the same move lands its cancel in about 10–30 ms after seeing it.
  - Both parties see Binance at about the same time from London, so the maker wins every race that starts at the same
    observation.
  - Who pays: the latency snipers, who now get only stale quotes from slow makers.
- **Multi-market:** yes. Quote all coins and timeframes; one Binance trigger pulls every quote on that coin, and on
  every alt when BTC moves.
- **Data:** recorded. Sim work needed:
  - add a `taker_delay_ms=250` parameter, so a recorded taker trade is treated as submitted 250 ms before its print;
  - check whether Polymarket's trade timestamps are the match time;
  - add a London latency profile (cancel 10–30 ms; Binance feed via London, about +120 ms from Tokyo).
- **First check (screen):** for every adverse maker fill on the train day, take the Binance move that preceded the
  print. Find the first moment a London observer would have seen it (our Binance recv time plus the London−Singapore
  offset). Count the fills where that moment is more than about 30 ms before (print − 0) and after (print − 250 ms).
  Those fills are avoidable only thanks to the delay.
  - If most adverse fills are avoidable, re-run mm-binance-pull in the sim with delay-aware fills.
- **Edge / capacity:** if half of the −1.6¢ adverse is avoided while keeping +0.6¢ spread, MM turns around +0¢ to
  +0.5¢/sh. At $1–2M/day of BTC/ETH 5m/15m maker volume that is $50–500/day. Speculative.
- **Risks:**
  - The delay has existed since around June 2026, so fast London makers already compete for this, which is why spreads
    are one tick.
  - "H2 adverse flow isn't preceded by Binance moves" (mm-side-pull) suggests some adverse flow is informed by other
    feeds (idea 2).
- **Latency:** needs London. Run paper from a London VPS before believing anything.

### 2. Multi-venue Chainlink nowcast (find the feed the adverse flow is using)
- **Mechanism:**
  - Chainlink is a consensus mid across top venues. Adverse flow that Binance doesn't explain may be driven by
    Coinbase, OKX or Bybit moves, or by direct Data Streams access (about 1 s ahead of our RTDS copy).
  - A composite mid of several venues should nowcast the settlement TWAP better than Binance plus our lagged
    Chainlink.
- **Who's on the other side:** stale makers (for a taker version) or nobody (as a defensive input to idea 1).
- **Multi-market:** yes, all markets of a coin share the nowcast.
- **Data:** missing. We need Coinbase (and ideally OKX/Bybit) BTC/ETH/SOL top of book recorded.
- **First check, with existing data and research only:**
  - Re-label the adverse fills from the adverse-flow study with the Chainlink value at its *observation* timestamp
    (`ts_ms`), not its receive time. This look-ahead is acceptable for diagnosis, not for trading.
  - If the Chainlink observation moved toward the taker in the ~1 s before the print significantly more often than
    Binance did, the informed flow is Chainlink-sourced.
  - Then decide between paying for Data Streams and recording Coinbase.
- **Edge:** defensive (fixes idea 1). As a taker it is probably priced, because the bots that have the feed are the
  ones we lose to.
- **Risks:** Data Streams access is permissioned and paid. A Coinbase feed from Singapore is slow.
- **Latency:** yes.

### 3. Strike-strip relative value: price the slow markets off the fast one at shared boundaries
- **Mechanism:**
  - At a boundary T where several timeframes end (every :00 has 5m, 15m and 1h; 4h and daily boundaries add more),
    each market is a digital `P(TWAP_T ≥ K_i)` on the same variable with a different strike K_i.
  - The 5m/15m books are liquid and bot-dominated, so their mids imply a precise distribution for TWAP_T.
  - With the 5m market's strike and price, one σ (implied from it, scaled with the TWAP shrinkage for the time left)
    prices the 1h/4h/daily digital at its own strike.
  - The 1h/4h/daily books are thin ($27k–$298k/day) and probably quoted by fewer, slower participants.
  - xtf-arb only checked the riskless monotonicity bound (0 violations). This idea is the statistical version: use
    the liquid market's implied vol, not realized vol (fv-taker used realized vol).
- **Who's on the other side:** slow makers and retail in 1h/4h/daily.
- **Multi-market:** yes. 5m/15m → 1h/4h/daily of the same coin.
- **Data:** recorded (books for all timeframes, K from Chainlink or Gamma; 4h K needs our own TWAP because Gamma has
  none).
- **First check (screen):**
  - In the last 15 min before each :00, compute `fair_1h = Φ((Φ⁻¹(mid_5m)·σ_eff + (K_5m − K_1h)/S) / σ_eff)`, where
    σ_eff is the remaining TWAP vol.
  - Measure (1h mid − fair_1h). Then run a taker screen at a 1¢ edge threshold. Fee rate 0.01 on 1h, so 0.25¢ at
    0.5: BTC 1h is cheap to take.
  - Train day, both halves.
- **Edge / capacity:** a few ¢/sh where it exists. Capacity is limited by 1h depth, maybe $20–100/day across 8 coins.
- **Risks:** 1h quotes may already be driven by the same bots off Binance. Measure staleness first: the lag of the
  1h mid behind the 5m mid in seconds.
- **Latency:** moderate. Seconds-scale staleness would not need London.

### 4. Endgame-maker generalized to 15m/1h/4h/daily, with the 5m as a guard
- **Mechanism:**
  - With 60 s TWAP settlement, the remaining uncertainty at τ < 60 s left has variance σ²τ³/(3·60²). It shrinks
    much faster than σ²τ, so in the last minute the true probability goes to 0/1 faster than a √τ model says.
  - That fits the measured late underconfidence on 5m (slope 1.11–1.28). Every timeframe uses the same TWAP, so the
    same physics applies to the 15m/1h/4h/daily finals, which are less watched.
  - Guard: the same-boundary 5m market's mid (idea 3) cancels bids when the fast market disagrees.
  - Who loses: impatient sellers of the favourite, and retail longshot buyers (gabigol's lottery leg is the other
    side).
- **Multi-market:** yes. All coins × all timeframes at each boundary. Portfolio-level cap per boundary, because
  same-boundary outcomes across coins are correlated.
- **Data:** recorded.
- **First check (screen):** calibration slope at 10–60 s left for 15m/1h/4h/1d separately (pooled coins), using the
  calibration-study code. If the slope is above 1 outside 5m, run the frozen endgame-maker-v2 config with
  `timeframes=[15m,1h]` as a *new* pre-registered variant.
- **Edge / capacity:** 5m H2 was +4.3¢/sh with a CI spanning 0. Adding timeframes adds fills more than edge, so maybe
  +2¢/sh on a few thousand shares a day, about $30–100/day.
- **Risks:**
  - Fills are adverse at the end (a favourite sold to us just before it flips).
  - The 1h/4h/1d samples are tiny: about 24 4h and 8 daily markets per day across coins.
- **Latency:** the 400 ms sim still made +2.3¢, so London is helpful but not required.

### 5. Opening-seconds staleness (next window's quotes when its strike gets fixed)
- **Mechanism:**
  - Every market is listed about 1 day ahead and trades before its window starts.
  - K = TWAP over the last 60 s before the start, so during that minute K becomes nearly known while spot can be
    away from it. At the start, Up already has a head start of about half of the last minute's drift (the TWAP lags
    spot).
  - Resting pre-open quotes at about 0.50 can be stale at the boundary, the busiest moment, when bots are occupied
    settling and repricing the window that just ended.
- **Who's on the other side:** passive pre-open makers and retail "next window" bettors.
- **Multi-market:** yes. Window n's final TWAP equals window n+1's K, so the same computation serves both, across 8
  coins at once.
- **Data:** recorded (books from 2 windows before open, Chainlink, Binance).
- **First check (screen):** for start − 5 s … start + 10 s, compute `fair = Φ((S_now − K_partial)/(σ·√T_eff))`.
  Measure how often best ask < fair − (fee + 1¢), and the 10 s markout. Check whether fv-taker excluded this phase;
  if it did not, this is already covered and dead.
- **Edge / capacity:** probably small, because the bots that make the 5m market do this computation. Capacity is the
  top-of-book size at the boundary.
- **Risks:** priced. With the taker delay, a 250 ms fill lag makes us the slow one.
- **Latency:** yes.

### 6. Regime-conditional calibration (hour of day, quarter-hour phase, vol regime, macro prints)
- **Mechanism:**
  - Markets price with roughly one rolling vol. Realized vol is clock-dependent: quarter-hour bursts (+26% absolute
    returns in the first 10 s), US-open hours, and scheduled macro prints (CPI/NFP at 13:30 UTC, FOMC at 19:00 UTC).
  - Where the market's implied vol is wrong in a predictable direction, mid-window prices are systematically too
    extreme or too timid.
  - Le 2026 finds Polymarket crypto mildly underconfident overall (1.05).
- **Who's on the other side:** everyone who quotes with an unconditional vol.
- **Multi-market:** yes. Pooled across coins and timeframes, and the trades go in every market of the regime at once.
- **Data:** recorded. The macro calendar is public and needs a small static table.
- **First check (screen):** calibration slope by (a) window start minute mod 15 ∈ {0} vs {5, 10}, (b) UTC hour
  bucket, (c) realized-vol percentile, on 5m/15m, both halves. Pre-register the buckets first, because this is an
  easy place to overfit.
- **Edge / capacity:** a slope of 1.1 instead of 1.0 at mid 0.75 is about 2–3 points of probability. Exploitable
  with maker orders only. Capacity is shared with endgame-maker.
- **Risks:** 3 days of data cannot separate regimes; one day has about 4 macro prints at most. Speculative until
  about 2 weeks of data.
- **Latency:** no.

### 7. Cross-asset lead: alt markets repriced by BTC moves
- **Mechanism:** bots in SOL/XRP/DOGE/BNB/HYPE/ZEC markets likely watch their own coin's feed. When BTC jumps, alt
  spot follows within a fraction of a second to minutes (minute-scale lags for small caps; the evidence is mixed at
  tick scale). The alt Polymarket book may wait for its own coin to move.
- **Who's on the other side:** single-feed alt bots and passive alt makers.
- **Multi-market:** yes. BTC Binance → all alt markets in the same window.
- **Data:** recorded.
- **First check (screen):** after |BTC 1 s return| above the 99th percentile, regress the alt PM mid change over
  0.2–5 s on the BTC return, controlling for the alt's own Binance return over the same horizon. Do the same for the
  alt's Binance price, because if the alt Binance moves within 100 ms the PM bots already see it.
- **Edge:** likely small. The calibration study shows alt 5m books reprice to their own Binance in 200–400 ms, and
  alts follow BTC within that. Better used as a cancel trigger in idea 1 than as a taker signal.
- **Latency:** yes.

### 8. Thin-book alt maker (ZEC/HYPE/BNB/DOGE 15m–1h)
- **Mechanism:**
  - The calibration study: maker P&L was 2.0–2.65¢/sh in DOGE/BNB/HYPE versus 0.41¢ on BTC, and wide books (>10¢)
    exist.
  - A maker quoting inside a wide book, with Binance-triggered cancels and fee rate 0.01 for takers, competes with
    few bots.
  - Who pays: retail takers, who are a larger share of flow in thin coins.
- **Multi-market:** yes, one quoting engine across about 20 thin markets.
- **Data:** recorded.
- **First check (sim):** mm-binance-pull restricted to non-BTC/ETH coins and 15m/1h, on both halves, with the
  taker-delay fix from idea 1. Report per-coin markout.
- **Edge / capacity:** about 1–2¢/sh on low volume (perhaps $50–200k/day notional across these), so $10–40/day.
- **Risks:**
  - Wide books are wide because informed flow is lumpy; the study found buying favourites there lost 10–23¢/sh.
  - Inventory risk.
- **Latency:** London helpful.

### 9. Deribit-implied vol for 4h and daily markets
- **Mechanism:**
  - For 4h and noon-ET-to-noon-ET daily digitals, the right σ is forward-looking. Deribit short-dated ATM IV (dailies
    expire at 08:00 UTC, so interpolate) or DVOL is a better input than a 15-min realized vol.
  - Skew adds a small negative drift to P(Up).
  - Thin 4h/daily books may be priced off realized vol or not repriced at all.
- **Who's on the other side:** retail in thin long windows.
- **Multi-market:** yes, BTC/ETH only (Deribit coverage). SOL/XRP only via Deribit's smaller books.
- **Data:** missing. However, DVOL history is available from Deribit's public API (`get_volatility_index_data`), so a
  backtest is possible without a recorder change. Recording the IV of the nearest-expiry ATM options is a small
  recorder addition.
- **First check:** log loss of the 4h/1d market mid versus Φ-model(DVOL) versus Φ-model(realized vol), at 1 h and
  15 min left.
- **Edge / capacity:** unknown. Capacity is tiny ($54k + $27k/day of total notional, mostly BTC). Expect at most tens
  of dollars a day.
- **Risks:**
  - The variance risk premium makes IV overstate future vol.
  - Sample size: about 6 BTC 4h markets and 1 BTC daily per day, so years to validate.
  - Low priority.
- **Latency:** no.

### 10. Kalshi 15m cross-venue signal
- **Mechanism:**
  - Kalshi lists 15m up/down markets on BTC/ETH/SOL/XRP/BNB/DOGE that compare 60 s CF BRTI averages, a contract almost
    identical to Polymarket's except for the index (BRTI vs Chainlink).
  - Its crowd is US retail. Disagreement between the two mids, net of the expected BRTI–Chainlink basis effect, is
    an independent signal.
  - Kalshi 15m windows align with ET quarter-hours, so they share our boundaries.
- **Who's on the other side:** whichever venue lags.
- **Multi-market:** yes, cross-venue.
- **Data:** missing. Kalshi has a public market-data REST/WS. We can't (and shouldn't) trade there; it would be
  information only.
- **First check:** record a week of Kalshi BTC/ETH 15m mids. Test whether (Kalshi − Polymarket) predicts the next
  30 s Polymarket mid change or the outcome residual.
- **Edge:** likely small. Polymarket is the larger, faster venue, so it probably leads.
- **Risks:** extra recorder scope. The basis is tiny, so outcomes nearly always agree.
- **Latency:** no.

### 11. Longshot fade as a maker (sell lottery tickets late)
- **Mechanism:**
  - gabigol's flow (and Dune's casual addresses) buys 3–30¢ underdogs.
  - In the last minute on 5m, prices are too timid (the late-window slope of 1.11–1.28 from idea 4), so late longshots
    are **overpriced**.
  - Resting asks on the underdog at 3–10¢ (economically the same as bids on the favourite at 90–97¢) is the mirror of
    endgame-maker, but triggered by longshot-buying flow and placed deeper.
- **Multi-market:** yes, all coins and timeframes.
- **Data:** recorded.
- **First check:** taker buy volume and markout at prices ≤ 0.10 in the last 120 s, by coin, train day. If longshot
  buyers lose more than 1¢/sh on average, size a sim variant of endgame-maker with band 0.90–0.97.
- **Edge / capacity:** small per share (1–3¢), shared with endgame-maker.
- **Risks:**
  - It overlaps idea 4. Count it as an endgame-maker variant (it uses the variant budget).
  - Tail risk: a 5¢ ask loses 95¢ when it's wrong.
- **Latency:** no.

### 12. Clock-phase drift (turn-of-the-candle directional tilt)
- **Mechanism:**
  - Older studies find positive returns concentrated at minutes 0/15/30/45. Kim & Hansen find opening order
    imbalance after quarter-hours predicts direction (about 0.5 bp).
  - A 5m window that spans a quarter-hour turn might carry a 1–2 point Up tilt, worth something only as a free maker
    tilt.
- **Multi-market:** yes.
- **Data:** recorded.
- **First check:** P(Up) by (start minute mod 15) across all 5m windows of all coins.
- **Edge:** speculative. Testability is poor: about 860 windows per coin over 3 days gives a standard error of about
  1.7 points per bucket, so this is not testable until weeks of data. Likely already arbitraged by perp algos.
- **Latency:** no.

### 13. Basket and portfolio risk layer (not an edge, but it multiplies the others)
- **Mechanism:**
  - Same-window outcomes across coins are highly correlated: alts move with BTC.
  - Ten small edges on one boundary are about 3 effective bets, not 10. Size per boundary by correlation (Kelly on
    the covariance), and hedge one coin's directional inventory with the opposite side of the most liquid correlated
    market (BTC/ETH 15m at fee rate 0.01 for ETH).
- **Multi-market:** yes.
- **Data:** recorded.
- **First check:** the outcome correlation matrix across coins per window (5m, 15m), and the realized variance of an
  equal-weight portfolio of endgame-maker fills versus independent.
- **Why it matters:** makes ideas 4, 8 and 11 sizeable without blowing up on one BTC move.

### 14. Shorter-market resolution as information for the longer market (likely no edge)
- **Mechanism:** when the :00–:15 15m market resolves, the 1h market learns nothing that isn't in spot. The resolved
  TWAP is the spot average. A mispricing could exist only if 1h quotes don't move until "news" (a resolution event)
  arrives.
- **First check:** event study of the 1h mid around each 15m resolution timestamp versus around random times.
- **Expect:** no effect. Listed so it isn't re-proposed.

### 15. Settlement-window flow (watch only)
- **Mechanism:** under the old single-print settlement, Binance volume in the final 10 s rose to 5.68% (from 3.05%).
  With the 60 s TWAP it fell back to 3.02%. A manipulator now needs sustained pressure over 60 s, which is
  detectable early in that minute in both the Binance tape and Polymarket flow.
- **First check:** signed Binance taker flow in the last 60 s versus (TWAP_end − K), conditioned on large Polymarket
  positions taken just before. Mostly a risk study for endgame-maker.
- **Edge:** unlikely to be tradeable; documented manipulation has largely gone.

## 3. Ranked top 5 to test first

1. **Model the 250 ms taker delay, then re-test market making from London (idea 1).** This is a verified mechanic
   our simulator ignores. It makes every taker result optimistic and could flip the main market-making failure.
   Cheap screen first, then a sim change (it changes `sim_version`, so re-run `bt validate`).
2. **Strike-strip relative value, pricing 1h/4h/daily off the 5m at shared boundaries (idea 3).** Structural,
   multi-market, uses the liquid market's implied vol rather than realized vol, and the 1h fee is only 0.25¢. Not
   tested before (xtf only checked the riskless bound). Testable today with recorded books.
3. **Endgame-maker generalized to 15m/1h/4h/daily with the 5m guard (idea 4).** It extends our only candidate using
   the TWAP-shrinkage mechanism, which holds for every timeframe. The first check is a calibration slope per
   timeframe, which costs minutes.
4. **Diagnose the source of adverse flow with Chainlink observation timestamps (idea 2).** One look-ahead-only
   diagnostic tells us whether to buy or record a faster feed (Coinbase or Data Streams) before more market-making
   work.
5. **Opening-seconds staleness at the strike fix (idea 5).** Cheap screen on recorded boundary books, and a
   mechanism that fv-taker may not have covered. Most likely already priced, so kill it fast if the screen is flat.

Not recommended now: idea 9 (sample too small), idea 10 (new recorder scope), idea 12 (untestable on 3 days), and
idea 14 (expected null).

## Addendum (controller, 2026-11-26): review of an external "strategy idea pack" PDF (Grok)
- Its settlement table is outdated. Checked live: 1h, daily and HYPE markets all resolve on the Chainlink 60 s TWAP stream (Gamma descriptions), not Binance candles.
- Its taker delay says 150 ms. Polymarket's live docs say 250 ms (`itode`). Keep it a parameter.
- Confirmed and new: the RTDS feed is legacy. PolyBolt `price.crypto.twap` streams the exact settlement TWAP; from 2026-11-30 Chainlink-served subscriptions need a Chainlink API key + CLOB credentials (user decision).
- New ideas worth adding to the test list: binary-implied vol surface across coins and windows (trade outliers vs siblings and vs our sigma forecast); BTC-shock → alt stale-quote event study at +150/+500/+2000 ms; time-of-day sigma seasonality as the cheapest model improvement; Kalshi 15m/hourly as a second-opinion signal; open-gap (stale strike) check; n_eff sizing across correlated coins (8 coins at rho≈0.8 ≈ 1.2 independent bets).
- Useful method notes: sample-size table (a 2 c edge needs ~4,900 independent bets), deflated Sharpe using our attempt log as N, clustering by window start and day, staking systems only reshape risk (Martingale as a negative control).
- Already tested (ours): ladder arbitrage (xtf-arb: real but tiny), complete-set arb, wallet copying.

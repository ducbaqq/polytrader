"""Gamma confirmation / fallback and Data API trades (spec "Settlement", "REST"), as the recorder's `resolveEnded`.

`Confirmer.poll()` (the runner calls it every 30 s):

- From `end + 30 s` it asks Gamma (`/events?slug=`) for each admitted market until answered: the market's first market
  is `closed` and `outcomePrices` has a 1 (`resolveEnded`'s rule). Each answer goes to `paper/gamma_resolution`
  `{slug, outcome, recv_ms, event}` (not a recorder stream, so `build` ignores it) and to `answers`.
- If the market has no Chainlink outcome (the settler gave up, or has not decided) and the fallback is still open, the
  answer is also a `pm_resolution` row `{slug, outcome, event, source: "paper-gamma"}` through `ingest.on_rows`, stamped
  at the answer's receive time; the router applies it on the strategy clock like any resolution and the session
  replay settles at the same time. The settler is told to give the market up (`give_up`), so the session never holds a
  second resolution row. The runner closes the fallback (`close_fallback`) when the drain ends: a row written after
  that would make the replay settle a market the live run left unresolved.
- From `end + 120 s` it downloads `{data}/v2/trades?condition_id=...&limit=1000&cursor=...`; the pages are buffered
  (with their cursor, so a failed page is retried from there) and the rows written, as `pm_api_trades` `{slug,
  **trade}` in one `ingest.on_rows` call, only when the download is complete.
- A market is given up on after `end + confirm_wait` (one more poll interval of grace if the API stayed paused).
  Errors back off per market and API (30 s, 60 s ... capped at 10 min, `rest.backoff_ms`) and are logged as
  `rest_error` events; a 429 pauses the API in `Rest` and is not counted as a failure.
"""

from __future__ import annotations

import json
import urllib.parse
from collections.abc import Callable
from dataclasses import dataclass, field

from polydata.paper.feeds.rest import DATA_URL, GAMMA_URL, RateLimited, Rest, backoff_ms
from polydata.paper.ingest import Ingest
from polydata.paper.settle import ChainlinkSettler

POLL_MS = 30_000
GAMMA_AFTER_MS = 30_000
TRADES_AFTER_MS = 120_000
MAX_PAGES = 200  # as the recorder's fetchTrades
SOURCE = "paper-gamma"


def gamma_outcome(events) -> str | None:
    """'Up' / 'Down' from a Gamma `/events?slug=` answer once the market closed with a 1 in `outcomePrices`
    (`resolveEnded`'s rule), else None."""
    if not isinstance(events, list) or not events or not isinstance(events[0], dict):
        return None
    mkts = events[0].get("markets") or [None]
    mm = mkts[0]
    if not isinstance(mm, dict) or not mm.get("closed"):
        return None
    try:
        prices = [float(x) for x in json.loads(mm.get("outcomePrices") or "[]")]
    except (TypeError, ValueError):
        return None
    if len(prices) > 0 and prices[0] == 1:
        return "Up"
    if len(prices) > 1 and prices[1] == 1:
        return "Down"
    return None


@dataclass
class _Mk:
    gamma_fails: int = 0
    gamma_next: int = 0
    gamma_abandoned: bool = False
    trades_fails: int = 0
    trades_next: int = 0
    trades_done: bool = False
    trades_abandoned: bool = False
    pages: list = field(default_factory=list)  # rows of the pages downloaded so far
    cursor: str | None = None
    n_pages: int = 0


class Confirmer:
    def __init__(self, rest: Rest, ingest: Ingest, settler: ChainlinkSettler, clock: Callable[[], int], *,
                 confirm_wait_ms: int = 900_000):
        self.rest, self.ingest, self.settler, self.clock = rest, ingest, settler, clock
        self.confirm_wait_ms = int(confirm_wait_ms)
        self.answers: dict[str, dict] = {}  # slug -> {slug, outcome, recv_ms, event}
        self.fallbacks: set[str] = set()  # slugs whose pm_resolution row came from Gamma
        self.api_trades: dict[str, int] = {}  # slug -> rows written
        self.fallback_open = True
        self._state: dict[str, _Mk] = {}
        self._busy = False

    # ------------------------------------------------------------------------------------------- queries
    def close_fallback(self) -> None:
        """No more `pm_resolution` rows from Gamma (the drain ended)."""
        self.fallback_open = False

    def holds(self, slug: str) -> bool:
        """The drain may wait for `slug`: Gamma can still give it an outcome (not yet asked successfully, not given up
        on), or its Gamma resolution row is in and the router has not applied it yet."""
        st = self._state.get(slug)
        if st is None:
            return slug in self.settler.infos and self.fallback_open
        if slug in self.fallbacks:
            return True
        return self.fallback_open and slug not in self.answers and not st.gamma_abandoned

    def outstanding(self) -> bool:
        """Anything left to ask: a Gamma answer or a trades download not yet done or given up."""
        for slug in self.settler.infos:
            st = self._state.get(slug)
            if st is None or (slug not in self.answers and not st.gamma_abandoned) \
                    or (not st.trades_done and not st.trades_abandoned):
                return True
        return False

    # ------------------------------------------------------------------------------------------- polling
    async def poll(self) -> None:
        if self._busy:
            return
        self._busy = True
        try:
            for slug, info in list(self.settler.infos.items()):
                end = int(info.window_end_ms)
                st = self._state.setdefault(slug, _Mk())
                now = self.clock()
                if now < end + GAMMA_AFTER_MS:
                    continue
                if slug not in self.answers and not st.gamma_abandoned:
                    await self._gamma(slug, info, st, end)
                now = self.clock()
                if now >= end + TRADES_AFTER_MS and not st.trades_done and not st.trades_abandoned:
                    await self._trades(slug, info, st, end)
        finally:
            self._busy = False

    def _error(self, source: str, e: Exception, fails: int, nxt: int) -> None:
        self.ingest.on_event("rest_error", source, f"{type(e).__name__}: {e} (attempt {fails}, next in "
                                                    f"{max(0, nxt - self.clock()) // 1000} s)"[:300])

    async def _gamma(self, slug: str, info, st: _Mk, end: int) -> None:
        now = self.clock()
        if now < st.gamma_next or self.rest.is_paused("gamma"):
            if now > end + self.confirm_wait_ms + POLL_MS:
                st.gamma_abandoned = True
            return
        try:
            events = await self.rest.get_json("gamma", f"{GAMMA_URL}/events?slug={urllib.parse.quote(slug)}")
            ok = True
        except RateLimited:
            return
        except Exception as e:  # noqa: BLE001 - any REST failure backs off
            ok = False
            st.gamma_fails += 1
            st.gamma_next = self.clock() + backoff_ms(st.gamma_fails)
            self._error(f"pm_resolve:{slug}", e, st.gamma_fails, st.gamma_next)
        now = self.clock()
        if ok:
            st.gamma_fails = 0
            outcome = gamma_outcome(events)
            if outcome is not None:
                self._answer(slug, outcome, events[0])
                return
        if now > end + self.confirm_wait_ms:
            st.gamma_abandoned = True

    def _answer(self, slug: str, outcome: str, event: dict) -> None:
        ing = self.ingest
        fallback = self.fallback_open and slug not in self.settler.decided and slug not in self.fallbacks
        if fallback:
            self.settler.give_up(slug, "outcome from Gamma")
            row = {"slug": slug, "outcome": outcome, "event": event, "source": SOURCE}
            recv = ing.on_rows([("pm_resolution", row)])
            self.fallbacks.add(slug)
        else:
            recv = max(int(self.clock()), ing.recv)
        rec = {"slug": slug, "outcome": outcome, "recv_ms": int(recv), "event": event}
        self.answers[slug] = rec
        ing.writer.write_paper("gamma_resolution", rec)

    async def _trades(self, slug: str, info, st: _Mk, end: int) -> None:
        now = self.clock()
        if now < st.trades_next or self.rest.is_paused("data"):
            if now > end + self.confirm_wait_ms + POLL_MS:
                st.trades_abandoned = True
            return
        cid = info.condition_id
        try:
            while st.n_pages < MAX_PAGES:
                url = f"{DATA_URL}/v2/trades?condition_id={cid}&limit=1000"
                if st.cursor:
                    url += f"&cursor={urllib.parse.quote(st.cursor, safe='')}"
                r = await self.rest.get_json("data", url)
                r = r if isinstance(r, dict) else {}
                st.pages.extend({"slug": slug, **t} for t in (r.get("data") or []) if isinstance(t, dict))
                st.n_pages += 1
                pg = r.get("pagination") or {}
                if not pg.get("has_more") or not pg.get("next_cursor"):
                    st.cursor = None
                    break
                st.cursor = str(pg["next_cursor"])
            rows, st.pages = st.pages, []
            if rows:
                self.ingest.on_rows([("pm_api_trades", row) for row in rows])
            self.api_trades[slug] = len(rows)
            st.trades_done = True
            st.trades_fails = 0
            return
        except RateLimited:
            return
        except Exception as e:  # noqa: BLE001
            st.trades_fails += 1
            st.trades_next = self.clock() + backoff_ms(st.trades_fails)
            self._error(f"pm_trades:{slug}", e, st.trades_fails, st.trades_next)
        if self.clock() > end + self.confirm_wait_ms:
            st.trades_abandoned = True
            st.pages, st.cursor = [], None


__all__ = ["Confirmer", "POLL_MS", "SOURCE", "gamma_outcome"]

"""Polymarket discovery and CLOB market feed (src/recorder/polymarket.ts).

Rows are produced exactly as the recorder writes them: TSV streams give the columns after recv_ms as text,
JSONL streams the object. The caller (Task 7's ingest) stamps recv_ms and numbers rows per stream.
"""

from __future__ import annotations

import asyncio
import json
from datetime import datetime, timezone
from typing import Callable, NamedTuple

from .rest import GAMMA_URL, RateLimited, Rest
from .series import LOOKAHEAD, PER_MARKET_SERIES, UNSUBSCRIBE_AFTER_END_MS, Series, lead_ms
from .ws import ReconnectingWs

CLOB_WS_URL = "wss://ws-subscriptions-clob.polymarket.com/ws/market"  # config.ts clobWsUrl
DISCOVERY_INTERVAL_MS = 30_000
SUBSCRIPTION_INTERVAL_MS = 5_000


def txt(v) -> str:
    """Recorder TSV cell: None/missing -> "", strings verbatim, JSON numbers by Python repr (round-trips)."""
    if v is None:
        return ""
    if isinstance(v, str):
        return v
    if isinstance(v, bool):
        return "true" if v else "false"
    if isinstance(v, (int, float)):
        return repr(v)
    return str(v)


def _lv(levels) -> str:
    return "|".join(f"{txt(x.get('price'))}:{txt(x.get('size'))}" for x in (levels or []))


def parse_clob(raw: str, key: Callable[[str], str]) -> list[tuple[str, tuple | dict]]:
    """One CLOB message (object or array of objects) -> [(stream, values)] in message order (polymarket.ts:50)."""
    if raw == "PONG" or raw == "":
        return []
    parsed = json.loads(raw)
    out: list[tuple[str, tuple | dict]] = []
    for m in parsed if isinstance(parsed, list) else [parsed]:
        et = m.get("event_type") if isinstance(m, dict) else None
        if et == "book":
            out.append(("pm_book", (txt(m.get("timestamp")), key(txt(m.get("asset_id"))), _lv(m.get("bids")), _lv(m.get("asks")), txt(m.get("hash")))))
        elif et == "price_change":
            ts = txt(m.get("timestamp"))
            for c in m.get("price_changes") or []:
                out.append(("pm_price_change", (ts, key(txt(c.get("asset_id"))), txt(c.get("side")), txt(c.get("price")), txt(c.get("size")), txt(c.get("best_bid")), txt(c.get("best_ask")))))
        elif et == "best_bid_ask":
            out.append(("pm_best_bid_ask", (txt(m.get("timestamp")), key(txt(m.get("asset_id"))), txt(m.get("best_bid")), txt(m.get("best_ask")), txt(m.get("spread")))))
        elif et == "last_trade_price":
            out.append(("pm_trade", (txt(m.get("timestamp")), key(txt(m.get("asset_id"))), txt(m.get("side")), txt(m.get("price")), txt(m.get("size")), txt(m.get("fee_rate_bps")), txt(m.get("transaction_hash")))))
        elif et == "new_market":
            out.append(("pm_new_market", m))
        else:
            out.append(("pm_other", m))
    return out


class Market(NamedTuple):
    slug: str
    series: Series
    condition_id: str
    up_token: str
    down_token: str
    start: int  # ms
    end: int  # ms


def _ms(iso: str) -> int:
    return int(datetime.fromisoformat(iso.replace("Z", "+00:00")).timestamp() * 1000)


class _State:
    __slots__ = ("m", "subscribed", "done")

    def __init__(self, m: Market):
        self.m, self.subscribed, self.done = m, False, False


class PolymarketFeed:
    def __init__(
        self,
        series: list[Series],
        emit: Callable[[list[tuple[str, object]]], None],
        event: Callable[[str, str, str], None],
        http: Rest,
        clock: Callable[[], int],
    ):
        self.series, self.emit, self.event, self.http, self.clock = series, emit, event, http, clock
        self._mk: dict[str, _State] = {}
        self._token_key: dict[str, str] = {}
        self._conns: dict[str, ReconnectingWs] = {}
        self._tasks: dict[str, asyncio.Task] = {}
        self._stopped = False

    def markets(self) -> list[Market]:
        return [s.m for s in self._mk.values()]

    def _key(self, token: str) -> str:
        return self._token_key.get(token, token)

    def _on_message(self, raw: str, recv_ms: int) -> None:
        rows = parse_clob(raw, self._key)
        if rows:
            self.emit(rows)

    # discovery (polymarket.ts:197)
    def _register(self, s: Series, event: dict) -> None:
        mk = (event.get("markets") or [None])[0]
        slug = event.get("slug")
        if not mk or slug in self._mk:
            return
        up, down = json.loads(mk["clobTokenIds"])
        outcomes = json.loads(mk["outcomes"])
        if outcomes[:2] != ["Up", "Down"]:
            self.event("pm_unexpected_outcomes", slug, json.dumps(outcomes))
        m = Market(
            slug, s, mk["conditionId"], up, down,
            _ms(mk.get("eventStartTime") or event["startTime"]), _ms(mk.get("endDate") or event["endDate"]),
        )
        # The discovery row goes out first: the caller ingests it before this market is ever subscribed
        # (rows of an undiscovered market would make it an orphan).
        self.emit([("pm_market_meta", {"series": s.slug, "event": event})])
        self._mk[slug] = _State(m)
        self._token_key[up] = f"{slug}|U"
        self._token_key[down] = f"{slug}|D"

    async def discover(self) -> None:
        iso = datetime.fromtimestamp(self.clock() / 1000, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"
        for s in self.series:
            if self.http.is_paused("gamma"):
                break
            try:
                events = await self.http.get_json(
                    "gamma",
                    f"{GAMMA_URL}/events?series_id={s.id}&closed=false&end_date_min={iso}&order=endDate&ascending=true&limit={LOOKAHEAD[s.recurrence]}",
                )
                for e in events:
                    self._register(s, e)
            except RateLimited:
                pass  # the Rest layer logged rate_limited once
            except Exception as e:  # noqa: BLE001
                self.event("rest_error", f"pm_discover:{s.slug}", f"{type(e).__name__}: {e}"[:300])
        self.update_subscriptions()

    # subscriptions (polymarket.ts:215)
    def _group(self, m: Market) -> str:
        return m.slug if m.series.slug in PER_MARKET_SERIES else m.series.slug

    def _tokens(self, group: str | None = None) -> list[str]:
        return [t for s in self._mk.values() if s.subscribed and not s.done and (group is None or self._group(s.m) == group)
                for t in (s.m.up_token, s.m.down_token)]

    def _conn_for(self, group: str) -> ReconnectingWs:
        c = self._conns.get(group)
        if c is None:
            def on_open(ws: ReconnectingWs, group=group):
                ids = self._tokens(group)
                if ids:
                    ws.send(json.dumps({"assets_ids": ids, "type": "market", "custom_feature_enabled": True}))

            c = ReconnectingWs(
                f"pm:{group}", lambda: CLOB_WS_URL, self._on_message, self.event, on_open=on_open,
                text_ping=("PING", 10_000), stale_ms=25_000, clock=self.clock,
            )
            self._conns[group] = c
            self._tasks[group] = asyncio.ensure_future(c.run())
        return c

    def update_subscriptions(self) -> None:
        now = self.clock()
        add: dict[str, list[str]] = {}
        remove: dict[str, list[str]] = {}
        for st in self._mk.values():
            m = st.m
            if not st.subscribed and m.start - lead_ms(m.series) <= now < m.end + UNSUBSCRIBE_AFTER_END_MS:
                st.subscribed = True
                add.setdefault(self._group(m), []).extend([m.up_token, m.down_token])
            elif st.subscribed and not st.done and now >= m.end + UNSUBSCRIBE_AFTER_END_MS:
                st.done = True
                remove.setdefault(self._group(m), []).extend([m.up_token, m.down_token])
        for group, ids in add.items():
            existed = group in self._conns
            c = self._conn_for(group)  # a new connection subscribes everything in on_open
            if existed and c.is_open():
                c.send(json.dumps({"assets_ids": ids, "operation": "subscribe", "custom_feature_enabled": True}))
        for group, ids in remove.items():
            c = self._conns.get(group)
            if c is None:
                continue
            if group in self._mk:  # per-market connection: close it entirely
                c.stop()
                del self._conns[group]
                self._tasks.pop(group, None)
            else:
                c.send(json.dumps({"assets_ids": ids, "operation": "unsubscribe"}))
        if add or remove:
            n = lambda d: sum(len(v) // 2 for v in d.values())  # noqa: E731
            self.event("pm_subscriptions", "polymarket", f"+{n(add)} -{n(remove)} markets, {len(self._tokens()) // 2} active on {len(self._conns)} connections")

    async def run(self) -> None:
        async def subs():
            while not self._stopped:
                await asyncio.sleep(SUBSCRIPTION_INTERVAL_MS / 1000)
                self.update_subscriptions()

        sub_task = asyncio.ensure_future(subs())
        try:
            while not self._stopped:
                try:
                    await self.discover()
                except Exception as e:  # noqa: BLE001
                    self.event("error", "pm_discover", str(e)[:300])
                await asyncio.sleep(DISCOVERY_INTERVAL_MS / 1000)
        finally:
            sub_task.cancel()
            self.stop()

    def stop(self) -> None:
        self._stopped = True
        for c in self._conns.values():
            c.stop()

"""Reconnecting WebSocket client: the Python port of src/recorder/wsClient.ts.

Backoff 1 s doubling to 30 s, text keepalive, stale timeout, max lifetime. Events (kind, source, detail) use the
recorder's names: ws_open, ws_close ("code=..., reconnect in N ms"), ws_stale, ws_rotate, ws_error, parse_error.
One deviation: the backoff resets after the first message of a connection, not on open, so a server that
accepts and immediately closes is not hammered once a second.
"""

from __future__ import annotations

import asyncio
from typing import Awaitable, Callable

from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed

MAX_PAYLOAD = 64 * 1024 * 1024


class ReconnectingWs:
    def __init__(
        self,
        name: str,
        url: Callable[[], str],
        on_message: Callable[[str, int], None],
        on_event: Callable[[str, str, str], None],
        *,
        on_open: Callable[["ReconnectingWs"], None] | None = None,
        text_ping: tuple[str, int] | None = None,
        stale_ms: int | None = None,
        max_lifetime_ms: int | None = None,
        clock: Callable[[], int],
        sleep: Callable[[float], Awaitable[None]] = asyncio.sleep,
    ):
        self.name, self.url, self.on_message, self.on_event = name, url, on_message, on_event
        self.on_open, self.text_ping, self.stale_ms, self.max_lifetime_ms = on_open, text_ping, stale_ms, max_lifetime_ms
        self.clock, self._sleep = clock, sleep
        self.messages = 0
        self.reconnects = 0
        self._backoff_ms = 1000
        self._stopped = False
        self._open = False
        self._out: asyncio.Queue[str] = asyncio.Queue()
        self._main: asyncio.Task | None = None
        self._wake: asyncio.Task | None = None

    def is_open(self) -> bool:
        return self._open

    def send(self, text: str) -> None:
        """Queue a text frame; dropped when not connected (as the recorder does)."""
        if self._open:
            self._out.put_nowait(text)

    def stop(self) -> None:
        self._stopped = True
        for t in (self._main, self._wake):
            if t is not None and not t.done():
                t.cancel()

    async def run(self) -> None:
        self._main = asyncio.current_task()
        try:
            while not self._stopped:
                code = await self._session()
                if self._stopped:
                    break
                self.reconnects += 1
                delay = self._backoff_ms
                self.on_event("ws_close", self.name, f"code={code}, reconnect in {delay} ms")
                self._backoff_ms = min(self._backoff_ms * 2, 30_000)
                self._wake = asyncio.ensure_future(self._sleep(delay / 1000))
                try:
                    await self._wake
                except asyncio.CancelledError:
                    if self._stopped:
                        break
                    raise
        except asyncio.CancelledError:
            if not self._stopped:
                raise
        finally:
            self._open = False

    async def _session(self) -> int:
        """One connection; returns the close code."""
        code = 1006
        tasks: list[asyncio.Task] = []
        reader = asyncio.current_task()
        reason: list[str] = []
        try:
            async with connect(self.url(), compression=None, max_size=MAX_PAYLOAD, ping_interval=None, close_timeout=1) as ws:
                opened = last = self.clock()
                self._open = True
                self._out = asyncio.Queue()
                self.on_event("ws_open", self.name, "")
                try:
                    if self.on_open:
                        self.on_open(self)
                except Exception as e:  # noqa: BLE001
                    self.on_event("parse_error", self.name, f"on_open: {e}"[:300])

                async def writer():
                    while True:
                        await ws.send(await self._out.get())

                async def watchdog():
                    nonlocal last
                    while True:
                        await asyncio.sleep(1)
                        now = self.clock()
                        if self.stale_ms and now - last > self.stale_ms:
                            self.on_event("ws_stale", self.name, f"no message for {now - last} ms")
                            reason.append("stale")
                            reader.cancel()
                            return
                        if self.max_lifetime_ms and now - opened > self.max_lifetime_ms:
                            self.on_event("ws_rotate", self.name, "max lifetime reached")
                            reason.append("rotate")
                            reader.cancel()
                            return

                async def pinger(payload: str, every_ms: int):
                    while True:
                        await asyncio.sleep(every_ms / 1000)
                        self._out.put_nowait(payload)

                tasks.append(asyncio.ensure_future(writer()))
                if self.stale_ms or self.max_lifetime_ms:
                    tasks.append(asyncio.ensure_future(watchdog()))
                if self.text_ping:
                    tasks.append(asyncio.ensure_future(pinger(*self.text_ping)))
                first = True
                try:
                    async for msg in ws:
                        now = self.clock()
                        last = now
                        self.messages += 1
                        if first:
                            first = False
                            self._backoff_ms = 1000
                        try:
                            self.on_message(msg if isinstance(msg, str) else msg.decode("utf-8", "replace"), now)
                        except Exception as e:  # noqa: BLE001
                            self.on_event("parse_error", self.name, f"{type(e).__name__}: {e}"[:300])
                except asyncio.CancelledError:
                    if self._stopped or not reason:
                        raise
                    # stale / rotate: the watchdog cancelled us on purpose
                    reader.uncancel()
                code = ws.close_code or 1006
        except asyncio.CancelledError:
            if self._stopped or not reason:
                raise
            reader.uncancel()
        except ConnectionClosed as e:
            code = e.rcvd.code if e.rcvd else 1006
        except Exception as e:  # noqa: BLE001  (connect failure)
            self.on_event("ws_error", self.name, f"{type(e).__name__}: {e}"[:300])
        finally:
            self._open = False
            for t in tasks:
                t.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
        return code

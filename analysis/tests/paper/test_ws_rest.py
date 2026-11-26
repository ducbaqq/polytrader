import asyncio
import http.server
import threading
import time

import pytest
from websockets.asyncio.server import serve

from polydata.paper.feeds.rest import Rest, RateLimited, backoff_ms
from polydata.paper.feeds.ws import ReconnectingWs


def test_ws_backoff_and_events():
    async def main():
        events, delays, n = [], [], 0
        done = asyncio.Event()

        async def handler(ws):
            nonlocal n
            n += 1
            if n <= 2:
                await ws.close()
            else:
                done.set()
                await ws.wait_closed()

        async def fake_sleep(s):
            delays.append(s)

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            c = ReconnectingWs("t", lambda: f"ws://127.0.0.1:{port}", lambda r, t: None,
                               lambda k, s, d: events.append((k, d)), clock=lambda: int(time.time() * 1000),
                               sleep=fake_sleep)
            task = asyncio.ensure_future(c.run())
            await asyncio.wait_for(done.wait(), 10)
            await asyncio.sleep(0.1)
            c.stop()
            await asyncio.wait_for(task, 5)
        return events, delays

    events, delays = asyncio.run(main())
    assert [k for k, _ in events] == ["ws_open", "ws_close", "ws_open", "ws_close", "ws_open"]
    assert delays[:2] == [1.0, 2.0]
    assert "reconnect in 1000 ms" in events[1][1] and "reconnect in 2000 ms" in events[3][1]


def test_ws_messages_and_parse_error():
    async def main():
        got, events = [], []
        stop_evt = asyncio.Event()

        async def handler(ws):
            await ws.send("hello")
            await ws.send("boom")
            await stop_evt.wait()

        def on_message(raw, t):
            if raw == "boom":
                raise ValueError("bad")
            got.append(raw)

        async with serve(handler, "127.0.0.1", 0) as server:
            port = server.sockets[0].getsockname()[1]
            c = ReconnectingWs("t", lambda: f"ws://127.0.0.1:{port}", on_message,
                               lambda k, s, d: events.append(k), clock=lambda: int(time.time() * 1000))
            task = asyncio.ensure_future(c.run())
            await asyncio.sleep(0.5)
            c.stop()
            stop_evt.set()
            await asyncio.wait_for(task, 5)
        return got, events

    got, events = asyncio.run(main())
    assert got == ["hello"] and "parse_error" in events


def test_rest_429_pauses_api():
    class H(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            if self.path == "/ok":
                body = b'{"a": 1}'
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
            else:
                self.send_response(429)
                self.send_header("Retry-After", "1")
                self.end_headers()

        def log_message(self, *a):
            pass

    srv = http.server.HTTPServer(("127.0.0.1", 0), H)
    threading.Thread(target=srv.serve_forever, daemon=True).start()
    base = f"http://127.0.0.1:{srv.server_port}"
    now = [1_000_000]
    events = []
    rest = Rest(lambda: now[0], lambda k, s, d: events.append((k, s)))

    async def main():
        assert await rest.get_json("data", base + "/ok") == {"a": 1}
        with pytest.raises(RateLimited):
            await rest.get_json("gamma", base + "/x")
        assert rest.paused_until["gamma"] - now[0] >= 10_000
        assert not rest.is_paused("data")
        with pytest.raises(RateLimited):  # paused: no request, no second event
            await rest.get_json("gamma", base + "/ok")
        now[0] += 10_001
        assert await rest.get_json("gamma", base + "/ok") == {"a": 1}

    asyncio.run(main())
    srv.shutdown()
    assert events == [("rate_limited", "pm_gamma")]


def test_backoff_ms():
    assert [backoff_ms(i) for i in (1, 2, 3, 4, 5, 6, 7, 8)] == [30_000, 60_000, 120_000, 240_000, 480_000, 600_000, 600_000, 600_000]

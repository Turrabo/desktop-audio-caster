"""HTTP server streaming an endless WAV to Cast receivers.

Contract (per plan review):
- RIFF/data size fields are 0xFFFFFFFF (endless stream).
- Fresh header per connection; Range requests answered 200 with the full
  live stream (the receiver probes with Range; there is no past to seek to).
- No Content-Length; chunked transfer.
- Per-client bounded queue, frame-aligned drop-oldest (~1 s), drops logged.
"""
from __future__ import annotations

import asyncio
import logging
import struct
import threading

from aiohttp import web

from .capture import CaptureFormat

log = logging.getLogger(__name__)

CLIENT_QUEUE_SECONDS = 0.3


def wav_header(fmt: CaptureFormat) -> bytes:
    byte_rate = fmt.rate * fmt.channels * fmt.sampwidth
    block_align = fmt.channels * fmt.sampwidth
    return b"".join([
        b"RIFF", struct.pack("<I", 0xFFFFFFFF), b"WAVE",
        b"fmt ", struct.pack("<IHHIIHH", 16, 1, fmt.channels, fmt.rate,
                             byte_rate, block_align, fmt.sampwidth * 8),
        b"data", struct.pack("<I", 0xFFFFFFFF),
    ])


class StreamServer:
    def __init__(self, fmt: CaptureFormat, port: int):
        self._fmt = fmt
        self._port = port
        self._loop: asyncio.AbstractEventLoop | None = None
        self._thread: threading.Thread | None = None
        self._clients: set[asyncio.Queue] = set()
        self._clients_lock = threading.Lock()
        self._started = threading.Event()
        self._runner: web.AppRunner | None = None
        self.get_count = 0
        # latest client's delivery counters (for end-to-end lag estimation)
        self.latest_client_bytes = 0
        self.latest_client_connected_at = 0.0

    # -- feeding (called from pacer thread) ---------------------------------

    def feed(self, chunk: bytes) -> None:
        loop = self._loop
        if loop is None or not self._clients:
            return
        loop.call_soon_threadsafe(self._fanout, chunk)

    def _fanout(self, chunk: bytes) -> None:
        with self._clients_lock:
            clients = list(self._clients)
        for q in clients:
            if q.full():
                try:
                    q.get_nowait()  # drop-oldest (whole chunks = frame-aligned)
                    log.debug("client queue full - dropped oldest chunk")
                except asyncio.QueueEmpty:
                    pass
            q.put_nowait(chunk)

    # -- HTTP ----------------------------------------------------------------

    async def _handle_stream(self, request: web.Request) -> web.StreamResponse:
        self.get_count += 1
        peer = request.remote
        log.info("stream client connected: %s (%s %s, range=%r)",
                 peer, request.method, request.path, request.headers.get("Range"))

        resp = web.StreamResponse(status=200, headers={
            "Content-Type": "audio/wav",
            "Cache-Control": "no-cache, no-store",
            "Accept-Ranges": "none",
            "Connection": "close",
        })
        resp.enable_chunked_encoding()
        await resp.prepare(request)
        await resp.write(wav_header(self._fmt))

        chunk_seconds = 0.02
        maxsize = max(2, int(CLIENT_QUEUE_SECONDS / chunk_seconds))
        q: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        with self._clients_lock:
            self._clients.add(q)
        import time as _time
        self.latest_client_bytes = 0
        self.latest_client_connected_at = _time.monotonic()
        try:
            while True:
                chunk = await q.get()
                await resp.write(chunk)
                self.latest_client_bytes += len(chunk)
        except (ConnectionResetError, asyncio.CancelledError, Exception) as e:
            log.info("stream client disconnected: %s (%s)", peer, type(e).__name__)
        finally:
            with self._clients_lock:
                self._clients.discard(q)
        return resp

    # -- lifecycle -------------------------------------------------------------

    def start(self) -> None:
        self._thread = threading.Thread(target=self._run, name="http-server", daemon=True)
        self._thread.start()
        if not self._started.wait(timeout=10):
            raise RuntimeError("HTTP server failed to start within 10 s")

    def _run(self) -> None:
        self._loop = asyncio.new_event_loop()
        asyncio.set_event_loop(self._loop)
        app = web.Application()
        # add_get registers HEAD automatically (served by the same handler;
        # aiohttp suppresses the body for HEAD).
        app.router.add_get("/stream.wav", self._handle_stream)

        async def _start():
            self._runner = web.AppRunner(app, access_log=None)
            await self._runner.setup()
            site = web.TCPSite(self._runner, host="0.0.0.0", port=self._port)
            await site.start()
            log.info("HTTP server listening on 0.0.0.0:%d", self._port)
            self._started.set()

        self._loop.run_until_complete(_start())
        self._loop.run_forever()

    def stop(self) -> None:
        loop = self._loop
        if loop is None:
            return

        async def _shutdown():
            if self._runner is not None:
                await self._runner.cleanup()
            loop.stop()

        asyncio.run_coroutine_threadsafe(_shutdown(), loop)
        if self._thread is not None:
            self._thread.join(timeout=5)

    def client_count(self) -> int:
        with self._clients_lock:
            return len(self._clients)

"""Single-clock stream pacer - the anti-drift core.

One monotonic sample counter decides how many bytes the outgoing stream needs.
Real captured bytes fill that need; any shortfall is filled with EXACTLY the
missing number of silence bytes. There is no second clock, so mute/unmute
cycles cannot accumulate offset, and the stream never gaps (keep-alive +
no receiver underrun).
"""
from __future__ import annotations

import logging
import threading
import time
from typing import Callable

from .capture import CaptureFormat

log = logging.getLogger(__name__)

TICK_SECONDS = 0.02          # 20 ms
MAX_BACKLOG_SECONDS = 0.2    # captured audio older than this is dropped (bounds latency)


class Pacer:
    def __init__(self, fmt: CaptureFormat, sink: Callable[[bytes], None]):
        self._fmt = fmt
        self._sink = sink
        self._fifo = bytearray()
        self._lock = threading.Lock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self.silence_bytes_sent = 0
        self.real_bytes_sent = 0
        self.dropped_bytes = 0

    def feed(self, data: bytes) -> None:
        """Called from the capture thread."""
        max_backlog = int(MAX_BACKLOG_SECONDS * self._fmt.bytes_per_second)
        with self._lock:
            self._fifo.extend(data)
            excess = len(self._fifo) - max_backlog
            if excess > 0:
                excess -= excess % self._fmt.frame_bytes  # frame-aligned drop
                del self._fifo[:excess]
                self.dropped_bytes += excess

    def start(self) -> None:
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="pacer", daemon=True)
        self._thread.start()

    def _run(self) -> None:
        fb = self._fmt.frame_bytes
        started = time.monotonic()
        samples_emitted = 0
        while not self._stop.is_set():
            time.sleep(TICK_SECONDS)
            target_samples = int((time.monotonic() - started) * self._fmt.rate)
            need = (target_samples - samples_emitted) * fb
            if need <= 0:
                continue
            with self._lock:
                take = min(need, len(self._fifo))
                take -= take % fb
                chunk = bytes(self._fifo[:take])
                del self._fifo[:take]
            silence = need - take
            if silence > 0:
                chunk += b"\x00" * silence
                self.silence_bytes_sent += silence
            self.real_bytes_sent += take
            samples_emitted += need // fb
            self._sink(chunk)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

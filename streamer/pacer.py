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

# Standing-backlog trim. The FIFO's inflow rate equals its outflow rate, so any
# depth it holds when the clock starts (or gains from a capture burst / clock
# skew) persists FOREVER as pure added latency - the cast plays that many ms
# behind and nothing else ever drains it. Every TRIM_CHECK_SECONDS the pacer
# looks at the MINIMUM depth seen over the window (the guaranteed-standing part,
# immune to inflow jitter) and drops all but a small residual, converting the
# permanent latency into one skip-forward.
TRIM_CHECK_SECONDS = 5.0
TRIM_RESIDUAL_SECONDS = 0.04  # jitter headroom left in place (2 ticks)


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
        self.trims = 0               # standing-backlog trims performed

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
        bps = self._fmt.bytes_per_second
        started = time.monotonic()
        samples_emitted = 0
        min_depth: int | None = None       # floor of post-take FIFO depth
        next_trim = started + TRIM_CHECK_SECONDS
        while not self._stop.is_set():
            time.sleep(TICK_SECONDS)
            now = time.monotonic()
            target_samples = int((now - started) * self._fmt.rate)
            need = (target_samples - samples_emitted) * fb
            if need <= 0:
                continue
            with self._lock:
                take = min(need, len(self._fifo))
                take -= take % fb
                chunk = bytes(self._fifo[:take])
                del self._fifo[:take]
                depth = len(self._fifo)
            if min_depth is None or depth < min_depth:
                min_depth = depth
            silence = need - take
            if silence > 0:
                chunk += b"\x00" * silence
                self.silence_bytes_sent += silence
            self.real_bytes_sent += take
            samples_emitted += need // fb
            self._sink(chunk)
            if now >= next_trim:
                self._trim_standing(min_depth, bps, fb)
                min_depth = None
                next_trim = now + TRIM_CHECK_SECONDS

    def _trim_standing(self, min_depth: int | None, bps: int, fb: int) -> None:
        """Drop the guaranteed-standing part of the FIFO (its window-minimum
        depth) beyond the jitter residual. min_depth is the floor observed over
        the whole window, so this never eats into normal inflow jitter."""
        residual = int(TRIM_RESIDUAL_SECONDS * bps)
        if min_depth is None or min_depth <= residual:
            return
        cut = min_depth - residual
        cut -= cut % fb
        if cut <= 0:
            return
        with self._lock:
            cut = min(cut, len(self._fifo))
            cut -= cut % fb
            del self._fifo[:cut]
            # Counters stay under the lock: feed()'s overflow drop increments
            # dropped_bytes from the capture thread, and += is not atomic.
            if cut > 0:
                self.dropped_bytes += cut
                self.trims += 1
        if cut > 0:
            log.info("pacer: trimmed %.0f ms standing backlog (latency debt)",
                     cut / bps * 1000)

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=2)

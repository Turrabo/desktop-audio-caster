"""Pacer clock catch-up clamp.

The pacer derives demand from wall time since the cast started, so a long stall
(suspend/resume, a pathological scheduler hiccup) asks for the whole gap in one
tick and materialises the shortfall as literal silence bytes. Unclamped, an
hour's gap is a ~690 MB allocation - copied again by the silence gate and again
by the mirror's frame loop while holding a lock. Past MAX_CATCHUP_SECONDS the
clock re-anchors and skips the gap instead.
"""
import threading
import unittest

import streamer.pacer as pacer_mod
from streamer.capture import CaptureFormat
from streamer.pacer import Pacer

FMT = CaptureFormat(48000, 2, 2)          # 192000 bytes/s, frame_bytes 4
BPS = FMT.bytes_per_second


class FakeClock:
    """Drives the pacer loop deterministically and injects one huge jump."""

    def __init__(self, stop_after: int, jump_at: int, jump: float):
        self.t = 1000.0
        self.ticks = 0
        self.stop_after = stop_after
        self.jump_at = jump_at
        self.jump = jump
        self.stop_event: threading.Event | None = None

    def monotonic(self):
        return self.t

    def sleep(self, seconds):
        self.ticks += 1
        self.t += seconds
        if self.ticks == self.jump_at:
            self.t += self.jump                  # the machine was asleep
        if self.ticks >= self.stop_after and self.stop_event is not None:
            self.stop_event.set()


class TestCatchupClamp(unittest.TestCase):
    def setUp(self):
        self._orig_time = pacer_mod.time

    def tearDown(self):
        pacer_mod.time = self._orig_time

    def _run_with_gap(self, gap_seconds: float):
        chunks = []
        p = Pacer(FMT, sink=chunks.append)
        clock = FakeClock(stop_after=12, jump_at=4, jump=gap_seconds)
        clock.stop_event = p._stop
        pacer_mod.time = clock
        p.feed(b"\x01" * (BPS // 10))            # some real audio available
        p.start()
        p._thread.join(timeout=5)
        return p, chunks

    def test_hour_long_gap_does_not_allocate_the_gap(self):
        p, chunks = self._run_with_gap(3600.0)
        ceiling = int(pacer_mod.MAX_CATCHUP_SECONDS * BPS) + FMT.frame_bytes
        self.assertTrue(chunks, "pacer emitted nothing")
        self.assertLessEqual(max(len(c) for c in chunks), ceiling)
        self.assertGreaterEqual(p.skips, 1)

    def test_clock_reanchors_so_later_ticks_are_normal(self):
        p, chunks = self._run_with_gap(3600.0)
        # After the skip the pacer must go back to ~one tick's worth per emit,
        # not keep trying to repay the gap.
        tail = chunks[-3:]
        tick_bytes = int(pacer_mod.TICK_SECONDS * BPS)
        for c in tail:
            self.assertLessEqual(len(c), tick_bytes * 3)

    def test_short_stall_is_still_filled_normally(self):
        """Only pathological gaps skip; ordinary jitter must still be paced."""
        p, chunks = self._run_with_gap(0.05)     # well under the ceiling
        self.assertEqual(p.skips, 0)

    def test_emitted_chunks_stay_frame_aligned(self):
        p, chunks = self._run_with_gap(3600.0)
        for c in chunks:
            self.assertEqual(len(c) % FMT.frame_bytes, 0)


if __name__ == "__main__":
    unittest.main()

"""Pacer standing-backlog trim: FIFO depth held at clock start (or gained from
a burst/clock skew) is permanent latency - inflow rate equals outflow rate, so
nothing else ever drains it. The trim drops the window-minimum depth beyond a
jitter residual, converting standing latency debt into one skip-forward.
"""
import time
import unittest

import streamer.pacer as pacer_mod
from streamer.capture import CaptureFormat
from streamer.pacer import Pacer

FMT = CaptureFormat(48000, 2, 2)          # 192000 bytes/s, frame_bytes 4
BPS = FMT.bytes_per_second


def ms_bytes(ms: float) -> int:
    n = int(BPS * ms / 1000)
    return n - (n % FMT.frame_bytes)


class TestTrimStanding(unittest.TestCase):
    def _pacer(self):
        self.sunk = []
        return Pacer(FMT, sink=self.sunk.append)

    def test_standing_debt_is_cut_to_residual(self):
        p = self._pacer()
        # Old (stale) audio first, newest audio last - the trim must eat the
        # FRONT (the debt) and leave the newest bytes intact.
        p._fifo.extend(b"\x01" * ms_bytes(100))   # stale debt
        p._fifo.extend(b"\x02" * ms_bytes(50))    # fresh audio
        p._trim_standing(min_depth=len(p._fifo), bps=BPS, fb=FMT.frame_bytes)
        self.assertEqual(p.trims, 1)
        # ~150ms debt cut down to the ~40ms residual
        residual = int(pacer_mod.TRIM_RESIDUAL_SECONDS * BPS)
        self.assertLessEqual(len(p._fifo), residual + FMT.frame_bytes)
        self.assertGreaterEqual(p.dropped_bytes, ms_bytes(100))
        # Survivors are the NEWEST bytes: trimming from the wrong end would
        # leave \x01 (the debt) in place and eat the live audio instead.
        self.assertEqual(p._fifo[-1], 0x02)
        self.assertNotIn(0x01, p._fifo)

    def test_depth_at_or_below_residual_untouched(self):
        p = self._pacer()
        p._fifo.extend(b"\x01" * ms_bytes(30))    # under the 40ms residual
        p._trim_standing(min_depth=len(p._fifo), bps=BPS, fb=FMT.frame_bytes)
        self.assertEqual(p.trims, 0)
        self.assertEqual(len(p._fifo), ms_bytes(30))

    def test_no_observation_no_trim(self):
        p = self._pacer()
        p._fifo.extend(b"\x01" * ms_bytes(150))
        p._trim_standing(min_depth=None, bps=BPS, fb=FMT.frame_bytes)
        self.assertEqual(p.trims, 0)

    def test_trim_uses_window_minimum_not_current_depth(self):
        """Inflow jitter (a just-arrived burst) must not be eaten: only the
        window MINIMUM - the guaranteed-standing part - is trimmed."""
        p = self._pacer()
        p._fifo.extend(b"\x01" * ms_bytes(100))   # current depth 100ms...
        p._trim_standing(min_depth=ms_bytes(50),  # ...but the floor was 50ms
                         bps=BPS, fb=FMT.frame_bytes)
        residual = int(pacer_mod.TRIM_RESIDUAL_SECONDS * BPS)
        expected_cut = ms_bytes(50) - residual
        expected_cut -= expected_cut % FMT.frame_bytes
        self.assertEqual(p.dropped_bytes, expected_cut)


class TestTrimLive(unittest.TestCase):
    """End-to-end through the real pacer thread: a startup burst becomes
    standing debt, and the running trim recovers it."""

    def setUp(self):
        self._orig_check = pacer_mod.TRIM_CHECK_SECONDS
        pacer_mod.TRIM_CHECK_SECONDS = 0.15

    def tearDown(self):
        pacer_mod.TRIM_CHECK_SECONDS = self._orig_check

    def test_startup_burst_gets_trimmed(self):
        p = Pacer(FMT, sink=lambda c: None)
        p.feed(b"\x01" * ms_bytes(150))           # debt before the clock starts
        p.start()
        try:
            start = time.monotonic()
            fed_ms = 0.0
            deadline = start + 3.0
            while time.monotonic() < deadline and p.trims == 0:
                # Closed-loop real-time inflow: feed exactly the elapsed wall
                # time's worth of audio, so sleep() coarseness (Windows timer
                # granularity, CI load) never starves the FIFO and drains the
                # seeded debt before a trim window can observe it.
                target_ms = (time.monotonic() - start) * 1000
                if target_ms > fed_ms:
                    p.feed(b"\x01" * ms_bytes(target_ms - fed_ms))
                    fed_ms = target_ms
                time.sleep(0.01)
            self.assertGreaterEqual(p.trims, 1)
            self.assertGreater(p.dropped_bytes, 0)
        finally:
            p.stop()


if __name__ == "__main__":
    unittest.main()

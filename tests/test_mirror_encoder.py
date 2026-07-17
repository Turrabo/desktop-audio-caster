"""Opus encoder gate (streamer._opus over assets/opus.dll).

Skips cleanly if the DLL is absent (non-Windows CI), so the eligibility
contract - opus missing -> mirror ineligible - is exercised, not crashed on.
"""
import struct
import math
import unittest

from streamer import _opus


def _tone_frame(freq=440, amp=3000):
    return b"".join(
        struct.pack("<hh", int(amp * math.sin(2 * math.pi * freq * n / 48000)),
                    int(amp * math.sin(2 * math.pi * freq * n / 48000)))
        for n in range(_opus.FRAME_SAMPLES))


@unittest.skipUnless(_opus.available(), "opus.dll not loadable")
class TestOpusEncoder(unittest.TestCase):
    def test_version_is_libopus(self):
        enc = _opus.OpusEncoder()
        self.addCleanup(enc.close)
        self.assertIn("libopus", enc.version)

    def test_encodes_frames(self):
        enc = _opus.OpusEncoder()
        self.addCleanup(enc.close)
        pkt = enc.encode(_tone_frame())
        self.assertGreater(len(pkt), 10)         # a real tone packet
        self.assertLess(len(pkt), 1000)

    def test_dtx_off_silence_still_emits(self):
        # DTX disabled -> even silence produces a (tiny) packet every frame,
        # so the 100 fps cadence the RTP timestamping assumes never breaks.
        enc = _opus.OpusEncoder()
        self.addCleanup(enc.close)
        silent = b"\x00" * (_opus.FRAME_SAMPLES * _opus.CHANNELS * 2)
        self.assertGreater(len(enc.encode(silent)), 0)

    def test_wrong_frame_size_rejected(self):
        enc = _opus.OpusEncoder()
        self.addCleanup(enc.close)
        with self.assertRaises(ValueError):
            enc.encode(b"\x00" * 100)


if __name__ == "__main__":
    unittest.main()

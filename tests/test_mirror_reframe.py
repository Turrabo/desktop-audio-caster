"""MirrorSink reframing: variable-size pacer chunks -> exact 480-sample frames.

Pure-logic test (no encoder / no DLL): feed() carves frames synchronously into
the queue, so the reframing and drop-oldest behaviour are checked without the
pump thread or opus.dll.
"""
import unittest

from streamer.mirror import FRAME_BYTES, MirrorSink


class TestReframe(unittest.TestCase):
    def _sink(self):
        return MirrorSink(on_packet=lambda pkt: None)

    def test_exact_multiple(self):
        s = self._sink()
        s.feed(b"\x00" * (FRAME_BYTES * 3))
        self.assertEqual(s.pending_frames(), 3)

    def test_carries_remainder(self):
        s = self._sink()
        s.feed(b"\x01" * (FRAME_BYTES + 100))     # 1 frame + 100 leftover
        self.assertEqual(s.pending_frames(), 1)
        s.feed(b"\x02" * (FRAME_BYTES - 100))     # completes the 2nd frame
        self.assertEqual(s.pending_frames(), 2)

    def test_odd_chunk_sizes_reassemble(self):
        s = self._sink()
        total = 0
        for chunk in (13, 1000, FRAME_BYTES, 7, FRAME_BYTES * 2, 500):
            s.feed(b"\x00" * chunk)
            total += chunk
        self.assertEqual(s.pending_frames(), total // FRAME_BYTES)

    def test_drop_oldest_bounds_backlog(self):
        s = self._sink()
        # feed far more than the queue holds; deque caps at QUEUE_FRAMES
        s.feed(b"\x00" * (FRAME_BYTES * (MirrorSink.QUEUE_FRAMES + 20)))
        self.assertEqual(s.pending_frames(), MirrorSink.QUEUE_FRAMES)
        self.assertEqual(s.dropped_frames, 20)

    def test_frame_bytes_is_10ms_stereo_48k(self):
        self.assertEqual(FRAME_BYTES, 480 * 2 * 2)


if __name__ == "__main__":
    unittest.main()

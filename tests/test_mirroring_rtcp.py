"""Offline tests for the Cast Feedback parser (mirroring spike M2).

A parser bug here would misread the spike's own success signal (dual-review
S2), so the format is exercised with hand-built packets straight off the
openscreen rtp_defines.h diagrams, plus the frame-id expansion arithmetic
the spec itself calls a trap.
"""
import struct
import unittest

from experiments.mirroring.rtcp_feedback import (
    ALL_PACKETS_LOST, CastFeedback, expand_frame_id, parse_compound)

SENDER_SSRC = 0x41727470
RECEIVER_SSRC = 0x52637672


def feedback_packet(ckpt: int, playout_ms: int,
                    nacks=(), cst2: bytes | None = None,
                    sender_ssrc: int = SENDER_SSRC) -> bytes:
    body = struct.pack(">II", RECEIVER_SSRC, sender_ssrc)
    body += b"CAST"
    body += struct.pack(">BBH", ckpt & 0xFF, len(nacks), playout_ms)
    for wfid, pid, bv in nacks:
        body += struct.pack(">BHB", wfid & 0xFF, pid, bv)
    if cst2 is not None:
        # feedback count 1, octet count = len, zero-pad to word boundary
        blob = b"CST2" + struct.pack(">BB", 1, len(cst2)) + cst2
        blob += bytes(-len(blob) % 4)
        body += blob
    words = (4 + len(body)) // 4 - 1
    return struct.pack(">BBH", 0x80 | 15, 206, words) + body


def receiver_report() -> bytes:
    body = struct.pack(">I", RECEIVER_SSRC)
    return struct.pack(">BBH", 0x80, 201, len(body) // 4) + body


class TestParseCompound(unittest.TestCase):
    def test_basic_feedback(self):
        fbs = parse_compound(feedback_packet(7, 400), SENDER_SSRC)
        self.assertEqual(len(fbs), 1)
        self.assertEqual(fbs[0].checkpoint_truncated, 7)
        self.assertEqual(fbs[0].playout_delay_ms, 400)
        self.assertEqual(fbs[0].nacks, [])
        self.assertFalse(fbs[0].has_cst2_ack)

    def test_compound_with_rr_prefix(self):
        # Real receivers send RR + feedback in one datagram.
        data = receiver_report() + feedback_packet(200, 350)
        fbs = parse_compound(data, SENDER_SSRC)
        self.assertEqual(len(fbs), 1)
        self.assertEqual(fbs[0].checkpoint_truncated, 200)

    def test_wrong_sender_ssrc_ignored(self):
        data = feedback_packet(9, 400, sender_ssrc=0xDEAD)
        self.assertEqual(parse_compound(data, SENDER_SSRC), [])

    def test_nack_fields(self):
        nacks = [(8, 0, 0b10100000), (9, ALL_PACKETS_LOST, 0)]
        fbs = parse_compound(feedback_packet(7, 400, nacks), SENDER_SSRC)
        self.assertEqual(fbs[0].nacks,
                         [(8, 0, 0b10100000), (9, ALL_PACKETS_LOST, 0)])

    def test_cst2_ack_vector(self):
        fbs = parse_compound(
            feedback_packet(7, 400, cst2=b"\xff\x01"), SENDER_SSRC)
        self.assertTrue(fbs[0].has_cst2_ack)
        self.assertEqual(fbs[0].ack_bitvector, b"\xff\x01")

    def test_garbage_and_truncation_survive(self):
        self.assertEqual(parse_compound(b"", SENDER_SSRC), [])
        self.assertEqual(parse_compound(b"\x00\x01\x02", SENDER_SSRC), [])
        whole = feedback_packet(7, 400)
        for cut in range(1, len(whole)):
            parse_compound(whole[:cut], SENDER_SSRC)  # must not raise


class TestExpandFrameId(unittest.TestCase):
    def test_below_256_identity(self):
        self.assertEqual(expand_frame_id(7, 7), 7)
        self.assertEqual(expand_frame_id(5, 9), 5)

    def test_wraps(self):
        # last_sent 300 (0x12C); truncated 0x2A (42) -> 298
        self.assertEqual(expand_frame_id(42, 300), 298)
        # exactly at a wrap boundary
        self.assertEqual(expand_frame_id(255, 256), 255)
        self.assertEqual(expand_frame_id(0, 256), 256)

    def test_large_values(self):
        last = 100_000
        for delta in (0, 1, 50, 119):
            expanded = expand_frame_id((last - delta) & 0xFF, last)
            self.assertEqual(expanded, last - delta)


if __name__ == "__main__":
    unittest.main()

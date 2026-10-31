"""Cast RTP/RTCP wire-format checks for CastRtpSender (offline, no network sent).

The crypto and the feedback parser both have known-answer tests; this closes the
gap on the packet builder itself - the 18-byte RTP+Cast header layout and the
Sender Report - so a future edit to the struct formats fails loudly.
"""
import struct
import unittest

from streamer import mirror
from streamer.mirror import CastRtpSender, OPUS_SAMPLES_PER_FRAME


def make_sender():
    # binds a UDP socket but sends nothing; dummy key/mask (crypto tested elsewhere)
    return CastRtpSender("127.0.0.1", 9, 0x11223344, 127, bytes(16), bytes(16))


class TestRtpPacket(unittest.TestCase):
    def test_header_layout(self):
        s = make_sender()
        self.addCleanup(s.stop)
        s._seq, s._rtp_ts = 7, 480
        pkt = s._build_packet(b"payload", frame_id=5)
        v_p_x_cc, m_pt, seq, ts, ssrc = struct.unpack(">BBHII", pkt[:12])
        self.assertEqual(v_p_x_cc, 0x80)             # V2, no pad/ext, CC0
        self.assertEqual(m_pt, 0x80 | 127)           # marker + payload type 127
        self.assertEqual(seq, 7)
        self.assertEqual(ts, 480)
        self.assertEqual(ssrc, 0x11223344)
        flags, fid, pid, maxpid = struct.unpack(">BBHH", pkt[12:18])
        self.assertEqual(flags, 0x80)                # key-frame bit, ext count 0
        self.assertEqual(fid, 5)
        self.assertEqual((pid, maxpid), (0, 0))      # one packet per frame
        self.assertEqual(pkt[18:], b"payload")

    def test_frame_id_truncates_to_8_bits(self):
        s = make_sender()
        self.addCleanup(s.stop)
        pkt = s._build_packet(b"x", frame_id=0x105)   # 261
        self.assertEqual(pkt[13], 0x05)

    def test_send_frame_advances_counters(self):
        s = make_sender()
        self.addCleanup(s.stop)
        s.send_frame(b"abc")
        s.send_frame(b"defgh")
        self.assertEqual(s._frame_id, 2)
        self.assertEqual(s._seq, 2)
        self.assertEqual(s._rtp_ts, 2 * OPUS_SAMPLES_PER_FRAME)
        self.assertEqual(s._octets, 3 + 5)
        self.assertEqual(s.last_sent_frame_id, 1)


class TestSenderReport(unittest.TestCase):
    def test_sr_layout(self):
        s = make_sender()
        self.addCleanup(s.stop)
        s._packets, s._octets = 3, 300
        sr = s._build_sr(rtp_ts=960)
        ver_rc, pt, length = struct.unpack(">BBH", sr[:4])
        self.assertEqual(ver_rc, 0x80)
        self.assertEqual(pt, 200)                    # RTCP Sender Report
        self.assertEqual(length, 6)                  # 32-bit words minus one
        ssrc, _ntp_s, _ntp_f, rtp_ts, pkts, octs = struct.unpack(">IIIIII", sr[4:28])
        self.assertEqual(ssrc, 0x11223344)
        self.assertEqual(rtp_ts, 960)
        self.assertEqual(pkts, 3)
        self.assertEqual(octs, 300)


class FakeSock:
    """Records sendto() so re-sends can be checked by frame id."""
    def __init__(self):
        self.sent = []

    def sendto(self, data, dest):
        self.sent.append(data)

    def close(self):
        pass


def sent_frame_ids(sock):
    # RTP packets only (byte1 has the marker bit); frame id low byte at [13]
    return [pkt[13] for pkt in sock.sent if pkt[1] & 0x80 and pkt[1] != 200]


class TestKickstart(unittest.TestCase):
    """A frozen checkpoint gets the frames past it re-sent without a NACK."""

    def make(self, frames=20, checkpoint=9, frozen_since=100.0):
        s = make_sender()
        self.addCleanup(s.stop)
        for _ in range(frames):
            s.send_frame(b"opus")
        s._sock.close()
        s._sock = FakeSock()
        s.stats.checkpoint = checkpoint
        s.stats.checkpoint_raw = checkpoint & 0xFF
        s.stats.checkpoint_raw_since = frozen_since
        return s

    def test_no_kick_before_threshold(self):
        s = self.make()
        self.assertEqual(s.maybe_kickstart(100.0 + mirror.KICKSTART_AFTER / 2), 0)
        self.assertEqual(s._sock.sent, [])
        self.assertEqual(s.stats.kickstarts, 0)

    def test_kick_resends_frames_after_checkpoint(self):
        s = self.make()
        n = s.maybe_kickstart(101.0)
        self.assertEqual(n, mirror.KICKSTART_FRAMES)
        self.assertEqual(sent_frame_ids(s._sock),
                         list(range(10, 10 + mirror.KICKSTART_FRAMES)))
        self.assertEqual(s.stats.kickstarts, 1)

    def test_kick_capped_at_last_sent(self):
        s = self.make(frames=12, checkpoint=9)          # only 10, 11 outstanding
        self.assertEqual(s.maybe_kickstart(101.0), 2)
        self.assertEqual(sent_frame_ids(s._sock), [10, 11])

    def test_no_kick_when_receiver_caught_up(self):
        s = self.make(frames=10, checkpoint=9)          # last_sent == checkpoint
        self.assertEqual(s.maybe_kickstart(101.0), 0)
        self.assertEqual(s.stats.kickstarts, 0)

    def test_no_kick_before_first_checkpoint(self):
        s = self.make(checkpoint=-1, frozen_since=0.0)
        self.assertEqual(s.maybe_kickstart(101.0), 0)

    def test_kicks_are_spaced_by_interval(self):
        s = self.make()
        s.maybe_kickstart(101.0)
        self.assertEqual(s.maybe_kickstart(101.0 + mirror.KICKSTART_INTERVAL / 2), 0)
        self.assertGreater(s.maybe_kickstart(101.0 + mirror.KICKSTART_INTERVAL + 0.01), 0)
        self.assertEqual(s.stats.kickstarts, 2)

    def test_wrapped_history_slot_is_not_resent(self):
        # 300 frames sent: slot (10 & 0xFF) now holds frame 266, not frame 10.
        # Re-sending 266 as "frame 10" would be wrong, so nothing goes out.
        s = self.make(frames=300, checkpoint=9)
        self.assertEqual(s.maybe_kickstart(101.0), 0)
        self.assertEqual(s._sock.sent, [])

    def test_cleared_stall_is_logged(self):
        s = self.make()
        s.maybe_kickstart(101.0)
        s.maybe_kickstart(101.5)
        s.stats.checkpoint_raw_since = 101.7             # checkpoint moved
        with self.assertLogs("streamer.mirror", "INFO") as cm:
            s.maybe_kickstart(101.8)
        self.assertIn("stall cleared after 1.7 s (2 kickstarts)", cm.output[0])

    def test_single_kick_clear_is_not_logged(self):
        # one kick is routinely just slow feedback; don't spam the log
        s = self.make()
        s.maybe_kickstart(101.0)
        s.stats.checkpoint_raw_since = 101.1
        with self.assertNoLogs("streamer.mirror", "INFO"):
            s.maybe_kickstart(101.2)


class TestRetransmitStats(unittest.TestCase):
    def test_counts_resends_and_misses(self):
        s = make_sender()
        self.addCleanup(s.stop)
        for _ in range(5):
            s.send_frame(b"opus")
        s._sock.close()
        s._sock = FakeSock()
        s._retransmit([(3, 0, 0), (200, 0, 0)])          # 200 never sent
        self.assertEqual(s.stats.retransmits, 1)
        self.assertEqual(s.stats.retx_misses, 1)
        self.assertEqual(sent_frame_ids(s._sock), [3])

    def test_stall_report_fields(self):
        s = make_sender()
        self.addCleanup(s.stop)
        for _ in range(20):
            s.send_frame(b"opus")
        s.stats.checkpoint = 9
        s.stats.checkpoint_raw_since = 97.0
        s.stats.last_feedback_at = 99.8
        r = s.stall_report(100.0)
        for part in ("checkpoint=9", "last_sent=19", "in_flight=10", "frozen=3.0s",
                     "feedback_age=0.2s", "last_nack=never",
                     "next_frame_in_history=True"):
            self.assertIn(part, r)


if __name__ == "__main__":
    unittest.main()

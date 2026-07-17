"""Cast RTP/RTCP wire-format checks for CastRtpSender (offline, no network sent).

The crypto and the feedback parser both have known-answer tests; this closes the
gap on the packet builder itself - the 18-byte RTP+Cast header layout and the
Sender Report - so a future edit to the struct formats fails loudly.
"""
import struct
import unittest

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


if __name__ == "__main__":
    unittest.main()

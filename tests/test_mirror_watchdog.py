"""MirrorSession failure detection + recovery, on fakes (no hardware/DLL).

Covers the reviewer-flagged subtlety (C3): a wedged receiver that still sends
RTCP must be caught by the RAW checkpoint byte freezing, not by expanded-lag
magnitude (which re-expands to look like progress).
"""
import unittest

from streamer import mirror
from streamer.capture import CaptureFormat

FMT_48K = CaptureFormat(rate=48000, channels=2, sampwidth=2)
FMT_44K = CaptureFormat(rate=44100, channels=2, sampwidth=2)


class FakeStats:
    def __init__(self, **kw):
        self.d = {"rtcp_datagrams": 0, "cast_feedbacks": 0, "checkpoint": -1,
                  "playout_delay_ms": 400, "nack_events": 0,
                  "first_feedback_at": None, "last_feedback_at": None,
                  "checkpoint_raw_since": 0.0}
        self.d.update(kw)

    def snapshot(self):
        return dict(self.d)


class FakeSender:
    def __init__(self, **kw):
        self.stats = FakeStats(**kw)

    def stop(self):
        pass


class FakeCapture:
    def __init__(self, healthy=True, fmt=FMT_48K):
        self.healthy = healthy
        self.format = fmt


class FakeCast:
    name = "Speaker"

    class socket_client:
        host = "192.168.1.5"


def make_session(capture=None, sender=None, monkey_ip="192.168.1.9"):
    s = mirror.MirrorSession(discovery=None, safe_cast=FakeCast(),
                             capture=capture or FakeCapture())
    s._host = "192.168.1.5"
    s._local_ip = monkey_ip
    s._sender = sender
    return s


class TestFailureReason(unittest.TestCase):
    def setUp(self):
        # stub source_ip_for so no real socket is opened; matches _local_ip
        self._orig = mirror.__dict__  # not used; patch caster instead
        import streamer.caster as caster
        self._caster = caster
        self._orig_ip = caster.source_ip_for
        caster.source_ip_for = lambda host: "192.168.1.9"

    def tearDown(self):
        self._caster.source_ip_for = self._orig_ip

    def test_healthy_returns_none(self):
        s = make_session(sender=FakeSender(checkpoint=500, last_feedback_at=99.9,
                                           checkpoint_raw_since=99.9))
        self.assertIsNone(s._failure_reason(now=100.0))

    def test_capture_unhealthy(self):
        s = make_session(capture=FakeCapture(healthy=False),
                         sender=FakeSender(checkpoint=500))
        self.assertEqual(s._failure_reason(100.0), "capture unhealthy")

    def test_format_change(self):
        s = make_session(capture=FakeCapture(fmt=FMT_44K),
                         sender=FakeSender(checkpoint=500))
        self.assertEqual(s._failure_reason(100.0), "capture format changed")

    def test_local_ip_change(self):
        self._caster.source_ip_for = lambda host: "10.0.0.1"   # differs
        s = make_session(sender=FakeSender(checkpoint=500))
        self.assertEqual(s._failure_reason(100.0), "local IP changed")

    def test_rtcp_silence(self):
        s = make_session(sender=FakeSender(checkpoint=500, last_feedback_at=90.0,
                                           checkpoint_raw_since=90.0))
        # 100 - 90 = 10 s > 6 s
        self.assertEqual(s._failure_reason(100.0), "rtcp silence")

    def test_checkpoint_stall_on_raw_byte(self):
        # Wedged receiver STILL sends feedback (last_feedback_at recent) but the
        # raw checkpoint byte hasn't moved for > 2 s. Expanded lag would look
        # fine; only the raw-since check catches this.
        s = make_session(sender=FakeSender(checkpoint=500, last_feedback_at=99.9,
                                           checkpoint_raw_since=97.0))
        self.assertEqual(s._failure_reason(100.0), "checkpoint stalled")

    def test_recent_raw_movement_is_healthy(self):
        s = make_session(sender=FakeSender(checkpoint=500, last_feedback_at=99.9,
                                           checkpoint_raw_since=99.5))
        self.assertIsNone(s._failure_reason(100.0))

    def test_no_checkpoint_yet_not_flagged_as_stall(self):
        # Before the first checkpoint, a zero raw_since must not read as stalled.
        s = make_session(sender=FakeSender(checkpoint=-1, last_feedback_at=99.9,
                                           checkpoint_raw_since=0.0))
        self.assertIsNone(s._failure_reason(100.0))


class TestRecovery(unittest.TestCase):
    def test_reoffer_exhaustion_returns_false(self):
        s = make_session(sender=FakeSender())
        calls = []
        s._establish = lambda: calls.append("establish")   # succeeds
        s._await_playing = lambda: False                    # never reaches PLAYING
        self.assertFalse(s._recover("rtcp silence", backoff=0.0))
        self.assertEqual(len(calls), mirror.REOFFER_MAX_ATTEMPTS)
        self.assertEqual(s.recast_count, mirror.REOFFER_MAX_ATTEMPTS)

    def test_reoffer_success_returns_true(self):
        s = make_session(sender=FakeSender())
        s._establish = lambda: None
        s._await_playing = lambda: True
        self.assertTrue(s._recover("rtcp silence", backoff=0.0))
        self.assertEqual(s.recast_count, 1)

    def test_reoffer_establish_error_then_gives_up(self):
        s = make_session(sender=FakeSender())
        def boom():
            raise RuntimeError("launch failed")
        s._establish = boom
        s._await_playing = lambda: True
        self.assertFalse(s._recover("app died", backoff=0.0))
        self.assertEqual(s.recast_count, 0)   # never established


if __name__ == "__main__":
    unittest.main()

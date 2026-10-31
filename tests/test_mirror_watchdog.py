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
    app_id = "85CDB22F"

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
        # raw checkpoint byte hasn't moved past the stall timeout. Expanded lag
        # would look fine; only the raw-since check catches this.
        s = make_session(sender=FakeSender(checkpoint=500, last_feedback_at=99.9,
                                           checkpoint_raw_since=96.0))
        self.assertEqual(s._failure_reason(100.0), "checkpoint stalled")

    def test_kickstart_window_is_not_a_stall(self):
        # 2.3 s frozen (a live kickstart-cleared stall) must not rebuild
        s = make_session(sender=FakeSender(checkpoint=500, last_feedback_at=99.9,
                                           checkpoint_raw_since=97.7))
        self.assertIsNone(s._failure_reason(100.0))

    def test_group_member_drop_is_reported(self):
        s = make_session(sender=FakeSender(checkpoint=500, last_feedback_at=99.9,
                                           checkpoint_raw_since=99.9))
        s._members.reset(now=0.0)
        s._members.observe("Kitchen", "705D30C6", 1.0)
        s._members.dropped(10.0)                       # arms
        s._members.observe("Kitchen", None, 97.0)
        self.assertEqual(s._failure_reason(100.0),
                         "group member dropped: Kitchen")

    def test_recent_raw_movement_is_healthy(self):
        s = make_session(sender=FakeSender(checkpoint=500, last_feedback_at=99.9,
                                           checkpoint_raw_since=99.5))
        self.assertIsNone(s._failure_reason(100.0))

    def test_no_checkpoint_yet_not_flagged_as_stall(self):
        # Before the first checkpoint, a zero raw_since must not read as stalled.
        s = make_session(sender=FakeSender(checkpoint=-1, last_feedback_at=99.9,
                                           checkpoint_raw_since=0.0))
        self.assertIsNone(s._failure_reason(100.0))

    def test_receiver_app_changed(self):
        # Someone cast something else to the speaker: app_id no longer ours.
        s = make_session(sender=FakeSender(checkpoint=500, last_feedback_at=99.9,
                                           checkpoint_raw_since=99.9))
        s._app_id = "85CDB22F"
        s._cast = FakeCast()
        s._cast.app_id = "CC1AD845"          # a different receiver
        self.assertEqual(s._failure_reason(100.0), "receiver app changed")

    def test_matching_app_is_healthy(self):
        s = make_session(sender=FakeSender(checkpoint=500, last_feedback_at=99.9,
                                           checkpoint_raw_since=99.9))
        s._app_id = "85CDB22F"               # matches FakeCast.app_id
        self.assertIsNone(s._failure_reason(100.0))

    def test_format_change_is_a_permanent_reason(self):
        self.assertIn("capture format changed", mirror.PERMANENT_REASONS)

    def test_transient_none_app_id_is_not_a_change(self):
        # A re-OFFER briefly leaves the receiver appless; a None must not read
        # as "someone else cast to it" (RTCP silence catches a dead receiver).
        s = make_session(sender=FakeSender(checkpoint=500, last_feedback_at=99.9,
                                           checkpoint_raw_since=99.9))
        s._app_id = "85CDB22F"
        s._cast = FakeCast()
        s._cast.app_id = None
        self.assertIsNone(s._failure_reason(100.0))


class TestGroupMemberWatch(unittest.TestCase):
    ARM = mirror.GroupMemberWatch.ARM_DELAY
    GRACE = mirror.GroupMemberWatch.DROP_GRACE

    def armed(self, **apps):
        w = mirror.GroupMemberWatch()
        w.reset(now=0.0)
        for name, app in apps.items():
            w.observe(name, app, 1.0)
        self.assertIsNone(w.dropped(self.ARM))         # arms, nothing dropped
        return w

    def test_nothing_reported_before_arming(self):
        w = mirror.GroupMemberWatch()
        w.reset(now=0.0)
        w.observe("Kitchen", "705D30C6", 1.0)
        w.observe("Kitchen", None, 2.0)                # launch churn
        self.assertIsNone(w.dropped(self.ARM - 0.1))

    def test_joined_member_going_idle_is_reported_after_grace(self):
        w = self.armed(Kitchen="705D30C6", Office="705D30C6")
        w.observe("Office", None, 10.0)
        self.assertIsNone(w.dropped(10.0 + self.GRACE / 2))
        self.assertEqual(w.dropped(10.0 + self.GRACE), "Office")

    def test_brief_blip_is_not_reported(self):
        w = self.armed(Kitchen="705D30C6")
        w.observe("Kitchen", None, 10.0)
        w.observe("Kitchen", "705D30C6", 10.5)
        self.assertIsNone(w.dropped(20.0))

    def test_member_cast_to_elsewhere_is_released(self):
        # someone put Spotify on one speaker: their choice, not a drop
        w = self.armed(Kitchen="705D30C6")
        w.observe("Kitchen", "CC32E753", 10.0)
        w.observe("Kitchen", None, 30.0)               # ...and later stopped it
        self.assertIsNone(w.dropped(40.0))

    def test_member_never_joined_is_not_watched(self):
        w = self.armed(Kitchen="705D30C6", Office=None)
        self.assertIsNone(w.dropped(40.0))
        w.observe("Office", "CC32E753", 41.0)          # late, other app
        w.observe("Office", None, 42.0)
        self.assertIsNone(w.dropped(50.0))

    def test_waits_to_arm_until_someone_joins(self):
        w = mirror.GroupMemberWatch()
        w.reset(now=0.0)
        w.observe("Kitchen", None, 1.0)
        self.assertIsNone(w.dropped(self.ARM + 1))     # not armed yet
        w.observe("Kitchen", "705D30C6", 7.0)
        self.assertIsNone(w.dropped(8.0))              # arms now
        w.observe("Kitchen", None, 9.0)
        self.assertEqual(w.dropped(9.0 + self.GRACE), "Kitchen")

    def test_reset_rearms_for_new_stream(self):
        w = self.armed(Kitchen="705D30C6")
        w.observe("Kitchen", None, 10.0)
        w.reset(now=11.0)                              # our relaunch
        self.assertIsNone(w.dropped(12.0))
        w.observe("Kitchen", "705D30C6", 13.0)
        self.assertIsNone(w.dropped(11.0 + self.ARM))

    def test_disable_stops_reports(self):
        w = self.armed(Kitchen="705D30C6")
        w.observe("Kitchen", None, 10.0)
        w.disable()
        self.assertIsNone(w.dropped(20.0))


class TestMemberRejoinBudget(unittest.TestCase):
    def test_budget_then_watch_disabled(self):
        s = make_session(sender=FakeSender())
        for i in range(mirror.MEMBER_REJOIN_MAX):
            self.assertTrue(s._member_rejoin_allowed(100.0 + i))
        with self.assertLogs("streamer.mirror", "WARNING"):
            self.assertFalse(s._member_rejoin_allowed(110.0))
        self.assertIsNone(s._members._arm_at)          # disabled

    def test_budget_refills_after_window(self):
        s = make_session(sender=FakeSender())
        for i in range(mirror.MEMBER_REJOIN_MAX):
            s._member_rejoin_allowed(100.0 + i)
        self.assertTrue(s._member_rejoin_allowed(
            100.0 + mirror.MEMBER_REJOIN_WINDOW + 10))


class TestSetTargetDelay(unittest.TestCase):
    def test_sets_target_and_flags_reoffer(self):
        s = make_session(sender=FakeSender())
        s.set_target_delay(50)
        self.assertEqual(s._target_delay, 50)
        self.assertTrue(s._reoffer_requested.is_set())


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

    def test_first_retry_is_fast_then_backs_off(self):
        s = make_session(sender=FakeSender())
        waits = []

        class FakeStop:
            def is_set(self):
                return False

            def wait(self, t):
                waits.append(t)
                return False
        s._stop = FakeStop()
        s._establish = lambda: None
        s._await_playing = lambda: False
        s._recover("checkpoint stalled", backoff=2.0)
        self.assertEqual(waits, [mirror.FIRST_RETRY_DELAY, 2.0])

    def test_member_drop_quits_app_before_relaunch(self):
        s = make_session(sender=FakeSender())
        calls = []
        s._quit_receiver_app = lambda: calls.append("quit")
        s._establish = lambda: calls.append("establish")
        s._await_playing = lambda: True
        self.assertTrue(s._recover("group member dropped: Kitchen", backoff=0.0))
        self.assertEqual(calls, ["quit", "establish"])

    def test_stall_recovery_does_not_quit_app(self):
        s = make_session(sender=FakeSender())
        calls = []
        s._quit_receiver_app = lambda: calls.append("quit")
        s._establish = lambda: calls.append("establish")
        s._await_playing = lambda: True
        s._recover("checkpoint stalled", backoff=0.0)
        self.assertEqual(calls, ["establish"])

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

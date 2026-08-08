"""Output-mode actuation + the feed-silence gate (fakes, no audio hardware).

Covers _apply_output_mode per mode, the auto initial state, the auto stale
-guard, and that _pacer_sink feeds zeros when silenced.
"""
import unittest

import streamer.appctl as appctl
import streamer.localmute as localmute


class FakeMute:
    def __init__(self):
        self.engaged = False
        self.releases = 0
        self.pin = None
        self.enforce = None
        self.armed = False

    def arm(self, on_foreign):
        self.armed = True
        self.on_foreign = on_foreign

    def disarm(self):
        self.armed = False

    def engage(self, pin=True, enforce=False):
        self.engaged = True
        self.pin = pin
        self.enforce = enforce

    def set_policy(self, pin=None, enforce=None):
        if pin is not None:
            self.pin = pin
        if enforce is not None:
            self.enforce = enforce

    def release(self):
        self.engaged = False
        self.releases += 1


def make_ctl(couples=True):
    ctl = appctl.AppController.__new__(appctl.AppController)      # skip __init__
    ctl.cfg = {"output_mode": "speakers"}
    ctl.mute = FakeMute()
    ctl._cast_silenced = False
    ctl._output_mode = "speakers"
    ctl._couples = couples
    ctl._auto_settle = None
    ctl._following_user = False
    ctl.cast_target = "Kitchen"
    ctl.session = object()
    ctl._notify = lambda *a: None
    ctl.enqueue = lambda fn: fn()                # run ops inline in tests
    return ctl


class TestApplyOutputMode(unittest.TestCase):
    def setUp(self):
        self._orig = localmute.endpoint_muted

    def tearDown(self):
        localmute.endpoint_muted = self._orig

    def test_speakers_mutes_and_casts_full(self):
        ctl = make_ctl()
        ctl._apply_output_mode("speakers")
        self.assertTrue(ctl.mute.engaged)
        self.assertFalse(ctl._cast_silenced)

    def test_speakers_pins_volume_when_capture_couples(self):
        ctl = make_ctl(couples=True)         # endpoint loopback path
        ctl._apply_output_mode("speakers")
        self.assertTrue(ctl.mute.pin)

    def test_speakers_no_pin_when_capture_decoupled(self):
        ctl = make_ctl(couples=False)        # process-loopback path
        ctl._apply_output_mode("speakers")
        self.assertFalse(ctl.mute.pin)

    def test_this_pc_unmutes_and_silences_cast(self):
        ctl = make_ctl()
        ctl._apply_output_mode("this_pc")
        self.assertFalse(ctl.mute.engaged)
        self.assertTrue(ctl._cast_silenced)

    def test_both_unmutes_and_casts(self):
        ctl = make_ctl()
        ctl._apply_output_mode("both")
        self.assertFalse(ctl.mute.engaged)
        self.assertFalse(ctl._cast_silenced)

    def test_auto_pc_muted_house_live(self):
        localmute.endpoint_muted = lambda: True
        ctl = make_ctl()
        ctl._apply_output_mode("auto")
        self.assertFalse(ctl._cast_silenced)     # PC muted -> speakers play

    def test_auto_pc_unmuted_desk_only(self):
        localmute.endpoint_muted = lambda: False
        ctl = make_ctl()
        ctl._apply_output_mode("auto")
        self.assertTrue(ctl._cast_silenced)      # PC unmuted -> desk only

    def test_unknown_mode_falls_back_to_speakers(self):
        ctl = make_ctl()
        ctl._apply_output_mode("bogus")
        self.assertTrue(ctl.mute.engaged)
        self.assertEqual(ctl._output_mode, "speakers")

    def test_speakers_does_not_hold_the_mute(self):
        ctl = make_ctl()
        ctl._apply_output_mode("speakers")
        self.assertFalse(ctl.mute.enforce)       # a starting position, not a lock

    def test_speakers_locked_holds_the_mute(self):
        ctl = make_ctl()
        ctl._apply_output_mode("speakers_locked")
        self.assertTrue(ctl.mute.engaged)
        self.assertTrue(ctl.mute.enforce)
        self.assertFalse(ctl._cast_silenced)


class TestFollowUser(unittest.TestCase):
    """'speakers' treats the mute key as intent: un-muting mid-cast moves the
    live cast to 'both' semantics instead of being fought."""

    def test_unmute_stops_pinning_and_keeps_casting(self):
        ctl = make_ctl(couples=True)
        ctl._apply_output_mode("speakers")
        self.assertTrue(ctl.mute.pin)
        ctl._do_follow_user(muted=False)
        self.assertTrue(ctl._following_user)
        self.assertFalse(ctl.mute.pin)           # loudness is the user's again
        self.assertFalse(ctl._cast_silenced)     # speakers keep playing

    def test_remute_restores_the_pin(self):
        ctl = make_ctl(couples=True)
        ctl._apply_output_mode("speakers")
        ctl._do_follow_user(muted=False)
        ctl._do_follow_user(muted=True)
        self.assertFalse(ctl._following_user)
        self.assertTrue(ctl.mute.pin)

    def test_decoupled_capture_never_pins(self):
        ctl = make_ctl(couples=False)            # process loopback
        ctl._apply_output_mode("speakers")
        ctl._do_follow_user(muted=False)
        ctl._do_follow_user(muted=True)
        self.assertFalse(ctl.mute.pin)

    def test_locked_mode_does_not_follow(self):
        ctl = make_ctl()
        ctl._apply_output_mode("speakers_locked")
        ctl._do_follow_user(muted=False)
        self.assertFalse(ctl._following_user)

    def test_ignored_when_no_cast(self):
        ctl = make_ctl()
        ctl._apply_output_mode("speakers")
        ctl.cast_target = None
        ctl._do_follow_user(muted=False)
        self.assertFalse(ctl._following_user)

    def test_repeated_same_state_is_a_no_op(self):
        ctl = make_ctl()
        ctl._apply_output_mode("speakers")
        ctl._do_follow_user(muted=False)
        ctl.mute.pin = "sentinel"
        ctl._do_follow_user(muted=False)         # same state again
        self.assertEqual(ctl.mute.pin, "sentinel")

    def test_apply_mode_resets_following(self):
        ctl = make_ctl()
        ctl._apply_output_mode("speakers")
        ctl._do_follow_user(muted=False)
        ctl._apply_output_mode("speakers")       # user re-picks the mode
        self.assertFalse(ctl._following_user)


class TestAutoSilenceGuard(unittest.TestCase):
    def test_ignored_when_not_auto(self):
        ctl = make_ctl()
        ctl._output_mode = "speakers"
        ctl._do_auto_silence(True)
        self.assertFalse(ctl._cast_silenced)

    def test_ignored_when_no_cast(self):
        ctl = make_ctl()
        ctl._output_mode = "auto"
        ctl.cast_target = None
        ctl._do_auto_silence(True)
        self.assertFalse(ctl._cast_silenced)

    def test_applies_in_auto(self):
        ctl = make_ctl()
        ctl._output_mode = "auto"
        ctl._do_auto_silence(True)
        self.assertTrue(ctl._cast_silenced)


class TestFeedSilenceGate(unittest.TestCase):
    def _ctl_with_server(self):
        ctl = make_ctl()
        self.fed = []
        ctl.server = type("S", (), {"feed": lambda _s, c: self.fed.append(c)})()
        ctl._mirror_feed = None
        return ctl

    def test_silenced_feeds_zeros(self):
        ctl = self._ctl_with_server()
        ctl._cast_silenced = True
        ctl._pacer_sink(b"abcd")
        self.assertEqual(self.fed[0], b"\x00\x00\x00\x00")

    def test_live_feeds_real_audio(self):
        ctl = self._ctl_with_server()
        ctl._cast_silenced = False
        ctl._pacer_sink(b"abcd")
        self.assertEqual(self.fed[0], b"abcd")

    def test_silence_reaches_mirror_too(self):
        ctl = self._ctl_with_server()
        mirror_fed = []
        ctl._mirror_feed = mirror_fed.append
        ctl._cast_silenced = True
        ctl._pacer_sink(b"xyz")
        self.assertEqual(mirror_fed[0], b"\x00\x00\x00")


if __name__ == "__main__":
    unittest.main()

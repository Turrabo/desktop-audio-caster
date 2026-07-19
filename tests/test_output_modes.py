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

    def engage(self, pin=True):
        self.engaged = True
        self.pin = pin

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
    ctl._auto_stop = None
    ctl.cast_target = "Kitchen"
    ctl.session = object()
    ctl._notify = lambda *a: None
    ctl._start_auto_monitor = lambda: None       # don't spawn a thread in tests
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

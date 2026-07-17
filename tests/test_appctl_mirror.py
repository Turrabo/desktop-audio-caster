"""AppController mirror wiring: mode resolution + the fallback stale-guard.

Discovery is faked so no zeroconf/network is touched; nothing casts.
"""
import unittest

import streamer.appctl as appctl
from streamer.capture import CaptureFormat
from streamer import _opus

FMT_48K = CaptureFormat(48000, 2, 2)
FMT_44K = CaptureFormat(44100, 2, 2)


class FakeDiscovery:
    def __init__(self, *a, **k):
        pass

    def list_devices(self):
        return []

    def stop(self):
        pass


class _Stopped:
    def __init__(self):
        self.stopped = False

    def stop(self):
        self.stopped = True


def make_ctl():
    ctl = appctl.AppController.__new__(appctl.AppController)     # skip __init__
    ctl.cfg = {"cast_mode": "auto", "mirror_target_delay_ms": 400}
    ctl.session = None
    ctl.cast_target = None
    ctl.cast_mode_active = "http"
    ctl._mirror_feed = None
    return ctl


class TestModeResolution(unittest.TestCase):
    def test_http_mode_disables_mirror(self):
        ctl = make_ctl()
        ctl.cfg["cast_mode"] = "http"
        ok, why = ctl._mirror_available(FMT_48K)
        self.assertFalse(ok)
        self.assertIn("http", why)

    def test_ineligible_format_disables_mirror(self):
        ctl = make_ctl()
        ok, why = ctl._mirror_available(FMT_44K)
        self.assertFalse(ok)
        self.assertIn("48", why)

    @unittest.skipUnless(_opus.available(), "opus.dll not loadable")
    def test_eligible_enables_mirror(self):
        ctl = make_ctl()
        ok, why = ctl._mirror_available(FMT_48K)
        self.assertTrue(ok)
        self.assertIsNone(why)


class TestFallbackStaleGuard(unittest.TestCase):
    def test_stale_fallback_is_noop(self):
        # A fallback enqueued for a session the user already replaced must not
        # touch the current session (re-entrancy guard).
        ctl = make_ctl()
        current = _Stopped()
        ctl.session = current
        ctl.cast_target = "Speaker"
        old = _Stopped()
        ctl._do_fallback(old, "rtcp silence")
        self.assertFalse(old.stopped)          # never stopped
        self.assertIs(ctl.session, current)    # untouched

    def test_fallback_after_stop_is_noop(self):
        ctl = make_ctl()
        ctl.session = None
        ctl.cast_target = None
        old = _Stopped()
        ctl._do_fallback(old, "rtcp silence")
        self.assertFalse(old.stopped)


if __name__ == "__main__":
    unittest.main()

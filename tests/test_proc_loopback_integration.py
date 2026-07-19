"""Volume-decoupling integration: the mute layer's conditional volume pin and
the open_capture backend choice (fakes only - no COM, no audio hardware).

The live COM capture is validated by hand (the spike), not here.
"""
import json
import unittest

import streamer.capture as capture
import streamer.localmute as localmute


class FakeEndpoint:
    def __init__(self, mute=0, vol=0.5):
        self._mute = mute
        self._vol = vol

    def GetMute(self):
        return self._mute

    def GetMasterVolumeLevelScalar(self):
        return self._vol

    def SetMute(self, m, _ctx):
        self._mute = m

    def SetMasterVolumeLevelScalar(self, v, _ctx):
        self._vol = v


class MuteTestBase(unittest.TestCase):
    def setUp(self):
        self.ep = FakeEndpoint()
        self._orig_ep = localmute._endpoint
        self._orig_marker = localmute._marker_path
        localmute._endpoint = lambda: self.ep
        self._marker = {}                       # in-memory marker
        localmute._marker_path = lambda: _FakePath(self._marker)

    def tearDown(self):
        localmute._endpoint = self._orig_ep
        localmute._marker_path = self._orig_marker


class _FakePath:
    """Just enough Path surface for localmute's marker I/O, backed by a dict."""
    def __init__(self, store):
        self._store = store

    def write_text(self, text, encoding=None):
        self._store["text"] = text

    def read_text(self, encoding=None):
        return self._store["text"]

    def exists(self):
        return "text" in self._store

    def unlink(self, missing_ok=False):
        self._store.pop("text", None)


class TestMutePinGating(MuteTestBase):
    def test_pin_true_pins_and_restores_volume(self):
        self.ep = FakeEndpoint(mute=0, vol=0.5)
        localmute._endpoint = lambda: self.ep
        m = localmute.LocalMute()
        m.engage(pin=True)
        self.assertEqual(self.ep._mute, 1)
        self.assertEqual(self.ep._vol, 1.0)          # pinned to full
        m.release()
        self.assertEqual(self.ep._mute, 0)
        self.assertEqual(self.ep._vol, 0.5)          # restored

    def test_pin_false_leaves_volume_untouched(self):
        self.ep = FakeEndpoint(mute=0, vol=0.5)
        localmute._endpoint = lambda: self.ep
        m = localmute.LocalMute()
        m.engage(pin=False)
        self.assertEqual(self.ep._mute, 1)           # still muted
        self.assertEqual(self.ep._vol, 0.5)          # NOT pinned
        self.ep._vol = 0.7                           # user changes it mid-cast
        m.release()
        self.assertEqual(self.ep._mute, 0)
        self.assertEqual(self.ep._vol, 0.7)          # left as the user set it

    def test_marker_records_pin(self):
        m = localmute.LocalMute()
        m.engage(pin=False)
        saved = json.loads(self._marker["text"])
        self.assertFalse(saved["pin"])
        m.release()


class TestCrashRecovery(MuteTestBase):
    def test_recover_restores_volume_only_when_pinned(self):
        self._marker["text"] = json.dumps({"mute": 0, "volume": 0.3, "pin": True})
        self.ep = FakeEndpoint(mute=1, vol=0.9)
        localmute._endpoint = lambda: self.ep
        localmute.recover_from_crash()
        self.assertEqual(self.ep._vol, 0.3)
        self.assertEqual(self.ep._mute, 0)
        self.assertNotIn("text", self._marker)       # marker cleared

    def test_recover_leaves_volume_when_not_pinned(self):
        self._marker["text"] = json.dumps({"mute": 0, "volume": 1.0, "pin": False})
        self.ep = FakeEndpoint(mute=1, vol=0.7)
        localmute._endpoint = lambda: self.ep
        localmute.recover_from_crash()
        self.assertEqual(self.ep._vol, 0.7)          # untouched
        self.assertEqual(self.ep._mute, 0)


class _FakeCapture:
    def __init__(self, couples):
        self.couples_volume = couples
        self.started = False

    def start(self):
        self.started = True


class TestOpenCaptureRouting(unittest.TestCase):
    def setUp(self):
        self._orig_lb = capture.LoopbackCapture
        self._orig_pl_cap = capture.ProcessLoopbackCapture
        self._orig_supported = capture._pl.supported
        self.made = {}

        def fake_lb(on_data, device_hint=None):
            c = _FakeCapture(couples=True)
            self.made["lb"] = c
            return c

        def fake_pl(on_data):
            c = _FakeCapture(couples=False)
            self.made["pl"] = c
            return c

        capture.LoopbackCapture = fake_lb
        capture.ProcessLoopbackCapture = fake_pl

    def tearDown(self):
        capture.LoopbackCapture = self._orig_lb
        capture.ProcessLoopbackCapture = self._orig_pl_cap
        capture._pl.supported = self._orig_supported

    def test_device_hint_forces_endpoint(self):
        capture._pl.supported = lambda: True
        cap = capture.open_capture(lambda d: None, device_hint="Speakers")
        self.assertTrue(cap.couples_volume)
        self.assertIn("lb", self.made)
        self.assertNotIn("pl", self.made)

    def test_proc_when_supported_and_no_hint(self):
        capture._pl.supported = lambda: True
        cap = capture.open_capture(lambda d: None, device_hint=None)
        self.assertFalse(cap.couples_volume)
        self.assertIn("pl", self.made)

    def test_endpoint_when_unsupported(self):
        capture._pl.supported = lambda: False
        cap = capture.open_capture(lambda d: None, device_hint=None)
        self.assertTrue(cap.couples_volume)
        self.assertIn("lb", self.made)

    def test_proc_failure_falls_back_to_endpoint(self):
        capture._pl.supported = lambda: True

        def boom(on_data):
            raise OSError("activation failed")

        capture.ProcessLoopbackCapture = boom
        cap = capture.open_capture(lambda d: None, device_hint=None)
        self.assertTrue(cap.couples_volume)          # fell back
        self.assertIn("lb", self.made)


class TestReadLoopErrorHandling(unittest.TestCase):
    """comtypes raises COMError on a failed HRESULT and COMError is NOT an
    OSError subclass. A narrower catch here would let a device-invalidated
    error kill the capture thread silently, leaving healthy=True so no watchdog
    ever recovers - a permanently silent cast. Guard that with real COMErrors."""

    def test_comerror_is_not_oserror(self):
        # The premise. If this ever becomes false the broad catches can narrow.
        import comtypes
        self.assertFalse(issubclass(comtypes.COMError, OSError))

    def _cap(self):
        cap = capture.ProcessLoopbackCapture.__new__(capture.ProcessLoopbackCapture)
        cap.format = capture.CaptureFormat(48000, 2, 2)
        cap.healthy = True
        cap.restart_count = 0
        cap._stop = __import__("threading").Event()
        cap._on_data = lambda b: None
        cap._evt = None
        return cap

    def test_com_error_triggers_reactivate_not_death(self):
        import comtypes
        cap = self._cap()
        boom = comtypes.COMError(-2004287484, "AUDCLNT_E_DEVICE_INVALIDATED", None)

        class Dead:
            def GetNextPacketSize(self):
                raise boom

        cap._capture = Dead()
        calls = {"n": 0}

        def fake_reactivate():
            calls["n"] += 1
            cap._stop.set()          # stop after the first recovery attempt
            return True

        cap._reactivate = fake_reactivate
        # Patch the event wait so the loop does not block on a real handle.
        orig_wait = capture._pl.wait_event
        capture._pl.wait_event = lambda h, ms: 0
        try:
            cap._read_loop()
        finally:
            capture._pl.wait_event = orig_wait
        self.assertEqual(calls["n"], 1)      # recovery ran; thread did not die

    def test_release_buffer_runs_even_if_on_data_raises(self):
        cap = self._cap()
        released = []

        class Client:
            def __init__(self):
                self.packets = [4, 0]

            def GetNextPacketSize(self):
                return self.packets.pop(0) if self.packets else 0

            def GetBuffer(self):
                return (ctypes_buf(), 4, 0, 0, 0)

            def ReleaseBuffer(self, frames):
                released.append(frames)
                cap._stop.set()

        def ctypes_buf():
            import ctypes
            return ctypes.cast(ctypes.create_string_buffer(16),
                               ctypes.POINTER(ctypes.c_ubyte))

        cap._capture = Client()
        cap._on_data = lambda b: (_ for _ in ()).throw(ValueError("sink blew up"))
        cap._reactivate = lambda: False
        orig_wait = capture._pl.wait_event
        capture._pl.wait_event = lambda h, ms: 0
        try:
            cap._read_loop()
        finally:
            capture._pl.wait_event = orig_wait
        # The packet was released despite on_data raising; without this the next
        # GetBuffer would fail AUDCLNT_E_OUT_OF_ORDER.
        self.assertEqual(released, [4])


if __name__ == "__main__":
    unittest.main()

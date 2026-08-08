"""LocalMute's response to someone ELSE changing the endpoint (fakes, no COM).

The old design re-asserted its mute on a 5 s poll, so a mute key press leaked
up to five seconds of audio into the room and was then silently overridden.
Changes now arrive by notification and the default is to FOLLOW the user;
holding the mute down is opt-in.
"""
import json
import unittest

import streamer.localmute as localmute


class FakeEndpoint:
    def __init__(self, mute=0, vol=0.5):
        self._mute = mute
        self._vol = vol
        self.writes = []

    def GetMute(self):
        return self._mute

    def GetMasterVolumeLevelScalar(self):
        return self._vol

    def SetMute(self, m, ctx):
        self._mute = m
        self.writes.append(("mute", m, ctx))

    def SetMasterVolumeLevelScalar(self, v, ctx):
        self._vol = v
        self.writes.append(("vol", v, ctx))


class _FakePath:
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


class MuteBase(unittest.TestCase):
    def setUp(self):
        self.ep = FakeEndpoint(mute=0, vol=0.4)
        self._orig_ep = localmute._endpoint
        self._orig_marker = localmute._marker_path
        localmute._endpoint = lambda: self.ep
        self._marker = {}
        localmute._marker_path = lambda: _FakePath(self._marker)
        self.m = localmute.LocalMute()

    def tearDown(self):
        try:
            self.m.disarm()
        except Exception:
            pass
        localmute._endpoint = self._orig_ep
        localmute._marker_path = self._orig_marker


class TestFollowsTheUser(MuteBase):
    def test_foreign_unmute_is_not_undone(self):
        self.m.engage(pin=False, enforce=False)
        self.assertEqual(self.ep._mute, 1)
        self.ep._mute = 0                        # the user hit the mute key
        self.m._handle_foreign(muted=False, volume=0.4)
        self.assertEqual(self.ep._mute, 0)       # left alone

    def test_foreign_unmute_becomes_the_restore_target(self):
        """Leaving the endpoint where the user last put it beats restoring a
        pre-cast state they have since overridden."""
        self.m.engage(pin=False, enforce=False)
        self.m._handle_foreign(muted=False, volume=0.4)
        self.m.release()
        self.assertEqual(self.ep._mute, 0)

    def test_listener_is_told(self):
        seen = []
        self.m.arm(lambda muted, vol: seen.append((muted, vol)))
        self.m.engage(pin=False, enforce=False)
        self.m._handle_foreign(muted=False, volume=0.4)
        self.assertEqual(seen, [(False, 0.4)])


class TestLockedMode(MuteBase):
    def test_foreign_unmute_is_undone(self):
        self.m.engage(pin=False, enforce=True)
        self.ep._mute = 0
        self.m._handle_foreign(muted=False, volume=0.4)
        self.assertEqual(self.ep._mute, 1)       # re-muted

    def test_prior_is_not_rewritten_by_our_own_correction(self):
        self.m.engage(pin=False, enforce=True)
        self.m._handle_foreign(muted=False, volume=0.4)
        self.m.release()
        self.assertEqual(self.ep._mute, 0)       # the true pre-cast state


class TestVolumePin(MuteBase):
    """The pin is mechanism, not policy: on the coupled capture path volume 0
    means a dead cast, so it is defended even in the following mode."""

    def test_volume_drop_while_muted_is_repinned(self):
        self.m.engage(pin=True, enforce=False)
        self.assertEqual(self.ep._vol, 1.0)
        self.ep._vol = 0.2
        self.m._handle_foreign(muted=True, volume=0.2)
        self.assertEqual(self.ep._vol, 1.0)

    def test_dropping_the_pin_hands_volume_back(self):
        self.m.engage(pin=True, enforce=False)
        self.m.set_policy(pin=False)
        self.ep._vol = 0.2
        self.m._handle_foreign(muted=True, volume=0.2)
        self.assertEqual(self.ep._vol, 0.2)      # no longer defended

    def test_dropping_the_pin_stops_restoring_the_old_volume(self):
        self.m.engage(pin=True, enforce=False)
        self.m.set_policy(pin=False)
        self.ep._vol = 0.75                      # user's own choice
        self.m.release()
        self.assertEqual(self.ep._vol, 0.75)

    def test_marker_tracks_the_dropped_pin(self):
        self.m.engage(pin=True, enforce=False)
        self.m.set_policy(pin=False)
        self.assertFalse(json.loads(self._marker["text"])["pin"])


class TestWriteTagging(MuteBase):
    def test_every_write_carries_the_app_context(self):
        """Untagged writes come back as GUID_NULL - indistinguishable from the
        mute key - and the watcher would fight its own restore."""
        self.m.engage(pin=True, enforce=False)
        self.m.release()
        self.assertTrue(self.ep.writes)
        for _kind, _value, ctx in self.ep.writes:
            self.assertIs(ctx, localmute.APP_CTX)


class TestDoubleEngage(MuteBase):
    def test_second_engage_does_not_overwrite_prior(self):
        self.m.engage(pin=False, enforce=False)
        self.m.engage(pin=False, enforce=False)   # re-selecting the same mode
        self.m.release()
        self.assertEqual(self.ep._mute, 0)        # not left muted

    def test_second_engage_updates_policy(self):
        self.m.engage(pin=False, enforce=False)
        self.m.engage(pin=False, enforce=True)
        self.ep._mute = 0
        self.m._handle_foreign(muted=False, volume=0.4)
        self.assertEqual(self.ep._mute, 1)        # now holding

    def test_release_twice_is_safe(self):
        self.m.engage(pin=True, enforce=False)
        self.m.release()
        self.ep._vol = 0.9
        self.m.release()
        self.assertEqual(self.ep._vol, 0.9)       # second release is a no-op


if __name__ == "__main__":
    unittest.main()

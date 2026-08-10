"""LocalMute's response to someone ELSE changing the endpoint (fakes, no COM).

The old design re-asserted its mute on a 5 s poll, so a mute key press leaked
up to five seconds of audio into the room and was then silently overridden.
The poll is now 0.25 s and the default is to FOLLOW the user; holding the mute
down is opt-in.

_speakers MUST stay patched alongside _endpoint, or these tests reach the
machine's real audio device.
"""
import json
import unittest

import streamer.localmute as localmute


class FakeEndpoint:
    """Includes the notification registration methods. Without them every
    arm() in the suite raised inside the COM thread and was swallowed, so the
    watcher was never actually registered and the tests that claimed to cover
    the armed paths were silently exercising the failure branch instead."""

    def __init__(self, mute=0, vol=0.5):
        self._mute = mute
        self._vol = vol
        self.writes = []
        self.callbacks = []

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

    def RegisterControlChangeNotify(self, cb):
        self.callbacks.append(cb)

    def UnregisterControlChangeNotify(self, cb):
        if cb in self.callbacks:
            self.callbacks.remove(cb)


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


class FakeDevice:
    """Stands in for the pycaw AudioDevice that _speakers() returns."""

    def __init__(self, endpoint, device_id="fake-endpoint-0"):
        self.EndpointVolume = endpoint
        self.id = device_id


class FakeEnumerator:
    """Records device-notification registrations instead of reaching the real
    MMDeviceEnumerator, so that path is exercised rather than skipped."""

    def __init__(self):
        self.callbacks = []

    def RegisterEndpointNotificationCallback(self, cb):
        self.callbacks.append(cb)

    def UnregisterEndpointNotificationCallback(self, cb):
        if cb in self.callbacks:
            self.callbacks.remove(cb)


class MuteBase(unittest.TestCase):
    """Isolates the tests from the machine's real audio device.

    _speakers MUST be patched, not just _endpoint: arm() resolves the device
    through _speakers() and holds its EndpointVolume, and engage() then prefers
    that held interface over _endpoint(). Patching only _endpoint left arm()
    reaching the real hardware, so running the suite muted and un-muted the
    machine's actual speakers - and left a muted machine un-muted.
    """

    def setUp(self):
        self.ep = FakeEndpoint(mute=0, vol=0.4)
        self._orig_ep = localmute._endpoint
        self._orig_speakers = localmute._speakers
        self._orig_marker = localmute._marker_path
        self._orig_enum = localmute.AudioUtilities.GetDeviceEnumerator
        localmute._endpoint = lambda: self.ep
        localmute._speakers = lambda: FakeDevice(self.ep)
        self.enum = FakeEnumerator()
        localmute.AudioUtilities.GetDeviceEnumerator = lambda: self.enum
        self._marker = {}
        localmute._marker_path = lambda: _FakePath(self._marker)
        self.m = localmute.LocalMute()

    def tearDown(self):
        try:
            self.m.disarm()
        except Exception:
            pass
        localmute._endpoint = self._orig_ep
        localmute._speakers = self._orig_speakers
        localmute._marker_path = self._orig_marker
        localmute.AudioUtilities.GetDeviceEnumerator = self._orig_enum


class TestFollowsTheUser(MuteBase):
    def test_foreign_unmute_is_not_undone(self):
        self.m.engage(pin=False, enforce=False)
        self.assertEqual(self.ep._mute, 1)
        self.ep._mute = 0                        # the user hit the mute key
        self.m._observe(muted=False, volume=0.4)
        self.assertEqual(self.ep._mute, 0)       # left alone

    def test_foreign_unmute_becomes_the_restore_target(self):
        """Leaving the endpoint where the user last put it beats restoring a
        pre-cast state they have since overridden."""
        self.m.engage(pin=False, enforce=False)
        self.m._observe(muted=False, volume=0.4)
        self.m.release()
        self.assertEqual(self.ep._mute, 0)

    def test_listener_is_told(self):
        """_observe reports upward by RETURNING; the watch loop makes the call
        outside the lock so a listener can never re-enter and deadlock."""
        self.m.engage(pin=False, enforce=False)
        self.assertEqual(self.m._observe(muted=False, volume=0.4), (False, 0.4))


class TestLockedMode(MuteBase):
    def test_foreign_unmute_is_undone(self):
        self.m.engage(pin=False, enforce=True)
        self.ep._mute = 0
        self.m._observe(muted=False, volume=0.4)
        self.assertEqual(self.ep._mute, 1)       # re-muted

    def test_prior_is_not_rewritten_by_our_own_correction(self):
        self.m.engage(pin=False, enforce=True)
        self.m._observe(muted=False, volume=0.4)
        self.m.release()
        self.assertEqual(self.ep._mute, 0)       # the true pre-cast state


class TestVolumePin(MuteBase):
    """The pin is mechanism, not policy: on the coupled capture path volume 0
    means a dead cast, so it is defended even in the following mode."""

    def test_volume_drop_while_muted_is_repinned(self):
        self.m.engage(pin=True, enforce=False)
        self.assertEqual(self.ep._vol, 1.0)
        self.ep._vol = 0.2
        self.m._observe(muted=True, volume=0.2)
        self.assertEqual(self.ep._vol, 1.0)

    def test_dropping_the_pin_hands_volume_back(self):
        self.m.engage(pin=True, enforce=False)
        self.m.set_policy(pin=False)
        self.ep._vol = 0.2
        self.m._observe(muted=True, volume=0.2)
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


class TestMarkerOrdering(MuteBase):
    """The recovery marker goes down BEFORE the endpoint is touched. Muting with
    no marker is the one combination that can leave a machine silently muted
    forever: a hard kill skips atexit and the next launch has nothing to
    restore from."""

    def _break_marker(self):
        class DeadPath:
            def write_text(self, _t, encoding=None):
                raise OSError("disk full")

            def exists(self):
                return False

            def unlink(self, missing_ok=False):
                pass
        localmute._marker_path = lambda: DeadPath()

    def test_engage_fails_rather_than_muting_without_a_marker(self):
        self._break_marker()
        with self.assertRaises(OSError):
            self.m.engage(pin=True, enforce=False)
        self.assertEqual(self.ep._mute, 0)        # never touched
        self.assertEqual(self.ep._vol, 0.4)       # never pinned
        self.assertFalse(self.m.engaged)

    def test_marker_describes_the_pinned_state_it_is_about_to_create(self):
        self.m.engage(pin=True, enforce=False)
        self.assertTrue(json.loads(self._marker["text"])["pin"])


class TestWriteTagging(MuteBase):
    def test_every_write_carries_the_app_context(self):
        """Untagged writes come back as GUID_NULL - indistinguishable from the
        mute key - and the watcher would fight its own restore."""
        self.m.engage(pin=True, enforce=False)
        self.m.release()
        self.assertTrue(self.ep.writes)
        for _kind, _value, ctx in self.ep.writes:
            self.assertIs(ctx, localmute.APP_CTX)


class TestRestoreTarget(MuteBase):
    """_prior is the pre-cast snapshot and nothing else. Only an OBSERVED mute
    change counts as the user's intent."""

    def test_volume_only_event_does_not_become_mute_intent(self):
        """The notification carries the CURRENT state, so while our own mute is
        held every unrelated volume event also reports muted=True. Treating that
        as 'the user wants mute' left the PC muted after the cast."""
        self.m.engage(pin=False, enforce=False)      # process-loopback path
        self.m._observe(muted=True, volume=0.55)   # volume changed only
        self.m.release()
        self.assertEqual(self.ep._mute, 0)           # pre-cast state

    def test_unmute_then_remute_restores_pre_cast_state(self):
        """Re-muting is the user cancelling their own override - they are
        sending audio back to the room, not asking to be left muted."""
        self.m.engage(pin=False, enforce=False)
        self.ep._mute = 0
        self.m._observe(muted=False, volume=0.4)
        self.ep._mute = 1
        self.m._observe(muted=True, volume=0.4)
        self.m.release()
        self.assertEqual(self.ep._mute, 0)

    def test_deliberate_unmute_survives_the_cast(self):
        """Pre-cast muted, user un-mutes mid-cast: they should not be re-muted
        when the cast ends."""
        self.ep._mute = 1
        self.m.engage(pin=False, enforce=False)
        self.ep._mute = 0
        self.m._observe(muted=False, volume=0.4)
        self.m.release()
        self.assertEqual(self.ep._mute, 0)

    def test_untouched_mute_restores_pre_cast_muted_state(self):
        self.ep._mute = 1
        self.m.engage(pin=False, enforce=False)
        self.m._observe(muted=True, volume=0.7)   # volume traffic only
        self.m.release()
        self.assertEqual(self.ep._mute, 1)

    def test_enforced_correction_is_not_user_intent(self):
        self.m.engage(pin=False, enforce=True)
        self.m._observe(muted=False, volume=0.4)  # undone by the hold
        self.m.release()
        self.assertEqual(self.ep._mute, 0)


class TestPinWrites(MuteBase):
    """Turning the pin on or off must WRITE, not just set a flag."""

    def test_dropping_the_pin_hands_back_the_users_volume(self):
        self.ep._vol = 0.30
        self.m.engage(pin=True, enforce=False)
        self.assertEqual(self.ep._vol, 1.0)
        self.m.set_policy(pin=False)
        self.assertEqual(self.ep._vol, 0.30)      # not our pinned 1.0

    def test_pin_drop_then_release_leaves_the_users_volume(self):
        self.ep._vol = 0.30
        self.m.engage(pin=True, enforce=False)
        self.m.set_policy(pin=False)
        self.ep._vol = 0.55                       # user adjusts after taking over
        self.m.release()
        self.assertEqual(self.ep._vol, 0.55)

    def test_re_enabling_the_pin_applies_it_immediately(self):
        self.m.engage(pin=True, enforce=False)
        self.m.set_policy(pin=False)
        self.ep._vol = 0.05                       # would scale the cast to 5%
        self.m.set_policy(pin=True)
        self.assertEqual(self.ep._vol, 1.0)

    def test_retaking_the_pin_snapshots_the_users_level(self):
        """While un-pinned the user may pick their own level. Re-taking the pin
        overwrites it with 100%, so it has to be captured first - otherwise
        release() hands back the stale pre-cast value and their choice is gone."""
        self.ep._vol = 0.30
        self.m.engage(pin=True, enforce=False)
        self.m.set_policy(pin=False)              # user un-mutes
        self.ep._vol = 0.80                       # user picks their own level
        self.m._poll_once()                       # the poll notices it
        self.m.set_policy(pin=True)               # user re-mutes
        self.assertEqual(self.ep._vol, 1.0)       # held again
        self.m.release()
        self.assertEqual(self.ep._vol, 0.80)      # theirs, not the stale 0.30

    def test_unmute_drops_the_pin_without_waiting_for_the_owner(self):
        """The corrective write must happen on the notification thread. Routed
        through the app's ops worker it lands whenever that worker is next
        free - seconds during a cast connect - and until then the user is
        un-muted at our pinned 100%."""
        self.ep._vol = 0.30
        self.m.engage(pin=True, enforce=False)
        self.ep._mute = 0
        self.m._observe(muted=False, volume=1.0)   # no owner callback
        self.assertEqual(self.ep._vol, 0.30)

    def test_pin_defence_only_while_actually_pinned(self):
        self.m.engage(pin=True, enforce=False)
        self.m.set_policy(pin=False)
        self.ep._vol = 0.2
        self.m._observe(muted=True, volume=0.2)
        self.assertEqual(self.ep._vol, 0.2)       # no longer defended


class TestReEngageAfterFollow(MuteBase):
    def _user_unmutes(self):
        """A real un-mute changes the endpoint and THEN notifies."""
        self.ep._mute = 0
        self.m._observe(muted=False, volume=self.ep._vol)

    def test_reselecting_speakers_remutes(self):
        self.m.engage(pin=False, enforce=False)
        self._user_unmutes()
        self.assertEqual(self.ep._mute, 0)
        self.m.engage(pin=False, enforce=False)           # user re-picks the mode
        self.assertEqual(self.ep._mute, 1)

    def test_switching_to_locked_after_a_follow_mutes(self):
        self.m.engage(pin=False, enforce=False)
        self._user_unmutes()
        self.m.engage(pin=False, enforce=True)
        self.assertEqual(self.ep._mute, 1)
        self._user_unmutes()
        self.assertEqual(self.ep._mute, 1)                # now holding

    def test_reselect_mutes_before_re_pinning(self):
        """Re-enabling the pin writes 100%. If that landed before the re-mute,
        the user would get a full-volume blast."""
        order = []
        self.ep.SetMute = lambda m, ctx: order.append(("mute", m))
        self.ep.SetMasterVolumeLevelScalar = lambda v, ctx: order.append(("vol", v))
        self.m.engage(pin=True, enforce=False)
        self.m.set_policy(pin=False)
        self.m._last_mute = False                  # user has us un-muted
        order.clear()
        self.m.engage(pin=True, enforce=False)     # re-picks speakers
        self.assertEqual(order[0], ("mute", 1))    # mute lands first
        self.assertIn(("vol", 1.0), order)

    def test_reselect_supersedes_the_earlier_override(self):
        self.ep._mute = 1                                  # pre-cast muted
        self.m.engage(pin=False, enforce=False)
        self._user_unmutes()
        self.m.engage(pin=False, enforce=False)            # re-picks speakers
        self.m.release()
        self.assertEqual(self.ep._mute, 1)                 # back to pre-cast


class TestEngageFailure(MuteBase):
    def test_engage_failure_reaches_the_caller(self):
        """A mode promising silence must not report success and then not mute."""
        def boom(*_a, **_k):
            raise OSError("endpoint gone")
        self.ep.GetMute = boom
        with self.assertRaises(OSError):
            self.m.engage(pin=False, enforce=False)


class TestPollLoop(MuteBase):
    """Drive _poll_once - the real comparison - rather than only calling
    _observe, so the read side is exercised too."""

    def test_poll_reports_a_user_unmute(self):
        self.m.engage(pin=False, enforce=False)
        self.m._poll_once()                      # baseline
        self.ep._mute = 0                        # user hits the mute key
        self.assertEqual(self.m._poll_once(), (False, 0.4))

    def test_poll_is_quiet_when_nothing_changes(self):
        self.m.engage(pin=False, enforce=False)
        self.m._poll_once()
        self.assertIsNone(self.m._poll_once())

    def test_poll_does_not_report_our_own_mute(self):
        """engage() records what it wrote, so the first poll must not mistake
        our own mute for the user muting."""
        self.m.engage(pin=False, enforce=False)
        self.assertIsNone(self.m._poll_once())

    def test_poll_reasserts_the_hold_in_locked_mode(self):
        self.m.engage(pin=False, enforce=True)
        self.m._poll_once()
        self.ep._mute = 0
        self.assertIsNone(self.m._poll_once())   # our correction, not intent
        self.assertEqual(self.ep._mute, 1)

    def test_poll_tracks_the_users_volume_while_unpinned(self):
        self.m.engage(pin=False, enforce=False)
        self.ep._vol = 0.66
        self.m._poll_once()
        self.m.release()
        self.assertEqual(self.ep._vol, 0.66)

    def test_watch_thread_starts_and_stops(self):
        self.m.arm(lambda *a: None)
        thread = self.m._thread
        self.assertTrue(thread.is_alive())
        self.m.disarm()
        thread.join(timeout=3)
        self.assertFalse(thread.is_alive())

    def test_arm_is_idempotent(self):
        self.m.arm(lambda *a: None)
        first = self.m._thread
        self.m.arm(lambda *a: None)
        self.assertIs(self.m._thread, first)


class TestDeviceChange(MuteBase):
    def _switch_device(self, new_id="dev-B"):
        """The default output becomes a different endpoint."""
        self.other = FakeEndpoint(mute=0, vol=0.9)
        localmute._speakers = lambda: FakeDevice(self.other, new_id)
        return self.m._poll_once()

    def test_same_device_is_not_treated_as_a_change(self):
        self.m.engage(pin=False, enforce=False)
        self.m._poll_once()
        self.m._poll_once()
        self.assertEqual(self.ep._mute, 1)

    def test_change_restores_the_old_device_and_takes_the_new_one(self):
        self.m.engage(pin=False, enforce=False)
        self.m._poll_once()
        self._switch_device()
        self.assertEqual(self.ep._mute, 0)        # old one put back
        self.assertEqual(self.other._mute, 1)     # new one silenced

    def test_change_carries_the_users_unmute(self):
        self.m.engage(pin=False, enforce=False)
        self.m._poll_once()
        self.ep._mute = 0
        self.m._poll_once()                       # user un-mutes, we follow
        self._switch_device()
        self.assertEqual(self.other._mute, 0)     # not silently re-muted

    def test_a_failed_poll_changes_nothing(self):
        """A transient resolve failure costs one cycle and nothing else - we
        still hold the device we muted, so the next poll simply retries. The
        previous design dropped the mute for the rest of the cast here."""
        self.m.engage(pin=False, enforce=False)
        self.m._poll_once()

        def boom():
            raise OSError("not ready")
        original, localmute._speakers = localmute._speakers, boom
        try:
            with self.assertRaises(OSError):
                self.m._poll_once()               # the watch loop catches this
        finally:
            localmute._speakers = original
        self.assertTrue(self.m.engaged)           # still holding
        self.assertEqual(self.ep._mute, 1)        # still silent
        self.assertIsNone(self.m._poll_once())    # and recovers


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
        self.m._observe(muted=False, volume=0.4)
        self.assertEqual(self.ep._mute, 1)        # now holding

    def test_release_twice_is_safe(self):
        self.m.engage(pin=True, enforce=False)
        self.m.release()
        self.ep._vol = 0.9
        self.m.release()
        self.assertEqual(self.ep._vol, 0.9)       # second release is a no-op


if __name__ == "__main__":
    unittest.main()


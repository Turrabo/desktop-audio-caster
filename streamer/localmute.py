"""Local endpoint control while casting.

engage() mutes the endpoint once at cast start. What happens NEXT is the
policy question, and there are two answers:

  enforce=False (the default) - the mute is a STARTING POSITION, not a lock. A
      foreign mute/un-mute is reported to the owner as user intent and the app
      follows it. Pressing the mute key mid-cast does what the user expects.
  enforce=True - the mute is HELD; a foreign un-mute is undone. Opt-in, for
      "keep the room silent whatever happens".

Either way changes arrive as WASAPI notifications
(IAudioEndpointVolumeCallback), not by polling, so the reaction is ~1-5 ms
instead of the old 5 s re-pin cycle. That interval was long enough that a mute
key press leaked several seconds of audio, which reads as a bug however it is
documented.

Own-vs-foreign is decided by the event-context GUID. Every write this module
makes is tagged with APP_CTX and notifications carrying it are ignored, so
there is no feedback loop. Everyone else's writes - including the keyboard mute
key, which reports GUID_NULL - are foreign.

THE VOLUME PIN IS DIFFERENT FROM THE MUTE. On the endpoint-loopback capture
path the endpoint volume scales the captured signal (volume 0 = dead capture),
so while we hold the mute the pin is defended even in the non-enforcing mode:
the user cannot be expressing an intent about loudness they can't hear, and the
alternative is a silently dead cast. That is mechanism, not policy. Once the
user un-mutes and the cast follows them to "both", the pin is dropped and
loudness is theirs again.

THREADING. One dedicated COM thread owns the endpoint entirely - registration,
reads, writes and teardown - because CoUninitialize is thread-affine and
comtypes only initialises the importing thread's apartment. Public methods
enqueue a command and wait for it, so callers (the ops worker, atexit) never
touch COM themselves. This mirrors ProcessLoopbackCapture's contract.

Crash-safety: a JSON marker records the prior state (including whether the
volume was pinned); if the app died hard (atexit never ran), the next start
restores the endpoint from the marker.
"""
from __future__ import annotations

import atexit
import json
import logging
import queue
import threading
from pathlib import Path

import comtypes
from comtypes import GUID
from pycaw.callbacks import AudioEndpointVolumeCallback, MMNotificationClient
from pycaw.pycaw import AudioUtilities

from .config import config_dir

log = logging.getLogger(__name__)

MARKER = "muted-by-streamer.marker"

# Identity stamped on every endpoint write this module makes, so the watcher can
# tell our own changes from the user's. Fixed and hard-coded on purpose: a GUID
# generated per run would stop crash recovery recognising a previous process's
# writes. Never regenerate this.
APP_CTX = GUID("{6F3A1C2E-7B41-4E9A-9D2F-0B5C8E1A4D33}")

# Endpoint calls are millisecond-scale, so this only ever bites when something
# is wedged. Kept short deliberately: release() + disarm() both wait on it
# during cast teardown, which itself sits inside shutdown()'s 6 s hard-exit
# budget. release() runs first, so even the worst case restores audio before
# the bail fires.
_COMMAND_TIMEOUT = 2.0


def _speakers():
    """Fresh IMMDevice for the current default render endpoint."""
    return AudioUtilities.GetSpeakers()


def _endpoint():
    """Fresh IAudioEndpointVolume. NOTE: a new COM object every call - a
    registration made on one of these dies with it, so anything that registers
    must HOLD its device object for as long as it wants notifications."""
    return _speakers().EndpointVolume


def _device_id(device) -> str | None:
    """Endpoint id, used to tell a real default-device change from a redundant
    notification. AudioUtilities.GetSpeakers() hands back a pycaw AudioDevice
    wrapper exposing `.id`; the raw IMMDevice's GetId() is the fallback in case
    a future pycaw returns the interface directly."""
    for get in (lambda: device.id, lambda: device.GetId()):
        try:
            value = get()
            if value:
                return str(value)
        except Exception:
            continue
    log.debug("could not read endpoint id; device-change detection degraded")
    return None


def _marker_path() -> Path:
    return config_dir() / MARKER


# pycaw's endpoint is a comtypes interface, so a failed HRESULT raises
# comtypes.COMError - which is NOT an OSError subclass. Every guard in this
# module therefore catches Exception: an endpoint hiccup must never crash the
# tray app, and release() in particular must never break cast teardown.


def endpoint_muted() -> bool:
    """Current endpoint mute state. Used for the initial reading when a cast
    starts; steady-state changes arrive by notification, not by polling this."""
    try:
        return bool(_endpoint().GetMute())
    except Exception:
        return False


def recover_from_crash() -> None:
    """Call at app start: if a marker survived a crash, restore the endpoint."""
    path = _marker_path()
    if not path.exists():
        return
    log.info("previous run left the endpoint modified - restoring")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        vol = _endpoint()
        # Only restore the volume if this run pinned it (older markers with no
        # "pin" key are from the pinning endpoint path - default True).
        if state.get("pin", True):
            vol.SetMasterVolumeLevelScalar(float(state.get("volume", 1.0)), APP_CTX)
        vol.SetMute(int(state.get("mute", 0)), APP_CTX)
    except Exception as e:
        log.warning("crash-recovery restore failed: %s", e)
    path.unlink(missing_ok=True)


class _VolumeWatch(AudioEndpointVolumeCallback):
    """Fires on an arbitrary Windows thread pool thread (the tid varies per
    event). Must be non-blocking by contract - Microsoft's rules for the sibling
    notification interfaces forbid waiting on a synchronisation object, calling
    Register/Unregister, or releasing the last reference to an MMDevice object
    from inside a callback. So: classify, hand off, return."""

    def __init__(self, on_change):
        super().__init__()
        self._on_change = on_change

    def on_notify(self, new_volume, new_mute, event_context, channels,
                  channel_volumes):
        try:
            # event_context points into an OS-owned struct that is only valid
            # for this call - compare in place, never stash the pointer.
            if event_context and event_context.contents == APP_CTX:
                return                      # our own write
        except Exception:
            pass                            # unreadable context -> treat as foreign
        self._on_change(bool(new_mute), float(new_volume))


class _DeviceWatch(MMNotificationClient):
    """Notices the default render endpoint changing under a live cast. Without
    this the watcher would stay bound to the old device: we would stop
    defending the endpoint that is actually playing AND restore our saved state
    onto the wrong device on release."""

    def __init__(self, on_default_render_changed):
        super().__init__()
        self._on_changed = on_default_render_changed

    # AudioUtilities.GetSpeakers() resolves eRender + eMultimedia specifically,
    # so only that pairing describes the device we actually hold. The
    # notification fires once PER ROLE, and the eCommunications default is
    # routinely a different device (a headset), so reacting to every role would
    # churn - releasing and re-engaging the same endpoint, un-muting audibly
    # each time - and never settle, because re-arming always resolves the
    # multimedia default straight back.
    WATCHED_FLOW = "eRender"
    WATCHED_ROLE = "eMultimedia"

    def on_default_device_changed(self, flow, flow_id, role, role_id,
                                  default_device_id):
        if str(flow) == self.WATCHED_FLOW and str(role) == self.WATCHED_ROLE:
            self._on_changed(default_device_id)


class LocalMute:
    def __init__(self):
        self._cmds: queue.Queue = queue.Queue()
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        # -- COM-thread-owned state (touch only from _run) --
        self._device = None          # held IMMDevice (registration lifetime)
        self._vol = None             # held IAudioEndpointVolume
        self._vol_cb = None
        self._enum = None
        self._dev_cb = None
        self._device_id: str | None = None
        # IMMUTABLE snapshot of the endpoint as we found it. Never rewritten
        # while a cast runs: it answers "what was here before us", and nothing
        # else. Conflating it with "what the user wants now" is how an earlier
        # version left the PC muted after a cast - any notification arriving
        # while our own mute was in effect looked like the user asking for mute.
        self._prior: dict | None = None
        # True once the user has taken the endpoint OUT of the mute we put it
        # in. Only an un-mute counts: we always mute, so a later re-mute is the
        # user cancelling their own override (they are sending audio back to the
        # room), not a new preference about where to leave the endpoint. Set
        # only on an OBSERVED CHANGE, so our own writes and unrelated volume
        # traffic can never masquerade as intent.
        self._user_unmuted = False
        self._last_mute: bool | None = None   # last mute state we wrote or saw
        self._pin = False                     # policy: should we hold 100%
        self._pinned_now = False              # fact: are we holding it
        # The volume release() should hand back. Starts as the pre-cast level
        # but is re-snapshotted whenever we TAKE the pin, because by then the
        # user may have chosen their own level while we were not holding it -
        # restoring the pre-cast value would silently discard that choice.
        self._volume_target = 1.0
        self._enforce = False
        self._on_foreign = None

    # -- public API (any thread; each call is executed on the COM thread) ----

    def arm(self, on_foreign) -> None:
        """Start watching the endpoint. on_foreign(muted, volume) is called from
        an internal thread whenever someone ELSE changes mute/volume; it must
        not block (the app enqueues onto its ops worker)."""
        self._call(lambda: self._do_arm(on_foreign))

    def engage(self, pin: bool = True, enforce: bool = False) -> None:
        """Mute the endpoint once, recording the prior state. When pin is True
        also hold the volume at 100% - needed only on the endpoint capture path
        where volume scales the capture.

        Raises if the endpoint could not be taken. A mode whose promise is
        "this PC stays quiet" must not report success and then not mute."""
        self._call(lambda: self._do_engage(bool(pin), bool(enforce)),
                   propagate=True)

    def set_policy(self, pin: bool | None = None, enforce: bool | None = None) -> None:
        """Adjust the live hold without tearing it down (e.g. dropping the pin
        when a foreign un-mute moves the cast to 'both')."""
        self._call(lambda: self._do_set_policy(pin, enforce))

    def release(self) -> None:
        """Restore the endpoint to its pre-cast state. Safe to call twice."""
        self._call(self._do_release)

    def disarm(self) -> None:
        """Stop watching and shut the COM thread down."""
        if self._thread is None:
            return
        self._call(self._do_disarm)
        self._stop.set()
        self._cmds.put(None)          # wake the loop so it can exit
        self._thread.join(timeout=_COMMAND_TIMEOUT)
        if self._thread.is_alive():
            # Safe to abandon: the next thread gets its own queue and stop
            # event, so a wedged one cannot consume or block its work.
            log.warning("endpoint COM thread did not exit; abandoning it")
        self._thread = None

    @property
    def engaged(self) -> bool:
        return self._prior is not None

    # -- command plumbing ---------------------------------------------------

    def _ensure_thread(self) -> None:
        """Start the COM thread on first use. Lazy rather than required, so
        engage()/release() work standalone - a caller that never armed still
        gets its mute, instead of a silent no-op.

        Each thread gets its OWN queue and stop event. Sharing them meant
        disarm()'s shutdown sentinel could survive unconsumed - the loop
        re-tests the stop flag before every get(), so any command arriving in
        that window is pulled first and the sentinel is left behind - and the
        NEXT thread would then read it as its own instruction to exit, making
        that cast's first command a two-second no-op.
        """
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop = threading.Event()
        self._cmds = queue.Queue()
        cmds, stop = self._cmds, self._stop
        self._thread = threading.Thread(target=lambda: self._run(cmds, stop),
                                        name="endpoint-com", daemon=True)
        self._thread.start()

    def _call(self, fn, propagate: bool = False) -> None:
        """Run fn on the COM thread and wait for it. Waiting keeps cast teardown
        ordered (the mute must really be gone before we report IDLE) and lets
        callers stay COM-free.

        propagate=True re-raises the command's exception on the calling thread.
        Used where failing silently would leave the app claiming something it is
        not doing - engage in particular, since a mode whose whole promise is
        "this PC stays quiet" must not report success and then not mute.
        Teardown paths leave it False: a release that fails should be logged,
        never allowed to break the rest of the teardown.
        """
        self._ensure_thread()
        done = threading.Event()
        box: list = []
        self._cmds.put((fn, done, box))
        if not done.wait(_COMMAND_TIMEOUT):
            log.warning("endpoint command timed out after %.0fs", _COMMAND_TIMEOUT)
            if propagate:
                raise TimeoutError("endpoint command timed out")
            return
        if box and propagate:
            raise box[0]

    def _run(self, cmds: queue.Queue, stop: threading.Event) -> None:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        try:
            while not stop.is_set():
                item = cmds.get()
                if item is None:
                    break
                fn, done, box = item
                try:
                    fn()
                except Exception as e:
                    log.warning("endpoint command failed: %s", e)
                    box.append(e)
                finally:
                    done.set()
        finally:
            # Release anyone still waiting rather than let them burn the full
            # timeout, and tell them the work did not run.
            while True:
                try:
                    item = cmds.get_nowait()
                except queue.Empty:
                    break
                if item is not None:
                    _fn, done, box = item
                    box.append(RuntimeError("endpoint thread shut down"))
                    done.set()
            try:
                self._do_disarm()
            except Exception:
                pass
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass

    # -- COM-thread implementations -----------------------------------------

    def _do_arm(self, on_foreign) -> None:
        self._on_foreign = on_foreign
        if self._vol is not None:
            return                                  # already armed
        self._device = _speakers()
        self._device_id = _device_id(self._device)
        self._vol = self._device.EndpointVolume
        self._vol_cb = _VolumeWatch(self._on_volume_event)
        self._vol.RegisterControlChangeNotify(self._vol_cb)
        try:
            self._enum = AudioUtilities.GetDeviceEnumerator()
            self._dev_cb = _DeviceWatch(self._on_device_event)
            self._enum.RegisterEndpointNotificationCallback(self._dev_cb)
        except Exception as e:
            # Losing default-device tracking degrades gracefully (we keep
            # defending the device we started on); losing the volume watch
            # would not, which is why that one is not in this try.
            log.warning("default-device watch unavailable: %s", e)
            self._enum = self._dev_cb = None
        log.debug("endpoint watch armed on %s", self._device_id)

    def _do_engage(self, pin: bool, enforce: bool) -> None:
        if self._prior is not None:
            # Already engaged. Re-capturing _prior here would record the
            # ALREADY-muted state and leave the PC muted after release. But the
            # user may have un-muted and been followed, so re-selecting a
            # speakers mode has to actually re-assert the mute - otherwise the
            # UI reports "muted" over an audible PC, and the mode the user just
            # picked does nothing until they next touch the mute key.
            # Re-assert the mute BEFORE applying policy. Re-enabling the pin
            # writes the volume to 100%, and doing that while the user currently
            # has us un-muted would be an audible full-volume blast - the same
            # "mute first, then raise volume" invariant the fresh path keeps.
            if self._last_mute is False:
                log.info("speakers mode re-selected while following - re-muting")
                self._safe_write(lambda v: v.SetMute(1, APP_CTX))
                self._last_mute = True
                self._user_unmuted = False   # an explicit pick supersedes it
            self._do_set_policy(pin, enforce)
            self._write_marker()
            return
        vol = self._vol if self._vol is not None else _endpoint()
        self._pin, self._enforce = pin, enforce
        self._prior = {"mute": vol.GetMute(),
                       "volume": vol.GetMasterVolumeLevelScalar()}
        self._volume_target = float(self._prior["volume"])
        self._user_unmuted = False
        self._pinned_now = bool(pin)      # intent, so the marker describes the
                                          # state the endpoint is about to be in
        # The marker goes down BEFORE any endpoint write, and a failure here is
        # fatal to the engage rather than logged and ignored. Muting with no
        # recovery marker is the one combination that can leave a machine
        # silently muted forever: a hard kill (including shutdown()'s os._exit
        # bail) skips atexit, and the next launch has nothing to restore from.
        # Better to fail the cast loudly with the PC still audible.
        try:
            _marker_path().write_text(json.dumps(self._restore_target()),
                                      encoding="utf-8")
        except OSError:
            self._prior = None
            self._pinned_now = False
            raise
        # Order matters: mute FIRST, then raise volume - never audible.
        vol.SetMute(1, APP_CTX)
        self._last_mute = True
        if pin:
            vol.SetMasterVolumeLevelScalar(1.0, APP_CTX)
        atexit.register(self._atexit_restore)
        log.info("local output muted%s%s (was mute=%s vol=%.2f)",
                 ", volume pinned to 100%" if pin else "",
                 ", held" if enforce else "",
                 self._prior["mute"], self._prior["volume"])

    def _restore_target(self) -> dict:
        """What release() should put back: the pre-cast snapshot, unless the user
        overrode our mute and never took it back - having deliberately un-muted,
        they should not be re-muted when the cast ends."""
        if self._prior is None:
            return {}
        mute = 0 if self._user_unmuted else int(self._prior["mute"])
        return {"mute": int(mute), "volume": float(self._volume_target),
                "pin": bool(self._pinned_now)}

    def _write_marker(self) -> None:
        if self._prior is None:
            return
        try:
            _marker_path().write_text(json.dumps(self._restore_target()),
                                      encoding="utf-8")
        except OSError as e:
            log.debug("marker rewrite failed: %s", e)

    def _read_volume(self) -> float | None:
        try:
            vol = self._vol if self._vol is not None else _endpoint()
            return float(vol.GetMasterVolumeLevelScalar())
        except Exception as e:
            log.debug("volume read failed: %s", e)
            return None

    def _apply_pin(self, enabled: bool) -> None:
        """Take or release the 100% hold, WRITING the endpoint each time.

        Setting a flag is not enough: it would only take effect on the next
        notification, which may never arrive. Taking the pin also re-snapshots
        the level the user is currently on, so release() hands back what they
        last chose rather than a stale pre-cast value.
        """
        enabled = bool(enabled)
        self._pin = enabled
        if self._prior is None or enabled == self._pinned_now:
            return
        if enabled:
            current = self._read_volume()
            if current is not None:
                self._volume_target = current
            self._safe_write(lambda v: v.SetMasterVolumeLevelScalar(1.0, APP_CTX))
        else:
            # Hand back the user's OWN level. The endpoint currently holds our
            # pinned 1.0, so simply letting go would leave them at full volume -
            # a blast the moment they un-mute, and their real level lost for good.
            self._safe_write(lambda v: v.SetMasterVolumeLevelScalar(
                float(self._volume_target), APP_CTX))
        self._pinned_now = enabled
        log.info("volume pin %s", "on" if enabled else "off")
        self._write_marker()

    def _do_set_policy(self, pin, enforce) -> None:
        if enforce is not None and enforce != self._enforce:
            self._enforce = bool(enforce)
            log.info("mute hold %s", "on" if enforce else "off")
        if pin is not None:
            self._apply_pin(pin)

    def _do_release(self) -> None:
        if self._prior is None:
            return
        target = self._restore_target()
        self._prior = None
        pinned, self._pinned_now = self._pinned_now, False
        self._user_unmuted = False
        self._last_mute = None
        try:
            vol = self._vol if self._vol is not None else _endpoint()
            # Restore volume FIRST (while still muted) and only if we are still
            # holding it. If the pin was dropped mid-cast the user's level was
            # already handed back at that moment, and whatever they set since is
            # theirs to keep.
            if pinned:
                vol.SetMasterVolumeLevelScalar(float(target["volume"]), APP_CTX)
            vol.SetMute(int(target["mute"]), APP_CTX)
            log.info("local output restored (mute=%s vol=%.2f pinned=%s)",
                     target["mute"], target["volume"], pinned)
        except Exception as e:
            log.warning("endpoint restore failed: %s", e)
        self._volume_target = 1.0
        # Both of these must run even if the restore above threw, and a failure
        # to clear the marker must not hide the atexit unregister behind it.
        try:
            _marker_path().unlink(missing_ok=True)
        except OSError as e:
            log.warning("could not clear the mute marker: %s", e)
        try:
            atexit.unregister(self._atexit_restore)
        except Exception:
            pass

    def _do_disarm(self) -> None:
        # Unregister BEFORE any restore writes: a watcher that is still live
        # sees our own restore as a foreign un-mute and (in enforce mode) undoes
        # it, leaving the machine muted against the user's original state.
        if self._vol is not None and self._vol_cb is not None:
            try:
                self._vol.UnregisterControlChangeNotify(self._vol_cb)
            except Exception as e:
                log.debug("volume unregister: %s", e)
        if self._enum is not None and self._dev_cb is not None:
            try:
                self._enum.UnregisterEndpointNotificationCallback(self._dev_cb)
            except Exception as e:
                log.debug("device unregister: %s", e)
        self._do_release()
        self._vol_cb = self._dev_cb = None
        self._vol = self._device = self._enum = None
        self._device_id = None

    # -- notification handling ----------------------------------------------

    def _on_volume_event(self, muted: bool, volume: float) -> None:
        """Called on an OS pool thread by _VolumeWatch (already filtered to
        foreign changes). Enqueue rather than act here."""
        self._cmds.put((lambda: self._handle_foreign(muted, volume),
                        threading.Event(), []))

    def _on_device_event(self, device_id) -> None:
        self._cmds.put((lambda: self._handle_device_change(device_id),
                        threading.Event(), []))

    def _handle_foreign(self, muted: bool, volume: float) -> None:
        if self._prior is not None:
            previous, self._last_mute = self._last_mute, muted
            if self._enforce and not muted:
                log.info("endpoint un-muted mid-cast - re-muting (hold on)")
                self._safe_write(lambda v: v.SetMute(1, APP_CTX))
                self._last_mute = True
                muted = True
            else:
                # Record intent only on an OBSERVED CHANGE of the mute. The
                # notification carries the endpoint's CURRENT state, so while we
                # hold our own mute every unrelated volume event also reports
                # muted=True; treating that as the user asking for mute is what
                # used to leave the PC muted after the cast.
                if previous is not None and muted != previous:
                    self._user_unmuted = not muted
                    if not muted and self._pinned_now:
                        # Release the hold HERE, on this thread. Routing it
                        # through the app's ops worker means it lands whenever
                        # that worker is next free - which is seconds while a
                        # cast is connecting - and until then the user is
                        # un-muted at our pinned 100%. The owner still asks for
                        # the same thing a moment later; that call is a no-op.
                        self._apply_pin(False)
                    self._write_marker()
                # Mechanism, not policy: on the coupled capture path a volume
                # drop would silently kill the cast, and the user cannot be
                # expressing an intent about loudness they can't hear.
                if self._pinned_now and muted and volume < 0.999:
                    log.info("endpoint volume dropped while muted - re-pinning 100%%")
                    self._safe_write(
                        lambda v: v.SetMasterVolumeLevelScalar(1.0, APP_CTX))
        cb = self._on_foreign
        if cb is not None:
            try:
                cb(muted, volume)
            except Exception as e:
                log.debug("foreign-change listener failed: %s", e)

    def _handle_device_change(self, device_id) -> None:
        if device_id == self._device_id:
            return
        log.info("default output changed mid-cast (%s -> %s)",
                 self._device_id, device_id)
        was_engaged = self._prior is not None
        pin, enforce = self._pin, self._enforce
        # Carry the user's override across: if they had un-muted and we were
        # following them, moving to a new device must not silently re-mute them.
        carried_user_unmuted = self._user_unmuted
        on_foreign = self._on_foreign
        # Each step is guarded separately. Re-arming the watcher is the most
        # failure-prone part (the new device may not be ready yet), and if it
        # took the mute down with it the whole point of the mode would be lost
        # for the rest of the cast - so engaging must still happen either way.
        try:
            self._do_disarm()           # restores + unregisters the old device
        except Exception as e:
            log.warning("releasing the old default failed: %s", e)
        try:
            self._do_arm(on_foreign)
        except Exception as e:
            log.warning("re-arming on the new default failed (%s); the endpoint "
                        "is still controlled, but changes are no longer watched", e)
        if not was_engaged:
            return
        try:
            self._do_engage(pin, enforce)
        except Exception as e:
            log.warning("re-engaging on the new default failed: %s", e)
            return
        if carried_user_unmuted and not enforce:
            log.info("carrying the user's un-mute onto the new default")
            self._safe_write(lambda v: v.SetMute(0, APP_CTX))
            self._last_mute = False
            self._user_unmuted = True
            self._write_marker()

    def _safe_write(self, fn) -> None:
        try:
            fn(self._vol if self._vol is not None else _endpoint())
        except Exception as e:
            log.debug("endpoint write failed: %s", e)

    # -- process exit -------------------------------------------------------

    def _atexit_restore(self) -> None:
        """Last-resort restore on interpreter exit. Deliberately does NOT touch
        the registration or the COM thread (which may already be gone): it just
        puts the endpoint back with a fresh handle. os._exit() skips atexit
        entirely, which is what the marker file is for."""
        if self._prior is None:
            return
        target = self._restore_target()
        pinned, self._pinned_now = self._pinned_now, False
        self._prior = None
        try:
            vol = _endpoint()
            if pinned:
                vol.SetMasterVolumeLevelScalar(float(target["volume"]), APP_CTX)
            vol.SetMute(int(target["mute"]), APP_CTX)
        except Exception as e:
            log.warning("atexit endpoint restore failed: %s", e)
        _marker_path().unlink(missing_ok=True)

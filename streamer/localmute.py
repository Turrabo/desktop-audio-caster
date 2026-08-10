"""Local endpoint control while casting.

engage() mutes the endpoint once at cast start. What happens NEXT is the
policy question, and there are two answers:

  enforce=False (the default) - the mute is a STARTING POSITION, not a lock. A
      foreign mute/un-mute is reported to the owner as user intent and the app
      follows it. Pressing the mute key mid-cast does what the user expects.
  enforce=True - the mute is HELD; a foreign un-mute is undone. Opt-in, for
      "keep the room silent whatever happens".

HOW CHANGES ARE NOTICED. One thread polls the endpoint every POLL_SECONDS and
compares against the state it last wrote or saw. A quarter second is quick
enough that the mute key feels immediate; the previous five-second cycle was
long enough to leak several seconds of audio into the room, which reads as a
bug however it is documented.

An earlier version of this module used WASAPI notifications
(IAudioEndpointVolumeCallback) for a ~2 ms reaction instead. It worked, but it
bought those two milliseconds with a long-lived COM registration whose lifetime
had to be managed across device changes, thread wedges and teardown, and that
lifetime turned out to be a reliable source of defects: watchers abandoned mid
-cast, registrations that failed silently and permanently, and teardown running
against the wrong cast's state. Polling holds no registration, so none of those
states exist. The reaction is 100x slower and still 20x faster than the
behaviour anyone complained about.

THE VOLUME PIN IS DIFFERENT FROM THE MUTE. On the endpoint-loopback capture
path the endpoint volume scales the captured signal (volume 0 = dead capture),
so while we hold the mute the pin is defended even in the non-enforcing mode:
the user cannot be expressing an intent about loudness they can't hear, and the
alternative is a silently dead cast. That is mechanism, not policy. Once the
user un-mutes and the cast follows them to "both", the pin is dropped and
loudness is theirs again.

THREADING. All endpoint work is serialised by one lock. The poll thread holds
it for each cycle; engage/release/set_policy hold it for their duration. The
poll thread initialises its own COM apartment; callers run on the app's ops
worker, which is serialised already.

Crash-safety: a JSON marker records the state to restore, written BEFORE the
endpoint is touched. If the app dies hard (atexit never ran), the next start
restores from it.
"""
from __future__ import annotations

import atexit
import json
import logging
import threading
from pathlib import Path

import comtypes
from comtypes import GUID
from pycaw.pycaw import AudioUtilities

from .config import config_dir

log = logging.getLogger(__name__)

MARKER = "muted-by-streamer.marker"

# How often the endpoint is compared against what we last wrote or saw. The
# worst-case delay before the app reacts to the mute key.
POLL_SECONDS = 0.25

# Stamped on every endpoint write this module makes. Nothing here depends on
# reading it back - polling compares against our own recorded state instead -
# but it identifies our writes to anything else on the system that looks.
APP_CTX = GUID("{6F3A1C2E-7B41-4E9A-9D2F-0B5C8E1A4D33}")


def _speakers():
    """Fresh AudioDevice for the current default render endpoint."""
    return AudioUtilities.GetSpeakers()


def _endpoint():
    """Fresh IAudioEndpointVolume for the current default render endpoint."""
    return _speakers().EndpointVolume


def _device_id(device) -> str | None:
    """Endpoint id, used to notice the default device changing under a cast.
    GetSpeakers() hands back a pycaw AudioDevice exposing `.id`; the raw
    IMMDevice's GetId() is a fallback in case a future pycaw returns the
    interface directly."""
    for get in (lambda: device.id, lambda: device.GetId()):
        try:
            value = get()
            if value:
                return str(value)
        except Exception:
            continue
    return None


def _marker_path() -> Path:
    return config_dir() / MARKER


# pycaw's endpoint is a comtypes interface, so a failed HRESULT raises
# comtypes.COMError - which is NOT an OSError subclass. Every guard in this
# module therefore catches Exception: an endpoint hiccup must never crash the
# tray app, and release() in particular must never break cast teardown.


def endpoint_muted() -> bool:
    """Current endpoint mute state, for the initial reading when a cast starts."""
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
        # Only restore the volume if that run pinned it (older markers with no
        # "pin" key are from the pinning endpoint path - default True).
        if state.get("pin", True):
            vol.SetMasterVolumeLevelScalar(float(state.get("volume", 1.0)), APP_CTX)
        vol.SetMute(int(state.get("mute", 0)), APP_CTX)
    except Exception as e:
        log.warning("crash-recovery restore failed: %s", e)
    try:
        path.unlink(missing_ok=True)
    except OSError as e:
        log.warning("could not clear the mute marker: %s", e)


class LocalMute:
    def __init__(self):
        self._lock = threading.RLock()
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._on_foreign = None
        # The device we are holding, kept for the life of the engage so that
        # release() restores the endpoint we actually muted rather than
        # whichever one happens to be default by then.
        self._device = None
        self._device_id: str | None = None
        # IMMUTABLE snapshot of the endpoint as we found it: what was here
        # before us, and nothing else.
        self._prior: dict | None = None
        # True once the user has taken the endpoint OUT of the mute we put it
        # in. Only an un-mute counts: we always mute, so a later re-mute is the
        # user cancelling their own override (sending audio back to the room),
        # not a new preference about where to leave the endpoint.
        self._user_unmuted = False
        self._last_mute: bool | None = None    # last state we wrote or saw
        self._pin = False                      # policy: should we hold 100%
        self._pinned_now = False               # fact: are we holding it
        self._enforce = False
        # The volume release() hands back. While we are NOT pinned the poll
        # keeps this on the user's live level, so it is always their latest
        # choice rather than a stale snapshot.
        self._volume_target = 1.0

    # -- public API ---------------------------------------------------------

    def arm(self, on_foreign) -> None:
        """Start watching the endpoint. on_foreign(muted, volume) is called from
        the poll thread whenever someone ELSE changes the mute; it must not
        block (the app enqueues onto its ops worker)."""
        with self._lock:
            self._on_foreign = on_foreign
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._watch_loop,
                                        name="endpoint-watch", daemon=True)
        self._thread.start()

    def disarm(self) -> None:
        """Stop watching and restore the endpoint."""
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None:
            thread.join(timeout=2)
        with self._lock:
            self._do_release()
            self._on_foreign = None

    def engage(self, pin: bool = True, enforce: bool = False) -> None:
        """Mute the endpoint once, recording the prior state. When pin is True
        also hold the volume at 100% - needed only on the endpoint capture path
        where volume scales the capture.

        Raises if the endpoint could not be taken: a mode whose promise is
        "this PC stays quiet" must not report success and then not mute."""
        with self._lock:
            self._do_engage(bool(pin), bool(enforce))

    def set_policy(self, pin: bool | None = None,
                   enforce: bool | None = None) -> None:
        """Adjust the live hold without tearing it down (e.g. dropping the pin
        when a foreign un-mute moves the cast to 'both')."""
        with self._lock:
            if enforce is not None and bool(enforce) != self._enforce:
                self._enforce = bool(enforce)
                log.info("mute hold %s", "on" if enforce else "off")
            if pin is not None:
                self._apply_pin(bool(pin))

    def release(self) -> None:
        """Restore the endpoint to its pre-cast state. Safe to call twice."""
        with self._lock:
            self._do_release()

    @property
    def engaged(self) -> bool:
        return self._prior is not None

    # -- the watch ----------------------------------------------------------

    def _watch_loop(self) -> None:
        comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
        try:
            while not self._stop.wait(POLL_SECONDS):
                pending = None
                try:
                    with self._lock:
                        pending = self._poll_once()
                except Exception as e:
                    log.debug("endpoint poll failed: %s", e)
                # Outside the lock: the owner's callback must never be able to
                # re-enter this module and deadlock against its own poll.
                if pending is not None:
                    cb = self._on_foreign
                    if cb is not None:
                        try:
                            cb(*pending)
                        except Exception as e:
                            log.debug("foreign-change listener failed: %s", e)
        finally:
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass

    def _poll_once(self):
        """One comparison against the endpoint. Returns (muted, volume) when the
        owner should be told the user changed something, else None."""
        device = _speakers()
        current_id = _device_id(device)
        if (self._prior is not None and self._device_id is not None
                and current_id != self._device_id):
            self._handle_device_change(device, current_id)
            return None
        if self._prior is None:
            self._device, self._device_id = device, current_id
        vol = device.EndpointVolume
        return self._observe(bool(vol.GetMute()),
                             float(vol.GetMasterVolumeLevelScalar()), vol)

    def _observe(self, muted: bool, volume: float, vol=None):
        """Compare an observation against what we last wrote or saw."""
        if vol is None:
            vol = self._writer()
        previous, self._last_mute = self._last_mute, muted
        changed = previous is not None and muted != previous
        if self._prior is None:
            # Watching without holding anything (the 'auto' output mode).
            return (muted, volume) if changed else None
        if not self._pinned_now:
            # Their live level, so release() hands back their latest choice.
            self._volume_target = volume
        if self._enforce and not muted:
            log.info("endpoint un-muted mid-cast - re-muting (hold on)")
            self._write(vol, lambda v: v.SetMute(1, APP_CTX))
            self._last_mute = True
            return None                      # our own correction, not intent
        if changed:
            self._user_unmuted = not muted
            if not muted and self._pinned_now:
                # Let go here, on this thread. The owner asks for the same thing
                # a moment later, but that request queues behind whatever the
                # ops worker is doing - seconds, during a cast connect - and
                # until then the user is un-muted at our pinned 100%.
                self._apply_pin(False, vol)
            self._write_marker()
            return (muted, volume)
        if self._pinned_now and muted and volume < 0.999:
            # Mechanism, not policy: on the coupled capture path a volume drop
            # would silently kill the cast.
            log.info("endpoint volume dropped while muted - re-pinning 100%%")
            self._write(vol, lambda v: v.SetMasterVolumeLevelScalar(1.0, APP_CTX))
        return None

    def _handle_device_change(self, device, new_id) -> None:
        """The default output changed under a live cast: put the old endpoint
        back and take the new one. Single-threaded, so there is no window where
        neither is owned."""
        log.info("default output changed mid-cast (%s -> %s)",
                 self._device_id, new_id)
        pin, enforce, carried = self._pin, self._enforce, self._user_unmuted
        try:
            self._do_release()
        except Exception as e:
            log.warning("releasing the old default failed: %s", e)
        self._device, self._device_id = device, new_id
        try:
            self._do_engage(pin, enforce)
        except Exception as e:
            log.warning("taking the new default failed: %s", e)
            return
        if carried and not enforce:
            log.info("carrying the user's un-mute onto the new default")
            self._write(None, lambda v: v.SetMute(0, APP_CTX))
            self._last_mute = False
            self._user_unmuted = True
            self._write_marker()

    # -- endpoint operations (all called under the lock) --------------------

    def _writer(self):
        """The endpoint to write to: the device we hold, else the current
        default."""
        if self._device is not None:
            try:
                return self._device.EndpointVolume
            except Exception as e:
                log.debug("held device unusable (%s); using the default", e)
        return _endpoint()

    def _write(self, vol, fn) -> None:
        try:
            fn(vol if vol is not None else self._writer())
        except Exception as e:
            log.debug("endpoint write failed: %s", e)

    def _restore_target(self) -> dict:
        """What release() should put back: the pre-cast snapshot, unless the
        user overrode our mute and never took it back - having deliberately
        un-muted, they should not be re-muted when the cast ends."""
        if self._prior is None:
            return {}
        return {"mute": 0 if self._user_unmuted else int(self._prior["mute"]),
                "volume": float(self._volume_target),
                "pin": bool(self._pinned_now)}

    def _write_marker(self) -> None:
        if self._prior is None:
            return
        try:
            _marker_path().write_text(json.dumps(self._restore_target()),
                                      encoding="utf-8")
        except OSError as e:
            log.debug("marker rewrite failed: %s", e)

    def _apply_pin(self, enabled: bool, vol=None) -> None:
        """Take or release the 100% hold, WRITING the endpoint each time.
        Setting a flag alone would only take effect on some later poll."""
        enabled = bool(enabled)
        self._pin = enabled
        if self._prior is None or enabled == self._pinned_now:
            return
        if enabled:
            self._write(vol, lambda v: v.SetMasterVolumeLevelScalar(1.0, APP_CTX))
        else:
            # Hand back the user's OWN level. The endpoint holds our pinned 1.0,
            # so simply letting go would leave them at full volume - a blast the
            # moment they un-mute, and their real level lost for good.
            self._write(vol, lambda v: v.SetMasterVolumeLevelScalar(
                float(self._volume_target), APP_CTX))
        self._pinned_now = enabled
        log.info("volume pin %s", "on" if enabled else "off")
        self._write_marker()

    def _do_engage(self, pin: bool, enforce: bool) -> None:
        if self._prior is not None:
            # Already engaged. Re-capturing _prior would record the ALREADY-muted
            # state and leave the PC muted after release. But the user may have
            # un-muted and been followed, so re-selecting a speakers mode has to
            # re-assert the mute - otherwise the UI reports "muted" over an
            # audible PC and the mode the user just picked does nothing.
            # Mute BEFORE applying policy: re-taking the pin writes 100%, which
            # on an un-muted endpoint would be an audible blast.
            if self._last_mute is False:
                log.info("speakers mode re-selected while following - re-muting")
                vol = self._writer()
                vol.SetMute(1, APP_CTX)      # raises: the caller must be told
                self._last_mute = True
                self._user_unmuted = False   # an explicit pick supersedes it
            self._enforce = bool(enforce)
            self._apply_pin(pin)
            self._write_marker()
            return
        if self._device is None:
            self._device = _speakers()
            self._device_id = _device_id(self._device)
        vol = self._device.EndpointVolume
        self._pin, self._enforce = pin, enforce
        self._prior = {"mute": vol.GetMute(),
                       "volume": vol.GetMasterVolumeLevelScalar()}
        self._volume_target = float(self._prior["volume"])
        self._user_unmuted = False
        self._pinned_now = bool(pin)     # intent, so the marker describes the
                                         # state the endpoint is about to be in
        # The marker goes down BEFORE any endpoint write, and a failure here is
        # fatal to the engage rather than logged and ignored. Muting with no
        # recovery marker is the one combination that can leave a machine
        # silently muted forever: a hard kill skips atexit and the next launch
        # has nothing to restore from. Better to fail loudly, still audible.
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

    def _do_release(self) -> None:
        if self._prior is None:
            return
        target = self._restore_target()
        self._prior = None
        pinned, self._pinned_now = self._pinned_now, False
        self._user_unmuted = False
        self._last_mute = None
        try:
            vol = self._writer()
            # Restore volume FIRST (while still muted) and only if we are still
            # holding it. If the pin was dropped mid-cast the user's level was
            # handed back then, and whatever they set since is theirs to keep.
            if pinned:
                vol.SetMasterVolumeLevelScalar(float(target["volume"]), APP_CTX)
            vol.SetMute(int(target["mute"]), APP_CTX)
            log.info("local output restored (mute=%s vol=%.2f pinned=%s)",
                     target["mute"], target["volume"], pinned)
        except Exception as e:
            log.warning("endpoint restore failed: %s", e)
        self._volume_target = 1.0
        self._device = None
        self._device_id = None
        try:
            _marker_path().unlink(missing_ok=True)
        except OSError as e:
            log.warning("could not clear the mute marker: %s", e)
        try:
            atexit.unregister(self._atexit_restore)
        except Exception:
            pass

    def _atexit_restore(self) -> None:
        """Last-resort restore on interpreter exit. os._exit() skips atexit
        entirely, which is what the marker file is for."""
        with self._lock:
            self._do_release()

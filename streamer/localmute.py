"""Local endpoint control while casting.

To keep this machine silent while casting, engage() always mutes the endpoint
and re-asserts that mute while the cast runs (a habitual mute-key press must not
un-silence the room). release() restores the user's prior mute.

The VOLUME pin is conditional. On this machine's driver the endpoint volume
scales the endpoint loopback capture (volume 0 = dead capture), so the endpoint
capture path pins the volume to 100% for a full-strength cast (pin=True) and
restores it on release. The process-loopback capture path taps the audio before
the volume stage, so it is decoupled - there pin=False leaves the volume alone,
and a mid-cast volume change by the user is legal.

Crash-safety: a JSON marker records the prior state (including whether the
volume was pinned); if the app died hard (atexit never ran), the next start
restores the endpoint from the marker.
"""
from __future__ import annotations

import atexit
import json
import logging
import threading
from pathlib import Path

from pycaw.pycaw import AudioUtilities

from .config import config_dir

log = logging.getLogger(__name__)

MARKER = "muted-by-streamer.marker"
REPIN_SECONDS = 5.0


def _endpoint():
    return AudioUtilities.GetSpeakers().EndpointVolume

# NOTE: per-app (mixer) session volumes are deliberately NOT touched by this
# app. The user mixes app volumes for D&D; a session at 1% quiets the cast by
# design. That is the user's mixing desk, not ours.


def _marker_path() -> Path:
    return config_dir() / MARKER


# pycaw's endpoint is a comtypes interface, so a failed HRESULT raises
# comtypes.COMError - which is NOT an OSError subclass. Every guard in this
# module therefore catches Exception: an endpoint hiccup must never crash the
# tray app, and release() in particular must never break cast teardown.


def endpoint_muted() -> bool:
    """Current endpoint mute state (for the 'auto' output mode, which watches
    the user's own mute rather than forcing one)."""
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
            vol.SetMasterVolumeLevelScalar(float(state.get("volume", 1.0)), None)
        vol.SetMute(int(state.get("mute", 0)), None)
    except Exception as e:
        log.warning("crash-recovery restore failed: %s", e)
    path.unlink(missing_ok=True)


class LocalMute:
    def __init__(self):
        self._prior: dict | None = None
        self._repin_stop: threading.Event | None = None

    def engage(self, pin: bool = True) -> None:
        """Mute the endpoint (always). When pin is True also pin the volume to
        100% - needed only on the endpoint capture path where volume scales the
        capture. The process-loopback path passes pin=False (decoupled)."""
        if self._prior is not None:
            # Already engaged (e.g. re-selecting "speakers" while in it).
            # Re-capturing _prior here would record the ALREADY-muted state and
            # leave the PC muted after release; the repin thread already holds
            # the endpoint, so there is nothing to do.
            log.debug("engage() while already engaged - ignoring")
            return
        vol = _endpoint()
        self._prior = {"mute": vol.GetMute(),
                       "volume": vol.GetMasterVolumeLevelScalar(),
                       "pin": bool(pin)}
        _marker_path().write_text(json.dumps(self._prior), encoding="utf-8")
        # Order matters: mute FIRST, then raise volume - never audible.
        vol.SetMute(1, None)
        if pin:
            vol.SetMasterVolumeLevelScalar(1.0, None)
        atexit.register(self.release)
        log.info("local output muted%s (was mute=%s vol=%.2f)",
                 ", volume pinned to 100%" if pin else "",
                 self._prior["mute"], self._prior["volume"])
        # Re-assert while casting: on both paths a stray mute-key press must not
        # un-silence the room; on the pinning path a volume-key press would also
        # attenuate the capture, so re-pin the volume too.
        self._repin_stop = threading.Event()
        threading.Thread(target=self._repin, args=(self._repin_stop, bool(pin)),
                         name="mute-repin", daemon=True).start()

    def _repin(self, stop: threading.Event, pin: bool) -> None:
        while not stop.wait(REPIN_SECONDS):
            try:
                vol = _endpoint()
                if not vol.GetMute():
                    log.info("endpoint un-muted mid-cast - re-muting")
                    vol.SetMute(1, None)
                if pin and vol.GetMasterVolumeLevelScalar() < 0.999:
                    log.info("endpoint volume dropped mid-cast - re-pinning 100%%")
                    vol.SetMasterVolumeLevelScalar(1.0, None)
            except Exception as e:
                log.debug("re-pin failed: %s", e)

    def release(self) -> None:
        if self._prior is None:
            return
        if self._repin_stop is not None:
            self._repin_stop.set()
            self._repin_stop = None
        try:
            vol = _endpoint()
            # Restore volume FIRST (while still muted) but only if we pinned it;
            # on the decoupled path the user's live volume is left as-is.
            if self._prior.get("pin", True):
                vol.SetMasterVolumeLevelScalar(float(self._prior["volume"]), None)
            vol.SetMute(int(self._prior["mute"]), None)
            log.info("local output restored (mute=%s vol=%.2f pin=%s)",
                     self._prior["mute"], self._prior["volume"],
                     self._prior.get("pin", True))
        except Exception as e:
            log.warning("endpoint restore failed: %s", e)
        self._prior = None
        _marker_path().unlink(missing_ok=True)
        try:
            atexit.unregister(self.release)
        except Exception:
            pass

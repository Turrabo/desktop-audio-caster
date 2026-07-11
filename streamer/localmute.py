"""Local endpoint control while casting.

This machine's driver applies the endpoint VOLUME to the loopback capture
(volume 0 = dead capture) but NOT the mute (probe-verified). So for a silent
machine AND a full-scale cast signal, engage() does both:
  - mute the endpoint (silence),
  - pin the endpoint volume to 100% (full-strength capture).
release() restores the user's prior mute AND volume.

Crash-safety: a JSON marker records the prior state; if the app died hard
(atexit never ran), the next start restores the endpoint from the marker.
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


def _sessions():
    """(name, SimpleAudioVolume) per app session with a live process."""
    out = []
    try:
        for s in AudioUtilities.GetAllSessions():
            if s.Process:
                out.append((s.Process.name(), s.SimpleAudioVolume))
    except OSError as e:
        log.debug("session enum failed: %s", e)
    return out


def _normalize_sessions() -> dict[str, float]:
    """Raise every app session to 1.0; return prior levels by process name.

    The Windows mixer multiplies each app's output before the endpoint mix -
    a forgotten 1% on chrome.exe made the cast stream 40 dB quiet while every
    visible volume control sat at 100%.
    """
    priors: dict[str, float] = {}
    for name, sv in _sessions():
        try:
            level = sv.GetMasterVolume()
            if name not in priors:
                priors[name] = level
            if level < 0.999:
                sv.SetMasterVolume(1.0, None)
                log.info("mixer: %s %.3f -> 1.0 for casting", name, level)
        except OSError as e:
            log.debug("session volume %s: %s", name, e)
    return priors


def _marker_path() -> Path:
    return config_dir() / MARKER


def _restore_sessions(priors: dict[str, float]) -> None:
    if not priors:
        return
    for name, sv in _sessions():
        if name in priors:
            try:
                sv.SetMasterVolume(float(priors[name]), None)
            except OSError as e:
                log.debug("session restore %s: %s", name, e)


def recover_from_crash() -> None:
    """Call at app start: if a marker survived a crash, restore the endpoint."""
    path = _marker_path()
    if not path.exists():
        return
    log.info("previous run left the endpoint modified - restoring")
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
        _restore_sessions(state.get("sessions", {}))
        vol = _endpoint()
        vol.SetMasterVolumeLevelScalar(float(state.get("volume", 1.0)), None)
        vol.SetMute(int(state.get("mute", 0)), None)
    except (OSError, ValueError, json.JSONDecodeError) as e:
        log.warning("crash-recovery restore failed: %s", e)
    path.unlink(missing_ok=True)


class LocalMute:
    def __init__(self):
        self._prior: dict | None = None
        self._repin_stop: threading.Event | None = None

    def engage(self) -> None:
        vol = _endpoint()
        self._prior = {"mute": vol.GetMute(),
                       "volume": vol.GetMasterVolumeLevelScalar()}
        # Order matters: mute FIRST, then raise levels - never audible.
        vol.SetMute(1, None)
        vol.SetMasterVolumeLevelScalar(1.0, None)
        self._prior["sessions"] = _normalize_sessions()
        _marker_path().write_text(json.dumps(self._prior), encoding="utf-8")
        atexit.register(self.release)
        log.info("local output muted, volume pinned to 100%% for capture "
                 "(was mute=%s vol=%.2f)", self._prior["mute"], self._prior["volume"])
        # Re-assert while casting: a habitual volume-key press would silently
        # attenuate the capture on this driver (volume applies to loopback).
        self._repin_stop = threading.Event()
        threading.Thread(target=self._repin, args=(self._repin_stop,),
                         name="mute-repin", daemon=True).start()

    def _repin(self, stop: threading.Event) -> None:
        while not stop.wait(REPIN_SECONDS):
            try:
                vol = _endpoint()
                if not vol.GetMute() or vol.GetMasterVolumeLevelScalar() < 0.999:
                    log.info("endpoint changed mid-cast - re-pinning mute + 100%%")
                    vol.SetMute(1, None)
                    vol.SetMasterVolumeLevelScalar(1.0, None)
                # New app sessions (or mixer fiddling) mid-cast get
                # normalized too; only NEW names extend the restore map.
                if self._prior is not None:
                    fresh = _normalize_sessions()
                    sessions = self._prior.setdefault("sessions", {})
                    changed = False
                    for name, level in fresh.items():
                        if name not in sessions:
                            sessions[name] = level
                            changed = True
                    if changed:
                        _marker_path().write_text(json.dumps(self._prior),
                                                  encoding="utf-8")
            except OSError as e:
                log.debug("re-pin failed: %s", e)

    def release(self) -> None:
        if self._prior is None:
            return
        if self._repin_stop is not None:
            self._repin_stop.set()
            self._repin_stop = None
        try:
            _restore_sessions(self._prior.get("sessions", {}))
            vol = _endpoint()
            # Order matters: restore volume FIRST (while still muted), then mute.
            vol.SetMasterVolumeLevelScalar(float(self._prior["volume"]), None)
            vol.SetMute(int(self._prior["mute"]), None)
            log.info("local output restored (mute=%s vol=%.2f)",
                     self._prior["mute"], self._prior["volume"])
        except OSError as e:
            log.warning("endpoint restore failed: %s", e)
        self._prior = None
        _marker_path().unlink(missing_ok=True)
        try:
            atexit.unregister(self.release)
        except Exception:
            pass

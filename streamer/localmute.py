"""Local endpoint mute while casting (probe-verified on this machine:
loopback capture SURVIVES endpoint mute, but NOT volume-0 - so we mute,
never zero, and warn if the endpoint is already at 0%).

Crash-safety: a marker file records that we muted; if the app died hard
(atexit never ran), the next start restores the endpoint.
"""
from __future__ import annotations

import atexit
import logging
from pathlib import Path

from pycaw.pycaw import AudioUtilities

from .config import config_dir

log = logging.getLogger(__name__)

MARKER = "muted-by-streamer.marker"


def _endpoint():
    return AudioUtilities.GetSpeakers().EndpointVolume


def _marker_path() -> Path:
    return config_dir() / MARKER


def recover_from_crash() -> None:
    """Call at app start: if a mute marker survived a crash, unmute."""
    if _marker_path().exists():
        log.info("previous run left the endpoint muted - restoring")
        try:
            _endpoint().SetMute(0, None)
        except OSError as e:
            log.warning("crash-recovery unmute failed: %s", e)
        _marker_path().unlink(missing_ok=True)


class LocalMute:
    def __init__(self):
        self._was_muted: int | None = None

    def engage(self) -> None:
        vol = _endpoint()
        if vol.GetMasterVolumeLevelScalar() < 0.005:
            log.warning("endpoint volume is ~0%% - on this driver that silences "
                        "the capture too; raise the LOCAL volume (it stays muted)")
        self._was_muted = vol.GetMute()
        vol.SetMute(1, None)
        _marker_path().write_text("muted", encoding="utf-8")
        atexit.register(self.release)
        log.info("local output muted for casting")

    def release(self) -> None:
        if self._was_muted is None:
            return
        try:
            _endpoint().SetMute(self._was_muted, None)
            log.info("local output restored (mute=%s)", self._was_muted)
        except OSError as e:
            log.warning("unmute failed: %s", e)
        self._was_muted = None
        _marker_path().unlink(missing_ok=True)
        try:
            atexit.unregister(self.release)
        except Exception:
            pass

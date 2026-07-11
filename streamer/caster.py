"""Cast control: resident discovery, connect, play, watchdog.

Reliability model:
- CastBrowser stays alive for the app's lifetime, so speaker reboots, new
  DHCP addresses and group-leader migration re-resolve automatically.
- pychromecast heals its own socket to a stable address.
- One watchdog covers the rest: player state dead, sustained BUFFERING,
  capture unhealthy, or our LAN IP changed -> re-derive URL, re-cast, with
  exponential backoff (2 s -> 30 s).
"""
from __future__ import annotations

import logging
import socket
import threading
import time

import pychromecast
import zeroconf
from pychromecast.discovery import CastBrowser, SimpleCastListener

from .safety import SafeCast

log = logging.getLogger(__name__)

WATCHDOG_PERIOD = 5.0
BUFFERING_WEDGE_SECONDS = 60.0
BACKOFF_START, BACKOFF_CAP = 2.0, 30.0

# Lag auto-trim: the Default Media Receiver buffers ~9 s of live WAV before it
# starts playing, and that backlog would persist forever (we stream realtime,
# it never drains). Seeking forward WITHIN the receiver's buffer skips the
# backlog without any HTTP interaction; measured floor is ~1.0 s end-to-end.
TRIM_THRESHOLD_SECONDS = 2.0   # trim whenever lag exceeds this...
TRIM_MARGIN_SECONDS = 0.4      # ...down to this much cushion above the edge
TRIM_MIN_INTERVAL = 10.0       # never seek more often than this


def source_ip_for(host: str) -> str:
    """The local IP the OS would route to `host` from - correct across NICs/VPNs."""
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect((host, 9))
        return s.getsockname()[0]
    finally:
        s.close()


class Discovery:
    """Resident CastBrowser wrapper. Read-only; owns the uuid->name map."""

    def __init__(self):
        self._zc = zeroconf.Zeroconf()
        self._browser = CastBrowser(SimpleCastListener(lambda u, s: None), self._zc)
        self._browser.start_discovery()

    def wait_for_devices(self, seconds: float = 6.0) -> None:
        time.sleep(seconds)

    @property
    def devices(self) -> dict[str, str]:
        """uuid-string -> friendly name (for safety group-member checks)."""
        return {str(u): info.friendly_name for u, info in self._browser.devices.items()}

    def list_devices(self) -> list[dict]:
        return [
            {"name": i.friendly_name, "model": i.model_name, "host": i.host,
             "port": i.port, "type": i.cast_type, "uuid": str(u)}
            for u, i in self._browser.devices.items()
        ]

    def find(self, name: str):
        for _, info in self._browser.devices.items():
            if info.friendly_name.lower() == name.lower():
                return info
        return None

    def connect(self, name: str) -> SafeCast:
        info = self.find(name)
        if info is None:
            known = ", ".join(sorted(i.friendly_name for i in self._browser.devices.values()))
            raise LookupError(f"device {name!r} not found. Known: {known}")
        cast = pychromecast.get_chromecast_from_cast_info(info, self._zc)
        cast.wait(timeout=15)
        return SafeCast(cast)

    def stop(self) -> None:
        self._browser.stop_discovery()
        self._zc.close()


class CastSession:
    """One casting session to one device/group, with watchdog."""

    def __init__(self, discovery: Discovery, safe_cast: SafeCast, port: int,
                 stream_type: str, capture, on_event=None, sent_seconds_fn=None):
        self._discovery = discovery
        self._cast = safe_cast
        self._port = port
        self._stream_type = stream_type
        self._capture = capture
        self._on_event = on_event or (lambda msg: None)
        self._sent_seconds_fn = sent_seconds_fn
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._current_url = ""
        self._buffering_since: float | None = None
        self._last_trim = 0.0
        self.recast_count = 0
        self.trim_count = 0

    # -- casting -----------------------------------------------------------

    def _stream_url(self) -> str:
        host = self._cast.socket_client.host
        return f"http://{source_ip_for(host)}:{self._port}/stream.wav"

    def start(self) -> None:
        self._play()
        self._thread = threading.Thread(target=self._watchdog, name="watchdog", daemon=True)
        self._thread.start()

    def _play(self) -> None:
        self._current_url = self._stream_url()
        mc = self._cast.media_controller
        log.info("casting %s to %r (stream_type=%s)",
                 self._current_url, self._cast.name, self._stream_type)
        mc.play_media(self._current_url, "audio/wav",
                      stream_type=self._stream_type, title="Desktop Audio")
        mc.block_until_active(timeout=15)

    # -- watchdog ------------------------------------------------------------

    def _watchdog(self) -> None:
        backoff = BACKOFF_START
        while not self._stop.is_set():
            time.sleep(WATCHDOG_PERIOD)
            try:
                reason = self._failure_reason()
                if reason is None:
                    backoff = BACKOFF_START
                    self._trim_lag()
                    continue
                log.warning("watchdog: %s - re-casting in %.0f s", reason, backoff)
                self._on_event(f"cast interrupted ({reason}), reconnecting")
                time.sleep(backoff)
                backoff = min(backoff * 2, BACKOFF_CAP)
                if self._stop.is_set():
                    return
                self._recover()
            except Exception as e:  # watchdog must never die
                log.error("watchdog error: %s", e)

    def _failure_reason(self) -> str | None:
        if not self._capture.healthy:
            return "capture unhealthy"

        mc = self._cast.media_controller
        state = mc.status.player_state if mc.status else "UNKNOWN"

        if state == "BUFFERING":
            if self._buffering_since is None:
                self._buffering_since = time.monotonic()
            elif time.monotonic() - self._buffering_since > BUFFERING_WEDGE_SECONDS:
                self._buffering_since = None
                return f"buffering wedged >{BUFFERING_WEDGE_SECONDS:.0f}s"
            return None
        self._buffering_since = None

        if state in ("IDLE", "UNKNOWN"):
            return f"player state {state}"

        try:
            if self._stream_url() != self._current_url:
                return "local IP changed"
        except OSError:
            return "no route to speaker"
        return None

    def lag_seconds(self) -> float | None:
        """End-to-end lag: seconds of audio sent minus seconds played."""
        if self._sent_seconds_fn is None:
            return None
        mc = self._cast.media_controller
        if not mc.status or mc.status.player_state != "PLAYING":
            return None
        played = mc.status.adjusted_current_time
        if played is None:
            return None
        return self._sent_seconds_fn() - played

    def _trim_lag(self) -> None:
        if time.monotonic() - self._last_trim < TRIM_MIN_INTERVAL:
            return
        lag = self.lag_seconds()
        if lag is None or lag <= TRIM_THRESHOLD_SECONDS:
            return
        mc = self._cast.media_controller
        played = mc.status.adjusted_current_time
        target = played + lag - TRIM_MARGIN_SECONDS
        log.info("trimming lag %.1fs -> ~%.1fs (seek %.1f -> %.1f)",
                 lag, TRIM_MARGIN_SECONDS + 0.6, played, target)
        mc.seek(target)
        self._last_trim = time.monotonic()
        self.trim_count += 1

    def _recover(self) -> None:
        # Re-resolve the device from live discovery (speaker may have a new IP
        # or the group leader may have migrated), then re-cast.
        name = self._cast.name
        try:
            fresh = self._discovery.connect(name)
            self._cast = fresh
            self._play()
            self.recast_count += 1
            self._on_event("cast recovered")
        except Exception as e:
            log.warning("recover failed (%s); will retry", e)

    # -- teardown ------------------------------------------------------------

    def status(self) -> dict:
        mc = self._cast.media_controller
        return {
            "device": self._cast.name,
            "player_state": mc.status.player_state if mc.status else None,
            "url": self._current_url,
            "recasts": self.recast_count,
            "volume": getattr(self._cast.status, "volume_level", None),
            # seconds of media the receiver has PLAYED (extrapolated between
            # status messages); compare with seconds SENT for end-to-end lag
            "played_seconds": (mc.status.adjusted_current_time
                               if mc.status else None),
        }

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            self._thread.join(timeout=WATCHDOG_PERIOD + 2)
        try:
            mc = self._cast.media_controller
            mc.stop()
            self._cast.quit_app()
        except Exception as e:
            log.debug("stop cleanup: %s", e)
        try:
            self._cast.disconnect(timeout=5)
        except Exception:
            pass

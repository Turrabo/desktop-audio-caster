"""AppController - owns all casting logic; UI layers are thin views.

Threading contract:
- ONE worker thread executes ops (start/stop/switch/volume) serially from a
  queue - rapid clicks can never double-bind the port or race teardown.
- State events go to listeners via callables; views marshal to their own
  thread (the tkinter UI drains a queue.Queue with an after-loop).
- pychromecast/pystray callbacks must only enqueue, never block.
"""
from __future__ import annotations

import concurrent.futures
import logging
import os
import queue
import threading
import time

from . import config as cfg_mod
from .capture import open_capture
from .caster import CastSession, Discovery
from .localmute import LocalMute, recover_from_crash
from .pacer import Pacer
from .server import StreamServer
from . import safety

log = logging.getLogger(__name__)

TRANSITIONAL_STATES = {"DISCOVERING", "CONNECTING", "LAUNCHING", "WAITING_STREAM",
                       "BUFFERING", "RECONNECTING", "STOPPING"}


class AppController:
    def __init__(self):
        self.cfg = cfg_mod.load()
        recover_from_crash()
        self._listeners: list = []
        self._ops: queue.Queue = queue.Queue()
        self._worker = threading.Thread(target=self._run_ops, name="ops", daemon=True)
        self._worker.start()

        self.state = "DISCOVERING"
        self.state_detail: str | None = None
        self.discovery = Discovery(on_change=self._on_devices_changed)
        self.capture = None
        # Whether the live capture couples the cast to endpoint volume (endpoint
        # loopback does; process loopback does not). Gates the mute layer's pin.
        self._couples = True
        self.pacer: Pacer | None = None
        self.server: StreamServer | None = None
        self.session = None            # CastSession or MirrorSession
        self.mute = LocalMute()
        self.cast_target: str | None = None
        self.cast_mode_active: str = "http"   # resolved path of the live session
        self._mirror_feed = None       # session.feed while mirroring, else None
        # When set, the pacer feeds SILENCE to the cast (speakers go quiet)
        # while local PC playback is untouched - the "this_pc" output mode and
        # the desk phase of "auto". Feeding zeros, not writing device volume, so
        # it is instant, reversible, and never touches the volume choke point.
        self._cast_silenced = False
        self._output_mode = "speakers"       # active mode of the live cast
        self._auto_stop: threading.Event | None = None   # auto-monitor stopper
        self.volumes = VolumeManager(self)

    # -- events ----------------------------------------------------------

    def add_listener(self, cb) -> None:
        """cb(kind, *args); called from arbitrary threads - views must enqueue."""
        self._listeners.append(cb)

    def _notify(self, kind: str, *args) -> None:
        for cb in list(self._listeners):
            try:
                cb(kind, *args)
            except Exception as e:
                log.debug("listener error: %s", e)

    def _set_state(self, state: str, detail: str | None = None) -> None:
        self.state, self.state_detail = state, detail
        log.info("state: %s %s", state, detail or "")
        self._notify("state", state, detail)

    def _on_devices_changed(self) -> None:
        # First discovery result ends the DISCOVERING state - without this the
        # UI sits on "Finding speakers..." forever.
        if self.state == "DISCOVERING" and self.discovery.list_devices():
            self._set_state("IDLE")
        self._notify("devices")

    # -- ops (serialized) -----------------------------------------------------

    def _run_ops(self) -> None:
        while True:
            op = self._ops.get()
            try:
                op()
            except Exception as e:
                log.exception("op failed")
                self._set_state("ERROR", str(e)[:200])

    def enqueue(self, fn) -> None:
        self._ops.put(fn)

    def busy(self) -> bool:
        return self.state in TRANSITIONAL_STATES

    # -- casting ------------------------------------------------------------------

    def start_cast(self, name: str) -> None:
        self.enqueue(lambda: self._do_start(name))

    def stop_cast(self) -> None:
        self.enqueue(self._do_stop)

    def _feed_pacer(self, data: bytes) -> None:
        """Capture callback. The capture starts before the pacer is built (the
        factory activates + starts inside open_capture); drop until it exists."""
        p = self.pacer
        if p is not None:
            p.feed(data)

    def _pacer_sink(self, chunk: bytes) -> None:
        """Fan-out from the single pacer clock: the HTTP server always (a cheap
        no-op when it has no clients, e.g. while mirroring) plus the mirror
        session when one is active. When silenced (this_pc / auto desk phase) the
        CAST gets zeros while local PC playback is untouched."""
        out = bytes(len(chunk)) if self._cast_silenced else chunk
        self.server.feed(out)
        mf = self._mirror_feed
        if mf is not None:
            mf(out)

    def _mirror_available(self, fmt) -> tuple[bool, str | None]:
        """Whether mirror mode can run for this capture format. Lazy-imports the
        mirror stack so a bad import (missing opus.dll etc.) can never break app
        startup - it just routes to HTTP."""
        if self.cfg["cast_mode"] == "http":
            return False, "cast_mode=http"
        try:
            from . import mirror, _opus
        except Exception as e:               # pragma: no cover - import guard
            return False, f"mirror import failed: {e}"
        if not mirror.eligible_format(fmt):
            return False, "capture is not 48 kHz/stereo/16-bit"
        if not _opus.available():
            return False, "opus.dll not loadable"
        return True, None

    def _do_start(self, name: str) -> None:
        if self.session is not None:
            self._do_stop()  # switch target = stop + start, same worker

        self._set_state("CONNECTING", name)
        # open_capture makes the backend choice and returns the capture already
        # STARTED, so its format is known here. It feeds at once; _feed_pacer
        # drops until the pacer is built a few lines down.
        self.capture = open_capture(on_data=self._feed_pacer,
                                    device_hint=self.cfg["capture_device"])
        self._couples = self.capture.couples_volume
        try:
            self.server = StreamServer(self.capture.format, self.cfg["port"])
            self.pacer = Pacer(self.capture.format, sink=self._pacer_sink)
            self.server.start()
            self.pacer.start()

            safe_cast = self.discovery.connect(name)
            self.cast_target = safe_cast.name
            self._apply_output_mode(self.cfg["output_mode"])

            can_mirror, why = self._mirror_available(self.capture.format)
            if self.cfg["cast_mode"] == "mirror" and not can_mirror:
                log.warning("cast_mode=mirror but ineligible (%s); using HTTP", why)
            started_mirror = can_mirror and self._try_start_mirror(safe_cast)
            if started_mirror:
                self.cast_mode_active = "mirror"
            else:
                if can_mirror:
                    # the failed mirror attempt disconnected the cast; reconnect
                    safe_cast = self.discovery.connect(name)
                self._start_http(safe_cast)
                self.cast_mode_active = "http"

            self.cfg["last_device"] = safe_cast.name
            cfg_mod.save(self.cfg)
        except Exception:
            # The capture is already RUNNING, so a failure here (port bind,
            # connect, ...) must tear it down. The ops worker only sets ERROR;
            # without this the capture survives as an orphan still bound to
            # _feed_pacer and would feed the NEXT session's pacer alongside its
            # own capture - two captures interleaving into one clock.
            self._abort_partial_start()
            raise

    def _abort_partial_start(self) -> None:
        """Release whatever _do_start managed to build before it failed."""
        self._stop_auto_monitor()
        self.mute.release()
        self._cast_silenced = False
        if self.session is not None:
            try:
                self.session.stop()
            except Exception as e:
                log.debug("partial-start session stop: %s", e)
            self.session = None
        for obj in (self.capture, self.pacer, self.server):
            if obj is not None:
                try:
                    obj.stop()
                except Exception as e:
                    log.debug("partial-start teardown: %s", e)
        self.capture = self.pacer = self.server = None
        self._mirror_feed = None
        self.cast_target = None

    def _try_start_mirror(self, safe_cast) -> bool:
        """Attempt a mirror session. On any start-time failure, tear it down and
        return False so the caller falls back to HTTP (no retry - a broken
        protocol is deterministic)."""
        from .mirror import MirrorSession
        self.server.serving = False       # no cleartext WAV while mirroring
        session = MirrorSession(
            self.discovery, safe_cast, self.capture,
            on_state=self._set_state,
            on_fallback_needed=self._on_fallback_needed,
            target_delay=self.cfg["mirror_target_delay_ms"])
        self._mirror_feed = session.feed
        try:
            session.start()               # blocks until PLAYING or raises
        except Exception as e:            # any start-time failure -> HTTP
            log.warning("mirror start failed (%s); falling back to HTTP", e)
            self._mirror_feed = None
            try:
                session.stop()
            except Exception:
                pass
            self.server.serving = True
            return False
        self.session = session
        return True

    def _start_http(self, safe_cast) -> None:
        self.server.serving = True
        self._mirror_feed = None
        server, capture = self.server, self.capture
        self.session = CastSession(
            self.discovery, safe_cast, self.cfg["port"], self.cfg["stream_type"],
            self.capture,
            on_state=self._set_state,
            fetch_count_fn=lambda: server.get_count,
            on_gave_up=self._on_session_gave_up,
            sent_seconds_fn=lambda: (server.latest_client_bytes
                                     / capture.format.bytes_per_second))
        self.session.start()

    # -- live latency ---------------------------------------------------------

    def set_target_delay(self, ms: int) -> None:
        self.enqueue(lambda: self._do_set_target_delay(ms))

    def _do_set_target_delay(self, ms: int) -> None:
        cfg_mod.set_user_value("mirror_target_delay_ms", ms)
        self.cfg["mirror_target_delay_ms"] = ms
        session = self.session
        if session is not None and hasattr(session, "set_target_delay"):
            session.set_target_delay(ms)      # live re-OFFER (mirror only)
        self._notify("target_delay", ms)

    # -- output routing -------------------------------------------------------

    def set_output_mode(self, mode: str) -> None:
        self.enqueue(lambda: self._do_set_output_mode(mode))

    def _do_set_output_mode(self, mode: str) -> None:
        cfg_mod.set_user_value("output_mode", mode)
        self.cfg["output_mode"] = mode
        if self.session is not None:      # apply live; else next cast picks it up
            self._apply_output_mode(mode)
        self._notify("output_mode", mode)

    def _apply_output_mode(self, mode: str) -> None:
        """Actuate an output mode on the live cast: the local PC mute (via
        LocalMute) and the cast-silence gate. Assumes a cast is up."""
        from . import localmute
        self._output_mode = mode
        self._stop_auto_monitor()
        if mode == "speakers":            # PC muted, full-strength cast
            # Pin the volume too only when the capture couples to it (endpoint
            # loopback); the process-loopback path is decoupled, so muting alone
            # keeps the cast at full strength without touching the volume.
            self.mute.engage(pin=self._couples)
            self._notify("mute", True)
            self._cast_silenced = False
        elif mode == "this_pc":           # PC audible, speakers fed silence
            self.mute.release()
            self._notify("mute", False)
            self._cast_silenced = True
        elif mode == "both":              # both audible (cast follows PC volume)
            self.mute.release()
            self._notify("mute", False)
            self._cast_silenced = False
        elif mode == "auto":              # the PC's own mute is the switch
            self.mute.release()           # never force; the user drives mute
            self._notify("mute", False)
            self._cast_silenced = not localmute.endpoint_muted()
            self._start_auto_monitor()
        else:
            log.warning("unknown output_mode %r; treating as speakers", mode)
            self._apply_output_mode("speakers")

    def _start_auto_monitor(self) -> None:
        self._auto_stop = threading.Event()
        threading.Thread(target=self._auto_monitor, args=(self._auto_stop,),
                         name="auto-output", daemon=True).start()

    def _stop_auto_monitor(self) -> None:
        if self._auto_stop is not None:
            self._auto_stop.set()
            self._auto_stop = None

    def _auto_monitor(self, stop: threading.Event) -> None:
        """Watch the endpoint mute; on a SETTLED change, flip the cast-silence
        gate inversely (PC muted -> speakers play; PC unmuted -> desk only). The
        settle absorbs a transient toggle (a Teams call, a media-key fumble) so
        a brief unmute doesn't strobe the whole house."""
        from . import localmute
        settle = 1.5
        acted = localmute.endpoint_muted()
        candidate, since = acted, time.monotonic()
        while not stop.wait(0.5):
            cur = localmute.endpoint_muted()
            if cur != candidate:
                candidate, since = cur, time.monotonic()
            elif cur != acted and time.monotonic() - since >= settle:
                acted = cur
                self.enqueue(lambda muted=cur: self._do_auto_silence(not muted))

    def _do_auto_silence(self, silenced: bool) -> None:
        # Stale-guard: only act if still auto and still the same live cast.
        if self._output_mode != "auto" or self.cast_target is None:
            return
        self._cast_silenced = silenced
        log.info("auto output: cast %s",
                 "silenced (listening at PC)" if silenced else "live (speakers)")

    def _do_stop(self, final: tuple[str, str | None] = ("IDLE", None)) -> None:
        self._set_state("STOPPING", self.cast_target)
        self._stop_auto_monitor()
        # Mute restore first: fast, local, and the thing users panic about.
        self.mute.release()
        self._notify("mute", False)
        self._cast_silenced = False
        if self.session is not None:
            try:
                self.session.stop()
            except Exception as e:
                log.debug("session stop: %s", e)
            self.session = None
        for obj in (self.capture, self.pacer, self.server):
            if obj is not None:
                try:
                    obj.stop()
                except Exception as e:
                    log.debug("teardown: %s", e)
        self.capture = self.pacer = self.server = None
        self._mirror_feed = None
        self.cast_target = None
        self._set_state(*final)

    def _on_session_gave_up(self, detail: str) -> None:
        """Watchdog decided retrying can't help (stream never fetched). Tear
        everything down - unmute, free capture/server, clear the target so the
        card shows play again - but land on ERROR so the banner persists."""
        self.enqueue(lambda: self._do_stop(final=("ERROR", detail)))

    def _on_fallback_needed(self, reason: str) -> None:
        """Mirror watchdog exhausted recovery. Swap to the HTTP path WITHOUT
        unmuting or tearing down capture/server. Fired from the watchdog thread,
        so enqueue; the op is stale-guarded against a user stop/switch that
        already replaced the session."""
        failed = self.session
        self.enqueue(lambda: self._do_fallback(failed, reason))

    def _do_fallback(self, failed_session, reason: str) -> None:
        if self.session is not failed_session or self.cast_target is None:
            log.info("fallback for %s ignored (session already replaced)", reason)
            return
        log.warning("mirror -> HTTP fallback for %r: %s", self.cast_target, reason)
        name = self.cast_target
        # A capture format change also invalidated the HTTP server's WAV header
        # (built at the old rate), so an in-place swap would still be wrong-pitch.
        # Rebuild the whole pipeline at the new format via a clean restart.
        from . import mirror
        if self.capture is not None and not mirror.eligible_format(self.capture.format):
            log.warning("capture format changed under mirror; full restart")
            self._do_stop()
            self._do_start(name)
            return
        self._mirror_feed = None
        try:
            failed_session.stop()      # stops only the mirror session
        except Exception as e:
            log.debug("mirror stop during fallback: %s", e)
        # capture, pacer, server, mute and cast_target all stay as-is; the HTTP
        # server starts serving again and a fresh connection carries the DMR.
        safe_cast = self.discovery.connect(name)
        self._start_http(safe_cast)
        self.cast_mode_active = "http"

    # -- device info ------------------------------------------------------------------

    def devices(self) -> dict[str, list[dict]]:
        """{'groups': [...], 'speakers': [...]} sorted by name."""
        groups, speakers = [], []
        for d in self.discovery.list_devices():
            (groups if d["type"] == "group" else speakers).append(d)
        return {"groups": sorted(groups, key=lambda d: d["name"].lower()),
                "speakers": sorted(speakers, key=lambda d: d["name"].lower())}

    # -- shutdown ------------------------------------------------------------------

    def shutdown(self, then=None) -> None:
        """Fast exit: teardown in the worker, hard-exit guarantee at 6 s."""
        def _bail():
            time.sleep(6)
            os._exit(0)
        threading.Thread(target=_bail, daemon=True).start()

        def _teardown():
            self._do_stop()
            self.volumes.close_all()
            self.discovery.stop()
            if then is not None:
                then()
        self.enqueue(_teardown)


class VolumeManager:
    """Reads/writes device volume. Connections live only while the popover is
    open (open_sweep -> close_all); the active cast target reuses the
    CastSession connection."""

    def __init__(self, ctl: AppController):
        self._ctl = ctl
        self._conns: dict[str, object] = {}   # name -> SafeCast
        self._lock = threading.Lock()
        self._debounce: dict[str, threading.Timer] = {}
        self.last_write: dict[str, float] = {}  # name -> monotonic time
        self.known_levels: dict[str, float] = {}  # name -> last seen 0..1

    def _get_cast(self, name: str):
        session = self._ctl.session
        if session is not None and self._ctl.cast_target == name:
            return session.safe_cast
        with self._lock:
            sc = self._conns.get(name)
        if sc is None:
            sc = self._ctl.discovery.connect(name, timeout=5)
            with self._lock:
                self._conns[name] = sc
        return sc

    def open_sweep(self, names: list[str], on_level) -> None:
        """Read volumes in parallel; on_level(name, 0..1) per result (worker
        threads - receiver must enqueue)."""
        def read_one(name):
            try:
                sc = self._get_cast(name)
                level = sc.status.volume_level if sc.status else None
                if level is not None:
                    self.known_levels[name] = float(level)
                    on_level(name, float(level))
            except Exception as e:
                log.debug("volume read %r failed: %s", name, e)

        def sweep():
            with concurrent.futures.ThreadPoolExecutor(max_workers=4) as pool:
                pool.map(read_one, names)
        threading.Thread(target=sweep, name="vol-sweep", daemon=True).start()

    def set_volume_debounced(self, name: str, level: float) -> None:
        """250 ms debounce per device; records last_write for echo suppression."""
        t = self._debounce.pop(name, None)
        if t is not None:
            t.cancel()

        def fire():
            self.last_write[name] = time.monotonic()
            self.known_levels[name] = level
            try:
                info = self._ctl.discovery.find(name)
                if info is not None and info.cast_type == "group":
                    self._set_group_flat(name, level)
                else:
                    sc = self._get_cast(name)
                    safety.set_volume(sc.unwrap_for_safety_module_only(), level,
                                      self._ctl.cfg, self._ctl.discovery.devices)
            except Exception as e:
                log.warning("volume set %r failed: %s", name, e)
        timer = threading.Timer(0.25, fire)
        timer.daemon = True
        self._debounce[name] = timer
        timer.start()

    def _set_group_flat(self, group_name: str, level: float) -> None:
        """Group slider semantics (user spec): every member speaker is set to
        the ABSOLUTE level - 0 is silence, 100 is that speaker's max. Google's
        native group volume rescales members proportionally from their prior
        levels, which reads as 'group at 100% yet speakers quiet' whenever
        members sit low."""
        sc = self._get_cast(group_name)
        members = safety.resolve_group_members(sc.unwrap_for_safety_module_only())
        known = self._ctl.discovery.devices
        names = [known[u] for u in (members or []) if u in known]
        if not names:
            # Membership unresolved - fall back to Google's proportional set
            # so the slider still does something.
            log.warning("group %r members unresolved; proportional fallback",
                        group_name)
            safety.set_volume(sc.unwrap_for_safety_module_only(), level,
                              self._ctl.cfg, known)
            return
        log.info("group %r flatten: %s -> %.2f", group_name, names, level)
        for member in names:
            self.last_write[member] = time.monotonic()
            self.known_levels[member] = level
            try:
                msc = self._get_cast(member)
                safety.set_volume(msc.unwrap_for_safety_module_only(), level,
                                  self._ctl.cfg, known)
            except Exception as e:
                log.warning("member volume %r failed: %s", member, e)

    def close_all(self) -> None:
        with self._lock:
            conns, self._conns = self._conns, {}
        for name, sc in conns.items():
            try:
                sc.disconnect(timeout=2)
            except Exception:
                pass

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
from .capture import LoopbackCapture
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
        self.capture: LoopbackCapture | None = None
        self.pacer: Pacer | None = None
        self.server: StreamServer | None = None
        self.session: CastSession | None = None
        self.mute = LocalMute()
        self.cast_target: str | None = None
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

    def _do_start(self, name: str) -> None:
        if self.session is not None:
            self._do_stop()  # switch target = stop + start, same worker

        self._set_state("CONNECTING", name)
        self.capture = LoopbackCapture(on_data=lambda d: self.pacer.feed(d),
                                       device_hint=self.cfg["capture_device"])
        self.server = StreamServer(self.capture.format, self.cfg["port"])
        self.pacer = Pacer(self.capture.format, sink=self.server.feed)
        self.server.start()
        self.pacer.start()
        self.capture.start()

        safe_cast = self.discovery.connect(name)
        if self.cfg["mute_local_while_casting"]:
            self.mute.engage()
            self._notify("mute", True)

        server, capture = self.server, self.capture
        self.session = CastSession(
            self.discovery, safe_cast, self.cfg["port"], self.cfg["stream_type"],
            self.capture,
            on_state=self._set_state,
            client_count_fn=server.client_count,
            sent_seconds_fn=lambda: (server.latest_client_bytes
                                     / capture.format.bytes_per_second))
        self.session.start()
        self.cast_target = safe_cast.name
        self.cfg["last_device"] = safe_cast.name
        cfg_mod.save(self.cfg)

    def _do_stop(self) -> None:
        self._set_state("STOPPING", self.cast_target)
        # Mute restore first: fast, local, and the thing users panic about.
        self.mute.release()
        self._notify("mute", False)
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
        self.cast_target = None
        self._set_state("IDLE")

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
            try:
                sc = self._get_cast(name)
                safety.set_volume(sc.unwrap_for_safety_module_only(), level,
                                  self._ctl.cfg, self._ctl.discovery.devices)
            except Exception as e:
                log.warning("volume set %r failed: %s", name, e)
        timer = threading.Timer(0.25, fire)
        timer.daemon = True
        self._debounce[name] = timer
        timer.start()

    def close_all(self) -> None:
        with self._lock:
            conns, self._conns = self._conns, {}
        for name, sc in conns.items():
            try:
                sc.disconnect(timeout=2)
            except Exception:
                pass

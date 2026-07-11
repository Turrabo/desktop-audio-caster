"""System tray UI.

Menu: device list (radio), Start/Stop casting, Quit. Failures surface as
tray notifications. Single-instance guarded by a localhost port bind.
"""
from __future__ import annotations

import logging
import socket
import threading

import pystray
from PIL import Image, ImageDraw

from . import config as cfg_mod
from .capture import LoopbackCapture
from .caster import CastSession, Discovery
from .localmute import LocalMute, recover_from_crash
from .pacer import Pacer
from .server import StreamServer

log = logging.getLogger(__name__)

SINGLE_INSTANCE_PORT = 48765


def _make_icon(active: bool) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    color = (66, 133, 244, 255) if active else (128, 128, 128, 255)
    # speaker body + cone
    d.polygon([(14, 24), (26, 24), (38, 12), (38, 52), (26, 40), (14, 40)], fill=color)
    if active:
        d.arc([40, 20, 56, 44], start=-50, end=50, fill=color, width=4)
    return img


class TrayApp:
    def __init__(self):
        self.cfg = cfg_mod.load()
        self.discovery = Discovery()
        self.capture: LoopbackCapture | None = None
        self.pacer: Pacer | None = None
        self.server: StreamServer | None = None
        self.session: CastSession | None = None
        self.mute = LocalMute()
        self.selected: str | None = self.cfg["last_device"]
        self.icon = pystray.Icon("desktop-audio-streamer", _make_icon(False),
                                 "Desktop Audio Streamer", menu=self._menu())

    # -- single instance ---------------------------------------------------

    def _claim_single_instance(self) -> bool:
        self._guard = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._guard.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
            return True
        except OSError:
            return False

    # -- menu ----------------------------------------------------------------

    def _menu(self) -> pystray.Menu:
        def device_item(name: str):
            return pystray.MenuItem(
                name,
                lambda icon, item: self._select(name),
                checked=lambda item, n=name: self.selected == n,
                radio=True)

        names = sorted({d["name"] for d in self.discovery.list_devices()})
        device_items = [device_item(n) for n in names] or [
            pystray.MenuItem("(discovering...)", None, enabled=False)]
        return pystray.Menu(
            pystray.MenuItem(
                lambda item: "Stop casting" if self.session else "Start casting",
                self._toggle, default=True),
            pystray.Menu.SEPARATOR,
            *device_items,
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("Refresh devices", lambda icon, item: icon.update_menu()),
            pystray.MenuItem("Quit", self._quit),
        )

    def _select(self, name: str) -> None:
        self.selected = name
        self.cfg["last_device"] = name
        cfg_mod.save(self.cfg)

    # -- casting ---------------------------------------------------------------

    def _toggle(self, icon, item) -> None:
        if self.session:
            self._stop_cast()
        else:
            threading.Thread(target=self._start_cast, daemon=True).start()

    def _start_cast(self) -> None:
        if not self.selected:
            self.icon.notify("Pick a speaker in the menu first", "No device selected")
            return
        try:
            self.capture = LoopbackCapture(on_data=lambda d: self.pacer.feed(d),
                                           device_hint=self.cfg["capture_device"])
            self.server = StreamServer(self.capture.format, self.cfg["port"])
            self.pacer = Pacer(self.capture.format, sink=self.server.feed)
            self.server.start()
            self.pacer.start()
            self.capture.start()

            safe_cast = self.discovery.connect(self.selected)
            if self.cfg["mute_local_while_casting"]:
                self.mute.engage()
            self.session = CastSession(
                self.discovery, safe_cast, self.cfg["port"], self.cfg["stream_type"],
                self.capture, on_event=lambda m: self.icon.notify(m, "Casting"))
            self.session.start()
            self.icon.icon = _make_icon(True)
            self.icon.update_menu()
            self.icon.notify(f"Casting to {self.selected}", "Started")
        except Exception as e:
            log.exception("start cast failed")
            self.icon.notify(str(e)[:220], "Cast failed")
            self._teardown_pipeline()

    def _stop_cast(self) -> None:
        if self.session:
            self.session.stop()
            self.session = None
        self.mute.release()
        self._teardown_pipeline()
        self.icon.icon = _make_icon(False)
        self.icon.update_menu()

    def _teardown_pipeline(self) -> None:
        for obj in (self.capture, self.pacer, self.server):
            if obj is not None:
                try:
                    obj.stop()
                except Exception as e:
                    log.debug("teardown: %s", e)
        self.capture = self.pacer = self.server = None

    # -- lifecycle ---------------------------------------------------------------

    def _quit(self, icon, item) -> None:
        self._stop_cast()
        self.discovery.stop()
        icon.stop()

    def run(self) -> int:
        if not self._claim_single_instance():
            log.error("another instance is already running")
            return 1
        recover_from_crash()
        self.discovery.wait_for_devices(4)
        self.icon.menu = self._menu()
        self.icon.run()
        return 0


def main() -> int:
    cfg_mod.setup_logging()
    return TrayApp().run()


if __name__ == "__main__":
    raise SystemExit(main())

"""Tray icon (pystray) + entry point wiring icon, popover and controller.

The icon is a view: left-click toggles the popover (never casts), right-click
offers Open / Exit, and the glyph colour tracks controller state:
grey idle, amber transitional, blue casting, red error.

Single instance: a localhost guard port doubles as a control channel - a
second launch sends SHOW to the first instance (which pops the popover) and
exits, so autostart + manual launch is never a silent no-op.
"""
from __future__ import annotations

import logging
import socket
import threading

import pystray
from PIL import Image, ImageDraw

from . import config as cfg_mod
from . import startup
from .appctl import AppController, TRANSITIONAL_STATES
from .ui.popover import Popover

log = logging.getLogger(__name__)

SINGLE_INSTANCE_PORT = 48765

COLORS = {
    "idle": (138, 138, 138, 255),
    "busy": (249, 171, 0, 255),
    "playing": (66, 133, 244, 255),
    "error": (217, 48, 37, 255),
}


def _glyph(color) -> Image.Image:
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    d.polygon([(14, 24), (26, 24), (38, 12), (38, 52), (26, 40), (14, 40)], fill=color)
    d.arc([40, 20, 56, 44], start=-50, end=50, fill=color, width=4)
    return img


def _state_color(state: str):
    if state == "PLAYING":
        return COLORS["playing"]
    if state == "ERROR":
        return COLORS["error"]
    if state in TRANSITIONAL_STATES:
        return COLORS["busy"]
    return COLORS["idle"]


class TrayApp:
    def __init__(self):
        self.ctl = AppController()
        self.popover = Popover(self.ctl, on_exit=self._exit)
        self.icon = pystray.Icon(
            "desktop-audio-streamer", _glyph(COLORS["idle"]),
            "Desktop Audio Streamer",
            menu=pystray.Menu(
                pystray.MenuItem("Open", self._show, default=True),
                pystray.MenuItem("Exit", lambda icon, item: self._exit()),
            ))
        self.ctl.add_listener(self._on_event)

    # -- single instance + control channel ---------------------------------

    def claim_instance(self) -> bool:
        self._guard = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        try:
            self._guard.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
        except OSError:
            # An instance exists - ask it to show its popover, then bow out.
            try:
                with socket.create_connection(
                        ("127.0.0.1", SINGLE_INSTANCE_PORT), timeout=2) as s:
                    s.sendall(b"SHOW\n")
            except OSError:
                pass
            return False
        self._guard.listen(2)
        threading.Thread(target=self._control_loop, name="instance-ctl",
                         daemon=True).start()
        return True

    def _control_loop(self) -> None:
        while True:
            try:
                conn, _ = self._guard.accept()
                with conn:
                    if b"SHOW" in conn.recv(16):
                        self.popover.post(self.popover.show)
            except OSError:
                return

    # -- events --------------------------------------------------------------

    def _on_event(self, kind: str, *args) -> None:
        if kind != "state":
            return
        state, detail = args
        self.icon.icon = _glyph(_state_color(state))
        if state == "PLAYING" and detail:
            name = detail.split("|", 1)[0]
            self.icon.title = f"Casting to {name}"
        elif state == "ERROR":
            self.icon.title = f"Error: {detail}"
            try:
                self.icon.notify(str(detail)[:200], "Desktop Audio Streamer")
            except Exception:
                pass
        else:
            self.icon.title = "Desktop Audio Streamer"

    def _show(self, icon=None, item=None) -> None:
        # pystray thread -> marshal into the Tk thread
        self.popover.post(self.popover.toggle)

    def _exit(self) -> None:
        log.info("exit requested")
        self.icon.visible = False
        self.ctl.shutdown(then=lambda: (self.icon.stop(), self.popover.quit()))

    # -- lifecycle --------------------------------------------------------------

    def run(self) -> int:
        if not self.claim_instance():
            log.info("another instance is running - asked it to show; exiting")
            return 0
        startup.repair_if_stale()
        self.icon.run_detached()          # pystray gets its own thread
        self.popover.run()                # tkinter owns the main thread
        return 0


def main() -> int:
    cfg_mod.setup_logging()
    return TrayApp().run()


if __name__ == "__main__":
    raise SystemExit(main())

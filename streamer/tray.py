"""Tray icon (pystray) + entry point wiring icon, popover and controller.

The icon is a view: left-click toggles the popover (never casts), right-click
offers Open / Exit, and the glyph colour tracks controller state:
grey idle, amber transitional, blue casting, red error.

Single instance: a localhost guard port doubles as a control channel - a
second launch sends SHOW to the first instance (which pops the popover) and
exits, so autostart + manual launch is never a silent no-op. The port is
claimed in main(), BEFORE any AppController is built, because constructing one
runs endpoint crash-recovery and a loser must not touch the winner's live cast.
"""
from __future__ import annotations

import logging
import socket
import threading

import pystray
from PIL import Image, ImageDraw, ImageFont

from . import config as cfg_mod
from . import firewall, startup
from .appctl import AppController, TRANSITIONAL_STATES
from .ui.fonts import ASSETS, ICONS
from .ui.popover import Popover

log = logging.getLogger(__name__)

SINGLE_INSTANCE_PORT = 48765

# GM3-dark state tints for the standard Cast glyph
COLORS = {
    "idle": (196, 199, 197, 255),     # on-surface-variant
    "busy": (253, 214, 99, 255),      # amber
    "playing": (168, 199, 250, 255),  # primary
    "error": (242, 184, 181, 255),    # error
}

_icon_font = None


def _glyph(color, connected: bool = False) -> Image.Image:
    """The standardised Chromecast icon (Material 'cast' glyph), tinted."""
    global _icon_font
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    try:
        if _icon_font is None:
            _icon_font = ImageFont.truetype(
                str(ASSETS / "MaterialIconsRound-Regular.otf"), 58)
        char = ICONS["cast_connected"] if connected else ICONS["cast"]
        d.text((32, 32), char, font=_icon_font, fill=color, anchor="mm")
    except OSError as e:
        log.warning("icon font unavailable (%s); drawing fallback", e)
        d.rounded_rectangle([6, 12, 58, 48], radius=6, outline=color, width=5)
    return img


def _state_color(state: str):
    if state == "PLAYING":
        return COLORS["playing"]
    if state == "ERROR":
        return COLORS["error"]
    if state in TRANSITIONAL_STATES:
        return COLORS["busy"]
    return COLORS["idle"]


def claim_instance() -> socket.socket | None:
    """Bind the guard port, returning the listening socket - or None if another
    instance already holds it (having first asked it to show its popover).

    Called BEFORE any AppController exists. That ordering is load-bearing:
    AppController.__init__ runs recover_from_crash(), which restores the
    endpoint from the mute marker and then deletes it. A second launch during a
    live cast therefore used to un-mute the PC mid-cast and destroy the running
    instance's crash-safety net - so if that instance later died hard, the
    endpoint stayed muted with nothing left to recover from."""
    guard = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        guard.bind(("127.0.0.1", SINGLE_INSTANCE_PORT))
    except OSError:
        guard.close()
        # An instance exists - ask it to show its popover, then bow out.
        try:
            with socket.create_connection(
                    ("127.0.0.1", SINGLE_INSTANCE_PORT), timeout=2) as s:
                s.sendall(b"SHOW\n")
        except OSError as e:
            # Either the port belongs to something that isn't us, or the owner
            # bound but is not listening yet. Either way we cannot hand off.
            log.warning("guard port %d is taken but did not answer (%s)",
                        SINGLE_INSTANCE_PORT, e)
        return None
    guard.listen(2)
    return guard


class TrayApp:
    def __init__(self, guard: socket.socket):
        self._guard = guard
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

    def _serve_control(self) -> None:
        threading.Thread(target=self._control_loop, name="instance-ctl",
                         daemon=True).start()

    def _control_loop(self) -> None:
        """Serve SHOW requests for the lifetime of the process.

        Only an accept() failure ends this loop. Per-connection errors are
        contained: a client that connects and resets (an EDR/AV agent probing
        localhost listeners will) raises ConnectionResetError - an OSError - and
        used to kill the loop for good. After that every later launch still
        connected and sent successfully, because the listening socket's backlog
        accepts it, so each one believed it had handed off and exited. The app
        became a permanent silent no-op until the orphan was killed by hand."""
        while True:
            try:
                conn, _ = self._guard.accept()
            except OSError as e:
                # Nothing can be served after this, so drop the port too. Left
                # bound, it would keep completing connections from the backlog:
                # every later launch would "hand off" to a loop that is gone and
                # exit silently. Closing it makes the next launch's bind()
                # succeed and take ownership instead.
                log.warning("instance control channel stopped: %s", e)
                try:
                    self._guard.close()
                except OSError:
                    pass
                return
            try:
                with conn:
                    # A peer that connects and says nothing must not wedge the
                    # single-threaded loop and lock out every future launch.
                    conn.settimeout(2)
                    if b"SHOW" in conn.recv(16):
                        self.popover.post(self.popover.show)
            except OSError as e:
                log.debug("control connection dropped: %s", e)

    # -- events --------------------------------------------------------------

    def _on_event(self, kind: str, *args) -> None:
        if kind != "state":
            return
        state, detail = args
        self.icon.icon = _glyph(_state_color(state), connected=state == "PLAYING")
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
        self._serve_control()
        startup.repair_if_stale()
        threading.Thread(
            target=lambda: firewall.ensure_allowed(self.ctl.cfg, cfg_mod.save),
            name="fw-register", daemon=True).start()
        self.icon.run_detached()          # pystray gets its own thread
        self.popover.run()                # tkinter owns the main thread
        return 0


def main() -> int:
    cfg_mod.setup_logging()
    guard = claim_instance()
    if guard is None:
        log.info("another instance is running - asked it to show; exiting")
        return 0
    return TrayApp(guard).run()


if __name__ == "__main__":
    raise SystemExit(main())

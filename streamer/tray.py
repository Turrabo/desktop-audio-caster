"""Tray icon (pystray) + entry point wiring icon, popover and controller.

The icon is a view: left-click toggles the popover (never casts), right-click
offers Open / Exit (plus "Update to vX" once a newer release is out - see
updater.py), and the glyph colour tracks controller state:
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
from PIL import Image, ImageDraw, ImageFont

from . import config as cfg_mod
from . import firewall, startup, updater
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


class TrayApp:
    def __init__(self):
        self.ctl = AppController()
        self.popover = Popover(self.ctl, on_exit=self._exit)
        self.checker = updater.UpdateChecker(on_found=self._on_update_found)
        self._updating = False
        self.icon = pystray.Icon(
            "desktop-audio-streamer", _glyph(COLORS["idle"]),
            "Desktop Audio Streamer",
            menu=pystray.Menu(
                pystray.MenuItem("Open", self._show, default=True),
                pystray.MenuItem(
                    lambda item: f"Update to v{self.checker.available.version}",
                    lambda icon, item: self._start_update(),
                    visible=lambda item: self.checker.available is not None,
                    enabled=lambda item: not self._updating),
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

    # -- self-update ----------------------------------------------------------

    def _notify(self, text: str) -> None:
        try:
            self.icon.notify(text, "Desktop Audio Streamer")
        except Exception:
            pass

    def _on_update_found(self, update) -> None:
        self.icon.update_menu()
        self._notify(f"Version {update.version} is available. Right-click the "
                     "tray icon and choose Update.")

    def _casting(self) -> bool:
        return self.ctl.session is not None or self.ctl.busy()

    def _start_update(self) -> None:
        """Download, verify, hand over to the installer step, exit. Only while
        idle: replacing the app mid-cast would cut the room off."""
        update, exe = self.checker.available, updater.frozen_exe()
        if update is None or exe is None or self._updating:
            return
        if self._casting():
            self._notify("Stop casting first, then choose Update.")
            return
        self._updating = True
        self.icon.update_menu()

        def run():
            dest = exe.with_name(updater.UPDATE_NAME)
            try:
                log.info("downloading update %s", update.version)
                updater.download(update, dest)
                if self._casting():
                    raise RuntimeError("casting started during the download")
                updater.hand_over(dest, exe)
            except Exception as exc:
                log.error("update failed: %s", exc)
                dest.unlink(missing_ok=True)
                self._updating = False
                self.icon.update_menu()
                self._notify(f"Update failed: {exc}")
                return
            log.info("handing over to update %s", update.version)
            self._exit()
        threading.Thread(target=run, name="update-apply", daemon=True).start()

    def _exit(self) -> None:
        log.info("exit requested")
        self.icon.visible = False
        self.checker.stop()
        self.ctl.shutdown(then=lambda: (self.icon.stop(), self.popover.quit()))

    # -- lifecycle --------------------------------------------------------------

    def run(self) -> int:
        if not self.claim_instance():
            log.info("another instance is running - asked it to show; exiting")
            return 0
        startup.repair_if_stale()
        exe = updater.frozen_exe()
        if exe is not None:                 # self-update is for the exe only
            updater.cleanup(exe.parent)
            self.checker.start()
        threading.Thread(
            target=lambda: firewall.ensure_allowed(self.ctl.cfg, cfg_mod.save),
            name="fw-register", daemon=True).start()
        self.icon.run_detached()          # pystray gets its own thread
        self.popover.run()                # tkinter owns the main thread
        return 0


def main() -> int:
    cfg_mod.setup_logging()
    return TrayApp().run()


if __name__ == "__main__":
    raise SystemExit(main())

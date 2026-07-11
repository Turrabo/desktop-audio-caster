"""Material 3 dark popover (Google Home idiom): cast-state header with chip,
Groups / Speakers device cards, M3 sliders, switch, exit.

Threading: this module owns the Tk main thread. Everything from other threads
(controller events, pystray clicks, volume sweep results) arrives through ONE
queue.Queue drained by a 50 ms after-loop. No tk call is ever made off-thread.
"""
from __future__ import annotations

import ctypes
import logging
import queue
import time
import tkinter as tk

from .. import startup
from .fonts import ICON_FONT, ICONS
from .widgets import (ACCENT, BG, CARD, CHIP_BG, CHIP_FG, ERROR_BG, ERROR_FG,
                      FONT, OUTLINE_VAR, SEMIBOLD, SUBTEXT, TEXT, WARN,
                      DeviceIcon, IconButton, Slider, Toggle, rounded_rect)

log = logging.getLogger(__name__)

POLL_MS = 50
MAGIC = "#010203"          # transparent-color key for rounded window corners
WIDTH = 372
RADIUS = 28                # M3 extra-large container
MARGIN = 16                # window inner margin

STATE_TEXT = {
    "IDLE": "Not casting",
    "DISCOVERING": "Finding speakers…",
    "CONNECTING": "Connecting to {d}…",
    "LAUNCHING": "Starting receiver on {d}…",
    "WAITING_STREAM": "Waiting for {d} to fetch the stream…",
    "BUFFERING": "{d} is buffering…",
    "RECONNECTING": "Reconnecting: {d}",
    "STOPPING": "Stopping…",
    "ERROR": "{d}",
}


def _dpi_aware() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


def _work_area_at(x: int, y: int) -> tuple[int, int, int, int]:
    try:
        class RECT(ctypes.Structure):
            _fields_ = [("left", ctypes.c_long), ("top", ctypes.c_long),
                        ("right", ctypes.c_long), ("bottom", ctypes.c_long)]

        class MONITORINFO(ctypes.Structure):
            _fields_ = [("cbSize", ctypes.c_ulong), ("rcMonitor", RECT),
                        ("rcWork", RECT), ("dwFlags", ctypes.c_ulong)]

        class POINT(ctypes.Structure):
            _fields_ = [("x", ctypes.c_long), ("y", ctypes.c_long)]

        mon = ctypes.windll.user32.MonitorFromPoint(POINT(x, y), 2)
        mi = MONITORINFO()
        mi.cbSize = ctypes.sizeof(MONITORINFO)
        ctypes.windll.user32.GetMonitorInfoW(mon, ctypes.byref(mi))
        w = mi.rcWork
        return w.left, w.top, w.right, w.bottom
    except Exception:
        return 0, 0, 1920, 1040


class Popover:
    CARD_W = WIDTH - 2 * MARGIN
    CARD_R = 20
    CARD_PAD = 16

    def __init__(self, ctl, on_exit):
        _dpi_aware()
        self.ctl = ctl
        self.on_exit = on_exit
        self.ui_queue: queue.Queue = queue.Queue()

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("Desktop Audio Streamer")

        self.win = tk.Toplevel(self.root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        try:
            self.win.attributes("-transparentcolor", MAGIC)
        except tk.TclError:
            pass
        self.win.configure(bg=MAGIC)

        self.backdrop = tk.Canvas(self.win, bg=MAGIC, highlightthickness=0)
        self.backdrop.pack(fill="both", expand=True)
        self.frame = tk.Frame(self.win, bg=BG)

        self._rows: dict[str, dict] = {}
        self._visible = False

        self._build_static()
        ctl.add_listener(self._on_ctl_event)
        self.root.after(POLL_MS, self._drain)

    # ---- cross-thread plumbing --------------------------------------------

    def post(self, fn) -> None:
        self.ui_queue.put(fn)

    def _drain(self) -> None:
        try:
            while True:
                fn = self.ui_queue.get_nowait()
                try:
                    fn()
                except Exception as e:
                    log.debug("ui op failed: %s", e)
        except queue.Empty:
            pass
        self.root.after(POLL_MS, self._drain)

    def _on_ctl_event(self, kind: str, *args) -> None:
        if kind == "state":
            state, detail = args
            self.post(lambda: self._apply_state(state, detail))
        elif kind == "devices":
            self.post(self._devices_changed)
        elif kind == "mute":
            engaged = args[0]
            self.post(lambda: self._mute_label(engaged))

    # ---- static layout ------------------------------------------------------

    def _build_static(self) -> None:
        f = self.frame

        header = tk.Frame(f, bg=BG)
        header.pack(fill="x", pady=(4, 12))
        self.header_icon = tk.Label(header, text=ICONS["cast"], fg=SUBTEXT,
                                    bg=BG, font=(ICON_FONT, -22))
        self.header_icon.pack(side="left", padx=(2, 10))
        self.status_lbl = tk.Label(header, text="Not casting", fg=TEXT, bg=BG,
                                   font=(SEMIBOLD, -16), anchor="w")
        self.status_lbl.pack(side="left", fill="x", expand=True)

        # chip canvas (pill) shown only for PLAYING / ERROR
        self.chip = tk.Canvas(f, height=32, bg=BG, highlightthickness=0)
        self.chip_visible = False

        self.mute_lbl = tk.Label(f, text="", fg=SUBTEXT, bg=BG,
                                 font=(FONT, -12), anchor="w")
        self.mute_lbl.pack(fill="x", padx=2)

        self.groups_hdr = self._section_label("Groups")
        self.groups_frame = tk.Frame(f, bg=BG)
        self.groups_frame.pack(fill="x")
        self.speakers_hdr = self._section_label("Speakers")
        self.speakers_frame = tk.Frame(f, bg=BG)
        self.speakers_frame.pack(fill="x")

        footer = tk.Frame(f, bg=BG)
        footer.pack(fill="x", pady=(16, 4))
        tk.Label(footer, text="Start with Windows", fg=SUBTEXT, bg=BG,
                 font=(FONT, -14)).pack(side="left", padx=(2, 12))
        self.startup_toggle = Toggle(footer, on_change=self._toggle_startup)
        self.startup_toggle.pack(side="left")

        exit_lbl = tk.Label(footer, text="Exit", fg=SUBTEXT, bg=BG,
                            font=(FONT, -14), cursor="hand2", padx=12)
        exit_lbl.pack(side="right")
        exit_lbl.bind("<ButtonRelease-1>", lambda e: self._exit())
        exit_lbl.bind("<Enter>", lambda e: exit_lbl.configure(fg=TEXT))
        exit_lbl.bind("<Leave>", lambda e: exit_lbl.configure(fg=SUBTEXT))

        self.win.bind("<Escape>", lambda e: self.hide())
        self.win.bind("<FocusOut>", self._maybe_close)

    def _section_label(self, text: str) -> tk.Label:
        lbl = tk.Label(self.frame, text=text, fg=SUBTEXT, bg=BG,
                       font=(SEMIBOLD, -14), anchor="w")
        lbl.pack(fill="x", pady=(16, 8), padx=2)
        return lbl

    # ---- device cards ------------------------------------------------------

    def _devices_changed(self) -> None:
        self._rebuild_rows()
        if self._visible:
            self._resweep()

    def _rebuild_rows(self) -> None:
        for fr in (self.groups_frame, self.speakers_frame):
            for child in fr.winfo_children():
                child.destroy()
        self._rows.clear()

        devs = self.ctl.devices()
        for section, frame in (("groups", self.groups_frame),
                               ("speakers", self.speakers_frame)):
            items = devs[section]
            if not items:
                tk.Label(frame, text="none found", fg=SUBTEXT, bg=BG,
                         font=(FONT, -13, "italic")).pack(anchor="w", padx=6)
            for d in items:
                self._make_card(frame, d)

        for name, level in self.ctl.volumes.known_levels.items():
            self._volume_arrived(name, level, from_cache=True)
        self._apply_state(self.ctl.state, self.ctl.state_detail)
        self._resize()

    def _make_card(self, parent: tk.Frame, d: dict) -> None:
        name = d["name"]
        cw, pad = self.CARD_W, self.CARD_PAD
        card = tk.Canvas(parent, width=cw, height=10, bg=BG,
                         highlightthickness=0)
        card.pack(pady=(0, 8))

        inner = tk.Frame(card, bg=CARD)
        card.create_window(pad, pad, window=inner, anchor="nw",
                           width=cw - 2 * pad)

        top = tk.Frame(inner, bg=CARD)
        top.pack(fill="x")
        DeviceIcon(top, kind=d["type"], bg=CARD).pack(side="left",
                                                      padx=(0, 16))
        text_col = tk.Frame(top, bg=CARD)
        text_col.pack(side="left", fill="x", expand=True)
        tk.Label(text_col, text=name, fg=TEXT, bg=CARD, anchor="w",
                 font=(SEMIBOLD, -16)).pack(fill="x")
        kind_line = ("Speaker group" if d["type"] == "group"
                     else d.get("model") or "Speaker")
        sub = tk.Label(text_col, text=kind_line, fg=SUBTEXT, bg=CARD,
                       anchor="w", font=(FONT, -14))
        sub.pack(fill="x", pady=(2, 0))
        btn = IconButton(top, on_click=lambda n=name: self._toggle_cast(n),
                         bg=CARD)
        btn.pack(side="right", padx=(12, 0))

        bottom = tk.Frame(inner, bg=CARD)
        bottom.pack(fill="x", pady=(12, 0))
        pct = tk.Label(bottom, text="–", fg=SUBTEXT, bg=CARD, width=4,
                       anchor="e", font=(FONT, -12))
        slider = Slider(
            bottom,
            on_change=lambda v, n=name: self._slider_change(n, v),
            on_release=lambda v, n=name: self._slider_release(n, v),
            bg=CARD, width=cw - 2 * pad - 44)
        slider.pack(side="left")
        pct.pack(side="right")

        inner.update_idletasks()
        ch = inner.winfo_reqheight() + 2 * pad
        card.configure(height=ch)
        rect = rounded_rect(card, 0, 0, cw, ch, self.CARD_R, fill=CARD,
                            outline="")
        card.tag_lower(rect)

        self._rows[name] = {"slider": slider, "button": btn, "sub": sub,
                            "pct": pct, "kind_line": kind_line}

    # ---- casting -----------------------------------------------------------

    def _toggle_cast(self, name: str) -> None:
        if self.ctl.busy():
            return
        if self.ctl.cast_target == name:
            self.ctl.stop_cast()
        else:
            self.ctl.start_cast(name)

    def _show_chip(self, text: str, icon: str, bg: str, fg: str) -> None:
        c = self.chip
        c.delete("all")
        c.configure(bg=BG)
        pad_x = 16
        f = (FONT, -14)
        tmp = c.create_text(0, 0, text=text, font=f, anchor="nw")
        x1, y1, x2, y2 = c.bbox(tmp)
        c.delete(tmp)
        w = (x2 - x1) + pad_x * 2 + 26
        rounded_rect(c, 0, 0, w, 32, 16, fill=bg, outline="")
        c.create_text(pad_x, 16, text=ICONS[icon], fill=fg, anchor="w",
                      font=(ICON_FONT, -18))
        c.create_text(pad_x + 26, 16, text=text, fill=fg, anchor="w", font=f)
        if not self.chip_visible:
            self.chip.pack(fill="x", pady=(0, 4), after=self.status_lbl.master)
            self.chip_visible = True

    def _hide_chip(self) -> None:
        if self.chip_visible:
            self.chip.pack_forget()
            self.chip_visible = False

    def _apply_state(self, state: str, detail: str | None) -> None:
        lag = None
        if state == "PLAYING" and detail and "|" in detail:
            detail, lag = detail.split("|", 1)

        if state == "PLAYING":
            self.status_lbl.configure(text=f"Casting to {detail}")
            self.header_icon.configure(text=ICONS["cast_connected"], fg=ACCENT)
            self._show_chip(f"Casting · lag {lag}s" if lag else "Casting",
                            "cast_connected", CHIP_BG, CHIP_FG)
        elif state == "ERROR":
            self.status_lbl.configure(text="Problem")
            self.header_icon.configure(text=ICONS["cast"], fg=ERROR_FG)
            self._show_chip(STATE_TEXT["ERROR"].format(d=detail or "error"),
                            "cast", ERROR_BG, ERROR_FG)
        else:
            text = STATE_TEXT.get(state, state).format(d=detail or "")
            self.status_lbl.configure(text=text)
            self.header_icon.configure(
                text=ICONS["cast"],
                fg=SUBTEXT if state in ("IDLE", "DISCOVERING") else WARN)
            self._hide_chip()

        busy = self.ctl.busy()
        for name, w in self._rows.items():
            is_target = self.ctl.cast_target == name
            w["button"].configure_state("stop" if is_target else "play",
                                        enabled=not busy)
            if is_target and state == "PLAYING":
                w["sub"].configure(text=f"Casting · lag {lag}s" if lag
                                   else "Casting", fg=ACCENT)
            elif w["sub"].cget("fg") == ACCENT:
                w["sub"].configure(text=w["kind_line"], fg=SUBTEXT)
        self._resize()

    def _mute_label(self, engaged: bool) -> None:
        self.mute_lbl.configure(
            text="PC output muted while casting" if engaged else "")

    # ---- volume ---------------------------------------------------------------

    def _slider_change(self, name: str, value: float) -> None:
        self._rows[name]["pct"].configure(text=f"{value:.0f}%")
        self.ctl.volumes.set_volume_debounced(name, value / 100.0)

    def _slider_release(self, name: str, value: float) -> None:
        self._rows[name]["pct"].configure(text=f"{value:.0f}%")
        self.ctl.volumes.set_volume_debounced(name, value / 100.0)

    def _volume_arrived(self, name: str, level: float,
                        from_cache: bool = False) -> None:
        w = self._rows.get(name)
        if w is None:
            return
        slider = w["slider"]
        if slider.dragging:
            return
        if not from_cache and time.monotonic() - \
                self.ctl.volumes.last_write.get(name, 0) < 1.5:
            return
        slider.set_value(level * 100)
        w["pct"].configure(text=f"{level * 100:.0f}%")

    def _resweep(self) -> None:
        names = [d["name"] for sec in self.ctl.devices().values() for d in sec]
        self.ctl.volumes.open_sweep(
            names, lambda n, lvl: self.post(lambda: self._volume_arrived(n, lvl)))

    # ---- show/hide ------------------------------------------------------------------

    def toggle(self) -> None:
        if self._visible:
            self.hide()
        else:
            self.show()

    def show(self) -> None:
        self._rebuild_rows()
        self.startup_toggle.set(startup.is_enabled())
        self._apply_state(self.ctl.state, self.ctl.state_detail)
        self._resweep()
        self.win.deiconify()
        self._resize()
        self._position()
        self._visible = True
        self.win.after(10, self.win.focus_force)

    def hide(self) -> None:
        if not self._visible:
            return
        self._visible = False
        self.win.withdraw()
        self.ctl.volumes.close_all()

    def _maybe_close(self, _event) -> None:
        def check():
            focus = self.win.focus_get()
            if focus is None or not str(focus).startswith(str(self.win)):
                self.hide()
        self.win.after(60, check)

    def _resize(self) -> None:
        self.frame.update_idletasks()
        w = WIDTH
        h = self.frame.winfo_reqheight() + 2 * MARGIN
        self.win.geometry(f"{w}x{h}")
        self.backdrop.configure(width=w, height=h)
        self.backdrop.delete("all")
        rounded_rect(self.backdrop, 0, 0, w, h, RADIUS, fill=BG,
                     outline=OUTLINE_VAR)
        self.backdrop.create_window(MARGIN, MARGIN, window=self.frame,
                                    anchor="nw", width=w - 2 * MARGIN)

    def _position(self) -> None:
        self.win.update_idletasks()
        px, py = self.root.winfo_pointerxy()
        w = self.win.winfo_width() or WIDTH
        h = self.win.winfo_height()
        left, top, right, bottom = _work_area_at(px, py)
        x = min(max(px - w // 2, left + 8), right - w - 8)
        y = py - h - 14
        if y < top + 8:
            y = min(py + 14, bottom - h - 8)
        self.win.geometry(f"+{x}+{y}")

    # ---- startup / exit ------------------------------------------------------------------

    def _toggle_startup(self, on: bool) -> None:
        try:
            if on:
                startup.enable()
            else:
                startup.disable()
        except Exception as e:
            log.warning("startup toggle failed: %s", e)
            self.startup_toggle.set(startup.is_enabled())

    def _exit(self) -> None:
        self.hide()
        self.on_exit()

    def run(self) -> None:
        self.root.mainloop()

    def quit(self) -> None:
        self.post(self.root.destroy)

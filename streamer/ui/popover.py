"""Google Home-style popover: status header, GROUPS / SPEAKERS device cards
with cast toggles and volume sliders, startup toggle, exit.

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
from .widgets import (ACCENT, BG, CARD, ERROR, FONT, GOOD, SUBTEXT, TEXT,
                      TRACK, WARN, DeviceIcon, IconButton, Slider, Toggle,
                      rounded_rect)

log = logging.getLogger(__name__)

POLL_MS = 50
MAGIC = "#010203"          # transparent-color key for rounded window corners
WIDTH = 372
RADIUS = 14

STATE_DOT = {"IDLE": SUBTEXT, "PLAYING": GOOD, "ERROR": ERROR}

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

        # Rounded backdrop; content frame sits inside its margin so the
        # corner curves stay visible.
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
        header.pack(fill="x", pady=(2, 8))
        self.dot = tk.Canvas(header, width=10, height=10, bg=BG,
                             highlightthickness=0)
        self.dot_id = self.dot.create_oval(1, 1, 9, 9, fill=SUBTEXT, outline="")
        self.dot.pack(side="left", padx=(2, 8), pady=4)
        self.status_lbl = tk.Label(header, text="Not casting", fg=TEXT, bg=BG,
                                   font=(FONT, 11, "bold"), anchor="w")
        self.status_lbl.pack(side="left", fill="x", expand=True)

        self.mute_lbl = tk.Label(f, text="", fg=SUBTEXT, bg=BG, font=(FONT, 8),
                                 anchor="w")
        self.mute_lbl.pack(fill="x", padx=2)

        self.groups_hdr = self._section_label("GROUPS")
        self.groups_frame = tk.Frame(f, bg=BG)
        self.groups_frame.pack(fill="x")
        self.speakers_hdr = self._section_label("SPEAKERS")
        self.speakers_frame = tk.Frame(f, bg=BG)
        self.speakers_frame.pack(fill="x")

        footer = tk.Frame(f, bg=BG)
        footer.pack(fill="x", pady=(10, 2))
        tk.Label(footer, text="Start with Windows", fg=SUBTEXT, bg=BG,
                 font=(FONT, 9)).pack(side="left", padx=(2, 8))
        self.startup_toggle = Toggle(footer, on_change=self._toggle_startup)
        self.startup_toggle.pack(side="left")

        exit_lbl = tk.Label(footer, text="Exit", fg=SUBTEXT, bg=BG,
                            font=(FONT, 10), cursor="hand2", padx=10)
        exit_lbl.pack(side="right")
        exit_lbl.bind("<ButtonRelease-1>", lambda e: self._exit())
        exit_lbl.bind("<Enter>", lambda e: exit_lbl.configure(fg=ERROR))
        exit_lbl.bind("<Leave>", lambda e: exit_lbl.configure(fg=SUBTEXT))

        self.win.bind("<Escape>", lambda e: self.hide())
        self.win.bind("<FocusOut>", self._maybe_close)

    def _section_label(self, text: str) -> tk.Label:
        lbl = tk.Label(self.frame, text=text, fg=SUBTEXT, bg=BG,
                       font=(FONT, 8, "bold"), anchor="w")
        lbl.pack(fill="x", pady=(8, 4), padx=2)
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
                tk.Label(frame, text="none found", fg=TRACK, bg=BG,
                         font=(FONT, 9, "italic")).pack(anchor="w", padx=6)
            for d in items:
                self._make_card(frame, d)

        # Apply cached levels instantly - rebuilt sliders must never sit
        # dead waiting for a sweep (the vanished-handles bug).
        for name, level in self.ctl.volumes.known_levels.items():
            self._volume_arrived(name, level, from_cache=True)
        self._apply_state(self.ctl.state, self.ctl.state_detail)
        self._resize()

    CARD_W = WIDTH - 24
    CARD_H = 96
    CARD_R = 12

    def _make_card(self, parent: tk.Frame, d: dict) -> None:
        name = d["name"]
        cw = self.CARD_W
        card = tk.Canvas(parent, width=cw, height=self.CARD_H, bg=BG,
                         highlightthickness=0)
        card.pack(pady=4)

        inner = tk.Frame(card, bg=CARD)
        card.create_window(14, 10, window=inner, anchor="nw", width=cw - 28)

        top = tk.Frame(inner, bg=CARD)
        top.pack(fill="x")
        DeviceIcon(top, kind=d["type"], bg=CARD).pack(side="left", padx=(0, 10))
        text_col = tk.Frame(top, bg=CARD)
        text_col.pack(side="left", fill="x", expand=True)
        tk.Label(text_col, text=name, fg=TEXT, bg=CARD, anchor="w",
                 font=(FONT, 11, "bold")).pack(fill="x")
        kind_line = ("Speaker group" if d["type"] == "group"
                     else d.get("model") or "Speaker")
        sub = tk.Label(text_col, text=kind_line, fg=SUBTEXT, bg=CARD,
                       anchor="w", font=(FONT, 8))
        sub.pack(fill="x")
        btn = IconButton(top, on_click=lambda n=name: self._toggle_cast(n),
                         bg=CARD)
        btn.pack(side="right")

        bottom = tk.Frame(inner, bg=CARD)
        bottom.pack(fill="x", pady=(7, 0))
        pct = tk.Label(bottom, text="–", fg=SUBTEXT, bg=CARD, width=5,
                       anchor="e", font=(FONT, 9))
        slider = Slider(
            bottom,
            on_change=lambda v, n=name: self._slider_change(n, v),
            on_release=lambda v, n=name: self._slider_release(n, v),
            bg=CARD, width=cw - 28 - 52)
        slider.pack(side="left")
        pct.pack(side="right")

        # Size the card to its actual content, then paint the rounded
        # backdrop UNDER the embedded frame (fixed heights clip the slider
        # row on scaled DPI).
        inner.update_idletasks()
        ch = inner.winfo_reqheight() + 20
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

    def _apply_state(self, state: str, detail: str | None) -> None:
        lag = None
        if state == "PLAYING" and detail and "|" in detail:
            detail, lag = detail.split("|", 1)
        text = STATE_TEXT.get(state, state).format(d=detail or "")
        if state == "PLAYING":
            text = f"Casting to {detail}"
        self.dot.itemconfigure(self.dot_id, fill=STATE_DOT.get(state, WARN))
        self.status_lbl.configure(text=text)

        busy = self.ctl.busy()
        for name, w in self._rows.items():
            is_target = self.ctl.cast_target == name
            w["button"].configure_state("stop" if is_target else "play",
                                        enabled=not busy)
            if is_target and state == "PLAYING":
                w["sub"].configure(text=f"casting · lag {lag}s" if lag
                                   else "casting", fg=ACCENT)
            elif w["sub"].cget("fg") == ACCENT:
                w["sub"].configure(fg=SUBTEXT)
                self._refresh_sub(name)

    def _mute_label(self, engaged: bool) -> None:
        self.mute_lbl.configure(
            text="PC output muted while casting" if engaged else "")

    # ---- volume ---------------------------------------------------------------

    def _slider_change(self, name: str, value: float) -> None:
        self._refresh_sub(name, value)
        self.ctl.volumes.set_volume_debounced(name, value / 100.0)

    def _slider_release(self, name: str, value: float) -> None:
        self._refresh_sub(name, value)
        self.ctl.volumes.set_volume_debounced(name, value / 100.0)

    def _refresh_sub(self, name: str, value: float | None = None) -> None:
        w = self._rows.get(name)
        if w is None:
            return
        if value is None:
            cached = self.ctl.volumes.known_levels.get(name)
            value = cached * 100 if cached is not None else None
        w["pct"].configure(text=f"{value:.0f}%" if value is not None else "–")
        if self.ctl.cast_target != name:
            w["sub"].configure(text=w["kind_line"], fg=SUBTEXT)

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
        self._refresh_sub(name, level * 100)

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
        h = self.frame.winfo_reqheight() + 24
        self.win.geometry(f"{w}x{h}")
        self.backdrop.configure(width=w, height=h)
        self.backdrop.delete("all")
        rounded_rect(self.backdrop, 0, 0, w, h, RADIUS, fill=BG, outline="#3a3a41")
        self.backdrop.create_window(12, 12, window=self.frame, anchor="nw",
                                    width=w - 24)

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

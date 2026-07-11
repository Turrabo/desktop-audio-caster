"""Tkinter popover: cast targets (GROUPS / SPEAKERS), volume, status, startup.

Threading: this module owns the Tk main thread. Everything arriving from other
threads (controller events, pystray clicks, volume sweep results) comes in
through ONE queue.Queue drained by a 50 ms after-loop. No tk call is ever made
off-thread.
"""
from __future__ import annotations

import ctypes
import logging
import queue
import time
import tkinter as tk
from tkinter import ttk

from .. import startup

log = logging.getLogger(__name__)

POLL_MS = 50

STATE_COLORS = {
    "IDLE": "#8a8a8a",
    "PLAYING": "#4285f4",
    "ERROR": "#d93025",
}
AMBER = "#f9ab00"

STATE_TEXT = {
    "IDLE": "Not casting",
    "DISCOVERING": "Finding speakers…",
    "CONNECTING": "Connecting to {d}…",
    "LAUNCHING": "Starting receiver on {d}…",
    "WAITING_STREAM": "Waiting for {d} to fetch the stream…",
    "BUFFERING": "{d} is buffering…",
    "RECONNECTING": "Reconnecting: {d}",
    "STOPPING": "Stopping…",
    "ERROR": "Error: {d}",
}


def _dpi_aware() -> None:
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
    except Exception:
        pass


def _work_area_at(x: int, y: int) -> tuple[int, int, int, int]:
    """Work area (left, top, right, bottom) of the monitor containing x,y."""
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
        self.win.configure(bg="#202124", padx=1, pady=1)

        self.frame = tk.Frame(self.win, bg="#2d2e31", padx=12, pady=10)
        self.frame.pack(fill="both", expand=True)

        self._rows: dict[str, dict] = {}       # name -> widgets
        self._dragging: str | None = None      # device being volume-dragged
        self._updating_ui = False              # programmatic slider set guard
        self._visible = False

        self._build_static()
        ctl.add_listener(self._on_ctl_event)   # arbitrary threads -> enqueue
        self.root.after(POLL_MS, self._drain)

    # ---- cross-thread plumbing ------------------------------------------

    def post(self, fn) -> None:
        """Safe from any thread."""
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
            self.post(self._rebuild_rows)
        elif kind == "mute":
            engaged = args[0]
            self.post(lambda: self._mute_label(engaged))

    # ---- static layout ---------------------------------------------------

    def _build_static(self) -> None:
        f = self.frame
        status = tk.Frame(f, bg=f["bg"])
        status.pack(fill="x", pady=(0, 8))
        self.dot = tk.Canvas(status, width=12, height=12, bg=f["bg"],
                             highlightthickness=0)
        self.dot_id = self.dot.create_oval(2, 2, 11, 11,
                                           fill=STATE_COLORS["IDLE"], outline="")
        self.dot.pack(side="left", padx=(0, 6))
        self.status_lbl = tk.Label(status, text="Not casting", fg="#e8eaed",
                                   bg=f["bg"], font=("Segoe UI", 10))
        self.status_lbl.pack(side="left")

        self.mute_lbl = tk.Label(f, text="", fg="#9aa0a6", bg=f["bg"],
                                 font=("Segoe UI", 8))
        self.mute_lbl.pack(fill="x")

        self.groups_frame = self._section("GROUPS")
        self.speakers_frame = self._section("SPEAKERS")

        bottom = tk.Frame(f, bg=f["bg"])
        bottom.pack(fill="x", pady=(10, 0))
        self.startup_var = tk.BooleanVar(value=startup.is_enabled())
        tk.Checkbutton(bottom, text="Start with Windows",
                       variable=self.startup_var, command=self._toggle_startup,
                       fg="#e8eaed", bg=f["bg"], selectcolor="#202124",
                       activebackground=f["bg"], activeforeground="#e8eaed",
                       font=("Segoe UI", 9)).pack(side="left")
        tk.Button(bottom, text="Exit", command=self._exit, fg="#e8eaed",
                  bg="#3c4043", activebackground="#5f6368", bd=0, padx=12,
                  font=("Segoe UI", 9)).pack(side="right")

        self.win.bind("<Escape>", lambda e: self.hide())
        self.win.bind("<FocusOut>", self._maybe_close)

    def _section(self, title: str) -> tk.Frame:
        lbl = tk.Label(self.frame, text=title, fg="#9aa0a6", bg=self.frame["bg"],
                       font=("Segoe UI", 8, "bold"), anchor="w")
        lbl.pack(fill="x", pady=(6, 2))
        frame = tk.Frame(self.frame, bg=self.frame["bg"])
        frame.pack(fill="x")
        return frame

    # ---- device rows -----------------------------------------------------------

    def _rebuild_rows(self) -> None:
        for child in self.groups_frame.winfo_children():
            child.destroy()
        for child in self.speakers_frame.winfo_children():
            child.destroy()
        self._rows.clear()

        devs = self.ctl.devices()
        for section, frame in (("groups", self.groups_frame),
                               ("speakers", self.speakers_frame)):
            items = devs[section]
            if not items:
                tk.Label(frame, text="(none found)", fg="#5f6368",
                         bg=frame["bg"], font=("Segoe UI", 9)).pack(anchor="w")
            for d in items:
                self._make_row(frame, d)
        self._resize()

    def _make_row(self, parent: tk.Frame, d: dict) -> None:
        name = d["name"]
        row = tk.Frame(parent, bg=parent["bg"])
        row.pack(fill="x", pady=2)

        is_target = self.ctl.cast_target == name
        btn = tk.Button(row, text="■" if is_target else "▶", width=3, bd=0,
                        fg="#ffffff", bg="#4285f4" if is_target else "#3c4043",
                        activebackground="#5f6368",
                        command=lambda: self._toggle_cast(name))
        btn.pack(side="left", padx=(0, 8))

        lbl = tk.Label(row, text=name, fg="#e8eaed", bg=row["bg"], width=16,
                       anchor="w", font=("Segoe UI", 10))
        lbl.pack(side="left")

        var = tk.DoubleVar(value=0)
        scale = ttk.Scale(row, from_=0, to=100, orient="horizontal", length=110,
                          variable=var,
                          command=lambda v, n=name: self._on_slider(n, float(v)))
        scale.state(["disabled"])  # enabled when its level arrives
        scale.pack(side="left", padx=(6, 0))
        scale.bind("<ButtonPress-1>", lambda e, n=name: self._drag_start(n))
        scale.bind("<ButtonRelease-1>", lambda e, n=name: self._drag_end(n))

        pct = tk.Label(row, text="–", fg="#9aa0a6", bg=row["bg"], width=4,
                       font=("Segoe UI", 8))
        pct.pack(side="left")

        self._rows[name] = {"button": btn, "scale": scale, "var": var, "pct": pct}

    # ---- casting ---------------------------------------------------------------

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
            text = f"Casting to {detail}" + (f" — lag {lag} s" if lag else "")
        color = STATE_COLORS.get(state, AMBER)
        self.dot.itemconfigure(self.dot_id, fill=color)
        self.status_lbl.configure(text=text)

        busy = self.ctl.busy()
        for name, w in self._rows.items():
            is_target = self.ctl.cast_target == name
            w["button"].configure(
                text="■" if is_target else "▶",
                bg="#4285f4" if is_target else "#3c4043",
                state="disabled" if busy else "normal")

    def _mute_label(self, engaged: bool) -> None:
        self.mute_lbl.configure(
            text="PC output muted while casting" if engaged else "")

    # ---- volume ---------------------------------------------------------------

    def _drag_start(self, name: str) -> None:
        self._dragging = name

    def _drag_end(self, name: str) -> None:
        self._dragging = None
        w = self._rows.get(name)
        if w and "disabled" not in w["scale"].state():
            self.ctl.volumes.set_volume_debounced(name, w["var"].get() / 100.0)

    def _on_slider(self, name: str, value: float) -> None:
        w = self._rows.get(name)
        if w is None:
            return
        w["pct"].configure(text=f"{value:3.0f}%")
        if self._updating_ui or "disabled" in w["scale"].state():
            return  # programmatic set or not-yet-initialised slider
        self.ctl.volumes.set_volume_debounced(name, value / 100.0)

    def _volume_arrived(self, name: str, level: float) -> None:
        w = self._rows.get(name)
        if w is None:
            return
        # echo suppression: never fight an active drag or a fresh write
        if self._dragging == name:
            return
        if time.monotonic() - self.ctl.volumes.last_write.get(name, 0) < 1.5:
            return
        w["scale"].state(["!disabled"])
        self._updating_ui = True
        try:
            w["var"].set(level * 100)
        finally:
            self._updating_ui = False
        w["pct"].configure(text=f"{level * 100:3.0f}%")

    # ---- show/hide ------------------------------------------------------------------

    def toggle(self) -> None:
        if self._visible:
            self.hide()
        else:
            self.show()

    def show(self) -> None:
        self._rebuild_rows()
        self.startup_var.set(startup.is_enabled())
        self._apply_state(self.ctl.state, self.ctl.state_detail)

        # volume sweep: connections live only while the popover is open
        names = [d["name"] for sec in self.ctl.devices().values() for d in sec]
        self.ctl.volumes.open_sweep(
            names, lambda n, lvl: self.post(lambda: self._volume_arrived(n, lvl)))

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
        # Close only if focus genuinely left the popover's widget tree -
        # slider/checkbox focus shifts inside the window must not close it.
        def check():
            focus = self.win.focus_get()
            if focus is None or str(focus).startswith(str(self.win)) is False:
                self.hide()
        self.win.after(60, check)

    def _resize(self) -> None:
        self.win.update_idletasks()
        w = max(320, self.frame.winfo_reqwidth() + 2)
        h = self.frame.winfo_reqheight() + 2
        self.win.geometry(f"{w}x{h}")

    def _position(self) -> None:
        self.win.update_idletasks()
        px, py = self.root.winfo_pointerxy()
        w, h = self.win.winfo_width(), self.win.winfo_height()
        left, top, right, bottom = _work_area_at(px, py)
        x = min(max(px - w // 2, left + 8), right - w - 8)
        y = py - h - 12
        if y < top + 8:
            y = min(py + 12, bottom - h - 8)
        self.win.geometry(f"+{x}+{y}")

    # ---- startup / exit ------------------------------------------------------------------

    def _toggle_startup(self) -> None:
        try:
            if self.startup_var.get():
                startup.enable()
            else:
                startup.disable()
        except Exception as e:
            log.warning("startup toggle failed: %s", e)
            self.startup_var.set(startup.is_enabled())

    def _exit(self) -> None:
        self.hide()
        self.status_lbl.configure(text="Exiting…")
        self.on_exit()

    def run(self) -> None:
        self.root.mainloop()

    def quit(self) -> None:
        """Callable from any thread."""
        self.post(self.root.destroy)

"""Material 3 dark popover (Google Home idiom) with native Win11 chrome:
cast-state header, error banner, Groups / Speakers device cards in a
height-capped scroll region, M3 sliders, switch, exit.

Chrome: a plain solid-surface toplevel; DWM rounds the corners and draws the
flyout shadow (DWMWA_WINDOW_CORNER_PREFERENCE - verified on Win11 to round,
shadow, and survive -alpha fades on an overrideredirect Tk window; pre-Win11
machines just get square corners). No color-key transparency.

Scale: every metric and font size goes through dp() from the DPI of the
monitor under the cursor, probed at show() time (DAS_UI_SCALE env overrides
for screenshots); a scale change rebuilds the whole widget tree and clears
the render cache.

Threading: this module owns the Tk main thread. Everything from other threads
(controller events, pystray clicks, volume sweep results) arrives through ONE
queue.Queue drained by a 50 ms after-loop. No tk call is ever made off-thread.
"""
from __future__ import annotations

import ctypes
import logging
import os
import queue
import time
import tkinter as tk
import tkinter.font as tkfont

from .. import startup
from . import fonts, render
from .widgets import (ACCENT, BG, CARD, DIVIDER, ERROR, ERROR_BG, ERROR_FG,
                      SUBTEXT, TEXT, WARN, DeviceIcon, GlyphButton, IconButton,
                      OptionList, Slider, TextButton, Toggle, ellipsize)

# Latency slider maps its 0..100 travel onto this millisecond range.
DELAY_MIN, DELAY_MAX = 30, 500
DELAY_PRESETS = (("Low", 50), ("Balanced", 150), ("Safe", 400))
OUTPUT_OPTIONS = (
    ("speakers", "Speakers only", "Room hears it, this PC is muted."),
    ("this_pc", "This PC only", "You hear it here, speakers stay silent."),
    ("both", "Both", "Speaker volume follows this PC's volume."),
    ("auto", "Auto", "Your PC mute switches: muted plays the room, "
     "unmuted keeps it here."),
)

log = logging.getLogger(__name__)

POLL_MS = 50
BASE_W = 392               # logical window width (dp)
MARGIN = 20                # logical outer margin
GUTTER = 8                 # scroll-thumb gutter inside the content column
BORDER_COLORREF = 0x0033312F   # DWM hairline, 0x00BBGGRR of #2F3133

STATE_TEXT = {
    "IDLE": "Not casting",
    "DISCOVERING": "Finding speakers…",
    "CONNECTING": "Connecting to {d}…",
    "LAUNCHING": "Starting receiver on {d}…",
    "WAITING_STREAM": "Waiting for {d} to fetch the stream…",
    "BUFFERING": "{d} is buffering…",
    "RECONNECTING": "Reconnecting: {d}",
    "STOPPING": "Stopping…",
}


def _set_app_id() -> None:
    """Give Windows a stable app identity so the taskbar/Alt-Tab entry groups
    and labels as this app (not 'python'), and uses our icon - not pythonw's."""
    try:
        ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID(
            "Turrabo.DesktopAudioStreamer")
    except Exception:
        pass


def _dpi_aware() -> str:
    user32 = ctypes.windll.user32
    try:
        user32.SetProcessDpiAwarenessContext.restype = ctypes.c_bool
        user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
        if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
            return "per-monitor-v2"
    except (AttributeError, OSError):
        pass
    try:
        ctypes.windll.shcore.SetProcessDpiAwareness(2)
        return "per-monitor"
    except Exception:
        pass
    try:
        user32.SetProcessDPIAware()
    except Exception:
        pass
    return "system"


def _monitor_info_at(x: int, y: int) -> tuple[tuple[int, int, int, int], float]:
    """Work area and DPI scale of the monitor containing (x, y)."""
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
        try:
            dx, dy = ctypes.c_uint(), ctypes.c_uint()
            ctypes.windll.shcore.GetDpiForMonitor(
                mon, 0, ctypes.byref(dx), ctypes.byref(dy))
            scale = dx.value / 96.0
        except Exception:
            scale = 1.0
        return (w.left, w.top, w.right, w.bottom), scale
    except Exception:
        return (0, 0, 1920, 1040), 1.0


class Popover:
    def __init__(self, ctl, on_exit):
        _set_app_id()
        log.debug("dpi awareness: %s", _dpi_aware())
        fonts.ensure_fonts()
        self.ctl = ctl
        self.on_exit = on_exit
        self.ui_queue: queue.Queue = queue.Queue()

        self.root = tk.Tk()
        self.root.withdraw()
        self.root.title("Desktop Audio Streamer")
        try:
            # default= sets the process-wide window icon (all toplevels +
            # Alt-Tab), replacing pythonw's Python feather.
            self.root.iconbitmap(default=str(fonts.APP_ICO))
        except tk.TclError as e:
            log.debug("iconbitmap failed: %s", e)

        self.win = tk.Toplevel(self.root)
        self.win.withdraw()
        self.win.overrideredirect(True)
        self.win.attributes("-topmost", True)
        self.win.configure(bg=BG)

        self._rows: dict[str, dict] = {}
        self._dev_sig: tuple | None = None
        self._visible = False
        self._pending_target: str | None = None   # device the user is starting
        self._fade_after: str | None = None
        self._resweep_after: str | None = None
        self._pending_rebuild = False
        self._view = "devices"          # "devices" | "settings"
        self._last_sig = None
        self._wa = None
        self._anchor: tuple[int, int] | None = None
        self._h = 0
        self._scrollable = False
        self._wacc = self._sacc = 0.0
        try:
            self._scale_override = float(os.environ.get("DAS_UI_SCALE", ""))
        except ValueError:
            self._scale_override = None

        self.scale = 0.0
        self._build_static(self._scale_override or 1.0)

        self.win.bind("<Escape>", lambda e: self.hide())
        self.win.bind("<FocusOut>", self._maybe_close)
        ctl.add_listener(self._on_ctl_event)
        self.root.after(POLL_MS, self._drain)

    def dp(self, v: float) -> int:
        return round(v * self.scale)

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

    def _build_static(self, scale: float) -> None:
        """(Re)build the whole widget tree at the given DPI scale."""
        self.scale = scale
        render.clear_cache()
        for child in self.win.winfo_children():
            child.destroy()
        self._rows.clear()
        self._last_sig = None
        self._banner_visible = False
        self._mute_packed = False
        dp = self.dp

        self.win_w = dp(BASE_W)
        self.content_w = self.win_w - 2 * dp(MARGIN)
        self.card_w = self.content_w - dp(GUTTER)
        self._ss = render.supersample(scale)
        self._f_name = tkfont.Font(family=fonts.MEDIUM, size=-dp(16))
        self._f_sub = tkfont.Font(family=fonts.FONT, size=-dp(14))
        self._f_status = tkfont.Font(family=fonts.MEDIUM, size=-dp(16))

        f = self.frame = tk.Frame(self.win, bg=BG)
        f.pack(fill="both", expand=True, padx=dp(MARGIN),
               pady=(dp(14), dp(14)))

        # Fixed-height header: reserve two lines so status text flipping
        # through transitional states never reflows the cards below it.
        header_h = max(dp(28), 2 * self._f_status.metrics("linespace") + dp(2))
        self.header = tk.Frame(f, bg=BG, height=header_h)
        self.header.pack(fill="x", pady=(0, dp(6)))
        self.header.pack_propagate(False)
        self.header_icon = tk.Label(self.header, bg=BG)
        self.header_icon.pack(side="left", padx=(dp(2), dp(12)))
        self.gear_btn = GlyphButton(self.header, self.scale, "settings",
                                    on_click=self._toggle_settings, bg=BG)
        self.gear_btn.pack(side="right", padx=(dp(8), 0))
        self.status_lbl = tk.Label(
            self.header, text="Not casting", fg=TEXT, bg=BG, anchor="w",
            justify="left", font=self._f_status,
            wraplength=self.content_w - dp(44) - dp(44))
        self.status_lbl.pack(side="left", fill="x", expand=True)
        self._set_header_icon("cast", SUBTEXT)

        self.banner = tk.Canvas(f, bg=BG, highlightthickness=0,
                                width=self.content_w)

        self.mute_lbl = tk.Label(f, text="", fg=SUBTEXT, bg=BG,
                                 font=(fonts.FONT, -dp(12)), anchor="w")

        self.host = tk.Frame(f, bg=BG)
        self.host.pack(fill="both", expand=True, pady=(dp(4), 0))
        self.scroll_canvas = tk.Canvas(
            self.host, bg=BG, highlightthickness=0,
            width=self.content_w - dp(GUTTER), yscrollincrement=dp(20))
        self.scroll_canvas.pack(side="left", fill="y")
        self.thumb = tk.Canvas(self.host, width=dp(4), bg=BG,
                               highlightthickness=0)
        self.thumb.pack(side="right", fill="y", padx=(dp(4), 0))
        self.devices_frame = tk.Frame(self.scroll_canvas, bg=BG)
        self.scroll_canvas.create_window(
            0, 0, window=self.devices_frame, anchor="nw",
            width=self.content_w - dp(GUTTER))
        self.devices_frame.bind("<Configure>", self._on_devices_configure)

        # Settings view: built once, packed in place of `host` on demand.
        self.settings_frame = tk.Frame(f, bg=BG)
        self._build_settings(self.settings_frame, dp)

        self._divider = tk.Frame(f, bg=DIVIDER, height=max(1, dp(1)))
        self._divider.pack(fill="x", pady=(dp(12), 0))
        footer = tk.Frame(f, bg=BG)
        footer.pack(fill="x", pady=(dp(10), 0))
        tk.Label(footer, text="Start with Windows", fg=TEXT, bg=BG,
                 font=(fonts.FONT, -dp(14))).pack(side="left", padx=(dp(2), dp(12)))
        self.startup_toggle = Toggle(footer, self.scale,
                                     on_change=self._toggle_startup)
        self.startup_toggle.pack(side="left")
        TextButton(footer, self.scale, "Exit", self._exit).pack(side="right")
        if self._view == "settings":
            self._show_settings_view()      # survive a DPI rebuild while open

    def _build_settings(self, parent, dp) -> None:
        """Latency slider (with preset chips) + output-mode list."""
        cw = self.content_w
        # -- Latency ----------------------------------------------------------
        lat_head = tk.Frame(parent, bg=BG)
        lat_head.pack(fill="x", pady=(dp(2), dp(6)))
        tk.Label(lat_head, text="Latency", fg=TEXT, bg=BG, anchor="w",
                 font=(fonts.MEDIUM, -dp(15))).pack(side="left", padx=dp(2))
        self.lat_value = tk.Label(lat_head, text="", fg=ACCENT, bg=BG,
                                  anchor="e", font=(fonts.MEDIUM, -dp(14)))
        self.lat_value.pack(side="right", padx=dp(2))
        self.lat_slider = Slider(
            parent, self.scale, width=cw - dp(4),
            on_change=self._latency_change, on_release=self._latency_release,
            bg=BG)
        self.lat_slider.pack(fill="x", pady=(0, dp(4)))
        chips = tk.Frame(parent, bg=BG)
        chips.pack(fill="x", pady=(0, dp(4)))
        for label, ms in DELAY_PRESETS:
            chip = tk.Label(chips, text=label, fg=ACCENT, bg=BG, cursor="hand2",
                            font=(fonts.MEDIUM, -dp(12)))
            chip.pack(side="left", padx=dp(8))
            chip.bind("<Button-1>", lambda e, m=ms: self._latency_preset(m))
        self.lat_note = tk.Label(parent, text="", fg=SUBTEXT, bg=BG, anchor="w",
                                 justify="left", font=(fonts.FONT, -dp(11)),
                                 wraplength=cw - dp(4))
        self.lat_note.pack(fill="x", pady=(0, dp(2)))

        tk.Frame(parent, bg=DIVIDER, height=max(1, dp(1))).pack(
            fill="x", pady=(dp(12), dp(12)))

        # -- Output -----------------------------------------------------------
        tk.Label(parent, text="Play on", fg=TEXT, bg=BG, anchor="w",
                 font=(fonts.MEDIUM, -dp(15))).pack(fill="x", padx=dp(2),
                                                    pady=(0, dp(6)))
        self.output_list = OptionList(
            parent, self.scale, OUTPUT_OPTIONS, on_change=self._output_change,
            caption_wrap=cw - dp(34), bg=BG)
        self.output_list.pack(fill="x")

    def _set_header_icon(self, name: str, color: str) -> None:
        dp = self.dp
        box, px = dp(26), dp(24)
        key = ("hicon", name, color, box, self._ss)
        ph = render.photo(key, box, box, self._ss, BG,
                          lambda d, k: render.glyph(d, box * k / 2, box * k / 2,
                                                    name, px * k, color))
        self.header_icon.configure(image=ph)
        self.header_icon._ph = ph

    # ---- error banner (fixed, above the scroll region) ---------------------

    def _show_banner(self, text: str) -> None:
        dp, c = self.dp, self.banner
        c.delete("all")
        wrap = self.content_w - dp(44) - dp(16)
        font = (fonts.FONT, -dp(14))
        tmp = c.create_text(0, 0, text=text, font=font, width=wrap, anchor="nw")
        x1, y1, x2, y2 = c.bbox(tmp)
        c.delete(tmp)
        h = max(dp(46), (y2 - y1) + 2 * dp(13))
        cw = self.content_w

        def paint(d, k):
            d.rounded_rectangle([0, 0, cw * k, h * k], radius=dp(12) * k,
                                fill=ERROR_BG)
            render.glyph(d, dp(26) * k, h * k / 2, "error", dp(20) * k,
                         ERROR_FG)

        ph = render.photo(("banner", cw, h, self._ss), cw, h, self._ss, BG,
                          paint)
        c.configure(height=h)
        c.create_image(0, 0, anchor="nw", image=ph)
        c._ph = ph
        c.create_text(dp(44), h / 2, text=text, font=font, fill=ERROR_FG,
                      width=wrap, anchor="w")
        if not self._banner_visible:
            c.pack(fill="x", pady=(dp(2), dp(4)), after=self.header)
            self._banner_visible = True

    def _hide_banner(self) -> None:
        if self._banner_visible:
            self.banner.pack_forget()
            self._banner_visible = False

    def _mute_label(self, engaged: bool) -> None:
        if engaged and not self._mute_packed:
            self.mute_lbl.configure(text="PC output muted while casting")
            self.mute_lbl.pack(fill="x", padx=self.dp(2),
                               pady=(0, self.dp(2)), before=self.host)
            self._mute_packed = True
        elif not engaged and self._mute_packed:
            self.mute_lbl.pack_forget()
            self._mute_packed = False
        else:
            return
        if self._visible:
            self._resize()

    # ---- settings view -----------------------------------------------------

    def _toggle_settings(self) -> None:
        if self._view == "settings":
            self._show_devices_view()
        else:
            self._show_settings_view()

    def _show_settings_view(self) -> None:
        self._view = "settings"
        self.gear_btn.set_glyph("arrow_back")
        self.host.pack_forget()
        self.settings_frame.pack(fill="x", pady=(self.dp(4), 0),
                                 before=self._divider)
        self._refresh_settings_controls()
        if self._visible:
            self._resize()

    def _show_devices_view(self) -> None:
        self._view = "devices"
        self.gear_btn.set_glyph("settings")
        self.settings_frame.pack_forget()
        self.host.pack(fill="both", expand=True, pady=(self.dp(4), 0),
                       before=self._divider)
        if self._pending_rebuild and not self._any_dragging():
            self._pending_rebuild = False
            self._rebuild_rows()
            self._resweep()
        if self._visible:
            self._resize()

    def _refresh_settings_controls(self) -> None:
        ms = int(self.ctl.cfg.get("mirror_target_delay_ms", 400))
        self.lat_slider.set_value(self._ms_to_slider(ms))
        self.lat_value.configure(text=f"{ms} ms")
        self.output_list.set(self.ctl.cfg.get("output_mode", "speakers"))
        self._update_latency_note()

    def _update_latency_note(self) -> None:
        if self.ctl.cfg.get("cast_mode") == "http":
            note = ("Cast mode is set to standard; latency tuning applies to "
                    "the low-latency path only.")
        elif self.ctl.session is not None and self.ctl.cast_mode_active == "http":
            note = ("This speaker is on the standard path; tuning takes effect "
                    "on low-latency casts.")
        else:
            note = "Lower is snappier; very low can stutter on busy Wi-Fi."
        self.lat_note.configure(text=note)

    @staticmethod
    def _ms_to_slider(ms: int) -> float:
        return max(0.0, min(100.0,
                            (ms - DELAY_MIN) / (DELAY_MAX - DELAY_MIN) * 100))

    @staticmethod
    def _slider_to_ms(v: float) -> int:
        ms = DELAY_MIN + (DELAY_MAX - DELAY_MIN) * v / 100
        return int(round(ms / 5.0) * 5)      # snap to 5 ms

    def _latency_change(self, v: float) -> None:
        self.lat_value.configure(text=f"{self._slider_to_ms(v)} ms")

    def _latency_release(self, v: float) -> None:
        ms = self._slider_to_ms(v)
        self.lat_value.configure(text=f"{ms} ms")
        self.ctl.set_target_delay(ms)

    def _latency_preset(self, ms: int) -> None:
        self.lat_slider.set_value(self._ms_to_slider(ms))
        self.lat_value.configure(text=f"{ms} ms")
        self.ctl.set_target_delay(ms)

    def _output_change(self, mode: str) -> None:
        self.ctl.set_output_mode(mode)

    # ---- device cards ------------------------------------------------------

    def _any_dragging(self) -> bool:
        return any(r["slider"].dragging for r in self._rows.values())

    def _devices_sig(self) -> tuple:
        devs = self.ctl.devices()
        return tuple((d["name"], d["type"], d.get("model"))
                     for sec in ("groups", "speakers") for d in devs[sec])

    def _devices_changed(self) -> None:
        # zeroconf fires add/update/remove constantly (TXT record churn,
        # notably DURING cast warm-up); rebuilding cards for those makes the
        # whole popover flicker. Only rebuild when the visible device list
        # actually changed (the empty list is a valid, stable state too).
        sig = self._devices_sig()
        if sig == self._dev_sig and (self._rows or not sig):
            return
        if self._view == "settings" or self._any_dragging():
            self._pending_rebuild = True     # rebuild when the list returns
            return
        self._rebuild_rows()
        if self._visible:
            self._resweep()

    def _rebuild_rows(self) -> None:
        self._dev_sig = self._devices_sig()
        keep = self.scroll_canvas.yview()[0]
        for child in self.devices_frame.winfo_children():
            child.destroy()
        self._rows.clear()
        dp = self.dp

        devs = self.ctl.devices()
        if not devs["groups"] and not devs["speakers"]:
            tk.Label(self.devices_frame, text="No speakers found yet",
                     fg=SUBTEXT, bg=BG, font=(fonts.FONT, -dp(14))
                     ).pack(pady=dp(24))
        else:
            first = True
            for title, items in (("Groups", devs["groups"]),
                                 ("Speakers", devs["speakers"])):
                if not items:
                    continue
                tk.Label(self.devices_frame, text=title, fg=SUBTEXT, bg=BG,
                         font=(fonts.MEDIUM, -dp(14)), anchor="w"
                         ).pack(fill="x", pady=(dp(4) if first else dp(14), dp(8)),
                                padx=dp(2))
                first = False
                for d in items:
                    self._make_card(d)

        for name, level in self.ctl.volumes.known_levels.items():
            self._volume_arrived(name, level, from_cache=True)
        self._apply_state(self.ctl.state, self.ctl.state_detail)
        if self._visible:
            self._resize()
        self.scroll_canvas.yview_moveto(keep)

    def _make_card(self, d: dict) -> None:
        name = d["name"]
        dp = self.dp
        cw, pad = self.card_w, dp(16)
        card = tk.Canvas(self.devices_frame, width=cw, height=10, bg=BG,
                         highlightthickness=0)
        card.pack(pady=(0, dp(8)))

        inner = tk.Frame(card, bg=CARD)
        card.create_window(pad, pad, window=inner, anchor="nw",
                           width=cw - 2 * pad)

        top = tk.Frame(inner, bg=CARD)
        top.pack(fill="x")
        badge = DeviceIcon(top, self.scale, kind=d["type"], bg=CARD)
        badge.pack(side="left", padx=(0, dp(14)))
        btn = IconButton(top, self.scale,
                         on_click=lambda n=name: self._toggle_cast(n), bg=CARD)
        btn.pack(side="right", padx=(dp(12), 0))
        text_col = tk.Frame(top, bg=CARD)
        text_col.pack(side="left", fill="x", expand=True)
        text_w = cw - 2 * pad - dp(40) - dp(14) - dp(40) - dp(12)
        tk.Label(text_col, text=ellipsize(name, self._f_name, text_w),
                 fg=TEXT, bg=CARD, anchor="w", font=self._f_name).pack(fill="x")
        kind_line = ellipsize(("Speaker group" if d["type"] == "group"
                               else d.get("model") or "Speaker"),
                              self._f_sub, text_w)
        sub = tk.Label(text_col, text=kind_line, fg=SUBTEXT, bg=CARD,
                       anchor="w", font=self._f_sub)
        sub.pack(fill="x", pady=(dp(3), 0))

        bottom = tk.Frame(inner, bg=CARD)
        bottom.pack(fill="x", pady=(dp(12), 0))
        pct = tk.Label(bottom, text="–", fg=SUBTEXT, bg=CARD, width=4,
                       anchor="e", font=(fonts.FONT, -dp(12)))
        slider = Slider(
            bottom, self.scale,
            width=cw - 2 * pad - dp(44) - dp(8),
            on_change=lambda v, n=name: self._slider_change(n, v),
            on_release=lambda v, n=name: self._slider_release(n, v),
            bg=CARD)
        slider.pack(side="left")
        pct.pack(side="right")

        inner.update_idletasks()
        ch = inner.winfo_reqheight() + 2 * pad
        card.configure(height=ch)
        r = dp(16)
        bg_ph = render.photo(("cardbg", cw, ch, r, self._ss), cw, ch,
                             self._ss, BG,
                             lambda d_, k: d_.rounded_rectangle(
                                 [0, 0, cw * k, ch * k], radius=r * k,
                                 fill=CARD))
        item = card.create_image(0, 0, anchor="nw", image=bg_ph)
        card._ph = bg_ph
        card.tag_lower(item)

        self._rows[name] = {"slider": slider, "button": btn, "badge": badge,
                            "sub": sub, "pct": pct, "kind_line": kind_line}

    # ---- casting -----------------------------------------------------------

    def _toggle_cast(self, name: str) -> None:
        if self.ctl.busy():
            return
        if self.ctl.cast_target == name:
            self.ctl.stop_cast()
        else:
            self._pending_target = name    # spin this card until it plays
            self.ctl.start_cast(name)

    def _apply_state(self, state: str, detail: str | None) -> None:
        lag = None
        if state == "PLAYING" and detail and "|" in detail:
            detail, lag = detail.split("|", 1)

        if state == "PLAYING":
            self.status_lbl.configure(text=f"Casting to {detail}")
            self._set_header_icon("cast_connected", ACCENT)
            self._hide_banner()
        elif state == "ERROR":
            self.status_lbl.configure(text="Problem")
            self._set_header_icon("cast", ERROR)
            self._show_banner(str(detail) if detail else "Something went wrong")
        else:
            text = STATE_TEXT.get(state, state).format(d=detail or "")
            self.status_lbl.configure(text=text)
            self._set_header_icon(
                "cast", SUBTEXT if state in ("IDLE", "DISCOVERING") else WARN)
            self._hide_banner()

        busy = self.ctl.busy()
        if state in ("IDLE", "PLAYING", "ERROR"):
            self._pending_target = None
        # The device warming up (or being torn down): cast_target once the
        # session exists, else the card the user just tapped.
        warming = (self.ctl.cast_target or self._pending_target) if busy else None
        for name, w in self._rows.items():
            is_target = self.ctl.cast_target == name
            if busy and name == warming:
                w["button"].configure_state("busy", enabled=False)
            else:
                w["button"].configure_state("stop" if is_target else "play",
                                            enabled=not busy)
            w["badge"].set_active(is_target and state == "PLAYING")
            if is_target and state == "PLAYING":
                w["sub"].configure(text=f"Casting · lag {lag}s" if lag
                                   else "Casting", fg=ACCENT)
            elif w["sub"].cget("fg") == ACCENT:
                w["sub"].configure(text=w["kind_line"], fg=SUBTEXT)

        # Resize only when window geometry actually changes: the banner
        # appearing/disappearing/rewording. The header is fixed-height, so
        # ordinary state flips (connecting -> launching -> casting) and
        # PLAYING lag ticks must not touch the layout at all.
        sig = (self._banner_visible, detail if state == "ERROR" else None)
        if sig != self._last_sig:
            self._last_sig = sig
            if self._visible:
                self._resize()

    # ---- volume ---------------------------------------------------------------

    def _slider_change(self, name: str, value: float) -> None:
        self._rows[name]["pct"].configure(text=f"{value:.0f}%")
        self.ctl.volumes.set_volume_debounced(name, value / 100.0)

    def _slider_release(self, name: str, value: float) -> None:
        self._rows[name]["pct"].configure(text=f"{value:.0f}%")
        self.ctl.volumes.set_volume_debounced(name, value / 100.0)
        if self._pending_rebuild and not self._any_dragging():
            self._pending_rebuild = False
            self._rebuild_rows()
            if self._visible:
                self._resweep()

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

    def _periodic_resweep(self) -> None:
        # Rebuilds no longer piggyback on zeroconf churn, so refresh volumes
        # (changed from phones etc.) on a slow deliberate cadence instead.
        if not self._visible:
            self._resweep_after = None
            return
        self._resweep()
        self._resweep_after = self.win.after(30000, self._periodic_resweep)

    # ---- scrolling ------------------------------------------------------------

    def _on_devices_configure(self, _event) -> None:
        self.scroll_canvas.configure(
            scrollregion=(0, 0, self.content_w - self.dp(GUTTER),
                          self.devices_frame.winfo_reqheight()))
        self._update_thumb()

    def _update_thumb(self) -> None:
        self.thumb.delete("all")
        if not self._scrollable:
            return
        top, bottom = self.scroll_canvas.yview()
        h = self.thumb.winfo_height()
        if h <= 1 or bottom - top >= 1.0:
            return
        dp = self.dp
        th = max(dp(28), round((bottom - top) * h))
        th -= th % 4                      # quantize: bounded image cache
        y = min(round(top * h), h - th)
        w4 = dp(4)
        ph = render.photo(("thumb", w4, th, self._ss), w4, th, self._ss, BG,
                          lambda d, k: d.rounded_rectangle(
                              [0, 0, w4 * k, th * k], radius=w4 * k / 2,
                              fill=DIVIDER))
        self.thumb.create_image(0, y, anchor="nw", image=ph)
        self.thumb._ph = ph

    def _bind_wheel(self) -> None:
        self._wacc = self._sacc = 0.0
        self.root.bind_all("<MouseWheel>", self._on_wheel)

    def _on_wheel(self, e):
        if not self._visible:
            return None
        try:
            w = self.root.winfo_containing(e.x_root, e.y_root)
        except (KeyError, tk.TclError):
            w = None
        if w is None:
            return None
        if isinstance(w, Slider):
            self._wacc += e.delta / 120
            steps = int(self._wacc)
            self._wacc -= steps
            if steps:
                w.wheel(steps)
            return "break"
        if self._scrollable and str(w).startswith(str(self.win)):
            self._sacc += e.delta / 120
            steps = int(self._sacc)
            self._sacc -= steps
            if steps:
                self.scroll_canvas.yview_scroll(-steps * 3, "units")
                self._update_thumb()
            return "break"
        return None

    # ---- show/hide ------------------------------------------------------------------

    def toggle(self) -> None:
        if self._visible:
            self.hide()
        else:
            self.show()

    def show(self) -> None:
        px, py = self.root.winfo_pointerxy()
        wa, s = _monitor_info_at(px, py)
        if self._scale_override:
            s = self._scale_override
        s = round(s, 2)
        self._wa = wa
        self._cancel_fade()
        if s != self.scale:
            self._build_static(s)
        if self._view == "settings":        # always open on the device list
            self._view = "devices"
            self.gear_btn.set_glyph("settings")
            self.settings_frame.pack_forget()
            self.host.pack(fill="both", expand=True, pady=(self.dp(4), 0),
                           before=self._divider)
        self._rebuild_rows()
        self.startup_toggle.set(startup.is_enabled())
        self._resweep()
        if self._resweep_after is None:
            self._resweep_after = self.win.after(30000, self._periodic_resweep)

        self.win.attributes("-alpha", 0.0)
        self.win.deiconify()
        self._apply_dwm()
        self._visible = True
        self._anchor = None
        self._resize()
        x, y = self._final_pos(px, py)
        self._anchor = (x, y + self._h)
        self._bind_wheel()
        self.win.after(10, self.win.focus_force)
        self._animate_in(x, y)

    def hide(self) -> None:
        if not self._visible:
            return
        self._visible = False
        self._cancel_fade()
        if self._resweep_after is not None:
            try:
                self.win.after_cancel(self._resweep_after)
            except Exception:
                pass
            self._resweep_after = None
        self.root.unbind_all("<MouseWheel>")
        self.win.withdraw()
        self.win.attributes("-alpha", 1.0)
        self.ctl.volumes.close_all()

    def _maybe_close(self, _event) -> None:
        def check():
            focus = self.win.focus_get()
            if focus is None or not str(focus).startswith(str(self.win)):
                self.hide()
        self.win.after(60, check)

    # ---- chrome / geometry -----------------------------------------------------

    def _apply_dwm(self) -> None:
        """Native rounded corners + hairline + flyout shadow. Re-applied on
        every show: Tk can silently recreate the HWND. No-op pre-Win11."""
        try:
            self.win.update_idletasks()
            hwnd = ctypes.windll.user32.GetParent(self.win.winfo_id())
            dwm = ctypes.windll.dwmapi
            for attr, val in ((33, 2), (34, BORDER_COLORREF)):
                v = ctypes.c_int(val)
                dwm.DwmSetWindowAttribute(ctypes.c_void_p(hwnd), attr,
                                          ctypes.byref(v), 4)
        except Exception:
            pass

    def _resize(self) -> None:
        dp = self.dp
        self.frame.update_idletasks()
        devices_view = self._view == "devices"
        dev_h = 0
        if devices_view:
            dev_h = self.devices_frame.winfo_reqheight()
            self.scroll_canvas.configure(height=dev_h)
        self.frame.update_idletasks()
        total = self.frame.winfo_reqheight() + 2 * dp(14)
        left, top, right, bottom = self._wa or (0, 0, 1920, 1040)
        cap = (bottom - top) - dp(24)
        self._scrollable = devices_view and total > cap
        if self._scrollable:
            self.scroll_canvas.configure(
                height=max(dp(120), dev_h - (total - cap)))
            total = cap
        self._h = total
        if self._anchor is not None:
            # Height changed while visible (banner, device list): keep the
            # bottom edge pinned so growth never pushes past the work area.
            ax, abot = self._anchor
            y = max(top + dp(8), min(abot, bottom - dp(8)) - total)
            self.win.geometry(f"{self.win_w}x{total}+{ax}+{y}")
        else:
            self.win.geometry(f"{self.win_w}x{total}")
        self.win.update_idletasks()
        self._update_thumb()

    def _final_pos(self, px: int, py: int) -> tuple[int, int]:
        dp = self.dp
        left, top, right, bottom = self._wa
        w, h = self.win_w, self._h
        x = min(max(px - w // 2, left + dp(8)), right - w - dp(8))
        y = py - h - dp(12)
        if y < top + dp(8):
            y = min(py + dp(12), bottom - h - dp(8))
        return x, max(top + dp(8), y)

    def _animate_in(self, x: int, y: int) -> None:
        seq = ((0.25, 10), (0.55, 6), (0.8, 3), (0.95, 1), (1.0, 0))

        def step(i: int) -> None:
            self._fade_after = None
            if not self._visible:
                return
            alpha, off = seq[i]
            self.win.attributes("-alpha", alpha)
            self.win.geometry(f"+{x}+{y + self.dp(off)}")
            if i + 1 < len(seq):
                self._fade_after = self.win.after(24, lambda: step(i + 1))

        step(0)

    def _cancel_fade(self) -> None:
        if self._fade_after is not None:
            try:
                self.win.after_cancel(self._fade_after)
            except Exception:
                pass
            self._fade_after = None

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

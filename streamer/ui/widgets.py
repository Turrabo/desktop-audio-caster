"""Material 3 dark widgets (Google Home idiom), Canvas-drawn.

Palette and metrics follow the GM3 dark ramp as observed in Google Home /
Android 14: 16px twin-track slider with a 4x44 bar handle, filled/tonal 40px
icon buttons, 52x32 switch, Material Icons Round glyphs. Font sizes are
negative (= exact pixels) throughout.
"""
from __future__ import annotations

import tkinter as tk
from typing import Callable

from .fonts import ICON_FONT, ICONS, ensure_fonts

# --- GM3 dark color roles ---------------------------------------------------
BG = "#131314"            # surface
CARD = "#1E1F20"          # surface-container
CARD_HOVER = "#2E2F30"    # +8% on-surface
BADGE = "#333537"         # surface-container-highest
TEXT = "#E3E3E3"          # on-surface
SUBTEXT = "#C4C7C5"       # on-surface-variant
OUTLINE = "#8E918F"
OUTLINE_VAR = "#444746"
ACCENT = "#A8C7FA"        # primary
ON_ACCENT = "#062E6F"     # on-primary
TONAL = "#3E4759"         # secondary-container
ON_TONAL = "#DAE2F9"      # on-secondary-container
CHIP_BG = "#0842A0"       # primary-container
CHIP_FG = "#D3E3FD"       # on-primary-container
TRACK = TONAL             # slider inactive track
STOP_DOT = ON_TONAL
GOOD = "#6DD58C"
WARN = "#FDD663"
ERROR = "#F2B8B5"
ERROR_BG = "#8C1D18"
ERROR_FG = "#F9DEDC"
DISABLED_FILL = "#363738"
DISABLED_GLYPH = "#6A6B6C"

FONT = "Segoe UI"
SEMIBOLD = "Segoe UI Semibold"

ensure_fonts()


def icon_text(canvas: tk.Canvas, x, y, name: str, px: int, fill: str,
              anchor="center") -> int:
    return canvas.create_text(x, y, text=ICONS[name], fill=fill, anchor=anchor,
                              font=(ICON_FONT, -px))


def rounded_rect(canvas: tk.Canvas, x1, y1, x2, y2, r, **kw) -> int:
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


def asym_rrect(canvas: tk.Canvas, x1, y1, x2, y2, rl, rr, **kw) -> int:
    """Rounded rect with different left/right radii (M3 slider tracks)."""
    pts = [x1 + rl, y1, x2 - rr, y1, x2, y1, x2, y1 + rr, x2, y2 - rr, x2, y2,
           x2 - rr, y2, x1 + rl, y2, x1, y2, x1, y2 - rl, x1, y1 + rl, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


class Slider(tk.Canvas):
    """M3 (Android 14) slider: 16px active/inactive tracks with asymmetric
    corner radii, 6px gaps around a 4x44 vertical bar handle, 4px stop dot.

    Value 0..100. on_change fires during drag (consumer debounces);
    on_release at drag end. set_value() never fires callbacks. Unknown state
    (value None): inactive track only.
    """

    H = 44
    TRACK_H = 16
    HANDLE_W, HANDLE_H, HANDLE_R = 4, 44, 2
    GAP = 6
    R_OUT, R_IN = 8, 2

    def __init__(self, parent, on_change: Callable[[float], None],
                 on_release: Callable[[float], None], bg=CARD, width=220):
        self.W = width
        super().__init__(parent, width=self.W, height=self.H, bg=bg,
                         highlightthickness=0, cursor="hand2")
        self._on_change = on_change
        self._on_release = on_release
        self.value: float | None = None
        self.dragging = False
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)
        self._draw()

    def _x_for(self, value: float) -> float:
        usable = self.W - self.HANDLE_W
        return self.HANDLE_W / 2 + usable * (value / 100.0)

    def _value_for(self, x: float) -> float:
        usable = self.W - self.HANDLE_W
        return min(100.0, max(0.0, (x - self.HANDLE_W / 2) / usable * 100.0))

    def _draw(self) -> None:
        self.delete("all")
        cy = self.H / 2
        ty1, ty2 = cy - self.TRACK_H / 2, cy + self.TRACK_H / 2

        if self.value is None:
            asym_rrect(self, 0, ty1, self.W, ty2, self.R_OUT, self.R_OUT,
                       fill=TRACK, outline="")
            return

        x = self._x_for(self.value)
        hw = (2 if self.dragging else self.HANDLE_W)
        left_end = x - hw / 2 - self.GAP
        right_start = x + hw / 2 + self.GAP

        if left_end > self.R_OUT:
            asym_rrect(self, 0, ty1, left_end, ty2, self.R_OUT, self.R_IN,
                       fill=ACCENT, outline="")
        if right_start < self.W - self.R_OUT:
            asym_rrect(self, right_start, ty1, self.W, ty2, self.R_IN,
                       self.R_OUT, fill=TRACK, outline="")
            if self.value < 96:
                self.create_oval(self.W - 8 - 2, cy - 2, self.W - 8 + 2,
                                 cy + 2, fill=STOP_DOT, outline="")

        rounded_rect(self, x - hw / 2, cy - self.HANDLE_H / 2,
                     x + hw / 2, cy + self.HANDLE_H / 2, self.HANDLE_R,
                     fill=ACCENT, outline="")

    def set_value(self, value: float | None) -> None:
        self.value = None if value is None else min(100.0, max(0.0, value))
        self._draw()

    def _press(self, e) -> None:
        if self.value is None:
            return
        self.dragging = True
        self.value = self._value_for(e.x)
        self._draw()
        self._on_change(self.value)

    def _drag(self, e) -> None:
        if not self.dragging:
            return
        self.value = self._value_for(e.x)
        self._draw()
        self._on_change(self.value)

    def _release(self, e) -> None:
        if not self.dragging:
            return
        self.dragging = False
        self._draw()
        self._on_release(self.value)


class IconButton(tk.Canvas):
    """M3 icon button, 40px. play = filled tonal; stop = filled primary
    (Google Home's active-cast affordance - not red)."""

    D = 40

    def __init__(self, parent, on_click: Callable[[], None], bg=CARD):
        super().__init__(parent, width=self.D, height=self.D, bg=bg,
                         highlightthickness=0, cursor="hand2")
        self._on_click = on_click
        self.mode = "play"
        self.enabled = True
        self._hover = False
        self.bind("<ButtonRelease-1>", lambda e: self._click())
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self._draw()

    def _set_hover(self, on: bool) -> None:
        self._hover = on
        self._draw()

    def _click(self) -> None:
        if self.enabled:
            self._on_click()

    def configure_state(self, mode: str, enabled: bool) -> None:
        self.mode, self.enabled = mode, enabled
        self._draw()

    def _draw(self) -> None:
        self.delete("all")
        d = self.D
        if not self.enabled:
            fill, glyph_color = DISABLED_FILL, DISABLED_GLYPH
        elif self.mode == "stop":
            fill, glyph_color = ACCENT, ON_ACCENT
        else:
            fill, glyph_color = ("#485163" if self._hover else TONAL), ON_TONAL
        self.create_oval(0, 0, d, d, fill=fill, outline="")
        icon_text(self, d / 2, d / 2, "stop" if self.mode == "stop" else "play",
                  24, glyph_color)


class Toggle(tk.Canvas):
    """M3 switch: 52x32 track, 16px off-handle / 24px on-handle."""

    W, H = 52, 32

    def __init__(self, parent, on_change: Callable[[bool], None], bg=BG):
        super().__init__(parent, width=self.W, height=self.H, bg=bg,
                         highlightthickness=0, cursor="hand2")
        self._on_change = on_change
        self.on = False
        self.bind("<ButtonRelease-1>", lambda e: self._flip())
        self._draw()

    def set(self, on: bool) -> None:
        self.on = on
        self._draw()

    def _flip(self) -> None:
        self.on = not self.on
        self._draw()
        self._on_change(self.on)

    def _draw(self) -> None:
        self.delete("all")
        w, h = self.W, self.H
        if self.on:
            rounded_rect(self, 0, 0, w, h, h / 2, fill=ACCENT, outline="")
            cx, r = w - 16, 12
            self.create_oval(cx - r, h / 2 - r, cx + r, h / 2 + r,
                             fill=ON_ACCENT, outline="")
            icon_text(self, cx, h / 2, "check", 14, ACCENT)
        else:
            rounded_rect(self, 1, 1, w - 1, h - 1, (h - 2) / 2, fill=BADGE,
                         outline=OUTLINE, width=2)
            cx, r = 16, 8
            self.create_oval(cx - r, h / 2 - r, cx + r, h / 2 + r,
                             fill=OUTLINE, outline="")


class DeviceIcon(tk.Canvas):
    """40px badge circle with a Material glyph for the device kind."""

    D = 40

    def __init__(self, parent, kind: str, bg=CARD, active: bool = False):
        super().__init__(parent, width=self.D, height=self.D, bg=bg,
                         highlightthickness=0)
        d = self.D
        self.create_oval(0, 0, d, d, fill=BADGE, outline="")
        glyph = {"group": "speaker_group", "cast": "tv"}.get(kind, "speaker")
        icon_text(self, d / 2, d / 2, glyph, 24,
                  ACCENT if active else SUBTEXT)

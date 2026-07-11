"""Custom-drawn tkinter widgets in a Google Home-like dark idiom.

Everything is Canvas-based: rounded cards, a thin slider with a round thumb
and filled track, circular icon buttons, a pill toggle. Colors follow the
Material dark palette Google Home uses.
"""
from __future__ import annotations

import tkinter as tk
from typing import Callable

# Material-dark palette (Google Home-ish)
BG = "#1f1f23"          # window background
CARD = "#2b2b30"        # card background
CARD_HOVER = "#33333a"
TEXT = "#e8eaed"
SUBTEXT = "#9aa0a6"
ACCENT = "#8ab4f8"      # material dark blue
ACCENT_DIM = "#5f84c4"
TRACK = "#4a4a52"
GOOD = "#81c995"
WARN = "#fdd663"
ERROR = "#f28b82"
STOP_RED = "#f28b82"

FONT = "Segoe UI"


def rounded_rect(canvas: tk.Canvas, x1, y1, x2, y2, r, **kw) -> int:
    """Draw a rounded rectangle; returns the polygon id."""
    pts = [x1 + r, y1, x2 - r, y1, x2, y1, x2, y1 + r, x2, y2 - r, x2, y2,
           x2 - r, y2, x1 + r, y2, x1, y2, x1, y2 - r, x1, y1 + r, x1, y1]
    return canvas.create_polygon(pts, smooth=True, **kw)


class Slider(tk.Canvas):
    """Thin-track slider with round thumb. Value 0..100.

    on_change(value) fires during drag (the consumer debounces);
    on_release(value) fires at drag end. set_value() never fires callbacks.
    An 'unknown' state (no reading yet) renders a dim track with no thumb.
    """

    H = 28
    PAD = 10
    TRACK_W = 3
    THUMB_R = 7

    def __init__(self, parent, on_change: Callable[[float], None],
                 on_release: Callable[[float], None], bg=CARD, width=220):
        self.W = width
        super().__init__(parent, width=self.W, height=self.H, bg=bg,
                         highlightthickness=0, cursor="hand2")
        self._on_change = on_change
        self._on_release = on_release
        self.value: float | None = None   # None = unknown
        self.dragging = False
        self._bg = bg
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)
        self._draw()

    # geometry helpers
    def _x_for(self, value: float) -> float:
        usable = self.W - 2 * self.PAD
        return self.PAD + usable * (value / 100.0)

    def _value_for(self, x: float) -> float:
        usable = self.W - 2 * self.PAD
        return min(100.0, max(0.0, (x - self.PAD) / usable * 100.0))

    def _draw(self) -> None:
        self.delete("all")
        cy = self.H / 2
        if self.value is None:
            self.create_line(self.PAD, cy, self.W - self.PAD, cy,
                             fill=TRACK, width=self.TRACK_W, capstyle="round")
            self.create_text(self.W / 2, cy, text="…", fill=SUBTEXT,
                             font=(FONT, 7))
            return
        x = self._x_for(self.value)
        self.create_line(self.PAD, cy, self.W - self.PAD, cy,
                         fill=TRACK, width=self.TRACK_W, capstyle="round")
        if x > self.PAD:
            self.create_line(self.PAD, cy, x, cy, fill=ACCENT,
                             width=self.TRACK_W, capstyle="round")
        r = self.THUMB_R + (2 if self.dragging else 0)
        self.create_oval(x - r, cy - r, x + r, cy + r, fill=ACCENT, outline="")

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
    """Circular icon button: play / stop glyphs, accent when active."""

    D = 34

    def __init__(self, parent, on_click: Callable[[], None], bg=CARD):
        super().__init__(parent, width=self.D, height=self.D, bg=bg,
                         highlightthickness=0, cursor="hand2")
        self._on_click = on_click
        self.mode = "play"        # play | stop
        self.enabled = True
        self.bind("<ButtonRelease-1>", lambda e: self._click())
        self.bind("<Enter>", lambda e: self._draw(hover=True))
        self.bind("<Leave>", lambda e: self._draw())
        self._draw()

    def _click(self) -> None:
        if self.enabled:
            self._on_click()

    def configure_state(self, mode: str, enabled: bool) -> None:
        self.mode, self.enabled = mode, enabled
        self._draw()

    def _draw(self, hover: bool = False) -> None:
        self.delete("all")
        d, pad = self.D, 2
        if self.mode == "stop":
            ring = STOP_RED
        else:
            ring = ACCENT if self.enabled else TRACK
        fill = ring if (hover and self.enabled) else ""
        glyph = TEXT if fill else ring
        self.create_oval(pad, pad, d - pad, d - pad, outline=ring, width=2,
                         fill=fill)
        c = d / 2
        if self.mode == "play":
            self.create_polygon(c - 4, c - 6, c - 4, c + 6, c + 7, c,
                                fill=glyph, outline="")
        else:
            self.create_rectangle(c - 5, c - 5, c + 5, c + 5, fill=glyph,
                                  outline="")


class Toggle(tk.Canvas):
    """Small pill toggle (Start with Windows)."""

    W, H = 36, 20

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
        col = ACCENT if self.on else TRACK
        rounded_rect(self, 1, 3, w - 1, h - 3, (h - 6) / 2, fill=col, outline="")
        x = w - h / 2 - 1 if self.on else h / 2 + 1
        r = h / 2 - 2
        self.create_oval(x - r, h / 2 - r, x + r, h / 2 + r,
                         fill=TEXT, outline="")


class DeviceIcon(tk.Canvas):
    """Circular badge with a speaker / group glyph."""

    D = 34

    def __init__(self, parent, kind: str, bg=CARD):
        super().__init__(parent, width=self.D, height=self.D, bg=bg,
                         highlightthickness=0)
        d = self.D
        self.create_oval(2, 2, d - 2, d - 2, fill="#3a3a41", outline="")
        c = d / 2
        if kind == "group":
            # three small speakers
            for dx in (-7, 0, 7):
                self.create_rectangle(c + dx - 2.4, c - 5, c + dx + 2.4, c + 5,
                                      outline=SUBTEXT, width=1.2)
                self.create_oval(c + dx - 1.4, c + 0.6, c + dx + 1.4, c + 3.4,
                                 outline=SUBTEXT, width=1)
        else:
            self.create_rectangle(c - 5, c - 8, c + 5, c + 8, outline=SUBTEXT,
                                  width=1.4)
            self.create_oval(c - 2.6, c - 5.4, c + 2.6, c - 0.2,
                             outline=SUBTEXT, width=1.2)
            self.create_oval(c - 3.4, c + 0.6, c + 3.4, c + 7,
                             outline=SUBTEXT, width=1.2)

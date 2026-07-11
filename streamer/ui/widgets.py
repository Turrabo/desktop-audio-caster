"""Material 3 dark widgets (Google Home idiom), PIL-rendered.

Every control is imagery from render.py (supersampled Pillow, LANCZOS
downscale) because Tk Canvas cannot antialias. All metrics are computed per
instance from the DPI scale passed at construction - nothing is sized at
import time.

Metrics follow the GM3 dark ramp as observed in Google Home / Android 14:
16dp twin-track slider with a 4x44 bar handle (narrows to 2 pressed), tonal /
filled 40dp icon buttons, 52x32 switch, Material Icons Round glyphs.
"""
from __future__ import annotations

import tkinter as tk
import tkinter.font as tkfont
from typing import Callable

from . import render
from .fonts import ensure_fonts

# --- GM3 dark color roles ---------------------------------------------------
BG = "#131314"            # surface
CARD = "#1E1F20"          # surface-container
BADGE = "#333537"         # surface-container-highest
TEXT = "#E3E3E3"          # on-surface
SUBTEXT = "#C4C7C5"       # on-surface-variant
OUTLINE = "#8E918F"
OUTLINE_VAR = "#444746"   # spinner track
DIVIDER = "#2F3133"       # hairline on surface
ACCENT = "#A8C7FA"        # primary
ON_ACCENT = "#062E6F"     # on-primary
TONAL = "#3E4759"         # secondary-container
ON_TONAL = "#DAE2F9"      # on-secondary-container
TONAL_HOVER = "#485163"   # secondary-container +8% on-surface
EXIT_HOVER = "#252930"    # primary 12% state layer on surface
TRACK = TONAL             # slider inactive track
STOP_DOT = ON_TONAL
WARN = "#FDD663"
ERROR = "#F2B8B5"
ERROR_BG = "#8C1D18"      # error-container
ERROR_FG = "#F9DEDC"      # on-error-container
DISABLED_FILL = "#2A2B2C"
DISABLED_GLYPH = "#6A6B6C"

ensure_fonts()


def ellipsize(text: str, font: tkfont.Font, max_px: int) -> str:
    """Trim text with a trailing ellipsis to fit max_px (Tk labels hard-clip)."""
    if font.measure(text) <= max_px:
        return text
    ell = "…"
    while text and font.measure(text + ell) > max_px:
        text = text[:-1]
    return (text + ell) if text else ell


def _asym(d, x1, y1, x2, y2, rl, rr, fill) -> None:
    """Rounded rect with different left/right radii (M3 slider tracks).
    The corners= overlay keeps its radius strictly under half the box in
    both axes - Pillow's corner decomposition rejects the degenerate case."""
    if x2 - x1 < 2:
        return
    h2 = (y2 - y1) / 2
    lo, hi = min(rl, rr), max(rl, rr)
    if x2 - x1 < 2 * hi + 4:
        d.rounded_rectangle([x1, y1, x2, y2],
                            radius=min(h2, (x2 - x1) / 2), fill=fill)
        return
    d.rounded_rectangle([x1, y1, x2, y2], radius=min(lo, h2 - 1), fill=fill)
    r = max(1.0, min(hi, h2) - 1)
    if rl >= rr:
        d.rounded_rectangle([x1, y1, x1 + 2 * hi + 2, y2], radius=r,
                            corners=(True, False, False, True), fill=fill)
    else:
        d.rounded_rectangle([x2 - 2 * hi - 2, y1, x2, y2], radius=r,
                            corners=(False, True, True, False), fill=fill)


class Slider(tk.Canvas):
    """M3 (Android 14) slider: 16dp active/inactive tracks with asymmetric
    corner radii, 6dp gaps around a 4x44 bar handle, 4dp stop dot.

    Value 0..100. on_change fires during drag and on wheel nudges (consumer
    debounces); on_release at drag end. set_value() never fires callbacks.
    Unknown state (value None): inactive track only, inputs ignored.
    Redraws are skipped unless the integer percent (or drag state) changed.
    """

    def __init__(self, parent, scale: float, width: int,
                 on_change: Callable[[float], None],
                 on_release: Callable[[float], None], bg=CARD):
        dp = lambda v: round(v * scale)
        self.W, self.H = width, dp(44)
        self.TRACK = dp(16)
        self.GAP = dp(6)
        self.HW = max(dp(4), 3)
        self.HW_PRESSED = max(dp(2), 2)
        self.R_OUT, self.R_IN = dp(8), max(dp(2), 1)
        self.DOT_R = max(dp(2), 2)
        self.DOT_INSET = dp(8)
        super().__init__(parent, width=self.W, height=self.H, bg=bg,
                         highlightthickness=0, cursor="hand2")
        self._bg = bg
        self._ss = render.supersample(scale)
        self._on_change = on_change
        self._on_release = on_release
        self.value: float | None = None
        self.dragging = False
        self._item = self.create_image(0, 0, anchor="nw")
        self._photo = None
        self._drawn: tuple | None = None
        self.bind("<ButtonPress-1>", self._press)
        self.bind("<B1-Motion>", self._drag)
        self.bind("<ButtonRelease-1>", self._release)
        self._draw()

    # geometry: handle center travels [HW/2 .. W-HW/2] for value 0..100
    def _x_for(self, value: float) -> float:
        return self.HW / 2 + (self.W - self.HW) * (value / 100.0)

    def _value_for(self, x: float) -> float:
        return min(100.0, max(0.0, (x - self.HW / 2) / (self.W - self.HW) * 100.0))

    def _draw(self) -> None:
        iv = None if self.value is None else round(self.value)
        state = (iv, self.dragging)
        if state == self._drawn:
            return
        self._drawn = state
        W, H = self.W, self.H

        def paint(d, k):
            cy = H * k / 2
            t = self.TRACK * k / 2
            ty1, ty2 = cy - t, cy + t
            if iv is None:
                d.rounded_rectangle([0, ty1, W * k, ty2],
                                    radius=self.R_OUT * k, fill=TRACK)
                return
            hw = (self.HW_PRESSED if self.dragging else self.HW) * k
            cx = self._x_for(iv) * k
            gap = self.GAP * k
            _asym(d, 0, ty1, cx - hw / 2 - gap, ty2,
                  self.R_OUT * k, self.R_IN * k, ACCENT)
            _asym(d, cx + hw / 2 + gap, ty1, W * k, ty2,
                  self.R_IN * k, self.R_OUT * k, TRACK)
            if iv < 96:
                dx, r = (W - self.DOT_INSET) * k, self.DOT_R * k
                d.ellipse([dx - r, cy - r, dx + r, cy + r], fill=STOP_DOT)
            d.rounded_rectangle([cx - hw / 2, 0, cx + hw / 2, H * k],
                                radius=hw / 2, fill=ACCENT)

        self._photo = render.photo_live(W, H, self._ss, self._bg, paint)
        self.itemconfigure(self._item, image=self._photo)

    def set_value(self, value: float | None) -> None:
        self.value = None if value is None else min(100.0, max(0.0, value))
        self._draw()

    def wheel(self, steps: int) -> None:
        """Nudge by 2% per wheel step (desktop affordance)."""
        if self.value is None or self.dragging or not steps:
            return
        nv = min(100.0, max(0.0, round(self.value) + steps * 2))
        if nv != self.value:
            self.value = nv
            self._draw()
            self._on_change(nv)

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
    """M3 icon button, 40dp. play = filled tonal; stop = filled primary
    (Google Home's active-cast affordance - not red); busy = an animated
    indeterminate spinner shown while the cast warms up / buffers."""

    SPIN_N = 12          # spinner frames (30 deg apart)
    SPIN_MS = 80         # ~1 rev/sec

    def __init__(self, parent, scale: float, on_click: Callable[[], None],
                 bg=CARD):
        dp = lambda v: round(v * scale)
        self._dp = dp
        self.D = dp(40)
        self._glyph_px = dp(24)
        super().__init__(parent, width=self.D, height=self.D, bg=bg,
                         highlightthickness=0, cursor="hand2")
        self._bg = bg
        self._ss = render.supersample(scale)
        self._on_click = on_click
        self.mode = "play"
        self.enabled = True
        self._hover = False
        self._spin_frame = 0
        self._spin_after: str | None = None
        self._item = self.create_image(0, 0, anchor="nw")
        self._photo = None
        self.bind("<ButtonRelease-1>", lambda e: self._click())
        self.bind("<Enter>", lambda e: self._set_hover(True))
        self.bind("<Leave>", lambda e: self._set_hover(False))
        self.bind("<Destroy>", lambda e: self._stop_spin())
        self._draw()

    def _set_hover(self, on: bool) -> None:
        if self.mode == "busy":
            return
        self._hover = on
        self._draw()

    def _click(self) -> None:
        if self.enabled:
            self._on_click()

    def configure_state(self, mode: str, enabled: bool) -> None:
        if mode == "busy":
            self.mode, self.enabled = "busy", False
            self.configure(cursor="")
            if self._spin_after is None:
                self._spin()
            return
        self._stop_spin()
        self.configure(cursor="hand2")
        self.mode, self.enabled = mode, enabled
        self._draw()

    def _stop_spin(self) -> None:
        if self._spin_after is not None:
            try:
                self.after_cancel(self._spin_after)
            except Exception:
                pass
            self._spin_after = None

    def _spin(self) -> None:
        if not self.winfo_exists():
            self._spin_after = None
            return
        self._draw_spinner(self._spin_frame)
        self._spin_frame = (self._spin_frame + 1) % self.SPIN_N
        self._spin_after = self.after(self.SPIN_MS, self._spin)

    def _draw_spinner(self, frame: int) -> None:
        D, dp = self.D, self._dp
        inset = dp(11)
        lw = max(dp(3), 2)
        start = frame * (360 / self.SPIN_N)
        key = ("spin", frame, D, self._bg, self._ss)

        def paint(d, k):
            d.ellipse([0, 0, D * k, D * k], fill=TONAL)
            box = [inset * k, inset * k, (D - inset) * k, (D - inset) * k]
            d.arc(box, 0, 360, fill=OUTLINE_VAR, width=round(lw * k))
            d.arc(box, start, start + 260, fill=ACCENT, width=round(lw * k))

        self._photo = render.photo(key, D, D, self._ss, self._bg, paint)
        self.itemconfigure(self._item, image=self._photo)

    def _draw(self) -> None:
        if not self.enabled:
            fill, glyph_color = DISABLED_FILL, DISABLED_GLYPH
        elif self.mode == "stop":
            fill, glyph_color = ACCENT, ON_ACCENT
        else:
            fill, glyph_color = (TONAL_HOVER if self._hover else TONAL), ON_TONAL
        D, px = self.D, self._glyph_px
        icon = "stop" if self.mode == "stop" else "play"
        key = ("iconbtn", icon, fill, glyph_color, D, self._bg, self._ss)

        def paint(d, k):
            d.ellipse([0, 0, D * k, D * k], fill=fill)
            render.glyph(d, D * k / 2, D * k / 2, icon, px * k, glyph_color)

        self._photo = render.photo(key, D, D, self._ss, self._bg, paint)
        self.itemconfigure(self._item, image=self._photo)


class Toggle(tk.Canvas):
    """M3 switch: 52x32dp track, 16dp off-handle / 24dp on-handle."""

    def __init__(self, parent, scale: float, on_change: Callable[[bool], None],
                 bg=BG):
        dp = lambda v: round(v * scale)
        self.W, self.H = dp(52), dp(32)
        self._dp = dp
        super().__init__(parent, width=self.W, height=self.H, bg=bg,
                         highlightthickness=0, cursor="hand2")
        self._bg = bg
        self._ss = render.supersample(scale)
        self._on_change = on_change
        self.on = False
        self._item = self.create_image(0, 0, anchor="nw")
        self._photo = None
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
        W, H, dp, on = self.W, self.H, self._dp, self.on
        key = ("switch", on, W, H, self._ss)

        def paint(d, k):
            if on:
                d.rounded_rectangle([0, 0, W * k, H * k], radius=H * k / 2,
                                    fill=ACCENT)
                cx, r = (W - dp(16)) * k, dp(12) * k
                d.ellipse([cx - r, H * k / 2 - r, cx + r, H * k / 2 + r],
                          fill=ON_ACCENT)
                render.glyph(d, cx, H * k / 2, "check", dp(16) * k, ACCENT)
            else:
                w = max(dp(2), 2) * k
                d.rounded_rectangle([w / 2, w / 2, W * k - w / 2, H * k - w / 2],
                                    radius=(H * k - w) / 2, outline=OUTLINE,
                                    width=round(w))
                cx, r = dp(16) * k, dp(8) * k
                d.ellipse([cx - r, H * k / 2 - r, cx + r, H * k / 2 + r],
                          fill=OUTLINE)

        self._photo = render.photo(key, W, H, self._ss, self._bg, paint)
        self.itemconfigure(self._item, image=self._photo)


class DeviceIcon(tk.Canvas):
    """40dp badge circle with a Material glyph; accent-filled while casting."""

    def __init__(self, parent, scale: float, kind: str, bg=CARD):
        dp = lambda v: round(v * scale)
        self.D = dp(40)
        self._glyph_px = dp(22)
        super().__init__(parent, width=self.D, height=self.D, bg=bg,
                         highlightthickness=0)
        self._bg = bg
        self._ss = render.supersample(scale)
        self._glyph = {"group": "speaker_group", "cast": "tv"}.get(kind, "speaker")
        self._item = self.create_image(0, 0, anchor="nw")
        self._photo = None
        self.active = False
        self._draw()

    def set_active(self, active: bool) -> None:
        if active != self.active:
            self.active = active
            self._draw()

    def _draw(self) -> None:
        D, px, icon = self.D, self._glyph_px, self._glyph
        fill = ACCENT if self.active else BADGE
        fg = ON_ACCENT if self.active else SUBTEXT
        key = ("badge", icon, self.active, D, self._bg, self._ss)

        def paint(d, k):
            d.ellipse([0, 0, D * k, D * k], fill=fill)
            render.glyph(d, D * k / 2, D * k / 2, icon, px * k, fg)

        self._photo = render.photo(key, D, D, self._ss, self._bg, paint)
        self.itemconfigure(self._item, image=self._photo)


class TextButton(tk.Canvas):
    """M3 text button: primary label, pill state layer on hover."""

    def __init__(self, parent, scale: float, text: str,
                 on_click: Callable[[], None], bg=BG):
        from . import fonts  # resolved family names
        dp = lambda v: round(v * scale)
        font = (fonts.MEDIUM, -dp(14))
        tw = tkfont.Font(family=fonts.MEDIUM, size=-dp(14)).measure(text)
        self.W, self.H = tw + 2 * dp(16), dp(36)
        super().__init__(parent, width=self.W, height=self.H, bg=bg,
                         highlightthickness=0, cursor="hand2")
        self._ss = render.supersample(scale)
        self._bg = bg
        self._on_click = on_click
        W, H = self.W, self.H
        key = ("txtbtn-pill", W, H, self._ss)

        def paint(d, k):
            d.rounded_rectangle([0, 0, W * k, H * k], radius=H * k / 2,
                                fill=EXIT_HOVER)

        self._pill = render.photo(key, W, H, self._ss, bg, paint)
        self._pill_item = self.create_image(0, 0, anchor="nw", state="hidden")
        self.itemconfigure(self._pill_item, image=self._pill)
        self.create_text(W / 2, H / 2, text=text, fill=ACCENT, font=font)
        self.bind("<Enter>", lambda e: self.itemconfigure(
            self._pill_item, state="normal"))
        self.bind("<Leave>", lambda e: self.itemconfigure(
            self._pill_item, state="hidden"))
        self.bind("<ButtonRelease-1>", lambda e: self._on_click())

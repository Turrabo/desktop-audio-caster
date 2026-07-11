"""PIL-rendered widget imagery. Tk Canvas has no antialiasing, so every
shape and glyph is drawn supersampled with Pillow, LANCZOS-downscaled, and
shown as a Tk PhotoImage.

Images composite onto the KNOWN solid color behind them (surface or card), so
edges come out clean without per-pixel window alpha. Static imagery is cached
under a full-identity key (kind + state + size + supersample); the cache is
cleared on DPI-scale rebuilds. Sliders render uncached (photo_live) and the
widget throttles redraws to integer-percent changes.
"""
from __future__ import annotations

from PIL import Image, ImageDraw, ImageFont, ImageTk

from .fonts import ICON_FONT_PATH, ICONS

_cache: dict[tuple, ImageTk.PhotoImage] = {}
_fonts: dict[int, ImageFont.FreeTypeFont] = {}


def supersample(scale: float) -> int:
    """Constant device-pixel quality budget: fewer passes as DPI rises."""
    if scale <= 1.25:
        return 4
    if scale <= 1.6:
        return 3
    return 2


def clear_cache() -> None:
    _cache.clear()


def _icon_font(px: int) -> ImageFont.FreeTypeFont:
    f = _fonts.get(px)
    if f is None:
        f = _fonts[px] = ImageFont.truetype(str(ICON_FONT_PATH), px)
    return f


def glyph(d: ImageDraw.ImageDraw, cx: float, cy: float, name: str, px: int,
          fill: str) -> None:
    """Icon glyph centered on (cx, cy); px already supersampled."""
    d.text((cx, cy), ICONS[name], font=_icon_font(px), fill=fill, anchor="mm")


def photo_live(w: int, h: int, ss: int, bg: str, draw_fn) -> ImageTk.PhotoImage:
    """Uncached supersampled image for dynamic content (sliders).
    draw_fn(d, k) draws onto an ImageDraw at k = ss coordinate multiplier."""
    img = Image.new("RGB", (w * ss, h * ss), bg)
    draw_fn(ImageDraw.Draw(img), ss)
    return ImageTk.PhotoImage(img.resize((w, h), Image.LANCZOS))


def photo(key: tuple, w: int, h: int, ss: int, bg: str, draw_fn) -> ImageTk.PhotoImage:
    """Cached variant; key must be full identity (state + w + h + ss)."""
    ph = _cache.get(key)
    if ph is None:
        ph = _cache[key] = photo_live(w, h, ss, bg, draw_fn)
    return ph

"""Generate assets/app.ico - the Windows app icon.

A Material-dark rounded tile with the primary-blue cast glyph, matching the
popover's palette (so the taskbar/Alt-Tab icon previews the UI). Each ICO size
is rendered with size-appropriate padding and rounding so the glyph still reads
at 16 px. Re-run after changing the palette or glyph:

    .venv\\Scripts\\python scripts\\make_icon.py
"""
from __future__ import annotations

from pathlib import Path

from PIL import Image, ImageDraw, ImageFont

ASSETS = Path(__file__).resolve().parents[1] / "assets"
GLYPH = chr(0xE308)                       # Material 'cast_connected'
FONT = ASSETS / "MaterialIconsRound-Regular.otf"

TILE_HI = (42, 44, 46)                    # gradient top   #2A2C2E
TILE_LO = (24, 25, 26)                    # gradient bottom #18191A
BORDER = (58, 60, 62)                     # edge hairline  #3A3C3E
PRIMARY = (168, 199, 250)                 # cast glyph     #A8C7FA
SS = 8                                     # supersample factor
SIZES = [16, 20, 24, 32, 40, 48, 64, 128, 256]


def render(size: int) -> Image.Image:
    s = size * SS
    img = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    d = ImageDraw.Draw(img)
    pad = round(s * (0.06 if size <= 24 else 0.09))
    rad = round(s * (0.16 if size <= 24 else 0.22))
    x0, y0, x1, y1 = pad, pad, s - pad, s - pad

    grad = Image.new("RGBA", (s, s), (0, 0, 0, 0))
    gd = ImageDraw.Draw(grad)
    for y in range(y0, y1):
        t = (y - y0) / max(1, y1 - y0)
        c = tuple(round(TILE_HI[i] + (TILE_LO[i] - TILE_HI[i]) * t) for i in range(3))
        gd.line([(x0, y), (x1, y)], fill=c + (255,))
    mask = Image.new("L", (s, s), 0)
    ImageDraw.Draw(mask).rounded_rectangle([x0, y0, x1, y1], radius=rad, fill=255)
    img.paste(grad, (0, 0), mask)
    d.rounded_rectangle([x0, y0, x1, y1], radius=rad, outline=BORDER + (255,),
                        width=max(SS, round(s * 0.008)))

    gpx = round((y1 - y0) * (0.82 if size <= 24 else 0.7))
    font = ImageFont.truetype(str(FONT), gpx)
    d.text((s / 2, s / 2 + s * 0.01), GLYPH, font=font, fill=PRIMARY + (255,),
           anchor="mm")
    return img.resize((size, size), Image.LANCZOS)


def main() -> None:
    imgs = [render(sz) for sz in SIZES]
    out = ASSETS / "app.ico"
    imgs[-1].save(out, sizes=[(i.width, i.height) for i in imgs],
                  append_images=imgs[:-1])
    print(f"wrote {out} ({', '.join(str(s) for s in SIZES)})")


if __name__ == "__main__":
    main()

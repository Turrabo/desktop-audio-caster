"""Private font loading (Windows GDI) for the bundled Google fonts.

Roboto Regular/Medium (UI text) and Material Icons Round (glyphs) ship in
assets/ (all Apache 2.0); AddFontResourceExW with FR_PRIVATE makes them usable
by this process only - no system install, no admin. Tk uses the GDI families;
the PIL render layer loads the icon OTF by file path (render.py), as does the
tray glyph (tray.py). Falls back to Segoe UI when Roboto is missing.
"""
from __future__ import annotations

import ctypes
import logging
import sys
from pathlib import Path

log = logging.getLogger(__name__)


def _assets_dir() -> Path:
    """assets/ next to the sources in dev, or unpacked beside the frozen exe
    (PyInstaller sets sys._MEIPASS to the bundle root in both build modes)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "assets"
    return Path(__file__).resolve().parents[2] / "assets"


ASSETS = _assets_dir()
FR_PRIVATE = 0x10

ICON_FONT_PATH = ASSETS / "MaterialIconsRound-Regular.otf"
APP_ICO = ASSETS / "app.ico"

# UI families, resolved by ensure_fonts(); Segoe fallbacks until then.
FONT = "Segoe UI"
MEDIUM = "Segoe UI Semibold"

# Material Icons codepoints (identical across Google icon families).
# chr() form keeps this file pure ASCII.
ICONS = {
    "speaker": chr(0xE32D),
    "speaker_group": chr(0xE32E),
    "cast": chr(0xE307),
    "cast_connected": chr(0xE308),
    "tv": chr(0xE333),
    "play": chr(0xE037),
    "stop": chr(0xE047),
    "check": chr(0xE5CA),
    "error": chr(0xE000),
}

_loaded = False


def _add_private(path: Path) -> bool:
    if not path.exists():
        log.warning("font missing at %s", path)
        return False
    return ctypes.windll.gdi32.AddFontResourceExW(str(path), FR_PRIVATE, 0) > 0


def ensure_fonts() -> None:
    """Load bundled fonts for this process; resolve the UI family names."""
    global _loaded, FONT, MEDIUM
    if _loaded:
        return
    _loaded = True
    if (_add_private(ASSETS / "Roboto-Regular.ttf")
            and _add_private(ASSETS / "Roboto-Medium.ttf")):
        FONT, MEDIUM = "Roboto", "Roboto Medium"
    else:
        log.warning("Roboto unavailable - using Segoe UI")
    if not _add_private(ICON_FONT_PATH):
        log.warning("icon font unavailable - glyphs will render as boxes")

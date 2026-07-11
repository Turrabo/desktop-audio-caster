"""Private font loading (Windows GDI) for the bundled Google icon font.

Material Icons Round (Apache 2.0, google/material-design-icons) ships in
assets/; AddFontResourceExW with FR_PRIVATE makes it usable by this process
only - no system install, no admin.
"""
from __future__ import annotations

import ctypes
import logging
from pathlib import Path

log = logging.getLogger(__name__)

ASSETS = Path(__file__).resolve().parents[2] / "assets"
FR_PRIVATE = 0x10

ICON_FONT = "Material Icons Round"

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
}

_loaded = False


def ensure_fonts() -> bool:
    """Load bundled fonts for this process. Returns True if usable."""
    global _loaded
    if _loaded:
        return True
    path = ASSETS / "MaterialIconsRound-Regular.otf"
    if not path.exists():
        log.warning("icon font missing at %s - falling back to text glyphs", path)
        return False
    added = ctypes.windll.gdi32.AddFontResourceExW(str(path), FR_PRIVATE, 0)
    _loaded = added > 0
    if not _loaded:
        log.warning("AddFontResourceExW failed for %s", path)
    return _loaded

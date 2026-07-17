"""Shared asset-path resolution for both dev and frozen (PyInstaller) runs.

Kept package-level (not under ui/) so non-UI modules - e.g. the Opus DLL
loader in mirror.py - can resolve assets/ without importing the UI layer.
"""
from __future__ import annotations

import sys
from pathlib import Path


def assets_dir() -> Path:
    """assets/ next to the sources in dev, or unpacked beside the frozen exe
    (PyInstaller sets sys._MEIPASS to the bundle root in both build modes)."""
    if getattr(sys, "frozen", False):
        return Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent)) / "assets"
    return Path(__file__).resolve().parents[1] / "assets"


ASSETS = assets_dir()

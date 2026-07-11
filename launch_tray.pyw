"""Startup shim / packaging entry point.

In dev, registry-launched processes start in System32 with no sys.path to the
repo - fix both. When frozen (PyInstaller), the bundle handles imports and cwd,
so skip the shim.
"""
import os
import sys
from pathlib import Path

if not getattr(sys, "frozen", False):
    REPO = Path(__file__).resolve().parent
    os.chdir(REPO)
    sys.path.insert(0, str(REPO))

from streamer.tray import main  # noqa: E402

sys.exit(main())

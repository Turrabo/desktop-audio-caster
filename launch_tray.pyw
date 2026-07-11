"""Startup shim: registry-launched processes start in System32 with no
sys.path to the repo - fix both, then run the tray app."""
import os
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parent
os.chdir(REPO)
sys.path.insert(0, str(REPO))

from streamer.tray import main  # noqa: E402

sys.exit(main())

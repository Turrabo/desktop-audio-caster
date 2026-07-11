"""Start-with-Windows via HKCU Run key. Per-user, no admin, silent (pythonw).

Self-repairing: enable() always writes the currently-correct command, and
repair_if_stale() (called at app start) rewrites an entry whose path drifted
(moved repo, rebuilt venv) so login-start never silently breaks.
"""
from __future__ import annotations

import logging
import sys
import winreg
from pathlib import Path

log = logging.getLogger(__name__)

RUN_KEY = r"Software\Microsoft\Windows\CurrentVersion\Run"
VALUE_NAME = "DesktopAudioStreamer"

REPO = Path(__file__).resolve().parents[1]


def _command() -> str:
    pythonw = Path(sys.executable).with_name("pythonw.exe")
    shim = REPO / "launch_tray.pyw"
    return f'"{pythonw}" "{shim}"'


def _read() -> str | None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY) as key:
            value, _ = winreg.QueryValueEx(key, VALUE_NAME)
            return value
    except OSError:
        return None


def is_enabled() -> bool:
    return _read() is not None


def enable() -> None:
    with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                        winreg.KEY_SET_VALUE) as key:
        winreg.SetValueEx(key, VALUE_NAME, 0, winreg.REG_SZ, _command())
    log.info("startup enabled: %s", _command())


def disable() -> None:
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, RUN_KEY, 0,
                            winreg.KEY_SET_VALUE) as key:
            winreg.DeleteValue(key, VALUE_NAME)
        log.info("startup disabled")
    except OSError:
        pass


def repair_if_stale() -> None:
    current = _read()
    if current is not None and current != _command():
        log.info("startup entry stale (%s) - rewriting", current)
        enable()

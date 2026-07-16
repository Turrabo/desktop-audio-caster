"""Best-effort Windows Firewall self-registration (frozen builds only).

Why: the speaker fetches our audio stream over inbound HTTP, and Windows
scopes the first-run firewall prompt to whichever network profile is active
at that moment. When the network later flips category (Private <-> Public --
router changes, Windows re-classification), the allow rule silently stops
applying and every cast wedges: receiver launches, stream never fetched.

So on first run of a given exe path, register an all-profiles inbound allow
rule for the running image: silently if the process is already elevated,
otherwise one elevated netsh attempt (a single UAC prompt on stock systems).
Either way the outcome is recorded in config and we never ask again for that
image path. Failure is non-fatal - the bounded watchdog error in caster.py
tells the user what to fix by hand.
"""
from __future__ import annotations

import ctypes
import logging
import subprocess
import sys

log = logging.getLogger(__name__)

RULE_NAME = "Desktop Audio Streamer"
CFG_KEY = "firewall_registered_image"


def _image_path() -> str:
    """The actual PE image of this process (NOT sys.executable, which under a
    venv launcher reports the shim; the firewall matches the real image)."""
    buf = ctypes.create_unicode_buffer(2048)
    ctypes.windll.kernel32.GetModuleFileNameW(None, buf, 2048)
    return buf.value


def _add_args(image: str) -> str:
    # localsubnet scope: casting only ever serves speakers on the local
    # subnet, and this keeps the unauthenticated stream port closed to
    # strangers when the rule applies on genuinely-public networks.
    return ('advfirewall firewall add rule name="{}" dir=in action=allow '
            'profile=any enable=yes remoteip=localsubnet program="{}"'
            .format(RULE_NAME, image))


def _delete_args() -> str:
    return 'advfirewall firewall delete rule name="{}"'.format(RULE_NAME)


def _run_netsh(args: str) -> int:
    return subprocess.call("netsh " + args,
                           creationflags=subprocess.CREATE_NO_WINDOW)


def ensure_allowed(cfg: dict, save_cfg) -> None:
    """Run on a background thread at app start; frozen builds only."""
    if not getattr(sys, "frozen", False):
        return
    try:
        image = _image_path()
        marker = str(cfg.get(CFG_KEY) or "")
        if marker.endswith(image):
            return
        try:
            _run_netsh(_delete_args())          # dedup: stale/moved-exe rules
            rc = _run_netsh(_add_args(image))
        except OSError as e:
            log.warning("netsh unavailable: %s", e)
            return
        if rc == 0:
            outcome = "registered"
            log.info("firewall rule registered (all profiles, localsubnet) "
                     "for %s", image)
        else:
            # Not elevated: one UAC-prompted attempt, then never nag again
            # (the bounded watchdog error is the fallback surface).
            log.info("netsh add rc=%d; attempting elevated registration", rc)
            shell32 = ctypes.windll.shell32
            shell32.ShellExecuteW.restype = ctypes.c_void_p
            r = shell32.ShellExecuteW(None, "runas", "netsh",
                                      _add_args(image), None, 0)
            launched = r is not None and int(r) > 32
            outcome = "asked" if launched else "declined"
            log.info("elevated firewall registration %s",
                     "launched" if launched else "declined/failed")
        cfg[CFG_KEY] = f"{outcome}:{image}"
        save_cfg(cfg)
    except Exception as e:                      # never block app startup
        log.warning("firewall self-registration failed: %s", e)

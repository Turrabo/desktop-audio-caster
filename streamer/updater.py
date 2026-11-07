"""Self-update from the GitHub releases page. Stdlib only, frozen exe only.

Flow (the running exe never touches its own file - PyInstaller's onefile
loader re-opens the exe BY NAME for every lazy import, so renaming it under a
live process breaks that process):

  1. check()      GitHub's releases/latest API -> an Update, or None.
  2. download()   the release exe to <exe dir>/DesktopAudioStreamer-update.exe,
                  verified against the API's size and sha256 digest.
  3. hand_over()  launch that file with --install-update; the caller then
                  exits normally.
  4. install()    (runs in the downloaded exe) waits for the old exe to be
                  released, copies itself over it atomically, launches it.
                  On any failure it relaunches whatever is in place, so the
                  app is never left missing.
  5. cleanup()    (runs in the new app at start) deletes the leftover file.

Trust: the exe is unsigned, so this trusts HTTPS to github.com and the
repo's releases. The digest guards against a truncated or corrupt download,
not against someone with write access to the releases.
"""
from __future__ import annotations

import hashlib
import json
import logging
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.request
from dataclasses import dataclass
from pathlib import Path

from . import __version__

log = logging.getLogger(__name__)

API_URL = "https://api.github.com/repos/Turrabo/desktop-audio-caster/releases/latest"
ASSET_NAME = "DesktopAudioStreamer.exe"
UPDATE_NAME = "DesktopAudioStreamer-update.exe"
INSTALL_FLAG = "--install-update"
HTTP_TIMEOUT = 15
FIRST_CHECK_DELAY = 60.0           # s after start: stay out of the startup path
CHECK_INTERVAL = 24 * 3600.0
RELEASE_WAIT = 20.0                # s for the old exe to exit and free its file

# A PyInstaller onefile child launching another onefile exe must ask it to
# set up its own environment, or it inherits ours (and our temp dir).
_SPAWN_ENV = {**os.environ, "PYINSTALLER_RESET_ENVIRONMENT": "1"}
_DETACHED = 0x00000008 | 0x00000200   # DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP


@dataclass(frozen=True)
class Update:
    version: str
    url: str
    size: int
    sha256: str | None


def parse_version(text: str) -> tuple[int, ...] | None:
    """'v1.3.1' / '1.3.1' -> (1, 3, 1); anything else (pre-release suffix,
    junk) -> None, which never compares as newer."""
    parts = text.strip().lstrip("vV").split(".")
    if not parts or not all(p.isdigit() for p in parts):
        return None
    return tuple(int(p) for p in parts)


def pick_update(release: dict, current: str = __version__) -> Update | None:
    """The release's exe asset if the release is newer than `current`."""
    latest, mine = parse_version(release.get("tag_name", "")), parse_version(current)
    if latest is None or mine is None or latest <= mine:
        return None
    for asset in release.get("assets", []):
        if asset.get("name") == ASSET_NAME:
            digest = asset.get("digest") or ""
            return Update(version=".".join(map(str, latest)),
                          url=asset["browser_download_url"],
                          size=int(asset["size"]),
                          sha256=digest[7:] if digest.startswith("sha256:") else None)
    return None


def _get(url: str):
    req = urllib.request.Request(url, headers={
        "User-Agent": f"DesktopAudioStreamer/{__version__}",
        "Accept": "application/vnd.github+json"})
    return urllib.request.urlopen(req, timeout=HTTP_TIMEOUT)


def check(opener=_get) -> Update | None:
    with opener(API_URL) as resp:
        return pick_update(json.load(resp))


def download(update: Update, dest: Path, opener=_get) -> Path:
    """Stream the exe to dest (via a .part file); raise unless it matches."""
    part = dest.with_name(dest.name + ".part")
    digest, size = hashlib.sha256(), 0
    try:
        with opener(update.url) as resp, open(part, "wb") as out:
            while chunk := resp.read(256 * 1024):
                out.write(chunk)
                digest.update(chunk)
                size += len(chunk)
        if size != update.size:
            raise ValueError(f"download size {size} != expected {update.size}")
        if update.sha256 and digest.hexdigest() != update.sha256.lower():
            raise ValueError("download sha256 mismatch")
        with open(part, "rb") as f:
            if f.read(2) != b"MZ":
                raise ValueError("download is not a Windows executable")
        os.replace(part, dest)
    finally:
        part.unlink(missing_ok=True)
    return dest


def hand_over(downloaded: Path, target: Path) -> None:
    """Launch the downloaded exe in install mode. The caller must then exit."""
    subprocess.Popen([str(downloaded), INSTALL_FLAG, str(target)],
                     env=_SPAWN_ENV, creationflags=_DETACHED, close_fds=True)


def _launch(exe: Path) -> None:
    subprocess.Popen([str(exe)], env=_SPAWN_ENV, creationflags=_DETACHED,
                     close_fds=True)


def install(source: Path, target: Path, wait: float = RELEASE_WAIT,
            launch=_launch) -> bool:
    """Copy source over target once the old exe lets go of it, then launch
    target. Copy-then-replace keeps target whole at every instant. Returns
    True if the new version went in; target is launched either way."""
    tmp = target.with_name(target.name + ".tmp")
    deadline = time.monotonic() + wait
    ok = False
    try:
        shutil.copyfile(source, tmp)
        while True:
            try:
                os.replace(tmp, target)    # fails while the old exe still runs
                ok = True
                break
            except PermissionError:
                if time.monotonic() > deadline:
                    raise
                time.sleep(0.25)
        log.info("update installed: %s", target)
    except Exception as exc:
        log.error("update install failed (%s); relaunching current version", exc)
    finally:
        tmp.unlink(missing_ok=True)
    launch(target)
    return ok


def maybe_run_installer(argv: list[str]) -> bool:
    """Entry hook, before any app start-up: True if this process was the
    installer step (and has finished), so the caller must exit at once."""
    if len(argv) < 3 or argv[1] != INSTALL_FLAG:
        return False
    from . import config as cfg_mod
    cfg_mod.setup_logging()
    install(Path(sys.executable), Path(argv[2]))
    return True


def cleanup(exe_dir: Path, tries: int = 40, gap: float = 0.5) -> None:
    """Delete the installer's leftover copy. It may still be exiting when the
    new app starts, so retry in the background for a while."""
    leftovers = [exe_dir / UPDATE_NAME, exe_dir / (UPDATE_NAME + ".part")]

    def run():
        for _ in range(tries):
            pending = [p for p in leftovers if p.exists()]
            for p in pending:
                try:
                    p.unlink()
                except OSError:
                    pass
            if not any(p.exists() for p in leftovers):
                return
            time.sleep(gap)
    threading.Thread(target=run, name="update-cleanup", daemon=True).start()


def frozen_exe() -> Path | None:
    """The running exe when packaged; None from source (no self-update)."""
    return Path(sys.executable) if getattr(sys, "frozen", False) else None


class UpdateChecker:
    """Background daily check. on_found(update) fires once per new version."""

    def __init__(self, on_found) -> None:
        self._on_found = on_found
        self._stop = threading.Event()
        self._announced: str | None = None
        self.available: Update | None = None

    def start(self) -> None:
        threading.Thread(target=self._run, name="update-check", daemon=True).start()

    def stop(self) -> None:
        self._stop.set()

    def _run(self) -> None:
        delay = FIRST_CHECK_DELAY
        while not self._stop.wait(delay):
            delay = CHECK_INTERVAL
            try:
                found = check()
            except Exception as exc:
                log.info("update check failed: %s", exc)
                continue
            self.available = found
            if found is not None and found.version != self._announced:
                self._announced = found.version
                log.info("update available: %s -> %s", __version__, found.version)
                self._on_found(found)

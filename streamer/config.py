"""Config + logging. Config lives in %APPDATA%/desktop-audio-streamer/config.json."""
import json
import logging
import logging.handlers
import os
import threading
from pathlib import Path

APP_NAME = "desktop-audio-streamer"

DEFAULTS = {
    "last_device": None,          # friendly name of last cast target
    "max_volume": 1.0,            # safety.py cap; 1.0 = uncapped (user lifted 2026-07-11)
    "allow_group_volume": True,   # group volume rescales member volumes
    "office_names": [],           # devices whose volume must never be changed (none)
    "port": 8765,
    "stream_type": "LIVE",        # LIVE | BUFFERED (trial decides)
    "capture_device": None,       # None = default output; else substring of device name
    "mute_local_while_casting": True,   # legacy; migrated to output_mode below
    "firewall_registered_image": None,  # exe path we've registered/asked for
    # Cast path: "auto" uses mirroring when eligible (48 kHz stereo + opus.dll)
    # and falls back to HTTP otherwise; "mirror" forces it (still falls back if
    # ineligible); "http" pins the original Default-Media-Receiver path.
    "cast_mode": "auto",
    "mirror_target_delay_ms": 400,
    # Where audio is audible while casting:
    #   speakers - PC muted (+ pinned 100%), cast at full strength (default)
    #   this_pc  - PC audible, cast fed silence (speakers quiet)
    #   both     - both audible (cast strength follows PC volume on this driver)
    #   auto     - the PC's own mute switches: muted -> speakers, unmuted -> PC
    "output_mode": "speakers",
}


def config_dir() -> Path:
    d = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def load() -> dict:
    cfg = dict(DEFAULTS)
    path = config_dir() / "config.json"
    if path.exists():
        try:
            on_disk = json.loads(path.read_text(encoding="utf-8"))
            # Migrate the legacy mute flag: derive output_mode BEFORE the new
            # default would otherwise override an explicit mute_local=false.
            if "output_mode" not in on_disk and "mute_local_while_casting" in on_disk:
                on_disk["output_mode"] = (
                    "speakers" if on_disk["mute_local_while_casting"] else "both")
            cfg.update(on_disk)
        except (json.JSONDecodeError, OSError) as e:
            logging.getLogger(__name__).warning("config load failed, using defaults: %s", e)
    return cfg


# Keys the app itself mutates at runtime. save() persists ONLY these -
# policy keys (max_volume, office_names, ...) edited on disk must never be
# clobbered by a running instance's stale in-memory copy.
APP_OWNED_KEYS = ("last_device", "stream_type", "firewall_registered_image")


_save_lock = threading.Lock()


def save(cfg: dict) -> None:
    # Locked + atomic: the ops worker (last_device) and the firewall
    # registration thread can both save during the first seconds of a run.
    with _save_lock:
        path = config_dir() / "config.json"
        on_disk = {}
        if path.exists():
            try:
                on_disk = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        merged = dict(DEFAULTS)
        merged.update(on_disk)
        merged.update({k: cfg[k] for k in APP_OWNED_KEYS if k in cfg})
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(merged, indent=2), encoding="utf-8")
        os.replace(tmp, path)


def set_user_value(key: str, value) -> None:
    """Persist a single user-policy key edited from the UI (e.g. output_mode,
    mirror_target_delay_ms). Read-modify-write under the same lock as save() so
    the two never clobber each other; preserves every other key, and is NOT
    filtered by APP_OWNED_KEYS since it is an explicit user edit."""
    with _save_lock:
        path = config_dir() / "config.json"
        on_disk = {}
        if path.exists():
            try:
                on_disk = json.loads(path.read_text(encoding="utf-8"))
            except (json.JSONDecodeError, OSError):
                pass
        merged = dict(DEFAULTS)
        merged.update(on_disk)
        merged[key] = value
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(merged, indent=2), encoding="utf-8")
        os.replace(tmp, path)


def setup_logging(verbose: bool = False) -> None:
    root = logging.getLogger()
    root.setLevel(logging.DEBUG)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)s: %(message)s")

    fh = logging.handlers.RotatingFileHandler(
        config_dir() / "streamer.log", maxBytes=2_000_000, backupCount=3, encoding="utf-8")
    fh.setFormatter(fmt)
    fh.setLevel(logging.DEBUG)
    root.addHandler(fh)

    ch = logging.StreamHandler()
    ch.setFormatter(fmt)
    ch.setLevel(logging.DEBUG if verbose else logging.INFO)
    root.addHandler(ch)

    # zeroconf/pychromecast are chatty at DEBUG
    logging.getLogger("zeroconf").setLevel(logging.INFO)
    logging.getLogger("pychromecast.socket_client").setLevel(logging.INFO)

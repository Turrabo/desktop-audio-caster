"""Config + logging. Config lives in %APPDATA%/desktop-audio-streamer/config.json."""
import json
import logging
import logging.handlers
import os
import threading
from pathlib import Path

APP_NAME = "desktop-audio-streamer"

# Bumped when a DEFAULTS value changes meaning and existing configs need
# rewriting. save() materialises the WHOLE of DEFAULTS to disk (deliberately -
# the file is meant to be readable and hand-editable), which means a user who
# never chose a setting still ends up with that setting written out. Changing a
# default therefore does NOT reach existing installs; only a migration keyed on
# this version does. See _migrated().
CONFIG_VERSION = 2

DEFAULTS = {
    "config_version": CONFIG_VERSION,
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
    #   speakers        - PC muted at cast start, cast at full strength (default).
    #                     The mute is a starting position, not a lock: unmuting
    #                     the PC mid-cast is treated as intent and moves the live
    #                     cast to "both" until you mute again.
    #   speakers_locked - as above but the mute is HELD; un-muting is undone
    #                     within milliseconds. Opt-in ("keep the room silent").
    #   this_pc         - PC audible, cast fed silence (speakers quiet)
    #   both            - both audible (cast strength follows PC volume on this driver)
    #   auto            - the PC's own mute switches: muted -> speakers, unmuted -> PC
    "output_mode": "speakers",
}

OUTPUT_MODES = ("speakers", "speakers_locked", "this_pc", "both", "auto")


def config_dir() -> Path:
    d = Path(os.environ.get("APPDATA", Path.home())) / APP_NAME
    d.mkdir(parents=True, exist_ok=True)
    return d


def _migrated(on_disk: dict) -> dict:
    """Bring a raw on-disk dict up to CONFIG_VERSION. Pure; returns a new dict.

    MUST be applied everywhere the file is read - load(), save() and
    set_user_value() alike. Applying it only in load() was a real bug: save()
    re-read the RAW file, rebuilt from DEFAULTS, and wrote the un-migrated
    default straight back, so a user who set mute_local_while_casting=false got
    silently re-muted on their first cast and the migration never ran again
    (output_mode was now present on disk).
    """
    out = dict(on_disk)
    # v1: derive output_mode from the legacy mute flag before the DEFAULTS value
    # can override an explicit mute_local=false.
    if "output_mode" not in out and "mute_local_while_casting" in out:
        out["output_mode"] = "speakers" if out["mute_local_while_casting"] else "both"
    # v2: "speakers" stopped meaning "hold the mute down" and became "mute at
    # cast start, then follow the user". Anyone who genuinely wanted the old
    # enforcing behaviour has to opt in, so there is nothing to rewrite here -
    # the semantic change is the migration. Recorded so a future version can
    # tell a v2-era "speakers" from a v3 one.
    out["config_version"] = CONFIG_VERSION
    return out


def _read_raw() -> dict:
    path = config_dir() / "config.json"
    if not path.exists():
        return {}
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        logging.getLogger(__name__).warning("config read failed, using defaults: %s", e)
        return {}


def load() -> dict:
    cfg = dict(DEFAULTS)
    cfg.update(_migrated(_read_raw()))
    return cfg


# Keys the app itself mutates at runtime. save() persists ONLY these -
# policy keys (max_volume, office_names, ...) edited on disk must never be
# clobbered by a running instance's stale in-memory copy.
APP_OWNED_KEYS = ("last_device", "stream_type", "firewall_registered_image")


_save_lock = threading.Lock()


def _write_locked(overrides: dict) -> None:
    """Read-modify-write the config under _save_lock. Callers hold no lock."""
    with _save_lock:
        path = config_dir() / "config.json"
        merged = dict(DEFAULTS)
        merged.update(_migrated(_read_raw()))
        merged.update(overrides)
        tmp = path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(merged, indent=2), encoding="utf-8")
        os.replace(tmp, path)


def save(cfg: dict) -> None:
    # Locked + atomic: the ops worker (last_device) and the firewall
    # registration thread can both save during the first seconds of a run.
    _write_locked({k: cfg[k] for k in APP_OWNED_KEYS if k in cfg})


def set_user_value(key: str, value) -> None:
    """Persist a single user-policy key edited from the UI (e.g. output_mode,
    mirror_target_delay_ms). Read-modify-write under the same lock as save() so
    the two never clobber each other; preserves every other key, and is NOT
    filtered by APP_OWNED_KEYS since it is an explicit user edit."""
    _write_locked({key: value})


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

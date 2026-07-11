"""Config + logging. Config lives in %APPDATA%/desktop-audio-streamer/config.json."""
import json
import logging
import logging.handlers
import os
from pathlib import Path

APP_NAME = "desktop-audio-streamer"

DEFAULTS = {
    "last_device": None,          # friendly name of last cast target
    "max_volume": 0.03,           # SAFETY: hard cap, see safety.py
    "allow_group_volume": False,  # SAFETY: group volume rescales member volumes; off by default
    "office_names": ["office"],   # SAFETY: devices whose volume must never be changed
    "port": 8765,
    "stream_type": "LIVE",        # LIVE | BUFFERED (trial decides)
    "capture_device": None,       # None = default output; else substring of device name
    "mute_local_while_casting": True,
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
            cfg.update(json.loads(path.read_text(encoding="utf-8")))
        except (json.JSONDecodeError, OSError) as e:
            logging.getLogger(__name__).warning("config load failed, using defaults: %s", e)
    return cfg


def save(cfg: dict) -> None:
    path = config_dir() / "config.json"
    path.write_text(json.dumps(cfg, indent=2), encoding="utf-8")


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

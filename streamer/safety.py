"""Volume safety layer - the ONLY module allowed to touch speaker volume.

Rules (standing user requirements, 2026-07-10):
  1. No volume may ever be set above ``max_volume`` (config, default 0.03).
  2. Devices named in ``office_names`` must NEVER have their volume changed,
     directly or indirectly.
  3. Group volume ops indirectly rescale every member's volume, so they are
     REFUSED unless ``allow_group_volume`` is true AND membership was resolved
     AND no protected device is a member. Fail closed: unresolvable membership
     means refusal.

An automated test (tests/test_no_rogue_volume.py) asserts that volume-setting
calls appear nowhere outside this module.
"""
from __future__ import annotations

import logging
import threading

import pychromecast
from pychromecast.controllers.multizone import MultizoneController

log = logging.getLogger(__name__)


class SafetyError(Exception):
    """A volume operation violated the safety rules."""


def _is_protected(name: str | None, office_names: list[str]) -> bool:
    return name is not None and name.strip().lower() in [n.lower() for n in office_names]


def resolve_group_members(cast: pychromecast.Chromecast, timeout: float = 5.0) -> list[str] | None:
    """Return member UUID strings for a group, or None if unresolvable."""
    if cast.cast_type != "group":
        return []
    mz = MultizoneController(cast.uuid)
    event = threading.Event()

    class Listener:
        def multizone_member_added(self, uuid):
            pass

        def multizone_member_removed(self, uuid):
            pass

        def multizone_status_received(self):
            event.set()

    mz.register_listener(Listener())
    cast.register_handler(mz)
    mz.update_members()
    if not event.wait(timeout):
        return None
    return [str(u) for u in mz.members]


def set_volume(
    cast: pychromecast.Chromecast,
    level: float,
    cfg: dict,
    known_devices: dict[str, str] | None = None,
) -> None:
    """The single permitted path to change a speaker's volume.

    known_devices: uuid-string -> friendly-name map from discovery, used to
    check group members against the protected list.
    """
    max_volume = float(cfg["max_volume"])
    office_names = list(cfg["office_names"])

    if level < 0:
        raise SafetyError(f"negative volume {level!r}")
    if level > max_volume:
        raise SafetyError(
            f"volume {level:.3f} exceeds hard cap {max_volume:.3f} - refused")
    if _is_protected(cast.name, office_names):
        raise SafetyError(
            f"device {cast.name!r} is volume-protected - refused")

    if cast.cast_type == "group":
        if not cfg.get("allow_group_volume", False):
            raise SafetyError(
                "group volume changes are disabled (allow_group_volume=false) - "
                "group volume rescales every member's own volume")
        members = resolve_group_members(cast)
        if members is None:
            raise SafetyError(
                f"could not resolve members of group {cast.name!r} - refusing (fail closed)")
        if known_devices is None:
            raise SafetyError("no device map supplied for member check - refusing (fail closed)")
        for uuid in members:
            member_name = known_devices.get(uuid)
            if member_name is None:
                raise SafetyError(
                    f"group {cast.name!r} has unknown member {uuid} - refusing (fail closed)")
            if _is_protected(member_name, office_names):
                raise SafetyError(
                    f"group {cast.name!r} contains protected device {member_name!r} - refused")

    log.info("safety: setting volume of %r to %.3f", cast.name, level)
    cast.set_volume(level)


class SafeCast:
    """Proxy around pychromecast.Chromecast that hides raw volume methods.

    Everything outside safety.py gets a SafeCast, never a bare Chromecast, so
    the volume methods are not reachable by accident. Attribute allowlist, not
    denylist: new pychromecast surface stays blocked until reviewed.
    """

    _ALLOWED = frozenset({
        "name", "uuid", "model_name", "cast_type", "status", "media_controller",
        "is_idle", "app_id", "app_display_name", "socket_client", "cast_info",
        "wait", "disconnect", "register_handler", "register_status_listener",
        "quit_app", "start_app",
    })

    def __init__(self, cast: pychromecast.Chromecast):
        object.__setattr__(self, "_cast", cast)

    def __getattr__(self, item):
        if item not in self._ALLOWED:
            raise AttributeError(
                f"{item!r} is not exposed by SafeCast (volume ops go through safety.set_volume)")
        return getattr(object.__getattribute__(self, "_cast"), item)

    def __setattr__(self, key, value):
        raise AttributeError("SafeCast is read-only")

    def unwrap_for_safety_module_only(self) -> pychromecast.Chromecast:
        return object.__getattribute__(self, "_cast")

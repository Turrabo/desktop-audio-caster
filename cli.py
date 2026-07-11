"""CLI for the desktop audio streamer.

  python cli.py devices
  python cli.py start "Living Room" [--stream-type LIVE|BUFFERED] [--no-mute]
  python cli.py status          (while a start is running, status is printed in-loop)

`start` runs in the foreground; Ctrl+C stops casting, restores local audio,
and quits the receiver app on the speaker.
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from streamer import config as cfg_mod
from streamer.capture import LoopbackCapture
from streamer.caster import CastSession, Discovery
from streamer.localmute import LocalMute, recover_from_crash
from streamer.pacer import Pacer
from streamer.server import StreamServer

log = logging.getLogger("cli")


def cmd_devices(_args) -> int:
    disc = Discovery()
    print("discovering (8 s)...")
    disc.wait_for_devices(8)
    for d in sorted(disc.list_devices(), key=lambda d: d["name"]):
        print(f"  {d['name']:<20} {d['type']:<6} {d['model']:<20} {d['host']}:{d['port']}")
    disc.stop()
    return 0


def cmd_start(args) -> int:
    cfg = cfg_mod.load()
    recover_from_crash()

    name = args.device or cfg["last_device"]
    if not name:
        print("no device given and no last device remembered", file=sys.stderr)
        return 2
    stream_type = args.stream_type or cfg["stream_type"]

    print(f"discovering {name!r}...")
    disc = Discovery()
    disc.wait_for_devices(6)

    capture = LoopbackCapture(on_data=lambda d: pacer.feed(d),
                              device_hint=cfg["capture_device"])
    server = StreamServer(capture.format, cfg["port"])
    pacer = Pacer(capture.format, sink=server.feed)

    server.start()
    pacer.start()
    capture.start()

    mute = LocalMute()
    session = None
    try:
        safe_cast = disc.connect(name)
        print(f"connected to {safe_cast.name!r} ({safe_cast.model_name}, "
              f"{safe_cast.cast_type}); volume={safe_cast.status.volume_level:.2f}")

        if cfg["mute_local_while_casting"] and not args.no_mute:
            mute.engage()

        session = CastSession(
            disc, safe_cast, cfg["port"], stream_type, capture,
            on_event=lambda m: print(f"[event] {m}"),
            sent_seconds_fn=lambda: server.latest_client_bytes / capture.format.bytes_per_second)
        session.start()

        cfg["last_device"] = safe_cast.name
        cfg["stream_type"] = stream_type
        cfg_mod.save(cfg)

        print("casting - Ctrl+C to stop")
        t0 = time.time()
        while args.duration is None or time.time() - t0 < args.duration:
            time.sleep(10)
            s = session.status()
            lag = session.lag_seconds()
            lag_str = f"{lag:5.1f}s" if lag is not None else "  n/a"
            print(f"[{time.time()-t0:6.0f}s] state={s['player_state']} "
                  f"lag={lag_str} trims={session.trim_count} recasts={s['recasts']} "
                  f"clients={server.client_count()} dropped={pacer.dropped_bytes}")
    except KeyboardInterrupt:
        print("\nstopping...")
        return 0
    finally:
        if session is not None:
            session.stop()
        mute.release()
        capture.stop()
        pacer.stop()
        server.stop()
        disc.stop()


def main() -> int:
    parser = argparse.ArgumentParser(prog="desktop-audio-streamer")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("devices", help="list Cast devices/groups on the LAN")

    p_start = sub.add_parser("start", help="start casting to a device or group")
    p_start.add_argument("device", nargs="?", help="friendly name (default: last used)")
    p_start.add_argument("--stream-type", choices=["LIVE", "BUFFERED"])
    p_start.add_argument("--no-mute", action="store_true",
                         help="do not mute local output while casting")
    p_start.add_argument("--duration", type=int, default=None,
                         help="stop automatically after N seconds (testing)")

    args = parser.parse_args()
    cfg_mod.setup_logging(args.verbose)

    if args.cmd == "devices":
        return cmd_devices(args)
    if args.cmd == "start":
        return cmd_start(args)
    return 1


if __name__ == "__main__":
    sys.exit(main())

"""CLI for the desktop audio streamer (same engine as the tray app).

  python cli.py devices
  python cli.py start "Living Room" [--mode auto|mirror|http]
                                    [--stream-type LIVE|BUFFERED] [--no-mute]
                                    [--duration N]

The status line prints mode=<resolved path>: "mirror" (sub-second Cast
mirroring) or "http" (the Default Media Receiver). --mode mirror still falls
back to http when the capture format or opus.dll make mirroring ineligible.

`start` runs in the foreground; Ctrl+C stops casting, restores local audio,
and quits the receiver app on the speaker.
"""
from __future__ import annotations

import argparse
import sys
import time

from streamer import config as cfg_mod
from streamer.appctl import AppController


def cmd_devices(_args) -> int:
    ctl = AppController()
    print("discovering (8 s)...")
    ctl.discovery.wait_for_devices(8)
    devs = ctl.devices()
    for section in ("groups", "speakers"):
        print(f"{section.upper()}:")
        for d in devs[section]:
            print(f"  {d['name']:<20} {d['model']:<20} {d['host']}:{d['port']}")
    ctl.discovery.stop()
    return 0


def cmd_start(args) -> int:
    ctl = AppController()
    if args.stream_type:
        ctl.cfg["stream_type"] = args.stream_type
    if args.mode:
        # per-run only: cast_mode is a disk-edited policy key, not in
        # APP_OWNED_KEYS, so this override is never persisted back to config
        ctl.cfg["cast_mode"] = args.mode
    if args.output_mode:
        ctl.cfg["output_mode"] = args.output_mode
    elif args.no_mute:
        ctl.cfg["output_mode"] = "both"   # legacy alias: don't mute the PC

    name = args.device or ctl.cfg["last_device"]
    if not name:
        print("no device given and no last device remembered", file=sys.stderr)
        return 2

    ctl.add_listener(lambda kind, *a: print(f"[{kind}] {' '.join(str(x) for x in a)}")
                     if kind == "state" else None)

    print(f"discovering {name!r}...")
    ctl.discovery.wait_for_devices(6)
    ctl.start_cast(name)

    t0 = time.time()
    try:
        while args.duration is None or time.time() - t0 < args.duration:
            time.sleep(10)
            session = ctl.session
            if session is None:
                continue
            lag = session.lag_seconds()
            lag_str = f"{lag:5.1f}s" if lag is not None else "  n/a"
            print(f"[{time.time()-t0:6.0f}s] mode={ctl.cast_mode_active} "
                  f"state={ctl.state} lag={lag_str} "
                  f"trims={session.trim_count} recasts={session.recast_count}")
        return 0
    except KeyboardInterrupt:
        print("\nstopping...")
        return 0
    finally:
        done = []
        ctl.shutdown(then=lambda: done.append(True))
        for _ in range(70):
            if done:
                break
            time.sleep(0.1)


def main() -> int:
    parser = argparse.ArgumentParser(prog="desktop-audio-streamer")
    parser.add_argument("-v", "--verbose", action="store_true")
    sub = parser.add_subparsers(dest="cmd", required=True)

    sub.add_parser("devices", help="list Cast devices/groups on the LAN")

    p_start = sub.add_parser("start", help="start casting to a device or group")
    p_start.add_argument("device", nargs="?", help="friendly name (default: last used)")
    p_start.add_argument("--stream-type", choices=["LIVE", "BUFFERED"])
    p_start.add_argument("--mode", choices=["auto", "mirror", "http"],
                         help="cast path (default from config: auto)")
    p_start.add_argument("--output-mode",
                         choices=["speakers", "this_pc", "both", "auto"],
                         help="where audio is audible while casting")
    p_start.add_argument("--no-mute", action="store_true",
                         help="legacy alias for --output-mode both")
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

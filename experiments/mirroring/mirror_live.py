"""EXPERIMENTAL: cast REAL desktop audio via MirrorSession (the M2 ear gate).

Wires the shipping plumbing - LoopbackCapture -> Pacer -> MirrorSession - so
what you hear is exactly what the integrated app will do. Local output is
muted while casting (same as the app) so the room hears only the speakers,
not the PC 0.4 s ahead.

Usage (repo root), while music/video is playing on this PC:
  .venv\\Scripts\\python -m experiments.mirroring.mirror_live "Dining Room"
  .venv\\Scripts\\python -m experiments.mirroring.mirror_live "Everywhere" --seconds 120

Ctrl+C stops, unmutes, quits the receiver app. --no-mute leaves local audio on
(useful only for a solo non-Office speaker A/B).
"""
from __future__ import annotations

import argparse
import logging
import sys
import time

from streamer.caster import Discovery
from streamer.capture import LoopbackCapture
from streamer.localmute import LocalMute
from streamer.pacer import Pacer
from streamer.mirror import MirrorSession

log = logging.getLogger("mirror_live")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("device")
    ap.add_argument("--seconds", type=int, default=120)
    ap.add_argument("--target-delay", type=int, default=400)
    ap.add_argument("--bitrate", type=int, default=128000)
    ap.add_argument("--no-mute", action="store_true")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    disco = Discovery()
    disco.wait_for_devices()
    capture = pacer = session = None
    mute = LocalMute()
    try:
        safe_cast = disco.connect(args.device)
        capture = LoopbackCapture(on_data=lambda d: pacer.feed(d))
        pacer = Pacer(capture.format, sink=lambda d: session.feed(d))
        session = MirrorSession(disco, safe_cast, capture,
                                on_state=lambda s, d=None: log.info("state: %s %s", s, d or ""),
                                target_delay=args.target_delay, bitrate=args.bitrate)
        pacer.start()
        capture.start()
        if not args.no_mute:
            mute.engage()
        session.start()

        t0 = time.monotonic()
        while time.monotonic() - t0 < args.seconds:
            time.sleep(5)
            st = session.status()
            log.info("t=%4.0fs playing=%s ckpt=%s playout=%sms rtcp=%s nacks=%s",
                     time.monotonic() - t0, st.get("playing"),
                     st.get("checkpoint"), st.get("playout_delay_ms"),
                     st.get("rtcp_datagrams"), st.get("nack_events"))
        return 0
    except KeyboardInterrupt:
        print("\nstopping...")
        return 0
    finally:
        mute.release()
        for obj in (session, capture, pacer):
            if obj is not None:
                try:
                    obj.stop()
                except Exception as exc:
                    log.warning("teardown %s: %s", type(obj).__name__, exc)
        disco.stop()


if __name__ == "__main__":
    sys.exit(main())

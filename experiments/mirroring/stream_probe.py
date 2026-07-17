"""EXPERIMENTAL: M3 transport probe - stream synthetic Opus to a receiver.

Night-time success = TRANSPORT success ONLY (checkpoint advancing, session
stable, receiver playout delay logged). Audio correctness and glass-to-glass
latency are daytime gates with the user present - see the spike plan.

Failure diagnosis is CHEAP-FIRST (dual-review): no inbound RTCP -> firewall;
RTCP but no checkpoint -> SR/frame-0 race or PT demux drop (retry --pt 96);
establishment already pre-cleared by the M0 probe; crypto pre-cleared by the
offline M1 tests.

Usage (repo root):
  .venv\\Scripts\\python -m experiments.mirroring.stream_probe "Dining Room" --seconds 600
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import threading
import time

from streamer.caster import Discovery
from . import webrtc_controller as wc
from .cast_rtp import (CastRtpSender, FRAME0_RESENDS, FRAME0_RESEND_SPACING)
from .opus_source import OpusSource

log = logging.getLogger("stream_probe")
FRAME_INTERVAL = 0.01           # 10 ms
STATUS_PERIOD = 5.0


def run(device: str, seconds: int, pt: int, target_delay: int,
        bit_rate: int, content: str = "tone", level_db: float = -60.0) -> dict:
    result: dict = {"device": device, "pt": pt, "targetDelay": target_delay,
                    "content": content, "level_db": level_db,
                    "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    disco = Discovery()
    disco.wait_for_devices()
    sender = None
    app_died = threading.Event()
    try:
        safe_cast = disco.connect(device)
        wc.launch_mirroring_app(safe_cast, wc.AUDIO_ONLY_APP_ID)
        launched_app = wc.AUDIO_ONLY_APP_ID

        class _AppWatch:
            def new_cast_status(self, status):
                if status.app_id != launched_app:
                    log.error("receiver app changed to %s - session dead",
                              status.app_id)
                    app_died.set()

        safe_cast.register_status_listener(_AppWatch())

        sig = wc.MirroringSignaling.create()
        safe_cast.register_handler(sig.controller)
        offer = wc.StreamOffer(rtp_payload_type=pt, target_delay=target_delay,
                               bit_rate=bit_rate)
        answer = sig.send_offer(offer)
        result["answer"] = {"udpPort": answer.udp_port,
                            "constraints": answer.constraints,
                            "sendIndexes": answer.send_indexes}
        if not answer.accepted:
            result["verdict"] = "STREAM_REFUSED"
            return result

        host = safe_cast.socket_client.host
        sender = CastRtpSender(host, answer.udp_port, offer.ssrc, pt,
                               offer.aes_key, offer.aes_iv_mask)
        sender.start()
        src = OpusSource(bit_rate=bit_rate, content=content,
                         level_db=level_db)

        total_frames = seconds * 100
        resend_at = {int((i + 1) * FRAME0_RESEND_SPACING / FRAME_INTERVAL)
                     for i in range(FRAME0_RESENDS)}
        t0 = time.monotonic()
        next_status = t0 + STATUS_PERIOD
        history: list[dict] = []
        for n in range(total_frames):
            # single-clock pacing: absolute schedule, no cumulative drift
            target = t0 + n * FRAME_INTERVAL
            delay = target - time.monotonic()
            if delay > 0:
                time.sleep(delay)
            sender.send_frame(src.next_packet())
            if n in resend_at:
                sender.resend_frame0()
            if app_died.is_set():
                result["verdict"] = "APP_DIED"
                break
            now = time.monotonic()
            if now >= next_status:
                next_status = now + STATUS_PERIOD
                snap = sender.stats.snapshot()
                snap["sent_frames"] = sender.last_sent_frame_id + 1
                snap["checkpoint_lag_frames"] = (
                    sender.last_sent_frame_id - snap["checkpoint"]
                    if snap["checkpoint"] >= 0 else None)
                snap["t"] = round(now - t0, 1)
                history.append(snap)
                log.info("t=%5.1fs sent=%d ckpt=%s lag=%s playout=%sms "
                         "rtcp=%d nacks=%d",
                         snap["t"], snap["sent_frames"], snap["checkpoint"],
                         snap["checkpoint_lag_frames"],
                         snap["playout_delay_ms"], snap["rtcp_datagrams"],
                         snap["nack_events"])
        else:
            result["verdict"] = None  # classified below

        result["history"] = history
        final = sender.stats.snapshot()
        final["sent_frames"] = sender.last_sent_frame_id + 1
        result["final"] = final

        if result.get("verdict") is None:
            if final["rtcp_datagrams"] == 0:
                result["verdict"] = "NO_RTCP"
                result["diagnosis"] = (
                    "No inbound RTCP at all -> check Windows Firewall inbound "
                    "UDP for this python image first (netsh advfirewall "
                    "firewall show rule name=all | findstr -i python), THEN "
                    "suspect the SR race.")
            elif final["checkpoint"] < 0:
                result["verdict"] = "NO_CHECKPOINT"
                result["diagnosis"] = (
                    "RTCP arrives but checkpoint never advanced -> SR/frame-0 "
                    "ordering, or PT demux drop: retry with --pt 96. Crypto "
                    "is pre-cleared offline (M1).")
            elif final["checkpoint"] < final["sent_frames"] - 200:
                result["verdict"] = "CHECKPOINT_STALLED"
            else:
                result["verdict"] = "TRANSPORT_OK"
        return result
    finally:
        if sender is not None:
            sender.stop()
        try:
            safe_cast.quit_app()
            safe_cast.disconnect(timeout=5)
        except Exception as exc:
            log.warning("teardown: %s", exc)
        disco.stop()
        result["ended"] = time.strftime("%Y-%m-%d %H:%M:%S")


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("device")
    ap.add_argument("--seconds", type=int, default=600)
    ap.add_argument("--pt", type=int, default=127)
    ap.add_argument("--target-delay", type=int,
                    default=wc.DEFAULT_TARGET_DELAY_MS)
    ap.add_argument("--bit-rate", type=int, default=128000)
    ap.add_argument("--content", choices=("tone", "click"), default="tone",
                    help="click = 1/s transient for glass-to-glass measurement")
    ap.add_argument("--level-db", type=float, default=-60.0,
                    help="content level dBFS (-60 inaudible; -25 audible)")
    ap.add_argument("--json", dest="json_path")
    args = ap.parse_args()
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(name)s %(levelname)s %(message)s")
    result = run(args.device, args.seconds, args.pt, args.target_delay,
                 args.bit_rate, args.content, args.level_db)
    print(json.dumps(result, indent=2, ensure_ascii=False))
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, ensure_ascii=False)
    return 0 if result.get("verdict") == "TRANSPORT_OK" else 1


if __name__ == "__main__":
    sys.exit(main())

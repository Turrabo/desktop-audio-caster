"""EXPERIMENTAL: Milestone 0 kill-check probe - launch + OFFER + ANSWER only.

Answers, per device: does a mirroring receiver app launch, does it advertise
the webrtc namespace, and what does it ANSWER to an audio-only OFFER?
Sends NO media, performs NO volume operations. Quits the app on exit.

Usage (from repo root):
  .venv\\Scripts\\python -m experiments.mirroring.probe_answer "Kitchen speaker"
  .venv\\Scripts\\python -m experiments.mirroring.probe_answer "Everywhere" --json out.json

Outcomes recorded per app id tried:
  ACCEPTED       app launched, ANSWER ok, index 0 in sendIndexes
  STREAM_REFUSED app launched, ANSWER ok, index 0 NOT accepted
  REJECTED       app launched, ANSWER result != ok
  NO_ANSWER      app launched + namespace up, but no ANSWER in 4 s
  NO_NAMESPACE   app launched but never advertised the webrtc namespace
  NO_LAUNCH      app did not launch
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
import time

from streamer.caster import Discovery
from . import webrtc_controller as wc

log = logging.getLogger("probe")


def probe_app(safe_cast, app_id: str, pt: int, target_delay: int) -> dict:
    """Try one receiver app id; return a structured outcome dict."""
    out: dict = {"app_id": app_id, "outcome": None}
    try:
        wc.launch_mirroring_app(safe_cast, app_id)
    except RuntimeError as exc:
        msg = str(exc)
        out["outcome"] = "NO_NAMESPACE" if "advertised" in msg else "NO_LAUNCH"
        out["detail"] = msg
        return out

    sig = wc.MirroringSignaling.create()
    safe_cast.register_handler(sig.controller)
    offer = wc.StreamOffer(rtp_payload_type=pt, target_delay=target_delay)
    out["offer"] = {"ssrc": offer.ssrc, "pt": pt, "targetDelay": target_delay}
    try:
        answer = sig.send_offer(offer)
    except RuntimeError as exc:
        out["outcome"] = "NO_ANSWER" if "no ANSWER" in str(exc) else "REJECTED"
        out["detail"] = str(exc)
        return out

    out["outcome"] = "ACCEPTED" if answer.accepted else "STREAM_REFUSED"
    out["answer"] = {
        "udpPort": answer.udp_port,
        "receiverSsrc": answer.receiver_ssrc,
        "sendIndexes": answer.send_indexes,
        "constraints": answer.constraints,
        "raw": answer.raw,
    }
    return out


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("device", help="friendly name of the speaker or group")
    ap.add_argument("--pt", type=int, default=127,
                    help="RTP payload type (127 AndroidTV hack / 96 canonical)")
    ap.add_argument("--target-delay", type=int,
                    default=wc.DEFAULT_TARGET_DELAY_MS)
    ap.add_argument("--json", dest="json_path",
                    help="also write the full result as JSON")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s")

    disco = Discovery()
    disco.wait_for_devices()
    result: dict = {"device": args.device, "probes": [],
                    "started": time.strftime("%Y-%m-%d %H:%M:%S")}
    try:
        safe_cast = disco.connect(args.device)
        info = disco.find(args.device)
        result["cast_type"] = info.cast_type if info else "?"
        for app_id in (wc.AUDIO_ONLY_APP_ID, wc.AV_APP_ID):
            probe = probe_app(safe_cast, app_id, args.pt, args.target_delay)
            result["probes"].append(probe)
            log.info("== %s -> %s", app_id, probe["outcome"])
            if probe["outcome"] in ("ACCEPTED", "STREAM_REFUSED"):
                break  # got a definitive protocol-level answer
            # try the next app id on launch/namespace/answer failures
        try:
            safe_cast.quit_app()
        except Exception as exc:  # teardown is best-effort
            log.warning("quit_app: %s", exc)
        safe_cast.disconnect(timeout=5)
    finally:
        disco.stop()

    print(json.dumps(result, indent=2, ensure_ascii=False))
    if args.json_path:
        with open(args.json_path, "w", encoding="utf-8") as fh:
            json.dump(result, fh, indent=2, ensure_ascii=False)
    accepted = any(p["outcome"] == "ACCEPTED" for p in result["probes"])
    return 0 if accepted else 1


if __name__ == "__main__":
    sys.exit(main())

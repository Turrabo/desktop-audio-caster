"""EXPERIMENTAL: Cast Streaming signaling controller (OFFER/ANSWER).

Adapted from chromecast-sink (Copyright (c) 2026 ねらひかだ, MIT License -
full text in THIRD_PARTY.md), cross-checked against chromium/openscreen.
Changes from upstream:
- targetDelay defaults to 400 ms: openscreen offer_messages.cc:267 requires
  target_delay > 0 for a valid audio stream (upstream's 0 is out-of-spec).
- App launch tries the audio-only receiver 85CDB22F before the A/V mirroring
  receiver 0F5096E8 (upstream hardcodes the latter). Both are empirical
  unknowns on Nest speakers.
- After launch, POLLS for the webrtc namespace instead of a fixed 0.5 s
  sleep (send_app_message raises UnsupportedNamespace if we lose that race).
- ANSWER handling distinguishes three outcomes: accepted (index 0 in
  sendIndexes), rejected-stream (ok but index 0 missing - "device may want
  video too"), and refused/timeout.

Payload type: 127 is openscreen's kAudioHackForAndroidTV (rtp_defines.h:99),
an empirical bet for Android-derived Nest firmware; 96 is the canonical Opus
PT and is available as a diagnostic retry, not a supported path.
"""
from __future__ import annotations

import json
import logging
import os
import secrets
import threading
import time
from dataclasses import dataclass, field

log = logging.getLogger(__name__)

WEBRTC_NAMESPACE = "urn:x-cast:com.google.cast.webrtc"
AUDIO_ONLY_APP_ID = "85CDB22F"   # openscreen: default audio-only streaming receiver
AV_APP_ID = "0F5096E8"           # Chrome Mirroring (A/V); upstream's choice
DEFAULT_TARGET_DELAY_MS = 400    # openscreen kDefaultTargetPlayoutDelay


@dataclass
class StreamOffer:
    """Audio stream parameters for the Cast Streaming OFFER."""

    ssrc: int = field(default_factory=lambda: secrets.randbelow(2**31) + 1)
    aes_key: bytes = field(default_factory=lambda: os.urandom(16))
    aes_iv_mask: bytes = field(default_factory=lambda: os.urandom(16))
    codec: str = "opus"
    sample_rate: int = 48000
    channels: int = 2
    bit_rate: int = 128000
    rtp_payload_type: int = 127
    target_delay: int = DEFAULT_TARGET_DELAY_MS


@dataclass
class StreamAnswer:
    """Parsed ANSWER from the receiver."""

    udp_port: int
    receiver_ssrc: int
    send_indexes: list[int]
    constraints: dict | None
    raw: dict

    @property
    def accepted(self) -> bool:
        """True when our audio stream (index 0) was accepted."""
        return 0 in self.send_indexes


class MirroringSignaling:
    """pychromecast controller for Cast Streaming signaling.

    Instantiated lazily via _make_controller() so this module can be
    imported (e.g. by the crypto unit tests) without pychromecast.
    """

    def __init__(self, controller) -> None:
        self._c = controller

    @classmethod
    def create(cls) -> "MirroringSignaling":
        return cls(_make_controller())

    @property
    def controller(self):
        return self._c

    def send_offer(self, offer: StreamOffer, timeout: float = 4.0) -> StreamAnswer:
        """Send OFFER and wait for ANSWER (openscreen kReplyTimeout is 4 s).

        Raises RuntimeError on rejection or timeout; the caller decides
        whether an ok-but-index-0-missing ANSWER is fatal (see .accepted).
        """
        return self._c.send_offer(offer, timeout)


def _make_controller():
    from pychromecast.controllers import BaseController

    class _WebRTCController(BaseController):
        def __init__(self) -> None:
            super().__init__(WEBRTC_NAMESPACE)
            self._answer_event = threading.Event()
            self._answer: StreamAnswer | None = None
            self._error: str | None = None
            self._seq_num = secrets.randbelow(2**30)

        def receive_message(self, _message, data: dict) -> bool:
            msg_type = data.get("type")
            log.debug("webrtc recv: %s", json.dumps(data))
            if msg_type == "ANSWER":
                self._handle_answer(data)
            return True

        def _handle_answer(self, data: dict) -> None:
            if data.get("result") != "ok":
                self._error = f"OFFER rejected: {json.dumps(data)}"
                log.error(self._error)
                self._answer_event.set()
                return
            answer = data.get("answer", {})
            self._answer = StreamAnswer(
                udp_port=answer["udpPort"],
                receiver_ssrc=answer.get("ssrcs", [0])[0],
                send_indexes=answer.get("sendIndexes", []),
                constraints=answer.get("constraints"),
                raw=data,
            )
            log.info("ANSWER: udpPort=%d sendIndexes=%s",
                     self._answer.udp_port, self._answer.send_indexes)
            if self._answer.constraints:
                # minDelay/maxDelay here is the receiver's real playout window
                log.info("ANSWER constraints: %s",
                         json.dumps(self._answer.constraints))
            self._answer_event.set()

        def send_offer(self, offer: StreamOffer, timeout: float) -> StreamAnswer:
            self._seq_num += 1
            self._answer = None
            self._error = None
            self._answer_event.clear()

            msg = {
                "type": "OFFER",
                "seqNum": self._seq_num,
                "offer": {
                    "castMode": "mirroring",
                    "receiverGetStatus": True,
                    "supportedStreams": [{
                        "index": 0,
                        "type": "audio_source",
                        "codecName": offer.codec,
                        "rtpProfile": "cast",
                        "rtpPayloadType": offer.rtp_payload_type,
                        "ssrc": offer.ssrc,
                        "targetDelay": offer.target_delay,
                        "aesKey": offer.aes_key.hex(),
                        "aesIvMask": offer.aes_iv_mask.hex(),
                        "timeBase": f"1/{offer.sample_rate}",
                        "bitRate": offer.bit_rate,
                        "sampleRate": offer.sample_rate,
                        "channels": offer.channels,
                        "receiverRtcpEventLog": True,
                    }],
                },
            }
            log.info("sending OFFER (seq=%d ssrc=%d pt=%d targetDelay=%dms)",
                     self._seq_num, offer.ssrc, offer.rtp_payload_type,
                     offer.target_delay)
            self.send_message(msg)

            if not self._answer_event.wait(timeout=timeout):
                raise RuntimeError(f"no ANSWER within {timeout}s")
            if self._error:
                raise RuntimeError(self._error)
            assert self._answer is not None
            return self._answer

    return _WebRTCController()


def launch_mirroring_app(safe_cast, app_id: str, timeout: float = 10.0) -> None:
    """Launch a mirroring receiver app on a SafeCast and wait until ready.

    "Ready" means BOTH the app is running AND the webrtc namespace is
    advertised (polled - no fixed sleep; the namespace can lag the app_id).
    """
    sc = safe_cast.socket_client
    if safe_cast.app_id != app_id:
        app_ready = threading.Event()

        class _Listener:
            def new_cast_status(self, status):
                if status.app_id == app_id:
                    app_ready.set()

        safe_cast.register_status_listener(_Listener())
        log.info("launching app %s on %r", app_id, safe_cast.name)
        safe_cast.start_app(app_id)
        if not app_ready.wait(timeout=timeout):
            raise RuntimeError(
                f"app {app_id} did not launch (current: {safe_cast.app_id})")

    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if WEBRTC_NAMESPACE in (sc.app_namespaces or []):
            log.info("webrtc namespace advertised by %s", app_id)
            return
        time.sleep(0.1)
    raise RuntimeError(
        f"app {app_id} running but never advertised {WEBRTC_NAMESPACE} "
        f"(namespaces: {sc.app_namespaces})")

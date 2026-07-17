"""EXPERIMENTAL: Cast RTP sender - encrypt, packetize, send, hear feedback.

Adapted from chromecast-sink cast_rtp.py (Copyright (c) 2026 ねらひかだ,
MIT License - full text in THIRD_PARTY.md); reworked per dual review:
- FIRST Sender Report is sent SYNCHRONOUSLY, anchored to the first frame's
  wall time, BEFORE frame 0: the receiver drops all RTP until it has an SR
  (openscreen receiver_impl.cc:270-273), and with no retransmission a
  dropped frame 0 wedges the checkpoint forever.
- Frame 0 is re-sent (identical bytes) a few times as a poor-man's
  retransmit for that one spike-fatal loss case.
- RTCP receive parses Cast Feedback properly (rtcp_feedback.py) instead of
  hex-logging: checkpoint (expanded), receiver playout delay, NACK counts.
- Encryption delegated to frame_crypto.py (offline-tested, M1 gate).

Deliberately NOT here (production scope): NACK-driven retransmission,
kickstart, ACK-gated flow control, adaptive playout delay.
"""
from __future__ import annotations

import logging
import socket
import struct
import threading
import time
from dataclasses import dataclass, field

from .frame_crypto import FrameCrypto
from .rtcp_feedback import expand_frame_id, parse_compound

log = logging.getLogger(__name__)

OPUS_SAMPLES_PER_FRAME = 480          # 10 ms at 48 kHz
_NTP_EPOCH_OFFSET = 2208988800        # 1900 -> 1970 epoch, seconds
RTCP_INTERVAL = 0.5                   # openscreen kRtcpReportInterval
FRAME0_RESENDS = 3
FRAME0_RESEND_SPACING = 0.05


@dataclass
class FeedbackStats:
    """What the receiver has told us, updated by the RTCP receive thread."""
    datagrams: int = 0
    feedbacks: int = 0
    checkpoint: int = -1              # expanded frame id, -1 = never
    playout_delay_ms: int = -1
    nack_events: int = 0
    first_feedback_at: float | None = None
    last_feedback_at: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict:
        with self.lock:
            return {
                "rtcp_datagrams": self.datagrams,
                "cast_feedbacks": self.feedbacks,
                "checkpoint": self.checkpoint,
                "playout_delay_ms": self.playout_delay_ms,
                "nack_events": self.nack_events,
            }


class CastRtpSender:
    """One negotiated audio stream to one receiver."""

    def __init__(self, host: str, udp_port: int, ssrc: int,
                 payload_type: int, aes_key: bytes, aes_iv_mask: bytes):
        self._dest = (host, udp_port)
        self._ssrc = ssrc & 0xFFFFFFFF
        self._pt = payload_type & 0x7F
        self._crypto = FrameCrypto(aes_key, aes_iv_mask)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(0.5)
        self._seq = 0
        self._frame_id = 0
        self._rtp_ts = 0
        self._packets = 0
        self._octets = 0
        self._running = False
        self._sr_thread: threading.Thread | None = None
        self._recv_thread: threading.Thread | None = None
        self._frame0_packet: bytes | None = None
        self.stats = FeedbackStats()

    # -- packet building -----------------------------------------------------

    def _build_packet(self, encrypted: bytes, frame_id: int) -> bytes:
        # 12-byte RTP header; marker set (every frame is one whole packet)
        rtp = struct.pack(">BBHII", 0x80, 0x80 | self._pt,
                          self._seq & 0xFFFF, self._rtp_ts & 0xFFFFFFFF,
                          self._ssrc)
        # 6-byte Cast extension: keyframe flag (audio: always), 8-bit frame
        # id, packet id / max packet id (always 0/0: one packet per frame)
        cast = struct.pack(">BBHH", 0x80, frame_id & 0xFF, 0, 0)
        return rtp + cast + encrypted

    def _build_sr(self, rtp_ts: int) -> bytes:
        now = time.time()
        return struct.pack(
            ">BBHIIIIII", 0x80, 200, 6, self._ssrc,
            (int(now) + _NTP_EPOCH_OFFSET) & 0xFFFFFFFF,
            int((now % 1) * (1 << 32)) & 0xFFFFFFFF,
            rtp_ts & 0xFFFFFFFF, self._packets & 0xFFFFFFFF,
            self._octets & 0xFFFFFFFF)

    # -- sending -------------------------------------------------------------

    def send_frame(self, opus_frame: bytes) -> None:
        if self._frame_id == 0:
            # lip-sync anchor MUST precede any RTP (see module docstring)
            self._sock.sendto(self._build_sr(0), self._dest)
        packet = self._build_packet(
            self._crypto.encrypt(self._frame_id, opus_frame), self._frame_id)
        self._sock.sendto(packet, self._dest)
        if self._frame_id == 0:
            self._frame0_packet = packet
        self._seq += 1
        self._frame_id += 1
        self._rtp_ts += OPUS_SAMPLES_PER_FRAME
        self._packets += 1
        self._octets += len(opus_frame)

    def resend_frame0(self) -> None:
        """Identical-bytes re-send: covers frame-0 loss with no NACK path."""
        if self._frame0_packet is not None:
            self._sock.sendto(self._frame0_packet, self._dest)

    @property
    def last_sent_frame_id(self) -> int:
        return self._frame_id - 1

    # -- background threads ----------------------------------------------------

    def start(self) -> None:
        self._running = True
        self._sr_thread = threading.Thread(
            target=self._sr_loop, name="rtcp-sr", daemon=True)
        self._recv_thread = threading.Thread(
            target=self._recv_loop, name="rtcp-recv", daemon=True)
        self._sr_thread.start()
        self._recv_thread.start()

    def _sr_loop(self) -> None:
        while self._running:
            try:
                self._sock.sendto(self._build_sr(self._rtp_ts), self._dest)
            except OSError as exc:
                log.debug("SR send: %s", exc)
            for _ in range(int(RTCP_INTERVAL / 0.05)):
                if not self._running:
                    return
                time.sleep(0.05)

    def _recv_loop(self) -> None:
        while self._running:
            try:
                data, _addr = self._sock.recvfrom(4096)
            except socket.timeout:
                continue
            except OSError:
                return
            now = time.monotonic()
            fbs = parse_compound(data, self._ssrc)
            with self.stats.lock:
                self.stats.datagrams += 1
                for fb in fbs:
                    self.stats.feedbacks += 1
                    self.stats.last_feedback_at = now
                    if self.stats.first_feedback_at is None:
                        self.stats.first_feedback_at = now
                    expanded = expand_frame_id(
                        fb.checkpoint_truncated, self.last_sent_frame_id)
                    # monotonic guard: a wild value = parser bug, not a stall
                    if expanded >= self.stats.checkpoint:
                        self.stats.checkpoint = expanded
                    else:
                        log.warning("checkpoint went backwards: %d -> %d",
                                    self.stats.checkpoint, expanded)
                    self.stats.playout_delay_ms = fb.playout_delay_ms
                    if fb.nacks:
                        self.stats.nack_events += 1
                        log.info("NACK: %s", fb.nacks[:4])

    def stop(self) -> None:
        self._running = False
        for t in (self._sr_thread, self._recv_thread):
            if t:
                t.join(timeout=2)
        try:
            self._sock.close()
        except OSError:
            pass
        log.info("sender stopped: %d frames, %d bytes, stats=%s",
                 self._frame_id, self._octets, self.stats.snapshot())

"""Cast mirroring protocol - the sub-second cast path.

Single home for the mirroring logic (promoted from experiments/mirroring,
which the spike proved on real hardware, commit b560d64): signaling
(OFFER/ANSWER on urn:x-cast:com.google.cast.webrtc), Cast RTP/RTCP with
AES-128-CTR, Cast Feedback parsing, and MirrorSession (the CastSession-shaped
session with reframing, NACK retransmission, and a watchdog).

Protocol behaviour is adapted from chromecast-sink (MIT, see
experiments/mirroring/THIRD_PARTY.md and assets/README.md for the shipped
attribution) and cross-checked against chromium/openscreen (BSD-3-Clause,
reference only). AES-128-CTR runs on Windows CNG via streamer._aesctr (no
third-party crypto dependency); Opus via streamer._opus (assets/opus.dll).
"""
from __future__ import annotations

import collections
import json
import logging
import os
import secrets
import socket
import struct
import threading
import time
from dataclasses import dataclass, field

from ._aesctr import FrameCrypto
from ._opus import CHANNELS, FRAME_SAMPLES, SAMPLE_RATE, OpusEncoder

log = logging.getLogger(__name__)

# Eligibility: the encoder is fixed at 48 kHz stereo 16-bit; a capture format
# that differs is routed to the HTTP path (no resampler in v1).
ELIGIBLE_RATE = SAMPLE_RATE
ELIGIBLE_CHANNELS = CHANNELS
ELIGIBLE_SAMPWIDTH = 2
FRAME_BYTES = FRAME_SAMPLES * ELIGIBLE_CHANNELS * ELIGIBLE_SAMPWIDTH  # 1920


def eligible_format(fmt) -> bool:
    return (fmt.rate == ELIGIBLE_RATE and fmt.channels == ELIGIBLE_CHANNELS
            and fmt.sampwidth == ELIGIBLE_SAMPWIDTH)

# -- protocol constants ------------------------------------------------------

WEBRTC_NAMESPACE = "urn:x-cast:com.google.cast.webrtc"
AUDIO_ONLY_APP_ID = "85CDB22F"   # openscreen: default audio-only streaming receiver
AV_APP_ID = "0F5096E8"           # Chrome Mirroring (A/V); fallback
DEFAULT_TARGET_DELAY_MS = 400    # openscreen kDefaultTargetPlayoutDelay
OPUS_SAMPLES_PER_FRAME = FRAME_SAMPLES     # 480 = 10 ms at 48 kHz
_NTP_EPOCH_OFFSET = 2208988800
RTCP_INTERVAL = 0.5

# -- Cast Feedback (RTCP) parsing --------------------------------------------

RTCP_PT_PAYLOAD_SPECIFIC = 206
FMT_FEEDBACK = 15
CAST_MAGIC = b"CAST"
CST2_MAGIC = b"CST2"
ALL_PACKETS_LOST = 0xFFFF
MAX_UNACKED_FRAMES = 120         # openscreen constants.h kMaxUnackedFrames


@dataclass
class CastFeedback:
    checkpoint_frame_id: int
    checkpoint_truncated: int
    playout_delay_ms: int
    nacks: list = field(default_factory=list)   # (within_frame_id, packet_id, bitvec)
    has_cst2_ack: bool = False
    ack_bitvector: bytes = b""


def expand_frame_id(truncated: int, last_sent: int) -> int:
    """Largest frame id <= last_sent congruent to `truncated` mod 256.
    Valid while the sender keeps <=MAX_UNACKED_FRAMES in flight."""
    if last_sent < 0:
        return truncated
    return last_sent - ((last_sent - truncated) & 0xFF)


def parse_compound(data: bytes, sender_ssrc: int) -> list:
    """Every Cast Feedback aimed at sender_ssrc in a compound RTCP datagram."""
    out: list = []
    offset, n = 0, len(data)
    while offset + 4 <= n:
        byte0 = data[offset]
        if (byte0 >> 6) != 2:
            break
        pt = data[offset + 1]
        length_words = struct.unpack_from(">H", data, offset + 2)[0]
        pkt_len = (length_words + 1) * 4
        if offset + pkt_len > n:
            break
        if pt == RTCP_PT_PAYLOAD_SPECIFIC and (byte0 & 0x1F) == FMT_FEEDBACK:
            fb = _parse_feedback(data[offset:offset + pkt_len], sender_ssrc)
            if fb is not None:
                out.append(fb)
        offset += pkt_len
    return out


def _parse_feedback(pkt: bytes, sender_ssrc: int):
    if len(pkt) < 20:
        return None
    if struct.unpack_from(">I", pkt, 8)[0] != (sender_ssrc & 0xFFFFFFFF):
        return None
    if pkt[12:16] != CAST_MAGIC:
        return None
    ckpt, loss_count = pkt[16], pkt[17]
    playout_ms = struct.unpack_from(">H", pkt, 18)[0]
    fb = CastFeedback(checkpoint_frame_id=ckpt, checkpoint_truncated=ckpt,
                      playout_delay_ms=playout_ms)
    pos = 20
    for _ in range(loss_count):
        if pos + 4 > len(pkt):
            return fb
        fb.nacks.append((pkt[pos], struct.unpack_from(">H", pkt, pos + 1)[0],
                         pkt[pos + 3]))
        pos += 4
    if pos + 6 <= len(pkt) and pkt[pos:pos + 4] == CST2_MAGIC:
        octets = pkt[pos + 5]
        fb.has_cst2_ack = True
        fb.ack_bitvector = pkt[pos + 6:pos + 6 + octets]
    return fb


# -- signaling (OFFER/ANSWER) ------------------------------------------------

@dataclass
class StreamOffer:
    ssrc: int = field(default_factory=lambda: secrets.randbelow(2**31) + 1)
    aes_key: bytes = field(default_factory=lambda: os.urandom(16))
    aes_iv_mask: bytes = field(default_factory=lambda: os.urandom(16))
    codec: str = "opus"
    sample_rate: int = 48000
    channels: int = 2
    bit_rate: int = 128000
    rtp_payload_type: int = 127          # AndroidTV hack PT; Nest is Android-derived
    target_delay: int = DEFAULT_TARGET_DELAY_MS


@dataclass
class StreamAnswer:
    udp_port: int
    receiver_ssrc: int
    send_indexes: list
    constraints: dict | None
    raw: dict

    @property
    def accepted(self) -> bool:
        return 0 in self.send_indexes


def make_signaling():
    """Build a pychromecast controller for the webrtc namespace (lazy import
    so this module loads without pychromecast, e.g. for the crypto tests)."""
    from pychromecast.controllers import BaseController

    class _WebRTCController(BaseController):
        def __init__(self) -> None:
            super().__init__(WEBRTC_NAMESPACE)
            self._answer_event = threading.Event()
            self._answer = None
            self._error = None
            self._seq_num = secrets.randbelow(2**30)

        def receive_message(self, _message, data: dict) -> bool:
            if data.get("type") == "ANSWER":
                self._handle_answer(data)
            return True

        def _handle_answer(self, data: dict) -> None:
            if data.get("result") != "ok":
                self._error = f"OFFER rejected: {json.dumps(data)}"
                self._answer_event.set()
                return
            a = data.get("answer", {})
            self._answer = StreamAnswer(
                udp_port=a["udpPort"], receiver_ssrc=a.get("ssrcs", [0])[0],
                send_indexes=a.get("sendIndexes", []),
                constraints=a.get("constraints"), raw=data)
            log.info("ANSWER: udpPort=%d sendIndexes=%s",
                     self._answer.udp_port, self._answer.send_indexes)
            self._answer_event.set()

        def send_offer(self, offer: StreamOffer, timeout: float) -> StreamAnswer:
            self._seq_num += 1
            self._answer = self._error = None
            self._answer_event.clear()
            msg = {"type": "OFFER", "seqNum": self._seq_num, "offer": {
                "castMode": "mirroring", "receiverGetStatus": True,
                "supportedStreams": [{
                    "index": 0, "type": "audio_source", "codecName": offer.codec,
                    "rtpProfile": "cast", "rtpPayloadType": offer.rtp_payload_type,
                    "ssrc": offer.ssrc, "targetDelay": offer.target_delay,
                    "aesKey": offer.aes_key.hex(), "aesIvMask": offer.aes_iv_mask.hex(),
                    "timeBase": f"1/{offer.sample_rate}", "bitRate": offer.bit_rate,
                    "sampleRate": offer.sample_rate, "channels": offer.channels,
                    "receiverRtcpEventLog": True}]}}
            log.info("sending OFFER (seq=%d ssrc=%d pt=%d targetDelay=%dms)",
                     self._seq_num, offer.ssrc, offer.rtp_payload_type,
                     offer.target_delay)
            self.send_message(msg)
            if not self._answer_event.wait(timeout=timeout):
                raise RuntimeError(f"no ANSWER within {timeout}s")
            if self._error:
                raise RuntimeError(self._error)
            return self._answer

    return _WebRTCController()


def launch_mirroring_app(safe_cast, app_id: str, timeout: float = 10.0) -> None:
    """Launch a mirroring receiver app and wait until BOTH it is running AND
    the webrtc namespace is advertised (polled; the namespace can lag app_id)."""
    sc = safe_cast.socket_client
    if safe_cast.app_id != app_id:
        ready = threading.Event()

        class _L:
            def new_cast_status(self, status):
                if status.app_id == app_id:
                    ready.set()

        safe_cast.register_status_listener(_L())
        log.info("launching app %s on %r", app_id, safe_cast.name)
        safe_cast.start_app(app_id)
        if not ready.wait(timeout=timeout):
            raise RuntimeError(f"app {app_id} did not launch (now {safe_cast.app_id})")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if WEBRTC_NAMESPACE in (sc.app_namespaces or []):
            return
        time.sleep(0.1)
    raise RuntimeError(f"app {app_id} up but no {WEBRTC_NAMESPACE} "
                       f"(namespaces: {sc.app_namespaces})")


# -- RTP sender --------------------------------------------------------------

@dataclass
class FeedbackStats:
    datagrams: int = 0
    feedbacks: int = 0
    checkpoint: int = -1
    checkpoint_raw: int = -1          # last truncated wire byte (stall detection)
    checkpoint_raw_since: float = 0.0
    playout_delay_ms: int = -1
    nack_events: int = 0
    first_feedback_at: float | None = None
    last_feedback_at: float | None = None
    lock: threading.Lock = field(default_factory=threading.Lock)

    def snapshot(self) -> dict:
        with self.lock:
            return {"rtcp_datagrams": self.datagrams, "cast_feedbacks": self.feedbacks,
                    "checkpoint": self.checkpoint, "playout_delay_ms": self.playout_delay_ms,
                    "nack_events": self.nack_events,
                    "first_feedback_at": self.first_feedback_at,
                    "last_feedback_at": self.last_feedback_at,
                    "checkpoint_raw_since": self.checkpoint_raw_since}


class CastRtpSender:
    """One negotiated audio stream: encrypt, packetize, send, hear feedback,
    retransmit NACKed packets. Promoted from the spike with NACK added."""

    NACK_HISTORY = 128
    NACK_MIN_INTERVAL = 0.1

    def __init__(self, host, udp_port, ssrc, payload_type, aes_key, aes_iv_mask):
        self._dest = (host, udp_port)
        self._ssrc = ssrc & 0xFFFFFFFF
        self._pt = payload_type & 0x7F
        self._crypto = FrameCrypto(aes_key, aes_iv_mask)
        self._sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._sock.settimeout(0.5)
        self._seq = self._frame_id = self._rtp_ts = 0
        self._packets = self._octets = 0
        self._running = False
        self._sr_thread = self._recv_thread = None
        self._frame0_packet = None
        self._history: dict = {}         # frame_id & 0xFF -> (packet_bytes, frame_id)
        self._last_retx: dict = {}       # frame_id & 0xFF -> monotonic
        self.stats = FeedbackStats()

    def _build_packet(self, encrypted: bytes, frame_id: int) -> bytes:
        rtp = struct.pack(">BBHII", 0x80, 0x80 | self._pt, self._seq & 0xFFFF,
                          self._rtp_ts & 0xFFFFFFFF, self._ssrc)
        cast = struct.pack(">BBHH", 0x80, frame_id & 0xFF, 0, 0)
        return rtp + cast + encrypted

    def _build_sr(self, rtp_ts: int) -> bytes:
        now = time.time()
        return struct.pack(">BBHIIIIII", 0x80, 200, 6, self._ssrc,
                           (int(now) + _NTP_EPOCH_OFFSET) & 0xFFFFFFFF,
                           int((now % 1) * (1 << 32)) & 0xFFFFFFFF,
                           rtp_ts & 0xFFFFFFFF, self._packets & 0xFFFFFFFF,
                           self._octets & 0xFFFFFFFF)

    def send_frame(self, opus_frame: bytes) -> None:
        if self._frame_id == 0:
            self._sock.sendto(self._build_sr(0), self._dest)   # SR before frame 0
        packet = self._build_packet(
            self._crypto.encrypt(self._frame_id, opus_frame), self._frame_id)
        self._sock.sendto(packet, self._dest)
        if self._frame_id == 0:
            self._frame0_packet = packet
        self._history[self._frame_id & 0xFF] = (packet, self._frame_id)
        self._seq += 1
        self._frame_id += 1
        self._rtp_ts += OPUS_SAMPLES_PER_FRAME
        self._packets += 1
        self._octets += len(opus_frame)

    def resend_frame0(self) -> None:
        if self._frame0_packet is not None:
            self._sock.sendto(self._frame0_packet, self._dest)

    @property
    def last_sent_frame_id(self) -> int:
        return self._frame_id - 1

    def start(self) -> None:
        self._running = True
        self._sr_thread = threading.Thread(target=self._sr_loop, name="rtcp-sr", daemon=True)
        self._recv_thread = threading.Thread(target=self._recv_loop, name="rtcp-recv", daemon=True)
        self._sr_thread.start()
        self._recv_thread.start()

    def _sr_loop(self) -> None:
        while self._running:
            try:
                self._sock.sendto(self._build_sr(self._rtp_ts), self._dest)
            except OSError:
                pass
            for _ in range(int(RTCP_INTERVAL / 0.05)):
                if not self._running:
                    return
                time.sleep(0.05)

    def _retransmit(self, nacks) -> None:
        now = time.monotonic()
        for within_frame_id, _packet_id, _bitvec in nacks:
            key = within_frame_id & 0xFF
            entry = self._history.get(key)
            if entry is None:
                continue
            if now - self._last_retx.get(key, 0.0) < self.NACK_MIN_INTERVAL:
                continue
            self._sock.sendto(entry[0], self._dest)
            self._last_retx[key] = now

    def _recv_loop(self) -> None:
        while self._running:
            try:
                data, _ = self._sock.recvfrom(4096)
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
                    if fb.checkpoint_truncated != self.stats.checkpoint_raw:
                        self.stats.checkpoint_raw = fb.checkpoint_truncated
                        self.stats.checkpoint_raw_since = now
                    expanded = expand_frame_id(fb.checkpoint_truncated,
                                               self.last_sent_frame_id)
                    if expanded >= self.stats.checkpoint:
                        self.stats.checkpoint = expanded
                    self.stats.playout_delay_ms = fb.playout_delay_ms
                    if fb.nacks:
                        self.stats.nack_events += 1
                        self._retransmit(fb.nacks)

    def stop(self) -> None:
        self._running = False
        for t in (self._sr_thread, self._recv_thread):
            if t:
                t.join(timeout=1)
        try:
            self._sock.close()
        except OSError:
            pass
        try:
            self._crypto.close()
        except Exception:
            pass


# -- reframing sink (pacer -> 480-sample Opus frames) ------------------------

class MirrorSink:
    """Carves the pacer's variable-size chunks into exact 480-sample frames,
    Opus-encodes each, and hands packets to `on_packet`. feed() runs on the
    pacer thread and never blocks (encode happens on a pump thread); the
    bounded frame queue drops oldest to cap added latency, like server clients.
    """

    QUEUE_FRAMES = 30            # ~0.3 s of 10 ms frames

    def __init__(self, on_packet, bitrate: int = 128000):
        self._on_packet = on_packet
        self._bitrate = bitrate
        self._encoder: OpusEncoder | None = None   # created in start()
        self._buf = bytearray()
        self._frames: collections.deque = collections.deque(maxlen=self.QUEUE_FRAMES)
        self._lock = threading.Lock()
        self._wake = threading.Event()
        self._stop = threading.Event()
        self._pump: threading.Thread | None = None
        self.dropped_frames = 0
        self.sent_frames = 0

    def pending_frames(self) -> int:
        """Frames carved and awaiting encode (test/telemetry helper)."""
        with self._lock:
            return len(self._frames)

    def feed(self, pcm: bytes) -> None:
        """Pacer thread: reframe into 480-sample PCM frames, enqueue."""
        with self._lock:
            self._buf.extend(pcm)
            while len(self._buf) >= FRAME_BYTES:
                frame = bytes(self._buf[:FRAME_BYTES])
                del self._buf[:FRAME_BYTES]
                if len(self._frames) == self._frames.maxlen:
                    self.dropped_frames += 1   # deque drops the oldest itself
                self._frames.append(frame)
        self._wake.set()

    def start(self) -> None:
        self._encoder = OpusEncoder(self._bitrate)
        self._stop.clear()
        self._pump = threading.Thread(target=self._run, name="mirror-pump", daemon=True)
        self._pump.start()

    def _run(self) -> None:
        while not self._stop.is_set():
            self._wake.wait(timeout=0.1)
            self._wake.clear()
            while True:
                with self._lock:
                    if not self._frames:
                        break
                    frame = self._frames.popleft()
                try:
                    packet = self._encoder.encode(frame)
                except Exception as exc:
                    log.warning("opus encode failed: %s", exc)
                    continue
                self._on_packet(packet)
                self.sent_frames += 1

    def stop(self) -> None:
        self._stop.set()
        self._wake.set()
        if self._pump is not None:
            self._pump.join(timeout=1)
        if self._encoder is not None:
            self._encoder.close()


# -- session (CastSession-shaped) --------------------------------------------

FIRST_CHECKPOINT_DEADLINE = 3.0     # s after ANSWER to see the first checkpoint
LAUNCH_TIMEOUT = 5.0
ANSWER_TIMEOUT = 4.0
MONITOR_PERIOD = 0.5
RTCP_SILENCE_TIMEOUT = 6.0          # no feedback at all this long -> dead
CHECKPOINT_STALL_TIMEOUT = 2.0      # raw checkpoint byte frozen while sending
REOFFER_MAX_ATTEMPTS = 2            # mid-session recoveries before HTTP fallback
BACKOFF_START, BACKOFF_CAP = 2.0, 30.0


class MirrorFirstFrameError(RuntimeError):
    """Raised when a mirror session cannot reach PLAYING (start-time failure)."""


class MirrorSession:
    """One mirroring session to one device/group. Exposes the same surface as
    CastSession (start/stop/lag_seconds/trim_count/recast_count/safe_cast and
    the on_state contract) so appctl, the popover, the tray, and the cli need
    no mirror-specific code paths.

    Reliability model:
    - Start-time failure (never reached PLAYING) raises MirrorFirstFrameError
      from start(); appctl falls straight back to HTTP (no retry - a broken
      protocol is deterministic).
    - Mid-session loss (was PLAYING) is recovered by re-OFFER with backoff up
      to REOFFER_MAX_ATTEMPTS; only then does on_fallback_needed fire, so a
      transient Wi-Fi blip does not cost the 1.1 s HTTP regression.
    """

    def __init__(self, discovery, safe_cast, capture, on_state=None,
                 on_fallback_needed=None, target_delay=DEFAULT_TARGET_DELAY_MS,
                 bitrate=128000):
        self._discovery = discovery
        self._cast = safe_cast
        self._capture = capture
        self._on_state = on_state or (lambda state, detail=None: None)
        self._on_fallback_needed = on_fallback_needed or (lambda reason: None)
        self._target_delay = target_delay
        self._bitrate = bitrate
        self._controller = None
        self._sender: CastRtpSender | None = None
        self._sink: MirrorSink | None = None
        self._watchdog: threading.Thread | None = None
        self._stop = threading.Event()
        self._playing = False
        self._answer_at = 0.0
        self._last_ui_state = ""
        self._host = ""
        self._local_ip = ""
        self.trim_count = 0        # mirror does not trim; kept for the contract
        self.recast_count = 0

    @property
    def safe_cast(self):
        return self._cast

    def _emit(self, state: str, detail: str | None = None) -> None:
        key = f"{state}:{detail}"
        if key != self._last_ui_state:
            self._last_ui_state = key
            self._on_state(state, detail)

    def feed(self, pcm: bytes) -> None:
        """Pacer sink. Safe before the sink exists (frames just wait)."""
        if self._sink is not None:
            self._sink.feed(pcm)

    def lag_seconds(self) -> float | None:
        """Receiver's live playout delay (its own measure), in seconds."""
        if not self._playing or self._sender is None:
            return None
        pd = self._sender.stats.playout_delay_ms
        return pd / 1000.0 if pd >= 0 else None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        if not eligible_format(self._capture.format):
            raise MirrorFirstFrameError(
                f"capture {self._capture.format} not mirror-eligible")
        self._emit("LAUNCHING", self._cast.name)
        self._host = self._cast.socket_client.host
        from .caster import source_ip_for
        self._local_ip = source_ip_for(self._host)
        self._controller = make_signaling()
        self._cast.register_handler(self._controller)

        self._establish()                         # raises on start-time failure
        if not self._await_playing():
            self._teardown_stream()
            raise MirrorFirstFrameError("no checkpoint within deadline")
        self._emit("PLAYING", self._playing_detail())

        self._watchdog = threading.Thread(target=self._run_watchdog,
                                          name="mirror-watchdog", daemon=True)
        self._watchdog.start()

    def _establish(self) -> None:
        """Launch (idempotent) + OFFER + fresh sender/sink. Raises on failure."""
        self._launch()
        offer = StreamOffer(target_delay=self._target_delay, bit_rate=self._bitrate)
        answer = self._controller.send_offer(offer, ANSWER_TIMEOUT)
        if not answer.accepted:
            raise MirrorFirstFrameError(
                f"stream refused (sendIndexes={answer.send_indexes})")
        self._log_constraints(answer)
        self._sender = CastRtpSender(self._host, answer.udp_port, offer.ssrc,
                                     offer.rtp_payload_type, offer.aes_key,
                                     offer.aes_iv_mask)
        self._sink = MirrorSink(self._sender.send_frame, bitrate=self._bitrate)
        self._sender.start()
        self._sink.start()
        self._answer_at = time.monotonic()
        self._playing = False
        self._emit("BUFFERING", self._cast.name)

    def _teardown_stream(self) -> None:
        """Stop the current sink+sender (keep the connection for re-OFFER)."""
        if self._sink is not None:
            self._sink.stop()
            self._sink = None
        if self._sender is not None:
            self._sender.stop()
            self._sender = None

    def _launch(self) -> str:
        errors = []
        for app_id in (AUDIO_ONLY_APP_ID, AV_APP_ID):
            try:
                launch_mirroring_app(self._cast, app_id, timeout=LAUNCH_TIMEOUT)
                return app_id
            except RuntimeError as exc:
                errors.append(f"{app_id}: {exc}")
        raise MirrorFirstFrameError("; ".join(errors))

    def _log_constraints(self, answer) -> None:
        c = (answer.constraints or {}).get("audio") if answer.constraints else None
        if c and ("minDelay" in c or "maxDelay" in c):
            log.info("receiver delay window: min=%s max=%s ms",
                     c.get("minDelay"), c.get("maxDelay"))

    def _playing_detail(self) -> str:
        lag = self.lag_seconds()
        return self._cast.name if lag is None else f"{self._cast.name}|{lag:.1f}"

    def _await_playing(self) -> bool:
        """Poll until the first checkpoint (True) or the deadline (False)."""
        while not self._stop.is_set():
            if self._sender and self._sender.stats.snapshot()["checkpoint"] >= 0:
                self._playing = True
                return True
            if time.monotonic() - self._answer_at > FIRST_CHECKPOINT_DEADLINE:
                return False
            time.sleep(0.1)
        return False

    def _failure_reason(self, now: float) -> str | None:
        """Why the live session is unhealthy, or None. Pure enough to unit-test
        with a fake clock + synthetic FeedbackStats."""
        if not self._capture.healthy:
            return "capture unhealthy"
        if not eligible_format(self._capture.format):
            return "capture format changed"
        try:
            from .caster import source_ip_for
            if source_ip_for(self._host) != self._local_ip:
                return "local IP changed"
        except OSError:
            return "no route to speaker"
        if self._sender is None:
            return "sender gone"
        snap = self._sender.stats.snapshot()
        last_fb = snap["last_feedback_at"]
        if last_fb is not None and now - last_fb > RTCP_SILENCE_TIMEOUT:
            return "rtcp silence"
        # raw checkpoint byte frozen while we keep sending = wedged receiver
        raw_since = snap["checkpoint_raw_since"]
        if (snap["checkpoint"] >= 0 and raw_since
                and now - raw_since > CHECKPOINT_STALL_TIMEOUT):
            return "checkpoint stalled"
        return None

    def _run_watchdog(self) -> None:
        backoff = BACKOFF_START
        while not self._stop.is_set():
            time.sleep(MONITOR_PERIOD)
            reason = self._failure_reason(time.monotonic())
            if reason is None:
                backoff = BACKOFF_START
                self._emit("PLAYING", self._playing_detail())
                continue
            log.warning("mirror unhealthy: %s", reason)
            if not self._recover(reason, backoff):
                log.error("mirror recovery exhausted (%s) -> HTTP fallback", reason)
                self._on_fallback_needed(reason)
                return
            backoff = min(backoff * 2, BACKOFF_CAP)

    def _recover(self, reason: str, backoff: float) -> bool:
        """Re-OFFER up to REOFFER_MAX_ATTEMPTS. True if PLAYING was restored."""
        for attempt in range(1, REOFFER_MAX_ATTEMPTS + 1):
            if self._stop.is_set():
                return False
            self._emit("RECONNECTING", f"{reason} (attempt {attempt})")
            self._teardown_stream()
            if self._stop.wait(backoff):
                return False
            try:
                self._establish()
            except Exception as exc:
                log.warning("re-OFFER attempt %d failed: %s", attempt, exc)
                continue
            self.recast_count += 1
            if self._await_playing():
                self._emit("PLAYING", self._playing_detail())
                return True
        return False

    def status(self) -> dict:
        snap = self._sender.stats.snapshot() if self._sender else {}
        return {"device": self._cast.name, "mode": "mirror",
                "playing": self._playing, "recasts": self.recast_count, **snap}

    def stop(self) -> None:
        self._stop.set()
        if self._watchdog is not None:
            self._watchdog.join(timeout=MONITOR_PERIOD + 1)
        self._teardown_stream()
        try:
            self._cast.quit_app()
        except Exception as exc:
            log.debug("quit_app: %s", exc)
        try:
            self._cast.disconnect(timeout=5)
        except Exception:
            pass

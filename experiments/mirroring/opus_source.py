"""EXPERIMENTAL: synthetic 48 kHz Opus frame source for the mirroring spike.

PyAV libopus encoder configured per the protocol requirements:
- frame_duration 10 (ms) -> 480 samples/frame (libopus default is 20 ms;
  the RTP timestamp advance is hard-coupled to 480, so this must hold)
- application lowdelay (minimal lookahead)
- DTX explicitly off (review N4: a -60 dB tone could otherwise be
  classified silent and suppressed, breaking the one-frame-per-10ms pacing)

Default content is a -60 dBFS 440 Hz tone: quiet enough to be inaudible at
normal volumes, non-zero so silence handling can't confuse the probe. For
the daytime gates there are audible modes: level_db raises the tone, and
content="click" emits one sharp 5 ms burst per second - the transient makes
the phone-recording glass-to-glass offset measurement trivial. Stdlib only
(a one-second precomputed int16 table; no numpy in this venv).
"""
from __future__ import annotations

import array
import math

import av

SAMPLE_RATE = 48000
CHANNELS = 2
FRAME_SAMPLES = 480          # 10 ms
TONE_HZ = 440.0              # divides 48000 -> the 1 s table loops cleanly


def _tone_table(level_db: float) -> array.array:
    """One second of interleaved stereo int16 tone (L == R)."""
    amp = 32767 * 10 ** (level_db / 20)
    t = array.array("h")
    for n in range(SAMPLE_RATE):
        v = int(amp * math.sin(2 * math.pi * TONE_HZ * n / SAMPLE_RATE))
        t.append(v)
        t.append(v)
    return t


def _click_table(level_db: float) -> array.array:
    """One second: a 5 ms 1 kHz burst at t=0, silence after."""
    amp = 32767 * 10 ** (level_db / 20)
    t = array.array("h")
    burst = int(SAMPLE_RATE * 0.005)
    for n in range(SAMPLE_RATE):
        v = int(amp * math.sin(2 * math.pi * 1000 * n / SAMPLE_RATE)) if n < burst else 0
        t.append(v)
        t.append(v)
    return t


class OpusSource:
    def __init__(self, bit_rate: int = 128000, content: str = "tone",
                 level_db: float = -60.0):
        cc = av.CodecContext.create("libopus", "w")
        cc.sample_rate = SAMPLE_RATE
        cc.layout = "stereo"
        cc.format = "s16"
        cc.bit_rate = bit_rate
        cc.options = {
            "frame_duration": "10",
            "application": "lowdelay",
            "dtx": "0",
        }
        cc.open()
        if cc.frame_size != FRAME_SAMPLES:
            raise RuntimeError(
                f"libopus frame_size={cc.frame_size}, need {FRAME_SAMPLES} "
                "(frame_duration option not honoured)")
        self._cc = cc
        make = _click_table if content == "click" else _tone_table
        self._table = make(level_db).tobytes()  # 1 s, loops cleanly
        self._frame_bytes = FRAME_SAMPLES * CHANNELS * 2
        self._pos = 0
        self._pts = 0
        self._pending: list[bytes] = []

    def _tone_frame(self) -> "av.AudioFrame":
        end = self._pos + self._frame_bytes
        pcm = self._table[self._pos:end]
        self._pos = end % len(self._table)
        frame = av.AudioFrame(format="s16", layout="stereo",
                              samples=FRAME_SAMPLES)
        frame.planes[0].update(pcm)
        frame.sample_rate = SAMPLE_RATE
        frame.pts = self._pts
        self._pts += FRAME_SAMPLES
        return frame

    def next_packet(self) -> bytes:
        """One encoded Opus packet (one 10 ms frame). The encoder's internal
        delay is absorbed by feeding until a packet emerges."""
        while not self._pending:
            for pkt in self._cc.encode(self._tone_frame()):
                self._pending.append(bytes(pkt))
        return self._pending.pop(0)


def self_check() -> dict:
    """Offline validation: packet cadence and sizes look like 10 ms Opus."""
    src = OpusSource()
    sizes = [len(src.next_packet()) for _ in range(100)]
    return {
        "packets": len(sizes),
        "min": min(sizes), "max": max(sizes),
        "mean": sum(sizes) / len(sizes),
        "expected_mean_range": (40, 400),   # ~128kbps/100fps = 160 B/pkt
    }


if __name__ == "__main__":
    print(self_check())

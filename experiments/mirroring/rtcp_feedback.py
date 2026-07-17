"""EXPERIMENTAL: Cast Feedback (RTCP) parser - the sender's success signal.

Ported from the wire diagrams in openscreen rtp_defines.h:142-309 (the
chromecast-sink reference only hex-logs this; a real parser is new code).

Cast Feedback packet (inside a compound RTCP datagram):
  RTCP common header: V=2|P|FMT=15, PT=206 (payload-specific), length
  SSRC of receiver (4) | SSRC of sender (4) | 'CAST' (4)
  checkpoint frame id (1, TRUNCATED to 8 bits) | loss field count (1) |
  current playout delay ms (2)
  loss fields (4 each): within-frame id (1) | lost packet id (2) | bitvec (1)
  optional 'CST2' (4) + feedback count (1) + bitvec octet count (1) +
  frame-level ACK bit vector

Checkpoint semantics: all frames <= it are fully RECEIVED (transport-level;
says nothing about decryption/playback - see the spike plan). The 8-bit
truncation is expanded against the highest frame id we have sent: the
checkpoint can never exceed it, so we take the largest value <= last_sent
congruent to the truncated byte mod 256.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass, field

RTCP_PT_PAYLOAD_SPECIFIC = 206
FMT_FEEDBACK = 15
CAST_MAGIC = b"CAST"
CST2_MAGIC = b"CST2"
ALL_PACKETS_LOST = 0xFFFF

# openscreen constants.h kMaxUnackedFrames: the design-safe checkpoint window.
MAX_UNACKED_FRAMES = 120


@dataclass
class CastFeedback:
    checkpoint_frame_id: int          # expanded (see expand_frame_id)
    checkpoint_truncated: int         # the raw 8-bit wire value
    playout_delay_ms: int             # receiver's CURRENT playout delay
    nacks: list[tuple[int, int, int]] = field(default_factory=list)
    # (within_frame_id_truncated, packet_id, bitvector); packet_id may be
    # ALL_PACKETS_LOST meaning the whole frame is missing
    has_cst2_ack: bool = False
    ack_bitvector: bytes = b""


def expand_frame_id(truncated: int, last_sent: int) -> int:
    """Largest frame id <= last_sent congruent to `truncated` mod 256.

    Correct while the sender honours the <=MAX_UNACKED_FRAMES in-flight
    window (the spike's frame rate is 100 fps, so ~1.2 s)."""
    if last_sent < 0:
        return truncated
    return last_sent - ((last_sent - truncated) & 0xFF)


def parse_compound(data: bytes, sender_ssrc: int) -> list[CastFeedback]:
    """Extract every Cast Feedback aimed at `sender_ssrc` from a compound
    RTCP datagram. Unknown/other packet types are skipped structurally.

    Checkpoint expansion is NOT done here (needs last_sent); callers use
    expand_frame_id. checkpoint_frame_id is set == checkpoint_truncated.
    """
    out: list[CastFeedback] = []
    offset = 0
    n = len(data)
    while offset + 4 <= n:
        byte0 = data[offset]
        if (byte0 >> 6) != 2:      # RTCP version must be 2
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


def _parse_feedback(pkt: bytes, sender_ssrc: int) -> CastFeedback | None:
    # header(4) + receiver ssrc(4) + sender ssrc(4) + CAST(4) + ckpt line(4)
    if len(pkt) < 20:
        return None
    media_ssrc = struct.unpack_from(">I", pkt, 8)[0]
    if media_ssrc != (sender_ssrc & 0xFFFFFFFF):
        return None
    if pkt[12:16] != CAST_MAGIC:
        return None
    ckpt = pkt[16]
    loss_count = pkt[17]
    playout_ms = struct.unpack_from(">H", pkt, 18)[0]

    fb = CastFeedback(checkpoint_frame_id=ckpt, checkpoint_truncated=ckpt,
                      playout_delay_ms=playout_ms)
    pos = 20
    for _ in range(loss_count):
        if pos + 4 > len(pkt):
            return fb  # truncated packet: keep what we have
        wfid = pkt[pos]
        packet_id = struct.unpack_from(">H", pkt, pos + 1)[0]
        bitvec = pkt[pos + 3]
        fb.nacks.append((wfid, packet_id, bitvec))
        pos += 4

    if pos + 6 <= len(pkt) and pkt[pos:pos + 4] == CST2_MAGIC:
        octets = pkt[pos + 5]
        start = pos + 6
        fb.has_cst2_ack = True
        fb.ack_bitvector = pkt[start:start + octets]
    return fb

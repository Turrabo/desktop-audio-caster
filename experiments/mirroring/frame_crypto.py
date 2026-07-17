"""EXPERIMENTAL: Cast Streaming per-frame encryption (AES-128-CTR).

Nonce construction per openscreen frame_crypto.cc:85-91 (cross-checked
byte-for-byte against chromecast-sink cast_rtp.py by dual review):
16 zero bytes; frame_id's low 32 bits written big-endian at offset 8;
the whole 16 bytes XORed with the stream's IV mask. CTR counter starts at
that nonce with block offset 0; the whole frame payload is one CTR stream.

RTP headers stay cleartext; only the payload is encrypted.
"""
from __future__ import annotations

from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

FRAME_ID_OFFSET = 8  # where the 32-bit frame id lands in the 16-byte nonce


class FrameCrypto:
    def __init__(self, aes_key: bytes, aes_iv_mask: bytes) -> None:
        if len(aes_key) != 16 or len(aes_iv_mask) != 16:
            raise ValueError("aes_key and aes_iv_mask must be 16 bytes")
        self._key = aes_key
        self._iv_mask = aes_iv_mask

    def nonce(self, frame_id: int) -> bytes:
        raw = bytearray(16)
        raw[FRAME_ID_OFFSET:FRAME_ID_OFFSET + 4] = (
            (frame_id & 0xFFFFFFFF).to_bytes(4, "big"))
        return bytes(b ^ m for b, m in zip(raw, self._iv_mask))

    def encrypt(self, frame_id: int, payload: bytes) -> bytes:
        enc = Cipher(algorithms.AES(self._key),
                     modes.CTR(self.nonce(frame_id))).encryptor()
        return enc.update(payload) + enc.finalize()

    def decrypt(self, frame_id: int, payload: bytes) -> bytes:
        # CTR is symmetric; kept separate for test readability.
        dec = Cipher(algorithms.AES(self._key),
                     modes.CTR(self.nonce(frame_id))).decryptor()
        return dec.update(payload) + dec.finalize()

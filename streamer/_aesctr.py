"""AES-128-CTR via Windows CNG (bcrypt.dll) - no third-party crypto dependency.

The Cast mirroring media path encrypts each frame with AES-128-CTR. CTR is
just: keystream = AES-ECB(counter blocks), ciphertext = plaintext XOR
keystream. CNG provides the AES-ECB primitive; we assemble the counter blocks
and the XOR here, so nothing shipped reimplements the cipher itself. Validated
byte-for-byte against a known-answer vector in tests/test_mirroring_crypto.py.

Nonce (openscreen frame_crypto.cc:85-91): 16 zero bytes, frame_id's low 32
bits big-endian at offset 8, XORed with the stream's 16-byte IV mask. That
nonce is the initial CTR counter; it increments big-endian per 16-byte block.
"""
from __future__ import annotations

import ctypes
from ctypes import wintypes

_bcrypt = ctypes.WinDLL("bcrypt.dll")

_BCRYPT_AES_ALGORITHM = "AES"
_BCRYPT_CHAINING_MODE = "ChainingMode"
_BCRYPT_CHAIN_MODE_ECB = "ChainingModeECB"
_BLOCK = 16
FRAME_ID_OFFSET = 8


def _nt(status: int, what: str) -> None:
    if status != 0:  # STATUS_SUCCESS
        raise OSError(f"{what} failed: NTSTATUS 0x{status & 0xFFFFFFFF:08X}")


class _EcbKey:
    """One AES-128 key handle in ECB mode (keystream generator for CTR)."""

    def __init__(self, key: bytes):
        if len(key) != 16:
            raise ValueError("AES-128 key must be 16 bytes")
        self._alg = ctypes.c_void_p()
        _nt(_bcrypt.BCryptOpenAlgorithmProvider(
            ctypes.byref(self._alg), _BCRYPT_AES_ALGORITHM, None, 0),
            "BCryptOpenAlgorithmProvider")
        mode = ctypes.create_unicode_buffer(_BCRYPT_CHAIN_MODE_ECB)
        _nt(_bcrypt.BCryptSetProperty(
            self._alg, _BCRYPT_CHAINING_MODE,
            ctypes.cast(mode, ctypes.POINTER(ctypes.c_ubyte)),
            len(_BCRYPT_CHAIN_MODE_ECB) * ctypes.sizeof(ctypes.c_wchar), 0),
            "BCryptSetProperty(ChainingMode)")
        self._key = ctypes.c_void_p()
        keybuf = (ctypes.c_ubyte * len(key)).from_buffer_copy(key)
        _nt(_bcrypt.BCryptGenerateSymmetricKey(
            self._alg, ctypes.byref(self._key), None, 0, keybuf, len(key), 0),
            "BCryptGenerateSymmetricKey")

    def ecb(self, blocks: bytes) -> bytes:
        """AES-ECB encrypt (len must be a multiple of 16). No IV, no padding."""
        inbuf = (ctypes.c_ubyte * len(blocks)).from_buffer_copy(blocks)
        out = (ctypes.c_ubyte * len(blocks))()
        done = wintypes.ULONG(0)
        _nt(_bcrypt.BCryptEncrypt(
            self._key, inbuf, len(blocks), None, None, 0,
            out, len(out), ctypes.byref(done), 0), "BCryptEncrypt")
        return bytes(out[:done.value])

    def close(self) -> None:
        if getattr(self, "_key", None):
            _bcrypt.BCryptDestroyKey(self._key)
            self._key = None
        if getattr(self, "_alg", None):
            _bcrypt.BCryptCloseAlgorithmProvider(self._alg, 0)
            self._alg = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


class FrameCrypto:
    """AES-128-CTR per Cast frame. Interface matches the spike's
    experiments/mirroring/frame_crypto.py so tests/probes port unchanged."""

    def __init__(self, aes_key: bytes, aes_iv_mask: bytes):
        if len(aes_key) != 16 or len(aes_iv_mask) != 16:
            raise ValueError("aes_key and aes_iv_mask must be 16 bytes")
        self._key = _EcbKey(aes_key)
        self._iv_mask = aes_iv_mask

    def nonce(self, frame_id: int) -> bytes:
        raw = bytearray(16)
        raw[FRAME_ID_OFFSET:FRAME_ID_OFFSET + 4] = (
            (frame_id & 0xFFFFFFFF).to_bytes(4, "big"))
        return bytes(b ^ m for b, m in zip(raw, self._iv_mask))

    def _keystream(self, frame_id: int, nbytes: int) -> bytes:
        counter = int.from_bytes(self.nonce(frame_id), "big")
        nblocks = (nbytes + _BLOCK - 1) // _BLOCK
        blocks = bytearray()
        for i in range(nblocks):
            blocks += ((counter + i) & ((1 << 128) - 1)).to_bytes(_BLOCK, "big")
        return self._key.ecb(bytes(blocks))[:nbytes]

    def encrypt(self, frame_id: int, payload: bytes) -> bytes:
        ks = self._keystream(frame_id, len(payload))
        return bytes(a ^ b for a, b in zip(payload, ks))

    def decrypt(self, frame_id: int, payload: bytes) -> bytes:
        return self.encrypt(frame_id, payload)   # CTR is symmetric

    def close(self) -> None:
        self._key.close()

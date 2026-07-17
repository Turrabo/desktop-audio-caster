"""ctypes binding for libopus (assets/opus.dll) - encoder only.

Built from the official opus 1.5.2 release with MSVC/UCRT (no MinGW runtime
deps); provenance + SHA-256 in assets/README.md. We ship only the encoder
surface the mirror path needs: create, ctl(bitrate, dtx), encode, destroy.
"""
from __future__ import annotations

import ctypes
import logging

from .assets import ASSETS

log = logging.getLogger(__name__)

# opus_defines.h
OPUS_APPLICATION_RESTRICTED_LOWDELAY = 2051
OPUS_SET_BITRATE_REQUEST = 4002
OPUS_SET_DTX_REQUEST = 4016
OPUS_SET_COMPLEXITY_REQUEST = 4010

SAMPLE_RATE = 48000
CHANNELS = 2
FRAME_SAMPLES = 480          # 10 ms at 48 kHz
_MAX_PACKET = 4000


class OpusError(RuntimeError):
    pass


_lib_singleton = None


def _load():
    global _lib_singleton
    if _lib_singleton is not None:
        return _lib_singleton
    dll_path = ASSETS / "opus.dll"
    lib = ctypes.CDLL(str(dll_path))
    lib.opus_get_version_string.restype = ctypes.c_char_p
    lib.opus_encoder_create.restype = ctypes.c_void_p
    lib.opus_encoder_create.argtypes = [
        ctypes.c_int, ctypes.c_int, ctypes.c_int, ctypes.POINTER(ctypes.c_int)]
    lib.opus_encode.restype = ctypes.c_int
    lib.opus_encode.argtypes = [
        ctypes.c_void_p, ctypes.POINTER(ctypes.c_int16), ctypes.c_int,
        ctypes.POINTER(ctypes.c_ubyte), ctypes.c_int]
    lib.opus_encoder_destroy.restype = None
    lib.opus_encoder_destroy.argtypes = [ctypes.c_void_p]
    # opus_encoder_ctl is variadic; do NOT set argtypes (pass typed c_int args)
    lib.opus_encoder_ctl.restype = ctypes.c_int
    _lib_singleton = lib
    return lib


class OpusEncoder:
    """48 kHz stereo, 10 ms frames, low-delay. One instance per session."""

    def __init__(self, bitrate: int = 128000):
        self._lib = _load()
        err = ctypes.c_int(0)
        self._enc = self._lib.opus_encoder_create(
            SAMPLE_RATE, CHANNELS, OPUS_APPLICATION_RESTRICTED_LOWDELAY,
            ctypes.byref(err))
        if err.value != 0 or not self._enc:
            raise OpusError(f"opus_encoder_create failed: {err.value}")
        self._ctl(OPUS_SET_BITRATE_REQUEST, bitrate)
        self._ctl(OPUS_SET_DTX_REQUEST, 0)   # never suppress: keeps 100 fps cadence
        self._out = (ctypes.c_ubyte * _MAX_PACKET)()
        log.info("libopus %s, %d bps, 10 ms low-delay",
                 self._lib.opus_get_version_string().decode(), bitrate)

    @property
    def version(self) -> str:
        return self._lib.opus_get_version_string().decode()

    def _ctl(self, request: int, value: int) -> None:
        # variadic: handle must be explicitly c_void_p (restype int overflows
        # the default marshalling), request/value explicitly c_int.
        rc = self._lib.opus_encoder_ctl(ctypes.c_void_p(self._enc),
                                        ctypes.c_int(request), ctypes.c_int(value))
        if rc != 0:
            raise OpusError(f"opus_encoder_ctl({request}) failed: {rc}")

    def encode(self, pcm480: bytes) -> bytes:
        """One 480-sample stereo s16 frame (1920 bytes) -> one Opus packet."""
        expected = FRAME_SAMPLES * CHANNELS * 2
        if len(pcm480) != expected:
            raise ValueError(f"frame must be {expected} bytes, got {len(pcm480)}")
        pcm = ctypes.cast(pcm480, ctypes.POINTER(ctypes.c_int16))
        n = self._lib.opus_encode(ctypes.c_void_p(self._enc), pcm, FRAME_SAMPLES,
                                  self._out, _MAX_PACKET)
        if n < 0:
            raise OpusError(f"opus_encode failed: {n}")
        return bytes(self._out[:n])

    def close(self) -> None:
        if getattr(self, "_enc", None):
            self._lib.opus_encoder_destroy(self._enc)
            self._enc = None

    def __del__(self):
        try:
            self.close()
        except Exception:
            pass


def available() -> bool:
    """True if opus.dll loads and an encoder can be created (eligibility check)."""
    try:
        OpusEncoder().close()
        return True
    except Exception as exc:
        log.info("opus unavailable: %s", exc)
        return False

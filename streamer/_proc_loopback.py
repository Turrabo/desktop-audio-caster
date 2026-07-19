"""Windows process-loopback capture activation (hand-rolled COM via ctypes).

Captures the whole system audio BEFORE the endpoint volume/mute stage, so the
cast is decoupled from the PC's volume and mute (proven: changing either leaves
the captured signal unchanged). Uses ActivateAudioInterfaceAsync with an AGILE
completion handler - the load-bearing detail, since a non-agile handler makes
the API return E_ILLEGAL_METHOD_CALL - on an MTA thread with Media Foundation
started.

Not tied to a render endpoint (survives default-device changes); captures at a
requested 48 kHz stereo 16-bit format. Needs Windows 10 build 20348+; supported()
is the ground truth (no GetVersionEx check - it is shimmed to Win 8 in the
unmanifested frozen exe and would disable the feature everywhere).
"""
from __future__ import annotations

import ctypes
import logging
import os
import threading
from ctypes import HRESULT, POINTER, byref, c_uint64, wintypes

import comtypes
from comtypes import COMMETHOD, COMObject, GUID, IUnknown

from pycaw.api.audioclient import IAudioClient
from pycaw.api.audioclient.depend import WAVEFORMATEX

log = logging.getLogger(__name__)

BYTE = ctypes.c_ubyte
UINT32 = ctypes.c_uint32
DWORD = wintypes.DWORD

VAD_PROCESS_LOOPBACK = "VAD\\Process_Loopback"
AUDCLNT_SHAREMODE_SHARED = 0
AUDCLNT_STREAMFLAGS_LOOPBACK = 0x00020000
AUDCLNT_STREAMFLAGS_EVENTCALLBACK = 0x00040000
AUDCLNT_ACTIVATION_TYPE_PROCESS_LOOPBACK = 1
PROCESS_LOOPBACK_MODE_INCLUDE = 0
PROCESS_LOOPBACK_MODE_EXCLUDE = 1
AUDCLNT_BUFFERFLAGS_SILENT = 0x2
VT_BLOB = 65
WAVE_FORMAT_PCM = 1
MF_VERSION = 0x00020070

IID_IAudioClient = GUID("{1CB9AD4C-DBFA-4C32-B178-C2F568A703B2}")
IID_IAudioCaptureClient = GUID("{C8ADBD64-E71E-48A0-A4DE-185C395CD317}")

CAPTURE_RATE = 48000
CAPTURE_CHANNELS = 2
CAPTURE_SAMPWIDTH = 2

# Typed kernel32 event calls. Declaring restype matters: the default c_int would
# sign-truncate a 64-bit HANDLE.
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)
_k32.CreateEventW.restype = wintypes.HANDLE
_k32.CreateEventW.argtypes = [ctypes.c_void_p, wintypes.BOOL, wintypes.BOOL,
                              wintypes.LPCWSTR]
_k32.WaitForSingleObject.restype = DWORD
_k32.WaitForSingleObject.argtypes = [wintypes.HANDLE, DWORD]
_k32.CloseHandle.restype = wintypes.BOOL
_k32.CloseHandle.argtypes = [wintypes.HANDLE]


def create_event():
    h = _k32.CreateEventW(None, False, False, None)
    if not h:
        raise ctypes.WinError(ctypes.get_last_error())
    return h


def wait_event(handle, timeout_ms: int) -> int:
    return _k32.WaitForSingleObject(handle, timeout_ms)


def close_event(handle) -> None:
    _k32.CloseHandle(handle)


class PROCESS_LOOPBACK_PARAMS(ctypes.Structure):
    _fields_ = [("TargetProcessId", DWORD), ("ProcessLoopbackMode", ctypes.c_int)]


class ACTIVATION_PARAMS(ctypes.Structure):
    _fields_ = [("ActivationType", ctypes.c_int),
                ("ProcessLoopbackParams", PROCESS_LOOPBACK_PARAMS)]


class PROPVARIANT(ctypes.Structure):
    _fields_ = [("vt", wintypes.USHORT), ("r1", wintypes.USHORT),
                ("r2", wintypes.USHORT), ("r3", wintypes.USHORT),
                ("cbSize", wintypes.ULONG), ("pBlobData", ctypes.c_void_p)]


class IActivateAudioInterfaceAsyncOperation(IUnknown):
    _iid_ = GUID("{72A22D78-CDE4-431D-B8CC-843A71199B6D}")
    _methods_ = (
        COMMETHOD([], HRESULT, "GetActivateResult",
                  (["out"], POINTER(HRESULT), "activateResult"),
                  (["out"], POINTER(POINTER(IUnknown)), "activatedInterface")),
    )


class IActivateAudioInterfaceCompletionHandler(IUnknown):
    _iid_ = GUID("{41D949AB-9862-444A-80F6-C261334DA5EB}")
    _methods_ = (
        COMMETHOD([], HRESULT, "ActivateCompleted",
                  (["in"], POINTER(IActivateAudioInterfaceAsyncOperation), "op")),
    )


class IAgileObject(IUnknown):
    # Marker (no methods): tells COM the object is callable from any apartment.
    # ActivateAudioInterfaceAsync requires an agile completion handler.
    _iid_ = GUID("{94EA2B94-E9CC-49E0-C0FF-EE64CA8F5B90}")
    _methods_ = ()


class IAudioCaptureClient(IUnknown):
    _iid_ = IID_IAudioCaptureClient
    _methods_ = (
        COMMETHOD([], HRESULT, "GetBuffer",
                  (["out"], POINTER(POINTER(BYTE)), "ppData"),
                  (["out"], POINTER(UINT32), "pNumFrames"),
                  (["out"], POINTER(DWORD), "pdwFlags"),
                  (["out"], POINTER(c_uint64), "pDevPos"),
                  (["out"], POINTER(c_uint64), "pQpcPos")),
        COMMETHOD([], HRESULT, "ReleaseBuffer", (["in"], UINT32, "NumFramesRead")),
        COMMETHOD([], HRESULT, "GetNextPacketSize",
                  (["out"], POINTER(UINT32), "pNumFrames")),
    )


class _Handler(COMObject):
    _com_interfaces_ = [IActivateAudioInterfaceCompletionHandler, IAgileObject]

    def __init__(self, done):
        super().__init__()
        self._done = done

    def ActivateCompleted(self, this, op):
        self._done.set()
        return 0


def capture_waveformat() -> WAVEFORMATEX:
    wfx = WAVEFORMATEX()
    wfx.wFormatTag = WAVE_FORMAT_PCM
    wfx.nChannels = CAPTURE_CHANNELS
    wfx.nSamplesPerSec = CAPTURE_RATE
    wfx.wBitsPerSample = CAPTURE_SAMPWIDTH * 8
    wfx.nBlockAlign = CAPTURE_CHANNELS * CAPTURE_SAMPWIDTH
    wfx.nAvgBytesPerSec = CAPTURE_RATE * wfx.nBlockAlign
    wfx.cbSize = 0
    return wfx


def mf_startup() -> None:
    ctypes.WinDLL("Mfplat.dll").MFStartup(MF_VERSION, 0)


def mf_shutdown() -> None:
    try:
        ctypes.WinDLL("Mfplat.dll").MFShutdown()
    except OSError:
        pass


def activate_process_loopback(exclude_pid: int, timeout: float = 5.0) -> IAudioClient:
    """Activate a whole-system (EXCLUDE exclude_pid) process-loopback audio
    client. MUST be called on an MTA thread with Media Foundation started.
    Returns an IAudioClient (not yet Initialized)."""
    params = ACTIVATION_PARAMS()
    params.ActivationType = AUDCLNT_ACTIVATION_TYPE_PROCESS_LOOPBACK
    params.ProcessLoopbackParams.TargetProcessId = exclude_pid
    params.ProcessLoopbackParams.ProcessLoopbackMode = PROCESS_LOOPBACK_MODE_EXCLUDE

    pv = PROPVARIANT()
    pv.vt = VT_BLOB
    pv.cbSize = ctypes.sizeof(params)
    pv.pBlobData = ctypes.addressof(params)

    done = threading.Event()
    handler = _Handler(done)                      # keep ref alive across the call
    fn = ctypes.WinDLL("Mmdevapi.dll").ActivateAudioInterfaceAsync
    fn.restype = ctypes.c_int32
    fn.argtypes = [wintypes.LPCWSTR, POINTER(GUID), POINTER(PROPVARIANT),
                   POINTER(IActivateAudioInterfaceCompletionHandler),
                   POINTER(POINTER(IActivateAudioInterfaceAsyncOperation))]
    hptr = handler.QueryInterface(IActivateAudioInterfaceCompletionHandler)
    op = POINTER(IActivateAudioInterfaceAsyncOperation)()
    hr = fn(VAD_PROCESS_LOOPBACK, byref(IID_IAudioClient), byref(pv), hptr, byref(op))
    if hr != 0:
        raise OSError("ActivateAudioInterfaceAsync hr=0x%08X" % (hr & 0xFFFFFFFF))
    if not done.wait(timeout=timeout):
        raise TimeoutError("activation callback never fired")
    act_hr, iface = op.GetActivateResult()
    if act_hr != 0:
        raise OSError("activate result hr=0x%08X" % (act_hr & 0xFFFFFFFF))
    return iface.QueryInterface(IAudioClient)


_supported_cache: bool | None = None


def supported() -> bool:
    """Whether process-loopback capture works on this machine. The trial (spawns
    an MTA thread, activates, Initializes and releases) is the sole ground truth
    - no shimmable version check. A DEFINITE answer is cached, since capability
    is constant per process; a probe that TIMES OUT is deliberately not cached,
    so a cold audio stack at boot cannot permanently downgrade a startup launch
    to the coupled path."""
    global _supported_cache
    if _supported_cache is not None:
        return _supported_cache
    result = {"ok": False, "definite": False}

    def probe():
        client = None
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            mf_startup()
            client = activate_process_loopback(os.getpid())
            wfx = capture_waveformat()
            client.Initialize(AUDCLNT_SHAREMODE_SHARED,
                              AUDCLNT_STREAMFLAGS_LOOPBACK
                              | AUDCLNT_STREAMFLAGS_EVENTCALLBACK,
                              2_000_000, 0, byref(wfx), None)
            result["ok"] = True
        except Exception as exc:
            log.info("process loopback not available: %s", exc)
        finally:
            result["definite"] = True
            # Drop the interface BEFORE CoUninitialize - releasing it after its
            # apartment is gone is undefined.
            client = None
            mf_shutdown()
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass

    t = threading.Thread(target=probe, name="procloop-probe", daemon=True)
    t.start()
    t.join(timeout=8)
    if not result["definite"]:
        log.info("process-loopback probe timed out; not caching (will retry)")
        return False
    _supported_cache = result["ok"]
    log.info("process-loopback capture supported: %s", _supported_cache)
    return _supported_cache

"""Desktop audio capture. Two backends behind one interface (open_capture):

- ProcessLoopbackCapture: whole-system capture BEFORE the endpoint volume/mute
  (Windows 10 build 20348+), so the cast is decoupled from the PC's volume/mute.
  Fixed 48 kHz stereo 16-bit; not tied to a render device.
- LoopbackCapture: WASAPI endpoint loopback (the fallback). 16-bit PCM at the
  device's native rate; the endpoint VOLUME scales it, so casting couples to the
  PC volume (hence the mute-and-pin machinery in localmute).

Both deliver 16-bit PCM into a callback and expose .format / .healthy /
.restart_count / .couples_volume. Health: read failures flip ``healthy`` False
and trigger recovery; the caster's watchdog reads ``healthy``.
"""
from __future__ import annotations

import ctypes
import logging
import os
import threading
import time
from dataclasses import dataclass
from typing import Callable

import comtypes
import pyaudiowpatch as pyaudio

from . import _proc_loopback as _pl

log = logging.getLogger(__name__)

FRAMES_PER_BUFFER = 480  # 10 ms at 48 kHz


@dataclass(frozen=True)
class CaptureFormat:
    rate: int
    channels: int
    sampwidth: int  # bytes per sample (2 = 16-bit)

    @property
    def bytes_per_second(self) -> int:
        return self.rate * self.channels * self.sampwidth

    @property
    def frame_bytes(self) -> int:
        return self.channels * self.sampwidth


class LoopbackCapture:
    couples_volume = True    # endpoint volume scales this capture (see localmute)

    def __init__(self, on_data: Callable[[bytes], None], device_hint: str | None = None):
        self._on_data = on_data
        self._device_hint = device_hint
        self._pa = pyaudio.PyAudio()
        self._stream = None
        self._thread: threading.Thread | None = None
        self._stop = threading.Event()
        self.healthy = False
        self.restart_count = 0
        self.format = self._probe_format()

    # -- device / format ------------------------------------------------

    def _find_loopback(self) -> dict:
        wasapi = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
        default_out = self._pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
        target_name = self._device_hint or default_out["name"]
        candidates = list(self._pa.get_loopback_device_info_generator())
        for lb in candidates:
            if target_name.lower() in lb["name"].lower():
                return lb
        if self._device_hint:
            raise RuntimeError(f"no loopback device matches {self._device_hint!r}")
        if not candidates:
            raise RuntimeError("no WASAPI loopback devices found")
        log.warning("default output %r has no loopback twin; using %r",
                    default_out["name"], candidates[0]["name"])
        return candidates[0]

    def _probe_format(self) -> CaptureFormat:
        lb = self._find_loopback()
        fmt = CaptureFormat(rate=int(lb["defaultSampleRate"]),
                            channels=min(2, int(lb["maxInputChannels"])) or 2,
                            sampwidth=2)
        log.info("capture format: %s Hz, %s ch, 16-bit (device %r)",
                 fmt.rate, fmt.channels, lb["name"])
        return fmt

    # -- lifecycle --------------------------------------------------------

    def start(self) -> None:
        self._stop.clear()
        self._start_silence_keeper()
        self._open_stream()
        self._thread = threading.Thread(target=self._run, name="capture", daemon=True)
        self._thread.start()

    def _start_silence_keeper(self) -> None:
        """Render zeros to the default output continuously.

        WASAPI loopback delivers NO frames (the read blocks) unless something
        is rendering to the endpoint. A permanent zero-stream keeps the engine
        running so capture reads always progress - which also makes teardown
        safe (no thread stuck in a native read) and keeps capture-health
        meaningful. Inaudible by construction (all zeros).
        """
        self._keeper_thread = threading.Thread(
            target=self._silence_keeper, name="silence-keeper", daemon=True)
        self._keeper_thread.start()

    def _silence_keeper(self) -> None:
        while not self._stop.is_set():
            try:
                wasapi = self._pa.get_host_api_info_by_type(pyaudio.paWASAPI)
                out = self._pa.get_device_info_by_index(wasapi["defaultOutputDevice"])
                rate = int(out["defaultSampleRate"])
                ch = min(2, int(out["maxOutputChannels"])) or 2
                stream = self._pa.open(format=pyaudio.paInt16, channels=ch, rate=rate,
                                       output=True, output_device_index=out["index"],
                                       frames_per_buffer=FRAMES_PER_BUFFER)
                zeros = b"\x00" * (FRAMES_PER_BUFFER * ch * 2)
                while not self._stop.is_set():
                    stream.write(zeros)
                stream.stop_stream()
                stream.close()
            except OSError as e:
                log.warning("silence keeper error (device change?): %s - reopening", e)
                time.sleep(1.0)

    def _open_stream(self) -> None:
        lb = self._find_loopback()
        new_fmt = CaptureFormat(rate=int(lb["defaultSampleRate"]),
                                channels=min(2, int(lb["maxInputChannels"])) or 2,
                                sampwidth=2)
        if new_fmt != self.format:
            # A WAV header (HTTP path) and the 48 kHz Opus encoder (mirror path)
            # are both fixed at open time, so a mid-flight rate change needs a
            # session restart. The mirror watchdog polls capture.format via
            # mirror.eligible_format() and triggers a full restart; the HTTP
            # path currently tolerates it (one re-anchor).
            log.warning("capture format changed %s -> %s", self.format, new_fmt)
            self.format = new_fmt
        self._stream = self._pa.open(
            format=pyaudio.paInt16,
            channels=self.format.channels,
            rate=self.format.rate,
            input=True,
            input_device_index=lb["index"],
            frames_per_buffer=FRAMES_PER_BUFFER,
        )
        self.healthy = True

    def _run(self) -> None:
        consecutive_errors = 0
        while not self._stop.is_set():
            try:
                data = self._stream.read(FRAMES_PER_BUFFER, exception_on_overflow=False)
                consecutive_errors = 0
                if data:
                    self._on_data(data)
            except OSError as e:
                consecutive_errors += 1
                log.warning("capture read error (%d): %s", consecutive_errors, e)
                if consecutive_errors >= 5:
                    self.healthy = False
                    self._reopen()
                    consecutive_errors = 0

    def _reopen(self) -> None:
        """Device invalidated (sleep/resume, default-device change, driver)."""
        while not self._stop.is_set():
            try:
                if self._stream is not None:
                    try:
                        self._stream.stop_stream()
                        self._stream.close()
                    except OSError:
                        pass
                time.sleep(1.0)
                self._open_stream()
                self.restart_count += 1
                log.info("capture reopened (restart #%d)", self.restart_count)
                return
            except (OSError, RuntimeError) as e:
                log.warning("capture reopen failed, retrying: %s", e)
                time.sleep(2.0)

    def stop(self) -> None:
        self._stop.set()
        # The silence keeper guarantees reads progress, so the capture thread
        # sees _stop promptly. Only touch native handles after BOTH threads
        # are confirmed dead - closing under a live read is an access violation.
        threads_dead = True
        for t in (self._thread, getattr(self, "_keeper_thread", None)):
            if t is not None:
                t.join(timeout=3)
                threads_dead = threads_dead and not t.is_alive()
        if not threads_dead:
            log.warning("capture threads did not exit; leaking PortAudio handles "
                        "instead of risking a native crash")
            return
        if self._stream is not None:
            try:
                self._stream.stop_stream()
                self._stream.close()
            except OSError:
                pass
            self._stream = None
        self._pa.terminate()


class ProcessLoopbackCapture:
    """Whole-system capture before the endpoint volume/mute (decoupled). The
    capture MTA thread owns the ENTIRE COM lifecycle - CoUninitialize is
    thread-affine, so all COM teardown runs in the thread's own finally, and
    stop() only signals + joins."""

    couples_volume = False

    def __init__(self, on_data: Callable[[bytes], None], device_hint=None):
        self._on_data = on_data
        self.format = CaptureFormat(_pl.CAPTURE_RATE, _pl.CAPTURE_CHANNELS,
                                    _pl.CAPTURE_SAMPWIDTH)
        self.healthy = False
        self.restart_count = 0
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None
        self._ready = threading.Event()
        self._ready_ok = False
        self._ready_exc: Exception | None = None
        self._client = None
        self._capture = None
        self._evt = None
        self._com_started = False

    def start(self) -> None:
        self._stop.clear()
        self._ready.clear()
        self._ready_ok = False
        self._ready_exc = None
        self._thread = threading.Thread(target=self._run, name="proc-capture",
                                        daemon=True)
        self._thread.start()
        if not self._ready.wait(timeout=10):
            self._stop.set()
            raise TimeoutError("process-loopback capture did not start in 10 s")
        if not self._ready_ok:
            raise self._ready_exc or OSError("process-loopback capture failed")

    def _run(self) -> None:
        try:
            comtypes.CoInitializeEx(comtypes.COINIT_MULTITHREADED)
            self._com_started = True
            _pl.mf_startup()
            self._activate()
            self._ready_ok = True
        except Exception as e:
            # Re-raise a COPY on the caller thread, never the live exception:
            # its traceback pins this frame's locals (the COM interfaces), which
            # would then be released after CoUninitialize, on another thread.
            self._ready_exc = OSError(
                "process-loopback activation failed: %s: %s"
                % (type(e).__name__, e))
            self._ready.set()
            self._teardown_com()
            return
        self._ready.set()
        try:
            self._read_loop()
        except Exception:
            # Nothing above catches this, and teardown MUST still run on this
            # thread (CoUninitialize is thread-affine).
            log.exception("proc capture thread died")
        finally:
            if not self._stop.is_set():
                # Died on its own rather than being stopped: mark unhealthy so
                # the caster watchdog tears down and re-casts.
                self.healthy = False
            self._teardown_com()

    def _activate(self) -> None:
        self._client = _pl.activate_process_loopback(os.getpid())
        wfx = _pl.capture_waveformat()
        self._client.Initialize(
            _pl.AUDCLNT_SHAREMODE_SHARED,
            _pl.AUDCLNT_STREAMFLAGS_LOOPBACK | _pl.AUDCLNT_STREAMFLAGS_EVENTCALLBACK,
            2_000_000, 0, ctypes.byref(wfx), None)
        self._evt = _pl.create_event()
        self._client.SetEventHandle(self._evt)
        unk = self._client.GetService(ctypes.byref(_pl.IID_IAudioCaptureClient))
        self._capture = unk.QueryInterface(_pl.IAudioCaptureClient)
        self._client.Start()
        self.healthy = True

    def _read_loop(self) -> None:
        blk = self.format.frame_bytes
        errors = 0
        while not self._stop.is_set():
            _pl.wait_event(self._evt, 100)     # timeout lets us see _stop
            try:
                n = self._capture.GetNextPacketSize()
                while n:
                    data, frames, flags, _dp, _qp = self._capture.GetBuffer()
                    try:
                        if not (flags & _pl.AUDCLNT_BUFFERFLAGS_SILENT):
                            # one copy of the whole buffer while it is still
                            # ours; the pacer fills silence gaps from its clock
                            self._on_data(ctypes.string_at(data, frames * blk))
                    finally:
                        # Must always release, even if on_data raised, or the
                        # next GetBuffer fails AUDCLNT_E_OUT_OF_ORDER.
                        self._capture.ReleaseBuffer(frames)
                    n = self._capture.GetNextPacketSize()
                errors = 0
            except Exception as e:
                # Broad by necessity: comtypes raises COMError on a failed
                # HRESULT and COMError is NOT an OSError subclass, so the
                # failures this exists for (device invalidated on sleep/resume,
                # audio service restarted) would slip straight through a
                # narrower catch and kill the thread silently.
                errors += 1
                log.warning("proc capture read error (%d): %s", errors, e)
                if errors >= 5:
                    if self._reactivate():
                        errors = 0
                    else:
                        self.healthy = False
                        return

    def _reactivate(self) -> bool:
        """Windows audio service restarts etc.: re-activate on this thread."""
        self._release_stream()
        try:
            if self._stop.wait(1.0):      # settle, but abandon on stop()
                return False
            self._activate()
            self.restart_count += 1
            log.info("proc capture re-activated (restart #%d)", self.restart_count)
            return True
        except Exception as e:
            log.warning("proc capture re-activation failed: %s", e)
            return False

    def _release_stream(self) -> None:
        if self._client is not None:
            try:
                self._client.Stop()
            except Exception:
                pass
        self._capture = None      # comtypes Releases on this thread when dropped
        self._client = None
        if self._evt:
            _pl.close_event(self._evt)
            self._evt = None

    def _teardown_com(self) -> None:
        self._release_stream()
        if self._com_started:
            _pl.mf_shutdown()
            try:
                comtypes.CoUninitialize()
            except Exception:
                pass
            self._com_started = False

    def stop(self) -> None:
        self._stop.set()
        if self._thread is not None:
            # Long enough to outlast a reactivation in flight (1 s settle, which
            # stop() short-circuits, plus a 5 s activation timeout); a shorter
            # join would routinely hit the leak path below.
            self._thread.join(timeout=8)
            if self._thread.is_alive():
                log.warning("proc capture thread did not exit; leaking COM "
                            "instead of an off-thread teardown")


def open_capture(on_data: Callable[[bytes], None], device_hint: str | None = None):
    """Pick a capture backend, returning it already started (so its format is
    known to the caller). A pinned capture device (device_hint) forces the
    endpoint path, which honours it; otherwise use process loopback when
    available (decoupled from volume), falling back to endpoint. Any activation
    failure falls back rather than erroring the cast.

    The first call also pays for supported()'s trial activation, so process
    loopback is activated twice on the first cast of a session; both are cheap
    on a machine that supports it, and supported() caches thereafter."""
    if device_hint is None and _pl.supported():
        try:
            cap = ProcessLoopbackCapture(on_data)
            cap.start()
            log.info("using process-loopback capture (volume-decoupled)")
            return cap
        except Exception as e:
            log.warning("process-loopback capture failed (%s); using endpoint", e)
    cap = LoopbackCapture(on_data, device_hint)
    cap.start()
    return cap

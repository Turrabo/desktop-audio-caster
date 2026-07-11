"""WASAPI loopback capture with format probing and self-healing.

Delivers 16-bit PCM at the device's native rate/channels into a callback.
Health: consecutive read failures flip ``healthy`` False and trigger reopen
attempts; the caster's watchdog reads ``healthy`` and restart_count.
"""
from __future__ import annotations

import logging
import threading
import time
from dataclasses import dataclass
from typing import Callable

import pyaudiowpatch as pyaudio

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
            # A WAV stream's header cannot change mid-flight; the caster must
            # restart the media session. Surfaced via format_changed.
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

"""Audio I/O manager wrapping sounddevice for 16 kHz input and 24 kHz output."""

from __future__ import annotations

import logging
import queue
import threading
from collections.abc import Callable
from typing import Any

import numpy as np
import sounddevice as sd

from clea.surfaces.voice.backends.base import (
    AUDIO_CHANNELS,
    INPUT_AUDIO_SAMPLE_RATE_HZ,
    OUTPUT_AUDIO_SAMPLE_RATE_HZ,
)

logger = logging.getLogger(__name__)


def list_audio_devices() -> list[dict[str, Any]]:
    """Return all available host audio devices."""
    devices = []
    try:
        raw_devices = sd.query_devices()
        for idx, dev in enumerate(raw_devices):
            devices.append(
                {
                    "index": idx,
                    "name": dev["name"],
                    "max_input_channels": dev["max_input_channels"],
                    "max_output_channels": dev["max_output_channels"],
                    "default_samplerate": dev["default_samplerate"],
                }
            )
    except Exception as exc:
        logger.warning("Failed querying audio devices: %s", exc)
    return devices


class AudioManager:
    """Manages full-duplex microphone recording and audio playback with barge-in support."""

    def __init__(
        self,
        *,
        input_device: str | int | None = None,
        output_device: str | int | None = None,
        mic_mute_while_speaking: bool = False,
        on_audio_input: Callable[[bytes], None] | None = None,
    ) -> None:
        self.input_device = input_device
        self.output_device = output_device
        self.mic_mute_while_speaking = mic_mute_while_speaking
        self._on_audio_input = on_audio_input

        self._input_stream: sd.InputStream | None = None
        self._output_stream: sd.OutputStream | None = None
        self._output_queue: queue.Queue[bytes] = queue.Queue()
        self._current_output_bytes = bytearray()
        self._is_speaking = threading.Event()
        self._lock = threading.Lock()
        self._running = False

    @property
    def is_speaking(self) -> bool:
        """True when assistant audio is actively being played out."""
        return self._is_speaking.is_set()

    def set_input_callback(self, callback: Callable[[bytes], None] | None) -> None:
        """Set or update the microphone PCM data callback."""
        with self._lock:
            self._on_audio_input = callback

    def start(self) -> None:
        """Start microphone input and audio output streams."""
        with self._lock:
            if self._running:
                return

            self._current_output_bytes.clear()
            self._drain_output_queue()
            self._is_speaking.clear()

            try:
                self._input_stream = sd.InputStream(
                    samplerate=INPUT_AUDIO_SAMPLE_RATE_HZ,
                    channels=AUDIO_CHANNELS,
                    dtype="int16",
                    device=self.input_device,
                    callback=self._input_stream_callback,
                )
                self._output_stream = sd.OutputStream(
                    samplerate=OUTPUT_AUDIO_SAMPLE_RATE_HZ,
                    channels=AUDIO_CHANNELS,
                    dtype="int16",
                    device=self.output_device,
                    callback=self._output_stream_callback,
                )
                self._input_stream.start()
                self._output_stream.start()
                self._running = True
                logger.info(
                    "AudioManager started (input=%s, output=%s, mute_while_speaking=%s).",
                    self.input_device,
                    self.output_device,
                    self.mic_mute_while_speaking,
                )
            except Exception:
                self.stop()
                raise

    def stop(self) -> None:
        """Stop and close both audio streams."""
        with self._lock:
            self._running = False
            self.stop_output()

            if self._input_stream is not None:
                try:
                    self._input_stream.stop()
                    self._input_stream.close()
                except Exception as exc:
                    logger.debug("Error stopping input stream: %s", exc)
                self._input_stream = None

            if self._output_stream is not None:
                try:
                    self._output_stream.stop()
                    self._output_stream.close()
                except Exception as exc:
                    logger.debug("Error stopping output stream: %s", exc)
                self._output_stream = None

    def play_audio(self, pcm_bytes: bytes) -> None:
        """Queue 24 kHz 16-bit mono PCM bytes for playback."""
        if not pcm_bytes or not self._running:
            return
        self._output_queue.put(pcm_bytes)
        self._is_speaking.set()

    def stop_output(self) -> None:
        """Immediately drop all buffered output audio on barge-in."""
        self._drain_output_queue()
        with self._lock:
            self._current_output_bytes.clear()
            self._is_speaking.clear()
        logger.debug("AudioManager output buffer cleared (barge-in flush).")

    def _drain_output_queue(self) -> None:
        while True:
            try:
                self._output_queue.get_nowait()
            except queue.Empty:
                break

    def _input_stream_callback(
        self,
        indata: np.ndarray,
        frames: int,
        time_info: Any,
        status: sd.CallbackFlags,
    ) -> None:
        if status:
            logger.debug("Audio input status flag: %s", status)

        if self.mic_mute_while_speaking and self._is_speaking.is_set():
            # Mute microphone while assistant is talking
            return

        cb = self._on_audio_input
        if cb is not None:
            pcm_bytes = indata.tobytes()
            try:
                cb(pcm_bytes)
            except Exception:
                logger.exception("Error in audio input callback.")

    def _output_stream_callback(
        self,
        outdata: np.ndarray,
        frames: int,
        time_info: Any,
        status: sd.CallbackFlags,
    ) -> None:
        if status:
            logger.debug("Audio output status flag: %s", status)

        # 16-bit mono = 2 bytes per frame
        bytes_needed = frames * 2
        buf = bytearray()

        while len(buf) < bytes_needed:
            with self._lock:
                if self._current_output_bytes:
                    take = min(bytes_needed - len(buf), len(self._current_output_bytes))
                    buf.extend(self._current_output_bytes[:take])
                    del self._current_output_bytes[:take]
                    continue

            # Need more chunks from queue
            try:
                chunk = self._output_queue.get_nowait()
                with self._lock:
                    self._current_output_bytes.extend(chunk)
            except queue.Empty:
                break

        if buf:
            self._is_speaking.set()
            got_frames = len(buf) // 2
            arr = np.frombuffer(buf[: got_frames * 2], dtype=np.int16).reshape(-1, 1)
            outdata[:got_frames] = arr
            if got_frames < frames:
                outdata[got_frames:] = 0
                if self._output_queue.empty() and not self._current_output_bytes:
                    self._is_speaking.clear()
        else:
            outdata[:] = 0
            self._is_speaking.clear()

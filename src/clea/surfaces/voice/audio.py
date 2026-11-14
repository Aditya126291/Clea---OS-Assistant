"""Audio I/O manager wrapping sounddevice for 16 kHz input and 24 kHz output."""

from __future__ import annotations

import logging
import math
import queue
import threading
import time
from collections.abc import Callable
from typing import Any

import numpy as np
import scipy.signal
import sounddevice as sd

from clea.surfaces.voice.backends.base import (
    AUDIO_CHANNELS,
    INPUT_AUDIO_SAMPLE_RATE_HZ,
    OUTPUT_AUDIO_SAMPLE_RATE_HZ,
)

logger = logging.getLogger(__name__)


class AudioDeviceNotFoundError(ValueError):
    """Raised when a requested audio device does not exist or has no channels."""


class AmbiguousAudioDeviceError(ValueError):
    """Raised when an audio device name substring matches multiple candidate devices."""


def refresh_audio_devices() -> None:
    """Re-initialize PortAudio so newly woken OS audio endpoints are discovered."""
    try:
        if getattr(sd, "_initialized", 0) > 0 and hasattr(sd, "_terminate"):
            sd._terminate()
        if getattr(sd, "_initialized", 0) == 0 and hasattr(sd, "_initialize"):
            sd._initialize()
    except Exception as exc:
        logger.debug("Could not refresh PortAudio device list: %s", exc)


def list_audio_devices() -> list[dict[str, Any]]:
    """Return all available host audio devices enriched with host API information."""
    devices = []
    try:
        raw_devices = sd.query_devices()
        try:
            hostapis = sd.query_hostapis()
        except Exception:
            hostapis = ()
        for idx, dev in enumerate(raw_devices):
            host_idx = dev.get("hostapi")
            host_name = "Unknown"
            if (
                host_idx is not None
                and isinstance(hostapis, (list, tuple))
                and 0 <= host_idx < len(hostapis)
            ):
                host_name = hostapis[host_idx].get("name", "Unknown")
            devices.append(
                {
                    "index": idx,
                    "name": dev["name"],
                    "hostapi": host_idx,
                    "hostapi_name": host_name,
                    "max_input_channels": dev["max_input_channels"],
                    "max_output_channels": dev["max_output_channels"],
                    "default_samplerate": dev["default_samplerate"],
                }
            )
    except Exception as exc:
        logger.warning("Failed querying audio devices: %s", exc)
    return devices


def resolve_audio_device(
    spec: str | int | None,
    kind: str = "input",
    preferred_hostapi: str | None = "WASAPI",
) -> dict[str, Any]:
    """Resolve an audio device specifier (index, name substring, or None for default).

    Prefers `preferred_hostapi` (e.g. 'WASAPI') to disambiguate across duplicate Windows devices.
    Fails with AudioDeviceNotFoundError if not found, or AmbiguousAudioDeviceError if ambiguous.
    """
    devices = list_audio_devices()
    valid_devices = [
        d
        for d in devices
        if (d["max_input_channels"] > 0 if kind == "input" else d["max_output_channels"] > 0)
    ]

    if not valid_devices:
        raise AudioDeviceNotFoundError(f"No audio {kind} devices found on the system.")

    # 1. Default device (spec is None)
    if spec is None:
        if preferred_hostapi:
            try:
                hostapis = sd.query_hostapis()
                for api in hostapis:
                    if preferred_hostapi.lower() in api.get("name", "").lower():
                        pref_def = api.get(
                            "default_input_device" if kind == "input" else "default_output_device"
                        )
                        if pref_def is not None and pref_def >= 0:
                            match = next((d for d in valid_devices if d["index"] == pref_def), None)
                            if match is not None:
                                return match
            except Exception as exc:
                logger.debug("Could not query hostapis for preferred default: %s", exc)

        try:
            def_idx = sd.default.device[0 if kind == "input" else 1]
            if def_idx is not None and def_idx >= 0:
                match = next((d for d in valid_devices if d["index"] == def_idx), None)
                if match is not None:
                    return match
        except Exception as exc:
            logger.debug("Could not get sounddevice default device: %s", exc)
        return valid_devices[0]

    # 2. Integer index
    if isinstance(spec, int):
        match = next((d for d in valid_devices if d["index"] == spec), None)
        if match is None:
            available = [f"#{d['index']}: {d['name']} ({d['hostapi_name']})" for d in valid_devices]
            raise AudioDeviceNotFoundError(
                f"Audio {kind} device index {spec} does not exist or has no {kind} channels. "
                f"Available devices: {available}"
            )
        return match

    # 3. String name or numeric string
    query = spec.strip()
    if query.isdigit():
        return resolve_audio_device(int(query), kind=kind, preferred_hostapi=preferred_hostapi)

    matches = [d for d in valid_devices if query.lower() in d["name"].lower()]

    if not matches:
        available = [f"#{d['index']}: {d['name']} ({d['hostapi_name']})" for d in valid_devices]
        raise AudioDeviceNotFoundError(
            f"No audio {kind} device matching '{query}' found. Available devices: {available}"
        )

    if len(matches) > 1 and preferred_hostapi:
        pref_matches = [
            d for d in matches if preferred_hostapi.lower() in d["hostapi_name"].lower()
        ]
        if len(pref_matches) == 1:
            return pref_matches[0]
        if len(pref_matches) > 1:
            matches = pref_matches

    if len(matches) > 1:
        match_names = [f"#{d['index']}: {d['name']} ({d['hostapi_name']})" for d in matches]
        raise AmbiguousAudioDeviceError(
            f"Multiple audio {kind} devices match '{query}': {match_names}. "
            "Please specify a more specific name substring or device index."
        )

    return matches[0]


def negotiate_input_sample_rate(
    device_idx: int | None,
    default_samplerate: float | int | None = None,
    candidate_rates: tuple[int, ...] = (16000, 48000, 44100),
    preferred_channels: int = 1,
) -> tuple[int, int]:
    """Negotiate a supported hardware sample rate and channel count for audio input.

    Probes candidate rates starting with `default_samplerate`, followed by
    `candidate_rates`. Tests channels starting with `preferred_channels`, falling
    back across channels (1 then 2) if necessary.
    Returns (negotiated_sample_rate, negotiated_channels).
    """
    candidates: list[int] = []
    if default_samplerate is not None and int(default_samplerate) > 0:
        candidates.append(int(default_samplerate))
    for r in candidate_rates:
        if r not in candidates:
            candidates.append(r)

    # Alternate channel preference (e.g. 1 then 2, or 2 then 1)
    ch_candidates = (preferred_channels, 2 if preferred_channels == 1 else 1)

    for rate in candidates:
        for ch in ch_candidates:
            try:
                sd.check_input_settings(
                    device=device_idx,
                    samplerate=rate,
                    channels=ch,
                    dtype="int16",
                )
                return rate, ch
            except Exception:
                continue

    # Fallback default if probing raises exceptions on all candidates (e.g., test mocks)
    return int(default_samplerate or 16000), preferred_channels


def resample_to_16k_mono(
    audio: np.ndarray,
    orig_sr: int,
    target_sr: int = 16000,
) -> np.ndarray:
    """Downmix to mono and resample audio to target sample rate (default: 16000 Hz) as np.int16.

    Input audio can be a 1D or 2D array of numeric samples (int16, float32, etc.).
    Returns a 1D np.int16 array with values clipped to [-32768, 32767].
    """
    if audio.size == 0:
        return np.empty(0, dtype=np.int16)

    # Sanitize float inputs to guard against NaN/Inf warnings/failures
    if np.issubdtype(audio.dtype, np.floating):
        audio = np.nan_to_num(audio, nan=0.0, posinf=32767.0, neginf=-32768.0)

    # Downmix multi-channel to mono
    if audio.ndim > 1:
        audio = audio.mean(axis=1)
    else:
        audio = audio.reshape(-1)

    if orig_sr == target_sr:
        if audio.dtype == np.int16:
            return np.ascontiguousarray(audio)
        return np.clip(np.round(audio), -32768, 32767).astype(np.int16)

    g = math.gcd(orig_sr, target_sr)
    up = target_sr // g
    down = orig_sr // g

    # Polyphase rational resampler with float32 computation
    resampled = scipy.signal.resample_poly(audio.astype(np.float32), up, down)
    return np.clip(np.round(resampled), -32768, 32767).astype(np.int16)


class AudioChunkBuffer:
    """Thread-safe FIFO buffer accumulating 16 kHz int16 audio and yielding exact chunks."""

    def __init__(self, chunk_size: int = 1280) -> None:
        self.chunk_size = chunk_size
        self._buffer = bytearray()
        self._bytes_per_chunk = chunk_size * 2  # 16-bit PCM = 2 bytes/sample
        self._lock = threading.Lock()

    def extend(self, pcm_bytes: bytes | np.ndarray) -> None:
        """Append PCM audio data (bytes or int16 numpy array) to the FIFO buffer."""
        if isinstance(pcm_bytes, np.ndarray):
            raw = np.ascontiguousarray(pcm_bytes.reshape(-1), dtype=np.int16).tobytes()
        else:
            raw = pcm_bytes
        if not raw:
            return
        with self._lock:
            self._buffer.extend(raw)

    def get_chunks(self) -> list[np.ndarray]:
        """Pop all available non-overlapping 1280-sample 1D np.int16 chunks."""
        chunks: list[np.ndarray] = []
        with self._lock:
            while len(self._buffer) >= self._bytes_per_chunk:
                raw_chunk = bytes(self._buffer[: self._bytes_per_chunk])
                del self._buffer[: self._bytes_per_chunk]
                chunk_arr = np.frombuffer(raw_chunk, dtype=np.int16)
                chunks.append(chunk_arr)
        return chunks

    def clear(self) -> None:
        """Flush all pending samples from the buffer."""
        with self._lock:
            self._buffer.clear()


def compute_audio_rms(pcm_bytes: bytes | np.ndarray) -> float:
    """Calculate RMS energy of 16-bit mono PCM audio chunk."""
    if pcm_bytes is None:
        return 0.0
    if isinstance(pcm_bytes, np.ndarray):
        arr = pcm_bytes.reshape(-1)
    else:
        if not pcm_bytes:
            return 0.0
        arr = np.frombuffer(pcm_bytes, dtype=np.int16)
    if len(arr) == 0:
        return 0.0
    return float(np.sqrt(np.mean(arr.astype(np.float32) ** 2)))


class AudioManager:
    """Manages full-duplex microphone recording and audio playback with barge-in support."""

    def __init__(
        self,
        *,
        input_device: str | int | None = None,
        output_device: str | int | None = None,
        preferred_hostapi: str | None = "WASAPI",
        mic_mute_while_speaking: bool = False,
        on_audio_input: Callable[[bytes], None] | None = None,
        chunk_size: int | None = None,
    ) -> None:
        self.input_device = input_device
        self.output_device = output_device
        self.preferred_hostapi = preferred_hostapi
        self.mic_mute_while_speaking = mic_mute_while_speaking
        self._on_audio_input = on_audio_input
        self.chunk_size = chunk_size
        self._chunk_buffer: AudioChunkBuffer | None = (
            AudioChunkBuffer(chunk_size=chunk_size) if chunk_size else None
        )
        self.input_sample_rate: int = INPUT_AUDIO_SAMPLE_RATE_HZ
        self.input_channels: int = AUDIO_CHANNELS

        self.resolved_input_device: dict[str, Any] | None = None
        self.resolved_output_device: dict[str, Any] | None = None

        self._input_stream: sd.InputStream | None = None
        self._output_stream: sd.OutputStream | None = None
        self._output_queue: queue.Queue[bytes] = queue.Queue()
        self._current_output_bytes = bytearray()
        self._input_accumulator = bytearray()
        self._is_speaking = threading.Event()
        self._lock = threading.RLock()
        self._running = False

    @property
    def is_speaking(self) -> bool:
        """True when assistant audio is actively being played out."""
        return self._is_speaking.is_set()

    def set_input_callback(self, callback: Callable[[bytes], None] | None) -> None:
        """Set or update the microphone PCM data callback."""
        with self._lock:
            self._on_audio_input = callback

    def start(self, *, max_attempts: int = 1, retry_delay: float = 1.0) -> None:
        """Start microphone input and audio output streams, retrying transient endpoint failures."""
        attempts = max(1, max_attempts)
        for attempt in range(1, attempts + 1):
            try:
                self._start_once()
                return
            except AmbiguousAudioDeviceError:
                raise
            except (AudioDeviceNotFoundError, sd.PortAudioError, OSError) as exc:
                if attempt >= attempts:
                    raise
                logger.warning(
                    "Audio initialization failed (attempt %d/%d: %s); retrying in %.1fs...",
                    attempt,
                    attempts,
                    exc,
                    retry_delay,
                )
                if retry_delay > 0:
                    time.sleep(retry_delay)
                refresh_audio_devices()

    def _start_once(self) -> None:
        """Start microphone input and audio output streams once."""
        with self._lock:
            if self._running:
                return

            self._current_output_bytes.clear()
            self._input_accumulator.clear()
            self._drain_output_queue()
            self._is_speaking.clear()

            # Resolve input & output devices
            self.resolved_input_device = resolve_audio_device(
                self.input_device,
                kind="input",
                preferred_hostapi=self.preferred_hostapi,
            )
            self.resolved_output_device = resolve_audio_device(
                self.output_device,
                kind="output",
                preferred_hostapi=self.preferred_hostapi,
            )

            inp_idx = self.resolved_input_device["index"]
            out_idx = self.resolved_output_device["index"]

            logger.info(
                "Audio input device: #%d '%s' (host API: %s, native rate: %d Hz, channels: %d)",
                inp_idx,
                self.resolved_input_device["name"],
                self.resolved_input_device.get("hostapi_name", "Unknown"),
                int(
                    self.resolved_input_device.get("default_samplerate", INPUT_AUDIO_SAMPLE_RATE_HZ)
                ),
                self.resolved_input_device.get("max_input_channels", 1),
            )
            logger.info(
                "Audio output device: #%d '%s' (host API: %s, native rate: %d Hz, channels: %d)",
                out_idx,
                self.resolved_output_device["name"],
                self.resolved_output_device.get("hostapi_name", "Unknown"),
                int(
                    self.resolved_output_device.get(
                        "default_samplerate", OUTPUT_AUDIO_SAMPLE_RATE_HZ
                    )
                ),
                self.resolved_output_device.get("max_output_channels", 2),
            )

            # Negotiate hardware sample rate and channels
            native_rate = int(
                self.resolved_input_device.get("default_samplerate", INPUT_AUDIO_SAMPLE_RATE_HZ)
            )
            sample_rate, channels = negotiate_input_sample_rate(
                device_idx=inp_idx,
                default_samplerate=native_rate,
                candidate_rates=(16000, 48000, 44100),
                preferred_channels=1,
            )
            self.input_sample_rate = sample_rate
            self.input_channels = channels
            blocksize = int(sample_rate * 0.08)

            if self._chunk_buffer is None:
                self._chunk_buffer = AudioChunkBuffer(chunk_size=self.chunk_size or 1280)
            else:
                self._chunk_buffer.clear()

            inp_wasapi = "WASAPI" in self.resolved_input_device.get("hostapi_name", "").upper()
            out_wasapi = "WASAPI" in self.resolved_output_device.get("hostapi_name", "").upper()
            inp_extra = (
                sd.WasapiSettings(auto_convert=True)
                if (inp_wasapi and hasattr(sd, "WasapiSettings"))
                else None
            )
            out_extra = (
                sd.WasapiSettings(auto_convert=True)
                if (out_wasapi and hasattr(sd, "WasapiSettings"))
                else None
            )

            try:
                self._input_stream = sd.InputStream(
                    samplerate=sample_rate,
                    channels=channels,
                    dtype="int16",
                    device=inp_idx,
                    blocksize=blocksize,
                    extra_settings=inp_extra,
                    callback=self._input_stream_callback,
                )
                self._output_stream = sd.OutputStream(
                    samplerate=OUTPUT_AUDIO_SAMPLE_RATE_HZ,
                    channels=AUDIO_CHANNELS,
                    dtype="int16",
                    device=out_idx,
                    extra_settings=out_extra,
                    callback=self._output_stream_callback,
                )
                self._input_stream.start()
                self._output_stream.start()
                self._running = True
                logger.info(
                    "AudioManager started (input=#%d, output=#%d, mute_while_speaking=%s).",
                    inp_idx,
                    out_idx,
                    self.mic_mute_while_speaking,
                )
            except Exception:
                self.stop()
                raise

    def stop(self) -> None:
        """Stop and close both audio streams."""
        with self._lock:
            self._running = False
            inp = self._input_stream
            out = self._output_stream
            self._input_stream = None
            self._output_stream = None
            self._input_accumulator.clear()
            if self._chunk_buffer is not None:
                self._chunk_buffer.clear()

        self.stop_output()

        if inp is not None:
            try:
                inp.stop()
                inp.close()
            except Exception as exc:
                logger.debug("Error stopping input stream: %s", exc)

        if out is not None:
            try:
                out.stop()
                out.close()
            except Exception as exc:
                logger.debug("Error stopping output stream: %s", exc)

    def play_audio(self, pcm_bytes: bytes) -> None:
        """Queue 24 kHz 16-bit mono PCM bytes for playback."""
        if not pcm_bytes:
            return
        with self._lock:
            if not self._running:
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
            # Mute microphone while speaking; clear buffer to prevent stale stitching
            if self._chunk_buffer is not None:
                self._chunk_buffer.clear()
            return

        cb = self._on_audio_input
        if cb is None:
            return

        resampled_16k = resample_to_16k_mono(
            indata,
            orig_sr=getattr(self, "input_sample_rate", INPUT_AUDIO_SAMPLE_RATE_HZ),
            target_sr=INPUT_AUDIO_SAMPLE_RATE_HZ,
        )

        if self._chunk_buffer is not None:
            self._chunk_buffer.extend(resampled_16k)
            for chunk in self._chunk_buffer.get_chunks():
                try:
                    cb(chunk.tobytes())
                except Exception:
                    logger.exception("Error in audio input callback.")
        else:
            try:
                cb(resampled_16k.tobytes())
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
                with self._lock:
                    if self._output_queue.empty() and not self._current_output_bytes:
                        self._is_speaking.clear()
        else:
            outdata[:] = 0
            with self._lock:
                if self._output_queue.empty() and not self._current_output_bytes:
                    self._is_speaking.clear()

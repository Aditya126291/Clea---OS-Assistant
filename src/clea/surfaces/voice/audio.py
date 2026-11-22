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


_VIRTUAL_LOOPBACK_INPUT_PATTERNS: tuple[str, ...] = (
    "stereo mix",
    "pc speaker",
    "what u hear",
    "wave out",
    "loopback",
    "cable output",
    "vb-audio",
    "virtual audio",
)

_OS_DEFAULT_ALIAS_INPUT_PATTERNS: tuple[str, ...] = (
    "microsoft sound mapper",
    "primary sound capture driver",
)

_BUILTIN_MIC_PATTERNS: tuple[str, ...] = (
    "microphone array",
    "internal microphone",
    "built-in",
    "realtek",
    "intel smart sound",
)

_SUSPECT_SILENT_MIC_PATTERNS: tuple[str, ...] = (
    "headset",
    "hands-free",
    "hands free",
    "hfp",
    "ag audio",
    "bluetooth",
    "earphone",
    "airpods",
    "buds",
    "tws",
    "virtual",
    "voicemeeter",
    "nvidia broadcast",
    "cable",
    "stereo mix",
    "pc speaker",
    "microsoft sound mapper",
    "primary sound capture",
)


def _is_virtual_loopback_input(name: str) -> bool:
    low = name.strip().lower()
    return any(pat in low for pat in _VIRTUAL_LOOPBACK_INPUT_PATTERNS)


def _is_os_default_alias_input(name: str) -> bool:
    low = name.strip().lower()
    return any(pat in low for pat in _OS_DEFAULT_ALIAS_INPUT_PATTERNS)


def _is_builtin_mic_input(name: str) -> bool:
    low = name.strip().lower()
    return any(pat in low for pat in _BUILTIN_MIC_PATTERNS)


def _is_suspect_silent_input_device(
    dev: dict[str, Any] | None,
    *,
    opened_via_fallback: bool = False,
) -> bool:
    """Return True for fallback, Bluetooth HFP, or virtual devices prone to near-zero RMS hangs.

    Built-in WASAPI hardware microphones (e.g. Realtek Microphone Array) are exempt from
    near-zero RMS failover even when opened via fallback, because hardware noise suppression
    in a quiet room legitimately drops idle RMS below 1.0 (bit-exact all-zero streams are
    still caught separately by ``max_zero_callbacks``).
    """
    if dev:
        name_low = str(dev.get("name", "")).strip().lower()
        host_upper = str(dev.get("hostapi_name", "")).upper()
        if (
            _is_builtin_mic_input(name_low)
            and "WASAPI" in host_upper
            and not _is_virtual_loopback_input(name_low)
            and not _is_os_default_alias_input(name_low)
        ):
            return False
        if opened_via_fallback:
            return True
        return any(pat in name_low for pat in _SUSPECT_SILENT_MIC_PATTERNS)
    return bool(opened_via_fallback)


def list_candidate_audio_devices(
    spec: str | int | None,
    kind: str = "input",
    preferred_hostapi: str | None = "WASAPI",
    *,
    demoted_device_names: set[str] | frozenset[str] | None = None,
    demoted_device_indices: set[int] | frozenset[int] | None = None,
) -> list[dict[str, Any]]:
    """Return an ordered list of candidate audio devices to try opening.

    Always starts with the primary device chosen by :func:`resolve_audio_device`
    (preserving ``AudioDeviceNotFoundError`` / ``AmbiguousAudioDeviceError``),
    unless that device is currently demoted due to sustained silent/dead audio.
    When *spec* is ``None`` (system default), appends same-name devices on other
    host APIs (e.g. DirectSound / MME when a Bluetooth WASAPI endpoint fails
    with PaErrorCode -9996), followed by active built-in microphones and
    remaining valid devices ordered by host API preference, placing virtual/loopback
    capture endpoints and demoted silent devices last.
    """
    primary = resolve_audio_device(spec, kind=kind, preferred_hostapi=preferred_hostapi)
    candidates: list[dict[str, Any]] = []
    seen_indices: set[int] = set()

    # Explicit integer index -> honor the user's exact index without silent redirection
    if isinstance(spec, int) or (isinstance(spec, str) and spec.strip().isdigit()):
        return [primary]

    devices = list_audio_devices()
    valid_devices = [
        d
        for d in devices
        if (d["max_input_channels"] > 0 if kind == "input" else d["max_output_channels"] > 0)
    ]

    demoted_names_low = {n.strip().lower() for n in (demoted_device_names or ()) if n}
    demoted_idxs = {int(i) for i in (demoted_device_indices or ())}

    def _is_demoted_name(name_low: str) -> bool:
        if not name_low:
            return False
        if name_low in demoted_names_low:
            return True
        # Windows MME truncates device names at 31 chars (MAXPNAMELEN - 1); match long prefixes
        # so demoting a WASAPI/DirectSound Bluetooth device also demotes its truncated MME entry.
        if len(name_low) >= 20:
            for d_name in demoted_names_low:
                if len(d_name) >= 20 and (
                    d_name.startswith(name_low) or name_low.startswith(d_name)
                ):
                    return True
        return False

    def _is_demoted(dev: dict[str, Any], *, primary_was_demoted: bool = False) -> bool:
        if int(dev["index"]) in demoted_idxs:
            return True
        name_low = str(dev.get("name", "")).strip().lower()
        if _is_demoted_name(name_low):
            return True
        # When the OS default input device is demoted as silent, OS-default alias drivers
        # ('Microsoft Sound Mapper - Input', 'Primary Sound Capture Driver') point to that
        # exact same dead default device and must also be demoted.
        if primary_was_demoted and _is_os_default_alias_input(name_low):
            return True
        return False

    def _host_rank(dev: dict[str, Any]) -> tuple[int, int, int]:
        api = str(dev.get("hostapi_name", "")).upper()
        if preferred_hostapi and preferred_hostapi.upper() in api:
            api_rank = 0
        elif "DIRECTSOUND" in api:
            api_rank = 1
        elif "MME" in api:
            api_rank = 2
        elif "WASAPI" in api:
            api_rank = 3
        else:
            api_rank = 4
        if kind == "input":
            name = str(dev.get("name", ""))
            virtual_rank = (
                1 if (_is_virtual_loopback_input(name) or _is_os_default_alias_input(name)) else 0
            )
            builtin_rank = 0 if _is_builtin_mic_input(name) else 1
            return (virtual_rank, api_rank, builtin_rank)
        return (0, api_rank, 0)

    def _add(dev: dict[str, Any]) -> None:
        idx = int(dev["index"])
        if idx not in seen_indices:
            seen_indices.add(idx)
            candidates.append(dev)

    primary_name_lower = str(primary.get("name", "")).strip().lower()
    primary_demoted = kind == "input" and spec is None and _is_demoted(primary)

    if not primary_demoted:
        _add(primary)

        # 1. Same device name across alternate host APIs (e.g. DirectSound / MME fallback)
        same_name = sorted(
            [
                d
                for d in valid_devices
                if str(d.get("name", "")).strip().lower() == primary_name_lower
            ],
            key=_host_rank,
        )
        for dev in same_name:
            _add(dev)

    # Explicit string query -> also include other host-API matches for that substring
    if isinstance(spec, str) and spec.strip():
        q = spec.strip().lower()
        name_matches = sorted(
            [d for d in valid_devices if q in str(d.get("name", "")).lower()],
            key=_host_rank,
        )
        for dev in name_matches:
            _add(dev)
        return candidates

    # 2. System default device from PortAudio (often MME/DirectSound), if not demoted
    try:
        def_idx = sd.default.device[0 if kind == "input" else 1]
        if def_idx is not None and def_idx >= 0:
            match = next((d for d in valid_devices if d["index"] == def_idx), None)
            if match is not None and not (
                kind == "input" and _is_demoted(match, primary_was_demoted=primary_demoted)
            ):
                _add(match)
    except Exception:
        pass

    # 3. Remaining non-demoted valid devices ordered by quality & host API preference
    non_demoted = [
        d
        for d in valid_devices
        if not (kind == "input" and _is_demoted(d, primary_was_demoted=primary_demoted))
    ]
    for dev in sorted(non_demoted, key=_host_rank):
        _add(dev)

    # 4. Demoted devices last as a final fallback so we never crash if all others fail
    demoted_remaining = [
        d
        for d in valid_devices
        if kind == "input" and _is_demoted(d, primary_was_demoted=primary_demoted)
    ]
    for dev in sorted(demoted_remaining, key=_host_rank):
        _add(dev)

    return candidates


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
    Normalized floating-point PCM in ``[-1.0, 1.0]`` is scaled to full-scale 16-bit signed PCM
    before resampling, while already-scaled float arrays (e.g. ``[-32768, 32767]``) are preserved.
    Returns a contiguous 1D np.int16 array with values clipped to [-32768, 32767].
    """
    if audio.size == 0:
        return np.empty(0, dtype=np.int16)

    # Check floating-point input BEFORE downmix (int16.mean(axis=1) becomes float64 in int16 scale)
    if np.issubdtype(audio.dtype, np.floating):
        finite_mask = np.isfinite(audio)
        is_normalized = False
        if np.any(finite_mask):
            max_abs = float(np.max(np.abs(audio[finite_mask])))
            if 0.0 < max_abs <= 2.0:
                is_normalized = True
        if is_normalized:
            audio = np.nan_to_num(audio, nan=0.0, posinf=1.0, neginf=-1.0)
            audio = audio.astype(np.float32, copy=False) * 32767.0
        else:
            audio = np.nan_to_num(audio, nan=0.0, posinf=32767.0, neginf=-32768.0)

    # Downmix multi-channel to mono
    if audio.ndim > 1:
        if audio.shape[1] == 1:
            audio = audio[:, 0]
        else:
            audio = audio.mean(axis=1)
    else:
        audio = audio.reshape(-1)

    if orig_sr == target_sr:
        if audio.dtype == np.int16:
            return np.ascontiguousarray(audio)
        return np.ascontiguousarray(np.clip(np.round(audio), -32768, 32767).astype(np.int16))

    g = math.gcd(orig_sr, target_sr)
    up = target_sr // g
    down = orig_sr // g

    # Polyphase rational resampler with float32 computation
    resampled = scipy.signal.resample_poly(audio.astype(np.float32), up, down)
    return np.ascontiguousarray(np.clip(np.round(resampled), -32768, 32767).astype(np.int16))


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
        self._last_input_callback_time: float = 0.0
        self._input_callback_count: int = 0
        self._consecutive_zero_callbacks: int = 0
        self._consecutive_silent_callbacks: int = 0
        self._silent_rms_threshold: float = 3.0
        self._last_input_rms: float = 0.0
        self._peak_input_rms: float = 0.0
        self._input_status_error_count: int = 0
        self._health_monitored: bool = False
        self._opened_via_fallback: bool = False
        self._demoted_input_names: dict[str, float] = {}
        self._demoted_input_indices: dict[int, float] = {}

    @property
    def is_speaking(self) -> bool:
        """True when assistant audio is actively being played out."""
        return self._is_speaking.is_set()

    def input_rms_telemetry(self) -> dict[str, Any]:
        """Return snapshot of active input device and live RMS telemetry."""
        with self._lock:
            dev = dict(self.resolved_input_device) if self.resolved_input_device else None
            return {
                "device": dev,
                "sample_rate": self.input_sample_rate,
                "channels": self.input_channels,
                "last_rms": round(self._last_input_rms, 2),
                "peak_rms": round(self._peak_input_rms, 2),
                "callback_count": self._input_callback_count,
                "consecutive_zero_callbacks": self._consecutive_zero_callbacks,
                "consecutive_silent_callbacks": self._consecutive_silent_callbacks,
            }

    def _active_demoted_devices(self) -> tuple[set[str], set[int]]:
        """Prune expired demotions and return currently demoted device names and indices."""
        now = time.monotonic()
        self._demoted_input_names = {
            k: exp for k, exp in self._demoted_input_names.items() if exp > now
        }
        self._demoted_input_indices = {
            k: exp for k, exp in self._demoted_input_indices.items() if exp > now
        }
        return set(self._demoted_input_names), set(self._demoted_input_indices)

    def demote_current_input_device(self, *, ttl_seconds: float = 120.0) -> None:
        """Temporarily demote the currently opened silent/dead input device for failover."""
        with self._lock:
            dev = self.resolved_input_device
            if not dev:
                return
            expiry = time.monotonic() + max(1.0, float(ttl_seconds))
            name_low = str(dev.get("name", "")).strip().lower()
            if name_low:
                self._demoted_input_names[name_low] = expiry
            if "index" in dev:
                self._demoted_input_indices[int(dev["index"])] = expiry

    def set_input_callback(self, callback: Callable[[bytes], None] | None) -> None:
        """Set or update the microphone PCM data callback."""
        with self._lock:
            self._on_audio_input = callback

    def flush_input_buffer(self) -> None:
        """Flush buffered microphone samples (e.g. when returning to wake-word IDLE)."""
        with self._lock:
            self._input_accumulator.clear()
            if self._chunk_buffer is not None:
                self._chunk_buffer.clear()

    def is_input_stream_healthy(
        self,
        *,
        max_callback_gap_seconds: float = 5.0,
        max_zero_callbacks: int = 125,
        max_silent_callbacks: int = 75,
    ) -> tuple[bool, str]:
        """Check whether the microphone InputStream is alive and delivering real frames.

        Returns ``(True, "ok")`` when healthy, or ``(False, reason)`` if the stream
        stopped, callbacks stalled (WASAPI/Bluetooth hang), sustained bit-exact
        all-zero buffers occur, or sustained near-zero RMS frames indicate a silent
        or inactive endpoint (e.g., idle Bluetooth HFP over DirectSound).
        """
        with self._lock:
            if not self._running or self._input_stream is None:
                return False, "input_stream_not_running"
            inp = self._input_stream
            last_cb = self._last_input_callback_time
            zero_streak = self._consecutive_zero_callbacks
            silent_streak = self._consecutive_silent_callbacks
            last_rms = self._last_input_rms
            active_dev = self.resolved_input_device
            via_fallback = self._opened_via_fallback

        try:
            if getattr(inp, "stopped", False) is True or getattr(inp, "active", True) is False:
                return False, "input_stream_inactive"
        except Exception as exc:
            return False, f"input_stream_status_error:{exc}"

        if last_cb > 0.0:
            gap = time.monotonic() - last_cb
            if gap > max_callback_gap_seconds:
                return False, f"input_callback_stalled_{gap:.1f}s"

        if zero_streak >= max_zero_callbacks:
            return False, f"sustained_zero_audio_{zero_streak}_blocks"

        if silent_streak >= max_silent_callbacks and _is_suspect_silent_input_device(
            active_dev, opened_via_fallback=via_fallback
        ):
            return False, f"sustained_silent_audio_{silent_streak}_blocks_rms_{last_rms:.2f}"

        return True, "ok"

    def restart_streams(
        self,
        *,
        max_attempts: int = 3,
        retry_delay: float = 0.5,
        demote_current: bool = False,
        demote_ttl_seconds: float = 120.0,
    ) -> bool:
        """Stop streams, refresh PortAudio endpoints, and reopen audio I/O safely.

        When *demote_current* is True and no explicit input device is pinned, the
        previously opened silent/dead input device (and same-name host-API copies)
        is demoted so failover switches to an active built-in/default microphone.
        """
        try:
            if demote_current and self.input_device is None and self.resolved_input_device:
                prev = self.resolved_input_device
                logger.warning(
                    "Demoting silent/unhealthy audio input device #%s '%s' (%s, last_rms=%.2f, "
                    "peak_rms=%.2f) for %.0fs to fail over to an active microphone.",
                    prev.get("index", "?"),
                    prev.get("name", "unknown"),
                    prev.get("hostapi_name", "Unknown"),
                    self._last_input_rms,
                    self._peak_input_rms,
                    demote_ttl_seconds,
                )
                self.demote_current_input_device(ttl_seconds=demote_ttl_seconds)
            self.stop()
            refresh_audio_devices()
            self.start(max_attempts=max_attempts, retry_delay=retry_delay)
            return True
        except Exception as exc:
            logger.warning("AudioManager stream recovery failed: %s", exc)
            return False

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

    def _open_input_stream(
        self,
        candidates: list[dict[str, Any]],
    ) -> tuple[sd.InputStream, dict[str, Any], int, int]:
        """Try opening and starting an InputStream across ordered candidate devices."""
        last_exc: Exception | None = None
        for idx_pos, dev in enumerate(candidates):
            inp_idx = int(dev["index"])
            native_rate = int(dev.get("default_samplerate", INPUT_AUDIO_SAMPLE_RATE_HZ))
            host_name = str(dev.get("hostapi_name", "Unknown"))
            logger.info(
                "Audio input device: #%d '%s' (host API: %s, native rate: %d Hz, channels: %d)",
                inp_idx,
                dev["name"],
                host_name,
                native_rate,
                dev.get("max_input_channels", 1),
            )
            sample_rate, channels = negotiate_input_sample_rate(
                device_idx=inp_idx,
                default_samplerate=native_rate,
                candidate_rates=(16000, 48000, 44100),
                preferred_channels=1,
            )
            blocksize = int(sample_rate * 0.08)
            inp_wasapi = "WASAPI" in host_name.upper()
            extra_options: list[Any] = (
                [sd.WasapiSettings(auto_convert=True), None]
                if (inp_wasapi and hasattr(sd, "WasapiSettings"))
                else [None]
            )

            for extra in extra_options:
                stream: sd.InputStream | None = None
                try:
                    # Set negotiated sample rate & channels BEFORE stream.start() so the very
                    # first audio callback resamples accurately without a startup race.
                    self.input_sample_rate = sample_rate
                    self.input_channels = channels
                    stream = sd.InputStream(
                        samplerate=sample_rate,
                        channels=channels,
                        dtype="int16",
                        device=inp_idx,
                        blocksize=blocksize,
                        extra_settings=extra,
                        callback=self._input_stream_callback,
                    )
                    stream.start()
                    self._opened_via_fallback = idx_pos > 0
                    if idx_pos > 0:
                        logger.warning(
                            "Audio input fell back to #%d '%s' (host API: %s, %d Hz, %dch).",
                            inp_idx,
                            dev["name"],
                            host_name,
                            sample_rate,
                            channels,
                        )
                    return stream, dev, sample_rate, channels
                except (sd.PortAudioError, OSError) as exc:
                    last_exc = exc
                    if stream is not None:
                        try:
                            stream.stop()
                            stream.close()
                        except Exception:
                            pass
                    if extra is not None:
                        logger.debug(
                            "Input device #%d '%s' failed with WASAPI auto_convert (%s); "
                            "retrying without extra_settings.",
                            inp_idx,
                            dev["name"],
                            exc,
                        )
                        continue
                    logger.warning(
                        "Failed opening audio input device #%d '%s' (%s): %s%s",
                        inp_idx,
                        dev["name"],
                        host_name,
                        exc,
                        "; trying fallback device..." if idx_pos + 1 < len(candidates) else ".",
                    )
                    break

        assert last_exc is not None
        raise last_exc

    def _open_output_stream(
        self,
        candidates: list[dict[str, Any]],
    ) -> tuple[sd.OutputStream, dict[str, Any]]:
        """Try opening and starting an OutputStream across ordered candidate devices."""
        last_exc: Exception | None = None
        for idx_pos, dev in enumerate(candidates):
            out_idx = int(dev["index"])
            host_name = str(dev.get("hostapi_name", "Unknown"))
            logger.info(
                "Audio output device: #%d '%s' (host API: %s, native rate: %d Hz, channels: %d)",
                out_idx,
                dev["name"],
                host_name,
                int(dev.get("default_samplerate", OUTPUT_AUDIO_SAMPLE_RATE_HZ)),
                dev.get("max_output_channels", 2),
            )
            out_wasapi = "WASAPI" in host_name.upper()
            extra_options: list[Any] = (
                [sd.WasapiSettings(auto_convert=True), None]
                if (out_wasapi and hasattr(sd, "WasapiSettings"))
                else [None]
            )
            for extra in extra_options:
                stream: sd.OutputStream | None = None
                try:
                    stream = sd.OutputStream(
                        samplerate=OUTPUT_AUDIO_SAMPLE_RATE_HZ,
                        channels=AUDIO_CHANNELS,
                        dtype="int16",
                        device=out_idx,
                        extra_settings=extra,
                        callback=self._output_stream_callback,
                    )
                    stream.start()
                    if idx_pos > 0:
                        logger.warning(
                            "Audio output fell back to #%d '%s' (host API: %s).",
                            out_idx,
                            dev["name"],
                            host_name,
                        )
                    return stream, dev
                except (sd.PortAudioError, OSError) as exc:
                    last_exc = exc
                    if stream is not None:
                        try:
                            stream.stop()
                            stream.close()
                        except Exception:
                            pass
                    if extra is not None:
                        continue
                    logger.warning(
                        "Failed opening audio output device #%d '%s' (%s): %s%s",
                        out_idx,
                        dev["name"],
                        host_name,
                        exc,
                        "; trying fallback device..." if idx_pos + 1 < len(candidates) else ".",
                    )
                    break

        assert last_exc is not None
        raise last_exc

    def _start_once(self) -> None:
        """Start microphone input and audio output streams once (with device fallback)."""
        with self._lock:
            if self._running:
                return

            self._current_output_bytes.clear()
            self._input_accumulator.clear()
            self._drain_output_queue()
            self._is_speaking.clear()
            self._consecutive_zero_callbacks = 0
            self._consecutive_silent_callbacks = 0
            self._last_input_rms = 0.0
            self._peak_input_rms = 0.0
            self._input_callback_count = 0
            self._input_status_error_count = 0

            demoted_names, demoted_indices = self._active_demoted_devices()

            # Resolve ordered candidate input & output devices (raises on invalid/ambiguous spec)
            input_candidates = list_candidate_audio_devices(
                self.input_device,
                kind="input",
                preferred_hostapi=self.preferred_hostapi,
                demoted_device_names=demoted_names,
                demoted_device_indices=demoted_indices,
            )
            output_candidates = list_candidate_audio_devices(
                self.output_device,
                kind="output",
                preferred_hostapi=self.preferred_hostapi,
            )

            if self._chunk_buffer is None:
                self._chunk_buffer = AudioChunkBuffer(chunk_size=self.chunk_size or 1280)
            else:
                self._chunk_buffer.clear()

            try:
                inp_stream, chosen_in, sample_rate, channels = self._open_input_stream(
                    input_candidates
                )
                self._input_stream = inp_stream
                self.resolved_input_device = chosen_in
                self.input_sample_rate = sample_rate
                self.input_channels = channels

                out_stream, chosen_out = self._open_output_stream(output_candidates)
                self._output_stream = out_stream
                self.resolved_output_device = chosen_out

                self._last_input_callback_time = time.monotonic()
                self._running = True
                self._health_monitored = True
                logger.info(
                    "AudioManager started (input=#%d '%s' [%s, %d Hz, %dch], output=#%d '%s', "
                    "mute_while_speaking=%s).",
                    chosen_in["index"],
                    chosen_in.get("name", "unknown"),
                    chosen_in.get("hostapi_name", "Unknown"),
                    sample_rate,
                    channels,
                    chosen_out["index"],
                    chosen_out.get("name", "unknown"),
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
        with self._lock:
            self._drain_output_queue()
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
        self._last_input_callback_time = time.monotonic()
        self._input_callback_count += 1
        if status:
            self._input_status_error_count += 1
            if self._input_status_error_count <= 3 or self._input_status_error_count % 50 == 0:
                logger.warning(
                    "Audio input status flag (%d total): %s",
                    self._input_status_error_count,
                    status,
                )
            else:
                logger.debug("Audio input status flag: %s", status)

        if self.mic_mute_while_speaking and self._is_speaking.is_set():
            # Mute microphone while speaking; clear buffer to prevent stale stitching
            self._consecutive_zero_callbacks = 0
            self._consecutive_silent_callbacks = 0
            if self._chunk_buffer is not None:
                self._chunk_buffer.clear()
            return

        if indata.size > 0 and not np.any(indata):
            self._consecutive_zero_callbacks += 1
        else:
            self._consecutive_zero_callbacks = 0

        resampled_16k = resample_to_16k_mono(
            indata,
            orig_sr=getattr(self, "input_sample_rate", INPUT_AUDIO_SAMPLE_RATE_HZ),
            target_sr=INPUT_AUDIO_SAMPLE_RATE_HZ,
        )

        rms = compute_audio_rms(resampled_16k)
        self._last_input_rms = rms
        if rms > self._peak_input_rms:
            self._peak_input_rms = rms
        if resampled_16k.size > 0 and rms < self._silent_rms_threshold:
            self._consecutive_silent_callbacks += 1
        else:
            self._consecutive_silent_callbacks = 0

        cb = self._on_audio_input
        if cb is None:
            return

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

                # Need more chunks from queue (read under lock so stop_output stays atomic)
                try:
                    chunk = self._output_queue.get_nowait()
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

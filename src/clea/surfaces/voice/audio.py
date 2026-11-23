"""Audio I/O manager wrapping sounddevice for 16 kHz input and 24 kHz output."""

from __future__ import annotations

import ctypes
import logging
import math
import queue
import sys
import threading
import time
import uuid
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


def _is_virtual_loopback_input(name: str) -> bool:
    low = name.strip().lower()
    return any(pat in low for pat in _VIRTUAL_LOOPBACK_INPUT_PATTERNS)


def _is_os_default_alias_input(name: str) -> bool:
    low = name.strip().lower()
    return any(pat in low for pat in _OS_DEFAULT_ALIAS_INPUT_PATTERNS)


def _is_builtin_mic_input(name: str) -> bool:
    low = name.strip().lower()
    return any(pat in low for pat in _BUILTIN_MIC_PATTERNS)


def _is_same_audio_device_name(name_a: str, name_b: str) -> bool:
    """Return True when two host-API device names refer to the same physical endpoint.

    Handles Windows MME 31-character truncation (``MAXPNAMELEN - 1``) by matching
    long shared prefixes (>= 20 chars).
    """
    a = name_a.strip().lower()
    b = name_b.strip().lower()
    if not a or not b:
        return False
    if a == b:
        return True
    return len(a) >= 20 and len(b) >= 20 and (a.startswith(b) or b.startswith(a))


if sys.platform == "win32":
    from ctypes import POINTER as _CT_POINTER
    from ctypes import byref as _ct_byref
    from ctypes import c_int as _ct_int
    from ctypes import c_long as _ct_long
    from ctypes import c_void_p as _ct_void_p
    from ctypes import wintypes as _ct_wintypes

    class _WinGUID(ctypes.Structure):
        _fields_ = [
            ("Data1", _ct_wintypes.DWORD),
            ("Data2", _ct_wintypes.WORD),
            ("Data3", _ct_wintypes.WORD),
            ("Data4", ctypes.c_ubyte * 8),
        ]

        @classmethod
        def from_str(cls, text: str) -> _WinGUID:
            parsed = uuid.UUID(text)
            guid = cls()
            guid.Data1 = parsed.fields[0]
            guid.Data2 = parsed.fields[1]
            guid.Data3 = parsed.fields[2]
            for i, byte_val in enumerate(parsed.bytes[8:]):
                guid.Data4[i] = byte_val
            return guid

    _CLSID_MMDEVICE_ENUMERATOR = _WinGUID.from_str("{BCDE0395-E52F-467C-8E3D-C4579291692E}")
    _IID_IMMDEVICE_ENUMERATOR = _WinGUID.from_str("{A95664D2-9614-4F35-A746-DE8DB63617E6}")
    _OLE32_PRIVATE_LOCK = threading.Lock()
    _OLE32_PRIVATE_DLL: Any = None

    def _get_private_ole32() -> Any:
        """Return a private ole32 WinDLL handle (never mutating global ctypes.windll.ole32)."""
        global _OLE32_PRIVATE_DLL
        if _OLE32_PRIVATE_DLL is not None:
            return _OLE32_PRIVATE_DLL
        with _OLE32_PRIVATE_LOCK:
            if _OLE32_PRIVATE_DLL is None:
                dll = ctypes.WinDLL("ole32")
                dll.CoInitializeEx.argtypes = [_ct_void_p, _ct_wintypes.DWORD]
                dll.CoInitializeEx.restype = _ct_long
                dll.CoUninitialize.argtypes = []
                dll.CoUninitialize.restype = None
                dll.CoCreateInstance.argtypes = [
                    _CT_POINTER(_WinGUID),
                    _ct_void_p,
                    _ct_wintypes.DWORD,
                    _CT_POINTER(_WinGUID),
                    _CT_POINTER(_ct_void_p),
                ]
                dll.CoCreateInstance.restype = _ct_long
                dll.CoTaskMemFree.argtypes = [_ct_void_p]
                dll.CoTaskMemFree.restype = None
                _OLE32_PRIVATE_DLL = dll
            return _OLE32_PRIVATE_DLL


def get_os_default_audio_endpoint_ids() -> tuple[str | None, str | None]:
    """Return ``(default_capture_id, default_render_id)`` from Windows Core Audio (read-only).

    Queries ``IMMDeviceEnumerator::GetDefaultAudioEndpoint`` for ``eConsole`` without
    reinitializing PortAudio or disrupting active audio streams. Uses a private
    ``ole32`` handle with module-level GUID types so global ``ctypes.windll.ole32``
    is never mutated. Returns ``(None, None)`` on non-Windows platforms or if Core
    Audio is unavailable.
    """
    if sys.platform != "win32":
        return None, None

    try:
        ole32 = _get_private_ole32()
        hr_init = int(ole32.CoInitializeEx(None, 0))  # COINIT_MULTITHREADED
        rpc_e_changed_mode = -2147417850  # 0x80010106
        should_uninit = hr_init in (0, 1)
        if not should_uninit and hr_init != rpc_e_changed_mode:
            return None, None

        enum_ptr = _ct_void_p()
        try:
            hr_create = int(
                ole32.CoCreateInstance(
                    _ct_byref(_CLSID_MMDEVICE_ENUMERATOR),
                    None,
                    23,  # CLSCTX_ALL
                    _ct_byref(_IID_IMMDEVICE_ENUMERATOR),
                    _ct_byref(enum_ptr),
                )
            )
            if hr_create != 0 or not enum_ptr.value:
                return None, None

            def _vtable_fn(ptr: _ct_void_p, index: int, restype: Any, *argtypes: Any) -> Any:
                vtbl = ctypes.cast(ptr, _CT_POINTER(_CT_POINTER(_ct_void_p)))[0]
                return ctypes.WINFUNCTYPE(restype, _ct_void_p, *argtypes)(vtbl[index])

            def _release(ptr: _ct_void_p) -> None:
                if ptr.value:
                    _vtable_fn(ptr, 2, _ct_wintypes.ULONG)(ptr)

            def _default_id(data_flow: int) -> str | None:
                dev_ptr = _ct_void_p()
                hr_dev = int(
                    _vtable_fn(
                        enum_ptr,
                        4,  # IMMDeviceEnumerator::GetDefaultAudioEndpoint
                        _ct_long,
                        _ct_int,
                        _ct_int,
                        _CT_POINTER(_ct_void_p),
                    )(enum_ptr, data_flow, 0, _ct_byref(dev_ptr))  # eConsole = 0
                )
                if hr_dev != 0 or not dev_ptr.value:
                    return None
                id_ptr = _ct_wintypes.LPWSTR()
                try:
                    hr_id = int(
                        _vtable_fn(
                            dev_ptr,
                            5,  # IMMDevice::GetId
                            _ct_long,
                            _CT_POINTER(_ct_wintypes.LPWSTR),
                        )(dev_ptr, _ct_byref(id_ptr))
                    )
                    if hr_id != 0 or not id_ptr.value:
                        return None
                    return str(id_ptr.value)
                finally:
                    if id_ptr.value:
                        ole32.CoTaskMemFree(id_ptr)
                    _release(dev_ptr)

            try:
                capture_id = _default_id(1)  # eCapture = 1
                render_id = _default_id(0)  # eRender = 0
                return capture_id, render_id
            finally:
                _release(enum_ptr)
        finally:
            if should_uninit:
                ole32.CoUninitialize()
    except Exception as exc:
        logger.debug("Could not query Windows default audio endpoints: %s", exc)
        return None, None


def list_candidate_audio_devices(
    spec: str | int | None,
    kind: str = "input",
    preferred_hostapi: str | None = "WASAPI",
) -> list[dict[str, Any]]:
    """Return an ordered list of candidate audio devices to try opening.

    Always starts with the primary device chosen by :func:`resolve_audio_device`
    (preserving ``AudioDeviceNotFoundError`` / ``AmbiguousAudioDeviceError``).
    When *spec* is ``None`` (system default), appends same-name devices on other
    host APIs (e.g. DirectSound / MME when a Bluetooth WASAPI endpoint fails
    with PaErrorCode -9996 / -9999), followed by remaining valid devices ordered
    by host API preference, placing virtual/loopback capture endpoints last.
    """
    primary = resolve_audio_device(spec, kind=kind, preferred_hostapi=preferred_hostapi)
    candidates: list[dict[str, Any]] = [primary]
    seen_indices: set[int] = {int(primary["index"])}

    # Explicit integer index -> honor the user's exact index without silent redirection
    if isinstance(spec, int) or (isinstance(spec, str) and spec.strip().isdigit()):
        return candidates

    devices = list_audio_devices()
    valid_devices = [
        d
        for d in devices
        if (d["max_input_channels"] > 0 if kind == "input" else d["max_output_channels"] > 0)
    ]

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

    primary_name = str(primary.get("name", ""))

    # 1. Same device name across alternate host APIs (e.g. DirectSound / MME fallback)
    same_name = sorted(
        [
            d
            for d in valid_devices
            if _is_same_audio_device_name(str(d.get("name", "")), primary_name)
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

    # 2. System default device from PortAudio (often MME/DirectSound)
    try:
        def_idx = sd.default.device[0 if kind == "input" else 1]
        if def_idx is not None and def_idx >= 0:
            match = next((d for d in valid_devices if d["index"] == def_idx), None)
            if match is not None:
                _add(match)
    except Exception:
        pass

    # 3. Remaining valid devices ordered by host API preference, virtual/loopback last
    for dev in sorted(valid_devices, key=_host_rank):
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


def _to_mono_int16_scale(audio: np.ndarray) -> np.ndarray:
    """Convert numeric 1D/2D audio to a 1D mono array in signed 16-bit PCM scale."""
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
            return audio[:, 0]
        return audio.mean(axis=1)
    return audio.reshape(-1)


def _resample_mono_scaled(
    mono_audio: np.ndarray,
    orig_sr: int,
    target_sr: int = 16000,
) -> np.ndarray:
    """Resample an already mono int16-scaled 1D array to contiguous np.int16."""
    if mono_audio.size == 0:
        return np.empty(0, dtype=np.int16)

    if orig_sr == target_sr:
        if mono_audio.dtype == np.int16:
            return np.ascontiguousarray(mono_audio)
        return np.ascontiguousarray(np.clip(np.round(mono_audio), -32768, 32767).astype(np.int16))

    g = math.gcd(orig_sr, target_sr)
    up = target_sr // g
    down = orig_sr // g

    # Polyphase rational resampler with float32 computation
    resampled = scipy.signal.resample_poly(mono_audio.astype(np.float32, copy=False), up, down)
    return np.ascontiguousarray(np.clip(np.round(resampled), -32768, 32767).astype(np.int16))


def _native_resample_block_size(orig_sr: int, target_sr: int, target_chunk_size: int) -> int:
    """Return an exact native-rate block size aligned to the rational downsampling factor.

    Ensures ``block_size * up / down`` is an exact integer (e.g. 3840 @ 48 kHz and
    3528 @ 44.1 kHz for 1280 @ 16 kHz) so polyphase resampling never suffers 10 ms
    boundary ringing or fractional sample-rate drift on variable WASAPI periods.
    """
    g = math.gcd(orig_sr, target_sr)
    down = max(1, orig_sr // g)
    approx = max(1, int(round(target_chunk_size * orig_sr / target_sr)))
    return max(down, math.ceil(approx / down) * down)


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

    mono = _to_mono_int16_scale(audio)
    return _resample_mono_scaled(mono, orig_sr=orig_sr, target_sr=target_sr)


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
        lazy_output: bool = True,
    ) -> None:
        self.input_device = input_device
        self.output_device = output_device
        self.preferred_hostapi = preferred_hostapi
        self.mic_mute_while_speaking = mic_mute_while_speaking
        self._on_audio_input = on_audio_input
        self.chunk_size = chunk_size
        self._lazy_output = bool(lazy_output)
        self._chunk_buffer: AudioChunkBuffer | None = (
            AudioChunkBuffer(chunk_size=chunk_size) if chunk_size else None
        )
        self.input_sample_rate: int = INPUT_AUDIO_SAMPLE_RATE_HZ
        self.input_channels: int = AUDIO_CHANNELS

        self.resolved_input_device: dict[str, Any] | None = None
        self.resolved_output_device: dict[str, Any] | None = None
        self._output_candidates: list[dict[str, Any]] = []

        self._input_stream: sd.InputStream | None = None
        self._output_stream: sd.OutputStream | None = None
        self._output_suspend_epoch: int = 0
        self._output_queue: queue.Queue[bytes] = queue.Queue()
        self._current_output_bytes = bytearray()
        self._input_accumulator = bytearray()
        self._native_mono_chunks: list[np.ndarray] = []
        self._native_mono_count: int = 0
        self._native_mono_sr: int = 0
        self._is_speaking = threading.Event()
        self._lock = threading.RLock()
        self._lifecycle_lock = threading.RLock()
        self._input_buf_lock = threading.Lock()
        self._running = False
        self._last_input_callback_time: float = 0.0
        self._input_callback_count: int = 0
        self._consecutive_zero_callbacks: int = 0
        self._consecutive_zero_samples: int = 0
        self._last_input_rms: float = 0.0
        self._peak_input_rms: float = 0.0
        self._input_status_error_count: int = 0
        self._health_monitored: bool = False
        self._wasapi_retry_delay: float = 0.35
        self._default_endpoint_ids: tuple[str | None, str | None] = (None, None)
        self._input_fell_back_to_other_device: bool = False
        self._fallback_reclaim_attempts: int = 0
        self._fallback_opened_at: float = 0.0
        self._fallback_reclaim_max_attempts: int = 3
        self._fallback_reclaim_base_delay: float = 3.0

    @property
    def is_speaking(self) -> bool:
        """True when assistant audio is actively being played out."""
        return self._is_speaking.is_set()

    @property
    def is_output_stream_active(self) -> bool:
        """True when the PortAudio OutputStream is currently open and active."""
        with self._lock:
            out = self._output_stream
        if out is None:
            return False
        try:
            return bool(getattr(out, "active", True)) and not bool(getattr(out, "stopped", False))
        except Exception:
            return False

    def wait_for_input_callbacks(
        self,
        *,
        timeout: float = 1.5,
        poll_interval: float = 0.025,
        min_callbacks: int = 1,
        stop_event: Any = None,
    ) -> bool:
        """Wait briefly (off the callback path) for active input callbacks to arrive.

        Returns True once ``_input_callback_count >= min_callbacks``, or False if
        ``timeout`` expires, ``stop_event`` is set, or the manager stops.
        """
        deadline = time.monotonic() + max(0.0, float(timeout))
        step = max(0.005, float(poll_interval))
        target = max(1, int(min_callbacks))
        while True:
            if stop_event is not None and stop_event.is_set():
                return False
            with self._lock:
                if not self._running:
                    return False
                if self._input_callback_count >= target:
                    return True
            remaining = deadline - time.monotonic()
            if remaining <= 0.0:
                return False
            time.sleep(min(step, remaining))

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
            }

    def has_default_audio_device_changed(self) -> tuple[bool, str]:
        """Check whether the OS default capture/render endpoint changed since streams opened.

        Only monitors unpinned directions (``self.input_device is None`` /
        ``self.output_device is None``). Never reinitializes PortAudio during the check.
        """
        if self.input_device is not None and self.output_device is not None:
            return False, "pinned"
        with self._lock:
            if not self._running:
                return False, "not_running"
            prev_in, prev_out = self._default_endpoint_ids

        if prev_in is None and prev_out is None:
            return False, "unavailable"

        cur_in, cur_out = get_os_default_audio_endpoint_ids()
        in_changed = (
            self.input_device is None
            and prev_in is not None
            and cur_in is not None
            and cur_in != prev_in
        )
        out_changed = (
            self.output_device is None
            and prev_out is not None
            and cur_out is not None
            and cur_out != prev_out
        )
        if in_changed and out_changed:
            return True, "os_default_audio_changed"
        if in_changed:
            return True, "os_default_input_changed"
        if out_changed:
            return True, "os_default_output_changed"
        return False, "ok"

    def should_reclaim_primary_input(self) -> tuple[bool, str]:
        """Check if a transient fallback to a non-default physical mic should reclaim primary.

        Only triggers when ``self.input_device is None`` AND startup fell back to a
        *different* physical microphone (e.g. built-in mic while Bluetooth HFP SCO was
        still negotiating during connect). Bounded to ``_fallback_reclaim_max_attempts``
        with exponential backoff so permanently broken devices never loop.
        """
        if self.input_device is not None:
            return False, "pinned"
        with self._lock:
            if not self._running or not self._input_fell_back_to_other_device:
                return False, "ok"
            if self._fallback_reclaim_attempts >= self._fallback_reclaim_max_attempts:
                return False, "reclaim_exhausted"
            delay = self._fallback_reclaim_base_delay * (2**self._fallback_reclaim_attempts)
            if (time.monotonic() - self._fallback_opened_at) < delay:
                return False, "backoff"
            self._fallback_reclaim_attempts += 1
            return True, "os_default_reclaim_primary_input"

    def set_input_callback(self, callback: Callable[[bytes], None] | None) -> None:
        """Set or update the microphone PCM data callback."""
        with self._lock:
            self._on_audio_input = callback

    def _clear_native_mono_buffer(self) -> None:
        with self._input_buf_lock:
            self._native_mono_chunks.clear()
            self._native_mono_count = 0
            self._native_mono_sr = 0

    def flush_input_buffer(self) -> None:
        """Flush buffered microphone samples (e.g. when returning to wake-word IDLE)."""
        self._clear_native_mono_buffer()
        with self._lock:
            self._input_accumulator.clear()
            if self._chunk_buffer is not None:
                self._chunk_buffer.clear()

    def is_input_stream_healthy(
        self,
        *,
        max_callback_gap_seconds: float = 5.0,
        max_zero_callbacks: int = 125,
    ) -> tuple[bool, str]:
        """Check whether the microphone InputStream is alive and delivering real frames.

        Returns ``(True, "ok")`` when healthy, or ``(False, reason)`` if the stream
        stopped, callbacks stalled (WASAPI/Bluetooth hang), or sustained bit-exact
        all-zero buffers indicate a dead endpoint.
        """
        with self._lock:
            if not self._running or self._input_stream is None:
                return False, "input_stream_not_running"
            inp = self._input_stream
            last_cb = self._last_input_callback_time
            zero_streak = self._consecutive_zero_callbacks

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

        return True, "ok"

    def restart_streams(
        self,
        *,
        max_attempts: int = 3,
        retry_delay: float = 0.5,
    ) -> bool:
        """Stop streams, refresh PortAudio endpoints, and reopen audio I/O safely.

        Preserves the output stream's active vs suspended state so hot-swap rebinds
        during IDLE never start zero-streaming on Bluetooth A2DP output.
        """
        with self._lifecycle_lock:
            with self._lock:
                restore_output = self._output_stream is not None
            try:
                self.stop()
                refresh_audio_devices()
                self.start(max_attempts=max_attempts, retry_delay=retry_delay)
                if restore_output:
                    self.start_output_stream()
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
            inp_wasapi = "WASAPI" in host_name.upper()
            if inp_wasapi:
                sample_rate, channels = negotiate_input_sample_rate(
                    device_idx=inp_idx,
                    default_samplerate=native_rate,
                    candidate_rates=(16000, 48000, 44100),
                    preferred_channels=1,
                )
            else:
                # DirectSound / MME report 44100 Hz for all endpoints regardless of hardware rate;
                # prefer 16000 Hz natively to avoid 16 kHz -> 44.1 kHz -> 16 kHz double-resampling.
                sample_rate, channels = negotiate_input_sample_rate(
                    device_idx=inp_idx,
                    default_samplerate=INPUT_AUDIO_SAMPLE_RATE_HZ,
                    candidate_rates=(INPUT_AUDIO_SAMPLE_RATE_HZ, native_rate, 48000, 44100),
                    preferred_channels=1,
                )
            # On Windows WASAPI, pass blocksize=0 so PortAudio uses the native WASAPI engine
            # period (~10 ms) without forcing an 80 ms PortAudio ring-buffer adapter; exact
            # 80 ms native-rate blocks are accumulated before polyphase resampling downstream.
            blocksize = 0 if inp_wasapi else int(sample_rate * 0.08)
            extra_options: list[Any] = (
                [sd.WasapiSettings(auto_convert=True), None]
                if (inp_wasapi and hasattr(sd, "WasapiSettings"))
                else [None]
            )
            # Bluetooth HFP SCO endpoints often need ~350-800 ms to wake from A2DP idle;
            # retry primary WASAPI after a brief settle pause before falling back.
            attempt_specs: list[tuple[Any, int, float]] = [
                (extra, blocksize, 0.0) for extra in extra_options
            ]
            if idx_pos == 0 and inp_wasapi and self._wasapi_retry_delay >= 0.0:
                attempt_specs.append((extra_options[0], blocksize, self._wasapi_retry_delay))
                if len(extra_options) > 1:
                    attempt_specs.append((extra_options[1], blocksize, 0.0))

            for attempt_idx, (extra, try_blocksize, delay_before) in enumerate(attempt_specs):
                if delay_before > 0.0:
                    logger.debug(
                        "Retrying primary WASAPI input #%d '%s' after %.2fs settle pause...",
                        inp_idx,
                        dev["name"],
                        delay_before,
                    )
                    time.sleep(delay_before)
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
                        blocksize=try_blocksize,
                        extra_settings=extra,
                        callback=self._input_stream_callback,
                    )
                    stream.start()
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
                    if attempt_idx + 1 < len(attempt_specs):
                        logger.debug(
                            "Input device #%d '%s' attempt %d/%d failed (%s); retrying...",
                            inp_idx,
                            dev["name"],
                            attempt_idx + 1,
                            len(attempt_specs),
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
            attempt_specs: list[tuple[Any, float]] = [(extra, 0.0) for extra in extra_options]
            if idx_pos == 0 and out_wasapi and self._wasapi_retry_delay >= 0.0:
                attempt_specs.append((extra_options[0], self._wasapi_retry_delay))
                if len(extra_options) > 1:
                    attempt_specs.append((extra_options[1], 0.0))

            for attempt_idx, (extra, delay_before) in enumerate(attempt_specs):
                if delay_before > 0.0:
                    logger.debug(
                        "Retrying primary WASAPI output #%d '%s' after %.2fs settle pause...",
                        out_idx,
                        dev["name"],
                        delay_before,
                    )
                    time.sleep(delay_before)
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
                    if attempt_idx + 1 < len(attempt_specs):
                        logger.debug(
                            "Output device #%d '%s' attempt %d/%d failed (%s); retrying...",
                            out_idx,
                            dev["name"],
                            attempt_idx + 1,
                            len(attempt_specs),
                            exc,
                        )
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
        """Start microphone input and audio output streams once (with device fallback).

        Never holds ``self._lock`` across PortAudio ``start()`` / ``stop()`` / ``close()``
        or blocking device probes so ``_output_stream_callback`` cannot deadlock against
        stream startup or error teardown.
        """
        with self._lifecycle_lock:
            with self._lock:
                if self._running:
                    return
                self._current_output_bytes.clear()
                self._input_accumulator.clear()
                self._drain_output_queue()
                self._is_speaking.clear()
                self._consecutive_zero_callbacks = 0
                self._consecutive_zero_samples = 0
                self._last_input_rms = 0.0
                self._peak_input_rms = 0.0
                self._input_callback_count = 0
                self._input_status_error_count = 0
                if self._chunk_buffer is None:
                    self._chunk_buffer = AudioChunkBuffer(chunk_size=self.chunk_size or 1280)
                else:
                    self._chunk_buffer.clear()
            self._clear_native_mono_buffer()

            # Capture current OS default endpoint IDs BEFORE resolving candidates so any
            # hot-swap occurring mid-startup is caught on the very next watchdog poll.
            captured_defaults = get_os_default_audio_endpoint_ids()

            # Resolve ordered candidate input & output devices (raises on invalid/ambiguous spec)
            input_candidates = list_candidate_audio_devices(
                self.input_device,
                kind="input",
                preferred_hostapi=self.preferred_hostapi,
            )
            output_candidates = list_candidate_audio_devices(
                self.output_device,
                kind="output",
                preferred_hostapi=self.preferred_hostapi,
            )

            inp_stream: sd.InputStream | None = None
            out_stream: sd.OutputStream | None = None
            chosen_out: dict[str, Any] = output_candidates[0]
            try:
                inp_stream, chosen_in, sample_rate, channels = self._open_input_stream(
                    input_candidates
                )
                if not self._lazy_output:
                    out_stream, chosen_out = self._open_output_stream(output_candidates)
            except Exception:
                for stream in (inp_stream, out_stream):
                    if stream is not None:
                        try:
                            stream.stop()
                            stream.close()
                        except Exception as exc:
                            logger.debug("Error closing partial stream on startup failure: %s", exc)
                self._clear_native_mono_buffer()
                with self._lock:
                    self._input_stream = None
                    self._output_stream = None
                    self._output_candidates = []
                    self._running = False
                    self._input_accumulator.clear()
                    if self._chunk_buffer is not None:
                        self._chunk_buffer.clear()
                self.stop_output()
                raise

            primary_in_name = str(input_candidates[0].get("name", "")) if input_candidates else ""
            chosen_in_name = str(chosen_in.get("name", ""))
            fell_back_other = (
                self.input_device is None
                and bool(primary_in_name)
                and not _is_same_audio_device_name(chosen_in_name, primary_in_name)
            )
            now = time.monotonic()
            with self._lock:
                prev_defaults = self._default_endpoint_ids
                self._input_stream = inp_stream
                self.resolved_input_device = chosen_in
                self.input_sample_rate = sample_rate
                self.input_channels = channels
                self._output_stream = out_stream
                self._output_candidates = list(output_candidates)
                self.resolved_output_device = chosen_out
                self._default_endpoint_ids = captured_defaults
                self._input_fell_back_to_other_device = fell_back_other
                if not fell_back_other or captured_defaults != prev_defaults:
                    self._fallback_reclaim_attempts = 0
                self._fallback_opened_at = now if fell_back_other else 0.0
                self._last_input_callback_time = now
                self._running = True
                self._health_monitored = True

            if fell_back_other:
                logger.warning(
                    "Primary OS default input '%s' was temporarily unavailable; using '%s' "
                    "temporarily and will attempt to reclaim primary OS default while idle.",
                    primary_in_name,
                    chosen_in_name,
                )
            logger.info(
                "AudioManager started (input=#%d '%s' [%s, %d Hz, %dch], output=#%d '%s' "
                "[active=%s], mute_while_speaking=%s).",
                chosen_in["index"],
                chosen_in.get("name", "unknown"),
                chosen_in.get("hostapi_name", "Unknown"),
                sample_rate,
                channels,
                chosen_out["index"],
                chosen_out.get("name", "unknown"),
                out_stream is not None,
                self.mic_mute_while_speaking,
            )

    def start_output_stream(self, *, _expected_suspend_epoch: int | None = None) -> bool:
        """Open and start the OutputStream on demand (deadlock-free under _lifecycle_lock).

        Returns True when the output stream is open and ready, or False if AudioManager
        is not running (or running in a unit-test harness that never called ``start()``),
        or if ``suspend_output_stream()`` superseded a pending ``play_audio()`` open.
        """
        with self._lock:
            if not self._running or not self._health_monitored:
                return False
            if (
                _expected_suspend_epoch is not None
                and self._output_suspend_epoch != _expected_suspend_epoch
            ):
                return False
            if self._output_stream is not None:
                return True

        with self._lifecycle_lock:
            with self._lock:
                if not self._running or not self._health_monitored:
                    return False
                if (
                    _expected_suspend_epoch is not None
                    and self._output_suspend_epoch != _expected_suspend_epoch
                ):
                    return False
                if self._output_stream is not None:
                    return True
                candidates = list(self._output_candidates)

            if not candidates:
                candidates = list_candidate_audio_devices(
                    self.output_device,
                    kind="output",
                    preferred_hostapi=self.preferred_hostapi,
                )

            out_stream, chosen_out = self._open_output_stream(candidates)
            should_close = False
            with self._lock:
                if not self._running or (
                    _expected_suspend_epoch is not None
                    and self._output_suspend_epoch != _expected_suspend_epoch
                ):
                    should_close = True
                else:
                    self._output_stream = out_stream
                    self._output_candidates = list(candidates)
                    self.resolved_output_device = chosen_out

            if should_close:
                try:
                    out_stream.stop()
                    out_stream.close()
                except Exception as exc:
                    logger.debug("Error closing output stream after concurrent stop: %s", exc)
                return False

            logger.info(
                "Audio output stream started on demand (output=#%d '%s' [%s]).",
                chosen_out["index"],
                chosen_out.get("name", "unknown"),
                chosen_out.get("hostapi_name", "Unknown"),
            )
            return True

    def suspend_output_stream(self, *, drain_timeout: float = 0.0) -> None:
        """Stop and close the OutputStream while keeping microphone input alive for IDLE.

        Prevents continuous zero-streaming to Bluetooth A2DP render endpoints while HFP
        input is listening for the wake word. Never holds ``self._lock`` across PortAudio
        ``stop()`` / ``close()`` so ``_output_stream_callback`` cannot deadlock.
        """
        if drain_timeout > 0.0:
            deadline = time.monotonic() + float(drain_timeout)
            while self._is_speaking.is_set() and time.monotonic() < deadline:
                time.sleep(min(0.02, max(0.001, deadline - time.monotonic())))

        with self._lifecycle_lock:
            with self._lock:
                self._output_suspend_epoch += 1
                out = self._output_stream
                self._output_stream = None
                self._drain_output_queue()
                self._current_output_bytes.clear()
                self._is_speaking.clear()

            if out is not None:
                try:
                    out.stop()
                    out.close()
                except Exception as exc:
                    logger.debug("Error suspending output stream: %s", exc)
                logger.info("Audio output stream suspended for IDLE.")

    def stop(self) -> None:
        """Stop and close both audio streams."""
        with self._lifecycle_lock:
            with self._lock:
                self._running = False
                self._output_suspend_epoch += 1
                inp = self._input_stream
                out = self._output_stream
                self._input_stream = None
                self._output_stream = None
                self._output_candidates = []
                self._input_accumulator.clear()
                if self._chunk_buffer is not None:
                    self._chunk_buffer.clear()
            self._clear_native_mono_buffer()
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
        """Queue 24 kHz 16-bit mono PCM bytes for playback, opening output on demand."""
        if not pcm_bytes:
            return
        need_open = False
        epoch = 0
        with self._lock:
            if not self._running:
                return
            epoch = self._output_suspend_epoch
            self._output_queue.put(pcm_bytes)
            self._is_speaking.set()
            need_open = self._output_stream is None and self._health_monitored
        if need_open:
            try:
                opened = self.start_output_stream(_expected_suspend_epoch=epoch)
            except Exception as exc:
                logger.warning("Failed starting audio output stream on demand: %s", exc)
                opened = False
            if not opened:
                with self._lock:
                    if self._output_suspend_epoch == epoch:
                        self._drain_output_queue()
                        self._current_output_bytes.clear()
                        self._is_speaking.clear()

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
            self._consecutive_zero_samples = 0
            self._clear_native_mono_buffer()
            if self._chunk_buffer is not None:
                self._chunk_buffer.clear()
            return

        if indata.size == 0:
            return

        orig_sr = getattr(self, "input_sample_rate", INPUT_AUDIO_SAMPLE_RATE_HZ)
        if not np.any(indata):
            block_80ms = max(1, int(orig_sr * 0.08))
            chunk_frames = int(indata.shape[0])
            if chunk_frames >= block_80ms:
                self._consecutive_zero_callbacks += 1
                self._consecutive_zero_samples = 0
            else:
                self._consecutive_zero_samples += chunk_frames
                if self._consecutive_zero_samples >= block_80ms:
                    step = self._consecutive_zero_samples // block_80ms
                    self._consecutive_zero_callbacks += step
                    self._consecutive_zero_samples %= block_80ms
        else:
            self._consecutive_zero_callbacks = 0
            self._consecutive_zero_samples = 0

        mono = _to_mono_int16_scale(indata)
        rms = compute_audio_rms(mono)
        self._last_input_rms = rms
        if rms > self._peak_input_rms:
            self._peak_input_rms = rms

        cb = self._on_audio_input
        if cb is None:
            return

        if self._chunk_buffer is not None:
            if orig_sr == INPUT_AUDIO_SAMPLE_RATE_HZ:
                self._chunk_buffer.extend(
                    _resample_mono_scaled(
                        mono, orig_sr=orig_sr, target_sr=INPUT_AUDIO_SAMPLE_RATE_HZ
                    )
                )
            else:
                # Accumulate native-rate mono samples into exact rational-aligned blocks
                # (e.g. 3840 @ 48 kHz, 3528 @ 44.1 kHz) BEFORE polyphase resampling so
                # 10 ms / variable-period WASAPI callbacks have zero boundary ringing or drift.
                native_block = _native_resample_block_size(
                    orig_sr,
                    INPUT_AUDIO_SAMPLE_RATE_HZ,
                    self._chunk_buffer.chunk_size,
                )
                ready_blocks: list[np.ndarray] = []
                with self._input_buf_lock:
                    if self._native_mono_sr != orig_sr:
                        self._native_mono_chunks.clear()
                        self._native_mono_count = 0
                        self._native_mono_sr = orig_sr
                    # Copy out of PortAudio's reusable callback buffer before storing
                    self._native_mono_chunks.append(np.array(mono, dtype=np.float32, copy=True))
                    self._native_mono_count += len(mono)
                    if self._native_mono_count >= native_block:
                        combined = (
                            self._native_mono_chunks[0]
                            if len(self._native_mono_chunks) == 1
                            else np.concatenate(self._native_mono_chunks)
                        )
                        offset = 0
                        total = len(combined)
                        while offset + native_block <= total:
                            ready_blocks.append(combined[offset : offset + native_block])
                            offset += native_block
                        if offset < total:
                            rem = np.array(combined[offset:], dtype=np.float32, copy=True)
                            self._native_mono_chunks = [rem]
                            self._native_mono_count = len(rem)
                        else:
                            self._native_mono_chunks = []
                            self._native_mono_count = 0
                for block in ready_blocks:
                    resampled = _resample_mono_scaled(
                        block,
                        orig_sr=orig_sr,
                        target_sr=INPUT_AUDIO_SAMPLE_RATE_HZ,
                    )
                    self._chunk_buffer.extend(resampled)

            for chunk in self._chunk_buffer.get_chunks():
                try:
                    cb(chunk.tobytes())
                except Exception:
                    logger.exception("Error in audio input callback.")
        else:
            resampled_16k = _resample_mono_scaled(
                mono,
                orig_sr=orig_sr,
                target_sr=INPUT_AUDIO_SAMPLE_RATE_HZ,
            )
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

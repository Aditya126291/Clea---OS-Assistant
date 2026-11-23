"""Wake word detection using livekit-wakeword v2 (primary) with openWakeWord startup fallback."""

from __future__ import annotations

import logging
import os
import tempfile
import threading
import time
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from clea.paths import default_data_dir
from clea.surfaces.voice.audio import compute_audio_rms, resample_to_16k_mono
from clea.surfaces.voice.config import WakeWordEngine

logger = logging.getLogger(__name__)

WAKE_WORD_SAMPLE_RATE_HZ = 16000
WAKE_WORD_CHUNK_SAMPLES = 1280

MODELS_SUBDIR = "models"
LIVEKIT_MODELS_SUBDIR = "wakeword"

ENV_WAKEWORD_MODEL_PATH = "CLEA_WAKEWORD_MODEL_PATH"
ENV_WAKEWORD_AUTO_DOWNLOAD = "CLEA_WAKEWORD_AUTO_DOWNLOAD"

LIVEKIT_WAKEWORD_RELEASE_TAG = "v2.1.3"
LIVEKIT_DOWNLOAD_TIMEOUT_SECONDS = 30.0

DEFAULT_LIVEKIT_THRESHOLD = 0.50
DEFAULT_OPENWAKEWORD_THRESHOLD = 0.30
LEGACY_OPENWAKEWORD_THRESHOLDS: tuple[float, ...] = (0.20, 0.30)

LIVEKIT_NEAR_MISS_FLOOR = 0.15
OPENWAKEWORD_NEAR_MISS_FLOOR = 0.08
DEFAULT_NEAR_MISS_LOG_INTERVAL_SECONDS = 2.0

LIVEKIT_BUILTIN_WAKEWORDS: dict[str, dict[str, Any]] = {
    "hey_jarvis": {
        "filename": "hey_jarvis.onnx",
        "url": (
            "https://github.com/livekit/livekit-wakeword/releases/download/"
            f"{LIVEKIT_WAKEWORD_RELEASE_TAG}/hey_jarvis.onnx"
        ),
        "min_bytes": 1_000_000,
    },
    "hey_livekit": {
        "filename": "hey_livekit.onnx",
        "url": (
            "https://github.com/livekit/livekit-wakeword/releases/download/"
            f"{LIVEKIT_WAKEWORD_RELEASE_TAG}/hey_livekit.onnx"
        ),
        "min_bytes": 1_000_000,
    },
}

FEATURE_MODELS: dict[str, dict[str, str]] = {
    "melspectrogram": {
        "filename": "melspectrogram.onnx",
        "url": "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/melspectrogram.onnx",
    },
    "embedding": {
        "filename": "embedding_model.onnx",
        "url": "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/embedding_model.onnx",
    },
}

BUILTIN_WAKEWORDS: dict[str, dict[str, str]] = {
    "hey_jarvis": {
        "filename": "hey_jarvis_v0.1.onnx",
        "url": "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/hey_jarvis_v0.1.onnx",
        "model_key": "hey_jarvis_v0.1",
    },
    "alexa": {
        "filename": "alexa_v0.1.onnx",
        "url": "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/alexa_v0.1.onnx",
        "model_key": "alexa_v0.1",
    },
    "hey_mycroft": {
        "filename": "hey_mycroft_v0.1.onnx",
        "url": "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/hey_mycroft_v0.1.onnx",
        "model_key": "hey_mycroft_v0.1",
    },
    "hey_rhasspy": {
        "filename": "hey_rhasspy_v0.1.onnx",
        "url": "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/hey_rhasspy_v0.1.onnx",
        "model_key": "hey_rhasspy_v0.1",
    },
    "timer": {
        "filename": "timer_v0.1.onnx",
        "url": "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/timer_v0.1.onnx",
        "model_key": "timer_v0.1",
    },
    "weather": {
        "filename": "weather_v0.1.onnx",
        "url": "https://github.com/dscripka/openWakeWord/releases/download/v0.5.1/weather_v0.1.onnx",
        "model_key": "weather_v0.1",
    },
}


class WakeWordModelNotFoundError(FileNotFoundError):
    """Raised when wake word or feature model files are missing in <user_data_dir>/models/."""


def wake_word_models_dir(data_dir: Path | None = None) -> Path:
    """Return the path to the models directory in the user data directory."""
    return (data_dir or default_data_dir()) / MODELS_SUBDIR


def livekit_wake_word_models_dir(data_dir: Path | None = None) -> Path:
    """Return the cache directory for livekit-wakeword v2 ONNX models."""
    return wake_word_models_dir(data_dir) / LIVEKIT_MODELS_SUBDIR


def _is_auto_download_enabled(allow_download: bool) -> bool:
    if not allow_download:
        return False
    env_flag = os.environ.get(ENV_WAKEWORD_AUTO_DOWNLOAD, "").strip().lower()
    if env_flag in {"0", "false", "no", "off"}:
        return False
    return True


def _validate_onnx_file(
    path: Path,
    *,
    min_bytes: int = 1,
    expected_bytes: int | None = None,
) -> None:
    """Validate basic file size and protobuf ModelProto header before accepting an ONNX file."""
    if not path.is_file():
        raise ValueError(f"ONNX model file does not exist: {path}")
    size = path.stat().st_size
    if size < max(1, int(min_bytes)):
        raise ValueError(
            f"ONNX model file {path.name} is too small ({size} bytes < {min_bytes} bytes)."
        )
    if expected_bytes is not None and expected_bytes > 0 and size != expected_bytes:
        raise ValueError(
            f"ONNX model file {path.name} size mismatch "
            f"({size} bytes != expected {expected_bytes} bytes)."
        )

    with open(path, "rb") as fh:
        head = fh.read(64)
    if not head:
        raise ValueError(f"ONNX model file {path.name} is empty.")

    stripped = head.lstrip()
    if stripped.startswith((b"<", b"{", b"[", b"http", b"HTTP")):
        raise ValueError(f"ONNX model file {path.name} appears to be HTML/JSON rather than ONNX.")

    first_byte = head[0]
    wire_type = first_byte & 0x07
    field_number = first_byte >> 3
    if wire_type not in (0, 2) or not (1 <= field_number <= 14):
        raise ValueError(
            f"ONNX model file {path.name} failed protobuf header validation "
            f"(leading byte 0x{first_byte:02x})."
        )


def resolve_wake_threshold(
    engine: str,
    *,
    threshold: float | None = None,
    livekit_threshold: float | None = None,
) -> float:
    """Resolve an engine-appropriate detection threshold.

    Protects ``livekit-wakeword`` v2 from accidentally running at legacy openWakeWord
    thresholds (``0.20`` / ``0.30``) unless ``livekit_threshold`` was explicitly provided.
    """
    normalized_engine = engine.strip().lower()
    if normalized_engine == "livekit":
        if livekit_threshold is not None:
            return float(livekit_threshold)
        if threshold is None:
            return DEFAULT_LIVEKIT_THRESHOLD
        cand = float(threshold)
        if any(abs(cand - legacy) <= 1e-6 for legacy in LEGACY_OPENWAKEWORD_THRESHOLDS):
            logger.warning(
                "Legacy openWakeWord wake_threshold=%.2f detected for livekit-wakeword v2; "
                "using safe default %.2f (set livekit_wake_threshold to override).",
                cand,
                DEFAULT_LIVEKIT_THRESHOLD,
            )
            return DEFAULT_LIVEKIT_THRESHOLD
        return cand

    if threshold is not None:
        return float(threshold)
    return DEFAULT_OPENWAKEWORD_THRESHOLD


def check_livekit_model_status(
    model_name: str = "hey_jarvis",
    *,
    cache_dir: Path | None = None,
    data_dir: Path | None = None,
) -> tuple[bool, Path, str]:
    """Read-only check whether a livekit-wakeword v2 model is available locally.

    Returns ``(is_ready, resolved_path, download_url)`` without mutating disk or network.
    """
    env_override = os.environ.get(ENV_WAKEWORD_MODEL_PATH, "").strip()
    if env_override:
        override_path = Path(env_override).expanduser()
        return override_path.is_file(), override_path, ""

    normalized = model_name.strip()
    candidate = Path(normalized).expanduser()
    if candidate.suffix.lower() == ".onnx" and (
        candidate.parent != Path(".") or candidate.is_file()
    ):
        return candidate.is_file(), candidate, ""

    key = candidate.stem if candidate.suffix.lower() == ".onnx" else normalized
    target_dir = cache_dir if cache_dir is not None else livekit_wake_word_models_dir(data_dir)
    builtin = LIVEKIT_BUILTIN_WAKEWORDS.get(key)
    filename = str(builtin["filename"]) if builtin else f"{key}.onnx"
    url = str(builtin["url"]) if builtin else ""
    target_path = target_dir / filename
    return target_path.is_file(), target_path, url


def resolve_livekit_wakeword_model(
    model_name: str = "hey_jarvis",
    *,
    cache_dir: Path | None = None,
    data_dir: Path | None = None,
    allow_download: bool = True,
    timeout: float = LIVEKIT_DOWNLOAD_TIMEOUT_SECONDS,
    min_bytes: int | None = None,
) -> Path:
    """Resolve (and lazily download if missing) a livekit-wakeword v2 ``.onnx`` model.

    Resolution order:
    1. Explicit ``CLEA_WAKEWORD_MODEL_PATH`` environment override.
    2. Explicit ``.onnx`` file path passed in ``model_name``.
    3. Cached file at ``<cache_dir>/<model_name>.onnx`` (default
       ``<clea_data_dir>/models/wakeword/<model_name>.onnx``).
    4. Atomic lazy download (temp file + size/header validation + rename) from the
       verified ``livekit/livekit-wakeword`` GitHub release URL when allowed.
    """
    env_override = os.environ.get(ENV_WAKEWORD_MODEL_PATH, "").strip()
    if env_override:
        override_path = Path(env_override).expanduser()
        if not override_path.is_file():
            raise WakeWordModelNotFoundError(
                f"{ENV_WAKEWORD_MODEL_PATH} points to missing model file: {override_path}"
            )
        try:
            _validate_onnx_file(override_path, min_bytes=min_bytes if min_bytes is not None else 1)
        except ValueError as exc:
            raise WakeWordModelNotFoundError(
                f"{ENV_WAKEWORD_MODEL_PATH} is not a valid ONNX file: {exc}"
            ) from exc
        return override_path

    normalized = model_name.strip()
    if not normalized:
        raise WakeWordModelNotFoundError("Wake word model name must not be empty.")

    candidate = Path(normalized).expanduser()
    if candidate.suffix.lower() == ".onnx" and (
        candidate.parent != Path(".") or candidate.is_file()
    ):
        if not candidate.is_file():
            raise WakeWordModelNotFoundError(f"Custom wake word model not found: {candidate}")
        try:
            _validate_onnx_file(candidate, min_bytes=min_bytes if min_bytes is not None else 1)
        except ValueError as exc:
            raise WakeWordModelNotFoundError(f"Invalid custom ONNX model: {exc}") from exc
        return candidate

    key = candidate.stem if candidate.suffix.lower() == ".onnx" else normalized
    target_dir = cache_dir if cache_dir is not None else livekit_wake_word_models_dir(data_dir)
    builtin = LIVEKIT_BUILTIN_WAKEWORDS.get(key)
    filename = str(builtin["filename"]) if builtin else f"{key}.onnx"
    target_path = target_dir / filename
    req_min_bytes = (
        int(min_bytes)
        if min_bytes is not None
        else (int(builtin["min_bytes"]) if builtin is not None else 1)
    )

    if target_path.is_file():
        try:
            _validate_onnx_file(target_path, min_bytes=req_min_bytes)
            return target_path
        except ValueError as exc:
            if builtin is None or not _is_auto_download_enabled(allow_download):
                raise WakeWordModelNotFoundError(
                    f"Cached livekit wake word model at {target_path} is invalid: {exc}"
                ) from exc
            logger.warning(
                "Cached livekit wake word model %s failed validation (%s); re-downloading...",
                target_path,
                exc,
            )

    if builtin is None:
        raise WakeWordModelNotFoundError(
            f"LiveKit wake word model '{key}' not found at {target_path} "
            "and has no built-in release URL."
        )

    if not _is_auto_download_enabled(allow_download):
        raise WakeWordModelNotFoundError(
            f"LiveKit wake word model '{key}' not found at {target_path} "
            "(automatic download disabled)."
        )

    url = str(builtin["url"])
    target_path.parent.mkdir(parents=True, exist_ok=True)
    logger.info("Downloading livekit-wakeword model '%s' from %s to %s...", key, url, target_path)

    tmp_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb",
            dir=target_path.parent,
            prefix=f".{target_path.name}.",
            suffix=".tmp",
            delete=False,
        ) as tmp_fh:
            tmp_path = Path(tmp_fh.name)
            req = urllib.request.Request(url, headers={"User-Agent": "Clea/0.1.0"})
            expected_bytes: int | None = None
            with urllib.request.urlopen(req, timeout=timeout) as resp:
                headers = getattr(resp, "headers", None)
                raw_len = headers.get("Content-Length") if headers is not None else None
                if raw_len is not None:
                    try:
                        parsed_len = int(str(raw_len).strip())
                        if parsed_len > 0:
                            expected_bytes = parsed_len
                    except ValueError:
                        expected_bytes = None
                while chunk := resp.read(65536):
                    tmp_fh.write(chunk)
            tmp_fh.flush()

        _validate_onnx_file(
            tmp_path,
            min_bytes=req_min_bytes,
            expected_bytes=expected_bytes,
        )
        tmp_path.replace(target_path)
        logger.info(
            "Saved livekit-wakeword model '%s' (%d bytes).",
            target_path.name,
            target_path.stat().st_size,
        )
        return target_path
    except Exception as exc:
        if tmp_path is not None and tmp_path.exists():
            try:
                tmp_path.unlink()
            except OSError:
                pass
        raise RuntimeError(
            f"Failed downloading livekit-wakeword model '{key}' from {url}: {exc}"
        ) from exc


def resolve_model_specs(
    model_name: str,
    data_dir: Path | None = None,
) -> list[tuple[str, str, Path]]:
    """Return list of (name, url, target_path) required for openWakeWord models."""
    target_dir = wake_word_models_dir(data_dir)
    specs: list[tuple[str, str, Path]] = []

    # Features models (melspectrogram, embedding)
    for feat_name, info in FEATURE_MODELS.items():
        specs.append((feat_name, info["url"], target_dir / info["filename"]))

    # Wake word model
    normalized = model_name.strip()
    custom_path = Path(normalized)
    if custom_path.suffix == ".onnx" or custom_path.is_file():
        # Custom file
        specs.append((custom_path.stem, "", custom_path))
    elif normalized in BUILTIN_WAKEWORDS:
        info = BUILTIN_WAKEWORDS[normalized]
        specs.append((normalized, info["url"], target_dir / info["filename"]))
    else:
        # Check if it exists as an onnx file in target_dir
        target_file = target_dir / f"{normalized}.onnx"
        specs.append((normalized, "", target_file))

    return specs


def check_models_status(
    model_name: str = "hey_jarvis",
    data_dir: Path | None = None,
) -> tuple[bool, list[tuple[str, str, Path]]]:
    """Check if all required openWakeWord models are present.

    Returns (all_present, missing_specs_list).
    """
    specs = resolve_model_specs(model_name, data_dir)
    missing = [s for s in specs if not s[2].exists()]
    return len(missing) == 0, missing


def download_wake_word_models(
    model_name: str = "hey_jarvis",
    data_dir: Path | None = None,
    print_fn: Callable[[str], None] = print,
) -> None:
    """Download missing openWakeWord fallback models to <user_data_dir>/models/."""
    target_dir = wake_word_models_dir(data_dir)
    target_dir.mkdir(parents=True, exist_ok=True)

    all_present, missing = check_models_status(model_name, data_dir)
    if all_present:
        print_fn("All wake word models already present.")
        return

    for name, url, path in missing:
        if not url:
            print_fn(f"Custom model '{name}' not found at {path} (cannot download automatically).")
            continue
        print_fn(f"Downloading {name} model from {url} to {path}...")
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp_path = path.with_suffix(".tmp")
        try:
            req = urllib.request.Request(url, headers={"User-Agent": "Clea/0.1.0"})
            with urllib.request.urlopen(req) as resp, open(tmp_path, "wb") as out_f:
                while chunk := resp.read(65536):
                    out_f.write(chunk)
            tmp_path.replace(path)
            print_fn(f"Saved {path.name}.")
        except Exception as exc:
            if tmp_path.exists():
                tmp_path.unlink()
            raise RuntimeError(f"Failed downloading {name} from {url}: {exc}") from exc


class WakeWordDetector:
    """Local wake-word detector using livekit-wakeword v2 with startup-only openWakeWord fallback.

    Strictly loads and runs a single engine per detector instance; ``livekit-wakeword``
    and ``openWakeWord`` are never instantiated simultaneously.
    """

    def __init__(
        self,
        *,
        model_name: str = "hey_jarvis",
        threshold: float | None = None,
        livekit_threshold: float | None = None,
        engine: WakeWordEngine = "auto",
        data_dir: Path | None = None,
        cache_dir: Path | None = None,
        model_instance: Any = None,
        allow_download: bool = True,
        near_miss_log_interval: float = DEFAULT_NEAR_MISS_LOG_INTERVAL_SECONDS,
    ) -> None:
        normalized_name = model_name.strip()
        requested_engine = engine.strip().lower()
        if requested_engine not in ("auto", "livekit", "openwakeword"):
            raise ValueError(
                f"Unsupported wake_word_engine {engine!r}; "
                "expected 'auto', 'livekit', or 'openwakeword'."
            )

        self.model_name = normalized_name
        self.requested_engine = requested_engine
        self.active_engine: str = requested_engine
        self.model_path: Path | None = None
        self.model_key = BUILTIN_WAKEWORDS.get(normalized_name, {}).get(
            "model_key", normalized_name
        )
        self.data_dir = data_dir
        self._cache_dir = cache_dir
        self._target_dir = wake_word_models_dir(data_dir)
        self._pending_samples: np.ndarray = np.empty(0, dtype=np.int16)
        self._lock = threading.RLock()
        self._near_miss_log_interval = max(0.0, float(near_miss_log_interval))
        self._last_near_miss_log_time: float | None = None
        self._window_start_time: float | None = None
        self._last_near_miss_seen_time: float | None = None
        self._window_peak_rms: float = 0.0
        self._window_peak_near_miss_score: float = 0.0

        if model_instance is not None:
            self._model = model_instance
            self.active_engine = (
                requested_engine
                if requested_engine in ("livekit", "openwakeword")
                else "openwakeword"
            )
            if self.active_engine == "livekit":
                self.model_key = Path(normalized_name).stem
                self.threshold = resolve_wake_threshold(
                    "livekit",
                    threshold=threshold,
                    livekit_threshold=livekit_threshold,
                )
            elif threshold is not None:
                self.threshold = float(threshold)
            elif livekit_threshold is not None:
                self.threshold = float(livekit_threshold)
            else:
                self.threshold = DEFAULT_LIVEKIT_THRESHOLD
            return

        if requested_engine == "openwakeword":
            self._load_openwakeword(
                model_name=normalized_name,
                threshold=threshold,
                data_dir=data_dir,
            )
            return

        if requested_engine == "livekit":
            self._load_livekit(
                model_name=normalized_name,
                threshold=threshold,
                livekit_threshold=livekit_threshold,
                data_dir=data_dir,
                cache_dir=cache_dir,
                allow_download=allow_download,
            )
            return

        # requested_engine == "auto": try livekit-wakeword v2 primary first,
        # falling back ONCE at startup to openWakeWord only if livekit fails.
        try:
            self._load_livekit(
                model_name=normalized_name,
                threshold=threshold,
                livekit_threshold=livekit_threshold,
                data_dir=data_dir,
                cache_dir=cache_dir,
                allow_download=allow_download,
            )
        except Exception as livekit_exc:
            logger.warning(
                "livekit-wakeword v2 unavailable (%s); falling back at startup to openWakeWord.",
                livekit_exc,
            )
            try:
                self._load_openwakeword(
                    model_name=normalized_name,
                    threshold=threshold,
                    data_dir=data_dir,
                )
            except WakeWordModelNotFoundError as oww_exc:
                raise WakeWordModelNotFoundError(
                    f"Wake word startup failed (livekit: {livekit_exc}; openWakeWord: {oww_exc})"
                ) from oww_exc

    @property
    def engine(self) -> str:
        """Return the active wake-word engine name ('livekit' or 'openwakeword')."""
        return self.active_engine

    @property
    def near_miss_floor(self) -> float:
        """Minimum score logged as a throttled INFO-level near-miss for the active engine."""
        if self.active_engine == "livekit":
            return LIVEKIT_NEAR_MISS_FLOOR
        return OPENWAKEWORD_NEAR_MISS_FLOOR

    def _load_livekit(
        self,
        *,
        model_name: str,
        threshold: float | None,
        livekit_threshold: float | None,
        data_dir: Path | None,
        cache_dir: Path | None,
        allow_download: bool,
    ) -> None:
        from livekit.wakeword import WakeWordModel

        model_path = resolve_livekit_wakeword_model(
            model_name,
            cache_dir=cache_dir,
            data_dir=data_dir,
            allow_download=allow_download,
        )
        resolved_threshold = resolve_wake_threshold(
            "livekit",
            threshold=threshold,
            livekit_threshold=livekit_threshold,
        )
        logger.info(
            "Loading livekit-wakeword v2 model from %s (threshold=%.2f)...",
            model_path,
            resolved_threshold,
        )
        self._model = WakeWordModel(
            models=[str(model_path)],
            sample_rate=WAKE_WORD_SAMPLE_RATE_HZ,
        )
        self.active_engine = "livekit"
        self.model_path = model_path
        self.model_key = model_path.stem
        self.threshold = resolved_threshold

    def _load_openwakeword(
        self,
        *,
        model_name: str,
        threshold: float | None,
        data_dir: Path | None,
    ) -> None:
        all_present, missing = check_models_status(model_name, data_dir)
        if not all_present:
            missing_names = ", ".join(m[0] for m in missing)
            raise WakeWordModelNotFoundError(
                f"Wake word model files ({missing_names}) are missing in {self._target_dir}. "
                "Run 'clea voice doctor --download-models' to download them."
            )

        specs = resolve_model_specs(model_name, data_dir)
        melspec_path = next(s[2] for s in specs if s[0] == "melspectrogram")
        embedding_path = next(s[2] for s in specs if s[0] == "embedding")
        wake_path = specs[-1][2]
        resolved_threshold = resolve_wake_threshold("openwakeword", threshold=threshold)

        from openwakeword.model import Model

        logger.info(
            "Loading openWakeWord fallback model from %s (threshold=%.2f)...",
            wake_path,
            resolved_threshold,
        )
        self._model = Model(
            wakeword_models=[str(wake_path)],
            melspec_model_path=str(melspec_path),
            embedding_model_path=str(embedding_path),
            inference_framework="onnx",
        )
        self.active_engine = "openwakeword"
        self.model_path = wake_path
        self.model_key = BUILTIN_WAKEWORDS.get(model_name, {}).get("model_key", wake_path.stem)
        self.threshold = resolved_threshold

    def _predict_single_chunk(self, chunk: np.ndarray) -> tuple[bool, float, str]:
        """Run active wake-word engine predict on a single contiguous 1D int16 16 kHz array."""
        now = time.monotonic()
        window_span = self._near_miss_log_interval
        if self._window_start_time is None:
            self._window_start_time = now
        elif window_span > 0.0:
            if (
                self._window_peak_near_miss_score == 0.0
                and (now - self._window_start_time) >= window_span
            ):
                self._window_peak_rms = 0.0
                self._window_start_time = now
            elif (
                self._window_peak_near_miss_score > 0.0
                and self._last_near_miss_seen_time is not None
                and (now - self._last_near_miss_seen_time) > (window_span * 2.0)
            ):
                self._window_peak_rms = 0.0
                self._window_peak_near_miss_score = 0.0
                self._last_near_miss_seen_time = None
                self._window_start_time = now

        contiguous = np.ascontiguousarray(chunk, dtype=np.int16)
        chunk_rms = compute_audio_rms(contiguous)
        if chunk_rms > self._window_peak_rms:
            self._window_peak_rms = chunk_rms

        predictions = self._model.predict(contiguous)

        if predictions:
            if self.model_key in predictions:
                best_score = float(predictions[self.model_key])
                best_key = self.model_key
            elif self.model_name in predictions:
                best_score = float(predictions[self.model_name])
                best_key = self.model_name
            else:
                best_key, best_val = max(predictions.items(), key=lambda item: float(item[1]))
                best_score = float(best_val)
        else:
            best_score = 0.0
            best_key = self.model_key

        if best_score >= self.threshold:
            logger.info(
                "Wake word detected: engine=%s, model=%s, score=%.3f "
                "(threshold=%.2f, peak_rms=%.1f)",
                self.active_engine,
                best_key,
                best_score,
                self.threshold,
                self._window_peak_rms,
            )
            self._window_peak_rms = 0.0
            self._window_peak_near_miss_score = 0.0
            self._last_near_miss_seen_time = None
            self._window_start_time = now
            return True, best_score, best_key

        if best_score >= self.near_miss_floor:
            if best_score > self._window_peak_near_miss_score:
                self._window_peak_near_miss_score = best_score
            self._last_near_miss_seen_time = now
            if (
                self._last_near_miss_log_time is None
                or (now - self._last_near_miss_log_time) >= self._near_miss_log_interval
            ):
                logger.info(
                    "Wake word near-miss: engine=%s, model=%s, score=%.3f "
                    "(threshold=%.2f, peak_rms=%.1f)",
                    self.active_engine,
                    best_key,
                    self._window_peak_near_miss_score,
                    self.threshold,
                    self._window_peak_rms,
                )
                self._last_near_miss_log_time = now
                self._window_peak_rms = 0.0
                self._window_peak_near_miss_score = 0.0
                self._last_near_miss_seen_time = None
                self._window_start_time = now
            else:
                logger.debug(
                    "Wake word near-miss (throttled): engine=%s, model=%s, score=%.3f "
                    "(threshold=%.2f, rms=%.1f)",
                    self.active_engine,
                    best_key,
                    best_score,
                    self.threshold,
                    chunk_rms,
                )

        return False, best_score, best_key

    def process_audio(
        self,
        pcm_bytes: bytes | np.ndarray,
        *,
        sample_rate: int = WAKE_WORD_SAMPLE_RATE_HZ,
    ) -> tuple[bool, float, str]:
        """Process audio into guaranteed mono 16 kHz int16 chunks for the active engine.

        Normalized float inputs in ``[-1.0, 1.0]``, multi-channel arrays, and non-16 kHz
        sample rates are automatically scaled, downmixed, and resampled. Multi-frame buffers
        larger than 1280 samples (80 ms) are stepped into exact 1280-sample chunks.

        Returns (detected: bool, score: float, model_key: str).
        """
        if pcm_bytes is None:
            return False, 0.0, ""

        if isinstance(pcm_bytes, np.ndarray):
            if pcm_bytes.size == 0:
                return False, 0.0, ""
            audio_arr = resample_to_16k_mono(
                pcm_bytes,
                orig_sr=sample_rate,
                target_sr=WAKE_WORD_SAMPLE_RATE_HZ,
            )
        else:
            if not pcm_bytes:
                return False, 0.0, ""
            usable_len = len(pcm_bytes) - (len(pcm_bytes) % 2)
            if usable_len == 0:
                return False, 0.0, ""
            raw_arr = np.frombuffer(pcm_bytes[:usable_len], dtype=np.int16)
            if sample_rate != WAKE_WORD_SAMPLE_RATE_HZ:
                audio_arr = resample_to_16k_mono(
                    raw_arr,
                    orig_sr=sample_rate,
                    target_sr=WAKE_WORD_SAMPLE_RATE_HZ,
                )
            else:
                audio_arr = np.ascontiguousarray(raw_arr, dtype=np.int16)

        if len(audio_arr) == 0:
            return False, 0.0, ""

        with self._lock:
            if self._pending_samples.size > 0:
                audio_arr = np.concatenate((self._pending_samples, audio_arr))
                self._pending_samples = np.empty(0, dtype=np.int16)

            if len(audio_arr) <= WAKE_WORD_CHUNK_SAMPLES:
                return self._predict_single_chunk(audio_arr)

            # Step multi-frame buffers in exact 1280-sample (80 ms) chunks.
            best_score = 0.0
            best_key = self.model_key
            detected_any = False
            offset = 0
            total = len(audio_arr)
            while offset + WAKE_WORD_CHUNK_SAMPLES <= total:
                chunk = audio_arr[offset : offset + WAKE_WORD_CHUNK_SAMPLES]
                offset += WAKE_WORD_CHUNK_SAMPLES
                det, score, key = self._predict_single_chunk(chunk)
                if score >= best_score:
                    best_score = score
                    best_key = key
                if det:
                    detected_any = True

            if offset < total:
                self._pending_samples = np.ascontiguousarray(audio_arr[offset:], dtype=np.int16)

            return detected_any, best_score, best_key

    def reset(self) -> None:
        """Reset internal prediction buffers and preprocessor/model state."""
        with self._lock:
            self._pending_samples = np.empty(0, dtype=np.int16)
            self._window_peak_rms = 0.0
            self._window_peak_near_miss_score = 0.0
            self._window_start_time = None
            self._last_near_miss_seen_time = None
            if hasattr(self._model, "reset"):
                try:
                    self._model.reset()
                    return
                except Exception as exc:
                    logger.debug("Failed calling model.reset(): %s", exc)
            if hasattr(self._model, "prediction_buffer"):
                self._model.prediction_buffer.clear()

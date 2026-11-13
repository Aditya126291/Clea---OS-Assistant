"""Wake word detection using openwakeword loaded from <user_data_dir>/models/."""

from __future__ import annotations

import logging
import urllib.request
from collections.abc import Callable
from pathlib import Path
from typing import Any

import numpy as np

from clea.paths import default_data_dir

logger = logging.getLogger(__name__)

MODELS_SUBDIR = "models"

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


def resolve_model_specs(
    model_name: str,
    data_dir: Path | None = None,
) -> list[tuple[str, str, Path]]:
    """Return list of (name, url, target_path) required for the given wake word."""
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
    """Check if all required models are present.

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
    """Download missing wake word models to <user_data_dir>/models/."""
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
    """Local openwakeword detector processing 16 kHz audio chunks."""

    def __init__(
        self,
        *,
        model_name: str = "hey_jarvis",
        threshold: float = 0.5,
        data_dir: Path | None = None,
        model_instance: Any = None,
    ) -> None:
        self.model_name = model_name
        self.threshold = threshold
        self.data_dir = data_dir
        self._target_dir = wake_word_models_dir(data_dir)

        if model_instance is not None:
            self._model = model_instance
            return

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

        from openwakeword.model import Model

        logger.info("Loading openwakeword model from %s...", wake_path)
        self._model = Model(
            wakeword_models=[str(wake_path)],
            melspec_model_path=str(melspec_path),
            embedding_model_path=str(embedding_path),
            inference_framework="onnx",
        )

    def process_audio(self, pcm_bytes: bytes) -> tuple[bool, float, str]:
        """Process 16 kHz 16-bit mono audio chunk.

        Returns (detected: bool, score: float, model_key: str).
        """
        if not pcm_bytes:
            return False, 0.0, ""

        audio_arr = np.frombuffer(pcm_bytes, dtype=np.int16)
        predictions = self._model.predict(audio_arr)

        best_score = 0.0
        best_key = ""
        for key, score in predictions.items():
            s = float(score)
            if s > best_score:
                best_score = s
                best_key = key

        if best_score >= self.threshold:
            logger.info(
                "Wake word detected: model=%s, score=%.3f (threshold=%.2f)",
                best_key,
                best_score,
                self.threshold,
            )
            return True, best_score, best_key

        return False, best_score, best_key

    def reset(self) -> None:
        """Reset internal prediction buffers."""
        if hasattr(self._model, "prediction_buffer"):
            self._model.prediction_buffer.clear()

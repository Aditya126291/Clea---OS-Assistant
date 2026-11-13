"""Unit tests for wake word model loading, missing model diagnostics, and detection."""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from clea.surfaces.voice.wake_word import (
    WakeWordDetector,
    WakeWordModelNotFoundError,
    check_models_status,
    download_wake_word_models,
    resolve_model_specs,
    wake_word_models_dir,
)


def test_wake_word_models_dir(tmp_path: Path) -> None:
    assert wake_word_models_dir(tmp_path) == tmp_path / "models"


def test_resolve_model_specs_builtin() -> None:
    specs = resolve_model_specs("hey_jarvis")
    names = [s[0] for s in specs]
    assert "melspectrogram" in names
    assert "embedding" in names
    assert "hey_jarvis" in names
    for _name, url, path in specs:
        assert path.name.endswith(".onnx")
        assert url.startswith("https://")


def test_resolve_model_specs_custom_path(tmp_path: Path) -> None:
    custom_model = tmp_path / "my_custom.onnx"
    specs = resolve_model_specs(str(custom_model), tmp_path)
    wake_spec = specs[-1]
    assert wake_spec[0] == "my_custom"
    assert wake_spec[2] == custom_model


def test_check_models_status_empty_dir(tmp_path: Path) -> None:
    all_ok, missing = check_models_status("hey_jarvis", data_dir=tmp_path)
    assert not all_ok
    assert len(missing) == 3


def test_wake_word_missing_models_raises_clear_error(tmp_path: Path) -> None:
    with pytest.raises(WakeWordModelNotFoundError) as exc_info:
        WakeWordDetector(data_dir=tmp_path)
    msg = str(exc_info.value)
    assert "clea voice doctor --download-models" in msg
    assert str(tmp_path / "models") in msg


def test_wake_word_detector_prediction_with_mock() -> None:
    mock_model = MagicMock()
    mock_model.predict.return_value = {"hey_jarvis_v0.1": 0.85}

    detector = WakeWordDetector(
        model_name="hey_jarvis",
        threshold=0.5,
        model_instance=mock_model,
    )

    audio_bytes = b"\x00\x01" * 160
    detected, score, key = detector.process_audio(audio_bytes)
    assert detected is True
    assert score == 0.85
    assert key == "hey_jarvis_v0.1"

    # Below threshold
    mock_model.predict.return_value = {"hey_jarvis_v0.1": 0.3}
    detected, score, key = detector.process_audio(audio_bytes)
    assert detected is False
    assert score == 0.3


def test_download_wake_word_models_downloads_to_target(tmp_path: Path) -> None:
    messages: list[str] = []

    fake_bytes = b"ONNX_MODEL_BINARY"

    class FakeResponse:
        def __init__(self) -> None:
            self._data = io.BytesIO(fake_bytes)

        def read(self, size: int = -1) -> bytes:
            return self._data.read(size)

        def __enter__(self) -> FakeResponse:
            return self

        def __exit__(self, *args: object) -> None:
            pass

    with patch("urllib.request.urlopen", return_value=FakeResponse()):
        download_wake_word_models(
            "hey_jarvis",
            data_dir=tmp_path,
            print_fn=messages.append,
        )

    models_dir = tmp_path / "models"
    assert (models_dir / "melspectrogram.onnx").exists()
    assert (models_dir / "embedding_model.onnx").exists()
    assert (models_dir / "hey_jarvis_v0.1.onnx").exists()
    assert any("Downloaded" in m or "Saved" in m for m in messages)

    # Calling again detects all models present
    messages.clear()
    download_wake_word_models("hey_jarvis", data_dir=tmp_path, print_fn=messages.append)
    assert "All wake word models already present." in messages[0]

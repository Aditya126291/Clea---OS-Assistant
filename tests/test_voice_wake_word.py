"""Unit tests for wake word model loading, missing model diagnostics, and detection."""

from __future__ import annotations

import io
import wave
from pathlib import Path
from unittest.mock import MagicMock, patch

import numpy as np
import pytest
import scipy.signal

from clea.surfaces.voice.audio import (
    AudioChunkBuffer,
    compute_audio_rms,
    resample_to_16k_mono,
)
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


def test_wake_word_detector_prediction_score_threshold_trigger() -> None:
    mock_model = MagicMock()

    # Just below threshold (0.49 < 0.50)
    mock_model.predict.return_value = {"hey_jarvis_v0.1": 0.49}
    detector = WakeWordDetector(
        model_name="hey_jarvis",
        threshold=0.50,
        model_instance=mock_model,
    )
    detected, score, key = detector.process_audio(np.zeros(1280, dtype=np.int16))
    assert detected is False
    assert score == 0.49
    assert key == "hey_jarvis_v0.1"

    # Exactly at threshold (0.50 >= 0.50)
    mock_model.predict.return_value = {"hey_jarvis_v0.1": 0.50}
    detected, score, key = detector.process_audio(np.zeros(1280, dtype=np.int16))
    assert detected is True
    assert score == 0.50

    # Above threshold (0.95 >= 0.50)
    mock_model.predict.return_value = {"hey_jarvis_v0.1": 0.95}
    detected, score, key = detector.process_audio(np.zeros(1280, dtype=np.int16))
    assert detected is True
    assert score == 0.95


def test_wake_word_detector_key_resolution_and_fallback() -> None:
    mock_model = MagicMock()
    mock_model.predict.return_value = {"other_model": 0.72}

    detector = WakeWordDetector(
        model_name="hey_jarvis",
        threshold=0.5,
        model_instance=mock_model,
    )

    detected, score, key = detector.process_audio(np.zeros(1280, dtype=np.int16))
    assert detected is True
    assert score == 0.72
    assert key == "other_model"


def test_wake_word_detector_reset_delegates_to_model_reset() -> None:
    mock_model = MagicMock()
    detector = WakeWordDetector(
        model_name="hey_jarvis",
        threshold=0.5,
        model_instance=mock_model,
    )
    detector.reset()
    mock_model.reset.assert_called_once()


def test_wake_word_detector_reset_fallback_prediction_buffer() -> None:
    mock_model = MagicMock(spec=["prediction_buffer"])
    mock_model.prediction_buffer = MagicMock()
    detector = WakeWordDetector(
        model_name="hey_jarvis",
        threshold=0.5,
        model_instance=mock_model,
    )
    detector.reset()
    mock_model.prediction_buffer.clear.assert_called_once()


def test_wake_word_detector_input_types_and_empty() -> None:
    mock_model = MagicMock()
    mock_model.predict.return_value = {"hey_jarvis_v0.1": 0.1}

    detector = WakeWordDetector(
        model_name="hey_jarvis",
        threshold=0.5,
        model_instance=mock_model,
    )

    # Empty inputs should return immediately without calling predict
    assert detector.process_audio(b"") == (False, 0.0, "")
    assert detector.process_audio(np.empty(0, dtype=np.int16)) == (False, 0.0, "")
    assert detector.process_audio(None) == (False, 0.0, "")  # type: ignore[arg-type]
    mock_model.predict.assert_not_called()

    # Valid 1D array
    arr = np.zeros(1280, dtype=np.int16)
    detected, score, key = detector.process_audio(arr)
    assert score == 0.1
    mock_model.predict.assert_called_once()


@pytest.mark.skipif(
    not check_models_status("hey_jarvis")[0],
    reason="Wake word ONNX models not downloaded",
)
def test_wake_word_scoring_negative_silence() -> None:
    detector = WakeWordDetector(model_name="hey_jarvis", threshold=0.5)
    silence_chunk = np.zeros(1280, dtype=np.int16)

    for _ in range(10):
        detected, score, key = detector.process_audio(silence_chunk)
        assert detected is False
        assert score < 0.01


@pytest.mark.skipif(
    not check_models_status("hey_jarvis")[0],
    reason="Wake word ONNX models not downloaded",
)
def test_wake_word_scoring_negative_noise() -> None:
    detector = WakeWordDetector(model_name="hey_jarvis", threshold=0.5)
    rng = np.random.default_rng(123)

    for _ in range(10):
        noise = rng.uniform(-1000, 1000, 1280).astype(np.int16)
        detected, score, key = detector.process_audio(noise)
        assert detected is False
        assert score < 0.05


@pytest.mark.skipif(
    not (check_models_status("hey_jarvis")[0] and Path("test_jarvis_16k.wav").exists()),
    reason="Wake word models or test_jarvis_16k.wav fixture not available",
)
def test_wake_word_resampled_48k_speech_pipeline() -> None:
    with wave.open("test_jarvis_16k.wav", "rb") as wf:
        raw_frames = wf.readframes(wf.getnframes())
        audio_16k = np.frombuffer(raw_frames, dtype=np.int16)

    # 1. Upsample 16 kHz to 48 kHz
    audio_48k = scipy.signal.resample_poly(audio_16k.astype(np.float32), 3, 1)
    audio_48k = np.clip(np.round(audio_48k), -32768, 32767).astype(np.int16)

    # 2. Resample 48 kHz back to 16 kHz using shared helper
    resampled_16k = resample_to_16k_mono(audio_48k, orig_sr=48000, target_sr=16000)
    assert resampled_16k.dtype == np.int16

    # 3. Buffer into AudioChunkBuffer
    buffer = AudioChunkBuffer(chunk_size=1280)
    buffer.extend(resampled_16k)
    chunks = buffer.get_chunks()
    assert len(chunks) > 0

    # 4. Stream chunks through detector and verify wake word detection (>0.50)
    detector = WakeWordDetector(model_name="hey_jarvis", threshold=0.5)
    max_score = 0.0
    detected_count = 0
    max_rms = 0.0

    for chunk in chunks:
        assert len(chunk) == 1280
        rms = compute_audio_rms(chunk)
        if rms > max_rms:
            max_rms = rms

        detected, score, key = detector.process_audio(chunk)
        if score > max_score:
            max_score = score
        if detected:
            detected_count += 1
            assert key == "hey_jarvis_v0.1"

    assert max_rms > 500.0, f"Expected active speech RMS > 500, got {max_rms}"
    assert max_score > 0.50, f"Expected wake word max score > 0.50, got {max_score}"
    assert detected_count >= 1, "Expected at least one wake word trigger"

"""Adversarial and empirical stress tests for openWakeWord detector and probe scoring."""

from __future__ import annotations

import math
import wave
from pathlib import Path

import numpy as np
import pytest

from clea.surfaces.voice.audio import (
    AudioChunkBuffer,
    compute_audio_rms,
)
from clea.surfaces.voice.wake_word import (
    WakeWordDetector,
    check_models_status,
)

MODELS_AVAILABLE = check_models_status("hey_jarvis")[0]
WAV_FIXTURE_EXISTS = Path("test_jarvis_16k.wav").exists()


# ---------------------------------------------------------------------------
# Test Suite 1: Prediction score accuracy on speech vs noise vs silence
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (MODELS_AVAILABLE and WAV_FIXTURE_EXISTS),
    reason="Wake word ONNX models or test_jarvis_16k.wav fixture not available",
)
def test_wake_word_speech_scoring_accuracy() -> None:
    """Valid speech must produce wake word score > 0.50 (empirically > 0.95)."""
    with wave.open("test_jarvis_16k.wav", "rb") as wf:
        raw = wf.readframes(wf.getnframes())
        speech_16k = np.frombuffer(raw, dtype=np.int16)

    detector = WakeWordDetector(model_name="hey_jarvis", threshold=0.50)
    buf = AudioChunkBuffer(chunk_size=1280)
    buf.extend(speech_16k)
    chunks = buf.get_chunks()

    scores = [detector.process_audio(c)[1] for c in chunks]
    max_score = max(scores)

    assert max_score > 0.50, f"Expected peak score > 0.50, got {max_score:.4f}"
    assert max_score >= 0.95, f"Expected high-confidence detection >= 0.95, got {max_score:.4f}"


@pytest.mark.skipif(not MODELS_AVAILABLE, reason="Wake word ONNX models not downloaded")
def test_wake_word_silence_scoring_accuracy() -> None:
    """Silence over 100 consecutive chunks (8 seconds) must remain strictly < 0.05 (and < 0.01)."""
    detector = WakeWordDetector(model_name="hey_jarvis", threshold=0.50)
    detector.reset()

    silence_chunk = np.zeros(1280, dtype=np.int16)
    for _ in range(100):
        detected, score, _ = detector.process_audio(silence_chunk)
        assert detected is False
        assert score < 0.01, f"Expected silence score < 0.01, got {score}"


@pytest.mark.skipif(not MODELS_AVAILABLE, reason="Wake word ONNX models not downloaded")
def test_wake_word_dc_offset_scoring_accuracy() -> None:
    """Constant DC offsets across audio range must score < 0.05."""
    detector = WakeWordDetector(model_name="hey_jarvis", threshold=0.50)
    for dc_val in (5, 50, -50, 500, -500, 5000, -5000):
        detector.reset()
        chunk = np.full(1280, dc_val, dtype=np.int16)
        for _ in range(20):
            detected, score, _ = detector.process_audio(chunk)
            assert detected is False
            assert score < 0.01, f"Expected DC offset {dc_val} score < 0.01, got {score}"


@pytest.mark.skipif(not MODELS_AVAILABLE, reason="Wake word ONNX models not downloaded")
def test_wake_word_tonal_noise_scoring_accuracy() -> None:
    """Sinusoidal acoustic tones (100 Hz to 6 kHz) at high volume must score < 0.05."""
    detector = WakeWordDetector(model_name="hey_jarvis", threshold=0.50)
    t = np.arange(1280 * 20) / 16000.0

    for freq in (100, 440, 1000, 2500, 4000, 6000):
        detector.reset()
        tone = (25000 * np.sin(2 * np.pi * freq * t)).astype(np.int16)
        for i in range(20):
            chunk = tone[i * 1280 : (i + 1) * 1280]
            detected, score, _ = detector.process_audio(chunk)
            assert detected is False
            assert score < 0.05, f"Expected tone {freq}Hz score < 0.05, got {score}"


@pytest.mark.skipif(not MODELS_AVAILABLE, reason="Wake word ONNX models not downloaded")
def test_wake_word_noise_never_triggers_false_detection() -> None:
    """Loud random noise (uniform and gaussian) must never trigger false positive (score < 0.50)."""
    detector = WakeWordDetector(model_name="hey_jarvis", threshold=0.50)
    rng = np.random.default_rng(999)

    # Test uniform noise across diverse amplitude scales
    for amp in (1000, 5000, 15000, 28000):
        detector.reset()
        for _ in range(25):
            noise_chunk = rng.integers(-amp, amp, size=1280, dtype=np.int16)
            detected, score, _ = detector.process_audio(noise_chunk)
            msg = f"False detection on uniform noise amp={amp} (score={score})"
            assert detected is False, msg
            assert score < 0.50

    # Test gaussian noise
    for sigma in (500, 2000, 6000):
        detector.reset()
        for _ in range(25):
            noise_chunk = np.clip(rng.normal(0, sigma, size=1280), -32768, 32767).astype(np.int16)
            detected, score, _ = detector.process_audio(noise_chunk)
            msg = f"False detection on gaussian noise sigma={sigma} (score={score})"
            assert detected is False, msg
            assert score < 0.50


# ---------------------------------------------------------------------------
# Test Suite 2: Robustness of WakeWordDetector.process_audio
# ---------------------------------------------------------------------------


@pytest.mark.skipif(not MODELS_AVAILABLE, reason="Wake word ONNX models not downloaded")
def test_wake_word_varying_chunk_sizes() -> None:
    """Detector must process chunks smaller and larger than 1280 samples without crashing."""
    detector = WakeWordDetector(model_name="hey_jarvis", threshold=0.50)
    for csize in (1, 16, 64, 128, 320, 640, 1000, 1280, 1281, 2560, 5120):
        chunk = np.zeros(csize, dtype=np.int16)
        detected, score, key = detector.process_audio(chunk)
        assert isinstance(detected, bool)
        assert isinstance(score, float)
        assert isinstance(key, str)


@pytest.mark.skipif(not MODELS_AVAILABLE, reason="Wake word ONNX models not downloaded")
def test_wake_word_non_contiguous_memory_layouts() -> None:
    """Detector must handle non-contiguous, strided, reversed, and Fortran arrays."""
    detector = WakeWordDetector(model_name="hey_jarvis", threshold=0.50)
    base = np.zeros(4000, dtype=np.int16)

    strided_2 = base[::2][:1280]
    assert not strided_2.flags["C_CONTIGUOUS"]
    det, score, key = detector.process_audio(strided_2)
    assert not det

    strided_3 = base[::3][:1280]
    assert not strided_3.flags["C_CONTIGUOUS"]
    det, score, key = detector.process_audio(strided_3)
    assert not det

    reversed_arr = base[:1280][::-1]
    assert not reversed_arr.flags["C_CONTIGUOUS"]
    det, score, key = detector.process_audio(reversed_arr)
    assert not det

    fortran_slice = np.asfortranarray(np.zeros((1280, 2), dtype=np.int16))[:, 0]
    det, score, key = detector.process_audio(fortran_slice)
    assert not det


@pytest.mark.skipif(not MODELS_AVAILABLE, reason="Wake word ONNX models not downloaded")
def test_wake_word_unexpected_dtypes_and_nan_inf() -> None:
    """Detector must handle float, int dtypes, and NaN/Inf without unhandled exceptions."""
    detector = WakeWordDetector(model_name="hey_jarvis", threshold=0.50)

    for dtype in (np.float32, np.float64, np.int32, np.int8, np.uint8, bool):
        arr = np.zeros(1280, dtype=dtype)
        det, score, key = detector.process_audio(arr)
        assert det is False
        assert score < 0.01

    # NaN and Inf in float inputs
    nan_arr = np.full(1280, np.nan, dtype=np.float32)
    det, score, key = detector.process_audio(nan_arr)
    assert det is False

    inf_arr = np.full(1280, np.inf, dtype=np.float32)
    det, score, key = detector.process_audio(inf_arr)
    assert det is False


@pytest.mark.skipif(not MODELS_AVAILABLE, reason="Wake word ONNX models not downloaded")
def test_wake_word_extreme_rail_saturation() -> None:
    """Full-scale saturation (+32767, -32768, rail square wave) must not fail or false trigger."""
    detector = WakeWordDetector(model_name="hey_jarvis", threshold=0.50)

    # Positive rail saturation
    pos_rail = np.full(1280, 32767, dtype=np.int16)
    det, score, key = detector.process_audio(pos_rail)
    assert det is False
    assert score < 0.01

    # Negative rail saturation
    neg_rail = np.full(1280, -32768, dtype=np.int16)
    det, score, key = detector.process_audio(neg_rail)
    assert det is False
    assert score < 0.01

    # Alternating maximum rail square wave
    pattern = [32767 if (i // 8) % 2 == 0 else -32768 for i in range(1280)]
    square_rail = np.array(pattern, dtype=np.int16)
    det, score, key = detector.process_audio(square_rail)
    assert det is False
    assert score < 0.01


# ---------------------------------------------------------------------------
# Test Suite 3: State isolation and reset() purging
# ---------------------------------------------------------------------------


@pytest.mark.skipif(
    not (MODELS_AVAILABLE and WAV_FIXTURE_EXISTS),
    reason="Wake word ONNX models or test_jarvis_16k.wav fixture not available",
)
def test_wake_word_reset_state_isolation() -> None:
    """Calling detector.reset() must completely purge internal buffers and prevent contamination."""
    with wave.open("test_jarvis_16k.wav", "rb") as wf:
        raw = wf.readframes(wf.getnframes())
        speech_16k = np.frombuffer(raw, dtype=np.int16)

    buf = AudioChunkBuffer(chunk_size=1280)
    buf.extend(speech_16k)
    speech_chunks = buf.get_chunks()

    detector = WakeWordDetector(model_name="hey_jarvis", threshold=0.50)

    # 1. Feed partial speech (first 12 chunks)
    for chunk in speech_chunks[:12]:
        detector.process_audio(chunk)

    # 2. Reset the detector
    detector.reset()

    # 3. Verify internal buffers are empty
    model = detector._model
    prep = getattr(model, "preprocessor", None)
    assert prep is not None
    assert len(prep.raw_data_buffer) == 0
    assert len(prep.raw_data_remainder) == 0
    assert prep.accumulated_samples == 0
    for _key, q in model.prediction_buffer.items():
        assert len(q) == 0

    # 4. Verify subsequent silence immediately produces near-zero scores
    for _ in range(10):
        det, score, _ = detector.process_audio(np.zeros(1280, dtype=np.int16))
        assert det is False
        assert score < 0.001


# ---------------------------------------------------------------------------
# Test Suite 4: Probe RMS calculation correctness
# ---------------------------------------------------------------------------


def test_compute_audio_rms_analytical_sine() -> None:
    """RMS of pure sine wave must match theoretical A / sqrt(2) within quantization error."""
    for amp in (100, 1000, 5000, 15000, 32767):
        t = np.arange(16000) / 16000.0  # 1s integer cycles at 1 kHz
        wave_arr = (amp * np.sin(2 * np.pi * 1000 * t)).astype(np.int16)
        computed = compute_audio_rms(wave_arr)
        expected = amp / math.sqrt(2)
        rel_err = abs(computed - expected) / expected
        msg = f"Sine amp={amp}: computed={computed}, expected={expected}, rel_err={rel_err}"
        assert rel_err < 0.005, msg


def test_compute_audio_rms_analytical_dc_and_square() -> None:
    """RMS of DC offset C must be |C|; RMS of square wave A must be A."""
    for dc in (0, 1, 10, 100, 1000, 10000, 32767):
        arr = np.full(1280, dc, dtype=np.int16)
        assert abs(compute_audio_rms(arr) - float(abs(dc))) < 1e-4

    for amp in (100, 1000, 32767):
        arr = np.array([amp if i % 2 == 0 else -amp for i in range(1280)], dtype=np.int16)
        assert abs(compute_audio_rms(arr) - float(amp)) < 1e-4


def test_compute_audio_rms_edge_cases() -> None:
    """compute_audio_rms must safely return 0.0 for None, empty bytes, and empty arrays."""
    assert compute_audio_rms(None) == 0.0
    assert compute_audio_rms(b"") == 0.0
    assert compute_audio_rms(np.empty(0, dtype=np.int16)) == 0.0

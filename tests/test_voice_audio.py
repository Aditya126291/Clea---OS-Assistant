"""Unit tests for AudioManager with barge-in flush and mic muting."""

from __future__ import annotations

import concurrent.futures
from unittest.mock import patch

import numpy as np
import pytest
import sounddevice as sd

from clea.surfaces.voice.audio import (
    AmbiguousAudioDeviceError,
    AudioChunkBuffer,
    AudioDeviceNotFoundError,
    AudioManager,
    compute_audio_rms,
    list_audio_devices,
    negotiate_input_sample_rate,
    refresh_audio_devices,
    resample_to_16k_mono,
    resolve_audio_device,
)


def generate_synthetic_sine(
    freq_hz: float,
    duration_s: float,
    sample_rate: int,
    amplitude: float = 16000.0,
    channels: int = 1,
) -> np.ndarray:
    """Generate deterministic int16 sine wave audio array."""
    num_samples = int(sample_rate * duration_s)
    t = np.linspace(0, duration_s, num_samples, endpoint=False)
    waveform = amplitude * np.sin(2 * np.pi * freq_hz * t)
    int_waveform = np.clip(np.round(waveform), -32768, 32767).astype(np.int16)
    if channels == 1:
        return int_waveform
    return np.repeat(int_waveform[:, np.newaxis], channels, axis=1)


def generate_synthetic_silence(
    duration_s: float, sample_rate: int, channels: int = 1
) -> np.ndarray:
    """Generate silence (all zeros) int16 array."""
    num_samples = int(sample_rate * duration_s)
    if channels == 1:
        return np.zeros(num_samples, dtype=np.int16)
    return np.zeros((num_samples, channels), dtype=np.int16)


def generate_synthetic_noise(
    duration_s: float,
    sample_rate: int,
    amplitude: float = 1000.0,
    seed: int = 42,
) -> np.ndarray:
    """Generate low-amplitude white noise int16 array."""
    num_samples = int(sample_rate * duration_s)
    rng = np.random.default_rng(seed)
    noise = rng.uniform(-amplitude, amplitude, num_samples)
    return np.clip(np.round(noise), -32768, 32767).astype(np.int16)


def test_list_audio_devices_returns_valid_structure() -> None:
    devices = list_audio_devices()
    assert isinstance(devices, list)
    for dev in devices:
        assert "name" in dev
        assert "max_input_channels" in dev
        assert "max_output_channels" in dev


def test_audio_manager_barge_in_clears_queue() -> None:
    manager = AudioManager(mic_mute_while_speaking=True)
    assert not manager.is_speaking

    # Play chunks
    chunk1 = b"\x01\x00" * 240
    chunk2 = b"\x02\x00" * 240
    manager.play_audio(chunk1)
    # Since running is False, play_audio doesn't queue unless running
    manager._running = True
    manager.play_audio(chunk1)
    manager.play_audio(chunk2)
    assert manager.is_speaking
    assert not manager._output_queue.empty()

    # Barge-in stop_output
    manager.stop_output()
    assert not manager.is_speaking
    assert manager._output_queue.empty()
    assert len(manager._current_output_bytes) == 0


def test_audio_manager_mic_mute_while_speaking() -> None:
    received: list[bytes] = []

    def on_input(data: bytes) -> None:
        received.append(data)

    manager = AudioManager(mic_mute_while_speaking=True, on_audio_input=on_input)
    fake_audio = np.ones((160, 1), dtype=np.int16)

    # 1. Not speaking: input is forwarded
    manager._input_stream_callback(fake_audio, 160, None, 0)  # type: ignore[arg-type]
    assert len(received) == 1
    assert received[0] == fake_audio.tobytes()

    # 2. Speaking: input is muted/dropped
    manager._is_speaking.set()
    manager._input_stream_callback(fake_audio, 160, None, 0)  # type: ignore[arg-type]
    assert len(received) == 1  # no new chunk added

    # 3. Speaking stopped: input flows again
    manager._is_speaking.clear()
    manager._input_stream_callback(fake_audio, 160, None, 0)  # type: ignore[arg-type]
    assert len(received) == 2


def test_audio_manager_output_callback_zero_fills() -> None:
    manager = AudioManager()
    out = np.empty((100, 1), dtype=np.int16)
    manager._output_stream_callback(out, 100, None, 0)  # type: ignore[arg-type]
    assert np.all(out == 0)
    assert not manager.is_speaking


def test_audio_manager_start_and_stop_lifecycle_no_deadlock() -> None:
    """Verify start() and stop() complete cleanly without deadlocking on lock or stream closing."""
    manager = AudioManager()
    manager.start()
    assert manager._running is True
    manager.stop()
    assert manager._running is False
    assert manager._input_stream is None
    assert manager._output_stream is None


def test_resolve_audio_device_by_substring_and_preferred_hostapi() -> None:
    mock_devices = [
        {
            "index": 0,
            "name": "USB Mic",
            "hostapi": 0,
            "hostapi_name": "MME",
            "max_input_channels": 1,
            "max_output_channels": 0,
            "default_samplerate": 44100,
        },
        {
            "index": 1,
            "name": "USB Mic",
            "hostapi": 1,
            "hostapi_name": "Windows WASAPI",
            "max_input_channels": 1,
            "max_output_channels": 0,
            "default_samplerate": 16000,
        },
        {
            "index": 2,
            "name": "Speakers",
            "hostapi": 1,
            "hostapi_name": "Windows WASAPI",
            "max_input_channels": 0,
            "max_output_channels": 2,
            "default_samplerate": 48000,
        },
    ]

    with patch("clea.surfaces.voice.audio.list_audio_devices", return_value=mock_devices):
        # 1. Resolves cleanly via preferred WASAPI hostapi
        dev = resolve_audio_device("USB Mic", kind="input", preferred_hostapi="WASAPI")
        assert dev["index"] == 1
        assert dev["hostapi_name"] == "Windows WASAPI"

        # 2. Resolves output device
        out_dev = resolve_audio_device("Speakers", kind="output")
        assert out_dev["index"] == 2

        # 3. Non-existent substring raises AudioDeviceNotFoundError
        with pytest.raises(AudioDeviceNotFoundError) as exc_nf:
            resolve_audio_device("BluetoothHeadset", kind="input")
        assert "BluetoothHeadset" in str(exc_nf.value)

        # 4. Out of range integer index raises AudioDeviceNotFoundError
        with pytest.raises(AudioDeviceNotFoundError):
            resolve_audio_device(999, kind="input")


def test_resolve_audio_device_raises_on_ambiguous_matches() -> None:
    mock_devices = [
        {
            "index": 0,
            "name": "Realtek Mic 1",
            "hostapi": 0,
            "hostapi_name": "Windows WASAPI",
            "max_input_channels": 1,
            "max_output_channels": 0,
            "default_samplerate": 16000,
        },
        {
            "index": 1,
            "name": "Realtek Mic 2",
            "hostapi": 0,
            "hostapi_name": "Windows WASAPI",
            "max_input_channels": 1,
            "max_output_channels": 0,
            "default_samplerate": 16000,
        },
    ]

    with patch("clea.surfaces.voice.audio.list_audio_devices", return_value=mock_devices):
        with pytest.raises(AmbiguousAudioDeviceError) as exc_amb:
            resolve_audio_device("Realtek", kind="input", preferred_hostapi="WASAPI")
        assert "Multiple audio input devices match 'Realtek'" in str(exc_amb.value)


def test_resolve_audio_device_none_spec_respects_preferred_hostapi() -> None:
    mock_devices = [
        {
            "index": 0,
            "name": "MME Mic",
            "hostapi": 0,
            "hostapi_name": "MME",
            "max_input_channels": 1,
            "max_output_channels": 0,
            "default_samplerate": 44100,
        },
        {
            "index": 1,
            "name": "WASAPI Mic",
            "hostapi": 1,
            "hostapi_name": "Windows WASAPI",
            "max_input_channels": 1,
            "max_output_channels": 0,
            "default_samplerate": 48000,
        },
    ]
    mock_hostapis = (
        {"name": "MME", "default_input_device": 0, "default_output_device": -1},
        {"name": "Windows WASAPI", "default_input_device": 1, "default_output_device": -1},
    )

    with (
        patch("clea.surfaces.voice.audio.list_audio_devices", return_value=mock_devices),
        patch("sounddevice.query_hostapis", return_value=mock_hostapis),
    ):
        # When preferred_hostapi is WASAPI, default device index 1 is resolved instead of 0
        dev = resolve_audio_device(None, kind="input", preferred_hostapi="WASAPI")
        assert dev["index"] == 1
        assert dev["name"] == "WASAPI Mic"


def test_negotiate_input_sample_rate_defaults_to_native() -> None:
    with patch("sounddevice.check_input_settings") as mock_check:
        mock_check.return_value = None  # Success
        rate, ch = negotiate_input_sample_rate(device_idx=1, default_samplerate=48000)
        assert rate == 48000
        assert ch == 1
        mock_check.assert_called_once_with(
            device=1,
            samplerate=48000,
            channels=1,
            dtype="int16",
        )


def test_negotiate_input_sample_rate_fallback_when_16k_fails() -> None:
    def fake_check(device: int | None, samplerate: int, channels: int, dtype: str) -> None:
        if samplerate == 16000:
            raise sd.PortAudioError("Invalid sample rate [PaErrorCode -9997]")
        if samplerate == 48000 and channels == 1:
            return  # Supported!
        raise sd.PortAudioError("Invalid settings")

    with patch("sounddevice.check_input_settings", side_effect=fake_check):
        rate, ch = negotiate_input_sample_rate(
            device_idx=1,
            default_samplerate=16000,
            candidate_rates=(16000, 48000, 44100),
        )
        assert rate == 48000
        assert ch == 1


def test_negotiate_input_sample_rate_stereo_fallback() -> None:
    def fake_check(device: int | None, samplerate: int, channels: int, dtype: str) -> None:
        if channels == 1:
            raise sd.PortAudioError("Invalid number of channels [PaErrorCode -9998]")
        if channels == 2 and samplerate == 48000:
            return
        raise sd.PortAudioError("Invalid settings")

    with patch("sounddevice.check_input_settings", side_effect=fake_check):
        rate, ch = negotiate_input_sample_rate(
            device_idx=1,
            default_samplerate=48000,
            preferred_channels=1,
        )
        assert rate == 48000
        assert ch == 2


def test_negotiate_input_sample_rate_fallback_when_all_fail() -> None:
    with patch("sounddevice.check_input_settings", side_effect=sd.PortAudioError("Unsupported")):
        rate, ch = negotiate_input_sample_rate(
            device_idx=1,
            default_samplerate=44100,
            preferred_channels=1,
        )
        assert rate == 44100
        assert ch == 1


def test_resample_48k_to_16k_exact_samples_and_frequency() -> None:
    audio_48k = generate_synthetic_sine(freq_hz=1000.0, duration_s=0.08, sample_rate=48000)
    assert len(audio_48k) == 3840

    resampled = resample_to_16k_mono(audio_48k, orig_sr=48000, target_sr=16000)
    assert len(resampled) == 1280
    assert resampled.dtype == np.int16

    # Verify FFT dominant peak at 1000 Hz
    fft_vals = np.abs(np.fft.rfft(resampled.astype(np.float32)))
    freqs = np.fft.rfftfreq(len(resampled), 1.0 / 16000)
    peak_freq = freqs[np.argmax(fft_vals)]
    assert abs(peak_freq - 1000.0) <= 20.0


def test_resample_44k_to_16k_exact_samples() -> None:
    audio_44k = generate_synthetic_sine(freq_hz=440.0, duration_s=0.08, sample_rate=44100)
    assert len(audio_44k) == 3528

    resampled = resample_to_16k_mono(audio_44k, orig_sr=44100, target_sr=16000)
    assert len(resampled) == 1280
    assert resampled.dtype == np.int16


def test_resample_16k_identity() -> None:
    audio_16k = generate_synthetic_sine(freq_hz=440.0, duration_s=0.08, sample_rate=16000)
    resampled = resample_to_16k_mono(audio_16k, orig_sr=16000, target_sr=16000)
    assert np.array_equal(audio_16k, resampled)


def test_resample_stereo_to_mono_downmix() -> None:
    ch1 = np.full(3840, 10000, dtype=np.int16)
    ch2 = np.full(3840, 20000, dtype=np.int16)
    stereo_48k = np.column_stack((ch1, ch2))

    resampled = resample_to_16k_mono(stereo_48k, orig_sr=48000, target_sr=16000)
    assert resampled.ndim == 1
    assert len(resampled) == 1280
    assert abs(np.mean(resampled[50:-50]) - 15000) < 50


def test_resample_anti_aliasing_filter() -> None:
    # 12,000 Hz at 48000 Hz is well above Nyquist (8000 Hz) for 16 kHz
    audio_above_nyquist = generate_synthetic_sine(
        freq_hz=12000.0, duration_s=0.1, sample_rate=48000, amplitude=20000.0
    )
    resampled = resample_to_16k_mono(audio_above_nyquist, orig_sr=48000, target_sr=16000)

    in_rms = compute_audio_rms(audio_above_nyquist)
    out_rms = compute_audio_rms(resampled)

    # Output should be heavily attenuated (>40 dB attenuation: ratio < 0.01)
    assert out_rms / in_rms < 0.01


def test_resample_clipping_preserves_int16_bounds() -> None:
    large_audio = np.array([-100000.0, 0.0, 100000.0], dtype=np.float32)
    resampled = resample_to_16k_mono(large_audio, orig_sr=16000, target_sr=16000)
    assert resampled.dtype == np.int16
    assert resampled[0] == -32768
    assert resampled[2] == 32767


def test_resample_empty_array() -> None:
    empty = np.empty(0, dtype=np.int16)
    resampled = resample_to_16k_mono(empty, orig_sr=48000, target_sr=16000)
    assert len(resampled) == 0
    assert resampled.dtype == np.int16


def test_audio_chunk_buffer_exact_1280_chunks_across_variable_packet_sizes() -> None:
    total_samples = 10000
    source_data = np.arange(total_samples, dtype=np.int16)

    buffer = AudioChunkBuffer(chunk_size=1280)
    packet_sizes = [500, 1000, 300, 1500, 800, 256, 1280, 2000, 2364]
    assert sum(packet_sizes) == total_samples

    all_drained: list[np.ndarray] = []
    idx = 0
    for p_size in packet_sizes:
        packet = source_data[idx : idx + p_size]
        buffer.extend(packet)
        idx += p_size
        chunks = buffer.get_chunks()
        all_drained.extend(chunks)

    expected_full_chunks = total_samples // 1280  # 7 chunks (8960 samples)
    assert len(all_drained) == expected_full_chunks

    concatenated = np.concatenate(all_drained)
    assert len(concatenated) == expected_full_chunks * 1280
    assert np.array_equal(concatenated, source_data[: len(concatenated)])

    rem_bytes = (total_samples % 1280) * 2
    assert len(buffer._buffer) == rem_bytes


def test_audio_chunk_buffer_clear() -> None:
    buffer = AudioChunkBuffer(chunk_size=1280)
    buffer.extend(np.ones(1000, dtype=np.int16))
    assert len(buffer._buffer) == 2000

    buffer.clear()
    assert len(buffer._buffer) == 0
    assert len(buffer.get_chunks()) == 0


def test_audio_chunk_buffer_thread_safety() -> None:
    buffer = AudioChunkBuffer(chunk_size=1280)
    num_writers = 4
    chunks_per_writer = 10
    samples_per_push = 1280

    def writer(thread_id: int) -> None:
        for _ in range(chunks_per_writer):
            data = np.full(samples_per_push, thread_id, dtype=np.int16)
            buffer.extend(data)

    with concurrent.futures.ThreadPoolExecutor(max_workers=num_writers) as executor:
        futures = [executor.submit(writer, i) for i in range(num_writers)]
        concurrent.futures.wait(futures)

    drained = buffer.get_chunks()
    assert len(drained) == num_writers * chunks_per_writer
    for c in drained:
        assert len(c) == 1280
        assert c.dtype == np.int16


def test_compute_audio_rms_silence() -> None:
    silence = np.zeros(1280, dtype=np.int16)
    assert compute_audio_rms(silence) == 0.0
    assert compute_audio_rms(silence.tobytes()) == 0.0


def test_compute_audio_rms_known_sine() -> None:
    sine = generate_synthetic_sine(
        freq_hz=440.0, duration_s=0.1, sample_rate=16000, amplitude=10000.0
    )
    rms_arr = compute_audio_rms(sine)
    rms_bytes = compute_audio_rms(sine.tobytes())
    assert abs(rms_arr - 7071.07) < 25.0
    assert abs(rms_bytes - 7071.07) < 25.0


def test_compute_audio_rms_empty_or_none() -> None:
    assert compute_audio_rms(b"") == 0.0
    assert compute_audio_rms(np.empty(0, dtype=np.int16)) == 0.0
    assert compute_audio_rms(None) == 0.0


def test_audio_manager_resampling_and_1280_framing_in_capture_loop() -> None:
    """Verify native 48 kHz/44.1 kHz input is resampled to 16 kHz mono and framed into 1280."""
    received_chunks: list[bytes] = []

    def on_input(data: bytes) -> None:
        received_chunks.append(data)

    manager = AudioManager(chunk_size=1280, on_audio_input=on_input)
    manager.input_sample_rate = 48000
    manager.input_channels = 2

    # 1. Feed 48 kHz stereo block (3840 frames = 80 ms)
    block_48k_stereo = np.full((3840, 2), 5000, dtype=np.int16)
    manager._input_stream_callback(block_48k_stereo, 3840, None, 0)  # type: ignore[arg-type]

    assert len(received_chunks) == 1
    chunk1 = received_chunks[0]
    assert len(chunk1) == 2560  # 1280 samples * 2 bytes = 2560 bytes
    arr1 = np.frombuffer(chunk1, dtype=np.int16)
    assert len(arr1) == 1280
    assert arr1.dtype == np.int16

    # 2. Feed 44.1 kHz mono block (3528 frames = 80 ms)
    manager.input_sample_rate = 44100
    manager.input_channels = 1
    block_44k = np.full((3528, 1), 3000, dtype=np.int16)
    manager._input_stream_callback(block_44k, 3528, None, 0)  # type: ignore[arg-type]

    # After 3528 samples at 44.1k -> resamples to ~1279.9 -> FIFO buffer yields 1280 chunks
    for c in received_chunks:
        assert len(c) == 2560
        arr = np.frombuffer(c, dtype=np.int16)
        assert len(arr) == 1280


def test_resolve_audio_device_wasapi_prioritization_and_substring() -> None:
    """Verify substring matching and WASAPI host API prioritization across duplicate devices."""
    mock_devices = [
        {
            "index": 10,
            "name": "Microphone Array (Realtek(R) Audio)",
            "hostapi": 0,
            "hostapi_name": "MME",
            "max_input_channels": 2,
            "max_output_channels": 0,
            "default_samplerate": 44100,
        },
        {
            "index": 15,
            "name": "Microphone Array (Realtek(R) Audio)",
            "hostapi": 1,
            "hostapi_name": "Windows WASAPI",
            "max_input_channels": 2,
            "max_output_channels": 0,
            "default_samplerate": 48000,
        },
        {
            "index": 14,
            "name": "Headset (Earphone)",
            "hostapi": 1,
            "hostapi_name": "Windows WASAPI",
            "max_input_channels": 1,
            "max_output_channels": 0,
            "default_samplerate": 16000,
        },
    ]

    with patch("clea.surfaces.voice.audio.list_audio_devices", return_value=mock_devices):
        # 1. Substring "realtek" matches MME (#10) and WASAPI (#15); WASAPI selects #15
        dev = resolve_audio_device("Realtek", kind="input", preferred_hostapi="WASAPI")
        assert dev["index"] == 15
        assert dev["hostapi_name"] == "Windows WASAPI"
        assert dev["default_samplerate"] == 48000

        # 2. Substring "earphone" resolves Headset #14
        dev_earphone = resolve_audio_device("earphone", kind="input", preferred_hostapi="WASAPI")
        assert dev_earphone["index"] == 14
        assert dev_earphone["default_samplerate"] == 16000

        # 3. Numeric string index resolution
        dev_by_idx = resolve_audio_device("15", kind="input")
        assert dev_by_idx["index"] == 15


def test_resample_to_16k_mono_sanitizes_nan_and_inf() -> None:
    """Verify float array with NaNs/Infs is safely sanitized without warnings or exceptions."""
    import warnings

    arr_with_nan = np.array([1000.0, np.nan, -1000.0, np.inf, -np.inf], dtype=np.float32)
    with warnings.catch_warnings(record=True) as recorded:
        warnings.simplefilter("always")
        resampled = resample_to_16k_mono(arr_with_nan, orig_sr=16000, target_sr=16000)
        assert resampled.dtype == np.int16
        assert len(resampled) == 5
        assert resampled[1] == 0
        assert resampled[3] == 32767
        assert resampled[4] == -32768
        assert not any("invalid value encountered" in str(w.message) for w in recorded)


def test_audio_manager_start_retries_on_transient_endpoint_wakeup() -> None:
    """AudioManager.start retries transient AudioDeviceNotFoundError / PortAudioError."""
    manager = AudioManager()
    attempts = 0

    def flaky_start_once() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise AudioDeviceNotFoundError("No audio input devices found on the system.")
        if attempts == 2:
            raise sd.PortAudioError("WASAPI endpoint not ready")
        manager._running = True

    with (
        patch.object(manager, "_start_once", side_effect=flaky_start_once),
        patch("clea.surfaces.voice.audio.refresh_audio_devices") as mock_refresh,
    ):
        manager.start(max_attempts=3, retry_delay=0.0)

    assert attempts == 3
    assert manager._running is True
    assert mock_refresh.call_count == 2


def test_audio_manager_start_does_not_retry_ambiguous_device() -> None:
    """AmbiguousAudioDeviceError is a config error and fails immediately without retry."""
    manager = AudioManager()
    attempts = 0

    def fail_ambiguous() -> None:
        nonlocal attempts
        attempts += 1
        raise AmbiguousAudioDeviceError("Multiple devices match")

    with (
        patch.object(manager, "_start_once", side_effect=fail_ambiguous),
        patch("clea.surfaces.voice.audio.refresh_audio_devices") as mock_refresh,
        pytest.raises(AmbiguousAudioDeviceError),
    ):
        manager.start(max_attempts=4, retry_delay=0.0)

    assert attempts == 1
    assert mock_refresh.call_count == 0


def test_refresh_audio_devices_reinitializes_portaudio() -> None:
    with (
        patch.object(sd, "_initialized", 1),
        patch.object(sd, "_terminate") as mock_term,
        patch.object(sd, "_initialize") as mock_init,
    ):
        # Simulate _terminate dropping _initialized to 0
        def drop_init() -> None:
            sd._initialized = 0

        mock_term.side_effect = drop_init
        refresh_audio_devices()
        assert mock_term.called
        assert mock_init.called


def test_audio_manager_resumes_capture_after_output_drains() -> None:
    """After Turn 1 output finishes draining, is_speaking resets and Turn 2 mic flows."""
    received: list[bytes] = []
    manager = AudioManager(
        mic_mute_while_speaking=True,
        on_audio_input=received.append,
        chunk_size=1280,
    )
    manager.input_sample_rate = 16000
    manager.input_channels = 1
    manager._running = True

    # Turn 1 playback: exact frame boundary (200 samples = 400 bytes)
    manager.play_audio(b"\x01\x00" * 200)
    assert manager.is_speaking is True

    # While speaking, mic input is muted
    mic_block = np.ones((1280, 1), dtype=np.int16)
    manager._input_stream_callback(mic_block, 1280, None, 0)  # type: ignore[arg-type]
    assert len(received) == 0

    # Drain exact 200 frames, then next callback sees empty queue -> clears is_speaking
    out = np.empty((200, 1), dtype=np.int16)
    manager._output_stream_callback(out, 200, None, 0)  # type: ignore[arg-type]
    manager._output_stream_callback(out, 200, None, 0)  # type: ignore[arg-type]
    assert manager.is_speaking is False

    # Turn 2 mic input must now be captured and forwarded
    manager._input_stream_callback(mic_block, 1280, None, 0)  # type: ignore[arg-type]
    assert len(received) == 1
    assert len(received[0]) == 2560

"""Unit tests for AudioManager with barge-in flush and mic muting."""

from __future__ import annotations

from unittest.mock import patch

import numpy as np
import pytest

from clea.surfaces.voice.audio import (
    AmbiguousAudioDeviceError,
    AudioDeviceNotFoundError,
    AudioManager,
    list_audio_devices,
    resolve_audio_device,
)


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

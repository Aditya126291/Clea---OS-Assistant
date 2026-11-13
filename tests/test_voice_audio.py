"""Unit tests for AudioManager with barge-in flush and mic muting."""

from __future__ import annotations

import numpy as np

from clea.surfaces.voice.audio import AudioManager, list_audio_devices


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

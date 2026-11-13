"""Tests for VoiceInstanceLock single-instance guard."""

from __future__ import annotations

from pathlib import Path

import pytest

from clea.surfaces.voice.lock import (
    VoiceAlreadyRunningError,
    VoiceInstanceLock,
    voice_lock_path,
)


def test_voice_lock_acquire_and_release(tmp_path: Path) -> None:
    lock_file = tmp_path / "voice.lock"
    lock = VoiceInstanceLock(path=lock_file)
    assert not lock_file.exists()

    lock.acquire()
    assert lock_file.exists()

    # Second instance in same directory raises VoiceAlreadyRunningError
    lock2 = VoiceInstanceLock(path=lock_file)
    with pytest.raises(VoiceAlreadyRunningError, match="already running"):
        lock2.acquire()

    # Release allows acquiring again
    lock.release()
    assert not lock_file.exists()

    lock2.acquire()
    assert lock_file.exists()
    lock2.release()


def test_voice_lock_context_manager(tmp_path: Path) -> None:
    lock_file = tmp_path / "voice.lock"
    with VoiceInstanceLock(path=lock_file) as lock:
        assert lock.path.exists()
        with pytest.raises(VoiceAlreadyRunningError):
            VoiceInstanceLock(path=lock_file).acquire()

    assert not lock_file.exists()


def test_voice_lock_path_default(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    monkeypatch.setenv("CLEA_DATA_DIR", str(tmp_path))
    assert voice_lock_path() == tmp_path / "voice.lock"

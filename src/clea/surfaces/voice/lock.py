"""Single-instance guard ensuring only one ``clea voice`` process runs."""

from __future__ import annotations

import contextlib
import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from clea.paths import default_data_dir

VOICE_LOCK_FILENAME = "voice.lock"


def voice_lock_path(data_dir: Path | None = None) -> Path:
    """Return the path to ``voice.lock`` in the Clea user data directory."""
    return (data_dir or default_data_dir()) / VOICE_LOCK_FILENAME


class VoiceAlreadyRunningError(OSError):
    """Raised when another Clea voice process holds the single-instance lock."""

    def __init__(self, message: str, *, info: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.info = info or {}


def _read_voice_lock_metadata(path: Path) -> dict[str, Any]:
    """Read JSON metadata stored after byte 0."""
    try:
        fd = os.open(path, os.O_RDONLY)
        try:
            os.lseek(fd, 1, os.SEEK_SET)
            raw = os.read(fd, 4096).decode("utf-8", errors="replace").strip()
        finally:
            os.close(fd)
        data = json.loads(raw)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


class VoiceInstanceLock:
    """OS-advisory non-blocking lock ensuring one voice process per data directory."""

    def __init__(self, path: Path | None = None) -> None:
        self.path = path or voice_lock_path()
        self._fd: int | None = None

    def acquire(self) -> None:
        """Acquire the lock or raise VoiceAlreadyRunningError."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            self._lock_fd(fd)
        except OSError as exc:
            os.close(fd)
            info = _read_voice_lock_metadata(self.path)
            pid = info.get("pid")
            suffix = f" (PID {pid})" if pid else ""
            raise VoiceAlreadyRunningError(
                f"Clea voice is already running{suffix}.",
                info=info,
            ) from exc

        payload = json.dumps(
            {
                "pid": os.getpid(),
                "started_at": datetime.now(UTC).isoformat(),
            }
        ).encode("utf-8")
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, b" " + payload)
            os.ftruncate(fd, 1 + len(payload))
        except OSError:
            pass
        self._fd = fd

    def release(self) -> None:
        """Release the lock and remove the lock file."""
        if self._fd is None:
            return
        fd = self._fd
        self._fd = None
        with contextlib.suppress(OSError):
            self._unlock_fd(fd)
        with contextlib.suppress(OSError):
            os.close(fd)
        with contextlib.suppress(OSError):
            self.path.unlink()

    def __enter__(self) -> VoiceInstanceLock:
        self.acquire()
        return self

    def __exit__(self, exc_type: Any, exc_val: Any, exc_tb: Any) -> None:
        self.release()

    @staticmethod
    def _lock_fd(fd: int) -> None:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)

    @staticmethod
    def _unlock_fd(fd: int) -> None:
        if sys.platform == "win32":
            import msvcrt

            os.lseek(fd, 0, os.SEEK_SET)
            msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(fd, fcntl.LOCK_UN)

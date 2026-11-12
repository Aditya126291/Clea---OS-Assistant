"""Session token, single-instance lock, and Origin allowlist for the daemon.

Shared by the daemon and surfaces (CLI, desktop shell). Depends only on ``clea.paths``.

Wire format: the token travels in the ``Sec-WebSocket-Protocol`` header as
``clea.v1, clea.token.<token>`` so it never appears in a URL (URLs get logged).
"""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.wintypes as wintypes
import functools
import hmac
import json
import os
import secrets
import subprocess
import sys
from collections.abc import Iterable
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from clea.paths import default_data_dir

PROTOCOL_NAME = "clea.v1"
TOKEN_SUBPROTOCOL_PREFIX = "clea.token."
TOKEN_FILENAME = "session_token"
LOCK_FILENAME = "daemon.lock"
ORIGINS_ENV_VAR = "CLEA_ALLOWED_ORIGINS"

# Local Vite dev server and the packaged Electron app (custom clea-app:// protocol).
DEFAULT_ALLOWED_ORIGINS: tuple[str, ...] = (
    "http://localhost:5173",
    "http://127.0.0.1:5173",
    "clea-app://app",
)
_LOCAL_HOSTNAMES = {"127.0.0.1", "localhost", "::1"}


def token_path(data_dir: Path | None = None) -> Path:
    """Return the location of the session token file."""
    return (data_dir or default_data_dir()) / TOKEN_FILENAME


def lock_path(data_dir: Path | None = None) -> Path:
    """Return the location of the daemon single-instance lock file."""
    return (data_dir or default_data_dir()) / LOCK_FILENAME


def generate_token() -> str:
    """Return a fresh 256-bit URL-safe random token."""
    return secrets.token_urlsafe(32)


@functools.lru_cache(maxsize=1)
def _current_windows_user_sid() -> str | None:
    """Return the SID string of the current Windows process user, or None on failure."""
    if sys.platform != "win32":
        return None
    try:
        advapi32 = ctypes.WinDLL("advapi32", use_last_error=True)
        kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        kernel32.GetCurrentProcess.restype = wintypes.HANDLE
        advapi32.OpenProcessToken.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.HANDLE),
        ]
        advapi32.OpenProcessToken.restype = wintypes.BOOL
        advapi32.GetTokenInformation.argtypes = [
            wintypes.HANDLE,
            ctypes.c_int,
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.POINTER(wintypes.DWORD),
        ]
        advapi32.GetTokenInformation.restype = wintypes.BOOL
        advapi32.ConvertSidToStringSidW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.LPWSTR),
        ]
        advapi32.ConvertSidToStringSidW.restype = wintypes.BOOL
        kernel32.LocalFree.argtypes = [ctypes.c_void_p]

        tok = wintypes.HANDLE()
        token_query = 0x0008
        if not advapi32.OpenProcessToken(
            kernel32.GetCurrentProcess(), token_query, ctypes.byref(tok)
        ):
            return None
        try:
            needed = wintypes.DWORD()
            advapi32.GetTokenInformation(tok, 1, None, 0, ctypes.byref(needed))
            if needed.value == 0:
                return None
            buf = ctypes.create_string_buffer(needed.value)
            if not advapi32.GetTokenInformation(tok, 1, buf, needed, ctypes.byref(needed)):
                return None
            sid_ptr = ctypes.c_void_p.from_buffer_copy(buf[: ctypes.sizeof(ctypes.c_void_p)])
            sid_str_p = wintypes.LPWSTR()
            if not advapi32.ConvertSidToStringSidW(sid_ptr, ctypes.byref(sid_str_p)):
                return None
            try:
                return sid_str_p.value
            finally:
                kernel32.LocalFree(sid_str_p)
        finally:
            kernel32.CloseHandle(tok)
    except OSError:
        return None


def _restrict_windows_acl(path: Path) -> None:
    """Restrict a file's NTFS DACL to the current user SID (best effort, no console flash)."""
    if sys.platform != "win32":
        return
    sid = _current_windows_user_sid()
    if not sid:
        return
    create_no_window = getattr(subprocess, "CREATE_NO_WINDOW", 0x08000000)
    with contextlib.suppress(OSError):
        subprocess.run(
            ["icacls", str(path), "/inheritance:r", "/grant:r", f"*{sid}:(F)"],
            capture_output=True,
            check=False,
            creationflags=create_no_window,
        )


def write_token(token: str, path: Path) -> None:
    """Persist the token readable only by the current user (POSIX 0600 / Windows user ACL)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    if tmp.exists():
        with contextlib.suppress(OSError):
            tmp.chmod(0o600)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    os.close(fd)
    # Lock down NTFS ACL before writing the secret so there is no permissive window.
    _restrict_windows_acl(tmp)
    tmp.write_text(token, encoding="utf-8")
    if path.exists():
        with contextlib.suppress(OSError):
            path.chmod(0o600)
    os.replace(tmp, path)
    with contextlib.suppress(OSError):
        path.chmod(0o600)


def read_token(path: Path | None = None) -> str | None:
    """Read the token written by a running daemon, or None if there isn't one."""
    target = path or token_path()
    try:
        value = target.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    return value or None


def remove_token(path: Path, expected: str) -> None:
    """Delete the token file on shutdown, but only if it still holds our token."""
    if read_token(path) == expected:
        with contextlib.suppress(OSError):
            path.unlink()


class DaemonAlreadyRunningError(OSError):
    """Raised when another Clea daemon already holds the single-instance lock."""

    def __init__(self, message: str, *, info: dict[str, Any] | None = None) -> None:
        super().__init__(message)
        self.info = info or {}


def _read_lock_metadata(path: Path) -> dict[str, Any]:
    """Read JSON metadata stored after byte 0 (safe on Windows while byte 0 is locked)."""
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


class InstanceLock:
    """OS-advisory/mandatory non-blocking lock ensuring one daemon per data directory."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._fd: int | None = None

    def acquire(self, *, host: str, port: int) -> None:
        """Acquire the lock or raise DaemonAlreadyRunningError."""
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_CREAT | os.O_RDWR, 0o600)
        try:
            self._lock_fd(fd)
        except OSError as exc:
            os.close(fd)
            info = _read_lock_metadata(self.path)
            pid = info.get("pid")
            bound_host = info.get("host")
            bound_port = info.get("port")
            details: list[str] = []
            if pid:
                details.append(f"PID {pid}")
            if bound_host and bound_port:
                details.append(f"ws://{bound_host}:{bound_port}")
            suffix = f" ({', '.join(details)})" if details else ""
            raise DaemonAlreadyRunningError(
                f"Clea daemon is already running{suffix}.",
                info=info,
            ) from exc

        payload = json.dumps({"pid": os.getpid(), "host": host, "port": port}).encode("utf-8")
        try:
            os.lseek(fd, 0, os.SEEK_SET)
            os.write(fd, b" " + payload)
            os.ftruncate(fd, 1 + len(payload))
        except OSError:
            pass
        self._fd = fd

    def update_port(self, *, host: str, port: int) -> None:
        """Update the recorded port after binding (useful when started with port=0)."""
        if self._fd is None:
            return
        payload = json.dumps({"pid": os.getpid(), "host": host, "port": port}).encode("utf-8")
        with contextlib.suppress(OSError):
            os.lseek(self._fd, 0, os.SEEK_SET)
            os.write(self._fd, b" " + payload)
            os.ftruncate(self._fd, 1 + len(payload))

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


def client_subprotocols(token: str) -> list[str]:
    """Subprotocol list a client offers to authenticate."""
    return [PROTOCOL_NAME, f"{TOKEN_SUBPROTOCOL_PREFIX}{token}"]


def extract_token(subprotocol_header: str | None) -> str | None:
    """Pull the token out of a ``Sec-WebSocket-Protocol`` header value."""
    if not subprotocol_header:
        return None
    for part in subprotocol_header.split(","):
        candidate = part.strip()
        if candidate.startswith(TOKEN_SUBPROTOCOL_PREFIX):
            return candidate[len(TOKEN_SUBPROTOCOL_PREFIX) :] or None
    return None


def token_matches(expected: str, supplied: str | None) -> bool:
    """Constant-time token comparison."""
    if not supplied:
        return False
    return hmac.compare_digest(expected.encode(), supplied.encode())


def normalize_origins(origins: Iterable[str]) -> frozenset[str]:
    """Validate an Origin allowlist. Wildcards and the opaque ``null`` origin are refused."""
    result: set[str] = set()
    for raw in origins:
        origin = raw.strip().rstrip("/")
        if not origin:
            continue
        lowered = origin.lower()
        if "*" in origin or lowered == "null":
            raise ValueError(f"Origin '{raw}' is not allowed: no wildcard or 'null' origins.")
        if "://" not in origin:
            raise ValueError(f"Origin '{raw}' must look like scheme://host[:port].")
        result.add(lowered)
    return frozenset(result)


def allowed_origins_from_env() -> frozenset[str]:
    """Allowlist from ``CLEA_ALLOWED_ORIGINS`` (comma-separated, replaces defaults) or defaults."""
    configured = os.environ.get(ORIGINS_ENV_VAR)
    if configured is None:
        return normalize_origins(DEFAULT_ALLOWED_ORIGINS)
    return normalize_origins(configured.split(","))


def host_header_is_local(host_header: str | None) -> bool:
    """True if the Host header names loopback (guards against DNS rebinding)."""
    if not host_header or "@" in host_header or any(ch.isspace() for ch in host_header):
        return False
    try:
        hostname = urlsplit(f"//{host_header}").hostname
    except ValueError:
        return False
    return hostname in _LOCAL_HOSTNAMES

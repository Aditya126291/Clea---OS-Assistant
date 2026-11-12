"""Session token and Origin allowlist for the daemon's WebSocket handshake.

Shared by the daemon and surfaces (CLI, desktop shell). Depends only on ``clea.paths``.

Wire format: the token travels in the ``Sec-WebSocket-Protocol`` header as
``clea.v1, clea.token.<token>`` so it never appears in a URL (URLs get logged).
"""

from __future__ import annotations

import contextlib
import hmac
import os
import secrets
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlsplit

from clea.paths import default_data_dir

PROTOCOL_NAME = "clea.v1"
TOKEN_SUBPROTOCOL_PREFIX = "clea.token."
TOKEN_FILENAME = "session_token"
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


def generate_token() -> str:
    """Return a fresh 256-bit URL-safe random token."""
    return secrets.token_urlsafe(32)


def write_token(token: str, path: Path) -> None:
    """Persist the token readable only by the current user (best effort on Windows)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
    with os.fdopen(fd, "w", encoding="utf-8") as handle:
        handle.write(token)
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

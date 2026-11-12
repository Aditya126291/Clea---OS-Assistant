"""Filesystem locations shared by the daemon and surfaces (no other clea imports)."""

from __future__ import annotations

import os
from pathlib import Path

from platformdirs import user_data_dir


def default_data_dir() -> Path:
    """Return the user data directory for Clea state, memory, tokens, and audit logs."""
    override = os.environ.get("CLEA_DATA_DIR")
    if override:
        return Path(override).expanduser().resolve()
    return Path(user_data_dir("clea", appauthor=False))

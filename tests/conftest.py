"""Root test configuration and global autouse fixtures for Clea test suite."""

from __future__ import annotations

import os
from pathlib import Path

import pytest

_NO_MODELS_TESTS = {
    "test_check_models_status_empty_dir",
    "test_wake_word_missing_models_raises_clear_error",
    "test_download_wake_word_models_downloads_to_target",
    "test_voice_doctor_does_not_download_silently",
    "test_voice_doctor_with_download_models",
}


@pytest.fixture(autouse=True)
def _sandbox_clea_environment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, request: pytest.FixtureRequest
) -> None:
    """Ensure all test executions across the suite are strictly isolated from host Clea data."""
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLEA_DATA_DIR", str(tmp_path))

    host_clea = Path(os.environ.get("LOCALAPPDATA", "")) / "clea"
    if host_clea.exists():
        host_models = host_clea / "models"
        if host_models.exists() and request.node.name not in _NO_MODELS_TESTS:
            try:
                os.symlink(host_models, tmp_path / "models", target_is_directory=True)
            except OSError:
                pass

        if request.node.name == "test_voice_doctor_diagnostics_output":
            host_token = host_clea / "session_token"
            if host_token.exists():
                (tmp_path / "session_token").write_text(
                    host_token.read_text(encoding="utf-8"), encoding="utf-8"
                )

    import clea.tools.windows_os as _windows_os

    _orig_get_desktop_name = _windows_os._get_current_desktop_name

    def _default_test_desktop_name(*, user32: object = None, kernel32: object = None) -> str | None:
        if user32 is not None or kernel32 is not None:
            return _orig_get_desktop_name(user32=user32, kernel32=kernel32)
        return "Default"

    monkeypatch.setattr(_windows_os, "_get_current_desktop_name", _default_test_desktop_name)

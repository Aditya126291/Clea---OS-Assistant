"""Root test configuration and global autouse fixtures for Clea test suite."""

from __future__ import annotations

import os
import shutil
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
    tmp_path_factory: pytest.TempPathFactory,
    monkeypatch: pytest.MonkeyPatch,
    request: pytest.FixtureRequest,
) -> None:
    """Ensure all test executions across the suite are strictly isolated from host Clea data."""
    # Use a dedicated sandbox directory separate from each test's `tmp_path` fixture so
    # pre-seeded logs/models never pollute tests that scan `tmp_path` (e.g. search_files).
    clea_data = tmp_path_factory.mktemp("clea_data")
    (clea_data / "logs").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLEA_DATA_DIR", str(clea_data))
    # Keep unit tests strictly offline and isolated from host wake-word/VAD env overrides.
    monkeypatch.setenv("CLEA_WAKEWORD_AUTO_DOWNLOAD", "0")
    monkeypatch.delenv("CLEA_WAKEWORD_MODEL_PATH", raising=False)
    monkeypatch.delenv("CLEA_VAD_SILENCE_DURATION_MS", raising=False)
    # Never auto-attach to the user's live Chrome during pytest runs.
    monkeypatch.setenv("CLEA_CDP_DISABLE_AUTO_DISCOVERY", "1")
    monkeypatch.delenv("CLEA_CDP_WS_URL", raising=False)
    monkeypatch.delenv("CLEA_CDP_URL", raising=False)
    # Prevent daemon startup from spawning host Get-StartApps PowerShell during unrelated tests.
    monkeypatch.setenv("CLEA_DISABLE_STARTUP_APP_WARMUP", "1")

    from clea.tools.cdp_bridge import set_default_cdp_bridge
    from clea.tools.uia_com import set_default_uia_com_engine
    from clea.tools.windows_os import (
        clear_last_screen_geometry,
        invalidate_installed_apps_cache,
    )

    set_default_cdp_bridge(None)
    set_default_uia_com_engine(None)
    clear_last_screen_geometry()
    invalidate_installed_apps_cache()

    host_clea = Path(os.environ.get("LOCALAPPDATA", "")) / "clea"
    if host_clea.exists():
        host_models = host_clea / "models"
        if host_models.exists() and request.node.name not in _NO_MODELS_TESTS:
            sandbox_models = clea_data / "models"
            sandbox_models.mkdir(parents=True, exist_ok=True)
            for item in host_models.iterdir():
                if item.is_file():
                    try:
                        # Copy rather than symlink so test writes can NEVER mutate
                        # host %LOCALAPPDATA%\clea.
                        shutil.copy2(item, sandbox_models / item.name)
                    except OSError:
                        pass

        if request.node.name == "test_voice_doctor_diagnostics_output":
            host_token = host_clea / "session_token"
            if host_token.exists():
                (clea_data / "session_token").write_text(
                    host_token.read_text(encoding="utf-8"), encoding="utf-8"
                )

    import clea.tools.windows_os as _windows_os

    _orig_get_desktop_name = _windows_os._get_current_desktop_name

    def _default_test_desktop_name(*, user32: object = None, kernel32: object = None) -> str | None:
        if user32 is not None or kernel32 is not None:
            return _orig_get_desktop_name(user32=user32, kernel32=kernel32)
        return "Default"

    monkeypatch.setattr(_windows_os, "_get_current_desktop_name", _default_test_desktop_name)

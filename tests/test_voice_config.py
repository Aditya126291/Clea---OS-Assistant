"""Tests for voice_config.toml loading, default generation, and validation."""

from __future__ import annotations

import tomllib
from pathlib import Path

import pytest
from pydantic import ValidationError

from clea.surfaces.voice.config import (
    VOICE_CONFIG_FILENAME,
    VoiceConfig,
    default_voice_config_toml,
    ensure_voice_config,
    load_voice_config,
    voice_config_path,
)


def test_voice_config_path_uses_clea_data_dir(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLEA_DATA_DIR", str(tmp_path))
    assert voice_config_path() == tmp_path.resolve() / VOICE_CONFIG_FILENAME


def test_default_toml_contains_comments_and_roundtrips_to_defaults() -> None:
    toml_text = default_voice_config_toml()
    assert "#" in toml_text
    assert "mic_mute_while_speaking = false" in toml_text
    assert 'wake_word_model = "hey_jarvis"' in toml_text
    assert 'hotkey = "Ctrl+Alt+V"' in toml_text
    assert 'media_resolution = "medium"' in toml_text
    assert 'consent_mode = "ask"' in toml_text

    parsed = tomllib.loads(toml_text)
    cfg = VoiceConfig.model_validate(parsed)
    assert cfg == VoiceConfig()
    assert cfg.wake_word_model == "hey_jarvis"
    assert cfg.wake_threshold == 0.30
    assert cfg.vad_mode == "server"
    assert cfg.input_device is None
    assert cfg.output_device is None
    assert cfg.mic_mute_while_speaking is False
    assert cfg.silence_timeout_seconds == 60.0
    assert cfg.hotkey == "Ctrl+Alt+V"
    assert cfg.max_burst_seconds == 30.0
    assert cfg.image_max_side == 768
    assert cfg.media_resolution == "medium"
    assert cfg.blocklist.window_titles == []
    assert cfg.blocklist.processes == []
    assert cfg.consent_mode == "ask"
    assert cfg.reconnect_limit == 3
    assert cfg.enable_web_search is False
    assert cfg.stall_speech_rms_threshold == 250.0


def test_load_voice_config_creates_default_file_if_missing(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLEA_DATA_DIR", str(tmp_path))
    target = voice_config_path()
    assert not target.exists()

    cfg = load_voice_config()
    assert target.exists()
    assert cfg == VoiceConfig()

    # Subsequent ensure_voice_config does not overwrite custom edits
    target.write_text('wake_word_model = "C:/models/custom.onnx"\n', encoding="utf-8")
    ensure_voice_config()
    assert "custom.onnx" in target.read_text(encoding="utf-8")


def test_load_voice_config_custom_values_and_devices(tmp_path: Path) -> None:
    cfg_path = tmp_path / "voice_config.toml"
    cfg_path.write_text(
        """
wake_word_model = "models/clea.onnx"
wake_threshold = 0.72
input_device = "Headset Microphone"
output_device = 3
mic_mute_while_speaking = true
silence_timeout_seconds = 90
hotkey = "Ctrl+Shift+Space"
max_burst_seconds = 15
image_max_side = 1024
media_resolution = "high"
consent_mode = "auto_allow"
reconnect_limit = 5

[blocklist]
window_titles = ["*1Password*", "*Incognito*"]
processes = ["keepassxc.exe"]
""",
        encoding="utf-8",
    )

    cfg = load_voice_config(cfg_path)
    assert cfg.wake_word_model == "models/clea.onnx"
    assert cfg.wake_threshold == 0.72
    assert cfg.input_device == "Headset Microphone"
    assert cfg.output_device == 3
    assert cfg.mic_mute_while_speaking is True
    assert cfg.silence_timeout_seconds == 90.0
    assert cfg.hotkey == "Ctrl+Shift+Space"
    assert cfg.max_burst_seconds == 15.0
    assert cfg.image_max_side == 1024
    assert cfg.media_resolution == "high"
    assert cfg.consent_mode == "auto_allow"
    assert cfg.reconnect_limit == 5
    assert cfg.blocklist.window_titles == ["*1Password*", "*Incognito*"]
    assert cfg.blocklist.processes == ["keepassxc.exe"]


def test_empty_device_string_normalizes_to_none(tmp_path: Path) -> None:
    cfg_path = tmp_path / "voice_config.toml"
    cfg_path.write_text('input_device = "  "\noutput_device = ""\n', encoding="utf-8")
    cfg = load_voice_config(cfg_path)
    assert cfg.input_device is None
    assert cfg.output_device is None


@pytest.mark.parametrize(
    "toml_body",
    [
        "wake_threshold = 1.5\n",
        "wake_threshold = -0.1\n",
        "wake_threshold = true\n",
        "wake_threshold = nan\n",
        'media_resolution = "ultra"\n',
        'consent_mode = "always"\n',
        "silence_timeout_seconds = 0\n",
        "silence_timeout_seconds = inf\n",
        "silence_timeout_seconds = false\n",
        "max_burst_seconds = -5\n",
        "max_burst_seconds = inf\n",
        "image_max_side = 16\n",
        "reconnect_limit = -1\n",
        "reconnect_limit = true\n",
        "input_device = true\n",
        "output_device = -1\n",
        'wake_word_model = "   "\n',
        'hotkey = ""\n',
        "unknown_setting = true\n",
    ],
)
def test_invalid_voice_config_rejected(tmp_path: Path, toml_body: str) -> None:
    cfg_path = tmp_path / "voice_config.toml"
    cfg_path.write_text(toml_body, encoding="utf-8")
    with pytest.raises(ValidationError):
        load_voice_config(cfg_path)


def test_load_voice_config_handles_utf8_bom(tmp_path: Path) -> None:
    cfg_path = tmp_path / "voice_config.toml"
    cfg_path.write_text("\ufeffwake_threshold = 0.65\n", encoding="utf-8")
    cfg = load_voice_config(cfg_path)
    assert cfg.wake_threshold == 0.65

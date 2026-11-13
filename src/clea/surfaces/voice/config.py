"""Voice surface configuration stored in ``<user_data_dir>/voice_config.toml``."""

from __future__ import annotations

import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from clea.paths import default_data_dir

MediaResolution = Literal["low", "medium", "high"]
ConsentMode = Literal["ask", "auto_allow", "deny"]

VOICE_CONFIG_FILENAME = "voice_config.toml"


class ScreenBlocklistConfig(BaseModel):
    """Window title and process patterns excluded from screen capture."""

    model_config = ConfigDict(extra="forbid")

    window_titles: list[str] = Field(default_factory=list)
    processes: list[str] = Field(default_factory=list)


class VoiceConfig(BaseModel):
    """Validated settings loaded from ``voice_config.toml``."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    wake_word_model: str = Field(
        default="hey_jarvis",
        min_length=1,
        description="Built-in openwakeword model name or path to a .onnx wake-word file.",
    )
    wake_threshold: float = Field(default=0.30, ge=0.0, le=1.0)
    vad_mode: Literal["server", "client"] = Field(
        default="server",
        description="Voice Activity Detection mode: 'server' or 'client'.",
    )
    input_device: str | int | None = None
    output_device: str | int | None = None
    mic_mute_while_speaking: bool = False
    silence_timeout_seconds: float = Field(default=60.0, gt=0.0)
    hotkey: str = Field(default="Ctrl+Alt+V", min_length=1)
    max_burst_seconds: float = Field(default=30.0, gt=0.0)
    image_max_side: int = Field(default=768, ge=64)
    media_resolution: MediaResolution = "medium"
    blocklist: ScreenBlocklistConfig = Field(default_factory=ScreenBlocklistConfig)
    consent_mode: ConsentMode = "ask"
    reconnect_limit: int = Field(default=3, ge=0)
    enable_web_search: bool = False
    stall_timeout_seconds: float = Field(default=15.0, gt=0.0)
    preferred_hostapi: str = Field(default="WASAPI")

    @field_validator(
        "wake_threshold",
        "silence_timeout_seconds",
        "stall_timeout_seconds",
        "max_burst_seconds",
        "image_max_side",
        "reconnect_limit",
        mode="before",
    )
    @classmethod
    def _reject_bool_for_numeric(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError("Boolean value is not valid for a numeric setting.")
        return value

    @field_validator("input_device", "output_device", mode="before")
    @classmethod
    def _validate_device(cls, value: Any) -> Any:
        if isinstance(value, bool):
            raise ValueError(
                "Audio device must be a device name string, non-negative index, or null."
            )
        if isinstance(value, int) and value < 0:
            raise ValueError("Audio device index must be >= 0.")
        if isinstance(value, str):
            stripped = value.strip()
            if not stripped:
                return None
            return stripped
        return value

    @field_validator("wake_word_model", "hotkey", mode="after")
    @classmethod
    def _strip_non_empty(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("Value must not be empty or whitespace.")
        return stripped


def voice_config_path(data_dir: Path | None = None) -> Path:
    """Return the path to ``voice_config.toml`` in the Clea user data directory."""
    base = data_dir if data_dir is not None else default_data_dir()
    return base / VOICE_CONFIG_FILENAME


def default_voice_config_toml() -> str:
    """Return the commented default ``voice_config.toml`` file contents."""
    return """# Clea Voice Surface Configuration
# Stored in Clea's user data directory; edit and restart the voice surface to apply changes.

# Wake word model name (e.g. "hey_jarvis") or path to a custom .onnx model file.
wake_word_model = "hey_jarvis"

# Wake word detection threshold in [0.0, 1.0]. Calibrated for WASAPI input (0.30 default).
wake_threshold = 0.30

# Voice Activity Detection mode: "server" (server-side Gemini VAD) or "client".
vad_mode = "server"

# Global push-to-talk / call-toggle hotkey.
hotkey = "Ctrl+Alt+V"

# Audio input/output device names or numeric indices (uncomment to override system default).
# input_device = ""
# output_device = ""

# One-line half-duplex toggle: set to true to mute mic while Clea is speaking.
mic_mute_while_speaking = false

# Automatically end a call after this many seconds of silence.
silence_timeout_seconds = 60

# Stall watchdog timeout: reconnect if audio streamed for N seconds with no server response.
stall_timeout_seconds = 15

# Preferred audio host API (e.g. "WASAPI" on Windows) to avoid duplicate device entries.
preferred_hostapi = "WASAPI"

# Maximum automatic reconnection attempts on transient session drops or GoAway notices.
reconnect_limit = 3

# Google Search grounding: set to true to enable Live Google Search grounding tool.
enable_web_search = false

# Screen capture settings for look_at_screen and watch_screen.
# Consent mode: "ask" (prompt user), "auto_allow", or "deny".
consent_mode = "ask"
# Maximum continuous watch_screen burst duration in seconds.
max_burst_seconds = 30
# Resize captured images so their longest side is at most this many pixels.
image_max_side = 768
# Live vision media resolution: "low", "medium", or "high".
media_resolution = "medium"

[blocklist]
# Window title and process patterns (glob or substring) that must never be captured.
# Example: window_titles = ["*1Password*", "*Bitwarden*", "*Private*"]
window_titles = []
# Example: processes = ["1password.exe", "bitwarden.exe", "keepassxc.exe"]
processes = []
"""


def ensure_voice_config(path: Path | None = None) -> Path:
    """Create ``voice_config.toml`` with commented defaults if missing; return its path."""
    target = path if path is not None else voice_config_path()
    if not target.exists():
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(default_voice_config_toml(), encoding="utf-8")
    return target


def load_voice_config(
    path: Path | None = None,
    *,
    create_if_missing: bool = True,
) -> VoiceConfig:
    """Load and validate ``voice_config.toml`` using ``tomllib``."""
    target = path if path is not None else voice_config_path()
    if not target.exists():
        if not create_if_missing:
            return VoiceConfig()
        ensure_voice_config(target)

    raw_text = target.read_text(encoding="utf-8-sig")
    raw = tomllib.loads(raw_text)
    return VoiceConfig.model_validate(raw)

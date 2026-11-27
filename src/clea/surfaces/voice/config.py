"""Voice surface configuration stored in ``<user_data_dir>/voice_config.toml``."""

from __future__ import annotations

import os
import tomllib
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from clea.paths import default_data_dir

MediaResolution = Literal["low", "medium", "high"]
ConsentMode = Literal["ask", "auto_allow", "deny"]
WakeWordEngine = Literal["auto", "livekit", "openwakeword"]

VOICE_CONFIG_FILENAME = "voice_config.toml"
DEFAULT_VAD_SILENCE_DURATION_MS = 1000
RECOMMENDED_MIN_IMAGE_MAX_SIDE = 1280
RECOMMENDED_MEDIA_RESOLUTION: MediaResolution = "high"


class ScreenBlocklistConfig(BaseModel):
    """Window title and process patterns excluded from screen capture."""

    model_config = ConfigDict(extra="forbid")

    window_titles: list[str] = Field(default_factory=list)
    processes: list[str] = Field(default_factory=list)


class VoiceConfig(BaseModel):
    """Validated settings loaded from ``voice_config.toml``."""

    model_config = ConfigDict(extra="forbid", allow_inf_nan=False)

    wake_word_engine: WakeWordEngine = Field(
        default="auto",
        description=(
            "Wake word inference engine: 'auto' (livekit-wakeword v2 primary with "
            "startup-only openwakeword fallback), 'livekit', or 'openwakeword'."
        ),
    )
    wake_word_model: str = Field(
        default="hey_jarvis",
        min_length=1,
        description="Built-in wake-word model name or path to a .onnx wake-word file.",
    )
    wake_threshold: float = Field(default=0.30, ge=0.0, le=1.0)
    livekit_wake_threshold: float = Field(
        default=0.50,
        ge=0.0,
        le=1.0,
        description=(
            "Detection threshold in [0.0, 1.0] for livekit-wakeword v2 models (default 0.50)."
        ),
    )
    vad_mode: Literal["server", "client"] = Field(
        default="server",
        description="Voice Activity Detection mode: 'server' or 'client'.",
    )
    vad_silence_duration_ms: int = Field(
        default=DEFAULT_VAD_SILENCE_DURATION_MS,
        ge=100,
        le=10000,
        description=(
            "Server-side Gemini Live VAD end-of-speech silence duration in milliseconds."
        ),
    )
    input_device: str | int | None = None
    output_device: str | int | None = None
    mic_mute_while_speaking: bool = False
    silence_timeout_seconds: float = Field(default=60.0, gt=0.0)
    hotkey: str = Field(default="Ctrl+Alt+V", min_length=1)
    max_burst_seconds: float = Field(default=30.0, gt=0.0)
    image_max_side: int = Field(default=1920, ge=64)
    media_resolution: MediaResolution = "high"
    screen_indicator_enabled: bool = True
    screen_indicator_height_px: int = Field(default=3, ge=1, le=32)
    blocklist: ScreenBlocklistConfig = Field(default_factory=ScreenBlocklistConfig)
    consent_mode: ConsentMode = "ask"
    reconnect_limit: int = Field(default=3, ge=0)
    enable_web_search: bool = False
    transcription_language: str | None = Field(default="en-IN")
    stall_timeout_seconds: float = Field(default=15.0, gt=0.0)
    stall_speech_rms_threshold: float = Field(default=250.0, ge=0.0)
    preferred_hostapi: str = Field(default="WASAPI")

    @field_validator(
        "wake_threshold",
        "livekit_wake_threshold",
        "vad_silence_duration_ms",
        "silence_timeout_seconds",
        "stall_timeout_seconds",
        "stall_speech_rms_threshold",
        "max_burst_seconds",
        "image_max_side",
        "screen_indicator_height_px",
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

    @field_validator("transcription_language", mode="before")
    @classmethod
    def _validate_transcription_language(cls, value: Any) -> Any:
        if value is None:
            return None
        if isinstance(value, bool) or not isinstance(value, str):
            raise ValueError("transcription_language must be a BCP-47 string or null.")
        stripped = value.strip()
        return stripped or None

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

# Wake word engine: "auto" (livekit-wakeword v2 primary with startup-only openwakeword fallback),
# "livekit", or "openwakeword".
wake_word_engine = "auto"

# Wake word model name (e.g. "hey_jarvis") or path to a custom .onnx model file.
wake_word_model = "hey_jarvis"

# Legacy/openWakeWord detection threshold in [0.0, 1.0]. Calibrated for WASAPI input (0.30 default).
wake_threshold = 0.30

# Detection threshold in [0.0, 1.0] for livekit-wakeword v2 models (0.50 default).
livekit_wake_threshold = 0.50

# Voice Activity Detection mode: "server" (server-side Gemini VAD) or "client".
vad_mode = "server"

# Server-side Gemini Live VAD end-of-speech silence duration in milliseconds (default 1000).
# Also configurable via CLEA_VAD_SILENCE_DURATION_MS environment variable.
vad_silence_duration_ms = 1000

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

# Speech energy threshold (RMS) to arm stall watchdog timer. Below this, audio is silence.
stall_speech_rms_threshold = 250.0

# Preferred audio host API (e.g. "WASAPI" on Windows) to avoid duplicate device entries.
preferred_hostapi = "WASAPI"

# Maximum automatic reconnection attempts on transient session drops or GoAway notices.
reconnect_limit = 3

# Google Search grounding: set to true to enable Live Google Search grounding tool.
enable_web_search = false

# Lock input audio transcription language (BCP-47, e.g. "en-IN" or "en-US"; set "" to auto-detect).
transcription_language = "en-IN"

# Screen capture settings for look_at_screen and watch_screen.
# Consent mode: "ask" (prompt user), "auto_allow", or "deny".
consent_mode = "ask"
# Maximum continuous watch_screen burst duration in seconds.
max_burst_seconds = 30
# Resize full_screen captures so their longest side is at most this many pixels
# (active_window captures stay 1:1 native pixel resolution).
image_max_side = 1920
# Live vision media resolution: "low", "medium", or "high".
media_resolution = "high"
# Show a thin click-through red top-edge bar while screen vision is active.
screen_indicator_enabled = true
# Height in pixels of the top-edge screen-monitoring bar.
screen_indicator_height_px = 3

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


def _apply_voice_env_overrides(raw: dict[str, Any]) -> dict[str, Any]:
    """Apply supported environment variable overrides on top of TOML config."""
    merged = dict(raw)
    raw_vad_ms = os.environ.get("CLEA_VAD_SILENCE_DURATION_MS")
    if raw_vad_ms is not None and raw_vad_ms.strip():
        merged["vad_silence_duration_ms"] = raw_vad_ms.strip()
    return merged


def load_voice_config(
    path: Path | None = None,
    *,
    create_if_missing: bool = True,
) -> VoiceConfig:
    """Load and validate ``voice_config.toml`` using ``tomllib``."""
    target = path if path is not None else voice_config_path()
    if not target.exists():
        if not create_if_missing:
            return VoiceConfig.model_validate(_apply_voice_env_overrides({}))
        ensure_voice_config(target)

    raw_text = target.read_text(encoding="utf-8-sig")
    raw = tomllib.loads(raw_text)
    return VoiceConfig.model_validate(_apply_voice_env_overrides(raw))


def check_legacy_vision_config_warnings(
    config: VoiceConfig,
    config_path: Path | None = None,
) -> list[str]:
    """Return non-destructive warnings when ``voice_config.toml`` uses degraded vision settings."""
    warnings: list[str] = []
    source = str(config_path) if config_path is not None else VOICE_CONFIG_FILENAME
    if config.image_max_side < RECOMMENDED_MIN_IMAGE_MAX_SIDE:
        warnings.append(
            f"Legacy vision config in {source}: image_max_side={config.image_max_side} "
            f"is below the recommended minimum of {RECOMMENDED_MIN_IMAGE_MAX_SIDE} "
            "(default 1920). Raise image_max_side for sharper screen grounding."
        )
    if config.media_resolution != RECOMMENDED_MEDIA_RESOLUTION:
        warnings.append(
            f"Legacy vision config in {source}: media_resolution={config.media_resolution!r} "
            f"is below the recommended {RECOMMENDED_MEDIA_RESOLUTION!r}. "
            'Set media_resolution = "high" for accurate visual coordinate calibration.'
        )
    return warnings

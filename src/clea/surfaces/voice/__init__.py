"""Voice surface package for Clea (config, backend interfaces, and deferred memory stubs)."""

from __future__ import annotations

from clea.surfaces.voice.config import (
    ConsentMode,
    MediaResolution,
    ScreenBlocklistConfig,
    VoiceConfig,
    default_voice_config_toml,
    ensure_voice_config,
    load_voice_config,
    voice_config_path,
)

__all__ = [
    "ConsentMode",
    "MediaResolution",
    "ScreenBlocklistConfig",
    "VoiceConfig",
    "default_voice_config_toml",
    "ensure_voice_config",
    "load_voice_config",
    "voice_config_path",
]

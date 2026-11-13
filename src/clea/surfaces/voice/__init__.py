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
from clea.surfaces.voice.memory_hooks import (
    BASE_VOICE_SYSTEM_INSTRUCTION,
    build_voice_system_instruction,
    consolidate_voice_call,
    load_voice_memory_context,
    resolve_voice_system_instruction,
    safe_consolidate_voice_call,
    safe_load_voice_memory_context,
    safe_store_voice_transcript,
    store_voice_transcript,
)

__all__ = [
    "BASE_VOICE_SYSTEM_INSTRUCTION",
    "ConsentMode",
    "MediaResolution",
    "ScreenBlocklistConfig",
    "VoiceConfig",
    "build_voice_system_instruction",
    "consolidate_voice_call",
    "default_voice_config_toml",
    "ensure_voice_config",
    "load_voice_config",
    "load_voice_memory_context",
    "resolve_voice_system_instruction",
    "safe_consolidate_voice_call",
    "safe_load_voice_memory_context",
    "safe_store_voice_transcript",
    "store_voice_transcript",
    "voice_config_path",
]

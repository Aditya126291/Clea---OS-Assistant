"""Voice surface package for Clea (config, backend interfaces, and deferred memory stubs)."""

from __future__ import annotations

from clea.surfaces.voice.audio import AudioManager, list_audio_devices
from clea.surfaces.voice.backends.gemini_live import GeminiLiveBackend
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
from clea.surfaces.voice.lock import (
    VoiceAlreadyRunningError,
    VoiceInstanceLock,
    voice_lock_path,
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
from clea.surfaces.voice.runner import VoiceRunner, voice_metrics_path
from clea.surfaces.voice.wake_word import (
    WakeWordDetector,
    WakeWordModelNotFoundError,
    check_models_status,
    download_wake_word_models,
    wake_word_models_dir,
)
from clea.surfaces.voice.win32 import (
    HotKeyManager,
    WindowInfo,
    dpi_to_scale_factor,
    enable_per_monitor_dpi_awareness,
    enumerate_visible_windows,
    get_window_process_name,
    get_window_title,
    scale_coordinate,
    scale_point,
    scale_rect,
    unscale_coordinate,
    unscale_point,
    unscale_rect,
)

__all__ = [
    "AudioManager",
    "BASE_VOICE_SYSTEM_INSTRUCTION",
    "ConsentMode",
    "GeminiLiveBackend",
    "HotKeyManager",
    "MediaResolution",
    "ScreenBlocklistConfig",
    "VoiceAlreadyRunningError",
    "VoiceConfig",
    "VoiceInstanceLock",
    "VoiceRunner",
    "WakeWordDetector",
    "WakeWordModelNotFoundError",
    "WindowInfo",
    "build_voice_system_instruction",
    "check_models_status",
    "consolidate_voice_call",
    "default_voice_config_toml",
    "download_wake_word_models",
    "dpi_to_scale_factor",
    "enable_per_monitor_dpi_awareness",
    "ensure_voice_config",
    "enumerate_visible_windows",
    "get_window_process_name",
    "get_window_title",
    "list_audio_devices",
    "load_voice_config",
    "load_voice_memory_context",
    "resolve_voice_system_instruction",
    "safe_consolidate_voice_call",
    "safe_load_voice_memory_context",
    "safe_store_voice_transcript",
    "scale_coordinate",
    "scale_point",
    "scale_rect",
    "store_voice_transcript",
    "unscale_coordinate",
    "unscale_point",
    "unscale_rect",
    "voice_config_path",
    "voice_lock_path",
    "voice_metrics_path",
    "wake_word_models_dir",
]

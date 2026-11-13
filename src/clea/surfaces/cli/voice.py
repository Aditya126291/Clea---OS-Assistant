"""Voice CLI handlers for ``clea voice``, ``clea voice doctor``, and ``clea voice test``."""

from __future__ import annotations

import asyncio
import os
import sys

import numpy as np

from clea.core import auth
from clea.paths import default_data_dir
from clea.surfaces.voice.audio import AudioManager, list_audio_devices
from clea.surfaces.voice.backends.base import OUTPUT_AUDIO_SAMPLE_RATE_HZ
from clea.surfaces.voice.config import load_voice_config, voice_config_path
from clea.surfaces.voice.lock import VoiceAlreadyRunningError
from clea.surfaces.voice.runner import VoiceRunner
from clea.surfaces.voice.wake_word import (
    WakeWordDetector,
    WakeWordModelNotFoundError,
    check_models_status,
    download_wake_word_models,
    wake_word_models_dir,
)


def run_voice_service(*, host: str = "127.0.0.1", port: int = 8765) -> None:
    """Run the main Clea voice background surface."""
    config = load_voice_config()
    daemon_url = f"ws://{host}:{port}"
    token = auth.read_token()

    if not token:
        print(
            "Note: Clea daemon is not running yet. Starting voice surface;\n"
            f"it will automatically connect when the daemon starts at {daemon_url}.",
            file=sys.stderr,
        )

    try:
        runner = VoiceRunner(
            config=config,
            daemon_url=daemon_url,
            token=token,
        )
        print("Starting Clea voice service (press Ctrl+C to quit)...")
        print(f"Wake word: '{config.wake_word_model}' (threshold: {config.wake_threshold})")
        print(f"Toggle hotkey: {config.hotkey}")
        asyncio.run(runner.run_forever())
    except VoiceAlreadyRunningError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except WakeWordModelNotFoundError as exc:
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        print("\nClea voice stopped.")


def run_voice_doctor(*, download_models: bool = False) -> None:
    """Run system and dependency diagnostics for the voice surface."""
    print("=== Clea Voice Doctor ===")
    print(f"Python: {sys.version.split()[0]} ({sys.platform})")
    print(f"Data directory: {default_data_dir()}")
    print(f"Config path: {voice_config_path()}")

    config = load_voice_config()
    print(f"Config: wake_word='{config.wake_word_model}', hotkey='{config.hotkey}'")
    print(f"        google_search_grounding={config.enable_web_search}")

    # 1. Check GEMINI_API_KEY
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    if api_key:
        print("[OK] GEMINI_API_KEY is configured.")
    else:
        print("[!] GEMINI_API_KEY is not set. Live voice calls will fail without an API key.")

    # 2. Check Audio Devices
    devices = list_audio_devices()
    input_devs = [d for d in devices if d["max_input_channels"] > 0]
    output_devs = [d for d in devices if d["max_output_channels"] > 0]

    if input_devs:
        print(f"[OK] Found {len(input_devs)} audio input device(s):")
        for d in input_devs[:3]:
            print(f"     - #{d['index']}: {d['name']}")
    else:
        print("[!] No audio input devices detected.")

    if output_devs:
        print(f"[OK] Found {len(output_devs)} audio output device(s):")
        for d in output_devs[:3]:
            print(f"     - #{d['index']}: {d['name']}")
    else:
        print("[!] No audio output devices detected.")

    # 3. Check Wake Word Models
    models_dir = wake_word_models_dir()
    print(f"Models directory: {models_dir}")
    all_present, missing = check_models_status(config.wake_word_model)

    if all_present:
        print(f"[OK] All required wake word models for '{config.wake_word_model}' are present.")
    else:
        print(f"[!] Missing {len(missing)} wake word model file(s) for '{config.wake_word_model}':")
        for name, url, path in missing:
            print(f"     - {name}: {path.name}")
            if url:
                print(f"       Source URL: {url}")
            else:
                print("       Source: local custom path")

        if download_models:
            print("\nDownloading missing models to models directory...")
            try:
                download_wake_word_models(
                    config.wake_word_model,
                    print_fn=lambda msg: print(f"  {msg}"),
                )
                print("[OK] Model download complete.")
            except Exception as exc:
                print(f"[FAIL] Download failed: {exc}", file=sys.stderr)
                raise SystemExit(1) from exc
        else:
            print("\nTo download missing models, run:")
            print("  clea voice doctor --download-models")
            print("Note: Clea never downloads models silently without this explicit flag.")

    # 4. Check Daemon
    token = auth.read_token()
    if token:
        print("[OK] Daemon session token found (daemon is running).")
    else:
        print("[i] Daemon session token not found. (Start daemon with 'clea daemon')")

    print("=========================")


def run_voice_test() -> None:
    """Run interactive audio and wake word test."""
    print("=== Clea Voice Test ===")
    config = load_voice_config()

    # 1. Output test: play a 0.2s 440 Hz test beep
    print("1. Testing audio output (24 kHz playback)...")
    audio = AudioManager(
        input_device=config.input_device,
        output_device=config.output_device,
    )
    audio.start()
    try:
        duration = 0.2
        t = np.linspace(0, duration, int(OUTPUT_AUDIO_SAMPLE_RATE_HZ * duration), False)
        tone = (np.sin(2 * np.pi * 440 * t) * 8000).astype(np.int16)
        audio.play_audio(tone.tobytes())
        print("   [OK] Audio beep sent to output device.")

        # 2. Wake word model verification
        print("2. Testing wake word detector loading...")
        all_present, missing = check_models_status(config.wake_word_model)
        if not all_present:
            print(
                f"   [!] Missing wake word models ({len(missing)} files). "
                "Run 'clea voice doctor --download-models'."
            )
        else:
            try:
                detector = WakeWordDetector(
                    model_name=config.wake_word_model,
                    threshold=config.wake_threshold,
                )
                # Feed a dummy 0.1s silence chunk
                silence = b"\x00" * 3200
                det, score, key = detector.process_audio(silence)
                print(
                    f"   [OK] Wake word model loaded successfully (dummy test score: {score:.3f})."
                )
            except Exception as exc:
                print(f"   [FAIL] Wake word detector failed: {exc}")
    finally:
        audio.stop()
    print("========================")

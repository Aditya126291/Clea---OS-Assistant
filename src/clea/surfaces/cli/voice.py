"""Voice CLI handlers for ``clea voice``, ``clea voice doctor``, and ``clea voice test``."""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import time
from typing import Any

import numpy as np

from clea.core import auth
from clea.paths import default_data_dir
from clea.surfaces.voice.audio import (
    AudioManager,
    compute_audio_rms,
    list_audio_devices,
    resolve_audio_device,
)
from clea.surfaces.voice.backends.base import (
    OUTPUT_AUDIO_SAMPLE_RATE_HZ,
    AudioOutputEvent,
    TranscriptOutputEvent,
    TurnCompleteEvent,
    VoiceSessionConfig,
)
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
from clea.surfaces.voice.win32 import enable_per_monitor_dpi_awareness


def _run_mic_check(device_idx: int, duration: float = 3.0) -> tuple[float, float]:
    """Record 3 seconds from device_idx to measure RMS and peak in memory (never saved to disk)."""
    import sounddevice as sd

    try:
        data = sd.rec(
            int(duration * 16000), samplerate=16000, channels=1, dtype="int16", device=device_idx
        )
        sd.wait()
    except Exception:
        dev_info = sd.query_devices(device_idx)
        native_rate = int(dev_info.get("default_samplerate", 44100))
        data = sd.rec(
            int(duration * native_rate),
            samplerate=native_rate,
            channels=1,
            dtype="int16",
            device=device_idx,
        )
        sd.wait()

    arr = data.flatten().astype(np.float64)
    if len(arr) == 0:
        return 0.0, 0.0
    rms = float(np.sqrt(np.mean(arr**2)))
    peak = float(np.max(np.abs(arr)))
    return rms, peak


async def _check_daemon_ping(
    token: str, host: str = "127.0.0.1", port: int = 8765
) -> tuple[bool, str]:
    """Send health.ping over WebSocket to the running daemon."""
    from websockets.asyncio.client import connect

    from clea.core.protocol import HealthPingRequest, ResponseFrame, parse_frame

    ws_url = f"ws://{host}:{port}"
    try:
        async with connect(
            ws_url,
            subprotocols=auth.client_subprotocols(token),
            open_timeout=2.0,
        ) as ws:
            req = HealthPingRequest()
            await ws.send(req.model_dump_json())
            raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
            frame = parse_frame(raw)
            if isinstance(frame, ResponseFrame) and frame.ok:
                return True, f"Daemon answered health.ping over {ws_url} (healthy)."
            return False, f"Daemon returned non-OK frame: {frame}"
    except Exception as exc:
        return False, f"Daemon did not respond to health.ping at {ws_url} ({exc})."


async def _check_live_api_connection(api_key: str) -> tuple[bool, str]:
    """Test connection handshake to Gemini Live API."""
    from clea.surfaces.voice.backends.gemini_live import GeminiLiveBackend

    backend = GeminiLiveBackend(api_key=api_key)
    try:
        await backend.connect(VoiceSessionConfig())
        await asyncio.sleep(0.5)
        return True, "Connected successfully to Gemini Live API (gemini-3.8-live)."
    except Exception as exc:
        return False, f"Gemini Live connection failed: {exc}"
    finally:
        await backend.close()


async def _run_live_voice_test_prompt(
    api_key: str, audio: AudioManager, *, debug_audio: bool = False
) -> None:
    """Send a test prompt to Gemini Live and stream spoken audio output."""
    from clea.surfaces.voice.backends.gemini_live import GeminiLiveBackend

    backend = GeminiLiveBackend(api_key=api_key)
    try:
        await backend.connect(VoiceSessionConfig())
        prompt = "Please say: 'Clea voice test successful.'"
        if debug_audio:
            print(f"   [AUDIO-DEBUG] Sending prompt to Gemini Live: \"{prompt}\"")
        await backend.send_text(prompt, end_of_turn=True)

        async for event in backend.events():
            if debug_audio:
                evt_type = type(event).__name__
                desc = ""
                if isinstance(event, AudioOutputEvent):
                    desc = f"({len(event.pcm_bytes)}b audio)"
                elif isinstance(event, TranscriptOutputEvent):
                    desc = f"({event.role}: '{event.text}')"
                print(f"   [AUDIO-DEBUG] RECV server message: {evt_type} {desc}", flush=True)

            if isinstance(event, AudioOutputEvent):
                audio.play_audio(event.pcm_bytes)
            elif isinstance(event, TranscriptOutputEvent):
                if event.role == "assistant":
                    print(f"   Clea: {event.text}", flush=True)
            elif isinstance(event, TurnCompleteEvent):
                print("   [OK] Live spoken response completed.")
                break
    except Exception as exc:
        print(f"   [FAIL] Live prompt test failed: {exc}")
    finally:
        await backend.close()


def run_voice_service(
    *, host: str = "127.0.0.1", port: int = 8765, debug_audio: bool = False
) -> None:
    """Run the main Clea voice background surface."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    enable_per_monitor_dpi_awareness()
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
            debug_audio=debug_audio,
        )
        print("Starting Clea voice service (press Ctrl+C to quit)...")
        print(f"Wake word: '{config.wake_word_model}' (threshold: {config.wake_threshold})")
        print(f"Toggle hotkey: {config.hotkey}")
        print(f"Listening on mic for '{config.wake_word_model}' or press {config.hotkey}...")
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
    print(f"        preferred_hostapi='{config.preferred_hostapi}'")

    # 1. Check GEMINI_LIVE_API_KEY / GEMINI_API_KEY
    live_key = os.environ.get("GEMINI_LIVE_API_KEY", "").strip()
    api_key = os.environ.get("GEMINI_API_KEY", "").strip()
    active_key = live_key or api_key
    if live_key:
        print("[OK] GEMINI_LIVE_API_KEY is configured for voice calls.")
    elif api_key:
        print("[OK] GEMINI_API_KEY is configured (used as fallback for voice calls).")
    else:
        print(
            "[!] Neither GEMINI_LIVE_API_KEY nor GEMINI_API_KEY is set. "
            "Live voice calls will fail without an API key."
        )

    # 2. Check Audio Devices & Selected Device Resolution
    devices = list_audio_devices()
    input_devs = [d for d in devices if d["max_input_channels"] > 0]
    output_devs = [d for d in devices if d["max_output_channels"] > 0]

    if input_devs:
        print(f"[OK] Found {len(input_devs)} audio input device(s):")
        for d in input_devs[:3]:
            print(f"     - #{d['index']}: {d['name']} ({d.get('hostapi_name', 'Unknown')})")
    else:
        print("[!] No audio input devices detected.")

    if output_devs:
        print(f"[OK] Found {len(output_devs)} audio output device(s):")
        for d in output_devs[:3]:
            print(f"     - #{d['index']}: {d['name']} ({d.get('hostapi_name', 'Unknown')})")
    else:
        print("[!] No audio output devices detected.")

    chosen_input: dict[str, Any] | None = None
    try:
        chosen_input = resolve_audio_device(
            config.input_device, kind="input", preferred_hostapi=config.preferred_hostapi
        )
        print(
            f"[OK] Selected input device: #{chosen_input['index']} '{chosen_input['name']}' "
            f"(host API: {chosen_input.get('hostapi_name', 'Unknown')}, "
            f"native rate: {int(chosen_input.get('default_samplerate', 16000))} Hz)"
        )
    except Exception as exc:
        print(f"[FAIL] Could not resolve input device '{config.input_device}': {exc}")

    try:
        chosen_output = resolve_audio_device(
            config.output_device, kind="output", preferred_hostapi=config.preferred_hostapi
        )
        print(
            f"[OK] Selected output device: #{chosen_output['index']} '{chosen_output['name']}' "
            f"(host API: {chosen_output.get('hostapi_name', 'Unknown')}, "
            f"native rate: {int(chosen_output.get('default_samplerate', 24000))} Hz)"
        )
    except Exception as exc:
        print(f"[FAIL] Could not resolve output device '{config.output_device}': {exc}")

    # 3. 3-second live mic check (reporting RMS and peak, warning if near-silent, NEVER saves audio)
    if chosen_input is not None:
        print(f"Running 3-second microphone check on #{chosen_input['index']} '{chosen_input['name']}'...")
        try:
            rms, peak = _run_mic_check(chosen_input["index"])
            print(f"[OK] 3-second mic check: RMS = {rms:.1f}, Peak = {peak:.1f}")
            if rms < 50.0:
                print(
                    "[!] Warning: Microphone is near-silent (RMS < 50.0). "
                    "Ensure microphone is unmuted and the correct physical device is selected."
                )
        except Exception as exc:
            print(f"[!] Microphone check error: {exc}")

    # 4. Check Wake Word Models
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

    # 5. Check Daemon (token + live health.ping)
    token = auth.read_token()
    if token:
        print("[OK] Daemon session token found.")
        try:
            ping_ok, ping_msg = asyncio.run(_check_daemon_ping(token=token))
            if ping_ok:
                print(f"[OK] {ping_msg}")
            else:
                print(f"[!] {ping_msg}")
        except Exception as exc:
            print(f"[!] Daemon ping check error: {exc}")
    else:
        print("[i] Daemon session token not found. (Start daemon with 'clea daemon')")

    # 6. Live API connection check
    if active_key:
        print("Checking Gemini Live API connection...")
        try:
            live_ok, live_msg = asyncio.run(_check_live_api_connection(active_key))
            if live_ok:
                print(f"[OK] {live_msg}")
            else:
                print(f"[!] Live API check: {live_msg}")
        except Exception as exc:
            print(f"[!] Live API check error: {exc}")
    else:
        print("[i] Live API connection check skipped (no API key configured).")

    print("=========================")


def run_voice_test(*, debug_audio: bool = False) -> None:
    """Run interactive audio and wake word test."""
    enable_per_monitor_dpi_awareness()
    print("=== Clea Voice Test ===")
    config = load_voice_config()

    # 1. Output test: play a 0.2s 440 Hz test beep
    print("1. Testing audio output (24 kHz playback)...")
    audio = AudioManager(
        input_device=config.input_device,
        output_device=config.output_device,
        preferred_hostapi=config.preferred_hostapi,
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
                # Feed a dummy 1280-sample (2560 bytes) silence chunk
                silence = b"\x00" * 2560
                det, score, key = detector.process_audio(silence)
                print(
                    f"   [OK] Wake word model loaded successfully (dummy test score: {score:.3f})."
                )
            except Exception as exc:
                print(f"   [FAIL] Wake word detector failed: {exc}")

        # 3. If debug_audio is True, test mic input stream for 2 seconds and print statistics
        if debug_audio:
            print("3. Testing live microphone input stream (--debug-audio)...")
            chunks_count = 0
            rms_vals: list[float] = []

            def _test_cb(pcm: bytes) -> None:
                nonlocal chunks_count
                chunks_count += 1
                rms = compute_audio_rms(pcm)
                rms_vals.append(rms)

            audio.set_input_callback(_test_cb)
            start_t = time.monotonic()
            while time.monotonic() - start_t < 2.0:
                time.sleep(0.1)

            elapsed = time.monotonic() - start_t
            fps = chunks_count / elapsed if elapsed > 0 else 0.0
            avg_rms = sum(rms_vals) / len(rms_vals) if rms_vals else 0.0
            print(f"   [OK] Mic stream: {fps:.1f} fps | Avg RMS: {avg_rms:.1f} ({chunks_count} frames)")

        # 4. Interactive Live API spoken prompt test if key configured
        live_key = os.environ.get("GEMINI_LIVE_API_KEY", "").strip() or os.environ.get("GEMINI_API_KEY", "").strip()
        if live_key:
            print("4. Testing Live API text prompt -> spoken answer...")
            asyncio.run(_run_live_voice_test_prompt(live_key, audio, debug_audio=debug_audio))
        else:
            print("4. Live API prompt test skipped (no API key configured).")

    finally:
        audio.stop()
    print("========================")

"""Voice CLI handlers for ``clea voice``, ``clea voice doctor``, and ``clea voice test``."""

from __future__ import annotations

import asyncio
import logging
import logging.handlers
import os
import sys
import time
from collections.abc import Callable
from typing import Any

import numpy as np
import sounddevice as sd

from clea.core import auth
from clea.paths import default_data_dir
from clea.surfaces.voice.audio import (
    AmbiguousAudioDeviceError,
    AudioDeviceNotFoundError,
    AudioManager,
    compute_audio_rms,
    list_audio_devices,
    list_candidate_audio_devices,
    negotiate_input_sample_rate,
    resample_to_16k_mono,
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
from clea.surfaces.voice.runner import VoiceRunner, voice_log_path
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

    dev_info = sd.query_devices(device_idx)
    native_rate = int(dev_info.get("default_samplerate", 16000))

    extra = None
    if hasattr(sd, "WasapiSettings"):
        try:
            hostapi = sd.query_hostapis(dev_info.get("hostapi", -1))
            if "WASAPI" in hostapi.get("name", "").upper():
                extra = sd.WasapiSettings(auto_convert=True)
        except Exception:
            pass

    sample_rate, channels = negotiate_input_sample_rate(
        device_idx=device_idx,
        default_samplerate=native_rate,
        preferred_channels=1,
    )

    data = sd.rec(
        int(duration * sample_rate),
        samplerate=sample_rate,
        channels=channels,
        dtype="int16",
        device=device_idx,
        extra_settings=extra,
    )
    sd.wait()

    resampled_16k = resample_to_16k_mono(data, orig_sr=sample_rate, target_sr=16000)
    arr = resampled_16k.flatten().astype(np.float64)
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
        await backend.connect(VoiceSessionConfig(system_instruction="Diagnostic connection check."))
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
        await backend.connect(
            VoiceSessionConfig(
                system_instruction=(
                    "You are a helpful voice assistant. Follow user requests concisely."
                )
            )
        )
        prompt = "Please say: 'Clea voice test successful.'"
        if debug_audio:
            print(f'   [AUDIO-DEBUG] Sending prompt to Gemini Live: "{prompt}"')
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


logger = logging.getLogger(__name__)


def _configure_voice_service_logging() -> None:
    """Configure console logging in foreground or rotating file logging under pythonw."""
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    if sys.stderr is None:
        log_file = voice_log_path()
        log_file.parent.mkdir(parents=True, exist_ok=True)
        handler = logging.handlers.RotatingFileHandler(
            log_file,
            maxBytes=5 * 1024 * 1024,
            backupCount=3,
            encoding="utf-8",
        )
        handler.setFormatter(logging.Formatter(fmt))
        logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    else:
        logging.basicConfig(
            level=logging.INFO,
            format="%(asctime)s [%(levelname)s] %(message)s",
            datefmt="%H:%M:%S",
        )


def run_voice_service(
    *, host: str = "127.0.0.1", port: int = 8765, debug_audio: bool = False
) -> None:
    """Run the main Clea voice background surface."""
    _configure_voice_service_logging()
    enable_per_monitor_dpi_awareness()
    daemon_url = f"ws://{host}:{port}"

    try:
        config = load_voice_config()
        if not auth.read_token():
            logger.info(
                "Clea daemon session token not found yet; voice surface will connect when "
                "daemon starts at %s.",
                daemon_url,
            )
            print(
                "Note: Clea daemon is not running yet. Starting voice surface;\n"
                f"it will automatically connect when the daemon starts at {daemon_url}.",
                file=sys.stderr,
            )

        runner = VoiceRunner(
            config=config,
            daemon_url=daemon_url,
            token=None,
            debug_audio=debug_audio,
        )
        print("Starting Clea voice service (press Ctrl+C to quit)...")
        print(f"Wake word: '{config.wake_word_model}' (threshold: {config.wake_threshold})")
        print(f"Toggle hotkey: {config.hotkey}")
        print(f"Listening on mic for '{config.wake_word_model}' or press {config.hotkey}...")
        asyncio.run(runner.run_forever())
    except VoiceAlreadyRunningError as exc:
        logger.error("%s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except WakeWordModelNotFoundError as exc:
        logger.error("%s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except (AudioDeviceNotFoundError, AmbiguousAudioDeviceError, sd.PortAudioError, OSError) as exc:
        logger.error("Voice service startup failed: %s", exc)
        print(f"Error: {exc}", file=sys.stderr)
        raise SystemExit(1) from exc
    except KeyboardInterrupt:
        print("\nClea voice stopped.")
    except Exception:
        logger.exception("Clea voice service failed unexpectedly")
        raise


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
        name_trunc = chosen_input["name"][:30]
        print(f"Running 3-second microphone check on #{chosen_input['index']} '{name_trunc}'...")
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
            if chunks_count == 0:
                print(
                    f"   [FAIL] Mic stream: 0 frames captured in {elapsed:.1f}s. "
                    "Microphone may be muted, in exclusive use, or needs permissions."
                )
            else:
                print(
                    f"   [OK] Mic stream: {fps:.1f} fps | "
                    f"Avg RMS: {avg_rms:.1f} ({chunks_count} frames)"
                )

        # 4. Interactive Live API spoken prompt test if key configured
        live_key = (
            os.environ.get("GEMINI_LIVE_API_KEY", "").strip()
            or os.environ.get("GEMINI_API_KEY", "").strip()
        )
        if live_key:
            print("4. Testing Live API text prompt -> spoken answer...")
            asyncio.run(_run_live_voice_test_prompt(live_key, audio, debug_audio=debug_audio))
        else:
            print("4. Live API prompt test skipped (no API key configured).")

    finally:
        audio.stop()
    print("========================")


def run_voice_diagnose_mic(
    *,
    duration: float = 2.0,
    print_fn: Callable[[str], None] = print,
    audio_manager_factory: Callable[..., AudioManager] | None = None,
    detector_factory: Callable[..., WakeWordDetector] | None = None,
) -> dict[str, Any]:
    """Read-only microphone & wake-word diagnostic (never mutates %LOCALAPPDATA%\\clea).

    Prints candidate input devices, active selected device, live RMS levels over
    *duration* seconds (default 2.0s), and peak wake-word model score.
    """
    enable_per_monitor_dpi_awareness()
    config = load_voice_config(create_if_missing=False)
    sample_duration = max(0.2, float(duration))

    print_fn("=== Clea Voice Mic Diagnostic (Read-Only) ===")
    candidates: list[dict[str, Any]] = []
    try:
        candidates = list_candidate_audio_devices(
            config.input_device,
            kind="input",
            preferred_hostapi=config.preferred_hostapi,
        )
    except Exception as exc:
        print_fn(f"[FAIL] Could not enumerate candidate input devices: {exc}")
        return {
            "ok": False,
            "error": str(exc),
            "candidates": [],
            "selected_device": None,
            "mean_rms": 0.0,
            "peak_rms": 0.0,
            "peak_wake_score": 0.0,
            "frames": 0,
        }

    print_fn(f"Candidate input devices ({len(candidates)}):")
    for idx_pos, dev in enumerate(candidates):
        print_fn(
            f"  {idx_pos + 1}. #{dev['index']} '{dev['name']}' "
            f"(host API: {dev.get('hostapi_name', 'Unknown')}, "
            f"native rate: {int(dev.get('default_samplerate', 16000))} Hz, "
            f"in_ch: {dev.get('max_input_channels', 0)})"
        )

    detector: WakeWordDetector | None = None
    detector_note = ""
    if detector_factory is not None:
        detector = detector_factory(
            model_name=config.wake_word_model,
            threshold=config.wake_threshold,
        )
    else:
        all_present, _missing = check_models_status(config.wake_word_model)
        if all_present:
            try:
                detector = WakeWordDetector(
                    model_name=config.wake_word_model,
                    threshold=config.wake_threshold,
                )
            except Exception as exc:
                detector_note = f"detector init failed: {exc}"
        else:
            detector_note = "wake-word model files not downloaded"

    rms_values: list[float] = []
    peak_wake_score = 0.0
    wake_triggered = False

    def _on_chunk(pcm_bytes: bytes) -> None:
        nonlocal peak_wake_score, wake_triggered
        rms = compute_audio_rms(pcm_bytes)
        rms_values.append(rms)
        if detector is not None:
            try:
                det, score, _key = detector.process_audio(pcm_bytes)
                if score > peak_wake_score:
                    peak_wake_score = float(score)
                if det:
                    wake_triggered = True
            except Exception:
                pass

    factory = audio_manager_factory or AudioManager
    audio = factory(
        input_device=config.input_device,
        output_device=config.output_device,
        preferred_hostapi=config.preferred_hostapi,
        on_audio_input=_on_chunk,
        chunk_size=1280,
    )

    selected: dict[str, Any] | None = None
    try:
        audio.start()
        selected = dict(audio.resolved_input_device) if audio.resolved_input_device else None
        if selected:
            print_fn(
                f"Active selected input: #{selected['index']} '{selected['name']}' "
                f"(host API: {selected.get('hostapi_name', 'Unknown')}, "
                f"negotiated: {audio.input_sample_rate} Hz, {audio.input_channels}ch)"
            )
        print_fn(
            f"Sampling live mic audio for {sample_duration:.1f}s "
            f"(say '{config.wake_word_model}' to test wake score)..."
        )
        time.sleep(sample_duration)
    finally:
        audio.stop()

    mean_rms = float(sum(rms_values) / len(rms_values)) if rms_values else 0.0
    peak_rms = float(max(rms_values)) if rms_values else 0.0
    print_fn(
        f"Live 16 kHz mono int16 telemetry: frames={len(rms_values)}, "
        f"mean_rms={mean_rms:.1f}, peak_rms={peak_rms:.1f}"
    )
    if detector is not None:
        print_fn(
            f"Wake word ({config.wake_word_model}): peak_score={peak_wake_score:.3f} "
            f"(threshold={config.wake_threshold:.2f}, triggered={wake_triggered})"
        )
    else:
        print_fn(f"Wake word ({config.wake_word_model}): skipped ({detector_note})")

    if len(rms_values) == 0 or peak_rms < 3.0:
        print_fn(
            "[!] Warning: Captured audio is silent/dead (peak_rms < 3.0). "
            "Check mic mute, Bluetooth HFP/A2DP conflict, or Windows privacy permissions."
        )
    print_fn("=============================================")
    return {
        "ok": True,
        "candidates": candidates,
        "selected_device": selected,
        "sample_rate": audio.input_sample_rate,
        "channels": audio.input_channels,
        "frames": len(rms_values),
        "mean_rms": round(mean_rms, 2),
        "peak_rms": round(peak_rms, 2),
        "peak_wake_score": round(peak_wake_score, 4),
        "wake_threshold": config.wake_threshold,
        "wake_triggered": wake_triggered,
    }

#!/usr/bin/env python3
"""Standalone wake word probe utility for diagnosing microphone input and openWakeWord.

Opens the configured/specified audio input device and streams 16 kHz 16-bit mono PCM
in 1280-sample (80 ms) chunks.
Prints every ~200 ms:
  - Input device name & index
  - Mic RMS energy (amplitude)
  - Current wake word score
  - Max score observed over the trailing 1.0 second

Prints a clear detection banner when the configured threshold is crossed.
Never saves audio, screenshots, or logs to disk.
"""

from __future__ import annotations

import argparse
import collections
import sys
import time
from typing import Any

import numpy as np
import sounddevice as sd

from clea.surfaces.voice.audio import (
    AudioChunkBuffer,
    compute_audio_rms,
    negotiate_input_sample_rate,
    resample_to_16k_mono,
    resolve_audio_device,
)
from clea.surfaces.voice.config import load_voice_config
from clea.surfaces.voice.wake_word import WakeWordDetector


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Probe microphone input and wake word scoring in real-time."
    )
    parser.add_argument(
        "--device",
        type=str,
        default=None,
        help="Input device name substring or numeric index (default: from voice_config.toml)",
    )
    parser.add_argument(
        "--threshold",
        type=float,
        default=None,
        help="Wake word detection threshold (default: from voice_config.toml)",
    )
    parser.add_argument(
        "--model",
        type=str,
        default=None,
        help="Wake word model name (default: from voice_config.toml, e.g. 'hey_jarvis')",
    )
    parser.add_argument(
        "--hostapi",
        type=str,
        default="WASAPI",
        help="Preferred audio host API (default: WASAPI)",
    )
    args = parser.parse_args()

    config = load_voice_config(create_if_missing=False)
    device_spec = args.device if args.device is not None else config.input_device
    threshold = args.threshold if args.threshold is not None else config.wake_threshold
    model_name = args.model if args.model is not None else config.wake_word_model

    # 1. Resolve input device
    try:
        device_info = resolve_audio_device(
            device_spec,
            kind="input",
            preferred_hostapi=args.hostapi,
        )
    except Exception as exc:
        print(f"Error resolving audio input device: {exc}", file=sys.stderr)
        sys.exit(1)

    dev_idx = device_info["index"]
    dev_name = device_info["name"]
    host_api = device_info.get("hostapi_name", "Unknown")
    native_rate = int(device_info.get("default_samplerate", 16000))

    # 2. Negotiate input sample rate and channels
    sample_rate, channels = negotiate_input_sample_rate(
        device_idx=dev_idx,
        default_samplerate=native_rate,
        preferred_channels=1,
    )
    blocksize = int(sample_rate * 0.08)

    print("=" * 65)
    print(" Clea Wake Word Diagnostic Probe")
    print("=" * 65)
    print(f"Device:       #{dev_idx} '{dev_name}'")
    print(f"Host API:     {host_api}")
    print(f"Native Rate:  {native_rate} Hz (recording at {sample_rate} Hz, {channels} ch)")
    print("Target Rate:  16000 Hz, 1 ch mono (polyphase resampled)")
    print(f"Model:        '{model_name}' (threshold: {threshold:.2f})")
    print(f"Block Size:   {blocksize} frames -> 1280 samples (80 ms)")
    print("=" * 65)
    print("Status: Initializing openWakeWord detector...")

    try:
        detector = WakeWordDetector(
            model_name=model_name,
            threshold=threshold,
        )
    except Exception as exc:
        print(f"Failed to load wake word detector: {exc}", file=sys.stderr)
        sys.exit(1)

    print("Detector ready. Listening... (Press Ctrl+C to stop)", flush=True)
    print("-" * 65, flush=True)

    # 1 second trailing window = 1.0s / 0.08s = 12.5 chunks
    score_history: collections.deque[tuple[float, float]] = collections.deque(maxlen=15)
    last_print_time = 0.0
    chunk_buffer = AudioChunkBuffer(chunk_size=1280)

    def audio_callback(indata: np.ndarray, frames: int, time_info: Any, status: Any) -> None:
        nonlocal last_print_time
        if status:
            print(f"[!] Stream status: {status}", file=sys.stderr)

        # Resample incoming hardware frames to 16 kHz mono int16
        resampled_16k = resample_to_16k_mono(indata, orig_sr=sample_rate, target_sr=16000)
        chunk_buffer.extend(resampled_16k)

        for chunk in chunk_buffer.get_chunks():
            now = time.monotonic()
            rms = compute_audio_rms(chunk)
            detected, score, key = detector.process_audio(chunk)

            score_history.append((now, score))
            # Remove entries older than 1 second
            while score_history and (now - score_history[0][0]) > 1.0:
                score_history.popleft()

            max_1s = max((s for _, s in score_history), default=0.0)

            # Print every ~200 ms (or on detection)
            if detected or (now - last_print_time) >= 0.2:
                last_print_time = now
                device_tag = f"#{dev_idx} {dev_name[:18]}"
                status_line = (
                    f"[{time.strftime('%H:%M:%S')}] {device_tag:<22} | "
                    f"RMS: {rms:6.1f} | {model_name}: {score:5.3f} | 1s Max: {max_1s:5.3f}"
                )
                print(status_line, flush=True)

            if detected:
                print(
                    f"\n>>> WAKE WORD DETECTED! model='{key}' "
                    f"score={score:.3f} >= threshold={threshold:.2f} <<<\n",
                    flush=True,
                )

    extra_settings = (
        sd.WasapiSettings(auto_convert=True)
        if ("WASAPI" in host_api.upper() and hasattr(sd, "WasapiSettings"))
        else None
    )

    try:
        with sd.InputStream(
            device=dev_idx,
            samplerate=sample_rate,
            channels=channels,
            dtype="int16",
            blocksize=blocksize,
            extra_settings=extra_settings,
            callback=audio_callback,
        ):
            while True:
                time.sleep(0.1)
    except KeyboardInterrupt:
        print("\nProbe stopped.")
    except Exception as exc:
        print(f"\n[FAIL] Audio stream error: {exc}", file=sys.stderr)
        sys.exit(1)


if __name__ == "__main__":
    main()

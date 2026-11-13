#!/usr/bin/env python3
"""Standalone Gemini Live API VAD probe for empirical testing of Hypothesis D.

Opens a gemini-3.8-live session and streams 16 kHz 16-bit mono PCM audio.
Compares:
  Mode 1 (Default): Server-side automatic Voice Activity Detection (default settings).
  Mode 2 (Manual):  Client-driven VAD with automatic activity detection disabled
                    and explicit activity_start / activity_end signals.

Logs every server message TYPE with exact timestamps.
Never saves audio, screenshots, or logs to disk. No secrets in output.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import time
import urllib.request
from typing import Any

from dotenv import load_dotenv
from google import genai
from google.genai import types

load_dotenv(".env")

SAMPLE_SPEECH_URL = (
    "https://storage.googleapis.com/generativeai-downloads/data/hello_are_you_there.pcm"
)


def fetch_test_speech_pcm() -> bytes:
    """Fetch official Google test PCM speech in memory (never written to disk)."""
    req = urllib.request.Request(
        SAMPLE_SPEECH_URL,
        headers={"User-Agent": "Clea-Live-Probe/1.0"},
    )
    with urllib.request.urlopen(req, timeout=10.0) as resp:
        return resp.read()


def record_mic_audio(device_idx: int | None, duration_seconds: float = 4.0) -> bytes:
    """Record a brief audio clip from physical microphone in memory."""
    import sounddevice as sd

    rate = 16000
    frames = int(duration_seconds * rate)
    print(
        f"Recording {duration_seconds:.1f}s from mic (device={device_idx})... Speak now!",
        flush=True,
    )

    extra = None
    if device_idx is not None and hasattr(sd, "WasapiSettings"):
        try:
            dev = sd.query_devices(device_idx)
            hostapi = sd.query_hostapis(dev.get("hostapi", -1))
            if "WASAPI" in hostapi.get("name", "").upper():
                extra = sd.WasapiSettings(auto_convert=True)
        except Exception:
            pass

    data = sd.rec(
        frames,
        samplerate=rate,
        channels=1,
        dtype="int16",
        device=device_idx,
        extra_settings=extra,
    )
    sd.wait()
    return data.tobytes()


def summarize_message(msg: types.LiveServerMessage) -> list[str]:
    types_seen: list[str] = []
    if msg.server_content is not None:
        sc = msg.server_content
        types_seen.append("server_content")
        if sc.interrupted:
            types_seen.append("interrupted")
        if sc.model_turn is not None and sc.model_turn.parts:
            types_seen.append("model_turn")
            for part in sc.model_turn.parts:
                if part.inline_data is not None and part.inline_data.data:
                    types_seen.append(f"audio({len(part.inline_data.data)}b)")
                if part.text:
                    types_seen.append(f"text('{part.text.strip()[:35]}')")
        if sc.input_transcription is not None and sc.input_transcription.text:
            types_seen.append(f"input_transcription('{sc.input_transcription.text.strip()}')")
        if sc.output_transcription is not None and sc.output_transcription.text:
            types_seen.append(
                f"output_transcription('{sc.output_transcription.text.strip()[:35]}')"
            )
        if sc.turn_complete:
            types_seen.append("turn_complete")
    if msg.tool_call is not None:
        types_seen.append("tool_call")
    if msg.session_resumption_update is not None:
        types_seen.append("session_resumption_update")
    if msg.usage_metadata is not None:
        types_seen.append("usage_metadata")
    if msg.go_away is not None:
        types_seen.append("go_away")
    return types_seen


async def run_probe(
    *,
    mode: str,
    audio_bytes: bytes,
    model: str = "gemini-3.8-live",
    silence_after_speech_seconds: float = 3.0,
) -> dict[str, Any]:
    api_key = os.environ.get("GEMINI_LIVE_API_KEY") or os.environ.get("GEMINI_API_KEY", "")
    if not api_key:
        print(
            "Error: Neither GEMINI_LIVE_API_KEY nor GEMINI_API_KEY is configured.", file=sys.stderr
        )
        sys.exit(1)

    client = genai.Client(api_key=api_key, http_options={"api_version": "v1beta"})

    if mode == "default":
        # Mode 1: Default server-side automatic VAD
        config = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
        )
    elif mode == "manual":
        # Mode 2: Client-side VAD with automatic activity detection disabled
        config = types.LiveConnectConfig(
            response_modalities=[types.Modality.AUDIO],
            input_audio_transcription=types.AudioTranscriptionConfig(),
            output_audio_transcription=types.AudioTranscriptionConfig(),
            realtime_input_config=types.RealtimeInputConfig(
                automatic_activity_detection=types.AutomaticActivityDetection(disabled=True)
            ),
        )
    else:
        raise ValueError(f"Unknown mode: {mode}")

    print(f"\n{'=' * 70}")
    print(f" PROBE RUN: mode={mode.upper()} | model={model}")
    print(f"{'=' * 70}", flush=True)

    t0 = time.monotonic()
    events_log: list[tuple[float, list[str]]] = []
    first_audio_time: float | None = None
    input_transcript: str | None = None
    output_transcript_parts: list[str] = []
    turn_complete_received = False

    async with client.aio.live.connect(model=model, config=config) as session:

        async def audio_sender():
            CHUNK_BYTES = 2560  # 1280 samples * 2 bytes = 80 ms
            if mode == "manual":
                print(f"[{time.monotonic() - t0:6.2f}s] SEND: activity_start", flush=True)
                await session.send_realtime_input(activity_start=types.ActivityStart())

            # Stream speech audio in 80 ms chunks
            for offset in range(0, len(audio_bytes), CHUNK_BYTES):
                chunk = audio_bytes[offset : offset + CHUNK_BYTES]
                await session.send_realtime_input(
                    audio=types.Blob(data=chunk, mime_type="audio/pcm;rate=16000")
                )
                await asyncio.sleep(0.08)

            print(
                f"[{time.monotonic() - t0:6.2f}s] Finished speech ({len(audio_bytes)} bytes).",
                flush=True,
            )

            if mode == "manual":
                print(f"[{time.monotonic() - t0:6.2f}s] SEND: activity_end", flush=True)
                await session.send_realtime_input(activity_end=types.ActivityEnd())
            else:
                # In default mode, continue streaming silence to let server VAD detect silence
                silence = b"\x00" * CHUNK_BYTES
                silence_chunks = int(silence_after_speech_seconds / 0.08)
                for _ in range(silence_chunks):
                    await session.send_realtime_input(
                        audio=types.Blob(data=silence, mime_type="audio/pcm;rate=16000")
                    )
                    await asyncio.sleep(0.08)

        async def response_receiver():
            nonlocal first_audio_time, input_transcript, turn_complete_received
            async for msg in session.receive():
                now = time.monotonic() - t0
                types_seen = summarize_message(msg)
                events_log.append((now, types_seen))
                print(f"[{now:6.2f}s] RECV: {', '.join(types_seen)}", flush=True)

                if msg.server_content is not None:
                    sc = msg.server_content
                    if sc.input_transcription is not None and sc.input_transcription.text:
                        input_transcript = sc.input_transcription.text
                    if sc.output_transcription is not None and sc.output_transcription.text:
                        output_transcript_parts.append(sc.output_transcription.text)
                    if sc.model_turn is not None and first_audio_time is None:
                        for p in sc.model_turn.parts or []:
                            if p.inline_data is not None and p.inline_data.data:
                                first_audio_time = now
                                break
                    if sc.turn_complete:
                        turn_complete_received = True
                        break

        sender_task = asyncio.create_task(audio_sender())
        receiver_task = asyncio.create_task(response_receiver())

        # Wait up to 15 seconds for turn to complete
        try:
            await asyncio.wait_for(asyncio.gather(sender_task, receiver_task), timeout=15.0)
        except TimeoutError:
            print(
                f"[{time.monotonic() - t0:6.2f}s] Session timed out waiting for turn completion.",
                flush=True,
            )
            sender_task.cancel()
            receiver_task.cancel()

    out_text = "".join(output_transcript_parts).strip()
    return {
        "mode": mode,
        "first_audio_latency": first_audio_time,
        "input_transcript": input_transcript,
        "output_transcript": out_text,
        "turn_complete": turn_complete_received,
        "events_count": len(events_log),
    }


def main() -> None:
    parser = argparse.ArgumentParser(description="Test Gemini Live VAD behavior.")
    parser.add_argument(
        "--mode",
        choices=["default", "manual", "both"],
        default="both",
        help="Test mode: default, manual, or both side-by-side (default: both)",
    )
    parser.add_argument(
        "--source",
        choices=["sample", "mic"],
        default="sample",
        help="Audio source: Google speech PCM in memory or mic (default: sample)",
    )
    parser.add_argument(
        "--device",
        type=str,
        default=None,
        help="Microphone device name substring (used if --source mic)",
    )
    parser.add_argument(
        "--mic-duration",
        type=float,
        default=4.0,
        help="Microphone recording duration in seconds (default: 4.0)",
    )
    args = parser.parse_args()

    # Obtain audio
    if args.source == "mic":
        from clea.surfaces.voice.audio import resolve_audio_device

        dev = resolve_audio_device(args.device, kind="input")
        audio = record_mic_audio(dev["index"], duration_seconds=args.mic_duration)
    else:
        print("Fetching official Google sample speech PCM in memory...", flush=True)
        audio = fetch_test_speech_pcm()

    modes = ["default", "manual"] if args.mode == "both" else [args.mode]
    results: list[dict[str, Any]] = []

    for m in modes:
        res = asyncio.run(run_probe(mode=m, audio_bytes=audio))
        results.append(res)
        time.sleep(1.0)

    print("\n" + "=" * 70)
    print(" SIDE-BY-SIDE VAD RESULTS COMPARISON")
    print("=" * 70)
    for r in results:
        m_name = r["mode"].upper()
        lat = f"{r['first_audio_latency']:.2f}s" if r["first_audio_latency"] else "None (No audio)"
        print(f"Mode:             {m_name}")
        print(f"First Audio TTFT: {lat}")
        print(f"User Input:       {r['input_transcript']}")
        print(f"Model Output:     {r['output_transcript']}")
        print(f"Turn Complete:    {r['turn_complete']}")
        print("-" * 70)


if __name__ == "__main__":
    main()

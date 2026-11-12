"""Tests for the VoiceBackend interface, voice tools, memory stubs, and boundary rules."""

from __future__ import annotations

import ast
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from clea.surfaces.voice.backends import (
    INPUT_AUDIO_SAMPLE_RATE_HZ,
    OUTPUT_AUDIO_SAMPLE_RATE_HZ,
    AudioOutputEvent,
    BackendErrorEvent,
    ContextCompressionConfig,
    GoAwayEvent,
    InterruptedEvent,
    SessionResumptionConfig,
    SessionResumptionUpdateEvent,
    ToolCallCancellationEvent,
    ToolCallEvent,
    TranscriptOutputEvent,
    TurnCompleteEvent,
    UsageUpdateEvent,
    VoiceBackend,
    VoiceBackendEvent,
    VoiceSessionConfig,
    VoiceToolResponse,
    default_voice_tools,
)
from clea.surfaces.voice.memory_hooks import (
    build_voice_system_instruction,
    consolidate_voice_call,
    load_voice_memory_context,
    store_voice_transcript,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
VOICE_SURFACE_DIR = REPO_ROOT / "src" / "clea" / "surfaces" / "voice"


def test_voice_backend_is_abstract() -> None:
    with pytest.raises(TypeError):
        VoiceBackend()  # type: ignore[abstract]


def test_default_voice_tools_cover_required_four_non_blocking_tools() -> None:
    tools = default_voice_tools()
    by_name = {tool.name: tool for tool in tools}
    assert set(by_name) == {"delegate_task", "look_at_screen", "watch_screen", "end_call"}
    for tool in tools:
        assert tool.behavior == "non_blocking"

    assert by_name["delegate_task"].parameters_schema["required"] == ["description"]

    look_props = by_name["look_at_screen"].parameters_schema["properties"]
    assert look_props["target"]["enum"] == ["active_window", "full_screen"]
    assert "monitor" in look_props
    assert by_name["look_at_screen"].parameters_schema["required"] == ["target", "reason"]

    watch_props = by_name["watch_screen"].parameters_schema["properties"]
    assert "seconds" in watch_props
    assert by_name["watch_screen"].parameters_schema["required"] == ["seconds", "reason"]

    assert by_name["end_call"].parameters_schema["required"] == ["reason"]


def test_voice_session_config_defaults_and_language_code_omitted() -> None:
    instruction = build_voice_system_instruction()
    assert "English" in instruction
    assert "Hindi" in instruction
    assert "Hinglish" in instruction

    cfg = VoiceSessionConfig(system_instruction=instruction)
    assert cfg.language_code is None
    assert cfg.input_sample_rate_hz == INPUT_AUDIO_SAMPLE_RATE_HZ == 16_000
    assert cfg.output_sample_rate_hz == OUTPUT_AUDIO_SAMPLE_RATE_HZ == 24_000
    assert cfg.media_resolution == "medium"
    assert cfg.enable_web_search is True
    assert cfg.session_resumption == SessionResumptionConfig(enabled=True, handle=None)
    assert cfg.context_compression == ContextCompressionConfig(enabled=True)
    assert cfg.reconnect_limit == 3


async def test_concrete_voice_backend_lifecycle_and_events() -> None:
    class FakeVoiceBackend(VoiceBackend):
        backend_name = "fake-live"

        def __init__(self) -> None:
            self.connected_config: VoiceSessionConfig | None = None
            self.closed = False
            self.audio_chunks: list[bytes] = []
            self.video_frames: list[tuple[bytes, str]] = []
            self.texts: list[tuple[str, bool]] = []
            self.tool_responses: list[VoiceToolResponse] = []

        @property
        def model_name(self) -> str:
            return "fake-live-model"

        async def connect(self, config: VoiceSessionConfig) -> None:
            self.connected_config = config

        async def close(self) -> None:
            self.closed = True

        async def send_audio(self, pcm_bytes: bytes) -> None:
            self.audio_chunks.append(pcm_bytes)

        async def send_video_frame(
            self,
            image_bytes: bytes,
            *,
            mime_type: str = "image/jpeg",
        ) -> None:
            self.video_frames.append((image_bytes, mime_type))

        async def send_text(self, text: str, *, end_of_turn: bool = True) -> None:
            self.texts.append((text, end_of_turn))

        async def send_tool_responses(self, responses: list[VoiceToolResponse]) -> None:
            self.tool_responses.extend(responses)

        async def events(self) -> AsyncIterator[VoiceBackendEvent]:
            yield TranscriptOutputEvent(role="user", text="Hi Clea", final=True)
            yield ToolCallEvent(
                call_id="tc-1",
                name="look_at_screen",
                arguments={"target": "active_window", "reason": "check error"},
            )
            yield ToolCallCancellationEvent(call_ids=["tc-1"])
            yield InterruptedEvent()
            yield AudioOutputEvent(pcm_bytes=b"\x00\x01" * 160, sample_rate_hz=24_000)
            yield TranscriptOutputEvent(role="assistant", text="Haan batao", final=True)
            yield SessionResumptionUpdateEvent(handle="resume-tok-1", resumable=True)
            yield GoAwayEvent(time_left_seconds=12.5)
            yield UsageUpdateEvent(input_tokens=50, output_tokens=20, total_tokens=70)
            yield TurnCompleteEvent()
            yield BackendErrorEvent(message="transient hiccup", recoverable=True)

    backend = FakeVoiceBackend()
    cfg = VoiceSessionConfig(system_instruction=build_voice_system_instruction())
    await backend.connect(cfg)
    assert backend.connected_config == cfg

    await backend.send_audio(b"\x00\x00" * 320)
    await backend.send_video_frame(b"jpeg-bytes", mime_type="image/jpeg")
    await backend.send_text("hello", end_of_turn=True)
    await backend.send_tool_responses(
        [
            VoiceToolResponse(
                call_id="tc-1",
                name="delegate_task",
                response={"result": "done"},
                scheduling="when_idle",
            )
        ]
    )

    received = [ev async for ev in backend.events()]
    assert [ev.type for ev in received] == [
        "transcript",
        "tool_call",
        "tool_call_cancellation",
        "interrupted",
        "audio_output",
        "transcript",
        "session_resumption_update",
        "go_away",
        "usage",
        "turn_complete",
        "error",
    ]
    assert isinstance(received[4], AudioOutputEvent)
    assert received[4].sample_rate_hz == 24_000
    assert backend.tool_responses[0].scheduling == "when_idle"
    await backend.end_audio_stream()
    await backend.close()
    assert backend.closed is True


def test_voice_backend_model_validation_rejects_invalid_bounds() -> None:
    from pydantic import ValidationError

    with pytest.raises(ValidationError):
        ContextCompressionConfig(trigger_tokens=1000, target_tokens=2000)
    with pytest.raises(ValidationError):
        AudioOutputEvent(pcm_bytes=b"\x00", sample_rate_hz=0)
    with pytest.raises(ValidationError):
        GoAwayEvent(time_left_seconds=-1.0)
    with pytest.raises(ValidationError):
        UsageUpdateEvent(input_tokens=-1)


async def test_deferred_memory_hooks_are_explicitly_stubbed() -> None:
    with pytest.raises(NotImplementedError, match="deferred"):
        await load_voice_memory_context()
    with pytest.raises(NotImplementedError, match="deferred"):
        await store_voice_transcript(call_id="c1", entries=[])
    with pytest.raises(NotImplementedError, match="deferred"):
        await consolidate_voice_call(call_id="c1")


def test_voice_surface_respects_architecture_and_vendor_isolation_boundaries() -> None:
    """No core internals or Gemini SDK imports outside voice/backends/gemini_live.py."""
    forbidden_prefixes = (
        "google",
        "clea.core.agent",
        "clea.core.sessions",
        "clea.memory",
        "clea.tools",
        "clea.providers",
    )
    for py_file in VOICE_SURFACE_DIR.rglob("*.py"):
        if py_file.name == "gemini_live.py":
            continue
        tree = ast.parse(py_file.read_text(encoding="utf-8"), filename=str(py_file))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                for alias in node.names:
                    assert not alias.name.startswith(forbidden_prefixes), (
                        f"Forbidden import {alias.name!r} in {py_file}"
                    )
            elif isinstance(node, ast.ImportFrom) and node.module:
                assert not node.module.startswith(forbidden_prefixes), (
                    f"Forbidden import from {node.module!r} in {py_file}"
                )

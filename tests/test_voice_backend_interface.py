"""Tests for the VoiceBackend interface, voice tools, memory stubs, and boundary rules."""

from __future__ import annotations

import ast
import logging
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
    VoiceToolMediaPart,
    VoiceToolResponse,
    default_voice_tools,
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

REPO_ROOT = Path(__file__).resolve().parents[1]
VOICE_SURFACE_DIR = REPO_ROOT / "src" / "clea" / "surfaces" / "voice"


@pytest.fixture(autouse=True)
def _sandbox_clea_environment(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("CLEA_DATA_DIR", str(tmp_path))


def test_voice_backend_is_abstract() -> None:
    with pytest.raises(TypeError):
        VoiceBackend()  # type: ignore[abstract]


def test_default_voice_tools_cover_required_tools() -> None:
    tools = default_voice_tools()
    by_name = {tool.name: tool for tool in tools}
    assert set(by_name) == {
        "delegate_task",
        "look_at_screen",
        "watch_screen",
        "end_call",
        "list_conversations",
        "search_conversations",
        "read_conversation",
        "search_memory",
        "post_to_active_chat",
        "navigate_ui",
        "create_chat",
        "send_chat_message",
        "rename_chat",
        "delete_chat",
        "save_memory_fact",
        "delete_memory_fact",
        "inspect_activity",
        "cancel_task",
        "open_command_palette",
        "open_or_focus_app",
        "control_media_volume",
        "read_clipboard",
        "write_clipboard",
        "inspect_git_workspace",
        "select_clipboard_item",
        "open_url",
        "control_system",
    }
    for tool in tools:
        assert tool.behavior == "non_blocking"

    assert by_name["delegate_task"].parameters_schema["required"] == ["description"]

    look_props = by_name["look_at_screen"].parameters_schema["properties"]
    assert look_props["target"]["enum"] == ["active_window", "full_screen"]
    assert "monitor" in look_props
    assert by_name["look_at_screen"].parameters_schema["required"] == ["target", "reason"]

    watch_props = by_name["watch_screen"].parameters_schema["properties"]
    assert "seconds" in watch_props
    assert watch_props["target"]["enum"] == ["active_window", "full_screen"]
    assert by_name["watch_screen"].parameters_schema["required"] == ["seconds", "reason"]

    assert by_name["end_call"].parameters_schema["required"] == ["reason"]
    assert by_name["list_conversations"].parameters_schema.get("required", []) == []
    assert by_name["search_conversations"].parameters_schema["required"] == ["query"]
    assert by_name["read_conversation"].parameters_schema["required"] == ["conversation_id"]
    assert by_name["search_memory"].parameters_schema["required"] == ["query"]
    assert by_name["post_to_active_chat"].parameters_schema["required"] == ["content"]
    assert by_name["navigate_ui"].parameters_schema["properties"]["view"]["enum"] == [
        "chat",
        "voice",
        "activity",
        "memory",
        "settings",
    ]
    assert by_name["navigate_ui"].parameters_schema["required"] == ["view"]
    assert by_name["create_chat"].parameters_schema["required"] == ["title"]
    assert by_name["send_chat_message"].parameters_schema["required"] == ["prompt"]
    assert by_name["rename_chat"].parameters_schema["required"] == ["conversation_id", "new_title"]
    assert by_name["delete_chat"].parameters_schema["required"] == ["conversation_id"]
    assert by_name["save_memory_fact"].parameters_schema["required"] == ["fact"]
    assert by_name["delete_memory_fact"].parameters_schema["required"] == ["query_or_id"]
    assert by_name["inspect_activity"].parameters_schema["required"] == []
    assert by_name["cancel_task"].parameters_schema["required"] == ["task_id"]
    assert by_name["open_command_palette"].parameters_schema["required"] == []
    assert by_name["open_or_focus_app"].parameters_schema["required"] == ["app_name"]
    assert by_name["control_media_volume"].parameters_schema["required"] == ["action"]
    assert by_name["control_media_volume"].parameters_schema["properties"]["action"]["enum"] == [
        "mute",
        "unmute",
        "toggle_mute",
        "volume_up",
        "volume_down",
        "set_volume",
        "play_pause",
        "next_track",
        "previous_track",
    ]
    assert by_name["read_clipboard"].parameters_schema.get("required", []) == []
    assert "include_history" in by_name["read_clipboard"].parameters_schema["properties"]
    assert "max_items" in by_name["read_clipboard"].parameters_schema["properties"]
    assert by_name["write_clipboard"].parameters_schema["required"] == ["text"]
    assert by_name["inspect_git_workspace"].parameters_schema.get("required", []) == []

    assert by_name["select_clipboard_item"].parameters_schema["required"] == ["index"]
    assert (
        by_name["select_clipboard_item"].parameters_schema["properties"]["index"]["type"]
        == "integer"
    )
    assert (
        by_name["select_clipboard_item"].parameters_schema["properties"]["index"]["minimum"]
        == 0
    )

    assert by_name["open_url"].parameters_schema["required"] == ["url"]
    assert by_name["open_url"].parameters_schema["properties"]["url"]["type"] == "string"
    assert "browser" in by_name["open_url"].parameters_schema["properties"]

    assert by_name["control_system"].parameters_schema["required"] == ["action"]
    cs_actions = by_name["control_system"].parameters_schema["properties"]["action"]["enum"]
    for expected_action in (
        "get_brightness",
        "set_brightness",
        "step_brightness",
        "brightness_up",
        "brightness_down",
        "minimize_window",
        "maximize_window",
        "restore_window",
        "close_window",
        "lock_screen",
        "open_settings",
    ):
        assert expected_action in cs_actions
    assert (
        by_name["control_system"].parameters_schema["properties"]["level"]["minimum"] == 0
    )
    assert (
        by_name["control_system"].parameters_schema["properties"]["level"]["maximum"] == 100
    )


def test_new_windows_os_voice_tools_registration_and_schemas() -> None:
    tools = {t.name: t for t in default_voice_tools()}

    # 1. select_clipboard_item
    assert "select_clipboard_item" in tools
    sel_tool = tools["select_clipboard_item"]
    assert sel_tool.behavior == "non_blocking"
    assert "clipboard history" in sel_tool.description
    assert sel_tool.parameters_schema["type"] == "object"
    assert sel_tool.parameters_schema["required"] == ["index"]
    assert sel_tool.parameters_schema["properties"]["index"]["type"] == "integer"
    assert sel_tool.parameters_schema["properties"]["index"]["minimum"] == 0

    # 2. open_url
    assert "open_url" in tools
    url_tool = tools["open_url"]
    assert url_tool.behavior == "non_blocking"
    assert "browser" in url_tool.description
    assert url_tool.parameters_schema["type"] == "object"
    assert url_tool.parameters_schema["required"] == ["url"]
    assert url_tool.parameters_schema["properties"]["url"]["type"] == "string"
    assert url_tool.parameters_schema["properties"]["browser"]["type"] == "string"

    # 3. control_system
    assert "control_system" in tools
    sys_tool = tools["control_system"]
    assert sys_tool.behavior == "non_blocking"
    assert "brightness" in sys_tool.description
    assert sys_tool.parameters_schema["type"] == "object"
    assert sys_tool.parameters_schema["required"] == ["action"]
    sys_props = sys_tool.parameters_schema["properties"]
    assert "get_brightness" in sys_props["action"]["enum"]
    assert "set_brightness" in sys_props["action"]["enum"]
    assert "lock_screen" in sys_props["action"]["enum"]
    assert "open_settings" in sys_props["action"]["enum"]
    assert sys_props["level"]["minimum"] == 0
    assert sys_props["level"]["maximum"] == 100
    assert "page" in sys_props
    assert "uri" in sys_props
    assert "window_action" in sys_props
    assert "window_title" in sys_props


def test_voice_session_config_defaults_and_language_code_omitted() -> None:
    instruction = build_voice_system_instruction()
    assert "English" in instruction
    assert "Hindi" in instruction
    assert "Hinglish" in instruction
    assert "look_at_screen" in instruction
    assert "active_app_name" in instruction
    assert "NEVER answer questions about what is currently on the screen" in instruction

    cfg = VoiceSessionConfig(system_instruction=instruction)
    assert cfg.language_code is None
    assert cfg.input_sample_rate_hz == INPUT_AUDIO_SAMPLE_RATE_HZ == 16_000
    assert cfg.output_sample_rate_hz == OUTPUT_AUDIO_SAMPLE_RATE_HZ == 24_000
    assert cfg.media_resolution == "high"
    assert cfg.enable_web_search is False
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
    with pytest.raises(ValidationError):
        VoiceToolMediaPart(data=b"", mime_type="image/png")
    with pytest.raises(ValidationError):
        VoiceToolMediaPart(data=b"\x89PNG", mime_type="   ")
    with pytest.raises(ValidationError):
        VoiceToolMediaPart(data=b"\x89PNG", mime_type="image/png", extra_field=True)  # type: ignore[call-arg]

    part = VoiceToolMediaPart(data=b"\x89PNG\r\n", mime_type=" image/png ")
    assert part.mime_type == "image/png"
    resp = VoiceToolResponse(
        call_id="c-media",
        name="look_at_screen",
        response={"status": "success"},
        media_parts=[part],
    )
    assert len(resp.media_parts) == 1
    assert resp.media_parts[0].data == b"\x89PNG\r\n"


async def test_memory_hooks_skip_empty_and_use_request_fn() -> None:
    calls: list[str] = []

    async def _fake_request(req):  # type: ignore[no-untyped-def]
        calls.append(req.method)
        if req.method == "memory.get":
            return {"long_term": "User lives in Pune."}
        if req.method == "memory.episodes":
            return {"episodes": [{"date": "2026-11-14", "summary": "Tested voice"}]}
        if req.method == "voice.history":
            return {
                "session_id": "voice_companion",
                "messages": [
                    {
                        "role": "user",
                        "text": "Recent ambient voice note",
                        "timestamp": "2026-11-14T10:00:00Z",
                        "call_id": "call-prev",
                    }
                ],
            }
        return {"ok": True}

    ctx = await load_voice_memory_context(request_fn=_fake_request)
    assert "User lives in Pune." in ctx
    assert "Tested voice" in ctx
    assert "Recent ambient voice note" in ctx
    assert calls == ["memory.get", "memory.episodes", "voice.history"]

    # Empty calls are skipped without making any daemon RPCs
    calls.clear()
    assert await store_voice_transcript(call_id="c1", entries=[], request_fn=_fake_request) is False
    assert await consolidate_voice_call(call_id="c1", entries=[], request_fn=_fake_request) is False
    assert calls == []


async def test_resolve_voice_system_instruction_falls_back_on_unimplemented_hook(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="clea.surfaces.voice.memory_hooks")

    async def _unimplemented_loader() -> str:
        raise NotImplementedError("custom stub")

    # Safe loader and resolver gracefully return None/fallback
    assert await safe_load_voice_memory_context(loader=_unimplemented_loader) is None

    instruction = await resolve_voice_system_instruction(memory_loader=_unimplemented_loader)
    assert instruction == BASE_VOICE_SYSTEM_INSTRUCTION
    assert "## Memory Context" not in instruction

    # Also verifies offline daemon fallback when no token/lock exists
    offline_instruction = await resolve_voice_system_instruction(data_dir=tmp_path)
    assert offline_instruction == BASE_VOICE_SYSTEM_INSTRUCTION

    warning_records = [
        rec
        for rec in caplog.records
        if rec.name == "clea.surfaces.voice.memory_hooks" and rec.levelno == logging.WARNING
    ]
    assert len(warning_records) == 3
    assert "Voice memory context hook is not implemented" in warning_records[0].getMessage()
    assert "Voice memory context hook is not implemented" in warning_records[1].getMessage()
    assert "Failed loading voice memory context" in warning_records[2].getMessage()


async def test_resolve_voice_system_instruction_falls_back_on_arbitrary_error(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="clea.surfaces.voice.memory_hooks")

    async def _failing_loader() -> str:
        raise RuntimeError("database unreachable")

    instruction = await resolve_voice_system_instruction(memory_loader=_failing_loader)
    assert instruction == BASE_VOICE_SYSTEM_INSTRUCTION
    assert "## Memory Context" not in instruction

    warning_records = [
        rec
        for rec in caplog.records
        if rec.name == "clea.surfaces.voice.memory_hooks" and rec.levelno == logging.WARNING
    ]
    assert len(warning_records) == 1
    assert "Failed loading voice memory context" in warning_records[0].getMessage()


async def test_resolve_voice_system_instruction_includes_context_when_provided(
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="clea.surfaces.voice.memory_hooks")

    async def _success_loader() -> str:
        return "User prefers short answers in Hindi."

    instruction = await resolve_voice_system_instruction(memory_loader=_success_loader)
    assert "## Memory Context" in instruction
    assert "User prefers short answers in Hindi." in instruction

    warning_records = [
        rec
        for rec in caplog.records
        if rec.name == "clea.surfaces.voice.memory_hooks" and rec.levelno == logging.WARNING
    ]
    assert len(warning_records) == 0


async def test_safe_transcript_and_consolidation_do_not_crash_active_calls(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="clea.surfaces.voice.memory_hooks")

    async def _unimplemented_store(*, call_id: str, entries: object) -> None:
        raise NotImplementedError("store stub")

    async def _unimplemented_consolidate(*, call_id: str) -> None:
        raise NotImplementedError("consolidate stub")

    # Custom NotImplementedError handlers must not raise
    await safe_store_voice_transcript(
        call_id="call-42",
        entries=[{"role": "user", "text": "hi"}],
        handler=_unimplemented_store,
    )
    await safe_consolidate_voice_call(call_id="call-42", handler=_unimplemented_consolidate)

    # Offline daemon calls (no token/lock in tmp_path) must also fail safely without raising
    await safe_store_voice_transcript(
        call_id="call-43",
        entries=[{"role": "user", "text": "hi"}],
        data_dir=tmp_path,
    )
    await safe_consolidate_voice_call(
        call_id="call-43",
        entries=[{"role": "user", "text": "hi"}],
        data_dir=tmp_path,
    )

    warning_records = [
        rec
        for rec in caplog.records
        if rec.name == "clea.surfaces.voice.memory_hooks" and rec.levelno == logging.WARNING
    ]
    assert len(warning_records) == 4
    assert "Voice transcript hook not implemented" in warning_records[0].getMessage()
    assert "Voice consolidation hook not implemented" in warning_records[1].getMessage()
    assert "Failed storing voice transcript" in warning_records[2].getMessage()
    assert "Failed consolidating voice call" in warning_records[3].getMessage()


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

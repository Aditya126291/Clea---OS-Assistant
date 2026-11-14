"""Tests for voice intent-to-tool routing, negative tool guardrails, and focus state injection."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from websockets.asyncio.client import connect

from clea.core import auth
from clea.core.daemon import CleaDaemon, build_daemon
from clea.core.protocol import (
    BaseRequest,
    ChatFocusParams,
    ChatFocusRequest,
    ResponseFrame,
    parse_frame,
)
from clea.memory.store import VOICE_COMPANION_SESSION_ID, MemoryStore
from clea.providers.gemini import GeminiProvider
from clea.surfaces.voice.audio import AudioManager
from clea.surfaces.voice.backends.base import (
    VoiceBackend,
    VoiceBackendEvent,
    VoiceSessionConfig,
    VoiceToolResponse,
    default_voice_tools,
)
from clea.surfaces.voice.backends.gemini_live import GeminiLiveBackend
from clea.surfaces.voice.config import VoiceConfig
from clea.surfaces.voice.memory_hooks import (
    BASE_VOICE_SYSTEM_INSTRUCTION,
    build_voice_system_instruction,
    extract_focused_chat_from_context,
    format_focus_state_context,
    resolve_voice_system_instruction,
)
from clea.surfaces.voice.runner import VoiceRunner
from clea.surfaces.voice.wake_word import WakeWordDetector


class _FakeLiveBackend(VoiceBackend):
    backend_name = "fake-live"

    def __init__(self) -> None:
        self.connected_config: VoiceSessionConfig | None = None
        self.event_queue: asyncio.Queue[VoiceBackendEvent | None] = asyncio.Queue()

    @property
    def model_name(self) -> str:
        return "fake-live-model"

    async def connect(self, config: VoiceSessionConfig) -> None:
        self.connected_config = config

    async def close(self) -> None:
        await self.event_queue.put(None)

    async def send_audio(self, pcm_bytes: bytes) -> None:
        pass

    async def send_video_frame(self, image_bytes: bytes, *, mime_type: str = "image/jpeg") -> None:
        pass

    async def send_text(self, text: str, *, end_of_turn: bool = True) -> None:
        pass

    async def send_tool_responses(self, responses: list[VoiceToolResponse]) -> None:
        pass

    async def events(self) -> AsyncIterator[VoiceBackendEvent]:
        while True:
            evt = await self.event_queue.get()
            if evt is None:
                break
            yield evt


class _FakeProvider(GeminiProvider):
    def __init__(self) -> None:
        pass

    async def run_chat(self, *args: Any, **kwargs: Any) -> Any:
        raise NotImplementedError


def _make_fake_audio() -> AudioManager:
    audio = AudioManager()
    audio.start = MagicMock()  # type: ignore[method-assign]
    audio.stop = MagicMock()  # type: ignore[method-assign]
    audio.play_audio = MagicMock()  # type: ignore[method-assign]
    audio.stop_output = MagicMock()  # type: ignore[method-assign]
    audio.set_input_callback = MagicMock()  # type: ignore[method-assign]
    return audio


def _make_fake_wake_word() -> WakeWordDetector:
    mock_model = MagicMock()
    mock_model.predict.return_value = {"hey_jarvis_v0.1": 0.0}
    return WakeWordDetector(model_instance=mock_model)


async def _wait_until(pred: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if pred():
            return
        await asyncio.sleep(0.05)
    raise TimeoutError("Predicate not satisfied within timeout.")


async def _send_rpc(ws: Any, req: BaseRequest) -> ResponseFrame:
    await ws.send(req.model_dump_json())
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        frame = parse_frame(raw)
        if isinstance(frame, ResponseFrame) and frame.id == req.id:
            return frame


# --- 1. BASE_VOICE_SYSTEM_INSTRUCTION Partitioning & Negative Rules ---


def test_base_voice_system_instruction_partitions_intents_and_blocks_screen_bias() -> None:
    prompt = BASE_VOICE_SYSTEM_INSTRUCTION

    assert "1. Visual / Active Screen Intent" in prompt
    assert "2. Memory / Past Conversation Recall Intent" in prompt
    assert "3. Clea App, Chat Output & Task Action Intent" in prompt
    assert "4. General Spoken Q&A" in prompt

    # Hard negative rules against screen-first bias for recall / focus
    assert "NEVER call `look_at_screen`" in prompt
    assert "NEVER tell the user to open or focus a chat on screen" in prompt
    assert "route directly to `post_to_active_chat`" in prompt
    assert "focused_chat_available" in prompt
    assert "Answer directly without calling any tool" in prompt

    # Must not advertise non-existent live voice tools
    for nonexistent_tool in (
        "list_conversations",
        "read_conversation",
        "search_memory",
        "create_chat",
        "navigate_ui",
    ):
        assert nonexistent_tool not in prompt


# --- 2. Tool Schema Negative Constraints & Gemini Live Propagation ---


def test_voice_tool_schemas_enforce_negative_routing_constraints() -> None:
    tools = {t.name: t for t in default_voice_tools()}

    look_desc = tools["look_at_screen"].description
    watch_desc = tools["watch_screen"].description
    search_desc = tools["search_conversations"].description
    post_desc = tools["post_to_active_chat"].description

    expected_negative = (
        "Only use when the user asks about what is currently visible on their screen. "
        "Do NOT call when the user asks about past conversations, memory, stored facts, "
        "or Clea app actions."
    )
    assert expected_negative in look_desc
    assert expected_negative in watch_desc

    assert "across ALL saved text conversations" in search_desc
    assert "Call this immediately" in search_desc
    assert "Never call `look_at_screen`" in search_desc

    assert "focused_chat_available" in post_desc
    assert "without calling `look_at_screen`" in post_desc


def test_gemini_live_config_forwards_updated_tool_descriptions() -> None:
    backend = GeminiLiveBackend(api_key="test-key", client=MagicMock())
    session_cfg = VoiceSessionConfig(
        system_instruction=BASE_VOICE_SYSTEM_INSTRUCTION,
        tools=default_voice_tools(),
    )
    live_cfg = backend._build_live_config(session_cfg)

    declarations = {}
    assert live_cfg.tools is not None
    for tool_entry in live_cfg.tools:
        for fn in tool_entry.function_declarations or []:
            assert fn.name is not None
            declarations[fn.name] = fn

    assert "Do NOT call when the user asks about past conversations" in (
        declarations["look_at_screen"].description or ""
    )
    assert "Do NOT call when the user asks about past conversations" in (
        declarations["watch_screen"].description or ""
    )
    assert "across ALL saved text conversations" in (
        declarations["search_conversations"].description or ""
    )


# --- 3. Deterministic Focus State Formatting & Extraction ---


def test_format_focus_state_context_handles_focused_and_unfocused_states() -> None:
    focused = format_focus_state_context(
        focused_chat_available=True,
        focused_chat_id="  conv-42\n ",
        focused_chat_title="  Docker   Cleanup\nPlan  ",
    )
    assert "## Call Startup Runtime State" in focused
    assert "focused_chat_available: true" in focused
    assert "focused_chat_id: conv-42" in focused
    assert "focused_chat_title: Docker Cleanup Plan" in focused

    untitled = format_focus_state_context(
        focused_chat_available=True,
        focused_chat_id="conv-99",
        focused_chat_title="   ",
    )
    assert "focused_chat_available: true" in untitled
    assert "focused_chat_title: (untitled)" in untitled

    unfocused = format_focus_state_context(focused_chat_available=False)
    assert "## Call Startup Runtime State" in unfocused
    assert "focused_chat_available: false" in unfocused
    assert "NEVER claim a chat is focused" in unfocused
    assert "NEVER ask the user to open or focus a chat" in unfocused

    # Defensive fallback: missing ID or voice_companion ID cannot claim focused=true
    missing_id = format_focus_state_context(focused_chat_available=True, focused_chat_id="  ")
    assert "focused_chat_available: false" in missing_id
    companion_id = format_focus_state_context(
        focused_chat_available=True,
        focused_chat_id=VOICE_COMPANION_SESSION_ID,
        focused_chat_title="Voice Companion",
    )
    assert "focused_chat_available: false" in companion_id


def test_extract_focused_chat_from_context_and_build_instruction() -> None:
    ctx_with_focus = (
        "## Long-Term Memory\nUser works on Clea.\n\n"
        "## Active Focused Text Chat (Read-Only Context)\n"
        "Active Conversation ID: `chat-123`\n"
        "Title: Voice Routing Spec\n"
        "Recent turns in this open text chat:\n"
        "- User: let's fix tool bias"
    )
    chat_id, chat_title = extract_focused_chat_from_context(ctx_with_focus)
    assert chat_id == "chat-123"
    assert chat_title == "Voice Routing Spec"

    instruction_true = build_voice_system_instruction(
        memory_context=ctx_with_focus,
        focused_chat_available=True,
    )
    assert "focused_chat_available: true" in instruction_true
    assert "focused_chat_id: chat-123" in instruction_true
    assert "focused_chat_title: Voice Routing Spec" in instruction_true

    instruction_false = build_voice_system_instruction(
        memory_context="## Long-Term Memory\nUser works on Clea.",
        focused_chat_available=False,
    )
    assert "focused_chat_available: false" in instruction_false
    assert "focused_chat_id:" not in instruction_false


async def test_resolve_voice_system_instruction_explicit_false_without_loader() -> None:
    instruction = await resolve_voice_system_instruction(
        memory_loader=None,
        focused_chat_available=False,
    )
    assert "focused_chat_available: false" in instruction


# --- 4. VoiceRunner Startup Context Injection (Offline, Unfocused, Focused, Daemon Event) ---


async def test_voice_runner_injects_focus_false_when_offline(tmp_path: Path) -> None:
    backend = _FakeLiveBackend()
    runner = VoiceRunner(
        config=VoiceConfig(silence_timeout_seconds=60.0),
        data_dir=tmp_path,
        backend_factory=lambda: backend,
        audio_manager=_make_fake_audio(),
        wake_word_detector=_make_fake_wake_word(),
        enable_hotkey=False,
    )

    await runner.start()
    try:
        await runner.start_call(trigger="hotkey")
        assert backend.connected_config is not None
        sys_inst = backend.connected_config.system_instruction
        assert "focused_chat_available: false" in sys_inst
        assert "focused_chat_id:" not in sys_inst
        await runner.end_call(reason="done")
    finally:
        await runner.stop()


async def test_voice_runner_injects_live_daemon_focus_state_for_hotkey_and_daemon_event(
    tmp_path: Path,
) -> None:
    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()
    chat_sid = "chat-focus-1"
    mem.append_transcript_message(
        session_id=chat_sid,
        role="user",
        text="Let's debug CrashLoopBackOff tomorrow.",
        title="K8s Deployment Fix",
        timestamp=datetime.now().astimezone(),
    )

    daemon: CleaDaemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=_FakeProvider(),
        memory=mem,
    )
    await daemon.start()
    url = f"ws://127.0.0.1:{daemon.bound_port}"

    backends: list[_FakeLiveBackend] = []

    def _factory() -> _FakeLiveBackend:
        b = _FakeLiveBackend()
        backends.append(b)
        return b

    runner = VoiceRunner(
        config=VoiceConfig(silence_timeout_seconds=60.0),
        data_dir=tmp_path,
        daemon_url=url,
        token=daemon.token,
        backend_factory=_factory,
        audio_manager=_make_fake_audio(),
        wake_word_detector=_make_fake_wake_word(),
        enable_hotkey=False,
    )
    runner_task = asyncio.create_task(runner.run_forever())
    try:
        await _wait_until(lambda: runner._daemon_listener_active)

        # Case A: Unfocused daemon connection -> focused_chat_available: false
        await runner.start_call(trigger="hotkey")
        assert backends[0].connected_config is not None
        inst_unfocused = backends[0].connected_config.system_instruction
        assert "focused_chat_available: false" in inst_unfocused
        assert "focused_chat_id:" not in inst_unfocused
        await runner.end_call(reason="done_a")

        # Focus chat via another UI RPC client on the daemon
        async with connect(url, subprotocols=auth.client_subprotocols(daemon.token)) as ui_ws:
            res = await _send_rpc(
                ui_ws,
                ChatFocusRequest(
                    params=ChatFocusParams(session_id=chat_sid),
                ),
            )
            assert res.ok

        # Case B: Focused chat + hotkey -> focused_chat_available: true with ID & title
        await runner.start_call(trigger="hotkey")
        assert backends[1].connected_config is not None
        inst_focused = backends[1].connected_config.system_instruction
        assert "focused_chat_available: true" in inst_focused
        assert f"focused_chat_id: {chat_sid}" in inst_focused
        assert "focused_chat_title: K8s Deployment Fix" in inst_focused
        await runner.end_call(reason="done_b")

        # Case C: trigger="daemon_event" (skips voice.start_call) still resolves chat.focus!
        await runner.start_call(trigger="daemon_event")
        assert backends[2].connected_config is not None
        inst_daemon_evt = backends[2].connected_config.system_instruction
        assert "focused_chat_available: true" in inst_daemon_evt
        assert f"focused_chat_id: {chat_sid}" in inst_daemon_evt
        assert "focused_chat_title: K8s Deployment Fix" in inst_daemon_evt
        await runner.end_call(reason="done_c")
    finally:
        await runner.stop()
        runner_task.cancel()
        await daemon.stop()


async def test_voice_runner_handles_focus_rpc_error_without_crashing(tmp_path: Path) -> None:
    backend = _FakeLiveBackend()
    runner = VoiceRunner(
        config=VoiceConfig(silence_timeout_seconds=60.0),
        data_dir=tmp_path,
        backend_factory=lambda: backend,
        audio_manager=_make_fake_audio(),
        wake_word_detector=_make_fake_wake_word(),
        enable_hotkey=False,
    )
    runner._daemon_ws = MagicMock()
    runner._daemon_listener_active = True

    async def _failing_request(req: BaseRequest) -> dict[str, Any]:
        raise TimeoutError(f"Timed out for {req.method}")

    runner._request_daemon = _failing_request  # type: ignore[method-assign]

    await runner.start()
    try:
        await runner.start_call(trigger="daemon_event")
        assert backend.connected_config is not None
        assert "focused_chat_available: false" in backend.connected_config.system_instruction
        await runner.end_call(reason="done_err")
    finally:
        runner._daemon_ws = None
        runner._daemon_listener_active = False
        await runner.stop()

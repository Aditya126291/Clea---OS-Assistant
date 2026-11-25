"""End-to-end tests for Clea Web UI self-control RPCs and Gemini Live voice tools."""

from __future__ import annotations

import asyncio
import contextlib
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

from websockets.asyncio.client import connect

from clea.core import auth
from clea.core.daemon import CleaDaemon, build_daemon
from clea.core.protocol import (
    ActivityListParams,
    ActivityListRequest,
    AssistantTokenEvent,
    BaseEvent,
    BaseRequest,
    CallEndedEvent,
    CallStartedEvent,
    ChatCreateAndOpenParams,
    ChatCreateAndOpenRequest,
    ConversationCreatedEvent,
    ConversationDeletedEvent,
    ConversationUpdatedEvent,
    MemoryUpdatedEvent,
    ResponseFrame,
    TaskStatusEvent,
    UiNavigateEvent,
    UiNavigateParams,
    UiNavigateRequest,
    VoiceStartCallParams,
    VoiceStartCallRequest,
    VoiceStateEvent,
    VoiceStatusRequest,
    parse_frame,
)
from clea.memory import MemoryStore
from clea.providers.base import LLMProvider, Message, StreamChunk, TokenUsage
from clea.surfaces.voice.audio import AudioManager
from clea.surfaces.voice.backends.base import (
    AudioOutputEvent,
    ToolCallEvent,
    TurnCompleteEvent,
    VoiceBackend,
    VoiceBackendEvent,
    VoiceSessionConfig,
    VoiceToolResponse,
)
from clea.surfaces.voice.base import default_voice_tools as shim_default_voice_tools
from clea.surfaces.voice.config import VoiceConfig
from clea.surfaces.voice.runner import VoiceRunner
from clea.surfaces.voice.wake_word import WakeWordDetector
from clea.tools import ToolDefinition


class FakeLiveBackend(VoiceBackend):
    """Minimal in-memory VoiceBackend for driving VoiceRunner tool calls."""

    backend_name = "fake-live"

    def __init__(self) -> None:
        self.connected_config: VoiceSessionConfig | None = None
        self.closed = False
        self.event_queue: asyncio.Queue[VoiceBackendEvent | None] = asyncio.Queue()
        self.sent_tool_responses: list[VoiceToolResponse] = []

    @property
    def model_name(self) -> str:
        return "fake-live-model"

    async def connect(self, config: VoiceSessionConfig) -> None:
        self.connected_config = config
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        await self.event_queue.put(None)

    async def send_audio(self, pcm_bytes: bytes) -> None:
        pass

    async def send_video_frame(self, image_bytes: bytes, *, mime_type: str = "image/jpeg") -> None:
        pass

    async def send_text(self, text: str, *, end_of_turn: bool = True) -> None:
        pass

    async def send_tool_responses(self, responses: list[VoiceToolResponse]) -> None:
        self.sent_tool_responses.extend(responses)

    async def events(self) -> AsyncIterator[VoiceBackendEvent]:
        while True:
            evt = await self.event_queue.get()
            if evt is None:
                break
            yield evt


class FakeStreamingProvider(LLMProvider):
    """Programmable streaming provider supporting token chunks and optional blocking gate."""

    provider_name = "fake"

    def __init__(
        self,
        *,
        chunks: list[str] | None = None,
        gate: asyncio.Event | None = None,
    ) -> None:
        self._chunks = chunks or ["Chunk 1 ", "Chunk 2"]
        self._gate = gate
        self.turn_count = 0
        self.recorded_prompts: list[str] = []

    @property
    def model_name(self) -> str:
        return "fake-chat-model"

    async def stream_turn(
        self,
        *,
        messages: list[Message],
        tools: list[ToolDefinition],
        system_prompt: str,
    ) -> AsyncIterator[StreamChunk]:
        self.turn_count += 1
        last_user = next((m.content for m in reversed(messages) if m.role == "user"), "")
        self.recorded_prompts.append(str(last_user))
        if self._gate is not None:
            await self._gate.wait()
        for chunk in self._chunks:
            yield StreamChunk(type="text_delta", text=chunk)
            await asyncio.sleep(0)
        yield StreamChunk(
            type="done",
            usage=TokenUsage(input_tokens=12, output_tokens=8),
        )


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
        await asyncio.sleep(0.02)
    raise TimeoutError("Predicate not satisfied within timeout.")


async def _send_rpc(ws: Any, req: BaseRequest) -> ResponseFrame:
    await ws.send(req.model_dump_json())
    while True:
        raw = await asyncio.wait_for(ws.recv(), timeout=5.0)
        frame = parse_frame(raw)
        if isinstance(frame, ResponseFrame) and frame.id == req.id:
            return frame


async def _invoke_voice_tool(
    backend: FakeLiveBackend,
    *,
    call_id: str,
    name: str,
    arguments: dict[str, Any] | None = None,
) -> VoiceToolResponse:
    before = len(backend.sent_tool_responses)
    await backend.event_queue.put(
        ToolCallEvent(
            call_id=call_id,
            name=name,
            arguments=arguments or {},
        )
    )
    await _wait_until(
        lambda: any(r.call_id == call_id for r in backend.sent_tool_responses[before:])
    )
    return next(r for r in backend.sent_tool_responses[before:] if r.call_id == call_id)


@asynccontextmanager
async def _running_voice_ui_env(
    tmp_path: Path,
    *,
    provider: LLMProvider | None = None,
) -> AsyncIterator[
    tuple[CleaDaemon, MemoryStore, VoiceRunner, FakeLiveBackend, Any, list[BaseEvent]]
]:
    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()
    active_provider = provider or FakeStreamingProvider()
    daemon: CleaDaemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=active_provider,
        memory=mem,
    )
    await daemon.start()

    url = f"ws://127.0.0.1:{daemon.bound_port}"
    backends: list[FakeLiveBackend] = []

    def _backend_factory() -> FakeLiveBackend:
        b = FakeLiveBackend()
        backends.append(b)
        return b

    runner = VoiceRunner(
        config=VoiceConfig(silence_timeout_seconds=60.0),
        data_dir=tmp_path,
        daemon_url=url,
        token=daemon.token,
        backend_factory=_backend_factory,
        audio_manager=_make_fake_audio(),
        wake_word_detector=_make_fake_wake_word(),
        enable_hotkey=False,
    )

    events: list[BaseEvent] = []
    runner_task = asyncio.create_task(runner.run_forever())
    reader_task: asyncio.Task[None] | None = None
    try:
        await _wait_until(lambda: runner._daemon_listener_active)

        async with (
            connect(url, subprotocols=auth.client_subprotocols(daemon.token)) as rpc_ws,
            connect(url, subprotocols=auth.client_subprotocols(daemon.token)) as ui_ws,
        ):

            async def _read_ui_events() -> None:
                while True:
                    raw = await ui_ws.recv()
                    frame = parse_frame(raw)
                    if isinstance(frame, BaseEvent):
                        events.append(frame)

            reader_task = asyncio.create_task(_read_ui_events())

            await runner.start_call(trigger="manual")
            await _wait_until(lambda: len(backends) == 1)
            backend = backends[-1]

            try:
                yield daemon, mem, runner, backend, rpc_ws, events
            finally:
                reader_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await reader_task
                if runner.call_state != "idle":
                    with contextlib.suppress(Exception):
                        await runner.end_call(reason="test_done")
    finally:
        if reader_task is not None and not reader_task.done():
            reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader_task
        await runner.stop()
        with contextlib.suppress(asyncio.CancelledError):
            await runner_task
        await daemon.stop()


def test_voice_base_shim_reexports_all_ui_control_tools() -> None:
    names = {tool.name for tool in shim_default_voice_tools()}
    expected_new = {
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
    }
    assert expected_new.issubset(names)


async def test_create_chat_when_active_conversation_is_none(tmp_path: Path) -> None:
    async with _running_voice_ui_env(tmp_path) as (daemon, mem, _runner, backend, rpc_ws, events):
        assert daemon._active_conversation_id is None

        resp = await _invoke_voice_tool(
            backend,
            call_id="call-create-1",
            name="create_chat",
            arguments={
                "title": "Auth Refactor Plan",
                "content": "## Step 1\nSwitch to short-lived tokens.",
                "open_in_ui": True,
            },
        )
        assert resp.is_error is False
        assert resp.response["ok"] is True
        sid = resp.response["session_id"]
        assert resp.response["title"] == "Auth Refactor Plan"
        assert daemon._active_conversation_id == sid

        await _wait_until(
            lambda: (
                any(
                    isinstance(e, ConversationCreatedEvent) and e.data.session_id == sid
                    for e in events
                )
                and any(
                    isinstance(e, UiNavigateEvent)
                    and e.data.view == "chat"
                    and e.data.conversation_id == sid
                    for e in events
                )
            )
        )

        created_evt = next(
            e
            for e in events
            if isinstance(e, ConversationCreatedEvent) and e.data.session_id == sid
        )
        assert created_evt.data.title == "Auth Refactor Plan"
        assert created_evt.data.initial_message == "## Step 1\nSwitch to short-lived tokens."
        assert created_evt.data.open_in_ui is True

        transcript = mem.get_transcript(sid)
        assert transcript is not None
        assert transcript["title"] == "Auth Refactor Plan"
        assert len(transcript["messages"]) == 1
        assert transcript["messages"][0]["role"] == "assistant"
        assert "short-lived tokens" in transcript["messages"][0]["text"]

        # Verify creating without content or open_in_ui=False works cleanly via RPC too.
        frame = await _send_rpc(
            rpc_ws,
            ChatCreateAndOpenRequest(
                params=ChatCreateAndOpenParams(
                    title="Empty Scratchpad", content="", open_in_ui=False
                )
            ),
        )
        assert frame.ok is True
        sid2 = frame.result["session_id"]
        transcript2 = mem.get_transcript(sid2)
        assert transcript2 is not None
        assert transcript2["title"] == "Empty Scratchpad"
        assert transcript2["messages"] == []


async def test_navigate_ui_views_conversation_and_command_palette(tmp_path: Path) -> None:
    async with _running_voice_ui_env(tmp_path) as (daemon, _mem, _runner, backend, _rpc, events):
        created = await _invoke_voice_tool(
            backend,
            call_id="call-c1",
            name="create_chat",
            arguments={"title": "Target Thread"},
        )
        sid = created.response["session_id"]
        assert daemon._active_conversation_id == sid

        for idx, view in enumerate(["activity", "memory", "settings", "voice"]):
            events.clear()
            nav = await _invoke_voice_tool(
                backend,
                call_id=f"call-nav-{idx}",
                name="navigate_ui",
                arguments={"view": view},
            )
            assert nav.is_error is False
            assert nav.response["view"] == view
            assert daemon._active_conversation_id is None
            await _wait_until(
                lambda view=view: any(
                    isinstance(e, UiNavigateEvent) and e.data.view == view for e in events
                )
            )

        # Navigate directly back to saved chat by conversation_id
        events.clear()
        nav_chat = await _invoke_voice_tool(
            backend,
            call_id="call-nav-chat",
            name="navigate_ui",
            arguments={"view": "chat", "conversation_id": sid},
        )
        assert nav_chat.is_error is False
        assert nav_chat.response["view"] == "chat"
        assert daemon._active_conversation_id == sid
        await _wait_until(
            lambda: any(
                isinstance(e, UiNavigateEvent)
                and e.data.view == "chat"
                and e.data.conversation_id == sid
                for e in events
            )
        )

        # Open command palette preserves active conversation focus
        events.clear()
        pal = await _invoke_voice_tool(
            backend,
            call_id="call-palette",
            name="open_command_palette",
            arguments={},
        )
        assert pal.is_error is False
        assert pal.response["open_command_palette"] is True
        assert daemon._active_conversation_id == sid
        await _wait_until(
            lambda: any(
                isinstance(e, UiNavigateEvent) and e.data.open_command_palette is True
                for e in events
            )
        )

        # Invalid view returns clean error
        bad = await _invoke_voice_tool(
            backend,
            call_id="call-nav-bad",
            name="navigate_ui",
            arguments={"view": "nonexistent"},
        )
        assert bad.is_error is True


async def test_send_chat_message_streams_live_in_new_and_existing_chat(tmp_path: Path) -> None:
    provider = FakeStreamingProvider(chunks=["Alpha ", "Beta"])
    async with _running_voice_ui_env(tmp_path, provider=provider) as (
        daemon,
        mem,
        _runner,
        backend,
        _rpc,
        events,
    ):
        assert daemon._active_conversation_id is None

        # 1. Omitting conversation_id creates a new titled chat and streams asynchronously
        res1 = await _invoke_voice_tool(
            backend,
            call_id="call-send-new",
            name="send_chat_message",
            arguments={"prompt": "Explain asyncio.Lock", "title": "Async Locks"},
        )
        assert res1.is_error is False
        assert res1.response["ok"] is True
        assert res1.response["status"] == "started"
        sid = res1.response["session_id"]
        assert daemon._active_conversation_id == sid

        await _wait_until(
            lambda: (
                any(
                    isinstance(e, AssistantTokenEvent)
                    and e.session_id == sid
                    and e.data.text == "Beta"
                    for e in events
                )
                and (mem.get_transcript(sid) or {}).get("messages")
                and len(mem.get_transcript(sid)["messages"]) >= 2  # type: ignore[index]
            )
        )

        tokens = [
            e.data.text
            for e in events
            if isinstance(e, AssistantTokenEvent) and e.session_id == sid
        ]
        assert tokens == ["Alpha ", "Beta"]
        assert any(
            isinstance(e, UiNavigateEvent)
            and e.data.view == "chat"
            and e.data.conversation_id == sid
            for e in events
        )
        transcript = mem.get_transcript(sid)
        assert transcript is not None
        assert transcript["title"] == "Async Locks"
        assert transcript["messages"][0]["role"] == "user"
        assert transcript["messages"][0]["text"] == "Explain asyncio.Lock"
        assert transcript["messages"][1]["role"] == "assistant"
        assert transcript["messages"][1]["text"] == "Alpha Beta"

        # 2. Sending follow-up into existing conversation_id appends second turn
        events.clear()
        res2 = await _invoke_voice_tool(
            backend,
            call_id="call-send-existing",
            name="send_chat_message",
            arguments={"prompt": "Give a short example", "conversation_id": sid},
        )
        assert res2.is_error is False
        assert res2.response["session_id"] == sid

        await _wait_until(lambda: len((mem.get_transcript(sid) or {}).get("messages", [])) >= 4)
        transcript2 = mem.get_transcript(sid)
        assert transcript2 is not None
        assert transcript2["messages"][2]["text"] == "Give a short example"
        assert transcript2["messages"][3]["text"] == "Alpha Beta"


async def test_rename_chat_and_delete_chat_guards(tmp_path: Path) -> None:
    async with _running_voice_ui_env(tmp_path) as (_daemon, mem, _runner, backend, _rpc, events):
        created = await _invoke_voice_tool(
            backend,
            call_id="call-cr",
            name="create_chat",
            arguments={"title": "Original Title", "content": "Draft notes"},
        )
        sid = created.response["session_id"]

        # Rename chat succeeds and broadcasts update
        events.clear()
        renamed = await _invoke_voice_tool(
            backend,
            call_id="call-ren",
            name="rename_chat",
            arguments={"conversation_id": sid, "new_title": "Renamed Notes"},
        )
        assert renamed.is_error is False
        assert renamed.response["title"] == "Renamed Notes"
        assert (mem.get_transcript(sid) or {})["title"] == "Renamed Notes"
        await _wait_until(
            lambda: any(
                isinstance(e, ConversationUpdatedEvent)
                and e.data.session_id == sid
                and e.data.title == "Renamed Notes"
                for e in events
            )
        )

        # Rename voice_companion is rejected
        ren_vc = await _invoke_voice_tool(
            backend,
            call_id="call-ren-vc",
            name="rename_chat",
            arguments={"conversation_id": "voice_companion", "new_title": "Illegal"},
        )
        assert ren_vc.is_error is True

        # Delete without confirmation is refused with requires_confirmation=True
        del_unconfirmed = await _invoke_voice_tool(
            backend,
            call_id="call-del-1",
            name="delete_chat",
            arguments={"conversation_id": sid, "confirmed": False},
        )
        assert del_unconfirmed.is_error is False
        assert del_unconfirmed.response["ok"] is False
        assert del_unconfirmed.response["deleted"] is False
        assert del_unconfirmed.response["requires_confirmation"] is True
        assert mem.get_transcript(sid) is not None

        # Delete voice_companion is protected even when confirmed=True
        del_vc = await _invoke_voice_tool(
            backend,
            call_id="call-del-vc",
            name="delete_chat",
            arguments={"conversation_id": "voice_companion", "confirmed": True},
        )
        assert del_vc.is_error is True

        # Confirmed delete succeeds and broadcasts ConversationDeletedEvent
        events.clear()
        del_ok = await _invoke_voice_tool(
            backend,
            call_id="call-del-2",
            name="delete_chat",
            arguments={"conversation_id": sid, "confirmed": True},
        )
        assert del_ok.is_error is False
        assert del_ok.response["ok"] is True
        assert del_ok.response["deleted"] is True
        assert mem.get_transcript(sid) is None
        await _wait_until(
            lambda: any(
                isinstance(e, ConversationDeletedEvent) and sid in e.data.session_ids
                for e in events
            )
        )


async def test_save_and_delete_memory_fact_broadcasts_updates(tmp_path: Path) -> None:
    async with _running_voice_ui_env(tmp_path) as (_daemon, mem, _runner, backend, _rpc, events):
        added = await _invoke_voice_tool(
            backend,
            call_id="call-mem-add",
            name="save_memory_fact",
            arguments={"fact": "User prefers concise pytest fixtures.", "category": "preferences"},
        )
        assert added.is_error is False
        assert added.response["ok"] is True
        assert added.response["section"] == "Preferences"
        fact_id = added.response["fact"]["id"]

        sections = mem.parse_sections()
        assert any(e.id == fact_id for e in sections["Preferences"])
        await _wait_until(
            lambda: any(
                isinstance(e, MemoryUpdatedEvent)
                and e.data.action == "added"
                and e.data.fact_id == fact_id
                and e.data.section == "Preferences"
                for e in events
            )
        )

        # Delete fact by ID/query and verify broadcast
        events.clear()
        deleted = await _invoke_voice_tool(
            backend,
            call_id="call-mem-del",
            name="delete_memory_fact",
            arguments={"query_or_id": fact_id},
        )
        assert deleted.is_error is False
        assert deleted.response["removed_count"] == 1
        assert fact_id in deleted.response["removed_ids"]
        assert not any(e.id == fact_id for e in mem.parse_sections()["Preferences"])
        await _wait_until(
            lambda: any(
                isinstance(e, MemoryUpdatedEvent)
                and e.data.action == "deleted"
                and fact_id in e.data.removed_ids
                for e in events
            )
        )


async def test_inspect_activity_and_cancel_running_task(tmp_path: Path) -> None:
    gate = asyncio.Event()
    provider = FakeStreamingProvider(gate=gate)
    async with _running_voice_ui_env(tmp_path, provider=provider) as (
        daemon,
        _mem,
        _runner,
        backend,
        rpc_ws,
        events,
    ):
        # Start an in-flight chat turn from voice that pauses on provider gate
        started = await _invoke_voice_tool(
            backend,
            call_id="call-start-slow",
            name="send_chat_message",
            arguments={"prompt": "Run long background analysis", "title": "Long Run"},
        )
        assert started.is_error is False

        await _wait_until(lambda: len(daemon._active_task_handles) >= 1)

        inspected = await _invoke_voice_tool(
            backend,
            call_id="call-inspect",
            name="inspect_activity",
            arguments={"limit": 10},
        )
        assert inspected.is_error is False
        running_tasks = [t for t in inspected.response["tasks"] if t["status"] == "running"]
        assert len(running_tasks) >= 1
        task_id = running_tasks[0]["task_id"]

        # Cancel the running task and verify status & TaskStatusEvent
        events.clear()
        cancelled = await _invoke_voice_tool(
            backend,
            call_id="call-cancel",
            name="cancel_task",
            arguments={"task_id": task_id},
        )
        assert cancelled.is_error is False
        assert cancelled.response["ok"] is True
        assert cancelled.response["status"] == "cancelled"

        await _wait_until(
            lambda: any(
                isinstance(e, TaskStatusEvent)
                and e.data.task_id == task_id
                and e.data.status == "cancelled"
                for e in events
            )
        )

        # Verify activity.list RPC also reflects the cancelled status
        list_frame = await _send_rpc(
            rpc_ws,
            ActivityListRequest(params=ActivityListParams(limit=10)),
        )
        assert list_frame.ok is True
        by_tid = {t["task_id"]: t for t in list_frame.result["tasks"]}
        assert by_tid[task_id]["status"] == "cancelled"
        gate.set()

        # Navigate RPC smoke check
        nav_frame = await _send_rpc(
            rpc_ws,
            UiNavigateRequest(params=UiNavigateParams(view="activity")),
        )
        assert nav_frame.ok is True
        assert nav_frame.result["view"] == "activity"


async def test_wake_word_call_broadcasts_state_hydrates_late_ui_and_attaches_on_ui_click(
    tmp_path: Path,
) -> None:
    """Wake-word calls broadcast state to UI, hydrate late clients, and attach on UI click."""
    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()
    daemon: CleaDaemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=FakeStreamingProvider(),
        memory=mem,
    )
    await daemon.start()

    url = f"ws://127.0.0.1:{daemon.bound_port}"
    backends: list[FakeLiveBackend] = []

    def _backend_factory() -> FakeLiveBackend:
        b = FakeLiveBackend()
        backends.append(b)
        return b

    wake_model = MagicMock()
    wake_scores = iter([{"hey_jarvis_v0.1": 0.95}])
    wake_model.predict.side_effect = lambda _arr: next(wake_scores, {"hey_jarvis_v0.1": 0.0})
    wake_detector = WakeWordDetector(model_instance=wake_model)

    runner = VoiceRunner(
        config=VoiceConfig(silence_timeout_seconds=60.0),
        data_dir=tmp_path,
        daemon_url=url,
        token=daemon.token,
        backend_factory=_backend_factory,
        audio_manager=_make_fake_audio(),
        wake_word_detector=wake_detector,
        enable_hotkey=False,
    )

    ui_events: list[BaseEvent] = []
    runner_task = asyncio.create_task(runner.run_forever())
    reader_task: asyncio.Task[None] | None = None
    try:
        await _wait_until(lambda: runner._daemon_listener_active)

        async with connect(url, subprotocols=auth.client_subprotocols(daemon.token)) as ui_ws:

            async def _read_ui() -> None:
                while True:
                    raw = await ui_ws.recv()
                    frame = parse_frame(raw)
                    if isinstance(frame, BaseEvent):
                        ui_events.append(frame)

            reader_task = asyncio.create_task(_read_ui())

            # Trigger wake-word call from audio chunk (no UI click)
            runner._on_audio_chunk(b"\x01\x00" * 1280)

            await _wait_until(
                lambda: (
                    len(backends) == 1
                    and runner.call_state == "active"
                    and any(isinstance(e, CallStartedEvent) for e in ui_events)
                    and any(
                        isinstance(e, VoiceStateEvent) and e.data.state == "listening"
                        for e in ui_events
                    )
                )
            )
            backend = backends[-1]
            started_evt = next(e for e in ui_events if isinstance(e, CallStartedEvent))
            assert started_evt.data.call_id == runner.call_id
            assert started_evt.data.call_state == "active"
            voice_states = [
                e.data.state for e in ui_events if isinstance(e, VoiceStateEvent)
            ]
            assert "thinking" in voice_states
            assert "listening" in voice_states

            # Emit assistant speech -> speaking -> turn complete -> listening
            await backend.event_queue.put(AudioOutputEvent(pcm_bytes=b"\x02\x00" * 160))
            await _wait_until(
                lambda: any(
                    isinstance(e, VoiceStateEvent) and e.data.state == "speaking"
                    for e in ui_events
                )
            )
            await backend.event_queue.put(TurnCompleteEvent())
            await _wait_until(lambda: runner.voice_state == "listening")

            # Late-connecting UI client hydrates active call via voice.status
            async with connect(
                url, subprotocols=auth.client_subprotocols(daemon.token)
            ) as late_ui_ws:
                status_resp = await _send_rpc(late_ui_ws, VoiceStatusRequest())
                assert status_resp.ok is True
                assert status_resp.result["call_id"] == runner.call_id
                assert status_resp.result["call_state"] == "active"
                assert status_resp.result["voice_state"] == "listening"

                # Manual UI click (trigger="ui") attaches cleanly instead of erroring
                attach_resp = await _send_rpc(
                    late_ui_ws,
                    VoiceStartCallRequest(params=VoiceStartCallParams(trigger="ui")),
                )
                assert attach_resp.ok is True
                assert attach_resp.result["call_id"] == runner.call_id
                assert attach_resp.result["call_state"] == "active"
                assert attach_resp.result["voice_state"] == "listening"

                # Duplicate non-UI start_call is still rejected while active
                dup_resp = await _send_rpc(
                    late_ui_ws,
                    VoiceStartCallRequest(params=VoiceStartCallParams(trigger="wake_word")),
                )
                assert dup_resp.ok is False
                assert "already active" in (dup_resp.error or "")

            # End call and verify idle + call_ended broadcasts reach UI
            ui_events.clear()
            await runner.end_call(reason="user_done")
            await _wait_until(
                lambda: (
                    any(
                        isinstance(e, VoiceStateEvent) and e.data.state == "idle"
                        for e in ui_events
                    )
                    and any(
                        isinstance(e, CallEndedEvent) and e.data.call_state == "idle"
                        for e in ui_events
                    )
                )
            )
    finally:
        if reader_task is not None and not reader_task.done():
            reader_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await reader_task
        await runner.stop()
        with contextlib.suppress(asyncio.CancelledError):
            await runner_task
        await daemon.stop()

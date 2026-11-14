"""Tests for voice call history, consolidation hooks, and background task recovery."""

from __future__ import annotations

import asyncio
import json
import logging
from collections.abc import AsyncIterator
from datetime import datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from websockets.asyncio.client import connect

from clea.core import auth
from clea.core.daemon import build_daemon
from clea.core.protocol import (
    ChatSendParams,
    ChatSendRequest,
    ConversationGetParams,
    ConversationGetRequest,
    ConversationResumeParams,
    ConversationResumeRequest,
    ConversationsListRequest,
    ConversationUpdatedEvent,
    ResponseFrame,
    TaskStatusEvent,
    VoiceEndCallParams,
    VoiceEndCallRequest,
    parse_frame,
)
from clea.memory.consolidation import ConsolidationManager
from clea.memory.store import MemoryStore
from clea.providers.base import LLMProvider, Message, StreamChunk
from clea.surfaces.voice.audio import AudioManager
from clea.surfaces.voice.backends.base import (
    TranscriptOutputEvent,
    TurnCompleteEvent,
    VoiceBackend,
    VoiceBackendEvent,
    VoiceSessionConfig,
    VoiceToolResponse,
)
from clea.surfaces.voice.config import VoiceConfig
from clea.surfaces.voice.memory_hooks import (
    BASE_VOICE_SYSTEM_INSTRUCTION,
    build_voice_call_title,
    normalize_voice_transcript,
    resolve_voice_system_instruction,
    safe_consolidate_voice_call,
    safe_store_voice_transcript,
)
from clea.surfaces.voice.runner import VoiceRunner
from clea.surfaces.voice.wake_word import WakeWordDetector


class FakeLiveBackend(VoiceBackend):
    """Minimal in-memory Live backend for voice memory integration tests."""

    backend_name = "fake-live"

    def __init__(self) -> None:
        self.connected_config: VoiceSessionConfig | None = None
        self.closed = False
        self.event_queue: asyncio.Queue[VoiceBackendEvent | None] = asyncio.Queue()

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
        pass

    async def events(self) -> AsyncIterator[VoiceBackendEvent]:
        while True:
            evt = await self.event_queue.get()
            if evt is None:
                break
            yield evt


class FakeConsolidationProvider(LLMProvider):
    """Fake provider supporting both chat turns and memory consolidation JSON output."""

    def __init__(
        self,
        *,
        consolidation_json: str | None = None,
        delay_seconds: float = 0.0,
        fail_with: Exception | None = None,
        chat_reply: str = "Continuing cleanly in text chat!",
    ) -> None:
        self.consolidation_json = consolidation_json or json.dumps(
            {
                "summary": "Discussed moving project deployment to Fly.io and tea preference.",
                "operations": [
                    {
                        "op": "add",
                        "target_section": "Preferences",
                        "text": "User prefers masala chai in the morning.",
                        "source_tag": "stated",
                        "reason": "Explicit user preference from voice call",
                    }
                ],
            }
        )
        self.delay_seconds = delay_seconds
        self.fail_with = fail_with
        self.chat_reply = chat_reply
        self.calls: list[dict[str, Any]] = []

    async def stream_turn(
        self,
        messages: list[Message],
        tools: list[dict[str, Any]],
        system_prompt: str,
        **kwargs: Any,
    ) -> AsyncIterator[StreamChunk]:
        self.calls.append(
            {
                "messages": list(messages),
                "system_prompt": system_prompt,
                "kwargs": kwargs,
            }
        )
        if self.delay_seconds > 0:
            await asyncio.sleep(self.delay_seconds)
        if self.fail_with is not None:
            raise self.fail_with

        if "memory consolidation" in system_prompt.lower() or "target_section" in system_prompt:
            yield StreamChunk(type="text_delta", text=self.consolidation_json)
        else:
            yield StreamChunk(type="text_delta", text=self.chat_reply)
        yield StreamChunk(type="done", text="", usage={"input_tokens": 10, "output_tokens": 5})


def _make_fake_audio() -> AudioManager:
    audio = AudioManager()
    audio.start = MagicMock()  # type: ignore[method-assign]
    audio.stop = MagicMock()  # type: ignore[method-assign]
    audio.play_audio = MagicMock()  # type: ignore[method-assign]
    audio.stop_output = MagicMock()  # type: ignore[method-assign]
    return audio


def _make_fake_wake_word() -> WakeWordDetector:
    mock_model = MagicMock()
    mock_model.predict.return_value = {"hey_jarvis_v0.1": 0.0}
    return WakeWordDetector(model_instance=mock_model)


async def _wait_until(predicate: Any, *, timeout: float = 3.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if predicate():
            return
        await asyncio.sleep(0.02)
    raise AssertionError(f"Condition not met within {timeout}s")


async def _send_rpc(ws: Any, req: Any) -> ResponseFrame:
    await ws.send(req.model_dump_json())
    while True:
        frame = parse_frame(await asyncio.wait_for(ws.recv(), timeout=3.0))
        if isinstance(frame, ResponseFrame) and frame.id == req.id:
            return frame


# --- Unit tests for transcript normalization and title generation ---


def test_normalize_voice_transcript_coalesces_deltas_and_preserves_order() -> None:
    entries = [
        {"role": "user", "text": "   ", "final": False, "timestamp": "2026-11-14T10:00:00+05:30"},
        {
            "role": "user",
            "text": "Remind ",
            "final": False,
            "timestamp": "2026-11-14T10:00:01+05:30",
        },
        {
            "role": "user",
            "text": "me about ",
            "final": False,
            "timestamp": "2026-11-14T10:00:02+05:30",
        },
        {"role": "user", "text": "chai!", "final": True, "timestamp": "2026-11-14T10:00:03+05:30"},
        {
            "role": "assistant",
            "text": "Got it, ",
            "final": False,
            "timestamp": "2026-11-14T10:00:02+05:30",  # out-of-order clock skew clamped
        },
        {
            "role": "assistant",
            "text": "noted!",
            "final": True,
            "timestamp": "2026-11-14T10:00:05+05:30",
        },
    ]

    normalized = normalize_voice_transcript(entries)
    assert len(normalized) == 2
    assert normalized[0]["role"] == "user"
    assert normalized[0]["text"] == "Remind me about chai!"
    assert normalized[1]["role"] == "assistant"
    assert normalized[1]["text"] == "Got it, noted!"
    ts0 = datetime.fromisoformat(normalized[0]["timestamp"])
    ts1 = datetime.fromisoformat(normalized[1]["timestamp"])
    assert ts1 >= ts0


def test_build_voice_call_title_uses_first_user_turn_and_fallbacks() -> None:
    entries = [
        {"role": "assistant", "text": "Hey there! How can I help today?"},
        {
            "role": "user",
            "text": "  Can you help me debug the daemon startup race condition please?  ",
        },
    ]
    title = build_voice_call_title(entries, max_len=45)
    assert title.startswith("Can you help me debug the daemon startup")
    assert len(title) <= 45

    assistant_only = [
        {
            "role": "assistant",
            "text": "Hello!",
            "timestamp": "2026-11-14T14:25:00+05:30",
        }
    ]
    assert build_voice_call_title(assistant_only) == "Voice call (14:25)"


# --- Integration tests: VoiceRunner <-> CleaDaemon History & Consolidation ---


async def test_voice_call_persists_to_history_and_can_resume_in_text_chat(tmp_path: Path) -> None:
    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()
    mem.add_or_update_entry(
        text="User lives in Bengaluru.",
        section="Profile",
        source="stated",
    )

    provider = FakeConsolidationProvider()
    daemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=provider,
        memory=mem,
    )
    daemon.consolidation.min_user_turns = 2
    await daemon.start()

    url = f"ws://127.0.0.1:{daemon.bound_port}"
    backend = FakeLiveBackend()
    runner = VoiceRunner(
        config=VoiceConfig(silence_timeout_seconds=60.0),
        data_dir=tmp_path,
        daemon_url=url,
        token=daemon.token,
        backend_factory=lambda: backend,
        audio_manager=_make_fake_audio(),
        wake_word_detector=_make_fake_wake_word(),
        enable_hotkey=False,
    )

    runner_task: asyncio.Task[None] | None = None
    try:
        runner_task = asyncio.create_task(runner.run_forever())
        await _wait_until(lambda: runner._daemon_listener_active)

        # Open a UI client websocket to verify History & events
        async with connect(url, subprotocols=auth.client_subprotocols(daemon.token)) as ui_ws:
            await runner.start_call(trigger="hotkey")
            assert runner.call_state == "active"
            call_id = runner.call_id
            assert call_id is not None

            # Verify VoiceRunner received memory context in Live system instruction
            assert backend.connected_config is not None
            assert "User lives in Bengaluru." in backend.connected_config.system_instruction

            # Stream multi-turn conversation (including partial deltas)
            await backend.event_queue.put(
                TranscriptOutputEvent(role="user", text="Hey Clea, ", final=False)
            )
            await backend.event_queue.put(
                TranscriptOutputEvent(
                    role="user",
                    text="I prefer masala chai every morning.",
                    final=True,
                )
            )
            await backend.event_queue.put(
                TranscriptOutputEvent(
                    role="assistant",
                    text="Got it, masala chai in the morning!",
                    final=True,
                )
            )
            await backend.event_queue.put(TurnCompleteEvent())
            await backend.event_queue.put(
                TranscriptOutputEvent(
                    role="user",
                    text="Also remind me about Fly.io deploy.",
                    final=True,
                )
            )
            await backend.event_queue.put(
                TranscriptOutputEvent(
                    role="assistant",
                    text="Sure, I will keep Fly.io in mind.",
                    final=True,
                )
            )
            await backend.event_queue.put(TurnCompleteEvent())
            await _wait_until(lambda: len(runner._transcript_history) >= 5)

            # End voice call
            await runner.end_call(reason="user_done")
            assert runner.call_state == "idle"
            assert runner.voice_state == "idle"

            # Verify ConversationUpdatedEvent was broadcast to UI clients
            saw_conv_updated = False
            deadline = asyncio.get_running_loop().time() + 2.0
            while asyncio.get_running_loop().time() < deadline:
                try:
                    raw = await asyncio.wait_for(ui_ws.recv(), timeout=0.2)
                except TimeoutError:
                    break
                ev = parse_frame(raw)
                if isinstance(ev, ConversationUpdatedEvent) and ev.data.session_id == call_id:
                    saw_conv_updated = True
                    break
            assert saw_conv_updated is True

            # Verify History sidebar query keeps sidebar clean while voice_companion has it
            list_resp = await _send_rpc(ui_ws, ConversationsListRequest())
            assert list_resp.ok
            convs = list_resp.result["conversations"]
            assert len(convs) == 0

            # Verify Voice Companion stream contains the turns
            from clea.core.protocol import VoiceHistoryParams, VoiceHistoryRequest

            vh_req = VoiceHistoryRequest(params=VoiceHistoryParams(limit=20))
            vh_resp = await _send_rpc(ui_ws, vh_req)
            assert vh_resp.ok
            vh_msgs = vh_resp.result["messages"]
            assert len(vh_msgs) == 4
            assert vh_msgs[0]["call_id"] == call_id
            assert vh_msgs[0]["text"] == "Hey Clea, I prefer masala chai every morning."

            # Verify chronological full transcript retrieval (turns + raw transcript timestamps)
            get_resp = await _send_rpc(
                ui_ws,
                ConversationGetRequest(params=ConversationGetParams(session_id=call_id)),
            )
            assert get_resp.ok
            turns = get_resp.result["turns"]
            assert len(turns) == 2
            assert turns[0]["user"] == "Hey Clea, I prefer masala chai every morning."
            assert turns[0]["assistant"] == "Got it, masala chai in the morning!"
            assert turns[1]["user"] == "Also remind me about Fly.io deploy."
            assert turns[1]["assistant"] == "Sure, I will keep Fly.io in mind."

            raw_tr = mem.get_transcript(call_id)
            assert raw_tr is not None
            raw_msgs = raw_tr["messages"]
            assert [m["role"] for m in raw_msgs] == ["user", "assistant", "user", "assistant"]
            parsed_ts = [datetime.fromisoformat(m["timestamp"]) for m in raw_msgs]
            assert parsed_ts == sorted(parsed_ts)

            # Verify user can reopen & continue the voice call in text chat
            resume_resp = await _send_rpc(
                ui_ws,
                ConversationResumeRequest(params=ConversationResumeParams(session_id=call_id)),
            )
            assert resume_resp.ok
            assert len(resume_resp.result["messages"]) == 4

            chat_resp = await _send_rpc(
                ui_ws,
                ChatSendRequest(
                    session_id=call_id,
                    params=ChatSendParams(text="Let's continue this in text."),
                ),
            )
            assert chat_resp.ok
            assert provider.chat_reply in chat_resp.result["text"]

            # Verify call-end consolidation extracted Durable Facts + Episode asynchronously
            await _wait_until(
                lambda: (
                    "masala chai" in mem.read_long_term()
                    and len(mem.get_recent_episodes(limit=10)) >= 1
                )
            )
            assert "User prefers masala chai in the morning." in mem.read_long_term()
            episodes = mem.get_recent_episodes(limit=10)
            assert any("Fly.io" in ep["summary"] for ep in episodes)
    finally:
        await runner.stop()
        if runner_task is not None:
            await asyncio.wait_for(runner_task, timeout=2.0)
        await daemon.stop()


async def test_ui_hangup_still_persists_voice_transcript_and_consolidates(tmp_path: Path) -> None:
    mem = MemoryStore(base_dir=tmp_path)
    provider = FakeConsolidationProvider()
    daemon = build_daemon(host="127.0.0.1", port=0, provider=provider, memory=mem)
    daemon.consolidation.min_user_turns = 1
    await daemon.start()

    url = f"ws://127.0.0.1:{daemon.bound_port}"
    backend = FakeLiveBackend()
    runner = VoiceRunner(
        config=VoiceConfig(silence_timeout_seconds=60.0),
        data_dir=tmp_path,
        daemon_url=url,
        token=daemon.token,
        backend_factory=lambda: backend,
        audio_manager=_make_fake_audio(),
        wake_word_detector=_make_fake_wake_word(),
        enable_hotkey=False,
    )

    runner_task: asyncio.Task[None] | None = None
    try:
        runner_task = asyncio.create_task(runner.run_forever())
        await _wait_until(lambda: runner._daemon_listener_active)

        await runner.start_call(trigger="hotkey")
        call_id = runner.call_id
        assert call_id is not None

        await backend.event_queue.put(
            TranscriptOutputEvent(role="user", text="Remember I drink masala chai.", final=True)
        )
        await backend.event_queue.put(
            TranscriptOutputEvent(role="assistant", text="Noted!", final=True)
        )
        await _wait_until(lambda: len(runner._transcript_history) == 2)

        # Simulate UI button click ending the call without transcript in UI request
        async with connect(url, subprotocols=auth.client_subprotocols(daemon.token)) as ui_ws:
            end_resp = await _send_rpc(
                ui_ws,
                VoiceEndCallRequest(params=VoiceEndCallParams(call_id=call_id, reason="ui_button")),
            )
            assert end_resp.ok

            # Wait for runner to process CallEndedEvent -> end_call -> transcript_sync + consolidate
            await _wait_until(lambda: runner.call_state == "idle")
            await _wait_until(lambda: mem.get_transcript(call_id) is not None)
            await _wait_until(lambda: "masala chai" in mem.read_long_term())

            tr = mem.get_transcript(call_id)
            assert tr is not None
            assert len(tr["messages"]) == 2
            assert tr["title"].startswith("Remember I drink masala chai.")
    finally:
        await runner.stop()
        if runner_task is not None:
            await asyncio.wait_for(runner_task, timeout=2.0)
        await daemon.stop()


async def test_empty_voice_call_is_not_persisted_or_consolidated(tmp_path: Path) -> None:
    mem = MemoryStore(base_dir=tmp_path)
    provider = FakeConsolidationProvider()
    daemon = build_daemon(host="127.0.0.1", port=0, provider=provider, memory=mem)
    await daemon.start()

    url = f"ws://127.0.0.1:{daemon.bound_port}"
    backend = FakeLiveBackend()
    runner = VoiceRunner(
        config=VoiceConfig(silence_timeout_seconds=60.0),
        data_dir=tmp_path,
        daemon_url=url,
        token=daemon.token,
        backend_factory=lambda: backend,
        audio_manager=_make_fake_audio(),
        wake_word_detector=_make_fake_wake_word(),
        enable_hotkey=False,
    )

    runner_task: asyncio.Task[None] | None = None
    try:
        runner_task = asyncio.create_task(runner.run_forever())
        await _wait_until(lambda: runner._daemon_listener_active)

        await runner.start_call(trigger="wake_word")
        call_id = runner.call_id
        assert call_id is not None

        # Only whitespace chunk -> effectively empty call
        await backend.event_queue.put(
            TranscriptOutputEvent(role="user", text="   \n  ", final=True)
        )
        await asyncio.sleep(0.05)

        await runner.end_call(reason="silence_timeout")
        assert runner.call_state == "idle"
        await asyncio.sleep(0.1)

        assert mem.get_transcript(call_id) is None
        assert mem.list_conversations() == []
        assert mem.get_recent_episodes(limit=10) == []
        assert provider.calls == []
    finally:
        await runner.stop()
        if runner_task is not None:
            await asyncio.wait_for(runner_task, timeout=2.0)
        await daemon.stop()


async def test_voice_memory_hook_errors_never_crash_or_delay_idle_transition(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.WARNING, logger="clea.surfaces.voice.memory_hooks")

    async def _failing_store(*, call_id: str, entries: object) -> None:
        raise RuntimeError("DB write failure")

    async def _failing_consolidate(*, call_id: str) -> None:
        raise ConnectionError("Ollama unreachable")

    assert (
        await safe_store_voice_transcript(
            call_id="c-err",
            entries=[{"role": "user", "text": "Hello"}],
            handler=_failing_store,
        )
        is False
    )
    assert (
        await safe_consolidate_voice_call(
            call_id="c-err",
            handler=_failing_consolidate,
        )
        is False
    )
    assert (
        await resolve_voice_system_instruction(data_dir=tmp_path) == BASE_VOICE_SYSTEM_INSTRUCTION
    )

    # VoiceRunner with no running daemon transitions back to IDLE cleanly even with transcript
    backend = FakeLiveBackend()
    runner = VoiceRunner(
        config=VoiceConfig(silence_timeout_seconds=60.0),
        data_dir=tmp_path,
        daemon_url="ws://127.0.0.1:1",
        token="offline-token",
        backend_factory=lambda: backend,
        audio_manager=_make_fake_audio(),
        wake_word_detector=_make_fake_wake_word(),
        enable_hotkey=False,
    )
    await runner.start()
    try:
        await runner.start_call(trigger="hotkey")
        await backend.event_queue.put(
            TranscriptOutputEvent(role="user", text="Offline call test", final=True)
        )
        await _wait_until(lambda: len(runner._transcript_history) == 1)
        await runner.end_call(reason="user_done")
        assert runner.call_state == "idle"
        assert runner.voice_state == "idle"
    finally:
        await runner.stop()


# --- Background task recovery, strong references & timeout tests ---


async def test_daemon_startup_reconciles_orphaned_running_background_tasks(tmp_path: Path) -> None:
    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()

    # Seed orphaned running & queued background tasks from a previous crashed daemon run
    mem.record_background_task(
        task_id="cons-old-session-1",
        title="Summarising session 18:11",
        status="running",
        started_at="2026-11-14T18:11:00+05:30",
        kind="background_task",
        session_id="old-session-1",
    )
    mem.record_background_task(
        task_id="agent-old-2",
        title="Check git status",
        status="queued",
        started_at="2026-11-14T18:12:00+05:30",
        kind="agent_run",
        session_id="old-session-2",
    )
    mem.record_background_task(
        task_id="cons-done-3",
        title="Summarising session 17:00",
        status="succeeded",
        started_at="2026-11-14T17:00:00+05:30",
        finished_at="2026-11-14T17:00:05+05:30",
        kind="background_task",
        session_id="done-session-3",
    )

    state_file = mem.memory_dir / "consolidation_state.json"
    state_file.write_text(
        json.dumps(
            {
                "sessions": {
                    "old-session-1": {
                        "status": "running",
                        "started_at": "2026-11-14T18:11:00+05:30",
                        "retry_count": 0,
                    }
                }
            }
        ),
        encoding="utf-8",
    )

    daemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=FakeConsolidationProvider(),
        memory=mem,
        token_file=auth.token_path(tmp_path),
        lock_file=auth.lock_path(tmp_path),
    )
    await daemon.start()
    try:
        tasks = mem.load_background_tasks()
        assert tasks["cons-old-session-1"]["status"] == "failed"
        assert tasks["cons-old-session-1"]["detail"] == "Interrupted by daemon restart"
        assert tasks["cons-old-session-1"]["finished_at"] is not None

        assert tasks["agent-old-2"]["status"] == "failed"
        assert tasks["agent-old-2"]["detail"] == "Interrupted by daemon restart"

        # Already-succeeded tasks remain untouched
        assert tasks["cons-done-3"]["status"] == "succeeded"

        state = json.loads(state_file.read_text(encoding="utf-8"))
        assert state["sessions"]["old-session-1"]["status"] == "failed"
        assert state["sessions"]["old-session-1"]["error"] == "Interrupted by daemon restart"
    finally:
        await daemon.stop()


async def test_consolidation_timeout_and_strong_task_refs_transition_to_failed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLEA_CONSOLIDATION_TIMEOUT_SECONDS", "0.05")
    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()
    session_id = "hung-session-1"
    for role, text in [
        ("user", "First question about project architecture."),
        ("assistant", "First answer."),
        ("user", "Second question about deployment."),
        ("assistant", "Second answer."),
    ]:
        mem.append_transcript_message(session_id=session_id, role=role, text=text)

    # Provider hangs far longer than timeout=0.05s
    hung_provider = FakeConsolidationProvider(delay_seconds=5.0)
    daemon = build_daemon(host="127.0.0.1", port=0, provider=hung_provider, memory=mem)
    daemon.consolidation.min_user_turns = 1
    await daemon.start()

    try:
        url = f"ws://127.0.0.1:{daemon.bound_port}"
        async with connect(url, subprotocols=auth.client_subprotocols(daemon.token)) as ws:
            task = daemon._schedule_consolidation_now(session_id)
            assert task in daemon._background_tasks

            events: list[TaskStatusEvent] = []
            deadline = asyncio.get_running_loop().time() + 2.0
            while len(events) < 2 and asyncio.get_running_loop().time() < deadline:
                frame = parse_frame(await asyncio.wait_for(ws.recv(), timeout=2.0))
                if (
                    isinstance(frame, TaskStatusEvent)
                    and frame.data.task_id == f"cons-{session_id}"
                ):
                    events.append(frame)

            assert len(events) == 2
            assert events[0].data.status == "running"
            assert events[0].data.title.startswith("Summarising session ")
            assert events[1].data.status == "failed"
            assert events[1].data.detail is not None
            assert "timed out" in events[1].data.detail.lower()
            assert events[1].data.finished_at is not None

            await asyncio.wait_for(task, timeout=1.0)
            await _wait_until(lambda: task not in daemon._background_tasks)

            tasks = mem.load_background_tasks()
            assert tasks[f"cons-{session_id}"]["status"] == "failed"

            state = daemon.consolidation.load_state()
            assert state["sessions"][session_id]["status"] == "failed"
    finally:
        await daemon.stop()


async def test_consolidation_empty_transcript_and_unexpected_error_never_stay_running(
    tmp_path: Path,
) -> None:
    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()
    manager = ConsolidationManager(
        memory=mem,
        provider=FakeConsolidationProvider(),
        min_user_turns=2,
        llm_timeout_seconds=1.0,
    )

    # 1. Missing/empty transcript must emit running -> succeeded (never stuck in running)
    empty_statuses: list[tuple[str, str | None]] = []

    async def _empty_cb(status: str, detail: str | None = None) -> None:
        empty_statuses.append((status, detail))

    ok = await manager.consolidate_session("nonexistent-session", on_task_status=_empty_cb)
    assert ok is True
    assert empty_statuses == [("running", None), ("succeeded", None)]

    # 2. Unexpected exception during transcript load/processing must emit running -> failed
    broken_mem = MemoryStore(base_dir=tmp_path / "broken")
    broken_mem.ensure_initialized()
    broken_manager = ConsolidationManager(
        memory=broken_mem,
        provider=FakeConsolidationProvider(),
        llm_timeout_seconds=1.0,
    )
    broken_mem.get_transcript = MagicMock(side_effect=OSError("disk I/O failure"))  # type: ignore[method-assign]

    err_statuses: list[tuple[str, str | None]] = []

    async def _err_cb(status: str, detail: str | None = None) -> None:
        err_statuses.append((status, detail))

    ok2 = await broken_manager.consolidate_session("disk-err-session", on_task_status=_err_cb)
    assert ok2 is False
    assert len(err_statuses) == 2
    assert err_statuses[0] == ("running", None)
    assert err_statuses[1][0] == "failed"
    assert "disk I/O failure" in (err_statuses[1][1] or "")


async def test_voice_transcript_sync_succeeds_during_subsequent_call_and_rejects_bad_id(
    tmp_path: Path,
) -> None:
    from clea.core.protocol import VoiceStartCallParams, VoiceStartCallRequest

    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()
    daemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=FakeConsolidationProvider(),
        memory=mem,
        token_file=auth.token_path(tmp_path),
        lock_file=auth.lock_path(tmp_path),
    )
    await daemon.start()
    try:
        url = f"ws://127.0.0.1:{daemon.bound_port}"
        async with connect(url, subprotocols=auth.client_subprotocols(daemon.token)) as ws:
            # Start a brand-new active call ("call-2")
            await ws.send(
                VoiceStartCallRequest(
                    params=VoiceStartCallParams(trigger="wake_word")
                ).model_dump_json()
            )
            start_frame = parse_frame(await asyncio.wait_for(ws.recv(), timeout=2.0))
            while not isinstance(start_frame, ResponseFrame):
                start_frame = parse_frame(await asyncio.wait_for(ws.recv(), timeout=2.0))
            assert start_frame.ok and start_frame.result is not None
            active_call_id = start_frame.result["call_id"]

            # Late transcript_sync + consolidate for an earlier call ("voice-prev-call")
            # that daemon never saw start_call for must still persist and consolidate!
            sync_req = VoiceEndCallRequest(
                params=VoiceEndCallParams(
                    call_id="voice-prev-call",
                    reason="transcript_sync",
                    transcript=[
                        {
                            "role": "user",
                            "text": "User only voice question",
                            "timestamp": "2026-11-14T10:00:00+00:00",
                        }
                    ],
                )
            )
            await ws.send(sync_req.model_dump_json())
            sync_resp = parse_frame(await asyncio.wait_for(ws.recv(), timeout=2.0))
            while not isinstance(sync_resp, ResponseFrame) or sync_resp.id != sync_req.id:
                sync_resp = parse_frame(await asyncio.wait_for(ws.recv(), timeout=2.0))
            assert sync_resp.ok is True

            # Normal mismatched voice.end_call (not transcript_sync/consolidate) must still fail
            bad_hangup = VoiceEndCallRequest(
                params=VoiceEndCallParams(call_id="wrong-call-id", reason="user_hangup")
            )
            await ws.send(bad_hangup.model_dump_json())
            bad_resp = parse_frame(await asyncio.wait_for(ws.recv(), timeout=2.0))
            while not isinstance(bad_resp, ResponseFrame) or bad_resp.id != bad_hangup.id:
                bad_resp = parse_frame(await asyncio.wait_for(ws.recv(), timeout=2.0))
            assert bad_resp.ok is False
            assert daemon._voice_call_id == active_call_id

            # Path-traversal call_id in transcript_sync must be rejected
            traversal_req = VoiceEndCallRequest(
                params=VoiceEndCallParams(
                    call_id="../evil",
                    reason="transcript_sync",
                    transcript=[
                        {
                            "role": "user",
                            "text": "hello",
                            "timestamp": "2026-11-14T10:00:00+00:00",
                        }
                    ],
                )
            )
            await ws.send(traversal_req.model_dump_json())
            trav_resp = parse_frame(await asyncio.wait_for(ws.recv(), timeout=2.0))
            while not isinstance(trav_resp, ResponseFrame) or trav_resp.id != traversal_req.id:
                trav_resp = parse_frame(await asyncio.wait_for(ws.recv(), timeout=2.0))
            assert trav_resp.ok is False

            # Consolidate for the earlier call succeeds
            cons_req = VoiceEndCallRequest(
                params=VoiceEndCallParams(call_id="voice-prev-call", reason="consolidate")
            )
            await ws.send(cons_req.model_dump_json())
            cons_resp = parse_frame(await asyncio.wait_for(ws.recv(), timeout=2.0))
            while not isinstance(cons_resp, ResponseFrame) or cons_resp.id != cons_req.id:
                cons_resp = parse_frame(await asyncio.wait_for(ws.recv(), timeout=2.0))
            assert cons_resp.ok is True

            # Wait for consolidation to finish
            await _wait_until(lambda: daemon.consolidation.is_consolidated("voice-prev-call"))
    finally:
        await daemon.stop()


def test_get_conversation_preserves_consecutive_and_trailing_user_turns(tmp_path: Path) -> None:
    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()

    mem.append_transcript_message(
        session_id="voice-user-turns",
        role="user",
        text="First question",
        title="First question",
        timestamp=datetime.fromisoformat("2026-11-14T10:00:00+00:00"),
    )
    mem.append_transcript_message(
        session_id="voice-user-turns",
        role="user",
        text="Second follow-up",
        timestamp=datetime.fromisoformat("2026-11-14T10:00:05+00:00"),
    )
    mem.append_transcript_message(
        session_id="voice-user-turns",
        role="assistant",
        text="Reply to second",
        timestamp=datetime.fromisoformat("2026-11-14T10:00:10+00:00"),
    )
    mem.append_transcript_message(
        session_id="voice-user-turns",
        role="user",
        text="Trailing interrupted query",
        timestamp=datetime.fromisoformat("2026-11-14T10:00:15+00:00"),
    )

    turns = mem.get_conversation("2026-11-14", "voice-user-turns")
    assert len(turns) == 3
    assert turns[0]["user"] == "First question"
    assert turns[0]["assistant"] == ""
    assert turns[1]["user"] == "Second follow-up"
    assert turns[1]["assistant"] == "Reply to second"
    assert turns[2]["user"] == "Trailing interrupted query"
    assert turns[2]["assistant"] == ""


async def test_voice_runner_transitions_idle_before_slow_hooks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import clea.surfaces.voice.runner as runner_mod

    backend = FakeLiveBackend()
    events: list[Any] = []
    hook_entered = asyncio.Event()
    release_hook = asyncio.Event()
    idle_before_hook = False

    async def _slow_store(**kwargs: Any) -> bool:
        nonlocal idle_before_hook
        idle_before_hook = (
            runner.call_state == "idle"
            and runner.voice_state == "idle"
            and len(runner._transcript_history) == 0
            and any(getattr(ev, "event", None) == "call_ended" for ev in events)
        )
        hook_entered.set()
        await release_hook.wait()
        return True

    async def _noop_consolidate(**kwargs: Any) -> bool:
        return True

    monkeypatch.setattr(runner_mod, "safe_store_voice_transcript", _slow_store)
    monkeypatch.setattr(runner_mod, "safe_consolidate_voice_call", _noop_consolidate)

    runner = VoiceRunner(
        config=VoiceConfig(silence_timeout_seconds=60.0),
        data_dir=tmp_path,
        daemon_url="ws://127.0.0.1:1",
        token="offline-token",
        backend_factory=lambda: backend,
        audio_manager=_make_fake_audio(),
        wake_word_detector=_make_fake_wake_word(),
        enable_hotkey=False,
        on_event=events.append,
    )
    await runner.start()
    try:
        await runner.start_call(trigger="hotkey")
        await backend.event_queue.put(
            TranscriptOutputEvent(role="user", text="Hi Clea", final=True)
        )
        await backend.event_queue.put(TurnCompleteEvent())
        await _wait_until(lambda: len(runner._transcript_history) >= 1)

        end_task = asyncio.create_task(runner.end_call(reason="user_hangup"))
        await asyncio.wait_for(hook_entered.wait(), timeout=2.0)
        assert idle_before_hook is True
        release_hook.set()
        await asyncio.wait_for(end_task, timeout=2.0)
    finally:
        await runner.stop()


async def test_daemon_stop_reconciles_running_tasks_and_env_timeout(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("CLEA_CONSOLIDATION_TIMEOUT_SECONDS", "invalid-float")
    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()
    manager = ConsolidationManager(memory=mem, provider=FakeConsolidationProvider())
    assert manager.llm_timeout_seconds == 60.0

    monkeypatch.setenv("CLEA_CONSOLIDATION_TIMEOUT_SECONDS", "12.5")
    manager2 = ConsolidationManager(memory=mem, provider=FakeConsolidationProvider())
    assert manager2.llm_timeout_seconds == 12.5

    daemon_mem = MemoryStore(base_dir=tmp_path / "daemon")
    daemon_mem.ensure_initialized()
    daemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=FakeConsolidationProvider(),
        memory=daemon_mem,
    )
    await daemon.start()
    daemon.agent.memory.record_background_task(
        task_id="cons-mid-shutdown",
        title="Summarising session 14:30",
        status="running",
        started_at="2026-11-14T14:30:00+00:00",
        session_id="mid-shutdown",
    )
    await daemon.stop()
    tasks = daemon.agent.memory.load_background_tasks()
    assert tasks["cons-mid-shutdown"]["status"] == "failed"
    assert "shutdown" in (tasks["cons-mid-shutdown"].get("detail") or "").lower()


async def test_standalone_memory_hook_rpcs_work_without_request_fn(tmp_path: Path) -> None:
    from clea.surfaces.voice.memory_hooks import (
        consolidate_voice_call,
        load_voice_memory_context,
        store_voice_transcript,
    )

    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()
    mem.add_or_update_entry(
        text="User works on Clea voice hooks.",
        section="Projects",
        source="stated",
    )
    daemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=FakeConsolidationProvider(),
        memory=mem,
        token_file=auth.token_path(tmp_path),
        lock_file=auth.lock_path(tmp_path),
    )
    daemon.consolidation.min_user_turns = 2
    await daemon.start()
    try:
        # 1. Explicit daemon_url + token without request_fn
        ctx = await load_voice_memory_context(
            daemon_url=f"ws://127.0.0.1:{daemon.bound_port}",
            token=daemon.token,
        )
        assert "User works on Clea voice hooks." in ctx

        # 2. Discovery purely from data_dir lock/token files (no daemon_url or token passed)
        entries = [
            {
                "role": "user",
                "text": "First standalone voice turn",
                "timestamp": "2026-11-14T11:00:00+00:00",
                "final": True,
            },
            {
                "role": "assistant",
                "text": "First standalone reply",
                "timestamp": "2026-11-14T11:00:02+00:00",
                "final": True,
            },
            {
                "role": "user",
                "text": "Second standalone voice turn",
                "timestamp": "2026-11-14T11:00:04+00:00",
                "final": True,
            },
            {
                "role": "assistant",
                "text": "Second standalone reply",
                "timestamp": "2026-11-14T11:00:06+00:00",
                "final": True,
            },
        ]
        stored = await store_voice_transcript(
            call_id="standalone-voice-1",
            entries=entries,
            data_dir=tmp_path,
        )
        assert stored is True
        tr = mem.get_transcript("standalone-voice-1")
        assert tr is not None
        assert len(tr["messages"]) == 4

        triggered = await consolidate_voice_call(
            call_id="standalone-voice-1",
            entries=entries,
            data_dir=tmp_path,
        )
        assert triggered is True
        await _wait_until(lambda: daemon.consolidation.is_consolidated("standalone-voice-1"))
    finally:
        await daemon.stop()

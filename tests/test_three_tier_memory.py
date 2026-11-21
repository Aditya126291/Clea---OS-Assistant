"""Comprehensive unit and end-to-end tests for Clea's 3-tier memory bank."""

from __future__ import annotations

import asyncio
import contextlib
import json
import sqlite3
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from websockets.asyncio.client import connect

from clea.core import auth
from clea.core.agent import AgentLoop
from clea.core.daemon import CleaDaemon, build_daemon
from clea.core.protocol import (
    BaseEvent,
    MemoryListFactsParams,
    MemoryListFactsRequest,
    MemoryManageFactParams,
    MemoryManageFactRequest,
    MemorySearchEpisodicParams,
    MemorySearchEpisodicRequest,
    MemoryUpdatedEvent,
    ResponseFrame,
    VoiceStartCallParams,
    VoiceStartCallRequest,
    parse_frame,
)
from clea.core.sessions import Session
from clea.memory import (
    EPISODIC_SUMMARY_SYSTEM_PROMPT,
    FactBankReconciler,
    MemoryDatabase,
    MemoryStore,
    ThreeTierMemoryBank,
    WorkingMemoryManager,
    is_synthetic_episodic_session,
    sanitize_fts5_query,
)
from clea.memory.consolidation import CONSOLIDATION_SYSTEM_PROMPT
from clea.memory.database import is_secret_or_sensitive
from clea.memory.facts import parse_explicit_fact_commands
from clea.memory.store import VOICE_COMPANION_SESSION_ID
from clea.memory.working import WORKING_SUMMARY_PREFIX, WORKING_SUMMARY_SYSTEM_PROMPT
from clea.providers.base import LLMProvider, Message, StreamChunk, ToolCall
from clea.surfaces.voice.audio import AudioManager
from clea.surfaces.voice.backends.base import (
    ToolCallEvent,
    VoiceBackend,
    VoiceBackendEvent,
    VoiceSessionConfig,
    VoiceToolResponse,
)
from clea.surfaces.voice.config import VoiceConfig
from clea.surfaces.voice.memory_hooks import (
    index_voice_call_episode,
    load_voice_memory_context,
    resolve_voice_system_instruction,
)
from clea.surfaces.voice.runner import VoiceRunner
from clea.surfaces.voice.screen_indicator import ScreenIndicator
from clea.surfaces.voice.wake_word import WakeWordDetector
from clea.tools import AuditLogger, RiskLevel, ToolDefinition, ToolRegistry
from clea.tools.memory_tools import register_memory_tools


class _FakeSummaryLLMProvider(LLMProvider):
    """Deterministic fake cloud LLM provider for working-memory compaction tests."""

    provider_name = "fake-cloud"

    def __init__(
        self,
        *,
        summary_text: str = "- Summarized earlier context.",
        error: Exception | None = None,
        delay_seconds: float = 0.0,
    ) -> None:
        self._summary_text = summary_text
        self._error = error
        self._delay_seconds = delay_seconds
        self.calls: list[dict[str, Any]] = []

    @property
    def model_name(self) -> str:
        return "fake-gemini-cloud"

    async def stream_turn(
        self,
        *,
        messages: list[Message],
        tools: list[ToolDefinition],
        system_prompt: str,
        response_format: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        self.calls.append(
            {
                "messages": [m.model_copy() for m in messages],
                "tools": list(tools),
                "system_prompt": system_prompt,
                "response_format": response_format,
            }
        )
        if self._delay_seconds > 0:
            await asyncio.sleep(self._delay_seconds)
        if self._error is not None:
            raise self._error
        yield StreamChunk(type="text_delta", text=self._summary_text)
        yield StreamChunk(type="done")


class _FakeReconcilerLLMProvider(LLMProvider):
    """Deterministic fake structured-JSON cloud LLM provider for Tier 3 reconciliation."""

    provider_name = "fake-cloud"

    def __init__(
        self,
        responses: list[str] | None = None,
        *,
        error: Exception | None = None,
        delay_seconds: float = 0.0,
    ) -> None:
        self._responses = list(responses or [])
        self._error = error
        self._delay_seconds = delay_seconds
        self.calls: list[dict[str, Any]] = []

    @property
    def model_name(self) -> str:
        return "fake-gemini-cloud"

    async def stream_turn(
        self,
        *,
        messages: list[Message],
        tools: list[ToolDefinition],
        system_prompt: str,
        response_format: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        self.calls.append(
            {
                "messages": [m.model_copy() for m in messages],
                "tools": list(tools),
                "system_prompt": system_prompt,
                "response_format": response_format,
            }
        )
        if self._delay_seconds > 0:
            await asyncio.sleep(self._delay_seconds)
        if self._error is not None:
            raise self._error
        payload = self._responses.pop(0) if self._responses else '{"decisions": []}'
        yield StreamChunk(type="text_delta", text=payload)
        yield StreamChunk(type="done")


class _FakeAgentProvider(LLMProvider):
    """Programmable fake provider recording every prompt and message history seen."""

    provider_name = "fake-cloud"

    def __init__(
        self,
        responder: Callable[[list[Message], str], list[StreamChunk]] | None = None,
    ) -> None:
        self._responder = responder
        self.recorded_calls: list[tuple[list[Message], str]] = []

    @property
    def model_name(self) -> str:
        return "fake-gemini-cloud"

    async def stream_turn(
        self,
        *,
        messages: list[Message],
        tools: list[ToolDefinition],
        system_prompt: str,
        response_format: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        snapshot = [m.model_copy(deep=True) for m in messages]
        self.recorded_calls.append((snapshot, system_prompt))
        if self._responder is not None:
            chunks = self._responder(snapshot, system_prompt)
            for chunk in chunks:
                yield chunk
            return
        yield StreamChunk(type="text_delta", text="Acknowledged.")
        yield StreamChunk(type="done")


class _FakeVoiceBackend(VoiceBackend):
    """Controllable in-memory VoiceBackend for end-to-end VoiceRunner tests."""

    backend_name = "fake-live"

    def __init__(self) -> None:
        self.connected_config: VoiceSessionConfig | None = None
        self.closed = False
        self.sent_tool_responses: list[VoiceToolResponse] = []
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
        self.sent_tool_responses.extend(responses)

    async def events(self) -> AsyncIterator[VoiceBackendEvent]:
        while True:
            evt = await self.event_queue.get()
            if evt is None:
                break
            yield evt


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


async def _wait_until(predicate: Callable[[], bool], *, timeout: float = 4.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while not predicate():
        if asyncio.get_running_loop().time() >= deadline:
            raise TimeoutError("Condition was not satisfied before timeout.")
        await asyncio.sleep(0.02)


async def _ws_rpc(ws: Any, req: Any) -> tuple[ResponseFrame, list[BaseEvent]]:
    await ws.send(req.model_dump_json())
    events: list[BaseEvent] = []
    while True:
        frame = parse_frame(await asyncio.wait_for(ws.recv(), timeout=5.0))
        if isinstance(frame, BaseEvent):
            events.append(frame)
        elif isinstance(frame, ResponseFrame) and frame.id == req.id:
            return frame, events


def _assert_tool_pairing_valid(messages: list[Message]) -> None:
    """Ensure no tool message or tool_call is orphaned across compacted messages."""
    pending_tool_ids: set[str] = set()
    for msg in messages:
        if msg.role == "assistant" and msg.tool_calls:
            pending_tool_ids.update(tc.id for tc in msg.tool_calls if tc.id)
        elif msg.role == "tool":
            assert msg.tool_call_id is not None
            assert msg.tool_call_id in pending_tool_ids, (
                f"Orphaned tool result {msg.tool_call_id!r} without preceding tool_call"
            )
            pending_tool_ids.discard(msg.tool_call_id)
        elif msg.role == "user":
            assert not pending_tool_ids, (
                f"Unresolved tool_calls before user turn: {pending_tool_ids}"
            )
    assert not pending_tool_ids, f"Trailing unresolved tool_calls: {pending_tool_ids}"


# ---------------------------------------------------------------------------
# 1. SQLite WAL & Schema
# ---------------------------------------------------------------------------


def test_sqlite_wal_schema_and_fts5_trigger_sync(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    db = MemoryDatabase(db_path)
    try:
        db.ensure_schema()
        assert db_path.exists()

        with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
            journal_mode = conn.execute("PRAGMA journal_mode;").fetchone()[0]
            assert str(journal_mode).lower() == "wal"

            tables = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type IN ('table', 'view')"
                ).fetchall()
            }
            assert {"episodic_sessions", "episodic_sessions_fts", "memory_facts"} <= tables

            triggers = {
                row[0]
                for row in conn.execute(
                    "SELECT name FROM sqlite_master WHERE type = 'trigger'"
                ).fetchall()
            }
            assert {
                "episodic_sessions_ai",
                "episodic_sessions_ad",
                "episodic_sessions_au",
            } <= triggers

        # Insert trigger sync
        db.upsert_episodic_session(
            session_id="sync-sess-1",
            surface="chat",
            title="Quantum Circuit Planner",
            summary="Designed qubit gate decomposition pipeline.",
            topics="quantum, gates, compiler",
        )
        with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
            conn.execute(
                "INSERT INTO episodic_sessions_fts(episodic_sessions_fts) VALUES('integrity-check')"
            )
            hits = conn.execute(
                "SELECT session_id FROM episodic_sessions_fts WHERE episodic_sessions_fts MATCH ?",
                ('"quantum"',),
            ).fetchall()
            assert [r[0] for r in hits] == ["sync-sess-1"]

        # Update trigger sync (old tokens removed, new tokens indexed)
        db.upsert_episodic_session(
            session_id="sync-sess-1",
            surface="chat",
            title="Classical Register Allocator",
            summary="Switched to linear scan register allocation.",
            topics="compiler, registers",
        )
        with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
            conn.execute(
                "INSERT INTO episodic_sessions_fts(episodic_sessions_fts) VALUES('integrity-check')"
            )
            old_hits = conn.execute(
                "SELECT session_id FROM episodic_sessions_fts WHERE episodic_sessions_fts MATCH ?",
                ('"quantum"',),
            ).fetchall()
            new_hits = conn.execute(
                "SELECT session_id FROM episodic_sessions_fts WHERE episodic_sessions_fts MATCH ?",
                ('"registers"',),
            ).fetchall()
            assert old_hits == []
            assert [r[0] for r in new_hits] == ["sync-sess-1"]

        # Delete trigger sync
        assert db.delete_episodic_session("sync-sess-1") is True
        with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
            conn.execute(
                "INSERT INTO episodic_sessions_fts(episodic_sessions_fts) VALUES('integrity-check')"
            )
            after_del = conn.execute(
                "SELECT session_id FROM episodic_sessions_fts WHERE episodic_sessions_fts MATCH ?",
                ('"registers"',),
            ).fetchall()
            assert after_del == []
    finally:
        db.close()


# ---------------------------------------------------------------------------
# 2. Tier 1: Working Memory
# ---------------------------------------------------------------------------


def test_working_memory_artifact_ttl_and_small_output() -> None:
    mgr = WorkingMemoryManager()
    assert mgr.artifact_char_threshold == 1500
    assert mgr.artifact_ttl_turns == 3
    assert mgr.keep_recent_turns == 6

    large_payload = "UI_SNAPSHOT_NODE " * 120  # > 1500 chars
    assert len(large_payload) > 1500
    large_msg = Message(
        role="tool",
        tool_name="ui_snapshot",
        tool_call_id="call-ui-1",
        content=large_payload,
    )
    small_msg = Message(
        role="tool",
        tool_name="get_system_info",
        tool_call_id="call-sys-1",
        content='{"os": "Windows 11"}',
    )

    assert mgr.tag_tool_artifact(large_msg, turn_number=1) is True
    assert mgr.tag_tool_artifact(small_msg, turn_number=1) is False

    messages = [
        Message(role="user", content="Take a snapshot"),
        Message(
            role="assistant",
            content="",
            tool_calls=[
                ToolCall(id="call-ui-1", name="ui_snapshot"),
                ToolCall(id="call-sys-1", name="get_system_info"),
            ],
        ),
        large_msg,
        small_msg,
        Message(role="assistant", content="Captured snapshot."),
    ]

    # Within TTL (< 3 turns elapsed): turns 1, 2, 3 remain intact
    for turn in (1, 2, 3):
        assert mgr.expire_artifacts(messages, current_turn=turn) == 0
        assert large_msg.artifact_expired is False
        assert large_msg.content == large_payload
        assert small_msg.artifact_expired is False
        assert small_msg.content == '{"os": "Windows 11"}'

    # Once >= 3 turns pass (turn 4 - turn 1 == 3): large ui_snapshot expires, small stays intact
    assert mgr.expire_artifacts(messages, current_turn=4) == 1
    assert large_msg.artifact_expired is True
    assert (
        large_msg.content
        == "[Expired tool artifact: ui_snapshot from turn 1 — re-run if current state needed]"
    )
    assert small_msg.artifact_expired is False
    assert small_msg.content == '{"os": "Windows 11"}'

    # Idempotent on subsequent turns
    assert mgr.expire_artifacts(messages, current_turn=5) == 0


async def test_working_memory_rolling_compaction_budgets_and_turn_boundaries() -> None:
    provider = _FakeSummaryLLMProvider(
        summary_text="- User investigated auth module.\n- Tool read_file inspected auth.py."
    )
    mgr = WorkingMemoryManager(
        max_messages=16,
        max_chars=24000,
        keep_recent_turns=6,
        provider=provider,
    )

    # Build 8 turns (turns 1..2 should be compacted; turns 3..8 preserved verbatim)
    messages: list[Message] = []
    # Turn 1 (plain)
    messages.extend(
        [
            Message(role="user", content="Turn 1: check auth config"),
            Message(role="assistant", content="Checking auth config now."),
        ]
    )
    # Turn 2 (with tool call & tool result inside older turns)
    messages.extend(
        [
            Message(role="user", content="Turn 2: read auth.py"),
            Message(
                role="assistant",
                content="",
                tool_calls=[
                    ToolCall(id="tc-old-2", name="read_file", arguments={"path": "auth.py"})
                ],
            ),
            Message(
                role="tool",
                tool_call_id="tc-old-2",
                tool_name="read_file",
                content="def read_token(): return 'ok'",
            ),
            Message(role="assistant", content="Read auth.py successfully."),
        ]
    )
    # Turns 3..8 (recent 6 turns, including tool calls in turns 4 and 7)
    for t in range(3, 9):
        messages.append(Message(role="user", content=f"Turn {t}: continue step {t}"))
        if t in (4, 7):
            call_id = f"tc-recent-{t}"
            messages.append(
                Message(
                    role="assistant",
                    content="",
                    tool_calls=[ToolCall(id=call_id, name="get_system_info", arguments={})],
                )
            )
            messages.append(
                Message(
                    role="tool",
                    tool_call_id=call_id,
                    tool_name="get_system_info",
                    content=f"status ok for turn {t}",
                )
            )
        messages.append(Message(role="assistant", content=f"Completed step {t}."))

    assert len(messages) > 16
    assert mgr.needs_compaction(messages) is True

    compacted, summary = await mgr.compact_if_needed(messages)
    assert summary is not None
    assert "User investigated auth module." in summary
    assert len(provider.calls) == 1
    assert provider.calls[0]["system_prompt"] == WORKING_SUMMARY_SYSTEM_PROMPT

    assert compacted[0].role == "user"
    assert compacted[0].content.startswith(WORKING_SUMMARY_PREFIX)
    assert "User investigated auth module." in compacted[0].content

    # Most recent 6 turns (starting at Turn 3 user message) preserved verbatim
    turn3_idx = next(
        idx
        for idx, m in enumerate(messages)
        if m.role == "user" and m.content.startswith("Turn 3:")
    )
    expected_recent = messages[turn3_idx:]
    assert compacted[1:] == expected_recent
    _assert_tool_pairing_valid(compacted)

    # Also verify char-budget trigger when message count is below max_messages
    char_provider = _FakeSummaryLLMProvider(summary_text="- Compacted due to char budget.")
    char_mgr = WorkingMemoryManager(
        max_messages=200,
        max_chars=1200,
        keep_recent_turns=6,
        provider=char_provider,
    )
    char_msgs = [
        m.model_copy(
            update={"content": f"{m.content} " + ("x" * 180)} if m.role == "user" else {}
        )
        for m in messages
    ]
    assert char_mgr.needs_compaction(char_msgs) is True
    char_compacted, char_summary = await char_mgr.compact_if_needed(char_msgs)
    assert char_summary == "- Compacted due to char budget."
    assert char_compacted[0].content.startswith(WORKING_SUMMARY_PREFIX)
    assert len([m for m in char_compacted[1:] if m.role == "user"]) == 6
    _assert_tool_pairing_valid(char_compacted)


async def test_working_memory_compaction_extractive_fallback_on_failure_and_timeout() -> None:
    def _build_turns() -> list[Message]:
        msgs: list[Message] = []
        for idx in range(1, 9):
            msgs.append(Message(role="user", content=f"Question {idx} about memory compaction"))
            msgs.append(Message(role="assistant", content=f"Answer {idx} details"))
        return msgs

    messages = _build_turns()

    # 1. Provider exception fallback
    failing_provider = _FakeSummaryLLMProvider(error=RuntimeError("503 service unavailable"))
    mgr_fail = WorkingMemoryManager(
        max_messages=10,
        keep_recent_turns=6,
        provider=failing_provider,
    )
    compacted_fail, summary_fail = await mgr_fail.compact_if_needed(messages)
    assert summary_fail is not None
    assert compacted_fail[0].content.startswith(WORKING_SUMMARY_PREFIX)
    assert "Question 1 about memory compaction" in compacted_fail[0].content
    assert "Question 2 about memory compaction" in compacted_fail[0].content
    assert compacted_fail[1:] == messages[4:]  # Turns 3..8 preserved verbatim

    # 2. Provider timeout fallback
    slow_provider = _FakeSummaryLLMProvider(delay_seconds=0.5)
    mgr_timeout = WorkingMemoryManager(
        max_messages=10,
        keep_recent_turns=6,
        llm_timeout_seconds=0.05,
        provider=slow_provider,
    )
    compacted_to, summary_to = await mgr_timeout.compact_if_needed(messages)
    assert summary_to is not None
    assert compacted_to[0].content.startswith(WORKING_SUMMARY_PREFIX)
    assert "Question 1 about memory compaction" in compacted_to[0].content
    assert compacted_to[1:] == messages[4:]


async def test_agent_loop_run_turn_artifact_ttl_and_rolling_compaction_e2e(
    tmp_path: Path,
) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()
    try:
        registry = ToolRegistry(audit_logger=AuditLogger(tmp_path / "logs" / "audit.jsonl"))
        register_memory_tools(registry, store)

        snapshot_payload = "BUTTON_REF_E1 " * 140  # > 1500 chars
        assert len(snapshot_payload) > 1500
        registry.register(
            name="ui_snapshot",
            description="Capture UI Automation snapshot.",
            input_schema={"type": "object", "properties": {}, "additionalProperties": False},
            risk_level=RiskLevel.READ_ONLY,
            handler=lambda: snapshot_payload,
        )

        def _agent_responder(messages: list[Message], system_prompt: str) -> list[StreamChunk]:
            last = messages[-1]
            if last.role == "user" and "take ui snapshot" in last.content.lower():
                return [
                    StreamChunk(
                        type="tool_call",
                        tool_call=ToolCall(id="snap-1", name="ui_snapshot", arguments={}),
                    ),
                    StreamChunk(type="done"),
                ]
            if last.role == "tool" and last.tool_name == "ui_snapshot":
                return [
                    StreamChunk(type="text_delta", text="Snapshot captured."),
                    StreamChunk(type="done"),
                ]
            return [
                StreamChunk(type="text_delta", text=f"Handled: {last.content}"),
                StreamChunk(type="done"),
            ]

        provider = _FakeAgentProvider(_agent_responder)
        summary_provider = _FakeSummaryLLMProvider(
            summary_text="- Turn 1 captured ui_snapshot.\n- Turn 2 discussed layout."
        )
        working_mgr = WorkingMemoryManager(
            artifact_char_threshold=1500,
            artifact_ttl_turns=3,
            max_messages=16,
            max_chars=24000,
            keep_recent_turns=6,
            provider=summary_provider,
        )
        loop = AgentLoop(
            provider=provider,
            registry=registry,
            memory=store,
            max_history_messages=24,
            working_memory=working_mgr,
        )
        session = Session(id="agent-tier1-e2e")

        # Turn 1: invokes ui_snapshot (>1500 chars) -> tagged at turn_index=1
        reply1 = await loop.run_turn(session=session, user_text="Please take UI snapshot")
        assert reply1 == "Snapshot captured."
        assert session.turn_count == 1

        # Turn 2 & Turn 3: artifact stays intact in messages sent to provider
        await loop.run_turn(session=session, user_text="Turn 2 check elements")
        turn2_msgs, _ = provider.recorded_calls[-1]
        turn2_tool = next(m for m in turn2_msgs if m.role == "tool")
        assert turn2_tool.content == snapshot_payload
        assert turn2_tool.artifact_expired is False

        await loop.run_turn(session=session, user_text="Turn 3 check layout")
        turn3_msgs, _ = provider.recorded_calls[-1]
        turn3_tool = next(m for m in turn3_msgs if m.role == "tool")
        assert turn3_tool.content == snapshot_payload
        assert turn3_tool.artifact_expired is False

        # Turn 4: 4 - 1 == 3 >= TTL -> ui_snapshot expired BEFORE provider call
        await loop.run_turn(session=session, user_text="Turn 4 verify expiration")
        turn4_msgs, _ = provider.recorded_calls[-1]
        turn4_tool = next(m for m in turn4_msgs if m.role == "tool")
        assert turn4_tool.artifact_expired is True
        assert (
            turn4_tool.content
            == "[Expired tool artifact: ui_snapshot from turn 1 — re-run if current state needed]"
        )

        # Turns 5..8 push message count past max_messages=16 -> triggers rolling compaction
        for t in range(5, 9):
            await loop.run_turn(session=session, user_text=f"Turn {t} continuing task")

        assert len(summary_provider.calls) == 1
        assert "Turn 1 captured ui_snapshot." in session.working_summary
        assert session.messages[0].role == "user"
        assert session.messages[0].content.startswith(WORKING_SUMMARY_PREFIX)
        recent_user_turns = [
            m.content for m in session.messages[1:] if m.role == "user"
        ]
        assert recent_user_turns == [
            "Turn 3 check layout",
            "Turn 4 verify expiration",
            "Turn 5 continuing task",
            "Turn 6 continuing task",
            "Turn 7 continuing task",
            "Turn 8 continuing task",
        ]
        _assert_tool_pairing_valid(session.messages)

        # Compacted working summary was also indexed in Tier 2 episodic FTS5
        ep_hits = store.search_episodic_memory("ui_snapshot")
        assert any(h["session_id"] == "agent-tier1-e2e" for h in ep_hits)
    finally:
        store.close()


# ---------------------------------------------------------------------------
# 3. Tier 2: Episodic FTS5 Memory
# ---------------------------------------------------------------------------


def test_episodic_fts5_indexing_chat_and_voice_update_and_ranking(tmp_path: Path) -> None:
    db_path = tmp_path / "memory.db"
    bank = ThreeTierMemoryBank(db_path)
    try:
        bank.ensure_schema()

        # Index a chat session via ThreeTierMemoryBank
        bank.index_session_messages(
            session_id="chat-ep-01",
            surface="chat",
            started_at="2026-11-20T09:00:00Z",
            ended_at="2026-11-20T09:15:00Z",
            fallback_title="PostgreSQL Schema Migration",
            messages=[
                Message(role="user", content="Help write an alembic migration for PostgreSQL."),
                Message(role="assistant", content="Generated PostgreSQL migration scripts."),
            ],
        )

        # Index a voice session via voice memory_hooks.index_voice_call_episode
        index_voice_call_episode(
            db_or_store=bank,
            call_id="voice-ep-01",
            title="Kubernetes Rollout Plan",
            summary="Reviewed kubernetes helm chart canary rollout and pod readiness probes.",
            topics="kubernetes, helm, canary, rollout",
            entries=[
                {
                    "role": "user",
                    "text": "Let us plan the kubernetes helm chart rollout.",
                    "timestamp": "2026-11-20T10:00:00Z",
                },
                {
                    "role": "assistant",
                    "text": "Sure, configure kubernetes readiness probes first.",
                    "timestamp": "2026-11-20T10:01:00Z",
                },
            ],
        )

        # Index a weaker chat mention of kubernetes (body-only, generic title)
        bank.upsert_episodic_session(
            session_id="chat-ep-02",
            surface="chat",
            started_at="2026-11-20T11:00:00Z",
            ended_at="2026-11-20T11:05:00Z",
            title="Morning Standup Notes",
            summary="Briefly mentioned kubernetes cluster upgrade next quarter.",
            topics="standup, schedule",
        )

        # Ranked results: title+topics+summary match ranks above weak summary-only match
        ranked = bank.search_episodic("kubernetes", limit=5)
        assert [hit["session_id"] for hit in ranked] == ["voice-ep-01", "chat-ep-02"]
        for hit in ranked:
            for key in ("session_id", "surface", "timestamp", "title", "summary", "snippet"):
                assert key in hit
                assert str(hit[key]).strip() != ""

        # Surface filter
        voice_only = bank.search_episodic("kubernetes", surface="voice")
        assert [h["session_id"] for h in voice_only] == ["voice-ep-01"]
        chat_only = bank.search_episodic("kubernetes", surface="chat")
        assert [h["session_id"] for h in chat_only] == ["chat-ep-02"]

        # Update existing session_id cleanly without FTS5 drift
        bank.upsert_episodic_session(
            session_id="chat-ep-01",
            surface="chat",
            title="Redis Sentinel Failover",
            summary="Configured Redis sentinel quorum and cache eviction.",
            topics="redis, sentinel, caching",
        )
        assert bank.search_episodic("alembic") == []
        assert bank.search_episodic("PostgreSQL") == []
        redis_hits = bank.search_episodic("Redis")
        assert [h["session_id"] for h in redis_hits] == ["chat-ep-01"]

        with contextlib.closing(sqlite3.connect(str(db_path))) as conn:
            row_count = conn.execute(
                "SELECT COUNT(*) FROM episodic_sessions WHERE session_id = ?",
                ("chat-ep-01",),
            ).fetchone()[0]
            assert row_count == 1
            conn.execute(
                "INSERT INTO episodic_sessions_fts(episodic_sessions_fts) VALUES('integrity-check')"
            )
    finally:
        bank.close()


@pytest.mark.parametrize(
    "raw_query",
    [
        'AND OR NOT NEAR "foo:bar" (test*) --- !!! ???',
        '"""""',
        "--- !!! ???",
        "title:secret NEAR/2 (foo* OR bar)",
        "(unclosed [brackets] {braces} : ; ^ ~ * - +)",
        'foo"bar:baz-qux',
        "C++ / C# async-await",
        "",
        "   ",
    ],
)
def test_episodic_fts5_sanitization_edge_cases(tmp_path: Path, raw_query: str) -> None:
    db = MemoryDatabase(tmp_path / "memory.db")
    try:
        db.ensure_schema()
        db.upsert_episodic_session(
            session_id="edge-1",
            surface="chat",
            title="Foo Bar Test Session",
            summary="Debugging foo:bar test cases and error codes.",
            topics="foo, bar, test",
        )

        sanitized = sanitize_fts5_query(raw_query)
        assert isinstance(sanitized, str)
        results = db.search_episodic(raw_query, limit=5)
        assert isinstance(results, list)
        if not raw_query.strip():
            assert results == []
        if "foo" in raw_query.lower():
            assert any(hit["session_id"] == "edge-1" for hit in results)
    finally:
        db.close()


async def test_episodic_search_e2e_chat_daemon_and_voice(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()
    store.index_episodic_session(
        session_id="chat-ep-ws",
        surface="chat",
        started_at="2026-11-20T08:00:00Z",
        ended_at="2026-11-20T08:10:00Z",
        title="WebSocket Subprotocol Auth",
        summary="Configured bearer subprotocol token validation.",
        topics="websocket, bearer, auth",
    )
    store.index_episodic_session(
        session_id="voice-ep-wal",
        surface="voice",
        started_at="2026-11-20T09:00:00Z",
        ended_at="2026-11-20T09:10:00Z",
        title="SQLite WAL Tuning",
        summary="Enabled WAL journal mode with normal sync.",
        topics="sqlite, wal, tuning",
    )

    registry = ToolRegistry(audit_logger=AuditLogger(tmp_path / "logs" / "audit.jsonl"))
    register_memory_tools(registry, store)

    # 1. Chat ToolRegistry direct execution
    tool_res = await registry.execute(
        "search_episodic_memory",
        {"query": "WebSocket bearer", "limit": 5, "surface": "chat"},
    )
    assert tool_res["count"] == 1
    assert tool_res["results"][0]["session_id"] == "chat-ep-ws"

    # 2. Chat AgentLoop end-to-end tool call
    def _chat_ep_responder(messages: list[Message], _sys: str) -> list[StreamChunk]:
        if any(m.role == "tool" and m.tool_name == "search_episodic_memory" for m in messages):
            tmsg = next(m for m in messages if m.role == "tool")
            return [
                StreamChunk(type="text_delta", text=f"Found: {tmsg.content}"),
                StreamChunk(type="done"),
            ]
        return [
            StreamChunk(
                type="tool_call",
                tool_call=ToolCall(
                    id="call-ep-1",
                    name="search_episodic_memory",
                    arguments={"query": "SQLite WAL", "surface": "voice"},
                ),
            ),
            StreamChunk(type="done"),
        ]

    agent = AgentLoop(
        provider=_FakeAgentProvider(_chat_ep_responder),
        registry=registry,
        memory=store,
    )
    reply = await agent.run_turn(
        session=Session(id="chat-ep-call"),
        user_text="Recall SQLite tuning",
    )
    assert "voice-ep-wal" in reply

    # 3. Daemon WebSocket RPC (memory.search_episodic) & VoiceRunner tool execution
    daemon: CleaDaemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=_FakeAgentProvider(),
        memory=store,
        registry=registry,
    )
    await daemon.start()
    url = f"ws://127.0.0.1:{daemon.bound_port}"
    backend = _FakeVoiceBackend()
    runner = VoiceRunner(
        config=VoiceConfig(silence_timeout_seconds=60.0, screen_indicator_enabled=False),
        data_dir=tmp_path,
        daemon_url=url,
        token=daemon.token,
        backend_factory=lambda: backend,
        audio_manager=_make_fake_audio(),
        wake_word_detector=_make_fake_wake_word(),
        screen_indicator=ScreenIndicator(enabled=False),
        enable_hotkey=False,
        daemon_retry_delay=0.05,
    )
    runner_task: asyncio.Task[None] | None = None
    try:
        # Daemon RPC direct test
        async with connect(url, subprotocols=auth.client_subprotocols(daemon.token)) as ws:
            resp, _ = await _ws_rpc(
                ws,
                MemorySearchEpisodicRequest(
                    params=MemorySearchEpisodicParams(query="SQLite WAL", limit=5, surface="voice")
                ),
            )
            assert resp.ok is True
            assert len(resp.result["results"]) == 1
            hit = resp.result["results"][0]
            assert hit["session_id"] == "voice-ep-wal"
            assert hit["surface"] == "voice"
            assert hit["title"] == "SQLite WAL Tuning"
            assert hit["summary"]
            assert hit["snippet"]
            assert hit["timestamp"]

        # VoiceRunner -> Daemon -> SQLite FTS5
        runner_task = asyncio.create_task(runner.run_forever())
        await _wait_until(lambda: runner._daemon_listener_active)
        await runner.start_call(trigger="manual")

        await backend.event_queue.put(
            ToolCallEvent(
                call_id="voice-tc-1",
                name="search_episodic_memory",
                arguments={"query": "WebSocket bearer", "limit": 5, "surface": "chat"},
            )
        )
        await backend.event_queue.put(
            ToolCallEvent(
                call_id="voice-tc-2",
                name="search_episodic_memory",
                arguments={"query": 'AND OR NOT NEAR "SQLite:WAL" (tuning*) --- !!!'},
            )
        )
        await _wait_until(lambda: len(backend.sent_tool_responses) >= 2)
        by_cid = {r.call_id: r for r in backend.sent_tool_responses}

        r1 = by_cid["voice-tc-1"]
        assert r1.is_error is False
        assert r1.response["results"][0]["session_id"] == "chat-ep-ws"

        r2 = by_cid["voice-tc-2"]
        assert r2.is_error is False
        assert any(item["session_id"] == "voice-ep-wal" for item in r2.response["results"])
    finally:
        await runner.stop()
        if runner_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await runner_task
        await daemon.stop()
        store.close()


# ---------------------------------------------------------------------------
# 4. Tier 3: Stateful Fact Bank & Parity
# ---------------------------------------------------------------------------


async def test_fact_bank_llm_reconciler_add_noop_update_delete(tmp_path: Path) -> None:
    db = MemoryDatabase(tmp_path / "memory.db")
    try:
        db.ensure_schema()

        # 1. ADD
        add_payload = json.dumps(
            {
                "decisions": [
                    {
                        "operation": "ADD",
                        "category": "environment",
                        "fact_key": "package_manager",
                        "fact_text": "User uses uv for Python packages.",
                        "confidence": 0.95,
                        "reason": "Explicit tooling preference",
                    }
                ]
            }
        )
        provider = _FakeReconcilerLLMProvider([add_payload])
        reconciler = FactBankReconciler(db, provider=provider)
        res_add = await reconciler.reconcile_text("I use uv for Python packages.")
        assert res_add.used_llm is True
        assert len(res_add.added_ids) == 1
        assert provider.calls[0]["response_format"]["mime_type"] == "application/json"

        fact_id = res_add.added_ids[0]
        active_after_add = db.list_facts(status="active")
        assert len(active_after_add) == 1
        assert active_after_add[0].id == fact_id
        assert active_after_add[0].category == "environment"
        assert active_after_add[0].fact_key == "package_manager"
        initial_created_at = active_after_add[0].created_at
        initial_updated_at = active_after_add[0].updated_at

        # 2. NOOP (touches updated_at without creating duplicate active rows)
        await asyncio.sleep(0.01)
        noop_payload = json.dumps(
            {
                "decisions": [
                    {
                        "operation": "NOOP",
                        "category": "environment",
                        "fact_key": "package_manager",
                        "fact_text": "User uses uv for Python packages.",
                        "target_fact_id": fact_id,
                        "confidence": 0.95,
                        "reason": "Already captured",
                    }
                ]
            }
        )
        provider._responses.append(noop_payload)
        res_noop = await reconciler.reconcile_text("Yes, I still use uv for Python packages.")
        assert res_noop.used_llm is True
        assert res_noop.noop_ids == [fact_id]
        active_after_noop = db.list_facts(status="active")
        assert len(active_after_noop) == 1
        assert active_after_noop[0].created_at == initial_created_at
        assert active_after_noop[0].updated_at > initial_updated_at

        # 3. UPDATE (marks prior row superseded and inserts new active row)
        update_payload = json.dumps(
            {
                "decisions": [
                    {
                        "operation": "UPDATE",
                        "category": "environment",
                        "fact_key": "package_manager",
                        "fact_text": "User uses uv workspaces for monorepo Python builds.",
                        "target_fact_id": fact_id,
                        "confidence": 0.96,
                        "reason": "Updated workflow detail",
                    }
                ]
            }
        )
        provider._responses.append(update_payload)
        res_upd = await reconciler.reconcile_text("Now I use uv workspaces for monorepos.")
        assert res_upd.used_llm is True
        assert res_upd.superseded_ids == [fact_id]
        assert len(res_upd.updated_ids) == 1
        new_fact_id = res_upd.updated_ids[0]
        assert new_fact_id != fact_id

        prior_row = db.get_fact(fact_id)
        assert prior_row is not None
        assert prior_row.status == "superseded"

        active_after_update = db.list_facts(status="active")
        assert len(active_after_update) == 1
        assert active_after_update[0].id == new_fact_id
        assert active_after_update[0].fact_text == (
            "User uses uv workspaces for monorepo Python builds."
        )

        # 4. DELETE (marks retracted row status='deleted')
        delete_payload = json.dumps(
            {
                "decisions": [
                    {
                        "operation": "DELETE",
                        "category": "environment",
                        "fact_key": "package_manager",
                        "fact_text": "User uses uv workspaces for monorepo Python builds.",
                        "target_fact_id": new_fact_id,
                        "confidence": 0.99,
                        "reason": "User retracted preference",
                    }
                ]
            }
        )
        provider._responses.append(delete_payload)
        res_del = await reconciler.reconcile_text("Forget what package manager I use.")
        assert res_del.used_llm is True
        assert res_del.deleted_ids == [new_fact_id]

        deleted_row = db.get_fact(new_fact_id)
        assert deleted_row is not None
        assert deleted_row.status == "deleted"
        assert db.list_facts(status="active") == []
    finally:
        db.close()


@pytest.mark.parametrize("mode", ["none_provider", "failing_provider", "timeout_provider"])
async def test_fact_bank_rule_based_fallback_remember_duplicate_forget(
    tmp_path: Path, mode: str
) -> None:
    db = MemoryDatabase(tmp_path / f"memory_{mode}.db")
    try:
        db.ensure_schema()
        provider: LLMProvider | None
        if mode == "none_provider":
            provider = None
        elif mode == "failing_provider":
            provider = _FakeReconcilerLLMProvider(error=RuntimeError("503 cloud unavailable"))
        else:
            provider = _FakeReconcilerLLMProvider(delay_seconds=0.5)

        reconciler = FactBankReconciler(db, provider=provider, llm_timeout_seconds=0.05)

        # 1. "remember that ..." -> ADD
        r_add = await reconciler.reconcile_text(
            "Remember that I prefer concise pytest tracebacks."
        )
        assert r_add.used_llm is False
        assert len(r_add.added_ids) == 1
        fact_id = r_add.added_ids[0]
        active = db.list_facts(status="active")
        assert len(active) == 1
        assert active[0].fact_text == "I prefer concise pytest tracebacks"
        first_updated_at = active[0].updated_at

        # 2. Duplicate "remember that ..." -> NOOP (touches updated_at, no duplicate)
        await asyncio.sleep(0.01)
        r_dup = await reconciler.reconcile_text(
            "Remember that I prefer concise pytest tracebacks."
        )
        assert r_dup.used_llm is False
        assert r_dup.noop_ids == [fact_id]
        active_after_dup = db.list_facts(status="active")
        assert len(active_after_dup) == 1
        assert active_after_dup[0].updated_at > first_updated_at

        # 3. "forget that ..." -> DELETE
        r_del = await reconciler.reconcile_text(
            "Forget that I prefer concise pytest tracebacks."
        )
        assert r_del.used_llm is False
        assert r_del.deleted_ids == [fact_id]
        assert db.list_facts(status="active") == []
        deleted_rec = db.get_fact(fact_id)
        assert deleted_rec is not None
        assert deleted_rec.status == "deleted"
    finally:
        db.close()


async def test_fact_bank_secret_filtering(tmp_path: Path) -> None:
    db = MemoryDatabase(tmp_path / "memory.db")
    store = MemoryStore(base_dir=tmp_path)
    try:
        store.ensure_initialized()
        secret_api = "My OpenAI api_key=sk-1234567890abcdefghijklmnop"
        secret_pwd = "password=SuperSecretPassword123"

        assert is_secret_or_sensitive(secret_api) is True
        assert is_secret_or_sensitive(secret_pwd) is True

        with pytest.raises(ValueError, match="sensitive credentials"):
            db.add_fact(secret_api, category="environment")
        with pytest.raises(ValueError, match="sensitive credentials"):
            db.manage_fact("add", fact_text=secret_pwd, category="constraint")

        assert parse_explicit_fact_commands(f"Remember that {secret_api}") == []
        assert parse_explicit_fact_commands(f"Remember that {secret_pwd}") == []

        # Even if an LLM reconciler emits a secret, execution rejects it
        leaky_payload = json.dumps(
            {
                "decisions": [
                    {
                        "operation": "ADD",
                        "category": "environment",
                        "fact_key": "leaked_key",
                        "fact_text": secret_api,
                    },
                    {
                        "operation": "ADD",
                        "category": "constraint",
                        "fact_key": "leaked_pwd",
                        "fact_text": secret_pwd,
                    },
                ]
            }
        )
        reconciler = FactBankReconciler(db, provider=_FakeReconcilerLLMProvider([leaky_payload]))
        res = await reconciler.reconcile_text("Store my credentials")
        assert res.added_ids == []
        assert db.list_facts(status=None) == []

        registry = ToolRegistry(audit_logger=AuditLogger(tmp_path / "logs" / "audit.jsonl"))
        register_memory_tools(registry, store)
        with pytest.raises(ValueError, match="sensitive credentials"):
            await registry.execute(
                "manage_memory",
                {"action": "add", "fact_text": secret_api, "category": "environment"},
                confirm=lambda _defn, _args: True,
            )
    finally:
        db.close()
        store.close()


async def test_manage_memory_tool_chat_daemon_and_voice_e2e(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()
    registry = ToolRegistry(audit_logger=AuditLogger(tmp_path / "logs" / "audit.jsonl"))
    register_memory_tools(registry, store)

    # 1. Chat ToolRegistry: add -> update -> delete
    chat_add = await registry.execute(
        "manage_memory",
        {
            "action": "add",
            "fact_text": "User runs pytest with -rs.",
            "category": "workflow",
            "fact_key": "pytest_flags",
        },
        confirm=lambda _defn, _args: True,
    )
    assert chat_add["operation"] == "add"
    chat_fact_id = chat_add["fact"]["id"]

    chat_upd = await registry.execute(
        "manage_memory",
        {
            "action": "update",
            "fact_id": chat_fact_id,
            "fact_text": "User runs pytest with -rs -q.",
            "category": "workflow",
        },
        confirm=lambda _defn, _args: True,
    )
    assert chat_upd["operation"] == "update"
    assert chat_upd["superseded_id"] == chat_fact_id
    chat_fact_id_2 = chat_upd["fact"]["id"]
    assert store.memory_db.get_fact(chat_fact_id).status == "superseded"  # type: ignore[union-attr]

    chat_del = await registry.execute(
        "manage_memory",
        {"action": "delete", "fact_id": chat_fact_id_2},
        confirm=lambda _defn, _args: True,
    )
    assert chat_del["operation"] == "delete"
    assert store.memory_db.get_fact(chat_fact_id_2).status == "deleted"  # type: ignore[union-attr]

    # 2. Daemon WebSocket RPC (memory.manage_fact & memory.list_facts) + VoiceRunner manage_memory
    daemon: CleaDaemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=_FakeAgentProvider(),
        memory=store,
        registry=registry,
    )
    await daemon.start()
    url = f"ws://127.0.0.1:{daemon.bound_port}"
    backend = _FakeVoiceBackend()
    runner = VoiceRunner(
        config=VoiceConfig(silence_timeout_seconds=60.0, screen_indicator_enabled=False),
        data_dir=tmp_path,
        daemon_url=url,
        token=daemon.token,
        backend_factory=lambda: backend,
        audio_manager=_make_fake_audio(),
        wake_word_detector=_make_fake_wake_word(),
        screen_indicator=ScreenIndicator(enabled=False),
        enable_hotkey=False,
        daemon_retry_delay=0.05,
    )
    runner_task: asyncio.Task[None] | None = None
    try:
        async with connect(url, subprotocols=auth.client_subprotocols(daemon.token)) as ws:
            # Daemon add
            r_add, ev_add = await _ws_rpc(
                ws,
                MemoryManageFactRequest(
                    params=MemoryManageFactParams(
                        action="add",
                        fact_text="User develops on Windows 11.",
                        category="environment",
                        fact_key="dev_os",
                    )
                ),
            )
            assert r_add.ok is True
            assert r_add.result["operation"] == "add"
            assert any(
                isinstance(e, MemoryUpdatedEvent) and e.data.action == "added" for e in ev_add
            )

            # Daemon list active
            r_list, _ = await _ws_rpc(
                ws,
                MemoryListFactsRequest(params=MemoryListFactsParams(status="active")),
            )
            assert r_list.ok is True
            assert r_list.result["total"] == 1
            assert r_list.result["facts"][0]["fact_text"] == "User develops on Windows 11."

            # Daemon update
            r_upd, ev_upd = await _ws_rpc(
                ws,
                MemoryManageFactRequest(
                    params=MemoryManageFactParams(
                        action="update",
                        fact_key="dev_os",
                        fact_text="User develops on Windows 11 Pro.",
                        category="environment",
                    )
                ),
            )
            assert r_upd.ok is True
            assert r_upd.result["operation"] == "update"
            assert r_upd.result["superseded_id"] is not None
            assert any(
                isinstance(e, MemoryUpdatedEvent) and e.data.action == "updated" for e in ev_upd
            )

            # Daemon delete
            r_del, ev_del = await _ws_rpc(
                ws,
                MemoryManageFactRequest(
                    params=MemoryManageFactParams(action="delete", fact_key="dev_os")
                ),
            )
            assert r_del.ok is True
            assert r_del.result["operation"] == "delete"
            assert any(
                isinstance(e, MemoryUpdatedEvent) and e.data.action == "deleted" for e in ev_del
            )

        # VoiceRunner -> manage_memory tool calls (add, list, update, delete)
        runner_task = asyncio.create_task(runner.run_forever())
        await _wait_until(lambda: runner._daemon_listener_active)
        await runner.start_call(trigger="manual")

        await backend.event_queue.put(
            ToolCallEvent(
                call_id="vm-add",
                name="manage_memory",
                arguments={
                    "action": "add",
                    "fact_text": "User drinks filter coffee at 9am.",
                    "category": "preference",
                    "fact_key": "morning_drink",
                },
            )
        )
        await _wait_until(lambda: len(backend.sent_tool_responses) >= 1)
        assert backend.sent_tool_responses[0].is_error is False
        assert backend.sent_tool_responses[0].response["operation"] == "add"
        assert backend.sent_tool_responses[0].response["fact"]["source_surface"] == "voice"
        assert (
            backend.sent_tool_responses[0].response["fact"]["source_session_id"]
            == runner.call_id
        )

        await backend.event_queue.put(
            ToolCallEvent(
                call_id="vm-list",
                name="manage_memory",
                arguments={"action": "list", "category": "preference"},
            )
        )
        await _wait_until(lambda: len(backend.sent_tool_responses) >= 2)
        assert backend.sent_tool_responses[1].is_error is False
        assert backend.sent_tool_responses[1].response["total"] == 1
        assert (
            backend.sent_tool_responses[1].response["facts"][0]["fact_text"]
            == "User drinks filter coffee at 9am."
        )

        await backend.event_queue.put(
            ToolCallEvent(
                call_id="vm-upd",
                name="manage_memory",
                arguments={
                    "action": "update",
                    "fact_key": "morning_drink",
                    "fact_text": "User drinks espresso at 9am.",
                    "category": "preference",
                },
            )
        )
        await _wait_until(lambda: len(backend.sent_tool_responses) >= 3)
        assert backend.sent_tool_responses[2].is_error is False
        assert backend.sent_tool_responses[2].response["operation"] == "update"

        await backend.event_queue.put(
            ToolCallEvent(
                call_id="vm-del",
                name="manage_memory",
                arguments={"action": "delete", "fact_key": "morning_drink"},
            )
        )
        await _wait_until(lambda: len(backend.sent_tool_responses) >= 4)
        assert backend.sent_tool_responses[3].is_error is False
        assert backend.sent_tool_responses[3].response["operation"] == "delete"
        assert store.list_active_facts(status="active") == []
    finally:
        await runner.stop()
        if runner_task is not None:
            with contextlib.suppress(asyncio.CancelledError):
                await runner_task
        await daemon.stop()
        store.close()


async def test_chat_and_voice_fact_bank_parity(tmp_path: Path) -> None:
    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()

    # Seed Tier 3 fact bank with active, superseded, and deleted facts
    old_ide = store.manage_fact(
        "add",
        fact_text="User writes code in Atom editor.",
        category="environment",
        fact_key="primary_ide",
    )
    store.manage_fact(
        "update",
        fact_id=old_ide["fact"]["id"],
        fact_text="User writes code in Cursor IDE.",
        category="environment",
        fact_key="primary_ide",
    )
    retracted = store.manage_fact(
        "add",
        fact_text="User wants verbose debug logs.",
        category="preference",
        fact_key="log_verbosity",
    )
    store.manage_fact("delete", fact_id=retracted["fact"]["id"])
    store.manage_fact(
        "add",
        fact_text="Never run git push without explicit approval.",
        category="constraint",
        fact_key="git_push_rule",
    )

    registry = ToolRegistry(audit_logger=AuditLogger(tmp_path / "logs" / "audit.jsonl"))
    register_memory_tools(registry, store)
    chat_provider = _FakeAgentProvider()
    agent = AgentLoop(provider=chat_provider, registry=registry, memory=store)

    daemon: CleaDaemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=chat_provider,
        memory=store,
        registry=registry,
    )
    await daemon.start()
    url = f"ws://127.0.0.1:{daemon.bound_port}"

    try:
        # 1. Chat: AgentLoop._build_system_prompt() and fake provider received system_prompt
        built_prompt = agent._build_system_prompt()
        await agent.run_turn(session=Session(id="parity-chat"), user_text="What IDE do I use?")
        _, received_prompt = chat_provider.recorded_calls[-1]

        for chat_prompt in (built_prompt, received_prompt):
            assert "## Active Memory Facts" in chat_prompt
            assert "User writes code in Cursor IDE." in chat_prompt
            assert "Never run git push without explicit approval." in chat_prompt
            assert "User writes code in Atom editor." not in chat_prompt
            assert "User wants verbose debug logs." not in chat_prompt

        # 2. Voice: load_voice_memory_context() & resolve_voice_system_instruction()
        voice_mem_ctx = await load_voice_memory_context(daemon_url=url, token=daemon.token)
        assert "## Active Memory Facts" in voice_mem_ctx
        assert "User writes code in Cursor IDE." in voice_mem_ctx
        assert "Never run git push without explicit approval." in voice_mem_ctx
        assert "User writes code in Atom editor." not in voice_mem_ctx
        assert "User wants verbose debug logs." not in voice_mem_ctx

        voice_sys = await resolve_voice_system_instruction(daemon_url=url, token=daemon.token)
        assert "## Active Memory Facts" in voice_sys
        assert "User writes code in Cursor IDE." in voice_sys
        assert "Never run git push without explicit approval." in voice_sys
        assert "User writes code in Atom editor." not in voice_sys
        assert "User wants verbose debug logs." not in voice_sys

        # 3. Daemon voice.start_call memory_context & instruction resolution
        async with connect(url, subprotocols=auth.client_subprotocols(daemon.token)) as ws:
            start_resp, _ = await _ws_rpc(
                ws,
                VoiceStartCallRequest(params=VoiceStartCallParams(trigger="manual")),
            )
            assert start_resp.ok is True
            start_ctx = start_resp.result["memory_context"]
            assert isinstance(start_ctx, str)
            assert "## Active Memory Facts" in start_ctx
            assert "User writes code in Cursor IDE." in start_ctx
            assert "Never run git push without explicit approval." in start_ctx
            assert "User writes code in Atom editor." not in start_ctx
            assert "User wants verbose debug logs." not in start_ctx

            resolved_from_start = await resolve_voice_system_instruction(memory_context=start_ctx)
            assert "User writes code in Cursor IDE." in resolved_from_start
            assert "Never run git push without explicit approval." in resolved_from_start
            assert "User writes code in Atom editor." not in resolved_from_start
            assert "User wants verbose debug logs." not in resolved_from_start
    finally:
        await daemon.stop()
        store.close()


def test_skip_indexing_synthetic_voice_companion_session(tmp_path: Path) -> None:
    """Synthetic voice_companion container must never be indexed into episodic_sessions / FTS5."""
    store = MemoryStore(base_dir=tmp_path)
    store.ensure_initialized()
    bank = ThreeTierMemoryBank(db=store.memory_db)

    assert is_synthetic_episodic_session(VOICE_COMPANION_SESSION_ID) is True
    assert is_synthetic_episodic_session("  Voice_Companion ") is True
    assert is_synthetic_episodic_session("call-voice-101") is False

    sample_entries = [
        {
            "role": "user",
            "text": "Let us review the quarterly roadmap over voice.",
            "timestamp": "2026-11-21T10:00:00+00:00",
        },
        {
            "role": "assistant",
            "text": "Sure, we can go over the quarterly roadmap milestones.",
            "timestamp": "2026-11-21T10:00:05+00:00",
        },
    ]

    try:
        # 1. Direct DB upsert, facade upsert/index, store index/append_episode, voice hook & daemon
        assert (
            store.memory_db.upsert_episodic_session(
                session_id=VOICE_COMPANION_SESSION_ID,
                surface="voice",
                title="Voice Companion",
                summary="Smushed unrelated calls about quarterly roadmap.",
                topics="quarterly roadmap",
            )
            is None
        )
        assert (
            bank.upsert_episodic_session(
                session_id="VOICE_COMPANION",
                surface="voice",
                title="Voice Companion",
                summary="Another attempt to index voice companion.",
                topics="roadmap",
            )
            is None
        )
        assert (
            bank.index_session_messages(
                session_id=VOICE_COMPANION_SESSION_ID,
                messages=sample_entries,
                surface="voice",
            )
            is None
        )
        assert (
            store.index_episodic_session(
                session_id=VOICE_COMPANION_SESSION_ID,
                surface="voice",
                title="Voice Companion",
                summary="Store attempt to index voice companion.",
                topics="roadmap",
            )
            is None
        )
        # Markdown episode still persists for legacy compatibility, but SQLite stays empty
        ep_path = store.append_episode(
            session_id=VOICE_COMPANION_SESSION_ID,
            summary="Companion summary that must not enter SQLite episodic index.",
        )
        assert ep_path.exists()

        assert (
            index_voice_call_episode(
                db_or_store=store,
                call_id=VOICE_COMPANION_SESSION_ID,
                entries=sample_entries,
            )
            is None
        )

        # Daemon _index_session_episodic also skips voice_companion cleanly
        registry = ToolRegistry(audit_logger=AuditLogger(tmp_path / "logs" / "audit.jsonl"))
        daemon = build_daemon(
            host="127.0.0.1",
            port=0,
            provider=_FakeAgentProvider(),
            memory=store,
            registry=registry,
        )
        assert (
            daemon._index_session_episodic(
                VOICE_COMPANION_SESSION_ID,
                surface="voice",
                messages=sample_entries,
            )
            is None
        )

        # Verify ZERO rows in both episodic_sessions and episodic_sessions_fts
        with sqlite3.connect(str(store.db_path)) as conn:
            base_count = conn.execute("SELECT COUNT(*) FROM episodic_sessions").fetchone()[0]
            fts_count = conn.execute("SELECT COUNT(*) FROM episodic_sessions_fts").fetchone()[0]
        assert base_count == 0
        assert fts_count == 0

        # 2. Companion UI log stays completely intact
        now_dt = datetime(2026, 11, 21, 10, 5, 0, tzinfo=UTC)
        assert store.append_voice_companion_call(
            call_id="call-voice-101",
            entries=[
                ("user", "Check Docker Compose healthchecks", now_dt),
                ("assistant", "I checked the Docker Compose healthchecks.", now_dt),
            ],
        )
        tail = store.get_voice_companion_tail(max_turns=10)
        assert len(tail) == 2
        assert tail[0]["call_id"] == "call-voice-101"

        # 3. Normal per-call voice session and chat session still index & search cleanly
        voice_indexed = index_voice_call_episode(
            db_or_store=store,
            call_id="call-voice-101",
            entries=[
                {
                    "role": "user",
                    "text": "Check Docker Compose healthchecks for the API container.",
                    "timestamp": "2026-11-21T10:05:00+00:00",
                },
                {
                    "role": "assistant",
                    "text": "Verified Docker Compose healthchecks for the API container.",
                    "timestamp": "2026-11-21T10:05:05+00:00",
                },
            ],
        )
        assert voice_indexed is not None
        assert voice_indexed["session_id"] == "call-voice-101"
        assert voice_indexed["surface"] == "voice"

        chat_indexed = store.index_episodic_session(
            session_id="chat-session-202",
            surface="chat",
            title="SQLite WAL Tuning",
            summary="Configured SQLite WAL mode and busy_timeout for local memory bank.",
            topics="sqlite, wal, memory",
        )
        assert chat_indexed is not None
        assert chat_indexed["session_id"] == "chat-session-202"

        voice_hits = store.search_episodic_memory("Docker Compose healthchecks", limit=5)
        assert len(voice_hits) == 1
        assert voice_hits[0]["session_id"] == "call-voice-101"
        assert voice_hits[0]["surface"] == "voice"

        chat_hits = store.search_episodic_memory("SQLite WAL busy_timeout", limit=5)
        assert len(chat_hits) == 1
        assert chat_hits[0]["session_id"] == "chat-session-202"
        assert chat_hits[0]["surface"] == "chat"

        with sqlite3.connect(str(store.db_path)) as conn:
            indexed_ids = {
                row[0] for row in conn.execute("SELECT session_id FROM episodic_sessions")
            }
        assert indexed_ids == {"call-voice-101", "chat-session-202"}
        assert VOICE_COMPANION_SESSION_ID not in indexed_ids
    finally:
        store.close()


def test_summarizer_prompts_enforce_literal_grounding() -> None:
    """Working, episodic, and consolidation prompts must require strict literal-only grounding."""
    prompts = {
        "working": WORKING_SUMMARY_SYSTEM_PROMPT,
        "episodic": EPISODIC_SUMMARY_SYSTEM_PROMPT,
        "consolidation": CONSOLIDATION_SYSTEM_PROMPT,
    }
    for name, prompt in prompts.items():
        lowered = " ".join(prompt.lower().split())
        assert "literally stated" in lowered, f"{name} prompt missing literal-only requirement"
        assert "never infer" in lowered, f"{name} prompt missing anti-inference constraint"
        assert "invent" in lowered and "unmentioned topics" in lowered, (
            f"{name} prompt missing anti-hallucination constraint"
        )
        assert "single literal sentence" in lowered, (
            f"{name} prompt missing short/trivial session rule"
        )
        assert "empty" in lowered, f"{name} prompt missing empty-output guidance for trivial turns"

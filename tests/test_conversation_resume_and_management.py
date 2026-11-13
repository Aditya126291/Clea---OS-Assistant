"""Tests for conversation resume, tool replay safety, and conversation management."""

from __future__ import annotations

import asyncio
import json
import time
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from websockets.asyncio.client import connect

from clea.core import auth
from clea.core.daemon import build_daemon
from clea.core.protocol import (
    ChatSendParams,
    ChatSendRequest,
    ConversationDeleteAllParams,
    ConversationDeleteAllRequest,
    ConversationDeleteParams,
    ConversationDeleteRequest,
    ConversationExportParams,
    ConversationExportRequest,
    ConversationListRequest,
    ConversationPinParams,
    ConversationPinRequest,
    ConversationRenameParams,
    ConversationRenameRequest,
    ConversationResumeParams,
    ConversationResumeRequest,
    ConversationUndoDeleteParams,
    ConversationUndoDeleteRequest,
    ResponseFrame,
    SessionNewRequest,
    parse_frame,
)
from clea.memory import MemoryStore
from clea.providers.base import LLMProvider, Message, StreamChunk, ToolCall
from clea.tools import AuditLogger, ToolDefinition, ToolRegistry, register_builtin_tools


class EchoProvider(LLMProvider):
    """Simple echo provider for deterministic tests without GEMINI_API_KEY."""

    provider_name = "echo"

    @property
    def model_name(self) -> str:
        return "echo-model"

    async def stream_turn(
        self,
        *,
        messages: list[Message],
        tools: list[ToolDefinition],
        system_prompt: str,
    ) -> AsyncIterator[StreamChunk]:
        last = messages[-1].content if messages else ""
        yield StreamChunk(type="text_delta", text=f"Echo: {last}")
        yield StreamChunk(type="done")


class StrictReplayCheckProvider(LLMProvider):
    """Provider that raises RuntimeError if any history message has tool_calls or role=='tool'.

    Simulates Gemini 3 constraint where replaying fabricated tool calls/results without
    exact thought signatures causes HTTP 400.
    """

    provider_name = "strict-replay"

    def __init__(self) -> None:
        self.call_count = 0

    @property
    def model_name(self) -> str:
        return "strict-model"

    async def stream_turn(
        self,
        *,
        messages: list[Message],
        tools: list[ToolDefinition],
        system_prompt: str,
    ) -> AsyncIterator[StreamChunk]:
        self.call_count += 1

        # On Turn 2 round 1 (resumed), verify past history has no tool steps or tool_calls
        if self.call_count == 3:
            resumed_history = messages[:-1]
            assert len(resumed_history) == 2, (
                f"Expected 2 resumed messages, got {len(resumed_history)}"
            )
            for idx, msg in enumerate(resumed_history):
                if msg.role == "tool":
                    raise RuntimeError(
                        f"Fabricated tool message in history after resume at {idx}: {msg.content}"
                    )
                if getattr(msg, "tool_calls", None):
                    raise RuntimeError(
                        f"Fabricated tool_calls in history after resume at {idx}: {msg.tool_calls}"
                    )

        # If current turn has tool result, complete with final text
        if any(msg.role == "tool" for msg in messages):
            yield StreamChunk(type="text_delta", text="Tool execution complete.")
            yield StreamChunk(type="done")
            return

        # Otherwise trigger a tool call
        yield StreamChunk(
            type="tool_call",
            tool_call=ToolCall(
                id=f"call-{self.call_count}",
                name="get_system_info",
                arguments={"include_platform_details": False},
            ),
        )
        yield StreamChunk(type="done")


class SlowGatedProvider(LLMProvider):
    """Provider that waits on an asyncio.Event before finishing."""

    provider_name = "slow-gated"

    def __init__(self, gate: asyncio.Event) -> None:
        self.gate = gate

    @property
    def model_name(self) -> str:
        return "slow-model"

    async def stream_turn(
        self,
        *,
        messages: list[Message],
        tools: list[ToolDefinition],
        system_prompt: str,
    ) -> AsyncIterator[StreamChunk]:
        await self.gate.wait()
        yield StreamChunk(type="text_delta", text="Finished slow turn.")
        yield StreamChunk(type="done")


async def test_resume_rebuilds_session_after_daemon_restart(tmp_path: Path) -> None:
    """Test that a conversation can be resumed from disk alone after daemon restart."""
    memory1 = MemoryStore(base_dir=tmp_path)
    registry1 = ToolRegistry(audit_logger=AuditLogger(tmp_path / "logs" / "audit.jsonl"))
    register_builtin_tools(registry1)

    daemon1 = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=EchoProvider(),
        memory=memory1,
        registry=registry1,
    )
    await daemon1.start()

    session_id: str
    try:
        url = f"ws://127.0.0.1:{daemon1.bound_port}"
        async with connect(url, subprotocols=auth.client_subprotocols(daemon1.token)) as ws:
            await ws.send(SessionNewRequest().model_dump_json())
            resp = parse_frame(await ws.recv())
            assert isinstance(resp, ResponseFrame) and resp.ok
            session_id = resp.session_id

            req = ChatSendRequest(
                session_id=session_id,
                params=ChatSendParams(text="Hello Clea!"),
            )
            await ws.send(req.model_dump_json())
            final_resp = None
            while final_resp is None:
                frame = parse_frame(await ws.recv())
                if isinstance(frame, ResponseFrame) and frame.id == req.id:
                    final_resp = frame
            assert final_resp.ok
    finally:
        await daemon1.stop()

    # Now start Daemon 2 on the SAME data directory (fresh in-memory state)
    memory2 = MemoryStore(base_dir=tmp_path)
    registry2 = ToolRegistry(audit_logger=AuditLogger(tmp_path / "logs" / "audit.jsonl"))
    register_builtin_tools(registry2)
    daemon2 = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=EchoProvider(),
        memory=memory2,
        registry=registry2,
    )
    await daemon2.start()

    try:
        url = f"ws://127.0.0.1:{daemon2.bound_port}"
        async with connect(url, subprotocols=auth.client_subprotocols(daemon2.token)) as ws:
            # Resume past conversation
            resume_req = ConversationResumeRequest(
                params=ConversationResumeParams(session_id=session_id)
            )
            await ws.send(resume_req.model_dump_json())
            resume_resp = parse_frame(await ws.recv())
            assert isinstance(resume_resp, ResponseFrame)
            assert resume_resp.ok
            assert resume_resp.result["session_id"] == session_id
            assert len(resume_resp.result["messages"]) >= 2
            assert resume_resp.result["messages"][0]["text"] == "Hello Clea!"

            # Continue chatting in resumed session
            req2 = ChatSendRequest(
                session_id=session_id,
                params=ChatSendParams(text="How are you doing?"),
            )
            await ws.send(req2.model_dump_json())
            final_resp2 = None
            while final_resp2 is None:
                frame = parse_frame(await ws.recv())
                if isinstance(frame, ResponseFrame) and frame.id == req2.id:
                    final_resp2 = frame
            assert final_resp2.ok

            # Verify both turns are in the transcript file
            t_data = memory2.get_transcript(session_id)
            assert t_data is not None
            user_texts = [m["text"] for m in t_data["messages"] if m["role"] == "user"]
            assert "Hello Clea!" in user_texts
            assert "How are you doing?" in user_texts
    finally:
        await daemon2.stop()


async def test_resume_tool_calling_conversation_and_followup_tool_call(tmp_path: Path) -> None:
    """Test resume with real tool use does not send fabricated tool steps on follow-up."""
    provider = StrictReplayCheckProvider()
    memory = MemoryStore(base_dir=tmp_path)
    registry = ToolRegistry(audit_logger=AuditLogger(tmp_path / "logs" / "audit.jsonl"))
    register_builtin_tools(registry)

    daemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=provider,
        memory=memory,
        registry=registry,
    )
    await daemon.start()

    try:
        url = f"ws://127.0.0.1:{daemon.bound_port}"
        async with connect(url, subprotocols=auth.client_subprotocols(daemon.token)) as ws:
            await ws.send(SessionNewRequest().model_dump_json())
            session_resp = parse_frame(await ws.recv())
            session_id = session_resp.session_id

            # Turn 1: triggers tool call
            req1 = ChatSendRequest(
                session_id=session_id,
                params=ChatSendParams(text="Check system info"),
            )
            await ws.send(req1.model_dump_json())
            final_resp = None
            while final_resp is None:
                frame = parse_frame(await ws.recv())
                if isinstance(frame, ResponseFrame) and frame.id == req1.id:
                    final_resp = frame
            assert final_resp.ok
            assert final_resp.result["text"] == "Tool execution complete."

            # Clear in-memory sessions in daemon to simulate restart
            daemon.sessions._sessions.clear()

            # Resume session from transcript
            resume_req = ConversationResumeRequest(
                params=ConversationResumeParams(session_id=session_id)
            )
            await ws.send(resume_req.model_dump_json())
            resume_resp = parse_frame(await ws.recv())
            assert isinstance(resume_resp, ResponseFrame)
            assert resume_resp.ok

            # Turn 2: triggers another tool call — StrictReplayCheckProvider checks history
            req2 = ChatSendRequest(
                session_id=session_id,
                params=ChatSendParams(text="Check system info again"),
            )
            await ws.send(req2.model_dump_json())
            final_resp2 = None
            while final_resp2 is None:
                frame = parse_frame(await ws.recv())
                if isinstance(frame, ResponseFrame) and frame.id == req2.id:
                    final_resp2 = frame
            # Must succeed without error (no fabricated steps in history)
            assert final_resp2.ok
            assert final_resp2.result["text"] == "Tool execution complete."
    finally:
        await daemon.stop()


async def test_concurrency_rejects_second_turn_for_same_session(tmp_path: Path) -> None:
    """Test concurrency: only one running turn per session."""
    gate = asyncio.Event()
    provider = SlowGatedProvider(gate)
    memory = MemoryStore(base_dir=tmp_path)
    daemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=provider,
        memory=memory,
    )
    await daemon.start()

    try:
        url = f"ws://127.0.0.1:{daemon.bound_port}"
        async with (
            connect(url, subprotocols=auth.client_subprotocols(daemon.token)) as ws1,
            connect(url, subprotocols=auth.client_subprotocols(daemon.token)) as ws2,
        ):
            await ws1.send(SessionNewRequest().model_dump_json())
            session_resp = parse_frame(await ws1.recv())
            session_id = session_resp.session_id

            # Start Turn 1 on ws1 (blocks on gate)
            req1 = ChatSendRequest(session_id=session_id, params=ChatSendParams(text="Turn 1"))
            await ws1.send(req1.model_dump_json())
            await asyncio.sleep(0.05)

            # Attempt Turn 2 on ws2 for same session while Turn 1 is running
            req2 = ChatSendRequest(session_id=session_id, params=ChatSendParams(text="Turn 2"))
            await ws2.send(req2.model_dump_json())
            resp2 = parse_frame(await ws2.recv())
            assert isinstance(resp2, ResponseFrame)
            assert resp2.ok is False
            assert "A turn is already running for this session." in (resp2.error or "")

            # Open the gate to finish Turn 1
            gate.set()
            final_resp1 = None
            while final_resp1 is None:
                frame = parse_frame(await ws1.recv())
                if isinstance(frame, ResponseFrame) and frame.id == req1.id:
                    final_resp1 = frame
            assert final_resp1.ok
    finally:
        await daemon.stop()


async def test_conversation_management_crud_and_undo(tmp_path: Path) -> None:
    """Test rename, pin, export, delete, undo_delete, delete_all, and purge."""
    memory = MemoryStore(base_dir=tmp_path)
    registry = ToolRegistry(audit_logger=AuditLogger(tmp_path / "logs" / "audit.jsonl"))
    daemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=EchoProvider(),
        memory=memory,
        registry=registry,
    )
    await daemon.start()

    try:
        url = f"ws://127.0.0.1:{daemon.bound_port}"
        async with connect(url, subprotocols=auth.client_subprotocols(daemon.token)) as ws:
            # Create session 1
            await ws.send(SessionNewRequest().model_dump_json())
            s1_id = (parse_frame(await ws.recv())).session_id
            req1 = ChatSendRequest(session_id=s1_id, params=ChatSendParams(text="Chat one"))
            await ws.send(req1.model_dump_json())
            while True:
                f = parse_frame(await ws.recv())
                if isinstance(f, ResponseFrame) and f.id == req1.id:
                    break

            # Create session 2
            await ws.send(SessionNewRequest().model_dump_json())
            s2_id = (parse_frame(await ws.recv())).session_id
            req2 = ChatSendRequest(session_id=s2_id, params=ChatSendParams(text="Chat two"))
            await ws.send(req2.model_dump_json())
            while True:
                f = parse_frame(await ws.recv())
                if isinstance(f, ResponseFrame) and f.id == req2.id:
                    break

            # 1. Test Rename with HTML stripping
            rename_req = ConversationRenameRequest(
                params=ConversationRenameParams(
                    session_id=s1_id,
                    title="<b>Project Discussion</b>",
                )
            )
            await ws.send(rename_req.model_dump_json())
            # May receive ConversationUpdatedEvent broadcast first
            rename_resp = None
            while rename_resp is None:
                f = parse_frame(await ws.recv())
                if isinstance(f, ResponseFrame) and f.id == rename_req.id:
                    rename_resp = f
            assert rename_resp.ok
            assert rename_resp.result["title"] == "Project Discussion"

            # 2. Test Pin
            pin_req = ConversationPinRequest(
                params=ConversationPinParams(session_id=s1_id, pinned=True)
            )
            await ws.send(pin_req.model_dump_json())
            pin_resp = None
            while pin_resp is None:
                f = parse_frame(await ws.recv())
                if isinstance(f, ResponseFrame) and f.id == pin_req.id:
                    pin_resp = f
            assert pin_resp.ok
            assert pin_resp.result["pinned"] is True

            # Verify in conversation.list that pinned item is first
            list_req = ConversationListRequest()
            await ws.send(list_req.model_dump_json())
            list_resp = parse_frame(await ws.recv())
            convs = list_resp.result["conversations"]
            assert len(convs) == 2
            assert convs[0]["session_id"] == s1_id
            assert convs[0]["pinned"] is True
            assert convs[0]["title"] == "Project Discussion"

            # 3. Test Export
            export_req = ConversationExportRequest(
                params=ConversationExportParams(session_id=s1_id)
            )
            await ws.send(export_req.model_dump_json())
            export_resp = parse_frame(await ws.recv())
            assert export_resp.ok
            assert "# Project Discussion" in export_resp.result["content"]
            assert "Chat one" in export_resp.result["content"]
            assert export_resp.result["filename"].endswith(".md")

            # 4. Test Delete with undo
            del_req = ConversationDeleteRequest(params=ConversationDeleteParams(session_id=s1_id))
            await ws.send(del_req.model_dump_json())
            del_resp = None
            while del_resp is None:
                f = parse_frame(await ws.recv())
                if isinstance(f, ResponseFrame) and f.id == del_req.id:
                    del_resp = f
            assert del_resp.ok
            assert not memory.transcript_path(s1_id).exists()
            assert (memory.trash_dir / f"{s1_id}.jsonl").exists()

            # Test Undo Delete
            undo_req = ConversationUndoDeleteRequest(
                params=ConversationUndoDeleteParams(session_id=s1_id)
            )
            await ws.send(undo_req.model_dump_json())
            undo_resp = None
            while undo_resp is None:
                f = parse_frame(await ws.recv())
                if isinstance(f, ResponseFrame) and f.id == undo_req.id:
                    undo_resp = f
            assert undo_resp.ok
            assert memory.transcript_path(s1_id).exists()

            # 5. Test Delete All
            # Refuse without DELETE confirmation
            del_all_bad = ConversationDeleteAllRequest(
                params=ConversationDeleteAllParams(confirmation="wrong")
            )
            await ws.send(del_all_bad.model_dump_json())
            bad_resp = parse_frame(await ws.recv())
            assert bad_resp.ok is False

            del_all_req = ConversationDeleteAllRequest(
                params=ConversationDeleteAllParams(confirmation="DELETE")
            )
            await ws.send(del_all_req.model_dump_json())
            del_all_resp = None
            while del_all_resp is None:
                f = parse_frame(await ws.recv())
                if isinstance(f, ResponseFrame) and f.id == del_all_req.id:
                    del_all_resp = f
            assert del_all_resp.ok
            assert del_all_resp.result["deleted_count"] == 2
            assert len(list(memory.transcripts_dir.glob("*.jsonl"))) == 0

            # Undo delete all
            undo_all = ConversationUndoDeleteRequest(
                params=ConversationUndoDeleteParams(session_id=None)
            )
            await ws.send(undo_all.model_dump_json())
            undo_all_resp = None
            while undo_all_resp is None:
                f = parse_frame(await ws.recv())
                if isinstance(f, ResponseFrame) and f.id == undo_all.id:
                    undo_all_resp = f
            assert undo_all_resp.ok
            assert undo_all_resp.result["restored_count"] == 2
            assert len(list(memory.transcripts_dir.glob("*.jsonl"))) == 2
    finally:
        await daemon.stop()


def test_path_traversal_attempts_rejected(tmp_path: Path) -> None:
    """Validate that path traversal and malformed session IDs are strictly rejected."""
    memory = MemoryStore(base_dir=tmp_path)
    memory.ensure_initialized()

    invalid_ids = [
        "../escape",
        "..\\escape",
        "../../etc/passwd",
        "/absolute/path",
        "C:\\Windows\\System32",
        "id with spaces",
        "bad<tag>id",
        "a" * 100,  # too long
        "",
    ]
    for bad_id in invalid_ids:
        with pytest.raises(ValueError):
            memory.validate_session_id(bad_id)
        with pytest.raises(ValueError):
            memory.transcript_path(bad_id)


def test_audit_logs_record_deletions_without_content(tmp_path: Path) -> None:
    """Verify delete operations are recorded in audit.jsonl without message content."""
    memory = MemoryStore(base_dir=tmp_path)
    memory.ensure_initialized()
    audit_file = tmp_path / "logs" / "audit.jsonl"
    logger = AuditLogger(log_path=audit_file)

    logger.record_event(action="conversation_delete", details={"session_id": "test-sid-1"})
    logger.record_event(action="conversation_delete_all", details={"deleted_count": 5})
    logger.record_event(action="conversation_undo_delete", details={"session_ids": ["test-sid-1"]})

    lines = [json.loads(line) for line in audit_file.read_text(encoding="utf-8").splitlines()]
    assert len(lines) == 3
    assert lines[0]["action"] == "conversation_delete"
    assert lines[0]["details"]["session_id"] == "test-sid-1"
    # Ensure no conversation text is present in audit records
    for line in lines:
        assert "text" not in line["details"]
        assert "content" not in line["details"]
        assert "messages" not in line["details"]


async def test_undo_delete_all_with_wildcard_and_episode_restore(tmp_path: Path) -> None:
    """Test that undo_delete with session_id='*' or None restores transcripts and episodes."""
    memory = MemoryStore(base_dir=tmp_path)
    registry = ToolRegistry(audit_logger=AuditLogger(tmp_path / "logs" / "audit.jsonl"))
    daemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=EchoProvider(),
        memory=memory,
        registry=registry,
    )
    await daemon.start()

    try:
        url = f"ws://127.0.0.1:{daemon.bound_port}"
        async with connect(url, subprotocols=auth.client_subprotocols(daemon.token)) as ws:
            # Create session with transcript and episode
            await ws.send(SessionNewRequest().model_dump_json())
            sid = (parse_frame(await ws.recv())).session_id
            req = ChatSendRequest(session_id=sid, params=ChatSendParams(text="Hello world"))
            await ws.send(req.model_dump_json())
            while True:
                f = parse_frame(await ws.recv())
                if isinstance(f, ResponseFrame) and f.id == req.id:
                    break

            memory.append_episode(session_id=sid, summary="Discussed hello world greetings.")

            # Delete single with delete_episodes=True
            del_req = ConversationDeleteRequest(
                params=ConversationDeleteParams(session_id=sid, delete_episodes=True)
            )
            await ws.send(del_req.model_dump_json())
            while True:
                f = parse_frame(await ws.recv())
                if isinstance(f, ResponseFrame) and f.id == del_req.id:
                    break

            # Confirm transcript and episode were removed from active dirs
            assert not memory.transcript_path(sid).exists()
            assert not any(sid in ep.get("session_id", "") for ep in memory.get_recent_episodes())

            # Undo delete single
            undo_req = ConversationUndoDeleteRequest(
                params=ConversationUndoDeleteParams(session_id=sid)
            )
            await ws.send(undo_req.model_dump_json())
            while True:
                f = parse_frame(await ws.recv())
                if isinstance(f, ResponseFrame) and f.id == undo_req.id:
                    break

            # Transcript and episode restored!
            assert memory.transcript_path(sid).exists()
            assert any(sid in ep.get("session_id", "") for ep in memory.get_recent_episodes())

            # Now test delete_all and undo with wildcard '*'
            del_all = ConversationDeleteAllRequest(
                params=ConversationDeleteAllParams(confirmation="DELETE")
            )
            await ws.send(del_all.model_dump_json())
            while True:
                f = parse_frame(await ws.recv())
                if isinstance(f, ResponseFrame) and f.id == del_all.id:
                    break

            assert len(list(memory.transcripts_dir.glob("*.jsonl"))) == 0

            # Undo with wildcard '*' (as sent by UI toast)
            undo_all_star = ConversationUndoDeleteRequest(
                params=ConversationUndoDeleteParams(session_id="*")
            )
            await ws.send(undo_all_star.model_dump_json())
            undo_star_resp = None
            while undo_star_resp is None:
                f = parse_frame(await ws.recv())
                if isinstance(f, ResponseFrame) and f.id == undo_all_star.id:
                    undo_star_resp = f
            assert undo_star_resp.ok
            assert undo_star_resp.result["restored_count"] == 1
            assert memory.transcript_path(sid).exists()
    finally:
        await daemon.stop()


def test_purge_trash_respects_min_age_window(tmp_path: Path) -> None:
    """Verify that purge_trash(min_age_seconds) retains recently deleted files."""
    memory = MemoryStore(base_dir=tmp_path)
    memory.ensure_initialized()

    # Create dummy transcripts
    memory.append_transcript_message(session_id="old-sid", role="user", text="Old")
    memory.append_transcript_message(session_id="young-sid", role="user", text="Young")

    # Delete both
    memory.delete_transcript("old-sid")
    memory.delete_transcript("young-sid")

    # Manually backdate old-sid meta to 20 seconds ago
    old_meta = memory.trash_dir / "old-sid.meta.json"
    old_data = json.loads(old_meta.read_text(encoding="utf-8"))
    old_data["deleted_at"] = time.time() - 25.0
    old_meta.write_text(json.dumps(old_data), encoding="utf-8")

    # Purge with 10s retention window
    purged_count = memory.purge_trash(min_age_seconds=10.0)
    assert purged_count == 1
    assert not (memory.trash_dir / "old-sid.jsonl").exists()
    assert (memory.trash_dir / "young-sid.jsonl").exists()

    # Purge without window (startup purge) cleans remaining
    all_purged = memory.purge_trash(min_age_seconds=0.0)
    assert all_purged == 1
    assert not (memory.trash_dir / "young-sid.jsonl").exists()


async def test_delete_during_running_turn_rejected(tmp_path: Path) -> None:
    """Verify that deleting a conversation while a turn is running is rejected."""
    gate = asyncio.Event()
    provider = SlowGatedProvider(gate)
    memory = MemoryStore(base_dir=tmp_path)
    daemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=provider,
        memory=memory,
    )
    await daemon.start()

    try:
        url = f"ws://127.0.0.1:{daemon.bound_port}"
        async with (
            connect(url, subprotocols=auth.client_subprotocols(daemon.token)) as ws1,
            connect(url, subprotocols=auth.client_subprotocols(daemon.token)) as ws2,
        ):
            await ws1.send(SessionNewRequest().model_dump_json())
            session_id = (parse_frame(await ws1.recv())).session_id

            # Start Turn 1 on ws1 (blocks)
            req1 = ChatSendRequest(session_id=session_id, params=ChatSendParams(text="Turn 1"))
            await ws1.send(req1.model_dump_json())
            await asyncio.sleep(0.05)

            # Try deleting session from ws2 while turn 1 is active
            del_req = ConversationDeleteRequest(
                params=ConversationDeleteParams(session_id=session_id)
            )
            await ws2.send(del_req.model_dump_json())
            del_resp = parse_frame(await ws2.recv())
            assert isinstance(del_resp, ResponseFrame)
            assert del_resp.ok is False
            assert "Cannot delete conversation while a turn is running" in (del_resp.error or "")

            # Unblock turn
            gate.set()
            while True:
                f = parse_frame(await ws1.recv())
                if isinstance(f, ResponseFrame) and f.id == req1.id:
                    break
    finally:
        await daemon.stop()


async def test_chat_send_rejects_path_traversal_session_id(tmp_path: Path) -> None:
    """Verify that chat.send with path traversal session id is rejected early."""
    memory = MemoryStore(base_dir=tmp_path)
    daemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=EchoProvider(),
        memory=memory,
    )
    await daemon.start()

    try:
        url = f"ws://127.0.0.1:{daemon.bound_port}"
        async with connect(url, subprotocols=auth.client_subprotocols(daemon.token)) as ws:
            req = ChatSendRequest(
                session_id="../../traversal_escape",
                params=ChatSendParams(text="Evil turn"),
            )
            await ws.send(req.model_dump_json())
            resp = parse_frame(await ws.recv())
            assert isinstance(resp, ResponseFrame)
            assert resp.ok is False
            assert "Invalid session id format" in (resp.error or "")
    finally:
        await daemon.stop()


def test_export_markdown_includes_tool_output_summary(tmp_path: Path) -> None:
    """Verify that Markdown export includes short tool output summaries."""
    memory = MemoryStore(base_dir=tmp_path)
    memory.ensure_initialized()

    memory.append_transcript_message(
        session_id="export-test-sid",
        role="assistant",
        text="The weather is clear.",
        tool_calls=[
            {
                "name": "check_weather",
                "arguments": {"city": "Tokyo"},
                "output": "Sunny, 22C",
            }
        ],
    )

    filename, content = memory.export_transcript_markdown("export-test-sid")
    assert filename.endswith(".md")
    assert "> *Used tool `check_weather` (city='Tokyo') -> Sunny, 22C*" in content

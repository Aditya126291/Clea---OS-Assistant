"""End-to-end WebSocket tests for daemon voice.* lifecycle and task delegation."""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from pathlib import Path

from websockets.asyncio.client import connect

from clea.core import auth
from clea.core.daemon import build_daemon
from clea.core.protocol import (
    ApprovalRequestedEvent,
    ApprovalResolvedEvent,
    ApprovalRespondParams,
    ApprovalRespondRequest,
    BaseEvent,
    CallEndedEvent,
    CallStartedEvent,
    ErrorEvent,
    ResponseFrame,
    TaskStatusEvent,
    VoiceDelegateTaskParams,
    VoiceDelegateTaskRequest,
    VoiceEndCallParams,
    VoiceEndCallRequest,
    VoiceStartCallParams,
    VoiceStartCallRequest,
    VoiceStateEvent,
    VoiceStatusRequest,
    parse_frame,
)
from clea.memory import MemoryStore
from clea.providers.base import LLMProvider, Message, StreamChunk, ToolCall
from clea.tools import AuditLogger, RiskLevel, ToolDefinition, ToolRegistry, register_builtin_tools


class FakeVoiceTaskProvider(LLMProvider):
    provider_name = "fake"

    @property
    def model_name(self) -> str:
        return "fake-model"

    async def stream_turn(
        self,
        *,
        messages: list[Message],
        tools: list[ToolDefinition],
        system_prompt: str,
    ) -> AsyncIterator[StreamChunk]:
        if any(msg.role == "tool" for msg in messages):
            tool_msg = next(msg for msg in messages if msg.role == "tool")
            yield StreamChunk(type="text_delta", text=f"Task done: {tool_msg.content}")
            yield StreamChunk(type="done")
            return

        yield StreamChunk(
            type="tool_call",
            tool_call=ToolCall(
                id="call-sys",
                name="get_system_info",
                arguments={"include_platform_details": False},
            ),
        )
        yield StreamChunk(type="done")


class FakeSlowGatedProvider(LLMProvider):
    def __init__(self, tool_name: str, arguments: dict[str, object]) -> None:
        self._tool_name = tool_name
        self._arguments = arguments

    async def stream_turn(
        self,
        *,
        messages: list[Message],
        tools: list[ToolDefinition],
        system_prompt: str,
    ) -> AsyncIterator[StreamChunk]:
        tool_msgs = [msg for msg in messages if msg.role == "tool"]
        if tool_msgs:
            last = tool_msgs[-1]
            yield StreamChunk(
                type="text_delta",
                text="Denied" if last.is_error else f"Completed: {last.content}",
            )
            yield StreamChunk(type="done")
            return
        yield StreamChunk(
            type="tool_call",
            tool_call=ToolCall(id="gated-1", name=self._tool_name, arguments=self._arguments),
        )
        yield StreamChunk(type="done")


class FailingTaskProvider(LLMProvider):
    async def stream_turn(
        self,
        *,
        messages: list[Message],
        tools: list[ToolDefinition],
        system_prompt: str,
    ) -> AsyncIterator[StreamChunk]:
        if False:
            yield StreamChunk(type="done")
        raise RuntimeError("background task boom")


async def test_daemon_voice_call_lifecycle_and_status(tmp_path: Path) -> None:
    daemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=FakeVoiceTaskProvider(),
        memory=MemoryStore(base_dir=tmp_path),
    )
    await daemon.start()
    try:
        url = f"ws://127.0.0.1:{daemon.bound_port}"
        async with (
            connect(url, subprotocols=auth.client_subprotocols(daemon.token)) as voice_ws,  # type: ignore[arg-type]
            connect(url, subprotocols=auth.client_subprotocols(daemon.token)) as ui_ws,  # type: ignore[arg-type]
        ):
            # Initial status is idle
            await voice_ws.send(VoiceStatusRequest().model_dump_json())
            status_resp = parse_frame(await voice_ws.recv())
            assert isinstance(status_resp, ResponseFrame) and status_resp.ok
            assert status_resp.result["call_state"] == "idle"
            assert status_resp.result["voice_state"] == "idle"
            assert status_resp.result["call_id"] is None

            # Start call broadcasts call_started to both clients
            start_req = VoiceStartCallRequest(params=VoiceStartCallParams(trigger="wake_word"))
            await voice_ws.send(start_req.model_dump_json())

            voice_frames = [parse_frame(await voice_ws.recv()), parse_frame(await voice_ws.recv())]
            call_started_voice = next(f for f in voice_frames if isinstance(f, CallStartedEvent))
            start_resp = next(f for f in voice_frames if isinstance(f, ResponseFrame))
            assert start_resp.ok
            call_id = start_resp.result["call_id"]
            assert call_started_voice.data.call_id == call_id
            assert call_started_voice.data.call_state == "active"

            ui_started = parse_frame(await ui_ws.recv())
            assert isinstance(ui_started, CallStartedEvent)
            assert ui_started.data.call_id == call_id

            # Duplicate start_call is rejected while active
            await voice_ws.send(VoiceStartCallRequest().model_dump_json())
            dup_resp = parse_frame(await voice_ws.recv())
            assert isinstance(dup_resp, ResponseFrame) and not dup_resp.ok

            # Status from UI reflects active call
            await ui_ws.send(VoiceStatusRequest().model_dump_json())
            ui_status = parse_frame(await ui_ws.recv())
            assert isinstance(ui_status, ResponseFrame) and ui_status.ok
            assert ui_status.result["call_state"] == "active"
            assert ui_status.result["call_id"] == call_id

            # End call with mismatched call_id fails
            await voice_ws.send(
                VoiceEndCallRequest(params=VoiceEndCallParams(call_id="wrong-id")).model_dump_json()
            )
            bad_end = parse_frame(await voice_ws.recv())
            assert isinstance(bad_end, ResponseFrame) and not bad_end.ok

            # End active call broadcasts call_ended and resets status to idle
            await voice_ws.send(
                VoiceEndCallRequest(
                    params=VoiceEndCallParams(call_id=call_id, reason="user_done")
                ).model_dump_json()
            )
            end_frames = [parse_frame(await voice_ws.recv()), parse_frame(await voice_ws.recv())]
            ended_evt = next(f for f in end_frames if isinstance(f, CallEndedEvent))
            end_resp = next(f for f in end_frames if isinstance(f, ResponseFrame))
            assert end_resp.ok and end_resp.result["call_state"] == "idle"
            assert ended_evt.data.call_id == call_id
            assert ended_evt.data.reason == "user_done"
            assert ended_evt.data.duration_seconds is not None
            assert ended_evt.data.duration_seconds >= 0.0

            ui_ended = parse_frame(await ui_ws.recv())
            assert isinstance(ui_ended, CallEndedEvent)
            assert ui_ended.data.call_state == "idle"

            # Idempotent end_call when already idle (no call_id) succeeds cleanly
            await voice_ws.send(VoiceEndCallRequest().model_dump_json())
            idem_resp = parse_frame(await voice_ws.recv())
            assert isinstance(idem_resp, ResponseFrame) and idem_resp.ok
            assert idem_resp.result["call_state"] == "idle"
            assert idem_resp.result["call_id"] is None
    finally:
        await daemon.stop()


async def test_daemon_voice_delegate_task_broadcasts_activity_and_approvals(
    tmp_path: Path,
) -> None:
    registry = ToolRegistry(audit_logger=AuditLogger(tmp_path / "logs" / "audit.jsonl"))
    register_builtin_tools(registry)
    executed = False

    def edit_note(note: str) -> str:
        nonlocal executed
        executed = True
        return f"saved:{note}"

    registry.register(
        name="edit_note",
        description="Edit a note.",
        input_schema={"type": "object", "properties": {"note": {"type": "string"}}},
        risk_level=RiskLevel.REVERSIBLE,
        handler=edit_note,
    )

    daemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=FakeSlowGatedProvider("edit_note", {"note": "grocery"}),
        memory=MemoryStore(base_dir=tmp_path),
        registry=registry,
        approval_timeout=5.0,
    )
    await daemon.start()
    try:
        url = f"ws://127.0.0.1:{daemon.bound_port}"
        async with (
            connect(url, subprotocols=auth.client_subprotocols(daemon.token)) as voice_ws,  # type: ignore[arg-type]
            connect(url, subprotocols=auth.client_subprotocols(daemon.token)) as ui_ws,  # type: ignore[arg-type]
        ):
            # Empty description rejected immediately
            bad_req = VoiceDelegateTaskRequest(params=VoiceDelegateTaskParams(description="   "))
            await voice_ws.send(bad_req.model_dump_json())
            bad_resp = parse_frame(await voice_ws.recv())
            assert isinstance(bad_resp, ResponseFrame) and not bad_resp.ok

            # Valid delegated task runs asynchronously and shows up on UI Activity + Approvals
            req = VoiceDelegateTaskRequest(
                params=VoiceDelegateTaskParams(description="Update my grocery note")
            )
            await voice_ws.send(req.model_dump_json())

            # UI should receive TaskStatusEvent(running) and ApprovalRequestedEvent
            ui_events: list[BaseEvent] = []
            while True:
                frame = parse_frame(await ui_ws.recv())
                if isinstance(frame, BaseEvent):
                    ui_events.append(frame)
                    if isinstance(frame, ApprovalRequestedEvent):
                        await ui_ws.send(
                            ApprovalRespondRequest(
                                session_id=frame.session_id,
                                params=ApprovalRespondParams(
                                    approval_id=frame.data.approval_id,
                                    decision="allow",
                                ),
                            ).model_dump_json()
                        )
                        break

            # Wait for UI to receive ApprovalResolvedEvent and TaskStatusEvent(succeeded)
            while not any(
                isinstance(e, TaskStatusEvent) and e.data.status == "succeeded" for e in ui_events
            ):
                frame = parse_frame(await ui_ws.recv())
                if isinstance(frame, BaseEvent):
                    ui_events.append(frame)

            assert any(
                isinstance(e, TaskStatusEvent)
                and e.data.task_id == req.id
                and e.data.status == "running"
                for e in ui_events
            )
            assert any(
                isinstance(e, ApprovalResolvedEvent) and e.data.decision == "allow"
                for e in ui_events
            )

            # Voice connection receives ResponseFrame with final output
            voice_resp: ResponseFrame | None = None
            while voice_resp is None:
                frame = parse_frame(await asyncio.wait_for(voice_ws.recv(), timeout=2.0))
                if isinstance(frame, ResponseFrame) and frame.id == req.id:
                    voice_resp = frame

            assert voice_resp.ok is True
            assert voice_resp.result["task_id"] == req.id
            assert voice_resp.result["status"] == "succeeded"
            assert "saved:grocery" in voice_resp.result["output"]
            assert executed is True
    finally:
        await daemon.stop()


async def test_daemon_voice_delegate_task_failure(tmp_path: Path) -> None:
    daemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=FailingTaskProvider(),
        memory=MemoryStore(base_dir=tmp_path),
    )
    await daemon.start()
    try:
        url = f"ws://127.0.0.1:{daemon.bound_port}"
        async with connect(
            url,
            subprotocols=auth.client_subprotocols(daemon.token),  # type: ignore[arg-type]
        ) as ws:
            req = VoiceDelegateTaskRequest(
                params=VoiceDelegateTaskParams(description="Run failing task")
            )
            await ws.send(req.model_dump_json())

            events: list[BaseEvent] = []
            final_resp: ResponseFrame | None = None
            while final_resp is None:
                frame = parse_frame(await ws.recv())
                if isinstance(frame, BaseEvent):
                    events.append(frame)
                elif isinstance(frame, ResponseFrame) and frame.id == req.id:
                    final_resp = frame

            assert final_resp.ok is False
            assert "background task boom" in (final_resp.error or "")
            assert any(isinstance(e, TaskStatusEvent) and e.data.status == "failed" for e in events)
            assert any(isinstance(e, ErrorEvent) and e.data.code == "turn_failed" for e in events)
    finally:
        await daemon.stop()


async def test_daemon_voice_delegate_task_disconnect_cleans_up_activity_and_approval(
    tmp_path: Path,
) -> None:
    registry = ToolRegistry(audit_logger=AuditLogger(tmp_path / "logs" / "audit.jsonl"))
    executed = False

    def gated(note: str) -> str:
        nonlocal executed
        executed = True
        return note

    registry.register(
        name="edit_note",
        description="Edit a note.",
        input_schema={"type": "object", "properties": {"note": {"type": "string"}}},
        risk_level=RiskLevel.REVERSIBLE,
        handler=gated,
    )

    daemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=FakeSlowGatedProvider("edit_note", {"note": "x"}),
        memory=MemoryStore(base_dir=tmp_path),
        registry=registry,
        approval_timeout=5.0,
    )
    await daemon.start()
    try:
        url = f"ws://127.0.0.1:{daemon.bound_port}"
        async with connect(
            url,
            subprotocols=auth.client_subprotocols(daemon.token),  # type: ignore[arg-type]
        ) as ui_ws:
            voice_ws = await connect(
                url,
                subprotocols=auth.client_subprotocols(daemon.token),  # type: ignore[arg-type]
            )
            req = VoiceDelegateTaskRequest(
                params=VoiceDelegateTaskParams(description="Edit note before drop")
            )
            await voice_ws.send(req.model_dump_json())

            # Wait for UI to receive TaskStatusEvent(running) and ApprovalRequestedEvent
            ui_events: list[BaseEvent] = []
            while not any(isinstance(e, ApprovalRequestedEvent) for e in ui_events):
                frame = parse_frame(await ui_ws.recv())
                if isinstance(frame, BaseEvent):
                    ui_events.append(frame)

            # Drop the voice socket unexpectedly mid-approval
            await voice_ws.close()

            while not any(
                isinstance(e, TaskStatusEvent) and e.data.status == "failed" for e in ui_events
            ):
                frame = parse_frame(await asyncio.wait_for(ui_ws.recv(), timeout=2.0))
                if isinstance(frame, BaseEvent):
                    ui_events.append(frame)

            resolved = [e for e in ui_events if isinstance(e, ApprovalResolvedEvent)]
            assert len(resolved) == 1
            assert resolved[0].data.decision == "deny"
            failed = [
                e for e in ui_events if isinstance(e, TaskStatusEvent) and e.data.status == "failed"
            ]
            assert len(failed) == 1
            assert failed[0].data.task_id == req.id
            assert executed is False
    finally:
        await daemon.stop()


async def test_daemon_voice_start_call_rejects_ending_state(tmp_path: Path) -> None:
    daemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=FakeVoiceTaskProvider(),
        memory=MemoryStore(base_dir=tmp_path),
    )
    daemon._call_state = "ending"
    await daemon.start()
    try:
        url = f"ws://127.0.0.1:{daemon.bound_port}"
        async with connect(
            url,
            subprotocols=auth.client_subprotocols(daemon.token),  # type: ignore[arg-type]
        ) as ws:
            await ws.send(VoiceStartCallRequest().model_dump_json())
            resp = parse_frame(await ws.recv())
            assert isinstance(resp, ResponseFrame) and not resp.ok
    finally:
        await daemon.stop()


async def test_daemon_voice_start_call_returns_memory_context_and_end_call_persists_transcript(
    tmp_path: Path,
) -> None:
    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()
    mem.add_or_update_entry(
        text="User lives in Bangalore.",
        section="Environment",
        source="stated",
    )

    daemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=FakeVoiceTaskProvider(),
        memory=mem,
    )
    await daemon.start()
    try:
        url = f"ws://127.0.0.1:{daemon.bound_port}"
        async with connect(
            url,
            subprotocols=auth.client_subprotocols(daemon.token),  # type: ignore[arg-type]
        ) as ws:
            # 1. Start call
            await ws.send(VoiceStartCallRequest().model_dump_json())
            # Consume broadcast event
            ev = parse_frame(await ws.recv())
            assert isinstance(ev, CallStartedEvent)
            # ResponseFrame
            resp = parse_frame(await ws.recv())
            assert isinstance(resp, ResponseFrame) and resp.ok
            assert resp.result.get("call_id") is not None
            assert "User lives in Bangalore." in resp.result.get("memory_context", "")

            call_id = resp.result["call_id"]

            # 2. End call with transcript
            transcript = [
                {"role": "user", "text": "What is the capital of India?"},
                {"role": "assistant", "text": "New Delhi."},
            ]
            end_req = VoiceEndCallRequest(
                params=VoiceEndCallParams(
                    call_id=call_id,
                    reason="user_done",
                    transcript=transcript,
                )
            )
            await ws.send(end_req.model_dump_json())
            ev_end = parse_frame(await ws.recv())
            assert isinstance(ev_end, CallEndedEvent)
            resp_end = parse_frame(await ws.recv())
            assert isinstance(resp_end, ResponseFrame) and resp_end.ok

            # 3. Check transcript was written to logs/transcripts/<call_id>.jsonl
            tr = mem.get_transcript(call_id)
            assert tr is not None
            assert len(tr["messages"]) == 2
            assert tr["messages"][0]["text"] == "What is the capital of India?"
            assert tr["messages"][1]["text"] == "New Delhi."
    finally:
        await daemon.stop()


async def test_daemon_voice_start_call_ui_trigger_fails_when_no_voice_process_running(
    tmp_path: Path,
) -> None:
    daemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=FakeVoiceTaskProvider(),
        memory=MemoryStore(base_dir=tmp_path),
    )
    await daemon.start()
    try:
        url = f"ws://127.0.0.1:{daemon.bound_port}"
        async with connect(
            url,
            subprotocols=auth.client_subprotocols(daemon.token),  # type: ignore[arg-type]
        ) as ws:
            # UI starts call, but no voice runner process is connected
            await ws.send(
                VoiceStartCallRequest(params=VoiceStartCallParams(trigger="ui")).model_dump_json()
            )

            # Expect broadcast error VoiceStateEvent and error ResponseFrame
            frames = [parse_frame(await ws.recv()), parse_frame(await ws.recv())]
            err_event = next(f for f in frames if isinstance(f, VoiceStateEvent))
            err_resp = next(f for f in frames if isinstance(f, ResponseFrame))

            assert err_event.data.state == "error"
            assert "Voice process not running" in (err_event.data.error or "")

            assert not err_resp.ok
            assert "Voice process not running" in (err_resp.error or "")
    finally:
        await daemon.stop()


async def test_daemon_cleans_up_call_on_voice_process_disconnect(tmp_path: Path) -> None:
    daemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=FakeVoiceTaskProvider(),
        memory=MemoryStore(base_dir=tmp_path),
    )
    await daemon.start()
    try:
        url = f"ws://127.0.0.1:{daemon.bound_port}"
        # 1. Voice process connects and starts call
        async with connect(
            url,
            subprotocols=auth.client_subprotocols(daemon.token),  # type: ignore[arg-type]
        ) as ws_voice:
            # Send initial VoiceStateEvent to identify as voice runner
            await ws_voice.send(
                VoiceStateEvent(data={"state": "idle"}).model_dump_json()  # type: ignore[arg-type]
            )
            await asyncio.sleep(0.05)

            # Start call from voice runner
            await ws_voice.send(
                VoiceStartCallRequest(
                    params=VoiceStartCallParams(trigger="wake_word")
                ).model_dump_json()
            )
            await asyncio.sleep(0.05)
            assert daemon._call_state == "active"

        # ws_voice is now closed (disconnected)
        await asyncio.sleep(0.1)
        assert daemon._call_state == "idle"
        assert daemon._voice_state == "idle"
    finally:
        await daemon.stop()

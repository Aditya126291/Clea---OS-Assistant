"""End-to-end tests for AgentLoop + CleaDaemon over localhost WebSocket."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from websockets.asyncio.client import connect
from websockets.exceptions import InvalidStatus

from clea.core import auth
from clea.core.daemon import CleaDaemon, build_daemon
from clea.core.protocol import (
    ApprovalRequestedEvent,
    ApprovalResolvedEvent,
    ApprovalRespondParams,
    ApprovalRespondRequest,
    AssistantTokenEvent,
    BaseEvent,
    ChatSendParams,
    ChatSendRequest,
    ConversationGetParams,
    ConversationsGetRequest,
    ConversationsListRequest,
    DailyParams,
    ErrorEvent,
    HealthPingRequest,
    MemoryGetDailyRequest,
    MemoryGetRequest,
    ResponseFrame,
    SessionNewRequest,
    SettingsGetRequest,
    TaskStatusEvent,
    ToolCallFinishedEvent,
    ToolCallStartedEvent,
    parse_frame,
)
from clea.memory import MemoryStore
from clea.providers.base import LLMProvider, Message, StreamChunk, ToolCall
from clea.tools import AuditLogger, RiskLevel, ToolDefinition, ToolRegistry, register_builtin_tools


class FakeToolCallingProvider(LLMProvider):
    """Deterministic provider that calls get_system_info once then streams a reply."""

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
            assert "local_time" in tool_msg.content
            for token in ["Hello ", "from Clea!"]:
                yield StreamChunk(type="text_delta", text=token)
            yield StreamChunk(type="done")
            return

        yield StreamChunk(
            type="tool_call",
            tool_call=ToolCall(
                id="call-1",
                name="get_system_info",
                arguments={"include_platform_details": False},
            ),
        )
        yield StreamChunk(type="done")


class FakeApprovalProvider(LLMProvider):
    """Calls a specified gated tool and reports the outcome."""

    provider_name = "fake"

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
            reply = "Denied." if last.is_error else f"Done: {last.content}"
            yield StreamChunk(type="text_delta", text=reply)
            yield StreamChunk(type="done")
            return

        yield StreamChunk(
            type="tool_call",
            tool_call=ToolCall(id="call-gated", name=self._tool_name, arguments=self._arguments),
        )
        yield StreamChunk(type="done")


class FailingProvider(LLMProvider):
    async def stream_turn(
        self,
        *,
        messages: list[Message],
        tools: list[ToolDefinition],
        system_prompt: str,
    ) -> AsyncIterator[StreamChunk]:
        if False:
            yield StreamChunk(type="done")
        raise RuntimeError("provider exploded")


def test_daemon_rejects_non_localhost_host(tmp_path: Path) -> None:
    registry = ToolRegistry(audit_logger=AuditLogger(tmp_path / "audit.jsonl"))
    register_builtin_tools(registry)
    with pytest.raises(ValueError, match="non-localhost"):
        build_daemon(
            host="0.0.0.0",
            port=0,
            provider=FakeToolCallingProvider(),
            memory=MemoryStore(base_dir=tmp_path),
            registry=registry,
        )


async def test_daemon_streams_reply_runs_tool_and_writes_daily_log(tmp_path: Path) -> None:
    memory = MemoryStore(base_dir=tmp_path)
    registry = ToolRegistry(audit_logger=AuditLogger(tmp_path / "logs" / "audit.jsonl"))
    register_builtin_tools(registry)
    daemon: CleaDaemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=FakeToolCallingProvider(),
        memory=memory,
        registry=registry,
    )
    await daemon.start()
    assert daemon.token_file.exists()

    try:
        url = f"ws://127.0.0.1:{daemon.bound_port}"
        async with connect(
            url,
            subprotocols=auth.client_subprotocols(daemon.token),  # type: ignore[arg-type]
        ) as ws:
            await ws.send(SessionNewRequest().model_dump_json())
            session_resp = parse_frame(await ws.recv())
            assert isinstance(session_resp, ResponseFrame)
            assert session_resp.ok and session_resp.session_id

            req = ChatSendRequest(
                session_id=session_resp.session_id,
                params=ChatSendParams(text="What time is it?"),
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

            assert final_resp.ok is True
            assert final_resp.result["text"] == "Hello from Clea!"
            assert [e.event for e in events] == [
                "task_status",
                "tool_call_started",
                "tool_call_finished",
                "assistant_token",
                "assistant_token",
                "task_status",
            ]
            assert isinstance(events[0], TaskStatusEvent)
            assert events[0].data.status == "running"
            assert events[0].data.kind == "chat_turn"
            assert isinstance(events[1], ToolCallStartedEvent)
            assert events[1].data.risk_level == "read_only"
            assert isinstance(events[2], ToolCallFinishedEvent)
            assert events[2].data.is_error is False
            assert isinstance(events[3], AssistantTokenEvent)
            assert isinstance(events[5], TaskStatusEvent)
            assert events[5].data.status == "succeeded"
            assert events[5].data.kind == "chat_turn"

            daily_content = memory.read_daily()
            assert "**User:** What time is it?" in daily_content
            assert "**Clea:** Hello from Clea!" in daily_content
    finally:
        await daemon.stop()
    assert not daemon.token_file.exists()


async def test_daemon_auth_and_origin_enforcement(tmp_path: Path) -> None:
    daemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=FakeToolCallingProvider(),
        memory=MemoryStore(base_dir=tmp_path),
    )
    await daemon.start()
    url = f"ws://127.0.0.1:{daemon.bound_port}"

    try:
        # Missing token -> 401
        with pytest.raises(InvalidStatus) as missing_exc:
            async with connect(url):
                pass
        assert missing_exc.value.response.status_code == 401

        # Wrong token -> 401
        with pytest.raises(InvalidStatus) as wrong_exc:
            async with connect(
                url,
                subprotocols=auth.client_subprotocols("not-the-right-token"),  # type: ignore[arg-type]
            ):
                pass
        assert wrong_exc.value.response.status_code == 401

        # Wrong Origin -> 403
        with pytest.raises(InvalidStatus) as bad_origin_exc:
            async with connect(
                url,
                subprotocols=auth.client_subprotocols(daemon.token),  # type: ignore[arg-type]
                origin="http://evil.example",  # type: ignore[arg-type]
            ):
                pass
        assert bad_origin_exc.value.response.status_code == 403

        # "null" Origin -> 403
        with pytest.raises(InvalidStatus) as null_origin_exc:
            async with connect(
                url,
                subprotocols=auth.client_subprotocols(daemon.token),  # type: ignore[arg-type]
                origin="null",  # type: ignore[arg-type]
            ):
                pass
        assert null_origin_exc.value.response.status_code == 403

        # Non-local Host header -> 403
        with pytest.raises(InvalidStatus) as bad_host_exc:
            async with connect(
                url,
                subprotocols=auth.client_subprotocols(daemon.token),  # type: ignore[arg-type]
                additional_headers={"Host": "evil.example:8765"},
            ):
                pass
        assert bad_host_exc.value.response.status_code == 403

        # Allowed browser/Electron Origins with valid token -> 101
        for allowed in ("http://localhost:5173", "clea-app://app"):
            async with connect(
                url,
                subprotocols=auth.client_subprotocols(daemon.token),  # type: ignore[arg-type]
                origin=allowed,  # type: ignore[arg-type]
            ) as ws:
                ping = HealthPingRequest()
                await ws.send(ping.model_dump_json())
                resp = parse_frame(await ws.recv())
                assert isinstance(resp, ResponseFrame)
                assert resp.ok and resp.result == {"status": "ok"}
    finally:
        await daemon.stop()


@pytest.mark.parametrize(
    ("risk_level", "decision", "expected_error"),
    [
        (RiskLevel.REVERSIBLE, "allow", False),
        (RiskLevel.DESTRUCTIVE, "deny", True),
    ],
)
async def test_daemon_approval_allow_and_deny(
    tmp_path: Path,
    risk_level: RiskLevel,
    decision: str,
    expected_error: bool,
) -> None:
    registry = ToolRegistry(audit_logger=AuditLogger(tmp_path / "logs" / "audit.jsonl"))
    ran = False

    def gated_tool(target: str) -> str:
        nonlocal ran
        ran = True
        return f"acted on {target}"

    registry.register(
        name="gated_tool",
        description="A gated tool.",
        input_schema={"type": "object", "properties": {"target": {"type": "string"}}},
        risk_level=risk_level,
        handler=gated_tool,
    )

    daemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=FakeApprovalProvider("gated_tool", {"target": "note.txt"}),
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
        ) as ws:
            req = ChatSendRequest(params=ChatSendParams(text="Please run gated tool"))
            await ws.send(req.model_dump_json())

            events: list[BaseEvent] = []
            final_resp: ResponseFrame | None = None
            while final_resp is None:
                frame = parse_frame(await ws.recv())
                if isinstance(frame, BaseEvent):
                    events.append(frame)
                    if isinstance(frame, ApprovalRequestedEvent):
                        assert frame.data.tool == "gated_tool"
                        assert frame.data.risk_level == risk_level.value
                        await ws.send(
                            ApprovalRespondRequest(
                                session_id=frame.session_id,
                                params=ApprovalRespondParams(
                                    approval_id=frame.data.approval_id,
                                    decision=decision,  # type: ignore[arg-type]
                                ),
                            ).model_dump_json()
                        )
                elif isinstance(frame, ResponseFrame) and frame.id == req.id:
                    final_resp = frame

            assert final_resp.ok is True
            resolved = next(e for e in events if isinstance(e, ApprovalResolvedEvent))
            assert resolved.data.decision == decision
            assert resolved.data.reason == "user"
            finished = next(e for e in events if isinstance(e, ToolCallFinishedEvent))
            assert finished.data.is_error is expected_error
            assert ran is (not expected_error)
    finally:
        await daemon.stop()


async def test_daemon_approval_timeout_denies(tmp_path: Path) -> None:
    registry = ToolRegistry(audit_logger=AuditLogger(tmp_path / "logs" / "audit.jsonl"))
    ran = False

    def wipe(item: str) -> str:
        nonlocal ran
        ran = True
        return item

    registry.register(
        name="wipe",
        description="Wipe",
        input_schema={"type": "object", "properties": {"item": {"type": "string"}}},
        risk_level=RiskLevel.DESTRUCTIVE,
        handler=wipe,
    )

    daemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=FakeApprovalProvider("wipe", {"item": "x"}),
        memory=MemoryStore(base_dir=tmp_path),
        registry=registry,
        approval_timeout=0.05,
    )
    await daemon.start()
    try:
        url = f"ws://127.0.0.1:{daemon.bound_port}"
        async with connect(
            url,
            subprotocols=auth.client_subprotocols(daemon.token),  # type: ignore[arg-type]
        ) as ws:
            req = ChatSendRequest(params=ChatSendParams(text="Wipe it"))
            await ws.send(req.model_dump_json())

            events: list[BaseEvent] = []
            final_resp: ResponseFrame | None = None
            while final_resp is None:
                frame = parse_frame(await ws.recv())
                if isinstance(frame, BaseEvent):
                    events.append(frame)
                elif isinstance(frame, ResponseFrame) and frame.id == req.id:
                    final_resp = frame

            assert final_resp.ok is True
            assert final_resp.result["text"] == "Denied."
            resolved = next(e for e in events if isinstance(e, ApprovalResolvedEvent))
            assert resolved.data.decision == "deny"
            assert resolved.data.reason == "timeout"
            assert ran is False
    finally:
        await daemon.stop()


async def test_daemon_disconnect_cancels_pending_approval(tmp_path: Path) -> None:
    registry = ToolRegistry(audit_logger=AuditLogger(tmp_path / "logs" / "audit.jsonl"))
    ran = False

    def wipe(item: str) -> str:
        nonlocal ran
        ran = True
        return item

    registry.register(
        name="wipe",
        description="Wipe",
        input_schema={"type": "object", "properties": {"item": {"type": "string"}}},
        risk_level=RiskLevel.DESTRUCTIVE,
        handler=wipe,
    )

    daemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=FakeApprovalProvider("wipe", {"item": "x"}),
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
        ) as ws:
            await ws.send(ChatSendRequest(params=ChatSendParams(text="Wipe it")).model_dump_json())
            while True:
                frame = parse_frame(await ws.recv())
                if isinstance(frame, ApprovalRequestedEvent):
                    break
        await asyncio.sleep(0.05)
        assert ran is False
    finally:
        await daemon.stop()


async def test_daemon_memory_conversations_settings_and_errors(tmp_path: Path) -> None:
    memory = MemoryStore(base_dir=tmp_path)
    memory.ensure_initialized()
    memory.write_long_term("# Long-Term\n\n- User loves keyboard shortcuts.\n")
    memory.append_daily_turn(session_id="s1", user_text="Hi", assistant_text="Hello!")

    daemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=FakeToolCallingProvider(),
        memory=memory,
    )
    await daemon.start()
    try:
        url = f"ws://127.0.0.1:{daemon.bound_port}"
        async with connect(
            url,
            subprotocols=auth.client_subprotocols(daemon.token),  # type: ignore[arg-type]
        ) as ws:
            # memory.get
            await ws.send(MemoryGetRequest().model_dump_json())
            mem_resp = parse_frame(await ws.recv())
            assert isinstance(mem_resp, ResponseFrame) and mem_resp.ok
            assert "keyboard shortcuts" in mem_resp.result["long_term"]
            assert len(mem_resp.result["days"]) == 1
            day = mem_resp.result["days"][0]

            # memory.get_daily valid & invalid
            await ws.send(MemoryGetDailyRequest(params=DailyParams(date=day)).model_dump_json())
            daily_resp = parse_frame(await ws.recv())
            assert isinstance(daily_resp, ResponseFrame) and daily_resp.ok
            assert "**User:** Hi" in daily_resp.result["content"]

            await ws.send(
                MemoryGetDailyRequest(params=DailyParams(date="../etc")).model_dump_json()
            )
            bad_daily = parse_frame(await ws.recv())
            assert isinstance(bad_daily, ResponseFrame) and not bad_daily.ok

            # conversations.list & conversations.get
            await ws.send(ConversationsListRequest().model_dump_json())
            conv_list = parse_frame(await ws.recv())
            assert isinstance(conv_list, ResponseFrame) and conv_list.ok
            assert len(conv_list.result["conversations"]) == 1
            assert conv_list.result["conversations"][0]["session_id"] == "s1"

            await ws.send(
                ConversationsGetRequest(
                    params=ConversationGetParams(date=day, session_id="s1")
                ).model_dump_json()
            )
            conv_get = parse_frame(await ws.recv())
            assert isinstance(conv_get, ResponseFrame) and conv_get.ok
            assert conv_get.result["turns"][0]["assistant"] == "Hello!"

            # memory.episodes
            await ws.send(
                '{"frame_type": "request", "method": "memory.episodes", "params": {"limit": 5}}'
            )
            ep_resp = parse_frame(await ws.recv())
            assert isinstance(ep_resp, ResponseFrame) and ep_resp.ok
            assert "episodes" in ep_resp.result

            # transcripts.list & transcripts.get
            await ws.send(
                json.dumps(
                    {
                        "frame_type": "request",
                        "method": "transcripts.list",
                        "params": {},
                    }
                )
            )
            tr_list = parse_frame(await ws.recv())
            assert isinstance(tr_list, ResponseFrame) and tr_list.ok
            assert len(tr_list.result["transcripts"]) >= 1

            await ws.send(
                json.dumps(
                    {
                        "frame_type": "request",
                        "method": "transcripts.get",
                        "params": {"session_id": "s1"},
                    }
                )
            )
            tr_get = parse_frame(await ws.recv())
            assert isinstance(tr_get, ResponseFrame) and tr_get.ok
            assert tr_get.result["session_id"] == "s1"
            assert len(tr_get.result["messages"]) == 2

            # dev.emit_sample_task
            await ws.send(
                '{"frame_type": "request", "method": "dev.emit_sample_task", "params": {}}'
            )
            # Receives 4 broadcast TaskStatusEvents then the response frame
            received_kinds = []
            for _ in range(4):
                f = parse_frame(await ws.recv())
                if isinstance(f, TaskStatusEvent):
                    received_kinds.append(f.data.kind)
            assert "background_task" in received_kinds
            assert "scheduled" in received_kinds
            assert "agent_run" in received_kinds

            dev_resp = parse_frame(await ws.recv())
            assert isinstance(dev_resp, ResponseFrame) and dev_resp.ok

            # settings.get never leaks the token
            await ws.send(SettingsGetRequest().model_dump_json())
            settings_resp = parse_frame(await ws.recv())
            assert isinstance(settings_resp, ResponseFrame) and settings_resp.ok
            assert settings_resp.result["provider"] == "fake"
            assert settings_resp.result["model"] == "fake-model"
            assert daemon.token not in json.dumps(settings_resp.result)

            # invalid frame returns ErrorEvent
            await ws.send("{not valid json")
            err_frame = parse_frame(await ws.recv())
            assert isinstance(err_frame, ErrorEvent)
            assert err_frame.data.code == "invalid_frame"
    finally:
        await daemon.stop()


async def test_daemon_provider_failure_emits_error_and_failed_task(tmp_path: Path) -> None:
    daemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=FailingProvider(),
        memory=MemoryStore(base_dir=tmp_path),
    )
    await daemon.start()
    try:
        url = f"ws://127.0.0.1:{daemon.bound_port}"
        async with connect(
            url,
            subprotocols=auth.client_subprotocols(daemon.token),  # type: ignore[arg-type]
        ) as ws:
            req = ChatSendRequest(params=ChatSendParams(text="Hi"))
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
            assert "exploded" in (final_resp.error or "")
            assert [e.event for e in events] == ["task_status", "task_status", "error"]
            assert isinstance(events[1], TaskStatusEvent)
            assert events[1].data.status == "failed"
            assert isinstance(events[2], ErrorEvent)
            assert events[2].data.code == "turn_failed"
    finally:
        await daemon.stop()


async def test_daemon_bind_failure_does_not_overwrite_or_create_token_file(
    tmp_path: Path,
) -> None:
    first = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=FakeToolCallingProvider(),
        memory=MemoryStore(base_dir=tmp_path),
    )
    await first.start()
    try:
        assert auth.read_token(first.token_file) == first.token

        # Second daemon targeting the already-bound port and same token_file must fail
        # without clobbering the active daemon's session token.
        second_same_file = build_daemon(
            host="127.0.0.1",
            port=first.bound_port,
            provider=FakeToolCallingProvider(),
            memory=MemoryStore(base_dir=tmp_path),
        )
        with pytest.raises(OSError):
            await second_same_file.start()
        assert auth.read_token(first.token_file) == first.token

        # Second daemon with an unused token_file must not leave a dead token file behind.
        fresh_token_file = tmp_path / "other" / "session_token"
        second_fresh_file = build_daemon(
            host="127.0.0.1",
            port=first.bound_port,
            provider=FakeToolCallingProvider(),
            memory=MemoryStore(base_dir=tmp_path),
            token_file=fresh_token_file,
        )
        with pytest.raises(OSError):
            await second_fresh_file.start()
        assert not fresh_token_file.exists()
    finally:
        await first.stop()


async def test_daemon_closes_server_if_write_token_fails(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import socket

    probe = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    probe.bind(("127.0.0.1", 0))
    reserved_port = probe.getsockname()[1]
    probe.close()

    daemon = build_daemon(
        host="127.0.0.1",
        port=reserved_port,
        provider=FakeToolCallingProvider(),
        memory=MemoryStore(base_dir=tmp_path),
    )

    def fail_write(_token: str, _path: Path) -> None:
        raise PermissionError("disk read-only")

    monkeypatch.setattr(auth, "write_token", fail_write)
    with pytest.raises(PermissionError, match="disk read-only"):
        await daemon.start()
    assert daemon._server is None

    # The port must be released cleanly so another listener can bind it.
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.bind(("127.0.0.1", reserved_port))
    finally:
        sock.close()


async def test_daemon_single_instance_lock_prevents_second_start_and_preserves_token(
    tmp_path: Path,
) -> None:
    first = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=FakeToolCallingProvider(),
        memory=MemoryStore(base_dir=tmp_path),
    )
    await first.start()
    try:
        second = build_daemon(
            host="127.0.0.1",
            port=0,
            provider=FakeToolCallingProvider(),
            memory=MemoryStore(base_dir=tmp_path),
        )
        with pytest.raises(auth.DaemonAlreadyRunningError, match="already running"):
            await second.start()
        assert auth.read_token(first.token_file) == first.token
    finally:
        await first.stop()

    assert not first.lock_file.exists()

    # After clean stop, another daemon can start using the same data dir.
    third = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=FakeToolCallingProvider(),
        memory=MemoryStore(base_dir=tmp_path),
    )
    await third.start()
    await third.stop()


async def test_daemon_port_conflict_across_data_dirs_releases_second_lock(tmp_path: Path) -> None:
    dir1 = tmp_path / "one"
    dir2 = tmp_path / "two"
    first = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=FakeToolCallingProvider(),
        memory=MemoryStore(base_dir=dir1),
    )
    await first.start()
    try:
        second = build_daemon(
            host="127.0.0.1",
            port=first.bound_port,
            provider=FakeToolCallingProvider(),
            memory=MemoryStore(base_dir=dir2),
        )
        with pytest.raises(OSError):
            await second.start()
        assert not second.lock_file.exists()
        assert not second.token_file.exists()
        assert auth.read_token(first.token_file) == first.token
    finally:
        await first.stop()

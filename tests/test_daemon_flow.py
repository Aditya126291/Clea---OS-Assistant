"""End-to-end tests for AgentLoop + CleaDaemon over localhost WebSocket."""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest
from websockets.asyncio.client import connect

from clea.core.daemon import CleaDaemon, build_daemon
from clea.core.protocol import EventFrame, RequestFrame, ResponseFrame, parse_frame
from clea.memory import MemoryStore
from clea.providers.base import LLMProvider, Message, StreamChunk, ToolCall
from clea.tools import AuditLogger, ToolDefinition, ToolRegistry, register_builtin_tools


class FakeToolCallingProvider(LLMProvider):
    """Deterministic provider that calls get_system_info once then streams a reply."""

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

    try:
        url = f"ws://127.0.0.1:{daemon.bound_port}"
        async with connect(url) as ws:
            await ws.send(RequestFrame(method="session.new").model_dump_json())
            session_resp = parse_frame(await ws.recv())
            assert isinstance(session_resp, ResponseFrame)
            assert session_resp.ok and session_resp.session_id

            req = RequestFrame(
                method="chat.send",
                session_id=session_resp.session_id,
                params={"text": "What time is it?"},
            )
            await ws.send(req.model_dump_json())

            events: list[EventFrame] = []
            final_resp: ResponseFrame | None = None
            while final_resp is None:
                frame = parse_frame(await ws.recv())
                if isinstance(frame, EventFrame):
                    events.append(frame)
                elif isinstance(frame, ResponseFrame) and frame.id == req.id:
                    final_resp = frame

            assert final_resp.ok is True
            assert final_resp.result["text"] == "Hello from Clea!"
            assert [e.event for e in events] == [
                "tool.start",
                "tool.end",
                "text.delta",
                "text.delta",
                "turn.done",
            ]

            daily_content = memory.read_daily()
            assert "**User:** What time is it?" in daily_content
            assert "**Clea:** Hello from Clea!" in daily_content
    finally:
        await daemon.stop()

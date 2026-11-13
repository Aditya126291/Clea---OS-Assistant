"""Tests for Part A Step 1 per-turn and per-call timing/size logging."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from clea.core.agent import AgentLoop
from clea.core.sessions import Session
from clea.memory import MemoryStore
from clea.providers.base import LLMProvider, Message, StreamChunk, TokenUsage, ToolCall
from clea.tools import AuditLogger, RiskLevel, ToolDefinition, ToolRegistry


class _MultiRoundUsageProvider(LLMProvider):
    provider_name = "fake"

    async def stream_turn(
        self,
        *,
        messages: list[Message],
        tools: list[ToolDefinition],
        system_prompt: str,
    ) -> AsyncIterator[StreamChunk]:
        if not any(m.role == "tool" for m in messages):
            yield StreamChunk(
                type="tool_call",
                tool_call=ToolCall(id="c1", name="echo", arguments={"x": "1"}),
            )
            yield StreamChunk(
                type="done",
                usage=TokenUsage(input_tokens=110, output_tokens=12, thought_tokens=0),
            )
            return

        yield StreamChunk(type="text_delta", text="Done!")
        yield StreamChunk(
            type="done",
            usage=TokenUsage(input_tokens=180, output_tokens=8, thought_tokens=0),
        )


class _FailOnRound2Provider(LLMProvider):
    provider_name = "fake"

    async def stream_turn(
        self,
        *,
        messages: list[Message],
        tools: list[ToolDefinition],
        system_prompt: str,
    ) -> AsyncIterator[StreamChunk]:
        if not any(m.role == "tool" for m in messages):
            yield StreamChunk(
                type="tool_call",
                tool_call=ToolCall(id="c1", name="echo", arguments={"x": "1"}),
            )
            yield StreamChunk(
                type="done",
                usage=TokenUsage(input_tokens=100, output_tokens=10, thought_tokens=0),
            )
            return

        raise RuntimeError("400 invalid_request")


def _make_registry(tmp_path: Path) -> ToolRegistry:
    reg = ToolRegistry(audit_logger=AuditLogger(tmp_path / "logs" / "audit.jsonl"))
    reg.register(
        name="echo",
        description="Echo input",
        input_schema={"type": "object", "properties": {"x": {"type": "string"}}},
        risk_level=RiskLevel.READ_ONLY,
        handler=lambda x="": x,
    )
    return reg


async def test_agent_logs_turn_and_call_metrics_without_leaking_to_chat(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="clea.core.agent")
    memory = MemoryStore(base_dir=tmp_path)
    memory.ensure_initialized()
    agent = AgentLoop(
        provider=_MultiRoundUsageProvider(),
        registry=_make_registry(tmp_path),
        memory=memory,
    )
    session = Session(id="s-metrics")
    streamed: list[str] = []

    async def on_delta(text: str) -> None:
        streamed.append(text)

    reply = await agent.run_turn(session=session, user_text="test", on_text_delta=on_delta)
    assert reply == "Done!"
    assert "".join(streamed) == "Done!"

    messages = [rec.getMessage() for rec in caplog.records if rec.name == "clea.core.agent"]
    call_logs = [m for m in messages if m.startswith("model_call ")]
    turn_logs = [m for m in messages if m.startswith("turn_metrics ")]
    assert len(call_logs) == 2
    assert "round=1 status=ok" in call_logs[0]
    assert "input_tokens=110 output_tokens=12" in call_logs[0]
    assert "round=2 status=ok" in call_logs[1]
    assert "input_tokens=180 output_tokens=8" in call_logs[1]

    assert len(turn_logs) == 1
    turn_log = turn_logs[0]
    for field in (
        "status=ok",
        "total_ms=",
        "ttft_ms=",
        "model_calls=2",
        "input_tokens=290",
        "output_tokens=20",
        "tools_count=1",
        "system_prompt_tokens_est=",
        "history_messages=1",
        "history_tokens_est=",
    ):
        assert field in turn_log


async def test_agent_logs_failed_model_call_duration_and_status(
    tmp_path: Path,
    caplog: pytest.LogCaptureFixture,
) -> None:
    caplog.set_level(logging.INFO, logger="clea.core.agent")
    memory = MemoryStore(base_dir=tmp_path)
    memory.ensure_initialized()
    agent = AgentLoop(
        provider=_FailOnRound2Provider(),
        registry=_make_registry(tmp_path),
        memory=memory,
    )
    session = Session(id="s-fail")

    with pytest.raises(RuntimeError, match="400 invalid_request"):
        await agent.run_turn(session=session, user_text="test")

    messages = [rec.getMessage() for rec in caplog.records if rec.name == "clea.core.agent"]
    call_logs = [m for m in messages if m.startswith("model_call ")]
    turn_logs = [m for m in messages if m.startswith("turn_metrics ")]
    assert len(call_logs) == 2
    assert "round=1 status=ok" in call_logs[0]
    assert "round=2 status=error" in call_logs[1]

    assert len(turn_logs) == 1
    assert "status=error" in turn_logs[0]
    assert "model_calls=2" in turn_logs[0]
    assert ", " in turn_logs[0].split("call_durations_ms=[", 1)[1].split("]", 1)[0]

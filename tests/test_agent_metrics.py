"""Tests for Part A Step 1 per-turn and per-call timing/size logging."""

from __future__ import annotations

import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any

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


async def test_failed_turn_rollback_and_subsequent_success(tmp_path: Path) -> None:
    """Regression test: A failed turn rolls back session messages and the next turn succeeds."""
    memory = MemoryStore(base_dir=tmp_path)
    memory.ensure_initialized()

    class _FlakyProvider(LLMProvider):
        provider_name = "flaky"

        def __init__(self) -> None:
            self.turn_count = 0

        async def stream_turn(
            self,
            *,
            messages: list[Message],
            tools: list[Any],
            system_prompt: str,
        ):
            self.turn_count += 1
            if self.turn_count == 2:
                # Simulate a catastrophic failure on turn 2
                raise RuntimeError("500 internal server error")
            yield StreamChunk(type="text_delta", text=f"Answer {self.turn_count}")
            yield StreamChunk(type="done", usage=TokenUsage(input_tokens=10, output_tokens=5))

    flaky = _FlakyProvider()
    agent = AgentLoop(
        provider=flaky,
        registry=_make_registry(tmp_path),
        memory=memory,
    )
    session = Session(id="s-recovery")

    # Turn 1: Success
    res1 = await agent.run_turn(session=session, user_text="Hello 1")
    assert res1 == "Answer 1"
    assert len(session.messages) == 2
    assert session.messages[0].role == "user"
    assert session.messages[0].content == "Hello 1"
    assert session.messages[1].role == "assistant"
    assert session.messages[1].content == "Answer 1"

    # Turn 2: Fails
    with pytest.raises(RuntimeError, match="500 internal server error"):
        await agent.run_turn(session=session, user_text="Hello 2 (will fail)")

    # Messages must have been rolled back to turn 1!
    assert len(session.messages) == 2
    assert session.messages[0].content == "Hello 1"
    assert session.messages[1].content == "Answer 1"

    # Turn 3: Succeeds cleanly
    res3 = await agent.run_turn(session=session, user_text="Hello 3")
    assert res3 == "Answer 3"
    assert len(session.messages) == 4
    assert session.messages[2].role == "user"
    assert session.messages[2].content == "Hello 3"
    assert session.messages[3].role == "assistant"
    assert session.messages[3].content == "Answer 3"


async def test_session_history_cap_sliding_window(tmp_path: Path) -> None:
    """Session history is capped with sliding window and summary of older turns."""
    memory = MemoryStore(base_dir=tmp_path)
    memory.ensure_initialized()

    class _SimpleEchoProvider(LLMProvider):
        provider_name = "echo"

        async def stream_turn(
            self, *, messages: list[Message], tools: list[Any], system_prompt: str
        ):
            yield StreamChunk(type="text_delta", text=f"Echo {messages[-1].content}")
            yield StreamChunk(type="done", usage=TokenUsage(input_tokens=10, output_tokens=5))

    agent = AgentLoop(
        provider=_SimpleEchoProvider(),
        registry=_make_registry(tmp_path),
        memory=memory,
        max_history_messages=6,  # Cap at 6
    )
    session = Session(id="s-cap")

    # Run 5 turns (each turn adds user + assistant = 2 messages -> total 10 messages)
    for i in range(1, 6):
        await agent.run_turn(session=session, user_text=f"Turn {i}")

    # History cap should have compacted earlier messages
    # Must have summary of older turns in the first message
    assert len(session.messages) <= 6
    assert "[Earlier conversation summary:" in session.messages[0].content

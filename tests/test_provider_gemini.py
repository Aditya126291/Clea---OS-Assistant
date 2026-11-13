"""Unit tests for the GeminiProvider message/tool translation and config validation."""

from __future__ import annotations

import pytest

from clea.providers.base import Message, ToolCall
from clea.providers.gemini import GeminiProvider, _to_gemini_steps, _to_gemini_tools
from clea.tools.registry import RiskLevel, ToolDefinition


def test_gemini_provider_requires_api_key(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    with pytest.raises(ValueError, match="GEMINI_API_KEY"):
        GeminiProvider()


def test_to_gemini_tools_and_steps() -> None:
    tools = [
        ToolDefinition(
            name="get_system_info",
            description="Get system info",
            input_schema={"type": "object", "properties": {}},
            risk_level=RiskLevel.READ_ONLY,
        )
    ]
    gemini_tools = _to_gemini_tools(tools)
    assert gemini_tools == [
        {
            "type": "function",
            "name": "get_system_info",
            "description": "Get system info",
            "parameters": {"type": "object", "properties": {}},
        }
    ]

    messages = [
        Message(role="user", content="Hi"),
        Message(
            role="assistant",
            content="",
            tool_calls=[ToolCall(id="c1", name="get_system_info", arguments={})],
        ),
        Message(
            role="tool",
            content='{"os": "Windows"}',
            tool_call_id="c1",
            tool_name="get_system_info",
        ),
    ]
    steps = _to_gemini_steps(messages)
    assert steps[0]["type"] == "user_input"
    assert steps[1]["type"] == "function_call"
    assert steps[2]["type"] == "function_result"
    assert steps[2]["call_id"] == "c1"


async def test_gemini_provider_extracts_and_sums_usage_across_hops(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    events = [
        SimpleNamespace(event_type="interaction.created"),
        SimpleNamespace(
            event_type="step.delta",
            index=0,
            delta=SimpleNamespace(type="text", text="Part 1 "),
        ),
        SimpleNamespace(
            event_type="interaction.completed",
            interaction=SimpleNamespace(
                status="requires_continuation",
                usage=SimpleNamespace(
                    total_input_tokens=100,
                    total_output_tokens=20,
                    total_thought_tokens=0,
                    total_cached_tokens=0,
                    total_tokens=120,
                ),
                steps=[],
            ),
        ),
        SimpleNamespace(event_type="interaction.created"),
        SimpleNamespace(
            event_type="step.delta",
            index=0,
            delta=SimpleNamespace(type="text", text="Part 2"),
        ),
        SimpleNamespace(
            event_type="interaction.completed",
            interaction=SimpleNamespace(
                status="completed",
                usage=SimpleNamespace(
                    total_input_tokens=120,
                    total_output_tokens=15,
                    total_thought_tokens=5,
                    total_cached_tokens=10,
                    total_tokens=150,
                ),
                steps=[],
            ),
        ),
    ]

    class _FakeStream:
        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            for ev in events:
                yield ev

    class _FakeInteractions:
        async def create(self, **_kwargs):
            return _FakeStream()

    provider = GeminiProvider(api_key="test-key", model="gemini-3.5-flash-lite")
    monkeypatch.setattr(
        provider,
        "_client",
        SimpleNamespace(aio=SimpleNamespace(interactions=_FakeInteractions())),
    )

    chunks = [
        chunk
        async for chunk in provider.stream_turn(
            messages=[Message(role="user", content="hi")],
            tools=[],
            system_prompt="You are Clea.",
        )
    ]

    assert [c.text for c in chunks if c.type == "text_delta"] == ["Part 1 ", "Part 2"]
    done = next(c for c in chunks if c.type == "done")
    assert done.usage is not None
    assert done.usage.input_tokens == 220
    assert done.usage.output_tokens == 35
    assert done.usage.thought_tokens == 5
    assert done.usage.cached_tokens == 10
    assert done.usage.total_tokens == 270


async def test_gemini_provider_captures_thought_signature_and_retries(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from types import SimpleNamespace

    attempts = 0

    class _FakeFailingFirstStream:
        def __aiter__(self):
            return self

        async def __anext__(self):
            raise TimeoutError("First event timed out")

    class _FakeSuccessStream:
        def __aiter__(self):
            return self._gen()

        async def _gen(self):
            yield SimpleNamespace(
                event_type="step.start",
                index=0,
                step=SimpleNamespace(
                    type="function_call",
                    id="call_1",
                    name="test_tool",
                    arguments={"q": "clea"},
                    thought_signature="sig_step_start_123",
                ),
            )
            yield SimpleNamespace(
                event_type="interaction.completed",
                interaction=SimpleNamespace(
                    status="completed",
                    usage=None,
                    steps=[
                        SimpleNamespace(
                            type="function_call",
                            id="call_1",
                            name="test_tool",
                            arguments={"q": "clea"},
                            thought_signature="sig_completed_456",
                        )
                    ],
                ),
            )

    class _FakeInteractions:
        async def create(self, **_kwargs):
            nonlocal attempts
            attempts += 1
            if attempts == 1:
                return _FakeFailingFirstStream()
            return _FakeSuccessStream()

    provider = GeminiProvider(
        api_key="test-key",
        model="gemini-3.5-flash-lite",
        first_event_timeout_s=0.1,
        max_retries=1,
    )
    monkeypatch.setattr(
        provider,
        "_client",
        SimpleNamespace(aio=SimpleNamespace(interactions=_FakeInteractions())),
    )

    chunks = [
        chunk
        async for chunk in provider.stream_turn(
            messages=[Message(role="user", content="hi")],
            tools=[],
            system_prompt="You are Clea.",
        )
    ]

    assert attempts == 2
    tool_chunk = next(c for c in chunks if c.type == "tool_call")
    assert tool_chunk.thought_signature == "sig_completed_456"
    done_chunk = next(c for c in chunks if c.type == "done")
    assert done_chunk.thought_signature == "sig_completed_456"

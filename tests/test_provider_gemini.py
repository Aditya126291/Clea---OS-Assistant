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

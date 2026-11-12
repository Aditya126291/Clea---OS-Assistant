"""Gemini API provider using the google-genai Interactions API."""

from __future__ import annotations

import json
import os
from collections.abc import AsyncIterator
from typing import Any

from google import genai

from clea.providers.base import LLMProvider, Message, StreamChunk, ToolCall
from clea.tools.registry import ToolDefinition

DEFAULT_GEMINI_MODEL = "gemini-3.8-flash"


def _to_gemini_tools(tools: list[ToolDefinition]) -> list[dict[str, Any]]:
    return [
        {
            "type": "function",
            "name": tool.name,
            "description": tool.description,
            "parameters": tool.input_schema,
        }
        for tool in tools
    ]


def _to_gemini_steps(messages: list[Message]) -> list[dict[str, Any]]:
    steps: list[dict[str, Any]] = []
    for msg in messages:
        if msg.role == "user":
            steps.append(
                {
                    "type": "user_input",
                    "content": [{"type": "text", "text": msg.content}],
                }
            )
        elif msg.role == "assistant":
            if msg.content:
                steps.append(
                    {
                        "type": "model_output",
                        "content": [{"type": "text", "text": msg.content}],
                    }
                )
            for call in msg.tool_calls:
                steps.append(
                    {
                        "type": "function_call",
                        "id": call.id,
                        "name": call.name,
                        "arguments": call.arguments,
                    }
                )
        elif msg.role == "tool":
            steps.append(
                {
                    "type": "function_result",
                    "call_id": msg.tool_call_id or "",
                    "name": msg.tool_name or "",
                    "result": msg.content,
                    "is_error": msg.is_error,
                }
            )
    return steps


class GeminiProvider(LLMProvider):
    """LLMProvider backed by Google's Gemini Interactions API."""

    provider_name = "gemini"

    @property
    def model_name(self) -> str:
        return self.model

    def __init__(
        self,
        *,
        api_key: str | None = None,
        model: str | None = None,
    ) -> None:
        resolved_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not resolved_key:
            raise ValueError(
                "GEMINI_API_KEY is not set. Add it to .env or your environment before starting."
            )
        self.model = model or os.environ.get("CLEA_MODEL", DEFAULT_GEMINI_MODEL)
        self._client = genai.Client(api_key=resolved_key)

    async def stream_turn(
        self,
        *,
        messages: list[Message],
        tools: list[ToolDefinition],
        system_prompt: str,
    ) -> AsyncIterator[StreamChunk]:
        """Stream one model response turn, yielding text deltas and tool calls."""
        kwargs: dict[str, Any] = {
            "model": self.model,
            "input": _to_gemini_steps(messages),
            "system_instruction": system_prompt,
            "stream": True,
            "store": False,
        }
        if tools:
            kwargs["tools"] = _to_gemini_tools(tools)

        stream = await self._client.aio.interactions.create(**kwargs)

        pending_calls: dict[int, dict[str, Any]] = {}
        final_tool_calls: list[ToolCall] = []

        async for event in stream:
            event_type = getattr(event, "event_type", None)

            if event_type == "step.start":
                step = getattr(event, "step", None)
                if getattr(step, "type", None) == "function_call":
                    pending_calls[event.index] = {
                        "id": step.id,
                        "name": step.name,
                        "arguments": dict(step.arguments or {}),
                        "args_chunks": [],
                    }

            elif event_type == "step.delta":
                delta = getattr(event, "delta", None)
                delta_type = getattr(delta, "type", None)
                if delta_type == "text" and getattr(delta, "text", ""):
                    yield StreamChunk(type="text_delta", text=delta.text)
                elif delta_type == "arguments_delta" and getattr(delta, "arguments", None):
                    entry = pending_calls.get(event.index)
                    if entry is not None:
                        entry["args_chunks"].append(delta.arguments)

            elif event_type == "interaction.completed":
                interaction = getattr(event, "interaction", None)
                steps = getattr(interaction, "steps", None) or []
                for step in steps:
                    if getattr(step, "type", None) == "function_call":
                        final_tool_calls.append(
                            ToolCall(
                                id=step.id,
                                name=step.name,
                                arguments=dict(step.arguments or {}),
                            )
                        )

        if not final_tool_calls and pending_calls:
            for idx in sorted(pending_calls):
                entry = pending_calls[idx]
                raw_args = "".join(entry["args_chunks"]).strip()
                parsed_args = json.loads(raw_args) if raw_args else entry["arguments"]
                final_tool_calls.append(
                    ToolCall(
                        id=entry["id"],
                        name=entry["name"],
                        arguments=parsed_args,
                    )
                )

        for call in final_tool_calls:
            yield StreamChunk(type="tool_call", tool_call=call)

        yield StreamChunk(type="done")

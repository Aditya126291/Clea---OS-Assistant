"""Gemini API provider using the google-genai Interactions API."""

from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from collections.abc import AsyncIterator
from datetime import datetime
from typing import Any

from google import genai

from clea.providers.base import LLMProvider, Message, StreamChunk, TokenUsage, ToolCall
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
            if getattr(msg, "thought_signature", None):
                steps.append(
                    {
                        "type": "thought",
                        "signature": msg.thought_signature,
                    }
                )
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


def _merge_token_usage(acc: TokenUsage | None, hop: TokenUsage) -> TokenUsage:
    if acc is None:
        return hop

    def _add(a: int | None, b: int | None) -> int | None:
        if a is None and b is None:
            return None
        return (a or 0) + (b or 0)

    return TokenUsage(
        input_tokens=_add(acc.input_tokens, hop.input_tokens),
        output_tokens=_add(acc.output_tokens, hop.output_tokens),
        thought_tokens=_add(acc.thought_tokens, hop.thought_tokens),
        cached_tokens=_add(acc.cached_tokens, hop.cached_tokens),
        total_tokens=_add(acc.total_tokens, hop.total_tokens),
    )


logger = logging.getLogger(__name__)

DEFAULT_FIRST_EVENT_TIMEOUT = 7.0


class _NullAsyncContext:
    def __init__(self, val: Any) -> None:
        self.val = val

    async def __aenter__(self) -> Any:
        return self.val

    async def __aexit__(self, *args: Any) -> None:
        pass


def _extract_headers(stream_obj: Any) -> dict[str, str]:
    for attr in ("headers", "_headers", "response", "_response", "raw_response"):
        val = getattr(stream_obj, attr, None)
        if val is not None:
            if hasattr(val, "headers") and val.headers is not None:
                return dict(val.headers)
            if isinstance(val, dict):
                return {str(k): str(v) for k, v in val.items()}
    return {}


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
        first_event_timeout_s: float | None = None,
        max_retries: int = 1,
    ) -> None:
        resolved_key = api_key or os.environ.get("GEMINI_API_KEY")
        if not resolved_key:
            raise ValueError(
                "GEMINI_API_KEY is not set. Add it to .env or your environment before starting."
            )
        self.model = model or os.environ.get("CLEA_MODEL", DEFAULT_GEMINI_MODEL)
        if first_event_timeout_s is not None:
            self.first_event_timeout_s = first_event_timeout_s
        else:
            env_timeout = os.environ.get("CLEA_FIRST_EVENT_TIMEOUT")
            self.first_event_timeout_s = (
                float(env_timeout) if env_timeout is not None else DEFAULT_FIRST_EVENT_TIMEOUT
            )
        self.max_retries = max_retries
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

        max_attempts = 1 + max(0, self.max_retries)
        stream_ctx = None
        stream_iter = None
        first_event = None
        has_first_event = False

        for attempt in range(1, max_attempts + 1):
            attempt_start = time.perf_counter()
            attempt_ctx = None
            try:
                raw_stream = await self._client.aio.interactions.create(**kwargs)
                headers = _extract_headers(raw_stream)
                gemini_headers = {
                    k: v for k, v in headers.items() if k.lower().startswith("x-gemini-")
                }
                logger.info(
                    "Gemini model response attempt=%d timestamp=%s headers=%s x_gemini_headers=%s",
                    attempt,
                    datetime.now().astimezone().isoformat(),
                    headers,
                    gemini_headers,
                )

                attempt_ctx = (
                    raw_stream
                    if hasattr(raw_stream, "__aenter__")
                    else _NullAsyncContext(raw_stream)
                )
                stream_obj = await attempt_ctx.__aenter__()

                if hasattr(stream_obj, "__aiter__"):
                    aiter = stream_obj.__aiter__()
                else:
                    aiter = stream_obj

                try:
                    first_event = await asyncio.wait_for(
                        aiter.__anext__(),
                        timeout=self.first_event_timeout_s,
                    )
                    has_first_event = True
                except StopAsyncIteration:
                    has_first_event = False
                    first_event = None

                stream_ctx = attempt_ctx
                stream_iter = aiter
                break
            except Exception as exc:
                duration_s = time.perf_counter() - attempt_start
                now_iso = datetime.now().astimezone().isoformat()
                if attempt_ctx is not None:
                    try:
                        await attempt_ctx.__aexit__(type(exc), exc, None)
                    except Exception:
                        pass
                if attempt < max_attempts:
                    logger.warning(
                        "Gemini stream first-event failed on attempt %d/%d after %.2fs (%s). "
                        "Retrying model call once... timestamp=%s",
                        attempt,
                        max_attempts,
                        duration_s,
                        exc or "timeout",
                        now_iso,
                    )
                    continue
                else:
                    logger.error(
                        "Gemini stream first-event failed on attempt %d/%d after %.2fs (%s). "
                        "No more retries. timestamp=%s",
                        attempt,
                        max_attempts,
                        duration_s,
                        exc or "timeout",
                        now_iso,
                    )
                    raise

        pending_calls: dict[int, dict[str, Any]] = {}
        final_tool_calls: list[ToolCall] = []
        final_usage: TokenUsage | None = None
        captured_thought_signature: str | None = None

        def _handle_event(event: Any) -> list[StreamChunk]:
            nonlocal final_usage, captured_thought_signature
            chunks_to_yield: list[StreamChunk] = []
            event_type = getattr(event, "event_type", None)

            if event_type == "interaction.created":
                pending_calls.clear()

            elif event_type == "step.start":
                step = getattr(event, "step", None)
                sig = getattr(step, "thought_signature", None) or getattr(step, "signature", None)
                if sig:
                    captured_thought_signature = sig
                if getattr(step, "type", None) == "function_call":
                    pending_calls[event.index] = {
                        "id": step.id,
                        "name": step.name,
                        "arguments": dict(step.arguments or {}),
                        "args_chunks": [],
                    }

            elif event_type == "step.delta":
                delta = getattr(event, "delta", None)
                sig = (
                    getattr(delta, "thought_signature", None)
                    or getattr(delta, "signature", None)
                    or (
                        getattr(delta, "thought", None)
                        and getattr(delta.thought, "signature", None)
                    )
                )
                if sig:
                    captured_thought_signature = sig
                delta_type = getattr(delta, "type", None)
                if delta_type == "text" and getattr(delta, "text", ""):
                    chunks_to_yield.append(StreamChunk(type="text_delta", text=delta.text))
                elif delta_type == "arguments_delta" and getattr(delta, "arguments", None):
                    entry = pending_calls.get(event.index)
                    if entry is not None:
                        entry["args_chunks"].append(delta.arguments)

            elif event_type == "interaction.completed":
                interaction = getattr(event, "interaction", None)
                usage_obj = getattr(interaction, "usage", None)
                if usage_obj is not None:
                    hop_usage = TokenUsage(
                        input_tokens=getattr(usage_obj, "total_input_tokens", None),
                        output_tokens=getattr(usage_obj, "total_output_tokens", None),
                        thought_tokens=getattr(usage_obj, "total_thought_tokens", None),
                        cached_tokens=getattr(usage_obj, "total_cached_tokens", None),
                        total_tokens=getattr(usage_obj, "total_tokens", None),
                    )
                    final_usage = _merge_token_usage(final_usage, hop_usage)
                steps = getattr(interaction, "steps", None) or []
                for step in steps:
                    sig = getattr(step, "thought_signature", None) or getattr(
                        step, "signature", None
                    )
                    if sig:
                        captured_thought_signature = sig
                    if getattr(step, "type", None) == "function_call":
                        final_tool_calls.append(
                            ToolCall(
                                id=step.id,
                                name=step.name,
                                arguments=dict(step.arguments or {}),
                            )
                        )
            return chunks_to_yield

        try:
            if has_first_event and first_event is not None:
                for c in _handle_event(first_event):
                    yield c

            if stream_iter is not None:
                async for event in stream_iter:
                    for c in _handle_event(event):
                        yield c
        finally:
            if stream_ctx is not None:
                try:
                    await stream_ctx.__aexit__(None, None, None)
                except Exception:
                    pass

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
            yield StreamChunk(
                type="tool_call",
                tool_call=call,
                thought_signature=captured_thought_signature,
            )

        yield StreamChunk(
            type="done",
            usage=final_usage,
            thought_signature=captured_thought_signature,
        )

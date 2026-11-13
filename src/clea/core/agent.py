"""Agent execution loop coordinating the LLM provider, tools, and memory."""

from __future__ import annotations

import json
import logging
import time
from collections.abc import Awaitable, Callable
from typing import Any

from clea.core.sessions import Session
from clea.memory.store import MemoryStore
from clea.providers.base import LLMProvider, Message, TokenUsage, ToolCall
from clea.tools.registry import ConfirmationCallback, ToolRegistry

logger = logging.getLogger(__name__)


def _estimate_tokens(text: str) -> int:
    """Approximate token count (~4 UTF-8 chars per token) for diagnostics."""
    if not text:
        return 0
    return (len(text) + 3) // 4


def _estimate_history_tokens(messages: list[Message]) -> int:
    total = 0
    for msg in messages:
        total += _estimate_tokens(msg.content)
        if msg.tool_calls:
            total += _estimate_tokens(json.dumps([call.model_dump() for call in msg.tool_calls]))
    return total


TextDeltaHandler = Callable[[str], Awaitable[None]]
ToolStartHandler = Callable[[ToolCall], Awaitable[None]]
ToolEndHandler = Callable[[ToolCall, str, bool], Awaitable[None]]

_BASE_SYSTEM_PROMPT = (
    "You are Clea, a helpful, concise personal AI assistant running locally on the user's "
    "desktop. Use registered tools whenever they help answer accurately, and never fabricate "
    "tool outputs."
)


def _serialize_tool_output(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, default=str)


class AgentLoop:
    """Runs multi-step model and tool-calling turns for a session."""

    def __init__(
        self,
        *,
        provider: LLMProvider,
        registry: ToolRegistry,
        memory: MemoryStore,
        max_tool_rounds: int = 8,
    ) -> None:
        self.provider = provider
        self.registry = registry
        self.memory = memory
        self.max_tool_rounds = max_tool_rounds

    def _build_system_prompt(self) -> str:
        long_term = self.memory.read_long_term().strip()
        if not long_term:
            return _BASE_SYSTEM_PROMPT
        return f"{_BASE_SYSTEM_PROMPT}\n\n## Long-Term Memory\n{long_term}"

    async def run_turn(
        self,
        *,
        session: Session,
        user_text: str,
        on_text_delta: TextDeltaHandler | None = None,
        on_tool_start: ToolStartHandler | None = None,
        on_tool_end: ToolEndHandler | None = None,
        confirm: ConfirmationCallback | None = None,
    ) -> str:
        """Process one user message, execute requested tools, and log the conversation."""
        turn_start = time.perf_counter()
        ttft_ms: float | None = None
        call_durations_ms: list[float] = []
        call_usages: list[TokenUsage] = []
        model_calls = 0
        status = "error"

        session.messages.append(Message(role="user", content=user_text))
        tools = self.registry.list_definitions()
        system_prompt = self._build_system_prompt()
        initial_history_messages = len(session.messages)
        initial_history_tokens_est = _estimate_history_tokens(session.messages)
        system_prompt_tokens_est = _estimate_tokens(system_prompt)
        tools_schema_tokens_est = _estimate_tokens(json.dumps([t.model_dump() for t in tools]))

        try:
            for round_idx in range(self.max_tool_rounds):
                text_parts: list[str] = []
                tool_calls: list[ToolCall] = []
                call_usage: TokenUsage | None = None
                call_start = time.perf_counter()
                call_ttft_ms: float | None = None
                call_status = "error"
                model_calls += 1

                call_history_messages = len(session.messages)
                call_history_tokens_est = _estimate_history_tokens(session.messages)

                try:
                    async for chunk in self.provider.stream_turn(
                        messages=session.messages,
                        tools=tools,
                        system_prompt=system_prompt,
                    ):
                        if chunk.type == "text_delta" and chunk.text:
                            now = time.perf_counter()
                            if ttft_ms is None:
                                ttft_ms = (now - turn_start) * 1000.0
                            if call_ttft_ms is None:
                                call_ttft_ms = (now - call_start) * 1000.0
                            text_parts.append(chunk.text)
                            if on_text_delta is not None:
                                await on_text_delta(chunk.text)
                        elif chunk.type == "tool_call" and chunk.tool_call is not None:
                            tool_calls.append(chunk.tool_call)
                        elif chunk.type == "done" and chunk.usage is not None:
                            call_usage = chunk.usage
                    call_status = "ok"
                finally:
                    call_ms = (time.perf_counter() - call_start) * 1000.0
                    call_durations_ms.append(round(call_ms, 1))
                    if call_usage is not None:
                        call_usages.append(call_usage)

                    logger.info(
                        "model_call session=%s round=%d status=%s duration_ms=%.1f ttft_ms=%s "
                        "history_messages=%d history_tokens_est=%d tools=%d "
                        "input_tokens=%s output_tokens=%s thought_tokens=%s",
                        session.id,
                        round_idx + 1,
                        call_status,
                        call_ms,
                        f"{call_ttft_ms:.1f}" if call_ttft_ms is not None else "null",
                        call_history_messages,
                        call_history_tokens_est,
                        len(tools),
                        call_usage.input_tokens if call_usage else None,
                        call_usage.output_tokens if call_usage else None,
                        call_usage.thought_tokens if call_usage else None,
                    )

                turn_text = "".join(text_parts)

                if not tool_calls:
                    session.messages.append(Message(role="assistant", content=turn_text))
                    self.memory.append_daily_turn(
                        session_id=session.id,
                        user_text=user_text,
                        assistant_text=turn_text,
                    )
                    status = "ok"
                    return turn_text

                session.messages.append(
                    Message(role="assistant", content=turn_text, tool_calls=tool_calls)
                )

                for call in tool_calls:
                    if on_tool_start is not None:
                        await on_tool_start(call)
                    is_error = False
                    try:
                        raw_result = await self.registry.execute(
                            call.name,
                            call.arguments,
                            confirm=confirm,
                        )
                        output = _serialize_tool_output(raw_result)
                    except Exception as exc:
                        is_error = True
                        output = f"Tool error: {exc}"

                    if on_tool_end is not None:
                        await on_tool_end(call, output, is_error)

                    session.messages.append(
                        Message(
                            role="tool",
                            content=output,
                            tool_call_id=call.id,
                            tool_name=call.name,
                            is_error=is_error,
                        )
                    )

            fallback = "I reached the maximum number of tool steps without finishing."
            session.messages.append(Message(role="assistant", content=fallback))
            self.memory.append_daily_turn(
                session_id=session.id,
                user_text=user_text,
                assistant_text=fallback,
            )
            status = "max_tool_rounds"
            return fallback
        finally:
            total_ms = (time.perf_counter() - turn_start) * 1000.0
            input_tokens_sum = (
                sum(u.input_tokens for u in call_usages if u.input_tokens is not None)
                if any(u.input_tokens is not None for u in call_usages)
                else None
            )
            output_tokens_sum = (
                sum(u.output_tokens for u in call_usages if u.output_tokens is not None)
                if any(u.output_tokens is not None for u in call_usages)
                else None
            )
            thought_tokens_sum = (
                sum(u.thought_tokens for u in call_usages if u.thought_tokens is not None)
                if any(u.thought_tokens is not None for u in call_usages)
                else None
            )
            logger.info(
                "turn_metrics session=%s status=%s total_ms=%.1f ttft_ms=%s "
                "model_calls=%d call_durations_ms=%s input_tokens=%s output_tokens=%s "
                "thought_tokens=%s tools_count=%d tools_schema_tokens_est=%d "
                "system_prompt_tokens_est=%d history_messages=%d history_tokens_est=%d",
                session.id,
                status,
                total_ms,
                f"{ttft_ms:.1f}" if ttft_ms is not None else "null",
                model_calls,
                call_durations_ms,
                input_tokens_sum,
                output_tokens_sum,
                thought_tokens_sum,
                len(tools),
                tools_schema_tokens_est,
                system_prompt_tokens_est,
                initial_history_messages,
                initial_history_tokens_est,
            )

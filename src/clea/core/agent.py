"""Agent execution loop coordinating the LLM provider, tools, and memory."""

from __future__ import annotations

import json
from collections.abc import Awaitable, Callable
from typing import Any

from clea.core.sessions import Session
from clea.memory.store import MemoryStore
from clea.providers.base import LLMProvider, Message, ToolCall
from clea.tools.registry import ConfirmationCallback, ToolRegistry

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
        session.messages.append(Message(role="user", content=user_text))
        tools = self.registry.list_definitions()
        system_prompt = self._build_system_prompt()

        for _ in range(self.max_tool_rounds):
            text_parts: list[str] = []
            tool_calls: list[ToolCall] = []

            async for chunk in self.provider.stream_turn(
                messages=session.messages,
                tools=tools,
                system_prompt=system_prompt,
            ):
                if chunk.type == "text_delta" and chunk.text:
                    text_parts.append(chunk.text)
                    if on_text_delta is not None:
                        await on_text_delta(chunk.text)
                elif chunk.type == "tool_call" and chunk.tool_call is not None:
                    tool_calls.append(chunk.tool_call)

            turn_text = "".join(text_parts)

            if not tool_calls:
                session.messages.append(Message(role="assistant", content=turn_text))
                self.memory.append_daily_turn(
                    session_id=session.id,
                    user_text=user_text,
                    assistant_text=turn_text,
                )
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
        return fallback

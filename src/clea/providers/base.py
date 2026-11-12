"""Swappable LLM provider interface and provider-agnostic message models."""

from __future__ import annotations

from abc import ABC, abstractmethod
from collections.abc import AsyncIterator
from typing import Any, Literal

from pydantic import BaseModel, Field

from clea.tools.registry import ToolDefinition


class ToolCall(BaseModel):
    """A tool invocation requested by the model."""

    id: str
    name: str
    arguments: dict[str, Any] = Field(default_factory=dict)


class Message(BaseModel):
    """Provider-agnostic conversation message stored in a session."""

    role: Literal["user", "assistant", "tool"]
    content: str = ""
    tool_calls: list[ToolCall] = Field(default_factory=list)
    tool_call_id: str | None = None
    tool_name: str | None = None
    is_error: bool = False


class StreamChunk(BaseModel):
    """Incremental chunk emitted by an LLM provider during a streamed turn."""

    type: Literal["text_delta", "tool_call", "done"]
    text: str = ""
    tool_call: ToolCall | None = None


class LLMProvider(ABC):
    """Abstract interface allowing Clea to swap LLM backends without changing core."""

    provider_name: str = "unknown"
    """Short display name of the backend, shown read-only in Settings."""

    @property
    def model_name(self) -> str:
        """Display name of the model in use."""
        return "unknown"

    @abstractmethod
    def stream_turn(
        self,
        *,
        messages: list[Message],
        tools: list[ToolDefinition],
        system_prompt: str,
    ) -> AsyncIterator[StreamChunk]:
        """Stream text deltas and any requested tool calls for the current turn."""
        raise NotImplementedError

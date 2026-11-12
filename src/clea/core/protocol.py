"""Typed WebSocket protocol frames shared between the core daemon and surfaces.

This module intentionally has zero dependencies on core internals so that
CLI, voice, and desktop UI surfaces can import it without coupling to the daemon.
"""

from __future__ import annotations

from typing import Annotated, Any, Literal
from uuid import uuid4

from pydantic import BaseModel, Field, TypeAdapter

ProtocolMethod = Literal["chat.send", "session.new", "health.ping"]
ProtocolEvent = Literal["text.delta", "tool.start", "tool.end", "turn.done", "error"]


def new_id() -> str:
    """Generate a unique identifier for protocol requests and sessions."""
    return uuid4().hex


class RequestFrame(BaseModel):
    """Client-to-daemon request message."""

    frame_type: Literal["request"] = "request"
    id: str = Field(default_factory=new_id)
    method: ProtocolMethod
    session_id: str | None = None
    params: dict[str, Any] = Field(default_factory=dict)


class ResponseFrame(BaseModel):
    """Daemon-to-client response paired with a request ID."""

    frame_type: Literal["response"] = "response"
    id: str
    ok: bool = True
    session_id: str | None = None
    result: dict[str, Any] = Field(default_factory=dict)
    error: str | None = None


class EventFrame(BaseModel):
    """Daemon-to-client asynchronous streaming event."""

    frame_type: Literal["event"] = "event"
    event: ProtocolEvent
    request_id: str | None = None
    session_id: str | None = None
    data: dict[str, Any] = Field(default_factory=dict)


ProtocolFrame = Annotated[
    RequestFrame | ResponseFrame | EventFrame,
    Field(discriminator="frame_type"),
]

_FRAME_ADAPTER: TypeAdapter[ProtocolFrame] = TypeAdapter(ProtocolFrame)


def parse_frame(raw: str | bytes) -> ProtocolFrame:
    """Parse and validate a raw JSON WebSocket message into a typed frame."""
    return _FRAME_ADAPTER.validate_json(raw)

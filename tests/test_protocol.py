"""Tests for the typed WebSocket protocol frames."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from clea.core.protocol import EventFrame, RequestFrame, ResponseFrame, parse_frame


def test_request_frame_roundtrip() -> None:
    req = RequestFrame(method="chat.send", session_id="sess-1", params={"text": "hello"})
    parsed = parse_frame(req.model_dump_json())
    assert isinstance(parsed, RequestFrame)
    assert parsed.frame_type == "request"
    assert parsed.method == "chat.send"
    assert parsed.session_id == "sess-1"
    assert parsed.params == {"text": "hello"}


def test_response_frame_roundtrip() -> None:
    resp = ResponseFrame(id="req-1", ok=True, session_id="sess-1", result={"text": "hi"})
    parsed = parse_frame(resp.model_dump_json())
    assert isinstance(parsed, ResponseFrame)
    assert parsed.frame_type == "response"
    assert parsed.id == "req-1"
    assert parsed.ok is True
    assert parsed.result == {"text": "hi"}


def test_event_frame_roundtrip() -> None:
    event = EventFrame(
        event="text.delta",
        request_id="req-1",
        session_id="sess-1",
        data={"text": "hel"},
    )
    parsed = parse_frame(event.model_dump_json())
    assert isinstance(parsed, EventFrame)
    assert parsed.frame_type == "event"
    assert parsed.event == "text.delta"
    assert parsed.data == {"text": "hel"}


def test_invalid_frame_rejected() -> None:
    with pytest.raises(ValidationError):
        parse_frame('{"frame_type": "unknown", "id": "1"}')

    with pytest.raises(ValidationError):
        parse_frame('{"frame_type": "request", "method": "unsupported.method"}')

"""Tests for GeminiLiveBackend event mapping, configuration, and encapsulation."""

from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from google.genai import types

from clea.surfaces.voice.backends.base import (
    AudioOutputEvent,
    ContextCompressionConfig,
    GoAwayEvent,
    InterruptedEvent,
    SessionResumptionConfig,
    SessionResumptionUpdateEvent,
    ToolCallCancellationEvent,
    ToolCallEvent,
    TranscriptOutputEvent,
    TurnCompleteEvent,
    UsageUpdateEvent,
    VoiceSessionConfig,
    VoiceToolDeclaration,
    VoiceToolResponse,
)
from clea.surfaces.voice.backends.gemini_live import GeminiLiveBackend


def test_gemini_backend_requires_api_key_when_no_client() -> None:
    backend = GeminiLiveBackend(api_key="")
    cfg = VoiceSessionConfig(system_instruction="sys")
    with pytest.raises(ValueError, match="GEMINI_API_KEY is not set"):
        asyncio.run(backend.connect(cfg))


async def test_gemini_backend_connect_builds_correct_live_config() -> None:
    mock_session = MagicMock()
    mock_session.send_tool_response = AsyncMock()

    # Empty async generator for receive
    async def empty_recv() -> Any:
        if False:
            yield None

    mock_session.receive.side_effect = empty_recv

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_session
    mock_ctx.__aexit__.return_value = None

    mock_client = MagicMock()
    mock_client.aio.live.connect.return_value = mock_ctx

    backend = GeminiLiveBackend(api_key="fake-key", client=mock_client)

    session_cfg = VoiceSessionConfig(
        system_instruction="Hello Clea voice",
        tools=[
            VoiceToolDeclaration(
                name="test_tool",
                description="desc",
                parameters_schema={"type": "object"},
                behavior="non_blocking",
            )
        ],
        enable_web_search=True,
        media_resolution="high",
        session_resumption=SessionResumptionConfig(enabled=True, handle="prev-handle"),
        context_compression=ContextCompressionConfig(trigger_tokens=2000, target_tokens=1000),
    )

    await backend.connect(session_cfg)

    # Check client.aio.live.connect call
    assert mock_client.aio.live.connect.called
    kwargs = mock_client.aio.live.connect.call_args.kwargs
    live_config: types.LiveConnectConfig = kwargs["config"]

    assert live_config.response_modalities == [types.Modality.AUDIO]
    assert live_config.system_instruction.parts[0].text == "Hello Clea voice"
    assert live_config.media_resolution == types.MediaResolution.MEDIA_RESOLUTION_HIGH
    assert live_config.session_resumption.handle == "prev-handle"
    assert live_config.context_window_compression.trigger_tokens == 2000
    assert live_config.context_window_compression.sliding_window.target_tokens == 1000
    assert live_config.input_audio_transcription is not None
    assert live_config.output_audio_transcription is not None

    # Tools should contain function declaration with NON_BLOCKING and google_search
    assert len(live_config.tools) == 2
    func_tool = next(t for t in live_config.tools if t.function_declarations)
    assert func_tool.function_declarations[0].name == "test_tool"
    assert func_tool.function_declarations[0].behavior == types.Behavior.NON_BLOCKING

    search_tool = next(t for t in live_config.tools if t.google_search)
    assert search_tool.google_search is not None

    await backend.close()


async def test_gemini_backend_maps_all_server_messages_to_normalized_events() -> None:
    # Prepare server messages
    msg1 = types.LiveServerMessage(
        server_content=types.LiveServerContent(
            model_turn=types.Content(
                parts=[
                    types.Part(
                        inline_data=types.Blob(
                            data=b"\x01\x02" * 120, mime_type="audio/pcm;rate=24000"
                        )
                    ),
                    types.Part(text="Assistant speaking text"),
                ]
            )
        )
    )
    msg2 = types.LiveServerMessage(
        server_content=types.LiveServerContent(
            input_transcription=types.Transcription(text="User spoke this", finished=True),
            output_transcription=types.Transcription(text="Assistant final text", finished=True),
            interrupted=True,
            turn_complete=True,
        )
    )
    msg3 = types.LiveServerMessage(
        tool_call=types.LiveServerToolCall(
            function_calls=[
                types.FunctionCall(
                    id="c-1", name="delegate_task", args={"description": "clean desk"}
                )
            ]
        )
    )
    msg4 = types.LiveServerMessage(
        tool_call_cancellation=types.LiveServerToolCallCancellation(ids=["c-1"])
    )
    msg5 = types.LiveServerMessage(
        session_resumption_update=types.LiveServerSessionResumptionUpdate(
            new_handle="handle-xyz",
            resumable=True,
        )
    )
    msg6 = types.LiveServerMessage(go_away=types.LiveServerGoAway(time_left="15s"))
    msg7 = types.LiveServerMessage(
        usage_metadata=types.UsageMetadata(
            prompt_token_count=100,
            response_token_count=50,
            total_token_count=150,
            cached_content_token_count=20,
            thoughts_token_count=10,
        )
    )

    async def fake_stream() -> Any:
        for m in [msg1, msg2, msg3, msg4, msg5, msg6, msg7]:
            yield m

    mock_session = MagicMock()
    mock_session.receive.side_effect = fake_stream

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_session
    mock_ctx.__aexit__.return_value = None

    mock_client = MagicMock()
    mock_client.aio.live.connect.return_value = mock_ctx

    backend = GeminiLiveBackend(api_key="test-key", client=mock_client)
    await backend.connect(VoiceSessionConfig(system_instruction="sys"))

    # Give receive loop a moment to process stream
    await asyncio.sleep(0.1)

    events = []
    # Collect events from queue without blocking
    while not backend._events_queue.empty():
        item = backend._events_queue.get_nowait()
        if item is not None:
            events.append(item)

    types_received = [e.type for e in events]
    assert types_received == [
        "audio_output",
        "transcript",
        "interrupted",
        "transcript",
        "transcript",
        "turn_complete",
        "tool_call",
        "tool_call_cancellation",
        "session_resumption_update",
        "go_away",
        "usage",
    ]

    assert isinstance(events[0], AudioOutputEvent)
    assert events[0].pcm_bytes == b"\x01\x02" * 120
    assert events[0].sample_rate_hz == 24_000

    assert isinstance(events[2], InterruptedEvent)
    assert isinstance(events[3], TranscriptOutputEvent) and events[3].role == "user"
    assert isinstance(events[4], TranscriptOutputEvent) and events[4].role == "assistant"
    assert isinstance(events[5], TurnCompleteEvent)

    assert isinstance(events[6], ToolCallEvent)
    assert events[6].call_id == "c-1"
    assert events[6].name == "delegate_task"
    assert events[6].arguments == {"description": "clean desk"}

    assert isinstance(events[7], ToolCallCancellationEvent)
    assert events[7].call_ids == ["c-1"]

    assert isinstance(events[8], SessionResumptionUpdateEvent)
    assert events[8].handle == "handle-xyz"
    assert backend.resumption_handle == "handle-xyz"

    assert isinstance(events[9], GoAwayEvent)
    assert events[9].time_left_seconds == 15.0

    assert isinstance(events[10], UsageUpdateEvent)
    assert events[10].input_tokens == 100
    assert events[10].output_tokens == 50
    assert events[10].total_tokens == 150
    assert events[10].cached_tokens == 20
    assert events[10].thought_tokens == 10

    # Verify zero google.genai types in events
    for e in events:
        assert not type(e).__module__.startswith("google")

    await backend.close()


async def test_gemini_backend_send_tool_responses_maps_scheduling() -> None:
    mock_session = MagicMock()
    mock_session.send_tool_response = AsyncMock()

    async def empty_recv() -> Any:
        if False:
            yield None

    mock_session.receive.side_effect = empty_recv
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_session
    mock_ctx.__aexit__.return_value = None
    mock_client = MagicMock()
    mock_client.aio.live.connect.return_value = mock_ctx

    backend = GeminiLiveBackend(api_key="k", client=mock_client)
    await backend.connect(VoiceSessionConfig(system_instruction="s"))

    responses = [
        VoiceToolResponse(
            call_id="call-1",
            name="look_at_screen",
            response={"ok": True},
            scheduling="when_idle",
        ),
        VoiceToolResponse(
            call_id="call-2",
            name="end_call",
            response={"reason": "bye"},
            scheduling="interrupt",
        ),
        VoiceToolResponse(
            call_id="call-3",
            name="delegate_task",
            response={"error": "denied"},
            is_error=True,
            scheduling="silent",
        ),
    ]

    await backend.send_tool_responses(responses)
    assert mock_session.send_tool_response.called
    kwargs = mock_session.send_tool_response.call_args.kwargs
    sent_list: list[types.FunctionResponse] = kwargs["function_responses"]

    assert len(sent_list) == 3
    assert sent_list[0].id == "call-1"
    assert sent_list[0].scheduling == types.FunctionResponseScheduling.WHEN_IDLE
    assert sent_list[1].id == "call-2"
    assert sent_list[1].scheduling == types.FunctionResponseScheduling.INTERRUPT
    assert sent_list[2].id == "call-3"
    assert sent_list[2].scheduling == types.FunctionResponseScheduling.SILENT
    assert sent_list[2].response == {"error": "denied"}

    await backend.close()

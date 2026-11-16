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
    VoiceToolMediaPart,
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

    delivered = False

    async def fake_stream() -> Any:
        nonlocal delivered
        if not delivered:
            delivered = True
            for m in [msg1, msg2, msg3, msg4, msg5, msg6, msg7]:
                yield m
        else:
            await asyncio.sleep(10.0)

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
    assert sent_list[0].parts is None
    assert sent_list[1].id == "call-2"
    assert sent_list[1].scheduling == types.FunctionResponseScheduling.INTERRUPT
    assert sent_list[2].id == "call-3"
    assert sent_list[2].scheduling == types.FunctionResponseScheduling.SILENT
    assert sent_list[2].response == {"error": "denied"}

    await backend.close()


async def test_gemini_backend_send_tool_responses_attaches_media_parts() -> None:
    mock_session = MagicMock()
    mock_session.send_tool_response = AsyncMock()
    mock_session.send_realtime_input = AsyncMock()

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
            call_id="call-img-1",
            name="look_at_screen",
            response={"status": "success", "active_app_name": "Google Antigravity"},
            media_parts=[
                VoiceToolMediaPart(data=b"jpeg-desktop", mime_type="image/jpeg"),
                VoiceToolMediaPart(data=b"png-window", mime_type="image/png"),
            ],
            scheduling="when_idle",
        ),
        VoiceToolResponse(
            call_id="call-img-err",
            name="look_at_screen",
            response={"error": "blocked"},
            media_parts=[VoiceToolMediaPart(data=b"should-not-send", mime_type="image/png")],
            is_error=True,
            scheduling="when_idle",
        ),
    ]

    await backend.send_tool_responses(responses)
    assert mock_session.send_realtime_input.call_count == 0
    assert mock_session.send_tool_response.called

    sent_list: list[types.FunctionResponse] = mock_session.send_tool_response.call_args.kwargs[
        "function_responses"
    ]
    assert len(sent_list) == 2

    ok_resp = sent_list[0]
    assert ok_resp.id == "call-img-1"
    assert ok_resp.parts is not None
    assert len(ok_resp.parts) == 2
    assert ok_resp.parts[0].inline_data == types.FunctionResponseBlob(
        mime_type="image/jpeg",
        data=b"jpeg-desktop",
    )
    assert ok_resp.parts[1].inline_data == types.FunctionResponseBlob(
        mime_type="image/png",
        data=b"png-window",
    )

    err_resp = sent_list[1]
    assert err_resp.id == "call-img-err"
    assert err_resp.response == {"error": "blocked"}
    assert err_resp.parts is None

    await backend.close()


async def test_gemini_backend_reconnect_cancels_existing_receive_task() -> None:
    """reconnect() must cancel the old receive loop and open a fresh session cleanly."""
    mock_session = MagicMock()

    async def empty_recv() -> Any:
        # Long-running generator simulating active connection
        while True:
            await asyncio.sleep(1.0)
            yield types.LiveServerMessage()

    mock_session.receive.side_effect = empty_recv
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_session
    mock_ctx.__aexit__.return_value = None
    mock_client = MagicMock()
    mock_client.aio.live.connect.return_value = mock_ctx

    backend = GeminiLiveBackend(api_key="fake-key", client=mock_client)
    await backend.connect(VoiceSessionConfig(system_instruction="sys"))
    first_task = backend._receive_task
    assert first_task is not None
    assert not first_task.done()

    # Reconnect
    await backend.reconnect()
    second_task = backend._receive_task

    assert second_task is not None
    assert second_task is not first_task
    assert first_task.cancelled() or first_task.done()

    await backend.close()


async def test_gemini_backend_receives_across_multiple_turns() -> None:
    """Backend must keep calling session.receive() after Turn 1 turn_complete."""
    turn_index = 0
    turn2_audio_sent = asyncio.Event()
    turn3_audio_sent = asyncio.Event()
    sent_blobs: list[Any] = []

    async def fake_send_realtime_input(*, audio: Any = None, **_kwargs: Any) -> None:
        if audio is not None:
            sent_blobs.append(audio)
            if len(sent_blobs) == 2:
                turn2_audio_sent.set()
            elif len(sent_blobs) >= 3:
                turn3_audio_sent.set()

    async def multi_turn_receive() -> Any:
        nonlocal turn_index
        turn_index += 1
        current_turn = turn_index
        if current_turn == 1:
            yield types.LiveServerMessage(
                server_content=types.LiveServerContent(
                    model_turn=types.Content(
                        parts=[
                            types.Part(
                                inline_data=types.Blob(
                                    data=b"\x11\x00" * 120, mime_type="audio/pcm;rate=24000"
                                )
                            )
                        ]
                    )
                )
            )
            yield types.LiveServerMessage(
                server_content=types.LiveServerContent(turn_complete=True)
            )
            return
        if current_turn == 2:
            await turn2_audio_sent.wait()
            yield types.LiveServerMessage(
                server_content=types.LiveServerContent(
                    model_turn=types.Content(
                        parts=[
                            types.Part(
                                inline_data=types.Blob(
                                    data=b"\x22\x00" * 120, mime_type="audio/pcm;rate=24000"
                                )
                            )
                        ]
                    )
                )
            )
            yield types.LiveServerMessage(
                server_content=types.LiveServerContent(turn_complete=True)
            )
            return
        if current_turn == 3:
            await turn3_audio_sent.wait()
            yield types.LiveServerMessage(
                server_content=types.LiveServerContent(
                    model_turn=types.Content(
                        parts=[
                            types.Part(
                                inline_data=types.Blob(
                                    data=b"\x33\x00" * 120, mime_type="audio/pcm;rate=24000"
                                )
                            )
                        ]
                    )
                )
            )
            yield types.LiveServerMessage(
                server_content=types.LiveServerContent(turn_complete=True)
            )
            return
        await asyncio.sleep(10.0)

    mock_session = MagicMock()
    mock_session.receive.side_effect = multi_turn_receive
    mock_session.send_realtime_input = AsyncMock(side_effect=fake_send_realtime_input)

    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_session
    mock_ctx.__aexit__.return_value = None
    mock_client = MagicMock()
    mock_client.aio.live.connect.return_value = mock_ctx

    backend = GeminiLiveBackend(api_key="fake-key", client=mock_client)
    await backend.connect(VoiceSessionConfig(system_instruction="sys"))

    collected: list[Any] = []

    async def collect_six() -> None:
        async for evt in backend.events():
            collected.append(evt)
            if len(collected) == 6:
                break

    collector = asyncio.create_task(collect_six())

    # Turn 1 audio -> receive Turn 1 audio + turn_complete
    await backend.send_audio(b"\x01\x00" * 320)
    await asyncio.sleep(0.05)

    # Turn 2 audio -> receive Turn 2 audio + turn_complete
    await backend.send_audio(b"\x02\x00" * 320)
    await asyncio.sleep(0.05)

    # Turn 3 audio -> receive Turn 3 audio + turn_complete
    await backend.send_audio(b"\x03\x00" * 320)
    await asyncio.wait_for(collector, timeout=2.0)

    assert len(sent_blobs) == 3
    assert [type(e) for e in collected] == [
        AudioOutputEvent,
        TurnCompleteEvent,
        AudioOutputEvent,
        TurnCompleteEvent,
        AudioOutputEvent,
        TurnCompleteEvent,
    ]
    assert collected[0].pcm_bytes == b"\x11\x00" * 120
    assert collected[2].pcm_bytes == b"\x22\x00" * 120
    assert collected[4].pcm_bytes == b"\x33\x00" * 120

    await backend.close()


async def test_gemini_backend_internal_reconnect_resumes_multi_turn() -> None:
    """Transient receive error after Turn 1 reconnects internally and continues Turn 2."""
    from clea.surfaces.voice.backends.base import BackendErrorEvent

    session_num = 0
    s1_calls = 0

    async def s1_receive() -> Any:
        nonlocal s1_calls
        s1_calls += 1
        if s1_calls == 1:
            yield types.LiveServerMessage(
                session_resumption_update=types.LiveServerSessionResumptionUpdate(
                    new_handle="resume-after-t1",
                    resumable=True,
                )
            )
            yield types.LiveServerMessage(
                server_content=types.LiveServerContent(
                    model_turn=types.Content(
                        parts=[
                            types.Part(
                                inline_data=types.Blob(
                                    data=b"\x11\x00" * 60, mime_type="audio/pcm;rate=24000"
                                )
                            )
                        ]
                    )
                )
            )
            yield types.LiveServerMessage(
                server_content=types.LiveServerContent(turn_complete=True)
            )
            return
        raise ConnectionError("transient ws drop")

    s2_calls = 0

    async def s2_receive() -> Any:
        nonlocal s2_calls
        s2_calls += 1
        if s2_calls == 1:
            yield types.LiveServerMessage(
                server_content=types.LiveServerContent(
                    model_turn=types.Content(
                        parts=[
                            types.Part(
                                inline_data=types.Blob(
                                    data=b"\x22\x00" * 60, mime_type="audio/pcm;rate=24000"
                                )
                            )
                        ]
                    )
                )
            )
            yield types.LiveServerMessage(
                server_content=types.LiveServerContent(turn_complete=True)
            )
            return
        await asyncio.sleep(10.0)

    s1 = MagicMock()
    s1.receive.side_effect = s1_receive
    s2 = MagicMock()
    s2.receive.side_effect = s2_receive

    def make_ctx(**_kwargs: Any) -> Any:
        nonlocal session_num
        session_num += 1
        ctx = AsyncMock()
        ctx.__aenter__.return_value = s1 if session_num == 1 else s2
        ctx.__aexit__.return_value = None
        return ctx

    mock_client = MagicMock()
    mock_client.aio.live.connect.side_effect = make_ctx

    backend = GeminiLiveBackend(api_key="fake-key", client=mock_client)
    await backend.connect(VoiceSessionConfig(system_instruction="sys", reconnect_limit=2))

    collected: list[Any] = []
    async for evt in backend.events():
        collected.append(evt)
        if len([e for e in collected if isinstance(e, TurnCompleteEvent)]) == 2:
            break

    assert mock_client.aio.live.connect.call_count == 2
    second_cfg = mock_client.aio.live.connect.call_args_list[1].kwargs["config"]
    assert second_cfg.session_resumption.handle == "resume-after-t1"
    assert any(isinstance(e, BackendErrorEvent) and e.recoverable for e in collected)
    audio_evts = [e for e in collected if isinstance(e, AudioOutputEvent)]
    assert [e.pcm_bytes for e in audio_evts] == [b"\x11\x00" * 60, b"\x22\x00" * 60]

    await backend.close()


async def test_gemini_backend_empty_receive_emits_error_and_closes_events_stream() -> None:
    """If session.receive() stops yielding without closing, events() terminates with error."""
    from clea.surfaces.voice.backends.base import BackendErrorEvent

    async def empty_recv() -> Any:
        if False:
            yield None

    mock_session = MagicMock()
    mock_session.receive.side_effect = empty_recv
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_session
    mock_ctx.__aexit__.return_value = None
    mock_client = MagicMock()
    mock_client.aio.live.connect.return_value = mock_ctx

    backend = GeminiLiveBackend(api_key="fake-key", client=mock_client)
    await backend.connect(VoiceSessionConfig(system_instruction="sys"))

    events = await asyncio.wait_for(_collect_all(backend), timeout=2.0)
    assert len(events) == 1
    assert isinstance(events[0], BackendErrorEvent)
    assert events[0].recoverable is False

    await backend.close()


async def _collect_all(backend: GeminiLiveBackend) -> list[Any]:
    return [evt async for evt in backend.events()]


async def test_gemini_backend_interaction_status_idle_emits_turn_complete() -> None:
    """interaction_status == IDLE without turn_complete=True still emits TurnCompleteEvent."""
    delivered = False

    async def recv_idle() -> Any:
        nonlocal delivered
        if not delivered:
            delivered = True
            yield types.LiveServerMessage(
                server_content=types.LiveServerContent(
                    interaction_status=types.InteractionStatus.IDLE
                )
            )
            return
        await asyncio.sleep(10.0)

    mock_session = MagicMock()
    mock_session.receive.side_effect = recv_idle
    mock_ctx = AsyncMock()
    mock_ctx.__aenter__.return_value = mock_session
    mock_ctx.__aexit__.return_value = None
    mock_client = MagicMock()
    mock_client.aio.live.connect.return_value = mock_ctx

    backend = GeminiLiveBackend(api_key="fake-key", client=mock_client)
    await backend.connect(VoiceSessionConfig(system_instruction="sys"))

    async def take_one() -> Any:
        async for evt in backend.events():
            return evt
        return None

    evt = await asyncio.wait_for(take_one(), timeout=2.0)
    assert isinstance(evt, TurnCompleteEvent)
    await backend.close()


async def test_gemini_backend_close_while_reconnecting_does_not_leak_session() -> None:
    """Closing while reconnect is awaiting __aenter__() must exit the newly entered session."""
    enter_started = asyncio.Event()
    allow_enter = asyncio.Event()
    ctx2_exited = asyncio.Event()

    async def hanging_recv() -> Any:
        await asyncio.sleep(10.0)
        if False:
            yield None

    s1 = MagicMock()
    s1.receive.side_effect = hanging_recv
    ctx1 = AsyncMock()
    ctx1.__aenter__.return_value = s1
    ctx1.__aexit__.return_value = None

    s2 = MagicMock()
    s2.receive.side_effect = hanging_recv
    ctx2 = AsyncMock()

    async def slow_enter() -> Any:
        enter_started.set()
        await allow_enter.wait()
        return s2

    async def track_exit(*_args: Any) -> None:
        ctx2_exited.set()

    ctx2.__aenter__.side_effect = slow_enter
    ctx2.__aexit__.side_effect = track_exit

    calls = 0

    def make_ctx(**_kwargs: Any) -> Any:
        nonlocal calls
        calls += 1
        return ctx1 if calls == 1 else ctx2

    mock_client = MagicMock()
    mock_client.aio.live.connect.side_effect = make_ctx

    backend = GeminiLiveBackend(api_key="fake-key", client=mock_client)
    await backend.connect(VoiceSessionConfig(system_instruction="sys"))

    reconnect_task = asyncio.create_task(backend.reconnect())
    await asyncio.wait_for(enter_started.wait(), timeout=1.0)

    await backend.close()
    allow_enter.set()
    await asyncio.wait_for(reconnect_task, timeout=1.0)

    assert ctx2_exited.is_set()
    assert backend._session is None


async def test_gemini_backend_defaults_to_high_media_resolution_and_handles_missing_enum(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    import google.genai.types as real_types

    mock_session = MagicMock()

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
    await backend.connect(VoiceSessionConfig(system_instruction="sys"))
    live_cfg: types.LiveConnectConfig = mock_client.aio.live.connect.call_args.kwargs["config"]
    assert live_cfg.media_resolution == types.MediaResolution.MEDIA_RESOLUTION_HIGH
    await backend.close()

    # Guarded attribute lookup cleanly tolerates an SDK lacking MediaResolution
    monkeypatch.delattr(real_types, "MediaResolution")
    built = backend._build_live_config(VoiceSessionConfig(system_instruction="sys"))
    assert built.media_resolution is None


async def test_gemini_backend_send_video_and_tool_responses_ignore_close_race() -> None:
    mock_session = MagicMock()

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
    await backend.connect(VoiceSessionConfig(system_instruction="sys"))

    async def fail_during_close(**_kwargs: Any) -> None:
        backend._closed = True
        raise RuntimeError("WebSocket closed")

    mock_session.send_realtime_input.side_effect = fail_during_close
    mock_session.send_tool_response.side_effect = fail_during_close

    await backend.send_video_frame(b"jpeg-bytes", mime_type="image/jpeg")
    await backend.send_tool_responses(
        [VoiceToolResponse(call_id="c1", name="look_at_screen", response={"status": "ok"})]
    )
    await backend.close()

"""Integration and state machine tests for VoiceRunner."""

from __future__ import annotations

import asyncio
import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest

from clea.core.protocol import (
    BaseEvent,
    CallEndedData,
    CallEndedEvent,
    CallStartedEvent,
    TranscriptEvent,
    UsageEvent,
    VoiceDelegateTaskRequest,
    VoiceStateEvent,
    parse_frame,
)
from clea.surfaces.voice.audio import AudioManager
from clea.surfaces.voice.backends.base import (
    AudioOutputEvent,
    BackendErrorEvent,
    InterruptedEvent,
    ToolCallEvent,
    TranscriptOutputEvent,
    UsageUpdateEvent,
    VoiceBackend,
    VoiceBackendEvent,
    VoiceSessionConfig,
    VoiceToolResponse,
)
from clea.surfaces.voice.config import VoiceConfig
from clea.surfaces.voice.lock import VoiceAlreadyRunningError
from clea.surfaces.voice.runner import VoiceRunner, voice_metrics_path


class FakeLiveBackend(VoiceBackend):
    """Controllable in-memory Live backend for testing."""

    backend_name = "fake-live"

    def __init__(self) -> None:
        self.connected_config: VoiceSessionConfig | None = None
        self.closed = False
        self.sent_audio: list[bytes] = []
        self.sent_tool_responses: list[VoiceToolResponse] = []
        self.event_queue: asyncio.Queue[VoiceBackendEvent | None] = asyncio.Queue()
        self.reconnect_count = 0

    async def _reconnect(self) -> None:
        self.reconnect_count += 1

    @property
    def model_name(self) -> str:
        return "fake-model"

    async def connect(self, config: VoiceSessionConfig) -> None:
        self.connected_config = config
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        await self.event_queue.put(None)

    async def send_audio(self, pcm_bytes: bytes) -> None:
        self.sent_audio.append(pcm_bytes)

    async def send_video_frame(self, image_bytes: bytes, *, mime_type: str = "image/jpeg") -> None:
        pass

    async def send_text(self, text: str, *, end_of_turn: bool = True) -> None:
        pass

    async def send_tool_responses(self, responses: list[VoiceToolResponse]) -> None:
        self.sent_tool_responses.extend(responses)

    async def events(self) -> AsyncIterator[VoiceBackendEvent]:
        while True:
            evt = await self.event_queue.get()
            if evt is None:
                break
            yield evt


def create_fake_audio_manager() -> AudioManager:
    audio = AudioManager()
    audio.start = MagicMock()  # type: ignore[method-assign]
    audio.stop = MagicMock()  # type: ignore[method-assign]
    audio.play_audio = MagicMock()  # type: ignore[method-assign]
    audio.stop_output = MagicMock()  # type: ignore[method-assign]
    return audio


def create_fake_wake_word() -> Any:
    from clea.surfaces.voice.wake_word import WakeWordDetector

    mock_model = MagicMock()
    mock_model.predict.return_value = {"hey_jarvis_v0.1": 0.0}
    return WakeWordDetector(model_instance=mock_model)


async def test_voice_runner_state_transitions_and_hotkey(tmp_path: Path) -> None:
    backend = FakeLiveBackend()
    audio = create_fake_audio_manager()
    cfg = VoiceConfig(silence_timeout_seconds=60.0)

    runner = VoiceRunner(
        config=cfg,
        data_dir=tmp_path,
        backend_factory=lambda: backend,
        audio_manager=audio,
        wake_word_detector=create_fake_wake_word(),
        enable_hotkey=False,
    )
    await runner.start()
    assert runner.call_state == "idle"
    assert runner.voice_state == "idle"

    # Start call via hotkey handler
    await runner._handle_hotkey_press()
    assert runner.call_state == "active"
    assert runner.voice_state == "listening"
    assert not backend.closed

    # End call via hotkey handler (Ending Trigger 3)
    await runner._handle_hotkey_press()
    assert runner.call_state == "idle"
    assert runner.voice_state == "idle"
    assert backend.closed

    await runner.stop()


async def test_voice_runner_ending_trigger_1_model_end_call_tool(tmp_path: Path) -> None:
    backend = FakeLiveBackend()
    audio = create_fake_audio_manager()
    cfg = VoiceConfig(silence_timeout_seconds=60.0)

    runner = VoiceRunner(
        config=cfg,
        data_dir=tmp_path,
        backend_factory=lambda: backend,
        audio_manager=audio,
        wake_word_detector=create_fake_wake_word(),
        enable_hotkey=False,
    )
    await runner.start()
    await runner.start_call(trigger="test")
    assert runner.call_state == "active"

    # Model emits end_call tool call
    await backend.event_queue.put(
        ToolCallEvent(
            call_id="call-end-1",
            name="end_call",
            arguments={"reason": "User said goodnight"},
        )
    )

    # Allow consumer task to process event
    await asyncio.sleep(0.05)
    assert runner.call_state == "idle"
    assert len(backend.sent_tool_responses) == 1
    assert backend.sent_tool_responses[0].name == "end_call"
    assert backend.closed

    await runner.stop()


async def test_voice_runner_ending_trigger_2_daemon_call_ended(tmp_path: Path) -> None:
    backend = FakeLiveBackend()
    audio = create_fake_audio_manager()
    cfg = VoiceConfig()

    runner = VoiceRunner(
        config=cfg,
        data_dir=tmp_path,
        backend_factory=lambda: backend,
        audio_manager=audio,
        wake_word_detector=create_fake_wake_word(),
        enable_hotkey=False,
    )
    await runner.start()
    await runner.start_call(trigger="test")
    assert runner.call_state == "active"

    # Simulate receiving CallEndedEvent from daemon
    fake_ended_frame = CallEndedEvent(
        data=CallEndedData(call_id="c-1", call_state="idle", reason="ui_hangup")
    )
    # Runner simulates receiving this frame in listener
    if fake_ended_frame.data.call_state == "idle":
        await runner.end_call(reason=fake_ended_frame.data.reason or "ui")

    assert runner.call_state == "idle"
    assert backend.closed
    await runner.stop()


async def test_voice_runner_ending_trigger_4_silence_timeout(tmp_path: Path) -> None:
    backend = FakeLiveBackend()
    audio = create_fake_audio_manager()
    # Very short silence timeout for fast test
    cfg = VoiceConfig(silence_timeout_seconds=0.1)

    runner = VoiceRunner(
        config=cfg,
        data_dir=tmp_path,
        backend_factory=lambda: backend,
        audio_manager=audio,
        wake_word_detector=create_fake_wake_word(),
        enable_hotkey=False,
    )
    await runner.start()
    await runner.start_call(trigger="test")
    assert runner.call_state == "active"

    # Wait for silence watchdog to fire (0.1s timeout)
    await asyncio.sleep(0.25)
    assert runner.call_state == "idle"
    assert backend.closed

    # Metrics log should record silence_timeout
    metrics_file = voice_metrics_path(tmp_path)
    assert metrics_file.exists()
    line = json.loads(metrics_file.read_text(encoding="utf-8").strip().splitlines()[-1])
    assert line["reason"] == "silence_timeout"
    assert "latency" in line
    assert line["latency"]["connect_ms"] >= 0

    await runner.stop()


async def test_voice_runner_silence_watchdog_with_streaming_silence_vs_speech(
    tmp_path: Path,
) -> None:
    """Verify continuous silent mic chunks do not prevent silence timeout.

    Active voice energy should reset the silence watchdog timer.
    """
    backend = FakeLiveBackend()
    audio = create_fake_audio_manager()
    cfg = VoiceConfig(silence_timeout_seconds=0.15)

    runner = VoiceRunner(
        config=cfg,
        data_dir=tmp_path,
        backend_factory=lambda: backend,
        audio_manager=audio,
        wake_word_detector=create_fake_wake_word(),
        enable_hotkey=False,
    )
    await runner.start()
    await runner.start_call(trigger="test")
    assert runner.call_state == "active"

    # 1. Feed pure silence chunks (RMS < 300) into _on_audio_chunk continuously
    silence_chunk = b"\x00\x00" * 160
    for _ in range(5):
        runner._on_audio_chunk(silence_chunk)
        await asyncio.sleep(0.04)

    # Allow watchdog poll interval to reliably trip on Windows timer resolution
    for _ in range(10):
        if runner.call_state == "idle":
            break
        await asyncio.sleep(0.03)

    # Silence watchdog should have fired because chunks had zero speech energy
    assert runner.call_state == "idle"
    await runner.stop()

    # 2. Now test speech energy (RMS >= 300) resetting the timer
    runner2 = VoiceRunner(
        config=cfg,
        data_dir=tmp_path,
        backend_factory=lambda: FakeLiveBackend(),
        audio_manager=audio,
        wake_word_detector=create_fake_wake_word(),
        enable_hotkey=False,
    )
    await runner2.start()
    await runner2.start_call(trigger="test")
    assert runner2.call_state == "active"

    # Speech chunk: high amplitude sine wave
    t = np.linspace(0, 0.02, 320, endpoint=False)
    speech_pcm = (np.sin(2 * np.pi * 300 * t) * 5000).astype(np.int16).tobytes()

    for _ in range(5):
        runner2._on_audio_chunk(speech_pcm)
        await asyncio.sleep(0.04)

    # Total elapsed ~0.2s > 0.15s, but because speech kept coming, call is STILL ACTIVE
    assert runner2.call_state == "active"
    await runner2.end_call(reason="done")
    await runner2.stop()


async def test_voice_runner_barge_in_and_audio_play(tmp_path: Path) -> None:
    backend = FakeLiveBackend()
    audio = create_fake_audio_manager()
    cfg = VoiceConfig(silence_timeout_seconds=60.0)

    runner = VoiceRunner(
        config=cfg,
        data_dir=tmp_path,
        backend_factory=lambda: backend,
        audio_manager=audio,
        wake_word_detector=create_fake_wake_word(),
        enable_hotkey=False,
    )
    await runner.start()
    await runner.start_call(trigger="test")

    # Backend sends audio output
    await backend.event_queue.put(AudioOutputEvent(pcm_bytes=b"\x01" * 160))
    await asyncio.sleep(0.05)
    assert runner.voice_state == "speaking"
    assert audio.play_audio.called

    # Backend sends InterruptedEvent (barge-in)
    await backend.event_queue.put(InterruptedEvent())
    await asyncio.sleep(0.05)
    assert runner.voice_state == "listening"
    assert audio.stop_output.called

    await runner.stop()


async def test_voice_runner_tool_delegation_to_daemon(tmp_path: Path) -> None:
    backend = FakeLiveBackend()
    audio = create_fake_audio_manager()
    cfg = VoiceConfig(silence_timeout_seconds=60.0)

    runner = VoiceRunner(
        config=cfg,
        data_dir=tmp_path,
        backend_factory=lambda: backend,
        audio_manager=audio,
        wake_word_detector=create_fake_wake_word(),
        enable_hotkey=False,
    )
    await runner.start()
    await runner.start_call(trigger="test")

    # Mock daemon websocket
    sent_to_daemon: list[str] = []
    mock_ws = MagicMock()

    async def fake_send(text: str) -> None:
        sent_to_daemon.append(text)
        frame = parse_frame(text)
        if isinstance(frame, VoiceDelegateTaskRequest):
            # Simulate daemon responding with success
            fut = runner._pending_tasks.get(frame.id)
            if fut and not fut.done():
                fut.set_result({"task_id": frame.id, "status": "succeeded", "output": "Done!"})

    mock_ws.send = fake_send
    runner._daemon_ws = mock_ws  # type: ignore[assignment]

    # Model triggers delegate_task
    await backend.event_queue.put(
        ToolCallEvent(
            call_id="call-del-1",
            name="delegate_task",
            arguments={"description": "Search flight status"},
        )
    )

    await asyncio.sleep(0.1)
    delegate_reqs = [
        f
        for f in (parse_frame(t) for t in sent_to_daemon)
        if isinstance(f, VoiceDelegateTaskRequest)
    ]
    assert len(delegate_reqs) == 1
    req_frame = delegate_reqs[0]
    assert req_frame.params.description == "Search flight status"

    # Backend should have received response
    assert len(backend.sent_tool_responses) == 1
    tool_resp = backend.sent_tool_responses[0]
    assert tool_resp.name == "delegate_task"
    assert tool_resp.response["status"] == "succeeded"
    assert tool_resp.response["output"] == "Done!"

    await runner.stop()


async def test_voice_runner_emits_all_typed_events(tmp_path: Path) -> None:
    """Requirement: Emits typed events (voice_state, call_started, etc.)."""
    backend = FakeLiveBackend()
    audio = create_fake_audio_manager()
    cfg = VoiceConfig(silence_timeout_seconds=60.0)

    emitted_events: list[BaseEvent] = []

    runner = VoiceRunner(
        config=cfg,
        data_dir=tmp_path,
        backend_factory=lambda: backend,
        audio_manager=audio,
        wake_word_detector=create_fake_wake_word(),
        enable_hotkey=False,
        on_event=emitted_events.append,
    )
    await runner.start()
    await runner.start_call(trigger="test")

    # Feed transcript
    await backend.event_queue.put(
        TranscriptOutputEvent(role="user", text="What is the weather?", final=True)
    )
    # Feed audio output
    await backend.event_queue.put(AudioOutputEvent(pcm_bytes=b"\x00" * 320))
    # Feed usage update
    await backend.event_queue.put(
        UsageUpdateEvent(
            input_tokens=120,
            output_tokens=60,
            total_tokens=180,
            cached_tokens=10,
            thought_tokens=5,
        )
    )
    await asyncio.sleep(0.08)

    # End call
    await runner.end_call(reason="completed")
    await runner.stop()

    event_types = [e.event for e in emitted_events]
    assert "voice_state" in event_types
    assert "call_started" in event_types
    assert "transcript" in event_types
    assert "usage" in event_types
    assert "call_ended" in event_types

    # Validate specific typed event instances
    state_evt = next(e for e in emitted_events if isinstance(e, VoiceStateEvent))
    assert state_evt.data.state in ("listening", "speaking", "thinking", "idle")

    start_evt = next(e for e in emitted_events if isinstance(e, CallStartedEvent))
    assert start_evt.data.call_state == "active"
    assert start_evt.data.call_id is not None

    transcript_evt = next(e for e in emitted_events if isinstance(e, TranscriptEvent))
    assert transcript_evt.data.role == "user"
    assert transcript_evt.data.text == "What is the weather?"

    usage_evt = next(e for e in emitted_events if isinstance(e, UsageEvent))
    assert usage_evt.data.total_tokens == 180

    end_evt = next(e for e in emitted_events if isinstance(e, CallEndedEvent))
    assert end_evt.data.call_state == "idle"
    assert end_evt.data.reason == "completed"

    # Also check async generator
    assert not runner._events_queue.empty()


async def test_voice_runner_invokes_deferred_transcript_and_consolidation_hooks(
    tmp_path: Path,
) -> None:
    backend = FakeLiveBackend()
    audio = create_fake_audio_manager()
    cfg = VoiceConfig()

    runner = VoiceRunner(
        config=cfg,
        data_dir=tmp_path,
        backend_factory=lambda: backend,
        audio_manager=audio,
        wake_word_detector=create_fake_wake_word(),
        enable_hotkey=False,
    )
    await runner.start()

    with (
        patch(
            "clea.surfaces.voice.runner.safe_store_voice_transcript",
            new_callable=AsyncMock,
        ) as mock_store,
        patch(
            "clea.surfaces.voice.runner.safe_consolidate_voice_call",
            new_callable=AsyncMock,
        ) as mock_cons,
    ):
        await runner.start_call(trigger="test")
        await backend.event_queue.put(
            TranscriptOutputEvent(role="user", text="Save me", final=True)
        )
        await asyncio.sleep(0.05)
        await runner.end_call(reason="done")

        assert mock_store.called
        assert mock_cons.called
        store_kwargs = mock_store.call_args.kwargs
        assert store_kwargs["call_id"] is not None
        assert len(store_kwargs["entries"]) == 1
        assert store_kwargs["entries"][0]["text"] == "Save me"

    await runner.stop()


async def test_voice_runner_single_instance_guard(tmp_path: Path) -> None:
    cfg = VoiceConfig()
    runner1 = VoiceRunner(
        config=cfg,
        data_dir=tmp_path,
        backend_factory=lambda: FakeLiveBackend(),
        audio_manager=create_fake_audio_manager(),
        wake_word_detector=create_fake_wake_word(),
        enable_hotkey=False,
    )
    await runner1.start()

    runner2 = VoiceRunner(
        config=cfg,
        data_dir=tmp_path,
        backend_factory=lambda: FakeLiveBackend(),
        audio_manager=create_fake_audio_manager(),
        wake_word_detector=create_fake_wake_word(),
        enable_hotkey=False,
    )

    with pytest.raises(VoiceAlreadyRunningError):
        await runner2.start()

    await runner1.stop()


async def test_voice_runner_surfaces_error_on_start_failure(tmp_path: Path) -> None:
    """Verify that if backend connection fails, VoiceRunner surfaces an error state with message."""
    cfg = VoiceConfig()
    backend = FakeLiveBackend()

    async def fail_connect(_cfg: VoiceSessionConfig) -> None:
        raise ConnectionError("Live API unreachable")

    backend.connect = fail_connect  # type: ignore[method-assign]

    states_seen: list[tuple[str, str | None]] = []

    runner = VoiceRunner(
        config=cfg,
        data_dir=tmp_path,
        backend_factory=lambda: backend,
        audio_manager=create_fake_audio_manager(),
        wake_word_detector=create_fake_wake_word(),
        enable_hotkey=False,
        on_event=lambda evt: (
            states_seen.append((evt.data.state, getattr(evt.data, "error", None)))
            if isinstance(evt, VoiceStateEvent)
            else None
        ),
    )
    await runner.start()

    await runner.start_call(trigger="test")
    assert runner.call_state == "idle"
    assert runner.voice_state == "error"
    # Verify that an error event was emitted with the error message
    error_events = [s for s in states_seen if s[0] == "error"]
    assert len(error_events) >= 1
    assert "Live API unreachable" in (error_events[-1][1] or "")

    await runner.stop()


async def test_voice_runner_stall_watchdog_triggers_reconnect(tmp_path: Path) -> None:
    """Verify stall watchdog triggers reconnection when audio streams without server responses."""
    # Set short stall timeout of 0.2s for testing
    cfg = VoiceConfig(stall_timeout_seconds=0.2)
    backend = FakeLiveBackend()

    runner = VoiceRunner(
        config=cfg,
        data_dir=tmp_path,
        backend_factory=lambda: backend,
        audio_manager=create_fake_audio_manager(),
        wake_word_detector=create_fake_wake_word(),
        enable_hotkey=False,
    )
    await runner.start()
    await runner.start_call(trigger="test")

    # Simulate audio chunks sent to active backend
    pcm_chunk = b"\x00" * 2560
    runner._on_audio_chunk(pcm_chunk)

    # Wait for stall watchdog to fire (sleep > 1.2s since watchdog checks every 1.0s)
    await asyncio.sleep(1.3)

    assert backend.reconnect_count >= 1
    await runner.end_call(reason="test_done")
    await runner.stop()


async def test_voice_runner_stall_watchdog_ends_call_after_second_stall(tmp_path: Path) -> None:
    """Verify stall watchdog ends call when server fails to respond after reconnect."""
    cfg = VoiceConfig(stall_timeout_seconds=0.1)
    backend = FakeLiveBackend()

    runner = VoiceRunner(
        config=cfg,
        data_dir=tmp_path,
        backend_factory=lambda: backend,
        audio_manager=create_fake_audio_manager(),
        wake_word_detector=create_fake_wake_word(),
        enable_hotkey=False,
    )
    await runner.start()
    await runner.start_call(trigger="test")

    # Send audio chunk to start stall timer
    pcm_chunk = b"\x00" * 2560
    runner._on_audio_chunk(pcm_chunk)

    # Wait for first stall -> reconnect
    await asyncio.sleep(0.3)
    assert backend.reconnect_count == 1

    # Send audio again without server response -> second stall
    runner._on_audio_chunk(pcm_chunk)
    await asyncio.sleep(0.3)

    assert runner.call_state == "idle"
    assert runner.voice_state == "error"
    await runner.stop()


async def test_voice_runner_daemon_event_does_not_send_redundant_start_req(tmp_path: Path) -> None:
    """Verify start_call(trigger='daemon_event') does not echo a request back to daemon."""
    cfg = VoiceConfig()
    backend = FakeLiveBackend()

    mock_ws = AsyncMock()
    sent_msgs: list[str] = []
    mock_ws.send = AsyncMock(side_effect=lambda msg: sent_msgs.append(msg))

    runner = VoiceRunner(
        config=cfg,
        data_dir=tmp_path,
        backend_factory=lambda: backend,
        audio_manager=create_fake_audio_manager(),
        wake_word_detector=create_fake_wake_word(),
        enable_hotkey=False,
    )
    runner._daemon_ws = mock_ws
    await runner.start()

    await runner.start_call(trigger="daemon_event")
    # Verify no VoiceStartCallRequest was sent
    start_calls = [m for m in sent_msgs if "voice.start_call" in m]
    assert len(start_calls) == 0

    await runner.end_call(reason="done")
    await runner.stop()


async def test_voice_runner_fatal_backend_error_transitions_to_error(tmp_path: Path) -> None:
    """Verify that a fatal BackendErrorEvent sets voice state to error and ends the call."""
    cfg = VoiceConfig()
    backend = FakeLiveBackend()

    runner = VoiceRunner(
        config=cfg,
        data_dir=tmp_path,
        backend_factory=lambda: backend,
        audio_manager=create_fake_audio_manager(),
        wake_word_detector=create_fake_wake_word(),
        enable_hotkey=False,
    )
    await runner.start()
    await runner.start_call(trigger="test")
    assert runner.call_state == "active"

    # Emit non-recoverable BackendErrorEvent
    await backend.event_queue.put(
        BackendErrorEvent(message="Quota exceeded 1011", recoverable=False)
    )
    await asyncio.sleep(0.05)

    assert runner.call_state == "idle"
    assert runner.voice_state == "error"
    await runner.stop()

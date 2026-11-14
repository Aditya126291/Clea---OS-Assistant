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
    VoiceStateData,
    VoiceStateEvent,
    parse_frame,
)
from clea.surfaces.voice.audio import (
    AmbiguousAudioDeviceError,
    AudioDeviceNotFoundError,
    AudioManager,
)
from clea.surfaces.voice.backends.base import (
    AudioOutputEvent,
    BackendErrorEvent,
    InterruptedEvent,
    ToolCallEvent,
    TranscriptOutputEvent,
    TurnCompleteEvent,
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
    cfg = VoiceConfig(stall_timeout_seconds=0.2, stall_speech_rms_threshold=250.0)
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

    # Simulate speech audio chunks (> stall_speech_rms_threshold) sent to active backend
    speech_chunk = np.full(1280, 1000, dtype=np.int16).tobytes()
    for _ in range(3):  # 3 * 0.08s = 0.24s > 0.2s
        runner._on_audio_chunk(speech_chunk)

    # Wait for stall watchdog to fire
    await asyncio.sleep(0.3)

    assert backend.reconnect_count >= 1
    await runner.end_call(reason="test_done")
    await runner.stop()


async def test_voice_runner_stall_watchdog_ends_call_after_second_stall(tmp_path: Path) -> None:
    """Verify stall watchdog ends call when server fails to respond after reconnect."""
    cfg = VoiceConfig(stall_timeout_seconds=0.1, stall_speech_rms_threshold=250.0)
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

    # Send speech chunk to start stall timer
    speech_chunk = np.full(1280, 1000, dtype=np.int16).tobytes()
    for _ in range(2):  # 0.16s > 0.1s
        runner._on_audio_chunk(speech_chunk)

    # Wait for first stall -> reconnect
    await asyncio.sleep(0.3)
    assert backend.reconnect_count == 1

    # Send speech audio again without server response -> second stall
    for _ in range(2):
        runner._on_audio_chunk(speech_chunk)
    await asyncio.sleep(0.3)

    assert runner.call_state == "idle"
    assert runner.voice_state == "error"
    await runner.stop()


async def test_voice_runner_stall_watchdog_does_not_fire_on_post_turn_silence(
    tmp_path: Path,
) -> None:
    """Verify 30+ seconds of low-RMS silence frames after turn_complete does not trigger stall."""
    # Set short stall timeout of 0.2s so if silence incorrectly advanced timer, it fires fast
    cfg = VoiceConfig(stall_timeout_seconds=0.2, stall_speech_rms_threshold=250.0)
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

    # Server sends TurnCompleteEvent
    await backend.event_queue.put(TurnCompleteEvent())
    await asyncio.sleep(0.05)
    assert runner.voice_state == "listening"

    # 30+ seconds of low-RMS silence frames: 380 chunks * 0.08s = 30.4s
    # All frames have amplitude 50 -> RMS = 50.0 << stall_speech_rms_threshold (250.0)
    silence_chunk = np.full(1280, 50, dtype=np.int16).tobytes()
    for _ in range(380):
        runner._on_audio_chunk(silence_chunk)

    # Allow watchdog loop to run
    await asyncio.sleep(0.3)

    # Watchdog must NOT trigger reconnect or end call
    assert backend.reconnect_count == 0
    assert runner.call_state == "active"
    assert runner.voice_state == "listening"
    assert runner._stall_timer_armed is False
    assert runner._stall_accumulated_seconds == 0.0

    await runner.end_call(reason="test_done")
    await runner.stop()


async def test_voice_runner_stall_watchdog_triggers_on_15s_speech(tmp_path: Path) -> None:
    """Verify 15+ seconds of speech-level RMS frames (> threshold) triggers reconnect."""
    # Default 15.0s stall timeout
    cfg = VoiceConfig(stall_timeout_seconds=15.0, stall_speech_rms_threshold=250.0)
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

    # 15+ seconds of speech-level RMS frames: 195 chunks * 0.08s = 15.6s > 15.0s
    # Speech frames have amplitude 1000 -> RMS = 1000.0 > 250.0
    speech_chunk = np.full(1280, 1000, dtype=np.int16).tobytes()
    for _ in range(195):
        runner._on_audio_chunk(speech_chunk)

    # Allow watchdog loop to run
    await asyncio.sleep(0.3)

    # Watchdog MUST trigger reconnect
    assert backend.reconnect_count >= 1
    assert runner.call_state == "active"
    assert runner.voice_state == "listening"
    assert runner._stall_timer_armed is False
    assert runner._stall_accumulated_seconds == 0.0

    await runner.end_call(reason="test_done")
    await runner.stop()


async def test_voice_runner_stall_watchdog_sets_reconnecting_state(tmp_path: Path) -> None:
    """Verify stall watchdog enters 'reconnecting' state instead of 'error' during resumption."""
    cfg = VoiceConfig(stall_timeout_seconds=0.1, stall_speech_rms_threshold=250.0)
    backend = FakeLiveBackend()

    reconnect_started = asyncio.Event()
    reconnect_proceed = asyncio.Event()

    async def slow_reconnect() -> None:
        backend.reconnect_count += 1
        reconnect_started.set()
        await reconnect_proceed.wait()

    backend._reconnect = slow_reconnect  # type: ignore[method-assign]

    states_seen: list[str] = []

    def on_event(evt: BaseEvent) -> None:
        if isinstance(evt, VoiceStateEvent):
            states_seen.append(evt.data.state)

    runner = VoiceRunner(
        config=cfg,
        data_dir=tmp_path,
        backend_factory=lambda: backend,
        audio_manager=create_fake_audio_manager(),
        wake_word_detector=create_fake_wake_word(),
        enable_hotkey=False,
        on_event=on_event,
    )
    await runner.start()
    await runner.start_call(trigger="test")

    # Send speech chunks to trigger stall
    speech_chunk = np.full(1280, 1000, dtype=np.int16).tobytes()
    for _ in range(2):
        runner._on_audio_chunk(speech_chunk)

    # Wait for reconnect to start
    await asyncio.wait_for(reconnect_started.wait(), timeout=1.0)

    # While reconnecting, state must be 'reconnecting' and NOT 'error'
    assert runner.voice_state == "reconnecting"
    assert "reconnecting" in states_seen
    assert "error" not in states_seen

    # Complete reconnection
    reconnect_proceed.set()
    await asyncio.sleep(0.05)

    assert runner.voice_state == "listening"
    assert backend.reconnect_count == 1

    await runner.end_call(reason="test_done")
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


async def test_voice_runner_recoverable_backend_error_sets_reconnecting_state(
    tmp_path: Path,
) -> None:
    """Verify recoverable BackendErrorEvent sets state to reconnecting and clears on next event."""
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
    assert runner.voice_state == "listening"

    # Emit recoverable BackendErrorEvent (e.g. transient disconnect during session resumption)
    await backend.event_queue.put(
        BackendErrorEvent(message="Connection dropped; reconnecting...", recoverable=True)
    )
    await asyncio.sleep(0.05)

    # Call remains active in non-alarming 'reconnecting' state
    assert runner.call_state == "active"
    assert runner.voice_state == "reconnecting"

    # Server recovers and sends TurnCompleteEvent
    await backend.event_queue.put(TurnCompleteEvent())
    await asyncio.sleep(0.05)

    # State returns to 'listening'
    assert runner.call_state == "active"
    assert runner.voice_state == "listening"

    await runner.end_call(reason="test_done")
    await runner.stop()


async def test_voice_runner_stall_watchdog_does_not_fire_while_speaking(tmp_path: Path) -> None:
    """Watchdog must not fire if assistant is speaking out loud."""
    cfg = VoiceConfig(stall_timeout_seconds=0.1)
    backend = FakeLiveBackend()
    audio_mgr = create_fake_audio_manager()

    runner = VoiceRunner(
        config=cfg,
        data_dir=tmp_path,
        backend_factory=lambda: backend,
        audio_manager=audio_mgr,
        wake_word_detector=create_fake_wake_word(),
        enable_hotkey=False,
    )
    await runner.start()
    await runner.start_call(trigger="test")

    # Mark assistant as actively speaking
    audio_mgr._is_speaking.set()
    speech_chunk = np.full(1280, 1000, dtype=np.int16).tobytes()
    for _ in range(3):
        runner._on_audio_chunk(speech_chunk)

    # Wait > stall_timeout_seconds
    await asyncio.sleep(0.3)

    # Watchdog must not have reconnected or ended the call
    assert backend.reconnect_count == 0
    assert runner.call_state == "active"
    await runner.stop()


async def test_voice_runner_wake_word_resets_immediately_on_detection(tmp_path: Path) -> None:
    """WakeWordDetector must be reset immediately upon detection to avoid multi-triggering."""
    cfg = VoiceConfig()
    backend = FakeLiveBackend()
    wake_detector = create_fake_wake_word()
    wake_detector._model.predict.return_value = {"hey_jarvis_v0.1": 0.95}
    wake_detector.reset = MagicMock()

    runner = VoiceRunner(
        config=cfg,
        data_dir=tmp_path,
        backend_factory=lambda: backend,
        audio_manager=create_fake_audio_manager(),
        wake_word_detector=wake_detector,
        enable_hotkey=False,
    )
    await runner.start()
    assert runner.call_state == "idle"

    # Feed valid 1280-sample (2560 bytes) chunk triggering wake word
    runner._on_audio_chunk(b"\x00" * 2560)

    # Verify reset was called immediately
    assert wake_detector.reset.called
    await runner.stop()


async def test_voice_runner_retries_audio_init_on_wasapi_wakeup(tmp_path: Path) -> None:
    """VoiceRunner.start retries audio initialization if WASAPI endpoints wake up slowly."""
    import sounddevice as sd

    cfg = VoiceConfig()
    audio = create_fake_audio_manager()
    attempts = 0

    def flaky_audio_start() -> None:
        nonlocal attempts
        attempts += 1
        if attempts == 1:
            raise AudioDeviceNotFoundError("No audio input devices found")
        if attempts == 2:
            raise sd.PortAudioError("WASAPI not ready")

    audio.start = MagicMock(side_effect=flaky_audio_start)  # type: ignore[method-assign]

    runner = VoiceRunner(
        config=cfg,
        data_dir=tmp_path,
        backend_factory=lambda: FakeLiveBackend(),
        audio_manager=audio,
        wake_word_detector=create_fake_wake_word(),
        enable_hotkey=False,
        audio_init_retries=4,
        audio_init_retry_delay=0.01,
    )
    with patch("clea.surfaces.voice.runner.refresh_audio_devices") as mock_refresh:
        await runner.start()

    assert attempts == 3
    assert mock_refresh.call_count == 2
    await runner.stop()


async def test_voice_runner_releases_lock_when_audio_init_exhausts_retries(tmp_path: Path) -> None:
    """VoiceRunner releases voice.lock if audio init fails after retries or ambiguous config."""
    cfg = VoiceConfig()
    audio = create_fake_audio_manager()
    audio.start = MagicMock(side_effect=AmbiguousAudioDeviceError("Multiple devices"))  # type: ignore[method-assign]

    runner = VoiceRunner(
        config=cfg,
        data_dir=tmp_path,
        backend_factory=lambda: FakeLiveBackend(),
        audio_manager=audio,
        wake_word_detector=create_fake_wake_word(),
        enable_hotkey=False,
        audio_init_retries=3,
        audio_init_retry_delay=0.01,
    )
    with pytest.raises(AmbiguousAudioDeviceError):
        await runner.start()

    assert audio.start.call_count == 1
    assert not (tmp_path / "voice.lock").exists()


async def test_voice_runner_retries_daemon_startup_including_missing_token_and_stale_401(
    tmp_path: Path,
) -> None:
    """VoiceRunner retries cleanly when daemon is booting (no token / refused / 401)."""
    from websockets.exceptions import InvalidStatus
    from websockets.http11 import Response

    from clea.core import auth

    cfg = VoiceConfig()
    audio = create_fake_audio_manager()
    runner = VoiceRunner(
        config=cfg,
        data_dir=tmp_path,
        daemon_url="ws://127.0.0.1:8765",
        token=None,
        backend_factory=lambda: FakeLiveBackend(),
        audio_manager=audio,
        wake_word_detector=create_fake_wake_word(),
        enable_hotkey=False,
        daemon_retry_delay=0.02,
    )

    connect_calls: list[list[str]] = []

    class FakeWS:
        def __init__(self) -> None:
            self.sent: list[str] = []

        async def __aenter__(self) -> FakeWS:
            return self

        async def __aexit__(self, *_args: object) -> None:
            return None

        async def send(self, data: str) -> None:
            self.sent.append(data)

        async def recv(self) -> str:
            runner._stop_event.set()
            await asyncio.sleep(0.01)
            return VoiceStateEvent(data=VoiceStateData(state="idle")).model_dump_json()

        async def close(self) -> None:
            return None

    fake_ws = FakeWS()

    def fake_connect(_url: str, *, subprotocols: list[str]) -> Any:
        connect_calls.append(subprotocols)
        if len(connect_calls) == 1:
            raise ConnectionRefusedError("daemon still booting")
        if len(connect_calls) == 2:
            # Simulate stale session_token rejected with HTTP 401 before daemon writes fresh token
            auth.write_token("fresh-token-123", auth.token_path(tmp_path))
            resp = Response(401, "Unauthorized", [])  # type: ignore[arg-type]
            raise InvalidStatus(resp)
        return fake_ws

    # Initially write a stale token after one missing-token poll
    async def seed_stale_token_soon() -> None:
        await asyncio.sleep(0.03)
        auth.write_token("stale-token", auth.token_path(tmp_path))

    seeder = asyncio.create_task(seed_stale_token_soon())
    with patch("clea.surfaces.voice.runner.connect", side_effect=fake_connect):
        await asyncio.wait_for(runner.run_forever(), timeout=3.0)
    await seeder

    assert len(connect_calls) == 3
    assert "clea.token.stale-token" in connect_calls[0]
    assert "clea.token.stale-token" in connect_calls[1]
    assert "clea.token.fresh-token-123" in connect_calls[2]
    assert len(fake_ws.sent) >= 1
    assert runner._daemon_ws is None
    assert not (tmp_path / "voice.lock").exists()


async def test_voice_runner_connects_to_late_starting_real_daemon(tmp_path: Path) -> None:
    """VoiceRunner retries against a closed port + stale token, then connects to real CleaDaemon."""
    import socket

    from clea.core import auth
    from clea.core.daemon import build_daemon
    from clea.memory import MemoryStore
    from clea.providers.base import LLMProvider, Message, StreamChunk
    from clea.tools import ToolDefinition

    class _DummyProvider(LLMProvider):
        async def stream_turn(
            self,
            *,
            messages: list[Message],
            tools: list[ToolDefinition],
            system_prompt: str,
        ) -> AsyncIterator[StreamChunk]:
            if False:
                yield StreamChunk(type="done")

    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
        s.bind(("127.0.0.1", 0))
        port = s.getsockname()[1]

    # Seed a stale token before daemon boots
    auth.write_token("stale-preboot-token", auth.token_path(tmp_path))

    runner = VoiceRunner(
        config=VoiceConfig(),
        data_dir=tmp_path,
        daemon_url=f"ws://127.0.0.1:{port}",
        token=None,
        backend_factory=lambda: FakeLiveBackend(),
        audio_manager=create_fake_audio_manager(),
        wake_word_detector=create_fake_wake_word(),
        enable_hotkey=False,
        daemon_retry_delay=0.05,
    )

    daemon = build_daemon(
        host="127.0.0.1",
        port=port,
        provider=_DummyProvider(),
        memory=MemoryStore(base_dir=tmp_path),
    )

    runner_task = asyncio.create_task(runner.run_forever())
    try:
        # Let VoiceRunner hit ConnectionRefusedError at least once
        await asyncio.sleep(0.15)
        assert not runner._daemon_ever_connected

        await daemon.start()
        try:
            # Wait for VoiceRunner to pick up the fresh token and connect
            for _ in range(40):
                if any(c.is_voice for c in daemon._connections):
                    break
                await asyncio.sleep(0.05)

            assert runner._daemon_ever_connected is True
            assert any(c.is_voice for c in daemon._connections)
        finally:
            runner._stop_event.set()
            await asyncio.wait_for(runner_task, timeout=3.0)
            await daemon.stop()
    finally:
        if not runner_task.done():
            runner._stop_event.set()
            await asyncio.wait_for(runner_task, timeout=3.0)

    assert not (tmp_path / "voice.lock").exists()


async def test_voice_runner_multi_turn_with_gemini_backend_and_audio_manager(
    tmp_path: Path,
) -> None:
    """Verify Turn 1 and Turn 2 both send user audio and receive/play server audio."""
    from google.genai import types

    from clea.surfaces.voice.backends.gemini_live import GeminiLiveBackend

    turn_call_count = 0
    sent_audio_chunks: list[bytes] = []
    turn1_user_audio_seen = asyncio.Event()
    turn2_user_audio_seen = asyncio.Event()

    async def fake_send_realtime_input(*, audio: Any = None, **_kwargs: Any) -> None:
        if audio is not None and audio.data:
            sent_audio_chunks.append(audio.data)
            if audio.data[:2] == b"\x10\x00":
                turn1_user_audio_seen.set()
            elif audio.data[:2] == b"\x20\x00":
                turn2_user_audio_seen.set()

    async def multi_turn_receive() -> Any:
        nonlocal turn_call_count
        turn_call_count += 1
        current = turn_call_count
        if current == 1:
            await turn1_user_audio_seen.wait()
            yield types.LiveServerMessage(
                server_content=types.LiveServerContent(
                    model_turn=types.Content(
                        parts=[
                            types.Part(
                                inline_data=types.Blob(
                                    data=b"\xaa\x00" * 120, mime_type="audio/pcm;rate=24000"
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
        if current == 2:
            await turn2_user_audio_seen.wait()
            yield types.LiveServerMessage(
                server_content=types.LiveServerContent(
                    model_turn=types.Content(
                        parts=[
                            types.Part(
                                inline_data=types.Blob(
                                    data=b"\xbb\x00" * 120, mime_type="audio/pcm;rate=24000"
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

    audio = AudioManager(mic_mute_while_speaking=True, chunk_size=1280)
    audio.input_sample_rate = 16000
    audio.input_channels = 1

    def fake_audio_start() -> None:
        audio._running = True

    def fake_audio_stop() -> None:
        audio._running = False
        audio.stop_output()

    audio.start = MagicMock(side_effect=fake_audio_start)  # type: ignore[method-assign]
    audio.stop = MagicMock(side_effect=fake_audio_stop)  # type: ignore[method-assign]

    states_seen: list[str] = []

    def on_event(evt: BaseEvent) -> None:
        if isinstance(evt, VoiceStateEvent):
            states_seen.append(evt.data.state)

    cfg = VoiceConfig(silence_timeout_seconds=60.0, mic_mute_while_speaking=True)
    runner = VoiceRunner(
        config=cfg,
        data_dir=tmp_path,
        backend_factory=lambda: GeminiLiveBackend(api_key="test-key", client=mock_client),
        audio_manager=audio,
        wake_word_detector=create_fake_wake_word(),
        enable_hotkey=False,
        on_event=on_event,
    )

    await runner.start()
    await runner.start_call(trigger="test")
    assert runner.call_state == "active"
    assert runner.voice_state == "listening"

    # Turn 1: mic captures 16 kHz user PCM -> backend sends -> server sends audio + turn_complete
    turn1_mic = np.full((1280, 1), 16, dtype=np.int16)  # 0x0010 little-endian -> b"\x10\x00"
    audio._input_stream_callback(turn1_mic, 1280, None, 0)  # type: ignore[arg-type]

    for _ in range(40):
        if audio.is_speaking and runner.voice_state == "listening":
            break
        await asyncio.sleep(0.02)
    assert audio.is_speaking is True
    assert runner.voice_state == "listening"

    # Drain output playback via _output_stream_callback (more frames than 120 samples)
    out_buf = np.empty((256, 1), dtype=np.int16)
    audio._output_stream_callback(out_buf, 256, None, 0)  # type: ignore[arg-type]
    assert np.array_equal(out_buf[:120, 0], np.full(120, 0x00AA, dtype=np.int16))
    assert np.all(out_buf[120:] == 0)
    assert audio.is_speaking is False

    # Turn 2: mic captures 16 kHz user PCM -> must still forward & receive Turn 2 response
    turn2_mic = np.full((1280, 1), 32, dtype=np.int16)  # 0x0020 little-endian -> b"\x20\x00"
    audio._input_stream_callback(turn2_mic, 1280, None, 0)  # type: ignore[arg-type]

    for _ in range(40):
        if audio.is_speaking and len(sent_audio_chunks) >= 2 and runner.voice_state == "listening":
            break
        await asyncio.sleep(0.02)

    assert len(sent_audio_chunks) >= 2
    assert any(c[:2] == b"\x10\x00" for c in sent_audio_chunks)
    assert any(c[:2] == b"\x20\x00" for c in sent_audio_chunks)
    assert audio.is_speaking is True
    assert runner.call_state == "active"
    assert runner.voice_state == "listening"

    # Drain Turn 2 output playback
    out_buf2 = np.empty((256, 1), dtype=np.int16)
    audio._output_stream_callback(out_buf2, 256, None, 0)  # type: ignore[arg-type]
    assert np.array_equal(out_buf2[:120, 0], np.full(120, 0x00BB, dtype=np.int16))
    assert audio.is_speaking is False
    assert states_seen.count("speaking") >= 2

    await runner.end_call(reason="test_done")
    await runner.stop()

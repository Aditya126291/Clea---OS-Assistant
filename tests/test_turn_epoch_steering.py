"""Deterministic unit tests for Worktree 3A voice steering, turn-epoch cancellation,
single-frame coordinate calibration, ActionVerifier wiring, cross-turn circuit breaker
persistence, legacy vision config warnings, and prompt/tool alignment.

Zero live desktop hooks, zero real sockets, zero real audio devices.
"""

from __future__ import annotations

import asyncio
import io
import logging
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from google.genai import types
from PIL import Image

from clea.core.agent import _BASE_SYSTEM_PROMPT
from clea.core.protocol import (
    OsCancelInflightRequest,
    OsGuiActionRequest,
    OsInspectScreenRequest,
    parse_frame,
)
from clea.surfaces.cli.voice import (
    build_default_action_verifier,
    run_voice_doctor,
    run_voice_service,
)
from clea.surfaces.voice.backends.base import (
    InterruptedEvent,
    ToolCallCancellationEvent,
    ToolCallEvent,
    TranscriptOutputEvent,
    TurnCompleteEvent,
    VoiceBackend,
    VoiceBackendEvent,
    VoiceSessionConfig,
    VoiceToolResponse,
    default_voice_tools,
)
from clea.surfaces.voice.backends.gemini_live import GeminiLiveBackend
from clea.surfaces.voice.config import (
    VoiceConfig,
    check_legacy_vision_config_warnings,
    load_voice_config,
)
from clea.surfaces.voice.memory_hooks import BASE_VOICE_SYSTEM_INSTRUCTION
from clea.surfaces.voice.runner import VoiceRunner
from clea.surfaces.voice.screen import ScreenCaptureResult
from clea.tools.guards import ActionVerifier, MediaSessionState, WindowContext


class _FakeVoiceBackend(VoiceBackend):
    backend_name = "fake-live"

    def __init__(self) -> None:
        self.event_queue: asyncio.Queue[VoiceBackendEvent | None] = asyncio.Queue()
        self.sent_tool_responses: list[VoiceToolResponse] = []
        self.sent_batches: list[list[VoiceToolResponse]] = []
        self.connected = False
        self.closed = False

    @property
    def model_name(self) -> str:
        return "fake-live-model"

    async def connect(self, config: VoiceSessionConfig) -> None:
        self.connected = True

    async def close(self) -> None:
        self.closed = True
        await self.event_queue.put(None)

    async def send_audio(self, pcm_bytes: bytes) -> None:
        pass

    async def send_video_frame(self, image_bytes: bytes, *, mime_type: str = "image/jpeg") -> None:
        pass

    async def send_text(self, text: str, *, end_of_turn: bool = True) -> None:
        pass

    async def send_tool_responses(self, responses: list[VoiceToolResponse]) -> None:
        self.sent_tool_responses.extend(responses)
        self.sent_batches.append(list(responses))

    async def events(self) -> AsyncIterator[VoiceBackendEvent]:
        while True:
            evt = await self.event_queue.get()
            if evt is None:
                break
            yield evt


def _make_fake_audio() -> MagicMock:
    audio = MagicMock()
    audio.is_speaking = False
    audio.resolved_input_device = None
    audio.resolved_output_device = None
    audio._health_monitored = False
    return audio


def _make_fake_wake_word() -> MagicMock:
    ww = MagicMock()
    ww.process_audio.return_value = (False, 0.0, "hey_jarvis")
    ww.active_engine = "mock"
    ww.model_key = "hey_jarvis"
    ww.threshold = 0.5
    ww.model_path = None
    return ww


def _make_png_bytes(width: int = 100, height: int = 80) -> bytes:
    img = Image.new("RGB", (width, height), (30, 60, 90))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_runner(
    tmp_path: Path,
    backend: _FakeVoiceBackend,
    *,
    config: VoiceConfig | None = None,
    screen_service: Any = None,
    action_verifier: Any = None,
    state_capture_fn: Any = None,
) -> VoiceRunner:
    indicator = MagicMock()
    indicator.enabled = True
    indicator.is_watching = False
    return VoiceRunner(
        config=config or VoiceConfig(consent_mode="auto_allow"),
        data_dir=tmp_path,
        backend_factory=lambda: backend,
        audio_manager=_make_fake_audio(),
        wake_word_detector=_make_fake_wake_word(),
        screen_service=screen_service,
        screen_indicator=indicator,
        action_verifier=action_verifier,
        state_capture_fn=state_capture_fn,
        enable_hotkey=False,
    )


# ---------------------------------------------------------------------------
# 1. ToolCallCancellationEvent: cancel by Gemini call_id + os.cancel_inflight
#    + silent suppression of tool response
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_tool_call_cancellation_cancels_by_call_id_and_suppresses_response(
    tmp_path: Path,
) -> None:
    backend = _FakeVoiceBackend()
    runner = _make_runner(tmp_path, backend)

    sent_frames: list[Any] = []
    rpc_started = asyncio.Event()
    rpc_cancelled = asyncio.Event()

    class _FakeWS:
        async def send(self, payload: str) -> None:
            frame = parse_frame(payload)
            sent_frames.append(frame)
            if isinstance(frame, OsGuiActionRequest):
                rpc_started.set()
            elif hasattr(frame, "id"):
                # Resolve startup/control RPCs immediately (leave OsGuiActionRequest pending)
                fut = runner._pending_tasks.get(frame.id)
                if fut is not None and not fut.done():
                    fut.set_result({"ok": True, "call_id": "call-cancel-test"})

    runner._daemon_ws = _FakeWS()  # type: ignore[assignment]
    runner._daemon_listener_active = True

    orig_request = runner._request_daemon

    async def _wrapped_request(req: Any, *, timeout: float = 5.0) -> dict[str, Any]:
        try:
            return await orig_request(req, timeout=timeout)
        except asyncio.CancelledError:
            if isinstance(req, OsGuiActionRequest):
                rpc_cancelled.set()
            raise

    runner._request_daemon = _wrapped_request  # type: ignore[method-assign]

    await runner.start()
    await runner.start_call(trigger="manual")

    # Emit a long-running gui_action ToolCallEvent with Gemini call_id != daemon RPC UUID
    await backend.event_queue.put(
        ToolCallEvent(
            call_id="gemini-call-42",
            name="gui_action",
            arguments={"action": "click_at", "x": 200, "y": 200, "coord_space": "screen"},
        )
    )
    await asyncio.wait_for(rpc_started.wait(), timeout=2.0)

    # Verify _call_id_to_task tracked (task, rpc_id, epoch) and rpc_id != gemini call_id
    assert "gemini-call-42" in runner._call_id_to_task
    tracked_task, tracked_rpc_id, tracked_epoch = runner._call_id_to_task["gemini-call-42"]
    assert tracked_rpc_id is not None
    assert tracked_rpc_id != "gemini-call-42"
    assert tracked_rpc_id in runner._pending_tasks
    assert tracked_epoch == runner._turn_epoch

    # Gemini sends ToolCallCancellationEvent for gemini-call-42
    await backend.event_queue.put(ToolCallCancellationEvent(call_ids=["gemini-call-42"]))
    await asyncio.wait_for(rpc_cancelled.wait(), timeout=2.0)
    await asyncio.sleep(0.05)

    assert tracked_task.cancelled() or tracked_task.done()
    assert "gemini-call-42" not in runner._call_id_to_task
    assert tracked_rpc_id not in runner._pending_tasks

    # Out-of-band os.cancel_inflight(reason="user_barge_in") must be sent to daemon
    cancel_reqs = [f for f in sent_frames if isinstance(f, OsCancelInflightRequest)]
    assert len(cancel_reqs) >= 1
    assert cancel_reqs[-1].params.reason == "user_barge_in"

    # Server-cancelled call_id MUST NOT send any FunctionResponse back to Gemini
    assert backend.sent_tool_responses == []

    await runner.end_call(reason="done")
    await runner.stop()


# ---------------------------------------------------------------------------
# 2. Local InterruptedEvent barge-in: bump turn_epoch, fire os.cancel_inflight,
#    send fast cancelled response, and drop stale-epoch results
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_interrupted_event_bumps_epoch_sends_fast_cancel_response_and_drops_stale(
    tmp_path: Path,
) -> None:
    backend = _FakeVoiceBackend()
    runner = _make_runner(tmp_path, backend)

    rpc_calls: list[Any] = []
    rpc_started = asyncio.Event()
    release_rpc = asyncio.Event()

    async def _fake_rpc(req: Any, *, timeout: float = 10.0) -> dict[str, Any]:
        rpc_calls.append(req)
        if isinstance(req, OsCancelInflightRequest):
            return {"ok": True, "cancelled": True}
        rpc_started.set()
        try:
            await release_rpc.wait()
        except asyncio.CancelledError:
            raise
        return {"ok": True, "status": "completed", "action": "click_at"}

    runner._rpc = _fake_rpc  # type: ignore[method-assign]

    await runner.start()
    await runner.start_call(trigger="test")
    epoch_before = runner._turn_epoch

    # Launch an in-flight tool call that Gemini has NOT server-cancelled
    await backend.event_queue.put(
        ToolCallEvent(
            call_id="call-barge-1",
            name="gui_action",
            arguments={"action": "click_at", "x": 100, "y": 100, "coord_space": "screen"},
        )
    )
    await asyncio.wait_for(rpc_started.wait(), timeout=2.0)

    # User barges in -> InterruptedEvent (no ToolCallCancellationEvent)
    await backend.event_queue.put(InterruptedEvent())
    await asyncio.sleep(0.05)

    assert runner._turn_epoch == epoch_before + 1

    # Out-of-band os.cancel_inflight fired to daemon
    cancel_reqs = [r for r in rpc_calls if isinstance(r, OsCancelInflightRequest)]
    assert len(cancel_reqs) >= 1
    assert cancel_reqs[-1].params.reason == "user_barge_in"

    # Fast cancelled tool response sent so Gemini Live never wedges waiting for FunctionResponse
    assert len(backend.sent_tool_responses) == 1
    resp = backend.sent_tool_responses[0]
    assert resp.call_id == "call-barge-1"
    assert resp.name == "gui_action"
    assert resp.is_error is False
    assert resp.response == {
        "ok": False,
        "cancelled": True,
        "reason": "user_barge_in",
    }

    # Verify stale-epoch result is dropped if a slow handler ignores CancelledError
    stale_backend = _FakeVoiceBackend()
    stale_runner = _make_runner(tmp_path / "stale", stale_backend)
    stale_started = asyncio.Event()
    stale_release = asyncio.Event()

    async def _uncancellable_rpc(req: Any, *, timeout: float = 10.0) -> dict[str, Any]:
        if isinstance(req, OsCancelInflightRequest):
            return {"ok": True}
        stale_started.set()
        try:
            await stale_release.wait()
        except asyncio.CancelledError:
            # Simulate an uncooperative coroutine returning a late stale result
            return {"ok": True, "status": "stale_completed"}
        return {"ok": True, "status": "stale_completed"}

    stale_runner._rpc = _uncancellable_rpc  # type: ignore[method-assign]
    await stale_runner.start()
    await stale_runner.start_call(trigger="test")
    await stale_backend.event_queue.put(
        ToolCallEvent(
            call_id="call-stale-1",
            name="open_or_focus_app",
            arguments={"app_name": "notepad"},
        )
    )
    await asyncio.wait_for(stale_started.wait(), timeout=2.0)
    await stale_backend.event_queue.put(InterruptedEvent())
    await asyncio.sleep(0.05)
    stale_release.set()
    await asyncio.sleep(0.05)

    # Only the single fast barge-in response should ever be sent; stale result must be dropped
    assert len(stale_backend.sent_tool_responses) == 1
    assert stale_backend.sent_tool_responses[0].response["cancelled"] is True

    await stale_runner.end_call(reason="done")
    await stale_runner.stop()
    await runner.end_call(reason="done")
    await runner.stop()


@pytest.mark.asyncio
async def test_gemini_live_backend_suppresses_server_cancelled_allows_local_barge_in() -> None:
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

    backend = GeminiLiveBackend(api_key="test-key", client=mock_client)
    await backend.connect(VoiceSessionConfig(system_instruction="sys"))

    # Server cancels call-server-1 via LiveServerToolCallCancellation
    await backend._handle_server_message(
        types.LiveServerMessage(
            tool_call_cancellation=types.LiveServerToolCallCancellation(ids=["call-server-1"])
        )
    )
    assert backend.is_call_cancelled("call-server-1") is True
    assert backend.is_call_cancelled("call-local-2") is False

    # Sending responses for both must suppress call-server-1 and send call-local-2 intact
    await backend.send_tool_responses(
        [
            VoiceToolResponse(
                call_id="call-server-1",
                name="gui_action",
                response={"ok": True},
            ),
            VoiceToolResponse(
                call_id="call-local-2",
                name="gui_action",
                response={"ok": False, "cancelled": True, "reason": "user_barge_in"},
                is_error=False,
                scheduling="silent",
            ),
        ]
    )
    assert mock_session.send_tool_response.call_count == 1
    sent: list[types.FunctionResponse] = mock_session.send_tool_response.call_args.kwargs[
        "function_responses"
    ]
    assert len(sent) == 1
    assert sent[0].id == "call-local-2"
    assert sent[0].response == {"ok": False, "cancelled": True, "reason": "user_barge_in"}
    assert sent[0].scheduling == types.FunctionResponseScheduling.SILENT

    # Sending ONLY a server-cancelled call_id must not invoke session.send_tool_response at all
    mock_session.send_tool_response.reset_mock()
    await backend.send_tool_responses(
        [VoiceToolResponse(call_id="call-server-1", name="gui_action", response={"ok": True})]
    )
    assert mock_session.send_tool_response.call_count == 0

    await backend.close()


# ---------------------------------------------------------------------------
# 3. Non-final user TranscriptOutputEvent preserves breaker & target HWND
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_non_final_user_transcript_preserves_circuit_breaker_and_target_hwnd(
    tmp_path: Path,
) -> None:
    backend = _FakeVoiceBackend()
    state = {
        "hwnd": 5555,
        "title": "App Window",
        "process_name": "app.exe",
        "dhash": "a" * 64,
    }
    runner = _make_runner(tmp_path, backend, state_capture_fn=lambda: dict(state))
    runner._rpc = AsyncMock(return_value={"ok": True, "status": "completed", "action": "click_at"})  # type: ignore[method-assign]

    await runner.start()
    await runner.start_call(trigger="test")

    runner._active_target_hwnd = 5555
    # Record 1 blind click with no progress
    await runner._handle_tool_call(
        ToolCallEvent(
            call_id="c1",
            name="gui_action",
            arguments={"action": "click_at", "x": 100, "y": 100, "coord_space": "screen"},
        ),
        backend,
    )
    assert runner._circuit_breaker.blind_click_no_progress_count == 1
    assert runner._active_target_hwnd == 5555

    # Stream multiple non-final user transcript chunks (and even a final chunk)
    await backend.event_queue.put(
        TranscriptOutputEvent(role="user", text="no click", final=False)
    )
    await backend.event_queue.put(
        TranscriptOutputEvent(role="user", text=" the button below", final=False)
    )
    await backend.event_queue.put(
        TranscriptOutputEvent(role="user", text="no click the button below", final=True)
    )
    await asyncio.sleep(0.05)

    # Breaker blind-click streak and _active_target_hwnd must STILL be intact!
    assert runner._circuit_breaker.blind_click_no_progress_count == 1
    assert runner._active_target_hwnd == 5555

    await runner.end_call(reason="done")
    await runner.stop()


# ---------------------------------------------------------------------------
# 4. Cross-turn ActionCircuitBreaker persistence across TurnCompleteEvent
#    on unchanged (hwnd, dHash) and reset on verified progress / target change
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_circuit_breaker_persists_across_turns_and_resets_on_progress_or_target_change(
    tmp_path: Path,
) -> None:
    backend = _FakeVoiceBackend()
    current_state = {
        "hwnd": 7001,
        "title": "Player",
        "process_name": "player.exe",
        "dhash": "0" * 64,
    }
    runner = _make_runner(tmp_path, backend, state_capture_fn=lambda: dict(current_state))

    rpc_calls: list[Any] = []

    async def _fake_rpc(req: Any, *, timeout: float = 10.0) -> dict[str, Any]:
        rpc_calls.append(req)
        return {"ok": True, "status": "completed", "action": "click_at"}

    runner._rpc = _fake_rpc  # type: ignore[method-assign]

    await runner.start()
    await runner.start_call(trigger="test")
    runner._active_target_hwnd = 7001

    # Turn 1: 1st blind click with no progress
    await runner._handle_tool_call(
        ToolCallEvent(
            call_id="t1-c1",
            name="gui_action",
            arguments={"action": "click_at", "x": 100, "y": 100, "coord_space": "screen"},
        ),
        backend,
    )
    assert runner._circuit_breaker.blind_click_no_progress_count == 1
    assert runner._circuit_breaker.tripped is False

    # TurnCompleteEvent while (hwnd=7001, dhash=0..0) is UNCHANGED -> streak preserved!
    await backend.event_queue.put(TurnCompleteEvent())
    await asyncio.sleep(0.05)
    assert runner._circuit_breaker.blind_click_no_progress_count == 1
    assert runner._active_target_hwnd == 7001

    # Turn 2: 2nd blind click on unchanged window trips the breaker across turns
    await runner._handle_tool_call(
        ToolCallEvent(
            call_id="t2-c2",
            name="gui_action",
            arguments={"action": "click_at", "x": 102, "y": 102, "coord_space": "screen"},
        ),
        backend,
    )
    assert runner._circuit_breaker.tripped is True

    # Another TurnCompleteEvent on unchanged (hwnd, dhash) STILL preserves tripped breaker!
    await backend.event_queue.put(TurnCompleteEvent())
    await asyncio.sleep(0.05)
    assert runner._circuit_breaker.tripped is True
    assert runner._active_target_hwnd == 7001

    # Next blind click is blocked BEFORE hitting daemon RPC
    calls_before = len(rpc_calls)
    await runner._handle_tool_call(
        ToolCallEvent(
            call_id="t3-blocked",
            name="gui_action",
            arguments={"action": "click_at", "x": 105, "y": 105, "coord_space": "screen"},
        ),
        backend,
    )
    assert len(rpc_calls) == calls_before
    assert backend.sent_tool_responses[-1].response["status"] == "blocked_by_circuit_breaker"

    # Case A: Explicit view dHash change (verified visual progress) resets breaker on TurnComplete
    current_state["dhash"] = "f" * 64
    await backend.event_queue.put(TurnCompleteEvent())
    await asyncio.sleep(0.05)
    assert runner._circuit_breaker.tripped is False
    assert runner._circuit_breaker.blind_click_no_progress_count == 0

    # Re-trip breaker on new view, then verify target window HWND change resets it
    for idx in range(2):
        await runner._handle_tool_call(
            ToolCallEvent(
                call_id=f"t4-c{idx}",
                name="gui_action",
                arguments={"action": "click_at", "x": 50 + idx, "y": 50, "coord_space": "screen"},
            ),
            backend,
        )
    assert runner._circuit_breaker.tripped is True

    current_state["hwnd"] = 8888
    current_state["title"] = "Other App"
    await backend.event_queue.put(TurnCompleteEvent())
    await asyncio.sleep(0.05)
    assert runner._circuit_breaker.tripped is False
    assert runner._active_target_hwnd == 8888

    await runner.end_call(reason="done")
    await runner.stop()


# ---------------------------------------------------------------------------
# 5. Wired Default ActionVerifier in voice.py & VoiceRunner._capture_desktop_state
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_wired_action_verifier_produces_live_desktop_and_media_state_diff(
    tmp_path: Path,
) -> None:
    focus_calls = 0

    class _FakeFocusBackend:
        def get_foreground_context(self) -> WindowContext:
            nonlocal focus_calls
            focus_calls += 1
            if focus_calls == 1:
                return WindowContext(hwnd=101, title="Before Title", process_name="app.exe")
            return WindowContext(hwnd=102, title="After Title", process_name="app.exe")

        def get_window_context(self, hwnd: int) -> WindowContext | None:
            return WindowContext(hwnd=hwnd, title="After Title", process_name="app.exe")

        def is_window_valid(self, hwnd: int) -> bool:
            return True

        def is_minimized(self, hwnd: int) -> bool:
            return False

        def get_extended_frame_bounds(self, hwnd: int) -> tuple[int, int, int, int]:
            return (0, 0, 800, 600)

        def get_monitor_work_area(self, hwnd: int) -> tuple[int, int, int, int]:
            return (0, 0, 1920, 1040)

    media_calls = 0

    class _FakeMediaBackend:
        def get_active_session(self, timeout_s: float = 0.5) -> MediaSessionState:
            nonlocal media_calls
            media_calls += 1
            if media_calls == 1:
                return MediaSessionState(
                    playback_status="paused", title="Song A", artist="Artist A", available=True
                )
            return MediaSessionState(
                playback_status="playing", title="Song B", artist="Artist B", available=True
            )

    verifier = build_default_action_verifier(
        focus_backend=_FakeFocusBackend(),
        media_backend=_FakeMediaBackend(),
    )
    assert isinstance(verifier, ActionVerifier)
    # ActionVerifier stores private _focus_backend/_media_backend; runner must read them!
    backend = _FakeVoiceBackend()
    runner = _make_runner(tmp_path, backend, action_verifier=verifier)
    runner._rpc = AsyncMock(return_value={"ok": True, "status": "completed", "action": "click_at"})  # type: ignore[method-assign]

    await runner._handle_tool_call(
        ToolCallEvent(
            call_id="ver-1",
            name="gui_action",
            arguments={"action": "click_at", "x": 100, "y": 100, "coord_space": "screen"},
        ),
        backend,
    )

    resp = backend.sent_tool_responses[-1].response
    diff = resp["state_diff"]
    assert diff["changed"] is True
    assert diff["window_changed"] is True
    assert diff["media_changed"] is True
    assert diff["before"]["hwnd"] == 101
    assert diff["after"]["hwnd"] == 102
    assert diff["before"]["media"]["playback_status"] == "paused"
    assert diff["after"]["media"]["playback_status"] == "playing"


def test_run_voice_service_wires_default_action_verifier(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("CLEA_DATA_DIR", str(tmp_path))
    sentinel_verifier = ActionVerifier(focus_backend=MagicMock(), media_backend=MagicMock())

    with (
        patch(
            "clea.surfaces.cli.voice.build_default_action_verifier",
            return_value=sentinel_verifier,
        ) as mock_builder,
        patch("clea.surfaces.cli.voice.VoiceRunner") as mock_runner_cls,
    ):
        mock_runner = MagicMock()
        mock_runner.run_forever = AsyncMock(return_value=None)
        mock_runner_cls.return_value = mock_runner

        run_voice_service()

    assert mock_builder.called
    assert mock_runner_cls.call_args.kwargs["action_verifier"] is sentinel_verifier


# ---------------------------------------------------------------------------
# 6. Single-frame look_at_screen, inspect_screen HWND sync, omitted coord_space
#    inheriting active normalized_1000, and legacy voice_config.toml warning
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_single_frame_look_at_screen_inspect_screen_hwnd_and_omitted_coord_space(
    tmp_path: Path,
) -> None:
    png_bytes = _make_png_bytes(1200, 800)
    captured_targets: list[str] = []

    class _FakeScreenService:
        _has_injected_api = False
        _grabber = None

        def capture(
            self,
            *,
            target: str = "active_window",
            monitor: int | None = None,
        ) -> ScreenCaptureResult:
            captured_targets.append(target)
            return ScreenCaptureResult(
                image_bytes=png_bytes,
                target=target,
                width=1200,
                height=800,
                window_title="Editor",
                process_name="code.exe",
                mime_type="image/png",
                app_name="VS Code",
                window_bounds=(100, 50, 1300, 850),
                monitor_index=1,
                monitor_bounds=(0, 0, 1920, 1080),
                captured_at="2026-11-27T00:00:00Z",
                capture_seq=1,
                image_width=1200,
                image_height=800,
                screen_width=1200 if target == "active_window" else 1920,
                screen_height=800 if target == "active_window" else 1080,
                virtual_left=100 if target == "active_window" else 0,
                virtual_top=50 if target == "active_window" else 0,
                dpi_scale=1.25,
                coord_space="normalized_1000",
                hwnd=9100,
            )

    backend = _FakeVoiceBackend()
    runner = _make_runner(tmp_path, backend, screen_service=_FakeScreenService())
    rpc_requests: list[Any] = []

    async def _fake_rpc(req: Any, *, timeout: float = 10.0) -> dict[str, Any]:
        rpc_requests.append(req)
        if isinstance(req, OsInspectScreenRequest):
            return {
                "ok": True,
                "status": "ok",
                "source": "active_window",
                "hwnd": 9200,
                "capture_id": "cap-inspect-9200",
                "capture_bounds": [200, 100, 1400, 900],
                "window_title": "Browser",
                "process_name": "chrome.exe",
                "width": 1200,
                "height": 800,
                "image_width": 1200,
                "image_height": 800,
                "screen_width": 1200,
                "screen_height": 800,
                "virtual_left": 200,
                "virtual_top": 100,
                "dpi_scale": 1.5,
                "coord_space": "normalized_1000",
            }
        return {"ok": True, "status": "completed", "action": "click_at"}

    runner._rpc = _fake_rpc  # type: ignore[method-assign]

    # 6a. Omitted target in look_at_screen defaults to single-frame active_window
    await runner._handle_tool_call(
        ToolCallEvent(
            call_id="look-default",
            name="look_at_screen",
            arguments={"reason": "check UI"},
        ),
        backend,
    )
    assert captured_targets[-1] == "active_window"
    resp_default = backend.sent_tool_responses[-1]
    assert len(resp_default.media_parts) == 1
    assert resp_default.response["frames_sent"] == 1
    assert resp_default.response["image_order"] == ["active_window"]
    assert runner._active_target_hwnd == 9100
    assert runner._last_screen_geometry is not None
    assert runner._last_screen_geometry["hwnd"] == 9100
    assert runner._last_screen_geometry["capture_bounds"] == [100, 50, 1300, 850]
    assert runner._last_screen_geometry["capture_id"]

    # 6b. Explicit full_screen sends ONLY 1 full_screen frame (no implicit dual-frame!)
    await runner._handle_tool_call(
        ToolCallEvent(
            call_id="look-full",
            name="look_at_screen",
            arguments={"target": "full_screen", "reason": "desktop layout"},
        ),
        backend,
    )
    assert captured_targets == ["active_window", "full_screen"]
    resp_full = backend.sent_tool_responses[-1]
    assert len(resp_full.media_parts) == 1
    assert resp_full.response["frames_sent"] == 1
    assert resp_full.response["image_order"] == ["full_screen"]
    assert "active_window_mime_type" not in resp_full.response

    # 6c. inspect_screen updates _active_target_hwnd and _last_screen_geometry
    await runner._handle_tool_call(
        ToolCallEvent(call_id="insp-1", name="inspect_screen", arguments={"target": "active"}),
        backend,
    )
    assert runner._active_target_hwnd == 9200
    assert runner._last_screen_geometry is not None
    assert runner._last_screen_geometry["hwnd"] == 9200
    assert runner._last_screen_geometry["capture_id"] == "cap-inspect-9200"
    assert runner._last_screen_geometry["capture_bounds"] == [200, 100, 1400, 900]

    # 6d. Pointer gui_action omitting coord_space inherits normalized_1000 + geometry
    await runner._handle_tool_call(
        ToolCallEvent(
            call_id="click-omitted",
            name="gui_action",
            arguments={"action": "click_at", "x": 500, "y": 500},
        ),
        backend,
    )
    gui_req = rpc_requests[-1]
    assert isinstance(gui_req, OsGuiActionRequest)
    assert gui_req.params.coord_space == "normalized_1000"
    assert gui_req.params.image_width == 1200
    assert gui_req.params.image_height == 800
    assert gui_req.params.screen_width == 1200
    assert gui_req.params.screen_height == 800
    assert gui_req.params.virtual_left == 200
    assert gui_req.params.virtual_top == 100
    assert gui_req.params.dpi_scale == 1.5
    assert gui_req.params.capture_id == "cap-inspect-9200"
    assert gui_req.params.capture_bounds == [200, 100, 1400, 900]
    assert gui_req.params.expected_hwnd == 9200

    # 6e. Explicit coord_space="screen" remains raw screen (does NOT overwrite with normalized_1000)
    await runner._handle_tool_call(
        ToolCallEvent(
            call_id="click-explicit-screen",
            name="gui_action",
            arguments={"action": "click_at", "x": 640, "y": 480, "coord_space": "screen"},
        ),
        backend,
    )
    gui_screen_req = rpc_requests[-1]
    assert isinstance(gui_screen_req, OsGuiActionRequest)
    assert gui_screen_req.params.coord_space == "screen"
    assert gui_screen_req.params.image_width is None


def test_legacy_vision_config_warning_non_destructive_and_emitted_in_doctor_and_startup(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setenv("CLEA_DATA_DIR", str(tmp_path))
    cfg_path = tmp_path / "voice_config.toml"
    original_toml = 'image_max_side = 768\nmedia_resolution = "low"\n'
    cfg_path.write_text(original_toml, encoding="utf-8")

    cfg = load_voice_config(cfg_path)
    warnings = check_legacy_vision_config_warnings(cfg, config_path=cfg_path)
    assert len(warnings) == 2
    assert any("image_max_side=768" in w for w in warnings)
    assert any("media_resolution='low'" in w for w in warnings)

    # Clean config produces zero warnings
    assert check_legacy_vision_config_warnings(VoiceConfig()) == []

    # voice doctor prints warnings and NEVER mutates the TOML file
    with (
        patch("clea.surfaces.cli.voice.list_audio_devices", return_value=[]),
        patch("clea.surfaces.cli.voice.resolve_audio_device", side_effect=RuntimeError("mocked")),
    ):
        run_voice_doctor(download_models=False)
    doctor_out = capsys.readouterr().out
    assert "image_max_side=768" in doctor_out
    assert "media_resolution='low'" in doctor_out
    assert cfg_path.read_text(encoding="utf-8") == original_toml

    # run_voice_service emits warnings at startup without mutating the TOML file
    with (
        caplog.at_level(logging.WARNING),
        patch("clea.surfaces.cli.voice.build_default_action_verifier", return_value=None),
        patch("clea.surfaces.cli.voice.VoiceRunner") as mock_runner_cls,
    ):
        mock_runner = MagicMock()
        mock_runner.run_forever = AsyncMock(return_value=None)
        mock_runner_cls.return_value = mock_runner
        run_voice_service()
    service_err = capsys.readouterr().err
    assert "image_max_side=768" in service_err
    assert "media_resolution='low'" in service_err
    assert cfg_path.read_text(encoding="utf-8") == original_toml


# ---------------------------------------------------------------------------
# 7. Prompt & Tool Schema Alignment
# ---------------------------------------------------------------------------


def test_prompt_and_tool_schema_alignment() -> None:
    tools = {t.name: t for t in default_voice_tools()}
    gui_tool = tools["gui_action"]
    assert "inspect_screen" in gui_tool.description
    assert "look_at_screen" in gui_tool.description
    assert "normalized_1000" in gui_tool.description
    assert "type_text" in gui_tool.description
    coord_desc = gui_tool.parameters_schema["properties"]["coord_space"]["description"]
    assert "inspect_screen" in coord_desc and "look_at_screen" in coord_desc

    open_url_tool = tools["open_url"]
    assert "protocol" in open_url_tool.description.lower()
    assert "goal_verified" in open_url_tool.description

    for label, prompt in (
        ("voice", BASE_VOICE_SYSTEM_INSTRUCTION),
        ("chat", _BASE_SYSTEM_PROMPT),
    ):
        low = prompt.lower()
        assert "inspect_screen" in low and "look_at_screen" in low, (
            f"{label} missing both screen tools"
        )
        assert "normalized_1000" in low, f"{label} missing normalized_1000"
        assert "goal_verified" in low, f"{label} missing goal_verified rule"
        assert "view_changed" in low, f"{label} missing view_changed rule"
        assert "zero-tool completion claim" in low, f"{label} missing zero-tool steering guard"
        assert "gui_action(type_text)" in low, f"{label} missing type_text preference"
        assert "write_clipboard" in low, f"{label} missing clipboard paste warning"

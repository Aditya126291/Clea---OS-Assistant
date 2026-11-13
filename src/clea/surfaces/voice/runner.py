"""Voice surface runner managing the call lifecycle, audio I/O, wake word, and backend."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import ConnectionClosed

from clea.core import auth
from clea.core.protocol import (
    CallEndedEvent,
    CallStartedEvent,
    ResponseFrame,
    VoiceDelegateTaskParams,
    VoiceDelegateTaskRequest,
    VoiceEndCallParams,
    VoiceEndCallRequest,
    VoiceStartCallParams,
    VoiceStartCallRequest,
    VoiceState,
    parse_frame,
)
from clea.paths import default_data_dir
from clea.surfaces.voice.audio import AudioManager
from clea.surfaces.voice.backends.base import (
    AudioOutputEvent,
    BackendErrorEvent,
    InterruptedEvent,
    SessionResumptionConfig,
    ToolCallCancellationEvent,
    ToolCallEvent,
    TranscriptOutputEvent,
    TurnCompleteEvent,
    UsageUpdateEvent,
    VoiceBackend,
    VoiceSessionConfig,
    VoiceToolResponse,
)
from clea.surfaces.voice.backends.gemini_live import GeminiLiveBackend
from clea.surfaces.voice.config import VoiceConfig, load_voice_config
from clea.surfaces.voice.lock import VoiceInstanceLock
from clea.surfaces.voice.memory_hooks import resolve_voice_system_instruction
from clea.surfaces.voice.wake_word import WakeWordDetector, WakeWordModelNotFoundError
from clea.surfaces.voice.win32 import HotKeyManager

logger = logging.getLogger(__name__)


def voice_metrics_path(data_dir: Path | None = None) -> Path:
    """Return path to voice_metrics.jsonl log file."""
    return (data_dir or default_data_dir()) / "logs" / "voice_metrics.jsonl"


class VoiceRunner:
    """Manages the full lifecycle of the voice surface."""

    def __init__(
        self,
        config: VoiceConfig | None = None,
        *,
        data_dir: Path | None = None,
        daemon_url: str = "ws://127.0.0.1:8765",
        token: str | None = None,
        backend_factory: Callable[[], VoiceBackend] | None = None,
        audio_manager: AudioManager | None = None,
        wake_word_detector: WakeWordDetector | None = None,
        hotkey_manager: HotKeyManager | None = None,
        enable_hotkey: bool = True,
    ) -> None:
        self.config = config or load_voice_config(create_if_missing=False)
        self.data_dir = data_dir
        self.daemon_url = daemon_url
        self.token = token
        self._backend_factory = backend_factory or (lambda: GeminiLiveBackend())
        self._audio_manager = audio_manager
        self._wake_word = wake_word_detector
        self._hotkey_manager = hotkey_manager
        self._enable_hotkey = enable_hotkey

        self._lock = VoiceInstanceLock(path=(data_dir or default_data_dir()) / "voice.lock")
        self._state: VoiceState = "idle"
        self._call_state: str = "idle"  # idle, connecting, active, ending
        self._call_id: str | None = None
        self._call_start_time: float = 0.0
        self._last_user_speech_time: float = 0.0
        self._usage_record: dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
            "thought_tokens": 0,
        }

        self._daemon_ws: ClientConnection | None = None
        self._active_backend: VoiceBackend | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event = asyncio.Event()
        self._backend_consume_task: asyncio.Task[None] | None = None
        self._silence_watchdog_task: asyncio.Task[None] | None = None
        self._pending_tasks: dict[str, asyncio.Future[dict[str, Any]]] = {}

    @property
    def call_state(self) -> str:
        return self._call_state

    @property
    def voice_state(self) -> VoiceState:
        return self._state

    @property
    def call_id(self) -> str | None:
        return self._call_id

    async def start(self) -> None:
        """Start the voice runner with lock acquisition and background tasks."""
        self._loop = asyncio.get_running_loop()
        self._lock.acquire()
        try:
            # 1. Initialize audio manager if not provided
            if self._audio_manager is None:
                self._audio_manager = AudioManager(
                    input_device=self.config.input_device,
                    output_device=self.config.output_device,
                    mic_mute_while_speaking=self.config.mic_mute_while_speaking,
                    on_audio_input=self._on_audio_chunk,
                )
            else:
                self._audio_manager.set_input_callback(self._on_audio_chunk)

            self._audio_manager.start()

            # 2. Initialize wake word detector if not provided
            if self._wake_word is None:
                try:
                    self._wake_word = WakeWordDetector(
                        model_name=self.config.wake_word_model,
                        threshold=self.config.wake_threshold,
                        data_dir=self.data_dir,
                    )
                except WakeWordModelNotFoundError as exc:
                    logger.error(str(exc))
                    raise

            # 3. Initialize Win32 hotkey if enabled
            if self._enable_hotkey:
                try:
                    if self._hotkey_manager is None:
                        self._hotkey_manager = HotKeyManager()
                    self._hotkey_manager.start(
                        self.config.hotkey,
                        on_triggered=self._on_hotkey_triggered,
                    )
                except Exception as exc:
                    logger.warning("Could not register hotkey '%s': %s", self.config.hotkey, exc)

            logger.info("VoiceRunner started in state: IDLE (hotkey=%s).", self.config.hotkey)
        except Exception:
            self._lock.release()
            raise

    async def run_forever(self) -> None:
        """Connect to daemon and process events until stopped."""
        await self.start()
        try:
            while not self._stop_event.is_set():
                try:
                    await self._connect_daemon_and_listen()
                except (OSError, ConnectionClosed) as exc:
                    if self._stop_event.is_set():
                        break
                    logger.info("Daemon connection lost (%s); retrying in 2s...", exc)
                    await asyncio.sleep(2.0)
        finally:
            await self.stop()

    async def stop(self) -> None:
        """Clean up all voice runner resources."""
        self._stop_event.set()

        if self._call_state in ("connecting", "active"):
            await self.end_call(reason="shutdown")

        if self._hotkey_manager is not None:
            self._hotkey_manager.stop()
            self._hotkey_manager = None

        if self._audio_manager is not None:
            self._audio_manager.stop()

        if self._daemon_ws is not None:
            with contextlib.suppress(Exception):
                await self._daemon_ws.close()
            self._daemon_ws = None

        self._lock.release()
        logger.info("VoiceRunner stopped.")

    def _on_hotkey_triggered(self) -> None:
        """Called from hotkey thread when Ctrl+Alt+V is pressed."""
        if self._loop is None or self._loop.is_closed():
            return
        asyncio.run_coroutine_threadsafe(self._handle_hotkey_press(), self._loop)

    async def _handle_hotkey_press(self) -> None:
        logger.info("Hotkey pressed. Current call state: %s", self._call_state)
        if self._call_state == "idle":
            await self.start_call(trigger="hotkey")
        elif self._call_state in ("connecting", "active"):
            await self.end_call(reason="hotkey")

    def _on_audio_chunk(self, pcm_bytes: bytes) -> None:
        """Called on sounddevice input thread for each microphone chunk."""
        if self._loop is None or self._loop.is_closed():
            return

        if self._call_state == "idle" and self._wake_word is not None:
            # Local wake word check in IDLE
            try:
                detected, score, _key = self._wake_word.process_audio(pcm_bytes)
                if detected:
                    logger.info("Wake word detected locally with score %.3f!", score)
                    asyncio.run_coroutine_threadsafe(
                        self.start_call(trigger="wake_word"),
                        self._loop,
                    )
            except Exception:
                logger.exception("Error during wake word processing.")
        elif self._call_state == "active" and self._active_backend is not None:
            # Stream audio to backend
            self._last_user_speech_time = time.monotonic()
            asyncio.run_coroutine_threadsafe(
                self._active_backend.send_audio(pcm_bytes),
                self._loop,
            )

    async def start_call(self, *, trigger: str = "manual") -> None:
        """Initiate transition to CONNECTING -> ACTIVE."""
        if self._call_state != "idle":
            logger.debug("Cannot start call; current state is %s", self._call_state)
            return

        self._call_state = "connecting"
        self._state = "thinking"
        logger.info("Starting call (trigger=%s)...", trigger)

        try:
            # 1. Start call in daemon if connected
            if self._daemon_ws is not None:
                start_req = VoiceStartCallRequest(
                    params=VoiceStartCallParams(trigger=trigger)  # type: ignore[arg-type]
                )
                await self._daemon_ws.send(start_req.model_dump_json())

            # 2. Resolve system instruction safely
            system_instruction = await resolve_voice_system_instruction()

            # 3. Create backend and connect
            session_cfg = VoiceSessionConfig(
                system_instruction=system_instruction,
                enable_web_search=self.config.enable_web_search,
                media_resolution=self.config.media_resolution,
                reconnect_limit=self.config.reconnect_limit,
                session_resumption=SessionResumptionConfig(enabled=True),
            )
            backend = self._backend_factory()
            await backend.connect(session_cfg)
            self._active_backend = backend

            self._call_state = "active"
            self._state = "listening"
            self._call_start_time = time.monotonic()
            self._last_user_speech_time = time.monotonic()
            self._usage_record = {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cached_tokens": 0,
                "thought_tokens": 0,
            }

            # Start backend event consumer
            self._backend_consume_task = asyncio.create_task(
                self._consume_backend_events(backend),
                name="VoiceRunnerBackendConsumer",
            )
            # Start silence timeout watchdog
            self._silence_watchdog_task = asyncio.create_task(
                self._silence_watchdog(),
                name="VoiceRunnerSilenceWatchdog",
            )
            logger.info("Call is now ACTIVE.")
        except Exception as exc:
            logger.error("Failed starting voice call: %s", exc)
            await self.end_call(reason=f"start_failed: {exc}")

    async def end_call(self, *, reason: str = "user_done") -> None:
        """End the active call and transition back to IDLE."""
        if self._call_state == "idle":
            return

        self._call_state = "ending"
        logger.info("Ending call (call_id=%s, reason=%s)...", self._call_id, reason)

        # Cancel watchdog and consumer
        if self._silence_watchdog_task is not None:
            self._silence_watchdog_task.cancel()
            self._silence_watchdog_task = None

        # Stop audio playback and clear output buffer immediately
        if self._audio_manager is not None:
            self._audio_manager.stop_output()

        # Close backend
        if self._active_backend is not None:
            backend = self._active_backend
            self._active_backend = None
            with contextlib.suppress(Exception):
                await backend.close()

        if self._backend_consume_task is not None:
            self._backend_consume_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._backend_consume_task
            self._backend_consume_task = None

        # Notify daemon if connected
        duration = time.monotonic() - self._call_start_time if self._call_start_time > 0 else 0.0
        if self._daemon_ws is not None and self._call_id:
            with contextlib.suppress(Exception):
                end_req = VoiceEndCallRequest(
                    params=VoiceEndCallParams(call_id=self._call_id, reason=reason)
                )
                await self._daemon_ws.send(end_req.model_dump_json())

        # Write usage & metrics record
        self._write_metrics_record(reason=reason, duration_seconds=duration)

        # Reset states
        self._call_state = "idle"
        self._state = "idle"
        self._call_id = None
        if self._wake_word is not None:
            self._wake_word.reset()
        logger.info("Call ended. Runner is now IDLE.")

    def _write_metrics_record(self, *, reason: str, duration_seconds: float) -> None:
        try:
            log_path = voice_metrics_path(self.data_dir)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "timestamp": datetime.now(UTC).isoformat(),
                "call_id": self._call_id,
                "reason": reason,
                "duration_seconds": round(duration_seconds, 2),
                "usage": self._usage_record,
            }
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(json.dumps(record) + "\n")
        except Exception as exc:
            logger.warning("Failed writing voice metrics: %s", exc)

    async def _silence_watchdog(self) -> None:
        """Ending trigger 4: Silence timeout."""
        timeout = self.config.silence_timeout_seconds
        poll_interval = min(1.0, max(0.05, timeout / 2))
        while self._call_state == "active":
            await asyncio.sleep(poll_interval)
            if self._audio_manager and self._audio_manager.is_speaking:
                # Assistant is talking; don't count silence against user
                self._last_user_speech_time = time.monotonic()
                continue
            elapsed = time.monotonic() - self._last_user_speech_time
            if elapsed >= timeout:
                logger.info("Silence timeout (%.1fs >= %.1fs) reached.", elapsed, timeout)
                await self.end_call(reason="silence_timeout")
                break

    async def _consume_backend_events(self, backend: VoiceBackend) -> None:
        """Consume and route events from active voice backend."""
        try:
            async for event in backend.events():
                if self._call_state != "active":
                    break

                if isinstance(event, AudioOutputEvent):
                    self._state = "speaking"
                    if self._audio_manager is not None:
                        self._audio_manager.play_audio(event.pcm_bytes)
                elif isinstance(event, InterruptedEvent):
                    # Barge-in: flush output audio immediately
                    logger.debug("Barge-in: flushing audio buffer.")
                    if self._audio_manager is not None:
                        self._audio_manager.stop_output()
                    self._state = "listening"
                elif isinstance(event, TurnCompleteEvent):
                    self._state = "listening"
                elif isinstance(event, TranscriptOutputEvent):
                    self._last_user_speech_time = time.monotonic()
                    logger.debug("[%s]: %s", event.role, event.text)
                elif isinstance(event, ToolCallEvent):
                    await self._handle_tool_call(event, backend)
                elif isinstance(event, ToolCallCancellationEvent):
                    for cid in event.call_ids:
                        fut = self._pending_tasks.pop(cid, None)
                        if fut and not fut.done():
                            fut.cancel()
                elif isinstance(event, UsageUpdateEvent):
                    self._usage_record["input_tokens"] += event.input_tokens
                    self._usage_record["output_tokens"] += event.output_tokens
                    self._usage_record["total_tokens"] += event.total_tokens
                    if event.cached_tokens:
                        self._usage_record["cached_tokens"] += event.cached_tokens
                    if event.thought_tokens:
                        self._usage_record["thought_tokens"] += event.thought_tokens
                elif isinstance(event, BackendErrorEvent):
                    logger.warning(
                        "Backend error: %s (recoverable=%s)", event.message, event.recoverable
                    )
                    if not event.recoverable:
                        await self.end_call(reason=f"backend_error: {event.message}")
                        break
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("Error in backend event consumer: %s", exc)
            await self.end_call(reason=f"consumer_error: {exc}")

    async def _handle_tool_call(self, event: ToolCallEvent, backend: VoiceBackend) -> None:
        """Execute or delegate live voice tool call."""
        logger.info("Executing voice tool: %s (%s)", event.name, event.call_id)

        # 1. end_call tool
        if event.name == "end_call":
            reason = str(event.arguments.get("reason", "model_ended_call"))
            resp = VoiceToolResponse(
                call_id=event.call_id,
                name=event.name,
                response={"status": "ok", "message": "Call ending"},
                scheduling="when_idle",
            )
            await backend.send_tool_responses([resp])
            await self.end_call(reason=reason)
            return

        # 2. delegate_task tool
        if event.name == "delegate_task":
            desc = str(event.arguments.get("description", ""))
            if self._daemon_ws is not None and desc.strip():
                req = VoiceDelegateTaskRequest(
                    params=VoiceDelegateTaskParams(description=desc.strip())
                )
                loop = asyncio.get_running_loop()
                fut: asyncio.Future[dict[str, Any]] = loop.create_future()
                self._pending_tasks[req.id] = fut

                try:
                    await self._daemon_ws.send(req.model_dump_json())
                    # Wait for task completion from daemon
                    res = await asyncio.wait_for(fut, timeout=60.0)
                    tool_resp = VoiceToolResponse(
                        call_id=event.call_id,
                        name=event.name,
                        response=res,
                        is_error=False,
                        scheduling="when_idle",
                    )
                except Exception as exc:
                    tool_resp = VoiceToolResponse(
                        call_id=event.call_id,
                        name=event.name,
                        response={"error": str(exc)},
                        is_error=True,
                        scheduling="when_idle",
                    )
                finally:
                    self._pending_tasks.pop(req.id, None)

                await backend.send_tool_responses([tool_resp])
            else:
                resp = VoiceToolResponse(
                    call_id=event.call_id,
                    name=event.name,
                    response={"error": "Daemon not connected or empty description"},
                    is_error=True,
                )
                await backend.send_tool_responses([resp])
            return

        # 3. look_at_screen / watch_screen stubs for Slice 1
        resp = VoiceToolResponse(
            call_id=event.call_id,
            name=event.name,
            response={
                "status": "not_implemented_in_slice_1",
                "note": "Screen capture arrives in Slice 2.",
            },
            scheduling="when_idle",
        )
        await backend.send_tool_responses([resp])

    async def _connect_daemon_and_listen(self) -> None:
        """Connect to Clea daemon and process incoming daemon requests/events."""
        token = self.token or auth.read_token(auth.token_path(self.data_dir))
        if not token:
            logger.debug("No daemon token found; running offline until daemon starts.")
            await asyncio.sleep(2.0)
            return

        async with connect(
            self.daemon_url,
            subprotocols=auth.client_subprotocols(token),  # type: ignore[arg-type]
        ) as ws:
            self._daemon_ws = ws
            logger.info("Connected to daemon at %s.", self.daemon_url)

            while not self._stop_event.is_set():
                raw = await ws.recv()
                frame = parse_frame(raw)

                if isinstance(frame, CallStartedEvent):
                    self._call_id = frame.data.call_id
                    if self._call_state == "idle":
                        # Daemon started call (e.g. from UI)
                        await self.start_call(trigger="daemon_event")
                elif isinstance(frame, CallEndedEvent):
                    if self._call_state in ("connecting", "active"):
                        await self.end_call(reason=frame.data.reason or "daemon_event")
                elif isinstance(frame, ResponseFrame):
                    # Check if this resolves a delegated task
                    fut = self._pending_tasks.get(frame.id)
                    if fut and not fut.done():
                        if frame.ok:
                            fut.set_result(frame.result)
                        else:
                            fut.set_exception(RuntimeError(frame.error or "Task failed"))

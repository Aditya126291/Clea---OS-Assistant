"""Voice surface runner managing the call lifecycle, audio I/O, wake word, and backend."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import logging.handlers
import sys
import time
from collections.abc import AsyncIterator, Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import sounddevice as sd
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import InvalidURI, WebSocketException

from clea.core import auth
from clea.core.protocol import (
    BaseEvent,
    CallEndedData,
    CallEndedEvent,
    CallStartedData,
    CallStartedEvent,
    ResponseFrame,
    TranscriptData,
    TranscriptEvent,
    UsageData,
    UsageEvent,
    VoiceDelegateTaskParams,
    VoiceDelegateTaskRequest,
    VoiceEndCallParams,
    VoiceEndCallRequest,
    VoiceStartCallParams,
    VoiceStartCallRequest,
    VoiceState,
    VoiceStateData,
    VoiceStateEvent,
    new_id,
    parse_frame,
)
from clea.paths import default_data_dir
from clea.surfaces.voice.audio import (
    AmbiguousAudioDeviceError,
    AudioDeviceNotFoundError,
    AudioManager,
    refresh_audio_devices,
)
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
from clea.surfaces.voice.memory_hooks import (
    resolve_voice_system_instruction,
    safe_consolidate_voice_call,
    safe_store_voice_transcript,
)
from clea.surfaces.voice.screen import ScreenCaptureService
from clea.surfaces.voice.wake_word import WakeWordDetector, WakeWordModelNotFoundError
from clea.surfaces.voice.win32 import HotKeyManager, enable_per_monitor_dpi_awareness

logger = logging.getLogger(__name__)

# Root-mean-square amplitude threshold for 16-bit PCM to distinguish
# active voice from background silence.
SPEECH_RMS_THRESHOLD: float = 300.0


def compute_audio_rms(pcm_bytes: bytes) -> float:
    """Calculate RMS energy of 16-bit mono PCM audio chunk."""
    if not pcm_bytes:
        return 0.0
    arr = np.frombuffer(pcm_bytes, dtype=np.int16)
    if len(arr) == 0:
        return 0.0
    return float(np.sqrt(np.mean(arr.astype(np.float32) ** 2)))


def voice_metrics_path(data_dir: Path | None = None) -> Path:
    """Return path to voice_metrics.jsonl log file."""
    return (data_dir or default_data_dir()) / "logs" / "voice_metrics.jsonl"


def voice_log_path(data_dir: Path | None = None) -> Path:
    """Return path to voice.log log file."""
    return (data_dir or default_data_dir()) / "logs" / "voice.log"


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
        on_event: Callable[[BaseEvent], None] | None = None,
        screen_service: ScreenCaptureService | None = None,
        debug_audio: bool = False,
        audio_init_retries: int = 8,
        audio_init_retry_delay: float = 1.5,
        daemon_retry_delay: float = 2.0,
    ) -> None:
        self.config = config or load_voice_config(create_if_missing=False)
        self.data_dir = data_dir
        self.daemon_url = daemon_url
        self.token = token
        self.debug_audio = debug_audio
        self._audio_init_retries = max(1, audio_init_retries)
        self._audio_init_retry_delay = max(0.0, audio_init_retry_delay)
        self._daemon_retry_delay = max(0.05, daemon_retry_delay)
        self._daemon_ever_connected = False
        self._setup_voice_file_logging()
        self._backend_factory = backend_factory or (lambda: GeminiLiveBackend())
        self._audio_manager = audio_manager
        self._wake_word = wake_word_detector
        self._hotkey_manager = hotkey_manager
        self._enable_hotkey = enable_hotkey
        self._on_event = on_event
        self._screen_service = screen_service or ScreenCaptureService(
            blocklist=self.config.blocklist,
            image_max_side=self.config.image_max_side,
        )
        self._watch_screen_task: asyncio.Task[None] | None = None

        self._lock = VoiceInstanceLock(path=(data_dir or default_data_dir()) / "voice.lock")
        self._state: VoiceState = "idle"
        self._call_state: str = "idle"  # idle, connecting, active, ending
        self._call_id: str | None = None
        self._call_start_time: float = 0.0
        self._last_user_speech_time: float = 0.0
        self._connect_latency_ms: float = 0.0
        self._first_audio_latency_ms: float | None = None
        self._transcript_history: list[dict[str, Any]] = []
        self._usage_record: dict[str, int] = {
            "input_tokens": 0,
            "output_tokens": 0,
            "total_tokens": 0,
            "cached_tokens": 0,
            "thought_tokens": 0,
        }

        self._events_queue: asyncio.Queue[BaseEvent] = asyncio.Queue()
        self._daemon_ws: ClientConnection | None = None
        self._active_backend: VoiceBackend | None = None
        self._loop: asyncio.AbstractEventLoop | None = None
        self._stop_event = asyncio.Event()
        self._backend_consume_task: asyncio.Task[None] | None = None
        self._silence_watchdog_task: asyncio.Task[None] | None = None
        self._stall_watchdog_task: asyncio.Task[None] | None = None
        self._pending_tasks: dict[str, asyncio.Future[dict[str, Any]]] = {}

        self._stall_timer_armed: bool = False
        self._stall_accumulated_seconds: float = 0.0
        self._audio_sent_start_time: float | None = None
        self._stall_reconnected_once: bool = False
        self._frames_sent_window: int = 0
        self._last_fps_report_time: float = time.monotonic()

    def _setup_voice_file_logging(self) -> None:
        """Ensure voice log file exists and receives log records."""
        try:
            log_file = voice_log_path(self.data_dir)
            log_file.parent.mkdir(parents=True, exist_ok=True)
            voice_logger = logging.getLogger("clea.surfaces.voice")
            root_logger = logging.getLogger()
            has_file = any(
                isinstance(h, logging.FileHandler)
                and getattr(h, "baseFilename", None) == str(log_file)
                for h in (*voice_logger.handlers, *root_logger.handlers)
            )
            if not has_file:
                fh = logging.handlers.RotatingFileHandler(
                    log_file,
                    maxBytes=5 * 1024 * 1024,
                    backupCount=3,
                    encoding="utf-8",
                )
                fh.setLevel(logging.INFO)
                fh.setFormatter(
                    logging.Formatter("%(asctime)s [%(levelname)s] %(name)s: %(message)s")
                )
                voice_logger.addHandler(fh)
        except Exception as exc:
            logger.debug("Could not configure voice log file handler: %s", exc)

    @property
    def call_state(self) -> str:
        return self._call_state

    @property
    def voice_state(self) -> VoiceState:
        return self._state

    @property
    def call_id(self) -> str | None:
        return self._call_id

    async def _send_daemon_payload(self, ws: ClientConnection, payload: str) -> None:
        try:
            await ws.send(payload)
        except (OSError, WebSocketException) as exc:
            logger.debug("Could not send VoiceStateEvent to daemon: %s", exc)

    def _set_voice_state(self, state: VoiceState, error: str | None = None) -> None:
        """Update voice state and emit a typed VoiceStateEvent."""
        if self._state != state or error is not None:
            self._state = state
            evt = VoiceStateEvent(data=VoiceStateData(state=state, error=error))
            self._emit_event(evt)
            if error:
                logger.error("Voice state error: %s", error)
            if (
                self._daemon_ws is not None
                and self._loop is not None
                and not self._loop.is_closed()
            ):
                ws = self._daemon_ws
                payload = evt.model_dump_json()
                try:
                    running_loop = None
                    with contextlib.suppress(RuntimeError):
                        running_loop = asyncio.get_running_loop()
                    if running_loop is self._loop:
                        self._loop.create_task(self._send_daemon_payload(ws, payload))
                    else:
                        asyncio.run_coroutine_threadsafe(
                            self._send_daemon_payload(ws, payload),
                            self._loop,
                        )
                except Exception as exc:
                    logger.debug("Could not send VoiceStateEvent to daemon: %s", exc)

    def _emit_event(self, event: BaseEvent) -> None:
        """Emit a typed protocol event to subscriber callback and internal queue."""
        self._events_queue.put_nowait(event)
        if self._on_event is not None:
            try:
                self._on_event(event)
            except Exception:
                logger.exception("Error in voice runner on_event callback.")

    async def events(self) -> AsyncIterator[BaseEvent]:
        """Yield typed protocol events emitted during runner execution."""
        while True:
            evt = await self._events_queue.get()
            yield evt

    async def start(self) -> None:
        """Start the voice runner with DPI awareness, lock acquisition, and background tasks."""
        enable_per_monitor_dpi_awareness()
        self._loop = asyncio.get_running_loop()
        self._lock.acquire()
        try:
            # 1. Initialize audio manager if not provided
            if self._audio_manager is None:
                self._audio_manager = AudioManager(
                    input_device=self.config.input_device,
                    output_device=self.config.output_device,
                    preferred_hostapi=self.config.preferred_hostapi,
                    mic_mute_while_speaking=self.config.mic_mute_while_speaking,
                    on_audio_input=self._on_audio_chunk,
                    chunk_size=1280,
                )
            else:
                self._audio_manager.set_input_callback(self._on_audio_chunk)

            for attempt in range(1, self._audio_init_retries + 1):
                try:
                    self._audio_manager.start()
                    break
                except AmbiguousAudioDeviceError:
                    raise
                except (AudioDeviceNotFoundError, sd.PortAudioError, OSError) as exc:
                    if attempt >= self._audio_init_retries or self._stop_event.is_set():
                        logger.error(
                            "Audio initialization failed after %d attempt(s): %s",
                            attempt,
                            exc,
                        )
                        raise
                    logger.warning(
                        "Audio devices not ready (attempt %d/%d: %s); retrying in %.1fs...",
                        attempt,
                        self._audio_init_retries,
                        exc,
                        self._audio_init_retry_delay,
                    )
                    if self._audio_init_retry_delay > 0:
                        with contextlib.suppress(TimeoutError):
                            await asyncio.wait_for(
                                self._stop_event.wait(),
                                timeout=self._audio_init_retry_delay,
                            )
                    if self._stop_event.is_set():
                        raise
                    refresh_audio_devices()

            if self._audio_manager.resolved_input_device:
                inp = self._audio_manager.resolved_input_device
                logger.info(
                    "Input device: #%d '%s' (host API: %s, native rate: %d Hz)",
                    inp["index"],
                    inp["name"],
                    inp.get("hostapi_name", "Unknown"),
                    int(inp.get("default_samplerate", 16000)),
                )
            if self._audio_manager.resolved_output_device:
                out = self._audio_manager.resolved_output_device
                logger.info(
                    "Output device: #%d '%s' (host API: %s, native rate: %d Hz)",
                    out["index"],
                    out["name"],
                    out.get("hostapi_name", "Unknown"),
                    int(out.get("default_samplerate", 24000)),
                )

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
            if self._audio_manager is not None:
                with contextlib.suppress(Exception):
                    self._audio_manager.stop()
            self._lock.release()
            raise

    async def run_forever(self) -> None:
        """Connect to daemon and process events until stopped."""
        await self.start()
        try:
            while not self._stop_event.is_set():
                try:
                    await self._connect_daemon_and_listen()
                except InvalidURI:
                    raise
                except (OSError, WebSocketException) as exc:
                    if self._stop_event.is_set():
                        break
                    if self._daemon_ever_connected:
                        logger.info(
                            "Daemon connection lost (%s); retrying in %.1fs...",
                            exc,
                            self._daemon_retry_delay,
                        )
                    else:
                        logger.info(
                            "Daemon at %s not ready yet (%s); retrying in %.1fs...",
                            self.daemon_url,
                            exc,
                            self._daemon_retry_delay,
                        )
                    with contextlib.suppress(TimeoutError):
                        await asyncio.wait_for(
                            self._stop_event.wait(),
                            timeout=self._daemon_retry_delay,
                        )
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
            now = time.monotonic()
            self._frames_sent_window += 1
            if self.debug_audio and (now - self._last_fps_report_time) >= 1.0:
                elapsed = now - self._last_fps_report_time
                fps = self._frames_sent_window / elapsed
                rms = compute_audio_rms(pcm_bytes)
                msg = f"[AUDIO-DEBUG] Wake listen: {fps:4.1f} fps | 16 kHz RMS: {rms:6.1f}"
                print(msg, flush=True)
                self._frames_sent_window = 0
                self._last_fps_report_time = now

            # Local wake word check in IDLE
            try:
                detected, score, _key = self._wake_word.process_audio(pcm_bytes)
                if detected:
                    self._wake_word.reset()
                    logger.info("Wake word detected locally with score %.3f!", score)
                    asyncio.run_coroutine_threadsafe(
                        self.start_call(trigger="wake_word"),
                        self._loop,
                    )
            except Exception:
                logger.exception("Error during wake word processing.")
        elif self._call_state == "active" and self._active_backend is not None:
            # Check audio energy for real speech vs background silence
            rms = compute_audio_rms(pcm_bytes)
            if rms >= SPEECH_RMS_THRESHOLD:
                self._last_user_speech_time = time.monotonic()

            is_speaking = bool(self._audio_manager and self._audio_manager.is_speaking)
            if is_speaking:
                # Keep disarmed while the assistant is speaking
                self._stall_timer_armed = False
                self._stall_accumulated_seconds = 0.0
                self._audio_sent_start_time = None
            else:
                # Only arm and advance the response-stall timer when outbound 16 kHz PCM
                # chunks exceed speech threshold (never accumulate stall time on silence).
                if rms >= self.config.stall_speech_rms_threshold:
                    if not self._stall_timer_armed:
                        self._stall_timer_armed = True
                        self._audio_sent_start_time = time.monotonic()
                    chunk_duration = (len(pcm_bytes) // 2) / 16000.0
                    self._stall_accumulated_seconds += chunk_duration

            self._frames_sent_window += 1
            now = time.monotonic()
            if self.debug_audio and (now - self._last_fps_report_time) >= 1.0:
                elapsed = now - self._last_fps_report_time
                fps = self._frames_sent_window / elapsed
                print(f"[AUDIO-DEBUG] Sent {fps:4.1f} fps | Mic RMS: {rms:6.1f}", flush=True)
                self._frames_sent_window = 0
                self._last_fps_report_time = now

            asyncio.run_coroutine_threadsafe(
                self._active_backend.send_audio(pcm_bytes),
                self._loop,
            )

    async def start_call(self, *, trigger: str = "manual") -> None:
        """Initiate transition to CONNECTING -> ACTIVE."""
        if self._call_state != "idle":
            logger.debug("Cannot start call; current state is %s", self._call_state)
            return

        if self._call_id is None:
            self._call_id = new_id()

        self._call_state = "connecting"
        self._set_voice_state("thinking")
        self._transcript_history.clear()
        self._first_audio_latency_ms = None
        logger.info("Starting call (call_id=%s, trigger=%s)...", self._call_id, trigger)

        try:
            # 1. Start call in daemon if connected
            if self._daemon_ws is not None and trigger != "daemon_event":
                start_req = VoiceStartCallRequest(
                    params=VoiceStartCallParams(trigger=trigger)  # type: ignore[arg-type]
                )
                try:
                    await self._daemon_ws.send(start_req.model_dump_json())
                except (OSError, WebSocketException) as exc:
                    logger.warning("Could not notify daemon of call start: %s", exc)

            # 2. Resolve system instruction safely
            system_instruction = await resolve_voice_system_instruction()

            # 3. Create backend and connect with latency measurement
            session_cfg = VoiceSessionConfig(
                system_instruction=system_instruction,
                enable_web_search=self.config.enable_web_search,
                media_resolution=self.config.media_resolution,
                reconnect_limit=self.config.reconnect_limit,
                session_resumption=SessionResumptionConfig(enabled=True),
                vad_mode=self.config.vad_mode,
            )
            connect_t0 = time.monotonic()
            backend = self._backend_factory()
            await backend.connect(session_cfg)
            self._connect_latency_ms = (time.monotonic() - connect_t0) * 1000
            self._active_backend = backend
            self._stall_reconnected_once = False
            self._stall_timer_armed = False
            self._stall_accumulated_seconds = 0.0
            self._audio_sent_start_time = None

            self._call_state = "active"
            self._set_voice_state("listening")
            self._call_start_time = time.monotonic()
            self._last_user_speech_time = time.monotonic()
            self._usage_record = {
                "input_tokens": 0,
                "output_tokens": 0,
                "total_tokens": 0,
                "cached_tokens": 0,
                "thought_tokens": 0,
            }

            # Emit typed CallStartedEvent
            self._emit_event(
                CallStartedEvent(
                    data=CallStartedData(
                        call_id=self._call_id,
                        call_state="active",
                        started_at=datetime.now(UTC).isoformat(),
                    )
                )
            )

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
            # Start stall watchdog
            self._stall_watchdog_task = asyncio.create_task(
                self._stall_watchdog(),
                name="VoiceRunnerStallWatchdog",
            )
            logger.info(
                "Call %s is now ACTIVE (connect_latency=%.1fms).",
                self._call_id,
                self._connect_latency_ms,
            )
        except Exception as exc:
            logger.error("Failed starting voice call: %s", exc)
            print(f"\n[ERROR] Voice call failed to start: {exc}", file=sys.stderr, flush=True)
            await self.end_call(reason=f"start_failed: {exc}", error=f"Voice call failed: {exc}")

    async def end_call(self, *, reason: str = "user_done", error: str | None = None) -> None:
        """End the active call and transition back to IDLE."""
        if self._call_state == "idle":
            return

        self._call_state = "ending"
        ended_call_id = self._call_id or new_id()
        duration = time.monotonic() - self._call_start_time if self._call_start_time > 0 else 0.0
        logger.info(
            "Ending call (call_id=%s, reason=%s, duration=%.2fs)...",
            ended_call_id,
            reason,
            duration,
        )

        # Cancel watchdog, screen watching, and consumer
        if self._silence_watchdog_task is not None:
            self._silence_watchdog_task.cancel()
            self._silence_watchdog_task = None

        if self._stall_watchdog_task is not None:
            self._stall_watchdog_task.cancel()
            self._stall_watchdog_task = None

        if self._watch_screen_task is not None:
            self._watch_screen_task.cancel()
            self._watch_screen_task = None

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

        # Reset call and voice state
        self._call_state = "idle"
        self._stall_timer_armed = False
        self._stall_accumulated_seconds = 0.0
        self._audio_sent_start_time = None
        if error is not None:
            self._set_voice_state("error", error=error)
        else:
            self._set_voice_state("idle")
        self._call_id = None
        if self._wake_word is not None:
            self._wake_word.reset()

        # Notify daemon if connected and we initiated the end (not already from daemon)
        if self._daemon_ws is not None and reason != "daemon_event":
            with contextlib.suppress(Exception):
                end_req = VoiceEndCallRequest(
                    params=VoiceEndCallParams(call_id=ended_call_id, reason=reason)
                )
                await self._daemon_ws.send(end_req.model_dump_json())

        # Safely invoke deferred transcript persistence and consolidation hooks
        await safe_store_voice_transcript(
            call_id=ended_call_id,
            entries=list(self._transcript_history),
        )
        await safe_consolidate_voice_call(call_id=ended_call_id)
        self._transcript_history.clear()

        # Write usage & metrics record
        self._write_metrics_record(
            call_id=ended_call_id,
            reason=reason,
            duration_seconds=duration,
        )

        # Emit typed CallEndedEvent
        self._emit_event(
            CallEndedEvent(
                data=CallEndedData(
                    call_id=ended_call_id,
                    call_state="idle",
                    reason=reason,
                    duration_seconds=round(duration, 3),
                )
            )
        )

        logger.info("Call %s ended. Runner is now IDLE.", ended_call_id)

    def _write_metrics_record(
        self,
        *,
        call_id: str,
        reason: str,
        duration_seconds: float,
    ) -> None:
        try:
            log_path = voice_metrics_path(self.data_dir)
            log_path.parent.mkdir(parents=True, exist_ok=True)
            record = {
                "timestamp": datetime.now(UTC).isoformat(),
                "call_id": call_id,
                "reason": reason,
                "duration_seconds": round(duration_seconds, 2),
                "latency": {
                    "connect_ms": round(self._connect_latency_ms, 1),
                    "first_audio_ms": round(self._first_audio_latency_ms, 1)
                    if self._first_audio_latency_ms is not None
                    else None,
                },
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

    async def _stall_watchdog(self) -> None:
        """Watch for audio streaming without server responses exceeding stall_timeout_seconds."""
        stall_limit = self.config.stall_timeout_seconds
        poll_interval = min(0.2, max(0.02, stall_limit / 4))
        while self._call_state == "active":
            await asyncio.sleep(poll_interval)
            if self._audio_manager and self._audio_manager.is_speaking:
                # Assistant is actively speaking; reset and disarm outbound stall timer
                self._stall_timer_armed = False
                self._stall_accumulated_seconds = 0.0
                self._audio_sent_start_time = None
                continue
            if self._stall_timer_armed and self._stall_accumulated_seconds >= stall_limit:
                handle = getattr(self._active_backend, "resumption_handle", None)
                elapsed = self._stall_accumulated_seconds
                if not self._stall_reconnected_once:
                    self._stall_reconnected_once = True
                    logger.error(
                        "STALL WATCHDOG: Audio streamed for %.1fs with zero server response! "
                        "Reconnecting live session with resumption handle '%s'...",
                        elapsed,
                        handle,
                    )
                    print(
                        f"\n[STALL WATCHDOG] Audio streamed for {elapsed:.1f}s with zero "
                        f"server response. Reconnecting live session with handle '{handle}'...",
                        file=sys.stderr,
                        flush=True,
                    )
                    self._stall_timer_armed = False
                    self._stall_accumulated_seconds = 0.0
                    self._audio_sent_start_time = None
                    self._set_voice_state("reconnecting")
                    reconnected = False
                    if self._active_backend is not None:
                        try:
                            rec_fn = getattr(self._active_backend, "reconnect", None) or getattr(
                                self._active_backend, "_reconnect", None
                            )
                            if callable(rec_fn):
                                res = rec_fn()
                                if asyncio.iscoroutine(res):
                                    await res
                                reconnected = True
                        except Exception as rec_err:
                            logger.error("Stall watchdog reconnection failed: %s", rec_err)

                    if reconnected:
                        self._stall_timer_armed = False
                        self._stall_accumulated_seconds = 0.0
                        self._audio_sent_start_time = None
                        self._set_voice_state("listening")
                    else:
                        await self.end_call(
                            reason="stall_timeout",
                            error="Session stalled: server not responding.",
                        )
                        break
                else:
                    logger.error(
                        "STALL WATCHDOG: Reconnection already attempted; server still not "
                        "responding after %.1fs. Ending call.",
                        elapsed,
                    )
                    print(
                        f"\n[STALL WATCHDOG] Server still not responding after reconnect "
                        f"({elapsed:.1f}s). Ending call.",
                        file=sys.stderr,
                        flush=True,
                    )
                    await self.end_call(
                        reason="stall_timeout",
                        error="Session stalled: server not responding after reconnect.",
                    )
                    break

    async def _consume_backend_events(self, backend: VoiceBackend) -> None:
        """Consume and route events from active voice backend."""
        try:
            async for event in backend.events():
                if self._call_state != "active":
                    break

                # Reset and disarm stall watchdog timer and reconnect state on any server response
                self._stall_timer_armed = False
                self._stall_accumulated_seconds = 0.0
                self._audio_sent_start_time = None
                self._stall_reconnected_once = False

                # Clear transient error or reconnecting state if backend recovered and returned data
                if (
                    self._state in ("error", "reconnecting")
                    and not isinstance(event, BackendErrorEvent)
                ):
                    self._set_voice_state("listening")

                if self.debug_audio:
                    evt_type = type(event).__name__
                    desc = ""
                    if isinstance(event, AudioOutputEvent):
                        desc = f"({len(event.pcm_bytes)}b audio)"
                    elif isinstance(event, TranscriptOutputEvent):
                        desc = f"({event.role}: '{event.text[:30]}')"
                    print(f"[AUDIO-DEBUG] RECV server message: {evt_type} {desc}", flush=True)

                if isinstance(event, AudioOutputEvent):
                    if self._first_audio_latency_ms is None:
                        self._first_audio_latency_ms = (
                            time.monotonic() - self._call_start_time
                        ) * 1000
                    self._set_voice_state("speaking")
                    if self._audio_manager is not None:
                        self._audio_manager.play_audio(event.pcm_bytes)
                elif isinstance(event, InterruptedEvent):
                    # Barge-in: flush output audio immediately
                    logger.debug("Barge-in: flushing audio buffer.")
                    if self._audio_manager is not None:
                        self._audio_manager.stop_output()
                    self._set_voice_state("listening")
                elif isinstance(event, TurnCompleteEvent):
                    self._set_voice_state("listening")
                elif isinstance(event, TranscriptOutputEvent):
                    if event.role == "user":
                        self._last_user_speech_time = time.monotonic()
                    self._transcript_history.append(
                        {
                            "role": event.role,
                            "text": event.text,
                            "final": event.final,
                            "timestamp": time.monotonic(),
                        }
                    )
                    self._emit_event(
                        TranscriptEvent(
                            data=TranscriptData(
                                call_id=self._call_id,
                                role=event.role,
                                text=event.text,
                                final=event.final,
                            )
                        )
                    )
                    logger.debug("[%s]: %s", event.role, event.text)
                elif isinstance(event, ToolCallEvent):
                    self._set_voice_state("thinking")
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
                    self._emit_event(
                        UsageEvent(
                            data=UsageData(
                                call_id=self._call_id,
                                input_tokens=self._usage_record["input_tokens"],
                                output_tokens=self._usage_record["output_tokens"],
                                total_tokens=self._usage_record["total_tokens"],
                                cached_tokens=self._usage_record["cached_tokens"] or None,
                                thought_tokens=self._usage_record["thought_tokens"] or None,
                            )
                        )
                    )
                elif isinstance(event, BackendErrorEvent):
                    logger.warning(
                        "Backend error: %s (recoverable=%s)", event.message, event.recoverable
                    )
                    print(f"\n[ERROR] Backend error: {event.message}", file=sys.stderr, flush=True)
                    if not event.recoverable:
                        await self.end_call(
                            reason=f"backend_error: {event.message}", error=event.message
                        )
                        break
                    else:
                        self._set_voice_state("reconnecting")
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.error("Error in backend event consumer: %s", exc)
            print(f"\n[ERROR] Backend event consumer error: {exc}", file=sys.stderr, flush=True)
            await self.end_call(
                reason=f"consumer_error: {exc}", error=f"Voice stream failed: {exc}"
            )

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

        # 3. look_at_screen tool
        if event.name == "look_at_screen":
            if self.config.consent_mode == "deny":
                resp = VoiceToolResponse(
                    call_id=event.call_id,
                    name=event.name,
                    response={
                        "status": "denied",
                        "error": "Screen capture is disabled by configuration (consent_mode=deny).",
                    },
                    is_error=True,
                    scheduling="when_idle",
                )
                await backend.send_tool_responses([resp])
                return

            target = str(event.arguments.get("target", "full_screen"))
            monitor = event.arguments.get("monitor")
            res = self._screen_service.capture(target=target, monitor=monitor)

            if res.blocked:
                resp = VoiceToolResponse(
                    call_id=event.call_id,
                    name=event.name,
                    response={
                        "status": "blocked",
                        "error": (
                            f"Active window '{res.window_title}' ({res.process_name}) "
                            "is protected by privacy blocklist."
                        ),
                    },
                    is_error=True,
                    scheduling="when_idle",
                )
                await backend.send_tool_responses([resp])
                return

            if res.image_bytes is not None:
                await backend.send_video_frame(res.image_bytes, mime_type="image/jpeg")
                resp = VoiceToolResponse(
                    call_id=event.call_id,
                    name=event.name,
                    response={
                        "status": "success",
                        "target": target,
                        "width": res.width,
                        "height": res.height,
                        "window_title": res.window_title,
                    },
                    scheduling="when_idle",
                )
            else:
                resp = VoiceToolResponse(
                    call_id=event.call_id,
                    name=event.name,
                    response={
                        "status": "error",
                        "error": res.error or "Screen capture failed to produce an image.",
                    },
                    is_error=True,
                    scheduling="when_idle",
                )
            await backend.send_tool_responses([resp])
            return

        # 4. watch_screen tool
        if event.name == "watch_screen":
            if self.config.consent_mode == "deny":
                resp = VoiceToolResponse(
                    call_id=event.call_id,
                    name=event.name,
                    response={
                        "status": "denied",
                        "error": "Continuous screen capture is disabled by configuration.",
                    },
                    is_error=True,
                    scheduling="when_idle",
                )
                await backend.send_tool_responses([resp])
                return

            try:
                req_seconds = float(event.arguments.get("seconds", 10.0))
            except (ValueError, TypeError):
                req_seconds = 10.0

            burst_seconds = max(1.0, min(req_seconds, self.config.max_burst_seconds))
            reason = str(event.arguments.get("reason", "Continuous workflow guidance"))

            if self._watch_screen_task is not None and not self._watch_screen_task.done():
                self._watch_screen_task.cancel()

            self._watch_screen_task = asyncio.create_task(
                self._stream_screen_burst(burst_seconds=burst_seconds, backend=backend),
                name="VoiceRunnerScreenBurst",
            )
            resp = VoiceToolResponse(
                call_id=event.call_id,
                name=event.name,
                response={
                    "status": "watching",
                    "duration_seconds": burst_seconds,
                    "reason": reason,
                },
                scheduling="when_idle",
            )
            await backend.send_tool_responses([resp])
            return

    async def _stream_screen_burst(
        self,
        *,
        burst_seconds: float,
        backend: VoiceBackend,
        interval: float = 1.0,
    ) -> None:
        """Stream periodic screen frames for a bounded burst duration."""
        t_end = time.monotonic() + burst_seconds
        logger.info("Starting screen watch burst for %.1f seconds.", burst_seconds)
        try:
            while time.monotonic() < t_end and self._call_state == "active":
                res = self._screen_service.capture(target="full_screen")
                if res.image_bytes is not None and not res.blocked:
                    await backend.send_video_frame(res.image_bytes, mime_type="image/jpeg")
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("Error during screen stream burst: %s", exc)
        finally:
            logger.debug("Screen watch burst ended.")

    async def _connect_daemon_and_listen(self) -> None:
        """Connect to Clea daemon and process incoming daemon requests/events."""
        token = self.token or auth.read_token(auth.token_path(self.data_dir))
        if not token:
            logger.debug("No daemon token found; running offline until daemon starts.")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._daemon_retry_delay)
            return

        try:
            async with connect(
                self.daemon_url,
                subprotocols=auth.client_subprotocols(token),  # type: ignore[arg-type]
            ) as ws:
                self._daemon_ws = ws
                self._daemon_ever_connected = True
                logger.info("Connected to daemon at %s.", self.daemon_url)
                # Notify daemon that voice process is connected and register state
                await ws.send(
                    VoiceStateEvent(data=VoiceStateData(state=self._state)).model_dump_json()
                )

                while not self._stop_event.is_set():
                    recv_task = asyncio.create_task(ws.recv())
                    stop_task = asyncio.create_task(self._stop_event.wait())
                    done, pending = await asyncio.wait(
                        {recv_task, stop_task},
                        return_when=asyncio.FIRST_COMPLETED,
                    )
                    for task in pending:
                        task.cancel()
                        with contextlib.suppress(asyncio.CancelledError, Exception):
                            await task
                    if recv_task in done:
                        if self._stop_event.is_set():
                            with contextlib.suppress(Exception):
                                recv_task.result()
                            break
                        raw = recv_task.result()
                    else:
                        break
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
        finally:
            self._daemon_ws = None

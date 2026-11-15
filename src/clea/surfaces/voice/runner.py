"""Voice surface runner managing the call lifecycle, audio I/O, wake word, and backend."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import logging.handlers
import sys
import time
from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import numpy as np
import sounddevice as sd
from websockets.asyncio.client import ClientConnection, connect
from websockets.exceptions import InvalidURI, WebSocketException

from clea.core import auth
from clea.core.protocol import (
    ActivityCancelParams,
    ActivityCancelRequest,
    ActivityListParams,
    ActivityListRequest,
    BaseEvent,
    BaseRequest,
    CallEndedData,
    CallEndedEvent,
    CallStartedData,
    CallStartedEvent,
    ChatCreateAndOpenParams,
    ChatCreateAndOpenRequest,
    ChatDeleteParams,
    ChatDeleteRequest,
    ChatFocusParams,
    ChatFocusRequest,
    ChatRenameParams,
    ChatRenameRequest,
    ChatSendFromVoiceParams,
    ChatSendFromVoiceRequest,
    MemoryAddFactParams,
    MemoryAddFactRequest,
    MemoryDeleteFactParams,
    MemoryDeleteFactRequest,
    OsClickUiElementParams,
    OsClickUiElementRequest,
    OsControlMediaVolumeParams,
    OsControlMediaVolumeRequest,
    OsControlSystemParams,
    OsControlSystemRequest,
    OsGuiActionParams,
    OsGuiActionRequest,
    OsInspectUiElementsParams,
    OsInspectUiElementsRequest,
    OsOpenOrFocusAppParams,
    OsOpenOrFocusAppRequest,
    OsOpenUrlParams,
    OsOpenUrlRequest,
    OsReadClipboardParams,
    OsReadClipboardRequest,
    OsSelectClipboardItemParams,
    OsSelectClipboardItemRequest,
    OsWriteClipboardParams,
    OsWriteClipboardRequest,
    ResponseFrame,
    TranscriptData,
    TranscriptEvent,
    TranscriptsGetParams,
    TranscriptsGetRequest,
    UiNavigateParams,
    UiNavigateRequest,
    UsageData,
    UsageEvent,
    VoiceDelegateTaskParams,
    VoiceDelegateTaskRequest,
    VoiceEndCallParams,
    VoiceEndCallRequest,
    VoiceListConversationsParams,
    VoiceListConversationsRequest,
    VoicePostToActiveChatParams,
    VoicePostToActiveChatRequest,
    VoiceReadConversationParams,
    VoiceReadConversationRequest,
    VoiceSearchConversationsParams,
    VoiceSearchConversationsRequest,
    VoiceSearchMemoryParams,
    VoiceSearchMemoryRequest,
    VoiceStartCallParams,
    VoiceStartCallRequest,
    VoiceState,
    VoiceStateData,
    VoiceStateEvent,
    WorkspaceGitInspectParams,
    WorkspaceGitInspectRequest,
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
    VoiceToolMediaPart,
    VoiceToolResponse,
)
from clea.surfaces.voice.backends.gemini_live import GeminiLiveBackend
from clea.surfaces.voice.config import VoiceConfig, load_voice_config
from clea.surfaces.voice.lock import VoiceInstanceLock
from clea.surfaces.voice.memory_hooks import (
    _VOICE_COMPANION_SESSION_ID,
    _daemon_rpc,
    extract_focused_chat_from_context,
    normalize_voice_transcript,
    resolve_voice_system_instruction,
    safe_consolidate_voice_call,
    safe_store_voice_transcript,
)
from clea.surfaces.voice.screen import ScreenCaptureService, is_window_blocked
from clea.surfaces.voice.screen_indicator import ScreenIndicator
from clea.surfaces.voice.wake_word import WakeWordDetector, WakeWordModelNotFoundError
from clea.surfaces.voice.win32 import (
    HotKeyManager,
    enable_per_monitor_dpi_awareness,
    get_target_monitor,
    resolve_app_display_name,
)

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
        screen_indicator: ScreenIndicator | None = None,
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
        self._screen_indicator = screen_indicator or ScreenIndicator(
            enabled=self.config.screen_indicator_enabled,
            height_px=self.config.screen_indicator_height_px,
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
        self._daemon_listener_active: bool = False
        self._daemon_task_refs: set[asyncio.Task[Any]] = set()
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

    def _spawn_daemon_callback_task(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        name: str,
    ) -> asyncio.Task[Any]:
        """Spawn a daemon-triggered lifecycle task with a strong reference."""
        task = asyncio.create_task(coro, name=name)
        self._daemon_task_refs.add(task)

        def _done(done_task: asyncio.Task[Any]) -> None:
            self._daemon_task_refs.discard(done_task)
            if not done_task.cancelled():
                exc = done_task.exception()
                if exc is not None:
                    logger.warning("Daemon callback task %s failed: %s", name, exc)

        task.add_done_callback(_done)
        return task

    async def _request_daemon(
        self,
        req: BaseRequest,
        *,
        timeout: float = 5.0,
    ) -> dict[str, Any]:
        """Send an RPC request over the active daemon listener connection."""
        ws = self._daemon_ws
        if ws is None or not self._daemon_listener_active:
            raise RuntimeError("No active daemon listener connection.")
        loop = asyncio.get_running_loop()
        fut: asyncio.Future[dict[str, Any]] = loop.create_future()
        self._pending_tasks[req.id] = fut
        try:
            await ws.send(req.model_dump_json())
            return dict(await asyncio.wait_for(fut, timeout=timeout) or {})
        finally:
            self._pending_tasks.pop(req.id, None)

    async def _rpc(self, req: BaseRequest, *, timeout: float = 10.0) -> dict[str, Any]:
        """Run a typed daemon RPC over the active listener socket or transient fallback."""
        if self._daemon_ws is not None:
            return await self._request_daemon(req, timeout=timeout)
        return await _daemon_rpc(
            req,
            daemon_url=self.daemon_url,
            token=self.token,
            data_dir=self.data_dir,
            timeout=timeout,
        )

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

            # 4. Initialize top-edge screen-monitoring overlay thread (if enabled & supported)
            try:
                self._screen_indicator.start()
            except Exception as exc:
                logger.warning("Could not start screen monitoring indicator: %s", exc)

            logger.info("VoiceRunner started in state: IDLE (hotkey=%s).", self.config.hotkey)
        except Exception:
            with contextlib.suppress(Exception):
                self._screen_indicator.stop()
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

        for task in list(self._daemon_task_refs):
            if not task.done():
                task.cancel()
        if self._daemon_task_refs:
            await asyncio.gather(*list(self._daemon_task_refs), return_exceptions=True)
            self._daemon_task_refs.clear()

        with contextlib.suppress(Exception):
            self._screen_indicator.stop()

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
            memory_context: str | None = None
            if self._daemon_ws is not None and trigger != "daemon_event":
                start_req = VoiceStartCallRequest(
                    params=VoiceStartCallParams(trigger=trigger)  # type: ignore[arg-type]
                )
                try:
                    if self._daemon_listener_active:
                        res = await self._request_daemon(start_req)
                        if isinstance(res.get("call_id"), str) and res["call_id"]:
                            self._call_id = res["call_id"]
                        if isinstance(res.get("memory_context"), str):
                            memory_context = res["memory_context"]
                    else:
                        await self._daemon_ws.send(start_req.model_dump_json())
                except (OSError, WebSocketException, TimeoutError, RuntimeError) as exc:
                    logger.warning("Could not notify daemon of call start: %s", exc)

            # 2. Resolve deterministic UI focus state for call startup context
            parsed_id, parsed_title = extract_focused_chat_from_context(memory_context)
            focused_chat_id = parsed_id
            focused_chat_title = parsed_title
            focused_chat_available = bool(parsed_id)
            if self._daemon_ws is not None and self._daemon_listener_active:
                try:
                    focus_res = await self._request_daemon(
                        ChatFocusRequest(params=ChatFocusParams())
                    )
                    raw_focus_id = focus_res.get("active_conversation_id")
                    if (
                        isinstance(raw_focus_id, str)
                        and raw_focus_id.strip()
                        and raw_focus_id.strip() != _VOICE_COMPANION_SESSION_ID
                    ):
                        focused_chat_id = " ".join(raw_focus_id.split())
                        focused_chat_available = True
                        focused_chat_title = parsed_title if parsed_id == focused_chat_id else None
                    else:
                        focused_chat_id = None
                        focused_chat_title = None
                        focused_chat_available = False
                except (OSError, WebSocketException, TimeoutError, RuntimeError) as exc:
                    logger.warning("Could not query focused chat state: %s", exc)

                if focused_chat_available and focused_chat_id and not focused_chat_title:
                    try:
                        tr_res = await self._request_daemon(
                            TranscriptsGetRequest(
                                params=TranscriptsGetParams(session_id=focused_chat_id)
                            )
                        )
                        raw_title = tr_res.get("title")
                        if isinstance(raw_title, str) and raw_title.strip():
                            focused_chat_title = " ".join(raw_title.split())
                    except (OSError, WebSocketException, TimeoutError, RuntimeError) as exc:
                        logger.debug(
                            "Could not fetch focused chat title for %s: %s",
                            focused_chat_id,
                            exc,
                        )

            # 3. Resolve system instruction safely
            request_fn = self._request_daemon if self._daemon_listener_active else None
            system_instruction = await resolve_voice_system_instruction(
                memory_context=memory_context,
                active_conversation_id=focused_chat_id,
                focused_chat_available=focused_chat_available,
                focused_chat_id=focused_chat_id,
                focused_chat_title=focused_chat_title,
                daemon_url=self.daemon_url,
                token=self.token,
                data_dir=self.data_dir,
                request_fn=request_fn,
            )

            # 3. Create backend and connect with latency measurement
            session_cfg = VoiceSessionConfig(
                system_instruction=system_instruction,
                enable_web_search=self.config.enable_web_search,
                media_resolution=self.config.media_resolution,
                reconnect_limit=self.config.reconnect_limit,
                session_resumption=SessionResumptionConfig(enabled=True),
                transcription_language=self.config.transcription_language,
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

    async def end_call(
        self,
        *,
        reason: str = "user_done",
        error: str | None = None,
        from_daemon: bool = False,
    ) -> None:
        """End the active call and transition back to IDLE."""
        if self._call_state in ("idle", "ending"):
            return

        self._call_state = "ending"
        ended_call_id = self._call_id or new_id()
        transcript_snapshot = list(self._transcript_history)
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
            watch_task = self._watch_screen_task
            self._watch_screen_task = None
            watch_task.cancel()
            if watch_task is not asyncio.current_task():
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await watch_task

        with contextlib.suppress(Exception):
            self._screen_indicator.hide()

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
            consume_task = self._backend_consume_task
            self._backend_consume_task = None
            if consume_task is not asyncio.current_task():
                consume_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await consume_task

        # Reset call and voice state immediately so hooks cannot block or corrupt IDLE/new calls
        self._call_state = "idle"
        self._stall_timer_armed = False
        self._stall_accumulated_seconds = 0.0
        self._audio_sent_start_time = None
        if error is not None:
            self._set_voice_state("error", error=error)
        else:
            self._set_voice_state("idle")
        self._call_id = None
        self._transcript_history.clear()
        if self._wake_word is not None:
            self._wake_word.reset()

        # Write usage & metrics record
        self._write_metrics_record(
            call_id=ended_call_id,
            reason=reason,
            duration_seconds=duration,
        )

        # Emit typed CallEndedEvent immediately on transition to IDLE
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

        # Notify daemon if connected and we initiated the end (not already from daemon)
        if self._daemon_ws is not None and not from_daemon and reason != "daemon_event":
            with contextlib.suppress(Exception):
                end_req = VoiceEndCallRequest(
                    params=VoiceEndCallParams(call_id=ended_call_id, reason=reason)
                )
                if self._daemon_listener_active:
                    await self._request_daemon(end_req, timeout=2.0)
                else:
                    await self._daemon_ws.send(end_req.model_dump_json())

        # Safely persist non-empty transcript and trigger asynchronous call-end consolidation
        request_fn = self._request_daemon if self._daemon_listener_active else None
        await safe_store_voice_transcript(
            call_id=ended_call_id,
            entries=transcript_snapshot,
            daemon_url=self.daemon_url,
            token=self.token,
            data_dir=self.data_dir,
            request_fn=request_fn,
        )
        if normalize_voice_transcript(transcript_snapshot):
            await safe_consolidate_voice_call(
                call_id=ended_call_id,
                entries=transcript_snapshot,
                daemon_url=self.daemon_url,
                token=self.token,
                data_dir=self.data_dir,
                request_fn=request_fn,
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
                if self._state in ("error", "reconnecting") and not isinstance(
                    event, BackendErrorEvent
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
                    if self._transcript_history:
                        self._transcript_history[-1]["final"] = True
                    self._set_voice_state("listening")
                elif isinstance(event, TranscriptOutputEvent):
                    if event.role == "user":
                        self._last_user_speech_time = time.monotonic()
                    ts_iso = datetime.now().astimezone().isoformat()
                    self._transcript_history.append(
                        {
                            "role": event.role,
                            "text": event.text,
                            "final": event.final,
                            "timestamp": ts_iso,
                        }
                    )
                    self._emit_event(
                        TranscriptEvent(
                            data=TranscriptData(
                                call_id=self._call_id,
                                role=event.role,
                                text=event.text,
                                final=event.final,
                                timestamp=ts_iso,
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
            if self._call_state == "active":
                logger.warning(
                    "Backend event stream ended while call %s was active.", self._call_id
                )
                await self.end_call(
                    reason="backend_stream_ended",
                    error="Voice stream ended unexpectedly.",
                )
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
            if target not in ("active_window", "full_screen"):
                target = "full_screen"

            raw_monitor = event.arguments.get("monitor")
            monitor: int | None = None
            if raw_monitor is not None and not isinstance(raw_monitor, bool):
                with contextlib.suppress(TypeError, ValueError):
                    monitor = int(raw_monitor)

            initial_bounds: tuple[int, int, int, int] | None = None
            has_api = getattr(self._screen_service, "_has_injected_api", False)
            if (
                sys.platform == "win32" and getattr(self._screen_service, "_grabber", None) is None
            ) or has_api:
                with contextlib.suppress(Exception):
                    mon = get_target_monitor(
                        monitor=monitor,
                        api=getattr(self._screen_service, "_api", None),
                    )
                    if mon is not None:
                        initial_bounds = mon.bounds

            # Show red bar when capture begins (excluded from capture via WDA_EXCLUDEFROMCAPTURE).
            if not self._screen_indicator.is_watching:
                self._screen_indicator.pulse(initial_bounds, duration_seconds=1.5)

            res = await asyncio.to_thread(
                self._screen_service.capture,
                target=target,
                monitor=monitor,
            )

            res_blocked = res.blocked or is_window_blocked(
                res.window_title or "",
                res.process_name or "",
                self.config.blocklist,
            )
            if res_blocked:
                if not self._screen_indicator.is_watching:
                    self._screen_indicator.hide()
                resp = VoiceToolResponse(
                    call_id=event.call_id,
                    name=event.name,
                    response={
                        "status": "blocked",
                        "error": (
                            f"Active window '{res.window_title}' ({res.process_name}) "
                            "is protected by privacy blocklist."
                        ),
                        "active_window_title": res.window_title,
                        "active_process_name": res.process_name,
                        "active_app_name": res.app_name,
                    },
                    is_error=True,
                    scheduling="when_idle",
                )
                await backend.send_tool_responses([resp])
                return

            if res.image_bytes:
                if (
                    res.monitor_bounds is not None
                    and res.monitor_bounds != initial_bounds
                    and not self._screen_indicator.is_watching
                ):
                    self._screen_indicator.pulse(res.monitor_bounds, duration_seconds=1.5)

                media_parts: list[VoiceToolMediaPart] = [
                    VoiceToolMediaPart(
                        data=res.image_bytes,
                        mime_type=res.mime_type,
                    )
                ]
                image_order: list[str] = [target]
                active_res = None

                # When full-screen context is requested alongside an active window on the same
                # display, also attach a 1:1 native-resolution active-window crop atomically so
                # small text and UI elements remain sharp on the exact same turn.
                if target == "full_screen" and res.window_bounds is not None:
                    wl, wt, wr, wb = res.window_bounds
                    if wr > wl and wb > wt:
                        active_overlaps_target = True
                        if res.monitor_bounds is not None:
                            ml, mt, mr, mb = res.monitor_bounds
                            active_overlaps_target = min(mr, wr) > max(ml, wl) and min(
                                mb, wb
                            ) > max(mt, wt)
                        if active_overlaps_target:
                            candidate = await asyncio.to_thread(
                                self._screen_service.capture,
                                target="active_window",
                            )
                            candidate_blocked = candidate.blocked or is_window_blocked(
                                candidate.window_title or "",
                                candidate.process_name or "",
                                self.config.blocklist,
                            )
                            same_monitor = (
                                res.monitor_index is None
                                or candidate.monitor_index is None
                                or candidate.monitor_index == res.monitor_index
                            )
                            same_window = (
                                (candidate.process_name, candidate.window_title)
                                == (res.process_name, res.window_title)
                            )
                            if (
                                not candidate_blocked
                                and candidate.image_bytes
                                and same_monitor
                                and same_window
                            ):
                                active_res = candidate
                                media_parts.append(
                                    VoiceToolMediaPart(
                                        data=active_res.image_bytes,
                                        mime_type=active_res.mime_type,
                                    )
                                )
                                image_order.append("active_window")

                payload: dict[str, Any] = {
                    "status": "success",
                    "target": target,
                    "frames_sent": len(media_parts),
                    "image_order": image_order,
                    "mime_type": res.mime_type,
                    "width": res.width,
                    "height": res.height,
                    "capture_seq": res.capture_seq,
                    "captured_at": res.captured_at,
                    "window_title": res.window_title,
                    "active_window_title": res.window_title,
                    "active_process_name": res.process_name,
                    "active_app_name": res.app_name,
                    "window_bounds": list(res.window_bounds) if res.window_bounds else None,
                    "monitor_index": res.monitor_index,
                    "monitor_bounds": list(res.monitor_bounds) if res.monitor_bounds else None,
                }
                if active_res is not None:
                    payload["active_window_mime_type"] = active_res.mime_type
                    payload["active_window_width"] = active_res.width
                    payload["active_window_height"] = active_res.height

                resp = VoiceToolResponse(
                    call_id=event.call_id,
                    name=event.name,
                    response=payload,
                    media_parts=media_parts,
                    scheduling="when_idle",
                )
            else:
                if not self._screen_indicator.is_watching:
                    self._screen_indicator.hide()
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
            target = str(event.arguments.get("target", "full_screen"))
            if target not in ("active_window", "full_screen"):
                target = "full_screen"

            if self._watch_screen_task is not None and not self._watch_screen_task.done():
                prev_task = self._watch_screen_task
                prev_task.cancel()
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await prev_task
                if self._watch_screen_task is prev_task:
                    self._watch_screen_task = None
                if self._screen_indicator.is_watching:
                    self._screen_indicator.stop_watching()

            initial_bounds: tuple[int, int, int, int] | None = None
            win_info = self._screen_service.get_active_window_info()
            service_blocklist = getattr(self._screen_service, "blocklist", None)
            if win_info is not None and (
                is_window_blocked(
                    win_info.title or "", win_info.process_name or "", self.config.blocklist
                )
                or (
                    service_blocklist is not None
                    and is_window_blocked(
                        win_info.title or "", win_info.process_name or "", service_blocklist
                    )
                )
            ):
                self._screen_indicator.stop_watching()
                resp = VoiceToolResponse(
                    call_id=event.call_id,
                    name=event.name,
                    response={
                        "status": "blocked",
                        "error": "Active window is protected by privacy blocklist.",
                    },
                    is_error=True,
                    scheduling="when_idle",
                )
                await backend.send_tool_responses([resp])
                return

            if win_info is not None and win_info.monitor_bounds is not None:
                initial_bounds = win_info.monitor_bounds
            elif (
                sys.platform == "win32" and getattr(self._screen_service, "_grabber", None) is None
            ) or getattr(self._screen_service, "_has_injected_api", False):
                with contextlib.suppress(Exception):
                    mon = get_target_monitor(api=getattr(self._screen_service, "_api", None))
                    if mon is not None:
                        initial_bounds = mon.bounds

            self._screen_indicator.start_watching(initial_bounds)

            self._watch_screen_task = asyncio.create_task(
                self._stream_screen_burst(
                    burst_seconds=burst_seconds,
                    backend=backend,
                    target=target,
                ),
                name="VoiceRunnerScreenBurst",
            )
            resp = VoiceToolResponse(
                call_id=event.call_id,
                name=event.name,
                response={
                    "status": "watching",
                    "duration_seconds": burst_seconds,
                    "reason": reason,
                    "target": target,
                    "active_window_title": win_info.title if win_info else None,
                    "active_process_name": win_info.process_name if win_info else None,
                    "active_app_name": (
                        resolve_app_display_name(win_info.process_name, win_info.title)
                        if win_info
                        else None
                    ),
                    "monitor_index": win_info.monitor_index if win_info else None,
                },
                scheduling="when_idle",
            )
            await backend.send_tool_responses([resp])
            return

        # 5. list_conversations tool
        if event.name == "list_conversations":
            raw_limit = event.arguments.get("limit")
            limit = 10
            if raw_limit is not None and not isinstance(raw_limit, bool):
                with contextlib.suppress(TypeError, ValueError):
                    limit = max(1, min(int(raw_limit), 50))
            try:
                res = await self._rpc(
                    VoiceListConversationsRequest(
                        params=VoiceListConversationsParams(limit=limit)
                    ),
                    timeout=10.0,
                )
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
                    response={"error": f"Failed to list conversations: {exc}"},
                    is_error=True,
                    scheduling="when_idle",
                )
            await backend.send_tool_responses([tool_resp])
            return

        # 5b. search_conversations tool
        if event.name == "search_conversations":
            query = str(event.arguments.get("query", "")).strip()
            raw_limit = event.arguments.get("limit")
            limit = 8
            if raw_limit is not None and not isinstance(raw_limit, bool):
                with contextlib.suppress(TypeError, ValueError):
                    limit = max(1, min(int(raw_limit), 50))
            if not query:
                await backend.send_tool_responses(
                    [
                        VoiceToolResponse(
                            call_id=event.call_id,
                            name=event.name,
                            response={"error": "Search query must not be empty."},
                            is_error=True,
                            scheduling="when_idle",
                        )
                    ]
                )
                return
            try:
                res = await self._rpc(
                    VoiceSearchConversationsRequest(
                        params=VoiceSearchConversationsParams(query=query, limit=limit)
                    ),
                    timeout=10.0,
                )
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
                    response={"error": f"Failed to search conversations: {exc}"},
                    is_error=True,
                    scheduling="when_idle",
                )
            await backend.send_tool_responses([tool_resp])
            return

        # 5c. read_conversation tool
        if event.name == "read_conversation":
            conversation_id = str(event.arguments.get("conversation_id", "")).strip()
            raw_max = event.arguments.get("max_turns")
            max_turns = 40
            if raw_max is not None and not isinstance(raw_max, bool):
                with contextlib.suppress(TypeError, ValueError):
                    max_turns = max(1, min(int(raw_max), 200))
            if not conversation_id:
                await backend.send_tool_responses(
                    [
                        VoiceToolResponse(
                            call_id=event.call_id,
                            name=event.name,
                            response={"error": "conversation_id must not be empty."},
                            is_error=True,
                            scheduling="when_idle",
                        )
                    ]
                )
                return
            try:
                res = await self._rpc(
                    VoiceReadConversationRequest(
                        params=VoiceReadConversationParams(
                            conversation_id=conversation_id,
                            max_turns=max_turns,
                        )
                    ),
                    timeout=10.0,
                )
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
                    response={"error": f"Failed to read conversation: {exc}"},
                    is_error=True,
                    scheduling="when_idle",
                )
            await backend.send_tool_responses([tool_resp])
            return

        # 5d. search_memory tool
        if event.name == "search_memory":
            query = str(event.arguments.get("query", "")).strip()
            raw_limit = event.arguments.get("limit")
            limit = 10
            if raw_limit is not None and not isinstance(raw_limit, bool):
                with contextlib.suppress(TypeError, ValueError):
                    limit = max(1, min(int(raw_limit), 50))
            if not query:
                await backend.send_tool_responses(
                    [
                        VoiceToolResponse(
                            call_id=event.call_id,
                            name=event.name,
                            response={"error": "Search query must not be empty."},
                            is_error=True,
                            scheduling="when_idle",
                        )
                    ]
                )
                return
            try:
                res = await self._rpc(
                    VoiceSearchMemoryRequest(
                        params=VoiceSearchMemoryParams(query=query, limit=limit)
                    ),
                    timeout=10.0,
                )
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
                    response={"error": f"Failed to search memory: {exc}"},
                    is_error=True,
                    scheduling="when_idle",
                )
            await backend.send_tool_responses([tool_resp])
            return

        async def _send_error(message: str) -> None:
            await backend.send_tool_responses(
                [
                    VoiceToolResponse(
                        call_id=event.call_id,
                        name=event.name,
                        response={"error": message},
                        is_error=True,
                        scheduling="when_idle",
                    )
                ]
            )

        async def _execute_rpc_tool(req: BaseRequest, *, error_prefix: str) -> None:
            try:
                res = await self._rpc(req, timeout=10.0)
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
                    response={"error": f"{error_prefix}: {exc}"},
                    is_error=True,
                    scheduling="when_idle",
                )
            await backend.send_tool_responses([tool_resp])

        def _req_str(key: str) -> str | None:
            val = event.arguments.get(key)
            if isinstance(val, str):
                stripped = val.strip()
                return stripped or None
            return None

        def _parse_bool(key: str, *, default: bool) -> bool:
            val = event.arguments.get(key)
            if isinstance(val, bool):
                return val
            if isinstance(val, str):
                low = val.strip().lower()
                if low in {"true", "1", "yes"}:
                    return True
                if low in {"false", "0", "no"}:
                    return False
            return default

        # 6. post_to_active_chat tool
        if event.name == "post_to_active_chat":
            raw_content = event.arguments.get("content")
            content = "" if raw_content is None else str(raw_content).strip()
            if not content:
                await _send_error("Content must not be empty.")
                return
            await _execute_rpc_tool(
                VoicePostToActiveChatRequest(
                    params=VoicePostToActiveChatParams(
                        content=content,
                        label=_req_str("label"),
                    )
                ),
                error_prefix="Failed to post to active chat",
            )
            return

        # 7. Clea Web UI self-control tools
        if event.name == "navigate_ui":
            raw_view = (_req_str("view") or "").lower()
            valid_views = ("chat", "voice", "activity", "memory", "settings")
            if raw_view not in valid_views:
                await _send_error(f"view must be one of: {', '.join(valid_views)}.")
                return
            await _execute_rpc_tool(
                UiNavigateRequest(
                    params=UiNavigateParams(
                        view=raw_view,  # type: ignore[arg-type]
                        conversation_id=_req_str("conversation_id"),
                    )
                ),
                error_prefix="Failed to navigate UI",
            )
            return

        if event.name == "create_chat":
            title = _req_str("title")
            if not title:
                await _send_error("title must not be empty.")
                return
            raw_content = event.arguments.get("content")
            content = raw_content.strip() if isinstance(raw_content, str) else ""
            await _execute_rpc_tool(
                ChatCreateAndOpenRequest(
                    params=ChatCreateAndOpenParams(
                        title=title,
                        content=content,
                        open_in_ui=_parse_bool("open_in_ui", default=True),
                    )
                ),
                error_prefix="Failed to create chat",
            )
            return

        if event.name == "send_chat_message":
            prompt = _req_str("prompt")
            if not prompt:
                await _send_error("prompt must not be empty.")
                return
            await _execute_rpc_tool(
                ChatSendFromVoiceRequest(
                    params=ChatSendFromVoiceParams(
                        prompt=prompt,
                        conversation_id=_req_str("conversation_id"),
                        title=_req_str("title"),
                    )
                ),
                error_prefix="Failed to send chat message",
            )
            return

        if event.name == "rename_chat":
            conversation_id = _req_str("conversation_id")
            new_title = _req_str("new_title")
            if not conversation_id or not new_title:
                await _send_error("conversation_id and new_title must not be empty.")
                return
            await _execute_rpc_tool(
                ChatRenameRequest(
                    params=ChatRenameParams(
                        conversation_id=conversation_id,
                        new_title=new_title,
                    )
                ),
                error_prefix="Failed to rename chat",
            )
            return

        if event.name == "delete_chat":
            conversation_id = _req_str("conversation_id")
            if not conversation_id:
                await _send_error("conversation_id must not be empty.")
                return
            await _execute_rpc_tool(
                ChatDeleteRequest(
                    params=ChatDeleteParams(
                        conversation_id=conversation_id,
                        confirmed=_parse_bool("confirmed", default=False),
                    )
                ),
                error_prefix="Failed to delete chat",
            )
            return

        if event.name == "save_memory_fact":
            fact = _req_str("fact")
            if not fact:
                await _send_error("fact must not be empty.")
                return
            await _execute_rpc_tool(
                MemoryAddFactRequest(
                    params=MemoryAddFactParams(
                        fact=fact,
                        category=_req_str("category"),
                    )
                ),
                error_prefix="Failed to save memory fact",
            )
            return

        if event.name == "delete_memory_fact":
            query_or_id = _req_str("query_or_id")
            if not query_or_id:
                await _send_error("query_or_id must not be empty.")
                return
            await _execute_rpc_tool(
                MemoryDeleteFactRequest(
                    params=MemoryDeleteFactParams(query_or_id=query_or_id)
                ),
                error_prefix="Failed to delete memory fact",
            )
            return

        if event.name == "inspect_activity":
            raw_limit = event.arguments.get("limit", 10)
            try:
                limit = 10 if raw_limit is None or isinstance(raw_limit, bool) else int(raw_limit)
            except (TypeError, ValueError):
                await _send_error("limit must be an integer between 1 and 100.")
                return
            limit = max(1, min(100, limit))
            await _execute_rpc_tool(
                ActivityListRequest(params=ActivityListParams(limit=limit)),
                error_prefix="Failed to inspect activity",
            )
            return

        if event.name == "cancel_task":
            task_id = _req_str("task_id")
            if not task_id:
                await _send_error("task_id must not be empty.")
                return
            await _execute_rpc_tool(
                ActivityCancelRequest(params=ActivityCancelParams(task_id=task_id)),
                error_prefix="Failed to cancel task",
            )
            return

        if event.name == "open_command_palette":
            await _execute_rpc_tool(
                UiNavigateRequest(params=UiNavigateParams(view="chat", open_command_palette=True)),
                error_prefix="Failed to open command palette",
            )
            return

        # 8. Windows OS & read-only Git workspace tools
        if event.name == "open_or_focus_app":
            app_name = _req_str("app_name")
            if not app_name:
                await _send_error("app_name must not be empty.")
                return
            await _execute_rpc_tool(
                OsOpenOrFocusAppRequest(
                    params=OsOpenOrFocusAppParams(
                        app_name=app_name,
                        target_path=_req_str("target_path"),
                    )
                ),
                error_prefix="Failed to open or focus app",
            )
            return

        if event.name == "control_media_volume":
            action = (_req_str("action") or "").lower()
            valid_actions = (
                "mute",
                "unmute",
                "toggle_mute",
                "volume_up",
                "volume_down",
                "set_volume",
                "play_pause",
                "next_track",
                "previous_track",
            )
            if action not in valid_actions:
                await _send_error("action must be one of: " + ", ".join(valid_actions) + ".")
                return
            raw_level = event.arguments.get("level")
            level: int | None = None
            if raw_level is not None:
                if isinstance(raw_level, bool):
                    await _send_error("level must be an integer between 0 and 100.")
                    return
                try:
                    level = max(0, min(100, int(raw_level)))
                except (TypeError, ValueError):
                    await _send_error("level must be an integer between 0 and 100.")
                    return
            if action == "set_volume" and level is None:
                await _send_error("level (0..100) is required when action is 'set_volume'.")
                return
            await _execute_rpc_tool(
                OsControlMediaVolumeRequest(
                    params=OsControlMediaVolumeParams(
                        action=action,  # type: ignore[arg-type]
                        level=level,
                    )
                ),
                error_prefix="Failed to control media/volume",
            )
            return

        if event.name == "read_clipboard":
            raw_max = event.arguments.get("max_chars", 8000)
            try:
                if raw_max is None:
                    max_chars = 8000
                elif isinstance(raw_max, bool):
                    raise ValueError("bool is not valid")
                else:
                    max_chars = max(1, min(100_000, int(raw_max)))
            except (TypeError, ValueError):
                await _send_error("max_chars must be an integer between 1 and 100000.")
                return

            include_history = _parse_bool("include_history", default=False)
            raw_items = event.arguments.get("max_items", 10)
            try:
                if raw_items is None:
                    max_items = 10
                elif isinstance(raw_items, bool):
                    raise ValueError("bool is not valid")
                else:
                    max_items = max(1, min(100, int(raw_items)))
            except (TypeError, ValueError):
                await _send_error("max_items must be an integer between 1 and 100.")
                return

            await _execute_rpc_tool(
                OsReadClipboardRequest(
                    params=OsReadClipboardParams(
                        max_chars=max_chars,
                        include_history=include_history,
                        max_items=max_items,
                    )
                ),
                error_prefix="Failed to read clipboard",
            )
            return

        if event.name == "select_clipboard_item":
            raw_index = event.arguments.get("index")
            if raw_index is None or isinstance(raw_index, bool):
                await _send_error("index must be an integer >= 0.")
                return
            try:
                index = int(raw_index)
            except (TypeError, ValueError):
                await _send_error("index must be an integer >= 0.")
                return
            if index < 0:
                await _send_error("index must be non-negative.")
                return
            await _execute_rpc_tool(
                OsSelectClipboardItemRequest(
                    params=OsSelectClipboardItemParams(index=index)
                ),
                error_prefix="Failed to select clipboard item",
            )
            return

        if event.name == "open_url":
            raw_url = event.arguments.get("url")
            url = "" if raw_url is None else str(raw_url).strip()
            if not url:
                await _send_error("url must not be empty.")
                return
            browser = _req_str("browser")
            await _execute_rpc_tool(
                OsOpenUrlRequest(
                    params=OsOpenUrlParams(
                        url=url,
                        browser=browser,
                    )
                ),
                error_prefix="Failed to open URL",
            )
            return

        if event.name == "control_system":
            action = (_req_str("action") or "").lower()
            if not action:
                await _send_error("action must not be empty.")
                return

            raw_level = event.arguments.get("level")
            level: int | None = None
            if raw_level is not None:
                if isinstance(raw_level, bool):
                    await _send_error("level must be an integer between 0 and 100.")
                    return
                try:
                    level = max(0, min(100, int(raw_level)))
                except (TypeError, ValueError):
                    await _send_error("level must be an integer between 0 and 100.")
                    return

            raw_step = event.arguments.get("step")
            step: int | None = None
            if raw_step is not None:
                if isinstance(raw_step, bool):
                    await _send_error("step must be an integer.")
                    return
                try:
                    step = int(raw_step)
                except (TypeError, ValueError):
                    await _send_error("step must be an integer.")
                    return

            raw_hwnd = event.arguments.get("hwnd")
            hwnd: int | None = None
            if raw_hwnd is not None:
                if isinstance(raw_hwnd, bool):
                    await _send_error("hwnd must be an integer.")
                    return
                try:
                    hwnd = int(raw_hwnd)
                except (TypeError, ValueError):
                    await _send_error("hwnd must be an integer.")
                    return

            await _execute_rpc_tool(
                OsControlSystemRequest(
                    params=OsControlSystemParams(
                        action=action,
                        level=level,
                        step=step,
                        window_action=_req_str("window_action"),
                        window_title=_req_str("window_title"),
                        hwnd=hwnd,
                        app_name=_req_str("app_name"),
                        page=_req_str("page"),
                        uri=_req_str("uri"),
                    )
                ),
                error_prefix="Failed to control system",
            )
            return

        if event.name == "write_clipboard":
            raw_text = event.arguments.get("text")
            if raw_text is None:
                await _send_error("text is required.")
                return
            text = raw_text if isinstance(raw_text, str) else str(raw_text)
            await _execute_rpc_tool(
                OsWriteClipboardRequest(params=OsWriteClipboardParams(text=text)),
                error_prefix="Failed to write clipboard",
            )
            return

        if event.name == "inspect_ui_elements":
            raw_max = event.arguments.get("max_elements", 60)
            if isinstance(raw_max, bool):
                await _send_error("max_elements must be an integer.")
                return
            try:
                max_elements = int(raw_max) if raw_max is not None else 60
            except (TypeError, ValueError):
                await _send_error("max_elements must be an integer.")
                return
            await _execute_rpc_tool(
                OsInspectUiElementsRequest(
                    params=OsInspectUiElementsParams(
                        app_name=_req_str("app_name"),
                        max_elements=max_elements,
                    )
                ),
                error_prefix="Failed to inspect UI elements",
            )
            return

        if event.name == "click_ui_element":
            name = _req_str("name")
            if not name:
                await _send_error("name is required.")
                return
            await _execute_rpc_tool(
                OsClickUiElementRequest(
                    params=OsClickUiElementParams(
                        name=name,
                        control_type=_req_str("control_type"),
                        app_name=_req_str("app_name"),
                    )
                ),
                error_prefix="Failed to click UI element",
            )
            return

        if event.name == "gui_action":
            action = _req_str("action")
            if not action:
                await _send_error("action is required.")
                return
            action_norm = action.lower()
            if action_norm not in {"click_at", "type_text", "press_keys", "scroll", "wait"}:
                await _send_error(f"Unsupported gui_action: {action!r}")
                return

            def _opt_int(key: str) -> int | None:
                raw = event.arguments.get(key)
                if raw is None:
                    return None
                if isinstance(raw, bool):
                    raise ValueError(f"{key} must be an integer.")
                return int(raw)

            try:
                x_val = _opt_int("x")
                y_val = _opt_int("y")
                clicks_val = _opt_int("clicks")
                delta_val = _opt_int("delta")
                raw_sec = event.arguments.get("seconds", 1.0)
                if isinstance(raw_sec, bool):
                    raise ValueError("seconds must be a number.")
                seconds_val = float(raw_sec) if raw_sec is not None else 1.0
            except (TypeError, ValueError) as exc:
                await _send_error(str(exc))
                return

            raw_text = event.arguments.get("text")
            text_val: str | None = None
            if raw_text is not None:
                text_val = raw_text if isinstance(raw_text, str) else str(raw_text)

            raw_keys = event.arguments.get("keys")
            keys_val: list[str] | None = None
            if raw_keys is not None:
                if not isinstance(raw_keys, list):
                    await _send_error("keys must be a list of key names.")
                    return
                keys_val = [str(k) for k in raw_keys]

            raw_button = _req_str("button") or "left"
            btn_norm = raw_button.lower()
            if btn_norm not in {"left", "right", "double"}:
                await _send_error(f"Unsupported mouse button: {raw_button!r}")
                return

            await _execute_rpc_tool(
                OsGuiActionRequest(
                    params=OsGuiActionParams(
                        action=action_norm,  # type: ignore[arg-type]
                        x=x_val,
                        y=y_val,
                        text=text_val,
                        keys=keys_val,
                        clicks=clicks_val if clicks_val is not None else 1,
                        button=btn_norm,  # type: ignore[arg-type]
                        delta=delta_val if delta_val is not None else 0,
                        seconds=seconds_val,
                    )
                ),
                error_prefix="Failed to execute GUI action",
            )
            return

        if event.name == "inspect_git_workspace":
            await _execute_rpc_tool(
                WorkspaceGitInspectRequest(
                    params=WorkspaceGitInspectParams(
                        repo_path=_req_str("repo_path"),
                        include_diff=_parse_bool("include_diff", default=False),
                        diff_stat_only=_parse_bool("diff_stat_only", default=True),
                    )
                ),
                error_prefix="Failed to inspect Git workspace",
            )
            return

        await _send_error(f"Unsupported voice tool: {event.name}")

    async def _stream_screen_burst(
        self,
        *,
        burst_seconds: float,
        backend: VoiceBackend,
        target: str = "full_screen",
        interval: float = 1.0,
    ) -> None:
        """Stream periodic screen frames for a bounded burst duration."""
        t_end = time.monotonic() + burst_seconds
        current_task = asyncio.current_task()
        logger.info(
            "Starting screen watch burst for %.1f seconds (target=%s).",
            burst_seconds,
            target,
        )
        try:
            while time.monotonic() < t_end and self._call_state == "active":
                res = await asyncio.to_thread(
                    self._screen_service.capture,
                    target=target,
                )
                if res.monitor_bounds is not None:
                    self._screen_indicator.update_watch_bounds(res.monitor_bounds)
                res_blocked = res.blocked or is_window_blocked(
                    res.window_title or "",
                    res.process_name or "",
                    self.config.blocklist,
                )
                if res.image_bytes is not None and not res_blocked:
                    await backend.send_video_frame(res.image_bytes, mime_type=res.mime_type)
                await asyncio.sleep(interval)
        except asyncio.CancelledError:
            pass
        except Exception as exc:
            logger.warning("Error during screen stream burst: %s", exc)
        finally:
            if self._watch_screen_task is current_task:
                self._watch_screen_task = None
                self._screen_indicator.stop_watching()
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
                self._daemon_listener_active = True
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
                            self._spawn_daemon_callback_task(
                                self.start_call(trigger="daemon_event"),
                                name=f"daemon-start-call-{self._call_id}",
                            )
                    elif isinstance(frame, CallEndedEvent):
                        if self._call_state in ("connecting", "active"):
                            self._spawn_daemon_callback_task(
                                self.end_call(
                                    reason=frame.data.reason or "daemon_event",
                                    from_daemon=True,
                                ),
                                name=f"daemon-end-call-{frame.data.call_id}",
                            )
                    elif isinstance(frame, ResponseFrame):
                        # Check if this resolves a delegated task or RPC request
                        fut = self._pending_tasks.get(frame.id)
                        if fut and not fut.done():
                            if frame.ok:
                                fut.set_result(frame.result)
                            else:
                                fut.set_exception(RuntimeError(frame.error or "Task failed"))
        finally:
            self._daemon_listener_active = False
            self._daemon_ws = None

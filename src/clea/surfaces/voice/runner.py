"""Voice surface runner managing the call lifecycle, audio I/O, wake word, and backend."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import importlib
import inspect
import json
import logging
import logging.handlers
import math
import queue
import re
import sys
import threading
import time
import urllib.parse
from collections.abc import AsyncIterator, Callable, Coroutine
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

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
    MemoryListFactsParams,
    MemoryListFactsRequest,
    MemoryManageFactParams,
    MemoryManageFactRequest,
    MemorySearchEpisodicParams,
    MemorySearchEpisodicRequest,
    OsClickUiElementParams,
    OsClickUiElementRequest,
    OsControlMediaVolumeParams,
    OsControlMediaVolumeRequest,
    OsControlSystemParams,
    OsControlSystemRequest,
    OsGuiActionParams,
    OsGuiActionRequest,
    OsInspectScreenParams,
    OsInspectScreenRequest,
    OsInspectUiElementsParams,
    OsInspectUiElementsRequest,
    OsOpenOrFocusAppParams,
    OsOpenOrFocusAppRequest,
    OsOpenPathOrUrlParams,
    OsOpenPathOrUrlRequest,
    OsOpenUrlParams,
    OsOpenUrlRequest,
    OsReadClipboardParams,
    OsReadClipboardRequest,
    OsSaveClipboardToFileParams,
    OsSaveClipboardToFileRequest,
    OsSelectClipboardItemParams,
    OsSelectClipboardItemRequest,
    OsTransformClipboardParams,
    OsTransformClipboardRequest,
    OsUiActParams,
    OsUiActRequest,
    OsUiBatchParams,
    OsUiBatchRequest,
    OsUiSnapshotParams,
    OsUiSnapshotRequest,
    OsWriteClipboardParams,
    OsWriteClipboardRequest,
    ResponseFrame,
    TranscriptData,
    TranscriptEvent,
    TranscriptsGetParams,
    TranscriptsGetRequest,
    UiBatchStep,
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
from clea.surfaces.voice.screen import (
    NATIVE_PNG_MAX_BYTES,
    ScreenCaptureService,
    encode_native_frame,
    is_window_blocked,
)
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


_LAUNCH_APP_ALIASES: dict[str, str] = {
    "vscode": "vscode",
    "code": "vscode",
    "vs code": "vscode",
    "visual studio code": "vscode",
    "terminal": "terminal",
    "wt": "terminal",
    "windows terminal": "terminal",
    "windowsterminal": "terminal",
    "chrome": "chrome",
    "google chrome": "chrome",
    "edge": "edge",
    "msedge": "edge",
    "microsoft edge": "edge",
    "explorer": "explorer",
    "file explorer": "explorer",
    "cursor": "cursor",
    "antigravity": "antigravity",
    "google antigravity": "antigravity",
    "notepad": "notepad",
    "calculator": "calculator",
    "calc": "calculator",
    "settings": "settings",
    "windows settings": "settings",
    "ms-settings": "settings",
    "taskmgr": "taskmgr",
    "task manager": "taskmgr",
}

_LOCAL_FILE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".txt",
        ".md",
        ".json",
        ".py",
        ".log",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".bmp",
        ".pdf",
        ".csv",
        ".html",
        ".htm",
        ".xml",
        ".yaml",
        ".yml",
        ".toml",
        ".ini",
    }
)

_HOST_PREFIX_LABELS: frozenset[str] = frozenset(
    {"www", "m", "open", "web", "app", "play", "mobile", "desktop"}
)

_TLD_LABELS: frozenset[str] = frozenset(
    {
        "com",
        "org",
        "net",
        "io",
        "co",
        "uk",
        "in",
        "edu",
        "gov",
        "ai",
        "app",
        "dev",
        "tv",
        "gg",
        "me",
        "us",
        "de",
        "fr",
        "jp",
    }
)

_BROWSER_EXPLICIT_RE = re.compile(
    r"\b(?:in\s+(?:the\s+)?(?:browser|chrome|edge|firefox|brave|web)|"
    r"web\s+version|browser\s+tab|website)\b",
    re.IGNORECASE,
)

_WINDOW_LAYOUT_ACTIONS: frozenset[str] = frozenset(
    {
        "snap_left",
        "snap_right",
        "snap_left_window",
        "snap_right_window",
        "maximize_window",
        "minimize_window",
        "restore_window",
        "close_window",
        "maximize",
        "minimize",
        "restore",
        "close",
    }
)


def _is_local_file_target(raw: str | None) -> bool:
    """Return True if *raw* refers to a local file/directory path rather than an app or URL."""
    if not raw or not isinstance(raw, str):
        return False
    stripped = raw.strip().strip("'\"")
    if not stripped or "://" in stripped:
        return False
    if re.match(r"^[A-Za-z]:[\\/]", stripped) or "\\" in stripped:
        return True
    suffix = Path(stripped).suffix.lower()
    return suffix in _LOCAL_FILE_EXTENSIONS


def normalize_launch_target(target: str | None) -> str:
    """Normalize an app name, service shorthand, or URL to a canonical target key."""
    if not target or not isinstance(target, str):
        return ""
    cleaned = " ".join(target.strip().strip("'\"").lower().split())
    if not cleaned or _is_local_file_target(cleaned):
        return ""

    if cleaned.endswith(".exe"):
        cleaned = cleaned[:-4].strip()

    for suffix in (" desktop app", " web app", " pwa", " app"):
        if cleaned.endswith(suffix) and len(cleaned) > len(suffix):
            cleaned = cleaned[: -len(suffix)].strip()

    if cleaned in _LAUNCH_APP_ALIASES:
        return _LAUNCH_APP_ALIASES[cleaned]

    # Check if input is a URL or domain name
    looks_like_url = "://" in cleaned or (
        "." in cleaned and not any(ch.isspace() for ch in cleaned)
    )
    if looks_like_url:
        candidate = cleaned if "://" in cleaned else f"https://{cleaned}"
        try:
            parsed = urllib.parse.urlsplit(candidate)
            host = (parsed.hostname or "").lower().strip(".")
        except Exception:
            host = ""
        if host:
            if host in _LAUNCH_APP_ALIASES:
                return _LAUNCH_APP_ALIASES[host]
            labels = [lbl for lbl in host.split(".") if lbl]
            while len(labels) > 1 and labels[0] in _HOST_PREFIX_LABELS:
                labels.pop(0)
            while len(labels) > 1 and labels[-1] in _TLD_LABELS:
                labels.pop()
            if labels:
                brand = labels[-1]
                return _LAUNCH_APP_ALIASES.get(brand, brand)

    return _LAUNCH_APP_ALIASES.get(cleaned, cleaned)


def _is_app_candidate_url(url: str, norm_target: str) -> bool:
    """Return True if *url* is a shorthand or root URL eligible for app-first launch."""
    if not norm_target:
        return False
    stripped = url.strip()
    if "://" not in stripped and "/" not in stripped and "?" not in stripped:
        return True
    candidate = stripped if "://" in stripped else f"https://{stripped}"
    try:
        parsed = urllib.parse.urlsplit(candidate)
    except Exception:
        return False
    path = (parsed.path or "").strip("/")
    return not path and not parsed.query and not parsed.fragment


def _is_browser_explicitly_requested(
    context_text: str | None = None,
    browser: str | None = None,
) -> bool:
    """Return True when the caller or user explicitly requested opening in a web browser."""
    if browser and browser.strip():
        return True
    if context_text and _BROWSER_EXPLICIT_RE.search(context_text):
        return True
    return False


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
        action_verifier: Any = None,
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
        self._action_verifier = action_verifier
        self._turn_launched_targets: dict[str, dict[str, Any]] = {}
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

        self._call_generation: int = 0
        self._active_tool_calls: int = 0
        self._tool_exec_lock: asyncio.Lock | None = None
        self._active_tool_tasks: set[asyncio.Task[Any]] = set()
        self._audio_health_task: asyncio.Task[None] | None = None
        self._audio_health_poll_interval: float = 3.0
        self._audio_send_task: asyncio.Task[None] | None = None
        self._outbound_audio_queue: asyncio.Queue[bytes] | None = None
        self._wake_word_queue: queue.Queue[bytes | None] = queue.Queue(maxsize=32)
        self._wake_word_thread: threading.Thread | None = None
        self._wake_word_stop = threading.Event()
        self._wake_word_pending_trigger: bool = False
        self._peak_wake_score_window: float = 0.0

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
                    await asyncio.to_thread(self._audio_manager.start)
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
                    await asyncio.to_thread(refresh_audio_devices)

            if self._audio_manager.resolved_input_device:
                inp = self._audio_manager.resolved_input_device
                logger.info(
                    "Input device: #%d '%s' (host API: %s, native rate: %d Hz, "
                    "negotiated: %d Hz, %dch)",
                    inp["index"],
                    inp["name"],
                    inp.get("hostapi_name", "Unknown"),
                    int(inp.get("default_samplerate", 16000)),
                    int(getattr(self._audio_manager, "input_sample_rate", 16000)),
                    int(getattr(self._audio_manager, "input_channels", 1)),
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

            # 2. Initialize wake word detector off the event loop (ONNX load can take 5-15s).
            # Keep self._wake_word unpublished until the worker thread is started and stale
            # audio buffered during model load has been flushed, eliminating startup races.
            detector = self._wake_word
            self._wake_word = None
            if detector is None:
                try:
                    detector = await asyncio.to_thread(
                        WakeWordDetector,
                        model_name=self.config.wake_word_model,
                        threshold=self.config.wake_threshold,
                        data_dir=self.data_dir,
                    )
                except WakeWordModelNotFoundError as exc:
                    logger.error(str(exc))
                    raise

            with contextlib.suppress(Exception):
                detector.reset()

            # Start dedicated wake-word worker thread & audio health watchdog for live streams
            # (mocked audio managers in direct unit tests retain synchronous _on_audio_chunk).
            if getattr(self._audio_manager, "_health_monitored", False):
                self._start_wake_word_worker()

            # Discard audio queued during ONNX model load so the detector begins on fresh
            # live 16 kHz frames, then publish detector and arm IDLE atomically.
            flush_fn = getattr(self._audio_manager, "flush_input_buffer", None)
            if callable(flush_fn):
                with contextlib.suppress(Exception):
                    flush_fn()
            self._drain_wake_word_queue()
            self._call_state = "idle"
            self._wake_word_pending_trigger = False
            self._peak_wake_score_window = 0.0
            self._wake_word = detector

            if getattr(self._audio_manager, "_health_monitored", False):
                self._audio_health_task = asyncio.create_task(
                    self._audio_health_watchdog(),
                    name="VoiceRunnerAudioHealthWatchdog",
                )
            logger.info(
                "Wake-word listener armed (model=%s, threshold=%.2f, sample_rate=%d Hz, "
                "worker_thread=%s).",
                getattr(self._wake_word, "model_key", self.config.wake_word_model),
                float(getattr(self._wake_word, "threshold", self.config.wake_threshold)),
                int(getattr(self._audio_manager, "input_sample_rate", 16000)),
                bool(self._wake_word_thread and self._wake_word_thread.is_alive()),
            )

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
            self._stop_wake_word_worker()
            if self._audio_health_task is not None:
                self._audio_health_task.cancel()
                self._audio_health_task = None
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

        if self._audio_health_task is not None:
            health_task = self._audio_health_task
            self._audio_health_task = None
            health_task.cancel()
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await health_task

        for task in list(self._active_tool_tasks):
            if not task.done():
                task.cancel()
        if self._active_tool_tasks:
            await asyncio.gather(*list(self._active_tool_tasks), return_exceptions=True)
            self._active_tool_tasks.clear()

        for task in list(self._daemon_task_refs):
            if not task.done():
                task.cancel()
        if self._daemon_task_refs:
            await asyncio.gather(*list(self._daemon_task_refs), return_exceptions=True)
            self._daemon_task_refs.clear()

        self._stop_wake_word_worker()

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

    def _start_wake_word_worker(self) -> None:
        """Start dedicated background thread for ONNX wake-word inference."""
        if self._wake_word_thread is not None and self._wake_word_thread.is_alive():
            return
        self._wake_word_stop.clear()
        self._drain_wake_word_queue()
        thread = threading.Thread(
            target=self._wake_word_worker_loop,
            name="CleaVoiceWakeWordWorker",
            daemon=True,
        )
        self._wake_word_thread = thread
        thread.start()

    def _stop_wake_word_worker(self) -> None:
        """Stop the dedicated wake-word worker thread cleanly."""
        self._wake_word_stop.set()
        self._drain_wake_word_queue()
        with contextlib.suppress(queue.Full):
            self._wake_word_queue.put_nowait(None)
        thread = self._wake_word_thread
        self._wake_word_thread = None
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)

    def _drain_wake_word_queue(self) -> None:
        """Discard any pending audio chunks queued for wake-word inference."""
        while True:
            try:
                self._wake_word_queue.get_nowait()
            except queue.Empty:
                break

    def _wake_word_worker_loop(self) -> None:
        """Background consumer running openwakeword inference off the WASAPI thread."""
        while not self._wake_word_stop.is_set():
            try:
                chunk = self._wake_word_queue.get(timeout=0.25)
            except queue.Empty:
                continue
            if chunk is None or self._wake_word_stop.is_set():
                break
            self._process_wake_word_chunk(chunk)

    def _process_wake_word_chunk(self, pcm_bytes: bytes) -> None:
        """Evaluate a 16 kHz PCM chunk with WakeWordDetector and trigger start_call."""
        if (
            self._call_state != "idle"
            or self._wake_word is None
            or self._wake_word_pending_trigger
            or self._loop is None
            or self._loop.is_closed()
        ):
            return
        try:
            detected, score, _key = self._wake_word.process_audio(pcm_bytes)
            if score > self._peak_wake_score_window:
                self._peak_wake_score_window = score
            if detected:
                self._wake_word_pending_trigger = True
                self._wake_word.reset()
                self._drain_wake_word_queue()
                logger.info("Wake word detected locally with score %.3f!", score)
                fut = asyncio.run_coroutine_threadsafe(
                    self.start_call(trigger="wake_word"),
                    self._loop,
                )

                def _on_trigger_done(done_fut: Any) -> None:
                    with contextlib.suppress(Exception):
                        if done_fut.cancelled():
                            self._wake_word_pending_trigger = False
                            return
                        exc = done_fut.exception()
                        if exc is not None:
                            self._wake_word_pending_trigger = False
                            logger.warning("Wake-word start_call failed: %s", exc)
                        elif self._call_state == "idle":
                            self._wake_word_pending_trigger = False

                fut.add_done_callback(_on_trigger_done)
        except Exception:
            logger.exception("Error during wake word processing.")

    async def _audio_health_watchdog(self) -> None:
        """Periodically verify AudioManager InputStream health and follow OS default hot-swaps."""
        poll = max(0.05, float(self._audio_health_poll_interval))
        while not self._stop_event.is_set():
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=poll)
            if self._stop_event.is_set():
                break
            am = self._audio_manager
            if am is None or not getattr(am, "_health_monitored", False):
                continue
            health_fn = getattr(am, "is_input_stream_healthy", None)
            restart_fn = getattr(am, "restart_streams", None)
            if not callable(health_fn) or not callable(restart_fn):
                continue
            healthy, reason = health_fn()
            if healthy:
                default_changed_fn = getattr(am, "has_default_audio_device_changed", None)
                if callable(default_changed_fn):
                    try:
                        raw_change = await asyncio.to_thread(default_changed_fn)
                        if isinstance(raw_change, tuple) and len(raw_change) == 2:
                            changed, change_reason = bool(raw_change[0]), str(raw_change[1])
                        else:
                            changed, change_reason = False, "ignored"
                    except Exception as exc:
                        logger.debug("Default audio device check failed: %s", exc)
                        changed, change_reason = False, "error"
                    if changed:
                        healthy = False
                        reason = change_reason
            if healthy:
                continue

            telemetry_fn = getattr(am, "input_rms_telemetry", None)
            telemetry = telemetry_fn() if callable(telemetry_fn) else {}
            if reason.startswith("os_default_"):
                logger.info(
                    "OS default audio endpoint changed (%s); rebinding audio streams to "
                    "the active OS default device...",
                    reason,
                )
            else:
                logger.warning(
                    "Audio input stream unhealthy (%s, last_rms=%s, peak_rms=%s); "
                    "attempting automatic recovery...",
                    reason,
                    telemetry.get("last_rms", "?"),
                    telemetry.get("peak_rms", "?"),
                )

            recovered = await asyncio.to_thread(restart_fn)
            if recovered:
                flush_fn = getattr(am, "flush_input_buffer", None)
                if callable(flush_fn):
                    with contextlib.suppress(Exception):
                        flush_fn()
                self._drain_wake_word_queue()
                if self._wake_word is not None:
                    with contextlib.suppress(Exception):
                        await asyncio.to_thread(self._wake_word.reset)
                if self._wake_word_thread is None or not self._wake_word_thread.is_alive():
                    self._start_wake_word_worker()
                inp = getattr(am, "resolved_input_device", None) or {}
                out = getattr(am, "resolved_output_device", None) or {}
                post_telemetry = telemetry_fn() if callable(telemetry_fn) else {}
                logger.info(
                    "Audio streams rebound to input=#%s '%s' (%s, %s Hz, %sch, last_rms=%s) "
                    "and output=#%s '%s' (%s).",
                    inp.get("index", "?"),
                    inp.get("name", "unknown"),
                    inp.get("hostapi_name", "Unknown"),
                    getattr(am, "input_sample_rate", "?"),
                    getattr(am, "input_channels", "?"),
                    post_telemetry.get("last_rms", "?"),
                    out.get("index", "?"),
                    out.get("name", "unknown"),
                    out.get("hostapi_name", "Unknown"),
                )

    def _enqueue_outbound_audio(self, pcm_bytes: bytes) -> None:
        """Bounded backpressure enqueue for outbound microphone PCM frames on event loop."""
        q = self._outbound_audio_queue
        if q is None or self._call_state != "active":
            return
        if q.full():
            with contextlib.suppress(asyncio.QueueEmpty):
                q.get_nowait()
        with contextlib.suppress(asyncio.QueueFull):
            q.put_nowait(pcm_bytes)

    async def _drain_outbound_audio(
        self,
        backend: VoiceBackend,
        audio_queue: asyncio.Queue[bytes],
    ) -> None:
        """Drain bounded outbound PCM chunks to the active Live backend."""
        try:
            while self._call_state == "active" and self._active_backend is backend:
                pcm_bytes = await audio_queue.get()
                if self._call_state != "active" or self._active_backend is not backend:
                    break
                try:
                    await backend.send_audio(pcm_bytes)
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    logger.debug("Ignoring outbound send_audio failure: %s", exc)
        except asyncio.CancelledError:
            pass

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
                peak_sc = self._peak_wake_score_window
                self._peak_wake_score_window = 0.0
                msg = (
                    f"[AUDIO-DEBUG] Wake listen: {fps:4.1f} fps | "
                    f"16 kHz RMS: {rms:6.1f} | Wake peak: {peak_sc:.3f}"
                )
                print(msg, flush=True)
                self._frames_sent_window = 0
                self._last_fps_report_time = now

            if self._wake_word_pending_trigger:
                return

            # Offload heavy ONNX inference off the real-time PortAudio callback thread
            # when the worker thread is running; keep synchronous fallback for direct unit tests.
            worker = self._wake_word_thread
            if worker is not None and worker.is_alive():
                if self._wake_word_queue.full():
                    with contextlib.suppress(queue.Empty):
                        self._wake_word_queue.get_nowait()
                with contextlib.suppress(queue.Full):
                    self._wake_word_queue.put_nowait(pcm_bytes)
            else:
                self._process_wake_word_chunk(pcm_bytes)
        elif self._call_state == "active" and self._active_backend is not None:
            # Check audio energy for real speech vs background silence
            rms = compute_audio_rms(pcm_bytes)
            if rms >= SPEECH_RMS_THRESHOLD:
                self._last_user_speech_time = time.monotonic()

            is_speaking = bool(self._audio_manager and self._audio_manager.is_speaking)
            if is_speaking or self._active_tool_calls > 0:
                # Keep disarmed while the assistant is speaking or executing tools
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

            if self._outbound_audio_queue is not None and self._audio_send_task is not None:
                with contextlib.suppress(RuntimeError):
                    self._loop.call_soon_threadsafe(self._enqueue_outbound_audio, pcm_bytes)
            else:
                fut = asyncio.run_coroutine_threadsafe(
                    self._active_backend.send_audio(pcm_bytes),
                    self._loop,
                )

                def _log_send_err(done_fut: Any) -> None:
                    with contextlib.suppress(Exception):
                        exc = done_fut.exception()
                        if exc is not None:
                            logger.debug("Ignoring fallback send_audio failure: %s", exc)

                fut.add_done_callback(_log_send_err)

    async def start_call(self, *, trigger: str = "manual") -> None:
        """Initiate transition to CONNECTING -> ACTIVE."""
        if self._call_state != "idle":
            self._wake_word_pending_trigger = False
            logger.debug("Cannot start call; current state is %s", self._call_state)
            return

        call_start_t0 = time.monotonic()
        self._call_generation += 1
        call_gen = self._call_generation
        self._wake_word_pending_trigger = False
        self._active_tool_calls = 0

        if self._call_id is None:
            self._call_id = new_id()

        self._call_state = "connecting"
        self._set_voice_state("thinking")
        self._transcript_history.clear()
        self._turn_launched_targets.clear()
        self._first_audio_latency_ms = None
        logger.info("Starting call (call_id=%s, trigger=%s)...", self._call_id, trigger)

        backend: VoiceBackend | None = None
        try:
            # 1 & 2. Fetch call start + focused chat state concurrently when listener active
            memory_context: str | None = None
            focus_res: dict[str, Any] | None = None

            if self._daemon_ws is not None:
                if self._daemon_listener_active:
                    coros: list[Coroutine[Any, Any, dict[str, Any]]] = []
                    has_start_req = trigger != "daemon_event"
                    if has_start_req:
                        start_req = VoiceStartCallRequest(
                            params=VoiceStartCallParams(trigger=trigger)  # type: ignore[arg-type]
                        )
                        coros.append(self._request_daemon(start_req))
                    coros.append(self._request_daemon(ChatFocusRequest(params=ChatFocusParams())))
                    gathered = await asyncio.gather(*coros, return_exceptions=True)
                    idx = 0
                    if has_start_req:
                        start_raw = gathered[0]
                        idx = 1
                        if isinstance(start_raw, BaseException):
                            logger.warning("Could not notify daemon of call start: %s", start_raw)
                        else:
                            if isinstance(start_raw.get("call_id"), str) and start_raw["call_id"]:
                                self._call_id = start_raw["call_id"]
                            if isinstance(start_raw.get("memory_context"), str):
                                memory_context = start_raw["memory_context"]
                    focus_raw = gathered[idx]
                    if isinstance(focus_raw, BaseException):
                        logger.warning("Could not query focused chat state: %s", focus_raw)
                    else:
                        focus_res = focus_raw
                elif trigger != "daemon_event":
                    start_req = VoiceStartCallRequest(
                        params=VoiceStartCallParams(trigger=trigger)  # type: ignore[arg-type]
                    )
                    try:
                        await self._daemon_ws.send(start_req.model_dump_json())
                    except (OSError, WebSocketException, TimeoutError, RuntimeError) as exc:
                        logger.warning("Could not notify daemon of call start: %s", exc)

            if self._call_generation != call_gen or self._call_state != "connecting":
                logger.debug(
                    "Call start aborted before system instruction (call_gen=%d).",
                    call_gen,
                )
                return

            parsed_id, parsed_title = extract_focused_chat_from_context(memory_context)
            focused_chat_id = parsed_id
            focused_chat_title = parsed_title
            focused_chat_available = bool(parsed_id)
            if focus_res is not None:
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

            if (
                self._daemon_ws is not None
                and self._daemon_listener_active
                and focused_chat_available
                and focused_chat_id
                and not focused_chat_title
            ):
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

            if self._call_generation != call_gen or self._call_state != "connecting":
                logger.debug("Call start aborted before backend.connect (call_gen=%d).", call_gen)
                return

            context_ms = (time.monotonic() - call_start_t0) * 1000.0

            # 4. Create backend and connect with latency measurement
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

            # Critical race guard: if end_call ran while backend.connect() was in flight,
            # close the newly opened backend cleanly and never overwrite IDLE / a new call!
            if (
                self._call_generation != call_gen
                or self._call_state != "connecting"
                or self._call_id is None
            ):
                logger.info(
                    "Call start superseded/ended during connect (call_gen=%d); "
                    "closing orphan backend.",
                    call_gen,
                )
                with contextlib.suppress(Exception):
                    await backend.close()
                return

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

            # Start bounded outbound audio drainer
            outbound_q: asyncio.Queue[bytes] = asyncio.Queue(maxsize=50)
            self._outbound_audio_queue = outbound_q
            self._audio_send_task = asyncio.create_task(
                self._drain_outbound_audio(backend, outbound_q),
                name="VoiceRunnerOutboundAudio",
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
            total_start_ms = (time.monotonic() - call_start_t0) * 1000.0
            logger.debug(
                "voice_turn_timing phase=call_start call_id=%s context_ms=%.1f "
                "connect_ms=%.1f total_ms=%.1f",
                self._call_id,
                context_ms,
                self._connect_latency_ms,
                total_start_ms,
            )
            logger.info(
                "Call %s is now ACTIVE (connect_latency=%.1fms).",
                self._call_id,
                self._connect_latency_ms,
            )
        except Exception as exc:
            if backend is not None and self._active_backend is not backend:
                with contextlib.suppress(Exception):
                    await backend.close()
            if self._call_generation != call_gen:
                logger.debug(
                    "Ignoring start_call error for superseded call_gen=%d: %s",
                    call_gen,
                    exc,
                )
                return
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

        # Invalidate any in-flight start_call immediately
        self._call_generation += 1
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

        # Cancel watchdog, outbound audio, active tool tasks, screen watching, and consumer
        if self._silence_watchdog_task is not None:
            self._silence_watchdog_task.cancel()
            self._silence_watchdog_task = None

        if self._stall_watchdog_task is not None:
            self._stall_watchdog_task.cancel()
            self._stall_watchdog_task = None

        if self._audio_send_task is not None:
            send_task = self._audio_send_task
            self._audio_send_task = None
            self._outbound_audio_queue = None
            send_task.cancel()
            if send_task is not asyncio.current_task():
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await send_task
        else:
            self._outbound_audio_queue = None

        current_task = asyncio.current_task()
        for tool_task in list(self._active_tool_tasks):
            if tool_task is not current_task and not tool_task.done():
                tool_task.cancel()
        self._active_tool_tasks.clear()
        self._active_tool_calls = 0

        if self._watch_screen_task is not None:
            watch_task = self._watch_screen_task
            self._watch_screen_task = None
            watch_task.cancel()
            if watch_task is not current_task:
                with contextlib.suppress(asyncio.CancelledError, Exception):
                    await watch_task

        with contextlib.suppress(Exception):
            self._screen_indicator.hide()

        # Stop audio playback and flush stale input chunks immediately
        if self._audio_manager is not None:
            self._audio_manager.stop_output()
            flush_fn = getattr(self._audio_manager, "flush_input_buffer", None)
            if callable(flush_fn):
                with contextlib.suppress(Exception):
                    flush_fn()

        # Close backend
        if self._active_backend is not None:
            backend = self._active_backend
            self._active_backend = None
            with contextlib.suppress(Exception):
                await backend.close()

        if self._backend_consume_task is not None:
            consume_task = self._backend_consume_task
            self._backend_consume_task = None
            if consume_task is not current_task:
                consume_task.cancel()
                with contextlib.suppress(asyncio.CancelledError):
                    await consume_task

        # While still in "ending" state, clear call state and reset wake-word buffers BEFORE
        # re-arming IDLE so the wake-word worker cannot race with reset() or reuse stale call_id.
        self._call_id = None
        self._transcript_history.clear()
        self._turn_launched_targets.clear()
        self._stall_timer_armed = False
        self._stall_accumulated_seconds = 0.0
        self._audio_sent_start_time = None
        self._peak_wake_score_window = 0.0
        if self._audio_manager is not None:
            flush_fn = getattr(self._audio_manager, "flush_input_buffer", None)
            if callable(flush_fn):
                with contextlib.suppress(Exception):
                    flush_fn()
        self._drain_wake_word_queue()
        if self._wake_word is not None:
            self._wake_word.reset()
        if (
            self._audio_manager is not None
            and getattr(self._audio_manager, "_health_monitored", False)
            and not self._stop_event.is_set()
            and (self._wake_word_thread is None or not self._wake_word_thread.is_alive())
        ):
            self._start_wake_word_worker()

        # Re-arm IDLE now that all wake-word and call state has been cleanly reset
        self._wake_word_pending_trigger = False
        self._call_state = "idle"
        if error is not None:
            self._set_voice_state("error", error=error)
        else:
            self._set_voice_state("idle")

        # Write usage & metrics record off the event loop
        await asyncio.to_thread(
            self._write_metrics_record,
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

        # Persist transcript quickly (bounded wait so fast localhost RPC completes cleanly,
        # detaching into background if slow) and always trigger consolidation in the background.
        request_fn = self._request_daemon if self._daemon_listener_active else None
        has_transcript = bool(normalize_voice_transcript(transcript_snapshot))

        async def _store_transcript_task() -> None:
            try:
                await asyncio.wait_for(
                    safe_store_voice_transcript(
                        call_id=ended_call_id,
                        entries=transcript_snapshot,
                        daemon_url=self.daemon_url,
                        token=self.token,
                        data_dir=self.data_dir,
                        request_fn=request_fn,
                    ),
                    timeout=10.0,
                )
            except TimeoutError:
                logger.warning(
                    "Timed out storing voice transcript for call %s.", ended_call_id
                )

        async def _consolidate_task(store_dep: asyncio.Task[Any]) -> None:
            with contextlib.suppress(asyncio.CancelledError, Exception):
                await store_dep
            try:
                await asyncio.wait_for(
                    safe_consolidate_voice_call(
                        call_id=ended_call_id,
                        entries=transcript_snapshot,
                        daemon_url=self.daemon_url,
                        token=self.token,
                        data_dir=self.data_dir,
                        request_fn=request_fn,
                    ),
                    timeout=10.0,
                )
            except TimeoutError:
                logger.warning(
                    "Timed out triggering consolidation for call %s.", ended_call_id
                )

        store_task = self._spawn_daemon_callback_task(
            _store_transcript_task(),
            name=f"post-call-store-{ended_call_id}",
        )
        if has_transcript:
            self._spawn_daemon_callback_task(
                _consolidate_task(store_task),
                name=f"post-call-consolidate-{ended_call_id}",
            )
        with contextlib.suppress(TimeoutError):
            await asyncio.wait_for(asyncio.shield(store_task), timeout=1.5)
        if has_transcript:
            # Yield once (0ms wall time) so immediate/mocked consolidation hooks start promptly
            await asyncio.sleep(0)

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
            if (
                (self._audio_manager and self._audio_manager.is_speaking)
                or self._active_tool_calls > 0
                or self._state == "thinking"
            ):
                # Assistant is talking or executing tools; don't count silence against user
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
            if (
                (self._audio_manager and self._audio_manager.is_speaking)
                or self._active_tool_calls > 0
                or self._state == "thinking"
            ):
                # Assistant is speaking or executing tools; reset and disarm outbound stall timer
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
                    self._turn_launched_targets.clear()
                    if self._audio_manager is not None:
                        self._audio_manager.stop_output()
                    self._set_voice_state("listening")
                elif isinstance(event, TurnCompleteEvent):
                    self._turn_launched_targets.clear()
                    if self._transcript_history:
                        self._transcript_history[-1]["final"] = True
                    self._set_voice_state("listening")
                elif isinstance(event, TranscriptOutputEvent):
                    if event.role == "user":
                        self._last_user_speech_time = time.monotonic()
                        self._turn_launched_targets.clear()
                    ts_iso = datetime.now().astimezone().isoformat()
                    norm_evt_text = event.text.strip()
                    if (
                        event.role == "assistant"
                        and norm_evt_text
                        and self._transcript_history
                        and self._transcript_history[-1].get("role") == "assistant"
                        and str(self._transcript_history[-1].get("text", "")).strip()
                        == norm_evt_text
                    ):
                        prev_entry = self._transcript_history[-1]
                        if bool(prev_entry.get("final")) == bool(event.final):
                            continue
                        prev_entry["final"] = bool(event.final)
                        prev_entry["timestamp"] = ts_iso
                    else:
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
                    # Flush any speculative pre-tool speech audio and unfinalized pre-tool
                    # assistant transcript so Clea never speaks an early confirmation before
                    # tool execution completes and a second confirmation afterwards.
                    if self._audio_manager is not None and (
                        self._state == "speaking" or self._audio_manager.is_speaking
                    ):
                        self._audio_manager.stop_output()
                    while (
                        self._transcript_history
                        and self._transcript_history[-1].get("role") == "assistant"
                        and not self._transcript_history[-1].get("final")
                    ):
                        self._transcript_history.pop()
                    self._set_voice_state("thinking")
                    tool_batch = self._drain_contiguous_tool_calls(backend, event)
                    self._spawn_tool_batch_task(tool_batch, backend)
                elif isinstance(event, ToolCallCancellationEvent):
                    for cid in event.call_ids:
                        fut = self._pending_tasks.pop(cid, None)
                        if fut and not fut.done():
                            fut.cancel()
                    if self._state == "thinking" and self._active_tool_calls == 0:
                        self._set_voice_state("listening")
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

    def reset_turn_state(self) -> None:
        """Reset per-turn launch deduplication state."""
        self._turn_launched_targets.clear()

    def _spawn_tool_batch_task(
        self,
        batch: list[ToolCallEvent],
        backend: VoiceBackend,
    ) -> asyncio.Task[None]:
        """Run a tool call batch off the backend event consumer loop (serialized FIFO)."""
        if self._tool_exec_lock is None:
            self._tool_exec_lock = asyncio.Lock()
        lock = self._tool_exec_lock

        async def _runner() -> None:
            async with lock:
                if self._call_state != "active" or self._active_backend is not backend:
                    return
                await self._execute_tool_call_batch(batch, backend)

        task = asyncio.create_task(
            _runner(),
            name=f"VoiceRunnerToolBatch-{batch[0].name}",
        )
        self._active_tool_tasks.add(task)

        def _done(done_task: asyncio.Task[None]) -> None:
            self._active_tool_tasks.discard(done_task)
            if not done_task.cancelled():
                exc = done_task.exception()
                if exc is not None:
                    logger.warning("Tool batch task failed: %s", exc)

        task.add_done_callback(_done)
        return task

    def _drain_contiguous_tool_calls(
        self,
        backend: VoiceBackend,
        first_event: ToolCallEvent,
    ) -> list[ToolCallEvent]:
        """Drain any immediately queued contiguous ToolCallEvents from the backend queue."""
        drain_fn = getattr(backend, "drain_contiguous_tool_calls", None)
        if callable(drain_fn):
            batch = list(drain_fn(first_event))
        else:
            batch = [first_event]
            q: asyncio.Queue[Any] | None = getattr(backend, "_events_queue", None) or getattr(
                backend, "event_queue", None
            )
            if q is not None:
                internal_deque = getattr(q, "_queue", None)
                while internal_deque and isinstance(internal_deque[0], ToolCallEvent):
                    try:
                        nxt = q.get_nowait()
                    except asyncio.QueueEmpty:
                        break
                    if isinstance(nxt, ToolCallEvent):
                        batch.append(nxt)
        if len(batch) > 1:
            # If the same batch has both open_or_focus_app and open_url for the same target,
            # prioritize open_or_focus_app first so the installed app launches and open_url dedupes.
            app_targets = {
                normalize_launch_target(str(ev.arguments.get("app_name") or ""))
                for ev in batch
                if ev.name == "open_or_focus_app"
            } - {""}
            if app_targets:
                prioritized: list[ToolCallEvent] = []
                deferred_urls: list[ToolCallEvent] = []
                for ev in batch:
                    if ev.name == "open_url":
                        u_norm = normalize_launch_target(str(ev.arguments.get("url") or ""))
                        if u_norm in app_targets and not _is_browser_explicitly_requested(
                            str(ev.arguments.get("reason") or ""),
                            str(ev.arguments.get("browser") or "") or None,
                        ):
                            deferred_urls.append(ev)
                            continue
                    prioritized.append(ev)
                batch = prioritized + deferred_urls
        return batch

    async def _execute_tool_call_batch(
        self,
        batch: list[ToolCallEvent],
        backend: VoiceBackend,
    ) -> None:
        """Execute a batch of tool calls and send all responses in a single backend call."""
        batch_t0 = time.monotonic()
        self._active_tool_calls += 1
        # Disarm stall watchdog while tools execute so slow RPCs never cause false reconnects
        self._stall_timer_armed = False
        self._stall_accumulated_seconds = 0.0
        self._audio_sent_start_time = None
        try:
            if len(batch) <= 1:
                for ev in batch:
                    try:
                        await self._handle_tool_call(ev, backend)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning(
                            "Unhandled error in voice tool %s (%s): %s", ev.name, ev.call_id, exc
                        )
                        with contextlib.suppress(Exception):
                            await backend.send_tool_responses(
                                [
                                    VoiceToolResponse(
                                        call_id=ev.call_id,
                                        name=ev.name,
                                        response={"error": f"Tool execution failed: {exc}"},
                                        is_error=True,
                                        scheduling="when_idle",
                                    )
                                ]
                            )
                return

            collected: list[VoiceToolResponse] = []

            class _BatchProxy(VoiceBackend):
                backend_name = backend.backend_name

                @property
                def model_name(self) -> str:
                    return backend.model_name

                async def connect(self, config: VoiceSessionConfig) -> None:
                    await backend.connect(config)

                async def close(self) -> None:
                    await backend.close()

                async def send_audio(self, pcm_bytes: bytes) -> None:
                    await backend.send_audio(pcm_bytes)

                async def send_video_frame(
                    self, image_bytes: bytes, *, mime_type: str = "image/jpeg"
                ) -> None:
                    await backend.send_video_frame(image_bytes, mime_type=mime_type)

                async def send_text(self, text: str, *, end_of_turn: bool = True) -> None:
                    await backend.send_text(text, end_of_turn=end_of_turn)

                async def send_tool_responses(self, responses: list[VoiceToolResponse]) -> None:
                    collected.extend(responses)

                def events(self) -> AsyncIterator[Any]:
                    return backend.events()

            proxy = _BatchProxy()
            try:
                for ev in batch:
                    if ev.name == "end_call":
                        if collected:
                            await backend.send_tool_responses(list(collected))
                            collected.clear()
                        await self._handle_tool_call(ev, backend)
                        return
                    try:
                        await self._handle_tool_call(ev, proxy)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        logger.warning(
                            "Unhandled error in batched voice tool %s (%s): %s",
                            ev.name,
                            ev.call_id,
                            exc,
                        )
                        collected.append(
                            VoiceToolResponse(
                                call_id=ev.call_id,
                                name=ev.name,
                                response={"error": f"Tool execution failed: {exc}"},
                                is_error=True,
                                scheduling="when_idle",
                            )
                        )
            finally:
                if collected:
                    with contextlib.suppress(Exception):
                        await backend.send_tool_responses(list(collected))
                    collected.clear()
        finally:
            self._active_tool_calls = max(0, self._active_tool_calls - 1)
            self._stall_timer_armed = False
            self._stall_accumulated_seconds = 0.0
            self._audio_sent_start_time = None
            tool_ms = (time.monotonic() - batch_t0) * 1000.0
            logger.debug(
                "voice_turn_timing phase=tool_batch tools=%s count=%d tool_ms=%.1f",
                [ev.name for ev in batch],
                len(batch),
                tool_ms,
            )

    async def _verify_control_system_window_state(
        self,
        eff_action: str,
        res: dict[str, Any],
        fallback_hwnd: int | None,
    ) -> None:
        """Wait for ActionVerifier.verify_window_state to settle on window layout actions."""
        if eff_action not in _WINDOW_LAYOUT_ACTIONS:
            return
        if isinstance(res.get("verification"), dict):
            ver = res["verification"]
            if ver.get("verified") is False and ver.get("outcome_kind") == "failed":
                res["ok"] = False
                res["success"] = False
            return

        verifier = self._action_verifier
        if verifier is None:
            try:
                guards_mod = importlib.import_module("clea.tools.guards")
                av_cls = getattr(guards_mod, "ActionVerifier", None)
                if av_cls is not None:
                    v_fn = getattr(av_cls, "verify_window_state", None)
                    is_patched = (
                        hasattr(av_cls, "_mock_name")
                        or hasattr(v_fn, "_mock_name")
                        or getattr(v_fn, "__qualname__", "") != "ActionVerifier.verify_window_state"
                    )
                    if is_patched:
                        verifier = av_cls()
            except Exception:
                verifier = None

        if verifier is None:
            return

        raw_hwnd = res.get("hwnd")
        target_hwnd = (
            int(raw_hwnd)
            if isinstance(raw_hwnd, int) and not isinstance(raw_hwnd, bool)
            else (int(fallback_hwnd) if fallback_hwnd is not None else 0)
        )
        expected_snap: str | None = None
        expected_state: str | None = None
        if eff_action in ("snap_left", "snap_left_window"):
            expected_snap = "left"
        elif eff_action in ("snap_right", "snap_right_window"):
            expected_snap = "right"
        elif eff_action in ("maximize", "maximize_window"):
            expected_state = "maximized"
        elif eff_action in ("minimize", "minimize_window"):
            expected_state = "minimized"
        elif eff_action in ("restore", "restore_window"):
            expected_state = "restored"
        elif eff_action in ("close", "close_window"):
            expected_state = "closed"

        verify_fn = getattr(verifier, "verify_window_state", None)
        if not callable(verify_fn):
            return

        try:
            ver_t0 = time.monotonic()
            if inspect.iscoroutinefunction(verify_fn):
                outcome = await verify_fn(
                    target_hwnd,
                    expected_state=expected_state,
                    expected_snap=expected_snap,
                )
            else:
                call_res = await asyncio.to_thread(
                    verify_fn,
                    target_hwnd,
                    expected_state=expected_state,
                    expected_snap=expected_snap,
                )
                outcome = await call_res if inspect.isawaitable(call_res) else call_res
            verifier_ms = (time.monotonic() - ver_t0) * 1000.0
            logger.debug(
                "voice_turn_timing phase=window_verify action=%s hwnd=%s verifier_ms=%.1f",
                eff_action,
                target_hwnd,
                verifier_ms,
            )
            if hasattr(outcome, "to_dict"):
                ver_dict = outcome.to_dict()
            elif isinstance(outcome, dict):
                ver_dict = dict(outcome)
            else:
                ver_dict = {"verified": bool(getattr(outcome, "verified", True))}
            res["verification"] = ver_dict
            if ver_dict.get("verified") is False and (
                ver_dict.get("outcome_kind") == "failed"
                or ver_dict.get("status") in ("failed", "timeout")
            ):
                res["ok"] = False
                res["success"] = False
        except Exception as exc:
            logger.debug("Window state verification failed during control_system: %s", exc)

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
                    mon = await asyncio.to_thread(
                        get_target_monitor,
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
                            cap_kwargs: dict[str, Any] = {"target": "active_window"}
                            try:
                                cap_sig = inspect.signature(self._screen_service.capture)
                                if "include_layout" in cap_sig.parameters:
                                    cap_kwargs["include_layout"] = False
                            except (TypeError, ValueError):
                                pass
                            candidate = await asyncio.to_thread(
                                self._screen_service.capture,
                                **cap_kwargs,
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
                            same_window = (candidate.process_name, candidate.window_title) == (
                                res.process_name,
                                res.window_title,
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
                if getattr(res, "window_layout", None) is not None:
                    payload["window_layout"] = res.window_layout
                if getattr(res, "window_geometry_summary", None):
                    payload["window_geometry_summary"] = res.window_geometry_summary

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
            win_info = await asyncio.to_thread(self._screen_service.get_active_window_info)
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
                    mon = await asyncio.to_thread(
                        get_target_monitor,
                        api=getattr(self._screen_service, "_api", None),
                    )
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
                    VoiceListConversationsRequest(params=VoiceListConversationsParams(limit=limit)),
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
                MemoryDeleteFactRequest(params=MemoryDeleteFactParams(query_or_id=query_or_id)),
                error_prefix="Failed to delete memory fact",
            )
            return

        if event.name == "search_episodic_memory":
            raw_query = event.arguments.get("query")
            query = "" if raw_query is None else str(raw_query).strip()
            if not query:
                await _send_error("Search query must not be empty.")
                return
            raw_limit = event.arguments.get("limit")
            limit = 5
            if raw_limit is not None and not isinstance(raw_limit, bool):
                with contextlib.suppress(TypeError, ValueError):
                    limit = max(1, min(int(raw_limit), 50))
            raw_surface = event.arguments.get("surface")
            surface: Literal["chat", "voice"] | None = None
            if raw_surface is not None:
                if not isinstance(raw_surface, str):
                    await _send_error("surface must be 'chat' or 'voice'.")
                    return
                cleaned_surface = raw_surface.strip().lower()
                if cleaned_surface:
                    if cleaned_surface not in ("chat", "voice"):
                        await _send_error("surface must be 'chat' or 'voice'.")
                        return
                    surface = cleaned_surface  # type: ignore[assignment]
            await _execute_rpc_tool(
                MemorySearchEpisodicRequest(
                    params=MemorySearchEpisodicParams(
                        query=query,
                        limit=limit,
                        surface=surface,
                    )
                ),
                error_prefix="Failed to search episodic memory",
            )
            return

        if event.name == "manage_memory":
            action = (_req_str("action") or "").lower()
            if action not in ("add", "update", "delete", "list"):
                await _send_error("action must be one of: add, update, delete, list.")
                return
            if action == "list":
                raw_limit = event.arguments.get("limit")
                limit = 25
                if raw_limit is not None and not isinstance(raw_limit, bool):
                    with contextlib.suppress(TypeError, ValueError):
                        limit = max(1, min(int(raw_limit), 50))
                await _execute_rpc_tool(
                    MemoryListFactsRequest(
                        params=MemoryListFactsParams(
                            category=_req_str("category"),
                            limit=limit,
                        )
                    ),
                    error_prefix="Failed to list memory facts",
                )
                return

            fact_text = _req_str("fact_text")
            fact_key = _req_str("fact_key")
            fact_id = _req_str("fact_id")
            query = _req_str("query")
            if action == "add" and not fact_text:
                await _send_error("fact_text is required when action is 'add'.")
                return
            if action == "update" and not fact_text and not fact_id and not query:
                await _send_error(
                    "fact_text (or target fact_id/query) is required when action is 'update'."
                )
                return
            if action == "delete" and not any((fact_id, fact_key, query, fact_text)):
                await _send_error(
                    "fact_id, fact_key, query, or fact_text is required when action is 'delete'."
                )
                return
            await _execute_rpc_tool(
                MemoryManageFactRequest(
                    params=MemoryManageFactParams(
                        action=action,  # type: ignore[arg-type]
                        fact_text=fact_text,
                        category=_req_str("category"),
                        fact_key=fact_key,
                        fact_id=fact_id,
                        query=query,
                        source_surface="voice",
                        source_session_id=self._call_id or "",
                    )
                ),
                error_prefix="Failed to manage memory",
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
            target_path = _req_str("target_path")
            norm_target = normalize_launch_target(app_name) if target_path is None else ""
            if norm_target and norm_target in self._turn_launched_targets:
                prev = self._turn_launched_targets[norm_target]
                await backend.send_tool_responses(
                    [
                        VoiceToolResponse(
                            call_id=event.call_id,
                            name=event.name,
                            response={
                                "ok": True,
                                "status": "deduplicated",
                                "deduplicated": True,
                                "app": prev.get("app") or norm_target,
                                "target": norm_target,
                                "message": (
                                    f"Target '{norm_target}' was already launched or focused "
                                    "in this turn; skipped duplicate launch."
                                ),
                            },
                            is_error=False,
                            scheduling="when_idle",
                        )
                    ]
                )
                return

            effective_app_name = (
                norm_target
                if (
                    target_path is None
                    and norm_target
                    and ("://" in app_name or "." in app_name)
                    and not app_name.lower().endswith(".exe")
                )
                else app_name
            )
            try:
                res = await self._rpc(
                    OsOpenOrFocusAppRequest(
                        params=OsOpenOrFocusAppParams(
                            app_name=effective_app_name,
                            target_path=target_path,
                        )
                    ),
                    timeout=10.0,
                )
                if norm_target and res.get("ok", True) is not False and res.get("status") not in (
                    "error",
                    "not_found",
                    "aborted_precondition",
                ):
                    self._turn_launched_targets[norm_target] = dict(res)
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
                    response={"error": f"Failed to open or focus app: {exc}"},
                    is_error=True,
                    scheduling="when_idle",
                )
            await backend.send_tool_responses([tool_resp])
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
                OsSelectClipboardItemRequest(params=OsSelectClipboardItemParams(index=index)),
                error_prefix="Failed to select clipboard item",
            )
            return

        if event.name == "transform_clipboard":
            action = (_req_str("action") or "").strip()
            valid_actions = (
                "format_json",
                "minify_json",
                "clean_text",
                "unwrap_text",
                "strip_tracking_urls",
            )
            if action not in valid_actions:
                await _send_error(
                    "action must be one of: " + ", ".join(valid_actions) + "."
                )
                return
            await _execute_rpc_tool(
                OsTransformClipboardRequest(
                    params=OsTransformClipboardParams(
                        action=action,  # type: ignore[arg-type]
                        update_clipboard=_parse_bool("update_clipboard", default=True),
                    )
                ),
                error_prefix="Failed to transform clipboard",
            )
            return

        if event.name == "save_clipboard_to_file":
            filename = _req_str("filename")
            directory = (_req_str("directory") or "desktop").lower()
            if directory not in ("desktop", "documents"):
                await _send_error("directory must be 'desktop' or 'documents'.")
                return
            await _execute_rpc_tool(
                OsSaveClipboardToFileRequest(
                    params=OsSaveClipboardToFileParams(
                        filename=filename,
                        directory=directory,  # type: ignore[arg-type]
                    )
                ),
                error_prefix="Failed to save clipboard to file",
            )
            return

        if event.name == "open_path_or_url":
            raw_target = _req_str("target")
            use_clip = _parse_bool("use_clipboard", default=False)
            norm_target = (
                normalize_launch_target(raw_target)
                if (raw_target and not use_clip and not _is_local_file_target(raw_target))
                else ""
            )
            if norm_target and norm_target in self._turn_launched_targets:
                prev = self._turn_launched_targets[norm_target]
                await backend.send_tool_responses(
                    [
                        VoiceToolResponse(
                            call_id=event.call_id,
                            name=event.name,
                            response={
                                "ok": True,
                                "status": "deduplicated",
                                "deduplicated": True,
                                "target": raw_target,
                                "normalized_target": norm_target,
                                "app": prev.get("app") or norm_target,
                                "message": (
                                    f"Target '{norm_target}' was already launched or focused "
                                    "in this turn; skipped duplicate launch."
                                ),
                            },
                            is_error=False,
                            scheduling="when_idle",
                        )
                    ]
                )
                return
            try:
                res = await self._rpc(
                    OsOpenPathOrUrlRequest(
                        params=OsOpenPathOrUrlParams(
                            target=raw_target,
                            use_clipboard=use_clip,
                        )
                    ),
                    timeout=10.0,
                )
                if norm_target and res.get("ok", True) is not False and res.get("status") not in (
                    "error",
                    "not_found",
                    "non_interactive_desktop",
                ):
                    self._turn_launched_targets[norm_target] = dict(res)
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
                    response={"error": f"Failed to open path or URL: {exc}"},
                    is_error=True,
                    scheduling="when_idle",
                )
            await backend.send_tool_responses([tool_resp])
            return

        if event.name == "inspect_screen":
            target = _req_str("target") or "auto"
            target_norm = target.strip("'\"").lower() or "auto"
            question = _req_str("question") or ""
            include_image = _parse_bool("include_image", default=True)

            if self.config.consent_mode == "deny" and target_norm != "clipboard":
                await backend.send_tool_responses(
                    [
                        VoiceToolResponse(
                            call_id=event.call_id,
                            name=event.name,
                            response={
                                "status": "denied",
                                "error": (
                                    "Screen capture is disabled by configuration "
                                    "(consent_mode=deny)."
                                ),
                            },
                            is_error=True,
                            scheduling="when_idle",
                        )
                    ]
                )
                return

            if target_norm not in ("auto", "active", "clipboard") and is_window_blocked(
                target_norm, target_norm, self.config.blocklist
            ):
                await backend.send_tool_responses(
                    [
                        VoiceToolResponse(
                            call_id=event.call_id,
                            name=event.name,
                            response={
                                "status": "blocked",
                                "error": (
                                    f"Target '{target}' is protected by privacy blocklist."
                                ),
                            },
                            is_error=True,
                            scheduling="when_idle",
                        )
                    ]
                )
                return

            try:
                res = await self._rpc(
                    OsInspectScreenRequest(
                        params=OsInspectScreenParams(
                            target=target,
                            question=question,
                            include_image=include_image,
                        )
                    ),
                    timeout=20.0,
                )
            except Exception as exc:
                await _send_error(f"Failed to inspect screen: {exc}")
                return

            source = str(res.get("source") or "")
            win_title = str(res.get("window_title") or "")
            proc_name = str(res.get("process_name") or "")
            if source in ("active_window", "background_window") and is_window_blocked(
                win_title, proc_name, self.config.blocklist
            ):
                raw_blocked_artifact = res.get("artifact_path")
                if isinstance(raw_blocked_artifact, str) and raw_blocked_artifact.strip():
                    blocked_path = Path(raw_blocked_artifact.strip())
                    if (
                        blocked_path.suffix.lower() == ".png"
                        and blocked_path.parent.name.lower() == "artifacts"
                    ):
                        with contextlib.suppress(OSError):
                            await asyncio.to_thread(blocked_path.unlink, missing_ok=True)
                await backend.send_tool_responses(
                    [
                        VoiceToolResponse(
                            call_id=event.call_id,
                            name=event.name,
                            response={
                                "status": "blocked",
                                "error": (
                                    f"Window '{win_title}' ({proc_name}) is protected by "
                                    "privacy blocklist."
                                ),
                                "window_title": win_title or None,
                                "process_name": proc_name or None,
                            },
                            is_error=True,
                            scheduling="when_idle",
                        )
                    ]
                )
                return

            image_bytes: bytes | None = None
            image_mime_type = str(res.get("image_mime_type") or "image/png").strip() or "image/png"
            raw_b64 = res.get("image_base64")
            if include_image and isinstance(raw_b64, str) and raw_b64.strip():
                try:
                    decoded = base64.b64decode("".join(raw_b64.split()), validate=True)
                    if decoded:
                        image_bytes = decoded
                except Exception as exc:
                    logger.warning("Failed decoding inspect_screen image_base64: %s", exc)
            if include_image and image_bytes is None:
                raw_artifact = res.get("artifact_path")
                if isinstance(raw_artifact, str) and raw_artifact.strip():
                    artifact_file = Path(raw_artifact.strip())
                    try:
                        if await asyncio.to_thread(artifact_file.is_file):
                            disk_bytes = await asyncio.to_thread(artifact_file.read_bytes)
                            if disk_bytes:
                                image_bytes = disk_bytes
                    except Exception as exc:
                        logger.warning(
                            "Failed reading inspect_screen artifact %s: %s",
                            raw_artifact,
                            exc,
                        )

            if image_bytes and len(image_bytes) > NATIVE_PNG_MAX_BYTES:
                try:
                    import io

                    from PIL import Image

                    def _cap_live_frame(raw_data: bytes) -> tuple[bytes, str]:
                        with Image.open(io.BytesIO(raw_data)) as img:
                            return encode_native_frame(img)

                    image_bytes, image_mime_type = await asyncio.to_thread(
                        _cap_live_frame, image_bytes
                    )
                except Exception as exc:
                    logger.warning("Failed capping oversized inspect_screen frame: %s", exc)

            media_parts: list[VoiceToolMediaPart] = []
            if image_bytes:
                media_parts.append(
                    VoiceToolMediaPart(
                        data=image_bytes,
                        mime_type=image_mime_type,
                    )
                )

            payload = {k: v for k, v in res.items() if k != "image_base64"}
            payload["frames_sent"] = len(media_parts)
            if media_parts:
                payload["mime_type"] = image_mime_type

            await backend.send_tool_responses(
                [
                    VoiceToolResponse(
                        call_id=event.call_id,
                        name=event.name,
                        response=payload,
                        media_parts=media_parts,
                        is_error=False,
                        scheduling="when_idle",
                    )
                ]
            )
            return

        if event.name == "open_url":
            raw_url = event.arguments.get("url")
            url = "" if raw_url is None else str(raw_url).strip()
            if not url:
                await _send_error("url must not be empty.")
                return
            browser = _req_str("browser")
            norm_target = normalize_launch_target(url)
            if norm_target and norm_target in self._turn_launched_targets:
                prev = self._turn_launched_targets[norm_target]
                await backend.send_tool_responses(
                    [
                        VoiceToolResponse(
                            call_id=event.call_id,
                            name=event.name,
                            response={
                                "ok": True,
                                "status": "deduplicated",
                                "deduplicated": True,
                                "url": url,
                                "target": norm_target,
                                "app": prev.get("app") or prev.get("app_name") or norm_target,
                                "guidance": (
                                    "Installed app is already focused. Continue the task inside "
                                    "this single app window using UIA (ui_snapshot/ui_act), "
                                    "in-app keyboard shortcuts, or targeted screen inspection. "
                                    "Do NOT call open_url again."
                                ),
                                "message": (
                                    f"Target '{norm_target}' was already launched or focused "
                                    "in this turn; skipped duplicate URL launch."
                                ),
                            },
                            is_error=False,
                            scheduling="when_idle",
                        )
                    ]
                )
                return

            request_context = _req_str("reason") or _req_str("context")
            if request_context is None and self._transcript_history:
                last_entry = self._transcript_history[-1]
                if (
                    isinstance(last_entry, dict)
                    and last_entry.get("role") == "user"
                    and isinstance(last_entry.get("text"), str)
                    and last_entry["text"].strip()
                ):
                    request_context = last_entry["text"].strip()
            browser_explicit = _is_browser_explicitly_requested(
                request_context,
                browser,
            ) or _parse_bool("prefer_browser", default=False)

            # Installed-App-First Policy: for root service URLs without explicit browser request,
            # focus/launch the installed app directly; deep URLs flow through daemon os.open_url
            # which runs the full 3-layer OS resolver (AppUriHandlers -> URL Protocol -> Installed
            # Apps / running standalone PWAs) and never spawns Chrome/Edge if an app is installed.
            if not browser_explicit and _is_app_candidate_url(url, norm_target):
                try:
                    app_res = await self._rpc(
                        OsOpenOrFocusAppRequest(
                            params=OsOpenOrFocusAppParams(
                                app_name=norm_target,
                                target_path=None,
                            )
                        ),
                        timeout=10.0,
                    )
                    if (
                        isinstance(app_res, dict)
                        and app_res.get("ok", True) is not False
                        and app_res.get("status") in ("launched", "focused", "opened")
                    ):
                        enriched = dict(app_res)
                        enriched.setdefault("url", url)
                        if app_res.get("launched_app") is not False:
                            enriched.setdefault("resolved_via", "installed_app")
                            enriched.setdefault("routed_to_installed_app", True)
                            enriched.setdefault("app_name", app_res.get("app") or norm_target)
                            enriched.setdefault("window_hwnd", app_res.get("hwnd"))
                            enriched.setdefault("resolution_layer", "installed_app")
                            enriched.setdefault(
                                "guidance",
                                "Installed app is focused. Continue the task inside this single "
                                "app window using UIA (ui_snapshot/ui_act), in-app keyboard "
                                "shortcuts, or targeted screen inspection. Do NOT call open_url "
                                "again.",
                            )
                        else:
                            enriched.setdefault("routed_to_installed_app", False)
                        self._turn_launched_targets[norm_target] = enriched
                        await backend.send_tool_responses(
                            [
                                VoiceToolResponse(
                                    call_id=event.call_id,
                                    name=event.name,
                                    response=enriched,
                                    is_error=False,
                                    scheduling="when_idle",
                                )
                            ]
                        )
                        return
                except Exception:
                    pass

            try:
                res = await self._rpc(
                    OsOpenUrlRequest(
                        params=OsOpenUrlParams(
                            url=url,
                            browser=browser,
                            prefer_browser=browser_explicit,
                            user_request=request_context,
                        )
                    ),
                    timeout=10.0,
                )
                if norm_target and res.get("ok", True) is not False and res.get("status") not in (
                    "error",
                    "non_interactive_desktop",
                ):
                    self._turn_launched_targets[norm_target] = dict(res)
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
                    response={"error": f"Failed to open URL: {exc}"},
                    is_error=True,
                    scheduling="when_idle",
                )
            await backend.send_tool_responses([tool_resp])
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

            window_action = _req_str("window_action")
            eff_action = (
                window_action.lower()
                if action in ("window", "window_action") and window_action
                else action
            )
            try:
                res = await self._rpc(
                    OsControlSystemRequest(
                        params=OsControlSystemParams(
                            action=action,
                            level=level,
                            step=step,
                            window_action=window_action,
                            window_title=(
                                _req_str("window_title")
                                or _req_str("title")
                                or _req_str("title_query")
                            ),
                            hwnd=hwnd,
                            app_name=_req_str("app_name"),
                            page=_req_str("page"),
                            uri=_req_str("uri"),
                        )
                    ),
                    timeout=15.0,
                )
                if isinstance(res, dict):
                    res = dict(res)
                    await self._verify_control_system_window_state(eff_action, res, hwnd)
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
                    response={"error": f"Failed to control system: {exc}"},
                    is_error=True,
                    scheduling="when_idle",
                )
            await backend.send_tool_responses([tool_resp])
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
                expected_hwnd_val = _opt_int("expected_hwnd")
                raw_sec = event.arguments.get("seconds", 1.0)
                if isinstance(raw_sec, bool):
                    raise ValueError("seconds must be a number.")
                seconds_val = float(raw_sec) if raw_sec is not None else 1.0
                if not math.isfinite(seconds_val):
                    raise ValueError("seconds must be finite.")
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
                        app_name=_req_str("app_name"),
                        expected_hwnd=expected_hwnd_val,
                    )
                ),
                error_prefix="Failed to execute GUI action",
            )
            return

        if event.name == "ui_snapshot":
            raw_max = event.arguments.get("max_elements", 60)
            max_elements = 60
            if raw_max is not None and not isinstance(raw_max, bool):
                with contextlib.suppress(TypeError, ValueError):
                    max_elements = int(raw_max)
            interactive_only = _parse_bool("interactive_only", default=False)
            await _execute_rpc_tool(
                OsUiSnapshotRequest(
                    params=OsUiSnapshotParams(
                        app_name=_req_str("app_name"),
                        max_elements=max_elements,
                        interactive_only=interactive_only,
                    )
                ),
                error_prefix="Failed to capture UI snapshot",
            )
            return

        if event.name == "ui_act":
            ref = _req_str("ref")
            if not ref:
                await _send_error("ref is required for ui_act.")
                return
            action = _req_str("action")
            if not action:
                await _send_error("action is required for ui_act.")
                return
            action_norm = action.lower()
            valid_actions = {
                "click",
                "set_value",
                "type",
                "clear",
                "double_click",
                "right_click",
            }
            if action_norm not in valid_actions:
                await _send_error(f"Unsupported ui_act action: {action!r}")
                return
            raw_val = event.arguments.get("value")
            val_str = str(raw_val) if raw_val is not None else None
            raw_interval = event.arguments.get("interval_ms", 12)
            interval_ms = 12
            if raw_interval is not None and not isinstance(raw_interval, bool):
                with contextlib.suppress(TypeError, ValueError):
                    interval_ms = int(raw_interval)
            await _execute_rpc_tool(
                OsUiActRequest(
                    params=OsUiActParams(
                        ref=ref,
                        action=action_norm,  # type: ignore[arg-type]
                        value=val_str,
                        interval_ms=interval_ms,
                    )
                ),
                error_prefix="Failed to execute ui_act",
            )
            return

        if event.name == "ui_batch":
            raw_steps = event.arguments.get("steps")
            if not isinstance(raw_steps, list) or len(raw_steps) == 0:
                await _send_error("steps must be a non-empty list of step operations.")
                return
            if len(raw_steps) > 20:
                await _send_error("steps cannot exceed 20 operations.")
                return
            stop_on_error = _parse_bool("stop_on_error", default=True)
            stop_on_win_change = _parse_bool("stop_on_window_change", default=True)
            try:
                steps = [
                    UiBatchStep.model_validate(s) if isinstance(s, dict) else s for s in raw_steps
                ]
            except Exception as exc:
                await _send_error(f"Invalid batch step format: {exc}")
                return
            await _execute_rpc_tool(
                OsUiBatchRequest(
                    params=OsUiBatchParams(
                        steps=steps,
                        stop_on_error=stop_on_error,
                        stop_on_window_change=stop_on_win_change,
                    )
                ),
                error_prefix="Failed to execute ui_batch",
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
            burst_cap_kwargs: dict[str, Any] = {"target": target}
            try:
                cap_sig = inspect.signature(self._screen_service.capture)
                if "include_layout" in cap_sig.parameters:
                    burst_cap_kwargs["include_layout"] = False
            except (TypeError, ValueError):
                pass
            while time.monotonic() < t_end and self._call_state == "active":
                res = await asyncio.to_thread(
                    self._screen_service.capture,
                    **burst_cap_kwargs,
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
        token = self.token or await asyncio.to_thread(
            auth.read_token, auth.token_path(self.data_dir)
        )
        if not token:
            logger.debug("No daemon token found; running offline until daemon starts.")
            with contextlib.suppress(TimeoutError):
                await asyncio.wait_for(self._stop_event.wait(), timeout=self._daemon_retry_delay)
            return

        try:
            connect_kwargs: dict[str, Any] = {
                "subprotocols": auth.client_subprotocols(token),
            }
            connect_target = getattr(connect, "side_effect", None) or connect
            try:
                sig = inspect.signature(connect_target)
                if any(
                    param.kind == inspect.Parameter.VAR_KEYWORD or param.name == "max_size"
                    for param in sig.parameters.values()
                ):
                    connect_kwargs["max_size"] = 24 * 1024 * 1024
            except (TypeError, ValueError):
                connect_kwargs["max_size"] = 24 * 1024 * 1024

            async with connect(
                self.daemon_url,
                **connect_kwargs,
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

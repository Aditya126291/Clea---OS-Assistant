"""Localhost-only WebSocket daemon owning the agent loop, tools, and memory."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import logging.handlers
import os
import subprocess
import time
from collections.abc import Awaitable, Coroutine, Iterable
from datetime import UTC, datetime
from http import HTTPStatus
from pathlib import Path
from typing import Any

from pydantic import ValidationError
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.datastructures import Headers
from websockets.exceptions import ConnectionClosed
from websockets.http11 import Request, Response

from clea.core import auth
from clea.core.agent import AgentLoop
from clea.core.protocol import (
    METHOD_RESULTS,
    ApprovalDecision,
    ApprovalRequestedData,
    ApprovalRequestedEvent,
    ApprovalResolvedData,
    ApprovalResolvedEvent,
    AssistantTokenData,
    AssistantTokenEvent,
    BaseRequest,
    CallEndedData,
    CallEndedEvent,
    CallStartedData,
    CallStartedEvent,
    CallState,
    ConsolidationSessionStatus,
    ConversationCreatedData,
    ConversationCreatedEvent,
    ConversationDeletedData,
    ConversationDeletedEvent,
    ConversationResumeMessage,
    ConversationSearchHit,
    ConversationUpdatedData,
    ConversationUpdatedEvent,
    EpisodeItem,
    ErrorData,
    ErrorEvent,
    MemoryArchiveItem,
    MemoryFactItem,
    MemorySearchHit,
    MemorySectionItem,
    MemoryUpdatedData,
    MemoryUpdatedEvent,
    PermissionTierInfo,
    ResponseFrame,
    ScreenCaptureTarget,
    SettingsGetResult,
    TaskStatusData,
    TaskStatusEvent,
    ToolCallFinishedData,
    ToolCallFinishedEvent,
    ToolCallStartedData,
    ToolCallStartedEvent,
    ToolInfo,
    TranscriptMessageItem,
    TranscriptSummaryItem,
    UiNavigateData,
    UiNavigateEvent,
    VoiceConversationListItem,
    VoiceConversationTurnItem,
    VoiceHistoryMessageItem,
    VoiceState,
    VoiceStateData,
    VoiceStateEvent,
    new_id,
    parse_frame,
)
from clea.core.sessions import Session, SessionManager
from clea.memory.consolidation import ConsolidationManager
from clea.memory.store import FIXED_SECTIONS, VOICE_COMPANION_SESSION_ID, MemoryStore
from clea.providers.base import LLMProvider, Message, ToolCall
from clea.providers.gemini import GeminiProvider
from clea.tools import (
    RiskLevel,
    ToolRegistry,
    register_builtin_tools,
    register_memory_tools,
    requires_approval,
)
from clea.tools.git_workspace import inspect_git_workspace
from clea.tools.registry import ToolDefinition
from clea.tools.windows_os import (
    control_media_volume,
    open_or_focus_app,
    read_clipboard,
    write_clipboard,
)

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
DEFAULT_APPROVAL_TIMEOUT_SECONDS = 120.0
_LOCALHOST_ONLY = {"127.0.0.1", "localhost", "::1"}

ServerFrameOut = Any  # any ResponseFrame or event model; all are pydantic models


def _validate_localhost(host: str) -> str:
    if host not in _LOCALHOST_ONLY:
        raise ValueError(
            f"Refusing to bind Clea daemon to non-localhost address '{host}'. Use 127.0.0.1 only."
        )
    return host


def _now_iso() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds")


_MEMORY_CATEGORY_ALIASES: dict[str, str] = {
    "profile": "Profile",
    "personal": "Profile",
    "user": "Profile",
    "identity": "Profile",
    "preference": "Preferences",
    "preferences": "Preferences",
    "pref": "Preferences",
    "prefs": "Preferences",
    "environment": "Environment",
    "env": "Environment",
    "setup": "Environment",
    "system": "Environment",
    "tools": "Environment",
    "project": "Projects",
    "projects": "Projects",
    "decision": "Decisions",
    "decisions": "Decisions",
    "open thread": "Open Threads",
    "open threads": "Open Threads",
    "open_thread": "Open Threads",
    "open_threads": "Open Threads",
    "thread": "Open Threads",
    "threads": "Open Threads",
    "todo": "Open Threads",
}


def _resolve_memory_section(category: str | None) -> str:
    """Resolve an arbitrary category string into one of MemoryStore FIXED_SECTIONS."""
    if not category or not category.strip():
        return "Preferences"
    norm = " ".join(category.strip().lower().replace("-", " ").split())
    if norm in _MEMORY_CATEGORY_ALIASES:
        return _MEMORY_CATEGORY_ALIASES[norm]
    for sec in FIXED_SECTIONS:
        if sec.lower() == norm:
            return sec
    return "Preferences"


class _Connection:
    """Per-WebSocket state: running chat turns and pending approvals."""

    def __init__(self, ws: ServerConnection) -> None:
        self.ws = ws
        self.tasks: set[asyncio.Task[None]] = set()
        self.approvals: dict[str, asyncio.Future[ApprovalDecision]] = {}
        self.current_session_id: str | None = None
        self.is_voice: bool = False

    def spawn(self, coro: Awaitable[None]) -> None:
        task = asyncio.ensure_future(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)

    async def close(self) -> None:
        """Cancel in-flight turns. Pending approvals die with them, so nothing executes."""
        tasks = list(self.tasks)
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)
        self.approvals.clear()


class _TurnState:
    """Timing bookkeeping for one chat turn's tool calls."""

    def __init__(self) -> None:
        self.current_call_id: str | None = None
        self.started: dict[str, float] = {}
        self.approval_wait: dict[str, float] = {}


class CleaDaemon:
    """WebSocket server exposing the typed protocol on 127.0.0.1."""

    def __init__(
        self,
        *,
        agent: AgentLoop,
        sessions: SessionManager | None = None,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
        token: str | None = None,
        token_file: Path | None = None,
        lock_file: Path | None = None,
        allowed_origins: Iterable[str] | None = None,
        approval_timeout: float = DEFAULT_APPROVAL_TIMEOUT_SECONDS,
    ) -> None:
        self.host = _validate_localhost(host)
        self.port = port
        self.agent = agent
        self.sessions = sessions or SessionManager()
        self.token = token or auth.generate_token()
        self.token_file = token_file or auth.token_path(agent.memory.base_dir)
        self.lock_file = lock_file or auth.lock_path(agent.memory.base_dir)
        self._instance_lock = auth.InstanceLock(self.lock_file)
        self.allowed_origins = (
            auth.normalize_origins(allowed_origins)
            if allowed_origins is not None
            else auth.allowed_origins_from_env()
        )
        self.approval_timeout = approval_timeout
        self._server: Server | None = None
        self._busy_sessions: set[str] = set()
        self._connections: set[_Connection] = set()
        self._shared_approvals: dict[str, asyncio.Future[ApprovalDecision]] = {}
        self._active_conversation_id: str | None = None
        self._voice_call_id: str | None = None
        self._call_state: CallState = "idle"
        self._call_started_monotonic: float | None = None
        self._voice_state: VoiceState = "idle"
        self._screen_capture_active: bool = False
        self._screen_capture_target: ScreenCaptureTarget | None = None
        try:
            min_turns = int(os.environ.get("CLEA_CONSOLIDATION_MIN_TURNS", "2"))
        except ValueError:
            min_turns = 2
        try:
            consolidation_timeout = float(
                os.environ.get("CLEA_CONSOLIDATION_TIMEOUT_SECONDS", "60.0")
            )
        except ValueError:
            consolidation_timeout = 60.0
        self.consolidation = ConsolidationManager(
            memory=agent.memory,
            provider=agent.provider,
            min_user_turns=min_turns,
            llm_timeout_seconds=consolidation_timeout,
        )
        self.consolidation_idle_seconds = 900.0
        self._idle_tasks: dict[str, asyncio.Task[None]] = {}
        self._consolidation_tasks: dict[str, asyncio.Task[None]] = {}
        self._consolidation_in_flight: set[str] = set()
        self._background_tasks: set[asyncio.Task[Any]] = set()
        self._active_task_handles: dict[str, asyncio.Task[Any]] = {}
        self._task_metadata: dict[str, dict[str, Any]] = {}
        self._cancelled_task_ids: set[str] = set()
        self._task_started_at: dict[str, str] = {}
        self._consolidation_task_terminal: dict[str, bool] = {}
        self._ended_voice_call_ids: set[str] = set()
        self._startup_task: asyncio.Task[None] | None = None

    def _spawn_background_task(
        self,
        coro: Coroutine[Any, Any, Any],
        *,
        name: str | None = None,
    ) -> asyncio.Task[Any]:
        """Spawn a background asyncio.Task while retaining a strong reference until done."""
        task = asyncio.create_task(coro, name=name)
        self._background_tasks.add(task)

        def _on_done(done_task: asyncio.Task[Any]) -> None:
            self._background_tasks.discard(done_task)
            if done_task.cancelled():
                return
            exc = done_task.exception()
            if exc is not None:
                logger.error(
                    "Unhandled exception in background task %s",
                    done_task.get_name(),
                    exc_info=exc,
                )

        task.add_done_callback(_on_done)
        return task

    def _reconcile_orphaned_tasks(self) -> list[dict[str, Any]]:
        """Reconcile orphaned 'running' Activity/consolidation tasks from prior runs."""
        reconciled_tasks = self.agent.memory.reconcile_orphaned_background_tasks(
            reason="Interrupted by daemon restart"
        )
        reconciled_cons = self.consolidation.reconcile_orphaned_running(
            reason="Interrupted by daemon restart"
        )
        if reconciled_tasks or reconciled_cons:
            logger.warning(
                "Reconciled %d orphaned background task(s) and %d consolidation session(s) "
                "on startup",
                len(reconciled_tasks),
                len(reconciled_cons),
            )
        return reconciled_tasks

    @property
    def bound_port(self) -> int:
        """Return the actual TCP port the daemon is listening on."""
        if self._server and self._server.sockets:
            return int(self._server.sockets[0].getsockname()[1])
        return self.port

    async def start(self) -> None:
        """Acquire single-instance lock, listen on localhost, and write the session token."""
        self.agent.memory.ensure_initialized()
        self.agent.memory.migrate_legacy_daily_logs()
        self.agent.memory.purge_trash()
        self._reconcile_orphaned_tasks()
        self._instance_lock.acquire(host=self.host, port=self.port)
        try:
            self._server = await serve(
                self._handle_connection,
                self.host,
                self.port,
                process_request=self._authenticate,
                select_subprotocol=self._select_subprotocol,
            )
            self._instance_lock.update_port(host=self.host, port=self.bound_port)
            auth.write_token(self.token, self.token_file)
            self._startup_task = self._spawn_background_task(
                self._run_startup_consolidation(),
                name="startup-consolidation",
            )
        except Exception:
            if self._server is not None:
                self._server.close()
                await self._server.wait_closed()
                self._server = None
            self._instance_lock.release()
            raise
        logger.info("Clea daemon listening on ws://%s:%d", self.host, self.bound_port)

    async def stop(self) -> None:
        """Gracefully close the WebSocket server and remove the token and lock files."""
        for t in list(self._idle_tasks.values()):
            if not t.done():
                t.cancel()
        vc_task = self._consolidation_tasks.get(VOICE_COMPANION_SESSION_ID)
        flush_timeout = min(self.consolidation.llm_timeout_seconds, 10.0)
        if vc_task is not None and not vc_task.done():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(asyncio.shield(vc_task), timeout=flush_timeout)
        elif self.consolidation.has_unconsolidated_voice_companion():
            with contextlib.suppress(Exception):
                await asyncio.wait_for(
                    self._execute_consolidation(
                        VOICE_COMPANION_SESSION_ID,
                        force=True,
                        log_context="on daemon shutdown",
                    ),
                    timeout=flush_timeout,
                )
        pending_tasks = list(self._background_tasks)
        for t in list(self._idle_tasks.values()):
            if t not in pending_tasks:
                pending_tasks.append(t)
        if self._startup_task is not None and self._startup_task not in pending_tasks:
            pending_tasks.append(self._startup_task)
        for t in pending_tasks:
            if not t.done():
                t.cancel()
        if pending_tasks:
            await asyncio.gather(*pending_tasks, return_exceptions=True)
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        self._idle_tasks.clear()
        self._consolidation_tasks.clear()
        self._consolidation_in_flight.clear()
        self._background_tasks.clear()
        self._active_task_handles.clear()
        self._task_metadata.clear()
        self._cancelled_task_ids.clear()
        with contextlib.suppress(Exception):
            self.agent.memory.reconcile_orphaned_background_tasks(
                reason="Interrupted by daemon shutdown"
            )
        with contextlib.suppress(Exception):
            self.consolidation.reconcile_orphaned_running(
                reason="Interrupted by daemon shutdown"
            )
        auth.remove_token(self.token_file, self.token)
        self._instance_lock.release()

    async def serve_forever(self) -> None:
        """Start the daemon and run until cancelled."""
        await self.start()
        assert self._server is not None
        try:
            await self._server.serve_forever()
        finally:
            await self.stop()

    # ------------------------------------------------------------------ handshake security

    @staticmethod
    def _select_subprotocol(_connection: ServerConnection, offered: Iterable[str]) -> str | None:
        return auth.PROTOCOL_NAME if auth.PROTOCOL_NAME in offered else None

    def _authenticate(self, connection: ServerConnection, request: Request) -> Response | None:
        """Reject the handshake unless Host, Origin and session token all check out."""
        headers: Headers = request.headers

        hosts = headers.get_all("Host")
        if len(hosts) != 1 or not auth.host_header_is_local(hosts[0]):
            logger.warning("Rejected connection: invalid or non-local Host header")
            return connection.respond(HTTPStatus.FORBIDDEN, "Forbidden\n")

        # Browsers always send Origin; non-browser clients (CLI) omit it and rely on the token.
        origins = headers.get_all("Origin")
        if len(origins) > 1:
            logger.warning("Rejected connection: multiple Origin headers")
            return connection.respond(HTTPStatus.FORBIDDEN, "Forbidden\n")
        if origins and origins[0].strip().rstrip("/").lower() not in self.allowed_origins:
            logger.warning("Rejected connection: Origin %r not in allowlist", origins[0])
            return connection.respond(HTTPStatus.FORBIDDEN, "Forbidden\n")

        supplied = auth.extract_token(", ".join(headers.get_all("Sec-WebSocket-Protocol")))
        if not auth.token_matches(self.token, supplied):
            logger.warning("Rejected connection: missing or invalid session token")
            return connection.respond(HTTPStatus.UNAUTHORIZED, "Unauthorized\n")

        return None

    # ------------------------------------------------------------------ transport

    async def _send(self, ws: ServerConnection, frame: ServerFrameOut) -> None:
        await ws.send(frame.model_dump_json())

    async def _broadcast(self, frame: ServerFrameOut) -> None:
        payload = frame.model_dump_json()
        for conn in list(self._connections):
            with contextlib.suppress(ConnectionClosed):
                await conn.ws.send(payload)

    def _canonical_consolidation_session_id(self, session_id: str) -> str:
        """Canonicalize voice call IDs to VOICE_COMPANION_SESSION_ID for Activity & deltas."""
        resolved = self.agent.memory.resolve_session_id(session_id)
        if resolved == VOICE_COMPANION_SESSION_ID:
            return VOICE_COMPANION_SESSION_ID
        if not self.agent.memory.transcript_path(resolved).exists():
            tr = self.agent.memory.get_transcript(session_id)
            if tr and tr.get("is_voice_call"):
                return VOICE_COMPANION_SESSION_ID
        return session_id

    def _format_session_consolidation_title(self, session_id: str) -> str:
        """Generate Activity task title for session consolidation."""
        canonical_sid = self._canonical_consolidation_session_id(session_id)
        if canonical_sid == VOICE_COMPANION_SESSION_ID:
            return "Consolidating voice memory"
        time_str = ""
        try:
            tr = self.agent.memory.get_transcript(canonical_sid)
            if tr and tr.get("started_at"):
                dt = datetime.fromisoformat(tr["started_at"])
                time_str = dt.strftime("%H:%M")
        except Exception:
            pass
        if not time_str:
            time_str = datetime.now().astimezone().strftime("%H:%M")
        return f"Summarising session {time_str}"

    async def _emit_consolidation_task_status(
        self,
        session_id: str,
        status: str,
        detail: str | None = None,
    ) -> None:
        canonical_sid = self._canonical_consolidation_session_id(session_id)
        task_id = f"cons-{canonical_sid}"
        title = self._format_session_consolidation_title(canonical_sid)
        now_iso = _now_iso()
        if status == "running":
            self._task_started_at[task_id] = now_iso
            self._consolidation_task_terminal[task_id] = False
            started_at = now_iso
        else:
            started_at = self._task_started_at.pop(task_id, now_iso)
            self._consolidation_task_terminal[task_id] = True
        finished_at = now_iso if status in ("succeeded", "failed", "cancelled") else None
        with contextlib.suppress(Exception):
            self.agent.memory.record_background_task(
                task_id=task_id,
                title=title,
                status=status,
                started_at=started_at,
                finished_at=finished_at,
                kind="background_task",
                detail=detail,
                session_id=canonical_sid,
            )
        await self._broadcast(
            TaskStatusEvent(
                data=TaskStatusData(
                    task_id=task_id,
                    title=title,
                    status=status,  # type: ignore[arg-type]
                    started_at=started_at,
                    finished_at=finished_at,
                    kind="background_task",
                    detail=detail,
                )
            )
        )

    async def _run_startup_consolidation(self) -> None:
        try:
            await self.consolidation.run_crash_recovery(
                on_task_status=lambda sid, st, det=None: self._emit_consolidation_task_status(
                    sid, st, det
                )
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.exception("Error during startup memory consolidation crash recovery")

    async def _execute_consolidation(
        self,
        session_id: str,
        *,
        force: bool,
        log_context: str,
    ) -> None:
        """Run consolidation for session_id with safe per-invocation terminal status handling."""
        canonical_sid = self._canonical_consolidation_session_id(session_id)
        resolved_sid = self.agent.memory.resolve_session_id(canonical_sid)
        keys_to_mark = {session_id, canonical_sid, resolved_sid}
        self._consolidation_in_flight.update(keys_to_mark)
        task_id = f"cons-{canonical_sid}"
        current = asyncio.current_task()
        if current is not None:
            self._active_task_handles.setdefault(task_id, current)
            self._task_metadata.setdefault(
                task_id,
                {
                    "title": self._format_session_consolidation_title(canonical_sid),
                    "kind": "background_task",
                    "started_at": _now_iso(),
                    "session_id": canonical_sid,
                },
            )
        local_started = False
        local_terminal = False

        async def _on_status(st: str, det: str | None = None) -> None:
            nonlocal local_started, local_terminal
            if st == "running":
                local_started = True
            else:
                local_terminal = True
            await self._emit_consolidation_task_status(canonical_sid, st, det)

        try:
            await self.consolidation.consolidate_session(
                canonical_sid,
                force=force,
                on_task_status=_on_status,
            )
        except asyncio.CancelledError:
            if (
                (local_started and not local_terminal)
                or self._consolidation_task_terminal.get(task_id) is False
            ):
                local_terminal = True
                was_cancelled = task_id in self._cancelled_task_ids
                with contextlib.suppress(Exception):
                    await self._emit_consolidation_task_status(
                        canonical_sid,
                        "cancelled" if was_cancelled else "failed",
                        "Cancelled by user" if was_cancelled else "Cancelled",
                    )
        except Exception as exc:
            logger.exception("Error consolidating session %s %s", canonical_sid, log_context)
            if (
                (local_started and not local_terminal)
                or self._consolidation_task_terminal.get(task_id) is False
            ):
                local_terminal = True
                with contextlib.suppress(Exception):
                    await self._emit_consolidation_task_status(canonical_sid, "failed", str(exc))
        finally:
            if (
                (local_started and not local_terminal)
                or self._consolidation_task_terminal.get(task_id) is False
            ):
                with contextlib.suppress(Exception):
                    await self._emit_consolidation_task_status(
                        canonical_sid, "failed", "Consolidation did not complete cleanly"
                    )
            if self._active_task_handles.get(task_id) is current:
                self._active_task_handles.pop(task_id, None)
                self._task_metadata.pop(task_id, None)
            self._cancelled_task_ids.discard(task_id)
            self._consolidation_in_flight.difference_update(keys_to_mark)
            for key in keys_to_mark:
                if self._consolidation_tasks.get(key) is current:
                    self._consolidation_tasks.pop(key, None)

    def _reset_session_idle_timer(self, session_id: str) -> None:
        old_task = self._idle_tasks.pop(session_id, None)
        if old_task is not None and not old_task.done():
            old_task.cancel()

        async def _timer() -> None:
            try:
                await asyncio.sleep(self.consolidation_idle_seconds)
                if self._idle_tasks.get(session_id) is asyncio.current_task():
                    self._idle_tasks.pop(session_id, None)
                canonical_sid = self._canonical_consolidation_session_id(session_id)
                resolved_sid = self.agent.memory.resolve_session_id(canonical_sid)
                if (
                    session_id in self._consolidation_in_flight
                    or canonical_sid in self._consolidation_in_flight
                    or resolved_sid in self._consolidation_in_flight
                    or self.consolidation.is_in_flight(canonical_sid)
                ):
                    return
                idle_force = canonical_sid != VOICE_COMPANION_SESSION_ID
                if not self.consolidation.should_consolidate(canonical_sid, force=idle_force):
                    return
                current = asyncio.current_task()
                if current is not None:
                    self._consolidation_tasks.setdefault(canonical_sid, current)
                    if resolved_sid and resolved_sid != canonical_sid:
                        self._consolidation_tasks.setdefault(resolved_sid, current)
                await self._execute_consolidation(
                    canonical_sid,
                    force=idle_force,
                    log_context="after idle timeout",
                )
            except asyncio.CancelledError:
                pass
            finally:
                if self._idle_tasks.get(session_id) is asyncio.current_task():
                    self._idle_tasks.pop(session_id, None)

        self._idle_tasks[session_id] = self._spawn_background_task(
            _timer(),
            name=f"cons-idle-{session_id}",
        )

    def _schedule_consolidation_now(
        self,
        session_id: str,
        *,
        force: bool = False,
    ) -> asyncio.Task[None] | None:
        canonical_sid = self._canonical_consolidation_session_id(session_id)
        resolved_sid = self.agent.memory.resolve_session_id(canonical_sid)
        existing_now = (
            self._consolidation_tasks.get(session_id)
            or self._consolidation_tasks.get(canonical_sid)
            or self._consolidation_tasks.get(resolved_sid)
        )
        if existing_now is not None and not existing_now.done():
            return existing_now

        if (
            session_id in self._consolidation_in_flight
            or canonical_sid in self._consolidation_in_flight
            or resolved_sid in self._consolidation_in_flight
            or self.consolidation.is_in_flight(canonical_sid)
        ):
            return None

        if not force and not self.consolidation.should_consolidate(canonical_sid, force=False):
            return None

        for idle_key in {session_id, canonical_sid}:
            old_task = self._idle_tasks.pop(idle_key, None)
            if old_task is not None and not old_task.done():
                old_task.cancel()

        keys_to_mark = {session_id, canonical_sid, resolved_sid}
        self._consolidation_in_flight.update(keys_to_mark)
        task_id = f"cons-{canonical_sid}"

        task = self._spawn_background_task(
            self._execute_consolidation(
                canonical_sid,
                force=force,
                log_context="on end/switch",
            ),
            name=f"cons-now-{canonical_sid}",
        )
        self._consolidation_tasks[canonical_sid] = task
        if session_id and session_id != canonical_sid:
            self._consolidation_tasks[session_id] = task
        if resolved_sid and resolved_sid != canonical_sid:
            self._consolidation_tasks[resolved_sid] = task
        self._active_task_handles[task_id] = task
        self._task_metadata[task_id] = {
            "title": self._format_session_consolidation_title(canonical_sid),
            "kind": "background_task",
            "started_at": _now_iso(),
            "session_id": canonical_sid,
        }

        def _cleanup_in_flight(done_task: asyncio.Task[None]) -> None:
            self._consolidation_in_flight.difference_update(keys_to_mark)
            for key in keys_to_mark:
                if self._consolidation_tasks.get(key) is done_task:
                    self._consolidation_tasks.pop(key, None)
            if self._active_task_handles.get(task_id) is done_task:
                self._active_task_handles.pop(task_id, None)
                self._task_metadata.pop(task_id, None)

        task.add_done_callback(_cleanup_in_flight)
        return task

    async def _handle_connection(self, ws: ServerConnection) -> None:
        conn = _Connection(ws)
        self._connections.add(conn)
        try:
            with contextlib.suppress(ConnectionClosed):
                async for raw_message in ws:
                    if isinstance(raw_message, bytes):
                        raw_text = raw_message.decode("utf-8", errors="replace")
                    else:
                        raw_text = raw_message

                    try:
                        frame = parse_frame(raw_text)
                    except ValidationError as exc:
                        await self._send(
                            ws,
                            ErrorEvent(
                                data=ErrorData(
                                    message=f"Invalid protocol frame: {exc}", code="invalid_frame"
                                )
                            ),
                        )
                        continue

                    if not isinstance(frame, BaseRequest):
                        if isinstance(frame, VoiceStateEvent):
                            conn.is_voice = True
                            self._voice_state = frame.data.state
                            await self._broadcast(frame)
                            continue
                        await self._send(
                            ws,
                            ErrorEvent(
                                data=ErrorData(
                                    message="Client may only send 'request' frames.",
                                    code="invalid_frame",
                                )
                            ),
                        )
                        continue

                    await self._dispatch_request(conn, frame)
        finally:
            self._connections.discard(conn)
            if conn.is_voice and self._call_state != "idle":
                ended_call_id = self._voice_call_id or ""
                duration_seconds = (
                    round(max(0.0, time.monotonic() - self._call_started_monotonic), 3)
                    if self._call_started_monotonic is not None
                    else None
                )
                self._voice_call_id = None
                self._call_state = "idle"
                self._call_started_monotonic = None
                self._voice_state = "idle"
                self._screen_capture_active = False
                self._screen_capture_target = None
                await self._broadcast(
                    VoiceStateEvent(
                        data=VoiceStateData(state="error", error="Voice process disconnected.")
                    )
                )
                await self._broadcast(
                    CallEndedEvent(
                        data=CallEndedData(
                            call_id=ended_call_id,
                            call_state="idle",
                            reason="voice_process_disconnected",
                            duration_seconds=duration_seconds,
                        )
                    )
                )
            for approval_id in list(conn.approvals):
                self._shared_approvals.pop(approval_id, None)
            if conn.current_session_id:
                self._schedule_consolidation_now(conn.current_session_id)
            await conn.close()

    def _schedule_trash_purge(self, delay_seconds: float = 15.0) -> None:
        async def _purge_after_delay() -> None:
            await asyncio.sleep(delay_seconds)
            purged = self.agent.memory.purge_trash(min_age_seconds=10.0)
            if purged > 0:
                logger.info("Purged %d transcripts from trash", purged)

        self._spawn_background_task(_purge_after_delay(), name="trash-purge")

    async def _broadcast_conversation_updated(self, session_id: str) -> None:
        tr = self.agent.memory.get_transcript(session_id)
        if not tr:
            return
        msgs = tr.get("messages", [])
        await self._broadcast(
            ConversationUpdatedEvent(
                data=ConversationUpdatedData(
                    session_id=session_id,
                    title=tr.get("title"),
                    updated_at=tr.get("updated_at") or tr.get("started_at"),
                    turns=(len(msgs) + 1) // 2,
                )
            )
        )

    def _persist_voice_transcript(
        self,
        call_id: str,
        transcript: list[dict[str, Any]] | None,
    ) -> bool:
        """Persist a non-empty voice call transcript chronologically into Voice Companion stream.

        Returns True if one or more messages were newly persisted.
        """
        if not transcript:
            return False

        cleaned: list[tuple[str, str, datetime]] = []
        fallback_dt: datetime | None = None

        for entry in transcript:
            if not isinstance(entry, dict):
                continue
            raw_role = str(entry.get("role", "assistant")).strip().lower()
            role = raw_role if raw_role in ("user", "assistant") else "assistant"
            text = str(entry.get("text", "")).strip()
            if not text:
                continue
            dt: datetime | None = None
            raw_ts = entry.get("timestamp")
            if isinstance(raw_ts, str) and raw_ts.strip():
                with contextlib.suppress(ValueError):
                    parsed = datetime.fromisoformat(raw_ts.strip())
                    dt = parsed if parsed.tzinfo is not None else parsed.astimezone()
            if dt is None:
                dt = datetime.now().astimezone()
            if fallback_dt is None:
                fallback_dt = dt
            cleaned.append((role, text, dt))

        if not cleaned:
            return False

        # Ensure monotonic chronological order
        for i in range(1, len(cleaned)):
            prev_dt = cleaned[i - 1][2]
            curr_role, curr_text, curr_dt = cleaned[i]
            if curr_dt < prev_dt:
                cleaned[i] = (curr_role, curr_text, prev_dt)

        return self.agent.memory.append_voice_companion_call(
            call_id=call_id,
            entries=cleaned,
        )

    def _audit_log(self, action: str, **details: Any) -> None:
        try:
            self.agent.registry._audit.record_event(action=action, details=details)
        except Exception as exc:
            logger.warning("Failed to record audit event %s: %s", action, exc)

    def _resume_session_from_transcript(self, session_id: str) -> Session:
        self.agent.memory.validate_session_id(session_id)
        if session_id in self._busy_sessions:
            raise ValueError("A turn is currently running for this session.")

        session = Session(id=session_id)
        transcript = self.agent.memory.get_transcript(session_id)
        if transcript:
            for msg in transcript.get("messages", []):
                role = msg.get("role")
                text = msg.get("text", "")
                tool_calls = msg.get("tool_calls", [])
                if role == "user":
                    session.messages.append(Message(role="user", content=text))
                elif role == "assistant":
                    tool_notes: list[str] = []
                    if tool_calls:
                        for tc in tool_calls:
                            tname = tc.get("name", "tool")
                            targs = tc.get("arguments", {})
                            items = list(targs.items())[:3]
                            arg_summary = ", ".join(f"{k}={v!r}" for k, v in items)
                            output = tc.get("output", "")
                            out_summary = f": {str(output).strip()[:60]}" if output else ""
                            tool_notes.append(f"[used tool {tname}: {arg_summary}{out_summary}]")
                    notes_str = "\n".join(tool_notes)
                    combined = f"{notes_str}\n\n{text}".strip() if notes_str else text
                    session.messages.append(Message(role="assistant", content=combined))
            self.agent._apply_history_cap(session)
        self.sessions._sessions[session.id] = session
        return session

    async def _respond(
        self,
        ws: ServerConnection,
        req: Any,
        result: Any = None,
        *,
        session_id: str | None = None,
        error: str | None = None,
    ) -> None:
        sid = session_id if session_id is not None else req.session_id
        if error is not None:
            await self._send(ws, ResponseFrame(id=req.id, ok=False, session_id=sid, error=error))
            return
        expected = METHOD_RESULTS[req.method]
        assert isinstance(result, expected), f"{req.method} must return {expected.__name__}"
        await self._send(
            ws, ResponseFrame(id=req.id, session_id=sid, result=result.model_dump(mode="json"))
        )

    async def _dispatch_request(self, conn: _Connection, req: Any) -> None:
        ws = conn.ws
        method = req.method

        if method == "health.ping":
            await self._respond(ws, req, METHOD_RESULTS[method]())
        elif method == "session.new":
            if conn.current_session_id:
                self._schedule_consolidation_now(conn.current_session_id)
            session = self.sessions.create()
            conn.current_session_id = session.id
            await self._respond(
                ws, req, METHOD_RESULTS[method](session_id=session.id), session_id=session.id
            )
        elif method == "chat.send":
            text = req.params.text.strip()
            if not text:
                await self._respond(ws, req, error="Missing non-empty 'text' parameter.")
                return
            conn.spawn(self._run_chat(conn, req, text))
        elif method == "chat.focus":
            if req.params.clear:
                self._active_conversation_id = None
            elif req.params.session_id is not None:
                target_id = req.params.session_id.strip() or None
                if target_id is not None:
                    try:
                        self.agent.memory.validate_session_id(target_id)
                        if target_id == VOICE_COMPANION_SESSION_ID:
                            self._active_conversation_id = None
                        else:
                            self._active_conversation_id = target_id
                    except ValueError:
                        self._active_conversation_id = None
                else:
                    self._active_conversation_id = None
            await self._respond(
                ws,
                req,
                METHOD_RESULTS[method](active_conversation_id=self._active_conversation_id),
            )
        elif method == "approval.respond":
            future = conn.approvals.get(req.params.approval_id) or self._shared_approvals.get(
                req.params.approval_id
            )
            if future is None or future.done():
                await self._respond(ws, req, error="Unknown or already resolved approval.")
                return
            future.set_result(req.params.decision)
            await self._respond(
                ws,
                req,
                METHOD_RESULTS[method](
                    approval_id=req.params.approval_id, decision=req.params.decision
                ),
            )
        elif method == "memory.get":
            memory = self.agent.memory
            last_cons, failed_cons = self.consolidation.get_status()
            await self._respond(
                ws,
                req,
                METHOD_RESULTS[method](
                    long_term=memory.read_long_term(),
                    days=memory.list_days(),
                    last_consolidated=last_cons,
                    failed_consolidation=failed_cons,
                ),
            )
        elif method == "memory.episodes":
            limit = getattr(req.params, "limit", 20)
            raw_eps = self.agent.memory.get_recent_episodes(limit=limit)
            episodes = [
                EpisodeItem(
                    date=ep.get("date", ""),
                    session_id=ep.get("session_id", ""),
                    time=ep.get("time", ""),
                    summary=ep.get("summary", ""),
                    trivial=bool(ep.get("trivial", False)),
                )
                for ep in raw_eps
            ]
            await self._respond(ws, req, METHOD_RESULTS[method](episodes=episodes))
        elif method == "memory.facts":
            sections_dict = self.agent.memory.parse_sections()
            sections = [
                MemorySectionItem(
                    name=sec_name,
                    entries=[
                        MemoryFactItem(
                            id=e.id,
                            date=e.date,
                            source=e.source,
                            text=e.text,
                        )
                        for e in entries
                    ],
                )
                for sec_name, entries in sections_dict.items()
            ]
            last_cons, failed_cons = self.consolidation.get_status()
            await self._respond(
                ws,
                req,
                METHOD_RESULTS[method](
                    sections=sections,
                    last_consolidated=last_cons,
                    failed_consolidation=failed_cons,
                ),
            )
        elif method == "memory.archive":
            entries, raw = self.agent.memory.read_archive()
            archive_items = [
                MemoryArchiveItem(
                    id=e.get("id", ""),
                    date=e.get("date", ""),
                    source=e.get("source", ""),
                    text=e.get("text", ""),
                    archived_at=e.get("archived_at"),
                    reason=e.get("reason"),
                )
                for e in entries
            ]
            await self._respond(ws, req, METHOD_RESULTS[method](entries=archive_items, raw=raw))
        elif method == "memory.status":
            det = self.consolidation.get_detailed_status()
            pending = [
                ConsolidationSessionStatus(
                    session_id=p["session_id"],
                    status=p["status"],
                    retry_count=p["retry_count"],
                    error=p.get("error"),
                    last_attempt_at=p.get("last_attempt_at"),
                    next_retry_at=p.get("next_retry_at"),
                )
                for p in det.get("pending", [])
            ]
            failed = [
                ConsolidationSessionStatus(
                    session_id=f["session_id"],
                    status=f["status"],
                    retry_count=f["retry_count"],
                    error=f.get("error"),
                    last_attempt_at=f.get("last_attempt_at"),
                    next_retry_at=f.get("next_retry_at"),
                )
                for f in det.get("failed", [])
            ]
            await self._respond(
                ws,
                req,
                METHOD_RESULTS[method](
                    last_consolidated_at=det.get("last_consolidated_at"),
                    pending=pending,
                    failed=failed,
                    completed_count=det.get("completed_count", 0),
                ),
            )
        elif method == "memory.reconsolidate":
            sid = req.params.session_id
            resolved_sid = self.agent.memory.resolve_session_id(sid)

            async def _on_reconsolidate_status(st: str, detail: str | None = None) -> None:
                await self._emit_consolidation_task_status(resolved_sid, st, detail)

            ok = await self.consolidation.reconsolidate_session(
                sid, on_task_status=_on_reconsolidate_status
            )
            state_info = self.consolidation.load_state().get("sessions", {}).get(
                resolved_sid
            ) or self.consolidation.load_state().get("sessions", {}).get(sid, {})
            current_status = state_info.get("status", "completed" if ok else "failed")
            err = state_info.get("last_error") or state_info.get("error") if not ok else None
            if ok:
                await self._broadcast(
                    MemoryUpdatedEvent(data=MemoryUpdatedData(action="updated"))
                )
            await self._respond(
                ws,
                req,
                METHOD_RESULTS[method](
                    session_id=resolved_sid,
                    ok=ok,
                    status=current_status,
                    error=err,
                ),
            )
        elif method == "memory.forget":
            target = req.params.id or req.params.query or ""
            removed = self.agent.memory.remove_entries(target)
            if not removed and req.params.id and req.params.query:
                removed = self.agent.memory.remove_entries(req.params.query)
            if removed:
                await self._broadcast(
                    MemoryUpdatedEvent(
                        data=MemoryUpdatedData(
                            action="deleted",
                            removed_count=len(removed),
                            removed_ids=[e.id for e in removed],
                        )
                    )
                )
            await self._respond(
                ws,
                req,
                METHOD_RESULTS[method](
                    ok=True,
                    removed_count=len(removed),
                    removed_ids=[e.id for e in removed],
                ),
            )
        elif method == "transcripts.list":
            limit = getattr(req.params, "limit", 50)
            raw_trs = self.agent.memory.list_transcripts(limit=limit)
            transcripts = [
                TranscriptSummaryItem(
                    session_id=t.get("session_id", ""),
                    started_at=t.get("started_at", ""),
                    title=t.get("title", ""),
                    message_count=t.get("message_count", 0),
                )
                for t in raw_trs
            ]
            await self._respond(ws, req, METHOD_RESULTS[method](transcripts=transcripts))
        elif method == "transcripts.get":
            transcript = self.agent.memory.get_transcript(req.params.session_id)
            if transcript is None:
                await self._respond(
                    ws, req, error=f"Transcript not found for session {req.params.session_id}"
                )
                return
            messages = [
                TranscriptMessageItem(
                    role=m.get("role", "unknown"),
                    text=m.get("text", ""),
                    timestamp=m.get("timestamp", ""),
                    tool_calls=m.get("tool_calls", []),
                )
                for m in transcript.get("messages", [])
            ]
            await self._respond(
                ws,
                req,
                METHOD_RESULTS[method](
                    session_id=transcript.get("session_id", req.params.session_id),
                    started_at=transcript.get("started_at", ""),
                    title=transcript.get("title", ""),
                    messages=messages,
                ),
            )
        elif method == "dev.emit_sample_task":
            # Dev-only endpoint to emit sample tasks for Activity tab testing
            now = _now_iso()
            await self._broadcast(
                TaskStatusEvent(
                    data=TaskStatusData(
                        task_id="dev-bg-1",
                        kind="background_task",
                        title="Summarising session 18:11",
                        status="running",
                        started_at=now,
                    )
                )
            )
            await self._broadcast(
                TaskStatusEvent(
                    data=TaskStatusData(
                        task_id="dev-sched-1",
                        kind="scheduled",
                        title="Daily calendar sync",
                        status="queued",
                        started_at=now,
                        next_run_at=_now_iso(),
                    )
                )
            )
            await self._broadcast(
                TaskStatusEvent(
                    data=TaskStatusData(
                        task_id="dev-agent-1",
                        kind="agent_run",
                        title="Summarize desktop files",
                        status="succeeded",
                        started_at=now,
                        finished_at=now,
                    )
                )
            )
            await self._broadcast(
                TaskStatusEvent(
                    data=TaskStatusData(
                        task_id="dev-fail-1",
                        kind="background_task",
                        title="Sync workspace index",
                        status="failed",
                        started_at=now,
                        finished_at=now,
                        detail="Connection timed out after 30s. Host 127.0.0.1 unreachable.",
                    )
                )
            )
            await self._respond(ws, req, METHOD_RESULTS[method](status="ok"))
        elif method == "memory.get_daily":
            try:
                content = self.agent.memory.read_daily_on(req.params.date)
            except ValueError as exc:
                await self._respond(ws, req, error=str(exc))
                return
            await self._respond(
                ws, req, METHOD_RESULTS[method](date=req.params.date, content=content)
            )
        elif method in ("conversations.list", "conversation.list"):
            await self._respond(
                ws,
                req,
                METHOD_RESULTS[method](conversations=self.agent.memory.list_conversations()),
            )
        elif method in ("conversations.get", "conversation.get"):
            try:
                date_val = getattr(req.params, "date", "") or ""
                turns = self.agent.memory.get_conversation(date_val, req.params.session_id)
            except ValueError as exc:
                await self._respond(ws, req, error=str(exc))
                return
            await self._respond(
                ws,
                req,
                METHOD_RESULTS[method](
                    date=date_val, session_id=req.params.session_id, turns=turns
                ),
            )
        elif method == "conversation.resume":
            session_id = req.params.session_id
            if session_id == VOICE_COMPANION_SESSION_ID:
                await self._respond(
                    ws, req, error="Voice Companion cannot be resumed as a text conversation."
                )
                return
            if session_id in self._busy_sessions:
                await self._respond(ws, req, error="A turn is currently running for this session.")
                return
            try:
                self.agent.memory.validate_session_id(session_id)
                session = self._resume_session_from_transcript(session_id)
                transcript = self.agent.memory.get_transcript(session_id)
                if not transcript:
                    await self._respond(ws, req, error=f"Conversation {session_id} not found.")
                    return
            except (ValueError, FileNotFoundError) as exc:
                await self._respond(ws, req, error=str(exc))
                return

            conn.current_session_id = session.id
            resume_messages = [
                ConversationResumeMessage(
                    role="user" if m.get("role") == "user" else "assistant",
                    text=m.get("text", ""),
                    timestamp=m.get("timestamp", ""),
                    tool_calls=m.get("tool_calls", []) or [],
                )
                for m in transcript.get("messages", [])
            ]
            pinned = bool(transcript.get("pinned", False))
            title = transcript.get("title", "Conversation")
            await self._respond(
                ws,
                req,
                METHOD_RESULTS[method](
                    session_id=session.id,
                    title=title,
                    pinned=pinned,
                    messages=resume_messages,
                ),
                session_id=session.id,
            )
        elif method == "conversation.rename":
            session_id = req.params.session_id
            if session_id == VOICE_COMPANION_SESSION_ID:
                await self._respond(
                    ws, req, error="Cannot rename the Voice Companion stream."
                )
                return
            new_title = req.params.title
            try:
                header = self.agent.memory.update_transcript_header(session_id, title=new_title)
            except (ValueError, FileNotFoundError) as exc:
                await self._respond(ws, req, error=str(exc))
                return
            clean_title = header.get("title", new_title)
            await self._broadcast(
                ConversationUpdatedEvent(
                    session_id=session_id,
                    data=ConversationUpdatedData(session_id=session_id, title=clean_title),
                )
            )
            await self._respond(
                ws,
                req,
                METHOD_RESULTS[method](session_id=session_id, title=clean_title),
                session_id=session_id,
            )
        elif method == "conversation.pin":
            session_id = req.params.session_id
            if session_id == VOICE_COMPANION_SESSION_ID:
                await self._respond(
                    ws, req, error="Cannot pin the Voice Companion stream."
                )
                return
            pinned = req.params.pinned
            try:
                header = self.agent.memory.update_transcript_header(session_id, pinned=pinned)
            except (ValueError, FileNotFoundError) as exc:
                await self._respond(ws, req, error=str(exc))
                return
            await self._broadcast(
                ConversationUpdatedEvent(
                    session_id=session_id,
                    data=ConversationUpdatedData(session_id=session_id, pinned=pinned),
                )
            )
            await self._respond(
                ws,
                req,
                METHOD_RESULTS[method](session_id=session_id, pinned=pinned),
                session_id=session_id,
            )
        elif method == "conversation.export":
            session_id = req.params.session_id
            try:
                filename, content = self.agent.memory.export_transcript_markdown(session_id)
                transcript = self.agent.memory.get_transcript(session_id)
                title = transcript.get("title", "Conversation") if transcript else "Conversation"
            except (ValueError, FileNotFoundError) as exc:
                await self._respond(ws, req, error=str(exc))
                return
            await self._respond(
                ws,
                req,
                METHOD_RESULTS[method](
                    session_id=session_id,
                    title=title,
                    filename=filename,
                    content=content,
                ),
                session_id=session_id,
            )
        elif method == "conversation.delete":
            session_id = req.params.session_id
            if session_id == VOICE_COMPANION_SESSION_ID:
                await self._respond(
                    ws, req, error="Cannot delete the Voice Companion stream."
                )
                return
            if session_id in self._busy_sessions:
                await self._respond(
                    ws, req, error="Cannot delete conversation while a turn is running."
                )
                return
            delete_episodes = req.params.delete_episodes
            try:
                await self._perform_conversation_delete(
                    session_id, delete_episodes=delete_episodes
                )
            except (ValueError, FileNotFoundError) as exc:
                await self._respond(ws, req, error=str(exc))
                return

            await self._respond(
                ws,
                req,
                METHOD_RESULTS[method](session_id=session_id, deleted=True, undo_window_seconds=10),
                session_id=session_id,
            )
        elif method == "conversation.delete_all":
            if self._busy_sessions:
                await self._respond(
                    ws, req, error="Cannot delete conversations while a turn is running."
                )
                return
            if req.params.confirmation != "DELETE":
                await self._respond(ws, req, error="Confirmation string 'DELETE' required.")
                return

            count = self.agent.memory.delete_all_transcripts()
            self._audit_log("conversation_delete_all", deleted_count=count)
            self.sessions._sessions.clear()
            self._active_conversation_id = None
            for c in list(self._connections):
                c.current_session_id = None

            self._schedule_trash_purge(delay_seconds=15.0)

            await self._broadcast(
                ConversationDeletedEvent(
                    data=ConversationDeletedData(session_ids=[], all=True),
                )
            )
            await self._respond(
                ws,
                req,
                METHOD_RESULTS[method](deleted_count=count, undo_window_seconds=10),
            )
        elif method == "conversation.undo_delete":
            session_id = req.params.session_id
            if session_id in ("", "*", "all"):
                session_id = None
            try:
                restored = self.agent.memory.undo_delete_transcript(session_id)
            except ValueError as exc:
                await self._respond(ws, req, error=str(exc))
                return
            self._audit_log("conversation_undo_delete", session_ids=restored)
            await self._broadcast(
                ConversationUpdatedEvent(
                    data=ConversationUpdatedData(session_id=session_id or ""),
                )
            )
            await self._respond(
                ws,
                req,
                METHOD_RESULTS[method](restored_count=len(restored), session_ids=restored),
            )
        elif method == "settings.get":
            await self._respond(ws, req, self._settings())
        elif method == "voice.status":
            await self._respond(
                ws,
                req,
                METHOD_RESULTS[method](
                    call_id=self._voice_call_id,
                    call_state=self._call_state,
                    voice_state=self._voice_state,
                    screen_capture_active=self._screen_capture_active,
                    screen_capture_target=self._screen_capture_target,
                ),
            )
        elif method == "voice.start_call":
            if self._call_state != "idle":
                await self._respond(ws, req, error="A voice call is already active.")
                return
            trigger = getattr(req.params, "trigger", "manual")
            if trigger in ("wake_word", "hotkey"):
                conn.is_voice = True
            elif trigger == "ui":
                voice_connected = any(getattr(c, "is_voice", False) for c in self._connections)
                if not voice_connected:
                    err_msg = "Voice process not running. Start it with 'clea voice'."
                    await self._broadcast(
                        VoiceStateEvent(data=VoiceStateData(state="error", error=err_msg))
                    )
                    await self._respond(ws, req, error=err_msg)
                    return
            call_id = new_id()
            self._voice_call_id = call_id
            self._call_state = "active"
            self._call_started_monotonic = time.monotonic()
            self._voice_state = "idle"
            self._screen_capture_active = False
            self._screen_capture_target = None
            await self._broadcast(
                CallStartedEvent(
                    request_id=req.id,
                    session_id=req.session_id,
                    data=CallStartedData(
                        call_id=call_id,
                        call_state=self._call_state,
                        started_at=_now_iso(),
                    ),
                )
            )
            memory_ctx = self.agent.memory.build_injected_context(
                include_voice_tail=True,
                voice_tail_turns=15,
                active_conversation_id=self._active_conversation_id,
            )
            await self._respond(
                ws,
                req,
                METHOD_RESULTS[method](
                    call_id=call_id,
                    call_state=self._call_state,
                    voice_state=self._voice_state,
                    memory_context=memory_ctx or None,
                ),
            )
        elif method == "voice.end_call":
            if req.params.reason in ("transcript_sync", "consolidate"):
                req_call_id = (req.params.call_id or "").strip()
                if not req_call_id:
                    await self._respond(ws, req, error="Unknown call_id.")
                    return
                try:
                    self.agent.memory.validate_session_id(req_call_id)
                except ValueError as exc:
                    await self._respond(ws, req, error=str(exc))
                    return
                if req.params.reason == "transcript_sync":
                    persisted = self._persist_voice_transcript(req_call_id, req.params.transcript)
                    await self._respond(
                        ws,
                        req,
                        METHOD_RESULTS[method](
                            call_id=req_call_id,
                            call_state=self._call_state,
                            voice_state=self._voice_state,
                        ),
                    )
                    if persisted:
                        await self._broadcast_conversation_updated(VOICE_COMPANION_SESSION_ID)
                        await self._broadcast_conversation_updated(req_call_id)
                    return
                tr = self.agent.memory.get_transcript(req_call_id)
                if tr and tr.get("messages"):
                    self._schedule_consolidation_now(req_call_id)
                self._ended_voice_call_ids.discard(req_call_id)
                await self._respond(
                    ws,
                    req,
                    METHOD_RESULTS[method](
                        call_id=req_call_id,
                        call_state=self._call_state,
                        voice_state=self._voice_state,
                    ),
                )
                return
            if (
                req.params.call_id is not None
                and self._voice_call_id is not None
                and req.params.call_id != self._voice_call_id
            ):
                await self._respond(ws, req, error="Unknown call_id.")
                return
            if self._voice_call_id is None or self._call_state == "idle":
                if req.params.call_id is not None:
                    await self._respond(ws, req, error="Unknown call_id.")
                    return
                await self._respond(
                    ws,
                    req,
                    METHOD_RESULTS[method](call_id=None, call_state="idle", voice_state="idle"),
                )
                return
            ended_id = self._voice_call_id
            if len(self._ended_voice_call_ids) >= 64:
                self._ended_voice_call_ids.clear()
            self._ended_voice_call_ids.add(ended_id)
            duration_seconds = (
                round(max(0.0, time.monotonic() - self._call_started_monotonic), 3)
                if self._call_started_monotonic is not None
                else None
            )
            self._voice_call_id = None
            self._call_state = "idle"
            self._call_started_monotonic = None
            self._voice_state = "idle"
            self._screen_capture_active = False
            self._screen_capture_target = None

            # Persist transcript to logs/transcripts/ and schedule consolidation if non-empty
            persisted = self._persist_voice_transcript(
                ended_id,
                getattr(req.params, "transcript", None),
            )
            if persisted and req.params.reason != "transcript_sync":
                self._schedule_consolidation_now(ended_id)

            await self._broadcast(
                CallEndedEvent(
                    request_id=req.id,
                    session_id=req.session_id,
                    data=CallEndedData(
                        call_id=ended_id,
                        call_state="idle",
                        reason=req.params.reason,
                        duration_seconds=duration_seconds,
                    ),
                )
            )
            await self._respond(
                ws,
                req,
                METHOD_RESULTS[method](call_id=ended_id, call_state="idle", voice_state="idle"),
            )
            if persisted:
                await self._broadcast_conversation_updated(VOICE_COMPANION_SESSION_ID)
                await self._broadcast_conversation_updated(ended_id)
        elif method == "voice.delegate_task":
            description = req.params.description.strip()
            if not description:
                await self._respond(ws, req, error="Missing non-empty 'description' parameter.")
                return
            conn.spawn(self._run_delegated_task(conn, req, description))
        elif method == "voice.history":
            limit = getattr(req.params, "limit", 100) or 100
            limit = max(1, min(int(limit), 500))
            tail = self.agent.memory.get_voice_companion_tail(max_turns=limit)
            items: list[VoiceHistoryMessageItem] = []
            for m in tail:
                if not isinstance(m, dict):
                    continue
                text = str(m.get("text", "")).strip()
                if not text:
                    continue
                role = "user" if m.get("role") == "user" else "assistant"
                items.append(
                    VoiceHistoryMessageItem(
                        role=role,
                        text=text,
                        timestamp=str(m.get("timestamp") or ""),
                        call_id=str(m.get("call_id") or "") or None,
                    )
                )
            await self._respond(
                ws,
                req,
                METHOD_RESULTS[method](
                    session_id=VOICE_COMPANION_SESSION_ID,
                    messages=items,
                ),
            )
        elif method == "voice.list_conversations":
            limit = getattr(req.params, "limit", 10) or 10
            limit = max(1, min(int(limit), 100))
            convs_raw = self.agent.memory.list_conversations(limit=limit)
            items = [VoiceConversationListItem(**c) for c in convs_raw]
            await self._respond(
                ws,
                req,
                METHOD_RESULTS[method](conversations=items),
            )
        elif method == "voice.search_conversations":
            query = getattr(req.params, "query", "")
            limit = getattr(req.params, "limit", 8) or 8
            limit = max(1, min(int(limit), 50))
            hits_raw = self.agent.memory.search_conversations(query=query, limit=limit)
            hits = [ConversationSearchHit(**hit) for hit in hits_raw]
            await self._respond(
                ws,
                req,
                METHOD_RESULTS[method](results=hits),
            )
        elif method == "voice.read_conversation":
            cid = str(getattr(req.params, "conversation_id", "") or "").strip()
            if not cid:
                await self._respond(ws, req, error="conversation_id cannot be empty.")
                return
            max_turns = getattr(req.params, "max_turns", 40) or 40
            max_turns = max(1, min(int(max_turns), 500))
            try:
                data = self.agent.memory.read_conversation(
                    conversation_id=cid,
                    max_turns=max_turns,
                )
            except ValueError:
                data = None
            if data is None:
                await self._respond(
                    ws,
                    req,
                    METHOD_RESULTS[method](
                        found=False,
                        conversation_id=cid,
                        session_id=cid,
                        message=f"Conversation '{cid}' not found.",
                    ),
                )
                return
            await self._respond(
                ws,
                req,
                METHOD_RESULTS[method](
                    found=True,
                    conversation_id=data["conversation_id"],
                    session_id=data["session_id"],
                    title=data["title"],
                    updated_at=data["updated_at"],
                    episode_summary=data["episode_summary"],
                    total_turns=data["total_turns"],
                    truncated=data["truncated"],
                    turns=[VoiceConversationTurnItem(**t) for t in data["turns"]],
                ),
            )
        elif method == "voice.search_memory":
            query = getattr(req.params, "query", "")
            limit = getattr(req.params, "limit", 10) or 10
            limit = max(1, min(int(limit), 50))
            hits_raw = self.agent.memory.search_memory(query=query, limit=limit)
            hits = [MemorySearchHit(**hit) for hit in hits_raw]
            await self._respond(
                ws,
                req,
                METHOD_RESULTS[method](results=hits),
            )
        elif method == "voice.post_to_active_chat":
            content = getattr(req.params, "content", "").strip()
            label = getattr(req.params, "label", None)
            if not content:
                await self._respond(ws, req, error="Content cannot be empty.")
                return
            if not self._active_conversation_id:
                await self._respond(
                    ws,
                    req,
                    METHOD_RESULTS[method](
                        ok=False,
                        session_id=None,
                        title=None,
                        message="No active text chat is currently focused in the UI.",
                    ),
                )
                return
            try:
                res = self.agent.memory.append_to_active_chat(
                    self._active_conversation_id,
                    content=content,
                    label=label,
                )
                active_sess = self.sessions.get(self._active_conversation_id)
                if active_sess is not None:
                    active_sess.messages.append(Message(role="assistant", content=res["message"]))
                await self._broadcast(
                    ConversationUpdatedEvent(
                        session_id=self._active_conversation_id,
                        data=ConversationUpdatedData(
                            session_id=self._active_conversation_id,
                            title=res["title"],
                            updated_at=res["updated_at"],
                            turns=res["turns"],
                            assistant_message=res["message"],
                        ),
                    )
                )
                await self._respond(
                    ws,
                    req,
                    METHOD_RESULTS[method](
                        ok=True,
                        session_id=self._active_conversation_id,
                        title=res["title"],
                        message=res["message"],
                    ),
                )
            except Exception as exc:
                await self._respond(
                    ws,
                    req,
                    METHOD_RESULTS[method](
                        ok=False,
                        session_id=self._active_conversation_id,
                        title=None,
                        message=f"Failed to post to active chat: {exc}",
                    ),
                )
        elif method == "ui.navigate":
            await self._handle_ui_navigate(conn, req)
        elif method == "chat.create_and_open":
            await self._handle_chat_create_and_open(conn, req)
        elif method == "chat.send_from_voice":
            await self._handle_chat_send_from_voice(conn, req)
        elif method == "chat.rename":
            await self._handle_chat_rename(conn, req)
        elif method == "chat.delete":
            await self._handle_chat_delete(conn, req)
        elif method == "memory.add_fact":
            await self._handle_memory_add_fact(conn, req)
        elif method == "memory.delete_fact":
            await self._handle_memory_delete_fact(conn, req)
        elif method == "activity.list":
            await self._handle_activity_list(conn, req)
        elif method == "activity.cancel":
            await self._handle_activity_cancel(conn, req)
        elif method == "os.open_or_focus_app":
            await self._handle_os_open_or_focus_app(conn, req)
        elif method == "os.control_media_volume":
            await self._handle_os_control_media_volume(conn, req)
        elif method == "os.read_clipboard":
            await self._handle_os_read_clipboard(conn, req)
        elif method == "os.write_clipboard":
            await self._handle_os_write_clipboard(conn, req)
        elif method == "workspace.git_inspect":
            await self._handle_workspace_git_inspect(conn, req)

    async def _perform_conversation_delete(
        self,
        session_id: str,
        *,
        delete_episodes: bool = False,
    ) -> None:
        """Delete transcript and clean up daemon/connection session state."""
        idle_task = self._idle_tasks.pop(session_id, None)
        if idle_task is not None and not idle_task.done():
            idle_task.cancel()
        self.agent.memory.delete_transcript(session_id, delete_episodes=delete_episodes)
        self._audit_log(
            "conversation_delete",
            session_id=session_id,
            delete_episodes=delete_episodes,
        )
        self.sessions._sessions.pop(session_id, None)
        if self._active_conversation_id == session_id:
            self._active_conversation_id = None
        for c in list(self._connections):
            if c.current_session_id == session_id:
                c.current_session_id = None

        self._schedule_trash_purge(delay_seconds=15.0)
        await self._broadcast(
            ConversationDeletedEvent(
                session_id=session_id,
                data=ConversationDeletedData(session_ids=[session_id], all=False),
            )
        )

    def _set_ui_session_focus(self, session_id: str | None) -> None:
        """Update active_conversation_id and non-voice UI connections' current_session_id."""
        self._active_conversation_id = session_id
        for c in list(self._connections):
            if not c.is_voice:
                c.current_session_id = session_id

    async def _handle_ui_navigate(self, conn: _Connection, req: Any) -> None:
        ws = conn.ws
        view = req.params.view
        raw_cid = (req.params.conversation_id or "").strip() or None
        open_palette = bool(req.params.open_command_palette)
        resolved_cid: str | None = None

        if raw_cid == VOICE_COMPANION_SESSION_ID:
            view = "voice"
            resolved_cid = None
            self._active_conversation_id = None
        elif raw_cid is not None:
            try:
                self.agent.memory.validate_session_id(raw_cid)
                candidate = self.agent.memory.resolve_session_id(raw_cid)
                has_transcript = self.agent.memory.transcript_path(candidate).exists()
                has_session = self.sessions.get(candidate) is not None
                if not has_transcript and not has_session:
                    await self._respond(ws, req, error=f"Conversation '{raw_cid}' not found.")
                    return
                resolved_cid = candidate
            except ValueError as exc:
                await self._respond(ws, req, error=str(exc))
                return
            view = "chat"
            self._set_ui_session_focus(resolved_cid)
        elif view != "chat" and not open_palette:
            self._active_conversation_id = None

        await self._broadcast(
            UiNavigateEvent(
                session_id=self._active_conversation_id,
                data=UiNavigateData(
                    view=view,
                    conversation_id=resolved_cid,
                    open_command_palette=open_palette,
                ),
            )
        )
        await self._respond(
            ws,
            req,
            METHOD_RESULTS["ui.navigate"](
                ok=True,
                view=view,
                active_conversation_id=self._active_conversation_id,
                open_command_palette=open_palette,
            ),
            session_id=resolved_cid,
        )

    async def _handle_chat_create_and_open(self, conn: _Connection, req: Any) -> None:
        ws = conn.ws
        session = self.sessions.create()
        try:
            meta = self.agent.memory.create_conversation(
                session_id=session.id,
                title=req.params.title,
                initial_assistant_message=req.params.content,
            )
        except ValueError as exc:
            await self._respond(ws, req, error=str(exc))
            return

        initial_msg = meta["message"]
        if initial_msg:
            session.messages.append(Message(role="assistant", content=initial_msg))

        self._active_conversation_id = session.id
        if req.params.open_in_ui:
            self._set_ui_session_focus(session.id)

        await self._broadcast(
            ConversationCreatedEvent(
                session_id=session.id,
                data=ConversationCreatedData(
                    session_id=session.id,
                    title=meta["title"],
                    started_at=meta["started_at"],
                    updated_at=meta["updated_at"],
                    turns=meta["turns"],
                    initial_message=initial_msg or None,
                    open_in_ui=req.params.open_in_ui,
                ),
            )
        )
        await self._broadcast(
            ConversationUpdatedEvent(
                session_id=session.id,
                data=ConversationUpdatedData(
                    session_id=session.id,
                    title=meta["title"],
                    updated_at=meta["updated_at"],
                    turns=meta["turns"],
                ),
            )
        )
        if req.params.open_in_ui:
            await self._broadcast(
                UiNavigateEvent(
                    session_id=session.id,
                    data=UiNavigateData(
                        view="chat",
                        conversation_id=session.id,
                        open_command_palette=False,
                    ),
                )
            )
        await self._respond(
            ws,
            req,
            METHOD_RESULTS["chat.create_and_open"](
                ok=True,
                session_id=session.id,
                title=meta["title"],
                message=initial_msg,
                open_in_ui=req.params.open_in_ui,
            ),
            session_id=session.id,
        )

    async def _handle_chat_send_from_voice(self, conn: _Connection, req: Any) -> None:
        ws = conn.ws
        prompt = req.params.prompt.strip()
        if not prompt:
            await self._respond(ws, req, error="Prompt must not be empty.")
            return

        raw_cid = (req.params.conversation_id or "").strip() or None
        raw_title = (req.params.title or "").strip() or None

        if raw_cid == VOICE_COMPANION_SESSION_ID:
            await self._respond(
                ws, req, error="Cannot send chat messages to the Voice Companion stream."
            )
            return

        created_new = False
        created_meta: dict[str, Any] | None = None
        if raw_cid is not None:
            try:
                self.agent.memory.validate_session_id(raw_cid)
                resolved_cid = self.agent.memory.resolve_session_id(raw_cid)
            except ValueError as exc:
                await self._respond(ws, req, error=str(exc))
                return
            if resolved_cid == VOICE_COMPANION_SESSION_ID:
                await self._respond(
                    ws, req, error="Cannot send chat messages to the Voice Companion stream."
                )
                return
            if resolved_cid in self._busy_sessions:
                await self._respond(
                    ws,
                    req,
                    error="A turn is already running for this session.",
                    session_id=resolved_cid,
                )
                return
            existing_sess = self.sessions.get(resolved_cid)
            existing_tr = self.agent.memory.get_transcript(resolved_cid)
            if existing_sess is None and existing_tr is None:
                await self._respond(ws, req, error=f"Conversation '{raw_cid}' not found.")
                return
            session = existing_sess or self._resume_session_from_transcript(resolved_cid)
            if existing_tr is None:
                created_meta = self.agent.memory.create_conversation(
                    session_id=session.id,
                    title=raw_title or prompt[:60],
                )
                chat_title = created_meta["title"]
            elif raw_title:
                header = self.agent.memory.update_transcript_header(session.id, title=raw_title)
                chat_title = str(header.get("title") or raw_title)
            else:
                chat_title = str(existing_tr.get("title") or raw_title or prompt[:60])
        else:
            created_new = True
            session = self.sessions.create()
            created_meta = self.agent.memory.create_conversation(
                session_id=session.id,
                title=raw_title or prompt[:60],
            )
            chat_title = created_meta["title"]

        if session.id in self._busy_sessions:
            await self._respond(
                ws,
                req,
                error="A turn is already running for this session.",
                session_id=session.id,
            )
            return

        self._set_ui_session_focus(session.id)
        turn_req_id = f"voice-chat-{new_id()[:8]}"

        if created_new and created_meta is not None:
            await self._broadcast(
                ConversationCreatedEvent(
                    session_id=session.id,
                    data=ConversationCreatedData(
                        session_id=session.id,
                        title=chat_title,
                        started_at=created_meta["started_at"],
                        updated_at=created_meta["updated_at"],
                        turns=created_meta["turns"],
                        initial_message=None,
                        open_in_ui=True,
                    ),
                )
            )

        await self._broadcast(
            UiNavigateEvent(
                session_id=session.id,
                data=UiNavigateData(
                    view="chat",
                    conversation_id=session.id,
                    open_command_palette=False,
                ),
            )
        )
        await self._broadcast(
            ConversationUpdatedEvent(
                session_id=session.id,
                request_id=turn_req_id,
                data=ConversationUpdatedData(
                    session_id=session.id,
                    title=chat_title,
                    updated_at=_now_iso(),
                    user_message=prompt,
                    request_id=turn_req_id,
                ),
            )
        )

        self._busy_sessions.add(session.id)
        try:
            self._spawn_background_task(
                self._run_voice_chat_turn(
                    session=session,
                    request_id=turn_req_id,
                    text=prompt,
                    title=chat_title,
                ),
                name=f"voice-chat-{session.id}",
            )
        except Exception:
            self._busy_sessions.discard(session.id)
            raise

        await self._respond(
            ws,
            req,
            METHOD_RESULTS["chat.send_from_voice"](
                ok=True,
                session_id=session.id,
                title=chat_title,
                request_id=turn_req_id,
                status="started",
            ),
            session_id=session.id,
        )

    async def _handle_chat_rename(self, conn: _Connection, req: Any) -> None:
        ws = conn.ws
        raw_id = req.params.conversation_id.strip()
        if raw_id == VOICE_COMPANION_SESSION_ID:
            await self._respond(ws, req, error="Cannot rename the Voice Companion stream.")
            return
        try:
            self.agent.memory.validate_session_id(raw_id)
            sid = self.agent.memory.resolve_session_id(raw_id)
            if sid == VOICE_COMPANION_SESSION_ID:
                await self._respond(ws, req, error="Cannot rename the Voice Companion stream.")
                return
            header = self.agent.memory.update_transcript_header(sid, title=req.params.new_title)
        except (ValueError, FileNotFoundError) as exc:
            await self._respond(ws, req, error=str(exc))
            return

        clean_title = str(header.get("title", req.params.new_title))
        await self._broadcast(
            ConversationUpdatedEvent(
                session_id=sid,
                data=ConversationUpdatedData(
                    session_id=sid,
                    title=clean_title,
                    updated_at=str(header.get("updated_at") or _now_iso()),
                ),
            )
        )
        await self._respond(
            ws,
            req,
            METHOD_RESULTS["chat.rename"](ok=True, session_id=sid, title=clean_title),
            session_id=sid,
        )

    async def _handle_chat_delete(self, conn: _Connection, req: Any) -> None:
        ws = conn.ws
        raw_id = req.params.conversation_id.strip()
        if raw_id == VOICE_COMPANION_SESSION_ID:
            await self._respond(ws, req, error="Cannot delete the Voice Companion stream.")
            return
        if not req.params.confirmed:
            await self._respond(
                ws,
                req,
                METHOD_RESULTS["chat.delete"](
                    ok=False,
                    session_id=raw_id,
                    deleted=False,
                    requires_confirmation=True,
                    episodes_removed=0,
                    message=(
                        "Deletion requires explicit verbal confirmation from the user. "
                        "Ask the user to confirm before calling delete_chat with confirmed=True."
                    ),
                ),
            )
            return

        try:
            self.agent.memory.validate_session_id(raw_id)
            sid = self.agent.memory.resolve_session_id(raw_id)
        except ValueError as exc:
            await self._respond(ws, req, error=str(exc))
            return

        if sid == VOICE_COMPANION_SESSION_ID:
            await self._respond(ws, req, error="Cannot delete the Voice Companion stream.")
            return
        if sid in self._busy_sessions:
            await self._respond(
                ws, req, error="Cannot delete conversation while a turn is running."
            )
            return

        try:
            await self._perform_conversation_delete(
                sid, delete_episodes=req.params.delete_episodes
            )
        except (ValueError, FileNotFoundError) as exc:
            await self._respond(ws, req, error=str(exc))
            return

        await self._respond(
            ws,
            req,
            METHOD_RESULTS["chat.delete"](
                ok=True,
                session_id=sid,
                deleted=True,
                requires_confirmation=False,
                episodes_removed=0,
                message=f"Deleted conversation {sid}.",
            ),
            session_id=sid,
        )

    async def _handle_memory_add_fact(self, conn: _Connection, req: Any) -> None:
        ws = conn.ws
        fact_text = req.params.fact.strip()
        if not fact_text:
            await self._respond(ws, req, error="Fact text must not be empty.")
            return
        section = _resolve_memory_section(req.params.category)
        try:
            entry = self.agent.memory.add_or_update_entry(
                text=fact_text,
                section=section,
                source="stated",
            )
        except ValueError as exc:
            await self._respond(ws, req, error=str(exc))
            return

        await self._broadcast(
            MemoryUpdatedEvent(
                data=MemoryUpdatedData(
                    action="added",
                    fact_id=entry.id,
                    section=entry.section,
                    text=entry.text,
                )
            )
        )
        await self._respond(
            ws,
            req,
            METHOD_RESULTS["memory.add_fact"](
                ok=True,
                fact=MemoryFactItem(
                    id=entry.id,
                    date=entry.date,
                    source=entry.source,
                    text=entry.text,
                ),
                section=entry.section,
            ),
        )

    async def _handle_memory_delete_fact(self, conn: _Connection, req: Any) -> None:
        ws = conn.ws
        query_or_id = req.params.query_or_id.strip()
        if not query_or_id:
            await self._respond(ws, req, error="query_or_id must not be empty.")
            return

        removed = self.agent.memory.remove_entries(query_or_id)
        removed_ids = [e.id for e in removed]
        if removed:
            await self._broadcast(
                MemoryUpdatedEvent(
                    data=MemoryUpdatedData(
                        action="deleted",
                        removed_count=len(removed),
                        removed_ids=removed_ids,
                    )
                )
            )
        await self._respond(
            ws,
            req,
            METHOD_RESULTS["memory.delete_fact"](
                ok=True,
                removed_count=len(removed),
                removed_ids=removed_ids,
            ),
        )

    async def _handle_activity_list(self, conn: _Connection, req: Any) -> None:
        ws = conn.ws
        limit = getattr(req.params, "limit", 10) or 10
        raw_tasks = self.agent.memory.load_background_tasks()
        records: dict[str, dict[str, Any]] = dict(raw_tasks)

        # Include live tracked tasks in case persistence hasn't flushed yet
        for tid, handle in list(self._active_task_handles.items()):
            if not handle.done() and tid not in records:
                meta = self._task_metadata.get(tid, {})
                records[tid] = {
                    "task_id": tid,
                    "title": meta.get("title", tid),
                    "status": "running",
                    "kind": meta.get("kind", "background_task"),
                    "started_at": meta.get("started_at", _now_iso()),
                    "finished_at": None,
                    "detail": None,
                }

        def _sort_key(rec: dict[str, Any]) -> tuple[int, str]:
            is_active = 1 if rec.get("status") in ("running", "queued") else 0
            ts = str(rec.get("finished_at") or rec.get("started_at") or "")
            return (is_active, ts)

        ordered = sorted(records.values(), key=_sort_key, reverse=True)[:limit]
        tasks: list[TaskStatusData] = []
        for rec in ordered:
            tasks.append(
                TaskStatusData(
                    task_id=str(rec.get("task_id", "")),
                    title=str(rec.get("title", "")),
                    status=rec.get("status", "failed"),  # type: ignore[arg-type]
                    started_at=str(rec.get("started_at", "")),
                    kind=rec.get("kind", "background_task"),  # type: ignore[arg-type]
                    finished_at=rec.get("finished_at"),
                    next_run_at=rec.get("next_run_at"),
                    detail=rec.get("detail"),
                )
            )
        await self._respond(ws, req, METHOD_RESULTS["activity.list"](tasks=tasks))

    async def _handle_activity_cancel(self, conn: _Connection, req: Any) -> None:
        ws = conn.ws
        task_id = req.params.task_id.strip()
        if not task_id:
            await self._respond(ws, req, error="task_id must not be empty.")
            return

        now_iso = _now_iso()
        handle = self._active_task_handles.get(task_id)
        if handle is not None and not handle.done():
            meta = self._task_metadata.get(task_id, {})
            title = str(meta.get("title") or task_id)
            kind = str(meta.get("kind") or "background_task")
            started_at = str(meta.get("started_at") or now_iso)
            session_id = meta.get("session_id")
            self._cancelled_task_ids.add(task_id)
            handle.cancel()
            with contextlib.suppress(Exception):
                self.agent.memory.record_background_task(
                    task_id=task_id,
                    title=title,
                    status="cancelled",
                    started_at=started_at,
                    finished_at=now_iso,
                    kind=kind,
                    detail="Cancelled by user",
                    session_id=session_id,
                )
            await self._broadcast(
                TaskStatusEvent(
                    session_id=session_id,
                    data=TaskStatusData(
                        task_id=task_id,
                        title=title,
                        status="cancelled",
                        started_at=started_at,
                        finished_at=now_iso,
                        kind=kind,  # type: ignore[arg-type]
                        detail="Cancelled by user",
                    ),
                )
            )
            await self._respond(
                ws,
                req,
                METHOD_RESULTS["activity.cancel"](
                    ok=True,
                    task_id=task_id,
                    status="cancelled",
                    message=f"Cancelled task '{task_id}'.",
                ),
            )
            return

        existing = self.agent.memory.load_background_tasks().get(task_id)
        if existing is not None:
            prev_status = str(existing.get("status", ""))
            if prev_status in ("running", "queued"):
                title = str(existing.get("title") or task_id)
                kind = str(existing.get("kind") or "background_task")
                started_at = str(existing.get("started_at") or now_iso)
                session_id = existing.get("session_id")
                self.agent.memory.record_background_task(
                    task_id=task_id,
                    title=title,
                    status="cancelled",
                    started_at=started_at,
                    finished_at=now_iso,
                    kind=kind,
                    detail="Cancelled by user",
                    session_id=session_id,
                )
                await self._broadcast(
                    TaskStatusEvent(
                        session_id=session_id,
                        data=TaskStatusData(
                            task_id=task_id,
                            title=title,
                            status="cancelled",
                            started_at=started_at,
                            finished_at=now_iso,
                            kind=kind,  # type: ignore[arg-type]
                            detail="Cancelled by user",
                        ),
                    )
                )
                await self._respond(
                    ws,
                    req,
                    METHOD_RESULTS["activity.cancel"](
                        ok=True,
                        task_id=task_id,
                        status="cancelled",
                        message=f"Cancelled task '{task_id}'.",
                    ),
                )
                return

            await self._respond(
                ws,
                req,
                METHOD_RESULTS["activity.cancel"](
                    ok=False,
                    task_id=task_id,
                    status=prev_status or "unknown",
                    message=f"Task '{task_id}' is already {prev_status} and cannot be cancelled.",
                ),
            )
            return

        await self._respond(
            ws,
            req,
            METHOD_RESULTS["activity.cancel"](
                ok=False,
                task_id=task_id,
                status="not_found",
                message=f"Task '{task_id}' not found.",
            ),
        )

    async def _handle_os_open_or_focus_app(self, conn: _Connection, req: Any) -> None:
        ws = conn.ws
        try:
            res = await asyncio.to_thread(
                open_or_focus_app,
                req.params.app_name,
                req.params.target_path,
            )
        except (ValueError, OSError, RuntimeError, TimeoutError) as exc:
            await self._respond(ws, req, error=str(exc))
            return
        self._audit_log(
            "os_open_or_focus_app",
            app=res.get("app"),
            status=res.get("status"),
        )
        await self._respond(ws, req, METHOD_RESULTS["os.open_or_focus_app"](**res))

    async def _handle_os_control_media_volume(self, conn: _Connection, req: Any) -> None:
        ws = conn.ws
        try:
            res = await asyncio.to_thread(
                control_media_volume,
                req.params.action,
                req.params.level,
            )
        except (ValueError, OSError, RuntimeError, TimeoutError) as exc:
            await self._respond(ws, req, error=str(exc))
            return
        self._audit_log(
            "os_control_media_volume",
            media_action=res.get("action"),
            level=res.get("level"),
        )
        await self._respond(ws, req, METHOD_RESULTS["os.control_media_volume"](**res))

    async def _handle_os_read_clipboard(self, conn: _Connection, req: Any) -> None:
        ws = conn.ws
        try:
            res = await asyncio.to_thread(read_clipboard, req.params.max_chars)
        except (ValueError, OSError, RuntimeError, TimeoutError) as exc:
            await self._respond(ws, req, error=str(exc))
            return
        self._audit_log(
            "os_read_clipboard",
            char_count=res.get("char_count"),
            truncated=res.get("truncated"),
        )
        await self._respond(ws, req, METHOD_RESULTS["os.read_clipboard"](**res))

    async def _handle_os_write_clipboard(self, conn: _Connection, req: Any) -> None:
        ws = conn.ws
        try:
            res = await asyncio.to_thread(write_clipboard, req.params.text)
        except (ValueError, OSError, RuntimeError, TimeoutError) as exc:
            await self._respond(ws, req, error=str(exc))
            return
        self._audit_log("os_write_clipboard", char_count=res.get("char_count"))
        await self._respond(ws, req, METHOD_RESULTS["os.write_clipboard"](**res))

    async def _handle_workspace_git_inspect(self, conn: _Connection, req: Any) -> None:
        ws = conn.ws
        try:
            res = await asyncio.to_thread(
                inspect_git_workspace,
                req.params.repo_path,
                req.params.include_diff,
                req.params.diff_stat_only,
            )
        except (ValueError, OSError, RuntimeError, TimeoutError, subprocess.SubprocessError) as exc:
            await self._respond(ws, req, error=str(exc))
            return
        self._audit_log(
            "workspace_git_inspect",
            repo_path=res.get("repo_path"),
            branch=res.get("branch"),
            is_clean=res.get("is_clean"),
        )
        await self._respond(ws, req, METHOD_RESULTS["workspace.git_inspect"](**res))

    def _settings(self) -> SettingsGetResult:
        policies = {
            RiskLevel.READ_ONLY: "auto_allow",
            RiskLevel.REVERSIBLE: "ask",
            RiskLevel.DESTRUCTIVE: "ask_confirm",
        }
        for level, policy in policies.items():  # keep the display in step with the registry
            assert (policy != "auto_allow") == requires_approval(level)
        return SettingsGetResult(
            provider=self.agent.provider.provider_name,
            model=self.agent.provider.model_name,
            host=self.host,
            port=self.bound_port,
            approval_timeout_seconds=self.approval_timeout,
            tools=[
                ToolInfo(name=t.name, description=t.description, risk_level=t.risk_level.value)
                for t in self.agent.registry.list_definitions()
            ],
            permission_tiers=[
                PermissionTierInfo(tier=level.value, policy=policy)
                for level, policy in policies.items()
            ],
        )

    # ------------------------------------------------------------------ chat turns

    async def _run_chat(self, conn: _Connection, req: Any, text: str) -> None:
        """Run one chat turn as its own task so the connection keeps reading (approvals)."""
        with contextlib.suppress(ConnectionClosed):
            await self._chat_turn(conn, req, text)

    async def _run_voice_chat_turn(
        self,
        *,
        session: Session,
        request_id: str,
        text: str,
        title: str | None = None,
    ) -> None:
        """Run a voice-triggered chat turn asynchronously and stream tokens to UI clients."""
        try:
            with contextlib.suppress(ConnectionClosed, asyncio.CancelledError):
                await self._execute_chat_turn(
                    session=session,
                    request_id=request_id,
                    text=text,
                    title=title or " ".join(text.split())[:80],
                    conn=None,
                    req=None,
                    broadcast_all_ui=True,
                    notify_turn_start=False,
                    busy_already_acquired=True,
                )
        finally:
            self._busy_sessions.discard(session.id)

    async def _chat_turn(self, conn: _Connection, req: Any, text: str) -> None:
        ws = conn.ws
        session_id = req.session_id
        if session_id:
            try:
                self.agent.memory.validate_session_id(session_id)
                if session_id == VOICE_COMPANION_SESSION_ID:
                    await self._respond(
                        ws, req, error="Cannot send chat messages to the Voice Companion stream."
                    )
                    return
            except ValueError as exc:
                await self._respond(ws, req, error=str(exc))
                return

        if session_id and self.sessions.get(session_id) is None:
            try:
                session = self._resume_session_from_transcript(session_id)
            except Exception:
                session = self.sessions.get_or_create(session_id)
        else:
            session = self.sessions.get_or_create(session_id)

        conn.current_session_id = session.id
        if session.id in self._busy_sessions:
            await self._respond(
                ws, req, error="A turn is already running for this session.", session_id=session.id
            )
            return
        await self._execute_chat_turn(
            session=session,
            request_id=req.id,
            text=text,
            title=" ".join(text.split())[:80],
            conn=conn,
            req=req,
            broadcast_all_ui=False,
            notify_turn_start=True,
        )

    async def _execute_chat_turn(
        self,
        *,
        session: Session,
        request_id: str,
        text: str,
        title: str,
        conn: _Connection | None,
        req: Any,
        broadcast_all_ui: bool,
        notify_turn_start: bool,
        busy_already_acquired: bool = False,
    ) -> None:
        ws = conn.ws if conn is not None else None
        if not busy_already_acquired:
            if session.id in self._busy_sessions:
                if ws is not None and req is not None:
                    await self._respond(
                        ws,
                        req,
                        error="A turn is already running for this session.",
                        session_id=session.id,
                    )
                return
            self._busy_sessions.add(session.id)

        state = _TurnState()
        started_at = _now_iso()
        current_task = asyncio.current_task()
        if current_task is not None:
            self._active_task_handles[request_id] = current_task
            self._task_metadata[request_id] = {
                "title": title,
                "kind": "chat_turn",
                "started_at": started_at,
                "session_id": session.id,
            }

        async def broadcast_session_event(event: Any) -> None:
            for c in list(self._connections):
                if (
                    c.current_session_id == session.id
                    or (ws is not None and c.ws == ws)
                    or (broadcast_all_ui and not c.is_voice)
                ):
                    with contextlib.suppress(ConnectionClosed):
                        await self._send(c.ws, event)

        if notify_turn_start:
            for c in list(self._connections):
                if (ws is None or c.ws != ws) and c.current_session_id == session.id:
                    with contextlib.suppress(ConnectionClosed):
                        await self._send(
                            c.ws,
                            ConversationUpdatedEvent(
                                session_id=session.id,
                                data=ConversationUpdatedData(
                                    session_id=session.id,
                                    user_message=text,
                                    request_id=request_id,
                                ),
                            ),
                        )

        async def task_status(status: str, detail: str | None = None) -> None:
            finished_at = None if status == "running" else _now_iso()
            if broadcast_all_ui:
                with contextlib.suppress(Exception):
                    self.agent.memory.record_background_task(
                        task_id=request_id,
                        title=title,
                        status=status,
                        started_at=started_at,
                        finished_at=finished_at,
                        kind="chat_turn",
                        detail=detail,
                        session_id=session.id,
                    )
            await broadcast_session_event(
                TaskStatusEvent(
                    request_id=request_id,
                    session_id=session.id,
                    data=TaskStatusData(
                        task_id=request_id,
                        title=title,
                        status=status,  # type: ignore[arg-type]
                        started_at=started_at,
                        finished_at=finished_at,
                        kind="chat_turn",
                        detail=detail,
                    ),
                ),
            )

        async def on_text_delta(delta: str) -> None:
            await broadcast_session_event(
                AssistantTokenEvent(
                    request_id=request_id,
                    session_id=session.id,
                    data=AssistantTokenData(text=delta),
                ),
            )

        def risk_of(name: str) -> str:
            try:
                return self.agent.registry.get(name).risk_level.value
            except KeyError:
                return RiskLevel.REVERSIBLE.value

        async def on_tool_start(call: ToolCall) -> None:
            state.current_call_id = call.id
            state.started[call.id] = time.monotonic()
            state.approval_wait[call.id] = 0.0
            await broadcast_session_event(
                ToolCallStartedEvent(
                    request_id=request_id,
                    session_id=session.id,
                    data=ToolCallStartedData(
                        call_id=call.id,
                        name=call.name,
                        arguments=call.arguments,
                        risk_level=risk_of(call.name),  # type: ignore[arg-type]
                    ),
                ),
            )

        async def on_tool_end(call: ToolCall, output: str, is_error: bool) -> None:
            elapsed = time.monotonic() - state.started.pop(call.id, time.monotonic())
            elapsed -= state.approval_wait.pop(call.id, 0.0)
            await broadcast_session_event(
                ToolCallFinishedEvent(
                    request_id=request_id,
                    session_id=session.id,
                    data=ToolCallFinishedData(
                        call_id=call.id,
                        name=call.name,
                        is_error=is_error,
                        output=output,
                        duration_ms=max(0, round(elapsed * 1000)),
                    ),
                ),
            )

        async def confirm(definition: ToolDefinition, arguments: dict[str, Any]) -> bool:
            approval_id = new_id()
            call_id = state.current_call_id
            future: asyncio.Future[ApprovalDecision] = asyncio.get_running_loop().create_future()
            if conn is not None:
                conn.approvals[approval_id] = future
            self._shared_approvals[approval_id] = future
            await broadcast_session_event(
                ApprovalRequestedEvent(
                    request_id=request_id,
                    session_id=session.id,
                    data=ApprovalRequestedData(
                        approval_id=approval_id,
                        call_id=call_id,
                        tool=definition.name,
                        arguments=arguments,
                        risk_level=definition.risk_level.value,
                        timeout_seconds=self.approval_timeout,
                    ),
                ),
            )
            waited_from = time.monotonic()
            decision: ApprovalDecision
            try:
                decision = await asyncio.wait_for(future, self.approval_timeout)
                reason = "user"
            except TimeoutError:
                decision, reason = "deny", "timeout"
            finally:
                if conn is not None:
                    conn.approvals.pop(approval_id, None)
                self._shared_approvals.pop(approval_id, None)
                if call_id is not None:
                    state.approval_wait[call_id] = state.approval_wait.get(call_id, 0.0) + (
                        time.monotonic() - waited_from
                    )
            await broadcast_session_event(
                ApprovalResolvedEvent(
                    request_id=request_id,
                    session_id=session.id,
                    data=ApprovalResolvedData(
                        approval_id=approval_id,
                        decision=decision,
                        reason=reason,  # type: ignore[arg-type]
                    ),
                ),
            )
            return decision == "allow"

        try:
            await task_status("running")
            try:
                reply = await self.agent.run_turn(
                    session=session,
                    user_text=text,
                    on_text_delta=on_text_delta,
                    on_tool_start=on_tool_start,
                    on_tool_end=on_tool_end,
                    confirm=confirm,
                )
            except asyncio.CancelledError:
                if request_id not in self._cancelled_task_ids:
                    with contextlib.suppress(Exception):
                        await task_status("cancelled", "Cancelled by user")
                with contextlib.suppress(Exception):
                    await broadcast_session_event(
                        ErrorEvent(
                            request_id=request_id,
                            session_id=session.id,
                            data=ErrorData(message="Turn cancelled by user", code="cancelled"),
                        )
                    )
                raise
            except ConnectionClosed:
                raise
            except Exception as exc:
                logger.exception("Error while running agent turn")
                await task_status("failed", str(exc))
                await broadcast_session_event(
                    ErrorEvent(
                        request_id=request_id,
                        session_id=session.id,
                        data=ErrorData(message=str(exc), code="turn_failed"),
                    ),
                )
                if ws is not None and req is not None:
                    await self._respond(ws, req, error=str(exc), session_id=session.id)
                return

            await task_status("succeeded")
            for c in list(self._connections):
                if (ws is None or c.ws != ws) and (not c.is_voice or not broadcast_all_ui):
                    with contextlib.suppress(ConnectionClosed):
                        await self._send(
                            c.ws,
                            ConversationUpdatedEvent(
                                session_id=session.id,
                                data=ConversationUpdatedData(
                                    session_id=session.id,
                                    assistant_message=reply,
                                    turns=(len(session.messages) + 1) // 2,
                                    request_id=request_id,
                                ),
                            ),
                        )
            if ws is not None and req is not None:
                await self._respond(
                    ws, req, METHOD_RESULTS["chat.send"](text=reply), session_id=session.id
                )
        finally:
            if self._active_task_handles.get(request_id) is asyncio.current_task():
                self._active_task_handles.pop(request_id, None)
                self._task_metadata.pop(request_id, None)
            self._cancelled_task_ids.discard(request_id)
            self._busy_sessions.discard(session.id)
            self._reset_session_idle_timer(session.id)

    # ------------------------------------------------------------------ delegated voice tasks

    async def _run_delegated_task(self, conn: _Connection, req: Any, description: str) -> None:
        """Run a voice-delegated task via AgentLoop in the background."""
        with contextlib.suppress(ConnectionClosed):
            await self._delegated_task(conn, req, description)

    async def _delegated_task(self, conn: _Connection, req: Any, description: str) -> None:
        ws = conn.ws
        target_sid = req.session_id
        if target_sid == VOICE_COMPANION_SESSION_ID:
            target_sid = None
        session = (
            self.sessions.get_or_create(target_sid)
            if target_sid is not None
            else self.sessions.create()
        )
        if session.id in self._busy_sessions:
            await self._respond(
                ws, req, error="A turn is already running for this session.", session_id=session.id
            )
            return
        self._busy_sessions.add(session.id)

        state = _TurnState()
        started_at = _now_iso()
        title = " ".join(description.split())[:80]
        task_started = False
        task_finished = False
        pending_approval_id: str | None = None
        current_task = asyncio.current_task()
        if current_task is not None:
            self._active_task_handles[req.id] = current_task
            self._task_metadata[req.id] = {
                "title": title,
                "kind": "agent_run",
                "started_at": started_at,
                "session_id": session.id,
            }

        async def task_status(status: str, detail: str | None = None) -> None:
            finished_at = None if status == "running" else _now_iso()
            with contextlib.suppress(Exception):
                self.agent.memory.record_background_task(
                    task_id=req.id,
                    title=title,
                    status=status,
                    started_at=started_at,
                    finished_at=finished_at,
                    kind="agent_run",
                    detail=detail,
                    session_id=session.id,
                )
            await self._broadcast(
                TaskStatusEvent(
                    request_id=req.id,
                    session_id=session.id,
                    data=TaskStatusData(
                        task_id=req.id,
                        title=title,
                        status=status,  # type: ignore[arg-type]
                        started_at=started_at,
                        finished_at=finished_at,
                        kind="agent_run",
                        detail=detail,
                    ),
                )
            )

        def risk_of(name: str) -> str:
            try:
                return self.agent.registry.get(name).risk_level.value
            except KeyError:
                return RiskLevel.REVERSIBLE.value

        async def on_tool_start(call: ToolCall) -> None:
            state.current_call_id = call.id
            state.started[call.id] = time.monotonic()
            state.approval_wait[call.id] = 0.0
            with contextlib.suppress(ConnectionClosed):
                await self._send(
                    ws,
                    ToolCallStartedEvent(
                        request_id=req.id,
                        session_id=session.id,
                        data=ToolCallStartedData(
                            call_id=call.id,
                            name=call.name,
                            arguments=call.arguments,
                            risk_level=risk_of(call.name),  # type: ignore[arg-type]
                        ),
                    ),
                )

        async def on_tool_end(call: ToolCall, output: str, is_error: bool) -> None:
            elapsed = time.monotonic() - state.started.pop(call.id, time.monotonic())
            elapsed -= state.approval_wait.pop(call.id, 0.0)
            with contextlib.suppress(ConnectionClosed):
                await self._send(
                    ws,
                    ToolCallFinishedEvent(
                        request_id=req.id,
                        session_id=session.id,
                        data=ToolCallFinishedData(
                            call_id=call.id,
                            name=call.name,
                            is_error=is_error,
                            output=output,
                            duration_ms=max(0, round(elapsed * 1000)),
                        ),
                    ),
                )

        async def confirm(definition: ToolDefinition, arguments: dict[str, Any]) -> bool:
            nonlocal pending_approval_id
            approval_id = new_id()
            call_id = state.current_call_id
            future: asyncio.Future[ApprovalDecision] = asyncio.get_running_loop().create_future()
            conn.approvals[approval_id] = future
            self._shared_approvals[approval_id] = future
            pending_approval_id = approval_id
            await self._broadcast(
                ApprovalRequestedEvent(
                    request_id=req.id,
                    session_id=session.id,
                    data=ApprovalRequestedData(
                        approval_id=approval_id,
                        call_id=call_id,
                        tool=definition.name,
                        arguments=arguments,
                        risk_level=definition.risk_level.value,
                        timeout_seconds=self.approval_timeout,
                    ),
                )
            )
            waited_from = time.monotonic()
            decision: ApprovalDecision
            try:
                decision = await asyncio.wait_for(future, self.approval_timeout)
                reason = "user"
            except TimeoutError:
                decision, reason = "deny", "timeout"
            finally:
                conn.approvals.pop(approval_id, None)
                self._shared_approvals.pop(approval_id, None)
                if call_id is not None:
                    state.approval_wait[call_id] = state.approval_wait.get(call_id, 0.0) + (
                        time.monotonic() - waited_from
                    )
            await self._broadcast(
                ApprovalResolvedEvent(
                    request_id=req.id,
                    session_id=session.id,
                    data=ApprovalResolvedData(
                        approval_id=approval_id,
                        decision=decision,
                        reason=reason,  # type: ignore[arg-type]
                    ),
                ),
            )
            pending_approval_id = None
            return decision == "allow"

        try:
            await task_status("running")
            task_started = True
            try:
                reply = await self.agent.run_turn(
                    session=session,
                    user_text=description,
                    on_tool_start=on_tool_start,
                    on_tool_end=on_tool_end,
                    confirm=confirm,
                )
            except (ConnectionClosed, asyncio.CancelledError):
                if pending_approval_id is not None:
                    with contextlib.suppress(Exception):
                        await self._broadcast(
                            ApprovalResolvedEvent(
                                request_id=req.id,
                                session_id=session.id,
                                data=ApprovalResolvedData(
                                    approval_id=pending_approval_id,
                                    decision="deny",
                                    reason="timeout",
                                ),
                            )
                        )
                    pending_approval_id = None
                if task_started and not task_finished:
                    task_finished = True
                    was_cancelled = req.id in self._cancelled_task_ids
                    if not was_cancelled:
                        with contextlib.suppress(Exception):
                            await task_status("failed", "Cancelled")
                raise
            except Exception as exc:
                logger.exception("Error while running delegated voice task")
                task_finished = True
                await task_status("failed", str(exc))
                with contextlib.suppress(ConnectionClosed):
                    await self._send(
                        ws,
                        ErrorEvent(
                            request_id=req.id,
                            session_id=session.id,
                            data=ErrorData(message=str(exc), code="turn_failed"),
                        ),
                    )
                    await self._respond(ws, req, error=str(exc), session_id=session.id)
                return

            task_finished = True
            await task_status("succeeded")
            with contextlib.suppress(ConnectionClosed):
                await self._respond(
                    ws,
                    req,
                    METHOD_RESULTS["voice.delegate_task"](
                        task_id=req.id, status="succeeded", output=reply
                    ),
                    session_id=session.id,
                )
        finally:
            if self._active_task_handles.get(req.id) is asyncio.current_task():
                self._active_task_handles.pop(req.id, None)
                self._task_metadata.pop(req.id, None)
            self._cancelled_task_ids.discard(req.id)
            self._busy_sessions.discard(session.id)


def build_daemon(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    provider: LLMProvider | None = None,
    memory: MemoryStore | None = None,
    registry: ToolRegistry | None = None,
    token: str | None = None,
    token_file: Path | None = None,
    lock_file: Path | None = None,
    allowed_origins: Iterable[str] | None = None,
    approval_timeout: float = DEFAULT_APPROVAL_TIMEOUT_SECONDS,
) -> CleaDaemon:
    """Assemble a CleaDaemon wired with default memory, tools, and Gemini provider."""
    resolved_memory = memory or MemoryStore()
    resolved_registry = registry or ToolRegistry()
    if not resolved_registry.list_definitions():
        register_builtin_tools(resolved_registry)
    try:
        resolved_registry.get("remember")
    except KeyError:
        register_memory_tools(resolved_registry, resolved_memory)
    resolved_provider = provider or GeminiProvider()
    agent = AgentLoop(
        provider=resolved_provider,
        registry=resolved_registry,
        memory=resolved_memory,
    )
    return CleaDaemon(
        agent=agent,
        host=host,
        port=port,
        token=token,
        token_file=token_file,
        lock_file=lock_file,
        allowed_origins=allowed_origins,
        approval_timeout=approval_timeout,
    )


def daemon_log_path(data_dir: Path | None = None) -> Path:
    """Return the path to the rotating background daemon log file."""
    mem = MemoryStore(base_dir=data_dir)
    return mem.base_dir / "logs" / "daemon.log"


def configure_daemon_logging(*, background: bool, data_dir: Path | None = None) -> Path | None:
    """Configure console logging (foreground) or rotating file logging (background)."""
    fmt = "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    if not background:
        logging.basicConfig(level=logging.INFO, format=fmt)
        return None

    log_file = daemon_log_path(data_dir)
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = logging.handlers.RotatingFileHandler(
        log_file,
        maxBytes=5 * 1024 * 1024,
        backupCount=3,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(fmt))
    logging.basicConfig(level=logging.INFO, handlers=[handler], force=True)
    return log_file


def run_daemon(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    background: bool = False,
) -> None:
    """Blocking entrypoint used by ``clea daemon``."""
    configure_daemon_logging(background=background)
    try:
        daemon = build_daemon(host=host, port=port)
        with contextlib.suppress(KeyboardInterrupt):
            asyncio.run(daemon.serve_forever())
    except Exception:
        if background:
            logger.exception("Clea background daemon failed")
        raise


def get_daemon_info() -> dict[str, Any]:
    """Return non-sensitive runtime paths for display at daemon startup."""
    mem = MemoryStore()
    return {
        "data_dir": str(mem.base_dir),
        "daily_log": str(mem.daily_log_path()),
        "token_file": str(auth.token_path(mem.base_dir)),
        "daemon_log": str(daemon_log_path(mem.base_dir)),
        "allowed_origins": sorted(auth.allowed_origins_from_env()),
    }

"""Localhost-only WebSocket daemon owning the agent loop, tools, and memory."""

from __future__ import annotations

import asyncio
import contextlib
import logging
import logging.handlers
import os
import time
from collections.abc import Awaitable, Iterable
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
    ConversationDeletedData,
    ConversationDeletedEvent,
    ConversationResumeMessage,
    ConversationUpdatedData,
    ConversationUpdatedEvent,
    EpisodeItem,
    ErrorData,
    ErrorEvent,
    MemoryArchiveItem,
    MemoryFactItem,
    MemorySectionItem,
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
    VoiceState,
    VoiceStateData,
    VoiceStateEvent,
    new_id,
    parse_frame,
)
from clea.core.sessions import Session, SessionManager
from clea.memory.consolidation import ConsolidationManager
from clea.memory.store import MemoryStore
from clea.providers.base import LLMProvider, Message, ToolCall
from clea.providers.gemini import GeminiProvider
from clea.tools import (
    RiskLevel,
    ToolRegistry,
    register_builtin_tools,
    register_memory_tools,
    requires_approval,
)
from clea.tools.registry import ToolDefinition

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
        self._voice_call_id: str | None = None
        self._call_state: CallState = "idle"
        self._call_started_monotonic: float | None = None
        self._voice_state: VoiceState = "idle"
        self._screen_capture_active: bool = False
        self._screen_capture_target: ScreenCaptureTarget | None = None
        self.consolidation = ConsolidationManager(
            memory=agent.memory,
            provider=agent.provider,
            min_user_turns=int(os.environ.get("CLEA_CONSOLIDATION_MIN_TURNS", "2")),
        )
        self.consolidation_idle_seconds = 900.0
        self._idle_tasks: dict[str, asyncio.Task[None]] = {}
        self._startup_task: asyncio.Task[None] | None = None

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
            self._startup_task = asyncio.create_task(self._run_startup_consolidation())
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
        if self._startup_task is not None and not self._startup_task.done():
            self._startup_task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await self._startup_task
        for t in list(self._idle_tasks.values()):
            if not t.done():
                t.cancel()
        self._idle_tasks.clear()

        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
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

    def _format_session_consolidation_title(self, session_id: str) -> str:
        """Generate title like 'Summarising session 18:11' based on session start time."""
        time_str = ""
        try:
            tr = self.agent.memory.get_transcript(session_id)
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
        title = self._format_session_consolidation_title(session_id)
        now_iso = _now_iso()
        finished_at = now_iso if status in ("succeeded", "failed") else None
        await self._broadcast(
            TaskStatusEvent(
                data=TaskStatusData(
                    task_id=f"cons-{session_id}",
                    title=title,
                    status=status,  # type: ignore[arg-type]
                    started_at=now_iso,
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
        except Exception:
            logger.exception("Error during startup memory consolidation crash recovery")

    def _reset_session_idle_timer(self, session_id: str) -> None:
        old_task = self._idle_tasks.pop(session_id, None)
        if old_task is not None and not old_task.done():
            old_task.cancel()

        async def _timer() -> None:
            try:
                await asyncio.sleep(self.consolidation_idle_seconds)
                await self.consolidation.consolidate_session(
                    session_id,
                    force=True,
                    on_task_status=lambda st, det=None: self._emit_consolidation_task_status(
                        session_id, st, det
                    ),
                )
            except asyncio.CancelledError:
                pass
            except Exception:
                logger.exception("Error consolidating session %s after idle timeout", session_id)
            finally:
                self._idle_tasks.pop(session_id, None)

        self._idle_tasks[session_id] = asyncio.create_task(_timer())

    def _schedule_consolidation_now(self, session_id: str) -> None:
        old_task = self._idle_tasks.pop(session_id, None)
        if old_task is not None and not old_task.done():
            old_task.cancel()

        async def _run() -> None:
            try:
                await self.consolidation.consolidate_session(
                    session_id,
                    force=True,
                    on_task_status=lambda st, det=None: self._emit_consolidation_task_status(
                        session_id, st, det
                    ),
                )
            except Exception:
                logger.exception("Error consolidating session %s on end/switch", session_id)

        asyncio.create_task(_run())

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
                self._call_state = "idle"
                self._voice_state = "idle"
                await self._broadcast(
                    VoiceStateEvent(
                        data=VoiceStateData(state="error", error="Voice process disconnected.")
                    )
                )
                await self._broadcast(
                    CallEndedEvent(
                        data=CallEndedData(
                            call_id=self._voice_call_id or "",
                            call_state="idle",
                            reason="voice_process_disconnected",
                            ended_at=_now_iso(),
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

        asyncio.create_task(_purge_after_delay())

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
            if session_id in self._busy_sessions:
                await self._respond(
                    ws, req, error="Cannot delete conversation while a turn is running."
                )
                return
            delete_episodes = req.params.delete_episodes
            try:
                self.agent.memory.delete_transcript(session_id, delete_episodes=delete_episodes)
            except (ValueError, FileNotFoundError) as exc:
                await self._respond(ws, req, error=str(exc))
                return

            self._audit_log(
                "conversation_delete",
                session_id=session_id,
                delete_episodes=delete_episodes,
            )
            self.sessions._sessions.pop(session_id, None)
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
            memory_ctx = self.agent.memory.build_injected_context()
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

            # Persist transcript to logs/transcripts/ and schedule consolidation if provided
            if getattr(req.params, "transcript", None):
                for entry in req.params.transcript:
                    role = entry.get("role", "assistant")
                    text = str(entry.get("text", "")).strip()
                    if text:
                        self.agent.memory.append_transcript_message(
                            session_id=ended_id,
                            role=role,
                            text=text,
                        )
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
        elif method == "voice.delegate_task":
            description = req.params.description.strip()
            if not description:
                await self._respond(ws, req, error="Missing non-empty 'description' parameter.")
                return
            conn.spawn(self._run_delegated_task(conn, req, description))

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

    async def _chat_turn(self, conn: _Connection, req: Any, text: str) -> None:
        ws = conn.ws
        session_id = req.session_id
        if session_id:
            try:
                self.agent.memory.validate_session_id(session_id)
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
        self._busy_sessions.add(session.id)

        state = _TurnState()
        started_at = _now_iso()
        title = " ".join(text.split())[:80]

        async def broadcast_session_event(event: Any) -> None:
            for c in list(self._connections):
                if c.current_session_id == session.id or c.ws == ws:
                    await self._send(c.ws, event)

        # Notify other connections of turn start
        for c in list(self._connections):
            if c.ws != ws and c.current_session_id == session.id:
                await self._send(
                    c.ws,
                    ConversationUpdatedEvent(
                        session_id=session.id,
                        data=ConversationUpdatedData(
                            session_id=session.id,
                            user_message=text,
                            request_id=req.id,
                        ),
                    ),
                )

        async def task_status(status: str, detail: str | None = None) -> None:
            await broadcast_session_event(
                TaskStatusEvent(
                    request_id=req.id,
                    session_id=session.id,
                    data=TaskStatusData(
                        task_id=req.id,
                        title=title,
                        status=status,  # type: ignore[arg-type]
                        started_at=started_at,
                        finished_at=None if status == "running" else _now_iso(),
                        kind="chat_turn",
                        detail=detail,
                    ),
                ),
            )

        async def on_text_delta(delta: str) -> None:
            await broadcast_session_event(
                AssistantTokenEvent(
                    request_id=req.id, session_id=session.id, data=AssistantTokenData(text=delta)
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
            await broadcast_session_event(
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
            approval_id = new_id()
            call_id = state.current_call_id
            future: asyncio.Future[ApprovalDecision] = asyncio.get_running_loop().create_future()
            conn.approvals[approval_id] = future
            self._shared_approvals[approval_id] = future
            await broadcast_session_event(
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
                conn.approvals.pop(approval_id, None)
                self._shared_approvals.pop(approval_id, None)
                if call_id is not None:
                    state.approval_wait[call_id] = state.approval_wait.get(call_id, 0.0) + (
                        time.monotonic() - waited_from
                    )
            await broadcast_session_event(
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
            except (ConnectionClosed, asyncio.CancelledError):
                raise
            except Exception as exc:
                logger.exception("Error while running agent turn")
                await task_status("failed", str(exc))
                await broadcast_session_event(
                    ErrorEvent(
                        request_id=req.id,
                        session_id=session.id,
                        data=ErrorData(message=str(exc), code="turn_failed"),
                    ),
                )
                await self._respond(ws, req, error=str(exc), session_id=session.id)
                return

            await task_status("succeeded")
            for c in list(self._connections):
                if c.ws != ws:
                    await self._send(
                        c.ws,
                        ConversationUpdatedEvent(
                            session_id=session.id,
                            data=ConversationUpdatedData(
                                session_id=session.id,
                                assistant_message=reply,
                                turns=(len(session.messages) + 1) // 2,
                                request_id=req.id,
                            ),
                        ),
                    )
            await self._respond(
                ws, req, METHOD_RESULTS["chat.send"](text=reply), session_id=session.id
            )
        finally:
            self._busy_sessions.discard(session.id)
            self._reset_session_idle_timer(session.id)

    # ------------------------------------------------------------------ delegated voice tasks

    async def _run_delegated_task(self, conn: _Connection, req: Any, description: str) -> None:
        """Run a voice-delegated task via AgentLoop in the background."""
        with contextlib.suppress(ConnectionClosed):
            await self._delegated_task(conn, req, description)

    async def _delegated_task(self, conn: _Connection, req: Any, description: str) -> None:
        ws = conn.ws
        session = (
            self.sessions.get_or_create(req.session_id)
            if req.session_id is not None
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

        async def task_status(status: str, detail: str | None = None) -> None:
            await self._broadcast(
                TaskStatusEvent(
                    request_id=req.id,
                    session_id=session.id,
                    data=TaskStatusData(
                        task_id=req.id,
                        title=title,
                        status=status,  # type: ignore[arg-type]
                        started_at=started_at,
                        finished_at=None if status == "running" else _now_iso(),
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

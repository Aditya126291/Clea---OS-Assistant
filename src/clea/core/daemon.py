"""Localhost-only WebSocket daemon owning the agent loop, tools, and memory."""

from __future__ import annotations

import asyncio
import contextlib
import logging
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
    ErrorData,
    ErrorEvent,
    PermissionTierInfo,
    ResponseFrame,
    SettingsGetResult,
    TaskStatusData,
    TaskStatusEvent,
    ToolCallFinishedData,
    ToolCallFinishedEvent,
    ToolCallStartedData,
    ToolCallStartedEvent,
    ToolInfo,
    new_id,
    parse_frame,
)
from clea.core.sessions import SessionManager
from clea.memory.store import MemoryStore
from clea.providers.base import LLMProvider, ToolCall
from clea.providers.gemini import GeminiProvider
from clea.tools import RiskLevel, ToolRegistry, register_builtin_tools, requires_approval
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
        allowed_origins: Iterable[str] | None = None,
        approval_timeout: float = DEFAULT_APPROVAL_TIMEOUT_SECONDS,
    ) -> None:
        self.host = _validate_localhost(host)
        self.port = port
        self.agent = agent
        self.sessions = sessions or SessionManager()
        self.token = token or auth.generate_token()
        self.token_file = token_file or auth.token_path(agent.memory.base_dir)
        self.allowed_origins = (
            auth.normalize_origins(allowed_origins)
            if allowed_origins is not None
            else auth.allowed_origins_from_env()
        )
        self.approval_timeout = approval_timeout
        self._server: Server | None = None
        self._busy_sessions: set[str] = set()

    @property
    def bound_port(self) -> int:
        """Return the actual TCP port the daemon is listening on."""
        if self._server and self._server.sockets:
            return int(self._server.sockets[0].getsockname()[1])
        return self.port

    async def start(self) -> None:
        """Write the session token and start listening for localhost WebSocket connections."""
        self.agent.memory.ensure_initialized()
        auth.write_token(self.token, self.token_file)
        self._server = await serve(
            self._handle_connection,
            self.host,
            self.port,
            process_request=self._authenticate,
            select_subprotocol=self._select_subprotocol,
        )
        logger.info("Clea daemon listening on ws://%s:%d", self.host, self.bound_port)

    async def stop(self) -> None:
        """Gracefully close the WebSocket server and remove the token file."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None
        auth.remove_token(self.token_file, self.token)

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

    async def _handle_connection(self, ws: ServerConnection) -> None:
        conn = _Connection(ws)
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
            await conn.close()

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
            session = self.sessions.create()
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
            future = conn.approvals.get(req.params.approval_id)
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
            await self._respond(
                ws,
                req,
                METHOD_RESULTS[method](long_term=memory.read_long_term(), days=memory.list_days()),
            )
        elif method == "memory.get_daily":
            try:
                content = self.agent.memory.read_daily_on(req.params.date)
            except ValueError as exc:
                await self._respond(ws, req, error=str(exc))
                return
            await self._respond(
                ws, req, METHOD_RESULTS[method](date=req.params.date, content=content)
            )
        elif method == "conversations.list":
            await self._respond(
                ws,
                req,
                METHOD_RESULTS[method](conversations=self.agent.memory.list_conversations()),
            )
        elif method == "conversations.get":
            try:
                turns = self.agent.memory.get_conversation(req.params.date, req.params.session_id)
            except ValueError as exc:
                await self._respond(ws, req, error=str(exc))
                return
            await self._respond(
                ws,
                req,
                METHOD_RESULTS[method](
                    date=req.params.date, session_id=req.params.session_id, turns=turns
                ),
            )
        elif method == "settings.get":
            await self._respond(ws, req, self._settings())

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
        session = self.sessions.get_or_create(req.session_id)
        if session.id in self._busy_sessions:
            await self._respond(
                ws, req, error="A turn is already running for this session.", session_id=session.id
            )
            return
        self._busy_sessions.add(session.id)

        state = _TurnState()
        started_at = _now_iso()
        title = " ".join(text.split())[:80]

        async def task_status(status: str, detail: str | None = None) -> None:
            await self._send(
                ws,
                TaskStatusEvent(
                    request_id=req.id,
                    session_id=session.id,
                    data=TaskStatusData(
                        task_id=req.id,
                        title=title,
                        status=status,  # type: ignore[arg-type]
                        started_at=started_at,
                        finished_at=None if status == "running" else _now_iso(),
                        detail=detail,
                    ),
                ),
            )

        async def on_text_delta(delta: str) -> None:
            await self._send(
                ws,
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
            approval_id = new_id()
            call_id = state.current_call_id
            future: asyncio.Future[ApprovalDecision] = asyncio.get_running_loop().create_future()
            conn.approvals[approval_id] = future
            await self._send(
                ws,
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
                if call_id is not None:
                    state.approval_wait[call_id] = state.approval_wait.get(call_id, 0.0) + (
                        time.monotonic() - waited_from
                    )
            await self._send(
                ws,
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

            await task_status("succeeded")
            await self._respond(
                ws, req, METHOD_RESULTS["chat.send"](text=reply), session_id=session.id
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
    allowed_origins: Iterable[str] | None = None,
    approval_timeout: float = DEFAULT_APPROVAL_TIMEOUT_SECONDS,
) -> CleaDaemon:
    """Assemble a CleaDaemon wired with default memory, tools, and Gemini provider."""
    resolved_memory = memory or MemoryStore()
    resolved_registry = registry or ToolRegistry()
    if not resolved_registry.list_definitions():
        register_builtin_tools(resolved_registry)
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
        allowed_origins=allowed_origins,
        approval_timeout=approval_timeout,
    )


def run_daemon(*, host: str = DEFAULT_HOST, port: int = DEFAULT_PORT) -> None:
    """Blocking entrypoint used by ``clea daemon``."""
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    )
    daemon = build_daemon(host=host, port=port)
    with contextlib.suppress(KeyboardInterrupt):
        asyncio.run(daemon.serve_forever())


def get_daemon_info() -> dict[str, Any]:
    """Return non-sensitive runtime paths for display at daemon startup."""
    mem = MemoryStore()
    return {
        "data_dir": str(mem.base_dir),
        "daily_log": str(mem.daily_log_path()),
        "token_file": str(auth.token_path(mem.base_dir)),
        "allowed_origins": sorted(auth.allowed_origins_from_env()),
    }

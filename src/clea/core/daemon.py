"""Localhost-only WebSocket daemon owning the agent loop, tools, and memory."""

from __future__ import annotations

import asyncio
import contextlib
import logging
from typing import Any

from pydantic import ValidationError
from websockets.asyncio.server import Server, ServerConnection, serve
from websockets.exceptions import ConnectionClosed

from clea.core.agent import AgentLoop
from clea.core.protocol import EventFrame, RequestFrame, ResponseFrame, parse_frame
from clea.core.sessions import SessionManager
from clea.memory.store import MemoryStore
from clea.providers.base import LLMProvider, ToolCall
from clea.providers.gemini import GeminiProvider
from clea.tools import ToolRegistry, register_builtin_tools

logger = logging.getLogger(__name__)

DEFAULT_HOST = "127.0.0.1"
DEFAULT_PORT = 8765
_LOCALHOST_ONLY = {"127.0.0.1", "localhost", "::1"}


def _validate_localhost(host: str) -> str:
    if host not in _LOCALHOST_ONLY:
        raise ValueError(
            f"Refusing to bind Clea daemon to non-localhost address '{host}'. Use 127.0.0.1 only."
        )
    return host


class CleaDaemon:
    """WebSocket server exposing the typed protocol on 127.0.0.1."""

    def __init__(
        self,
        *,
        agent: AgentLoop,
        sessions: SessionManager | None = None,
        host: str = DEFAULT_HOST,
        port: int = DEFAULT_PORT,
    ) -> None:
        self.host = _validate_localhost(host)
        self.port = port
        self.agent = agent
        self.sessions = sessions or SessionManager()
        self._server: Server | None = None

    @property
    def bound_port(self) -> int:
        """Return the actual TCP port the daemon is listening on."""
        if self._server and self._server.sockets:
            return int(self._server.sockets[0].getsockname()[1])
        return self.port

    async def start(self) -> None:
        """Start listening for localhost WebSocket connections."""
        self.agent.memory.ensure_initialized()
        self._server = await serve(self._handle_connection, self.host, self.port)
        logger.info("Clea daemon listening on ws://%s:%d", self.host, self.bound_port)

    async def stop(self) -> None:
        """Gracefully close the WebSocket server."""
        if self._server is not None:
            self._server.close()
            await self._server.wait_closed()
            self._server = None

    async def serve_forever(self) -> None:
        """Start the daemon and run until cancelled."""
        await self.start()
        assert self._server is not None
        try:
            await self._server.serve_forever()
        finally:
            await self.stop()

    async def _send_frame(
        self,
        ws: ServerConnection,
        frame: ResponseFrame | EventFrame,
    ) -> None:
        await ws.send(frame.model_dump_json())

    async def _handle_connection(self, ws: ServerConnection) -> None:
        async for raw_message in ws:
            if isinstance(raw_message, bytes):
                raw_text = raw_message.decode("utf-8", errors="replace")
            else:
                raw_text = raw_message

            try:
                frame = parse_frame(raw_text)
            except ValidationError as exc:
                await self._send_frame(
                    ws,
                    EventFrame(event="error", data={"message": f"Invalid protocol frame: {exc}"}),
                )
                continue

            if not isinstance(frame, RequestFrame):
                await self._send_frame(
                    ws,
                    EventFrame(
                        event="error",
                        data={"message": "Client may only send 'request' frames."},
                    ),
                )
                continue

            await self._dispatch_request(ws, frame)

    async def _dispatch_request(self, ws: ServerConnection, req: RequestFrame) -> None:
        if req.method == "health.ping":
            await self._send_frame(ws, ResponseFrame(id=req.id, result={"status": "ok"}))
            return

        if req.method == "session.new":
            session = self.sessions.create()
            await self._send_frame(
                ws,
                ResponseFrame(id=req.id, session_id=session.id, result={"session_id": session.id}),
            )
            return

        if req.method == "chat.send":
            text = str(req.params.get("text", "")).strip()
            if not text:
                await self._send_frame(
                    ws,
                    ResponseFrame(
                        id=req.id,
                        ok=False,
                        session_id=req.session_id,
                        error="Missing non-empty 'text' parameter.",
                    ),
                )
                return

            session = self.sessions.get_or_create(req.session_id)

            async def on_text_delta(delta: str) -> None:
                await self._send_frame(
                    ws,
                    EventFrame(
                        event="text.delta",
                        request_id=req.id,
                        session_id=session.id,
                        data={"text": delta},
                    ),
                )

            async def on_tool_start(call: ToolCall) -> None:
                await self._send_frame(
                    ws,
                    EventFrame(
                        event="tool.start",
                        request_id=req.id,
                        session_id=session.id,
                        data={"name": call.name, "call_id": call.id, "arguments": call.arguments},
                    ),
                )

            async def on_tool_end(call: ToolCall, output: str, is_error: bool) -> None:
                await self._send_frame(
                    ws,
                    EventFrame(
                        event="tool.end",
                        request_id=req.id,
                        session_id=session.id,
                        data={
                            "name": call.name,
                            "call_id": call.id,
                            "is_error": is_error,
                            "output": output,
                        },
                    ),
                )

            try:
                reply = await self.agent.run_turn(
                    session=session,
                    user_text=text,
                    on_text_delta=on_text_delta,
                    on_tool_start=on_tool_start,
                    on_tool_end=on_tool_end,
                )
            except ConnectionClosed:
                raise
            except Exception as exc:
                logger.exception("Error while running agent turn")
                await self._send_frame(
                    ws,
                    ResponseFrame(
                        id=req.id,
                        ok=False,
                        session_id=session.id,
                        error=str(exc),
                    ),
                )
                return

            await self._send_frame(
                ws,
                EventFrame(
                    event="turn.done",
                    request_id=req.id,
                    session_id=session.id,
                    data={"text": reply},
                ),
            )
            await self._send_frame(
                ws,
                ResponseFrame(
                    id=req.id,
                    ok=True,
                    session_id=session.id,
                    result={"text": reply},
                ),
            )


def build_daemon(
    *,
    host: str = DEFAULT_HOST,
    port: int = DEFAULT_PORT,
    provider: LLMProvider | None = None,
    memory: MemoryStore | None = None,
    registry: ToolRegistry | None = None,
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
    return CleaDaemon(agent=agent, host=host, port=port)


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
    }

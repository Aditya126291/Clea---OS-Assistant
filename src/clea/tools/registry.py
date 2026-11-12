"""MCP-compatible tool registry with risk-level permissions and audit logging."""

from __future__ import annotations

import inspect
import json
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from enum import StrEnum
from pathlib import Path
from typing import Any

from pydantic import BaseModel, Field

from clea.memory.store import default_data_dir


class RiskLevel(StrEnum):
    """Risk classification for registered tools."""

    READ_ONLY = "read_only"
    REVERSIBLE = "reversible"
    DESTRUCTIVE = "destructive"


def requires_approval(level: RiskLevel) -> bool:
    """Policy: read_only runs automatically; reversible and destructive need approval."""
    return level is not RiskLevel.READ_ONLY


class ToolDefinition(BaseModel):
    """MCP-compatible metadata and JSON Schema for a tool."""

    name: str
    description: str
    input_schema: dict[str, Any] = Field(
        default_factory=lambda: {"type": "object", "properties": {}, "additionalProperties": False}
    )
    risk_level: RiskLevel = RiskLevel.READ_ONLY

    def to_mcp_tool(self) -> dict[str, Any]:
        """Return an MCP-compatible tool descriptor."""
        return {
            "name": self.name,
            "description": self.description,
            "inputSchema": self.input_schema,
        }


class ToolNotFoundError(KeyError):
    """Raised when attempting to execute an unregistered tool."""


class PermissionDeniedError(PermissionError):
    """Raised when a destructive tool is invoked without confirmation approval."""


ToolHandler = Callable[..., Awaitable[Any] | Any]
ConfirmationCallback = Callable[[ToolDefinition, dict[str, Any]], Awaitable[bool] | bool]


@dataclass(slots=True)
class RegisteredTool:
    """Internal pairing of a tool definition and its callable handler."""

    definition: ToolDefinition
    handler: ToolHandler


class AuditLogger:
    """Appends structured JSONL audit records for every tool invocation."""

    def __init__(self, log_path: Path | None = None) -> None:
        self.log_path = (log_path or (default_data_dir() / "logs" / "audit.jsonl")).resolve()

    def record(
        self,
        *,
        tool_name: str,
        risk_level: RiskLevel,
        arguments: dict[str, Any],
        allowed: bool,
        outcome: str,
        error: str | None = None,
    ) -> None:
        """Write one audit log line to disk."""
        self.log_path.parent.mkdir(parents=True, exist_ok=True)
        entry = {
            "timestamp": datetime.now(UTC).isoformat(),
            "tool": tool_name,
            "risk_level": risk_level.value,
            "arguments": arguments,
            "allowed": allowed,
            "outcome": outcome,
            "error": error,
        }
        with self.log_path.open("a", encoding="utf-8") as handle:
            handle.write(json.dumps(entry, default=str) + "\n")


async def _maybe_await(value: Any) -> Any:
    if inspect.isawaitable(value):
        return await value
    return value


class ToolRegistry:
    """Central registry that validates, authorizes, audits, and executes tools."""

    def __init__(self, audit_logger: AuditLogger | None = None) -> None:
        self._tools: dict[str, RegisteredTool] = {}
        self._audit = audit_logger or AuditLogger()

    def register(
        self,
        *,
        name: str,
        description: str,
        input_schema: dict[str, Any] | None = None,
        risk_level: RiskLevel = RiskLevel.READ_ONLY,
        handler: ToolHandler,
    ) -> ToolDefinition:
        """Register a tool by name with its JSON schema, risk level, and handler."""
        if name in self._tools:
            raise ValueError(f"Tool '{name}' is already registered.")
        definition = ToolDefinition(
            name=name,
            description=description,
            input_schema=input_schema
            or {"type": "object", "properties": {}, "additionalProperties": False},
            risk_level=risk_level,
        )
        self._tools[name] = RegisteredTool(definition=definition, handler=handler)
        return definition

    def get(self, name: str) -> ToolDefinition:
        """Return the ToolDefinition for a registered tool."""
        if name not in self._tools:
            raise ToolNotFoundError(f"Unknown tool: {name}")
        return self._tools[name].definition

    def list_definitions(self) -> list[ToolDefinition]:
        """Return all registered tool definitions."""
        return [item.definition for item in self._tools.values()]

    def list_mcp_schemas(self) -> list[dict[str, Any]]:
        """Return MCP-compatible tool descriptors for all registered tools."""
        return [item.definition.to_mcp_tool() for item in self._tools.values()]

    async def execute(
        self,
        name: str,
        arguments: dict[str, Any] | None = None,
        *,
        confirm: ConfirmationCallback | None = None,
    ) -> Any:
        """Execute a registered tool after enforcing risk-level permission checks."""
        if name not in self._tools:
            raise ToolNotFoundError(f"Unknown tool: {name}")

        tool = self._tools[name]
        args = arguments or {}

        if requires_approval(tool.definition.risk_level):
            approved = False
            if confirm is not None:
                approved = bool(await _maybe_await(confirm(tool.definition, args)))
            if not approved:
                msg = f"Permission denied for {tool.definition.risk_level.value} tool '{name}'."
                self._audit.record(
                    tool_name=name,
                    risk_level=tool.definition.risk_level,
                    arguments=args,
                    allowed=False,
                    outcome="denied",
                    error=msg,
                )
                raise PermissionDeniedError(msg)

        try:
            result = await _maybe_await(tool.handler(**args))
        except Exception as exc:
            self._audit.record(
                tool_name=name,
                risk_level=tool.definition.risk_level,
                arguments=args,
                allowed=True,
                outcome="error",
                error=str(exc),
            )
            raise

        self._audit.record(
            tool_name=name,
            risk_level=tool.definition.risk_level,
            arguments=args,
            allowed=True,
            outcome="ok",
        )
        return result

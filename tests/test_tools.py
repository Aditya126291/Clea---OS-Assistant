"""Tests for the MCP-compatible tool registry, permissions, and audit log."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from clea.tools import (
    AuditLogger,
    PermissionDeniedError,
    RiskLevel,
    ToolNotFoundError,
    ToolRegistry,
    register_builtin_tools,
)


def _read_audit_entries(audit_path: Path) -> list[dict]:
    lines = audit_path.read_text(encoding="utf-8").strip().splitlines()
    return [json.loads(line) for line in lines]


async def test_builtin_read_only_tool_and_mcp_schema(tmp_path: Path) -> None:
    audit_path = tmp_path / "logs" / "audit.jsonl"
    registry = ToolRegistry(audit_logger=AuditLogger(audit_path))
    register_builtin_tools(registry)

    mcp_tools = registry.list_mcp_schemas()
    assert len(mcp_tools) == 1
    assert mcp_tools[0]["name"] == "get_system_info"
    assert "inputSchema" in mcp_tools[0]

    result = await registry.execute("get_system_info", {"include_platform_details": True})
    assert "local_time" in result
    assert "os" in result

    entries = _read_audit_entries(audit_path)
    assert len(entries) == 1
    assert entries[0]["tool"] == "get_system_info"
    assert entries[0]["allowed"] is True
    assert entries[0]["outcome"] == "ok"


async def test_destructive_tool_refused_without_confirmation(tmp_path: Path) -> None:
    audit_path = tmp_path / "logs" / "audit.jsonl"
    registry = ToolRegistry(audit_logger=AuditLogger(audit_path))

    called = False

    def wipe_file(path: str) -> str:
        nonlocal called
        called = True
        return f"deleted {path}"

    registry.register(
        name="wipe_file",
        description="Delete a file permanently.",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        risk_level=RiskLevel.DESTRUCTIVE,
        handler=wipe_file,
    )

    with pytest.raises(PermissionDeniedError):
        await registry.execute("wipe_file", {"path": "important.txt"})
    assert called is False

    async def deny(_defn, _args) -> bool:
        return False

    with pytest.raises(PermissionDeniedError):
        await registry.execute("wipe_file", {"path": "important.txt"}, confirm=deny)
    assert called is False

    entries = _read_audit_entries(audit_path)
    assert len(entries) == 2
    assert all(entry["allowed"] is False and entry["outcome"] == "denied" for entry in entries)


async def test_destructive_tool_runs_when_confirmed(tmp_path: Path) -> None:
    audit_path = tmp_path / "logs" / "audit.jsonl"
    registry = ToolRegistry(audit_logger=AuditLogger(audit_path))

    registry.register(
        name="wipe_file",
        description="Delete a file permanently.",
        input_schema={"type": "object", "properties": {"path": {"type": "string"}}},
        risk_level=RiskLevel.DESTRUCTIVE,
        handler=lambda path: f"deleted {path}",
    )

    result = await registry.execute(
        "wipe_file",
        {"path": "scratch.txt"},
        confirm=lambda _defn, _args: True,
    )
    assert result == "deleted scratch.txt"

    entries = _read_audit_entries(audit_path)
    assert entries[-1]["allowed"] is True
    assert entries[-1]["outcome"] == "ok"


async def test_unknown_tool_raises(tmp_path: Path) -> None:
    registry = ToolRegistry(audit_logger=AuditLogger(tmp_path / "audit.jsonl"))
    with pytest.raises(ToolNotFoundError):
        await registry.execute("does_not_exist")

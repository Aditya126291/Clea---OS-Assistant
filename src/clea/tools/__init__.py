"""Tool registry and built-in tools."""

from clea.tools.memory_tools import register_memory_tools
from clea.tools.registry import (
    AuditLogger,
    ConfirmationCallback,
    PermissionDeniedError,
    RiskLevel,
    ToolDefinition,
    ToolNotFoundError,
    ToolRegistry,
    requires_approval,
)
from clea.tools.system_controls import (
    manage_window,
    register_system_control_tools,
    system_control,
    system_diagnostics,
)
from clea.tools.system_info import register_builtin_tools

__all__ = [
    "AuditLogger",
    "ConfirmationCallback",
    "PermissionDeniedError",
    "RiskLevel",
    "ToolDefinition",
    "ToolNotFoundError",
    "ToolRegistry",
    "manage_window",
    "register_builtin_tools",
    "register_memory_tools",
    "register_system_control_tools",
    "requires_approval",
    "system_control",
    "system_diagnostics",
]

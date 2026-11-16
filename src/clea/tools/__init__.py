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
from clea.tools.system_info import register_builtin_tools
from clea.tools.timers import (
    TimerListResult,
    TimerManager,
    cancel_timer_or_reminder,
    get_default_timer_manager,
    list_timers_and_reminders,
    parse_duration,
    parse_when,
    register_timer_tools,
    send_windows_toast_and_chime,
    set_default_timer_manager,
    set_reminder,
    set_timer,
)

__all__ = [
    "AuditLogger",
    "ConfirmationCallback",
    "PermissionDeniedError",
    "RiskLevel",
    "TimerListResult",
    "TimerManager",
    "ToolDefinition",
    "ToolNotFoundError",
    "ToolRegistry",
    "cancel_timer_or_reminder",
    "get_default_timer_manager",
    "list_timers_and_reminders",
    "parse_duration",
    "parse_when",
    "register_builtin_tools",
    "register_memory_tools",
    "register_timer_tools",
    "requires_approval",
    "send_windows_toast_and_chime",
    "set_default_timer_manager",
    "set_reminder",
    "set_timer",
]

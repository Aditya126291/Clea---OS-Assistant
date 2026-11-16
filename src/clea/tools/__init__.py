"""Tool registry and built-in tools."""

from clea.tools.file_intel import (
    manage_file,
    read_document,
    register_file_tools,
    search_files,
)
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
    "manage_file",
    "manage_window",
    "parse_duration",
    "parse_when",
    "read_document",
    "register_builtin_tools",
    "register_file_tools",
    "register_memory_tools",
    "register_system_control_tools",
    "register_timer_tools",
    "requires_approval",
    "search_files",
    "send_windows_toast_and_chime",
    "set_default_timer_manager",
    "set_reminder",
    "set_timer",
    "system_control",
    "system_diagnostics",
]

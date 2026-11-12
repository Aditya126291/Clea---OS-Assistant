"""Example read-only tool for Milestone 1."""

from __future__ import annotations

import platform
import socket
from datetime import datetime
from typing import Any

from clea.tools.registry import RiskLevel, ToolRegistry


def get_system_info(include_platform_details: bool = True) -> dict[str, Any]:
    """Return local time and basic host environment details."""
    now = datetime.now().astimezone()
    info: dict[str, Any] = {
        "local_time": now.isoformat(timespec="seconds"),
        "timezone": str(now.tzinfo),
    }
    if include_platform_details:
        info.update(
            {
                "os": platform.system(),
                "os_release": platform.release(),
                "machine": platform.machine(),
                "hostname": socket.gethostname(),
                "python_version": platform.python_version(),
            }
        )
    return info


def register_builtin_tools(registry: ToolRegistry) -> None:
    """Register the Milestone 1 built-in tools on the given registry."""
    registry.register(
        name="get_system_info",
        description=(
            "Get the current local date/time and basic host system information "
            "(OS, hostname, architecture, Python version)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "include_platform_details": {
                    "type": "boolean",
                    "description": "Include OS, hostname, and Python version details.",
                    "default": True,
                }
            },
            "additionalProperties": False,
        },
        risk_level=RiskLevel.READ_ONLY,
        handler=get_system_info,
    )

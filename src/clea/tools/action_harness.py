"""Desktop state-diff capture + circuit-breaker re-exports for OS/Agent tools."""

from __future__ import annotations

import contextlib
import logging
from typing import TYPE_CHECKING, Any

from clea.core.circuit_breaker import (
    SUGGESTED_RECOVERY_ACTIONS,
    ActionCircuitBreaker,
    StateCaptureFn,
    compute_state_diff,
    is_blind_click_action,
    is_blind_gui_action,
    is_deterministic_action,
    is_state_diff_tool,
    merge_tool_state_diff,
)
from clea.tools.cdp_bridge import (
    CDPBridge,
    get_default_cdp_bridge,
    is_chromium_process,
)

if TYPE_CHECKING:
    from clea.tools.guards import FocusPreconditionBackend, MediaVerificationBackend

logger = logging.getLogger(__name__)


def capture_desktop_state(
    *,
    focus_backend: FocusPreconditionBackend | None = None,
    cdp_bridge: CDPBridge | None = None,
    media_backend: MediaVerificationBackend | None = None,
    include_media: bool = True,
    allow_live_cdp: bool = True,
) -> dict[str, Any]:
    """Capture lightweight foreground window, Chromium CDP page, and GSMTC media state."""
    state: dict[str, Any] = {
        "hwnd": None,
        "title": "",
        "process_name": "",
        "url": "",
        "page_title": "",
        "target_id": None,
        "is_pwa": None,
        "media": None,
    }

    if focus_backend is not None:
        with contextlib.suppress(Exception):
            ctx = focus_backend.get_foreground_context()
            if ctx is not None:
                state["hwnd"] = int(ctx.hwnd) if ctx.hwnd else None
                state["title"] = str(ctx.title or "")
                state["process_name"] = str(ctx.process_name or "")

    proc_name = str(state.get("process_name") or "")
    should_probe_cdp = cdp_bridge is not None or (
        allow_live_cdp and is_chromium_process(proc_name)
    )
    if should_probe_cdp and (not proc_name or is_chromium_process(proc_name)):
        bridge = cdp_bridge or get_default_cdp_bridge()
        with contextlib.suppress(Exception):
            cdp_state = bridge.get_active_page_state_sync(
                title_hint=state["title"] or None,
                preferred_browser=proc_name if is_chromium_process(proc_name) else None,
            )
            if isinstance(cdp_state, dict) and cdp_state.get("ok"):
                state["url"] = str(cdp_state.get("url") or "")
                state["page_title"] = str(cdp_state.get("title") or "")
                state["target_id"] = cdp_state.get("target_id")
                state["is_pwa"] = cdp_state.get("is_pwa")

    if include_media and media_backend is not None:
        with contextlib.suppress(Exception):
            media = media_backend.get_active_session(timeout_s=0.25)
            if media is not None and getattr(media, "available", False):
                state["media"] = {
                    "title": str(getattr(media, "title", "") or ""),
                    "artist": str(getattr(media, "artist", "") or ""),
                    "playback_status": str(getattr(media, "playback_status", "") or ""),
                }

    return state


__all__ = [
    "ActionCircuitBreaker",
    "SUGGESTED_RECOVERY_ACTIONS",
    "StateCaptureFn",
    "capture_desktop_state",
    "compute_state_diff",
    "is_blind_click_action",
    "is_blind_gui_action",
    "is_deterministic_action",
    "is_state_diff_tool",
    "merge_tool_state_diff",
]

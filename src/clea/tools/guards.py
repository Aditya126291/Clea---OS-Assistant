"""Deterministic runtime precondition guards and closed-loop action verifiers.

Provides zero-token Win32 and WinRT probes cleanly separated behind Protocols so
window, UI automation, input, and media actions can verify preconditions before
every step and confirm actual OS state changes afterwards without live desktop
side effects in unit tests.
"""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import importlib
import logging
import sys
import threading
import time
from collections.abc import Callable
from ctypes import wintypes
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Protocol, runtime_checkable

from clea.core.protocol import VerificationScope
from clea.surfaces.voice.win32 import (
    _MONITORINFOEXW,
    _RECT,
    DWMWA_EXTENDED_FRAME_BOUNDS,
    MONITOR_DEFAULTTONEAREST,
    PROCESS_QUERY_LIMITED_INFORMATION,
)

logger = logging.getLogger(__name__)

_DEFAULT_BOUNDS_TOLERANCE_PX = 24
_GW_OWNER = 4
_GWL_STYLE = -16
_WS_POPUP = 0x80000000


def _enable_dpi_awareness_guards(user32: Any) -> None:
    """Ensure Win32 coordinates match DWM physical pixels across DPI scales."""
    if sys.platform != "win32" or not isinstance(user32, ctypes.CDLL):
        return
    with contextlib.suppress(Exception):
        if hasattr(user32, "SetProcessDpiAwarenessContext"):
            user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
            # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
            if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
                return
    with contextlib.suppress(Exception):
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        if hasattr(shcore, "SetProcessDpiAwareness"):
            shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
            shcore.SetProcessDpiAwareness.restype = ctypes.c_long
            shcore.SetProcessDpiAwareness(2)


# ---------------------------------------------------------------------------
# Typed Context, Error & Outcome Models
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class WindowContext:
    """Snapshot of a Win32 top-level window used for focus/geometry checks."""

    hwnd: int
    title: str = ""
    process_name: str = ""
    pid: int = 0
    bounds: tuple[int, int, int, int] | None = None
    work_area: tuple[int, int, int, int] | None = None
    minimized: bool = False

    def to_dict(self) -> dict[str, Any]:
        return {
            "hwnd": self.hwnd,
            "title": self.title,
            "process_name": self.process_name,
            "pid": self.pid,
            "bounds": list(self.bounds) if self.bounds is not None else None,
            "work_area": list(self.work_area) if self.work_area is not None else None,
            "minimized": self.minimized,
        }


@dataclass(frozen=True, init=False)
class MediaSessionState:
    """Snapshot of Windows GlobalSystemMediaTransportControls active session."""

    playback_status: str = "unavailable"
    title: str = ""
    artist: str = ""
    source_app: str = ""
    available: bool = True
    error: str | None = None

    def __init__(
        self,
        playback_status: str | None = None,
        *,
        status: str | None = None,
        title: str = "",
        artist: str = "",
        source_app: str = "",
        available: bool = True,
        error: str | None = None,
    ) -> None:
        resolved = playback_status if playback_status is not None else (status or "unavailable")
        object.__setattr__(self, "playback_status", str(resolved))
        object.__setattr__(self, "title", str(title or ""))
        object.__setattr__(self, "artist", str(artist or ""))
        object.__setattr__(self, "source_app", str(source_app or ""))
        object.__setattr__(self, "available", bool(available))
        object.__setattr__(self, "error", error)

    @property
    def status(self) -> str:
        """Alias for ``playback_status`` matching verification status naming."""
        return self.playback_status

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": self.playback_status,
            "playback_status": self.playback_status,
            "title": self.title,
            "artist": self.artist,
            "source_app": self.source_app,
            "available": self.available,
            "error": self.error,
        }


class PreconditionFailedError(RuntimeError):
    """Raised before executing an action step when focus/window integrity fails."""

    def __init__(
        self,
        reason: str,
        *,
        expected_hwnd: int | None = None,
        expected_title: str = "",
        expected_process: str = "",
        actual_hwnd: int | None = None,
        actual_title: str = "",
        actual_process: str = "",
        step_label: str = "",
        message: str | None = None,
    ) -> None:
        self.reason = reason
        self.expected_hwnd = expected_hwnd
        self.expected_title = expected_title
        self.expected_process = expected_process
        self.actual_hwnd = actual_hwnd
        self.actual_title = actual_title
        self.actual_process = actual_process
        self.step_label = step_label

        if message is None:
            step_desc = f" before step '{step_label}'" if step_label else ""
            if reason == "no_target_window":
                message = f"No target window available{step_desc}."
            elif reason == "window_closed":
                message = (
                    f"Target window closed{step_desc}: {expected_title!r} "
                    f"(hwnd={expected_hwnd})."
                )
            elif reason == "window_minimized":
                message = (
                    f"Target window minimized{step_desc}: {expected_title!r} "
                    f"(hwnd={expected_hwnd})."
                )
            elif reason == "focus_lost":
                message = (
                    f"Focus lost{step_desc}: expected {expected_title!r} "
                    f"({expected_process or 'unknown'}), got {actual_title!r} "
                    f"({actual_process or 'unknown'})."
                )
            elif reason == "target_point_outside_window":
                message = (
                    f"Target point outside window bounds{step_desc}: "
                    f"{expected_title!r} (hwnd={expected_hwnd})."
                )
            elif reason == "target_point_occluded":
                message = (
                    f"Target point occluded{step_desc}: expected {expected_title!r} "
                    f"(hwnd={expected_hwnd}), hit {actual_title!r} "
                    f"({actual_process or 'unknown'}, hwnd={actual_hwnd})."
                )
            else:
                message = f"Precondition failed ({reason}){step_desc}."
        super().__init__(message)

    def to_dict(self) -> dict[str, Any]:
        return {
            "status": "aborted_precondition",
            "reason": self.reason,
            "step_label": self.step_label,
            "expected_hwnd": self.expected_hwnd,
            "expected_title": self.expected_title,
            "expected_process": self.expected_process,
            "actual_hwnd": self.actual_hwnd,
            "actual_title": self.actual_title,
            "actual_process": self.actual_process,
            "message": str(self),
        }

    def to_abort_payload(self) -> dict[str, Any]:
        """Build a structured tool response for an aborted precondition."""
        details = self.to_dict()
        return {
            "ok": False,
            "status": "aborted_precondition",
            "reason": self.reason,
            "error": str(self),
            "aborted_precondition": details,
        }


UNAVAILABLE_VERIFICATION_REASONS = frozenset(
    {
        "winrt_unavailable",
        "no_media_session",
        "no_active_session",
        "media_verification_unavailable",
        "winrt_timeout",
    }
)

_FAILED_ACTION_STATUSES = frozenset(
    {
        "error",
        "failed",
        "aborted",
        "aborted_precondition",
        "cancelled",
        "blocked_by_circuit_breaker",
        "verification_failed",
        "stale_ref",
        "target_not_found",
        "non_interactive_desktop",
        "timeout",
    }
)

_WINDOW_LAYOUT_SYSTEM_ACTIONS = frozenset(
    {
        "snap_left",
        "snap_right",
        "snap_left_window",
        "snap_right_window",
        "maximize",
        "maximize_window",
        "minimize",
        "minimize_window",
        "restore",
        "restore_window",
        "close",
        "close_window",
    }
)


def classify_action_verification(
    tool_name: str,
    *,
    action: str | None = None,
    result: dict[str, Any] | None = None,
    state_diff: dict[str, Any] | None = None,
    verification: dict[str, Any] | None = None,
    expected_scope: VerificationScope | None = None,
) -> tuple[VerificationScope, bool]:
    """Determine ``(verification_scope, goal_verified)`` for an OS/browser action.

    Separates "command dispatched" from "user-visible goal verified" so models
    never conflate media transport changes or no-op dispatches with confirmed
    in-app navigation or goal completion.
    """
    res = result if isinstance(result, dict) else {}
    diff = (
        state_diff
        if isinstance(state_diff, dict)
        else (res.get("state_diff") if isinstance(res.get("state_diff"), dict) else {})
    )
    ver = (
        verification
        if isinstance(verification, dict)
        else (res.get("verification") if isinstance(res.get("verification"), dict) else None)
    )

    norm_tool = (tool_name or "").strip().lower().removeprefix("os.")
    norm_action = (action if action is not None else str(res.get("action") or "")).strip().lower()
    status = str(res.get("status") or "").strip().lower()
    ok = res.get("ok", res.get("success", True)) is not False

    def _apply_expected_scope(
        achieved: VerificationScope, verified: bool
    ) -> tuple[VerificationScope, bool]:
        if expected_scope is None or not verified:
            return (achieved, verified)
        if achieved == expected_scope:
            return (achieved, True)
        # browser_dom satisfies in_app_view goals for browser-hosted apps
        if expected_scope == "in_app_view" and achieved == "browser_dom":
            return (achieved, True)
        return (achieved, False)

    if not ok or status in _FAILED_ACTION_STATUSES:
        if norm_tool == "control_media_volume":
            return ("media_session", False)
        if norm_tool == "open_or_focus_app":
            return ("window_focus", False)
        if norm_tool == "control_system" and norm_action in _WINDOW_LAYOUT_SYSTEM_ACTIONS:
            return ("window_layout", False)
        return ("dispatch_only", False)

    view_changed = bool(diff.get("view_changed") or res.get("view_changed"))
    window_changed = bool(diff.get("window_changed"))
    url_changed = bool(diff.get("url_changed"))
    title_changed = bool(diff.get("title_changed"))
    media_changed = bool(diff.get("media_changed"))
    cdp_used = bool(res.get("cdp_used") or res.get("source") == "cdp")
    ver_verified: bool | None = bool(ver.get("verified")) if isinstance(ver, dict) else None

    if norm_tool == "control_media_volume":
        if ver_verified is not None:
            return _apply_expected_scope("media_session", ver_verified)
        if norm_action == "set_volume":
            return _apply_expected_scope("media_session", True)
        return _apply_expected_scope("media_session", media_changed)

    if norm_tool == "open_or_focus_app":
        if ver_verified is not None:
            return _apply_expected_scope("window_focus", ver_verified)
        if status == "focused" and bool(res.get("hwnd")):
            return _apply_expected_scope("window_focus", True)
        if window_changed or view_changed:
            return _apply_expected_scope("window_focus", True)
        return _apply_expected_scope("dispatch_only", False)

    if norm_tool == "control_system":
        if norm_action in {"open_settings", "settings"}:
            if view_changed:
                return _apply_expected_scope("in_app_view", True)
            if window_changed:
                return _apply_expected_scope("window_focus", True)
            return _apply_expected_scope("dispatch_only", False)
        if norm_action in _WINDOW_LAYOUT_SYSTEM_ACTIONS:
            if ver_verified is not None:
                return _apply_expected_scope("window_layout", ver_verified)
            return _apply_expected_scope("window_layout", window_changed)
        if norm_action in {"inspect_window_layout", "inspect_layout", "layout"}:
            return _apply_expected_scope("window_layout", True)
        if norm_action in {
            "get_brightness",
            "set_brightness",
            "step_brightness",
            "brightness_up",
            "brightness_down",
        }:
            return _apply_expected_scope("dispatch_only", True)
        return _apply_expected_scope("dispatch_only", bool(window_changed or view_changed))

    if norm_tool == "open_url":
        if cdp_used:
            verified = bool(url_changed or title_changed or status == "navigated")
            return _apply_expected_scope("browser_dom", verified)
        is_custom_proto = str(res.get("resolution_layer") or "").strip().lower() in {
            "url_protocol",
            "custom_protocol",
        } or (
            bool(res.get("protocol_scheme"))
            and str(res.get("protocol_scheme") or "").strip().lower() not in {"http", "https"}
        )
        if is_custom_proto:
            if view_changed:
                return _apply_expected_scope("in_app_view", True)
            if window_changed:
                return _apply_expected_scope("window_focus", True)
            return _apply_expected_scope("dispatch_only", False)
        if url_changed:
            return _apply_expected_scope("browser_dom", True)
        if view_changed:
            return _apply_expected_scope("in_app_view", True)
        if window_changed or (
            bool(res.get("routed_to_installed_app"))
            and status == "focused"
            and bool(res.get("window_hwnd") or res.get("hwnd"))
        ):
            return _apply_expected_scope("window_focus", True)
        return _apply_expected_scope("dispatch_only", False)

    if norm_tool == "gui_action":
        if norm_action == "wait":
            return _apply_expected_scope("dispatch_only", True)
        raw_ref = str(res.get("ref") or "").strip().lstrip("@").lower()
        if cdp_used or (raw_ref.startswith("b") and raw_ref[1:].isdigit()):
            verified = bool(diff.get("changed")) if diff else (status in {"navigated", "completed"})
            return _apply_expected_scope("browser_dom", verified)
        if view_changed:
            return _apply_expected_scope("in_app_view", True)
        if url_changed:
            return _apply_expected_scope("browser_dom", True)
        if window_changed or title_changed:
            return _apply_expected_scope("window_focus", True)
        if media_changed:
            return _apply_expected_scope("media_session", True)
        return _apply_expected_scope("dispatch_only", False)

    if norm_tool in {"ui_act", "click_ui_element", "ui_batch"}:
        if cdp_used:
            verified = bool(diff.get("changed")) if diff else True
            return _apply_expected_scope("browser_dom", verified)
        if view_changed:
            return _apply_expected_scope("in_app_view", True)
        if url_changed:
            return _apply_expected_scope("browser_dom", True)
        ui_diff = res.get("diff")
        has_ui_diff = isinstance(ui_diff, dict) and bool(
            ui_diff.get("changed_values")
            or ui_diff.get("added_elements")
            or ui_diff.get("removed_count")
            or ui_diff.get("window_changed")
        )
        if has_ui_diff:
            return _apply_expected_scope("ui_element_delta", True)
        if window_changed or title_changed:
            return _apply_expected_scope("window_focus", True)
        target_state = res.get("target_state")
        if isinstance(target_state, dict) and target_state.get("available") is True:
            return _apply_expected_scope("ui_element_delta", True)
        method = str(res.get("method") or "").strip().lower()
        if method in {"invoke", "select", "toggle", "expand_collapse", "value_pattern"}:
            return _apply_expected_scope("ui_element_delta", True)
        return _apply_expected_scope("dispatch_only", False)

    if view_changed:
        return _apply_expected_scope("in_app_view", True)
    if url_changed:
        return _apply_expected_scope("browser_dom", True)
    if window_changed:
        return _apply_expected_scope("window_focus", True)
    if media_changed:
        return _apply_expected_scope("media_session", True)
    return _apply_expected_scope("dispatch_only", False)


def apply_verification_metadata(
    tool_name: str,
    result: dict[str, Any],
    *,
    action: str | None = None,
    state_diff: dict[str, Any] | None = None,
    expected_scope: VerificationScope | None = None,
) -> dict[str, Any]:
    """Attach ``verification_scope`` and ``goal_verified`` to a tool result copy.

    Preserves explicit tool-reported ``verification_scope`` / ``goal_verified``
    unless ``state_diff`` provides stronger observed evidence (e.g. ``view_changed``).
    """
    out = dict(result)
    effective_diff = (
        state_diff
        if isinstance(state_diff, dict)
        else (out.get("state_diff") if isinstance(out.get("state_diff"), dict) else None)
    )
    computed_scope, computed_verified = classify_action_verification(
        tool_name,
        action=action,
        result=out,
        state_diff=effective_diff,
        expected_scope=expected_scope,
    )
    existing_scope = out.get("verification_scope")
    existing_verified = out.get("goal_verified")
    if (
        isinstance(existing_scope, str)
        and existing_scope
        and isinstance(existing_verified, bool)
    ):
        if (
            isinstance(effective_diff, dict)
            and effective_diff.get("view_changed")
            and not existing_verified
        ):
            out["verification_scope"] = "in_app_view"
            out["goal_verified"] = True
        return out

    out["verification_scope"] = computed_scope
    out["goal_verified"] = computed_verified
    return out


@dataclass(frozen=True)
class VerificationOutcome:
    """Result of deterministic closed-loop post-action verification."""

    verified: bool
    reason: str
    status: str = "verified"
    actual_status: str | None = None
    track_title: str = ""
    artist: str = ""
    source_app: str = ""
    actual_bounds: tuple[int, int, int, int] | None = None
    expected_bounds: tuple[int, int, int, int] | None = None
    work_area: tuple[int, int, int, int] | None = None
    elapsed_ms: float = 0.0
    actual: dict[str, Any] = field(default_factory=dict)
    occluded_by_unrelated_window: bool = False
    occluding_windows: tuple[str, ...] = ()

    def __post_init__(self) -> None:
        if not self.verified and self.status == "verified":
            inferred = "unavailable" if self.is_unavailable else "failed"
            object.__setattr__(self, "status", inferred)

    @property
    def is_unavailable(self) -> bool:
        """True when verification could not read OS state rather than observing a mismatch."""
        if self.verified:
            return False
        if self.status == "unavailable":
            return True
        return self.reason in UNAVAILABLE_VERIFICATION_REASONS or self.reason.startswith(
            "winrt_error"
        )

    @property
    def outcome_kind(self) -> str:
        """Distinguish 'verified', 'unavailable', and real 'failed' outcomes."""
        if self.verified:
            return "verified"
        if self.is_unavailable:
            return "unavailable"
        return "failed"

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "verified": self.verified,
            "status": self.status,
            "outcome_kind": self.outcome_kind,
            "reason": self.reason,
            "actual": dict(self.actual),
            "elapsed_ms": round(self.elapsed_ms, 2),
        }
        if self.occluded_by_unrelated_window:
            payload["occluded_by_unrelated_window"] = True
            payload["occluding_windows"] = list(self.occluding_windows)
        return payload


# ---------------------------------------------------------------------------
# Backends
# ---------------------------------------------------------------------------


@runtime_checkable
class FocusPreconditionBackend(Protocol):
    """OS probe boundary for window validity, focus, and visible DWM geometry."""

    def get_foreground_context(self) -> WindowContext | None: ...

    def get_window_context(self, hwnd: int) -> WindowContext | None: ...

    def is_window_valid(self, hwnd: int) -> bool: ...

    def is_minimized(self, hwnd: int) -> bool: ...

    def get_extended_frame_bounds(self, hwnd: int) -> tuple[int, int, int, int] | None: ...

    def get_monitor_work_area(self, hwnd: int) -> tuple[int, int, int, int] | None: ...


@runtime_checkable
class MediaVerificationBackend(Protocol):
    """OS probe boundary for active system media transport session state."""

    def get_active_session(self, timeout_s: float = 0.5) -> MediaSessionState: ...


def _load_windll(name: str) -> Any:
    """Load a Win32 DLL with use_last_error=True, returning None off Windows."""
    if sys.platform != "win32":
        return None
    try:
        return ctypes.WinDLL(name, use_last_error=True)
    except OSError:
        return None


def _configure_guard_win32_dlls(
    *,
    user32: Any = None,
    dwmapi: Any = None,
    kernel32: Any = None,
) -> None:
    """Configure explicit 64-bit c_void_p ctypes signatures on Win32 DLL objects."""
    if isinstance(user32, ctypes.CDLL):
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = ctypes.c_void_p

        if hasattr(user32, "IsWindow"):
            user32.IsWindow.argtypes = [ctypes.c_void_p]
            user32.IsWindow.restype = wintypes.BOOL

        if hasattr(user32, "IsWindowVisible"):
            user32.IsWindowVisible.argtypes = [ctypes.c_void_p]
            user32.IsWindowVisible.restype = wintypes.BOOL

        user32.IsIconic.argtypes = [ctypes.c_void_p]
        user32.IsIconic.restype = wintypes.BOOL

        user32.GetWindowThreadProcessId.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
        ]
        user32.GetWindowThreadProcessId.restype = wintypes.DWORD

        user32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
        user32.GetWindowTextLengthW.restype = ctypes.c_int

        user32.GetWindowTextW.argtypes = [
            ctypes.c_void_p,
            wintypes.LPWSTR,
            ctypes.c_int,
        ]
        user32.GetWindowTextW.restype = ctypes.c_int

        user32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]
        user32.GetWindowRect.restype = wintypes.BOOL

        user32.MonitorFromWindow.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        user32.MonitorFromWindow.restype = ctypes.c_void_p

        user32.GetMonitorInfoW.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(_MONITORINFOEXW),
        ]
        user32.GetMonitorInfoW.restype = wintypes.BOOL

        if hasattr(user32, "GetAncestor"):
            user32.GetAncestor.argtypes = [ctypes.c_void_p, wintypes.UINT]
            user32.GetAncestor.restype = ctypes.c_void_p

        if hasattr(user32, "IsChild"):
            user32.IsChild.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
            user32.IsChild.restype = wintypes.BOOL

        if hasattr(user32, "GetWindow"):
            user32.GetWindow.argtypes = [ctypes.c_void_p, wintypes.UINT]
            user32.GetWindow.restype = ctypes.c_void_p

        if hasattr(user32, "GetWindowLongW"):
            user32.GetWindowLongW.argtypes = [ctypes.c_void_p, ctypes.c_int]
            user32.GetWindowLongW.restype = ctypes.c_long

        if hasattr(user32, "WindowFromPoint"):
            user32.WindowFromPoint.argtypes = [wintypes.POINT]
            user32.WindowFromPoint.restype = ctypes.c_void_p

        if hasattr(user32, "GetClassNameW"):
            user32.GetClassNameW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR, ctypes.c_int]
            user32.GetClassNameW.restype = ctypes.c_int

    if isinstance(dwmapi, ctypes.CDLL) and hasattr(dwmapi, "DwmGetWindowAttribute"):
        dwmapi.DwmGetWindowAttribute.argtypes = [
            ctypes.c_void_p,
            wintypes.DWORD,
            ctypes.c_void_p,
            wintypes.DWORD,
        ]
        dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long

    if isinstance(kernel32, ctypes.CDLL):
        kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
        kernel32.OpenProcess.restype = ctypes.c_void_p

        if hasattr(kernel32, "QueryFullProcessImageNameW"):
            kernel32.QueryFullProcessImageNameW.argtypes = [
                ctypes.c_void_p,
                wintypes.DWORD,
                wintypes.LPWSTR,
                ctypes.POINTER(wintypes.DWORD),
            ]
            kernel32.QueryFullProcessImageNameW.restype = wintypes.BOOL

        kernel32.CloseHandle.argtypes = [ctypes.c_void_p]
        kernel32.CloseHandle.restype = wintypes.BOOL


class Win32FocusPreconditionBackend:
    """Win32 implementation of ``FocusPreconditionBackend`` with 64-bit pointer safety."""

    def __init__(
        self,
        *,
        user32: Any = None,
        dwmapi: Any = None,
        kernel32: Any = None,
    ) -> None:
        self._user32 = user32 if user32 is not None else _load_windll("user32")
        is_live_u32 = user32 is None or isinstance(user32, ctypes.CDLL)
        self._dwmapi = (
            dwmapi if dwmapi is not None else (_load_windll("dwmapi") if is_live_u32 else None)
        )
        self._kernel32 = (
            kernel32
            if kernel32 is not None
            else (_load_windll("kernel32") if is_live_u32 else None)
        )
        _enable_dpi_awareness_guards(self._user32)
        _configure_guard_win32_dlls(
            user32=self._user32,
            dwmapi=self._dwmapi,
            kernel32=self._kernel32,
        )

    def _read_title(self, hwnd: int) -> str:
        if self._user32 is None or not hwnd:
            return ""
        try:
            length = 0
            if hasattr(self._user32, "GetWindowTextLengthW"):
                raw_len = self._user32.GetWindowTextLengthW(hwnd)
                length = int(raw_len) if isinstance(raw_len, int | bool) else 0
            buf_len = max(length + 1, 256)
            buf = ctypes.create_unicode_buffer(buf_len)
            copied = self._user32.GetWindowTextW(hwnd, buf, buf_len)
            if isinstance(copied, str):
                return copied
            return buf.value if copied else ""
        except Exception:
            return ""

    def _read_pid_and_process(self, hwnd: int) -> tuple[int, str]:
        if self._user32 is None or not hwnd:
            return 0, ""
        pid_val = 0
        try:
            pid = wintypes.DWORD(0)
            self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            pid_val = int(pid.value)
        except Exception:
            return 0, ""

        if pid_val <= 0 or self._kernel32 is None or not hasattr(self._kernel32, "OpenProcess"):
            return pid_val, ""

        h_proc = None
        try:
            h_proc = self._kernel32.OpenProcess(
                PROCESS_QUERY_LIMITED_INFORMATION,
                False,
                pid_val,
            )
            if not h_proc:
                return pid_val, ""
            if hasattr(self._kernel32, "QueryFullProcessImageNameW"):
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(1024)
                if self._kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
                    return pid_val, Path(buf.value).name
            return pid_val, ""
        except Exception:
            return pid_val, ""
        finally:
            if h_proc:
                with contextlib.suppress(Exception):
                    self._kernel32.CloseHandle(h_proc)

    def is_window_valid(self, hwnd: int) -> bool:
        if not hwnd or hwnd <= 0 or self._user32 is None:
            return False
        try:
            if hasattr(self._user32, "IsWindow"):
                res = self._user32.IsWindow(hwnd)
                if isinstance(res, int | bool):
                    return bool(res)
            if hasattr(self._user32, "IsWindowVisible"):
                vis = self._user32.IsWindowVisible(hwnd)
                if isinstance(vis, int | bool):
                    return bool(vis)
                return bool(vis)
        except Exception:
            return False
        return False

    def is_minimized(self, hwnd: int) -> bool:
        if not hwnd or hwnd <= 0 or self._user32 is None:
            return False
        if not hasattr(self._user32, "IsIconic"):
            return False
        try:
            res = self._user32.IsIconic(hwnd)
            if not isinstance(res, int | bool):
                return False
            return bool(res)
        except Exception:
            return False

    def get_extended_frame_bounds(self, hwnd: int) -> tuple[int, int, int, int] | None:
        if not hwnd or hwnd <= 0:
            return None
        rect = _RECT()
        if self._dwmapi is not None and hasattr(self._dwmapi, "DwmGetWindowAttribute"):
            with contextlib.suppress(Exception):
                hr = self._dwmapi.DwmGetWindowAttribute(
                    hwnd,
                    DWMWA_EXTENDED_FRAME_BOUNDS,
                    ctypes.byref(rect),
                    ctypes.sizeof(rect),
                )
                if hr == 0 and rect.right > rect.left and rect.bottom > rect.top:
                    return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))

        if self._user32 is not None and hasattr(self._user32, "GetWindowRect"):
            with contextlib.suppress(Exception):
                if (
                    self._user32.GetWindowRect(hwnd, ctypes.byref(rect))
                    and rect.right > rect.left
                    and rect.bottom > rect.top
                ):
                    return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))

        if self._user32 is not None and not isinstance(self._user32, ctypes.CDLL):
            moved_rects = getattr(self._user32, "_clea_last_moved_rects", None)
            if isinstance(moved_rects, dict):
                cached = moved_rects.get(int(hwnd))
                if (
                    isinstance(cached, tuple)
                    and len(cached) == 4
                    and cached[2] > cached[0]
                    and cached[3] > cached[1]
                ):
                    return cached
        return None

    def get_monitor_work_area(self, hwnd: int) -> tuple[int, int, int, int] | None:
        if not hwnd or hwnd <= 0 or self._user32 is None:
            return None
        if hasattr(self._user32, "MonitorFromWindow") and hasattr(
            self._user32, "GetMonitorInfoW"
        ):
            try:
                h_monitor = self._user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
                if h_monitor:
                    mi = _MONITORINFOEXW()
                    mi.cbSize = ctypes.sizeof(_MONITORINFOEXW)
                    if self._user32.GetMonitorInfoW(h_monitor, ctypes.byref(mi)):
                        left = int(mi.rcWork.left)
                        top = int(mi.rcWork.top)
                        right = int(mi.rcWork.right)
                        bottom = int(mi.rcWork.bottom)
                        if right > left and bottom > top:
                            return (left, top, right, bottom)
            except Exception:
                pass
        if not isinstance(self._user32, ctypes.CDLL):
            cached = getattr(self._user32, "_clea_last_work_areas", None)
            if isinstance(cached, dict) and int(hwnd) in cached:
                val = cached[int(hwnd)]
                if isinstance(val, tuple) and len(val) == 4:
                    return (int(val[0]), int(val[1]), int(val[2]), int(val[3]))
        return None

    def get_window_context(self, hwnd: int) -> WindowContext | None:
        if not hwnd or hwnd <= 0 or not self.is_window_valid(hwnd):
            return None
        title = self._read_title(hwnd)
        pid, process_name = self._read_pid_and_process(hwnd)
        minimized = self.is_minimized(hwnd)
        bounds = None if minimized else self.get_extended_frame_bounds(hwnd)
        work_area = self.get_monitor_work_area(hwnd)
        return WindowContext(
            hwnd=int(hwnd),
            title=title,
            process_name=process_name,
            pid=pid,
            bounds=bounds,
            work_area=work_area,
            minimized=minimized,
        )

    def get_root_hwnd(self, hwnd: int) -> int:
        if not hwnd or hwnd <= 0 or self._user32 is None:
            return int(hwnd) if hwnd else 0
        if hasattr(self._user32, "GetAncestor"):
            with contextlib.suppress(Exception):
                root = self._user32.GetAncestor(hwnd, 2)
                if root:
                    return int(root)
        return int(hwnd)

    def get_root_owner_hwnd(self, hwnd: int) -> int:
        if not hwnd or hwnd <= 0 or self._user32 is None:
            return int(hwnd) if hwnd else 0
        if hasattr(self._user32, "GetAncestor"):
            with contextlib.suppress(Exception):
                root_owner = self._user32.GetAncestor(hwnd, 3)
                if root_owner:
                    return int(root_owner)
        return int(hwnd)

    def _read_pid(self, hwnd: int) -> int:
        if (
            self._user32 is None
            or not hwnd
            or not hasattr(self._user32, "GetWindowThreadProcessId")
        ):
            return 0
        try:
            pid = wintypes.DWORD(0)
            self._user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
            return int(pid.value)
        except Exception:
            return 0

    def _get_owner_hwnd(self, hwnd: int) -> int:
        if not hwnd or hwnd <= 0 or self._user32 is None or not hasattr(self._user32, "GetWindow"):
            return 0
        try:
            owner = self._user32.GetWindow(hwnd, _GW_OWNER)
            if isinstance(owner, int) and not isinstance(owner, bool) and owner > 0:
                return int(owner)
        except Exception:
            return 0
        return 0

    def _is_owned_or_popup_window(self, hwnd: int) -> bool:
        if not hwnd or hwnd <= 0 or self._user32 is None:
            return False
        if self._get_owner_hwnd(hwnd) > 0:
            return True
        if hasattr(self._user32, "GetWindowLongW"):
            with contextlib.suppress(Exception):
                style = self._user32.GetWindowLongW(hwnd, _GWL_STYLE)
                if isinstance(style, int) and not isinstance(style, bool):
                    if style & _WS_POPUP:
                        return True
        if hasattr(self._user32, "GetClassNameW"):
            with contextlib.suppress(Exception):
                buf = ctypes.create_unicode_buffer(128)
                copied = self._user32.GetClassNameW(hwnd, buf, 128)
                cls_name = buf.value.strip().lower() if copied else ""
                if cls_name == "#32768" or any(
                    tok in cls_name for tok in ("popup", "menu", "dropdown", "tooltip")
                ):
                    return True
        return False

    def is_same_window_tree(self, expected_hwnd: int, actual_hwnd: int) -> bool:
        if not expected_hwnd or not actual_hwnd or expected_hwnd <= 0 or actual_hwnd <= 0:
            return False
        exp_int = int(expected_hwnd)
        act_int = int(actual_hwnd)
        if exp_int == act_int:
            return True
        if self._user32 is None:
            return False
        if hasattr(self._user32, "IsChild"):
            with contextlib.suppress(Exception):
                if bool(self._user32.IsChild(exp_int, act_int)) or bool(
                    self._user32.IsChild(act_int, exp_int)
                ):
                    return True
        exp_root = exp_int
        act_root = act_int
        exp_owner = exp_int
        act_owner = act_int
        if hasattr(self._user32, "GetAncestor"):
            exp_root = self.get_root_hwnd(exp_int)
            act_root = self.get_root_hwnd(act_int)
            if exp_root and act_root and exp_root == act_root:
                return True
            exp_owner = self.get_root_owner_hwnd(exp_int)
            act_owner = self.get_root_owner_hwnd(act_int)
            if exp_owner and act_owner and (
                exp_owner == act_owner
                or exp_owner == act_int
                or act_owner == exp_int
                or exp_owner == act_root
                or act_owner == exp_root
            ):
                return True

        # Walk explicit Win32 GW_OWNER chain (owned menus, modals, dropdown popups)
        target_anchors = {h for h in (exp_int, exp_root, exp_owner) if h > 0}
        cur = act_int
        seen_owners: set[int] = {cur}
        for _ in range(6):
            parent_owner = self._get_owner_hwnd(cur)
            if parent_owner <= 0:
                break
            if parent_owner in target_anchors:
                return True
            if parent_owner in seen_owners:
                break
            seen_owners.add(parent_owner)
            cur = parent_owner

        # Same non-zero PID + popup/owned/menu window class (common in Electron/WinUI menus)
        if self._is_owned_or_popup_window(act_int) or (
            act_root != act_int and self._is_owned_or_popup_window(act_root)
        ):
            exp_pid = self._read_pid(exp_int)
            act_pid = self._read_pid(act_int)
            if exp_pid > 0 and act_pid > 0 and exp_pid == act_pid:
                return True
        return False

    def window_from_point(self, px: int, py: int) -> int | None:
        """Return the HWND physically under screen point *(px, py)* via Win32 WindowFromPoint."""
        if self._user32 is None or not hasattr(self._user32, "WindowFromPoint"):
            return None
        try:
            hwnd = self._user32.WindowFromPoint(wintypes.POINT(int(px), int(py)))
            if isinstance(hwnd, int) and not isinstance(hwnd, bool) and hwnd > 0:
                return int(hwnd)
        except Exception:
            return None
        return None

    def get_foreground_hwnd(self) -> int | None:
        """Fast foreground HWND lookup without OpenProcess or DWM queries."""
        if self._user32 is None or not hasattr(self._user32, "GetForegroundWindow"):
            return None
        try:
            fg = self._user32.GetForegroundWindow()
            if isinstance(fg, int) and not isinstance(fg, bool) and fg > 0:
                return int(fg)
        except Exception:
            return None
        return None

    def get_foreground_context(self) -> WindowContext | None:
        if self._user32 is None or not hasattr(self._user32, "GetForegroundWindow"):
            return None
        try:
            fg = self._user32.GetForegroundWindow()
            hwnd = int(fg) if fg else 0
        except Exception:
            return None
        if hwnd <= 0:
            return None
        return self.get_window_context(hwnd)

    def _is_window_cloaked(self, hwnd: int) -> bool:
        if not hwnd or hwnd <= 0 or self._dwmapi is None:
            return False
        if not hasattr(self._dwmapi, "DwmGetWindowAttribute"):
            return False
        try:
            cloaked = ctypes.c_uint32(0)
            hr = self._dwmapi.DwmGetWindowAttribute(
                hwnd,
                14,  # DWMWA_CLOAKED
                ctypes.byref(cloaked),
                ctypes.sizeof(cloaked),
            )
            return hr == 0 and bool(cloaked.value)
        except Exception:
            return False

    def _is_overlay_or_tool_window(self, hwnd: int) -> bool:
        if not hwnd or hwnd <= 0 or self._user32 is None:
            return False
        if not hasattr(self._user32, "GetWindowLongW"):
            return False
        try:
            raw_style = self._user32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
            if not isinstance(raw_style, int) or isinstance(raw_style, bool):
                return False
            # WS_EX_TOOLWINDOW (0x80) or WS_EX_TOPMOST (0x8) floating overlays
            return bool(raw_style & (0x00000080 | 0x00000008))
        except Exception:
            return False

    def find_occluding_windows(
        self,
        hwnd: int,
        *,
        target_bounds: tuple[int, int, int, int] | None = None,
        tolerance_px: int = _DEFAULT_BOUNDS_TOLERANCE_PX,
    ) -> list[dict[str, Any]]:
        """Return visible, non-overlay windows strictly above *hwnd* in Z-order that overlap it."""
        if not hwnd or hwnd <= 0 or self._user32 is None:
            return []
        bounds = (
            target_bounds
            if target_bounds is not None
            else self.get_extended_frame_bounds(hwnd)
        )
        if bounds is None:
            return []
        tl, tt, tr, tb = bounds
        target_area = max(1, (tr - tl) * (tb - tt))

        higher_hwnds: list[int] = []
        seen: set[int] = {int(hwnd)}
        used_get_window = False
        if hasattr(self._user32, "GetWindow"):
            cur = int(hwnd)
            for _ in range(256):
                try:
                    raw_prev = self._user32.GetWindow(cur, 3)  # GW_HWNDPREV
                except Exception:
                    break
                if not isinstance(raw_prev, int) or isinstance(raw_prev, bool):
                    break
                used_get_window = True
                if raw_prev <= 0 or raw_prev in seen:
                    break
                seen.add(raw_prev)
                higher_hwnds.append(raw_prev)
                cur = raw_prev

        if not used_get_window and hasattr(self._user32, "EnumWindows"):
            enum_higher: list[int] = []
            found_target = False

            def _on_enum(raw_h: int, _lparam: Any) -> bool:
                nonlocal found_target
                if found_target:
                    return False
                if not isinstance(raw_h, int) or isinstance(raw_h, bool) or raw_h <= 0:
                    return True
                if raw_h == int(hwnd) or self.is_same_window_tree(hwnd, raw_h):
                    found_target = True
                    return False
                if raw_h not in seen:
                    seen.add(raw_h)
                    enum_higher.append(raw_h)
                return True

            with contextlib.suppress(Exception):
                if isinstance(self._user32, ctypes.CDLL) and hasattr(ctypes, "WINFUNCTYPE"):
                    cb = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, wintypes.LPARAM)(
                        _on_enum
                    )
                    self._user32.EnumWindows(cb, 0)
                else:
                    self._user32.EnumWindows(_on_enum, 0)
            if found_target:
                higher_hwnds = enum_higher

        ignored_titles = {"program manager", "windows input experience", "default ime"}
        occluders: list[dict[str, Any]] = []
        for other in higher_hwnds:
            if self.is_same_window_tree(hwnd, other):
                continue
            if hasattr(self._user32, "IsWindowVisible"):
                try:
                    vis = self._user32.IsWindowVisible(other)
                    if not (isinstance(vis, int | bool) and bool(vis)):
                        continue
                except Exception:
                    continue
            if self.is_minimized(other):
                continue
            if self._is_overlay_or_tool_window(other):
                continue
            if self._is_window_cloaked(other):
                continue
            title = self._read_title(other).strip()
            if not title or title.lower() in ignored_titles:
                continue
            ob = self.get_extended_frame_bounds(other)
            if ob is None:
                continue
            ol, ot, or_, obot = ob
            if (or_ - ol) < 80 or (obot - ot) < 80:
                continue
            ix_l = max(tl, ol)
            ix_t = max(tt, ot)
            ix_r = min(tr, or_)
            ix_b = min(tb, obot)
            if (ix_r - ix_l) <= tolerance_px or (ix_b - ix_t) <= tolerance_px:
                continue
            overlap_area = (ix_r - ix_l) * (ix_b - ix_t)
            overlap_ratio = overlap_area / float(target_area)
            if overlap_ratio < 0.05:
                continue
            _, proc_name = self._read_pid_and_process(other)
            occluders.append(
                {
                    "hwnd": other,
                    "title": title,
                    "process_name": proc_name,
                    "bounds": ob,
                    "overlap_ratio": round(overlap_ratio, 3),
                }
            )
        return occluders


# ---------------------------------------------------------------------------
# PreconditionGuard
# ---------------------------------------------------------------------------


class PreconditionGuard:
    """Context manager checking target window validity and focus before every step."""

    def __init__(
        self,
        backend: FocusPreconditionBackend,
        expected_hwnd: int | None = None,
        require_foreground: bool = True,
        allow_minimized: bool = False,
    ) -> None:
        self._backend = backend
        self._expected_hwnd = expected_hwnd
        self._require_foreground = require_foreground
        self._allow_minimized = allow_minimized
        self._target: WindowContext | None = None

    @property
    def target(self) -> WindowContext | None:
        """Locked target ``WindowContext`` resolved upon ``__enter__``."""
        return self._target

    def __enter__(self) -> PreconditionGuard:
        if self._expected_hwnd is not None:
            ctx = self._backend.get_window_context(self._expected_hwnd)
            if ctx is None or ctx.hwnd <= 0:
                raise PreconditionFailedError(
                    reason="no_target_window",
                    expected_hwnd=self._expected_hwnd,
                )
            self._target = ctx
        else:
            ctx = self._backend.get_foreground_context()
            if ctx is None or ctx.hwnd <= 0:
                raise PreconditionFailedError(reason="no_target_window")
            self._target = ctx
        return self

    def __exit__(self, exc_type: Any, exc: Any, tb: Any) -> bool:
        return False

    def assert_ready(self, step_label: str = "", *, fast: bool = False) -> WindowContext:
        """Validate target existence, unminimized state, and foreground focus."""
        if self._target is None:
            raise PreconditionFailedError(
                reason="no_target_window",
                expected_hwnd=self._expected_hwnd,
                step_label=step_label,
            )

        target = self._target
        if not self._backend.is_window_valid(target.hwnd):
            raise PreconditionFailedError(
                reason="window_closed",
                expected_hwnd=target.hwnd,
                expected_title=target.title,
                expected_process=target.process_name,
                step_label=step_label,
            )

        if not self._allow_minimized and self._backend.is_minimized(target.hwnd):
            raise PreconditionFailedError(
                reason="window_minimized",
                expected_hwnd=target.hwnd,
                expected_title=target.title,
                expected_process=target.process_name,
                step_label=step_label,
            )

        if self._require_foreground:
            fast_verified = False
            if hasattr(self._backend, "get_foreground_hwnd"):
                fg_hwnd = self._backend.get_foreground_hwnd()
                if fg_hwnd is not None and fg_hwnd > 0:
                    if fg_hwnd == target.hwnd or (
                        hasattr(self._backend, "is_same_window_tree")
                        and self._backend.is_same_window_tree(target.hwnd, fg_hwnd)
                    ):
                        fast_verified = True
            if not fast_verified:
                fg = self._backend.get_foreground_context()
                fg_matches = fg is not None and fg.hwnd == target.hwnd
                if (
                    not fg_matches
                    and fg is not None
                    and hasattr(self._backend, "is_same_window_tree")
                ):
                    fg_matches = bool(self._backend.is_same_window_tree(target.hwnd, fg.hwnd))
                if not fg_matches:
                    raise PreconditionFailedError(
                        reason="focus_lost",
                        expected_hwnd=target.hwnd,
                        expected_title=target.title,
                        expected_process=target.process_name,
                        actual_hwnd=fg.hwnd if fg is not None else None,
                        actual_title=fg.title if fg is not None else "",
                        actual_process=fg.process_name if fg is not None else "",
                        step_label=step_label,
                    )

        if fast:
            return target
        return self._backend.get_window_context(target.hwnd) or target

    def assert_point_targets_window(
        self,
        px: int,
        py: int,
        *,
        step_label: str = "",
    ) -> WindowContext:
        """Verify *(px, py)* lies inside target bounds and hits target's window tree."""
        ctx = self.assert_ready(step_label=step_label)
        bounds = ctx.bounds
        if bounds is not None and len(bounds) == 4:
            left, top, right, bottom = bounds
            if right > left and bottom > top:
                if not (left <= int(px) < right and top <= int(py) < bottom):
                    raise PreconditionFailedError(
                        reason="target_point_outside_window",
                        expected_hwnd=ctx.hwnd,
                        expected_title=ctx.title,
                        expected_process=ctx.process_name,
                        step_label=step_label,
                    )

        if hasattr(self._backend, "window_from_point"):
            hit_hwnd = self._backend.window_from_point(int(px), int(py))
            if hit_hwnd is not None and hit_hwnd > 0:
                same_tree = hit_hwnd == ctx.hwnd or (
                    hasattr(self._backend, "is_same_window_tree")
                    and bool(self._backend.is_same_window_tree(ctx.hwnd, hit_hwnd))
                )
                if not same_tree:
                    occ = self._backend.get_window_context(hit_hwnd)
                    raise PreconditionFailedError(
                        reason="target_point_occluded",
                        expected_hwnd=ctx.hwnd,
                        expected_title=ctx.title,
                        expected_process=ctx.process_name,
                        actual_hwnd=hit_hwnd,
                        actual_title=occ.title if occ is not None else "",
                        actual_process=occ.process_name if occ is not None else "",
                        step_label=step_label,
                    )
        return ctx


# ---------------------------------------------------------------------------
# WinRT Media Session Verification Backend
# ---------------------------------------------------------------------------


_WINRT_PLAYBACK_STATUS_NAMES: dict[int, str] = {
    0: "closed",
    1: "unknown",  # GlobalSystemMediaTransportControlsSessionPlaybackStatus.OPENED
    2: "changing",
    3: "stopped",
    4: "playing",
    5: "paused",
}


def _normalize_playback_status(raw_status: Any) -> str:
    if raw_status is None:
        return "unknown"
    name = getattr(raw_status, "name", None)
    if isinstance(name, str) and name.strip():
        lowered = name.strip().lower()
        if lowered in {"playing", "paused", "stopped", "closed", "changing"}:
            return lowered
        if lowered == "opened":
            return "unknown"
    with contextlib.suppress(TypeError, ValueError):
        return _WINRT_PLAYBACK_STATUS_NAMES.get(int(raw_status), "unknown")
    text = str(raw_status).strip().lower()
    if text in {"playing", "paused", "stopped", "closed", "changing", "unavailable"}:
        return text
    return "unknown"


class WinRTMediaVerificationBackend:
    """WinRT GlobalSystemMediaTransportControls session reader with clean fallback."""

    def __init__(
        self,
        *,
        module_loader: Callable[[str], Any] | None = None,
    ) -> None:
        self._module_loader = module_loader or importlib.import_module

    def _import_control_module(self) -> Any | None:
        try:
            return self._module_loader("winrt.windows.media.control")
        except ImportError:
            return None
        except Exception as exc:
            logger.debug("WinRT media control import failed: %s", exc)
            return None

    async def _query_session_async(self, control_mod: Any) -> MediaSessionState:
        mgr_cls = control_mod.GlobalSystemMediaTransportControlsSessionManager
        manager = await mgr_cls.request_async()
        if manager is None:
            return MediaSessionState(
                playback_status="unavailable",
                available=True,
                error="no_media_session",
            )
        session = manager.get_current_session()
        if session is None:
            return MediaSessionState(
                playback_status="unavailable",
                available=True,
                error="no_media_session",
            )

        playback_status = "unknown"
        with contextlib.suppress(Exception):
            info = session.get_playback_info()
            if info is not None:
                playback_status = _normalize_playback_status(getattr(info, "playback_status", None))

        title = ""
        artist = ""
        with contextlib.suppress(Exception):
            props = await session.try_get_media_properties_async()
            if props is not None:
                title = str(getattr(props, "title", "") or "")
                artist = str(getattr(props, "artist", "") or "")

        source_app = str(getattr(session, "source_app_user_model_id", "") or "")
        return MediaSessionState(
            playback_status=playback_status,
            title=title,
            artist=artist,
            source_app=source_app,
            available=True,
            error=None,
        )

    def get_active_session(self, timeout_s: float = 0.5) -> MediaSessionState:
        control_mod = self._import_control_module()
        if control_mod is None:
            return MediaSessionState(
                playback_status="unavailable",
                available=False,
                error="winrt_unavailable",
            )

        bounded_timeout = max(0.05, float(timeout_s))

        async def _runner() -> MediaSessionState:
            return await asyncio.wait_for(
                self._query_session_async(control_mod),
                timeout=bounded_timeout,
            )

        in_running_loop = False
        try:
            asyncio.get_running_loop()
            in_running_loop = True
        except RuntimeError:
            in_running_loop = False

        if not in_running_loop:
            try:
                return asyncio.run(_runner())
            except TimeoutError:
                return MediaSessionState(
                    playback_status="unavailable",
                    available=True,
                    error="winrt_timeout",
                )
            except Exception as exc:
                logger.debug("WinRT media query failed: %s", exc)
                return MediaSessionState(
                    playback_status="unavailable",
                    available=True,
                    error=f"winrt_error:{exc}",
                )

        result_box: list[MediaSessionState] = []
        error_box: list[BaseException] = []

        def _thread_target() -> None:
            try:
                result_box.append(asyncio.run(_runner()))
            except BaseException as exc:  # pragma: no cover - defensive thread boundary
                error_box.append(exc)

        worker = threading.Thread(
            target=_thread_target,
            name="clea-winrt-media-probe",
            daemon=True,
        )
        worker.start()
        worker.join(timeout=bounded_timeout + 0.1)
        if worker.is_alive():
            return MediaSessionState(
                playback_status="unavailable",
                available=True,
                error="winrt_timeout",
            )
        if error_box:
            err = error_box[0]
            if isinstance(err, TimeoutError | asyncio.TimeoutError):
                return MediaSessionState(
                    playback_status="unavailable",
                    available=True,
                    error="winrt_timeout",
                )
            return MediaSessionState(
                playback_status="unavailable",
                available=True,
                error=f"winrt_error:{err}",
            )
        if result_box:
            return result_box[0]
        return MediaSessionState(
            playback_status="unavailable",
            available=True,
            error="no_media_session",
        )


# ---------------------------------------------------------------------------
# ActionVerifier (Closed-Loop Post-Condition Verifiers)
# ---------------------------------------------------------------------------


def _rect_to_xywh(rect: tuple[int, int, int, int]) -> list[int]:
    left, top, right, bottom = rect
    return [left, top, right - left, bottom - top]


def _bounds_within_tolerance(
    actual: tuple[int, int, int, int],
    expected: tuple[int, int, int, int],
    tolerance_px: int,
) -> bool:
    al, at, ar, ab = actual
    el, et, er, eb = expected
    aw, ah = ar - al, ab - at
    ew, eh = er - el, eb - et
    return (
        abs(al - el) <= tolerance_px
        and abs(at - et) <= tolerance_px
        and abs(ar - er) <= tolerance_px
        and abs(ab - eb) <= tolerance_px
        and abs(aw - ew) <= tolerance_px
        and abs(ah - eh) <= tolerance_px
    )


class ActionVerifier:
    """Zero-token deterministic closed-loop verifier for OS media and window actions."""

    def __init__(
        self,
        focus_backend: FocusPreconditionBackend | None = None,
        media_backend: MediaVerificationBackend | None = None,
        *,
        monotonic_fn: Callable[[], float] = time.monotonic,
        sleep_fn: Callable[[float], None] = time.sleep,
    ) -> None:
        self._focus_backend = focus_backend
        self._media_backend = media_backend
        self._monotonic = monotonic_fn
        self._sleep = sleep_fn

    def _get_focus_backend(self) -> FocusPreconditionBackend:
        if self._focus_backend is None:
            self._focus_backend = Win32FocusPreconditionBackend()
        return self._focus_backend

    def _get_media_backend(self) -> MediaVerificationBackend:
        if self._media_backend is None:
            self._media_backend = WinRTMediaVerificationBackend()
        return self._media_backend

    def probe_media_session(self, timeout_s: float = 0.4) -> MediaSessionState | None:
        """Read a single non-polling snapshot of the active OS media session."""
        backend = self._get_media_backend()
        try:
            return backend.get_active_session(timeout_s=timeout_s)
        except Exception as exc:
            logger.debug("Media probe failed: %s", exc)
            return MediaSessionState(
                playback_status="unavailable",
                available=False,
                error=f"probe_error:{exc}",
            )

    def verify_media_state(
        self,
        expected_status: str | None = None,
        expected_title_contains: str | None = None,
        timeout_s: float = 1.5,
        poll_interval_s: float = 0.15,
        *,
        baseline: MediaSessionState | None = None,
    ) -> VerificationOutcome:
        """Poll OS media transport controls until state matches or timeout expires."""
        backend = self._get_media_backend()
        start = self._monotonic()
        deadline = start + max(0.0, float(timeout_s))
        interval = max(0.01, float(poll_interval_s))
        norm_expected = expected_status.strip().lower() if expected_status else None
        title_needle = expected_title_contains.strip().lower() if expected_title_contains else None

        last_session = MediaSessionState(
            playback_status="unavailable",
            available=False,
            error="no_media_session",
        )
        last_reason = "media_state_mismatch"

        while True:
            remaining = max(0.05, deadline - self._monotonic())
            raw = backend.get_active_session(timeout_s=min(0.5, remaining))
            if raw is None:
                session = MediaSessionState(
                    playback_status="unavailable",
                    available=True,
                    error="no_media_session",
                )
            else:
                session = raw
            last_session = session

            if not session.available and session.playback_status == "unavailable":
                elapsed_ms = max(0.0, (self._monotonic() - start) * 1000.0)
                return VerificationOutcome(
                    verified=False,
                    status="unavailable",
                    reason=session.error or "media_verification_unavailable",
                    actual_status="unavailable",
                    track_title=session.title,
                    artist=session.artist,
                    source_app=session.source_app,
                    elapsed_ms=elapsed_ms,
                    actual=session.to_dict(),
                )

            actual_stat = session.playback_status.strip().lower()
            if actual_stat == "unavailable":
                last_reason = session.error or "no_media_session"
            else:
                status_ok = True
                if norm_expected is not None:
                    status_ok = actual_stat == norm_expected
                    if not status_ok:
                        last_reason = f"expected_{norm_expected}_got_{actual_stat}"

                title_ok = True
                if title_needle is not None:
                    title_ok = title_needle in session.title.lower()
                    if not title_ok:
                        last_reason = "title_mismatch"

                baseline_ok = True
                if (
                    norm_expected is None
                    and title_needle is None
                    and baseline is not None
                    and baseline.available
                    and baseline.playback_status != "unavailable"
                ):
                    if actual_stat == "changing":
                        baseline_ok = False
                        last_reason = "media_state_changing"
                    else:
                        changed = (
                            actual_stat != baseline.playback_status.strip().lower()
                            or (bool(session.title) and session.title != baseline.title)
                            or (bool(session.artist) and session.artist != baseline.artist)
                        )
                        baseline_ok = changed
                        if not changed:
                            last_reason = "media_state_unchanged"
                elif norm_expected is None and title_needle is None:
                    if actual_stat not in {"playing", "paused", "stopped"}:
                        status_ok = False
                        last_reason = f"inactive_media_state_{actual_stat}"

                if status_ok and title_ok and baseline_ok:
                    elapsed_ms = max(0.0, (self._monotonic() - start) * 1000.0)
                    reason = (
                        "media_state_changed"
                        if (norm_expected is None and title_needle is None and baseline is not None)
                        else "media_state_matched"
                    )
                    return VerificationOutcome(
                        verified=True,
                        status="verified",
                        reason=reason,
                        actual_status=actual_stat,
                        track_title=session.title,
                        artist=session.artist,
                        source_app=session.source_app,
                        elapsed_ms=elapsed_ms,
                        actual=session.to_dict(),
                    )

            now = self._monotonic()
            if now >= deadline:
                elapsed_ms = max(0.0, (now - start) * 1000.0)
                final_status = (
                    "unavailable"
                    if last_session.playback_status == "unavailable"
                    else "timeout"
                )
                return VerificationOutcome(
                    verified=False,
                    status=final_status,
                    reason=last_reason,
                    actual_status=last_session.playback_status,
                    track_title=last_session.title,
                    artist=last_session.artist,
                    source_app=last_session.source_app,
                    elapsed_ms=elapsed_ms,
                    actual=last_session.to_dict(),
                )
            self._sleep(min(interval, max(0.0, deadline - now)))

    def verify_window_state(
        self,
        hwnd: int,
        expected_state: str | None = None,
        expected_snap: str | None = None,
        expected_bounds: tuple[int, int, int, int] | None = None,
        timeout_s: float = 0.6,
        poll_interval_s: float = 0.02,
        tolerance_px: int = _DEFAULT_BOUNDS_TOLERANCE_PX,
        check_occlusion: bool | None = None,
    ) -> VerificationOutcome:
        """Poll Win32 window state, DWM bounds, and Z-order occlusion until settled or timeout."""
        valid_states = {"closed", "minimized", "maximized", "focused", "restored"}
        valid_snaps = {"left", "snap_left", "right", "snap_right"}

        norm_state: str | None = None
        if expected_state is not None:
            norm_state = expected_state.strip().lower()
            if norm_state not in valid_states:
                raise ValueError(
                    f"Unsupported expected_state {expected_state!r}. "
                    f"Must be one of {sorted(valid_states)}."
                )

        norm_snap: str | None = None
        if expected_snap is not None:
            norm_snap = expected_snap.strip().lower()
            if norm_snap not in valid_snaps:
                raise ValueError(
                    f"Unsupported expected_snap {expected_snap!r}. "
                    f"Must be one of {sorted(valid_snaps)}."
                )

        if expected_bounds is not None:
            _bx, _by, bw, bh = expected_bounds
            if bw <= 0 or bh <= 0:
                raise ValueError("expected_bounds width and height must be positive.")

        should_check_occlusion = (
            bool(check_occlusion) if check_occlusion is not None else (norm_snap is not None)
        )

        backend = self._get_focus_backend()
        start = self._monotonic()
        deadline = start + max(0.0, float(timeout_s))
        interval = max(0.01, float(poll_interval_s))

        last_reason = "window_state_mismatch"
        last_bounds: tuple[int, int, int, int] | None = None
        last_expected_rect: tuple[int, int, int, int] | None = None
        last_work: tuple[int, int, int, int] | None = None
        last_minimized = False
        last_foreground_hwnd: int | None = None
        last_occluders: list[dict[str, Any]] = []

        while True:
            valid = backend.is_window_valid(hwnd)

            if norm_state == "closed":
                if not valid:
                    elapsed_ms = max(0.0, (self._monotonic() - start) * 1000.0)
                    return VerificationOutcome(
                        verified=True,
                        status="verified",
                        reason="window_closed",
                        actual_status="closed",
                        elapsed_ms=elapsed_ms,
                        actual={"hwnd": hwnd, "valid": False},
                    )
                last_reason = "window_still_open"
            elif not valid:
                elapsed_ms = max(0.0, (self._monotonic() - start) * 1000.0)
                return VerificationOutcome(
                    verified=False,
                    status="failed",
                    reason="window_closed",
                    actual_status="closed",
                    elapsed_ms=elapsed_ms,
                    actual={"hwnd": hwnd, "valid": False},
                )
            else:
                last_minimized = backend.is_minimized(hwnd)
                if norm_state == "minimized":
                    if last_minimized:
                        elapsed_ms = max(0.0, (self._monotonic() - start) * 1000.0)
                        return VerificationOutcome(
                            verified=True,
                            status="verified",
                            reason="window_minimized",
                            actual_status="minimized",
                            elapsed_ms=elapsed_ms,
                            actual={"hwnd": hwnd, "valid": True, "minimized": True},
                        )
                    last_reason = "window_not_minimized"
                else:
                    matched = True
                    last_occluders = []
                    if last_minimized:
                        matched = False
                        last_reason = "window_minimized"

                    if matched and norm_state == "focused":
                        fg = backend.get_foreground_context()
                        last_foreground_hwnd = fg.hwnd if fg is not None else None
                        fg_matches = last_foreground_hwnd == hwnd
                        if (
                            not fg_matches
                            and last_foreground_hwnd is not None
                            and hasattr(backend, "is_same_window_tree")
                        ):
                            fg_matches = bool(
                                backend.is_same_window_tree(hwnd, last_foreground_hwnd)
                            )
                        if not fg_matches:
                            matched = False
                            last_reason = "not_foreground"

                    needs_bounds = (
                        norm_state == "maximized"
                        or norm_snap is not None
                        or expected_bounds is not None
                    )
                    if matched and needs_bounds:
                        actual_bounds = backend.get_extended_frame_bounds(hwnd)
                        last_bounds = actual_bounds
                        work_area = backend.get_monitor_work_area(hwnd)
                        last_work = work_area

                        target_rect: tuple[int, int, int, int] | None = None
                        if expected_bounds is not None:
                            bx, by, bw, bh = expected_bounds
                            target_rect = (int(bx), int(by), int(bx + bw), int(by + bh))
                        elif work_area is not None:
                            wl, wt, wr, wb = work_area
                            work_w = wr - wl
                            half_w = work_w // 2
                            if norm_snap in {"left", "snap_left"}:
                                target_rect = (wl, wt, wl + half_w, wb)
                            elif norm_snap in {"right", "snap_right"}:
                                target_rect = (wl + half_w, wt, wr, wb)
                            elif norm_state == "maximized":
                                target_rect = (wl, wt, wr, wb)

                        last_expected_rect = target_rect
                        if actual_bounds is None:
                            matched = False
                            last_reason = "bounds_unavailable"
                        elif target_rect is None:
                            matched = False
                            last_reason = "work_area_unavailable"
                        elif not _bounds_within_tolerance(actual_bounds, target_rect, tolerance_px):
                            matched = False
                            last_reason = "bounds_mismatch"

                    if (
                        matched
                        and should_check_occlusion
                        and hasattr(backend, "find_occluding_windows")
                    ):
                        with contextlib.suppress(Exception):
                            occluders = backend.find_occluding_windows(
                                hwnd,
                                target_bounds=last_bounds,
                                tolerance_px=tolerance_px,
                            )
                            if isinstance(occluders, list) and occluders:
                                last_occluders = occluders
                                matched = False
                                last_reason = "occluded_by_unrelated_window"

                    if matched:
                        elapsed_ms = max(0.0, (self._monotonic() - start) * 1000.0)
                        actual_payload: dict[str, Any] = {
                            "hwnd": hwnd,
                            "valid": True,
                            "minimized": last_minimized,
                            "occluded_by_unrelated_window": False,
                        }
                        if last_foreground_hwnd is not None:
                            actual_payload["foreground_hwnd"] = last_foreground_hwnd
                        if last_bounds is not None:
                            actual_payload["bounds"] = _rect_to_xywh(last_bounds)
                        if last_expected_rect is not None:
                            actual_payload["expected_bounds"] = _rect_to_xywh(last_expected_rect)
                        if last_work is not None:
                            actual_payload["work_area"] = _rect_to_xywh(last_work)
                        return VerificationOutcome(
                            verified=True,
                            status="verified",
                            reason="window_state_matched",
                            actual_status=norm_state or norm_snap or "matched",
                            actual_bounds=last_bounds,
                            expected_bounds=last_expected_rect,
                            work_area=last_work,
                            elapsed_ms=elapsed_ms,
                            actual=actual_payload,
                            occluded_by_unrelated_window=False,
                        )

            now = self._monotonic()
            if now >= deadline:
                elapsed_ms = max(0.0, (now - start) * 1000.0)
                occluded = bool(last_occluders)
                occluder_names = tuple(
                    str(item.get("title") or item.get("process_name") or item.get("hwnd") or "")
                    for item in last_occluders
                    if isinstance(item, dict)
                )
                fail_payload: dict[str, Any] = {
                    "hwnd": hwnd,
                    "valid": valid,
                    "minimized": last_minimized,
                }
                if occluded:
                    fail_payload["occluded_by_unrelated_window"] = True
                    fail_payload["occluding_windows"] = list(occluder_names)
                if last_foreground_hwnd is not None:
                    fail_payload["foreground_hwnd"] = last_foreground_hwnd
                if last_bounds is not None:
                    fail_payload["bounds"] = _rect_to_xywh(last_bounds)
                if last_expected_rect is not None:
                    fail_payload["expected_bounds"] = _rect_to_xywh(last_expected_rect)
                if last_work is not None:
                    fail_payload["work_area"] = _rect_to_xywh(last_work)
                return VerificationOutcome(
                    verified=False,
                    status="timeout",
                    reason=last_reason,
                    actual_status=(
                        "occluded"
                        if occluded
                        else ("minimized" if last_minimized else "mismatch")
                    ),
                    actual_bounds=last_bounds,
                    expected_bounds=last_expected_rect,
                    work_area=last_work,
                    elapsed_ms=elapsed_ms,
                    actual=fail_payload,
                    occluded_by_unrelated_window=occluded,
                    occluding_windows=occluder_names,
                )
            self._sleep(min(interval, max(0.0, deadline - now)))

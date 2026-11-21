"""Window snapping/focus, hardware/media controls, and system diagnostics tools."""

from __future__ import annotations

import contextlib
import csv
import ctypes
import ctypes.wintypes as wintypes
import logging
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable
from pathlib import Path
from typing import Any

from clea.surfaces.voice.win32 import (
    _MONITORINFOEXW,
    _RECT,
    DWMWA_EXTENDED_FRAME_BOUNDS,
    PROCESS_QUERY_LIMITED_INFORMATION,
    resolve_app_display_name,
)
from clea.tools.guards import (
    ActionVerifier,
    FocusPreconditionBackend,
    MediaVerificationBackend,
    PreconditionFailedError,
    PreconditionGuard,
    Win32FocusPreconditionBackend,
)
from clea.tools.registry import RiskLevel, ToolRegistry

logger = logging.getLogger(__name__)

# Win32 Window constants
SW_HIDE = 0
SW_SHOWNORMAL = 1
SW_SHOWMINIMIZED = 2
SW_MAXIMIZE = 3
SW_SHOWNOACTIVATE = 4
SW_SHOW = 5
SW_MINIMIZE = 6
SW_SHOWMINNOACTIVE = 7
SW_SHOWNA = 8
SW_RESTORE = 9

WM_CLOSE = 0x0010
MONITOR_DEFAULTTONEAREST = 2
GA_ROOT = 2

# Win32 Virtual Key codes
VK_VOLUME_MUTE = 0xAD  # 173
VK_VOLUME_DOWN = 0xAE  # 174
VK_VOLUME_UP = 0xAF  # 175
VK_MEDIA_NEXT_TRACK = 0xB0  # 176
VK_MEDIA_PREV_TRACK = 0xB1  # 177
VK_MEDIA_STOP = 0xB2  # 178
VK_MEDIA_PLAY_PAUSE = 0xB3  # 179

# Win32 WM_APPCOMMAND constants
WM_APPCOMMAND = 0x0319
APPCOMMAND_VOLUME_MUTE = 8
APPCOMMAND_VOLUME_DOWN = 9
APPCOMMAND_VOLUME_UP = 10
APPCOMMAND_MEDIA_NEXTTRACK = 11
APPCOMMAND_MEDIA_PREVIOUSTRACK = 12
APPCOMMAND_MEDIA_STOP = 13
APPCOMMAND_MEDIA_PLAY_PAUSE = 14

KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002

VALID_WINDOW_ACTIONS = (
    "list",
    "inspect_layout",
    "layout",
    "focus",
    "snap_left",
    "snap_right",
    "maximize",
    "minimize",
    "restore",
    "close",
)

VALID_CONTROLS = ("volume", "brightness", "media", "theme")

VALID_VOLUME_ACTIONS = ("get", "set", "up", "down", "mute", "unmute", "toggle_mute")
VALID_BRIGHTNESS_ACTIONS = ("get", "set", "up", "down")
VALID_MEDIA_ACTIONS = ("play_pause", "play", "pause", "next", "previous", "prev", "stop")
VALID_THEME_ACTIONS = ("dark", "light", "toggle")

VALID_DIAGNOSTIC_CHECKS = ("overview", "battery", "network", "top_processes", "port")


RECT = _RECT
MONITORINFO = _MONITORINFOEXW


class SYSTEM_POWER_STATUS(ctypes.Structure):
    _fields_ = [
        ("ACLineStatus", ctypes.c_ubyte),
        ("BatteryFlag", ctypes.c_ubyte),
        ("BatteryLifePercent", ctypes.c_ubyte),
        ("SystemStatusFlag", ctypes.c_ubyte),
        ("BatteryLifeTime", ctypes.c_ulong),
        ("BatteryFullLifeTime", ctypes.c_ulong),
    ]


class MEMORYSTATUSEX(ctypes.Structure):
    _fields_ = [
        ("dwLength", ctypes.c_ulong),
        ("dwMemoryLoad", ctypes.c_ulong),
        ("ullTotalPhys", ctypes.c_ulonglong),
        ("ullAvailPhys", ctypes.c_ulonglong),
        ("ullTotalPageFile", ctypes.c_ulonglong),
        ("ullAvailPageFile", ctypes.c_ulonglong),
        ("ullTotalVirtual", ctypes.c_ulonglong),
        ("ullAvailVirtual", ctypes.c_ulonglong),
        ("ullAvailExtendedVirtual", ctypes.c_ulonglong),
    ]


_SYSTEM_CONTROLS_USER32: Any = None
_SYSTEM_CONTROLS_KERNEL32: Any = None


def _configure_system_controls_user32(u32: Any) -> Any:
    """Attach explicit 64-bit Win32 ctypes signatures (c_void_p handles) to user32."""
    if u32 is None or not hasattr(ctypes, "WINFUNCTYPE"):
        return u32
    wndenumproc = ctypes.WINFUNCTYPE(wintypes.BOOL, ctypes.c_void_p, wintypes.LPARAM)
    if hasattr(u32, "GetForegroundWindow"):
        u32.GetForegroundWindow.argtypes = []
        u32.GetForegroundWindow.restype = ctypes.c_void_p
    if hasattr(u32, "GetDesktopWindow"):
        u32.GetDesktopWindow.argtypes = []
        u32.GetDesktopWindow.restype = ctypes.c_void_p
    if hasattr(u32, "IsWindow"):
        u32.IsWindow.argtypes = [ctypes.c_void_p]
        u32.IsWindow.restype = wintypes.BOOL
    if hasattr(u32, "IsWindowVisible"):
        u32.IsWindowVisible.argtypes = [ctypes.c_void_p]
        u32.IsWindowVisible.restype = wintypes.BOOL
    if hasattr(u32, "IsIconic"):
        u32.IsIconic.argtypes = [ctypes.c_void_p]
        u32.IsIconic.restype = wintypes.BOOL
    if hasattr(u32, "IsZoomed"):
        u32.IsZoomed.argtypes = [ctypes.c_void_p]
        u32.IsZoomed.restype = wintypes.BOOL
    if hasattr(u32, "GetAncestor"):
        u32.GetAncestor.argtypes = [ctypes.c_void_p, wintypes.UINT]
        u32.GetAncestor.restype = ctypes.c_void_p
    if hasattr(u32, "IsChild"):
        u32.IsChild.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        u32.IsChild.restype = wintypes.BOOL
    if hasattr(u32, "GetWindowThreadProcessId"):
        u32.GetWindowThreadProcessId.argtypes = [
            ctypes.c_void_p,
            ctypes.POINTER(wintypes.DWORD),
        ]
        u32.GetWindowThreadProcessId.restype = wintypes.DWORD
    if hasattr(u32, "BringWindowToTop"):
        u32.BringWindowToTop.argtypes = [ctypes.c_void_p]
        u32.BringWindowToTop.restype = wintypes.BOOL
    if hasattr(u32, "AttachThreadInput"):
        u32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        u32.AttachThreadInput.restype = wintypes.BOOL
    if hasattr(u32, "GetWindowRect"):
        u32.GetWindowRect.argtypes = [ctypes.c_void_p, ctypes.POINTER(_RECT)]
        u32.GetWindowRect.restype = wintypes.BOOL
    if hasattr(u32, "GetWindowTextLengthW"):
        u32.GetWindowTextLengthW.argtypes = [ctypes.c_void_p]
        u32.GetWindowTextLengthW.restype = ctypes.c_int
    if hasattr(u32, "GetWindowTextW"):
        u32.GetWindowTextW.argtypes = [ctypes.c_void_p, wintypes.LPWSTR, ctypes.c_int]
        u32.GetWindowTextW.restype = ctypes.c_int
    if hasattr(u32, "EnumWindows"):
        u32.EnumWindows.argtypes = [wndenumproc, wintypes.LPARAM]
        u32.EnumWindows.restype = wintypes.BOOL
    if hasattr(u32, "OpenDesktopW"):
        u32.OpenDesktopW.argtypes = [
            wintypes.LPCWSTR,
            wintypes.DWORD,
            wintypes.BOOL,
            wintypes.DWORD,
        ]
        u32.OpenDesktopW.restype = ctypes.c_void_p
    if hasattr(u32, "EnumDesktopWindows"):
        u32.EnumDesktopWindows.argtypes = [ctypes.c_void_p, wndenumproc, wintypes.LPARAM]
        u32.EnumDesktopWindows.restype = wintypes.BOOL
    if hasattr(u32, "CloseDesktop"):
        u32.CloseDesktop.argtypes = [ctypes.c_void_p]
        u32.CloseDesktop.restype = wintypes.BOOL
    if hasattr(u32, "MonitorFromWindow"):
        u32.MonitorFromWindow.argtypes = [ctypes.c_void_p, wintypes.DWORD]
        u32.MonitorFromWindow.restype = ctypes.c_void_p
    if hasattr(u32, "GetMonitorInfoW"):
        u32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        u32.GetMonitorInfoW.restype = wintypes.BOOL
    if hasattr(u32, "ShowWindow"):
        u32.ShowWindow.argtypes = [ctypes.c_void_p, ctypes.c_int]
        u32.ShowWindow.restype = wintypes.BOOL
    if hasattr(u32, "SetForegroundWindow"):
        u32.SetForegroundWindow.argtypes = [ctypes.c_void_p]
        u32.SetForegroundWindow.restype = wintypes.BOOL
    if hasattr(u32, "MoveWindow"):
        u32.MoveWindow.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.BOOL,
        ]
        u32.MoveWindow.restype = wintypes.BOOL
    if hasattr(u32, "PostMessageW"):
        u32.PostMessageW.argtypes = [
            ctypes.c_void_p,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        u32.PostMessageW.restype = wintypes.BOOL
    if hasattr(u32, "SendMessageW"):
        u32.SendMessageW.argtypes = [
            ctypes.c_void_p,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        u32.SendMessageW.restype = wintypes.LPARAM
    if hasattr(u32, "DefWindowProcW"):
        u32.DefWindowProcW.argtypes = [
            ctypes.c_void_p,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        u32.DefWindowProcW.restype = wintypes.LPARAM
    if hasattr(u32, "keybd_event"):
        u32.keybd_event.argtypes = [
            ctypes.c_ubyte,
            ctypes.c_ubyte,
            wintypes.DWORD,
            ctypes.c_size_t,
        ]
        u32.keybd_event.restype = None
    return u32


def _configure_system_controls_kernel32(k32: Any) -> Any:
    """Attach explicit Win32 ctypes signatures to kernel32."""
    if k32 is None:
        return None
    if hasattr(k32, "GetCurrentThreadId"):
        k32.GetCurrentThreadId.argtypes = []
        k32.GetCurrentThreadId.restype = wintypes.DWORD
    if hasattr(k32, "GetSystemPowerStatus"):
        k32.GetSystemPowerStatus.argtypes = [ctypes.c_void_p]
        k32.GetSystemPowerStatus.restype = wintypes.BOOL
    if hasattr(k32, "GlobalMemoryStatusEx"):
        k32.GlobalMemoryStatusEx.argtypes = [ctypes.c_void_p]
        k32.GlobalMemoryStatusEx.restype = wintypes.BOOL
    return k32


def _enable_dpi_awareness_sc(user32: Any) -> None:
    if sys.platform != "win32" or not isinstance(user32, ctypes.CDLL):
        return
    with contextlib.suppress(Exception):
        if hasattr(user32, "SetProcessDpiAwarenessContext"):
            user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            user32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
            if user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4)):
                return
    with contextlib.suppress(Exception):
        shcore = ctypes.WinDLL("shcore", use_last_error=True)
        if hasattr(shcore, "SetProcessDpiAwareness"):
            shcore.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
            shcore.SetProcessDpiAwareness.restype = ctypes.c_long
            shcore.SetProcessDpiAwareness(2)


def _get_user32(user32: Any = None) -> Any:
    global _SYSTEM_CONTROLS_USER32
    if user32 is not None:
        return user32
    if _SYSTEM_CONTROLS_USER32 is not None:
        return _SYSTEM_CONTROLS_USER32
    if sys.platform == "win32" and hasattr(ctypes, "WinDLL"):
        loaded = _configure_system_controls_user32(
            ctypes.WinDLL("user32", use_last_error=True)
        )
        _enable_dpi_awareness_sc(loaded)
        _SYSTEM_CONTROLS_USER32 = loaded
        return _SYSTEM_CONTROLS_USER32
    return None


def _get_kernel32(kernel32: Any = None) -> Any:
    global _SYSTEM_CONTROLS_KERNEL32
    if kernel32 is not None:
        return kernel32
    if _SYSTEM_CONTROLS_KERNEL32 is not None:
        return _SYSTEM_CONTROLS_KERNEL32
    if sys.platform == "win32" and hasattr(ctypes, "WinDLL"):
        _SYSTEM_CONTROLS_KERNEL32 = _configure_system_controls_kernel32(
            ctypes.WinDLL("kernel32", use_last_error=True)
        )
        return _SYSTEM_CONTROLS_KERNEL32
    return None


def _resolve_psutil(psutil_mod: Any = None) -> Any:
    """Resolve psutil module if provided or installed in python environment."""
    if psutil_mod is not None:
        return psutil_mod
    try:
        import psutil

        return psutil
    except ImportError:
        return None


# ---------------------------------------------------------------------------
# Tool 1: manage_window
# ---------------------------------------------------------------------------


def _get_window_title_ctypes(hwnd: int, user32: Any) -> str:
    if not hasattr(user32, "GetWindowTextLengthW"):
        return ""
    try:
        length = int(user32.GetWindowTextLengthW(hwnd))
    except Exception:
        return ""
    if length <= 0:
        return ""
    buf = ctypes.create_unicode_buffer(length + 1)
    ret = user32.GetWindowTextW(hwnd, buf, length + 1)
    if isinstance(ret, str):
        return ret
    return buf.value


def _is_window_cloaked_sc(hwnd: int, user32: Any) -> bool:
    if sys.platform != "win32" or not isinstance(user32, ctypes.CDLL):
        return False
    try:
        dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        if not hasattr(dwmapi, "DwmGetWindowAttribute"):
            return False
        dwmapi.DwmGetWindowAttribute.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
        cloaked = ctypes.c_uint32(0)
        hr = dwmapi.DwmGetWindowAttribute(
            ctypes.c_void_p(hwnd),
            14,  # DWMWA_CLOAKED
            ctypes.byref(cloaked),
            ctypes.sizeof(cloaked),
        )
        return hr == 0 and bool(cloaked.value)
    except Exception:
        return False


def _enumerate_windows_ctypes(user32: Any) -> list[dict[str, Any]]:
    windows: list[dict[str, Any]] = []
    seen_hwnds: set[int] = set()
    fg_hwnd = 0
    if hasattr(user32, "GetForegroundWindow"):
        try:
            fg_val = user32.GetForegroundWindow()
            fg_hwnd = int(fg_val) if fg_val else 0
        except Exception:
            fg_hwnd = 0

    def _on_window(hwnd: int, _lparam: Any) -> bool:
        try:
            h = int(hwnd)
            if h in seen_hwnds:
                return True
            if hasattr(user32, "IsWindowVisible") and not bool(user32.IsWindowVisible(h)):
                return True
            cloaked = _is_window_cloaked_sc(h, user32)
            if cloaked:
                is_iconic = bool(hasattr(user32, "IsIconic") and user32.IsIconic(h))
                if not is_iconic:
                    return True
            title = _get_window_title_ctypes(h, user32).strip()
            if not title:
                return True
            seen_hwnds.add(h)
            windows.append(
                {
                    "hwnd": h,
                    "title": title,
                    "is_foreground": bool(h == fg_hwnd),
                }
            )
        except Exception:
            pass
        return True

    wndenumproc = None
    if hasattr(user32, "EnumWindows"):
        wndenumproc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        cb = wndenumproc(_on_window)
        try:
            user32.EnumWindows(cb, 0)
        except TypeError:
            # Handles mocks expecting python callables directly
            user32.EnumWindows(_on_window, 0)

    # If EnumWindows returned no windows (e.g. background worker or virtual desktop session),
    # fall back to EnumDesktopWindows on the interactive "default" desktop if available
    if not windows and hasattr(user32, "OpenDesktopW") and hasattr(user32, "EnumDesktopWindows"):
        try:
            DESKTOP_ENUMERATE = 0x0040
            DESKTOP_READOBJECTS = 0x0001
            hdesk = user32.OpenDesktopW(
                "default", 0, False, DESKTOP_ENUMERATE | DESKTOP_READOBJECTS
            )
            if hdesk:
                try:
                    if wndenumproc is not None:
                        cb_desk = wndenumproc(_on_window)
                        user32.EnumDesktopWindows(hdesk, cb_desk, 0)
                    else:
                        user32.EnumDesktopWindows(hdesk, _on_window, 0)
                except TypeError:
                    user32.EnumDesktopWindows(hdesk, _on_window, 0)
                finally:
                    if hasattr(user32, "CloseDesktop"):
                        user32.CloseDesktop(hdesk)
        except Exception:
            pass

    return windows


_WINDOW_TITLE_ALIASES: dict[str, tuple[str, ...]] = {
    "calc": ("calculator",),
    "calc.exe": ("calculator",),
    "calculator": ("calculator",),
    "calculator.exe": ("calculator",),
    "vscode": ("visual studio code", "code"),
    "vs code": ("visual studio code", "code"),
    "code": ("visual studio code",),
    "wt": ("terminal", "windows terminal", "powershell", "command prompt"),
    "terminal": ("windows terminal", "powershell", "command prompt"),
    "windows terminal": ("terminal", "windows terminal", "powershell", "command prompt"),
    "edge": ("microsoft edge", "edge"),
    "msedge": ("microsoft edge", "edge"),
    "msedge.exe": ("microsoft edge", "edge"),
    "chrome": ("google chrome", "chrome"),
    "google chrome": ("google chrome", "chrome"),
    "explorer": ("file explorer", "explorer"),
    "file explorer": ("file explorer", "explorer"),
    "notepad.exe": ("notepad",),
    "cursor": ("cursor",),
    "antigravity": ("antigravity",),
    "spotify": ("spotify",),
}


def _resolve_root_hwnd_sc(hwnd: int, user32: Any) -> int:
    if not hwnd or not isinstance(user32, ctypes.CDLL) or not hasattr(user32, "GetAncestor"):
        return int(hwnd)
    with contextlib.suppress(Exception):
        root = user32.GetAncestor(int(hwnd), GA_ROOT)
        if root and (not hasattr(user32, "IsWindowVisible") or bool(user32.IsWindowVisible(root))):
            return int(root)
    return int(hwnd)


def _resolve_target_window(
    user32: Any,
    title_query: str | int | None = None,
) -> tuple[int, str]:
    """Find matching window by title_query, HWND, or use active foreground window."""
    if title_query is not None and not isinstance(title_query, str):
        if isinstance(title_query, int) and not isinstance(title_query, bool):
            title_query = str(title_query)
        else:
            raise ValueError("title_query must be a string or integer HWND.")

    def _rank_key(item: dict[str, Any]) -> tuple[int, int, int]:
        return (
            0 if item.get("is_foreground") else 1,
            1 if _is_window_cloaked_sc(item["hwnd"], user32) else 0,
            len(item["title"]),
        )

    if title_query and title_query.strip():
        q = title_query.strip()
        q_lower = q.lower()
        windows = _enumerate_windows_ctypes(user32)

        # 1. Match by numeric HWND if q is digit
        if q.isdigit():
            target_hwnd = int(q)
            for w in windows:
                if w["hwnd"] == target_hwnd:
                    resolved = _resolve_root_hwnd_sc(w["hwnd"], user32)
                    return resolved, _get_window_title_ctypes(resolved, user32) or w["title"]

        # 2. Exact match (case-insensitive)
        exact = [w for w in windows if w["title"].strip().lower() == q_lower]
        if exact:
            exact.sort(key=_rank_key)
            chosen = exact[0]
            resolved = _resolve_root_hwnd_sc(chosen["hwnd"], user32)
            return resolved, _get_window_title_ctypes(resolved, user32) or chosen["title"]

        # 3. Substring match (case-insensitive, prioritize foreground/uncloaked then shortest title)
        matches = [w for w in windows if q_lower in w["title"].lower()]
        if not matches:
            for alias_term in _WINDOW_TITLE_ALIASES.get(q_lower, ()):
                matches = [w for w in windows if alias_term in w["title"].lower()]
                if matches:
                    break
        if matches:
            matches.sort(key=_rank_key)
            chosen = matches[0]
            resolved = _resolve_root_hwnd_sc(chosen["hwnd"], user32)
            return resolved, _get_window_title_ctypes(resolved, user32) or chosen["title"]
        raise ValueError(f"No visible window found matching title_query={title_query!r}.")

    if hasattr(user32, "GetForegroundWindow"):
        fg = user32.GetForegroundWindow()
        if fg:
            hwnd = _resolve_root_hwnd_sc(int(fg), user32)
            title = _get_window_title_ctypes(hwnd, user32)
            return hwnd, title
    raise RuntimeError("No active foreground window found.")


def _get_monitor_work_area(hwnd: int, user32: Any) -> tuple[int, int, int, int]:
    """Return (left, top, right, bottom) of the window's monitor work area."""
    h_monitor = 0
    if hasattr(user32, "MonitorFromWindow"):
        h_monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)

    mi = MONITORINFO()
    mi.cbSize = ctypes.sizeof(MONITORINFO)
    if hasattr(user32, "GetMonitorInfoW"):
        user32.GetMonitorInfoW(h_monitor, ctypes.byref(mi))

    left = int(mi.rcWork.left)
    top = int(mi.rcWork.top)
    right = int(mi.rcWork.right)
    bottom = int(mi.rcWork.bottom)

    # Fallback to sensible defaults if monitor info query returned 0 width/height
    if right <= left:
        right = left + 1920
    if bottom <= top:
        bottom = top + 1080

    return left, top, right, bottom


def _is_same_window_tree_sc(u32: Any, expected_hwnd: int, actual_hwnd: int) -> bool:
    if not expected_hwnd or not actual_hwnd:
        return False
    if int(expected_hwnd) == int(actual_hwnd):
        return True
    if hasattr(u32, "IsChild"):
        with contextlib.suppress(Exception):
            if bool(u32.IsChild(expected_hwnd, actual_hwnd)) or bool(
                u32.IsChild(actual_hwnd, expected_hwnd)
            ):
                return True
    if hasattr(u32, "GetAncestor"):
        with contextlib.suppress(Exception):
            exp_root = u32.GetAncestor(expected_hwnd, GA_ROOT)
            act_root = u32.GetAncestor(actual_hwnd, GA_ROOT)
            if exp_root and act_root and int(exp_root) == int(act_root):
                return True
    return False


def _focus_window_hwnd_sc(
    u32: Any,
    hwnd: int,
    *,
    kernel32: Any = None,
    restore_if_iconic: bool = True,
    attach_fallback: bool | None = None,
) -> None:
    """Bring *hwnd* to foreground cleanly, using cur_tid<->fg_tid AttachThreadInput on Win32."""
    use_fallback = (
        isinstance(u32, ctypes.CDLL) if attach_fallback is None else bool(attach_fallback)
    )
    if not use_fallback:
        if restore_if_iconic and hasattr(u32, "ShowWindow"):
            u32.ShowWindow(hwnd, SW_RESTORE)
        if hasattr(u32, "SetForegroundWindow"):
            u32.SetForegroundWindow(hwnd)
        return

    if restore_if_iconic and hasattr(u32, "IsIconic"):
        with contextlib.suppress(Exception):
            if bool(u32.IsIconic(hwnd)) and hasattr(u32, "ShowWindow"):
                u32.ShowWindow(hwnd, SW_RESTORE)
    fg_ok = bool(u32.SetForegroundWindow(hwnd)) if hasattr(u32, "SetForegroundWindow") else False
    with contextlib.suppress(Exception):
        fg_after = u32.GetForegroundWindow() if hasattr(u32, "GetForegroundWindow") else 0
        if fg_ok and fg_after and _is_same_window_tree_sc(u32, hwnd, int(fg_after)):
            return
        k32 = _get_kernel32(kernel32)
        cur_tid = (
            int(k32.GetCurrentThreadId() or 0)
            if (k32 is not None and hasattr(k32, "GetCurrentThreadId"))
            else 0
        )
        fg_tid = (
            int(u32.GetWindowThreadProcessId(fg_after, None) or 0)
            if (fg_after and hasattr(u32, "GetWindowThreadProcessId"))
            else 0
        )
        target_tid = (
            int(u32.GetWindowThreadProcessId(hwnd, None) or 0)
            if hasattr(u32, "GetWindowThreadProcessId")
            else 0
        )
        attached_fg = False
        attached_target = False
        if cur_tid and hasattr(u32, "AttachThreadInput"):
            if fg_tid and fg_tid != cur_tid:
                attached_fg = bool(u32.AttachThreadInput(cur_tid, fg_tid, True))
            if target_tid and target_tid not in (cur_tid, fg_tid):
                attached_target = bool(u32.AttachThreadInput(cur_tid, target_tid, True))
        try:
            if hasattr(u32, "BringWindowToTop"):
                u32.BringWindowToTop(hwnd)
            if hasattr(u32, "ShowWindow"):
                u32.ShowWindow(hwnd, SW_SHOW)
            if hasattr(u32, "SetForegroundWindow"):
                u32.SetForegroundWindow(hwnd)
        finally:
            if attached_target:
                with contextlib.suppress(Exception):
                    u32.AttachThreadInput(cur_tid, target_tid, False)
            if attached_fg:
                with contextlib.suppress(Exception):
                    u32.AttachThreadInput(cur_tid, fg_tid, False)


def _get_dwm_frame_insets_sc(hwnd: int, u32: Any) -> tuple[int, int, int, int]:
    """Return invisible DWM resize-border insets (left, top, right, bottom)."""
    if sys.platform != "win32" or not isinstance(u32, ctypes.CDLL):
        return (0, 0, 0, 0)
    if not hasattr(u32, "GetWindowRect"):
        return (0, 0, 0, 0)
    if hasattr(u32, "IsIconic") and bool(u32.IsIconic(hwnd)):
        return (0, 0, 0, 0)
    if hasattr(u32, "IsZoomed") and bool(u32.IsZoomed(hwnd)):
        return (0, 0, 0, 0)
    with contextlib.suppress(Exception):
        dwmapi = ctypes.WinDLL("dwmapi", use_last_error=True)
        if not hasattr(dwmapi, "DwmGetWindowAttribute"):
            return (0, 0, 0, 0)
        dwmapi.DwmGetWindowAttribute.argtypes = [
            ctypes.c_void_p,
            ctypes.c_uint32,
            ctypes.c_void_p,
            ctypes.c_uint32,
        ]
        dwmapi.DwmGetWindowAttribute.restype = ctypes.c_long
        win_rect = RECT()
        frame_rect = RECT()
        if not u32.GetWindowRect(hwnd, ctypes.byref(win_rect)):
            return (0, 0, 0, 0)
        hr = dwmapi.DwmGetWindowAttribute(
            ctypes.c_void_p(hwnd),
            DWMWA_EXTENDED_FRAME_BOUNDS,
            ctypes.byref(frame_rect),
            ctypes.sizeof(frame_rect),
        )
        if hr != 0:
            return (0, 0, 0, 0)
        inset_left = max(0, min(32, int(frame_rect.left) - int(win_rect.left)))
        inset_top = max(0, min(32, int(frame_rect.top) - int(win_rect.top)))
        inset_right = max(0, min(32, int(win_rect.right) - int(frame_rect.right)))
        inset_bottom = max(0, min(32, int(win_rect.bottom) - int(frame_rect.bottom)))
        return (inset_left, inset_top, inset_right, inset_bottom)
    return (0, 0, 0, 0)


def _move_window_snap_sc(
    u32: Any,
    hwnd: int,
    x: int,
    y: int,
    w: int,
    h: int,
    *,
    snap_restore_guard: PreconditionGuard | None = None,
    work_area: tuple[int, int, int, int] | None = None,
) -> None:
    """Restore/unmaximize as needed and MoveWindow compensating for invisible DWM borders."""
    is_live = isinstance(u32, ctypes.CDLL)
    if is_live:
        if hasattr(u32, "IsIconic") and bool(u32.IsIconic(hwnd)) and hasattr(u32, "ShowWindow"):
            u32.ShowWindow(hwnd, SW_RESTORE)
        if hasattr(u32, "IsZoomed") and bool(u32.IsZoomed(hwnd)) and hasattr(u32, "ShowWindow"):
            u32.ShowWindow(hwnd, SW_SHOWNORMAL)
    elif hasattr(u32, "ShowWindow"):
        u32.ShowWindow(hwnd, SW_RESTORE)

    if snap_restore_guard is not None:
        snap_restore_guard.assert_ready("snap_move_window")

    if hasattr(u32, "MoveWindow"):
        inset_l, inset_t, inset_r, inset_b = _get_dwm_frame_insets_sc(hwnd, u32)
        u32.MoveWindow(
            hwnd,
            x - inset_l,
            y - inset_t,
            w + inset_l + inset_r,
            h + inset_t + inset_b,
            True,
        )
        if not is_live:
            with contextlib.suppress(Exception):
                moved_map = getattr(u32, "_clea_last_moved_rects", None)
                if not isinstance(moved_map, dict):
                    moved_map = {}
                    u32._clea_last_moved_rects = moved_map
                moved_map[int(hwnd)] = (int(x), int(y), int(x + w), int(y + h))
                if work_area is not None:
                    wa_map = getattr(u32, "_clea_last_work_areas", None)
                    if not isinstance(wa_map, dict):
                        wa_map = {}
                        u32._clea_last_work_areas = wa_map
                    wa_map[int(hwnd)] = (
                        int(work_area[0]),
                        int(work_area[1]),
                        int(work_area[2]),
                        int(work_area[3]),
                    )


def _read_process_name_sc(hwnd: int, user32: Any, kernel32: Any = None) -> str:
    """Read process executable name for *hwnd* when available."""
    if user32 is None or not hwnd or not hasattr(user32, "GetWindowThreadProcessId"):
        return ""
    pid_val = 0
    with contextlib.suppress(Exception):
        pid = wintypes.DWORD(0)
        user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        pid_val = int(pid.value)
    if pid_val <= 0:
        return ""
    k32 = kernel32
    if k32 is None and isinstance(user32, ctypes.CDLL):
        k32 = _get_kernel32()
    if k32 is None or not hasattr(k32, "OpenProcess"):
        return ""
    h_proc = None
    try:
        h_proc = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid_val)
        if not h_proc:
            return ""
        if hasattr(k32, "QueryFullProcessImageNameW"):
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if k32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
                return Path(buf.value).name
        return ""
    except Exception:
        return ""
    finally:
        if h_proc and hasattr(k32, "CloseHandle"):
            with contextlib.suppress(Exception):
                k32.CloseHandle(h_proc)


def _get_extended_frame_bounds_sc(
    hwnd: int,
    user32: Any,
    dwmapi: Any = None,
    focus_backend: FocusPreconditionBackend | None = None,
) -> tuple[int, int, int, int] | None:
    """Return (left, top, right, bottom) visible DWM frame bounds for *hwnd*."""
    if focus_backend is not None:
        with contextlib.suppress(Exception):
            b = focus_backend.get_extended_frame_bounds(hwnd)
            if b is not None and len(b) == 4 and b[2] > b[0] and b[3] > b[1]:
                return (int(b[0]), int(b[1]), int(b[2]), int(b[3]))

    dwm = dwmapi
    if dwm is None and sys.platform == "win32" and isinstance(user32, ctypes.CDLL):
        with contextlib.suppress(Exception):
            dwm = ctypes.WinDLL("dwmapi", use_last_error=True)
            if hasattr(dwm, "DwmGetWindowAttribute"):
                dwm.DwmGetWindowAttribute.argtypes = [
                    ctypes.c_void_p,
                    ctypes.c_uint32,
                    ctypes.c_void_p,
                    ctypes.c_uint32,
                ]
                dwm.DwmGetWindowAttribute.restype = ctypes.c_long

    rect = RECT()
    if dwm is not None and hasattr(dwm, "DwmGetWindowAttribute"):
        with contextlib.suppress(Exception):
            hr = dwm.DwmGetWindowAttribute(
                ctypes.c_void_p(hwnd) if isinstance(dwm, ctypes.CDLL) else hwnd,
                DWMWA_EXTENDED_FRAME_BOUNDS,
                ctypes.byref(rect),
                ctypes.sizeof(rect),
            )
            if hr == 0 and rect.right > rect.left and rect.bottom > rect.top:
                return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))

    if user32 is not None and hasattr(user32, "GetWindowRect"):
        with contextlib.suppress(Exception):
            if (
                user32.GetWindowRect(hwnd, ctypes.byref(rect))
                and rect.right > rect.left
                and rect.bottom > rect.top
            ):
                return (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))

    if user32 is not None and not isinstance(user32, ctypes.CDLL):
        moved_rects = getattr(user32, "_clea_last_moved_rects", None)
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


def _detect_window_slot(
    bounds: tuple[int, int, int, int],
    work_area: tuple[int, int, int, int],
    *,
    is_maximized: bool,
    extends_behind_foreground: bool,
    tolerance_px: int = 24,
) -> tuple[float, str]:
    """Return (width_ratio, detected_slot) for a window relative to its monitor rcWork.

    detected_slot is one of: "left_half" | "right_half" | "maximized" | "full_width" | "custom".
    """
    left, top, right, bottom = bounds
    wl, wt, wr, wb = work_area
    width = max(0, right - left)
    height = max(0, bottom - top)
    work_w = max(1, wr - wl)
    work_h = max(1, wb - wt)
    half_w = work_w // 2
    width_ratio = round(width / float(work_w), 4)
    height_ratio = height / float(work_h)

    near_top_bottom = (
        (abs(top - wt) <= tolerance_px * 2 and abs(bottom - wb) <= tolerance_px * 2)
        or height_ratio >= 0.75
    )
    is_half_width = abs(width - half_w) <= tolerance_px * 2 or (0.42 <= width_ratio <= 0.58)
    if near_top_bottom and is_half_width:
        if abs(left - wl) <= tolerance_px * 2 and abs(right - (wl + half_w)) <= tolerance_px * 2:
            return width_ratio, "left_half"
        if abs(left - (wl + half_w)) <= tolerance_px * 2 and abs(right - wr) <= tolerance_px * 2:
            return width_ratio, "right_half"

    covers_full_width = width_ratio >= 0.85 or (
        abs(left - wl) <= tolerance_px * 2 and abs(right - wr) <= tolerance_px * 2
    )
    covers_full_work = covers_full_width and (
        (abs(top - wt) <= tolerance_px * 2 and abs(bottom - wb) <= tolerance_px * 2)
        or height_ratio >= 0.85
    )
    if extends_behind_foreground and covers_full_width:
        return width_ratio, "full_width"
    if is_maximized and covers_full_work:
        return width_ratio, "maximized"
    if covers_full_width:
        return width_ratio, "full_width"
    if is_maximized:
        return width_ratio, "maximized"
    return width_ratio, "custom"


def format_window_layout_summary(layout_or_windows: Any) -> str:
    """Format a concise `[OS Window Geometry (Z-order)]` ground-truth summary."""
    if isinstance(layout_or_windows, dict):
        windows = list(layout_or_windows.get("windows") or [])
    elif isinstance(layout_or_windows, list | tuple):
        windows = list(layout_or_windows)
    else:
        windows = []

    lines = ["[OS Window Geometry (Z-order)]"]
    if not windows:
        lines.append("No visible top-level windows detected.")
        return "\n".join(lines)

    for idx, win in enumerate(windows):
        if not isinstance(win, dict):
            continue
        z_idx = int(win.get("z_index", idx))
        title = str(win.get("title") or "")
        app_name = str(win.get("app_name") or title or "Unknown")
        hwnd = int(win.get("hwnd", 0))
        b = win.get("bounds") or {}
        if isinstance(b, dict):
            l_val = int(b.get("left", win.get("left", 0)))
            t_val = int(b.get("top", win.get("top", 0)))
            r_val = int(b.get("right", win.get("right", 0)))
            b_val = int(b.get("bottom", win.get("bottom", 0)))
            w_val = int(b.get("width", win.get("width", max(0, r_val - l_val))))
            h_val = int(b.get("height", win.get("height", max(0, b_val - t_val))))
        else:
            l_val, t_val, r_val, b_val = 0, 0, 0, 0
            w_val, h_val = 0, 0
        rc = win.get("rcWork") or win.get("rc_work") or {}
        if isinstance(rc, dict):
            wl = int(rc.get("left", 0))
            wt = int(rc.get("top", 0))
            wr = int(rc.get("right", 1920))
            wb = int(rc.get("bottom", 1040))
        else:
            wl, wt, wr, wb = 0, 0, 1920, 1040
        slot = str(win.get("detected_slot") or "custom")
        ratio = float(win.get("width_ratio", 0.0))
        is_max = bool(win.get("is_maximized", False))
        behind = bool(win.get("extends_behind_foreground", False))
        fg_tag = " (foreground)" if z_idx == 0 else ""
        warn_tag = (
            " [WARNING: occluded behind foreground window — NOT a side-by-side split]"
            if behind
            else ""
        )
        lines.append(
            f"z={z_idx}{fg_tag}: {title!r} (app={app_name}, hwnd={hwnd}) "
            f"bounds=({l_val},{t_val},{r_val},{b_val}) [{w_val}x{h_val}] "
            f"rcWork=({wl},{wt},{wr},{wb}) width_ratio={ratio:.2f} "
            f"detected_slot={slot} is_maximized={is_max} "
            f"extends_behind_foreground={behind}{warn_tag}"
        )
    return "\n".join(lines)


def _coerce_rect_tuple(raw: Any) -> tuple[int, int, int, int] | None:
    if raw is None:
        return None
    if isinstance(raw, tuple | list) and len(raw) == 4:
        l_v, t_v, r_v, b_v = int(raw[0]), int(raw[1]), int(raw[2]), int(raw[3])
        if r_v > l_v and b_v > t_v:
            return (l_v, t_v, r_v, b_v)
    if isinstance(raw, dict):
        if all(k in raw for k in ("left", "top", "right", "bottom")):
            l_v = int(raw["left"])
            t_v = int(raw["top"])
            r_v = int(raw["right"])
            b_v = int(raw["bottom"])
            if r_v > l_v and b_v > t_v:
                return (l_v, t_v, r_v, b_v)
        if all(k in raw for k in ("x", "y", "width", "height")):
            l_v = int(raw["x"])
            t_v = int(raw["y"])
            w_v = int(raw["width"])
            h_v = int(raw["height"])
            if w_v > 0 and h_v > 0:
                return (l_v, t_v, l_v + w_v, t_v + h_v)
    return None


def inspect_window_layout(
    *,
    user32: Any = None,
    dwmapi: Any = None,
    kernel32: Any = None,
    focus_backend: FocusPreconditionBackend | None = None,
    windows: list[Any] | None = None,
    tolerance_px: int = 24,
) -> dict[str, Any]:
    """Deterministically inspect top-level visible windows in Z-order (front-to-back).

    Returns each window's title, app_name, hwnd, z_index, DWMWA_EXTENDED_FRAME_BOUNDS,
    monitor rcWork, is_maximized, width_ratio, detected_slot, and extends_behind_foreground.
    """
    u32 = user32
    if u32 is None and focus_backend is None and windows is None:
        u32 = _get_user32()

    active_backend = focus_backend
    if active_backend is None and u32 is not None and hasattr(u32, "backend"):
        cand = getattr(u32, "backend", None)
        if isinstance(cand, FocusPreconditionBackend):
            active_backend = cand

    raw_candidates: list[dict[str, Any]] = []

    if windows is not None:
        for item in windows:
            if isinstance(item, dict):
                hwnd = int(item.get("hwnd", 0))
                title = str(item.get("title") or "").strip()
                proc = str(item.get("process_name") or "").strip()
                app = str(item.get("app_name") or "").strip()
                vis = bool(item.get("is_visible", item.get("valid", True)))
                minimized = bool(item.get("minimized", item.get("is_minimized", False)))
                is_max = bool(item.get("is_maximized", False))
                bounds = _coerce_rect_tuple(
                    item.get("bounds")
                    or item.get("extended_frame_bounds")
                    or (
                        (item["left"], item["top"], item["right"], item["bottom"])
                        if all(k in item for k in ("left", "top", "right", "bottom"))
                        else None
                    )
                )
                work = _coerce_rect_tuple(
                    item.get("rcWork")
                    or item.get("rc_work")
                    or item.get("work_area")
                    or item.get("monitor_bounds")
                )
            else:
                hwnd = int(getattr(item, "hwnd", 0) or 0)
                title = str(getattr(item, "title", "") or "").strip()
                proc = str(getattr(item, "process_name", "") or "").strip()
                app = str(getattr(item, "app_name", "") or "").strip()
                vis = bool(getattr(item, "is_visible", getattr(item, "valid", True)))
                minimized = bool(
                    getattr(item, "minimized", getattr(item, "is_minimized", False))
                )
                is_max = bool(getattr(item, "is_maximized", False))
                bounds = _coerce_rect_tuple(getattr(item, "bounds", None))
                work = _coerce_rect_tuple(
                    getattr(item, "work_area", None)
                    or getattr(item, "rcWork", None)
                    or getattr(item, "monitor_bounds", None)
                )
            if not vis or minimized or not title or title.lower() == "program manager":
                continue
            if bounds is None and hwnd > 0:
                bounds = _get_extended_frame_bounds_sc(
                    hwnd, u32, dwmapi=dwmapi, focus_backend=active_backend
                )
            if bounds is None:
                continue
            if work is None:
                if active_backend is not None and hwnd > 0:
                    work = active_backend.get_monitor_work_area(hwnd)
                if work is None and u32 is not None and hwnd > 0:
                    work = _get_monitor_work_area(hwnd, u32)
                if work is None:
                    work = (0, 0, 1920, 1040)
            if not app or app == "Unknown application":
                resolved_app = resolve_app_display_name(proc, title)
                app = resolved_app if resolved_app != "Unknown application" else (title or proc)
            raw_candidates.append(
                {
                    "hwnd": hwnd,
                    "title": title,
                    "process_name": proc,
                    "app_name": app,
                    "bounds": bounds,
                    "work_area": work,
                    "is_maximized": is_max,
                }
            )
    elif u32 is not None and hasattr(u32, "EnumWindows"):
        enum_list = _enumerate_windows_ctypes(u32)
        fg_hwnd = 0
        if hasattr(u32, "GetForegroundWindow"):
            with contextlib.suppress(Exception):
                fg_val = u32.GetForegroundWindow()
                if isinstance(fg_val, int) and not isinstance(fg_val, bool):
                    fg_hwnd = int(fg_val)
        if (
            fg_hwnd
            and not isinstance(u32, ctypes.CDLL)
            and any(w["hwnd"] == fg_hwnd for w in enum_list[1:])
        ):
            enum_list.sort(key=lambda w: 0 if w["hwnd"] == fg_hwnd else 1)

        for item in enum_list:
            h = int(item["hwnd"])
            title = str(item.get("title") or "").strip()
            if not title or title.lower() == "program manager":
                continue
            is_min = False
            if active_backend is not None:
                is_min = active_backend.is_minimized(h)
            elif hasattr(u32, "IsIconic"):
                with contextlib.suppress(Exception):
                    ic = u32.IsIconic(h)
                    if isinstance(ic, int | bool):
                        is_min = bool(ic)
            if is_min:
                continue

            bounds = _get_extended_frame_bounds_sc(
                h, u32, dwmapi=dwmapi, focus_backend=active_backend
            )
            if bounds is None:
                continue
            work = (
                active_backend.get_monitor_work_area(h)
                if active_backend is not None
                else None
            )
            if work is None:
                work = _get_monitor_work_area(h, u32)

            is_max = False
            if hasattr(u32, "IsZoomed"):
                with contextlib.suppress(Exception):
                    zm = u32.IsZoomed(h)
                    if isinstance(zm, int | bool):
                        is_max = bool(zm)

            proc = ""
            if active_backend is not None:
                with contextlib.suppress(Exception):
                    ctx = active_backend.get_window_context(h)
                    if ctx is not None and ctx.process_name:
                        proc = ctx.process_name
            if not proc:
                proc = _read_process_name_sc(h, u32, kernel32=kernel32)
            resolved_app = resolve_app_display_name(proc, title)
            app = resolved_app if resolved_app != "Unknown application" else title

            raw_candidates.append(
                {
                    "hwnd": h,
                    "title": title,
                    "process_name": proc,
                    "app_name": app,
                    "bounds": bounds,
                    "work_area": work,
                    "is_maximized": is_max,
                }
            )
    elif active_backend is not None and hasattr(active_backend, "windows"):
        backend_wins = getattr(active_backend, "windows", {})
        fg_ctx = active_backend.get_foreground_context()
        fg_hwnd = fg_ctx.hwnd if fg_ctx is not None else 0
        win_objs = (
            list(backend_wins.values())
            if isinstance(backend_wins, dict)
            else list(backend_wins)
        )
        if fg_hwnd:
            win_objs.sort(key=lambda w: 0 if getattr(w, "hwnd", 0) == fg_hwnd else 1)
        return inspect_window_layout(
            focus_backend=active_backend,
            windows=win_objs,
            tolerance_px=tolerance_px,
        )

    window_entries: list[dict[str, Any]] = []
    for z_idx, cand in enumerate(raw_candidates):
        left, top, right, bottom = cand["bounds"]
        width = max(0, right - left)
        height = max(0, bottom - top)
        wl, wt, wr, wb = cand["work_area"]
        work_w = max(1, wr - wl)
        work_h = max(1, wb - wt)
        is_max = bool(cand["is_maximized"])

        extends_behind_fg = False
        if z_idx > 0:
            for higher in raw_candidates[:z_idx]:
                hl, ht, hr, hb = higher["bounds"]
                ix_l = max(left, hl)
                ix_t = max(top, ht)
                ix_r = min(right, hr)
                ix_b = min(bottom, hb)
                if (ix_r - ix_l) > tolerance_px and (ix_b - ix_t) > tolerance_px:
                    extends_behind_fg = True
                    break

        width_ratio, detected_slot = _detect_window_slot(
            cand["bounds"],
            cand["work_area"],
            is_maximized=is_max,
            extends_behind_foreground=extends_behind_fg,
            tolerance_px=tolerance_px,
        )

        bounds_dict = {
            "left": left,
            "top": top,
            "right": right,
            "bottom": bottom,
            "width": width,
            "height": height,
        }
        work_dict = {
            "left": wl,
            "top": wt,
            "right": wr,
            "bottom": wb,
            "width": work_w,
            "height": work_h,
        }
        window_entries.append(
            {
                "hwnd": int(cand["hwnd"]),
                "title": cand["title"],
                "app_name": cand["app_name"],
                "process_name": cand["process_name"],
                "z_index": z_idx,
                "is_foreground": z_idx == 0,
                "left": left,
                "top": top,
                "right": right,
                "bottom": bottom,
                "width": width,
                "height": height,
                "bounds": bounds_dict,
                "extended_frame_bounds": dict(bounds_dict),
                "rcWork": work_dict,
                "rc_work": dict(work_dict),
                "is_maximized": is_max,
                "width_ratio": width_ratio,
                "detected_slot": detected_slot,
                "extends_behind_foreground": extends_behind_fg,
            }
        )

    summary_text = format_window_layout_summary(window_entries)
    top_two = window_entries[:2]
    has_true_split = (
        len(top_two) == 2
        and {top_two[0]["detected_slot"], top_two[1]["detected_slot"]}
        == {"left_half", "right_half"}
        and not any(w["extends_behind_foreground"] for w in top_two)
    )
    has_occluded_background = any(w["extends_behind_foreground"] for w in window_entries)

    return {
        "ok": True,
        "count": len(window_entries),
        "windows": window_entries,
        "is_true_split_screen": has_true_split,
        "has_occluded_background": has_occluded_background,
        "summary": summary_text,
        "os_window_geometry": summary_text,
    }


def manage_window(
    action: str,
    title_query: str | None = None,
    *,
    user32: Any = None,
    focus_backend: FocusPreconditionBackend | None = None,
    verifier: ActionVerifier | None = None,
    verify_timeout_s: float = 1.0,
) -> dict[str, Any]:
    """Manage application windows using Win32 ctypes."""
    if not isinstance(action, str):
        raise ValueError("action must be a string.")
    norm_action = action.strip().lower()
    if norm_action not in VALID_WINDOW_ACTIONS:
        actions_str = ", ".join(VALID_WINDOW_ACTIONS)
        raise ValueError(f"Unsupported window action {action!r}. Valid actions: {actions_str}.")

    u32 = _get_user32(user32)
    if u32 is None:
        raise OSError("Win32 user32 is not available on this platform.")

    if norm_action == "list":
        windows = _enumerate_windows_ctypes(u32)
        return {
            "ok": True,
            "action": "list",
            "windows": windows,
            "count": len(windows),
        }

    if norm_action in ("inspect_layout", "layout"):
        layout = inspect_window_layout(user32=u32, focus_backend=focus_backend)
        layout["action"] = norm_action
        return layout

    hwnd, title = _resolve_target_window(u32, title_query)
    inferred_focus_backend = focus_backend
    if inferred_focus_backend is None and hasattr(u32, "backend"):
        cand_backend = getattr(u32, "backend", None)
        if isinstance(cand_backend, FocusPreconditionBackend):
            inferred_focus_backend = cand_backend
    if inferred_focus_backend is None and norm_action in ("snap_left", "snap_right"):
        inferred_focus_backend = Win32FocusPreconditionBackend(user32=u32)

    should_verify = (
        verifier is not None
        or inferred_focus_backend is not None
        or user32 is None
        or norm_action in ("snap_left", "snap_right")
    )
    active_verifier = (
        verifier
        if verifier is not None
        else (
            ActionVerifier(focus_backend=inferred_focus_backend)
            if should_verify
            else None
        )
    )
    active_focus_backend = (
        inferred_focus_backend
        if inferred_focus_backend is not None
        else (active_verifier._get_focus_backend() if active_verifier is not None else None)
    )

    if norm_action == "focus":
        _focus_window_hwnd_sc(u32, hwnd, restore_if_iconic=True)
        result: dict[str, Any] = {
            "ok": True,
            "action": "focus",
            "hwnd": hwnd,
            "title": title,
        }
        if active_verifier is not None:
            outcome = active_verifier.verify_window_state(
                hwnd,
                expected_state="focused",
                timeout_s=verify_timeout_s,
            )
            result["verification"] = outcome.to_dict()
            result["verified"] = outcome.verified
        return result

    if norm_action in ("snap_left", "snap_right"):
        left, top, right, bottom = _get_monitor_work_area(hwnd, u32)
        work_width = right - left
        work_height = bottom - top
        half_width = work_width // 2

        if norm_action == "snap_left":
            x = left
            y = top
            w = half_width
            h = work_height
            snap_side = "left"
        else:
            x = left + half_width
            y = top
            w = work_width - half_width
            h = work_height
            snap_side = "right"

        if active_focus_backend is not None:
            try:
                with PreconditionGuard(
                    active_focus_backend,
                    expected_hwnd=hwnd,
                    require_foreground=False,
                    allow_minimized=True,
                ) as guard:
                    guard.assert_ready("snap_restore")
                    _move_window_snap_sc(
                        u32,
                        hwnd,
                        x,
                        y,
                        w,
                        h,
                        snap_restore_guard=guard,
                        work_area=(left, top, right, bottom),
                    )
                    guard.assert_ready("snap_focus")
                    _focus_window_hwnd_sc(u32, hwnd, restore_if_iconic=False)
            except PreconditionFailedError as exc:
                payload = exc.to_abort_payload()
                payload["action"] = norm_action
                payload["hwnd"] = hwnd
                payload["title"] = title
                return payload
        else:
            _move_window_snap_sc(u32, hwnd, x, y, w, h, work_area=(left, top, right, bottom))
            _focus_window_hwnd_sc(u32, hwnd, restore_if_iconic=False)

        result = {
            "ok": True,
            "action": norm_action,
            "hwnd": hwnd,
            "title": title,
            "rect": {"x": x, "y": y, "width": w, "height": h},
        }
        if active_verifier is not None:
            outcome = active_verifier.verify_window_state(
                hwnd,
                expected_snap=snap_side,
                timeout_s=verify_timeout_s,
            )
            if not outcome.verified and isinstance(u32, ctypes.CDLL):
                _move_window_snap_sc(u32, hwnd, x, y, w, h)
                outcome = active_verifier.verify_window_state(
                    hwnd,
                    expected_snap=snap_side,
                    timeout_s=verify_timeout_s,
                )
            result["verification"] = outcome.to_dict()
            result["verified"] = outcome.verified
        return result

    if norm_action == "maximize":
        if hasattr(u32, "ShowWindow"):
            u32.ShowWindow(hwnd, SW_MAXIMIZE)
        _focus_window_hwnd_sc(u32, hwnd, restore_if_iconic=False)
        result = {"ok": True, "action": "maximize", "hwnd": hwnd, "title": title}
        if active_verifier is not None:
            outcome = active_verifier.verify_window_state(
                hwnd,
                expected_state="maximized",
                timeout_s=verify_timeout_s,
            )
            result["verification"] = outcome.to_dict()
            result["verified"] = outcome.verified
        return result

    if norm_action == "minimize":
        if hasattr(u32, "ShowWindow"):
            u32.ShowWindow(hwnd, SW_MINIMIZE)
        result = {"ok": True, "action": "minimize", "hwnd": hwnd, "title": title}
        if active_verifier is not None:
            outcome = active_verifier.verify_window_state(
                hwnd,
                expected_state="minimized",
                timeout_s=verify_timeout_s,
            )
            result["verification"] = outcome.to_dict()
            result["verified"] = outcome.verified
        return result

    if norm_action == "restore":
        if hasattr(u32, "ShowWindow"):
            u32.ShowWindow(hwnd, SW_RESTORE)
        _focus_window_hwnd_sc(u32, hwnd, restore_if_iconic=False)
        result = {"ok": True, "action": "restore", "hwnd": hwnd, "title": title}
        if active_verifier is not None:
            outcome = active_verifier.verify_window_state(
                hwnd,
                expected_state="restored",
                timeout_s=verify_timeout_s,
            )
            result["verification"] = outcome.to_dict()
            result["verified"] = outcome.verified
        return result

    if norm_action == "close":
        if hasattr(u32, "PostMessageW"):
            u32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        elif hasattr(u32, "SendMessageW"):
            u32.SendMessageW(hwnd, WM_CLOSE, 0, 0)
        result = {"ok": True, "action": "close", "hwnd": hwnd, "title": title}
        if active_verifier is not None:
            outcome = active_verifier.verify_window_state(
                hwnd,
                expected_state="closed",
                timeout_s=verify_timeout_s,
            )
            result["verification"] = outcome.to_dict()
            result["verified"] = outcome.verified
        return result

    raise ValueError(f"Unhandled window action {action!r}.")


# ---------------------------------------------------------------------------
# Tool 2: system_control
# ---------------------------------------------------------------------------

_COREAUDIO_POWERSHELL_SCRIPT = r"""
param([string]$Action, [double]$Scalar = 0.5, [bool]$Mute = $false)
Add-Type -TypeDefinition @"
using System;
using System.Runtime.InteropServices;

[Guid("5CDF2C82-841E-4546-9722-0CF74078229A"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IAudioEndpointVolume {
    int RegisterControlChangeNotify(IntPtr pNotify);
    int UnregisterControlChangeNotify(IntPtr pNotify);
    int GetChannelCount(out uint pnChannelCount);
    int SetMasterVolumeLevel(float fLevelDB, Guid pguidEventContext);
    int SetMasterVolumeLevelScalar(float fLevel, Guid pguidEventContext);
    int GetMasterVolumeLevel(out float pfLevelDB);
    int GetMasterVolumeLevelScalar(out float pfLevel);
    int SetChannelVolumeLevel(uint nChannel, float fLevelDB, Guid pguidEventContext);
    int SetChannelVolumeLevelScalar(uint nChannel, float fLevel, Guid pguidEventContext);
    int GetChannelVolumeLevel(uint nChannel, out float pfLevelDB);
    int GetChannelVolumeLevelScalar(uint nChannel, out float pfLevel);
    int SetMute([MarshalAs(UnmanagedType.Bool)] bool bMute, Guid pguidEventContext);
    int GetMute([MarshalAs(UnmanagedType.Bool)] out bool pbMute);
}

[Guid("D666063F-1587-4E43-81F1-B948E807363F"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDevice {
    int Activate(ref Guid iid, int dwClsCtx, IntPtr pActivationParams,
                 [MarshalAs(UnmanagedType.IUnknown)] out object ppInterface);
}

[Guid("A95664D2-9614-4F35-A746-DE8DB63617E6"), InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
interface IMMDeviceEnumerator {
    int EnumAudioEndpoints(int dataFlow, int dwStateMask, out IntPtr ppDevices);
    int GetDefaultAudioEndpoint(int dataFlow, int role, out IMMDevice ppEndpoint);
}

[ComImport, Guid("BCDE0395-E52F-467C-8E3D-C4579291692E")]
class MMDeviceEnumeratorComObject {}

public static class CleaAudioMasterHelper {
    private static IAudioEndpointVolume GetEndpoint() {
        var enumerator = (IMMDeviceEnumerator)(new MMDeviceEnumeratorComObject());
        IMMDevice device;
        Marshal.ThrowExceptionForHR(enumerator.GetDefaultAudioEndpoint(0, 1, out device));
        var iid = typeof(IAudioEndpointVolume).GUID;
        object endpointObj;
        Marshal.ThrowExceptionForHR(device.Activate(ref iid, 23, IntPtr.Zero, out endpointObj));
        return (IAudioEndpointVolume)endpointObj;
    }
    public static float GetScalar() {
        var ep = GetEndpoint();
        float val = 0;
        Marshal.ThrowExceptionForHR(ep.GetMasterVolumeLevelScalar(out val));
        return val;
    }
    public static void SetScalar(float scalar) {
        var ep = GetEndpoint();
        Marshal.ThrowExceptionForHR(ep.SetMasterVolumeLevelScalar(scalar, Guid.Empty));
    }
    public static bool GetMute() {
        var ep = GetEndpoint();
        bool isMuted = false;
        Marshal.ThrowExceptionForHR(ep.GetMute(out isMuted));
        return isMuted;
    }
    public static void SetMute(bool mute) {
        var ep = GetEndpoint();
        Marshal.ThrowExceptionForHR(ep.SetMute(mute, Guid.Empty));
    }
}
"@

if ($Action -eq "get") {
    $v = [CleaAudioMasterHelper]::GetScalar()
    $m = [CleaAudioMasterHelper]::GetMute()
    Write-Output "$([math]::Round($v * 100)) $m"
} elseif ($Action -eq "set") {
    [CleaAudioMasterHelper]::SetScalar([float]$Scalar)
} elseif ($Action -eq "mute") {
    [CleaAudioMasterHelper]::SetMute($true)
} elseif ($Action -eq "unmute") {
    [CleaAudioMasterHelper]::SetMute($false)
} elseif ($Action -eq "toggle_mute") {
    $m = [CleaAudioMasterHelper]::GetMute()
    [CleaAudioMasterHelper]::SetMute(-not $m)
    Write-Output "$(-not $m)"
}
"""


def _run_coreaudio_powershell(
    action: str,
    scalar: float = 0.5,
    powershell_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> tuple[int | None, bool | None]:
    """Run CoreAudio helper via PowerShell."""
    runner = powershell_runner or subprocess.run
    ps_exe = shutil.which("powershell.exe") or "powershell.exe"
    proc = runner(
        [
            ps_exe,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"& {{ {_COREAUDIO_POWERSHELL_SCRIPT} }} -Action {action} -Scalar {scalar:.4f}",
        ],
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=8.0,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"PowerShell CoreAudio failed: {detail or f'exit {proc.returncode}'}")

    out = (proc.stdout or "").strip()
    if action == "get" and out:
        parts = out.split()
        vol = int(parts[0]) if len(parts) > 0 else 50
        mute = parts[1].lower() == "true" if len(parts) > 1 else False
        return vol, mute
    if action == "toggle_mute" and out:
        mute = out.split()[0].lower() == "true"
        return None, mute
    return None, None


def _send_appcommand_volume(action: str, user32: Any) -> bool:
    """Send volume WM_APPCOMMAND to foreground or desktop window."""
    u32 = _get_user32(user32)
    if not u32:
        return False

    cmd_map = {
        "mute": APPCOMMAND_VOLUME_MUTE,
        "unmute": APPCOMMAND_VOLUME_MUTE,
        "toggle_mute": APPCOMMAND_VOLUME_MUTE,
        "up": APPCOMMAND_VOLUME_UP,
        "down": APPCOMMAND_VOLUME_DOWN,
    }
    cmd = cmd_map.get(action)
    if cmd is None:
        return False

    hwnd = 0
    if hasattr(u32, "GetForegroundWindow"):
        try:
            fg = u32.GetForegroundWindow()
            hwnd = int(fg) if fg else 0
        except Exception:
            hwnd = 0
    if not hwnd and hasattr(u32, "GetDesktopWindow"):
        try:
            dt = u32.GetDesktopWindow()
            hwnd = int(dt) if dt else 0
        except Exception:
            hwnd = 0

    lparam = cmd << 16
    sent = False
    if hasattr(u32, "SendMessageW"):
        try:
            u32.SendMessageW(hwnd, WM_APPCOMMAND, hwnd, lparam)
            sent = True
        except Exception:
            pass
    if not sent and hasattr(u32, "DefWindowProcW"):
        try:
            u32.DefWindowProcW(hwnd, WM_APPCOMMAND, hwnd, lparam)
            sent = True
        except Exception:
            pass
    return sent


def _handle_volume_control(
    action: str,
    value: int | None = None,
    *,
    user32: Any = None,
    powershell_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    if not isinstance(action, str):
        raise ValueError("action must be a string.")
    if isinstance(value, bool):
        raise ValueError("value must be an integer (not a boolean).")

    norm_action = action.strip().lower()
    if norm_action not in VALID_VOLUME_ACTIONS:
        actions_str = ", ".join(VALID_VOLUME_ACTIONS)
        raise ValueError(f"Unsupported volume action {action!r}. Valid actions: {actions_str}.")

    if norm_action == "set" and value is None:
        raise ValueError("value (0..100) is required when action is 'set'.")

    # 1. Try pycaw / comtypes if available
    try:
        from comtypes import CLSCTX_ALL
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume

        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        endpoint = interface.QueryInterface(IAudioEndpointVolume)

        if norm_action == "get":
            vol = int(round(endpoint.GetMasterVolumeLevelScalar() * 100))
            is_muted = bool(endpoint.GetMute())
            return {
                "ok": True,
                "control": "volume",
                "action": "get",
                "volume": vol,
                "muted": is_muted,
            }
        if norm_action == "set":
            if value is None:
                raise ValueError("value (0..100) is required when action is 'set'.")
            clamped = max(0, min(100, int(value)))
            endpoint.SetMasterVolumeLevelScalar(float(clamped) / 100.0, None)
            return {"ok": True, "control": "volume", "action": "set", "volume": clamped}
        if norm_action in ("up", "down"):
            step = int(value) if value is not None else 5
            curr = int(round(endpoint.GetMasterVolumeLevelScalar() * 100))
            new_v = max(0, min(100, curr + step if norm_action == "up" else curr - step))
            endpoint.SetMasterVolumeLevelScalar(float(new_v) / 100.0, None)
            return {
                "ok": True,
                "control": "volume",
                "action": norm_action,
                "volume": new_v,
                "previous_volume": curr,
            }
        if norm_action == "mute":
            endpoint.SetMute(1, None)
            return {"ok": True, "control": "volume", "action": "mute", "muted": True}
        if norm_action == "unmute":
            endpoint.SetMute(0, None)
            return {"ok": True, "control": "volume", "action": "unmute", "muted": False}
        if norm_action == "toggle_mute":
            curr = bool(endpoint.GetMute())
            endpoint.SetMute(0 if curr else 1, None)
            return {"ok": True, "control": "volume", "action": "toggle_mute", "muted": not curr}
    except ImportError:
        pass
    except Exception as exc:
        logger.debug("pycaw volume control failed (%s); trying PowerShell / Win32 fallback.", exc)

    # 2. PowerShell CoreAudio fallback
    try:
        if norm_action == "get":
            vol, mute = _run_coreaudio_powershell("get", powershell_runner=powershell_runner)
            return {
                "ok": True,
                "control": "volume",
                "action": "get",
                "volume": vol if vol is not None else 50,
                "muted": bool(mute),
            }
        if norm_action == "set":
            if value is None:
                raise ValueError("value (0..100) is required when action is 'set'.")
            clamped = max(0, min(100, int(value)))
            scalar = float(clamped) / 100.0
            _run_coreaudio_powershell("set", scalar=scalar, powershell_runner=powershell_runner)
            return {"ok": True, "control": "volume", "action": "set", "volume": clamped}
        if norm_action in ("up", "down"):
            vol, _ = _run_coreaudio_powershell("get", powershell_runner=powershell_runner)
            curr = vol if vol is not None else 50
            step = int(value) if value is not None else 5
            new_v = max(0, min(100, curr + step if norm_action == "up" else curr - step))
            scalar = float(new_v) / 100.0
            _run_coreaudio_powershell("set", scalar=scalar, powershell_runner=powershell_runner)
            return {
                "ok": True,
                "control": "volume",
                "action": norm_action,
                "volume": new_v,
                "previous_volume": curr,
            }
        if norm_action == "mute":
            _run_coreaudio_powershell("mute", powershell_runner=powershell_runner)
            return {"ok": True, "control": "volume", "action": "mute", "muted": True}
        if norm_action == "unmute":
            _run_coreaudio_powershell("unmute", powershell_runner=powershell_runner)
            return {"ok": True, "control": "volume", "action": "unmute", "muted": False}
        if norm_action == "toggle_mute":
            _, mute = _run_coreaudio_powershell("toggle_mute", powershell_runner=powershell_runner)
            return {"ok": True, "control": "volume", "action": "toggle_mute", "muted": bool(mute)}
    except Exception as exc:
        logger.debug(
            "PowerShell CoreAudio failed (%s); trying WM_APPCOMMAND / keybd_event fallback.", exc
        )

    # 3. Win32 WM_APPCOMMAND and keybd_event fallback for volume up/down/mute
    u32 = _get_user32(user32)
    if u32:
        if norm_action in ("mute", "unmute", "toggle_mute", "up", "down"):
            appcmd_ok = _send_appcommand_volume(norm_action, u32)
            if hasattr(u32, "keybd_event"):
                if norm_action in ("mute", "unmute", "toggle_mute"):
                    u32.keybd_event(VK_VOLUME_MUTE, 0, KEYEVENTF_EXTENDEDKEY, 0)
                    u32.keybd_event(VK_VOLUME_MUTE, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)
                elif norm_action == "up":
                    u32.keybd_event(VK_VOLUME_UP, 0, KEYEVENTF_EXTENDEDKEY, 0)
                    u32.keybd_event(VK_VOLUME_UP, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)
                elif norm_action == "down":
                    u32.keybd_event(VK_VOLUME_DOWN, 0, KEYEVENTF_EXTENDEDKEY, 0)
                    u32.keybd_event(VK_VOLUME_DOWN, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)
            if appcmd_ok or hasattr(u32, "keybd_event"):
                res: dict[str, Any] = {
                    "ok": True,
                    "control": "volume",
                    "action": norm_action,
                }
                if norm_action in ("mute", "unmute", "toggle_mute"):
                    res["muted"] = (
                        True
                        if norm_action == "mute"
                        else (False if norm_action == "unmute" else None)
                    )
                return res

    raise RuntimeError(f"Unable to execute volume action {action!r}: no audio backend succeeded.")


def _handle_brightness_control(
    action: str,
    value: int | None = None,
    *,
    powershell_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    if not isinstance(action, str):
        raise ValueError("action must be a string.")
    if isinstance(value, bool):
        raise ValueError("value must be an integer (not a boolean).")

    norm_action = action.strip().lower()
    if norm_action not in VALID_BRIGHTNESS_ACTIONS:
        actions_str = ", ".join(VALID_BRIGHTNESS_ACTIONS)
        raise ValueError(
            f"Unsupported brightness action {action!r}. Valid actions: {actions_str}."
        )

    if norm_action == "set" and value is None:
        raise ValueError("value (0..100) is required when action is 'set'.")

    runner = powershell_runner or subprocess.run
    ps_exe = shutil.which("powershell.exe") or "powershell.exe"

    def _get_brightness() -> int:
        cmd = (
            "(Get-CimInstance -Namespace root\\wmi -ClassName "
            "WmiMonitorBrightness).CurrentBrightness"
        )
        proc = runner(
            [ps_exe, "-NoProfile", "-NonInteractive", "-Command", cmd],
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8.0,
            check=False,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"Brightness query failed: {err}")
        val = (proc.stdout or "").strip()
        if not val:
            raise RuntimeError(
                "No brightness value returned (display may not expose WmiMonitorBrightness)."
            )
        return int(val.split()[0])

    def _set_brightness(level: int) -> None:
        clamped = max(0, min(100, int(level)))
        script = (
            "$b = Get-CimInstance -Namespace root\\wmi -ClassName WmiMonitorBrightnessMethods; "
            "if ($b) { "
            f"$b | Invoke-CimMethod -MethodName WmiSetBrightness "
            f"-Arguments @{{Timeout = 1; Brightness = [uint32]{clamped}}} "
            "} else { throw 'WmiMonitorBrightnessMethods not available' }"
        )
        proc = runner(
            [ps_exe, "-NoProfile", "-NonInteractive", "-Command", script],
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=8.0,
            check=False,
        )
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"Brightness set failed: {err}")

    try:
        if norm_action == "get":
            curr = _get_brightness()
            return {"ok": True, "control": "brightness", "action": "get", "brightness": curr}

        if norm_action == "set":
            if value is None:
                raise ValueError("value (0..100) is required when action is 'set'.")
            clamped = max(0, min(100, int(value)))
            _set_brightness(clamped)
            return {"ok": True, "control": "brightness", "action": "set", "brightness": clamped}

        if norm_action in ("up", "down"):
            curr = _get_brightness()
            step = int(value) if value is not None else 10
            new_b = max(0, min(100, curr + step if norm_action == "up" else curr - step))
            _set_brightness(new_b)
            return {
                "ok": True,
                "control": "brightness",
                "action": norm_action,
                "brightness": new_b,
                "previous_brightness": curr,
            }
    except Exception as exc:
        logger.debug("WMI brightness operation failed: %s", exc)
        return {
            "ok": False,
            "control": "brightness",
            "action": norm_action,
            "supported": False,
            "message": (
                "Display brightness control is not available on this monitor "
                "(external monitors often do not expose WMI brightness methods)."
            ),
            "error": str(exc),
        }

    raise ValueError(f"Unhandled brightness action {action!r}.")


def _handle_media_control(
    action: str,
    *,
    user32: Any = None,
    media_backend: MediaVerificationBackend | None = None,
    verifier: ActionVerifier | None = None,
    verify_timeout_s: float = 1.5,
) -> dict[str, Any]:
    if not isinstance(action, str):
        raise ValueError("action must be a string.")
    norm_action = action.strip().lower()
    if norm_action not in VALID_MEDIA_ACTIONS:
        raise ValueError(
            f"Unsupported media action {action!r}. Valid actions: play_pause, next, previous, stop."
        )

    vk_map = {
        "play_pause": VK_MEDIA_PLAY_PAUSE,
        "play": VK_MEDIA_PLAY_PAUSE,
        "pause": VK_MEDIA_PLAY_PAUSE,
        "next": VK_MEDIA_NEXT_TRACK,
        "previous": VK_MEDIA_PREV_TRACK,
        "prev": VK_MEDIA_PREV_TRACK,
        "stop": VK_MEDIA_STOP,
    }
    vk = vk_map[norm_action]
    should_verify = verifier is not None or media_backend is not None or user32 is None
    active_verifier = (
        verifier
        if verifier is not None
        else (ActionVerifier(media_backend=media_backend) if should_verify else None)
    )
    baseline = None
    if active_verifier is not None and norm_action in {"play_pause", "next", "previous", "prev"}:
        baseline = active_verifier.probe_media_session()

    u32 = _get_user32(user32)
    if u32 and hasattr(u32, "keybd_event"):
        u32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY, 0)
        u32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)

    result: dict[str, Any] = {"ok": True, "control": "media", "action": norm_action}
    if active_verifier is not None:
        expected_status_map = {
            "play": "playing",
            "pause": "paused",
            "stop": "stopped",
        }
        use_baseline = norm_action in {"play_pause", "next", "previous", "prev"}
        outcome = active_verifier.verify_media_state(
            expected_status=expected_status_map.get(norm_action),
            timeout_s=verify_timeout_s,
            baseline=baseline if use_baseline else None,
        )
        result["verification"] = outcome.to_dict()
    return result


def _handle_theme_control(
    action: str,
    *,
    winreg_mod: Any = None,
) -> dict[str, Any]:
    if not isinstance(action, str):
        raise ValueError("action must be a string.")
    norm_action = action.strip().lower()
    if norm_action not in VALID_THEME_ACTIONS:
        actions_str = ", ".join(VALID_THEME_ACTIONS)
        raise ValueError(f"Unsupported theme action {action!r}. Valid actions: {actions_str}.")

    wreg = winreg_mod
    if wreg is None:
        try:
            import winreg

            wreg = winreg
        except ImportError as exc:
            raise OSError("winreg is only available on Windows.") from exc

    subkey = r"Software\Microsoft\Windows\CurrentVersion\Themes\Personalize"

    def _get_current() -> str:
        try:
            with wreg.OpenKey(wreg.HKEY_CURRENT_USER, subkey) as k:
                val, _ = wreg.QueryValueEx(k, "AppsUseLightTheme")
                return "light" if val == 1 else "dark"
        except Exception:
            return "dark"

    def _apply(theme: str) -> None:
        int_val = 1 if theme == "light" else 0
        with wreg.CreateKey(wreg.HKEY_CURRENT_USER, subkey) as k:
            wreg.SetValueEx(k, "AppsUseLightTheme", 0, wreg.REG_DWORD, int_val)
            wreg.SetValueEx(k, "SystemUsesLightTheme", 0, wreg.REG_DWORD, int_val)

    if norm_action in ("dark", "light"):
        _apply(norm_action)
        return {"ok": True, "control": "theme", "action": norm_action, "theme": norm_action}

    if norm_action == "toggle":
        curr = _get_current()
        new_theme = "dark" if curr == "light" else "light"
        _apply(new_theme)
        return {
            "ok": True,
            "control": "theme",
            "action": "toggle",
            "theme": new_theme,
            "previous_theme": curr,
        }

    raise ValueError(f"Unhandled theme action {action!r}.")


def system_control(
    control: str,
    action: str,
    value: int | None = None,
    *,
    user32: Any = None,
    powershell_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    winreg_mod: Any = None,
    media_backend: MediaVerificationBackend | None = None,
    verifier: ActionVerifier | None = None,
) -> dict[str, Any]:
    """Control system hardware, media, or theme settings."""
    if not isinstance(control, str):
        raise ValueError("control must be a string.")
    if not isinstance(action, str):
        raise ValueError("action must be a string.")
    if isinstance(value, bool):
        raise ValueError("value must be an integer (not a boolean).")

    norm_ctrl = control.strip().lower()
    if norm_ctrl not in VALID_CONTROLS:
        raise ValueError(
            f"Unsupported control {control!r}. Valid controls: {', '.join(VALID_CONTROLS)}."
        )

    if norm_ctrl == "volume":
        return _handle_volume_control(
            action, value, user32=user32, powershell_runner=powershell_runner
        )
    if norm_ctrl == "brightness":
        return _handle_brightness_control(action, value, powershell_runner=powershell_runner)
    if norm_ctrl == "media":
        return _handle_media_control(
            action,
            user32=user32,
            media_backend=media_backend,
            verifier=verifier,
        )
    if norm_ctrl == "theme":
        return _handle_theme_control(action, winreg_mod=winreg_mod)

    raise ValueError(f"Unhandled control {control!r}.")


# ---------------------------------------------------------------------------
# Tool 3: system_diagnostics
# ---------------------------------------------------------------------------


def _get_battery_info(psutil_mod: Any = None, kernel32: Any = None) -> dict[str, Any]:
    ps = _resolve_psutil(psutil_mod)
    if ps is not None:
        try:
            b = ps.sensors_battery()
            if b is not None:
                return {
                    "percentage": round(b.percent, 1),
                    "plugged": bool(b.power_plugged),
                    "status": "charging" if b.power_plugged else "discharging",
                }
        except Exception:
            pass

    k32 = _get_kernel32(kernel32)
    if k32 and hasattr(k32, "GetSystemPowerStatus"):
        status = SYSTEM_POWER_STATUS()
        if k32.GetSystemPowerStatus(ctypes.byref(status)):
            is_plugged = bool(status.ACLineStatus == 1)
            pct = int(status.BatteryLifePercent)
            if status.BatteryFlag == 128 or pct == 255:
                return {"percentage": None, "plugged": is_plugged, "status": "no_battery"}
            return {
                "percentage": pct,
                "plugged": is_plugged,
                "status": "charging" if is_plugged else "discharging",
            }

    return {"percentage": None, "plugged": True, "status": "unknown"}


def _get_network_info(
    powershell_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    connected = False
    ssid: str | None = None
    signal: str | None = None
    runner = powershell_runner or subprocess.run

    netsh_exe = shutil.which("netsh.exe") or shutil.which("netsh")
    if netsh_exe:
        try:
            proc = runner(
                [netsh_exe, "wlan", "show", "interfaces"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=4.0,
                check=False,
            )
            out = proc.stdout or ""
            for line in out.splitlines():
                line_str = line.strip()
                if line_str.startswith("State") and ":" in line_str:
                    connected = "connected" in line_str.split(":", 1)[1].lower()
                elif (
                    line_str.startswith("SSID")
                    and not line_str.startswith("SSID name")
                    and ":" in line_str
                ):
                    ssid = line_str.split(":", 1)[1].strip()
                elif line_str.startswith("Signal") and ":" in line_str:
                    signal = line_str.split(":", 1)[1].strip()
        except Exception:
            pass

    # Quick internet connectivity check
    has_internet = False
    try:
        s = socket.create_connection(("1.1.1.1", 53), timeout=0.6)
        s.close()
        has_internet = True
    except Exception:
        try:
            s = socket.create_connection(("8.8.8.8", 53), timeout=0.6)
            s.close()
            has_internet = True
        except Exception:
            has_internet = False

    return {
        "connected": connected or has_internet,
        "ssid": ssid,
        "signal": signal,
        "internet": has_internet,
    }


def _get_ram_info(psutil_mod: Any = None, kernel32: Any = None) -> dict[str, Any]:
    ps = _resolve_psutil(psutil_mod)
    if ps is not None:
        try:
            vm = ps.virtual_memory()
            return {
                "total_gb": round(vm.total / (1024**3), 2),
                "used_gb": round(vm.used / (1024**3), 2),
                "percent": round(vm.percent, 1),
            }
        except Exception:
            pass

    k32 = _get_kernel32(kernel32)
    if k32 and hasattr(k32, "GlobalMemoryStatusEx"):
        m = MEMORYSTATUSEX()
        m.dwLength = ctypes.sizeof(MEMORYSTATUSEX)
        if k32.GlobalMemoryStatusEx(ctypes.byref(m)):
            total_gb = round(m.ullTotalPhys / (1024**3), 2)
            used_gb = round((m.ullTotalPhys - m.ullAvailPhys) / (1024**3), 2)
            return {
                "total_gb": total_gb,
                "used_gb": used_gb,
                "percent": int(m.dwMemoryLoad),
            }

    return {"total_gb": 0.0, "used_gb": 0.0, "percent": 0.0}


def _get_top_processes(psutil_mod: Any = None, limit: int = 5) -> list[dict[str, Any]]:
    ps = _resolve_psutil(psutil_mod)
    if ps is not None:
        try:
            procs: list[dict[str, Any]] = []
            for p in ps.process_iter(["pid", "name", "memory_info", "cpu_percent"]):
                try:
                    info = p.info
                    mem = (
                        round(info["memory_info"].rss / (1024 * 1024), 1)
                        if info.get("memory_info")
                        else 0.0
                    )
                    procs.append({
                        "pid": info["pid"],
                        "name": info["name"] or "<unknown>",
                        "memory_mb": mem,
                        "cpu_percent": info.get("cpu_percent") or 0.0,
                    })
                except Exception:
                    continue
            procs.sort(key=lambda x: x["memory_mb"], reverse=True)
            return procs[:limit]
        except Exception:
            pass

    # Fallback to tasklist.exe
    tasklist_exe = shutil.which("tasklist.exe") or shutil.which("tasklist")
    if tasklist_exe:
        try:
            proc = subprocess.run(
                [tasklist_exe, "/FO", "CSV", "/NH"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=4.0,
                check=False,
            )
            out = proc.stdout or ""
            reader = csv.reader(out.strip().splitlines())
            procs = []
            for row in reader:
                if len(row) >= 5:
                    name = row[0]
                    try:
                        pid = int(row[1])
                    except ValueError:
                        continue
                    mem_str = (
                        row[4]
                        .replace(",", "")
                        .replace(" ", "")
                        .replace("K", "")
                        .replace("\xa0", "")
                    )
                    try:
                        kb = int(mem_str)
                    except ValueError:
                        kb = 0
                    procs.append({
                        "pid": pid,
                        "name": name,
                        "memory_mb": round(kb / 1024, 1),
                        "cpu_percent": 0.0,
                    })
            procs.sort(key=lambda x: x["memory_mb"], reverse=True)
            return procs[:limit]
        except Exception:
            pass

    return []


def _inspect_port(
    port: int,
    kill_port_process: bool = False,
    *,
    psutil_mod: Any = None,
    process_killer: Callable[[int], None] | None = None,
    powershell_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    if isinstance(port, bool) or not isinstance(port, int) or port <= 0 or port > 65535:
        raise ValueError(f"Invalid port {port!r}. Port must be between 1 and 65535.")

    found_pid: int | None = None
    target_port_str = str(port)

    # 1. Inspect via netstat -ano -p tcp
    netstat_exe = shutil.which("netstat.exe") or shutil.which("netstat")
    if netstat_exe:
        try:
            proc = subprocess.run(
                [netstat_exe, "-ano", "-p", "tcp"],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=4.0,
                check=False,
            )
            for line in (proc.stdout or "").splitlines():
                parts = line.split()
                if len(parts) >= 5 and parts[0].upper() == "TCP":
                    local_addr = parts[1]
                    state = parts[3].upper()
                    # Match exact port at end of address string, preventing 80 matching 8080
                    addr_port = local_addr.rsplit(":", 1)[-1]
                    if state == "LISTENING" and addr_port == target_port_str:
                        try:
                            found_pid = int(parts[4])
                            break
                        except ValueError:
                            continue
        except Exception:
            pass

    # 2. Try PowerShell Get-NetTCPConnection fallback if netstat did not resolve a listener
    if found_pid is None:
        try:
            runner = powershell_runner or subprocess.run
            ps_exe = shutil.which("powershell.exe") or "powershell.exe"
            script = (
                f"(Get-NetTCPConnection -LocalPort {port} -State Listen "
                "-ErrorAction SilentlyContinue | "
                "Select-Object -ExpandProperty OwningProcess -First 1)"
            )
            ps_proc = runner(
                [ps_exe, "-NoProfile", "-NonInteractive", "-Command", script],
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=4.0,
                check=False,
            )
            out_pid = (ps_proc.stdout or "").strip()
            if out_pid and out_pid.isdigit():
                found_pid = int(out_pid)
        except Exception:
            pass

    if found_pid is None:
        return {
            "ok": True,
            "check": "port",
            "port": port,
            "listening": False,
            "pid": None,
            "process_name": None,
            "killed": False,
        }

    # Resolve process name
    proc_name = "<unknown>"
    ps = _resolve_psutil(psutil_mod)
    if ps is not None:
        try:
            p = ps.Process(found_pid)
            proc_name = p.name()
        except Exception:
            pass

    if proc_name == "<unknown>":
        tasklist_exe = shutil.which("tasklist.exe") or shutil.which("tasklist")
        if tasklist_exe:
            try:
                proc = subprocess.run(
                    [tasklist_exe, "/FI", f"PID eq {found_pid}", "/FO", "CSV", "/NH"],
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=3.0,
                    check=False,
                )
                reader = csv.reader((proc.stdout or "").strip().splitlines())
                for row in reader:
                    if len(row) >= 2 and row[0]:
                        proc_name = row[0]
                        break
            except Exception:
                pass

    if not kill_port_process:
        return {
            "ok": True,
            "check": "port",
            "port": port,
            "listening": True,
            "pid": found_pid,
            "process_name": proc_name,
            "killed": False,
        }

    # Terminate process if requested, guarding critical system PIDs <= 4
    if found_pid <= 4:
        return {
            "ok": False,
            "check": "port",
            "port": port,
            "listening": True,
            "pid": found_pid,
            "process_name": proc_name,
            "killed": False,
            "error": f"Refusing to terminate critical system PID {found_pid}.",
        }

    if process_killer is not None:
        try:
            process_killer(found_pid)
        except Exception as exc:
            return {
                "ok": False,
                "check": "port",
                "port": port,
                "listening": True,
                "pid": found_pid,
                "process_name": proc_name,
                "killed": False,
                "error": f"Failed to terminate process PID {found_pid}: {exc}",
            }
    else:
        taskkill_exe = shutil.which("taskkill.exe") or shutil.which("taskkill")
        if taskkill_exe:
            kill_proc = subprocess.run(
                [taskkill_exe, "/F", "/PID", str(found_pid)],
                capture_output=True,
                text=True,
                timeout=4.0,
                check=False,
            )
            if kill_proc.returncode != 0:
                err_detail = (kill_proc.stderr or kill_proc.stdout or "").strip()
                return {
                    "ok": False,
                    "check": "port",
                    "port": port,
                    "listening": True,
                    "pid": found_pid,
                    "process_name": proc_name,
                    "killed": False,
                    "error": (
                        f"Failed to terminate process (PID {found_pid}): "
                        f"{err_detail or f'exit code {kill_proc.returncode}'}"
                    ),
                }
        else:
            import os
            import signal

            try:
                os.kill(found_pid, signal.SIGTERM)
            except Exception as exc:
                return {
                    "ok": False,
                    "check": "port",
                    "port": port,
                    "listening": True,
                    "pid": found_pid,
                    "process_name": proc_name,
                    "killed": False,
                    "error": f"Failed to terminate process PID {found_pid}: {exc}",
                }

    return {
        "ok": True,
        "check": "port",
        "port": port,
        "listening": True,
        "pid": found_pid,
        "process_name": proc_name,
        "killed": True,
        "message": f"Terminated process '{proc_name}' (PID {found_pid}) listening on port {port}.",
    }


def system_diagnostics(
    check: str = "overview",
    port: int | None = None,
    kill_port_process: bool = False,
    *,
    psutil_mod: Any = None,
    kernel32: Any = None,
    powershell_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    process_killer: Callable[[int], None] | None = None,
) -> dict[str, Any]:
    """Inspect system hardware health, battery, network, resource usage, or listening ports."""
    if not isinstance(check, str):
        raise ValueError("check must be a string.")
    norm_check = check.strip().lower()

    # Automatically infer check="port" if port is provided and check was left as default "overview"
    if port is not None and norm_check == "overview":
        norm_check = "port"

    if norm_check not in VALID_DIAGNOSTIC_CHECKS:
        checks_str = ", ".join(VALID_DIAGNOSTIC_CHECKS)
        raise ValueError(f"Unsupported diagnostic check {check!r}. Valid checks: {checks_str}.")

    if norm_check == "battery":
        battery = _get_battery_info(psutil_mod=psutil_mod, kernel32=kernel32)
        return {"ok": True, "check": "battery", "battery": battery}

    if norm_check == "network":
        network = _get_network_info(powershell_runner=powershell_runner)
        return {"ok": True, "check": "network", "network": network}

    if norm_check == "top_processes":
        procs = _get_top_processes(psutil_mod=psutil_mod, limit=5)
        return {"ok": True, "check": "top_processes", "top_processes": procs}

    if norm_check == "port":
        if port is None:
            raise ValueError("port is required when check is 'port'.")
        return _inspect_port(
            port=port,
            kill_port_process=kill_port_process,
            psutil_mod=psutil_mod,
            process_killer=process_killer,
            powershell_runner=powershell_runner,
        )

    if norm_check == "overview":
        battery = _get_battery_info(psutil_mod=psutil_mod, kernel32=kernel32)
        network = _get_network_info(powershell_runner=powershell_runner)
        ram = _get_ram_info(psutil_mod=psutil_mod, kernel32=kernel32)
        procs = _get_top_processes(psutil_mod=psutil_mod, limit=5)
        return {
            "ok": True,
            "check": "overview",
            "battery": battery,
            "network": network,
            "ram": ram,
            "top_processes": procs,
        }

    raise ValueError(f"Unhandled diagnostic check {check!r}.")


# ---------------------------------------------------------------------------
# Tool Registry Integration
# ---------------------------------------------------------------------------


def register_system_control_tools(
    registry: ToolRegistry,
    *,
    manage_window_risk: RiskLevel = RiskLevel.READ_ONLY,
    system_control_risk: RiskLevel = RiskLevel.READ_ONLY,
    system_diagnostics_risk: RiskLevel = RiskLevel.READ_ONLY,
    focus_backend: FocusPreconditionBackend | None = None,
    media_backend: MediaVerificationBackend | None = None,
    verifier: ActionVerifier | None = None,
    user32: Any = None,
) -> None:
    """Register window management, hardware/system controls, and diagnostic tools."""
    if focus_backend is not None or verifier is not None or user32 is not None:

        def _manage_window_handler(
            action: str,
            title_query: str | None = None,
        ) -> dict[str, Any]:
            return manage_window(
                action,
                title_query,
                user32=user32,
                focus_backend=focus_backend,
                verifier=verifier,
            )

        manage_window_fn: Callable[..., dict[str, Any]] = _manage_window_handler
    else:
        manage_window_fn = manage_window

    if media_backend is not None or verifier is not None or user32 is not None:

        def _system_control_handler(
            control: str,
            action: str,
            value: int | None = None,
        ) -> dict[str, Any]:
            return system_control(
                control,
                action,
                value,
                user32=user32,
                media_backend=media_backend,
                verifier=verifier,
            )

        system_control_fn: Callable[..., dict[str, Any]] = _system_control_handler
    else:
        system_control_fn = system_control

    registry.register(
        name="manage_window",
        description=(
            "Inspect and control application window placement on Windows. "
            "Supported actions: 'list' (all visible windows), 'inspect_layout' / 'layout' "
            "(deterministic Win32 Z-order layout and slot geometry), 'focus', "
            "'snap_left' (snap window to left half / 50% of screen), "
            "'snap_right' (snap window to right half / 50% of screen), "
            "'maximize', 'minimize', 'restore', 'close'. "
            "Specify the target window by app name or window title substring in `title_query` "
            "(e.g., 'Calculator', 'Notepad', 'Chrome'), or omit `title_query` to act on the "
            "current foreground window."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": list(VALID_WINDOW_ACTIONS),
                    "description": "Window action to perform.",
                },
                "title_query": {
                    "type": "string",
                    "description": (
                        "Optional application name or window title substring to match "
                        "(e.g., 'Calculator', 'Notepad'), or omit for the active foreground window."
                    ),
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        risk_level=manage_window_risk,
        handler=manage_window_fn,
    )

    if "inspect_window_layout" not in {d.name for d in registry.list_definitions()}:
        registry.register(
            name="inspect_window_layout",
            description=(
                "Return deterministic Win32 window geometry in Z-order (front-to-back) with "
                "DWMWA_EXTENDED_FRAME_BOUNDS, monitor rcWork, width_ratio, detected_slot "
                "('left_half', 'right_half', 'maximized', 'full_width', 'custom'), and "
                "extends_behind_foreground to verify true 50/50 split-screen vs occluded windows."
            ),
            input_schema={
                "type": "object",
                "properties": {},
                "additionalProperties": False,
            },
            risk_level=RiskLevel.READ_ONLY,
            handler=lambda: inspect_window_layout(
                user32=user32,
                focus_backend=focus_backend,
            ),
        )

    registry.register(
        name="system_control",
        description=(
            "Control hardware and system settings on Windows: volume (get, set 0..100, up, down, "
            "mute, unmute, toggle_mute), display brightness (get, set 0..100, up, down), "
            "media playback (play_pause, next, previous, stop), and theme (dark, light, toggle)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "control": {
                    "type": "string",
                    "enum": list(VALID_CONTROLS),
                    "description": "System subsystem to control: volume, brightness, media, theme.",
                },
                "action": {
                    "type": "string",
                    "description": (
                        "Action to perform on target control. "
                        "volume: get, set, up, down, mute, unmute, toggle_mute; "
                        "brightness: get, set, up, down; "
                        "media: play_pause, next, previous, stop; "
                        "theme: dark, light, toggle."
                    ),
                },
                "value": {
                    "type": "integer",
                    "description": (
                        "Optional level or adjustment value (0..100 for volume and brightness)."
                    ),
                },
            },
            "required": ["control", "action"],
            "additionalProperties": False,
        },
        risk_level=system_control_risk,
        handler=system_control_fn,
    )

    registry.register(
        name="system_diagnostics",
        description=(
            "Query system health and hardware diagnostics: overview (battery, network, RAM, "
            "top processes), battery, network, top_processes, or port (inspect TCP listener, "
            "optionally terminate owning PID)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "check": {
                    "type": "string",
                    "enum": list(VALID_DIAGNOSTIC_CHECKS),
                    "description": (
                        "Diagnostic check to run: overview, battery, network, top_processes, port."
                    ),
                    "default": "overview",
                },
                "port": {
                    "type": "integer",
                    "description": "TCP port number to inspect when check is 'port'.",
                },
                "kill_port_process": {
                    "type": "boolean",
                    "description": (
                        "Whether to terminate the process listening on the specified port."
                    ),
                    "default": False,
                },
            },
            "additionalProperties": False,
        },
        risk_level=system_diagnostics_risk,
        handler=system_diagnostics,
    )

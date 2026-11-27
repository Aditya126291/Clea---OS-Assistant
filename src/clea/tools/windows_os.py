"""Safe Tier-1 Windows OS tools: app launch/focus, media/volume, and clipboard access.

All subprocess invocations strictly enforce ``shell=False``. All Win32 ctypes
functions set explicit ``argtypes`` and ``restype`` (using ``c_void_p`` for
64-bit memory handles/pointers) and accept optional injected API doubles for
deterministic unit testing.
"""

from __future__ import annotations

import base64
import contextlib
import ctypes
import difflib
import inspect
import json
import logging
import math
import os
import re
import shutil
import subprocess
import sys
import threading
import time
import urllib.parse
import uuid
from collections.abc import Callable, Iterator
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from clea.tools.cdp_bridge import (
    CDP_REMOTE_DEBUGGING_HINT,
    CDPBridge,
    get_default_cdp_bridge,
    has_explicit_default_cdp_bridge,
    is_cdp_ref,
    is_chromium_app_name,
    is_chromium_process,
)
from clea.tools.clipboard import ClipboardLockedError, Win32ClipboardBackend
from clea.tools.guards import (
    ActionVerifier,
    FocusPreconditionBackend,
    MediaVerificationBackend,
    PreconditionFailedError,
    PreconditionGuard,
    Win32FocusPreconditionBackend,
)
from clea.tools.registry import RiskLevel, ToolRegistry
from clea.tools.subprocess_utils import run_powershell_hidden
from clea.tools.system_controls import inspect_window_layout
from clea.tools.uia_com import (
    NativeUiaComEngine,
    UiaComUnavailableError,
    classify_accessibility_coverage,
    get_default_uia_com_engine,
    has_explicit_default_uia_com_engine,
)

logger = logging.getLogger(__name__)

# Window show/focus constants
SW_SHOWNORMAL: int = 1
SW_MAXIMIZE: int = 3
SW_SHOW: int = 5
SW_MINIMIZE: int = 6
SW_RESTORE: int = 9
WM_CLOSE: int = 0x0010
UOI_NAME: int = 2
GA_ROOT: int = 2
GW_OWNER: int = 4
DWMWA_EXTENDED_FRAME_BOUNDS: int = 9
DWMWA_CLOAKED: int = 14
HWND_TOP: int = 0
HWND_TOPMOST: int = -1
HWND_NOTOPMOST: int = -2
SWP_NOSIZE: int = 0x0001
SWP_NOMOVE: int = 0x0002
SWP_SHOWWINDOW: int = 0x0040
VK_TAB_OS: int = 0x09
VK_RETURN_OS: int = 0x0D
MONITOR_DEFAULTTONEAREST: int = 2
_STARTF_USESHOWWINDOW: int = getattr(subprocess, "STARTF_USESHOWWINDOW", 0x00000001)

_WINDOWS_OS_DWMAPI: Any = None

# Virtual key codes for media & step volume controls
VK_VOLUME_MUTE: int = 0xAD
VK_VOLUME_DOWN: int = 0xAE
VK_VOLUME_UP: int = 0xAF
VK_MEDIA_NEXT_TRACK: int = 0xB0
VK_MEDIA_PREV_TRACK: int = 0xB1
VK_MEDIA_PLAY_PAUSE: int = 0xB3

KEYEVENTF_EXTENDEDKEY: int = 0x0001
KEYEVENTF_KEYUP: int = 0x0002

# Clipboard constants
CF_UNICODETEXT: int = 13
GMEM_MOVEABLE: int = 0x0002
DEFAULT_CLIPBOARD_MAX_CHARS: int = 8000
MAX_CLIPBOARD_CHARS: int = 100_000

PROCESS_QUERY_LIMITED_INFORMATION: int = 0x1000

# Allowlisted ms-settings URIs
_ALLOWED_MS_SETTINGS: frozenset[str] = frozenset(
    {
        "ms-settings:",
        "ms-settings:home",
        "ms-settings:system",
        "ms-settings:display",
        "ms-settings:sound",
        "ms-settings:notifications",
        "ms-settings:quiethours",
        "ms-settings:powersleep",
        "ms-settings:batterysaver",
        "ms-settings:storagesense",
        "ms-settings:storagepolicies",
        "ms-settings:multitasking",
        "ms-settings:clipboard",
        "ms-settings:project",
        "ms-settings:about",
        "ms-settings:accounts",
        "ms-settings:bluetooth",
        "ms-settings:connecteddevices",
        "ms-settings:printers",
        "ms-settings:mousetouchpad",
        "ms-settings:network",
        "ms-settings:network-wifi",
        "ms-settings:network-ethernet",
        "ms-settings:personalization",
        "ms-settings:personalization-background",
        "ms-settings:personalization-colors",
        "ms-settings:themes",
        "ms-settings:lockscreen",
        "ms-settings:appsfeatures",
        "ms-settings:defaultapps",
        "ms-settings:optionalfeatures",
        "ms-settings:startupapps",
        "ms-settings:dateandtime",
        "ms-settings:regionlanguage",
        "ms-settings:gaming-gamebar",
        "ms-settings:easeofaccess",
        "ms-settings:privacy",
        "ms-settings:privacy-microphone",
        "ms-settings:privacy-webcam",
        "ms-settings:privacy-location",
        "ms-settings:windowsdefender",
        "ms-settings:windowsupdate",
    }
)

_ALLOWED_URL_SCHEMES: frozenset[str] = frozenset({"http", "https"})

_DANGEROUS_URL_SCHEMES: frozenset[str] = frozenset(
    {
        "file",
        "javascript",
        "vbscript",
        "jscript",
        "ms-msdt",
        "search-ms",
        "shell",
        "cmd",
        "powershell",
        "hta",
        "wscript",
        "data",
        "blob",
        "ms-officecmd",
    }
)

_DANGEROUS_SCHEME_PREFIXES: tuple[str, ...] = (
    "file:",
    "javascript:",
    "vbscript:",
    "jscript:",
    "ms-msdt:",
    "search-ms:",
    "shell:",
    "cmd:",
    "powershell:",
    "hta:",
    "wscript:",
    "data:",
    "blob:",
    "ms-officecmd:",
    "ms-settings:",
    "ws:",
    "wss:",
    "about:",
    "chrome:",
    "edge:",
)

_RFC3986_SCHEME_PREFIX_RE = re.compile(r"^([a-zA-Z][a-zA-Z0-9+.-]*):")
_HOST_PORT_RE = re.compile(
    r"^(?:localhost|[a-zA-Z0-9-]+(?:\.[a-zA-Z0-9-]+)+):\d+(?:[/?#]|$)",
    re.IGNORECASE,
)
_CUSTOM_URL_FORBIDDEN_CHARS_RE = re.compile(r"[|;`$()<>\"'\\]|&&|\|\|")
_STALE_GEOMETRY_TOLERANCE_PX: int = 12
_SCREEN_GEOMETRY_MAX_AGE_S: float = 120.0

_SITE_SHORTCUTS: dict[str, str] = {
    "google": "https://www.google.com",
    "github": "https://github.com",
    "youtube": "https://www.youtube.com",
    "spotify": "https://open.spotify.com",
    "whatsapp": "https://web.whatsapp.com",
    "netflix": "https://www.netflix.com",
    "discord": "https://discord.com/app",
    "slack": "https://app.slack.com",
    "notion": "https://www.notion.so",
    "reddit": "https://www.reddit.com",
    "twitter": "https://x.com",
    "x": "https://x.com",
    "gmail": "https://mail.google.com",
    "mail": "https://mail.google.com",
    "docs": "https://docs.google.com",
    "chatgpt": "https://chatgpt.com",
    "wikipedia": "https://www.wikipedia.org",
}

VALID_SYSTEM_ACTIONS: tuple[str, ...] = (
    "get_brightness",
    "set_brightness",
    "step_brightness",
    "brightness_up",
    "brightness_down",
    "minimize_window",
    "maximize_window",
    "restore_window",
    "close_window",
    "snap_left",
    "snap_right",
    "snap_left_window",
    "snap_right_window",
    "minimize",
    "maximize",
    "restore",
    "close",
    "lock_screen",
    "lock",
    "open_settings",
    "settings",
    "window",
    "window_action",
    "inspect_window_layout",
    "inspect_layout",
    "layout",
)

VALID_MEDIA_VOLUME_ACTIONS: tuple[str, ...] = (
    "mute",
    "unmute",
    "toggle_mute",
    "volume_up",
    "volume_down",
    "set_volume",
    "play_pause",
    "next_track",
    "previous_track",
)

_MEDIA_VOLUME_VK_MAP: dict[str, int] = {
    "mute": VK_VOLUME_MUTE,
    "unmute": VK_VOLUME_MUTE,
    "toggle_mute": VK_VOLUME_MUTE,
    "volume_up": VK_VOLUME_UP,
    "volume_down": VK_VOLUME_DOWN,
    "play_pause": VK_MEDIA_PLAY_PAUSE,
    "next_track": VK_MEDIA_NEXT_TRACK,
    "previous_track": VK_MEDIA_PREV_TRACK,
}

_SHELL_METACHAR_RE = re.compile(r"[&|;`$()<>\"'\\\n\r\t/]")
_SAFE_APP_TOKEN_RE = re.compile(r"^[A-Za-z0-9._-]+$")
_DISALLOWED_SHELL_BINARIES: frozenset[str] = frozenset(
    {
        "cmd",
        "cmd.exe",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        "bash",
        "bash.exe",
        "sh",
        "sh.exe",
        "wscript",
        "wscript.exe",
        "cscript",
        "cscript.exe",
        "mshta",
        "mshta.exe",
        "rundll32",
        "rundll32.exe",
        "regsvr32",
        "regsvr32.exe",
    }
)


@dataclass(frozen=True)
class _AppSpec:
    canonical_name: str
    process_names: tuple[str, ...]
    title_substrings: tuple[str, ...]
    candidate_exes: tuple[str, ...]
    install_paths: tuple[str, ...] = ()
    launch_args: tuple[str, ...] = ()
    excluded_titles: tuple[str, ...] = ()


@dataclass(frozen=True)
class EnumeratedWindow:
    """Minimal top-level window descriptor used for focus matching."""

    hwnd: int
    title: str
    process_name: str
    is_visible: bool = True


_KNOWN_APPS: dict[str, _AppSpec] = {
    "vscode": _AppSpec(
        canonical_name="vscode",
        process_names=("code.exe",),
        title_substrings=("visual studio code",),
        candidate_exes=("Code.exe", "code.exe", "code"),
        install_paths=(
            r"%LOCALAPPDATA%\Programs\Microsoft VS Code\Code.exe",
            r"%ProgramFiles%\Microsoft VS Code\Code.exe",
        ),
    ),
    "terminal": _AppSpec(
        canonical_name="terminal",
        process_names=("windowsterminal.exe", "wt.exe"),
        title_substrings=("windows terminal", "powershell", "command prompt"),
        candidate_exes=("wt.exe", "WindowsTerminal.exe"),
        install_paths=(r"%LOCALAPPDATA%\Microsoft\WindowsApps\wt.exe",),
    ),
    "chrome": _AppSpec(
        canonical_name="chrome",
        process_names=("chrome.exe",),
        title_substrings=("google chrome",),
        candidate_exes=("chrome.exe",),
        install_paths=(
            r"%ProgramFiles%\Google\Chrome\Application\chrome.exe",
            r"%ProgramFiles(x86)%\Google\Chrome\Application\chrome.exe",
            r"%LOCALAPPDATA%\Google\Chrome\Application\chrome.exe",
        ),
    ),
    "edge": _AppSpec(
        canonical_name="edge",
        process_names=("msedge.exe",),
        title_substrings=("microsoft edge", "microsoft\u200b edge"),
        candidate_exes=("msedge.exe",),
        install_paths=(
            r"%ProgramFiles(x86)%\Microsoft\Edge\Application\msedge.exe",
            r"%ProgramFiles%\Microsoft\Edge\Application\msedge.exe",
        ),
    ),
    "explorer": _AppSpec(
        canonical_name="explorer",
        process_names=("explorer.exe",),
        title_substrings=(),
        candidate_exes=("explorer.exe",),
        install_paths=(r"%SystemRoot%\explorer.exe",),
        excluded_titles=("program manager",),
    ),
    "cursor": _AppSpec(
        canonical_name="cursor",
        process_names=("cursor.exe",),
        title_substrings=("cursor",),
        candidate_exes=("Cursor.exe", "cursor.exe"),
        install_paths=(
            r"%LOCALAPPDATA%\Programs\cursor\Cursor.exe",
            r"%LOCALAPPDATA%\Programs\Cursor\Cursor.exe",
        ),
    ),
    "antigravity": _AppSpec(
        canonical_name="antigravity",
        process_names=("antigravity.exe",),
        title_substrings=("antigravity",),
        candidate_exes=("Antigravity.exe", "antigravity.exe"),
        install_paths=(
            r"%LOCALAPPDATA%\Programs\Antigravity\Antigravity.exe",
            r"%ProgramFiles%\Antigravity\Antigravity.exe",
        ),
    ),
    "notepad": _AppSpec(
        canonical_name="notepad",
        process_names=("notepad.exe",),
        title_substrings=("notepad",),
        candidate_exes=("notepad.exe",),
        install_paths=(
            r"%SystemRoot%\System32\notepad.exe",
            r"%SystemRoot%\notepad.exe",
        ),
    ),
    "calculator": _AppSpec(
        canonical_name="calculator",
        process_names=("calculatorapp.exe", "calculator.exe", "applicationframehost.exe"),
        title_substrings=("calculator",),
        candidate_exes=("calc.exe",),
        install_paths=(r"%SystemRoot%\System32\calc.exe",),
    ),
    "settings": _AppSpec(
        canonical_name="settings",
        process_names=("systemsettings.exe", "applicationframehost.exe"),
        title_substrings=("settings",),
        candidate_exes=("explorer.exe",),
        install_paths=(r"%SystemRoot%\explorer.exe",),
        launch_args=("ms-settings:",),
    ),
    "taskmgr": _AppSpec(
        canonical_name="taskmgr",
        process_names=("taskmgr.exe",),
        title_substrings=("task manager",),
        candidate_exes=("taskmgr.exe",),
        install_paths=(r"%SystemRoot%\System32\taskmgr.exe",),
    ),
}

_APP_ALIASES: dict[str, str] = {
    "vscode": "vscode",
    "code": "vscode",
    "code.exe": "vscode",
    "vs code": "vscode",
    "visual studio code": "vscode",
    "terminal": "terminal",
    "wt": "terminal",
    "wt.exe": "terminal",
    "windows terminal": "terminal",
    "windowsterminal.exe": "terminal",
    "chrome": "chrome",
    "chrome.exe": "chrome",
    "google chrome": "chrome",
    "edge": "edge",
    "msedge": "edge",
    "msedge.exe": "edge",
    "microsoft edge": "edge",
    "explorer": "explorer",
    "explorer.exe": "explorer",
    "file explorer": "explorer",
    "cursor": "cursor",
    "cursor.exe": "cursor",
    "antigravity": "antigravity",
    "antigravity.exe": "antigravity",
    "google antigravity": "antigravity",
    "notepad": "notepad",
    "notepad.exe": "notepad",
    "calculator": "calculator",
    "calc": "calculator",
    "calc.exe": "calculator",
    "calculator.exe": "calculator",
    "settings": "settings",
    "windows settings": "settings",
    "ms-settings": "settings",
    "taskmgr": "taskmgr",
    "taskmgr.exe": "taskmgr",
    "task manager": "taskmgr",
}


# --------------------------------------------------------------------------- Win32 DLL loading


def _get_win32_user32(user32: Any = None) -> Any:
    """Return user32 ctypes DLL with explicit signatures or the injected test double."""
    if user32 is not None:
        return user32
    if sys.platform != "win32":
        raise OSError("Windows OS tools require Windows or mocked Win32 APIs.")

    import ctypes.wintypes as wintypes

    u32 = ctypes.WinDLL("user32", use_last_error=True)

    wndenumproc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    u32.EnumWindows.argtypes = [wndenumproc, wintypes.LPARAM]
    u32.EnumWindows.restype = wintypes.BOOL

    if hasattr(u32, "IsWindow"):
        u32.IsWindow.argtypes = [wintypes.HWND]
        u32.IsWindow.restype = wintypes.BOOL

    u32.IsWindowVisible.argtypes = [wintypes.HWND]
    u32.IsWindowVisible.restype = wintypes.BOOL

    u32.IsIconic.argtypes = [wintypes.HWND]
    u32.IsIconic.restype = wintypes.BOOL

    u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
    u32.GetWindowTextLengthW.restype = ctypes.c_int

    u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
    u32.GetWindowTextW.restype = ctypes.c_int

    u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
    u32.GetWindowThreadProcessId.restype = wintypes.DWORD

    u32.ShowWindow.argtypes = [wintypes.HWND, ctypes.c_int]
    u32.ShowWindow.restype = wintypes.BOOL

    u32.SetForegroundWindow.argtypes = [wintypes.HWND]
    u32.SetForegroundWindow.restype = wintypes.BOOL

    u32.SetCursorPos.argtypes = [ctypes.c_int, ctypes.c_int]
    u32.SetCursorPos.restype = wintypes.BOOL

    u32.SendInput.argtypes = [wintypes.UINT, ctypes.c_void_p, ctypes.c_int]
    u32.SendInput.restype = wintypes.UINT

    if hasattr(u32, "mouse_event"):
        u32.mouse_event.argtypes = [
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            wintypes.DWORD,
            ctypes.c_size_t,
        ]
        u32.mouse_event.restype = None

    u32.keybd_event.argtypes = [wintypes.BYTE, wintypes.BYTE, wintypes.DWORD, ctypes.c_size_t]
    u32.keybd_event.restype = None

    u32.OpenClipboard.argtypes = [wintypes.HWND]
    u32.OpenClipboard.restype = wintypes.BOOL

    u32.CloseClipboard.argtypes = []
    u32.CloseClipboard.restype = wintypes.BOOL

    u32.EmptyClipboard.argtypes = []
    u32.EmptyClipboard.restype = wintypes.BOOL

    u32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
    u32.IsClipboardFormatAvailable.restype = wintypes.BOOL

    u32.GetClipboardData.argtypes = [wintypes.UINT]
    u32.GetClipboardData.restype = ctypes.c_void_p

    u32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
    u32.SetClipboardData.restype = ctypes.c_void_p

    if hasattr(u32, "GetForegroundWindow"):
        u32.GetForegroundWindow.argtypes = []
        u32.GetForegroundWindow.restype = wintypes.HWND

    if hasattr(u32, "PostMessageW"):
        u32.PostMessageW.argtypes = [
            wintypes.HWND,
            wintypes.UINT,
            wintypes.WPARAM,
            wintypes.LPARAM,
        ]
        u32.PostMessageW.restype = wintypes.BOOL

    if hasattr(u32, "LockWorkStation"):
        u32.LockWorkStation.argtypes = []
        u32.LockWorkStation.restype = wintypes.BOOL

    if hasattr(u32, "GetAncestor"):
        u32.GetAncestor.argtypes = [wintypes.HWND, wintypes.UINT]
        u32.GetAncestor.restype = wintypes.HWND

    if hasattr(u32, "IsChild"):
        u32.IsChild.argtypes = [wintypes.HWND, wintypes.HWND]
        u32.IsChild.restype = wintypes.BOOL

    if hasattr(u32, "GetWindow"):
        u32.GetWindow.argtypes = [wintypes.HWND, wintypes.UINT]
        u32.GetWindow.restype = wintypes.HWND

    if hasattr(u32, "BringWindowToTop"):
        u32.BringWindowToTop.argtypes = [wintypes.HWND]
        u32.BringWindowToTop.restype = wintypes.BOOL

    if hasattr(u32, "MonitorFromWindow"):
        u32.MonitorFromWindow.argtypes = [wintypes.HWND, wintypes.DWORD]
        u32.MonitorFromWindow.restype = ctypes.c_void_p

    if hasattr(u32, "GetMonitorInfoW"):
        u32.GetMonitorInfoW.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        u32.GetMonitorInfoW.restype = wintypes.BOOL

    if hasattr(u32, "MoveWindow"):
        u32.MoveWindow.argtypes = [
            wintypes.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.BOOL,
        ]
        u32.MoveWindow.restype = wintypes.BOOL

    if hasattr(u32, "SetWindowPos"):
        u32.SetWindowPos.argtypes = [
            wintypes.HWND,
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.UINT,
        ]
        u32.SetWindowPos.restype = wintypes.BOOL

    if hasattr(u32, "IsZoomed"):
        u32.IsZoomed.argtypes = [wintypes.HWND]
        u32.IsZoomed.restype = wintypes.BOOL

    if hasattr(u32, "GetWindowRect"):
        u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.c_void_p]
        u32.GetWindowRect.restype = wintypes.BOOL

    if hasattr(u32, "AttachThreadInput"):
        u32.AttachThreadInput.argtypes = [wintypes.DWORD, wintypes.DWORD, wintypes.BOOL]
        u32.AttachThreadInput.restype = wintypes.BOOL

    if hasattr(u32, "AllowSetForegroundWindow"):
        u32.AllowSetForegroundWindow.argtypes = [wintypes.DWORD]
        u32.AllowSetForegroundWindow.restype = wintypes.BOOL

    if hasattr(u32, "GetSystemMetrics"):
        u32.GetSystemMetrics.argtypes = [ctypes.c_int]
        u32.GetSystemMetrics.restype = ctypes.c_int

    with contextlib.suppress(Exception):
        if hasattr(u32, "SetProcessDpiAwarenessContext"):
            u32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            u32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
            u32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))

    return u32


def _get_win32_kernel32(kernel32: Any = None) -> Any:
    """Return kernel32 ctypes DLL with explicit signatures or the injected test double."""
    if kernel32 is not None:
        return kernel32
    if sys.platform != "win32":
        raise OSError("Windows OS tools require Windows or mocked Win32 APIs.")

    import ctypes.wintypes as wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

    if hasattr(k32, "GetCurrentThreadId"):
        k32.GetCurrentThreadId.argtypes = []
        k32.GetCurrentThreadId.restype = wintypes.DWORD

    k32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    k32.OpenProcess.restype = wintypes.HANDLE

    k32.CloseHandle.argtypes = [wintypes.HANDLE]
    k32.CloseHandle.restype = wintypes.BOOL

    if hasattr(k32, "QueryFullProcessImageNameW"):
        k32.QueryFullProcessImageNameW.argtypes = [
            wintypes.HANDLE,
            wintypes.DWORD,
            wintypes.LPWSTR,
            ctypes.POINTER(wintypes.DWORD),
        ]
        k32.QueryFullProcessImageNameW.restype = wintypes.BOOL

    k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
    k32.GlobalAlloc.restype = ctypes.c_void_p

    k32.GlobalLock.argtypes = [ctypes.c_void_p]
    k32.GlobalLock.restype = ctypes.c_void_p

    k32.GlobalUnlock.argtypes = [ctypes.c_void_p]
    k32.GlobalUnlock.restype = wintypes.BOOL

    k32.GlobalFree.argtypes = [ctypes.c_void_p]
    k32.GlobalFree.restype = ctypes.c_void_p

    return k32


def _get_current_desktop_name(
    *,
    user32: Any = None,
    kernel32: Any = None,
) -> str | None:
    """Return the active Win32 thread desktop name (e.g. 'Default' or 'exebox-...')."""
    if sys.platform != "win32" and user32 is None and kernel32 is None:
        return None
    try:
        u32 = _get_win32_user32(user32)
        k32 = _get_win32_kernel32(kernel32)

        if isinstance(k32, ctypes.CDLL):
            k32.GetCurrentThreadId.argtypes = []
            k32.GetCurrentThreadId.restype = ctypes.c_uint32

        if isinstance(u32, ctypes.CDLL):
            u32.GetThreadDesktop.argtypes = [ctypes.c_uint32]
            u32.GetThreadDesktop.restype = ctypes.c_void_p
            u32.GetUserObjectInformationW.argtypes = [
                ctypes.c_void_p,
                ctypes.c_int,
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.POINTER(ctypes.c_uint32),
            ]
            u32.GetUserObjectInformationW.restype = ctypes.c_int

        tid = k32.GetCurrentThreadId()
        hdesk = u32.GetThreadDesktop(tid)
        if not hdesk:
            return None
        buf = ctypes.create_unicode_buffer(256)
        needed = ctypes.c_uint32(0)
        ok = u32.GetUserObjectInformationW(
            hdesk,
            UOI_NAME,
            buf,
            ctypes.sizeof(buf),
            ctypes.byref(needed),
        )
        if not ok:
            return None
        name = buf.value.strip()
        return name or None
    except Exception:
        return None


def _check_interactive_desktop(
    desktop_name_fn: Callable[[], str | None] | None = None,
) -> str | None:
    """Return the non-default desktop name if isolated from WinSta0\\Default, else None."""
    fn = desktop_name_fn or _get_current_desktop_name
    name = fn()
    if isinstance(name, str) and name.strip() and name.strip().lower() != "default":
        return name.strip()
    return None


def _build_gui_popen_kwargs() -> dict[str, Any]:
    """Build subprocess.Popen kwargs that ensure visible normal-window GUI launches."""
    kwargs: dict[str, Any] = {
        "shell": False,
        "stdout": subprocess.DEVNULL,
        "stderr": subprocess.DEVNULL,
    }
    if sys.platform == "win32" and hasattr(subprocess, "STARTUPINFO"):
        si = subprocess.STARTUPINFO()
        si.dwFlags |= _STARTF_USESHOWWINDOW
        si.wShowWindow = SW_SHOWNORMAL
        kwargs["startupinfo"] = si
    return kwargs


def _open_via_shell(
    target: str,
    *,
    startfile_fn: Callable[[str], Any] | None = None,
    popen_fn: Callable[..., Any] = subprocess.Popen,
) -> None:
    """Open a local file or URL visibly via the Windows shell."""
    if startfile_fn is not None:
        startfile_fn(target)
        return
    if hasattr(os, "startfile"):
        os.startfile(target)
        return
    if sys.platform == "win32":
        shell32 = ctypes.WinDLL("shell32", use_last_error=True)
        shell32.ShellExecuteW.argtypes = [
            ctypes.c_void_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_wchar_p,
            ctypes.c_int,
        ]
        shell32.ShellExecuteW.restype = ctypes.c_void_p
        res = shell32.ShellExecuteW(None, "open", target, None, None, SW_SHOWNORMAL)
        if int(res or 0) <= 32:
            raise OSError(f"ShellExecuteW failed opening '{target}' (code={int(res or 0)}).")
        return
    popen_fn(["explorer.exe", target], **_build_gui_popen_kwargs())


def _non_interactive_desktop_result(
    app_label: str,
    non_default: str,
    resolved_target: Path | None = None,
) -> dict[str, Any]:
    """Return a structured non_interactive_desktop response for sandboxed daemons."""
    return {
        "ok": False,
        "status": "non_interactive_desktop",
        "app": app_label,
        "window_title": None,
        "hwnd": None,
        "executable": None,
        "target_path": str(resolved_target) if resolved_target is not None else None,
        "error": (
            f"Cannot launch or focus '{app_label}' visibly because the daemon is running on "
            f"isolated desktop '{non_default}' instead of 'WinSta0\\Default'. Start the Clea "
            "daemon from an interactive Windows Terminal / PowerShell session."
        ),
    }


# --------------------------------------------------------------------------- App Launcher / Focuser


def _expand_env_vars(template: str, env: dict[str, str]) -> str:
    def _replace(match: re.Match[str]) -> str:
        key = match.group(1)
        for env_key, env_val in env.items():
            if env_key.lower() == key.lower():
                return env_val
        return match.group(0)

    return re.sub(r"%([^%]+)%", _replace, template)


def _search_standard_install_dirs(exe_names: tuple[str, ...], env: dict[str, str]) -> str | None:
    base_roots = [
        _expand_env_vars(r"%LOCALAPPDATA%\Programs", env),
        _expand_env_vars(r"%ProgramFiles%", env),
        _expand_env_vars(r"%ProgramFiles(x86)%", env),
    ]
    exe_lower = {name.lower() for name in exe_names}
    for root_str in base_roots:
        if "%" in root_str or not root_str:
            continue
        root = Path(root_str)
        if not root.is_dir():
            continue
        try:
            for child in root.iterdir():
                if child.is_file() and child.name.lower() in exe_lower:
                    return str(child)
                if child.is_dir():
                    for candidate in exe_names:
                        nested = child / candidate
                        if nested.is_file():
                            return str(nested)
        except OSError:
            continue
    return None


def _resolve_spec_and_command(
    app_name: str,
    *,
    which_fn: Callable[[str], str | None] = shutil.which,
    env: dict[str, str] | None = None,
    require_executable: bool = True,
) -> tuple[_AppSpec, list[str] | None]:
    """Validate an application name/alias and optionally resolve its launch command."""
    if not isinstance(app_name, str):
        raise ValueError("app_name must be a non-empty string.")
    stripped = app_name.strip()
    if not stripped:
        raise ValueError("app_name must not be empty.")
    if ".." in stripped or _SHELL_METACHAR_RE.search(stripped):
        raise ValueError(f"Invalid characters in application name: {app_name!r}")

    normalized = " ".join(stripped.lower().split())
    if (
        normalized in _DISALLOWED_SHELL_BINARIES
        or f"{normalized}.exe" in _DISALLOWED_SHELL_BINARIES
    ):
        raise ValueError(f"Application '{app_name}' is disallowed for security reasons.")

    env_map = dict(os.environ) if env is None else env

    canonical = _APP_ALIASES.get(normalized)
    if canonical is not None:
        spec = _KNOWN_APPS[canonical]
    else:
        if not _SAFE_APP_TOKEN_RE.fullmatch(stripped):
            raise ValueError(f"Unsupported or malformed application name: {app_name!r}")
        stem = normalized[:-4] if normalized.endswith(".exe") else normalized
        exe_candidates = (
            (stripped,) if normalized.endswith(".exe") else (stripped, f"{stripped}.exe")
        )
        spec = _AppSpec(
            canonical_name=stem,
            process_names=(f"{stem}.exe",),
            title_substrings=(stem,),
            candidate_exes=exe_candidates,
        )

    if not require_executable:
        return spec, None

    resolved_exe: str | None = None
    for cand in spec.candidate_exes:
        found = which_fn(cand)
        if found:
            resolved_exe = found
            break

    if resolved_exe is None:
        for template in spec.install_paths:
            expanded = _expand_env_vars(template, env_map)
            if "%" not in expanded and Path(expanded).is_file():
                resolved_exe = expanded
                break

    if resolved_exe is None and canonical is None:
        resolved_exe = _search_standard_install_dirs(spec.candidate_exes, env_map)

    if resolved_exe is None:
        raise ValueError(
            f"Application '{app_name}' could not be resolved to an allowed executable."
        )

    return spec, [resolved_exe, *spec.launch_args]


def resolve_app_spec(
    app_name: str,
    *,
    which_fn: Callable[[str], str | None] = shutil.which,
    env: dict[str, str] | None = None,
) -> tuple[str, list[str]]:
    """Validate and resolve *app_name* to ``(canonical_name, command_argv)``."""
    spec, cmd = _resolve_spec_and_command(
        app_name,
        which_fn=which_fn,
        env=env,
        require_executable=True,
    )
    assert cmd is not None
    return spec.canonical_name, cmd


def _get_win32_dwmapi(dwmapi: Any = None) -> Any:
    """Return cached dwmapi ctypes DLL with explicit DwmGetWindowAttribute signature."""
    global _WINDOWS_OS_DWMAPI
    if dwmapi is not None:
        return dwmapi
    if _WINDOWS_OS_DWMAPI is not None:
        return _WINDOWS_OS_DWMAPI
    if sys.platform != "win32":
        return None
    with contextlib.suppress(Exception):
        loaded = ctypes.WinDLL("dwmapi", use_last_error=True)
        if hasattr(loaded, "DwmGetWindowAttribute"):
            loaded.DwmGetWindowAttribute.argtypes = [
                ctypes.c_void_p,
                ctypes.c_uint32,
                ctypes.c_void_p,
                ctypes.c_uint32,
            ]
            loaded.DwmGetWindowAttribute.restype = ctypes.c_long
        _WINDOWS_OS_DWMAPI = loaded
        return _WINDOWS_OS_DWMAPI
    return None


def _is_window_cloaked_ctypes(hwnd: int, user32: Any) -> bool:
    """Return True when running on live Win32 and DWM reports the window is cloaked."""
    if sys.platform != "win32" or not isinstance(user32, ctypes.CDLL):
        return False
    try:
        dwmapi = _get_win32_dwmapi()
        if dwmapi is None or not hasattr(dwmapi, "DwmGetWindowAttribute"):
            return False
        cloaked = ctypes.c_uint32(0)
        hr = dwmapi.DwmGetWindowAttribute(
            ctypes.c_void_p(hwnd),
            DWMWA_CLOAKED,
            ctypes.byref(cloaked),
            ctypes.sizeof(cloaked),
        )
        return hr == 0 and bool(cloaked.value)
    except Exception:
        return False


def _resolve_root_window_hwnd(hwnd: int, user32: Any) -> int:
    """Resolve top-level GA_ROOT HWND when running against Win32 user32."""
    if not hwnd or user32 is None or not hasattr(user32, "GetAncestor"):
        return int(hwnd)
    try:
        root = user32.GetAncestor(int(hwnd), GA_ROOT)
        if root and (not hasattr(user32, "IsWindowVisible") or bool(user32.IsWindowVisible(root))):
            return int(root)
    except Exception:
        pass
    return int(hwnd)


_BROWSER_WINDOW_TITLE_SUFFIXES: tuple[str, ...] = (
    " - google chrome",
    " - microsoft edge",
    " - microsoft\u200b edge",
    " - brave",
    " - mozilla firefox",
    " - arc",
)

_BROWSER_CANONICAL_NAMES: frozenset[str] = frozenset({"chrome", "edge", "firefox", "brave", "arc"})


def _is_browser_tab_window_title(title: str | None) -> bool:
    low = (title or "").strip().lower()
    return any(low.endswith(suffix) for suffix in _BROWSER_WINDOW_TITLE_SUFFIXES)


def _pick_best_matching_window(
    matches: list[EnumeratedWindow],
    user32: Any,
    *,
    prefer_standalone_pwa: bool = False,
    prefer_browser_window: bool = False,
) -> EnumeratedWindow:
    """Prefer standalone PWA (when requested), foreground, non-minimized, unowned windows."""
    if len(matches) == 1:
        return matches[0]
    fg_hwnd = 0
    if user32 is not None and hasattr(user32, "GetForegroundWindow"):
        with contextlib.suppress(Exception):
            fg = user32.GetForegroundWindow()
            fg_hwnd = int(fg) if fg else 0

    def _score(win: EnumeratedWindow) -> tuple[int, int, int, int, int, int]:
        fg_root = _resolve_root_window_hwnd(fg_hwnd, user32) if fg_hwnd else 0
        is_fg = 1 if (fg_hwnd and (win.hwnd == fg_hwnd or win.hwnd == fg_root)) else 0
        uncloaked = 0 if _is_window_cloaked_ctypes(win.hwnd, user32) else 1
        is_not_min = 1
        if user32 is not None and hasattr(user32, "IsIconic"):
            with contextlib.suppress(Exception):
                is_not_min = 0 if bool(user32.IsIconic(win.hwnd)) else 1
        unowned = 1
        if user32 is not None and hasattr(user32, "GetWindow"):
            with contextlib.suppress(Exception):
                unowned = 0 if user32.GetWindow(win.hwnd, GW_OWNER) else 1
        is_browser_tab = _is_browser_tab_window_title(win.title)
        if prefer_standalone_pwa:
            pwa_priority = 0 if is_browser_tab else 1
            # Shorter standalone title beats noisy tab titles
            title_tiebreak = -len(win.title or "")
        elif prefer_browser_window:
            pwa_priority = 1 if is_browser_tab else 0
            title_tiebreak = len(win.title or "")
        else:
            pwa_priority = 0
            title_tiebreak = len(win.title or "")
        return (pwa_priority, is_fg, uncloaked, is_not_min, unowned, title_tiebreak)

    return max(matches, key=_score)


def _is_same_window_tree_os(u32: Any, expected_hwnd: int, actual_hwnd: int) -> bool:
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
            exp_root = u32.GetAncestor(int(expected_hwnd), GA_ROOT)
            act_root = u32.GetAncestor(int(actual_hwnd), GA_ROOT)
            if exp_root and act_root and int(exp_root) == int(act_root):
                return True
    return False


def _focus_window_hwnd(
    u32: Any,
    hwnd: int,
    *,
    kernel32: Any = None,
    restore_if_iconic: bool = True,
    attach_fallback: bool | None = None,
) -> None:
    """Restore if minimized and bring *hwnd* to foreground with cur_tid<->fg_tid attach fallback."""
    if restore_if_iconic and hasattr(u32, "IsIconic") and bool(u32.IsIconic(hwnd)):
        u32.ShowWindow(hwnd, SW_RESTORE)
    fg_ok = bool(u32.SetForegroundWindow(hwnd))
    use_fallback = (
        isinstance(u32, ctypes.CDLL) if attach_fallback is None else bool(attach_fallback)
    )
    if not use_fallback:
        return
    with contextlib.suppress(Exception):
        fg_after = u32.GetForegroundWindow() if hasattr(u32, "GetForegroundWindow") else 0
        if fg_ok and fg_after and _is_same_window_tree_os(u32, hwnd, int(fg_after)):
            return
        k32: Any = None
        with contextlib.suppress(Exception):
            k32 = _get_win32_kernel32(kernel32)
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


def _enumerate_windows_ctypes(user32: Any, kernel32: Any) -> list[EnumeratedWindow]:
    """Enumerate visible top-level windows using Win32 ctypes."""
    import ctypes.wintypes as wintypes

    windows: list[EnumeratedWindow] = []

    def _get_process_name(hwnd: int) -> str:
        pid = wintypes.DWORD()
        if hasattr(user32, "GetWindowThreadProcessId"):
            user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
        if not pid.value or kernel32 is None or not hasattr(kernel32, "OpenProcess"):
            return "<unknown>"
        h_proc = kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
        if not h_proc:
            return "<unknown>"
        try:
            if hasattr(kernel32, "QueryFullProcessImageNameW"):
                buf = ctypes.create_unicode_buffer(1024)
                size = wintypes.DWORD(1024)
                if kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
                    return Path(buf.value).name
            return "<unknown>"
        finally:
            with contextlib.suppress(Exception):
                kernel32.CloseHandle(h_proc)

    def _on_window(hwnd: int, _lparam: int) -> bool:
        is_vis = bool(user32.IsWindowVisible(hwnd))
        is_iconic = bool(hasattr(user32, "IsIconic") and user32.IsIconic(hwnd))
        if is_vis or is_iconic:
            if _is_window_cloaked_ctypes(int(hwnd), user32) and not is_iconic:
                return True
            length = int(user32.GetWindowTextLengthW(hwnd))
            title = ""
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                copied = int(user32.GetWindowTextW(hwnd, buf, length + 1))
                title = buf.value if copied > 0 else ""
            proc_name = _get_process_name(hwnd)
            windows.append(
                EnumeratedWindow(
                    hwnd=int(hwnd),
                    title=title,
                    process_name=proc_name,
                    is_visible=True,
                )
            )
        return True

    wndenumproc = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    cb = wndenumproc(_on_window)
    user32.EnumWindows(cb, 0)
    return windows


def _window_matches_spec(
    window: EnumeratedWindow,
    spec: _AppSpec,
    user32: Any = None,
) -> bool:
    is_iconic = bool(
        user32 is not None and hasattr(user32, "IsIconic") and user32.IsIconic(window.hwnd)
    )
    if not window.is_visible and not is_iconic:
        return False
    title = (window.title or "").strip()
    if not title:
        return False
    title_lower = title.lower()
    if title_lower in spec.excluded_titles:
        return False

    proc_lower = (window.process_name or "").strip().lower()
    if proc_lower and proc_lower != "<unknown>":
        if proc_lower == "applicationframehost.exe":
            return proc_lower in spec.process_names and any(
                sub in title_lower for sub in spec.title_substrings
            )
        if spec.canonical_name in {"chrome", "edge"}:
            # Distinguish normal Chromium browser windows ("... - Google Chrome" / "... - Edge")
            # from standalone Chromium PWAs (e.g. YouTube PWA) sharing chrome.exe / msedge.exe.
            return proc_lower in spec.process_names and any(
                sub in title_lower for sub in spec.title_substrings
            )
        if spec.canonical_name == "explorer":
            return proc_lower in spec.process_names
        return proc_lower in spec.process_names

    if spec.title_substrings:
        return any(sub in title_lower for sub in spec.title_substrings)
    return False


def _validate_target_path(
    target_path: str | None,
    env: dict[str, str] | None = None,
) -> Path | None:
    if target_path is None:
        return None
    if not isinstance(target_path, str):
        raise ValueError("target_path must be a string path.")
    stripped = target_path.strip()
    if not stripped:
        return None
    if any(ord(ch) < 32 for ch in stripped):
        raise ValueError("target_path contains invalid control characters.")

    env_map = dict(os.environ) if env is None else env
    expanded = _expand_env_vars(stripped, env_map)
    cand_path = Path(expanded).expanduser()
    resolved = cand_path.resolve()
    if not resolved.exists() and not cand_path.is_absolute():
        home = Path.home()
        for base in (home / "Desktop", home / "Documents", home):
            alt = (base / cand_path).resolve()
            if alt.exists():
                return alt
    if not resolved.exists():
        raise ValueError(f"target_path does not exist: {target_path!r}")
    return resolved


_INSTALLED_APPS_CACHE: list[dict[str, str]] | None = None
_INSTALLED_APPS_CACHE_TIME: float = 0.0
_INSTALLED_APPS_FAILURE_UNTIL: float = 0.0
_INSTALLED_APPS_CACHE_LOCK = threading.Lock()
_APP_CACHE_TTL: float = 300.0  # 5 minutes
_APP_CACHE_MISS_REFRESH_INTERVAL: float = 10.0  # throttle live PowerShell miss refreshes
_APP_CACHE_FAILURE_TTL: float = 45.0  # negative/failure backoff to avoid repeated 10s stalls


def _is_disallowed_shell_app(name: str, appid: str) -> bool:
    """Return True if an application is a disallowed shell binary or script interpreter."""
    low_name = name.strip().lower()
    low_appid = appid.strip().lower()

    # If AppID contains a file path, inspect only the executable filename and stem
    if "\\" in low_appid or "/" in low_appid:
        raw_target = low_appid.strip('"')
        exe_name = Path(raw_target).name.lower()
        if (
            exe_name in _DISALLOWED_SHELL_BINARIES
            or Path(exe_name).stem.lower() in _DISALLOWED_SHELL_BINARIES
        ):
            return True
    else:
        if low_appid in _DISALLOWED_SHELL_BINARIES:
            return True

    # Check whole-word / isolated token matches on app name (e.g. 'cmd', 'powershell', 'bash')
    for dis in _DISALLOWED_SHELL_BINARIES:
        pattern = rf"\b{re.escape(dis)}(\.exe)?\b"
        if re.search(pattern, low_name):
            return True

    return False


def invalidate_installed_apps_cache() -> None:
    """Clear the cached StartApps index so newly installed/uninstalled apps are picked up."""
    global _INSTALLED_APPS_CACHE, _INSTALLED_APPS_CACHE_TIME, _INSTALLED_APPS_FAILURE_UNTIL
    with _INSTALLED_APPS_CACHE_LOCK:
        _INSTALLED_APPS_CACHE = None
        _INSTALLED_APPS_CACHE_TIME = 0.0
        _INSTALLED_APPS_FAILURE_UNTIL = 0.0


def get_installed_apps(
    *,
    force_refresh: bool = False,
    app_catalog_fetcher: Callable[[], list[dict[str, str]]] | None = None,
) -> list[dict[str, str]]:
    """Retrieve catalog of installed Start Menu / UWP apps via Get-StartApps with caching."""
    global _INSTALLED_APPS_CACHE, _INSTALLED_APPS_CACHE_TIME, _INSTALLED_APPS_FAILURE_UNTIL
    now = time.monotonic()
    with _INSTALLED_APPS_CACHE_LOCK:
        if (
            not force_refresh
            and _INSTALLED_APPS_CACHE is not None
            and (now - _INSTALLED_APPS_CACHE_TIME) < _APP_CACHE_TTL
        ):
            return list(_INSTALLED_APPS_CACHE)
        if (
            not force_refresh
            and app_catalog_fetcher is None
            and _INSTALLED_APPS_CACHE is None
            and now < _INSTALLED_APPS_FAILURE_UNTIL
        ):
            return []

        if app_catalog_fetcher is not None:
            raw_apps = app_catalog_fetcher()
        else:
            ps_exe = shutil.which("powershell.exe")
            if not ps_exe:
                _INSTALLED_APPS_FAILURE_UNTIL = now + _APP_CACHE_FAILURE_TTL
                return []
            try:
                proc = run_powershell_hidden(
                    "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress",
                    timeout=10.0,
                    ps_exe=ps_exe,
                    check=False,
                )
                if proc.returncode != 0 or not (proc.stdout or "").strip():
                    _INSTALLED_APPS_FAILURE_UNTIL = time.monotonic() + _APP_CACHE_FAILURE_TTL
                    return []
                parsed = json.loads(proc.stdout.strip())
                raw_apps = parsed if isinstance(parsed, list) else [parsed]
            except Exception as exc:
                logger.debug("Failed to retrieve StartApps: %s", exc)
                _INSTALLED_APPS_FAILURE_UNTIL = time.monotonic() + _APP_CACHE_FAILURE_TTL
                return []

        cleaned: list[dict[str, str]] = []
        for item in raw_apps:
            if isinstance(item, dict) and "Name" in item and "AppID" in item:
                name = str(item["Name"]).strip()
                appid = str(item["AppID"]).strip()
                if name and appid:
                    if _is_disallowed_shell_app(name, appid):
                        continue
                    cleaned.append({"Name": name, "AppID": appid})

        _INSTALLED_APPS_CACHE = cleaned
        _INSTALLED_APPS_CACHE_TIME = time.monotonic()
        _INSTALLED_APPS_FAILURE_UNTIL = 0.0
        return list(cleaned)


def find_matching_start_app(
    query: str,
    catalog: list[dict[str, str]],
) -> dict[str, str] | None:
    """Fuzzy match an application name against the StartApps catalog."""
    q = query.strip().lower()
    if not q:
        return None

    for entry in catalog:
        if entry["Name"].strip().lower() == q:
            return entry

    for entry in catalog:
        if entry["AppID"].strip().lower() == q:
            return entry

    for entry in catalog:
        name_lower = entry["Name"].strip().lower()
        words = re.split(r"[\s._-]+", name_lower)
        if q in words or name_lower.startswith(q):
            return entry

    for entry in catalog:
        if q in entry["Name"].strip().lower():
            return entry

    best_match: dict[str, str] | None = None
    best_ratio = 0.0
    for entry in catalog:
        name_lower = entry["Name"].strip().lower()
        ratio = difflib.SequenceMatcher(None, q, name_lower).ratio()
        if ratio > best_ratio and ratio >= 0.6:
            best_ratio = ratio
            best_match = entry

    return best_match


_EXPLICIT_BROWSER_REQUEST_RE = re.compile(
    r"\b(?:in|using|via|with|on)\s+(?:the\s+)?(?:google\s+chrome|chrome|microsoft\s+edge|"
    r"edge|msedge|firefox|mozilla\s+firefox|brave|arc|web\s+browser|default\s+browser|"
    r"browser|a\s+browser(?:\s+tab)?)\b"
    r"|\b(?:web|browser)\s+version\b"
    r"|\b(?:in|as)\s+a\s+(?:browser\s+)?tab\b"
    r"|\bon\s+the\s+web\b",
    re.IGNORECASE,
)

_BROWSER_DIRECTIVE_SUFFIX_RE = re.compile(
    r"\s+(?:(?:in|using|via|with|on)\s+(?:the\s+)?(?:google\s+chrome|chrome|microsoft\s+edge|"
    r"edge|msedge|firefox|mozilla\s+firefox|brave|arc|web\s+browser|default\s+browser|"
    r"browser|a\s+browser(?:\s+tab)?)|(?:web|browser)\s+version|on\s+the\s+web)\s*$",
    re.IGNORECASE,
)

_LEADING_OPEN_VERB_RE = re.compile(r"^(?:open|launch|start|focus|run)\s+", re.IGNORECASE)

_LOCAL_FILE_EXTENSIONS: frozenset[str] = frozenset(
    {
        ".exe",
        ".txt",
        ".md",
        ".json",
        ".py",
        ".log",
        ".png",
        ".jpg",
        ".jpeg",
        ".webp",
        ".bmp",
        ".pdf",
        ".csv",
        ".yaml",
        ".yml",
    }
)

_INSTALLED_APP_ROUTED_GUIDANCE: str = (
    "Installed app is focused. Continue the task inside this single app window using UIA "
    "(ui_snapshot/ui_act), in-app keyboard shortcuts, or targeted screen inspection. "
    "Do NOT call open_url again."
)

_CDP_PWA_NAVIGATED_GUIDANCE: str = (
    "Installed PWA navigated directly via CDP inside its single app window. "
    "Verify the returned URL/title, use ui_snapshot()/ui_act() with CDP refs (b1..bN) "
    "for in-page actions, or call open_url(url) without browser=... for further in-PWA "
    "deep-link navigation."
)

_STRIPPED_HOST_SUBDOMAINS: frozenset[str] = frozenset(
    {"www", "open", "web", "app", "m", "mobile", "mail", "play", "desktop", "portal"}
)

_NON_APP_FALLBACK_SUBDOMAINS: frozenset[str] = frozenset(
    {
        "docs",
        "doc",
        "developer",
        "developers",
        "dev",
        "api",
        "status",
        "support",
        "help",
        "blog",
        "about",
        "careers",
        "jobs",
        "legal",
        "privacy",
        "accounts",
        "account",
        "auth",
        "login",
        "id",
        "billing",
    }
)

_GENERIC_SLD_LABELS: frozenset[str] = frozenset({"co", "com", "org", "net", "edu", "gov", "ac"})

_GENERIC_APP_QUALIFIERS: tuple[str, ...] = ("app", "pwa", "music", "messenger", "web")

_BROWSER_APP_NAME_MARKERS: frozenset[str] = frozenset(
    {
        "chrome",
        "google chrome",
        "edge",
        "microsoft edge",
        "msedge",
        "firefox",
        "mozilla firefox",
        "brave",
        "opera",
        "opera gx",
        "opera gx browser",
        "arc",
        "safari",
    }
)

_SAFE_URL_SCHEME_RE = re.compile(r"^[a-z0-9][a-z0-9+.-]*$")

_RESERVED_URL_SCHEMES: frozenset[str] = frozenset(
    {
        "http",
        "https",
        "mailto",
        "file",
        "ftp",
        "ftps",
        "ws",
        "wss",
        "javascript",
        "data",
        "vbscript",
        "cmd",
        "powershell",
        "ms-settings",
        "shell",
        "about",
        "chrome",
        "edge",
        "microsoft-edge",
        "firefox",
        "brave",
        "opera",
        "safari",
    }
)


@dataclass(frozen=True)
class UrlAppResolution:
    """Dynamic 3-layer OS app resolution outcome for an http/https URL."""

    app_name: str
    resolution_layer: str  # "app_uri_handler" | "url_protocol" | "installed_app"
    app_id: str | None = None
    protocol_scheme: str | None = None
    window: EnumeratedWindow | None = None
    package_family_name: str | None = None
    candidate_tokens: tuple[str, ...] = ()
    from_cached_catalog: bool = False


def _is_browser_app_name(name: str | None) -> bool:
    low = " ".join((name or "").strip().lower().split())
    return low in _BROWSER_APP_NAME_MARKERS


def extract_url_app_tokens(url: str) -> list[str]:
    """Dynamically extract ordered app/brand/protocol candidate tokens from any URL host.

    Zero hardcoded domain-to-app dictionaries: strips generic subdomains (www, open, web,
    app, m, mail, etc.) and public TLD suffixes purely structurally so arbitrary newly
    installed apps resolve automatically.
    """
    if not isinstance(url, str) or not url.strip():
        return []
    candidate = url.strip()
    if "://" not in candidate:
        candidate = f"https://{candidate}"
    try:
        parsed = urllib.parse.urlsplit(candidate)
        host = (parsed.hostname or "").lower().strip(".")
    except Exception:
        return []
    if not host or host == "localhost":
        return []
    # Ignore numeric IPv4/IPv6 addresses
    if re.fullmatch(r"[0-9.]+", host) or ":" in host:
        return []

    labels = [lbl for lbl in host.split(".") if lbl]
    if not labels:
        return []

    # Strip trailing public TLD / ccSLD suffixes
    if (
        len(labels) >= 3
        and len(labels[-1]) == 2
        and labels[-2] in _GENERIC_SLD_LABELS
    ):
        labels = labels[:-2]
    elif len(labels) >= 2:
        labels = labels[:-1]

    # Strip leading generic web-host subdomains (www, open, web, app, m, mail, ...)
    while len(labels) > 1 and labels[0] in _STRIPPED_HOST_SUBDOMAINS:
        labels.pop(0)

    if not labels:
        return []

    tokens: list[str] = []

    def _add_token(tok: str) -> None:
        cleaned = " ".join(tok.strip().lower().split())
        if (
            len(cleaned) >= 2
            and cleaned not in _BROWSER_APP_NAME_MARKERS
            and cleaned not in tokens
        ):
            tokens.append(cleaned)

    brand = labels[-1]
    brand_spaced = brand.replace("-", " ") if "-" in brand else ""
    if len(labels) >= 2:
        sub = labels[-2]
        _add_token(f"{brand}-{sub}")
        _add_token(f"{brand} {sub}")
        _add_token(f"{sub}-{brand}")
        _add_token(f"{sub} {brand}")
        if sub not in _NON_APP_FALLBACK_SUBDOMAINS:
            _add_token(brand)
            if brand_spaced:
                _add_token(brand_spaced)
    else:
        _add_token(brand)
        if brand_spaced:
            _add_token(brand_spaced)

    return tokens


def _find_app_uri_handlers(url: str) -> list[dict[str, str]]:
    """Layer 1: Query Windows WinRT Launcher.FindAppUriHandlersAsync(uri) if available."""
    if sys.platform != "win32":
        return []
    try:
        from winrt.windows.foundation import Uri
        from winrt.windows.system import Launcher
    except ImportError:
        return []

    def _invoke_winrt_sync() -> Any:
        uri = Uri(url)
        op = Launcher.find_app_uri_handlers_async(uri)
        if inspect.isawaitable(op):
            import asyncio

            return asyncio.run(op)
        if hasattr(op, "get_results"):
            return op.get_results()
        return op

    try:
        import asyncio

        try:
            asyncio.get_running_loop()
            has_running_loop = True
        except RuntimeError:
            has_running_loop = False

        if not has_running_loop:
            handlers_raw = _invoke_winrt_sync()
        else:
            # Create AND await WinRT Uri/IAsyncOperation inside the dedicated worker thread
            # so loop-bound/COM-STA objects never cross event loops or threads.
            result_box: list[Any] = []
            err_box: list[BaseException] = []

            def _runner() -> None:
                try:
                    result_box.append(_invoke_winrt_sync())
                except BaseException as exc:  # pragma: no cover - defensive
                    err_box.append(exc)

            t = threading.Thread(target=_runner, daemon=True)
            t.start()
            t.join(timeout=3.0)
            if err_box:
                return []
            handlers_raw = result_box[0] if result_box else []
    except Exception as exc:
        logger.debug("WinRT FindAppUriHandlersAsync failed for %s: %s", url, exc)
        return []

    results: list[dict[str, str]] = []
    for item in handlers_raw or ():
        try:
            display_info = getattr(item, "display_info", None)
            name = str(getattr(display_info, "display_name", "") or "").strip()
            aumid = str(getattr(item, "app_user_model_id", "") or "").strip()
            pfn = str(getattr(item, "package_family_name", "") or "").strip()
            if not name:
                name = aumid or pfn
            if not name or _is_browser_app_name(name):
                continue
            results.append(
                {
                    "name": name,
                    "app_id": aumid,
                    "package_family_name": pfn,
                }
            )
        except Exception:
            continue
    return results


def _clean_protocol_friendly_name(raw: Any, fallback: str) -> str:
    if not isinstance(raw, str) or not raw.strip():
        return fallback
    text = raw.strip()
    text = re.sub(r"^url\s*:\s*", "", text, flags=re.IGNORECASE).strip()
    text = re.sub(r"\s+protocol$", "", text, flags=re.IGNORECASE).strip()
    return text or fallback


def _extract_exe_stem_from_command(raw_cmd: Any) -> str:
    if not isinstance(raw_cmd, str) or not raw_cmd.strip():
        return ""
    stripped = raw_cmd.strip()
    if stripped.startswith('"'):
        end = stripped.find('"', 1)
        exe_part = stripped[1:end] if end > 1 else stripped.strip('"')
    else:
        exe_part = stripped.split()[0]
    stem = Path(exe_part).stem.strip()
    return stem


def is_registered_url_protocol(
    scheme: str,
    *,
    registry_lookup: Callable[[str], bool] | None = None,
) -> bool:
    """Return True if *scheme* is a safe, OS-registered Windows URL Protocol.

    Enforces ``_DANGEROUS_URL_SCHEMES`` unconditionally (even when ``registry_lookup`` is
    injected) and checks ``HKCR\\<scheme>\\URL Protocol`` (plus
    ``HKCU\\Software\\Classes\\<scheme>\\URL Protocol``) via ``winreg`` on Windows.
    """
    if not isinstance(scheme, str):
        return False
    clean = scheme.strip().lower().rstrip(":")
    if (
        not clean
        or len(clean) < 2
        or not re.fullmatch(r"[a-z][a-z0-9+.-]*", clean)
        or clean in _DANGEROUS_URL_SCHEMES
        or clean in _DISALLOWED_SHELL_BINARIES
        or clean in {"http", "https", "ms-settings", "ws", "wss", "about", "chrome", "edge"}
    ):
        return False

    if registry_lookup is not None:
        return bool(registry_lookup(clean))

    if sys.platform != "win32":
        return False

    try:
        import winreg
    except ImportError:
        return False

    roots = (
        (winreg.HKEY_CLASSES_ROOT, clean),
        (winreg.HKEY_CURRENT_USER, f"Software\\Classes\\{clean}"),
    )
    for root_hive, subkey in roots:
        try:
            with winreg.OpenKey(root_hive, subkey) as key:
                try:
                    winreg.QueryValueEx(key, "URL Protocol")
                    return True
                except FileNotFoundError:
                    continue
        except OSError:
            continue
    return False


def _capture_view_fp_safe(
    hwnd: int | None = None,
    *,
    view_fingerprint_fn: Callable[..., Any] | None = None,
    include_heading: bool = True,
) -> Any:
    """Capture a WindowViewFingerprint safely via injected callback or view_fingerprint.py."""
    if view_fingerprint_fn is not None:
        try:
            return view_fingerprint_fn(hwnd)
        except TypeError:
            return view_fingerprint_fn()
    try:
        from clea.tools.view_fingerprint import capture_window_view_fingerprint

        return capture_window_view_fingerprint(hwnd=hwnd, include_heading=include_heading)
    except Exception:
        return None


def _compare_view_fps_safe(before: Any, after: Any) -> dict[str, Any]:
    """Compare two WindowViewFingerprint snapshots safely."""
    try:
        from clea.tools.view_fingerprint import compare_view_fingerprints

        return compare_view_fingerprints(before, after)
    except Exception:
        if before is None or after is None:
            return {
                "view_changed": False,
                "visual_delta": 0.0,
                "heading_before": str(getattr(before, "heading", "") or ""),
                "heading_after": str(getattr(after, "heading", "") or ""),
                "heading_changed": False,
            }
        b_hex = str(getattr(before, "dhash_hex", "") or "")
        a_hex = str(getattr(after, "dhash_hex", "") or "")
        b_head = str(getattr(before, "heading", "") or "")
        a_head = str(getattr(after, "heading", "") or "")
        delta = 0.0
        if b_hex and a_hex:
            try:
                xor_bits = int(b_hex, 16) ^ int(a_hex, 16)
                delta = round(xor_bits.bit_count() / 256.0, 4)
            except ValueError:
                delta = 1.0 if b_hex != a_hex else 0.0
        heading_changed = bool(b_head and a_head and b_head != a_head) or (
            not b_head and bool(a_head)
        )
        return {
            "view_changed": bool(delta >= 0.04 or heading_changed),
            "visual_delta": delta,
            "heading_before": b_head,
            "heading_after": a_head,
            "heading_changed": heading_changed,
        }


def _cancellable_settle_wait_safe(
    seconds: float,
    *,
    cancel_event: threading.Event | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> bool:
    """Wait briefly for async UI navigation while respecting cancellation."""
    if seconds <= 0:
        return not (cancel_event is not None and cancel_event.is_set())
    try:
        from clea.tools.view_fingerprint import cancellable_settle_wait

        return cancellable_settle_wait(seconds, cancel_event=cancel_event, sleep_fn=sleep_fn)
    except Exception:
        if cancel_event is not None:
            return not cancel_event.wait(seconds)
        (sleep_fn or time.sleep)(seconds)
        return True


def _check_url_protocol_registered(scheme: str) -> dict[str, str] | None:
    """Layer 2: Query HKCU\\Software\\Classes\\<scheme> and HKCR\\<scheme> for URL Protocol."""
    if sys.platform != "win32":
        return None
    clean_scheme = scheme.strip().lower()
    if (
        not clean_scheme
        or len(clean_scheme) < 2
        or clean_scheme in _RESERVED_URL_SCHEMES
        or clean_scheme in _DISALLOWED_SHELL_BINARIES
        or not _SAFE_URL_SCHEME_RE.fullmatch(clean_scheme)
    ):
        return None

    try:
        import winreg
    except ImportError:
        return None

    roots = (
        ("HKCU", winreg.HKEY_CURRENT_USER, f"Software\\Classes\\{clean_scheme}"),
        ("HKCR", winreg.HKEY_CLASSES_ROOT, clean_scheme),
    )
    for root_label, root_hive, subkey in roots:
        try:
            with winreg.OpenKey(root_hive, subkey) as key:
                try:
                    winreg.QueryValueEx(key, "URL Protocol")
                except FileNotFoundError:
                    continue

                friendly_raw = ""
                with contextlib.suppress(OSError):
                    friendly_raw, _ = winreg.QueryValueEx(key, "")

                cmd_raw = ""
                with contextlib.suppress(OSError):
                    with winreg.OpenKey(key, r"shell\open\command") as cmd_key:
                        cmd_raw, _ = winreg.QueryValueEx(cmd_key, "")

                exe_stem = _extract_exe_stem_from_command(cmd_raw)
                friendly = _clean_protocol_friendly_name(
                    friendly_raw, exe_stem or clean_scheme
                )
                if _is_browser_app_name(friendly) or _is_browser_app_name(exe_stem):
                    continue
                return {
                    "scheme": clean_scheme,
                    "app_name": friendly,
                    "exe_stem": exe_stem,
                    "command": str(cmd_raw or ""),
                    "registry_root": root_label,
                }
        except OSError:
            continue
    return None


def _window_matches_dynamic_tokens(
    win: EnumeratedWindow,
    tokens: list[str],
    user32: Any = None,
) -> bool:
    """Return True if *win* is a standalone app/PWA window matching any dynamic token."""
    is_iconic = bool(
        user32 is not None and hasattr(user32, "IsIconic") and user32.IsIconic(win.hwnd)
    )
    if not win.is_visible and not is_iconic:
        return False
    title = (win.title or "").strip()
    if not title or _is_browser_tab_window_title(title):
        return False
    title_low = title.lower()
    proc_stem = Path(win.process_name or "").stem.strip().lower()
    proc_is_browser = proc_stem in {"chrome", "msedge", "firefox", "brave", "opera", "arc"}

    for tok in tokens:
        t = tok.strip().lower()
        if len(t) < 2:
            continue
        t_compact = re.sub(r"[^a-z0-9]+", "", t)
        if not proc_is_browser and proc_stem and (
            proc_stem == t or (t_compact and proc_stem == t_compact)
        ):
            return True
        # Match whole word/phrase in standalone window title (works for PWAs & native apps)
        if re.search(rf"(?<![a-z0-9]){re.escape(t)}(?![a-z0-9])", title_low):
            return True
        if t_compact and len(t_compact) >= 3 and t_compact != t:
            title_compact = re.sub(r"[^a-z0-9]+", "", title_low)
            if title_compact == t_compact:
                return True
    return False


def _find_matching_standalone_window(
    tokens: list[str],
    windows: list[EnumeratedWindow],
    user32: Any,
) -> EnumeratedWindow | None:
    matches = [w for w in windows if _window_matches_dynamic_tokens(w, tokens, user32=user32)]
    if not matches:
        return None
    return _pick_best_matching_window(matches, user32, prefer_standalone_pwa=True)


def _match_catalog_entry_for_tokens(
    tokens: list[str],
    catalog: list[dict[str, str]],
) -> dict[str, str] | None:
    """Dynamically match candidate URL tokens against live StartApps / PWA catalog."""
    if not tokens or not catalog:
        return None

    for tok in tokens:
        t = tok.strip().lower()
        if len(t) < 2 or _is_browser_app_name(t):
            continue
        t_compact = re.sub(r"[^a-z0-9]+", "", t)

        # Pass 1: exact lowercase match
        for entry in catalog:
            name_low = " ".join(entry.get("Name", "").strip().lower().split())
            if not name_low or _is_browser_app_name(name_low):
                continue
            if name_low == t:
                return entry

        # Pass 2: compact alphanumeric exact match (e.g. 'youtube-music' == 'YouTube Music')
        if t_compact:
            for entry in catalog:
                name_low = " ".join(entry.get("Name", "").strip().lower().split())
                if not name_low or _is_browser_app_name(name_low):
                    continue
                if re.sub(r"[^a-z0-9]+", "", name_low) == t_compact:
                    return entry

        # Pass 3: generic app qualifiers (e.g. '<brand> music', '<brand> pwa', '<brand> app')
        for entry in catalog:
            name_low = " ".join(entry.get("Name", "").strip().lower().split())
            if not name_low or _is_browser_app_name(name_low):
                continue
            if any(name_low == f"{t} {q}" for q in _GENERIC_APP_QUALIFIERS):
                return entry

    return None


def resolve_url_to_installed_app(
    url: str,
    *,
    app_uri_handler_finder: Callable[[str], list[dict[str, str]]] | None = None,
    url_protocol_checker: Callable[[str], bool | dict[str, Any] | None] | None = None,
    app_catalog_fetcher: Callable[[], list[dict[str, str]]] | None = None,
    window_enumerator: Callable[[], list[EnumeratedWindow]] | None = None,
    user32: Any = None,
    kernel32: Any = None,
    force_refresh_catalog: bool = False,
) -> UrlAppResolution | None:
    """Dynamically resolve an http/https URL to an installed OS app using 3 live OS layers.

    Resolution order (zero hardcoded domain-to-app dictionaries):
      Layer 1: Windows ``AppUriHandlers`` (Store / MSIX apps via WinRT Launcher).
      Layer 2: Windows Registry custom ``URL Protocol`` (``HKCU\\Software\\Classes\\<scheme>``
               and ``HKCR\\<scheme>`` with ``URL Protocol`` value).
      Layer 3: Currently open standalone app/PWA windows (excluding browser tab windows)
               and the live cached ``get_installed_apps()`` catalog (StartApps / PWAs).
    """
    if not isinstance(url, str) or not url.strip():
        return None
    target_url = url.strip() if "://" in url else f"https://{url.strip()}"
    tokens = extract_url_app_tokens(target_url)

    def _get_windows() -> list[EnumeratedWindow]:
        if window_enumerator is not None:
            return list(window_enumerator())
        if user32 is not None or sys.platform == "win32":
            try:
                u32 = _get_win32_user32(user32)
                k32 = _get_win32_kernel32(kernel32)
                return _enumerate_windows_ctypes(u32, k32)
            except Exception:
                return []
        return []

    windows_cache: list[EnumeratedWindow] | None = None

    def _cached_windows() -> list[EnumeratedWindow]:
        nonlocal windows_cache
        if windows_cache is None:
            windows_cache = _get_windows()
        return windows_cache

    # Layer 1: Windows AppUriHandlers (Store / MSIX Apps)
    uri_finder = app_uri_handler_finder or _find_app_uri_handlers
    try:
        handlers = uri_finder(target_url) or []
    except Exception as exc:
        logger.debug("AppUriHandlers lookup error for %s: %s", target_url, exc)
        handlers = []

    for handler in handlers:
        if not isinstance(handler, dict):
            continue
        h_name = str(handler.get("name") or handler.get("display_name") or "").strip()
        h_aumid = str(handler.get("app_id") or handler.get("app_user_model_id") or "").strip()
        h_pfn = str(handler.get("package_family_name") or "").strip()
        if not h_name or _is_browser_app_name(h_name):
            continue
        win_tokens = [h_name.lower(), *tokens]
        existing_win = _find_matching_standalone_window(win_tokens, _cached_windows(), user32)
        return UrlAppResolution(
            app_name=h_name,
            resolution_layer="app_uri_handler",
            app_id=h_aumid or None,
            package_family_name=h_pfn or None,
            window=existing_win,
            candidate_tokens=tuple(tokens),
        )

    if not tokens:
        return None

    # Layer 2: Windows Registry Custom URL Protocol (Native Win32 / Electron Apps)
    proto_checker = url_protocol_checker or _check_url_protocol_registered
    protocol_candidates: list[str] = []
    for tok in tokens:
        for cand in (tok.replace(" ", "-"), re.sub(r"[^a-z0-9]+", "", tok)):
            if (
                len(cand) >= 2
                and cand not in _RESERVED_URL_SCHEMES
                and cand not in protocol_candidates
                and _SAFE_URL_SCHEME_RE.fullmatch(cand)
            ):
                protocol_candidates.append(cand)

    for scheme in protocol_candidates:
        try:
            proto_res = proto_checker(scheme)
        except Exception as exc:
            logger.debug("URL protocol check error for scheme '%s': %s", scheme, exc)
            proto_res = None
        if not proto_res:
            continue
        if isinstance(proto_res, dict):
            app_name = str(proto_res.get("app_name") or scheme).strip()
            exe_stem = str(proto_res.get("exe_stem") or "").strip()
            resolved_scheme = str(proto_res.get("scheme") or scheme).strip().lower()
        else:
            app_name = scheme
            exe_stem = ""
            resolved_scheme = scheme
        win_tokens = [
            t for t in [app_name.lower(), exe_stem.lower(), resolved_scheme, *tokens] if t
        ]
        existing_win = _find_matching_standalone_window(win_tokens, _cached_windows(), user32)
        return UrlAppResolution(
            app_name=app_name,
            resolution_layer="url_protocol",
            protocol_scheme=resolved_scheme,
            window=existing_win,
            candidate_tokens=tuple(tokens),
        )

    # Layer 3: Running standalone app/PWA windows + Live installed apps catalog
    existing_standalone = _find_matching_standalone_window(tokens, _cached_windows(), user32)
    if existing_standalone is not None:
        return UrlAppResolution(
            app_name=existing_standalone.title.strip() or tokens[0],
            resolution_layer="installed_app",
            window=existing_standalone,
            candidate_tokens=tuple(tokens),
        )

    was_cached = _INSTALLED_APPS_CACHE is not None and not force_refresh_catalog
    cache_age = (time.monotonic() - _INSTALLED_APPS_CACHE_TIME) if was_cached else 0.0
    catalog = get_installed_apps(
        force_refresh=force_refresh_catalog,
        app_catalog_fetcher=app_catalog_fetcher,
    )
    matched_entry = _match_catalog_entry_for_tokens(tokens, catalog)
    # Automatic cache invalidation on miss so newly installed apps/PWAs are picked up
    # immediately without restarting Clea (throttled for live PowerShell calls).
    if matched_entry is None and was_cached and (
        app_catalog_fetcher is not None or cache_age >= _APP_CACHE_MISS_REFRESH_INTERVAL
    ):
        invalidate_installed_apps_cache()
        catalog = get_installed_apps(
            force_refresh=True,
            app_catalog_fetcher=app_catalog_fetcher,
        )
        matched_entry = _match_catalog_entry_for_tokens(tokens, catalog)
        was_cached = False
    if matched_entry is not None:
        return UrlAppResolution(
            app_name=matched_entry["Name"],
            resolution_layer="installed_app",
            app_id=matched_entry["AppID"],
            candidate_tokens=tuple(tokens),
            from_cached_catalog=was_cached,
        )

    return None


def is_explicit_browser_request(
    text: str | None = None,
    *,
    browser: str | None = None,
) -> bool:
    """Return True when the caller/user explicitly requests opening in a web browser."""
    if isinstance(browser, str) and browser.strip():
        return True
    if not isinstance(text, str) or not text.strip():
        return False
    return bool(_EXPLICIT_BROWSER_REQUEST_RE.search(text))


def extract_explicit_browser(
    text: str | None = None,
    *,
    browser: str | None = None,
) -> str | None:
    """Return a specific browser name ('chrome', 'edge', 'firefox', 'brave') or None for default."""
    if isinstance(browser, str) and browser.strip():
        b_low = " ".join(browser.strip().lower().split())
        if b_low in {"default", "browser", "web", "web browser", "default browser"}:
            return None
        if b_low in {"google chrome", "chrome", "chrome.exe"}:
            return "chrome"
        if b_low in {"microsoft edge", "edge", "msedge", "msedge.exe"}:
            return "edge"
        if b_low in {"mozilla firefox", "firefox", "firefox.exe"}:
            return "firefox"
        if b_low in {"brave", "brave.exe"}:
            return "brave"
        return b_low
    if not isinstance(text, str) or not text.strip():
        return None
    low = text.lower()
    if re.search(r"\b(?:google\s+)?chrome\b", low):
        return "chrome"
    if re.search(r"\b(?:microsoft\s+edge|msedge|edge)\b", low):
        return "edge"
    if re.search(r"\b(?:mozilla\s+)?firefox\b", low):
        return "firefox"
    if re.search(r"\bbrave\b", low):
        return "brave"
    return None


def _strip_browser_directive(text: str) -> str:
    cleaned = text.strip().strip("'\"")
    cleaned = _BROWSER_DIRECTIVE_SUFFIX_RE.sub("", cleaned).strip()
    cleaned = _LEADING_OPEN_VERB_RE.sub("", cleaned).strip()
    return cleaned


def normalize_launch_target(target: str | None) -> str:
    """Normalize app names, aliases, domains, and URLs to a canonical lowercase target token."""
    if not isinstance(target, str):
        return ""
    stripped = _strip_browser_directive(target)
    if not stripped:
        return ""

    low = " ".join(stripped.lower().split())
    if low in _APP_ALIASES:
        return _APP_ALIASES[low]
    if low in {"x", "twitter"}:
        return "twitter"
    if low in {"mail", "gmail"}:
        return "gmail"

    looks_like_url = low.startswith(("http://", "https://")) or (
        "." in low
        and " " not in low
        and "\\" not in low
        and not any(low.endswith(ext) for ext in _LOCAL_FILE_EXTENSIONS)
    )
    if looks_like_url:
        try:
            parsed = urllib.parse.urlsplit(stripped if "://" in stripped else f"https://{stripped}")
            host = (parsed.hostname or "").lower().strip(".")
        except Exception:
            host = ""
        if host:
            if host == "youtu.be":
                return "youtube"
            if host in {"x.com", "www.x.com"}:
                return "twitter"
            if host == "mail.google.com":
                return "gmail"
            if host == "docs.google.com":
                return "docs"
            tokens = extract_url_app_tokens(stripped)
            if tokens:
                stem = tokens[-1]
                return _APP_ALIASES.get(stem, stem)

    if low.endswith(".exe"):
        low = low[:-4].strip()
    for suffix in (" app", " pwa", " desktop", " music"):
        if low.endswith(suffix) and low != "github desktop":
            base = low[: -len(suffix)].strip()
            if base:
                low = base
                break
    return _APP_ALIASES.get(low, low)


normalize_app_or_url_target = normalize_launch_target


def find_installed_service_app(
    target: str,
    catalog: list[dict[str, str]],
) -> dict[str, str] | None:
    """Dynamically match a service/URL target against installed Start Menu / Store / PWA apps."""
    if not isinstance(target, str) or not target.strip() or not catalog:
        return None
    tokens: list[str] = []
    if "://" in target or ("." in target and " " not in target):
        tokens.extend(extract_url_app_tokens(target))
    norm = normalize_launch_target(target)
    if norm and norm not in tokens:
        tokens.append(norm)
    return _match_catalog_entry_for_tokens(tokens, catalog)


def has_installed_app(
    target: str,
    *,
    which_fn: Callable[[str], str | None] = shutil.which,
    env: dict[str, str] | None = None,
    app_catalog_fetcher: Callable[[], list[dict[str, str]]] | None = None,
) -> bool:
    """Return True if *target* resolves to an installed native, Store, or PWA app."""
    norm = normalize_launch_target(target)
    if not norm:
        return False
    try:
        _resolve_spec_and_command(norm, which_fn=which_fn, env=env, require_executable=True)
        return True
    except ValueError:
        pass
    catalog = get_installed_apps(app_catalog_fetcher=app_catalog_fetcher)
    return find_installed_service_app(norm, catalog) is not None


def _resolve_service_fallback_url(raw_target: str, norm_target: str) -> str | None:
    cleaned = _strip_browser_directive(raw_target)
    low = cleaned.lower()
    if low.startswith(("http://", "https://")):
        return cleaned
    if norm_target in _SITE_SHORTCUTS:
        return _SITE_SHORTCUTS[norm_target]
    if low in _SITE_SHORTCUTS:
        return _SITE_SHORTCUTS[low]
    if (
        "." in cleaned
        and " " not in cleaned
        and "\\" not in cleaned
        and not any(low.endswith(ext) for ext in _LOCAL_FILE_EXTENSIONS)
    ):
        return f"https://{cleaned}"
    return None


_ORIG_GET_INSTALLED_APPS = get_installed_apps


def open_or_focus_app(
    app_name: str,
    target_path: str | None = None,
    *,
    user32: Any = None,
    kernel32: Any = None,
    window_enumerator: Callable[[], list[EnumeratedWindow]] | None = None,
    which_fn: Callable[[str], str | None] = shutil.which,
    popen_fn: Callable[..., Any] = subprocess.Popen,
    env: dict[str, str] | None = None,
    app_catalog_fetcher: Callable[[], list[dict[str, str]]] | None = None,
    startfile_fn: Callable[[str], Any] | None = None,
    desktop_name_fn: Callable[[], str | None] | None = None,
    focus_backend: FocusPreconditionBackend | None = None,
    verifier: ActionVerifier | None = None,
    verify_timeout_s: float = 1.2,
    prefer_browser: bool = False,
    browser: str | None = None,
    user_request: str | None = None,
) -> dict[str, Any]:
    """Focus a running application window or launch an installed desktop/Store/PWA app.

    Installed-App-First Policy:
    Resolves and launches the installed native/Store/PWA application first. Only falls
    back to opening a browser URL if (a) the caller/user explicitly requests the browser
    or web version (e.g. ``prefer_browser=True``, ``browser='chrome'``, or
    ``'open YouTube in Chrome'``), or (b) no installed app match exists on the machine
    and the target corresponds to a known web service or URL.
    """
    resolved_target = _validate_target_path(target_path, env=env)
    if not isinstance(app_name, str):
        raise ValueError("app_name must be a non-empty string.")

    raw_app_name = app_name
    explicit_browser = bool(prefer_browser) or is_explicit_browser_request(
        user_request or raw_app_name,
        browser=browser,
    )
    chosen_browser = extract_explicit_browser(user_request or raw_app_name, browser=browser)

    if explicit_browser and resolved_target is None:
        clean_target = _strip_browser_directive(raw_app_name)
        norm_target = normalize_launch_target(clean_target)
        fallback_url = _resolve_service_fallback_url(clean_target, norm_target)
        if fallback_url is not None:
            url_res = open_url(
                fallback_url,
                browser=chosen_browser,
                startfile_fn=startfile_fn,
                popen_fn=popen_fn,
                which_fn=which_fn,
                env=env,
                desktop_name_fn=desktop_name_fn,
                user32=user32,
                kernel32=kernel32,
                window_enumerator=window_enumerator,
                prefer_browser=True,
            )
            return {
                "ok": bool(url_res.get("ok", True)),
                "status": "launched"
                if url_res.get("ok", True)
                else str(url_res.get("status") or "error"),
                "app": norm_target or clean_target,
                "window_title": url_res.get("window_title"),
                "hwnd": url_res.get("window_hwnd"),
                "executable": url_res.get("browser") or "default",
                "target_path": url_res.get("url", fallback_url),
                "url": url_res.get("url", fallback_url),
                "browser": url_res.get("browser") or "default",
                "launched_app": False,
                "reused_browser_window": bool(url_res.get("reused_browser_window", False)),
                "normalized_target": norm_target,
            }

    original_url_candidate: str | None = None
    stripped_raw = raw_app_name.strip()
    if resolved_target is None and stripped_raw.lower().startswith(("http://", "https://")):
        original_url_candidate = stripped_raw
        norm_from_url = normalize_launch_target(stripped_raw)
        if norm_from_url:
            app_name = norm_from_url
    elif resolved_target is None and _LEADING_OPEN_VERB_RE.match(stripped_raw):
        cleaned_verb = _strip_browser_directive(stripped_raw)
        if cleaned_verb:
            app_name = cleaned_verb

    if resolved_target is None and isinstance(app_name, str):
        app_stripped = app_name.strip()
        app_norm = " ".join(app_stripped.lower().split())
        text_exts = {".txt", ".md", ".json", ".py", ".log"}
        media_exts = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".pdf"}
        suffix = Path(app_stripped).suffix.lower()
        if app_norm not in _APP_ALIASES and suffix in (text_exts | media_exts):
            try:
                file_target = _validate_target_path(app_stripped, env=env)
            except ValueError:
                file_target = None
            if file_target is not None and file_target.is_file():
                if suffix in text_exts:
                    app_name = "notepad"
                    resolved_target = file_target
                else:
                    non_default = _check_interactive_desktop(desktop_name_fn)
                    if non_default is not None:
                        return _non_interactive_desktop_result(
                            file_target.name, non_default, file_target
                        )
                    _open_via_shell(
                        str(file_target),
                        startfile_fn=startfile_fn,
                        popen_fn=popen_fn,
                    )
                    return {
                        "ok": True,
                        "status": "launched",
                        "app": file_target.name,
                        "window_title": None,
                        "hwnd": None,
                        "executable": "explorer.exe",
                        "target_path": str(file_target),
                    }

    spec: _AppSpec | None = None
    resolve_err: Exception | None = None
    try:
        spec, _ = _resolve_spec_and_command(
            app_name,
            which_fn=which_fn,
            env=env,
            require_executable=False,
        )
    except ValueError as exc:
        resolve_err = exc

    u32 = _get_win32_user32(user32)
    if resolved_target is None and spec is not None:
        if window_enumerator is not None:
            windows = list(window_enumerator())
        else:
            k32 = _get_win32_kernel32(kernel32)
            windows = _enumerate_windows_ctypes(u32, k32)

        known_matches = [win for win in windows if _window_matches_spec(win, spec, user32=u32)]
        if known_matches:
            win = _pick_best_matching_window(known_matches, u32)
            non_default = _check_interactive_desktop(desktop_name_fn)
            if non_default is not None:
                return _non_interactive_desktop_result(spec.canonical_name, non_default)
            target_hwnd = _resolve_root_window_hwnd(int(win.hwnd), u32)
            _focus_window_hwnd(u32, target_hwnd)
            res_focus: dict[str, Any] = {
                "ok": True,
                "status": "focused",
                "app": spec.canonical_name,
                "window_title": win.title,
                "hwnd": target_hwnd,
                "executable": None,
                "target_path": None,
                "launched_app": True,
                "normalized_target": normalize_launch_target(spec.canonical_name),
            }
            if (
                verifier is not None
                or focus_backend is not None
                or (user32 is None and window_enumerator is None)
            ):
                active_verifier = verifier or ActionVerifier(focus_backend=focus_backend)
                outcome = active_verifier.verify_window_state(
                    target_hwnd,
                    expected_state="focused",
                    timeout_s=verify_timeout_s,
                )
                res_focus["verification"] = outcome.to_dict()
            return res_focus

    if spec is not None:
        try:
            launched_spec, cmd = _resolve_spec_and_command(
                app_name,
                which_fn=which_fn,
                env=env,
                require_executable=True,
            )
            assert cmd is not None
            if resolved_target is not None:
                cmd = [*cmd, str(resolved_target)]

            non_default = _check_interactive_desktop(desktop_name_fn)
            if non_default is not None:
                return _non_interactive_desktop_result(
                    launched_spec.canonical_name, non_default, resolved_target
                )

            popen_fn(cmd, **_build_gui_popen_kwargs())
            return {
                "ok": True,
                "status": "launched",
                "app": launched_spec.canonical_name,
                "window_title": None,
                "hwnd": None,
                "executable": cmd[0],
                "target_path": str(resolved_target) if resolved_target is not None else None,
                "launched_app": True,
                "normalized_target": normalize_launch_target(launched_spec.canonical_name),
            }
        except ValueError as exc:
            resolve_err = exc

    # Check input safety before dynamic StartApps lookup
    if not isinstance(app_name, str):
        raise ValueError("app_name must be a non-empty string.")
    stripped = app_name.strip()
    if not stripped:
        raise ValueError("app_name must not be empty.")
    if ".." in stripped or _SHELL_METACHAR_RE.search(stripped):
        raise ValueError(f"Invalid characters in application name: {app_name!r}")
    normalized = " ".join(stripped.lower().split())
    if (
        normalized in _DISALLOWED_SHELL_BINARIES
        or f"{normalized}.exe" in _DISALLOWED_SHELL_BINARIES
    ):
        raise ValueError(f"Application '{app_name}' is disallowed for security reasons.")

    was_cached = _INSTALLED_APPS_CACHE is not None
    catalog = get_installed_apps(app_catalog_fetcher=app_catalog_fetcher)
    matched_app = find_matching_start_app(stripped, catalog)
    # Automatic cache invalidation: if a target isn't in the cached StartApps snapshot,
    # refresh once so newly installed apps are picked up immediately without restarting Clea.
    if matched_app is None and was_cached:
        invalidate_installed_apps_cache()
        catalog = get_installed_apps(
            force_refresh=True,
            app_catalog_fetcher=app_catalog_fetcher,
        )
        matched_app = find_matching_start_app(stripped, catalog)

    if matched_app is not None:
        matched_name = matched_app["Name"]
        matched_appid = matched_app["AppID"]

        if resolved_target is None:
            if window_enumerator is not None:
                windows = list(window_enumerator())
            else:
                k32 = _get_win32_kernel32(kernel32)
                windows = _enumerate_windows_ctypes(u32, k32)

            matched_norm = normalize_launch_target(matched_name)
            matched_is_browser = matched_norm in _BROWSER_CANONICAL_NAMES
            match_tokens = [matched_name.lower()]
            if matched_norm and len(matched_norm) >= 2 and matched_norm not in match_tokens:
                match_tokens.append(matched_norm)

            def _is_window_candidate(win: EnumeratedWindow) -> bool:
                is_iconic = bool(
                    u32 is not None
                    and hasattr(u32, "IsIconic")
                    and u32.IsIconic(win.hwnd)
                )
                if not win.is_visible and not is_iconic:
                    return False
                if not matched_is_browser and _is_browser_tab_window_title(win.title):
                    return False
                title_low = (win.title or "").lower()
                proc_low = (win.process_name or "").lower()
                proc_stem = Path(win.process_name or "").stem.strip().lower()
                proc_is_browser = proc_stem in {
                    "chrome",
                    "msedge",
                    "firefox",
                    "brave",
                    "opera",
                    "arc",
                }
                for tok in match_tokens:
                    if tok in title_low:
                        return True
                    if (matched_is_browser or not proc_is_browser) and tok in proc_low:
                        return True
                return False

            start_matches = [win for win in windows if _is_window_candidate(win)]
            if start_matches:
                win = _pick_best_matching_window(
                    start_matches,
                    u32,
                    prefer_standalone_pwa=not matched_is_browser,
                    prefer_browser_window=matched_is_browser,
                )
                non_default = _check_interactive_desktop(desktop_name_fn)
                if non_default is not None:
                    return _non_interactive_desktop_result(matched_name, non_default)
                target_hwnd = _resolve_root_window_hwnd(int(win.hwnd), u32)
                _focus_window_hwnd(u32, target_hwnd)
                res_matched_focus: dict[str, Any] = {
                    "ok": True,
                    "status": "focused",
                    "app": matched_name,
                    "window_title": win.title,
                    "hwnd": target_hwnd,
                    "executable": None,
                    "target_path": None,
                    "launched_app": True,
                    "normalized_target": normalize_launch_target(matched_name),
                }
                if (
                    verifier is not None
                    or focus_backend is not None
                    or (user32 is None and window_enumerator is None)
                ):
                    active_verifier = verifier or ActionVerifier(focus_backend=focus_backend)
                    outcome = active_verifier.verify_window_state(
                        target_hwnd,
                        expected_state="focused",
                        timeout_s=verify_timeout_s,
                    )
                    res_matched_focus["verification"] = outcome.to_dict()
                return res_matched_focus

        non_default = _check_interactive_desktop(desktop_name_fn)
        if non_default is not None:
            return _non_interactive_desktop_result(matched_name, non_default, resolved_target)

        launch_cmd = ["explorer.exe", f"shell:AppsFolder\\{matched_appid}"]
        if resolved_target is not None:
            launch_cmd.append(str(resolved_target))
        try:
            popen_fn(launch_cmd, **_build_gui_popen_kwargs())
        except OSError:
            invalidate_installed_apps_cache()
            raise
        return {
            "ok": True,
            "status": "launched",
            "app": matched_name,
            "window_title": None,
            "hwnd": None,
            "executable": launch_cmd[0],
            "target_path": str(resolved_target) if resolved_target is not None else None,
            "launched_app": True,
            "normalized_target": normalize_launch_target(matched_name),
        }

    # Browser URL fallback ONLY when no installed app exists on the machine
    if resolved_target is None:
        norm_fallback = normalize_launch_target(raw_app_name)
        fallback_url = original_url_candidate or _resolve_service_fallback_url(
            stripped, norm_fallback
        )
        if fallback_url is not None:
            url_res = open_url(
                fallback_url,
                browser=chosen_browser,
                startfile_fn=startfile_fn,
                popen_fn=popen_fn,
                which_fn=which_fn,
                env=env,
                desktop_name_fn=desktop_name_fn,
                user32=user32,
                kernel32=kernel32,
                window_enumerator=window_enumerator,
                prefer_browser=True,
            )
            return {
                "ok": bool(url_res.get("ok", True)),
                "status": "launched"
                if url_res.get("ok", True)
                else str(url_res.get("status") or "error"),
                "app": norm_fallback or stripped,
                "window_title": url_res.get("window_title"),
                "hwnd": url_res.get("window_hwnd"),
                "executable": url_res.get("browser") or "default",
                "target_path": url_res.get("url", fallback_url),
                "url": url_res.get("url", fallback_url),
                "browser": url_res.get("browser") or "default",
                "launched_app": False,
                "reused_browser_window": bool(url_res.get("reused_browser_window", False)),
                "normalized_target": norm_fallback,
            }

    if resolve_err is not None:
        raise resolve_err
    raise ValueError(f"Application '{app_name}' could not be resolved to an allowed executable.")


# --------------------------------------------------------------------------- System Volume & Media


_POWERSHELL_SET_VOLUME_SCRIPT = r"""
param([double]$Scalar)
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
    int GetMute(out bool pbMute);
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

public static class CleaAudioEndpoint {
    public static void SetMasterVolumeScalar(float scalar) {
        var enumerator = (IMMDeviceEnumerator)(new MMDeviceEnumeratorComObject());
        IMMDevice device;
        Marshal.ThrowExceptionForHR(enumerator.GetDefaultAudioEndpoint(0, 1, out device));
        var iid = typeof(IAudioEndpointVolume).GUID;
        object endpointObj;
        Marshal.ThrowExceptionForHR(device.Activate(ref iid, 23, IntPtr.Zero, out endpointObj));
        var endpoint = (IAudioEndpointVolume)endpointObj;
        Marshal.ThrowExceptionForHR(endpoint.SetMasterVolumeLevelScalar(scalar, Guid.Empty));
    }
}
"@
[CleaAudioEndpoint]::SetMasterVolumeScalar([float]$Scalar)
"""


def _set_system_master_volume(level: int) -> None:
    """Set Windows master output volume (0..100) via pycaw or CoreAudio PowerShell fallback."""
    if sys.platform != "win32":
        raise OSError("Setting system volume requires Windows or an injected volume_setter.")

    scalar = max(0.0, min(1.0, float(level) / 100.0))

    try:
        from comtypes import CLSCTX_ALL  # type: ignore[import-not-found]
        from pycaw.pycaw import AudioUtilities, IAudioEndpointVolume  # type: ignore[import-not-found]

        devices = AudioUtilities.GetSpeakers()
        interface = devices.Activate(IAudioEndpointVolume._iid_, CLSCTX_ALL, None)
        endpoint = interface.QueryInterface(IAudioEndpointVolume)
        endpoint.SetMasterVolumeLevelScalar(scalar, None)
        return
    except ImportError:
        pass
    except Exception as exc:
        logger.debug("pycaw volume setter failed (%s); falling back to PowerShell CoreAudio.", exc)

    ps_exe = shutil.which("powershell.exe") or shutil.which("powershell")
    if not ps_exe:
        raise RuntimeError("Neither pycaw nor PowerShell is available to set system volume.")

    proc = run_powershell_hidden(
        f"& {{ {_POWERSHELL_SET_VOLUME_SCRIPT} }} -Scalar {scalar:.4f}",
        timeout=8.0,
        ps_exe=ps_exe,
        check=False,
    )
    if proc.returncode != 0:
        detail = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"Failed to set system volume: {detail or f'exit {proc.returncode}'}")


def control_media_volume(
    action: str,
    level: int | None = None,
    *,
    user32: Any = None,
    volume_setter: Callable[[int], None] | None = None,
    media_backend: MediaVerificationBackend | None = None,
    verifier: ActionVerifier | None = None,
    verify_timeout_s: float = 1.5,
) -> dict[str, Any]:
    """Control system audio volume or media playback via Win32 virtual keys or CoreAudio."""
    if not isinstance(action, str):
        raise ValueError("action must be a string.")
    normalized = action.strip().lower()
    if normalized not in VALID_MEDIA_VOLUME_ACTIONS:
        raise ValueError(
            f"Unsupported media/volume action {action!r}. "
            f"Valid actions: {', '.join(VALID_MEDIA_VOLUME_ACTIONS)}."
        )

    clamped_level: int | None = None
    if level is not None:
        if isinstance(level, bool):
            raise ValueError("level must be an integer between 0 and 100.")
        try:
            parsed_level = int(level)
        except (TypeError, ValueError) as exc:
            raise ValueError("level must be an integer between 0 and 100.") from exc
        clamped_level = max(0, min(100, parsed_level))

    if normalized == "set_volume":
        if clamped_level is None:
            raise ValueError("level (0..100) is required when action is 'set_volume'.")
        setter = volume_setter or _set_system_master_volume
        setter(clamped_level)
        return {
            "ok": True,
            "action": "set_volume",
            "level": clamped_level,
        }

    is_playback_action = normalized in {"play_pause", "next_track", "previous_track"}
    should_verify = is_playback_action and (
        verifier is not None or media_backend is not None or user32 is None
    )
    active_verifier = (
        verifier
        if verifier is not None
        else (ActionVerifier(media_backend=media_backend) if should_verify else None)
    )
    baseline = active_verifier.probe_media_session() if active_verifier is not None else None

    vk = _MEDIA_VOLUME_VK_MAP[normalized]
    u32 = _get_win32_user32(user32)
    u32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY, 0)
    u32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)

    res: dict[str, Any] = {
        "ok": True,
        "action": normalized,
        "level": clamped_level,
    }
    if active_verifier is not None:
        outcome = active_verifier.verify_media_state(
            baseline=baseline,
            timeout_s=verify_timeout_s,
        )
        res["verification"] = outcome.to_dict()
    return res


# --------------------------------------------------------------------------- Clipboard


@contextlib.contextmanager
def _open_clipboard(user32: Any, *, retries: int = 5, retry_delay: float = 0.02):
    opened = False
    for attempt in range(max(1, retries)):
        if user32.OpenClipboard(None):
            opened = True
            break
        if attempt + 1 < retries:
            time.sleep(retry_delay)
    if not opened:
        raise RuntimeError("Could not open Windows clipboard (locked by another process).")
    try:
        yield
    finally:
        with contextlib.suppress(Exception):
            user32.CloseClipboard()


_WINRT_CLIPBOARD_TYPE = (
    "[Windows.ApplicationModel.DataTransfer.Clipboard, "
    "Windows.ApplicationModel.DataTransfer, ContentType = WindowsRuntime]"
)
_WINRT_CLIPBOARD_RESULT_TYPE = (
    "[Windows.ApplicationModel.DataTransfer.ClipboardHistoryItemsResult, "
    "Windows.ApplicationModel, ContentType = WindowsRuntime]"
)
_WINRT_STREAM_REF_TYPE = (
    "[Windows.Storage.Streams.RandomAccessStreamReference, "
    "Windows.Storage.Streams, ContentType = WindowsRuntime]"
)
_WINRT_STREAM_TYPE = (
    "[Windows.Storage.Streams.IRandomAccessStreamWithContentType, "
    "Windows.Storage.Streams, ContentType = WindowsRuntime]"
)

_POWERSHELL_READ_CLIPBOARD_HISTORY_SCRIPT_TEMPLATE = r"""
param(
    [int]$MaxItems = 10,
    [int]$MaxChars = 8000,
    [switch]$IncludeImageBytes
)

$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.Runtime.WindowsRuntime
Add-Type -AssemblyName System.Drawing

function Await-AsyncOp($asyncOp, $targetType = $null) {
    $asTaskGeneric = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.IsGenericMethodDefinition
    } | Select-Object -First 1
    if (-not $asTaskGeneric) {
        throw "Could not find AsTask generic method"
    }
    if (-not $targetType -and $asyncOp.GetType().GetGenericArguments().Count -gt 0) {
        $targetType = $asyncOp.GetType().GetGenericArguments()[0]
    }
    $asTask = $asTaskGeneric.MakeGenericMethod($targetType)
    $task = $asTask.Invoke($null, @($asyncOp))
    [void]$task.Wait(5000)
    return $task.Result
}

$historyEnabled = $false
try {
    $clip = __WINRT_CLIPBOARD_TYPE__
    $historyEnabled = $clip::IsHistoryEnabled()
} catch {
    $historyEnabled = $false
}

$historyOp = __WINRT_CLIPBOARD_TYPE__::GetHistoryItemsAsync()
$historyResultType = __WINRT_CLIPBOARD_RESULT_TYPE__
$historyResult = Await-AsyncOp $historyOp $historyResultType

$rawItems = @()
if ($historyResult -and $historyResult.Items) {
    $rawItems = @($historyResult.Items)
}
$totalItems = $rawItems.Count

$limit = [Math]::Min($totalItems, $MaxItems)
$outItems = @()

for ($i = 0; $i -lt $limit; $i++) {
    $item = $rawItems[$i]
    $id = ""
    try { $id = [string]$item.Id } catch {}
    $ts = ""
    try { $ts = $item.Timestamp.ToString("o") } catch {}
    $content = $item.Content

    $itemDict = [ordered]@{
        index = $i
        id = $id
        timestamp = $ts
        kind = "unknown"
        text = $null
        truncated = $false
        image = $null
    }

    $handled = $false
    try {
        $txtFmt = [Windows.ApplicationModel.DataTransfer.StandardDataFormats]::Text
        if ($content.Contains($txtFmt)) {
            $textOp = $content.GetTextAsync()
            $text = Await-AsyncOp $textOp ([string])
            $truncated = $false
            if ($text -and $text.Length -gt $MaxChars) {
                $text = $text.Substring(0, $MaxChars)
                $truncated = $true
            }
            $itemDict.kind = "text"
            $itemDict.text = $text
            $itemDict.truncated = $truncated
            $handled = $true
        }
    } catch {
        # format check or fetch failed
    }

    if (-not $handled) {
        try {
            $bmpFmt = [Windows.ApplicationModel.DataTransfer.StandardDataFormats]::Bitmap
            if ($content.Contains($bmpFmt)) {
                $bitmapOp = $content.GetBitmapAsync()
                $streamRefType = __WINRT_STREAM_REF_TYPE__
                $streamRef = Await-AsyncOp $bitmapOp $streamRefType
                if ($streamRef) {
                    $streamOp = $streamRef.OpenReadAsync()
                    $streamType = __WINRT_STREAM_TYPE__
                    $raStream = Await-AsyncOp $streamOp $streamType
                    $streamExt = [System.IO.WindowsRuntimeStreamExtensions]
                    $asStreamMethod = $streamExt.GetMethods() | Where-Object {
                        $_.Name -eq 'AsStreamForRead' -and $_.GetParameters().Count -eq 1
                    } | Select-Object -First 1
                    $netStream = $asStreamMethod.Invoke($null, @($raStream))
                    $img = [System.Drawing.Image]::FromStream($netStream)
                    $w = $img.Width
                    $h = $img.Height
                    $imgData = [ordered]@{
                        width = $w
                        height = $h
                        format = "png"
                        bytes = $null
                    }
                    if ($IncludeImageBytes) {
                        $ms = New-Object System.IO.MemoryStream
                        $img.Save($ms, [System.Drawing.Imaging.ImageFormat]::Png)
                        $imgData.bytes = [Convert]::ToBase64String($ms.ToArray())
                        $ms.Dispose()
                    }
                    $img.Dispose()
                    $netStream.Dispose()
                    $raStream.Dispose()

                    $itemDict.kind = "image"
                    $itemDict.image = $imgData
                    $handled = $true
                }
            }
        } catch {
            # bitmap check or fetch failed
        }
    }

    $outItems += $itemDict
}

$output = [ordered]@{
    history_enabled = [bool]$historyEnabled
    total_items = $totalItems
    items = $outItems
}

$output | ConvertTo-Json -Depth 5 -Compress
"""

_POWERSHELL_READ_CLIPBOARD_HISTORY_SCRIPT = (
    _POWERSHELL_READ_CLIPBOARD_HISTORY_SCRIPT_TEMPLATE.replace(
        "__WINRT_CLIPBOARD_TYPE__",
        _WINRT_CLIPBOARD_TYPE,
    )
    .replace("__WINRT_CLIPBOARD_RESULT_TYPE__", _WINRT_CLIPBOARD_RESULT_TYPE)
    .replace("__WINRT_STREAM_REF_TYPE__", _WINRT_STREAM_REF_TYPE)
    .replace("__WINRT_STREAM_TYPE__", _WINRT_STREAM_TYPE)
)

_POWERSHELL_SELECT_CLIPBOARD_ITEM_SCRIPT_TEMPLATE = r"""
param(
    [int]$ItemIndex
)

$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.Runtime.WindowsRuntime

function Await-AsyncOp($asyncOp, $targetType = $null) {
    $asTaskGeneric = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.IsGenericMethodDefinition
    } | Select-Object -First 1
    if (-not $asTaskGeneric) {
        throw "Could not find AsTask generic method"
    }
    if (-not $targetType -and $asyncOp.GetType().GetGenericArguments().Count -gt 0) {
        $targetType = $asyncOp.GetType().GetGenericArguments()[0]
    }
    $asTask = $asTaskGeneric.MakeGenericMethod($targetType)
    $task = $asTask.Invoke($null, @($asyncOp))
    [void]$task.Wait(5000)
    return $task.Result
}

$historyOp = __WINRT_CLIPBOARD_TYPE__::GetHistoryItemsAsync()
$historyResultType = __WINRT_CLIPBOARD_RESULT_TYPE__
$historyResult = Await-AsyncOp $historyOp $historyResultType

$rawItems = @()
if ($historyResult -and $historyResult.Items) {
    $rawItems = @($historyResult.Items)
}

if ($rawItems.Count -eq 0 -or $ItemIndex -lt 0 -or $ItemIndex -ge $rawItems.Count) {
    $cnt = $rawItems.Count
    throw "Index $ItemIndex is out of range (history items count: $cnt)"
}

$targetItem = $rawItems[$ItemIndex]
$status = __WINRT_CLIPBOARD_TYPE__::SetHistoryItemAsContent($targetItem)

$successEnum = [Windows.ApplicationModel.DataTransfer.SetHistoryItemAsContentStatus]::Success
$out = [ordered]@{
    success = ($status -eq $successEnum)
    status = $status.ToString()
    index = $ItemIndex
    id = [string]$targetItem.Id
}

$out | ConvertTo-Json -Compress
"""

_POWERSHELL_SELECT_CLIPBOARD_ITEM_SCRIPT = (
    _POWERSHELL_SELECT_CLIPBOARD_ITEM_SCRIPT_TEMPLATE.replace(
        "__WINRT_CLIPBOARD_TYPE__",
        _WINRT_CLIPBOARD_TYPE,
    ).replace("__WINRT_CLIPBOARD_RESULT_TYPE__", _WINRT_CLIPBOARD_RESULT_TYPE)
)


def _fallback_snapshot_to_win32_dict(
    cap: int, *, include_image_bytes: bool = False
) -> dict[str, Any]:
    """Read clipboard formats via Win32ClipboardBackend (PNG, CF_BITMAP, CF_HDROP, text)."""
    try:
        from clea.tools.clipboard import Win32ClipboardBackend

        snap = Win32ClipboardBackend().read_snapshot()
    except Exception as exc:
        logger.warning("Win32ClipboardBackend snapshot fallback failed: %s", exc)
        return {
            "ok": True,
            "kind": "empty",
            "text": "",
            "truncated": False,
            "char_count": 0,
            "total_chars": 0,
        }

    if snap.kind == "image":
        w, h = snap.image_dimensions or (0, 0)
        image_meta: dict[str, Any] = {
            "width": w,
            "height": h,
            "format": (snap.image_format or "png").lower(),
            "size_bytes": len(snap.image_bytes) if snap.image_bytes else 0,
        }
        if include_image_bytes and snap.image_bytes:
            image_meta["image_bytes_base64"] = base64.b64encode(snap.image_bytes).decode("ascii")
        return {
            "ok": True,
            "kind": "image",
            "text": "",
            "truncated": False,
            "char_count": 0,
            "total_chars": 0,
            "image": image_meta,
        }
    if snap.kind == "files":
        file_text = "\n".join(snap.file_paths)
        truncated = len(file_text) > cap
        shown = file_text[:cap] if truncated else file_text
        return {
            "ok": True,
            "kind": "files",
            "text": shown,
            "truncated": truncated,
            "char_count": len(shown),
            "total_chars": len(file_text),
            "file_paths": list(snap.file_paths),
            "has_image_files": snap.has_image_files,
        }
    if snap.kind == "text" and snap.text is not None:
        total_chars = len(snap.text)
        truncated = total_chars > cap
        shown = snap.text[:cap] if truncated else snap.text
        return {
            "ok": True,
            "kind": "text" if total_chars > 0 else "empty",
            "text": shown,
            "truncated": truncated,
            "char_count": len(shown),
            "total_chars": total_chars,
        }
    return {
        "ok": True,
        "kind": "empty",
        "text": "",
        "truncated": False,
        "char_count": 0,
        "total_chars": 0,
    }


def _read_clipboard_win32(
    cap: int,
    *,
    user32: Any = None,
    kernel32: Any = None,
    include_image_bytes: bool = False,
) -> dict[str, Any]:
    if user32 is None and kernel32 is None:
        return _fallback_snapshot_to_win32_dict(cap, include_image_bytes=include_image_bytes)

    u32 = _get_win32_user32(user32)
    k32 = _get_win32_kernel32(kernel32)

    missing_unicode_text = False
    with _open_clipboard(u32):
        if hasattr(u32, "IsClipboardFormatAvailable") and not u32.IsClipboardFormatAvailable(
            CF_UNICODETEXT
        ):
            missing_unicode_text = True
        else:
            handle = u32.GetClipboardData(CF_UNICODETEXT)
            if not handle:
                missing_unicode_text = True
            else:
                ptr = k32.GlobalLock(handle)
                if not ptr:
                    raise RuntimeError("Failed to lock clipboard data handle.")
                try:
                    if isinstance(ptr, str):
                        raw_text = ptr
                    elif isinstance(ptr, (bytes, bytearray)):
                        raw_text = bytes(ptr).decode("utf-16-le").rstrip("\x00")
                    else:
                        raw_text = ctypes.wstring_at(ptr)
                finally:
                    k32.GlobalUnlock(handle)

    if missing_unicode_text:
        return {
            "ok": True,
            "text": "",
            "truncated": False,
            "char_count": 0,
            "total_chars": 0,
        }

    total_chars = len(raw_text)
    truncated = total_chars > cap
    text = raw_text[:cap] if truncated else raw_text
    return {
        "ok": True,
        "text": text,
        "truncated": truncated,
        "char_count": len(text),
        "total_chars": total_chars,
    }


def read_clipboard(
    max_chars: int = DEFAULT_CLIPBOARD_MAX_CHARS,
    *,
    include_history: bool = False,
    max_items: int = 10,
    include_image_bytes: bool = False,
    user32: Any = None,
    kernel32: Any = None,
    powershell_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    """Read UTF-16LE Unicode text or history items from the Windows clipboard.

    When ``include_history=False`` (default), reads active clipboard content via
    ``Win32ClipboardBackend`` (or injected Win32 test doubles). When
    ``include_history=True``, queries WinRT Clipboard History via PowerShell 5.1,
    falling back to Win32 on error or when the active item kind is unknown.
    """
    if isinstance(max_chars, bool):
        raise ValueError("max_chars must be an integer.")
    try:
        cap = int(max_chars)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_chars must be an integer.") from exc
    cap = max(1, min(cap, MAX_CLIPBOARD_CHARS))

    if not include_history:
        return _read_clipboard_win32(
            cap,
            user32=user32,
            kernel32=kernel32,
            include_image_bytes=include_image_bytes,
        )

    history_enabled = False
    total_items = 0
    items: list[dict[str, Any]] = []
    winrt_success = False

    ps_exe = shutil.which("powershell.exe")
    if ps_exe:
        try:
            inc_bytes_flag = " -IncludeImageBytes" if include_image_bytes else ""
            proc = run_powershell_hidden(
                (
                    f"& {{ {_POWERSHELL_READ_CLIPBOARD_HISTORY_SCRIPT} }} "
                    f"-MaxItems {int(max_items)} -MaxChars {int(cap)}{inc_bytes_flag}"
                ),
                timeout=10.0,
                ps_exe=ps_exe,
                runner=powershell_runner,
                check=False,
            )
            if proc.returncode == 0 and proc.stdout.strip():
                data = json.loads(proc.stdout.strip())
                history_enabled = bool(data.get("history_enabled", False))
                total_items = int(data.get("total_items", 0))
                raw_items = data.get("items", [])
                if isinstance(raw_items, list):
                    items = raw_items
                winrt_success = True
        except Exception as exc:
            logger.debug("WinRT clipboard history failed (%s); falling back to Win32.", exc)

    top_kind: str | None = None
    extra_fields: dict[str, Any] = {}
    if winrt_success and items:
        top_item = items[0]
        raw_top_kind = str(top_item.get("kind") or "unknown")
        if raw_top_kind == "text" and top_item.get("text") is not None:
            top_kind = "text"
            text = top_item["text"]
            truncated = bool(top_item.get("truncated", False))
            total_chars = len(text)
            top_image = None
        elif raw_top_kind == "image" and isinstance(top_item.get("image"), dict):
            top_kind = "image"
            text = ""
            truncated = False
            total_chars = 0
            top_image = top_item.get("image")
        elif powershell_runner is None:
            win32_res = _read_clipboard_win32(
                cap,
                user32=user32,
                kernel32=kernel32,
                include_image_bytes=include_image_bytes,
            )
            text = win32_res["text"]
            truncated = win32_res["truncated"]
            total_chars = win32_res["total_chars"]
            top_image = win32_res.get("image")
            top_kind = win32_res.get("kind") or "empty"
            if "file_paths" in win32_res:
                extra_fields["file_paths"] = win32_res["file_paths"]
                extra_fields["has_image_files"] = bool(win32_res.get("has_image_files", False))
            if top_kind in ("image", "files", "text"):
                patched_item = dict(top_item)
                patched_item["kind"] = top_kind
                if top_kind == "image" and top_image is not None:
                    patched_item["image"] = top_image
                elif top_kind in ("text", "files"):
                    patched_item["text"] = text
                    patched_item["truncated"] = truncated
                items = [patched_item, *items[1:]]
        else:
            top_kind = raw_top_kind
            text = ""
            truncated = False
            total_chars = 0
            top_image = top_item.get("image")
    else:
        win32_res = _read_clipboard_win32(
            cap,
            user32=user32,
            kernel32=kernel32,
            include_image_bytes=include_image_bytes,
        )
        text = win32_res["text"]
        truncated = win32_res["truncated"]
        total_chars = win32_res["total_chars"]
        top_image = win32_res.get("image")
        top_kind = win32_res.get("kind")
        if "file_paths" in win32_res:
            extra_fields["file_paths"] = win32_res["file_paths"]
            extra_fields["has_image_files"] = bool(win32_res.get("has_image_files", False))

    res: dict[str, Any] = {
        "ok": True,
        "text": text,
        "truncated": truncated,
        "char_count": len(text),
        "total_chars": total_chars,
        "history_enabled": history_enabled,
        "total_items": total_items,
        "items": items,
    }
    if top_kind is not None:
        res["kind"] = top_kind
    if top_image is not None:
        res["image"] = top_image
    res.update(extra_fields)
    return res


def select_clipboard_item(
    index: int,
    *,
    powershell_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    """Restore a clipboard history item by index via WinRT SetHistoryItemAsContent."""
    if isinstance(index, bool) or not isinstance(index, int):
        raise ValueError("index must be an integer.")
    if index < 0:
        raise ValueError("index must be non-negative.")

    ps_exe = shutil.which("powershell.exe")
    if not ps_exe:
        raise RuntimeError("powershell.exe is required for clipboard history operations.")

    proc = run_powershell_hidden(
        f"& {{ {_POWERSHELL_SELECT_CLIPBOARD_ITEM_SCRIPT} }} -ItemIndex {index}",
        timeout=10.0,
        ps_exe=ps_exe,
        runner=powershell_runner,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"Failed to select clipboard item {index}: {err}")

    try:
        data = json.loads(proc.stdout.strip())
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"Unexpected output selecting clipboard item: {proc.stdout}") from exc

    return {
        "ok": bool(data.get("success", False)),
        "success": bool(data.get("success", False)),
        "index": index,
        "item_id": data.get("id"),
        "status": data.get("status", "Unknown"),
    }


def write_clipboard(
    text: str,
    *,
    user32: Any = None,
    kernel32: Any = None,
) -> dict[str, Any]:
    """Write UTF-16LE Unicode text to the Windows clipboard."""
    if not isinstance(text, str):
        raise ValueError("text must be a string.")

    encoded = text.encode("utf-16-le") + b"\x00\x00"
    u32 = _get_win32_user32(user32)
    k32 = _get_win32_kernel32(kernel32)

    with _open_clipboard(u32):
        if not u32.EmptyClipboard():
            raise RuntimeError("Failed to empty Windows clipboard.")

        h_mem = k32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
        if not h_mem:
            raise RuntimeError("Failed to allocate global memory for clipboard.")

        ptr = k32.GlobalLock(h_mem)
        if not ptr:
            if hasattr(k32, "GlobalFree"):
                with contextlib.suppress(Exception):
                    k32.GlobalFree(h_mem)
            raise RuntimeError("Failed to lock allocated clipboard memory.")

        try:
            ctypes.memmove(ptr, encoded, len(encoded))
        finally:
            k32.GlobalUnlock(h_mem)

        if not u32.SetClipboardData(CF_UNICODETEXT, h_mem):
            if hasattr(k32, "GlobalFree"):
                with contextlib.suppress(Exception):
                    k32.GlobalFree(h_mem)
            raise RuntimeError("Failed to write Unicode text to Windows clipboard.")

    return {
        "ok": True,
        "char_count": len(text),
    }


# --------------------------------------------------------------------------- URL & System Controls


def _find_existing_browser_window(
    windows: list[EnumeratedWindow],
    *,
    browser_name: str | None,
    url_tokens: list[str],
    user32: Any,
) -> EnumeratedWindow | None:
    """Find an already-open browser window to reuse (preferring one already on the target site)."""
    if not windows:
        return None

    b_low = (browser_name or "").strip().lower()
    browser_spec = _KNOWN_APPS.get(b_low) if b_low else None

    candidates: list[EnumeratedWindow] = []
    for win in windows:
        is_iconic = bool(
            user32 is not None and hasattr(user32, "IsIconic") and user32.IsIconic(win.hwnd)
        )
        if not win.is_visible and not is_iconic:
            continue
        if browser_spec is not None:
            if _window_matches_spec(win, browser_spec, user32=user32):
                candidates.append(win)
        elif b_low:
            title_low = (win.title or "").lower()
            proc_low = (win.process_name or "").lower()
            if _is_browser_tab_window_title(win.title) and (
                b_low in title_low or b_low in proc_low
            ):
                candidates.append(win)
        else:
            if _is_browser_tab_window_title(win.title):
                candidates.append(win)

    if not candidates:
        return None

    # Prefer a browser window already on that domain/site
    if url_tokens:
        domain_matches = [
            win
            for win in candidates
            if any(
                re.search(rf"(?<![a-z0-9]){re.escape(tok)}(?![a-z0-9])", (win.title or "").lower())
                for tok in url_tokens
                if len(tok) >= 2
            )
        ]
        if domain_matches:
            return _pick_best_matching_window(domain_matches, user32, prefer_browser_window=True)

    return _pick_best_matching_window(candidates, user32, prefer_browser_window=True)


def _is_deep_url(url: str) -> bool:
    """Return True if *url* has a non-root path, query string, or fragment."""
    with contextlib.suppress(Exception):
        parsed = urllib.parse.urlsplit(url if "://" in url else f"https://{url}")
        path = (parsed.path or "").strip("/")
        return bool(path or parsed.query or parsed.fragment)
    return False


def _is_chromium_browser_name(browser_name: str | None) -> bool:
    return is_chromium_app_name(browser_name)


def open_url(
    url: str,
    browser: str | None = None,
    *,
    startfile_fn: Callable[[str], Any] | None = None,
    popen_fn: Callable[..., Any] | None = None,
    which_fn: Callable[[str], str | None] = shutil.which,
    env: dict[str, str] | None = None,
    desktop_name_fn: Callable[[], str | None] | None = None,
    app_catalog_fetcher: Callable[[], list[dict[str, str]]] | None = None,
    app_uri_handler_finder: Callable[[str], list[dict[str, str]]] | None = None,
    url_protocol_checker: Callable[[str], bool | dict[str, Any] | None] | None = None,
    registry_lookup: Callable[[str], bool] | None = None,
    user32: Any = None,
    kernel32: Any = None,
    window_enumerator: Callable[[], list[EnumeratedWindow]] | None = None,
    prefer_browser: bool = False,
    user_request: str | None = None,
    cdp_bridge: CDPBridge | None = None,
    pwa_launch_settle_s: float | None = None,
    view_fingerprint_fn: Callable[..., Any] | None = None,
    settle_wait_s: float | None = None,
    cancel_event: threading.Event | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    verify_view: bool | None = None,
) -> dict[str, Any]:
    """Validate and open an http/https URL or registered OS custom URL Protocol URI.

    For ``http``/``https`` URLs, unless the caller explicitly requests a browser
    (``prefer_browser=True``, ``browser=...``, or browser phrasing in ``user_request``),
    resolves dynamically across:
      1. Windows ``AppUriHandlers`` (Store / MSIX apps)
      2. Windows Registry custom ``URL Protocol`` keys (native Win32 / Electron apps)
      3. Running standalone app/PWA windows & live ``get_installed_apps()`` catalog

    For non-http/https URIs with a valid RFC 3986 leading ``<scheme>:``, enforces the
    dangerous-scheme blocklist and verifies ``HKCR\\<scheme>\\URL Protocol`` before
    launching directly via the OS protocol handler (never ``shell=True``).
    """
    effective_popen = popen_fn if popen_fn is not None else subprocess.Popen
    if not isinstance(url, str):
        raise ValueError("url must be a string.")
    stripped = url.strip()
    if not stripped:
        raise ValueError("url must not be empty.")

    if any(ord(ch) < 32 or ord(ch) == 127 for ch in stripped):
        raise ValueError("URL contains invalid control characters.")

    lower_stripped = stripped.lower()
    if any(lower_stripped.startswith(prefix) for prefix in _DANGEROUS_SCHEME_PREFIXES):
        raise ValueError(f"Disallowed URL scheme in {url!r}.")

    # Reject local Windows drive paths (e.g. C:\..., C:/..., C:calc.exe)
    if re.match(r"^[A-Za-z]:", stripped):
        raise ValueError(
            f"Local file paths and Windows drive paths are not permitted in open_url: {url!r}"
        )

    # Reject UNC paths (\\server\... or //server/...) and leading slashes/backslashes
    if re.search(r"^[\\/]", stripped):
        raise ValueError(f"Leading slashes/backslashes are not allowed in URL: {url!r}")

    # Reject command lines or shell binaries (e.g. 'cmd.exe /c calc', 'powershell ...')
    first_token = re.split(r"[\s/\\?#]", lower_stripped)[0]
    if first_token in _DISALLOWED_SHELL_BINARIES:
        raise ValueError(f"Disallowed command or shell binary in URL: {url!r}")

    # Detect valid RFC 3986 leading <scheme>: without mangling custom protocols into https://
    scheme_match = (
        None if _HOST_PORT_RE.match(stripped) else _RFC3986_SCHEME_PREFIX_RE.match(stripped)
    )
    if scheme_match is not None:
        detected_scheme = scheme_match.group(1).lower()
        if (
            detected_scheme in _DANGEROUS_URL_SCHEMES
            or detected_scheme in _DISALLOWED_SHELL_BINARIES
            or detected_scheme in {"ms-settings", "ws", "wss", "about", "chrome", "edge"}
        ):
            raise ValueError(f"Disallowed URL scheme '{detected_scheme}' in {url!r}.")

        if detected_scheme not in _ALLOWED_URL_SCHEMES:
            if any(ch.isspace() for ch in stripped):
                raise ValueError(
                    f"Whitespace is not permitted in custom protocol URI: {url!r}"
                )
            if _CUSTOM_URL_FORBIDDEN_CHARS_RE.search(stripped):
                raise ValueError(
                    f"Disallowed shell metacharacter in custom protocol URI: {url!r}"
                )
            before_query = stripped.split("?", 1)[0]
            if "&" in before_query:
                raise ValueError(
                    f"Disallowed shell metacharacter '&' in custom protocol URI: {url!r}"
                )

            eff_registry_lookup: Callable[[str], bool] | None = registry_lookup
            if eff_registry_lookup is None and url_protocol_checker is not None:

                def _protocol_checker_adapter(
                    scheme_arg: str,
                    _checker: Callable[[str], bool | dict[str, Any] | None] = url_protocol_checker,
                ) -> bool:
                    return bool(_checker(scheme_arg))

                eff_registry_lookup = _protocol_checker_adapter

            if not is_registered_url_protocol(
                detected_scheme,
                registry_lookup=eff_registry_lookup,
            ):
                raise ValueError(
                    f"Unregistered or unsupported URL scheme '{detected_scheme}' in {url!r}."
                )

            non_default = _check_interactive_desktop(desktop_name_fn)
            if non_default is not None:
                return {
                    "ok": False,
                    "success": False,
                    "url": stripped,
                    "scheme": detected_scheme,
                    "protocol_scheme": detected_scheme,
                    "browser": None,
                    "routed_to_installed_app": False,
                    "resolution_layer": "url_protocol",
                    "status": "non_interactive_desktop",
                    "goal_verified": False,
                    "verification_scope": "dispatch_only",
                    "message": (
                        f"Cannot launch URL protocol '{detected_scheme}:' visibly because the "
                        f"daemon is running on isolated desktop '{non_default}' instead of "
                        "'WinSta0\\Default'."
                    ),
                }

            should_verify_view = (
                view_fingerprint_fn is not None
                or verify_view is True
                or (
                    verify_view is None
                    and startfile_fn is None
                    and popen_fn is None
                    and sys.platform == "win32"
                )
            )
            u32_proto = user32
            if u32_proto is None and should_verify_view and sys.platform == "win32":
                with contextlib.suppress(Exception):
                    u32_proto = _get_win32_user32(None)

            before_hwnd = 0
            if u32_proto is not None and hasattr(u32_proto, "GetForegroundWindow"):
                with contextlib.suppress(Exception):
                    raw_fg = u32_proto.GetForegroundWindow()
                    if isinstance(raw_fg, int) and not isinstance(raw_fg, bool) and raw_fg > 0:
                        before_hwnd = raw_fg
            before_win_hwnds = (
                {int(w.hwnd) for w in window_enumerator()}
                if window_enumerator is not None
                else set()
            )
            before_fp = (
                _capture_view_fp_safe(
                    before_hwnd or None,
                    view_fingerprint_fn=view_fingerprint_fn,
                )
                if should_verify_view
                else None
            )

            _open_via_shell(
                stripped,
                startfile_fn=startfile_fn,
                popen_fn=effective_popen,
            )

            if should_verify_view:
                eff_settle = (
                    settle_wait_s
                    if settle_wait_s is not None
                    else (0.2 if view_fingerprint_fn is None else 0.0)
                )
                _cancellable_settle_wait_safe(
                    eff_settle,
                    cancel_event=cancel_event,
                    sleep_fn=sleep_fn,
                )

            after_hwnd = 0
            if u32_proto is not None and hasattr(u32_proto, "GetForegroundWindow"):
                with contextlib.suppress(Exception):
                    raw_fg_after = u32_proto.GetForegroundWindow()
                    if (
                        isinstance(raw_fg_after, int)
                        and not isinstance(raw_fg_after, bool)
                        and raw_fg_after > 0
                    ):
                        after_hwnd = raw_fg_after
            after_win_hwnds = (
                {int(w.hwnd) for w in window_enumerator()}
                if window_enumerator is not None
                else set()
            )
            after_fp = (
                _capture_view_fp_safe(
                    after_hwnd or before_hwnd or None,
                    view_fingerprint_fn=view_fingerprint_fn,
                )
                if should_verify_view
                else None
            )

            view_diff = (
                _compare_view_fps_safe(before_fp, after_fp)
                if should_verify_view
                else {
                    "view_changed": False,
                    "visual_delta": 0.0,
                    "heading_before": "",
                    "heading_after": "",
                    "heading_changed": False,
                }
            )
            window_changed = bool(
                (after_hwnd > 0 and after_hwnd != before_hwnd)
                or bool(after_win_hwnds - before_win_hwnds)
            )
            view_changed = bool(view_diff.get("view_changed", False))
            visual_delta = float(view_diff.get("visual_delta", 0.0))
            heading_before = str(view_diff.get("heading_before", "") or "")
            heading_after = str(view_diff.get("heading_after", "") or "")
            goal_verified = bool(view_changed or window_changed)
            verification_scope = "in_app_view" if goal_verified else "dispatch_only"

            return {
                "ok": True,
                "success": True,
                "status": "launched",
                "url": stripped,
                "scheme": detected_scheme,
                "protocol_scheme": detected_scheme,
                "browser": None,
                "launched_app": True,
                "routed_to_installed_app": True,
                "resolution_layer": "url_protocol",
                "hwnd": after_hwnd or None,
                "window_changed": window_changed,
                "view_changed": view_changed,
                "visual_delta": visual_delta,
                "heading_before": heading_before,
                "heading_after": heading_after,
                "goal_verified": goal_verified,
                "verification_scope": verification_scope,
                "state_diff": {
                    "changed": goal_verified,
                    "window_changed": window_changed,
                    "view_changed": view_changed,
                    "visual_delta": visual_delta,
                    "heading_before": heading_before,
                    "heading_after": heading_after,
                },
                "message": (
                    f"Launched registered URL protocol '{detected_scheme}:' via OS handler."
                ),
            }

    # If URL has no scheme, the host portion before path cannot have unencoded spaces
    if "://" not in stripped:
        host_candidate = stripped.split("/")[0].split("\\")[0]
        if any(ch.isspace() for ch in host_candidate):
            raise ValueError(f"Invalid host with whitespace in URL: {url!r}")

    if lower_stripped in _SITE_SHORTCUTS:
        target_url = _SITE_SHORTCUTS[lower_stripped]
    elif "://" not in stripped:
        target_url = f"https://{stripped}"
    else:
        target_url = stripped

    parsed = urllib.parse.urlsplit(target_url)
    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_URL_SCHEMES:
        raise ValueError(
            f"Invalid URL scheme '{parsed.scheme}'. Only http and https URLs are allowed."
        )

    if not parsed.netloc or any(ch.isspace() for ch in parsed.netloc):
        raise ValueError(f"Invalid URL: host/domain is required in {url!r}")

    if isinstance(browser, str) and not browser.strip():
        browser = None

    explicit_browser = bool(prefer_browser) or is_explicit_browser_request(
        user_request, browser=browser
    )
    if explicit_browser and browser is None and user_request:
        browser = extract_explicit_browser(user_request)

    norm_target = normalize_launch_target(target_url)
    url_tokens = extract_url_app_tokens(target_url)

    # Keep hermetic unit tests isolated from real desktop windows unless a window enumerator
    # or user32 double was supplied or this is a real (unmocked) OS invocation.
    allow_live_win32 = (
        window_enumerator is not None
        or user32 is not None
        or (popen_fn is None and startfile_fn is None and app_catalog_fetcher is None)
    )
    allow_live_cdp = (
        cdp_bridge is not None
        or has_explicit_default_cdp_bridge()
        or (
            popen_fn is None
            and startfile_fn is None
            and app_catalog_fetcher is None
            and user32 is None
            and window_enumerator is None
        )
    )
    effective_cdp_bridge: CDPBridge | None = (
        cdp_bridge
        if cdp_bridge is not None
        else (get_default_cdp_bridge() if allow_live_cdp else None)
    )
    effective_win_enum: Callable[[], list[EnumeratedWindow]] | None = (
        window_enumerator if window_enumerator is not None else (None if allow_live_win32 else list)
    )

    def _get_u32_optional() -> Any:
        if user32 is not None:
            return user32
        if allow_live_win32 and sys.platform == "win32":
            with contextlib.suppress(Exception):
                return _get_win32_user32(None)
        return None

    def _enumerate_current_windows() -> list[EnumeratedWindow]:
        if effective_win_enum is not None:
            return list(effective_win_enum())
        u32_live = _get_u32_optional()
        if u32_live is None:
            return []
        try:
            k32_live = _get_win32_kernel32(kernel32)
            return _enumerate_windows_ctypes(u32_live, k32_live)
        except Exception:
            return []

    def _make_routed_result(
        *,
        app_name: str,
        status: str,
        resolution_layer: str,
        hwnd: int | None = None,
        window_title: str | None = None,
        executable: str | None = None,
        target_path: str | None = None,
        protocol_scheme: str | None = None,
        ok: bool = True,
        guidance: str | None = None,
    ) -> dict[str, Any]:
        return {
            "ok": ok,
            "success": ok,
            "status": status,
            "app": app_name,
            "app_name": app_name,
            "window_title": window_title,
            "hwnd": hwnd,
            "window_hwnd": hwnd,
            "executable": executable,
            "target_path": target_path,
            "url": target_url,
            "browser": None,
            "launched_app": True,
            "routed_to_installed_app": True,
            "resolution_layer": resolution_layer,
            "protocol_scheme": protocol_scheme,
            "normalized_target": norm_target or (url_tokens[0] if url_tokens else ""),
            "guidance": _INSTALLED_APP_ROUTED_GUIDANCE if guidance is None else guidance,
            "message": (
                f"Routed URL to installed app '{app_name}' via {resolution_layer} "
                "(app-first policy)."
            ),
        }

    # Dynamic 3-Layer OS App-First URL Routing (applies to ALL http/https URLs including
    # deep links unless the user/caller explicitly requested a browser).
    if not explicit_browser and browser is None:
        resolution = resolve_url_to_installed_app(
            target_url,
            app_uri_handler_finder=app_uri_handler_finder,
            url_protocol_checker=url_protocol_checker,
            app_catalog_fetcher=app_catalog_fetcher,
            window_enumerator=effective_win_enum,
            user32=user32,
            kernel32=kernel32,
        )

        if resolution is not None:
            non_default = _check_interactive_desktop(desktop_name_fn)
            if non_default is not None:
                return {
                    "ok": False,
                    "success": False,
                    "url": target_url,
                    "browser": None,
                    "app": resolution.app_name,
                    "app_name": resolution.app_name,
                    "routed_to_installed_app": False,
                    "resolution_layer": resolution.resolution_layer,
                    "status": "non_interactive_desktop",
                    "message": (
                        f"Cannot focus or launch '{resolution.app_name}' visibly because the "
                        f"daemon is running on isolated desktop '{non_default}' instead of "
                        "'WinSta0\\Default'."
                    ),
                }

            # If a standalone window for the resolved app is already open, focus/restore it!
            if resolution.window is not None:
                u32_active = _get_u32_optional()
                target_hwnd = (
                    _resolve_root_window_hwnd(int(resolution.window.hwnd), u32_active)
                    if u32_active is not None
                    else int(resolution.window.hwnd)
                )
                if u32_active is not None:
                    _focus_window_hwnd(u32_active, target_hwnd, kernel32=kernel32)

                # If this is a Chromium PWA window and CDP is enabled, navigate it directly!
                if effective_cdp_bridge is not None and is_chromium_process(
                    resolution.window.process_name
                ):
                    nav = effective_cdp_bridge.navigate_sync(
                        target_url,
                        prefer_pwa=True,
                        create_if_missing=False,
                        preferred_browser=resolution.window.process_name,
                    )
                    if isinstance(nav, dict) and nav.get("ok"):
                        routed = _make_routed_result(
                            app_name=resolution.app_name,
                            status="navigated",
                            resolution_layer=resolution.resolution_layer,
                            hwnd=target_hwnd,
                            window_title=str(nav.get("title") or resolution.window.title or ""),
                            protocol_scheme=resolution.protocol_scheme,
                            guidance=_CDP_PWA_NAVIGATED_GUIDANCE,
                        )
                        routed["url"] = str(nav.get("url") or target_url)
                        routed["title"] = str(nav.get("title") or "")
                        routed["target_id"] = nav.get("target_id")
                        routed["is_pwa"] = bool(nav.get("is_pwa", True))
                        routed["cdp_used"] = True
                        routed["before_url"] = nav.get("before_url")
                        routed["after_url"] = nav.get("after_url")
                        if isinstance(nav.get("state_diff"), dict):
                            routed["state_diff"] = nav["state_diff"]
                        routed["message"] = (
                            f"Navigated installed PWA '{resolution.app_name}' directly via CDP "
                            f"to {routed['url']}."
                        )
                        return routed

                return _make_routed_result(
                    app_name=resolution.app_name,
                    status="focused",
                    resolution_layer=resolution.resolution_layer,
                    hwnd=target_hwnd,
                    window_title=resolution.window.title,
                    protocol_scheme=resolution.protocol_scheme,
                )

            # Layer 1: AppUriHandlers launch via AUMID / AppsFolder or open_or_focus_app
            if resolution.resolution_layer == "app_uri_handler":
                if resolution.app_id:
                    launch_cmd = ["explorer.exe", f"shell:AppsFolder\\{resolution.app_id}"]
                    effective_popen(launch_cmd, **_build_gui_popen_kwargs())
                    return _make_routed_result(
                        app_name=resolution.app_name,
                        status="launched",
                        resolution_layer="app_uri_handler",
                        executable=launch_cmd[0],
                    )
                app_res = open_or_focus_app(
                    resolution.app_name,
                    user32=user32,
                    kernel32=kernel32,
                    window_enumerator=effective_win_enum,
                    which_fn=which_fn,
                    popen_fn=effective_popen,
                    env=env,
                    app_catalog_fetcher=app_catalog_fetcher,
                    startfile_fn=startfile_fn,
                    desktop_name_fn=desktop_name_fn,
                )
                return _make_routed_result(
                    app_name=str(app_res.get("app") or resolution.app_name),
                    status=str(app_res.get("status") or "launched"),
                    resolution_layer="app_uri_handler",
                    hwnd=app_res.get("hwnd"),
                    window_title=app_res.get("window_title"),
                    executable=app_res.get("executable"),
                    target_path=app_res.get("target_path"),
                    ok=bool(app_res.get("ok", True)),
                )

            # Layer 2: Windows Registry custom URL Protocol launch (single-instance native)
            if resolution.resolution_layer == "url_protocol" and resolution.protocol_scheme:
                proto_uri = f"{resolution.protocol_scheme}:"
                _open_via_shell(
                    proto_uri,
                    startfile_fn=startfile_fn,
                    popen_fn=effective_popen,
                )
                return _make_routed_result(
                    app_name=resolution.app_name,
                    status="launched",
                    resolution_layer="url_protocol",
                    executable=proto_uri,
                    protocol_scheme=resolution.protocol_scheme,
                )

            # Layer 3: Live StartApps / PWA launch with automatic cache invalidation on failure
            if resolution.resolution_layer == "installed_app":
                if effective_cdp_bridge is not None:
                    nav_existing = effective_cdp_bridge.navigate_sync(
                        target_url,
                        prefer_pwa=True,
                        create_if_missing=False,
                    )
                    if isinstance(nav_existing, dict) and nav_existing.get("ok"):
                        matched_hwnd: int | None = None
                        if allow_live_win32:
                            u32_pwa = _get_u32_optional()
                            if u32_pwa is not None:
                                nav_title = str(nav_existing.get("title") or resolution.app_name)
                                for cand in _enumerate_current_windows():
                                    if not is_chromium_process(cand.process_name):
                                        continue
                                    cand_low = (cand.title or "").lower()
                                    if (
                                        (nav_title and nav_title.lower() in cand_low)
                                        or (
                                            resolution.app_name
                                            and resolution.app_name.lower() in cand_low
                                        )
                                    ) and not _is_browser_tab_window_title(cand.title):
                                        matched_hwnd = _resolve_root_window_hwnd(
                                            int(cand.hwnd), u32_pwa
                                        )
                                        _focus_window_hwnd(u32_pwa, matched_hwnd, kernel32=kernel32)
                                        break
                        routed_cdp = _make_routed_result(
                            app_name=resolution.app_name,
                            status="navigated",
                            resolution_layer="installed_app",
                            hwnd=matched_hwnd,
                            window_title=str(nav_existing.get("title") or resolution.app_name),
                            guidance=_CDP_PWA_NAVIGATED_GUIDANCE,
                        )
                        routed_cdp["url"] = str(nav_existing.get("url") or target_url)
                        routed_cdp["title"] = str(nav_existing.get("title") or "")
                        routed_cdp["target_id"] = nav_existing.get("target_id")
                        routed_cdp["is_pwa"] = bool(nav_existing.get("is_pwa", True))
                        routed_cdp["cdp_used"] = True
                        routed_cdp["before_url"] = nav_existing.get("before_url")
                        routed_cdp["after_url"] = nav_existing.get("after_url")
                        if isinstance(nav_existing.get("state_diff"), dict):
                            routed_cdp["state_diff"] = nav_existing["state_diff"]
                        routed_cdp["message"] = (
                            f"Navigated installed PWA '{resolution.app_name}' directly via CDP "
                            f"to {routed_cdp['url']}."
                        )
                        return routed_cdp
                try:
                    app_res = open_or_focus_app(
                        resolution.app_name,
                        user32=user32,
                        kernel32=kernel32,
                        window_enumerator=effective_win_enum,
                        which_fn=which_fn,
                        popen_fn=effective_popen,
                        env=env,
                        app_catalog_fetcher=app_catalog_fetcher,
                        startfile_fn=startfile_fn,
                        desktop_name_fn=desktop_name_fn,
                    )
                    routed_l3 = _make_routed_result(
                        app_name=str(app_res.get("app") or resolution.app_name),
                        status=str(app_res.get("status") or "launched"),
                        resolution_layer="installed_app",
                        hwnd=app_res.get("hwnd"),
                        window_title=app_res.get("window_title"),
                        executable=app_res.get("executable"),
                        target_path=app_res.get("target_path"),
                        ok=bool(app_res.get("ok", True)),
                    )
                    if (
                        routed_l3.get("ok")
                        and effective_cdp_bridge is not None
                        and _is_deep_url(target_url)
                    ):
                        settle_budget = (
                            max(0.0, float(pwa_launch_settle_s))
                            if pwa_launch_settle_s is not None
                            else (1.5 if popen_fn is None else 0.0)
                        )
                        deadline = time.monotonic() + settle_budget
                        nav = effective_cdp_bridge.navigate_sync(
                            target_url,
                            prefer_pwa=True,
                            create_if_missing=False,
                        )
                        while (
                            isinstance(nav, dict)
                            and not nav.get("ok")
                            and nav.get("cdp_available") is True
                            and nav.get("status") == "target_not_found"
                            and time.monotonic() < deadline
                        ):
                            time.sleep(min(0.2, max(0.01, deadline - time.monotonic())))
                            nav = effective_cdp_bridge.navigate_sync(
                                target_url,
                                prefer_pwa=True,
                                create_if_missing=False,
                            )
                        if isinstance(nav, dict) and nav.get("ok"):
                            routed_l3["status"] = "navigated"
                            routed_l3["url"] = str(nav.get("url") or target_url)
                            routed_l3["title"] = str(nav.get("title") or "")
                            routed_l3["window_title"] = (
                                str(nav.get("title") or "") or routed_l3.get("window_title")
                            )
                            routed_l3["target_id"] = nav.get("target_id")
                            routed_l3["is_pwa"] = bool(nav.get("is_pwa", True))
                            routed_l3["cdp_used"] = True
                            routed_l3["guidance"] = _CDP_PWA_NAVIGATED_GUIDANCE
                            routed_l3["message"] = (
                                f"Launched and navigated installed PWA '{routed_l3['app_name']}' "
                                f"directly via CDP to {routed_l3['url']}."
                            )
                            routed_l3["before_url"] = nav.get("before_url")
                            routed_l3["after_url"] = nav.get("after_url")
                            if isinstance(nav.get("state_diff"), dict):
                                routed_l3["state_diff"] = nav["state_diff"]
                    return routed_l3
                except (OSError, ValueError) as exc:
                    logger.debug(
                        "Installed app launch failed for '%s' (%s); refreshing catalog cache.",
                        resolution.app_name,
                        exc,
                    )
                    invalidate_installed_apps_cache()
                    refreshed_catalog = get_installed_apps(
                        force_refresh=True,
                        app_catalog_fetcher=app_catalog_fetcher,
                    )
                    refreshed_match = _match_catalog_entry_for_tokens(
                        url_tokens, refreshed_catalog
                    )
                    if refreshed_match is not None:
                        app_res = open_or_focus_app(
                            refreshed_match["Name"],
                            user32=user32,
                            kernel32=kernel32,
                            window_enumerator=effective_win_enum,
                            which_fn=which_fn,
                            popen_fn=effective_popen,
                            env=env,
                            app_catalog_fetcher=lambda: refreshed_catalog,
                            startfile_fn=startfile_fn,
                            desktop_name_fn=desktop_name_fn,
                        )
                        return _make_routed_result(
                            app_name=str(app_res.get("app") or refreshed_match["Name"]),
                            status=str(app_res.get("status") or "launched"),
                            resolution_layer="installed_app",
                            hwnd=app_res.get("hwnd"),
                            window_title=app_res.get("window_title"),
                            executable=app_res.get("executable"),
                            target_path=app_res.get("target_path"),
                            ok=bool(app_res.get("ok", True)),
                        )
                    # App was uninstalled -> fall through cleanly to browser opening.

    # Browser path: explicit browser requested OR no installed app exists.
    # Reuse any existing browser window (preferring one already on that domain/site)
    # and NEVER pass --new-window.
    reused_browser_hwnd: int | None = None
    reused_browser_title: str | None = None

    if browser is None:
        non_default = _check_interactive_desktop(desktop_name_fn)
        if non_default is not None:
            return {
                "ok": False,
                "success": False,
                "url": target_url,
                "browser": "default",
                "status": "non_interactive_desktop",
                "routed_to_installed_app": False,
                "message": (
                    f"Cannot open '{target_url}' visibly because the daemon is running on "
                    f"isolated desktop '{non_default}' instead of 'WinSta0\\Default'."
                ),
            }
        if allow_live_win32:
            u32_b = _get_u32_optional()
            existing_browser = _find_existing_browser_window(
                _enumerate_current_windows(),
                browser_name=None,
                url_tokens=url_tokens,
                user32=u32_b,
            )
            if existing_browser is not None:
                reused_browser_hwnd = (
                    _resolve_root_window_hwnd(int(existing_browser.hwnd), u32_b)
                    if u32_b is not None
                    else int(existing_browser.hwnd)
                )
                reused_browser_title = existing_browser.title
                if u32_b is not None:
                    _focus_window_hwnd(u32_b, reused_browser_hwnd, kernel32=kernel32)
        _open_via_shell(target_url, startfile_fn=startfile_fn, popen_fn=effective_popen)
        return {
            "ok": True,
            "success": True,
            "url": target_url,
            "browser": "default",
            "status": "opened",
            "launched_app": False,
            "routed_to_installed_app": False,
            "reused_browser_window": reused_browser_hwnd is not None,
            "hwnd": reused_browser_hwnd,
            "window_hwnd": reused_browser_hwnd,
            "window_title": reused_browser_title,
            "normalized_target": norm_target,
        }

    b_clean = browser.strip().lower()
    if b_clean in _DISALLOWED_SHELL_BINARIES or _SHELL_METACHAR_RE.search(b_clean):
        raise ValueError(f"Disallowed browser name: {browser!r}")

    non_default = _check_interactive_desktop(desktop_name_fn)
    if non_default is not None:
        return {
            "ok": False,
            "success": False,
            "url": target_url,
            "browser": b_clean,
            "status": "non_interactive_desktop",
            "routed_to_installed_app": False,
            "message": (
                f"Cannot open '{target_url}' visibly because the daemon is running on "
                f"isolated desktop '{non_default}' instead of 'WinSta0\\Default'."
            ),
        }

    # Prefer CDP direct tab navigation for explicit Chromium browser requests (reuses the
    # existing regular browser window without hijacking any standalone PWA window).
    if effective_cdp_bridge is not None and _is_chromium_browser_name(b_clean):
        nav = effective_cdp_bridge.navigate_sync(
            target_url,
            prefer_pwa=False,
            create_if_missing=True,
            preferred_browser=b_clean,
        )
        if isinstance(nav, dict) and nav.get("ok"):
            if allow_live_win32:
                u32_b = _get_u32_optional()
                existing_browser = _find_existing_browser_window(
                    _enumerate_current_windows(),
                    browser_name=b_clean,
                    url_tokens=url_tokens,
                    user32=u32_b,
                )
                if existing_browser is not None:
                    reused_browser_hwnd = (
                        _resolve_root_window_hwnd(int(existing_browser.hwnd), u32_b)
                        if u32_b is not None
                        else int(existing_browser.hwnd)
                    )
                    reused_browser_title = existing_browser.title
                    if u32_b is not None:
                        _focus_window_hwnd(u32_b, reused_browser_hwnd, kernel32=kernel32)
            cdp_result: dict[str, Any] = {
                "ok": True,
                "success": True,
                "url": str(nav.get("url") or target_url),
                "title": str(nav.get("title") or ""),
                "browser": b_clean,
                "status": "navigated",
                "launched_app": False,
                "routed_to_installed_app": False,
                "reused_browser_window": True,
                "hwnd": reused_browser_hwnd,
                "window_hwnd": reused_browser_hwnd,
                "window_title": str(nav.get("title") or "") or reused_browser_title,
                "target_id": nav.get("target_id"),
                "is_pwa": bool(nav.get("is_pwa", False)),
                "cdp_used": True,
                "before_url": nav.get("before_url"),
                "after_url": nav.get("after_url"),
                "normalized_target": norm_target,
            }
            if isinstance(nav.get("state_diff"), dict):
                cdp_result["state_diff"] = nav["state_diff"]
            return cdp_result

    browser_exe: str | None = None
    env_map = dict(os.environ) if env is None else env
    if b_clean in _KNOWN_APPS:
        spec = _KNOWN_APPS[b_clean]
        for cand in spec.candidate_exes:
            found = which_fn(cand)
            if found:
                browser_exe = found
                break
        if not browser_exe:
            for p in spec.install_paths:
                exp = _expand_env_vars(p, env_map)
                if Path(exp).is_file():
                    browser_exe = exp
                    break
    else:
        browser_exe = which_fn(b_clean) or which_fn(f"{b_clean}.exe")

    if not browser_exe:
        raise ValueError(f"Browser '{browser}' could not be resolved to an executable.")

    if allow_live_win32:
        u32_b = _get_u32_optional()
        existing_browser = _find_existing_browser_window(
            _enumerate_current_windows(),
            browser_name=b_clean,
            url_tokens=url_tokens,
            user32=u32_b,
        )
        if existing_browser is not None:
            reused_browser_hwnd = (
                _resolve_root_window_hwnd(int(existing_browser.hwnd), u32_b)
                if u32_b is not None
                else int(existing_browser.hwnd)
            )
            reused_browser_title = existing_browser.title
            if u32_b is not None:
                _focus_window_hwnd(u32_b, reused_browser_hwnd, kernel32=kernel32)

    # Never pass --new-window: reuse the existing browser window/tab bar cleanly.
    effective_popen([browser_exe, target_url], **_build_gui_popen_kwargs())
    return {
        "ok": True,
        "success": True,
        "url": target_url,
        "browser": b_clean,
        "status": "opened",
        "launched_app": False,
        "routed_to_installed_app": False,
        "reused_browser_window": reused_browser_hwnd is not None,
        "hwnd": reused_browser_hwnd,
        "window_hwnd": reused_browser_hwnd,
        "window_title": reused_browser_title,
        "normalized_target": norm_target,
    }


def _get_wmi_brightness(
    powershell_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> int:
    """Read current display brightness percentage (0..100) via WmiMonitorBrightness."""
    ps_exe = shutil.which("powershell.exe") or "powershell.exe"
    proc = run_powershell_hidden(
        (
            "(Get-CimInstance -Namespace root\\wmi "
            "-ClassName WmiMonitorBrightness).CurrentBrightness"
        ),
        timeout=8.0,
        ps_exe=ps_exe,
        runner=powershell_runner,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"Failed to query display brightness: {err}")
    val = proc.stdout.strip()
    if not val:
        raise RuntimeError("No brightness value returned (display may not support WMI brightness).")
    return int(val.split()[0])


def _set_wmi_brightness(
    level: int,
    powershell_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> None:
    """Set display brightness percentage (0..100) via WmiMonitorBrightnessMethods."""
    clamped = max(0, min(100, int(level)))
    ps_exe = shutil.which("powershell.exe") or "powershell.exe"
    script = (
        "$b = Get-CimInstance -Namespace root\\wmi -ClassName WmiMonitorBrightnessMethods; "
        "if ($b) { "
        f"Invoke-CimMethod -InputObject $b -MethodName WmiSetBrightness "
        f"-Arguments @{{Timeout = 1; Brightness = [uint32]{clamped}}} "
        "} else { throw 'WmiMonitorBrightnessMethods not available' }"
    )
    proc = run_powershell_hidden(
        script,
        timeout=8.0,
        ps_exe=ps_exe,
        runner=powershell_runner,
        check=False,
    )
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"Failed to set display brightness: {err}")


def _resolve_window_hwnd(
    hwnd: int | None = None,
    app_name: str | None = None,
    title: str | None = None,
    *,
    user32: Any = None,
    kernel32: Any = None,
    window_enumerator: Callable[[], list[EnumeratedWindow]] | None = None,
) -> int:
    """Resolve a window HWND from explicit handle, title/app query, or foreground window."""
    if hwnd is not None:
        if isinstance(hwnd, bool) or not isinstance(hwnd, int):
            raise ValueError("hwnd must be an integer.")
        return int(hwnd)

    u32 = _get_win32_user32(user32)

    if app_name or title:
        if window_enumerator is not None:
            windows = list(window_enumerator())
        else:
            k32 = _get_win32_kernel32(kernel32)
            windows = _enumerate_windows_ctypes(u32, k32)

        target_title = (title or "").lower().strip()
        target_app = (app_name or "").lower().strip()

        if target_app and not target_title:
            normalized_app = " ".join(target_app.split())
            canonical = _APP_ALIASES.get(normalized_app)
            if canonical is not None:
                spec = _KNOWN_APPS[canonical]
                spec_matches = [win for win in windows if _window_matches_spec(win, spec)]
                if spec_matches:
                    best = _pick_best_matching_window(spec_matches, u32)
                    return _resolve_root_window_hwnd(int(best.hwnd), u32)

        fallback_matches: list[EnumeratedWindow] = []
        for win in windows:
            if not win.is_visible:
                continue
            w_title = (win.title or "").lower()
            w_proc = (win.process_name or "").lower()
            if target_title and target_title in w_title:
                fallback_matches.append(win)
            elif target_app and (target_app in w_proc or target_app in w_title):
                fallback_matches.append(win)

        if fallback_matches:
            query_norm = normalize_launch_target(target_app or target_title)
            is_browser_q = query_norm in _BROWSER_CANONICAL_NAMES or _is_browser_tab_window_title(
                target_title
            )
            best = _pick_best_matching_window(
                fallback_matches,
                u32,
                prefer_standalone_pwa=not is_browser_q,
                prefer_browser_window=is_browser_q,
            )
            return _resolve_root_window_hwnd(int(best.hwnd), u32)

        raise ValueError(
            f"No visible window found matching app_name={app_name!r}, title={title!r}."
        )

    if hasattr(u32, "GetForegroundWindow"):
        fg = u32.GetForegroundWindow()
        if fg:
            return _resolve_root_window_hwnd(int(fg), u32)

    raise RuntimeError("No active foreground window found.")


class _OSRECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class _OSMONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_uint32),
        ("rcMonitor", _OSRECT),
        ("rcWork", _OSRECT),
        ("dwFlags", ctypes.c_uint32),
    ]


def _get_monitor_work_area_os(hwnd: int, user32: Any) -> tuple[int, int, int, int]:
    """Return (left, top, right, bottom) work area of the window's monitor."""
    h_monitor = 0
    if hasattr(user32, "MonitorFromWindow"):
        with contextlib.suppress(Exception):
            h_monitor = user32.MonitorFromWindow(hwnd, MONITOR_DEFAULTTONEAREST)
    mi = _OSMONITORINFO()
    mi.cbSize = ctypes.sizeof(_OSMONITORINFO)
    if h_monitor and hasattr(user32, "GetMonitorInfoW"):
        with contextlib.suppress(Exception):
            user32.GetMonitorInfoW(h_monitor, ctypes.byref(mi))
    left = int(mi.rcWork.left)
    top = int(mi.rcWork.top)
    right = int(mi.rcWork.right)
    bottom = int(mi.rcWork.bottom)
    if right <= left:
        right = left + 1920
    if bottom <= top:
        bottom = top + 1080
    return left, top, right, bottom


def _read_window_title_os(hwnd: int, user32: Any, focus_backend: Any = None) -> str | None:
    if user32 is not None and hasattr(user32, "GetWindowTextLengthW") and hasattr(
        user32, "GetWindowTextW"
    ):
        with contextlib.suppress(Exception):
            length = int(user32.GetWindowTextLengthW(hwnd) or 0)
            if length > 0:
                buf = ctypes.create_unicode_buffer(length + 1)
                copied = user32.GetWindowTextW(hwnd, buf, length + 1)
                val = buf if isinstance(copied, str) else buf.value
                if isinstance(copied, str):
                    val = copied
                if val:
                    return str(val)
    if focus_backend is not None and hasattr(focus_backend, "get_window_context"):
        with contextlib.suppress(Exception):
            ctx = focus_backend.get_window_context(hwnd)
            if ctx is not None and ctx.title:
                return ctx.title
    return None


def _get_dwm_frame_insets_os(hwnd: int, u32: Any) -> tuple[int, int, int, int]:
    """Return invisible DWM border insets (left, top, right, bottom) to compensate MoveWindow."""
    if sys.platform != "win32" or not isinstance(u32, ctypes.CDLL):
        return (0, 0, 0, 0)
    if not hasattr(u32, "GetWindowRect"):
        return (0, 0, 0, 0)
    if hasattr(u32, "IsIconic") and bool(u32.IsIconic(hwnd)):
        return (0, 0, 0, 0)
    if hasattr(u32, "IsZoomed") and bool(u32.IsZoomed(hwnd)):
        return (0, 0, 0, 0)
    with contextlib.suppress(Exception):
        dwmapi = _get_win32_dwmapi()
        if dwmapi is None or not hasattr(dwmapi, "DwmGetWindowAttribute"):
            return (0, 0, 0, 0)
        win_rect = _OSRECT()
        frame_rect = _OSRECT()
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
        return (
            max(0, min(32, int(frame_rect.left) - int(win_rect.left))),
            max(0, min(32, int(frame_rect.top) - int(win_rect.top))),
            max(0, min(32, int(win_rect.right) - int(frame_rect.right))),
            max(0, min(32, int(win_rect.bottom) - int(frame_rect.bottom))),
        )
    return (0, 0, 0, 0)


def _raise_window_z_order_os(
    u32: Any,
    hwnd: int,
    *,
    force_topmost_bump: bool = False,
) -> None:
    """Promote *hwnd* to the top of the normal Z-order via SetWindowPos / BringWindowToTop."""
    if u32 is None or not hwnd:
        return
    flags = SWP_NOMOVE | SWP_NOSIZE | SWP_SHOWWINDOW
    if hasattr(u32, "SetWindowPos"):
        is_live = isinstance(u32, ctypes.CDLL)
        with contextlib.suppress(Exception):
            if force_topmost_bump:
                topmost_arg = ctypes.c_void_p(HWND_TOPMOST) if is_live else HWND_TOPMOST
                notopmost_arg = ctypes.c_void_p(HWND_NOTOPMOST) if is_live else HWND_NOTOPMOST
                u32.SetWindowPos(hwnd, topmost_arg, 0, 0, 0, 0, flags)
                u32.SetWindowPos(hwnd, notopmost_arg, 0, 0, 0, 0, flags)
            else:
                top_arg = ctypes.c_void_p(HWND_TOP) if is_live else HWND_TOP
                u32.SetWindowPos(hwnd, top_arg, 0, 0, 0, 0, flags)
    if hasattr(u32, "BringWindowToTop"):
        with contextlib.suppress(Exception):
            u32.BringWindowToTop(hwnd)


def _move_window_snap_os(
    u32: Any,
    hwnd: int,
    x: int,
    y: int,
    w: int,
    h: int,
    *,
    guard: PreconditionGuard | None = None,
    work_area: tuple[int, int, int, int] | None = None,
) -> None:
    """Restore/unmaximize target window, MoveWindow with DWM compensation, and raise Z-order."""
    is_live = isinstance(u32, ctypes.CDLL)
    was_zoomed = False
    if is_live:
        if hasattr(u32, "IsIconic") and bool(u32.IsIconic(hwnd)) and hasattr(u32, "ShowWindow"):
            u32.ShowWindow(hwnd, SW_RESTORE)
        if hasattr(u32, "IsZoomed") and bool(u32.IsZoomed(hwnd)) and hasattr(u32, "ShowWindow"):
            was_zoomed = True
            u32.ShowWindow(hwnd, SW_SHOWNORMAL)
    elif hasattr(u32, "ShowWindow"):
        u32.ShowWindow(hwnd, SW_RESTORE)

    if guard is not None:
        guard.assert_ready("snap_move_window")

    if hasattr(u32, "MoveWindow"):
        il, it, ir, ib = _get_dwm_frame_insets_os(hwnd, u32)
        u32.MoveWindow(hwnd, x - il, y - it, w + il + ir, h + it + ib, True)
        if is_live and was_zoomed and (il, it, ir, ib) == (0, 0, 0, 0):
            nil, nit, nir, nib = _get_dwm_frame_insets_os(hwnd, u32)
            if (nil, nit, nir, nib) != (0, 0, 0, 0):
                u32.MoveWindow(hwnd, x - nil, y - nit, w + nil + nir, h + nit + nib, True)
        if not is_live:
            try:
                moved = getattr(u32, "_clea_last_moved_rects", None)
                if not isinstance(moved, dict):
                    moved = {}
                    u32._clea_last_moved_rects = moved
                moved[int(hwnd)] = (x, y, x + w, y + h)
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
            except Exception:
                pass
    _raise_window_z_order_os(u32, hwnd)


def control_system(
    action: str,
    *,
    level: int | None = None,
    step: int | None = None,
    page: str | None = None,
    uri: str | None = None,
    hwnd: int | None = None,
    app_name: str | None = None,
    title: str | None = None,
    window_title: str | None = None,
    title_query: str | None = None,
    window_action: str | None = None,
    user32: Any = None,
    kernel32: Any = None,
    window_enumerator: Callable[[], list[EnumeratedWindow]] | None = None,
    wmi_brightness_getter: Callable[[], int] | None = None,
    wmi_brightness_setter: Callable[[int], None] | None = None,
    startfile_fn: Callable[[str], Any] | None = None,
    popen_fn: Callable[..., Any] = subprocess.Popen,
    powershell_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    focus_backend: FocusPreconditionBackend | None = None,
    verifier: ActionVerifier | None = None,
    verify_timeout_s: float = 0.6,
    view_fingerprint_fn: Callable[..., Any] | None = None,
    settle_wait_s: float | None = None,
    cancel_event: threading.Event | None = None,
    sleep_fn: Callable[[float], None] | None = None,
    verify_view: bool | None = None,
    **_extra: Any,
) -> dict[str, Any]:
    """Control Windows display brightness, window state, screen lock, or settings navigation."""
    if not isinstance(action, str):
        raise ValueError("action must be a string.")
    normalized = action.strip().lower()

    if normalized in ("window", "window_action") and window_action:
        w_act = window_action.strip().lower()
        if w_act in ("minimize", "maximize", "restore", "close"):
            normalized = f"{w_act}_window"
        elif w_act in (
            "minimize_window",
            "maximize_window",
            "restore_window",
            "close_window",
            "snap_left",
            "snap_right",
            "snap_left_window",
            "snap_right_window",
        ):
            normalized = w_act
        else:
            normalized = w_act

    if normalized not in VALID_SYSTEM_ACTIONS:
        actions_str = ", ".join(VALID_SYSTEM_ACTIONS)
        raise ValueError(f"Unsupported system action {action!r}. Valid actions: {actions_str}.")

    # 1. Brightness controls
    if normalized == "get_brightness":
        getter = wmi_brightness_getter or (
            lambda: _get_wmi_brightness(powershell_runner=powershell_runner)
        )
        curr = getter()
        return {
            "ok": True,
            "success": True,
            "action": "get_brightness",
            "level": curr,
        }

    if normalized == "set_brightness":
        if level is None:
            raise ValueError("level (0..100) is required when action is 'set_brightness'.")
        if isinstance(level, bool):
            raise ValueError("level must be an integer between 0 and 100.")
        if isinstance(level, (int, float)) and not math.isfinite(level):
            raise ValueError("level must be a finite number between 0 and 100.")
        try:
            parsed_level = int(level)
        except (TypeError, ValueError, OverflowError) as exc:
            raise ValueError("level must be an integer between 0 and 100.") from exc
        clamped = max(0, min(100, parsed_level))
        setter = wmi_brightness_setter or (
            lambda lvl: _set_wmi_brightness(lvl, powershell_runner=powershell_runner)
        )
        setter(clamped)
        return {
            "ok": True,
            "success": True,
            "action": "set_brightness",
            "level": clamped,
        }

    if normalized in ("step_brightness", "brightness_up", "brightness_down"):
        if step is not None:
            if isinstance(step, bool):
                raise ValueError("step must be an integer.")
            if isinstance(step, (int, float)) and not math.isfinite(step):
                raise ValueError("step must be a finite integer.")
            try:
                st = int(step)
            except (TypeError, ValueError, OverflowError) as exc:
                raise ValueError("step must be an integer.") from exc
        else:
            st = -10 if normalized == "brightness_down" else 10

        getter = wmi_brightness_getter or (
            lambda: _get_wmi_brightness(powershell_runner=powershell_runner)
        )
        curr = getter()
        new_l = max(0, min(100, curr + st))
        setter = wmi_brightness_setter or (
            lambda lvl: _set_wmi_brightness(lvl, powershell_runner=powershell_runner)
        )
        setter(new_l)
        return {
            "ok": True,
            "success": True,
            "action": "step_brightness",
            "level": new_l,
            "previous_level": curr,
            "step": st,
        }

    # 2. Deterministic Win32 window layout inspection
    if normalized in ("inspect_window_layout", "inspect_layout", "layout"):
        layout = inspect_window_layout(
            user32=user32,
            kernel32=kernel32,
            focus_backend=focus_backend,
        )
        return {
            "success": True,
            "action": normalized,
            **layout,
        }

    # 3. Window actions
    if normalized in (
        "minimize_window",
        "minimize",
        "maximize_window",
        "maximize",
        "restore_window",
        "restore",
        "close_window",
        "close",
        "snap_left",
        "snap_left_window",
        "snap_right",
        "snap_right_window",
    ):
        effective_title = (
            title
            if title is not None
            else (window_title if window_title is not None else title_query)
        )
        target_hwnd = _resolve_window_hwnd(
            hwnd=hwnd,
            app_name=app_name,
            title=effective_title,
            user32=user32,
            kernel32=kernel32,
            window_enumerator=window_enumerator,
        )
        u32 = _get_win32_user32(user32)
        is_snap_action = normalized in (
            "snap_left",
            "snap_left_window",
            "snap_right",
            "snap_right_window",
        )
        inferred_focus_backend = focus_backend
        if inferred_focus_backend is None and verifier is None:
            candidate_backend = getattr(u32, "backend", None)
            if isinstance(candidate_backend, FocusPreconditionBackend):
                inferred_focus_backend = candidate_backend
            elif is_snap_action and user32 is not None:
                inferred_focus_backend = Win32FocusPreconditionBackend(user32=u32)

        should_verify = (
            is_snap_action
            or verifier is not None
            or inferred_focus_backend is not None
            or (user32 is None and window_enumerator is None)
        )
        active_verifier = (
            verifier
            if verifier is not None
            else (ActionVerifier(focus_backend=inferred_focus_backend) if should_verify else None)
        )
        active_focus_backend = (
            inferred_focus_backend
            if inferred_focus_backend is not None
            else (active_verifier._get_focus_backend() if active_verifier is not None else None)
        )
        resolved_title = _read_window_title_os(target_hwnd, u32, active_focus_backend)

        if is_snap_action:
            is_left = normalized in ("snap_left", "snap_left_window")
            canon_snap = "snap_left" if is_left else "snap_right"
            snap_side = "left" if is_left else "right"
            left, top, right, bottom = _get_monitor_work_area_os(target_hwnd, u32)
            if (
                active_focus_backend is not None
                and user32 is not None
                and not hasattr(u32, "GetMonitorInfoW")
            ):
                backend_work = active_focus_backend.get_monitor_work_area(target_hwnd)
                if backend_work is not None:
                    left, top, right, bottom = backend_work
            work_width = right - left
            work_height = bottom - top
            half_width = work_width // 2
            if is_left:
                x, y, w, h = left, top, half_width, work_height
            else:
                x, y, w, h = left + half_width, top, work_width - half_width, work_height

            if active_focus_backend is not None:
                try:
                    with PreconditionGuard(
                        active_focus_backend,
                        expected_hwnd=target_hwnd,
                        require_foreground=False,
                        allow_minimized=True,
                    ) as guard:
                        guard.assert_ready("snap_restore")
                        _move_window_snap_os(
                            u32,
                            target_hwnd,
                            x,
                            y,
                            w,
                            h,
                            guard=guard,
                            work_area=(left, top, right, bottom),
                        )
                        guard.assert_ready("snap_focus")
                        if hasattr(u32, "SetForegroundWindow"):
                            _focus_window_hwnd(u32, target_hwnd, restore_if_iconic=False)
                except PreconditionFailedError as exc:
                    payload = exc.to_abort_payload()
                    payload["success"] = False
                    payload["action"] = canon_snap
                    payload["hwnd"] = target_hwnd
                    if resolved_title:
                        payload["window_title"] = resolved_title
                    return payload
            else:
                _move_window_snap_os(
                    u32, target_hwnd, x, y, w, h, work_area=(left, top, right, bottom)
                )
                if hasattr(u32, "SetForegroundWindow"):
                    _focus_window_hwnd(u32, target_hwnd, restore_if_iconic=False)

            res_snap: dict[str, Any] = {
                "ok": True,
                "success": True,
                "action": canon_snap,
                "hwnd": target_hwnd,
                "rect": {"x": x, "y": y, "width": w, "height": h},
            }
            if resolved_title:
                res_snap["window_title"] = resolved_title
            if active_verifier is not None:
                outcome = active_verifier.verify_window_state(
                    target_hwnd,
                    expected_snap=snap_side,
                    timeout_s=verify_timeout_s,
                )
                if not outcome.verified:
                    if outcome.occluded_by_unrelated_window:
                        _raise_window_z_order_os(u32, target_hwnd, force_topmost_bump=True)
                        if hasattr(u32, "SetForegroundWindow"):
                            _focus_window_hwnd(u32, target_hwnd, restore_if_iconic=False)
                        outcome = active_verifier.verify_window_state(
                            target_hwnd,
                            expected_snap=snap_side,
                            timeout_s=verify_timeout_s,
                        )
                    elif isinstance(u32, ctypes.CDLL):
                        _move_window_snap_os(
                            u32, target_hwnd, x, y, w, h, work_area=(left, top, right, bottom)
                        )
                        _raise_window_z_order_os(u32, target_hwnd, force_topmost_bump=True)
                        if hasattr(u32, "SetForegroundWindow"):
                            _focus_window_hwnd(u32, target_hwnd, restore_if_iconic=False)
                        outcome = active_verifier.verify_window_state(
                            target_hwnd,
                            expected_snap=snap_side,
                            timeout_s=verify_timeout_s,
                        )
                res_snap["verification"] = outcome.to_dict()
                res_snap["verified"] = outcome.verified
                if outcome.occluded_by_unrelated_window:
                    res_snap["occluded_by_unrelated_window"] = True
                    res_snap["occluding_windows"] = list(outcome.occluding_windows)
            invalidate_last_screen_geometry(f"control_system:{canon_snap}")
            return res_snap

        if normalized in ("minimize_window", "minimize"):
            u32.ShowWindow(target_hwnd, SW_MINIMIZE)
            invalidate_last_screen_geometry("control_system:minimize_window")
            res_win: dict[str, Any] = {
                "ok": True,
                "success": True,
                "action": "minimize_window",
                "hwnd": target_hwnd,
            }
            if active_verifier is not None:
                res_win["verification"] = active_verifier.verify_window_state(
                    target_hwnd,
                    expected_state="minimized",
                    timeout_s=verify_timeout_s,
                ).to_dict()
            return res_win
        if normalized in ("maximize_window", "maximize"):
            u32.ShowWindow(target_hwnd, SW_MAXIMIZE)
            invalidate_last_screen_geometry("control_system:maximize_window")
            res_win = {
                "ok": True,
                "success": True,
                "action": "maximize_window",
                "hwnd": target_hwnd,
            }
            if active_verifier is not None:
                res_win["verification"] = active_verifier.verify_window_state(
                    target_hwnd,
                    expected_state="maximized",
                    timeout_s=verify_timeout_s,
                ).to_dict()
            return res_win
        if normalized in ("restore_window", "restore"):
            u32.ShowWindow(target_hwnd, SW_RESTORE)
            invalidate_last_screen_geometry("control_system:restore_window")
            res_win = {
                "ok": True,
                "success": True,
                "action": "restore_window",
                "hwnd": target_hwnd,
            }
            if active_verifier is not None:
                res_win["verification"] = active_verifier.verify_window_state(
                    target_hwnd,
                    expected_state="restored",
                    timeout_s=verify_timeout_s,
                ).to_dict()
            return res_win
        if normalized in ("close_window", "close"):
            u32.PostMessageW(target_hwnd, WM_CLOSE, 0, 0)
            invalidate_last_screen_geometry("control_system:close_window")
            res_win = {
                "ok": True,
                "success": True,
                "action": "close_window",
                "hwnd": target_hwnd,
            }
            if active_verifier is not None:
                res_win["verification"] = active_verifier.verify_window_state(
                    target_hwnd,
                    expected_state="closed",
                    timeout_s=verify_timeout_s,
                ).to_dict()
            return res_win

    # 3. Screen locking
    if normalized in ("lock_screen", "lock"):
        u32 = _get_win32_user32(user32)
        if hasattr(u32, "LockWorkStation"):
            u32.LockWorkStation()
        return {
            "ok": True,
            "success": True,
            "action": "lock_screen",
        }

    # 4. Settings navigation
    if normalized in ("open_settings", "settings"):
        target_input = (uri or page or "").strip()
        if not target_input:
            target_uri = "ms-settings:"
        elif target_input.startswith("ms-settings:"):
            target_uri = target_input
        else:
            target_uri = f"ms-settings:{target_input}"

        if target_uri not in _ALLOWED_MS_SETTINGS:
            raise ValueError(
                f"ms-settings URI '{target_uri}' is not in the allowlist. "
                f"Allowed settings: {', '.join(sorted(_ALLOWED_MS_SETTINGS))}."
            )

        should_verify_view = (
            view_fingerprint_fn is not None
            or verify_view is True
            or (
                verify_view is None
                and startfile_fn is None
                and popen_fn is subprocess.Popen
                and sys.platform == "win32"
            )
        )

        before_hwnd, before_title = (0, "")
        before_fp = None
        if should_verify_view:
            with contextlib.suppress(Exception):
                before_hwnd, before_title = _read_foreground_hwnd_and_title(
                    user32=user32,
                    focus_backend=focus_backend,
                )
            before_fp = _capture_view_fp_safe(
                before_hwnd or None,
                view_fingerprint_fn=view_fingerprint_fn,
            )

        starter = startfile_fn or getattr(os, "startfile", None)
        if starter is not None:
            starter(target_uri)
        else:
            popen_fn(
                ["explorer.exe", target_uri],
                shell=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )

        if not should_verify_view:
            return {
                "ok": True,
                "success": True,
                "action": "open_settings",
                "uri": target_uri,
            }

        eff_settle = (
            settle_wait_s
            if settle_wait_s is not None
            else (0.2 if view_fingerprint_fn is None else 0.0)
        )
        _cancellable_settle_wait_safe(
            eff_settle,
            cancel_event=cancel_event,
            sleep_fn=sleep_fn,
        )

        after_hwnd, after_title = (0, "")
        with contextlib.suppress(Exception):
            after_hwnd, after_title = _read_foreground_hwnd_and_title(
                user32=user32,
                focus_backend=focus_backend,
            )
        after_fp = _capture_view_fp_safe(
            after_hwnd or before_hwnd or None,
            view_fingerprint_fn=view_fingerprint_fn,
        )
        view_diff = _compare_view_fps_safe(before_fp, after_fp)
        window_changed = bool(
            (before_hwnd > 0 and after_hwnd > 0 and after_hwnd != before_hwnd)
            or (before_title and after_title and after_title != before_title)
        )
        view_changed = bool(view_diff.get("view_changed", False))
        visual_delta = float(view_diff.get("visual_delta", 0.0))
        heading_before = str(view_diff.get("heading_before", "") or "")
        heading_after = str(view_diff.get("heading_after", "") or "")
        heading_changed = bool(view_diff.get("heading_changed", False))
        goal_verified = bool(view_changed or window_changed)
        verification_scope = (
            "in_app_view"
            if view_changed
            else ("window_focus" if window_changed else "dispatch_only")
        )
        return {
            "ok": True,
            "success": True,
            "action": "open_settings",
            "uri": target_uri,
            "window_changed": window_changed,
            "view_changed": view_changed,
            "visual_delta": visual_delta,
            "heading_before": heading_before,
            "heading_after": heading_after,
            "heading_changed": heading_changed,
            "goal_verified": goal_verified,
            "verification_scope": verification_scope,
            "state_diff": {
                "changed": goal_verified,
                "window_changed": window_changed,
                "view_changed": view_changed,
                "visual_delta": visual_delta,
                "heading_before": heading_before,
                "heading_after": heading_after,
                "heading_changed": heading_changed,
            },
        }

    raise ValueError(f"Unhandled system action: {action!r}")


# -------------------------------------------------------------- Windows UI Automation & GUI Input


INPUT_MOUSE: int = 0
INPUT_KEYBOARD: int = 1
INPUT_HARDWARE: int = 2

MOUSEEVENTF_MOVE: int = 0x0001
MOUSEEVENTF_LEFTDOWN: int = 0x0002
MOUSEEVENTF_LEFTUP: int = 0x0004
MOUSEEVENTF_RIGHTDOWN: int = 0x0008
MOUSEEVENTF_RIGHTUP: int = 0x0010
MOUSEEVENTF_WHEEL: int = 0x0800
MOUSEEVENTF_ABSOLUTE: int = 0x8000

KEYEVENTF_UNICODE: int = 0x0004

MAX_GUI_TYPE_TEXT_LEN: int = 2000
MAX_UI_INSPECT_ELEMENTS: int = 150
UI_AUTOMATION_TIMEOUT_SECONDS: float = 5.0
UIA_MAX_DEPTH: int = 8
UIA_MAX_NODES: int = 250

SM_CXSCREEN_OS: int = 0
SM_CYSCREEN_OS: int = 1
SM_XVIRTUALSCREEN_OS: int = 76
SM_YVIRTUALSCREEN_OS: int = 77
SM_CXVIRTUALSCREEN_OS: int = 78
SM_CYVIRTUALSCREEN_OS: int = 79

VALID_GUI_ACTIONS: frozenset[str] = frozenset(
    {
        "click_at",
        "double_click",
        "right_click",
        "move",
        "type_text",
        "press_keys",
        "scroll",
        "wait",
        "navigate_back",
        "navigate_forward",
        "click",
        "type",
        "press",
    }
)
VALID_COORD_SPACES: frozenset[str] = frozenset({"normalized_1000", "image", "screen"})
VALID_UI_ACT_ACTIONS: frozenset[str] = frozenset(
    {"click", "double_click", "right_click", "set_value", "type", "clear", "press"}
)
VALID_CLICK_BUTTONS: frozenset[str] = frozenset({"left", "right", "double"})

_SCREEN_GEOMETRY_LOCK = threading.RLock()
_LAST_SCREEN_GEOMETRY: dict[str, Any] | None = None


def set_last_screen_geometry(
    *,
    image_width: int,
    image_height: int,
    screen_width: int,
    screen_height: int,
    virtual_left: int = 0,
    virtual_top: int = 0,
    dpi_scale: float = 1.0,
    coord_space: str = "normalized_1000",
    target_hwnd: int | None = None,
    hwnd: int | None = None,
    window_bounds: tuple[int, int, int, int] | list[int] | None = None,
    capture_bounds: tuple[int, int, int, int] | list[int] | None = None,
    capture_id: str | None = None,
    capture_seq: int | None = None,
) -> dict[str, Any]:
    """Store the latest screenshot geometry so subsequent gui_action calls scale accurately."""
    global _LAST_SCREEN_GEOMETRY
    geom: dict[str, Any] = {
        "image_width": max(1, int(image_width)),
        "image_height": max(1, int(image_height)),
        "screen_width": max(1, int(screen_width)),
        "screen_height": max(1, int(screen_height)),
        "virtual_left": int(virtual_left),
        "virtual_top": int(virtual_top),
        "dpi_scale": float(dpi_scale) if dpi_scale and dpi_scale > 0 else 1.0,
        "coord_space": str(coord_space or "normalized_1000"),
        "captured_at": time.monotonic(),
    }
    resolved_hwnd = (
        int(hwnd)
        if hwnd is not None and int(hwnd) > 0
        else (int(target_hwnd) if target_hwnd is not None and int(target_hwnd) > 0 else None)
    )
    if resolved_hwnd is not None:
        geom["hwnd"] = resolved_hwnd
        geom["target_hwnd"] = resolved_hwnd
    resolved_bounds = (
        capture_bounds
        if capture_bounds is not None and len(capture_bounds) == 4
        else (window_bounds if window_bounds is not None and len(window_bounds) == 4 else None)
    )
    if resolved_bounds is not None:
        bounds_tuple = tuple(int(v) for v in resolved_bounds)
        geom["capture_bounds"] = bounds_tuple
        geom["window_bounds"] = bounds_tuple
    if capture_id is not None and str(capture_id).strip():
        geom["capture_id"] = str(capture_id).strip()
    if capture_seq is not None:
        geom["capture_seq"] = int(capture_seq)
    with _SCREEN_GEOMETRY_LOCK:
        _LAST_SCREEN_GEOMETRY = geom
    return dict(geom)


def _is_valid_recent_screen_geometry(
    geom: dict[str, Any] | None,
    *,
    max_age_s: float = _SCREEN_GEOMETRY_MAX_AGE_S,
) -> bool:
    """Return True if *geom* contains positive screen dimensions and is not expired."""
    if not isinstance(geom, dict):
        return False
    sw = geom.get("screen_width")
    sh = geom.get("screen_height")
    if not isinstance(sw, int) or not isinstance(sh, int) or sw <= 0 or sh <= 0:
        return False
    captured_at = geom.get("captured_at")
    if (
        isinstance(captured_at, (int, float))
        and (time.monotonic() - float(captured_at)) > max_age_s
    ):
        return False
    return True


def _query_live_window_bounds(
    hwnd: int,
    *,
    window_bounds_fn: Callable[[int], tuple[int, int, int, int] | None] | None = None,
    active_guard: PreconditionGuard | None = None,
    focus_backend: FocusPreconditionBackend | None = None,
    user32: Any = None,
) -> tuple[int, int, int, int] | None:
    """Query live physical DWM extended frame bounds for *hwnd* without throwing."""
    if not hwnd or hwnd <= 0:
        return None
    if window_bounds_fn is not None:
        with contextlib.suppress(Exception):
            res = window_bounds_fn(hwnd)
            if isinstance(res, (tuple, list)) and len(res) == 4:
                return tuple(int(v) for v in res)
        return None
    guard_backend = getattr(active_guard, "backend", None) or getattr(
        active_guard, "_backend", None
    )
    for backend_obj in (guard_backend, focus_backend):
        if backend_obj is not None and hasattr(backend_obj, "get_extended_frame_bounds"):
            with contextlib.suppress(Exception):
                res = backend_obj.get_extended_frame_bounds(hwnd)
                if isinstance(res, (tuple, list)) and len(res) == 4:
                    return tuple(int(v) for v in res)
    if user32 is not None and not isinstance(user32, ctypes.CDLL):
        if hasattr(user32, "get_extended_frame_bounds"):
            with contextlib.suppress(Exception):
                res = user32.get_extended_frame_bounds(hwnd)
                if isinstance(res, (tuple, list)) and len(res) == 4:
                    return tuple(int(v) for v in res)
        if hasattr(user32, "GetWindowRect"):
            with contextlib.suppress(Exception):
                rect = _OSRECT()
                if user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    if rect.right > rect.left and rect.bottom > rect.top:
                        return (
                            int(rect.left),
                            int(rect.top),
                            int(rect.right),
                            int(rect.bottom),
                        )
        return None
    if sys.platform == "win32":
        with contextlib.suppress(Exception):
            from clea.surfaces.voice.win32 import get_window_bounds

            return get_window_bounds(hwnd)
    return None


def get_last_screen_geometry() -> dict[str, Any] | None:
    """Return a copy of the latest recorded screenshot geometry, if available."""
    with _SCREEN_GEOMETRY_LOCK:
        return dict(_LAST_SCREEN_GEOMETRY) if _LAST_SCREEN_GEOMETRY is not None else None


def clear_last_screen_geometry() -> None:
    """Clear the cached screenshot geometry."""
    global _LAST_SCREEN_GEOMETRY
    with _SCREEN_GEOMETRY_LOCK:
        _LAST_SCREEN_GEOMETRY = None


def invalidate_last_screen_geometry(reason: str = "") -> None:
    """Invalidate cached screenshot geometry after window layout or focus mutations."""
    if reason:
        logger.debug("Invalidating cached screen geometry: %s", reason)
    clear_last_screen_geometry()


def _query_virtual_screen_metrics(user32: Any = None) -> tuple[int, int, int, int]:
    """Return physical (virtual_left, virtual_top, screen_width, screen_height)."""
    u32 = user32
    if u32 is None and sys.platform == "win32":
        with contextlib.suppress(Exception):
            u32 = _get_win32_user32(None)
    if u32 is not None and hasattr(u32, "GetSystemMetrics"):
        with contextlib.suppress(Exception):
            v_w = int(u32.GetSystemMetrics(SM_CXVIRTUALSCREEN_OS) or 0)
            v_h = int(u32.GetSystemMetrics(SM_CYVIRTUALSCREEN_OS) or 0)
            if v_w > 0 and v_h > 0:
                v_l = int(u32.GetSystemMetrics(SM_XVIRTUALSCREEN_OS) or 0)
                v_t = int(u32.GetSystemMetrics(SM_YVIRTUALSCREEN_OS) or 0)
                return (v_l, v_t, v_w, v_h)
            p_w = int(u32.GetSystemMetrics(SM_CXSCREEN_OS) or 0)
            p_h = int(u32.GetSystemMetrics(SM_CYSCREEN_OS) or 0)
            if p_w > 0 and p_h > 0:
                return (0, 0, p_w, p_h)
    return (0, 0, 1920, 1080)


def convert_coordinates_to_screen(
    x: int | float,
    y: int | float,
    *,
    coord_space: str | None = None,
    screen_width: int | None = None,
    screen_height: int | None = None,
    virtual_left: int | None = None,
    virtual_top: int | None = None,
    image_width: int | None = None,
    image_height: int | None = None,
    dpi_scale: float | None = None,
    user32: Any = None,
) -> tuple[int, int, dict[str, Any]]:
    """Convert ``(x, y)`` from ``normalized_1000``, ``image``, or ``screen`` to physical pixels."""
    space = (coord_space or "screen").strip().lower()
    if space not in VALID_COORD_SPACES:
        raise ValueError(
            f"Unsupported coord_space {coord_space!r}. Must be one of {sorted(VALID_COORD_SPACES)}."
        )
    if (
        isinstance(x, bool)
        or isinstance(y, bool)
        or not isinstance(x, (int, float))
        or not isinstance(y, (int, float))
    ):
        raise ValueError("x and y must be numbers.")
    if not math.isfinite(float(x)) or not math.isfinite(float(y)):
        raise ValueError("x and y must be finite numbers.")
    if float(x) < 0 or float(y) < 0:
        raise ValueError("x and y must be non-negative.")

    if space == "screen":
        px = int(round(float(x)))
        py = int(round(float(y)))
        return px, py, {
            "coord_space": "screen",
            "physical_x": px,
            "physical_y": py,
        }

    cached = get_last_screen_geometry()
    fallback_l, fallback_t, fallback_w, fallback_h = (
        (
            cached["virtual_left"],
            cached["virtual_top"],
            cached["screen_width"],
            cached["screen_height"],
        )
        if cached is not None
        else _query_virtual_screen_metrics(user32=user32)
    )
    v_left = int(virtual_left) if virtual_left is not None else int(fallback_l)
    v_top = int(virtual_top) if virtual_top is not None else int(fallback_t)
    s_width = int(screen_width) if screen_width is not None else int(fallback_w)
    s_height = int(screen_height) if screen_height is not None else int(fallback_h)
    eff_dpi = (
        float(dpi_scale)
        if dpi_scale is not None and dpi_scale > 0
        else (float(cached["dpi_scale"]) if cached is not None else 1.0)
    )
    if s_width <= 0 or s_height <= 0:
        raise ValueError("screen_width and screen_height must be positive integers.")

    if space == "normalized_1000":
        if float(x) > 1000.0 or float(y) > 1000.0:
            raise ValueError("x and y must be within [0, 1000] for coord_space='normalized_1000'.")
        raw_px = v_left + int(round((float(x) / 1000.0) * s_width))
        raw_py = v_top + int(round((float(y) / 1000.0) * s_height))
        px = max(v_left, min(v_left + s_width - 1, raw_px))
        py = max(v_top, min(v_top + s_height - 1, raw_py))
        return px, py, {
            "coord_space": "normalized_1000",
            "physical_x": px,
            "physical_y": py,
            "screen_width": s_width,
            "screen_height": s_height,
            "virtual_left": v_left,
            "virtual_top": v_top,
            "dpi_scale": eff_dpi,
        }

    # space == "image"
    img_w = (
        int(image_width)
        if image_width is not None
        else (int(cached["image_width"]) if cached is not None else 0)
    )
    img_h = (
        int(image_height)
        if image_height is not None
        else (int(cached["image_height"]) if cached is not None else 0)
    )
    if img_w <= 0 or img_h <= 0:
        raise ValueError(
            "image_width and image_height are required for coord_space='image' when no prior "
            "screen geometry is cached."
        )
    raw_px = v_left + int(round((float(x) / float(img_w)) * s_width))
    raw_py = v_top + int(round((float(y) / float(img_h)) * s_height))
    px = max(v_left, min(v_left + s_width - 1, raw_px))
    py = max(v_top, min(v_top + s_height - 1, raw_py))
    return px, py, {
        "coord_space": "image",
        "physical_x": px,
        "physical_y": py,
        "image_width": img_w,
        "image_height": img_h,
        "screen_width": s_width,
        "screen_height": s_height,
        "virtual_left": v_left,
        "virtual_top": v_top,
        "dpi_scale": eff_dpi,
    }
VALID_UI_CONTROL_TYPES: tuple[str, ...] = (
    "Button",
    "Edit",
    "MenuItem",
    "TabItem",
    "Hyperlink",
    "ListItem",
    "CheckBox",
    "RadioButton",
    "ComboBox",
    "Text",
)

_CONTROL_TYPE_TO_ROLE: dict[str, str] = {
    "button": "button",
    "splitbutton": "button",
    "edit": "input",
    "input": "input",
    "hyperlink": "link",
    "link": "link",
    "tabitem": "tab",
    "tab": "tab",
    "menuitem": "menu_item",
    "menu_item": "menu_item",
    "checkbox": "checkbox",
    "radiobutton": "radio",
    "radio": "radio",
    "combobox": "combo",
    "combo": "combo",
    "listitem": "list_item",
    "list_item": "list_item",
    "text": "text",
}

_ROLE_TO_CONTROL_TYPE: dict[str, str] = {
    "button": "Button",
    "input": "Edit",
    "link": "Hyperlink",
    "tab": "TabItem",
    "menu_item": "MenuItem",
    "checkbox": "CheckBox",
    "radio": "RadioButton",
    "combo": "ComboBox",
    "list_item": "ListItem",
    "text": "Text",
}

INTERACTIVE_UI_ROLES: frozenset[str] = frozenset(
    {
        "button",
        "input",
        "link",
        "tab",
        "menu_item",
        "checkbox",
        "radio",
        "combo",
        "list_item",
    }
)


def _normalize_ui_role(raw_control_type: str) -> str:
    """Normalize a UI Automation ControlType name into a semantic role."""
    cleaned = raw_control_type.strip().lower()
    if cleaned.startswith("controltype."):
        cleaned = cleaned[len("controltype.") :]
    return _CONTROL_TYPE_TO_ROLE.get(cleaned, cleaned or "text")


def _normalize_ref_id(ref: str) -> str:
    """Normalize an element reference (case-insensitive, optional leading '@', e<digits>)."""
    if not isinstance(ref, str):
        raise ValueError("ref must be a string (e.g. 'e1' or '@e1').")
    cleaned = ref.strip()
    if cleaned.startswith("@"):
        cleaned = cleaned[1:].strip()
    cleaned = cleaned.lower()
    if not cleaned or not re.fullmatch(r"e\d+", cleaned):
        raise ValueError(
            f"ref must be a non-empty element reference matching 'e<digits>' "
            f"(e.g. 'e1' or '@e1'), got {ref!r}."
        )
    return cleaned


class _UiRefStore:
    """Thread-safe module-level store holding the most recent UI snapshot and element refs."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.snapshot_id: str | None = None
        self.window_title: str = ""
        self.hwnd: int = 0
        self.elements_by_ref: dict[str, dict[str, Any]] = {}
        self.stale: bool = False
        self.stale_reason: str = ""

    def update(
        self,
        *,
        snapshot_id: str,
        window_title: str,
        hwnd: int,
        elements_by_ref: dict[str, dict[str, Any]],
    ) -> None:
        """Replace the stored snapshot and ref table atomically."""
        with self._lock:
            self.snapshot_id = snapshot_id
            self.window_title = window_title
            self.hwnd = hwnd
            self.elements_by_ref = {k: dict(v) for k, v in elements_by_ref.items()}
            self.stale = False
            self.stale_reason = ""

    def clear(self) -> None:
        """Clear the stored snapshot and ref table."""
        with self._lock:
            self.snapshot_id = None
            self.window_title = ""
            self.hwnd = 0
            self.elements_by_ref = {}
            self.stale = False
            self.stale_reason = ""

    def mark_stale(self, reason: str = "view_changed") -> None:
        """Mark the current ref table stale after an in-app view transition."""
        with self._lock:
            self.stale = True
            self.stale_reason = str(reason or "view_changed")

    def resolve(self, ref: str) -> tuple[str, str, int, dict[str, Any]]:
        """Look up a ref (supporting optional leading '@' and case-insensitivity)."""
        norm_ref = _normalize_ref_id(ref)
        with self._lock:
            if self.stale:
                raise ValueError(
                    f"Unknown or stale UI ref {ref!r}: UI snapshot {self.snapshot_id} is stale "
                    f"({self.stale_reason or 'view_changed'}). Call ui_snapshot() again."
                )
            if not self.snapshot_id or not self.elements_by_ref:
                raise ValueError(
                    f"Unknown or stale UI ref {ref!r}: no active snapshot. "
                    "Call ui_snapshot() first."
                )
            entry = self.elements_by_ref.get(norm_ref)
            if entry is None:
                avail = ", ".join(list(self.elements_by_ref.keys())[:25])
                raise ValueError(
                    f"Unknown or stale UI ref {ref!r}: UI element ref {ref!r} not found in "
                    f"current snapshot {self.snapshot_id}. Available refs: {avail or 'none'}."
                )
            bounds = dict(entry.get("bounds") or {})
            center = dict(entry.get("center") or {})
            return (
                self.snapshot_id,
                self.window_title,
                self.hwnd,
                {
                    **entry,
                    "bounds": bounds,
                    "center": center,
                },
            )

    def __getitem__(self, ref: str) -> dict[str, Any]:
        with self._lock:
            norm_ref = _normalize_ref_id(ref)
            if norm_ref not in self.elements_by_ref:
                avail = ", ".join(list(self.elements_by_ref.keys())[:25]) or "none"
                raise KeyError(
                    f"UI element ref {ref!r} not found in current snapshot. Available refs: {avail}"
                )
            return self.elements_by_ref[norm_ref]

    def __setitem__(self, ref: str, value: dict[str, Any]) -> None:
        with self._lock:
            norm_ref = _normalize_ref_id(ref)
            self.elements_by_ref[norm_ref] = dict(value)

    def __contains__(self, ref: object) -> bool:
        if not isinstance(ref, str):
            return False
        try:
            norm_ref = _normalize_ref_id(ref)
        except ValueError:
            return False
        with self._lock:
            return norm_ref in self.elements_by_ref

    def __len__(self) -> int:
        with self._lock:
            return len(self.elements_by_ref)

    def __iter__(self) -> Any:
        with self._lock:
            return iter(list(self.elements_by_ref.keys()))

    def keys(self) -> list[str]:
        with self._lock:
            return list(self.elements_by_ref.keys())

    def values(self) -> list[dict[str, Any]]:
        with self._lock:
            return [dict(v) for v in self.elements_by_ref.values()]

    def items(self) -> list[tuple[str, dict[str, Any]]]:
        with self._lock:
            return [(k, dict(v)) for k, v in self.elements_by_ref.items()]

    def get(self, ref: str, default: Any = None) -> Any:
        try:
            norm_ref = _normalize_ref_id(ref)
        except ValueError:
            return default
        with self._lock:
            return self.elements_by_ref.get(norm_ref, default)


_UI_REF_STORE = _UiRefStore()
_LAST_UI_SNAPSHOT: dict[str, Any] | None = None
_LAST_UI_SNAPSHOT_TIME: float = 0.0
_LAST_UI_WINDOW_TITLE: str = ""

VALID_UI_BATCH_OPS: frozenset[str] = frozenset({"act", "press_keys", "scroll", "wait", "click_at"})


def clear_ui_ref_store() -> None:
    """Clear cached UI element refs and snapshot state (used for test isolation)."""
    global _LAST_UI_SNAPSHOT, _LAST_UI_SNAPSHOT_TIME, _LAST_UI_WINDOW_TITLE
    _UI_REF_STORE.clear()
    _LAST_UI_SNAPSHOT = None
    _LAST_UI_SNAPSHOT_TIME = 0.0
    _LAST_UI_WINDOW_TITLE = ""


def set_ui_snapshot_state(snapshot: Any) -> None:
    """Explicitly cache a snapshot dictionary and populate element refs."""
    global _LAST_UI_SNAPSHOT, _LAST_UI_SNAPSHOT_TIME, _LAST_UI_WINDOW_TITLE
    _UI_REF_STORE.clear()
    if hasattr(snapshot, "model_dump"):
        snap_dict = snapshot.model_dump()
    elif isinstance(snapshot, dict):
        snap_dict = snapshot
    else:
        snap_dict = {}

    raw_elements = snap_dict.get("elements") or []
    elements_by_ref: dict[str, dict[str, Any]] = {}
    llm_elements: list[dict[str, Any]] = []
    for idx, raw_el in enumerate(raw_elements):
        el = (
            raw_el.model_dump()
            if hasattr(raw_el, "model_dump")
            else (dict(raw_el) if isinstance(raw_el, dict) else {})
        )
        ref = str(el.get("ref") or f"e{idx}")
        role = str(el.get("role") or el.get("control_type") or "").strip().lower()
        name = str(el.get("name") or "").strip()
        val = str(el["value"]) if el.get("value") is not None else None
        bounds = dict(
            el.get("bounds")
            or {"x": int(el.get("x", 0)), "y": int(el.get("y", 0)), "width": 0, "height": 0}
        )
        center = dict(el.get("center") or {"cx": int(el.get("x", 0)), "cy": int(el.get("y", 0))})
        entry = {
            "ref": ref,
            "role": role,
            "name": name,
            "value": val,
            "enabled": bool(el.get("enabled", True)),
            "focused": bool(el.get("focused", False)),
            "bounds": bounds,
            "center": center,
            "x": int(el.get("x", center.get("cx", 0))),
            "y": int(el.get("y", center.get("cy", 0))),
            "automation_id": str(el.get("automation_id") or ""),
            "control_type": str(el.get("control_type") or el.get("role") or ""),
        }
        elements_by_ref[ref] = entry
        llm_elements.append(
            {
                "ref": ref,
                "role": role,
                "name": name,
                "value": val,
                "x": entry["x"],
                "y": entry["y"],
                "automation_id": entry["automation_id"],
                "control_type": entry["control_type"],
                "enabled": entry["enabled"],
                "focused": entry["focused"],
            }
        )

    snapshot_id = str(snap_dict.get("snapshot_id") or f"snap_{uuid.uuid4().hex[:12]}")
    win_title = str(snap_dict.get("window_title") or "")
    _UI_REF_STORE.update(
        snapshot_id=snapshot_id,
        window_title=win_title,
        hwnd=int(snap_dict.get("hwnd", 0) or 0),
        elements_by_ref=elements_by_ref,
    )
    _LAST_UI_SNAPSHOT = {
        "ok": True,
        "snapshot_id": snapshot_id,
        "window_title": win_title,
        "count": len(elements_by_ref),
        "element_count": len(elements_by_ref),
        "elements": llm_elements,
    }
    _LAST_UI_SNAPSHOT_TIME = time.time()
    _LAST_UI_WINDOW_TITLE = win_title


def _get_foreground_window_title(user32: Any = None) -> str:
    """Retrieve the title of the current foreground window via Win32."""
    u32 = _get_win32_user32(user32)
    if hasattr(u32, "foreground_title"):
        return str(u32.foreground_title)
    if not hasattr(u32, "GetForegroundWindow"):
        return ""
    hwnd = u32.GetForegroundWindow()
    if not hwnd:
        return ""
    if (
        hasattr(u32, "window_titles")
        and isinstance(u32.window_titles, dict)
        and hwnd in u32.window_titles
    ):
        return str(u32.window_titles[hwnd])
    length = 0
    if hasattr(u32, "GetWindowTextLengthW"):
        length = int(u32.GetWindowTextLengthW(hwnd))
    if length <= 0:
        length = 256
    buf = ctypes.create_unicode_buffer(length + 1)
    if hasattr(u32, "GetWindowTextW"):
        copied = int(u32.GetWindowTextW(hwnd, buf, length + 1))
        if copied > 0:
            return buf.value
    return ""


_EXTENDED_VK_KEYS: frozenset[str] = frozenset(
    {
        "delete",
        "up",
        "down",
        "left",
        "right",
        "home",
        "end",
        "pageup",
        "pagedown",
    }
)

_KEY_ALIASES: dict[str, str] = {
    "control": "ctrl",
    "esc": "escape",
    "del": "delete",
    "pgup": "pageup",
    "page_up": "pageup",
    "pgdn": "pagedown",
    "page_down": "pagedown",
    "return": "enter",
    " ": "space",
}

_ALLOWED_VK_MAP: dict[str, int] = {
    "enter": 0x0D,
    "tab": 0x09,
    "escape": 0x1B,
    "space": 0x20,
    "backspace": 0x08,
    "delete": 0x2E,
    "up": 0x26,
    "down": 0x28,
    "left": 0x25,
    "right": 0x27,
    "home": 0x24,
    "end": 0x23,
    "pageup": 0x21,
    "pagedown": 0x22,
    "ctrl": 0x11,
    "alt": 0x12,
    "shift": 0x10,
    **{f"f{i}": 0x6F + i for i in range(1, 13)},
    **{chr(c): c - 32 for c in range(ord("a"), ord("z") + 1)},
    **{str(d): 0x30 + d for d in range(10)},
}

_BLOCKED_KEY_COMBOS: tuple[frozenset[str], ...] = (
    frozenset({"alt", "f4"}),
    frozenset({"ctrl", "alt", "delete"}),
    frozenset({"ctrl", "shift", "escape"}),
)

_ULONG_PTR = ctypes.c_size_t


class _MOUSEINPUT(ctypes.Structure):
    _fields_ = [
        ("dx", ctypes.c_long),
        ("dy", ctypes.c_long),
        ("mouseData", ctypes.c_uint32),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _KEYBDINPUT(ctypes.Structure):
    _fields_ = [
        ("wVk", ctypes.c_uint16),
        ("wScan", ctypes.c_uint16),
        ("dwFlags", ctypes.c_uint32),
        ("time", ctypes.c_uint32),
        ("dwExtraInfo", _ULONG_PTR),
    ]


class _HARDWAREINPUT(ctypes.Structure):
    _fields_ = [
        ("uMsg", ctypes.c_uint32),
        ("wParamL", ctypes.c_uint16),
        ("wParamH", ctypes.c_uint16),
    ]


class _INPUT_UNION(ctypes.Union):
    _fields_ = [
        ("mi", _MOUSEINPUT),
        ("ki", _KEYBDINPUT),
        ("hi", _HARDWAREINPUT),
    ]


class _INPUT(ctypes.Structure):
    _fields_ = [
        ("type", ctypes.c_uint32),
        ("union", _INPUT_UNION),
    ]


_POWERSHELL_UIA_RESOLVE_WINDOW = (
    "Add-Type -AssemblyName UIAutomationClient; "
    "Add-Type -AssemblyName UIAutomationTypes; "
    "[Console]::OutputEncoding = [System.Text.Encoding]::UTF8; "
    "$root = [System.Windows.Automation.AutomationElement]::RootElement; "
    "$win = $null; "
    "if ($TargetHwnd -ne 0) { "
    "  try { $win = [System.Windows.Automation.AutomationElement]::FromHandle("
    "[IntPtr]$TargetHwnd) } catch { $win = $null } "
    "} "
    "if ($win -eq $null -and $AppName -ne '') { "
    "  $winCond = New-Object System.Windows.Automation.PropertyCondition("
    "[System.Windows.Automation.AutomationElement]::ControlTypeProperty, "
    "[System.Windows.Automation.ControlType]::Window); "
    "  $wins = $root.FindAll([System.Windows.Automation.TreeScope]::Children, $winCond); "
    "  foreach ($w in $wins) { "
    "    try { "
    "      $wn = [string]$w.Current.Name; "
    "      if ($wn -and $wn.IndexOf($AppName, "
    "[System.StringComparison]::OrdinalIgnoreCase) -ge 0) { "
    "        $win = $w; break "
    "      } "
    "    } catch { continue } "
    "  } "
    "} "
    "if ($win -eq $null -and $AppName -eq '') { "
    "  try { "
    "    $cur = [System.Windows.Automation.AutomationElement]::FocusedElement; "
    "    $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker; "
    "    while ($cur -ne $null -and $cur -ne $root) { "
    "      $p = $walker.GetParent($cur); "
    "      if ($p -eq $null -or $p -eq $root) { $win = $cur; break } "
    "      $cur = $p "
    "    } "
    "  } catch { $win = $null } "
    "} "
    "if ($win -eq $null -and $AppName -eq '') { "
    "  $win = $root.FindFirst([System.Windows.Automation.TreeScope]::Children, "
    "[System.Windows.Automation.Condition]::TrueCondition) "
    "} "
)

_POWERSHELL_UIA_BOUNDED_WALK_SETUP = (
    "$ctAllowed = @("
    "'Button','Edit','MenuItem','TabItem','Hyperlink',"
    "'ListItem','CheckBox','RadioButton','ComboBox','Text'"
    "); "
    "$uiaCacheReq = New-Object System.Windows.Automation.CacheRequest; "
    "$uiaCacheReq.TreeScope = [System.Windows.Automation.TreeScope]::Element; "
    "$uiaCacheReq.AutomationElementMode = "
    "[System.Windows.Automation.AutomationElementMode]::Full; "
    "$uiaCacheReq.Add([System.Windows.Automation.AutomationElement]::NameProperty); "
    "$uiaCacheReq.Add([System.Windows.Automation.AutomationElement]::AutomationIdProperty); "
    "$uiaCacheReq.Add([System.Windows.Automation.AutomationElement]::ControlTypeProperty); "
    "$uiaCacheReq.Add([System.Windows.Automation.AutomationElement]::BoundingRectangleProperty); "
    "$uiaCacheReq.Add([System.Windows.Automation.AutomationElement]::IsOffscreenProperty); "
    "$uiaCacheReq.Add([System.Windows.Automation.AutomationElement]::IsEnabledProperty); "
    "$uiaCacheReq.Add([System.Windows.Automation.AutomationElement]::HasKeyboardFocusProperty); "
    "$uiaCacheReq.Add([System.Windows.Automation.ValuePattern]::Pattern); "
    "$uiaCacheReq.Add([System.Windows.Automation.ValuePattern]::ValueProperty); "
    "$uiaCacheReq.Add([System.Windows.Automation.RangeValuePattern]::Pattern); "
    "$uiaCacheReq.Add([System.Windows.Automation.RangeValuePattern]::ValueProperty); "
    "function Get-UiaProps($El) { "
    "  try { "
    "    $c = $El.Cached; "
    "    [void]$c.ControlType; "
    "    return $c "
    "  } catch { "
    "    return $El.Current "
    "  } "
    "} "
    "function Get-BoundedUiaNodes($RootEl, [int]$MaxDepth, [int]$MaxNodes) { "
    "  $out = New-Object System.Collections.ArrayList; "
    "  if ($RootEl -eq $null) { return ,$out } "
    "  $walker = [System.Windows.Automation.TreeWalker]::ControlViewWalker; "
    "  $scope = $uiaCacheReq.Activate(); "
    "  try { "
    "    $queue = New-Object System.Collections.ArrayList; "
    "    $cachedRoot = try { $RootEl.GetUpdatedCache($uiaCacheReq) } catch { $RootEl }; "
    "    [void]$queue.Add(@($cachedRoot, 0)); "
    "    $qi = 0; $visited = 0; "
    "    while ($qi -lt $queue.Count -and $visited -lt $MaxNodes) { "
    "      $entry = $queue[$qi]; $qi++; "
    "      $node = $entry[0]; $depth = [int]$entry[1]; "
    "      if ($node -eq $null) { continue } "
    "      $visited++; "
    "      if ($depth -gt 0) { [void]$out.Add($node) } "
    "      if ($depth -lt $MaxDepth) { "
    "        $child = try { $walker.GetFirstChild($node, $uiaCacheReq) } catch { $null }; "
    "        while ($child -ne $null -and ($visited + $queue.Count - $qi) -lt $MaxNodes) { "
    "          [void]$queue.Add(@($child, ($depth + 1))); "
    "          $child = try { $walker.GetNextSibling($child, $uiaCacheReq) } catch { $null } "
    "        } "
    "      } "
    "    } "
    "  } finally { "
    "    if ($scope -ne $null) { $scope.Dispose() } "
    "  } "
    "  return ,$out "
    "} "
)

_POWERSHELL_INSPECT_UI_SCRIPT = (
    "param([long]$TargetHwnd = 0, [int]$MaxElements = 60, [string]$AppNameBase64 = '', "
    f"[int]$MaxDepth = {UIA_MAX_DEPTH}, [int]$MaxNodes = {UIA_MAX_NODES}) "
    "$AppName = if ($AppNameBase64) { "
    "[System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($AppNameBase64)) "
    "} else { '' }; "
    + _POWERSHELL_UIA_RESOLVE_WINDOW
    + _POWERSHELL_UIA_BOUNDED_WALK_SETUP
    + "$winTitle = if ($win -ne $null) { "
    "try { [string]$win.Current.Name } catch { '' } } else { '' }; "
    "$items = New-Object System.Collections.ArrayList; "
    "if ($win -ne $null) { "
    "  $all = Get-BoundedUiaNodes $win $MaxDepth $MaxNodes; "
    "  foreach ($el in $all) { "
    "    try { "
    "      $cur = Get-UiaProps $el; "
    "      if ($cur.IsOffscreen) { continue } "
    "      $ctName = ($cur.ControlType.ProgrammaticName -replace '^ControlType\\.', ''); "
    "      if ($ctAllowed -notcontains $ctName) { continue } "
    "      $r = $cur.BoundingRectangle; "
    "      if ([double]::IsInfinity($r.Width) -or [double]::IsInfinity($r.Height) "
    "-or [double]::IsNaN($r.Width) -or [double]::IsNaN($r.Height) "
    "-or $r.Width -le 0 -or $r.Height -le 0) { continue } "
    "      $elName = [string]$cur.Name; "
    "      $elAid = [string]$cur.AutomationId; "
    "      $elVal = $null; "
    "      $vp = $null; "
    "      try { "
    "        if ($el.TryGetCachedPattern([System.Windows.Automation.ValuePattern]::Pattern, "
    "[ref]$vp)) { $elVal = [string]$vp.Cached.Value } "
    "        elseif ($el.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, "
    "[ref]$vp)) { $elVal = [string]$vp.Current.Value } "
    "      } catch { $elVal = $null } "
    "      if ($ctName -eq 'Text' -and [string]::IsNullOrWhiteSpace($elName) "
    "-and [string]::IsNullOrWhiteSpace($elAid) -and [string]::IsNullOrEmpty($elVal)) { continue } "
    "      $cx = [int][Math]::Round($r.Left + ($r.Width / 2.0)); "
    "      $cy = [int][Math]::Round($r.Top + ($r.Height / 2.0)); "
    "      [void]$items.Add(@{ "
    "        index = $items.Count; "
    "        name = $elName; "
    "        automation_id = $elAid; "
    "        control_type = $ctName; "
    "        value = $elVal; "
    "        x = $cx; "
    "        y = $cy "
    "      }); "
    "      if ($items.Count -ge $MaxElements) { break } "
    "    } catch { continue } "
    "  } "
    "} "
    "@{ window_title = $winTitle; count = $items.Count; elements = [object[]]$items.ToArray() } "
    "| ConvertTo-Json -Depth 4 -Compress"
)

_POWERSHELL_CLICK_UI_SCRIPT = (
    "param([long]$TargetHwnd = 0, [string]$TargetNameBase64 = '', "
    "[string]$ControlTypeFilter = '', [string]$AppNameBase64 = '', "
    f"[int]$MaxDepth = {UIA_MAX_DEPTH}, [int]$MaxNodes = {UIA_MAX_NODES}) "
    "$TargetName = if ($TargetNameBase64) { "
    "[System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($TargetNameBase64)) "
    "} else { '' }; "
    "$AppName = if ($AppNameBase64) { "
    "[System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($AppNameBase64)) "
    "} else { '' }; "
    + _POWERSHELL_UIA_RESOLVE_WINDOW
    + _POWERSHELL_UIA_BOUNDED_WALK_SETUP
    + "$exactMatch = $null; $exactIsText = $false; "
    "$subMatch = $null; $subIsText = $false; $aidMatch = $null; "
    "$avail = New-Object System.Collections.ArrayList; "
    "if ($win -ne $null) { "
    "  $all = Get-BoundedUiaNodes $win $MaxDepth $MaxNodes; "
    "  foreach ($el in $all) { "
    "    try { "
    "      $cur = Get-UiaProps $el; "
    "      if ($cur.IsOffscreen) { continue } "
    "      $ctName = ($cur.ControlType.ProgrammaticName -replace '^ControlType\\.', ''); "
    "      if ($ControlTypeFilter -ne '') { "
    "        if (-not $ctName.Equals($ControlTypeFilter, "
    "[System.StringComparison]::OrdinalIgnoreCase)) { continue } "
    "      } elseif ($ctAllowed -notcontains $ctName) { continue } "
    "      $r = $cur.BoundingRectangle; "
    "      if ([double]::IsInfinity($r.Width) -or [double]::IsInfinity($r.Height) "
    "-or [double]::IsNaN($r.Width) -or [double]::IsNaN($r.Height) "
    "-or $r.Width -le 0 -or $r.Height -le 0) { continue } "
    "      $elName = [string]$cur.Name; "
    "      $elAid = [string]$cur.AutomationId; "
    "      if (-not [string]::IsNullOrWhiteSpace($elName) -and $avail.Count -lt 40) { "
    "        if (-not $avail.Contains($elName)) { [void]$avail.Add($elName) } "
    "      } "
    "      $isText = ($ctName -eq 'Text'); "
    "      if ($elName.Equals($TargetName, [System.StringComparison]::OrdinalIgnoreCase)) { "
    "        if ($exactMatch -eq $null -or ($exactIsText -and -not $isText)) { "
    "          $exactMatch = $el; $exactIsText = $isText "
    "        } "
    "      } elseif (-not [string]::IsNullOrEmpty($elName) -and "
    "$elName.IndexOf($TargetName, [System.StringComparison]::OrdinalIgnoreCase) -ge 0) { "
    "        if ($subMatch -eq $null -or ($subIsText -and -not $isText)) { "
    "          $subMatch = $el; $subIsText = $isText "
    "        } "
    "      } elseif ($aidMatch -eq $null -and -not [string]::IsNullOrEmpty($elAid) -and "
    "$elAid.Equals($TargetName, [System.StringComparison]::OrdinalIgnoreCase)) { "
    "        $aidMatch = $el "
    "      } "
    "    } catch { continue } "
    "  } "
    "} "
    "$chosen = if ($exactMatch -ne $null) { $exactMatch } "
    "elseif ($subMatch -ne $null) { $subMatch } else { $aidMatch }; "
    "if ($chosen -eq $null) { "
    "  @{ found = $false; available = [object[]]$avail.ToArray() } "
    "| ConvertTo-Json -Depth 4 -Compress "
    "} else { "
    "  $cc = Get-UiaProps $chosen; "
    "  $cr = $cc.BoundingRectangle; "
    "  $cx = [int][Math]::Round($cr.Left + ($cr.Width / 2.0)); "
    "  $cy = [int][Math]::Round($cr.Top + ($cr.Height / 2.0)); "
    "  $mType = ($cc.ControlType.ProgrammaticName -replace '^ControlType\\.', ''); "
    "  $mName = if (-not [string]::IsNullOrEmpty($cc.Name)) { [string]$cc.Name } "
    "else { [string]$cc.AutomationId }; "
    "  $invPat = $null; $didInvoke = $false; "
    "  try { "
    "    if ($chosen.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, "
    "[ref]$invPat)) { "
    "      $invPat.Invoke(); $didInvoke = $true "
    "    } "
    "  } catch { $didInvoke = $false } "
    "  @{ found = $true; invoked = $didInvoke; name = $mName; "
    "control_type = $mType; x = $cx; y = $cy } | ConvertTo-Json -Depth 4 -Compress "
    "}"
)

_POWERSHELL_SNAPSHOT_UI_SCRIPT = (
    "param([long]$TargetHwnd = 0, [int]$MaxElements = 80, "
    "[int]$InteractiveOnly = 0, [string]$AppNameBase64 = '', "
    f"[int]$MaxDepth = {UIA_MAX_DEPTH}, [int]$MaxNodes = {UIA_MAX_NODES}) "
    "$AppName = if ($AppNameBase64) { "
    "[System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($AppNameBase64)) "
    "} else { '' }; "
    + _POWERSHELL_UIA_RESOLVE_WINDOW
    + _POWERSHELL_UIA_BOUNDED_WALK_SETUP
    + "$winTitle = if ($win -ne $null) { "
    "try { [string]$win.Current.Name } catch { '' } } else { '' }; "
    "$winHwnd = if ($win -ne $null) { "
    "try { [long]$win.Current.NativeWindowHandle } catch { 0 } } else { 0 }; "
    "$items = New-Object System.Collections.ArrayList; "
    "if ($win -ne $null) { "
    "  $all = Get-BoundedUiaNodes $win $MaxDepth $MaxNodes; "
    "  foreach ($el in $all) { "
    "    try { "
    "      $cur = Get-UiaProps $el; "
    "      if ($cur.IsOffscreen) { continue } "
    "      $ctName = ($cur.ControlType.ProgrammaticName -replace '^ControlType\\.', ''); "
    "      if ($ctAllowed -notcontains $ctName) { continue } "
    "      $r = $cur.BoundingRectangle; "
    "      if ([double]::IsInfinity($r.Width) -or [double]::IsInfinity($r.Height) "
    "-or [double]::IsNaN($r.Width) -or [double]::IsNaN($r.Height) "
    "-or $r.Width -le 0 -or $r.Height -le 0) { continue } "
    "      $bx = [int][Math]::Round($r.Left); "
    "      $by = [int][Math]::Round($r.Top); "
    "      $bw = [int][Math]::Round($r.Width); "
    "      $bh = [int][Math]::Round($r.Height); "
    "      if ($bw -le 0 -or $bh -le 0) { continue } "
    "      $isEnabled = [bool]$cur.IsEnabled; "
    "      $isFocused = [bool]$cur.HasKeyboardFocus; "
    "      if ($InteractiveOnly -ne 0 -and ($ctName -eq 'Text' -or -not $isEnabled)) { continue } "
    "      $elName = [string]$cur.Name; "
    "      $elAid = [string]$cur.AutomationId; "
    "      $elVal = $null; "
    "      $vp = $null; "
    "      try { "
    "        if ($el.TryGetCachedPattern([System.Windows.Automation.ValuePattern]::Pattern, "
    "[ref]$vp)) { $elVal = [string]$vp.Cached.Value } "
    "        elseif ($el.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, "
    "[ref]$vp)) { $elVal = [string]$vp.Current.Value } "
    "      } catch { $elVal = $null } "
    "      if ($elVal -eq $null) { "
    "        $rvp = $null; "
    "        try { "
    "          if ($el.TryGetCachedPattern("
    "[System.Windows.Automation.RangeValuePattern]::Pattern, [ref]$rvp)) { "
    "            $elVal = [string]$rvp.Cached.Value "
    "          } elseif ($el.TryGetCurrentPattern("
    "[System.Windows.Automation.RangeValuePattern]::Pattern, [ref]$rvp)) { "
    "            $elVal = [string]$rvp.Current.Value "
    "          } "
    "        } catch { $elVal = $null } "
    "      } "
    "      if ($ctName -eq 'Text' -and [string]::IsNullOrWhiteSpace($elName) "
    "-and [string]::IsNullOrWhiteSpace([string]$elVal)) { continue } "
    "      $cx = [int][Math]::Round($r.Left + ($r.Width / 2.0)); "
    "      $cy = [int][Math]::Round($r.Top + ($r.Height / 2.0)); "
    "      [void]$items.Add(@{ "
    "        name = $elName; "
    "        automation_id = $elAid; "
    "        control_type = $ctName; "
    "        value = $elVal; "
    "        enabled = $isEnabled; "
    "        focused = $isFocused; "
    "        bounds = @{ x = $bx; y = $by; width = $bw; height = $bh }; "
    "        center = @{ cx = $cx; cy = $cy } "
    "      }); "
    "      if ($items.Count -ge $MaxElements) { break } "
    "    } catch { continue } "
    "  } "
    "} "
    "@{ window_title = $winTitle; hwnd = $winHwnd; count = $items.Count; "
    "elements = [object[]]$items.ToArray() } | ConvertTo-Json -Depth 5 -Compress"
)

_POWERSHELL_UI_ACT_SCRIPT = (
    "param([long]$TargetHwnd = 0, [string]$WindowTitleBase64 = '', "
    "[string]$Action = 'click', [string]$AutomationIdBase64 = '', "
    "[string]$NameBase64 = '', [string]$ControlType = '', "
    "[int]$ExpectedX = 0, [int]$ExpectedY = 0, [int]$ExpectedW = 0, [int]$ExpectedH = 0, "
    "[int]$CenterX = 0, [int]$CenterY = 0, [string]$ValueBase64 = '', "
    f"[int]$MaxDepth = {UIA_MAX_DEPTH}, [int]$MaxNodes = {UIA_MAX_NODES}) "
    "$AppName = if ($WindowTitleBase64) { "
    "[System.Text.Encoding]::UTF8.GetString("
    "[System.Convert]::FromBase64String($WindowTitleBase64)) "
    "} else { '' }; "
    "$TargetAid = if ($AutomationIdBase64) { "
    "[System.Text.Encoding]::UTF8.GetString("
    "[System.Convert]::FromBase64String($AutomationIdBase64)) "
    "} else { '' }; "
    "$TargetName = if ($NameBase64) { "
    "[System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($NameBase64)) "
    "} else { '' }; "
    "$TargetValue = if ($ValueBase64) { "
    "[System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($ValueBase64)) "
    "} else { '' }; "
    + _POWERSHELL_UIA_RESOLVE_WINDOW
    + _POWERSHELL_UIA_BOUNDED_WALK_SETUP
    + "$chosen = $null; $bestScore = -1; $bestDist = [int]::MaxValue; "
    "$chosenCx = $CenterX; $chosenCy = $CenterY; "
    "if ($win -ne $null) { "
    "  $all = Get-BoundedUiaNodes $win $MaxDepth $MaxNodes; "
    "  foreach ($el in $all) { "
    "    try { "
    "      $cur = Get-UiaProps $el; "
    "      if ($cur.IsOffscreen) { continue } "
    "      $ctName = ($cur.ControlType.ProgrammaticName -replace '^ControlType\\.', ''); "
    "      if ($ControlType -ne '') { "
    "        if (-not $ctName.Equals($ControlType, "
    "[System.StringComparison]::OrdinalIgnoreCase)) { continue } "
    "      } elseif ($ctAllowed -notcontains $ctName) { continue } "
    "      $r = $cur.BoundingRectangle; "
    "      if ([double]::IsInfinity($r.Width) -or [double]::IsInfinity($r.Height) "
    "-or [double]::IsNaN($r.Width) -or [double]::IsNaN($r.Height) "
    "-or $r.Width -le 0 -or $r.Height -le 0) { continue } "
    "      $bx = [int][Math]::Round($r.Left); $by = [int][Math]::Round($r.Top); "
    "      $bw = [int][Math]::Round($r.Width); $bh = [int][Math]::Round($r.Height); "
    "      $cx = [int][Math]::Round($r.Left + ($r.Width / 2.0)); "
    "      $cy = [int][Math]::Round($r.Top + ($r.Height / 2.0)); "
    "      $elName = [string]$cur.Name; $elAid = [string]$cur.AutomationId; "
    "      $dist = [Math]::Abs($cx - $CenterX) + [Math]::Abs($cy - $CenterY); "
    "      $boxDist = [Math]::Abs($bx - $ExpectedX) + [Math]::Abs($by - $ExpectedY) "
    "+ [Math]::Abs($bw - $ExpectedW) + [Math]::Abs($bh - $ExpectedH); "
    "      $score = 0; "
    "      if ($TargetAid -ne '' -and $elAid -eq $TargetAid) { $score += 100 } "
    "      if ($TargetName -ne '' -and $elName -eq $TargetName) { $score += 50 } "
    "      if ($ExpectedW -gt 0 -and $ExpectedH -gt 0 -and $boxDist -le 12) { $score += 40 } "
    "      elseif ($dist -le 10) { $score += 30 } "
    "      if ($score -lt 50 -and -not ($TargetAid -eq '' -and $TargetName -eq '' "
    "-and $ExpectedW -gt 0 -and $boxDist -le 12)) { continue } "
    "      if ($score -gt $bestScore -or ($score -eq $bestScore -and $dist -lt $bestDist)) { "
    "        $bestScore = $score; $bestDist = $dist; $chosen = $el; "
    "        $chosenCx = $cx; $chosenCy = $cy "
    "      } "
    "    } catch { continue } "
    "  } "
    "} "
    "if ($chosen -eq $null) { "
    "  @{ found = $false; acted = $false; method = ''; cx = $CenterX; cy = $CenterY } "
    "| ConvertTo-Json -Depth 4 -Compress "
    "} else { "
    "  $method = ''; "
    "  if ($Action -eq 'click') { "
    "    $pat = $null; "
    "    try { "
    "      if ($chosen.TryGetCurrentPattern([System.Windows.Automation.InvokePattern]::Pattern, "
    "[ref]$pat)) { $pat.Invoke(); $method = 'invoke' } "
    "      elseif ($chosen.TryGetCurrentPattern("
    "[System.Windows.Automation.SelectionItemPattern]::Pattern, [ref]$pat)) { "
    "        $pat.Select(); $method = 'select' "
    "      } "
    "      elseif ($chosen.TryGetCurrentPattern("
    "[System.Windows.Automation.TogglePattern]::Pattern, [ref]$pat)) { "
    "        $pat.Toggle(); $method = 'toggle' "
    "      } "
    "      elseif ($chosen.TryGetCurrentPattern("
    "[System.Windows.Automation.ExpandCollapsePattern]::Pattern, [ref]$pat)) { "
    "        if ($pat.Current.ExpandCollapseState -eq "
    "[System.Windows.Automation.ExpandCollapseState]::Expanded) { $pat.Collapse() } "
    "        else { $pat.Expand() }; "
    "        $method = 'expand_collapse' "
    "      } "
    "    } catch { $method = '' } "
    "  } elseif ($Action -eq 'set_value') { "
    "    $vp = $null; "
    "    try { "
    "      if ($chosen.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, "
    "[ref]$vp)) { "
    "        if (-not $vp.Current.IsReadOnly) { "
    "          $vp.SetValue($TargetValue); $method = 'value_pattern' "
    "        } "
    "      } "
    "    } catch { $method = '' } "
    "  } "
    "  @{ found = $true; acted = ($method -ne ''); method = $method; "
    "cx = $chosenCx; cy = $chosenCy } | ConvertTo-Json -Depth 4 -Compress "
    "}"
)


def _resolve_target_process_info(
    *,
    hwnd: int,
    app_name: str | None = None,
    user32: Any = None,
    kernel32: Any = None,
    window_enumerator: Callable[[], list[EnumeratedWindow]] | None = None,
    powershell_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> tuple[str, str, bool | None]:
    """Return ``(process_name, window_title, is_pwa_hint)`` for a target HWND/app."""
    if is_chromium_app_name(app_name):
        low = (app_name or "").strip().lower()
        proc = "msedge.exe" if "edge" in low else ("brave.exe" if "brave" in low else "chrome.exe")
        title = ""
        if window_enumerator is not None and hwnd > 0:
            with contextlib.suppress(Exception):
                for win in window_enumerator():
                    if int(win.hwnd) == int(hwnd):
                        title = win.title or ""
                        break
        elif hwnd > 0 and (user32 is not None or powershell_runner is None):
            u32 = user32 if user32 is not None else (
                _get_win32_user32(None) if sys.platform == "win32" else None
            )
            if u32 is not None:
                title = _read_window_title_os(hwnd, u32) or ""
        return proc, title, False

    # Look up in window_enumerator when supplied
    if window_enumerator is not None and hwnd > 0:
        with contextlib.suppress(Exception):
            for win in window_enumerator():
                if int(win.hwnd) == int(hwnd):
                    is_pwa_hint = (
                        not _is_browser_tab_window_title(win.title)
                        if is_chromium_process(win.process_name)
                        else None
                    )
                    return win.process_name, win.title, is_pwa_hint

    # Do not probe live Win32 processes during mock PowerShell tests lacking Win32 doubles
    has_win32_double = user32 is not None or window_enumerator is not None
    if powershell_runner is not None and not has_win32_double:
        return "", "", None

    if hwnd > 0:
        u32 = user32 if user32 is not None else (
            _get_win32_user32(None) if sys.platform == "win32" else None
        )
        k32 = kernel32 if kernel32 is not None else (
            _get_win32_kernel32(None) if sys.platform == "win32" else None
        )
        title = _read_window_title_os(hwnd, u32) or "" if u32 is not None else ""
        proc_name = ""
        if u32 is not None and hasattr(u32, "GetWindowThreadProcessId"):
            with contextlib.suppress(Exception):
                pid = ctypes.c_uint32(0)
                u32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
                pid_val = int(pid.value)
                if pid_val > 0 and k32 is not None and hasattr(k32, "OpenProcess"):
                    h_proc = k32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid_val)
                    if h_proc:
                        try:
                            if hasattr(k32, "QueryFullProcessImageNameW"):
                                buf = ctypes.create_unicode_buffer(1024)
                                size = ctypes.c_uint32(1024)
                                if k32.QueryFullProcessImageNameW(
                                    h_proc, 0, buf, ctypes.byref(size)
                                ):
                                    proc_name = Path(buf.value).name
                        finally:
                            with contextlib.suppress(Exception):
                                k32.CloseHandle(h_proc)
        if is_chromium_process(proc_name):
            is_pwa_hint = not _is_browser_tab_window_title(title) if title else None
            return proc_name, title, is_pwa_hint
        return proc_name, title, None

    return "", "", None


def _chromium_cdp_unavailable_snapshot(
    *,
    window_title: str = "",
    process_name: str = "",
    error: str | None = None,
) -> dict[str, Any]:
    """Fast (<200ms) structured fallback when Chromium UIA is blocked and CDP is off."""
    return {
        "ok": False,
        "status": "cdp_unavailable",
        "source": "cdp",
        "cdp_available": False,
        "snapshot_id": "",
        "window_title": window_title,
        "process_name": process_name,
        "count": 0,
        "element_count": 0,
        "elements": [],
        "message": (
            "Blocked slow PowerShell UIA traversal on Chromium process "
            f"'{process_name or 'chrome'}'. CDP remote debugging is currently unavailable."
        ),
        "hint": (
            "Use open_url(...) for direct navigation or enable "
            f"{CDP_REMOTE_DEBUGGING_HINT.removeprefix('Enable remote debugging at ')}"
        ),
        "error": error or CDP_REMOTE_DEBUGGING_HINT,
    }


def _resolve_target_hwnd_optional(
    app_name: str | None,
    *,
    powershell_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    user32: Any = None,
    kernel32: Any = None,
    window_enumerator: Callable[[], list[EnumeratedWindow]] | None = None,
    focus_if_app_name: bool = True,
) -> int:
    """Resolve an HWND when live or Win32 doubles are provided; return 0 in mock PS tests."""
    has_win32_double = user32 is not None or window_enumerator is not None
    if powershell_runner is not None and not has_win32_double:
        return 0

    if app_name is not None:
        cleaned = app_name.strip()
        if not cleaned:
            raise ValueError("app_name must not be empty when provided.")
        hwnd = _resolve_window_hwnd(
            app_name=cleaned,
            user32=user32,
            kernel32=kernel32,
            window_enumerator=window_enumerator,
        )
        if focus_if_app_name:
            u32 = _get_win32_user32(user32)
            if hasattr(u32, "IsIconic") and u32.IsIconic(hwnd):
                u32.ShowWindow(hwnd, SW_RESTORE)
            if hasattr(u32, "SetForegroundWindow"):
                u32.SetForegroundWindow(hwnd)
        return hwnd

    u32 = _get_win32_user32(user32)
    if hasattr(u32, "GetForegroundWindow"):
        fg = u32.GetForegroundWindow()
        if fg:
            return int(fg)
    return 0


def _resolve_powershell_exe(
    powershell_runner: Callable[..., subprocess.CompletedProcess[str]] | None,
) -> str:
    ps_exe = shutil.which("powershell.exe")
    if ps_exe:
        return ps_exe
    if powershell_runner is not None:
        return "powershell.exe"
    raise RuntimeError("powershell.exe is required for Windows UI Automation operations.")


def _send_win32_inputs(u32: Any, inputs: list[_INPUT]) -> int:
    if not inputs:
        return 0
    arr = (_INPUT * len(inputs))(*inputs)
    if hasattr(u32, "SendInput"):
        sent = int(u32.SendInput(len(inputs), arr, ctypes.sizeof(_INPUT)))
        if sent != len(inputs):
            raise RuntimeError(
                f"Win32 SendInput failed to inject input events ({sent}/{len(inputs)})."
            )
        return sent
    raise RuntimeError("Win32 SendInput API is unavailable.")


def _perform_mouse_click(
    x: int,
    y: int,
    *,
    clicks: int = 1,
    button: str = "left",
    user32: Any = None,
) -> None:
    u32 = _get_win32_user32(user32)
    if hasattr(u32, "SetCursorPos"):
        moved = u32.SetCursorPos(int(x), int(y))
        if moved is not None and not moved:
            raise RuntimeError(f"Win32 SetCursorPos({x}, {y}) failed.")

    actual_clicks = 2 if button == "double" and clicks == 1 else clicks
    if button in {"left", "double"}:
        down_flag, up_flag = MOUSEEVENTF_LEFTDOWN, MOUSEEVENTF_LEFTUP
    elif button == "right":
        down_flag, up_flag = MOUSEEVENTF_RIGHTDOWN, MOUSEEVENTF_RIGHTUP
    else:
        raise ValueError(f"Unsupported mouse button: {button!r}")

    if hasattr(u32, "SendInput"):
        events: list[_INPUT] = []
        for _ in range(actual_clicks):
            events.append(
                _INPUT(type=INPUT_MOUSE, union=_INPUT_UNION(mi=_MOUSEINPUT(dwFlags=down_flag)))
            )
            events.append(
                _INPUT(type=INPUT_MOUSE, union=_INPUT_UNION(mi=_MOUSEINPUT(dwFlags=up_flag)))
            )
        _send_win32_inputs(u32, events)
    elif hasattr(u32, "mouse_event"):
        for _ in range(actual_clicks):
            u32.mouse_event(down_flag, 0, 0, 0, 0)
            u32.mouse_event(up_flag, 0, 0, 0, 0)
    else:
        raise RuntimeError("Win32 SendInput/mouse_event is unavailable.")


def _run_powershell_uia_json(
    ps_command: str,
    *,
    operation: str,
    powershell_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    """Execute a PowerShell 5.1 UIA command with shell=False and parse its JSON output."""
    ps_exe = _resolve_powershell_exe(powershell_runner)
    try:
        proc = run_powershell_hidden(
            ps_command,
            timeout=UI_AUTOMATION_TIMEOUT_SECONDS,
            ps_exe=ps_exe,
            runner=powershell_runner,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            f"{operation} timed out after {UI_AUTOMATION_TIMEOUT_SECONDS}s."
        ) from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"{operation} failed: {err}")

    raw = (proc.stdout or "").strip()
    if not raw:
        raise RuntimeError(f"{operation} returned empty output.")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"{operation} returned invalid JSON: {exc}") from exc
    if not isinstance(data, dict):
        raise RuntimeError(f"{operation} returned non-object JSON payload.")
    return data


_ORIG_RUN_POWERSHELL_UIA_JSON = _run_powershell_uia_json


def _resolve_uia_com_engine(
    *,
    uia_backend: NativeUiaComEngine | None = None,
    powershell_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> NativeUiaComEngine | None:
    """Return an active in-process COM UIA engine, or None to fall back to PowerShell UIA."""
    if (
        powershell_runner is not None
        or _run_powershell_uia_json is not _ORIG_RUN_POWERSHELL_UIA_JSON
    ):
        return None
    if uia_backend is not None:
        return uia_backend if uia_backend.is_available() else None
    if sys.platform == "win32" or has_explicit_default_uia_com_engine():
        engine = get_default_uia_com_engine()
        return engine if engine.is_available() else None
    return None


def _read_foreground_hwnd_and_title(
    *,
    user32: Any = None,
    focus_backend: FocusPreconditionBackend | None = None,
) -> tuple[int, str]:
    """Read active foreground HWND and title safely without raising on mocked/headless hosts."""
    if focus_backend is not None:
        try:
            ctx = focus_backend.get_foreground_context()
            if ctx is not None:
                return int(getattr(ctx, "hwnd", 0) or 0), str(getattr(ctx, "title", "") or "")
        except Exception:
            pass
    try:
        u32 = _get_win32_user32(user32)
        fg_hwnd = 0
        if hasattr(u32, "GetForegroundWindow"):
            raw = u32.GetForegroundWindow()
            if isinstance(raw, int) and not isinstance(raw, bool):
                fg_hwnd = int(raw)
        fg_title = _get_foreground_window_title(user32=u32)
        return fg_hwnd, fg_title
    except Exception:
        return 0, ""


def _ensure_window_foreground(hwnd: int, u32: Any) -> None:
    """Restore and foreground the target window when it is not currently active."""
    if hwnd <= 0:
        return
    if hasattr(u32, "GetForegroundWindow"):
        try:
            if int(u32.GetForegroundWindow()) == hwnd:
                return
        except (TypeError, ValueError):
            pass
    if hasattr(u32, "IsIconic") and u32.IsIconic(hwnd):
        u32.ShowWindow(hwnd, SW_RESTORE)
    if hasattr(u32, "SetForegroundWindow"):
        u32.SetForegroundWindow(hwnd)


def _send_key_chord(u32: Any, keys: list[str]) -> list[str]:
    """Validate and dispatch a key chord via Win32 SendInput."""
    if not isinstance(keys, list) or len(keys) == 0:
        raise ValueError("keys must be a non-empty list of key names for 'press_keys'.")
    if len(keys) > 6:
        raise ValueError("keys chord cannot exceed 6 keys.")
    normalized_keys = [_normalize_key_name(k) for k in keys]
    combo_set = frozenset(normalized_keys)
    for blocked in _BLOCKED_KEY_COMBOS:
        if blocked.issubset(combo_set):
            raise ValueError(f"Blocked dangerous key combination: {'+'.join(normalized_keys)}")
    events: list[_INPUT] = []
    for k_name in normalized_keys:
        vk = _ALLOWED_VK_MAP[k_name]
        ext = KEYEVENTF_EXTENDEDKEY if k_name in _EXTENDED_VK_KEYS else 0
        events.append(
            _INPUT(
                type=INPUT_KEYBOARD,
                union=_INPUT_UNION(ki=_KEYBDINPUT(wVk=vk, wScan=0, dwFlags=ext)),
            )
        )
    for k_name in reversed(normalized_keys):
        vk = _ALLOWED_VK_MAP[k_name]
        ext = KEYEVENTF_EXTENDEDKEY if k_name in _EXTENDED_VK_KEYS else 0
        events.append(
            _INPUT(
                type=INPUT_KEYBOARD,
                union=_INPUT_UNION(ki=_KEYBDINPUT(wVk=vk, wScan=0, dwFlags=ext | KEYEVENTF_KEYUP)),
            )
        )
    _send_win32_inputs(u32, events)
    return normalized_keys


_OS_INPUT_LOCK = threading.RLock()
_ACTIVE_CANCEL_LOCK = threading.Lock()
_ACTIVE_CANCEL_EVENTS: set[threading.Event] = set()

BULK_TEXT_PASTE_THRESHOLD: int = 40

_MODIFIER_VK_CANDIDATES: tuple[tuple[int, bool], ...] = (
    (0xA2, False),  # VK_LCONTROL
    (0xA3, True),   # VK_RCONTROL
    (0x11, False),  # VK_CONTROL
    (0xA0, False),  # VK_LSHIFT
    (0xA1, False),  # VK_RSHIFT
    (0x10, False),  # VK_SHIFT
    (0xA4, False),  # VK_LMENU
    (0xA5, True),   # VK_RMENU
    (0x12, False),  # VK_MENU
    (0x5B, True),   # VK_LWIN
    (0x5C, True),   # VK_RWIN
)


class ActionCancelledError(RuntimeError):
    """Raised when an in-flight Win32 OS input action is cancelled out-of-band."""


@contextlib.contextmanager
def track_cancellable_os_action(
    cancel_event: threading.Event | None = None,
) -> Iterator[threading.Event]:
    """Register a cooperative cancellation event for the duration of an OS action."""
    ev = cancel_event if cancel_event is not None else threading.Event()
    with _ACTIVE_CANCEL_LOCK:
        _ACTIVE_CANCEL_EVENTS.add(ev)
    try:
        yield ev
    finally:
        with _ACTIVE_CANCEL_LOCK:
            _ACTIVE_CANCEL_EVENTS.discard(ev)


def cancel_inflight_os_actions() -> int:
    """Signal all registered in-flight OS input cancellation events and return count."""
    with _ACTIVE_CANCEL_LOCK:
        events = list(_ACTIVE_CANCEL_EVENTS)
    for ev in events:
        ev.set()
    return len(events)


def _check_cancelled(cancel_event: threading.Event | None) -> None:
    if cancel_event is not None and cancel_event.is_set():
        raise ActionCancelledError("os_action_cancelled")


def _release_stuck_modifiers(u32: Any) -> list[int]:
    """Release any physically/logically stuck modifier keys via KEYEVENTF_KEYUP."""
    if u32 is None or not hasattr(u32, "GetAsyncKeyState") or not hasattr(u32, "SendInput"):
        return []
    released: list[int] = []
    events: list[_INPUT] = []
    for vk, is_ext in _MODIFIER_VK_CANDIDATES:
        with contextlib.suppress(Exception):
            raw_state = u32.GetAsyncKeyState(vk)
            if isinstance(raw_state, int) and not isinstance(raw_state, bool):
                if (raw_state & 0x8000) != 0:
                    flags = KEYEVENTF_KEYUP | (KEYEVENTF_EXTENDEDKEY if is_ext else 0)
                    events.append(
                        _INPUT(
                            type=INPUT_KEYBOARD,
                            union=_INPUT_UNION(ki=_KEYBDINPUT(wVk=vk, wScan=0, dwFlags=flags)),
                        )
                    )
                    released.append(vk)
    if events:
        with contextlib.suppress(Exception):
            _send_win32_inputs(u32, events)
    return released


def enter_text_tiered(
    text: str,
    *,
    u32: Any,
    interval_ms: int = 12,
    sleep_fn: Callable[[float], None] | None = None,
    guard: PreconditionGuard | None = None,
    clipboard_backend: Any | None = None,
    cancel_event: threading.Event | None = None,
    use_bulk_paste: bool | None = None,
    paste_settle_s: float = 0.15,
    step_label: str = "type_text",
) -> dict[str, Any]:
    """Enter text via Tier-2 atomic clipboard paste (bulk/multiline) or Tier-3 SendInput.

    Note: Tier-1 ``ValuePattern`` has replace semantics and is strictly reserved for
    ``ui_act(action="set_value")``, never append-style typing.
    """
    _check_cancelled(cancel_event)
    sleeper = sleep_fn or time.sleep
    should_paste = (
        use_bulk_paste
        if use_bulk_paste is not None
        else (len(text) >= BULK_TEXT_PASTE_THRESHOLD or "\n" in text or "\r" in text)
    )

    effective_cb: Any | None = clipboard_backend
    if effective_cb is None and should_paste and isinstance(u32, ctypes.CDLL):
        effective_cb = Win32ClipboardBackend()

    if (
        should_paste
        and effective_cb is not None
        and hasattr(effective_cb, "capture_raw_formats")
        and hasattr(effective_cb, "write_text_transient")
        and hasattr(effective_cb, "restore_raw_formats")
    ):
        with _OS_INPUT_LOCK:
            _check_cancelled(cancel_event)
            _release_stuck_modifiers(u32)
            if guard is not None:
                guard.assert_ready(step_label=f"{step_label}:pre_paste")
            saved_formats: dict[int, bytes] | None = None
            try:
                saved_formats = effective_cb.capture_raw_formats(attempts=5, retry_delay_s=0.02)
                effective_cb.write_text_transient(text, attempts=5, retry_delay_s=0.02)
            except ClipboardLockedError as lock_exc:
                logger.info(
                    "Clipboard locked before bulk paste (%s); falling back to SendInput.",
                    lock_exc,
                )
                saved_formats = None
            else:
                restored_ok = True
                try:
                    _check_cancelled(cancel_event)
                    if guard is not None:
                        guard.assert_ready(step_label=f"{step_label}:ctrl_v")
                    _send_key_chord(u32, ["ctrl", "v"])
                    remaining = max(0.0, float(paste_settle_s))
                    while remaining > 0.0:
                        _check_cancelled(cancel_event)
                        chunk = min(0.025, remaining)
                        sleeper(chunk)
                        remaining -= chunk
                finally:
                    try:
                        effective_cb.restore_raw_formats(
                            saved_formats or {},
                            exclude_history=True,
                            attempts=5,
                            retry_delay_s=0.02,
                        )
                    except Exception as restore_exc:
                        restored_ok = False
                        logger.warning("Failed to restore clipboard after paste: %s", restore_exc)
                return {
                    "method": "clipboard_paste_atomic",
                    "char_count": len(text),
                    "interval_ms": 0,
                    "clipboard_restored": restored_ok,
                }

    with _OS_INPUT_LOCK:
        clamped_interval = _send_unicode_text(
            u32,
            text,
            interval_ms=interval_ms,
            sleep_fn=sleeper,
            guard=guard,
            step_label=step_label,
            cancel_event=cancel_event,
        )
    return {
        "method": "send_unicode_text",
        "char_count": len(text),
        "interval_ms": clamped_interval,
    }


def _send_unicode_text(
    u32: Any,
    text: str,
    *,
    interval_ms: int = 12,
    sleep_fn: Callable[[float], None] | None = None,
    guard: PreconditionGuard | None = None,
    step_label: str = "type_text",
    cancel_event: threading.Event | None = None,
) -> int:
    """Send Unicode text via Win32 SendInput with atomic keydown/keyup pairs."""
    if isinstance(interval_ms, bool) or not isinstance(interval_ms, int):
        raise ValueError("interval_ms must be an integer.")
    clamped_interval = max(0, min(200, interval_ms))
    if not text:
        return clamped_interval

    normalized_text = text.replace("\r\n", "\n").replace("\r", "\n")

    def _encode_char_pairs(ch: str) -> list[list[_INPUT]]:
        if ch == "\n":
            return [
                [
                    _INPUT(
                        type=INPUT_KEYBOARD,
                        union=_INPUT_UNION(
                            ki=_KEYBDINPUT(wVk=VK_RETURN_OS, wScan=0, dwFlags=0)
                        ),
                    ),
                    _INPUT(
                        type=INPUT_KEYBOARD,
                        union=_INPUT_UNION(
                            ki=_KEYBDINPUT(
                                wVk=VK_RETURN_OS,
                                wScan=0,
                                dwFlags=KEYEVENTF_KEYUP,
                            )
                        ),
                    ),
                ]
            ]
        if ch == "\t":
            return [
                [
                    _INPUT(
                        type=INPUT_KEYBOARD,
                        union=_INPUT_UNION(
                            ki=_KEYBDINPUT(wVk=VK_TAB_OS, wScan=0, dwFlags=0)
                        ),
                    ),
                    _INPUT(
                        type=INPUT_KEYBOARD,
                        union=_INPUT_UNION(
                            ki=_KEYBDINPUT(
                                wVk=VK_TAB_OS,
                                wScan=0,
                                dwFlags=KEYEVENTF_KEYUP,
                            )
                        ),
                    ),
                ]
            ]
        utf16_bytes = ch.encode("utf-16-le")
        pairs: list[list[_INPUT]] = []
        for idx in range(0, len(utf16_bytes), 2):
            code_unit = int.from_bytes(utf16_bytes[idx : idx + 2], "little")
            pairs.append(
                [
                    _INPUT(
                        type=INPUT_KEYBOARD,
                        union=_INPUT_UNION(
                            ki=_KEYBDINPUT(wVk=0, wScan=code_unit, dwFlags=KEYEVENTF_UNICODE)
                        ),
                    ),
                    _INPUT(
                        type=INPUT_KEYBOARD,
                        union=_INPUT_UNION(
                            ki=_KEYBDINPUT(
                                wVk=0,
                                wScan=code_unit,
                                dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP,
                            )
                        ),
                    ),
                ]
            )
        return pairs

    sleeper = sleep_fn or time.sleep
    delay_s = clamped_interval / 1000.0
    last_idx = len(normalized_text) - 1
    _release_stuck_modifiers(u32)
    is_real_guard = isinstance(guard, PreconditionGuard)
    for char_idx, ch in enumerate(normalized_text):
        _check_cancelled(cancel_event)
        if guard is not None:
            if is_real_guard:
                guard.assert_ready(step_label=f"{step_label}_char_{char_idx}", fast=True)
            else:
                guard.assert_ready(step_label=f"{step_label}_char_{char_idx}")
        _check_cancelled(cancel_event)
        for pair in _encode_char_pairs(ch):
            _send_win32_inputs(u32, pair)
        if clamped_interval > 0 and char_idx < last_idx:
            sleeper(delay_s)
    return clamped_interval


def _extract_element_geometry(item: dict[str, Any]) -> tuple[dict[str, int], dict[str, int]]:
    """Extract normalized bounds and center dicts from a parsed UIA element item."""
    raw_bounds = item.get("bounds")
    if isinstance(raw_bounds, dict):
        bx = int(raw_bounds.get("x", 0))
        by = int(raw_bounds.get("y", 0))
        bw = int(raw_bounds.get("width", 0))
        bh = int(raw_bounds.get("height", 0))
    else:
        bx = int(item.get("x", 0))
        by = int(item.get("y", 0))
        bw = int(item.get("width", 0))
        bh = int(item.get("height", 0))

    raw_center = item.get("center")
    if isinstance(raw_center, dict):
        cx = int(raw_center.get("cx", 0))
        cy = int(raw_center.get("cy", 0))
    elif "cx" in item or "cy" in item:
        cx = int(item.get("cx", 0))
        cy = int(item.get("cy", 0))
    elif bw > 0 and bh > 0:
        cx = round(bx + (bw / 2.0))
        cy = round(by + (bh / 2.0))
    else:
        cx = bx
        cy = by

    return {"x": bx, "y": by, "width": bw, "height": bh}, {"cx": cx, "cy": cy}


def ui_snapshot(
    app_name: str | None = None,
    max_elements: int = 80,
    interactive_only: bool = False,
    *,
    powershell_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    user32: Any = None,
    kernel32: Any = None,
    window_enumerator: Callable[[], list[EnumeratedWindow]] | None = None,
    cdp_bridge: CDPBridge | None = None,
    uia_backend: NativeUiaComEngine | None = None,
    _target_hwnd: int | None = None,
) -> dict[str, Any]:
    """Capture a semantic UI snapshot (CDP 'b1'..'bN' for Chromium, UIA 'e1'..'eN' for native)."""
    global _LAST_UI_SNAPSHOT, _LAST_UI_SNAPSHOT_TIME, _LAST_UI_WINDOW_TITLE
    if app_name is not None and not isinstance(app_name, str):
        raise ValueError("app_name must be a string when provided.")
    if isinstance(max_elements, bool) or not isinstance(max_elements, int):
        raise ValueError("max_elements must be an integer.")
    if not isinstance(interactive_only, bool):
        raise ValueError("interactive_only must be a boolean.")
    clamped_max = max(1, min(MAX_UI_INSPECT_ELEMENTS, max_elements))

    cleaned_app = app_name.strip() if app_name is not None else None
    if cleaned_app == "":
        raise ValueError("app_name must not be empty when provided.")

    if _target_hwnd is not None and _target_hwnd > 0:
        hwnd = int(_target_hwnd)
    else:
        hwnd = _resolve_target_hwnd_optional(
            cleaned_app,
            powershell_runner=powershell_runner,
            user32=user32,
            kernel32=kernel32,
            window_enumerator=window_enumerator,
            focus_if_app_name=True,
        )

    # Chromium UIA Guard: NEVER run UIA (COM or PowerShell) against chrome/msedge/brave.
    # Always delegate to the CDP bridge or return a fast (<200ms) structured fallback.
    proc_name, target_title, is_pwa_hint = _resolve_target_process_info(
        hwnd=hwnd,
        app_name=cleaned_app,
        user32=user32,
        kernel32=kernel32,
        window_enumerator=window_enumerator,
        powershell_runner=powershell_runner,
    )
    if is_chromium_process(proc_name) or is_chromium_app_name(cleaned_app):
        bridge = cdp_bridge or get_default_cdp_bridge()
        eff_title_hint = target_title or (
            None if is_chromium_app_name(cleaned_app) else cleaned_app
        )
        cdp_res = bridge.snapshot_sync(
            max_elements=min(clamped_max, 60),
            prefer_pwa=is_pwa_hint,
            title_hint=eff_title_hint,
            interactive_only=interactive_only,
            preferred_browser=(
                proc_name or (cleaned_app if is_chromium_app_name(cleaned_app) else None)
            ),
        )
        if isinstance(cdp_res, dict) and cdp_res.get("ok"):
            _LAST_UI_SNAPSHOT = cdp_res
            _LAST_UI_SNAPSHOT_TIME = time.time()
            _LAST_UI_WINDOW_TITLE = str(cdp_res.get("window_title") or cdp_res.get("title") or "")
            return cdp_res
        if isinstance(cdp_res, dict) and cdp_res.get("cdp_available") is True:
            out = dict(cdp_res)
            if not out.get("window_title"):
                out["window_title"] = target_title
            out.setdefault("process_name", proc_name or (cleaned_app or "chrome.exe"))
            return out
        return _chromium_cdp_unavailable_snapshot(
            window_title=target_title,
            process_name=proc_name or (cleaned_app or "chrome.exe"),
            error=str(cdp_res.get("error") or "") if isinstance(cdp_res, dict) else None,
        )

    # Primary path on Windows: in-process native COM IUIAutomation engine
    com_engine = _resolve_uia_com_engine(
        uia_backend=uia_backend,
        powershell_runner=powershell_runner,
    )
    if com_engine is not None:
        try:
            com_snap = com_engine.snapshot(
                hwnd=hwnd,
                app_name=cleaned_app,
                max_elements=clamped_max,
                interactive_only=interactive_only,
            )
            snapshot_id = str(com_snap.get("snapshot_id") or f"snap_{uuid.uuid4().hex[:12]}")
            window_title = str(com_snap.get("window_title") or target_title or "")
            raw_com_hwnd = com_snap.get("hwnd")
            resolved_hwnd = hwnd or (
                int(raw_com_hwnd)
                if isinstance(raw_com_hwnd, int) and not isinstance(raw_com_hwnd, bool)
                else 0
            )
            elements_by_ref = dict(com_snap.get("elements_by_ref") or {})
            llm_elements = list(com_snap.get("elements") or [])
            _UI_REF_STORE.update(
                snapshot_id=snapshot_id,
                window_title=window_title,
                hwnd=resolved_hwnd,
                elements_by_ref=elements_by_ref,
            )
            snap_result: dict[str, Any] = {
                "ok": True,
                "source": "uia_com",
                "snapshot_id": snapshot_id,
                "window_title": window_title,
                "count": len(llm_elements),
                "element_count": len(llm_elements),
                "elements": llm_elements,
                "accessibility_coverage": com_snap.get("accessibility_coverage"),
                "content_element_count": com_snap.get("content_element_count"),
            }
            if com_snap.get("hint"):
                snap_result["hint"] = com_snap["hint"]
            _LAST_UI_SNAPSHOT = snap_result
            _LAST_UI_SNAPSHOT_TIME = time.time()
            _LAST_UI_WINDOW_TITLE = window_title
            return snap_result
        except UiaComUnavailableError as exc:
            logger.debug("COM UIA snapshot unavailable, falling back to PowerShell: %s", exc)
        except Exception as exc:
            logger.debug("COM UIA snapshot failed, falling back to PowerShell: %s", exc)

    app_b64 = base64.b64encode(cleaned_app.encode("utf-8")).decode("ascii") if cleaned_app else ""
    interactive_flag = 1 if interactive_only else 0
    ps_command = (
        f"& {{ {_POWERSHELL_SNAPSHOT_UI_SCRIPT} }} "
        f"-TargetHwnd {hwnd} -MaxElements {clamped_max} "
        f"-InteractiveOnly {interactive_flag} -AppNameBase64 '{app_b64}'"
    )
    data = _run_powershell_uia_json(
        ps_command,
        operation="UI Automation snapshot",
        powershell_runner=powershell_runner,
    )

    window_title = str(data.get("window_title") or "")
    raw_hwnd = data.get("hwnd")
    resolved_hwnd = hwnd or (
        int(raw_hwnd) if isinstance(raw_hwnd, int) and not isinstance(raw_hwnd, bool) else 0
    )

    raw_elements = data.get("elements") or []
    if isinstance(raw_elements, dict):
        raw_elements = [raw_elements]

    llm_elements = []
    elements_by_ref = {}

    for item in raw_elements:
        if not isinstance(item, dict):
            continue
        raw_ct = str(item.get("control_type") or item.get("role") or "")
        role = _normalize_ui_role(raw_ct)
        name = str(item.get("name") or "")
        raw_val = item.get("value")
        value = str(raw_val) if raw_val is not None else None
        enabled = bool(item.get("enabled", True))
        focused = bool(item.get("focused", False))
        automation_id = str(item.get("automation_id") or "")

        if role == "text":
            if interactive_only:
                continue
            if not name.strip() and (value is None or not value.strip()):
                continue
        elif interactive_only and not enabled:
            continue

        bounds, center = _extract_element_geometry(item)
        cx = int(center.get("cx", 0))
        cy = int(center.get("cy", 0))

        if "ref" in item and item["ref"]:
            ref = str(item["ref"])
        elif "bounds" in item:
            ref = f"e{len(llm_elements) + 1}"
        else:
            ref = f"e{len(llm_elements)}"

        control_type = str(item.get("control_type") or "") or _ROLE_TO_CONTROL_TYPE.get(role, "")

        elements_by_ref[ref] = {
            "ref": ref,
            "role": role,
            "name": name,
            "value": value,
            "enabled": enabled,
            "focused": focused,
            "bounds": bounds,
            "center": center,
            "x": cx,
            "y": cy,
            "automation_id": automation_id,
            "control_type": control_type,
        }

        if "bounds" in item:
            llm_elements.append(
                {
                    "ref": ref,
                    "role": role,
                    "name": name,
                    "value": value,
                    "enabled": enabled,
                    "focused": focused,
                }
            )
        else:
            llm_elements.append(
                {
                    "ref": ref,
                    "role": role,
                    "name": name,
                    "value": value,
                    "x": cx,
                    "y": cy,
                    "automation_id": automation_id,
                    "control_type": control_type,
                    "enabled": enabled,
                    "focused": focused,
                }
            )

        if len(llm_elements) >= clamped_max:
            break

    snapshot_id = f"snap_{uuid.uuid4().hex[:12]}"
    _UI_REF_STORE.update(
        snapshot_id=snapshot_id,
        window_title=window_title,
        hwnd=resolved_hwnd,
        elements_by_ref=elements_by_ref,
    )

    coverage, content_count, guidance = classify_accessibility_coverage(
        list(elements_by_ref.values())
    )
    snap_result = {
        "ok": True,
        "snapshot_id": snapshot_id,
        "window_title": window_title,
        "count": len(llm_elements),
        "element_count": len(llm_elements),
        "elements": llm_elements,
        "accessibility_coverage": coverage,
        "content_element_count": content_count,
    }
    if guidance:
        snap_result["hint"] = guidance
    _LAST_UI_SNAPSHOT = snap_result
    _LAST_UI_SNAPSHOT_TIME = time.time()
    _LAST_UI_WINDOW_TITLE = window_title

    return snap_result


def _action_runner_accepts_guard(runner: Any) -> bool:
    """Return True if a gui_action runner explicitly accepts the internal _guard kwarg."""
    if runner is gui_action:
        return True
    try:
        params = inspect.signature(runner).parameters
    except (TypeError, ValueError):
        return False
    return "_guard" in params


def ui_act(
    ref: str,
    action: str = "click",
    value: str | None = None,
    interval_ms: int = 12,
    refresh_snapshot: bool = False,
    *,
    powershell_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    user32: Any = None,
    sleep_fn: Callable[[float], None] | None = None,
    ui_snapshot_fn: Callable[..., dict[str, Any]] | None = None,
    gui_action_fn: Callable[..., dict[str, Any]] | None = None,
    focus_backend: FocusPreconditionBackend | None = None,
    expected_hwnd: int | None = None,
    require_foreground: bool = True,
    cdp_bridge: CDPBridge | None = None,
    uia_backend: NativeUiaComEngine | None = None,
    _guard: PreconditionGuard | None = None,
    _capture_snapshot: bool | None = None,
    cancel_event: threading.Event | None = None,
    clipboard_backend: Any | None = None,
    view_fingerprint_fn: Callable[..., Any] | None = None,
    verify_view: bool | None = None,
) -> dict[str, Any]:
    """Execute a semantic action on an indexed UI ('e#') or CDP browser ('b#') element."""
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError("ref must be a non-empty element reference (e.g. 'e1', '@e1', or 'b1').")

    if not isinstance(action, str) or not action.strip():
        raise ValueError("action must be a non-empty string.")
    norm_action = action.strip().lower()
    if norm_action not in VALID_UI_ACT_ACTIONS:
        raise ValueError(
            f"Unsupported UI act action {action!r}. Unsupported ui_act action; "
            f"must be one of {sorted(VALID_UI_ACT_ACTIONS)}."
        )
    if not isinstance(refresh_snapshot, bool):
        raise ValueError("refresh_snapshot must be a boolean.")

    # Route browser CDP refs ('b1', 'b2', ...) directly to the CDP bridge
    if is_cdp_ref(ref):
        bridge = cdp_bridge or get_default_cdp_bridge()
        return bridge.act_sync(
            ref,
            norm_action,
            text=value if norm_action in {"type", "set_value"} else None,
            key=value if norm_action == "press" else None,
        )

    norm_ref = _normalize_ref_id(ref)

    if isinstance(interval_ms, bool) or not isinstance(interval_ms, int):
        raise ValueError("interval_ms must be an integer.")
    clamped_interval = max(0, min(200, interval_ms))

    if norm_action == "set_value":
        if value is None or not isinstance(value, str):
            raise ValueError("value must be a string for 'set_value' action.")
        if len(value) > MAX_GUI_TYPE_TEXT_LEN:
            raise ValueError(
                f"value length ({len(value)}) exceeds maximum allowed ({MAX_GUI_TYPE_TEXT_LEN})."
            )
    elif norm_action == "type":
        if not isinstance(value, str) or len(value) == 0:
            raise ValueError(
                "value string is required for 'type' action (must be a non-empty string)."
            )
        if len(value) > MAX_GUI_TYPE_TEXT_LEN:
            raise ValueError(
                f"value length ({len(value)}) exceeds maximum allowed ({MAX_GUI_TYPE_TEXT_LEN})."
            )
    elif norm_action == "press":
        if not isinstance(value, str) or not value.strip():
            raise ValueError(
                "value key name is required for 'press' action (e.g. 'Enter')."
            )

    snap_runner = ui_snapshot_fn or ui_snapshot
    action_runner = gui_action_fn or gui_action

    global _LAST_UI_SNAPSHOT, _LAST_UI_SNAPSHOT_TIME, _LAST_UI_WINDOW_TITLE
    if (not _UI_REF_STORE or _UI_REF_STORE.stale) and ui_snapshot_fn is not None:
        snap = snap_runner(user32=user32)
        set_ui_snapshot_state(snap)

    if norm_ref not in _UI_REF_STORE:
        avail = ", ".join(list(_UI_REF_STORE.keys())[:25]) or "none"
        curr_snap = _UI_REF_STORE.snapshot_id or "none"
        if not _UI_REF_STORE.snapshot_id:
            raise ValueError(
                f"Unknown or stale UI ref {ref!r}: no active snapshot. Call ui_snapshot() first."
            )
        raise ValueError(
            f"Unknown or stale UI ref {ref!r}: UI element ref {ref!r} not found in "
            f"current snapshot {curr_snap}. Available refs: {avail}."
        )

    snapshot_id, window_title, snapshot_hwnd, entry = _UI_REF_STORE.resolve(norm_ref)
    bounds = entry.get("bounds") or {}
    center = entry.get("center") or {}
    target_cx = int(center.get("cx") or entry.get("x", 0))
    target_cy = int(center.get("cy") or entry.get("y", 0))
    before_snap = _LAST_UI_SNAPSHOT or {
        "ok": True,
        "snapshot_id": snapshot_id,
        "window_title": window_title,
        "count": len(_UI_REF_STORE),
        "elements": list(_UI_REF_STORE.values()),
    }

    is_fake_user32 = (
        hasattr(user32, "sent_inputs")
        or hasattr(user32, "cursor_moves")
        or hasattr(user32, "key_events")
    )
    before_fg_hwnd, before_fg_title = _read_foreground_hwnd_and_title(
        user32=user32, focus_backend=focus_backend
    )
    should_verify_view = (
        view_fingerprint_fn is not None
        or verify_view is True
        or (
            verify_view is None
            and not is_fake_user32
            and user32 is None
            and powershell_runner is None
            and uia_backend is None
            and gui_action_fn is None
            and sys.platform == "win32"
        )
    )
    before_view_fp = (
        _capture_view_fp_safe(
            before_fg_hwnd or (snapshot_hwnd if snapshot_hwnd > 0 else None),
            view_fingerprint_fn=view_fingerprint_fn,
        )
        if should_verify_view
        else None
    )

    com_engine = _resolve_uia_com_engine(
        uia_backend=uia_backend,
        powershell_runner=powershell_runner,
    )

    method = ""
    should_guard = _guard is None and (
        focus_backend is not None
        or (
            user32 is None
            and (gui_action_fn is None or gui_action_fn is gui_action)
            and powershell_runner is None
            and uia_backend is None
        )
    )
    target_guard_hwnd = (
        expected_hwnd
        if expected_hwnd is not None
        else (snapshot_hwnd if snapshot_hwnd > 0 else None)
    )
    resolved_guard_hwnd: int | None = target_guard_hwnd
    active_guard: PreconditionGuard | None = _guard
    aborted_result: dict[str, Any] | None = None
    stale_result: dict[str, Any] | None = None

    def _assert_step(step_label: str) -> None:
        _check_cancelled(cancel_event)
        if active_guard is not None:
            active_guard.assert_ready(step_label)

    def _assert_point(px: int, py: int, step_label: str) -> None:
        _check_cancelled(cancel_event)
        if active_guard is not None:
            active_guard.assert_point_targets_window(px, py, step_label=step_label)

    can_try_ps_uia = (
        powershell_runner is not None
        or _run_powershell_uia_json is not _ORIG_RUN_POWERSHELL_UIA_JSON
        or (
            com_engine is None
            and uia_backend is None
            and shutil.which("powershell.exe") is not None
        )
    )
    can_try_uia = (
        gui_action_fn is None
        and norm_action in {"click", "set_value"}
        and (com_engine is not None or can_try_ps_uia)
    )

    def _run_uia_pattern_step() -> bool:
        nonlocal method, target_cx, target_cy, stale_result
        _assert_step(f"uia_{norm_action}")

        # 1. Primary path: direct cached COM IUIAutomationElement pattern dispatch (<15 ms)
        com_failed = False
        if com_engine is not None:
            try:
                com_res = com_engine.act(snapshot_id, norm_ref, norm_action, value=value)
                if com_res.get("stale"):
                    stale_result = {
                        "ok": False,
                        "status": "stale_ref",
                        "stale_ref": True,
                        "reason": str(com_res.get("reason") or "stale_element"),
                        "snapshot_id": snapshot_id,
                        "ref": norm_ref,
                        "action": norm_action,
                        "role": entry.get("role", ""),
                        "name": entry.get("name", ""),
                        "method": "",
                        "error": str(
                            com_res.get("error")
                            or f"UI element ref {norm_ref!r} is stale. Call ui_snapshot()."
                        ),
                    }
                    return True
                if com_res.get("found"):
                    if isinstance(com_res.get("cx"), int) and not isinstance(
                        com_res.get("cx"), bool
                    ):
                        target_cx = int(com_res["cx"])
                    if isinstance(com_res.get("cy"), int) and not isinstance(
                        com_res.get("cy"), bool
                    ):
                        target_cy = int(com_res["cy"])
                if com_res.get("acted"):
                    method = str(
                        com_res.get("method")
                        or ("value_pattern" if norm_action == "set_value" else "invoke")
                    )
                    return True
                # Cached handle unsupported or LRU cache miss while COM is healthy -> Win32 fallback
                return False
            except UiaComUnavailableError as exc:
                com_failed = True
                logger.debug("COM UIA act unavailable, falling back to PowerShell: %s", exc)
            except Exception as exc:
                com_failed = True
                logger.debug("COM UIA act failed, falling back: %s", exc)

        if not (
            can_try_ps_uia
            or (com_failed and uia_backend is None and shutil.which("powershell.exe") is not None)
        ):
            return False

        # 2. Fallback path: PowerShell UIA pattern dispatch
        title_b64 = (
            base64.b64encode(window_title.encode("utf-8")).decode("ascii")
            if window_title
            else ""
        )
        aid_b64 = (
            base64.b64encode(entry.get("automation_id", "").encode("utf-8")).decode("ascii")
            if entry.get("automation_id")
            else ""
        )
        name_b64 = (
            base64.b64encode(entry.get("name", "").encode("utf-8")).decode("ascii")
            if entry.get("name")
            else ""
        )
        val_b64 = (
            base64.b64encode(value.encode("utf-8")).decode("ascii")
            if norm_action == "set_value" and value
            else ""
        )
        ct_arg = str(entry.get("control_type") or "")
        if ct_arg and not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", ct_arg):
            ct_arg = ""
        ps_target_hwnd = (
            resolved_guard_hwnd
            if (resolved_guard_hwnd is not None and resolved_guard_hwnd > 0)
            else (snapshot_hwnd if snapshot_hwnd > 0 else (expected_hwnd or 0))
        )
        ps_command = (
            f"& {{ {_POWERSHELL_UI_ACT_SCRIPT} }} "
            f"-TargetHwnd {ps_target_hwnd} -WindowTitleBase64 '{title_b64}' "
            f"-Action '{norm_action}' -AutomationIdBase64 '{aid_b64}' "
            f"-NameBase64 '{name_b64}' -ControlType '{ct_arg}' "
            f"-ExpectedX {int(bounds.get('x', target_cx))} "
            f"-ExpectedY {int(bounds.get('y', target_cy))} "
            f"-ExpectedW {int(bounds.get('width', 0))} "
            f"-ExpectedH {int(bounds.get('height', 0))} "
            f"-CenterX {target_cx} -CenterY {target_cy} -ValueBase64 '{val_b64}'"
        )
        data = _run_powershell_uia_json(
            ps_command,
            operation=f"UI Automation {norm_action}",
            powershell_runner=powershell_runner,
        )
        if data.get("found"):
            if isinstance(data.get("cx"), int) and not isinstance(data.get("cx"), bool):
                target_cx = int(data["cx"])
            if isinstance(data.get("cy"), int) and not isinstance(data.get("cy"), bool):
                target_cy = int(data["cy"])
        if data.get("acted"):
            method = str(
                data.get("method")
                or ("value_pattern" if norm_action == "set_value" else "invoke")
            )
            return True
        return False

    def _run_win32_fallback_steps() -> None:
        nonlocal method
        u32 = _get_win32_user32(user32)
        if active_guard is None:
            _ensure_window_foreground(snapshot_hwnd, u32)

        with _OS_INPUT_LOCK:
            if norm_action == "click":
                _assert_point(target_cx, target_cy, "win32_click")
                _perform_mouse_click(target_cx, target_cy, clicks=1, button="left", user32=u32)
                method = "win32_click"
            elif norm_action == "double_click":
                _assert_point(target_cx, target_cy, "win32_double_click")
                _perform_mouse_click(
                    target_cx, target_cy, clicks=1, button="double", user32=u32
                )
                method = "win32_double_click"
            elif norm_action == "right_click":
                _assert_point(target_cx, target_cy, "win32_right_click")
                _perform_mouse_click(target_cx, target_cy, clicks=1, button="right", user32=u32)
                method = "win32_right_click"
            elif norm_action == "set_value":
                assert value is not None
                _assert_point(target_cx, target_cy, "set_value_click")
                _perform_mouse_click(target_cx, target_cy, clicks=1, button="left", user32=u32)
                _assert_step("set_value_select_all")
                _send_key_chord(u32, ["ctrl", "a"])
                _assert_step("set_value_backspace")
                _send_key_chord(u32, ["backspace"])
                if value:
                    _assert_step("set_value_type_text")
                    enter_text_tiered(
                        value,
                        u32=u32,
                        interval_ms=clamped_interval,
                        sleep_fn=sleep_fn,
                        guard=active_guard,
                        clipboard_backend=clipboard_backend,
                        cancel_event=cancel_event,
                        step_label="set_value_type_text",
                    )
                method = "keyboard_fallback"
            elif norm_action == "type":
                assert value is not None
                _assert_point(target_cx, target_cy, "type_click")
                _perform_mouse_click(target_cx, target_cy, clicks=1, button="left", user32=u32)
                _assert_step("type_text")
                enter_text_tiered(
                    value,
                    u32=u32,
                    interval_ms=clamped_interval,
                    sleep_fn=sleep_fn,
                    guard=active_guard,
                    clipboard_backend=clipboard_backend,
                    cancel_event=cancel_event,
                    step_label="type_text",
                )
                method = "win32_type"
            else:  # "clear"
                _assert_point(target_cx, target_cy, "clear_click")
                _perform_mouse_click(target_cx, target_cy, clicks=1, button="left", user32=u32)
                _assert_step("clear_select_all")
                _send_key_chord(u32, ["ctrl", "a"])
                _assert_step("clear_backspace")
                _send_key_chord(u32, ["backspace"])
                method = "win32_clear"

    def _run_act_steps() -> None:
        nonlocal method, aborted_result
        runner_guard_kw: dict[str, Any] = (
            {"_guard": active_guard}
            if (active_guard is not None and _action_runner_accepts_guard(action_runner))
            else {}
        )
        sleep_kw: dict[str, Any] = (
            {"sleep_fn": sleep_fn}
            if (action_runner is gui_action and sleep_fn is not None)
            else {}
        )

        def _check_abort(res: Any) -> bool:
            nonlocal aborted_result
            if isinstance(res, dict) and res.get("status") == "aborted_precondition":
                aborted_result = res
                return True
            return False

        if gui_action_fn is not None:
            if norm_action == "click":
                _assert_step("click")
                if _check_abort(
                    action_runner(
                        action="click_at",
                        x=target_cx,
                        y=target_cy,
                        clicks=1,
                        button="left",
                        user32=user32,
                        **runner_guard_kw,
                    )
                ):
                    return
                method = "click_at"
            elif norm_action == "double_click":
                _assert_step("double_click")
                if _check_abort(
                    action_runner(
                        action="click_at",
                        x=target_cx,
                        y=target_cy,
                        clicks=2,
                        button="double",
                        user32=user32,
                        **runner_guard_kw,
                    )
                ):
                    return
                method = "double_click"
            elif norm_action == "right_click":
                _assert_step("right_click")
                if _check_abort(
                    action_runner(
                        action="click_at",
                        x=target_cx,
                        y=target_cy,
                        clicks=1,
                        button="right",
                        user32=user32,
                        **runner_guard_kw,
                    )
                ):
                    return
                method = "right_click"
            elif norm_action == "clear":
                _assert_step("clear_click")
                if _check_abort(
                    action_runner(
                        action="click_at",
                        x=target_cx,
                        y=target_cy,
                        clicks=1,
                        button="left",
                        user32=user32,
                        **runner_guard_kw,
                    )
                ):
                    return
                _assert_step("clear_select_all")
                if _check_abort(
                    action_runner(
                        action="press_keys",
                        keys=["ctrl", "a"],
                        user32=user32,
                        **runner_guard_kw,
                    )
                ):
                    return
                _assert_step("clear_backspace")
                if _check_abort(
                    action_runner(
                        action="press_keys",
                        keys=["backspace"],
                        user32=user32,
                        **runner_guard_kw,
                    )
                ):
                    return
                method = "clear"
            elif norm_action == "type":
                assert value is not None
                _assert_step("type_click")
                if _check_abort(
                    action_runner(
                        action="click_at",
                        x=target_cx,
                        y=target_cy,
                        clicks=1,
                        button="left",
                        user32=user32,
                        **runner_guard_kw,
                    )
                ):
                    return
                _assert_step("type_text")
                try:
                    step_res = action_runner(
                        action="type_text",
                        text=value,
                        interval_ms=clamped_interval,
                        user32=user32,
                        **sleep_kw,
                        **runner_guard_kw,
                    )
                except TypeError:
                    step_res = action_runner(
                        action="type_text",
                        text=value,
                        user32=user32,
                        **runner_guard_kw,
                    )
                if _check_abort(step_res):
                    return
                method = "type"
            elif norm_action == "set_value":
                val_str = str(value) if value is not None else ""
                _assert_step("set_value_click")
                if _check_abort(
                    action_runner(
                        action="click_at",
                        x=target_cx,
                        y=target_cy,
                        clicks=1,
                        button="left",
                        user32=user32,
                        **runner_guard_kw,
                    )
                ):
                    return
                _assert_step("set_value_select_all")
                if _check_abort(
                    action_runner(
                        action="press_keys",
                        keys=["ctrl", "a"],
                        user32=user32,
                        **runner_guard_kw,
                    )
                ):
                    return
                _assert_step("set_value_backspace")
                if _check_abort(
                    action_runner(
                        action="press_keys",
                        keys=["backspace"],
                        user32=user32,
                        **runner_guard_kw,
                    )
                ):
                    return
                if val_str:
                    _assert_step("set_value_type_text")
                    try:
                        step_res = action_runner(
                            action="type_text",
                            text=val_str,
                            interval_ms=clamped_interval,
                            user32=user32,
                            **sleep_kw,
                            **runner_guard_kw,
                        )
                    except TypeError:
                        step_res = action_runner(
                            action="type_text",
                            text=val_str,
                            user32=user32,
                            **runner_guard_kw,
                        )
                    if _check_abort(step_res):
                        return
                method = "set_value"
            elif norm_action == "press":
                assert value is not None
                _assert_step("press_focus_click")
                if _check_abort(
                    action_runner(
                        action="click_at",
                        x=target_cx,
                        y=target_cy,
                        clicks=1,
                        button="left",
                        user32=user32,
                        **runner_guard_kw,
                    )
                ):
                    return
                _assert_step("press_key")
                if _check_abort(
                    action_runner(
                        action="press_keys",
                        keys=[value.strip()],
                        user32=user32,
                        **runner_guard_kw,
                    )
                ):
                    return
                method = "press"
        else:
            acted_via_uia = _run_uia_pattern_step() if can_try_uia else False
            if not acted_via_uia:
                _run_win32_fallback_steps()

    if should_guard:
        backend = focus_backend or Win32FocusPreconditionBackend()
        try:
            if can_try_uia:
                with PreconditionGuard(
                    backend,
                    expected_hwnd=target_guard_hwnd,
                    require_foreground=False,
                ) as bg_guard:
                    active_guard = bg_guard
                    if bg_guard.target is not None and bg_guard.target.hwnd > 0:
                        resolved_guard_hwnd = bg_guard.target.hwnd
                    acted_via_uia = _run_uia_pattern_step()
                    if not acted_via_uia:
                        bg_guard.assert_ready(
                            "win32_click" if norm_action == "click" else "set_value_click"
                        )
                active_guard = None
                if not acted_via_uia:
                    with PreconditionGuard(
                        backend,
                        expected_hwnd=resolved_guard_hwnd,
                        require_foreground=require_foreground,
                    ) as fg_guard:
                        active_guard = fg_guard
                        _run_win32_fallback_steps()
            else:
                with PreconditionGuard(
                    backend,
                    expected_hwnd=target_guard_hwnd,
                    require_foreground=require_foreground,
                ) as owned_guard:
                    active_guard = owned_guard
                    if owned_guard.target is not None and owned_guard.target.hwnd > 0:
                        resolved_guard_hwnd = owned_guard.target.hwnd
                    _run_act_steps()
        except PreconditionFailedError as exc:
            abort = exc.to_abort_payload()
            abort.update(
                {
                    "snapshot_id": snapshot_id,
                    "ref": norm_ref,
                    "action": norm_action,
                    "role": entry.get("role", ""),
                    "name": entry.get("name", ""),
                    "method": method,
                    "x": target_cx,
                    "y": target_cy,
                }
            )
            return abort
        except ActionCancelledError as exc:
            return {
                "ok": False,
                "status": "cancelled",
                "cancelled": True,
                "error": str(exc),
                "snapshot_id": snapshot_id,
                "ref": norm_ref,
                "action": norm_action,
                "role": entry.get("role", ""),
                "name": entry.get("name", ""),
                "method": method,
            }
        finally:
            active_guard = None
    else:
        try:
            _run_act_steps()
        except ActionCancelledError as exc:
            return {
                "ok": False,
                "status": "cancelled",
                "cancelled": True,
                "error": str(exc),
                "snapshot_id": snapshot_id,
                "ref": norm_ref,
                "action": norm_action,
                "role": entry.get("role", ""),
                "name": entry.get("name", ""),
                "method": method,
            }

    if stale_result is not None:
        return stale_result

    if aborted_result is not None:
        return aborted_result

    if _capture_snapshot is False:
        res_no_snap: dict[str, Any] = {
            "ok": True,
            "status": "completed",
            "snapshot_id": snapshot_id,
            "ref": norm_ref,
            "action": norm_action,
            "role": entry.get("role", ""),
            "name": entry.get("name", ""),
            "method": method,
            "x": target_cx,
            "y": target_cy,
        }
        if norm_action in {"set_value", "type"} and value is not None:
            res_no_snap["char_count"] = len(value)
            res_no_snap["interval_ms"] = clamped_interval
        return res_no_snap

    after_fg_hwnd, after_fg_title = _read_foreground_hwnd_and_title(
        user32=user32, focus_backend=focus_backend
    )
    window_switched = bool(
        (before_fg_hwnd > 0 and after_fg_hwnd > 0 and after_fg_hwnd != before_fg_hwnd)
        or (before_fg_title and after_fg_title and after_fg_title != before_fg_title)
    )
    need_full_refresh = bool(
        refresh_snapshot
        or (_capture_snapshot is True)
        or (ui_snapshot_fn is not None)
        or window_switched
    )

    if not need_full_refresh:
        active_hwnd = after_fg_hwnd or (
            resolved_guard_hwnd
            if (resolved_guard_hwnd is not None and resolved_guard_hwnd > 0)
            else snapshot_hwnd
        )
        active_title = after_fg_title or window_title
        old_value = entry.get("value")
        target_state: dict[str, Any] = {"available": False, "cached": False}
        if com_engine is not None:
            try:
                target_state = com_engine.read_element_state(snapshot_id, norm_ref)
            except Exception:
                target_state = {"available": False, "cached": True}
        if not target_state.get("cached"):
            inferred_val = (
                value
                if norm_action == "set_value"
                else ("" if norm_action == "clear" else old_value)
            )
            target_state = {
                "available": True,
                "cached": False,
                "ref": norm_ref,
                "role": entry.get("role", ""),
                "name": entry.get("name", ""),
                "value": inferred_val,
                "enabled": entry.get("enabled", True),
                "focused": entry.get("focused", False),
            }

        new_value = (
            target_state.get("value")
            if target_state.get("available")
            else (
                value
                if norm_action == "set_value"
                else ("" if norm_action == "clear" else old_value)
            )
        )
        new_name = str(target_state.get("name") or entry.get("name") or "")
        if target_state.get("available"):
            updated_entry = dict(entry)
            updated_entry["name"] = new_name
            updated_entry["value"] = new_value
            if "enabled" in target_state:
                updated_entry["enabled"] = bool(target_state["enabled"])
            if "focused" in target_state:
                updated_entry["focused"] = bool(target_state["focused"])
            _UI_REF_STORE[norm_ref] = updated_entry

        changed_values: list[dict[str, Any]] = []
        if new_value != old_value:
            changed_values.append(
                {
                    "ref": norm_ref,
                    "role": entry.get("role", ""),
                    "name": new_name,
                    "old_value": old_value,
                    "new_value": new_value,
                }
            )
        light_diff: dict[str, Any] = {
            "window_changed": False,
            "window_title": active_title,
            "changed_values": changed_values,
            "added_elements": [],
            "removed_count": 0,
        }
        light_snapshot: dict[str, Any] = {
            "ok": True,
            "status": "lightweight_delta",
            "source": "uia_com" if com_engine is not None else None,
            "snapshot_id": snapshot_id,
            "window_title": active_title,
            "count": 1,
            "element_count": 1,
            "elements": [
                {
                    "ref": norm_ref,
                    "role": entry.get("role", ""),
                    "name": new_name,
                    "value": new_value,
                    "enabled": bool(target_state.get("enabled", entry.get("enabled", True))),
                    "focused": bool(target_state.get("focused", entry.get("focused", False))),
                }
            ],
        }
        light_res: dict[str, Any] = {
            "ok": True,
            "status": "completed",
            "snapshot_id": snapshot_id,
            "ref": norm_ref,
            "action": norm_action,
            "role": entry.get("role", ""),
            "name": new_name,
            "method": method,
            "x": target_cx,
            "y": target_cy,
            "active_window_title": active_title,
            "active_hwnd": active_hwnd,
            "target_state": target_state,
            "snapshot_refreshed": False,
            "diff": light_diff,
            "snapshot": light_snapshot,
        }
        if should_verify_view:
            after_view_fp = _capture_view_fp_safe(
                active_hwnd or None,
                view_fingerprint_fn=view_fingerprint_fn,
            )
            view_diff = _compare_view_fps_safe(before_view_fp, after_view_fp)
            view_changed = bool(view_diff.get("view_changed", False))
            visual_delta = float(view_diff.get("visual_delta", 0.0))
            heading_before = str(view_diff.get("heading_before", "") or "")
            heading_after = str(view_diff.get("heading_after", "") or "")
            light_diff["view_changed"] = view_changed
            light_diff["visual_delta"] = visual_delta
            light_diff["heading_before"] = heading_before
            light_diff["heading_after"] = heading_after
            light_res["view_changed"] = view_changed
            light_res["visual_delta"] = visual_delta
            light_res["heading_before"] = heading_before
            light_res["heading_after"] = heading_after
            if view_changed:
                _UI_REF_STORE.mark_stale("view_changed")
                _LAST_UI_SNAPSHOT_TIME = 0.0
                light_res["refs_stale"] = True
                light_res["goal_verified"] = True
                light_res["verification_scope"] = "in_app_view"
        if norm_action in {"set_value", "type"} and value is not None:
            light_res["char_count"] = len(value)
            light_res["interval_ms"] = clamped_interval
        return light_res

    after_snap: dict[str, Any] = {}
    if ui_snapshot_fn is not None:
        after_snap = ui_snapshot_fn(user32=user32)
        if isinstance(after_snap, dict) and after_snap.get("source") == "cdp":
            if after_snap.get("ok"):
                _record_cdp_snapshot_state(after_snap)
        else:
            set_ui_snapshot_state(after_snap)
    elif (
        (com_engine is not None or (powershell_runner is None and shutil.which("powershell.exe")))
        and (not is_fake_user32 or uia_backend is not None or has_explicit_default_uia_com_engine())
    ):
        after_target_hwnd = (
            after_fg_hwnd
            if (window_switched and after_fg_hwnd > 0)
            else (
                resolved_guard_hwnd
                if (resolved_guard_hwnd is not None and resolved_guard_hwnd > 0)
                else (snapshot_hwnd if snapshot_hwnd > 0 else 0)
            )
        )
        with contextlib.suppress(Exception):
            if ui_snapshot_fn is None and snap_runner is ui_snapshot and after_target_hwnd > 0:
                snap_kw: dict[str, Any] = {"user32": user32, "_target_hwnd": after_target_hwnd}
                if cdp_bridge is not None:
                    snap_kw["cdp_bridge"] = cdp_bridge
                if uia_backend is not None:
                    snap_kw["uia_backend"] = uia_backend
                after_snap = ui_snapshot(**snap_kw)
            else:
                snap_kw_no_hwnd: dict[str, Any] = {"user32": user32}
                if (
                    ui_snapshot_fn is None
                    and snap_runner is ui_snapshot
                    and uia_backend is not None
                ):
                    snap_kw_no_hwnd["uia_backend"] = uia_backend
                after_snap = snap_runner(**snap_kw_no_hwnd)
            if isinstance(after_snap, dict) and after_snap.get("source") == "cdp":
                if after_snap.get("ok"):
                    _record_cdp_snapshot_state(after_snap)
            elif not (isinstance(after_snap, dict) and after_snap.get("source") == "uia_com"):
                set_ui_snapshot_state(after_snap)
    else:
        after_snap = _LAST_UI_SNAPSHOT or {
            "ok": True,
            "snapshot_id": snapshot_id,
            "window_title": window_title,
            "count": len(_UI_REF_STORE),
            "elements": list(_UI_REF_STORE.values()),
        }

    diff = compute_ui_diff(before_snap, after_snap)
    res: dict[str, Any] = {
        "ok": True,
        "status": "completed",
        "snapshot_id": snapshot_id,
        "ref": norm_ref,
        "action": norm_action,
        "role": entry.get("role", ""),
        "name": entry.get("name", ""),
        "method": method,
        "x": target_cx,
        "y": target_cy,
        "active_window_title": (
            after_fg_title or str(after_snap.get("window_title") or window_title)
        ),
        "active_hwnd": after_fg_hwnd or snapshot_hwnd,
        "snapshot_refreshed": True,
        "diff": diff,
        "snapshot": after_snap,
    }
    if norm_action in {"set_value", "type"} and value is not None:
        res["char_count"] = len(value)
        res["interval_ms"] = clamped_interval
    return res


def inspect_ui_elements(
    app_name: str | None = None,
    max_elements: int = 60,
    *,
    powershell_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    user32: Any = None,
    kernel32: Any = None,
    window_enumerator: Callable[[], list[EnumeratedWindow]] | None = None,
    cdp_bridge: CDPBridge | None = None,
    uia_backend: NativeUiaComEngine | None = None,
) -> dict[str, Any]:
    """Inspect visible interactive and readout UI Automation elements in a window."""
    if app_name is not None and not isinstance(app_name, str):
        raise ValueError("app_name must be a string when provided.")
    if isinstance(max_elements, bool) or not isinstance(max_elements, int):
        raise ValueError("max_elements must be an integer.")
    clamped_max = max(1, min(MAX_UI_INSPECT_ELEMENTS, max_elements))

    cleaned_app = app_name.strip() if app_name is not None else None
    if cleaned_app == "":
        raise ValueError("app_name must not be empty when provided.")

    hwnd = _resolve_target_hwnd_optional(
        cleaned_app,
        powershell_runner=powershell_runner,
        user32=user32,
        kernel32=kernel32,
        window_enumerator=window_enumerator,
        focus_if_app_name=True,
    )

    proc_name, target_title, is_pwa_hint = _resolve_target_process_info(
        hwnd=hwnd,
        app_name=cleaned_app,
        user32=user32,
        kernel32=kernel32,
        window_enumerator=window_enumerator,
        powershell_runner=powershell_runner,
    )
    if is_chromium_process(proc_name) or is_chromium_app_name(cleaned_app):
        bridge = cdp_bridge or get_default_cdp_bridge()
        eff_title_hint = target_title or (
            None if is_chromium_app_name(cleaned_app) else cleaned_app
        )
        cdp_res = bridge.snapshot_sync(
            max_elements=min(clamped_max, 60),
            prefer_pwa=is_pwa_hint,
            title_hint=eff_title_hint,
            preferred_browser=(
                proc_name or (cleaned_app if is_chromium_app_name(cleaned_app) else None)
            ),
        )
        if isinstance(cdp_res, dict) and cdp_res.get("ok"):
            return {
                "ok": True,
                "source": "cdp",
                "window_title": str(cdp_res.get("window_title") or cdp_res.get("title") or ""),
                "count": int(cdp_res.get("count", 0)),
                "elements": [
                    {
                        "index": idx,
                        "name": str(el.get("name") or ""),
                        "automation_id": str(el.get("ref") or ""),
                        "control_type": str(el.get("role") or ""),
                        "value": el.get("value"),
                        "x": 0,
                        "y": 0,
                    }
                    for idx, el in enumerate(cdp_res.get("elements") or [])
                    if isinstance(el, dict)
                ],
            }
        if isinstance(cdp_res, dict) and cdp_res.get("cdp_available") is True:
            out = dict(cdp_res)
            if not out.get("window_title"):
                out["window_title"] = target_title
            out.setdefault("count", 0)
            out.setdefault("elements", [])
            return out
        return _chromium_cdp_unavailable_snapshot(
            window_title=target_title,
            process_name=proc_name or (cleaned_app or "chrome.exe"),
            error=str(cdp_res.get("error") or "") if isinstance(cdp_res, dict) else None,
        )

    com_engine = _resolve_uia_com_engine(
        uia_backend=uia_backend,
        powershell_runner=powershell_runner,
    )
    if com_engine is not None:
        try:
            com_res = com_engine.inspect_elements(
                hwnd=hwnd,
                app_name=cleaned_app,
                max_elements=clamped_max,
            )
            if not com_res.get("window_title") and target_title:
                com_res["window_title"] = target_title
            return com_res
        except UiaComUnavailableError as exc:
            logger.debug("COM UIA inspect unavailable, falling back to PowerShell: %s", exc)
        except Exception as exc:
            logger.debug("COM UIA inspect failed, falling back to PowerShell: %s", exc)

    ps_exe = _resolve_powershell_exe(powershell_runner)
    app_b64 = base64.b64encode(cleaned_app.encode("utf-8")).decode("ascii") if cleaned_app else ""
    ps_cmd = (
        f"& {{ {_POWERSHELL_INSPECT_UI_SCRIPT} }} "
        f"-TargetHwnd {hwnd} -MaxElements {clamped_max} -AppNameBase64 '{app_b64}'"
    )
    try:
        proc = run_powershell_hidden(
            ps_cmd,
            timeout=UI_AUTOMATION_TIMEOUT_SECONDS,
            ps_exe=ps_exe,
            runner=powershell_runner,
            check=False,
        )
    except subprocess.TimeoutExpired as exc:
        raise TimeoutError(
            f"UI Automation inspection timed out after {UI_AUTOMATION_TIMEOUT_SECONDS}s."
        ) from exc
    if proc.returncode != 0:
        err = (proc.stderr or proc.stdout or "").strip()
        raise RuntimeError(f"UI Automation inspection failed: {err}")

    raw = (proc.stdout or "").strip()
    if not raw:
        raise RuntimeError("UI Automation inspection returned empty output.")

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        raise RuntimeError(f"UI Automation inspection returned invalid JSON: {exc}") from exc
    window_title = str(data.get("window_title") or "")
    raw_elements = data.get("elements") or []
    if isinstance(raw_elements, dict):
        raw_elements = [raw_elements]

    elements: list[dict[str, Any]] = []
    for item in raw_elements:
        if not isinstance(item, dict):
            continue
        elements.append(
            {
                "index": len(elements),
                "name": str(item.get("name") or ""),
                "automation_id": str(item.get("automation_id") or ""),
                "control_type": str(item.get("control_type") or ""),
                "value": str(item["value"]) if item.get("value") is not None else None,
                "x": int(item.get("x", 0)),
                "y": int(item.get("y", 0)),
            }
        )
        if len(elements) >= clamped_max:
            break

    coverage, _, guidance = classify_accessibility_coverage(elements)
    ps_inspect_out: dict[str, Any] = {
        "ok": True,
        "window_title": window_title,
        "count": len(elements),
        "elements": elements,
        "accessibility_coverage": coverage,
    }
    if guidance:
        ps_inspect_out["hint"] = guidance
    return ps_inspect_out


def _select_matching_ui_element(
    elements: list[dict[str, Any]],
    name: str,
    control_type: str | None = None,
) -> dict[str, Any]:
    """Match an element by exact case-insensitive name, substring name, or exact automation_id."""
    ct_filter = control_type.strip().lower() if control_type else None
    candidates = [
        el
        for el in elements
        if not ct_filter or str(el.get("control_type") or "").strip().lower() == ct_filter
    ]
    target_lower = name.lower()
    for el in candidates:
        if str(el.get("name") or "").strip().lower() == target_lower:
            return el
    for el in candidates:
        if target_lower in str(el.get("name") or "").lower():
            return el
    for el in candidates:
        if str(el.get("automation_id") or "").strip().lower() == target_lower:
            return el

    avail = []
    for el in candidates:
        nm = str(el.get("name") or "").strip()
        if nm and nm not in avail:
            avail.append(nm)
        if len(avail) >= 25:
            break
    avail_str = ", ".join(avail) if avail else "none"
    ct_note = f" with control_type={control_type!r}" if control_type else ""
    raise ValueError(f"UI element {name!r}{ct_note} not found. Available elements: {avail_str}")


def click_ui_element(
    name: str,
    control_type: str | None = None,
    app_name: str | None = None,
    *,
    powershell_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    user32: Any = None,
    kernel32: Any = None,
    window_enumerator: Callable[[], list[EnumeratedWindow]] | None = None,
    focus_backend: FocusPreconditionBackend | None = None,
    expected_hwnd: int | None = None,
    require_foreground: bool = True,
    uia_backend: NativeUiaComEngine | None = None,
) -> dict[str, Any]:
    """Find and invoke or click a visible UI Automation control by name."""
    if not isinstance(name, str) or not name.strip():
        raise ValueError("name must be a non-empty string.")
    target_name = name.strip()

    cleaned_ct: str | None = None
    if control_type is not None:
        if not isinstance(control_type, str) or not control_type.strip():
            raise ValueError("control_type must be a non-empty string when provided.")
        cleaned_ct = control_type.strip()
        if not re.fullmatch(r"[A-Za-z][A-Za-z0-9]*", cleaned_ct):
            raise ValueError(f"Invalid UI Automation control_type: {control_type!r}")

    cleaned_app: str | None = None
    if app_name is not None:
        if not isinstance(app_name, str) or not app_name.strip():
            raise ValueError("app_name must not be empty when provided.")
        cleaned_app = app_name.strip()

    if expected_hwnd is not None and cleaned_app is None:
        hwnd = int(expected_hwnd)
    else:
        hwnd = _resolve_target_hwnd_optional(
            cleaned_app,
            powershell_runner=powershell_runner,
            user32=user32,
            kernel32=kernel32,
            window_enumerator=window_enumerator,
            focus_if_app_name=False,
        )

    should_guard = focus_backend is not None or (
        user32 is None
        and powershell_runner is None
        and window_enumerator is None
        and uia_backend is None
    )
    guard_hwnd = expected_hwnd if expected_hwnd is not None else (hwnd if hwnd > 0 else None)
    ps_target_hwnd = (
        int(expected_hwnd)
        if (expected_hwnd is not None and expected_hwnd > 0)
        else (hwnd if hwnd > 0 else 0)
    )

    def _run_click(
        locate_guard: PreconditionGuard | None,
        mouse_guard_fn: Callable[[], None] | None,
        target_hwnd: int = ps_target_hwnd,
    ) -> dict[str, Any]:
        if locate_guard is not None:
            locate_guard.assert_ready("click_ui_locate")

        proc_name, target_title, _ = _resolve_target_process_info(
            hwnd=target_hwnd,
            app_name=cleaned_app,
            user32=user32,
            kernel32=kernel32,
            window_enumerator=window_enumerator,
            powershell_runner=powershell_runner,
        )
        if is_chromium_process(proc_name) or is_chromium_app_name(cleaned_app):
            return _chromium_cdp_unavailable_snapshot(
                window_title=target_title,
                process_name=proc_name or (cleaned_app or "chrome.exe"),
                error=(
                    "Blocked PowerShell UIA click on Chromium window. Use ui_snapshot() + "
                    "ui_act('b1', 'click') or open_url(...) instead."
                ),
            )

        com_engine = _resolve_uia_com_engine(
            uia_backend=uia_backend,
            powershell_runner=powershell_runner,
        )
        if com_engine is not None:
            com_data: dict[str, Any] | None = None
            try:
                com_data = com_engine.click_element(
                    target_name,
                    control_type=cleaned_ct,
                    hwnd=target_hwnd,
                    app_name=cleaned_app,
                )
            except UiaComUnavailableError as exc:
                logger.debug("COM UIA click unavailable, falling back to PowerShell: %s", exc)
            except Exception as exc:
                logger.debug("COM UIA click failed, falling back to PowerShell: %s", exc)

            if com_data is not None:
                if not com_data.get("found"):
                    avail_raw = com_data.get("available") or []
                    avail = [str(item) for item in avail_raw if item]
                    ct_note = f" (control_type={cleaned_ct!r})" if cleaned_ct else ""
                    avail_str = ", ".join(avail) if avail else "none"
                    raise ValueError(
                        f"UI element {target_name!r}{ct_note} not found. "
                        f"Available elements: {avail_str}"
                    )
                cx = int(com_data.get("x", 0))
                cy = int(com_data.get("y", 0))
                invoked = bool(com_data.get("invoked", False))
                if not invoked:
                    if mouse_guard_fn is not None:
                        mouse_guard_fn()
                    _perform_mouse_click(cx, cy, clicks=1, button="left", user32=user32)
                return {
                    "ok": True,
                    "status": "clicked",
                    "name": str(com_data.get("name") or target_name),
                    "control_type": str(com_data.get("control_type") or cleaned_ct or ""),
                    "x": cx,
                    "y": cy,
                    "method": str(com_data.get("method") or "invoke") if invoked else "click",
                }

        ps_exe = _resolve_powershell_exe(powershell_runner)
        name_b64 = base64.b64encode(target_name.encode("utf-8")).decode("ascii")
        app_b64 = (
            base64.b64encode(cleaned_app.encode("utf-8")).decode("ascii") if cleaned_app else ""
        )
        ct_arg = cleaned_ct or ""
        ps_cmd = (
            f"& {{ {_POWERSHELL_CLICK_UI_SCRIPT} }} "
            f"-TargetHwnd {target_hwnd} -TargetNameBase64 '{name_b64}' "
            f"-ControlTypeFilter '{ct_arg}' -AppNameBase64 '{app_b64}'"
        )
        try:
            proc = run_powershell_hidden(
                ps_cmd,
                timeout=UI_AUTOMATION_TIMEOUT_SECONDS,
                ps_exe=ps_exe,
                runner=powershell_runner,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise TimeoutError(
                f"UI Automation click timed out after {UI_AUTOMATION_TIMEOUT_SECONDS}s."
            ) from exc
        if proc.returncode != 0:
            err = (proc.stderr or proc.stdout or "").strip()
            raise RuntimeError(f"UI Automation click failed: {err}")

        raw = (proc.stdout or "").strip()
        if not raw:
            raise RuntimeError("UI Automation click returned empty output.")

        try:
            data = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise RuntimeError(f"UI Automation click returned invalid JSON: {exc}") from exc

        if "elements" in data and "found" not in data:
            raw_elements = data.get("elements") or []
            if isinstance(raw_elements, dict):
                raw_elements = [raw_elements]
            chosen = _select_matching_ui_element(raw_elements, target_name, cleaned_ct)
            cx = int(chosen.get("x", 0))
            cy = int(chosen.get("y", 0))
            invoked = bool(chosen.get("supports_invoke", False))
            if not invoked:
                if mouse_guard_fn is not None:
                    mouse_guard_fn()
                _perform_mouse_click(cx, cy, clicks=1, button="left", user32=user32)
            return {
                "ok": True,
                "status": "clicked",
                "name": str(chosen.get("name") or target_name),
                "control_type": str(chosen.get("control_type") or cleaned_ct or ""),
                "x": cx,
                "y": cy,
                "method": "invoke" if invoked else "click",
            }

        if not data.get("found"):
            avail_raw = data.get("available") or []
            if isinstance(avail_raw, str):
                avail = [avail_raw]
            else:
                avail = [str(item) for item in avail_raw if item]
            ct_note = f" (control_type={cleaned_ct!r})" if cleaned_ct else ""
            avail_str = ", ".join(avail) if avail else "none"
            raise ValueError(
                f"UI element {target_name!r}{ct_note} not found. Available elements: {avail_str}"
            )

        cx = int(data.get("x", 0))
        cy = int(data.get("y", 0))
        invoked = bool(data.get("invoked", False))
        if not invoked:
            if mouse_guard_fn is not None:
                mouse_guard_fn()
            _perform_mouse_click(cx, cy, clicks=1, button="left", user32=user32)

        return {
            "ok": True,
            "status": "clicked",
            "name": str(data.get("name") or target_name),
            "control_type": str(data.get("control_type") or cleaned_ct or ""),
            "x": cx,
            "y": cy,
            "method": "invoke" if invoked else "click",
        }

    if should_guard:
        backend = focus_backend or Win32FocusPreconditionBackend()
        try:
            with PreconditionGuard(
                backend,
                expected_hwnd=guard_hwnd,
                require_foreground=False,
            ) as locate_guard:
                fallback_hwnd = (
                    locate_guard.target.hwnd
                    if locate_guard.target is not None
                    else guard_hwnd
                )
                resolved_target_hwnd = (
                    ps_target_hwnd
                    if ps_target_hwnd > 0
                    else (fallback_hwnd if fallback_hwnd is not None and fallback_hwnd > 0 else 0)
                )

                def _mouse_guard() -> None:
                    locate_guard.assert_ready("click_ui_mouse")
                    if require_foreground:
                        with PreconditionGuard(
                            backend,
                            expected_hwnd=fallback_hwnd,
                            require_foreground=True,
                        ) as fg_guard:
                            fg_guard.assert_ready("click_ui_mouse")

                return _run_click(locate_guard, _mouse_guard, target_hwnd=resolved_target_hwnd)
        except PreconditionFailedError as exc:
            abort = exc.to_abort_payload()
            abort["name"] = target_name
            abort["control_type"] = cleaned_ct or ""
            return abort
    return _run_click(None, None)


def _normalize_key_name(raw_key: str) -> str:
    if not isinstance(raw_key, str):
        raise ValueError(f"Key must be a string, got {type(raw_key).__name__}.")
    cleaned = raw_key.strip().lower()
    if not cleaned:
        raise ValueError("Key name must not be empty.")
    canonical = _KEY_ALIASES.get(cleaned, cleaned)
    if canonical not in _ALLOWED_VK_MAP:
        raise ValueError(f"Unsupported or disallowed key: {raw_key!r}")
    return canonical


def gui_action(
    action: str,
    x: int | float | None = None,
    y: int | float | None = None,
    text: str | None = None,
    keys: list[str] | None = None,
    clicks: int = 1,
    button: str = "left",
    delta: int = 0,
    seconds: float = 1.0,
    interval_ms: int = 12,
    *,
    coord_space: str | None = None,
    image_width: int | None = None,
    image_height: int | None = None,
    screen_width: int | None = None,
    screen_height: int | None = None,
    virtual_left: int | None = None,
    virtual_top: int | None = None,
    dpi_scale: float | None = None,
    capture_id: str | None = None,
    capture_bounds: tuple[int, int, int, int] | list[int] | None = None,
    window_bounds_fn: Callable[[int], tuple[int, int, int, int] | None] | None = None,
    ref: str | None = None,
    key: str | None = None,
    cdp_bridge: CDPBridge | None = None,
    app_name: str | None = None,
    user32: Any = None,
    kernel32: Any = None,
    sleep_fn: Callable[[float], None] | None = None,
    focus_backend: FocusPreconditionBackend | None = None,
    expected_hwnd: int | None = None,
    require_foreground: bool = True,
    _guard: PreconditionGuard | None = None,
    cancel_event: threading.Event | None = None,
    clipboard_backend: Any | None = None,
) -> dict[str, Any]:
    """Perform a safety-bounded GUI input, CDP browser ref/history, or wait action."""
    if not isinstance(action, str):
        raise ValueError("action must be a string.")
    normalized_action = action.strip().lower()
    if normalized_action not in VALID_GUI_ACTIONS:
        raise ValueError(
            f"Unsupported GUI action {action!r}. Must be one of {sorted(VALID_GUI_ACTIONS)}."
        )

    # Browser history navigation via CDP
    if normalized_action in {"navigate_back", "navigate_forward"}:
        bridge = cdp_bridge or get_default_cdp_bridge()
        direction = "back" if normalized_action == "navigate_back" else "forward"
        explicit_chromium = is_chromium_app_name(app_name)
        pref_browser = app_name if explicit_chromium else None
        pref_pwa = False if explicit_chromium else None
        res = bridge.navigate_history_sync(
            direction,
            prefer_pwa=pref_pwa,
            preferred_browser=pref_browser,
        )
        res.setdefault("action", normalized_action)
        return res

    # Direct CDP element ref execution (b1..bN) via gui_action
    if ref is not None:
        if not is_cdp_ref(ref):
            raise ValueError(
                f"gui_action ref must be a browser CDP ref like 'b1', got {ref!r}."
            )
        bridge = cdp_bridge or get_default_cdp_bridge()
        cdp_act_map = {
            "click": "click",
            "click_at": "click",
            "double_click": "double_click",
            "right_click": "right_click",
            "type": "type",
            "type_text": "type",
            "press": "press",
            "press_keys": "press",
        }
        cdp_action = cdp_act_map.get(normalized_action)
        if cdp_action is None:
            raise ValueError(
                f"Action {action!r} is not supported with browser ref {ref!r}."
            )
        eff_key = key or (keys[0] if isinstance(keys, list) and keys else None)
        res = bridge.act_sync(ref, cdp_action, text=text, key=eff_key)
        res.setdefault("action", normalized_action)
        return res

    if normalized_action in {"click", "type", "press"}:
        raise ValueError(
            f"Action {normalized_action!r} requires a browser CDP ref (e.g. ref='b1')."
        )

    if normalized_action == "wait":
        if isinstance(seconds, bool) or not isinstance(seconds, (int, float)):
            raise ValueError("seconds must be a number for 'wait' action.")
        if not math.isfinite(float(seconds)):
            raise ValueError("seconds must be finite.")
        clamped_seconds = max(0.1, min(10.0, float(seconds)))
        sleeper = sleep_fn or time.sleep
        sleeper(clamped_seconds)
        return {
            "ok": True,
            "status": "completed",
            "action": "wait",
            "seconds": clamped_seconds,
        }

    # Validate arguments before checking focus/window preconditions
    btn = button.strip().lower() if isinstance(button, str) else ""
    pointer_actions = {"click_at", "double_click", "right_click", "move"}

    cached_geom = get_last_screen_geometry()
    active_geom: dict[str, Any] | None = None
    if _is_valid_recent_screen_geometry(cached_geom):
        assert cached_geom is not None
        if capture_id is None or cached_geom.get("capture_id") in (None, capture_id):
            active_geom = cached_geom

    explicit_capture_bounds: tuple[int, int, int, int] | None = None
    if capture_bounds is not None and len(capture_bounds) == 4:
        cb = tuple(int(v) for v in capture_bounds)
        if cb[2] > cb[0] and cb[3] > cb[1]:
            explicit_capture_bounds = cb

    norm_capture_bounds: tuple[int, int, int, int] | None = explicit_capture_bounds
    if norm_capture_bounds is None and active_geom is not None:
        raw_cb = active_geom.get("capture_bounds") or active_geom.get("window_bounds")
        if isinstance(raw_cb, (tuple, list)) and len(raw_cb) == 4:
            cb = tuple(int(v) for v in raw_cb)
            if cb[2] > cb[0] and cb[3] > cb[1]:
                norm_capture_bounds = cb

    has_passed_capture_geom = (
        explicit_capture_bounds is not None
        or (screen_width is not None and screen_height is not None)
    )
    if coord_space is None:
        if normalized_action in pointer_actions and (
            has_passed_capture_geom or active_geom is not None
        ):
            inherited_cs = (
                str(active_geom.get("coord_space") or "normalized_1000").strip().lower()
                if active_geom is not None
                else "normalized_1000"
            )
            effective_coord_space = (
                inherited_cs if inherited_cs in VALID_COORD_SPACES else "normalized_1000"
            )
        else:
            effective_coord_space = "screen"
    else:
        effective_coord_space = coord_space.strip().lower()

    if effective_coord_space not in VALID_COORD_SPACES:
        raise ValueError(
            f"Unsupported coord_space {coord_space!r}. "
            f"Must be one of {sorted(VALID_COORD_SPACES)}."
        )

    eff_virtual_left = virtual_left
    eff_virtual_top = virtual_top
    eff_screen_width = screen_width
    eff_screen_height = screen_height
    eff_image_width = image_width
    eff_image_height = image_height
    eff_dpi_scale = dpi_scale

    if norm_capture_bounds is not None and eff_screen_width is None and eff_screen_height is None:
        if eff_virtual_left is None:
            eff_virtual_left = norm_capture_bounds[0]
        if eff_virtual_top is None:
            eff_virtual_top = norm_capture_bounds[1]
        eff_screen_width = norm_capture_bounds[2] - norm_capture_bounds[0]
        eff_screen_height = norm_capture_bounds[3] - norm_capture_bounds[1]

    if active_geom is not None:
        if eff_virtual_left is None:
            eff_virtual_left = int(active_geom["virtual_left"])
        if eff_virtual_top is None:
            eff_virtual_top = int(active_geom["virtual_top"])
        if eff_screen_width is None:
            eff_screen_width = int(active_geom["screen_width"])
        if eff_screen_height is None:
            eff_screen_height = int(active_geom["screen_height"])
        if eff_image_width is None:
            eff_image_width = int(active_geom["image_width"])
        if eff_image_height is None:
            eff_image_height = int(active_geom["image_height"])
        if eff_dpi_scale is None:
            eff_dpi_scale = float(active_geom["dpi_scale"])

    if normalized_action in pointer_actions:
        if x is None or y is None or isinstance(x, bool) or isinstance(y, bool):
            raise ValueError(f"x and y coordinates are required for '{normalized_action}'.")
        if effective_coord_space == "screen":
            if not isinstance(x, int) or not isinstance(y, int) or x < 0 or y < 0:
                raise ValueError(
                    f"x and y must be non-negative integers for '{normalized_action}'."
                )
        else:
            if (
                not isinstance(x, (int, float))
                or not isinstance(y, (int, float))
                or not math.isfinite(float(x))
                or not math.isfinite(float(y))
                or x < 0
                or y < 0
            ):
                raise ValueError(
                    f"x and y must be non-negative finite numbers for '{normalized_action}'."
                )
        if normalized_action == "click_at":
            if isinstance(clicks, bool) or not isinstance(clicks, int) or clicks < 1 or clicks > 3:
                raise ValueError("clicks must be an integer between 1 and 3.")
            if btn not in VALID_CLICK_BUTTONS:
                raise ValueError(
                    f"Unsupported button {button!r}. Must be one of {sorted(VALID_CLICK_BUTTONS)}."
                )
    elif normalized_action == "type_text":
        if not isinstance(text, str) or len(text) == 0:
            raise ValueError("text must be a non-empty string for 'type_text'.")
        if len(text) > MAX_GUI_TYPE_TEXT_LEN:
            raise ValueError(
                f"text length ({len(text)}) exceeds maximum allowed ({MAX_GUI_TYPE_TEXT_LEN})."
            )
    elif normalized_action == "scroll":
        if isinstance(delta, bool) or not isinstance(delta, int) or delta == 0:
            raise ValueError("delta must be a non-zero integer for 'scroll' action.")
        if (x is None) != (y is None):
            raise ValueError("Both x and y must be provided together for 'scroll'.")
        if x is not None and y is not None:
            if (
                isinstance(x, bool)
                or isinstance(y, bool)
                or not isinstance(x, (int, float))
                or not isinstance(y, (int, float))
            ):
                raise ValueError("x and y must be numeric for 'scroll'.")
            if effective_coord_space == "screen" and (
                not isinstance(x, int) or not isinstance(y, int)
            ):
                raise ValueError("x and y must be integers for 'scroll'.")
            if x < 0 or y < 0:
                raise ValueError("x and y must be non-negative integers for 'scroll'.")

    def _resolve_physical_xy(u32: Any) -> tuple[int, int, dict[str, Any]]:
        assert x is not None and y is not None
        return convert_coordinates_to_screen(
            x,
            y,
            coord_space=effective_coord_space,
            screen_width=eff_screen_width,
            screen_height=eff_screen_height,
            virtual_left=eff_virtual_left,
            virtual_top=eff_virtual_top,
            image_width=eff_image_width,
            image_height=eff_image_height,
            dpi_scale=eff_dpi_scale,
            user32=u32,
        )

    def _read_actual_hwnd(u32: Any, active_guard: PreconditionGuard | None) -> int | None:
        guard_backend = getattr(active_guard, "backend", None) or getattr(
            active_guard, "_backend", None
        )
        for backend_obj in (guard_backend, focus_backend):
            if backend_obj is not None and hasattr(backend_obj, "get_foreground_context"):
                with contextlib.suppress(Exception):
                    fg_ctx = backend_obj.get_foreground_context()
                    if fg_ctx is not None and isinstance(fg_ctx.hwnd, int) and fg_ctx.hwnd > 0:
                        return int(fg_ctx.hwnd)
        if u32 is not None and hasattr(u32, "GetForegroundWindow"):
            with contextlib.suppress(Exception):
                raw_fg = u32.GetForegroundWindow()
                if isinstance(raw_fg, int) and not isinstance(raw_fg, bool) and raw_fg > 0:
                    return int(raw_fg)
        return None

    active_geom_hwnd: int | None = None
    if active_geom is not None:
        raw_geom_hwnd = active_geom.get("hwnd") or active_geom.get("target_hwnd")
        if (
            isinstance(raw_geom_hwnd, int)
            and not isinstance(raw_geom_hwnd, bool)
            and raw_geom_hwnd > 0
        ):
            active_geom_hwnd = raw_geom_hwnd

    def _execute_gui_action(
        active_guard: PreconditionGuard | None,
        eff_expected_hwnd: int | None,
    ) -> dict[str, Any]:
        _check_cancelled(cancel_event)
        if active_guard is not None:
            active_guard.assert_ready(f"gui_action:{normalized_action}")

        u32 = _get_win32_user32(user32)
        audit_expected_hwnd = (
            eff_expected_hwnd if eff_expected_hwnd is not None else active_geom_hwnd
        )
        actual_fg_hwnd = _read_actual_hwnd(u32, active_guard)

        if normalized_action in pointer_actions:
            stale_check_bounds = (
                explicit_capture_bounds
                if explicit_capture_bounds is not None
                else (norm_capture_bounds if effective_coord_space != "screen" else None)
            )
            if audit_expected_hwnd is not None and stale_check_bounds is not None:
                live_bounds = _query_live_window_bounds(
                    audit_expected_hwnd,
                    window_bounds_fn=window_bounds_fn,
                    active_guard=active_guard,
                    focus_backend=focus_backend,
                    user32=user32,
                )
                if live_bounds is not None and any(
                    abs(int(live_bounds[i]) - int(stale_check_bounds[i]))
                    > _STALE_GEOMETRY_TOLERANCE_PX
                    for i in range(4)
                ):
                    return {
                        "ok": False,
                        "status": "aborted",
                        "reason": "stale_screen_geometry",
                        "action": normalized_action,
                        "goal_verified": False,
                        "verification_scope": "dispatch_only",
                        "coord_space": effective_coord_space,
                        "expected_hwnd": audit_expected_hwnd,
                        "actual_hwnd": actual_fg_hwnd,
                        "expected_bounds": list(stale_check_bounds),
                        "actual_bounds": list(live_bounds),
                        "error": (
                            f"Target window (hwnd={audit_expected_hwnd}) moved or resized since "
                            f"screen capture: captured {stale_check_bounds}, live {live_bounds}. "
                            "Recapture screen before clicking."
                        ),
                    }

            with _OS_INPUT_LOCK:
                _check_cancelled(cancel_event)
                px, py, coord_meta = _resolve_physical_xy(u32)
                if normalized_action == "move":
                    if hasattr(u32, "SetCursorPos"):
                        moved = u32.SetCursorPos(px, py)
                        if moved is not None and not moved:
                            raise RuntimeError(f"Win32 SetCursorPos({px}, {py}) failed.")
                    return {
                        "ok": True,
                        "status": "completed",
                        "action": "move",
                        "x": px,
                        "y": py,
                        "expected_hwnd": audit_expected_hwnd,
                        "actual_hwnd": actual_fg_hwnd,
                        **coord_meta,
                    }

                if active_guard is not None:
                    active_guard.assert_point_targets_window(
                        px, py, step_label=f"gui_action:{normalized_action}"
                    )

                eff_btn = (
                    "right"
                    if normalized_action == "right_click"
                    else ("left" if normalized_action == "double_click" else btn)
                )
                eff_clicks = (
                    2
                    if normalized_action == "double_click"
                    else (1 if normalized_action == "right_click" else clicks)
                )
                _perform_mouse_click(px, py, clicks=eff_clicks, button=eff_btn, user32=u32)
            actual_clicks = 2 if eff_btn == "double" and eff_clicks == 1 else eff_clicks
            return {
                "ok": True,
                "status": "completed",
                "action": normalized_action,
                "x": px,
                "y": py,
                "clicks": actual_clicks,
                "button": eff_btn,
                "expected_hwnd": audit_expected_hwnd,
                "actual_hwnd": actual_fg_hwnd,
                **coord_meta,
            }

        if normalized_action == "type_text":
            assert text is not None
            entry_res = enter_text_tiered(
                text,
                u32=u32,
                interval_ms=interval_ms,
                sleep_fn=sleep_fn,
                guard=active_guard,
                clipboard_backend=clipboard_backend,
                cancel_event=cancel_event,
                step_label="gui_action_type_text",
            )
            res_type: dict[str, Any] = {
                "ok": True,
                "status": "completed",
                "action": "type_text",
                "char_count": entry_res["char_count"],
                "interval_ms": entry_res["interval_ms"],
                "entry_method": entry_res["method"],
                "expected_hwnd": audit_expected_hwnd,
                "actual_hwnd": actual_fg_hwnd,
            }
            if "clipboard_restored" in entry_res:
                res_type["clipboard_restored"] = entry_res["clipboard_restored"]
            return res_type

        if normalized_action == "press_keys":
            with _OS_INPUT_LOCK:
                _check_cancelled(cancel_event)
                _release_stuck_modifiers(u32)
                normalized_keys = _send_key_chord(u32, keys or [])
            return {
                "ok": True,
                "status": "completed",
                "action": "press_keys",
                "keys": normalized_keys,
                "expected_hwnd": audit_expected_hwnd,
                "actual_hwnd": actual_fg_hwnd,
            }

        # normalized_action == "scroll"
        scroll_x: int | None = None
        scroll_y: int | None = None
        coord_meta_scroll: dict[str, Any] = {}
        clamped_delta = max(-12000, min(12000, delta))
        with _OS_INPUT_LOCK:
            _check_cancelled(cancel_event)
            if x is not None and y is not None:
                scroll_x, scroll_y, coord_meta_scroll = _resolve_physical_xy(u32)
                if active_guard is not None:
                    active_guard.assert_point_targets_window(
                        scroll_x, scroll_y, step_label="gui_action:scroll"
                    )
                if hasattr(u32, "SetCursorPos"):
                    moved = u32.SetCursorPos(scroll_x, scroll_y)
                    if moved is not None and not moved:
                        raise RuntimeError(f"Win32 SetCursorPos({scroll_x}, {scroll_y}) failed.")

            scroll_input = _INPUT(
                type=INPUT_MOUSE,
                union=_INPUT_UNION(
                    mi=_MOUSEINPUT(
                        mouseData=clamped_delta & 0xFFFFFFFF,
                        dwFlags=MOUSEEVENTF_WHEEL,
                    )
                ),
            )
            if hasattr(u32, "SendInput"):
                _send_win32_inputs(u32, [scroll_input])
            elif hasattr(u32, "mouse_event"):
                u32.mouse_event(MOUSEEVENTF_WHEEL, 0, 0, clamped_delta & 0xFFFFFFFF, 0)
            else:
                raise RuntimeError("Win32 SendInput/mouse_event is unavailable.")

        return {
            "ok": True,
            "status": "completed",
            "action": "scroll",
            "delta": clamped_delta,
            "x": scroll_x,
            "y": scroll_y,
            "expected_hwnd": audit_expected_hwnd,
            "actual_hwnd": actual_fg_hwnd,
            **coord_meta_scroll,
        }

    if _guard is not None:
        try:
            return _execute_gui_action(_guard, expected_hwnd)
        except ActionCancelledError as exc:
            return {
                "ok": False,
                "status": "cancelled",
                "cancelled": True,
                "error": str(exc),
                "action": normalized_action,
            }

    should_guard = focus_backend is not None or user32 is None
    resolved_expected_hwnd = expected_hwnd
    if resolved_expected_hwnd is None and isinstance(app_name, str) and app_name.strip():
        target_app_query = app_name.strip()
        try:
            resolved_expected_hwnd = _resolve_target_hwnd_optional(
                target_app_query,
                user32=user32,
                kernel32=kernel32,
                focus_if_app_name=False,
            )
        except ValueError as exc:
            if should_guard:
                abort = PreconditionFailedError(
                    reason="no_target_window",
                    step_label=f"gui_action:{normalized_action}",
                    expected_title=target_app_query,
                    message=str(exc),
                ).to_abort_payload()
                abort["action"] = normalized_action
                return abort
            raise

    if should_guard:
        backend = focus_backend or Win32FocusPreconditionBackend()
        try:
            with PreconditionGuard(
                backend,
                expected_hwnd=resolved_expected_hwnd,
                require_foreground=require_foreground,
            ) as guard:
                return _execute_gui_action(guard, resolved_expected_hwnd)
        except PreconditionFailedError as exc:
            abort = exc.to_abort_payload()
            abort["action"] = normalized_action
            return abort
        except ActionCancelledError as exc:
            return {
                "ok": False,
                "status": "cancelled",
                "cancelled": True,
                "error": str(exc),
                "action": normalized_action,
            }

    try:
        return _execute_gui_action(None, resolved_expected_hwnd)
    except ActionCancelledError as exc:
        return {
            "ok": False,
            "status": "cancelled",
            "cancelled": True,
            "error": str(exc),
            "action": normalized_action,
        }


# ---------------------------------------------------------------------------
# State Diff & Speculative Batching


def compute_ui_diff(before: Any, after: Any) -> dict[str, Any]:
    """Compute structural UI diff between two snapshots generically across apps and browsers."""
    if hasattr(before, "model_dump"):
        before_dict = before.model_dump()
    elif isinstance(before, dict):
        before_dict = before
    else:
        before_dict = {}

    if hasattr(after, "model_dump"):
        after_dict = after.model_dump()
    elif isinstance(after, dict):
        after_dict = after
    else:
        after_dict = {}

    before_title = str(before_dict.get("window_title") or "")
    after_title = str(after_dict.get("window_title") or "")
    window_changed = before_title != after_title

    def _normalize_el(item: Any) -> dict[str, Any]:
        if hasattr(item, "model_dump"):
            return item.model_dump()
        return dict(item) if isinstance(item, dict) else {}

    before_els = [_normalize_el(x) for x in (before_dict.get("elements") or [])]
    after_els = [_normalize_el(x) for x in (after_dict.get("elements") or [])]

    matched_before: set[int] = set()
    matched_after: set[int] = set()
    pairs: list[tuple[int, int]] = []
    changed_values: list[dict[str, Any]] = []

    # 1. Match by automation_id when non-empty in both
    for j, a in enumerate(after_els):
        aid_a = str(a.get("automation_id") or "").strip()
        if not aid_a:
            continue
        role_a = str(a.get("role") or a.get("control_type") or "").strip().lower()
        for i, b in enumerate(before_els):
            if i in matched_before:
                continue
            aid_b = str(b.get("automation_id") or "").strip()
            role_b = str(b.get("role") or b.get("control_type") or "").strip().lower()
            if aid_a == aid_b and (not role_a or not role_b or role_a == role_b):
                matched_before.add(i)
                matched_after.add(j)
                pairs.append((i, j))
                break

    # 2. Match by (role, name) when name is non-empty (favoring closest spatial coordinates)
    for j, a in enumerate(after_els):
        if j in matched_after:
            continue
        name_a = str(a.get("name") or "").strip().lower()
        role_a = str(a.get("role") or a.get("control_type") or "").strip().lower()
        if not name_a:
            continue
        xa = int(a.get("x", 0))
        ya = int(a.get("y", 0))

        best_i: int | None = None
        best_dist = float("inf")
        for i, b in enumerate(before_els):
            if i in matched_before:
                continue
            name_b = str(b.get("name") or "").strip().lower()
            role_b = str(b.get("role") or b.get("control_type") or "").strip().lower()
            if name_a == name_b and (not role_a or not role_b or role_a == role_b):
                xb = int(b.get("x", 0))
                yb = int(b.get("y", 0))
                dist = abs(xa - xb) + abs(ya - yb)
                if dist < best_dist:
                    best_dist = dist
                    best_i = i

        if best_i is not None:
            matched_before.add(best_i)
            matched_after.add(j)
            pairs.append((best_i, j))

    # 3. Match by (role, position) within 15px (e.g. dynamic text readouts or edited fields)
    for j, a in enumerate(after_els):
        if j in matched_after:
            continue
        role_a = str(a.get("role") or a.get("control_type") or "").strip().lower()
        xa = int(a.get("x", 0))
        ya = int(a.get("y", 0))
        has_pos_a = xa > 0 or ya > 0
        if not role_a or not has_pos_a:
            continue
        for i, b in enumerate(before_els):
            if i in matched_before:
                continue
            role_b = str(b.get("role") or b.get("control_type") or "").strip().lower()
            xb = int(b.get("x", 0))
            yb = int(b.get("y", 0))
            has_pos_b = xb > 0 or yb > 0
            if (
                role_b
                and role_a == role_b
                and has_pos_b
                and abs(xa - xb) <= 15
                and abs(ya - yb) <= 15
            ):
                matched_before.add(i)
                matched_after.add(j)
                pairs.append((i, j))
                break

    # Check for value / readout changes among matched pairs
    for i, j in pairs:
        b = before_els[i]
        a = after_els[j]
        b_val = b.get("value")
        a_val = a.get("value")
        b_name = str(b.get("name") or "")
        a_name = str(a.get("name") or "")
        role = str(a.get("role") or b.get("role") or "")
        role_lower = role.lower()

        b_val_clean = str(b_val) if b_val is not None else None
        a_val_clean = str(a_val) if a_val is not None else None

        if b_val_clean != a_val_clean:
            changed_values.append(
                {
                    "ref": str(a.get("ref") or b.get("ref") or ""),
                    "role": role,
                    "name": a_name or b_name,
                    "old_value": b_val_clean,
                    "new_value": a_val_clean,
                }
            )
        elif b_name != a_name and (
            role_lower in {"text", "readout", "label", "status_bar", "title_bar"}
            or (b_val is None and a_val is None)
        ):
            changed_values.append(
                {
                    "ref": str(a.get("ref") or b.get("ref") or ""),
                    "role": role,
                    "name": a_name or b_name,
                    "old_value": b_name,
                    "new_value": a_name,
                }
            )

    added_elements: list[dict[str, Any]] = []
    for j, a in enumerate(after_els):
        if j not in matched_after:
            added_elements.append(
                {
                    "ref": str(a.get("ref") or f"e{j}"),
                    "role": str(a.get("role") or a.get("control_type") or "").strip().lower(),
                    "name": str(a.get("name") or ""),
                    "value": str(a["value"]) if a.get("value") is not None else None,
                    "x": int(a.get("x", 0)),
                    "y": int(a.get("y", 0)),
                    "automation_id": str(a.get("automation_id") or ""),
                    "control_type": str(a.get("control_type") or a.get("role") or ""),
                    "enabled": bool(a.get("enabled", True)),
                    "focused": bool(a.get("focused", False)),
                }
            )
    added_elements = added_elements[:25]
    removed_count = max(0, len(before_els) - len(matched_before))

    return {
        "window_changed": window_changed,
        "window_title": after_title,
        "changed_values": changed_values,
        "added_elements": added_elements,
        "removed_count": removed_count,
    }


def ui_batch(
    steps: list[dict[str, Any]],
    stop_on_error: bool = True,
    stop_on_window_change: bool = True,
    *,
    ui_snapshot_fn: Callable[..., dict[str, Any]] | None = None,
    gui_action_fn: Callable[..., dict[str, Any]] | None = None,
    window_title_fn: Callable[[], str] | None = None,
    user32: Any = None,
    focus_backend: FocusPreconditionBackend | None = None,
    expected_hwnd: int | None = None,
    require_foreground: bool = True,
    cdp_bridge: CDPBridge | None = None,
    cancel_event: threading.Event | None = None,
) -> dict[str, Any]:
    """Execute speculative multi-step GUI actions in a single round-trip with state diffing."""
    if not isinstance(steps, list) or len(steps) < 1 or len(steps) > 20:
        raise ValueError("steps must be a list containing between 1 and 20 step operations.")

    # Validate step shapes
    for idx, s in enumerate(steps):
        if hasattr(s, "model_dump"):
            s_dict = s.model_dump(exclude_unset=True)
        elif isinstance(s, dict):
            s_dict = s
        else:
            raise ValueError(f"Step {idx} must be a dictionary.")
        op = str(s_dict.get("op") or "").strip().lower()
        if op not in VALID_UI_BATCH_OPS:
            valid_ops = sorted(VALID_UI_BATCH_OPS)
            raise ValueError(
                f"Unsupported batch op {op!r} in step {idx}. Must be one of {valid_ops}."
            )
        if op == "act":
            if not s_dict.get("ref"):
                raise ValueError(f"Step {idx} 'act' requires 'ref'.")
            act = str(s_dict.get("action") or "").strip().lower()
            if act not in VALID_UI_ACT_ACTIONS:
                valid_acts = sorted(VALID_UI_ACT_ACTIONS)
                raise ValueError(
                    f"Unsupported action {act!r} in step {idx} 'act'. Must be one of {valid_acts}."
                )
            if act == "type":
                val = s_dict.get("value")
                if val is None or not isinstance(val, str):
                    raise ValueError(
                        f"Step {idx} 'act' with action 'type' requires string 'value'."
                    )
        elif op == "press_keys":
            keys = s_dict.get("keys")
            if not isinstance(keys, list) or len(keys) == 0:
                raise ValueError(f"Step {idx} 'press_keys' requires non-empty list 'keys'.")
        elif op == "scroll":
            delta = s_dict.get("delta")
            if isinstance(delta, bool) or not isinstance(delta, int) or delta == 0:
                raise ValueError(f"Step {idx} 'scroll' requires non-zero integer 'delta'.")
        elif op == "wait":
            sec = s_dict.get("seconds", 0.5)
            if (
                isinstance(sec, bool)
                or not isinstance(sec, (int, float))
                or not math.isfinite(float(sec))
                or float(sec) < 0
            ):
                raise ValueError(
                    f"Step {idx} 'wait' requires non-negative finite number for 'seconds'."
                )
        elif op == "click_at":
            raw_cs = s_dict.get("coord_space")
            eff_space = "screen"
            if raw_cs is not None:
                if not isinstance(raw_cs, str) or not raw_cs.strip():
                    raise ValueError(
                        f"Step {idx} 'click_at' coord_space must be one of "
                        f"{sorted(VALID_COORD_SPACES)}."
                    )
                eff_space = raw_cs.strip().lower()
                if eff_space not in VALID_COORD_SPACES:
                    raise ValueError(
                        f"Step {idx} 'click_at' unsupported coord_space {raw_cs!r}. "
                        f"Must be one of {sorted(VALID_COORD_SPACES)}."
                    )
            x = s_dict.get("x")
            y = s_dict.get("y")
            if eff_space == "screen":
                if (
                    x is None
                    or y is None
                    or isinstance(x, bool)
                    or isinstance(y, bool)
                    or not isinstance(x, int)
                    or not isinstance(y, int)
                    or x < 0
                    or y < 0
                ):
                    raise ValueError(
                        f"Step {idx} 'click_at' requires non-negative integer 'x' and 'y'."
                    )
            else:
                if (
                    x is None
                    or y is None
                    or isinstance(x, bool)
                    or isinstance(y, bool)
                    or not isinstance(x, (int, float))
                    or not isinstance(y, (int, float))
                    or not math.isfinite(float(x))
                    or not math.isfinite(float(y))
                    or x < 0
                    or y < 0
                ):
                    raise ValueError(
                        f"Step {idx} 'click_at' requires non-negative numeric 'x' and 'y' "
                        f"for coord_space={eff_space!r}."
                    )
            for dim_key in ("image_width", "image_height", "screen_width", "screen_height"):
                dim_val = s_dict.get(dim_key)
                if dim_val is not None and (
                    isinstance(dim_val, bool) or not isinstance(dim_val, int) or dim_val <= 0
                ):
                    raise ValueError(
                        f"Step {idx} 'click_at' {dim_key} must be a positive integer."
                    )
            for off_key in ("virtual_left", "virtual_top"):
                off_val = s_dict.get(off_key)
                if off_val is not None and (
                    isinstance(off_val, bool) or not isinstance(off_val, int)
                ):
                    raise ValueError(
                        f"Step {idx} 'click_at' {off_key} must be an integer."
                    )
            dpi_val = s_dict.get("dpi_scale")
            if dpi_val is not None and (
                isinstance(dpi_val, bool)
                or not isinstance(dpi_val, (int, float))
                or not math.isfinite(float(dpi_val))
                or float(dpi_val) <= 0
            ):
                raise ValueError(
                    f"Step {idx} 'click_at' dpi_scale must be a positive finite number."
                )
            btn = s_dict.get("button", "left")
            if btn is not None and str(btn).strip().lower() not in VALID_CLICK_BUTTONS:
                valid_btns = sorted(VALID_CLICK_BUTTONS)
                raise ValueError(
                    f"Step {idx} 'click_at' unsupported button {btn!r}. "
                    f"Must be one of {valid_btns}."
                )
            clicks = s_dict.get("clicks", 1)
            if isinstance(clicks, bool) or not isinstance(clicks, int) or clicks < 1 or clicks > 3:
                raise ValueError(
                    f"Step {idx} 'click_at' clicks must be an integer between 1 and 3."
                )

    snap_runner = ui_snapshot_fn or ui_snapshot
    action_runner = gui_action_fn or gui_action
    title_getter = window_title_fn or (lambda: _get_foreground_window_title(user32))

    def _run_snap() -> dict[str, Any]:
        if cdp_bridge is not None and snap_runner is ui_snapshot:
            return snap_runner(user32=user32, cdp_bridge=cdp_bridge)
        return snap_runner(user32=user32)

    is_stale = _UI_REF_STORE.stale or (time.time() - _LAST_UI_SNAPSHOT_TIME) > 60.0
    has_fresh_cdp_snap = (
        isinstance(_LAST_UI_SNAPSHOT, dict)
        and _LAST_UI_SNAPSHOT.get("source") == "cdp"
        and not is_stale
    )
    if (not _UI_REF_STORE and not has_fresh_cdp_snap) or _LAST_UI_SNAPSHOT is None or is_stale:
        before_snapshot = _run_snap()
        if not (
            isinstance(before_snapshot, dict)
            and before_snapshot.get("source") in {"cdp", "uia_com"}
        ):
            set_ui_snapshot_state(before_snapshot)
    else:
        before_snapshot = _LAST_UI_SNAPSHOT

    initial_title = str(before_snapshot.get("window_title") or "")
    if not initial_title:
        with contextlib.suppress(Exception):
            initial_title = title_getter()

    step_results: list[dict[str, Any]] = []
    status = "completed"
    should_guard = focus_backend is not None or (
        user32 is None and (gui_action_fn is None or gui_action_fn is gui_action)
    )
    target_guard_hwnd = (
        expected_hwnd
        if expected_hwnd is not None
        else (_UI_REF_STORE.hwnd if _UI_REF_STORE.hwnd > 0 else None)
    )
    guard_cm: Any = (
        PreconditionGuard(
            focus_backend or Win32FocusPreconditionBackend(),
            expected_hwnd=target_guard_hwnd,
            require_foreground=require_foreground,
        )
        if should_guard
        else contextlib.nullcontext()
    )

    try:
        with guard_cm as guard:
            runner_guard_kw: dict[str, Any] = (
                {"_guard": guard}
                if (guard is not None and _action_runner_accepts_guard(action_runner))
                else {}
            )
            for idx, s in enumerate(steps):
                _check_cancelled(cancel_event)
                s_dict = (
                    s.model_dump(exclude_unset=True) if hasattr(s, "model_dump") else dict(s)
                )
                op = str(s_dict.get("op") or "").strip().lower()
                trigger_nav = False

                if guard is not None:
                    step_label = str(s_dict.get("label") or f"step_{idx + 1}")
                    guard.assert_ready(step_label)

                try:
                    if op == "act":
                        ref = str(s_dict.get("ref") or "").strip()
                        action = str(s_dict.get("action") or "").strip().lower()
                        val = s_dict.get("value")
                        step_interval = s_dict.get("interval_ms")
                        act_kwargs: dict[str, Any] = {
                            "ui_snapshot_fn": snap_runner,
                            "gui_action_fn": action_runner,
                            "user32": user32,
                            "_guard": guard,
                            "_capture_snapshot": False,
                        }
                        if cdp_bridge is not None:
                            act_kwargs["cdp_bridge"] = cdp_bridge
                        if step_interval is not None and not isinstance(step_interval, bool):
                            with contextlib.suppress(TypeError, ValueError):
                                act_kwargs["interval_ms"] = int(step_interval)
                        act_res = ui_act(
                            ref,
                            action,
                            val,
                            **act_kwargs,
                        )
                        if (
                            isinstance(act_res, dict)
                            and act_res.get("status") == "aborted_precondition"
                        ):
                            abort_res = dict(act_res)
                            abort_res["steps_executed"] = len(step_results)
                            abort_res["step_results"] = step_results
                            return abort_res
                        if isinstance(act_res, dict) and act_res.get("ok") is False:
                            raise RuntimeError(
                                str(
                                    act_res.get("error")
                                    or act_res.get("message")
                                    or f"Action {action!r} on {ref!r} failed"
                                )
                            )
                        step_results.append(
                            {
                                "step": idx,
                                "op": "act",
                                "ref": ref,
                                "action": action,
                                "status": "ok",
                            }
                        )
                        if action in {"click", "double_click"}:
                            trigger_nav = True

                    elif op == "press_keys":
                        keys = [str(k) for k in s_dict["keys"]]
                        step_res = action_runner(
                            action="press_keys", keys=keys, user32=user32, **runner_guard_kw
                        )
                        if (
                            isinstance(step_res, dict)
                            and step_res.get("status") == "aborted_precondition"
                        ):
                            abort_res = dict(step_res)
                            abort_res["steps_executed"] = len(step_results)
                            abort_res["step_results"] = step_results
                            return abort_res
                        step_results.append(
                            {
                                "step": idx,
                                "op": "press_keys",
                                "status": "ok",
                            }
                        )
                        norm_keys = [k.strip().lower() for k in keys]
                        if any(k in {"enter", "return", "tab", "escape", "esc"} for k in norm_keys):
                            trigger_nav = True

                    elif op == "scroll":
                        delta = int(s_dict["delta"])
                        x = s_dict.get("x")
                        y = s_dict.get("y")
                        step_res = action_runner(
                            action="scroll", delta=delta, x=x, y=y, user32=user32, **runner_guard_kw
                        )
                        if (
                            isinstance(step_res, dict)
                            and step_res.get("status") == "aborted_precondition"
                        ):
                            abort_res = dict(step_res)
                            abort_res["steps_executed"] = len(step_results)
                            abort_res["step_results"] = step_results
                            return abort_res
                        step_results.append(
                            {
                                "step": idx,
                                "op": "scroll",
                                "status": "ok",
                            }
                        )

                    elif op == "wait":
                        sec = float(s_dict.get("seconds", 0.5))
                        action_runner(action="wait", seconds=sec, user32=user32, **runner_guard_kw)
                        step_results.append(
                            {
                                "step": idx,
                                "op": "wait",
                                "status": "ok",
                            }
                        )

                    elif op == "click_at":
                        raw_cs = s_dict.get("coord_space")
                        step_cs = (
                            raw_cs.strip().lower()
                            if isinstance(raw_cs, str) and raw_cs.strip()
                            else None
                        )
                        x_val: int | float = (
                            int(s_dict["x"])
                            if step_cs in (None, "screen")
                            else float(s_dict["x"])
                        )
                        y_val: int | float = (
                            int(s_dict["y"])
                            if step_cs in (None, "screen")
                            else float(s_dict["y"])
                        )
                        clicks = int(s_dict.get("clicks", 1))
                        btn = str(s_dict.get("button", "left"))
                        click_extra_kw: dict[str, Any] = {}
                        if step_cs is not None:
                            click_extra_kw["coord_space"] = step_cs
                        for int_geom_key in (
                            "image_width",
                            "image_height",
                            "screen_width",
                            "screen_height",
                            "virtual_left",
                            "virtual_top",
                        ):
                            if s_dict.get(int_geom_key) is not None:
                                click_extra_kw[int_geom_key] = int(s_dict[int_geom_key])
                        if s_dict.get("dpi_scale") is not None:
                            click_extra_kw["dpi_scale"] = float(s_dict["dpi_scale"])
                        step_res = action_runner(
                            action="click_at",
                            x=x_val,
                            y=y_val,
                            clicks=clicks,
                            button=btn,
                            user32=user32,
                            **click_extra_kw,
                            **runner_guard_kw,
                        )
                        if (
                            isinstance(step_res, dict)
                            and step_res.get("status") == "aborted_precondition"
                        ):
                            abort_res = dict(step_res)
                            abort_res["steps_executed"] = len(step_results)
                            abort_res["step_results"] = step_results
                            return abort_res
                        step_results.append(
                            {
                                "step": idx,
                                "op": "click_at",
                                "status": "ok",
                            }
                        )
                        trigger_nav = True

                except (PreconditionFailedError, ActionCancelledError):
                    raise
                except Exception as exc:
                    step_results.append(
                        {
                            "step": idx,
                            "op": op,
                            "status": "error",
                            "error": str(exc),
                        }
                    )
                    if stop_on_error:
                        status = "partial_error"
                        break
                    continue

                if stop_on_window_change and trigger_nav:
                    if guard is not None and (idx + 1 < len(steps)):
                        next_step = steps[idx + 1]
                        next_label = (
                            str(next_step.get("label") or f"step_{idx + 2}")
                            if isinstance(next_step, dict)
                            else f"step_{idx + 2}"
                        )
                        guard.assert_ready(next_label)
                    current_title = ""
                    with contextlib.suppress(Exception):
                        current_title = title_getter()
                    if current_title and current_title != initial_title:
                        status = "stopped_on_window_change"
                        break
    except PreconditionFailedError as exc:
        abort = exc.to_abort_payload()
        abort["steps_executed"] = len(step_results)
        abort["step_results"] = step_results
        return abort
    except ActionCancelledError as exc:
        return {
            "ok": False,
            "status": "cancelled",
            "cancelled": True,
            "error": str(exc),
            "steps_executed": len(step_results),
            "step_results": step_results,
        }

    if status == "completed" and any(r.get("status") == "error" for r in step_results):
        status = "partial_error"

    after_snapshot = _run_snap()
    if isinstance(after_snapshot, dict) and after_snapshot.get("source") == "cdp":
        if after_snapshot.get("ok"):
            _record_cdp_snapshot_state(after_snapshot)
    elif not (isinstance(after_snapshot, dict) and after_snapshot.get("source") == "uia_com"):
        set_ui_snapshot_state(after_snapshot)
    diff = compute_ui_diff(before_snapshot, after_snapshot)

    return {
        "ok": True,
        "status": status,
        "steps_executed": len(step_results),
        "step_results": step_results,
        "diff": diff,
        "snapshot": after_snapshot,
    }


def _record_cdp_snapshot_state(cdp_res: dict[str, Any]) -> None:
    """Record a successful CDP snapshot without overwriting native UIA refs."""
    global _LAST_UI_SNAPSHOT, _LAST_UI_SNAPSHOT_TIME, _LAST_UI_WINDOW_TITLE
    _LAST_UI_SNAPSHOT = cdp_res
    _LAST_UI_SNAPSHOT_TIME = time.time()
    _LAST_UI_WINDOW_TITLE = str(cdp_res.get("window_title") or cdp_res.get("title") or "")


def register_windows_os_tools(
    registry: ToolRegistry,
    *,
    risk_level: RiskLevel = RiskLevel.READ_ONLY,
    focus_backend: FocusPreconditionBackend | None = None,
    verifier: ActionVerifier | None = None,
    cdp_bridge: CDPBridge | None = None,
    user32: Any = None,
    kernel32: Any = None,
) -> None:
    """Register interactive Windows OS desktop control and UI automation tools for Chat."""

    def _open_or_focus_handler(
        app_name: str,
        target_path: str | None = None,
    ) -> dict[str, Any]:
        return open_or_focus_app(
            app_name,
            target_path,
            user32=user32,
            kernel32=kernel32,
            focus_backend=focus_backend,
            verifier=verifier,
        )

    def _ui_snapshot_handler(
        app_name: str | None = None,
        max_elements: int | None = 60,
        interactive_only: bool | None = False,
    ) -> dict[str, Any]:
        return ui_snapshot(
            app_name,
            60 if max_elements is None else max_elements,
            interactive_only=False if interactive_only is None else interactive_only,
            user32=user32,
            kernel32=kernel32,
            cdp_bridge=cdp_bridge,
        )

    def _ui_act_handler(
        ref: str,
        action: str | None = "click",
        value: str | None = None,
        interval_ms: int | None = 12,
        refresh_snapshot: bool | None = False,
    ) -> dict[str, Any]:
        return ui_act(
            ref,
            "click" if action is None else action,
            value,
            interval_ms=12 if interval_ms is None else interval_ms,
            refresh_snapshot=False if refresh_snapshot is None else refresh_snapshot,
            user32=user32,
            focus_backend=focus_backend,
            cdp_bridge=cdp_bridge,
        )

    def _ui_batch_handler(
        steps: list[dict[str, Any]],
        stop_on_error: bool | None = True,
        stop_on_window_change: bool | None = True,
    ) -> dict[str, Any]:
        return ui_batch(
            steps,
            stop_on_error=True if stop_on_error is None else stop_on_error,
            stop_on_window_change=(
                True if stop_on_window_change is None else stop_on_window_change
            ),
            user32=user32,
            focus_backend=focus_backend,
            cdp_bridge=cdp_bridge,
        )

    def _click_ui_element_handler(
        name: str,
        control_type: str | None = None,
        app_name: str | None = None,
    ) -> dict[str, Any]:
        return click_ui_element(
            name,
            control_type,
            app_name,
            user32=user32,
            kernel32=kernel32,
            focus_backend=focus_backend,
        )

    def _open_url_handler(
        url: str,
        browser: str | None = None,
    ) -> dict[str, Any]:
        return open_url(
            url,
            browser,
            user32=user32,
            kernel32=kernel32,
            cdp_bridge=cdp_bridge,
        )

    def _gui_action_handler(
        action: str,
        x: int | float | None = None,
        y: int | float | None = None,
        text: str | None = None,
        keys: list[str] | None = None,
        clicks: int | None = 1,
        button: str | None = "left",
        delta: int | None = 0,
        seconds: float | None = 1.0,
        interval_ms: int | None = 12,
        coord_space: str | None = None,
        image_width: int | None = None,
        image_height: int | None = None,
        capture_id: str | None = None,
        capture_bounds: list[int] | None = None,
        ref: str | None = None,
        key: str | None = None,
        app_name: str | None = None,
        expected_hwnd: int | None = None,
    ) -> dict[str, Any]:
        return gui_action(
            action,
            x=x,
            y=y,
            text=text,
            keys=keys,
            clicks=1 if clicks is None else clicks,
            button="left" if button is None else button,
            delta=0 if delta is None else delta,
            seconds=1.0 if seconds is None else seconds,
            interval_ms=12 if interval_ms is None else interval_ms,
            coord_space=coord_space,
            image_width=image_width,
            image_height=image_height,
            capture_id=capture_id,
            capture_bounds=capture_bounds,
            ref=ref,
            key=key,
            cdp_bridge=cdp_bridge,
            app_name=app_name,
            expected_hwnd=expected_hwnd,
            user32=user32,
            kernel32=kernel32,
            focus_backend=focus_backend,
        )

    def _inspect_ui_elements_handler(
        app_name: str | None = None,
        max_elements: int | None = 60,
    ) -> dict[str, Any]:
        return inspect_ui_elements(
            app_name,
            60 if max_elements is None else max_elements,
            user32=user32,
            kernel32=kernel32,
        )

    registry.register(
        name="open_or_focus_app",
        description=(
            "Focus an already-open Windows application window if visible, or launch the "
            "installed native, Microsoft Store, or PWA app first (e.g., YouTube, Spotify, "
            "WhatsApp, vscode, terminal, chrome, edge, explorer, notepad, calculator, "
            "settings, taskmgr). Always prefer this tool over browser URLs for services "
            "that may be installed as desktop/Store/PWA apps; only falls back to the "
            "browser if no installed app exists or the user explicitly requests the browser "
            "(e.g. 'open YouTube in Chrome')."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "app_name": {
                    "type": "string",
                    "description": (
                        "Application or service name/alias (e.g., youtube, spotify, whatsapp, "
                        "notepad, calculator, vscode). Resolves installed native/Store/PWA apps "
                        "first unless a browser/web request is explicitly stated."
                    ),
                },
                "target_path": {
                    "type": "string",
                    "description": "Optional existing local file or directory path to open.",
                },
            },
            "required": ["app_name"],
            "additionalProperties": False,
        },
        risk_level=risk_level,
        handler=_open_or_focus_handler,
    )

    try:
        registry.get("open_url")
    except KeyError:
        registry.register(
            name="open_url",
            description=(
                "Navigate directly to an http/https URL. Omit 'browser' to navigate inside an "
                "installed Chromium PWA (e.g. YouTube Watch Later "
                "'https://www.youtube.com/playlist?list=WL') via CDP without spawning Chrome; "
                "pass browser='chrome' (or 'edge'/'brave') when the user explicitly asks for "
                "the browser."
            ),
            input_schema={
                "type": "object",
                "properties": {
                    "url": {
                        "type": "string",
                        "description": "Web URL (http:// or https://) or site shorthand.",
                    },
                    "browser": {
                        "type": "string",
                        "description": (
                            "Optional explicit browser (e.g. 'chrome', 'edge'). Omit to navigate "
                            "inside an installed PWA when available."
                        ),
                    },
                },
                "required": ["url"],
                "additionalProperties": False,
            },
            risk_level=risk_level,
            handler=_open_url_handler,
        )

    registry.register(
        name="ui_snapshot",
        description=(
            "Capture visible UI elements and accessibility tree for the active window or "
            "specified application with stable element reference IDs: CDP refs ('b1', 'b2', ...) "
            "for Chrome/Edge/Brave browsers and Chromium PWAs, or UIA refs ('e1', 'e2', ...) "
            "for native Windows apps."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "app_name": {
                    "type": "string",
                    "description": "Optional target application or window title.",
                },
                "max_elements": {
                    "type": "integer",
                    "description": "Maximum number of UI elements to capture (1..200).",
                },
                "interactive_only": {
                    "type": "boolean",
                    "description": "Whether to return only interactive controls.",
                },
            },
            "additionalProperties": False,
        },
        risk_level=risk_level,
        handler=_ui_snapshot_handler,
    )

    registry.register(
        name="ui_act",
        description=(
            "Perform a semantic action on a UI element identified by its snapshot reference ID "
            "('b1'..'bN' for Chromium browsers/PWAs via CDP, or 'e1'..'eN' for native Windows "
            "apps) and return a verified before -> after state diff."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "ref": {
                    "type": "string",
                    "description": (
                        "Element reference ID from ui_snapshot (e.g. 'b1' for browser/PWA "
                        "CDP elements, or 'e1' for native UIA elements)."
                    ),
                },
                "action": {
                    "type": "string",
                    "enum": sorted(VALID_UI_ACT_ACTIONS),
                    "description": "Semantic action to execute on the element.",
                },
                "value": {
                    "type": "string",
                    "description": (
                        "Text value for 'set_value'/'type', or key name for 'press' (e.g. 'Enter')."
                    ),
                },
                "interval_ms": {
                    "type": "integer",
                    "description": "Inter-character typing interval in milliseconds (0..200).",
                },
                "refresh_snapshot": {
                    "type": "boolean",
                    "description": (
                        "When true, re-walk the full UI tree after acting. Defaults to false "
                        "(fast lightweight target delta unless the active window changes)."
                    ),
                },
            },
            "required": ["ref", "action"],
            "additionalProperties": False,
        },
        risk_level=risk_level,
        handler=_ui_act_handler,
    )

    registry.register(
        name="ui_batch",
        description=(
            "Execute a sequence of 1..20 GUI/UI steps in one call with focus precondition "
            "checks and one final UI diff snapshot."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "steps": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "op": {
                                "type": "string",
                                "enum": sorted(VALID_UI_BATCH_OPS),
                            },
                            "ref": {"type": "string"},
                            "action": {
                                "type": "string",
                                "enum": sorted(VALID_UI_ACT_ACTIONS),
                            },
                            "value": {"type": "string"},
                            "keys": {"type": "array", "items": {"type": "string"}},
                            "delta": {"type": "integer"},
                            "seconds": {"type": "number"},
                            "x": {"type": "number"},
                            "y": {"type": "number"},
                            "coord_space": {
                                "type": "string",
                                "enum": sorted(VALID_COORD_SPACES),
                            },
                            "image_width": {"type": "integer"},
                            "image_height": {"type": "integer"},
                            "clicks": {"type": "integer"},
                            "button": {
                                "type": "string",
                                "enum": sorted(VALID_CLICK_BUTTONS),
                            },
                        },
                        "required": ["op"],
                    },
                    "description": "1 to 20 action steps to execute sequentially.",
                },
                "stop_on_error": {
                    "type": "boolean",
                    "description": "Abort remaining steps if any step errors (default true).",
                },
                "stop_on_window_change": {
                    "type": "boolean",
                    "description": "Stop early if foreground window title changes (default true).",
                },
            },
            "required": ["steps"],
            "additionalProperties": False,
        },
        risk_level=risk_level,
        handler=_ui_batch_handler,
    )

    registry.register(
        name="click_ui_element",
        description=(
            "Find and invoke or left-click a visible UI control by name in a Windows app "
            "or the active foreground window."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "name": {
                    "type": "string",
                    "description": "Visible label or name of the UI control to click.",
                },
                "control_type": {
                    "type": "string",
                    "description": "Optional UI Automation control type filter (e.g. 'Button').",
                },
                "app_name": {
                    "type": "string",
                    "description": "Optional target application or window title.",
                },
            },
            "required": ["name"],
            "additionalProperties": False,
        },
        risk_level=risk_level,
        handler=_click_ui_element_handler,
    )

    registry.register(
        name="gui_action",
        description=(
            "Perform a desktop GUI input, browser CDP ref/history action, or wait step with "
            "focus precondition checking and DPI-exact coordinate scaling: click_at, "
            "double_click, right_click, move, type_text, press_keys, scroll, wait, "
            "navigate_back, navigate_forward, or ref='b1' with click/type/press."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": sorted(VALID_GUI_ACTIONS),
                    "description": "GUI input, CDP browser action, or wait step to execute.",
                },
                "x": {"type": "number", "description": "Horizontal coordinate (>= 0)."},
                "y": {"type": "number", "description": "Vertical coordinate (>= 0)."},
                "coord_space": {
                    "type": "string",
                    "enum": sorted(VALID_COORD_SPACES),
                    "description": (
                        "Coordinate space for (x, y): 'normalized_1000' (0..1000 from "
                        "look_at_screen/inspect_screen), 'image' (pixels in screenshot), or "
                        "'screen' (physical virtual-screen pixels, default)."
                    ),
                },
                "image_width": {
                    "type": "integer",
                    "description": "Screenshot image width when coord_space='image'.",
                },
                "image_height": {
                    "type": "integer",
                    "description": "Screenshot image height when coord_space='image'.",
                },
                "ref": {
                    "type": "string",
                    "description": "Optional browser CDP ref (e.g. 'b1') for direct CDP execution.",
                },
                "key": {
                    "type": "string",
                    "description": "Single key name (e.g. 'Enter') when using action='press'.",
                },
                "text": {"type": "string", "description": "Unicode text to type (1..2000 chars)."},
                "keys": {
                    "type": "array",
                    "items": {"type": "string"},
                    "description": "Key names for 'press_keys' (e.g. ['ctrl', 's']).",
                },
                "clicks": {"type": "integer", "description": "Click count (1..3, default 1)."},
                "button": {
                    "type": "string",
                    "enum": sorted(VALID_CLICK_BUTTONS),
                    "description": "Mouse button for 'click_at'.",
                },
                "delta": {"type": "integer", "description": "Mouse wheel scroll delta."},
                "seconds": {"type": "number", "description": "Wait duration in seconds."},
                "interval_ms": {
                    "type": "integer",
                    "description": "Inter-character typing interval in milliseconds (0..200).",
                },
                "app_name": {
                    "type": "string",
                    "description": (
                        "Optional target application name or window title; enforces that the "
                        "named window is non-minimized and focused without un-minimizing it."
                    ),
                },
                "expected_hwnd": {
                    "type": "integer",
                    "description": "Optional explicit target window HWND for precondition check.",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        risk_level=risk_level,
        handler=_gui_action_handler,
    )

    registry.register(
        name="inspect_ui_elements",
        description=(
            "Inspect visible interactive UI Automation controls and coordinates in a "
            "Windows application or the active foreground window."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "app_name": {
                    "type": "string",
                    "description": "Optional target application or window title.",
                },
                "max_elements": {
                    "type": "integer",
                    "description": "Maximum number of UI elements to return (1..150).",
                },
            },
            "additionalProperties": False,
        },
        risk_level=risk_level,
        handler=_inspect_ui_elements_handler,
    )

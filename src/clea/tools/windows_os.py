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
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Window show/focus constants
SW_MAXIMIZE: int = 3
SW_MINIMIZE: int = 6
SW_RESTORE: int = 9
WM_CLOSE: int = 0x0010

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
        "ms-settings:display",
        "ms-settings:sound",
        "ms-settings:notifications",
        "ms-settings:quiethours",
        "ms-settings:powersleep",
        "ms-settings:batterysaver",
        "ms-settings:storagesense",
        "ms-settings:multitasking",
        "ms-settings:project",
        "ms-settings:about",
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
        "ms-settings:dateandtime",
        "ms-settings:regionlanguage",
        "ms-settings:gaming-gamebar",
        "ms-settings:easeofaccess",
        "ms-settings:privacy",
        "ms-settings:windowsupdate",
    }
)

_ALLOWED_URL_SCHEMES: frozenset[str] = frozenset({"http", "https"})

_DANGEROUS_SCHEME_PREFIXES: tuple[str, ...] = (
    "file:",
    "javascript:",
    "data:",
    "vbscript:",
    "cmd:",
    "powershell:",
    "ms-settings:",
    "shell:",
    "ws:",
    "wss:",
    "about:",
    "chrome:",
    "edge:",
)

_SITE_SHORTCUTS: dict[str, str] = {
    "google": "https://www.google.com",
    "github": "https://github.com",
    "youtube": "https://www.youtube.com",
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

    return u32


def _get_win32_kernel32(kernel32: Any = None) -> Any:
    """Return kernel32 ctypes DLL with explicit signatures or the injected test double."""
    if kernel32 is not None:
        return kernel32
    if sys.platform != "win32":
        raise OSError("Windows OS tools require Windows or mocked Win32 APIs.")

    import ctypes.wintypes as wintypes

    k32 = ctypes.WinDLL("kernel32", use_last_error=True)

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
        if bool(user32.IsWindowVisible(hwnd)):
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


def _window_matches_spec(window: EnumeratedWindow, spec: _AppSpec) -> bool:
    if not window.is_visible:
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
_APP_CACHE_TTL: float = 300.0  # 5 minutes


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


def get_installed_apps(
    *,
    force_refresh: bool = False,
    app_catalog_fetcher: Callable[[], list[dict[str, str]]] | None = None,
) -> list[dict[str, str]]:
    """Retrieve catalog of installed Start Menu / UWP apps via Get-StartApps with caching."""
    global _INSTALLED_APPS_CACHE, _INSTALLED_APPS_CACHE_TIME
    now = time.monotonic()
    if (
        not force_refresh
        and _INSTALLED_APPS_CACHE is not None
        and (now - _INSTALLED_APPS_CACHE_TIME) < _APP_CACHE_TTL
    ):
        return _INSTALLED_APPS_CACHE

    if app_catalog_fetcher is not None:
        raw_apps = app_catalog_fetcher()
    else:
        ps_exe = shutil.which("powershell.exe")
        if not ps_exe:
            return []
        try:
            proc = subprocess.run(
                [
                    ps_exe,
                    "-NoProfile",
                    "-NonInteractive",
                    "-Command",
                    "Get-StartApps | Select-Object Name, AppID | ConvertTo-Json -Compress",
                ],
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10.0,
                check=False,
            )
            if proc.returncode != 0 or not proc.stdout.strip():
                return []
            parsed = json.loads(proc.stdout.strip())
            raw_apps = parsed if isinstance(parsed, list) else [parsed]
        except Exception as exc:
            logger.debug("Failed to retrieve StartApps: %s", exc)
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
    _INSTALLED_APPS_CACHE_TIME = now
    return cleaned


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
) -> dict[str, Any]:
    """Focus a running application window or launch an allowed desktop application.

    If a matching visible window is already open and no ``target_path`` is supplied,
    restores the window if minimized and brings it to the foreground. Otherwise
    launches the validated executable or Start Menu / UWP app with ``shell=False``.
    """
    resolved_target = _validate_target_path(target_path, env=env)
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
                    if hasattr(os, "startfile"):
                        os.startfile(str(file_target))
                    else:
                        popen_fn(
                            ["explorer.exe", str(file_target)],
                            shell=False,
                            stdout=subprocess.DEVNULL,
                            stderr=subprocess.DEVNULL,
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

        for win in windows:
            if _window_matches_spec(win, spec):
                if hasattr(u32, "IsIconic") and bool(u32.IsIconic(win.hwnd)):
                    u32.ShowWindow(win.hwnd, SW_RESTORE)
                u32.SetForegroundWindow(win.hwnd)
                return {
                    "ok": True,
                    "status": "focused",
                    "app": spec.canonical_name,
                    "window_title": win.title,
                    "hwnd": int(win.hwnd),
                    "executable": None,
                    "target_path": None,
                }

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

            popen_fn(
                cmd,
                shell=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
            return {
                "ok": True,
                "status": "launched",
                "app": launched_spec.canonical_name,
                "window_title": None,
                "hwnd": None,
                "executable": cmd[0],
                "target_path": str(resolved_target) if resolved_target is not None else None,
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

    catalog = get_installed_apps(app_catalog_fetcher=app_catalog_fetcher)
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

            for win in windows:
                if not win.is_visible:
                    continue
                w_title = (win.title or "").lower()
                w_proc = (win.process_name or "").lower()
                if matched_name.lower() in w_title or matched_name.lower() in w_proc:
                    if hasattr(u32, "IsIconic") and bool(u32.IsIconic(win.hwnd)):
                        u32.ShowWindow(win.hwnd, SW_RESTORE)
                    u32.SetForegroundWindow(win.hwnd)
                    return {
                        "ok": True,
                        "status": "focused",
                        "app": matched_name,
                        "window_title": win.title,
                        "hwnd": int(win.hwnd),
                        "executable": None,
                        "target_path": None,
                    }

        launch_cmd = ["explorer.exe", f"shell:AppsFolder\\{matched_appid}"]
        if resolved_target is not None:
            launch_cmd.append(str(resolved_target))
        popen_fn(
            launch_cmd,
            shell=False,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
        )
        return {
            "ok": True,
            "status": "launched",
            "app": matched_name,
            "window_title": None,
            "hwnd": None,
            "executable": launch_cmd[0],
            "target_path": str(resolved_target) if resolved_target is not None else None,
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

    proc = subprocess.run(
        [
            ps_exe,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"& {{ {_POWERSHELL_SET_VOLUME_SCRIPT} }} -Scalar {scalar:.4f}",
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
        raise RuntimeError(f"Failed to set system volume: {detail or f'exit {proc.returncode}'}")


def control_media_volume(
    action: str,
    level: int | None = None,
    *,
    user32: Any = None,
    volume_setter: Callable[[int], None] | None = None,
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

    vk = _MEDIA_VOLUME_VK_MAP[normalized]
    u32 = _get_win32_user32(user32)
    u32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY, 0)
    u32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)
    return {
        "ok": True,
        "action": normalized,
        "level": clamped_level,
    }


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
        runner = powershell_runner or subprocess.run
        try:
            inc_bytes_flag = " -IncludeImageBytes" if include_image_bytes else ""
            cmd = [
                ps_exe,
                "-NoProfile",
                "-NonInteractive",
                "-Command",
                (
                    f"& {{ {_POWERSHELL_READ_CLIPBOARD_HISTORY_SCRIPT} }} "
                    f"-MaxItems {int(max_items)} -MaxChars {int(cap)}{inc_bytes_flag}"
                ),
            ]
            proc = runner(
                cmd,
                shell=False,
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=10.0,
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

    runner = powershell_runner or subprocess.run
    proc = runner(
        [
            ps_exe,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            f"& {{ {_POWERSHELL_SELECT_CLIPBOARD_ITEM_SCRIPT} }} -ItemIndex {index}",
        ],
        shell=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=10.0,
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


def open_url(
    url: str,
    browser: str | None = None,
    *,
    startfile_fn: Callable[[str], Any] | None = None,
    popen_fn: Callable[..., Any] = subprocess.Popen,
    which_fn: Callable[[str], str | None] = shutil.which,
    env: dict[str, str] | None = None,
) -> dict[str, Any]:
    """Validate and open an http/https URL in the default or specified browser."""
    if not isinstance(url, str):
        raise ValueError("url must be a string.")
    stripped = url.strip()
    if not stripped:
        raise ValueError("url must not be empty.")

    if any(ord(ch) < 32 for ch in stripped):
        raise ValueError("URL contains invalid control characters.")

    lower_stripped = stripped.lower()
    if any(lower_stripped.startswith(prefix) for prefix in _DANGEROUS_SCHEME_PREFIXES):
        raise ValueError(f"Disallowed URL scheme in {url!r}. Only http and https URLs are allowed.")

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

    if browser is None:
        starter = startfile_fn or getattr(os, "startfile", None)
        if starter is not None:
            starter(target_url)
        else:
            popen_fn(
                ["explorer.exe", target_url],
                shell=False,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
            )
        return {
            "ok": True,
            "success": True,
            "url": target_url,
            "browser": "default",
            "status": "opened",
        }

    b_clean = browser.strip().lower()
    if b_clean in _DISALLOWED_SHELL_BINARIES or _SHELL_METACHAR_RE.search(b_clean):
        raise ValueError(f"Disallowed browser name: {browser!r}")

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

    popen_fn(
        [browser_exe, target_url],
        shell=False,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return {
        "ok": True,
        "success": True,
        "url": target_url,
        "browser": b_clean,
        "status": "opened",
    }


def _get_wmi_brightness(
    powershell_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> int:
    """Read current display brightness percentage (0..100) via WmiMonitorBrightness."""
    runner = powershell_runner or subprocess.run
    ps_exe = shutil.which("powershell.exe") or "powershell.exe"
    proc = runner(
        [
            ps_exe,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            (
                "(Get-CimInstance -Namespace root\\wmi "
                "-ClassName WmiMonitorBrightness).CurrentBrightness"
            ),
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
    runner = powershell_runner or subprocess.run
    ps_exe = shutil.which("powershell.exe") or "powershell.exe"
    script = (
        "$b = Get-CimInstance -Namespace root\\wmi -ClassName WmiMonitorBrightnessMethods; "
        "if ($b) { "
        f"Invoke-CimMethod -InputObject $b -MethodName WmiSetBrightness "
        f"-Arguments @{{Timeout = 1; Brightness = [uint32]{clamped}}} "
        "} else { throw 'WmiMonitorBrightnessMethods not available' }"
    )
    proc = runner(
        [
            ps_exe,
            "-NoProfile",
            "-NonInteractive",
            "-Command",
            script,
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
                for win in windows:
                    if _window_matches_spec(win, spec):
                        return int(win.hwnd)

        for win in windows:
            if not win.is_visible:
                continue
            w_title = (win.title or "").lower()
            w_proc = (win.process_name or "").lower()
            if target_title and target_title in w_title:
                return int(win.hwnd)
            if target_app and (target_app in w_proc or target_app in w_title):
                return int(win.hwnd)

        raise ValueError(
            f"No visible window found matching app_name={app_name!r}, title={title!r}."
        )

    if hasattr(u32, "GetForegroundWindow"):
        fg = u32.GetForegroundWindow()
        if fg:
            return int(fg)

    raise RuntimeError("No active foreground window found.")


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
    window_action: str | None = None,
    user32: Any = None,
    kernel32: Any = None,
    window_enumerator: Callable[[], list[EnumeratedWindow]] | None = None,
    wmi_brightness_getter: Callable[[], int] | None = None,
    wmi_brightness_setter: Callable[[int], None] | None = None,
    startfile_fn: Callable[[str], Any] | None = None,
    popen_fn: Callable[..., Any] = subprocess.Popen,
    powershell_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
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
        elif w_act in ("minimize_window", "maximize_window", "restore_window", "close_window"):
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

    # 2. Window actions
    if normalized in (
        "minimize_window",
        "minimize",
        "maximize_window",
        "maximize",
        "restore_window",
        "restore",
        "close_window",
        "close",
    ):
        effective_title = window_title if title is None else title
        target_hwnd = _resolve_window_hwnd(
            hwnd=hwnd,
            app_name=app_name,
            title=effective_title,
            user32=user32,
            kernel32=kernel32,
            window_enumerator=window_enumerator,
        )
        u32 = _get_win32_user32(user32)

        if normalized in ("minimize_window", "minimize"):
            u32.ShowWindow(target_hwnd, SW_MINIMIZE)
            return {
                "ok": True,
                "success": True,
                "action": "minimize_window",
                "hwnd": target_hwnd,
            }
        if normalized in ("maximize_window", "maximize"):
            u32.ShowWindow(target_hwnd, SW_MAXIMIZE)
            return {
                "ok": True,
                "success": True,
                "action": "maximize_window",
                "hwnd": target_hwnd,
            }
        if normalized in ("restore_window", "restore"):
            u32.ShowWindow(target_hwnd, SW_RESTORE)
            return {
                "ok": True,
                "success": True,
                "action": "restore_window",
                "hwnd": target_hwnd,
            }
        if normalized in ("close_window", "close"):
            u32.PostMessageW(target_hwnd, WM_CLOSE, 0, 0)
            return {
                "ok": True,
                "success": True,
                "action": "close_window",
                "hwnd": target_hwnd,
            }

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
        return {
            "ok": True,
            "success": True,
            "action": "open_settings",
            "uri": target_uri,
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

VALID_GUI_ACTIONS: frozenset[str] = frozenset(
    {"click_at", "type_text", "press_keys", "scroll", "wait"}
)
VALID_UI_ACT_ACTIONS: frozenset[str] = frozenset(
    {"click", "double_click", "right_click", "set_value", "type", "clear"}
)
VALID_CLICK_BUTTONS: frozenset[str] = frozenset({"left", "right", "double"})
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

    def clear(self) -> None:
        """Clear the stored snapshot and ref table."""
        with self._lock:
            self.snapshot_id = None
            self.window_title = ""
            self.hwnd = 0
            self.elements_by_ref = {}

    def resolve(self, ref: str) -> tuple[str, str, int, dict[str, Any]]:
        """Look up a ref (supporting optional leading '@' and case-insensitivity)."""
        norm_ref = _normalize_ref_id(ref)
        with self._lock:
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

VALID_UI_ACT_ACTIONS: frozenset[str] = frozenset(
    {"click", "set_value", "type", "clear", "double_click", "right_click"}
)
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

_POWERSHELL_UIA_TYPES_COND = (
    "$ctList = @("
    "[System.Windows.Automation.ControlType]::Button,"
    "[System.Windows.Automation.ControlType]::Edit,"
    "[System.Windows.Automation.ControlType]::MenuItem,"
    "[System.Windows.Automation.ControlType]::TabItem,"
    "[System.Windows.Automation.ControlType]::Hyperlink,"
    "[System.Windows.Automation.ControlType]::ListItem,"
    "[System.Windows.Automation.ControlType]::CheckBox,"
    "[System.Windows.Automation.ControlType]::RadioButton,"
    "[System.Windows.Automation.ControlType]::ComboBox,"
    "[System.Windows.Automation.ControlType]::Text"
    "); "
    "$ctConds = @(); "
    "foreach ($ct in $ctList) { "
    "  $ctConds += New-Object System.Windows.Automation.PropertyCondition("
    "[System.Windows.Automation.AutomationElement]::ControlTypeProperty, $ct) "
    "}; "
    "$typeCond = New-Object System.Windows.Automation.OrCondition($ctConds); "
)

_POWERSHELL_INSPECT_UI_SCRIPT = (
    "param([long]$TargetHwnd = 0, [int]$MaxElements = 60, [string]$AppNameBase64 = '') "
    "$AppName = if ($AppNameBase64) { "
    "[System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($AppNameBase64)) "
    "} else { '' }; "
    + _POWERSHELL_UIA_RESOLVE_WINDOW
    + _POWERSHELL_UIA_TYPES_COND
    + "$winTitle = if ($win -ne $null) { "
    "try { [string]$win.Current.Name } catch { '' } } else { '' }; "
    "$items = New-Object System.Collections.ArrayList; "
    "if ($win -ne $null) { "
    "  $all = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, $typeCond); "
    "  foreach ($el in $all) { "
    "    try { "
    "      $cur = $el.Current; "
    "      if ($cur.IsOffscreen) { continue } "
    "      $r = $cur.BoundingRectangle; "
    "      if ([double]::IsInfinity($r.Width) -or [double]::IsInfinity($r.Height) "
    "-or [double]::IsNaN($r.Width) -or [double]::IsNaN($r.Height) "
    "-or $r.Width -le 0 -or $r.Height -le 0) { continue } "
    "      $elName = [string]$cur.Name; "
    "      $elAid = [string]$cur.AutomationId; "
    "      $elVal = $null; "
    "      $vp = $null; "
    "      try { "
    "        if ($el.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, "
    "[ref]$vp)) { "
    "          $elVal = [string]$vp.Current.Value "
    "        } "
    "      } catch { $elVal = $null } "
    "      $ctName = ($cur.ControlType.ProgrammaticName -replace '^ControlType\\.', ''); "
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
    "[string]$ControlTypeFilter = '', [string]$AppNameBase64 = '') "
    "$TargetName = if ($TargetNameBase64) { "
    "[System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($TargetNameBase64)) "
    "} else { '' }; "
    "$AppName = if ($AppNameBase64) { "
    "[System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($AppNameBase64)) "
    "} else { '' }; "
    + _POWERSHELL_UIA_RESOLVE_WINDOW
    + _POWERSHELL_UIA_TYPES_COND
    + "$searchCond = $typeCond; "
    "if ($ControlTypeFilter -ne '') { "
    "  $ctProp = [System.Windows.Automation.ControlType]::$ControlTypeFilter; "
    "  if ($ctProp -ne $null) { "
    "    $searchCond = New-Object System.Windows.Automation.PropertyCondition("
    "[System.Windows.Automation.AutomationElement]::ControlTypeProperty, $ctProp) "
    "  } "
    "} "
    "$exactMatch = $null; $exactIsText = $false; "
    "$subMatch = $null; $subIsText = $false; $aidMatch = $null; "
    "$avail = New-Object System.Collections.ArrayList; "
    "if ($win -ne $null) { "
    "  $all = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, $searchCond); "
    "  foreach ($el in $all) { "
    "    try { "
    "      $cur = $el.Current; "
    "      if ($cur.IsOffscreen) { continue } "
    "      $r = $cur.BoundingRectangle; "
    "      if ([double]::IsInfinity($r.Width) -or [double]::IsInfinity($r.Height) "
    "-or [double]::IsNaN($r.Width) -or [double]::IsNaN($r.Height) "
    "-or $r.Width -le 0 -or $r.Height -le 0) { continue } "
    "      $ctName = ($cur.ControlType.ProgrammaticName -replace '^ControlType\\.', ''); "
    "      if ($ControlTypeFilter -ne '' -and "
    "-not $ctName.Equals($ControlTypeFilter, [System.StringComparison]::OrdinalIgnoreCase)) { "
    "        continue "
    "      } "
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
    "  $cc = $chosen.Current; "
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
    "[int]$InteractiveOnly = 0, [string]$AppNameBase64 = '') "
    "$AppName = if ($AppNameBase64) { "
    "[System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String($AppNameBase64)) "
    "} else { '' }; "
    + _POWERSHELL_UIA_RESOLVE_WINDOW
    + _POWERSHELL_UIA_TYPES_COND
    + "$winTitle = if ($win -ne $null) { "
    "try { [string]$win.Current.Name } catch { '' } } else { '' }; "
    "$winHwnd = if ($win -ne $null) { "
    "try { [long]$win.Current.NativeWindowHandle } catch { 0 } } else { 0 }; "
    "$items = New-Object System.Collections.ArrayList; "
    "if ($win -ne $null) { "
    "  $all = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, $typeCond); "
    "  foreach ($el in $all) { "
    "    try { "
    "      $cur = $el.Current; "
    "      if ($cur.IsOffscreen) { continue } "
    "      $r = $cur.BoundingRectangle; "
    "      if ([double]::IsInfinity($r.Width) -or [double]::IsInfinity($r.Height) "
    "-or [double]::IsNaN($r.Width) -or [double]::IsNaN($r.Height) "
    "-or $r.Width -le 0 -or $r.Height -le 0) { continue } "
    "      $bx = [int][Math]::Round($r.Left); "
    "      $by = [int][Math]::Round($r.Top); "
    "      $bw = [int][Math]::Round($r.Width); "
    "      $bh = [int][Math]::Round($r.Height); "
    "      if ($bw -le 0 -or $bh -le 0) { continue } "
    "      $ctName = ($cur.ControlType.ProgrammaticName -replace '^ControlType\\.', ''); "
    "      $isEnabled = [bool]$cur.IsEnabled; "
    "      $isFocused = [bool]$cur.HasKeyboardFocus; "
    "      if ($InteractiveOnly -ne 0 -and ($ctName -eq 'Text' -or -not $isEnabled)) { continue } "
    "      $elName = [string]$cur.Name; "
    "      $elAid = [string]$cur.AutomationId; "
    "      $elVal = $null; "
    "      $vp = $null; "
    "      try { "
    "        if ($el.TryGetCurrentPattern([System.Windows.Automation.ValuePattern]::Pattern, "
    "[ref]$vp)) { "
    "          $elVal = [string]$vp.Current.Value "
    "        } "
    "      } catch { $elVal = $null } "
    "      if ($elVal -eq $null) { "
    "        $rvp = $null; "
    "        try { "
    "          if ($el.TryGetCurrentPattern("
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
    "[int]$CenterX = 0, [int]$CenterY = 0, [string]$ValueBase64 = '') "
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
    + _POWERSHELL_UIA_TYPES_COND
    + "$searchCond = $typeCond; "
    "if ($ControlType -ne '') { "
    "  $ctProp = $null; "
    "  try { $ctProp = [System.Windows.Automation.ControlType]::$ControlType } "
    "catch { $ctProp = $null }; "
    "  if ($ctProp -ne $null) { "
    "    $searchCond = New-Object System.Windows.Automation.PropertyCondition("
    "[System.Windows.Automation.AutomationElement]::ControlTypeProperty, $ctProp) "
    "  } "
    "} "
    "$chosen = $null; $bestScore = -1; $bestDist = [int]::MaxValue; "
    "$chosenCx = $CenterX; $chosenCy = $CenterY; "
    "if ($win -ne $null) { "
    "  $all = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, $searchCond); "
    "  foreach ($el in $all) { "
    "    try { "
    "      $cur = $el.Current; "
    "      if ($cur.IsOffscreen) { continue } "
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
    "    try { $chosen.SetFocus() } catch {} "
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
        if sent == 0:
            raise RuntimeError("Win32 SendInput failed to inject input events.")
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
    runner = powershell_runner or subprocess.run
    cmd = [
        ps_exe,
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        ps_command,
    ]
    try:
        proc = runner(
            cmd,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=UI_AUTOMATION_TIMEOUT_SECONDS,
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


def _send_unicode_text(
    u32: Any,
    text: str,
    *,
    interval_ms: int = 12,
    sleep_fn: Callable[[float], None] | None = None,
) -> int:
    """Send Unicode text via Win32 SendInput with clamped inter-character pacing."""
    if isinstance(interval_ms, bool) or not isinstance(interval_ms, int):
        raise ValueError("interval_ms must be an integer.")
    clamped_interval = max(0, min(200, interval_ms))
    if not text:
        return clamped_interval

    def _encode_char_events(chunk: str) -> list[_INPUT]:
        utf16_bytes = chunk.encode("utf-16-le")
        chunk_events: list[_INPUT] = []
        for idx in range(0, len(utf16_bytes), 2):
            code_unit = int.from_bytes(utf16_bytes[idx : idx + 2], "little")
            chunk_events.append(
                _INPUT(
                    type=INPUT_KEYBOARD,
                    union=_INPUT_UNION(
                        ki=_KEYBDINPUT(wVk=0, wScan=code_unit, dwFlags=KEYEVENTF_UNICODE)
                    ),
                )
            )
            chunk_events.append(
                _INPUT(
                    type=INPUT_KEYBOARD,
                    union=_INPUT_UNION(
                        ki=_KEYBDINPUT(
                            wVk=0,
                            wScan=code_unit,
                            dwFlags=KEYEVENTF_UNICODE | KEYEVENTF_KEYUP,
                        )
                    ),
                )
            )
        return chunk_events

    if clamped_interval == 0:
        _send_win32_inputs(u32, _encode_char_events(text))
        return clamped_interval

    sleeper = sleep_fn or time.sleep
    delay_s = clamped_interval / 1000.0
    last_idx = len(text) - 1
    for char_idx, ch in enumerate(text):
        _send_win32_inputs(u32, _encode_char_events(ch))
        if char_idx < last_idx:
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
) -> dict[str, Any]:
    """Capture a semantic UI Automation snapshot and assign compact ref IDs ('e1'..'eN')."""
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

    hwnd = _resolve_target_hwnd_optional(
        cleaned_app,
        powershell_runner=powershell_runner,
        user32=user32,
        kernel32=kernel32,
        window_enumerator=window_enumerator,
        focus_if_app_name=True,
    )

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

    llm_elements: list[dict[str, Any]] = []
    elements_by_ref: dict[str, dict[str, Any]] = {}

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

    snap_result = {
        "ok": True,
        "snapshot_id": snapshot_id,
        "window_title": window_title,
        "count": len(llm_elements),
        "element_count": len(llm_elements),
        "elements": llm_elements,
    }
    global _LAST_UI_SNAPSHOT, _LAST_UI_SNAPSHOT_TIME, _LAST_UI_WINDOW_TITLE
    _LAST_UI_SNAPSHOT = snap_result
    _LAST_UI_SNAPSHOT_TIME = time.time()
    _LAST_UI_WINDOW_TITLE = window_title

    return snap_result


def ui_act(
    ref: str,
    action: str = "click",
    value: str | None = None,
    interval_ms: int = 12,
    *,
    powershell_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
    user32: Any = None,
    sleep_fn: Callable[[float], None] | None = None,
    ui_snapshot_fn: Callable[..., dict[str, Any]] | None = None,
    gui_action_fn: Callable[..., dict[str, Any]] | None = None,
    _capture_snapshot: bool = True,
) -> dict[str, Any]:
    """Execute a semantic action on an indexed UI element and return verification diff."""
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError("ref must be a non-empty element reference (e.g. 'e1' or '@e1').")
    norm_ref = _normalize_ref_id(ref)

    if not isinstance(action, str) or not action.strip():
        raise ValueError("action must be a non-empty string.")
    norm_action = action.strip().lower()
    if norm_action not in VALID_UI_ACT_ACTIONS:
        raise ValueError(
            f"Unsupported UI act action {action!r}. Unsupported ui_act action; "
            f"must be one of {sorted(VALID_UI_ACT_ACTIONS)}."
        )

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

    snap_runner = ui_snapshot_fn or ui_snapshot
    action_runner = gui_action_fn or gui_action

    global _LAST_UI_SNAPSHOT, _LAST_UI_SNAPSHOT_TIME, _LAST_UI_WINDOW_TITLE
    if not _UI_REF_STORE and ui_snapshot_fn is not None:
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

    method = ""
    if gui_action_fn is not None:
        if norm_action == "click":
            action_runner(
                action="click_at",
                x=target_cx,
                y=target_cy,
                clicks=1,
                button="left",
                user32=user32,
            )
            method = "click_at"
        elif norm_action == "double_click":
            action_runner(
                action="click_at",
                x=target_cx,
                y=target_cy,
                clicks=2,
                button="double",
                user32=user32,
            )
            method = "double_click"
        elif norm_action == "right_click":
            action_runner(
                action="click_at",
                x=target_cx,
                y=target_cy,
                clicks=1,
                button="right",
                user32=user32,
            )
            method = "right_click"
        elif norm_action == "clear":
            action_runner(
                action="click_at",
                x=target_cx,
                y=target_cy,
                clicks=1,
                button="left",
                user32=user32,
            )
            action_runner(action="press_keys", keys=["ctrl", "a"], user32=user32)
            action_runner(action="press_keys", keys=["backspace"], user32=user32)
            method = "clear"
        elif norm_action == "type":
            assert value is not None
            action_runner(
                action="click_at",
                x=target_cx,
                y=target_cy,
                clicks=1,
                button="left",
                user32=user32,
            )
            try:
                action_runner(
                    action="type_text",
                    text=value,
                    interval_ms=clamped_interval,
                    user32=user32,
                )
            except TypeError:
                action_runner(action="type_text", text=value, user32=user32)
            method = "type"
        elif norm_action == "set_value":
            val_str = str(value) if value is not None else ""
            action_runner(
                action="click_at",
                x=target_cx,
                y=target_cy,
                clicks=1,
                button="left",
                user32=user32,
            )
            action_runner(action="press_keys", keys=["ctrl", "a"], user32=user32)
            action_runner(action="press_keys", keys=["backspace"], user32=user32)
            if val_str:
                try:
                    action_runner(
                        action="type_text",
                        text=val_str,
                        interval_ms=clamped_interval,
                        user32=user32,
                    )
                except TypeError:
                    action_runner(action="type_text", text=val_str, user32=user32)
            method = "set_value"
    else:
        acted_via_uia = False
        if norm_action in {"click", "set_value"} and (
            powershell_runner is not None or shutil.which("powershell.exe") is not None
        ):
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
            ps_command = (
                f"& {{ {_POWERSHELL_UI_ACT_SCRIPT} }} "
                f"-TargetHwnd {snapshot_hwnd} -WindowTitleBase64 '{title_b64}' "
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
                acted_via_uia = True
                method = str(
                    data.get("method")
                    or ("value_pattern" if norm_action == "set_value" else "invoke")
                )

        if not acted_via_uia:
            u32 = _get_win32_user32(user32)
            _ensure_window_foreground(snapshot_hwnd, u32)

            if norm_action == "click":
                _perform_mouse_click(target_cx, target_cy, clicks=1, button="left", user32=u32)
                method = "win32_click"
            elif norm_action == "double_click":
                _perform_mouse_click(target_cx, target_cy, clicks=1, button="double", user32=u32)
                method = "win32_double_click"
            elif norm_action == "right_click":
                _perform_mouse_click(target_cx, target_cy, clicks=1, button="right", user32=u32)
                method = "win32_right_click"
            elif norm_action == "set_value":
                assert value is not None
                _perform_mouse_click(target_cx, target_cy, clicks=1, button="left", user32=u32)
                _send_key_chord(u32, ["ctrl", "a"])
                _send_key_chord(u32, ["backspace"])
                if value:
                    _send_unicode_text(u32, value, interval_ms=clamped_interval, sleep_fn=sleep_fn)
                method = "keyboard_fallback"
            elif norm_action == "type":
                assert value is not None
                _perform_mouse_click(target_cx, target_cy, clicks=1, button="left", user32=u32)
                _send_unicode_text(u32, value, interval_ms=clamped_interval, sleep_fn=sleep_fn)
                method = "win32_type"
            else:  # "clear"
                _perform_mouse_click(target_cx, target_cy, clicks=1, button="left", user32=u32)
                _send_key_chord(u32, ["ctrl", "a"])
                _send_key_chord(u32, ["backspace"])
                method = "win32_clear"

    if not _capture_snapshot:
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

    after_snap: dict[str, Any] = {}
    is_fake_user32 = (
        hasattr(user32, "sent_inputs")
        or hasattr(user32, "cursor_moves")
        or hasattr(user32, "key_events")
    )
    if ui_snapshot_fn is not None:
        after_snap = ui_snapshot_fn(user32=user32)
        set_ui_snapshot_state(after_snap)
    elif (
        powershell_runner is None
        and shutil.which("powershell.exe") is not None
        and not is_fake_user32
    ):
        with contextlib.suppress(Exception):
            after_snap = snap_runner(user32=user32)
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

    ps_exe = _resolve_powershell_exe(powershell_runner)
    runner = powershell_runner or subprocess.run
    app_b64 = base64.b64encode(cleaned_app.encode("utf-8")).decode("ascii") if cleaned_app else ""
    cmd = [
        ps_exe,
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        (
            f"& {{ {_POWERSHELL_INSPECT_UI_SCRIPT} }} "
            f"-TargetHwnd {hwnd} -MaxElements {clamped_max} -AppNameBase64 '{app_b64}'"
        ),
    ]
    try:
        proc = runner(
            cmd,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=UI_AUTOMATION_TIMEOUT_SECONDS,
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

    return {
        "ok": True,
        "window_title": window_title,
        "count": len(elements),
        "elements": elements,
    }


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

    hwnd = _resolve_target_hwnd_optional(
        cleaned_app,
        powershell_runner=powershell_runner,
        user32=user32,
        kernel32=kernel32,
        window_enumerator=window_enumerator,
        focus_if_app_name=True,
    )

    ps_exe = _resolve_powershell_exe(powershell_runner)
    runner = powershell_runner or subprocess.run
    name_b64 = base64.b64encode(target_name.encode("utf-8")).decode("ascii")
    app_b64 = base64.b64encode(cleaned_app.encode("utf-8")).decode("ascii") if cleaned_app else ""
    ct_arg = cleaned_ct or ""
    cmd = [
        ps_exe,
        "-NoProfile",
        "-NonInteractive",
        "-Command",
        (
            f"& {{ {_POWERSHELL_CLICK_UI_SCRIPT} }} "
            f"-TargetHwnd {hwnd} -TargetNameBase64 '{name_b64}' "
            f"-ControlTypeFilter '{ct_arg}' -AppNameBase64 '{app_b64}'"
        ),
    ]
    try:
        proc = runner(
            cmd,
            shell=False,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=UI_AUTOMATION_TIMEOUT_SECONDS,
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
    x: int | None = None,
    y: int | None = None,
    text: str | None = None,
    keys: list[str] | None = None,
    clicks: int = 1,
    button: str = "left",
    delta: int = 0,
    seconds: float = 1.0,
    interval_ms: int = 12,
    *,
    user32: Any = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> dict[str, Any]:
    """Perform a safety-bounded low-level GUI input action via Win32 ctypes."""
    if not isinstance(action, str):
        raise ValueError("action must be a string.")
    normalized_action = action.strip().lower()
    if normalized_action not in VALID_GUI_ACTIONS:
        raise ValueError(
            f"Unsupported GUI action {action!r}. Must be one of {sorted(VALID_GUI_ACTIONS)}."
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

    u32 = _get_win32_user32(user32)

    if normalized_action == "click_at":
        if x is None or y is None or isinstance(x, bool) or isinstance(y, bool):
            raise ValueError("x and y integer coordinates are required for 'click_at'.")
        if not isinstance(x, int) or not isinstance(y, int) or x < 0 or y < 0:
            raise ValueError("x and y must be non-negative integers for 'click_at'.")
        if isinstance(clicks, bool) or not isinstance(clicks, int) or clicks < 1 or clicks > 3:
            raise ValueError("clicks must be an integer between 1 and 3.")
        btn = button.strip().lower() if isinstance(button, str) else ""
        if btn not in VALID_CLICK_BUTTONS:
            raise ValueError(
                f"Unsupported button {button!r}. Must be one of {sorted(VALID_CLICK_BUTTONS)}."
            )
        _perform_mouse_click(x, y, clicks=clicks, button=btn, user32=u32)
        actual_clicks = 2 if btn == "double" and clicks == 1 else clicks
        return {
            "ok": True,
            "status": "completed",
            "action": "click_at",
            "x": x,
            "y": y,
            "clicks": actual_clicks,
            "button": btn,
        }

    if normalized_action == "type_text":
        if not isinstance(text, str) or len(text) == 0:
            raise ValueError("text must be a non-empty string for 'type_text'.")
        if len(text) > MAX_GUI_TYPE_TEXT_LEN:
            raise ValueError(
                f"text length ({len(text)}) exceeds maximum allowed ({MAX_GUI_TYPE_TEXT_LEN})."
            )
        clamped_interval = _send_unicode_text(
            u32,
            text,
            interval_ms=interval_ms,
            sleep_fn=sleep_fn,
        )
        return {
            "ok": True,
            "status": "completed",
            "action": "type_text",
            "char_count": len(text),
            "interval_ms": clamped_interval,
        }

    if normalized_action == "press_keys":
        normalized_keys = _send_key_chord(u32, keys or [])
        return {
            "ok": True,
            "status": "completed",
            "action": "press_keys",
            "keys": normalized_keys,
        }

    # normalized_action == "scroll"
    if isinstance(delta, bool) or not isinstance(delta, int) or delta == 0:
        raise ValueError("delta must be a non-zero integer for 'scroll' action.")
    if (x is None) != (y is None):
        raise ValueError("Both x and y must be provided together for 'scroll'.")
    if x is not None and y is not None:
        if (
            isinstance(x, bool)
            or isinstance(y, bool)
            or not isinstance(x, int)
            or not isinstance(y, int)
        ):
            raise ValueError("x and y must be integers for 'scroll'.")
        if x < 0 or y < 0:
            raise ValueError("x and y must be non-negative integers for 'scroll'.")
        if hasattr(u32, "SetCursorPos"):
            moved = u32.SetCursorPos(x, y)
            if moved is not None and not moved:
                raise RuntimeError(f"Win32 SetCursorPos({x}, {y}) failed.")

    clamped_delta = max(-12000, min(12000, delta))
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
        "x": x,
        "y": y,
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
            x = s_dict.get("x")
            y = s_dict.get("y")
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

    is_stale = (time.time() - _LAST_UI_SNAPSHOT_TIME) > 60.0
    if not _UI_REF_STORE or _LAST_UI_SNAPSHOT is None or is_stale:
        before_snapshot = snap_runner(user32=user32)
        set_ui_snapshot_state(before_snapshot)
    else:
        before_snapshot = _LAST_UI_SNAPSHOT

    initial_title = str(before_snapshot.get("window_title") or "")
    if not initial_title:
        with contextlib.suppress(Exception):
            initial_title = title_getter()

    step_results: list[dict[str, Any]] = []
    status = "completed"

    for idx, s in enumerate(steps):
        s_dict = s.model_dump(exclude_unset=True) if hasattr(s, "model_dump") else dict(s)
        op = str(s_dict.get("op") or "").strip().lower()
        trigger_nav = False

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
                    "_capture_snapshot": False,
                }
                if step_interval is not None and not isinstance(step_interval, bool):
                    with contextlib.suppress(TypeError, ValueError):
                        act_kwargs["interval_ms"] = int(step_interval)
                ui_act(
                    ref,
                    action,
                    val,
                    **act_kwargs,
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
                action_runner(action="press_keys", keys=keys, user32=user32)
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
                action_runner(action="scroll", delta=delta, x=x, y=y, user32=user32)
                step_results.append(
                    {
                        "step": idx,
                        "op": "scroll",
                        "status": "ok",
                    }
                )

            elif op == "wait":
                sec = float(s_dict.get("seconds", 0.5))
                action_runner(action="wait", seconds=sec, user32=user32)
                step_results.append(
                    {
                        "step": idx,
                        "op": "wait",
                        "status": "ok",
                    }
                )

            elif op == "click_at":
                x = int(s_dict["x"])
                y = int(s_dict["y"])
                clicks = int(s_dict.get("clicks", 1))
                btn = str(s_dict.get("button", "left"))
                action_runner(action="click_at", x=x, y=y, clicks=clicks, button=btn, user32=user32)
                step_results.append(
                    {
                        "step": idx,
                        "op": "click_at",
                        "status": "ok",
                    }
                )
                trigger_nav = True

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
            current_title = ""
            with contextlib.suppress(Exception):
                current_title = title_getter()
            if current_title and current_title != initial_title:
                status = "stopped_on_window_change"
                break

    if status == "completed" and any(r.get("status") == "error" for r in step_results):
        status = "partial_error"

    after_snapshot = snap_runner(user32=user32)
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

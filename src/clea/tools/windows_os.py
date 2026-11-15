"""Safe Tier-1 Windows OS tools: app launch/focus, media/volume, and clipboard access.

All subprocess invocations strictly enforce ``shell=False``. All Win32 ctypes
functions set explicit ``argtypes`` and ``restype`` (using ``c_void_p`` for
64-bit memory handles/pointers) and accept optional injected API doubles for
deterministic unit testing.
"""

from __future__ import annotations

import contextlib
import ctypes
import difflib
import json
import logging
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
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
    resolved = Path(expanded).expanduser().resolve()
    if not resolved.exists():
        raise ValueError(f"target_path does not exist: {target_path!r}")
    return resolved


_INSTALLED_APPS_CACHE: list[dict[str, str]] | None = None
_INSTALLED_APPS_CACHE_TIME: float = 0.0
_APP_CACHE_TTL: float = 300.0  # 5 minutes


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
                low_name = name.lower()
                low_appid = appid.lower()
                if any(dis in low_name or dis in low_appid for dis in _DISALLOWED_SHELL_BINARIES):
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


_POWERSHELL_READ_CLIPBOARD_HISTORY_SCRIPT = r"""
param(
    [int]$MaxItems = 10,
    [int]$MaxChars = 8000,
    [switch]$IncludeImageBytes
)

$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.Runtime.WindowsRuntime
Add-Type -AssemblyName System.Drawing

function Await-AsyncOp($asyncOp) {
    $asTaskGeneric = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.IsGenericMethodDefinition
    } | Select-Object -First 1
    if (-not $asTaskGeneric) {
        throw "Could not find AsTask generic method"
    }
    $targetType = $asyncOp.GetType().GetGenericArguments()[0]
    $asTask = $asTaskGeneric.MakeGenericMethod($targetType)
    $task = $asTask.Invoke($null, @($asyncOp))
    $task.Wait(5000)
    return $task.Result
}

$historyEnabled = $false
try {
    $clip = [Windows.ApplicationModel.DataTransfer.Clipboard,
        Windows.ApplicationModel.DataTransfer, ContentType = WindowsRuntime]
    $historyEnabled = $clip::IsHistoryEnabled()
} catch {
    $historyEnabled = $false
}

$historyOp = [Windows.ApplicationModel.DataTransfer.Clipboard,
    Windows.ApplicationModel.DataTransfer, ContentType = WindowsRuntime]::GetHistoryItemsAsync()
$historyResult = Await-AsyncOp $historyOp

$rawItems = $historyResult.Items
$totalItems = 0
if ($rawItems) {
    $totalItems = $rawItems.Count
}

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
            $text = Await-AsyncOp $textOp
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
                $streamRef = Await-AsyncOp $bitmapOp
                if ($streamRef) {
                    $streamOp = $streamRef.OpenReadAsync()
                    $raStream = Await-AsyncOp $streamOp
                    $streamExt = [System.IO.WindowsRuntimeStreamExtensions]
                    $netStream = $streamExt::AsStreamForRead($raStream)
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

_POWERSHELL_SELECT_CLIPBOARD_ITEM_SCRIPT = r"""
param(
    [int]$ItemIndex
)

$ErrorActionPreference = 'Stop'

Add-Type -AssemblyName System.Runtime.WindowsRuntime

function Await-AsyncOp($asyncOp) {
    $asTaskGeneric = [System.WindowsRuntimeSystemExtensions].GetMethods() | Where-Object {
        $_.Name -eq 'AsTask' -and $_.GetParameters().Count -eq 1 -and $_.IsGenericMethodDefinition
    } | Select-Object -First 1
    if (-not $asTaskGeneric) {
        throw "Could not find AsTask generic method"
    }
    $targetType = $asyncOp.GetType().GetGenericArguments()[0]
    $asTask = $asTaskGeneric.MakeGenericMethod($targetType)
    $task = $asTask.Invoke($null, @($asyncOp))
    $task.Wait(5000)
    return $task.Result
}

$historyOp = [Windows.ApplicationModel.DataTransfer.Clipboard,
    Windows.ApplicationModel.DataTransfer, ContentType = WindowsRuntime]::GetHistoryItemsAsync()
$historyResult = Await-AsyncOp $historyOp

if (-not $historyResult.Items -or $ItemIndex -lt 0 -or $ItemIndex -ge $historyResult.Items.Count) {
    $cnt = 0
    if ($historyResult.Items) { $cnt = $historyResult.Items.Count }
    throw "Index $ItemIndex is out of range (history items count: $cnt)"
}

$targetItem = $historyResult.Items[$ItemIndex]
$status = [Windows.ApplicationModel.DataTransfer.Clipboard,
    Windows.ApplicationModel.DataTransfer,
    ContentType = WindowsRuntime]::SetHistoryItemAsContent($targetItem)

$successEnum = [Windows.ApplicationModel.DataTransfer.SetHistoryItemAsContentStatus]::Success
$out = [ordered]@{
    success = ($status -eq $successEnum)
    status = $status.ToString()
    index = $ItemIndex
    id = [string]$targetItem.Id
}

$out | ConvertTo-Json -Compress
"""


def _read_clipboard_win32(
    cap: int,
    *,
    user32: Any = None,
    kernel32: Any = None,
) -> dict[str, Any]:
    u32 = _get_win32_user32(user32)
    k32 = _get_win32_kernel32(kernel32)

    with _open_clipboard(u32):
        if hasattr(u32, "IsClipboardFormatAvailable") and not u32.IsClipboardFormatAvailable(
            CF_UNICODETEXT
        ):
            return {
                "ok": True,
                "text": "",
                "truncated": False,
                "char_count": 0,
                "total_chars": 0,
            }

        handle = u32.GetClipboardData(CF_UNICODETEXT)
        if not handle:
            return {
                "ok": True,
                "text": "",
                "truncated": False,
                "char_count": 0,
                "total_chars": 0,
            }

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

    When ``include_history=False`` (default), reads active clipboard text via classic
    Win32 APIs. When ``include_history=True``, queries WinRT Clipboard History via
    PowerShell 5.1, wrapping format checks in try/catch and falling back to Win32 on error.
    """
    if isinstance(max_chars, bool):
        raise ValueError("max_chars must be an integer.")
    try:
        cap = int(max_chars)
    except (TypeError, ValueError) as exc:
        raise ValueError("max_chars must be an integer.") from exc
    cap = max(1, min(cap, MAX_CLIPBOARD_CHARS))

    if not include_history:
        return _read_clipboard_win32(cap, user32=user32, kernel32=kernel32)

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

    if winrt_success and items:
        top_item = items[0]
        if top_item.get("kind") == "text" and top_item.get("text") is not None:
            text = top_item["text"]
            truncated = bool(top_item.get("truncated", False))
            total_chars = len(text)
            top_image = None
        else:
            text = ""
            truncated = False
            total_chars = 0
            top_image = top_item.get("image")
    else:
        win32_res = _read_clipboard_win32(cap, user32=user32, kernel32=kernel32)
        text = win32_res["text"]
        truncated = win32_res["truncated"]
        total_chars = win32_res["total_chars"]
        top_image = None

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
    if top_image is not None:
        res["image"] = top_image
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

    if lower_stripped in _SITE_SHORTCUTS:
        target_url = _SITE_SHORTCUTS[lower_stripped]
    elif "://" not in stripped and not stripped.startswith(("//", "\\\\")):
        target_url = f"https://{stripped}"
    else:
        target_url = stripped

    parsed = urllib.parse.urlsplit(target_url)
    scheme = parsed.scheme.lower()
    if scheme not in _ALLOWED_URL_SCHEMES:
        raise ValueError(
            f"Invalid URL scheme '{parsed.scheme}'. Only http and https URLs are allowed."
        )

    if not parsed.netloc:
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
    user32: Any = None,
    kernel32: Any = None,
    window_enumerator: Callable[[], list[EnumeratedWindow]] | None = None,
    wmi_brightness_getter: Callable[[], int] | None = None,
    wmi_brightness_setter: Callable[[int], None] | None = None,
    startfile_fn: Callable[[str], Any] | None = None,
    popen_fn: Callable[..., Any] = subprocess.Popen,
    powershell_runner: Callable[..., subprocess.CompletedProcess[str]] | None = None,
) -> dict[str, Any]:
    """Control Windows display brightness, window state, screen lock, or settings navigation."""
    if not isinstance(action, str):
        raise ValueError("action must be a string.")
    normalized = action.strip().lower()
    if normalized not in VALID_SYSTEM_ACTIONS:
        actions_str = ", ".join(VALID_SYSTEM_ACTIONS)
        raise ValueError(
            f"Unsupported system action {action!r}. Valid actions: {actions_str}."
        )

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
        try:
            parsed_level = int(level)
        except (TypeError, ValueError) as exc:
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
            try:
                st = int(step)
            except (TypeError, ValueError) as exc:
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
        target_hwnd = _resolve_window_hwnd(
            hwnd=hwnd,
            app_name=app_name,
            title=title,
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

"""Windows ctypes integration for global hotkeys, DPI awareness, and window enumeration.

All ctypes calls explicitly set argtypes and restype.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from typing import Any

logger = logging.getLogger(__name__)

# Modifiers for RegisterHotKey
MOD_ALT = 0x0001
MOD_CONTROL = 0x0002
MOD_SHIFT = 0x0004
MOD_WIN = 0x0008
MOD_NOREPEAT = 0x4000

# Windows Message IDs
WM_HOTKEY = 0x0312
WM_QUIT = 0x0012

HOTKEY_ID = 0x0001

# DPI Awareness Constants
DEFAULT_DPI: int = 96
DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2: int = -4
PROCESS_PER_MONITOR_DPI_AWARE: int = 2

# Process Access Rights
PROCESS_QUERY_LIMITED_INFORMATION: int = 0x1000


@dataclass(frozen=True)
class WindowInfo:
    """Information for an enumerated top-level window."""

    hwnd: int
    title: str
    process_name: str
    is_visible: bool


def parse_hotkey(hotkey_str: str) -> tuple[int, int]:
    """Parse a hotkey string like 'Ctrl+Alt+V' into (modifiers, vk_code)."""
    parts = [p.strip().lower() for p in hotkey_str.split("+") if p.strip()]
    if not parts:
        raise ValueError(f"Invalid empty hotkey string: {hotkey_str!r}")

    modifiers = MOD_NOREPEAT
    key_part = ""

    for part in parts:
        if part in {"ctrl", "control"}:
            modifiers |= MOD_CONTROL
        elif part in {"alt"}:
            modifiers |= MOD_ALT
        elif part in {"shift"}:
            modifiers |= MOD_SHIFT
        elif part in {"win", "windows", "super"}:
            modifiers |= MOD_WIN
        else:
            if key_part:
                raise ValueError(f"Multiple primary keys specified in hotkey: {hotkey_str!r}")
            key_part = part

    if not key_part:
        raise ValueError(f"No key specified in hotkey: {hotkey_str!r}")

    # Resolve virtual key code
    if len(key_part) == 1:
        char = key_part.upper()
        if "A" <= char <= "Z" or "0" <= char <= "9":
            vk = ord(char)
        else:
            raise ValueError(f"Unsupported single-character hotkey: {key_part!r}")
    elif key_part == "space":
        vk = 0x20
    elif key_part.startswith("f") and key_part[1:].isdigit():
        f_num = int(key_part[1:])
        if 1 <= f_num <= 24:
            vk = 0x70 + (f_num - 1)
        else:
            raise ValueError(f"F-key out of range: {key_part!r}")
    else:
        raise ValueError(f"Unsupported key in hotkey: {key_part!r}")

    return modifiers, vk


# ------------------------------------------------------------- DPI and Coordinate Scaling


def dpi_to_scale_factor(dpi: int | float) -> float:
    """Convert DPI value to a scale factor relative to standard 96 DPI."""
    if dpi <= 0:
        raise ValueError(f"DPI must be positive, got {dpi}")
    return float(dpi) / float(DEFAULT_DPI)


def scale_coordinate(coord: int | float, scale_factor: float) -> int:
    """Scale a logical coordinate by the given DPI scale factor."""
    return int(round(coord * scale_factor))


def unscale_coordinate(coord: int | float, scale_factor: float) -> int:
    """Convert a physical coordinate back to logical units."""
    if scale_factor <= 0:
        raise ValueError(f"Scale factor must be positive, got {scale_factor}")
    return int(round(coord / scale_factor))


def scale_point(
    pt: tuple[int | float, int | float],
    scale_factor: float,
) -> tuple[int, int]:
    """Scale an (x, y) point by the given scale factor."""
    return (scale_coordinate(pt[0], scale_factor), scale_coordinate(pt[1], scale_factor))


def unscale_point(
    pt: tuple[int | float, int | float],
    scale_factor: float,
) -> tuple[int, int]:
    """Convert a physical (x, y) point back to logical units."""
    return (unscale_coordinate(pt[0], scale_factor), unscale_coordinate(pt[1], scale_factor))


def scale_rect(
    rect: tuple[int | float, int | float, int | float, int | float],
    scale_factor: float,
) -> tuple[int, int, int, int]:
    """Scale a (left, top, right, bottom) rectangle by the given scale factor."""
    return (
        scale_coordinate(rect[0], scale_factor),
        scale_coordinate(rect[1], scale_factor),
        scale_coordinate(rect[2], scale_factor),
        scale_coordinate(rect[3], scale_factor),
    )


def unscale_rect(
    rect: tuple[int | float, int | float, int | float, int | float],
    scale_factor: float,
) -> tuple[int, int, int, int]:
    """Convert a physical (left, top, right, bottom) rectangle back to logical units."""
    return (
        unscale_coordinate(rect[0], scale_factor),
        unscale_coordinate(rect[1], scale_factor),
        unscale_coordinate(rect[2], scale_factor),
        unscale_coordinate(rect[3], scale_factor),
    )


# --------------------------------------------------------------------------- Win32 API Wrapper


class Win32API:
    """Wrapper around user32, kernel32, and shcore DLLs with strict ctypes argtypes and restype."""

    def __init__(
        self,
        user32: Any = None,
        kernel32: Any = None,
        shcore: Any = None,
    ) -> None:
        if user32 is None or kernel32 is None:
            if sys.platform != "win32":
                raise OSError("Win32API requires Windows or mocked DLLs.")
            import ctypes.wintypes as wintypes

            u32 = ctypes.WinDLL("user32", use_last_error=True)
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)
            try:
                shc = ctypes.WinDLL("shcore", use_last_error=True)
            except OSError:
                shc = None

            # Hotkeys and message pump
            u32.RegisterHotKey.argtypes = [
                wintypes.HWND,
                ctypes.c_int,
                wintypes.UINT,
                wintypes.UINT,
            ]
            u32.RegisterHotKey.restype = wintypes.BOOL

            u32.UnregisterHotKey.argtypes = [wintypes.HWND, ctypes.c_int]
            u32.UnregisterHotKey.restype = wintypes.BOOL

            u32.GetMessageW.argtypes = [
                ctypes.POINTER(wintypes.MSG),
                wintypes.HWND,
                wintypes.UINT,
                wintypes.UINT,
            ]
            u32.GetMessageW.restype = wintypes.BOOL

            u32.TranslateMessage.argtypes = [ctypes.POINTER(wintypes.MSG)]
            u32.TranslateMessage.restype = wintypes.BOOL

            u32.DispatchMessageW.argtypes = [ctypes.POINTER(wintypes.MSG)]
            u32.DispatchMessageW.restype = ctypes.c_ssize_t

            u32.PostThreadMessageW.argtypes = [
                wintypes.DWORD,
                wintypes.UINT,
                wintypes.WPARAM,
                wintypes.LPARAM,
            ]
            u32.PostThreadMessageW.restype = wintypes.BOOL

            k32.GetCurrentThreadId.argtypes = []
            k32.GetCurrentThreadId.restype = wintypes.DWORD

            # DPI awareness
            if hasattr(u32, "SetProcessDpiAwarenessContext"):
                u32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
                u32.SetProcessDpiAwarenessContext.restype = wintypes.BOOL
            if hasattr(u32, "SetProcessDPIAware"):
                u32.SetProcessDPIAware.argtypes = []
                u32.SetProcessDPIAware.restype = wintypes.BOOL
            if hasattr(u32, "GetDpiForSystem"):
                u32.GetDpiForSystem.argtypes = []
                u32.GetDpiForSystem.restype = wintypes.UINT
            if hasattr(u32, "GetDpiForWindow"):
                u32.GetDpiForWindow.argtypes = [wintypes.HWND]
                u32.GetDpiForWindow.restype = wintypes.UINT
            if shc is not None and hasattr(shc, "SetProcessDpiAwareness"):
                shc.SetProcessDpiAwareness.argtypes = [ctypes.c_int]
                shc.SetProcessDpiAwareness.restype = ctypes.HRESULT

            # Window enumeration and inspection
            WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            u32.EnumWindows.argtypes = [WNDENUMPROC, wintypes.LPARAM]
            u32.EnumWindows.restype = wintypes.BOOL

            u32.IsWindowVisible.argtypes = [wintypes.HWND]
            u32.IsWindowVisible.restype = wintypes.BOOL

            u32.GetWindowTextLengthW.argtypes = [wintypes.HWND]
            u32.GetWindowTextLengthW.restype = ctypes.c_int

            u32.GetWindowTextW.argtypes = [wintypes.HWND, wintypes.LPWSTR, ctypes.c_int]
            u32.GetWindowTextW.restype = ctypes.c_int

            u32.GetWindowThreadProcessId.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            u32.GetWindowThreadProcessId.restype = wintypes.DWORD

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

            self.user32 = u32
            self.kernel32 = k32
            self.shcore = shc
        else:
            self.user32 = user32
            self.kernel32 = kernel32
            self.shcore = shcore


def enable_per_monitor_dpi_awareness(api: Win32API | None = None) -> bool:
    """Enable per-monitor DPI awareness at process start.

    Tries SetProcessDpiAwarenessContext (v2) -> SetProcessDpiAwareness (shcore)
    -> SetProcessDPIAware.
    Returns True if successfully set, False otherwise.
    """
    if api is None:
        if sys.platform != "win32":
            logger.debug("DPI awareness is only supported on Windows.")
            return False
        try:
            api = Win32API()
        except OSError:
            return False

    # Tier 1: Windows 10 Creators Update (1703)+ Per-Monitor V2
    if hasattr(api.user32, "SetProcessDpiAwarenessContext"):
        try:
            res = api.user32.SetProcessDpiAwarenessContext(
                ctypes.c_void_p(DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2)
            )
            if res:
                logger.debug(
                    "Enabled Per-Monitor V2 DPI awareness via SetProcessDpiAwarenessContext."
                )
                return True
        except Exception as exc:
            logger.debug("SetProcessDpiAwarenessContext failed: %s", exc)

    # Tier 2: Windows 8.1+ SetProcessDpiAwareness via shcore
    if api.shcore is not None and hasattr(api.shcore, "SetProcessDpiAwareness"):
        try:
            hr = api.shcore.SetProcessDpiAwareness(PROCESS_PER_MONITOR_DPI_AWARE)
            # S_OK = 0, E_ACCESSDENIED / already set = 0x80070005
            if hr == 0:
                logger.debug("Enabled Per-Monitor DPI awareness via shcore.SetProcessDpiAwareness.")
                return True
        except Exception as exc:
            logger.debug("shcore.SetProcessDpiAwareness failed: %s", exc)

    # Tier 3: Windows Vista+ SetProcessDPIAware via user32
    if hasattr(api.user32, "SetProcessDPIAware"):
        try:
            res = api.user32.SetProcessDPIAware()
            if res:
                logger.debug("Enabled system DPI awareness via SetProcessDPIAware.")
                return True
        except Exception as exc:
            logger.debug("SetProcessDPIAware failed: %s", exc)

    logger.warning("Could not enable DPI awareness on this platform.")
    return False


def get_window_title(hwnd: int, api: Win32API | None = None) -> str:
    """Retrieve the title text of a window."""
    if api is None:
        api = Win32API()

    length = int(api.user32.GetWindowTextLengthW(hwnd))
    if length <= 0:
        return ""

    buf = ctypes.create_unicode_buffer(length + 1)
    copied = api.user32.GetWindowTextW(hwnd, buf, length + 1)
    return buf.value if copied > 0 else ""


def get_window_process_name(hwnd: int, api: Win32API | None = None) -> str:
    """Retrieve process executable name for a window.

    If process cannot be opened (e.g. elevated window), returns '<unknown>'
    rather than raising or blocking.
    """
    if api is None:
        api = Win32API()

    import ctypes.wintypes as wintypes

    pid = wintypes.DWORD()
    api.user32.GetWindowThreadProcessId(hwnd, ctypes.byref(pid))
    if pid.value == 0:
        return "<unknown>"

    # Try opening process with limited query rights
    h_proc = api.kernel32.OpenProcess(PROCESS_QUERY_LIMITED_INFORMATION, False, pid.value)
    if not h_proc:
        # Access denied or process terminated (elevated window) -> treat as unknown
        logger.debug(
            "Could not open PID %d for HWND %s (elevated window); returning <unknown>.",
            pid.value,
            hwnd,
        )
        return "<unknown>"

    try:
        if hasattr(api.kernel32, "QueryFullProcessImageNameW"):
            buf = ctypes.create_unicode_buffer(1024)
            size = wintypes.DWORD(1024)
            if api.kernel32.QueryFullProcessImageNameW(h_proc, 0, buf, ctypes.byref(size)):
                return Path(buf.value).name
        return "<unknown>"
    finally:
        with contextlib.suppress(Exception):
            api.kernel32.CloseHandle(h_proc)


def enumerate_visible_windows(api: Win32API | None = None) -> list[WindowInfo]:
    """Enumerate visible top-level windows with titles and process names."""
    if api is None:
        api = Win32API()

    import ctypes.wintypes as wintypes

    results: list[WindowInfo] = []

    def enum_proc(hwnd: int, lparam: int) -> bool:
        if bool(api.user32.IsWindowVisible(hwnd)):
            title = get_window_title(hwnd, api)
            proc_name = get_window_process_name(hwnd, api)
            results.append(
                WindowInfo(
                    hwnd=hwnd,
                    title=title,
                    process_name=proc_name,
                    is_visible=True,
                )
            )
        return True

    WNDENUMPROC = ctypes.WINFUNCTYPE(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
    cb = WNDENUMPROC(enum_proc)
    api.user32.EnumWindows(cb, 0)
    return results


# --------------------------------------------------------------------------- Global HotKey Manager


class HotKeyManager:
    """Dedicated thread running a Win32 message pump for RegisterHotKey."""

    def __init__(self, api: Win32API | None = None) -> None:
        self._api = api
        self._thread: threading.Thread | None = None
        self._thread_id: int | None = None
        self._stop_event = threading.Event()
        self._init_event = threading.Event()
        self._init_error: Exception | None = None
        self._callback: Callable[[], None] | None = None

    def start(self, hotkey_str: str, on_triggered: Callable[[], None]) -> None:
        """Start the hotkey listener thread. Raises on registration failure."""
        modifiers, vk = parse_hotkey(hotkey_str)
        self._callback = on_triggered
        self._stop_event.clear()
        self._init_event.clear()
        self._init_error = None

        thread = threading.Thread(
            target=self._run_loop,
            args=(modifiers, vk),
            name="CleaVoiceHotKeyThread",
            daemon=True,
        )
        self._thread = thread
        thread.start()

        self._init_event.wait(timeout=3.0)
        if self._init_error is not None:
            raise self._init_error

    def _run_loop(self, modifiers: int, vk: int) -> None:
        try:
            if self._api is None:
                self._api = Win32API()
            thread_id = int(self._api.kernel32.GetCurrentThreadId())
            self._thread_id = thread_id

            reg_ok = bool(self._api.user32.RegisterHotKey(None, HOTKEY_ID, modifiers, vk))
            if not reg_ok:
                err = ctypes.get_last_error() if hasattr(ctypes, "get_last_error") else 0
                raise OSError(f"RegisterHotKey failed with error code {err}")

            self._init_event.set()
            logger.info("Win32 hotkey registered successfully (thread %d).", thread_id)
        except Exception as exc:
            self._init_error = exc
            self._init_event.set()
            return

        try:
            if sys.platform == "win32":
                import ctypes.wintypes as wintypes

                msg = wintypes.MSG()
                msg_ptr = ctypes.byref(msg)
            else:
                msg = None
                msg_ptr = None

            while not self._stop_event.is_set():
                res = self._api.user32.GetMessageW(msg_ptr, None, 0, 0)
                # GetMessage returns 0 on WM_QUIT, -1 on error
                if res in (0, -1, False):
                    break

                current_msg = getattr(msg, "message", 0) if msg else 0
                if current_msg == WM_HOTKEY:
                    logger.debug("Win32 WM_HOTKEY received.")
                    if self._callback is not None:
                        try:
                            self._callback()
                        except Exception:
                            logger.exception("Error in hotkey callback.")
                elif current_msg == WM_QUIT:
                    break

                self._api.user32.TranslateMessage(msg_ptr)
                self._api.user32.DispatchMessageW(msg_ptr)
        finally:
            with contextlib.suppress(Exception):
                self._api.user32.UnregisterHotKey(None, HOTKEY_ID)
            logger.info("Win32 hotkey unregistered.")

    def stop(self) -> None:
        """Post WM_QUIT to the hotkey message loop and wait for thread termination."""
        self._stop_event.set()
        if self._api is not None and self._thread_id is not None:
            try:
                self._api.user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)
            except Exception:
                pass
        if self._thread is not None and self._thread.is_alive():
            self._thread.join(timeout=2.0)
            self._thread = None
        self._thread_id = None

"""Windows ctypes integration for global hotkeys with dedicated message pump.

All ctypes calls explicitly set argtypes and restype.
"""

from __future__ import annotations

import contextlib
import ctypes
import logging
import sys
import threading
from collections.abc import Callable
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


class Win32API:
    """Wrapper around user32 and kernel32 DLLs with strict ctypes argtypes and restype."""

    def __init__(self, user32: Any = None, kernel32: Any = None) -> None:
        if user32 is None or kernel32 is None:
            if sys.platform != "win32":
                raise OSError("Win32API requires Windows or mocked DLLs.")
            import ctypes.wintypes as wintypes

            u32 = ctypes.WinDLL("user32", use_last_error=True)
            k32 = ctypes.WinDLL("kernel32", use_last_error=True)

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

            self.user32 = u32
            self.kernel32 = k32
        else:
            self.user32 = user32
            self.kernel32 = kernel32


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

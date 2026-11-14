"""Win32 click-through top-edge red indicator bar for active screen monitoring."""

from __future__ import annotations

import contextlib
import ctypes
import logging
import os
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Protocol

from clea.surfaces.voice.win32 import MonitorInfo, Win32API, get_target_monitor

logger = logging.getLogger(__name__)

# Visual defaults (Tailwind red-500 #EF4444)
DEFAULT_INDICATOR_COLOR_RGB: tuple[int, int, int] = (239, 68, 68)
DEFAULT_INDICATOR_HEIGHT_PX: int = 3
DEFAULT_PULSE_SECONDS: float = 1.5

# Win32 window & extended styles
WS_POPUP: int = 0x80000000
WS_EX_TOPMOST: int = 0x00000008
WS_EX_TRANSPARENT: int = 0x00000020
WS_EX_TOOLWINDOW: int = 0x00000080
WS_EX_LAYERED: int = 0x00080000
WS_EX_NOACTIVATE: int = 0x08000000

INDICATOR_EX_STYLE: int = (
    WS_EX_TOPMOST | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
)

# SetWindowPos flags & Z-order
HWND_TOPMOST: int = -1
SWP_NOSIZE: int = 0x0001
SWP_NOMOVE: int = 0x0002
SWP_NOZORDER: int = 0x0004
SWP_NOACTIVATE: int = 0x0010
SWP_SHOWWINDOW: int = 0x0040
SWP_HIDEWINDOW: int = 0x0080
SWP_NOOWNERZORDER: int = 0x0200

# ShowWindow commands
SW_HIDE: int = 0
SW_SHOWNOACTIVATE: int = 4

# Layered window & display affinity constants
LWA_ALPHA: int = 0x00000002
WDA_EXCLUDEFROMCAPTURE: int = 0x00000011

# Window messages
WM_DESTROY: int = 0x0002
WM_PAINT: int = 0x000F
WM_CLOSE: int = 0x0010
WM_QUIT: int = 0x0012
WM_ERASEBKGND: int = 0x0014
WM_MOUSEACTIVATE: int = 0x0021
WM_NCHITTEST: int = 0x0084
WM_TIMER: int = 0x0113
WM_APP: int = 0x8000
WM_APP_SYNC: int = WM_APP + 1

HTTRANSPARENT: int = -1
MA_NOACTIVATE: int = 3
PULSE_TIMER_ID: int = 1001
PULSE_TIMER_POLL_MS: int = 100


def rgb_to_colorref(rgb: tuple[int, int, int]) -> int:
    """Convert an (r, g, b) tuple to a Win32 COLORREF (0x00BBGGRR)."""
    r = max(0, min(255, int(rgb[0])))
    g = max(0, min(255, int(rgb[1])))
    b = max(0, min(255, int(rgb[2])))
    return (b << 16) | (g << 8) | r


def compute_bar_rect(
    monitor_bounds: tuple[int, int, int, int],
    height_px: int = DEFAULT_INDICATOR_HEIGHT_PX,
) -> tuple[int, int, int, int]:
    """Compute (x, y, width, height) for a top-edge bar spanning *monitor_bounds*."""
    left, top, right, bottom = monitor_bounds
    width = max(1, int(right) - int(left))
    max_h = max(1, int(bottom) - int(top))
    height = max(1, min(int(height_px), max_h))
    return (int(left), int(top), width, height)


@dataclass(frozen=True)
class IndicatorRect:
    """Positioned indicator geometry in virtual-screen coordinates."""

    x: int
    y: int
    width: int
    height: int


class _IndicatorBackend(Protocol):
    """Internal windowing backend executed exclusively on the indicator message thread."""

    @property
    def thread_id(self) -> int | None: ...

    @property
    def hwnd(self) -> int | None: ...

    def create(self, colorref: int, initial_rect: tuple[int, int, int, int]) -> None: ...

    def show(self, rect: tuple[int, int, int, int]) -> None: ...

    def hide(self) -> None: ...

    def pump_once(self) -> bool: ...

    def request_sync(self) -> None: ...

    def request_quit(self) -> None: ...

    def start_timer(self, interval_ms: int) -> None: ...

    def stop_timer(self) -> None: ...

    def destroy(self) -> None: ...


class _Win32OverlayBackend:
    """Real Win32 layered topmost click-through window backend."""

    def __init__(self) -> None:
        import ctypes.wintypes as wintypes

        self._wintypes = wintypes
        self._user32 = ctypes.WinDLL("user32", use_last_error=True)
        self._gdi32 = ctypes.WinDLL("gdi32", use_last_error=True)
        self._kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._configure_signatures()

        self._hwnd: int | None = None
        self._thread_id: int | None = None
        self._hinstance: int | None = None
        self._class_name: str = ""
        self._brush: int | None = None
        self._wndproc_ref: object | None = None
        self._shown: bool = False
        self._last_rect: tuple[int, int, int, int] | None = None
        self._timer_active: bool = False
        self._on_sync: Callable[[], None] | None = None

    def set_sync_callback(self, callback: Callable[[], None]) -> None:
        self._on_sync = callback

    def _configure_signatures(self) -> None:
        wt = self._wintypes
        lresult = ctypes.c_ssize_t
        u32 = self._user32
        g32 = self._gdi32
        k32 = self._kernel32

        k32.GetCurrentThreadId.argtypes = []
        k32.GetCurrentThreadId.restype = wt.DWORD

        k32.GetModuleHandleW.argtypes = [wt.LPCWSTR]
        k32.GetModuleHandleW.restype = wt.HMODULE

        g32.CreateSolidBrush.argtypes = [wt.DWORD]
        g32.CreateSolidBrush.restype = wt.HBRUSH

        g32.DeleteObject.argtypes = [wt.HGDIOBJ]
        g32.DeleteObject.restype = wt.BOOL

        u32.DefWindowProcW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        u32.DefWindowProcW.restype = lresult

        u32.UnregisterClassW.argtypes = [wt.LPCWSTR, wt.HINSTANCE]
        u32.UnregisterClassW.restype = wt.BOOL

        u32.CreateWindowExW.argtypes = [
            wt.DWORD,
            wt.LPCWSTR,
            wt.LPCWSTR,
            wt.DWORD,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wt.HWND,
            wt.HMENU,
            wt.HINSTANCE,
            wt.LPVOID,
        ]
        u32.CreateWindowExW.restype = wt.HWND

        u32.DestroyWindow.argtypes = [wt.HWND]
        u32.DestroyWindow.restype = wt.BOOL

        u32.SetLayeredWindowAttributes.argtypes = [wt.HWND, wt.DWORD, wt.BYTE, wt.DWORD]
        u32.SetLayeredWindowAttributes.restype = wt.BOOL

        if hasattr(u32, "SetWindowDisplayAffinity"):
            u32.SetWindowDisplayAffinity.argtypes = [wt.HWND, wt.DWORD]
            u32.SetWindowDisplayAffinity.restype = wt.BOOL

        u32.SetWindowPos.argtypes = [
            wt.HWND,
            wt.HWND,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wt.UINT,
        ]
        u32.SetWindowPos.restype = wt.BOOL

        u32.ShowWindow.argtypes = [wt.HWND, ctypes.c_int]
        u32.ShowWindow.restype = wt.BOOL

        u32.InvalidateRect.argtypes = [wt.HWND, ctypes.c_void_p, wt.BOOL]
        u32.InvalidateRect.restype = wt.BOOL

        u32.FillRect.argtypes = [wt.HDC, ctypes.POINTER(wt.RECT), wt.HBRUSH]
        u32.FillRect.restype = ctypes.c_int

        u32.GetClientRect.argtypes = [wt.HWND, ctypes.POINTER(wt.RECT)]
        u32.GetClientRect.restype = wt.BOOL

        u32.SetTimer.argtypes = [wt.HWND, ctypes.c_size_t, wt.UINT, ctypes.c_void_p]
        u32.SetTimer.restype = ctypes.c_size_t

        u32.KillTimer.argtypes = [wt.HWND, ctypes.c_size_t]
        u32.KillTimer.restype = wt.BOOL

        u32.GetMessageW.argtypes = [
            ctypes.POINTER(wt.MSG),
            wt.HWND,
            wt.UINT,
            wt.UINT,
        ]
        u32.GetMessageW.restype = wt.BOOL

        u32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
        u32.TranslateMessage.restype = wt.BOOL

        u32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
        u32.DispatchMessageW.restype = lresult

        u32.PostMessageW.argtypes = [wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM]
        u32.PostMessageW.restype = wt.BOOL

        u32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
        u32.PostThreadMessageW.restype = wt.BOOL

        u32.PostQuitMessage.argtypes = [ctypes.c_int]
        u32.PostQuitMessage.restype = None

    @property
    def thread_id(self) -> int | None:
        return self._thread_id

    @property
    def hwnd(self) -> int | None:
        return self._hwnd

    def create(self, colorref: int, initial_rect: tuple[int, int, int, int]) -> None:
        wt = self._wintypes
        lresult = ctypes.c_ssize_t
        self._thread_id = int(self._kernel32.GetCurrentThreadId())
        self._hinstance = self._kernel32.GetModuleHandleW(None)
        self._brush = self._gdi32.CreateSolidBrush(colorref)
        self._class_name = f"CleaScreenIndicator_{os.getpid()}_{id(self)}"

        wndproc_type = ctypes.WINFUNCTYPE(lresult, wt.HWND, wt.UINT, wt.WPARAM, wt.LPARAM)

        class _WNDCLASSEXW(ctypes.Structure):
            _fields_ = [
                ("cbSize", wt.UINT),
                ("style", wt.UINT),
                ("lpfnWndProc", wndproc_type),
                ("cbClsExtra", ctypes.c_int),
                ("cbWndExtra", ctypes.c_int),
                ("hInstance", wt.HINSTANCE),
                ("hIcon", wt.HICON),
                ("hCursor", wt.HANDLE),
                ("hbrBackground", wt.HBRUSH),
                ("lpszMenuName", wt.LPCWSTR),
                ("lpszClassName", wt.LPCWSTR),
                ("hIconSm", wt.HICON),
            ]

        class _PAINTSTRUCT(ctypes.Structure):
            _fields_ = [
                ("hdc", wt.HDC),
                ("fErase", wt.BOOL),
                ("rcPaint", wt.RECT),
                ("fRestore", wt.BOOL),
                ("fIncUpdate", wt.BOOL),
                ("rgbReserved", ctypes.c_byte * 32),
            ]

        self._user32.BeginPaint.argtypes = [wt.HWND, ctypes.POINTER(_PAINTSTRUCT)]
        self._user32.BeginPaint.restype = wt.HDC
        self._user32.EndPaint.argtypes = [wt.HWND, ctypes.POINTER(_PAINTSTRUCT)]
        self._user32.EndPaint.restype = wt.BOOL

        def _wnd_proc(hwnd: int, msg: int, wparam: int, lparam: int) -> int:
            if msg == WM_NCHITTEST:
                return HTTRANSPARENT
            if msg == WM_MOUSEACTIVATE:
                return MA_NOACTIVATE
            if msg == WM_ERASEBKGND:
                if wparam and self._brush:
                    rc = wt.RECT()
                    if self._user32.GetClientRect(hwnd, ctypes.byref(rc)):
                        self._user32.FillRect(wparam, ctypes.byref(rc), self._brush)
                return 1
            if msg == WM_PAINT:
                ps = _PAINTSTRUCT()
                hdc = self._user32.BeginPaint(hwnd, ctypes.byref(ps))
                if hdc and self._brush:
                    self._user32.FillRect(hdc, ctypes.byref(ps.rcPaint), self._brush)
                self._user32.EndPaint(hwnd, ctypes.byref(ps))
                return 0
            if msg in (WM_APP_SYNC, WM_TIMER):
                if self._on_sync is not None:
                    self._on_sync()
                return 0
            if msg == WM_CLOSE:
                self._user32.DestroyWindow(hwnd)
                return 0
            if msg == WM_DESTROY:
                self._user32.PostQuitMessage(0)
                return 0
            return int(self._user32.DefWindowProcW(hwnd, msg, wparam, lparam))

        wndproc_cb = wndproc_type(_wnd_proc)
        self._wndproc_ref = wndproc_cb

        self._user32.RegisterClassExW.argtypes = [ctypes.POINTER(_WNDCLASSEXW)]
        self._user32.RegisterClassExW.restype = wt.ATOM

        wc = _WNDCLASSEXW()
        wc.cbSize = ctypes.sizeof(_WNDCLASSEXW)
        wc.style = 0
        wc.lpfnWndProc = wndproc_cb
        wc.hInstance = self._hinstance
        wc.hbrBackground = self._brush
        wc.lpszClassName = self._class_name

        atom = self._user32.RegisterClassExW(ctypes.byref(wc))
        if not atom:
            err = ctypes.get_last_error()
            raise OSError(f"RegisterClassExW failed with error code {err}")

        x, y, width, height = initial_rect
        hwnd = self._user32.CreateWindowExW(
            INDICATOR_EX_STYLE,
            self._class_name,
            "Clea Screen Monitor Indicator",
            WS_POPUP,
            x,
            y,
            width,
            height,
            None,
            None,
            self._hinstance,
            None,
        )
        if not hwnd:
            err = ctypes.get_last_error()
            with contextlib.suppress(Exception):
                self._user32.UnregisterClassW(self._class_name, self._hinstance)
            raise OSError(f"CreateWindowExW failed with error code {err}")

        self._hwnd = int(hwnd)
        self._user32.SetLayeredWindowAttributes(self._hwnd, 0, 255, LWA_ALPHA)
        if hasattr(self._user32, "SetWindowDisplayAffinity"):
            ok = bool(self._user32.SetWindowDisplayAffinity(self._hwnd, WDA_EXCLUDEFROMCAPTURE))
            if not ok:
                logger.debug(
                    "SetWindowDisplayAffinity(WDA_EXCLUDEFROMCAPTURE) returned False (err=%s).",
                    ctypes.get_last_error(),
                )

    def show(self, rect: tuple[int, int, int, int]) -> None:
        if not self._hwnd:
            return
        x, y, width, height = rect
        if self._shown and self._last_rect == rect:
            return
        flags = SWP_NOACTIVATE | SWP_SHOWWINDOW | SWP_NOOWNERZORDER
        self._user32.SetWindowPos(self._hwnd, HWND_TOPMOST, x, y, width, height, flags)
        self._user32.ShowWindow(self._hwnd, SW_SHOWNOACTIVATE)
        self._user32.InvalidateRect(self._hwnd, None, True)
        self._shown = True
        self._last_rect = rect

    def hide(self) -> None:
        if not self._hwnd or not self._shown:
            return
        self._user32.ShowWindow(self._hwnd, SW_HIDE)
        self._shown = False

    def pump_once(self) -> bool:
        wt = self._wintypes
        msg = wt.MSG()
        res = self._user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        if res in (0, -1, False):
            return False
        if msg.message == WM_QUIT:
            return False
        self._user32.TranslateMessage(ctypes.byref(msg))
        self._user32.DispatchMessageW(ctypes.byref(msg))
        return True

    def request_sync(self) -> None:
        if self._hwnd:
            with contextlib.suppress(Exception):
                self._user32.PostMessageW(self._hwnd, WM_APP_SYNC, 0, 0)

    def request_quit(self) -> None:
        if self._hwnd:
            with contextlib.suppress(Exception):
                self._user32.PostMessageW(self._hwnd, WM_CLOSE, 0, 0)
        if self._thread_id:
            with contextlib.suppress(Exception):
                self._user32.PostThreadMessageW(self._thread_id, WM_QUIT, 0, 0)

    def start_timer(self, interval_ms: int) -> None:
        if self._hwnd and not self._timer_active:
            self._user32.SetTimer(self._hwnd, PULSE_TIMER_ID, interval_ms, None)
            self._timer_active = True

    def stop_timer(self) -> None:
        if self._hwnd and self._timer_active:
            with contextlib.suppress(Exception):
                self._user32.KillTimer(self._hwnd, PULSE_TIMER_ID)
            self._timer_active = False

    def destroy(self) -> None:
        self.stop_timer()
        if self._hwnd:
            with contextlib.suppress(Exception):
                self._user32.DestroyWindow(self._hwnd)
            self._hwnd = None
        if self._class_name and self._hinstance:
            with contextlib.suppress(Exception):
                self._user32.UnregisterClassW(self._class_name, self._hinstance)
            self._class_name = ""
        if self._brush:
            with contextlib.suppress(Exception):
                self._gdi32.DeleteObject(self._brush)
            self._brush = None
        self._wndproc_ref = None
        self._shown = False
        self._thread_id = None


class ScreenIndicator:
    """Non-intrusive top-edge red bar shown when Clea is watching or capturing the screen."""

    def __init__(
        self,
        *,
        enabled: bool = True,
        height_px: int = DEFAULT_INDICATOR_HEIGHT_PX,
        color_rgb: tuple[int, int, int] = DEFAULT_INDICATOR_COLOR_RGB,
        pulse_seconds: float = DEFAULT_PULSE_SECONDS,
        api: Win32API | None = None,
        backend_factory: Callable[[], _IndicatorBackend] | None = None,
    ) -> None:
        self.enabled = bool(enabled)
        self.height_px = max(1, int(height_px))
        self.color_rgb = color_rgb
        self.pulse_seconds = max(0.05, float(pulse_seconds))
        self._api = api
        self._backend_factory = backend_factory

        self._lock = threading.Lock()
        self._watching = False
        self._pulse_deadline = 0.0
        self._rect: tuple[int, int, int, int] = (0, 0, 1920, self.height_px)
        self._shown_state = False

        self._thread: threading.Thread | None = None
        self._backend: _IndicatorBackend | None = None
        self._ready_event = threading.Event()
        self._stop_event = threading.Event()
        self._stopped = False

    @property
    def is_supported(self) -> bool:
        """Return True when overlay rendering is active (Windows or injected backend)."""
        return self.enabled and (sys.platform == "win32" or self._backend_factory is not None)

    @property
    def is_watching(self) -> bool:
        with self._lock:
            return self._watching

    @property
    def is_visible(self) -> bool:
        if not self.enabled:
            return False
        with self._lock:
            return self._watching or (
                self._pulse_deadline > 0.0 and time.monotonic() < self._pulse_deadline
            )

    @property
    def current_rect(self) -> tuple[int, int, int, int]:
        with self._lock:
            return self._rect

    def start(self) -> None:
        """Start the dedicated Win32 overlay message-loop thread if supported."""
        if not self.is_supported:
            return
        with self._lock:
            if self._thread is not None and self._thread.is_alive():
                return
            self._stopped = False
            self._stop_event.clear()
            self._ready_event.clear()

            thread = threading.Thread(
                target=self._run_loop,
                name="CleaScreenIndicatorThread",
                daemon=True,
            )
            self._thread = thread
        thread.start()
        self._ready_event.wait(timeout=3.0)

    def _resolve_monitor_bounds(
        self,
        monitor_bounds: tuple[int, int, int, int] | None = None,
    ) -> tuple[int, int, int, int]:
        if (
            monitor_bounds is not None
            and monitor_bounds[2] > monitor_bounds[0]
            and monitor_bounds[3] > monitor_bounds[1]
        ):
            return monitor_bounds

        mon: MonitorInfo | None = None
        if sys.platform == "win32" or self._api is not None:
            with contextlib.suppress(Exception):
                mon = get_target_monitor(api=self._api)
        if mon is not None and mon.width > 0 and mon.height > 0:
            return mon.bounds
        return (0, 0, 1920, 1080)

    def _ensure_started_if_needed(self) -> None:
        if not self.is_supported:
            return
        with self._lock:
            if self._stopped or (self._thread is not None and self._thread.is_alive()):
                return
        self.start()

    def _request_sync(self) -> None:
        backend = self._backend
        if backend is not None:
            backend.request_sync()

    def _apply_desired_state(self) -> None:
        """Synchronize Win32 window state on the overlay thread."""
        backend = self._backend
        if backend is None:
            return

        now = time.monotonic()
        with self._lock:
            if not self.enabled or self._stop_event.is_set():
                should_show = False
                need_timer = False
            elif self._watching:
                should_show = True
                need_timer = False
            elif self._pulse_deadline > now:
                should_show = True
                need_timer = True
            else:
                self._pulse_deadline = 0.0
                should_show = False
                need_timer = False
            rect = self._rect

        if need_timer:
            backend.start_timer(PULSE_TIMER_POLL_MS)
        else:
            backend.stop_timer()

        if should_show:
            backend.show(rect)
            self._shown_state = True
        else:
            backend.hide()
            self._shown_state = False

    def _run_loop(self) -> None:
        backend: _IndicatorBackend | None = None
        try:
            backend = (
                self._backend_factory()
                if self._backend_factory is not None
                else _Win32OverlayBackend()
            )
            if hasattr(backend, "set_sync_callback"):
                backend.set_sync_callback(self._apply_desired_state)  # type: ignore[attr-defined]
            self._backend = backend

            initial_bounds = self._resolve_monitor_bounds(None)
            initial_rect = compute_bar_rect(initial_bounds, self.height_px)
            with self._lock:
                self._rect = initial_rect

            backend.create(rgb_to_colorref(self.color_rgb), initial_rect)
            self._ready_event.set()
            self._apply_desired_state()

            while not self._stop_event.is_set():
                if not backend.pump_once():
                    break
        except Exception as exc:
            logger.debug("ScreenIndicator message loop exited: %s", exc)
            self._ready_event.set()
        finally:
            if backend is not None:
                with contextlib.suppress(Exception):
                    backend.destroy()
            self._backend = None
            self._shown_state = False
            self._ready_event.set()

    def pulse(
        self,
        monitor_bounds: tuple[int, int, int, int] | None = None,
        *,
        duration_seconds: float | None = None,
    ) -> None:
        """Show the red top bar for *duration_seconds* (extending any active pulse)."""
        if not self.enabled:
            return
        resolved = self._resolve_monitor_bounds(monitor_bounds)
        rect = compute_bar_rect(resolved, self.height_px)
        dur = self.pulse_seconds if duration_seconds is None else max(0.05, float(duration_seconds))
        deadline = time.monotonic() + dur
        with self._lock:
            self._rect = rect
            if deadline > self._pulse_deadline:
                self._pulse_deadline = deadline

        if not self.is_supported:
            return
        self._ensure_started_if_needed()
        self._request_sync()

    def start_watching(
        self,
        monitor_bounds: tuple[int, int, int, int] | None = None,
    ) -> None:
        """Show the red top bar continuously until stop_watching/hide/stop."""
        if not self.enabled:
            return
        resolved = self._resolve_monitor_bounds(monitor_bounds)
        rect = compute_bar_rect(resolved, self.height_px)
        with self._lock:
            self._watching = True
            self._pulse_deadline = 0.0
            self._rect = rect

        if not self.is_supported:
            return
        self._ensure_started_if_needed()
        self._request_sync()

    def update_watch_bounds(
        self,
        monitor_bounds: tuple[int, int, int, int] | None,
    ) -> None:
        """Reposition the indicator bar during active watching if the target monitor changed."""
        if not self.enabled or monitor_bounds is None:
            return
        resolved = self._resolve_monitor_bounds(monitor_bounds)
        new_rect = compute_bar_rect(resolved, self.height_px)
        changed = False
        with self._lock:
            if self._watching and new_rect != self._rect:
                self._rect = new_rect
                changed = True
        if changed and self.is_supported:
            self._request_sync()

    def stop_watching(self) -> None:
        """End continuous watching mode and hide the red bar immediately."""
        with self._lock:
            self._watching = False
            self._pulse_deadline = 0.0
        if self.is_supported:
            self._request_sync()

    def hide(self) -> None:
        """Immediately hide the red bar (cancelling any watch or pulse)."""
        with self._lock:
            self._watching = False
            self._pulse_deadline = 0.0
        if self.is_supported:
            self._request_sync()

    def stop(self) -> None:
        """Destroy the overlay window and join the dedicated Win32 message-loop thread."""
        with self._lock:
            self._stopped = True
            self._watching = False
            self._pulse_deadline = 0.0
        self._stop_event.set()

        backend = self._backend
        if backend is not None:
            with contextlib.suppress(Exception):
                backend.request_quit()

        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=2.0)
        self._thread = None

"""Unit tests for Win32 ctypes hotkey, DPI awareness, scaled coordinates, and window enumeration."""

from __future__ import annotations

import ctypes
import time
from unittest.mock import MagicMock

import pytest

from clea.surfaces.voice.win32 import (
    DEFAULT_DPI,
    DWMWA_EXTENDED_FRAME_BOUNDS,
    MOD_ALT,
    MOD_CONTROL,
    MOD_NOREPEAT,
    MOD_SHIFT,
    MONITORINFOF_PRIMARY,
    PROCESS_PER_MONITOR_DPI_AWARE,
    WM_HOTKEY,
    HotKeyManager,
    MonitorInfo,
    Win32API,
    WindowInfo,
    dpi_to_scale_factor,
    enable_per_monitor_dpi_awareness,
    enumerate_monitors,
    enumerate_visible_windows,
    get_monitor_for_window,
    get_target_monitor,
    get_window_bounds,
    get_window_process_name,
    get_window_title,
    parse_hotkey,
    resolve_app_display_name,
    scale_coordinate,
    scale_point,
    scale_rect,
    unscale_coordinate,
    unscale_point,
    unscale_rect,
)


def test_parse_hotkey_valid() -> None:
    mods, vk = parse_hotkey("Ctrl+Alt+V")
    assert mods == (MOD_NOREPEAT | MOD_CONTROL | MOD_ALT)
    assert vk == ord("V")

    mods2, vk2 = parse_hotkey("ctrl+shift+space")
    assert mods2 == (MOD_NOREPEAT | MOD_CONTROL | MOD_SHIFT)
    assert vk2 == 0x20

    mods3, vk3 = parse_hotkey("Alt+F5")
    assert mods3 == (MOD_NOREPEAT | MOD_ALT)
    assert vk3 == 0x74  # 0x70 + 4


def test_parse_hotkey_invalid() -> None:
    with pytest.raises(ValueError, match="Invalid empty"):
        parse_hotkey("")

    with pytest.raises(ValueError, match="No key specified"):
        parse_hotkey("Ctrl+Alt")

    with pytest.raises(ValueError, match="Multiple primary keys"):
        parse_hotkey("Ctrl+A+B")

    with pytest.raises(ValueError, match="Unsupported"):
        parse_hotkey("Ctrl+Alt+???")


def test_hotkey_manager_lifecycle_with_mocked_win32() -> None:
    mock_u32 = MagicMock()
    mock_k32 = MagicMock()

    mock_k32.GetCurrentThreadId.return_value = 1234
    mock_u32.RegisterHotKey.return_value = 1
    mock_u32.UnregisterHotKey.return_value = 1
    mock_u32.PostThreadMessageW.return_value = 1

    triggered = False

    def on_hotkey() -> None:
        nonlocal triggered
        triggered = True

    # Simulate message pump: first message is WM_HOTKEY, then loop ends
    call_count = 0

    def fake_get_message(
        msg_ptr: object, hwnd: object, min_filter: object, max_filter: object
    ) -> int:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            if msg_ptr:
                try:
                    msg_ptr._obj.message = WM_HOTKEY  # type: ignore[attr-defined]
                except AttributeError:
                    pass
            return 1
        time.sleep(0.05)
        return 0  # WM_QUIT

    mock_u32.GetMessageW.side_effect = fake_get_message

    api = Win32API(user32=mock_u32, kernel32=mock_k32)
    manager = HotKeyManager(api=api)

    manager.start("Ctrl+Alt+V", on_triggered=on_hotkey)
    time.sleep(0.15)
    manager.stop()

    assert mock_u32.RegisterHotKey.called
    assert mock_u32.UnregisterHotKey.called
    assert mock_u32.PostThreadMessageW.called


def test_hotkey_manager_registration_failure() -> None:
    mock_u32 = MagicMock()
    mock_k32 = MagicMock()

    mock_k32.GetCurrentThreadId.return_value = 5678
    mock_u32.RegisterHotKey.return_value = 0  # registration failure

    api = Win32API(user32=mock_u32, kernel32=mock_k32)
    manager = HotKeyManager(api=api)

    with pytest.raises(OSError, match="RegisterHotKey failed"):
        manager.start("Ctrl+Alt+V", on_triggered=lambda: None)

    manager.stop()


# --------------------------------------------------- DPI and Scaled Coordinates Tests


def test_dpi_to_scale_factor() -> None:
    assert dpi_to_scale_factor(96) == 1.0  # 100%
    assert dpi_to_scale_factor(120) == 1.25  # 125%
    assert dpi_to_scale_factor(144) == 1.50  # 150%
    assert dpi_to_scale_factor(192) == 2.00  # 200%

    with pytest.raises(ValueError, match="DPI must be positive"):
        dpi_to_scale_factor(0)
    with pytest.raises(ValueError, match="DPI must be positive"):
        dpi_to_scale_factor(-96)


def test_scaled_coordinates_100_percent() -> None:
    scale = dpi_to_scale_factor(DEFAULT_DPI)  # 1.0
    assert scale_coordinate(100, scale) == 100
    assert unscale_coordinate(100, scale) == 100
    assert scale_point((100, 200), scale) == (100, 200)
    assert unscale_point((100, 200), scale) == (100, 200)
    assert scale_rect((10, 20, 1920, 1080), scale) == (10, 20, 1920, 1080)
    assert unscale_rect((10, 20, 1920, 1080), scale) == (10, 20, 1920, 1080)


def test_scaled_coordinates_125_percent() -> None:
    # 125% display scaling (120 DPI)
    scale = dpi_to_scale_factor(120)  # 1.25
    assert scale == 1.25

    # Coordinates
    assert scale_coordinate(100, scale) == 125
    assert scale_coordinate(200, scale) == 250
    assert scale_coordinate(0, scale) == 0
    assert scale_coordinate(-80, scale) == -100

    # Unscale
    assert unscale_coordinate(125, scale) == 100
    assert unscale_coordinate(250, scale) == 200
    assert unscale_coordinate(-100, scale) == -80

    # Points
    assert scale_point((100, 200), scale) == (125, 250)
    assert unscale_point((125, 250), scale) == (100, 200)

    # Rectangles (1920x1080 standard full HD -> 2400x1350 physical pixels)
    logical_rect = (0, 0, 1920, 1080)
    physical_rect = scale_rect(logical_rect, scale)
    assert physical_rect == (0, 0, 2400, 1350)
    assert unscale_rect(physical_rect, scale) == logical_rect


def test_scaled_coordinates_150_percent() -> None:
    # 150% display scaling (144 DPI)
    scale = dpi_to_scale_factor(144)  # 1.50
    assert scale == 1.50

    # Coordinates
    assert scale_coordinate(100, scale) == 150
    assert scale_coordinate(200, scale) == 300
    assert scale_coordinate(0, scale) == 0
    assert scale_coordinate(-100, scale) == -150

    # Unscale
    assert unscale_coordinate(150, scale) == 100
    assert unscale_coordinate(300, scale) == 200
    assert unscale_coordinate(-150, scale) == -100

    # Points
    assert scale_point((100, 200), scale) == (150, 300)
    assert unscale_point((150, 300), scale) == (100, 200)

    # Rectangles (1920x1080 standard full HD -> 2880x1620 physical pixels)
    logical_rect = (0, 0, 1920, 1080)
    physical_rect = scale_rect(logical_rect, scale)
    assert physical_rect == (0, 0, 2880, 1620)
    assert unscale_rect(physical_rect, scale) == logical_rect


def test_unscale_invalid_scale_factor() -> None:
    with pytest.raises(ValueError, match="Scale factor must be positive"):
        unscale_coordinate(100, 0)
    with pytest.raises(ValueError, match="Scale factor must be positive"):
        unscale_point((100, 100), -1.25)
    with pytest.raises(ValueError, match="Scale factor must be positive"):
        unscale_rect((0, 0, 100, 100), 0)


def test_enable_per_monitor_dpi_awareness_tier1_v2() -> None:
    mock_u32 = MagicMock()
    mock_k32 = MagicMock()
    mock_u32.SetProcessDpiAwarenessContext.return_value = 1

    api = Win32API(user32=mock_u32, kernel32=mock_k32)
    assert enable_per_monitor_dpi_awareness(api=api) is True
    assert mock_u32.SetProcessDpiAwarenessContext.called


def test_enable_per_monitor_dpi_awareness_tier2_shcore_fallback() -> None:
    mock_u32 = MagicMock()
    mock_k32 = MagicMock()
    mock_shc = MagicMock()

    # Tier 1 fails
    mock_u32.SetProcessDpiAwarenessContext.return_value = 0
    # Tier 2 shcore succeeds (S_OK = 0)
    mock_shc.SetProcessDpiAwareness.return_value = 0

    api = Win32API(user32=mock_u32, kernel32=mock_k32, shcore=mock_shc)
    assert enable_per_monitor_dpi_awareness(api=api) is True
    mock_shc.SetProcessDpiAwareness.assert_called_once_with(PROCESS_PER_MONITOR_DPI_AWARE)


def test_enable_per_monitor_dpi_awareness_tier3_user32_fallback() -> None:
    mock_u32 = MagicMock(spec=["SetProcessDPIAware"])
    mock_k32 = MagicMock()
    mock_u32.SetProcessDPIAware.return_value = 1

    api = Win32API(user32=mock_u32, kernel32=mock_k32, shcore=None)
    assert enable_per_monitor_dpi_awareness(api=api) is True
    assert mock_u32.SetProcessDPIAware.called


def test_enable_per_monitor_dpi_awareness_all_fail() -> None:
    mock_u32 = MagicMock(spec=[])
    mock_k32 = MagicMock()
    api = Win32API(user32=mock_u32, kernel32=mock_k32, shcore=None)
    assert enable_per_monitor_dpi_awareness(api=api) is False


# --------------------------------------------- Window Title & Process Extraction Tests


def test_get_window_title_and_process_name_normal() -> None:
    mock_u32 = MagicMock()
    mock_k32 = MagicMock()

    mock_u32.GetWindowTextLengthW.return_value = 10

    def fake_get_text(hwnd: int, buf: object, max_len: int) -> int:
        ctypes.memmove(buf, ctypes.create_unicode_buffer("Clea Voice", max_len), max_len * 2)  # type: ignore[arg-type]
        return 10

    mock_u32.GetWindowTextW.side_effect = fake_get_text

    def fake_get_pid(hwnd: int, pid_ptr: object) -> int:
        pid_ptr._obj.value = 4321  # type: ignore[attr-defined]
        return 1

    mock_u32.GetWindowThreadProcessId.side_effect = fake_get_pid
    mock_k32.OpenProcess.return_value = 9999  # Valid handle

    def fake_query_name(handle: int, flags: int, buf: object, size_ptr: object) -> int:
        ctypes.memmove(buf, ctypes.create_unicode_buffer(r"C:\Program Files\App\app.exe"), 64)  # type: ignore[arg-type]
        return 1

    mock_k32.QueryFullProcessImageNameW.side_effect = fake_query_name
    mock_k32.CloseHandle.return_value = 1

    api = Win32API(user32=mock_u32, kernel32=mock_k32)
    title = get_window_title(1001, api)
    proc_name = get_window_process_name(1001, api)

    assert title == "Clea Voice"
    assert proc_name == "app.exe"
    assert mock_k32.CloseHandle.called


def test_get_window_process_name_elevated_window_returns_unknown_and_does_not_block() -> None:
    """Answer 3: If process name cannot be read, treat as unknown and check window title."""
    mock_u32 = MagicMock()
    mock_k32 = MagicMock()

    mock_u32.GetWindowTextLengthW.return_value = 12

    def fake_get_text(hwnd: int, buf: object, max_len: int) -> int:
        ctypes.memmove(buf, ctypes.create_unicode_buffer("Task Manager", max_len), max_len * 2)  # type: ignore[arg-type]
        return 12

    mock_u32.GetWindowTextW.side_effect = fake_get_text

    def fake_get_pid(hwnd: int, pid_ptr: object) -> int:
        pid_ptr._obj.value = 4  # System/elevated PID
        return 1

    mock_u32.GetWindowThreadProcessId.side_effect = fake_get_pid
    # OpenProcess returns 0 / NULL due to ERROR_ACCESS_DENIED on elevated window
    mock_k32.OpenProcess.return_value = 0

    api = Win32API(user32=mock_u32, kernel32=mock_k32)
    title = get_window_title(1002, api)
    proc_name = get_window_process_name(1002, api)

    # Window title is read; process name falls back to '<unknown>' without crashing
    assert title == "Task Manager"
    assert proc_name == "<unknown>"
    assert not mock_k32.CloseHandle.called


def test_enumerate_visible_windows_with_mocked_api() -> None:
    mock_u32 = MagicMock()
    mock_k32 = MagicMock()

    mock_u32.IsWindowVisible.side_effect = lambda hwnd: hwnd in (101, 102)
    mock_u32.GetWindowTextLengthW.return_value = 5

    def fake_get_text(hwnd: int, buf: object, max_len: int) -> int:
        t = "Win A" if hwnd == 101 else "Win B"
        ctypes.memmove(buf, ctypes.create_unicode_buffer(t, max_len), max_len * 2)  # type: ignore[arg-type]
        return len(t)

    mock_u32.GetWindowTextW.side_effect = fake_get_text

    def fake_get_pid(hwnd: int, pid_ptr: object) -> int:
        pid_ptr._obj.value = 111 if hwnd == 101 else 222  # type: ignore[attr-defined]
        return 1

    mock_u32.GetWindowThreadProcessId.side_effect = fake_get_pid
    mock_k32.OpenProcess.return_value = 0  # Elevated fallback test

    def fake_enum(cb: object, lparam: int) -> int:
        cb(101, 0)  # visible
        cb(999, 0)  # invisible
        cb(102, 0)  # visible
        return 1

    mock_u32.EnumWindows.side_effect = fake_enum

    api = Win32API(user32=mock_u32, kernel32=mock_k32)
    windows = enumerate_visible_windows(api=api)

    assert len(windows) == 2
    assert windows[0] == WindowInfo(
        hwnd=101, title="Win A", process_name="<unknown>", is_visible=True
    )
    assert windows[1] == WindowInfo(
        hwnd=102, title="Win B", process_name="<unknown>", is_visible=True
    )


def test_enable_per_monitor_dpi_awareness_already_set_via_e_accessdenied() -> None:
    mock_u32 = MagicMock()
    mock_k32 = MagicMock()
    mock_shc = MagicMock()
    mock_u32.SetProcessDpiAwarenessContext.return_value = 0
    err = OSError("already set")
    err.winerror = -2147024891
    mock_shc.SetProcessDpiAwareness.side_effect = err

    api = Win32API(user32=mock_u32, kernel32=mock_k32, shcore=mock_shc)
    assert enable_per_monitor_dpi_awareness(api=api) is True
    assert not mock_u32.SetProcessDPIAware.called


def test_enumerate_monitors_and_monitor_from_window_multi_display() -> None:
    mock_u32 = MagicMock()
    mock_k32 = MagicMock()

    mon_specs = {
        1001: (0, 0, 1920, 1080, 0, 0, 1920, 1032, MONITORINFOF_PRIMARY, r"\\.\DISPLAY1"),
        1002: (-1920, 0, 0, 1080, -1920, 0, 0, 1040, 0, r"\\.\DISPLAY2"),
    }

    def fake_enum_monitors(_hdc: object, _clip: object, cb: object, _lparam: int) -> int:
        cb(1001, 0, None, 0)
        cb(1002, 0, None, 0)
        return 1

    def fake_get_mon_info(hmon: int, mi_ptr: object) -> int:
        spec = mon_specs.get(hmon)
        if spec is None:
            return 0
        mi = mi_ptr._obj  # type: ignore[attr-defined]
        mi.rcMonitor.left, mi.rcMonitor.top, mi.rcMonitor.right, mi.rcMonitor.bottom = spec[0:4]
        mi.rcWork.left, mi.rcWork.top, mi.rcWork.right, mi.rcWork.bottom = spec[4:8]
        mi.dwFlags = spec[8]
        mi.szDevice = spec[9]
        return 1

    mock_u32.EnumDisplayMonitors.side_effect = fake_enum_monitors
    mock_u32.GetMonitorInfoW.side_effect = fake_get_mon_info
    mock_u32.MonitorFromWindow.side_effect = lambda hwnd, _flags: 1002 if hwnd == 42 else 1001
    mock_u32.GetForegroundWindow.return_value = 42

    api = Win32API(user32=mock_u32, kernel32=mock_k32)
    monitors = enumerate_monitors(api=api)
    assert len(monitors) == 2
    assert monitors[0].index == 1 and monitors[0].is_primary is True
    assert monitors[1].index == 2 and monitors[1].bounds == (-1920, 0, 0, 1080)

    fg_mon = get_target_monitor(api=api)
    assert fg_mon is not None
    assert fg_mon.index == 2
    assert fg_mon.bounds == (-1920, 0, 0, 1080)

    explicit_mon = get_target_monitor(monitor=1, api=api)
    assert explicit_mon is not None
    assert explicit_mon.index == 1

    # Explicit window handle overrides foreground window
    win_mon = get_monitor_for_window(99, api=api)
    assert win_mon is not None
    assert win_mon.index == 1


def test_get_window_bounds_dwm_fallback_minimized_and_maximized() -> None:
    mock_u32 = MagicMock()
    mock_k32 = MagicMock()
    mock_dwm = MagicMock()
    mock_u32.IsIconic.return_value = 0
    mock_u32.IsZoomed.return_value = 0

    def fake_dwm(hwnd: int, attr: int, rect_ptr: object, _size: int) -> int:
        assert attr == DWMWA_EXTENDED_FRAME_BOUNDS
        r = rect_ptr._obj  # type: ignore[attr-defined]
        r.left, r.top, r.right, r.bottom = (100, 50, 1500, 950)
        return 0

    mock_dwm.DwmGetWindowAttribute.side_effect = fake_dwm
    api = Win32API(user32=mock_u32, kernel32=mock_k32, dwmapi=mock_dwm)

    assert get_window_bounds(55, api=api) == (100, 50, 1500, 950)

    # DWM failure -> falls back to GetWindowRect, and IsZoomed clamps overhang to monitor
    mock_dwm.DwmGetWindowAttribute.side_effect = None
    mock_dwm.DwmGetWindowAttribute.return_value = -1

    def fake_rect(hwnd: int, rect_ptr: object) -> int:
        r = rect_ptr._obj  # type: ignore[attr-defined]
        r.left, r.top, r.right, r.bottom = (-8, -8, 1928, 1048)
        return 1

    mock_u32.GetWindowRect.side_effect = fake_rect
    mock_u32.IsZoomed.return_value = 1
    mon = MonitorInfo(1, 0, 0, 1920, 1080, 0, 0, 1920, 1040, is_primary=True)
    assert get_window_bounds(55, api=api, clamp_to_monitor=mon) == (0, 0, 1920, 1048)

    # Minimized window returns None
    mock_u32.IsIconic.return_value = 1
    assert get_window_bounds(55, api=api) is None


def test_resolve_app_display_name() -> None:
    assert resolve_app_display_name("Antigravity.exe") == "Google Antigravity"
    assert resolve_app_display_name("Code.exe") == "Visual Studio Code"
    assert resolve_app_display_name("WindowsTerminal.exe") == "Windows Terminal"
    assert resolve_app_display_name("Taskmgr.exe") == "Task Manager"
    assert resolve_app_display_name("zed.exe") == "Zed"
    assert resolve_app_display_name("ApplicationFrameHost.exe", "Calculator") == "Calculator"
    assert resolve_app_display_name("ApplicationFrameHost.exe", "") == "Windows App"
    assert resolve_app_display_name("MyCustomTool.exe") == "MyCustomTool"
    assert (
        resolve_app_display_name("<unknown>", "runner.py - Clea - Antigravity")
        == "Google Antigravity"
    )
    assert resolve_app_display_name("<unknown>", "Untitled") == "Unknown application"

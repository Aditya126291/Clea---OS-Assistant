"""Unit tests for window management, system hardware controls, and system diagnostics."""

from __future__ import annotations

import ctypes
import subprocess
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from clea.tools.registry import AuditLogger, RiskLevel, ToolRegistry
from clea.tools.system_controls import (
    SW_MAXIMIZE,
    SW_MINIMIZE,
    SW_RESTORE,
    VK_MEDIA_NEXT_TRACK,
    VK_MEDIA_PLAY_PAUSE,
    VK_MEDIA_PREV_TRACK,
    VK_MEDIA_STOP,
    WM_CLOSE,
    manage_window,
    register_system_control_tools,
    system_control,
    system_diagnostics,
)

# ---------------------------------------------------------------------------
# Helpers for mocking Win32
# ---------------------------------------------------------------------------


def _create_mock_user32(
    windows: list[tuple[int, str, bool]] | None = None,
    foreground_hwnd: int = 100,
    work_area: tuple[int, int, int, int] = (0, 0, 1920, 1040),
) -> MagicMock:
    """Create a mock user32 with controlled windows and monitor info."""
    u32 = MagicMock()
    u32.GetForegroundWindow.return_value = foreground_hwnd

    window_list = windows or [
        (100, "Visual Studio Code - Clea", True),
        (200, "Google Chrome", True),
        (300, "", True),  # Empty title -> should be filtered
        (400, "Hidden Window", False),  # Invisible -> should be filtered
    ]

    def _fake_enum_windows(callback: Any, lparam: Any) -> int:
        for hwnd, _title, _vis in window_list:
            callback(hwnd, lparam)
        return 1

    u32.EnumWindows.side_effect = _fake_enum_windows

    def _fake_is_visible(hwnd: int) -> int:
        for h, _t, vis in window_list:
            if h == hwnd:
                return 1 if vis else 0
        return 0

    u32.IsWindowVisible.side_effect = _fake_is_visible

    def _fake_get_length(hwnd: int) -> int:
        for h, t, _vis in window_list:
            if h == hwnd:
                return len(t)
        return 0

    u32.GetWindowTextLengthW.side_effect = _fake_get_length

    def _fake_get_text(hwnd: int, buf: Any, max_len: int) -> int:
        for h, t, _vis in window_list:
            if h == hwnd:
                cbuf = ctypes.create_unicode_buffer(t, max_len)
                ctypes.memmove(buf, cbuf, min(len(t) * 2, max_len * 2))
                return len(t)
        return 0

    u32.GetWindowTextW.side_effect = _fake_get_text

    def _fake_get_monitor_info(hmonitor: Any, mi_ptr: Any) -> int:
        mi = mi_ptr._obj
        mi.rcWork.left = work_area[0]
        mi.rcWork.top = work_area[1]
        mi.rcWork.right = work_area[2]
        mi.rcWork.bottom = work_area[3]
        return 1

    u32.GetMonitorInfoW.side_effect = _fake_get_monitor_info
    u32.MonitorFromWindow.return_value = 1
    return u32


# ---------------------------------------------------------------------------
# Test manage_window
# ---------------------------------------------------------------------------


def test_manage_window_list_filters_windows() -> None:
    u32 = _create_mock_user32(
        windows=[
            (101, "Editor", True),
            (102, "Browser", True),
            (103, "   ", True),  # Whitespace only -> filter out
            (104, "Hidden App", False),  # Invisible -> filter out
        ],
        foreground_hwnd=101,
    )
    result = manage_window("list", user32=u32)
    assert result["ok"] is True
    assert result["action"] == "list"
    assert result["count"] == 2
    titles = [w["title"] for w in result["windows"]]
    assert titles == ["Editor", "Browser"]
    assert result["windows"][0]["is_foreground"] is True
    assert result["windows"][1]["is_foreground"] is False


def test_manage_window_focus_by_title_and_foreground_fallback() -> None:
    u32 = _create_mock_user32(
        windows=[
            (10, "Terminal - powershell", True),
            (20, "Spotify Premium", True),
        ],
        foreground_hwnd=10,
    )

    # Focus by query
    res = manage_window("focus", title_query="spotify", user32=u32)
    assert res["ok"] is True
    assert res["hwnd"] == 20
    assert res["title"] == "Spotify Premium"
    u32.ShowWindow.assert_called_with(20, SW_RESTORE)
    u32.SetForegroundWindow.assert_called_with(20)

    # Focus foreground fallback when title_query is omitted
    res_fg = manage_window("focus", title_query=None, user32=u32)
    assert res_fg["ok"] is True
    assert res_fg["hwnd"] == 10
    u32.ShowWindow.assert_called_with(10, SW_RESTORE)
    u32.SetForegroundWindow.assert_called_with(10)


def test_manage_window_focus_not_found() -> None:
    u32 = _create_mock_user32(windows=[(1, "Notepad", True)])
    with pytest.raises(ValueError, match="No visible window found"):
        manage_window("focus", title_query="Calculator", user32=u32)


def test_manage_window_snap_left_and_right_work_area_math() -> None:
    # 1920x1080 display with 40px taskbar at bottom -> rcWork: (0, 0, 1920, 1040)
    u32 = _create_mock_user32(
        windows=[(55, "Main Window", True)],
        work_area=(0, 0, 1920, 1040),
    )

    # Snap Left: x=0, y=0, w=960, h=1040
    res_l = manage_window("snap_left", title_query="Main", user32=u32)
    assert res_l["ok"] is True
    assert res_l["action"] == "snap_left"
    assert res_l["rect"] == {"x": 0, "y": 0, "width": 960, "height": 1040}
    u32.ShowWindow.assert_called_with(55, SW_RESTORE)
    u32.MoveWindow.assert_called_with(55, 0, 0, 960, 1040, True)
    u32.SetForegroundWindow.assert_called_with(55)

    # Snap Right: x=960, y=0, w=960, h=1040
    res_r = manage_window("snap_right", title_query="Main", user32=u32)
    assert res_r["ok"] is True
    assert res_r["action"] == "snap_right"
    assert res_r["rect"] == {"x": 960, "y": 0, "width": 960, "height": 1040}
    u32.MoveWindow.assert_called_with(55, 960, 0, 960, 1040, True)


def test_manage_window_snap_with_taskbar_offset() -> None:
    # Multi-monitor or left-aligned taskbar: rcWork (60, 0, 1920, 1080) -> width = 1860, half = 930
    u32 = _create_mock_user32(
        windows=[(77, "Code", True)],
        work_area=(60, 0, 1920, 1080),
    )
    res_l = manage_window("snap_left", title_query="Code", user32=u32)
    assert res_l["rect"] == {"x": 60, "y": 0, "width": 930, "height": 1080}
    u32.MoveWindow.assert_called_with(77, 60, 0, 930, 1080, True)

    res_r = manage_window("snap_right", title_query="Code", user32=u32)
    assert res_r["rect"] == {"x": 990, "y": 0, "width": 930, "height": 1080}
    u32.MoveWindow.assert_called_with(77, 990, 0, 930, 1080, True)


def test_manage_window_maximize_minimize_restore_close() -> None:
    u32 = _create_mock_user32(windows=[(88, "App", True)])

    manage_window("maximize", title_query="App", user32=u32)
    u32.ShowWindow.assert_called_with(88, SW_MAXIMIZE)

    manage_window("minimize", title_query="App", user32=u32)
    u32.ShowWindow.assert_called_with(88, SW_MINIMIZE)

    manage_window("restore", title_query="App", user32=u32)
    u32.ShowWindow.assert_called_with(88, SW_RESTORE)

    manage_window("close", title_query="App", user32=u32)
    u32.PostMessageW.assert_called_with(88, WM_CLOSE, 0, 0)


def test_manage_window_invalid_action() -> None:
    u32 = _create_mock_user32()
    with pytest.raises(ValueError, match="Unsupported window action"):
        manage_window("destroy_screen", user32=u32)


# ---------------------------------------------------------------------------
# Test system_control
# ---------------------------------------------------------------------------


def test_system_control_volume_clamping_and_actions() -> None:
    fake_ps = MagicMock()
    # Mock powershell runner returning volume=40, mute=false for get
    fake_ps.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="40 False\n", stderr=""
    )

    # 1. Volume get
    res_get = system_control("volume", "get", powershell_runner=fake_ps)
    assert res_get["ok"] is True
    assert res_get["volume"] == 40
    assert res_get["muted"] is False

    # 2. Volume set clamped to 0..100
    res_set_over = system_control("volume", "set", value=150, powershell_runner=fake_ps)
    assert res_set_over["ok"] is True
    assert res_set_over["volume"] == 100

    res_set_under = system_control("volume", "set", value=-25, powershell_runner=fake_ps)
    assert res_set_under["ok"] is True
    assert res_set_under["volume"] == 0

    # 3. Volume set requires value
    with pytest.raises(ValueError, match="value .* is required"):
        system_control("volume", "set", value=None, powershell_runner=fake_ps)

    # 4. Volume up & down
    res_up = system_control("volume", "up", value=15, powershell_runner=fake_ps)
    assert res_up["ok"] is True
    assert res_up["volume"] == 55  # 40 + 15

    res_down = system_control("volume", "down", value=20, powershell_runner=fake_ps)
    assert res_down["ok"] is True
    assert res_down["volume"] == 20  # 40 - 20

    # 5. Volume mute / unmute / toggle_mute
    res_mute = system_control("volume", "mute", powershell_runner=fake_ps)
    assert res_mute["ok"] is True
    assert res_mute["muted"] is True

    res_unmute = system_control("volume", "unmute", powershell_runner=fake_ps)
    assert res_unmute["ok"] is True
    assert res_unmute["muted"] is False

    fake_ps.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="True\n", stderr=""
    )
    res_toggle = system_control("volume", "toggle_mute", powershell_runner=fake_ps)
    assert res_toggle["ok"] is True
    assert res_toggle["muted"] is True


def test_system_control_volume_win32_keybd_fallback() -> None:
    u32 = MagicMock()
    # Mock powershell failure to test Win32 keybd_event fallback
    failing_ps = MagicMock()
    failing_ps.return_value = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="error"
    )

    res = system_control("volume", "up", user32=u32, powershell_runner=failing_ps)
    assert res["ok"] is True
    assert res["action"] == "up"
    assert u32.keybd_event.called


def test_system_control_brightness_get_set_and_external_monitor_error() -> None:
    fake_ps = MagicMock()
    fake_ps.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="60\n", stderr=""
    )

    # 1. Brightness get
    res_get = system_control("brightness", "get", powershell_runner=fake_ps)
    assert res_get["ok"] is True
    assert res_get["brightness"] == 60

    # 2. Brightness set with clamping
    res_set = system_control("brightness", "set", value=85, powershell_runner=fake_ps)
    assert res_set["ok"] is True
    assert res_set["brightness"] == 85

    res_set_clamp = system_control("brightness", "set", value=120, powershell_runner=fake_ps)
    assert res_set_clamp["brightness"] == 100

    # 3. Brightness up / down
    res_up = system_control("brightness", "up", value=15, powershell_runner=fake_ps)
    assert res_up["brightness"] == 75

    # 4. External desktop monitor failure (non-zero return or exception)
    failing_ps = MagicMock()
    failing_ps.return_value = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="WmiMonitorBrightness not supported"
    )
    res_fail = system_control("brightness", "get", powershell_runner=failing_ps)
    assert res_fail["ok"] is False
    assert res_fail["supported"] is False
    assert "Display brightness control is not available" in res_fail["message"]


def test_system_control_media_dispatch() -> None:
    u32 = MagicMock()

    system_control("media", "play_pause", user32=u32)
    u32.keybd_event.assert_any_call(VK_MEDIA_PLAY_PAUSE, 0, 1, 0)

    system_control("media", "next", user32=u32)
    u32.keybd_event.assert_any_call(VK_MEDIA_NEXT_TRACK, 0, 1, 0)

    system_control("media", "previous", user32=u32)
    u32.keybd_event.assert_any_call(VK_MEDIA_PREV_TRACK, 0, 1, 0)

    system_control("media", "stop", user32=u32)
    u32.keybd_event.assert_any_call(VK_MEDIA_STOP, 0, 1, 0)


def test_system_control_theme_toggle_and_set() -> None:
    mock_reg = MagicMock()
    # Mock current theme as light (1)
    mock_reg.QueryValueEx.return_value = (1, 4)

    # Toggle from light -> dark
    res_toggle = system_control("theme", "toggle", winreg_mod=mock_reg)
    assert res_toggle["ok"] is True
    assert res_toggle["theme"] == "dark"
    assert res_toggle["previous_theme"] == "light"

    # Set dark explicitly
    res_dark = system_control("theme", "dark", winreg_mod=mock_reg)
    assert res_dark["ok"] is True
    assert res_dark["theme"] == "dark"

    # Set light explicitly
    res_light = system_control("theme", "light", winreg_mod=mock_reg)
    assert res_light["ok"] is True
    assert res_light["theme"] == "light"


def test_system_control_invalid_control() -> None:
    with pytest.raises(ValueError, match="Unsupported control"):
        system_control("coffee_machine", "brew")


# ---------------------------------------------------------------------------
# Test system_diagnostics
# ---------------------------------------------------------------------------


def test_system_diagnostics_battery_and_overview() -> None:
    mock_k32 = MagicMock()

    def fake_power_status(ptr: Any) -> int:
        status = ptr._obj
        status.ACLineStatus = 1
        status.BatteryFlag = 1
        status.BatteryLifePercent = 88
        return 1

    mock_k32.GetSystemPowerStatus.side_effect = fake_power_status

    def fake_mem_status(ptr: Any) -> int:
        m = ptr._obj
        m.ullTotalPhys = 16 * (1024**3)
        m.ullAvailPhys = 8 * (1024**3)
        m.dwMemoryLoad = 50
        return 1

    mock_k32.GlobalMemoryStatusEx.side_effect = fake_mem_status

    # Battery check
    res_bat = system_diagnostics("battery", kernel32=mock_k32)
    assert res_bat["ok"] is True
    assert res_bat["battery"]["percentage"] == 88
    assert res_bat["battery"]["plugged"] is True
    assert res_bat["battery"]["status"] == "charging"

    # Overview check
    with patch("clea.tools.system_controls._get_network_info") as mock_net:
        mock_net.return_value = {"connected": True, "ssid": "OfficeWiFi", "internet": True}
        res_ov = system_diagnostics("overview", kernel32=mock_k32)
        assert res_ov["ok"] is True
        assert res_ov["battery"]["percentage"] == 88
        assert res_ov["ram"]["total_gb"] == 16.0
        assert res_ov["ram"]["used_gb"] == 8.0
        assert res_ov["network"]["ssid"] == "OfficeWiFi"


def test_system_diagnostics_port_inspection_and_process_guard() -> None:
    sample_netstat = (
        "  Proto  Local Address          Foreign Address        State           PID\n"
        "  TCP    127.0.0.1:5173         0.0.0.0:0              LISTENING       8120\n"
        "  TCP    0.0.0.0:445            0.0.0.0:0              LISTENING       4\n"
    )

    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=sample_netstat, stderr=""
        )

        # 1. Port lookup without kill
        res_info = system_diagnostics("port", port=5173, kill_port_process=False)
        assert res_info["ok"] is True
        assert res_info["listening"] is True
        assert res_info["pid"] == 8120
        assert res_info["killed"] is False

        # 2. Port lookup for unused port
        res_unused = system_diagnostics("port", port=9999)
        assert res_unused["ok"] is True
        assert res_unused["listening"] is False
        assert res_unused["pid"] is None

        # 3. Kill port process for user PID 8120
        mock_killer = MagicMock()
        res_kill = system_diagnostics(
            "port", port=5173, kill_port_process=True, process_killer=mock_killer
        )
        assert res_kill["ok"] is True
        assert res_kill["killed"] is True
        mock_killer.assert_called_with(8120)

        # 4. Critical system PID <= 4 refused
        res_crit = system_diagnostics(
            "port", port=445, kill_port_process=True, process_killer=mock_killer
        )
        assert res_crit["ok"] is False
        assert res_crit["killed"] is False
        assert "Refusing to terminate critical system PID" in res_crit["error"]


def test_system_diagnostics_missing_port() -> None:
    with pytest.raises(ValueError, match="port is required"):
        system_diagnostics("port", port=None)


def test_system_diagnostics_invalid_port_numbers() -> None:
    for invalid_port in (-1, 0, 65536, 100000):
        with pytest.raises(ValueError, match="Invalid port"):
            system_diagnostics("port", port=invalid_port)


def test_system_diagnostics_critical_pids_refusal() -> None:
    for crit_pid in (0, 1, 2, 3, 4):
        sample_netstat = (
            "  Proto  Local Address          Foreign Address        State           PID\n"
            f"  TCP    0.0.0.0:135            0.0.0.0:0              LISTENING       {crit_pid}\n"
        )
        with patch("subprocess.run") as mock_run:
            mock_run.return_value = subprocess.CompletedProcess(
                args=[], returncode=0, stdout=sample_netstat, stderr=""
            )
            mock_killer = MagicMock()
            res = system_diagnostics(
                "port", port=135, kill_port_process=True, process_killer=mock_killer
            )
            assert res["ok"] is False
            assert res["killed"] is False
            assert f"Refusing to terminate critical system PID {crit_pid}" in res["error"]
            mock_killer.assert_not_called()


def test_system_diagnostics_battery_no_battery_flag() -> None:
    mock_k32 = MagicMock()

    def fake_no_battery(ptr: Any) -> int:
        status = ptr._obj
        status.ACLineStatus = 1
        status.BatteryFlag = 128  # No system battery
        status.BatteryLifePercent = 255
        return 1

    mock_k32.GetSystemPowerStatus.side_effect = fake_no_battery
    res = system_diagnostics("battery", kernel32=mock_k32)
    assert res["ok"] is True
    assert res["battery"]["percentage"] is None
    assert res["battery"]["status"] == "no_battery"
    assert res["battery"]["plugged"] is True


def test_system_control_media_aliases() -> None:
    u32 = MagicMock()
    for act in ("play", "pause"):
        res = system_control("media", act, user32=u32)
        assert res["ok"] is True
        assert res["action"] == act
    res_prev = system_control("media", "prev", user32=u32)
    assert res_prev["ok"] is True
    assert res_prev["action"] == "prev"


def test_empty_and_invalid_arguments() -> None:
    with pytest.raises(ValueError, match="action must be a string"):
        manage_window(123)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="control must be a string"):
        system_control(123, "get")  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="action must be a string"):
        system_control("volume", 123)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="value must be an integer"):
        system_control("volume", "set", value=True)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="check must be a string"):
        system_diagnostics(123)  # type: ignore[arg-type]

    with pytest.raises(ValueError, match="Invalid port"):
        system_diagnostics("port", port=True)  # type: ignore[arg-type]


def test_system_diagnostics_port_exact_match_not_substring() -> None:
    # Port 8080 listening; querying port 80 must NOT match 8080
    sample_netstat_8080 = (
        "  Proto  Local Address          Foreign Address        State           PID\n"
        "  TCP    127.0.0.1:8080         0.0.0.0:0              LISTENING       9999\n"
    )
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=sample_netstat_8080, stderr=""
        )
        res_80 = system_diagnostics("port", port=80)
        assert res_80["ok"] is True
        assert res_80["listening"] is False
        assert res_80["pid"] is None

        # When port 80 actually listens, it matches exactly
        sample_netstat_80 = (
            "  Proto  Local Address          Foreign Address        State           PID\n"
            "  TCP    127.0.0.1:80           0.0.0.0:0              LISTENING       4430\n"
        )
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=sample_netstat_80, stderr=""
        )
        res_exact = system_diagnostics("port", port=80)
        assert res_exact["ok"] is True
        assert res_exact["listening"] is True
        assert res_exact["pid"] == 4430


def test_system_diagnostics_port_powershell_net_tcp_fallback() -> None:
    # Netstat returns empty stdout, but PowerShell Get-NetTCPConnection resolves PID
    fake_ps = MagicMock()
    fake_ps.return_value = subprocess.CompletedProcess(
        args=[], returncode=0, stdout="5678\n", stderr=""
    )
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout="", stderr=""
        )
        res = system_diagnostics("port", port=3000, powershell_runner=fake_ps)
        assert res["ok"] is True
        assert res["listening"] is True
        assert res["pid"] == 5678


def test_manage_window_hwnd_targeting_and_numeric_query() -> None:
    u32 = _create_mock_user32(
        windows=[(67070, "Gemini Application", True)],
        foreground_hwnd=67070,
    )
    # 1. Target by integer HWND
    res_int = manage_window("focus", title_query=67070, user32=u32)
    assert res_int["ok"] is True
    assert res_int["hwnd"] == 67070

    # 2. Target by string numeric HWND
    res_str = manage_window("focus", title_query="67070", user32=u32)
    assert res_str["ok"] is True
    assert res_str["hwnd"] == 67070

    # 3. Reject boolean title_query
    with pytest.raises(ValueError, match="title_query must be a string or integer HWND"):
        manage_window("focus", title_query=True, user32=u32)  # type: ignore[arg-type]


def test_system_control_volume_wm_appcommand_fallback() -> None:
    u32 = MagicMock()
    del u32.keybd_event  # Force WM_APPCOMMAND path
    failing_ps = MagicMock()
    failing_ps.return_value = subprocess.CompletedProcess(
        args=[], returncode=1, stdout="", stderr="error"
    )

    res_mute = system_control("volume", "mute", user32=u32, powershell_runner=failing_ps)
    assert res_mute["ok"] is True
    assert res_mute["action"] == "mute"
    assert res_mute["muted"] is True
    assert u32.SendMessageW.called or u32.DefWindowProcW.called


def test_system_diagnostics_auto_infer_port_check() -> None:
    sample_netstat = (
        "  Proto  Local Address          Foreign Address        State           PID\n"
        "  TCP    127.0.0.1:5173         0.0.0.0:0              LISTENING       1234\n"
    )
    with patch("subprocess.run") as mock_run:
        mock_run.return_value = subprocess.CompletedProcess(
            args=[], returncode=0, stdout=sample_netstat, stderr=""
        )
        # Calling without check="port" should infer "port" because port=5173 is supplied
        res = system_diagnostics(port=5173)
        assert res["ok"] is True
        assert res["check"] == "port"
        assert res["pid"] == 1234


def test_system_diagnostics_kill_failure_reporting() -> None:
    sample_netstat = (
        "  Proto  Local Address          Foreign Address        State           PID\n"
        "  TCP    127.0.0.1:5173         0.0.0.0:0              LISTENING       8888\n"
    )

    def fake_subprocess_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "netstat" in str(cmd[0]):
            return subprocess.CompletedProcess(
                args=cmd, returncode=0, stdout=sample_netstat, stderr=""
            )
        if "taskkill" in str(cmd[0]):
            return subprocess.CompletedProcess(
                args=cmd, returncode=1, stdout="", stderr="ERROR: Access is denied."
            )
        return subprocess.CompletedProcess(args=cmd, returncode=0, stdout="", stderr="")

    with patch("subprocess.run", side_effect=fake_subprocess_run):
        res = system_diagnostics("port", port=5173, kill_port_process=True)
        assert res["ok"] is False
        assert res["killed"] is False
        assert "Access is denied" in res["error"]


def test_manage_window_desktop_windows_fallback() -> None:
    u32 = MagicMock()
    # EnumWindows finds nothing (e.g. background desktop)
    u32.EnumWindows.side_effect = lambda cb, lparam: 1
    # OpenDesktopW returns valid desktop handle
    u32.OpenDesktopW.return_value = 500

    def fake_enum_desktop(hdesk: int, cb: Any, lparam: Any) -> int:
        u32.IsWindowVisible.return_value = 1
        u32.GetWindowTextLengthW.return_value = 11
        cb(777, lparam)
        return 1

    u32.EnumDesktopWindows.side_effect = fake_enum_desktop

    def fake_get_text(hwnd: int, buf: Any, max_len: int) -> int:
        ctypes.memmove(buf, ctypes.create_unicode_buffer("Desktop App", max_len), 24)
        return 11

    u32.GetWindowTextW.side_effect = fake_get_text

    res = manage_window("list", user32=u32)
    assert res["ok"] is True
    assert res["count"] >= 1
    assert any(w["title"] == "Desktop App" for w in res["windows"])
    u32.CloseDesktop.assert_called_with(500)



# ---------------------------------------------------------------------------
# Test ToolRegistry integration
# ---------------------------------------------------------------------------


async def test_tool_registry_registration_and_execution(tmp_path: Any) -> None:
    audit_file = tmp_path / "audit.jsonl"
    reg = ToolRegistry(audit_logger=AuditLogger(audit_file))
    register_system_control_tools(reg)

    definitions = {defn.name: defn for defn in reg.list_definitions()}
    assert "manage_window" in definitions
    assert "system_control" in definitions
    assert "system_diagnostics" in definitions

    assert definitions["manage_window"].risk_level == RiskLevel.READ_ONLY
    assert definitions["system_control"].risk_level == RiskLevel.READ_ONLY
    assert definitions["system_diagnostics"].risk_level == RiskLevel.READ_ONLY

    # Execute system_diagnostics via registry
    with patch("clea.tools.system_controls._get_battery_info") as mock_bat:
        mock_bat.return_value = {"percentage": 95, "plugged": True, "status": "charging"}
        res = await reg.execute("system_diagnostics", {"check": "battery"})
        assert res["ok"] is True
        assert res["battery"]["percentage"] == 95

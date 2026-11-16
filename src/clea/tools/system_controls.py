"""Window snapping/focus, hardware/media controls, and system diagnostics tools."""

from __future__ import annotations

import csv
import ctypes
import ctypes.wintypes as wintypes
import logging
import shutil
import socket
import subprocess
import sys
from collections.abc import Callable
from typing import Any

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


class RECT(ctypes.Structure):
    _fields_ = [
        ("left", ctypes.c_long),
        ("top", ctypes.c_long),
        ("right", ctypes.c_long),
        ("bottom", ctypes.c_long),
    ]


class MONITORINFO(ctypes.Structure):
    _fields_ = [
        ("cbSize", ctypes.c_ulong),
        ("rcMonitor", RECT),
        ("rcWork", RECT),
        ("dwFlags", ctypes.c_ulong),
    ]


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


def _get_user32(user32: Any = None) -> Any:
    if user32 is not None:
        return user32
    if sys.platform == "win32" and hasattr(ctypes, "windll"):
        return ctypes.windll.user32
    return None


def _get_kernel32(kernel32: Any = None) -> Any:
    if kernel32 is not None:
        return kernel32
    if sys.platform == "win32" and hasattr(ctypes, "windll"):
        return ctypes.windll.kernel32
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

    if title_query and title_query.strip():
        q = title_query.strip()
        q_lower = q.lower()
        windows = _enumerate_windows_ctypes(user32)

        # 1. Match by numeric HWND if q is digit
        if q.isdigit():
            target_hwnd = int(q)
            for w in windows:
                if w["hwnd"] == target_hwnd:
                    return w["hwnd"], w["title"]

        # 2. Exact match (case-insensitive)
        for w in windows:
            if w["title"].strip().lower() == q_lower:
                return w["hwnd"], w["title"]

        # 3. Substring match (case-insensitive, prioritize shortest title)
        matches = [w for w in windows if q_lower in w["title"].lower()]
        if matches:
            matches.sort(key=lambda item: len(item["title"]))
            return matches[0]["hwnd"], matches[0]["title"]
        raise ValueError(f"No visible window found matching title_query={title_query!r}.")

    if hasattr(user32, "GetForegroundWindow"):
        fg = user32.GetForegroundWindow()
        if fg:
            hwnd = int(fg)
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


def manage_window(
    action: str,
    title_query: str | None = None,
    *,
    user32: Any = None,
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

    hwnd, title = _resolve_target_window(u32, title_query)

    if norm_action == "focus":
        if hasattr(u32, "ShowWindow"):
            u32.ShowWindow(hwnd, SW_RESTORE)
        if hasattr(u32, "SetForegroundWindow"):
            u32.SetForegroundWindow(hwnd)
        return {
            "ok": True,
            "action": "focus",
            "hwnd": hwnd,
            "title": title,
        }

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
        else:
            x = left + half_width
            y = top
            w = work_width - half_width
            h = work_height

        if hasattr(u32, "ShowWindow"):
            u32.ShowWindow(hwnd, SW_RESTORE)
        if hasattr(u32, "MoveWindow"):
            u32.MoveWindow(hwnd, x, y, w, h, True)
        if hasattr(u32, "SetForegroundWindow"):
            u32.SetForegroundWindow(hwnd)

        return {
            "ok": True,
            "action": norm_action,
            "hwnd": hwnd,
            "title": title,
            "rect": {"x": x, "y": y, "width": w, "height": h},
        }

    if norm_action == "maximize":
        if hasattr(u32, "ShowWindow"):
            u32.ShowWindow(hwnd, SW_MAXIMIZE)
        if hasattr(u32, "SetForegroundWindow"):
            u32.SetForegroundWindow(hwnd)
        return {"ok": True, "action": "maximize", "hwnd": hwnd, "title": title}

    if norm_action == "minimize":
        if hasattr(u32, "ShowWindow"):
            u32.ShowWindow(hwnd, SW_MINIMIZE)
        return {"ok": True, "action": "minimize", "hwnd": hwnd, "title": title}

    if norm_action == "restore":
        if hasattr(u32, "ShowWindow"):
            u32.ShowWindow(hwnd, SW_RESTORE)
        if hasattr(u32, "SetForegroundWindow"):
            u32.SetForegroundWindow(hwnd)
        return {"ok": True, "action": "restore", "hwnd": hwnd, "title": title}

    if norm_action == "close":
        if hasattr(u32, "PostMessageW"):
            u32.PostMessageW(hwnd, WM_CLOSE, 0, 0)
        elif hasattr(u32, "SendMessageW"):
            u32.SendMessageW(hwnd, WM_CLOSE, 0, 0)
        return {"ok": True, "action": "close", "hwnd": hwnd, "title": title}

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
    u32 = _get_user32(user32)
    if u32 and hasattr(u32, "keybd_event"):
        u32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY, 0)
        u32.keybd_event(vk, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0)
    return {"ok": True, "control": "media", "action": norm_action}


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
        return _handle_media_control(action, user32=user32)
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
) -> None:
    """Register window management, hardware/system controls, and diagnostic tools."""
    registry.register(
        name="manage_window",
        description=(
            "Inspect and control application window placement on Windows. "
            "Supported actions: 'list' (all visible windows), 'focus', 'snap_left' (left 50%), "
            "'snap_right' (right 50%), 'maximize', 'minimize', 'restore', 'close'. "
            "Target window can be specified by title_query, or defaults to foreground window."
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
                    "description": "Optional window title substring to match.",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        risk_level=manage_window_risk,
        handler=manage_window,
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
        handler=system_control,
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

"""Deterministic unit tests for visual coordinate calibration, 1568px cap,
stale-geometry guards, _ALLOWED_MS_SETTINGS, ui_act view-change staleness,
and generalized Windows URL Protocol registry routing.
"""

from __future__ import annotations

import io
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from PIL import Image

import clea.tools.vision as vision_mod
from clea.surfaces.voice.screen import (
    MAX_VISION_DIMENSION,
    ScreenCaptureService,
)
from clea.surfaces.voice.win32 import MONITORINFOF_PRIMARY, Win32API
from clea.tools.guards import WindowContext
from clea.tools.view_fingerprint import WindowViewFingerprint
from clea.tools.vision import (
    ArtifactStore,
    ScreenVisionService,
    Win32ScreenVisionBackend,
    WindowCapture,
    WindowInfo,
)
from clea.tools.windows_os import (
    _ALLOWED_MS_SETTINGS,
    _DANGEROUS_URL_SCHEMES,
    _UI_REF_STORE,
    clear_last_screen_geometry,
    clear_ui_ref_store,
    control_system,
    get_last_screen_geometry,
    gui_action,
    is_registered_url_protocol,
    open_url,
    set_last_screen_geometry,
    set_ui_snapshot_state,
    ui_act,
)


class _FakePointerUser32:
    """Hermetic Win32 user32 test double recording cursor moves and mouse inputs."""

    def __init__(self, foreground_hwnd: int = 500) -> None:
        self.foreground_window = foreground_hwnd
        self.cursor_moves: list[tuple[int, int]] = []
        self.sent_inputs: list[Any] = []
        self.window_rects: dict[int, tuple[int, int, int, int]] = {}

    def GetForegroundWindow(self) -> int:
        return self.foreground_window

    def SetCursorPos(self, x: int, y: int) -> int:
        self.cursor_moves.append((int(x), int(y)))
        return 1

    def SendInput(self, count: int, inputs: Any, size: int) -> int:
        for i in range(count):
            self.sent_inputs.append(inputs[i])
        return count

    def GetWindowRect(self, hwnd: int, rect_ptr: Any) -> int:
        bounds = self.window_rects.get(int(hwnd))
        if bounds is None:
            return 0
        r = getattr(rect_ptr, "_obj", getattr(rect_ptr, "contents", None))
        if r is not None:
            r.left, r.top, r.right, r.bottom = bounds
        return 1


class _FakeFocusBackend:
    """Hermetic FocusPreconditionBackend double with controllable live DWM bounds."""

    def __init__(self, hwnd: int, bounds: tuple[int, int, int, int]) -> None:
        self.hwnd = hwnd
        self.bounds = bounds

    def get_foreground_context(self) -> WindowContext:
        return WindowContext(
            hwnd=self.hwnd,
            title="Test App",
            process_name="testapp.exe",
            pid=1234,
            bounds=self.bounds,
            minimized=False,
        )

    def get_window_context(self, hwnd: int) -> WindowContext | None:
        if hwnd == self.hwnd:
            return self.get_foreground_context()
        return None

    def is_window_valid(self, hwnd: int) -> bool:
        return hwnd == self.hwnd

    def is_minimized(self, hwnd: int) -> bool:
        return False

    def get_extended_frame_bounds(self, hwnd: int) -> tuple[int, int, int, int] | None:
        return self.bounds if hwnd == self.hwnd else None

    def get_monitor_work_area(self, hwnd: int) -> tuple[int, int, int, int] | None:
        return (0, 0, 1920, 1040)

    def get_window_at_point(self, x: int, y: int) -> WindowContext | None:
        return self.get_foreground_context()


def _make_mock_monitor_api(
    *,
    fg_hwnd: int = 888,
    win_bounds: tuple[int, int, int, int] = (0, 0, 2560, 1440),
    mon_bounds: tuple[int, int, int, int] = (0, 0, 2560, 1440),
) -> Win32API:
    import ctypes

    mock_u32 = MagicMock()
    mock_k32 = MagicMock()
    mock_dwm = MagicMock()

    mock_u32.SetProcessDpiAwarenessContext.return_value = 1
    mock_u32.GetForegroundWindow.return_value = fg_hwnd
    mock_u32.IsIconic.return_value = 0
    mock_u32.IsZoomed.return_value = 0
    title = "High-DPI Studio"
    exe = r"C:\Apps\Studio\studio.exe"
    mock_u32.GetWindowTextLengthW.return_value = len(title)

    def fake_get_text(_hwnd: int, buf: object, max_len: int) -> int:
        ctypes.memmove(buf, ctypes.create_unicode_buffer(title, max_len), len(title) * 2)  # type: ignore[arg-type]
        return len(title)

    mock_u32.GetWindowTextW.side_effect = fake_get_text

    def fake_get_pid(_hwnd: int, pid_ptr: object) -> int:
        pid_ptr._obj.value = 4321  # type: ignore[attr-defined]
        return 1

    mock_u32.GetWindowThreadProcessId.side_effect = fake_get_pid
    mock_k32.OpenProcess.return_value = 777
    mock_k32.CloseHandle.return_value = 1

    def fake_query_name(_h: int, _f: int, buf: object, _s: object) -> int:
        ctypes.memmove(buf, ctypes.create_unicode_buffer(exe, 1024), (len(exe) + 1) * 2)  # type: ignore[arg-type]
        return 1

    mock_k32.QueryFullProcessImageNameW.side_effect = fake_query_name

    def fake_enum_monitors(_hdc: object, _clip: object, cb: object, _lparam: int) -> int:
        cb(101, 0, None, 0)
        return 1

    def fake_get_mon_info(_hmon: int, mi_ptr: object) -> int:
        mi = mi_ptr._obj  # type: ignore[attr-defined]
        mi.rcMonitor.left, mi.rcMonitor.top, mi.rcMonitor.right, mi.rcMonitor.bottom = mon_bounds
        mi.rcWork.left, mi.rcWork.top, mi.rcWork.right, mi.rcWork.bottom = mon_bounds
        mi.dwFlags = MONITORINFOF_PRIMARY
        mi.szDevice = r"\\.\DISPLAY1"
        return 1

    mock_u32.EnumDisplayMonitors.side_effect = fake_enum_monitors
    mock_u32.GetMonitorInfoW.side_effect = fake_get_mon_info
    mock_u32.MonitorFromWindow.return_value = 101

    def fake_dwm(_h: int, _attr: int, rect_ptr: object, _sz: int) -> int:
        r = rect_ptr._obj  # type: ignore[attr-defined]
        r.left, r.top, r.right, r.bottom = win_bounds
        return 0

    mock_dwm.DwmGetWindowAttribute.side_effect = fake_dwm
    return Win32API(user32=mock_u32, kernel32=mock_k32, dwmapi=mock_dwm)


# ---------------------------------------------------------------------------
# 1. DWM capture_bounds Parity on capture_window_png, find_window, inspect_screen
# ---------------------------------------------------------------------------


def test_capture_window_png_sets_exact_dwm_and_win_rect_capture_bounds() -> None:
    """WindowCapture.capture_bounds matches exact cropped DWM rect (or win_rect fallback)."""
    mock_user32 = MagicMock()
    mock_gdi32 = MagicMock()
    mock_dwmapi = MagicMock()

    # GetWindowRect includes 8px invisible left/right/bottom drop shadow
    def fake_get_rect(_hwnd: int, ptr: Any) -> int:
        r = getattr(ptr, "_obj", getattr(ptr, "contents", None))
        r.left, r.top, r.right, r.bottom = 92, 100, 1108, 908
        return 1

    # DWM extended frame bounds = visible window (100, 100, 1100, 900)
    def fake_dwm(_hwnd: int, _attr: int, ptr: Any, _size: int) -> int:
        r = getattr(ptr, "_obj", getattr(ptr, "contents", None))
        r.left, r.top, r.right, r.bottom = 100, 100, 1100, 900
        return 0

    mock_user32.GetWindowRect.side_effect = fake_get_rect
    mock_user32.GetDC.return_value = 100
    mock_user32.GetForegroundWindow.return_value = 9999
    mock_gdi32.CreateCompatibleDC.return_value = 200
    mock_gdi32.CreateCompatibleBitmap.return_value = 300
    mock_gdi32.SelectObject.return_value = 400
    mock_user32.PrintWindow.return_value = 1
    mock_dwmapi.DwmGetWindowAttribute.side_effect = fake_dwm
    mock_gdi32.GetDIBits.side_effect = lambda dc, bmp, s, lines, buf, bmi, u: (
        buf.__setitem__(slice(0, 4), b"\x10\x20\x30\xff") or lines
    )

    backend = Win32ScreenVisionBackend(
        user32=mock_user32,
        gdi32=mock_gdi32,
        dwmapi=mock_dwmapi,
    )
    cap_dwm = backend.capture_window_png(777)
    assert cap_dwm.capture_bounds == (100, 100, 1100, 900)
    assert cap_dwm.width == 1000
    assert cap_dwm.height == 800

    # Without DWM available, capture_bounds falls back cleanly to exact win_rect
    backend_no_dwm = Win32ScreenVisionBackend(
        user32=mock_user32,
        gdi32=mock_gdi32,
        dwmapi=None,
    )
    cap_raw = backend_no_dwm.capture_window_png(777)
    assert cap_raw.capture_bounds == (92, 100, 1108, 908)
    assert cap_raw.width == 1016
    assert cap_raw.height == 808


def test_find_window_populates_dwm_bounds_matching_get_foreground_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """find_window() resolves DWM extended frame bounds via get_window_bounds(hwnd)."""
    mock_user32 = MagicMock()
    mock_gdi32 = MagicMock()
    mock_kernel32 = MagicMock()

    mock_user32.IsWindowVisible.return_value = 1
    mock_user32.GetWindowLongW.return_value = 0
    mock_user32.IsIconic.return_value = 0
    mock_user32.GetForegroundWindow.return_value = 42

    # GetWindowRect returns oversized shadow bounds (-8, -8, 1928, 1048)
    def fake_get_rect(_hwnd: int, ptr: Any) -> int:
        r = getattr(ptr, "_obj", getattr(ptr, "contents", None))
        r.left, r.top, r.right, r.bottom = -8, -8, 1928, 1048
        return 1

    mock_user32.GetWindowRect.side_effect = fake_get_rect
    mock_user32.EnumWindows.side_effect = lambda cb, lp: (cb(42, lp), 1)[1]

    monkeypatch.setattr(vision_mod, "get_window_title", lambda _h: "Settings")
    monkeypatch.setattr(vision_mod, "get_window_process_name", lambda _h: "SystemSettings.exe")
    monkeypatch.setattr(vision_mod, "get_window_bounds", lambda _h: (0, 0, 1920, 1040))

    backend = Win32ScreenVisionBackend(
        user32=mock_user32,
        gdi32=mock_gdi32,
        kernel32=mock_kernel32,
    )
    fg = backend.get_foreground_window()
    found = backend.find_window("settings")

    assert fg is not None and found is not None
    assert found.bounds == (0, 0, 1920, 1040)
    assert found.bounds == fg.bounds


def test_inspect_screen_uses_capture_bounds_and_propagates_hwnd_and_capture_id(
    tmp_path: Path,
) -> None:
    """inspect_screen derives geometry from capture.capture_bounds and stores hwnd/capture_id."""
    clear_last_screen_geometry()
    buf = io.BytesIO()
    Image.new("RGB", (1000, 800), color=(40, 80, 120)).save(buf, format="PNG")
    png_bytes = buf.getvalue()

    class _FakeBackend:
        def get_foreground_window(self) -> WindowInfo:
            # Deliberately mismatched win.bounds (includes 8px drop shadow)
            return WindowInfo(
                hwnd=321,
                title="Notes",
                process_name="notepad.exe",
                bounds=(92, 192, 1108, 1008),
            )

        def find_window(self, query: str) -> WindowInfo | None:
            return self.get_foreground_window()

        def is_minimized(self, hwnd: int) -> bool:
            return False

        def probe_uia_text(self, hwnd: int, max_chars: int = 3000) -> str:
            return "Hello"

        def capture_window_png(self, hwnd: int) -> WindowCapture:
            return WindowCapture(
                png_bytes=png_bytes,
                width=1000,
                height=800,
                window_title="Notes",
                process_name="notepad.exe",
                capture_bounds=(100, 200, 1100, 1000),
            )

    svc = ScreenVisionService(
        backend=_FakeBackend(),
        artifact_store=ArtifactStore(tmp_path),
        desktop_name_fn=lambda: "Default",
    )
    res = svc.inspect(target="active", include_image=True)
    assert res.status == "ok"
    assert res.hwnd == 321
    assert res.capture_id is not None and res.capture_id.startswith("cap_")
    assert res.capture_bounds == (100, 200, 1100, 1000)
    assert res.virtual_left == 100
    assert res.virtual_top == 200
    assert res.screen_width == 1000
    assert res.screen_height == 800

    res_dict = res.to_dict(include_attachment=False)
    assert res_dict["hwnd"] == 321
    assert res_dict["capture_id"] == res.capture_id
    assert res_dict["capture_bounds"] == (100, 200, 1100, 1000)

    geom = get_last_screen_geometry()
    assert geom is not None
    assert geom["hwnd"] == 321
    assert geom["capture_id"] == res.capture_id
    assert geom["capture_bounds"] == (100, 200, 1100, 1000)
    assert geom["virtual_left"] == 100
    assert geom["virtual_top"] == 200
    assert geom["screen_width"] == 1000
    assert geom["screen_height"] == 800
    clear_last_screen_geometry()


# ---------------------------------------------------------------------------
# 2. Omitted coord_space Inheritance vs Explicit coord_space="screen"
# ---------------------------------------------------------------------------


def test_gui_action_omitted_coord_space_inherits_active_normalized_1000() -> None:
    """Pointer actions omitting coord_space inherit active capture's normalized_1000 geometry."""
    clear_last_screen_geometry()
    try:
        u32 = _FakePointerUser32(foreground_hwnd=555)
        u32.window_rects[555] = (200, 100, 1200, 900)
        set_last_screen_geometry(
            image_width=1000,
            image_height=800,
            screen_width=1000,
            screen_height=800,
            virtual_left=200,
            virtual_top=100,
            coord_space="normalized_1000",
            hwnd=555,
            capture_id="cap_test_001",
            capture_bounds=(200, 100, 1200, 900),
        )

        # Center (500, 500) in normalized_1000 on [200..1200, 100..900] -> (700, 500)
        res = gui_action("click_at", x=500, y=500, user32=u32)
        assert res["ok"] is True
        assert res["coord_space"] == "normalized_1000"
        assert res["physical_x"] == 700
        assert res["physical_y"] == 500
        assert res["expected_hwnd"] == 555
        assert res["actual_hwnd"] == 555
        assert u32.cursor_moves == [(700, 500)]

        # Explicit coord_space="screen" stays raw physical pixels
        u32.cursor_moves.clear()
        res_raw = gui_action("click_at", x=500, y=500, coord_space="screen", user32=u32)
        assert res_raw["ok"] is True
        assert res_raw["coord_space"] == "screen"
        assert res_raw["physical_x"] == 500
        assert res_raw["physical_y"] == 500
        assert u32.cursor_moves == [(500, 500)]

        # Move action also inherits active normalized_1000
        u32.cursor_moves.clear()
        res_move = gui_action("move", x=250, y=750, user32=u32)
        assert res_move["ok"] is True
        assert res_move["coord_space"] == "normalized_1000"
        assert res_move["physical_x"] == 450
        assert res_move["physical_y"] == 700
        assert u32.cursor_moves == [(450, 700)]
    finally:
        clear_last_screen_geometry()


# ---------------------------------------------------------------------------
# 3. Stale Screen Geometry Abort on Move/Resize
# ---------------------------------------------------------------------------


def test_gui_action_aborts_stale_screen_geometry_when_window_moves_or_resizes() -> None:
    """Clicking or moving aborts cleanly with stale_screen_geometry when bounds drift > 12px."""
    clear_last_screen_geometry()
    try:
        u32 = _FakePointerUser32(foreground_hwnd=900)
        focus_backend = _FakeFocusBackend(hwnd=900, bounds=(100, 100, 1100, 900))
        set_last_screen_geometry(
            image_width=1000,
            image_height=800,
            screen_width=1000,
            screen_height=800,
            virtual_left=100,
            virtual_top=100,
            coord_space="normalized_1000",
            hwnd=900,
            capture_id="cap_stale_1",
            capture_bounds=(100, 100, 1100, 900),
        )

        # Small jitter within <= 12px tolerance succeeds
        focus_backend.bounds = (108, 105, 1108, 905)
        res_ok = gui_action(
            "click_at",
            x=500,
            y=500,
            expected_hwnd=900,
            focus_backend=focus_backend,
            user32=u32,
        )
        assert res_ok["ok"] is True
        assert len(u32.sent_inputs) > 0

        # Window moved by 150px horizontally -> abort cleanly before dispatching any click!
        u32.cursor_moves.clear()
        u32.sent_inputs.clear()
        focus_backend.bounds = (250, 100, 1250, 900)
        res_moved = gui_action(
            "click_at",
            x=500,
            y=500,
            expected_hwnd=900,
            focus_backend=focus_backend,
            user32=u32,
        )
        assert res_moved["ok"] is False
        assert res_moved["status"] == "aborted"
        assert res_moved["reason"] == "stale_screen_geometry"
        assert res_moved["action"] == "click_at"
        assert res_moved["goal_verified"] is False
        assert res_moved["verification_scope"] == "dispatch_only"
        assert u32.cursor_moves == []
        assert u32.sent_inputs == []

        # Window resized vertically by 80px -> move also aborts cleanly
        focus_backend.bounds = (100, 100, 1100, 980)
        res_resized = gui_action(
            "move",
            x=500,
            y=500,
            expected_hwnd=900,
            capture_bounds=(100, 100, 1100, 900),
            focus_backend=focus_backend,
            user32=u32,
        )
        assert res_resized["ok"] is False
        assert res_resized["status"] == "aborted"
        assert res_resized["reason"] == "stale_screen_geometry"
        assert res_resized["action"] == "move"
        assert res_resized["goal_verified"] is False
        assert res_resized["verification_scope"] == "dispatch_only"
        assert u32.cursor_moves == []
    finally:
        clear_last_screen_geometry()


# ---------------------------------------------------------------------------
# 4. screen.py 1568px Cap on active_window & Encoded Dimension Parity
# ---------------------------------------------------------------------------


def test_screen_capture_active_window_caps_at_1568px_and_reports_encoded_dims() -> None:
    """active_window caps longest side to 1568px and reports width/height == image_width/height."""
    api = _make_mock_monitor_api(
        fg_hwnd=888,
        win_bounds=(0, 0, 2560, 1440),
        mon_bounds=(0, 0, 2560, 1440),
    )
    canvas = Image.new("RGB", (2560, 1440), color=(30, 60, 90))
    svc = ScreenCaptureService(api=api, grabber=lambda *a, **k: canvas.copy())

    res = svc.capture(target="active_window")
    assert res.blocked is False
    assert res.image_bytes is not None

    decoded = Image.open(io.BytesIO(res.image_bytes))
    expected_h = int(round(1440 * (MAX_VISION_DIMENSION / 2560.0)))
    assert decoded.size == (MAX_VISION_DIMENSION, expected_h)
    assert res.width == MAX_VISION_DIMENSION
    assert res.height == expected_h
    assert res.image_width == res.width
    assert res.image_height == res.height
    assert res.screen_width == 2560
    assert res.screen_height == 1440
    assert res.virtual_left == 0
    assert res.virtual_top == 0
    assert res.capture_bounds == (0, 0, 2560, 1440)
    assert res.capture_id is not None and res.capture_id.startswith("cap_")


# ---------------------------------------------------------------------------
# 5. Generalized Windows URL Protocol Registry Routing & Dangerous Blocklist
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("dangerous_scheme", sorted(_DANGEROUS_URL_SCHEMES))
def test_dangerous_url_schemes_blocked_even_if_registry_lookup_true(
    dangerous_scheme: str,
) -> None:
    """Dangerous schemes are unconditionally blocked regardless of registry state."""
    assert is_registered_url_protocol(dangerous_scheme, registry_lookup=lambda _s: True) is False
    with pytest.raises(ValueError, match="Disallowed URL scheme"):
        open_url(
            f"{dangerous_scheme}:payload",
            registry_lookup=lambda _s: True,
            startfile_fn=lambda _u: None,
        )


def test_open_url_custom_protocol_routing_and_verification() -> None:
    """Registered OS URL protocols launch via startfile without https:// mangling."""
    launched: list[str] = []

    fp_before = WindowViewFingerprint(
        hwnd=100,
        dhash_hex="0" * 64,
        heading="Home",
        captured_at=1.0,
        source="injected",
    )
    fp_after_changed = WindowViewFingerprint(
        hwnd=100,
        dhash_hex="f" * 64,
        heading="Liked Songs",
        captured_at=2.0,
        source="injected",
    )
    fps = iter([fp_before, fp_after_changed])

    assert is_registered_url_protocol("spotify", registry_lookup=lambda s: s == "spotify") is True
    assert (
        is_registered_url_protocol("unknownproto", registry_lookup=lambda s: s == "spotify")
        is False
    )

    res_verified = open_url(
        "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M",
        registry_lookup=lambda s: s == "spotify",
        startfile_fn=launched.append,
        desktop_name_fn=lambda: "Default",
        view_fingerprint_fn=lambda _h=None: next(fps),
    )
    assert launched == ["spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"]
    assert res_verified["ok"] is True
    assert res_verified["url"] == "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M"
    assert res_verified["scheme"] == "spotify"
    assert res_verified["protocol_scheme"] == "spotify"
    assert res_verified["view_changed"] is True
    assert res_verified["goal_verified"] is True
    assert res_verified["verification_scope"] == "in_app_view"

    # When fingerprint does not change and no window changes -> dispatch_only, goal_verified=False
    fps_same = iter([fp_before, fp_before])
    res_unverified = open_url(
        "spotify:playlist:37i9dQZF1DXcBWIGoYBM5M",
        registry_lookup=lambda s: s == "spotify",
        startfile_fn=launched.append,
        desktop_name_fn=lambda: "Default",
        view_fingerprint_fn=lambda _h=None: next(fps_same),
    )
    assert res_unverified["ok"] is True
    assert res_unverified["view_changed"] is False
    assert res_unverified["goal_verified"] is False
    assert res_unverified["verification_scope"] == "dispatch_only"


@pytest.mark.parametrize(
    "bad_custom_uri",
    [
        "spotify:track:123\ncalc",
        "spotify:track:123\rcalc",
        "spotify:track:123 calc",
        "spotify:track:123|calc",
        "spotify:track:123;calc",
        "spotify:track:123&calc",
        "spotify:track:123&&calc",
        "spotify:track:123`calc`",
        "spotify:track:123$(calc)",
        "unregisteredproto:something",
        "fakeapp://open/item",
    ],
)
def test_open_url_rejects_unregistered_and_injected_custom_uris(bad_custom_uri: str) -> None:
    """Unregistered schemes and control/whitespace/metacharacter injections raise ValueError."""
    launched: list[str] = []
    with pytest.raises(ValueError):
        open_url(
            bad_custom_uri,
            registry_lookup=lambda s: s == "spotify",
            startfile_fn=launched.append,
        )
    assert launched == []


# ---------------------------------------------------------------------------
# 6. _ALLOWED_MS_SETTINGS Expansion & open_settings View Verification
# ---------------------------------------------------------------------------


def test_allowed_ms_settings_expansion_and_view_verification() -> None:
    """New Windows 11 Settings pages are allowlisted and open_settings verifies view delta."""
    required_pages = {
        "ms-settings:system",
        "ms-settings:privacy-microphone",
        "ms-settings:privacy-webcam",
        "ms-settings:privacy-location",
        "ms-settings:home",
        "ms-settings:accounts",
        "ms-settings:windowsdefender",
        "ms-settings:storagepolicies",
        "ms-settings:clipboard",
        "ms-settings:startupapps",
    }
    assert required_pages.issubset(_ALLOWED_MS_SETTINGS)

    opened: list[str] = []
    fp1 = WindowViewFingerprint(
        hwnd=200, dhash_hex="0" * 64, heading="Home", captured_at=1.0, source="injected"
    )
    fp2 = WindowViewFingerprint(
        hwnd=200, dhash_hex="f" * 64, heading="Microphone", captured_at=2.0, source="injected"
    )

    fps_changed = iter([fp1, fp2])
    res_ok = control_system(
        "open_settings",
        page="privacy-microphone",
        startfile_fn=opened.append,
        view_fingerprint_fn=lambda _h=None: next(fps_changed),
    )
    assert opened == ["ms-settings:privacy-microphone"]
    assert res_ok["ok"] is True
    assert res_ok["view_changed"] is True
    assert res_ok["goal_verified"] is True
    assert res_ok["verification_scope"] == "in_app_view"

    fps_noop = iter([fp1, fp1])
    res_noop = control_system(
        "open_settings",
        page="system",
        startfile_fn=opened.append,
        view_fingerprint_fn=lambda _h=None: next(fps_noop),
    )
    assert opened[-1] == "ms-settings:system"
    assert res_noop["ok"] is True
    assert res_noop["view_changed"] is False
    assert res_noop["goal_verified"] is False
    assert res_noop["verification_scope"] == "dispatch_only"


# ---------------------------------------------------------------------------
# 7. ui_act(refresh_snapshot=False) Marks Refs Stale on view_changed=True
# ---------------------------------------------------------------------------


def test_ui_act_refresh_snapshot_false_marks_refs_stale_on_view_change() -> None:
    """ui_act(..., refresh_snapshot=False) marks refs stale when view_changed is True."""
    clear_ui_ref_store()
    try:
        set_ui_snapshot_state(
            {
                "ok": True,
                "snapshot_id": "snap_view_1",
                "window_title": "Settings",
                "hwnd": 700,
                "elements": [
                    {
                        "ref": "e1",
                        "role": "button",
                        "name": "Privacy & security",
                        "x": 120,
                        "y": 340,
                        "enabled": True,
                        "focused": False,
                    }
                ],
            }
        )
        assert _UI_REF_STORE.stale is False

        fp_before = WindowViewFingerprint(
            hwnd=700,
            dhash_hex="1" * 64,
            heading="System",
            captured_at=1.0,
            source="injected",
        )
        fp_after = WindowViewFingerprint(
            hwnd=700,
            dhash_hex="e" * 64,
            heading="Privacy & security",
            captured_at=2.0,
            source="injected",
        )
        fps = iter([fp_before, fp_after])
        u32 = _FakePointerUser32(foreground_hwnd=700)

        res = ui_act(
            "e1",
            action="click",
            refresh_snapshot=False,
            user32=u32,
            gui_action_fn=lambda **kw: {"ok": True, "status": "completed"},
            view_fingerprint_fn=lambda _h=None: next(fps),
        )

        assert res["ok"] is True
        assert res["snapshot_refreshed"] is False
        assert res["view_changed"] is True
        assert res["visual_delta"] > 0.04
        assert res["heading_after"] == "Privacy & security"
        assert res["refs_stale"] is True
        assert res["diff"]["view_changed"] is True
        assert res["diff"]["visual_delta"] == res["visual_delta"]
        assert res["diff"]["heading_after"] == "Privacy & security"
        assert _UI_REF_STORE.stale is True

        # Subsequent ui_act without refreshing snapshot rejects the stale ref cleanly
        with pytest.raises(ValueError, match="stale"):
            ui_act(
                "e1",
                action="click",
                refresh_snapshot=False,
                user32=u32,
                gui_action_fn=lambda **kw: {"ok": True, "status": "completed"},
            )
    finally:
        clear_ui_ref_store()

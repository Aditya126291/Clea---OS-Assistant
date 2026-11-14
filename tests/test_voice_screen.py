"""Tests for Voice Slice 2 screen capture and vision tools."""

from __future__ import annotations

import asyncio
import hashlib
import io
from collections.abc import AsyncIterator
from pathlib import Path
from unittest.mock import MagicMock

from PIL import Image

from clea.surfaces.voice.audio import AudioManager
from clea.surfaces.voice.backends.base import (
    ToolCallEvent,
    VoiceBackend,
    VoiceBackendEvent,
    VoiceSessionConfig,
    VoiceToolResponse,
)
from clea.surfaces.voice.config import ScreenBlocklistConfig, VoiceConfig
from clea.surfaces.voice.runner import VoiceRunner
from clea.surfaces.voice.screen import (
    ScreenCaptureService,
    encode_frame,
    encode_native_frame,
    is_window_blocked,
)
from clea.surfaces.voice.wake_word import WakeWordDetector
from clea.surfaces.voice.win32 import (
    MONITORINFOF_PRIMARY,
    Win32API,
    WindowInfo,
)


class FakeLiveBackend(VoiceBackend):
    """Controllable in-memory Live backend for testing."""

    backend_name = "fake-live"

    def __init__(self) -> None:
        self.connected_config: VoiceSessionConfig | None = None
        self.closed = False
        self.sent_audio: list[bytes] = []
        self.sent_tool_responses: list[VoiceToolResponse] = []
        self.sent_video_frames: list[bytes] = []
        self.sent_video_mime_types: list[str] = []
        self.event_queue: asyncio.Queue[VoiceBackendEvent | None] = asyncio.Queue()

    @property
    def model_name(self) -> str:
        return "fake-model"

    async def connect(self, config: VoiceSessionConfig) -> None:
        self.connected_config = config
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        await self.event_queue.put(None)

    async def send_audio(self, pcm_bytes: bytes) -> None:
        self.sent_audio.append(pcm_bytes)

    async def send_video_frame(self, image_bytes: bytes, *, mime_type: str = "image/jpeg") -> None:
        self.sent_video_frames.append(image_bytes)
        self.sent_video_mime_types.append(mime_type)

    async def send_text(self, text: str, *, end_of_turn: bool = True) -> None:
        pass

    async def send_tool_responses(self, responses: list[VoiceToolResponse]) -> None:
        self.sent_tool_responses.extend(responses)

    async def events(self) -> AsyncIterator[VoiceBackendEvent]:
        while True:
            evt = await self.event_queue.get()
            if evt is None:
                break
            yield evt


def create_fake_audio_manager() -> AudioManager:
    audio = AudioManager()
    audio.start = MagicMock()  # type: ignore[method-assign]
    audio.stop = MagicMock()  # type: ignore[method-assign]
    audio.play_audio = MagicMock()  # type: ignore[method-assign]
    audio.stop_output = MagicMock()  # type: ignore[method-assign]
    return audio


def create_fake_wake_word() -> WakeWordDetector:
    ww = WakeWordDetector()
    ww.start = MagicMock()  # type: ignore[method-assign]
    ww.stop = MagicMock()  # type: ignore[method-assign]
    ww.reset = MagicMock()  # type: ignore[method-assign]
    return ww


def test_is_window_blocked() -> None:
    blocklist = ScreenBlocklistConfig(
        window_titles=["*1Password*", "Bitwarden", "Online Banking"],
        processes=["keepass.exe", "*vault*"],
    )

    assert is_window_blocked("My 1Password Vault", "1password.exe", blocklist) is True
    assert is_window_blocked("Bitwarden - Password Manager", "chrome.exe", blocklist) is True
    assert is_window_blocked("Welcome to Online Banking", "msedge.exe", blocklist) is True
    assert is_window_blocked("Personal", "keepass.exe", blocklist) is True
    assert is_window_blocked("Secure Store", "vault_agent.exe", blocklist) is True

    # Non-blocked
    assert is_window_blocked("VS Code - Project", "code.exe", blocklist) is False
    assert is_window_blocked("YouTube - Google Chrome", "chrome.exe", blocklist) is False


def test_encode_frame_resizing_and_jpeg() -> None:
    # 1. Downscales large image proportionally
    large_img = Image.new("RGBA", (1920, 1080), color=(255, 0, 0, 255))
    jpeg_bytes = encode_frame(large_img, max_side=768)
    assert len(jpeg_bytes) > 0

    decoded = Image.open(io.BytesIO(jpeg_bytes))
    assert decoded.format == "JPEG"
    assert decoded.width == 768
    assert decoded.height == 432

    # 2. Leaves small image at original dimensions
    small_img = Image.new("RGB", (300, 200), color=(0, 255, 0))
    small_jpeg = encode_frame(small_img, max_side=768)
    decoded_small = Image.open(io.BytesIO(small_jpeg))
    assert decoded_small.width == 300
    assert decoded_small.height == 200


def test_screen_capture_service_mocked_grabber() -> None:
    fake_img = Image.new("RGB", (800, 600), color=(10, 20, 30))

    def _fake_grab(*args, **kwargs) -> Image.Image:
        return fake_img

    svc = ScreenCaptureService(
        image_max_side=768,
        grabber=_fake_grab,
    )

    res = svc.capture(target="full_screen")
    assert res.blocked is False
    assert res.image_bytes is not None
    assert res.width == 800
    assert res.height == 600


def test_screen_capture_service_active_window_blocklist() -> None:
    blocklist = ScreenBlocklistConfig(window_titles=["*Secret*"])
    svc = ScreenCaptureService(blocklist=blocklist)

    # Mock get_active_window_info
    svc.get_active_window_info = MagicMock(
        return_value=WindowInfo(
            hwnd=1234,
            title="My Secret Notepad",
            process_name="notepad.exe",
            is_visible=True,
        )
    )

    res = svc.capture(target="active_window")
    assert res.blocked is True
    assert res.image_bytes is None
    assert res.window_title == "My Secret Notepad"


async def test_voice_runner_look_at_screen_success(tmp_path: Path) -> None:
    fake_img = Image.new("RGB", (640, 480), color=(50, 100, 150))
    backend = FakeLiveBackend()

    screen_svc = ScreenCaptureService(
        image_max_side=640,
        grabber=lambda *a, **k: fake_img,
    )

    cfg = VoiceConfig(consent_mode="auto_allow")
    runner = VoiceRunner(
        config=cfg,
        data_dir=tmp_path,
        backend_factory=lambda: backend,
        audio_manager=create_fake_audio_manager(),
        wake_word_detector=create_fake_wake_word(),
        screen_service=screen_svc,
        enable_hotkey=False,
    )
    await runner.start()
    await runner.start_call(trigger="test")

    # Trigger look_at_screen
    call_ev = ToolCallEvent(
        call_id="call-look-1",
        name="look_at_screen",
        arguments={"target": "full_screen", "reason": "inspect desktop"},
    )
    await runner._handle_tool_call(call_ev, backend)

    # Frame sent to backend
    assert len(backend.sent_video_frames) == 1
    assert len(backend.sent_tool_responses) == 1
    resp = backend.sent_tool_responses[0]
    assert resp.call_id == "call-look-1"
    assert resp.response["status"] == "success"
    assert resp.response["target"] == "full_screen"

    await runner.stop()


async def test_voice_runner_look_at_screen_denied_by_consent(tmp_path: Path) -> None:
    backend = FakeLiveBackend()

    cfg = VoiceConfig(consent_mode="deny")
    runner = VoiceRunner(
        config=cfg,
        data_dir=tmp_path,
        backend_factory=lambda: backend,
        audio_manager=create_fake_audio_manager(),
        wake_word_detector=create_fake_wake_word(),
        enable_hotkey=False,
    )
    await runner.start()
    await runner.start_call(trigger="test")

    call_ev = ToolCallEvent(
        call_id="call-look-2",
        name="look_at_screen",
        arguments={"target": "full_screen", "reason": "inspect"},
    )
    await runner._handle_tool_call(call_ev, backend)

    assert len(backend.sent_video_frames) == 0
    assert len(backend.sent_tool_responses) == 1
    resp = backend.sent_tool_responses[0]
    assert resp.response["status"] == "denied"
    assert resp.is_error is True

    await runner.stop()


async def test_voice_runner_watch_screen_burst(tmp_path: Path) -> None:
    fake_img = Image.new("RGB", (400, 300), color=(10, 20, 30))
    backend = FakeLiveBackend()

    screen_svc = ScreenCaptureService(
        image_max_side=400,
        grabber=lambda *a, **k: fake_img,
    )

    cfg = VoiceConfig(consent_mode="auto_allow", max_burst_seconds=5.0)
    runner = VoiceRunner(
        config=cfg,
        data_dir=tmp_path,
        backend_factory=lambda: backend,
        audio_manager=create_fake_audio_manager(),
        wake_word_detector=create_fake_wake_word(),
        screen_service=screen_svc,
        enable_hotkey=False,
    )
    await runner.start()
    await runner.start_call(trigger="test")

    call_ev = ToolCallEvent(
        call_id="call-watch-1",
        name="watch_screen",
        arguments={"seconds": 2.0, "reason": "continuous view"},
    )
    await runner._handle_tool_call(call_ev, backend)

    assert len(backend.sent_tool_responses) == 1
    resp = backend.sent_tool_responses[0]
    assert resp.response["status"] == "watching"
    assert resp.response["duration_seconds"] == 2.0

    # Let the burst loop stream for a short duration
    await asyncio.sleep(0.05)
    assert len(backend.sent_video_frames) >= 1

    await runner.end_call(reason="done")
    await runner.stop()


def _make_dual_monitor_mock_api(
    *,
    fg_hwnd: int = 777,
    title: str = "runner.py - Clea - Antigravity",
    exe_path: str = r"C:\Users\test\AppData\Local\Programs\Antigravity\Antigravity.exe",
    win_bounds: tuple[int, int, int, int] = (2100, 120, 3600, 960),
    minimized: bool = False,
) -> Win32API:
    import ctypes

    mock_u32 = MagicMock()
    mock_k32 = MagicMock()
    mock_dwm = MagicMock()

    state = {
        "hwnd": fg_hwnd,
        "title": title,
        "exe": exe_path,
        "bounds": win_bounds,
        "minimized": minimized,
    }

    mon_specs = {
        101: (0, 0, 1920, 1080, 0, 0, 1920, 1032, MONITORINFOF_PRIMARY, r"\\.\DISPLAY1"),
        102: (1920, 0, 3840, 1080, 1920, 0, 3840, 1040, 0, r"\\.\DISPLAY2"),
    }

    mock_u32.SetProcessDpiAwarenessContext.return_value = 1
    mock_u32.GetForegroundWindow.side_effect = lambda: state["hwnd"]
    mock_u32.IsIconic.side_effect = lambda _h: 1 if state["minimized"] else 0
    mock_u32.IsZoomed.return_value = 0
    mock_u32.GetWindowTextLengthW.side_effect = lambda _h: len(state["title"])

    def fake_get_text(_hwnd: int, buf: object, max_len: int) -> int:
        t = str(state["title"])
        ctypes.memmove(buf, ctypes.create_unicode_buffer(t, max_len), len(t) * 2)  # type: ignore[arg-type]
        return len(t)

    mock_u32.GetWindowTextW.side_effect = fake_get_text

    def fake_get_pid(_hwnd: int, pid_ptr: object) -> int:
        pid_ptr._obj.value = 8888  # type: ignore[attr-defined]
        return 1

    mock_u32.GetWindowThreadProcessId.side_effect = fake_get_pid
    mock_k32.OpenProcess.return_value = 5555
    mock_k32.CloseHandle.return_value = 1

    def fake_query_name(_h: int, _f: int, buf: object, _s: object) -> int:
        exe = str(state["exe"])
        ctypes.memmove(buf, ctypes.create_unicode_buffer(exe, 1024), (len(exe) + 1) * 2)  # type: ignore[arg-type]
        return 1

    mock_k32.QueryFullProcessImageNameW.side_effect = fake_query_name

    def fake_enum_monitors(_hdc: object, _clip: object, cb: object, _lparam: int) -> int:
        cb(101, 0, None, 0)
        cb(102, 0, None, 0)
        return 1

    def fake_get_mon_info(hmon: int, mi_ptr: object) -> int:
        spec = mon_specs[hmon]
        mi = mi_ptr._obj  # type: ignore[attr-defined]
        mi.rcMonitor.left, mi.rcMonitor.top, mi.rcMonitor.right, mi.rcMonitor.bottom = spec[0:4]
        mi.rcWork.left, mi.rcWork.top, mi.rcWork.right, mi.rcWork.bottom = spec[4:8]
        mi.dwFlags = spec[8]
        mi.szDevice = spec[9]
        return 1

    mock_u32.EnumDisplayMonitors.side_effect = fake_enum_monitors
    mock_u32.GetMonitorInfoW.side_effect = fake_get_mon_info
    mock_u32.MonitorFromWindow.side_effect = lambda _h, _f: (
        102 if state["bounds"][0] >= 1920 else 101
    )

    def fake_dwm(_h: int, _attr: int, rect_ptr: object, _sz: int) -> int:
        r = rect_ptr._obj  # type: ignore[attr-defined]
        r.left, r.top, r.right, r.bottom = state["bounds"]
        return 0

    mock_dwm.DwmGetWindowAttribute.side_effect = fake_dwm

    api = Win32API(user32=mock_u32, kernel32=mock_k32, dwmapi=mock_dwm)
    api._test_state = state  # type: ignore[attr-defined]
    return api


def test_encode_native_frame_zero_downscale_and_jpeg_fallback() -> None:
    img = Image.new("RGB", (1720, 980), color=(25, 35, 45))
    png_bytes, mime = encode_native_frame(img)
    assert mime == "image/png"
    decoded = Image.open(io.BytesIO(png_bytes))
    assert decoded.format == "PNG"
    assert decoded.size == (1720, 980)

    # Tiny threshold triggers quality=94 JPEG fallback while keeping 1:1 pixel dimensions
    jpeg_bytes, fallback_mime = encode_native_frame(img, max_png_bytes=10)
    assert fallback_mime == "image/jpeg"
    decoded_fb = Image.open(io.BytesIO(jpeg_bytes))
    assert decoded_fb.format == "JPEG"
    assert decoded_fb.size == (1720, 980)


def test_screen_capture_un_downscaled_active_window_multi_monitor_and_no_cache() -> None:
    api = _make_dual_monitor_mock_api()
    canvas = Image.new("RGB", (3840, 1080), color=(10, 10, 10))
    # Monitor 1 = green, Monitor 2 = blue, active window on Monitor 2 = bright orange
    canvas.paste(Image.new("RGB", (1920, 1080), (0, 180, 0)), (0, 0))
    canvas.paste(Image.new("RGB", (1920, 1080), (0, 60, 200)), (1920, 0))
    canvas.paste(Image.new("RGB", (1500, 840), (240, 120, 20)), (2100, 120))

    svc = ScreenCaptureService(
        api=api,
        grabber=lambda *a, **k: canvas.copy(),
    )

    # 1. active_window captures ONLY the 1500x840 window at 1:1 native resolution as PNG
    res1 = svc.capture(target="active_window")
    assert res1.blocked is False
    assert res1.mime_type == "image/png"
    assert res1.width == 1500
    assert res1.height == 840
    assert res1.process_name == "Antigravity.exe"
    assert res1.app_name == "Google Antigravity"
    assert res1.window_title == "runner.py - Clea - Antigravity"
    assert res1.window_bounds == (2100, 120, 3600, 960)
    assert res1.monitor_index == 2
    assert res1.monitor_bounds == (1920, 0, 3840, 1080)
    assert res1.capture_seq == 1
    assert res1.captured_at is not None

    decoded1 = Image.open(io.BytesIO(res1.image_bytes))  # type: ignore[arg-type]
    assert decoded1.size == (1500, 840)
    r, g, b = decoded1.getpixel((10, 10))
    assert r > 200 and g < 160 and b < 60

    # 2. full_screen captures ONLY Monitor 2 (active window's monitor), NOT Monitor 1
    res2 = svc.capture(target="full_screen")
    assert res2.blocked is False
    assert res2.mime_type == "image/jpeg"
    assert res2.width == 1920
    assert res2.height == 1080
    assert res2.monitor_index == 2
    assert res2.monitor_bounds == (1920, 0, 3840, 1080)
    assert res2.process_name == "Antigravity.exe"
    assert res2.app_name == "Google Antigravity"
    assert res2.capture_seq == 2

    decoded2 = Image.open(io.BytesIO(res2.image_bytes))  # type: ignore[arg-type]
    assert decoded2.size == (1920, 1080)
    # Corner pixel (10, 10) is blue (Monitor 2), NOT green (Monitor 1)
    r2, g2, b2 = decoded2.getpixel((10, 10))
    assert b2 > 150 and g2 < 100

    # 3. Consecutive capture after changing desktop reflects new pixels & new app (no caching)
    api._test_state["title"] = "PowerShell 7"  # type: ignore[attr-defined]
    api._test_state["exe"] = r"C:\Program Files\PowerShell\7\pwsh.exe"  # type: ignore[attr-defined]
    api._test_state["bounds"] = (100, 100, 900, 700)  # type: ignore[attr-defined]
    canvas.paste(Image.new("RGB", (800, 600), (220, 20, 220)), (100, 100))

    res3 = svc.capture(target="active_window")
    assert res3.capture_seq == 3
    assert res3.width == 800
    assert res3.height == 600
    assert res3.monitor_index == 1
    assert res3.process_name == "pwsh.exe"
    assert res3.app_name == "PowerShell"
    decoded3 = Image.open(io.BytesIO(res3.image_bytes))  # type: ignore[arg-type]
    r3, g3, b3 = decoded3.getpixel((20, 20))
    assert r3 > 180 and b3 > 180 and g3 < 60


def test_screen_capture_minimized_active_window_falls_back_to_monitor() -> None:
    api = _make_dual_monitor_mock_api(minimized=True)
    canvas = Image.new("RGB", (3840, 1080), (0, 60, 200))
    svc = ScreenCaptureService(api=api, grabber=lambda *a, **k: canvas.copy())

    res = svc.capture(target="active_window")
    assert res.blocked is False
    assert res.window_bounds is None
    assert res.width == 1920
    assert res.height == 1080


async def test_voice_runner_look_at_screen_sends_ground_truth_and_pulses_indicator(
    tmp_path: Path,
) -> None:
    api = _make_dual_monitor_mock_api()
    canvas = Image.new("RGB", (3840, 1080), (40, 80, 120))
    screen_svc = ScreenCaptureService(api=api, grabber=lambda *a, **k: canvas.copy())
    backend = FakeLiveBackend()
    fake_indicator = MagicMock()
    fake_indicator.enabled = True
    fake_indicator.is_watching = False

    cfg = VoiceConfig(consent_mode="auto_allow")
    runner = VoiceRunner(
        config=cfg,
        data_dir=tmp_path,
        backend_factory=lambda: backend,
        audio_manager=create_fake_audio_manager(),
        wake_word_detector=create_fake_wake_word(),
        screen_service=screen_svc,
        screen_indicator=fake_indicator,
        enable_hotkey=False,
    )
    await runner.start()
    await runner.start_call(trigger="test")

    call_ev = ToolCallEvent(
        call_id="call-native-1",
        name="look_at_screen",
        arguments={"target": "active_window", "reason": "read code in Antigravity"},
    )
    await runner._handle_tool_call(call_ev, backend)

    assert len(backend.sent_video_frames) == 1
    assert backend.sent_video_mime_types == ["image/png"]
    resp = backend.sent_tool_responses[0]
    assert resp.response["status"] == "success"
    assert resp.response["target"] == "active_window"
    assert resp.response["mime_type"] == "image/png"
    assert resp.response["active_app_name"] == "Google Antigravity"
    assert resp.response["active_process_name"] == "Antigravity.exe"
    assert resp.response["active_window_title"] == "runner.py - Clea - Antigravity"
    assert resp.response["capture_seq"] == 1
    assert resp.response["captured_at"] is not None
    assert fake_indicator.pulse.called

    await runner.end_call(reason="done")
    assert fake_indicator.hide.called
    await runner.stop()
    assert fake_indicator.stop.called


async def test_voice_runner_watch_screen_starts_updates_and_stops_indicator(
    tmp_path: Path,
) -> None:
    api = _make_dual_monitor_mock_api()
    canvas = Image.new("RGB", (3840, 1080), (40, 80, 120))
    screen_svc = ScreenCaptureService(api=api, grabber=lambda *a, **k: canvas.copy())
    backend = FakeLiveBackend()
    fake_indicator = MagicMock()
    fake_indicator.enabled = True
    fake_indicator.is_watching = False

    cfg = VoiceConfig(consent_mode="auto_allow", max_burst_seconds=5.0)
    runner = VoiceRunner(
        config=cfg,
        data_dir=tmp_path,
        backend_factory=lambda: backend,
        audio_manager=create_fake_audio_manager(),
        wake_word_detector=create_fake_wake_word(),
        screen_service=screen_svc,
        screen_indicator=fake_indicator,
        enable_hotkey=False,
    )
    await runner.start()
    await runner.start_call(trigger="test")

    call_ev = ToolCallEvent(
        call_id="call-watch-native",
        name="watch_screen",
        arguments={"seconds": 2.0, "reason": "watch Antigravity"},
    )
    await runner._handle_tool_call(call_ev, backend)

    assert fake_indicator.start_watching.called
    await asyncio.sleep(0.05)
    assert fake_indicator.update_watch_bounds.called

    await runner.end_call(reason="done")
    assert fake_indicator.hide.called
    await runner.stop()


def test_encode_frame_and_native_fallback_high_entropy_jpeg_does_not_crash() -> None:
    # Deterministic high-entropy 1920x1080 RGB pixels (>2.5 MB PNG so native hits JPEG fallback).
    chunk = hashlib.sha256(b"clea-high-entropy-seed").digest()
    row_bytes = (chunk * ((1920 * 3 // len(chunk)) + 1))[: 1920 * 3]
    buf = bytearray()
    for y in range(1080):
        rot = (y * 17) % len(row_bytes)
        buf.extend(row_bytes[rot:] + row_bytes[:rot])
    # Add fine per-pixel variation so neither PNG nor JPEG subsampling=0 compresses trivially.
    for idx in range(0, len(buf), 7):
        buf[idx] = (buf[idx] + (idx % 251)) & 0xFF
    img = Image.frombytes("RGB", (1920, 1080), bytes(buf))

    jpeg_bytes = encode_frame(img, max_side=1920, quality=90)
    assert len(jpeg_bytes) > 0
    decoded = Image.open(io.BytesIO(jpeg_bytes))
    assert decoded.format == "JPEG"
    assert decoded.size == (1920, 1080)

    native_bytes, mime = encode_native_frame(img, fallback_jpeg_quality=94, max_png_bytes=1024)
    assert mime == "image/jpeg"
    native_decoded = Image.open(io.BytesIO(native_bytes))
    assert native_decoded.format == "JPEG"
    assert native_decoded.size == (1920, 1080)


def test_screen_capture_service_grabber_without_api_does_not_crop_to_desktop() -> None:
    fake_img = Image.new("RGB", (640, 480), color=(20, 40, 60))
    svc = ScreenCaptureService(grabber=lambda *a, **k: fake_img)

    win_res = svc.capture(target="active_window")
    full_res = svc.capture(target="full_screen")

    assert svc._api is None
    assert win_res.width == 640 and win_res.height == 480
    assert full_res.width == 640 and full_res.height == 480
    assert win_res.window_bounds is None


def test_screen_capture_service_clips_offscreen_window_bounds() -> None:
    # Window overhangs left edge of monitor 1 (-12 px -> clipped to 0..800 = 800 px wide).
    api = _make_dual_monitor_mock_api(win_bounds=(-12, 40, 800, 600))
    canvas = Image.new("RGB", (3840, 1080), (40, 80, 120))
    svc = ScreenCaptureService(api=api, grabber=lambda *a, **k: canvas.copy())

    res = svc.capture(target="active_window")
    assert res.image_bytes is not None
    assert res.width == 800
    assert res.height == 560

    # Completely off-screen bounds fall back cleanly to target monitor bounds instead of black void.
    off_api = _make_dual_monitor_mock_api(win_bounds=(5000, 5000, 5800, 5600))
    off_svc = ScreenCaptureService(api=off_api, grabber=lambda *a, **k: canvas.copy())
    off_res = off_svc.capture(target="active_window")
    assert off_res.image_bytes is not None
    assert off_res.width == 1920
    assert off_res.height == 1080


async def test_voice_runner_look_at_screen_full_screen_also_sends_native_active_window_crop(
    tmp_path: Path,
) -> None:
    api = _make_dual_monitor_mock_api()
    canvas = Image.new("RGB", (3840, 1080), (40, 80, 120))
    screen_svc = ScreenCaptureService(api=api, grabber=lambda *a, **k: canvas.copy())
    backend = FakeLiveBackend()
    fake_indicator = MagicMock()
    fake_indicator.enabled = True
    fake_indicator.is_watching = False

    cfg = VoiceConfig(consent_mode="auto_allow")
    runner = VoiceRunner(
        config=cfg,
        data_dir=tmp_path,
        backend_factory=lambda: backend,
        audio_manager=create_fake_audio_manager(),
        wake_word_detector=create_fake_wake_word(),
        screen_service=screen_svc,
        screen_indicator=fake_indicator,
        enable_hotkey=False,
    )
    await runner.start()
    await runner.start_call(trigger="test")

    call_ev = ToolCallEvent(
        call_id="call-full-dual",
        name="look_at_screen",
        arguments={"target": "full_screen", "reason": "inspect desktop and code"},
    )
    await runner._handle_tool_call(call_ev, backend)

    assert len(backend.sent_video_frames) == 2
    assert backend.sent_video_mime_types == ["image/jpeg", "image/png"]
    full_img = Image.open(io.BytesIO(backend.sent_video_frames[0]))
    native_img = Image.open(io.BytesIO(backend.sent_video_frames[1]))
    assert full_img.size == (1920, 1080)
    assert native_img.size == (1500, 840)

    resp = backend.sent_tool_responses[0]
    assert resp.response["status"] == "success"
    assert resp.response["frames_sent"] == 2
    assert resp.response["active_window_mime_type"] == "image/png"
    assert resp.response["active_window_width"] == 1500
    assert resp.response["active_window_height"] == 840

    await runner.end_call(reason="done")
    await runner.stop()


async def test_voice_runner_watch_screen_respects_blocklist(tmp_path: Path) -> None:
    blocklist = ScreenBlocklistConfig(
        window_titles=["*1Password*"],
        processes=["1Password.exe"],
    )
    api = _make_dual_monitor_mock_api(
        title="1Password - Personal Vault",
        exe_path=r"C:\Program Files\1Password\1Password.exe",
    )
    canvas = Image.new("RGB", (3840, 1080), (40, 80, 120))
    screen_svc = ScreenCaptureService(
        api=api,
        blocklist=blocklist,
        grabber=lambda *a, **k: canvas.copy(),
    )
    backend = FakeLiveBackend()
    fake_indicator = MagicMock()
    fake_indicator.enabled = True
    fake_indicator.is_watching = False

    cfg = VoiceConfig(consent_mode="auto_allow", max_burst_seconds=5.0, blocklist=blocklist)
    runner = VoiceRunner(
        config=cfg,
        data_dir=tmp_path,
        backend_factory=lambda: backend,
        audio_manager=create_fake_audio_manager(),
        wake_word_detector=create_fake_wake_word(),
        screen_service=screen_svc,
        screen_indicator=fake_indicator,
        enable_hotkey=False,
    )
    await runner.start()
    await runner.start_call(trigger="test")

    call_ev = ToolCallEvent(
        call_id="call-watch-blocked",
        name="watch_screen",
        arguments={"seconds": 2.0, "reason": "assist"},
    )
    await runner._handle_tool_call(call_ev, backend)

    assert len(backend.sent_video_frames) == 0
    fake_indicator.start_watching.assert_not_called()
    resp = backend.sent_tool_responses[0]
    assert resp.is_error is True
    assert resp.response["status"] == "blocked"

    await runner.stop()

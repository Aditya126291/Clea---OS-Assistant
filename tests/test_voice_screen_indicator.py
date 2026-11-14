"""Unit tests for the Win32 top-edge red screen-monitoring overlay indicator."""

from __future__ import annotations

import sys
import threading
import time
from pathlib import Path

import pytest

from clea.surfaces.voice.audio import AudioManager
from clea.surfaces.voice.backends.base import (
    VoiceBackend,
    VoiceSessionConfig,
    VoiceToolResponse,
)
from clea.surfaces.voice.config import VoiceConfig
from clea.surfaces.voice.runner import VoiceRunner
from clea.surfaces.voice.screen_indicator import (
    DEFAULT_INDICATOR_COLOR_RGB,
    INDICATOR_EX_STYLE,
    WDA_EXCLUDEFROMCAPTURE,
    WS_EX_LAYERED,
    WS_EX_NOACTIVATE,
    WS_EX_TOOLWINDOW,
    WS_EX_TOPMOST,
    WS_EX_TRANSPARENT,
    ScreenIndicator,
    compute_bar_rect,
    rgb_to_colorref,
)
from clea.surfaces.voice.wake_word import WakeWordDetector


class _FakeOverlayBackend:
    """Thread-safe in-memory indicator backend exercising the message loop thread."""

    def __init__(self) -> None:
        self.thread_id = 42
        self.hwnd = 9001
        self.created_colorref: int | None = None
        self.created_rect: tuple[int, int, int, int] | None = None
        self.show_history: list[tuple[int, int, int, int]] = []
        self.hide_count = 0
        self.destroyed = False
        self.timer_ms: int | None = None
        self._sync_cb = None
        self._queue_lock = threading.Lock()
        self._events: list[str] = []
        self._wake = threading.Condition()

    def set_sync_callback(self, callback: object) -> None:
        self._sync_cb = callback  # type: ignore[assignment]

    def create(self, colorref: int, initial_rect: tuple[int, int, int, int]) -> None:
        self.created_colorref = colorref
        self.created_rect = initial_rect

    def show(self, rect: tuple[int, int, int, int]) -> None:
        self.show_history.append(rect)

    def hide(self) -> None:
        self.hide_count += 1

    def start_timer(self, interval_ms: int) -> None:
        self.timer_ms = interval_ms

    def stop_timer(self) -> None:
        self.timer_ms = None

    def request_sync(self) -> None:
        with self._wake:
            self._events.append("sync")
            self._wake.notify_all()

    def request_quit(self) -> None:
        with self._wake:
            self._events.append("quit")
            self._wake.notify_all()

    def pump_once(self) -> bool:
        with self._wake:
            while not self._events:
                timeout = (self.timer_ms / 1000.0) if self.timer_ms else None
                notified = self._wake.wait(timeout=timeout)
                if not notified and self.timer_ms:
                    self._events.append("timer")
                    break
            ev = self._events.pop(0)
        if ev == "quit":
            return False
        if ev in ("sync", "timer") and self._sync_cb is not None:
            self._sync_cb()
        return True

    def destroy(self) -> None:
        self.destroyed = True


def _wait_until(predicate: object, timeout: float = 2.0) -> bool:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():  # type: ignore[operator]
            return True
        time.sleep(0.01)
    return bool(predicate())  # type: ignore[operator]


def test_indicator_styles_colorref_and_bar_geometry() -> None:
    assert rgb_to_colorref(DEFAULT_INDICATOR_COLOR_RGB) == 0x004444EF
    assert WDA_EXCLUDEFROMCAPTURE == 0x00000011
    assert INDICATOR_EX_STYLE == (
        WS_EX_TOPMOST | WS_EX_LAYERED | WS_EX_TRANSPARENT | WS_EX_TOOLWINDOW | WS_EX_NOACTIVATE
    )
    assert compute_bar_rect((0, 0, 1920, 1080), height_px=3) == (0, 0, 1920, 3)
    assert compute_bar_rect((-1920, 100, 0, 1180), height_px=4) == (-1920, 100, 1920, 4)


def test_screen_indicator_pulse_watch_reposition_and_clean_stop() -> None:
    backends: list[_FakeOverlayBackend] = []

    def make_backend() -> _FakeOverlayBackend:
        b = _FakeOverlayBackend()
        backends.append(b)
        return b

    indicator = ScreenIndicator(
        enabled=True,
        height_px=3,
        pulse_seconds=0.15,
        backend_factory=make_backend,
    )
    indicator.start()
    assert len(backends) == 1
    backend = backends[0]
    assert backend.created_colorref == 0x004444EF
    assert indicator.is_visible is False

    # 1. Single capture pulse shows immediately and extends on consecutive pulse
    indicator.pulse((0, 0, 1920, 1080), duration_seconds=0.15)
    assert indicator.is_visible is True
    assert _wait_until(lambda: (0, 0, 1920, 3) in backend.show_history)

    # Extend timer back-to-back
    hides_before = backend.hide_count
    indicator.pulse((0, 0, 1920, 1080), duration_seconds=0.25)
    time.sleep(0.08)
    assert indicator.is_visible is True

    # Auto-hides once extended pulse expires
    assert _wait_until(lambda: backend.hide_count > hides_before, timeout=1.0)
    assert indicator.is_visible is False

    # 2. Continuous watching shows, repositions across monitors, and hides immediately
    indicator.start_watching((0, 0, 1920, 1080))
    assert indicator.is_watching is True
    assert indicator.is_visible is True
    assert _wait_until(lambda: backend.show_history[-1] == (0, 0, 1920, 3))

    indicator.update_watch_bounds((1920, 0, 3840, 1080))
    assert _wait_until(lambda: backend.show_history[-1] == (1920, 0, 1920, 3))

    indicator.stop_watching()
    assert indicator.is_watching is False
    assert indicator.is_visible is False
    assert _wait_until(lambda: backend.hide_count > hides_before + 1)

    # 3. Clean thread shutdown destroys overlay window
    indicator.stop()
    assert backend.destroyed is True
    assert indicator._thread is None


def test_screen_indicator_disabled_and_non_windows_noop(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    factory_called = False

    def fail_factory() -> _FakeOverlayBackend:
        nonlocal factory_called
        factory_called = True
        return _FakeOverlayBackend()

    disabled = ScreenIndicator(enabled=False, backend_factory=fail_factory)
    disabled.start()
    disabled.pulse((0, 0, 1920, 1080))
    disabled.start_watching((0, 0, 1920, 1080))
    assert disabled.is_visible is False
    assert disabled.is_watching is False
    assert factory_called is False
    disabled.stop()

    monkeypatch.setattr(sys, "platform", "linux")
    non_win = ScreenIndicator(enabled=True)
    assert non_win.is_supported is False
    non_win.start()
    non_win.pulse((0, 0, 1920, 1080))
    non_win.start_watching((0, 0, 1920, 1080))
    non_win.stop_watching()
    non_win.hide()
    non_win.stop()


@pytest.mark.skipif(sys.platform != "win32", reason="Real Win32 overlay requires Windows")
def test_screen_indicator_real_win32_window_lifecycle() -> None:
    import ctypes
    import ctypes.wintypes as wintypes

    indicator = ScreenIndicator(enabled=True, height_px=3, pulse_seconds=0.2)
    indicator.start()
    try:
        assert indicator._backend is not None
        hwnd = indicator._backend.hwnd
        assert hwnd is not None

        u32 = ctypes.WinDLL("user32", use_last_error=True)
        u32.GetWindowLongW.argtypes = [wintypes.HWND, ctypes.c_int]
        u32.GetWindowLongW.restype = ctypes.c_long
        u32.IsWindowVisible.argtypes = [wintypes.HWND]
        u32.IsWindowVisible.restype = wintypes.BOOL
        u32.GetWindowRect.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.RECT)]
        u32.GetWindowRect.restype = wintypes.BOOL
        ex_style = u32.GetWindowLongW(hwnd, -20)  # GWL_EXSTYLE
        assert (ex_style & INDICATOR_EX_STYLE) == INDICATOR_EX_STYLE

        if hasattr(u32, "GetWindowDisplayAffinity"):
            u32.GetWindowDisplayAffinity.argtypes = [wintypes.HWND, ctypes.POINTER(wintypes.DWORD)]
            u32.GetWindowDisplayAffinity.restype = wintypes.BOOL
            affinity = wintypes.DWORD(0)
            if u32.GetWindowDisplayAffinity(hwnd, ctypes.byref(affinity)):
                assert affinity.value == WDA_EXCLUDEFROMCAPTURE

        def _wait_visible(expected: bool, timeout: float = 1.0) -> bool:
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if bool(u32.IsWindowVisible(hwnd)) is expected:
                    return True
                time.sleep(0.02)
            return bool(u32.IsWindowVisible(hwnd)) is expected

        indicator.pulse((0, 0, 800, 600), duration_seconds=0.2)
        assert indicator.is_visible is True
        assert _wait_visible(True) is True

        rc = wintypes.RECT()
        assert u32.GetWindowRect(hwnd, ctypes.byref(rc))
        assert (rc.left, rc.top, rc.right, rc.bottom) == (0, 0, 800, 3)

        # Pulse timer must automatically hide the real HWND when duration elapses.
        assert _wait_visible(False, timeout=1.0) is True
        assert indicator.is_visible is False

        indicator.start_watching((0, 0, 800, 600))
        assert indicator.is_watching is True
        assert _wait_visible(True) is True

        indicator.stop_watching()
        assert indicator.is_visible is False
        assert _wait_visible(False) is True
    finally:
        indicator.stop()
    assert indicator._thread is None

    # Calls after stop() remain strict no-ops.
    indicator.pulse((0, 0, 800, 600), duration_seconds=0.5)
    indicator.start_watching((0, 0, 800, 600))
    assert indicator.is_visible is False
    assert indicator.is_watching is False
    assert indicator._thread is None


async def test_voice_runner_respects_screen_indicator_disabled_config(tmp_path: Path) -> None:
    class _NoopBackend(VoiceBackend):
        backend_name = "noop"

        @property
        def model_name(self) -> str:
            return "noop"

        async def connect(self, config: VoiceSessionConfig) -> None:
            pass

        async def close(self) -> None:
            pass

        async def send_audio(self, pcm_bytes: bytes) -> None:
            pass

        async def send_video_frame(
            self, image_bytes: bytes, *, mime_type: str = "image/jpeg"
        ) -> None:
            pass

        async def send_text(self, text: str, *, end_of_turn: bool = True) -> None:
            pass

        async def send_tool_responses(self, responses: list[VoiceToolResponse]) -> None:
            pass

        async def events(self):  # type: ignore[override]
            if False:
                yield None

    audio = AudioManager()
    audio.start = lambda *a, **k: None  # type: ignore[method-assign]
    audio.stop = lambda *a, **k: None  # type: ignore[method-assign]
    audio.stop_output = lambda *a, **k: None  # type: ignore[method-assign]
    ww = WakeWordDetector()
    ww.start = lambda *a, **k: None  # type: ignore[method-assign]
    ww.stop = lambda *a, **k: None  # type: ignore[method-assign]
    ww.reset = lambda *a, **k: None  # type: ignore[method-assign]

    runner = VoiceRunner(
        config=VoiceConfig(screen_indicator_enabled=False, screen_indicator_height_px=5),
        data_dir=tmp_path,
        backend_factory=_NoopBackend,
        audio_manager=audio,
        wake_word_detector=ww,
        enable_hotkey=False,
    )
    await runner.start()
    assert runner._screen_indicator.enabled is False
    assert runner._screen_indicator.height_px == 5
    assert runner._screen_indicator._thread is None
    await runner.stop()

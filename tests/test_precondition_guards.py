"""Deterministic unit and E2E tests for PreconditionGuard and ActionVerifier."""

from __future__ import annotations

import asyncio
import ctypes
import json
import sys
import threading
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest
from websockets.asyncio.client import connect

from clea.core import auth
from clea.core.agent import _BASE_SYSTEM_PROMPT, AgentLoop
from clea.core.daemon import CleaDaemon, build_daemon
from clea.core.protocol import (
    METHOD_RESULTS,
    ChatSendParams,
    ChatSendRequest,
    ErrorEvent,
    HealthPingRequest,
    OsClickUiElementResult,
    OsControlMediaVolumeResult,
    OsControlSystemResult,
    OsGuiActionParams,
    OsGuiActionRequest,
    OsGuiActionResult,
    OsOpenOrFocusAppResult,
    OsUiActResult,
    OsUiBatchResult,
    ResponseFrame,
    ToolCallFinishedEvent,
    parse_frame,
)
from clea.core.sessions import Session
from clea.memory.store import MemoryStore
from clea.providers.base import LLMProvider, Message, StreamChunk, ToolCall
from clea.surfaces.voice.memory_hooks import BASE_VOICE_SYSTEM_INSTRUCTION
from clea.tools import system_controls, windows_os
from clea.tools.guards import (
    ActionVerifier,
    MediaSessionState,
    PreconditionFailedError,
    PreconditionGuard,
    VerificationOutcome,
    Win32FocusPreconditionBackend,
    WindowContext,
    WinRTMediaVerificationBackend,
)
from clea.tools.registry import AuditLogger, ToolRegistry
from clea.tools.system_controls import (
    manage_window,
    register_system_control_tools,
    system_control,
)
from clea.tools.windows_os import (
    EnumeratedWindow,
    click_ui_element,
    control_media_volume,
    control_system,
    gui_action,
    open_or_focus_app,
    register_windows_os_tools,
    set_ui_snapshot_state,
    ui_act,
    ui_batch,
)

# ---------------------------------------------------------------------------
# Deterministic Test Doubles
# ---------------------------------------------------------------------------


@dataclass
class _FakeWindowState:
    hwnd: int
    title: str
    process_name: str = "app.exe"
    pid: int = 100
    valid: bool = True
    minimized: bool = False
    bounds: tuple[int, int, int, int] = (100, 100, 900, 700)
    work_area: tuple[int, int, int, int] = (0, 0, 1920, 1040)


class FakeFocusPreconditionBackend:
    """In-memory deterministic FocusPreconditionBackend for unit and E2E tests."""

    def __init__(
        self,
        windows: dict[int, _FakeWindowState] | None = None,
        foreground_hwnd: int | None = None,
    ) -> None:
        self.windows: dict[int, _FakeWindowState] = windows or {}
        self.foreground_hwnd = foreground_hwnd
        self.poll_hooks: list[Callable[[int], None]] = []
        self.poll_count: int = 0

    def _run_hooks(self) -> None:
        self.poll_count += 1
        if self.poll_hooks:
            hook = self.poll_hooks.pop(0)
            hook(self.poll_count)

    def get_foreground_context(self) -> WindowContext | None:
        if self.foreground_hwnd is None:
            return None
        return self.get_window_context(self.foreground_hwnd)

    def get_window_context(self, hwnd: int) -> WindowContext | None:
        win = self.windows.get(hwnd)
        if win is None or not win.valid:
            return None
        return WindowContext(
            hwnd=win.hwnd,
            title=win.title,
            process_name=win.process_name,
            pid=win.pid,
            bounds=None if win.minimized else win.bounds,
            work_area=win.work_area,
            minimized=win.minimized,
        )

    def is_window_valid(self, hwnd: int) -> bool:
        self._run_hooks()
        win = self.windows.get(hwnd)
        return bool(win is not None and win.valid)

    def is_minimized(self, hwnd: int) -> bool:
        win = self.windows.get(hwnd)
        return bool(win is not None and win.minimized)

    def get_extended_frame_bounds(self, hwnd: int) -> tuple[int, int, int, int] | None:
        win = self.windows.get(hwnd)
        if win is None or not win.valid:
            return None
        return win.bounds

    def get_monitor_work_area(self, hwnd: int) -> tuple[int, int, int, int] | None:
        win = self.windows.get(hwnd)
        if win is None or not win.valid:
            return None
        return win.work_area


class FakeMediaVerificationBackend:
    """In-memory deterministic MediaVerificationBackend returning queued snapshots."""

    def __init__(self, states: list[MediaSessionState] | MediaSessionState) -> None:
        if isinstance(states, list):
            self._states = list(states)
        else:
            self._states = [states]
        self.calls = 0

    def get_active_session(self, timeout_s: float = 0.5) -> MediaSessionState:
        self.calls += 1
        if len(self._states) > 1:
            return self._states.pop(0)
        return self._states[0]


class _FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, dt: float) -> None:
        self.sleeps.append(dt)
        self.now += max(0.05, dt)


class _FakeUser32ForControls:
    """Minimal user32 double mutating FakeFocusPreconditionBackend cleanly."""

    def __init__(
        self,
        backend: FakeFocusPreconditionBackend,
        *,
        allow_resize: bool = True,
        allow_focus: bool = True,
        allow_close: bool = True,
    ) -> None:
        self.backend = backend
        self.allow_resize = allow_resize
        self.allow_focus = allow_focus
        self.allow_close = allow_close
        self.key_events: list[int] = []
        self.sent_inputs: list[Any] = []
        self.moved_rects: list[tuple[int, int, int, int, int]] = []

    def GetForegroundWindow(self) -> int:
        return self.backend.foreground_hwnd or 0

    def IsWindow(self, hwnd: int) -> bool:
        return self.backend.is_window_valid(hwnd)

    def IsWindowVisible(self, hwnd: int) -> bool:
        win = self.backend.windows.get(hwnd)
        return bool(win and win.valid)

    def IsIconic(self, hwnd: int) -> bool:
        return self.backend.is_minimized(hwnd)

    def GetWindowTextLengthW(self, hwnd: int) -> int:
        win = self.backend.windows.get(hwnd)
        return len(win.title) if win else 0

    def GetWindowTextW(self, hwnd: int, buf: Any, max_count: int) -> int:
        win = self.backend.windows.get(hwnd)
        title = win.title if win else ""
        buf.value = title[: max_count - 1]
        return len(buf.value)

    def EnumWindows(self, callback: Any, lparam: int) -> bool:
        for hwnd in list(self.backend.windows):
            if not callback(hwnd, lparam):
                break
        return True

    def MonitorFromWindow(self, hwnd: int, flags: int) -> int:
        return 0x1_0000_9999

    def GetMonitorInfoW(self, hmonitor: int, mi_ptr: Any) -> bool:
        win = next(iter(self.backend.windows.values()), None)
        wl, wt, wr, wb = win.work_area if win else (0, 0, 1920, 1040)
        obj = getattr(mi_ptr, "_obj", None)
        if obj is not None:
            obj.rcWork.left = wl
            obj.rcWork.top = wt
            obj.rcWork.right = wr
            obj.rcWork.bottom = wb
        return True

    def ShowWindow(self, hwnd: int, cmd: int) -> bool:
        win = self.backend.windows.get(hwnd)
        if win is None:
            return False
        if cmd == 6:  # SW_MINIMIZE
            win.minimized = True
        elif cmd == 3:  # SW_MAXIMIZE
            win.minimized = False
            if self.allow_resize:
                win.bounds = win.work_area
        elif cmd == 9:  # SW_RESTORE
            win.minimized = False
        return True

    def MoveWindow(self, hwnd: int, x: int, y: int, w: int, h: int, repaint: bool) -> bool:
        self.moved_rects.append((hwnd, x, y, w, h))
        win = self.backend.windows.get(hwnd)
        if win is None:
            return False
        if self.allow_resize:
            win.bounds = (x, y, x + w, y + h)
        return True

    def SetForegroundWindow(self, hwnd: int) -> bool:
        if self.allow_focus and hwnd in self.backend.windows:
            self.backend.foreground_hwnd = hwnd
            return True
        return False

    def PostMessageW(self, hwnd: int, msg: int, wparam: int, lparam: int) -> bool:
        win = self.backend.windows.get(hwnd)
        if win is not None and self.allow_close:
            win.valid = False
        return True

    def keybd_event(self, vk: int, scan: int, flags: int, extra: int) -> None:
        self.key_events.append(vk)

    def SendInput(self, count: int, inputs: Any, size: int) -> int:
        self.sent_inputs.append(count)
        return count


# ---------------------------------------------------------------------------
# PreconditionGuard Tests
# ---------------------------------------------------------------------------


def test_precondition_guard_focus_stolen_by_windows_security_aborts_before_step_2() -> None:
    vscode = _FakeWindowState(
        hwnd=0x1_0000_1234,
        title="Visual Studio Code",
        process_name="Code.exe",
        pid=4242,
    )
    modal = _FakeWindowState(
        hwnd=0x1_0000_5678,
        title="Windows Security",
        process_name="CredentialUIBroker.exe",
        pid=8888,
    )
    backend = FakeFocusPreconditionBackend(
        windows={vscode.hwnd: vscode, modal.hwnd: modal},
        foreground_hwnd=vscode.hwnd,
    )
    executed: list[str] = []

    def step_1() -> None:
        executed.append("step_1")
        # Simulated UAC / Windows Security modal steals focus right after step 1
        backend.foreground_hwnd = modal.hwnd

    def step_2() -> None:
        executed.append("step_2")

    with pytest.raises(PreconditionFailedError) as exc_info:
        with PreconditionGuard(backend) as guard:
            assert guard.target is not None
            assert guard.target.hwnd == vscode.hwnd
            guard.assert_ready("step_1")
            step_1()
            guard.assert_ready("step_2")
            step_2()

    err = exc_info.value
    assert err.reason == "focus_lost"
    assert err.expected_hwnd == vscode.hwnd
    assert err.expected_title == "Visual Studio Code"
    assert err.expected_process == "Code.exe"
    assert err.actual_hwnd == modal.hwnd
    assert err.actual_title == "Windows Security"
    assert err.actual_process == "CredentialUIBroker.exe"
    assert err.step_label == "step_2"
    assert executed == ["step_1"]

    abort = err.to_abort_payload()
    assert abort["ok"] is False
    assert abort["status"] == "aborted_precondition"
    assert abort["reason"] == "focus_lost"
    assert abort["aborted_precondition"]["actual_title"] == "Windows Security"


def test_precondition_guard_window_closed_and_minimized_and_no_target() -> None:
    # 1. No target window available
    empty_backend = FakeFocusPreconditionBackend(windows={}, foreground_hwnd=None)
    with pytest.raises(PreconditionFailedError) as exc_none:
        with PreconditionGuard(empty_backend):
            pass
    assert exc_none.value.reason == "no_target_window"

    # 2. Window closed between steps
    win = _FakeWindowState(hwnd=501, title="Notepad", process_name="notepad.exe")
    backend = FakeFocusPreconditionBackend(windows={501: win}, foreground_hwnd=501)
    with pytest.raises(PreconditionFailedError) as exc_closed:
        with PreconditionGuard(backend) as guard:
            guard.assert_ready("step_1")
            win.valid = False
            guard.assert_ready("step_2")
    assert exc_closed.value.reason == "window_closed"
    assert exc_closed.value.step_label == "step_2"

    # 3. Window minimized between steps
    win2 = _FakeWindowState(hwnd=502, title="Terminal", process_name="WindowsTerminal.exe")
    backend2 = FakeFocusPreconditionBackend(windows={502: win2}, foreground_hwnd=502)
    with pytest.raises(PreconditionFailedError) as exc_min:
        with PreconditionGuard(backend2) as guard:
            guard.assert_ready("step_1")
            win2.minimized = True
            guard.assert_ready("step_2")
    assert exc_min.value.reason == "window_minimized"
    assert exc_min.value.expected_title == "Terminal"

    # 4. allow_minimized=True and require_foreground=False permit background minimized target
    other = _FakeWindowState(hwnd=503, title="Browser", process_name="msedge.exe")
    backend3 = FakeFocusPreconditionBackend(
        windows={502: win2, 503: other},
        foreground_hwnd=503,
    )
    with PreconditionGuard(
        backend3,
        expected_hwnd=502,
        require_foreground=False,
        allow_minimized=True,
    ) as guard:
        ctx = guard.assert_ready("bg_step")
        assert ctx.hwnd == 502
        assert ctx.minimized is True


# ---------------------------------------------------------------------------
# 64-Bit Win32 Signatures & Handle Handling Tests
# ---------------------------------------------------------------------------


@pytest.mark.skipif(sys.platform != "win32", reason="Windows ctypes DLL check")
def test_win32_focus_backend_uses_64bit_void_pointer_signatures() -> None:
    backend = Win32FocusPreconditionBackend()
    u32 = backend._user32
    dwm = backend._dwmapi
    k32 = backend._kernel32
    assert u32 is not None
    assert k32 is not None

    assert u32.GetForegroundWindow.restype is ctypes.c_void_p
    assert u32.IsWindow.argtypes == [ctypes.c_void_p]
    assert u32.IsIconic.argtypes == [ctypes.c_void_p]
    assert u32.GetWindowThreadProcessId.argtypes[0] is ctypes.c_void_p
    assert u32.GetWindowTextLengthW.argtypes == [ctypes.c_void_p]
    assert u32.GetWindowTextW.argtypes[0] is ctypes.c_void_p
    assert u32.GetWindowRect.argtypes[0] is ctypes.c_void_p
    assert u32.MonitorFromWindow.argtypes[0] is ctypes.c_void_p
    assert u32.MonitorFromWindow.restype is ctypes.c_void_p
    assert u32.GetMonitorInfoW.argtypes[0] is ctypes.c_void_p

    if dwm is not None and hasattr(dwm, "DwmGetWindowAttribute"):
        assert dwm.DwmGetWindowAttribute.argtypes[0] is ctypes.c_void_p

    assert k32.OpenProcess.restype is ctypes.c_void_p
    assert k32.QueryFullProcessImageNameW.argtypes[0] is ctypes.c_void_p
    assert k32.CloseHandle.argtypes == [ctypes.c_void_p]


def test_win32_focus_backend_supports_64bit_hwnd_and_hmonitor_and_closes_handle() -> None:
    high_hwnd = 0x1_8000_ABCD
    high_hmon = 0x2_9000_BEEF
    high_proc = 0x3_7000_CAFE
    closed_handles: list[int] = []

    class _FakeUser32High64:
        def GetForegroundWindow(self) -> int:
            return high_hwnd

        def IsWindow(self, hwnd: int) -> bool:
            assert hwnd == high_hwnd
            return True

        def IsIconic(self, hwnd: int) -> bool:
            return False

        def GetWindowThreadProcessId(self, hwnd: int, pid_ptr: Any) -> int:
            obj = getattr(pid_ptr, "_obj", None)
            if obj is not None:
                obj.value = 9191
            return 1

        def GetWindowTextLengthW(self, hwnd: int) -> int:
            return 11

        def GetWindowTextW(self, hwnd: int, buf: Any, max_count: int) -> int:
            buf.value = "64bit App"
            return 9

        def GetWindowRect(self, hwnd: int, rect_ptr: Any) -> bool:
            obj = getattr(rect_ptr, "_obj", None)
            if obj is not None:
                obj.left, obj.top, obj.right, obj.bottom = -1920, 0, -960, 1040
            return True

        def MonitorFromWindow(self, hwnd: int, flags: int) -> int:
            assert hwnd == high_hwnd
            return high_hmon

        def GetMonitorInfoW(self, hmon: int, mi_ptr: Any) -> bool:
            assert hmon == high_hmon
            obj = getattr(mi_ptr, "_obj", None)
            if obj is not None:
                obj.rcWork.left = -1920
                obj.rcWork.top = 0
                obj.rcWork.right = 0
                obj.rcWork.bottom = 1040
            return True

    class _FakeDwmHigh64:
        def DwmGetWindowAttribute(self, hwnd: int, attr: int, rect_ptr: Any, size: int) -> int:
            assert hwnd == high_hwnd
            assert attr == 9
            obj = getattr(rect_ptr, "_obj", None)
            if obj is not None:
                obj.left, obj.top, obj.right, obj.bottom = -1920, 0, -960, 1040
            return 0

    class _FakeKernelHigh64:
        def OpenProcess(self, access: int, inherit: bool, pid: int) -> int:
            assert pid == 9191
            return high_proc

        def QueryFullProcessImageNameW(
            self, h_proc: int, flags: int, buf: Any, size_ptr: Any
        ) -> bool:
            assert h_proc == high_proc
            buf.value = r"C:\Program Files\Demo\HighApp.exe"
            return True

        def CloseHandle(self, handle: int) -> bool:
            closed_handles.append(handle)
            return True

    backend = Win32FocusPreconditionBackend(
        user32=_FakeUser32High64(),
        dwmapi=_FakeDwmHigh64(),
        kernel32=_FakeKernelHigh64(),
    )
    ctx = backend.get_foreground_context()
    assert ctx is not None
    assert ctx.hwnd == high_hwnd
    assert ctx.title == "64bit App"
    assert ctx.process_name == "HighApp.exe"
    assert ctx.pid == 9191
    assert ctx.bounds == (-1920, 0, -960, 1040)
    assert ctx.work_area == (-1920, 0, 0, 1040)
    assert closed_handles == [high_proc]


# ---------------------------------------------------------------------------
# ActionVerifier Media Verification Tests
# ---------------------------------------------------------------------------


def test_verify_media_state_polls_across_transient_states_and_handles_timeouts() -> None:
    clock = _FakeClock()
    backend = FakeMediaVerificationBackend(
        [
            MediaSessionState("paused", title="Old Song", artist="Artist", source_app="Spotify"),
            MediaSessionState(
                "changing", title="Bohemian Rhapsody", artist="Queen", source_app="Spotify"
            ),
            MediaSessionState(
                "playing", title="Bohemian Rhapsody", artist="Queen", source_app="Spotify"
            ),
        ]
    )
    verifier = ActionVerifier(
        media_backend=backend,
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
    )
    outcome = verifier.verify_media_state(
        expected_status="playing",
        expected_title_contains="bohemian",
        timeout_s=1.5,
        poll_interval_s=0.15,
    )
    assert outcome.verified is True
    assert outcome.status == "verified"
    assert outcome.actual_status == "playing"
    assert outcome.track_title == "Bohemian Rhapsody"
    assert outcome.artist == "Queen"
    assert outcome.source_app == "Spotify"
    assert backend.calls == 3
    assert outcome.elapsed_ms > 0.0

    # Timeout when session stays paused
    stuck_clock = _FakeClock()
    stuck_backend = FakeMediaVerificationBackend(
        MediaSessionState("paused", title="Static Track", artist="Artist", source_app="Edge")
    )
    stuck_verifier = ActionVerifier(
        media_backend=stuck_backend,
        monotonic_fn=stuck_clock.monotonic,
        sleep_fn=stuck_clock.sleep,
    )
    timeout_outcome = stuck_verifier.verify_media_state(
        expected_status="playing",
        timeout_s=0.45,
        poll_interval_s=0.15,
    )
    assert timeout_outcome.verified is False
    assert timeout_outcome.status == "timeout"
    assert timeout_outcome.actual_status == "paused"
    assert timeout_outcome.reason == "expected_playing_got_paused"


def test_winrt_media_backend_unavailable_and_mocked_async_session() -> None:
    # 1. Missing winrt.windows.media.control cleanly returns status="unavailable"
    def _raise_missing(name: str) -> Any:
        raise ModuleNotFoundError(f"No module named {name!r}")

    unavailable_backend = WinRTMediaVerificationBackend(module_loader=_raise_missing)
    state = unavailable_backend.get_active_session(timeout_s=0.2)
    assert state.status == "unavailable"
    assert state.playback_status == "unavailable"
    assert state.available is False

    verifier = ActionVerifier(media_backend=unavailable_backend)
    outcome = verifier.verify_media_state(expected_status="playing")
    assert outcome.verified is False
    assert outcome.status == "unavailable"
    assert outcome.actual_status == "unavailable"

    # 2. Mocked WinRT module works inside AND outside a running event loop
    class _FakeStatusEnum:
        name = "PLAYING"

    class _FakeSession:
        source_app_user_model_id = "SpotifyAB.SpotifyMusic"

        def get_playback_info(self) -> Any:
            return SimpleNamespace(playback_status=_FakeStatusEnum())

        async def try_get_media_properties_async(self) -> Any:
            return SimpleNamespace(title="Midnight City", artist="M83")

    class _FakeManager:
        def get_current_session(self) -> Any:
            return _FakeSession()

    class _FakeManagerCls:
        @staticmethod
        async def request_async() -> Any:
            return _FakeManager()

    fake_mod = SimpleNamespace(GlobalSystemMediaTransportControlsSessionManager=_FakeManagerCls)
    winrt_backend = WinRTMediaVerificationBackend(module_loader=lambda _n: fake_mod)

    sync_session = winrt_backend.get_active_session(timeout_s=0.5)
    assert sync_session.playback_status == "playing"
    assert sync_session.title == "Midnight City"
    assert sync_session.artist == "M83"
    assert sync_session.source_app == "SpotifyAB.SpotifyMusic"

    async def _inside_loop() -> MediaSessionState:
        return winrt_backend.get_active_session(timeout_s=0.5)

    loop_session = asyncio.run(_inside_loop())
    assert loop_session.playback_status == "playing"
    assert loop_session.title == "Midnight City"


# ---------------------------------------------------------------------------
# ActionVerifier Window Geometry & State Verification Tests
# ---------------------------------------------------------------------------


def test_verify_window_state_snap_maximized_and_refused_resize() -> None:
    clock = _FakeClock()
    # Secondary negative-coordinate monitor rcWork = (-1920, 0, 0, 1040)
    win = _FakeWindowState(
        hwnd=901,
        title="Editor",
        bounds=(-1920, 0, -960, 1040),
        work_area=(-1920, 0, 0, 1040),
    )
    backend = FakeFocusPreconditionBackend(windows={901: win}, foreground_hwnd=901)
    verifier = ActionVerifier(
        focus_backend=backend,
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
    )

    # Left half with 7px DWM invisible-border variance (within 16px tolerance)
    win.bounds = (-1913, 0, -967, 1033)
    left_outcome = verifier.verify_window_state(901, expected_snap="left", timeout_s=0.3)
    assert left_outcome.verified is True
    assert left_outcome.reason == "window_state_matched"

    # Right half exact
    win.bounds = (-960, 0, 0, 1040)
    right_outcome = verifier.verify_window_state(901, expected_snap="right", timeout_s=0.3)
    assert right_outcome.verified is True

    # Maximized with 8px variance
    win.bounds = (-1912, 8, -8, 1032)
    max_outcome = verifier.verify_window_state(901, expected_state="maximized", timeout_s=0.3)
    assert max_outcome.verified is True

    # Explicit bounds (x, y, w, h)
    win.bounds = (100, 200, 900, 800)
    explicit_outcome = verifier.verify_window_state(
        901,
        expected_bounds=(100, 200, 800, 600),
        timeout_s=0.3,
    )
    assert explicit_outcome.verified is True

    # Refuses to resize (stays 400px wide instead of 960px left half)
    win.bounds = (-1920, 0, -1520, 1040)
    fail_outcome = verifier.verify_window_state(901, expected_snap="left", timeout_s=0.2)
    assert fail_outcome.verified is False
    assert fail_outcome.status == "timeout"
    assert fail_outcome.reason == "bounds_mismatch"


# ---------------------------------------------------------------------------
# Chat ToolRegistry + AgentLoop End-to-End Tests
# ---------------------------------------------------------------------------


def test_chat_tool_registry_and_agent_loop_window_and_media_verification(tmp_path: Path) -> None:
    win = _FakeWindowState(
        hwnd=0x1_0000_7777,
        title="Visual Studio Code",
        process_name="Code.exe",
        bounds=(100, 100, 900, 700),
        work_area=(0, 0, 1920, 1040),
    )
    focus_backend = FakeFocusPreconditionBackend(windows={win.hwnd: win}, foreground_hwnd=win.hwnd)
    media_backend = FakeMediaVerificationBackend(
        [
            MediaSessionState("paused", title="Track A", artist="Artist A", source_app="Spotify"),
            MediaSessionState("playing", title="Track A", artist="Artist A", source_app="Spotify"),
        ]
    )
    u32 = _FakeUser32ForControls(focus_backend, allow_resize=True)

    registry = ToolRegistry()
    verifier = ActionVerifier(focus_backend=focus_backend, media_backend=media_backend)
    register_system_control_tools(
        registry,
        user32=u32,
        focus_backend=focus_backend,
        media_backend=media_backend,
        verifier=verifier,
    )

    class _FakeChatProvider(LLMProvider):
        def __init__(self) -> None:
            self.step = 0

        async def stream_turn(
            self,
            *,
            messages: list[Message],
            tools: list[Any],
            system_prompt: str,
            response_format: dict[str, Any] | None = None,
        ) -> Any:
            assert "aborted_precondition" in system_prompt
            self.step += 1
            if self.step == 1:
                yield StreamChunk(
                    type="tool_call",
                    tool_call=ToolCall(
                        id="call_snap",
                        name="manage_window",
                        arguments={"action": "snap_left", "title_query": "Visual Studio Code"},
                    ),
                )
                yield StreamChunk(
                    type="tool_call",
                    tool_call=ToolCall(
                        id="call_play",
                        name="system_control",
                        arguments={"control": "media", "action": "play"},
                    ),
                )
                yield StreamChunk(type="done")
                return
            yield StreamChunk(
                type="text_delta",
                text="Snapped VS Code left and verified Spotify is playing.",
            )
            yield StreamChunk(type="done")

    mem = MemoryStore(base_dir=tmp_path / "memory")
    agent = AgentLoop(provider=_FakeChatProvider(), registry=registry, memory=mem)
    session = Session(id="s1")
    assistant_text = asyncio.run(
        agent.run_turn(session=session, user_text="Snap VS Code left and play")
    )
    assert "verified" in assistant_text.lower()

    tool_outputs: dict[str, dict[str, Any]] = {}
    for msg in session.messages:
        if msg.role == "tool" and msg.tool_name:
            tool_outputs[msg.tool_name] = json.loads(msg.content)

    assert tool_outputs["manage_window"]["verification"]["verified"] is True
    assert tool_outputs["manage_window"]["rect"] == {
        "x": 0,
        "y": 0,
        "width": 960,
        "height": 1040,
    }
    assert tool_outputs["system_control"]["verification"]["verified"] is True
    assert tool_outputs["system_control"]["verification"]["actual"]["playback_status"] == "playing"


def test_direct_system_controls_refused_resize_returns_unverified() -> None:
    win = _FakeWindowState(
        hwnd=404,
        title="Locked Dialog",
        bounds=(200, 200, 600, 500),
        work_area=(0, 0, 1920, 1040),
    )
    focus_backend = FakeFocusPreconditionBackend(windows={404: win}, foreground_hwnd=404)
    u32 = _FakeUser32ForControls(focus_backend, allow_resize=False)
    clock = _FakeClock()
    verifier = ActionVerifier(
        focus_backend=focus_backend,
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
    )
    res = manage_window("maximize", "Locked Dialog", user32=u32, verifier=verifier)
    assert res["ok"] is True
    assert res["verification"]["verified"] is False
    assert res["verification"]["reason"] == "bounds_mismatch"

    # Media control advances baseline -> new track and verifies
    media_backend = FakeMediaVerificationBackend(
        [
            MediaSessionState("playing", title="Previous Song", source_app="Spotify"),
            MediaSessionState("playing", title="Next Song", source_app="Spotify"),
        ]
    )
    media_verifier = ActionVerifier(
        media_backend=media_backend,
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
    )
    m_res = system_control("media", "next", user32=u32, verifier=media_verifier)
    assert m_res["verification"]["verified"] is True


# ---------------------------------------------------------------------------
# Voice Daemon RPC (os.*) End-to-End Tests
# ---------------------------------------------------------------------------


class _FakeConn:
    def __init__(self) -> None:
        self.sent: list[str] = []
        self.ws = self
        self.current_session_id: str | None = None
        self.is_voice: bool = False

    async def send(self, payload: str) -> None:
        self.sent.append(payload)


def _rpc_call(daemon: CleaDaemon, method: str, params: dict[str, Any]) -> dict[str, Any]:
    conn = _FakeConn()
    req = parse_frame(
        json.dumps(
            {
                "frame_type": "request",
                "id": f"req_{method}",
                "method": method,
                "params": params,
            }
        )
    )
    assert getattr(req, "frame_type", None) == "request"
    asyncio.run(daemon._dispatch_request(conn, req))
    assert len(conn.sent) == 1
    frame = json.loads(conn.sent[0])
    assert frame["ok"] is True, frame
    return frame["result"]


def test_daemon_os_rpc_precondition_abort_and_closed_loop_verification(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    vscode = _FakeWindowState(
        hwnd=0x1_0000_2000,
        title="Visual Studio Code",
        process_name="Code.exe",
        bounds=(50, 50, 1200, 900),
        work_area=(0, 0, 1920, 1040),
    )
    modal = _FakeWindowState(
        hwnd=0x1_0000_3000,
        title="Windows Security",
        process_name="CredentialUIBroker.exe",
    )
    focus_backend = FakeFocusPreconditionBackend(
        windows={vscode.hwnd: vscode, modal.hwnd: modal},
        foreground_hwnd=vscode.hwnd,
    )
    media_backend = FakeMediaVerificationBackend(
        [
            MediaSessionState("paused", title="Old Song", source_app="Spotify"),
            MediaSessionState("playing", title="Old Song", source_app="Spotify"),
        ]
    )
    clock = _FakeClock()
    verifier = ActionVerifier(
        focus_backend=focus_backend,
        media_backend=media_backend,
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
    )

    daemon = build_daemon(
        memory=MemoryStore(base_dir=tmp_path / "memory"),
        provider=SimpleNamespace(name="fake"),  # type: ignore[arg-type]
        focus_backend=focus_backend,
        media_backend=media_backend,
        action_verifier=verifier,
    )

    u32 = _FakeUser32ForControls(focus_backend, allow_resize=True)

    # Inject deterministic OS user32 doubles inside windows_os module for daemon handlers
    import clea.tools.windows_os as wos

    monkeypatch.setattr(wos, "_get_win32_user32", lambda user32=None: u32)
    monkeypatch.setattr(
        wos,
        "_enumerate_windows_ctypes",
        lambda *_args, **_kwargs: [
            EnumeratedWindow(
                hwnd=vscode.hwnd,
                title=vscode.title,
                process_name=vscode.process_name,
                is_visible=True,
            )
        ],
    )

    # 1. os.open_or_focus_app focusing VS Code returns verified=True
    focus_backend.foreground_hwnd = modal.hwnd  # start unfocused so SetForegroundWindow runs
    open_res = _rpc_call(daemon, "os.open_or_focus_app", {"app_name": "vscode"})
    OsOpenOrFocusAppResult.model_validate(open_res)
    assert open_res["status"] == "focused"
    assert open_res["verification"]["verified"] is True

    # 2. os.control_media_volume(play_pause) verifies paused -> playing
    media_res = _rpc_call(daemon, "os.control_media_volume", {"action": "play_pause"})
    OsControlMediaVolumeResult.model_validate(media_res)
    assert media_res["verification"]["verified"] is True
    assert media_res["verification"]["reason"] == "media_state_changed"

    # 3. os.control_system(maximize_window) verifies maximized bounds vs rcWork
    sys_res = _rpc_call(daemon, "os.control_system", {"action": "maximize_window"})
    OsControlSystemResult.model_validate(sys_res)
    assert sys_res["verification"]["verified"] is True

    # 4. os.ui_batch aborts cleanly on focus_lost before step 2
    set_ui_snapshot_state(
        {
            "ok": True,
            "snapshot_id": "snap_guard_1",
            "window_title": "Visual Studio Code",
            "hwnd": vscode.hwnd,
            "count": 1,
            "elements": [
                {
                    "ref": "e1",
                    "role": "Button",
                    "name": "Run",
                    "x": 100,
                    "y": 100,
                    "bounds": {"x": 90, "y": 90, "width": 20, "height": 20},
                    "center": {"cx": 100, "cy": 100},
                }
            ],
        }
    )

    batch_calls: list[str] = []

    def _patched_gui_action(action: str, **kwargs: Any) -> dict[str, Any]:
        batch_calls.append(action)
        if action == "click_at":
            # Simulate modal popup stealing focus immediately after step 1 completes
            focus_backend.foreground_hwnd = modal.hwnd
        return {"ok": True, "status": "completed", "action": action}

    monkeypatch.setattr(wos, "gui_action", _patched_gui_action)
    focus_backend.foreground_hwnd = vscode.hwnd
    batch_res = _rpc_call(
        daemon,
        "os.ui_batch",
        {
            "steps": [
                {"op": "click_at", "x": 100, "y": 100},
                {"op": "press_keys", "keys": ["ctrl", "s"]},
            ]
        },
    )
    OsUiBatchResult.model_validate(batch_res)
    assert batch_res["ok"] is False
    assert batch_res["status"] == "aborted_precondition"
    assert batch_res["reason"] == "focus_lost"
    assert batch_res["steps_executed"] == 1
    assert batch_res["aborted_precondition"]["actual_title"] == "Windows Security"
    assert batch_res["aborted_precondition"]["step_label"] == "step_2"
    assert batch_calls == ["click_at"]  # press_keys (step 2) NEVER ran

    set_ui_snapshot_state({})


def test_ui_act_click_ui_element_and_gui_action_abort_on_focus_loss() -> None:
    vscode = _FakeWindowState(hwnd=701, title="Visual Studio Code", process_name="Code.exe")
    modal = _FakeWindowState(hwnd=702, title="Windows Security", process_name="Sec.exe")
    backend = FakeFocusPreconditionBackend(
        windows={701: vscode, 702: modal},
        foreground_hwnd=701,
    )
    u32 = _FakeUser32ForControls(backend)

    # Direct ui_act multi-step set_value aborts when modal steals focus between click and typing
    set_ui_snapshot_state(
        {
            "ok": True,
            "snapshot_id": "snap_act_guard",
            "window_title": "Visual Studio Code",
            "hwnd": 701,
            "count": 1,
            "elements": [
                {
                    "ref": "e1",
                    "role": "Edit",
                    "name": "Search",
                    "x": 50,
                    "y": 50,
                    "bounds": {"x": 40, "y": 40, "width": 100, "height": 20},
                    "center": {"cx": 50, "cy": 50},
                }
            ],
        }
    )
    act_steps: list[str] = []

    def _steal_after_click(action: str, **kwargs: Any) -> dict[str, Any]:
        act_steps.append(action)
        if action == "click_at":
            backend.foreground_hwnd = modal.hwnd
        return {"ok": True}

    act_res = ui_act(
        "e1",
        "set_value",
        "secret",
        gui_action_fn=_steal_after_click,
        focus_backend=backend,
        _capture_snapshot=False,
    )
    OsUiActResult.model_validate(act_res)
    assert act_res["ok"] is False
    assert act_res["status"] == "aborted_precondition"
    assert act_res["reason"] == "focus_lost"
    assert act_steps == ["click_at"]  # press_keys & type_text NEVER invoked

    # Direct click_ui_element aborts between UIA scan and mouse click fallback
    backend.foreground_hwnd = 701
    ps_calls = 0

    def _ps_runner(*args: Any, **kwargs: Any) -> Any:
        nonlocal ps_calls
        ps_calls += 1
        backend.foreground_hwnd = modal.hwnd
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"found": True, "x": 20, "y": 30, "invoked": False}),
            stderr="",
        )

    click_res = click_ui_element(
        "OK",
        powershell_runner=_ps_runner,
        user32=u32,
        focus_backend=backend,
    )
    OsClickUiElementResult.model_validate(click_res)
    assert ps_calls == 1
    assert click_res["ok"] is False
    assert click_res["status"] == "aborted_precondition"
    assert u32.sent_inputs == []  # mouse click NEVER dispatched into Windows Security

    # Direct gui_action aborts immediately when expected window is already minimized
    backend.foreground_hwnd = 701
    vscode.minimized = True
    gui_res = gui_action(
        "type_text",
        text="hello",
        user32=u32,
        focus_backend=backend,
        expected_hwnd=701,
    )
    OsGuiActionResult.model_validate(gui_res)
    assert gui_res["ok"] is False
    assert gui_res["status"] == "aborted_precondition"
    assert gui_res["reason"] == "window_minimized"

    # Direct ui_batch aborts cleanly on focus loss before step 2 even if stop_on_error=False
    batch_executed: list[str] = []

    def _batch_gui(action: str, **kwargs: Any) -> dict[str, Any]:
        batch_executed.append(action)
        if action == "click_at":
            backend.foreground_hwnd = modal.hwnd
        return {"ok": True, "status": "completed", "action": action}

    vscode.minimized = False
    backend.foreground_hwnd = 701
    direct_batch = ui_batch(
        [
            {"op": "click_at", "x": 10, "y": 10},
            {"op": "press_keys", "keys": ["ctrl", "a"]},
        ],
        stop_on_error=False,
        gui_action_fn=_batch_gui,
        ui_snapshot_fn=lambda **_kw: {
            "ok": True,
            "snapshot_id": "snap_direct",
            "window_title": "Visual Studio Code",
            "hwnd": 701,
            "count": 0,
            "elements": [],
        },
        user32=u32,
        focus_backend=backend,
        expected_hwnd=701,
    )
    OsUiBatchResult.model_validate(direct_batch)
    assert direct_batch["ok"] is False
    assert direct_batch["status"] == "aborted_precondition"
    assert direct_batch["reason"] == "focus_lost"
    assert direct_batch["steps_executed"] == 1
    assert batch_executed == ["click_at"]

    set_ui_snapshot_state({})


def test_direct_tools_preserve_legacy_mock_paths_and_verify_when_media_or_focus_injected() -> None:
    clock = _FakeClock()
    win = _FakeWindowState(hwnd=801, title="Notepad", work_area=(0, 0, 1920, 1040))
    focus_backend = FakeFocusPreconditionBackend(windows={801: win}, foreground_hwnd=801)
    media_backend = FakeMediaVerificationBackend(
        [
            MediaSessionState("paused", title="Song 1"),
            MediaSessionState("playing", title="Song 1"),
        ]
    )
    u32 = _FakeUser32ForControls(focus_backend)
    verifier = ActionVerifier(
        focus_backend=focus_backend,
        media_backend=media_backend,
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
    )

    open_res = open_or_focus_app(
        "notepad",
        user32=u32,
        window_enumerator=lambda: [
            EnumeratedWindow(hwnd=801, title="Notepad", process_name="notepad.exe", is_visible=True)
        ],
        verifier=verifier,
    )
    assert open_res["status"] == "focused"
    assert open_res["verification"]["verified"] is True

    media_res = control_media_volume("play_pause", user32=u32, verifier=verifier)
    assert media_res["verification"]["verified"] is True

    sys_res = control_system("minimize_window", hwnd=801, user32=u32, verifier=verifier)
    assert sys_res["verification"]["verified"] is True
    assert set(METHOD_RESULTS.keys()) >= {"os.ui_batch", "os.control_system"}


def test_prompts_require_honest_reporting_of_aborted_or_unverified_actions() -> None:
    assert "aborted_precondition" in _BASE_SYSTEM_PROMPT
    assert "verification.verified=false" in _BASE_SYSTEM_PROMPT
    assert "aborted_precondition" in BASE_VOICE_SYSTEM_INSTRUCTION
    assert "verification.verified=false" in BASE_VOICE_SYSTEM_INSTRUCTION


def test_system_controls_and_windows_os_64bit_win32_signatures_and_high_hwnd() -> None:
    if sys.platform == "win32":
        u32 = system_controls._get_user32()
        k32 = system_controls._get_kernel32()
        assert u32 is not None
        assert k32 is not None
        assert u32 is not ctypes.windll.user32
        assert k32 is not ctypes.windll.kernel32
        assert u32.GetForegroundWindow.restype is ctypes.c_void_p
        assert u32.GetDesktopWindow.restype is ctypes.c_void_p
        assert u32.IsWindow.argtypes == [ctypes.c_void_p]
        assert u32.IsWindowVisible.argtypes == [ctypes.c_void_p]
        assert u32.GetWindowTextLengthW.argtypes == [ctypes.c_void_p]
        assert u32.MonitorFromWindow.argtypes[0] is ctypes.c_void_p
        assert u32.MonitorFromWindow.restype is ctypes.c_void_p
        assert u32.GetMonitorInfoW.argtypes == [ctypes.c_void_p, ctypes.c_void_p]
        assert u32.ShowWindow.argtypes[0] is ctypes.c_void_p
        assert u32.SetForegroundWindow.argtypes == [ctypes.c_void_p]
        assert u32.MoveWindow.argtypes[0] is ctypes.c_void_p
        assert u32.PostMessageW.argtypes[0] is ctypes.c_void_p
        assert u32.SendMessageW.argtypes[0] is ctypes.c_void_p
        assert u32.DefWindowProcW.argtypes[0] is ctypes.c_void_p
        assert k32.GetSystemPowerStatus.argtypes == [ctypes.c_void_p]
        assert k32.GlobalMemoryStatusEx.argtypes == [ctypes.c_void_p]

        wos_u32 = windows_os._get_win32_user32()
        assert wos_u32.IsWindow.argtypes[0] is ctypes.c_void_p

    high_hwnd = 0x1_0000_BEEF
    win = _FakeWindowState(
        hwnd=high_hwnd,
        title="Code Editor",
        process_name="Code.exe",
        bounds=(200, 100, 1200, 800),
        work_area=(0, 0, 1920, 1040),
    )
    backend = FakeFocusPreconditionBackend(windows={high_hwnd: win}, foreground_hwnd=high_hwnd)
    fake_u32 = _FakeUser32ForControls(backend)
    clock = _FakeClock()
    verifier = ActionVerifier(
        focus_backend=backend,
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
    )

    res = manage_window(
        "snap_left",
        title_query="Code",
        user32=fake_u32,
        focus_backend=backend,
        verifier=verifier,
    )
    assert res["ok"] is True
    assert res["hwnd"] == high_hwnd
    assert res["rect"] == {"x": 0, "y": 0, "width": 960, "height": 1040}
    assert res["verification"]["verified"] is True


def test_ui_batch_and_ui_act_forward_guard_to_real_gui_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target_hwnd = 0x1_0000_1234
    other_hwnd = 0x1_0000_5678
    modal_hwnd = 0x1_0000_9999

    target = _FakeWindowState(hwnd=target_hwnd, title="Editor", process_name="Code.exe")
    other = _FakeWindowState(hwnd=other_hwnd, title="Browser", process_name="msedge.exe")
    modal = _FakeWindowState(
        hwnd=modal_hwnd, title="Windows Security", process_name="CredentialUIBroker.exe"
    )
    backend = FakeFocusPreconditionBackend(
        windows={target_hwnd: target, other_hwnd: other, modal_hwnd: modal},
        foreground_hwnd=other_hwnd,
    )
    fake_u32 = _FakeUser32ForControls(backend)

    def _forbid_live_win32(*_a: Any, **_kw: Any) -> Any:
        raise AssertionError(
            "Must not construct Win32FocusPreconditionBackend when guard/backend injected"
        )

    monkeypatch.setattr(windows_os, "Win32FocusPreconditionBackend", _forbid_live_win32)

    snap_payload = {
        "ok": True,
        "snapshot_id": "snap_guard_fwd",
        "hwnd": target_hwnd,
        "window_title": "Editor",
        "count": 1,
        "elements": [
            {
                "ref": "e1",
                "name": "Input Box",
                "role": "textbox",
                "x": 120,
                "y": 130,
                "width": 120,
                "height": 28,
                "cx": 180,
                "cy": 144,
            }
        ],
    }
    set_ui_snapshot_state(snap_payload)

    # 1. require_foreground=False via ui_batch must flow all the way into real gui_action
    #    both through ui_act (op="act") and directly (op="press_keys"), without falling back
    #    to Win32FocusPreconditionBackend(require_foreground=True).
    res_bg = ui_batch(
        steps=[
            {"op": "act", "ref": "e1", "action": "click", "label": "click_input"},
            {"op": "press_keys", "keys": ["ctrl", "s"], "label": "save_file"},
        ],
        expected_hwnd=target_hwnd,
        require_foreground=False,
        focus_backend=backend,
        gui_action_fn=None,
        user32=fake_u32,
        ui_snapshot_fn=lambda **_kw: snap_payload,
        window_title_fn=lambda: "Editor",
    )
    assert res_bg["ok"] is True
    assert res_bg["status"] == "completed"
    assert res_bg["steps_executed"] == 2
    assert len(fake_u32.sent_inputs) == 2

    # 2. If focus is stolen during step 1's real gui_action SendInput inside ui_batch,
    #    real gui_action on step 2 must fail the locked PreconditionGuard and abort cleanly.
    backend.foreground_hwnd = target_hwnd
    fake_u32.sent_inputs.clear()

    class _FocusStealingUser32(_FakeUser32ForControls):
        def SendInput(self, count: int, inputs: Any, size: int) -> int:
            res = super().SendInput(count, inputs, size)
            # Modal steals focus right after the first input (click) completes
            self.backend.foreground_hwnd = modal_hwnd
            return res

    stealing_u32 = _FocusStealingUser32(backend)
    res_abort = ui_batch(
        steps=[
            {"op": "act", "ref": "e1", "action": "click", "label": "step1_click"},
            {"op": "press_keys", "keys": ["ctrl", "a"], "label": "step2_select_all"},
        ],
        expected_hwnd=target_hwnd,
        require_foreground=True,
        focus_backend=backend,
        gui_action_fn=None,
        user32=stealing_u32,
        ui_snapshot_fn=lambda **_kw: snap_payload,
        window_title_fn=lambda: "Editor",
    )
    assert res_abort["ok"] is False
    assert res_abort["status"] == "aborted_precondition"
    assert res_abort["aborted_precondition"]["reason"] == "focus_lost"
    assert res_abort["aborted_precondition"]["actual_title"] == "Windows Security"
    assert len(stealing_u32.sent_inputs) == 1


def test_gui_action_type_text_aborts_mid_typing_when_focus_stolen() -> None:
    target_hwnd = 0x1_0000_2000
    modal_hwnd = 0x1_0000_9999
    target = _FakeWindowState(hwnd=target_hwnd, title="Notepad", process_name="notepad.exe")
    modal = _FakeWindowState(hwnd=modal_hwnd, title="UAC Prompt", process_name="consent.exe")
    backend = FakeFocusPreconditionBackend(
        windows={target_hwnd: target, modal_hwnd: modal},
        foreground_hwnd=target_hwnd,
    )
    fake_u32 = _FakeUser32ForControls(backend)

    sleeps = 0

    def _steal_after_first_char(_sec: float) -> None:
        nonlocal sleeps
        sleeps += 1
        backend.foreground_hwnd = modal_hwnd

    res = gui_action(
        "type_text",
        text="secret",
        interval_ms=15,
        user32=fake_u32,
        sleep_fn=_steal_after_first_char,
        focus_backend=backend,
        expected_hwnd=target_hwnd,
    )
    assert res["ok"] is False
    assert res["status"] == "aborted_precondition"
    assert res["aborted_precondition"]["reason"] == "focus_lost"
    assert res["aborted_precondition"]["actual_title"] == "UAC Prompt"
    assert res["aborted_precondition"]["step_label"] == "gui_action_type_text_char_1"
    # Only the first char ('s') was dispatched before focus theft stopped remaining chars
    assert len(fake_u32.sent_inputs) == 1
    assert sleeps == 1


def test_verify_window_state_rejects_invalid_expectations() -> None:
    win = _FakeWindowState(hwnd=901, title="Editor")
    backend = FakeFocusPreconditionBackend(windows={901: win}, foreground_hwnd=901)
    clock = _FakeClock()
    verifier = ActionVerifier(
        focus_backend=backend,
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
    )

    with pytest.raises(ValueError, match="expected_state"):
        verifier.verify_window_state(901, expected_state="unknown")

    with pytest.raises(ValueError, match="expected_snap"):
        verifier.verify_window_state(901, expected_snap="top")

    with pytest.raises(ValueError, match="expected_bounds"):
        verifier.verify_window_state(901, expected_bounds=(0, 0, 0, 100))


def test_control_system_snap_left_right_with_guard_and_daemon_rpc(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notepad = _FakeWindowState(
        hwnd=0x1_0000_7100,
        title="Untitled - Notepad",
        process_name="notepad.exe",
        bounds=(200, 100, 1000, 700),
        work_area=(0, 0, 1920, 1040),
    )
    calc = _FakeWindowState(
        hwnd=0x1_0000_7200,
        title="Calculator",
        process_name="ApplicationFrameHost.exe",
        bounds=(100, 100, 500, 600),
        work_area=(0, 0, 1920, 1040),
    )
    focus_backend = FakeFocusPreconditionBackend(
        windows={notepad.hwnd: notepad, calc.hwnd: calc},
        foreground_hwnd=notepad.hwnd,
    )
    u32 = _FakeUser32ForControls(focus_backend, allow_resize=True)
    clock = _FakeClock()
    verifier = ActionVerifier(
        focus_backend=focus_backend,
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
    )

    # 1. Direct snap_left targeting app_name="notepad"
    res_left = control_system(
        "snap_left",
        app_name="notepad",
        user32=u32,
        window_enumerator=lambda: [
            EnumeratedWindow(
                hwnd=notepad.hwnd,
                title=notepad.title,
                process_name=notepad.process_name,
                is_visible=True,
            ),
            EnumeratedWindow(
                hwnd=calc.hwnd,
                title=calc.title,
                process_name=calc.process_name,
                is_visible=True,
            ),
        ],
        focus_backend=focus_backend,
        verifier=verifier,
    )
    assert res_left["ok"] is True
    assert res_left["action"] == "snap_left"
    assert res_left["rect"] == {"x": 0, "y": 0, "width": 960, "height": 1040}
    assert res_left["verification"]["verified"] is True

    # 2. window + window_action="snap_right" targeting window_title="Calculator"
    res_right = control_system(
        "window",
        window_action="snap_right",
        window_title="Calculator",
        user32=u32,
        window_enumerator=lambda: [
            EnumeratedWindow(
                hwnd=notepad.hwnd,
                title=notepad.title,
                process_name=notepad.process_name,
                is_visible=True,
            ),
            EnumeratedWindow(
                hwnd=calc.hwnd,
                title=calc.title,
                process_name=calc.process_name,
                is_visible=True,
            ),
        ],
        focus_backend=focus_backend,
        verifier=verifier,
    )
    assert res_right["ok"] is True
    assert res_right["action"] == "snap_right"
    assert res_right["rect"] == {"x": 960, "y": 0, "width": 960, "height": 1040}
    assert res_right["verification"]["verified"] is True

    # 3. Foreground fallback when no app_name/title passed
    focus_backend.foreground_hwnd = notepad.hwnd
    res_fg = control_system(
        "snap_right",
        user32=u32,
        focus_backend=focus_backend,
        verifier=verifier,
    )
    assert res_fg["ok"] is True
    assert res_fg["hwnd"] == notepad.hwnd
    assert res_fg["verification"]["verified"] is True

    # 4. PreconditionGuard aborts cleanly if target is already invalid/closed
    notepad.valid = False
    res_closed = control_system(
        "snap_left",
        hwnd=notepad.hwnd,
        user32=u32,
        focus_backend=focus_backend,
        verifier=verifier,
    )
    assert res_closed["ok"] is False
    assert res_closed["status"] == "aborted_precondition"
    assert res_closed["aborted_precondition"]["reason"] == "no_target_window"

    # 5. Daemon RPC round-trip for os.control_system snap_left
    notepad.valid = True

    def _patched_control_system(
        action: str,
        *,
        focus_backend: Any = None,
        verifier: Any = None,
        **kw: Any,
    ) -> dict[str, Any]:
        return control_system(
            action,
            user32=u32,
            window_enumerator=lambda: [
                EnumeratedWindow(
                    hwnd=notepad.hwnd,
                    title=notepad.title,
                    process_name=notepad.process_name,
                    is_visible=True,
                )
            ],
            focus_backend=focus_backend,
            verifier=verifier,
            **kw,
        )

    monkeypatch.setattr("clea.core.daemon.control_system", _patched_control_system)
    mem = MemoryStore(base_dir=tmp_path / "mem")
    mem.ensure_initialized()
    daemon = build_daemon(
        host="127.0.0.1",
        port=0,
        memory=mem,
        provider=SimpleNamespace(provider_name="fake", model_name="fake"),
        focus_backend=focus_backend,
        action_verifier=verifier,
    )
    rpc_res = _rpc_call(
        daemon,
        "os.control_system",
        {"action": "snap_left", "app_name": "notepad"},
    )
    validated = OsControlSystemResult.model_validate(rpc_res)
    assert validated.action == "snap_left"
    assert validated.rect == {"x": 0, "y": 0, "width": 960, "height": 1040}
    assert validated.verification is not None and validated.verification.verified is True


async def test_chat_gui_action_typing_minimized_abort_and_nonblocking_ws(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    notepad = _FakeWindowState(
        hwnd=0x1_0000_8100,
        title="Untitled - Notepad",
        process_name="notepad.exe",
        bounds=(100, 100, 900, 700),
        work_area=(0, 0, 1920, 1040),
    )
    modal = _FakeWindowState(
        hwnd=0x1_0000_8200,
        title="Windows Security",
        process_name="CredentialUIBroker.exe",
    )
    focus_backend = FakeFocusPreconditionBackend(
        windows={notepad.hwnd: notepad, modal.hwnd: modal},
        foreground_hwnd=notepad.hwnd,
    )
    fake_u32 = _FakeUser32ForControls(focus_backend)
    worker_threads: list[int] = []
    main_thread = threading.get_ident()
    steal_on_sleep = False

    def _sleep_maybe_steal(_sec: float) -> None:
        if steal_on_sleep:
            focus_backend.foreground_hwnd = modal.hwnd

    def _mock_gui_action(action: str, **kwargs: Any) -> dict[str, Any]:
        worker_threads.append(threading.get_ident())
        # Verify ToolRegistry.execute offloaded sync call off the event loop thread
        return gui_action(
            action,
            user32=fake_u32,
            sleep_fn=_sleep_maybe_steal,
            focus_backend=focus_backend,
            **{k: v for k, v in kwargs.items() if k not in ("user32", "focus_backend")},
        )

    monkeypatch.setattr(windows_os, "gui_action", _mock_gui_action)

    class _ChatGuiProvider(LLMProvider):
        provider_name = "fake"
        model_name = "fake"

        def __init__(self) -> None:
            self.mode = "type"

        async def stream_turn(
            self,
            *,
            messages: list[Message],
            tools: list[Any],
            system_prompt: str,
            response_format: dict[str, Any] | None = None,
        ) -> Any:
            if tools:
                tool_names = {t.name for t in tools}
                assert {
                    "gui_action",
                    "ui_act",
                    "ui_batch",
                    "click_ui_element",
                    "manage_window",
                } <= tool_names
            tool_msgs = [m for m in messages if m.role == "tool"]
            if self.mode == "timeout":
                raise TimeoutError()
            if tool_msgs:
                yield StreamChunk(type="text_delta", text=f"Result: {tool_msgs[-1].content}")
                yield StreamChunk(type="done")
                return
            yield StreamChunk(
                type="tool_call",
                tool_call=ToolCall(
                    id="call_gui_1",
                    name="gui_action",
                    arguments={"action": "type_text", "text": "hello", "interval_ms": 10},
                ),
            )
            yield StreamChunk(type="done")

    mem = MemoryStore(base_dir=tmp_path / "mem")
    mem.ensure_initialized()
    audit_path = tmp_path / "logs" / "audit.jsonl"
    reg = ToolRegistry(audit_logger=AuditLogger(audit_path))
    provider = _ChatGuiProvider()
    daemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=provider,
        memory=mem,
        registry=reg,
        focus_backend=focus_backend,
    )
    await daemon.start()
    try:
        url = f"ws://127.0.0.1:{daemon.bound_port}"
        async with connect(
            url,
            subprotocols=auth.client_subprotocols(daemon.token),  # type: ignore[arg-type]
        ) as ws:
            # 1. Normal Chat typing executes off main event loop thread and writes audit.jsonl
            req1 = ChatSendRequest(params=ChatSendParams(text="Type hello in Notepad"))
            await ws.send(req1.model_dump_json())

            ping_req = HealthPingRequest()
            await ws.send(ping_req.model_dump_json())

            events1: list[Any] = []
            got_ping = False
            final1: ResponseFrame | None = None
            while final1 is None or not got_ping:
                frame = parse_frame(await asyncio.wait_for(ws.recv(), timeout=5.0))
                if isinstance(frame, ResponseFrame) and frame.id == ping_req.id:
                    assert frame.ok is True
                    got_ping = True
                elif isinstance(frame, ResponseFrame) and frame.id == req1.id:
                    final1 = frame
                else:
                    events1.append(frame)

            assert final1.ok is True
            assert worker_threads and worker_threads[0] != main_thread
            finished1 = next(e for e in events1 if isinstance(e, ToolCallFinishedEvent))
            assert finished1.data.is_error is False
            parsed1 = json.loads(finished1.data.output)
            assert parsed1["ok"] is True
            assert parsed1["status"] == "completed"
            assert parsed1["char_count"] == 5

            # 2. Minimized-window Chat input aborts cleanly without crashing daemon/WS
            notepad.minimized = True
            req2 = ChatSendRequest(params=ChatSendParams(text="Type into minimized Notepad"))
            await ws.send(req2.model_dump_json())

            events2: list[Any] = []
            final2: ResponseFrame | None = None
            while final2 is None:
                frame = parse_frame(await asyncio.wait_for(ws.recv(), timeout=5.0))
                if isinstance(frame, ResponseFrame) and frame.id == req2.id:
                    final2 = frame
                else:
                    events2.append(frame)

            assert final2.ok is True
            finished2 = next(e for e in events2 if isinstance(e, ToolCallFinishedEvent))
            assert finished2.data.is_error is False
            parsed2 = json.loads(finished2.data.output)
            assert parsed2["ok"] is False
            assert parsed2["status"] == "aborted_precondition"
            assert parsed2["aborted_precondition"]["reason"] == "window_minimized"

            # 3. Focus-loss mid-typing in Chat aborts cleanly
            notepad.minimized = False
            focus_backend.foreground_hwnd = notepad.hwnd
            steal_on_sleep = True
            req3 = ChatSendRequest(params=ChatSendParams(text="Type while modal steals focus"))
            await ws.send(req3.model_dump_json())

            events3: list[Any] = []
            final3: ResponseFrame | None = None
            while final3 is None:
                frame = parse_frame(await asyncio.wait_for(ws.recv(), timeout=5.0))
                if isinstance(frame, ResponseFrame) and frame.id == req3.id:
                    final3 = frame
                else:
                    events3.append(frame)

            assert final3.ok is True
            finished3 = next(e for e in events3 if isinstance(e, ToolCallFinishedEvent))
            parsed3 = json.loads(finished3.data.output)
            assert parsed3["status"] == "aborted_precondition"
            assert parsed3["aborted_precondition"]["actual_title"] == "Windows Security"

            # 4. Empty TimeoutError() never returns blank error string ("Daemon request failed")
            provider.mode = "timeout"
            req4 = ChatSendRequest(params=ChatSendParams(text="Trigger empty timeout"))
            await ws.send(req4.model_dump_json())

            events4: list[Any] = []
            final4: ResponseFrame | None = None
            while final4 is None:
                frame = parse_frame(await asyncio.wait_for(ws.recv(), timeout=5.0))
                if isinstance(frame, ResponseFrame) and frame.id == req4.id:
                    final4 = frame
                else:
                    events4.append(frame)

            assert final4.ok is False
            assert final4.error and "TimeoutError" in final4.error
            err_evt = next(e for e in events4 if isinstance(e, ErrorEvent))
            assert err_evt.data.message

            audit_lines = [
                json.loads(line)
                for line in audit_path.read_text(encoding="utf-8").splitlines()
                if line.strip()
            ]
            assert len([a for a in audit_lines if a["tool"] == "gui_action"]) == 3
    finally:
        await daemon.stop()


def test_verification_outcome_classification_and_winrt_import_and_prompts() -> None:
    ok_out = VerificationOutcome(verified=True, reason="media_state_matched", status="verified")
    unavail_winrt = VerificationOutcome(
        verified=False, reason="winrt_unavailable", status="unavailable"
    )
    unavail_session = VerificationOutcome(
        verified=False, reason="no_media_session", status="unavailable"
    )
    fail_bounds = VerificationOutcome(
        verified=False, reason="bounds_mismatch", status="timeout"
    )
    assert ok_out.outcome_kind == "verified"
    assert unavail_winrt.outcome_kind == "unavailable"
    assert unavail_session.outcome_kind == "unavailable"
    assert fail_bounds.outcome_kind == "failed"

    for prompt in (_BASE_SYSTEM_PROMPT, BASE_VOICE_SYSTEM_INSTRUCTION):
        assert "winrt_unavailable" in prompt
        assert "no_media_session" in prompt
        assert "bounds_mismatch" in prompt
        assert 'do NOT say "I can\'t do that"' in prompt

    if sys.platform == "win32":
        import winrt.windows.media.control as mc

        assert hasattr(mc, "GlobalSystemMediaTransportControlsSessionManager")


def test_open_or_focus_app_notepad_child_and_root_hwnd_verification() -> None:
    root_hwnd = 0x1_0000_A000
    child_fg_hwnd = 0x1_0000_A010
    other_hwnd = 0x1_0000_B000
    closed_handles: list[int] = []

    class _Win11NotepadUser32:
        def __init__(self) -> None:
            self.fg = other_hwnd

        def IsWindow(self, hwnd: int) -> bool:
            return hwnd in (root_hwnd, child_fg_hwnd, other_hwnd)

        def IsWindowVisible(self, hwnd: int) -> bool:
            return self.IsWindow(hwnd)

        def IsIconic(self, hwnd: int) -> bool:
            return False

        def GetAncestor(self, hwnd: int, flags: int) -> int:
            if hwnd in (root_hwnd, child_fg_hwnd) and flags in (2, 3):
                return root_hwnd
            return hwnd

        def IsChild(self, parent: int, child: int) -> bool:
            return parent == root_hwnd and child == child_fg_hwnd

        def GetForegroundWindow(self) -> int:
            return self.fg

        def GetWindowTextLengthW(self, hwnd: int) -> int:
            return len(self._title(hwnd))

        def GetWindowTextW(self, hwnd: int, buf: Any, max_count: int) -> int:
            title = self._title(hwnd)
            buf.value = title[: max_count - 1]
            return len(buf.value)

        def _title(self, hwnd: int) -> str:
            if hwnd in (root_hwnd, child_fg_hwnd):
                return "Untitled - Notepad"
            return "Other App"

        def GetWindowThreadProcessId(self, hwnd: int, pid_ptr: Any) -> int:
            if pid_ptr is not None:
                pid_val = 4141 if hwnd in (root_hwnd, child_fg_hwnd) else 9191
                ctypes.cast(pid_ptr, ctypes.POINTER(ctypes.c_uint32)).contents.value = pid_val
            return 101

        def GetWindowRect(self, hwnd: int, rect_ptr: Any) -> bool:
            obj = getattr(rect_ptr, "_obj", None)
            if obj is not None:
                obj.left, obj.top, obj.right, obj.bottom = 100, 100, 900, 700
            return True

        def MonitorFromWindow(self, hwnd: int, flags: int) -> int:
            return 1

        def GetMonitorInfoW(self, hmon: int, mi_ptr: Any) -> bool:
            obj = getattr(mi_ptr, "_obj", None)
            if obj is not None:
                obj.rcWork.left, obj.rcWork.top, obj.rcWork.right, obj.rcWork.bottom = (
                    0,
                    0,
                    1920,
                    1040,
                )
            return True

        def ShowWindow(self, hwnd: int, cmd: int) -> bool:
            return True

        def SetForegroundWindow(self, hwnd: int) -> bool:
            # Simulate Windows 11 WinUI3 Notepad returning inner child HWND as foreground
            if hwnd == root_hwnd:
                self.fg = child_fg_hwnd
                return True
            return False

    class _Kernel32:
        def OpenProcess(self, access: int, inherit: bool, pid: int) -> int:
            return 0x1_0000_0055

        def QueryFullProcessImageNameW(
            self, handle: int, flags: int, buf: Any, size_ptr: Any
        ) -> bool:
            buf.value = r"C:\Windows\System32\notepad.exe"
            return True

        def CloseHandle(self, handle: int) -> bool:
            closed_handles.append(int(handle))
            return True

    u32 = _Win11NotepadUser32()
    k32 = _Kernel32()
    backend = Win32FocusPreconditionBackend(user32=u32, dwmapi=SimpleNamespace(), kernel32=k32)
    assert backend.is_same_window_tree(root_hwnd, child_fg_hwnd) is True
    assert backend.is_same_window_tree(root_hwnd, other_hwnd) is False

    clock = _FakeClock()
    verifier = ActionVerifier(
        focus_backend=backend,
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
    )
    res = open_or_focus_app(
        "notepad",
        user32=u32,
        kernel32=k32,
        window_enumerator=lambda: [
            EnumeratedWindow(
                hwnd=child_fg_hwnd,
                title="Untitled - Notepad",
                process_name="notepad.exe",
                is_visible=True,
            )
        ],
        focus_backend=backend,
        verifier=verifier,
    )
    assert res["status"] == "focused"
    assert res["hwnd"] == root_hwnd
    assert res["verification"]["verified"] is True
    assert res["verification"]["reason"] == "window_state_matched"
    assert closed_handles


def test_verification_outcome_normalizes_contradictory_status() -> None:
    failed = VerificationOutcome(verified=False, reason="bounds_mismatch")
    assert failed.status == "failed"
    assert failed.outcome_kind == "failed"
    assert failed.to_dict()["outcome_kind"] == "failed"

    unavail = VerificationOutcome(verified=False, reason="winrt_unavailable")
    assert unavail.status == "unavailable"
    assert unavail.outcome_kind == "unavailable"
    assert unavail.to_dict()["outcome_kind"] == "unavailable"


def test_focus_window_hwnd_attaches_cur_tid_to_fg_tid() -> None:
    calls: list[tuple[str, Any]] = []
    fg_holder = {"hwnd": 111}

    class _LockUser32:
        def IsIconic(self, hwnd: int) -> int:
            return 0

        def SetForegroundWindow(self, hwnd: int) -> int:
            calls.append(("SetForegroundWindow", hwnd))
            # Fail until cur_tid is attached to fg_tid
            if ("AttachThreadInput", 777, 888, True) in calls:
                fg_holder["hwnd"] = hwnd
                return 1
            return 0

        def GetForegroundWindow(self) -> int:
            return fg_holder["hwnd"]

        def GetWindowThreadProcessId(self, hwnd: int, pid_ptr: Any) -> int:
            return 888 if hwnd == 111 else 999

        def AttachThreadInput(self, id_attach: int, id_attach_to: int, attach: bool) -> int:
            calls.append(("AttachThreadInput", id_attach, id_attach_to, bool(attach)))
            return 1

        def BringWindowToTop(self, hwnd: int) -> int:
            calls.append(("BringWindowToTop", hwnd))
            return 1

        def ShowWindow(self, hwnd: int, cmd: int) -> int:
            calls.append(("ShowWindow", hwnd, cmd))
            return 1

    class _CurKernel32:
        def GetCurrentThreadId(self) -> int:
            return 777

    windows_os._focus_window_hwnd(
        _LockUser32(),
        222,
        kernel32=_CurKernel32(),
        attach_fallback=True,
    )
    assert fg_holder["hwnd"] == 222
    assert ("AttachThreadInput", 777, 888, True) in calls
    assert ("AttachThreadInput", 777, 888, False) in calls


def test_send_unicode_text_dispatches_vk_return_and_tab_and_app_name_minimized_aborts() -> None:
    sent_vk_and_flags: list[tuple[int, int, int]] = []
    show_calls: list[tuple[int, int]] = []

    class _FakeInputUser32:
        def EnumWindows(self, cb: Any, lparam: int) -> int:
            cb(301, lparam)
            return 1

        def IsWindowVisible(self, hwnd: int) -> int:
            return 1

        def IsIconic(self, hwnd: int) -> int:
            return 1 if hwnd == 301 else 0

        def ShowWindow(self, hwnd: int, cmd: int) -> int:
            show_calls.append((hwnd, cmd))
            return 1

        def GetWindowTextLengthW(self, hwnd: int) -> int:
            return len("Untitled - Notepad")

        def GetWindowTextW(self, hwnd: int, buf: Any, max_len: int) -> int:
            buf.value = "Untitled - Notepad"
            return len(buf.value)

        def GetWindowThreadProcessId(self, hwnd: int, pid_ptr: Any) -> int:
            if pid_ptr is not None:
                ctypes.cast(pid_ptr, ctypes.POINTER(ctypes.c_uint32)).contents.value = 500
            return 1

        def GetForegroundWindow(self) -> int:
            return 999

        def SendInput(self, count: int, ptr: Any, size: int) -> int:
            arr = ctypes.cast(ptr, ctypes.POINTER(windows_os._INPUT * count)).contents
            for item in arr:
                ki = item.union.ki
                sent_vk_and_flags.append((int(ki.wVk), int(ki.wScan), int(ki.dwFlags)))
            return count

    class _FakeKernel32:
        def OpenProcess(self, access: int, inherit: bool, pid: int) -> int:
            return 77

        def QueryFullProcessImageNameW(
            self, handle: int, flags: int, buf: Any, size_ptr: Any
        ) -> bool:
            buf.value = r"C:\Windows\System32\notepad.exe"
            return True

        def CloseHandle(self, handle: int) -> bool:
            return True

    u32 = _FakeInputUser32()
    k32 = _FakeKernel32()
    notepad_win = _FakeWindowState(
        hwnd=301,
        title="Untitled - Notepad",
        process_name="notepad.exe",
        minimized=True,
    )
    chrome_win = _FakeWindowState(hwnd=999, title="Clea", process_name="chrome.exe")
    backend = FakeFocusPreconditionBackend(
        windows={301: notepad_win, 999: chrome_win},
        foreground_hwnd=999,
    )

    # Minimized target app specified via app_name must abort WITHOUT restoring or sending keys
    abort_res = gui_action(
        "type_text",
        text="Hello",
        app_name="notepad",
        user32=u32,
        kernel32=k32,
        focus_backend=backend,
    )
    assert abort_res["ok"] is False
    assert abort_res["status"] == "aborted_precondition"
    assert abort_res["reason"] == "window_minimized"
    assert show_calls == []
    assert sent_vk_and_flags == []

    # Missing target app returns clean aborted_precondition instead of crashing
    missing_res = gui_action(
        "type_text",
        text="Hello",
        app_name="NonExistentAppXYZ",
        user32=u32,
        kernel32=k32,
        focus_backend=backend,
    )
    assert missing_res["ok"] is False
    assert missing_res["status"] == "aborted_precondition"
    assert missing_res["reason"] == "no_target_window"

    # Non-minimized foreground Notepad dispatches VK_RETURN (0x0D) and VK_TAB (0x09)
    notepad_win.minimized = False
    backend.foreground_hwnd = 301
    ok_res = gui_action(
        "type_text",
        text="A\r\nB\tC",
        app_name="notepad",
        interval_ms=0,
        user32=u32,
        kernel32=k32,
        focus_backend=backend,
    )
    assert ok_res["ok"] is True
    assert ok_res["char_count"] == len("A\r\nB\tC")
    assert (0x0D, 0, 0) in sent_vk_and_flags
    assert (0x0D, 0, windows_os.KEYEVENTF_KEYUP) in sent_vk_and_flags
    assert (0x09, 0, 0) in sent_vk_and_flags
    assert (0x09, 0, windows_os.KEYEVENTF_KEYUP) in sent_vk_and_flags


async def test_chat_gui_action_handles_null_optional_args_and_daemon_survives_error(
    tmp_path: Path,
) -> None:
    notepad = _FakeWindowState(hwnd=501, title="Untitled - Notepad", process_name="notepad.exe")
    backend = FakeFocusPreconditionBackend(windows={501: notepad}, foreground_hwnd=501)
    u32 = SimpleNamespace(
        SendInput=lambda c, p, s: c,
    )

    reg = ToolRegistry(audit_logger=AuditLogger(tmp_path / "audit.jsonl"))
    register_windows_os_tools(reg, focus_backend=backend, user32=u32)

    # Chat models sometimes pass explicit None for defaulted optional args
    tool_res = await reg.execute(
        "gui_action",
        {
            "action": "type_text",
            "text": "hi",
            "clicks": None,
            "button": None,
            "delta": None,
            "seconds": None,
            "interval_ms": None,
            "expected_hwnd": 501,
        },
    )
    assert tool_res["ok"] is True
    assert tool_res["status"] == "completed"

    # WebSocket loop must survive unexpected exceptions in _dispatch_request
    mem = MemoryStore(base_dir=tmp_path / "mem")
    mem.ensure_initialized()
    daemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=SimpleNamespace(),  # type: ignore[arg-type]
        memory=mem,
        registry=reg,
        focus_backend=backend,
    )
    orig_dispatch = daemon._dispatch_request

    async def _flaky_dispatch(conn: Any, frame: Any) -> None:
        if getattr(frame, "method", "") == "os.gui_action" and getattr(
            getattr(frame, "params", None), "action", ""
        ) == "wait":
            raise RuntimeError("boom inside dispatch")
        await orig_dispatch(conn, frame)

    daemon._dispatch_request = _flaky_dispatch  # type: ignore[method-assign]
    await daemon.start()
    try:
        url = f"ws://127.0.0.1:{daemon.bound_port}"
        async with connect(
            url,
            subprotocols=auth.client_subprotocols(daemon.token),  # type: ignore[arg-type]
        ) as ws:
            bad_req = OsGuiActionRequest(params=OsGuiActionParams(action="wait", seconds=0.2))
            await ws.send(bad_req.model_dump_json())
            err_frame = parse_frame(await asyncio.wait_for(ws.recv(), timeout=5.0))
            assert isinstance(err_frame, ResponseFrame)
            assert err_frame.id == bad_req.id
            assert err_frame.ok is False
            assert err_frame.error is not None
            assert "boom inside dispatch" in str(err_frame.error)

            # Connection remains alive for subsequent requests including os.gui_action
            ping_req = HealthPingRequest()
            await ws.send(ping_req.model_dump_json())
            ping_frame = parse_frame(await asyncio.wait_for(ws.recv(), timeout=5.0))
            assert isinstance(ping_frame, ResponseFrame)
            assert ping_frame.id == ping_req.id
            assert ping_frame.ok is True

            notepad.minimized = True
            min_req = OsGuiActionRequest(
                params=OsGuiActionParams(
                    action="type_text",
                    text="hello",
                    expected_hwnd=501,
                )
            )
            await ws.send(min_req.model_dump_json())
            min_frame = parse_frame(await asyncio.wait_for(ws.recv(), timeout=5.0))
            assert isinstance(min_frame, ResponseFrame)
            assert min_frame.id == min_req.id
            assert min_frame.ok is True
            assert min_frame.result is not None
            assert min_frame.result["status"] == "aborted_precondition"
            assert min_frame.result["reason"] == "window_minimized"
    finally:
        await daemon.stop()


def test_control_system_snap_calculator_known_app_spec() -> None:
    calc = _FakeWindowState(hwnd=811, title="Calculator", process_name="CalculatorApp.exe")
    backend = FakeFocusPreconditionBackend(windows={811: calc}, foreground_hwnd=811)
    moved: list[tuple[int, int, int, int, int]] = []

    class _CalcUser32:
        def ShowWindow(self, hwnd: int, cmd: int) -> int:
            return 1

        def MoveWindow(self, hwnd: int, x: int, y: int, w: int, h: int, repaint: bool) -> int:
            moved.append((hwnd, x, y, w, h))
            calc.bounds = (x, y, x + w, y + h)
            return 1

        def SetForegroundWindow(self, hwnd: int) -> int:
            backend.foreground_hwnd = hwnd
            return 1

    clock = _FakeClock()
    verifier = ActionVerifier(
        focus_backend=backend,
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
    )
    res = control_system(
        "snap_left",
        app_name="calculator",
        user32=_CalcUser32(),
        window_enumerator=lambda: [
            EnumeratedWindow(
                hwnd=811,
                title="Calculator",
                process_name="CalculatorApp.exe",
                is_visible=True,
            )
        ],
        focus_backend=backend,
        verifier=verifier,
    )
    assert res["ok"] is True
    assert res["action"] == "snap_left"
    assert res["hwnd"] == 811
    assert moved == [(811, 0, 0, 960, 1040)]
    assert res["verification"]["verified"] is True


def test_send_unicode_text_emits_atomic_pairs_per_code_unit_and_guards_between_pairs() -> None:
    timeline: list[tuple[Any, ...]] = []
    notepad = _FakeWindowState(hwnd=0x1_0000_6100, title="Notepad", process_name="notepad.exe")
    modal = _FakeWindowState(
        hwnd=0x1_0000_6200, title="Windows Security", process_name="CredentialUIBroker.exe"
    )

    class _TimelineBackend(FakeFocusPreconditionBackend):
        def is_window_valid(self, hwnd: int) -> bool:
            timeline.append(("guard_valid", hwnd))
            return super().is_window_valid(hwnd)

    class _RecordingUser32:
        def SendInput(self, count: int, ptr: Any, size: int) -> int:
            assert size == ctypes.sizeof(windows_os._INPUT)
            arr = ctypes.cast(ptr, ctypes.POINTER(windows_os._INPUT * count)).contents
            items = [
                (
                    int(item.type),
                    int(item.union.ki.wVk),
                    int(item.union.ki.wScan),
                    int(item.union.ki.dwFlags),
                )
                for item in arr
            ]
            timeline.append(("send_input", count, items))
            return count

    def _record_sleep(dt: float) -> None:
        timeline.append(("sleep", dt))

    backend = _TimelineBackend(
        windows={notepad.hwnd: notepad, modal.hwnd: modal},
        foreground_hwnd=notepad.hwnd,
    )
    u32 = _RecordingUser32()

    # 1. Non-zero pacing with BMP Unicode, surrogate pair ('😀'), CRLF, and tab
    sample = "Aé😀\r\n\tZ"
    with PreconditionGuard(backend, expected_hwnd=notepad.hwnd) as guard:
        clamped = windows_os._send_unicode_text(
            u32,
            sample,
            interval_ms=12,
            sleep_fn=_record_sleep,
            guard=guard,
            step_label="type_sample",
        )
    assert clamped == 12

    send_calls = [evt for evt in timeline if evt[0] == "send_input"]
    # 'A' (1 pair), 'é' (1 pair), '😀' (2 UTF-16 code units = 2 pairs), '\n' (1), '\t' (1), 'Z' (1)
    assert len(send_calls) == 7
    assert all(count == 2 for _, count, _ in send_calls)

    assert send_calls[0][2] == [
        (windows_os.INPUT_KEYBOARD, 0, ord("A"), windows_os.KEYEVENTF_UNICODE),
        (
            windows_os.INPUT_KEYBOARD,
            0,
            ord("A"),
            windows_os.KEYEVENTF_UNICODE | windows_os.KEYEVENTF_KEYUP,
        ),
    ]
    assert send_calls[1][2] == [
        (windows_os.INPUT_KEYBOARD, 0, ord("é"), windows_os.KEYEVENTF_UNICODE),
        (
            windows_os.INPUT_KEYBOARD,
            0,
            ord("é"),
            windows_os.KEYEVENTF_UNICODE | windows_os.KEYEVENTF_KEYUP,
        ),
    ]
    # Surrogate pair code units for 😀 (U+1F600 -> 0xD83D, 0xDE00) each get atomic SendInput(2)
    assert send_calls[2][2] == [
        (windows_os.INPUT_KEYBOARD, 0, 0xD83D, windows_os.KEYEVENTF_UNICODE),
        (
            windows_os.INPUT_KEYBOARD,
            0,
            0xD83D,
            windows_os.KEYEVENTF_UNICODE | windows_os.KEYEVENTF_KEYUP,
        ),
    ]
    assert send_calls[3][2] == [
        (windows_os.INPUT_KEYBOARD, 0, 0xDE00, windows_os.KEYEVENTF_UNICODE),
        (
            windows_os.INPUT_KEYBOARD,
            0,
            0xDE00,
            windows_os.KEYEVENTF_UNICODE | windows_os.KEYEVENTF_KEYUP,
        ),
    ]
    assert send_calls[4][2] == [
        (windows_os.INPUT_KEYBOARD, windows_os.VK_RETURN_OS, 0, 0),
        (windows_os.INPUT_KEYBOARD, windows_os.VK_RETURN_OS, 0, windows_os.KEYEVENTF_KEYUP),
    ]
    assert send_calls[5][2] == [
        (windows_os.INPUT_KEYBOARD, windows_os.VK_TAB_OS, 0, 0),
        (windows_os.INPUT_KEYBOARD, windows_os.VK_TAB_OS, 0, windows_os.KEYEVENTF_KEYUP),
    ]
    assert send_calls[6][2] == [
        (windows_os.INPUT_KEYBOARD, 0, ord("Z"), windows_os.KEYEVENTF_UNICODE),
        (
            windows_os.INPUT_KEYBOARD,
            0,
            ord("Z"),
            windows_os.KEYEVENTF_UNICODE | windows_os.KEYEVENTF_KEYUP,
        ),
    ]

    # Verify ordering: guard -> send_input(s) -> sleep, never sleep/guard inside a pair
    sleep_calls = [evt for evt in timeline if evt[0] == "sleep"]
    assert len(sleep_calls) == 5  # 6 normalized chars -> 5 inter-char sleeps
    assert [evt[0] for evt in timeline] == [
        "guard_valid",
        "send_input",
        "sleep",
        "guard_valid",
        "send_input",
        "sleep",
        "guard_valid",
        "send_input",
        "send_input",
        "sleep",
        "guard_valid",
        "send_input",
        "sleep",
        "guard_valid",
        "send_input",
        "sleep",
        "guard_valid",
        "send_input",
    ]

    # 2. interval_ms=0 still dispatches atomic SendInput(2) per code unit, zero sleeps, and
    #    aborts cleanly between atomic character pairs if focus is stolen mid-stream
    timeline.clear()
    zero_calls = 0

    class _StealOnSecondCharUser32(_RecordingUser32):
        def SendInput(self, count: int, ptr: Any, size: int) -> int:
            nonlocal zero_calls
            res = super().SendInput(count, ptr, size)
            zero_calls += 1
            if zero_calls == 1:
                backend.foreground_hwnd = modal.hwnd
            return res

    with pytest.raises(PreconditionFailedError) as exc_zero:
        with PreconditionGuard(backend, expected_hwnd=notepad.hwnd) as guard_zero:
            windows_os._send_unicode_text(
                _StealOnSecondCharUser32(),
                "abc",
                interval_ms=0,
                sleep_fn=_record_sleep,
                guard=guard_zero,
                step_label="fast_type",
            )
    assert exc_zero.value.reason == "focus_lost"
    assert exc_zero.value.step_label == "fast_type_char_1"
    assert [evt for evt in timeline if evt[0] == "sleep"] == []
    assert len([evt for evt in timeline if evt[0] == "send_input"]) == 1


def test_background_capable_actions_vs_foreground_raw_sendinput() -> None:
    target = _FakeWindowState(
        hwnd=0x1_0000_7100,
        title="Calculator",
        process_name="CalculatorApp.exe",
        bounds=(100, 100, 500, 600),
        work_area=(0, 0, 1920, 1040),
    )
    fg_other = _FakeWindowState(
        hwnd=0x1_0000_7200,
        title="Google Chrome",
        process_name="chrome.exe",
        bounds=(0, 0, 1920, 1040),
        work_area=(0, 0, 1920, 1040),
    )
    backend = FakeFocusPreconditionBackend(
        windows={target.hwnd: target, fg_other.hwnd: fg_other},
        foreground_hwnd=fg_other.hwnd,
    )
    media_backend = FakeMediaVerificationBackend(
        [
            MediaSessionState("paused", title="Song A", source_app="Spotify"),
            MediaSessionState("playing", title="Song A", source_app="Spotify"),
            MediaSessionState("playing", title="Song A", source_app="Spotify"),
            MediaSessionState("paused", title="Song A", source_app="Spotify"),
        ]
    )
    clock = _FakeClock()
    verifier = ActionVerifier(
        focus_backend=backend,
        media_backend=media_backend,
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
    )

    class _TrackingUser32(_FakeUser32ForControls):
        def __init__(self) -> None:
            super().__init__(backend, allow_resize=True)
            self.foreground_calls: list[int] = []
            self.show_calls: list[tuple[int, int]] = []
            self.cursor_moves: list[tuple[int, int]] = []

        def SetForegroundWindow(self, hwnd: int) -> bool:
            self.foreground_calls.append(hwnd)
            return super().SetForegroundWindow(hwnd)

        def ShowWindow(self, hwnd: int, cmd: int) -> bool:
            self.show_calls.append((hwnd, cmd))
            return super().ShowWindow(hwnd, cmd)

        def SetCursorPos(self, x: int, y: int) -> bool:
            self.cursor_moves.append((x, y))
            return True

    u32 = _TrackingUser32()
    enum_windows = [
        EnumeratedWindow(
            hwnd=target.hwnd,
            title=target.title,
            process_name=target.process_name,
            is_visible=True,
        ),
        EnumeratedWindow(
            hwnd=fg_other.hwnd,
            title=fg_other.title,
            process_name=fg_other.process_name,
            is_visible=True,
        ),
    ]

    # 1. UIA pattern-based click_ui_element succeeds on non-foreground target
    #    without SetForegroundWindow
    res_click_uia = click_ui_element(
        "Equals",
        control_type="Button",
        app_name="Calculator",
        powershell_runner=lambda cmd, **_kw: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {
                    "found": True,
                    "invoked": True,
                    "name": "Equals",
                    "control_type": "Button",
                    "x": 300,
                    "y": 400,
                }
            ),
            stderr="",
        ),
        user32=u32,
        window_enumerator=lambda: enum_windows,
        focus_backend=backend,
    )
    assert res_click_uia["ok"] is True
    assert res_click_uia["method"] == "invoke"
    assert backend.foreground_hwnd == fg_other.hwnd
    assert u32.foreground_calls == []
    assert u32.show_calls == []
    assert u32.sent_inputs == []

    # 2. UIA pattern-based ui_act (click + set_value) succeeds on non-foreground target
    #    without SetFocus or focus_lost
    assert "SetFocus" not in windows_os._POWERSHELL_UI_ACT_SCRIPT
    set_ui_snapshot_state(
        {
            "ok": True,
            "snapshot_id": "snap_bg_uia",
            "window_title": "Calculator",
            "hwnd": target.hwnd,
            "count": 2,
            "elements": [
                {
                    "ref": "e1",
                    "role": "Button",
                    "name": "Equals",
                    "x": 300,
                    "y": 400,
                    "bounds": {"x": 280, "y": 380, "width": 40, "height": 40},
                    "center": {"cx": 300, "cy": 400},
                },
                {
                    "ref": "e2",
                    "role": "Edit",
                    "name": "Expression",
                    "x": 250,
                    "y": 150,
                    "bounds": {"x": 150, "y": 135, "width": 200, "height": 30},
                    "center": {"cx": 250, "cy": 150},
                },
            ],
        }
    )

    res_act_click = ui_act(
        "e1",
        "click",
        powershell_runner=lambda cmd, **_kw: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"found": True, "acted": True, "method": "invoke", "cx": 300, "cy": 400}
            ),
            stderr="",
        ),
        user32=u32,
        focus_backend=backend,
        _capture_snapshot=False,
    )
    assert res_act_click["ok"] is True
    assert res_act_click["method"] == "invoke"

    res_act_set = ui_act(
        "e2",
        "set_value",
        "42",
        powershell_runner=lambda cmd, **_kw: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"found": True, "acted": True, "method": "value_pattern", "cx": 250, "cy": 150}
            ),
            stderr="",
        ),
        user32=u32,
        focus_backend=backend,
        _capture_snapshot=False,
    )
    assert res_act_set["ok"] is True
    assert res_act_set["method"] == "value_pattern"
    assert backend.foreground_hwnd == fg_other.hwnd
    assert u32.foreground_calls == []
    assert u32.sent_inputs == []

    # 3. Background-capable manage_window (minimize/restore/maximize/snap) & media controls
    #    do NOT enforce require_foreground=True
    res_min = manage_window(
        "minimize",
        title_query="Calculator",
        user32=u32,
        focus_backend=backend,
        verifier=verifier,
    )
    assert res_min["ok"] is True
    assert res_min["verification"]["verified"] is True
    assert u32.foreground_calls == []

    res_restore = manage_window(
        "restore",
        title_query="Calculator",
        user32=u32,
        focus_backend=backend,
        verifier=verifier,
    )
    assert res_restore["ok"] is True
    assert res_restore["verification"]["verified"] is True

    backend.foreground_hwnd = fg_other.hwnd
    u32.foreground_calls.clear()

    res_max = manage_window(
        "maximize",
        title_query="Calculator",
        user32=u32,
        focus_backend=backend,
        verifier=verifier,
    )
    assert res_max["ok"] is True
    assert res_max["verification"]["verified"] is True

    backend.foreground_hwnd = fg_other.hwnd
    u32.foreground_calls.clear()

    res_media = system_control("media", "play_pause", user32=u32, verifier=verifier)
    assert res_media["ok"] is True
    assert res_media["verification"]["verified"] is True
    assert u32.foreground_calls == []

    res_snap = manage_window(
        "snap_left",
        title_query="Calculator",
        user32=u32,
        focus_backend=backend,
        verifier=verifier,
    )
    assert res_snap["ok"] is True
    assert res_snap["verification"]["verified"] is True

    # Reset foreground back to Chrome so target is non-foreground for fallback/raw input checks
    backend.foreground_hwnd = fg_other.hwnd
    u32.foreground_calls.clear()
    u32.sent_inputs.clear()

    # 4. Raw SendInput paths (click_ui_element/ui_act falling back to SendInput, and gui_action)
    #    MUST still enforce require_foreground=True and abort with focus_lost
    abort_click_fb = click_ui_element(
        "History",
        app_name="Calculator",
        powershell_runner=lambda cmd, **_kw: SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"found": True, "invoked": False, "name": "History", "x": 150, "y": 90}
            ),
            stderr="",
        ),
        user32=u32,
        window_enumerator=lambda: enum_windows,
        focus_backend=backend,
    )
    assert abort_click_fb["ok"] is False
    assert abort_click_fb["status"] == "aborted_precondition"
    assert abort_click_fb["reason"] == "focus_lost"
    assert u32.sent_inputs == []

    abort_act_click_fb = ui_act(
        "e1",
        "click",
        powershell_runner=lambda cmd, **_kw: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"found": True, "acted": False, "method": "", "cx": 300, "cy": 400}),
            stderr="",
        ),
        user32=u32,
        focus_backend=backend,
        _capture_snapshot=False,
    )
    assert abort_act_click_fb["ok"] is False
    assert abort_act_click_fb["status"] == "aborted_precondition"
    assert abort_act_click_fb["reason"] == "focus_lost"
    assert u32.sent_inputs == []

    abort_act_fb = ui_act(
        "e2",
        "set_value",
        "42",
        powershell_runner=lambda cmd, **_kw: SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"found": True, "acted": False, "method": "", "cx": 250, "cy": 150}),
            stderr="",
        ),
        user32=u32,
        focus_backend=backend,
        _capture_snapshot=False,
    )
    assert abort_act_fb["ok"] is False
    assert abort_act_fb["status"] == "aborted_precondition"
    assert abort_act_fb["reason"] == "focus_lost"
    assert u32.sent_inputs == []

    abort_act_type = ui_act(
        "e2",
        "type",
        "42",
        user32=u32,
        focus_backend=backend,
        _capture_snapshot=False,
    )
    assert abort_act_type["ok"] is False
    assert abort_act_type["status"] == "aborted_precondition"
    assert abort_act_type["reason"] == "focus_lost"
    assert u32.sent_inputs == []

    abort_gui = gui_action(
        "type_text",
        text="42",
        user32=u32,
        focus_backend=backend,
        expected_hwnd=target.hwnd,
    )
    assert abort_gui["ok"] is False
    assert abort_gui["status"] == "aborted_precondition"
    assert abort_gui["reason"] == "focus_lost"
    assert abort_gui["aborted_precondition"]["expected_hwnd"] == target.hwnd
    assert abort_gui["aborted_precondition"]["actual_hwnd"] == fg_other.hwnd
    assert u32.sent_inputs == []

    # 5. windows_os background-capable control_system & control_media_volume also work
    #    on non-foreground targets without require_foreground=True
    res_cs_min = control_system(
        "minimize_window",
        hwnd=target.hwnd,
        user32=u32,
        focus_backend=backend,
        verifier=verifier,
    )
    assert res_cs_min["ok"] is True
    assert res_cs_min["verification"]["verified"] is True
    assert u32.foreground_calls == []

    res_cs_max = control_system(
        "maximize_window",
        hwnd=target.hwnd,
        user32=u32,
        focus_backend=backend,
        verifier=verifier,
    )
    assert res_cs_max["ok"] is True
    assert res_cs_max["verification"]["verified"] is True
    assert u32.foreground_calls == []

    res_cs_restore = control_system(
        "restore_window",
        hwnd=target.hwnd,
        user32=u32,
        focus_backend=backend,
        verifier=verifier,
    )
    assert res_cs_restore["ok"] is True
    assert res_cs_restore["verification"]["verified"] is True
    assert u32.foreground_calls == []

    res_cs_media = control_media_volume("play_pause", user32=u32, verifier=verifier)
    assert res_cs_media["ok"] is True
    assert res_cs_media["verification"]["verified"] is True
    assert u32.foreground_calls == []

    res_cs_snap = control_system(
        "snap_left",
        hwnd=target.hwnd,
        user32=u32,
        focus_backend=backend,
        verifier=verifier,
    )
    assert res_cs_snap["ok"] is True
    assert res_cs_snap["verification"]["verified"] is True

    set_ui_snapshot_state({})


def test_uia_expected_hwnd_after_snap_mid_ps_close_and_partial_sendinput(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    target = _FakeWindowState(hwnd=0x1_0000_9100, title="Calculator", process_name="calc.exe")
    fg_other = _FakeWindowState(hwnd=0x1_0000_9200, title="Chrome", process_name="chrome.exe")
    backend = FakeFocusPreconditionBackend(
        windows={target.hwnd: target, fg_other.hwnd: fg_other},
        foreground_hwnd=fg_other.hwnd,
    )

    class _TrackingUser32(_FakeUser32ForControls):
        def __init__(self) -> None:
            super().__init__(backend)
            self.foreground_calls: list[int] = []

        def SetForegroundWindow(self, hwnd: int) -> bool:
            self.foreground_calls.append(hwnd)
            return super().SetForegroundWindow(hwnd)

    u32 = _TrackingUser32()

    # 1. click_ui_element with explicit expected_hwnd (and app_name=None) targets expected_hwnd,
    #    NOT GetForegroundWindow()
    captured_cmds: list[str] = []

    def _ps_expected_hwnd(cmd: list[str], **_kw: Any) -> Any:
        captured_cmds.append(cmd[4])
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps(
                {"found": True, "invoked": True, "name": "Equals", "x": 100, "y": 200}
            ),
            stderr="",
        )

    res_expected = click_ui_element(
        "Equals",
        expected_hwnd=target.hwnd,
        powershell_runner=_ps_expected_hwnd,
        user32=u32,
        focus_backend=backend,
    )
    assert res_expected["ok"] is True
    assert f"-TargetHwnd {target.hwnd}" in captured_cmds[0]
    assert u32.foreground_calls == []

    # 2. Target window closing mid-PowerShell preserves reason="window_closed" & expected_title
    def _ps_close_during_click(cmd: list[str], **_kw: Any) -> Any:
        target.valid = False
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"found": True, "invoked": False, "name": "Hist", "x": 10, "y": 10}),
            stderr="",
        )

    closed_click = click_ui_element(
        "Hist",
        expected_hwnd=target.hwnd,
        powershell_runner=_ps_close_during_click,
        user32=u32,
        focus_backend=backend,
    )
    assert closed_click["ok"] is False
    assert closed_click["status"] == "aborted_precondition"
    assert closed_click["reason"] == "window_closed"
    assert closed_click["aborted_precondition"]["expected_title"] == "Calculator"
    assert closed_click["aborted_precondition"]["step_label"] == "click_ui_mouse"
    assert u32.sent_inputs == []

    target.valid = True
    set_ui_snapshot_state(
        {
            "ok": True,
            "snapshot_id": "snap_close_act",
            "window_title": "Calculator",
            "hwnd": 0,
            "count": 1,
            "elements": [
                {"ref": "e1", "role": "Button", "name": "Equals", "x": 100, "y": 200},
            ],
        }
    )

    def _ps_close_during_act(cmd: list[str], **_kw: Any) -> Any:
        target.valid = False
        return SimpleNamespace(
            returncode=0,
            stdout=json.dumps({"found": True, "acted": False, "method": "", "cx": 100, "cy": 200}),
            stderr="",
        )

    closed_act = ui_act(
        "e1",
        "click",
        expected_hwnd=target.hwnd,
        powershell_runner=_ps_close_during_act,
        user32=u32,
        focus_backend=backend,
        _capture_snapshot=False,
    )
    assert closed_act["ok"] is False
    assert closed_act["status"] == "aborted_precondition"
    assert closed_act["reason"] == "window_closed"
    assert closed_act["aborted_precondition"]["expected_title"] == "Calculator"
    assert closed_act["aborted_precondition"]["step_label"] == "win32_click"
    assert u32.sent_inputs == []

    # 3. ui_act after_snap passes _target_hwnd=expected_hwnd even when snapshot_hwnd == 0
    target.valid = True
    snap_kwargs: list[dict[str, Any]] = []
    act_ps_cmds: list[str] = []

    def _fake_ui_snapshot(**kw: Any) -> dict[str, Any]:
        snap_kwargs.append(kw)
        return {
            "ok": True,
            "snapshot_id": "snap_after_bg",
            "window_title": "Calculator",
            "hwnd": target.hwnd,
            "count": 1,
            "elements": [
                {"ref": "e1", "role": "Button", "name": "Equals", "x": 100, "y": 200},
            ],
        }

    def _fake_uia_json(ps_cmd: str, **_kw: Any) -> dict[str, Any]:
        act_ps_cmds.append(ps_cmd)
        return {"found": True, "acted": True, "method": "invoke", "cx": 100, "cy": 200}

    monkeypatch.setattr(windows_os.shutil, "which", lambda _exe: "powershell.exe")
    monkeypatch.setattr(windows_os, "_run_powershell_uia_json", _fake_uia_json)
    monkeypatch.setattr(windows_os, "ui_snapshot", _fake_ui_snapshot)

    res_after = ui_act(
        "e1",
        "click",
        expected_hwnd=target.hwnd,
        focus_backend=backend,
        _capture_snapshot=True,
    )
    assert res_after["ok"] is True
    assert res_after["method"] == "invoke"
    assert f"-TargetHwnd {target.hwnd}" in act_ps_cmds[0]
    assert snap_kwargs == [{"user32": None, "_target_hwnd": target.hwnd}]

    # 4. Partial SendInput (1 of 2 events) raises instead of leaving keydown stuck
    class _PartialSendInputU32:
        def SendInput(self, count: int, ptr: Any, size: int) -> int:
            return 1 if count == 2 else count

    with pytest.raises(RuntimeError, match="SendInput failed"):
        windows_os._send_unicode_text(_PartialSendInputU32(), "a", interval_ms=0)

    set_ui_snapshot_state({})




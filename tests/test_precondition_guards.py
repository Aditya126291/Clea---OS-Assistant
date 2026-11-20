"""Deterministic unit and E2E tests for PreconditionGuard and ActionVerifier."""

from __future__ import annotations

import asyncio
import ctypes
import json
import sys
from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from clea.core.agent import _BASE_SYSTEM_PROMPT, AgentLoop
from clea.core.daemon import CleaDaemon, build_daemon
from clea.core.protocol import (
    METHOD_RESULTS,
    OsClickUiElementResult,
    OsControlMediaVolumeResult,
    OsControlSystemResult,
    OsGuiActionResult,
    OsOpenOrFocusAppResult,
    OsUiActResult,
    OsUiBatchResult,
    parse_frame,
)
from clea.core.sessions import Session
from clea.memory.store import MemoryStore
from clea.providers.base import LLMProvider, Message, StreamChunk, ToolCall
from clea.surfaces.voice.memory_hooks import BASE_VOICE_SYSTEM_INSTRUCTION
from clea.tools.guards import (
    ActionVerifier,
    MediaSessionState,
    PreconditionFailedError,
    PreconditionGuard,
    Win32FocusPreconditionBackend,
    WindowContext,
    WinRTMediaVerificationBackend,
)
from clea.tools.registry import ToolRegistry
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

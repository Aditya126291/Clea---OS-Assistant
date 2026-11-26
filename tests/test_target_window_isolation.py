"""Deterministic unit tests for target-window isolation, owned-popup awareness,
WindowFromPoint hit-testing, VoiceRunner HWND inheritance, focus-aware circuit breaker,
and out-of-band WebSocket cancellation (os.cancel_inflight).
"""

from __future__ import annotations

import asyncio
import ctypes
import subprocess
import threading
import time
from collections.abc import AsyncIterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import patch

import pytest
from PIL import Image
from websockets.asyncio.client import connect

from clea.core import auth
from clea.core.agent import AgentLoop
from clea.core.circuit_breaker import ActionCircuitBreaker, compute_state_diff
from clea.core.daemon import CleaDaemon
from clea.core.protocol import OsCancelInflightRequest, OsGuiActionRequest, parse_frame
from clea.memory.store import MemoryStore
from clea.providers.base import LLMProvider, Message, StreamChunk
from clea.surfaces.voice.backends.base import (
    ToolCallEvent,
    VoiceBackend,
    VoiceBackendEvent,
    VoiceSessionConfig,
    VoiceToolResponse,
)
from clea.surfaces.voice.config import VoiceConfig
from clea.surfaces.voice.runner import VoiceRunner
from clea.surfaces.voice.screen import ScreenCaptureService
from clea.tools import windows_os
from clea.tools.cdp_bridge import CDPBridge
from clea.tools.guards import (
    PreconditionFailedError,
    PreconditionGuard,
    Win32FocusPreconditionBackend,
    WindowContext,
)
from clea.tools.registry import ToolDefinition, ToolRegistry


def test_is_same_window_tree_accepts_owned_popups_and_rejects_unrelated() -> None:
    target_hwnd = 0x1000
    owned_popup_hwnd = 0x1001
    same_pid_popup_menu_hwnd = 0x1002
    same_pid_other_toplevel_hwnd = 0x1003
    unrelated_other_pid_hwnd = 0x2000

    class _FakeUser32:
        def GetAncestor(self, hwnd: int, flag: int) -> int:
            return hwnd

        def IsChild(self, parent: int, child: int) -> bool:
            return False

        def GetWindow(self, hwnd: int, cmd: int) -> int:
            if cmd == 4 and hwnd == owned_popup_hwnd:
                return target_hwnd
            return 0

        def GetWindowLongW(self, hwnd: int, index: int) -> int:
            if index == -16 and hwnd == same_pid_popup_menu_hwnd:
                return 0x80000000  # WS_POPUP
            return 0

        def GetClassNameW(self, hwnd: int, buf: Any, _max_len: int) -> int:
            if hwnd == same_pid_popup_menu_hwnd:
                buf.value = "#32768"
                return 6
            buf.value = "Chrome_WidgetWin_1"
            return 18

        def GetWindowThreadProcessId(self, hwnd: int, pid_ptr: Any) -> int:
            pid_map = {
                target_hwnd: 4242,
                owned_popup_hwnd: 4242,
                same_pid_popup_menu_hwnd: 4242,
                same_pid_other_toplevel_hwnd: 4242,
                unrelated_other_pid_hwnd: 9999,
            }
            ctypes.cast(pid_ptr, ctypes.POINTER(ctypes.c_uint32)).contents.value = pid_map.get(
                hwnd, 0
            )
            return 1

    backend = Win32FocusPreconditionBackend(
        user32=_FakeUser32(), dwmapi=SimpleNamespace(), kernel32=SimpleNamespace()
    )
    assert backend.is_same_window_tree(target_hwnd, owned_popup_hwnd) is True
    assert backend.is_same_window_tree(target_hwnd, same_pid_popup_menu_hwnd) is True
    assert backend.is_same_window_tree(target_hwnd, same_pid_other_toplevel_hwnd) is False
    assert backend.is_same_window_tree(target_hwnd, unrelated_other_pid_hwnd) is False


def test_precondition_guard_fast_assert_ready_skips_extra_context_lookups() -> None:
    calls: list[str] = []

    class _FastBackend:
        def get_foreground_hwnd(self) -> int:
            calls.append("get_foreground_hwnd")
            return 100

        def get_foreground_context(self) -> WindowContext:
            calls.append("get_foreground_context")
            return WindowContext(hwnd=200, title="Other", process_name="other.exe")

        def get_window_context(self, hwnd: int) -> WindowContext:
            calls.append("get_window_context")
            return WindowContext(
                hwnd=hwnd,
                title="Target App",
                process_name="target.exe",
                bounds=(0, 0, 800, 600),
            )

        def is_window_valid(self, hwnd: int) -> bool:
            return hwnd == 100

        def is_minimized(self, hwnd: int) -> bool:
            return False

        def is_same_window_tree(self, a: int, b: int) -> bool:
            return a == b

        def get_extended_frame_bounds(self, _hwnd: int) -> tuple[int, int, int, int]:
            return (0, 0, 800, 600)

        def get_monitor_work_area(self, _hwnd: int) -> tuple[int, int, int, int]:
            return (0, 0, 1920, 1040)

    backend = _FastBackend()
    with PreconditionGuard(backend, expected_hwnd=100) as guard:  # type: ignore[arg-type]
        calls.clear()
        ctx = guard.assert_ready("char_0", fast=True)
        assert ctx.hwnd == 100
        assert calls == ["get_foreground_hwnd"]


def test_assert_point_targets_window_blocks_outside_and_occluded_points() -> None:
    class _PointBackend:
        def __init__(self, hit_hwnd: int) -> None:
            self.hit_hwnd = hit_hwnd

        def get_foreground_hwnd(self) -> int:
            return 100

        def get_foreground_context(self) -> WindowContext:
            return WindowContext(
                hwnd=100,
                title="Spotify",
                process_name="Spotify.exe",
                bounds=(100, 100, 900, 700),
            )

        def get_window_context(self, hwnd: int) -> WindowContext:
            if hwnd == 100:
                return WindowContext(
                    hwnd=100,
                    title="Spotify",
                    process_name="Spotify.exe",
                    bounds=(100, 100, 900, 700),
                )
            return WindowContext(
                hwnd=hwnd,
                title="Antigravity",
                process_name="Antigravity.exe",
                bounds=(0, 0, 1920, 1080),
            )

        def is_window_valid(self, hwnd: int) -> bool:
            return hwnd in (100, 200)

        def is_minimized(self, _hwnd: int) -> bool:
            return False

        def is_same_window_tree(self, a: int, b: int) -> bool:
            return (a == 100 and b in (100, 101))

        def window_from_point(self, _px: int, _py: int) -> int:
            return self.hit_hwnd

        def get_extended_frame_bounds(self, _hwnd: int) -> tuple[int, int, int, int]:
            return (100, 100, 900, 700)

        def get_monitor_work_area(self, _hwnd: int) -> tuple[int, int, int, int]:
            return (0, 0, 1920, 1040)

    # 1. Outside target bounds
    with PreconditionGuard(_PointBackend(100), expected_hwnd=100) as g1:  # type: ignore[arg-type]
        with pytest.raises(PreconditionFailedError) as exc1:
            g1.assert_point_targets_window(50, 50, step_label="click_at")
        assert exc1.value.reason == "target_point_outside_window"

    # 2. Inside target bounds, occluded by another window
    with PreconditionGuard(_PointBackend(200), expected_hwnd=100) as g2:  # type: ignore[arg-type]
        with pytest.raises(PreconditionFailedError) as exc2:
            g2.assert_point_targets_window(300, 300, step_label="click_at")
        assert exc2.value.reason == "target_point_occluded"
        assert exc2.value.actual_hwnd == 200
        assert exc2.value.actual_process == "Antigravity.exe"

    # 3. Inside bounds and WindowFromPoint hits owned popup/child (101)
    with PreconditionGuard(_PointBackend(101), expected_hwnd=100) as g3:  # type: ignore[arg-type]
        ctx = g3.assert_point_targets_window(300, 300, step_label="click_at")
        assert ctx.hwnd == 100


def test_gui_action_aborts_occluded_click_while_cdp_and_uia_background_unaffected(
    tmp_path: Path,
) -> None:
    cursor_calls: list[tuple[int, int]] = []

    class _DummyU32:
        def SetCursorPos(self, x: int, y: int) -> int:
            cursor_calls.append((x, y))
            return 1

        def SendInput(self, *_a: Any) -> int:
            raise AssertionError("SendInput must not run when point is occluded")

    class _OccludedBackend:
        def get_foreground_hwnd(self) -> int:
            return 100

        def get_foreground_context(self) -> WindowContext:
            return WindowContext(
                hwnd=100, title="Target", process_name="target.exe", bounds=(0, 0, 500, 500)
            )

        def get_window_context(self, hwnd: int) -> WindowContext:
            if hwnd == 100:
                return WindowContext(
                    hwnd=100, title="Target", process_name="target.exe", bounds=(0, 0, 500, 500)
                )
            return WindowContext(hwnd=999, title="Overlay", process_name="overlay.exe")

        def is_window_valid(self, hwnd: int) -> bool:
            return hwnd in (100, 999)

        def is_minimized(self, _hwnd: int) -> bool:
            return False

        def is_same_window_tree(self, a: int, b: int) -> bool:
            return a == b

        def window_from_point(self, _px: int, _py: int) -> int:
            return 999

        def get_extended_frame_bounds(self, _hwnd: int) -> tuple[int, int, int, int]:
            return (0, 0, 500, 500)

        def get_monitor_work_area(self, _hwnd: int) -> tuple[int, int, int, int]:
            return (0, 0, 1920, 1040)

    res = windows_os.gui_action(
        "click_at",
        x=250,
        y=250,
        user32=_DummyU32(),
        focus_backend=_OccludedBackend(),  # type: ignore[arg-type]
        expected_hwnd=100,
    )
    assert res["ok"] is False
    assert res["status"] == "aborted_precondition"
    assert res["reason"] == "target_point_occluded"
    assert cursor_calls == []

    # Background UIA pattern call (InvokePattern) on unfocused/occluded target MUST still work!
    windows_os.set_ui_snapshot_state(
        {
            "ok": True,
            "snapshot_id": "snap-bg-uia",
            "window_title": "Target",
            "hwnd": 100,
            "elements": [
                {
                    "ref": "e1",
                    "role": "button",
                    "name": "Save",
                    "automation_id": "save_btn",
                    "control_type": "Button",
                    "bounds": {"x": 20, "y": 20, "width": 80, "height": 30},
                    "center": {"cx": 60, "cy": 35},
                }
            ],
        }
    )

    class _UnfocusedBackend(_OccludedBackend):
        def get_foreground_hwnd(self) -> int:
            return 999  # Different window in foreground!

    def _ok_invoke(cmd: Any, **_kw: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout='{"found": true, "acted": true, "method": "invoke"}',
            stderr="",
        )

    uia_res = windows_os.ui_act(
        "e1",
        "click",
        powershell_runner=_ok_invoke,
        focus_backend=_UnfocusedBackend(),  # type: ignore[arg-type]
        _capture_snapshot=False,
    )
    assert uia_res["ok"] is True
    assert uia_res["method"] == "invoke"

    # Background CDP call must never touch Win32 focus/hit-test gates either
    class _MockCDPBridge(CDPBridge):
        def act_sync(self, ref: str, action: str, **_kw: Any) -> dict[str, Any]:
            return {
                "ok": True,
                "status": "completed",
                "ref": ref,
                "action": action,
                "source": "cdp",
            }

    cdp_res = windows_os.ui_act(
        "b1",
        "click",
        cdp_bridge=_MockCDPBridge(devtools_port_path=tmp_path / "missing"),
        focus_backend=_UnfocusedBackend(),  # type: ignore[arg-type]
    )
    assert cdp_res["ok"] is True
    assert cdp_res["source"] == "cdp"


@pytest.mark.asyncio
async def test_control_system_invalidates_geometry_and_voice_runner_inherits_hwnd(
    tmp_path: Path,
) -> None:
    windows_os.set_last_screen_geometry(
        image_width=1280,
        image_height=720,
        screen_width=1920,
        screen_height=1080,
        target_hwnd=555,
    )
    assert windows_os.get_last_screen_geometry() is not None

    class _WinU32:
        def IsWindow(self, _hwnd: int) -> bool:
            return True

        def IsWindowVisible(self, _hwnd: int) -> bool:
            return True

        def IsIconic(self, _hwnd: int) -> bool:
            return False

        def ShowWindow(self, _hwnd: int, _cmd: int) -> bool:
            return True

        def GetWindowTextLengthW(self, _hwnd: int) -> int:
            return 6

        def GetWindowTextW(self, _hwnd: int, buf: Any, _n: int) -> int:
            buf.value = "Player"
            return 6

    windows_os.control_system(
        "minimize_window",
        hwnd=555,
        user32=_WinU32(),
        window_enumerator=lambda: [],
    )
    assert windows_os.get_last_screen_geometry() is None

    # VoiceRunner inherits expected_hwnd from open_or_focus_app & look_at_screen,
    # clears on minimize, and fires OsCancelInflightRequest when gui_action times out.
    sent: list[VoiceToolResponse] = []
    rpc_requests: list[Any] = []

    class _DummyBackend(VoiceBackend):
        backend_name = "dummy"

        @property
        def model_name(self) -> str:
            return "dummy"

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
            sent.extend(responses)

        async def events(self) -> AsyncIterator[VoiceBackendEvent]:
            if False:
                yield  # pragma: no cover

    runner = VoiceRunner(
        config=VoiceConfig(),
        data_dir=tmp_path,
        backend_factory=_DummyBackend,
        enable_hotkey=False,
        screen_service=ScreenCaptureService(
            grabber=lambda **_kw: Image.new("RGB", (640, 360), "black"),
        ),
    )
    backend = _DummyBackend()
    simulate_timeout = False

    async def _fake_rpc(req: Any, *, timeout: float = 10.0) -> dict[str, Any]:
        rpc_requests.append(req)
        if req.method == "os.open_or_focus_app":
            return {"ok": True, "status": "focused", "app": "notepad", "hwnd": 777}
        if req.method == "os.gui_action" and simulate_timeout:
            raise TimeoutError("timed out")
        return {"ok": True, "status": "completed", "action": "click_at"}

    runner._rpc = _fake_rpc  # type: ignore[method-assign]

    await runner._handle_tool_call(
        ToolCallEvent(call_id="c1", name="open_or_focus_app", arguments={"app_name": "notepad"}),
        backend,
    )
    assert runner._active_target_hwnd == 777

    await runner._handle_tool_call(
        ToolCallEvent(
            call_id="c2",
            name="gui_action",
            arguments={"action": "click_at", "x": 120, "y": 240},
        ),
        backend,
    )
    gui_req = [r for r in rpc_requests if isinstance(r, OsGuiActionRequest)][-1]
    assert gui_req.params.expected_hwnd == 777

    simulate_timeout = True
    await runner._handle_tool_call(
        ToolCallEvent(
            call_id="c3",
            name="gui_action",
            arguments={"action": "type_text", "text": "hello"},
        ),
        backend,
    )
    assert any(isinstance(r, OsCancelInflightRequest) for r in rpc_requests)


def test_circuit_breaker_unintended_focus_switch_trips_immediately_and_same_proc_resets() -> None:
    cb = ActionCircuitBreaker()
    spotify = {"hwnd": 100, "title": "Spotify", "process_name": "Spotify.exe"}
    ide = {"hwnd": 200, "title": "Antigravity", "process_name": "Antigravity.exe"}

    # Blind click leaks into another process -> must trip immediately, NOT reset progress!
    cb.record_outcome(
        "gui_action",
        {"action": "click_at", "x": 10, "y": 10},
        result={"ok": True},
        state_diff=compute_state_diff(spotify, ide),
    )
    assert cb.tripped is True
    assert "unintended_focus_switch" in cb.trip_reason

    # Same-process progress still resets no-progress streak
    cb2 = ActionCircuitBreaker()
    s0 = {"hwnd": 501, "title": "Home", "process_name": "chrome.exe", "url": "https://a/1"}
    s1 = {"hwnd": 501, "title": "Next", "process_name": "chrome.exe", "url": "https://a/2"}
    cb2.record_outcome(
        "gui_action",
        {"action": "click_at", "x": 10, "y": 10},
        result={"ok": True},
        state_diff=compute_state_diff(s0, s0),
    )
    assert cb2.blind_click_no_progress_count == 1
    cb2.record_outcome(
        "gui_action",
        {"action": "click_at", "x": 20, "y": 20},
        result={"ok": True},
        state_diff=compute_state_diff(s0, s1),
    )
    assert cb2.tripped is False
    assert cb2.blind_click_no_progress_count == 0


class _DummyProvider(LLMProvider):
    async def stream_turn(
        self,
        *,
        messages: list[Message],
        tools: list[ToolDefinition],
        system_prompt: str,
        response_format: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(type="text_delta", text="ok")
        yield StreamChunk(type="done")


@pytest.mark.asyncio
async def test_daemon_out_of_band_os_cancel_inflight_preempts_slow_gui_action(
    tmp_path: Path,
) -> None:
    store = MemoryStore(base_dir=tmp_path / "mem")
    agent = AgentLoop(provider=_DummyProvider(), registry=ToolRegistry(), memory=store)
    daemon = CleaDaemon(agent=agent, port=0, token="test-token-123456")

    started = threading.Event()
    finished_cancelled = False

    def _slow_gui_action(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal finished_cancelled
        cancel_ev = kwargs.get("cancel_event")
        assert isinstance(cancel_ev, threading.Event)
        started.set()
        fired = cancel_ev.wait(timeout=3.0)
        finished_cancelled = bool(fired)
        if fired:
            return {
                "ok": False,
                "status": "cancelled",
                "cancelled": True,
                "error": "os_action_cancelled",
                "action": "type_text",
            }
        return {"ok": True, "status": "completed", "action": "type_text"}

    with patch("clea.core.daemon.gui_action", side_effect=_slow_gui_action):
        await daemon.start()
        try:
            url = f"ws://127.0.0.1:{daemon.bound_port}"
            async with connect(url, subprotocols=auth.client_subprotocols(daemon.token)) as ws:
                gui_req = OsGuiActionRequest(params={"action": "type_text", "text": "slow"})  # type: ignore[arg-type]
                await ws.send(gui_req.model_dump_json())

                deadline = time.monotonic() + 2.0
                while not started.is_set() and time.monotonic() < deadline:
                    await asyncio.sleep(0.01)
                assert started.is_set()

                cancel_req = OsCancelInflightRequest()
                await ws.send(cancel_req.model_dump_json())

                frames = []
                for _ in range(2):
                    raw = await asyncio.wait_for(ws.recv(), timeout=2.0)
                    frames.append(parse_frame(raw))

                by_id = {f.id: f for f in frames}
                cancel_resp = by_id[cancel_req.id]
                gui_resp = by_id[gui_req.id]
                assert cancel_resp.ok is True
                assert cancel_resp.result["cancelled"] is True
                assert cancel_resp.result["cancelled_count"] >= 1
                assert finished_cancelled is True
                assert gui_resp.ok is True
                assert gui_resp.result["status"] == "cancelled"
                assert gui_resp.result["cancelled"] is True
        finally:
            await daemon.stop()

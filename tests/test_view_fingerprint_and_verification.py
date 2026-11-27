"""Deterministic unit tests for in-app view fingerprinting, verification scope,
circuit-breaker view tracking, protocol models, and daemon forensic telemetry.

Zero live Win32/desktop hooks, zero real processes, zero writes to %LOCALAPPDATA%\\clea.
"""

from __future__ import annotations

import json
import threading
from pathlib import Path
from typing import Any
from unittest.mock import patch

import pytest
from PIL import Image, ImageDraw

from clea.core.agent import AgentLoop
from clea.core.circuit_breaker import (
    ActionCircuitBreaker,
    compute_state_diff,
    is_state_diff_tool,
    merge_tool_state_diff,
)
from clea.core.daemon import CleaDaemon, _Connection
from clea.core.protocol import (
    OsClickUiElementParams,
    OsClickUiElementRequest,
    OsControlMediaVolumeParams,
    OsControlMediaVolumeRequest,
    OsControlSystemParams,
    OsControlSystemRequest,
    OsGuiActionParams,
    OsGuiActionRequest,
    OsInspectScreenParams,
    OsInspectScreenRequest,
    OsOpenOrFocusAppParams,
    OsOpenOrFocusAppRequest,
    OsOpenUrlParams,
    OsOpenUrlRequest,
    OsUiActParams,
    OsUiActRequest,
    ResponseFrame,
    parse_frame,
)
from clea.memory.store import MemoryStore
from clea.providers.base import LLMProvider, StreamChunk
from clea.tools.guards import (
    MediaSessionState,
    WindowContext,
    apply_verification_metadata,
    classify_action_verification,
)
from clea.tools.registry import AuditLogger, ToolRegistry
from clea.tools.view_fingerprint import (
    DEFAULT_DHASH_THRESHOLD,
    WindowViewFingerprint,
    cancellable_settle_wait,
    capture_window_view_fingerprint,
    compare_view_fingerprints,
    compute_dhash_256,
    dhash_to_hex,
    extract_uia_heading_signature,
    normalized_hamming_distance,
)
from clea.tools.vision import ScreenInspectionResult
from clea.tools.windows_os import is_registered_url_protocol

# --------------------------------------------------------------------------- Helpers & Doubles


def _make_home_view_image(width: int = 320, height: int = 200) -> Image.Image:
    """Synthetic structured UI view (sidebar + header + card grid)."""
    img = Image.new("RGB", (width, height), (24, 24, 28))
    draw = ImageDraw.Draw(img)
    # Left sidebar
    draw.rectangle([0, 0, 72, height], fill=(16, 16, 20))
    for i in range(5):
        y = 18 + i * 26
        draw.rectangle([10, y, 60, y + 12], fill=(64, 68, 80))
    # Top banner
    draw.rectangle([82, 10, width - 12, 48], fill=(42, 92, 168))
    # Card grid
    for row in range(2):
        for col in range(3):
            x0 = 86 + col * 74
            y0 = 60 + row * 64
            draw.rectangle([x0, y0, x0 + 62, y0 + 50], fill=(55 + col * 20, 58 + row * 25, 72))
    return img


def _make_page_jump_image(width: int = 320, height: int = 200) -> Image.Image:
    """Distinct in-app view (light settings form with bright left-to-right toggle rows)."""
    img = Image.new("RGB", (width, height), (236, 238, 244))
    draw = ImageDraw.Draw(img)
    # Right-aligned wide hero + alternating horizontal stripes
    draw.rectangle([16, 14, width - 16, 54], fill=(24, 140, 96))
    for i in range(4):
        y = 68 + i * 30
        draw.rectangle([16, y, width - 16, y + 20], fill=(255, 255, 255))
        draw.rectangle([width - 54, y + 4, width - 24, y + 16], fill=(20, 120, 220))
    return img


class _DummyProvider(LLMProvider):
    provider_name = "dummy"
    model_name = "dummy-model"

    async def stream_turn(self, messages: Any, tools: Any = None, **kwargs: Any):  # type: ignore[override]
        yield StreamChunk(text="ok")


class _FakeWebSocket:
    def __init__(self) -> None:
        self.sent_raw: list[str] = []

    async def send(self, payload: str) -> None:
        self.sent_raw.append(payload)

    def last_response(self) -> ResponseFrame:
        assert self.sent_raw, "No frame sent on fake websocket"
        frame = parse_frame(self.sent_raw[-1])
        assert isinstance(frame, ResponseFrame)
        return frame


class _FakeFocusBackend:
    def __init__(self, ctx: WindowContext | None = None) -> None:
        self.ctx = ctx or WindowContext(
            hwnd=4200,
            title="Settings",
            process_name="SystemSettings.exe",
            pid=1234,
            bounds=(100, 80, 1100, 780),
            work_area=(0, 0, 1920, 1040),
            minimized=False,
        )

    def get_foreground_hwnd(self) -> int | None:
        return self.ctx.hwnd if self.ctx else None

    def get_foreground_context(self) -> WindowContext | None:
        return self.ctx

    def get_window_context(self, hwnd: int) -> WindowContext | None:
        if self.ctx and self.ctx.hwnd == hwnd:
            return self.ctx
        return None

    def is_window_valid(self, hwnd: int) -> bool:
        return self.ctx is not None and self.ctx.hwnd == hwnd

    def is_minimized(self, hwnd: int) -> bool:
        return bool(self.ctx and self.ctx.minimized)

    def get_extended_frame_bounds(self, hwnd: int) -> tuple[int, int, int, int] | None:
        return self.ctx.bounds if self.ctx else None

    def get_monitor_work_area(self, hwnd: int) -> tuple[int, int, int, int] | None:
        return self.ctx.work_area if self.ctx else None


class _FakeMediaBackend:
    def __init__(self, state: MediaSessionState) -> None:
        self.state = state

    def get_active_session(self, timeout_s: float = 0.5) -> MediaSessionState:
        return self.state


def _make_test_daemon(
    tmp_path: Path,
    *,
    focus_backend: Any = None,
    media_backend: Any = None,
    view_fingerprint_fn: Any = None,
    view_settle_fn: Any = None,
) -> tuple[CleaDaemon, _Connection, _FakeWebSocket, Path]:
    mem = MemoryStore(base_dir=tmp_path / "memory")
    audit_path = tmp_path / "audit.jsonl"
    reg = ToolRegistry(audit_logger=AuditLogger(log_path=audit_path))
    agent = AgentLoop(provider=_DummyProvider(), registry=reg, memory=mem)
    daemon = CleaDaemon(
        agent=agent,
        token="test-token",
        token_file=tmp_path / "token",
        lock_file=tmp_path / "daemon.lock",
        focus_backend=focus_backend,
        media_backend=media_backend,
        view_fingerprint_fn=view_fingerprint_fn,
        view_settle_seconds=0.0,
        view_settle_fn=view_settle_fn,
    )
    ws = _FakeWebSocket()
    conn = _Connection(ws)  # type: ignore[arg-type]
    return daemon, conn, ws, audit_path


def _read_audit_entries(audit_path: Path) -> list[dict[str, Any]]:
    if not audit_path.exists():
        return []
    return [
        json.loads(line)
        for line in audit_path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


# --------------------------------------------------------------------------- 1. dHash & Fingerprint


def test_dhash_stability_on_minor_noise_vs_genuine_page_jump() -> None:
    base_img = _make_home_view_image()
    noisy_img = base_img.copy()
    draw = ImageDraw.Draw(noisy_img)
    # Simulate blinking caret (2x10 px) + tiny clock/equalizer wiggle (6x4 px)
    draw.rectangle([150, 22, 152, 32], fill=(255, 255, 255))
    draw.rectangle([305, 188, 311, 192], fill=(120, 210, 140))

    jump_img = _make_page_jump_image()

    h_base = compute_dhash_256(base_img)
    h_noisy = compute_dhash_256(noisy_img)
    h_jump = compute_dhash_256(jump_img)

    hex_base = dhash_to_hex(h_base)
    hex_noisy = dhash_to_hex(h_noisy)
    hex_jump = dhash_to_hex(h_jump)

    assert len(hex_base) == 64
    assert len(hex_noisy) == 64
    assert len(hex_jump) == 64

    noise_delta = normalized_hamming_distance(hex_base, hex_noisy)
    jump_delta = normalized_hamming_distance(hex_base, hex_jump)
    assert normalized_hamming_distance(h_base, h_jump) == pytest.approx(jump_delta)

    assert 0.0 <= noise_delta < DEFAULT_DHASH_THRESHOLD
    assert jump_delta >= DEFAULT_DHASH_THRESHOLD

    fp_before = capture_window_view_fingerprint(
        101, capture_fn=lambda _h: base_img, heading_fn=lambda _h: "Home"
    )
    fp_noisy = capture_window_view_fingerprint(
        101, capture_fn=lambda _h: noisy_img, heading_fn=lambda _h: "Home"
    )
    fp_jump = capture_window_view_fingerprint(
        101, capture_fn=lambda _h: jump_img, heading_fn=lambda _h: "Home"
    )
    assert fp_before is not None and fp_noisy is not None and fp_jump is not None
    assert fp_before.source == "injected"

    cmp_noise = compare_view_fingerprints(fp_before, fp_noisy)
    assert cmp_noise["view_changed"] is False
    assert cmp_noise["heading_changed"] is False
    assert cmp_noise["visual_delta"] < DEFAULT_DHASH_THRESHOLD

    cmp_jump = compare_view_fingerprints(fp_before, fp_jump)
    assert cmp_jump["view_changed"] is True
    assert cmp_jump["visual_delta"] >= DEFAULT_DHASH_THRESHOLD


def test_uia_heading_delta_detection_and_chrome_only_skip() -> None:
    called = False

    def _should_not_run(_hwnd: int | None) -> str:
        nonlocal called
        called = True
        return "Should Not Probe"

    assert (
        extract_uia_heading_signature(
            123,
            uia_probe_fn=_should_not_run,
            accessibility_coverage="chrome_only",
        )
        == ""
    )
    assert called is False

    # Non-throwing behavior on probe failure
    def _boom(_hwnd: int | None) -> str:
        raise RuntimeError("UIA COM timeout")

    assert extract_uia_heading_signature(123, uia_probe_fn=_boom) == ""

    # Heading delta triggers view_changed even with identical visual dHash
    same_hex = dhash_to_hex(0xABCDEF123456)
    before = WindowViewFingerprint(
        hwnd=500, dhash_hex=same_hex, heading="System > Display", captured_at=1.0, source="injected"
    )
    after = WindowViewFingerprint(
        hwnd=500,
        dhash_hex=same_hex,
        heading="Privacy & security > Microphone",
        captured_at=1.2,
        source="injected",
    )
    diff = compare_view_fingerprints(before, after)
    assert diff["visual_delta"] == 0.0
    assert diff["heading_changed"] is True
    assert diff["heading_before"] == "System > Display"
    assert diff["heading_after"] == "Privacy & security > Microphone"
    assert diff["view_changed"] is True


def test_cancellable_settle_wait_respects_cancel_event() -> None:
    ev = threading.Event()
    ev.set()
    slept: list[float] = []
    assert cancellable_settle_wait(0.25, cancel_event=ev, sleep_fn=slept.append) is False
    assert slept == []

    ev2 = threading.Event()
    assert cancellable_settle_wait(0.1, cancel_event=ev2, sleep_fn=slept.append) is True
    assert sum(slept) == pytest.approx(0.1)


# --------------------------------------------------------------------------- 2. Circuit Breaker


def test_circuit_breaker_view_diff_and_turn_persistence() -> None:
    assert is_state_diff_tool("control_system", {"action": "open_settings"}) is True
    assert is_state_diff_tool("os.control_system", {"action": "open_settings"}) is True
    assert is_state_diff_tool("control_system", {"action": "get_brightness"}) is False

    dhash_a = dhash_to_hex(0)
    dhash_b = dhash_to_hex((1 << 128) - 1)  # 50% Hamming delta

    diff_same = compute_state_diff(
        {
            "hwnd": 10,
            "title": "App",
            "process_name": "app.exe",
            "dhash": dhash_a,
            "heading": "Home",
        },
        {
            "hwnd": 10,
            "title": "App",
            "process_name": "app.exe",
            "dhash": dhash_a,
            "heading": "Home",
        },
    )
    assert diff_same["view_changed"] is False
    assert diff_same["changed"] is False

    diff_view = compute_state_diff(
        {
            "hwnd": 10,
            "title": "App",
            "process_name": "app.exe",
            "dhash": dhash_a,
            "heading": "Home",
        },
        {
            "hwnd": 10,
            "title": "App",
            "process_name": "app.exe",
            "dhash": dhash_b,
            "heading": "Liked",
        },
    )
    assert diff_view["window_changed"] is False
    assert diff_view["view_changed"] is True
    assert diff_view["visual_delta"] == pytest.approx(0.5)
    assert diff_view["heading_before"] == "Home"
    assert diff_view["heading_after"] == "Liked"
    assert diff_view["changed"] is True

    # Merge preserves view_changed
    merged = merge_tool_state_diff(diff_same, {"state_diff": diff_view})
    assert merged is not None
    assert merged["view_changed"] is True
    assert merged["changed"] is True

    # Circuit breaker: same-HWND view_changed=True counts as genuine forward progress
    breaker = ActionCircuitBreaker(max_blind_clicks_without_progress=2)
    breaker.record_outcome(
        "gui_action",
        {"action": "click_at", "x": 100, "y": 100},
        result={"ok": True},
        state_diff=diff_same,
    )
    assert breaker.blind_click_no_progress_count == 1
    assert breaker.current_window_view_key() == (10, dhash_a)
    assert breaker.has_active_blind_streak() is True

    # Unchanged (hwnd, dhash) across TurnComplete preserves streak
    was_reset = breaker.on_turn_complete(current_hwnd=10, current_dhash=dhash_a)
    assert was_reset is False
    assert breaker.blind_click_no_progress_count == 1

    # Verified in-app view jump resets no-progress streak (not falsely treated as bounce)
    breaker.record_outcome(
        "gui_action",
        {"action": "click_at", "x": 120, "y": 140},
        result={"ok": True},
        state_diff=diff_view,
    )
    assert breaker.blind_click_no_progress_count == 0
    assert breaker.tripped is False
    assert breaker.last_dhash == dhash_b

    # Bouncing visually back to dhash_a triggers bounce detection
    diff_bounce = compute_state_diff(
        {
            "hwnd": 10,
            "title": "App",
            "process_name": "app.exe",
            "dhash": dhash_b,
            "heading": "Liked",
        },
        {
            "hwnd": 10,
            "title": "App",
            "process_name": "app.exe",
            "dhash": dhash_a,
            "heading": "Home",
        },
    )
    breaker.record_outcome(
        "gui_action",
        {"action": "click_at", "x": 120, "y": 140},
        result={"ok": True},
        state_diff=diff_bounce,
    )
    assert breaker.tripped is True

    # Changing target window resets breaker cleanly on turn complete
    assert breaker.on_turn_complete(current_hwnd=999, current_dhash=dhash_b) is True
    assert breaker.tripped is False
    assert breaker.blind_click_no_progress_count == 0


# --------------------------------------------------------------------------- 3. Verification Scope


def test_verification_scope_contract_across_mutating_tools() -> None:
    # control_media_volume: media_session; False when unverified or when expected in_app_view
    assert classify_action_verification(
        "control_media_volume",
        action="play_pause",
        result={"ok": True, "action": "play_pause", "verification": {"verified": True}},
    ) == ("media_session", True)
    assert classify_action_verification(
        "control_media_volume",
        action="play_pause",
        result={"ok": True, "action": "play_pause", "verification": {"verified": True}},
        expected_scope="in_app_view",
    ) == ("media_session", False)
    assert classify_action_verification(
        "control_media_volume",
        action="play_pause",
        result={"ok": True, "action": "play_pause", "verification": {"verified": False}},
    ) == ("media_session", False)

    # control_system("open_settings"): dispatch_only/False unchanged, in_app_view/True on jump
    assert classify_action_verification(
        "control_system",
        action="open_settings",
        result={"ok": True, "action": "open_settings"},
        state_diff={"changed": False, "window_changed": False, "view_changed": False},
    ) == ("dispatch_only", False)
    assert classify_action_verification(
        "control_system",
        action="open_settings",
        result={"ok": True, "action": "open_settings"},
        state_diff={"changed": True, "window_changed": False, "view_changed": True},
    ) == ("in_app_view", True)

    # custom-protocol open_url
    assert classify_action_verification(
        "open_url",
        result={"ok": True, "protocol_scheme": "myapp", "resolution_layer": "url_protocol"},
        state_diff={"changed": False, "window_changed": False, "view_changed": False},
    ) == ("dispatch_only", False)
    assert classify_action_verification(
        "open_url",
        result={"ok": True, "protocol_scheme": "myapp", "resolution_layer": "url_protocol"},
        state_diff={"changed": True, "window_changed": False, "view_changed": True},
    ) == ("in_app_view", True)

    # blind gui_action
    assert classify_action_verification(
        "gui_action",
        action="click_at",
        result={"ok": True, "status": "completed"},
        state_diff={"changed": False, "window_changed": False, "view_changed": False},
    ) == ("dispatch_only", False)
    assert classify_action_verification(
        "gui_action",
        action="click_at",
        result={"ok": True, "status": "completed"},
        state_diff={"changed": True, "window_changed": False, "view_changed": True},
    ) == ("in_app_view", True)

    # apply_verification_metadata upgrades dispatch_only/False when state_diff proves view_changed
    upgraded = apply_verification_metadata(
        "open_url",
        {"ok": True, "verification_scope": "dispatch_only", "goal_verified": False},
        state_diff={"changed": True, "view_changed": True},
    )
    assert upgraded["verification_scope"] == "in_app_view"
    assert upgraded["goal_verified"] is True


# ----------------------------------------------------------------- 4. Daemon Wiring & Telemetry


@pytest.mark.asyncio
async def test_daemon_open_settings_noop_vs_view_changed_and_audit(tmp_path: Path) -> None:
    same_hex = dhash_to_hex(0x1111)
    jump_hex = dhash_to_hex(0xFFFFFFFFFFFFFFFF0000000000000000)

    # Case A: open_settings no-op (unchanged view, unchanged window)
    fps_noop = iter(
        [
            WindowViewFingerprint(hwnd=4200, dhash_hex=same_hex, heading="Home", source="injected"),
            WindowViewFingerprint(hwnd=4200, dhash_hex=same_hex, heading="Home", source="injected"),
        ]
    )
    settle_calls: list[float] = []
    daemon, conn, ws, audit_path = _make_test_daemon(
        tmp_path,
        focus_backend=_FakeFocusBackend(),
        view_fingerprint_fn=lambda _hwnd=None: next(fps_noop),
        view_settle_fn=lambda secs, **_kw: settle_calls.append(secs) or True,
    )

    req = OsControlSystemRequest(
        params=OsControlSystemParams(action="open_settings", page="privacy-microphone")
    )
    with patch(
        "clea.core.daemon.control_system",
        return_value={
            "ok": True,
            "success": True,
            "action": "open_settings",
            "status": "success",
            "uri": "ms-settings:privacy-microphone",
        },
    ):
        await daemon._handle_os_control_system(conn, req)

    resp = ws.last_response()
    assert resp.ok is True
    assert resp.result is not None
    assert resp.result["verification_scope"] == "dispatch_only"
    assert resp.result["goal_verified"] is False
    assert resp.result["state_diff"]["view_changed"] is False
    assert len(settle_calls) == 1

    entries = _read_audit_entries(audit_path)
    assert len(entries) == 1
    assert entries[0]["action"] == "os_control_system"
    details = entries[0]["details"]
    assert details["system_action"] == "open_settings"
    assert details["page"] == "privacy-microphone"
    assert details["uri"] == "ms-settings:privacy-microphone"
    assert details["view_changed"] is False
    assert details["goal_verified"] is False
    assert details["verification_scope"] == "dispatch_only"

    # Case B: open_settings with genuine view jump -> in_app_view + goal_verified=True
    fps_jump = iter(
        [
            WindowViewFingerprint(hwnd=4200, dhash_hex=same_hex, heading="Home", source="injected"),
            WindowViewFingerprint(
                hwnd=4200, dhash_hex=jump_hex, heading="Microphone", source="injected"
            ),
        ]
    )
    daemon._view_fingerprint_fn = lambda _hwnd=None: next(fps_jump)
    with patch(
        "clea.core.daemon.control_system",
        return_value={
            "ok": True,
            "success": True,
            "action": "open_settings",
            "status": "success",
            "uri": "ms-settings:privacy-microphone",
        },
    ):
        await daemon._handle_os_control_system(conn, req)

    resp2 = ws.last_response()
    assert resp2.ok is True
    assert resp2.result is not None
    assert resp2.result["verification_scope"] == "in_app_view"
    assert resp2.result["goal_verified"] is True
    assert resp2.result["state_diff"]["view_changed"] is True
    assert resp2.result["state_diff"]["heading_after"] == "Microphone"


@pytest.mark.asyncio
async def test_daemon_open_url_custom_protocol_and_dangerous_scheme_error_audit(
    tmp_path: Path,
) -> None:
    dhash_a = dhash_to_hex(0x0)
    dhash_b = dhash_to_hex((1 << 128) - 1)
    fps = iter(
        [
            WindowViewFingerprint(hwnd=700, dhash_hex=dhash_a, heading="Home", source="injected"),
            WindowViewFingerprint(hwnd=700, dhash_hex=dhash_a, heading="Home", source="injected"),
            WindowViewFingerprint(hwnd=700, dhash_hex=dhash_a, heading="Home", source="injected"),
            WindowViewFingerprint(
                hwnd=700, dhash_hex=dhash_b, heading="Playlist", source="injected"
            ),
        ]
    )
    daemon, conn, ws, audit_path = _make_test_daemon(
        tmp_path,
        focus_backend=_FakeFocusBackend(
            WindowContext(hwnd=700, title="MediaApp", process_name="MediaApp.exe")
        ),
        view_fingerprint_fn=lambda _hwnd=None: next(fps),
    )

    # 1. Custom protocol dispatch with no view/window change -> dispatch_only / False
    req_proto = OsOpenUrlRequest(params=OsOpenUrlParams(url="customapp:collection:tracks"))
    with patch(
        "clea.core.daemon.open_url",
        return_value={
            "ok": True,
            "success": True,
            "status": "launched",
            "url": "customapp:collection:tracks",
            "resolution_layer": "url_protocol",
            "protocol_scheme": "customapp",
        },
    ):
        await daemon._handle_os_open_url(conn, req_proto)

    resp_noop = ws.last_response()
    assert resp_noop.ok is True
    assert resp_noop.result is not None
    assert resp_noop.result["verification_scope"] == "dispatch_only"
    assert resp_noop.result["goal_verified"] is False
    assert resp_noop.result["protocol_scheme"] == "customapp"

    # 2. Custom protocol dispatch with view change -> in_app_view / True
    with patch(
        "clea.core.daemon.open_url",
        return_value={
            "ok": True,
            "success": True,
            "status": "launched",
            "url": "customapp:collection:tracks",
            "resolution_layer": "url_protocol",
            "protocol_scheme": "customapp",
        },
    ):
        await daemon._handle_os_open_url(conn, req_proto)

    resp_ok = ws.last_response()
    assert resp_ok.ok is True
    assert resp_ok.result is not None
    assert resp_ok.result["verification_scope"] == "in_app_view"
    assert resp_ok.result["goal_verified"] is True

    # 3. Dangerous scheme raises ValueError -> error response + structured error audit
    req_bad = OsOpenUrlRequest(params=OsOpenUrlParams(url="ms-msdt:/id PCWDiagnostic"))
    with patch(
        "clea.core.daemon.open_url",
        side_effect=ValueError("Disallowed URL scheme 'ms-msdt'"),
    ):
        await daemon._handle_os_open_url(conn, req_bad)

    resp_err = ws.last_response()
    assert resp_err.ok is False
    assert "ms-msdt" in (resp_err.error or "")

    entries = _read_audit_entries(audit_path)
    assert len(entries) == 3
    assert entries[0]["details"]["scheme"] == "customapp"
    assert entries[0]["details"]["goal_verified"] is False
    assert entries[1]["details"]["view_changed"] is True
    assert entries[1]["details"]["goal_verified"] is True
    assert entries[2]["action"] == "os_open_url"
    assert entries[2]["details"]["status"] == "error"
    assert entries[2]["details"]["scheme"] == "ms-msdt"
    assert entries[2]["details"]["error_type"] == "ValueError"


@pytest.mark.asyncio
async def test_daemon_gui_action_ui_act_inspect_screen_and_sanitized_audit(
    tmp_path: Path,
) -> None:
    dhash_a = dhash_to_hex(0x0)
    dhash_b = dhash_to_hex((1 << 128) - 1)
    fps = iter(
        [
            # gui_action type_text (secret must never be logged)
            WindowViewFingerprint(hwnd=900, dhash_hex=dhash_a, heading="Login", source="injected"),
            WindowViewFingerprint(hwnd=900, dhash_hex=dhash_a, heading="Login", source="injected"),
            # gui_action click_at with capture_id + capture_bounds + view jump
            WindowViewFingerprint(hwnd=900, dhash_hex=dhash_a, heading="Login", source="injected"),
            WindowViewFingerprint(
                hwnd=900, dhash_hex=dhash_b, heading="Dashboard", source="injected"
            ),
            # ui_act set_value (secret value must never be logged)
            WindowViewFingerprint(
                hwnd=900, dhash_hex=dhash_b, heading="Dashboard", source="injected"
            ),
            WindowViewFingerprint(
                hwnd=900, dhash_hex=dhash_b, heading="Dashboard", source="injected"
            ),
        ]
    )
    daemon, conn, ws, audit_path = _make_test_daemon(
        tmp_path,
        focus_backend=_FakeFocusBackend(
            WindowContext(hwnd=900, title="App", process_name="App.exe")
        ),
        view_fingerprint_fn=lambda _hwnd=None: next(fps),
    )

    # 1. inspect_screen forwards hwnd, capture_id, capture_bounds
    fake_inspect = ScreenInspectionResult(
        status="ok",
        source="active_window",
        window_title="App",
        process_name="App.exe",
        width=1280,
        height=720,
        image_width=1280,
        image_height=720,
        screen_width=1280,
        screen_height=720,
        virtual_left=100,
        virtual_top=50,
        coord_space="normalized_1000",
    )
    # Attach fields even before coord-vision adds them to ScreenInspectionResult if run standalone
    object.__setattr__(fake_inspect, "hwnd", 900)
    object.__setattr__(fake_inspect, "capture_id", "cap_abc123")
    object.__setattr__(fake_inspect, "capture_bounds", (100, 50, 1380, 770))

    with patch.object(daemon.screen_vision_service, "inspect", return_value=fake_inspect):
        await daemon._handle_os_inspect_screen(
            conn,
            OsInspectScreenRequest(
                params=OsInspectScreenParams(target="auto", include_image=False)
            ),
        )
    resp_ins = ws.last_response()
    assert resp_ins.ok is True
    assert resp_ins.result is not None
    assert resp_ins.result["hwnd"] == 900
    assert resp_ins.result["capture_id"] == "cap_abc123"
    assert resp_ins.result["capture_bounds"] == [100, 50, 1380, 770]

    # 2. gui_action(type_text) must never leak secret text in audit.jsonl
    secret_text = "SUPER_SECRET_PASSWORD_999!"
    with patch(
        "clea.core.daemon.gui_action",
        return_value={
            "ok": True,
            "status": "completed",
            "action": "type_text",
            "char_count": len(secret_text),
        },
    ):
        await daemon._handle_os_gui_action(
            conn,
            OsGuiActionRequest(
                params=OsGuiActionParams(action="type_text", text=secret_text, expected_hwnd=900)
            ),
        )
    assert secret_text not in audit_path.read_text(encoding="utf-8")

    # 3. gui_action(click_at) forwards capture_id/capture_bounds and audits geometry + verification
    received_kwargs: dict[str, Any] = {}

    def _fake_gui_action(*args: Any, **kwargs: Any) -> dict[str, Any]:
        received_kwargs.update(kwargs)
        return {
            "ok": True,
            "status": "completed",
            "action": "click_at",
            "coord_space": "normalized_1000",
            "x": 500,
            "y": 400,
            "physical_x": 740,
            "physical_y": 338,
            "expected_hwnd": 900,
            "actual_hwnd": 900,
        }

    with patch("clea.core.daemon.gui_action", side_effect=_fake_gui_action):
        await daemon._handle_os_gui_action(
            conn,
            OsGuiActionRequest(
                params=OsGuiActionParams(
                    action="click_at",
                    x=500,
                    y=400,
                    coord_space="normalized_1000",
                    expected_hwnd=900,
                    capture_id="cap_abc123",
                    capture_bounds=[100, 50, 1380, 770],
                )
            ),
        )
    assert received_kwargs["capture_id"] == "cap_abc123"
    assert received_kwargs["capture_bounds"] == [100, 50, 1380, 770]

    resp_click = ws.last_response()
    assert resp_click.ok is True
    assert resp_click.result is not None
    assert resp_click.result["verification_scope"] == "in_app_view"
    assert resp_click.result["goal_verified"] is True

    # 4. ui_act logs role/name/method/view_changed/goal_verified and never raw value
    secret_val = "TOP_SECRET_API_KEY_42"
    with patch(
        "clea.core.daemon.ui_act",
        return_value={
            "ok": True,
            "status": "completed",
            "action": "set_value",
            "ref": "e3",
            "role": "input",
            "name": "API Key",
            "method": "value_pattern",
            "target_state": {"available": True, "value": secret_val},
        },
    ):
        await daemon._handle_os_ui_act(
            conn,
            OsUiActRequest(params=OsUiActParams(ref="e3", action="set_value", value=secret_val)),
        )
    resp_act = ws.last_response()
    assert resp_act.ok is True
    assert resp_act.result is not None
    assert resp_act.result["verification_scope"] == "ui_element_delta"
    assert resp_act.result["goal_verified"] is True

    raw_audit = audit_path.read_text(encoding="utf-8")
    assert secret_text not in raw_audit
    assert secret_val not in raw_audit

    entries = _read_audit_entries(audit_path)
    gui_click_entry = [e for e in entries if e["action"] == "os_gui_action"][-1]["details"]
    assert gui_click_entry["coord_space"] == "normalized_1000"
    assert gui_click_entry["x"] == 500
    assert gui_click_entry["y"] == 400
    assert gui_click_entry["physical_x"] == 740
    assert gui_click_entry["physical_y"] == 338
    assert gui_click_entry["expected_hwnd"] == 900
    assert gui_click_entry["actual_hwnd"] == 900
    assert gui_click_entry["state_diff_changed"] is True
    assert gui_click_entry["view_changed"] is True
    assert gui_click_entry["goal_verified"] is True

    ui_act_entry = [e for e in entries if e["action"] == "os_ui_act"][-1]["details"]
    assert ui_act_entry["ref"] == "e3"
    assert ui_act_entry["ui_action"] == "set_value"
    assert ui_act_entry["role"] == "input"
    assert ui_act_entry["name"] == "API Key"
    assert ui_act_entry["method"] == "value_pattern"
    assert ui_act_entry["view_changed"] is False
    assert ui_act_entry["goal_verified"] is True


@pytest.mark.asyncio
async def test_daemon_media_and_open_app_verification_scope_and_exception_audits(
    tmp_path: Path,
) -> None:
    daemon, conn, ws, audit_path = _make_test_daemon(
        tmp_path,
        focus_backend=_FakeFocusBackend(),
        media_backend=_FakeMediaBackend(
            MediaSessionState(playback_status="paused", title="Song A", artist="Artist A")
        ),
    )

    # control_media_volume unverified -> media_session / False
    with patch(
        "clea.core.daemon.control_media_volume",
        return_value={
            "ok": True,
            "action": "play_pause",
            "level": None,
            "verification": {
                "verified": False,
                "status": "timeout",
                "reason": "media_state_unchanged",
            },
        },
    ):
        await daemon._handle_os_control_media_volume(
            conn,
            OsControlMediaVolumeRequest(params=OsControlMediaVolumeParams(action="play_pause")),
        )
    resp_media = ws.last_response()
    assert resp_media.ok is True
    assert resp_media.result is not None
    assert resp_media.result["verification_scope"] == "media_session"
    assert resp_media.result["goal_verified"] is False

    # open_or_focus_app focused with verified=True -> window_focus / True
    with patch(
        "clea.core.daemon.open_or_focus_app",
        return_value={
            "ok": True,
            "status": "focused",
            "app": "settings",
            "hwnd": 4200,
            "verification": {
                "verified": True,
                "status": "verified",
                "reason": "window_state_matched",
            },
        },
    ):
        await daemon._handle_os_open_or_focus_app(
            conn,
            OsOpenOrFocusAppRequest(params=OsOpenOrFocusAppParams(app_name="settings")),
        )
    resp_app = ws.last_response()
    assert resp_app.ok is True
    assert resp_app.result is not None
    assert resp_app.result["verification_scope"] == "window_focus"
    assert resp_app.result["goal_verified"] is True

    # Structured error audits for exceptions prior to completion in all 5 mutating handlers
    with patch("clea.core.daemon.control_system", side_effect=ValueError("bad settings uri")):
        await daemon._handle_os_control_system(
            conn,
            OsControlSystemRequest(
                params=OsControlSystemParams(action="open_settings", page="bad")
            ),
        )
    with patch("clea.core.daemon.gui_action", side_effect=RuntimeError("SendInput failed")):
        await daemon._handle_os_gui_action(
            conn, OsGuiActionRequest(params=OsGuiActionParams(action="click_at", x=10, y=20))
        )
    with patch("clea.core.daemon.ui_act", side_effect=TimeoutError("UIA COM timed out")):
        await daemon._handle_os_ui_act(
            conn, OsUiActRequest(params=OsUiActParams(ref="e1", action="click"))
        )
    with patch("clea.core.daemon.open_url", side_effect=ValueError("blocked scheme")):
        await daemon._handle_os_open_url(
            conn, OsOpenUrlRequest(params=OsOpenUrlParams(url="javascript:alert(1)"))
        )
    with patch("clea.core.daemon.click_ui_element", side_effect=OSError("window gone")):
        await daemon._handle_os_click_ui_element(
            conn, OsClickUiElementRequest(params=OsClickUiElementParams(name="Submit"))
        )

    entries = _read_audit_entries(audit_path)
    error_entries = {
        e["action"]: e["details"]
        for e in entries
        if isinstance(e.get("details"), dict) and e["details"].get("status") == "error"
    }
    assert set(error_entries.keys()) == {
        "os_control_system",
        "os_gui_action",
        "os_ui_act",
        "os_open_url",
        "os_click_ui_element",
    }
    assert error_entries["os_control_system"]["error_type"] == "ValueError"
    assert error_entries["os_gui_action"]["error_type"] == "RuntimeError"
    assert error_entries["os_ui_act"]["error_type"] == "TimeoutError"
    assert error_entries["os_open_url"]["error_type"] == "ValueError"
    assert error_entries["os_click_ui_element"]["error_type"] == "OSError"


def test_custom_url_protocol_registry_helper_and_dangerous_scheme_blocklist() -> None:
    # Registered safe custom protocol returns True via injected registry lookup
    assert (
        is_registered_url_protocol("customapp", registry_lookup=lambda s: s == "customapp") is True
    )
    assert (
        is_registered_url_protocol("unregistered", registry_lookup=lambda s: s == "customapp")
        is False
    )

    # Dangerous schemes are unconditionally rejected even if a registry lookup returns True
    for dangerous in (
        "file",
        "javascript",
        "vbscript",
        "jscript",
        "ms-msdt",
        "search-ms",
        "shell",
        "cmd",
        "powershell",
        "hta",
        "wscript",
        "data",
        "blob",
        "ms-officecmd",
    ):
        assert is_registered_url_protocol(dangerous, registry_lookup=lambda _s: True) is False

"""Unit and integration tests for unified screen and window vision (inspect_screen)."""

from __future__ import annotations

import base64
import ctypes
import io
import json
import os
import sys
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from PIL import Image
from websockets.asyncio.client import connect as ws_connect

from clea.core import auth
from clea.core.agent import _BASE_SYSTEM_PROMPT, AgentLoop
from clea.core.daemon import CleaDaemon
from clea.core.protocol import (
    ChatImageAttachment,
    ChatSendParams,
    ChatSendRequest,
    OsInspectScreenParams,
    OsInspectScreenRequest,
    ResponseFrame,
    SessionNewRequest,
    parse_frame,
)
from clea.core.sessions import Session, SessionManager
from clea.memory.store import MemoryStore
from clea.providers.base import LLMProvider, Message, StreamChunk, ToolCall
from clea.providers.gemini import _to_gemini_steps
from clea.surfaces.voice.backends.base import (
    ToolCallEvent,
    VoiceBackend,
    VoiceSessionConfig,
    VoiceToolResponse,
    default_voice_tools,
)
from clea.surfaces.voice.config import ScreenBlocklistConfig, VoiceConfig
from clea.surfaces.voice.memory_hooks import BASE_VOICE_SYSTEM_INSTRUCTION
from clea.surfaces.voice.runner import VoiceRunner
from clea.tools.clipboard import ClipboardService, ClipboardSnapshot
from clea.tools.registry import RiskLevel, ToolDefinition, ToolRegistry
from clea.tools.vision import (
    MAX_VISION_DIMENSION,
    PW_RENDERFULLCONTENT,
    SRCCOPY_CAPTUREBLT,
    ArtifactStore,
    ScreenVisionService,
    Win32ScreenVisionBackend,
    WindowCapture,
    WindowInfo,
    _downscale_png,
    _enable_dpi_awareness,
    get_default_screen_vision_service,
    inspect_screen,
    register_vision_tools,
    set_default_screen_vision_service,
)


def _make_dummy_png(
    width: int = 100, height: int = 80, color: tuple[int, int, int] = (255, 0, 0)
) -> bytes:
    """Create lossless dummy PNG bytes."""
    img = Image.new("RGB", (width, height), color)
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


class FakeClipboardBackend:
    """In-memory mock clipboard backend."""

    def __init__(self, snapshot: ClipboardSnapshot | None = None, seq: int = 1) -> None:
        self.snapshot = snapshot or ClipboardSnapshot(kind="empty")
        self.seq = seq

    def get_sequence_number(self) -> int:
        return self.seq

    def read_snapshot(self) -> ClipboardSnapshot:
        return self.snapshot

    def write_text(self, text: str) -> None:
        self.snapshot = ClipboardSnapshot(kind="text", text=text)
        self.seq += 1


class FakeScreenVisionBackend:
    """Deterministic mock screen vision backend for unit tests."""

    def __init__(self) -> None:
        self.foreground_window: WindowInfo | None = WindowInfo(
            hwnd=1001,
            title="Antigravity Workspace",
            process_name="antigravity.exe",
            app_name="Google Antigravity",
            bounds=(0, 0, 1920, 1080),
        )
        self.windows: dict[str, WindowInfo] = {
            "vscode": WindowInfo(
                hwnd=2002,
                title="clea - Visual Studio Code",
                process_name="Code.exe",
                app_name="Visual Studio Code",
                bounds=(100, 100, 1500, 900),
            ),
            "terminal": WindowInfo(
                hwnd=3003,
                title="Administrator: Windows PowerShell",
                process_name="WindowsTerminal.exe",
                app_name="Windows Terminal",
                bounds=(50, 50, 1000, 600),
            ),
            "minimized_app": WindowInfo(
                hwnd=4004,
                title="Slack | General",
                process_name="slack.exe",
                app_name="Slack",
                bounds=(0, 0, 800, 600),
            ),
        }
        self.minimized_hwnds: set[int] = {4004}
        self.uia_texts: dict[int, str] = {
            1001: "File Edit View Terminal Help\nWorkspace Ready",
            2002: "src/clea/tools/vision.py - Line 1: import ctypes",
            3003: "PS C:\\Users\\clea> pytest tests/test_screen_vision.py\n29 passed",
            4004: "Slack notifications",
        }
        self.captures: dict[int, WindowCapture] = {
            1001: WindowCapture(
                png_bytes=_make_dummy_png(800, 600, (10, 20, 30)),
                width=800,
                height=600,
                window_title="Antigravity Workspace",
                process_name="antigravity.exe",
            ),
            2002: WindowCapture(
                png_bytes=_make_dummy_png(1400, 800, (40, 50, 60)),
                width=1400,
                height=800,
                window_title="clea - Visual Studio Code",
                process_name="Code.exe",
            ),
            3003: WindowCapture(
                png_bytes=_make_dummy_png(950, 550, (70, 80, 90)),
                width=950,
                height=550,
                window_title="Administrator: Windows PowerShell",
                process_name="WindowsTerminal.exe",
            ),
        }
        self.capture_call_count = 0

    def get_foreground_window(self) -> WindowInfo | None:
        return self.foreground_window

    def find_window(self, query: str) -> WindowInfo | None:
        q = (query or "").lower().strip()
        for k, v in self.windows.items():
            if q in k or q in v.title.lower() or q in v.process_name.lower():
                return v
        return None

    def is_minimized(self, hwnd: int) -> bool:
        return hwnd in self.minimized_hwnds

    def probe_uia_text(self, hwnd: int, max_chars: int = 3000) -> str:
        return self.uia_texts.get(hwnd, "")[:max_chars]

    def capture_window_png(self, hwnd: int) -> WindowCapture:
        self.capture_call_count += 1
        if hwnd in self.captures:
            return self.captures[hwnd]
        return WindowCapture(
            png_bytes=_make_dummy_png(640, 480),
            width=640,
            height=480,
            window_title="Generic Window",
            process_name="app.exe",
        )


# ===================================================================== TESTS


def test_artifact_store_save_and_prune(tmp_path: Path) -> None:
    store = ArtifactStore(data_dir=tmp_path)
    assert store.artifacts_dir.exists()

    png1 = _make_dummy_png(100, 100)
    png2 = _make_dummy_png(200, 200)

    f1 = store.save_png(png1, prefix="test_fresh")
    f2 = store.save_png(png2, prefix="test_old")

    assert f1.exists()
    assert f2.exists()

    # Manually backdate f2 to 25 hours ago
    past_mtime = time.time() - (25 * 3600)
    os.utime(f2, (past_mtime, past_mtime))

    pruned_count = store.prune_expired(max_age_seconds=86400)
    assert pruned_count == 1
    assert not f2.exists()
    assert f1.exists()


def test_downscale_png_capping() -> None:
    # 2000 x 1000 image exceeds 1568 max dimension
    raw = _make_dummy_png(2000, 1000)
    capped_bytes, (w, h) = _downscale_png(raw, max_side=MAX_VISION_DIMENSION)
    assert max(w, h) == MAX_VISION_DIMENSION
    assert w == 1568
    assert h == 784

    # 800 x 600 image remains untouched
    small_raw = _make_dummy_png(800, 600)
    small_bytes, (sw, sh) = _downscale_png(small_raw, max_side=MAX_VISION_DIMENSION)
    assert (sw, sh) == (800, 600)
    assert small_bytes == small_raw


def test_auto_routing_to_clipboard_when_recent_image(tmp_path: Path) -> None:
    clip_backend = FakeClipboardBackend(
        snapshot=ClipboardSnapshot(
            kind="image",
            image_bytes=_make_dummy_png(400, 300),
            image_format="PNG",
            image_dimensions=(400, 300),
        ),
        seq=10,
    )
    clip_service = ClipboardService(backend=clip_backend)
    clip_service.poll_sequence()

    vision_backend = FakeScreenVisionBackend()
    store = ArtifactStore(data_dir=tmp_path)
    service = ScreenVisionService(
        backend=vision_backend,
        clipboard_service=clip_service,
        artifact_store=store,
    )

    # Within 10 seconds of clipboard change with an image -> routes to clipboard
    res = service.inspect(target="auto")
    assert res.status == "ok"
    assert res.source == "clipboard"
    assert res.dimensions == (400, 300)
    assert res.artifact_path is not None
    assert Path(res.artifact_path).exists()
    assert res.attachment is not None
    assert res.attachment["mime_type"] == "image/png"
    assert vision_backend.capture_call_count == 0


def test_auto_routing_falls_back_to_active_when_clipboard_is_text(tmp_path: Path) -> None:
    clip_backend = FakeClipboardBackend(
        snapshot=ClipboardSnapshot(kind="text", text="Hello world"),
        seq=11,
    )
    clip_service = ClipboardService(backend=clip_backend)
    clip_service.poll_sequence()

    vision_backend = FakeScreenVisionBackend()
    store = ArtifactStore(data_dir=tmp_path)
    service = ScreenVisionService(
        backend=vision_backend,
        clipboard_service=clip_service,
        artifact_store=store,
    )

    # Clipboard is text -> auto-routing routes to active window
    res = service.inspect(target="auto")
    assert res.status == "ok"
    assert res.source == "active_window"
    assert res.window_title == "Antigravity Workspace"
    assert res.dimensions == (800, 600)
    assert vision_backend.capture_call_count == 1


def test_auto_routing_falls_back_to_active_when_clipboard_older_than_10s(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    clip_backend = FakeClipboardBackend(
        snapshot=ClipboardSnapshot(
            kind="image",
            image_bytes=_make_dummy_png(300, 200),
        ),
        seq=12,
    )
    clip_service = ClipboardService(backend=clip_backend)
    clip_service.poll_sequence()

    # Simulate 15 seconds elapsed since clipboard sequence changed
    clip_service._last_changed_monotonic = time.monotonic() - 15.0

    vision_backend = FakeScreenVisionBackend()
    store = ArtifactStore(data_dir=tmp_path)
    service = ScreenVisionService(
        backend=vision_backend,
        clipboard_service=clip_service,
        artifact_store=store,
    )

    res = service.inspect(target="auto")
    assert res.status == "ok"
    assert res.source == "active_window"
    assert res.window_title == "Antigravity Workspace"


def test_explicit_clipboard_mode_with_text(tmp_path: Path) -> None:
    clip_backend = FakeClipboardBackend(
        snapshot=ClipboardSnapshot(kind="text", text="Deterministic transform text"),
        seq=1,
    )
    clip_service = ClipboardService(backend=clip_backend)
    vision_backend = FakeScreenVisionBackend()
    store = ArtifactStore(data_dir=tmp_path)
    service = ScreenVisionService(
        backend=vision_backend,
        clipboard_service=clip_service,
        artifact_store=store,
    )

    res = service.inspect(target="clipboard")
    assert res.status == "ok"
    assert res.source == "clipboard_text"
    assert res.text == "Deterministic transform text"
    assert res.artifact_path is None
    assert res.attachment is None


def test_explicit_clipboard_mode_empty(tmp_path: Path) -> None:
    clip_backend = FakeClipboardBackend(snapshot=ClipboardSnapshot(kind="empty"))
    clip_service = ClipboardService(backend=clip_backend)
    vision_backend = FakeScreenVisionBackend()
    store = ArtifactStore(data_dir=tmp_path)
    service = ScreenVisionService(
        backend=vision_backend,
        clipboard_service=clip_service,
        artifact_store=store,
    )

    res = service.inspect(target="clipboard")
    assert res.status == "empty"
    assert "Clipboard is empty" in (res.message or "")


def test_background_window_resolution_and_capture(tmp_path: Path) -> None:
    vision_backend = FakeScreenVisionBackend()
    clip_service = ClipboardService(backend=FakeClipboardBackend())
    store = ArtifactStore(data_dir=tmp_path)
    service = ScreenVisionService(
        backend=vision_backend,
        clipboard_service=clip_service,
        artifact_store=store,
    )

    # Query for "vscode" matches Code.exe in background
    res = service.inspect(target="vscode")
    assert res.status == "ok"
    assert res.source == "background_window"
    assert res.window_title == "clea - Visual Studio Code"
    assert res.process_name == "Code.exe"
    assert res.dimensions == (1400, 800)
    assert res.uia_text == "src/clea/tools/vision.py - Line 1: import ctypes"
    assert res.artifact_path is not None
    assert Path(res.artifact_path).exists()
    assert res.attachment is not None


def test_minimized_window_handling(tmp_path: Path) -> None:
    vision_backend = FakeScreenVisionBackend()
    clip_service = ClipboardService(backend=FakeClipboardBackend())
    store = ArtifactStore(data_dir=tmp_path)
    service = ScreenVisionService(
        backend=vision_backend,
        clipboard_service=clip_service,
        artifact_store=store,
    )

    res = service.inspect(target="slack")
    assert res.status == "minimized"
    assert res.source == "background_window"
    assert res.window_title == "Slack | General"
    assert res.process_name == "slack.exe"
    assert "is minimized" in (res.message or "")
    assert res.artifact_path is None
    assert res.attachment is None


def test_window_not_found(tmp_path: Path) -> None:
    vision_backend = FakeScreenVisionBackend()
    clip_service = ClipboardService(backend=FakeClipboardBackend())
    store = ArtifactStore(data_dir=tmp_path)
    service = ScreenVisionService(
        backend=vision_backend,
        clipboard_service=clip_service,
        artifact_store=store,
    )

    res = service.inspect(target="nonexistent_process_xyz")
    assert res.status == "not_found"
    assert "No window matching" in (res.message or "")


def test_include_image_false_probes_text_only(tmp_path: Path) -> None:
    vision_backend = FakeScreenVisionBackend()
    clip_service = ClipboardService(backend=FakeClipboardBackend())
    store = ArtifactStore(data_dir=tmp_path)
    service = ScreenVisionService(
        backend=vision_backend,
        clipboard_service=clip_service,
        artifact_store=store,
    )

    res = service.inspect(target="terminal", include_image=False)
    assert res.status == "ok"
    assert res.source == "background_window"
    assert res.window_title == "Administrator: Windows PowerShell"
    assert "29 passed" in (res.uia_text or "")
    assert res.artifact_path is None
    assert res.attachment is None
    assert vision_backend.capture_call_count == 0


def test_tool_registry_registration_and_execution(tmp_path: Path) -> None:
    registry = ToolRegistry()
    vision_backend = FakeScreenVisionBackend()
    clip_service = ClipboardService(backend=FakeClipboardBackend())
    store = ArtifactStore(data_dir=tmp_path)
    service = ScreenVisionService(
        backend=vision_backend,
        clipboard_service=clip_service,
        artifact_store=store,
    )

    register_vision_tools(registry, service)
    tool_def = registry.get("inspect_screen")
    assert tool_def.name == "inspect_screen"
    assert tool_def.risk_level == RiskLevel.READ_ONLY

    # Run tool execution via inspect_screen helper
    out = inspect_screen(target="vscode", service=service)
    assert isinstance(out, dict)
    assert out["status"] == "ok"
    assert out["source"] == "background_window"
    assert out["window_title"] == "clea - Visual Studio Code"
    assert "attachment" in out
    assert out["attachment"]["mime_type"] == "image/png"


@pytest.mark.asyncio
async def test_agent_loop_multimodal_turn_delivery_without_memory_bloat(tmp_path: Path) -> None:
    """Verify tool delivery attaches image to turn but strips raw bytes from persisted history."""
    registry = ToolRegistry()
    vision_backend = FakeScreenVisionBackend()
    clip_service = ClipboardService(backend=FakeClipboardBackend())
    store = ArtifactStore(data_dir=tmp_path)
    service = ScreenVisionService(
        backend=vision_backend,
        clipboard_service=clip_service,
        artifact_store=store,
    )
    register_vision_tools(registry, service)

    memory = MemoryStore(base_dir=tmp_path / "memory")
    session = Session(id="test_vision_sess")

    captured_steps: list[dict[str, Any]] = []

    class MockVisionLLMProvider(LLMProvider):
        provider_name = "mock_gemini"

        async def stream_turn(
            self,
            *,
            messages: list[Message],
            tools: list[ToolDefinition],
            system_prompt: str,
            response_format=None,
        ):
            # Record steps as converted by _to_gemini_steps
            steps = _to_gemini_steps(messages)
            captured_steps.append(steps)

            if len(messages) == 1 and messages[0].role == "user":
                # First model response: request inspect_screen tool call
                yield StreamChunk(
                    type="tool_call",
                    tool_call=ToolCall(
                        id="call_inspect_1",
                        name="inspect_screen",
                        arguments={"target": "vscode"},
                    ),
                )
            else:
                # Second model response: answer after seeing the tool result
                yield StreamChunk(
                    type="text_delta", text="I see your code editor open to vision.py."
                )
                yield StreamChunk(type="done")

    provider = MockVisionLLMProvider()
    agent = AgentLoop(provider=provider, registry=registry, memory=memory)

    reply = await agent.run_turn(session=session, user_text="What is on my screen?")
    assert "I see your code editor" in reply

    # Check the tool message in session.messages
    tool_msgs = [m for m in session.messages if m.role == "tool"]
    assert len(tool_msgs) == 1
    t_msg = tool_msgs[0]

    # Verify content is JSON string without base64 blob
    parsed_content = json.loads(t_msg.content)
    assert parsed_content["status"] == "ok"
    assert parsed_content["source"] == "background_window"
    assert "attachment" not in parsed_content
    assert "data:image" not in t_msg.content
    assert "base64" not in t_msg.content

    # Verify that during the active turn, _to_gemini_steps received the image attachment
    assert len(captured_steps) >= 2
    second_hop_steps = captured_steps[1]
    func_result_step = next(s for s in second_hop_steps if s.get("type") == "function_result")
    assert isinstance(func_result_step["result"], list)
    assert func_result_step["result"][0]["type"] == "text"
    assert func_result_step["result"][1]["type"] == "image"
    assert func_result_step["result"][1]["mime_type"] == "image/png"
    assert len(func_result_step["result"][1]["data"]) > 100

    # Verify that after the turn completes, image_bytes are cleared from in-memory messages
    for msg in session.messages:
        assert getattr(msg, "image_bytes", None) is None


def test_win32_backend_find_window_priority() -> None:
    """Test Win32ScreenVisionBackend window filtering and prioritization with mock user32."""
    mock_user32 = MagicMock()
    mock_gdi32 = MagicMock()
    mock_kernel32 = MagicMock()

    # Setup 2 windows: hwnd 10 (minimized) and hwnd 20 (non-minimized)
    mock_user32.IsWindowVisible.return_value = 1
    mock_user32.GetWindowLongW.return_value = 0  # not tool window

    def fake_get_window_rect(hwnd, rect_ptr):
        r = getattr(rect_ptr, "_obj", getattr(rect_ptr, "contents", None))
        if r is not None:
            r.left = 100
            r.top = 100
            r.right = 1000
            r.bottom = 800
        return 1

    mock_user32.GetWindowRect.side_effect = fake_get_window_rect
    mock_user32.IsIconic.side_effect = lambda h: 1 if h == 10 else 0

    hwnds = [10, 20]

    def fake_enum_windows(cb, lparam):
        for h in hwnds:
            cb(h, lparam)
        return 1

    mock_user32.EnumWindows.side_effect = fake_enum_windows

    backend = Win32ScreenVisionBackend(
        user32=mock_user32,
        gdi32=mock_gdi32,
        kernel32=mock_kernel32,
    )

    # Mock window titles and process names
    def fake_title(h, api=None):
        return "Visual Studio Code" if h == 20 else "VS Code - Minimized"

    def fake_proc(h, api=None):
        return "Code.exe"

    import clea.tools.vision as vision_mod
    original_title = vision_mod.get_window_title
    original_proc = vision_mod.get_window_process_name
    try:
        vision_mod.get_window_title = fake_title
        vision_mod.get_window_process_name = fake_proc

        # Query for "code" should prefer the non-minimized window (hwnd 20)
        found = backend.find_window("code")
        assert found is not None
        assert found.hwnd == 20
        assert found.title == "Visual Studio Code"
    finally:
        vision_mod.get_window_title = original_title
        vision_mod.get_window_process_name = original_proc


def test_global_service_singleton() -> None:
    svc = ScreenVisionService()
    set_default_screen_vision_service(svc)
    assert get_default_screen_vision_service() is svc
    set_default_screen_vision_service(None)
    new_svc = get_default_screen_vision_service()
    assert new_svc is not None
    assert new_svc is not svc


@pytest.mark.asyncio
async def test_daemon_initializes_vision_service_and_prunes_housekeeping(tmp_path: Path) -> None:
    from clea.core.daemon import CleaDaemon
    from clea.core.sessions import SessionManager

    memory = MemoryStore(base_dir=tmp_path / "memory")
    provider = MagicMock()
    provider.provider_name = "mock"
    provider.model_name = "mock-model"
    registry = ToolRegistry()
    agent = AgentLoop(provider=provider, registry=registry, memory=memory)

    clip_service = ClipboardService(backend=FakeClipboardBackend())
    vision_backend = FakeScreenVisionBackend()
    artifact_store = ArtifactStore(data_dir=tmp_path)
    vision_service = ScreenVisionService(
        backend=vision_backend,
        clipboard_service=clip_service,
        artifact_store=artifact_store,
    )

    daemon = CleaDaemon(
        agent=agent,
        sessions=SessionManager(),
        clipboard_service=clip_service,
        screen_vision_service=vision_service,
        token="test_token",
        token_file=tmp_path / "token.txt",
        lock_file=tmp_path / "daemon.lock",
    )

    assert daemon.screen_vision_service is vision_service
    assert registry.get("inspect_screen") is not None

    # Test housekeeping tick prunes expired artifacts
    old_file = artifact_store.save_png(_make_dummy_png(50, 50), prefix="test_expire")
    past_time = time.time() - 90000
    os.utime(old_file, (past_time, past_time))
    assert old_file.exists()

    daemon.housekeeping_tick()
    assert not old_file.exists()


def test_auto_routing_whitespace_target_routes_to_auto(tmp_path: Path) -> None:
    clip_backend = FakeClipboardBackend(
        snapshot=ClipboardSnapshot(
            kind="image",
            image_bytes=_make_dummy_png(320, 240),
        ),
        seq=50,
    )
    clip_service = ClipboardService(backend=clip_backend)
    clip_service.poll_sequence()

    vision_backend = FakeScreenVisionBackend()
    store = ArtifactStore(data_dir=tmp_path)
    service = ScreenVisionService(
        backend=vision_backend,
        clipboard_service=clip_service,
        artifact_store=store,
    )

    # Whitespace target should route to auto -> clipboard (since recent image)
    res = service.inspect(target="   ")
    assert res.status == "ok"
    assert res.source == "clipboard"
    assert res.dimensions == (320, 240)


def test_downscale_png_cmyk_conversion() -> None:
    # PIL fails with KeyError: 'CMYK' or cannot write mode CMYK as PNG if not converted
    im = Image.new("CMYK", (2000, 1000), (0, 100, 100, 0))
    buf = io.BytesIO()
    im.save(buf, format="JPEG")
    jpeg_cmyk_bytes = buf.getvalue()

    capped_bytes, (w, h) = _downscale_png(jpeg_cmyk_bytes, max_side=MAX_VISION_DIMENSION)
    assert max(w, h) == MAX_VISION_DIMENSION
    assert w == 1568
    assert h == 784

    # Verify output is valid PNG
    with Image.open(io.BytesIO(capped_bytes)) as out_img:
        assert out_img.format == "PNG"
        assert out_img.mode in ("RGB", "RGBA")


def test_win32_backend_capture_failure_raises() -> None:
    mock_user32 = MagicMock()
    mock_gdi32 = MagicMock()

    # GetWindowRect succeeds with 1000x800
    def fake_get_rect(hwnd, ptr):
        r = getattr(ptr, "_obj", getattr(ptr, "contents", None))
        if r is not None:
            r.left = 0
            r.top = 0
            r.right = 1000
            r.bottom = 800
        return 1

    mock_user32.GetWindowRect.side_effect = fake_get_rect
    mock_user32.GetDC.return_value = 100
    mock_gdi32.CreateCompatibleDC.return_value = 200
    mock_gdi32.CreateCompatibleBitmap.return_value = 300
    mock_gdi32.SelectObject.return_value = 400

    # PrintWindow fails (returns 0)
    mock_user32.PrintWindow.return_value = 0
    # Window is not foreground
    mock_user32.GetForegroundWindow.return_value = 9999

    backend = Win32ScreenVisionBackend(user32=mock_user32, gdi32=mock_gdi32)
    with pytest.raises(RuntimeError, match="PrintWindow failed to capture pixels"):
        backend.capture_window_png(1234)


def test_service_inspect_capture_error(tmp_path: Path) -> None:
    vision_backend = FakeScreenVisionBackend()
    # Force capture_window_png to raise
    vision_backend.capture_window_png = MagicMock(side_effect=RuntimeError("Device context lost"))
    clip_service = ClipboardService(backend=FakeClipboardBackend())
    store = ArtifactStore(data_dir=tmp_path)
    service = ScreenVisionService(
        backend=vision_backend,
        clipboard_service=clip_service,
        artifact_store=store,
    )

    res = service.inspect(target="vscode")
    assert res.status == "error"
    assert res.source == "background_window"
    assert "Device context lost" in (res.message or "")


def test_win32_backend_find_window_skips_zero_size() -> None:
    mock_user32 = MagicMock()
    mock_user32.IsWindowVisible.return_value = 1
    mock_user32.GetWindowLongW.return_value = 0  # not tool window

    # Window 10 is 0x0 at origin (0,0,0,0) -> should be skipped
    # Window 20 is normal (100, 100, 800, 600) -> should match
    def fake_rect(hwnd, ptr):
        r = getattr(ptr, "_obj", getattr(ptr, "contents", None))
        if r is not None:
            if hwnd == 10:
                r.left = 0
                r.top = 0
                r.right = 0
                r.bottom = 0
            else:
                r.left = 100
                r.top = 100
                r.right = 800
                r.bottom = 600
        return 1

    mock_user32.GetWindowRect.side_effect = fake_rect
    mock_user32.IsIconic.return_value = 0

    def fake_enum(cb, lparam):
        cb(10, lparam)
        cb(20, lparam)
        return 1

    mock_user32.EnumWindows.side_effect = fake_enum
    backend = Win32ScreenVisionBackend(user32=mock_user32)

    import clea.tools.vision as vision_mod
    original_title = vision_mod.get_window_title
    original_proc = vision_mod.get_window_process_name
    try:
        vision_mod.get_window_title = lambda h, api=None: "My Code Editor"
        vision_mod.get_window_process_name = lambda h, api=None: "Code.exe"

        found = backend.find_window("code")
        assert found is not None
        assert found.hwnd == 20  # Window 10 was skipped because dimensions are 0x0
    finally:
        vision_mod.get_window_title = original_title
        vision_mod.get_window_process_name = original_proc


def test_daemon_preserves_custom_service_artifact_store(tmp_path: Path) -> None:
    from clea.core.daemon import CleaDaemon
    from clea.core.sessions import SessionManager

    custom_artifacts_dir = tmp_path / "custom_artifacts_dir"
    custom_store = ArtifactStore(data_dir=custom_artifacts_dir)
    vision_backend = FakeScreenVisionBackend()
    clip_service = ClipboardService(backend=FakeClipboardBackend())
    custom_service = ScreenVisionService(
        backend=vision_backend,
        clipboard_service=clip_service,
        artifact_store=custom_store,
    )

    memory = MemoryStore(base_dir=tmp_path / "memory")
    provider = MagicMock()
    provider.provider_name = "mock"
    provider.model_name = "mock-model"
    agent = AgentLoop(provider=provider, registry=ToolRegistry(), memory=memory)

    daemon = CleaDaemon(
        agent=agent,
        sessions=SessionManager(),
        clipboard_service=clip_service,
        screen_vision_service=custom_service,
        token="token_xyz",
        token_file=tmp_path / "token.txt",
        lock_file=tmp_path / "daemon.lock",
    )

    # daemon.artifact_store should be custom_store, preserving custom_artifacts_dir
    assert daemon.artifact_store is custom_store
    assert daemon.artifact_store.artifacts_dir == custom_artifacts_dir / "artifacts"


def test_screen_inspection_result_preserves_question(tmp_path: Path) -> None:
    vision_backend = FakeScreenVisionBackend()
    clip_service = ClipboardService(backend=FakeClipboardBackend())
    store = ArtifactStore(data_dir=tmp_path)
    service = ScreenVisionService(
        backend=vision_backend,
        clipboard_service=clip_service,
        artifact_store=store,
    )

    q = "What is the status code?"
    res = service.inspect(target="terminal", question=q, include_image=False)
    assert res.status == "ok"
    assert res.question == q
    assert "What is the status code?" in (res.message or "")

    d = res.to_dict()
    assert d["question"] == q


def test_win32_backend_dwm_crop_dpi_scale_guard() -> None:
    """Verify DWM bounds larger than window rect (DPI scale disparity) are safely ignored."""
    mock_user32 = MagicMock()
    mock_gdi32 = MagicMock()
    mock_dwmapi = MagicMock()

    # Logical window rect: 1000 x 800
    def fake_get_rect(hwnd, ptr):
        r = getattr(ptr, "_obj", getattr(ptr, "contents", None))
        if r is not None:
            r.left = 100
            r.top = 100
            r.right = 1100
            r.bottom = 900
        return 1

    mock_user32.GetWindowRect.side_effect = fake_get_rect
    mock_user32.GetDC.return_value = 100
    mock_gdi32.CreateCompatibleDC.return_value = 200
    mock_gdi32.CreateCompatibleBitmap.return_value = 300
    mock_gdi32.SelectObject.return_value = 400
    mock_user32.PrintWindow.return_value = 1

    # DWM returns physical pixels on 175% monitor (1750 x 1400), exceeding logical 1000 x 800
    def fake_dwm(hwnd, attr, ptr, size):
        r = getattr(ptr, "_obj", getattr(ptr, "contents", None))
        if r is not None:
            r.left = 175
            r.top = 175
            r.right = 1925
            r.bottom = 1575
        return 0

    mock_dwmapi.DwmGetWindowAttribute.side_effect = fake_dwm

    # GetDIBits returns 800 lines
    def fake_get_dibits(dc, bmp, start, lines, buf, bmi, usage):
        return lines

    mock_gdi32.GetDIBits.side_effect = fake_get_dibits

    backend = Win32ScreenVisionBackend(
        user32=mock_user32,
        gdi32=mock_gdi32,
        dwmapi=mock_dwmapi,
    )

    cap = backend.capture_window_png(5555)
    assert cap is not None
    # Because 1000x800 <= 1568, dimensions should remain 1000 x 800 without bad crop
    assert cap.width == 1000
    assert cap.height == 800


def test_win32_backend_probe_uia_text_enum_children() -> None:
    """Verify child window enumeration fallback reads titles and WM_GETTEXT controls."""
    mock_user32 = MagicMock()

    def fake_enum_child(hwnd, cb, lparam):
        cb(101, 0)
        cb(102, 0)
        return 1

    mock_user32.EnumChildWindows.side_effect = fake_enum_child

    # Child 101 has title via GetWindowTextW
    # Child 102 has empty title, but SendMessageTimeoutW populates WM_GETTEXT
    def fake_msg_timeout(hwnd, msg, wparam, lparam, flags, timeout, res_ptr):
        if hwnd == 102 and msg == 0x000D:  # WM_GETTEXT
            lparam.value = "Active prompt line: npm test"
            return 1
        return 0

    mock_user32.SendMessageTimeoutW.side_effect = fake_msg_timeout

    backend = Win32ScreenVisionBackend(user32=mock_user32)

    import clea.tools.vision as vision_mod
    original_title = vision_mod.get_window_title
    try:
        vision_mod.get_window_title = (
            lambda h, api=None: "Terminal Tab 1" if h == 101 else ""
        )
        probed = backend.probe_uia_text(100)
        assert "Terminal Tab 1" in probed
        assert "Active prompt line: npm test" in probed
    finally:
        vision_mod.get_window_title = original_title


@pytest.mark.skipif(sys.platform != "win32", reason="Windows-only Win32 DLL signature check")
def test_win32_64bit_c_void_p_signatures_configured() -> None:
    """Verify real Win32 DLL handles use explicit 64-bit c_void_p argtypes and restype."""
    backend = Win32ScreenVisionBackend()
    u32 = backend._user32
    g32 = backend._gdi32
    dwm = backend._dwmapi
    k32 = backend._kernel32

    assert isinstance(u32, ctypes.WinDLL)
    assert isinstance(g32, ctypes.WinDLL)
    assert isinstance(k32, ctypes.WinDLL)

    assert u32.GetForegroundWindow.restype is ctypes.c_void_p
    assert u32.IsWindowVisible.argtypes == [ctypes.c_void_p]
    assert u32.IsIconic.argtypes == [ctypes.c_void_p]
    assert u32.GetWindowLongW.argtypes[0] is ctypes.c_void_p
    assert u32.GetWindowRect.argtypes[0] is ctypes.c_void_p
    assert u32.GetDC.argtypes == [ctypes.c_void_p]
    assert u32.GetDC.restype is ctypes.c_void_p
    assert u32.GetWindowDC.argtypes == [ctypes.c_void_p]
    assert u32.GetWindowDC.restype is ctypes.c_void_p
    assert u32.ReleaseDC.argtypes == [ctypes.c_void_p, ctypes.c_void_p]
    assert u32.PrintWindow.argtypes[:2] == [ctypes.c_void_p, ctypes.c_void_p]
    if hasattr(u32, "SetProcessDpiAwarenessContext"):
        assert u32.SetProcessDpiAwarenessContext.argtypes == [ctypes.c_void_p]

    assert g32.CreateCompatibleDC.argtypes == [ctypes.c_void_p]
    assert g32.CreateCompatibleDC.restype is ctypes.c_void_p
    assert g32.CreateCompatibleBitmap.argtypes[0] is ctypes.c_void_p
    assert g32.CreateCompatibleBitmap.restype is ctypes.c_void_p
    assert g32.SelectObject.argtypes == [ctypes.c_void_p, ctypes.c_void_p]
    assert g32.SelectObject.restype is ctypes.c_void_p
    assert g32.DeleteObject.argtypes == [ctypes.c_void_p]
    assert g32.DeleteDC.argtypes == [ctypes.c_void_p]
    assert g32.BitBlt.argtypes[0] is ctypes.c_void_p
    assert g32.BitBlt.argtypes[5] is ctypes.c_void_p
    assert g32.GetDIBits.argtypes[:2] == [ctypes.c_void_p, ctypes.c_void_p]
    assert g32.GetDIBits.argtypes[4] is ctypes.c_void_p
    assert g32.GetDIBits.argtypes[5] is ctypes.c_void_p

    if dwm is not None and hasattr(dwm, "DwmGetWindowAttribute"):
        assert dwm.DwmGetWindowAttribute.argtypes[0] is ctypes.c_void_p
        assert dwm.DwmGetWindowAttribute.argtypes[2] is ctypes.c_void_p

    assert k32.OpenProcess.restype is ctypes.c_void_p
    assert k32.CloseHandle.argtypes == [ctypes.c_void_p]
    if hasattr(k32, "QueryFullProcessImageNameW"):
        assert k32.QueryFullProcessImageNameW.argtypes[0] is ctypes.c_void_p


def test_enable_dpi_awareness_fallback_and_already_set() -> None:
    """Verify Per-Monitor V2 DPI setup with shcore fallback and E_ACCESSDENIED tolerance."""
    u32_ok = MagicMock()
    u32_ok.SetProcessDpiAwarenessContext.return_value = 1
    assert _enable_dpi_awareness(user32=u32_ok, shcore=MagicMock()) is True
    assert u32_ok.SetProcessDpiAwarenessContext.called

    # Fallback to shcore when context returns 0 and shcore returns E_ACCESSDENIED (already set)
    u32_fail = MagicMock()
    u32_fail.SetProcessDpiAwarenessContext.return_value = 0
    shcore_already = MagicMock()
    shcore_already.SetProcessDpiAwareness.return_value = -2147024891
    assert _enable_dpi_awareness(user32=u32_fail, shcore=shcore_already) is True
    shcore_already.SetProcessDpiAwareness.assert_called_once_with(2)


def test_service_inspect_non_interactive_desktop(tmp_path: Path) -> None:
    """Verify non-Default sandbox desktop returns status='non_interactive_desktop' cleanly."""
    vision_backend = FakeScreenVisionBackend()
    clip_backend = FakeClipboardBackend(
        snapshot=ClipboardSnapshot(
            kind="image",
            image_bytes=_make_dummy_png(200, 120, (5, 10, 15)),
            image_format="PNG",
            image_dimensions=(200, 120),
        ),
        seq=7,
    )
    clip_service = ClipboardService(backend=clip_backend)
    store = ArtifactStore(data_dir=tmp_path)
    service = ScreenVisionService(
        backend=vision_backend,
        clipboard_service=clip_service,
        artifact_store=store,
        desktop_name_fn=lambda: "exebox-sandbox-1",
    )

    active_res = service.inspect(target="active")
    assert active_res.status == "non_interactive_desktop"
    assert active_res.source == "active_window"
    assert "exebox-sandbox-1" in (active_res.message or "")
    assert "WinSta0\\Default" in (active_res.message or "")
    assert vision_backend.capture_call_count == 0

    bg_res = service.inspect(target="vscode")
    assert bg_res.status == "non_interactive_desktop"
    assert bg_res.source == "background_window"
    assert vision_backend.capture_call_count == 0

    # Explicit clipboard target still works
    clip_res = service.inspect(target="clipboard")
    assert clip_res.status == "ok"
    assert clip_res.source == "clipboard"


def test_win32_capture_foreground_bitblt_fallback_and_bgrx_alpha_promotion() -> None:
    """Verify foreground BitBlt(SRCCOPY|CAPTUREBLT) fallback on zero PrintWindow & BGRX->RGBA."""
    mock_user32 = MagicMock()
    mock_gdi32 = MagicMock()
    mock_dwmapi = MagicMock()

    width, height = 20, 10
    hwnd = 0x1_0000_2222  # >32-bit handle value to verify 64-bit safe comparisons

    def fake_get_rect(h, ptr):
        r = getattr(ptr, "_obj", getattr(ptr, "contents", None))
        r.left, r.top, r.right, r.bottom = 100, 200, 100 + width, 200 + height
        return 1

    mock_user32.GetWindowRect.side_effect = fake_get_rect
    mock_user32.GetDC.return_value = 0x1_0000_0001
    mock_user32.GetWindowDC.return_value = 0x1_0000_0002
    mock_user32.GetForegroundWindow.return_value = hwnd
    mock_gdi32.CreateCompatibleDC.return_value = 0x1_0000_0003
    mock_gdi32.CreateCompatibleBitmap.return_value = 0x1_0000_0004
    mock_gdi32.SelectObject.return_value = 0x1_0000_0005
    mock_dwmapi.DwmGetWindowAttribute.return_value = 1  # no DWM crop

    # PrintWindow claims success, but produces all-zero buffer on first GetDIBits call
    mock_user32.PrintWindow.return_value = 1
    mock_gdi32.BitBlt.return_value = 1

    dib_calls = {"count": 0}
    # BGRX pixel (B=30, G=120, R=210, X=0) promotes to opaque RGBA (210, 120, 30, 255)
    bgrx_payload = bytes([30, 120, 210, 0]) * (width * height)

    def fake_get_dibits(dc, bmp, start, lines, buf, bmi, usage):
        dib_calls["count"] += 1
        if dib_calls["count"] == 1:
            # All zeros from PrintWindow
            buf.raw = b"\x00" * (width * height * 4)
        else:
            # Non-zero BGRX frame after GetWindowDC + BitBlt fallback
            buf.raw = bgrx_payload
        return lines

    mock_gdi32.GetDIBits.side_effect = fake_get_dibits

    backend = Win32ScreenVisionBackend(
        user32=mock_user32,
        gdi32=mock_gdi32,
        dwmapi=mock_dwmapi,
    )

    cap = backend.capture_window_png(hwnd)
    assert cap.width == width
    assert cap.height == height
    mock_user32.PrintWindow.assert_called_once_with(hwnd, 0x1_0000_0003, PW_RENDERFULLCONTENT)
    mock_user32.GetWindowDC.assert_called_once_with(hwnd)
    mock_gdi32.BitBlt.assert_called_once_with(
        0x1_0000_0003,
        0,
        0,
        width,
        height,
        0x1_0000_0002,
        0,
        0,
        SRCCOPY_CAPTUREBLT,
    )
    mock_user32.ReleaseDC.assert_any_call(hwnd, 0x1_0000_0002)

    with Image.open(io.BytesIO(cap.png_bytes)) as img:
        assert img.format == "PNG"
        assert img.mode == "RGBA"
        assert img.getpixel((0, 0)) == (210, 120, 30, 255)


def test_win32_capture_dwm_clamped_crop_and_downscale() -> None:
    """Verify DWM extended frame bounds crop drop shadows, clamp offsets, and cap at 1568px."""
    mock_user32 = MagicMock()
    mock_gdi32 = MagicMock()
    mock_dwmapi = MagicMock()

    # Window rect: (100, 100, 2100, 1100) -> 2000 x 1000
    # DWM rect: (98, 100, 2093, 1093) -> negative left offset (-2), right at 1993, bottom 993
    # Clamped crop should be (0, 0, 1993, 993) -> 1993 x 993 -> downscaled to max_side=1568
    def fake_get_rect(hwnd, ptr):
        r = getattr(ptr, "_obj", getattr(ptr, "contents", None))
        r.left, r.top, r.right, r.bottom = 100, 100, 2100, 1100
        return 1

    def fake_dwm(hwnd, attr, ptr, size):
        r = getattr(ptr, "_obj", getattr(ptr, "contents", None))
        r.left, r.top, r.right, r.bottom = 98, 100, 2093, 1093
        return 0

    mock_user32.GetWindowRect.side_effect = fake_get_rect
    mock_user32.GetDC.return_value = 100
    mock_user32.GetForegroundWindow.return_value = 9999
    mock_gdi32.CreateCompatibleDC.return_value = 200
    mock_gdi32.CreateCompatibleBitmap.return_value = 300
    mock_gdi32.SelectObject.return_value = 400
    mock_user32.PrintWindow.return_value = 1
    mock_dwmapi.DwmGetWindowAttribute.side_effect = fake_dwm

    def fake_get_dibits(dc, bmp, start, lines, buf, bmi, usage):
        # Set first pixel non-zero BGRA with alpha=255
        buf[0:4] = b"\x10\x20\x30\xff"
        return lines

    mock_gdi32.GetDIBits.side_effect = fake_get_dibits

    backend = Win32ScreenVisionBackend(
        user32=mock_user32,
        gdi32=mock_gdi32,
        dwmapi=mock_dwmapi,
    )

    cap = backend.capture_window_png(777)
    assert max(cap.width, cap.height) == MAX_VISION_DIMENSION
    assert cap.width == 1568
    expected_h = round(993 * (1568 / 1993))
    assert cap.height == expected_h


@pytest.mark.asyncio
async def test_chat_send_image_attachment_end_to_end_and_purges_memory(tmp_path: Path) -> None:
    """Verify Ctrl+V chat.send image reaches CleaDaemon -> AgentLoop -> Gemini and purges bytes."""
    captured_steps: list[list[dict[str, Any]]] = []

    class CapturingProvider(LLMProvider):
        provider_name = "mock_gemini"
        model_name = "mock-vision"

        async def stream_turn(
            self,
            *,
            messages: list[Message],
            tools: list[ToolDefinition],
            system_prompt: str,
            response_format=None,
        ):
            captured_steps.append(_to_gemini_steps(messages))
            yield StreamChunk(type="text_delta", text="I see a bright green rectangle.")
            yield StreamChunk(type="done")

    memory = MemoryStore(base_dir=tmp_path / "memory")
    registry = ToolRegistry()
    agent = AgentLoop(provider=CapturingProvider(), registry=registry, memory=memory)
    sessions = SessionManager()
    clip_service = ClipboardService(backend=FakeClipboardBackend())
    artifact_store = ArtifactStore(data_dir=tmp_path)
    vision_service = ScreenVisionService(
        backend=FakeScreenVisionBackend(),
        clipboard_service=clip_service,
        artifact_store=artifact_store,
    )

    daemon = CleaDaemon(
        agent=agent,
        sessions=sessions,
        clipboard_service=clip_service,
        screen_vision_service=vision_service,
        host="127.0.0.1",
        port=0,
        token="vision_e2e_token",
        token_file=tmp_path / "token.txt",
        lock_file=tmp_path / "daemon.lock",
    )

    await daemon.start()
    try:
        ws_url = f"ws://127.0.0.1:{daemon.bound_port}"
        async with ws_connect(
            ws_url,
            subprotocols=auth.client_subprotocols("vision_e2e_token"),  # type: ignore[arg-type]
            max_size=24 * 1024 * 1024,
        ) as ws:
            # 1. Create session
            new_req = SessionNewRequest()
            await ws.send(new_req.model_dump_json())
            raw = await ws.recv()
            init_frame = parse_frame(raw)
            assert isinstance(init_frame, ResponseFrame) and init_frame.ok
            sid = init_frame.session_id
            assert sid is not None

            # 2. Send oversized PNG image attachment (2000x1000) with text
            raw_png = _make_dummy_png(2000, 1000, (0, 220, 40))
            att = ChatImageAttachment(
                data_base64=base64.b64encode(raw_png).decode("ascii"),
                mime_type="image/png",
                name="pasted_shot.png",
                width=2000,
                height=1000,
            )
            send_req = ChatSendRequest(
                session_id=sid,
                params=ChatSendParams(text="What is in this pasted image?", image=att),
            )
            await ws.send(send_req.model_dump_json())

            final_resp: ResponseFrame | None = None
            while True:
                frame = parse_frame(await ws.recv())
                if isinstance(frame, ResponseFrame) and frame.id == send_req.id:
                    final_resp = frame
                    break

            assert final_resp is not None and final_resp.ok
            assert "bright green rectangle" in str(final_resp.result.get("text", ""))

            # Verify Gemini step received the downscaled PNG image part
            assert len(captured_steps) == 1
            user_step = captured_steps[0][0]
            assert user_step["type"] == "user_input"
            parts = user_step["content"]
            img_parts = [p for p in parts if p.get("type") == "image"]
            assert len(img_parts) == 1
            assert img_parts[0]["mime_type"] == "image/png"
            delivered_png = base64.b64decode(img_parts[0]["data"])
            with Image.open(io.BytesIO(delivered_png)) as delivered_im:
                assert delivered_im.size == (1568, 784)

            # Verify artifact was saved on disk
            paste_artifacts = list(artifact_store.artifacts_dir.glob("chat_paste_*.png"))
            assert len(paste_artifacts) == 1

            # Verify in-memory session purged raw image bytes after turn
            sess = sessions.get(sid)
            assert sess is not None
            for msg in sess.messages:
                assert msg.image_bytes is None

            # 3. Invalid base64 attachment is rejected cleanly without crashing
            bad_req = ChatSendRequest(
                session_id=sid,
                params=ChatSendParams(
                    text="broken",
                    image=ChatImageAttachment(data_base64="!!!not-base64!!!"),
                ),
            )
            await ws.send(bad_req.model_dump_json())
            bad_frame = parse_frame(await ws.recv())
            assert isinstance(bad_frame, ResponseFrame)
            assert not bad_frame.ok
            assert "Invalid base64" in (bad_frame.error or "")

            # 4. Empty text with valid image falls back to inspect prompt automatically
            img_only_png = _make_dummy_png(300, 200, (10, 40, 200))
            img_only_req = ChatSendRequest(
                session_id=sid,
                params=ChatSendParams(
                    text="",
                    image=ChatImageAttachment(
                        data_base64=base64.b64encode(img_only_png).decode("ascii"),
                        name="clip_only.png",
                    ),
                ),
            )
            await ws.send(img_only_req.model_dump_json())
            while True:
                frame = parse_frame(await ws.recv())
                if isinstance(frame, ResponseFrame) and frame.id == img_only_req.id:
                    assert frame.ok
                    break
            assert len(captured_steps) == 2
            last_user_step = [s for s in captured_steps[1] if s["type"] == "user_input"][-1]
            assert "Please inspect this image." in last_user_step["content"][0]["text"]
            assert any(p.get("type") == "image" for p in last_user_step["content"])
    finally:
        await daemon.stop()


class _RecordingVoiceBackend(VoiceBackend):
    """Minimal live voice backend double recording sent tool responses and frames."""

    backend_name = "recording_mock"

    def __init__(self) -> None:
        self.sent_tool_responses: list[VoiceToolResponse] = []
        self.sent_video_frames: list[tuple[bytes, str]] = []

    async def connect(self, config: VoiceSessionConfig) -> None:
        return None

    async def close(self) -> None:
        return None

    async def send_audio(self, pcm_bytes: bytes) -> None:
        return None

    async def send_video_frame(self, image_bytes: bytes, *, mime_type: str = "image/jpeg") -> None:
        self.sent_video_frames.append((image_bytes, mime_type))

    async def send_text(self, text: str, *, end_of_turn: bool = True) -> None:
        return None

    async def send_tool_responses(self, responses: list[VoiceToolResponse]) -> None:
        self.sent_tool_responses.extend(responses)

    async def events(self):
        if False:
            yield None


@pytest.mark.asyncio
async def test_voice_runner_inspect_screen_rpc_and_live_multimodal_delivery(tmp_path: Path) -> None:
    """Verify os.inspect_screen RPC + VoiceRunner atomic multimodal PNG delivery across targets."""
    # Verify tool declaration and prompt routing rules for both Chat & Voice
    voice_tools = {t.name: t for t in default_voice_tools()}
    assert "inspect_screen" in voice_tools
    assert "inspect_screen" in BASE_VOICE_SYSTEM_INSTRUCTION
    assert "what's on my clipboard image" in BASE_VOICE_SYSTEM_INSTRUCTION
    assert "inspect_screen" in _BASE_SYSTEM_PROMPT
    assert "what's on my clipboard image" in _BASE_SYSTEM_PROMPT

    # Spin up real CleaDaemon with deterministic clipboard image + fake screen vision backend
    clip_png = _make_dummy_png(480, 320, (250, 180, 20))
    clip_backend = FakeClipboardBackend(
        snapshot=ClipboardSnapshot(
            kind="image",
            image_bytes=clip_png,
            image_format="PNG",
            image_dimensions=(480, 320),
        ),
        seq=42,
    )
    clip_service = ClipboardService(backend=clip_backend)
    clip_service.poll_sequence()

    vision_backend = FakeScreenVisionBackend()
    vision_backend.windows["1password"] = WindowInfo(
        hwnd=5005,
        title="1Password - Personal Vault",
        process_name="1Password.exe",
        app_name="1Password",
        bounds=(0, 0, 900, 600),
    )
    artifact_store = ArtifactStore(data_dir=tmp_path)
    vision_service = ScreenVisionService(
        backend=vision_backend,
        clipboard_service=clip_service,
        artifact_store=artifact_store,
    )

    memory = MemoryStore(base_dir=tmp_path / "memory")
    provider = MagicMock()
    provider.provider_name = "mock"
    provider.model_name = "mock"
    agent = AgentLoop(provider=provider, registry=ToolRegistry(), memory=memory)
    daemon = CleaDaemon(
        agent=agent,
        sessions=SessionManager(),
        clipboard_service=clip_service,
        screen_vision_service=vision_service,
        host="127.0.0.1",
        port=0,
        token="voice_vision_token",
        token_file=tmp_path / "token.txt",
        lock_file=tmp_path / "daemon.lock",
    )

    await daemon.start()
    try:
        ws_url = f"ws://127.0.0.1:{daemon.bound_port}"

        # 1. Verify raw typed daemon RPC os.inspect_screen directly over WebSocket
        async with ws_connect(
            ws_url,
            subprotocols=auth.client_subprotocols("voice_vision_token"),  # type: ignore[arg-type]
            max_size=24 * 1024 * 1024,
        ) as ws:
            rpc_req = OsInspectScreenRequest(
                params=OsInspectScreenParams(
                    target="vscode",
                    question="What file is open?",
                    include_image=True,
                )
            )
            await ws.send(rpc_req.model_dump_json())
            raw_frame = parse_frame(await ws.recv())
            assert isinstance(raw_frame, ResponseFrame) and raw_frame.ok
            assert raw_frame.result["status"] == "ok"
            assert raw_frame.result["source"] == "background_window"
            assert raw_frame.result["window_title"] == "clea - Visual Studio Code"
            assert raw_frame.result["image_mime_type"] == "image/png"
            assert isinstance(raw_frame.result["image_base64"], str)
            assert len(base64.b64decode(raw_frame.result["image_base64"])) > 100

        # 2. Verify VoiceRunner._handle_tool_call delivers clipboard + background PNG frames
        voice_backend = _RecordingVoiceBackend()
        runner = VoiceRunner(
            config=VoiceConfig(
                blocklist=ScreenBlocklistConfig(
                    window_titles=["1Password"],
                    processes=["1Password.exe"],
                )
            ),
            data_dir=tmp_path,
            daemon_url=ws_url,
            token="voice_vision_token",
            backend_factory=lambda: voice_backend,
            enable_hotkey=False,
        )

        # 2a. Clipboard image via target="clipboard"
        await runner._handle_tool_call(
            ToolCallEvent(
                call_id="call-clip-1",
                name="inspect_screen",
                arguments={"target": "clipboard", "question": "What's on my clipboard image?"},
            ),
            voice_backend,
        )
        assert len(voice_backend.sent_tool_responses) == 1
        clip_resp = voice_backend.sent_tool_responses[-1]
        assert clip_resp.call_id == "call-clip-1"
        assert clip_resp.is_error is False
        assert clip_resp.response["status"] == "ok"
        assert clip_resp.response["source"] == "clipboard"
        assert clip_resp.response["frames_sent"] == 1
        assert "image_base64" not in clip_resp.response
        assert len(clip_resp.media_parts) == 1
        assert clip_resp.media_parts[0].mime_type == "image/png"
        with Image.open(io.BytesIO(clip_resp.media_parts[0].data)) as im:
            assert im.size == (480, 320)

        # 2b. Background window via target="vscode"
        await runner._handle_tool_call(
            ToolCallEvent(
                call_id="call-bg-1",
                name="inspect_screen",
                arguments={"target": "vscode", "question": "Look at VS Code"},
            ),
            voice_backend,
        )
        bg_resp = voice_backend.sent_tool_responses[-1]
        assert bg_resp.call_id == "call-bg-1"
        assert bg_resp.is_error is False
        assert bg_resp.response["status"] == "ok"
        assert bg_resp.response["window_title"] == "clea - Visual Studio Code"
        assert bg_resp.response["frames_sent"] == 1
        assert "image_base64" not in bg_resp.response
        assert len(bg_resp.media_parts) == 1
        with Image.open(io.BytesIO(bg_resp.media_parts[0].data)) as im:
            assert im.size == (1400, 800)

        # 2c. Privacy-blocklisted window (1Password) is blocked without attaching media
        await runner._handle_tool_call(
            ToolCallEvent(
                call_id="call-blocked-1",
                name="inspect_screen",
                arguments={"target": "1password"},
            ),
            voice_backend,
        )
        blocked_resp = voice_backend.sent_tool_responses[-1]
        assert blocked_resp.is_error is True
        assert blocked_resp.response["status"] == "blocked"
        assert blocked_resp.media_parts == []

        # 2d. Artifact path fallback when daemon omits image_base64
        saved_disk_png = artifact_store.save_png(_make_dummy_png(120, 90, (90, 90, 90)), "disk")

        async def fake_rpc_disk_only(req, *, timeout=10.0):
            return {
                "ok": True,
                "status": "ok",
                "source": "active_window",
                "window_title": "Disk Fallback Window",
                "process_name": "app.exe",
                "artifact_path": str(saved_disk_png),
                "image_base64": None,
                "image_mime_type": "image/png",
            }

        runner._rpc = fake_rpc_disk_only  # type: ignore[method-assign]
        await runner._handle_tool_call(
            ToolCallEvent(
                call_id="call-disk-1",
                name="inspect_screen",
                arguments={"target": "active"},
            ),
            voice_backend,
        )
        disk_resp = voice_backend.sent_tool_responses[-1]
        assert disk_resp.is_error is False
        assert disk_resp.response["frames_sent"] == 1
        assert len(disk_resp.media_parts) == 1
        with Image.open(io.BytesIO(disk_resp.media_parts[0].data)) as im:
            assert im.size == (120, 90)

        # 2e. Non-interactive desktop returns structured status without dropping message
        async def fake_rpc_non_interactive(req, *, timeout=10.0):
            return {
                "ok": False,
                "status": "non_interactive_desktop",
                "source": "active_window",
                "message": "Isolated desktop exebox-test",
                "image_base64": None,
            }

        runner._rpc = fake_rpc_non_interactive  # type: ignore[method-assign]
        await runner._handle_tool_call(
            ToolCallEvent(
                call_id="call-ni-1",
                name="inspect_screen",
                arguments={"target": "active"},
            ),
            voice_backend,
        )
        ni_resp = voice_backend.sent_tool_responses[-1]
        assert ni_resp.is_error is False
        assert ni_resp.response["status"] == "non_interactive_desktop"
        assert ni_resp.response["frames_sent"] == 0
        assert ni_resp.media_parts == []

        # 2f. consent_mode="deny" denies screen capture target while still permitting clipboard
        deny_runner = VoiceRunner(
            config=VoiceConfig(consent_mode="deny"),
            data_dir=tmp_path,
            daemon_url=ws_url,
            token="voice_vision_token",
            backend_factory=lambda: voice_backend,
            enable_hotkey=False,
        )
        await deny_runner._handle_tool_call(
            ToolCallEvent(
                call_id="call-deny-win",
                name="inspect_screen",
                arguments={"target": "vscode"},
            ),
            voice_backend,
        )
        deny_resp = voice_backend.sent_tool_responses[-1]
        assert deny_resp.is_error is True
        assert deny_resp.response["status"] == "denied"
        assert deny_resp.media_parts == []

        await deny_runner._handle_tool_call(
            ToolCallEvent(
                call_id="call-allow-clip",
                name="inspect_screen",
                arguments={"target": "clipboard"},
            ),
            voice_backend,
        )
        allow_clip_resp = voice_backend.sent_tool_responses[-1]
        assert allow_clip_resp.is_error is False
        assert allow_clip_resp.response["status"] == "ok"
        assert len(allow_clip_resp.media_parts) == 1
    finally:
        await daemon.stop()


def test_win32_find_window_resolves_app_aliases_and_skips_untitled_helpers() -> None:
    """Verify 'vscode', 'VS Code', and 'wt' resolve real process names and skip untitled helpers."""
    mock_user32 = MagicMock()
    mock_user32.IsWindowVisible.return_value = 1
    mock_user32.GetWindowLongW.return_value = 0
    mock_user32.IsIconic.return_value = 0

    def fake_rect(hwnd, ptr):
        r = getattr(ptr, "_obj", getattr(ptr, "contents", None))
        if r is not None:
            r.left, r.top, r.right, r.bottom = 0, 0, 1280, 720
        return 1

    mock_user32.GetWindowRect.side_effect = fake_rect

    # Enumerate an untitled Code.exe helper window BEFORE the real VS Code window
    def fake_enum(cb, lparam):
        cb(10, lparam)  # untitled Code.exe helper
        cb(20, lparam)  # real VS Code window
        cb(30, lparam)  # real Windows Terminal window
        return 1

    mock_user32.EnumWindows.side_effect = fake_enum
    backend = Win32ScreenVisionBackend(user32=mock_user32)

    import clea.tools.vision as vision_mod

    orig_title = vision_mod.get_window_title
    orig_proc = vision_mod.get_window_process_name
    try:
        titles = {10: "", 20: "clea - Visual Studio Code", 30: "PowerShell 7"}
        procs = {10: "Code.exe", 20: "Code.exe", 30: "WindowsTerminal.exe"}
        vision_mod.get_window_title = lambda h, api=None: titles[h]
        vision_mod.get_window_process_name = lambda h, api=None: procs[h]

        vs_alias = backend.find_window("vscode")
        assert vs_alias is not None
        assert vs_alias.hwnd == 20
        assert vs_alias.title == "clea - Visual Studio Code"

        vs_spaced = backend.find_window("VS Code")
        assert vs_spaced is not None
        assert vs_spaced.hwnd == 20

        wt_alias = backend.find_window("wt")
        assert wt_alias is not None
        assert wt_alias.hwnd == 30
    finally:
        vision_mod.get_window_title = orig_title
        vision_mod.get_window_process_name = orig_proc


def test_win32_capture_deselects_bitmap_before_getdibits_and_promotes_mixed_dwm_gdi_alpha() -> None:
    """Verify MSDN GetDIBits deselection order and mixed DWM-shadow + GDI-zero-alpha promotion."""
    mock_user32 = MagicMock()
    mock_gdi32 = MagicMock()
    mock_dwmapi = MagicMock()

    full_w, full_h = 10, 10
    call_order: list[tuple[str, Any]] = []

    def fake_get_rect(hwnd, ptr):
        r = getattr(ptr, "_obj", getattr(ptr, "contents", None))
        r.left, r.top, r.right, r.bottom = 100, 100, 100 + full_w, 100 + full_h
        return 1

    def fake_dwm(hwnd, attr, ptr, size):
        r = getattr(ptr, "_obj", getattr(ptr, "contents", None))
        # Crop 2px drop shadow on all sides -> inner 6x6 frame
        r.left, r.top, r.right, r.bottom = 102, 102, 108, 108
        return 0

    mock_user32.GetWindowRect.side_effect = fake_get_rect
    mock_dwmapi.DwmGetWindowAttribute.side_effect = fake_dwm
    mock_user32.GetDC.return_value = 0x101
    mock_user32.GetForegroundWindow.return_value = 9999
    mock_gdi32.CreateCompatibleDC.return_value = 0x202
    mock_gdi32.CreateCompatibleBitmap.return_value = 0x303
    mock_user32.PrintWindow.return_value = 1

    def fake_select_object(dc, obj):
        call_order.append(("SelectObject", obj))
        return 0x404 if obj == 0x303 else 0x303

    mock_gdi32.SelectObject.side_effect = fake_select_object

    # Build a 10x10 BGRA frame:
    # - Outer 2px border: translucent DWM shadow (B=50, G=50, R=50, A=128)
    # - Inner row y=2: opaque DWM titlebar (B=40, G=30, R=20, A=255)
    # - Inner rows y=3..7: GDI-drawn white/colored client pixels with A=0 (B=20, G=180, R=240, A=0)
    raw = bytearray(full_w * full_h * 4)
    for y in range(full_h):
        for x in range(full_w):
            idx = (y * full_w + x) * 4
            if x < 2 or x >= 8 or y < 2 or y >= 8:
                raw[idx : idx + 4] = bytes([50, 50, 50, 128])
            elif y == 2:
                raw[idx : idx + 4] = bytes([40, 30, 20, 255])
            else:
                raw[idx : idx + 4] = bytes([20, 180, 240, 0])

    def fake_get_dibits(dc, bmp, start, lines, buf, bmi, usage):
        call_order.append(("GetDIBits", bmp))
        buf.raw = bytes(raw)
        return lines

    mock_gdi32.GetDIBits.side_effect = fake_get_dibits

    backend = Win32ScreenVisionBackend(
        user32=mock_user32,
        gdi32=mock_gdi32,
        dwmapi=mock_dwmapi,
    )
    cap = backend.capture_window_png(555)
    assert (cap.width, cap.height) == (6, 6)

    # Confirm h_bitmap (0x303) was deselected (SelectObject with old_bmp 0x404) BEFORE GetDIBits
    assert call_order == [
        ("SelectObject", 0x303),
        ("SelectObject", 0x404),
        ("GetDIBits", 0x303),
    ]

    with Image.open(io.BytesIO(cap.png_bytes)) as img:
        assert img.mode == "RGBA"
        # DWM titlebar row stays intact and opaque
        assert img.getpixel((0, 0)) == (20, 30, 40, 255)
        # GDI client interior promoted from A=0 to opaque RGBA (240, 180, 20, 255)
        assert img.getpixel((2, 2)) == (240, 180, 20, 255)


def test_service_inspect_clipboard_include_image_false_skips_artifact_and_attachment(
    tmp_path: Path,
) -> None:
    clip_backend = FakeClipboardBackend(
        snapshot=ClipboardSnapshot(
            kind="image",
            image_bytes=_make_dummy_png(320, 240, (12, 34, 56)),
        ),
        seq=99,
    )
    clip_service = ClipboardService(backend=clip_backend)
    store = ArtifactStore(data_dir=tmp_path)
    service = ScreenVisionService(
        backend=FakeScreenVisionBackend(),
        clipboard_service=clip_service,
        artifact_store=store,
    )

    res = service.inspect(target="clipboard", include_image=False)
    assert res.status == "ok"
    assert res.source == "clipboard"
    assert res.dimensions == (320, 240)
    assert res.artifact_path is None
    assert res.attachment is None
    assert list(store.artifacts_dir.glob("*.png")) == []


@pytest.mark.asyncio
async def test_voice_runner_inspect_screen_blocklist_cleanup_quoted_clipboard_and_size_cap(
    tmp_path: Path,
) -> None:
    """Verify pre-RPC & post-RPC blocklist artifact cleanup, quoted clipboard, and Live size cap."""
    clip_png = _make_dummy_png(240, 160, (100, 150, 200))
    clip_backend = FakeClipboardBackend(
        snapshot=ClipboardSnapshot(
            kind="image",
            image_bytes=clip_png,
            image_format="PNG",
            image_dimensions=(240, 160),
        ),
        seq=8,
    )
    clip_service = ClipboardService(backend=clip_backend)
    vision_backend = FakeScreenVisionBackend()
    # Set active foreground window to a blocklisted password manager
    vision_backend.foreground_window = WindowInfo(
        hwnd=5005,
        title="1Password - Secret Vault",
        process_name="1Password.exe",
        app_name="1Password",
        bounds=(0, 0, 800, 600),
    )
    artifact_store = ArtifactStore(data_dir=tmp_path)
    vision_service = ScreenVisionService(
        backend=vision_backend,
        clipboard_service=clip_service,
        artifact_store=artifact_store,
    )

    memory = MemoryStore(base_dir=tmp_path / "memory")
    provider = MagicMock()
    provider.provider_name = "mock"
    provider.model_name = "mock"
    daemon = CleaDaemon(
        agent=AgentLoop(provider=provider, registry=ToolRegistry(), memory=memory),
        sessions=SessionManager(),
        clipboard_service=clip_service,
        screen_vision_service=vision_service,
        host="127.0.0.1",
        port=0,
        token="voice_priv_token",
        token_file=tmp_path / "token.txt",
        lock_file=tmp_path / "daemon.lock",
    )

    await daemon.start()
    try:
        ws_url = f"ws://127.0.0.1:{daemon.bound_port}"
        voice_backend = _RecordingVoiceBackend()
        runner = VoiceRunner(
            config=VoiceConfig(
                blocklist=ScreenBlocklistConfig(
                    window_titles=["1Password"],
                    processes=["1Password.exe"],
                )
            ),
            data_dir=tmp_path,
            daemon_url=ws_url,
            token="voice_priv_token",
            backend_factory=lambda: voice_backend,
            enable_hotkey=False,
        )

        # 1. Pre-RPC blocklist hit for explicit target="1password" never even captures
        await runner._handle_tool_call(
            ToolCallEvent(
                call_id="call-pre-block",
                name="inspect_screen",
                arguments={"target": "1password"},
            ),
            voice_backend,
        )
        assert voice_backend.sent_tool_responses[-1].is_error is True
        assert voice_backend.sent_tool_responses[-1].response["status"] == "blocked"
        assert vision_backend.capture_call_count == 0
        assert list(artifact_store.artifacts_dir.glob("*.png")) == []

        # 2. Post-RPC blocklist hit for target="active" deletes the daemon-saved PNG artifact
        await runner._handle_tool_call(
            ToolCallEvent(
                call_id="call-post-block",
                name="inspect_screen",
                arguments={"target": "active"},
            ),
            voice_backend,
        )
        assert voice_backend.sent_tool_responses[-1].is_error is True
        assert voice_backend.sent_tool_responses[-1].response["status"] == "blocked"
        assert list(artifact_store.artifacts_dir.glob("*.png")) == []

        # 3. Quoted "'clipboard'" is allowed under consent_mode="deny"
        deny_runner = VoiceRunner(
            config=VoiceConfig(consent_mode="deny"),
            data_dir=tmp_path,
            daemon_url=ws_url,
            token="voice_priv_token",
            backend_factory=lambda: voice_backend,
            enable_hotkey=False,
        )
        await deny_runner._handle_tool_call(
            ToolCallEvent(
                call_id="call-quoted-clip",
                name="inspect_screen",
                arguments={"target": "'clipboard'"},
            ),
            voice_backend,
        )
        quoted_resp = voice_backend.sent_tool_responses[-1]
        assert quoted_resp.is_error is False
        assert quoted_resp.response["status"] == "ok"
        assert len(quoted_resp.media_parts) == 1

        # 4. Oversized >2.5 MiB noisy PNG is re-encoded via encode_native_frame JPEG fallback
        noisy = Image.frombytes("RGB", (1100, 1100), os.urandom(1100 * 1100 * 3))
        noisy_buf = io.BytesIO()
        noisy.save(noisy_buf, format="PNG")
        noisy_png = noisy_buf.getvalue()
        assert len(noisy_png) > 2_500_000

        async def fake_rpc_huge(req, *, timeout=10.0):
            return {
                "ok": True,
                "status": "ok",
                "source": "clipboard",
                "image_base64": base64.b64encode(noisy_png).decode("ascii"),
                "image_mime_type": "image/png",
            }

        runner._rpc = fake_rpc_huge  # type: ignore[method-assign]
        await runner._handle_tool_call(
            ToolCallEvent(
                call_id="call-huge-png",
                name="inspect_screen",
                arguments={"target": "clipboard"},
            ),
            voice_backend,
        )
        huge_resp = voice_backend.sent_tool_responses[-1]
        assert huge_resp.is_error is False
        assert len(huge_resp.media_parts) == 1
        assert huge_resp.media_parts[0].mime_type == "image/jpeg"
        assert len(huge_resp.media_parts[0].data) < len(noisy_png)
    finally:
        await daemon.stop()


@pytest.mark.asyncio
async def test_chat_send_whitespace_base64_and_purges_mime_type(tmp_path: Path) -> None:
    """Verify whitespace-wrapped base64 JPEG decodes cleanly and AgentLoop purges mime_type."""
    captured_steps: list[list[dict[str, Any]]] = []

    class CapturingProvider(LLMProvider):
        provider_name = "mock_gemini"
        model_name = "mock-vision"

        async def stream_turn(
            self,
            *,
            messages: list[Message],
            tools: list[ToolDefinition],
            system_prompt: str,
            response_format=None,
        ):
            captured_steps.append(_to_gemini_steps(messages))
            yield StreamChunk(type="text_delta", text="Decoded cleanly.")
            yield StreamChunk(type="done")

    memory = MemoryStore(base_dir=tmp_path / "memory")
    agent = AgentLoop(provider=CapturingProvider(), registry=ToolRegistry(), memory=memory)
    sessions = SessionManager()
    daemon = CleaDaemon(
        agent=agent,
        sessions=sessions,
        clipboard_service=ClipboardService(backend=FakeClipboardBackend()),
        screen_vision_service=ScreenVisionService(
            backend=FakeScreenVisionBackend(),
            clipboard_service=ClipboardService(backend=FakeClipboardBackend()),
            artifact_store=ArtifactStore(data_dir=tmp_path),
        ),
        host="127.0.0.1",
        port=0,
        token="ws_b64_token",
        token_file=tmp_path / "token.txt",
        lock_file=tmp_path / "daemon.lock",
    )

    await daemon.start()
    try:
        ws_url = f"ws://127.0.0.1:{daemon.bound_port}"
        async with ws_connect(
            ws_url,
            subprotocols=auth.client_subprotocols("ws_b64_token"),  # type: ignore[arg-type]
            max_size=24 * 1024 * 1024,
        ) as ws:
            new_req = SessionNewRequest()
            await ws.send(new_req.model_dump_json())
            init_frame = parse_frame(await ws.recv())
            assert isinstance(init_frame, ResponseFrame) and init_frame.ok
            sid = init_frame.session_id
            assert sid is not None

            im = Image.new("RGB", (120, 80), (20, 90, 210))
            jbuf = io.BytesIO()
            im.save(jbuf, format="JPEG")
            raw_b64 = base64.b64encode(jbuf.getvalue()).decode("ascii")
            wrapped_b64 = "\n".join(raw_b64[i : i + 64] for i in range(0, len(raw_b64), 64))

            send_req = ChatSendRequest(
                session_id=sid,
                params=ChatSendParams(
                    text="Check wrapped jpeg",
                    image=ChatImageAttachment(
                        data_base64=wrapped_b64,
                        mime_type="image/jpeg",
                        name="photo.jpg",
                    ),
                ),
            )
            await ws.send(send_req.model_dump_json())
            while True:
                frame = parse_frame(await ws.recv())
                if isinstance(frame, ResponseFrame) and frame.id == send_req.id:
                    assert frame.ok
                    break

            assert len(captured_steps) == 1
            sess = sessions.get(sid)
            assert sess is not None
            for msg in sess.messages:
                assert msg.image_bytes is None
                assert msg.image_mime_type is None
    finally:
        await daemon.stop()




"""Unit and integration tests for unified screen and window vision (inspect_screen)."""

from __future__ import annotations

import io
import json
import os
import time
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from PIL import Image

from clea.core.agent import AgentLoop
from clea.core.sessions import Session
from clea.memory.store import MemoryStore
from clea.providers.base import LLMProvider, Message, StreamChunk, ToolCall
from clea.providers.gemini import _to_gemini_steps
from clea.tools.clipboard import ClipboardService, ClipboardSnapshot
from clea.tools.registry import RiskLevel, ToolDefinition, ToolRegistry
from clea.tools.vision import (
    MAX_VISION_DIMENSION,
    ArtifactStore,
    ScreenVisionService,
    Win32ScreenVisionBackend,
    WindowCapture,
    WindowInfo,
    _downscale_png,
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


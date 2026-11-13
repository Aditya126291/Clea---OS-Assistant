"""Tests for Voice Slice 2 screen capture and vision tools."""

from __future__ import annotations

import asyncio
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
    is_window_blocked,
)
from clea.surfaces.voice.wake_word import WakeWordDetector
from clea.surfaces.voice.win32 import WindowInfo


class FakeLiveBackend(VoiceBackend):
    """Controllable in-memory Live backend for testing."""

    backend_name = "fake-live"

    def __init__(self) -> None:
        self.connected_config: VoiceSessionConfig | None = None
        self.closed = False
        self.sent_audio: list[bytes] = []
        self.sent_tool_responses: list[VoiceToolResponse] = []
        self.sent_video_frames: list[bytes] = []
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

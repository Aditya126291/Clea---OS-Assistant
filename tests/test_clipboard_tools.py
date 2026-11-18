"""Unit tests for lock-resilient ClipboardService, local transforms, and MCP tools."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock, patch

import pytest

from clea.core.agent import AgentLoop
from clea.core.daemon import CleaDaemon
from clea.memory.store import MemoryStore
from clea.providers.base import LLMProvider, Message, StreamChunk
from clea.tools import (
    ClipboardBackend,
    ClipboardLockedError,
    ClipboardService,
    ClipboardSnapshot,
    PermissionDeniedError,
    RiskLevel,
    ToolDefinition,
    ToolRegistry,
    Win32ClipboardBackend,
    open_path_or_url,
    read_clipboard,
    register_clipboard_tools,
    save_clipboard_to_file,
    transform_clipboard,
)

# --------------------------------------------------------------------------- In-Memory Fake Backend


class FakeClipboardBackend:
    """Pure in-memory ClipboardBackend preventing any OS clipboard interaction."""

    def __init__(self, initial_snapshot: ClipboardSnapshot | None = None) -> None:
        self.sequence_number = 1
        self._snapshot = initial_snapshot or ClipboardSnapshot(kind="empty")
        self.written_texts: list[str] = []

    def get_sequence_number(self) -> int:
        return self.sequence_number

    def read_snapshot(self) -> ClipboardSnapshot:
        return self._snapshot

    def write_text(self, text: str) -> None:
        self.written_texts.append(text)
        self.sequence_number += 1
        self._snapshot = ClipboardSnapshot(
            kind="text",
            text=text,
            char_count=len(text),
            line_count=len(text.splitlines()) if text else 0,
        )

    def set_text(self, text: str) -> None:
        self.sequence_number += 1
        self._snapshot = ClipboardSnapshot(
            kind="text",
            text=text,
            char_count=len(text),
            line_count=len(text.splitlines()) if text else 0,
        )

    def set_image(
        self,
        image_bytes: bytes,
        dimensions: tuple[int, int] = (120, 80),
        image_format: str = "PNG",
    ) -> None:
        self.sequence_number += 1
        self._snapshot = ClipboardSnapshot(
            kind="image",
            image_bytes=image_bytes,
            image_format=image_format,
            image_dimensions=dimensions,
        )

    def set_files(self, file_paths: list[str]) -> None:
        self.sequence_number += 1
        self._snapshot = ClipboardSnapshot(
            kind="files",
            file_paths=file_paths,
        )

    def set_empty(self) -> None:
        self.sequence_number += 1
        self._snapshot = ClipboardSnapshot(kind="empty")


class MockLLMProvider(LLMProvider):
    """Stub provider for daemon tests."""

    async def stream_turn(
        self,
        *,
        messages: list[Message],
        tools: list[ToolDefinition],
        system_prompt: str,
        response_format: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(type="text_delta", text="OK")
        yield StreamChunk(type="done")


# ----------------------------------------------------------- Protocol & Sequence Tests


def test_fake_backend_implements_protocol() -> None:
    backend = FakeClipboardBackend()
    assert isinstance(backend, ClipboardBackend)


def test_sequence_tracking_and_time_elapsed(tmp_path: Path) -> None:
    backend = FakeClipboardBackend()
    service = ClipboardService(backend=backend, default_save_dir=tmp_path)

    # Initial poll
    seq1 = service.poll_sequence()
    assert seq1 == 1
    elapsed1 = service.seconds_since_last_change()
    assert elapsed1 >= 0.0

    # Poll without changes
    seq2 = service.poll_sequence()
    assert seq2 == 1

    # Modify clipboard content
    backend.write_text("New content")
    seq3 = service.poll_sequence()
    assert seq3 == 2
    elapsed3 = service.seconds_since_last_change()
    assert elapsed3 <= 0.5


# ----------------------------------------------------- Lock Retry Behavior Tests


def test_open_clipboard_retry_success_on_attempt_3() -> None:
    """Verify OpenClipboard succeeds if contention clears on attempt 3."""
    mock_user32 = MagicMock()
    # Fails twice (0, 0), then succeeds (1)
    mock_user32.OpenClipboard.side_effect = [0, 0, 1]
    backend = Win32ClipboardBackend(user32=mock_user32, kernel32=MagicMock())

    with patch("time.sleep") as mock_sleep:
        with backend.open_clipboard():
            pass

    assert mock_user32.OpenClipboard.call_count == 3
    assert mock_sleep.call_count == 2
    mock_sleep.assert_called_with(0.025)
    assert mock_user32.CloseClipboard.call_count == 1


def test_open_clipboard_fails_after_3_attempts_raises_error() -> None:
    """Verify ClipboardLockedError is raised if all 3 attempts fail."""
    mock_user32 = MagicMock()
    mock_user32.OpenClipboard.side_effect = [0, 0, 0]
    backend = Win32ClipboardBackend(user32=mock_user32, kernel32=MagicMock())

    with patch("time.sleep") as mock_sleep:
        with pytest.raises(ClipboardLockedError) as exc_info:
            with backend.open_clipboard():
                pass

    assert "Windows clipboard is currently locked" in str(exc_info.value)
    assert mock_user32.OpenClipboard.call_count == 3
    assert mock_sleep.call_count == 2
    assert mock_user32.CloseClipboard.call_count == 0


def test_open_clipboard_always_closes_on_internal_exception() -> None:
    """Verify CloseClipboard is called in finally block even when block raises."""
    mock_user32 = MagicMock()
    mock_user32.OpenClipboard.return_value = 1
    backend = Win32ClipboardBackend(user32=mock_user32, kernel32=MagicMock())

    with pytest.raises(RuntimeError, match="Inner block error"):
        with backend.open_clipboard():
            raise RuntimeError("Inner block error")

    assert mock_user32.CloseClipboard.call_count == 1


# -------------------------------------------------- Transform: format_json Tests


def test_format_json_pretty(tmp_path: Path) -> None:
    backend = FakeClipboardBackend()
    backend.set_text('{"name":"Clea","version":1,"features":["voice","tools"]}')
    service = ClipboardService(backend=backend, default_save_dir=tmp_path)

    res = service.format_json(minify=False, update_clipboard=True)
    assert res["status"] == "ok"
    assert res["action"] == "format_json"
    assert res["updated_clipboard"] is True

    parsed_back = json.loads(backend.written_texts[-1])
    assert parsed_back["name"] == "Clea"
    assert "\n  " in backend.written_texts[-1]  # 2-space indentation


def test_format_json_minify(tmp_path: Path) -> None:
    backend = FakeClipboardBackend()
    backend.set_text('{\n  "status": "ready",\n  "count": 42\n}')
    service = ClipboardService(backend=backend, default_save_dir=tmp_path)

    res = service.format_json(minify=True, update_clipboard=True)
    assert res["status"] == "ok"
    assert res["action"] == "minify_json"
    assert backend.written_texts[-1] == '{"status":"ready","count":42}'


def test_format_json_no_update_clipboard(tmp_path: Path) -> None:
    backend = FakeClipboardBackend()
    backend.set_text('{"foo": "bar"}')
    service = ClipboardService(backend=backend, default_save_dir=tmp_path)

    res = service.format_json(minify=False, update_clipboard=False)
    assert res["status"] == "ok"
    assert res["updated_clipboard"] is False
    assert len(backend.written_texts) == 0


def test_format_json_invalid_json(tmp_path: Path) -> None:
    backend = FakeClipboardBackend()
    backend.set_text('{"unclosed": "string')
    service = ClipboardService(backend=backend, default_save_dir=tmp_path)

    res = service.format_json(minify=False)
    assert res["status"] == "error"
    assert "Invalid JSON" in res["error"]
    assert len(backend.written_texts) == 0


def test_format_json_non_text(tmp_path: Path) -> None:
    backend = FakeClipboardBackend()
    backend.set_empty()
    service = ClipboardService(backend=backend, default_save_dir=tmp_path)

    res = service.format_json()
    assert res["status"] == "error"
    assert "does not contain text" in res["error"]


# --------------------------------------------------- Transform: clean_text Tests


def test_clean_text_strips_ansi_normalizes_crlf_and_trailing_spaces(tmp_path: Path) -> None:
    backend = FakeClipboardBackend()
    input_text = (
        "\x1b[32mSuccess!\x1b[0m   \r\n"
        "Line with trailing whitespace   \r\n"
        "\r\n\r\n\r\n\r\n"  # 4 blank lines (5 newlines) -> should collapse to 2 blank lines
        "End line."
    )
    backend.set_text(input_text)
    service = ClipboardService(backend=backend, default_save_dir=tmp_path)

    res = service.clean_text(unwrap_lines=False, update_clipboard=True)
    assert res["status"] == "ok"
    assert res["action"] == "clean_text"

    cleaned = backend.written_texts[-1]
    assert "\x1b" not in cleaned
    assert "Success!" in cleaned
    assert "\r" not in cleaned
    assert "Line with trailing whitespace\n" in cleaned
    # Ensure collapsed to max 2 blank lines (3 newlines between paragraphs)
    assert "\n\n\nEnd line." in cleaned
    assert "\n\n\n\n" not in cleaned


def test_clean_text_unwraps_lines(tmp_path: Path) -> None:
    backend = FakeClipboardBackend()
    wrapped_text = (
        "The quick brown fox\n"
        "jumps over the lazy dog\n"
        "and rests quietly.\n\n"
        "Second paragraph has\n"
        "its own wrapped lines."
    )
    backend.set_text(wrapped_text)
    service = ClipboardService(backend=backend, default_save_dir=tmp_path)

    res = service.clean_text(unwrap_lines=True, update_clipboard=True)
    assert res["status"] == "ok"
    assert res["action"] == "unwrap_text"

    unwrapped = backend.written_texts[-1]
    expected = (
        "The quick brown fox jumps over the lazy dog and rests quietly.\n\n"
        "Second paragraph has its own wrapped lines."
    )
    assert unwrapped == expected


def test_clean_text_non_text(tmp_path: Path) -> None:
    backend = FakeClipboardBackend()
    backend.set_empty()
    service = ClipboardService(backend=backend, default_save_dir=tmp_path)

    res = service.clean_text()
    assert res["status"] == "error"
    assert "does not contain text" in res["error"]


# --------------------------------------------- Transform: strip_tracking_urls Tests


def test_strip_tracking_urls_youtube_and_search(tmp_path: Path) -> None:
    backend = FakeClipboardBackend()
    text = (
        "Check out this video: "
        "https://www.youtube.com/watch?v=dQw4w9WgXcQ&si=track123&utm_source=share\n"
        "Also search query: "
        "https://www.google.com/search?q=clea+assistant&fbclid=fb123&gclid=g456"
    )
    backend.set_text(text)
    service = ClipboardService(backend=backend, default_save_dir=tmp_path)

    res = service.strip_tracking_urls(update_clipboard=True)
    assert res["status"] == "ok"
    assert res["urls_cleaned_count"] == 2
    assert res["updated_clipboard"] is True

    cleaned = backend.written_texts[-1]
    assert "https://www.youtube.com/watch?v=dQw4w9WgXcQ" in cleaned
    assert "si=" not in cleaned
    assert "utm_source=" not in cleaned
    assert "https://www.google.com/search?q=clea+assistant" in cleaned
    assert "fbclid=" not in cleaned
    assert "gclid=" not in cleaned


def test_strip_tracking_urls_preserves_trailing_punctuation(tmp_path: Path) -> None:
    backend = FakeClipboardBackend()
    text = "Read the docs at https://example.com/guide?utm_medium=email. It is great!"
    backend.set_text(text)
    service = ClipboardService(backend=backend, default_save_dir=tmp_path)

    res = service.strip_tracking_urls(update_clipboard=True)
    assert res["status"] == "ok"
    assert res["urls_cleaned_count"] == 1

    cleaned = backend.written_texts[-1]
    assert cleaned == "Read the docs at https://example.com/guide. It is great!"


def test_strip_tracking_urls_unaffected_when_no_tracking(tmp_path: Path) -> None:
    backend = FakeClipboardBackend()
    text = "Visit https://news.ycombinator.com/item?id=12345 directly."
    backend.set_text(text)
    service = ClipboardService(backend=backend, default_save_dir=tmp_path)

    res = service.strip_tracking_urls(update_clipboard=True)
    assert res["status"] == "ok"
    assert res["urls_cleaned_count"] == 0
    assert res["updated_clipboard"] is False
    assert len(backend.written_texts) == 0


# -------------------------------------------------- Transform: save_to_file Tests


def test_save_to_file_json_auto_extension(tmp_path: Path) -> None:
    backend = FakeClipboardBackend()
    backend.set_text('{"project": "Clea", "status": "active"}')
    service = ClipboardService(backend=backend, default_save_dir=tmp_path)

    res = service.save_to_file(directory="desktop")
    assert res["status"] == "saved"
    assert res["kind"] == "text"
    saved_path = Path(res["saved_path"])
    assert saved_path.is_file()
    assert saved_path.suffix == ".json"
    assert "Clea" in saved_path.read_text(encoding="utf-8")


def test_save_to_file_python_auto_extension(tmp_path: Path) -> None:
    backend = FakeClipboardBackend()
    py_code = "def get_greeting(name: str) -> str:\n    return f'Hello {name}'\n"
    backend.set_text(py_code)
    service = ClipboardService(backend=backend, default_save_dir=tmp_path)

    res = service.save_to_file(directory="desktop")
    assert res["status"] == "saved"
    saved_path = Path(res["saved_path"])
    assert saved_path.is_file()
    assert saved_path.suffix == ".py"


def test_save_to_file_markdown_auto_extension(tmp_path: Path) -> None:
    backend = FakeClipboardBackend()
    md_content = "# Project Overview\n\n- Milestone 1\n- Milestone 2\n"
    backend.set_text(md_content)
    service = ClipboardService(backend=backend, default_save_dir=tmp_path)

    res = service.save_to_file(directory="desktop")
    assert res["status"] == "saved"
    saved_path = Path(res["saved_path"])
    assert saved_path.is_file()
    assert saved_path.suffix == ".md"


def test_save_to_file_plain_text_auto_extension(tmp_path: Path) -> None:
    backend = FakeClipboardBackend()
    backend.set_text("Just a regular message without code or formatting.")
    service = ClipboardService(backend=backend, default_save_dir=tmp_path)

    res = service.save_to_file(directory="desktop")
    assert res["status"] == "saved"
    saved_path = Path(res["saved_path"])
    assert saved_path.is_file()
    assert saved_path.suffix == ".txt"


def test_save_to_file_image_bytes(tmp_path: Path) -> None:
    backend = FakeClipboardBackend()
    fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
    backend.set_image(fake_png, dimensions=(100, 100))
    service = ClipboardService(backend=backend, default_save_dir=tmp_path)

    res = service.save_to_file(filename="screenshot", directory="desktop")
    assert res["status"] == "saved"
    assert res["kind"] == "image"
    saved_path = Path(res["saved_path"])
    assert saved_path.is_file()
    assert saved_path.suffix == ".png"
    assert saved_path.read_bytes() == fake_png


def test_save_to_file_collision_safe_naming(tmp_path: Path) -> None:
    backend = FakeClipboardBackend()
    backend.set_text("test collision content")
    service = ClipboardService(backend=backend, default_save_dir=tmp_path)

    res1 = service.save_to_file(filename="notes.txt", directory="desktop")
    res2 = service.save_to_file(filename="notes.txt", directory="desktop")
    res3 = service.save_to_file(filename="notes.txt", directory="desktop")

    path1 = Path(res1["saved_path"])
    path2 = Path(res2["saved_path"])
    path3 = Path(res3["saved_path"])

    assert path1.name == "notes.txt"
    assert path2.name == "notes-1.txt"
    assert path3.name == "notes-2.txt"
    assert path1.is_file() and path2.is_file() and path3.is_file()


def test_save_to_file_sanitizes_path_traversal(tmp_path: Path) -> None:
    backend = FakeClipboardBackend()
    backend.set_text("safe content")
    service = ClipboardService(backend=backend, default_save_dir=tmp_path)

    res = service.save_to_file(filename="../../escape/secrets.txt", directory="desktop")
    assert res["status"] == "saved"
    saved_path = Path(res["saved_path"])
    # File must be stored directly within target_dir
    assert saved_path.parent.resolve() == tmp_path.resolve()
    assert saved_path.name == "secrets.txt"


def test_save_to_file_empty_clipboard(tmp_path: Path) -> None:
    backend = FakeClipboardBackend()
    backend.set_empty()
    service = ClipboardService(backend=backend, default_save_dir=tmp_path)

    res = service.save_to_file()
    assert res["status"] == "error"
    assert "Clipboard is empty" in res["error"]


# --------------------------------------------- ToolRegistry & Execution Tests


async def test_tool_registry_registration_and_execution(tmp_path: Path) -> None:
    backend = FakeClipboardBackend()
    backend.set_text('{"tool": "clea_clipboard"}')
    service = ClipboardService(backend=backend, default_save_dir=tmp_path)

    registry = ToolRegistry()
    register_clipboard_tools(
        registry,
        service,
        read_risk=RiskLevel.READ_ONLY,
        transform_risk=RiskLevel.READ_ONLY,
        save_risk=RiskLevel.REVERSIBLE,
    )

    defs = {d.name: d for d in registry.list_definitions()}
    assert "read_clipboard" in defs
    assert "transform_clipboard" in defs
    assert "save_clipboard_to_file" in defs

    # 1. read_clipboard
    read_res = await registry.execute("read_clipboard", {"max_chars": 2000})
    assert read_res["status"] == "ok"
    assert read_res["kind"] == "text"
    assert "clea_clipboard" in read_res["preview"]

    # 2. transform_clipboard
    trans_res = await registry.execute(
        "transform_clipboard",
        {"action": "format_json", "update_clipboard": True},
    )
    assert trans_res["status"] == "ok"
    assert trans_res["action"] == "format_json"

    # 3. save_clipboard_to_file requires approval (REVERSIBLE)
    with pytest.raises(PermissionDeniedError):
        await registry.execute("save_clipboard_to_file", {"filename": "registry_test.json"})

    save_res = await registry.execute(
        "save_clipboard_to_file",
        {"filename": "registry_test.json"},
        confirm=lambda _d, _a: True,
    )
    assert save_res["status"] == "saved"
    assert Path(save_res["saved_path"]).is_file()


def test_read_clipboard_capped_truncation() -> None:
    backend = FakeClipboardBackend()
    long_text = "A" * 6000
    backend.set_text(long_text)
    service = ClipboardService(backend=backend)

    res = read_clipboard(max_chars=4000, clipboard_service=service)
    assert res["status"] == "ok"
    assert res["kind"] == "text"
    assert res["truncated"] is True
    assert len(res["preview"]) == 4000 + len("... [truncated]")


def test_read_clipboard_image_and_files() -> None:
    backend = FakeClipboardBackend()
    backend.set_image(b"fake_image_bytes", dimensions=(640, 480))
    service = ClipboardService(backend=backend)

    img_res = read_clipboard(clipboard_service=service)
    assert img_res["status"] == "ok"
    assert img_res["kind"] == "image"
    assert img_res["dimensions"] == (640, 480)
    assert img_res["width"] == 640
    assert img_res["height"] == 480

    backend.set_files(["C:\\test\\doc.pdf", "C:\\test\\photo.jpg"])
    files_res = read_clipboard(clipboard_service=service)
    assert files_res["status"] == "ok"
    assert files_res["kind"] == "files"
    assert files_res["file_count"] == 2
    assert files_res["has_image_files"] is True


def test_transform_clipboard_and_save_direct_calls(tmp_path: Path) -> None:
    backend = FakeClipboardBackend()
    backend.set_text("\x1b[31mError Line\x1b[0m   ")
    service = ClipboardService(backend=backend, default_save_dir=tmp_path)

    t_res = transform_clipboard("clean_text", update_clipboard=True, clipboard_service=service)
    assert t_res["status"] == "ok"
    assert backend.written_texts[-1] == "Error Line"

    s_res = save_clipboard_to_file(
        filename="error_log", directory="desktop", clipboard_service=service
    )
    assert s_res["status"] == "saved"
    assert Path(s_res["saved_path"]).is_file()


# --------------------------------------------- 64-Bit Win32 & DIB/DIBV5 Tests


def test_win32_ctypes_64bit_void_p_signatures() -> None:
    """Verify Win32ClipboardBackend configures c_void_p pointer signatures on Windows."""
    import ctypes
    import sys

    if sys.platform != "win32":
        pytest.skip("Win32 DLL signatures apply on Windows only")

    backend = Win32ClipboardBackend()
    assert backend._user32.GetClipboardData.restype is ctypes.c_void_p
    assert backend._user32.SetClipboardData.restype is ctypes.c_void_p
    assert backend._user32.SetClipboardData.argtypes[1] is ctypes.c_void_p
    assert backend._user32.OpenClipboard.argtypes == [ctypes.c_void_p]
    assert backend._kernel32.GlobalAlloc.restype is ctypes.c_void_p
    assert backend._kernel32.GlobalLock.argtypes == [ctypes.c_void_p]
    assert backend._kernel32.GlobalLock.restype is ctypes.c_void_p
    assert backend._kernel32.GlobalUnlock.argtypes == [ctypes.c_void_p]
    assert backend._kernel32.GlobalSize.argtypes == [ctypes.c_void_p]
    assert backend._kernel32.GlobalSize.restype is ctypes.c_size_t
    assert backend._gdi32.GetObjectW.argtypes == [
        ctypes.c_void_p,
        ctypes.c_int,
        ctypes.c_void_p,
    ]
    assert backend._gdi32.GetObjectW.restype is ctypes.c_int
    assert backend._gdi32.CreateCompatibleDC.argtypes == [ctypes.c_void_p]
    assert backend._gdi32.CreateCompatibleDC.restype is ctypes.c_void_p
    assert backend._gdi32.DeleteDC.argtypes == [ctypes.c_void_p]
    assert backend._gdi32.GetDIBits.argtypes[0] is ctypes.c_void_p
    assert backend._gdi32.GetDIBits.argtypes[1] is ctypes.c_void_p
    assert backend._gdi32.GetDIBits.argtypes[4] is ctypes.c_void_p
    assert backend._gdi32.GetDIBits.argtypes[5] is ctypes.c_void_p
    assert backend._gdi32.GetDIBits.restype is ctypes.c_int


def test_dib_and_dibv5_reconstruction_into_png() -> None:
    """Verify CF_DIB (biSize=40) and CF_DIBV5 (biSize=124, BI_BITFIELDS) decode to PNG."""
    import ctypes
    import io
    import struct

    from PIL import Image

    from clea.tools.clipboard import CF_DIB, CF_DIBV5, _convert_dib_to_png

    # 1. Construct a 24-bit RGB CF_DIB buffer (16x12)
    src_img = Image.new("RGB", (16, 12), (42, 128, 210))
    bmp_buf = io.BytesIO()
    src_img.save(bmp_buf, format="BMP")
    dib_bytes = bmp_buf.getvalue()[14:]

    png_bytes, dims = _convert_dib_to_png(dib_bytes)
    assert png_bytes is not None
    assert dims == (16, 12)
    with Image.open(io.BytesIO(png_bytes)) as decoded:
        assert decoded.format == "PNG"
        assert decoded.size == (16, 12)
        assert decoded.getpixel((0, 0)) == (42, 128, 210)

    # 2. Construct a 32-bit BI_BITFIELDS CF_DIBV5 buffer (8x6)
    w, h = 8, 6
    hdr = bytearray(124)
    struct.pack_into("<IiiHHIIiiII", hdr, 0, 124, w, h, 1, 32, 3, w * h * 4, 0, 0, 0, 0)
    struct.pack_into("<IIII", hdr, 40, 0x00FF0000, 0x0000FF00, 0x000000FF, 0xFF000000)
    pixels = bytes([210, 128, 42, 255] * (w * h))  # BGRA
    dibv5_bytes = bytes(hdr) + pixels

    png_v5, dims_v5 = _convert_dib_to_png(dibv5_bytes)
    assert png_v5 is not None
    assert dims_v5 == (8, 6)
    with Image.open(io.BytesIO(png_v5)) as decoded_v5:
        assert decoded_v5.format == "PNG"
        assert decoded_v5.size == (8, 6)
        assert decoded_v5.getpixel((0, 0)) == (42, 128, 210, 255)

    # 3. Verify Win32ClipboardBackend.read_snapshot() prioritizes CF_DIBV5 using 64-bit buffer
    buf = ctypes.create_string_buffer(dibv5_bytes, len(dibv5_bytes))
    addr = ctypes.addressof(buf)
    mock_user32 = MagicMock()
    mock_kernel32 = MagicMock()
    mock_user32.OpenClipboard.return_value = 1
    mock_user32.RegisterClipboardFormatW.return_value = 49152
    mock_user32.IsClipboardFormatAvailable.side_effect = lambda fmt: fmt == CF_DIBV5
    mock_user32.GetClipboardData.return_value = addr
    mock_kernel32.GlobalLock.return_value = addr
    mock_kernel32.GlobalSize.return_value = len(dibv5_bytes)

    backend = Win32ClipboardBackend(user32=mock_user32, kernel32=mock_kernel32)
    snap = backend.read_snapshot()
    assert snap.kind == "image"
    assert snap.image_format == "PNG"
    assert snap.image_dimensions == (8, 6)
    assert snap.image_bytes is not None
    assert mock_user32.IsClipboardFormatAvailable.call_args_list[-1][0][0] in (CF_DIBV5, CF_DIB)


# --------------------------------------------- Desktop Path & Direct Execution Tests


def test_save_to_file_resolves_strictly_to_path_home_desktop(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify directory='desktop' resolves strictly to Path.home() / 'Desktop', never OneDrive."""
    fake_home = tmp_path / "user_home"
    onedrive_desktop = fake_home / "OneDrive" / "Desktop"
    onedrive_desktop.mkdir(parents=True)
    # Deliberately omit fake_home / "Desktop" to verify mkdir(parents=True, exist_ok=True)
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: fake_home))

    backend = FakeClipboardBackend()
    backend.set_text("https://www.youtube.com/watch?v=dQw4w9WgXcQ")
    service = ClipboardService(backend=backend, default_save_dir=None)

    res = service.save_to_file(filename="youtube_url.txt", directory="desktop")
    assert res["status"] == "saved"
    expected_path = (fake_home / "Desktop" / "youtube_url.txt").resolve()
    assert Path(res["saved_path"]) == expected_path
    assert expected_path.is_file()
    assert "OneDrive" not in res["saved_path"]
    assert expected_path.read_text(encoding="utf-8") == "https://www.youtube.com/watch?v=dQw4w9WgXcQ"


async def test_default_save_clipboard_runs_without_approval_and_voice_direct(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Verify save_clipboard_to_file executes directly in-process in ToolRegistry and Voice."""
    from clea.surfaces.voice.backends.base import default_voice_tools

    # 1. ToolRegistry default registration executes directly without PermissionDeniedError
    fake_backend = FakeClipboardBackend()
    fake_backend.set_text("https://youtu.be/abc123")
    service = ClipboardService(backend=fake_backend, default_save_dir=tmp_path)
    registry = ToolRegistry()
    register_clipboard_tools(registry, service)

    res = await registry.execute(
        "save_clipboard_to_file",
        {"filename": "youtube_url.txt", "directory": "desktop"},
    )
    assert res["status"] == "saved"
    assert Path(res["saved_path"]).is_file()

    # 2. Voice tool declarations expose save_clipboard_to_file directly and warn delegate_task
    voice_tools = {t.name: t for t in default_voice_tools()}
    assert "save_clipboard_to_file" in voice_tools
    assert "transform_clipboard" in voice_tools
    assert "save_clipboard_to_file" in voice_tools["delegate_task"].description


# --------------------------------------------- CleaDaemon Integration Tests


def test_daemon_clipboard_service_and_housekeeping_tick(tmp_path: Path) -> None:
    fake_backend = FakeClipboardBackend()
    service = ClipboardService(backend=fake_backend, default_save_dir=tmp_path)
    mem = MemoryStore(base_dir=tmp_path / "memory")
    registry = ToolRegistry()
    agent = AgentLoop(provider=MockLLMProvider(), registry=registry, memory=mem)

    daemon = CleaDaemon(
        agent=agent,
        token="test-token",
        token_file=tmp_path / "token.txt",
        lock_file=tmp_path / "lock.json",
        clipboard_service=service,
    )

    # Ensure clipboard service is attached
    assert daemon.clipboard_service is service

    # Ensure tools were automatically registered
    defs = {d.name: d for d in daemon.agent.registry.list_definitions()}
    assert "read_clipboard" in defs
    assert "transform_clipboard" in defs
    assert "save_clipboard_to_file" in defs
    assert "open_path_or_url" in defs

    # Poll sequence and advance
    assert daemon.clipboard_service.poll_sequence() == 1
    fake_backend.write_text("Hello Clea")

    # Call housekeeping tick
    daemon.housekeeping_tick()
    assert daemon.clipboard_service.poll_sequence() == 2


# --------------------------------------------- Step 1 Live Bug Regression Tests


def test_win32_cf_bitmap_triggers_imagegrab_fallback(monkeypatch: pytest.MonkeyPatch) -> None:
    from PIL import Image

    from clea.tools.clipboard import CF_BITMAP

    class _DummyFn:
        argtypes: list[object] = []
        restype: object = None

    class _FakeUser32:
        GetClipboardSequenceNumber = _DummyFn()
        OpenClipboard = _DummyFn()
        CloseClipboard = _DummyFn()
        EmptyClipboard = _DummyFn()
        GetClipboardData = _DummyFn()
        SetClipboardData = _DummyFn()
        IsClipboardFormatAvailable = _DummyFn()
        RegisterClipboardFormatW = _DummyFn()

        def __init__(self) -> None:
            self.OpenClipboard = lambda hwnd: 1
            self.CloseClipboard = lambda: 1
            self.EmptyClipboard = lambda: 1
            self.GetClipboardSequenceNumber = lambda: 42
            self.RegisterClipboardFormatW = lambda name: 49152
            self.IsClipboardFormatAvailable = lambda fmt: int(fmt == CF_BITMAP)
            self.GetClipboardData = lambda fmt: None

    class _FakeKernel32:
        GlobalAlloc = _DummyFn()
        GlobalLock = _DummyFn()
        GlobalUnlock = _DummyFn()
        GlobalSize = _DummyFn()

    sample_img = Image.new("RGBA", (12, 8), (255, 0, 0, 255))
    monkeypatch.setattr("PIL.ImageGrab.grabclipboard", lambda: sample_img)

    backend = Win32ClipboardBackend(user32=_FakeUser32(), kernel32=_FakeKernel32())
    snapshot = backend.read_snapshot()

    assert snapshot.kind == "image"
    assert snapshot.image_dimensions == (12, 8)
    assert snapshot.image_bytes is not None


def test_windows_os_read_clipboard_active_image_and_history_reflection(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from clea.tools import windows_os

    script = windows_os._POWERSHELL_READ_CLIPBOARD_HISTORY_SCRIPT
    assert "AsStreamForRead" in script
    assert "GetMethods()" in script

    class _FakeLiveUser32:
        def OpenClipboard(self, hwnd: object) -> int:
            return 1

        def CloseClipboard(self) -> int:
            return 1

        def IsClipboardFormatAvailable(self, fmt: int) -> int:
            return 0

    monkeypatch.setattr(windows_os, "_get_win32_user32", lambda u32=None: _FakeLiveUser32())
    monkeypatch.setattr(windows_os, "_get_win32_kernel32", lambda k32=None: object())
    monkeypatch.setattr(
        Win32ClipboardBackend,
        "read_snapshot",
        lambda self: ClipboardSnapshot(
            kind="image",
            image_bytes=b"\x89PNG\r\n\x1a\nfake",
            image_dimensions=(1916, 1025),
        ),
    )

    result = windows_os.read_clipboard(include_history=False)

    assert result["ok"] is True
    assert result["kind"] == "image"
    assert result["image"] is not None
    assert result["image"]["width"] == 1916
    assert result["image"]["height"] == 1025


def test_open_path_or_url_opens_saved_desktop_file_and_clipboard_url(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import webbrowser

    desktop_dir = tmp_path / "Desktop"
    desktop_dir.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    print_file = desktop_dir / "print.txt"
    print_file.write_text("hello desktop", encoding="utf-8")

    opened: list[str] = []
    backend = FakeClipboardBackend()
    service = ClipboardService(backend=backend, default_save_dir=desktop_dir)

    # 1. Desktop relative filename
    res_rel = open_path_or_url("print.txt", clipboard_service=service, opener_fn=opened.append)
    assert res_rel["status"] == "opened"
    assert Path(opened[-1]).resolve() == print_file.resolve()

    # 2. Full path
    res_abs = open_path_or_url(str(print_file), clipboard_service=service, opener_fn=opened.append)
    assert res_abs["status"] == "opened"
    assert Path(opened[-1]).resolve() == print_file.resolve()

    # 3. Default to last_saved_path when target omitted (even if clipboard holds plain text)
    backend.write_text("saved content")
    save_res = service.save_to_file(filename="recent.txt")
    assert save_res["status"] == "saved"
    res_last = open_path_or_url(clipboard_service=service, opener_fn=opened.append)
    assert res_last["status"] == "opened"
    assert Path(opened[-1]).name == "recent.txt"

    # 4. Clipboard URL via use_clipboard=True
    backend.write_text("https://example.com/watch?v=123")
    res_url = open_path_or_url(
        use_clipboard=True, clipboard_service=service, opener_fn=opened.append
    )
    assert res_url["status"] == "opened"
    assert opened[-1] == "https://example.com/watch?v=123"

    # 5. Missing file returns error
    res_missing = open_path_or_url(
        "missing_file.txt", clipboard_service=service, opener_fn=opened.append
    )
    assert res_missing["status"] == "error"

    # 6. Disallowed shell binary rejection
    res_bad = open_path_or_url("cmd.exe", clipboard_service=service, opener_fn=opened.append)
    assert res_bad["status"] == "error"

    # 7. ToolRegistry invocation (with os.startfile and webbrowser stubbed)
    import asyncio
    import os

    from clea.tools import AuditLogger

    browser_opened: list[str] = []
    monkeypatch.setattr(
        os, "startfile", lambda url: browser_opened.append(url), raising=False
    )
    monkeypatch.setattr(webbrowser, "open", lambda url: browser_opened.append(url) or True)
    registry = ToolRegistry(audit_logger=AuditLogger(log_path=tmp_path / "audit.jsonl"))
    register_clipboard_tools(registry, service)
    reg_res = asyncio.run(
        registry.execute("open_path_or_url", {"target": "https://example.com/docs"})
    )
    assert reg_res["status"] == "opened"
    assert browser_opened == ["https://example.com/docs"]


def test_open_or_focus_app_resolves_desktop_relative_target_path(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from clea.tools import windows_os

    desktop_dir = tmp_path / "Desktop"
    desktop_dir.mkdir()
    monkeypatch.setattr(Path, "home", staticmethod(lambda: tmp_path))

    print_file = desktop_dir / "print.txt"
    print_file.write_text("print('ok')", encoding="utf-8")

    popen_calls: list[list[str]] = []

    class _FakeUser32:
        def EnumWindows(self, cb: object, lparam: int) -> int:
            return 1

        def IsWindowVisible(self, hwnd: int) -> int:
            return 0

    res = windows_os.open_or_focus_app(
        "notepad",
        target_path="print.txt",
        user32=_FakeUser32(),
        which_fn=lambda name: r"C:\Windows\System32\notepad.exe" if "notepad" in name else None,
        popen_fn=lambda cmd, **kw: popen_calls.append(list(cmd)) or object(),
    )

    assert res["ok"] is True
    assert popen_calls
    assert str(print_file.resolve()) in popen_calls[0]


# --------------------------------- PNG IEND Trim, CF_BITMAP GetDIBits, & Unified Read Tests


def test_registered_png_iend_heap_padding_trim_and_decode() -> None:
    """Verify registered PNG format strips Windows heap padding after IEND and decodes."""
    import ctypes
    import io

    from PIL import Image

    from clea.tools.clipboard import PNG_IEND_TRAILER, _decode_registered_png

    src = Image.new("RGBA", (14, 10), (12, 134, 210, 200))
    raw_buf = io.BytesIO()
    src.save(raw_buf, format="PNG")
    clean_png = raw_buf.getvalue()
    assert clean_png.endswith(PNG_IEND_TRAILER)

    # Simulate Windows GlobalSize heap padding after IEND chunk
    padded_png = clean_png + b"\x00\xde\xad\xbe\xef" * 24

    trimmed, dims = _decode_registered_png(padded_png)
    assert trimmed == clean_png
    assert dims == (14, 10)
    with Image.open(io.BytesIO(trimmed)) as decoded:
        assert decoded.format == "PNG"
        assert decoded.size == (14, 10)
        assert decoded.getpixel((0, 0)) == (12, 134, 210, 200)

    # Corrupt/empty payloads return (None, None) rather than crashing
    assert _decode_registered_png(b"") == (None, None)
    assert _decode_registered_png(b"not a png at all" + PNG_IEND_TRAILER + b"\x00\x00") == (
        None,
        None,
    )

    # Full Win32ClipboardBackend.read_snapshot() path with GlobalSize returning padded length
    cbuf = ctypes.create_string_buffer(padded_png, len(padded_png))
    addr = ctypes.addressof(cbuf)
    cf_png = 49155

    mock_user32 = MagicMock()
    mock_kernel32 = MagicMock()
    mock_user32.OpenClipboard.return_value = 1
    mock_user32.RegisterClipboardFormatW.return_value = cf_png
    mock_user32.IsClipboardFormatAvailable.side_effect = lambda fmt: int(fmt == cf_png)
    mock_user32.GetClipboardData.return_value = addr
    mock_kernel32.GlobalLock.return_value = addr
    mock_kernel32.GlobalSize.return_value = len(padded_png)

    backend = Win32ClipboardBackend(
        user32=mock_user32, kernel32=mock_kernel32, gdi32=MagicMock()
    )
    snap = backend.read_snapshot()
    assert snap.kind == "image"
    assert snap.image_format == "PNG"
    assert snap.image_dimensions == (14, 10)
    assert snap.image_bytes == clean_png


def test_cf_bitmap_getdibits_32bit_bgra_extraction() -> None:
    """Verify CF_BITMAP extraction via GetObjectW + CreateCompatibleDC + GetDIBits (BGRA)."""
    import ctypes
    import io

    from PIL import Image

    from clea.tools.clipboard import (
        _BITMAPINFOHEADER,
        _WIN32_BITMAP,
        BI_RGB,
        CF_BITMAP,
        DIB_RGB_COLORS,
    )

    w, h = 10, 6
    fake_hbitmap = 0x00007FF812345678
    fake_hdc = 0x00007FF8ABCDEF00
    bgra_pixels = bytes([210, 128, 42, 255] * (w * h))

    captured_bmi: dict[str, Any] = {}
    deleted_dcs: list[Any] = []

    class _FakeGdi32:
        def GetObjectW(self, hbm: Any, size: int, bmp_ptr: Any) -> int:
            assert hbm == fake_hbitmap
            assert size == ctypes.sizeof(_WIN32_BITMAP)
            bmp = ctypes.cast(bmp_ptr, ctypes.POINTER(_WIN32_BITMAP)).contents
            bmp.bmWidth = w
            bmp.bmHeight = h
            return size

        def CreateCompatibleDC(self, hdc: Any) -> int:
            assert hdc is None
            return fake_hdc

        def GetDIBits(
            self,
            hdc: Any,
            hbm: Any,
            start: int,
            lines: int,
            buf_ptr: Any,
            bmi_ptr: Any,
            usage: int,
        ) -> int:
            assert hdc == fake_hdc
            assert hbm == fake_hbitmap
            assert start == 0
            assert lines == h
            assert usage == DIB_RGB_COLORS
            bmi = ctypes.cast(bmi_ptr, ctypes.POINTER(_BITMAPINFOHEADER)).contents
            captured_bmi["biSize"] = bmi.biSize
            captured_bmi["biWidth"] = bmi.biWidth
            captured_bmi["biHeight"] = bmi.biHeight
            captured_bmi["biPlanes"] = bmi.biPlanes
            captured_bmi["biBitCount"] = bmi.biBitCount
            captured_bmi["biCompression"] = bmi.biCompression
            ctypes.memmove(buf_ptr, bgra_pixels, len(bgra_pixels))
            return h

        def DeleteDC(self, hdc: Any) -> int:
            deleted_dcs.append(hdc)
            return 1

    mock_user32 = MagicMock()
    mock_user32.OpenClipboard.return_value = 1
    mock_user32.RegisterClipboardFormatW.return_value = 49152
    mock_user32.IsClipboardFormatAvailable.side_effect = lambda fmt: int(fmt == CF_BITMAP)
    mock_user32.GetClipboardData.side_effect = (
        lambda fmt: fake_hbitmap if fmt == CF_BITMAP else None
    )

    backend = Win32ClipboardBackend(
        user32=mock_user32,
        kernel32=MagicMock(),
        gdi32=_FakeGdi32(),
    )
    snap = backend.read_snapshot()

    assert snap.kind == "image"
    assert snap.image_format == "PNG"
    assert snap.image_dimensions == (10, 6)
    assert snap.image_bytes is not None
    assert captured_bmi == {
        "biSize": 40,
        "biWidth": 10,
        "biHeight": -6,
        "biPlanes": 1,
        "biBitCount": 32,
        "biCompression": BI_RGB,
    }
    assert deleted_dcs == [fake_hdc]

    with Image.open(io.BytesIO(snap.image_bytes)) as decoded:
        assert decoded.format == "PNG"
        assert decoded.size == (10, 6)
        assert decoded.getpixel((0, 0)) == (42, 128, 210, 255)


def test_cf_bitmap_getdibits_promotes_zero_alpha_bgrx_and_handles_real_gdi_bitmap() -> None:
    """Verify GDI BGRX (all-zero alpha) promotion and real Win32 HBITMAP roundtrip."""
    import ctypes
    import io
    import sys

    from PIL import Image

    from clea.tools.clipboard import _WIN32_BITMAP, _decode_hbitmap_to_png

    w, h = 4, 3
    bgrx_pixels = bytes([200, 100, 50, 0] * (w * h))
    deleted_dcs: list[Any] = []

    class _ZeroAlphaGdi32:
        def GetObjectW(self, hbm: Any, size: int, bmp_ptr: Any) -> int:
            bmp = ctypes.cast(bmp_ptr, ctypes.POINTER(_WIN32_BITMAP)).contents
            bmp.bmWidth = w
            bmp.bmHeight = h
            return size

        def CreateCompatibleDC(self, hdc: Any) -> int:
            return 999

        def GetDIBits(
            self,
            hdc: Any,
            hbm: Any,
            start: int,
            lines: int,
            buf_ptr: Any,
            bmi_ptr: Any,
            usage: int,
        ) -> int:
            ctypes.memmove(buf_ptr, bgrx_pixels, len(bgrx_pixels))
            return h

        def DeleteDC(self, hdc: Any) -> int:
            deleted_dcs.append(hdc)
            return 1

    png_bytes, dims = _decode_hbitmap_to_png(1234, _ZeroAlphaGdi32())
    assert dims == (4, 3)
    assert png_bytes is not None
    assert deleted_dcs == [999]
    with Image.open(io.BytesIO(png_bytes)) as decoded:
        assert decoded.getpixel((0, 0)) == (50, 100, 200, 255)

    # Pure-black GDI BGRX bitmap (all bytes zero) must also promote alpha to 255, not 0
    black_bgrx = b"\x00" * (w * h * 4)

    class _BlackBgrxGdi32(_ZeroAlphaGdi32):
        def GetDIBits(
            self,
            hdc: Any,
            hbm: Any,
            start: int,
            lines: int,
            buf_ptr: Any,
            bmi_ptr: Any,
            usage: int,
        ) -> int:
            ctypes.memmove(buf_ptr, black_bgrx, len(black_bgrx))
            return h

    black_png, black_dims = _decode_hbitmap_to_png(1234, _BlackBgrxGdi32())
    assert black_dims == (4, 3)
    assert black_png is not None
    with Image.open(io.BytesIO(black_png)) as black_decoded:
        assert black_decoded.getpixel((0, 0)) == (0, 0, 0, 255)

    # Real Win32 in-memory HBITMAP test (never touches user clipboard)
    if sys.platform == "win32":
        import ctypes.wintypes as wintypes

        backend = Win32ClipboardBackend()
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetDC.argtypes = [ctypes.c_void_p]
        user32.GetDC.restype = ctypes.c_void_p
        user32.ReleaseDC.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        user32.ReleaseDC.restype = wintypes.BOOL

        gdi32 = backend._gdi32
        gdi32.CreateCompatibleBitmap.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_int]
        gdi32.CreateCompatibleBitmap.restype = ctypes.c_void_p
        gdi32.SelectObject.argtypes = [ctypes.c_void_p, ctypes.c_void_p]
        gdi32.SelectObject.restype = ctypes.c_void_p
        gdi32.PatBlt.argtypes = [
            ctypes.c_void_p,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            ctypes.c_int,
            wintypes.DWORD,
        ]
        gdi32.PatBlt.restype = wintypes.BOOL
        gdi32.DeleteObject.argtypes = [ctypes.c_void_p]
        gdi32.DeleteObject.restype = wintypes.BOOL

        hdc_screen = user32.GetDC(None)
        hdc_mem = gdi32.CreateCompatibleDC(hdc_screen)
        hbmp = gdi32.CreateCompatibleBitmap(hdc_screen, 6, 4)
        old = gdi32.SelectObject(hdc_mem, hbmp)
        gdi32.PatBlt(hdc_mem, 0, 0, 6, 4, 0x00FF0062)  # WHITENESS
        gdi32.SelectObject(hdc_mem, old)
        try:
            real_png, real_dims = _decode_hbitmap_to_png(hbmp, gdi32)
            assert real_dims == (6, 4)
            assert real_png is not None
            with Image.open(io.BytesIO(real_png)) as real_img:
                assert real_img.size == (6, 4)
                assert real_img.getpixel((0, 0)) == (255, 255, 255, 255)

            old2 = gdi32.SelectObject(hdc_mem, hbmp)
            gdi32.PatBlt(hdc_mem, 0, 0, 6, 4, 0x00000042)  # BLACKNESS
            gdi32.SelectObject(hdc_mem, old2)
            real_black_png, real_black_dims = _decode_hbitmap_to_png(hbmp, gdi32)
            assert real_black_dims == (6, 4)
            assert real_black_png is not None
            with Image.open(io.BytesIO(real_black_png)) as real_black_img:
                assert real_black_img.size == (6, 4)
                assert real_black_img.getpixel((0, 0)) == (0, 0, 0, 255)
        finally:
            gdi32.DeleteObject(hbmp)
            gdi32.DeleteDC(hdc_mem)
            user32.ReleaseDC(None, hdc_screen)


def test_windows_os_read_clipboard_unifies_live_reads_and_avoids_unknown(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify windows_os._read_clipboard_win32 delegates directly and never reports 'unknown'."""
    import subprocess

    from clea.tools import windows_os

    monkeypatch.setattr(
        windows_os,
        "_get_win32_user32",
        lambda _u32=None: (_ for _ in ()).throw(AssertionError("_get_win32_user32 should not run")),
    )
    monkeypatch.setattr(
        Win32ClipboardBackend,
        "read_snapshot",
        lambda self: ClipboardSnapshot(
            kind="image",
            image_bytes=b"\x89PNG\r\n\x1a\nfake_png_bytes",
            image_dimensions=(1280, 720),
        ),
    )

    # 1. include_history=False delegates directly to Win32ClipboardBackend.read_snapshot()
    direct_res = windows_os.read_clipboard(include_history=False)
    assert direct_res["ok"] is True
    assert direct_res["kind"] == "image"
    assert direct_res["image"]["width"] == 1280
    assert direct_res["image"]["height"] == 720

    # 2. include_history=True when WinRT top item is 'unknown' (and powershell_runner is None)
    monkeypatch.setattr(windows_os.shutil, "which", lambda _exe: "powershell.exe")
    monkeypatch.setattr(
        windows_os.subprocess,
        "run",
        lambda *args, **kwargs: subprocess.CompletedProcess(
            args[0] if args else ["powershell.exe"],
            0,
            stdout=json.dumps(
                {
                    "history_enabled": True,
                    "total_items": 1,
                    "items": [
                        {
                            "index": 0,
                            "id": "{id-0}",
                            "timestamp": "2026-11-18T14:00:00Z",
                            "kind": "unknown",
                            "text": None,
                            "truncated": False,
                            "image": None,
                        }
                    ],
                }
            ),
            stderr="",
        ),
    )

    from clea.core.protocol import OsReadClipboardResult

    hist_res = windows_os.read_clipboard(include_history=True)
    assert hist_res["ok"] is True
    assert hist_res["kind"] == "image"
    assert hist_res["image"]["width"] == 1280
    assert hist_res["image"]["height"] == 720
    assert hist_res["items"][0]["kind"] == "image"
    assert hist_res["items"][0]["image"]["width"] == 1280
    assert hist_res["items"][0]["image"]["height"] == 720
    parsed_model = OsReadClipboardResult(**hist_res)
    assert parsed_model.kind == "image"
    assert parsed_model.items[0].kind == "image"

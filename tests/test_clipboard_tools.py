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
    assert res["status"] == "ok"
    assert res["kind"] == "text"
    saved_path = Path(res["path"])
    assert saved_path.exists()
    assert saved_path.suffix == ".json"
    assert "Clea" in saved_path.read_text(encoding="utf-8")


def test_save_to_file_python_auto_extension(tmp_path: Path) -> None:
    backend = FakeClipboardBackend()
    py_code = "def get_greeting(name: str) -> str:\n    return f'Hello {name}'\n"
    backend.set_text(py_code)
    service = ClipboardService(backend=backend, default_save_dir=tmp_path)

    res = service.save_to_file(directory="desktop")
    assert res["status"] == "ok"
    saved_path = Path(res["path"])
    assert saved_path.exists()
    assert saved_path.suffix == ".py"


def test_save_to_file_markdown_auto_extension(tmp_path: Path) -> None:
    backend = FakeClipboardBackend()
    md_content = "# Project Overview\n\n- Milestone 1\n- Milestone 2\n"
    backend.set_text(md_content)
    service = ClipboardService(backend=backend, default_save_dir=tmp_path)

    res = service.save_to_file(directory="desktop")
    assert res["status"] == "ok"
    saved_path = Path(res["path"])
    assert saved_path.exists()
    assert saved_path.suffix == ".md"


def test_save_to_file_plain_text_auto_extension(tmp_path: Path) -> None:
    backend = FakeClipboardBackend()
    backend.set_text("Just a regular message without code or formatting.")
    service = ClipboardService(backend=backend, default_save_dir=tmp_path)

    res = service.save_to_file(directory="desktop")
    assert res["status"] == "ok"
    saved_path = Path(res["path"])
    assert saved_path.exists()
    assert saved_path.suffix == ".txt"


def test_save_to_file_image_bytes(tmp_path: Path) -> None:
    backend = FakeClipboardBackend()
    fake_png = b"\x89PNG\r\n\x1a\n" + b"\x00" * 40
    backend.set_image(fake_png, dimensions=(100, 100))
    service = ClipboardService(backend=backend, default_save_dir=tmp_path)

    res = service.save_to_file(filename="screenshot", directory="desktop")
    assert res["status"] == "ok"
    assert res["kind"] == "image"
    saved_path = Path(res["path"])
    assert saved_path.exists()
    assert saved_path.suffix == ".png"
    assert saved_path.read_bytes() == fake_png


def test_save_to_file_collision_safe_naming(tmp_path: Path) -> None:
    backend = FakeClipboardBackend()
    backend.set_text("test collision content")
    service = ClipboardService(backend=backend, default_save_dir=tmp_path)

    res1 = service.save_to_file(filename="notes.txt", directory="desktop")
    res2 = service.save_to_file(filename="notes.txt", directory="desktop")
    res3 = service.save_to_file(filename="notes.txt", directory="desktop")

    path1 = Path(res1["path"])
    path2 = Path(res2["path"])
    path3 = Path(res3["path"])

    assert path1.name == "notes.txt"
    assert path2.name == "notes-1.txt"
    assert path3.name == "notes-2.txt"
    assert path1.exists() and path2.exists() and path3.exists()


def test_save_to_file_sanitizes_path_traversal(tmp_path: Path) -> None:
    backend = FakeClipboardBackend()
    backend.set_text("safe content")
    service = ClipboardService(backend=backend, default_save_dir=tmp_path)

    res = service.save_to_file(filename="../../escape/secrets.txt", directory="desktop")
    assert res["status"] == "ok"
    saved_path = Path(res["path"])
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
    assert save_res["status"] == "ok"
    assert Path(save_res["path"]).exists()


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
    assert s_res["status"] == "ok"
    assert Path(s_res["path"]).exists()


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

    # Poll sequence and advance
    assert daemon.clipboard_service.poll_sequence() == 1
    fake_backend.write_text("Hello Clea")

    # Call housekeeping tick
    daemon.housekeeping_tick()
    assert daemon.clipboard_service.poll_sequence() == 2

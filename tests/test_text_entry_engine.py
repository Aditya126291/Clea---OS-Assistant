"""Deterministic unit tests for clipboard multi-format save/restore, Win+V exclusion,
Tiered TextEntryEngine, modifier hygiene, and cooperative cancellation.
"""

from __future__ import annotations

import ctypes
import struct
import subprocess
import threading
from typing import Any
from unittest.mock import patch

import pytest

from clea.tools import windows_os
from clea.tools.clipboard import CF_DIB, CF_UNICODETEXT, ClipboardLockedError, Win32ClipboardBackend


class _FakeClipboardUser32:
    def __init__(self, *, open_failures: int = 0) -> None:
        self.open_failures_remaining = open_failures
        self.open_calls = 0
        self.close_calls = 0
        self.empty_calls = 0
        self.opened = False
        self.formats: dict[int, int] = {}
        self.registered: dict[str, int] = {}
        self._next_reg = 0xC100

    def OpenClipboard(self, _hwnd: Any) -> int:
        self.open_calls += 1
        if self.open_failures_remaining > 0:
            self.open_failures_remaining -= 1
            return 0
        self.opened = True
        return 1

    def CloseClipboard(self) -> int:
        self.close_calls += 1
        self.opened = False
        return 1

    def EmptyClipboard(self) -> int:
        self.empty_calls += 1
        self.formats.clear()
        return 1

    def RegisterClipboardFormatW(self, name: str) -> int:
        if name not in self.registered:
            self.registered[name] = self._next_reg
            self._next_reg += 1
        return self.registered[name]

    def EnumClipboardFormats(self, fmt: int) -> int:
        ordered = sorted(self.formats.keys())
        if fmt == 0:
            return ordered[0] if ordered else 0
        try:
            idx = ordered.index(fmt)
        except ValueError:
            return 0
        return ordered[idx + 1] if idx + 1 < len(ordered) else 0

    def IsClipboardFormatAvailable(self, fmt: int) -> int:
        return 1 if fmt in self.formats else 0

    def GetClipboardData(self, fmt: int) -> int:
        return self.formats.get(fmt, 0)

    def SetClipboardData(self, fmt: int, handle: int) -> int:
        self.formats[int(fmt)] = int(handle)
        return int(handle)


class _FakeKernel32:
    def __init__(self) -> None:
        self._blocks: dict[int, ctypes.Array[ctypes.c_char]] = {}
        self._next_handle = 0x5000

    def seed_bytes(self, raw: bytes) -> int:
        buf = ctypes.create_string_buffer(raw, len(raw))
        handle = self._next_handle
        self._next_handle += 1
        self._blocks[handle] = buf
        return handle

    def read_bytes(self, handle: int) -> bytes:
        buf = self._blocks[handle]
        return bytes(buf[:])

    def GlobalAlloc(self, _flags: int, size: int) -> int:
        buf = ctypes.create_string_buffer(max(1, int(size)))
        handle = self._next_handle
        self._next_handle += 1
        self._blocks[handle] = buf
        return handle

    def GlobalLock(self, handle: int) -> int:
        buf = self._blocks.get(int(handle))
        if buf is None:
            return 0
        return ctypes.addressof(buf)

    def GlobalUnlock(self, _handle: int) -> int:
        return 1

    def GlobalSize(self, handle: int) -> int:
        buf = self._blocks.get(int(handle))
        return len(buf) if buf is not None else 0

    def GlobalFree(self, handle: int) -> int:
        self._blocks.pop(int(handle), None)
        return 0


class _FakeInputUser32:
    def __init__(self, *, stuck_vks: set[int] | None = None) -> None:
        self.stuck_vks = set(stuck_vks or ())
        self.sent_batches: list[list[dict[str, int]]] = []
        self.cursor_moves: list[tuple[int, int]] = []
        self.fg_hwnd = 1001

    def GetForegroundWindow(self) -> int:
        return self.fg_hwnd

    def SetCursorPos(self, x: int, y: int) -> int:
        self.cursor_moves.append((int(x), int(y)))
        return 1

    def GetAsyncKeyState(self, vk: int) -> int:
        return 0x8000 if int(vk) in self.stuck_vks else 0

    def SendInput(self, count: int, arr: Any, _cb: int) -> int:
        batch: list[dict[str, int]] = []
        for idx in range(int(count)):
            item = arr[idx]
            if int(item.type) == windows_os.INPUT_KEYBOARD:
                ki = item.union.ki
                batch.append(
                    {
                        "type": int(item.type),
                        "vk": int(ki.wVk),
                        "scan": int(ki.wScan),
                        "flags": int(ki.dwFlags),
                    }
                )
            else:
                mi = item.union.mi
                batch.append(
                    {
                        "type": int(item.type),
                        "flags": int(mi.dwFlags),
                    }
                )
        self.sent_batches.append(batch)
        return int(count)


def test_open_clipboard_preserves_3_default_and_recovers_with_5_retries() -> None:
    u32_fail = _FakeClipboardUser32(open_failures=4)
    k32 = _FakeKernel32()
    backend_fail = Win32ClipboardBackend(user32=u32_fail, kernel32=k32)

    with patch("time.sleep") as mock_sleep:
        with pytest.raises(ClipboardLockedError):
            with backend_fail.open_clipboard():
                pass
    assert u32_fail.open_calls == 3
    mock_sleep.assert_called_with(0.025)

    u32_ok = _FakeClipboardUser32(open_failures=4)
    backend_ok = Win32ClipboardBackend(user32=u32_ok, kernel32=k32)
    with patch("time.sleep") as mock_sleep:
        with backend_ok.open_clipboard(attempts=5, retry_delay_s=0.02):
            pass
    assert u32_ok.open_calls == 5
    mock_sleep.assert_called_with(0.02)
    assert u32_ok.close_calls == 1


def test_capture_and_restore_multiformat_and_winv_exclusion_flags() -> None:
    u32 = _FakeClipboardUser32()
    k32 = _FakeKernel32()
    html_fmt = u32.RegisterClipboardFormatW("HTML Format")
    u32.formats[CF_UNICODETEXT] = k32.seed_bytes("User text\0".encode("utf-16-le"))
    u32.formats[CF_DIB] = k32.seed_bytes(b"\x28\x00\x00\x00FAKE_DIB")
    u32.formats[html_fmt] = k32.seed_bytes(b"Version:0.9\r\n<b>User text</b>\x00")

    cb = Win32ClipboardBackend(user32=u32, kernel32=k32)
    saved = cb.capture_raw_formats()
    assert set(saved.keys()) == {CF_UNICODETEXT, CF_DIB, html_fmt}

    cb.write_text_transient("1. Item one\n2. Item two")
    assert CF_UNICODETEXT in u32.formats
    for flag_name in (
        "ExcludeClipboardContentFromMonitorProcessing",
        "CanIncludeInClipboardHistory",
        "CanUploadToCloudClipboard",
    ):
        fmt_id = u32.registered[flag_name]
        assert fmt_id in u32.formats
        assert k32.read_bytes(u32.formats[fmt_id]) == struct.pack("<I", 0)

    cb.restore_raw_formats(saved, exclude_history=True)
    assert k32.read_bytes(u32.formats[CF_UNICODETEXT]) == "User text\0".encode("utf-16-le")
    assert k32.read_bytes(u32.formats[CF_DIB]) == b"\x28\x00\x00\x00FAKE_DIB"
    assert k32.read_bytes(u32.formats[html_fmt]) == b"Version:0.9\r\n<b>User text</b>\x00"
    assert u32.registered["CanIncludeInClipboardHistory"] in u32.formats


def test_enter_text_tiered_bulk_paste_restores_clipboard_and_waits_150ms() -> None:
    u32_cb = _FakeClipboardUser32()
    k32 = _FakeKernel32()
    u32_cb.formats[CF_UNICODETEXT] = k32.seed_bytes("Original\0".encode("utf-16-le"))
    cb = Win32ClipboardBackend(user32=u32_cb, kernel32=k32)

    u32_in = _FakeInputUser32(stuck_vks={0x10})  # stuck Shift
    sleeps: list[float] = []
    long_list = "\n".join(f"{i}. Item {i}" for i in range(1, 51))

    res = windows_os.gui_action(
        "type_text",
        text=long_list,
        user32=u32_in,
        clipboard_backend=cb,
        sleep_fn=sleeps.append,
    )
    assert res["ok"] is True
    assert res["entry_method"] == "clipboard_paste_atomic"
    assert res["clipboard_restored"] is True
    assert pytest.approx(sum(sleeps), rel=1e-6) == 0.15
    # Original clipboard content is restored cleanly
    assert k32.read_bytes(u32_cb.formats[CF_UNICODETEXT]) == "Original\0".encode("utf-16-le")
    # Stuck Shift keyup emitted, plus Ctrl+V chord
    flat_kbd = [ev for batch in u32_in.sent_batches for ev in batch]
    assert {"type": 1, "vk": 0x10, "scan": 0, "flags": windows_os.KEYEVENTF_KEYUP} in flat_kbd
    assert any(ev["vk"] == 0x56 and ev["flags"] == 0 for ev in flat_kbd)


def test_enter_text_tiered_clipboard_locked_falls_back_to_send_unicode_text() -> None:
    u32_cb = _FakeClipboardUser32(open_failures=99)
    cb = Win32ClipboardBackend(user32=u32_cb, kernel32=_FakeKernel32())
    u32_in = _FakeInputUser32()

    res = windows_os.enter_text_tiered(
        "A" * 45,
        u32=u32_in,
        interval_ms=0,
        clipboard_backend=cb,
    )
    assert res["method"] == "send_unicode_text"
    assert res["char_count"] == 45
    assert len(u32_in.sent_batches) == 45


def test_enter_text_tiered_short_single_line_skips_clipboard() -> None:
    class _ForbiddenClipboard:
        def capture_raw_formats(self, **_kw: Any) -> dict[int, bytes]:
            raise AssertionError("Short single-line text must not touch clipboard")

        def write_text_transient(self, _text: str, **_kw: Any) -> None:
            raise AssertionError("Short single-line text must not touch clipboard")

        def restore_raw_formats(self, _saved: dict[int, bytes], **_kw: Any) -> None:
            raise AssertionError("Short single-line text must not touch clipboard")

    u32_in = _FakeInputUser32()
    res = windows_os.enter_text_tiered(
        "Hello world",
        u32=u32_in,
        interval_ms=0,
        clipboard_backend=_ForbiddenClipboard(),
    )
    assert res["method"] == "send_unicode_text"
    assert len(u32_in.sent_batches) == len("Hello world")


def test_cooperative_cancel_event_aborts_tier3_typing_and_tier2_paste_restoring_clipboard() -> None:
    u32_in = _FakeInputUser32()
    cancel_ev = threading.Event()
    calls = 0

    class _CancelGuard:
        def assert_ready(self, step_label: str = "") -> None:
            nonlocal calls
            calls += 1
            if calls == 4:
                cancel_ev.set()

    with pytest.raises(windows_os.ActionCancelledError):
        windows_os._send_unicode_text(
            u32_in,
            "abcdefghij",
            interval_ms=0,
            guard=_CancelGuard(),  # type: ignore[arg-type]
            cancel_event=cancel_ev,
        )
    # Stopped before typing char index 4
    assert len(u32_in.sent_batches) == 3

    # Now test Tier-2 cancellation during post-paste settle still restores original clipboard
    u32_cb = _FakeClipboardUser32()
    k32 = _FakeKernel32()
    u32_cb.formats[CF_UNICODETEXT] = k32.seed_bytes("KeepMe\0".encode("utf-16-le"))
    cb = Win32ClipboardBackend(user32=u32_cb, kernel32=k32)
    cancel_paste = threading.Event()

    def _cancel_on_first_sleep(_dt: float) -> None:
        cancel_paste.set()

    res = windows_os.gui_action(
        "type_text",
        text="line 1\nline 2",
        user32=u32_in,
        clipboard_backend=cb,
        sleep_fn=_cancel_on_first_sleep,
        cancel_event=cancel_paste,
    )
    assert res["ok"] is False
    assert res["status"] == "cancelled"
    assert res["cancelled"] is True
    assert k32.read_bytes(u32_cb.formats[CF_UNICODETEXT]) == "KeepMe\0".encode("utf-16-le")


def test_ui_act_type_never_calls_uia_value_pattern_while_set_value_does() -> None:
    ps_commands: list[str] = []

    def _fake_ps(cmd: Any, **_kw: Any) -> subprocess.CompletedProcess[str]:
        cmd_str = cmd if isinstance(cmd, str) else " ".join(str(x) for x in cmd)
        ps_commands.append(cmd_str)
        return subprocess.CompletedProcess(
            args=cmd,
            returncode=0,
            stdout='{"found": true, "acted": true, "method": "value_pattern"}',
            stderr="",
        )

    windows_os.set_ui_snapshot_state(
        {
            "ok": True,
            "snapshot_id": "snap-tier1",
            "window_title": "Editor",
            "hwnd": 77,
            "elements": [
                {
                    "ref": "e1",
                    "role": "edit",
                    "name": "Body",
                    "automation_id": "editor_body",
                    "control_type": "Edit",
                    "bounds": {"x": 10, "y": 10, "width": 300, "height": 200},
                    "center": {"cx": 160, "cy": 110},
                }
            ],
        }
    )

    u32_in = _FakeInputUser32()
    res_type = windows_os.ui_act(
        "e1",
        "type",
        value="append this",
        interval_ms=0,
        powershell_runner=_fake_ps,
        user32=u32_in,
        _capture_snapshot=False,
    )
    assert res_type["ok"] is True
    assert res_type["method"] == "win32_type"
    assert ps_commands == []  # Never touched ValuePattern for 'type'

    res_set = windows_os.ui_act(
        "e1",
        "set_value",
        value="replace all",
        powershell_runner=_fake_ps,
        _capture_snapshot=False,
    )
    assert res_set["ok"] is True
    assert res_set["method"] == "value_pattern"
    assert len(ps_commands) == 1
    assert "-Action 'set_value'" in ps_commands[0]

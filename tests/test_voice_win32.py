"""Unit tests for Win32 ctypes hotkey module."""

from __future__ import annotations

import time
from unittest.mock import MagicMock

import pytest

from clea.surfaces.voice.win32 import (
    MOD_ALT,
    MOD_CONTROL,
    MOD_NOREPEAT,
    MOD_SHIFT,
    WM_HOTKEY,
    HotKeyManager,
    Win32API,
    parse_hotkey,
)


def test_parse_hotkey_valid() -> None:
    mods, vk = parse_hotkey("Ctrl+Alt+V")
    assert mods == (MOD_NOREPEAT | MOD_CONTROL | MOD_ALT)
    assert vk == ord("V")

    mods2, vk2 = parse_hotkey("ctrl+shift+space")
    assert mods2 == (MOD_NOREPEAT | MOD_CONTROL | MOD_SHIFT)
    assert vk2 == 0x20

    mods3, vk3 = parse_hotkey("Alt+F5")
    assert mods3 == (MOD_NOREPEAT | MOD_ALT)
    assert vk3 == 0x74  # 0x70 + 4


def test_parse_hotkey_invalid() -> None:
    with pytest.raises(ValueError, match="Invalid empty"):
        parse_hotkey("")

    with pytest.raises(ValueError, match="No key specified"):
        parse_hotkey("Ctrl+Alt")

    with pytest.raises(ValueError, match="Multiple primary keys"):
        parse_hotkey("Ctrl+A+B")

    with pytest.raises(ValueError, match="Unsupported"):
        parse_hotkey("Ctrl+Alt+???")


def test_hotkey_manager_lifecycle_with_mocked_win32() -> None:
    mock_u32 = MagicMock()
    mock_k32 = MagicMock()

    mock_k32.GetCurrentThreadId.return_value = 1234
    mock_u32.RegisterHotKey.return_value = 1
    mock_u32.UnregisterHotKey.return_value = 1
    mock_u32.PostThreadMessageW.return_value = 1

    triggered = False

    def on_hotkey() -> None:
        nonlocal triggered
        triggered = True

    # Simulate message pump: first message is WM_HOTKEY, then loop ends
    call_count = 0

    def fake_get_message(msg_ptr, hwnd, min_filter, max_filter) -> int:
        nonlocal call_count
        call_count += 1
        if call_count == 1:
            if msg_ptr:
                try:
                    msg_ptr._obj.message = WM_HOTKEY
                except AttributeError:
                    pass
            return 1
        time.sleep(0.05)
        return 0  # WM_QUIT

    mock_u32.GetMessageW.side_effect = fake_get_message

    api = Win32API(user32=mock_u32, kernel32=mock_k32)
    manager = HotKeyManager(api=api)

    manager.start("Ctrl+Alt+V", on_triggered=on_hotkey)
    time.sleep(0.15)
    manager.stop()

    assert mock_u32.RegisterHotKey.called
    assert mock_u32.UnregisterHotKey.called
    assert mock_u32.PostThreadMessageW.called


def test_hotkey_manager_registration_failure() -> None:
    mock_u32 = MagicMock()
    mock_k32 = MagicMock()

    mock_k32.GetCurrentThreadId.return_value = 5678
    mock_u32.RegisterHotKey.return_value = 0  # registration failure

    api = Win32API(user32=mock_u32, kernel32=mock_k32)
    manager = HotKeyManager(api=api)

    with pytest.raises(OSError, match="RegisterHotKey failed"):
        manager.start("Ctrl+Alt+V", on_triggered=lambda: None)

    manager.stop()

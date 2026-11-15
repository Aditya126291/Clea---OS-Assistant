"""Unit and end-to-end RPC/voice tests for Tier-1 Windows OS & Git workspace tools."""

from __future__ import annotations

import asyncio
import base64
import contextlib
import ctypes
import json
import subprocess
import threading
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

import clea.core.daemon as daemon_mod
from clea.core.daemon import CleaDaemon, build_daemon
from clea.core.protocol import (
    METHOD_RESULTS,
    OsClickUiElementResult,
    OsControlMediaVolumeResult,
    OsControlSystemParams,
    OsControlSystemRequest,
    OsControlSystemResult,
    OsGuiActionResult,
    OsInspectUiElementsResult,
    OsOpenOrFocusAppResult,
    OsOpenUrlParams,
    OsOpenUrlRequest,
    OsOpenUrlResult,
    OsReadClipboardResult,
    OsSelectClipboardItemParams,
    OsSelectClipboardItemRequest,
    OsSelectClipboardItemResult,
    OsWriteClipboardResult,
    ResponseFrame,
    WorkspaceGitInspectResult,
    parse_frame,
)
from clea.memory import MemoryStore
from clea.providers.base import LLMProvider, Message, StreamChunk, TokenUsage
from clea.surfaces.voice.audio import AudioManager
from clea.surfaces.voice.backends.base import (
    ToolCallEvent,
    VoiceBackend,
    VoiceBackendEvent,
    VoiceSessionConfig,
    VoiceToolResponse,
)
from clea.surfaces.voice.config import VoiceConfig
from clea.surfaces.voice.runner import VoiceRunner
from clea.surfaces.voice.wake_word import WakeWordDetector
from clea.tools import ToolDefinition
from clea.tools.git_workspace import MAX_DIFF_CHARS, inspect_git_workspace
from clea.tools.registry import AuditLogger, ToolRegistry
from clea.tools.windows_os import (
    _INPUT,
    _POWERSHELL_CLICK_UI_SCRIPT,
    _POWERSHELL_INSPECT_UI_SCRIPT,
    _POWERSHELL_READ_CLIPBOARD_HISTORY_SCRIPT,
    _POWERSHELL_SELECT_CLIPBOARD_ITEM_SCRIPT,
    _POWERSHELL_SNAPSHOT_UI_SCRIPT,
    _POWERSHELL_UI_ACT_SCRIPT,
    _UI_REF_STORE,
    CF_UNICODETEXT,
    INPUT_KEYBOARD,
    INPUT_MOUSE,
    KEYEVENTF_EXTENDEDKEY,
    KEYEVENTF_KEYUP,
    KEYEVENTF_UNICODE,
    MOUSEEVENTF_LEFTDOWN,
    MOUSEEVENTF_LEFTUP,
    MOUSEEVENTF_RIGHTDOWN,
    MOUSEEVENTF_RIGHTUP,
    MOUSEEVENTF_WHEEL,
    SW_MAXIMIZE,
    SW_MINIMIZE,
    SW_RESTORE,
    VALID_MEDIA_VOLUME_ACTIONS,
    VALID_SYSTEM_ACTIONS,
    VK_MEDIA_NEXT_TRACK,
    VK_MEDIA_PLAY_PAUSE,
    VK_MEDIA_PREV_TRACK,
    VK_VOLUME_DOWN,
    VK_VOLUME_MUTE,
    VK_VOLUME_UP,
    WM_CLOSE,
    EnumeratedWindow,
    click_ui_element,
    control_media_volume,
    control_system,
    find_matching_start_app,
    get_installed_apps,
    gui_action,
    inspect_ui_elements,
    open_or_focus_app,
    open_url,
    read_clipboard,
    resolve_app_spec,
    select_clipboard_item,
    ui_act,
    ui_snapshot,
    write_clipboard,
)

# --------------------------------------------------------------------------- Test Doubles


@pytest.fixture(autouse=True)
def _isolate_clea_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Ensure all test executions are strictly isolated from %LOCALAPPDATA%\\clea."""
    log_dir = tmp_path / "logs"
    log_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLEA_DATA_DIR", str(tmp_path))


class _FakeClipboardWin32:
    """In-memory UTF-16LE clipboard backing Win32 user32 + kernel32 contracts."""

    def __init__(self, initial_text: str | None = None) -> None:
        self.opened = False
        self.buffers: dict[int, ctypes.Array[ctypes.c_char]] = {}
        self.next_handle = 100
        self.clipboard_handle: int | None = None
        if initial_text is not None:
            encoded = initial_text.encode("utf-16-le") + b"\x00\x00"
            h = self.GlobalAlloc(0x0002, len(encoded))
            ptr = self.GlobalLock(h)
            ctypes.memmove(ptr, encoded, len(encoded))
            self.GlobalUnlock(h)
            self.clipboard_handle = h

    # user32 surface
    def OpenClipboard(self, _hwnd: Any) -> int:
        self.opened = True
        return 1

    def CloseClipboard(self) -> int:
        self.opened = False
        return 1

    def EmptyClipboard(self) -> int:
        if not self.opened:
            return 0
        self.clipboard_handle = None
        return 1

    def IsClipboardFormatAvailable(self, fmt: int) -> int:
        return 1 if (fmt == CF_UNICODETEXT and self.clipboard_handle is not None) else 0

    def GetClipboardData(self, fmt: int) -> int | None:
        if not self.opened or fmt != CF_UNICODETEXT:
            return None
        return self.clipboard_handle

    def SetClipboardData(self, fmt: int, handle: int) -> int | None:
        if not self.opened or fmt != CF_UNICODETEXT or handle not in self.buffers:
            return None
        self.clipboard_handle = handle
        return handle

    # kernel32 surface
    def GlobalAlloc(self, _flags: int, size: int) -> int:
        handle = self.next_handle
        self.next_handle += 1
        self.buffers[handle] = ctypes.create_string_buffer(max(2, size))
        return handle

    def GlobalLock(self, handle: int) -> int | None:
        buf = self.buffers.get(handle)
        return ctypes.addressof(buf) if buf is not None else None

    def GlobalUnlock(self, _handle: int) -> int:
        return 1

    def GlobalFree(self, handle: int) -> None:
        self.buffers.pop(handle, None)


class _FakeWindowUser32:
    """Minimal user32 double tracking window focus, state, and keybd_event calls."""

    def __init__(self, *, iconic_hwnds: set[int] | None = None) -> None:
        self.iconic_hwnds = iconic_hwnds or set()
        self.show_calls: list[tuple[int, int]] = []
        self.foreground_calls: list[int] = []
        self.key_events: list[tuple[int, int, int, int]] = []
        self.post_messages: list[tuple[int, int, int, int]] = []
        self.lock_workstation_calls: int = 0
        self.foreground_window: int = 42

    def IsIconic(self, hwnd: int) -> int:
        return 1 if hwnd in self.iconic_hwnds else 0

    def ShowWindow(self, hwnd: int, cmd: int) -> int:
        self.show_calls.append((hwnd, cmd))
        return 1

    def SetForegroundWindow(self, hwnd: int) -> int:
        self.foreground_calls.append(hwnd)
        return 1

    def keybd_event(self, vk: int, scan: int, flags: int, extra: int) -> None:
        self.key_events.append((vk, scan, flags, extra))

    def PostMessageW(self, hwnd: int, msg: int, wparam: int, lparam: int) -> int:
        self.post_messages.append((hwnd, msg, wparam, lparam))
        return 1

    def LockWorkStation(self) -> int:
        self.lock_workstation_calls += 1
        return 1

    def GetForegroundWindow(self) -> int:
        return self.foreground_window


class _FakeLiveBackend(VoiceBackend):
    backend_name = "fake-live"

    def __init__(self) -> None:
        self.connected_config: VoiceSessionConfig | None = None
        self.closed = False
        self.event_queue: asyncio.Queue[VoiceBackendEvent | None] = asyncio.Queue()
        self.sent_tool_responses: list[VoiceToolResponse] = []

    @property
    def model_name(self) -> str:
        return "fake-live-model"

    async def connect(self, config: VoiceSessionConfig) -> None:
        self.connected_config = config
        self.closed = False

    async def close(self) -> None:
        self.closed = True
        await self.event_queue.put(None)

    async def send_audio(self, pcm_bytes: bytes) -> None:
        pass

    async def send_video_frame(self, image_bytes: bytes, *, mime_type: str = "image/jpeg") -> None:
        pass

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


class _FakeProvider(LLMProvider):
    provider_name = "fake"

    @property
    def model_name(self) -> str:
        return "fake-chat-model"

    async def stream_turn(
        self,
        *,
        messages: list[Message],
        tools: list[ToolDefinition],
        system_prompt: str,
    ) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(type="text_delta", text="ok")
        yield StreamChunk(type="done", usage=TokenUsage(input_tokens=1, output_tokens=1))


def _make_fake_audio() -> AudioManager:
    audio = AudioManager()
    audio.start = MagicMock()  # type: ignore[method-assign]
    audio.stop = MagicMock()  # type: ignore[method-assign]
    audio.play_audio = MagicMock()  # type: ignore[method-assign]
    audio.stop_output = MagicMock()  # type: ignore[method-assign]
    audio.set_input_callback = MagicMock()  # type: ignore[method-assign]
    return audio


def _make_fake_wake_word() -> WakeWordDetector:
    mock_model = MagicMock()
    mock_model.predict.return_value = {"hey_jarvis_v0.1": 0.0}
    return WakeWordDetector(model_instance=mock_model)


async def _wait_until(pred: Any, timeout: float = 5.0) -> None:
    deadline = asyncio.get_running_loop().time() + timeout
    while asyncio.get_running_loop().time() < deadline:
        if pred():
            return
        await asyncio.sleep(0.02)
    raise TimeoutError("Predicate not satisfied within timeout.")


async def _invoke_voice_tool(
    backend: _FakeLiveBackend,
    *,
    call_id: str,
    name: str,
    arguments: dict[str, Any] | None = None,
) -> VoiceToolResponse:
    before = len(backend.sent_tool_responses)
    await backend.event_queue.put(
        ToolCallEvent(call_id=call_id, name=name, arguments=arguments or {})
    )
    await _wait_until(
        lambda: any(r.call_id == call_id for r in backend.sent_tool_responses[before:])
    )
    return next(r for r in backend.sent_tool_responses[before:] if r.call_id == call_id)


@asynccontextmanager
async def _running_voice_daemon_env(
    tmp_path: Path,
) -> AsyncIterator[tuple[CleaDaemon, VoiceRunner, _FakeLiveBackend]]:
    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()
    log_file = tmp_path / "logs" / "audit.jsonl"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    reg = ToolRegistry(audit_logger=AuditLogger(log_file))
    daemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=_FakeProvider(),
        memory=mem,
        registry=reg,
    )
    await daemon.start()
    url = f"ws://127.0.0.1:{daemon.bound_port}"
    backends: list[_FakeLiveBackend] = []

    def _factory() -> _FakeLiveBackend:
        b = _FakeLiveBackend()
        backends.append(b)
        return b

    runner = VoiceRunner(
        config=VoiceConfig(silence_timeout_seconds=60.0),
        data_dir=tmp_path,
        daemon_url=url,
        token=daemon.token,
        backend_factory=_factory,
        audio_manager=_make_fake_audio(),
        wake_word_detector=_make_fake_wake_word(),
        enable_hotkey=False,
    )
    runner_task = asyncio.create_task(runner.run_forever())
    try:
        await _wait_until(lambda: runner._daemon_listener_active)
        await runner.start_call(trigger="manual")
        await _wait_until(lambda: len(backends) == 1)
        try:
            yield daemon, runner, backends[-1]
        finally:
            if runner.call_state != "idle":
                with contextlib.suppress(Exception):
                    await runner.end_call(reason="test_done")
    finally:
        await runner.stop()
        with contextlib.suppress(asyncio.CancelledError):
            await runner_task
        await daemon.stop()


# --------------------------------------------------------------------------- 1. open_or_focus_app


def test_resolve_app_spec_aliases_and_install_paths(tmp_path: Path) -> None:
    local_programs = tmp_path / "LocalAppData" / "Programs"
    vscode_exe = local_programs / "Microsoft VS Code" / "Code.exe"
    vscode_exe.parent.mkdir(parents=True)
    vscode_exe.write_text("stub", encoding="utf-8")

    env = {"LOCALAPPDATA": str(tmp_path / "LocalAppData")}

    canonical, cmd = resolve_app_spec("code", which_fn=lambda _n: None, env=env)
    assert canonical == "vscode"
    assert cmd == [str(vscode_exe)]

    canonical_term, cmd_term = resolve_app_spec(
        "wt",
        which_fn=lambda name: r"C:\Apps\wt.exe" if name == "wt.exe" else None,
        env={},
    )
    assert canonical_term == "terminal"
    assert cmd_term == [r"C:\Apps\wt.exe"]

    canonical_settings, cmd_settings = resolve_app_spec(
        "settings",
        which_fn=lambda name: r"C:\Windows\explorer.exe" if name == "explorer.exe" else None,
        env={},
    )
    assert canonical_settings == "settings"
    assert cmd_settings == [r"C:\Windows\explorer.exe", "ms-settings:"]


@pytest.mark.parametrize(
    "bad_app",
    [
        "",
        "   ",
        "cmd",
        "powershell.exe",
        "pwsh",
        "notepad & calc",
        "code; whoami",
        "../notepad.exe",
        r"C:\Windows\System32\calc.exe",
        "nonexistent-binary",
    ],
)
def test_open_or_focus_app_rejects_disallowed_or_malformed_apps(bad_app: str) -> None:
    user32 = _FakeWindowUser32()
    with pytest.raises(ValueError):
        open_or_focus_app(
            bad_app,
            user32=user32,
            window_enumerator=list,
            which_fn=lambda _n: None,
            env={},
        )


def test_open_or_focus_app_focuses_existing_window_and_restores_iconic() -> None:
    user32 = _FakeWindowUser32(iconic_hwnds={42})
    launched: list[Any] = []
    windows = [
        EnumeratedWindow(hwnd=11, title="Notepad", process_name="notepad.exe", is_visible=True),
        EnumeratedWindow(
            hwnd=42,
            title="Clea - Visual Studio Code",
            process_name="Code.exe",
            is_visible=True,
        ),
    ]

    res = open_or_focus_app(
        "vscode",
        user32=user32,
        window_enumerator=lambda: windows,
        popen_fn=lambda *a, **kw: launched.append((a, kw)),
    )

    assert res["ok"] is True
    assert res["status"] == "focused"
    assert res["app"] == "vscode"
    assert res["window_title"] == "Clea - Visual Studio Code"
    assert res["hwnd"] == 42
    assert user32.show_calls == [(42, SW_RESTORE)]
    assert user32.foreground_calls == [42]
    assert launched == []


def test_open_or_focus_app_launches_with_shell_false_and_valid_target_path(
    tmp_path: Path,
) -> None:
    user32 = _FakeWindowUser32()
    target_dir = tmp_path / "repo"
    target_dir.mkdir()
    popen_calls: list[tuple[Any, dict[str, Any]]] = []

    windows = [
        EnumeratedWindow(
            hwnd=42,
            title="Existing VS Code",
            process_name="Code.exe",
            is_visible=True,
        ),
    ]

    # When target_path is passed, launches instead of merely focusing existing window.
    res = open_or_focus_app(
        "code",
        str(target_dir),
        user32=user32,
        window_enumerator=lambda: windows,
        which_fn=lambda name: r"C:\VSCode\Code.exe" if name == "Code.exe" else None,
        popen_fn=lambda cmd, **kwargs: popen_calls.append((cmd, kwargs)),
        env={},
    )

    assert res["status"] == "launched"
    assert res["app"] == "vscode"
    assert res["executable"] == r"C:\VSCode\Code.exe"
    assert res["target_path"] == str(target_dir.resolve())
    assert len(popen_calls) == 1
    cmd, kwargs = popen_calls[0]
    assert cmd == [r"C:\VSCode\Code.exe", str(target_dir.resolve())]
    assert kwargs["shell"] is False

    # Rejects nonexistent target_path before launching anything.
    with pytest.raises(ValueError, match="does not exist"):
        open_or_focus_app(
            "code",
            str(tmp_path / "missing-folder"),
            user32=user32,
            window_enumerator=list,
            which_fn=lambda _n: r"C:\VSCode\Code.exe",
            popen_fn=lambda cmd, **kwargs: popen_calls.append((cmd, kwargs)),
            env={},
        )
    assert len(popen_calls) == 1


# --------------------------------------------------------------------------- 2. Volume & Media


@pytest.mark.parametrize(
    ("action", "expected_vk"),
    [
        ("mute", VK_VOLUME_MUTE),
        ("unmute", VK_VOLUME_MUTE),
        ("toggle_mute", VK_VOLUME_MUTE),
        ("volume_up", VK_VOLUME_UP),
        ("volume_down", VK_VOLUME_DOWN),
        ("play_pause", VK_MEDIA_PLAY_PAUSE),
        ("next_track", VK_MEDIA_NEXT_TRACK),
        ("previous_track", VK_MEDIA_PREV_TRACK),
    ],
)
def test_control_media_volume_virtual_key_actions(action: str, expected_vk: int) -> None:
    user32 = _FakeWindowUser32()
    res = control_media_volume(action, user32=user32)

    assert res == {"ok": True, "action": action, "level": None}
    assert user32.key_events == [
        (expected_vk, 0, KEYEVENTF_EXTENDEDKEY, 0),
        (expected_vk, 0, KEYEVENTF_EXTENDEDKEY | KEYEVENTF_KEYUP, 0),
    ]


def test_control_media_volume_set_volume_clamps_and_validates() -> None:
    recorded_levels: list[int] = []

    res_low = control_media_volume("set_volume", -25, volume_setter=recorded_levels.append)
    res_mid = control_media_volume("set_volume", 37, volume_setter=recorded_levels.append)
    res_high = control_media_volume("set_volume", 150, volume_setter=recorded_levels.append)

    assert res_low == {"ok": True, "action": "set_volume", "level": 0}
    assert res_mid == {"ok": True, "action": "set_volume", "level": 37}
    assert res_high == {"ok": True, "action": "set_volume", "level": 100}
    assert recorded_levels == [0, 37, 100]

    with pytest.raises(ValueError, match="required"):
        control_media_volume("set_volume", None, volume_setter=recorded_levels.append)

    with pytest.raises(ValueError, match="Unsupported"):
        control_media_volume("eject_disc", user32=_FakeWindowUser32())

    assert len(VALID_MEDIA_VOLUME_ACTIONS) == 9


# --------------------------------------------------------------------------- 3. Clipboard


def test_clipboard_write_and_read_roundtrip_and_truncation() -> None:
    clip = _FakeClipboardWin32()

    # Empty clipboard returns empty string without error.
    empty_res = read_clipboard(user32=clip, kernel32=clip)
    assert empty_res == {
        "ok": True,
        "text": "",
        "truncated": False,
        "char_count": 0,
        "total_chars": 0,
    }

    sample = "Hello Clea — Windows clipboard Unicode roundtrip!"
    w_res = write_clipboard(sample, user32=clip, kernel32=clip)
    assert w_res == {"ok": True, "char_count": len(sample)}
    assert clip.opened is False

    r_full = read_clipboard(max_chars=8000, user32=clip, kernel32=clip)
    assert r_full == {
        "ok": True,
        "text": sample,
        "truncated": False,
        "char_count": len(sample),
        "total_chars": len(sample),
    }

    r_capped = read_clipboard(max_chars=10, user32=clip, kernel32=clip)
    assert r_capped == {
        "ok": True,
        "text": sample[:10],
        "truncated": True,
        "char_count": 10,
        "total_chars": len(sample),
    }


# --------------------------------------------------------------------------- 4. Git Inspector


def test_inspect_git_workspace_parses_outputs_and_enforces_read_only(tmp_path: Path) -> None:
    main_repo = tmp_path / "Clea"
    main_repo.mkdir()
    (main_repo / ".git").mkdir()

    sibling_wt = tmp_path / "Clea-os-tools"
    sibling_wt.mkdir()
    (sibling_wt / ".git").write_text("gitdir: ../Clea/.git/worktrees/Clea-os-tools\n")

    recorded_cmds: list[tuple[list[str], str, bool]] = []

    def _fake_run(
        cmd: list[str],
        *,
        cwd: str,
        shell: bool,
        **_kwargs: Any,
    ) -> subprocess.CompletedProcess[str]:
        recorded_cmds.append((list(cmd), cwd, shell))
        sub = cmd[1]
        if sub == "rev-parse":
            out = "feat/windows-os-tools\n"
        elif sub == "status":
            out = (
                "## feat/windows-os-tools...origin/master [ahead 1]\n"
                "M  src/clea/core/protocol.py\n"
                " M src/clea/core/daemon.py\n"
                "MM src/clea/tools/windows_os.py\n"
                "?? tests/test_windows_os_tools.py\n"
            )
        elif sub == "worktree":
            out = (
                f"worktree {main_repo}\n"
                "HEAD 1111111111111111\n"
                "branch refs/heads/master\n\n"
                f"worktree {sibling_wt}\n"
                "HEAD 2222222222222222\n"
                "branch refs/heads/feat/windows-os-tools\n\n"
                "worktree C:/temp/detached\n"
                "HEAD 3333333333333333\n"
                "detached\n"
            )
        elif sub == "log":
            out = (
                "abc1234 Add Tier-1 Windows OS tools\n"
                "def5678 Previous master commit\n"
            )
        elif sub == "diff":
            if "--stat" in cmd:
                out = " src/clea/core/daemon.py | 12 +++++++-----\n"
            else:
                out = "diff --git a/x b/x\n+" + ("A" * (MAX_DIFF_CHARS + 500))
        else:
            raise AssertionError(f"Unexpected git command: {cmd}")
        return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

    res = inspect_git_workspace(
        str(Path("..") / "Clea-os-tools"),
        include_diff=True,
        diff_stat_only=True,
        default_root=main_repo,
        run_fn=_fake_run,
    )

    assert res["ok"] is True
    assert res["repo_path"] == str(sibling_wt.resolve())
    assert res["branch"] == "feat/windows-os-tools"
    assert res["is_clean"] is False
    assert res["staged"] == ["src/clea/core/protocol.py", "src/clea/tools/windows_os.py"]
    assert res["unstaged"] == ["src/clea/core/daemon.py", "src/clea/tools/windows_os.py"]
    assert res["untracked"] == ["tests/test_windows_os_tools.py"]
    assert len(res["worktrees"]) == 3
    assert res["worktrees"][1]["branch"] == "feat/windows-os-tools"
    assert res["worktrees"][2]["branch"] == "(detached)"
    assert res["recent_commits"] == [
        {
            "sha": "abc1234",
            "subject": "Add Tier-1 Windows OS tools",
            "oneline": "abc1234 Add Tier-1 Windows OS tools",
        },
        {
            "sha": "def5678",
            "subject": "Previous master commit",
            "oneline": "def5678 Previous master commit",
        },
    ]
    assert res["diff"] == " src/clea/core/daemon.py | 12 +++++++-----"
    assert res["diff_truncated"] is False
    assert all(shell is False for _, _, shell in recorded_cmds)
    assert all(cwd == str(sibling_wt.resolve()) for _, cwd, _ in recorded_cmds)

    # Full diff should cap at MAX_DIFF_CHARS when diff_stat_only=False.
    res_patch = inspect_git_workspace(
        include_diff=True,
        diff_stat_only=False,
        default_root=main_repo,
        run_fn=_fake_run,
    )
    assert res_patch["diff_truncated"] is True
    assert res_patch["diff"] is not None
    assert len(res_patch["diff"]) == MAX_DIFF_CHARS


def test_inspect_git_workspace_rejects_non_repo_paths(tmp_path: Path) -> None:
    not_a_repo = tmp_path / "plain-dir"
    not_a_repo.mkdir()

    with pytest.raises(ValueError, match="not a Git repository"):
        inspect_git_workspace(str(not_a_repo), default_root=tmp_path)

    with pytest.raises(ValueError, match="does not exist"):
        inspect_git_workspace(str(tmp_path / "missing"), default_root=tmp_path)


# --------------------------------------------------------------------------- 5. Voice -> Daemon E2E


async def test_voice_runner_to_daemon_e2e_for_all_os_and_git_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: dict[str, list[Any]] = {
        "open": [],
        "volume": [],
        "read_clip": [],
        "write_clip": [],
        "git": [],
        "select_clip": [],
        "open_url": [],
        "control_sys": [],
    }

    def _fake_open(app_name: str, target_path: str | None = None) -> dict[str, Any]:
        calls["open"].append((app_name, target_path))
        return {
            "ok": True,
            "status": "focused",
            "app": "vscode",
            "window_title": "Clea-os-tools - Visual Studio Code",
            "hwnd": 999,
            "executable": None,
            "target_path": target_path,
        }

    def _fake_volume(action: str, level: int | None = None) -> dict[str, Any]:
        calls["volume"].append((action, level))
        return {"ok": True, "action": action, "level": level}

    def _fake_read_clip(
        max_chars: int = 8000,
        include_history: bool = False,
        max_items: int = 10,
        include_image_bytes: bool = False,
    ) -> dict[str, Any]:
        calls["read_clip"].append(max_chars)
        return {
            "ok": True,
            "text": "copied text",
            "truncated": False,
            "char_count": 11,
            "total_chars": 11,
            "history_enabled": include_history,
            "total_items": 1 if include_history else 0,
            "items": [],
        }

    def _fake_write_clip(text: str) -> dict[str, Any]:
        calls["write_clip"].append(text)
        return {"ok": True, "char_count": len(text)}

    def _fake_select_clip(index: int) -> dict[str, Any]:
        calls["select_clip"].append(index)
        return {
            "ok": True,
            "success": True,
            "status": "selected",
            "index": index,
            "item_id": f"item-{index}",
            "message": f"Selected item {index}",
        }

    def _fake_open_url(url: str, browser: str | None = None) -> dict[str, Any]:
        calls["open_url"].append((url, browser))
        return {
            "ok": True,
            "success": True,
            "status": "opened",
            "url": url,
            "browser": browser,
            "message": f"Opened {url}",
        }

    def _fake_control_system(action: str, **kwargs: Any) -> dict[str, Any]:
        calls["control_sys"].append((action, kwargs))
        res: dict[str, Any] = {
            "ok": True,
            "success": True,
            "action": action,
            "status": "success",
        }
        for k in ("level", "step", "window_title", "hwnd", "page", "uri"):
            if k in kwargs and kwargs[k] is not None:
                res[k] = kwargs[k]
        return res

    def _fake_git(
        repo_path: str | None = None,
        include_diff: bool = False,
        diff_stat_only: bool = True,
    ) -> dict[str, Any]:
        calls["git"].append((repo_path, include_diff, diff_stat_only))
        return {
            "ok": True,
            "repo_path": r"C:\Users\Aditya Kumar\OneDrive\Desktop\Clea-os-tools",
            "branch": "feat/windows-os-tools",
            "is_clean": False,
            "status": "## feat/windows-os-tools\n M src/clea/core/daemon.py",
            "staged": [],
            "unstaged": ["src/clea/core/daemon.py"],
            "untracked": [],
            "worktrees": [
                {
                    "path": r"C:\Users\Aditya Kumar\OneDrive\Desktop\Clea-os-tools",
                    "head": "deadbeef",
                    "branch": "feat/windows-os-tools",
                }
            ],
            "recent_commits": [
                {
                    "sha": "deadbeef",
                    "subject": "Initial",
                    "oneline": "deadbeef Initial",
                }
            ],
            "diff": "daemon.py | 5 +++++" if include_diff else None,
            "diff_truncated": False,
        }

    import clea.tools.windows_os as windows_os_mod

    daemon_mod.windows_os = windows_os_mod
    monkeypatch.setattr(daemon_mod, "open_or_focus_app", _fake_open)
    monkeypatch.setattr(daemon_mod, "control_media_volume", _fake_volume)
    monkeypatch.setattr(daemon_mod, "read_clipboard", _fake_read_clip)
    monkeypatch.setattr(windows_os_mod, "read_clipboard", _fake_read_clip)
    monkeypatch.setattr(daemon_mod, "write_clipboard", _fake_write_clip)
    monkeypatch.setattr(daemon_mod, "select_clipboard_item", _fake_select_clip)
    monkeypatch.setattr(daemon_mod, "open_url", _fake_open_url)
    monkeypatch.setattr(daemon_mod, "control_system", _fake_control_system)
    monkeypatch.setattr(daemon_mod, "inspect_git_workspace", _fake_git)

    async with _running_voice_daemon_env(tmp_path) as (_daemon, _runner, backend):
        r1 = await _invoke_voice_tool(
            backend,
            call_id="c-open",
            name="open_or_focus_app",
            arguments={"app_name": "vscode"},
        )
        assert r1.is_error is False
        assert OsOpenOrFocusAppResult.model_validate(r1.response).status == "focused"
        assert calls["open"] == [("vscode", None)]

        # Clamps out-of-range volume levels 150 -> 100 before/through RPC.
        r2 = await _invoke_voice_tool(
            backend,
            call_id="c-vol",
            name="control_media_volume",
            arguments={"action": "set_volume", "level": 150},
        )
        assert r2.is_error is False
        vol_model = OsControlMediaVolumeResult.model_validate(r2.response)
        assert vol_model.action == "set_volume"
        assert vol_model.level == 100
        assert calls["volume"] == [("set_volume", 100)]

        r3 = await _invoke_voice_tool(
            backend,
            call_id="c-read",
            name="read_clipboard",
            arguments={"max_chars": 2048},
        )
        assert r3.is_error is False
        assert OsReadClipboardResult.model_validate(r3.response).text == "copied text"
        assert calls["read_clip"] == [2048]

        r4 = await _invoke_voice_tool(
            backend,
            call_id="c-write",
            name="write_clipboard",
            arguments={"text": "hello windows"},
        )
        assert r4.is_error is False
        assert OsWriteClipboardResult.model_validate(r4.response).char_count == len("hello windows")
        assert calls["write_clip"] == ["hello windows"]

        r5 = await _invoke_voice_tool(
            backend,
            call_id="c-git",
            name="inspect_git_workspace",
            arguments={"include_diff": True, "diff_stat_only": True},
        )
        assert r5.is_error is False
        git_model = WorkspaceGitInspectResult.model_validate(r5.response)
        assert git_model.branch == "feat/windows-os-tools"
        assert git_model.diff == "daemon.py | 5 +++++"
        assert calls["git"] == [(None, True, True)]

        r6 = await _invoke_voice_tool(
            backend,
            call_id="c-select-clip",
            name="select_clipboard_item",
            arguments={"index": 2},
        )
        assert r6.is_error is False
        sel_model = OsSelectClipboardItemResult.model_validate(r6.response)
        assert sel_model.status == "selected"
        assert sel_model.index == 2
        assert calls["select_clip"] == [2]

        r7 = await _invoke_voice_tool(
            backend,
            call_id="c-open-url",
            name="open_url",
            arguments={"url": "https://github.com", "browser": "chrome"},
        )
        assert r7.is_error is False
        url_model = OsOpenUrlResult.model_validate(r7.response)
        assert url_model.status == "opened"
        assert url_model.url == "https://github.com"
        assert url_model.browser == "chrome"
        assert calls["open_url"] == [("https://github.com", "chrome")]

        r8 = await _invoke_voice_tool(
            backend,
            call_id="c-sys-bright",
            name="control_system",
            arguments={"action": "set_brightness", "level": 75},
        )
        assert r8.is_error is False
        sys_b_model = OsControlSystemResult.model_validate(r8.response)
        assert sys_b_model.action == "set_brightness"
        assert sys_b_model.level == 75

        r9 = await _invoke_voice_tool(
            backend,
            call_id="c-sys-win",
            name="control_system",
            arguments={"action": "minimize_window", "window_title": "Visual Studio Code"},
        )
        assert r9.is_error is False
        sys_w_model = OsControlSystemResult.model_validate(r9.response)
        assert sys_w_model.action == "minimize_window"
        assert sys_w_model.window_title == "Visual Studio Code"

        r10 = await _invoke_voice_tool(
            backend,
            call_id="c-sys-set",
            name="control_system",
            arguments={"action": "open_settings", "page": "display"},
        )
        assert r10.is_error is False
        sys_s_model = OsControlSystemResult.model_validate(r10.response)
        assert sys_s_model.action == "open_settings"
        assert sys_s_model.page == "display"

        # Invalid arguments return tool error response cleanly.
        r_bad = await _invoke_voice_tool(
            backend,
            call_id="c-bad-vol",
            name="control_media_volume",
            arguments={"action": "set_volume"},
        )
        assert r_bad.is_error is True
        assert "level" in str(r_bad.response.get("error", ""))

        r_bad_sel = await _invoke_voice_tool(
            backend,
            call_id="c-bad-sel",
            name="select_clipboard_item",
            arguments={"index": -1},
        )
        assert r_bad_sel.is_error is True
        assert "non-negative" in str(r_bad_sel.response.get("error", ""))

        r_bad_url = await _invoke_voice_tool(
            backend,
            call_id="c-bad-url",
            name="open_url",
            arguments={"url": "   "},
        )
        assert r_bad_url.is_error is True
        assert "url" in str(r_bad_url.response.get("error", ""))

        r_bad_sys = await _invoke_voice_tool(
            backend,
            call_id="c-bad-sys",
            name="control_system",
            arguments={"action": "   "},
        )
        assert r_bad_sys.is_error is True
        assert "action" in str(r_bad_sys.response.get("error", ""))

    assert set(METHOD_RESULTS) >= {
        "os.open_or_focus_app",
        "os.control_media_volume",
        "os.read_clipboard",
        "os.write_clipboard",
        "os.select_clipboard_item",
        "os.open_url",
        "os.control_system",
        "workspace.git_inspect",
    }


async def test_daemon_direct_rpc_e2e_for_new_windows_os_methods(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from websockets.asyncio.client import connect

    from clea.core import auth

    calls: dict[str, list[Any]] = {
        "select": [],
        "open_url": [],
        "control": [],
    }

    def _fake_select(index: int) -> dict[str, Any]:
        calls["select"].append(index)
        return {"ok": True, "success": True, "status": "selected", "index": index}

    def _fake_url(url: str, browser: str | None = None) -> dict[str, Any]:
        calls["open_url"].append((url, browser))
        return {"ok": True, "success": True, "status": "opened", "url": url, "browser": browser}

    def _fake_control(action: str, **kwargs: Any) -> dict[str, Any]:
        calls["control"].append((action, kwargs))
        return {"ok": True, "success": True, "action": action, "status": "success", **kwargs}

    monkeypatch.setattr(daemon_mod, "select_clipboard_item", _fake_select)
    monkeypatch.setattr(daemon_mod, "open_url", _fake_url)
    monkeypatch.setattr(daemon_mod, "control_system", _fake_control)

    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()
    log_file = tmp_path / "logs" / "audit.jsonl"
    log_file.parent.mkdir(parents=True, exist_ok=True)
    reg = ToolRegistry(audit_logger=AuditLogger(log_file))
    daemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=_FakeProvider(),
        memory=mem,
        registry=reg,
    )
    await daemon.start()
    token = daemon.token

    try:
        async with connect(
            f"ws://127.0.0.1:{daemon.bound_port}",
            subprotocols=auth.client_subprotocols(token),
        ) as ws:
            # 1. os.select_clipboard_item
            req_sel = OsSelectClipboardItemRequest(
                params=OsSelectClipboardItemParams(index=3)
            )
            await ws.send(req_sel.model_dump_json())
            raw_sel = await asyncio.wait_for(ws.recv(), timeout=5.0)
            frame_sel = parse_frame(raw_sel)
            assert isinstance(frame_sel, ResponseFrame)
            assert frame_sel.id == req_sel.id
            assert frame_sel.error is None
            res_sel = OsSelectClipboardItemResult.model_validate(frame_sel.result)
            assert res_sel.status == "selected"
            assert res_sel.index == 3
            assert calls["select"] == [3]

            # 2. os.open_url
            req_url = OsOpenUrlRequest(
                params=OsOpenUrlParams(url="https://docs.python.org", browser="edge")
            )
            await ws.send(req_url.model_dump_json())
            raw_url = await asyncio.wait_for(ws.recv(), timeout=5.0)
            frame_url = parse_frame(raw_url)
            assert isinstance(frame_url, ResponseFrame)
            assert frame_url.id == req_url.id
            assert frame_url.error is None
            res_url = OsOpenUrlResult.model_validate(frame_url.result)
            assert res_url.status == "opened"
            assert res_url.url == "https://docs.python.org"
            assert res_url.browser == "edge"
            assert calls["open_url"] == [("https://docs.python.org", "edge")]

            # 3. os.control_system
            req_ctrl = OsControlSystemRequest(
                params=OsControlSystemParams(action="set_brightness", level=80)
            )
            await ws.send(req_ctrl.model_dump_json())
            raw_ctrl = await asyncio.wait_for(ws.recv(), timeout=5.0)
            frame_ctrl = parse_frame(raw_ctrl)
            assert isinstance(frame_ctrl, ResponseFrame)
            assert frame_ctrl.id == req_ctrl.id
            assert frame_ctrl.error is None
            res_ctrl = OsControlSystemResult.model_validate(frame_ctrl.result)
            assert res_ctrl.action == "set_brightness"
            assert res_ctrl.level == 80
            assert len(calls["control"]) == 1
    finally:
        await daemon.stop()


# ----------------------------------------------------------- 6. WinRT Clipboard History


def test_read_clipboard_winrt_text_and_image_items() -> None:
    history_json = json.dumps(
        {
            "history_enabled": True,
            "total_items": 2,
            "items": [
                {
                    "index": 0,
                    "id": "{guid-0}",
                    "timestamp": "2026-11-15T04:30:00Z",
                    "kind": "text",
                    "text": "WinRT text content",
                    "truncated": False,
                    "image": None,
                },
                {
                    "index": 1,
                    "id": "{guid-1}",
                    "timestamp": "2026-11-15T04:29:00Z",
                    "kind": "image",
                    "text": None,
                    "truncated": False,
                    "image": {
                        "width": 800,
                        "height": 600,
                        "format": "png",
                        "bytes": "iVBORw0KGgoAAAANSUhEUg==",
                    },
                },
            ],
        }
    )

    runner_calls: list[list[str]] = []

    def _mock_runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        runner_calls.append(cmd)
        return subprocess.CompletedProcess(cmd, 0, stdout=history_json, stderr="")

    res = read_clipboard(
        include_history=True,
        max_items=5,
        include_image_bytes=True,
        powershell_runner=_mock_runner,
    )

    assert res["ok"] is True
    assert res["text"] == "WinRT text content"
    assert res["truncated"] is False
    assert res["history_enabled"] is True
    assert res["total_items"] == 2
    assert len(res["items"]) == 2
    assert res["items"][0]["kind"] == "text"
    assert res["items"][1]["kind"] == "image"
    assert res["items"][1]["image"]["width"] == 800
    assert res["items"][1]["image"]["bytes"] == "iVBORw0KGgoAAAANSUhEUg=="

    assert len(runner_calls) == 1
    cmd_str = " ".join(runner_calls[0])
    assert "powershell.exe" in runner_calls[0][0]
    assert "-MaxItems 5" in cmd_str
    assert "-IncludeImageBytes" in cmd_str

    parsed_model = OsReadClipboardResult(**res)
    assert isinstance(parsed_model.text, str)
    assert parsed_model.text == "WinRT text content"


def test_read_clipboard_history_deserialization_live() -> None:
    """Verify live read_clipboard(include_history=True) produces str text
    and deserializes cleanly into OsReadClipboardResult.
    """
    res = read_clipboard(include_history=True)
    assert res["ok"] is True
    assert isinstance(res["text"], str)
    assert not isinstance(res["text"], list)
    if res.get("items"):
        for item in res["items"]:
            if item.get("text") is not None:
                assert isinstance(item["text"], str)
                assert not isinstance(item["text"], list)
    parsed = OsReadClipboardResult(**res)
    assert isinstance(parsed.text, str)


def test_read_clipboard_winrt_fallback_to_win32_on_error() -> None:
    clip = _FakeClipboardWin32("fallback text from win32")

    def _failing_runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="WinRT access denied")

    res = read_clipboard(
        include_history=True,
        user32=clip,
        kernel32=clip,
        powershell_runner=_failing_runner,
    )

    assert res["ok"] is True
    assert res["text"] == "fallback text from win32"
    assert res["history_enabled"] is False
    assert res["total_items"] == 0
    assert res["items"] == []


def test_read_clipboard_no_history_does_not_invoke_powershell() -> None:
    clip = _FakeClipboardWin32("classic text")
    called = False

    def _runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal called
        called = True
        return subprocess.CompletedProcess(cmd, 0, stdout="{}", stderr="")

    res = read_clipboard(
        include_history=False,
        user32=clip,
        kernel32=clip,
        powershell_runner=_runner,
    )
    assert res["ok"] is True
    assert res["text"] == "classic text"
    assert "history_enabled" not in res
    assert called is False


def test_select_clipboard_item_success_and_errors() -> None:
    def _mock_runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        if "-ItemIndex 2" in " ".join(cmd):
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout=json.dumps(
                    {"success": True, "status": "Success", "index": 2, "id": "guid-2"}
                ),
                stderr="",
            )
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Index out of range")

    res = select_clipboard_item(2, powershell_runner=_mock_runner)
    assert res["ok"] is True
    assert res["success"] is True
    assert res["index"] == 2
    assert res["item_id"] == "guid-2"
    assert res["status"] == "Success"

    with pytest.raises(RuntimeError, match="Failed to select clipboard item"):
        select_clipboard_item(99, powershell_runner=_mock_runner)

    with pytest.raises(ValueError, match="non-negative"):
        select_clipboard_item(-1, powershell_runner=_mock_runner)

    with pytest.raises(ValueError, match="integer"):
        select_clipboard_item("invalid", powershell_runner=_mock_runner)  # type: ignore[arg-type]


# ----------------------------------------------------------- 7. Dynamic App Discovery


def test_get_installed_apps_caching_and_sanitization() -> None:
    raw_catalog = [
        {"Name": "Spotify", "AppID": "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"},
        {"Name": "Calculator", "AppID": "Microsoft.WindowsCalculator_8wekyb3d8bbwe!App"},
        {"Name": "Command Prompt", "AppID": "cmd.exe"},
        {"Name": "PowerShell 7", "AppID": "pwsh.exe"},
    ]
    fetch_count = 0

    def _fetcher() -> list[dict[str, str]]:
        nonlocal fetch_count
        fetch_count += 1
        return raw_catalog

    apps = get_installed_apps(force_refresh=True, app_catalog_fetcher=_fetcher)
    assert len(apps) == 2
    assert {a["Name"] for a in apps} == {"Spotify", "Calculator"}
    assert fetch_count == 1

    # Second call returns cached list without invoking fetcher
    apps_cached = get_installed_apps(force_refresh=False, app_catalog_fetcher=_fetcher)
    assert apps_cached == apps
    assert fetch_count == 1

    # Force refresh calls fetcher again
    get_installed_apps(force_refresh=True, app_catalog_fetcher=_fetcher)
    assert fetch_count == 2


def test_find_matching_start_app() -> None:
    catalog = [
        {"Name": "Visual Studio Code", "AppID": "Microsoft.VisualStudioCode"},
        {"Name": "Spotify Music", "AppID": "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!App"},
        {"Name": "Blender 5.3", "AppID": "BlenderFoundation.Blender"},
        {"Name": "Calculator", "AppID": "Microsoft.WindowsCalculator"},
    ]

    m1 = find_matching_start_app("Spotify Music", catalog)
    assert m1 is not None and m1["Name"] == "Spotify Music"

    m2 = find_matching_start_app("Microsoft.WindowsCalculator", catalog)
    assert m2 is not None and m2["Name"] == "Calculator"

    m3 = find_matching_start_app("blender", catalog)
    assert m3 is not None and m3["Name"] == "Blender 5.3"

    m4 = find_matching_start_app("spotfy", catalog)
    assert m4 is not None and m4["Name"] == "Spotify Music"

    assert find_matching_start_app("nonexistent_unknown_xyz", catalog) is None
    assert find_matching_start_app("", catalog) is None


def test_open_or_focus_app_dynamic_start_apps_flow(tmp_path: Path) -> None:
    user32 = _FakeWindowUser32(iconic_hwnds={77})
    catalog = [
        {"Name": "Spotify", "AppID": "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"},
    ]
    popen_calls: list[list[str]] = []

    # 1. When app is already running, focuses window
    windows = [
        EnumeratedWindow(
            hwnd=77, title="Spotify Free", process_name="Spotify.exe", is_visible=True
        ),
    ]
    res_focus = open_or_focus_app(
        "spotify",
        user32=user32,
        window_enumerator=lambda: windows,
        app_catalog_fetcher=lambda: catalog,
        popen_fn=lambda cmd, **kw: popen_calls.append(cmd),
    )
    assert res_focus["ok"] is True
    assert res_focus["status"] == "focused"
    assert res_focus["app"].lower() == "spotify"
    assert res_focus["hwnd"] == 77
    assert user32.show_calls == [(77, SW_RESTORE)]
    assert user32.foreground_calls == [77]
    assert popen_calls == []

    # 2. When app is not running, launches via explorer.exe shell:AppsFolder\...
    res_launch = open_or_focus_app(
        "spotify",
        user32=user32,
        window_enumerator=list,
        which_fn=lambda _n: None,
        app_catalog_fetcher=lambda: catalog,
        popen_fn=lambda cmd, **kw: popen_calls.append(cmd),
    )
    assert res_launch["ok"] is True
    assert res_launch["status"] == "launched"
    assert res_launch["app"] == "Spotify"
    assert res_launch["executable"] == "explorer.exe"
    expected_app_id = "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"
    assert popen_calls == [["explorer.exe", f"shell:AppsFolder\\{expected_app_id}"]]


# --------------------------------------------------------------------------- 8. URL Launcher


def test_open_url_success_and_shortcuts() -> None:
    startfile_urls: list[str] = []
    popen_calls: list[list[str]] = []

    # Default browser via startfile
    res1 = open_url(
        "https://example.com/test",
        startfile_fn=startfile_urls.append,
    )
    assert res1["ok"] is True
    assert res1["success"] is True
    assert res1["url"] == "https://example.com/test"
    assert res1["browser"] == "default"
    assert startfile_urls == ["https://example.com/test"]

    # Site shortcut resolution: 'github' -> 'https://github.com'
    res2 = open_url("github", startfile_fn=startfile_urls.append)
    assert res2["url"] == "https://github.com"
    assert startfile_urls[-1] == "https://github.com"

    # Site shortcut resolution: 'docs' -> 'https://docs.google.com'
    res_docs = open_url("docs", startfile_fn=startfile_urls.append)
    assert res_docs["url"] == "https://docs.google.com"
    assert startfile_urls[-1] == "https://docs.google.com"

    # Site shortcut resolution: 'mail' -> 'https://mail.google.com'
    res_mail = open_url("mail", startfile_fn=startfile_urls.append)
    assert res_mail["url"] == "https://mail.google.com"
    assert startfile_urls[-1] == "https://mail.google.com"

    # Domain shorthand: 'docs.python.org' -> 'https://docs.python.org'
    res3 = open_url("docs.python.org", startfile_fn=startfile_urls.append)
    assert res3["url"] == "https://docs.python.org"
    assert startfile_urls[-1] == "https://docs.python.org"

    # Specified browser launch
    res4 = open_url(
        "https://clea.ai",
        browser="chrome",
        which_fn=lambda n: r"C:\Chrome\chrome.exe" if "chrome" in n else None,
        popen_fn=lambda cmd, **kw: popen_calls.append(cmd),
    )
    assert res4["ok"] is True
    assert res4["browser"] == "chrome"
    assert popen_calls == [[r"C:\Chrome\chrome.exe", "https://clea.ai"]]


@pytest.mark.parametrize(
    "bad_url",
    [
        "",
        "   ",
        "file:///C:/Windows/System32/cmd.exe",
        "javascript:alert(1)",
        "data:text/html,<script>alert(1)</script>",
        "cmd://something",
        "powershell://test",
        "http://",
        "https://",
        "https://example.com\nwith-newline",
        "cmd.exe /c calc",
        "powershell -c calc",
        r"C:\windows\notepad.exe",
        r"D:/photos/image.png",
        "C:calc.exe",
        "D:script.bat",
        "c:relative/path",
        r"\\server\share\calc.exe",
        r"//server/share/file",
        "/root/etc/passwd",
        r"\windows\system32",
        "notepad.exe notes.txt",
    ],
)
def test_open_url_rejects_dangerous_or_malformed_urls(bad_url: str) -> None:
    with pytest.raises(ValueError):
        open_url(bad_url, startfile_fn=lambda _u: None)


def test_open_url_disallowed_or_missing_browser() -> None:
    with pytest.raises(ValueError, match="Disallowed browser"):
        open_url("https://example.com", browser="cmd.exe")

    with pytest.raises(ValueError, match="could not be resolved"):
        open_url(
            "https://example.com",
            browser="nonexistent-browser-xyz",
            which_fn=lambda _n: None,
        )


# --------------------------------------------------------------------------- 9. System Controls


def test_control_system_brightness_controls() -> None:
    current = 65
    set_levels: list[int] = []

    def _getter() -> int:
        return current

    def _setter(lvl: int) -> None:
        nonlocal current
        current = lvl
        set_levels.append(lvl)

    # get_brightness
    res_get = control_system("get_brightness", wmi_brightness_getter=_getter)
    assert res_get == {"ok": True, "success": True, "action": "get_brightness", "level": 65}

    # set_brightness
    res_set = control_system(
        "set_brightness",
        level=80,
        wmi_brightness_getter=_getter,
        wmi_brightness_setter=_setter,
    )
    assert res_set == {"ok": True, "success": True, "action": "set_brightness", "level": 80}
    assert current == 80

    # step_brightness (+10 default for brightness_up)
    res_up = control_system(
        "brightness_up",
        wmi_brightness_getter=_getter,
        wmi_brightness_setter=_setter,
    )
    assert res_up["level"] == 90
    assert current == 90

    # step_brightness (-10 default for brightness_down)
    res_down = control_system(
        "brightness_down",
        wmi_brightness_getter=_getter,
        wmi_brightness_setter=_setter,
    )
    assert res_down["level"] == 80
    assert current == 80

    # step_brightness custom step with clamp
    res_step = control_system(
        "step_brightness",
        step=50,
        wmi_brightness_getter=_getter,
        wmi_brightness_setter=_setter,
    )
    assert res_step["level"] == 100
    assert current == 100


def test_control_system_window_actions() -> None:
    user32 = _FakeWindowUser32()
    user32.foreground_window = 101

    # minimize explicit hwnd
    res_min = control_system("minimize_window", hwnd=55, user32=user32)
    assert res_min == {"ok": True, "success": True, "action": "minimize_window", "hwnd": 55}
    assert (55, SW_MINIMIZE) in user32.show_calls

    # maximize foreground fallback
    res_max = control_system("maximize_window", user32=user32)
    assert res_max == {"ok": True, "success": True, "action": "maximize_window", "hwnd": 101}
    assert (101, SW_MAXIMIZE) in user32.show_calls

    # restore matching window by title
    windows = [
        EnumeratedWindow(hwnd=202, title="Document - WordPad", process_name="wordpad.exe"),
    ]
    res_restore = control_system(
        "restore_window",
        title="WordPad",
        user32=user32,
        window_enumerator=lambda: windows,
    )
    assert res_restore == {"ok": True, "success": True, "action": "restore_window", "hwnd": 202}
    assert (202, SW_RESTORE) in user32.show_calls

    # close window
    res_close = control_system("close_window", hwnd=202, user32=user32)
    assert res_close == {"ok": True, "success": True, "action": "close_window", "hwnd": 202}
    assert user32.post_messages == [(202, WM_CLOSE, 0, 0)]


def test_control_system_lock_and_settings() -> None:
    user32 = _FakeWindowUser32()
    opened_uris: list[str] = []

    # lock screen
    res_lock = control_system("lock_screen", user32=user32)
    assert res_lock == {"ok": True, "success": True, "action": "lock_screen"}
    assert user32.lock_workstation_calls == 1

    # open allowlisted settings
    res_set1 = control_system(
        "open_settings",
        page="display",
        startfile_fn=opened_uris.append,
    )
    assert res_set1 == {
        "ok": True,
        "success": True,
        "action": "open_settings",
        "uri": "ms-settings:display",
    }
    assert opened_uris == ["ms-settings:display"]

    res_set2 = control_system(
        "open_settings",
        uri="ms-settings:sound",
        startfile_fn=opened_uris.append,
    )
    assert res_set2 == {
        "ok": True,
        "success": True,
        "action": "open_settings",
        "uri": "ms-settings:sound",
    }

    # reject non-allowlisted settings
    with pytest.raises(ValueError, match="not in the allowlist"):
        control_system("open_settings", page="malicious_page", startfile_fn=opened_uris.append)

    # reject unsupported action
    with pytest.raises(ValueError, match="Unsupported system action"):
        control_system("destroy_universe")

    assert "get_brightness" in VALID_SYSTEM_ACTIONS
    assert "lock_screen" in VALID_SYSTEM_ACTIONS
    assert "open_settings" in VALID_SYSTEM_ACTIONS


def test_powershell_winrt_single_line_type_literals() -> None:
    expected_literal = (
        "[Windows.ApplicationModel.DataTransfer.Clipboard, "
        "Windows.ApplicationModel.DataTransfer, ContentType = WindowsRuntime]"
    )
    assert expected_literal in _POWERSHELL_READ_CLIPBOARD_HISTORY_SCRIPT
    assert expected_literal in _POWERSHELL_SELECT_CLIPBOARD_ITEM_SCRIPT

    # Verify no line break splits the WinRT type literal
    for script in (
        _POWERSHELL_READ_CLIPBOARD_HISTORY_SCRIPT,
        _POWERSHELL_SELECT_CLIPBOARD_ITEM_SCRIPT,
    ):
        for line in script.splitlines():
            if "Windows.ApplicationModel.DataTransfer.Clipboard" in line:
                assert "ContentType = WindowsRuntime]" in line


def test_get_installed_apps_preserves_apps_containing_sh_and_cmd() -> None:
    raw_catalog = [
        {"Name": "Adobe Photoshop 2024", "AppID": "Photoshop"},
        {"Name": "Microsoft Publisher", "AppID": "Microsoft.Publisher"},
        {"Name": "Wireshark", "AppID": "Wireshark"},
        {"Name": "ShareX", "AppID": "ShareX"},
        {"Name": "Genshin Impact", "AppID": "GenshinImpact"},
        {"Name": "Git GUI", "AppID": r"C:\Program Files\Git\cmd\git-gui.exe"},
        {"Name": "Command Prompt", "AppID": r"C:\Windows\System32\cmd.exe"},
        {"Name": "PowerShell 7", "AppID": "pwsh.exe"},
        {"Name": "Git Bash", "AppID": "bash.exe"},
        {"Name": "sh", "AppID": "sh"},
    ]
    apps = get_installed_apps(force_refresh=True, app_catalog_fetcher=lambda: raw_catalog)
    app_names = [a["Name"] for a in apps]
    assert "Adobe Photoshop 2024" in app_names
    assert "Microsoft Publisher" in app_names
    assert "Wireshark" in app_names
    assert "ShareX" in app_names
    assert "Genshin Impact" in app_names
    assert "Git GUI" in app_names
    assert "Command Prompt" not in app_names
    assert "PowerShell 7" not in app_names
    assert "Git Bash" not in app_names
    assert "sh" not in app_names


def test_control_system_window_title_window_action_and_extra_kwargs() -> None:
    user32 = _FakeWindowUser32()

    # Pass window_title (alias for title) with extra kwargs
    res1 = control_system(
        "minimize_window",
        window_title="Notepad",
        hwnd=77,
        user32=user32,
        extra_field="ignored",
    )
    assert res1["ok"] is True
    assert res1["hwnd"] == 77
    assert (77, SW_MINIMIZE) in user32.show_calls

    # Pass action="window" with window_action="maximize"
    res2 = control_system(
        "window",
        window_action="maximize",
        window_title="Notepad",
        hwnd=88,
        user32=user32,
    )
    assert res2["ok"] is True
    assert res2["action"] == "maximize_window"
    assert res2["hwnd"] == 88
    assert (88, SW_MAXIMIZE) in user32.show_calls

    # Pass action="window_action" with window_action="restore"
    res3 = control_system(
        "window_action",
        window_action="restore",
        window_title="Notepad",
        hwnd=99,
        user32=user32,
    )
    assert res3["ok"] is True
    assert res3["action"] == "restore_window"
    assert res3["hwnd"] == 99
    assert (99, SW_RESTORE) in user32.show_calls


def test_control_system_float_infinity_raises_value_error() -> None:
    with pytest.raises(ValueError, match="finite"):
        control_system(
            "set_brightness",
            level=float("inf"),
            wmi_brightness_setter=lambda _x: None,
        )

    with pytest.raises(ValueError, match="finite"):
        control_system(
            "set_brightness",
            level=float("-inf"),
            wmi_brightness_setter=lambda _x: None,
        )

    with pytest.raises(ValueError, match="finite"):
        control_system(
            "step_brightness",
            step=float("inf"),
            wmi_brightness_getter=lambda: 50,
            wmi_brightness_setter=lambda _x: None,
        )

    with pytest.raises(ValueError, match="finite"):
        control_system(
            "step_brightness",
            step=float("-inf"),
            wmi_brightness_getter=lambda: 50,
            wmi_brightness_setter=lambda _x: None,
        )


# ------------------------------------------------------------ UI Automation & GUI Input Tests


class _FakeInputUser32:
    """Minimal user32 double capturing cursor positioning and Win32 SendInput payloads."""

    def __init__(self, *, hwnd: int = 101) -> None:
        self.hwnd = hwnd
        self.cursor_moves: list[tuple[int, int]] = []
        self.sent_inputs: list[_INPUT] = []
        self.show_calls: list[tuple[int, int]] = []
        self.foreground_calls: list[int] = []

    def GetForegroundWindow(self) -> int:
        return self.hwnd

    def IsIconic(self, _hwnd: int) -> int:
        return 0

    def ShowWindow(self, hwnd: int, cmd: int) -> int:
        self.show_calls.append((hwnd, cmd))
        return 1

    def SetForegroundWindow(self, hwnd: int) -> int:
        self.foreground_calls.append(hwnd)
        return 1

    def SetCursorPos(self, x: int, y: int) -> int:
        self.cursor_moves.append((x, y))
        return 1

    def SendInput(self, count: int, ptr: Any, cb_size: int) -> int:
        assert cb_size == ctypes.sizeof(_INPUT)
        arr = ctypes.cast(ptr, ctypes.POINTER(_INPUT * count)).contents
        for idx in range(count):
            copied = _INPUT()
            ctypes.memmove(ctypes.byref(copied), ctypes.byref(arr[idx]), ctypes.sizeof(_INPUT))
            self.sent_inputs.append(copied)
        return count


def test_inspect_ui_elements_parses_powershell_json_and_clamps_max_elements() -> None:
    calls: list[tuple[list[str], dict[str, Any]]] = []

    def _mock_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((cmd, kwargs))
        payload = {
            "window_title": "Calculator",
            "count": 2,
            "elements": [
                {
                    "index": 0,
                    "name": "Display is 3628800",
                    "automation_id": "CalculatorResults",
                    "control_type": "Text",
                    "value": "3628800",
                    "x": 240,
                    "y": 120,
                },
                {
                    "index": 1,
                    "name": "One",
                    "automation_id": "num1Button",
                    "control_type": "Button",
                    "value": None,
                    "x": 110,
                    "y": 420,
                },
                {
                    "index": 2,
                    "name": "Zero",
                    "automation_id": "num0Button",
                    "control_type": "Button",
                    "value": None,
                    "x": 180,
                    "y": 480,
                },
            ],
        }
        return subprocess.CompletedProcess(cmd, 0, stdout=json.dumps(payload), stderr="")

    res = inspect_ui_elements(
        app_name="Calculator",
        max_elements=999,
        powershell_runner=_mock_runner,
    )
    assert res["ok"] is True
    assert res["window_title"] == "Calculator"
    assert res["count"] == 3
    assert res["elements"][0]["value"] == "3628800"
    assert res["elements"][1]["x"] == 110
    assert res["elements"][1]["y"] == 420

    assert len(calls) == 1
    cmd, kwargs = calls[0]
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == 5.0
    assert cmd[1:4] == ["-NoProfile", "-NonInteractive", "-Command"]
    assert "UIAutomationClient" in cmd[4]
    assert "UIAutomationTypes" in cmd[4]
    assert "-MaxElements 150" in cmd[4]

    # Lower-bound clamp to 1 truncates returned elements as well
    res_low = inspect_ui_elements(max_elements=0, powershell_runner=_mock_runner)
    assert "-MaxElements 1" in calls[1][0][4]
    assert res_low["count"] == 1

    # Verify PS script contains required UIA assemblies and control types
    for ctype in ("Button", "Edit", "MenuItem", "TabItem", "Hyperlink", "ListItem", "CheckBox"):
        assert ctype in _POWERSHELL_INSPECT_UI_SCRIPT
    assert "InvokePattern" in _POWERSHELL_CLICK_UI_SCRIPT


def test_inspect_ui_elements_validation_and_error_paths() -> None:
    with pytest.raises(ValueError, match="app_name"):
        inspect_ui_elements(app_name="   ")

    with pytest.raises(ValueError, match="max_elements"):
        inspect_ui_elements(max_elements=True)  # type: ignore[arg-type]

    def _failing_runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="UIA error")

    with pytest.raises(RuntimeError, match="UIA error"):
        inspect_ui_elements(powershell_runner=_failing_runner)

    def _timeout_runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=5.0)

    with pytest.raises(TimeoutError, match="timed out"):
        inspect_ui_elements(powershell_runner=_timeout_runner)

    with pytest.raises(TimeoutError, match="timed out"):
        click_ui_element("One", powershell_runner=_timeout_runner)

    def _bad_json_runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout="not-json", stderr="")

    with pytest.raises(RuntimeError, match="invalid JSON"):
        inspect_ui_elements(powershell_runner=_bad_json_runner)

    with pytest.raises(RuntimeError, match="invalid JSON"):
        click_ui_element("One", powershell_runner=_bad_json_runner)


def test_inspect_ui_elements_resolves_known_app_alias_hwnd() -> None:
    captured_cmds: list[list[str]] = []
    u32 = _FakeInputUser32()
    windows = [
        EnumeratedWindow(
            hwnd=909,
            title="Clea - Visual Studio Code",
            process_name="Code.exe",
            is_visible=True,
        )
    ]

    def _mock_runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured_cmds.append(cmd)
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps({"window_title": "Clea - Visual Studio Code", "elements": []}),
            stderr="",
        )

    res = inspect_ui_elements(
        app_name="vscode",
        powershell_runner=_mock_runner,
        user32=u32,
        window_enumerator=lambda: windows,
    )
    assert res["window_title"] == "Clea - Visual Studio Code"
    assert "-TargetHwnd 909" in captured_cmds[0][4]
    assert u32.foreground_calls == [909]


def test_gui_action_raises_on_win32_sendinput_or_setcursorpos_failure() -> None:
    class _FailingSendInputUser32(_FakeInputUser32):
        def SendInput(self, count: int, ptr: Any, cb_size: int) -> int:
            return 0

    class _FailingCursorUser32(_FakeInputUser32):
        def SetCursorPos(self, x: int, y: int) -> int:
            return 0

    with pytest.raises(RuntimeError, match="SendInput failed"):
        gui_action("click_at", x=10, y=20, user32=_FailingSendInputUser32())

    with pytest.raises(RuntimeError, match="SendInput failed"):
        gui_action("type_text", text="hi", user32=_FailingSendInputUser32())

    with pytest.raises(RuntimeError, match="SendInput failed"):
        gui_action("press_keys", keys=["enter"], user32=_FailingSendInputUser32())

    with pytest.raises(RuntimeError, match="SendInput failed"):
        gui_action("scroll", delta=-120, user32=_FailingSendInputUser32())

    with pytest.raises(RuntimeError, match="SetCursorPos"):
        gui_action("click_at", x=10, y=20, user32=_FailingCursorUser32())

    with pytest.raises(RuntimeError, match="SetCursorPos"):
        gui_action("scroll", x=10, y=20, delta=-120, user32=_FailingCursorUser32())


def test_click_ui_element_invoke_fallback_matching_and_errors() -> None:
    # 1. InvokePattern supported -> no mouse click needed
    u32_invoke = _FakeInputUser32()

    def _invoke_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs["shell"] is False
        assert kwargs["timeout"] == 5.0
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps(
                {
                    "found": True,
                    "invoked": True,
                    "name": "Equals",
                    "control_type": "Button",
                    "x": 320,
                    "y": 480,
                }
            ),
            stderr="",
        )

    res_inv = click_ui_element(
        "Equals",
        control_type="Button",
        app_name="Calculator",
        powershell_runner=_invoke_runner,
        user32=u32_invoke,
        window_enumerator=lambda: [
            EnumeratedWindow(
                hwnd=101,
                title="Calculator",
                process_name="ApplicationFrameHost.exe",
                is_visible=True,
            )
        ],
    )
    assert res_inv["status"] == "clicked"
    assert res_inv["name"] == "Equals"
    assert res_inv["control_type"] == "Button"
    assert res_inv["x"] == 320
    assert res_inv["y"] == 480
    assert res_inv["method"] == "invoke"
    assert u32_invoke.cursor_moves == []
    assert u32_invoke.sent_inputs == []

    # 2. InvokePattern unsupported -> SetCursorPos + SendInput left click fallback
    u32_click = _FakeInputUser32()

    def _no_invoke_runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps(
                {
                    "found": True,
                    "invoked": False,
                    "name": "History Tab",
                    "control_type": "TabItem",
                    "x": 150,
                    "y": 90,
                }
            ),
            stderr="",
        )

    res_clk = click_ui_element(
        "History",
        control_type="TabItem",
        powershell_runner=_no_invoke_runner,
        user32=u32_click,
    )
    assert res_clk["status"] == "clicked"
    assert res_clk["method"] == "click"
    assert u32_click.cursor_moves == [(150, 90)]
    assert len(u32_click.sent_inputs) == 2
    assert u32_click.sent_inputs[0].type == INPUT_MOUSE
    assert u32_click.sent_inputs[0].union.mi.dwFlags == MOUSEEVENTF_LEFTDOWN
    assert u32_click.sent_inputs[1].union.mi.dwFlags == MOUSEEVENTF_LEFTUP

    # 3. Exact match beats substring match in elements fallback & control_type filters
    u32_match = _FakeInputUser32()

    def _elements_runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps(
                {
                    "elements": [
                        {"name": "Play All", "control_type": "Button", "x": 10, "y": 20},
                        {"name": "Play", "control_type": "MenuItem", "x": 30, "y": 40},
                        {"name": "Play", "control_type": "Button", "x": 50, "y": 60},
                    ]
                }
            ),
            stderr="",
        )

    res_exact = click_ui_element("play", powershell_runner=_elements_runner, user32=u32_match)
    assert res_exact["x"] == 30
    assert res_exact["y"] == 40

    res_ct = click_ui_element(
        "play",
        control_type="Button",
        powershell_runner=_elements_runner,
        user32=u32_match,
    )
    assert res_ct["x"] == 50
    assert res_ct["y"] == 60

    # 4. Missing element raises ValueError listing available names
    def _missing_runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps({"found": False, "available": ["One", "Two", "Clear"]}),
            stderr="",
        )

    with pytest.raises(ValueError, match="One, Two, Clear"):
        click_ui_element("Factorial", powershell_runner=_missing_runner)


def test_gui_action_click_type_keys_scroll_and_wait() -> None:
    u32 = _FakeInputUser32()

    # click_at right click
    r_click = gui_action("click_at", x=400, y=300, button="right", clicks=1, user32=u32)
    assert r_click["status"] == "completed"
    assert r_click["clicks"] == 1
    assert u32.cursor_moves == [(400, 300)]
    assert [inp.union.mi.dwFlags for inp in u32.sent_inputs] == [
        MOUSEEVENTF_RIGHTDOWN,
        MOUSEEVENTF_RIGHTUP,
    ]

    # click_at double click
    u32.cursor_moves.clear()
    u32.sent_inputs.clear()
    r_dbl = gui_action("click_at", x=10, y=20, button="double", user32=u32)
    assert r_dbl["clicks"] == 2
    assert [inp.union.mi.dwFlags for inp in u32.sent_inputs] == [
        MOUSEEVENTF_LEFTDOWN,
        MOUSEEVENTF_LEFTUP,
        MOUSEEVENTF_LEFTDOWN,
        MOUSEEVENTF_LEFTUP,
    ]

    # click_at invalid coords
    with pytest.raises(ValueError, match="non-negative"):
        gui_action("click_at", x=-1, y=10, user32=u32)

    # type_text Unicode SendInput
    u32.sent_inputs.clear()
    r_type = gui_action("type_text", text="10!", user32=u32)
    assert r_type["char_count"] == 3
    assert len(u32.sent_inputs) == 6
    assert all(inp.type == INPUT_KEYBOARD for inp in u32.sent_inputs)
    scans = [(inp.union.ki.wScan, inp.union.ki.dwFlags) for inp in u32.sent_inputs]
    assert scans == [
        (ord("1"), KEYEVENTF_UNICODE),
        (ord("1"), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
        (ord("0"), KEYEVENTF_UNICODE),
        (ord("0"), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
        (ord("!"), KEYEVENTF_UNICODE),
        (ord("!"), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
    ]

    with pytest.raises(ValueError, match="non-empty"):
        gui_action("type_text", text="", user32=u32)
    with pytest.raises(ValueError, match="2000"):
        gui_action("type_text", text="x" * 2001, user32=u32)

    # press_keys chord + blocked combos
    u32.sent_inputs.clear()
    r_keys = gui_action("press_keys", keys=["ctrl", "l"], user32=u32)
    assert r_keys["keys"] == ["ctrl", "l"]
    vk_seq = [(inp.union.ki.wVk, inp.union.ki.dwFlags) for inp in u32.sent_inputs]
    assert vk_seq == [
        (0x11, 0),
        (0x4C, 0),
        (0x4C, KEYEVENTF_KEYUP),
        (0x11, KEYEVENTF_KEYUP),
    ]

    for blocked in (
        ["alt", "f4"],
        ["ctrl", "alt", "delete"],
        ["control", "alt", "del"],
        ["ctrl", "shift", "escape"],
        ["ctrl", "shift", "esc"],
    ):
        with pytest.raises(ValueError, match="Blocked dangerous key combination"):
            gui_action("press_keys", keys=blocked, user32=u32)

    with pytest.raises(ValueError, match="disallowed key"):
        gui_action("press_keys", keys=["win"], user32=u32)

    # scroll
    u32.cursor_moves.clear()
    u32.sent_inputs.clear()
    r_scroll = gui_action("scroll", x=500, y=600, delta=-360, user32=u32)
    assert r_scroll["delta"] == -360
    assert u32.cursor_moves == [(500, 600)]
    assert len(u32.sent_inputs) == 1
    assert u32.sent_inputs[0].union.mi.dwFlags == MOUSEEVENTF_WHEEL
    assert u32.sent_inputs[0].union.mi.mouseData == (-360 & 0xFFFFFFFF)

    # wait clamping
    slept: list[float] = []
    assert gui_action("wait", seconds=0.01, sleep_fn=slept.append)["seconds"] == 0.1
    assert gui_action("wait", seconds=99.0, sleep_fn=slept.append)["seconds"] == 10.0
    assert slept == [0.1, 10.0]


@pytest.mark.asyncio
async def test_voice_runner_to_daemon_e2e_gui_multistep_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    inspect_calls: list[tuple[str | None, int]] = []
    click_calls: list[tuple[str, str | None, str | None]] = []
    gui_calls: list[tuple[str, tuple[Any, ...]]] = []

    def _fake_inspect(app_name: str | None = None, max_elements: int = 60) -> dict[str, Any]:
        inspect_calls.append((app_name, max_elements))
        return {
            "ok": True,
            "window_title": app_name or "Calculator",
            "count": 1,
            "elements": [
                {
                    "index": 0,
                    "name": "Display is 3628800",
                    "automation_id": "CalculatorResults",
                    "control_type": "Text",
                    "value": "3628800",
                    "x": 200,
                    "y": 100,
                }
            ],
        }

    def _fake_click(
        name: str,
        control_type: str | None = None,
        app_name: str | None = None,
    ) -> dict[str, Any]:
        click_calls.append((name, control_type, app_name))
        return {
            "ok": True,
            "status": "clicked",
            "name": name,
            "control_type": control_type or "Button",
            "x": 150,
            "y": 350,
            "method": "invoke",
        }

    def _fake_gui(
        action: str,
        x: int | None = None,
        y: int | None = None,
        text: str | None = None,
        keys: list[str] | None = None,
        clicks: int = 1,
        button: str = "left",
        delta: int = 0,
        seconds: float = 1.0,
    ) -> dict[str, Any]:
        gui_calls.append((action, (x, y, text, keys, clicks, button, delta, seconds)))
        return gui_action(
            action,
            x=x,
            y=y,
            text=text,
            keys=keys,
            clicks=clicks,
            button=button,
            delta=delta,
            seconds=seconds,
            user32=_FakeInputUser32(),
            sleep_fn=lambda _s: None,
        )

    monkeypatch.setattr(daemon_mod, "inspect_ui_elements", _fake_inspect)
    monkeypatch.setattr(daemon_mod, "click_ui_element", _fake_click)
    monkeypatch.setattr(daemon_mod, "gui_action", _fake_gui)

    async with _running_voice_daemon_env(tmp_path) as (_daemon, _runner, backend):
        r_wait = await _invoke_voice_tool(
            backend,
            call_id="c-wait",
            name="gui_action",
            arguments={"action": "wait", "seconds": 1.5},
        )
        assert r_wait.is_error is False
        assert r_wait.response["seconds"] == 1.5

        r_insp = await _invoke_voice_tool(
            backend,
            call_id="c-insp",
            name="inspect_ui_elements",
            arguments={"app_name": "Calculator", "max_elements": 40},
        )
        assert r_insp.is_error is False
        parsed_insp = OsInspectUiElementsResult(**r_insp.response)
        assert parsed_insp.window_title == "Calculator"
        assert parsed_insp.elements[0].value == "3628800"
        assert inspect_calls == [("Calculator", 40)]

        r_clk = await _invoke_voice_tool(
            backend,
            call_id="c-clk",
            name="click_ui_element",
            arguments={"name": "Factorial", "control_type": "Button", "app_name": "Calculator"},
        )
        assert r_clk.is_error is False
        parsed_clk = OsClickUiElementResult(**r_clk.response)
        assert parsed_clk.status == "clicked"
        assert click_calls == [("Factorial", "Button", "Calculator")]

        r_type = await _invoke_voice_tool(
            backend,
            call_id="c-type",
            name="gui_action",
            arguments={"action": "type_text", "text": "10!"},
        )
        assert r_type.is_error is False
        assert OsGuiActionResult(**r_type.response).char_count == 3

        # Blocked chord flows back as tool error
        r_blocked = await _invoke_voice_tool(
            backend,
            call_id="c-blocked",
            name="gui_action",
            arguments={"action": "press_keys", "keys": ["alt", "f4"]},
        )
        assert r_blocked.is_error is True
        assert "Blocked dangerous key combination" in str(r_blocked.response.get("error", ""))

        # Non-finite seconds is rejected cleanly before RPC serialization
        r_inf = await _invoke_voice_tool(
            backend,
            call_id="c-inf",
            name="gui_action",
            arguments={"action": "wait", "seconds": float("inf")},
        )
        assert r_inf.is_error is True
        assert "finite" in str(r_inf.response.get("error", ""))

    assert set(METHOD_RESULTS) >= {
        "os.inspect_ui_elements",
        "os.click_ui_element",
        "os.gui_action",
    }


# --------------------------------------------------------------------- 5. Semantic Snapshot & Refs


def _sample_mixed_uia_payload() -> dict[str, Any]:
    return {
        "window_title": "Search Portal - Browser",
        "hwnd": 777,
        "count": 8,
        "elements": [
            {
                "name": "Address and search bar",
                "automation_id": "urlBar",
                "control_type": "Edit",
                "value": "https://example.com",
                "enabled": True,
                "focused": False,
                "bounds": {"x": 120, "y": 40, "width": 600, "height": 32},
                "center": {"cx": 420, "cy": 56},
            },
            {
                "name": "   ",
                "automation_id": "emptySpacer",
                "control_type": "Text",
                "value": None,
                "enabled": True,
                "focused": False,
                "bounds": {"x": 0, "y": 0, "width": 10, "height": 10},
                "center": {"cx": 5, "cy": 5},
            },
            {
                "name": "Status: Ready",
                "automation_id": "statusLabel",
                "control_type": "Text",
                "value": None,
                "enabled": True,
                "focused": False,
                "bounds": {"x": 20, "y": 100, "width": 200, "height": 20},
                "center": {"cx": 120, "cy": 110},
            },
            {
                "name": "Query",
                "automation_id": "searchInput",
                "control_type": "Edit",
                "value": "",
                "enabled": True,
                "focused": True,
                "bounds": {"x": 200, "y": 200, "width": 300, "height": 36},
                "center": {"cx": 350, "cy": 218},
            },
            {
                "name": "Submit",
                "automation_id": "submitBtn",
                "control_type": "Button",
                "value": None,
                "enabled": True,
                "focused": False,
                "bounds": {"x": 520, "y": 200, "width": 90, "height": 36},
                "center": {"cx": 565, "cy": 218},
            },
            {
                "name": "Disabled Action",
                "automation_id": "disabledBtn",
                "control_type": "Button",
                "value": None,
                "enabled": False,
                "focused": False,
                "bounds": {"x": 620, "y": 200, "width": 90, "height": 36},
                "center": {"cx": 665, "cy": 218},
            },
            {
                "name": "Docs",
                "automation_id": "",
                "control_type": "Hyperlink",
                "value": None,
                "enabled": True,
                "focused": False,
                "bounds": {"x": 40, "y": 260, "width": 60, "height": 20},
                "center": {"cx": 70, "cy": 270},
            },
            {
                "name": "Settings",
                "automation_id": "settingsTab",
                "control_type": "TabItem",
                "value": None,
                "enabled": True,
                "focused": False,
                "bounds": {"x": 120, "y": 260, "width": 80, "height": 24},
                "center": {"cx": 160, "cy": 272},
            },
            {
                "name": "Auto-sync",
                "automation_id": "syncCheck",
                "control_type": "CheckBox",
                "value": None,
                "enabled": True,
                "focused": False,
                "bounds": {"x": 220, "y": 260, "width": 100, "height": 24},
                "center": {"cx": 270, "cy": 272},
            },
            {
                "name": "Region",
                "automation_id": "regionCombo",
                "control_type": "ComboBox",
                "value": "us-east",
                "enabled": True,
                "focused": False,
                "bounds": {"x": 340, "y": 260, "width": 120, "height": 24},
                "center": {"cx": 400, "cy": 272},
            },
            {
                "name": "Mode A",
                "automation_id": "modeRadio",
                "control_type": "RadioButton",
                "value": None,
                "enabled": True,
                "focused": False,
                "bounds": {"x": 480, "y": 260, "width": 80, "height": 24},
                "center": {"cx": 520, "cy": 272},
            },
            {
                "name": "File",
                "automation_id": "fileMenu",
                "control_type": "MenuItem",
                "value": None,
                "enabled": True,
                "focused": False,
                "bounds": {"x": 10, "y": 10, "width": 40, "height": 20},
                "center": {"cx": 30, "cy": 20},
            },
            {
                "name": "Item One",
                "automation_id": "item1",
                "control_type": "ListItem",
                "value": None,
                "enabled": True,
                "focused": False,
                "bounds": {"x": 10, "y": 300, "width": 100, "height": 20},
                "center": {"cx": 60, "cy": 310},
            },
        ],
    }


def test_ui_snapshot_parses_mixed_controls_filters_and_populates_ref_store() -> None:
    _UI_REF_STORE.clear()
    calls: list[tuple[list[str], dict[str, Any]]] = []
    u32 = _FakeInputUser32(hwnd=777)

    def _mock_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        calls.append((cmd, kwargs))
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps(_sample_mixed_uia_payload()),
            stderr="",
        )

    snap = ui_snapshot(
        app_name="Browser",
        max_elements=999,
        interactive_only=False,
        powershell_runner=_mock_runner,
        user32=u32,
        window_enumerator=lambda: [
            EnumeratedWindow(
                hwnd=777,
                title="Search Portal - Browser",
                process_name="msedge.exe",
                is_visible=True,
            )
        ],
    )

    assert snap["snapshot_id"].startswith("snap_")
    assert snap["window_title"] == "Search Portal - Browser"
    # Empty-whitespace Text element is dropped; 12 remaining elements
    assert snap["element_count"] == 12
    assert len(snap["elements"]) == 12

    # Verify PowerShell invocation safety & clamping
    cmd, kwargs = calls[0]
    assert kwargs["shell"] is False
    assert kwargs["timeout"] == 5.0
    assert cmd[1:4] == ["-NoProfile", "-NonInteractive", "-Command"]
    assert "UIAutomationClient" in cmd[4]
    assert "UIAutomationTypes" in cmd[4]
    assert "RangeValuePattern" in _POWERSHELL_SNAPSHOT_UI_SCRIPT
    assert "-MaxElements 150" in cmd[4]
    assert "-InteractiveOnly 0" in cmd[4]
    assert "-TargetHwnd 777" in cmd[4]

    # Verify compact LLM schema (no raw coordinates or automation_id leaked)
    for idx, el in enumerate(snap["elements"], start=1):
        assert el["ref"] == f"e{idx}"
        assert set(el.keys()) == {"ref", "role", "name", "value", "enabled", "focused"}

    roles_by_ref = {el["ref"]: el["role"] for el in snap["elements"]}
    assert roles_by_ref == {
        "e1": "input",
        "e2": "text",
        "e3": "input",
        "e4": "button",
        "e5": "button",  # disabled button still included when interactive_only=False
        "e6": "link",
        "e7": "tab",
        "e8": "checkbox",
        "e9": "combo",
        "e10": "radio",
        "e11": "menu_item",
        "e12": "list_item",
    }
    assert snap["elements"][2]["focused"] is True
    assert snap["elements"][4]["enabled"] is False
    assert snap["elements"][8]["value"] == "us-east"

    # Verify internal _UiRefStore holds bounds, center, and automation_id
    sid, wtitle, hwnd, entry = _UI_REF_STORE.resolve("e4")
    assert sid == snap["snapshot_id"]
    assert wtitle == "Search Portal - Browser"
    assert hwnd == 777
    assert entry["bounds"] == {"x": 520, "y": 200, "width": 90, "height": 36}
    assert entry["center"] == {"cx": 565, "cy": 218}
    assert entry["automation_id"] == "submitBtn"

    # interactive_only=True drops Text readouts AND disabled controls
    snap_interactive = ui_snapshot(
        max_elements=0,
        interactive_only=True,
        powershell_runner=_mock_runner,
    )
    assert "-MaxElements 1" in calls[1][0][4]
    assert "-InteractiveOnly 1" in calls[1][0][4]
    assert snap_interactive["element_count"] == 1
    assert snap_interactive["elements"][0]["role"] == "input"

    snap_interactive_all = ui_snapshot(
        max_elements=80,
        interactive_only=True,
        powershell_runner=_mock_runner,
    )
    assert snap_interactive_all["element_count"] == 10
    assert all(el["role"] != "text" and el["enabled"] for el in snap_interactive_all["elements"])


def test_ui_snapshot_validation_and_error_paths() -> None:
    with pytest.raises(ValueError, match="app_name"):
        ui_snapshot(app_name="   ")
    with pytest.raises(ValueError, match="max_elements"):
        ui_snapshot(max_elements=True)  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="interactive_only"):
        ui_snapshot(interactive_only="yes")  # type: ignore[arg-type]

    def _timeout_runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        raise subprocess.TimeoutExpired(cmd=cmd, timeout=5.0)

    with pytest.raises(TimeoutError, match="timed out"):
        ui_snapshot(powershell_runner=_timeout_runner)

    def _fail_runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Snapshot failure")

    with pytest.raises(RuntimeError, match="Snapshot failure"):
        ui_snapshot(powershell_runner=_fail_runner)

    def _bad_json_runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(cmd, 0, stdout="{bad", stderr="")

    with pytest.raises(RuntimeError, match="invalid JSON"):
        ui_snapshot(powershell_runner=_bad_json_runner)


def test_ui_act_click_patterns_and_coordinate_fallback() -> None:
    _UI_REF_STORE.clear()
    with pytest.raises(ValueError, match="no active snapshot"):
        ui_act("e1")

    def _snap_runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps(_sample_mixed_uia_payload()),
            stderr="",
        )

    ui_snapshot(powershell_runner=_snap_runner)

    # 1. Click via native UIA pattern (InvokePattern / SelectionItemPattern / TogglePattern / etc.)
    act_cmds: list[str] = []
    u32_uia = _FakeInputUser32(hwnd=777)

    def _invoke_runner(cmd: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
        assert kwargs["shell"] is False
        assert kwargs["timeout"] == 5.0
        act_cmds.append(cmd[4])
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps(
                {"found": True, "acted": True, "method": "invoke", "cx": 565, "cy": 218}
            ),
            stderr="",
        )

    res_invoke = ui_act("@E4", action="click", powershell_runner=_invoke_runner, user32=u32_uia)
    assert res_invoke["ok"] is True
    assert res_invoke["ref"] == "e4"
    assert res_invoke["method"] == "invoke"
    assert u32_uia.cursor_moves == []
    assert u32_uia.sent_inputs == []
    expected_aid_b64 = base64.b64encode(b"submitBtn").decode("ascii")
    expected_name_b64 = base64.b64encode(b"Submit").decode("ascii")
    assert f"-AutomationIdBase64 '{expected_aid_b64}'" in act_cmds[0]
    assert f"-NameBase64 '{expected_name_b64}'" in act_cmds[0]
    assert "-ExpectedX 520 -ExpectedY 200 -ExpectedW 90 -ExpectedH 36" in act_cmds[0]

    for pat in (
        "InvokePattern",
        "SelectionItemPattern",
        "TogglePattern",
        "ExpandCollapsePattern",
        "ValuePattern",
    ):
        assert pat in _POWERSHELL_UI_ACT_SCRIPT

    # 2. Click fallback to Win32 SetCursorPos(cx, cy) + left click when UIA patterns unsupported
    u32_fallback = _FakeInputUser32(hwnd=777)

    def _no_pattern_runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps({"found": True, "acted": False, "method": "", "cx": 72, "cy": 271}),
            stderr="",
        )

    res_fb = ui_act("e6", action="click", powershell_runner=_no_pattern_runner, user32=u32_fallback)
    assert res_fb["method"] == "win32_click"
    assert res_fb["x"] == 72
    assert res_fb["y"] == 271
    assert u32_fallback.cursor_moves == [(72, 271)]
    assert [inp.union.mi.dwFlags for inp in u32_fallback.sent_inputs] == [
        MOUSEEVENTF_LEFTDOWN,
        MOUSEEVENTF_LEFTUP,
    ]

    # 3. double_click and right_click use Win32 SendInput directly at stored center
    u32_mouse = _FakeInputUser32(hwnd=777)
    res_dbl = ui_act("e4", action="double_click", user32=u32_mouse)
    assert res_dbl["method"] == "win32_double_click"
    assert u32_mouse.cursor_moves == [(565, 218)]
    assert [inp.union.mi.dwFlags for inp in u32_mouse.sent_inputs] == [
        MOUSEEVENTF_LEFTDOWN,
        MOUSEEVENTF_LEFTUP,
        MOUSEEVENTF_LEFTDOWN,
        MOUSEEVENTF_LEFTUP,
    ]

    u32_mouse.cursor_moves.clear()
    u32_mouse.sent_inputs.clear()
    res_right = ui_act("@e4", action="right_click", user32=u32_mouse)
    assert res_right["method"] == "win32_right_click"
    assert u32_mouse.cursor_moves == [(565, 218)]
    assert [inp.union.mi.dwFlags for inp in u32_mouse.sent_inputs] == [
        MOUSEEVENTF_RIGHTDOWN,
        MOUSEEVENTF_RIGHTUP,
    ]


def test_ui_act_set_value_type_clear_pacing_and_ref_errors() -> None:
    _UI_REF_STORE.clear()

    def _snap_runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps(_sample_mixed_uia_payload()),
            stderr="",
        )

    ui_snapshot(powershell_runner=_snap_runner)

    # 1. set_value via UIA ValuePattern -> instant, no mouse/keyboard Win32 injection
    u32_vp = _FakeInputUser32(hwnd=777)

    def _vp_runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        expected_val_b64 = base64.b64encode(b"hello world").decode("ascii")
        assert f"-ValueBase64 '{expected_val_b64}'" in cmd[4]
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps(
                {"found": True, "acted": True, "method": "value_pattern", "cx": 350, "cy": 218}
            ),
            stderr="",
        )

    res_vp = ui_act(
        "e3",
        action="set_value",
        value="hello world",
        powershell_runner=_vp_runner,
        user32=u32_vp,
    )
    assert res_vp["method"] == "value_pattern"
    assert res_vp["char_count"] == 11
    assert u32_vp.cursor_moves == []
    assert u32_vp.sent_inputs == []

    # 2. set_value fallback when ValuePattern unsupported -> click + Ctrl+A + Backspace + paced
    u32_fb = _FakeInputUser32(hwnd=777)
    sleeps_fb: list[float] = []

    def _no_vp_runner(cmd: list[str], **_kwargs: Any) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps({"found": False, "acted": False, "method": "", "cx": 350, "cy": 218}),
            stderr="",
        )

    res_fb = ui_act(
        "@e3",
        action="set_value",
        value="ab",
        interval_ms=999,  # clamps to 200ms
        powershell_runner=_no_vp_runner,
        user32=u32_fb,
        sleep_fn=sleeps_fb.append,
    )
    assert res_fb["method"] == "keyboard_fallback"
    assert res_fb["interval_ms"] == 200
    assert u32_fb.cursor_moves == [(350, 218)]
    assert sleeps_fb == [0.2]
    # Sequence: 2 mouse (click), 4 key (ctrl+a), 2 key (backspace), 4 key ('a','b' Unicode)
    assert [inp.type for inp in u32_fb.sent_inputs] == [
        INPUT_MOUSE,
        INPUT_MOUSE,
        *([INPUT_KEYBOARD] * 10),
    ]
    vk_and_scans = [
        (inp.union.ki.wVk, inp.union.ki.wScan, inp.union.ki.dwFlags)
        for inp in u32_fb.sent_inputs[2:]
    ]
    assert vk_and_scans == [
        (0x11, 0, 0),
        (0x41, 0, 0),
        (0x41, 0, KEYEVENTF_KEYUP),
        (0x11, 0, KEYEVENTF_KEYUP),
        (0x08, 0, 0),
        (0x08, 0, KEYEVENTF_KEYUP),
        (0, ord("a"), KEYEVENTF_UNICODE),
        (0, ord("a"), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
        (0, ord("b"), KEYEVENTF_UNICODE),
        (0, ord("b"), KEYEVENTF_UNICODE | KEYEVENTF_KEYUP),
    ]

    # 3. type action -> focuses element and sends paced Unicode without Ctrl+A/Backspace
    u32_type = _FakeInputUser32(hwnd=777)
    sleeps_type: list[float] = []
    res_type = ui_act(
        "E3",
        action="type",
        value="xyz",
        interval_ms=15,
        user32=u32_type,
        sleep_fn=sleeps_type.append,
    )
    assert res_type["method"] == "win32_type"
    assert res_type["char_count"] == 3
    assert sleeps_type == [0.015, 0.015]
    assert u32_type.cursor_moves == [(350, 218)]
    assert len(u32_type.sent_inputs) == 8  # 2 mouse + 6 unicode key events

    # 4. clear action -> focuses element and sends Ctrl+A + Backspace
    u32_clear = _FakeInputUser32(hwnd=777)
    res_clear = ui_act("e3", action="clear", user32=u32_clear)
    assert res_clear["method"] == "win32_clear"
    assert u32_clear.cursor_moves == [(350, 218)]
    assert len(u32_clear.sent_inputs) == 8  # 2 mouse + 4 ctrl+a + 2 backspace

    # 5. Unknown/malformed ref and parameter errors
    for bad_ref in ("e999", "@e999", "@@e3", "@", ""):
        with pytest.raises(ValueError, match="ref"):
            ui_act(bad_ref, action="click")

    with pytest.raises(ValueError, match="Unsupported ui_act action"):
        ui_act("e3", action="hover")
    with pytest.raises(ValueError, match="set_value"):
        ui_act("e3", action="set_value", value=None)
    with pytest.raises(ValueError, match="type"):
        ui_act("e3", action="type", value="")
    with pytest.raises(ValueError, match="interval_ms"):
        ui_act("e3", action="type", value="a", interval_ms=True)  # type: ignore[arg-type]


def test_gui_action_type_text_pacing_and_ref_store_thread_safety() -> None:
    u32 = _FakeInputUser32()
    sleeps: list[float] = []
    res = gui_action("type_text", text="abc", interval_ms=12, user32=u32, sleep_fn=sleeps.append)
    assert res["char_count"] == 3
    assert res["interval_ms"] == 12
    assert sleeps == [0.012, 0.012]
    assert len(u32.sent_inputs) == 6

    # Zero pacing sends in one batch with no sleeps
    sleeps.clear()
    u32.sent_inputs.clear()
    res_fast = gui_action(
        "type_text",
        text="abc",
        interval_ms=-10,
        user32=u32,
        sleep_fn=sleeps.append,
    )
    assert res_fast["interval_ms"] == 0
    assert sleeps == []
    assert len(u32.sent_inputs) == 6

    # Concurrent update & resolve smoke check on _UI_REF_STORE
    errors: list[Exception] = []

    def _writer(idx: int) -> None:
        try:
            _UI_REF_STORE.update(
                snapshot_id=f"snap_{idx}",
                window_title="App",
                hwnd=idx,
                elements_by_ref={
                    "e1": {
                        "ref": "e1",
                        "role": "button",
                        "name": f"Btn {idx}",
                        "value": None,
                        "enabled": True,
                        "focused": False,
                        "bounds": {"x": 10, "y": 10, "width": 50, "height": 20},
                        "center": {"cx": 35, "cy": 20},
                        "automation_id": f"btn_{idx}",
                        "control_type": "Button",
                    }
                },
            )
        except Exception as exc:  # pragma: no cover
            errors.append(exc)

    threads = [threading.Thread(target=_writer, args=(i,)) for i in range(1, 15)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert not errors
    sid, _, _, entry = _UI_REF_STORE.resolve("e1")
    assert sid.startswith("snap_")
    assert entry["ref"] == "e1"


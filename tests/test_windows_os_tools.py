"""Unit and end-to-end RPC/voice tests for Tier-1 Windows OS & Git workspace tools."""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
import json
import subprocess
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
    OsControlMediaVolumeResult,
    OsControlSystemParams,
    OsControlSystemRequest,
    OsControlSystemResult,
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
    _POWERSHELL_READ_CLIPBOARD_HISTORY_SCRIPT,
    _POWERSHELL_SELECT_CLIPBOARD_ITEM_SCRIPT,
    CF_UNICODETEXT,
    KEYEVENTF_EXTENDEDKEY,
    KEYEVENTF_KEYUP,
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
    control_media_volume,
    control_system,
    find_matching_start_app,
    get_installed_apps,
    open_or_focus_app,
    open_url,
    read_clipboard,
    resolve_app_spec,
    select_clipboard_item,
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

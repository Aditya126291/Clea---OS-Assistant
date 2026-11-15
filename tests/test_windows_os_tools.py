"""Unit and end-to-end RPC/voice tests for Tier-1 Windows OS & Git workspace tools."""

from __future__ import annotations

import asyncio
import contextlib
import ctypes
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
    OsOpenOrFocusAppResult,
    OsReadClipboardResult,
    OsWriteClipboardResult,
    WorkspaceGitInspectResult,
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
from clea.tools.windows_os import (
    CF_UNICODETEXT,
    KEYEVENTF_EXTENDEDKEY,
    KEYEVENTF_KEYUP,
    SW_RESTORE,
    VALID_MEDIA_VOLUME_ACTIONS,
    VK_MEDIA_NEXT_TRACK,
    VK_MEDIA_PLAY_PAUSE,
    VK_MEDIA_PREV_TRACK,
    VK_VOLUME_DOWN,
    VK_VOLUME_MUTE,
    VK_VOLUME_UP,
    EnumeratedWindow,
    control_media_volume,
    open_or_focus_app,
    read_clipboard,
    resolve_app_spec,
    write_clipboard,
)

# --------------------------------------------------------------------------- Test Doubles


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
    """Minimal user32 double tracking window focus and keybd_event calls."""

    def __init__(self, *, iconic_hwnds: set[int] | None = None) -> None:
        self.iconic_hwnds = iconic_hwnds or set()
        self.show_calls: list[tuple[int, int]] = []
        self.foreground_calls: list[int] = []
        self.key_events: list[tuple[int, int, int, int]] = []

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
    daemon = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=_FakeProvider(),
        memory=mem,
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

    def _fake_read_clip(max_chars: int = 8000) -> dict[str, Any]:
        calls["read_clip"].append(max_chars)
        return {
            "ok": True,
            "text": "copied text",
            "truncated": False,
            "char_count": 11,
            "total_chars": 11,
        }

    def _fake_write_clip(text: str) -> dict[str, Any]:
        calls["write_clip"].append(text)
        return {"ok": True, "char_count": len(text)}

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

    monkeypatch.setattr(daemon_mod, "open_or_focus_app", _fake_open)
    monkeypatch.setattr(daemon_mod, "control_media_volume", _fake_volume)
    monkeypatch.setattr(daemon_mod, "read_clipboard", _fake_read_clip)
    monkeypatch.setattr(daemon_mod, "write_clipboard", _fake_write_clip)
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

        # Invalid arguments return tool error response cleanly.
        r_bad = await _invoke_voice_tool(
            backend,
            call_id="c-bad-vol",
            name="control_media_volume",
            arguments={"action": "set_volume"},
        )
        assert r_bad.is_error is True
        assert "level" in str(r_bad.response.get("error", ""))

    assert set(METHOD_RESULTS) >= {
        "os.open_or_focus_app",
        "os.control_media_volume",
        "os.read_clipboard",
        "os.write_clipboard",
        "workspace.git_inspect",
    }

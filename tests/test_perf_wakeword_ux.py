"""Regression tests for perf, hidden subprocesses, wake-word UX, Z-order & prompts.

Covers all five hardening areas on branch ``fix/perf-wakeword-ux``:
1. Hidden subprocess flags (CREATE_NO_WINDOW + SW_HIDE) & installed-app TTL/failure cache.
2. Non-blocking background memory reconciliation, MemoryStore.ensure_initialized()
   idempotency, and bounded GeminiProvider mid-stream event timeout.
3. VoiceRunner wake-word reliability & state resets + AudioManager fallback & health checks.
4. Split-screen Z-order occlusion detection & Chromium PWA vs. Chrome disambiguation.
5. Anti-refusal & cheap-first escalation prompts across Chat and Voice surfaces.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import sys
import time
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import numpy as np
import pytest
import sounddevice as sd

from clea.core.agent import _BASE_SYSTEM_PROMPT, AgentLoop
from clea.core.sessions import Session
from clea.memory.store import MemoryStore
from clea.providers.base import LLMProvider, Message, StreamChunk, TokenUsage
from clea.providers.gemini import GeminiProvider
from clea.surfaces.cli import autostart
from clea.surfaces.voice.audio import AudioManager
from clea.surfaces.voice.backends.base import (
    AudioOutputEvent,
    BackendErrorEvent,
    ToolCallCancellationEvent,
    ToolCallEvent,
    TranscriptOutputEvent,
    TurnCompleteEvent,
    VoiceBackend,
    VoiceBackendEvent,
    VoiceSessionConfig,
    VoiceToolResponse,
)
from clea.surfaces.voice.config import VoiceConfig
from clea.surfaces.voice.memory_hooks import BASE_VOICE_SYSTEM_INSTRUCTION
from clea.surfaces.voice.runner import VoiceRunner
from clea.surfaces.voice.wake_word import WakeWordDetector
from clea.tools import file_intel, git_workspace, system_controls, vision, windows_os
from clea.tools.guards import ActionVerifier, FocusPreconditionBackend, WindowContext
from clea.tools.registry import ToolRegistry
from clea.tools.subprocess_utils import (
    CREATE_NO_WINDOW,
    STARTF_USESHOWWINDOW,
    SW_HIDE,
    hidden_subprocess_kwargs,
    popen_hidden,
    run_hidden,
    run_powershell_hidden,
)


@pytest.fixture(autouse=True)
def _isolate_clea_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[Path]:
    """Keep all tests isolated from real %LOCALAPPDATA%\\clea and reset app cache."""
    data_dir = tmp_path / "clea_data"
    data_dir.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLEA_DATA_DIR", str(data_dir))
    windows_os._INSTALLED_APPS_CACHE = None
    windows_os._INSTALLED_APPS_CACHE_TIME = 0.0
    windows_os._INSTALLED_APPS_FAILURE_UNTIL = 0.0
    yield data_dir
    windows_os._INSTALLED_APPS_CACHE = None
    windows_os._INSTALLED_APPS_CACHE_TIME = 0.0
    windows_os._INSTALLED_APPS_FAILURE_UNTIL = 0.0


def _assert_hidden_subprocess_kwargs(kwargs: dict[str, Any]) -> None:
    """Assert that a subprocess call carries CREATE_NO_WINDOW + hidden STARTUPINFO."""
    flags = int(kwargs.get("creationflags", 0))
    assert flags & CREATE_NO_WINDOW == CREATE_NO_WINDOW, (
        f"Expected CREATE_NO_WINDOW (0x{CREATE_NO_WINDOW:08x}) in creationflags=0x{flags:08x}"
    )
    si = kwargs.get("startupinfo")
    assert si is not None, "Expected hidden STARTUPINFO on Windows subprocess call"
    assert getattr(si, "dwFlags", 0) & STARTF_USESHOWWINDOW == STARTF_USESHOWWINDOW
    assert getattr(si, "wShowWindow", -1) == SW_HIDE


# ===========================================================================
# 1. Hidden Subprocess Flags & Installed-App Cache
# ===========================================================================


def test_hidden_subprocess_kwargs_and_helpers_enforce_no_window(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """hidden_subprocess_kwargs, run_hidden, popen_hidden, run_powershell_hidden stay hidden."""
    monkeypatch.setattr(sys, "platform", "win32")

    kw = hidden_subprocess_kwargs(extra_flags=0x00000010)
    _assert_hidden_subprocess_kwargs(kw)
    assert kw["creationflags"] & 0x00000010 == 0x00000010

    run_calls: list[tuple[Any, dict[str, Any]]] = []
    popen_calls: list[tuple[Any, dict[str, Any]]] = []

    def fake_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        run_calls.append((cmd, dict(kwargs)))
        return subprocess.CompletedProcess(cmd, 0, stdout="ok", stderr="")

    class FakePopen:
        def __init__(self, cmd: Any, **kwargs: Any) -> None:
            popen_calls.append((cmd, dict(kwargs)))

    monkeypatch.setattr(subprocess, "run", fake_run)
    monkeypatch.setattr(subprocess, "Popen", FakePopen)

    run_hidden(["git", "status"], capture_output=True, text=True)
    popen_hidden(["cmd", "/c", "echo", "hi"])
    run_powershell_hidden("Write-Output 'hi'", timeout=3.0, ps_exe="powershell.exe")

    assert len(run_calls) == 2
    assert len(popen_calls) == 1

    _assert_hidden_subprocess_kwargs(run_calls[0][1])
    _assert_hidden_subprocess_kwargs(popen_calls[0][1])

    ps_cmd, ps_kwargs = run_calls[1]
    assert "-NoProfile" in ps_cmd
    assert "-NonInteractive" in ps_cmd
    assert "-Command" in ps_cmd
    assert ps_kwargs.get("shell") is False
    _assert_hidden_subprocess_kwargs(ps_kwargs)


def test_background_helpers_pass_hidden_flags_across_modules(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Background PowerShell/CMD helpers across all 6 modules pass CREATE_NO_WINDOW + SW_HIDE."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr("shutil.which", lambda name: name)
    monkeypatch.setitem(sys.modules, "send2trash", None)

    captured: list[tuple[str, Any, dict[str, Any]]] = []
    current_label = ""

    def fake_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        captured.append((current_label, cmd, dict(kwargs)))
        if current_label == "windows_os":
            payload = json.dumps([{"Name": "YouTube", "AppID": "Chrome._crx_yt"}])
            return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr="")
        if current_label == "system_controls":
            return subprocess.CompletedProcess(cmd, 0, stdout="45 false\n", stderr="")
        if current_label == "vision":
            return subprocess.CompletedProcess(cmd, 0, stdout="Search\nPlay\n", stderr="")
        if current_label == "autostart":
            return subprocess.CompletedProcess(
                cmd,
                0,
                stdout="TaskName: CleaDaemon\nStatus: Ready\n",
                stderr="",
            )
        return subprocess.CompletedProcess(cmd, 0, stdout="ok\n", stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    # 1. windows_os.get_installed_apps
    current_label = "windows_os"
    apps = windows_os.get_installed_apps(force_refresh=True)
    assert apps and apps[0]["Name"] == "YouTube"

    # 2. system_controls._run_coreaudio_powershell
    current_label = "system_controls"
    vol, muted = system_controls._run_coreaudio_powershell("get")
    assert vol == 45 and muted is False

    # 3. vision.Win32ScreenVisionBackend.probe_uia_text (fallback PowerShell path)
    current_label = "vision"
    monkeypatch.setitem(sys.modules, "comtypes", None)
    monkeypatch.setitem(sys.modules, "comtypes.client", None)
    backend = vision.Win32ScreenVisionBackend(user32=SimpleNamespace(), gdi32=None, dwmapi=None)
    uia_text = backend.probe_uia_text(12345)
    assert "Search" in uia_text

    # 4. file_intel._recycle_path (PowerShell Recycle Bin path)
    current_label = "file_intel"
    recycle_target = tmp_path / "trash_me.txt"
    recycle_target.write_text("temp", encoding="utf-8")
    monkeypatch.setattr("os.name", "nt")
    assert file_intel._recycle_path(recycle_target) == "powershell_recycle_bin"

    # 5. git_workspace._run_git
    current_label = "git_workspace"
    git_res = git_workspace._run_git(tmp_path, ["status", "--short"])
    assert git_res.strip() == "ok"

    # 6. surfaces/cli/autostart.py
    current_label = "autostart"
    status = autostart.status_autostart(runner=subprocess.run)
    assert status.installed is True

    labels_seen = {lbl for lbl, _cmd, _kw in captured}
    assert labels_seen == {
        "windows_os",
        "system_controls",
        "vision",
        "file_intel",
        "git_workspace",
        "autostart",
    }
    for lbl, _cmd, kw in captured:
        _assert_hidden_subprocess_kwargs(kw)
        assert lbl  # non-empty sanity check


def test_installed_apps_cache_hit_and_failure_backoff(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """get_installed_apps/find_installed_service_app/has_installed_app use TTL + failure backoff."""
    monkeypatch.setattr(sys, "platform", "win32")
    monkeypatch.setattr(
        "shutil.which",
        lambda name: "powershell.exe" if "powershell" in name else None,
    )

    spawn_count = 0
    mode = "ok"

    def fake_run(cmd: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
        nonlocal spawn_count
        spawn_count += 1
        _assert_hidden_subprocess_kwargs(kwargs)
        if mode == "fail":
            return subprocess.CompletedProcess(cmd, 1, stdout="", stderr="Access denied")
        payload = json.dumps(
            [
                {"Name": "YouTube", "AppID": "Chrome._crx_yt123"},
                {"Name": "Spotify", "AppID": "SpotifyAB.SpotifyMusic!Spotify"},
            ]
        )
        return subprocess.CompletedProcess(cmd, 0, stdout=payload, stderr="")

    monkeypatch.setattr(subprocess, "run", fake_run)

    # First call populates cache with 1 PowerShell spawn
    catalog1 = windows_os.get_installed_apps()
    assert len(catalog1) == 2
    assert spawn_count == 1

    # Repeated calls + service lookup + has_installed_app hit cache without respawning PowerShell
    catalog2 = windows_os.get_installed_apps()
    assert len(catalog2) == 2
    assert windows_os.find_installed_service_app("youtube", catalog2) is not None
    assert windows_os.has_installed_app("spotify", which_fn=lambda _x: None, env={}) is True
    assert (
        windows_os.has_installed_app("https://youtube.com", which_fn=lambda _x: None, env={})
        is True
    )
    assert spawn_count == 1

    # Reset cache and simulate PowerShell failure -> negative backoff avoids repeat spawns
    windows_os._INSTALLED_APPS_CACHE = None
    windows_os._INSTALLED_APPS_CACHE_TIME = 0.0
    windows_os._INSTALLED_APPS_FAILURE_UNTIL = 0.0
    mode = "fail"

    assert windows_os.get_installed_apps() == []
    assert spawn_count == 2
    # Subsequent calls within failure backoff window do NOT spawn PowerShell again
    assert windows_os.get_installed_apps() == []
    assert windows_os.has_installed_app("spotify", which_fn=lambda _x: None, env={}) is False
    assert spawn_count == 2

    # Explicit force_refresh bypasses failure backoff
    mode = "ok"
    refreshed = windows_os.get_installed_apps(force_refresh=True)
    assert len(refreshed) == 2
    assert spawn_count == 3


# ===========================================================================
# 2. Non-Blocking Background Memory, ensure_initialized() & Gemini Timeout
# ===========================================================================


class _StubChatProvider(LLMProvider):
    """Instant single-turn stub LLMProvider for AgentLoop latency testing."""

    provider_name = "stub"

    def __init__(self, reply: str = "Done.") -> None:
        self._reply = reply

    @property
    def model_name(self) -> str:
        return "stub-model"

    async def stream_turn(
        self,
        *,
        messages: list[Any],
        system_prompt: str,
        tools: list[Any] | None = None,
        previous_interaction_id: str | None = None,
    ) -> AsyncIterator[StreamChunk]:
        yield StreamChunk(type="text_delta", text=self._reply)
        yield StreamChunk(
            type="done",
            usage=TokenUsage(input_tokens=10, output_tokens=5, total_tokens=15),
        )


async def test_agent_loop_background_memory_hooks_non_blocking_and_aclose(
    tmp_path: Path,
) -> None:
    """background_memory_hooks=True returns run_turn immediately and aclose cancels cleanly."""
    store = MemoryStore(base_dir=tmp_path / "mem_store")
    store.ensure_initialized()

    reconcile_started = asyncio.Event()
    compact_started = asyncio.Event()
    release_reconcile = asyncio.Event()
    release_compact = asyncio.Event()
    compact_cancelled = False

    async def slow_reconcile(
        text: str,
        *,
        source_surface: str = "chat",
        source_session_id: str | None = None,
    ) -> list[Any]:
        reconcile_started.set()
        await release_reconcile.wait()
        return []

    async def slow_compact(
        messages: list[Any],
    ) -> tuple[list[Any], str | None]:
        nonlocal compact_cancelled
        compact_started.set()
        try:
            await release_compact.wait()
        except asyncio.CancelledError:
            compact_cancelled = True
            raise
        return messages, None

    agent = AgentLoop(
        provider=_StubChatProvider("Immediate reply."),
        registry=ToolRegistry(),
        memory=store,
        auto_reconcile_llm=True,
        background_memory_hooks=True,
        background_memory_timeout_seconds=10.0,
    )
    agent.fact_reconciler.reconcile_text = slow_reconcile  # type: ignore[method-assign]
    agent.working_memory.compact_if_needed = slow_compact  # type: ignore[method-assign]

    conv = Session(id="conv-bg-1", title="BG Test")
    t0 = time.monotonic()
    reply = await agent.run_turn(
        session=conv,
        user_text="Remember that I prefer dark mode everywhere.",
    )
    elapsed = time.monotonic() - t0

    assert reply == "Immediate reply."
    assert elapsed < 0.5, f"run_turn blocked for {elapsed:.2f}s waiting on background memory!"

    # Background task reached fact extraction, then working-memory compaction without blocking turn
    await asyncio.wait_for(reconcile_started.wait(), timeout=1.0)
    assert len(agent._background_memory_tasks) >= 1
    release_reconcile.set()
    await asyncio.wait_for(compact_started.wait(), timeout=1.0)

    # aclose() cleanly cancels and clears all pending background memory tasks
    await agent.aclose()
    assert compact_cancelled is True
    assert len(agent._background_memory_tasks) == 0


def test_memory_store_ensure_initialized_is_idempotent(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """MemoryStore.ensure_initialized() only runs schema/migration once across repeated calls."""
    store = MemoryStore(base_dir=tmp_path / "idempotent_store")

    migration_calls = 0
    schema_calls = 0
    orig_migrate = store.migrate_legacy_daily_logs
    orig_schema = store.memory_db.ensure_schema

    def counting_migrate(*args: Any, **kwargs: Any) -> int:
        nonlocal migration_calls
        migration_calls += 1
        return orig_migrate(*args, **kwargs)

    def counting_schema() -> None:
        nonlocal schema_calls
        schema_calls += 1
        orig_schema()

    monkeypatch.setattr(store, "migrate_legacy_daily_logs", counting_migrate)
    monkeypatch.setattr(store.memory_db, "ensure_schema", counting_schema)

    for _ in range(5):
        store.ensure_initialized()

    assert store._initialized is True
    assert migration_calls == 1
    assert schema_calls == 1


async def test_gemini_provider_bounded_mid_stream_event_timeout() -> None:
    """GeminiProvider raises TimeoutError promptly when a stream stalls mid-turn."""
    provider = GeminiProvider(
        api_key="test-key",
        first_event_timeout_s=0.2,
        stream_event_timeout_s=0.06,
    )

    class _HangingStream:
        def __init__(self) -> None:
            self._idx = 0

        async def __aenter__(self) -> _HangingStream:
            return self

        async def __aexit__(self, *_args: Any) -> None:
            return None

        def __aiter__(self) -> _HangingStream:
            return self

        async def __anext__(self) -> Any:
            if self._idx == 0:
                self._idx += 1
                return SimpleNamespace(
                    event_type="step.delta",
                    delta=SimpleNamespace(type="text", text="partial "),
                    index=0,
                )
            await asyncio.sleep(5.0)
            raise StopAsyncIteration

    fake_interactions = SimpleNamespace(create=AsyncMock(return_value=_HangingStream()))
    provider._client = SimpleNamespace(aio=SimpleNamespace(interactions=fake_interactions))

    received: list[str] = []
    t0 = time.monotonic()
    with pytest.raises(TimeoutError, match="stalled"):
        async for chunk in provider.stream_turn(
            messages=[Message(role="user", content="hello")],
            tools=[],
            system_prompt="Be concise.",
        ):
            if chunk.type == "text_delta":
                received.append(chunk.text)
    elapsed = time.monotonic() - t0

    assert received == ["partial "]
    assert elapsed < 1.0, f"Mid-stream stall did not time out promptly ({elapsed:.2f}s)"


# ===========================================================================
# 3. Wake-Word Reliability, VoiceRunner State Resets & AudioManager Health
# ===========================================================================


class _FakeVoiceBackend(VoiceBackend):
    """Controllable in-memory VoiceBackend for VoiceRunner state-reset testing."""

    backend_name = "fake-live"

    def __init__(self, *, connect_gate: asyncio.Event | None = None) -> None:
        self.connect_gate = connect_gate
        self.connect_started = asyncio.Event()
        self.connected = False
        self.closed = False
        self.sent_tool_responses: list[VoiceToolResponse] = []
        self.event_queue: asyncio.Queue[VoiceBackendEvent | None] = asyncio.Queue()

    @property
    def model_name(self) -> str:
        return "fake-voice-model"

    async def connect(self, config: VoiceSessionConfig) -> None:
        self.connect_started.set()
        if self.connect_gate is not None:
            await self.connect_gate.wait()
        self.connected = True
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
            ev = await self.event_queue.get()
            if ev is None:
                break
            yield ev


def _make_fake_audio() -> AudioManager:
    audio = AudioManager()
    audio.start = MagicMock()  # type: ignore[method-assign]
    audio.stop = MagicMock()  # type: ignore[method-assign]
    audio.play_audio = MagicMock()  # type: ignore[method-assign]
    audio.stop_output = MagicMock()  # type: ignore[method-assign]
    audio.flush_input_buffer = MagicMock()  # type: ignore[method-assign]
    return audio


def _make_controllable_wake_word(
    initial_score: float = 0.0,
) -> tuple[WakeWordDetector, list[float]]:
    score_box = [initial_score]
    mock_model = MagicMock()
    mock_model.predict.side_effect = lambda _arr: {"hey_jarvis_v0.1": score_box[0]}
    mock_model.reset = MagicMock()
    detector = WakeWordDetector(
        model_instance=mock_model,
        threshold=0.5,
    )
    return detector, score_box


async def test_voice_runner_wake_word_resumes_after_multi_tool_turn_and_suppressed_audio(
    tmp_path: Path,
) -> None:
    """Wake word cleanly re-arms after multi-tool turn, suppressed pre-tool audio & dup turns."""
    backends: list[_FakeVoiceBackend] = []

    def backend_factory() -> _FakeVoiceBackend:
        b = _FakeVoiceBackend()
        backends.append(b)
        return b

    audio = _make_fake_audio()
    wake_word, score_box = _make_controllable_wake_word(0.0)
    runner = VoiceRunner(
        config=VoiceConfig(silence_timeout_seconds=60.0),
        data_dir=tmp_path / "voice1",
        backend_factory=backend_factory,
        audio_manager=audio,
        wake_word_detector=wake_word,
        enable_hotkey=False,
    )
    runner._loop = asyncio.get_running_loop()

    async def fake_execute_tool(name: str, arguments: dict[str, Any]) -> dict[str, Any]:
        return {"ok": True, "tool": name, "args": arguments}

    runner._execute_tool_call = fake_execute_tool  # type: ignore[method-assign]

    with (
        patch(
            "clea.surfaces.voice.runner.resolve_voice_system_instruction",
            new=AsyncMock(return_value="Be helpful."),
        ),
        patch("clea.surfaces.voice.runner.safe_store_voice_transcript", new=AsyncMock()),
        patch("clea.surfaces.voice.runner.safe_consolidate_voice_call", new=AsyncMock()),
    ):
        await runner.start_call(trigger="wake_word")
        b1 = backends[0]

        # Emit speculative pre-tool speech audio + unfinalized assistant transcript
        await b1.event_queue.put(AudioOutputEvent(pcm_bytes=b"\x01\x02" * 64))
        await b1.event_queue.put(
            TranscriptOutputEvent(role="assistant", text="Opening now...", final=False)
        )
        # Queue two contiguous tool calls
        await b1.event_queue.put(
            ToolCallEvent(call_id="c1", name="open_or_focus_app", arguments={"app_name": "youtube"})
        )
        await b1.event_queue.put(
            ToolCallEvent(
                call_id="c2",
                name="control_system",
                arguments={"action": "snap_left", "window_title": "YouTube"},
            )
        )
        # Duplicate TurnCompleteEvents + final transcript
        await b1.event_queue.put(
            TranscriptOutputEvent(role="assistant", text="YouTube is snapped left.", final=True)
        )
        await b1.event_queue.put(TurnCompleteEvent())
        await b1.event_queue.put(TurnCompleteEvent())

        # Wait until both tool responses were sent back to backend
        for _ in range(50):
            if len(b1.sent_tool_responses) >= 2:
                break
            await asyncio.sleep(0.02)

        assert len(b1.sent_tool_responses) == 2
        # Pre-tool speculative audio was suppressed via stop_output()
        assert audio.stop_output.call_count >= 1
        # Unfinalized pre-tool assistant transcript was pruned
        texts = [entry["text"] for entry in runner._transcript_history]
        assert "Opening now..." not in texts
        assert "YouTube is snapped left." in texts

        # End call -> verify complete state reset + wake-word re-arming
        await runner.end_call(reason="user_done")
        assert runner._call_state == "idle"
        assert runner._state == "idle"
        assert runner._wake_word_pending_trigger is False
        assert audio.flush_input_buffer.call_count >= 1
        assert wake_word._model.reset.call_count >= 1

        # Simulate wake-word trigger on fresh audio chunk -> starts call #2 cleanly
        score_box[0] = 0.95
        pcm = (np.ones(1280, dtype=np.int16) * 500).tobytes()
        runner._start_wake_word_worker()
        try:
            runner._on_audio_chunk(pcm)
            for _ in range(50):
                if len(backends) >= 2 and runner._call_state == "active":
                    break
                await asyncio.sleep(0.02)
            assert len(backends) == 2
            assert runner._call_state == "active"
        finally:
            runner._stop_wake_word_worker()
            await runner.end_call(reason="test_cleanup")


async def test_voice_runner_mid_connect_cancel_and_tool_cancellation_rearm(
    tmp_path: Path,
) -> None:
    """end_call during connect() invalidates stale connect; cancellation & errors recover IDLE."""
    gate = asyncio.Event()
    b_slow = _FakeVoiceBackend(connect_gate=gate)
    b_next = _FakeVoiceBackend()
    created = iter([b_slow, b_next])

    audio = _make_fake_audio()
    wake_word, _ = _make_controllable_wake_word(0.0)
    runner = VoiceRunner(
        config=VoiceConfig(silence_timeout_seconds=60.0),
        data_dir=tmp_path / "voice2",
        backend_factory=lambda: next(created),
        audio_manager=audio,
        wake_word_detector=wake_word,
        enable_hotkey=False,
    )
    runner._loop = asyncio.get_running_loop()

    with (
        patch(
            "clea.surfaces.voice.runner.resolve_voice_system_instruction",
            new=AsyncMock(return_value="Be helpful."),
        ),
        patch("clea.surfaces.voice.runner.safe_store_voice_transcript", new=AsyncMock()),
        patch("clea.surfaces.voice.runner.safe_consolidate_voice_call", new=AsyncMock()),
    ):
        # 1. Start call and cancel mid-connect
        start_task = asyncio.create_task(runner.start_call(trigger="wake_word"))
        await asyncio.wait_for(b_slow.connect_started.wait(), timeout=1.0)
        assert runner._call_state == "connecting"

        await runner.end_call(reason="cancelled_mid_connect")
        gate.set()
        await asyncio.wait_for(start_task, timeout=1.0)

        assert b_slow.closed is True
        assert runner._call_state == "idle"
        assert runner._active_backend is None
        assert runner._wake_word_pending_trigger is False

        # 2. Start a second call, exercise ToolCallCancellationEvent + non-recoverable error
        await runner.start_call(trigger="wake_word")
        assert runner._call_state == "active"
        pending_fut: asyncio.Future[dict[str, Any]] = asyncio.get_running_loop().create_future()
        runner._pending_tasks["cid-99"] = pending_fut
        runner._set_voice_state("thinking")

        await b_next.event_queue.put(ToolCallCancellationEvent(call_ids=["cid-99"]))
        for _ in range(30):
            if pending_fut.cancelled() and runner._state == "listening":
                break
            await asyncio.sleep(0.02)
        assert pending_fut.cancelled() is True
        assert runner._state == "listening"

        await b_next.event_queue.put(
            BackendErrorEvent(message="socket closed", recoverable=False)
        )
        for _ in range(40):
            if runner._call_state == "idle":
                break
            await asyncio.sleep(0.02)
        assert runner._call_state == "idle"
        assert runner._wake_word_pending_trigger is False
        assert wake_word._model.reset.call_count >= 2


def test_audio_manager_device_fallback_and_stream_health(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """AudioManager falls back on PortAudio open failure and detects dead/stalled/zero streams."""
    fake_devices = [
        {
            "name": "Headset Mic",
            "hostapi": 0,
            "max_input_channels": 1,
            "max_output_channels": 0,
            "default_samplerate": 48000.0,
        },
        {
            "name": "Headset Mic",
            "hostapi": 1,
            "max_input_channels": 1,
            "max_output_channels": 0,
            "default_samplerate": 16000.0,
        },
        {
            "name": "Speakers",
            "hostapi": 0,
            "max_input_channels": 0,
            "max_output_channels": 2,
            "default_samplerate": 48000.0,
        },
    ]
    fake_hostapis = [
        {"name": "Windows WASAPI", "default_input_device": 0, "default_output_device": 2},
        {"name": "Windows DirectSound", "default_input_device": 1, "default_output_device": 2},
    ]

    class _FakeStream:
        def __init__(self, **kwargs: Any) -> None:
            self.kwargs = kwargs
            self.stopped = False
            self.active = True

        def start(self) -> None:
            pass

        def stop(self) -> None:
            self.stopped = True
            self.active = False

        def close(self) -> None:
            pass

    attempted_input_devices: list[int] = []

    def fake_input_stream(**kwargs: Any) -> _FakeStream:
        dev_idx = int(kwargs["device"])
        attempted_input_devices.append(dev_idx)
        if dev_idx == 0:
            raise sd.PortAudioError("Invalid device [-9996]")
        return _FakeStream(**kwargs)

    monkeypatch.setattr(sd, "query_devices", lambda: fake_devices)
    monkeypatch.setattr(sd, "query_hostapis", lambda: fake_hostapis)
    monkeypatch.setattr(sd, "check_input_settings", lambda **_kw: None)
    monkeypatch.setattr(sd, "InputStream", fake_input_stream)
    monkeypatch.setattr(sd, "OutputStream", lambda **kw: _FakeStream(**kw))
    monkeypatch.setattr(sd, "default", SimpleNamespace(device=(0, 2)))

    mgr = AudioManager()
    mgr.start()
    try:
        assert 0 in attempted_input_devices
        assert mgr.resolved_input_device is not None
        assert mgr.resolved_input_device["index"] == 1
        assert mgr.resolved_input_device["hostapi_name"] == "Windows DirectSound"

        # 1. Healthy stream
        ok, reason = mgr.is_input_stream_healthy(max_callback_gap_seconds=5.0)
        assert ok is True and reason == "ok"

        # 2. Stalled callbacks
        mgr._last_input_callback_time = time.monotonic() - 12.0
        ok, reason = mgr.is_input_stream_healthy(max_callback_gap_seconds=5.0)
        assert ok is False and reason.startswith("input_callback_stalled_")

        # 3. Sustained all-zero silence blocks
        mgr._last_input_callback_time = time.monotonic()
        mgr._consecutive_zero_callbacks = 150
        ok, reason = mgr.is_input_stream_healthy(max_zero_callbacks=125)
        assert ok is False and reason.startswith("sustained_zero_audio_")

        # 4. Stopped/inactive stream
        mgr._consecutive_zero_callbacks = 0
        assert mgr._input_stream is not None
        mgr._input_stream.active = False  # type: ignore[attr-defined]
        ok, reason = mgr.is_input_stream_healthy()
        assert ok is False and reason == "input_stream_inactive"
    finally:
        mgr.stop()


# ===========================================================================
# 4. Split-Screen Z-Order Occlusion & Chromium PWA Disambiguation
# ===========================================================================


class _FakeOcclusionBackend(FocusPreconditionBackend):
    """FocusPreconditionBackend returning a left-snapped target occluded by a higher window."""

    def __init__(self, occluders: list[dict[str, Any]]) -> None:
        self._occluders = occluders

    def get_foreground_context(self) -> WindowContext | None:
        return WindowContext(hwnd=101, title="YouTube", process_name="chrome.exe")

    def get_window_context(self, hwnd: int) -> WindowContext | None:
        if hwnd == 101:
            return WindowContext(hwnd=101, title="YouTube", process_name="chrome.exe")
        return None

    def is_window_valid(self, hwnd: int) -> bool:
        return hwnd == 101

    def is_minimized(self, hwnd: int) -> bool:
        return False

    def get_extended_frame_bounds(self, hwnd: int) -> tuple[int, int, int, int] | None:
        return (0, 0, 960, 1040) if hwnd == 101 else None

    def get_monitor_work_area(self, hwnd: int) -> tuple[int, int, int, int] | None:
        return (0, 0, 1920, 1040)

    def find_occluding_windows(
        self,
        hwnd: int,
        *,
        target_bounds: tuple[int, int, int, int] | None = None,
        tolerance_px: int = 24,
    ) -> list[dict[str, Any]]:
        return list(self._occluders)


def test_action_verifier_and_inspect_window_layout_detect_z_order_occlusion() -> None:
    """verify_window_state and inspect_window_layout flag higher Z-order occluding windows."""
    # 1. ActionVerifier fails snap verification when an unrelated higher Z-order window overlaps
    backend = _FakeOcclusionBackend(
        occluders=[{"hwnd": 999, "title": "Google Chrome", "process_name": "chrome.exe"}]
    )
    verifier = ActionVerifier(focus_backend=backend)
    outcome = verifier.verify_window_state(
        hwnd=101,
        expected_snap="left",
        timeout_s=0.04,
        poll_interval_s=0.01,
        check_occlusion=True,
    )
    assert outcome.verified is False
    assert outcome.reason == "occluded_by_unrelated_window"
    assert outcome.occluded_by_unrelated_window is True
    assert "Google Chrome" in outcome.occluding_windows

    # Clear occluder -> passes cleanly
    clean_verifier = ActionVerifier(focus_backend=_FakeOcclusionBackend(occluders=[]))
    clean_outcome = clean_verifier.verify_window_state(
        hwnd=101,
        expected_snap="left",
        timeout_s=0.04,
        poll_interval_s=0.01,
        check_occlusion=True,
    )
    assert clean_outcome.verified is True
    assert clean_outcome.occluded_by_unrelated_window is False

    # 2. inspect_window_layout detects an unrelated higher Z-order window overlapping snapped target
    occluded_layout = system_controls.inspect_window_layout(
        windows=[
            {
                "hwnd": 500,
                "title": "Antigravity",
                "process_name": "Antigravity.exe",
                "bounds": (0, 0, 1920, 1040),
                "work_area": (0, 0, 1920, 1040),
                "is_maximized": True,
            },
            {
                "hwnd": 101,
                "title": "YouTube",
                "process_name": "chrome.exe",
                "bounds": (0, 0, 960, 1040),
                "work_area": (0, 0, 1920, 1040),
            },
            {
                "hwnd": 102,
                "title": "Spotify",
                "process_name": "Spotify.exe",
                "bounds": (960, 0, 1920, 1040),
                "work_area": (0, 0, 1920, 1040),
            },
        ]
    )
    assert occluded_layout["is_true_split_screen"] is False
    assert occluded_layout["has_occluded_background"] is True
    yt_entry = next(w for w in occluded_layout["windows"] if w["hwnd"] == 101)
    assert yt_entry["occluded_by_unrelated_window"] is True
    assert "Antigravity" in yt_entry["occluding_windows"]

    # Clean non-overlapping 50/50 split reports true split-screen with zero occlusion
    clean_layout = system_controls.inspect_window_layout(
        windows=[
            {
                "hwnd": 101,
                "title": "YouTube",
                "process_name": "chrome.exe",
                "bounds": (0, 0, 960, 1040),
                "work_area": (0, 0, 1920, 1040),
            },
            {
                "hwnd": 102,
                "title": "Spotify",
                "process_name": "Spotify.exe",
                "bounds": (960, 0, 1920, 1040),
                "work_area": (0, 0, 1920, 1040),
            },
        ]
    )
    assert clean_layout["is_true_split_screen"] is True
    assert clean_layout["has_occluded_background"] is False
    assert all(not w["occluded_by_unrelated_window"] for w in clean_layout["windows"])


def test_chromium_pwa_vs_chrome_browser_window_disambiguation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Standalone Chromium PWA ('YouTube') and Chrome tabs disambiguate accurately."""
    pwa_win = windows_os.EnumeratedWindow(
        hwnd=201,
        title="YouTube",
        process_name="chrome.exe",
        is_visible=True,
    )
    chrome_tab = windows_os.EnumeratedWindow(
        hwnd=202,
        title="Some Tab - Google Chrome",
        process_name="chrome.exe",
        is_visible=True,
    )
    yt_chrome_tab = windows_os.EnumeratedWindow(
        hwnd=203,
        title="Lo-fi Beats - YouTube - Google Chrome",
        process_name="chrome.exe",
        is_visible=True,
    )

    chrome_spec = windows_os._KNOWN_APPS["chrome"]
    assert windows_os._window_matches_spec(chrome_tab, chrome_spec) is True
    assert windows_os._window_matches_spec(pwa_win, chrome_spec) is False

    # _pick_best_matching_window respects prefer_standalone_pwa vs prefer_browser_window
    fake_u32 = SimpleNamespace(GetForegroundWindow=lambda: 203, IsIconic=lambda _h: False)
    assert (
        windows_os._pick_best_matching_window(
            [yt_chrome_tab, pwa_win],
            fake_u32,
            prefer_standalone_pwa=True,
        ).hwnd
        == 201
    )
    assert (
        windows_os._pick_best_matching_window(
            [pwa_win, yt_chrome_tab],
            fake_u32,
            prefer_browser_window=True,
        ).hwnd
        == 203
    )

    # system_controls._resolve_target_window picks standalone YouTube PWA over Chrome tab,
    # and picks Google Chrome tab over YouTube PWA when querying "Chrome"
    enum_windows = [
        {"hwnd": 203, "title": "Lo-fi Beats - YouTube - Google Chrome", "is_foreground": True},
        {"hwnd": 202, "title": "Some Tab - Google Chrome", "is_foreground": False},
        {"hwnd": 201, "title": "YouTube", "is_foreground": False},
    ]
    monkeypatch.setattr(
        system_controls,
        "_enumerate_windows_ctypes",
        lambda _u32: list(enum_windows),
    )
    monkeypatch.setattr(system_controls, "_resolve_root_hwnd_sc", lambda h, _u32: h)
    monkeypatch.setattr(system_controls, "_is_window_cloaked_sc", lambda _h, _u32: False)
    title_by_hwnd = {w["hwnd"]: w["title"] for w in enum_windows}
    monkeypatch.setattr(
        system_controls,
        "_get_window_title_ctypes",
        lambda h, _u32: title_by_hwnd.get(h, ""),
    )

    yt_hwnd, yt_title = system_controls._resolve_target_window(fake_u32, "YouTube")
    assert yt_hwnd == 201 and yt_title == "YouTube"

    chrome_hwnd, chrome_title = system_controls._resolve_target_window(fake_u32, "Chrome")
    assert chrome_hwnd in {202, 203} and "Google Chrome" in chrome_title


# ===========================================================================
# 5. Anti-Refusal & Cheap-First Escalation Prompts (Chat & Voice)
# ===========================================================================


def test_chat_and_voice_prompts_enforce_anti_refusal_and_cheap_first_escalation() -> None:
    """Both Chat and Voice system prompts forbid chatbot refusals and mandate cheap-first ladder."""
    for label, prompt in (
        ("chat", _BASE_SYSTEM_PROMPT),
        ("voice", BASE_VOICE_SYSTEM_INSTRUCTION),
    ):
        low = prompt.lower()
        # Anti-refusal guidance for local logged-in apps
        assert "already signed in" in low, f"{label} prompt missing local signed-in context"
        assert "watch history" in low, f"{label} prompt missing YouTube watch history guidance"
        assert "spotify" in low and "library" in low, (
            f"{label} prompt missing Spotify library guidance"
        )
        assert "step 1" in low, f"{label} prompt missing anti-stop-at-Step-1 guidance"

        # Cheap-first escalation ladder: Step A -> Step B -> Step C
        assert "cheap-first" in low, f"{label} prompt missing cheap-first escalation policy"
        assert "step a" in low and "ui_snapshot" in low and "gsmtc" in low, (
            f"{label} prompt missing Step A deterministic/native/UIA guidance"
        )
        assert "step b" in low and "mechanical" in low, (
            f"{label} prompt missing Step B mechanical verification guidance"
        )
        assert "step c" in low and "single" in low and "escalat" in low, (
            f"{label} prompt missing Step C single-shot vision escalation on failure"
        )

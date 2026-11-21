"""Deterministic regression tests for the runtime reliability fixes.

Covers:
- Fix 1: double-reply / premature confirmation prevention (AgentLoop, Voice, Daemon) and
  window tools waiting for ``ActionVerifier.verify_window_state`` before returning.
- Fix 2: installed-app-first launching and per-turn single-target launch deduplication.
- Fix 3: Win32 window layout ground truth (``inspect_window_layout``) and vision metadata.
- Fix 4: AgentLoop honest incomplete/failed outcomes and stop-on-interrupt / focus_lost.

All tests use in-memory fakes; no daemon, voice runner, or live Win32 side effects.
"""

from __future__ import annotations

import asyncio
import io
import json
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest
from google.genai import types as genai_types
from PIL import Image

from clea.core.agent import (
    _BASE_SYSTEM_PROMPT,
    AgentLoop,
    _dedupe_response_text,
    _extract_launch_target,
    _prioritize_app_first_calls,
    _user_explicitly_requested_browser,
    normalize_launch_target,
)
from clea.core.daemon import build_daemon
from clea.core.protocol import (
    ConversationUpdatedEvent,
    OsOpenOrFocusAppRequest,
    OsOpenUrlRequest,
    parse_frame,
)
from clea.core.sessions import Session
from clea.memory.store import MemoryStore
from clea.providers.base import LLMProvider, Message, StreamChunk, ToolCall
from clea.surfaces.voice.audio import AudioManager
from clea.surfaces.voice.backends.base import (
    AudioOutputEvent,
    ToolCallEvent,
    TranscriptOutputEvent,
    TurnCompleteEvent,
    VoiceBackend,
    VoiceBackendEvent,
    VoiceSessionConfig,
    VoiceToolResponse,
)
from clea.surfaces.voice.backends.gemini_live import GeminiLiveBackend
from clea.surfaces.voice.config import VoiceConfig
from clea.surfaces.voice.memory_hooks import BASE_VOICE_SYSTEM_INSTRUCTION
from clea.surfaces.voice.runner import VoiceRunner
from clea.surfaces.voice.runner import normalize_launch_target as voice_normalize_launch_target
from clea.surfaces.voice.wake_word import WakeWordDetector
from clea.tools import windows_os
from clea.tools.guards import ActionVerifier, WindowContext
from clea.tools.registry import AuditLogger, RiskLevel, ToolRegistry
from clea.tools.system_controls import (
    format_window_layout_summary,
    inspect_window_layout,
    manage_window,
)
from clea.tools.vision import (
    ArtifactStore,
    ScreenVisionService,
    WindowCapture,
    WindowInfo,
    inspect_screen,
)
from clea.tools.windows_os import (
    EnumeratedWindow,
    control_system,
    is_explicit_browser_request,
    open_url,
)
from clea.tools.windows_os import (
    normalize_launch_target as tools_normalize_launch_target,
)

# ---------------------------------------------------------------------------
# Shared fixtures & fakes
# ---------------------------------------------------------------------------


@pytest.fixture(autouse=True)
def _isolate_clea_data_dir(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """Never touch %LOCALAPPDATA%\\clea from these tests."""
    (tmp_path / "logs").mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("CLEA_DATA_DIR", str(tmp_path))


Round = list[StreamChunk]


def _text(text: str) -> StreamChunk:
    return StreamChunk(type="text_delta", text=text)


def _call(call_id: str, name: str, **arguments: Any) -> StreamChunk:
    return StreamChunk(
        type="tool_call",
        tool_call=ToolCall(id=call_id, name=name, arguments=arguments),
    )


def _done() -> StreamChunk:
    return StreamChunk(type="done")


class _ScriptedProvider(LLMProvider):
    """Replays a fixed list of model rounds; repeats the last round if exhausted."""

    provider_name = "fake"
    model_name = "fake-model"

    def __init__(self, rounds: list[Round]) -> None:
        self._rounds = rounds
        self.calls = 0
        self.system_prompts: list[str] = []

    async def stream_turn(
        self,
        *,
        messages: list[Message],
        tools: list[Any],
        system_prompt: str,
        response_format: dict[str, Any] | None = None,
    ) -> AsyncIterator[StreamChunk]:
        self.system_prompts.append(system_prompt)
        idx = min(self.calls, len(self._rounds) - 1)
        self.calls += 1
        for chunk in self._rounds[idx]:
            yield chunk


class _ToolRecorder:
    """Registers fake tools that record invocations and return scripted results."""

    def __init__(self, registry: ToolRegistry) -> None:
        self.registry = registry
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def add(
        self,
        name: str,
        result: Any | Callable[..., Any] = None,
    ) -> None:
        def _handler(**kwargs: Any) -> Any:
            self.calls.append((name, dict(kwargs)))
            if callable(result):
                return result(**kwargs)
            if result is None:
                return {"ok": True, "status": "completed"}
            return result

        self.registry.register(
            name=name,
            description=f"fake {name}",
            input_schema={"type": "object", "properties": {}, "additionalProperties": True},
            risk_level=RiskLevel.READ_ONLY,
            handler=_handler,
        )

    def names(self) -> list[str]:
        return [n for n, _ in self.calls]


def _make_agent(
    tmp_path: Path,
    rounds: list[Round],
    **agent_kwargs: Any,
) -> tuple[AgentLoop, _ScriptedProvider, _ToolRecorder]:
    provider = _ScriptedProvider(rounds)
    registry = ToolRegistry(audit_logger=AuditLogger(tmp_path / "logs" / "audit.jsonl"))
    recorder = _ToolRecorder(registry)
    mem = MemoryStore(base_dir=tmp_path / "memory")
    agent = AgentLoop(provider=provider, registry=registry, memory=mem, **agent_kwargs)
    return agent, provider, recorder


def _run(
    agent: AgentLoop,
    user_text: str,
    *,
    session: Session | None = None,
) -> tuple[str, list[str], Session]:
    sess = session or Session(id="s-runtime")
    deltas: list[str] = []

    async def _on_delta(text: str) -> None:
        deltas.append(text)

    reply = asyncio.run(agent.run_turn(session=sess, user_text=user_text, on_text_delta=_on_delta))
    return reply, deltas, sess


def _tool_outputs(session: Session) -> dict[str, list[dict[str, Any]]]:
    out: dict[str, list[dict[str, Any]]] = {}
    for msg in session.messages:
        if msg.role == "tool" and msg.tool_name:
            try:
                parsed = json.loads(msg.content)
            except json.JSONDecodeError:
                parsed = {"raw": msg.content}
            out.setdefault(msg.tool_name, []).append(parsed)
    return out


# ---------------------------------------------------------------------------
# Fix 1 (AgentLoop side): no pre-tool streaming / duplicate reply emission
# ---------------------------------------------------------------------------


def test_agent_loop_never_streams_premature_confirmation_from_tool_round(tmp_path: Path) -> None:
    agent, provider, tools = _make_agent(
        tmp_path,
        [
            [
                _text("I've opened YouTube and snapped it left."),
                _call("c1", "manage_window", action="snap_left", title_query="YouTube"),
                _done(),
            ],
            [_text("YouTube is snapped to the left half."), _done()],
        ],
    )
    tools.add(
        "manage_window",
        {"ok": True, "action": "snap_left", "verification": {"verified": True}},
    )

    reply, deltas, session = _run(agent, "snap youtube left")

    assert provider.calls == 2
    assert reply == "YouTube is snapped to the left half."
    # Exactly one confirmation reached the surface, and only after the tool returned.
    assert deltas == ["YouTube is snapped to the left half."]
    assert "opened YouTube" not in "".join(deltas)
    tool_round_msgs = [m for m in session.messages if m.role == "assistant" and m.tool_calls]
    assert len(tool_round_msgs) == 1
    assert tool_round_msgs[0].content == ""
    final_assistant = [m for m in session.messages if m.role == "assistant" and not m.tool_calls]
    assert len(final_assistant) == 1


def test_agent_loop_dedupes_repeated_confirmation_sentences(tmp_path: Path) -> None:
    agent, _provider, _tools = _make_agent(
        tmp_path,
        [[_text("Done, Notepad is open. "), _text("Done, Notepad is open."), _done()]],
    )
    reply, deltas, _ = _run(agent, "hi")
    assert reply == "Done, Notepad is open."
    assert "".join(deltas) == "Done, Notepad is open."


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Opened it. Opened it.", "Opened it."),
        ("Line A\nLine A\nLine B", "Line A\nLine B"),
        ("All good here.", "All good here."),
        ("", ""),
    ],
)
def test_dedupe_response_text(raw: str, expected: str) -> None:
    assert _dedupe_response_text(raw) == expected


def test_agent_prompt_forbids_premature_completion_and_enforces_policies() -> None:
    low = _BASE_SYSTEM_PROMPT.lower()
    assert "before calling the tool and receiving its tool result" in low
    assert "installed-app-first" in low
    assert "never fire both an app launch and a browser url launch" in low
    assert "[OS Window Geometry (Z-order)]" in _BASE_SYSTEM_PROMPT
    assert "extends_behind_foreground" in _BASE_SYSTEM_PROMPT
    assert "detected_slot" in _BASE_SYSTEM_PROMPT
    assert "user_interrupted=true" in _BASE_SYSTEM_PROMPT
    # Pre-existing honesty contract preserved
    assert "aborted_precondition" in _BASE_SYSTEM_PROMPT
    assert "verification.verified=false" in _BASE_SYSTEM_PROMPT


# ---------------------------------------------------------------------------
# Fix 2 (AgentLoop side): normalization, app-first, per-turn single-target dedupe
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("YouTube", "youtube"),
        ("youtube", "youtube"),
        ("https://www.youtube.com", "youtube"),
        ("youtube.com", "youtube"),
        ("https://youtu.be/dQw4w9WgXcQ", "youtube"),
        ("https://m.youtube.com/watch?v=1", "youtube"),
        ("Spotify", "spotify"),
        ("https://open.spotify.com", "spotify"),
        ("WhatsApp", "whatsapp"),
        ("https://web.whatsapp.com", "whatsapp"),
        ("WhatsApp Desktop", "whatsapp"),
        ("VS Code", "vscode"),
        ("code.exe", "vscode"),
        ("https://x.com/home", "twitter"),
        ("", ""),
        (None, ""),
    ],
)
def test_normalize_launch_target(raw: str | None, expected: str) -> None:
    assert normalize_launch_target(raw) == expected


def test_extract_launch_target_kinds() -> None:
    assert _extract_launch_target("open_or_focus_app", {"app_name": "YouTube"}) == (
        "app",
        "youtube",
    )
    assert _extract_launch_target("open_url", {"url": "https://www.youtube.com"}) == (
        "url",
        "youtube",
    )
    assert _extract_launch_target("open_path_or_url", {"target": "youtube.com"}) == (
        "url",
        "youtube",
    )
    file_kind = _extract_launch_target("open_path_or_url", {"target": r"C:\notes\a.txt"})
    assert file_kind is not None and file_kind[0] == "file"
    assert _extract_launch_target("manage_window", {"action": "snap_left"}) is None


def test_user_explicitly_requested_browser_detection() -> None:
    assert _user_explicitly_requested_browser("open youtube in chrome", {}) is True
    assert _user_explicitly_requested_browser("open the youtube website", {}) is True
    assert _user_explicitly_requested_browser("open https://youtube.com", {}) is True
    assert _user_explicitly_requested_browser("open youtube", {"browser": "edge"}) is True
    assert _user_explicitly_requested_browser("open youtube", {}) is False
    assert _user_explicitly_requested_browser("open spotify please", {"browser": ""}) is False


def test_prioritize_app_first_reorders_same_target_url_after_app() -> None:
    url_call = ToolCall(id="u", name="open_url", arguments={"url": "https://www.youtube.com"})
    app_call = ToolCall(id="a", name="open_or_focus_app", arguments={"app_name": "YouTube"})
    other = ToolCall(id="o", name="manage_window", arguments={"action": "list"})
    ordered = _prioritize_app_first_calls([url_call, other, app_call], "open youtube")
    assert [c.id for c in ordered] == ["o", "a", "u"]
    # Explicit browser request keeps the model's order
    kept = _prioritize_app_first_calls([url_call, app_call], "open youtube in the browser")
    assert [c.id for c in kept] == ["u", "a"]


def test_agent_loop_never_fires_app_and_url_launch_for_same_target_in_one_turn(
    tmp_path: Path,
) -> None:
    agent, _provider, tools = _make_agent(
        tmp_path,
        [
            [
                # Model emits URL launch first and app launch second for the same target.
                _call("u1", "open_url", url="https://www.youtube.com"),
                _call("a1", "open_or_focus_app", app_name="YouTube"),
                _done(),
            ],
            [_text("YouTube is open."), _done()],
        ],
    )
    tools.add(
        "open_or_focus_app",
        {"ok": True, "status": "launched", "app": "YouTube", "hwnd": None},
    )
    tools.add("open_url", {"ok": True, "status": "opened", "url": "https://www.youtube.com"})

    reply, _deltas, session = _run(agent, "open youtube")

    assert reply == "YouTube is open."
    assert tools.names() == ["open_or_focus_app"]
    assert tools.calls[0][1] == {"app_name": "YouTube"}
    outputs = _tool_outputs(session)
    assert outputs["open_url"][0]["status"] == "deduplicated"
    assert outputs["open_url"][0]["target"] == "youtube"
    assert outputs["open_url"][0]["deduplicated"] is True


def test_agent_loop_dedupes_repeat_launch_across_rounds(tmp_path: Path) -> None:
    agent, _provider, tools = _make_agent(
        tmp_path,
        [
            [_call("a1", "open_or_focus_app", app_name="Spotify"), _done()],
            [_call("a2", "open_or_focus_app", app_name="spotify"), _done()],
            [_text("Spotify is open."), _done()],
        ],
    )
    tools.add("open_or_focus_app", {"ok": True, "status": "launched", "app": "Spotify"})
    reply, _deltas, session = _run(agent, "open spotify")
    assert reply == "Spotify is open."
    assert tools.names() == ["open_or_focus_app"]
    assert _tool_outputs(session)["open_or_focus_app"][1]["status"] == "deduplicated"


def test_agent_loop_redirects_url_launch_to_installed_app_first(tmp_path: Path) -> None:
    agent, _provider, tools = _make_agent(
        tmp_path,
        [
            [_call("u1", "open_url", url="https://www.youtube.com"), _done()],
            [_text("Opened the YouTube app."), _done()],
        ],
    )
    tools.add("open_or_focus_app", {"ok": True, "status": "launched", "app": "YouTube"})
    tools.add("open_url", {"ok": True, "status": "opened"})

    reply, _deltas, session = _run(agent, "open youtube")

    assert reply == "Opened the YouTube app."
    assert tools.names() == ["open_or_focus_app"]
    assert tools.calls[0][1] == {"app_name": "youtube"}
    assert _tool_outputs(session)["open_url"][0]["status"] == "launched"


def test_agent_loop_falls_back_to_url_when_no_installed_app(tmp_path: Path) -> None:
    agent, _provider, tools = _make_agent(
        tmp_path,
        [
            [_call("u1", "open_url", url="https://www.youtube.com"), _done()],
            [_text("Opened YouTube in your browser."), _done()],
        ],
    )

    def _no_app(**_kw: Any) -> dict[str, Any]:
        raise ValueError("Application 'youtube' could not be resolved to an allowed executable.")

    tools.add("open_or_focus_app", _no_app)
    tools.add("open_url", {"ok": True, "status": "opened", "url": "https://www.youtube.com"})

    reply, _deltas, _session = _run(agent, "open youtube")

    assert tools.names() == ["open_or_focus_app", "open_url"]
    assert reply == "Opened YouTube in your browser."


def test_agent_loop_respects_explicit_browser_request(tmp_path: Path) -> None:
    agent, _provider, tools = _make_agent(
        tmp_path,
        [
            [_call("u1", "open_url", url="https://www.youtube.com", browser="chrome"), _done()],
            [_text("Opened YouTube in Chrome."), _done()],
        ],
    )
    tools.add("open_or_focus_app", {"ok": True, "status": "launched"})
    tools.add("open_url", {"ok": True, "status": "opened"})

    reply, _deltas, _session = _run(agent, "open youtube in chrome")

    assert tools.names() == ["open_url"]
    assert reply == "Opened YouTube in Chrome."


# ---------------------------------------------------------------------------
# Fix 4: honest incomplete outcomes & stop-on-interrupt
# ---------------------------------------------------------------------------


def test_agent_loop_max_turns_reports_incomplete_not_done(tmp_path: Path) -> None:
    agent, provider, tools = _make_agent(
        tmp_path,
        [
            [
                _text("Done! Everything is arranged."),
                _call("c", "manage_window", action="snap_left", title_query="Notepad"),
                _done(),
            ]
        ],
        max_turns=2,
    )
    tools.add("manage_window", {"ok": True, "verification": {"verified": True}})

    reply, deltas, _ = _run(agent, "arrange my windows")

    assert agent.max_turns == 2
    assert provider.calls == 2
    assert "maximum number of tool steps" in reply
    assert "incomplete" in reply.lower()
    assert "Done!" not in reply
    assert deltas == [reply]


def test_agent_loop_max_turns_includes_unrecovered_failure(tmp_path: Path) -> None:
    agent, _provider, tools = _make_agent(
        tmp_path,
        [[_call("c", "manage_window", action="snap_left", title_query="Notepad"), _done()]],
        max_turns=1,
    )
    tools.add(
        "manage_window",
        {"ok": True, "verification": {"verified": False, "reason": "bounds_mismatch"}},
    )
    reply, _, _ = _run(agent, "snap notepad left")
    assert "maximum number of tool steps" in reply
    assert "bounds_mismatch" in reply


@pytest.mark.parametrize(
    "failed_result",
    [
        {"ok": False, "error": "No visible window found matching title_query='Notepad'."},
        {"ok": True, "verification": {"verified": False, "reason": "bounds_mismatch"}},
        {"ok": True, "verified": False, "reason": "state_mismatch"},
        {"ok": False, "status": "aborted_precondition", "reason": "window_minimized"},
    ],
)
def test_agent_loop_overrides_hallucinated_success_after_unrecovered_failure(
    tmp_path: Path,
    failed_result: dict[str, Any],
) -> None:
    agent, _provider, tools = _make_agent(
        tmp_path,
        [
            [_call("c", "manage_window", action="snap_left", title_query="Notepad"), _done()],
            [_text("Done! Notepad is snapped left."), _done()],
        ],
    )
    tools.add("manage_window", failed_result)

    reply, deltas, _ = _run(agent, "snap notepad left")

    assert "Done!" not in reply
    assert "could not complete or verify" in reply
    assert "incomplete" in reply
    assert "manage_window" in reply
    assert deltas == [reply]


def test_agent_loop_tool_exception_is_unrecovered_failure(tmp_path: Path) -> None:
    agent, _provider, tools = _make_agent(
        tmp_path,
        [
            [_call("c", "manage_window", action="maximize", title_query="Calc"), _done()],
            [_text("All set, Calculator is maximized."), _done()],
        ],
    )

    def _boom(**_kw: Any) -> Any:
        raise RuntimeError("user32 unavailable")

    tools.add("manage_window", _boom)
    reply, _, _ = _run(agent, "maximize calculator")
    assert "tool_error" in reply
    assert "All set" not in reply


def test_agent_loop_keeps_honest_failure_text_from_model(tmp_path: Path) -> None:
    agent, _provider, tools = _make_agent(
        tmp_path,
        [
            [_call("c", "manage_window", action="snap_left", title_query="Notepad"), _done()],
            [_text("I couldn't snap Notepad: verification failed (bounds_mismatch)."), _done()],
        ],
    )
    tools.add(
        "manage_window",
        {"ok": True, "verification": {"verified": False, "reason": "bounds_mismatch"}},
    )
    reply, _, _ = _run(agent, "snap notepad left")
    assert reply == "I couldn't snap Notepad: verification failed (bounds_mismatch)."


def test_agent_loop_recovered_failure_allows_success_reply(tmp_path: Path) -> None:
    results = iter(
        [
            {"ok": True, "verification": {"verified": False, "reason": "timeout"}},
            {"ok": True, "verification": {"verified": True, "reason": "window_state_matched"}},
        ]
    )
    agent, _provider, tools = _make_agent(
        tmp_path,
        [
            [_call("c1", "manage_window", action="snap_left", title_query="Notepad"), _done()],
            [_call("c2", "manage_window", action="snap_left", title_query="Notepad"), _done()],
            [_text("Notepad is snapped left."), _done()],
        ],
    )
    tools.add("manage_window", lambda **_kw: next(results))
    reply, _, _ = _run(agent, "snap notepad left")
    assert reply == "Notepad is snapped left."
    assert tools.names() == ["manage_window", "manage_window"]


def test_agent_loop_unavailable_media_verification_is_not_a_failure(tmp_path: Path) -> None:
    agent, _provider, tools = _make_agent(
        tmp_path,
        [
            [_call("c", "system_control", control="media", action="play"), _done()],
            [_text("Sent play to your media player."), _done()],
        ],
    )
    tools.add(
        "system_control",
        {
            "ok": True,
            "verification": {
                "verified": False,
                "status": "unavailable",
                "reason": "winrt_unavailable",
            },
        },
    )
    reply, _, _ = _run(agent, "play music")
    assert reply == "Sent play to your media player."


def test_agent_loop_read_only_failure_does_not_block_success(tmp_path: Path) -> None:
    agent, _provider, tools = _make_agent(
        tmp_path,
        [
            [_call("c", "inspect_window_layout"), _done()],
            [_text("Here is your layout."), _done()],
        ],
    )
    tools.add("inspect_window_layout", {"ok": False, "error": "enum failed"})
    reply, _, _ = _run(agent, "what is my layout")
    assert reply == "Here is your layout."


@pytest.mark.parametrize(
    "interrupt_result",
    [
        {
            "ok": False,
            "status": "aborted_precondition",
            "reason": "focus_lost",
            "aborted_precondition": {
                "reason": "focus_lost",
                "expected_title": "Untitled - Notepad",
                "actual_title": "Windows Security",
            },
        },
        {"ok": False, "status": "stopped", "user_interrupted": True},
        # Serialized string tool output is detected too
        json.dumps({"ok": False, "user_interrupted": True}),
    ],
)
def test_agent_loop_stops_on_interrupt_without_close_reopen_or_spawn(
    tmp_path: Path,
    interrupt_result: Any,
) -> None:
    agent, provider, tools = _make_agent(
        tmp_path,
        [
            [
                _call("c1", "gui_action", action="type_text", text="hi", app_name="notepad"),
                _call("c2", "manage_window", action="close", title_query="Notepad"),
                _call("c3", "open_or_focus_app", app_name="notepad"),
                _done(),
            ],
            [_text("Retried and typed it."), _done()],
        ],
    )
    tools.add("gui_action", interrupt_result)
    tools.add("manage_window", {"ok": True})
    tools.add("open_or_focus_app", {"ok": True, "status": "launched"})

    reply, deltas, session = _run(agent, "type hi in notepad")

    # Only the interrupted tool ran; no close / reopen / spawn and no further model round.
    assert tools.names() == ["gui_action"]
    assert provider.calls == 1
    assert "Action incomplete" in reply
    assert "did not retry, close, or reopen" in reply
    assert "Retried" not in reply
    assert deltas == [reply]
    tool_round = [m for m in session.messages if m.role == "assistant" and m.tool_calls]
    assert [c.id for c in tool_round[0].tool_calls or []] == ["c1"]
    if isinstance(interrupt_result, dict) and "aborted_precondition" in interrupt_result:
        assert "Windows Security" in reply
        assert "focus_lost" in reply


# ---------------------------------------------------------------------------
# Fix 1 (Voice / Daemon / Win32 settle): double-reply & premature confirmation
# ---------------------------------------------------------------------------


@dataclass
class _FakeWindow:
    hwnd: int
    title: str
    process_name: str = "app.exe"
    pid: int = 100
    valid: bool = True
    minimized: bool = False
    maximized: bool = False
    bounds: tuple[int, int, int, int] = (100, 100, 900, 700)
    work_area: tuple[int, int, int, int] = (0, 0, 1920, 1040)


class _DelayedSettleFocusBackend:
    """FocusPreconditionBackend where window bounds settle after N verification polls."""

    def __init__(
        self,
        windows: dict[int, _FakeWindow],
        *,
        foreground_hwnd: int,
        settle_after_polls: int = 3,
    ) -> None:
        self.windows = windows
        self.foreground_hwnd = foreground_hwnd
        self.settle_after_polls = settle_after_polls
        self.pending_bounds: dict[int, tuple[int, int, int, int]] = {}
        self.bounds_polls = 0

    def get_foreground_context(self) -> WindowContext | None:
        return self.get_window_context(self.foreground_hwnd)

    def get_window_context(self, hwnd: int) -> WindowContext | None:
        win = self.windows.get(hwnd)
        if win is None or not win.valid:
            return None
        return WindowContext(
            hwnd=win.hwnd,
            title=win.title,
            process_name=win.process_name,
            pid=win.pid,
            bounds=None if win.minimized else win.bounds,
            work_area=win.work_area,
            minimized=win.minimized,
        )

    def is_window_valid(self, hwnd: int) -> bool:
        win = self.windows.get(hwnd)
        return bool(win is not None and win.valid)

    def is_minimized(self, hwnd: int) -> bool:
        win = self.windows.get(hwnd)
        return bool(win is not None and win.minimized)

    def get_extended_frame_bounds(self, hwnd: int) -> tuple[int, int, int, int] | None:
        win = self.windows.get(hwnd)
        if win is None or not win.valid:
            return None
        self.bounds_polls += 1
        if hwnd in self.pending_bounds and self.bounds_polls >= self.settle_after_polls:
            win.bounds = self.pending_bounds.pop(hwnd)
        return win.bounds

    def get_monitor_work_area(self, hwnd: int) -> tuple[int, int, int, int] | None:
        win = self.windows.get(hwnd)
        if win is None or not win.valid:
            return None
        return win.work_area


class _FakeClock:
    def __init__(self) -> None:
        self.now = 100.0
        self.sleeps: list[float] = []

    def monotonic(self) -> float:
        return self.now

    def sleep(self, dt: float) -> None:
        self.sleeps.append(dt)
        self.now += max(0.02, dt)


class _DelayedMoveUser32:
    """Records MoveWindow and defers bounds update into _DelayedSettleFocusBackend."""

    def __init__(
        self,
        backend: _DelayedSettleFocusBackend,
        *,
        allow_resize: bool = True,
    ) -> None:
        self.backend = backend
        self.allow_resize = allow_resize
        self.moved: list[tuple[int, int, int, int, int]] = []

    def GetForegroundWindow(self) -> int:
        return self.backend.foreground_hwnd

    def IsWindow(self, hwnd: int) -> bool:
        return self.backend.is_window_valid(hwnd)

    def IsWindowVisible(self, hwnd: int) -> bool:
        return self.backend.is_window_valid(hwnd)

    def IsIconic(self, hwnd: int) -> bool:
        return self.backend.is_minimized(hwnd)

    def IsZoomed(self, hwnd: int) -> bool:
        win = self.backend.windows.get(hwnd)
        return bool(win and win.maximized)

    def GetWindowTextLengthW(self, hwnd: int) -> int:
        win = self.backend.windows.get(hwnd)
        return len(win.title) if win else 0

    def GetWindowTextW(self, hwnd: int, buf: Any, max_count: int) -> int:
        win = self.backend.windows.get(hwnd)
        title = win.title if win else ""
        buf.value = title[: max_count - 1]
        return len(buf.value)

    def EnumWindows(self, callback: Any, lparam: int) -> bool:
        for hwnd in list(self.backend.windows):
            if not callback(hwnd, lparam):
                break
        return True

    def MonitorFromWindow(self, hwnd: int, flags: int) -> int:
        return 1

    def GetMonitorInfoW(self, hmon: int, mi_ptr: Any) -> bool:
        win = next(iter(self.backend.windows.values()), None)
        wl, wt, wr, wb = win.work_area if win else (0, 0, 1920, 1040)
        obj = getattr(mi_ptr, "_obj", None)
        if obj is not None:
            obj.rcWork.left, obj.rcWork.top, obj.rcWork.right, obj.rcWork.bottom = wl, wt, wr, wb
        return True

    def ShowWindow(self, hwnd: int, cmd: int) -> bool:
        win = self.backend.windows.get(hwnd)
        if win is not None and cmd in (1, 9):
            win.minimized = False
            win.maximized = False
        return True

    def MoveWindow(self, hwnd: int, x: int, y: int, w: int, h: int, repaint: bool) -> bool:
        self.moved.append((hwnd, x, y, w, h))
        if self.allow_resize:
            self.backend.pending_bounds[hwnd] = (x, y, x + w, y + h)
        return True

    def SetForegroundWindow(self, hwnd: int) -> bool:
        if hwnd in self.backend.windows:
            self.backend.foreground_hwnd = hwnd
            return True
        return False


def test_manage_window_and_control_system_wait_for_verify_window_state_to_settle() -> None:
    notepad = _FakeWindow(
        hwnd=501,
        title="Untitled - Notepad",
        process_name="notepad.exe",
        bounds=(200, 150, 1000, 750),
        work_area=(0, 0, 1920, 1040),
    )
    backend = _DelayedSettleFocusBackend(
        {501: notepad},
        foreground_hwnd=501,
        settle_after_polls=3,
    )
    u32 = _DelayedMoveUser32(backend, allow_resize=True)
    clock = _FakeClock()
    verifier = ActionVerifier(
        focus_backend=backend,
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
    )

    res_manage = manage_window(
        "snap_left",
        title_query="Notepad",
        user32=u32,
        focus_backend=backend,
        verifier=verifier,
        verify_timeout_s=1.0,
    )
    assert res_manage["ok"] is True
    assert res_manage["verified"] is True
    assert res_manage["verification"]["verified"] is True
    assert backend.bounds_polls >= 3
    assert notepad.bounds == (0, 0, 960, 1040)

    # Now test control_system snap_right waiting for ActionVerifier.verify_window_state to settle
    backend.bounds_polls = 0
    res_ctrl = control_system(
        "snap_right",
        app_name="notepad",
        user32=u32,
        window_enumerator=lambda: [
            EnumeratedWindow(
                hwnd=501,
                title="Untitled - Notepad",
                process_name="notepad.exe",
                is_visible=True,
            )
        ],
        focus_backend=backend,
        verifier=verifier,
        verify_timeout_s=1.0,
    )
    assert res_ctrl["ok"] is True
    assert res_ctrl["verified"] is True
    assert res_ctrl["verification"]["verified"] is True
    assert backend.bounds_polls >= 3
    assert notepad.bounds == (960, 0, 1920, 1040)


def test_daemon_os_control_system_waits_for_verifier_and_marks_unverified_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    locked = _FakeWindow(
        hwnd=777,
        title="Locked Window",
        process_name="locked.exe",
        bounds=(100, 100, 500, 500),
        work_area=(0, 0, 1920, 1040),
    )
    backend = _DelayedSettleFocusBackend({777: locked}, foreground_hwnd=777, settle_after_polls=99)
    clock = _FakeClock()
    verifier = ActionVerifier(
        focus_backend=backend,
        monotonic_fn=clock.monotonic,
        sleep_fn=clock.sleep,
    )

    # Simulate control_system returning without verification dict so daemon runs settle check
    monkeypatch.setattr(
        "clea.core.daemon.control_system",
        lambda action, **_kw: {"ok": True, "success": True, "action": action, "hwnd": 777},
    )

    daemon = build_daemon(
        memory=MemoryStore(base_dir=tmp_path / "memory"),
        provider=SimpleNamespace(provider_name="fake", model_name="fake"),  # type: ignore[arg-type]
        focus_backend=backend,
        action_verifier=verifier,
    )

    class _FakeConn:
        def __init__(self) -> None:
            self.sent: list[str] = []
            self.ws = self
            self.current_session_id: str | None = None
            self.is_voice = True

        async def send(self, payload: str) -> None:
            self.sent.append(payload)

    conn = _FakeConn()
    req = parse_frame(
        json.dumps(
            {
                "frame_type": "request",
                "id": "req-snap-verify",
                "method": "os.control_system",
                "params": {"action": "snap_left", "hwnd": 777},
            }
        )
    )
    asyncio.run(daemon._dispatch_request(conn, req))
    frame = json.loads(conn.sent[0])
    assert frame["ok"] is True
    result = frame["result"]
    assert result["verification"]["verified"] is False
    assert result["ok"] is False
    assert result["status"] == "verification_failed"


async def test_gemini_live_backend_suppresses_pre_tool_content_and_dedupes_events() -> None:
    backend = GeminiLiveBackend(api_key="fake")

    # 1. Server message containing BOTH speculative pre-tool audio/text AND tool_call
    #    must emit ONLY ToolCallEvent (suppressing premature audio, text, and turn_complete).
    pre_tool_msg = genai_types.LiveServerMessage(
        server_content=genai_types.LiveServerContent(
            model_turn=genai_types.Content(
                parts=[
                    genai_types.Part(
                        inline_data=genai_types.Blob(
                            data=b"\x01\x02" * 40,
                            mime_type="audio/pcm;rate=24000",
                        )
                    ),
                    genai_types.Part(text="Done! Opening YouTube now."),
                ]
            ),
            output_transcription=genai_types.Transcription(
                text="Done! Opening YouTube now.",
                finished=True,
            ),
            turn_complete=True,
        ),
        tool_call=genai_types.LiveServerToolCall(
            function_calls=[
                genai_types.FunctionCall(
                    id="fc-1",
                    name="open_or_focus_app",
                    args={"app_name": "YouTube"},
                )
            ]
        ),
    )
    await backend._handle_server_message(pre_tool_msg)

    emitted: list[VoiceBackendEvent] = []
    while not backend._events_queue.empty():
        emitted.append(backend._events_queue.get_nowait())
    assert len(emitted) == 1
    assert isinstance(emitted[0], ToolCallEvent)
    assert emitted[0].call_id == "fc-1"
    assert not any(
        isinstance(e, (AudioOutputEvent, TranscriptOutputEvent, TurnCompleteEvent))
        for e in emitted
    )

    # 2. Post-tool reply with identical model_turn text + output_transcription + duplicate
    #    turn_complete / IDLE frames must emit only one transcript and one TurnCompleteEvent.
    reply_msg = genai_types.LiveServerMessage(
        server_content=genai_types.LiveServerContent(
            model_turn=genai_types.Content(
                parts=[genai_types.Part(text="YouTube is open.")]
            ),
            output_transcription=genai_types.Transcription(
                text="YouTube is open.",
                finished=False,
            ),
            turn_complete=True,
        )
    )
    dup_idle_msg = genai_types.LiveServerMessage(
        server_content=genai_types.LiveServerContent(
            interaction_status=genai_types.InteractionStatus.IDLE,
        )
    )
    await backend._handle_server_message(reply_msg)
    await backend._handle_server_message(dup_idle_msg)

    post_events: list[VoiceBackendEvent] = []
    while not backend._events_queue.empty():
        post_events.append(backend._events_queue.get_nowait())

    transcripts = [e for e in post_events if isinstance(e, TranscriptOutputEvent)]
    turn_completes = [e for e in post_events if isinstance(e, TurnCompleteEvent)]
    assert len(transcripts) == 1
    assert transcripts[0].text == "YouTube is open."
    assert len(turn_completes) == 1


class _BatchFakeVoiceBackend(VoiceBackend):
    backend_name = "fake-live"

    def __init__(self) -> None:
        self.event_queue: asyncio.Queue[VoiceBackendEvent | None] = asyncio.Queue()
        self.response_batches: list[list[VoiceToolResponse]] = []

    @property
    def model_name(self) -> str:
        return "fake-live"

    async def connect(self, config: VoiceSessionConfig) -> None:
        return None

    async def close(self) -> None:
        await self.event_queue.put(None)

    async def send_audio(self, pcm_bytes: bytes) -> None:
        return None

    async def send_video_frame(self, image_bytes: bytes, *, mime_type: str = "image/jpeg") -> None:
        return None

    async def send_text(self, text: str, *, end_of_turn: bool = True) -> None:
        return None

    async def send_tool_responses(self, responses: list[VoiceToolResponse]) -> None:
        self.response_batches.append(list(responses))

    async def events(self) -> AsyncIterator[VoiceBackendEvent]:
        while True:
            evt = await self.event_queue.get()
            if evt is None:
                break
            yield evt


def _make_voice_runner(tmp_path: Path) -> tuple[VoiceRunner, AudioManager]:
    audio = AudioManager()
    audio.start = MagicMock()  # type: ignore[method-assign]
    audio.stop = MagicMock()  # type: ignore[method-assign]
    audio.play_audio = MagicMock()  # type: ignore[method-assign]
    audio.stop_output = MagicMock()  # type: ignore[method-assign]
    audio.set_input_callback = MagicMock()  # type: ignore[method-assign]
    mock_ww_model = MagicMock()
    mock_ww_model.predict.return_value = {"hey_jarvis_v0.1": 0.0}
    runner = VoiceRunner(
        config=VoiceConfig(silence_timeout_seconds=60.0),
        data_dir=tmp_path,
        audio_manager=audio,
        wake_word_detector=WakeWordDetector(model_instance=mock_ww_model),
        enable_hotkey=False,
    )
    return runner, audio


async def test_voice_runner_batches_tool_calls_prioritizes_app_first_and_dedupes_launch(
    tmp_path: Path,
) -> None:
    runner, _audio = _make_voice_runner(tmp_path)
    backend = _BatchFakeVoiceBackend()
    rpc_requests: list[Any] = []

    async def _fake_rpc(req: Any, *, timeout: float = 10.0) -> dict[str, Any]:
        rpc_requests.append(req)
        if isinstance(req, OsOpenOrFocusAppRequest):
            return {
                "ok": True,
                "status": "launched",
                "app": req.params.app_name,
                "hwnd": None,
            }
        if isinstance(req, OsOpenUrlRequest):
            return {"ok": True, "status": "opened", "url": req.params.url}
        return {"ok": True}

    runner._rpc = _fake_rpc  # type: ignore[method-assign]

    # Queue open_url BEFORE open_or_focus_app for the same target ("youtube")
    ev_url = ToolCallEvent(
        call_id="c-url",
        name="open_url",
        arguments={"url": "https://www.youtube.com"},
    )
    ev_app = ToolCallEvent(
        call_id="c-app",
        name="open_or_focus_app",
        arguments={"app_name": "YouTube"},
    )
    await backend.event_queue.put(ev_app)

    batch = runner._drain_contiguous_tool_calls(backend, ev_url)
    assert [e.call_id for e in batch] == ["c-app", "c-url"]

    await runner._execute_tool_call_batch(batch, backend)

    # Atomic single send_tool_responses call prevents Gemini Live from speaking twice.
    assert len(backend.response_batches) == 1
    sent = {r.call_id: r.response for r in backend.response_batches[0]}
    assert set(sent) == {"c-app", "c-url"}
    assert sent["c-app"]["status"] == "launched"
    assert sent["c-url"]["status"] == "deduplicated"
    assert sent["c-url"]["deduplicated"] is True
    assert len(rpc_requests) == 1
    assert isinstance(rpc_requests[0], OsOpenOrFocusAppRequest)

    # Resetting turn state allows launching again in the next turn.
    runner.reset_turn_state()
    await runner._handle_tool_call(
        ToolCallEvent(
            call_id="c-url-next-turn",
            name="open_url",
            arguments={"url": "https://www.youtube.com"},
        ),
        backend,
    )
    assert isinstance(rpc_requests[-1], OsOpenOrFocusAppRequest)
    assert rpc_requests[-1].params.app_name == "youtube"


def test_daemon_chat_turn_sets_request_id_on_conversation_updated(tmp_path: Path) -> None:
    agent, _provider, _tools = _make_agent(tmp_path, [[_text("Single reply."), _done()]])
    daemon = build_daemon(
        memory=agent.memory,
        provider=agent.provider,
        registry=agent.registry,
    )

    class _FakeConn:
        def __init__(self) -> None:
            self.ws = self
            self.current_session_id: str | None = None
            self.is_voice = False
            self.tasks: list[asyncio.Task[Any]] = []
            self.approvals: dict[str, Any] = {}
            self.frames: list[Any] = []

        def spawn(self, coro: Any) -> None:
            self.tasks.append(asyncio.ensure_future(coro))

        async def send(self, payload: str) -> None:
            self.frames.append(parse_frame(payload))

    origin_conn = _FakeConn()
    peer_conn = _FakeConn()
    daemon._connections.add(origin_conn)  # type: ignore[arg-type]
    daemon._connections.add(peer_conn)  # type: ignore[arg-type]
    req = parse_frame(
        json.dumps(
            {
                "frame_type": "request",
                "id": "req-chat-1",
                "method": "chat.send",
                "params": {"text": "hello"},
            }
        )
    )

    async def _run_chat_req() -> None:
        await daemon._dispatch_request(origin_conn, req)  # type: ignore[arg-type]
        if origin_conn.tasks:
            await asyncio.gather(*origin_conn.tasks)

    asyncio.run(_run_chat_req())

    # Originating connection does NOT receive a duplicate ConversationUpdatedEvent
    assert not any(isinstance(f, ConversationUpdatedEvent) for f in origin_conn.frames)
    # Peer UI connection receives ConversationUpdatedEvent with top-level request_id
    peer_updates = [f for f in peer_conn.frames if isinstance(f, ConversationUpdatedEvent)]
    assert len(peer_updates) == 1
    assert peer_updates[0].request_id == "req-chat-1"
    assert peer_updates[0].data.assistant_message == "Single reply."


def test_voice_system_instruction_covers_reliability_rules() -> None:
    assert "Single Post-Tool Confirmation Rule" in BASE_VOICE_SYSTEM_INSTRUCTION
    assert "Installed-App-First Policy" in BASE_VOICE_SYSTEM_INSTRUCTION
    assert "[OS Window Geometry (Z-order)]" in BASE_VOICE_SYSTEM_INSTRUCTION
    assert "extends_behind_foreground" in BASE_VOICE_SYSTEM_INSTRUCTION
    assert voice_normalize_launch_target("https://www.youtube.com") == "youtube"


# ---------------------------------------------------------------------------
# Fix 2 (windows_os tools side): App-First resolution & normalization
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("YouTube", "youtube"),
        ("https://www.youtube.com", "youtube"),
        ("youtube.com", "youtube"),
        ("https://youtu.be/abc", "youtube"),
        ("Spotify", "spotify"),
        ("https://open.spotify.com", "spotify"),
        ("WhatsApp", "whatsapp"),
        ("https://web.whatsapp.com", "whatsapp"),
        ("open YouTube in Chrome", "youtube"),
    ],
)
def test_tools_normalize_launch_target(raw: str, expected: str) -> None:
    assert tools_normalize_launch_target(raw) == expected


def test_open_url_prefers_installed_app_unless_browser_explicitly_requested(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(windows_os, "_INSTALLED_APPS_CACHE", None)
    monkeypatch.setattr(windows_os, "_INSTALLED_APPS_CACHE_TIME", 0.0)

    catalog = [
        {"Name": "YouTube", "AppID": "Chrome._crx_agimnkijcaahngcdmfeangaknmldooml"},
        {"Name": "Spotify", "AppID": "SpotifyAB.SpotifyMusic_zpdnekdrzrea0!Spotify"},
    ]
    launched_cmds: list[list[str]] = []
    shell_urls: list[str] = []

    class _MinUser32:
        def GetForegroundWindow(self) -> int:
            return 0

    # 1. Default open_url("https://www.youtube.com") resolves installed YouTube app first
    res_app = open_url(
        "https://www.youtube.com",
        app_catalog_fetcher=lambda: catalog,
        user32=_MinUser32(),
        window_enumerator=list,
        popen_fn=lambda cmd, **_kw: launched_cmds.append(list(cmd)),
        startfile_fn=shell_urls.append,
        desktop_name_fn=lambda: "Default",
    )
    assert res_app["ok"] is True
    assert res_app["launched_app"] is True
    assert res_app["normalized_target"] == "youtube"
    assert res_app["app"] == "YouTube"
    assert shell_urls == []
    assert len(launched_cmds) == 1
    assert launched_cmds[0][0] == "explorer.exe"
    assert "shell:AppsFolder\\" in launched_cmds[0][1]

    # 2. Explicit browser request opens browser URL instead of installed app
    assert is_explicit_browser_request("open YouTube in Chrome") is True
    launched_cmds.clear()
    res_browser = open_url(
        "https://www.youtube.com",
        user_request="open YouTube in Chrome",
        app_catalog_fetcher=lambda: catalog,
        which_fn=lambda name: r"C:\Chrome\chrome.exe" if name == "chrome.exe" else None,
        popen_fn=lambda cmd, **_kw: launched_cmds.append(list(cmd)),
        startfile_fn=shell_urls.append,
        desktop_name_fn=lambda: "Default",
    )
    assert res_browser["ok"] is True
    assert res_browser["launched_app"] is False
    assert res_browser["status"] == "opened"
    assert launched_cmds == [[r"C:\Chrome\chrome.exe", "https://www.youtube.com"]]

    # 3. No installed app match -> falls back cleanly to default browser shell launch
    monkeypatch.setattr(windows_os, "_INSTALLED_APPS_CACHE", None)
    res_fallback = open_url(
        "https://www.youtube.com",
        app_catalog_fetcher=list,
        startfile_fn=shell_urls.append,
        desktop_name_fn=lambda: "Default",
    )
    assert res_fallback["ok"] is True
    assert res_fallback["launched_app"] is False
    assert res_fallback["status"] == "opened"
    assert shell_urls == ["https://www.youtube.com"]


# ---------------------------------------------------------------------------
# Fix 3: Win32 layout ground truth (inspect_window_layout) & Vision metadata
# ---------------------------------------------------------------------------


def test_inspect_window_layout_detects_occluded_full_width_behind_right_half() -> None:
    layout = inspect_window_layout(
        windows=[
            {
                "hwnd": 1001,
                "title": "Untitled - Notepad",
                "app_name": "Notepad",
                "process_name": "notepad.exe",
                "bounds": (960, 0, 1920, 1040),
                "rcWork": (0, 0, 1920, 1040),
                "is_maximized": False,
            },
            {
                "hwnd": 1002,
                "title": "YouTube",
                "app_name": "YouTube",
                "process_name": "chrome.exe",
                "bounds": (0, 0, 1920, 1040),
                "rcWork": (0, 0, 1920, 1040),
                "is_maximized": False,
            },
        ]
    )

    assert layout["ok"] is True
    assert layout["count"] == 2
    fg, bg = layout["windows"]
    assert fg["z_index"] == 0
    assert fg["title"] == "Untitled - Notepad"
    assert fg["detected_slot"] == "right_half"
    assert fg["width_ratio"] == pytest.approx(0.5, abs=0.01)
    assert fg["extends_behind_foreground"] is False

    assert bg["z_index"] == 1
    assert bg["title"] == "YouTube"
    assert bg["detected_slot"] == "full_width"
    assert bg["width_ratio"] == pytest.approx(1.0, abs=0.01)
    assert bg["extends_behind_foreground"] is True

    summary = layout["summary"]
    assert summary.startswith("[OS Window Geometry (Z-order)]")
    assert "detected_slot=right_half" in summary
    assert "detected_slot=full_width" in summary
    assert "extends_behind_foreground=True" in summary
    assert format_window_layout_summary(layout) == summary


def test_inspect_window_layout_true_50_50_split_maximized_and_custom() -> None:
    split = inspect_window_layout(
        windows=[
            {
                "hwnd": 2001,
                "title": "Notepad",
                "app_name": "Notepad",
                "bounds": (960, 0, 1920, 1040),
                "rcWork": (0, 0, 1920, 1040),
                "is_maximized": False,
            },
            {
                "hwnd": 2002,
                "title": "YouTube",
                "app_name": "YouTube",
                "bounds": (0, 0, 960, 1040),
                "rcWork": (0, 0, 1920, 1040),
                "is_maximized": False,
            },
            {
                "hwnd": 2003,
                "title": "Calculator",
                "app_name": "Calculator",
                "bounds": (100, 100, 500, 600),
                "rcWork": (0, 0, 1920, 1040),
                "is_maximized": False,
            },
            {
                "hwnd": 2004,
                "title": "Visual Studio Code",
                "app_name": "Visual Studio Code",
                "bounds": (0, 0, 1920, 1040),
                "rcWork": (0, 0, 1920, 1040),
                "is_maximized": True,
            },
        ]
    )
    by_title = {w["title"]: w for w in split["windows"]}
    assert by_title["Notepad"]["detected_slot"] == "right_half"
    assert by_title["Notepad"]["extends_behind_foreground"] is False
    assert by_title["YouTube"]["detected_slot"] == "left_half"
    assert by_title["YouTube"]["extends_behind_foreground"] is False
    assert by_title["Calculator"]["detected_slot"] == "custom"
    # Background maximized window occluded by foreground right_half reports full_width + occluded
    assert by_title["Visual Studio Code"]["detected_slot"] == "full_width"
    assert by_title["Visual Studio Code"]["is_maximized"] is True
    assert by_title["Visual Studio Code"]["extends_behind_foreground"] is True

    # Foreground maximized window reports detected_slot="maximized"
    fg_max = inspect_window_layout(
        windows=[
            {
                "hwnd": 2004,
                "title": "Visual Studio Code",
                "app_name": "Visual Studio Code",
                "bounds": (0, 0, 1920, 1040),
                "rcWork": (0, 0, 1920, 1040),
                "is_maximized": True,
            }
        ]
    )
    assert fg_max["windows"][0]["detected_slot"] == "maximized"
    assert fg_max["windows"][0]["extends_behind_foreground"] is False


def test_inspect_window_layout_reads_dwmwa_extended_frame_bounds_via_win32_doubles() -> None:
    rects = {
        3001: (960, 0, 1920, 1040),
        3002: (0, 0, 1920, 1040),
    }
    titles = {
        3001: "Untitled - Notepad",
        3002: "YouTube",
    }

    class _FakeUser32:
        def GetForegroundWindow(self) -> int:
            return 3001

        def EnumWindows(self, cb: Any, lparam: int) -> bool:
            for h in (3001, 3002):
                cb(h, lparam)
            return True

        def IsWindowVisible(self, hwnd: int) -> bool:
            return hwnd in titles

        def IsIconic(self, hwnd: int) -> bool:
            return False

        def IsZoomed(self, hwnd: int) -> bool:
            return False

        def GetWindowTextLengthW(self, hwnd: int) -> int:
            return len(titles.get(hwnd, ""))

        def GetWindowTextW(self, hwnd: int, buf: Any, max_len: int) -> int:
            t = titles.get(hwnd, "")
            buf.value = t[: max_len - 1]
            return len(buf.value)

        def MonitorFromWindow(self, hwnd: int, flags: int) -> int:
            return 1

        def GetMonitorInfoW(self, hmon: int, mi_ptr: Any) -> bool:
            obj = getattr(mi_ptr, "_obj", None)
            if obj is not None:
                obj.rcWork.left, obj.rcWork.top, obj.rcWork.right, obj.rcWork.bottom = (
                    0,
                    0,
                    1920,
                    1040,
                )
            return True

    class _FakeDwm:
        def DwmGetWindowAttribute(self, hwnd: int, attr: int, rect_ptr: Any, size: int) -> int:
            assert attr == 9  # DWMWA_EXTENDED_FRAME_BOUNDS
            l_v, t_v, r_v, b_v = rects[hwnd]
            obj = getattr(rect_ptr, "_obj", None)
            if obj is not None:
                obj.left, obj.top, obj.right, obj.bottom = l_v, t_v, r_v, b_v
            return 0

    layout = inspect_window_layout(user32=_FakeUser32(), dwmapi=_FakeDwm())
    assert layout["count"] == 2
    assert layout["windows"][0]["detected_slot"] == "right_half"
    assert layout["windows"][1]["detected_slot"] == "full_width"
    assert layout["windows"][1]["extends_behind_foreground"] is True


def test_screen_vision_attaches_os_window_geometry_metadata(tmp_path: Path) -> None:
    png_buf = io.BytesIO()
    Image.new("RGB", (64, 48), color=(30, 40, 50)).save(png_buf, format="PNG")
    png_bytes = png_buf.getvalue()

    fg_win = WindowInfo(
        hwnd=4001,
        title="Untitled - Notepad",
        process_name="notepad.exe",
        app_name="Notepad",
        bounds=(960, 0, 1920, 1040),
    )

    class _FakeVisionBackend:
        def get_foreground_window(self) -> WindowInfo:
            return fg_win

        def find_window(self, query: str) -> WindowInfo | None:
            return fg_win if "notepad" in query.lower() else None

        def is_minimized(self, hwnd: int) -> bool:
            return False

        def probe_uia_text(self, hwnd: int, max_chars: int = 3000) -> str:
            return "hello"

        def capture_window_png(self, hwnd: int) -> WindowCapture:
            return WindowCapture(
                png_bytes=png_bytes,
                width=64,
                height=48,
                window_title=fg_win.title,
                process_name=fg_win.process_name,
            )

    def _layout_inspector() -> dict[str, Any]:
        return inspect_window_layout(
            windows=[
                {
                    "hwnd": 4001,
                    "title": "Untitled - Notepad",
                    "app_name": "Notepad",
                    "bounds": (960, 0, 1920, 1040),
                    "rcWork": (0, 0, 1920, 1040),
                },
                {
                    "hwnd": 4002,
                    "title": "YouTube",
                    "app_name": "YouTube",
                    "bounds": (0, 0, 1920, 1040),
                    "rcWork": (0, 0, 1920, 1040),
                },
            ]
        )

    service = ScreenVisionService(
        backend=_FakeVisionBackend(),
        artifact_store=ArtifactStore(data_dir=tmp_path),
        desktop_name_fn=lambda: "Default",
        layout_inspector=_layout_inspector,
    )
    result = inspect_screen(target="active", question="Are my windows split?", service=service)

    assert result["status"] == "ok"
    assert "[OS Window Geometry (Z-order)]" in result["message"]
    assert "[OS Window Geometry (Z-order)]" in result["os_window_geometry"]
    assert "detected_slot=right_half" in result["os_window_geometry"]
    assert "detected_slot=full_width" in result["os_window_geometry"]
    assert "extends_behind_foreground=True" in result["os_window_geometry"]
    assert result["window_layout"]["windows"][1]["extends_behind_foreground"] is True

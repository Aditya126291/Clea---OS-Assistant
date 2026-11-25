"""Deterministic unit tests for the unified Chrome/PWA CDP bridge, bounded native UIA,
DPI-exact coordinate scaling, harness state-diff verification, and 2-click circuit breaker.

Zero real Chrome connections, zero live Win32 input side effects, zero writes to
%LOCALAPPDATA%\\clea.
"""

from __future__ import annotations

import asyncio
import json
import subprocess
import time
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest
from PIL import Image

from clea.core.agent import _BASE_SYSTEM_PROMPT, AgentLoop
from clea.core.protocol import OsGuiActionRequest, OsOpenUrlRequest
from clea.core.sessions import Session
from clea.memory.store import MemoryStore
from clea.providers.base import LLMProvider, Message, StreamChunk, ToolCall
from clea.surfaces.voice.backends.base import (
    ToolCallEvent,
    VoiceBackend,
    VoiceBackendEvent,
    VoiceSessionConfig,
    VoiceToolResponse,
)
from clea.surfaces.voice.config import VoiceConfig
from clea.surfaces.voice.memory_hooks import BASE_VOICE_SYSTEM_INSTRUCTION
from clea.surfaces.voice.runner import VoiceRunner
from clea.surfaces.voice.screen import ScreenCaptureService
from clea.surfaces.voice.win32 import MonitorInfo, Win32API
from clea.tools import windows_os
from clea.tools.action_harness import (
    ActionCircuitBreaker,
    capture_desktop_state,
    compute_state_diff,
)
from clea.tools.cdp_bridge import (
    CDP_REMOTE_DEBUGGING_HINT,
    CDPBridge,
    parse_devtools_active_port_file,
)
from clea.tools.guards import MediaSessionState, WindowContext
from clea.tools.registry import RiskLevel, ToolRegistry

# ===========================================================================
# Mock Browser-Level CDP WebSocket Server / Transport Double
# ===========================================================================


class MockCDPBrowserState:
    """In-memory browser-level CDP simulator supporting flattened target sessions."""

    def __init__(self) -> None:
        self.targets: dict[str, dict[str, Any]] = {
            "target-yt-pwa": {
                "targetId": "target-yt-pwa",
                "type": "page",
                "url": "https://www.youtube.com/",
                "title": "YouTube",
                "is_pwa": True,
                "window_id": 10,
                "window_state": "minimized",
                "has_focus": False,
                "history": [
                    {"id": 1, "url": "https://www.youtube.com/", "title": "YouTube"},
                ],
                "history_index": 0,
                "ax_nodes": [
                    {
                        "nodeId": "1",
                        "ignored": True,
                        "role": {"value": "RootWebArea"},
                        "name": {"value": "YouTube"},
                    },
                    {
                        "nodeId": "2",
                        "ignored": False,
                        "role": {"value": "link"},
                        "name": {"value": "Watch Later"},
                        "backendDOMNodeId": 101,
                        "properties": [{"name": "focused", "value": {"value": False}}],
                    },
                    {
                        "nodeId": "3",
                        "ignored": False,
                        "role": {"value": "searchbox"},
                        "name": {"value": "Search"},
                        "value": {"value": ""},
                        "backendDOMNodeId": 102,
                        "properties": [],
                    },
                    {
                        "nodeId": "4",
                        "ignored": False,
                        "role": {"value": "button"},
                        "name": {"value": "Hidden Noise"},
                        "backendDOMNodeId": 103,
                        "properties": [{"name": "hidden", "value": {"value": True}}],
                    },
                    {
                        "nodeId": "5",
                        "ignored": False,
                        "role": {"value": "button"},
                        "name": {"value": "   "},
                        "backendDOMNodeId": 104,
                        "properties": [],
                    },
                    {
                        "nodeId": "6",
                        "ignored": False,
                        "role": {"value": "heading"},
                        "name": {"value": "Home Feed"},
                        "backendDOMNodeId": 105,
                        "properties": [],
                    },
                ],
            },
            "target-chrome-tab": {
                "targetId": "target-chrome-tab",
                "type": "page",
                "url": "https://www.youtube.com/feed/subscriptions",
                "title": "Subscriptions - YouTube",
                "is_pwa": False,
                "window_id": 20,
                "window_state": "normal",
                "has_focus": True,
                "history": [
                    {
                        "id": 10,
                        "url": "https://www.youtube.com/",
                        "title": "YouTube",
                    },
                    {
                        "id": 11,
                        "url": "https://www.youtube.com/feed/subscriptions",
                        "title": "Subscriptions - YouTube",
                    },
                ],
                "history_index": 1,
                "ax_nodes": [
                    {
                        "nodeId": "20",
                        "ignored": False,
                        "role": {"value": "button"},
                        "name": {"value": "Play All"},
                        "backendDOMNodeId": 201,
                        "properties": [],
                    }
                ],
            },
        }
        self.commands_log: list[dict[str, Any]] = []
        self.connected_urls: list[str] = []
        self.next_session_num = 1
        self.sessions: dict[str, str] = {}
        self.fail_connect_with: Exception | None = None
        self.inserted_texts: list[str] = []
        self.dispatched_keys: list[str] = []
        self.mouse_events: list[dict[str, Any]] = []

    def connector(self) -> Any:
        state = self

        async def _connect(ws_url: str, *, open_timeout: float = 1.5) -> _MockCDPWebSocket:
            state.connected_urls.append(ws_url)
            if state.fail_connect_with is not None:
                raise state.fail_connect_with
            return _MockCDPWebSocket(state)

        return _connect

    def handle_command(self, frame: dict[str, Any]) -> dict[str, Any]:
        self.commands_log.append(frame)
        msg_id = frame["id"]
        method = frame["method"]
        params = frame.get("params") or {}
        session_id = frame.get("sessionId")

        def _ok(result: dict[str, Any]) -> dict[str, Any]:
            return {"id": msg_id, "result": result}

        if method == "Target.getTargets":
            return _ok(
                {
                    "targetInfos": [
                        {
                            "targetId": t["targetId"],
                            "type": t["type"],
                            "url": t["url"],
                            "title": t["title"],
                        }
                        for t in self.targets.values()
                    ]
                }
            )

        if method == "Target.attachToTarget":
            assert params.get("flatten") is True, "Must attach with flatten=True on browser WS"
            tid = params["targetId"]
            sid = f"sess-{self.next_session_num}"
            self.next_session_num += 1
            self.sessions[sid] = tid
            return _ok({"sessionId": sid})

        if method == "Target.detachFromTarget":
            sid = params.get("sessionId")
            if sid:
                self.sessions.pop(sid, None)
            return _ok({})

        if method == "Target.activateTarget":
            tid = params["targetId"]
            for k, t in self.targets.items():
                t["has_focus"] = k == tid
            return _ok({})

        if method == "Target.createTarget":
            url = params.get("url", "about:blank")
            new_tid = f"target-created-{len(self.targets) + 1}"
            self.targets[new_tid] = {
                "targetId": new_tid,
                "type": "page",
                "url": url,
                "title": f"Tab: {url}",
                "is_pwa": False,
                "window_id": 20,
                "window_state": "normal",
                "has_focus": True,
                "history": [{"id": 100, "url": url, "title": f"Tab: {url}"}],
                "history_index": 0,
                "ax_nodes": [],
            }
            return _ok({"targetId": new_tid})

        if method == "Browser.getWindowForTarget":
            tid = params["targetId"]
            t = self.targets[tid]
            return _ok(
                {
                    "windowId": t["window_id"],
                    "bounds": {
                        "left": 0,
                        "top": 0,
                        "width": 1280,
                        "height": 800,
                        "windowState": t["window_state"],
                    },
                }
            )

        if method == "Browser.setWindowBounds":
            wid = params["windowId"]
            new_state = (params.get("bounds") or {}).get("windowState", "normal")
            for t in self.targets.values():
                if t["window_id"] == wid:
                    t["window_state"] = new_state
            return _ok({})

        # Flattened session commands require sessionId
        assert session_id and session_id in self.sessions, f"Missing/unknown sessionId for {method}"
        tid = self.sessions[session_id]
        target = self.targets[tid]

        if method == "Runtime.evaluate":
            return _ok(
                {
                    "result": {
                        "type": "object",
                        "value": {
                            "url": target["url"],
                            "title": target["title"],
                            "readyState": "complete",
                            "hasFocus": target["has_focus"],
                            "visibilityState": "visible",
                            "isPwa": target["is_pwa"],
                        },
                    }
                }
            )

        if method == "Page.navigate":
            new_url = params["url"]
            new_title = (
                "Watch Later - YouTube"
                if "list=WL" in new_url
                else f"Page: {new_url}"
            )
            target["url"] = new_url
            target["title"] = new_title
            new_id = len(target["history"]) + 100
            target["history"] = target["history"][: target["history_index"] + 1]
            target["history"].append({"id": new_id, "url": new_url, "title": new_title})
            target["history_index"] = len(target["history"]) - 1
            return _ok({"frameId": "frame-1"})

        if method == "Page.getNavigationHistory":
            return _ok(
                {
                    "currentIndex": target["history_index"],
                    "entries": list(target["history"]),
                }
            )

        if method == "Page.navigateToHistoryEntry":
            entry_id = params["entryId"]
            for idx, entry in enumerate(target["history"]):
                if entry["id"] == entry_id:
                    target["history_index"] = idx
                    target["url"] = entry["url"]
                    target["title"] = entry["title"]
                    break
            return _ok({})

        if method == "Accessibility.getFullAXTree":
            return _ok({"nodes": list(target["ax_nodes"])})

        if method == "DOM.scrollIntoViewIfNeeded":
            return _ok({})

        if method == "DOM.getBoxModel":
            return _ok({"model": {"content": [100, 200, 220, 200, 220, 240, 100, 240]}})

        if method == "DOM.focus":
            return _ok({})

        if method == "Input.dispatchMouseEvent":
            self.mouse_events.append(dict(params))
            if params.get("type") == "mouseReleased" and tid == "target-yt-pwa":
                # Simulate clicking "Watch Later" navigating the PWA
                target["url"] = "https://www.youtube.com/playlist?list=WL"
                target["title"] = "Watch Later - YouTube"
                target["history"].append(
                    {"id": 999, "url": target["url"], "title": target["title"]}
                )
                target["history_index"] = len(target["history"]) - 1
            return _ok({})

        if method == "Input.insertText":
            self.inserted_texts.append(str(params.get("text") or ""))
            return _ok({})

        if method == "Input.dispatchKeyEvent":
            if params.get("type") == "keyDown":
                self.dispatched_keys.append(str(params.get("key") or ""))
            return _ok({})

        return {"id": msg_id, "error": {"code": -32601, "message": f"Unknown method {method}"}}


class _MockCDPWebSocket:
    def __init__(self, state: MockCDPBrowserState) -> None:
        self._state = state
        self._responses: asyncio.Queue[str] = asyncio.Queue()
        self.closed = False

    async def send(self, data: str) -> None:
        frame = json.loads(data)
        # Also inject an unsolicited CDP event frame to prove client ignores events safely
        await self._responses.put(json.dumps({"method": "Page.lifecycleEvent", "params": {}}))
        resp = self._state.handle_command(frame)
        await self._responses.put(json.dumps(resp))

    async def recv(self) -> str:
        return await self._responses.get()

    async def close(self) -> None:
        self.closed = True


class _FakeU32Input:
    """Minimal Win32 user32 double recording cursor moves, clicks, restores, and focus."""

    def __init__(self, *, fg_hwnd: int = 501, iconic_hwnds: set[int] | None = None) -> None:
        self.fg_hwnd = fg_hwnd
        self.iconic_hwnds = set(iconic_hwnds or set())
        self.cursor_moves: list[tuple[int, int]] = []
        self.sent_inputs: list[Any] = []
        self.restored: list[int] = []
        self.focused: list[int] = []

    def GetForegroundWindow(self) -> int:
        return self.fg_hwnd

    def IsIconic(self, hwnd: int) -> bool:
        return hwnd in self.iconic_hwnds

    def ShowWindow(self, hwnd: int, cmd: int) -> bool:
        if cmd == windows_os.SW_RESTORE:
            self.restored.append(hwnd)
            self.iconic_hwnds.discard(hwnd)
        return True

    def SetForegroundWindow(self, hwnd: int) -> bool:
        self.focused.append(hwnd)
        self.fg_hwnd = hwnd
        return True

    def SetCursorPos(self, x: int, y: int) -> bool:
        self.cursor_moves.append((int(x), int(y)))
        return True

    def SendInput(self, n: int, arr: Any, _size: int) -> int:
        for i in range(n):
            self.sent_inputs.append(arr[i])
        return n


# ===========================================================================
# 1. Zero-Config Discovery & Browser-Level WebSocket Routing
# ===========================================================================


@pytest.mark.asyncio
async def test_cdp_discovery_order_devtools_active_port_env_and_legacy_fallback(
    tmp_path: Path,
) -> None:
    """Ordered fallback: env override -> DevToolsActivePort -> legacy 9222/json/version."""
    # 1. Valid DevToolsActivePort parsing
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text("9333\n/devtools/browser/abc-123\n", encoding="utf-8")
    assert parse_devtools_active_port_file(port_file) == "ws://127.0.0.1:9333/devtools/browser/abc-123"

    # Malformed / out-of-range port files return None gracefully
    bad_file = tmp_path / "BadPort"
    bad_file.write_text("notaport\n/devtools/browser/x\n", encoding="utf-8")
    assert parse_devtools_active_port_file(bad_file) is None
    assert parse_devtools_active_port_file(tmp_path / "missing") is None

    # 2. Env override takes precedence over DevToolsActivePort
    bridge_env = CDPBridge(
        devtools_port_path=port_file,
        env={"CLEA_CDP_WS_URL": "ws://127.0.0.1:9444/devtools/browser/override"},
    )
    urls_env = await bridge_env.discover_ws_urls()
    assert urls_env[0] == "ws://127.0.0.1:9444/devtools/browser/override"
    assert "ws://127.0.0.1:9333/devtools/browser/abc-123" in urls_env

    # 3. Chrome DevToolsActivePort with Edge/Brave fallback
    edge_file = tmp_path / "EdgeDevToolsActivePort"
    edge_file.write_text("9555\n/devtools/browser/edge-guid\n", encoding="utf-8")
    bridge_multi = CDPBridge(
        devtools_port_paths=[tmp_path / "missing_chrome", edge_file],
        env={},
    )
    assert await bridge_multi.discover_ws_urls() == [
        "ws://127.0.0.1:9555/devtools/browser/edge-guid"
    ]

    # 4. Legacy 127.0.0.1:9222/json/version fallback when port files absent
    fetched_http_urls: list[str] = []

    def _fake_http(url: str, _timeout: float) -> dict[str, Any]:
        fetched_http_urls.append(url)
        return {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/legacy"}

    bridge_legacy = CDPBridge(
        devtools_port_paths=[tmp_path / "missing"],
        env={},
        http_json_fetcher=_fake_http,
    )
    assert await bridge_legacy.discover_ws_urls() == [
        "ws://127.0.0.1:9222/devtools/browser/legacy"
    ]
    assert fetched_http_urls == ["http://127.0.0.1:9222/json/version"]
    assert all("/json/list" not in u for u in fetched_http_urls)


@pytest.mark.asyncio
async def test_cdp_browser_ws_uses_target_get_targets_and_flatten_true(tmp_path: Path) -> None:
    """Bridge uses Target.getTargets + Target.attachToTarget(flatten=True) over browser WS."""
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text("9333\n/devtools/browser/live-144\n", encoding="utf-8")
    mock_state = MockCDPBrowserState()
    bridge = CDPBridge(
        devtools_port_path=port_file,
        env={},
        ws_connector=mock_state.connector(),
    )

    targets = await bridge.list_targets()
    assert len(targets) == 2
    by_id = {t.target_id: t for t in targets}
    assert by_id["target-yt-pwa"].is_pwa is True
    assert by_id["target-chrome-tab"].is_pwa is False

    methods = [cmd["method"] for cmd in mock_state.commands_log]
    assert "Target.getTargets" in methods
    assert "Target.attachToTarget" in methods
    assert mock_state.connected_urls == ["ws://127.0.0.1:9333/devtools/browser/live-144"]

    # PWA selection never touches regular Chrome tabs; regular selection never hijacks PWAs
    pwa_match = await bridge.find_target(
        "https://www.youtube.com/playlist?list=WL", prefer_pwa=True
    )
    assert pwa_match is not None and pwa_match.target_id == "target-yt-pwa"

    chrome_match = await bridge.find_target(
        "https://www.youtube.com/playlist?list=WL", prefer_pwa=False
    )
    assert chrome_match is not None and chrome_match.target_id == "target-chrome-tab"

    # Unrelated PWA origin returns None when prefer_pwa=True (never hijacks YouTube PWA)
    assert await bridge.find_target("https://example.org/docs", prefer_pwa=True) is None


@pytest.mark.asyncio
async def test_cdp_graceful_degradation_when_remote_debugging_off(tmp_path: Path) -> None:
    """When Chrome remote debugging is off or refused, degrade fast with cdp_available=False."""
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text("9333\n/devtools/browser/dead\n", encoding="utf-8")
    mock_state = MockCDPBrowserState()
    mock_state.fail_connect_with = ConnectionRefusedError("Connection refused")

    bridge = CDPBridge(
        devtools_port_path=port_file,
        env={},
        connect_timeout=0.5,
        ws_connector=mock_state.connector(),
    )
    t0 = time.monotonic()
    res_nav = await bridge.cdp_navigate("https://www.youtube.com/playlist?list=WL")
    res_snap = await bridge.cdp_snapshot()
    elapsed = time.monotonic() - t0

    assert elapsed < 1.0
    assert res_nav["cdp_available"] is False
    assert res_nav["ok"] is False
    assert CDP_REMOTE_DEBUGGING_HINT in res_nav["hint"]
    assert res_snap["cdp_available"] is False
    assert CDP_REMOTE_DEBUGGING_HINT in res_snap["hint"]


# ===========================================================================
# 2. Layer-3 PWA Deep-Link Navigation vs. Explicit Browser Routing
# ===========================================================================


def test_open_url_pwa_deep_link_and_explicit_chrome_routing_via_cdp(tmp_path: Path) -> None:
    """open_url without browser navigates YouTube PWA; browser='chrome' navigates Chrome tab."""
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text("9333\n/devtools/browser/unified\n", encoding="utf-8")
    mock_state = MockCDPBrowserState()
    bridge = CDPBridge(
        devtools_port_path=port_file,
        env={},
        ws_connector=mock_state.connector(),
    )

    u32 = _FakeU32Input(fg_hwnd=900, iconic_hwnds={501})
    popen_calls: list[list[str]] = []
    shell_calls: list[str] = []

    open_windows = [
        windows_os.EnumeratedWindow(
            hwnd=502,
            title="Subscriptions - YouTube - Google Chrome",
            process_name="chrome.exe",
            is_visible=True,
        ),
        windows_os.EnumeratedWindow(
            hwnd=501,
            title="YouTube",
            process_name="chrome.exe",
            is_visible=False,  # Minimized PWA!
        ),
    ]

    # Case 1: "Go to Watch Later" -> no explicit browser -> navigates existing YouTube PWA!
    res_pwa = windows_os.open_url(
        "https://www.youtube.com/playlist?list=WL",
        app_uri_handler_finder=lambda _u: [],
        url_protocol_checker=lambda _s: None,
        app_catalog_fetcher=lambda: [{"Name": "YouTube", "AppID": "Chrome._crx_yt"}],
        window_enumerator=lambda: open_windows,
        user32=u32,
        popen_fn=lambda cmd, **_kw: popen_calls.append(list(cmd)),
        startfile_fn=shell_calls.append,
        desktop_name_fn=lambda: "Default",
        cdp_bridge=bridge,
    )
    assert res_pwa["ok"] is True
    assert res_pwa["status"] == "navigated"
    assert res_pwa["cdp_used"] is True
    assert res_pwa["routed_to_installed_app"] is True
    assert res_pwa["is_pwa"] is True
    assert res_pwa["target_id"] == "target-yt-pwa"
    assert res_pwa["url"] == "https://www.youtube.com/playlist?list=WL"
    assert res_pwa["title"] == "Watch Later - YouTube"
    assert res_pwa["window_hwnd"] == 501
    # Minimized HWND was restored & focused; zero browser processes spawned
    assert u32.restored == [501]
    assert u32.focused == [501]
    assert popen_calls == []
    assert shell_calls == []
    # Regular Chrome tab was NOT modified
    assert mock_state.targets["target-chrome-tab"]["url"] == (
        "https://www.youtube.com/feed/subscriptions"
    )
    assert mock_state.targets["target-yt-pwa"]["url"] == "https://www.youtube.com/playlist?list=WL"

    # Case 2: "Open Watch Later in Chrome" -> browser="chrome" -> navigates regular Chrome tab,
    # NEVER hijacking the standalone PWA!
    mock_state.targets["target-yt-pwa"]["url"] = "https://www.youtube.com/"
    res_chrome = windows_os.open_url(
        "https://www.youtube.com/playlist?list=WL",
        browser="chrome",
        app_uri_handler_finder=lambda _u: [],
        url_protocol_checker=lambda _s: None,
        app_catalog_fetcher=lambda: [{"Name": "YouTube", "AppID": "Chrome._crx_yt"}],
        window_enumerator=lambda: open_windows,
        user32=u32,
        popen_fn=lambda cmd, **_kw: popen_calls.append(list(cmd)),
        startfile_fn=shell_calls.append,
        desktop_name_fn=lambda: "Default",
        cdp_bridge=bridge,
    )
    assert res_chrome["ok"] is True
    assert res_chrome["status"] == "navigated"
    assert res_chrome["cdp_used"] is True
    assert res_chrome["routed_to_installed_app"] is False
    assert res_chrome["browser"] == "chrome"
    assert res_chrome["is_pwa"] is False
    assert res_chrome["target_id"] == "target-chrome-tab"
    assert res_chrome["url"] == "https://www.youtube.com/playlist?list=WL"
    assert res_chrome["window_hwnd"] == 502
    assert popen_calls == []
    # Standalone YouTube PWA stayed untouched!
    assert mock_state.targets["target-yt-pwa"]["url"] == "https://www.youtube.com/"


# ===========================================================================
# 3. Compact cdp_snapshot, cdp_act & cdp_navigate_history
# ===========================================================================


@pytest.mark.asyncio
async def test_cdp_snapshot_filtering_act_before_after_diff_and_history_back(
    tmp_path: Path,
) -> None:
    """cdp_snapshot strips noise/empty/hidden nodes; cdp_act & history_back return verified diff."""
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text("9333\n/devtools/browser/snap\n", encoding="utf-8")
    mock_state = MockCDPBrowserState()
    bridge = CDPBridge(
        devtools_port_path=port_file,
        env={},
        ws_connector=mock_state.connector(),
    )

    # 1. Compact snapshot strips ignored, hidden, and blank nodes; assigns deterministic b1..bN
    snap = await bridge.cdp_snapshot(target_id="target-yt-pwa", max_elements=60)
    assert snap["ok"] is True
    assert snap["source"] == "cdp"
    assert snap["target_id"] == "target-yt-pwa"
    assert snap["is_pwa"] is True
    refs = [(el["ref"], el["role"], el["name"]) for el in snap["elements"]]
    assert refs == [
        ("b1", "link", "Watch Later"),
        ("b2", "searchbox", "Search"),
        ("b3", "heading", "Home Feed"),
    ]

    # Ref cap enforcement
    capped = await bridge.cdp_snapshot(target_id="target-yt-pwa", max_elements=1)
    assert capped["count"] == 1
    assert capped["truncated"] is True

    # Re-populate full refs for action testing
    await bridge.cdp_snapshot(target_id="target-yt-pwa", max_elements=60)

    # 2. cdp_act("b1", "click") scrolls into view, dispatches mouse events, returns URL/title diff
    act_click = await bridge.cdp_act("b1", "click")
    assert act_click["ok"] is True
    assert act_click["ref"] == "b1"
    assert act_click["before_url"] == "https://www.youtube.com/"
    assert act_click["after_url"] == "https://www.youtube.com/playlist?list=WL"
    assert act_click["after_title"] == "Watch Later - YouTube"
    assert act_click["state_diff"]["url_changed"] is True
    assert act_click["state_diff"]["title_changed"] is True
    assert any(m["type"] == "mousePressed" for m in mock_state.mouse_events)

    # 3. cdp_act("b2", "type") + cdp_act("b2", "press", key="Enter")
    act_type = await bridge.cdp_act("b2", "type", text="lofi beats")
    assert act_type["ok"] is True
    assert mock_state.inserted_texts == ["lofi beats"]

    act_press = await bridge.cdp_act("b2", "press", key="Enter")
    assert act_press["ok"] is True
    assert mock_state.dispatched_keys == ["Enter"]

    # Stale ref fails cleanly
    stale = await bridge.cdp_act("b99", "click")
    assert stale["ok"] is False
    assert stale["status"] == "stale_ref"

    # 4. cdp_navigate_history("back") returns to previous YouTube home URL
    hist_back = await bridge.cdp_navigate_history("back", target_id="target-yt-pwa")
    assert hist_back["ok"] is True
    assert hist_back["direction"] == "back"
    assert hist_back["before_url"] == "https://www.youtube.com/playlist?list=WL"
    assert hist_back["after_url"] == "https://www.youtube.com/"
    assert hist_back["state_diff"]["url_changed"] is True

    # Going back past beginning of history returns structured no_history_entry
    no_more = await bridge.cdp_navigate_history("back", target_id="target-yt-pwa")
    assert no_more["ok"] is False
    assert no_more["status"] == "no_history_entry"


# ===========================================================================
# 4. Chromium UIA Guard & Bounded Native UIA TreeWalker + CacheRequest
# ===========================================================================


def test_chromium_uia_guard_blocks_powershell_and_delegates_to_cdp_or_fast_fallback(
    tmp_path: Path,
) -> None:
    """PowerShell UIA must NEVER run on chrome.exe/msedge.exe/brave.exe."""
    ps_called: list[list[str]] = []

    def _forbidden_ps_runner(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        ps_called.append(list(cmd))
        raise AssertionError("PowerShell UIA must NEVER execute on Chromium processes!")

    u32 = _FakeU32Input(fg_hwnd=501)
    chrome_windows = [
        windows_os.EnumeratedWindow(
            hwnd=501,
            title="YouTube",
            process_name="chrome.exe",
            is_visible=True,
        )
    ]

    # 1. With CDP active -> ui_snapshot delegates cleanly to CDP and returns b1..bN refs
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text("9333\n/devtools/browser/guard\n", encoding="utf-8")
    mock_state = MockCDPBrowserState()
    bridge = CDPBridge(
        devtools_port_path=port_file,
        env={},
        ws_connector=mock_state.connector(),
    )

    snap_cdp = windows_os.ui_snapshot(
        app_name="YouTube",
        powershell_runner=_forbidden_ps_runner,
        user32=u32,
        window_enumerator=lambda: chrome_windows,
        cdp_bridge=bridge,
    )
    assert ps_called == []
    assert snap_cdp["ok"] is True
    assert snap_cdp["source"] == "cdp"
    assert snap_cdp["elements"][0]["ref"] == "b1"

    # ui_act("b1", "click") and gui_action(action="navigate_back") delegate to CDP cleanly
    act_res = windows_os.ui_act("b1", "click", cdp_bridge=bridge)
    assert act_res["ok"] is True
    assert act_res["after_url"] == "https://www.youtube.com/playlist?list=WL"

    back_res = windows_os.gui_action("navigate_back", cdp_bridge=bridge)
    assert back_res["ok"] is True
    assert back_res["after_url"] == "https://www.youtube.com/"

    # 2. With CDP disabled -> fast (<200ms) structured cdp_unavailable response, ZERO PowerShell
    offline_state = MockCDPBrowserState()
    offline_state.fail_connect_with = ConnectionRefusedError("refused")
    offline_bridge = CDPBridge(
        devtools_port_path=port_file,
        env={},
        connect_timeout=0.2,
        ws_connector=offline_state.connector(),
    )
    t0 = time.monotonic()
    snap_off = windows_os.ui_snapshot(
        app_name="YouTube",
        powershell_runner=_forbidden_ps_runner,
        user32=u32,
        window_enumerator=lambda: chrome_windows,
        cdp_bridge=offline_bridge,
    )
    elapsed_ms = (time.monotonic() - t0) * 1000.0
    assert ps_called == []
    assert elapsed_ms < 200.0
    assert snap_off["ok"] is False
    assert snap_off["status"] == "cdp_unavailable"
    assert snap_off["cdp_available"] is False
    assert "open_url" in snap_off["hint"]
    assert "chrome://inspect/#remote-debugging" in snap_off["hint"]

    # Also verify explicit app_name="Chrome" / "Edge" / "Brave" never touches PowerShell
    for browser_app in ("Chrome", "Microsoft Edge", "Brave"):
        res_guard = windows_os.ui_snapshot(
            app_name=browser_app,
            powershell_runner=_forbidden_ps_runner,
            cdp_bridge=offline_bridge,
        )
        assert res_guard["status"] == "cdp_unavailable"
    assert ps_called == []


def test_open_url_layer3_launches_closed_pwa_then_navigates_and_degrades_gracefully(
    tmp_path: Path,
) -> None:
    """Closed Layer-3 PWA launches via open_or_focus_app, then navigates via CDP (or falls back)."""
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text("9333\n/devtools/browser/l3\n", encoding="utf-8")
    mock_state = MockCDPBrowserState()
    # Initially PWA isn't open in CDP; simulating launch creates the PWA target in mock_state
    saved_pwa = mock_state.targets.pop("target-yt-pwa")
    bridge = CDPBridge(
        devtools_port_path=port_file,
        env={},
        ws_connector=mock_state.connector(),
    )
    popen_calls: list[list[str]] = []

    def _launch_pwa(cmd: list[str], **_kw: Any) -> None:
        popen_calls.append(list(cmd))
        mock_state.targets["target-yt-pwa"] = dict(saved_pwa)

    res = windows_os.open_url(
        "https://www.youtube.com/playlist?list=WL",
        app_uri_handler_finder=lambda _u: [],
        url_protocol_checker=lambda _s: None,
        app_catalog_fetcher=lambda: [{"Name": "YouTube", "AppID": "Chrome._crx_yt"}],
        window_enumerator=list,
        popen_fn=_launch_pwa,
        desktop_name_fn=lambda: "Default",
        cdp_bridge=bridge,
    )
    assert res["ok"] is True
    assert res["status"] == "navigated"
    assert res["cdp_used"] is True
    assert res["is_pwa"] is True
    assert res["url"] == "https://www.youtube.com/playlist?list=WL"
    assert popen_calls == [["explorer.exe", r"shell:AppsFolder\Chrome._crx_yt"]]

    # Graceful fallback when CDP is unavailable during explicit browser="chrome" request
    offline_state = MockCDPBrowserState()
    offline_state.fail_connect_with = ConnectionRefusedError("disabled")
    offline_bridge = CDPBridge(
        devtools_port_path=port_file,
        env={},
        connect_timeout=0.2,
        ws_connector=offline_state.connector(),
    )
    popen_chrome: list[list[str]] = []
    res_fallback = windows_os.open_url(
        "https://www.youtube.com/playlist?list=WL",
        browser="chrome",
        which_fn=lambda exe: r"C:\Chrome\chrome.exe" if exe == "chrome.exe" else None,
        popen_fn=lambda cmd, **_kw: popen_chrome.append(list(cmd)),
        desktop_name_fn=lambda: "Default",
        cdp_bridge=offline_bridge,
    )
    assert res_fallback["ok"] is True
    assert res_fallback["status"] == "opened"
    assert res_fallback.get("cdp_used", False) is False
    assert popen_chrome == [[r"C:\Chrome\chrome.exe", "https://www.youtube.com/playlist?list=WL"]]


def test_gui_action_executes_cdp_refs_and_guards_inspect_and_click_ui_on_chromium(
    tmp_path: Path,
) -> None:
    """gui_action executes b1..bN refs via CDP; inspect/click_ui_element guard Chromium."""
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text("9333\n/devtools/browser/gui-cdp\n", encoding="utf-8")
    mock_state = MockCDPBrowserState()
    bridge = CDPBridge(
        devtools_port_path=port_file,
        env={},
        ws_connector=mock_state.connector(),
    )

    def _forbidden_ps(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        raise AssertionError("PowerShell UIA must NEVER execute on Chromium!")

    u32 = _FakeU32Input(fg_hwnd=501)
    chrome_windows = [
        windows_os.EnumeratedWindow(
            hwnd=501, title="YouTube", process_name="chrome.exe", is_visible=True
        )
    ]

    # inspect_ui_elements delegates to CDP instead of PowerShell
    insp = windows_os.inspect_ui_elements(
        app_name="YouTube",
        powershell_runner=_forbidden_ps,
        user32=u32,
        window_enumerator=lambda: chrome_windows,
        cdp_bridge=bridge,
    )
    assert insp["ok"] is True
    assert insp["source"] == "cdp"
    assert insp["elements"][0]["name"] == "Watch Later"

    # click_ui_element refuses PowerShell UIA on Chromium
    clk = windows_os.click_ui_element(
        "Watch Later",
        app_name="YouTube",
        powershell_runner=_forbidden_ps,
        user32=u32,
        window_enumerator=lambda: chrome_windows,
    )
    assert clk["ok"] is False
    assert clk["status"] == "cdp_unavailable"

    # gui_action directly executes CDP refs b1 (click), b2 (type), b2 (press)
    r_click = windows_os.gui_action("click", ref="b1", cdp_bridge=bridge)
    assert r_click["ok"] is True
    assert r_click["after_url"] == "https://www.youtube.com/playlist?list=WL"

    r_type = windows_os.gui_action("type", ref="b2", text="ambient", cdp_bridge=bridge)
    assert r_type["ok"] is True
    assert mock_state.inserted_texts[-1] == "ambient"

    r_press = windows_os.gui_action("press", ref="b2", key="Enter", cdp_bridge=bridge)
    assert r_press["ok"] is True
    assert mock_state.dispatched_keys[-1] == "Enter"

    r_ui_press = windows_os.ui_act("b2", "press", value="Escape", cdp_bridge=bridge)
    assert r_ui_press["ok"] is True
    assert mock_state.dispatched_keys[-1] == "Escape"


def test_bounded_native_uia_scripts_use_treewalker_cacherequest_and_no_unbounded_findall() -> None:
    """All native UIA scripts use bounded TreeWalker + CacheRequest (zero Descendants)."""
    scripts = (
        windows_os._POWERSHELL_SNAPSHOT_UI_SCRIPT,
        windows_os._POWERSHELL_INSPECT_UI_SCRIPT,
        windows_os._POWERSHELL_CLICK_UI_SCRIPT,
        windows_os._POWERSHELL_UI_ACT_SCRIPT,
    )
    for script in scripts:
        assert "TreeScope]::Descendants" not in script
        assert "FindAll([System.Windows.Automation.TreeScope]::Descendants" not in script
        assert "TreeWalker]::ControlViewWalker" in script
        assert "System.Windows.Automation.CacheRequest" in script
        assert "GetFirstChild($node, $uiaCacheReq)" in script
        assert "GetNextSibling($child, $uiaCacheReq)" in script
        assert "return ,$out" in script
        assert "Get-UiaProps" in script
        assert "$MaxDepth = 8" in script
        assert "$MaxNodes = 250" in script
        assert "Get-BoundedUiaNodes" in script


# ===========================================================================
# 5. DPI-Exact Coordinate Scaling (`look_at_screen` + `gui_action`)
# ===========================================================================


def test_dpi_exact_coordinate_conversion_normalized_1000_and_image_across_dpi_scales() -> None:
    """normalized_1000 and image coordinates scale accurately to physical screen pixels."""
    # 1. 150% DPI 1920x1080 physical display downscaled to 768x432 image
    u32 = _FakeU32Input()
    windows_os.set_last_screen_geometry(
        image_width=768,
        image_height=432,
        screen_width=1920,
        screen_height=1080,
        virtual_left=0,
        virtual_top=0,
        dpi_scale=1.5,
        coord_space="normalized_1000",
    )

    # normalized_1000 center (500, 500) -> physical (960, 540)
    r_norm = windows_os.gui_action(
        "click_at", x=500, y=500, coord_space="normalized_1000", user32=u32
    )
    assert r_norm["ok"] is True
    assert r_norm["coord_space"] == "normalized_1000"
    assert r_norm["physical_x"] == 960
    assert r_norm["physical_y"] == 540
    assert u32.cursor_moves[-1] == (960, 540)

    # normalized_1000 max boundary (1000, 1000) clamps cleanly to (1919, 1079)
    r_edge = windows_os.gui_action(
        "move", x=1000, y=1000, coord_space="normalized_1000", user32=u32
    )
    assert r_edge["physical_x"] == 1919
    assert r_edge["physical_y"] == 1079
    assert u32.cursor_moves[-1] == (1919, 1079)

    # 2. image pixel coords on 768x432 screenshot at 150% DPI -> exact 1920x1080 physical coords
    r_img = windows_os.gui_action(
        "double_click", x=192, y=108, coord_space="image", user32=u32
    )
    assert r_img["ok"] is True
    assert r_img["clicks"] == 2
    assert r_img["physical_x"] == 480
    assert r_img["physical_y"] == 270
    assert u32.cursor_moves[-1] == (480, 270)

    # 3. 125% DPI secondary monitor with negative virtual_left=-1920
    px, py, meta = windows_os.convert_coordinates_to_screen(
        250,
        500,
        coord_space="normalized_1000",
        screen_width=1920,
        screen_height=1080,
        virtual_left=-1920,
        virtual_top=0,
        dpi_scale=1.25,
    )
    assert (px, py) == (-1440, 540)
    assert meta["dpi_scale"] == 1.25

    # Out-of-range normalized_1000 fails cleanly
    with pytest.raises(ValueError, match=r"\[0, 1000\]"):
        windows_os.gui_action(
            "click_at", x=1001, y=500, coord_space="normalized_1000", user32=u32
        )


@pytest.mark.asyncio
async def test_look_at_screen_returns_geometry_metadata_and_forwards_to_gui_action(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """look_at_screen returns DPI-exact geometry and VoiceRunner forwards it to gui_action RPC."""
    raw_img = Image.new("RGB", (1920, 1080), color=(30, 40, 50))
    fake_api = Win32API(user32=MagicMock(), kernel32=MagicMock())
    fake_mon = MonitorInfo(
        index=1,
        left=0,
        top=0,
        right=1920,
        bottom=1080,
        work_left=0,
        work_top=0,
        work_right=1920,
        work_bottom=1040,
        is_primary=True,
        hmonitor=42,
    )
    monkeypatch.setattr(
        "clea.surfaces.voice.screen.enable_per_monitor_dpi_awareness", lambda _a: True
    )
    monkeypatch.setattr("clea.surfaces.voice.screen.enumerate_monitors", lambda _a: [fake_mon])
    monkeypatch.setattr("clea.surfaces.voice.screen.get_target_monitor", lambda **_kw: fake_mon)
    monkeypatch.setattr(
        "clea.surfaces.voice.screen.get_dpi_scale_for_monitor", lambda *_a, **_kw: 1.5
    )
    monkeypatch.setattr("clea.surfaces.voice.screen.inspect_window_layout_win32", lambda _a: [])
    monkeypatch.setattr(
        "clea.surfaces.voice.runner.get_target_monitor", lambda **_kw: fake_mon
    )

    screen_svc = ScreenCaptureService(
        image_max_side=768,
        api=fake_api,
        grabber=lambda *a, **kw: raw_img,
    )
    screen_svc.get_active_window_info = lambda: None  # type: ignore[method-assign]

    sent_responses: list[VoiceToolResponse] = []
    rpc_requests: list[Any] = []

    class _DummyBackend(VoiceBackend):
        backend_name = "dummy"

        @property
        def model_name(self) -> str:
            return "dummy"

        async def connect(self, config: VoiceSessionConfig) -> None:
            pass

        async def close(self) -> None:
            pass

        async def send_audio(self, pcm_bytes: bytes) -> None:
            pass

        async def send_video_frame(
            self, image_bytes: bytes, *, mime_type: str = "image/jpeg"
        ) -> None:
            pass

        async def send_text(self, text: str, *, end_of_turn: bool = True) -> None:
            pass

        async def send_tool_responses(self, responses: list[VoiceToolResponse]) -> None:
            sent_responses.extend(responses)

        async def events(self) -> AsyncIterator[VoiceBackendEvent]:
            if False:
                yield  # pragma: no cover

    runner = VoiceRunner(
        config=VoiceConfig(consent_mode="auto_allow"),
        data_dir=tmp_path,
        backend_factory=_DummyBackend,
        screen_service=screen_svc,
        enable_hotkey=False,
    )
    backend = _DummyBackend()

    async def _fake_rpc(req: Any, *, timeout: float = 10.0) -> dict[str, Any]:
        rpc_requests.append(req)
        return {"ok": True, "status": "completed", "action": "click_at"}

    runner._rpc = _fake_rpc  # type: ignore[method-assign]

    await runner._handle_tool_call(
        ToolCallEvent(call_id="look-1", name="look_at_screen", arguments={"target": "full_screen"}),
        backend,
    )
    look_payload = sent_responses[-1].response
    assert look_payload["image_width"] == 768
    assert look_payload["image_height"] == 432
    assert look_payload["screen_width"] == 1920
    assert look_payload["screen_height"] == 1080
    assert look_payload["virtual_left"] == 0
    assert look_payload["virtual_top"] == 0
    assert look_payload["dpi_scale"] == 1.5
    assert look_payload["coord_space"] == "normalized_1000"

    # Subsequent gui_action with coord_space="normalized_1000" forwards exact geometry over RPC
    await runner._handle_tool_call(
        ToolCallEvent(
            call_id="gui-1",
            name="gui_action",
            arguments={"action": "click_at", "x": 500, "y": 500, "coord_space": "normalized_1000"},
        ),
        backend,
    )
    assert isinstance(rpc_requests[-1], OsGuiActionRequest)
    params = rpc_requests[-1].params
    assert params.coord_space == "normalized_1000"
    assert params.image_width == 768
    assert params.image_height == 432
    assert params.screen_width == 1920
    assert params.screen_height == 1080
    assert params.dpi_scale == 1.5


# ===========================================================================
# 6. Harness State-Diff Capture & Hard 2-Click Circuit Breaker
# ===========================================================================


def test_desktop_state_capture_diff_and_circuit_breaker_unit_rules(tmp_path: Path) -> None:
    """capture_desktop_state captures window+CDP+GSMTC; breaker trips on 2 clicks / 3 snaps."""
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text("9333\n/devtools/browser/diff\n", encoding="utf-8")
    mock_state = MockCDPBrowserState()
    bridge = CDPBridge(
        devtools_port_path=port_file,
        env={},
        ws_connector=mock_state.connector(),
    )

    class _FakeFocus:
        def __init__(self, title: str) -> None:
            self.title = title

        def get_foreground_context(self) -> WindowContext:
            return WindowContext(hwnd=501, title=self.title, process_name="chrome.exe")

    class _FakeMedia:
        def __init__(self, track: str) -> None:
            self.track = track

        def get_active_session(self, timeout_s: float = 0.5) -> MediaSessionState:
            return MediaSessionState(playback_status="Playing", title=self.track, available=True)

    before = capture_desktop_state(
        focus_backend=_FakeFocus("YouTube"),  # type: ignore[arg-type]
        cdp_bridge=bridge,
        media_backend=_FakeMedia("Track A"),  # type: ignore[arg-type]
    )
    mock_state.targets["target-yt-pwa"]["url"] = "https://www.youtube.com/playlist?list=WL"
    mock_state.targets["target-yt-pwa"]["title"] = "Watch Later - YouTube"
    after = capture_desktop_state(
        focus_backend=_FakeFocus("Watch Later - YouTube"),  # type: ignore[arg-type]
        cdp_bridge=bridge,
        media_backend=_FakeMedia("Track B"),  # type: ignore[arg-type]
    )
    diff = compute_state_diff(before, after)
    assert diff["changed"] is True
    assert diff["window_changed"] is True
    assert diff["url_changed"] is True
    assert diff["title_changed"] is True
    assert diff["media_changed"] is True

    # 1. Bounce oscillation (S0 -> S1 -> S0) trips after 2 blind clicks and blocks 3rd
    cb = ActionCircuitBreaker()
    s0 = {"hwnd": 501, "title": "Home", "process_name": "chrome.exe", "url": "https://yt/home"}
    s1 = {"hwnd": 501, "title": "Shorts", "process_name": "chrome.exe", "url": "https://yt/shorts"}
    cb.record_outcome(
        "gui_action",
        {"action": "click_at", "x": 50, "y": 50},
        result={"ok": True},
        state_diff=compute_state_diff(s0, s1),
    )
    assert cb.tripped is False
    cb.record_outcome(
        "gui_action",
        {"action": "click_at", "x": 50, "y": 50},
        result={"ok": True},
        state_diff=compute_state_diff(s1, s0),
    )
    assert cb.tripped is True
    blocked = cb.check_before_tool("gui_action", {"action": "click_at", "x": 60, "y": 60})
    assert blocked is not None
    assert blocked["status"] == "blocked_by_circuit_breaker"
    assert blocked["circuit_breaker_tripped"] is True
    assert "Use open_url() for direct URL navigation" in blocked["suggested_recovery"]
    # Deterministic recovery tools remain allowed even while tripped
    assert (
        cb.check_before_tool("open_url", {"url": "https://www.youtube.com/playlist?list=WL"})
        is None
    )
    assert cb.check_before_tool("gui_action", {"action": "navigate_back"}) is None

    # 2. 3 consecutive ui_snapshot failures trip the breaker
    cb2 = ActionCircuitBreaker()
    for _ in range(2):
        cb2.record_outcome("ui_snapshot", {}, result={"ok": False, "status": "cdp_unavailable"})
        assert cb2.tripped is False
    cb2.record_outcome("ui_snapshot", {}, result={"ok": False, "status": "cdp_unavailable"})
    assert cb2.tripped is True
    assert cb2.check_before_tool("gui_action", {"action": "scroll", "delta": -300}) is not None


@pytest.mark.asyncio
async def test_agent_loop_state_diff_2_click_breaker_blocks_3rd_and_resets(
    tmp_path: Path,
) -> None:
    """AgentLoop attaches state_diff, trips after 2 unchanged clicks, blocks 3rd, and resets."""
    current_state = {
        "hwnd": 501,
        "title": "YouTube",
        "process_name": "chrome.exe",
        "url": "https://www.youtube.com/",
        "page_title": "YouTube",
    }
    gui_exec_count = 0

    def _gui_handler(
        action: str, x: int | None = None, y: int | None = None, **_kw: Any
    ) -> dict[str, Any]:
        nonlocal gui_exec_count
        gui_exec_count += 1
        return {"ok": True, "status": "completed", "action": action, "x": x, "y": y}

    def _open_url_handler(url: str, browser: str | None = None) -> dict[str, Any]:
        current_state["url"] = url
        current_state["title"] = "Watch Later - YouTube"
        current_state["page_title"] = "Watch Later - YouTube"
        return {"ok": True, "status": "navigated", "url": url, "title": "Watch Later - YouTube"}

    registry = ToolRegistry()
    registry.register(
        name="gui_action",
        description="gui",
        risk_level=RiskLevel.READ_ONLY,
        handler=_gui_handler,
    )
    registry.register(
        name="open_url",
        description="url",
        risk_level=RiskLevel.READ_ONLY,
        handler=_open_url_handler,
    )

    class _ScriptedProvider(LLMProvider):
        provider_name = "scripted"
        model_name = "scripted"

        def __init__(self, turns: list[list[StreamChunk]]) -> None:
            self._turns = list(turns)

        async def stream_turn(
            self,
            messages: list[Message],
            tools: Any,
            system_prompt: str,
            **kwargs: Any,
        ) -> AsyncIterator[StreamChunk]:
            for ch in self._turns.pop(0):
                yield ch

    # Turn 1: 3 blind clicks -> 1st & 2nd execute with unchanged state_diff, 3rd is BLOCKED,
    # then open_url recovers and resets the breaker so a subsequent click is permitted!
    provider = _ScriptedProvider(
        [
            [
                StreamChunk(
                    type="tool_call",
                    tool_call=ToolCall(
                        id="c1",
                        name="gui_action",
                        arguments={"action": "click_at", "x": 100, "y": 100},
                    ),
                ),
                StreamChunk(
                    type="tool_call",
                    tool_call=ToolCall(
                        id="c2",
                        name="gui_action",
                        arguments={"action": "click_at", "x": 110, "y": 100},
                    ),
                ),
                StreamChunk(
                    type="tool_call",
                    tool_call=ToolCall(
                        id="c3",
                        name="gui_action",
                        arguments={"action": "click_at", "x": 120, "y": 100},
                    ),
                ),
                StreamChunk(type="done", text=""),
            ],
            [
                StreamChunk(
                    type="tool_call",
                    tool_call=ToolCall(
                        id="u1",
                        name="open_url",
                        arguments={"url": "https://www.youtube.com/playlist?list=WL"},
                    ),
                ),
                StreamChunk(
                    type="tool_call",
                    tool_call=ToolCall(
                        id="c4",
                        name="gui_action",
                        arguments={"action": "click_at", "x": 200, "y": 200},
                    ),
                ),
                StreamChunk(type="done", text=""),
            ],
            [
                StreamChunk(
                    type="text_delta",
                    text=(
                        "Navigated directly to Watch Later via open_url after the click breaker "
                        "blocked blind clicking."
                    ),
                ),
                StreamChunk(type="done", text=""),
            ],
            # Turn 2: fresh user turn resets breaker cleanly
            [
                StreamChunk(
                    type="tool_call",
                    tool_call=ToolCall(
                        id="c5",
                        name="gui_action",
                        arguments={"action": "click_at", "x": 300, "y": 300},
                    ),
                ),
                StreamChunk(type="done", text=""),
            ],
            [
                StreamChunk(type="text_delta", text="Clicked once on the new turn."),
                StreamChunk(type="done", text=""),
            ],
        ]
    )

    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()
    agent = AgentLoop(
        provider=provider,
        registry=registry,
        memory=mem,
        state_capture_fn=lambda: dict(current_state),
    )
    session = Session(id="sess-cb-1")

    await agent.run_turn(session=session, user_text="Go to Watch Later")

    tool_msgs = {
        m.tool_call_id: json.loads(m.content) for m in session.messages if m.role == "tool"
    }
    # Click 1 & Click 2 ran with state_diff attached (changed=False)
    assert tool_msgs["c1"]["state_diff"]["changed"] is False
    assert tool_msgs["c2"]["state_diff"]["changed"] is False
    # Click 3 was BLOCKED BEFORE EXECUTION!
    assert tool_msgs["c3"]["status"] == "blocked_by_circuit_breaker"
    assert tool_msgs["c3"]["circuit_breaker_tripped"] is True
    assert len(tool_msgs["c3"]["suggested_recovery"]) >= 3
    # open_url ran with verified URL change and RESET the breaker!
    assert tool_msgs["u1"]["status"] == "navigated"
    assert tool_msgs["u1"]["state_diff"]["url_changed"] is True
    # Click 4 ran because open_url reset the breaker
    assert tool_msgs["c4"]["status"] == "completed"
    # Total actual gui_action executions in Turn 1 = 3 (c1, c2, c4; c3 blocked!)
    assert gui_exec_count == 3

    # Turn 2 starts with a fresh breaker
    await agent.run_turn(session=session, user_text="Try clicking once more")
    assert gui_exec_count == 4


@pytest.mark.asyncio
async def test_voice_runner_circuit_breaker_blocks_3rd_click_and_resets_on_new_turn(
    tmp_path: Path,
) -> None:
    """VoiceRunner blocks 3rd blind click, allows open_url recovery, and resets on new turn."""
    state = {
        "hwnd": 501,
        "title": "YouTube",
        "process_name": "chrome.exe",
        "url": "https://www.youtube.com/",
        "page_title": "YouTube",
    }
    sent: list[VoiceToolResponse] = []
    rpc_calls: list[Any] = []

    class _DummyBackend(VoiceBackend):
        backend_name = "dummy"

        @property
        def model_name(self) -> str:
            return "dummy"

        async def connect(self, config: VoiceSessionConfig) -> None:
            pass

        async def close(self) -> None:
            pass

        async def send_audio(self, pcm_bytes: bytes) -> None:
            pass

        async def send_video_frame(
            self, image_bytes: bytes, *, mime_type: str = "image/jpeg"
        ) -> None:
            pass

        async def send_text(self, text: str, *, end_of_turn: bool = True) -> None:
            pass

        async def send_tool_responses(self, responses: list[VoiceToolResponse]) -> None:
            sent.extend(responses)

        async def events(self) -> AsyncIterator[VoiceBackendEvent]:
            if False:
                yield  # pragma: no cover

    runner = VoiceRunner(
        config=VoiceConfig(),
        data_dir=tmp_path,
        backend_factory=_DummyBackend,
        enable_hotkey=False,
        state_capture_fn=lambda: dict(state),
    )
    backend = _DummyBackend()

    async def _fake_rpc(req: Any, *, timeout: float = 10.0) -> dict[str, Any]:
        rpc_calls.append(req)
        if isinstance(req, OsOpenUrlRequest):
            state["url"] = req.params.url
            state["title"] = "Watch Later - YouTube"
            return {"ok": True, "status": "navigated", "url": req.params.url}
        return {"ok": True, "status": "completed", "action": "click_at"}

    runner._rpc = _fake_rpc  # type: ignore[method-assign]

    # 2 blind clicks with unchanged state
    for idx in (1, 2):
        await runner._handle_tool_call(
            ToolCallEvent(
                call_id=f"vc{idx}",
                name="gui_action",
                arguments={"action": "click_at", "x": 100 + idx, "y": 100},
            ),
            backend,
        )
        assert sent[-1].response["state_diff"]["changed"] is False

    # 3rd blind click is blocked BEFORE hitting daemon RPC
    await runner._handle_tool_call(
        ToolCallEvent(
            call_id="vc3",
            name="gui_action",
            arguments={"action": "click_at", "x": 103, "y": 100},
        ),
        backend,
    )
    assert sent[-1].is_error is True
    assert sent[-1].response["status"] == "blocked_by_circuit_breaker"
    assert sent[-1].response["circuit_breaker_tripped"] is True
    assert len(rpc_calls) == 2

    # Reset on new user turn allows clicking again
    runner.reset_turn_state()
    await runner._handle_tool_call(
        ToolCallEvent(
            call_id="vc4",
            name="gui_action",
            arguments={"action": "click_at", "x": 150, "y": 150},
        ),
        backend,
    )
    assert sent[-1].is_error is False
    assert sent[-1].response["status"] == "completed"
    assert len(rpc_calls) == 3


# ===========================================================================
# 7. Prompt Alignment
# ===========================================================================


def test_prompts_align_with_pwa_open_url_cdp_refs_normalized_1000_and_state_diff() -> None:
    """Both Chat and Voice prompts teach direct PWA open_url, CDP refs, normalized_1000 & diff."""
    for label, prompt in (
        ("chat", _BASE_SYSTEM_PROMPT),
        ("voice", BASE_VOICE_SYSTEM_INSTRUCTION),
    ):
        low = prompt.lower()
        assert "https://www.youtube.com/playlist?list=wl" in low, (
            f"{label} missing Watch Later PWA URL"
        )
        assert "b1" in low and "cdp" in low, f"{label} missing CDP refs guidance"
        assert "normalized_1000" in low, f"{label} missing normalized_1000 coordinate rule"
        assert "state_diff" in low, f"{label} missing state_diff grounding rule"
        assert 'browser="chrome"' in low or "browser='chrome'" in low, (
            f"{label} missing explicit Chrome browser guidance"
        )


# ===========================================================================
# 8. Additional Edge-Case Regression Tests
# ===========================================================================


@pytest.mark.asyncio
async def test_cdp_stale_devtools_active_port_falls_back_to_legacy_9222(tmp_path: Path) -> None:
    """If DevToolsActivePort points to a dead port, connection falls back to legacy 9222."""
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text("59999\n/devtools/browser/stale-dead\n", encoding="utf-8")
    mock_state = MockCDPBrowserState()
    attempted: list[str] = []

    async def _selective_connect(ws_url: str, *, open_timeout: float = 1.5) -> _MockCDPWebSocket:
        attempted.append(ws_url)
        if "59999" in ws_url:
            raise ConnectionRefusedError("stale DevToolsActivePort")
        return _MockCDPWebSocket(mock_state)

    def _legacy_http(url: str, _timeout: float) -> dict[str, Any]:
        assert url == "http://127.0.0.1:9222/json/version"
        return {"webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/live-9222"}

    bridge = CDPBridge(
        devtools_port_path=port_file,
        env={},
        ws_connector=_selective_connect,
        http_json_fetcher=_legacy_http,
    )
    res = await bridge.cdp_get_active_page_state()
    assert res["ok"] is True
    assert attempted == [
        "ws://127.0.0.1:59999/devtools/browser/stale-dead",
        "ws://127.0.0.1:9222/devtools/browser/live-9222",
    ]


@pytest.mark.asyncio
async def test_cdp_navigate_same_path_different_query_settles_on_new_query(
    tmp_path: Path,
) -> None:
    """Same-path query transitions (?list=LL -> ?list=WL) do not settle prematurely."""
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text("9333\n/devtools/browser/query\n", encoding="utf-8")
    mock_state = MockCDPBrowserState()
    mock_state.targets["target-yt-pwa"]["url"] = "https://www.youtube.com/playlist?list=LL"
    mock_state.targets["target-yt-pwa"]["title"] = "Liked videos - YouTube"

    bridge = CDPBridge(
        devtools_port_path=port_file,
        env={},
        ws_connector=mock_state.connector(),
    )
    res = await bridge.cdp_navigate(
        "https://www.youtube.com/playlist?list=WL",
        prefer_pwa=True,
        create_if_missing=False,
    )
    assert res["ok"] is True
    assert res["before_url"] == "https://www.youtube.com/playlist?list=LL"
    assert res["after_url"] == "https://www.youtube.com/playlist?list=WL"
    assert res["state_diff"]["url_changed"] is True


def test_circuit_breaker_non_consecutive_progress_resets_click_streak() -> None:
    """A blind click that genuinely advances state resets consecutive no-progress count."""
    cb = ActionCircuitBreaker()
    s0 = {"hwnd": 501, "title": "Home", "process_name": "chrome.exe", "url": "https://yt/"}
    s1 = {"hwnd": 501, "title": "Library", "process_name": "chrome.exe", "url": "https://yt/lib"}

    # Click 1: no progress (count -> 1)
    cb.record_outcome(
        "gui_action",
        {"action": "click_at", "x": 10, "y": 10},
        result={"ok": True},
        state_diff=compute_state_diff(s0, s0),
    )
    assert cb.tripped is False
    assert cb.blind_click_no_progress_count == 1

    # Click 2: genuine forward progress to brand-new state s1 (count resets -> 0)
    cb.record_outcome(
        "gui_action",
        {"action": "click_at", "x": 20, "y": 20},
        result={"ok": True},
        state_diff=compute_state_diff(s0, s1),
    )
    assert cb.tripped is False
    assert cb.blind_click_no_progress_count == 0

    # Click 3: single no-progress click on s1 must NOT immediately trip (count -> 1)
    cb.record_outcome(
        "gui_action",
        {"action": "click_at", "x": 30, "y": 30},
        result={"ok": True},
        state_diff=compute_state_diff(s1, s1),
    )
    assert cb.tripped is False
    assert cb.blind_click_no_progress_count == 1

    # Click 4: second consecutive no-progress click on s1 trips breaker (count -> 2)
    cb.record_outcome(
        "gui_action",
        {"action": "click_at", "x": 30, "y": 30},
        result={"ok": True},
        state_diff=compute_state_diff(s1, s1),
    )
    assert cb.tripped is True


@pytest.mark.asyncio
async def test_cdp_navigate_waits_past_two_polls_for_delayed_navigation(tmp_path: Path) -> None:
    """_wait_for_settlement must NOT bail out after 2 polls (~80ms) before Page.navigate commits."""
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text("9333\n/devtools/browser/delayed\n", encoding="utf-8")
    mock_state = MockCDPBrowserState()
    pending_nav: dict[str, Any] = {"polls": 0, "target_url": ""}

    orig_handle = mock_state.handle_command

    def _delayed_handle(frame: dict[str, Any]) -> dict[str, Any]:
        method = frame["method"]
        params = frame.get("params") or {}
        session_id = frame.get("sessionId")
        if method == "Page.navigate" and session_id:
            pending_nav["target_url"] = str(params.get("url") or "")
            pending_nav["polls"] = 0
            return {"id": frame["id"], "result": {"frameId": "frame-1"}}
        if method == "Runtime.evaluate" and session_id and pending_nav["target_url"]:
            pending_nav["polls"] += 1
            if pending_nav["polls"] >= 4:
                tid = mock_state.sessions[session_id]
                mock_state.targets[tid]["url"] = pending_nav["target_url"]
                mock_state.targets[tid]["title"] = "Watch Later - YouTube"
                pending_nav["target_url"] = ""
        return orig_handle(frame)

    mock_state.handle_command = _delayed_handle  # type: ignore[method-assign]
    bridge = CDPBridge(
        devtools_port_path=port_file,
        env={},
        ws_connector=mock_state.connector(),
    )
    res = await bridge.cdp_navigate(
        "https://www.youtube.com/playlist?list=WL",
        prefer_pwa=True,
        create_if_missing=False,
        settle_timeout_s=1.5,
    )
    assert res["ok"] is True
    assert pending_nav["polls"] >= 4
    assert res["before_url"] == "https://www.youtube.com/"
    assert res["after_url"] == "https://www.youtube.com/playlist?list=WL"
    assert res["state_diff"]["url_changed"] is True


@pytest.mark.asyncio
async def test_cdp_quad_center_rejects_zero_area_and_negative_coords_and_js_fallback(
    tmp_path: Path,
) -> None:
    """Zero-height, zero-width, or negative quads return None and fall back to JS click."""
    from clea.tools.cdp_bridge import _quad_center

    assert _quad_center([10, 20, 110, 20, 110, 20, 10, 20]) is None  # zero height
    assert _quad_center([10, 20, 10, 20, 10, 60, 10, 60]) is None  # zero width
    assert _quad_center([-50, -20, -10, -20, -10, -5, -50, -5]) is None  # negative center
    assert _quad_center([10, 20, 50, 20, 50, 40, 10, 40]) == (30.0, 30.0)

    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text("9333\n/devtools/browser/zero-quad\n", encoding="utf-8")
    mock_state = MockCDPBrowserState()
    orig_handle = mock_state.handle_command

    def _zero_height_box(frame: dict[str, Any]) -> dict[str, Any]:
        method = frame["method"]
        if method == "DOM.getBoxModel":
            return {
                "id": frame["id"],
                "result": {"model": {"content": [10, 20, 110, 20, 110, 20, 10, 20]}},
            }
        if method == "DOM.resolveNode":
            return {"id": frame["id"], "result": {"object": {"objectId": "obj-101"}}}
        if method == "Runtime.callFunctionOn":
            return {"id": frame["id"], "result": {"result": {"type": "undefined"}}}
        return orig_handle(frame)

    mock_state.handle_command = _zero_height_box  # type: ignore[method-assign]
    bridge = CDPBridge(
        devtools_port_path=port_file,
        env={},
        ws_connector=mock_state.connector(),
    )
    snap = await bridge.cdp_snapshot(prefer_pwa=True)
    assert snap["ok"] is True
    act = await bridge.cdp_act("b1", "click")
    assert act["ok"] is True
    assert act["method"] == "cdp_js_click"


@pytest.mark.asyncio
async def test_cdp_title_suffix_stripping_preferred_browser_and_single_target_probe(
    tmp_path: Path,
) -> None:
    """Suffix stripping picks exact tab; preferred_browser isolates Edge; target_id skips tabs."""
    chrome_port = tmp_path / "Google" / "Chrome" / "User Data" / "DevToolsActivePort"
    chrome_port.parent.mkdir(parents=True)
    chrome_port.write_text("9222\n/devtools/browser/chrome-guid\n", encoding="utf-8")
    edge_port = tmp_path / "Microsoft" / "Edge" / "User Data" / "DevToolsActivePort"
    edge_port.parent.mkdir(parents=True)
    edge_port.write_text("9223\n/devtools/browser/edge-guid\n", encoding="utf-8")

    bridge_env = CDPBridge(env={"LOCALAPPDATA": str(tmp_path)})
    edge_urls = await bridge_env.discover_ws_urls(preferred_browser="edge")
    assert edge_urls == ["ws://127.0.0.1:9223/devtools/browser/edge-guid"]
    chrome_urls = await bridge_env.discover_ws_urls(preferred_browser="chrome")
    assert chrome_urls == ["ws://127.0.0.1:9222/devtools/browser/chrome-guid"]

    # "Watch Later - YouTube - Google Chrome" must match "Watch Later - YouTube" over "YouTube"
    mock_state = MockCDPBrowserState()
    mock_state.targets["target-yt-short"] = {
        "targetId": "target-yt-short",
        "type": "page",
        "url": "https://www.youtube.com/",
        "title": "YouTube",
        "is_pwa": False,
        "window_id": 20,
        "window_state": "normal",
        "has_focus": False,
        "history": [],
        "history_index": 0,
        "ax_nodes": [],
    }
    mock_state.targets["target-yt-wl"] = {
        "targetId": "target-yt-wl",
        "type": "page",
        "url": "https://www.youtube.com/playlist?list=WL",
        "title": "Watch Later - YouTube",
        "is_pwa": False,
        "window_id": 20,
        "window_state": "normal",
        "has_focus": False,
        "history": [],
        "history_index": 0,
        "ax_nodes": [],
    }
    bridge = CDPBridge(
        devtools_port_path=chrome_port,
        env={},
        ws_connector=mock_state.connector(),
    )
    state_res = await bridge.cdp_get_active_page_state(
        prefer_pwa=False,
        title_hint="Watch Later - YouTube - Google Chrome",
    )
    assert state_res["ok"] is True
    assert state_res["target_id"] == "target-yt-wl"

    # Explicit target_id must NOT attach to unrelated targets
    mock_state.commands_log.clear()
    snap_single = await bridge.cdp_snapshot(target_id="target-yt-pwa")
    assert snap_single["ok"] is True
    attached_ids = [
        (frame.get("params") or {})["targetId"]
        for frame in mock_state.commands_log
        if frame.get("method") == "Target.attachToTarget"
    ]
    assert set(attached_ids) == {"target-yt-pwa"}


def test_vision_inspect_screen_guards_chromium_uia_and_records_dpi_geometry(
    tmp_path: Path,
) -> None:
    """inspect_screen skips UIA on Chromium and records window bounds for normalized_1000."""
    import io

    from clea.tools.vision import (
        ArtifactStore,
        ScreenVisionService,
        WindowCapture,
        WindowInfo,
    )

    probe_calls: list[int] = []

    class _FakeVisionBackend:
        def get_foreground_window(self) -> WindowInfo | None:
            return WindowInfo(
                hwnd=909,
                title="Watch Later - YouTube",
                process_name="chrome.exe",
                bounds=(100, 50, 900, 650),
            )

        def find_window(self, query: str) -> WindowInfo | None:
            return self.get_foreground_window()

        def is_minimized(self, hwnd: int) -> bool:
            return False

        def probe_uia_text(self, hwnd: int, max_chars: int = 3000) -> str:
            probe_calls.append(hwnd)
            return "SHOULD_NOT_RUN"

        def capture_window_png(self, hwnd: int) -> WindowCapture:
            buf = io.BytesIO()
            Image.new("RGB", (800, 600), color=(20, 40, 60)).save(buf, format="PNG")
            return WindowCapture(
                png_bytes=buf.getvalue(),
                width=800,
                height=600,
                window_title="Watch Later - YouTube",
                process_name="chrome.exe",
            )

    svc = ScreenVisionService(
        backend=_FakeVisionBackend(),  # type: ignore[arg-type]
        artifact_store=ArtifactStore(data_dir=tmp_path),
        desktop_name_fn=lambda: "Default",
    )
    res = svc.inspect(target="active", include_image=True)
    assert res.status == "ok"
    # Chromium UIA guard prevented probe_uia_text from running on chrome.exe
    assert probe_calls == []
    assert res.uia_text is None
    assert res.image_width == 800
    assert res.image_height == 600
    assert res.screen_width == 800
    assert res.screen_height == 600
    assert res.virtual_left == 100
    assert res.virtual_top == 50
    assert res.coord_space == "normalized_1000"

    # Follow-up normalized_1000 click at (500, 500) must land at window center (500, 350)!
    u32 = _FakeU32Input()
    click_res = windows_os.gui_action(
        "click_at",
        x=500,
        y=500,
        coord_space="normalized_1000",
        user32=u32,
    )
    assert click_res["ok"] is True
    assert u32.cursor_moves[-1] == (500, 350)


@pytest.mark.asyncio
async def test_daemon_os_rpc_attaches_state_diff_and_preserves_chromium_guard_fields(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """CleaDaemon attaches state_diff on os.gui_action and preserves Chromium guard fields."""
    from clea.core import daemon as daemon_mod
    from clea.core.daemon import build_daemon
    from clea.core.protocol import (
        OsGuiActionParams,
        OsInspectUiElementsParams,
        OsInspectUiElementsRequest,
    )

    state = {"title": "YouTube", "hwnd": 501}

    class _FakeFocus:
        def get_foreground_context(self) -> WindowContext:
            return WindowContext(
                hwnd=int(state["hwnd"]),
                title=str(state["title"]),
                process_name="notepad.exe",
            )

        def is_minimized(self, hwnd: int) -> bool:
            return False

        def is_foreground(self, hwnd: int) -> bool:
            return int(hwnd) == int(state["hwnd"])

    class _FakeMedia:
        def get_active_session(self, timeout_s: float = 0.5) -> MediaSessionState:
            return MediaSessionState(playback_status="Playing", title="Track 1", available=True)

    def _fake_gui_action(
        action: str, x: Any, y: Any, *_args: Any, **_kwargs: Any
    ) -> dict[str, Any]:
        state["title"] = "Watch Later - Notepad"
        return {"ok": True, "status": "completed", "action": action, "x": x, "y": y}

    def _fake_inspect_ui(*_args: Any, **_kwargs: Any) -> dict[str, Any]:
        return windows_os._chromium_cdp_unavailable_snapshot(
            window_title="YouTube", process_name="chrome.exe"
        )

    monkeypatch.setattr(daemon_mod, "gui_action", _fake_gui_action)
    monkeypatch.setattr(daemon_mod, "inspect_ui_elements", _fake_inspect_ui)

    class _DummyProvider(LLMProvider):
        provider_name = "dummy"
        model_name = "dummy"

        async def stream_turn(self, *args: Any, **kwargs: Any) -> AsyncIterator[StreamChunk]:
            yield StreamChunk(type="done", text="")

    mem = MemoryStore(base_dir=tmp_path)
    mem.ensure_initialized()
    d = build_daemon(
        host="127.0.0.1",
        port=0,
        provider=_DummyProvider(),
        memory=mem,
        focus_backend=_FakeFocus(),  # type: ignore[arg-type]
        media_backend=_FakeMedia(),  # type: ignore[arg-type]
    )

    sent_payloads: list[Any] = []

    async def _fake_respond(
        _ws: Any, _req: Any, result: Any = None, *, error: str | None = None
    ) -> None:
        assert error is None
        sent_payloads.append(result)

    d._respond = _fake_respond  # type: ignore[method-assign]
    fake_conn = MagicMock()

    await d._handle_os_gui_action(
        fake_conn,
        OsGuiActionRequest(params=OsGuiActionParams(action="click_at", x=100, y=200)),
    )
    gui_res = sent_payloads[-1]
    assert gui_res.ok is True
    assert gui_res.state_diff is not None
    assert gui_res.state_diff["changed"] is True
    assert gui_res.state_diff["title_changed"] is True
    assert gui_res.state_diff["before"]["title"] == "YouTube"
    assert gui_res.state_diff["after"]["title"] == "Watch Later - Notepad"

    await d._handle_os_inspect_ui_elements(
        fake_conn,
        OsInspectUiElementsRequest(params=OsInspectUiElementsParams(app_name="chrome")),
    )
    insp_res = sent_payloads[-1]
    assert insp_res.ok is False
    assert insp_res.status == "cdp_unavailable"
    assert insp_res.cdp_available is False
    assert insp_res.hint is not None and "chrome://inspect/#remote-debugging" in insp_res.hint


def test_circuit_breaker_blocks_blind_ui_batch_and_requires_state_advance() -> None:
    """Tripped breaker blocks blind ui_batch click_at and ignores unchanged focused open_url."""
    cb = ActionCircuitBreaker()
    s0 = {"hwnd": 501, "title": "YouTube", "process_name": "chrome.exe", "url": "https://yt/"}
    for _ in range(2):
        cb.record_outcome(
            "gui_action",
            {"action": "click_at", "x": 10, "y": 10},
            result={"ok": True},
            state_diff=compute_state_diff(s0, s0),
        )
    assert cb.tripped is True

    # Blind ui_batch click_at cannot bypass tripped breaker
    blocked = cb.check_before_tool(
        "ui_batch",
        {"steps": [{"op": "click_at", "x": 20, "y": 20}]},
    )
    assert blocked is not None
    assert blocked["status"] == "blocked_by_circuit_breaker"

    # Unchanged fallback open_url(status="focused", changed=False) must NOT reset breaker
    cb.record_outcome(
        "open_url",
        {"url": "https://www.youtube.com/playlist?list=WL"},
        result={"ok": True, "status": "focused", "cdp_used": False},
        state_diff=compute_state_diff(s0, s0),
    )
    assert cb.tripped is True

    # Native ui_act("e1", "press", value="Enter") executes click + press_keys (not a no-op!)
    windows_os.set_ui_snapshot_state(
        {
            "ok": True,
            "snapshot_id": "snap-press",
            "window_title": "Notepad",
            "hwnd": 42,
            "elements": [
                {
                    "ref": "e1",
                    "role": "edit",
                    "name": "Editor",
                    "bounds": {"x": 10, "y": 10, "width": 200, "height": 40},
                    "center": {"cx": 110, "cy": 30},
                }
            ],
        }
    )
    recorded_actions: list[dict[str, Any]] = []

    def _fake_gui(**kw: Any) -> dict[str, Any]:
        recorded_actions.append(dict(kw))
        return {"ok": True, "status": "completed"}

    act_res = windows_os.ui_act(
        "e1",
        "press",
        value="Enter",
        gui_action_fn=_fake_gui,
        _capture_snapshot=False,
    )
    assert act_res["ok"] is True
    assert act_res["method"] == "press"
    assert [a["action"] for a in recorded_actions] == ["click_at", "press_keys"]
    assert recorded_actions[1]["keys"] == ["Enter"]


def test_open_url_layer3_retries_cdp_after_launching_closed_pwa(tmp_path: Path) -> None:
    """When a closed PWA launches and appears in CDP on a retry poll, open_url navigates it."""
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text("9333\n/devtools/browser/retry-pwa\n", encoding="utf-8")
    mock_state = MockCDPBrowserState()
    pwa_target = mock_state.targets.pop("target-yt-pwa")

    bridge = CDPBridge(
        devtools_port_path=port_file,
        env={},
        ws_connector=mock_state.connector(),
    )
    nav_attempts = 0
    orig_nav = bridge.navigate_sync

    def _delayed_pwa_nav(*args: Any, **kwargs: Any) -> dict[str, Any]:
        nonlocal nav_attempts
        nav_attempts += 1
        # PWA target appears on 3rd attempt (1st pre-launch, 2nd post-launch miss, 3rd hit)
        if nav_attempts == 3:
            mock_state.targets["target-yt-pwa"] = pwa_target
        return orig_nav(*args, **kwargs)

    bridge.navigate_sync = _delayed_pwa_nav  # type: ignore[method-assign]

    res = windows_os.open_url(
        "https://www.youtube.com/playlist?list=WL",
        app_catalog_fetcher=lambda: [{"Name": "YouTube", "AppID": "Chrome._crx_yt"}],
        app_uri_handler_finder=lambda _host: [],
        url_protocol_checker=lambda _scheme: False,
        window_enumerator=lambda: [],
        popen_fn=lambda cmd, **_kw: MagicMock(),
        desktop_name_fn=lambda: "Default",
        cdp_bridge=bridge,
        pwa_launch_settle_s=0.6,
    )
    assert res["ok"] is True
    assert res["status"] == "navigated"
    assert res["cdp_used"] is True
    assert res["url"] == "https://www.youtube.com/playlist?list=WL"
    assert nav_attempts == 3
    assert "Do NOT call open_url again" not in res["guidance"]
    assert "CDP" in res["guidance"]
    bridge.close()


# ===========================================================================
# 10. Regression Fixes: Empty Browser Normalization, Persistent CDP WS Reuse,
#     CDP PWA Guidance, Preferred-Browser History, ui_batch coord_space, UIA Timeout
# ===========================================================================


def test_open_url_empty_or_whitespace_browser_routes_via_layer3_pwa_cdp(tmp_path: Path) -> None:
    """browser='' or browser='   ' is normalized to None and routes through Layer 3 PWA CDP."""
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text("9333\n/devtools/browser/empty-browser\n", encoding="utf-8")
    mock_state = MockCDPBrowserState()
    bridge = CDPBridge(
        devtools_port_path=port_file,
        env={},
        ws_connector=mock_state.connector(),
    )
    u32 = _FakeU32Input(fg_hwnd=502, iconic_hwnds={501})
    open_windows = [
        windows_os.EnumeratedWindow(
            hwnd=501,
            title="YouTube",
            process_name="chrome.exe",
            is_visible=True,
        ),
    ]

    try:
        for blank_browser in ("", "   ", "\t"):
            mock_state.targets["target-yt-pwa"]["url"] = "https://www.youtube.com/"
            mock_state.targets["target-yt-pwa"]["title"] = "YouTube"
            res = windows_os.open_url(
                "https://www.youtube.com/playlist?list=WL",
                browser=blank_browser,
                app_uri_handler_finder=lambda _u: [],
                url_protocol_checker=lambda _s: None,
                app_catalog_fetcher=lambda: [{"Name": "YouTube", "AppID": "Chrome._crx_yt"}],
                window_enumerator=lambda: open_windows,
                user32=u32,
                desktop_name_fn=lambda: "Default",
                cdp_bridge=bridge,
            )
            assert res["ok"] is True
            assert res["routed_to_installed_app"] is True
            assert res["status"] == "navigated"
            assert res["cdp_used"] is True
            assert res["url"] == "https://www.youtube.com/playlist?list=WL"

        # When no installed app exists, browser="" falls back to default shell open (no ValueError)
        shell_opened: list[str] = []
        res_default = windows_os.open_url(
            "https://example.org/docs",
            browser="   ",
            app_uri_handler_finder=lambda _u: [],
            url_protocol_checker=lambda _s: None,
            app_catalog_fetcher=list,
            window_enumerator=list,
            startfile_fn=shell_opened.append,
            desktop_name_fn=lambda: "Default",
            cdp_bridge=bridge,
        )
        assert res_default["ok"] is True
        assert res_default["browser"] == "default"
        assert res_default["status"] == "opened"
        assert shell_opened == ["https://example.org/docs"]
    finally:
        bridge.close()


def test_open_url_cdp_pwa_guidance_vs_non_cdp_fallback_guidance(tmp_path: Path) -> None:
    """CDP-navigated PWAs use CDP guidance; non-CDP fallback preserves legacy guidance."""
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text("9333\n/devtools/browser/guidance\n", encoding="utf-8")
    mock_state = MockCDPBrowserState()
    bridge = CDPBridge(
        devtools_port_path=port_file,
        env={},
        ws_connector=mock_state.connector(),
    )
    offline_state = MockCDPBrowserState()
    offline_state.fail_connect_with = ConnectionRefusedError("disabled")
    offline_bridge = CDPBridge(
        devtools_port_path=port_file,
        env={},
        ws_connector=offline_state.connector(),
    )

    u32 = _FakeU32Input(fg_hwnd=900)
    open_windows = [
        windows_os.EnumeratedWindow(
            hwnd=501,
            title="YouTube",
            process_name="chrome.exe",
            is_visible=True,
        ),
    ]

    try:
        # 1. Running PWA window navigated via CDP
        res_running_cdp = windows_os.open_url(
            "https://www.youtube.com/playlist?list=WL",
            app_uri_handler_finder=lambda _u: [],
            url_protocol_checker=lambda _s: None,
            app_catalog_fetcher=lambda: [{"Name": "YouTube", "AppID": "Chrome._crx_yt"}],
            window_enumerator=lambda: open_windows,
            user32=u32,
            desktop_name_fn=lambda: "Default",
            cdp_bridge=bridge,
        )
        assert res_running_cdp["cdp_used"] is True
        assert "Do NOT call open_url again" not in res_running_cdp["guidance"]
        assert "CDP" in res_running_cdp["guidance"]
        assert "open_url(url)" in res_running_cdp["guidance"]

        # 2. Layer 3 installed_app with existing CDP target (no matching Win32 window returned)
        res_l3_cdp = windows_os.open_url(
            "https://www.youtube.com/feed/history",
            app_uri_handler_finder=lambda _u: [],
            url_protocol_checker=lambda _s: None,
            app_catalog_fetcher=lambda: [{"Name": "YouTube", "AppID": "Chrome._crx_yt"}],
            window_enumerator=list,
            user32=u32,
            desktop_name_fn=lambda: "Default",
            cdp_bridge=bridge,
        )
        assert res_l3_cdp["cdp_used"] is True
        assert "Do NOT call open_url again" not in res_l3_cdp["guidance"]
        assert "CDP" in res_l3_cdp["guidance"]

        # 3. Non-CDP fallback preserves _INSTALLED_APP_ROUTED_GUIDANCE
        res_fallback = windows_os.open_url(
            "https://www.youtube.com/playlist?list=WL",
            app_uri_handler_finder=lambda _u: [],
            url_protocol_checker=lambda _s: None,
            app_catalog_fetcher=lambda: [{"Name": "YouTube", "AppID": "Chrome._crx_yt"}],
            window_enumerator=lambda: open_windows,
            user32=u32,
            desktop_name_fn=lambda: "Default",
            cdp_bridge=offline_bridge,
        )
        assert res_fallback["status"] == "focused"
        assert res_fallback.get("cdp_used") is not True
        assert "Do NOT call open_url again" in res_fallback["guidance"]
    finally:
        bridge.close()
        offline_bridge.close()


@pytest.mark.asyncio
async def test_cdp_bridge_reuses_persistent_ws_across_async_and_sync_calls(
    tmp_path: Path,
) -> None:
    """Multiple async or sync CDP calls share one persistent browser WS and clean up cleanly."""
    import concurrent.futures

    from clea.tools.cdp_bridge import set_default_cdp_bridge

    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text("9333\n/devtools/browser/persistent\n", encoding="utf-8")

    # 1. Pure async sequence reuses a single browser WebSocket
    created_async_sockets: list[_MockCDPWebSocket] = []
    async_state = MockCDPBrowserState()

    async def _async_connector(ws_url: str, *, open_timeout: float = 1.5) -> _MockCDPWebSocket:
        async_state.connected_urls.append(ws_url)
        ws = _MockCDPWebSocket(async_state)
        created_async_sockets.append(ws)
        return ws

    async_bridge = CDPBridge(
        devtools_port_path=port_file,
        env={},
        ws_connector=_async_connector,
    )
    assert async_bridge.is_connected is False
    nav_res = await async_bridge.cdp_navigate(
        "https://www.youtube.com/playlist?list=WL", prefer_pwa=True, create_if_missing=False
    )
    assert nav_res["ok"] is True
    assert async_bridge.is_connected is True
    snap_res = await async_bridge.cdp_snapshot(prefer_pwa=True)
    assert snap_res["ok"] is True
    act_res = await async_bridge.cdp_act("b1", "click")
    assert act_res["ok"] is True
    state_res = await async_bridge.cdp_get_active_page_state(prefer_pwa=True)
    assert state_res["ok"] is True
    hist_res = await async_bridge.cdp_navigate_history("back")
    assert hist_res["ok"] is True

    assert len(async_state.connected_urls) == 1
    assert len(created_async_sockets) == 1
    assert created_async_sockets[0].closed is False

    await async_bridge.aclose()
    assert created_async_sockets[0].closed is True
    assert async_bridge.is_connected is False

    # 2. Sync wrappers (including across worker threads) reuse ONE connection on shared bg loop
    created_sync_sockets: list[_MockCDPWebSocket] = []
    sync_state = MockCDPBrowserState()

    async def _sync_connector(ws_url: str, *, open_timeout: float = 1.5) -> _MockCDPWebSocket:
        sync_state.connected_urls.append(ws_url)
        ws = _MockCDPWebSocket(sync_state)
        created_sync_sockets.append(ws)
        return ws

    sync_bridge = CDPBridge(
        devtools_port_path=port_file,
        env={},
        ws_connector=_sync_connector,
    )
    set_default_cdp_bridge(sync_bridge)
    try:
        r1 = await asyncio.to_thread(
            sync_bridge.navigate_sync,
            "https://www.youtube.com/playlist?list=WL",
            prefer_pwa=True,
            create_if_missing=False,
        )
        assert r1["ok"] is True
        r2 = await asyncio.to_thread(sync_bridge.snapshot_sync, prefer_pwa=True)
        assert r2["ok"] is True
        with concurrent.futures.ThreadPoolExecutor(max_workers=2) as pool:
            f_act = pool.submit(sync_bridge.act_sync, "b1", "click")
            f_state = pool.submit(sync_bridge.get_active_page_state_sync, prefer_pwa=True)
            assert f_act.result()["ok"] is True
            assert f_state.result()["ok"] is True

        assert len(sync_state.connected_urls) == 1
        assert len(created_sync_sockets) == 1
        bg_thread = sync_bridge._bg_thread
        assert bg_thread is not None and bg_thread.is_alive()
    finally:
        # Clearing default bridge must close the persistent socket and stop the bg loop thread
        set_default_cdp_bridge(None)

    assert created_sync_sockets[0].closed is True
    assert sync_bridge.is_connected is False
    assert sync_bridge._bg_thread is None
    assert not bg_thread.is_alive()


@pytest.mark.asyncio
async def test_cdp_bridge_reconnects_after_dropped_socket_and_avoids_duplicate_mutation(
    tmp_path: Path,
) -> None:
    """Dropped persistent sockets trigger 1x transparent reconnect without double-clicking."""
    import websockets.exceptions
    from websockets.protocol import State

    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text("9333\n/devtools/browser/reconnect\n", encoding="utf-8")

    mock_state = MockCDPBrowserState()
    sockets: list[Any] = []

    class _DroppableWS(_MockCDPWebSocket):
        def __init__(self, state: MockCDPBrowserState) -> None:
            super().__init__(state)
            self.state = State.OPEN
            self.fail_next_send: Exception | None = None
            self.fail_on_method: tuple[str, Exception] | None = None

        async def send(self, data: str) -> None:
            if self.fail_next_send is not None:
                exc = self.fail_next_send
                self.fail_next_send = None
                self.state = State.CLOSED
                raise exc
            if self.fail_on_method is not None:
                frame = json.loads(data)
                target_method, exc = self.fail_on_method
                if frame.get("method") == target_method:
                    self.fail_on_method = None
                    self.state = State.CLOSED
                    raise exc
            await super().send(data)

        async def close(self) -> None:
            self.closed = True
            self.state = State.CLOSED

    async def _connector(ws_url: str, *, open_timeout: float = 1.5) -> _DroppableWS:
        mock_state.connected_urls.append(ws_url)
        ws = _DroppableWS(mock_state)
        sockets.append(ws)
        return ws

    bridge = CDPBridge(
        devtools_port_path=port_file,
        env={},
        ws_connector=_connector,
    )
    try:
        # Call 1 establishes WS #1
        snap1 = await bridge.cdp_snapshot(prefer_pwa=True)
        assert snap1["ok"] is True
        assert len(sockets) == 1

        # Case A: WS #1 closes cleanly (State.CLOSED) before Call 2 -> opens WS #2 cleanly
        sockets[0].state = State.CLOSED
        state2 = await bridge.cdp_get_active_page_state(prefer_pwa=True)
        assert state2["ok"] is True
        assert len(sockets) == 2

        # Case B: WS #2 appears open, then abruptly raises ConnectionClosedError on Call 3 ->
        # transparently reconnects to WS #3 and retries once!
        sockets[1].fail_next_send = websockets.exceptions.ConnectionClosedError(None, None)
        snap3 = await bridge.cdp_snapshot(prefer_pwa=True)
        assert snap3["ok"] is True
        assert len(sockets) == 3

        # Case B2: Reused WS #3 drops on pre-mutation Target.attachToTarget during cdp_act ->
        # safe to reconnect to WS #4 and execute click exactly once!
        mock_state.mouse_events.clear()
        sockets[2].fail_on_method = ("Target.attachToTarget", ConnectionResetError("peer reset"))
        act4 = await bridge.cdp_act("b1", "click")
        assert act4["ok"] is True
        assert len(sockets) == 4
        pressed_events = [e for e in mock_state.mouse_events if e.get("type") == "mousePressed"]
        assert len(pressed_events) == 1

        # Case C: Non-idempotent safety — if WS #4 drops *after* mousePressed is dispatched
        # (during mouseReleased), cdp_act must NOT retry/double-click, and must invalidate WS #4.
        mock_state.mouse_events.clear()
        sockets[3].fail_on_method = (
            "Input.dispatchMouseEvent",
            ConnectionResetError("drop during click"),
        )
        # Allow mouseMoved to succeed, drop on mousePressed (after mark_mutated)
        orig_send = sockets[3].send
        mouse_dispatch_seen = 0

        async def _drop_on_second_mouse_event(data: str) -> None:
            nonlocal mouse_dispatch_seen
            frame = json.loads(data)
            if frame.get("method") == "Input.dispatchMouseEvent":
                mouse_dispatch_seen += 1
                if mouse_dispatch_seen == 2:
                    sockets[3].state = State.CLOSED
                    raise ConnectionResetError("dropped mid-click")
            await orig_send(data)

        sockets[3].fail_on_method = None
        sockets[3].send = _drop_on_second_mouse_event  # type: ignore[method-assign]
        act_fail = await bridge.cdp_act("b1", "click")
        assert act_fail["ok"] is False
        assert act_fail["status"] == "cdp_error"
        assert len(sockets) == 4  # did NOT retry after mutation boundary
        assert bridge.is_connected is False

        # Subsequent call transparently opens WS #5
        state5 = await bridge.cdp_get_active_page_state(prefer_pwa=True)
        assert state5["ok"] is True
        assert len(sockets) == 5
    finally:
        await bridge.aclose()

    # Also verify sync wrappers transparently reconnect on the shared background loop
    sync_sockets: list[_DroppableWS] = []
    sync_state = MockCDPBrowserState()

    async def _sync_droppable_connector(ws_url: str, *, open_timeout: float = 1.5) -> _DroppableWS:
        sync_state.connected_urls.append(ws_url)
        ws = _DroppableWS(sync_state)
        sync_sockets.append(ws)
        return ws

    sync_bridge = CDPBridge(
        devtools_port_path=port_file,
        env={},
        ws_connector=_sync_droppable_connector,
    )
    try:
        r1 = await asyncio.to_thread(sync_bridge.snapshot_sync, prefer_pwa=True)
        assert r1["ok"] is True
        assert len(sync_sockets) == 1

        # Drop socket mid-call on next reused sync invocation -> reconnects seamlessly
        sync_sockets[0].fail_next_send = websockets.exceptions.ConnectionClosedError(None, None)
        r2 = await asyncio.to_thread(
            sync_bridge.navigate_sync,
            "https://www.youtube.com/playlist?list=WL",
            prefer_pwa=True,
            create_if_missing=False,
        )
        assert r2["ok"] is True
        assert r2["url"] == "https://www.youtube.com/playlist?list=WL"
        assert len(sync_sockets) == 2

        # Subsequent sync call reuses WS #2 (still 2 total connects)
        r3 = await asyncio.to_thread(sync_bridge.get_active_page_state_sync, prefer_pwa=True)
        assert r3["ok"] is True
        assert len(sync_sockets) == 2
    finally:
        sync_bridge.close()


@pytest.mark.asyncio
async def test_cdp_bridge_switches_connection_when_preferred_browser_changes(
    tmp_path: Path,
) -> None:
    """Switching preferred_browser from Chrome to Edge reconnects to Edge's DevToolsActivePort."""
    chrome_port = tmp_path / "Google" / "Chrome" / "User Data" / "DevToolsActivePort"
    chrome_port.parent.mkdir(parents=True)
    chrome_port.write_text("9222\n/devtools/browser/chrome-id\n", encoding="utf-8")

    edge_port = tmp_path / "Microsoft" / "Edge" / "User Data" / "DevToolsActivePort"
    edge_port.parent.mkdir(parents=True)
    edge_port.write_text("9223\n/devtools/browser/edge-id\n", encoding="utf-8")

    mock_state = MockCDPBrowserState()
    bridge = CDPBridge(
        env={"LOCALAPPDATA": str(tmp_path)},
        ws_connector=mock_state.connector(),
    )
    try:
        # Two Chrome calls reuse Chrome's single connection
        res_c1 = await bridge.cdp_get_active_page_state(preferred_browser="chrome")
        res_c2 = await bridge.cdp_get_active_page_state(preferred_browser="chrome.exe")
        assert res_c1["ok"] is True
        assert res_c2["ok"] is True
        assert mock_state.connected_urls == ["ws://127.0.0.1:9222/devtools/browser/chrome-id"]

        # Switching to Edge connects to Edge's endpoint; second Edge call reuses Edge's connection
        res_e1 = await bridge.cdp_get_active_page_state(preferred_browser="edge")
        res_e2 = await bridge.cdp_get_active_page_state(preferred_browser="msedge.exe")
        assert res_e1["ok"] is True
        assert res_e2["ok"] is True
        assert mock_state.connected_urls == [
            "ws://127.0.0.1:9222/devtools/browser/chrome-id",
            "ws://127.0.0.1:9223/devtools/browser/edge-id",
        ]
    finally:
        await bridge.aclose()


def test_gui_action_navigate_history_passes_preferred_browser_from_app_name(
    tmp_path: Path,
) -> None:
    """gui_action('navigate_back'/'navigate_forward', app_name=...) forwards Chromium browser."""
    chrome_port = tmp_path / "Google" / "Chrome" / "User Data" / "DevToolsActivePort"
    chrome_port.parent.mkdir(parents=True)
    chrome_port.write_text("9222\n/devtools/browser/chrome-hist\n", encoding="utf-8")
    edge_port = tmp_path / "Microsoft" / "Edge" / "User Data" / "DevToolsActivePort"
    edge_port.parent.mkdir(parents=True)
    edge_port.write_text("9223\n/devtools/browser/edge-hist\n", encoding="utf-8")
    brave_port = tmp_path / "BraveSoftware" / "Brave-Browser" / "User Data" / "DevToolsActivePort"
    brave_port.parent.mkdir(parents=True)
    brave_port.write_text("9224\n/devtools/browser/brave-hist\n", encoding="utf-8")

    mock_state = MockCDPBrowserState()
    bridge = CDPBridge(
        env={"LOCALAPPDATA": str(tmp_path)},
        ws_connector=mock_state.connector(),
    )
    try:
        res_edge = windows_os.gui_action(
            "navigate_back",
            app_name="Microsoft Edge",
            cdp_bridge=bridge,
        )
        assert res_edge["ok"] is True
        assert mock_state.connected_urls[-1] == "ws://127.0.0.1:9223/devtools/browser/edge-hist"

        res_brave = windows_os.gui_action(
            "navigate_forward",
            app_name="Brave Browser",
            cdp_bridge=bridge,
        )
        assert res_brave["ok"] is True
        assert mock_state.connected_urls[-1] == "ws://127.0.0.1:9224/devtools/browser/brave-hist"

        # Non-browser app_name (e.g. PWA title 'YouTube') passes preferred_browser=None
        captured_pref: list[str | None] = []
        orig_nav_hist = bridge.navigate_history_sync

        def _spy_hist(direction: str = "back", **kw: Any) -> dict[str, Any]:
            captured_pref.append(kw.get("preferred_browser"))
            return orig_nav_hist(direction, **kw)

        bridge.navigate_history_sync = _spy_hist  # type: ignore[method-assign]
        res_pwa = windows_os.gui_action("navigate_back", app_name="YouTube", cdp_bridge=bridge)
        assert res_pwa["ok"] is True
        assert captured_pref == [None]
    finally:
        bridge.close()


def test_ui_batch_forwards_coord_space_and_preserves_legacy_compatibility() -> None:
    """ui_batch forwards coord_space on click_at steps, scales DPI accurately, and validates."""
    from clea.core.protocol import OsUiBatchParams, OsUiBatchRequest, UiBatchStep, parse_frame

    # 1. Protocol model round-trip with coord_space + float coords
    req = OsUiBatchRequest(
        params=OsUiBatchParams(
            steps=[
                UiBatchStep(
                    op="click_at",
                    x=500.0,
                    y=250.5,
                    coord_space="normalized_1000",
                ),
                UiBatchStep(
                    op="click_at",
                    x=384,
                    y=216,
                    coord_space="image",
                    image_width=768,
                    image_height=432,
                ),
            ]
        )
    )
    parsed = parse_frame(req.model_dump_json())
    assert isinstance(parsed, OsUiBatchRequest)
    assert parsed.params.steps[0].coord_space == "normalized_1000"
    assert parsed.params.steps[1].coord_space == "image"
    assert parsed.params.steps[1].image_width == 768

    # 2. Real gui_action scaling through ui_batch using recorded screen geometry (150% DPI)
    windows_os.set_last_screen_geometry(
        image_width=768,
        image_height=432,
        screen_width=1920,
        screen_height=1080,
        virtual_left=0,
        virtual_top=0,
        dpi_scale=1.5,
        coord_space="normalized_1000",
    )
    windows_os.set_ui_snapshot_state(
        {
            "ok": True,
            "snapshot_id": "snap-batch-cs",
            "window_title": "TestApp",
            "hwnd": 77,
            "elements": [],
        }
    )
    u32 = _FakeU32Input(fg_hwnd=77)
    res_scaled = windows_os.ui_batch(
        [
            {"op": "click_at", "x": 500, "y": 500, "coord_space": "normalized_1000"},
            {
                "op": "click_at",
                "x": 192,
                "y": 108,
                "coord_space": "image",
                "image_width": 768,
                "image_height": 432,
            },
        ],
        ui_snapshot_fn=lambda **_kw: {
            "ok": True,
            "window_title": "TestApp",
            "hwnd": 77,
            "elements": [],
        },
        window_title_fn=lambda: "TestApp",
        user32=u32,
    )
    assert res_scaled["status"] == "completed"
    assert res_scaled["steps_executed"] == 2
    assert u32.cursor_moves == [(960, 540), (480, 270)]

    # 3. Backwards compatibility: omitting coord_space must NOT pass coord_space kwarg
    legacy_calls: list[dict[str, Any]] = []

    def _strict_legacy_runner(
        *,
        action: str,
        x: int,
        y: int,
        clicks: int = 1,
        button: str = "left",
        user32: Any = None,
    ) -> dict[str, Any]:
        legacy_calls.append({"action": action, "x": x, "y": y, "clicks": clicks, "button": button})
        return {"ok": True, "status": "completed"}

    res_legacy = windows_os.ui_batch(
        [{"op": "click_at", "x": 120, "y": 240}],
        ui_snapshot_fn=lambda **_kw: {"ok": True, "window_title": "TestApp", "elements": []},
        gui_action_fn=_strict_legacy_runner,
        window_title_fn=lambda: "TestApp",
        user32=u32,
    )
    assert res_legacy["status"] == "completed"
    assert legacy_calls == [
        {"action": "click_at", "x": 120, "y": 240, "clicks": 1, "button": "left"}
    ]

    # 4. Validation rejects unsupported coord_space or invalid image dimensions
    with pytest.raises(ValueError, match="unsupported coord_space"):
        windows_os.ui_batch([{"op": "click_at", "x": 10, "y": 10, "coord_space": "bogus"}])
    with pytest.raises(ValueError, match="image_width"):
        windows_os.ui_batch(
            [{"op": "click_at", "x": 10, "y": 10, "coord_space": "image", "image_width": 0}]
        )


def test_vision_probe_uia_text_uses_2_5s_powershell_timeout(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Win32ScreenVisionBackend.probe_uia_text uses a 2.5s timeout for cold PS 5.1 startup."""
    import clea.tools.vision as vision_mod

    recorded_timeouts: list[float] = []

    def _fake_ps(script: str, *, timeout: float = 0.0, check: bool = False) -> Any:
        recorded_timeouts.append(timeout)
        return subprocess.CompletedProcess(
            args=["powershell"], returncode=0, stdout="Native Window Text\n", stderr=""
        )

    monkeypatch.setattr(vision_mod, "get_window_process_name", lambda _hwnd: "notepad.exe")
    monkeypatch.setattr(vision_mod, "_is_module_available", lambda _mod: False)
    monkeypatch.setattr(vision_mod, "run_powershell_hidden", _fake_ps)

    backend = vision_mod.Win32ScreenVisionBackend(user32=MagicMock(), gdi32=None, dwmapi=None)
    text = backend.probe_uia_text(12345)
    assert text == "Native Window Text"
    assert recorded_timeouts == [2.5]


# ===========================================================================
# 11. Adversarial Review Regression Tests (Concurrency, Multi-Browser Pooling,
#     Target Selection, Protocol Validation, Voice ui_batch Geometry)
# ===========================================================================


@pytest.mark.asyncio
async def test_cdp_bridge_real_loopback_recv_timeout_preserves_persistent_ws() -> None:
    """A per-command recv() timeout keeps a healthy persistent loopback WebSocket alive."""
    from websockets.asyncio.server import serve

    connect_count = 0

    async def _handler(ws: Any) -> None:
        nonlocal connect_count
        connect_count += 1
        async for raw in ws:
            req = json.loads(raw)
            mid = req["id"]
            method = req.get("method")
            if method == "Target.getTargets":
                # Also emit a malformed non-JSON frame first to verify resilient skipping
                await ws.send("not-valid-json")
                await ws.send(
                    json.dumps(
                        {
                            "id": mid,
                            "result": {
                                "targetInfos": [
                                    {
                                        "targetId": "t1",
                                        "type": "page",
                                        "url": "https://example.com/",
                                        "title": "Example",
                                    }
                                ]
                            },
                        }
                    )
                )
            elif method == "Browser.getWindowForTarget":
                await ws.send(
                    json.dumps(
                        {
                            "id": mid,
                            "result": {"windowId": 1, "bounds": {"windowState": "normal"}},
                        }
                    )
                )
            elif method == "Target.attachToTarget":
                await ws.send(json.dumps({"id": mid, "result": {"sessionId": "s1"}}))
            elif method == "Target.detachFromTarget":
                await ws.send(json.dumps({"id": mid, "result": {}}))
            elif method == "Runtime.evaluate":
                await ws.send(
                    json.dumps(
                        {
                            "id": mid,
                            "result": {
                                "result": {
                                    "value": {
                                        "url": "https://example.com/",
                                        "title": "Example",
                                        "readyState": "complete",
                                        "hasFocus": True,
                                        "visibilityState": "visible",
                                        "isPwa": False,
                                    }
                                }
                            },
                        }
                    )
                )
            elif method == "Accessibility.getFullAXTree":
                await asyncio.sleep(0.35)
                await ws.send(json.dumps({"id": mid, "result": {"nodes": []}}))

    async with serve(_handler, "127.0.0.1", 0) as server:
        port = server.sockets[0].getsockname()[1]
        bridge = CDPBridge(ws_url=f"ws://127.0.0.1:{port}", command_timeout=0.2)
        try:
            snap = await bridge.cdp_snapshot()
            assert snap["ok"] is False
            assert snap["status"] == "cdp_error"
            assert bridge.is_connected is True

            # Follow-up call reuses the exact same WebSocket (still 1 connection total)
            state = await bridge.cdp_get_active_page_state()
            assert state["ok"] is True
            assert state["url"] == "https://example.com/"
            assert connect_count == 1
        finally:
            await bridge.aclose()


@pytest.mark.asyncio
async def test_cdp_bridge_concurrent_multi_browser_and_edge_ref_routing(tmp_path: Path) -> None:
    """Concurrent Chrome+Edge calls do not kill each other, and cdp_act routes b1 back to Edge."""
    chrome_port = tmp_path / "Google" / "Chrome" / "User Data" / "DevToolsActivePort"
    chrome_port.parent.mkdir(parents=True)
    chrome_port.write_text("9222\n/devtools/browser/chrome-pool\n", encoding="utf-8")

    edge_port = tmp_path / "Microsoft" / "Edge" / "User Data" / "DevToolsActivePort"
    edge_port.parent.mkdir(parents=True)
    edge_port.write_text("9223\n/devtools/browser/edge-pool\n", encoding="utf-8")

    chrome_targets = [
        {
            "targetId": "yt-pwa",
            "type": "page",
            "url": "https://www.youtube.com/playlist?list=WL",
            "title": "Watch Later - YouTube",
        },
        {
            "targetId": "c-tab-active",
            "type": "page",
            "url": "https://docs.python.org/",
            "title": "Python Docs",
        },
        {
            "targetId": "c-tab-store",
            "type": "page",
            "url": "https://chromewebstore.google.com/",
            "title": "Chrome Web Store",
        },
    ]
    edge_targets = [
        {
            "targetId": "e-tab-1",
            "type": "page",
            "url": "https://example.com/edge",
            "title": "Edge Page",
        },
    ]
    connected_urls: list[str] = []
    history_navigated: list[tuple[str, str]] = []
    clicked_on: list[tuple[str, str]] = []

    class _PerBrowserWS:
        def __init__(self, url: str) -> None:
            self.url = url
            self.is_edge = "edge" in url
            self.closed = False
            self._q: asyncio.Queue[str] = asyncio.Queue()
            self._sessions: dict[str, str] = {}

        async def send(self, data: str) -> None:
            if self.closed:
                raise ConnectionResetError(f"closed {self.url}")
            req = json.loads(data)
            mid = req["id"]
            method = req["method"]
            params = req.get("params") or {}
            sid = req.get("sessionId")
            targets = edge_targets if self.is_edge else chrome_targets
            if method == "Target.getTargets":
                await asyncio.sleep(0.02)
                if self.closed:
                    raise ConnectionResetError(f"closed mid-flight {self.url}")
                res: dict[str, Any] = {"targetInfos": targets}
            elif method == "Browser.getWindowForTarget":
                tid = params["targetId"]
                res = {"windowId": 1 if tid == "yt-pwa" else 2, "bounds": {"windowState": "normal"}}
            elif method == "Target.attachToTarget":
                tid = params["targetId"]
                if not any(t["targetId"] == tid for t in targets):
                    await self._q.put(
                        json.dumps(
                            {"id": mid, "error": {"code": -32000, "message": f"No target {tid}"}}
                        )
                    )
                    return
                nsid = f"s-{tid}"
                self._sessions[nsid] = tid
                res = {"sessionId": nsid}
            elif method in (
                "Target.detachFromTarget",
                "Target.activateTarget",
                "Page.navigate",
                "DOM.scrollIntoViewIfNeeded",
            ):
                res = {}
            elif method == "Runtime.evaluate":
                tid = self._sessions[sid]
                target = next(t for t in targets if t["targetId"] == tid)
                res = {
                    "result": {
                        "value": {
                            "url": target["url"],
                            "title": target["title"],
                            "readyState": "complete",
                            "hasFocus": tid in {"c-tab-active", "e-tab-1"},
                            "visibilityState": "visible",
                            "isPwa": tid == "yt-pwa",
                        }
                    }
                }
            elif method == "Accessibility.getFullAXTree":
                tid = self._sessions[sid]
                res = {
                    "nodes": [
                        {
                            "nodeId": "1",
                            "role": {"value": "button"},
                            "name": {"value": f"Btn {tid}"},
                            "backendDOMNodeId": 101,
                        }
                    ]
                }
            elif method == "Page.getNavigationHistory":
                res = {
                    "currentIndex": 1,
                    "entries": [
                        {"id": 1, "url": "https://prev/", "title": "Prev"},
                        {"id": 2, "url": "https://cur/", "title": "Cur"},
                    ],
                }
            elif method == "Page.navigateToHistoryEntry":
                history_navigated.append((self.url, self._sessions[sid]))
                res = {}
            elif method == "DOM.getBoxModel":
                res = {"model": {"content": [10, 10, 50, 10, 50, 50, 10, 50]}}
            elif method == "Input.dispatchMouseEvent":
                if params.get("type") == "mousePressed":
                    clicked_on.append((self.url, self._sessions[sid]))
                res = {}
            else:
                res = {}
            await self._q.put(json.dumps({"id": mid, "result": res}))

        async def recv(self) -> str:
            return await self._q.get()

        async def close(self) -> None:
            self.closed = True

    async def _connector(ws_url: str, *, open_timeout: float = 1.5) -> _PerBrowserWS:
        connected_urls.append(ws_url)
        return _PerBrowserWS(ws_url)

    bridge = CDPBridge(env={"LOCALAPPDATA": str(tmp_path)}, ws_connector=_connector)
    try:
        # 1. Concurrent Chrome + Edge calls succeed cleanly without closing each other
        r_chrome, r_edge = await asyncio.gather(
            bridge.cdp_get_active_page_state(preferred_browser="chrome"),
            bridge.cdp_get_active_page_state(preferred_browser="edge"),
        )
        assert r_chrome["ok"] is True and r_chrome["url"] == "https://docs.python.org/"
        assert r_edge["ok"] is True and r_edge["url"] == "https://example.com/edge"
        assert len(connected_urls) == 2

        # 2. Switching back to Chrome reuses existing Chrome socket (still 2 connects total)
        r_chrome_again = await bridge.cdp_get_active_page_state(preferred_browser="chrome")
        assert r_chrome_again["ok"] is True
        assert len(connected_urls) == 2

        # 3. Edge snapshot -> Chrome query -> cdp_act("b1") still routes b1 to Edge!
        snap_e = await bridge.cdp_snapshot(preferred_browser="edge")
        assert snap_e["ok"] is True and snap_e["target_id"] == "e-tab-1"
        await bridge.cdp_get_active_page_state(preferred_browser="chrome")
        act_e = await bridge.cdp_act("b1", "click")
        assert act_e["ok"] is True
        assert clicked_on[-1] == ("ws://127.0.0.1:9223/devtools/browser/edge-pool", "e-tab-1")

        # 4. ui_snapshot(app_name="chrome") picks focused Python Docs, NOT Chrome Web Store
        snap_c = await asyncio.to_thread(
            windows_os.ui_snapshot,
            app_name="chrome",
            cdp_bridge=bridge,
            user32=_FakeU32Input(fg_hwnd=10),
            window_enumerator=lambda: [
                windows_os.EnumeratedWindow(
                    hwnd=10,
                    title="Python Docs - Google Chrome",
                    process_name="chrome.exe",
                    is_visible=True,
                )
            ],
        )
        assert snap_c["ok"] is True
        assert snap_c["target_id"] == "c-tab-active"
        assert snap_c["title"] == "Python Docs"

        # 5. navigate_back with app_name="chrome" after PWA nav does NOT hijack YouTube PWA
        await bridge.cdp_navigate(
            "https://www.youtube.com/playlist?list=WL",
            prefer_pwa=True,
            create_if_missing=False,
        )
        hist_res = await asyncio.to_thread(
            windows_os.gui_action,
            "navigate_back",
            app_name="chrome",
            cdp_bridge=bridge,
        )
        assert hist_res["ok"] is True
        assert history_navigated == [
            ("ws://127.0.0.1:9222/devtools/browser/chrome-pool", "c-tab-active")
        ]

        # 6. capture_desktop_state routes msedge.exe to Edge's CDP endpoint
        class _EdgeFocus:
            def get_foreground_context(self) -> WindowContext:
                return WindowContext(
                    hwnd=123,
                    title="Edge Page - Microsoft Edge",
                    process_name="msedge.exe",
                )

        captured = await asyncio.to_thread(
            capture_desktop_state,
            focus_backend=_EdgeFocus(),  # type: ignore[arg-type]
            cdp_bridge=bridge,
            include_media=False,
        )
        assert captured["url"] == "https://example.com/edge"
    finally:
        await bridge.aclose()


@pytest.mark.asyncio
async def test_cdp_legacy_9222_rejects_mismatched_browser_and_title_hint_prioritizes_large_tabs(
    tmp_path: Path,
) -> None:
    """Legacy /json/version rejects Chrome for Edge requests, and >20-tab probe prioritizes hint."""
    # 1. /json/version returning Chrome must NOT satisfy preferred_browser="edge"
    bridge_legacy = CDPBridge(
        env={"LOCALAPPDATA": str(tmp_path)},
        http_json_fetcher=lambda _url, _to: {
            "Browser": "Chrome/144.0.7559.133",
            "webSocketDebuggerUrl": "ws://127.0.0.1:9222/devtools/browser/chrome-legacy",
        },
    )
    assert await bridge_legacy.discover_ws_urls(preferred_browser="chrome") == [
        "ws://127.0.0.1:9222/devtools/browser/chrome-legacy"
    ]
    assert await bridge_legacy.discover_ws_urls(preferred_browser="edge") == []

    # 2. With 25 tabs in one window, target #24 matching title_hint still gets eval_probe=True
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text("9222\n/devtools/browser/many-tabs\n", encoding="utf-8")
    probed_targets: list[str] = []
    many_targets = [
        {
            "targetId": f"tab-{i}",
            "type": "page",
            "url": f"https://example.com/page/{i}",
            "title": "Target Watch Later" if i == 24 else f"Filler Tab {i}",
        }
        for i in range(25)
    ]

    class _ManyTabsWS:
        def __init__(self) -> None:
            self.closed = False
            self._q: asyncio.Queue[str] = asyncio.Queue()
            self._sessions: dict[str, str] = {}

        async def send(self, data: str) -> None:
            req = json.loads(data)
            mid, method, params, sid = (
                req["id"],
                req["method"],
                req.get("params") or {},
                req.get("sessionId"),
            )
            if method == "Target.getTargets":
                res: dict[str, Any] = {"targetInfos": many_targets}
            elif method == "Browser.getWindowForTarget":
                res = {"windowId": 1, "bounds": {"windowState": "normal"}}
            elif method == "Target.attachToTarget":
                tid = params["targetId"]
                probed_targets.append(tid)
                self._sessions[f"s-{tid}"] = tid
                res = {"sessionId": f"s-{tid}"}
            elif method == "Runtime.evaluate":
                tid = self._sessions[sid]
                t = next(x for x in many_targets if x["targetId"] == tid)
                res = {
                    "result": {
                        "value": {
                            "url": t["url"],
                            "title": t["title"],
                            "readyState": "complete",
                            "hasFocus": tid == "tab-24",
                            "visibilityState": "visible",
                            "isPwa": False,
                        }
                    }
                }
            else:
                res = {}
            await self._q.put(json.dumps({"id": mid, "result": res}))

        async def recv(self) -> str:
            return await self._q.get()

        async def close(self) -> None:
            self.closed = True

    bridge_many = CDPBridge(
        devtools_port_path=port_file,
        env={},
        ws_connector=lambda _u, open_timeout=1.5: asyncio.sleep(0, result=_ManyTabsWS()),
    )
    try:
        st = await bridge_many.cdp_get_active_page_state(title_hint="Target Watch Later")
        assert st["ok"] is True
        assert st["target_id"] == "tab-24"
        assert "tab-24" in probed_targets
    finally:
        await bridge_many.aclose()


def test_cdp_snapshot_status_ui_batch_preservation_protocol_and_state_diff_merge(
    tmp_path: Path,
) -> None:
    """Verify no_active_target preservation, ui_batch CDP state, OsUiBatchResult, and diff merge."""
    from clea.core.circuit_breaker import merge_tool_state_diff
    from clea.core.protocol import OsGuiActionResult, OsUiActResult, OsUiBatchResult

    # 1. Connected CDP with 0 tabs preserves no_active_target & cdp_available=True
    port_file = tmp_path / "DevToolsActivePort"
    port_file.write_text("9222\n/devtools/browser/empty\n", encoding="utf-8")

    class _EmptyTargetsWS:
        def __init__(self) -> None:
            self.closed = False
            self._q: asyncio.Queue[str] = asyncio.Queue()

        async def send(self, data: str) -> None:
            req = json.loads(data)
            await self._q.put(json.dumps({"id": req["id"], "result": {"targetInfos": []}}))

        async def recv(self) -> str:
            return await self._q.get()

        async def close(self) -> None:
            self.closed = True

    b_empty = CDPBridge(
        devtools_port_path=port_file,
        env={},
        ws_connector=lambda _u, open_timeout=1.5: asyncio.sleep(0, result=_EmptyTargetsWS()),
    )
    try:
        s_empty = windows_os.ui_snapshot(
            app_name="chrome",
            cdp_bridge=b_empty,
            user32=_FakeU32Input(fg_hwnd=10),
            window_enumerator=lambda: [
                windows_os.EnumeratedWindow(
                    hwnd=10,
                    title="New Tab - Google Chrome",
                    process_name="chrome.exe",
                    is_visible=True,
                )
            ],
        )
        assert s_empty["ok"] is False
        assert s_empty["status"] == "no_active_target"
        assert s_empty["cdp_available"] is True
    finally:
        b_empty.close()

    # 2. ui_batch on CDP preserves source="cdp" and does not pollute _UI_REF_STORE
    windows_os.clear_ui_ref_store()
    b_res = windows_os.ui_batch(
        [{"op": "wait", "seconds": 0.0}],
        ui_snapshot_fn=lambda **_kw: {
            "ok": True,
            "source": "cdp",
            "snapshot_id": "cdp_keep",
            "window_title": "Python Docs",
            "elements": [{"ref": "b1", "role": "button", "name": "OK"}],
        },
        window_title_fn=lambda: "Python Docs",
        user32=_FakeU32Input(fg_hwnd=10),
    )
    assert b_res["ok"] is True
    assert windows_os._LAST_UI_SNAPSHOT is not None
    assert windows_os._LAST_UI_SNAPSHOT.get("source") == "cdp"
    assert len(windows_os._UI_REF_STORE) == 0

    # 3. OsUiBatchResult validates _chromium_cdp_unavailable_snapshot (process_name allowed)
    snap_unavail = windows_os._chromium_cdp_unavailable_snapshot(
        window_title="YouTube", process_name="chrome.exe"
    )
    validated_batch = OsUiBatchResult.model_validate(
        {
            "ok": True,
            "status": "completed",
            "steps_executed": 1,
            "step_results": [{"step": 0, "op": "wait", "status": "ok"}],
            "snapshot": snap_unavail,
        }
    )
    assert validated_batch.snapshot is not None
    assert validated_batch.snapshot.process_name == "chrome.exe"
    assert "hint" in OsGuiActionResult.model_fields
    assert "hint" in OsUiActResult.model_fields

    # 4. merge_tool_state_diff preserves daemon page_title over generic Win32 window title
    cap_diff = compute_state_diff(
        {"hwnd": 1, "title": "YouTube", "process_name": "chrome.exe"},
        {"hwnd": 1, "title": "YouTube", "process_name": "chrome.exe"},
    )
    daemon_res = {
        "state_diff": compute_state_diff(
            {
                "hwnd": 1,
                "title": "YouTube",
                "process_name": "chrome.exe",
                "url": "https://www.youtube.com/",
                "page_title": "Home - YouTube",
            },
            {
                "hwnd": 1,
                "title": "YouTube",
                "process_name": "chrome.exe",
                "url": "https://www.youtube.com/playlist?list=WL",
                "page_title": "Watch Later - YouTube",
            },
        )
    }
    merged = merge_tool_state_diff(cap_diff, daemon_res)
    assert merged is not None
    assert merged["before"]["page_title"] == "Home - YouTube"
    assert merged["after"]["page_title"] == "Watch Later - YouTube"

    # 5. open_url(url, browser="chrome.exe") uses CDP instead of skipping it
    port_file.write_text("9333\n/devtools/browser/chrome-exe\n", encoding="utf-8")
    mock_state = MockCDPBrowserState()
    b_exe = CDPBridge(devtools_port_path=port_file, env={}, ws_connector=mock_state.connector())
    try:
        res_exe = windows_os.open_url(
            "https://www.youtube.com/playlist?list=WL",
            browser="chrome.exe",
            app_uri_handler_finder=lambda _u: [],
            url_protocol_checker=lambda _s: None,
            app_catalog_fetcher=list,
            window_enumerator=list,
            desktop_name_fn=lambda: "Default",
            cdp_bridge=b_exe,
        )
        assert res_exe["ok"] is True
        assert res_exe["cdp_used"] is True
        assert res_exe["is_pwa"] is False
    finally:
        b_exe.close()


@pytest.mark.asyncio
async def test_voice_runner_ui_batch_injects_last_screen_geometry_on_click_at() -> None:
    """VoiceRunner injects _last_screen_geometry into ui_batch click_at normalized_1000 steps."""
    from clea.core.protocol import OsUiBatchRequest
    from clea.surfaces.voice.backends.base import ToolCallEvent
    from clea.surfaces.voice.config import VoiceConfig
    from clea.surfaces.voice.runner import VoiceRunner

    sent_rpcs: list[Any] = []
    sent_tool_resps: list[Any] = []

    class _FakeBackend:
        async def send_tool_responses(self, responses: list[Any]) -> None:
            sent_tool_resps.extend(responses)

    runner = VoiceRunner(VoiceConfig(), token="test-token")
    runner._last_screen_geometry = {
        "image_width": 768,
        "image_height": 432,
        "screen_width": 2560,
        "screen_height": 1440,
        "virtual_left": -2560,
        "virtual_top": 0,
        "dpi_scale": 1.5,
        "coord_space": "normalized_1000",
    }

    async def _fake_rpc(req: Any, *, timeout: float = 10.0) -> dict[str, Any]:
        sent_rpcs.append(req)
        return {"ok": True, "status": "completed", "steps_executed": 1, "step_results": []}

    runner._rpc = _fake_rpc  # type: ignore[method-assign]

    await runner._handle_tool_call(
        ToolCallEvent(
            call_id="c-batch-geom",
            name="ui_batch",
            arguments={
                "steps": [
                    {"op": "click_at", "x": 500, "y": 500, "coord_space": "normalized_1000"},
                ]
            },
        ),
        _FakeBackend(),  # type: ignore[arg-type]
    )

    assert len(sent_rpcs) == 1
    assert isinstance(sent_rpcs[0], OsUiBatchRequest)
    step = sent_rpcs[0].params.steps[0]
    assert step.coord_space == "normalized_1000"
    assert step.screen_width == 2560
    assert step.screen_height == 1440
    assert step.virtual_left == -2560
    assert step.virtual_top == 0
    assert step.dpi_scale == 1.5
    assert step.image_width == 768
    assert step.image_height == 432
    assert len(sent_tool_resps) == 1 and sent_tool_resps[0].is_error is False

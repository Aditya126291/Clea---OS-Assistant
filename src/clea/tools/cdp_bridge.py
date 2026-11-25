"""Lean native Python Chrome / Chromium PWA DevTools Protocol (CDP) bridge.

Controls both regular Chrome tabs and installed Chromium PWAs (which share the
same browser process) over the browser-level CDP WebSocket using ``websockets``
and Python stdlib only — zero local LLMs and zero Playwright/Node subprocesses.

Discovery order (zero-config):
  1. Environment override: ``CLEA_CDP_WS_URL`` or ``CLEA_CDP_URL``
  2. Native ``DevToolsActivePort`` file (Chrome 144+ ``chrome://inspect/#remote-debugging``)
     at ``%LOCALAPPDATA%\\Google\\Chrome\\User Data\\DevToolsActivePort``, with optional
     Edge and Brave fallbacks
  3. Legacy ``http://127.0.0.1:9222/json/version`` probe

Always communicates over the browser-level WebSocket using ``Target.getTargets``,
``Target.attachToTarget(flatten=True)``, ``Target.activateTarget``, and
``Target.createTarget`` (never assuming HTTP ``/json/list`` is enabled).
"""

from __future__ import annotations

import asyncio
import atexit
import concurrent.futures
import contextlib
import json
import logging
import os
import re
import threading
import time
import urllib.parse
import urllib.request
import uuid
from collections.abc import Awaitable, Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import websockets
from websockets.asyncio.client import connect as ws_connect

logger = logging.getLogger(__name__)

CDP_REMOTE_DEBUGGING_HINT = "Enable remote debugging at chrome://inspect/#remote-debugging"
DEFAULT_CONNECT_TIMEOUT_S = 1.5
DEFAULT_COMMAND_TIMEOUT_S = 3.0
DEFAULT_NAV_SETTLE_TIMEOUT_S = 2.5
DEFAULT_ACT_SETTLE_TIMEOUT_S = 1.5
DEFAULT_MAX_SNAPSHOT_ELEMENTS = 60
DEFAULT_MAX_SNAPSHOT_CHARS = 6000
MAX_LABEL_CHARS = 140
MAX_VALUE_CHARS = 120

CHROMIUM_PROCESS_NAMES: frozenset[str] = frozenset(
    {
        "chrome",
        "chrome.exe",
        "msedge",
        "msedge.exe",
        "brave",
        "brave.exe",
    }
)

_BROWSER_APP_ALIASES: frozenset[str] = frozenset(
    {
        "chrome",
        "google chrome",
        "edge",
        "msedge",
        "microsoft edge",
        "brave",
        "brave browser",
    }
)

_INTERACTIVE_AX_ROLES: frozenset[str] = frozenset(
    {
        "link",
        "button",
        "textbox",
        "searchbox",
        "tab",
        "menuitem",
        "menuitemcheckbox",
        "menuitemradio",
        "checkbox",
        "combobox",
        "radio",
        "switch",
        "slider",
        "spinbutton",
        "option",
    }
)

_MEANINGFUL_AX_ROLES: frozenset[str] = _INTERACTIVE_AX_ROLES | frozenset({"heading"})

_ROLE_NORMALIZATION: dict[str, str] = {
    "link": "link",
    "button": "button",
    "togglebutton": "button",
    "popupbutton": "button",
    "textbox": "textbox",
    "textarea": "textbox",
    "searchbox": "searchbox",
    "tab": "tab",
    "menuitem": "menuitem",
    "menuitemcheckbox": "menuitem",
    "menuitemradio": "menuitem",
    "checkbox": "checkbox",
    "combobox": "combobox",
    "comboboxgrouping": "combobox",
    "comboboxmenubutton": "combobox",
    "textfieldwithcombobox": "combobox",
    "radio": "radio",
    "radiobutton": "radio",
    "switch": "switch",
    "slider": "slider",
    "spinbutton": "spinbutton",
    "option": "option",
    "listboxoption": "option",
    "heading": "heading",
}

_STRIPPED_HOST_SUBDOMAINS: tuple[str, ...] = (
    "www.",
    "m.",
    "mobile.",
    "open.",
    "web.",
    "app.",
    "music.",
    "play.",
)

_CDP_KEY_MAP: dict[str, dict[str, Any]] = {
    "enter": {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "text": "\r"},
    "return": {"key": "Enter", "code": "Enter", "windowsVirtualKeyCode": 13, "text": "\r"},
    "tab": {"key": "Tab", "code": "Tab", "windowsVirtualKeyCode": 9},
    "escape": {"key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27},
    "esc": {"key": "Escape", "code": "Escape", "windowsVirtualKeyCode": 27},
    "backspace": {"key": "Backspace", "code": "Backspace", "windowsVirtualKeyCode": 8},
    "delete": {"key": "Delete", "code": "Delete", "windowsVirtualKeyCode": 46},
    "space": {"key": " ", "code": "Space", "windowsVirtualKeyCode": 32, "text": " "},
    "arrowup": {"key": "ArrowUp", "code": "ArrowUp", "windowsVirtualKeyCode": 38},
    "up": {"key": "ArrowUp", "code": "ArrowUp", "windowsVirtualKeyCode": 38},
    "arrowdown": {"key": "ArrowDown", "code": "ArrowDown", "windowsVirtualKeyCode": 40},
    "down": {"key": "ArrowDown", "code": "ArrowDown", "windowsVirtualKeyCode": 40},
    "arrowleft": {"key": "ArrowLeft", "code": "ArrowLeft", "windowsVirtualKeyCode": 37},
    "left": {"key": "ArrowLeft", "code": "ArrowLeft", "windowsVirtualKeyCode": 37},
    "arrowright": {"key": "ArrowRight", "code": "ArrowRight", "windowsVirtualKeyCode": 39},
    "right": {"key": "ArrowRight", "code": "ArrowRight", "windowsVirtualKeyCode": 39},
    "home": {"key": "Home", "code": "Home", "windowsVirtualKeyCode": 36},
    "end": {"key": "End", "code": "End", "windowsVirtualKeyCode": 35},
    "pageup": {"key": "PageUp", "code": "PageUp", "windowsVirtualKeyCode": 33},
    "pagedown": {"key": "PageDown", "code": "PageDown", "windowsVirtualKeyCode": 34},
}


class CDPUnavailableError(RuntimeError):
    """Raised internally when Chrome remote debugging cannot be reached."""


class CDPCommandError(RuntimeError):
    """Raised when a CDP JSON-RPC command returns an error object."""

    def __init__(self, method: str, message: str, code: int | None = None) -> None:
        self.method = method
        self.code = code
        super().__init__(f"CDP {method} error{f' ({code})' if code is not None else ''}: {message}")


@dataclass(frozen=True)
class CDPTargetInfo:
    """Classified Chromium page target."""

    target_id: str
    url: str
    title: str
    is_pwa: bool = False
    window_id: int | None = None
    window_state: str | None = None
    window_bounds: dict[str, Any] | None = None
    has_focus: bool = False
    visibility_state: str = "visible"
    browser_context_id: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "target_id": self.target_id,
            "url": self.url,
            "title": self.title,
            "is_pwa": self.is_pwa,
            "window_id": self.window_id,
            "window_state": self.window_state,
            "has_focus": self.has_focus,
        }


@dataclass(frozen=True)
class CachedCDPRef:
    """Cached browser accessibility node reference (`b1..bN`)."""

    ref: str
    target_id: str
    backend_dom_node_id: int | None
    role: str
    name: str
    value: str | None
    url: str
    title: str
    is_pwa: bool = False
    enabled: bool = True
    focused: bool = False
    browser_key: str | None = None


def is_chromium_process(process_name: str | None) -> bool:
    """Return True if *process_name* belongs to a supported Chromium browser/PWA."""
    if not process_name:
        return False
    low = process_name.strip().lower()
    if low in CHROMIUM_PROCESS_NAMES:
        return True
    stem = low[:-4] if low.endswith(".exe") else low
    return stem in CHROMIUM_PROCESS_NAMES


def is_chromium_app_name(app_name: str | None) -> bool:
    """Return True if an explicit app_name refers directly to Chrome/Edge/Brave."""
    if not app_name:
        return False
    low = " ".join(app_name.strip().lower().split())
    return low in _BROWSER_APP_ALIASES or is_chromium_process(low)


def is_cdp_ref(ref: str | None) -> bool:
    """Return True if *ref* is a CDP element reference (`b1`, `@b2`, etc.)."""
    if not isinstance(ref, str):
        return False
    cleaned = ref.strip().lstrip("@").lower()
    return bool(re.fullmatch(r"b[0-9]+", cleaned))


def normalize_cdp_ref(ref: str) -> str:
    """Normalize a CDP element ref string such as '@B1' -> 'b1'."""
    if not isinstance(ref, str) or not ref.strip():
        raise ValueError("CDP ref must be a non-empty string (e.g. 'b1').")
    cleaned = ref.strip().lstrip("@").lower()
    if not re.fullmatch(r"b[0-9]+", cleaned):
        raise ValueError(f"Invalid CDP element ref: {ref!r}. Expected format like 'b1'.")
    return cleaned


def _cdp_unavailable_payload(error: str | None = None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "cdp_available": False,
        "status": "cdp_unavailable",
        "hint": CDP_REMOTE_DEBUGGING_HINT,
    }
    if error:
        payload["error"] = error
    return payload


def _normalize_host(raw: str | None) -> str:
    """Extract normalized lowercase host without common subdomains (www/m/open/web/app)."""
    if not raw or not isinstance(raw, str):
        return ""
    s = raw.strip().lower()
    if not s:
        return ""
    if "://" in s or "/" in s or "." in s:
        candidate = s if "://" in s else f"https://{s}"
        with contextlib.suppress(Exception):
            parsed = urllib.parse.urlsplit(candidate)
            host = (parsed.hostname or "").lower().strip(".")
            if host:
                for prefix in _STRIPPED_HOST_SUBDOMAINS:
                    if host.startswith(prefix) and len(host) > len(prefix):
                        host = host[len(prefix) :]
                        break
                return host
    return s


def _extract_origin(url: str | None) -> str:
    if not url or "://" not in url:
        return ""
    with contextlib.suppress(Exception):
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme and parsed.netloc:
            return f"{parsed.scheme.lower()}://{parsed.netloc.lower()}"
    return ""


def _host_brand_token(host: str) -> str:
    if not host:
        return ""
    parts = [p for p in host.split(".") if p]
    if len(parts) >= 2:
        # Handle common 2-part public suffixes like co.uk / co.in
        if parts[-2] in {"co", "com", "org", "net", "gov", "edu", "ac"} and len(parts) >= 3:
            return parts[-3]
        return parts[-2]
    return parts[0]


def _normalize_browser_key(preferred_browser: str | None) -> str | None:
    """Map a browser/process name to a canonical Chromium family key ('chrome'|'edge'|'brave')."""
    if not preferred_browser:
        return None
    low = " ".join(preferred_browser.strip().lower().split())
    if "edge" in low:
        return "edge"
    if "brave" in low:
        return "brave"
    if "chrome" in low:
        return "chrome"
    return None


def _http_version_matches_browser(
    data: Mapping[str, Any],
    preferred_browser: str | None,
) -> bool:
    """Return True if a ``/json/version`` response is compatible with *preferred_browser*."""
    req_key = _normalize_browser_key(preferred_browser)
    if req_key is None:
        return True
    raw_browser = str(data.get("Browser") or "").strip().lower()
    if not raw_browser:
        return True
    if req_key == "edge":
        return "edg" in raw_browser
    if req_key == "chrome":
        return "edg" not in raw_browser and "brave" not in raw_browser
    if req_key == "brave":
        return "edg" not in raw_browser
    return True


def _default_devtools_port_entries(
    env: Mapping[str, str] | None = None,
    *,
    preferred_browser: str | None = None,
) -> list[tuple[Path, str]]:
    """Return ordered native ``(DevToolsActivePort path, browser_key)`` entries."""
    env_map = os.environ if env is None else env
    local_app_data = env_map.get("LOCALAPPDATA", "").strip()
    if not local_app_data:
        return []
    base = Path(local_app_data)
    by_browser: dict[str, Path] = {
        "chrome": base / "Google" / "Chrome" / "User Data" / "DevToolsActivePort",
        "edge": base / "Microsoft" / "Edge" / "User Data" / "DevToolsActivePort",
        "brave": base / "BraveSoftware" / "Brave-Browser" / "User Data" / "DevToolsActivePort",
    }
    browser_key = _normalize_browser_key(preferred_browser)
    if browser_key is not None:
        return [(by_browser[browser_key], browser_key)]
    return [
        (by_browser["chrome"], "chrome"),
        (by_browser["edge"], "edge"),
        (by_browser["brave"], "brave"),
    ]


def _default_devtools_port_paths(
    env: Mapping[str, str] | None = None,
    *,
    preferred_browser: str | None = None,
) -> list[Path]:
    """Return ordered native DevToolsActivePort paths (Chrome -> Edge -> Brave)."""
    return [
        path
        for path, _ in _default_devtools_port_entries(env, preferred_browser=preferred_browser)
    ]


def parse_devtools_active_port_file(path: Path) -> str | None:
    """Parse a Chromium ``DevToolsActivePort`` file into a browser WebSocket URL."""
    try:
        if not path.is_file():
            return None
        raw = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None

    lines = [line.strip() for line in raw.splitlines() if line.strip()]
    if len(lines) < 2:
        return None
    port_str, ws_path = lines[0], lines[1]
    if not port_str.isdigit():
        return None
    port = int(port_str)
    if port < 1 or port > 65535:
        return None
    if not ws_path.startswith("/"):
        ws_path = f"/{ws_path}"
    return f"ws://127.0.0.1:{port}{ws_path}"


def _fetch_json_sync(url: str, timeout: float = DEFAULT_CONNECT_TIMEOUT_S) -> dict[str, Any] | None:
    """Perform a short-timeout local HTTP GET returning parsed JSON or None."""
    try:
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            if getattr(resp, "status", 200) != 200:
                return None
            raw = resp.read().decode("utf-8", errors="replace")
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _truncate_text(text: str, limit: int) -> str:
    clean = " ".join(text.split())
    if len(clean) <= limit:
        return clean
    return clean[: max(1, limit - 1)].rstrip() + "…"


def _extract_ax_str(field: Any) -> str:
    if isinstance(field, dict):
        val = field.get("value")
        return "" if val is None else str(val)
    if field is None:
        return ""
    return str(field)


def _extract_ax_properties(node: dict[str, Any]) -> dict[str, Any]:
    props: dict[str, Any] = {}
    for item in node.get("properties") or []:
        if not isinstance(item, dict):
            continue
        pname = str(item.get("name") or "").strip().lower()
        if not pname:
            continue
        val_obj = item.get("value")
        if isinstance(val_obj, dict):
            props[pname] = val_obj.get("value")
        else:
            props[pname] = val_obj
    return props


def _quad_center(quad: Sequence[Any]) -> tuple[float, float] | None:
    if not isinstance(quad, Sequence) or len(quad) < 8:
        return None
    try:
        xs = [float(quad[0]), float(quad[2]), float(quad[4]), float(quad[6])]
        ys = [float(quad[1]), float(quad[3]), float(quad[5]), float(quad[7])]
    except (TypeError, ValueError):
        return None
    if (max(xs) - min(xs)) <= 0.0 or (max(ys) - min(ys)) <= 0.0:
        return None
    cx = sum(xs) / 4.0
    cy = sum(ys) / 4.0
    if cx < 0.0 or cy < 0.0:
        return None
    return (cx, cy)


_BROWSER_TITLE_SUFFIXES: tuple[str, ...] = (
    " - google chrome",
    " - microsoft edge",
    " - brave",
    " - chromium",
)


def _strip_browser_title_suffix(title: str) -> str:
    """Strip trailing Win32 browser window suffixes (e.g. ' - Google Chrome')."""
    low = " ".join((title or "").strip().lower().split())
    for suffix in _BROWSER_TITLE_SUFFIXES:
        if low.endswith(suffix) and len(low) > len(suffix):
            return low[: -len(suffix)].strip()
    return low


def _run_async_safe(coro: Awaitable[Any]) -> Any:
    """Execute an async coroutine safely whether or not an event loop is already running."""
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        return asyncio.run(coro)

    def _runner() -> Any:
        return asyncio.run(coro)

    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as pool:
        return pool.submit(_runner).result()


class _CDPConnection:
    """Minimal browser-level CDP JSON-RPC client supporting flattened target sessions."""

    def __init__(
        self,
        ws: Any,
        *,
        command_timeout: float = DEFAULT_COMMAND_TIMEOUT_S,
        browser_key: str | None = None,
    ) -> None:
        self._ws = ws
        self._command_timeout = command_timeout
        self.browser_key = browser_key
        self._next_id = 0
        self._lock = asyncio.Lock()
        self._closed = False
        self._ref_lock = threading.Lock()
        self._in_flight = 0
        self._retire_pending = False

    def acquire(self) -> None:
        with self._ref_lock:
            self._in_flight += 1

    async def release(self) -> None:
        should_close = False
        with self._ref_lock:
            if self._in_flight > 0:
                self._in_flight -= 1
            if self._in_flight == 0 and self._retire_pending:
                should_close = True
        if should_close:
            await self.close()

    async def retire(self) -> None:
        """Close this connection once all currently in-flight operations complete."""
        should_close = False
        with self._ref_lock:
            self._retire_pending = True
            if self._in_flight == 0:
                should_close = True
        if should_close:
            await self.close()

    @property
    def is_open(self) -> bool:
        """Return True if the underlying WebSocket transport appears open and usable."""
        if self._closed:
            return False
        ws = self._ws
        if ws is None:
            return False
        # websockets >= 14 asyncio ClientConnection exposes .state (State.OPEN)
        state = getattr(ws, "state", None)
        if state is not None:
            state_name = getattr(state, "name", None)
            if isinstance(state_name, str):
                return state_name.upper() == "OPEN"
        # Legacy websockets / custom test doubles
        closed_attr = getattr(ws, "closed", None)
        if isinstance(closed_attr, bool):
            return not closed_attr
        open_attr = getattr(ws, "open", None)
        if isinstance(open_attr, bool):
            return open_attr
        close_code = getattr(ws, "close_code", None)
        if close_code is not None:
            return False
        return True

    async def send(
        self,
        method: str,
        params: dict[str, Any] | None = None,
        *,
        session_id: str | None = None,
        timeout: float | None = None,
    ) -> dict[str, Any]:
        """Send a CDP command and wait for its matching response id."""
        eff_timeout = self._command_timeout if timeout is None else timeout
        async with self._lock:
            if not self.is_open:
                self._closed = True
                raise ConnectionError(f"CDP WebSocket connection is closed before sending {method}")
            self._next_id += 1
            msg_id = self._next_id
            payload: dict[str, Any] = {"id": msg_id, "method": method}
            if params:
                payload["params"] = params
            if session_id:
                payload["sessionId"] = session_id

            try:
                await asyncio.wait_for(self._ws.send(json.dumps(payload)), timeout=eff_timeout)
            except TimeoutError:
                self._closed = True
                raise
            except (websockets.WebSocketException, OSError, EOFError):
                self._closed = True
                raise

            deadline = time.monotonic() + eff_timeout
            while True:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError(f"CDP command {method} timed out after {eff_timeout:.1f}s")
                try:
                    raw = await asyncio.wait_for(self._ws.recv(), timeout=remaining)
                except TimeoutError:
                    raise
                except (websockets.WebSocketException, OSError, EOFError):
                    self._closed = True
                    raise
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", errors="replace")
                try:
                    frame = json.loads(raw)
                except (ValueError, TypeError):
                    continue
                if not isinstance(frame, dict):
                    continue
                if frame.get("id") != msg_id:
                    # Skip unsolicited CDP event notifications
                    continue
                if "error" in frame and frame["error"] is not None:
                    err = frame["error"]
                    if isinstance(err, dict):
                        raise CDPCommandError(
                            method,
                            str(err.get("message") or err),
                            code=err.get("code"),
                        )
                    raise CDPCommandError(method, str(err))
                result = frame.get("result")
                return result if isinstance(result, dict) else {}

    async def close(self) -> None:
        self._closed = True
        if self._ws is not None:
            with contextlib.suppress(Exception):
                await self._ws.close()


@dataclass
class _PooledCDPConnection:
    """Cached browser-level CDP WebSocket bound to an event loop and browser slot."""

    conn: _CDPConnection
    loop: asyncio.AbstractEventLoop
    ws_url: str
    browser_key: str | None
    last_used: float = 0.0


class CDPBridge:
    """Async + sync Chrome/PWA CDP bridge with persistent WS reuse and ref caching."""

    def __init__(
        self,
        *,
        ws_url: str | None = None,
        devtools_port_path: Path | str | None = None,
        devtools_port_paths: Sequence[Path | str] | None = None,
        legacy_version_url: str = "http://127.0.0.1:9222/json/version",
        connect_timeout: float = DEFAULT_CONNECT_TIMEOUT_S,
        command_timeout: float = DEFAULT_COMMAND_TIMEOUT_S,
        env: Mapping[str, str] | None = None,
        ws_connector: Callable[..., Awaitable[Any]] | None = None,
        http_json_fetcher: Callable[[str, float], dict[str, Any] | None] | None = None,
    ) -> None:
        self._explicit_ws_url = ws_url
        self._devtools_port_path = Path(devtools_port_path) if devtools_port_path else None
        self._devtools_port_paths = (
            [Path(p) for p in devtools_port_paths] if devtools_port_paths is not None else None
        )
        self._legacy_version_url = legacy_version_url
        self._connect_timeout = max(0.1, float(connect_timeout))
        self._command_timeout = max(0.2, float(command_timeout))
        self._env = env
        self._ws_connector = ws_connector
        self._http_json_fetcher = http_json_fetcher

        self._cache_lock = threading.RLock()
        self._refs_by_target: dict[str, dict[str, CachedCDPRef]] = {}
        self._latest_refs: dict[str, CachedCDPRef] = {}
        self._last_target_id: str | None = None
        self._last_ws_url: str | None = None

        # Persistent browser-level WebSocket pool (lazily opened & reused per loop/browser)
        self._state_lock = threading.RLock()
        self._pool: dict[tuple[int, str], _PooledCDPConnection] = {}
        self._loop_locks: dict[int, tuple[asyncio.AbstractEventLoop, asyncio.Lock]] = {}

        # Dedicated lazy background event-loop thread shared by all *_sync wrappers
        self._bg_lock = threading.RLock()
        self._bg_loop: asyncio.AbstractEventLoop | None = None
        self._bg_thread: threading.Thread | None = None

    @property
    def is_connected(self) -> bool:
        """Return True if any persistent browser WebSocket connection is currently open."""
        with self._state_lock:
            return any(entry.conn.is_open for entry in self._pool.values())

    def __enter__(self) -> CDPBridge:
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()

    async def __aenter__(self) -> CDPBridge:
        return self

    async def __aexit__(self, *_exc: object) -> None:
        await self.aclose()

    def _effective_env(self) -> Mapping[str, str]:
        return os.environ if self._env is None else self._env

    def _auto_discovery_disabled(self) -> bool:
        if (
            self._explicit_ws_url
            or self._devtools_port_path is not None
            or self._devtools_port_paths is not None
            or self._ws_connector is not None
            or self._http_json_fetcher is not None
            or self._env is not None
        ):
            return False
        flag = os.environ.get("CLEA_CDP_DISABLE_AUTO_DISCOVERY", "").strip().lower()
        return flag in {"1", "true", "yes", "on"}

    def _allow_legacy_http_probe(self) -> bool:
        if not self._legacy_version_url:
            return False
        if self._http_json_fetcher is not None:
            return True
        if self._ws_connector is not None:
            # Mocked WS transport without an injected HTTP fetcher: never hit live 127.0.0.1:9222
            return False
        flag = os.environ.get("CLEA_CDP_DISABLE_AUTO_DISCOVERY", "").strip().lower()
        return flag not in {"1", "true", "yes", "on"}

    def _has_pinned_endpoint(self) -> bool:
        env_map = self._effective_env()
        has_env_override = bool(
            (env_map.get("CLEA_CDP_WS_URL") or env_map.get("CLEA_CDP_URL") or "").strip()
        )
        return bool(
            self._explicit_ws_url
            or has_env_override
            or self._devtools_port_path is not None
            or self._devtools_port_paths is not None
        )

    async def _resolve_http_version(
        self,
        base_or_version_url: str,
        *,
        preferred_browser: str | None = None,
    ) -> str | None:
        fetcher = self._http_json_fetcher or _fetch_json_sync
        url_clean = base_or_version_url.strip().rstrip("/")
        if not url_clean.endswith("/json/version"):
            url_clean = f"{url_clean}/json/version"
        data = await asyncio.to_thread(fetcher, url_clean, self._connect_timeout)
        if isinstance(data, dict):
            if not _http_version_matches_browser(data, preferred_browser):
                return None
            ws_u = data.get("webSocketDebuggerUrl")
            if isinstance(ws_u, str) and ws_u.startswith(("ws://", "wss://")):
                return ws_u
        return None

    async def _discover_ws_candidates(
        self,
        *,
        preferred_browser: str | None = None,
    ) -> list[tuple[str, str | None]]:
        """Return ordered ``(ws_url, browser_key)`` candidates without connecting yet."""
        if self._auto_discovery_disabled():
            return []

        candidates: list[tuple[str, str | None]] = []
        seen: set[str] = set()

        def _add(url: str | None, browser_key: str | None = None) -> None:
            if url and url.startswith(("ws://", "wss://")) and url not in seen:
                seen.add(url)
                candidates.append((url, browser_key))

        # 1. Explicit ws_url or env override (CLEA_CDP_WS_URL / CLEA_CDP_URL)
        env_map = self._effective_env()
        raw_override = (
            self._explicit_ws_url
            or env_map.get("CLEA_CDP_WS_URL")
            or env_map.get("CLEA_CDP_URL")
            or ""
        ).strip()
        if raw_override:
            if raw_override.startswith(("ws://", "wss://")):
                _add(raw_override, None)
            elif raw_override.startswith(("http://", "https://")):
                resolved = await self._resolve_http_version(raw_override)
                _add(resolved, None)

        # 2. Native DevToolsActivePort file (Chrome -> Edge -> Brave, or preferred_browser)
        port_entries: list[tuple[Path, str | None]] = []
        if self._devtools_port_path is not None:
            port_entries.append((self._devtools_port_path, None))
        if self._devtools_port_paths is not None:
            port_entries.extend((p, None) for p in self._devtools_port_paths)
        elif self._devtools_port_path is None:
            port_entries.extend(
                _default_devtools_port_entries(env_map, preferred_browser=preferred_browser)
            )

        for path, b_key in port_entries:
            parsed = parse_devtools_active_port_file(path)
            _add(parsed, b_key)

        # 3. Legacy http://127.0.0.1:9222/json/version fallback
        if self._allow_legacy_http_probe() and (
            not candidates or self._http_json_fetcher is not None
        ):
            resolved_legacy = await self._resolve_http_version(
                self._legacy_version_url,
                preferred_browser=preferred_browser,
            )
            _add(resolved_legacy, _normalize_browser_key(preferred_browser))

        return candidates

    async def discover_ws_urls(
        self,
        *,
        preferred_browser: str | None = None,
    ) -> list[str]:
        """Return ordered candidate browser WebSocket URLs without connecting yet."""
        return [
            url
            for url, _ in await self._discover_ws_candidates(
                preferred_browser=preferred_browser
            )
        ]

    async def _try_connect_ws(
        self,
        ws_url: str,
        *,
        browser_key: str | None = None,
    ) -> _CDPConnection:
        if self._ws_connector is not None:
            ws = await asyncio.wait_for(
                self._ws_connector(ws_url, open_timeout=self._connect_timeout),
                timeout=self._connect_timeout,
            )
        else:
            ws = await ws_connect(
                ws_url,
                open_timeout=self._connect_timeout,
                close_timeout=1.0,
                ping_interval=None,
                max_size=16 * 1024 * 1024,
            )
        self._last_ws_url = ws_url
        return _CDPConnection(
            ws,
            command_timeout=self._command_timeout,
            browser_key=browser_key,
        )

    async def _open_connection(
        self,
        *,
        preferred_browser: str | None = None,
    ) -> tuple[_CDPConnection, str, str | None]:
        """Connect to the first reachable browser-level CDP WebSocket."""
        if self._auto_discovery_disabled():
            raise CDPUnavailableError(
                "No Chrome DevTools endpoint found (auto-discovery disabled)."
            )

        candidates = await self._discover_ws_candidates(preferred_browser=preferred_browser)
        tried: set[str] = set()
        last_err: Exception | None = None

        for ws_url, browser_key in candidates:
            tried.add(ws_url)
            try:
                conn = await self._try_connect_ws(ws_url, browser_key=browser_key)
                return conn, ws_url, browser_key
            except (
                OSError,
                TimeoutError,
                websockets.WebSocketException,
                RuntimeError,
                ValueError,
            ) as exc:
                last_err = exc
                logger.debug("CDP connect failed for %s: %s", ws_url, exc)
                continue

        # If a stale DevToolsActivePort failed to connect, still try legacy 9222 fallback
        if self._allow_legacy_http_probe() and self._http_json_fetcher is None and candidates:
            fallback_ws = await self._resolve_http_version(
                self._legacy_version_url,
                preferred_browser=preferred_browser,
            )
            if fallback_ws and fallback_ws not in tried:
                req_b = _normalize_browser_key(preferred_browser)
                try:
                    conn = await self._try_connect_ws(fallback_ws, browser_key=req_b)
                    return conn, fallback_ws, req_b
                except (
                    OSError,
                    TimeoutError,
                    websockets.WebSocketException,
                    RuntimeError,
                    ValueError,
                ) as exc:
                    last_err = exc
                    logger.debug("CDP legacy fallback failed for %s: %s", fallback_ws, exc)

        if not candidates and last_err is None:
            raise CDPUnavailableError(
                "No Chrome DevTools endpoint found (DevToolsActivePort missing and port 9222 "
                "unavailable)."
            )

        raise CDPUnavailableError(
            f"Unable to connect to Chrome CDP endpoint: {last_err}"
            if last_err is not None
            else "Unable to connect to Chrome CDP endpoint."
        )

    def _get_loop_lock(self, loop: asyncio.AbstractEventLoop) -> asyncio.Lock:
        loop_id = id(loop)
        with self._state_lock:
            stale_ids = [
                lid
                for lid, (lp, _lk) in self._loop_locks.items()
                if lp.is_closed() and lid != loop_id
            ]
            for lid in stale_ids:
                self._loop_locks.pop(lid, None)
            existing = self._loop_locks.get(loop_id)
            if existing is None or existing[0] is not loop:
                lock = asyncio.Lock()
                self._loop_locks[loop_id] = (loop, lock)
                return lock
            return existing[1]

    def _pick_reusable_locked(
        self,
        current_loop: asyncio.AbstractEventLoop,
        req_browser: str | None,
    ) -> _PooledCDPConnection | None:
        loop_id = id(current_loop)
        loop_entries = [
            (slot, entry)
            for (lid, slot), entry in self._pool.items()
            if lid == loop_id and entry.loop is current_loop and entry.conn.is_open
        ]
        if not loop_entries:
            return None

        by_slot = dict(loop_entries)
        if req_browser is not None:
            if req_browser in by_slot:
                return by_slot[req_browser]
            if self._has_pinned_endpoint() and "default" in by_slot:
                return by_slot["default"]
            return None

        # Unspecified browser: reuse pinned/default, else Chrome, else most recently used
        if "default" in by_slot:
            return by_slot["default"]
        if "chrome" in by_slot:
            return by_slot["chrome"]
        return max((e for _, e in loop_entries), key=lambda e: e.last_used)

    async def _retire_conn_safely(
        self,
        conn: _CDPConnection,
        conn_loop: asyncio.AbstractEventLoop | None,
        current_loop: asyncio.AbstractEventLoop | None,
    ) -> None:
        if conn_loop is None or conn_loop is current_loop:
            await conn.retire()
        elif not conn_loop.is_closed() and conn_loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(conn.retire(), conn_loop)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(fut.result, 2.0)
        else:
            with contextlib.suppress(Exception):
                await conn.retire()

    async def _close_conn_safely(
        self,
        conn: _CDPConnection,
        conn_loop: asyncio.AbstractEventLoop | None,
        current_loop: asyncio.AbstractEventLoop | None,
    ) -> None:
        if conn_loop is None or conn_loop is current_loop:
            await conn.close()
        elif not conn_loop.is_closed() and conn_loop.is_running():
            fut = asyncio.run_coroutine_threadsafe(conn.close(), conn_loop)
            with contextlib.suppress(Exception):
                await asyncio.to_thread(fut.result, 2.0)
        else:
            with contextlib.suppress(Exception):
                await conn.close()

    async def _ensure_connection(
        self,
        *,
        preferred_browser: str | None = None,
    ) -> tuple[_CDPConnection, bool]:
        """Return an acquired persistent browser-level connection, connecting lazily if needed."""
        current_loop = asyncio.get_running_loop()
        loop_id = id(current_loop)
        req_browser = _normalize_browser_key(preferred_browser)
        loop_lock = self._get_loop_lock(current_loop)

        async with loop_lock:
            dead_entries: list[_PooledCDPConnection] = []
            generic_candidates: list[_PooledCDPConnection] = []
            with self._state_lock:
                for key in list(self._pool.keys()):
                    entry = self._pool[key]
                    if entry.loop.is_closed() or not entry.conn.is_open:
                        dead_entries.append(self._pool.pop(key))

                picked = self._pick_reusable_locked(current_loop, req_browser)
                if picked is not None:
                    picked.last_used = time.monotonic()
                    picked.conn.acquire()
                elif req_browser is not None:
                    generic_candidates = [
                        entry
                        for (lid, _slot), entry in self._pool.items()
                        if lid == loop_id
                        and entry.loop is current_loop
                        and entry.conn.is_open
                        and entry.browser_key is None
                        and entry.ws_url
                    ]

            for dead in dead_entries:
                await self._retire_conn_safely(dead.conn, dead.loop, current_loop)

            if picked is not None:
                return picked.conn, True

            # If an open generic connection (e.g. legacy 9222) already matches the first
            # candidate URL for preferred_browser on this loop, reuse it instead of reconnecting.
            if generic_candidates and req_browser is not None:
                candidates = await self._discover_ws_candidates(
                    preferred_browser=preferred_browser
                )
                if candidates:
                    first_url = candidates[0][0]
                    with self._state_lock:
                        for entry in generic_candidates:
                            if entry.ws_url == first_url and entry.conn.is_open:
                                entry.last_used = time.monotonic()
                                entry.conn.acquire()
                                return entry.conn, True

            conn, ws_url, browser_key = await self._open_connection(
                preferred_browser=preferred_browser
            )
            eff_browser = browser_key or req_browser
            conn.browser_key = eff_browser
            slot = eff_browser or "default"
            pool_key = (loop_id, slot)

            replaced: list[_PooledCDPConnection] = []
            with self._state_lock:
                old = self._pool.get(pool_key)
                if old is not None and old.conn is not conn:
                    replaced.append(old)
                self._pool[pool_key] = _PooledCDPConnection(
                    conn=conn,
                    loop=current_loop,
                    ws_url=ws_url,
                    browser_key=eff_browser,
                    last_used=time.monotonic(),
                )
                conn.acquire()

            for stale in replaced:
                await self._retire_conn_safely(stale.conn, stale.loop, current_loop)

            return conn, False

    async def _invalidate_connection(self, failed_conn: _CDPConnection) -> None:
        """Detach and retire *failed_conn* from the persistent connection pool."""
        current_loop = asyncio.get_running_loop()
        loop_lock = self._get_loop_lock(current_loop)
        async with loop_lock:
            removed: list[_PooledCDPConnection] = []
            with self._state_lock:
                for key in list(self._pool.keys()):
                    if self._pool[key].conn is failed_conn:
                        removed.append(self._pool.pop(key))
            if removed:
                for entry in removed:
                    await self._retire_conn_safely(entry.conn, entry.loop, current_loop)
            else:
                with contextlib.suppress(Exception):
                    await failed_conn.retire()

    async def _call_with_conn(
        self,
        operation: Callable[[_CDPConnection, Callable[[], None]], Awaitable[Any]],
        *,
        preferred_browser: str | None = None,
    ) -> Any:
        """Run *operation(conn, mark_mutated)* reusing the persistent WS with 1x reconnect."""
        for attempt in range(2):
            conn, reused = await self._ensure_connection(preferred_browser=preferred_browser)
            mutated = False

            def _mark_mutated() -> None:
                nonlocal mutated
                mutated = True

            try:
                try:
                    result = await operation(conn, _mark_mutated)
                except TimeoutError:
                    # Keep healthy open browser sockets alive on per-command recv timeouts;
                    # only invalidate if the transport itself became closed (e.g. send timeout).
                    if not conn.is_open:
                        await self._invalidate_connection(conn)
                    raise
                except (websockets.WebSocketException, OSError, EOFError):
                    await self._invalidate_connection(conn)
                    if attempt == 0 and reused and not mutated:
                        logger.debug(
                            "CDP persistent WebSocket dropped; reconnecting and retrying once."
                        )
                        continue
                    raise

                if not conn.is_open:
                    await self._invalidate_connection(conn)
                return result
            finally:
                await conn.release()
        raise CDPUnavailableError("Unable to execute CDP operation after reconnect.")

    def _stop_sync_loop(self) -> None:
        with self._bg_lock:
            bg_loop = self._bg_loop
            bg_thread = self._bg_thread
            self._bg_loop = None
            self._bg_thread = None

        if bg_loop is not None and not bg_loop.is_closed():
            with contextlib.suppress(Exception):
                bg_loop.call_soon_threadsafe(bg_loop.stop)
        if (
            bg_thread is not None
            and bg_thread.is_alive()
            and threading.current_thread() is not bg_thread
        ):
            bg_thread.join(timeout=2.0)

    async def aclose(self) -> None:
        """Close all cached persistent browser WebSockets and stop the sync loop if active."""
        with self._state_lock:
            entries = list(self._pool.values())
            self._pool.clear()

        if entries:
            try:
                running_loop = asyncio.get_running_loop()
            except RuntimeError:
                running_loop = None
            for entry in entries:
                await self._close_conn_safely(entry.conn, entry.loop, running_loop)

        with self._bg_lock:
            has_bg = self._bg_loop is not None or self._bg_thread is not None
            on_bg_thread = (
                self._bg_thread is not None and threading.current_thread() is self._bg_thread
            )
        if has_bg and not on_bg_thread:
            await asyncio.to_thread(self._stop_sync_loop)

    def close(self) -> None:
        """Synchronously close all cached browser WebSockets and stop the sync event loop."""
        with self._bg_lock:
            bg_loop = self._bg_loop
            bg_thread = self._bg_thread
            self._bg_loop = None
            self._bg_thread = None

        with self._state_lock:
            entries = list(self._pool.values())
            self._pool.clear()

        for entry in entries:
            conn = entry.conn
            conn_loop = entry.loop
            if (
                bg_loop is not None
                and conn_loop is bg_loop
                and not bg_loop.is_closed()
                and bg_loop.is_running()
                and threading.current_thread() is not bg_thread
            ):
                with contextlib.suppress(Exception):
                    asyncio.run_coroutine_threadsafe(conn.close(), bg_loop).result(timeout=2.0)
            elif (
                conn_loop is not None
                and not conn_loop.is_closed()
                and conn_loop.is_running()
            ):
                try:
                    running_loop = asyncio.get_running_loop()
                except RuntimeError:
                    running_loop = None
                if conn_loop is running_loop:
                    with contextlib.suppress(Exception):
                        running_loop.create_task(conn.close())
                else:
                    with contextlib.suppress(Exception):
                        asyncio.run_coroutine_threadsafe(conn.close(), conn_loop).result(
                            timeout=2.0
                        )
            else:
                try:
                    running_loop = asyncio.get_running_loop()
                except RuntimeError:
                    with contextlib.suppress(Exception):
                        asyncio.run(conn.close())
                else:
                    with contextlib.suppress(Exception):
                        running_loop.create_task(conn.close())

        if bg_loop is not None and not bg_loop.is_closed():
            with contextlib.suppress(Exception):
                bg_loop.call_soon_threadsafe(bg_loop.stop)
        if (
            bg_thread is not None
            and bg_thread.is_alive()
            and threading.current_thread() is not bg_thread
        ):
            bg_thread.join(timeout=2.0)

    def _ensure_sync_loop(self) -> asyncio.AbstractEventLoop:
        with self._bg_lock:
            if (
                self._bg_loop is not None
                and not self._bg_loop.is_closed()
                and self._bg_thread is not None
                and self._bg_thread.is_alive()
            ):
                return self._bg_loop

            ready = threading.Event()
            loop_holder: list[asyncio.AbstractEventLoop] = []

            def _run_bg_loop() -> None:
                loop = asyncio.new_event_loop()
                asyncio.set_event_loop(loop)
                loop_holder.append(loop)
                ready.set()
                try:
                    loop.run_forever()
                finally:
                    try:
                        pending = asyncio.all_tasks(loop)
                        for task in pending:
                            task.cancel()
                        if pending:
                            loop.run_until_complete(
                                asyncio.gather(*pending, return_exceptions=True)
                            )
                        loop.run_until_complete(loop.shutdown_asyncgens())
                    except Exception:
                        pass
                    finally:
                        loop.close()

            thread = threading.Thread(
                target=_run_bg_loop,
                name="clea-cdp-bridge-loop",
                daemon=True,
            )
            thread.start()
            ready.wait(timeout=5.0)
            if not loop_holder:
                raise RuntimeError("Failed to start CDPBridge background event loop.")
            self._bg_loop = loop_holder[0]
            self._bg_thread = thread
            return self._bg_loop

    def _run_sync(self, coro: Awaitable[Any]) -> Any:
        """Execute *coro* on this bridge's shared background event loop so WS is reused."""
        loop = self._ensure_sync_loop()
        if threading.current_thread() is self._bg_thread:
            return _run_async_safe(coro)
        future = asyncio.run_coroutine_threadsafe(coro, loop)
        return future.result()

    async def _attach_flat(self, conn: _CDPConnection, target_id: str) -> str:
        res = await conn.send(
            "Target.attachToTarget",
            {"targetId": target_id, "flatten": True},
        )
        session_id = str(res.get("sessionId") or "")
        if not session_id:
            raise CDPCommandError("Target.attachToTarget", "Missing sessionId in response")
        return session_id

    async def _detach_quiet(self, conn: _CDPConnection, session_id: str) -> None:
        if not session_id or not conn.is_open:
            return
        with contextlib.suppress(Exception):
            await conn.send("Target.detachFromTarget", {"sessionId": session_id}, timeout=1.0)

    async def _eval_page_meta(
        self,
        conn: _CDPConnection,
        session_id: str,
    ) -> dict[str, Any]:
        """Evaluate lightweight display-mode, focus, URL, title, and readyState metadata."""
        expr = (
            "(() => ({"
            "url: location.href, "
            "title: document.title, "
            "readyState: document.readyState, "
            "hasFocus: document.hasFocus(), "
            "visibilityState: document.visibilityState, "
            "isPwa: Boolean("
            "window.matchMedia && ("
            "window.matchMedia('(display-mode: standalone)').matches || "
            "window.matchMedia('(display-mode: window-controls-overlay)').matches || "
            "window.matchMedia('(display-mode: minimal-ui)').matches"
            "))"
            "}))()"
        )
        res = await conn.send(
            "Runtime.evaluate",
            {"expression": expr, "returnByValue": True},
            session_id=session_id,
            timeout=min(self._command_timeout, 1.5),
        )
        val = (res.get("result") or {}).get("value")
        return val if isinstance(val, dict) else {}

    async def _inspect_target_item_on_conn(
        self,
        conn: _CDPConnection,
        item: dict[str, Any],
        *,
        eval_probe: bool = True,
        window_info: tuple[int | None, str | None, dict[str, Any] | None] | None = None,
    ) -> CDPTargetInfo:
        """Inspect a single raw Target.getTargets page entry."""
        target_id = str(item.get("targetId") or "").strip()
        url = str(item.get("url") or "")
        title = str(item.get("title") or "")

        if window_info is not None:
            window_id, window_state, window_bounds = window_info
        else:
            window_id = None
            window_state = None
            window_bounds = None
            with contextlib.suppress(CDPCommandError, TimeoutError):
                win_res = await conn.send(
                    "Browser.getWindowForTarget",
                    {"targetId": target_id},
                    timeout=min(self._command_timeout, 1.0),
                )
                raw_wid = win_res.get("windowId")
                if isinstance(raw_wid, int) and not isinstance(raw_wid, bool):
                    window_id = raw_wid
                b = win_res.get("bounds")
                if isinstance(b, dict):
                    window_bounds = dict(b)
                    ws_state = b.get("windowState")
                    if isinstance(ws_state, str):
                        window_state = ws_state

        is_pwa = False
        has_focus = False
        visibility_state = "visible"

        # Attach briefly to inspect authoritative display-mode: standalone media query
        if eval_probe and (url.startswith(("http://", "https://")) or not url):
            session_id = ""
            try:
                session_id = await self._attach_flat(conn, target_id)
                meta = await self._eval_page_meta(conn, session_id)
                if meta:
                    if "isPwa" in meta:
                        is_pwa = bool(meta["isPwa"])
                    if "hasFocus" in meta:
                        has_focus = bool(meta["hasFocus"])
                    if isinstance(meta.get("visibilityState"), str):
                        visibility_state = str(meta["visibilityState"])
                    if isinstance(meta.get("url"), str) and meta["url"]:
                        url = meta["url"]
                    if isinstance(meta.get("title"), str) and meta["title"]:
                        title = meta["title"]
            except (CDPCommandError, TimeoutError) as exc:
                logger.debug("Skipping Runtime.evaluate probe on target %s: %s", target_id, exc)
            finally:
                await self._detach_quiet(conn, session_id)

        return CDPTargetInfo(
            target_id=target_id,
            url=url,
            title=title,
            is_pwa=is_pwa,
            window_id=window_id,
            window_state=window_state,
            window_bounds=window_bounds,
            has_focus=has_focus,
            visibility_state=visibility_state,
            browser_context_id=(
                str(item["browserContextId"]) if item.get("browserContextId") else None
            ),
        )

    async def _list_targets_on_conn(
        self,
        conn: _CDPConnection,
        *,
        only_target_id: str | None = None,
        max_eval_probes: int = 20,
        title_hint: str | None = None,
        url_hint: str | None = None,
    ) -> list[CDPTargetInfo]:
        """Enumerate and classify open page targets into installed PWAs vs. regular tabs."""
        raw = await conn.send("Target.getTargets")
        raw_targets = raw.get("targetInfos") or []
        page_items: list[dict[str, Any]] = []

        for item in raw_targets:
            if not isinstance(item, dict):
                continue
            if str(item.get("type") or "") != "page":
                continue
            target_id = str(item.get("targetId") or "").strip()
            if not target_id:
                continue
            url = str(item.get("url") or "")
            if url.startswith(("devtools://", "chrome-extension://", "chrome-untrusted://")):
                continue
            if only_target_id is not None:
                if target_id == only_target_id:
                    return [await self._inspect_target_item_on_conn(conn, item, eval_probe=True)]
                continue
            page_items.append(item)

        if only_target_id is not None:
            return []

        # Query window bounds first (fast browser-level RPC, no target attach).
        # Tabs sharing a windowId with >=2 tabs belong to a multi-tab browser window,
        # whereas single-tab windows may be standalone PWAs and are prioritized for eval.
        win_infos: list[tuple[int | None, str | None, dict[str, Any] | None]] = []
        window_tab_counts: dict[int, int] = {}
        for item in page_items:
            target_id = str(item.get("targetId") or "").strip()
            w_id: int | None = None
            w_state: str | None = None
            w_bounds: dict[str, Any] | None = None
            with contextlib.suppress(CDPCommandError, TimeoutError):
                win_res = await conn.send(
                    "Browser.getWindowForTarget",
                    {"targetId": target_id},
                    timeout=min(self._command_timeout, 1.0),
                )
                raw_wid = win_res.get("windowId")
                if isinstance(raw_wid, int) and not isinstance(raw_wid, bool):
                    w_id = raw_wid
                    window_tab_counts[w_id] = window_tab_counts.get(w_id, 0) + 1
                b = win_res.get("bounds")
                if isinstance(b, dict):
                    w_bounds = dict(b)
                    ws_state = b.get("windowState")
                    if isinstance(ws_state, str):
                        w_state = ws_state
            win_infos.append((w_id, w_state, w_bounds))

        # Prioritize hint-matching targets first, then single-tab windows (potential PWAs),
        # then remaining multi-tab targets when bounding eval probes on large sessions.
        hint_clean = _strip_browser_title_suffix((title_hint or "").strip().lower())
        url_host = _normalize_host(url_hint) if url_hint else ""
        hint_idxs: list[int] = []
        if hint_clean or url_host:
            for i, item in enumerate(page_items):
                item_title = _strip_browser_title_suffix(str(item.get("title") or "").lower())
                item_host = _normalize_host(str(item.get("url") or ""))
                title_hit = bool(
                    hint_clean
                    and item_title
                    and (hint_clean in item_title or item_title in hint_clean)
                )
                host_hit = bool(
                    url_host
                    and item_host
                    and (
                        item_host == url_host
                        or item_host.endswith(f".{url_host}")
                        or url_host.endswith(f".{item_host}")
                    )
                )
                if title_hit or host_hit:
                    hint_idxs.append(i)

        probe_indices: set[int] = set()
        hint_set = set(hint_idxs)
        single_tab_idxs = [
            i
            for i, (wid, _, _) in enumerate(win_infos)
            if i not in hint_set and (wid is None or window_tab_counts.get(wid, 0) <= 1)
        ]
        single_set = set(single_tab_idxs)
        multi_tab_idxs = [
            i for i in range(len(page_items)) if i not in hint_set and i not in single_set
        ]
        for i in hint_idxs + single_tab_idxs + multi_tab_idxs:
            if len(probe_indices) >= max(1, max_eval_probes):
                break
            probe_indices.add(i)

        classified: list[CDPTargetInfo] = []
        for idx, item in enumerate(page_items):
            classified.append(
                await self._inspect_target_item_on_conn(
                    conn,
                    item,
                    eval_probe=(idx in probe_indices),
                    window_info=win_infos[idx],
                )
            )
        return classified

    async def list_targets(
        self,
        *,
        preferred_browser: str | None = None,
    ) -> list[CDPTargetInfo]:
        """Connect (or reuse persistent WS), list, and classify all open Chromium page targets."""

        async def _op(
            conn: _CDPConnection, _mark_mutated: Callable[[], None]
        ) -> list[CDPTargetInfo]:
            return await self._list_targets_on_conn(conn)

        return await self._call_with_conn(_op, preferred_browser=preferred_browser)

    def _select_target_from_list(
        self,
        targets: Sequence[CDPTargetInfo],
        origin_or_host: str | None = None,
        *,
        prefer_pwa: bool | None = False,
        title_hint: str | None = None,
    ) -> CDPTargetInfo | None:
        """Select the best matching target strictly respecting PWA vs. regular-tab boundaries."""
        if not targets:
            return None

        if prefer_pwa is True:
            pool = [t for t in targets if t.is_pwa]
        elif prefer_pwa is False:
            pool = [t for t in targets if not t.is_pwa]
        else:
            pool = list(targets)

        if not pool:
            return None

        target_origin = _extract_origin(origin_or_host)
        target_host = _normalize_host(origin_or_host)
        target_brand = _host_brand_token(target_host) if target_host else ""
        hint_raw_low = " ".join((title_hint or "").strip().lower().split())
        hint_clean = _strip_browser_title_suffix(hint_raw_low)

        def _score(t: CDPTargetInfo) -> tuple[int, int, int, int, int]:
            t_origin = _extract_origin(t.url)
            t_host = _normalize_host(t.url)
            t_brand = _host_brand_token(t_host) if t_host else ""
            t_title_low = " ".join((t.title or "").strip().lower().split())
            t_title_clean = _strip_browser_title_suffix(t_title_low)

            match_rank = 0
            if target_origin and t_origin and t_origin == target_origin:
                match_rank = 5
            elif target_host and t_host and (
                t_host == target_host
                or t_host.endswith(f".{target_host}")
                or target_host.endswith(f".{t_host}")
            ):
                match_rank = 4
            elif target_brand and t_brand and target_brand == t_brand:
                match_rank = 3
            elif target_brand and target_brand in t_title_low:
                match_rank = 2

            title_rank = 0
            title_specificity = 0
            if hint_clean:
                if hint_clean == t_title_clean or (hint_raw_low and hint_raw_low == t_title_low):
                    title_rank = 3
                    title_specificity = len(t_title_clean)
                elif (
                    hint_clean in t_title_clean
                    or (t_title_clean and t_title_clean in hint_clean)
                    or (t_title_low and hint_raw_low and t_title_low in hint_raw_low)
                ):
                    title_rank = 2
                    title_specificity = len(t_title_clean)

            focus_rank = 2 if t.has_focus else (1 if t.visibility_state == "visible" else 0)
            non_blank = 0 if t.url.startswith(("about:blank", "chrome://newtab")) else 1
            return (match_rank, title_rank, title_specificity, focus_rank, non_blank)

        scored = sorted(pool, key=_score, reverse=True)
        best = scored[0]

        if prefer_pwa is True and (origin_or_host or title_hint):
            # Strict origin/host/title match required for PWAs so we never hijack an unrelated PWA
            best_score = _score(best)
            if best_score[0] == 0 and best_score[1] == 0:
                return None

        return best

    async def find_target(
        self,
        origin_or_host: str | None = None,
        *,
        prefer_pwa: bool = False,
        title_hint: str | None = None,
    ) -> CDPTargetInfo | None:
        """Find a matching page target for *origin_or_host* respecting ``prefer_pwa``."""
        targets = await self.list_targets()
        return self._select_target_from_list(
            targets,
            origin_or_host,
            prefer_pwa=prefer_pwa,
            title_hint=title_hint,
        )

    async def _wait_for_settlement(
        self,
        conn: _CDPConnection,
        session_id: str,
        *,
        expected_url: str | None = None,
        before_url: str = "",
        before_title: str = "",
        timeout_s: float = DEFAULT_NAV_SETTLE_TIMEOUT_S,
    ) -> tuple[str, str]:
        """Poll briefly for URL/title/readyState settlement with a hard upper bound."""
        deadline = time.monotonic() + max(0.05, timeout_s)
        last_url = before_url
        last_title = before_title
        poll_idx = 0

        while True:
            if not conn.is_open:
                return last_url, last_title
            poll_idx += 1
            meta: dict[str, Any] = {}
            with contextlib.suppress(
                CDPCommandError, TimeoutError, websockets.WebSocketException, OSError, EOFError
            ):
                meta = await self._eval_page_meta(conn, session_id)
            if not conn.is_open:
                return last_url, last_title
            cur_url = str(meta.get("url") or last_url)
            cur_title = str(meta.get("title") or last_title)
            ready_state = str(meta.get("readyState") or "").lower()
            last_url, last_title = cur_url, cur_title

            url_settled = False
            if expected_url is not None and cur_url:
                if cur_url == expected_url:
                    url_settled = True
                else:
                    cur_parts = urllib.parse.urlsplit(cur_url)
                    exp_parts = urllib.parse.urlsplit(expected_url)
                    same_host = _normalize_host(cur_url) == _normalize_host(expected_url)
                    same_path = cur_parts.path.rstrip("/") == exp_parts.path.rstrip("/")
                    same_query = (not exp_parts.query) or (cur_parts.query == exp_parts.query)
                    url_settled = same_host and same_path and same_query
            changed = (cur_url != before_url) or (cur_title != before_title)

            if ready_state in {"interactive", "complete"}:
                if expected_url is not None:
                    # Do NOT bail out prematurely while waiting for a real navigation to commit;
                    # only return early once the expected URL settles or a real new URL commits.
                    if url_settled or (
                        cur_url
                        and cur_url != before_url
                        and not cur_url.startswith(("about:blank", "chrome://newtab"))
                    ):
                        return cur_url, cur_title
                elif changed or poll_idx >= 2:
                    return cur_url, cur_title

            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return last_url, last_title
            await asyncio.sleep(min(0.08, remaining))

    async def _restore_if_minimized(self, conn: _CDPConnection, target: CDPTargetInfo) -> None:
        if target.window_id is not None and target.window_state == "minimized":
            with contextlib.suppress(CDPCommandError, TimeoutError):
                await conn.send(
                    "Browser.setWindowBounds",
                    {"windowId": target.window_id, "bounds": {"windowState": "normal"}},
                    timeout=1.0,
                )

    async def cdp_navigate(
        self,
        url: str,
        *,
        prefer_pwa: bool = False,
        create_if_missing: bool = True,
        settle_timeout_s: float = DEFAULT_NAV_SETTLE_TIMEOUT_S,
        preferred_browser: str | None = None,
    ) -> dict[str, Any]:
        """Navigate a matched PWA or regular Chrome tab via CDP ``Page.navigate``."""

        async def _op(
            conn: _CDPConnection, mark_mutated: Callable[[], None]
        ) -> dict[str, Any]:
            session_id = ""
            try:
                targets = await self._list_targets_on_conn(conn, url_hint=url)
                matched = self._select_target_from_list(targets, url, prefer_pwa=prefer_pwa)
                before_url = matched.url if matched is not None else ""
                before_title = matched.title if matched is not None else ""
                target_is_pwa = matched.is_pwa if matched is not None else bool(prefer_pwa)

                if matched is not None:
                    target_id = matched.target_id
                    await self._restore_if_minimized(conn, matched)
                    with contextlib.suppress(CDPCommandError, TimeoutError):
                        await conn.send("Target.activateTarget", {"targetId": target_id})
                    session_id = await self._attach_flat(conn, target_id)
                    nav_res = await conn.send(
                        "Page.navigate",
                        {"url": url},
                        session_id=session_id,
                    )
                    if nav_res.get("errorText"):
                        return {
                            "ok": False,
                            "cdp_available": True,
                            "status": "navigation_error",
                            "url": before_url,
                            "requested_url": url,
                            "title": before_title,
                            "target_id": target_id,
                            "is_pwa": target_is_pwa,
                            "error": str(nav_res["errorText"]),
                        }
                elif create_if_missing:
                    mark_mutated()
                    created = await conn.send("Target.createTarget", {"url": url})
                    target_id = str(created.get("targetId") or "")
                    if not target_id:
                        return {
                            "ok": False,
                            "cdp_available": True,
                            "status": "create_target_failed",
                            "url": url,
                            "is_pwa": False,
                            "error": "Target.createTarget did not return a targetId.",
                        }
                    target_is_pwa = False
                    with contextlib.suppress(CDPCommandError, TimeoutError):
                        await conn.send("Target.activateTarget", {"targetId": target_id})
                    session_id = await self._attach_flat(conn, target_id)
                else:
                    return {
                        "ok": False,
                        "cdp_available": True,
                        "status": "target_not_found",
                        "url": url,
                        "title": "",
                        "target_id": None,
                        "is_pwa": bool(prefer_pwa),
                        "message": (
                            f"No open {'PWA' if prefer_pwa else 'Chrome'} target matched {url!r}."
                        ),
                    }

                final_url, final_title = await self._wait_for_settlement(
                    conn,
                    session_id,
                    expected_url=url,
                    before_url=before_url,
                    before_title=before_title,
                    timeout_s=settle_timeout_s,
                )
                if not final_url:
                    final_url = url
                with self._cache_lock:
                    self._last_target_id = target_id

                state_diff = {
                    "changed": (before_url != final_url) or (before_title != final_title),
                    "window_changed": False,
                    "url_changed": before_url != final_url,
                    "title_changed": before_title != final_title,
                    "before": {"url": before_url, "title": before_title},
                    "after": {"url": final_url, "title": final_title},
                }
                return {
                    "ok": True,
                    "cdp_available": True,
                    "status": "navigated",
                    "url": final_url,
                    "requested_url": url,
                    "title": final_title,
                    "target_id": target_id,
                    "is_pwa": target_is_pwa,
                    "before_url": before_url,
                    "before_title": before_title,
                    "after_url": final_url,
                    "after_title": final_title,
                    "state_diff": state_diff,
                }
            finally:
                await self._detach_quiet(conn, session_id)

        try:
            return await self._call_with_conn(_op, preferred_browser=preferred_browser)
        except CDPUnavailableError as exc:
            return _cdp_unavailable_payload(str(exc))
        except (
            CDPCommandError,
            TimeoutError,
            OSError,
            websockets.WebSocketException,
            EOFError,
        ) as exc:
            return {
                "ok": False,
                "cdp_available": True,
                "status": "cdp_error",
                "url": url,
                "error": str(exc),
            }

    async def cdp_get_active_page_state(
        self,
        target_id: str | None = None,
        *,
        prefer_pwa: bool | None = None,
        title_hint: str | None = None,
        preferred_browser: str | None = None,
    ) -> dict[str, Any]:
        """Return lightweight ``{url, title, target_id, is_pwa}`` for the active browser page."""

        async def _op(
            conn: _CDPConnection, _mark_mutated: Callable[[], None]
        ) -> dict[str, Any]:
            chosen: CDPTargetInfo | None = None
            if target_id:
                matched_only = await self._list_targets_on_conn(conn, only_target_id=target_id)
                if matched_only:
                    chosen = matched_only[0]
            if chosen is None:
                targets = await self._list_targets_on_conn(conn, title_hint=title_hint)
                chosen = self._select_target_from_list(
                    targets,
                    None,
                    prefer_pwa=prefer_pwa,
                    title_hint=title_hint,
                )
            if chosen is None:
                return {
                    "ok": False,
                    "cdp_available": True,
                    "status": "no_active_target",
                    "url": "",
                    "title": "",
                    "target_id": None,
                    "is_pwa": False,
                }
            return {
                "ok": True,
                "cdp_available": True,
                "status": "ok",
                "url": chosen.url,
                "title": chosen.title,
                "target_id": chosen.target_id,
                "is_pwa": chosen.is_pwa,
            }

        try:
            return await self._call_with_conn(_op, preferred_browser=preferred_browser)
        except (
            CDPUnavailableError,
            CDPCommandError,
            TimeoutError,
            OSError,
            websockets.WebSocketException,
            EOFError,
        ) as exc:
            return _cdp_unavailable_payload(str(exc))

    async def cdp_snapshot(
        self,
        target_id: str | None = None,
        *,
        max_elements: int = DEFAULT_MAX_SNAPSHOT_ELEMENTS,
        max_chars: int = DEFAULT_MAX_SNAPSHOT_CHARS,
        prefer_pwa: bool | None = None,
        title_hint: str | None = None,
        interactive_only: bool = False,
        preferred_browser: str | None = None,
    ) -> dict[str, Any]:
        """Capture a compact, token-budgeted AXTree snapshot with deterministic refs ``b1..bN``."""
        clamped_max = max(1, min(150, int(max_elements)))
        char_budget = max(500, int(max_chars))

        async def _op(
            conn: _CDPConnection, _mark_mutated: Callable[[], None]
        ) -> dict[str, Any]:
            session_id = ""
            try:
                chosen: CDPTargetInfo | None = None
                if target_id:
                    matched_only = await self._list_targets_on_conn(conn, only_target_id=target_id)
                    if matched_only:
                        chosen = matched_only[0]
                if chosen is None:
                    targets = await self._list_targets_on_conn(conn, title_hint=title_hint)
                    chosen = self._select_target_from_list(
                        targets,
                        None,
                        prefer_pwa=prefer_pwa,
                        title_hint=title_hint,
                    )
                if chosen is None:
                    return {
                        "ok": False,
                        "cdp_available": True,
                        "source": "cdp",
                        "status": "no_active_target",
                        "window_title": "",
                        "count": 0,
                        "element_count": 0,
                        "elements": [],
                        "message": "No open Chromium page target found.",
                    }

                session_id = await self._attach_flat(conn, chosen.target_id)
                ax_res = await conn.send("Accessibility.getFullAXTree", {}, session_id=session_id)
                raw_nodes = ax_res.get("nodes") or []

                allowed_roles = _INTERACTIVE_AX_ROLES if interactive_only else _MEANINGFUL_AX_ROLES
                elements: list[dict[str, Any]] = []
                cached_map: dict[str, CachedCDPRef] = {}
                used_chars = 0
                truncated = False

                for node in raw_nodes:
                    if not isinstance(node, dict):
                        continue
                    if node.get("ignored") is True:
                        continue
                    raw_role = _extract_ax_str(node.get("role")).strip().lower()
                    role = _ROLE_NORMALIZATION.get(raw_role, raw_role)
                    if role not in allowed_roles:
                        continue

                    props = _extract_ax_properties(node)
                    if (
                        props.get("hidden") is True
                        or props.get("ignored") is True
                        or props.get("invisible") is True
                        or props.get("hiddenroot") is True
                    ):
                        continue

                    disabled = bool(props.get("disabled") is True)
                    enabled = not disabled
                    focused = bool(props.get("focused") is True)
                    if interactive_only and not enabled:
                        continue

                    name = _truncate_text(_extract_ax_str(node.get("name")), MAX_LABEL_CHARS)
                    raw_val = _extract_ax_str(node.get("value"))
                    value = _truncate_text(raw_val, MAX_VALUE_CHARS) if raw_val.strip() else None

                    # Strip empty/noise nodes
                    if not name and not value:
                        continue

                    raw_backend = node.get("backendDOMNodeId")
                    backend_id = (
                        int(raw_backend)
                        if isinstance(raw_backend, int) and not isinstance(raw_backend, bool)
                        else None
                    )
                    if role != "heading" and backend_id is None:
                        continue

                    if len(elements) >= clamped_max:
                        truncated = True
                        break

                    ref = f"b{len(elements) + 1}"
                    compact_el: dict[str, Any] = {
                        "ref": ref,
                        "role": role,
                        "name": name,
                        "enabled": enabled,
                        "focused": focused,
                    }
                    if value is not None:
                        compact_el["value"] = value

                    encoded_len = len(json.dumps(compact_el, ensure_ascii=False))
                    if elements and (used_chars + encoded_len) > char_budget:
                        truncated = True
                        break

                    elements.append(compact_el)
                    used_chars += encoded_len
                    cached_map[ref] = CachedCDPRef(
                        ref=ref,
                        target_id=chosen.target_id,
                        backend_dom_node_id=backend_id,
                        role=role,
                        name=name,
                        value=value,
                        url=chosen.url,
                        title=chosen.title,
                        is_pwa=chosen.is_pwa,
                        enabled=enabled,
                        focused=focused,
                        browser_key=conn.browser_key or _normalize_browser_key(preferred_browser),
                    )

                snapshot_id = f"cdp_snap_{uuid.uuid4().hex[:12]}"
                with self._cache_lock:
                    self._refs_by_target[chosen.target_id] = dict(cached_map)
                    self._latest_refs = dict(cached_map)
                    self._last_target_id = chosen.target_id

                return {
                    "ok": True,
                    "cdp_available": True,
                    "source": "cdp",
                    "status": "ok",
                    "snapshot_id": snapshot_id,
                    "target_id": chosen.target_id,
                    "url": chosen.url,
                    "title": chosen.title,
                    "window_title": chosen.title,
                    "is_pwa": chosen.is_pwa,
                    "count": len(elements),
                    "element_count": len(elements),
                    "truncated": truncated,
                    "elements": elements,
                }
            finally:
                await self._detach_quiet(conn, session_id)

        try:
            return await self._call_with_conn(_op, preferred_browser=preferred_browser)
        except CDPUnavailableError as exc:
            return _cdp_unavailable_payload(str(exc))
        except (
            CDPCommandError,
            TimeoutError,
            OSError,
            websockets.WebSocketException,
            EOFError,
        ) as exc:
            return {
                "ok": False,
                "cdp_available": True,
                "source": "cdp",
                "status": "cdp_error",
                "window_title": "",
                "count": 0,
                "element_count": 0,
                "elements": [],
                "error": str(exc),
            }

    def _lookup_cached_ref(
        self,
        norm_ref: str,
        target_id: str | None = None,
    ) -> CachedCDPRef | None:
        with self._cache_lock:
            if target_id and target_id in self._refs_by_target:
                entry = self._refs_by_target[target_id].get(norm_ref)
                if entry is not None:
                    return entry
            entry = self._latest_refs.get(norm_ref)
            if entry is not None:
                return entry
            for target_refs in self._refs_by_target.values():
                if norm_ref in target_refs:
                    return target_refs[norm_ref]
        return None

    async def cdp_act(
        self,
        ref: str,
        action: str = "click",
        *,
        text: str | None = None,
        key: str | None = None,
        target_id: str | None = None,
        settle_timeout_s: float = DEFAULT_ACT_SETTLE_TIMEOUT_S,
        preferred_browser: str | None = None,
    ) -> dict[str, Any]:
        """Execute a CDP action (click/double_click/right_click/type/set_value/clear/press)."""
        norm_ref = normalize_cdp_ref(ref)
        act_norm = (action or "").strip().lower()
        valid_actions = {
            "click",
            "double_click",
            "right_click",
            "type",
            "set_value",
            "clear",
            "press",
        }
        if act_norm not in valid_actions:
            raise ValueError(
                f"Unsupported CDP action {action!r}. Must be one of {sorted(valid_actions)}."
            )
        if act_norm in {"type", "set_value"} and (text is None or not isinstance(text, str)):
            raise ValueError(f"text must be provided as a string for CDP action {act_norm!r}.")
        if act_norm == "press" and (not isinstance(key, str) or not key.strip()):
            raise ValueError("key must be a non-empty string for CDP action 'press'.")

        cached = self._lookup_cached_ref(norm_ref, target_id=target_id)
        if cached is None:
            return {
                "ok": False,
                "cdp_available": True,
                "source": "cdp",
                "status": "stale_ref",
                "ref": norm_ref,
                "action": act_norm,
                "error": (
                    f"Unknown or stale browser ref {ref!r}. Call ui_snapshot() to refresh refs."
                ),
            }
        if cached.backend_dom_node_id is None:
            return {
                "ok": False,
                "cdp_available": True,
                "source": "cdp",
                "status": "non_interactive_ref",
                "ref": norm_ref,
                "action": act_norm,
                "error": f"Element {norm_ref!r} has no backendDOMNodeId and cannot be acted on.",
            }

        resolved_target_id = target_id or cached.target_id

        async def _op(
            conn: _CDPConnection, mark_mutated: Callable[[], None]
        ) -> dict[str, Any]:
            session_id = ""
            try:
                with contextlib.suppress(CDPCommandError, TimeoutError):
                    await conn.send("Target.activateTarget", {"targetId": resolved_target_id})
                session_id = await self._attach_flat(conn, resolved_target_id)

                before_meta: dict[str, Any] = {}
                with contextlib.suppress(CDPCommandError, TimeoutError):
                    before_meta = await self._eval_page_meta(conn, session_id)
                before_url = str(before_meta.get("url") or cached.url)
                before_title = str(before_meta.get("title") or cached.title)

                backend_id = cached.backend_dom_node_id
                with contextlib.suppress(CDPCommandError, TimeoutError):
                    await conn.send(
                        "DOM.scrollIntoViewIfNeeded",
                        {"backendNodeId": backend_id},
                        session_id=session_id,
                    )

                method = ""
                if act_norm in {"click", "double_click", "right_click"}:
                    center: tuple[float, float] | None = None
                    with contextlib.suppress(CDPCommandError, TimeoutError):
                        box = await conn.send(
                            "DOM.getBoxModel",
                            {"backendNodeId": backend_id},
                            session_id=session_id,
                        )
                        model = box.get("model") if isinstance(box, dict) else None
                        if isinstance(model, dict):
                            for quad_key in ("content", "padding", "border", "margin"):
                                quad = model.get(quad_key)
                                if quad:
                                    center = _quad_center(quad)
                                    if center is not None:
                                        break

                    if center is not None:
                        cx, cy = center
                        btn = "right" if act_norm == "right_click" else "left"
                        click_count = 2 if act_norm == "double_click" else 1
                        await conn.send(
                            "Input.dispatchMouseEvent",
                            {"type": "mouseMoved", "x": cx, "y": cy, "button": "none"},
                            session_id=session_id,
                        )
                        mark_mutated()
                        for idx in range(1, click_count + 1):
                            await conn.send(
                                "Input.dispatchMouseEvent",
                                {
                                    "type": "mousePressed",
                                    "x": cx,
                                    "y": cy,
                                    "button": btn,
                                    "clickCount": idx,
                                },
                                session_id=session_id,
                            )
                            await conn.send(
                                "Input.dispatchMouseEvent",
                                {
                                    "type": "mouseReleased",
                                    "x": cx,
                                    "y": cy,
                                    "button": btn,
                                    "clickCount": idx,
                                },
                                session_id=session_id,
                            )
                        method = f"cdp_{act_norm}"
                    else:
                        # Fallback via DOM.resolveNode + JS .click()
                        resolved = await conn.send(
                            "DOM.resolveNode",
                            {"backendNodeId": backend_id},
                            session_id=session_id,
                        )
                        obj_id = ((resolved.get("object") or {}).get("objectId")) or ""
                        if not obj_id:
                            raise CDPCommandError(
                                "DOM.getBoxModel",
                                f"Could not compute click coords or resolve node for {norm_ref}",
                            )
                        mark_mutated()
                        await conn.send(
                            "Runtime.callFunctionOn",
                            {
                                "objectId": obj_id,
                                "functionDeclaration": "function() { this.click(); }",
                            },
                            session_id=session_id,
                        )
                        method = "cdp_js_click"

                elif act_norm in {"type", "set_value", "clear"}:
                    await conn.send(
                        "DOM.focus", {"backendNodeId": backend_id}, session_id=session_id
                    )
                    if act_norm in {"set_value", "clear"}:
                        with contextlib.suppress(CDPCommandError, TimeoutError):
                            resolved = await conn.send(
                                "DOM.resolveNode",
                                {"backendNodeId": backend_id},
                                session_id=session_id,
                            )
                            obj_id = ((resolved.get("object") or {}).get("objectId")) or ""
                            if obj_id:
                                mark_mutated()
                                clear_js = (
                                    "function() {"
                                    "  if (this.isContentEditable) {"
                                    "    this.textContent = '';"
                                    "  } else if ('value' in this) {"
                                    "    const p = Object.getPrototypeOf(this);"
                                    "    const d = p && Object.getOwnPropertyDescriptor("
                                    "      p, 'value');"
                                    "    if (d && typeof d.set === 'function') {"
                                    "      d.set.call(this, '');"
                                    "    } else {"
                                    "      this.value = '';"
                                    "    }"
                                    "  }"
                                    "  this.dispatchEvent(new Event('input', {bubbles:true}));"
                                    "  this.dispatchEvent(new Event('change', {bubbles:true}));"
                                    "}"
                                )
                                await conn.send(
                                    "Runtime.callFunctionOn",
                                    {
                                        "objectId": obj_id,
                                        "functionDeclaration": clear_js,
                                    },
                                    session_id=session_id,
                                )
                    if act_norm in {"type", "set_value"} and text:
                        mark_mutated()
                        await conn.send(
                            "Input.insertText",
                            {"text": text},
                            session_id=session_id,
                        )
                    method = f"cdp_{act_norm}"

                elif act_norm == "press":
                    assert key is not None
                    with contextlib.suppress(CDPCommandError, TimeoutError):
                        await conn.send(
                            "DOM.focus",
                            {"backendNodeId": backend_id},
                            session_id=session_id,
                        )
                    key_spec = _CDP_KEY_MAP.get(key.strip().lower())
                    if key_spec is None:
                        k_char = key.strip()
                        key_spec = {
                            "key": k_char,
                            "code": f"Key{k_char.upper()}" if len(k_char) == 1 else k_char,
                            "text": k_char if len(k_char) == 1 else "",
                        }
                    down_params = {"type": "keyDown", **key_spec}
                    up_params = {
                        "type": "keyUp",
                        **{k: v for k, v in key_spec.items() if k != "text"},
                    }
                    mark_mutated()
                    await conn.send("Input.dispatchKeyEvent", down_params, session_id=session_id)
                    await conn.send("Input.dispatchKeyEvent", up_params, session_id=session_id)
                    method = "cdp_press"

                after_url, after_title = await self._wait_for_settlement(
                    conn,
                    session_id,
                    before_url=before_url,
                    before_title=before_title,
                    timeout_s=settle_timeout_s,
                )
                with self._cache_lock:
                    self._last_target_id = resolved_target_id
                state_diff = {
                    "changed": (before_url != after_url) or (before_title != after_title),
                    "window_changed": False,
                    "url_changed": before_url != after_url,
                    "title_changed": before_title != after_title,
                    "before": {"url": before_url, "title": before_title},
                    "after": {"url": after_url, "title": after_title},
                }
                return {
                    "ok": True,
                    "cdp_available": True,
                    "source": "cdp",
                    "status": "completed",
                    "ref": norm_ref,
                    "action": act_norm,
                    "role": cached.role,
                    "name": cached.name,
                    "method": method,
                    "target_id": resolved_target_id,
                    "is_pwa": cached.is_pwa,
                    "before_url": before_url,
                    "after_url": after_url,
                    "before_title": before_title,
                    "after_title": after_title,
                    "url": after_url,
                    "title": after_title,
                    "state_diff": state_diff,
                }
            finally:
                await self._detach_quiet(conn, session_id)

        eff_browser = preferred_browser or cached.browser_key
        try:
            return await self._call_with_conn(_op, preferred_browser=eff_browser)
        except CDPUnavailableError as exc:
            return _cdp_unavailable_payload(str(exc))
        except (
            CDPCommandError,
            TimeoutError,
            OSError,
            websockets.WebSocketException,
            EOFError,
        ) as exc:
            return {
                "ok": False,
                "cdp_available": True,
                "source": "cdp",
                "status": "cdp_error",
                "ref": norm_ref,
                "action": act_norm,
                "target_id": resolved_target_id,
                "error": str(exc),
            }

    async def cdp_navigate_history(
        self,
        direction: str = "back",
        *,
        target_id: str | None = None,
        prefer_pwa: bool | None = None,
        title_hint: str | None = None,
        settle_timeout_s: float = DEFAULT_ACT_SETTLE_TIMEOUT_S,
        preferred_browser: str | None = None,
    ) -> dict[str, Any]:
        """Navigate back/forward via ``Page.getNavigationHistory`` + ``navigateToHistoryEntry``."""
        dir_norm = (direction or "").strip().lower()
        if dir_norm not in {"back", "forward"}:
            raise ValueError(f"direction must be 'back' or 'forward', got {direction!r}.")

        async def _op(
            conn: _CDPConnection, mark_mutated: Callable[[], None]
        ) -> dict[str, Any]:
            session_id = ""
            try:
                chosen: CDPTargetInfo | None = None
                if target_id:
                    matched_only = await self._list_targets_on_conn(
                        conn, only_target_id=target_id
                    )
                    if matched_only:
                        chosen = matched_only[0]
                if chosen is None:
                    targets = await self._list_targets_on_conn(conn, title_hint=title_hint)
                    if prefer_pwa is True:
                        pool = [t for t in targets if t.is_pwa]
                    elif prefer_pwa is False:
                        pool = [t for t in targets if not t.is_pwa]
                    else:
                        pool = list(targets)

                    if title_hint:
                        chosen = self._select_target_from_list(
                            pool,
                            None,
                            prefer_pwa=prefer_pwa,
                            title_hint=title_hint,
                        )
                    else:
                        focused = [t for t in pool if t.has_focus]
                        if focused:
                            with self._cache_lock:
                                last_tid = self._last_target_id
                            chosen = next(
                                (t for t in focused if last_tid and t.target_id == last_tid),
                                focused[0],
                            )
                        else:
                            with self._cache_lock:
                                last_tid = self._last_target_id
                            if last_tid:
                                chosen = next(
                                    (t for t in pool if t.target_id == last_tid),
                                    None,
                                )
                            if chosen is None:
                                chosen = self._select_target_from_list(
                                    pool, None, prefer_pwa=prefer_pwa
                                )
                if chosen is None:
                    return {
                        "ok": False,
                        "cdp_available": True,
                        "status": "no_active_target",
                        "direction": dir_norm,
                        "error": "No open Chromium page target found.",
                    }

                session_id = await self._attach_flat(conn, chosen.target_id)
                before_url = chosen.url
                before_title = chosen.title
                history = await conn.send("Page.getNavigationHistory", {}, session_id=session_id)
                current_index = int(history.get("currentIndex", 0))
                entries = history.get("entries") or []
                next_index = current_index - 1 if dir_norm == "back" else current_index + 1

                if next_index < 0 or next_index >= len(entries):
                    return {
                        "ok": False,
                        "cdp_available": True,
                        "status": "no_history_entry",
                        "direction": dir_norm,
                        "target_id": chosen.target_id,
                        "is_pwa": chosen.is_pwa,
                        "url": before_url,
                        "title": before_title,
                        "message": f"No {dir_norm} navigation history entry available.",
                    }

                target_entry = entries[next_index]
                entry_id = target_entry.get("id")
                expected_url = str(target_entry.get("url") or "")
                mark_mutated()
                await conn.send(
                    "Page.navigateToHistoryEntry",
                    {"entryId": entry_id},
                    session_id=session_id,
                )
                after_url, after_title = await self._wait_for_settlement(
                    conn,
                    session_id,
                    expected_url=expected_url or None,
                    before_url=before_url,
                    before_title=before_title,
                    timeout_s=settle_timeout_s,
                )
                if not after_url:
                    after_url = expected_url
                if not after_title:
                    after_title = str(target_entry.get("title") or "")

                state_diff = {
                    "changed": (before_url != after_url) or (before_title != after_title),
                    "window_changed": False,
                    "url_changed": before_url != after_url,
                    "title_changed": before_title != after_title,
                    "before": {"url": before_url, "title": before_title},
                    "after": {"url": after_url, "title": after_title},
                }
                return {
                    "ok": True,
                    "cdp_available": True,
                    "status": "navigated",
                    "direction": dir_norm,
                    "target_id": chosen.target_id,
                    "is_pwa": chosen.is_pwa,
                    "before_url": before_url,
                    "after_url": after_url,
                    "before_title": before_title,
                    "after_title": after_title,
                    "url": after_url,
                    "title": after_title,
                    "state_diff": state_diff,
                }
            finally:
                await self._detach_quiet(conn, session_id)

        try:
            return await self._call_with_conn(_op, preferred_browser=preferred_browser)
        except CDPUnavailableError as exc:
            return _cdp_unavailable_payload(str(exc))
        except (
            CDPCommandError,
            TimeoutError,
            OSError,
            websockets.WebSocketException,
            EOFError,
        ) as exc:
            return {
                "ok": False,
                "cdp_available": True,
                "status": "cdp_error",
                "direction": dir_norm,
                "error": str(exc),
            }

    # ------------------------------------------------------------------ Sync wrappers

    def navigate_sync(
        self,
        url: str,
        *,
        prefer_pwa: bool = False,
        create_if_missing: bool = True,
        settle_timeout_s: float = DEFAULT_NAV_SETTLE_TIMEOUT_S,
        preferred_browser: str | None = None,
    ) -> dict[str, Any]:
        return self._run_sync(
            self.cdp_navigate(
                url,
                prefer_pwa=prefer_pwa,
                create_if_missing=create_if_missing,
                settle_timeout_s=settle_timeout_s,
                preferred_browser=preferred_browser,
            )
        )

    def snapshot_sync(
        self,
        target_id: str | None = None,
        *,
        max_elements: int = DEFAULT_MAX_SNAPSHOT_ELEMENTS,
        max_chars: int = DEFAULT_MAX_SNAPSHOT_CHARS,
        prefer_pwa: bool | None = None,
        title_hint: str | None = None,
        interactive_only: bool = False,
        preferred_browser: str | None = None,
    ) -> dict[str, Any]:
        return self._run_sync(
            self.cdp_snapshot(
                target_id=target_id,
                max_elements=max_elements,
                max_chars=max_chars,
                prefer_pwa=prefer_pwa,
                title_hint=title_hint,
                interactive_only=interactive_only,
                preferred_browser=preferred_browser,
            )
        )

    def act_sync(
        self,
        ref: str,
        action: str = "click",
        *,
        text: str | None = None,
        key: str | None = None,
        target_id: str | None = None,
        settle_timeout_s: float = DEFAULT_ACT_SETTLE_TIMEOUT_S,
        preferred_browser: str | None = None,
    ) -> dict[str, Any]:
        return self._run_sync(
            self.cdp_act(
                ref,
                action,
                text=text,
                key=key,
                target_id=target_id,
                settle_timeout_s=settle_timeout_s,
                preferred_browser=preferred_browser,
            )
        )

    def navigate_history_sync(
        self,
        direction: str = "back",
        *,
        target_id: str | None = None,
        prefer_pwa: bool | None = None,
        title_hint: str | None = None,
        settle_timeout_s: float = DEFAULT_ACT_SETTLE_TIMEOUT_S,
        preferred_browser: str | None = None,
    ) -> dict[str, Any]:
        return self._run_sync(
            self.cdp_navigate_history(
                direction,
                target_id=target_id,
                prefer_pwa=prefer_pwa,
                title_hint=title_hint,
                settle_timeout_s=settle_timeout_s,
                preferred_browser=preferred_browser,
            )
        )

    def get_active_page_state_sync(
        self,
        target_id: str | None = None,
        *,
        prefer_pwa: bool | None = None,
        title_hint: str | None = None,
        preferred_browser: str | None = None,
    ) -> dict[str, Any]:
        return self._run_sync(
            self.cdp_get_active_page_state(
                target_id=target_id,
                prefer_pwa=prefer_pwa,
                title_hint=title_hint,
                preferred_browser=preferred_browser,
            )
        )


_DEFAULT_CDP_BRIDGE: CDPBridge | None = None
_DEFAULT_CDP_LOCK = threading.Lock()


def has_explicit_default_cdp_bridge() -> bool:
    """Return True if a process-wide default ``CDPBridge`` has been explicitly installed."""
    with _DEFAULT_CDP_LOCK:
        return _DEFAULT_CDP_BRIDGE is not None


def get_default_cdp_bridge() -> CDPBridge:
    """Return the process-wide default ``CDPBridge`` singleton."""
    global _DEFAULT_CDP_BRIDGE
    with _DEFAULT_CDP_LOCK:
        if _DEFAULT_CDP_BRIDGE is None:
            _DEFAULT_CDP_BRIDGE = CDPBridge()
        return _DEFAULT_CDP_BRIDGE


def set_default_cdp_bridge(bridge: CDPBridge | None) -> None:
    """Override or clear the process-wide default ``CDPBridge`` (closing any previous bridge)."""
    global _DEFAULT_CDP_BRIDGE
    with _DEFAULT_CDP_LOCK:
        old = _DEFAULT_CDP_BRIDGE
        _DEFAULT_CDP_BRIDGE = bridge
    if old is not None and old is not bridge:
        close_fn = getattr(old, "close", None)
        if callable(close_fn):
            with contextlib.suppress(Exception):
                close_fn()


def close_default_cdp_bridge() -> None:
    """Close any open persistent connection on the default ``CDPBridge`` without clearing it."""
    with _DEFAULT_CDP_LOCK:
        bridge = _DEFAULT_CDP_BRIDGE
    if bridge is not None:
        close_fn = getattr(bridge, "close", None)
        if callable(close_fn):
            with contextlib.suppress(Exception):
                close_fn()


atexit.register(close_default_cdp_bridge)


async def cdp_navigate(
    url: str,
    *,
    prefer_pwa: bool = False,
    create_if_missing: bool = True,
    bridge: CDPBridge | None = None,
) -> dict[str, Any]:
    return await (bridge or get_default_cdp_bridge()).cdp_navigate(
        url,
        prefer_pwa=prefer_pwa,
        create_if_missing=create_if_missing,
    )


async def cdp_snapshot(
    target_id: str | None = None,
    *,
    max_elements: int = DEFAULT_MAX_SNAPSHOT_ELEMENTS,
    interactive_only: bool = False,
    bridge: CDPBridge | None = None,
) -> dict[str, Any]:
    return await (bridge or get_default_cdp_bridge()).cdp_snapshot(
        target_id=target_id,
        max_elements=max_elements,
        interactive_only=interactive_only,
    )


async def cdp_act(
    ref: str,
    action: str = "click",
    *,
    text: str | None = None,
    key: str | None = None,
    target_id: str | None = None,
    bridge: CDPBridge | None = None,
) -> dict[str, Any]:
    return await (bridge or get_default_cdp_bridge()).cdp_act(
        ref,
        action,
        text=text,
        key=key,
        target_id=target_id,
    )


async def cdp_navigate_history(
    direction: str = "back",
    *,
    target_id: str | None = None,
    bridge: CDPBridge | None = None,
) -> dict[str, Any]:
    return await (bridge or get_default_cdp_bridge()).cdp_navigate_history(
        direction,
        target_id=target_id,
    )


async def cdp_get_active_page_state(
    target_id: str | None = None,
    *,
    bridge: CDPBridge | None = None,
) -> dict[str, Any]:
    return await (bridge or get_default_cdp_bridge()).cdp_get_active_page_state(
        target_id=target_id,
    )

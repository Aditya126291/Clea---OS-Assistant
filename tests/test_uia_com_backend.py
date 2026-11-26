"""Deterministic unit tests for the in-process native Win32 UIA COM engine,
OS tooling wiring (ui_snapshot/ui_act/inspect_ui_elements/click_ui_element/probe_uia_text),
accessibility coverage classification, opt-in post-action re-snapshot, and daemon
async installed-apps cache warmup.

All tests are strictly mocked: zero live desktop windows, zero real COM desktop hooks.
"""

from __future__ import annotations

import asyncio
import ctypes
import json
import subprocess
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any
from unittest.mock import MagicMock

import pytest

import clea.core.daemon as daemon_mod
import clea.tools.vision as vision_mod
import clea.tools.windows_os as windows_os
from clea.core.agent import AgentLoop
from clea.core.daemon import CleaDaemon
from clea.core.protocol import OsUiActParams, OsUiSnapshotResult
from clea.memory.store import MemoryStore
from clea.providers.base import LLMProvider, Message, StreamChunk
from clea.tools.cdp_bridge import CDPBridge
from clea.tools.registry import ToolRegistry
from clea.tools.uia_com import (
    COINIT_MULTITHREADED,
    E_NOINTERFACE,
    EXPAND_COLLAPSE_STATE_COLLAPSED,
    EXPAND_COLLAPSE_STATE_EXPANDED,
    TOGGLE_STATE_OFF,
    TOGGLE_STATE_ON,
    TREE_SCOPE_ELEMENT,
    UIA_AUTOMATION_ID_PROPERTY_ID,
    UIA_BOUNDING_RECTANGLE_PROPERTY_ID,
    UIA_BUTTON_CONTROL_TYPE_ID,
    UIA_CHECKBOX_CONTROL_TYPE_ID,
    UIA_COMBOBOX_CONTROL_TYPE_ID,
    UIA_CONTROL_TYPE_PROPERTY_ID,
    UIA_E_ELEMENTNOTAVAILABLE,
    UIA_EDIT_CONTROL_TYPE_ID,
    UIA_EXPAND_COLLAPSE_PATTERN_ID,
    UIA_GROUP_CONTROL_TYPE_ID,
    UIA_INVOKE_PATTERN_ID,
    UIA_LIST_ITEM_CONTROL_TYPE_ID,
    UIA_MENU_ITEM_CONTROL_TYPE_ID,
    UIA_NAME_PROPERTY_ID,
    UIA_PANE_CONTROL_TYPE_ID,
    UIA_RANGE_VALUE_PATTERN_ID,
    UIA_RANGE_VALUE_VALUE_PROPERTY_ID,
    UIA_SELECTION_ITEM_PATTERN_ID,
    UIA_SLIDER_CONTROL_TYPE_ID,
    UIA_TAB_ITEM_CONTROL_TYPE_ID,
    UIA_TEXT_CONTROL_TYPE_ID,
    UIA_TOGGLE_PATTERN_ID,
    UIA_VALUE_PATTERN_ID,
    UIA_VALUE_VALUE_PROPERTY_ID,
    UIA_WINDOW_CONTROL_TYPE_ID,
    NativeUiaComEngine,
    classify_accessibility_coverage,
    set_default_uia_com_engine,
)

# --------------------------------------------------------------------------- Mock COM Doubles


class _FakeRect:
    def __init__(self, left: int, top: int, right: int, bottom: int) -> None:
        self.left = left
        self.top = top
        self.right = right
        self.bottom = bottom


class _FakeCOMError(OSError):
    """Deterministic stand-in for _ctypes.COMError carrying an HRESULT."""

    def __init__(self, hresult: int, text: str = "COM error") -> None:
        super().__init__(hresult, text)
        self.hresult = hresult


class _FakeCacheRequest:
    def __init__(self) -> None:
        self.TreeScope: int | None = None
        self.AutomationElementMode: int | None = None
        self.properties: list[int] = []
        self.patterns: list[int] = []
        self.released = False

    def AddProperty(self, prop_id: int) -> None:
        self.properties.append(prop_id)

    def AddPattern(self, pattern_id: int) -> None:
        self.patterns.append(pattern_id)

    def Release(self) -> int:
        self.released = True
        return 0


class _FakeInvokePattern:
    def __init__(self, on_invoke: Any = None, *, fail_exc: BaseException | None = None) -> None:
        self.invoked = 0
        self.on_invoke = on_invoke
        self.fail_exc = fail_exc
        self.released = False

    def Invoke(self) -> None:
        if self.fail_exc is not None:
            raise self.fail_exc
        self.invoked += 1
        if callable(self.on_invoke):
            self.on_invoke()

    def Release(self) -> int:
        self.released = True
        return 0


class _FakeValuePattern:
    def __init__(
        self,
        initial: str = "",
        *,
        read_only: bool = False,
        fail_exc: BaseException | None = None,
    ) -> None:
        self.value = initial
        self.CurrentIsReadOnly = read_only
        self.set_calls: list[str] = []
        self.fail_exc = fail_exc
        self.released = False

    @property
    def CurrentValue(self) -> str:
        return self.value

    @property
    def CachedValue(self) -> str:
        return self.value

    def SetValue(self, val: str) -> None:
        if self.fail_exc is not None:
            raise self.fail_exc
        self.set_calls.append(val)
        self.value = val

    def Release(self) -> int:
        self.released = True
        return 0


class _FakeSelectionItemPattern:
    def __init__(self, selected: bool = False) -> None:
        self.selected = selected
        self.select_calls = 0
        self.released = False

    @property
    def CurrentIsSelected(self) -> bool:
        return self.selected

    def Select(self) -> None:
        self.select_calls += 1
        self.selected = True

    def Release(self) -> int:
        self.released = True
        return 0


class _FakeTogglePattern:
    def __init__(self, state: int = TOGGLE_STATE_OFF) -> None:
        self.state = state
        self.toggle_calls = 0
        self.released = False

    @property
    def CurrentToggleState(self) -> int:
        return self.state

    def Toggle(self) -> None:
        self.toggle_calls += 1
        self.state = TOGGLE_STATE_ON if self.state == TOGGLE_STATE_OFF else TOGGLE_STATE_OFF

    def Release(self) -> int:
        self.released = True
        return 0


class _FakeExpandCollapsePattern:
    def __init__(self, state: int = EXPAND_COLLAPSE_STATE_COLLAPSED) -> None:
        self.state = state
        self.expand_calls = 0
        self.collapse_calls = 0
        self.released = False

    @property
    def CurrentExpandCollapseState(self) -> int:
        return self.state

    def Expand(self) -> None:
        self.expand_calls += 1
        self.state = EXPAND_COLLAPSE_STATE_EXPANDED

    def Collapse(self) -> None:
        self.collapse_calls += 1
        self.state = EXPAND_COLLAPSE_STATE_COLLAPSED

    def Release(self) -> int:
        self.released = True
        return 0


class _FakeUiaElement:
    def __init__(
        self,
        *,
        control_type: int,
        name: str = "",
        automation_id: str = "",
        rect: _FakeRect | None = None,
        is_offscreen: bool = False,
        is_enabled: bool = True,
        has_focus: bool = False,
        hwnd: int = 0,
        children: list[_FakeUiaElement] | None = None,
        patterns: dict[int, Any] | None = None,
        stale: bool = False,
        unsupported_pattern_raises: bool = False,
    ) -> None:
        self.control_type = control_type
        self.name = name
        self.automation_id = automation_id
        self.rect = rect
        self.is_offscreen = is_offscreen
        self.is_enabled = is_enabled
        self.has_focus = has_focus
        self.hwnd = hwnd
        self.children: list[_FakeUiaElement] = list(children or [])
        self.parent: _FakeUiaElement | None = None
        for ch in self.children:
            ch.parent = self
        self.patterns: dict[int, Any] = dict(patterns or {})
        self.stale = stale
        self.unsupported_pattern_raises = unsupported_pattern_raises
        self.released = False
        self.release_count = 0
        self.release_thread_id: int | None = None
        self.access_thread_ids: list[int] = []

    def _check_alive(self) -> None:
        self.access_thread_ids.append(threading.get_ident())
        if self.stale:
            raise _FakeCOMError(UIA_E_ELEMENTNOTAVAILABLE, "Element no longer available")

    @property
    def CachedControlType(self) -> int:
        self._check_alive()
        return self.control_type

    @property
    def CurrentControlType(self) -> int:
        self._check_alive()
        return self.control_type

    @property
    def CachedName(self) -> str:
        self._check_alive()
        return self.name

    @property
    def CurrentName(self) -> str:
        self._check_alive()
        return self.name

    @property
    def CachedAutomationId(self) -> str:
        self._check_alive()
        return self.automation_id

    @property
    def CurrentAutomationId(self) -> str:
        self._check_alive()
        return self.automation_id

    @property
    def CachedIsOffscreen(self) -> bool:
        self._check_alive()
        return self.is_offscreen

    @property
    def CachedIsEnabled(self) -> bool:
        self._check_alive()
        return self.is_enabled

    @property
    def CurrentIsEnabled(self) -> bool:
        self._check_alive()
        return self.is_enabled

    @property
    def CachedHasKeyboardFocus(self) -> bool:
        self._check_alive()
        return self.has_focus

    @property
    def CurrentHasKeyboardFocus(self) -> bool:
        self._check_alive()
        return self.has_focus

    @property
    def CachedNativeWindowHandle(self) -> int:
        return self.hwnd

    @property
    def CachedBoundingRectangle(self) -> _FakeRect | None:
        self._check_alive()
        return self.rect

    @property
    def CurrentBoundingRectangle(self) -> _FakeRect | None:
        self._check_alive()
        return self.rect

    def GetCachedPropertyValue(self, prop_id: int) -> Any:
        self._check_alive()
        if prop_id == UIA_VALUE_VALUE_PROPERTY_ID and UIA_VALUE_PATTERN_ID in self.patterns:
            return self.patterns[UIA_VALUE_PATTERN_ID].value
        return None

    def GetCachedPattern(self, pattern_id: int) -> Any:
        return self.GetCurrentPattern(pattern_id)

    def GetCurrentPattern(self, pattern_id: int) -> Any:
        self._check_alive()
        if pattern_id in self.patterns:
            return self.patterns[pattern_id]
        if self.unsupported_pattern_raises:
            raise _FakeCOMError(E_NOINTERFACE, "Pattern not supported")
        return None

    def Release(self) -> int:
        self.released = True
        self.release_count += 1
        self.release_thread_id = threading.get_ident()
        return 0


class _FakeTreeWalker:
    def __init__(self) -> None:
        self.first_child_calls = 0
        self.walk_thread_ids: list[int] = []

    def GetParentElement(self, node: _FakeUiaElement) -> _FakeUiaElement | None:
        self.walk_thread_ids.append(threading.get_ident())
        return node.parent

    def GetFirstChildElementBuildCache(
        self, node: _FakeUiaElement, _cache_req: Any
    ) -> _FakeUiaElement | None:
        self.walk_thread_ids.append(threading.get_ident())
        self.first_child_calls += 1
        return node.children[0] if node.children else None

    def GetNextSiblingElementBuildCache(
        self, node: _FakeUiaElement, _cache_req: Any
    ) -> _FakeUiaElement | None:
        self.walk_thread_ids.append(threading.get_ident())
        parent = node.parent
        if parent is None:
            return None
        try:
            idx = parent.children.index(node)
        except ValueError:
            return None
        return parent.children[idx + 1] if idx + 1 < len(parent.children) else None


class _FakeUIAutomation:
    def __init__(
        self,
        *,
        windows_by_hwnd: dict[int, _FakeUiaElement],
        focused: _FakeUiaElement | None = None,
        supports_timeouts: bool = True,
        automation_class: str = "CUIAutomation8",
    ) -> None:
        self.windows_by_hwnd = dict(windows_by_hwnd)
        self.focused = focused
        self.walker = _FakeTreeWalker()
        self.cache_requests: list[_FakeCacheRequest] = []
        self.element_from_handle_calls: list[int] = []
        self.automation_class = automation_class
        self.root = _FakeUiaElement(
            control_type=UIA_PANE_CONTROL_TYPE_ID,
            name="Desktop",
            children=list(windows_by_hwnd.values()),
        )
        if supports_timeouts:
            self.ConnectionTimeout = 0
            self.TransactionTimeout = 0

    @property
    def ControlViewWalker(self) -> _FakeTreeWalker:
        return self.walker

    def CreateCacheRequest(self) -> _FakeCacheRequest:
        req = _FakeCacheRequest()
        self.cache_requests.append(req)
        return req

    def GetRootElement(self) -> _FakeUiaElement:
        return self.root

    def GetFocusedElement(self) -> _FakeUiaElement | None:
        return self.focused

    def CompareElements(self, a: Any, b: Any) -> bool:
        return a is b

    def ElementFromHandleBuildCache(self, hwnd: int, _cache_req: Any) -> _FakeUiaElement | None:
        self.element_from_handle_calls.append(hwnd)
        return self.windows_by_hwnd.get(hwnd)

    def ElementFromHandle(self, hwnd: int) -> _FakeUiaElement | None:
        self.element_from_handle_calls.append(hwnd)
        return self.windows_by_hwnd.get(hwnd)

    def CheckNotSupported(self, val: Any) -> bool:
        return val is None


class _FakeInputUser32:
    def __init__(self, *, hwnd: int = 1001, title: str = "Mock App") -> None:
        self.hwnd = hwnd
        self.foreground_title = title
        self.cursor_moves: list[tuple[int, int]] = []
        self.sent_inputs: list[Any] = []

    def GetForegroundWindow(self) -> int:
        return self.hwnd

    def SetForegroundWindow(self, hwnd: int) -> bool:
        self.hwnd = hwnd
        return True

    def IsIconic(self, _hwnd: int) -> bool:
        return False

    def ShowWindow(self, _hwnd: int, _cmd: int) -> bool:
        return True

    def SetCursorPos(self, x: int, y: int) -> int:
        self.cursor_moves.append((int(x), int(y)))
        return 1

    def SendInput(self, count: int, arr: Any, _cb: int) -> int:
        for i in range(int(count)):
            self.sent_inputs.append(arr[i])
        return int(count)


@pytest.fixture(autouse=True)
def _reset_uia_state() -> Any:
    windows_os.clear_ui_ref_store()
    set_default_uia_com_engine(None)
    yield
    windows_os.clear_ui_ref_store()
    set_default_uia_com_engine(None)


# --------------------------------------------------------------------------- 1. MTA & Hang Guard


def test_mta_thread_init_cuiautomation8_timeouts_and_cuiautomation_fallback() -> None:
    caller_tid = threading.get_ident()
    co_init_calls: list[tuple[int, int]] = []
    co_uninit_calls: list[int] = []
    create_calls: list[tuple[Any, Any, int]] = []

    win = _FakeUiaElement(
        control_type=UIA_WINDOW_CONTROL_TYPE_ID,
        name="Settings",
        hwnd=5001,
        children=[
            _FakeUiaElement(
                control_type=UIA_BUTTON_CONTROL_TYPE_ID,
                name="Apply",
                rect=_FakeRect(100, 200, 200, 240),
            )
        ],
    )
    auto8 = _FakeUIAutomation(windows_by_hwnd={5001: win}, supports_timeouts=True)
    fake_uia_mod = SimpleNamespace(
        CUIAutomation8="CLSID_CUIAutomation8",
        CUIAutomation="CLSID_CUIAutomation",
        IUIAutomation2="IID_IUIAutomation2",
        IUIAutomation="IID_IUIAutomation",
    )

    def _create_obj_v8(clsid: Any, interface: Any = None) -> Any:
        create_calls.append((clsid, interface, threading.get_ident()))
        assert clsid == "CLSID_CUIAutomation8"
        assert interface == "IID_IUIAutomation2"
        return auto8

    engine8 = NativeUiaComEngine(
        connection_timeout_ms=1750,
        transaction_timeout_ms=1900,
        get_module_fn=lambda _dll: fake_uia_mod,
        create_object_fn=_create_obj_v8,
        co_initialize_fn=lambda flags: co_init_calls.append((flags, threading.get_ident())),
        co_uninitialize_fn=lambda: co_uninit_calls.append(threading.get_ident()),
    )
    try:
        assert engine8.is_available() is True
        worker_tid = engine8.worker_thread_id
        assert worker_tid is not None and worker_tid != caller_tid
        assert co_init_calls == [(COINIT_MULTITHREADED, worker_tid)]
        assert create_calls == [("CLSID_CUIAutomation8", "IID_IUIAutomation2", worker_tid)]
        assert engine8.automation_class == "CUIAutomation8"
        assert engine8.timeouts_configured is True
        assert auto8.ConnectionTimeout == 1750
        assert auto8.TransactionTimeout == 1900

        # CacheRequest must be TreeScope_Element with key properties & patterns
        assert len(auto8.cache_requests) == 1
        req = auto8.cache_requests[0]
        assert req.TreeScope == TREE_SCOPE_ELEMENT
        assert UIA_NAME_PROPERTY_ID in req.properties
        assert UIA_AUTOMATION_ID_PROPERTY_ID in req.properties
        assert UIA_CONTROL_TYPE_PROPERTY_ID in req.properties
        assert UIA_BOUNDING_RECTANGLE_PROPERTY_ID in req.properties
        assert UIA_VALUE_PATTERN_ID in req.patterns
        assert UIA_RANGE_VALUE_PATTERN_ID in req.patterns

        snap = engine8.snapshot(hwnd=5001)
        assert snap["count"] == 1
        assert all(tid == worker_tid for tid in auto8.walker.walk_thread_ids)
    finally:
        engine8.close()

    assert co_uninit_calls == [worker_tid]

    # Fallback: CUIAutomation8 fails -> cleanly falls back to CUIAutomation
    fallback_calls: list[Any] = []
    auto_legacy = _FakeUIAutomation(
        windows_by_hwnd={5001: win},
        supports_timeouts=False,
        automation_class="CUIAutomation",
    )

    def _create_obj_fallback(clsid: Any, interface: Any = None) -> Any:
        fallback_calls.append((clsid, interface))
        if clsid == "CLSID_CUIAutomation8":
            raise OSError("Class not registered")
        return auto_legacy

    engine_fb = NativeUiaComEngine(
        get_module_fn=lambda _dll: fake_uia_mod,
        create_object_fn=_create_obj_fallback,
        co_initialize_fn=lambda _f: None,
        co_uninitialize_fn=lambda: None,
    )
    try:
        assert engine_fb.is_available() is True
        assert engine_fb.automation_class == "CUIAutomation"
        assert engine_fb.timeouts_configured is False
        assert fallback_calls == [
            ("CLSID_CUIAutomation8", "IID_IUIAutomation2"),
            ("CLSID_CUIAutomation", "IID_IUIAutomation"),
        ]
        snap_fb = engine_fb.snapshot(hwnd=5001)
        assert snap_fb["count"] == 1
        assert snap_fb["elements"][0]["name"] == "Apply"
    finally:
        engine_fb.close()


# --------------------------------------------------------------------------- 2. Adaptive BFS & DPI


def test_adaptive_container_skipping_depth_node_caps_and_physical_pixel_bounds() -> None:
    # Build 10 nested unlabeled Pane/Group wrappers enclosing 3 real controls at 150% DPI coords
    buttons = [
        _FakeUiaElement(
            control_type=UIA_BUTTON_CONTROL_TYPE_ID,
            name="Save",
            automation_id="btnSave",
            rect=_FakeRect(1875, 1350, 2125, 1410),  # Exact physical pixels (no DPI drift!)
        ),
        _FakeUiaElement(
            control_type=UIA_BUTTON_CONTROL_TYPE_ID,
            name="Cancel",
            automation_id="btnCancel",
            rect=_FakeRect(2140, 1350, 2340, 1410),
        ),
        _FakeUiaElement(
            control_type=UIA_TEXT_CONTROL_TYPE_ID,
            name="Status Ready",
            rect=_FakeRect(100, 100, 400, 130),
        ),
    ]
    wrappers: list[_FakeUiaElement] = []
    cur_children = buttons
    for idx in range(10):
        wrapper = _FakeUiaElement(
            control_type=UIA_PANE_CONTROL_TYPE_ID if idx % 2 == 0 else UIA_GROUP_CONTROL_TYPE_ID,
            name="",
            automation_id="",
            children=cur_children,
        )
        wrappers.append(wrapper)
        cur_children = [wrapper]

    win = _FakeUiaElement(
        control_type=UIA_WINDOW_CONTROL_TYPE_ID,
        name="HighDPI Studio",
        hwnd=7001,
        children=cur_children,
    )
    auto = _FakeUIAutomation(windows_by_hwnd={7001: win})

    # max_nodes=3 would fail if the 10 unlabeled Pane/Group wrappers consumed quota!
    engine = NativeUiaComEngine(
        max_depth=16,
        max_nodes=3,
        com_factory=lambda: (auto, None),
        co_initialize_fn=lambda _f: None,
        co_uninitialize_fn=lambda: None,
    )
    try:
        snap = engine.snapshot(hwnd=7001)
        assert snap["count"] == 3
        assert [el["name"] for el in snap["elements"]] == ["Save", "Cancel", "Status Ready"]
        # Unmodified physical screen pixel coordinates preserved
        e1 = snap["elements_by_ref"]["e1"]
        assert e1["bounds"] == {"x": 1875, "y": 1350, "width": 250, "height": 60}
        assert e1["center"] == {"cx": 2000, "cy": 1380}
        # Intermediate unlabeled wrappers were released immediately after walking
        assert all(w.released for w in wrappers)

        # Depth cap check: max_depth=5 cannot reach buttons nested 11 levels deep
        shallow_snap = engine.snapshot(hwnd=7001, max_depth=5)
        assert shallow_snap["count"] == 0

        # Node cap check: max_nodes=2 stops after 2 non-wrapper nodes
        capped_snap = engine.snapshot(hwnd=7001, max_depth=16, max_nodes=2)
        assert capped_snap["count"] == 2
        assert [el["name"] for el in capped_snap["elements"]] == ["Save", "Cancel"]
    finally:
        engine.close()


# ----------------------------------------------------------------------- 3. Patterns, LRU, Stale


def test_cached_pattern_dispatch_set_value_only_lru_eviction_and_stale_recovery() -> None:
    inv_pat = _FakeInvokePattern()
    val_pat = _FakeValuePattern(initial="initial")
    sel_pat = _FakeSelectionItemPattern(selected=False)
    tog_pat = _FakeTogglePattern(state=TOGGLE_STATE_OFF)
    exp_pat = _FakeExpandCollapsePattern(state=EXPAND_COLLAPSE_STATE_COLLAPSED)

    el_invoke = _FakeUiaElement(
        control_type=UIA_BUTTON_CONTROL_TYPE_ID,
        name="Run",
        rect=_FakeRect(10, 10, 90, 40),
        patterns={UIA_INVOKE_PATTERN_ID: inv_pat},
    )
    el_value = _FakeUiaElement(
        control_type=UIA_EDIT_CONTROL_TYPE_ID,
        name="Query",
        rect=_FakeRect(10, 50, 310, 80),
        patterns={UIA_VALUE_PATTERN_ID: val_pat},
    )
    el_select = _FakeUiaElement(
        control_type=UIA_TAB_ITEM_CONTROL_TYPE_ID,
        name="Console",
        rect=_FakeRect(10, 90, 110, 120),
        patterns={UIA_SELECTION_ITEM_PATTERN_ID: sel_pat},
    )
    el_toggle = _FakeUiaElement(
        control_type=UIA_CHECKBOX_CONTROL_TYPE_ID,
        name="Dark Mode",
        rect=_FakeRect(10, 130, 150, 160),
        patterns={UIA_TOGGLE_PATTERN_ID: tog_pat},
    )
    el_expand = _FakeUiaElement(
        control_type=UIA_COMBOBOX_CONTROL_TYPE_ID,
        name="Theme",
        rect=_FakeRect(10, 170, 210, 200),
        patterns={UIA_EXPAND_COLLAPSE_PATTERN_ID: exp_pat},
    )
    el_no_pat = _FakeUiaElement(
        control_type=UIA_BUTTON_CONTROL_TYPE_ID,
        name="CanvasBtn",
        rect=_FakeRect(10, 210, 110, 250),
        unsupported_pattern_raises=True,
    )

    win = _FakeUiaElement(
        control_type=UIA_WINDOW_CONTROL_TYPE_ID,
        name="Control Panel",
        hwnd=8001,
        children=[el_invoke, el_value, el_select, el_toggle, el_expand, el_no_pat],
    )
    auto = _FakeUIAutomation(windows_by_hwnd={8001: win})

    engine = NativeUiaComEngine(
        max_cached_elements=20,
        com_factory=lambda: (auto, None),
        co_initialize_fn=lambda _f: None,
        co_uninitialize_fn=lambda: None,
    )
    u32 = _FakeInputUser32(hwnd=8001, title="Control Panel")
    try:
        snap = windows_os.ui_snapshot(uia_backend=engine, user32=u32, _target_hwnd=8001)
        assert snap["source"] == "uia_com"
        assert snap["count"] == 6
        walks_after_snap = auto.walker.first_child_calls

        # InvokePattern
        r1 = windows_os.ui_act("e1", "click", uia_backend=engine, user32=u32)
        assert r1["ok"] is True and r1["method"] == "invoke"
        assert inv_pat.invoked == 1

        # ValuePattern on set_value (replace-only)
        r2 = windows_os.ui_act("e2", "set_value", value="hello", uia_backend=engine, user32=u32)
        assert r2["ok"] is True and r2["method"] == "value_pattern"
        assert val_pat.set_calls == ["hello"]
        assert r2["diff"]["changed_values"][0]["old_value"] == "initial"
        assert r2["diff"]["changed_values"][0]["new_value"] == "hello"

        # Worktree 1 rule: 'type' MUST NEVER invoke ValuePattern (append-only via Win32)
        r2_type = windows_os.ui_act(
            "e2", "type", value=" world", interval_ms=0, uia_backend=engine, user32=u32
        )
        assert r2_type["ok"] is True and r2_type["method"] == "win32_type"
        assert val_pat.set_calls == ["hello"]  # Unchanged!
        assert len(u32.sent_inputs) > 0

        # SelectionItemPattern
        r3 = windows_os.ui_act("e3", "click", uia_backend=engine, user32=u32)
        assert r3["ok"] is True and r3["method"] == "select"
        assert sel_pat.select_calls == 1
        assert r3["target_state"]["is_selected"] is True

        # TogglePattern
        r4 = windows_os.ui_act("e4", "click", uia_backend=engine, user32=u32)
        assert r4["ok"] is True and r4["method"] == "toggle"
        assert tog_pat.toggle_calls == 1
        assert r4["target_state"]["toggle_state"] == "on"

        # ExpandCollapsePattern: Expand then Collapse
        r5a = windows_os.ui_act("e5", "click", uia_backend=engine, user32=u32)
        assert r5a["ok"] is True and r5a["method"] == "expand_collapse"
        assert exp_pat.expand_calls == 1
        assert r5a["target_state"]["expand_collapse_state"] == "expanded"

        r5b = windows_os.ui_act("e5", "click", uia_backend=engine, user32=u32)
        assert r5b["ok"] is True and r5b["method"] == "expand_collapse"
        assert exp_pat.collapse_calls == 1
        assert r5b["target_state"]["expand_collapse_state"] == "collapsed"

        # E_NOINTERFACE (unsupported pattern) must NOT misclassify as stale -> Win32 click fallback
        u32.cursor_moves.clear()
        r6 = windows_os.ui_act("e6", "click", uia_backend=engine, user32=u32)
        assert r6["ok"] is True and r6["method"] == "win32_click"
        assert u32.cursor_moves == [(60, 230)]

        # Zero extra tree walks happened across all cached pattern actions!
        assert auto.walker.first_child_calls == walks_after_snap

        # Stale handle recovery: invalidate e1 -> returns structured stale_ref error & releases it
        u32.cursor_moves.clear()
        el_invoke.stale = True
        r_stale = windows_os.ui_act("e1", "click", uia_backend=engine, user32=u32)
        assert r_stale["ok"] is False
        assert r_stale["status"] == "stale_ref"
        assert r_stale["stale_ref"] is True
        assert r_stale["reason"] == "stale_element"
        assert "0x80040201" in r_stale["error"]
        assert el_invoke.released is True
        assert u32.cursor_moves == []  # Never clicked stale coordinates!
    finally:
        engine.close()

    # LRU Eviction test: max_cached_elements=3 with 5 elements evicts & releases oldest 2
    lru_els = [
        _FakeUiaElement(
            control_type=UIA_BUTTON_CONTROL_TYPE_ID,
            name=f"Btn {i}",
            rect=_FakeRect(10, 10 + i * 30, 100, 35 + i * 30),
        )
        for i in range(5)
    ]
    lru_win = _FakeUiaElement(
        control_type=UIA_WINDOW_CONTROL_TYPE_ID,
        name="LRU Window",
        hwnd=8002,
        children=lru_els,
    )
    lru_auto = _FakeUIAutomation(windows_by_hwnd={8002: lru_win})
    lru_engine = NativeUiaComEngine(
        max_cached_elements=3,
        com_factory=lambda: (lru_auto, None),
        co_initialize_fn=lambda _f: None,
        co_uninitialize_fn=lambda: None,
    )
    try:
        lru_snap = lru_engine.snapshot(hwnd=8002)
        sid = lru_snap["snapshot_id"]
        assert lru_engine.cached_element_count == 3
        assert lru_engine.evicted_count == 2
        assert lru_els[0].released is True
        assert lru_els[1].released is True
        assert lru_els[0].release_thread_id == lru_engine.worker_thread_id
        assert lru_els[2].released is False
        assert lru_els[3].released is False
        assert lru_els[4].released is False
        assert lru_engine.has_cached_element(sid, "e1") is False
        assert lru_engine.has_cached_element(sid, "e3") is True
    finally:
        lru_engine.close()

    # Closing the engine explicitly releases remaining cached COM handles on the MTA thread
    assert all(el.released for el in lru_els)


# --------------------------------------------------------------------------- 4. Coverage Classes


def test_accessibility_coverage_classification_chrome_only_sparse_and_rich() -> None:
    chrome_frame = [
        {"role": "menu_item", "control_type": "MenuItem", "name": "System", "automation_id": ""},
        {
            "role": "button",
            "control_type": "Button",
            "name": "Minimize",
            "automation_id": "Minimize",
        },
        {
            "role": "button",
            "control_type": "Button",
            "name": "Maximize",
            "automation_id": "Maximize",
        },
        {"role": "button", "control_type": "Button", "name": "Restore", "automation_id": "Restore"},
        {"role": "button", "control_type": "Button", "name": "Close", "automation_id": "Close"},
    ]

    # Frame-only (0 content) and <=2 content -> chrome_only + guidance
    cov0, cnt0, hint0 = classify_accessibility_coverage(chrome_frame)
    assert cov0 == "chrome_only" and cnt0 == 0 and hint0 is not None
    assert "inspect_screen" in hint0

    two_content = chrome_frame + [
        {"role": "button", "control_type": "Button", "name": "Play"},
        {"role": "text", "control_type": "Text", "name": "Track 1"},
    ]
    cov2, cnt2, hint2 = classify_accessibility_coverage(two_content)
    assert cov2 == "chrome_only" and cnt2 == 2 and hint2 is not None

    # 3..6 content controls -> sparse (no chrome_only guidance)
    sparse_els = chrome_frame + [
        {"role": "button", "control_type": "Button", "name": f"Action {i}"} for i in range(4)
    ]
    cov_s, cnt_s, hint_s = classify_accessibility_coverage(sparse_els)
    assert cov_s == "sparse" and cnt_s == 4 and hint_s is None

    # >6 content controls -> rich
    rich_els = chrome_frame + [
        {"role": "button", "control_type": "Button", "name": f"Item {i}"} for i in range(7)
    ]
    cov_r, cnt_r, hint_r = classify_accessibility_coverage(rich_els)
    assert cov_r == "rich" and cnt_r == 7 and hint_r is None

    # End-to-end via ui_snapshot on a chrome-only frame tree
    frame_win = _FakeUiaElement(
        control_type=UIA_WINDOW_CONTROL_TYPE_ID,
        name="DirectX Game",
        hwnd=9001,
        children=[
            _FakeUiaElement(
                control_type=UIA_MENU_ITEM_CONTROL_TYPE_ID,
                name="System",
                rect=_FakeRect(0, 0, 24, 24),
            ),
            _FakeUiaElement(
                control_type=UIA_BUTTON_CONTROL_TYPE_ID,
                name="Minimize",
                rect=_FakeRect(1800, 0, 1840, 30),
            ),
            _FakeUiaElement(
                control_type=UIA_BUTTON_CONTROL_TYPE_ID,
                name="Maximize",
                rect=_FakeRect(1840, 0, 1880, 30),
            ),
            _FakeUiaElement(
                control_type=UIA_BUTTON_CONTROL_TYPE_ID,
                name="Close",
                rect=_FakeRect(1880, 0, 1920, 30),
            ),
        ],
    )
    auto = _FakeUIAutomation(windows_by_hwnd={9001: frame_win})
    engine = NativeUiaComEngine(
        com_factory=lambda: (auto, None),
        co_initialize_fn=lambda _f: None,
        co_uninitialize_fn=lambda: None,
    )
    try:
        snap = windows_os.ui_snapshot(uia_backend=engine, _target_hwnd=9001)
        assert snap["accessibility_coverage"] == "chrome_only"
        assert snap["content_element_count"] == 0
        assert "inspect_screen" in snap["hint"]
        # Protocol model accepts accessibility_coverage cleanly
        parsed = OsUiSnapshotResult.model_validate(
            {k: v for k, v in snap.items() if k in OsUiSnapshotResult.model_fields}
        )
        assert parsed.accessibility_coverage == "chrome_only"
    finally:
        engine.close()


# ----------------------------------------------------------------- 5. Refresh, Fallback & Warmup


def test_ui_act_refresh_snapshot_false_vs_true_and_window_switch_auto_refresh() -> None:
    u32 = _FakeInputUser32(hwnd=9100, title="Main Window")

    def _switch_window() -> None:
        u32.hwnd = 9101
        u32.foreground_title = "Confirm Dialog"

    ok_btn_pat = _FakeInvokePattern()
    open_dlg_pat = _FakeInvokePattern(on_invoke=_switch_window)

    main_win = _FakeUiaElement(
        control_type=UIA_WINDOW_CONTROL_TYPE_ID,
        name="Main Window",
        hwnd=9100,
        children=[
            _FakeUiaElement(
                control_type=UIA_BUTTON_CONTROL_TYPE_ID,
                name="Save",
                rect=_FakeRect(10, 10, 90, 40),
                patterns={UIA_INVOKE_PATTERN_ID: ok_btn_pat},
            ),
            _FakeUiaElement(
                control_type=UIA_BUTTON_CONTROL_TYPE_ID,
                name="Open Dialog",
                rect=_FakeRect(100, 10, 200, 40),
                patterns={UIA_INVOKE_PATTERN_ID: open_dlg_pat},
            ),
        ],
    )
    dialog_win = _FakeUiaElement(
        control_type=UIA_WINDOW_CONTROL_TYPE_ID,
        name="Confirm Dialog",
        hwnd=9101,
        children=[
            _FakeUiaElement(
                control_type=UIA_BUTTON_CONTROL_TYPE_ID,
                name="Confirm",
                rect=_FakeRect(50, 50, 150, 80),
            )
        ],
    )
    auto = _FakeUIAutomation(windows_by_hwnd={9100: main_win, 9101: dialog_win})
    engine = NativeUiaComEngine(
        com_factory=lambda: (auto, None),
        co_initialize_fn=lambda _f: None,
        co_uninitialize_fn=lambda: None,
    )
    try:
        windows_os.ui_snapshot(uia_backend=engine, user32=u32, _target_hwnd=9100)
        walks_1 = auto.walker.first_child_calls

        # Default refresh_snapshot=False -> lightweight delta, ZERO full re-walk
        res_light = windows_os.ui_act("e1", "click", uia_backend=engine, user32=u32)
        assert res_light["ok"] is True
        assert res_light["snapshot_refreshed"] is False
        assert res_light["snapshot"]["status"] == "lightweight_delta"
        assert res_light["snapshot"]["count"] == 1
        assert res_light["active_window_title"] == "Main Window"
        assert res_light["active_hwnd"] == 9100
        assert res_light["target_state"]["available"] is True
        assert auto.walker.first_child_calls == walks_1

        # Explicit refresh_snapshot=True -> full tree walk & snapshot returned
        res_full = windows_os.ui_act(
            "e1", "click", refresh_snapshot=True, uia_backend=engine, user32=u32
        )
        assert res_full["ok"] is True
        assert res_full["snapshot_refreshed"] is True
        assert res_full["snapshot"] is not None
        assert res_full["snapshot"]["count"] == 2
        assert res_full["snapshot"]["window_title"] == "Main Window"
        assert auto.walker.first_child_calls > walks_1
        walks_2 = auto.walker.first_child_calls

        # Default refresh_snapshot=False when action triggers window/dialog switch -> auto re-walks!
        res_switch = windows_os.ui_act("e2", "click", uia_backend=engine, user32=u32)
        assert res_switch["ok"] is True
        assert res_switch["snapshot_refreshed"] is True
        assert res_switch["snapshot"] is not None
        assert res_switch["snapshot"]["window_title"] == "Confirm Dialog"
        assert res_switch["diff"]["window_changed"] is True
        assert res_switch["active_hwnd"] == 9101
        assert auto.walker.first_child_calls > walks_2

        # Protocol param defaults to refresh_snapshot=False and accepts True
        assert OsUiActParams(ref="e1", action="click").refresh_snapshot is False
        assert (
            OsUiActParams(ref="e1", action="click", refresh_snapshot=True).refresh_snapshot is True
        )
    finally:
        engine.close()


def test_com_routing_inspect_click_probe_chromium_guard_and_powershell_fallback(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    inv_pat = _FakeInvokePattern()
    win = _FakeUiaElement(
        control_type=UIA_WINDOW_CONTROL_TYPE_ID,
        name="Notepad",
        hwnd=9200,
        children=[
            _FakeUiaElement(
                control_type=UIA_BUTTON_CONTROL_TYPE_ID,
                name="Save",
                automation_id="saveBtn",
                rect=_FakeRect(20, 20, 100, 50),
                patterns={UIA_INVOKE_PATTERN_ID: inv_pat},
            ),
            _FakeUiaElement(
                control_type=UIA_TEXT_CONTROL_TYPE_ID,
                name="Hello from COM UIA",
                rect=_FakeRect(20, 70, 300, 100),
            ),
            _FakeUiaElement(
                control_type=UIA_LIST_ITEM_CONTROL_TYPE_ID,
                name="Item 1",
                rect=_FakeRect(20, 110, 200, 130),
            ),
        ],
    )
    auto = _FakeUIAutomation(windows_by_hwnd={9200: win})
    engine = NativeUiaComEngine(
        com_factory=lambda: (auto, None),
        co_initialize_fn=lambda _f: None,
        co_uninitialize_fn=lambda: None,
    )
    try:
        # 1. inspect_ui_elements routes through COM
        insp = windows_os.inspect_ui_elements(
            uia_backend=engine,
            window_enumerator=lambda: [
                windows_os.EnumeratedWindow(
                    hwnd=9200, title="Notepad", process_name="notepad.exe", is_visible=True
                )
            ],
            user32=_FakeInputUser32(hwnd=9200, title="Notepad"),
        )
        assert insp["ok"] is True
        assert insp["source"] == "uia_com"
        assert insp["count"] == 3
        assert insp["accessibility_coverage"] == "sparse"

        # 2. click_ui_element routes through COM and invokes InvokePattern directly
        clk = windows_os.click_ui_element(
            "Save",
            expected_hwnd=9200,
            uia_backend=engine,
            user32=_FakeInputUser32(hwnd=9200, title="Notepad"),
        )
        assert clk["ok"] is True
        assert clk["method"] == "invoke"
        assert inv_pat.invoked == 1

        # 3. vision.py probe_uia_text uses COM in-process without PowerShell
        monkeypatch.setattr(vision_mod, "get_window_process_name", lambda _h: "notepad.exe")
        monkeypatch.setattr(
            vision_mod,
            "run_powershell_hidden",
            lambda *_a, **_kw: (_ for _ in ()).throw(AssertionError("PowerShell must not run")),
        )
        v_backend = vision_mod.Win32ScreenVisionBackend(
            user32=MagicMock(), gdi32=None, dwmapi=None, uia_backend=engine
        )
        assert v_backend.probe_uia_text(9200) == "Hello from COM UIA"

        # 4. Chromium guard remains strictly intact (zero COM calls for Chrome/Edge)
        before_com_calls = len(auto.element_from_handle_calls)
        offline_cdp = CDPBridge(devtools_port_path=tmp_path / "missing")
        chrome_snap = windows_os.ui_snapshot(
            app_name="chrome",
            cdp_bridge=offline_cdp,
            uia_backend=engine,
            window_enumerator=lambda: [
                windows_os.EnumeratedWindow(
                    hwnd=9300,
                    title="Google Chrome",
                    process_name="chrome.exe",
                    is_visible=True,
                )
            ],
            user32=_FakeInputUser32(hwnd=9300, title="Google Chrome"),
        )
        assert chrome_snap["ok"] is False
        assert chrome_snap["cdp_available"] is False
        assert len(auto.element_from_handle_calls) == before_com_calls
    finally:
        engine.close()

    # 5. PowerShell fallback parity when COM is unavailable OR powershell_runner is injected
    broken_engine = NativeUiaComEngine(
        com_factory=lambda: (_ for _ in ()).throw(OSError("COM init failed")),
        co_initialize_fn=lambda _f: None,
        co_uninitialize_fn=lambda: None,
    )
    assert broken_engine.is_available() is False
    ps_calls: list[str] = []

    def _fake_ps(cmd: list[str], **_kw: Any) -> subprocess.CompletedProcess[str]:
        ps_calls.append(cmd[4])
        return subprocess.CompletedProcess(
            cmd,
            0,
            stdout=json.dumps(
                {
                    "window_title": "Legacy App",
                    "hwnd": 4321,
                    "elements": [
                        {
                            "name": "OK",
                            "control_type": "Button",
                            "automation_id": "okBtn",
                            "bounds": {"x": 10, "y": 10, "width": 60, "height": 25},
                            "center": {"cx": 40, "cy": 22},
                        }
                    ],
                }
            ),
            stderr="",
        )

    ps_snap = windows_os.ui_snapshot(
        uia_backend=broken_engine,
        powershell_runner=_fake_ps,
    )
    assert ps_snap["ok"] is True
    assert ps_snap["window_title"] == "Legacy App"
    assert ps_snap["accessibility_coverage"] == "chrome_only"
    assert len(ps_calls) == 1


@pytest.mark.asyncio
async def test_daemon_start_triggers_async_installed_apps_warmup_nonblocking_and_failure_safe(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    class _DummyProvider(LLMProvider):
        model_name = "dummy"

        async def stream_turn(
            self, messages: list[Message], tools: Any = None, **kwargs: Any
        ) -> Any:
            yield StreamChunk(text="ok")

    started_ev = threading.Event()
    release_ev = threading.Event()
    call_count = 0

    def _slow_get_installed_apps() -> list[dict[str, str]]:
        nonlocal call_count
        call_count += 1
        started_ev.set()
        release_ev.wait(timeout=2.0)
        return [{"Name": "Spotify", "AppID": "SpotifyAB.SpotifyMusic!Spotify"}]

    monkeypatch.delenv("CLEA_DISABLE_STARTUP_APP_WARMUP", raising=False)
    monkeypatch.setattr(daemon_mod, "get_installed_apps", _slow_get_installed_apps)

    memory = MemoryStore(tmp_path / "memory")
    agent = AgentLoop(provider=_DummyProvider(), memory=memory, registry=ToolRegistry())
    daemon = CleaDaemon(
        agent=agent,
        host="127.0.0.1",
        port=0,
        token_file=tmp_path / "token",
        lock_file=tmp_path / "daemon.lock",
    )

    t0 = time.monotonic()
    await daemon.start()
    start_elapsed = time.monotonic() - t0
    try:
        # start() MUST return immediately without waiting for Get-StartApps
        assert start_elapsed < 1.5
        assert daemon._app_cache_warmup_task is not None
        # Yield to event loop so the scheduled background task launches its worker thread
        await asyncio.sleep(0)
        assert started_ev.wait(timeout=1.5) is True
        release_ev.set()
        await asyncio.wait_for(daemon._app_cache_warmup_task, timeout=2.0)
        assert call_count == 1
    finally:
        release_ev.set()
        await daemon.stop()

    # Failure-safe check: exception in get_installed_apps() never crashes start() or stop()
    def _failing_get_installed_apps() -> list[dict[str, str]]:
        raise RuntimeError("StartApps failed")

    monkeypatch.setattr(daemon_mod, "get_installed_apps", _failing_get_installed_apps)
    daemon2 = CleaDaemon(
        agent=AgentLoop(
            provider=_DummyProvider(),
            memory=MemoryStore(tmp_path / "memory2"),
            registry=ToolRegistry(),
        ),
        host="127.0.0.1",
        port=0,
        token_file=tmp_path / "token2",
        lock_file=tmp_path / "daemon2.lock",
    )
    await daemon2.start()
    try:
        assert daemon2._app_cache_warmup_task is not None
        await asyncio.wait_for(daemon2._app_cache_warmup_task, timeout=2.0)
        assert daemon2._app_cache_warmup_task.exception() is None
    finally:
        await daemon2.stop()


# ------------------------------------------------------------------ 6. Real-COM Contract & Safety


def test_null_com_pointers_and_cached_property_ex_prevent_false_empty_values_and_live_ipc() -> None:
    """Verify NULL COM pointers (ptr=0x0) terminate walks cleanly, GetCachedPropertyValueEx
    avoids false '' values on Buttons / reads RangeValue on Sliders, and snapshot() never
    makes live GetCurrentPattern IPC calls.
    """
    not_supported_sentinel = ctypes.c_void_p(0xBEEF)

    class _ComLikeElement(_FakeUiaElement):
        def __init__(self, *args: Any, range_value: float | None = None, **kwargs: Any) -> None:
            super().__init__(*args, **kwargs)
            self.range_value = range_value
            self.current_pattern_calls = 0

        def GetCachedPropertyValue(self, _prop_id: int) -> Any:
            # Mirrors real Win32 UIA where ignoreDefaultValue=False returns "" for unsupported props
            return ""

        def GetCachedPropertyValueEx(self, prop_id: int, ignore_default: bool) -> Any:
            self._check_alive()
            assert ignore_default is True
            if prop_id == UIA_VALUE_VALUE_PROPERTY_ID and UIA_VALUE_PATTERN_ID in self.patterns:
                return self.patterns[UIA_VALUE_PATTERN_ID].value
            if prop_id == UIA_RANGE_VALUE_VALUE_PROPERTY_ID and self.range_value is not None:
                return self.range_value
            return not_supported_sentinel

        def GetCachedPattern(self, pattern_id: int) -> Any:
            self._check_alive()
            return self.patterns.get(pattern_id, ctypes.c_void_p(0))

        def GetCurrentPattern(self, pattern_id: int) -> Any:
            self.current_pattern_calls += 1
            return self.patterns.get(pattern_id, ctypes.c_void_p(0))

    class _NullPointerWalker(_FakeTreeWalker):
        """Returns ctypes.c_void_p(0) (matching comtypes NULL POINTER) instead of None."""

        def GetParentElement(self, node: _FakeUiaElement) -> Any:
            res = super().GetParentElement(node)
            return ctypes.c_void_p(0) if res is None else res

        def GetFirstChildElementBuildCache(self, node: _FakeUiaElement, cache_req: Any) -> Any:
            res = super().GetFirstChildElementBuildCache(node, cache_req)
            return ctypes.c_void_p(0) if res is None else res

        def GetNextSiblingElementBuildCache(self, node: _FakeUiaElement, cache_req: Any) -> Any:
            res = super().GetNextSiblingElementBuildCache(node, cache_req)
            return ctypes.c_void_p(0) if res is None else res

    btn = _ComLikeElement(
        control_type=UIA_BUTTON_CONTROL_TYPE_ID,
        name="Mute",
        rect=_FakeRect(10, 10, 90, 40),
    )
    slider = _ComLikeElement(
        control_type=UIA_SLIDER_CONTROL_TYPE_ID,
        name="Volume",
        rect=_FakeRect(100, 10, 300, 40),
        range_value=75.0,
    )
    win = _ComLikeElement(
        control_type=UIA_WINDOW_CONTROL_TYPE_ID,
        name="Mixer",
        hwnd=9400,
        children=[btn, slider],
    )
    auto = _FakeUIAutomation(windows_by_hwnd={9400: win})
    auto.walker = _NullPointerWalker()
    auto.CheckNotSupported = lambda val: (  # type: ignore[method-assign]
        val is None or (isinstance(val, ctypes.c_void_p) and val.value == 0xBEEF)
    )
    orig_from_handle = auto.ElementFromHandleBuildCache
    auto.ElementFromHandleBuildCache = lambda h, cr: (  # type: ignore[method-assign]
        orig_from_handle(h, cr) or ctypes.c_void_p(0)
    )

    engine = NativeUiaComEngine(
        max_nodes=2,  # Exact node budget: NULL pointers must NOT consume quota!
        com_factory=lambda: (auto, None),
        co_initialize_fn=lambda _f: None,
        co_uninitialize_fn=lambda: None,
    )
    try:
        snap = engine.snapshot(hwnd=9400)
        assert snap["count"] == 2
        assert snap["elements"][0]["name"] == "Mute"
        assert snap["elements"][0]["value"] is None  # NOT "" and NOT sentinel repr!
        assert snap["elements"][1]["name"] == "Volume"
        assert snap["elements"][1]["role"] == "slider"
        assert snap["elements"][1]["value"] == "75.0"
        # Zero live GetCurrentPattern cross-process IPC calls during cached snapshot walk
        assert btn.current_pattern_calls == 0
        assert slider.current_pattern_calls == 0

        # Unknown HWND returning NULL COM pointer degrades cleanly to empty snapshot
        empty_snap = engine.snapshot(hwnd=999999)
        assert empty_snap["ok"] is True
        assert empty_snap["count"] == 0
    finally:
        engine.close()


def test_ui_batch_preserves_uia_com_bounds_and_vision_probe_skips_ps_when_com_empty(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify ui_batch does not clobber _UI_REF_STORE bounds on uia_com snapshots,
    and vision.probe_uia_text skips slow PowerShell fallback when COM UIA already probed.
    """
    inv_pat = _FakeInvokePattern()
    win = _FakeUiaElement(
        control_type=UIA_WINDOW_CONTROL_TYPE_ID,
        name="Studio",
        hwnd=9500,
        children=[
            _FakeUiaElement(
                control_type=UIA_BUTTON_CONTROL_TYPE_ID,
                name="Export",
                rect=_FakeRect(1400, 900, 1520, 960),
                patterns={UIA_INVOKE_PATTERN_ID: inv_pat},
            )
        ],
    )
    auto = _FakeUIAutomation(windows_by_hwnd={9500: win})
    engine = NativeUiaComEngine(
        com_factory=lambda: (auto, None),
        co_initialize_fn=lambda _f: None,
        co_uninitialize_fn=lambda: None,
    )
    set_default_uia_com_engine(engine)
    u32 = _FakeInputUser32(hwnd=9500, title="Studio")
    try:
        batch_res = windows_os.ui_batch(
            [{"op": "act", "ref": "e1", "action": "click"}],
            user32=u32,
        )
        assert batch_res["ok"] is True
        # _UI_REF_STORE MUST retain exact physical pixel bounds/center/hwnd (not wiped to 0,0!)
        _, _, stored_hwnd, stored_entry = windows_os._UI_REF_STORE.resolve("e1")
        assert stored_hwnd == 9500
        assert stored_entry["bounds"] == {"x": 1400, "y": 900, "width": 120, "height": 60}
        assert stored_entry["center"] == {"cx": 1460, "cy": 930}

        # Empty/canvas window probed by COM UIA returns "" -> MUST skip 2.5s PowerShell fallback
        monkeypatch.setattr(vision_mod, "get_window_process_name", lambda _h: "game.exe")
        monkeypatch.setattr(
            vision_mod,
            "run_powershell_hidden",
            lambda *_a, **_kw: (_ for _ in ()).throw(
                AssertionError("PowerShell UIA fallback must not run after successful COM probe")
            ),
        )
        v_backend = vision_mod.Win32ScreenVisionBackend(
            user32=MagicMock(), gdi32=None, dwmapi=None, uia_backend=engine
        )
        # Window 9500 has only a Button (no Text/Edit/Document), so COM returns "" cleanly
        assert v_backend.probe_uia_text(9500) == ""
    finally:
        set_default_uia_com_engine(None)
        engine.close()


def test_com_unavailable_fallback_without_injected_powershell_runner(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Verify ui_snapshot actually tries com_engine.is_available() and falls back to
    PowerShell UIA when COM is unavailable and no powershell_runner is injected.
    """
    init_attempts = 0

    def _failing_factory() -> Any:
        nonlocal init_attempts
        init_attempts += 1
        raise OSError("Simulated COM unavailable")

    broken = NativeUiaComEngine(
        com_factory=_failing_factory,
        co_initialize_fn=lambda _f: None,
        co_uninitialize_fn=lambda: None,
    )
    ps_invoked = 0

    def _fake_ps_uia_json(
        _command: str,
        *,
        operation: str = "",
        powershell_runner: Any = None,
        timeout_seconds: float = 0.0,
    ) -> dict[str, Any]:
        nonlocal ps_invoked
        ps_invoked += 1
        return {
            "window_title": "Fallback Window",
            "hwnd": 7777,
            "elements": [
                {
                    "name": "Proceed",
                    "control_type": "Button",
                    "bounds": {"x": 10, "y": 20, "width": 80, "height": 30},
                    "center": {"cx": 50, "cy": 35},
                }
            ],
        }

    # Test real fallback path: broken default engine + _run_powershell_uia_json untouched check
    # via _resolve_uia_com_engine returning None when is_available() is False
    assert broken.is_available() is False
    assert init_attempts == 1
    assert windows_os._resolve_uia_com_engine(uia_backend=broken) is None

    # Verify fallback execution end-to-end when _run_powershell_uia_json handles the PS leg
    monkeypatch.setattr(windows_os, "_run_powershell_uia_json", _fake_ps_uia_json)
    snap = windows_os.ui_snapshot(uia_backend=broken)
    assert snap["ok"] is True
    assert snap["window_title"] == "Fallback Window"
    assert ps_invoked == 1


@pytest.mark.skipif(sys.platform != "win32", reason="Windows COM MTA apartment verification")
def test_real_com_worker_thread_runs_in_true_mta_apartment_across_lifecycles() -> None:
    """Verify the real Windows COM worker thread enters true APTTYPE_MTA (1), never STA (0),
    both on first comtypes import and on subsequent engine lifecycles (no desktop windows touched).
    """

    def _query_apartment_type() -> tuple[int, int]:
        ole32 = ctypes.WinDLL("ole32")
        apt = ctypes.c_int(-1)
        qual = ctypes.c_int(-1)
        hr = int(ole32.CoGetApartmentType(ctypes.byref(apt), ctypes.byref(qual)))
        return hr, int(apt.value)

    for _ in range(2):
        eng = NativeUiaComEngine()
        try:
            assert eng.is_available() is True
            assert eng.automation_class in {"CUIAutomation8", "CUIAutomation"}
            hr, apt_type = eng._submit(_query_apartment_type)
            assert hr == 0
            assert apt_type == 1  # APTTYPE_MTA (0 = STA, 1 = MTA)
            assert eng._co_initialized_on_worker is True
        finally:
            eng.close()

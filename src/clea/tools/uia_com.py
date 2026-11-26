"""Native in-process Win32 UI Automation COM backend (comtypes).

Runs all COM IUIAutomation calls on a single persistent background MTA thread
(``CoInitializeEx(COINIT_MULTITHREADED)``) inside Clea's Per-Monitor-V2 DPI-aware
process so:
- Bounding rectangles are reported in exact physical screen pixels (no 125% DPI drift).
- ``CUIAutomation8`` ``ConnectionTimeout`` and ``TransactionTimeout`` guard against
  hung target applications, with automatic fallback to ``CUIAutomation``.
- Live ``IUIAutomationElement`` handles are cached in a bounded LRU keyed by
  ``(snapshot_id, ref)`` with explicit COM ``Release()`` on eviction, enabling
  sub-15 ms pattern dispatch without re-walking the UI tree.
"""

from __future__ import annotations

import collections
import concurrent.futures
import contextlib
import ctypes
import logging
import math
import queue
import sys
import threading
import uuid
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from typing import Any, Literal

logger = logging.getLogger(__name__)

# --------------------------------------------------------------------------- Constants

COINIT_MULTITHREADED: int = 0x0
RPC_E_CHANGED_MODE: int = -2147417850  # 0x80010106

# Stale / disconnected COM HRESULTs (signed 32-bit and unsigned forms)
UIA_E_ELEMENTNOTAVAILABLE: int = -2147220991  # 0x80040201
UIA_E_ELEMENTNOTENABLED: int = -2147220990  # 0x80040202
UIA_E_NOCLICKABLEPOINT: int = -2147220992  # 0x80040200
UIA_E_NOTSUPPORTED: int = -2147220988  # 0x80040204
RPC_E_DISCONNECTED: int = -2147417848  # 0x80010108
CO_E_OBJNOTCONNECTED: int = -2147220995  # 0x800401FD
RPC_S_SERVER_UNAVAILABLE: int = -2147023174  # 0x800706BA
E_HANDLE: int = -2147024890  # 0x80070006
E_NOINTERFACE: int = -2147467262  # 0x80004002
E_NOTIMPL: int = -2147467263  # 0x80004001

_STALE_HRESULTS: frozenset[int] = frozenset(
    {
        UIA_E_ELEMENTNOTAVAILABLE,
        UIA_E_ELEMENTNOTAVAILABLE & 0xFFFFFFFF,
        RPC_E_DISCONNECTED,
        RPC_E_DISCONNECTED & 0xFFFFFFFF,
        CO_E_OBJNOTCONNECTED,
        CO_E_OBJNOTCONNECTED & 0xFFFFFFFF,
        RPC_S_SERVER_UNAVAILABLE,
        RPC_S_SERVER_UNAVAILABLE & 0xFFFFFFFF,
        E_HANDLE,
        E_HANDLE & 0xFFFFFFFF,
    }
)

_UNSUPPORTED_PATTERN_HRESULTS: frozenset[int] = frozenset(
    {
        E_NOINTERFACE,
        E_NOINTERFACE & 0xFFFFFFFF,
        E_NOTIMPL,
        E_NOTIMPL & 0xFFFFFFFF,
        UIA_E_NOTSUPPORTED,
        UIA_E_NOTSUPPORTED & 0xFFFFFFFF,
    }
)

# UIA TreeScope & AutomationElementMode constants
TREE_SCOPE_ELEMENT: int = 1
AUTOMATION_ELEMENT_MODE_FULL: int = 1

# UIA Property IDs
UIA_BOUNDING_RECTANGLE_PROPERTY_ID: int = 30001
UIA_CONTROL_TYPE_PROPERTY_ID: int = 30003
UIA_NAME_PROPERTY_ID: int = 30005
UIA_HAS_KEYBOARD_FOCUS_PROPERTY_ID: int = 30008
UIA_IS_ENABLED_PROPERTY_ID: int = 30010
UIA_AUTOMATION_ID_PROPERTY_ID: int = 30011
UIA_NATIVE_WINDOW_HANDLE_PROPERTY_ID: int = 30020
UIA_IS_OFFSCREEN_PROPERTY_ID: int = 30022
UIA_VALUE_VALUE_PROPERTY_ID: int = 30045
UIA_VALUE_IS_READONLY_PROPERTY_ID: int = 30046
UIA_RANGE_VALUE_VALUE_PROPERTY_ID: int = 30047
UIA_EXPAND_COLLAPSE_STATE_PROPERTY_ID: int = 30070
UIA_SELECTION_ITEM_IS_SELECTED_PROPERTY_ID: int = 30079
UIA_TOGGLE_STATE_PROPERTY_ID: int = 30086

# UIA Pattern IDs
UIA_INVOKE_PATTERN_ID: int = 10000
UIA_VALUE_PATTERN_ID: int = 10002
UIA_RANGE_VALUE_PATTERN_ID: int = 10003
UIA_EXPAND_COLLAPSE_PATTERN_ID: int = 10005
UIA_SELECTION_ITEM_PATTERN_ID: int = 10010
UIA_TOGGLE_PATTERN_ID: int = 10015

# UIA ControlType IDs
UIA_BUTTON_CONTROL_TYPE_ID: int = 50000
UIA_CHECKBOX_CONTROL_TYPE_ID: int = 50002
UIA_COMBOBOX_CONTROL_TYPE_ID: int = 50003
UIA_EDIT_CONTROL_TYPE_ID: int = 50004
UIA_HYPERLINK_CONTROL_TYPE_ID: int = 50005
UIA_LIST_ITEM_CONTROL_TYPE_ID: int = 50007
UIA_MENU_ITEM_CONTROL_TYPE_ID: int = 50011
UIA_RADIO_BUTTON_CONTROL_TYPE_ID: int = 50013
UIA_SLIDER_CONTROL_TYPE_ID: int = 50015
UIA_TAB_ITEM_CONTROL_TYPE_ID: int = 50019
UIA_TEXT_CONTROL_TYPE_ID: int = 50020
UIA_TREE_ITEM_CONTROL_TYPE_ID: int = 50024
UIA_CUSTOM_CONTROL_TYPE_ID: int = 50025
UIA_GROUP_CONTROL_TYPE_ID: int = 50026
UIA_DATA_ITEM_CONTROL_TYPE_ID: int = 50029
UIA_DOCUMENT_CONTROL_TYPE_ID: int = 50030
UIA_SPLIT_BUTTON_CONTROL_TYPE_ID: int = 50031
UIA_WINDOW_CONTROL_TYPE_ID: int = 50032
UIA_PANE_CONTROL_TYPE_ID: int = 50033

# ExpandCollapseState / ToggleState values
EXPAND_COLLAPSE_STATE_COLLAPSED: int = 0
EXPAND_COLLAPSE_STATE_EXPANDED: int = 1
EXPAND_COLLAPSE_STATE_PARTIALLY_EXPANDED: int = 2
EXPAND_COLLAPSE_STATE_LEAF_NODE: int = 3

TOGGLE_STATE_OFF: int = 0
TOGGLE_STATE_ON: int = 1
TOGGLE_STATE_INDETERMINATE: int = 2

_TOGGLE_STATE_NAMES: dict[int, str] = {
    TOGGLE_STATE_OFF: "off",
    TOGGLE_STATE_ON: "on",
    TOGGLE_STATE_INDETERMINATE: "indeterminate",
}

_EXPAND_STATE_NAMES: dict[int, str] = {
    EXPAND_COLLAPSE_STATE_COLLAPSED: "collapsed",
    EXPAND_COLLAPSE_STATE_EXPANDED: "expanded",
    EXPAND_COLLAPSE_STATE_PARTIALLY_EXPANDED: "partially_expanded",
    EXPAND_COLLAPSE_STATE_LEAF_NODE: "leaf",
}

CONTROL_TYPE_ID_TO_NAME: dict[int, str] = {
    UIA_BUTTON_CONTROL_TYPE_ID: "Button",
    UIA_CHECKBOX_CONTROL_TYPE_ID: "CheckBox",
    UIA_COMBOBOX_CONTROL_TYPE_ID: "ComboBox",
    UIA_EDIT_CONTROL_TYPE_ID: "Edit",
    UIA_HYPERLINK_CONTROL_TYPE_ID: "Hyperlink",
    UIA_LIST_ITEM_CONTROL_TYPE_ID: "ListItem",
    UIA_MENU_ITEM_CONTROL_TYPE_ID: "MenuItem",
    UIA_RADIO_BUTTON_CONTROL_TYPE_ID: "RadioButton",
    UIA_SLIDER_CONTROL_TYPE_ID: "Slider",
    UIA_TAB_ITEM_CONTROL_TYPE_ID: "TabItem",
    UIA_TEXT_CONTROL_TYPE_ID: "Text",
    UIA_TREE_ITEM_CONTROL_TYPE_ID: "TreeItem",
    UIA_CUSTOM_CONTROL_TYPE_ID: "Custom",
    UIA_GROUP_CONTROL_TYPE_ID: "Group",
    UIA_DATA_ITEM_CONTROL_TYPE_ID: "DataItem",
    UIA_DOCUMENT_CONTROL_TYPE_ID: "Document",
    UIA_SPLIT_BUTTON_CONTROL_TYPE_ID: "SplitButton",
    UIA_WINDOW_CONTROL_TYPE_ID: "Window",
    UIA_PANE_CONTROL_TYPE_ID: "Pane",
}

_CONTROL_TYPE_TO_ROLE: dict[str, str] = {
    "button": "button",
    "splitbutton": "button",
    "edit": "input",
    "input": "input",
    "hyperlink": "link",
    "link": "link",
    "tabitem": "tab",
    "tab": "tab",
    "menuitem": "menu_item",
    "menu_item": "menu_item",
    "checkbox": "checkbox",
    "radiobutton": "radio",
    "radio": "radio",
    "combobox": "combo",
    "combo": "combo",
    "listitem": "list_item",
    "list_item": "list_item",
    "text": "text",
    "treeitem": "list_item",
    "dataitem": "list_item",
    "slider": "slider",
    "document": "input",
}

_ALLOWED_SNAPSHOT_CONTROL_TYPES: frozenset[str] = frozenset(
    {
        "Button",
        "SplitButton",
        "Edit",
        "Document",
        "MenuItem",
        "TabItem",
        "Hyperlink",
        "ListItem",
        "TreeItem",
        "DataItem",
        "CheckBox",
        "RadioButton",
        "ComboBox",
        "Slider",
        "Text",
    }
)

_STRUCTURAL_WRAPPER_TYPES: frozenset[str] = frozenset({"Pane", "Group"})
_PROBE_TEXT_CONTROL_TYPES: frozenset[str] = frozenset({"Text", "Edit", "Document"})

# Default engine budgets & timeouts
DEFAULT_CONNECTION_TIMEOUT_MS: int = 1800
DEFAULT_TRANSACTION_TIMEOUT_MS: int = 1800
DEFAULT_MAX_DEPTH: int = 16
DEFAULT_MAX_NODES: int = 400
DEFAULT_MAX_CACHED_ELEMENTS: int = 512
DEFAULT_CALL_TIMEOUT_SECONDS: float = 5.0

AccessibilityCoverage = Literal["chrome_only", "sparse", "rich"]

CHROME_ONLY_GUIDANCE: str = (
    "Window exposes only standard frame chrome via UI Automation (<=2 content controls); "
    "use inspect_screen/look_at_screen with gui_action coordinates or keyboard shortcuts "
    "instead of UIA."
)

_WINDOW_CHROME_NAMES: frozenset[str] = frozenset(
    {
        "minimize",
        "maximize",
        "restore",
        "restore down",
        "close",
        "system",
        "minimize window",
        "maximize window",
        "restore window",
        "close window",
    }
)

_WINDOW_CHROME_AUTOMATION_IDS: frozenset[str] = frozenset(
    {
        "minimize",
        "maximize",
        "restore",
        "close",
        "minimize-button",
        "maximize-button",
        "restore-button",
        "close-button",
        "titlebar",
        "systemmenubar",
    }
)

_WINDOW_CHROME_ROLES: frozenset[str] = frozenset(
    {"button", "menu_item", "menuitem", "titlebar", "menubar", ""}
)


# --------------------------------------------------------------------------- Exceptions & Helpers


class UiaComUnavailableError(RuntimeError):
    """Raised when the in-process COM UIA engine cannot be initialized or reached."""


class UiaStaleElementError(RuntimeError):
    """Raised when a cached COM IUIAutomationElement handle is no longer valid."""

    def __init__(self, message: str, *, hresult: int | None = None) -> None:
        super().__init__(message)
        self.hresult = hresult


def _extract_hresult(exc: BaseException) -> int | None:
    """Extract a COM HRESULT integer from COMError / OSError / custom exceptions."""
    hr = getattr(exc, "hresult", None)
    if isinstance(hr, int) and not isinstance(hr, bool):
        return hr
    winerr = getattr(exc, "winerror", None)
    if isinstance(winerr, int) and not isinstance(winerr, bool):
        return winerr
    args = getattr(exc, "args", ())
    if args and isinstance(args[0], int) and not isinstance(args[0], bool):
        return int(args[0])
    return None


def is_stale_com_error(exc: BaseException) -> bool:
    """Return True if an exception represents an invalidated / stale UIA COM handle."""
    if isinstance(exc, UiaStaleElementError):
        return True
    hr = _extract_hresult(exc)
    if hr is not None and (hr in _STALE_HRESULTS or (hr & 0xFFFFFFFF) in _STALE_HRESULTS):
        return True
    return False


def _is_unsupported_pattern_error(exc: BaseException) -> bool:
    """Return True if a COM error merely indicates a pattern is not supported."""
    hr = _extract_hresult(exc)
    if hr is not None and (
        hr in _UNSUPPORTED_PATTERN_HRESULTS
        or (hr & 0xFFFFFFFF) in _UNSUPPORTED_PATTERN_HRESULTS
    ):
        return True
    return False


def _format_hresult(hr: int | None) -> str:
    if hr is None:
        return "unknown"
    return f"0x{hr & 0xFFFFFFFF:08X}"


_COMTYPES_IMPORT_LOCK = threading.Lock()
_S_OK: int = 0
_S_FALSE: int = 1


def _is_valid_com_obj(obj: Any) -> bool:
    """Return False for None and NULL comtypes/ctypes COM pointers (ptr=0x0)."""
    if obj is None:
        return False
    if isinstance(obj, (ctypes.c_void_p, ctypes._Pointer)):
        return bool(obj)
    return True


def _ensure_pmv2_dpi_awareness() -> None:
    """Best-effort idempotent Per-Monitor-V2 DPI awareness for physical pixel UIA bounds."""
    if sys.platform != "win32":
        return
    with contextlib.suppress(Exception):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        if hasattr(user32, "SetProcessDpiAwarenessContext"):
            user32.SetProcessDpiAwarenessContext.argtypes = [ctypes.c_void_p]
            user32.SetProcessDpiAwarenessContext.restype = ctypes.c_int
            # DPI_AWARENESS_CONTEXT_PER_MONITOR_AWARE_V2 = -4
            user32.SetProcessDpiAwarenessContext(ctypes.c_void_p(-4))


def _win32_co_initialize_mta() -> int:
    """Initialize the calling thread explicitly as COM MTA via ole32.CoInitializeEx."""
    ole32 = ctypes.WinDLL("ole32")
    co_init = ole32.CoInitializeEx
    co_init.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    co_init.restype = ctypes.c_long
    hr = int(co_init(None, COINIT_MULTITHREADED))
    if hr not in (_S_OK, _S_FALSE):
        raise OSError(hr, f"CoInitializeEx(COINIT_MULTITHREADED) failed: {_format_hresult(hr)}")
    return hr


def _win32_co_uninitialize() -> None:
    """Balance a prior ole32.CoInitializeEx call on the calling thread."""
    ole32 = ctypes.WinDLL("ole32")
    co_uninit = ole32.CoUninitialize
    co_uninit.argtypes = []
    co_uninit.restype = None
    co_uninit()


def _import_comtypes_preserving_mta() -> tuple[Any, int]:
    """Import ``comtypes`` without letting its top-level CoInitializeEx flip this thread to STA.

    Returns ``(comtypes_module, extra_co_init_calls)`` where ``extra_co_init_calls`` is 1 if
    this call triggered the first-ever import of ``comtypes`` (which runs ``CoInitializeEx(0)``
    on the current thread) and 0 otherwise.
    """
    with _COMTYPES_IMPORT_LOCK:
        first_import = "comtypes" not in sys.modules
        if first_import:
            had_flag = hasattr(sys, "coinit_flags")
            prev_flag = getattr(sys, "coinit_flags", None)
            sys.coinit_flags = COINIT_MULTITHREADED  # type: ignore[attr-defined]
            try:
                import comtypes
            finally:
                if had_flag:
                    sys.coinit_flags = prev_flag  # type: ignore[attr-defined]
                elif hasattr(sys, "coinit_flags"):
                    delattr(sys, "coinit_flags")
            return comtypes, 1
        import comtypes

        return comtypes, 0


def _release_com_object(obj: Any) -> None:
    """Explicitly release a COM pointer once without risking double-Release in __del__."""
    if obj is None:
        return
    try:
        if isinstance(obj, (ctypes.c_void_p, ctypes._Pointer)):
            if bool(obj):
                release_fn = getattr(obj, "Release", None)
                if callable(release_fn):
                    with contextlib.suppress(Exception):
                        release_fn()
                # Null out the raw pointer slot so comtypes' __del__ will not Release() twice.
                ctypes.cast(ctypes.byref(obj), ctypes.POINTER(ctypes.c_void_p)).contents.value = 0
            return
        release_fn = getattr(obj, "Release", None) or getattr(obj, "release", None)
        if callable(release_fn):
            release_fn()
    except Exception:
        pass


def is_window_chrome_element(el: dict[str, Any]) -> bool:
    """Return True when an element is standard window-frame chrome (Minimize/Maximize/Close/etc)."""
    role = str(el.get("role") or "").strip().lower()
    ct = str(el.get("control_type") or "").strip().lower()
    if ct.startswith("controltype."):
        ct = ct[len("controltype.") :]
    if role and role not in _WINDOW_CHROME_ROLES and ct not in _WINDOW_CHROME_ROLES:
        return False
    name = str(el.get("name") or "").strip().lower()
    aid = str(el.get("automation_id") or "").strip().lower()
    if name in _WINDOW_CHROME_NAMES:
        return True
    if aid and aid in _WINDOW_CHROME_AUTOMATION_IDS:
        return True
    return False


def classify_accessibility_coverage(
    elements: Sequence[dict[str, Any]],
) -> tuple[AccessibilityCoverage, int, str | None]:
    """Classify UIA accessibility coverage after filtering out standard window-frame chrome.

    Thresholds (on actual content controls):
    - ``<= 2`` -> ``"chrome_only"`` (with concise fallback guidance)
    - ``3..6`` -> ``"sparse"``
    - ``> 6``  -> ``"rich"``
    """
    content_count = sum(
        1 for el in elements if isinstance(el, dict) and not is_window_chrome_element(el)
    )
    if content_count <= 2:
        return "chrome_only", content_count, CHROME_ONLY_GUIDANCE
    if content_count <= 6:
        return "sparse", content_count, None
    return "rich", content_count, None


def _normalize_control_type_name(raw_ct: Any) -> str:
    """Normalize a UIA ControlType ID or string to its canonical PascalCase name."""
    if isinstance(raw_ct, int) and not isinstance(raw_ct, bool):
        return CONTROL_TYPE_ID_TO_NAME.get(raw_ct, "")
    text = str(raw_ct or "").strip()
    if text.lower().startswith("controltype."):
        text = text[len("controltype.") :]
    return text


def _control_type_to_role(control_type: str) -> str:
    cleaned = control_type.strip().lower()
    if cleaned.startswith("controltype."):
        cleaned = cleaned[len("controltype.") :]
    return _CONTROL_TYPE_TO_ROLE.get(cleaned, cleaned or "text")


def _extract_rect(raw_rect: Any) -> tuple[int, int, int, int, int, int] | None:
    """Extract physical screen pixel (x, y, w, h, cx, cy) from RECT or sequence/dict."""
    if raw_rect is None:
        return None
    left: float
    top: float
    right: float
    bottom: float
    if hasattr(raw_rect, "left") and hasattr(raw_rect, "right"):
        left = float(raw_rect.left)
        top = float(raw_rect.top)
        right = float(raw_rect.right)
        bottom = float(raw_rect.bottom)
    elif isinstance(raw_rect, dict):
        left = float(raw_rect.get("left", raw_rect.get("x", 0)))
        top = float(raw_rect.get("top", raw_rect.get("y", 0)))
        if "right" in raw_rect and "bottom" in raw_rect:
            right = float(raw_rect["right"])
            bottom = float(raw_rect["bottom"])
        else:
            right = left + float(raw_rect.get("width", 0))
            bottom = top + float(raw_rect.get("height", 0))
    elif isinstance(raw_rect, (tuple, list)) and len(raw_rect) == 4:
        left, top, right, bottom = (float(v) for v in raw_rect)
    else:
        return None

    width = right - left
    height = bottom - top
    if (
        not math.isfinite(left)
        or not math.isfinite(top)
        or not math.isfinite(width)
        or not math.isfinite(height)
        or width <= 0
        or height <= 0
    ):
        return None
    bx = int(round(left))
    by = int(round(top))
    bw = int(round(width))
    bh = int(round(height))
    if bw <= 0 or bh <= 0:
        return None
    cx = int(round(left + (width / 2.0)))
    cy = int(round(top + (height / 2.0)))
    return bx, by, bw, bh, cx, cy


def _safe_prop(obj: Any, *attr_names: str, default: Any = None) -> Any:
    """Read the first available attribute from a COM element, propagating stale errors."""
    for name in attr_names:
        if hasattr(obj, name):
            try:
                val = getattr(obj, name)
                if callable(val):
                    val = val()
                return val
            except Exception as exc:
                if is_stale_com_error(exc):
                    raise
                continue
    return default


def _is_not_supported_sentinel(val: Any, automation: Any) -> bool:
    if val is None:
        return True
    with contextlib.suppress(Exception):
        if automation is not None and hasattr(automation, "CheckNotSupported"):
            if bool(automation.CheckNotSupported(val)):
                return True
    return False


# --------------------------------------------------------------------------- Live Element Entry


@dataclass
class _LiveElementEntry:
    element: Any
    hwnd: int
    window_title: str
    role: str
    name: str
    control_type: str
    automation_id: str
    value: str | None
    enabled: bool
    focused: bool
    bounds: dict[str, int]
    center: dict[str, int]


@dataclass
class _ComRuntimeContext:
    automation: Any
    walker: Any
    cache_request: Any
    uia_module: Any
    automation_class: str
    timeouts_configured: bool


# --------------------------------------------------------------------------- Native UIA COM Engine


class NativeUiaComEngine:
    """Thread-safe in-process Win32 IUIAutomation COM engine backed by a single MTA thread."""

    def __init__(
        self,
        *,
        connection_timeout_ms: int = DEFAULT_CONNECTION_TIMEOUT_MS,
        transaction_timeout_ms: int = DEFAULT_TRANSACTION_TIMEOUT_MS,
        max_depth: int = DEFAULT_MAX_DEPTH,
        max_nodes: int = DEFAULT_MAX_NODES,
        max_cached_elements: int = DEFAULT_MAX_CACHED_ELEMENTS,
        call_timeout_seconds: float = DEFAULT_CALL_TIMEOUT_SECONDS,
        com_factory: Callable[[], _ComRuntimeContext | tuple[Any, Any]] | None = None,
        get_module_fn: Callable[[str], Any] | None = None,
        create_object_fn: Callable[..., Any] | None = None,
        co_initialize_fn: Callable[[int], Any] | None = None,
        co_uninitialize_fn: Callable[[], Any] | None = None,
    ) -> None:
        self._connection_timeout_ms = max(250, int(connection_timeout_ms))
        self._transaction_timeout_ms = max(250, int(transaction_timeout_ms))
        self._max_depth = max(1, int(max_depth))
        self._max_nodes = max(1, int(max_nodes))
        self._max_cached_elements = max(1, int(max_cached_elements))
        self._call_timeout_seconds = max(0.5, float(call_timeout_seconds))
        self._com_factory = com_factory
        self._get_module_fn = get_module_fn
        self._create_object_fn = create_object_fn
        self._co_initialize_fn = co_initialize_fn
        self._co_uninitialize_fn = co_uninitialize_fn

        self._lifecycle_lock = threading.Lock()
        self._job_queue: queue.Queue[
            tuple[Callable[[], Any], concurrent.futures.Future[Any]] | None
        ] = queue.Queue()
        self._worker_thread: threading.Thread | None = None
        self._worker_thread_id: int | None = None
        self._ready_event = threading.Event()
        self._init_error: BaseException | None = None
        self._closed = False

        # Accessed exclusively on the MTA worker thread once running:
        self._ctx: _ComRuntimeContext | None = None
        self._live_elements: collections.OrderedDict[tuple[str, str], _LiveElementEntry] = (
            collections.OrderedDict()
        )
        self._evicted_count: int = 0
        self._co_initialized_on_worker: bool = False
        self._co_init_flags_used: int | None = None

    @property
    def automation_class(self) -> str | None:
        return self._ctx.automation_class if self._ctx is not None else None

    @property
    def timeouts_configured(self) -> bool:
        return bool(self._ctx.timeouts_configured) if self._ctx is not None else False

    @property
    def worker_thread_id(self) -> int | None:
        return self._worker_thread_id

    @property
    def co_init_flags_used(self) -> int | None:
        return self._co_init_flags_used

    @property
    def evicted_count(self) -> int:
        return self._evicted_count

    @property
    def cached_element_count(self) -> int:
        return len(self._live_elements)

    # --------------------------------------------------------------- Lifecycle & MTA Dispatch

    def start(self) -> None:
        """Start the persistent MTA worker thread and initialize IUIAutomation."""
        with self._lifecycle_lock:
            if self._closed:
                raise UiaComUnavailableError("NativeUiaComEngine has been closed.")
            if self._worker_thread is not None and self._worker_thread.is_alive():
                if self._init_error is not None:
                    raise UiaComUnavailableError(str(self._init_error)) from self._init_error
                return
            self._ready_event.clear()
            self._init_error = None
            t = threading.Thread(
                target=self._mta_worker_main,
                name="clea-uia-com-mta",
                daemon=True,
            )
            self._worker_thread = t
            t.start()

        if not self._ready_event.wait(timeout=self._call_timeout_seconds):
            raise UiaComUnavailableError("Timed out initializing Win32 UIA COM MTA worker.")
        if self._init_error is not None:
            raise UiaComUnavailableError(str(self._init_error)) from self._init_error

    def is_available(self) -> bool:
        """Return True if the in-process COM UIA engine is ready for use."""
        if self._closed:
            return False
        if (
            self._com_factory is None
            and self._create_object_fn is None
            and self._get_module_fn is None
            and sys.platform != "win32"
        ):
            return False
        try:
            self.start()
            return self._ctx is not None and self._init_error is None
        except Exception as exc:
            logger.debug("Native UIA COM backend unavailable: %s", exc)
            return False

    def _submit(self, fn: Callable[[], Any], *, timeout: float | None = None) -> Any:
        """Execute ``fn`` strictly on the persistent MTA worker thread."""
        self.start()
        if threading.get_ident() == self._worker_thread_id:
            return fn()
        fut: concurrent.futures.Future[Any] = concurrent.futures.Future()
        self._job_queue.put((fn, fut))
        eff_timeout = self._call_timeout_seconds if timeout is None else timeout
        try:
            return fut.result(timeout=eff_timeout)
        except concurrent.futures.TimeoutError as exc:
            raise TimeoutError(
                f"Native UIA COM operation timed out after {eff_timeout:.1f}s."
            ) from exc

    def _mta_worker_main(self) -> None:
        """Persistent MTA worker loop owning all COM pointers for this engine."""
        self._worker_thread_id = threading.get_ident()
        co_uninit: Callable[[], Any] | None = self._co_uninitialize_fn
        co_init_balance = 0

        try:
            if self._co_initialize_fn is not None:
                self._co_initialize_fn(COINIT_MULTITHREADED)
                self._co_init_flags_used = COINIT_MULTITHREADED
                co_init_balance = 1
            else:
                _ensure_pmv2_dpi_awareness()
                # Initialize true COM MTA on this worker thread BEFORE importing comtypes,
                # because comtypes.__init__ otherwise runs CoInitializeEx(COINIT_APARTMENTTHREADED)
                # (STA) on first import.
                _win32_co_initialize_mta()
                co_init_balance = 1
                co_uninit = self._co_uninitialize_fn or _win32_co_uninitialize
                _, extra_inits = _import_comtypes_preserving_mta()
                co_init_balance += extra_inits
                self._co_init_flags_used = COINIT_MULTITHREADED

            self._co_initialized_on_worker = co_init_balance > 0
            self._ctx = self._initialize_com_context()
        except BaseException as exc:
            self._init_error = exc
            self._ctx = None
            self._ready_event.set()
            if co_uninit is not None:
                for _ in range(co_init_balance):
                    with contextlib.suppress(Exception):
                        co_uninit()
            return

        self._ready_event.set()

        try:
            while True:
                job = self._job_queue.get()
                if job is None:
                    break
                fn, fut = job
                if fut.cancelled():
                    continue
                try:
                    result = fn()
                except BaseException as exc:
                    if not fut.done():
                        fut.set_exception(exc)
                else:
                    if not fut.done():
                        fut.set_result(result)
        finally:
            self._clear_live_cache_on_worker()
            if self._ctx is not None:
                _release_com_object(self._ctx.cache_request)
                _release_com_object(self._ctx.walker)
                _release_com_object(self._ctx.automation)
                self._ctx = None
            if co_uninit is not None:
                for _ in range(co_init_balance):
                    with contextlib.suppress(Exception):
                        co_uninit()

    def _initialize_com_context(self) -> _ComRuntimeContext:
        """Create CUIAutomation8 (with hang timeouts) or fall back to CUIAutomation."""
        if self._com_factory is not None:
            created = self._com_factory()
            if isinstance(created, _ComRuntimeContext):
                return created
            automation, uia_mod = created
            automation_class, timeouts_ok = self._configure_automation_timeouts(automation)
            walker = automation.ControlViewWalker
            cache_req = self._build_cache_request(automation)
            return _ComRuntimeContext(
                automation=automation,
                walker=walker,
                cache_request=cache_req,
                uia_module=uia_mod,
                automation_class=automation_class,
                timeouts_configured=timeouts_ok,
            )

        if self._get_module_fn is not None and self._create_object_fn is not None:
            get_module = self._get_module_fn
            create_object = self._create_object_fn
        else:
            import comtypes.client

            get_module = self._get_module_fn or comtypes.client.GetModule
            create_object = self._create_object_fn or comtypes.client.CreateObject

        uia_mod = get_module("UIAutomationCore.dll")
        automation: Any = None
        automation_class = "CUIAutomation"
        timeouts_ok = False

        # Prefer CUIAutomation8 (IUIAutomation2) for ConnectionTimeout & TransactionTimeout
        if hasattr(uia_mod, "CUIAutomation8") and hasattr(uia_mod, "IUIAutomation2"):
            try:
                automation = create_object(
                    uia_mod.CUIAutomation8,
                    interface=uia_mod.IUIAutomation2,
                )
                automation_class = "CUIAutomation8"
                timeouts_ok = self._apply_timeouts(automation)
            except Exception as exc:
                logger.debug("CUIAutomation8 unavailable, falling back to CUIAutomation: %s", exc)
                _release_com_object(automation)
                automation = None

        if automation is None:
            automation = create_object(
                uia_mod.CUIAutomation,
                interface=uia_mod.IUIAutomation,
            )
            automation_class = "CUIAutomation"
            timeouts_ok = False

        walker = automation.ControlViewWalker
        cache_req = self._build_cache_request(automation)
        return _ComRuntimeContext(
            automation=automation,
            walker=walker,
            cache_request=cache_req,
            uia_module=uia_mod,
            automation_class=automation_class,
            timeouts_configured=timeouts_ok,
        )

    def _apply_timeouts(self, automation: Any) -> bool:
        """Apply ConnectionTimeout and TransactionTimeout on IUIAutomation2."""
        applied = False
        if hasattr(automation, "ConnectionTimeout") and hasattr(automation, "TransactionTimeout"):
            try:
                automation.ConnectionTimeout = self._connection_timeout_ms
                automation.TransactionTimeout = self._transaction_timeout_ms
                applied = True
            except Exception as exc:
                logger.debug("Failed setting UIA2 timeouts: %s", exc)
        return applied

    def _configure_automation_timeouts(self, automation: Any) -> tuple[str, bool]:
        """Determine automation class and apply timeouts for injected COM factories."""
        if hasattr(automation, "ConnectionTimeout") and hasattr(automation, "TransactionTimeout"):
            ok = self._apply_timeouts(automation)
            cls_name = getattr(automation, "automation_class", "CUIAutomation8")
            return str(cls_name), ok
        cls_name = getattr(automation, "automation_class", "CUIAutomation")
        return str(cls_name), False

    def _build_cache_request(self, automation: Any) -> Any:
        """Create an IUIAutomationCacheRequest with TreeScope_Element and needed props/patterns."""
        cache_req = automation.CreateCacheRequest()
        with contextlib.suppress(Exception):
            cache_req.TreeScope = TREE_SCOPE_ELEMENT
        with contextlib.suppress(Exception):
            cache_req.AutomationElementMode = AUTOMATION_ELEMENT_MODE_FULL
        for prop_id in (
            UIA_NAME_PROPERTY_ID,
            UIA_AUTOMATION_ID_PROPERTY_ID,
            UIA_CONTROL_TYPE_PROPERTY_ID,
            UIA_BOUNDING_RECTANGLE_PROPERTY_ID,
            UIA_IS_OFFSCREEN_PROPERTY_ID,
            UIA_IS_ENABLED_PROPERTY_ID,
            UIA_HAS_KEYBOARD_FOCUS_PROPERTY_ID,
            UIA_NATIVE_WINDOW_HANDLE_PROPERTY_ID,
            UIA_VALUE_VALUE_PROPERTY_ID,
            UIA_RANGE_VALUE_VALUE_PROPERTY_ID,
            UIA_TOGGLE_STATE_PROPERTY_ID,
            UIA_EXPAND_COLLAPSE_STATE_PROPERTY_ID,
            UIA_SELECTION_ITEM_IS_SELECTED_PROPERTY_ID,
        ):
            with contextlib.suppress(Exception):
                cache_req.AddProperty(prop_id)
        for pat_id in (UIA_VALUE_PATTERN_ID, UIA_RANGE_VALUE_PATTERN_ID):
            with contextlib.suppress(Exception):
                cache_req.AddPattern(pat_id)
        return cache_req

    def close(self) -> None:
        """Release all cached COM pointers on the MTA thread and shut it down cleanly."""
        with self._lifecycle_lock:
            if self._closed:
                return
            self._closed = True
            t = self._worker_thread
            if t is not None and t.is_alive():
                self._job_queue.put(None)
        if t is not None and t.is_alive() and threading.get_ident() != t.ident:
            t.join(timeout=2.0)

    # --------------------------------------------------------------- Live Element LRU Cache

    def _cache_live_element_on_worker(
        self,
        snapshot_id: str,
        ref: str,
        entry: _LiveElementEntry,
    ) -> None:
        key = (snapshot_id, ref)
        existing = self._live_elements.pop(key, None)
        if existing is not None and existing.element is not entry.element:
            _release_com_object(existing.element)
        self._live_elements[key] = entry
        while len(self._live_elements) > self._max_cached_elements:
            _, evicted = self._live_elements.popitem(last=False)
            self._evicted_count += 1
            _release_com_object(evicted.element)

    def _evict_live_element_on_worker(self, snapshot_id: str, ref: str) -> None:
        removed = self._live_elements.pop((snapshot_id, ref), None)
        if removed is not None:
            _release_com_object(removed.element)

    def _clear_live_cache_on_worker(self) -> None:
        while self._live_elements:
            _, entry = self._live_elements.popitem(last=False)
            _release_com_object(entry.element)

    def clear_cache(self) -> None:
        """Explicitly evict and release all cached live COM element handles."""
        if self._worker_thread is None or not self._worker_thread.is_alive() or self._closed:
            return
        with contextlib.suppress(Exception):
            self._submit(self._clear_live_cache_on_worker)

    def has_cached_element(self, snapshot_id: str, ref: str) -> bool:
        """Return True if ``(snapshot_id, ref)`` currently has a live COM handle cached."""
        if self._worker_thread is None or not self._worker_thread.is_alive() or self._closed:
            return False
        try:
            return bool(self._submit(lambda: (snapshot_id, ref) in self._live_elements))
        except Exception:
            return False

    # --------------------------------------------------------------- COM Element Helpers

    def _get_pattern(
        self,
        element: Any,
        pattern_id: int,
        interface_name: str,
        *,
        cached: bool = False,
    ) -> Any:
        """Fetch and QueryInterface a UIA pattern, returning None if unsupported."""
        assert self._ctx is not None
        if not _is_valid_com_obj(element):
            return None
        getter_name = "GetCachedPattern" if cached else "GetCurrentPattern"
        getter = getattr(element, getter_name, None)
        if not callable(getter):
            return None
        try:
            raw_pat = getter(pattern_id)
        except Exception as exc:
            if is_stale_com_error(exc):
                raise
            if _is_unsupported_pattern_error(exc):
                return None
            return None
        if not _is_valid_com_obj(raw_pat):
            return None
        iface = (
            getattr(self._ctx.uia_module, interface_name, None)
            if self._ctx.uia_module is not None
            else None
        )
        if iface is not None and hasattr(raw_pat, "QueryInterface"):
            try:
                typed_pat = raw_pat.QueryInterface(iface)
                if typed_pat is not raw_pat:
                    _release_com_object(raw_pat)
                return typed_pat if _is_valid_com_obj(typed_pat) else None
            except Exception as exc:
                _release_com_object(raw_pat)
                if is_stale_com_error(exc):
                    raise
                return None
        return raw_pat

    def _read_element_value(self, element: Any, *, prefer_cached: bool = True) -> str | None:
        """Read string value from ValuePattern or RangeValuePattern.

        When ``prefer_cached=True`` (used during snapshot tree walks), only cached properties
        and cached patterns are read so non-value controls never incur live cross-process COM
        IPC round-trips. Uses ``GetCachedPropertyValueEx(..., True)`` (``ignoreDefaultValue``)
        when available so controls without ValuePattern return ``None`` instead of ``""``.
        """
        assert self._ctx is not None
        if not _is_valid_com_obj(element):
            return None
        automation = self._ctx.automation

        if prefer_cached:
            prop_ex = getattr(element, "GetCachedPropertyValueEx", None)
            prop_plain = getattr(element, "GetCachedPropertyValue", None)
            if callable(prop_ex) or callable(prop_plain):
                for prop_id in (UIA_VALUE_VALUE_PROPERTY_ID, UIA_RANGE_VALUE_VALUE_PROPERTY_ID):
                    try:
                        raw = prop_ex(prop_id, True) if callable(prop_ex) else prop_plain(prop_id)
                        if not _is_not_supported_sentinel(raw, automation) and not isinstance(
                            raw, (ctypes.c_void_p, ctypes._Pointer)
                        ):
                            return str(raw)
                    except Exception as exc:
                        if is_stale_com_error(exc):
                            raise
        else:
            cur_prop_ex = getattr(element, "GetCurrentPropertyValueEx", None)
            if callable(cur_prop_ex):
                for prop_id in (UIA_VALUE_VALUE_PROPERTY_ID, UIA_RANGE_VALUE_VALUE_PROPERTY_ID):
                    try:
                        raw = cur_prop_ex(prop_id, True)
                        if not _is_not_supported_sentinel(raw, automation) and not isinstance(
                            raw, (ctypes.c_void_p, ctypes._Pointer)
                        ):
                            return str(raw)
                    except Exception as exc:
                        if is_stale_com_error(exc):
                            raise

        # Try ValuePattern (strictly cached when prefer_cached=True, live when False)
        vp = self._get_pattern(
            element, UIA_VALUE_PATTERN_ID, "IUIAutomationValuePattern", cached=prefer_cached
        )
        if vp is not None:
            try:
                val = _safe_prop(
                    vp,
                    "CachedValue" if prefer_cached else "CurrentValue",
                    "CurrentValue",
                    default=None,
                )
                if val is not None:
                    return str(val)
            finally:
                _release_com_object(vp)

        # Try RangeValuePattern (strictly cached when prefer_cached=True, live when False)
        rvp = self._get_pattern(
            element,
            UIA_RANGE_VALUE_PATTERN_ID,
            "IUIAutomationRangeValuePattern",
            cached=prefer_cached,
        )
        if rvp is not None:
            try:
                val = _safe_prop(
                    rvp,
                    "CachedValue" if prefer_cached else "CurrentValue",
                    "CurrentValue",
                    default=None,
                )
                if val is not None:
                    return str(val)
            finally:
                _release_com_object(rvp)

        return None

    def _extract_cached_node_info(self, element: Any) -> dict[str, Any]:
        """Extract cached properties from a walked UIA node."""
        raw_ct = _safe_prop(element, "CachedControlType", "CurrentControlType", default=0)
        control_type = _normalize_control_type_name(raw_ct)
        name = str(_safe_prop(element, "CachedName", "CurrentName", default="") or "")
        automation_id = str(
            _safe_prop(element, "CachedAutomationId", "CurrentAutomationId", default="") or ""
        )
        is_offscreen = bool(
            _safe_prop(element, "CachedIsOffscreen", "CurrentIsOffscreen", default=False)
        )
        is_enabled = bool(_safe_prop(element, "CachedIsEnabled", "CurrentIsEnabled", default=True))
        is_focused = bool(
            _safe_prop(
                element,
                "CachedHasKeyboardFocus",
                "CurrentHasKeyboardFocus",
                default=False,
            )
        )
        raw_rect = _safe_prop(
            element,
            "CachedBoundingRectangle",
            "CurrentBoundingRectangle",
            default=None,
        )
        return {
            "control_type": control_type,
            "name": name,
            "automation_id": automation_id,
            "is_offscreen": is_offscreen,
            "is_enabled": is_enabled,
            "is_focused": is_focused,
            "raw_rect": raw_rect,
        }

    def _resolve_window_element_on_worker(
        self,
        *,
        hwnd: int = 0,
        app_name: str | None = None,
    ) -> tuple[Any, str, int]:
        """Resolve root UIA window element by HWND, title substring, or focused window."""
        assert self._ctx is not None
        auto = self._ctx.automation
        walker = self._ctx.walker
        cache_req = self._ctx.cache_request

        win: Any = None
        if hwnd and hwnd > 0:
            try:
                candidate = (
                    auto.ElementFromHandleBuildCache(hwnd, cache_req)
                    if hasattr(auto, "ElementFromHandleBuildCache")
                    else auto.ElementFromHandle(hwnd)
                )
                win = candidate if _is_valid_com_obj(candidate) else None
            except Exception as exc:
                logger.debug("ElementFromHandle(%s) failed: %s", hwnd, exc)
                win = None

        if win is None and app_name:
            needle = app_name.strip().lower()
            root = auto.GetRootElement()
            if _is_valid_com_obj(root):
                try:
                    child = self._get_first_child(walker, root, cache_req)
                    while child is not None:
                        next_sib = self._get_next_sibling(walker, child, cache_req)
                        try:
                            ct = _normalize_control_type_name(
                                _safe_prop(
                                    child, "CachedControlType", "CurrentControlType", default=0
                                )
                            )
                            wname = str(
                                _safe_prop(child, "CachedName", "CurrentName", default="") or ""
                            )
                            if (ct == "Window" or not ct) and wname and needle in wname.lower():
                                win = child
                                _release_com_object(next_sib)
                                break
                        except Exception:
                            pass
                        _release_com_object(child)
                        child = next_sib
                finally:
                    _release_com_object(root)

        if win is None and not app_name and not (hwnd and hwnd > 0):
            root = auto.GetRootElement()
            if _is_valid_com_obj(root):
                try:
                    cur: Any = None
                    with contextlib.suppress(Exception):
                        focused = auto.GetFocusedElement()
                        if _is_valid_com_obj(focused):
                            cur = focused
                    if cur is not None:
                        is_desktop = False
                        with contextlib.suppress(Exception):
                            is_desktop = bool(auto.CompareElements(cur, root))
                        if is_desktop:
                            _release_com_object(cur)
                            cur = None
                    while cur is not None:
                        parent: Any = None
                        with contextlib.suppress(Exception):
                            raw_parent = walker.GetParentElement(cur)
                            if _is_valid_com_obj(raw_parent):
                                parent = raw_parent
                        if parent is None:
                            win = cur
                            break
                        is_root = False
                        with contextlib.suppress(Exception):
                            is_root = bool(auto.CompareElements(parent, root))
                        if is_root:
                            _release_com_object(parent)
                            win = cur
                            break
                        _release_com_object(cur)
                        cur = parent
                    if win is None:
                        win = self._get_first_child(walker, root, cache_req)
                finally:
                    _release_com_object(root)

        if not _is_valid_com_obj(win):
            return None, "", int(hwnd or 0)

        win_title = str(_safe_prop(win, "CachedName", "CurrentName", default="") or "")
        raw_win_hwnd = _safe_prop(
            win,
            "CachedNativeWindowHandle",
            "CurrentNativeWindowHandle",
            default=0,
        )
        resolved_hwnd = int(hwnd or 0) or (
            int(raw_win_hwnd)
            if isinstance(raw_win_hwnd, int) and not isinstance(raw_win_hwnd, bool)
            else 0
        )
        return win, win_title, resolved_hwnd

    def _get_first_child(self, walker: Any, node: Any, cache_req: Any) -> Any:
        if not _is_valid_com_obj(node):
            return None
        try:
            res = (
                walker.GetFirstChildElementBuildCache(node, cache_req)
                if hasattr(walker, "GetFirstChildElementBuildCache")
                else walker.GetFirstChildElement(node)
            )
            return res if _is_valid_com_obj(res) else None
        except Exception:
            return None

    def _get_next_sibling(self, walker: Any, node: Any, cache_req: Any) -> Any:
        if not _is_valid_com_obj(node):
            return None
        try:
            res = (
                walker.GetNextSiblingElementBuildCache(node, cache_req)
                if hasattr(walker, "GetNextSiblingElementBuildCache")
                else walker.GetNextSiblingElement(node)
            )
            return res if _is_valid_com_obj(res) else None
        except Exception:
            return None

    def _walk_tree_bfs_on_worker(
        self,
        root_el: Any,
        *,
        max_depth: int,
        max_nodes: int,
    ) -> list[tuple[Any, dict[str, Any]]]:
        """Bounded adaptive BFS over UIA ControlView skipping unlabeled Pane/Group wrappers.

        Unlabeled structural ``Pane`` / ``Group`` wrapper nodes are descended into without
        consuming ``max_nodes`` quota and are released immediately after enqueuing their children.
        Returned COM handles are owned by the caller (who must cache or release them).
        """
        assert self._ctx is not None
        if not _is_valid_com_obj(root_el):
            return []
        walker = self._ctx.walker
        cache_req = self._ctx.cache_request

        results: list[tuple[Any, dict[str, Any]]] = []
        bfs_queue: collections.deque[tuple[Any, int]] = collections.deque([(root_el, 0)])
        visited = 0
        raw_visits = 0
        max_raw_visits = max(max_nodes * 8, max_nodes + 256)

        try:
            while bfs_queue and visited < max_nodes and raw_visits < max_raw_visits:
                node, depth = bfs_queue.popleft()
                if not _is_valid_com_obj(node):
                    continue
                raw_visits += 1

                is_root = depth == 0
                keep_handle = False

                if not is_root:
                    try:
                        info = self._extract_cached_node_info(node)
                    except Exception:
                        _release_com_object(node)
                        continue

                    ct = info["control_type"]
                    is_unlabeled_wrapper = (
                        ct in _STRUCTURAL_WRAPPER_TYPES
                        and not info["name"].strip()
                        and not info["automation_id"].strip()
                    )
                    if not is_unlabeled_wrapper:
                        visited += 1
                        results.append((node, info))
                        keep_handle = True

                # Enqueue children if within depth budget and node quota remains
                if depth < max_depth and visited < max_nodes:
                    child = self._get_first_child(walker, node, cache_req)
                    while child is not None and (raw_visits + len(bfs_queue)) < max_raw_visits:
                        bfs_queue.append((child, depth + 1))
                        child = self._get_next_sibling(walker, child, cache_req)
                    if child is not None:
                        _release_com_object(child)

                if not is_root and not keep_handle:
                    _release_com_object(node)
        finally:
            while bfs_queue:
                leftover, leftover_depth = bfs_queue.popleft()
                if leftover_depth > 0:
                    _release_com_object(leftover)

        return results

    # --------------------------------------------------------------- High-Level Operations

    def snapshot(
        self,
        *,
        hwnd: int = 0,
        app_name: str | None = None,
        max_elements: int = 80,
        interactive_only: bool = False,
        snapshot_id: str | None = None,
        max_depth: int | None = None,
        max_nodes: int | None = None,
    ) -> dict[str, Any]:
        """Capture a UIA snapshot via in-process COM and cache live element handles."""
        eff_depth = self._max_depth if max_depth is None else max(1, int(max_depth))
        eff_nodes = self._max_nodes if max_nodes is None else max(1, int(max_nodes))
        eff_snap_id = snapshot_id or f"snap_{uuid.uuid4().hex[:12]}"

        def _op() -> dict[str, Any]:
            win, win_title, resolved_hwnd = self._resolve_window_element_on_worker(
                hwnd=hwnd, app_name=app_name
            )
            if win is None:
                coverage, content_count, guidance = classify_accessibility_coverage([])
                out_empty: dict[str, Any] = {
                    "ok": True,
                    "source": "uia_com",
                    "snapshot_id": eff_snap_id,
                    "window_title": win_title,
                    "hwnd": resolved_hwnd,
                    "count": 0,
                    "element_count": 0,
                    "elements": [],
                    "elements_by_ref": {},
                    "accessibility_coverage": coverage,
                    "content_element_count": content_count,
                }
                if guidance:
                    out_empty["hint"] = guidance
                return out_empty

            try:
                walked = self._walk_tree_bfs_on_worker(
                    win, max_depth=eff_depth, max_nodes=eff_nodes
                )
            finally:
                _release_com_object(win)

            llm_elements: list[dict[str, Any]] = []
            elements_by_ref: dict[str, dict[str, Any]] = {}

            for idx, (el_handle, info) in enumerate(walked):
                if len(llm_elements) >= max_elements:
                    # Release remaining unconsumed handles
                    for rest_el, _ in walked[idx:]:
                        _release_com_object(rest_el)
                    break

                ct = info["control_type"]
                if info["is_offscreen"] or ct not in _ALLOWED_SNAPSHOT_CONTROL_TYPES:
                    _release_com_object(el_handle)
                    continue

                rect_vals = _extract_rect(info["raw_rect"])
                if rect_vals is None:
                    _release_com_object(el_handle)
                    continue
                bx, by, bw, bh, cx, cy = rect_vals

                role = _control_type_to_role(ct)
                enabled = bool(info["is_enabled"])
                focused = bool(info["is_focused"])
                name = info["name"]
                automation_id = info["automation_id"]

                if role == "text":
                    if interactive_only:
                        _release_com_object(el_handle)
                        continue
                elif interactive_only and not enabled:
                    _release_com_object(el_handle)
                    continue

                try:
                    val = self._read_element_value(el_handle, prefer_cached=True)
                except Exception:
                    val = None

                if role == "text" and not name.strip() and (val is None or not val.strip()):
                    _release_com_object(el_handle)
                    continue

                ref = f"e{len(llm_elements) + 1}"
                bounds = {"x": bx, "y": by, "width": bw, "height": bh}
                center = {"cx": cx, "cy": cy}

                elements_by_ref[ref] = {
                    "ref": ref,
                    "role": role,
                    "name": name,
                    "value": val,
                    "enabled": enabled,
                    "focused": focused,
                    "bounds": bounds,
                    "center": center,
                    "x": cx,
                    "y": cy,
                    "automation_id": automation_id,
                    "control_type": ct,
                }
                llm_elements.append(
                    {
                        "ref": ref,
                        "role": role,
                        "name": name,
                        "value": val,
                        "enabled": enabled,
                        "focused": focused,
                    }
                )
                self._cache_live_element_on_worker(
                    eff_snap_id,
                    ref,
                    _LiveElementEntry(
                        element=el_handle,
                        hwnd=resolved_hwnd,
                        window_title=win_title,
                        role=role,
                        name=name,
                        control_type=ct,
                        automation_id=automation_id,
                        value=val,
                        enabled=enabled,
                        focused=focused,
                        bounds=bounds,
                        center=center,
                    ),
                )

            coverage, content_count, guidance = classify_accessibility_coverage(
                list(elements_by_ref.values())
            )
            result: dict[str, Any] = {
                "ok": True,
                "source": "uia_com",
                "snapshot_id": eff_snap_id,
                "window_title": win_title,
                "hwnd": resolved_hwnd,
                "count": len(llm_elements),
                "element_count": len(llm_elements),
                "elements": llm_elements,
                "elements_by_ref": elements_by_ref,
                "accessibility_coverage": coverage,
                "content_element_count": content_count,
            }
            if guidance:
                result["hint"] = guidance
            return result

        return self._submit(_op)

    def act(
        self,
        snapshot_id: str,
        ref: str,
        action: str,
        value: str | None = None,
    ) -> dict[str, Any]:
        """Dispatch a direct UIA pattern action on a cached live COM handle (<15 ms).

        Preserves Worktree 1's rule: ``ValuePattern`` is strictly replace-only
        (``action == 'set_value'``), never used for ``action == 'type'``.
        """
        norm_action = action.strip().lower()

        def _op() -> dict[str, Any]:
            key = (snapshot_id, ref)
            entry = self._live_elements.get(key)
            if entry is None:
                return {
                    "ok": False,
                    "found": False,
                    "acted": False,
                    "stale": False,
                    "cache_miss": True,
                    "method": "",
                }
            self._live_elements.move_to_end(key)
            el = entry.element
            cx = int(entry.center.get("cx", 0))
            cy = int(entry.center.get("cy", 0))

            # Never invoke ValuePattern for append-only 'type'
            if norm_action == "type":
                return {
                    "ok": True,
                    "found": True,
                    "acted": False,
                    "stale": False,
                    "method": "",
                    "cx": cx,
                    "cy": cy,
                }

            try:
                # Probe liveness / fresh bounding rectangle first if available
                raw_rect = _safe_prop(el, "CurrentBoundingRectangle", default=None)
                fresh_rect = _extract_rect(raw_rect)
                if fresh_rect is not None:
                    _, _, _, _, cx, cy = fresh_rect
                    entry.center = {"cx": cx, "cy": cy}

                if norm_action == "set_value":
                    vp = self._get_pattern(
                        el, UIA_VALUE_PATTERN_ID, "IUIAutomationValuePattern", cached=False
                    )
                    if vp is not None:
                        try:
                            is_ro = bool(_safe_prop(vp, "CurrentIsReadOnly", default=False))
                            if not is_ro:
                                vp.SetValue("" if value is None else str(value))
                                entry.value = "" if value is None else str(value)
                                return {
                                    "ok": True,
                                    "found": True,
                                    "acted": True,
                                    "stale": False,
                                    "method": "value_pattern",
                                    "cx": cx,
                                    "cy": cy,
                                }
                        finally:
                            _release_com_object(vp)
                    return {
                        "ok": True,
                        "found": True,
                        "acted": False,
                        "stale": False,
                        "method": "",
                        "cx": cx,
                        "cy": cy,
                    }

                if norm_action == "click":
                    method = self._dispatch_click_patterns_on_worker(el)
                    return {
                        "ok": True,
                        "found": True,
                        "acted": bool(method),
                        "stale": False,
                        "method": method,
                        "cx": cx,
                        "cy": cy,
                    }

                return {
                    "ok": True,
                    "found": True,
                    "acted": False,
                    "stale": False,
                    "method": "",
                    "cx": cx,
                    "cy": cy,
                }
            except Exception as exc:
                if is_stale_com_error(exc):
                    self._evict_live_element_on_worker(snapshot_id, ref)
                    hr_str = _format_hresult(_extract_hresult(exc))
                    return {
                        "ok": False,
                        "found": False,
                        "acted": False,
                        "stale": True,
                        "status": "stale_ref",
                        "reason": "stale_element",
                        "snapshot_id": snapshot_id,
                        "ref": ref,
                        "error": (
                            f"UI element ref {ref!r} from snapshot {snapshot_id} is no longer "
                            f"available (COM handle invalidated: {hr_str}). "
                            "Call ui_snapshot() to refresh element refs."
                        ),
                    }
                return {
                    "ok": True,
                    "found": True,
                    "acted": False,
                    "stale": False,
                    "method": "",
                    "cx": cx,
                    "cy": cy,
                }

        return self._submit(_op)

    def _dispatch_click_patterns_on_worker(self, el: Any) -> str:
        """Try Invoke -> SelectionItem -> Toggle -> ExpandCollapse patterns on a COM element."""
        # 1. InvokePattern
        inv = self._get_pattern(
            el, UIA_INVOKE_PATTERN_ID, "IUIAutomationInvokePattern", cached=False
        )
        if inv is not None:
            try:
                inv.Invoke()
                return "invoke"
            except Exception as exc:
                if is_stale_com_error(exc):
                    raise
            finally:
                _release_com_object(inv)

        # 2. SelectionItemPattern
        sel = self._get_pattern(
            el, UIA_SELECTION_ITEM_PATTERN_ID, "IUIAutomationSelectionItemPattern", cached=False
        )
        if sel is not None:
            try:
                sel.Select()
                return "select"
            except Exception as exc:
                if is_stale_com_error(exc):
                    raise
            finally:
                _release_com_object(sel)

        # 3. TogglePattern
        tog = self._get_pattern(
            el, UIA_TOGGLE_PATTERN_ID, "IUIAutomationTogglePattern", cached=False
        )
        if tog is not None:
            try:
                tog.Toggle()
                return "toggle"
            except Exception as exc:
                if is_stale_com_error(exc):
                    raise
            finally:
                _release_com_object(tog)

        # 4. ExpandCollapsePattern
        exp = self._get_pattern(
            el,
            UIA_EXPAND_COLLAPSE_PATTERN_ID,
            "IUIAutomationExpandCollapsePattern",
            cached=False,
        )
        if exp is not None:
            try:
                state = _safe_prop(
                    exp,
                    "CurrentExpandCollapseState",
                    default=EXPAND_COLLAPSE_STATE_COLLAPSED,
                )
                if int(state) == EXPAND_COLLAPSE_STATE_EXPANDED:
                    exp.Collapse()
                else:
                    exp.Expand()
                return "expand_collapse"
            except Exception as exc:
                if is_stale_com_error(exc):
                    raise
            finally:
                _release_com_object(exp)

        return ""

    def read_element_state(self, snapshot_id: str, ref: str) -> dict[str, Any]:
        """Read fresh state/value of a single cached element without walking the tree (<5 ms)."""

        def _op() -> dict[str, Any]:
            key = (snapshot_id, ref)
            entry = self._live_elements.get(key)
            if entry is None:
                return {"available": False, "cached": False}
            self._live_elements.move_to_end(key)
            el = entry.element
            try:
                name = str(_safe_prop(el, "CurrentName", default=entry.name) or "")
                enabled = bool(_safe_prop(el, "CurrentIsEnabled", default=entry.enabled))
                focused = bool(_safe_prop(el, "CurrentHasKeyboardFocus", default=entry.focused))
                val = self._read_element_value(el, prefer_cached=False)

                state_out: dict[str, Any] = {
                    "available": True,
                    "cached": True,
                    "ref": ref,
                    "role": entry.role,
                    "name": name,
                    "value": val,
                    "enabled": enabled,
                    "focused": focused,
                }

                # Optional pattern state readouts
                tog = self._get_pattern(
                    el, UIA_TOGGLE_PATTERN_ID, "IUIAutomationTogglePattern", cached=False
                )
                if tog is not None:
                    try:
                        t_state = _safe_prop(tog, "CurrentToggleState", default=None)
                        if isinstance(t_state, int):
                            state_out["toggle_state"] = _TOGGLE_STATE_NAMES.get(
                                t_state, str(t_state)
                            )
                    finally:
                        _release_com_object(tog)

                exp = self._get_pattern(
                    el,
                    UIA_EXPAND_COLLAPSE_PATTERN_ID,
                    "IUIAutomationExpandCollapsePattern",
                    cached=False,
                )
                if exp is not None:
                    try:
                        e_state = _safe_prop(exp, "CurrentExpandCollapseState", default=None)
                        if isinstance(e_state, int):
                            state_out["expand_collapse_state"] = _EXPAND_STATE_NAMES.get(
                                e_state, str(e_state)
                            )
                    finally:
                        _release_com_object(exp)

                sel = self._get_pattern(
                    el,
                    UIA_SELECTION_ITEM_PATTERN_ID,
                    "IUIAutomationSelectionItemPattern",
                    cached=False,
                )
                if sel is not None:
                    try:
                        is_sel = _safe_prop(sel, "CurrentIsSelected", default=None)
                        if is_sel is not None:
                            state_out["is_selected"] = bool(is_sel)
                    finally:
                        _release_com_object(sel)

                entry.name = name
                entry.value = val
                entry.enabled = enabled
                entry.focused = focused
                return state_out
            except Exception as exc:
                if is_stale_com_error(exc):
                    self._evict_live_element_on_worker(snapshot_id, ref)
                    return {"available": False, "cached": True, "stale": True}
                return {
                    "available": False,
                    "cached": True,
                    "name": entry.name,
                    "value": entry.value,
                    "enabled": entry.enabled,
                    "focused": entry.focused,
                }

        return self._submit(_op)

    def inspect_elements(
        self,
        *,
        hwnd: int = 0,
        app_name: str | None = None,
        max_elements: int = 60,
        max_depth: int | None = None,
        max_nodes: int | None = None,
    ) -> dict[str, Any]:
        """Inspect visible UIA controls without retaining live COM handles."""
        eff_depth = self._max_depth if max_depth is None else max(1, int(max_depth))
        eff_nodes = self._max_nodes if max_nodes is None else max(1, int(max_nodes))

        def _op() -> dict[str, Any]:
            win, win_title, _ = self._resolve_window_element_on_worker(
                hwnd=hwnd, app_name=app_name
            )
            if win is None:
                coverage, content_count, guidance = classify_accessibility_coverage([])
                empty_res: dict[str, Any] = {
                    "ok": True,
                    "source": "uia_com",
                    "window_title": win_title,
                    "count": 0,
                    "elements": [],
                    "accessibility_coverage": coverage,
                    "content_element_count": content_count,
                }
                if guidance:
                    empty_res["hint"] = guidance
                return empty_res

            try:
                walked = self._walk_tree_bfs_on_worker(
                    win, max_depth=eff_depth, max_nodes=eff_nodes
                )
            finally:
                _release_com_object(win)

            elements: list[dict[str, Any]] = []
            try:
                for el_handle, info in walked:
                    if len(elements) >= max_elements:
                        continue
                    ct = info["control_type"]
                    if info["is_offscreen"] or ct not in _ALLOWED_SNAPSHOT_CONTROL_TYPES:
                        continue
                    rect_vals = _extract_rect(info["raw_rect"])
                    if rect_vals is None:
                        continue
                    _, _, _, _, cx, cy = rect_vals
                    name = info["name"]
                    aid = info["automation_id"]
                    try:
                        val = self._read_element_value(el_handle, prefer_cached=True)
                    except Exception:
                        val = None
                    if ct == "Text" and not name.strip() and not aid.strip() and not val:
                        continue
                    elements.append(
                        {
                            "index": len(elements),
                            "name": name,
                            "automation_id": aid,
                            "control_type": ct,
                            "value": val,
                            "x": cx,
                            "y": cy,
                        }
                    )
            finally:
                for el_handle, _ in walked:
                    _release_com_object(el_handle)

            coverage, content_count, guidance = classify_accessibility_coverage(elements)
            out: dict[str, Any] = {
                "ok": True,
                "source": "uia_com",
                "window_title": win_title,
                "count": len(elements),
                "elements": elements,
                "accessibility_coverage": coverage,
                "content_element_count": content_count,
            }
            if guidance:
                out["hint"] = guidance
            return out

        return self._submit(_op)

    def click_element(
        self,
        name: str,
        *,
        control_type: str | None = None,
        hwnd: int = 0,
        app_name: str | None = None,
        max_depth: int | None = None,
        max_nodes: int | None = None,
    ) -> dict[str, Any]:
        """Locate a control by name/automation_id and invoke via UIA pattern if supported."""
        eff_depth = self._max_depth if max_depth is None else max(1, int(max_depth))
        eff_nodes = self._max_nodes if max_nodes is None else max(1, int(max_nodes))
        target_lower = name.strip().lower()
        ct_filter = control_type.strip().lower() if control_type else None

        def _op() -> dict[str, Any]:
            win, _win_title, _ = self._resolve_window_element_on_worker(
                hwnd=hwnd, app_name=app_name
            )
            if win is None:
                return {"found": False, "invoked": False, "available": []}

            try:
                walked = self._walk_tree_bfs_on_worker(
                    win, max_depth=eff_depth, max_nodes=eff_nodes
                )
            finally:
                _release_com_object(win)

            exact_match: tuple[Any, dict[str, Any], tuple[int, int, int, int, int, int]] | None = (
                None
            )
            exact_is_text = False
            sub_match: tuple[Any, dict[str, Any], tuple[int, int, int, int, int, int]] | None = None
            sub_is_text = False
            aid_match: tuple[Any, dict[str, Any], tuple[int, int, int, int, int, int]] | None = None
            available: list[str] = []

            try:
                for el_handle, info in walked:
                    if info["is_offscreen"]:
                        continue
                    ct = info["control_type"]
                    if ct_filter:
                        if ct.lower() != ct_filter:
                            continue
                    elif ct not in _ALLOWED_SNAPSHOT_CONTROL_TYPES:
                        continue
                    rect_vals = _extract_rect(info["raw_rect"])
                    if rect_vals is None:
                        continue

                    el_name = info["name"]
                    el_aid = info["automation_id"]
                    if el_name.strip() and len(available) < 40 and el_name not in available:
                        available.append(el_name)

                    is_text = ct == "Text"
                    if el_name.strip().lower() == target_lower:
                        if exact_match is None or (exact_is_text and not is_text):
                            exact_match = (el_handle, info, rect_vals)
                            exact_is_text = is_text
                    elif el_name and target_lower in el_name.lower():
                        if sub_match is None or (sub_is_text and not is_text):
                            sub_match = (el_handle, info, rect_vals)
                            sub_is_text = is_text
                    elif aid_match is None and el_aid and el_aid.strip().lower() == target_lower:
                        aid_match = (el_handle, info, rect_vals)

                chosen = exact_match or sub_match or aid_match
                if chosen is None:
                    return {"found": False, "invoked": False, "available": available}

                el_handle, info, rect_vals = chosen
                _, _, _, _, cx, cy = rect_vals
                method = ""
                try:
                    method = self._dispatch_click_patterns_on_worker(el_handle)
                except Exception:
                    method = ""

                matched_name = info["name"] or info["automation_id"] or name
                return {
                    "found": True,
                    "invoked": bool(method),
                    "method": method or "click",
                    "name": matched_name,
                    "control_type": info["control_type"] or (control_type or ""),
                    "x": cx,
                    "y": cy,
                }
            finally:
                for el_handle, _ in walked:
                    _release_com_object(el_handle)

        return self._submit(_op)

    def probe_text(
        self,
        hwnd: int,
        *,
        max_chars: int = 3000,
        max_depth: int = 6,
        max_nodes: int = 120,
    ) -> str:
        """Fast in-process UIA text probe extracting readable Text/Edit/Document lines."""
        if not hwnd or hwnd <= 0 or max_chars <= 0:
            return ""

        def _op() -> str:
            win, _, _ = self._resolve_window_element_on_worker(hwnd=hwnd)
            if win is None:
                return ""
            try:
                walked = self._walk_tree_bfs_on_worker(
                    win, max_depth=max(1, int(max_depth)), max_nodes=max(1, int(max_nodes))
                )
            finally:
                _release_com_object(win)

            lines: list[str] = []
            seen: set[str] = set()
            total_len = 0
            try:
                for el_handle, info in walked:
                    if len(lines) >= 40 or total_len >= max_chars:
                        continue
                    if info["control_type"] not in _PROBE_TEXT_CONTROL_TYPES:
                        continue
                    text_val = info["name"].strip()
                    if not text_val:
                        with contextlib.suppress(Exception):
                            v = self._read_element_value(el_handle, prefer_cached=True)
                            if v:
                                text_val = v.strip()
                    if text_val and text_val not in seen:
                        seen.add(text_val)
                        lines.append(text_val)
                        total_len += len(text_val) + 1
            finally:
                for el_handle, _ in walked:
                    _release_com_object(el_handle)

            return "\n".join(lines)[:max_chars]

        return str(self._submit(_op))


# --------------------------------------------------------------------------- Singleton Accessors

_DEFAULT_UIA_ENGINE_LOCK = threading.Lock()
_DEFAULT_UIA_ENGINE: NativeUiaComEngine | None = None
_EXPLICIT_DEFAULT_UIA_ENGINE: bool = False


def get_default_uia_com_engine() -> NativeUiaComEngine:
    """Return the process-wide default NativeUiaComEngine singleton."""
    global _DEFAULT_UIA_ENGINE
    with _DEFAULT_UIA_ENGINE_LOCK:
        if _DEFAULT_UIA_ENGINE is None or _DEFAULT_UIA_ENGINE._closed:
            _DEFAULT_UIA_ENGINE = NativeUiaComEngine()
        return _DEFAULT_UIA_ENGINE


def set_default_uia_com_engine(engine: NativeUiaComEngine | None) -> None:
    """Override or reset the process-wide default NativeUiaComEngine (for DI/tests)."""
    global _DEFAULT_UIA_ENGINE, _EXPLICIT_DEFAULT_UIA_ENGINE
    prev: NativeUiaComEngine | None = None
    with _DEFAULT_UIA_ENGINE_LOCK:
        prev = _DEFAULT_UIA_ENGINE
        _DEFAULT_UIA_ENGINE = engine
        _EXPLICIT_DEFAULT_UIA_ENGINE = engine is not None
    if prev is not None and prev is not engine:
        with contextlib.suppress(Exception):
            prev.close()


def has_explicit_default_uia_com_engine() -> bool:
    """Return True if an explicit default UIA COM engine has been injected."""
    with _DEFAULT_UIA_ENGINE_LOCK:
        return _EXPLICIT_DEFAULT_UIA_ENGINE


def close_default_uia_com_engine() -> None:
    """Gracefully shut down the process-wide default NativeUiaComEngine if active."""
    global _DEFAULT_UIA_ENGINE, _EXPLICIT_DEFAULT_UIA_ENGINE
    with _DEFAULT_UIA_ENGINE_LOCK:
        prev = _DEFAULT_UIA_ENGINE
        _DEFAULT_UIA_ENGINE = None
        _EXPLICIT_DEFAULT_UIA_ENGINE = False
    if prev is not None:
        with contextlib.suppress(Exception):
            prev.close()

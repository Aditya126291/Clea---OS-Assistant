"""In-app view-change fingerprinting via 256-bit perceptual dHash + bounded UIA headings.

Provides zero-LLM, sub-20 ms deterministic visual and structural view-change detection
for Electron, Chromium, WinUI, and native Windows desktop applications where the top-level
HWND and window title remain constant across in-app navigation (e.g., Settings sub-pages,
media library views, sidebar tabs).
"""

from __future__ import annotations

import contextlib
import ctypes
import io
import logging
import sys
import threading
import time
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any, Literal

from PIL import Image

from clea.surfaces.voice.win32 import get_window_bounds, get_window_process_name
from clea.tools.cdp_bridge import is_chromium_process
from clea.tools.uia_com import (
    get_default_uia_com_engine,
    has_explicit_default_uia_com_engine,
)

logger = logging.getLogger(__name__)

DEFAULT_DHASH_THRESHOLD: float = 0.04
_DHASH_WIDTH: int = 16
_DHASH_HEIGHT: int = 16
_DHASH_BITS: int = _DHASH_WIDTH * _DHASH_HEIGHT
_DHASH_MASK: int = (1 << _DHASH_BITS) - 1

FingerprintSource = Literal["mss", "print_window", "injected", "unavailable"]


@dataclass(frozen=True)
class WindowViewFingerprint:
    """Compact visual + structural view snapshot for a single window."""

    hwnd: int | None
    dhash_hex: str
    heading: str = ""
    captured_at: float = 0.0
    source: FingerprintSource | str = "unavailable"

    def to_dict(self) -> dict[str, Any]:
        return {
            "hwnd": self.hwnd,
            "dhash_hex": self.dhash_hex,
            "dhash": self.dhash_hex,
            "heading": self.heading,
            "captured_at": self.captured_at,
            "source": str(self.source),
        }


def _coerce_pil_image(image: Any) -> Image.Image:
    """Coerce a PIL Image or encoded image bytes into a PIL Image."""
    if isinstance(image, Image.Image):
        return image
    if isinstance(image, (bytes, bytearray, memoryview)):
        return Image.open(io.BytesIO(bytes(image)))
    raise TypeError(f"Expected PIL.Image.Image or image bytes, got {type(image).__name__}.")


def compute_dhash_256(image: Any) -> int:
    """Compute a 16x16 horizontal-gradient 256-bit difference hash (dHash).

    Resizes the image to grayscale 17x16 and compares each pixel against its
    left neighbor (256 comparisons total). Runs in ~2-15 ms using Pillow alone.
    """
    pil_img = _coerce_pil_image(image)
    resample = getattr(getattr(Image, "Resampling", Image), "BILINEAR", 2)
    gray = pil_img.convert("L").resize((_DHASH_WIDTH + 1, _DHASH_HEIGHT), resample=resample)
    pixels = gray.tobytes()

    bits = 0
    stride = _DHASH_WIDTH + 1
    bit_index = 0
    for row in range(_DHASH_HEIGHT):
        row_offset = row * stride
        for col in range(_DHASH_WIDTH):
            if pixels[row_offset + col + 1] > pixels[row_offset + col]:
                bits |= 1 << bit_index
            bit_index += 1
    return bits


def dhash_to_hex(val: int) -> str:
    """Format a 256-bit dHash integer as an exact 64-character lowercase hex string."""
    if isinstance(val, bool) or not isinstance(val, int):
        raise TypeError(f"Expected integer dHash value, got {type(val).__name__}.")
    return f"{val & _DHASH_MASK:064x}"


def _parse_dhash_int(value: int | str) -> int:
    if isinstance(value, bool):
        raise ValueError("Boolean is not a valid dHash value.")
    if isinstance(value, int):
        return value & _DHASH_MASK
    if isinstance(value, str):
        cleaned = value.strip().lower()
        if cleaned.startswith("0x"):
            cleaned = cleaned[2:]
        if not cleaned:
            raise ValueError("Empty dHash hex string.")
        return int(cleaned, 16) & _DHASH_MASK
    raise TypeError(f"Unsupported dHash type: {type(value).__name__}.")


def normalized_hamming_distance(a: int | str, b: int | str) -> float:
    """Return the normalized Hamming distance in ``[0.0, 1.0]`` between two 256-bit dHashes."""
    ia = _parse_dhash_int(a)
    ib = _parse_dhash_int(b)
    return float((ia ^ ib).bit_count()) / float(_DHASH_BITS)


def _normalize_heading_text(raw: Any, *, max_chars: int = 200) -> str:
    """Normalize raw heading probe output into a compact deterministic signature."""
    if raw is None:
        return ""
    if isinstance(raw, dict):
        if str(raw.get("accessibility_coverage") or "").strip().lower() == "chrome_only":
            return ""
        for key in ("heading", "heading_signature", "text"):
            val = raw.get(key)
            if isinstance(val, str) and val.strip():
                return _normalize_heading_text(val, max_chars=max_chars)
        elements = raw.get("elements")
        if isinstance(elements, list):
            parts: list[str] = []
            for item in elements:
                if not isinstance(item, dict):
                    continue
                role = str(item.get("role") or item.get("control_type") or "").strip().lower()
                name = str(item.get("name") or "").strip()
                if not name:
                    continue
                if role in {"text", "heading", "tab", "tabitem", "document"}:
                    if name not in parts:
                        parts.append(name)
                if len(parts) >= 3:
                    break
            return " | ".join(parts)[:max_chars]
        return ""
    if isinstance(raw, (list, tuple)):
        parts = [str(item).strip() for item in raw if str(item).strip()]
        deduped = list(dict.fromkeys(parts))[:3]
        return " | ".join(deduped)[:max_chars]
    text = str(raw).strip()
    if not text:
        return ""
    lines = [" ".join(line.split()) for line in text.splitlines() if line.strip()]
    deduped_lines = list(dict.fromkeys(lines))[:3]
    return " | ".join(deduped_lines)[:max_chars]


def _is_module_available(module_name: str) -> bool:
    import importlib.util

    try:
        return importlib.util.find_spec(module_name) is not None
    except Exception:
        return False


def extract_uia_heading_signature(
    hwnd: int | None = None,
    *,
    max_depth: int = 4,
    max_nodes: int = 40,
    uia_probe_fn: Callable[..., Any] | None = None,
    accessibility_coverage: str | None = None,
) -> str:
    """Extract a shallow, bounded UIA heading signature for *hwnd* without raising.

    Skips UIA traversal completely when ``accessibility_coverage == "chrome_only"``
    or when the target window belongs to a Chromium process.
    """
    if str(accessibility_coverage or "").strip().lower() == "chrome_only":
        return ""

    if uia_probe_fn is not None:
        try:
            try:
                raw = uia_probe_fn(hwnd, max_depth=max_depth, max_nodes=max_nodes)
            except TypeError:
                try:
                    raw = uia_probe_fn(hwnd)
                except TypeError:
                    raw = uia_probe_fn()
            return _normalize_heading_text(raw)
        except Exception as exc:
            logger.debug("Injected uia_probe_fn failed for hwnd=%s: %s", hwnd, exc)
            return ""

    if not hwnd or isinstance(hwnd, bool) or int(hwnd) <= 0:
        return ""

    target_hwnd = int(hwnd)
    with contextlib.suppress(Exception):
        proc_name = get_window_process_name(target_hwnd)
        if proc_name and is_chromium_process(proc_name):
            return ""

    if not has_explicit_default_uia_com_engine() and not (
        sys.platform == "win32" and _is_module_available("comtypes")
    ):
        return ""

    try:
        engine = get_default_uia_com_engine()
        if not engine.is_available():
            return ""
        raw_text = engine.probe_text(
            target_hwnd,
            max_chars=240,
            max_depth=max(1, int(max_depth)),
            max_nodes=max(1, int(max_nodes)),
        )
        return _normalize_heading_text(raw_text)
    except Exception as exc:
        logger.debug("UIA heading probe failed for hwnd=%s: %s", target_hwnd, exc)
        return ""


def _resolve_live_foreground_hwnd(focus_backend: Any = None) -> int | None:
    if focus_backend is not None:
        with contextlib.suppress(Exception):
            if hasattr(focus_backend, "get_foreground_hwnd"):
                fg = focus_backend.get_foreground_hwnd()
                if isinstance(fg, int) and not isinstance(fg, bool) and fg > 0:
                    return int(fg)
            if hasattr(focus_backend, "get_foreground_context"):
                ctx = focus_backend.get_foreground_context()
                if ctx is not None and getattr(ctx, "hwnd", 0):
                    return int(ctx.hwnd)
    if sys.platform != "win32":
        return None
    with contextlib.suppress(Exception):
        user32 = ctypes.WinDLL("user32", use_last_error=True)
        user32.GetForegroundWindow.argtypes = []
        user32.GetForegroundWindow.restype = ctypes.c_void_p
        fg = user32.GetForegroundWindow()
        if isinstance(fg, int) and not isinstance(fg, bool) and fg > 0:
            return int(fg)
    return None


def _is_window_foreground_unoccluded(hwnd: int, focus_backend: Any = None) -> bool:
    fg = _resolve_live_foreground_hwnd(focus_backend=focus_backend)
    if fg is None or fg <= 0:
        return False
    if fg == hwnd:
        return True
    if focus_backend is not None and hasattr(focus_backend, "is_same_window_tree"):
        with contextlib.suppress(Exception):
            return bool(focus_backend.is_same_window_tree(hwnd, fg))
    return False


def _capture_region_mss(bounds: tuple[int, int, int, int]) -> Image.Image | None:
    left, top, right, bottom = bounds
    width = int(right) - int(left)
    height = int(bottom) - int(top)
    if width <= 0 or height <= 0:
        return None
    try:
        import mss  # type: ignore[import-not-found]

        with mss.mss() as sct:
            raw = sct.grab({"left": int(left), "top": int(top), "width": width, "height": height})
            return Image.frombytes("RGB", raw.size, raw.bgra, "raw", "BGRX")
    except Exception as exc:
        logger.debug("mss capture failed for bounds=%s: %s", bounds, exc)
        return None


def _capture_via_print_window(hwnd: int) -> Image.Image | None:
    try:
        from clea.tools.vision import Win32ScreenVisionBackend

        backend = Win32ScreenVisionBackend()
        cap = backend.capture_window_png(int(hwnd))
        if cap is not None and getattr(cap, "png_bytes", None):
            return Image.open(io.BytesIO(cap.png_bytes))
    except Exception as exc:
        logger.debug("PrintWindow fallback capture failed for hwnd=%s: %s", hwnd, exc)
    return None


def capture_window_view_fingerprint(
    hwnd: int | None = None,
    *,
    bounds: tuple[int, int, int, int] | None = None,
    include_heading: bool = True,
    accessibility_coverage: str | None = None,
    capture_fn: Callable[..., Any] | None = None,
    heading_fn: Callable[..., Any] | None = None,
    focus_backend: Any = None,
) -> WindowViewFingerprint | None:
    """Capture a compact visual dHash + optional shallow UIA heading for *hwnd*.

    Uses fast ``mss`` grab on DWM-cropped bounds when *hwnd* is in the foreground,
    falling back to Win32 ``PrintWindow`` via ``vision.py`` when occluded/background.
    Never raises.
    """
    try:
        target_hwnd = (
            int(hwnd)
            if isinstance(hwnd, int) and not isinstance(hwnd, bool) and hwnd > 0
            else _resolve_live_foreground_hwnd(focus_backend=focus_backend)
        )
        now = time.monotonic()

        def _get_heading(resolved_hwnd: int | None) -> str:
            if not include_heading:
                return ""
            if heading_fn is not None:
                return extract_uia_heading_signature(
                    resolved_hwnd,
                    uia_probe_fn=heading_fn,
                    accessibility_coverage=accessibility_coverage,
                )
            return extract_uia_heading_signature(
                resolved_hwnd,
                accessibility_coverage=accessibility_coverage,
            )

        if capture_fn is not None:
            try:
                raw_cap = capture_fn(target_hwnd)
            except TypeError:
                raw_cap = capture_fn()
            if raw_cap is None:
                return None
            if isinstance(raw_cap, WindowViewFingerprint):
                if include_heading and not raw_cap.heading and heading_fn is not None:
                    heading = _get_heading(raw_cap.hwnd or target_hwnd)
                    return WindowViewFingerprint(
                        hwnd=raw_cap.hwnd if raw_cap.hwnd is not None else target_hwnd,
                        dhash_hex=raw_cap.dhash_hex,
                        heading=heading,
                        captured_at=raw_cap.captured_at or now,
                        source=raw_cap.source,
                    )
                return raw_cap

            source: str = "injected"
            img_obj = raw_cap
            if isinstance(raw_cap, tuple) and len(raw_cap) == 2:
                img_obj, raw_src = raw_cap
                if isinstance(raw_src, str) and raw_src.strip():
                    source = raw_src.strip()

            dhash_hex = dhash_to_hex(compute_dhash_256(img_obj))
            heading = _get_heading(target_hwnd)
            return WindowViewFingerprint(
                hwnd=target_hwnd,
                dhash_hex=dhash_hex,
                heading=heading,
                captured_at=now,
                source=source,
            )

        if target_hwnd is None or target_hwnd <= 0:
            return None

        resolved_bounds = bounds
        if resolved_bounds is None and focus_backend is not None:
            with contextlib.suppress(Exception):
                resolved_bounds = focus_backend.get_extended_frame_bounds(target_hwnd)
        if resolved_bounds is None and sys.platform == "win32":
            with contextlib.suppress(Exception):
                resolved_bounds = get_window_bounds(target_hwnd)

        img: Image.Image | None = None
        cap_source: FingerprintSource = "unavailable"

        if resolved_bounds is not None and _is_window_foreground_unoccluded(
            target_hwnd, focus_backend=focus_backend
        ):
            img = _capture_region_mss(resolved_bounds)
            if img is not None:
                cap_source = "mss"

        if img is None and sys.platform == "win32":
            img = _capture_via_print_window(target_hwnd)
            if img is not None:
                cap_source = "print_window"

        heading = _get_heading(target_hwnd)
        if img is None:
            if heading:
                return WindowViewFingerprint(
                    hwnd=target_hwnd,
                    dhash_hex="",
                    heading=heading,
                    captured_at=now,
                    source="unavailable",
                )
            return None

        dhash_hex = dhash_to_hex(compute_dhash_256(img))
        return WindowViewFingerprint(
            hwnd=target_hwnd,
            dhash_hex=dhash_hex,
            heading=heading,
            captured_at=now,
            source=cap_source,
        )
    except Exception as exc:
        logger.debug("capture_window_view_fingerprint failed: %s", exc)
        return None


def compare_view_fingerprints(
    before: WindowViewFingerprint | None,
    after: WindowViewFingerprint | None,
    *,
    threshold: float = DEFAULT_DHASH_THRESHOLD,
) -> dict[str, Any]:
    """Compare before/after view fingerprints and report visual + heading deltas."""
    heading_before = (before.heading or "").strip() if before is not None else ""
    heading_after = (after.heading or "").strip() if after is not None else ""
    heading_changed = bool(heading_before or heading_after) and (heading_before != heading_after)

    visual_delta = 0.0
    if (
        before is not None
        and after is not None
        and bool(before.dhash_hex)
        and bool(after.dhash_hex)
    ):
        with contextlib.suppress(ValueError, TypeError):
            visual_delta = round(
                normalized_hamming_distance(before.dhash_hex, after.dhash_hex),
                6,
            )

    view_changed = bool((visual_delta >= float(threshold)) or heading_changed)
    return {
        "view_changed": view_changed,
        "visual_delta": visual_delta,
        "heading_before": heading_before,
        "heading_after": heading_after,
        "heading_changed": heading_changed,
    }


def cancellable_settle_wait(
    seconds: float = 0.2,
    *,
    cancel_event: threading.Event | None = None,
    sleep_fn: Callable[[float], None] | None = None,
) -> bool:
    """Wait briefly for async UI navigation to settle; return False if cancelled."""
    duration = max(0.0, float(seconds))
    if cancel_event is not None and cancel_event.is_set():
        return False
    if duration <= 0.0:
        return True

    if cancel_event is not None and sleep_fn is None:
        return not cancel_event.wait(timeout=duration)

    sleeper = sleep_fn or time.sleep
    if cancel_event is None:
        sleeper(duration)
        return True

    remaining = duration
    slice_s = 0.05
    while remaining > 0.0:
        if cancel_event.is_set():
            return False
        step = min(slice_s, remaining)
        sleeper(step)
        remaining -= step
    return not cancel_event.is_set()


__all__ = [
    "DEFAULT_DHASH_THRESHOLD",
    "FingerprintSource",
    "WindowViewFingerprint",
    "cancellable_settle_wait",
    "capture_window_view_fingerprint",
    "compare_view_fingerprints",
    "compute_dhash_256",
    "dhash_to_hex",
    "extract_uia_heading_signature",
    "normalized_hamming_distance",
]

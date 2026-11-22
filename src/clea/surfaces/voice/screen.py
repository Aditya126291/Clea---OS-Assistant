"""Screen capture and frame encoding service for live voice vision.

Provides per-monitor DPI-aware capture of the active window (1:1 native pixel
resolution) and active display monitor using ``mss`` / Pillow and Win32,
enriches every frame with ground-truth foreground window and process metadata,
enforces ScreenBlocklistConfig, and encodes frames without stale frame caching.
"""

from __future__ import annotations

import contextlib
import fnmatch
import io
import logging
import sys
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import Any

from PIL import Image

from clea.surfaces.voice.config import ScreenBlocklistConfig
from clea.surfaces.voice.win32 import (
    MonitorInfo,
    Win32API,
    WindowInfo,
    enable_per_monitor_dpi_awareness,
    enumerate_monitors,
    format_window_geometry_z_order,
    get_monitor_for_window,
    get_target_monitor,
    get_window_bounds,
    get_window_process_name,
    get_window_title,
    inspect_window_layout_win32,
    resolve_app_display_name,
)

logger = logging.getLogger(__name__)

NATIVE_PNG_MAX_BYTES = 2_500_000


@dataclass(frozen=True)
class ScreenCaptureResult:
    """Outcome of a screen capture operation."""

    image_bytes: bytes | None
    target: str
    width: int
    height: int
    window_title: str | None = None
    process_name: str | None = None
    blocked: bool = False
    error: str | None = None
    mime_type: str = "image/jpeg"
    app_name: str | None = None
    window_bounds: tuple[int, int, int, int] | None = None
    monitor_index: int | None = None
    monitor_bounds: tuple[int, int, int, int] | None = None
    captured_at: str | None = None
    capture_seq: int = 0
    window_layout: list[dict[str, Any]] | None = None
    window_geometry_summary: str | None = None


def is_window_blocked(
    title: str,
    process_name: str,
    blocklist: ScreenBlocklistConfig,
) -> bool:
    """Return True if title or process matches any configured blocklist pattern."""
    t_lower = (title or "").lower().strip()
    p_lower = (process_name or "").lower().strip()

    for pat in blocklist.window_titles:
        p = pat.lower().strip()
        if p and (p in t_lower or fnmatch.fnmatch(t_lower, p)):
            return True

    for pat in blocklist.processes:
        p = pat.lower().strip()
        if p and (p in p_lower or fnmatch.fnmatch(p_lower, p)):
            return True

    return False


def _clip_bounds_to_monitors(
    bounds: tuple[int, int, int, int],
    monitors: Sequence[MonitorInfo],
    fallback_bounds: tuple[int, int, int, int] | None,
) -> tuple[int, int, int, int] | None:
    """Clip window bounds to overlapping monitor(s) so off-screen edges capture no dead void."""
    left, top, right, bottom = bounds
    if right <= left or bottom <= top:
        return fallback_bounds

    if monitors:
        overlapping = [
            m
            for m in monitors
            if min(right, m.right) > max(left, m.left) and min(bottom, m.bottom) > max(top, m.top)
        ]
        if not overlapping:
            return fallback_bounds
        area_left = min(m.left for m in overlapping)
        area_top = min(m.top for m in overlapping)
        area_right = max(m.right for m in overlapping)
        area_bottom = max(m.bottom for m in overlapping)
        clipped = (
            max(left, area_left),
            max(top, area_top),
            min(right, area_right),
            min(bottom, area_bottom),
        )
        return clipped if clipped[2] > clipped[0] and clipped[3] > clipped[1] else fallback_bounds

    if fallback_bounds is not None:
        fb_left, fb_top, fb_right, fb_bottom = fallback_bounds
        if min(right, fb_right) > max(left, fb_left) and min(bottom, fb_bottom) > max(top, fb_top):
            clipped = (
                max(left, fb_left),
                max(top, fb_top),
                min(right, fb_right),
                min(bottom, fb_bottom),
            )
            if clipped[2] > clipped[0] and clipped[3] > clipped[1]:
                return clipped
        return fallback_bounds

    return bounds


def encode_frame(
    image: Image.Image,
    *,
    max_side: int | None = 1920,
    quality: int = 90,
) -> bytes:
    """Convert image to RGB, optionally downscale to max_side, and encode to JPEG bytes."""
    img = image
    if img.mode != "RGB":
        img = img.convert("RGB")

    w, h = img.size
    longest = max(w, h)
    if max_side is not None and max_side > 0 and longest > max_side and longest > 0:
        scale = max_side / float(longest)
        new_w = max(1, int(round(w * scale)))
        new_h = max(1, int(round(h * scale)))
        resample = getattr(Image, "Resampling", Image).LANCZOS
        img = img.resize((new_w, new_h), resample=resample)

    buf = io.BytesIO()
    save_kwargs: dict[str, object] = {"format": "JPEG", "quality": quality}
    if quality >= 90:
        # Disable chroma subsampling so fine colored IDE text and borders stay crisp.
        save_kwargs["subsampling"] = 0
    img.save(buf, **save_kwargs)
    return buf.getvalue()


def encode_native_frame(
    image: Image.Image,
    *,
    fallback_jpeg_quality: int = 94,
    max_png_bytes: int = NATIVE_PNG_MAX_BYTES,
) -> tuple[bytes, str]:
    """Encode an un-downscaled 1:1 native frame as lossless PNG (with JPEG fallback if huge)."""
    img = image
    if img.mode != "RGB":
        img = img.convert("RGB")

    png_buf = io.BytesIO()
    img.save(png_buf, format="PNG", compress_level=1)
    png_bytes = png_buf.getvalue()
    if len(png_bytes) <= max_png_bytes:
        return png_bytes, "image/png"

    jpeg_buf = io.BytesIO()
    img.save(
        jpeg_buf,
        format="JPEG",
        quality=fallback_jpeg_quality,
        subsampling=0,
    )
    return jpeg_buf.getvalue(), "image/jpeg"


class ScreenCaptureService:
    """Service capturing fresh display frames with DPI awareness and blocklist enforcement."""

    def __init__(
        self,
        *,
        blocklist: ScreenBlocklistConfig | None = None,
        image_max_side: int = 1920,
        jpeg_quality: int = 90,
        api: Win32API | None = None,
        grabber: Callable[..., Image.Image] | None = None,
    ) -> None:
        self.blocklist = blocklist or ScreenBlocklistConfig()
        self.image_max_side = image_max_side
        self.jpeg_quality = jpeg_quality
        self._api = api
        self._has_injected_api = api is not None
        self._grabber = grabber
        self._capture_seq = 0
        self._seq_lock = threading.Lock()

    def _get_api(self) -> Win32API:
        if self._api is None:
            self._api = Win32API()
        return self._api

    def get_active_window_info(self) -> WindowInfo | None:
        """Inspect the current foreground window and its bounds/monitor on Windows."""
        if not self._has_injected_api and (sys.platform != "win32" or self._grabber is not None):
            return None

        try:
            api = self._get_api()
            hwnd = api.user32.GetForegroundWindow()
            if not isinstance(hwnd, int) or not hwnd:
                return None

            title = get_window_title(hwnd, api)
            proc_name = get_window_process_name(hwnd, api)
            mon = get_monitor_for_window(hwnd, api)
            bounds = get_window_bounds(hwnd, api, clamp_to_monitor=mon)
            return WindowInfo(
                hwnd=hwnd,
                title=title,
                process_name=proc_name,
                is_visible=True,
                bounds=bounds,
                monitor_index=mon.index if mon is not None else None,
                monitor_bounds=mon.bounds if mon is not None else None,
            )
        except Exception as exc:
            logger.debug("Failed getting active window info: %s", exc)
            return None

    def _grab_screen_region(
        self,
        bounds: tuple[int, int, int, int] | None,
        *,
        virtual_origin: tuple[int, int] = (0, 0),
    ) -> Image.Image:
        """Capture physical screen bounds (mss -> ImageGrab fallback) or injected grabber."""
        if self._grabber is not None:
            if bounds is not None:
                try:
                    img = self._grabber(bbox=bounds, all_screens=True)
                except TypeError:
                    img = self._grabber(all_screens=True)
                target_w = bounds[2] - bounds[0]
                target_h = bounds[3] - bounds[1]
                if (
                    self._has_injected_api
                    and target_w > 0
                    and target_h > 0
                    and img.size != (target_w, target_h)
                ):
                    ox, oy = virtual_origin
                    crop_left = max(0, bounds[0] - ox)
                    crop_top = max(0, bounds[1] - oy)
                    crop_right = min(img.width, bounds[2] - ox)
                    crop_bottom = min(img.height, bounds[3] - oy)
                    if crop_right > crop_left and crop_bottom > crop_top:
                        img = img.crop((crop_left, crop_top, crop_right, crop_bottom))
                return img
            return self._grabber(all_screens=True)

        # Fresh mss capture per call (GDI HDC is not safe to cache across threads).
        try:
            import mss

            mss_cls = getattr(mss, "MSS", mss.mss)
            with mss_cls() as sct:
                if bounds is not None and bounds[2] > bounds[0] and bounds[3] > bounds[1]:
                    mon_rect = {
                        "left": bounds[0],
                        "top": bounds[1],
                        "width": bounds[2] - bounds[0],
                        "height": bounds[3] - bounds[1],
                    }
                else:
                    mon_rect = sct.monitors[1] if len(sct.monitors) > 1 else sct.monitors[0]
                shot = sct.grab(mon_rect)
                return Image.frombytes("RGB", shot.size, shot.bgra, "raw", "BGRX")
        except Exception as mss_exc:
            logger.debug("mss capture failed, falling back to ImageGrab: %s", mss_exc)

        from PIL import ImageGrab

        if bounds is not None and bounds[2] > bounds[0] and bounds[3] > bounds[1]:
            try:
                return ImageGrab.grab(bbox=bounds, all_screens=True)
            except Exception:
                return ImageGrab.grab(bbox=bounds)

        try:
            return ImageGrab.grab(all_screens=True)
        except Exception:
            return ImageGrab.grab()

    def capture(
        self,
        *,
        target: str = "full_screen",
        monitor: int | None = None,
        include_layout: bool = True,
    ) -> ScreenCaptureResult:
        """Capture a fresh active-window or active-monitor frame with blocklist & metadata."""
        with self._seq_lock:
            self._capture_seq += 1
            seq = self._capture_seq
        captured_at = datetime.now(UTC).isoformat()

        # Enable Per-Monitor V2 DPI awareness BEFORE querying window/monitor bounds.
        if self._has_injected_api or self._grabber is None:
            with contextlib.suppress(Exception):
                enable_per_monitor_dpi_awareness(self._api)

        win_info = self.get_active_window_info()
        app_name = (
            resolve_app_display_name(win_info.process_name, win_info.title)
            if win_info is not None
            else None
        )

        if win_info is not None and is_window_blocked(
            win_info.title, win_info.process_name, self.blocklist
        ):
            logger.warning(
                "Screen capture blocked for window '%s' (%s) by blocklist.",
                win_info.title,
                win_info.process_name,
            )
            return ScreenCaptureResult(
                image_bytes=None,
                target=target,
                width=0,
                height=0,
                window_title=win_info.title,
                process_name=win_info.process_name,
                blocked=True,
                app_name=app_name,
                window_bounds=win_info.bounds,
                monitor_index=win_info.monitor_index,
                monitor_bounds=win_info.monitor_bounds,
                captured_at=captured_at,
                capture_seq=seq,
            )

        target_mon: MonitorInfo | None = None
        monitors: list[MonitorInfo] = []
        virtual_origin = (0, 0)
        window_layout: list[dict[str, Any]] | None = None
        window_geometry_summary: str | None = None
        if self._has_injected_api or (sys.platform == "win32" and self._grabber is None):
            with contextlib.suppress(Exception):
                api = self._get_api()
                monitors = enumerate_monitors(api)
                if monitors:
                    virtual_origin = (
                        min(m.left for m in monitors),
                        min(m.top for m in monitors),
                    )
                target_mon = get_target_monitor(
                    hwnd=win_info.hwnd if win_info is not None else None,
                    monitor=monitor,
                    api=api,
                )
                if not monitors and target_mon is not None:
                    virtual_origin = (min(0, target_mon.left), min(0, target_mon.top))
                if include_layout:
                    layout_items = inspect_window_layout_win32(api)
                    if layout_items:
                        window_layout = layout_items
                        window_geometry_summary = format_window_geometry_z_order(layout_items)

        effective_monitor_index = (
            target_mon.index
            if target_mon is not None
            else (win_info.monitor_index if win_info is not None else None)
        )
        effective_monitor_bounds = (
            target_mon.bounds
            if target_mon is not None
            else (win_info.monitor_bounds if win_info is not None else None)
        )

        # Preserve simple test grabbers when no Win32API mock was injected.
        capture_bounds: tuple[int, int, int, int] | None = None
        if self._grabber is None or self._has_injected_api:
            if target == "active_window":
                win_bounds = win_info.bounds if win_info is not None else None
                if (
                    win_bounds is not None
                    and win_bounds[2] > win_bounds[0]
                    and win_bounds[3] > win_bounds[1]
                ):
                    capture_bounds = _clip_bounds_to_monitors(
                        win_bounds,
                        monitors,
                        effective_monitor_bounds,
                    )
                elif effective_monitor_bounds is not None:
                    capture_bounds = effective_monitor_bounds
            else:
                capture_bounds = effective_monitor_bounds

        try:
            img = self._grab_screen_region(capture_bounds, virtual_origin=virtual_origin)
            if target == "active_window":
                frame_bytes, mime_type = encode_native_frame(img)
            else:
                frame_bytes = encode_frame(
                    img,
                    max_side=self.image_max_side,
                    quality=self.jpeg_quality,
                )
                mime_type = "image/jpeg"

            return ScreenCaptureResult(
                image_bytes=frame_bytes,
                target=target,
                width=img.width,
                height=img.height,
                window_title=win_info.title if win_info else None,
                process_name=win_info.process_name if win_info else None,
                blocked=False,
                mime_type=mime_type,
                app_name=app_name,
                window_bounds=win_info.bounds if win_info else None,
                monitor_index=effective_monitor_index,
                monitor_bounds=effective_monitor_bounds,
                captured_at=captured_at,
                capture_seq=seq,
                window_layout=window_layout,
                window_geometry_summary=window_geometry_summary,
            )
        except Exception as exc:
            logger.warning("Screen capture failed: %s", exc)
            return ScreenCaptureResult(
                image_bytes=None,
                target=target,
                width=0,
                height=0,
                window_title=win_info.title if win_info else None,
                process_name=win_info.process_name if win_info else None,
                blocked=False,
                error=str(exc),
                app_name=app_name,
                window_bounds=win_info.bounds if win_info else None,
                monitor_index=effective_monitor_index,
                monitor_bounds=effective_monitor_bounds,
                captured_at=captured_at,
                capture_seq=seq,
                window_layout=window_layout,
                window_geometry_summary=window_geometry_summary,
            )

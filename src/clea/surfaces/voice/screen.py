"""Screen capture and frame encoding service for live voice vision (Slice 2).

Provides DPI-aware full-screen and active-window capture using Pillow and Win32,
checks window titles and process names against ScreenBlocklistConfig, resizes frames
to image_max_side, and encodes them to JPEG for live streaming to Gemini Live API.
"""

from __future__ import annotations

import fnmatch
import io
import logging
import sys
from collections.abc import Callable
from dataclasses import dataclass

from PIL import Image

from clea.surfaces.voice.config import ScreenBlocklistConfig
from clea.surfaces.voice.win32 import Win32API, WindowInfo, enable_per_monitor_dpi_awareness

logger = logging.getLogger(__name__)


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


def encode_frame(
    image: Image.Image,
    *,
    max_side: int = 768,
    quality: int = 80,
) -> bytes:
    """Convert image to RGB, proportionally downscale to max_side, and encode to JPEG bytes."""
    img = image
    if img.mode != "RGB":
        img = img.convert("RGB")

    w, h = img.size
    longest = max(w, h)
    if longest > max_side and longest > 0:
        scale = max_side / float(longest)
        new_w = max(1, int(round(w * scale)))
        new_h = max(1, int(round(h * scale)))
        # Resampling.LANCZOS or Image.LANCZOS
        resample = getattr(Image, "Resampling", Image).LANCZOS
        img = img.resize((new_w, new_h), resample=resample)

    buf = io.BytesIO()
    img.save(buf, format="JPEG", quality=quality, optimize=True)
    return buf.getvalue()


class ScreenCaptureService:
    """Service capturing display frames with DPI awareness and blocklist enforcement."""

    def __init__(
        self,
        *,
        blocklist: ScreenBlocklistConfig | None = None,
        image_max_side: int = 768,
        jpeg_quality: int = 80,
        api: Win32API | None = None,
        grabber: Callable[..., Image.Image] | None = None,
    ) -> None:
        self.blocklist = blocklist or ScreenBlocklistConfig()
        self.image_max_side = image_max_side
        self.jpeg_quality = jpeg_quality
        self._api = api
        self._grabber = grabber

    def _get_api(self) -> Win32API:
        if self._api is None:
            self._api = Win32API()
        return self._api

    def get_active_window_info(self) -> WindowInfo | None:
        """Inspect the current foreground window on Windows."""
        if sys.platform != "win32" and self._api is None:
            return None

        try:
            api = self._get_api()
            hwnd = api.user32.GetForegroundWindow()
            if not hwnd:
                return None

            from clea.surfaces.voice.win32 import get_window_process_name, get_window_title

            title = get_window_title(hwnd, api)
            proc_name = get_window_process_name(hwnd, api)
            return WindowInfo(
                hwnd=hwnd,
                title=title,
                process_name=proc_name,
                is_visible=True,
            )
        except Exception as exc:
            logger.debug("Failed getting active window info: %s", exc)
            return None

    def capture(
        self,
        *,
        target: str = "full_screen",
        monitor: int | None = None,
    ) -> ScreenCaptureResult:
        """Capture screen or active window, checking privacy blocklist."""
        win_info: WindowInfo | None = None

        if target == "active_window":
            win_info = self.get_active_window_info()
            if win_info is not None and is_window_blocked(
                win_info.title, win_info.process_name, self.blocklist
            ):
                logger.warning(
                    "Screen capture blocked for active window '%s' (%s) by blocklist.",
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
                )

        try:
            enable_per_monitor_dpi_awareness(self._api)
        except Exception as exc:
            logger.debug("DPI awareness setup error: %s", exc)

        try:
            if self._grabber is not None:
                img = self._grabber(all_screens=True)
            else:
                from PIL import ImageGrab

                try:
                    img = ImageGrab.grab(all_screens=True)
                except Exception:
                    img = ImageGrab.grab()

            jpeg_bytes = encode_frame(
                img,
                max_side=self.image_max_side,
                quality=self.jpeg_quality,
            )
            return ScreenCaptureResult(
                image_bytes=jpeg_bytes,
                target=target,
                width=img.width,
                height=img.height,
                window_title=win_info.title if win_info else None,
                process_name=win_info.process_name if win_info else None,
                blocked=False,
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
            )

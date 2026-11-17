"""Unified Screen and Window Vision tool (inspect_screen).

Supports inspecting the active window, background occluded windows via Win32
PrintWindow, fast UI Automation text probing, and recent clipboard image/text.
Ephemeral image artifacts are stored in <data_dir>/artifacts/ and pruned after 24 hours.
Multimodal image bytes are scoped strictly to the active turn without memory bloat.
"""

from __future__ import annotations

import contextlib
import ctypes
import io
import logging
import subprocess
import sys
import time
import uuid
from ctypes import wintypes
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from PIL import Image
from pydantic import BaseModel, Field

from clea.memory.store import default_data_dir
from clea.surfaces.voice.win32 import (
    _RECT,
    DWMWA_EXTENDED_FRAME_BOUNDS,
    get_window_bounds,
    get_window_process_name,
    get_window_title,
    resolve_app_display_name,
)
from clea.tools.clipboard import (
    IMAGE_EXTENSIONS,
    ClipboardService,
    get_default_clipboard_service,
)
from clea.tools.registry import RiskLevel, ToolRegistry

logger = logging.getLogger(__name__)

# Win32 Constants for background capture & window filtering
PW_RENDERFULLCONTENT = 0x00000002
GWL_EXSTYLE = -20
WS_EX_TOOLWINDOW = 0x00000080
MAX_VISION_DIMENSION = 1568
DEFAULT_PROBE_MAX_CHARS = 3000
CLIPBOARD_IMAGE_RECENCY_THRESHOLD_SECONDS = 10.0


# ----------------------------------------------------------------- Data Models


class WindowInfo(BaseModel):
    """Ground-truth metadata for an enumerated or inspected top-level window."""

    hwnd: int
    title: str
    process_name: str
    app_name: str | None = None
    is_visible: bool = True
    bounds: tuple[int, int, int, int] | None = None


class WindowCapture(BaseModel):
    """Raw lossless image bytes and dimensions captured from a window."""

    png_bytes: bytes
    width: int
    height: int
    window_title: str | None = None
    process_name: str | None = None


class ScreenInspectionResult(BaseModel):
    """Structured outcome of an inspect_screen invocation."""

    status: Literal["ok", "minimized", "not_found", "empty", "error"]
    source: (
        Literal["active_window", "background_window", "clipboard", "clipboard_text"]
        | str
        | None
    )
    window_title: str | None = None
    process_name: str | None = None
    question: str | None = None
    dimensions: tuple[int, int] | None = None
    width: int | None = None
    height: int | None = None
    artifact_path: str | None = None
    uia_text: str | None = None
    text: str | None = None
    message: str | None = None
    attachment: dict[str, Any] | None = Field(default=None, exclude=True)

    def to_dict(self, include_attachment: bool = True) -> dict[str, Any]:
        """Return dict representation, optionally including ephemeral attachment bytes."""
        res: dict[str, Any] = {
            "status": self.status,
            "source": self.source,
            "window_title": self.window_title,
            "process_name": self.process_name,
            "question": self.question,
            "dimensions": self.dimensions,
            "width": self.width,
            "height": self.height,
            "artifact_path": self.artifact_path,
            "uia_text": self.uia_text,
            "text": self.text,
            "message": self.message,
        }
        if include_attachment and self.attachment is not None:
            res["attachment"] = self.attachment
        return res


# ---------------------------------------------------- Backend Protocol & Win32


@runtime_checkable
class ScreenVisionBackend(Protocol):
    """Protocol for discovering, probing, and capturing windows."""

    def get_foreground_window(self) -> WindowInfo | None:
        """Return metadata for the currently active foreground window."""
        ...

    def find_window(self, query: str) -> WindowInfo | None:
        """Find a top-level window matching title, process, or app name."""
        ...

    def is_minimized(self, hwnd: int) -> bool:
        """Return True if the target window is minimized / iconic."""
        ...

    def probe_uia_text(self, hwnd: int, max_chars: int = DEFAULT_PROBE_MAX_CHARS) -> str:
        """Extract readable control text from the window with a safety cap."""
        ...

    def capture_window_png(self, hwnd: int) -> WindowCapture:
        """Capture window pixels via PrintWindow into a cropped/capped PNG."""
        ...


class _BITMAPINFOHEADER(ctypes.Structure):
    _fields_ = [
        ("biSize", wintypes.DWORD),
        ("biWidth", wintypes.LONG),
        ("biHeight", wintypes.LONG),
        ("biPlanes", wintypes.WORD),
        ("biBitCount", wintypes.WORD),
        ("biCompression", wintypes.DWORD),
        ("biSizeImage", wintypes.DWORD),
        ("biXPelsPerMeter", wintypes.LONG),
        ("biYPelsPerMeter", wintypes.LONG),
        ("biClrUsed", wintypes.DWORD),
        ("biClrImportant", wintypes.DWORD),
    ]


class _BITMAPINFO(ctypes.Structure):
    _fields_ = [
        ("bmiHeader", _BITMAPINFOHEADER),
        ("bmiColors", wintypes.DWORD * 3),
    ]


class Win32ScreenVisionBackend:
    """Win32 ctypes implementation for non-stealing occluded capture and UIA text probe."""

    def __init__(
        self,
        user32: Any = None,
        gdi32: Any = None,
        dwmapi: Any = None,
        kernel32: Any = None,
    ) -> None:
        if sys.platform == "win32":
            self._user32 = user32 or getattr(ctypes.windll, "user32", None)
            self._gdi32 = gdi32 or getattr(ctypes.windll, "gdi32", None)
            self._dwmapi = dwmapi or getattr(ctypes.windll, "dwmapi", None)
            self._kernel32 = kernel32 or getattr(ctypes.windll, "kernel32", None)
        else:
            self._user32 = user32
            self._gdi32 = gdi32
            self._dwmapi = dwmapi
            self._kernel32 = kernel32

    def is_minimized(self, hwnd: int) -> bool:
        """Check user32.IsIconic(hwnd)."""
        if not self._user32 or not hasattr(self._user32, "IsIconic") or not hwnd:
            return False
        try:
            res = self._user32.IsIconic(hwnd)
            return bool(res)
        except Exception:
            return False

    def get_foreground_window(self) -> WindowInfo | None:
        """Resolve the active foreground window."""
        if not self._user32 or not hasattr(self._user32, "GetForegroundWindow"):
            return None
        hwnd = self._user32.GetForegroundWindow()
        if not hwnd:
            return None
        if hasattr(self._user32, "IsWindowVisible") and not self._user32.IsWindowVisible(hwnd):
            return None
        title = get_window_title(hwnd)
        proc_name = get_window_process_name(hwnd)
        app_name = resolve_app_display_name(proc_name, title)
        bounds = get_window_bounds(hwnd)
        return WindowInfo(
            hwnd=hwnd,
            title=title,
            process_name=proc_name,
            app_name=app_name,
            is_visible=True,
            bounds=bounds,
        )

    def find_window(self, query: str) -> WindowInfo | None:
        """Enumerate visible windows and match against process name, title, or app name."""
        if not self._user32 or not hasattr(self._user32, "EnumWindows"):
            return None
        q = (query or "").strip().lower().strip("'\"")
        if not q:
            return None
        q_stem = q[:-4] if q.endswith(".exe") else q

        candidates: list[tuple[int, WindowInfo, bool, int]] = []

        def enum_proc(hwnd: int, _lparam: int) -> bool:
            if not self._user32.IsWindowVisible(hwnd):
                return True

            # Skip tool windows without titles
            if hasattr(self._user32, "GetWindowLongW"):
                ex_style = self._user32.GetWindowLongW(hwnd, GWL_EXSTYLE)
                if ex_style & WS_EX_TOOLWINDOW:
                    t = get_window_title(hwnd)
                    if not t:
                        return True

            rect = _RECT()
            if hasattr(self._user32, "GetWindowRect"):
                if self._user32.GetWindowRect(hwnd, ctypes.byref(rect)):
                    if rect.right <= rect.left or rect.bottom <= rect.top:
                        return True

            title = get_window_title(hwnd)
            proc = get_window_process_name(hwnd)
            app = resolve_app_display_name(proc, title)
            t_lower = title.lower()
            p_lower = proc.lower()
            p_stem = p_lower[:-4] if p_lower.endswith(".exe") else p_lower
            a_lower = app.lower() if app and app != "Unknown application" else ""

            matched = False
            priority = 0
            if q == p_stem or q == p_lower or q_stem == p_stem:
                matched = True
                priority = 4
            elif a_lower and q == a_lower:
                matched = True
                priority = 3
            elif q in p_lower or q in p_stem or q_stem in p_stem:
                matched = True
                priority = 2
            elif q in t_lower or (a_lower and q in a_lower):
                matched = True
                priority = 1

            if matched:
                minimized = self.is_minimized(hwnd)
                bounds = (int(rect.left), int(rect.top), int(rect.right), int(rect.bottom))
                win_info = WindowInfo(
                    hwnd=hwnd,
                    title=title,
                    process_name=proc,
                    app_name=app,
                    is_visible=True,
                    bounds=bounds,
                )
                candidates.append((hwnd, win_info, minimized, priority))

            return True

        win_functype = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
        wnd_enum_proc = win_functype(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
        cb = wnd_enum_proc(enum_proc)
        try:
            self._user32.EnumWindows(cb, 0)
        except Exception as exc:
            logger.debug("EnumWindows failed in find_window: %s", exc)

        if not candidates:
            return None

        # Sort: prefer non-minimized first, then highest match priority
        candidates.sort(key=lambda c: (0 if not c[2] else 1, -c[3]))
        return candidates[0][1]

    def capture_window_png(self, hwnd: int) -> WindowCapture:
        """Capture window via PrintWindow(PW_RENDERFULLCONTENT), crop DWM borders, downscale."""
        if not self._user32 or not self._gdi32:
            raise RuntimeError("Win32 user32/gdi32 not available for screen vision.")

        title = get_window_title(hwnd)
        proc_name = get_window_process_name(hwnd)

        # 1. Full window bounds from GetWindowRect
        win_rect = _RECT()
        if not self._user32.GetWindowRect(hwnd, ctypes.byref(win_rect)):
            raise RuntimeError(f"Failed to get window rect for HWND {hwnd}")

        full_w = win_rect.right - win_rect.left
        full_h = win_rect.bottom - win_rect.top
        if full_w <= 0 or full_h <= 0:
            raise RuntimeError(f"Window HWND {hwnd} has invalid zero or negative dimensions")

        # 2. Visible frame bounds from DwmGetWindowAttribute to crop drop shadows
        dwm_rect = _RECT()
        obtained_dwm = False
        if self._dwmapi and hasattr(self._dwmapi, "DwmGetWindowAttribute"):
            hr = self._dwmapi.DwmGetWindowAttribute(
                hwnd,
                DWMWA_EXTENDED_FRAME_BOUNDS,
                ctypes.byref(dwm_rect),
                ctypes.sizeof(dwm_rect),
            )
            if hr == 0 and dwm_rect.right > dwm_rect.left and dwm_rect.bottom > dwm_rect.top:
                obtained_dwm = True

        # 3. Create compatible memory DC and bitmap
        hdc_screen = self._user32.GetDC(0)
        if not hdc_screen:
            raise RuntimeError("Failed to get screen DC")
        mem_dc = self._gdi32.CreateCompatibleDC(hdc_screen)
        if not mem_dc:
            self._user32.ReleaseDC(0, hdc_screen)
            raise RuntimeError("Failed to create memory DC")
        h_bitmap = self._gdi32.CreateCompatibleBitmap(hdc_screen, full_w, full_h)
        if not h_bitmap:
            self._gdi32.DeleteDC(mem_dc)
            self._user32.ReleaseDC(0, hdc_screen)
            raise RuntimeError("Failed to create compatible bitmap")
        old_bmp = self._gdi32.SelectObject(mem_dc, h_bitmap)

        captured = False
        try:
            # PW_RENDERFULLCONTENT (0x00000002) allows capturing occluded/background windows
            res = self._user32.PrintWindow(hwnd, mem_dc, PW_RENDERFULLCONTENT)
            if not res:
                # Fallback to standard PrintWindow
                res = self._user32.PrintWindow(hwnd, mem_dc, 0)
            captured = bool(res)

            # Fallback for active foreground window if PrintWindow failed
            if not captured and hasattr(self._user32, "GetForegroundWindow"):
                if self._user32.GetForegroundWindow() == hwnd:
                    res_blt = self._gdi32.BitBlt(
                        mem_dc,
                        0,
                        0,
                        full_w,
                        full_h,
                        hdc_screen,
                        win_rect.left,
                        win_rect.top,
                        0x00CC0020,  # SRCCOPY
                    )
                    captured = bool(res_blt)

            if not captured:
                raise RuntimeError(f"PrintWindow failed to capture pixels for HWND {hwnd}")

            # 4. Extract pixel bytes via GetDIBits
            bmi = _BITMAPINFO()
            bmi.bmiHeader.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            bmi.bmiHeader.biWidth = full_w
            bmi.bmiHeader.biHeight = -full_h  # top-down DIB
            bmi.bmiHeader.biPlanes = 1
            bmi.bmiHeader.biBitCount = 32
            bmi.bmiHeader.biCompression = 0

            buf = ctypes.create_string_buffer(full_w * full_h * 4)
            lines = self._gdi32.GetDIBits(
                mem_dc,
                h_bitmap,
                0,
                full_h,
                buf,
                ctypes.byref(bmi),
                0,
            )
            if lines == 0:
                raise RuntimeError(
                    f"GetDIBits failed to retrieve bitmap pixels for HWND {hwnd}"
                )
            if lines != full_h:
                logger.warning(
                    "GetDIBits returned %d lines out of %d for HWND %s", lines, full_h, hwnd
                )
        finally:
            self._gdi32.SelectObject(mem_dc, old_bmp)
            self._gdi32.DeleteObject(h_bitmap)
            self._gdi32.DeleteDC(mem_dc)
            self._user32.ReleaseDC(0, hdc_screen)

        img = Image.frombuffer("RGBA", (full_w, full_h), buf, "raw", "BGRA", 0, 1)

        # 5. Crop invisible 7-8px drop-shadow margins if DWM bounds are valid sub-rectangle
        if obtained_dwm:
            crop_w = dwm_rect.right - dwm_rect.left
            crop_h = dwm_rect.bottom - dwm_rect.top
            crop_x = max(0, dwm_rect.left - win_rect.left)
            crop_y = max(0, dwm_rect.top - win_rect.top)
            # Only crop if DWM bounds are a valid sub-rectangle within the captured bitmap
            if 0 < crop_w <= full_w and 0 < crop_h <= full_h:
                crop_x2 = min(full_w, crop_x + crop_w)
                crop_y2 = min(full_h, crop_y + crop_h)
                if (
                    crop_x2 > crop_x
                    and crop_y2 > crop_y
                    and (crop_x > 0 or crop_y > 0 or crop_x2 < full_w or crop_y2 < full_h)
                ):
                    img = img.crop((crop_x, crop_y, crop_x2, crop_y2))

        # 6. Downscale proportionally only if max(width, height) > 1568
        w, h = img.size
        longest = max(w, h)
        if longest > MAX_VISION_DIMENSION:
            scale = float(MAX_VISION_DIMENSION) / float(longest)
            new_w = max(1, int(round(w * scale)))
            new_h = max(1, int(round(h * scale)))
            resample = getattr(Image, "Resampling", Image).LANCZOS
            img = img.resize((new_w, new_h), resample=resample)

        # 7. Encode lossless PNG
        out_buf = io.BytesIO()
        img.save(out_buf, format="PNG")
        png_bytes = out_buf.getvalue()

        return WindowCapture(
            png_bytes=png_bytes,
            width=img.width,
            height=img.height,
            window_title=title,
            process_name=proc_name,
        )

    def probe_uia_text(self, hwnd: int, max_chars: int = DEFAULT_PROBE_MAX_CHARS) -> str:
        """Fast UIA text probe extracting readable text controls under hwnd."""
        if not hwnd:
            return ""

        # 1. Try uiautomation or pywinauto if installed
        for mod_name in ("uiautomation", "pywinauto"):
            if mod_name in sys.modules or _is_module_available(mod_name):
                try:
                    if mod_name == "uiautomation":
                        import uiautomation as auto

                        control = auto.ControlFromHandle(hwnd)
                        if control:
                            lines: list[str] = []
                            total_len = 0
                            for item, _depth in auto.WalkTree(control, maxDepth=4):
                                name = getattr(item, "Name", None) or ""
                                if name and name not in lines:
                                    lines.append(name)
                                    total_len += len(name)
                                    if total_len >= max_chars or len(lines) >= 40:
                                        break
                            if lines:
                                return "\n".join(lines)[:max_chars]
                    elif mod_name == "pywinauto":
                        from pywinauto import Application

                        app = Application().connect(handle=hwnd)
                        win = app.window(handle=hwnd)
                        raw_texts = win.texts()
                        lines = [t.strip() for t in raw_texts if t and t.strip()]
                        if lines:
                            return "\n".join(lines)[:max_chars]
                except Exception as exc:
                    logger.debug("Module %s UIA probe failed: %s", mod_name, exc)

        # 2. PowerShell UIAutomationClient fallback
        ps_script = (
            f"Add-Type -AssemblyName UIAutomationClient; "
            f"$win = [System.Windows.Automation.AutomationElement]::FromHandle([IntPtr]{hwnd}); "
            f"if ($win -eq $null) {{ exit 0 }}; "
            f"$conds = New-Object System.Collections.ArrayList; "
            f"[void]$conds.Add((New-Object System.Windows.Automation.PropertyCondition("
            f"[System.Windows.Automation.AutomationElement]::ControlTypeProperty, "
            f"[System.Windows.Automation.ControlType]::Text))); "
            f"[void]$conds.Add((New-Object System.Windows.Automation.PropertyCondition("
            f"[System.Windows.Automation.AutomationElement]::ControlTypeProperty, "
            f"[System.Windows.Automation.ControlType]::Edit))); "
            f"[void]$conds.Add((New-Object System.Windows.Automation.PropertyCondition("
            f"[System.Windows.Automation.AutomationElement]::ControlTypeProperty, "
            f"[System.Windows.Automation.ControlType]::Document))); "
            f"$orCond = New-Object System.Windows.Automation.OrCondition($conds.ToArray()); "
            f"$items = $win.FindAll([System.Windows.Automation.TreeScope]::Descendants, $orCond); "
            f"$txts = New-Object System.Collections.ArrayList; "
            f"foreach ($it in $items) {{ "
            f"  try {{ "
            f"    $n = [string]$it.Current.Name; "
            f"    if (-not [string]::IsNullOrWhiteSpace($n) -and -not $txts.Contains($n)) {{ "
            f"      [void]$txts.Add($n); "
            f"      if ($txts.Count -ge 40) {{ break }} "
            f"    }} "
            f"  }} catch {{ continue }} "
            f"}}; "
            f"$txts -join [Environment]::NewLine"
        )
        try:
            res = subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps_script],
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=2.0,
                check=False,
            )
            if res.returncode == 0 and res.stdout.strip():
                return res.stdout.strip()[:max_chars]
        except Exception as exc:
            logger.debug("PowerShell UIA probe failed: %s", exc)

        # 3. Fast Win32 child windows text probe
        if self._user32 and hasattr(self._user32, "EnumChildWindows"):
            child_texts: list[str] = []
            total_chars = 0

            def enum_child_proc(child_hwnd: int, _lparam: int) -> bool:
                nonlocal total_chars
                t = get_window_title(child_hwnd)
                if not t and hasattr(self._user32, "SendMessageTimeoutW"):
                    buf_len = 512
                    buf = ctypes.create_unicode_buffer(buf_len)
                    res_val = wintypes.DWORD()
                    ret = self._user32.SendMessageTimeoutW(
                        child_hwnd, 0x000D, buf_len, buf, 0x0002, 50, ctypes.byref(res_val)
                    )
                    if ret and buf.value:
                        t = buf.value.strip()

                if t and t not in child_texts:
                    child_texts.append(t)
                    total_chars += len(t)
                    if total_chars >= max_chars or len(child_texts) >= 30:
                        return False
                return True

            win_functype = getattr(ctypes, "WINFUNCTYPE", ctypes.CFUNCTYPE)
            wnd_enum_proc = win_functype(wintypes.BOOL, wintypes.HWND, wintypes.LPARAM)
            cb = wnd_enum_proc(enum_child_proc)
            with contextlib.suppress(Exception):
                self._user32.EnumChildWindows(hwnd, cb, 0)
            if child_texts:
                return "\n".join(child_texts)[:max_chars]

        return ""


def _is_module_available(module_name: str) -> bool:
    """Check if a module can be imported without raising."""
    import importlib.util

    try:
        spec = importlib.util.find_spec(module_name)
        return spec is not None
    except Exception:
        return False


# ---------------------------------------------------- Ephemeral Artifact Store


class ArtifactStore:
    """Ephemeral on-disk storage for captured PNG artifacts with automatic 24h retention."""

    def __init__(self, data_dir: Path | None = None) -> None:
        base = data_dir or default_data_dir()
        self._artifacts_dir = base / "artifacts"
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)

    @property
    def artifacts_dir(self) -> Path:
        """Return the directory path where artifacts are stored."""
        return self._artifacts_dir

    def save_png(self, png_bytes: bytes, prefix: str = "vision") -> Path:
        """Save lossless PNG bytes to <data_dir>/artifacts/<prefix>_<timestamp>_<id>.png."""
        self._artifacts_dir.mkdir(parents=True, exist_ok=True)
        clean_prefix = (
            "".join(c if c.isalnum() or c in ("_", "-") else "_" for c in prefix).strip("_")
            or "vision"
        )
        filename = f"{clean_prefix}_{int(time.time())}_{uuid.uuid4().hex[:8]}.png"
        target = self._artifacts_dir / filename
        target.write_bytes(png_bytes)
        return target

    def prune_expired(self, max_age_seconds: int = 86400) -> int:
        """Delete artifact PNG files older than max_age_seconds (default 24 hours)."""
        if not self._artifacts_dir.exists():
            return 0
        cutoff = time.time() - max_age_seconds
        pruned = 0
        try:
            for item in self._artifacts_dir.iterdir():
                if item.is_file() and item.suffix.lower() == ".png":
                    try:
                        if item.stat().st_mtime < cutoff:
                            item.unlink(missing_ok=True)
                            pruned += 1
                    except Exception as exc:
                        logger.debug("Failed pruning artifact %s: %s", item, exc)
        except Exception as exc:
            logger.debug("Failed reading artifacts dir %s: %s", self._artifacts_dir, exc)
        if pruned:
            logger.info("Pruned %d expired vision artifact(s) from %s", pruned, self._artifacts_dir)
        return pruned


# -------------------------------------------------------- ScreenVisionService


class ScreenVisionService:
    """Coordinates screen inspection, window resolution, and clipboard auto-routing."""

    def __init__(
        self,
        backend: ScreenVisionBackend | None = None,
        clipboard_service: ClipboardService | None = None,
        artifact_store: ArtifactStore | None = None,
    ) -> None:
        self._backend = backend or Win32ScreenVisionBackend()
        self._clipboard = clipboard_service
        self._artifact_store = artifact_store or ArtifactStore()

    @property
    def backend(self) -> ScreenVisionBackend:
        """Return the active ScreenVisionBackend."""
        return self._backend

    @property
    def clipboard_service(self) -> ClipboardService:
        """Return the bound or global default ClipboardService."""
        if self._clipboard is not None:
            return self._clipboard
        return get_default_clipboard_service()

    @property
    def artifact_store(self) -> ArtifactStore:
        """Return the active ArtifactStore."""
        return self._artifact_store

    def inspect(
        self,
        target: str = "auto",
        question: str | None = None,
        include_image: bool = True,
    ) -> ScreenInspectionResult:
        """Execute screen inspection routing to clipboard, active window, or background window."""
        t_raw = (target or "").strip()
        t_norm = t_raw.strip("'\"") or "auto"
        q_norm = question.strip() if question else None

        # 1. Smart Auto-Routing
        if t_norm.lower() == "auto":
            clip = self.clipboard_service
            try:
                clip.poll_sequence()
                if clip.seconds_since_last_change() <= CLIPBOARD_IMAGE_RECENCY_THRESHOLD_SECONDS:
                    snapshot = clip.backend.read_snapshot()
                    if snapshot.kind == "image" or (
                        snapshot.kind == "files" and snapshot.has_image_files
                    ):
                        return self._inspect_clipboard(snapshot, question=q_norm)
            except Exception as exc:
                logger.debug("Clipboard auto-routing poll failed: %s", exc)
            t_norm = "active"

        # 2. Explicit Clipboard Mode
        if t_norm.lower() == "clipboard":
            try:
                snapshot = self.clipboard_service.backend.read_snapshot()
            except Exception as exc:
                return ScreenInspectionResult(
                    status="error",
                    source="clipboard",
                    question=q_norm,
                    message=f"Failed reading clipboard: {exc}",
                )
            return self._inspect_clipboard(snapshot, question=q_norm)

        # 3. Active Window or Background App/Window Mode
        if t_norm.lower() == "active":
            win = self._backend.get_foreground_window()
            source = "active_window"
        else:
            win = self._backend.find_window(t_norm)
            source = "background_window"

        if win is None:
            return ScreenInspectionResult(
                status="not_found",
                source=source,
                question=q_norm,
                message=f"No window matching '{t_norm}' was found.",
            )

        if self._backend.is_minimized(win.hwnd):
            return ScreenInspectionResult(
                status="minimized",
                source=source,
                window_title=win.title,
                process_name=win.process_name,
                question=q_norm,
                message=(
                    f"Window '{win.title}' ({win.process_name}) is minimized. "
                    "Windows paused rendering for this window."
                ),
            )

        uia_text = self._backend.probe_uia_text(win.hwnd)

        if include_image:
            try:
                capture = self._backend.capture_window_png(win.hwnd)
                # Ensure capped at 1568px
                final_bytes = capture.png_bytes
                w, h = capture.width, capture.height
                if max(w, h) > MAX_VISION_DIMENSION:
                    final_bytes, (w, h) = _downscale_png(final_bytes, MAX_VISION_DIMENSION)

                saved_path = self._artifact_store.save_png(final_bytes, prefix="vision_window")
                attachment = {"mime_type": "image/png", "data": final_bytes}
                msg = (
                    f"Captured window '{win.title}' ({win.process_name}) [{w}x{h}]."
                    if not q_norm
                    else (
                        f"Captured window '{win.title}' ({win.process_name}) [{w}x{h}] "
                        f"for question: '{q_norm}'."
                    )
                )
                return ScreenInspectionResult(
                    status="ok",
                    source=source,
                    window_title=win.title,
                    process_name=win.process_name,
                    question=q_norm,
                    dimensions=(w, h),
                    width=w,
                    height=h,
                    artifact_path=str(saved_path),
                    uia_text=uia_text or None,
                    message=msg,
                    attachment=attachment,
                )
            except Exception as exc:
                logger.warning("Failed window capture for HWND %s: %s", win.hwnd, exc)
                return ScreenInspectionResult(
                    status="error",
                    source=source,
                    window_title=win.title,
                    process_name=win.process_name,
                    question=q_norm,
                    uia_text=uia_text or None,
                    message=f"Failed capturing window pixels: {exc}",
                )

        # include_image is False: text probe only
        msg = (
            f"Probed window '{win.title}' ({win.process_name}) text without image capture."
            if not q_norm
            else f"Probed window '{win.title}' ({win.process_name}) text for question: '{q_norm}'."
        )
        return ScreenInspectionResult(
            status="ok",
            source=source,
            window_title=win.title,
            process_name=win.process_name,
            question=q_norm,
            uia_text=uia_text or None,
            message=msg,
        )

    def _inspect_clipboard(
        self, snapshot: Any, question: str | None = None
    ) -> ScreenInspectionResult:
        """Process clipboard snapshot into a ScreenInspectionResult."""
        if snapshot.kind == "empty":
            return ScreenInspectionResult(
                status="empty",
                source="clipboard",
                question=question,
                message="Clipboard is empty or contains an unsupported format.",
            )

        if snapshot.kind == "text":
            text_val = snapshot.text or ""
            msg = (
                f"Clipboard contains text ({len(text_val)} chars): {text_val[:200]}..."
                if not question
                else (
                    f"Clipboard contains text ({len(text_val)} chars) for question "
                    f"'{question}': {text_val[:200]}..."
                )
            )
            return ScreenInspectionResult(
                status="ok",
                source="clipboard_text",
                text=text_val,
                question=question,
                message=msg,
            )

        if snapshot.kind == "files" and not snapshot.has_image_files:
            file_sample = ", ".join(snapshot.file_paths[:3])
            return ScreenInspectionResult(
                status="empty",
                source="clipboard",
                question=question,
                message=f"Clipboard contains non-image file(s) ({file_sample}).",
            )

        # Image or copied image files
        img_bytes = snapshot.image_bytes
        if not img_bytes and snapshot.kind == "files" and snapshot.has_image_files:
            img_path = next(
                (p for p in snapshot.file_paths if p.lower().endswith(IMAGE_EXTENSIONS)), None
            )
            if img_path:
                try:
                    with Image.open(img_path) as im:
                        if im.mode in ("CMYK", "YCbCr", "LAB", "HSV"):
                            im = im.convert("RGB")
                        buf = io.BytesIO()
                        im.save(buf, format="PNG")
                        img_bytes = buf.getvalue()
                except Exception as exc:
                    logger.debug("Failed opening copied image file %s: %s", img_path, exc)

        if not img_bytes:
            return ScreenInspectionResult(
                status="empty",
                source="clipboard",
                question=question,
                message="Clipboard image content could not be read.",
            )

        # Normalize and cap dimensions at 1568px
        final_bytes, (w, h) = _downscale_png(img_bytes, MAX_VISION_DIMENSION)
        saved_path = self._artifact_store.save_png(final_bytes, prefix="vision_clipboard")
        attachment = {"mime_type": "image/png", "data": final_bytes}

        msg = (
            f"Captured image from clipboard [{w}x{h}]."
            if not question
            else f"Captured image from clipboard [{w}x{h}] for question: '{question}'."
        )
        return ScreenInspectionResult(
            status="ok",
            source="clipboard",
            question=question,
            dimensions=(w, h),
            width=w,
            height=h,
            artifact_path=str(saved_path),
            message=msg,
            attachment=attachment,
        )


def _downscale_png(png_bytes: bytes, max_side: int = 1568) -> tuple[bytes, tuple[int, int]]:
    """Ensure PNG image is proportionally capped at max_side and encoded losslessly."""
    with Image.open(io.BytesIO(png_bytes)) as img:
        if img.mode in ("CMYK", "YCbCr", "LAB", "HSV"):
            img = img.convert("RGB")
        elif img.mode == "P" and "transparency" in img.info:
            img = img.convert("RGBA")

        w, h = img.size
        longest = max(w, h)
        if longest <= max_side and img.format == "PNG":
            return png_bytes, (w, h)

        if longest > max_side:
            scale = float(max_side) / float(longest)
            new_w = max(1, int(round(w * scale)))
            new_h = max(1, int(round(h * scale)))
            resample = getattr(Image, "Resampling", Image).LANCZOS
            img = img.resize((new_w, new_h), resample=resample)
            w, h = img.size

        buf = io.BytesIO()
        img.save(buf, format="PNG")
        return buf.getvalue(), (w, h)


# ----------------------------------------------------- Tool Handlers & Registry

_default_screen_vision_service: ScreenVisionService | None = None


def get_default_screen_vision_service() -> ScreenVisionService:
    """Return the global default ScreenVisionService instance."""
    global _default_screen_vision_service
    if _default_screen_vision_service is None:
        _default_screen_vision_service = ScreenVisionService()
    return _default_screen_vision_service


def set_default_screen_vision_service(service: ScreenVisionService | None) -> None:
    """Set or reset the global default ScreenVisionService instance."""
    global _default_screen_vision_service
    _default_screen_vision_service = service


def inspect_screen(
    target: str = "auto",
    question: str = "",
    include_image: bool = True,
    *,
    service: ScreenVisionService | None = None,
) -> dict[str, Any]:
    """Inspect active window, background window, or recent clipboard image."""
    svc = service or get_default_screen_vision_service()
    res = svc.inspect(
        target="auto" if target is None else target,
        question=question or "",
        include_image=True if include_image is None else bool(include_image),
    )
    return res.to_dict(include_attachment=True)


def register_vision_tools(
    registry: ToolRegistry,
    screen_vision_service: ScreenVisionService | None = None,
    *,
    risk_level: RiskLevel = RiskLevel.READ_ONLY,
) -> None:
    """Register the inspect_screen tool in ToolRegistry."""
    service = screen_vision_service or get_default_screen_vision_service()

    registry.register(
        name="inspect_screen",
        description=(
            "Inspect the active screen or a specific background window, or inspect recently copied "
            "clipboard images/text. Automatically routes to clipboard if an image was copied "
            "in the last 10 seconds, or targets the active foreground or named application window."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "default": "auto",
                    "description": (
                        "Target to inspect: 'auto' (smart router), 'active' (current foreground "
                        "window), 'clipboard' (clipboard image or text), or an application/window "
                        "title query (e.g. 'vscode', 'chrome', 'terminal')."
                    ),
                },
                "question": {
                    "type": "string",
                    "default": "",
                    "description": (
                        "Optional question or visual focus description for the inspection."
                    ),
                },
                "include_image": {
                    "type": "boolean",
                    "default": True,
                    "description": (
                        "Whether to capture an image screenshot or perform text probe only."
                    ),
                },
            },
            "additionalProperties": False,
        },
        risk_level=risk_level,
        handler=lambda target="auto", question="", include_image=True: inspect_screen(
            target=target,
            question=question,
            include_image=include_image,
            service=service,
        ),
    )

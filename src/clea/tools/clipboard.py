"""Lock-resilient Windows clipboard service, deterministic transforms, and MCP tools."""

from __future__ import annotations

import contextlib
import ctypes
import ctypes.wintypes as wintypes
import io
import json
import logging
import re
import struct
import sys
import time
import urllib.parse
import webbrowser
from collections.abc import Callable, Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Literal, Protocol, runtime_checkable

from clea.tools.registry import RiskLevel, ToolRegistry

logger = logging.getLogger(__name__)

# ----------------------------------------------------------- Win32 Clipboard Constants

CF_TEXT = 1
CF_BITMAP = 2
CF_DIB = 8
CF_UNICODETEXT = 13
CF_HDROP = 15
CF_DIBV5 = 17

BI_RGB = 0
DIB_RGB_COLORS = 0
PNG_IEND_TRAILER = b"\x00\x00\x00\x00IEND\xaeB\x60\x82"

GMEM_MOVEABLE = 0x0002
GMEM_ZEROINIT = 0x0040

IMAGE_EXTENSIONS = (".png", ".jpg", ".jpeg", ".webp", ".bmp")

# URL tracking parameters to strip
TRACKING_PARAMS = {
    "fbclid",
    "gclid",
    "si",
    "ref",
    "ref_src",
    "mc_cid",
    "mc_eid",
    "igshid",
}

URL_REGEX = re.compile(r"https?://[^\s<>\"]+")
ANSI_REGEX = re.compile(r"\x1b\[[0-9;]*[a-zA-Z]")

_DISALLOWED_OPEN_BINARIES: frozenset[str] = frozenset(
    {
        "cmd",
        "cmd.exe",
        "powershell",
        "powershell.exe",
        "pwsh",
        "pwsh.exe",
        "bash",
        "bash.exe",
        "sh",
        "sh.exe",
        "wscript",
        "wscript.exe",
        "cscript",
        "cscript.exe",
        "mshta",
        "mshta.exe",
        "rundll32",
        "rundll32.exe",
        "regsvr32",
        "regsvr32.exe",
    }
)


# ----------------------------------------------------------- Error Classes & Snapshots


class ClipboardLockedError(RuntimeError):
    """Raised when the Windows clipboard cannot be opened after lock contention retries."""


@dataclass(slots=True)
class ClipboardSnapshot:
    """Immutable snapshot of the current clipboard content."""

    kind: Literal["text", "image", "files", "empty"]
    text: str | None = None
    image_bytes: bytes | None = None
    image_format: str | None = None
    image_dimensions: tuple[int, int] | None = None
    file_paths: list[str] = field(default_factory=list)
    has_image_files: bool = False
    char_count: int = 0
    line_count: int = 0

    def __post_init__(self) -> None:
        if self.text is not None:
            if not self.char_count:
                self.char_count = len(self.text)
            if not self.line_count:
                self.line_count = len(self.text.splitlines()) if self.text else 0
        if self.file_paths and not self.has_image_files:
            self.has_image_files = any(
                p.lower().endswith(IMAGE_EXTENSIONS) for p in self.file_paths
            )


# ------------------------------------------------ Backend Protocol & Win32 Implementation


@runtime_checkable
class ClipboardBackend(Protocol):
    """Protocol for reading and writing clipboard state."""

    def get_sequence_number(self) -> int:
        """Return the clipboard sequence integer without reading contents."""
        ...

    def read_snapshot(self) -> ClipboardSnapshot:
        """Read the active clipboard format and return a ClipboardSnapshot."""
        ...

    def write_text(self, text: str) -> None:
        """Write Unicode text to the clipboard with lock-resilient guards."""
        ...


def _parse_png_dimensions(png_bytes: bytes) -> tuple[int, int] | None:
    """Fast header parser for PNG dimensions without loading full image."""
    if len(png_bytes) >= 24 and png_bytes[:8] == b"\x89PNG\r\n\x1a\n":
        try:
            return struct.unpack(">II", png_bytes[16:24])
        except Exception as exc:
            logger.warning("Failed unpacking PNG IHDR dimensions: %s", exc)
    try:
        from PIL import Image

        with Image.open(io.BytesIO(png_bytes)) as img:
            return img.size
    except Exception as exc:
        logger.warning("Failed reading PNG dimensions via PIL: %s", exc)
        return None


class _WIN32_BITMAP(ctypes.Structure):
    """Win32 GDI BITMAP structure queried via gdi32.GetObjectW."""

    _fields_ = [
        ("bmType", wintypes.LONG),
        ("bmWidth", wintypes.LONG),
        ("bmHeight", wintypes.LONG),
        ("bmWidthBytes", wintypes.LONG),
        ("bmPlanes", wintypes.WORD),
        ("bmBitsPixel", wintypes.WORD),
        ("bmBits", ctypes.c_void_p),
    ]


class _BITMAPINFOHEADER(ctypes.Structure):
    """40-byte Win32 BITMAPINFOHEADER for GetDIBits BI_RGB extraction."""

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


def _configure_win32_signatures(
    user32: Any, kernel32: Any, shell32: Any, gdi32: Any = None
) -> None:
    """Declare explicit 64-bit safe ctypes argtypes/restype using c_void_p for handles."""
    if isinstance(user32, ctypes.CDLL):
        user32.OpenClipboard.argtypes = [ctypes.c_void_p]
        user32.OpenClipboard.restype = wintypes.BOOL

        user32.CloseClipboard.argtypes = []
        user32.CloseClipboard.restype = wintypes.BOOL

        user32.EmptyClipboard.argtypes = []
        user32.EmptyClipboard.restype = wintypes.BOOL

        user32.GetClipboardData.argtypes = [wintypes.UINT]
        user32.GetClipboardData.restype = ctypes.c_void_p

        user32.SetClipboardData.argtypes = [wintypes.UINT, ctypes.c_void_p]
        user32.SetClipboardData.restype = ctypes.c_void_p

        user32.IsClipboardFormatAvailable.argtypes = [wintypes.UINT]
        user32.IsClipboardFormatAvailable.restype = wintypes.BOOL

        user32.RegisterClipboardFormatW.argtypes = [wintypes.LPCWSTR]
        user32.RegisterClipboardFormatW.restype = wintypes.UINT

        if hasattr(user32, "EnumClipboardFormats"):
            user32.EnumClipboardFormats.argtypes = [wintypes.UINT]
            user32.EnumClipboardFormats.restype = wintypes.UINT

        if hasattr(user32, "GetClipboardSequenceNumber"):
            user32.GetClipboardSequenceNumber.argtypes = []
            user32.GetClipboardSequenceNumber.restype = wintypes.DWORD

    if isinstance(kernel32, ctypes.CDLL):
        kernel32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
        kernel32.GlobalAlloc.restype = ctypes.c_void_p

        kernel32.GlobalLock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalLock.restype = ctypes.c_void_p

        kernel32.GlobalUnlock.argtypes = [ctypes.c_void_p]
        kernel32.GlobalUnlock.restype = wintypes.BOOL

        kernel32.GlobalSize.argtypes = [ctypes.c_void_p]
        kernel32.GlobalSize.restype = ctypes.c_size_t

        if hasattr(kernel32, "GlobalFree"):
            kernel32.GlobalFree.argtypes = [ctypes.c_void_p]
            kernel32.GlobalFree.restype = ctypes.c_void_p

    if isinstance(shell32, ctypes.CDLL) and hasattr(shell32, "DragQueryFileW"):
        shell32.DragQueryFileW.argtypes = [
            ctypes.c_void_p,
            wintypes.UINT,
            wintypes.LPWSTR,
            wintypes.UINT,
        ]
        shell32.DragQueryFileW.restype = wintypes.UINT

    if isinstance(gdi32, ctypes.CDLL):
        gdi32.GetObjectW.argtypes = [ctypes.c_void_p, ctypes.c_int, ctypes.c_void_p]
        gdi32.GetObjectW.restype = ctypes.c_int

        gdi32.CreateCompatibleDC.argtypes = [ctypes.c_void_p]
        gdi32.CreateCompatibleDC.restype = ctypes.c_void_p

        gdi32.DeleteDC.argtypes = [ctypes.c_void_p]
        gdi32.DeleteDC.restype = wintypes.BOOL

        gdi32.GetDIBits.argtypes = [
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.UINT,
            wintypes.UINT,
            ctypes.c_void_p,
            ctypes.c_void_p,
            wintypes.UINT,
        ]
        gdi32.GetDIBits.restype = ctypes.c_int


def _decode_registered_png(raw_bytes: bytes) -> tuple[bytes | None, tuple[int, int] | None]:
    """Strip trailing Windows heap padding after the PNG IEND chunk and decode via Pillow."""
    if not raw_bytes:
        return None, None

    iend_idx = raw_bytes.find(PNG_IEND_TRAILER)
    trimmed = raw_bytes[: iend_idx + 12] if iend_idx != -1 else raw_bytes

    try:
        from PIL import Image

        with Image.open(io.BytesIO(trimmed)) as img:
            img.load()
            dims = img.size
            if img.format == "PNG":
                return trimmed, dims
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue(), dims
    except Exception as exc:
        logger.warning("Failed decoding registered PNG clipboard bytes: %s", exc)
        return None, None


def _decode_hbitmap_to_png(
    hbitmap: Any, gdi32: Any
) -> tuple[bytes | None, tuple[int, int] | None]:
    """Decode a Win32 CF_BITMAP HBITMAP into lossless PNG bytes via GDI GetDIBits."""
    if not hbitmap or not gdi32:
        return None, None

    try:
        bmp = _WIN32_BITMAP()
        if not gdi32.GetObjectW(hbitmap, ctypes.sizeof(_WIN32_BITMAP), ctypes.byref(bmp)):
            logger.warning("gdi32.GetObjectW failed for CF_BITMAP handle.")
            return None, None

        w = int(bmp.bmWidth)
        h = abs(int(bmp.bmHeight))
        if w <= 0 or h <= 0 or w > 32768 or h > 32768:
            logger.warning("Invalid CF_BITMAP dimensions: %dx%d", w, h)
            return None, None

        hdc = gdi32.CreateCompatibleDC(None)
        if not hdc:
            logger.warning("gdi32.CreateCompatibleDC failed.")
            return None, None

        try:
            bmi = _BITMAPINFOHEADER()
            bmi.biSize = ctypes.sizeof(_BITMAPINFOHEADER)
            bmi.biWidth = w
            bmi.biHeight = -h
            bmi.biPlanes = 1
            bmi.biBitCount = 32
            bmi.biCompression = BI_RGB

            buf = (ctypes.c_ubyte * (w * h * 4))()
            scanlines = gdi32.GetDIBits(
                hdc,
                hbitmap,
                0,
                h,
                buf,
                ctypes.byref(bmi),
                DIB_RGB_COLORS,
            )
            if not scanlines:
                logger.warning("gdi32.GetDIBits failed (scanlines=%s).", scanlines)
                return None, None

            from PIL import Image

            raw_bytes = bytes(buf)
            # 24-bit/BGRX GDI bitmaps leave every alpha byte at 0x00; promote to opaque RGBA.
            if raw_bytes[3::4] == b"\x00" * (w * h):
                img = Image.frombuffer("RGB", (w, h), raw_bytes, "raw", "BGRX", 0, 1).convert(
                    "RGBA"
                )
            else:
                img = Image.frombuffer("RGBA", (w, h), raw_bytes, "raw", "BGRA", 0, 1)

            out = io.BytesIO()
            img.save(out, format="PNG")
            return out.getvalue(), (w, h)
        finally:
            with contextlib.suppress(Exception):
                gdi32.DeleteDC(hdc)
    except Exception as exc:
        logger.warning("Failed decoding CF_BITMAP via GetDIBits: %s", exc, exc_info=True)
        return None, None


def _convert_dib_to_png(dib_bytes: bytes) -> tuple[bytes | None, tuple[int, int] | None]:
    """Convert CF_DIB / CF_DIBV5 bytes into lossless PNG bytes by prepending BITMAPFILEHEADER."""
    if len(dib_bytes) < 40:
        logger.warning("DIB buffer too short (%d bytes) to parse BITMAPINFOHEADER.", len(dib_bytes))
        return None, None

    try:
        from PIL import Image

        bi_size = struct.unpack_from("<I", dib_bytes, 0)[0]
        bit_count = struct.unpack_from("<H", dib_bytes, 14)[0]
        compression = struct.unpack_from("<I", dib_bytes, 16)[0]
        clr_used = struct.unpack_from("<I", dib_bytes, 32)[0] if bi_size >= 36 else 0

        # BITMAPINFOHEADER (biSize == 40) with BI_BITFIELDS (3) appends 3 DWORD masks (12 bytes)
        # before pixel data. V4 (108) and V5 (124) headers already embed masks inside biSize.
        masks_bytes = 12 if (bi_size == 40 and compression == 3) else 0
        palette_entries = (
            clr_used if clr_used > 0 else ((1 << bit_count) if bit_count <= 8 else 0)
        )
        off_bits = 14 + bi_size + masks_bytes + (palette_entries * 4)

        bmp_header = struct.pack("<2sIHHI", b"BM", 14 + len(dib_bytes), 0, 0, off_bits)
        with Image.open(io.BytesIO(bmp_header + dib_bytes)) as img:
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue(), img.size
    except Exception as exc:
        logger.warning("Failed converting CF_DIB/CF_DIBV5 bytes to PNG: %s", exc, exc_info=True)
        return None, None


_NON_HGLOBAL_CLIPBOARD_FORMATS = frozenset(
    {
        CF_BITMAP,
        3,  # CF_METAFILEPICT
        9,  # CF_PALETTE
        14,  # CF_ENHMETAFILE
        0x0080,  # CF_OWNERDISPLAY
        0x0081,  # CF_DSPTEXT
        0x0082,  # CF_DSPBITMAP
        0x0083,  # CF_DSPMETAFILEPICT
        0x008E,  # CF_DSPENHMETAFILE
    }
)

_CLIPBOARD_HISTORY_EXCLUSION_FORMATS = (
    "ExcludeClipboardContentFromMonitorProcessing",
    "CanIncludeInClipboardHistory",
    "CanUploadToCloudClipboard",
)


class Win32ClipboardBackend:
    """Win32 ctypes implementation with lock contention retries and priority format detection."""

    def __init__(
        self,
        user32: Any = None,
        kernel32: Any = None,
        shell32: Any = None,
        gdi32: Any = None,
    ) -> None:
        if sys.platform == "win32":
            self._user32 = user32 or ctypes.WinDLL("user32", use_last_error=True)
            self._kernel32 = kernel32 or ctypes.WinDLL("kernel32", use_last_error=True)
            self._shell32 = shell32 or ctypes.WinDLL("shell32", use_last_error=True)
            self._gdi32 = gdi32 or ctypes.WinDLL("gdi32", use_last_error=True)
        else:
            self._user32 = user32
            self._kernel32 = kernel32
            self._shell32 = shell32
            self._gdi32 = gdi32
        _configure_win32_signatures(self._user32, self._kernel32, self._shell32, self._gdi32)

    def get_sequence_number(self) -> int:
        """Call user32.GetClipboardSequenceNumber() reading only the serial integer."""
        if not self._user32 or not hasattr(self._user32, "GetClipboardSequenceNumber"):
            return 0
        try:
            return int(self._user32.GetClipboardSequenceNumber())
        except Exception as exc:
            logger.warning("Error getting clipboard sequence number: %s", exc)
            return 0

    @contextmanager
    def open_clipboard(
        self,
        *,
        attempts: int = 3,
        retry_delay_s: float = 0.025,
    ) -> Iterator[None]:
        """Context manager attempting OpenClipboard with configurable retry backoff."""
        if not self._user32 or not hasattr(self._user32, "OpenClipboard"):
            raise RuntimeError("Win32 user32.OpenClipboard is not available.")

        total_attempts = max(1, int(attempts))
        opened = False
        for attempt in range(total_attempts):
            try:
                res = self._user32.OpenClipboard(None)
            except Exception as exc:
                logger.warning("OpenClipboard raised exception on attempt %d: %s", attempt + 1, exc)
                res = 0
            if res:
                opened = True
                break
            if attempt < total_attempts - 1:
                time.sleep(retry_delay_s)

        if not opened:
            raise ClipboardLockedError(
                "Windows clipboard is currently locked by another application "
                f"after {total_attempts} attempts."
            )

        try:
            yield
        finally:
            if hasattr(self._user32, "CloseClipboard"):
                with contextlib.suppress(Exception):
                    self._user32.CloseClipboard()

    def _read_global_bytes(self, h_data: Any, fmt_label: str) -> bytes | None:
        """Lock an HGLOBAL handle and copy its raw bytes safely on 64-bit Windows."""
        p_data = self._kernel32.GlobalLock(h_data)
        if not p_data:
            logger.warning(
                "GlobalLock failed for clipboard format %s (last_error=%s).",
                fmt_label,
                ctypes.get_last_error() if sys.platform == "win32" else None,
            )
            return None
        try:
            size = int(self._kernel32.GlobalSize(h_data))
            if size <= 0:
                logger.warning("GlobalSize returned %d for clipboard format %s.", size, fmt_label)
                return None
            return ctypes.string_at(p_data, size)
        finally:
            self._kernel32.GlobalUnlock(h_data)

    def read_snapshot(self) -> ClipboardSnapshot:
        """Read clipboard content according to format priority."""
        if not self._user32 or not self._kernel32:
            return ClipboardSnapshot(kind="empty")

        image_available = False
        with self.open_clipboard():
            has_fmt = hasattr(self._user32, "IsClipboardFormatAvailable")

            # 1. Registered PNG format (trim trailing Windows GlobalSize heap padding at IEND)
            cf_png = (
                self._user32.RegisterClipboardFormatW("PNG")
                if hasattr(self._user32, "RegisterClipboardFormatW")
                else 0
            )
            if cf_png and has_fmt and self._user32.IsClipboardFormatAvailable(cf_png):
                image_available = True
                h_data = self._user32.GetClipboardData(cf_png)
                if h_data:
                    raw_png = self._read_global_bytes(h_data, "PNG")
                    if raw_png:
                        png_bytes, dims = _decode_registered_png(raw_png)
                        if png_bytes:
                            return ClipboardSnapshot(
                                kind="image",
                                image_bytes=png_bytes,
                                image_format="PNG",
                                image_dimensions=dims,
                            )
                else:
                    logger.warning("GetClipboardData returned NULL for registered PNG format.")

            # 2. CF_BITMAP (2) via GDI GetDIBits (GDI synthesizes CF_BITMAP from CF_DIBV5/CF_DIB)
            if has_fmt and self._user32.IsClipboardFormatAvailable(CF_BITMAP):
                image_available = True
                h_bitmap = self._user32.GetClipboardData(CF_BITMAP)
                if h_bitmap:
                    png_bytes, dims = _decode_hbitmap_to_png(h_bitmap, self._gdi32)
                    if png_bytes:
                        return ClipboardSnapshot(
                            kind="image",
                            image_bytes=png_bytes,
                            image_format="PNG",
                            image_dimensions=dims,
                        )
                else:
                    logger.warning("GetClipboardData returned NULL for CF_BITMAP.")

            # 3. Fallback: CF_DIBV5 (17) and CF_DIB (8) if CF_BITMAP was unavailable
            for dib_fmt, dib_label in ((CF_DIBV5, "CF_DIBV5"), (CF_DIB, "CF_DIB")):
                if has_fmt and self._user32.IsClipboardFormatAvailable(dib_fmt):
                    image_available = True
                    h_data = self._user32.GetClipboardData(dib_fmt)
                    if h_data:
                        dib_bytes = self._read_global_bytes(h_data, dib_label)
                        if dib_bytes:
                            png_bytes, dims = _convert_dib_to_png(dib_bytes)
                            if png_bytes:
                                return ClipboardSnapshot(
                                    kind="image",
                                    image_bytes=png_bytes,
                                    image_format="PNG",
                                    image_dimensions=dims,
                                )
                    else:
                        logger.warning("GetClipboardData returned NULL for %s.", dib_label)

            # 3. CF_HDROP (15) - Copied files
            if has_fmt and self._user32.IsClipboardFormatAvailable(CF_HDROP):
                h_data = self._user32.GetClipboardData(CF_HDROP)
                if h_data and self._shell32 and hasattr(self._shell32, "DragQueryFileW"):
                    count = self._shell32.DragQueryFileW(h_data, 0xFFFFFFFF, None, 0)
                    paths: list[str] = []
                    buf = ctypes.create_unicode_buffer(2048)
                    for i in range(count):
                        self._shell32.DragQueryFileW(h_data, i, buf, 2048)
                        paths.append(buf.value)
                    return ClipboardSnapshot(
                        kind="files",
                        file_paths=paths,
                    )

            # 4. CF_UNICODETEXT (13)
            if has_fmt and self._user32.IsClipboardFormatAvailable(CF_UNICODETEXT):
                h_data = self._user32.GetClipboardData(CF_UNICODETEXT)
                if h_data:
                    p_data = self._kernel32.GlobalLock(h_data)
                    if p_data:
                        try:
                            text = ctypes.wstring_at(p_data)
                            return ClipboardSnapshot(
                                kind="text",
                                text=text,
                            )
                        finally:
                            self._kernel32.GlobalUnlock(h_data)
                    else:
                        logger.warning("GlobalLock failed for CF_UNICODETEXT.")

        # Fallback outside OpenClipboard lock: PIL.ImageGrab if image formats failed or CF_BITMAP
        if image_available:
            try:
                from PIL import Image, ImageGrab

                grabbed = ImageGrab.grabclipboard()
                if isinstance(grabbed, Image.Image):
                    buf = io.BytesIO()
                    grabbed.save(buf, format="PNG")
                    png_bytes = buf.getvalue()
                    return ClipboardSnapshot(
                        kind="image",
                        image_bytes=png_bytes,
                        image_format="PNG",
                        image_dimensions=grabbed.size,
                    )
                if isinstance(grabbed, list) and grabbed:
                    return ClipboardSnapshot(
                        kind="files",
                        file_paths=[str(p) for p in grabbed],
                    )
            except Exception as exc:
                logger.warning("PIL.ImageGrab fallback failed to decode clipboard image: %s", exc)

        return ClipboardSnapshot(kind="empty")

    def write_text(self, text: str) -> None:
        """Write Unicode text to clipboard using Movable Global Memory."""
        if not self._user32 or not self._kernel32:
            raise RuntimeError("Win32 clipboard APIs are not available.")

        with self.open_clipboard():
            self._user32.EmptyClipboard()
            encoded = (text + "\0").encode("utf-16le")
            h_mem = self._kernel32.GlobalAlloc(GMEM_MOVEABLE, len(encoded))
            if not h_mem:
                raise MemoryError("Failed to allocate global memory for clipboard text.")
            p_mem = self._kernel32.GlobalLock(h_mem)
            if not p_mem:
                self._kernel32.GlobalFree(h_mem)
                raise MemoryError("Failed to lock global memory for clipboard text.")
            try:
                ctypes.memmove(p_mem, encoded, len(encoded))
            finally:
                self._kernel32.GlobalUnlock(h_mem)

            if not self._user32.SetClipboardData(CF_UNICODETEXT, h_mem):
                self._kernel32.GlobalFree(h_mem)
                raise RuntimeError("Failed to set clipboard data.")

    def _get_exclusion_format_ids(self) -> dict[str, int]:
        """Resolve Win32 registered format IDs used to hide transient writes from Win+V."""
        if not self._user32 or not hasattr(self._user32, "RegisterClipboardFormatW"):
            return {}
        out: dict[str, int] = {}
        for name in _CLIPBOARD_HISTORY_EXCLUSION_FORMATS:
            with contextlib.suppress(Exception):
                fmt_id = self._user32.RegisterClipboardFormatW(name)
                if isinstance(fmt_id, int) and fmt_id > 0:
                    out[name] = fmt_id
        return out

    def _set_raw_format_locked(self, fmt_id: int, raw_bytes: bytes) -> bool:
        """Write raw bytes into the currently open clipboard under *fmt_id*."""
        alloc_size = max(len(raw_bytes), 1)
        h_mem = self._kernel32.GlobalAlloc(GMEM_MOVEABLE, alloc_size)
        if not h_mem:
            return False
        p_mem = self._kernel32.GlobalLock(h_mem)
        if not p_mem:
            if hasattr(self._kernel32, "GlobalFree"):
                with contextlib.suppress(Exception):
                    self._kernel32.GlobalFree(h_mem)
            return False
        try:
            payload = raw_bytes if raw_bytes else b"\x00"
            ctypes.memmove(p_mem, payload, len(payload))
        finally:
            self._kernel32.GlobalUnlock(h_mem)

        if not self._user32.SetClipboardData(fmt_id, h_mem):
            if hasattr(self._kernel32, "GlobalFree"):
                with contextlib.suppress(Exception):
                    self._kernel32.GlobalFree(h_mem)
            return False
        return True

    def _mark_exclude_from_history_locked(self) -> None:
        """Tag the current open clipboard write so Win+V and Cloud Clipboard ignore it."""
        zero_dword = struct.pack("<I", 0)
        for _name, fmt_id in self._get_exclusion_format_ids().items():
            with contextlib.suppress(Exception):
                self._set_raw_format_locked(fmt_id, zero_dword)

    def capture_raw_formats(
        self,
        *,
        attempts: int = 5,
        retry_delay_s: float = 0.02,
        max_total_bytes: int = 32 * 1024 * 1024,
    ) -> dict[int, bytes]:
        """Capture all HGLOBAL clipboard formats for lossless post-paste restoration."""
        if not self._user32 or not self._kernel32:
            return {}

        exclusion_ids = set(self._get_exclusion_format_ids().values())
        captured: dict[int, bytes] = {}
        total_bytes = 0

        with self.open_clipboard(attempts=attempts, retry_delay_s=retry_delay_s):
            format_ids: list[int] = []
            if hasattr(self._user32, "EnumClipboardFormats"):
                cur = 0
                for _ in range(64):
                    nxt = self._user32.EnumClipboardFormats(cur)
                    if not isinstance(nxt, int) or nxt <= 0:
                        break
                    if nxt in format_ids:
                        break
                    format_ids.append(nxt)
                    cur = nxt
            elif hasattr(self._user32, "IsClipboardFormatAvailable"):
                candidates = [CF_UNICODETEXT, CF_TEXT, CF_DIBV5, CF_DIB, CF_HDROP]
                if hasattr(self._user32, "RegisterClipboardFormatW"):
                    for reg_name in ("HTML Format", "Rich Text Format", "PNG"):
                        with contextlib.suppress(Exception):
                            reg_id = self._user32.RegisterClipboardFormatW(reg_name)
                            if isinstance(reg_id, int) and reg_id > 0:
                                candidates.append(reg_id)
                for cand in candidates:
                    with contextlib.suppress(Exception):
                        if self._user32.IsClipboardFormatAvailable(cand):
                            format_ids.append(cand)

            for fmt_id in format_ids:
                if fmt_id in _NON_HGLOBAL_CLIPBOARD_FORMATS or fmt_id in exclusion_ids:
                    continue
                if 0x0300 <= fmt_id <= 0x03FF:
                    continue
                h_data = None
                with contextlib.suppress(Exception):
                    h_data = self._user32.GetClipboardData(fmt_id)
                if not h_data:
                    continue
                raw = None
                with contextlib.suppress(Exception):
                    raw = self._read_global_bytes(h_data, f"fmt:{fmt_id}")
                if raw is None:
                    continue
                if total_bytes + len(raw) > max_total_bytes:
                    logger.warning(
                        "Skipping clipboard format %d (%d bytes) exceeding capture budget.",
                        fmt_id,
                        len(raw),
                    )
                    continue
                captured[fmt_id] = raw
                total_bytes += len(raw)

        return captured

    def write_text_transient(
        self,
        text: str,
        *,
        attempts: int = 5,
        retry_delay_s: float = 0.02,
    ) -> None:
        """Write Unicode text transiently and mark it excluded from Windows Clipboard History."""
        if not self._user32 or not self._kernel32:
            raise RuntimeError("Win32 clipboard APIs are not available.")

        encoded = (text + "\0").encode("utf-16le")
        with self.open_clipboard(attempts=attempts, retry_delay_s=retry_delay_s):
            self._user32.EmptyClipboard()
            if not self._set_raw_format_locked(CF_UNICODETEXT, encoded):
                raise RuntimeError("Failed to set transient clipboard text data.")
            self._mark_exclude_from_history_locked()

    def restore_raw_formats(
        self,
        saved: dict[int, bytes],
        *,
        exclude_history: bool = True,
        attempts: int = 5,
        retry_delay_s: float = 0.02,
    ) -> None:
        """Restore captured HGLOBAL clipboard formats, optionally hiding from Win+V."""
        if not self._user32 or not self._kernel32:
            raise RuntimeError("Win32 clipboard APIs are not available.")

        with self.open_clipboard(attempts=attempts, retry_delay_s=retry_delay_s):
            self._user32.EmptyClipboard()
            for fmt_id, raw_bytes in saved.items():
                if not self._set_raw_format_locked(int(fmt_id), raw_bytes):
                    logger.warning("Failed to restore clipboard format %s.", fmt_id)
            if exclude_history:
                self._mark_exclude_from_history_locked()


# ------------------------------------------------ Local Extension & Collision Detection


def _detect_text_extension(content: str) -> str:
    """Detect file extension (.json, .py, .md, .txt) from text heuristics."""
    stripped = content.strip()
    if not stripped:
        return ".txt"

    # 1. JSON check: must parse into dict or list
    try:
        parsed = json.loads(stripped)
        if isinstance(parsed, (dict, list)):
            return ".json"
    except Exception:
        pass

    # 2. Markdown check: code fences, headings, quotes, bullet lists, markdown links
    if re.search(r"^```\w*", stripped, re.MULTILINE):
        return ".md"
    md_patterns = [
        re.compile(r"^#{1,6}\s+\S+", re.MULTILINE),
        re.compile(r"^>\s+\S+", re.MULTILINE),
        re.compile(r"^[-*]\s+\S+", re.MULTILINE),
        re.compile(r"\[.*?\]\(https?://.*?\)", re.MULTILINE),
    ]
    if sum(1 for pat in md_patterns if pat.search(stripped)) >= 2:
        return ".md"

    # 3. Python code check
    py_patterns = [
        re.compile(r"^\s*def\s+\w+\s*\(", re.MULTILINE),
        re.compile(r"^\s*class\s+\w+.*?:", re.MULTILINE),
        re.compile(r"^\s*(?:import\s+\w+|from\s+\w+\s+import\s+\w+)", re.MULTILINE),
        re.compile(r'if\s+__name__\s*==\s*[\'"]__main__[\'"]:', re.MULTILINE),
        re.compile(r"^\s*@(dataclass|property|classmethod|staticmethod|override)\b", re.MULTILINE),
    ]
    if sum(1 for pat in py_patterns if pat.search(stripped)) >= 1:
        return ".py"

    if any(pat.search(stripped) for pat in md_patterns):
        return ".md"

    return ".txt"


def _resolve_collision(path: Path) -> Path:
    """Return non-colliding path by appending -1, -2 if path already exists."""
    if not path.exists():
        return path
    parent = path.parent
    stem = path.stem
    suffix = path.suffix
    counter = 1
    while True:
        candidate = parent / f"{stem}-{counter}{suffix}"
        if not candidate.exists():
            return candidate
        counter += 1


def _clean_single_url(url: str) -> tuple[str, bool]:
    """Strip marketing/tracking query parameters from a single URL."""
    parsed = urllib.parse.urlsplit(url)
    if not parsed.query:
        return url, False
    query_pairs = urllib.parse.parse_qsl(parsed.query, keep_blank_values=True)
    kept: list[tuple[str, str]] = []
    modified = False
    for k, v in query_pairs:
        lower_k = k.lower()
        if lower_k.startswith("utm_") or lower_k in TRACKING_PARAMS:
            modified = True
        else:
            kept.append((k, v))
    if not modified:
        return url, False
    new_query = urllib.parse.urlencode(kept)
    cleaned = urllib.parse.urlunsplit(
        (
            parsed.scheme,
            parsed.netloc,
            parsed.path,
            new_query,
            parsed.fragment,
        )
    )
    return cleaned, True


# --------------------------------------------------------------------------- ClipboardService


class ClipboardService:
    """Service tracking clipboard sequence changes and executing local deterministic transforms."""

    def __init__(
        self,
        backend: ClipboardBackend | None = None,
        default_save_dir: Path | None = None,
    ) -> None:
        self._backend = backend or Win32ClipboardBackend()
        self.default_save_dir = default_save_dir
        self.last_saved_path: Path | None = None
        self._last_sequence: int | None = None
        self._last_changed_monotonic: float = time.monotonic()

    @property
    def backend(self) -> ClipboardBackend:
        """Return the underlying clipboard backend."""
        return self._backend

    def poll_sequence(self) -> int:
        """Poll the clipboard sequence number without reading data to track change timestamps."""
        seq = self._backend.get_sequence_number()
        if self._last_sequence is None:
            self._last_sequence = seq
            self._last_changed_monotonic = time.monotonic()
        elif seq != self._last_sequence:
            self._last_sequence = seq
            self._last_changed_monotonic = time.monotonic()
        return seq

    def seconds_since_last_change(self) -> float:
        """Return elapsed seconds since the clipboard sequence number last changed."""
        if self._last_sequence is None:
            self.poll_sequence()
        return time.monotonic() - self._last_changed_monotonic

    # ----------------------------------------------------------------------- Transforms

    def format_json(
        self,
        minify: bool = False,
        update_clipboard: bool = True,
    ) -> dict[str, Any]:
        """Parse JSON from clipboard text, format with indent=2 or compact, and update."""
        snapshot = self._backend.read_snapshot()
        if snapshot.kind != "text" or snapshot.text is None:
            return {
                "status": "error",
                "action": "minify_json" if minify else "format_json",
                "error": "Clipboard does not contain text to parse as JSON.",
            }

        raw = snapshot.text.strip()
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            return {
                "status": "error",
                "action": "minify_json" if minify else "format_json",
                "error": f"Invalid JSON: {exc.msg} (line {exc.lineno}, column {exc.colno}).",
            }

        if minify:
            formatted = json.dumps(parsed, separators=(",", ":"), ensure_ascii=False)
        else:
            formatted = json.dumps(parsed, indent=2, ensure_ascii=False)

        if update_clipboard:
            self._backend.write_text(formatted)

        preview = formatted[:1000] + ("..." if len(formatted) > 1000 else "")
        return {
            "status": "ok",
            "action": "minify_json" if minify else "format_json",
            "updated_clipboard": update_clipboard,
            "original_chars": len(snapshot.text),
            "formatted_chars": len(formatted),
            "preview": preview,
        }

    def clean_text(
        self,
        unwrap_lines: bool = False,
        update_clipboard: bool = True,
    ) -> dict[str, Any]:
        """Strip ANSI escapes, normalize newlines, trim trailing whitespace, collapse blanks."""
        snapshot = self._backend.read_snapshot()
        if snapshot.kind != "text" or snapshot.text is None:
            return {
                "status": "error",
                "action": "unwrap_text" if unwrap_lines else "clean_text",
                "error": "Clipboard does not contain text to clean.",
            }

        text = snapshot.text
        # 1. Strip ANSI escape codes
        text = ANSI_REGEX.sub("", text)
        # 2. Normalize \r\n to \n
        text = text.replace("\r\n", "\n").replace("\r", "\n")
        # 3. Strip trailing line whitespace
        lines = [line.rstrip() for line in text.split("\n")]
        text = "\n".join(lines)
        # 4. Collapse 3+ blank lines to 2 (4+ consecutive newlines -> 3 newlines)
        text = re.sub(r"\n{4,}", "\n\n\n", text)

        # 5. Unwrap hard-wrapped lines if requested
        if unwrap_lines:
            paragraphs = re.split(r"\n{2,}", text)
            unwrapped_paras: list[str] = []
            for p in paragraphs:
                p_lines = [line.strip() for line in p.split("\n") if line.strip()]
                if p_lines:
                    unwrapped_paras.append(" ".join(p_lines))
                else:
                    unwrapped_paras.append("")
            text = "\n\n".join(unwrapped_paras)

        if update_clipboard:
            self._backend.write_text(text)

        preview = text[:1000] + ("..." if len(text) > 1000 else "")
        return {
            "status": "ok",
            "action": "unwrap_text" if unwrap_lines else "clean_text",
            "updated_clipboard": update_clipboard,
            "original_chars": len(snapshot.text),
            "cleaned_chars": len(text),
            "preview": preview,
        }

    def strip_tracking_urls(
        self,
        update_clipboard: bool = True,
    ) -> dict[str, Any]:
        """Find http/https URLs in clipboard text, strip tracking query parameters, and update."""
        snapshot = self._backend.read_snapshot()
        if snapshot.kind != "text" or snapshot.text is None:
            return {
                "status": "error",
                "action": "strip_tracking_urls",
                "error": "Clipboard does not contain text to inspect for URLs.",
            }

        text = snapshot.text
        cleaned_urls: list[dict[str, str]] = []
        modified = False

        def _replace_match(match: re.Match[str]) -> str:
            nonlocal modified
            raw_url = match.group(0)
            trailing = ""
            while raw_url and raw_url[-1] in ".,);:!?'\"":
                trailing = raw_url[-1] + trailing
                raw_url = raw_url[:-1]

            clean_url, was_changed = _clean_single_url(raw_url)
            if was_changed:
                modified = True
                cleaned_urls.append({"original": raw_url, "cleaned": clean_url})
                return clean_url + trailing
            return match.group(0)

        cleaned_text = URL_REGEX.sub(_replace_match, text)

        if modified and update_clipboard:
            self._backend.write_text(cleaned_text)

        preview = cleaned_text[:1000] + ("..." if len(cleaned_text) > 1000 else "")
        return {
            "status": "ok",
            "action": "strip_tracking_urls",
            "updated_clipboard": update_clipboard and modified,
            "urls_cleaned_count": len(cleaned_urls),
            "cleaned_urls": cleaned_urls,
            "preview": preview,
        }

    def save_to_file(
        self,
        filename: str | None = None,
        directory: Literal["desktop", "documents"] = "desktop",
    ) -> dict[str, Any]:
        """Save clipboard text or image to a file with collision avoidance."""
        if self.default_save_dir is not None:
            sub = self.default_save_dir / directory
            target_dir = sub if sub.is_dir() else self.default_save_dir
        else:
            folder_name = "Desktop" if directory == "desktop" else "Documents"
            target_dir = Path.home() / folder_name
        target_dir.mkdir(parents=True, exist_ok=True)

        # Sanitize filename against path traversal
        clean_name: str | None = None
        if filename:
            name_only = Path(filename).name
            name_only = name_only.replace("..", "").strip()
            if name_only and name_only not in (".", ".."):
                clean_name = name_only

        snapshot = self._backend.read_snapshot()
        if snapshot.kind == "empty":
            return {"status": "error", "error": "Clipboard is empty, nothing to save."}

        # Image or copied image files
        if snapshot.kind == "image" or (snapshot.kind == "files" and snapshot.has_image_files):
            img_bytes = snapshot.image_bytes
            if snapshot.kind == "files" and not img_bytes:
                img_path = next(
                    p for p in snapshot.file_paths if p.lower().endswith(IMAGE_EXTENSIONS)
                )
                try:
                    from PIL import Image

                    with Image.open(img_path) as im:
                        buf = io.BytesIO()
                        im.save(buf, format="PNG")
                        img_bytes = buf.getvalue()
                except Exception as exc:
                    return {
                        "status": "error",
                        "error": f"Failed reading copied image file {img_path}: {exc}",
                    }

            if not img_bytes:
                return {"status": "error", "error": "Clipboard image bytes are unavailable."}

            if clean_name:
                if not clean_name.lower().endswith(".png"):
                    clean_name = f"{Path(clean_name).stem}.png"
            else:
                clean_name = f"clipboard_{int(time.time())}.png"

            final_path = _resolve_collision(target_dir / clean_name)
            final_path.write_bytes(img_bytes)
            saved_path = final_path.resolve()
            if not saved_path.is_file():
                raise RuntimeError(
                    f"Clipboard image write completed but file was not found at {saved_path}."
                )
            self.last_saved_path = saved_path
            return {
                "status": "saved",
                "saved_path": str(saved_path),
                "path": str(saved_path),
                "filename": saved_path.name,
                "size_bytes": len(img_bytes),
                "kind": "image",
            }

        # Text content or file path listings
        if snapshot.kind == "files":
            content = "\n".join(snapshot.file_paths)
        else:
            content = snapshot.text or ""

        if clean_name and Path(clean_name).suffix:
            final_filename = clean_name
        else:
            stem = Path(clean_name).stem if clean_name else f"clipboard_{int(time.time())}"
            ext = _detect_text_extension(content)
            final_filename = f"{stem}{ext}"

        final_path = _resolve_collision(target_dir / final_filename)
        final_path.write_text(content, encoding="utf-8")
        saved_path = final_path.resolve()
        if not saved_path.is_file():
            raise RuntimeError(
                f"Clipboard text write completed but file was not found at {saved_path}."
            )
        self.last_saved_path = saved_path
        return {
            "status": "saved",
            "saved_path": str(saved_path),
            "path": str(saved_path),
            "filename": saved_path.name,
            "size_bytes": len(content.encode("utf-8")),
            "kind": "text",
        }

    @staticmethod
    def _open_resolved_target(
        target_str: str,
        *,
        kind: Literal["file", "url"],
        browser_fallback_url: str,
        filename: str | None = None,
        opener_fn: Callable[[str], Any] | None = None,
        desktop_name_fn: Callable[[], str | None] | None = None,
    ) -> dict[str, Any]:
        """Verify an interactive desktop and open a validated file path or HTTP/HTTPS URL."""
        from clea.tools.windows_os import _check_interactive_desktop, _open_via_shell

        non_default = _check_interactive_desktop(desktop_name_fn)
        if non_default is not None:
            res_non_interactive: dict[str, Any] = {
                "ok": False,
                "status": "non_interactive_desktop",
                "kind": kind,
                "target": target_str,
                "error": (
                    f"Cannot open '{target_str}' visibly because the daemon is running on "
                    f"isolated desktop '{non_default}' instead of 'WinSta0\\Default'. Start "
                    "the Clea daemon from an interactive Windows Terminal / PowerShell session."
                ),
            }
            if filename is not None:
                res_non_interactive["filename"] = filename
            return res_non_interactive

        try:
            if opener_fn is not None:
                opener_fn(target_str)
            elif sys.platform == "win32":
                _open_via_shell(target_str)
            else:
                webbrowser.open(browser_fallback_url)
        except Exception as exc:
            logger.warning("Failed opening %s %s: %s", kind, target_str, exc)
            return {"status": "error", "error": f"Failed to open {kind}: {exc}"}

        opened_res: dict[str, Any] = {
            "status": "opened",
            "kind": kind,
            "target": target_str,
        }
        if filename is not None:
            opened_res["filename"] = filename
        return opened_res

    def open_path_or_url(
        self,
        target: str | None = None,
        *,
        use_clipboard: bool = False,
        opener_fn: Callable[[str], Any] | None = None,
        desktop_name_fn: Callable[[], str | None] | None = None,
    ) -> dict[str, Any]:
        """Open a local file (Desktop/Documents/last saved) or http/https URL on Windows."""
        candidate = target.strip() if isinstance(target, str) else ""
        if use_clipboard:
            snapshot = self._backend.read_snapshot()
            if snapshot.kind == "text" and snapshot.text and snapshot.text.strip():
                url_match = URL_REGEX.search(snapshot.text)
                if url_match:
                    candidate = url_match.group(0).rstrip(".,);:!?'\"")
                else:
                    candidate = snapshot.text.strip()
        elif not candidate:
            if self.last_saved_path is not None and self.last_saved_path.is_file():
                candidate = str(self.last_saved_path)
            else:
                snapshot = self._backend.read_snapshot()
                if snapshot.kind == "text" and snapshot.text and snapshot.text.strip():
                    url_match = URL_REGEX.search(snapshot.text)
                    if url_match:
                        candidate = url_match.group(0).rstrip(".,);:!?'\"")
                    else:
                        candidate = snapshot.text.strip()

        if not candidate:
            return {
                "status": "error",
                "error": (
                    "No target path or URL provided, and clipboard does not contain a "
                    "URL or file path."
                ),
            }

        if any(ord(ch) < 32 for ch in candidate):
            return {"status": "error", "error": "Target contains invalid control characters."}

        # Open http/https URL
        lower = candidate.lower()
        if lower.startswith(("http://", "https://")):
            parsed = urllib.parse.urlsplit(candidate)
            if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
                return {"status": "error", "error": f"Invalid HTTP/HTTPS URL: {candidate}"}
            return self._open_resolved_target(
                candidate,
                kind="url",
                browser_fallback_url=candidate,
                opener_fn=opener_fn,
                desktop_name_fn=desktop_name_fn,
            )

        # Reject shell binaries
        first_tok = re.split(r"[\s/\\]", lower)[0]
        if first_tok in _DISALLOWED_OPEN_BINARIES:
            return {"status": "error", "error": f"Opening executable '{first_tok}' is disallowed."}

        # Resolve local file path
        raw_path = Path(candidate).expanduser()
        resolved_file: Path | None = None
        if raw_path.is_absolute() and raw_path.is_file():
            resolved_file = raw_path.resolve()
        else:
            candidates: list[Path] = []
            if self.default_save_dir is not None:
                candidates.append(self.default_save_dir / raw_path.name)
                candidates.append(self.default_save_dir / candidate)
            home = Path.home()
            candidates.extend(
                [
                    home / "Desktop" / candidate,
                    home / "Desktop" / raw_path.name,
                    home / "Documents" / candidate,
                    home / "Documents" / raw_path.name,
                    home / candidate,
                ]
            )
            if (
                self.last_saved_path is not None
                and self.last_saved_path.name.lower() == raw_path.name.lower()
            ):
                candidates.append(self.last_saved_path)

            for cand in candidates:
                with contextlib.suppress(OSError):
                    if cand.is_file():
                        resolved_file = cand.resolve()
                        break

        if resolved_file is None or not resolved_file.is_file():
            return {"status": "error", "error": f"File not found on disk: {candidate}"}

        return self._open_resolved_target(
            str(resolved_file),
            kind="file",
            browser_fallback_url=resolved_file.as_uri(),
            filename=resolved_file.name,
            opener_fn=opener_fn,
            desktop_name_fn=desktop_name_fn,
        )


# ---------------------------------------------------- Tool Handlers & Registry

_default_clipboard_service: ClipboardService | None = None


def get_default_clipboard_service() -> ClipboardService:
    """Return the global default ClipboardService instance."""
    global _default_clipboard_service
    if _default_clipboard_service is None:
        _default_clipboard_service = ClipboardService()
    return _default_clipboard_service


def set_default_clipboard_service(service: ClipboardService | None) -> None:
    """Set or reset the global default ClipboardService instance."""
    global _default_clipboard_service
    _default_clipboard_service = service


def read_clipboard(
    max_chars: int = 4000,
    *,
    clipboard_service: ClipboardService | None = None,
) -> dict[str, Any]:
    """Inspect current clipboard content type, dimensions or line/char counts, and text preview."""
    service = clipboard_service or get_default_clipboard_service()
    snapshot = service.backend.read_snapshot()
    cap = max(1, min(int(max_chars), 4000))

    if snapshot.kind == "text":
        text = snapshot.text or ""
        truncated = len(text) > cap
        preview = text[:cap] + ("... [truncated]" if truncated else "")
        return {
            "status": "ok",
            "kind": "text",
            "char_count": snapshot.char_count,
            "line_count": snapshot.line_count,
            "truncated": truncated,
            "preview": preview,
        }
    elif snapshot.kind == "image":
        res: dict[str, Any] = {
            "status": "ok",
            "kind": "image",
            "image_format": snapshot.image_format or "PNG",
            "dimensions": snapshot.image_dimensions,
            "size_bytes": len(snapshot.image_bytes) if snapshot.image_bytes else 0,
        }
        if snapshot.image_dimensions is not None:
            res["width"] = snapshot.image_dimensions[0]
            res["height"] = snapshot.image_dimensions[1]
        return res
    elif snapshot.kind == "files":
        return {
            "status": "ok",
            "kind": "files",
            "file_count": len(snapshot.file_paths),
            "file_paths": snapshot.file_paths,
            "has_image_files": snapshot.has_image_files,
        }
    else:
        return {
            "status": "ok",
            "kind": "empty",
            "message": "Clipboard is empty or contains an unsupported format.",
        }


def transform_clipboard(
    action: Literal[
        "format_json", "minify_json", "clean_text", "unwrap_text", "strip_tracking_urls"
    ],
    update_clipboard: bool = True,
    *,
    clipboard_service: ClipboardService | None = None,
) -> dict[str, Any]:
    """Execute a deterministic local transformation on clipboard contents."""
    service = clipboard_service or get_default_clipboard_service()
    if action == "format_json":
        return service.format_json(minify=False, update_clipboard=update_clipboard)
    elif action == "minify_json":
        return service.format_json(minify=True, update_clipboard=update_clipboard)
    elif action == "clean_text":
        return service.clean_text(unwrap_lines=False, update_clipboard=update_clipboard)
    elif action == "unwrap_text":
        return service.clean_text(unwrap_lines=True, update_clipboard=update_clipboard)
    elif action == "strip_tracking_urls":
        return service.strip_tracking_urls(update_clipboard=update_clipboard)
    else:
        return {
            "status": "error",
            "error": (
                f"Unsupported action: '{action}'. Choose from: "
                "format_json, minify_json, clean_text, unwrap_text, strip_tracking_urls."
            ),
        }


def save_clipboard_to_file(
    filename: str | None = None,
    directory: Literal["desktop", "documents"] = "desktop",
    *,
    clipboard_service: ClipboardService | None = None,
) -> dict[str, Any]:
    """Save current clipboard text, code, or image to a file in desktop or documents."""
    service = clipboard_service or get_default_clipboard_service()
    return service.save_to_file(filename=filename, directory=directory)


def open_path_or_url(
    target: str | None = None,
    use_clipboard: bool = False,
    *,
    clipboard_service: ClipboardService | None = None,
    opener_fn: Callable[[str], Any] | None = None,
    desktop_name_fn: Callable[[], str | None] | None = None,
) -> dict[str, Any]:
    """Open a saved local file (e.g. Desktop/Documents) or http/https URL on Windows."""
    service = clipboard_service or get_default_clipboard_service()
    return service.open_path_or_url(
        target=target,
        use_clipboard=use_clipboard,
        opener_fn=opener_fn,
        desktop_name_fn=desktop_name_fn,
    )


def register_clipboard_tools(
    registry: ToolRegistry,
    clipboard_service: ClipboardService | None = None,
    *,
    read_risk: RiskLevel = RiskLevel.READ_ONLY,
    transform_risk: RiskLevel = RiskLevel.READ_ONLY,
    save_risk: RiskLevel = RiskLevel.READ_ONLY,
    open_risk: RiskLevel = RiskLevel.READ_ONLY,
) -> None:
    """Register clipboard inspection, transform, save, and open tools in ToolRegistry."""
    service = clipboard_service or get_default_clipboard_service()

    registry.register(
        name="read_clipboard",
        description=(
            "Inspect current clipboard content type (text, image, files, empty), character/line "
            "count or image dimensions, and text preview (capped safely at 4000 characters)."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "max_chars": {
                    "type": "integer",
                    "default": 4000,
                    "description": "Maximum characters of text preview to return (capped at 4000).",
                },
            },
            "additionalProperties": False,
        },
        risk_level=read_risk,
        handler=lambda max_chars=4000: read_clipboard(
            max_chars=max_chars, clipboard_service=service
        ),
    )

    registry.register(
        name="transform_clipboard",
        description=(
            "Run local deterministic transforms on clipboard content: format_json (pretty-print), "
            "minify_json (compact), clean_text (strip ANSI, trailing whitespace, excess blanks), "
            "unwrap_text (join wrapped lines into paragraphs), or strip_tracking_urls."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "action": {
                    "type": "string",
                    "enum": [
                        "format_json",
                        "minify_json",
                        "clean_text",
                        "unwrap_text",
                        "strip_tracking_urls",
                    ],
                    "description": "Transform action to execute.",
                },
                "update_clipboard": {
                    "type": "boolean",
                    "default": True,
                    "description": "Whether to write the transformed result back to clipboard.",
                },
            },
            "required": ["action"],
            "additionalProperties": False,
        },
        risk_level=transform_risk,
        handler=lambda action, update_clipboard=True: transform_clipboard(
            action=action, update_clipboard=update_clipboard, clipboard_service=service
        ),
    )

    registry.register(
        name="save_clipboard_to_file",
        description=(
            "Directly save the current Windows clipboard content (text, URLs, code, or image "
            "screenshot) to a file on Desktop or Documents with auto-detected extension "
            "(.json, .py, .md, .txt, .png) and collision-safe naming. Always invoke this tool "
            "directly whenever the user asks to save their clipboard or copied item to a file "
            "(e.g., 'save my clipboard to desktop', 'save what I copied as youtube_url.txt')."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "filename": {
                    "type": "string",
                    "description": "Optional custom filename. Auto-detects extension if omitted.",
                },
                "directory": {
                    "type": "string",
                    "enum": ["desktop", "documents"],
                    "default": "desktop",
                    "description": "Target directory to save into: 'desktop' or 'documents'.",
                },
            },
            "additionalProperties": False,
        },
        risk_level=save_risk,
        handler=lambda filename=None, directory="desktop": save_clipboard_to_file(
            filename=filename, directory=directory, clipboard_service=service
        ),
    )

    registry.register(
        name="open_path_or_url",
        description=(
            "Open a saved local file (e.g., on Desktop or Documents, or the most recently saved "
            "clipboard file) or an explicit http/https URL (provided or extracted from the "
            "clipboard) using Windows default application / browser. For opening services that "
            "may be installed as native/Store/PWA apps (e.g. YouTube, Spotify, WhatsApp), prefer "
            "open_or_focus_app first unless the user explicitly asked for the browser/web URL."
        ),
        input_schema={
            "type": "object",
            "properties": {
                "target": {
                    "type": "string",
                    "description": (
                        "Optional file path, filename (e.g. 'print.txt'), or http/https URL. "
                        "If omitted, uses the clipboard URL/path or most recently saved file."
                    ),
                },
                "use_clipboard": {
                    "type": "boolean",
                    "default": False,
                    "description": (
                        "When true, reads the URL or path directly from the active clipboard."
                    ),
                },
            },
            "additionalProperties": False,
        },
        risk_level=open_risk,
        handler=lambda target=None, use_clipboard=False: open_path_or_url(
            target=target, use_clipboard=use_clipboard, clipboard_service=service
        ),
    )

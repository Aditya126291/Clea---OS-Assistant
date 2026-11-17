"""Lock-resilient Windows clipboard service, deterministic transforms, and MCP tools."""

from __future__ import annotations

import contextlib
import ctypes
import io
import json
import logging
import re
import struct
import sys
import time
import urllib.parse
from collections.abc import Iterator
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
        except Exception:
            pass
    try:
        from PIL import Image

        with Image.open(io.BytesIO(png_bytes)) as img:
            return img.size
    except Exception:
        return None


def _convert_dib_to_png(dib_bytes: bytes) -> tuple[bytes | None, tuple[int, int] | None]:
    """Convert CF_DIB bytes to lossless PNG bytes and return (png_bytes, dimensions)."""
    try:
        from PIL import BmpImagePlugin, Image

        with BmpImagePlugin.DibImageFile(io.BytesIO(dib_bytes)) as dib_img:
            buf = io.BytesIO()
            dib_img.save(buf, format="PNG")
            return buf.getvalue(), dib_img.size
    except Exception:
        pass

    try:
        from PIL import Image

        bi_size = struct.unpack_from("<I", dib_bytes, 0)[0]
        bit_count = struct.unpack_from("<H", dib_bytes, 14)[0]
        clr_used = struct.unpack_from("<I", dib_bytes, 32)[0] if bi_size >= 36 else 0
        num_colors = clr_used if clr_used else (1 << bit_count if bit_count <= 8 else 0)
        off_bits = 14 + bi_size + (num_colors * 4)
        bmp_header = struct.pack("<2sIHHI", b"BM", 14 + len(dib_bytes), 0, 0, off_bits)
        with Image.open(io.BytesIO(bmp_header + dib_bytes)) as img:
            buf = io.BytesIO()
            img.save(buf, format="PNG")
            return buf.getvalue(), img.size
    except Exception as exc:
        logger.debug("Failed converting DIB bytes to PNG: %s", exc)
        return None, None


class Win32ClipboardBackend:
    """Win32 ctypes implementation with lock contention retries and priority format detection."""

    def __init__(
        self,
        user32: Any = None,
        kernel32: Any = None,
        shell32: Any = None,
    ) -> None:
        if sys.platform == "win32":
            self._user32 = user32 or getattr(ctypes.windll, "user32", None)
            self._kernel32 = kernel32 or getattr(ctypes.windll, "kernel32", None)
            self._shell32 = shell32 or getattr(ctypes.windll, "shell32", None)
        else:
            self._user32 = user32
            self._kernel32 = kernel32
            self._shell32 = shell32

    def get_sequence_number(self) -> int:
        """Call user32.GetClipboardSequenceNumber() reading only the serial integer."""
        if not self._user32 or not hasattr(self._user32, "GetClipboardSequenceNumber"):
            return 0
        try:
            return int(self._user32.GetClipboardSequenceNumber())
        except Exception as exc:
            logger.debug("Error getting clipboard sequence number: %s", exc)
            return 0

    @contextmanager
    def open_clipboard(self) -> Iterator[None]:
        """Context manager attempting OpenClipboard up to 3 times with 25ms backoff."""
        if not self._user32 or not hasattr(self._user32, "OpenClipboard"):
            raise RuntimeError("Win32 user32.OpenClipboard is not available.")

        opened = False
        attempts = 3
        for attempt in range(attempts):
            try:
                res = self._user32.OpenClipboard(None)
            except Exception:
                res = 0
            if res:
                opened = True
                break
            if attempt < attempts - 1:
                time.sleep(0.025)

        if not opened:
            raise ClipboardLockedError(
                "Windows clipboard is currently locked by another application after 3 attempts."
            )

        try:
            yield
        finally:
            if hasattr(self._user32, "CloseClipboard"):
                with contextlib.suppress(Exception):
                    self._user32.CloseClipboard()

    def read_snapshot(self) -> ClipboardSnapshot:
        """Read clipboard content according to format priority."""
        if not self._user32 or not self._kernel32:
            return ClipboardSnapshot(kind="empty")

        with self.open_clipboard():
            # 1. Registered PNG format
            cf_png = (
                self._user32.RegisterClipboardFormatW("PNG")
                if hasattr(self._user32, "RegisterClipboardFormatW")
                else 0
            )
            if (
                cf_png
                and hasattr(self._user32, "IsClipboardFormatAvailable")
                and self._user32.IsClipboardFormatAvailable(cf_png)
            ):
                h_data = self._user32.GetClipboardData(cf_png)
                if h_data:
                    p_data = self._kernel32.GlobalLock(h_data)
                    if p_data:
                        try:
                            size = self._kernel32.GlobalSize(h_data)
                            png_bytes = ctypes.string_at(p_data, size)
                            dims = _parse_png_dimensions(png_bytes)
                            return ClipboardSnapshot(
                                kind="image",
                                image_bytes=png_bytes,
                                image_format="PNG",
                                image_dimensions=dims,
                            )
                        finally:
                            self._kernel32.GlobalUnlock(h_data)

            # 2. CF_DIB (8)
            if (
                hasattr(self._user32, "IsClipboardFormatAvailable")
                and self._user32.IsClipboardFormatAvailable(CF_DIB)
            ):
                h_data = self._user32.GetClipboardData(CF_DIB)
                if h_data:
                    p_data = self._kernel32.GlobalLock(h_data)
                    if p_data:
                        try:
                            size = self._kernel32.GlobalSize(h_data)
                            dib_bytes = ctypes.string_at(p_data, size)
                            png_bytes, dims = _convert_dib_to_png(dib_bytes)
                            if png_bytes:
                                return ClipboardSnapshot(
                                    kind="image",
                                    image_bytes=png_bytes,
                                    image_format="PNG",
                                    image_dimensions=dims,
                                )
                        finally:
                            self._kernel32.GlobalUnlock(h_data)

            # Fallback for images: PIL.ImageGrab if available
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
            except Exception:
                pass

            # 3. CF_HDROP (15) - Copied files
            if (
                hasattr(self._user32, "IsClipboardFormatAvailable")
                and self._user32.IsClipboardFormatAvailable(CF_HDROP)
            ):
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
            if (
                hasattr(self._user32, "IsClipboardFormatAvailable")
                and self._user32.IsClipboardFormatAvailable(CF_UNICODETEXT)
            ):
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

            # 5. Empty/unsupported
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
            return {
                "status": "ok",
                "path": str(final_path.resolve()),
                "filename": final_path.name,
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
        return {
            "status": "ok",
            "path": str(final_path.resolve()),
            "filename": final_path.name,
            "size_bytes": len(content.encode("utf-8")),
            "kind": "text",
        }


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
        return {
            "status": "ok",
            "kind": "image",
            "image_format": snapshot.image_format or "PNG",
            "dimensions": snapshot.image_dimensions,
            "size_bytes": len(snapshot.image_bytes) if snapshot.image_bytes else 0,
        }
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


def register_clipboard_tools(
    registry: ToolRegistry,
    clipboard_service: ClipboardService | None = None,
    *,
    read_risk: RiskLevel = RiskLevel.READ_ONLY,
    transform_risk: RiskLevel = RiskLevel.READ_ONLY,
    save_risk: RiskLevel = RiskLevel.REVERSIBLE,
) -> None:
    """Register read_clipboard, transform_clipboard, and save_clipboard_to_file in ToolRegistry."""
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
            "Save current clipboard text, code, or image to a file on desktop or documents "
            "with auto-detected extension (.json, .py, .md, .txt, .png) and collision-safe naming."
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

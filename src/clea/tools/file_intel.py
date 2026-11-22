"""Local file search, zero-app document reading, and safe file operations.

This module provides three tools:
- ``search_files``: Search user directories or explicit paths with filtering and recency sorting.
- ``read_document``: Zero-app document text extraction (.docx, .xlsx, .pdf, .txt, .csv, code).
- ``manage_file``: Safe file operations (move, rename, copy, archive_zip, organize_folder, recycle).
"""

from __future__ import annotations

import fnmatch
import logging
import os
import re
import shutil
import time
import xml.etree.ElementTree as ET
import zipfile
import zlib
from datetime import datetime
from pathlib import Path
from typing import Any

from clea.tools.registry import RiskLevel, ToolRegistry
from clea.tools.subprocess_utils import run_hidden, run_powershell_hidden

logger = logging.getLogger(__name__)

# Directory names to skip during recursive search
SKIP_DIR_NAMES: frozenset[str] = frozenset(
    {
        ".git",
        "node_modules",
        ".venv",
        "venv",
        "__pycache__",
        "$recycle.bin",
        ".pytest_cache",
        ".ruff_cache",
        ".idea",
        ".vscode",
        ".hg",
        ".svn",
    }
)

# File categories for folder organization
FOLDER_ORGANIZATION_CATEGORIES: dict[str, frozenset[str]] = {
    "Documents": frozenset(
        {
            ".pdf",
            ".docx",
            ".doc",
            ".xlsx",
            ".xls",
            ".pptx",
            ".ppt",
            ".txt",
            ".rtf",
            ".csv",
            ".odt",
            ".ods",
            ".odp",
            ".tsv",
            ".epub",
        }
    ),
    "Images": frozenset(
        {
            ".png",
            ".jpg",
            ".jpeg",
            ".gif",
            ".bmp",
            ".svg",
            ".webp",
            ".ico",
            ".tiff",
            ".tif",
            ".heic",
            ".raw",
            ".psd",
        }
    ),
    "Archives": frozenset(
        {
            ".zip",
            ".rar",
            ".7z",
            ".tar",
            ".gz",
            ".bz2",
            ".xz",
            ".iso",
            ".tgz",
        }
    ),
    "Installers": frozenset(
        {
            ".exe",
            ".msi",
            ".dmg",
            ".pkg",
            ".deb",
            ".rpm",
            ".appx",
            ".msix",
        }
    ),
    "Code": frozenset(
        {
            ".py",
            ".js",
            ".ts",
            ".jsx",
            ".tsx",
            ".html",
            ".css",
            ".json",
            ".yaml",
            ".yml",
            ".xml",
            ".sh",
            ".bat",
            ".ps1",
            ".c",
            ".cpp",
            ".h",
            ".rs",
            ".go",
            ".java",
            ".kt",
            ".sql",
            ".toml",
            ".ini",
            ".lua",
            ".dart",
        }
    ),
    "Media": frozenset(
        {
            ".mp4",
            ".mp3",
            ".mkv",
            ".avi",
            ".mov",
            ".wav",
            ".flac",
            ".aac",
            ".m4a",
            ".webm",
            ".ogg",
            ".wmv",
        }
    ),
}

# System files to ignore during folder organization to protect folder settings
IGNORE_ORGANIZATION_FILES: frozenset[str] = frozenset(
    {
        "desktop.ini",
        "thumbs.db",
        ".ds_store",
    }
)


def _resolve_user_directories() -> list[Path]:
    """Resolve Desktop, Downloads, and Documents folders, including OneDrive redirects."""
    candidates: list[Path] = []

    # 1. Check OneDrive environment redirects if present
    for env_var in ("OneDrive", "OneDriveConsumer", "OneDriveCommercial"):
        od_str = os.environ.get(env_var)
        if od_str and od_str.strip():
            od_path = Path(od_str.strip())
            candidates.extend(
                [
                    od_path / "Desktop",
                    od_path / "Documents",
                    od_path / "Downloads",
                ]
            )

    # 2. Check Windows Shell Folders in Registry if on Windows
    if os.name == "nt":
        try:
            import winreg

            sub_key = r"Software\Microsoft\Windows\CurrentVersion\Explorer\User Shell Folders"
            with winreg.OpenKey(winreg.HKEY_CURRENT_USER, sub_key) as key:
                # Desktop, Personal (Documents), Downloads GUIDs
                for val_name in (
                    "Desktop",
                    "Personal",
                    "{374DE290-123F-4565-9164-39C4925E467B}",
                    "{7D83EE9B-2244-4E70-B1F5-5393042AF1E4}",
                ):
                    try:
                        raw_val, _ = winreg.QueryValueEx(key, val_name)
                        if raw_val:
                            expanded = os.path.expandvars(str(raw_val))
                            candidates.append(Path(expanded))
                    except OSError:
                        pass
        except Exception:
            pass

    # 3. Standard user home directories
    home = Path.home()
    candidates.extend(
        [
            home / "Desktop",
            home / "Downloads",
            home / "Documents",
        ]
    )

    # Deduplicate existing directories while preserving order
    existing: list[Path] = []
    seen: set[Path] = set()
    for candidate in candidates:
        try:
            resolved = candidate.resolve()
            if resolved.is_dir() and resolved not in seen:
                seen.add(resolved)
                existing.append(resolved)
        except OSError:
            continue

    return existing


def search_files(
    query: str = "",
    directory: str = "user_dirs",
    extension: str | None = None,
    modified_within_days: int | None = None,
    limit: int = 20,
) -> list[dict[str, Any]]:
    """Search files in user directories or an explicit directory.

    Args:
        query: Substring or glob pattern to match against filename. Empty matches all.
        directory: Directory to search, or 'user_dirs' for Desktop, Downloads, Documents.
        extension: Optional extension filter (e.g. '.pdf' or 'docx').
        modified_within_days: Only include files modified within this many days.
        limit: Maximum number of matches to return (sorted by recency).
    """
    search_roots: list[Path] = []
    dir_str = "" if directory is None else str(directory).strip()
    if not dir_str or dir_str.lower() == "user_dirs":
        search_roots = _resolve_user_directories()
        if not search_roots:
            search_roots = [Path.home()]
    else:
        expanded = os.path.expandvars(dir_str)
        candidate = Path(expanded).expanduser().resolve()
        if not candidate.exists():
            raise FileNotFoundError(f"Search directory does not exist: {candidate}")
        if not candidate.is_dir():
            raise NotADirectoryError(f"Search path is not a directory: {candidate}")
        search_roots = [candidate]

    # Normalize extension filter
    norm_ext: str | None = None
    if extension is not None:
        ext_clean = extension.strip().lower()
        if ext_clean:
            norm_ext = ext_clean if ext_clean.startswith(".") else f".{ext_clean}"

    # Calculate modified cutoff time
    cutoff_time: float | None = None
    if modified_within_days is not None:
        if modified_within_days < 0:
            raise ValueError(
                f"modified_within_days must be non-negative, got: {modified_within_days}"
            )
        cutoff_time = time.time() - (modified_within_days * 86400.0)

    # Prepare query matching
    query_str = (query or "").strip().lower()
    has_glob = bool(
        "*" in query_str or "?" in query_str or ("[" in query_str and "]" in query_str)
    )

    matched_items: list[tuple[float, dict[str, Any]]] = []
    seen_paths: set[Path] = set()

    for root in search_roots:
        for dirpath, dirnames, filenames in os.walk(root, topdown=True, onerror=None):
            # Prune noisy directories in-place so os.walk does not descend
            dirnames[:] = [
                d
                for d in dirnames
                if d.lower() not in SKIP_DIR_NAMES and not d.lower().startswith("$recycle.bin")
            ]

            for fname in filenames:
                fname_lower = fname.lower()

                # Query filter
                if query_str:
                    if has_glob:
                        if not fnmatch.fnmatch(fname_lower, query_str):
                            continue
                    else:
                        if query_str not in fname_lower:
                            continue

                # Extension filter
                if norm_ext and not fname_lower.endswith(norm_ext):
                    continue

                full_path = Path(dirpath) / fname
                try:
                    resolved_path = full_path.resolve()
                    if resolved_path in seen_paths:
                        continue
                    stat = resolved_path.stat()
                except OSError:
                    continue

                # Recency filter
                if cutoff_time is not None and stat.st_mtime < cutoff_time:
                    continue

                seen_paths.add(resolved_path)
                mtime_iso = datetime.fromtimestamp(stat.st_mtime).isoformat(timespec="seconds")
                item = {
                    "path": str(resolved_path),
                    "name": resolved_path.name,
                    "size": stat.st_size,
                    "modified": mtime_iso,
                    "extension": resolved_path.suffix.lower(),
                }
                matched_items.append((stat.st_mtime, item))

    # Sort descending by modification time (most recent first)
    matched_items.sort(key=lambda x: x[0], reverse=True)

    limit_val = max(1, limit) if limit is not None else 20
    return [item for _, item in matched_items[:limit_val]]


def _read_text_file(
    file_path: Path,
    section: str | int | None = None,
) -> tuple[str, str, dict[str, Any]]:
    """Read plain text, markdown, csv, or code files with encodings and line slicing.

    Returns:
        tuple of (file_type, sliced_text, line_info)
    """
    suffix = file_path.suffix.lower()
    is_csv = suffix in (".csv", ".tsv")
    if is_csv:
        file_type = "csv"
    elif suffix in (".json", ".jsonl"):
        file_type = "json"
    elif suffix in (".md", ".markdown"):
        file_type = "markdown"
    elif suffix in (".log",):
        file_type = "log"
    elif suffix in (
        ".py",
        ".js",
        ".ts",
        ".jsx",
        ".tsx",
        ".html",
        ".css",
        ".sql",
        ".sh",
        ".ps1",
        ".bat",
        ".toml",
        ".yaml",
        ".yml",
        ".xml",
        ".c",
        ".cpp",
        ".h",
        ".rs",
        ".go",
    ):
        file_type = "code"
    else:
        file_type = "text"

    raw_bytes = file_path.read_bytes()
    try:
        text = raw_bytes.decode("utf-8")
    except UnicodeDecodeError:
        try:
            text = raw_bytes.decode("latin-1")
        except UnicodeDecodeError:
            text = raw_bytes.decode("utf-8", errors="replace")

    text = text.replace("\r\n", "\n").replace("\r", "\n")
    all_lines = text.splitlines()
    total_lines = len(all_lines)

    lines = all_lines
    if section is not None:
        if isinstance(section, int) or (isinstance(section, str) and section.isdigit()):
            count = int(section)
            if count <= 0:
                raise ValueError(f"Invalid section line count {section}: must be >= 1")
            lines = all_lines[:count]
        elif isinstance(section, str) and "-" in section:
            parts = section.split("-", 1)
            try:
                start_l = int(parts[0].strip())
                end_l = int(parts[1].strip())
            except ValueError as exc:
                raise ValueError(
                    f"Invalid section line range '{section}'. "
                    "Expected format 'start-end' (e.g. '1-50')."
                ) from exc
            if start_l < 1:
                start_l = 1
            if start_l > end_l:
                raise ValueError(
                    f"Invalid section line range '{section}': start line {start_l} "
                    f"is greater than end line {end_l}."
                )
            lines = all_lines[start_l - 1 : min(total_lines, end_l)]
        else:
            raise ValueError(
                f"Invalid section specification for text document: '{section}'. "
                "Expected integer line count (e.g. 50) or line range (e.g. '1-50')."
            )

    sliced_text = "\n".join(lines)
    info: dict[str, Any] = {
        "total_lines": total_lines,
        "lines_returned": len(lines),
    }
    if is_csv:
        info["total_rows"] = total_lines
        info["rows_returned"] = len(lines)

    return file_type, sliced_text, info


def _read_docx_content(file_path: Path) -> str:
    """Extract readable text from a .docx file without external GUI apps."""
    # 1. Try python-docx if installed
    try:
        import docx

        doc = docx.Document(str(file_path))
        lines: list[str] = []
        for p in doc.paragraphs:
            if p.text and p.text.strip():
                lines.append(p.text.strip())
        for table in doc.tables:
            for row in table.rows:
                cells = [c.text.strip() for c in row.cells]
                if any(cells):
                    lines.append("\t".join(cells))
        return "\n\n".join(lines)
    except ImportError:
        pass

    # 2. Stdlib fallback using zipfile + ElementTree
    try:
        with zipfile.ZipFile(file_path, "r") as z:
            if "word/document.xml" not in z.namelist():
                raise ValueError(
                    f"Invalid DOCX file: 'word/document.xml' not found in {file_path.name}"
                )
            xml_data = z.read("word/document.xml")
        root = ET.fromstring(xml_data)
    except (zipfile.BadZipFile, ET.ParseError) as exc:
        raise ValueError(f"Corrupt or invalid DOCX archive: {exc}") from exc

    # Locate body element
    body = None
    for elem in root.iter():
        tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
        if tag == "body":
            body = elem
            break
    if body is None:
        body = root

    def _extract_para_text(p_elem: ET.Element) -> str:
        parts: list[str] = []
        for child in p_elem.iter():
            c_tag = child.tag.split("}")[-1] if "}" in child.tag else child.tag
            if c_tag == "t":
                parts.append(child.text or "")
            elif c_tag == "tab":
                parts.append("\t")
            elif c_tag == "br":
                parts.append("\n")
        return "".join(parts).strip()

    lines: list[str] = []

    def _extract_blocks(container: ET.Element) -> None:
        for elem in container:
            tag = elem.tag.split("}")[-1] if "}" in elem.tag else elem.tag
            if tag == "p":
                p_text = _extract_para_text(elem)
                if p_text:
                    lines.append(p_text)
            elif tag == "tbl":
                for tr in elem.iter():
                    tr_tag = tr.tag.split("}")[-1] if "}" in tr.tag else tr.tag
                    if tr_tag == "tr":
                        cells: list[str] = []
                        for tc in tr:
                            tc_tag = tc.tag.split("}")[-1] if "}" in tc.tag else tc.tag
                            if tc_tag == "tc":
                                tc_text = _extract_para_text(tc)
                                cells.append(tc_text)
                        if any(cells):
                            lines.append("\t".join(cells))
            elif tag in ("sdt", "sdtContent"):
                for sub in elem.iter():
                    sub_tag = sub.tag.split("}")[-1] if "}" in sub.tag else sub.tag
                    if sub_tag == "sdtContent":
                        _extract_blocks(sub)
                        break
                else:
                    _extract_blocks(elem)

    _extract_blocks(body)
    return "\n\n".join(lines)


def _read_xlsx_content(file_path: Path, section: str | int | None = None) -> str:
    """Extract sheet names and cell values from an .xlsx file."""
    # 1. Try openpyxl if installed
    try:
        import openpyxl

        wb = openpyxl.load_workbook(str(file_path), data_only=True, read_only=True)
        sheet_names = wb.sheetnames
        target_sheets: list[str] = []
        if section is not None:
            if isinstance(section, int) or (isinstance(section, str) and section.isdigit()):
                idx = int(section) - 1
                if 0 <= idx < len(sheet_names):
                    target_sheets = [sheet_names[idx]]
                else:
                    wb.close()
                    raise ValueError(
                        f"Sheet index {section} out of range (1..{len(sheet_names)}): {sheet_names}"
                    )
            elif isinstance(section, str):
                matched = [s for s in sheet_names if s.lower() == section.lower()]
                if matched:
                    target_sheets = [matched[0]]
                else:
                    wb.close()
                    raise ValueError(
                        f"Sheet '{section}' not found in workbook. Available: {sheet_names}"
                    )
        else:
            target_sheets = sheet_names

        lines: list[str] = []
        for sname in target_sheets:
            ws = wb[sname]
            lines.append(f"--- Sheet: {sname} ---")
            for row in ws.iter_rows(values_only=True):
                if any(c is not None for c in row):
                    row_vals = [str(c) if c is not None else "" for c in row]
                    lines.append("\t".join(row_vals))
        wb.close()
        return "\n".join(lines)
    except ImportError:
        pass

    # 2. Stdlib fallback using zipfile + ElementTree
    try:
        with zipfile.ZipFile(file_path, "r") as z:
            namelist = z.namelist()

            # Shared strings
            shared_strings: list[str] = []
            if "xl/sharedStrings.xml" in namelist:
                sst_root = ET.fromstring(z.read("xl/sharedStrings.xml"))
                for si in sst_root.iter():
                    si_tag = si.tag.split("}")[-1] if "}" in si.tag else si.tag
                    if si_tag == "si":
                        si_text = "".join(
                            t.text or ""
                            for t in si.iter()
                            if (t.tag.split("}")[-1] if "}" in t.tag else t.tag) == "t"
                        )
                        shared_strings.append(si_text)

            # Relationship map from xl/_rels/workbook.xml.rels
            rels_map: dict[str, str] = {}
            rels_path = "xl/_rels/workbook.xml.rels"
            if rels_path in namelist:
                rels_root = ET.fromstring(z.read(rels_path))
                for rel in rels_root.iter():
                    r_id = rel.attrib.get("Id", "")
                    r_target = rel.attrib.get("Target", "")
                    if r_id and r_target:
                        if not r_target.startswith("xl/"):
                            norm_target = f"xl/{r_target.lstrip('/')}"
                        else:
                            norm_target = r_target
                        rels_map[r_id] = norm_target

            # Discover sheet names and exact target XML files
            sheet_names_list: list[str] = []
            sheet_targets: list[str] = []

            if "xl/workbook.xml" in namelist:
                wb_root = ET.fromstring(z.read("xl/workbook.xml"))
                for s in wb_root.iter():
                    s_tag = s.tag.split("}")[-1] if "}" in s.tag else s.tag
                    if s_tag == "sheet":
                        s_name = s.attrib.get("name", "")
                        r_id = ""
                        for attr_k, attr_v in s.attrib.items():
                            if attr_k.endswith("id") or attr_k == "id":
                                r_id = attr_v
                                break
                        target_file = rels_map.get(r_id, "")
                        if not target_file:
                            sheet_id = s.attrib.get("sheetId", str(len(sheet_names_list) + 1))
                            candidate_target = f"xl/worksheets/sheet{sheet_id}.xml"
                            if candidate_target in namelist:
                                target_file = candidate_target
                            else:
                                candidate_target = (
                                    f"xl/worksheets/sheet{len(sheet_names_list) + 1}.xml"
                                )
                                if candidate_target in namelist:
                                    target_file = candidate_target

                        sheet_names_list.append(s_name)
                        sheet_targets.append(target_file)

            if not sheet_names_list:
                for name in sorted(namelist):
                    if name.startswith("xl/worksheets/sheet") and name.endswith(".xml"):
                        sheet_names_list.append(Path(name).stem)
                        sheet_targets.append(name)

            # Select target sheets based on section
            selected_indices: list[int] = []
            if section is not None:
                if isinstance(section, int) or (isinstance(section, str) and section.isdigit()):
                    idx = int(section) - 1
                    if 0 <= idx < len(sheet_names_list):
                        selected_indices = [idx]
                    else:
                        raise ValueError(
                            f"Sheet index {section} out of range "
                            f"(1..{len(sheet_names_list)}): {sheet_names_list}"
                        )
                elif isinstance(section, str):
                    matched_idx = [
                        i
                        for i, name in enumerate(sheet_names_list)
                        if name.lower() == section.lower()
                    ]
                    if matched_idx:
                        selected_indices = matched_idx
                    else:
                        raise ValueError(
                            f"Sheet '{section}' not found. Available sheets: {sheet_names_list}"
                        )
            else:
                selected_indices = list(range(len(sheet_names_list)))

            def _col_str_to_idx(col_str: str) -> int:
                idx = 0
                for ch in col_str.upper():
                    if "A" <= ch <= "Z":
                        idx = idx * 26 + (ord(ch) - ord("A") + 1)
                return max(0, idx - 1)

            result_lines: list[str] = []
            for idx in selected_indices:
                s_name = sheet_names_list[idx]
                target_xml = sheet_targets[idx] if idx < len(sheet_targets) else None
                if not target_xml or target_xml not in namelist:
                    continue

                result_lines.append(f"--- Sheet: {s_name} ---")
                sheet_root = ET.fromstring(z.read(target_xml))

                for row_elem in sheet_root.iter():
                    r_tag = row_elem.tag.split("}")[-1] if "}" in row_elem.tag else row_elem.tag
                    if r_tag == "row":
                        cell_dict: dict[int, str] = {}
                        max_col = -1
                        for c_elem in row_elem:
                            c_tag = (
                                c_elem.tag.split("}")[-1] if "}" in c_elem.tag else c_elem.tag
                            )
                            if c_tag == "c":
                                r_attr = c_elem.attrib.get("r", "")
                                col_letters = "".join(filter(str.isalpha, r_attr))
                                col_idx = (
                                    _col_str_to_idx(col_letters)
                                    if col_letters
                                    else len(cell_dict)
                                )
                                if col_idx > max_col:
                                    max_col = col_idx

                                c_type = c_elem.attrib.get("t", "")
                                v_child = None
                                is_child = None
                                for child in c_elem:
                                    tag = (
                                        child.tag.split("}")[-1]
                                        if "}" in child.tag
                                        else child.tag
                                    )
                                    if tag == "v":
                                        v_child = child
                                    elif tag == "is":
                                        is_child = child

                                val_str = ""
                                if c_type == "s" and v_child is not None and v_child.text:
                                    try:
                                        s_idx = int(v_child.text)
                                        if 0 <= s_idx < len(shared_strings):
                                            val_str = shared_strings[s_idx]
                                    except ValueError:
                                        pass
                                elif c_type == "inlineStr":
                                    container = is_child if is_child is not None else c_elem
                                    val_str = "".join(
                                        t.text or ""
                                        for t in container.iter()
                                        if (t.tag.split("}")[-1] if "}" in t.tag else t.tag) == "t"
                                    )
                                elif c_type == "b" and v_child is not None:
                                    val_str = "TRUE" if v_child.text == "1" else "FALSE"
                                elif v_child is not None and v_child.text:
                                    val_str = v_child.text

                                cell_dict[col_idx] = val_str

                        if max_col >= 0:
                            row_vals = [cell_dict.get(c, "") for c in range(max_col + 1)]
                            if any(row_vals):
                                result_lines.append("\t".join(row_vals))

        return "\n".join(result_lines)
    except (zipfile.BadZipFile, ET.ParseError) as exc:
        raise ValueError(f"Corrupt or invalid XLSX archive: {exc}") from exc


def _extract_pdf_pure_python(
    file_path: Path, section: str | int | None = None
) -> tuple[str, str | None]:
    """Extract readable text from standard unencrypted PDF streams using pure Python stdlib."""
    try:
        raw_data = file_path.read_bytes()
    except OSError as exc:
        return "", f"Could not read PDF file: {exc}"

    if not raw_data.startswith(b"%PDF-"):
        return "", "Invalid PDF format: missing %PDF header"

    stream_re = re.compile(rb"stream\r?\n(.*?)\r?\nendstream", re.DOTALL)
    decompressed_streams: list[bytes] = []

    for match in stream_re.finditer(raw_data):
        stream_bytes = match.group(1)
        header_start = max(0, match.start() - 200)
        preceding = raw_data[header_start : match.start()]
        if b"/FlateDecode" in preceding:
            try:
                dec = zlib.decompress(stream_bytes)
                decompressed_streams.append(dec)
            except Exception:
                try:
                    dec = zlib.decompress(stream_bytes, -zlib.MAX_WBITS)
                    decompressed_streams.append(dec)
                except Exception:
                    pass
        else:
            decompressed_streams.append(stream_bytes)

    if not decompressed_streams:
        return "", None

    extracted_text_blocks: list[str] = []
    bt_re = re.compile(rb"BT\s+(.*?)\s+ET", re.DOTALL)
    inner_str_re = re.compile(rb"\((?:\\.|[^\\\)])*\)")

    def _decode_pdf_str(raw_str_bytes: bytes) -> str:
        s = raw_str_bytes[1:-1]
        parts: list[str] = []
        i = 0
        while i < len(s):
            if s[i : i + 1] == b"\\":
                if i + 1 >= len(s):
                    break
                nxt = s[i + 1 : i + 2]
                if nxt == b"n":
                    parts.append("\n")
                    i += 2
                elif nxt == b"r":
                    parts.append("\r")
                    i += 2
                elif nxt == b"t":
                    parts.append("\t")
                    i += 2
                elif nxt in (b"(", b")", b"\\"):
                    parts.append(chr(nxt[0]))
                    i += 2
                elif nxt.isdigit():
                    oct_match = re.match(rb"[0-7]{1,3}", s[i + 1 : i + 4])
                    if oct_match:
                        parts.append(chr(int(oct_match.group(), 8)))
                        i += 1 + len(oct_match.group())
                    else:
                        parts.append(chr(nxt[0]))
                        i += 2
                else:
                    parts.append(chr(nxt[0]))
                    i += 2
            else:
                parts.append(chr(s[i]))
                i += 1
        return "".join(parts)

    for stream in decompressed_streams:
        stream_lines: list[str] = []
        for bt in bt_re.finditer(stream):
            block = bt.group(1)
            for match in re.finditer(
                rb"(\((?:\\.|[^\\\)])*\)\s*(?:Tj|'|\")|\[(.*?)\]\s*TJ)", block, re.DOTALL
            ):
                full_m = match.group(0)
                if b"TJ" in full_m:
                    arr_content = match.group(2) or b""
                    line_parts = [
                        _decode_pdf_str(sm.group(0))
                        for sm in inner_str_re.finditer(arr_content)
                    ]
                    if line_parts:
                        stream_lines.append("".join(line_parts))
                else:
                    sm = inner_str_re.search(full_m)
                    if sm:
                        stream_lines.append(_decode_pdf_str(sm.group(0)))
        if stream_lines:
            extracted_text_blocks.append("\n".join(stream_lines))

    if not extracted_text_blocks:
        return "", None

    full_pdf_text = "\n\n".join(extracted_text_blocks).strip()
    return full_pdf_text, None


def _read_pdf_content(
    file_path: Path, section: str | int | None = None
) -> tuple[str, str | None]:
    """Extract text from a PDF via pypdf, fitz, pdftotext, stdlib stream, or return error."""
    # 1. Try pypdf
    try:
        import pypdf

        reader = pypdf.PdfReader(str(file_path))
        pages = reader.pages
        total_pages = len(pages)
        if section is not None:
            p_idx = _parse_page_number(section, total_pages)
            return (pages[p_idx].extract_text() or "").strip(), None
        return "\n\n".join((p.extract_text() or "").strip() for p in pages), None
    except ImportError:
        pass

    # 2. Try fitz (PyMuPDF)
    try:
        import fitz

        doc = fitz.open(str(file_path))
        total_pages = len(doc)
        if section is not None:
            p_idx = _parse_page_number(section, total_pages)
            return doc[p_idx].get_text().strip(), None
        return "\n\n".join(p.get_text().strip() for p in doc), None
    except ImportError:
        pass

    # 3. Try pdftotext CLI if installed
    pdftotext = shutil.which("pdftotext")
    if pdftotext:
        cmd = [pdftotext, str(file_path), "-"]
        if section is not None:
            try:
                p_num = int(re.search(r"\d+", str(section)).group())  # type: ignore[union-attr]
                cmd = [pdftotext, "-f", str(p_num), "-l", str(p_num), str(file_path), "-"]
            except (ValueError, AttributeError):
                pass
        proc = run_hidden(cmd, capture_output=True, text=True, timeout=15.0, check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip(), None

    # 4. Try pure Python stdlib stream text extraction
    stream_text, _ = _extract_pdf_pure_python(file_path, section=section)
    if stream_text:
        return stream_text, None

    # 5. No PDF backend installed or extraction not possible
    err_msg = (
        "No PDF backend is installed. Install 'pypdf' or 'pymupdf' (or ensure pdftotext CLI "
        "is available) to enable direct PDF reading."
    )
    return "", err_msg


def _parse_page_number(section: str | int, total_pages: int) -> int:
    """Parse a 1-based page number or section string into a 0-based index."""
    if isinstance(section, int):
        idx = section - 1
    else:
        m = re.search(r"\d+", str(section))
        if not m:
            raise ValueError(f"Invalid page specification '{section}'")
        idx = int(m.group()) - 1

    if idx < 0 or idx >= total_pages:
        raise ValueError(f"Page {section} out of bounds (1..{total_pages})")
    return idx


def read_document(
    path: str,
    max_chars: int = 8000,
    section: str | int | None = None,
) -> dict[str, Any]:
    """Extract clean readable text directly without launching external GUI apps.

    Args:
        path: Path to the document or text file.
        max_chars: Maximum characters to return in content (default 8000).
        section: Optional section (sheet name/index for xlsx, page for pdf, lines for text).

    Returns:
        Structured metadata: path, file_type, total_chars, truncated, content, etc.
    """
    raw_str = str(path).strip()
    expanded = os.path.expandvars(raw_str)
    file_path = Path(expanded).expanduser().resolve()

    if not file_path.exists():
        raise FileNotFoundError(f"File not found: {file_path}")
    if file_path.is_dir():
        raise IsADirectoryError(f"Path is a directory, not a document: {file_path}")

    suffix = file_path.suffix.lower()
    full_text = ""
    error_note: str | None = None
    line_info: dict[str, Any] = {}

    if suffix in (".docx", ".doc"):
        file_type = "docx"
        full_text = _read_docx_content(file_path)
    elif suffix in (".xlsx", ".xls"):
        file_type = "xlsx"
        full_text = _read_xlsx_content(file_path, section=section)
    elif suffix == ".pdf":
        file_type = "pdf"
        full_text, error_note = _read_pdf_content(file_path, section=section)
    else:
        file_type, full_text, line_info = _read_text_file(file_path, section=section)

    total_chars = len(full_text)
    limit = max(1, max_chars) if max_chars is not None else 8000
    truncated = total_chars > limit
    content = full_text[:limit] if truncated else full_text

    result: dict[str, Any] = {
        "path": str(file_path),
        "file_type": file_type,
        "total_chars": total_chars,
        "truncated": truncated,
        "content": content,
    }
    if section is not None:
        result["section"] = section
    if line_info:
        result.update(line_info)
        if truncated:
            content_lines = len(content.splitlines())
            result["lines_returned"] = content_lines
            if "total_rows" in line_info:
                result["rows_returned"] = content_lines
            result["truncation_info"] = (
                f"Truncated to {len(content)} of {total_chars} characters "
                f"({content_lines} of {line_info['total_lines']} lines). "
                "Use max_chars or section to read more."
            )
    if error_note:
        result["error"] = error_note

    return result


def _recycle_path(target: Path) -> str:
    """Send a file or directory to the Windows Recycle Bin safely."""
    # 1. Try send2trash if installed
    try:
        import send2trash

        send2trash.send2trash(str(target))
        return "send2trash"
    except ImportError:
        pass

    # 2. Windows fallback using PowerShell Microsoft.VisualBasic.FileIO.FileSystem
    if os.name == "nt":
        method = "DeleteDirectory" if target.is_dir() else "DeleteFile"
        escaped_path = str(target.resolve()).replace("'", "''")
        ps_cmd = (
            f"Add-Type -AssemblyName Microsoft.VisualBasic; "
            f"[Microsoft.VisualBasic.FileIO.FileSystem]::{method}('{escaped_path}', "
            f"[Microsoft.VisualBasic.FileIO.UIOption]::OnlyErrorDialogs, "
            f"[Microsoft.VisualBasic.FileIO.RecycleOption]::SendToRecycleBin)"
        )
        ps_exe = (
            shutil.which("powershell.exe")
            or shutil.which("powershell")
            or shutil.which("pwsh")
            or "powershell.exe"
        )
        proc = run_powershell_hidden(
            ps_cmd,
            encoded=True,
            timeout=15.0,
            ps_exe=ps_exe,
            check=False,
        )
        if proc.returncode != 0:
            err = proc.stderr.strip() or proc.stdout.strip()
            raise OSError(f"Failed to recycle '{target}' via PowerShell: {err}")
        return "powershell_recycle_bin"

    raise RuntimeError(
        "Recycling files requires 'send2trash' on non-Windows platforms or PowerShell on Windows."
    )


def _classify_for_organization(ext: str) -> str | None:
    """Classify a file extension into one of the designated organization folders."""
    ext_lower = ext.lower()
    for category, extensions in FOLDER_ORGANIZATION_CATEGORIES.items():
        if ext_lower in extensions:
            return category
    return None


def manage_file(
    action: str,
    source: str,
    destination: str | None = None,
    confirm: bool = False,
) -> dict[str, Any]:
    """Perform safe, non-destructive file operations.

    Actions:
        - 'move': Move file or folder to destination.
        - 'rename': Rename file or folder to destination name.
        - 'copy': Copy file or folder to destination.
        - 'archive_zip': Compress file or folder into a .zip archive.
        - 'organize_folder': Group files into category folders (dry-run unless confirm=True).
        - 'recycle': Send file or folder to Windows Recycle Bin.
    """
    raw_action = str(action).strip().lower()
    source_raw = os.path.expandvars(str(source).strip())
    source_path = Path(source_raw).expanduser().resolve()

    if not source_path.exists() and not source_path.is_symlink():
        raise FileNotFoundError(f"Source file or directory does not exist: {source_path}")

    # --- 1. MOVE ---
    if raw_action == "move":
        if not destination:
            raise ValueError("destination path is required for 'move' action")
        dest_raw = str(destination).strip()
        dest_path = Path(os.path.expandvars(dest_raw)).expanduser().resolve()
        is_dest_dir_hint = dest_raw.endswith(("/", "\\"))
        if dest_path.is_dir() or is_dest_dir_hint:
            dest_path.mkdir(parents=True, exist_ok=True)
            target_dest = dest_path / source_path.name
        else:
            target_dest = dest_path

        if source_path == target_dest:
            return {
                "action": "move",
                "source": str(source_path),
                "destination": str(target_dest),
                "status": "ok",
                "message": "Source and destination are identical; no-op.",
            }

        target_dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.move(str(source_path), str(target_dest))
        return {
            "action": "move",
            "source": str(source_path),
            "destination": str(target_dest),
            "status": "ok",
        }

    # --- 2. RENAME ---
    if raw_action == "rename":
        if not destination:
            raise ValueError("destination name is required for 'rename' action")
        dest_str = str(destination).strip()
        if "/" in dest_str or "\\" in dest_str:
            target_dest = Path(os.path.expandvars(dest_str)).expanduser().resolve()
        else:
            target_dest = source_path.parent / dest_str

        if target_dest == source_path:
            return {
                "action": "rename",
                "source": str(source_path),
                "destination": str(target_dest),
                "status": "ok",
                "message": "Source and destination are identical; no-op.",
            }

        if target_dest.exists():
            raise FileExistsError(f"Destination already exists: {target_dest}")
        source_path.rename(target_dest)
        return {
            "action": "rename",
            "source": str(source_path),
            "destination": str(target_dest),
            "status": "ok",
        }

    # --- 3. COPY ---
    if raw_action == "copy":
        if not destination:
            raise ValueError("destination path is required for 'copy' action")
        dest_raw = str(destination).strip()
        dest_path = Path(os.path.expandvars(dest_raw)).expanduser().resolve()
        is_dest_dir_hint = dest_raw.endswith(("/", "\\"))
        if dest_path.is_dir() or is_dest_dir_hint:
            dest_path.mkdir(parents=True, exist_ok=True)
            target_dest = dest_path / source_path.name
        else:
            target_dest = dest_path

        if source_path == target_dest:
            return {
                "action": "copy",
                "source": str(source_path),
                "destination": str(target_dest),
                "status": "ok",
                "message": "Source and destination are identical; no-op.",
            }

        target_dest.parent.mkdir(parents=True, exist_ok=True)
        if source_path.is_dir():
            shutil.copytree(str(source_path), str(target_dest), dirs_exist_ok=True)
        else:
            shutil.copy2(str(source_path), str(target_dest))
        return {
            "action": "copy",
            "source": str(source_path),
            "destination": str(target_dest),
            "status": "ok",
        }

    # --- 4. ARCHIVE_ZIP ---
    if raw_action == "archive_zip":
        if destination:
            dest_path = Path(os.path.expandvars(str(destination).strip())).expanduser().resolve()
            if dest_path.is_dir() or str(destination).strip().endswith(("/", "\\")):
                dest_path = dest_path / f"{source_path.name}.zip"
            elif not dest_path.name.lower().endswith(".zip"):
                dest_path = dest_path.with_suffix(".zip")
        else:
            if source_path.is_dir():
                dest_path = source_path.parent / f"{source_path.name}.zip"
            else:
                dest_path = source_path.with_suffix(".zip")

        dest_path.parent.mkdir(parents=True, exist_ok=True)

        with zipfile.ZipFile(dest_path, "w", zipfile.ZIP_DEFLATED) as zf:
            if source_path.is_file():
                zf.write(source_path, arcname=source_path.name)
            else:
                dest_resolved = dest_path.resolve()
                for root, _, files in os.walk(source_path):
                    for file in files:
                        f_path = Path(root) / file
                        if f_path.resolve() == dest_resolved:
                            continue
                        rel_path = f_path.relative_to(source_path)
                        zf.write(f_path, arcname=str(rel_path))

        return {
            "action": "archive_zip",
            "source": str(source_path),
            "destination": str(dest_path),
            "size": dest_path.stat().st_size,
            "status": "ok",
        }

    # --- 5. ORGANIZE_FOLDER ---
    if raw_action == "organize_folder":
        if not source_path.is_dir():
            raise NotADirectoryError(
                f"organize_folder requires a directory source, got: {source_path}"
            )

        planned_moves: list[dict[str, str]] = []
        skipped_files: list[str] = []

        for item in sorted(source_path.iterdir()):
            if not item.is_file():
                continue
            item_lower = item.name.lower()
            if item_lower in IGNORE_ORGANIZATION_FILES:
                skipped_files.append(item.name)
                continue

            cat = _classify_for_organization(item.suffix)
            if cat is None:
                skipped_files.append(item.name)
                continue

            target_folder = source_path / cat
            dest_file = target_folder / item.name
            if dest_file.exists() and dest_file != item:
                stem = dest_file.stem
                suf = dest_file.suffix
                counter = 1
                while dest_file.exists():
                    dest_file = dest_file.parent / f"{stem}_{counter}{suf}"
                    counter += 1

            planned_moves.append(
                {
                    "file": item.name,
                    "category": cat,
                    "source": str(item),
                    "destination": str(dest_file),
                }
            )

        if not confirm:
            return {
                "action": "organize_folder",
                "source": str(source_path),
                "confirm": False,
                "dry_run": True,
                "planned_moves": planned_moves,
                "total_files": len(planned_moves),
                "skipped_files": skipped_files,
                "message": (
                    f"Preview: {len(planned_moves)} files will be organized into categories. "
                    "Re-run with confirm=True to execute."
                ),
            }

        executed_moves: list[dict[str, str]] = []
        for move in planned_moves:
            src_f = Path(move["source"])
            dst_f = Path(move["destination"])
            dst_f.parent.mkdir(parents=True, exist_ok=True)
            if dst_f.exists() and dst_f != src_f:
                stem = dst_f.stem
                suf = dst_f.suffix
                counter = 1
                while dst_f.exists():
                    dst_f = dst_f.parent / f"{stem}_{counter}{suf}"
                    counter += 1
            shutil.move(str(src_f), str(dst_f))
            executed_moves.append(
                {
                    "file": move["file"],
                    "category": move["category"],
                    "source": str(src_f),
                    "destination": str(dst_f),
                }
            )

        return {
            "action": "organize_folder",
            "source": str(source_path),
            "confirm": True,
            "dry_run": False,
            "executed_moves": executed_moves,
            "total_files": len(executed_moves),
            "skipped_files": skipped_files,
            "message": f"Successfully organized {len(executed_moves)} files.",
        }

    # --- 6. RECYCLE ---
    if raw_action == "recycle":
        backend = _recycle_path(source_path)
        return {
            "action": "recycle",
            "source": str(source_path),
            "backend": backend,
            "status": "ok",
        }

    raise ValueError(
        f"Unsupported action: '{action}'. Allowed actions: move, rename, copy, archive_zip, "
        "organize_folder, recycle"
    )


# --------------------------------------------------------------------------- MCP Tool Registration


SEARCH_FILES_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "query": {
            "type": "string",
            "description": (
                "Filename substring or glob pattern (e.g. '*.pdf', 'invoice'). "
                "Empty matches all files."
            ),
            "default": "",
        },
        "directory": {
            "type": "string",
            "description": (
                "Directory path to search. Set to 'user_dirs' (default) to search Desktop, "
                "Downloads, and Documents."
            ),
            "default": "user_dirs",
        },
        "extension": {
            "type": "string",
            "description": "Optional file extension filter (e.g. '.docx', 'pdf', 'xlsx').",
        },
        "modified_within_days": {
            "type": "integer",
            "description": "Only return files modified within this many days from today.",
        },
        "limit": {
            "type": "integer",
            "description": "Maximum number of file results to return (default 20).",
            "default": 20,
        },
    },
    "additionalProperties": False,
}

READ_DOCUMENT_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "path": {
            "type": "string",
            "description": "Path to the document or text file to read.",
        },
        "max_chars": {
            "type": "integer",
            "description": "Maximum characters to extract (default 8000).",
            "default": 8000,
        },
        "section": {
            "description": (
                "Optional section to extract: sheet name/index for xlsx, "
                "page number for pdf, or line count for text."
            ),
            "anyOf": [
                {"type": "string"},
                {"type": "integer"},
            ],
        },
    },
    "required": ["path"],
    "additionalProperties": False,
}

MANAGE_FILE_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "action": {
            "type": "string",
            "enum": ["move", "rename", "copy", "archive_zip", "organize_folder", "recycle"],
            "description": (
                "File operation: 'move', 'rename', 'copy', 'archive_zip', 'organize_folder' "
                "(preview plan unless confirm=True), or 'recycle' (safe Windows Recycle Bin)."
            ),
        },
        "source": {
            "type": "string",
            "description": "Path to the source file or directory.",
        },
        "destination": {
            "type": "string",
            "description": (
                "Target destination path or name (required for move, rename, copy; "
                "optional for archive_zip)."
            ),
        },
        "confirm": {
            "type": "boolean",
            "description": (
                "Confirmation flag required to execute bulk/destructive operations "
                "like organize_folder."
            ),
            "default": False,
        },
    },
    "required": ["action", "source"],
    "additionalProperties": False,
}


def register_file_tools(registry: ToolRegistry) -> None:
    """Register file intelligence tools on the given ToolRegistry."""
    registry.register(
        name="search_files",
        description=(
            "Search local files in user directories (Desktop, Downloads, Documents) or an "
            "explicit directory path with filename substring/glob matching, extension filtering, "
            "and recency sorting."
        ),
        input_schema=SEARCH_FILES_SCHEMA,
        risk_level=RiskLevel.READ_ONLY,
        handler=search_files,
    )
    registry.register(
        name="read_document",
        description=(
            "Extract clean readable text directly from documents (.docx, .xlsx, .pdf, .txt, .csv, "
            ".md, .json, source code) without launching external GUI apps."
        ),
        input_schema=READ_DOCUMENT_SCHEMA,
        risk_level=RiskLevel.READ_ONLY,
        handler=read_document,
    )
    registry.register(
        name="manage_file",
        description=(
            "Perform safe file operations: move, rename, copy, archive_zip, organize_folder "
            "(dry-run preview unless confirm=True), or recycle to Windows Recycle Bin."
        ),
        input_schema=MANAGE_FILE_SCHEMA,
        risk_level=RiskLevel.REVERSIBLE,
        handler=manage_file,
    )

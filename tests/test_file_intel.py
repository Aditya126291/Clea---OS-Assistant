"""Unit tests for local file search, zero-app document reader, and safe file manager."""

from __future__ import annotations

import os
import sys
import time
import zipfile
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from clea.tools import (
    AuditLogger,
    PermissionDeniedError,
    RiskLevel,
    ToolRegistry,
)
from clea.tools.file_intel import (
    manage_file,
    read_document,
    register_file_tools,
    search_files,
)

# --------------------------------------------------------------------------- Fixtures & Helpers


def _create_mock_docx(path: Path, paragraphs: list[str]) -> Path:
    """Create a minimal real .docx file with valid OpenXML document.xml."""
    doc_xml_parts = [
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
        '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">',
        "<w:body>",
    ]
    for p in paragraphs:
        doc_xml_parts.append(f"<w:p><w:r><w:t>{p}</w:t></w:r></w:p>")
    doc_xml_parts.append("</w:body></w:document>")
    xml_content = "".join(doc_xml_parts)

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr("word/document.xml", xml_content)
    return path


def _create_mock_xlsx(
    path: Path,
    sheets: dict[str, list[list[str]]],
) -> Path:
    """Create a minimal real .xlsx file with valid OpenXML workbook and sheet XMLs."""
    # Build shared strings table
    shared_strings: list[str] = []
    sst_map: dict[str, int] = {}
    for rows in sheets.values():
        for row in rows:
            for cell in row:
                if cell not in sst_map:
                    sst_map[cell] = len(shared_strings)
                    shared_strings.append(cell)

    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        # 1. xl/workbook.xml
        wb_parts = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">',
            "<sheets>",
        ]
        for idx, sname in enumerate(sheets.keys(), start=1):
            wb_parts.append(f'<sheet name="{sname}" sheetId="{idx}" r:id="rId{idx}"/>')
        wb_parts.append("</sheets></workbook>")
        z.writestr("xl/workbook.xml", "".join(wb_parts))

        # 2. xl/sharedStrings.xml
        sst_parts = [
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
            '<sst xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            f'count="{len(shared_strings)}">',
        ]
        for s in shared_strings:
            sst_parts.append(f"<si><t>{s}</t></si>")
        sst_parts.append("</sst>")
        z.writestr("xl/sharedStrings.xml", "".join(sst_parts))

        # 3. Worksheets
        for s_idx, (_sname, rows) in enumerate(sheets.items(), start=1):
            ws_parts = [
                '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>',
                '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">',
                "<sheetData>",
            ]
            for r_idx, row in enumerate(rows, start=1):
                ws_parts.append(f'<row r="{r_idx}">')
                for _c_idx, val in enumerate(row):
                    s_id = sst_map[val]
                    ws_parts.append(f'<c t="s"><v>{s_id}</v></c>')
                ws_parts.append("</row>")
            ws_parts.append("</sheetData></worksheet>")
            z.writestr(f"xl/worksheets/sheet{s_idx}.xml", "".join(ws_parts))

    return path


# --------------------------------------------------------------------------- search_files Tests


def test_search_files_filters_by_name_and_glob(tmp_path: Path) -> None:
    (tmp_path / "budget_2026.xlsx").write_text("dummy", encoding="utf-8")
    (tmp_path / "budget_2025.xlsx").write_text("dummy", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("dummy", encoding="utf-8")
    (tmp_path / "readme.md").write_text("dummy", encoding="utf-8")

    # Substring search
    results = search_files(query="budget", directory=str(tmp_path))
    assert len(results) == 2
    names = {r["name"] for r in results}
    assert names == {"budget_2026.xlsx", "budget_2025.xlsx"}

    # Glob pattern search
    results_glob = search_files(query="*_2026.*", directory=str(tmp_path))
    assert len(results_glob) == 1
    assert results_glob[0]["name"] == "budget_2026.xlsx"


def test_search_files_filters_by_extension(tmp_path: Path) -> None:
    (tmp_path / "a.pdf").write_text("dummy", encoding="utf-8")
    (tmp_path / "b.txt").write_text("dummy", encoding="utf-8")
    (tmp_path / "c.PDF").write_text("dummy", encoding="utf-8")

    # Extension with dot
    res_dot = search_files(directory=str(tmp_path), extension=".pdf")
    assert len(res_dot) == 2
    assert {r["name"].lower() for r in res_dot} == {"a.pdf", "c.pdf"}

    # Extension without dot
    res_nodot = search_files(directory=str(tmp_path), extension="txt")
    assert len(res_nodot) == 1
    assert res_nodot[0]["name"] == "b.txt"


def test_search_files_filters_by_recency(tmp_path: Path) -> None:
    now = time.time()
    f_recent = tmp_path / "recent.txt"
    f_recent.write_text("new", encoding="utf-8")
    os.utime(f_recent, (now, now))

    f_old = tmp_path / "old.txt"
    f_old.write_text("old", encoding="utf-8")
    old_time = now - (10 * 86400.0)  # 10 days ago
    os.utime(f_old, (old_time, old_time))

    res_all = search_files(directory=str(tmp_path))
    assert len(res_all) == 2

    res_recent = search_files(directory=str(tmp_path), modified_within_days=5)
    assert len(res_recent) == 1
    assert res_recent[0]["name"] == "recent.txt"


def test_search_files_ignores_noisy_directories(tmp_path: Path) -> None:
    git_dir = tmp_path / ".git"
    git_dir.mkdir()
    (git_dir / "config.txt").write_text("git config", encoding="utf-8")

    node_dir = tmp_path / "node_modules"
    node_dir.mkdir()
    (node_dir / "package.json").write_text("{}", encoding="utf-8")

    venv_dir = tmp_path / ".venv"
    venv_dir.mkdir()
    (venv_dir / "pyvenv.cfg").write_text("dummy", encoding="utf-8")

    cache_dir = tmp_path / "__pycache__"
    cache_dir.mkdir()
    (cache_dir / "cache.pyc").write_text("dummy", encoding="utf-8")

    recycle_dir = tmp_path / "$RECYCLE.BIN"
    recycle_dir.mkdir()
    (recycle_dir / "trashed.txt").write_text("dummy", encoding="utf-8")

    good_file = tmp_path / "valid.txt"
    good_file.write_text("valid content", encoding="utf-8")

    results = search_files(directory=str(tmp_path))
    assert len(results) == 1
    assert results[0]["name"] == "valid.txt"


def test_search_files_user_dirs_resolution(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> None:
    desktop = tmp_path / "Desktop"
    desktop.mkdir()
    (desktop / "desktop_item.txt").write_text("item", encoding="utf-8")

    downloads = tmp_path / "Downloads"
    downloads.mkdir()
    (downloads / "download_item.txt").write_text("item", encoding="utf-8")

    documents = tmp_path / "Documents"
    documents.mkdir()
    (documents / "doc_item.txt").write_text("item", encoding="utf-8")

    # Mock Path.home() to point to tmp_path
    monkeypatch.setattr(Path, "home", lambda: tmp_path)
    monkeypatch.setenv("OneDrive", "")
    monkeypatch.setenv("OneDriveConsumer", "")
    monkeypatch.setenv("OneDriveCommercial", "")

    results = search_files(directory="user_dirs")
    names = {r["name"] for r in results}
    assert "desktop_item.txt" in names
    assert "download_item.txt" in names
    assert "doc_item.txt" in names


def test_search_files_invalid_directory_raises(tmp_path: Path) -> None:
    missing = tmp_path / "does_not_exist"
    with pytest.raises(FileNotFoundError):
        search_files(directory=str(missing))

    a_file = tmp_path / "regular_file.txt"
    a_file.write_text("hi", encoding="utf-8")
    with pytest.raises(NotADirectoryError):
        search_files(directory=str(a_file))


# --------------------------------------------------------------------------- read_document Tests


def test_read_document_plain_text_and_code(tmp_path: Path) -> None:
    txt_file = tmp_path / "test.txt"
    txt_file.write_text("Hello Clea\nLine 2\nLine 3", encoding="utf-8")

    res = read_document(str(txt_file))
    assert res["file_type"] == "text"
    assert res["total_chars"] == len("Hello Clea\nLine 2\nLine 3")
    assert res["truncated"] is False
    assert "Hello Clea" in res["content"]

    # Test section with line range
    res_sec = read_document(str(txt_file), section="1-2")
    assert res_sec["content"] == "Hello Clea\nLine 2"

    # Test code file
    py_file = tmp_path / "main.py"
    py_file.write_text("def hello(): pass", encoding="utf-8")
    res_code = read_document(str(py_file))
    assert res_code["file_type"] == "code"
    assert "def hello()" in res_code["content"]


def test_read_document_csv(tmp_path: Path) -> None:
    csv_file = tmp_path / "data.csv"
    csv_file.write_text("Name,Age,Role\nAlice,30,Engineer\nBob,35,Designer", encoding="utf-8")

    res = read_document(str(csv_file))
    assert res["file_type"] == "csv"
    assert "Alice,30,Engineer" in res["content"]


def test_read_document_docx_stdlib(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Ensure python-docx is not used so stdlib zipfile + ET fallback executes
    monkeypatch.setitem(sys.modules, "docx", None)

    docx_path = tmp_path / "sample.docx"
    _create_mock_docx(
        docx_path,
        [
            "Title of the Document",
            "This is paragraph one with important details.",
            "Paragraph two concludes the note.",
        ],
    )

    res = read_document(str(docx_path))
    assert res["file_type"] == "docx"
    assert "Title of the Document" in res["content"]
    assert "paragraph one" in res["content"]
    assert "concludes the note" in res["content"]
    assert res["truncated"] is False


def test_read_document_xlsx_stdlib(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    # Ensure openpyxl is not used so stdlib zipfile + ET fallback executes
    monkeypatch.setitem(sys.modules, "openpyxl", None)

    xlsx_path = tmp_path / "financials.xlsx"
    sheets_data = {
        "Q1": [["Metric", "Value"], ["Revenue", "$100k"], ["Cost", "$40k"]],
        "Q2": [["Metric", "Value"], ["Revenue", "$120k"], ["Cost", "$45k"]],
    }
    _create_mock_xlsx(xlsx_path, sheets_data)

    # Read all sheets
    res = read_document(str(xlsx_path))
    assert res["file_type"] == "xlsx"
    assert "--- Sheet: Q1 ---" in res["content"]
    assert "--- Sheet: Q2 ---" in res["content"]
    assert "$100k" in res["content"]
    assert "$120k" in res["content"]

    # Read by sheet name
    res_q1 = read_document(str(xlsx_path), section="Q1")
    assert "--- Sheet: Q1 ---" in res_q1["content"]
    assert "--- Sheet: Q2 ---" not in res_q1["content"]

    # Read by 1-based index
    res_q2 = read_document(str(xlsx_path), section=2)
    assert "--- Sheet: Q2 ---" in res_q2["content"]
    assert "--- Sheet: Q1 ---" not in res_q2["content"]

    # Invalid sheet name raises ValueError
    with pytest.raises(ValueError, match="Sheet 'NonExistent' not found"):
        read_document(str(xlsx_path), section="NonExistent")


def test_read_document_pdf_mocked(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    pdf_file = tmp_path / "sample.pdf"
    pdf_file.write_text("%PDF-1.4 mock content", encoding="utf-8")

    # Mock pypdf
    mock_page_1 = MagicMock()
    mock_page_1.extract_text.return_value = "Page 1: Introduction to Clea AI"
    mock_page_2 = MagicMock()
    mock_page_2.extract_text.return_value = "Page 2: Architecture & Tools"

    mock_pypdf = MagicMock()
    mock_reader = MagicMock()
    mock_reader.pages = [mock_page_1, mock_page_2]
    mock_pypdf.PdfReader.return_value = mock_reader

    monkeypatch.setitem(sys.modules, "pypdf", mock_pypdf)

    res = read_document(str(pdf_file))
    assert res["file_type"] == "pdf"
    assert "Page 1: Introduction" in res["content"]
    assert "Page 2: Architecture" in res["content"]
    assert res["truncated"] is False

    # Mock page selection via section
    res_page_2 = read_document(str(pdf_file), section=2)
    assert res_page_2["content"] == "Page 2: Architecture & Tools"


def test_read_document_pdf_without_backend_returns_structured_error(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_file = tmp_path / "report.pdf"
    pdf_file.write_text("%PDF-1.4 dummy", encoding="utf-8")

    monkeypatch.setitem(sys.modules, "pypdf", None)
    monkeypatch.setitem(sys.modules, "fitz", None)
    monkeypatch.setattr("shutil.which", lambda _name: None)

    res = read_document(str(pdf_file))
    assert res["file_type"] == "pdf"
    assert res["total_chars"] == 0
    assert "No PDF backend is installed" in res["error"]


def test_read_document_truncation(tmp_path: Path) -> None:
    big_file = tmp_path / "big.txt"
    big_file.write_text("A" * 2000, encoding="utf-8")

    res = read_document(str(big_file), max_chars=500)
    assert res["total_chars"] == 2000
    assert res["truncated"] is True
    assert len(res["content"]) == 500


def test_read_document_missing_file_and_directory(tmp_path: Path) -> None:
    missing = tmp_path / "ghost.txt"
    with pytest.raises(FileNotFoundError):
        read_document(str(missing))

    sub_dir = tmp_path / "my_dir"
    sub_dir.mkdir()
    with pytest.raises(IsADirectoryError):
        read_document(str(sub_dir))


# --------------------------------------------------------------------------- manage_file Tests


def test_manage_file_move(tmp_path: Path) -> None:
    src = tmp_path / "source.txt"
    src.write_text("move me", encoding="utf-8")
    dst = tmp_path / "sub" / "moved.txt"

    res = manage_file(action="move", source=str(src), destination=str(dst))
    assert res["status"] == "ok"
    assert not src.exists()
    assert dst.exists()
    assert dst.read_text(encoding="utf-8") == "move me"


def test_manage_file_rename(tmp_path: Path) -> None:
    src = tmp_path / "old_name.txt"
    src.write_text("rename me", encoding="utf-8")

    # Rename with simple filename
    res = manage_file(action="rename", source=str(src), destination="new_name.txt")
    assert res["status"] == "ok"
    assert not src.exists()
    new_path = tmp_path / "new_name.txt"
    assert new_path.exists()
    assert new_path.read_text(encoding="utf-8") == "rename me"

    # Rename collision raises FileExistsError
    collider = tmp_path / "collider.txt"
    collider.write_text("exists", encoding="utf-8")
    with pytest.raises(FileExistsError):
        manage_file(action="rename", source=str(new_path), destination="collider.txt")


def test_manage_file_copy(tmp_path: Path) -> None:
    src = tmp_path / "orig.txt"
    src.write_text("copy me", encoding="utf-8")
    dst = tmp_path / "backup" / "orig_copy.txt"

    res = manage_file(action="copy", source=str(src), destination=str(dst))
    assert res["status"] == "ok"
    assert src.exists()
    assert dst.exists()
    assert dst.read_text(encoding="utf-8") == "copy me"


def test_manage_file_archive_zip(tmp_path: Path) -> None:
    folder = tmp_path / "project"
    folder.mkdir()
    (folder / "file1.txt").write_text("content 1", encoding="utf-8")
    (folder / "sub").mkdir()
    (folder / "sub" / "file2.txt").write_text("content 2", encoding="utf-8")

    res = manage_file(action="archive_zip", source=str(folder))
    assert res["status"] == "ok"
    zip_path = Path(res["destination"])
    assert zip_path.exists()
    assert zip_path.suffix == ".zip"

    # Verify zip content
    with zipfile.ZipFile(zip_path, "r") as z:
        names = z.namelist()
        assert "file1.txt" in names
        assert "sub/file2.txt" in [n.replace("\\", "/") for n in names]


def test_manage_file_organize_folder_dry_run_vs_confirm(tmp_path: Path) -> None:
    downloads = tmp_path / "Downloads"
    downloads.mkdir()

    (downloads / "document.pdf").write_text("pdf", encoding="utf-8")
    (downloads / "photo.png").write_text("png", encoding="utf-8")
    (downloads / "script.py").write_text("py", encoding="utf-8")
    (downloads / "archive.zip").write_text("zip", encoding="utf-8")
    (downloads / "installer.msi").write_text("msi", encoding="utf-8")
    (downloads / "song.mp3").write_text("mp3", encoding="utf-8")
    (downloads / "unknown.xyz").write_text("xyz", encoding="utf-8")

    # 1. Dry run (confirm=False)
    dry_run_res = manage_file(action="organize_folder", source=str(downloads), confirm=False)
    assert dry_run_res["dry_run"] is True
    assert dry_run_res["confirm"] is False
    assert dry_run_res["total_files"] == 6
    assert "unknown.xyz" in dry_run_res["skipped_files"]

    # Ensure no files were actually moved
    assert (downloads / "document.pdf").exists()
    assert not (downloads / "Documents").exists()

    # 2. Confirm execution (confirm=True)
    real_res = manage_file(action="organize_folder", source=str(downloads), confirm=True)
    assert real_res["dry_run"] is False
    assert real_res["confirm"] is True
    assert real_res["total_files"] == 6

    # Verify moved locations
    assert not (downloads / "document.pdf").exists()
    assert (downloads / "Documents" / "document.pdf").exists()
    assert (downloads / "Images" / "photo.png").exists()
    assert (downloads / "Code" / "script.py").exists()
    assert (downloads / "Archives" / "archive.zip").exists()
    assert (downloads / "Installers" / "installer.msi").exists()
    assert (downloads / "Media" / "song.mp3").exists()
    assert (downloads / "unknown.xyz").exists()  # unclassified remains untouched


def test_manage_file_recycle(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    test_file = tmp_path / "discard.txt"
    test_file.write_text("discardable", encoding="utf-8")

    # Mock send2trash
    mock_send2trash = MagicMock()
    monkeypatch.setitem(sys.modules, "send2trash", mock_send2trash)

    res = manage_file(action="recycle", source=str(test_file))
    assert res["status"] == "ok"
    assert res["backend"] == "send2trash"
    mock_send2trash.send2trash.assert_called_once_with(str(test_file))


def test_manage_file_unsupported_action(tmp_path: Path) -> None:
    f = tmp_path / "file.txt"
    f.write_text("hi", encoding="utf-8")
    with pytest.raises(ValueError, match="Unsupported action"):
        manage_file(action="destroy_everything", source=str(f))


# --------------------------------------------------------------------------- Registry Tests


async def test_tool_registry_registration_and_execution(tmp_path: Path) -> None:
    audit_path = tmp_path / "logs" / "audit.jsonl"
    registry = ToolRegistry(audit_logger=AuditLogger(audit_path))
    register_file_tools(registry)

    # Check registered tools
    definitions = {d.name: d for d in registry.list_definitions()}
    assert "search_files" in definitions
    assert "read_document" in definitions
    assert "manage_file" in definitions

    assert definitions["search_files"].risk_level == RiskLevel.READ_ONLY
    assert definitions["read_document"].risk_level == RiskLevel.READ_ONLY
    assert definitions["manage_file"].risk_level == RiskLevel.REVERSIBLE

    # Test executing read_document through registry
    sample_file = tmp_path / "greeting.txt"
    sample_file.write_text("Hello from Clea Registry", encoding="utf-8")

    doc_res = await registry.execute("read_document", {"path": str(sample_file)})
    assert doc_res["content"] == "Hello from Clea Registry"

    # Test manage_file permission enforcement (Reversible requires approval)
    with pytest.raises(PermissionDeniedError):
        await registry.execute(
            "manage_file",
            {"action": "archive_zip", "source": str(sample_file)},
        )

    # Confirm callback allows execution
    res = await registry.execute(
        "manage_file",
        {"action": "archive_zip", "source": str(sample_file)},
        confirm=lambda _defn, _args: True,
    )
    assert res["status"] == "ok"
    assert Path(res["destination"]).exists()


# --------------------------------------------------------------------------- Advanced Edge Cases


def test_search_files_limit_and_sorting_order(tmp_path: Path) -> None:
    now = time.time()
    for i in range(10):
        f = tmp_path / f"file_{i}.txt"
        f.write_text(f"content {i}", encoding="utf-8")
        # Ensure distinct timestamps: file_9 is newest, file_0 is oldest
        file_time = now - ((10 - i) * 100)
        os.utime(f, (file_time, file_time))

    res = search_files(directory=str(tmp_path), limit=3)
    assert len(res) == 3
    # First item should be the newest (file_9)
    assert res[0]["name"] == "file_9.txt"
    assert res[1]["name"] == "file_8.txt"
    assert res[2]["name"] == "file_7.txt"


def test_read_document_empty_file(tmp_path: Path) -> None:
    empty_file = tmp_path / "empty.txt"
    empty_file.write_text("", encoding="utf-8")
    res = read_document(str(empty_file))
    assert res["total_chars"] == 0
    assert res["truncated"] is False
    assert res["content"] == ""


def test_read_document_corrupt_docx_raises_value_error(tmp_path: Path) -> None:
    corrupt = tmp_path / "bad.docx"
    corrupt.write_text("not a zip file", encoding="utf-8")
    with pytest.raises(ValueError, match="Corrupt or invalid DOCX archive"):
        read_document(str(corrupt))


def test_read_document_corrupt_xlsx_raises_value_error(tmp_path: Path) -> None:
    corrupt = tmp_path / "bad.xlsx"
    corrupt.write_text("not a zip file", encoding="utf-8")
    with pytest.raises(ValueError, match="Corrupt or invalid XLSX archive"):
        read_document(str(corrupt))


def test_read_document_python_docx_installed_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    docx_file = tmp_path / "doc.docx"
    docx_file.write_text("dummy", encoding="utf-8")

    mock_docx = MagicMock()
    mock_doc = MagicMock()
    p1 = MagicMock()
    p1.text = "Para 1 from python-docx"
    mock_doc.paragraphs = [p1]
    mock_doc.tables = []
    mock_docx.Document.return_value = mock_doc

    monkeypatch.setitem(sys.modules, "docx", mock_docx)

    res = read_document(str(docx_file))
    assert res["file_type"] == "docx"
    assert "Para 1 from python-docx" in res["content"]


def test_read_document_openpyxl_installed_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    xlsx_file = tmp_path / "sheet.xlsx"
    xlsx_file.write_text("dummy", encoding="utf-8")

    mock_openpyxl = MagicMock()
    mock_wb = MagicMock()
    mock_wb.sheetnames = ["Summary"]
    mock_ws = MagicMock()
    mock_ws.iter_rows.return_value = [("Item", "Value"), ("Widget", 42)]
    mock_wb.__getitem__.return_value = mock_ws
    mock_openpyxl.load_workbook.return_value = mock_wb

    monkeypatch.setitem(sys.modules, "openpyxl", mock_openpyxl)

    res = read_document(str(xlsx_file))
    assert res["file_type"] == "xlsx"
    assert "--- Sheet: Summary ---" in res["content"]
    assert "Widget\t42" in res["content"]


def test_read_document_fitz_installed_branch(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    pdf_file = tmp_path / "sample.pdf"
    pdf_file.write_text("%PDF-1.4 mock", encoding="utf-8")

    # pypdf not installed, fitz installed
    monkeypatch.setitem(sys.modules, "pypdf", None)

    mock_fitz = MagicMock()
    mock_page = MagicMock()
    mock_page.get_text.return_value = "Content extracted via PyMuPDF"
    mock_doc = [mock_page]
    mock_fitz.open.return_value = mock_doc

    monkeypatch.setitem(sys.modules, "fitz", mock_fitz)

    res = read_document(str(pdf_file))
    assert res["file_type"] == "pdf"
    assert "Content extracted via PyMuPDF" in res["content"]


def test_manage_file_missing_arguments(tmp_path: Path) -> None:
    src = tmp_path / "file.txt"
    src.write_text("data", encoding="utf-8")

    with pytest.raises(ValueError, match="destination path is required for 'move'"):
        manage_file(action="move", source=str(src))

    with pytest.raises(ValueError, match="destination name is required for 'rename'"):
        manage_file(action="rename", source=str(src))

    with pytest.raises(ValueError, match="destination path is required for 'copy'"):
        manage_file(action="copy", source=str(src))

    with pytest.raises(FileNotFoundError):
        manage_file(action="move", source=str(tmp_path / "missing.txt"), destination="dest.txt")


def test_manage_file_organize_folder_destination_collision(tmp_path: Path) -> None:
    downloads = tmp_path / "Downloads"
    downloads.mkdir()

    # Create an existing file inside Documents
    docs_dir = downloads / "Documents"
    docs_dir.mkdir()
    (docs_dir / "report.pdf").write_text("existing", encoding="utf-8")

    # Create a new report.pdf in Downloads
    (downloads / "report.pdf").write_text("new report", encoding="utf-8")

    # Organize with confirm=True
    res = manage_file(action="organize_folder", source=str(downloads), confirm=True)
    assert res["status"] if "status" in res else res["confirm"] is True
    # The new file should have been renamed with suffix _1 to avoid collision
    assert (docs_dir / "report.pdf").read_text(encoding="utf-8") == "existing"
    assert (docs_dir / "report_1.pdf").read_text(encoding="utf-8") == "new report"


def test_read_document_xlsx_with_non_sequential_sheet_rels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    # Ensure stdlib fallback is tested
    monkeypatch.setitem(sys.modules, "openpyxl", None)

    xlsx_path = tmp_path / "deleted_sheet.xlsx"
    with zipfile.ZipFile(xlsx_path, "w", zipfile.ZIP_DEFLATED) as z:
        # workbook has a sheet pointing to rId1
        z.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main" '
            'xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">'
            '<sheets><sheet name="ActiveData" sheetId="2" r:id="rId1"/></sheets></workbook>',
        )
        # rels maps rId1 to worksheets/sheet2.xml (sheet1 was deleted)
        z.writestr(
            "xl/_rels/workbook.xml.rels",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">'
            '<Relationship Id="rId1" '
            'Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/worksheet" '
            'Target="worksheets/sheet2.xml"/></Relationships>',
        )
        # sheet2.xml has the actual content
        z.writestr(
            "xl/worksheets/sheet2.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData><row r="1">'
            '<c r="A1" t="inlineStr"><is><t>Found Data In Sheet 2</t></is></c>'
            "</row></sheetData></worksheet>",
        )

    res = read_document(str(xlsx_path))
    assert res["file_type"] == "xlsx"
    assert "--- Sheet: ActiveData ---" in res["content"]
    assert "Found Data In Sheet 2" in res["content"]


def test_read_document_xlsx_sparse_columns_alignment(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "openpyxl", None)

    xlsx_path = tmp_path / "sparse.xlsx"
    with zipfile.ZipFile(xlsx_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "xl/workbook.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<workbook xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheets><sheet name="Sparse" sheetId="1"/></sheets></workbook>',
        )
        # Row 1 has A1 and C1 (B1 empty). Row 2 has A2, B2, C2.
        z.writestr(
            "xl/worksheets/sheet1.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<worksheet xmlns="http://schemas.openxmlformats.org/spreadsheetml/2006/main">'
            '<sheetData>'
            '<row r="1">'
            '<c r="A1" t="inlineStr"><is><t>Name</t></is></c>'
            '<c r="C1" t="inlineStr"><is><t>Score</t></is></c>'
            "</row>"
            '<row r="2">'
            '<c r="A2" t="inlineStr"><is><t>Alice</t></is></c>'
            '<c r="B2" t="inlineStr"><is><t>GradeA</t></is></c>'
            '<c r="C2" t="inlineStr"><is><t>95</t></is></c>'
            "</row>"
            "</sheetData></worksheet>",
        )

    res = read_document(str(xlsx_path))
    lines = res["content"].splitlines()
    assert lines[1] == "Name\t\tScore"
    assert lines[2] == "Alice\tGradeA\t95"


def test_read_document_docx_sdt_and_breaks(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setitem(sys.modules, "docx", None)

    docx_path = tmp_path / "sdt_breaks.docx"
    with zipfile.ZipFile(docx_path, "w", zipfile.ZIP_DEFLATED) as z:
        z.writestr(
            "word/document.xml",
            '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>'
            '<w:document xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">'
            "<w:body>"
            "<w:sdt><w:sdtContent><w:p><w:r><w:t>Header inside SDT</w:t></w:r></w:p>"
            "</w:sdtContent></w:sdt>"
            "<w:p>"
            "<w:r><w:t>First line</w:t></w:r>"
            "<w:r><w:br/></w:r>"
            "<w:r><w:t>Second line</w:t></w:r>"
            "<w:r><w:tab/></w:r>"
            "<w:r><w:t>Tabbed part</w:t></w:r>"
            "</w:p>"
            "</w:body></w:document>",
        )

    res = read_document(str(docx_path))
    assert res["file_type"] == "docx"
    assert "Header inside SDT" in res["content"]
    assert "First line\nSecond line\tTabbed part" in res["content"]


def test_read_document_text_line_and_row_truncation_info(tmp_path: Path) -> None:
    # 1. Plain text file
    txt_path = tmp_path / "lines.txt"
    txt_path.write_text("\n".join(f"Line {i}" for i in range(1, 21)), encoding="utf-8")

    res = read_document(str(txt_path), max_chars=50)
    assert res["total_lines"] == 20
    assert res["lines_returned"] < 20
    assert res["truncated"] is True
    assert "truncation_info" in res
    assert "Truncated to" in res["truncation_info"]

    # 2. CSV file
    csv_path = tmp_path / "table.csv"
    csv_path.write_text("col1,col2\nval1,val2\nval3,val4", encoding="utf-8")
    res_csv = read_document(str(csv_path))
    assert res_csv["file_type"] == "csv"
    assert res_csv["total_rows"] == 3
    assert res_csv["rows_returned"] == 3
    assert res_csv["truncated"] is False


def test_read_document_text_invalid_section_raises(tmp_path: Path) -> None:
    txt = tmp_path / "doc.txt"
    txt.write_text("L1\nL2\nL3", encoding="utf-8")

    with pytest.raises(ValueError, match="Invalid section specification"):
        read_document(str(txt), section="non_numeric")

    with pytest.raises(ValueError, match="Invalid section line count"):
        read_document(str(txt), section=0)

    with pytest.raises(ValueError, match="Invalid section line range"):
        read_document(str(txt), section="5-2")


def test_search_files_glob_character_classes(tmp_path: Path) -> None:
    (tmp_path / "item_1.txt").write_text("1", encoding="utf-8")
    (tmp_path / "item_2.txt").write_text("2", encoding="utf-8")
    (tmp_path / "item_a.txt").write_text("a", encoding="utf-8")

    res = search_files(query="item_[0-9].txt", directory=str(tmp_path))
    names = {r["name"] for r in res}
    assert names == {"item_1.txt", "item_2.txt"}
    assert "item_a.txt" not in names


def test_search_files_whitespace_extension_and_none_directory(tmp_path: Path) -> None:
    (tmp_path / "valid.txt").write_text("hi", encoding="utf-8")

    # query=None should not crash
    res = search_files(query=None, directory=str(tmp_path), extension="   ")
    assert len(res) == 1
    assert res[0]["name"] == "valid.txt"

    # negative modified_within_days raises ValueError
    with pytest.raises(ValueError, match="modified_within_days must be non-negative"):
        search_files(directory=str(tmp_path), modified_within_days=-2)


def test_manage_file_organize_folder_ignores_system_files(tmp_path: Path) -> None:
    folder = tmp_path / "organized_test"
    folder.mkdir()
    (folder / "desktop.ini").write_text("[ShellClassInfo]", encoding="utf-8")
    (folder / "thumbs.db").write_text("mockdb", encoding="utf-8")
    (folder / "contract.pdf").write_text("pdf", encoding="utf-8")

    # Dry run preview
    dry_run = manage_file(action="organize_folder", source=str(folder), confirm=False)
    assert dry_run["total_files"] == 1
    assert "desktop.ini" in dry_run["skipped_files"]
    assert "thumbs.db" in dry_run["skipped_files"]

    # Confirm run
    res = manage_file(action="organize_folder", source=str(folder), confirm=True)
    assert res["total_files"] == 1
    assert (folder / "desktop.ini").exists()
    assert (folder / "thumbs.db").exists()
    assert (folder / "Documents" / "contract.pdf").exists()


def test_manage_file_archive_zip_inside_source_folder(tmp_path: Path) -> None:
    project = tmp_path / "my_project"
    project.mkdir()
    (project / "file.txt").write_text("code", encoding="utf-8")
    dest_zip = project / "bundle.zip"

    res = manage_file(action="archive_zip", source=str(project), destination=str(dest_zip))
    assert res["status"] == "ok"
    assert dest_zip.exists()
    with zipfile.ZipFile(dest_zip, "r") as z:
        names = z.namelist()
        assert "file.txt" in names
        assert "bundle.zip" not in names


def test_manage_file_archive_zip_destination_directory(tmp_path: Path) -> None:
    project = tmp_path / "repo"
    project.mkdir()
    (project / "code.py").write_text("print(1)", encoding="utf-8")

    backup_dir = tmp_path / "backups"
    backup_dir.mkdir()

    res = manage_file(action="archive_zip", source=str(project), destination=str(backup_dir))
    assert res["status"] == "ok"
    expected_zip = backup_dir / "repo.zip"
    assert expected_zip.exists()


def test_manage_file_move_and_copy_destination_directory_hint(tmp_path: Path) -> None:
    f = tmp_path / "doc.txt"
    f.write_text("hello", encoding="utf-8")

    # Copy with directory hint (trailing slash) to non-existent directory
    dest_dir_str = str(tmp_path / "new_folder") + "/"
    res_copy = manage_file(action="copy", source=str(f), destination=dest_dir_str)
    assert res_copy["status"] == "ok"
    assert (tmp_path / "new_folder" / "doc.txt").exists()

    # Move with directory hint
    dest_dir_str2 = str(tmp_path / "new_folder2") + "\\"
    res_move = manage_file(action="move", source=str(f), destination=dest_dir_str2)
    assert res_move["status"] == "ok"
    assert not f.exists()
    assert (tmp_path / "new_folder2" / "doc.txt").exists()


def test_manage_file_same_source_and_destination(tmp_path: Path) -> None:
    f = tmp_path / "same.txt"
    f.write_text("identity", encoding="utf-8")

    res_move = manage_file(action="move", source=str(f), destination=str(f))
    assert res_move["status"] == "ok"
    assert "identical" in res_move["message"]

    res_copy = manage_file(action="copy", source=str(f), destination=str(f))
    assert res_copy["status"] == "ok"
    assert "identical" in res_copy["message"]


def test_read_document_pdf_pure_python_stream(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    import zlib

    monkeypatch.setitem(sys.modules, "pypdf", None)
    monkeypatch.setitem(sys.modules, "fitz", None)
    monkeypatch.setattr("shutil.which", lambda _name: None)

    # Build a minimal valid PDF containing a FlateDecode text stream
    pdf_path = tmp_path / "pure_python.pdf"
    content_stream = b"BT /F1 12 Tf 72 712 Td (Hello Pure Python PDF) Tj ET"
    compressed_stream = zlib.compress(content_stream)

    pdf_bytes = (
        b"%PDF-1.4\n"
        b"1 0 obj\n"
        b"<< /Length " + str(len(compressed_stream)).encode() + b" /Filter /FlateDecode >>\n"
        b"stream\n" + compressed_stream + b"\nendstream\n"
        b"endobj\n"
        b"%%EOF\n"
    )
    pdf_path.write_bytes(pdf_bytes)

    res = read_document(str(pdf_path))
    assert res["file_type"] == "pdf"
    assert "Hello Pure Python PDF" in res["content"]



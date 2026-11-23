"""Tests for the XLSX/XLSM renderer in pixcell.docs.xlsx."""

from __future__ import annotations

import datetime
import importlib.util
import subprocess
import sys
import zipfile
from pathlib import Path

import pytest

import pixcell.docs as docs
from pixcell.docs import HTML, DocsError, DocumentTooLarge, MissingExtra
from pixcell.docs import xlsx as xlsx_mod

HAS_OPENPYXL = importlib.util.find_spec("openpyxl") is not None
requires_openpyxl = pytest.mark.skipif(not HAS_OPENPYXL, reason="openpyxl is not installed")

XLSX_MIME = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
XLSM_MIME = "application/vnd.ms-excel.sheet.macroenabled.12"


def _inject_cached_formula_value(xlsx_path: Path, formula_expr: str, cached_value: str) -> None:
    """Add a ``<v>`` cached result after ``<f>{formula_expr}</f>`` in sheet1.xml.

    ``openpyxl`` writes formulas without evaluating them, whereas Excel/LibreOffice save
    the last computed value in ``<v>`` so ``data_only=True`` readers can display it.
    """
    with zipfile.ZipFile(xlsx_path, "r") as zin:
        entries = {name: zin.read(name) for name in zin.namelist()}

    sheet_xml = entries["xl/worksheets/sheet1.xml"].decode("utf-8")
    needle = f"<f>{formula_expr}</f>"
    assert needle in sheet_xml, f"Formula {needle!r} not found in sheet1.xml"
    entries["xl/worksheets/sheet1.xml"] = sheet_xml.replace(
        needle, f"{needle}<v>{cached_value}</v>", 1
    ).encode("utf-8")

    with zipfile.ZipFile(xlsx_path, "w", compression=zipfile.ZIP_DEFLATED) as zout:
        for name, data in entries.items():
            zout.writestr(name, data)


def _build_sample_workbook(path: Path) -> Path:
    """Build a 2-sheet workbook with merged cells, cached formula, date, styles, and unsafe text."""
    import openpyxl
    from openpyxl.styles import Font, PatternFill

    wb = openpyxl.Workbook()
    wb.properties.title = "Budget & Forecast <Q1>"

    ws1 = wb.active
    ws1.title = "Summary"
    ws1["A1"] = "Quarterly Summary"
    ws1.merge_cells("A1:C1")
    ws1["A1"].font = Font(bold=True)
    ws1["A1"].fill = PatternFill(fill_type="solid", start_color="FFFFEEAA", end_color="FFFFEEAA")

    ws1["A2"] = "Item"
    ws1["B2"] = "Amount"
    ws1["C2"] = "Date"
    ws1["A3"] = '<script>alert("xss")</script> & "quotes"'
    ws1["B3"] = "=100+250.5"
    ws1["B3"].number_format = "$#,##0.00"
    ws1["C3"] = datetime.date(2025, 4, 15)
    ws1["C3"].number_format = "yyyy-mm-dd"
    ws1["A4"] = "Margin"
    ws1["B4"] = 0.275
    ws1["B4"].number_format = "0.0%"
    ws1["C4"] = True

    ws2 = wb.create_sheet(title="Details <2>")
    ws2["A1"] = "Region"
    ws2["B1"] = "Units"
    ws2["A2"] = "North"
    ws2.merge_cells("A2:A3")
    ws2["B2"] = 42
    ws2["B3"] = 58

    wb.save(path)
    _inject_cached_formula_value(path, "100+250.5", "350.5")
    return path


def test_registry_resolves_xlsx_xlsm_mime_and_sniffing(tmp_path: Path):
    assert docs.supports("budget.xlsx")
    assert docs.supports("macros.xlsm")
    assert docs.resolve("BUDGET.XLSX").name == "xlsx"
    assert docs.resolve("MACROS.XLSM").name == "xlsx"
    assert docs.resolve("budget.xlsx").extra == "xlsx"
    assert docs.resolve("blob.bin", mime_type=XLSX_MIME).name == "xlsx"
    assert docs.resolve("blob.bin", mime_type=XLSM_MIME).name == "xlsx"

    # Extensionless OOXML spreadsheet sniffed via xl/workbook.xml
    no_ext = tmp_path / "downloaded_spreadsheet"
    with zipfile.ZipFile(no_ext, "w") as zf:
        zf.writestr("xl/workbook.xml", "<workbook/>")
    assert docs.supports(no_ext)
    assert docs.resolve(no_ext).name == "xlsx"


def test_importing_pixcell_docs_does_not_import_openpyxl():
    code = (
        "import sys, pixcell.docs as docs\n"
        "assert docs.supports('budget.xlsx')\n"
        "assert docs.supports('macros.xlsm')\n"
        "assert 'openpyxl' not in sys.modules\n"
        "assert 'pixcell.docs.xlsx' not in sys.modules\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_missing_extra_when_openpyxl_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    xlsx_path = tmp_path / "sample.xlsx"
    xlsx_path.write_bytes(b"PK\x03\x04dummy")
    monkeypatch.setitem(sys.modules, "openpyxl", None)

    with pytest.raises(MissingExtra) as exc_info:
        docs.open(xlsx_path)
    assert exc_info.value.extra == "xlsx"
    assert 'pip install "pixcell[xlsx]"' in str(exc_info.value)

    with pytest.raises(MissingExtra) as direct_exc:
        xlsx_mod.render(xlsx_path)
    assert direct_exc.value.extra == "xlsx"


@requires_openpyxl
def test_xlsx_renders_sheets_merges_formula_dates_styles_and_escaping(tmp_path: Path):
    xlsx_path = _build_sample_workbook(tmp_path / "budget.xlsx")

    doc = docs.open(xlsx_path)
    assert isinstance(doc, HTML)
    assert doc.source == xlsx_path
    assert doc.title == "Budget & Forecast <Q1>"

    # Sheet order preserved and titles escaped
    pos_summary = doc.html.index('data-name="Summary"')
    pos_details = doc.html.index('data-name="Details &lt;2&gt;"')
    assert pos_summary < pos_details
    assert "<h2>Summary</h2>" in doc.html
    assert "<h2>Details &lt;2&gt;</h2>" in doc.html
    assert "<title>Budget &amp; Forecast &lt;Q1&gt;</title>" in doc.html

    # Merged cell colspan & rowspan, bold font, and solid RGB fill
    assert 'colspan="3"' in doc.html
    assert 'rowspan="2"' in doc.html
    assert "font-weight:bold" in doc.html
    assert "background-color:#FFEEAA" in doc.html
    assert "Quarterly Summary" in doc.html

    # Formula renders cached value formatted as currency, not raw formula text
    assert "$350.50" in doc.html
    assert "=100+250.5" not in doc.html

    # Date, percentage, and boolean formatting
    assert "2025-04-15" in doc.html
    assert "27.5%" in doc.html
    assert ">TRUE<" in doc.html

    # HTML-unsafe cell text is escaped
    assert "<script>" not in doc.html
    assert (
        "&lt;script&gt;alert(&quot;xss&quot;)&lt;/script&gt; &amp; &quot;quotes&quot;"
        in doc.html
    )

    # Extensionless workbook opened via OOXML magic-byte sniffing
    no_ext = _build_sample_workbook(tmp_path / "unnamed_workbook")
    sniffed = docs.open(no_ext)
    assert isinstance(sniffed, HTML)
    assert "$350.50" in sniffed.html

    # .xlsm suffix opens through the same renderer
    xlsm_path = _build_sample_workbook(tmp_path / "macros.xlsm")
    xlsm_doc = docs.open(xlsm_path)
    assert isinstance(xlsm_doc, HTML)
    assert "$350.50" in xlsm_doc.html


@requires_openpyxl
def test_xlsx_read_only_mode_renders_cached_values_and_styles(tmp_path: Path):
    xlsx_path = _build_sample_workbook(tmp_path / "ro.xlsx")

    doc = docs.open(xlsx_path, read_only=True)
    assert isinstance(doc, HTML)
    assert "Quarterly Summary" in doc.html
    assert "$350.50" in doc.html
    assert "2025-04-15" in doc.html
    assert "font-weight:bold" in doc.html
    assert "background-color:#FFEEAA" in doc.html
    assert "<script>" not in doc.html


@requires_openpyxl
def test_xlsx_respects_max_pages_and_row_col_truncation(tmp_path: Path):
    import openpyxl

    wb = openpyxl.Workbook()
    ws1 = wb.active
    ws1.title = "Sheet-One"
    for r in range(1, 11):
        for c in range(1, 7):
            ws1.cell(row=r, column=c, value=f"R{r}C{c}")

    for idx in range(2, 5):
        ws = wb.create_sheet(title=f"Sheet-{idx}")
        ws["A1"] = f"Marker-{idx}"

    path = tmp_path / "grid.xlsx"
    wb.save(path)

    # Cap sheets via max_pages and cap rows/cols per sheet
    doc = docs.open(path, max_pages=2, max_rows=3, max_cols=4)
    assert isinstance(doc, HTML)

    # Sheet count capped at 2 with truncation notice
    assert "Sheet-One" in doc.html
    assert "Sheet-2" in doc.html
    assert "Sheet-3" not in doc.html
    assert "Sheet-4" not in doc.html
    assert "showing 2 of 4 sheets" in doc.html

    # Rows capped at 3 and columns capped at 4 with truncation notice
    assert "R3C4" in doc.html
    assert "R4C1" not in doc.html
    assert "R1C5" not in doc.html
    assert "rows truncated at 3" in doc.html
    assert "columns truncated at 4" in doc.html

    # Also verify truncation in streaming read_only=True mode
    doc_ro = docs.open(path, max_pages=1, max_rows=3, max_cols=4, read_only=True)
    assert "R3C4" in doc_ro.html
    assert "R4C1" not in doc_ro.html
    assert "R1C5" not in doc_ro.html
    assert "rows truncated at 3" in doc_ro.html
    assert "columns truncated at 4" in doc_ro.html


@requires_openpyxl
def test_xlsx_validation_zip_bomb_and_invalid_archive(tmp_path: Path):
    import openpyxl

    wb = openpyxl.Workbook()
    ws = wb.active
    for row_idx in range(1, 15):
        ws.cell(row=row_idx, column=1, value=f"Row{row_idx}-" + ("A" * 10_000))
    bomb_path = tmp_path / "bomb.xlsx"
    wb.save(bomb_path)
    assert bomb_path.stat().st_size < 10_000

    with pytest.raises(DocumentTooLarge):
        docs.open(bomb_path, max_bytes=50_000)

    corrupt = tmp_path / "bad.xlsx"
    corrupt.write_bytes(b"not a zip archive")
    with pytest.raises(DocsError):
        docs.open(corrupt)

    with pytest.raises(ValueError, match="max_rows"):
        xlsx_mod.render(bomb_path, max_rows=0)
    with pytest.raises(ValueError, match="max_cols"):
        xlsx_mod.render(bomb_path, max_cols=0)
    with pytest.raises(ValueError, match="max_pages"):
        xlsx_mod.render(bomb_path, max_pages=0)

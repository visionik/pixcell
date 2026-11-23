"""XLSX/XLSM renderer using ``openpyxl`` for :mod:`pixcell.docs`.

``openpyxl`` (MIT) lives behind the ``pixcell[xlsx]`` optional extra and is imported
lazily only when a spreadsheet is opened.

Read-only vs. merged-cell tradeoff
----------------------------------
``openpyxl``'s streaming ``read_only=True`` mode parses worksheet XML on the fly without
materialising a full cell graph in memory, which keeps large spreadsheets fast and bounded.
However, ``ReadOnlyWorksheet`` does not expose ``merged_cells`` ranges. By default
(``read_only=None``), :func:`render` inspects the OOXML archive's uncompressed size first:
workbooks up to :data:`DEFAULT_READ_ONLY_THRESHOLD_BYTES` (2 MiB uncompressed) are opened
normally (``read_only=False, data_only=True``) so merged cells render with accurate
``colspan``/``rowspan`` attributes, while larger workbooks switch to
``read_only=True, data_only=True`` to avoid high memory use (trading off merged-cell
spanning). Callers may also pass ``read_only=True`` or ``read_only=False`` explicitly.
"""

from __future__ import annotations

import datetime
import html
import math
import re
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

from pixcell.docs.errors import DocsError, DocumentTooLarge, MissingExtra
from pixcell.docs.types import DEFAULT_MAX_BYTES, DEFAULT_MAX_PAGES, HTML

DEFAULT_MAX_ROWS = 1000
"""Default maximum number of rows rendered per worksheet."""

DEFAULT_MAX_COLS = 100
"""Default maximum number of columns rendered per worksheet."""

DEFAULT_READ_ONLY_THRESHOLD_BYTES = 2 * 1024 * 1024
"""Uncompressed archive size above which ``read_only=None`` switches to streaming mode."""

_DC_TITLE_TAG = "{http://purl.org/dc/elements/1.1/}title"
_HEX_RGB_RE = re.compile(r"^[0-9A-Fa-f]{6}([0-9A-Fa-f]{2})?$")
_QUOTED_TEXT_RE = re.compile(r'"[^"]*"')
_BRACKET_TAG_RE = re.compile(r"\[[^\]]*\]")
_CURRENCY_BRACKET_RE = re.compile(r"\[\$([^-\]]+)")
_DECIMALS_RE = re.compile(r"\.([0#?]+)")

_STYLESHEET = """\
body { font-family: system-ui, -apple-system, sans-serif; margin: 1rem; color: #1f2328; }
.xlsx-sheet { margin-bottom: 2rem; page-break-after: always; break-after: page; }
.xlsx-sheet:last-of-type { page-break-after: auto; break-after: auto; }
.xlsx-sheet h2 { font-size: 1.1rem; margin: 0 0 0.5rem 0; }
.xlsx-table { border-collapse: collapse; font-size: 0.9rem; }
.xlsx-table td { border: 1px solid #d0d7de; padding: 0.25rem 0.5rem; vertical-align: top; white-space: pre-wrap; }
.xlsx-table td.num { text-align: right; font-variant-numeric: tabular-nums; }
.xlsx-empty, .xlsx-truncated { color: #57606a; font-style: italic; font-size: 0.85rem; margin: 0.5rem 0; }
"""


def _import_openpyxl() -> Any:
    try:
        import openpyxl
    except ImportError as exc:
        raise MissingExtra(
            "xlsx",
            renderer="xlsx",
            dependency=getattr(exc, "name", None) or "openpyxl",
        ) from exc
    return openpyxl


def _inspect_xlsx_archive(path: Path, *, max_bytes: int | None) -> tuple[str, int]:
    """Validate uncompressed size guard rail and read optional ``dc:title`` metadata.

    Returns ``(title, total_uncompressed_bytes)``.
    """
    try:
        with zipfile.ZipFile(path, "r") as archive:
            total_uncompressed = sum(max(0, info.file_size) for info in archive.infolist())
            if max_bytes is not None and total_uncompressed > max_bytes:
                raise DocumentTooLarge(path, size=total_uncompressed, max_bytes=max_bytes)

            title = ""
            if "docProps/core.xml" in archive.namelist():
                info = archive.getinfo("docProps/core.xml")
                if info.file_size <= 1_048_576:
                    try:
                        root = ET.fromstring(archive.read("docProps/core.xml"))
                    except ET.ParseError:
                        root = None
                    if root is not None:
                        for elem in root.iter():
                            if (
                                elem.tag == _DC_TITLE_TAG
                                or elem.tag.rsplit("}", 1)[-1].lower() == "title"
                            ):
                                text = "".join(elem.itertext()).strip()
                                if text:
                                    title = text
                                    break
            return title, total_uncompressed
    except zipfile.BadZipFile as exc:
        raise DocsError(f"Invalid XLSX archive {path}: {exc}") from exc


def _clean_format_section(number_format: str, *, negative: bool = False) -> tuple[str, str]:
    """Return ``(cleaned_section, currency_symbol)`` for a spreadsheet number format."""
    sections = number_format.split(";")
    section = sections[1] if negative and len(sections) > 1 and sections[1] else sections[0]
    currency = ""
    bracket_match = _CURRENCY_BRACKET_RE.search(section)
    if bracket_match:
        currency = bracket_match.group(1)
    cleaned = _QUOTED_TEXT_RE.sub("", section)
    cleaned = _BRACKET_TAG_RE.sub("", cleaned)
    cleaned = re.sub(r"\\.", "", cleaned)
    if not currency:
        for sym in ("$", "€", "£", "¥"):
            if sym in cleaned:
                currency = sym
                break
    return cleaned, currency


def _format_datetime(value: datetime.datetime, number_format: str) -> str:
    cleaned, _ = _clean_format_section(number_format)
    lower = cleaned.lower()
    has_date_tokens = any(tok in lower for tok in ("y", "d"))
    has_time_tokens = any(tok in lower for tok in ("h", "s", "am/pm", "a/p"))
    has_time_value = (value.hour, value.minute, value.second, value.microsecond) != (0, 0, 0, 0)

    if has_time_tokens and not has_date_tokens:
        if "s" not in lower and value.second == 0:
            return value.strftime("%H:%M")
        return value.strftime("%H:%M:%S")
    if not has_time_tokens and not has_time_value:
        return value.strftime("%Y-%m-%d")
    if "s" not in lower and value.second == 0:
        return value.strftime("%Y-%m-%d %H:%M")
    return value.strftime("%Y-%m-%d %H:%M:%S")


def _format_number(value: int | float, number_format: str) -> str:
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return str(value)

    fmt = (number_format or "General").strip()
    if not fmt or fmt.lower() in ("general", "@"):
        if isinstance(value, int):
            return str(value)
        if value.is_integer() and abs(value) < 1e15:
            return str(int(value))
        return f"{value:.15g}"

    negative = value < 0
    cleaned, currency = _clean_format_section(fmt, negative=negative)
    dec_match = _DECIMALS_RE.search(cleaned)
    decimals = len(dec_match.group(1)) if dec_match else 0
    use_grouping = "," in cleaned

    if "%" in cleaned:
        scaled = value * 100
        sep = "," if use_grouping else ""
        return f"{scaled:{sep}.{decimals}f}%"

    if "e+" in cleaned.lower() or "e-" in cleaned.lower():
        return f"{value:.{decimals}E}"

    if any(ch in cleaned for ch in ("0", "#")):
        sep = "," if use_grouping else ""
        if currency:
            sign = "-" if negative else ""
            return f"{sign}{currency}{abs(value):{sep}.{decimals}f}"
        return f"{value:{sep}.{decimals}f}"

    if isinstance(value, int):
        return str(value)
    if value.is_integer() and abs(value) < 1e15:
        return str(int(value))
    return f"{value:.15g}"


def _format_cell_value(cell: Any) -> tuple[str, bool]:
    """Format a cell's cached value as plain text and report whether it is numeric."""
    value = cell.value
    if value is None:
        return "", False
    if isinstance(value, bool):
        return ("TRUE" if value else "FALSE"), False
    number_format = getattr(cell, "number_format", None) or "General"
    if isinstance(value, datetime.datetime):
        return _format_datetime(value, number_format), False
    if isinstance(value, datetime.date):
        return value.isoformat(), False
    if isinstance(value, datetime.time):
        if value.second == 0 and "s" not in number_format.lower():
            return value.strftime("%H:%M"), False
        return value.strftime("%H:%M:%S"), False
    if isinstance(value, datetime.timedelta):
        return str(value), False
    if isinstance(value, (int, float)):
        return _format_number(value, number_format), True
    return str(value), False


def _extract_fill_hex(cell: Any) -> str | None:
    """Return a CSS ``#RRGGBB`` background colour for solid RGB fills, or ``None``."""
    fill = getattr(cell, "fill", None)
    if fill is None or getattr(fill, "fill_type", None) != "solid":
        return None
    color = getattr(fill, "fgColor", None) or getattr(fill, "start_color", None)
    if color is None or getattr(color, "type", None) != "rgb":
        return None
    rgb = getattr(color, "rgb", None)
    if not isinstance(rgb, str) or not _HEX_RGB_RE.match(rgb):
        return None
    if len(rgb) == 8:
        # OOXML uses AARRGGBB; alpha 00 denotes transparent / no fill sentinel (e.g. 00000000).
        if rgb[:2] == "00":
            return None
        return f"#{rgb[2:].upper()}"
    return f"#{rgb.upper()}"


def _cell_style(cell: Any) -> str:
    styles: list[str] = []
    font = getattr(cell, "font", None)
    if font is not None and getattr(font, "bold", False):
        styles.append("font-weight:bold")
    bg = _extract_fill_hex(cell)
    if bg is not None:
        styles.append(f"background-color:{bg}")
    return ";".join(styles)


def _collect_merges(
    ws: Any,
    *,
    num_rows: int,
    num_cols: int,
) -> tuple[dict[tuple[int, int], tuple[int, int]], set[tuple[int, int]]]:
    """Build merged-cell span and covered-cell maps clamped to the rendered grid."""
    anchors: dict[tuple[int, int], tuple[int, int]] = {}
    covered: set[tuple[int, int]] = set()
    merged = getattr(ws, "merged_cells", None)
    if merged is None:
        return anchors, covered

    for cell_range in getattr(merged, "ranges", ()):
        min_col, min_row, max_col, max_row = cell_range.bounds
        if min_row > num_rows or min_col > num_cols:
            continue
        end_row = min(max_row, num_rows)
        end_col = min(max_col, num_cols)
        rowspan = end_row - min_row + 1
        colspan = end_col - min_col + 1
        if rowspan == 1 and colspan == 1:
            continue
        anchors[(min_row, min_col)] = (rowspan, colspan)
        for r in range(min_row, end_row + 1):
            for c in range(min_col, end_col + 1):
                if (r, c) != (min_row, min_col):
                    covered.add((r, c))
    return anchors, covered


def _render_sheet(
    ws: Any,
    *,
    sheet_index: int,
    sheet_name: str,
    max_rows: int | None,
    max_cols: int | None,
) -> str:
    sheet_max_row = ws.max_row
    sheet_max_col = ws.max_column

    iter_max_row: int | None = None
    if max_rows is not None:
        if sheet_max_row is not None and sheet_max_row > 0:
            iter_max_row = min(sheet_max_row, max_rows + 1)
        else:
            iter_max_row = max_rows + 1

    iter_max_col: int | None = None
    if max_cols is not None and sheet_max_col is not None and sheet_max_col > 0:
        iter_max_col = min(sheet_max_col, max_cols + 1)

    rows: list[tuple[Any, ...]] = []
    rows_truncated = False
    cols_truncated = False

    for row_idx, raw_row in enumerate(
        ws.iter_rows(min_row=1, max_row=iter_max_row, min_col=1, max_col=iter_max_col),
        start=1,
    ):
        if max_rows is not None and row_idx > max_rows:
            rows_truncated = True
            break
        if max_cols is not None and len(raw_row) > max_cols:
            cols_truncated = True
            raw_row = raw_row[:max_cols]
        rows.append(raw_row)

    if max_cols is not None and sheet_max_col is not None and sheet_max_col > max_cols:
        cols_truncated = True

    num_rows = len(rows)
    num_cols = max((len(r) for r in rows), default=0)
    anchors, covered = _collect_merges(ws, num_rows=num_rows, num_cols=num_cols)

    has_content = bool(anchors) or any(
        cell.value is not None or _extract_fill_hex(cell) is not None
        for row in rows
        for cell in row
    )

    escaped_name = html.escape(sheet_name, quote=True)
    parts = [
        f'<section class="xlsx-sheet" data-sheet="{sheet_index}" data-name="{escaped_name}">',
        f"<h2>{html.escape(sheet_name, quote=False)}</h2>",
    ]

    if not has_content or num_rows == 0 or num_cols == 0:
        parts.append('<p class="xlsx-empty">Empty sheet</p>')
    else:
        table_lines = ['<table class="xlsx-table">', "<tbody>"]
        for r_idx, row in enumerate(rows, start=1):
            cell_html: list[str] = []
            for c_idx in range(1, num_cols + 1):
                if (r_idx, c_idx) in covered:
                    continue
                cell = row[c_idx - 1] if c_idx <= len(row) else None
                attrs: list[str] = []
                span = anchors.get((r_idx, c_idx))
                if span is not None:
                    rowspan, colspan = span
                    if rowspan > 1:
                        attrs.append(f'rowspan="{rowspan}"')
                    if colspan > 1:
                        attrs.append(f'colspan="{colspan}"')
                if cell is not None:
                    text, is_numeric = _format_cell_value(cell)
                    if is_numeric:
                        attrs.append('class="num"')
                    style = _cell_style(cell)
                    if style:
                        attrs.append(f'style="{style}"')
                else:
                    text = ""
                attr_str = (" " + " ".join(attrs)) if attrs else ""
                cell_html.append(f"<td{attr_str}>{html.escape(text, quote=True)}</td>")
            table_lines.append("<tr>" + "".join(cell_html) + "</tr>")
        table_lines.append("</tbody>")
        table_lines.append("</table>")
        parts.append("\n".join(table_lines))

    trunc_notes: list[str] = []
    if rows_truncated and max_rows is not None:
        if sheet_max_row is not None and sheet_max_row > max_rows:
            trunc_notes.append(f"rows truncated at {max_rows} of {sheet_max_row}")
        else:
            trunc_notes.append(f"rows truncated at {max_rows}")
    if cols_truncated and max_cols is not None:
        if sheet_max_col is not None and sheet_max_col > max_cols:
            trunc_notes.append(f"columns truncated at {max_cols} of {sheet_max_col}")
        else:
            trunc_notes.append(f"columns truncated at {max_cols}")
    if trunc_notes:
        note = "; ".join(trunc_notes)
        parts.append(f'<p class="xlsx-truncated">Truncated: {html.escape(note, quote=False)}.</p>')

    parts.append("</section>")
    return "\n".join(parts)


def render(
    path: Path,
    *,
    max_pages: int | None = DEFAULT_MAX_PAGES,
    max_bytes: int | None = DEFAULT_MAX_BYTES,
    max_rows: int | None = DEFAULT_MAX_ROWS,
    max_cols: int | None = DEFAULT_MAX_COLS,
    read_only: bool | None = None,
) -> HTML:
    """Convert an ``.xlsx``/``.xlsm`` workbook to self-contained :class:`HTML` via ``openpyxl``.

    Each worksheet is rendered as a ``<section class="xlsx-sheet">`` containing an HTML
    table of cached cell values (``data_only=True``).

    Parameters
    ----------
    path:
        Path to the ``.xlsx`` or ``.xlsm`` file.
    max_pages:
        Maximum number of worksheets to render (``None`` for no cap).
    max_bytes:
        Maximum total uncompressed size across ZIP archive entries (``None`` for no cap).
        Raises :class:`~pixcell.docs.DocumentTooLarge` if exceeded.
    max_rows:
        Maximum number of rows rendered per worksheet (default :data:`DEFAULT_MAX_ROWS`).
    max_cols:
        Maximum number of columns rendered per worksheet (default :data:`DEFAULT_MAX_COLS`).
    read_only:
        Whether to open the workbook with ``openpyxl``'s streaming ``read_only`` mode.
        When ``None`` (the default), workbooks with uncompressed size up to
        :data:`DEFAULT_READ_ONLY_THRESHOLD_BYTES` are loaded with ``read_only=False`` so
        merged cells emit ``colspan``/``rowspan``, and larger workbooks use
        ``read_only=True`` to bound memory usage.
    """
    if max_pages is not None and max_pages < 1:
        raise ValueError("max_pages must be at least 1 or None")
    if max_bytes is not None and max_bytes < 0:
        raise ValueError("max_bytes must be non-negative or None")
    if max_rows is not None and max_rows < 1:
        raise ValueError("max_rows must be at least 1 or None")
    if max_cols is not None and max_cols < 1:
        raise ValueError("max_cols must be at least 1 or None")

    openpyxl = _import_openpyxl()
    title, uncompressed_size = _inspect_xlsx_archive(path, max_bytes=max_bytes)

    use_read_only = (
        uncompressed_size > DEFAULT_READ_ONLY_THRESHOLD_BYTES
        if read_only is None
        else bool(read_only)
    )

    try:
        with path.open("rb") as stream:
            wb = openpyxl.load_workbook(stream, read_only=use_read_only, data_only=True)
            try:
                all_sheet_names = list(wb.sheetnames)
                total_sheets = len(all_sheet_names)
                if max_pages is not None and total_sheets > max_pages:
                    sheet_names = all_sheet_names[:max_pages]
                    sheets_truncated = True
                else:
                    sheet_names = all_sheet_names
                    sheets_truncated = False

                sections: list[str] = []
                for idx, sheet_name in enumerate(sheet_names, start=1):
                    ws = wb[sheet_name]
                    sections.append(
                        _render_sheet(
                            ws,
                            sheet_index=idx,
                            sheet_name=sheet_name,
                            max_rows=max_rows,
                            max_cols=max_cols,
                        )
                    )
            finally:
                wb.close()
    except DocsError:
        raise
    except Exception as exc:
        raise DocsError(f"Failed to open XLSX {path}: {exc}") from exc

    if sheets_truncated and max_pages is not None:
        sections.append(
            f'<p class="xlsx-truncated">Truncated: showing {len(sheet_names)} of '
            f"{total_sheets} sheets (max_pages={max_pages}).</p>"
        )

    head_parts = ['<meta charset="utf-8">']
    if title:
        head_parts.append(f"<title>{html.escape(title, quote=False)}</title>")
    head_parts.append(f"<style>\n{_STYLESHEET}</style>")
    head_html = "\n".join(head_parts)
    body_html = "\n".join(sections)

    document_html = (
        "<!DOCTYPE html>\n"
        "<html>\n"
        "<head>\n"
        f"{head_html}\n"
        "</head>\n"
        "<body>\n"
        f"{body_html}\n"
        "</body>\n"
        "</html>\n"
    )
    return HTML(html=document_html, title=title, source=path)

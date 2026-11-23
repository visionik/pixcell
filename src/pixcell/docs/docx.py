"""DOCX renderer using ``mammoth`` for :mod:`pixcell.docs`.

``mammoth`` (BSD-2-Clause) lives behind the ``pixcell[docx]`` optional extra and is
imported lazily only when a ``.docx`` document is opened.
"""

from __future__ import annotations

import html
import logging
import xml.etree.ElementTree as ET
import zipfile
from pathlib import Path
from typing import Any

from pixcell.docs.errors import DocsError, DocumentTooLarge, MissingExtra
from pixcell.docs.types import DEFAULT_MAX_BYTES, HTML

logger = logging.getLogger(__name__)

_DC_TITLE_TAG = "{http://purl.org/dc/elements/1.1/}title"


def _import_mammoth() -> Any:
    try:
        import mammoth
    except ImportError as exc:
        raise MissingExtra(
            "docx",
            renderer="docx",
            dependency=getattr(exc, "name", None) or "mammoth",
        ) from exc
    return mammoth


def _inspect_docx_archive(path: Path, *, max_bytes: int | None) -> str:
    """Validate uncompressed size guard rail and read optional ``dc:title`` metadata."""
    try:
        with zipfile.ZipFile(path, "r") as archive:
            if max_bytes is not None:
                total_uncompressed = sum(max(0, info.file_size) for info in archive.infolist())
                if total_uncompressed > max_bytes:
                    raise DocumentTooLarge(path, size=total_uncompressed, max_bytes=max_bytes)

            if "docProps/core.xml" not in archive.namelist():
                return ""
            info = archive.getinfo("docProps/core.xml")
            if info.file_size > 1_048_576:
                return ""
            try:
                root = ET.fromstring(archive.read("docProps/core.xml"))
            except ET.ParseError:
                return ""
            for elem in root.iter():
                if elem.tag == _DC_TITLE_TAG or elem.tag.rsplit("}", 1)[-1].lower() == "title":
                    text = "".join(elem.itertext()).strip()
                    if text:
                        return text
    except zipfile.BadZipFile as exc:
        raise DocsError(f"Invalid DOCX archive {path}: {exc}") from exc
    return ""


def render(
    path: Path,
    *,
    style_map: str | None = None,
    max_bytes: int | None = DEFAULT_MAX_BYTES,
) -> HTML:
    """Convert a ``.docx`` file to self-contained :class:`HTML` via ``mammoth``.

    Embedded images are converted to inline ``data:`` URIs (mammoth's default). Any
    conversion warnings or errors reported by mammoth are logged via :mod:`logging`.
    """
    if max_bytes is not None and max_bytes < 0:
        raise ValueError("max_bytes must be non-negative or None")

    mammoth = _import_mammoth()
    title = _inspect_docx_archive(path, max_bytes=max_bytes)

    kwargs: dict[str, Any] = {}
    if style_map is not None:
        kwargs["style_map"] = style_map

    try:
        with path.open("rb") as stream:
            result = mammoth.convert_to_html(stream, **kwargs)
    except DocsError:
        raise
    except Exception as exc:
        raise DocsError(f"Failed to convert DOCX {path}: {exc}") from exc

    for msg in getattr(result, "messages", ()):
        msg_type = getattr(msg, "type", "warning")
        msg_text = getattr(msg, "message", str(msg))
        if msg_type == "error":
            logger.error("mammoth (%s): %s", path, msg_text)
        else:
            logger.warning("mammoth (%s): %s", path, msg_text)

    head_parts = ['<meta charset="utf-8">']
    if title:
        head_parts.append(f"<title>{html.escape(title)}</title>")
    head_html = "\n".join(head_parts)
    document_html = (
        "<!DOCTYPE html>\n"
        "<html>\n"
        "<head>\n"
        f"{head_html}\n"
        "</head>\n"
        "<body>\n"
        f"{result.value}\n"
        "</body>\n"
        "</html>\n"
    )
    return HTML(html=document_html, title=title, source=path)

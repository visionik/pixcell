"""Tests for the DOCX renderer in pixcell.docs.docx."""

from __future__ import annotations

import base64
import importlib.util
import io
import logging
import sys
import zipfile
from pathlib import Path

import pytest
from PIL import Image as PILImage

import pixcell.docs as docs
from pixcell.docs import HTML, DocsError, DocumentTooLarge, MissingExtra
from pixcell.docs import docx as docx_mod

HAS_MAMMOTH = importlib.util.find_spec("mammoth") is not None
requires_mammoth = pytest.mark.skipif(not HAS_MAMMOTH, reason="mammoth is not installed")

DOCX_MIME = "application/vnd.openxmlformats-officedocument.wordprocessingml.document"


def _make_png_bytes() -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", (4, 4), (40, 120, 220)).save(buf, format="PNG")
    return buf.getvalue()


def _build_docx(
    path: Path,
    *,
    title: str = "Quarterly Memo",
    include_image: bool = False,
    unknown_style: str | None = None,
    extra_padding: int = 0,
) -> tuple[Path, bytes]:
    """Build a minimal valid OOXML .docx file using only stdlib zipfile."""
    png_bytes = _make_png_bytes()

    content_types = """\
<?xml version="1.0" encoding="UTF-8"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="png" ContentType="image/png"/>
  <Override PartName="/word/document.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.document.main+xml"/>
  <Override PartName="/word/styles.xml" ContentType="application/vnd.openxmlformats-officedocument.wordprocessingml.styles+xml"/>
  <Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>
</Types>
"""

    root_rels = """\
<?xml version="1.0" encoding="UTF-8"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="word/document.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
</Relationships>
"""

    doc_rels_entries = [
        '  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/styles" Target="styles.xml"/>'
    ]
    if include_image:
        doc_rels_entries.append(
            '  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="media/dot.png"/>'
        )
    doc_rels = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        + "\n".join(doc_rels_entries)
        + "\n</Relationships>\n"
    )

    styles_xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<w:styles xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main">
  <w:style w:type="paragraph" w:styleId="Heading1">
    <w:name w:val="heading 1"/>
  </w:style>
</w:styles>
"""

    image_paragraph = ""
    if include_image:
        image_paragraph = """\
    <w:p>
      <w:r>
        <w:drawing>
          <wp:inline xmlns:wp="http://schemas.openxmlformats.org/drawingml/2006/wordprocessingDrawing">
            <wp:docPr id="1" name="Dot" descr="Blue dot"/>
            <a:graphic xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
              <a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/picture">
                <pic:pic xmlns:pic="http://schemas.openxmlformats.org/drawingml/2006/picture">
                  <pic:blipFill>
                    <a:blip r:embed="rId2"/>
                  </pic:blipFill>
                </pic:pic>
              </a:graphicData>
            </a:graphic>
          </wp:inline>
        </w:drawing>
      </w:r>
    </w:p>
"""

    extra_style_para = ""
    if unknown_style:
        extra_style_para = f"""\
    <w:p>
      <w:pPr><w:pStyle w:val="{unknown_style}"/></w:pPr>
      <w:r><w:t>Custom styled text</w:t></w:r>
    </w:p>
"""

    padding_para = ""
    if extra_padding > 0:
        padding_para = f"    <w:p><w:r><w:t>{'X' * extra_padding}</w:t></w:r></w:p>\n"

    document_xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<w:document
    xmlns:w="http://schemas.openxmlformats.org/wordprocessingml/2006/main"
    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships">
  <w:body>
    <w:p>
      <w:pPr><w:pStyle w:val="Heading1"/></w:pPr>
      <w:r><w:t>Executive Summary</w:t></w:r>
    </w:p>
    <w:p>
      <w:r><w:t xml:space="preserve">This is </w:t></w:r>
      <w:r><w:rPr><w:b/></w:rPr><w:t>important bold</w:t></w:r>
      <w:r><w:t> text.</w:t></w:r>
    </w:p>
{image_paragraph}{extra_style_para}{padding_para}  </w:body>
</w:document>
"""

    core_xml = f"""\
<?xml version="1.0" encoding="UTF-8"?>
<cp:coreProperties
    xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
    xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:title>{title}</dc:title>
</cp:coreProperties>
"""

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("word/_rels/document.xml.rels", doc_rels)
        zf.writestr("word/styles.xml", styles_xml)
        zf.writestr("word/document.xml", document_xml)
        zf.writestr("docProps/core.xml", core_xml)
        if include_image:
            zf.writestr("word/media/dot.png", png_bytes)

    return path, png_bytes


def test_registry_resolves_docx_by_suffix_mime_and_sniffing(tmp_path: Path):
    assert docs.supports("memo.docx")
    assert docs.resolve("MEMO.DOCX").name == "docx"
    assert docs.resolve("memo.docx").extra == "docx"
    assert docs.resolve("blob.bin", mime_type=DOCX_MIME).name == "docx"

    no_ext, _ = _build_docx(tmp_path / "downloaded_word_doc")
    assert docs.supports(no_ext)
    assert docs.resolve(no_ext).name == "docx"


def test_missing_extra_when_mammoth_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    docx_path = tmp_path / "sample.docx"
    docx_path.write_bytes(b"PK\x03\x04dummy")
    monkeypatch.setitem(sys.modules, "mammoth", None)

    with pytest.raises(MissingExtra) as exc_info:
        docs.open(docx_path)
    assert exc_info.value.extra == "docx"
    assert 'pip install "pixcell[docx]"' in str(exc_info.value)

    with pytest.raises(MissingExtra) as direct_exc:
        docx_mod.render(docx_path)
    assert direct_exc.value.extra == "docx"


@requires_mammoth
def test_docx_converts_heading_bold_title_and_embedded_image(tmp_path: Path):
    docx_path, png_bytes = _build_docx(tmp_path / "memo.docx", include_image=True)

    doc = docs.open(docx_path)
    assert isinstance(doc, HTML)
    assert doc.source == docx_path
    assert doc.title == "Quarterly Memo"
    assert "<h1>Executive Summary</h1>" in doc.html
    assert "<strong>important bold</strong>" in doc.html
    assert "<p>This is <strong>important bold</strong> text.</p>" in doc.html

    expected_uri = f"data:image/png;base64,{base64.b64encode(png_bytes).decode('ascii')}"
    assert expected_uri in doc.html

    # Extensionless DOCX opened via OOXML magic-byte sniffing
    sniffed, _ = _build_docx(tmp_path / "unnamed_docx")
    sniffed_doc = docs.open(sniffed)
    assert isinstance(sniffed_doc, HTML)
    assert "<h1>Executive Summary</h1>" in sniffed_doc.html


@requires_mammoth
def test_docx_logs_mammoth_warnings(tmp_path: Path, caplog: pytest.LogCaptureFixture):
    docx_path, _ = _build_docx(tmp_path / "warn.docx", unknown_style="UnrecognizedCustomStyle")

    with caplog.at_level(logging.WARNING, logger="pixcell.docs.docx"):
        doc = docs.open(docx_path)
    assert isinstance(doc, HTML)
    assert any("UnrecognizedCustomStyle" in rec.message for rec in caplog.records)


@requires_mammoth
def test_docx_rejects_oversized_uncompressed_and_invalid_zip(tmp_path: Path):
    bomb_path, _ = _build_docx(tmp_path / "bomb.docx", extra_padding=200_000)
    assert bomb_path.stat().st_size < 10_000

    with pytest.raises(DocumentTooLarge):
        docs.open(bomb_path, max_bytes=50_000)

    corrupt = tmp_path / "bad.docx"
    corrupt.write_bytes(b"not a zip file")
    with pytest.raises(DocsError):
        docs.open(corrupt)

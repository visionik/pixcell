"""Tests for pixcell.docs registry, sniffing, guard rails, output types, and errors."""

from __future__ import annotations

import subprocess
import sys
import zipfile
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest
from PIL import Image as PILImage

import pixcell.docs as docs
from pixcell.docs import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_PAGES,
    HTML,
    SVG,
    DocsError,
    DocumentTooLarge,
    LazyPages,
    MissingExtra,
    Picture,
    Registry,
    Renderer,
    UnsupportedType,
    sniff_mime,
)


def test_importing_pixcell_docs_does_not_import_optional_backends():
    code = (
        "import sys, pixcell.docs as docs\n"
        "assert docs.supports('photo.png')\n"
        "assert docs.supports('diagram.svg')\n"
        "assert docs.supports('report.pdf')\n"
        "assert docs.supports('novel.epub')\n"
        "assert docs.supports('memo.docx')\n"
        "assert len(docs.renderers()) >= 5\n"
        "assert 'resvg_py' not in sys.modules\n"
        "assert 'pixcell.docs.svg' not in sys.modules\n"
        "assert 'pypdfium2' not in sys.modules\n"
        "assert 'pixcell.docs.pdf' not in sys.modules\n"
        "assert 'mammoth' not in sys.modules\n"
        "assert 'pixcell.docs.docx' not in sys.modules\n"
        "assert 'pixcell.docs.epub' not in sys.modules\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_registry_picks_by_suffix_case_insensitive_and_compound():
    reg = Registry()
    reg.register("json", loader=lambda p: HTML(html=p.read_text()), suffixes=(".json",))
    reg.register(
        "excalidraw",
        loader=lambda p: SVG(svg="<svg/>"),
        suffixes=(".excalidraw", ".excalidraw.json"),
    )

    assert reg.resolve("BOARD.EXCALIDRAW").name == "excalidraw"
    assert reg.resolve("board.EXCALIDRAW.JSON").name == "excalidraw"
    assert reg.resolve("data.JSON").name == "json"
    assert reg.supports("board.excalidraw")
    assert not reg.supports("unknown.xyz")


def test_registry_picks_by_explicit_mime_type(tmp_path: Path):
    f = tmp_path / "blob.bin"
    f.write_text("<svg xmlns='http://www.w3.org/2000/svg' width='10' height='10'/>", encoding="utf-8")
    doc = docs.open(f, mime_type="image/svg+xml; charset=utf-8")
    assert isinstance(doc, SVG)
    assert doc.width == 10.0 and doc.height == 10.0


def test_sniff_mime_for_magic_bytes_and_zip_containers(tmp_path: Path):
    assert sniff_mime(b"%PDF-1.7\n...") == "application/pdf"
    assert sniff_mime(b"\x89PNG\r\n\x1a\nrest") == "image/png"
    assert sniff_mime(b"\xff\xd8\xff\xe0...") == "image/jpeg"
    assert sniff_mime(b"GIF89a...") == "image/gif"
    assert sniff_mime(b"RIFF\x00\x00\x00\x00WEBP...") == "image/webp"
    assert sniff_mime(b"II*\x00rest") == "image/tiff"
    assert sniff_mime(b"MM\x00*rest") == "image/tiff"
    assert sniff_mime(b"  \n<svg width='10' height='10'></svg>") == "image/svg+xml"
    assert sniff_mime(b"\xef\xbb\xbf<?xml version='1.0'?><svg></svg>") == "image/svg+xml"
    assert sniff_mime(b"<!DOCTYPE html><html></html>") == "text/html"
    assert sniff_mime(b"plain text without magic") is None

    # EPUB fast path (uncompressed mimetype entry in header)
    epub_path = tmp_path / "book_no_ext"
    with zipfile.ZipFile(epub_path, "w", compression=zipfile.ZIP_STORED) as zf:
        zf.writestr("mimetype", "application/epub+zip")
    assert sniff_mime(epub_path.read_bytes()[:256], epub_path) == "application/epub+zip"

    # OOXML containers (DOCX, XLSX, PPTX) and plain ZIP
    for marker, expected in [
        ("word/document.xml", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
        ("xl/workbook.xml", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
        ("ppt/presentation.xml", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
        ("hello.txt", "application/zip"),
    ]:
        zpath = tmp_path / f"archive_{marker.replace('/', '_')}"
        with zipfile.ZipFile(zpath, "w") as zf:
            zf.writestr(marker, "<root/>")
        assert sniff_mime(zpath.read_bytes()[:64], zpath) == expected


def test_registry_picks_by_sniffing_mime_and_custom_sniffer(tmp_path: Path):
    reg = Registry()
    reg.register(
        "pdf",
        loader=lambda p: HTML(html=f"<p>{p.name}</p>", base=p.parent, source=p),
        suffixes=(".pdf",),
        mime_types=("application/pdf",),
        extra="pdf",
    )
    reg.register(
        "mermaid",
        loader=lambda p: SVG(svg="<svg/>", source=p),
        suffixes=(".mmd",),
        sniff=lambda header, _path: header.lstrip().startswith(b"graph "),
    )

    pdf_no_ext = tmp_path / "downloaded_doc"
    pdf_no_ext.write_bytes(b"%PDF-1.7\n%%EOF\n")
    assert reg.supports(pdf_no_ext)
    assert reg.resolve(pdf_no_ext).name == "pdf"
    opened = reg.open(pdf_no_ext)
    assert isinstance(opened, HTML)
    assert opened.base == tmp_path

    mmd_no_ext = tmp_path / "flowchart"
    mmd_no_ext.write_text("graph TD\n  A --> B\n", encoding="utf-8")
    assert reg.resolve(mmd_no_ext).name == "mermaid"
    assert isinstance(reg.open(mmd_no_ext), SVG)


def test_missing_extra_from_open_for_custom_and_svg_renderers(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    reg = Registry()

    def broken_pdf_renderer(_path: Path):
        import _pixcell_nonexistent_pdf_backend  # type: ignore[import-not-found]  # noqa: F401

    reg.register("pdf", loader=broken_pdf_renderer, suffixes=(".pdf",), extra="pdf")
    pdf_file = tmp_path / "sample.pdf"
    pdf_file.write_bytes(b"%PDF-1.7\n")

    with pytest.raises(MissingExtra) as exc_info:
        reg.open(pdf_file)
    assert isinstance(exc_info.value, DocsError)
    assert exc_info.value.extra == "pdf"
    assert 'pip install "pixcell[pdf]"' in str(exc_info.value)

    # Simulate missing resvg_py when rasterising via pixcell.docs.open
    svg_file = tmp_path / "sample.svg"
    svg_file.write_text("<svg xmlns='http://www.w3.org/2000/svg' width='20' height='20'/>", encoding="utf-8")
    monkeypatch.setitem(sys.modules, "resvg_py", None)

    with pytest.raises(MissingExtra) as svg_exc:
        docs.open(svg_file, rasterize=True)
    assert svg_exc.value.extra == "svg"
    assert 'pip install "pixcell[svg]"' in str(svg_exc.value)


def test_unsupported_type_errors(tmp_path: Path):
    unknown = tmp_path / "notes.unknownext"
    unknown.write_text("just some arbitrary text", encoding="utf-8")
    assert not docs.supports(unknown)
    with pytest.raises(UnsupportedType) as exc_info:
        docs.open(unknown)
    assert isinstance(exc_info.value, DocsError)
    assert exc_info.value.path == unknown
    assert exc_info.value.suffix == ".unknownext"

    # Extensionless HTML has a sniffed MIME type, with no HTML renderer registered yet.
    html_file = tmp_path / "mystery"
    html_file.write_bytes(b"<!DOCTYPE html><html><body>hi</body></html>\n")
    with pytest.raises(UnsupportedType) as html_exc:
        docs.open(html_file)
    assert html_exc.value.mime_type == "text/html"


def test_guard_rails_max_bytes_and_max_pages(tmp_path: Path):
    assert DEFAULT_MAX_BYTES == 50 * 1024 * 1024
    assert DEFAULT_MAX_PAGES == 200

    big = tmp_path / "big.svg"
    big.write_text("<svg xmlns='http://www.w3.org/2000/svg'>" + (" " * 200) + "</svg>", encoding="utf-8")
    with pytest.raises(DocumentTooLarge) as exc_info:
        docs.open(big, max_bytes=50)
    assert exc_info.value.max_bytes == 50
    assert exc_info.value.size > 50
    # Disabling the guard rail works
    assert isinstance(docs.open(big, max_bytes=None), SVG)

    # max_pages caps lazily without rendering pages
    rendered_indices: list[int] = []
    reg = Registry()

    def multi_page_renderer(path: Path) -> Picture:
        def make_page(index: int) -> PILImage.Image:
            rendered_indices.append(index)
            return PILImage.new("RGB", (4, 4), (index, 0, 0))

        return Picture(pages=LazyPages(10, make_page), source=path)

    reg.register("multi", loader=multi_page_renderer, suffixes=(".multi",))
    doc_file = tmp_path / "deck.multi"
    doc_file.write_bytes(b"data")

    pic = reg.open(doc_file, max_pages=3)
    assert isinstance(pic, Picture)
    assert rendered_indices == []  # still lazy!
    assert len(pic) == 3 and pic.page_count == 3
    assert pic.total_pages == 10 and pic.truncated is True
    assert pic.pages[2].getpixel((0, 0)) == (2, 0, 0)
    assert rendered_indices == [2]
    with pytest.raises(IndexError):
        _ = pic.pages[3]

    # Invalid guard rail values
    with pytest.raises(ValueError):
        docs.open(big, max_bytes=-1)
    with pytest.raises(ValueError):
        docs.open(big, max_pages=0)


def test_output_types_frozen_lazy_pages_and_html_base(tmp_path: Path):
    called: list[int] = []
    closed = []

    pages = LazyPages(
        4,
        lambda i: (called.append(i), PILImage.new("RGB", (2, 2), (i * 50, 0, 0)))[1],
        cache=True,
        close=lambda: closed.append(True),
    )
    pic = Picture(pages=pages, title="demo")
    with pytest.raises(FrozenInstanceError):
        pic.title = "changed"  # type: ignore[misc]

    assert len(pages) == 4 and called == []
    first = pages[0]
    assert pages[0] is first  # cached
    assert pages[-1].getpixel((0, 0)) == (150, 0, 0)
    assert len(pages[1:3]) == 2
    assert called == [0, 3, 1, 2]
    with pic:
        pass
    assert closed == [True]

    html = HTML(html="<h1>Hi</h1>", base=tmp_path)
    assert html.base == tmp_path
    assert html.base_url == tmp_path.resolve().as_uri()
    assert HTML(html="<p/>", base="https://example.com/docs/").base_url == "https://example.com/docs/"
    assert HTML(html="<p/>").base_url is None


def test_register_override_priority_and_unregister(tmp_path: Path):
    reg = Registry()
    reg.register("first", loader=lambda p: HTML(html="first"), suffixes=(".foo",), priority=0)
    reg.register("second", loader=lambda p: HTML(html="second"), suffixes=(".foo",), priority=0)
    assert reg.resolve("x.foo").name == "second"

    reg.register("high", loader=lambda p: HTML(html="high"), suffixes=(".foo",), priority=10)
    assert reg.resolve("x.foo").name == "high"
    assert reg.unregister("high") is True
    assert reg.unregister("nonexistent") is False
    assert reg.resolve("x.foo").name == "second"

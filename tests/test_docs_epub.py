"""Tests for the built-in EPUB renderer in pixcell.docs.epub."""

from __future__ import annotations

import base64
import io
import zipfile
from pathlib import Path

import pytest
from PIL import Image as PILImage

import pixcell.docs as docs
from pixcell.docs import HTML, DocsError, DocumentTooLarge
from pixcell.docs import epub as epub_mod


def _make_png_bytes(colour: tuple[int, int, int] = (200, 40, 60), size: tuple[int, int] = (4, 4)) -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", size, colour).save(buf, format="PNG")
    return buf.getvalue()


def _build_epub(
    path: Path,
    *,
    title: str = "A Tale of Two Chapters",
    chapters: list[tuple[str, str]] | None = None,
    extra_files: dict[str, bytes | str] | None = None,
    manifest_extra: list[tuple[str, str, str]] | None = None,
    compress_mimetype: bool = False,
) -> tuple[Path, bytes]:
    """Build a minimal valid EPUB archive using only stdlib zipfile."""
    png_bytes = _make_png_bytes()
    if chapters is None:
        chapters = [
            (
                "text/ch1.xhtml",
                """\
<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
  <head>
    <title>Chapter 1</title>
    <link rel="stylesheet" type="text/css" href="../styles/book.css"/>
    <script type="text/javascript">window.evilHead = 1;</script>
  </head>
  <body>
    <h1 onclick="alert('nope')">Chapter One</h1>
    <p>First<script>alert("xss")</script> paragraph &amp; intro &nbsp;text.</p>
    <img src="../images/pixel.png" alt="red dot"/>
    <a href="javascript:alert(1)">link</a>
  </body>
</html>
""",
            ),
            (
                "text/ch2.xhtml",
                """\
<?xml version="1.0" encoding="UTF-8"?>
<html xmlns="http://www.w3.org/1999/xhtml">
  <head><title>Chapter 2</title></head>
  <body>
    <h1>Chapter Two</h1>
    <p>Second chapter body.</p>
  </body>
</html>
""",
            ),
        ]

    manifest_items = [
        '    <item id="img1" href="images/pixel.png" media-type="image/png"/>',
        '    <item id="css1" href="styles/book.css" media-type="text/css"/>',
    ]
    spine_items: list[str] = []
    for idx, (href, _content) in enumerate(chapters, start=1):
        item_id = f"ch{idx}"
        manifest_items.append(
            f'    <item id="{item_id}" href="{href}" media-type="application/xhtml+xml"/>'
        )
        spine_items.append(f'    <itemref idref="{item_id}"/>')
    for item_id, href, media_type in manifest_extra or ():
        manifest_items.append(
            f'    <item id="{item_id}" href="{href}" media-type="{media_type}"/>'
        )

    opf = (
        '<?xml version="1.0" encoding="UTF-8"?>\n'
        '<package xmlns="http://www.idpf.org/2007/opf" version="3.0" unique-identifier="uid">\n'
        '  <metadata xmlns:dc="http://purl.org/dc/elements/1.1/">\n'
        f"    <dc:title>{title}</dc:title>\n"
        "  </metadata>\n"
        "  <manifest>\n"
        + "\n".join(manifest_items)
        + "\n  </manifest>\n"
        "  <spine>\n"
        + "\n".join(spine_items)
        + "\n  </spine>\n"
        "</package>\n"
    )

    container_xml = """\
<?xml version="1.0" encoding="UTF-8"?>
<container version="1.0" xmlns="urn:oasis:names:tc:opendocument:xmlns:container">
  <rootfiles>
    <rootfile full-path="OEBPS/content.opf" media-type="application/oebps-package+xml"/>
  </rootfiles>
</container>
"""

    css = "body { font-family: serif; background: url('../images/pixel.png'); }\n"

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        # Per EPUB OCF spec, mimetype is stored uncompressed as the first entry.
        zf.writestr(
            "mimetype",
            "application/epub+zip",
            compress_type=zipfile.ZIP_DEFLATED if compress_mimetype else zipfile.ZIP_STORED,
        )
        zf.writestr("META-INF/container.xml", container_xml)
        zf.writestr("OEBPS/content.opf", opf)
        zf.writestr("OEBPS/images/pixel.png", png_bytes)
        zf.writestr("OEBPS/styles/book.css", css)
        for href, content in chapters:
            zf.writestr(f"OEBPS/{href}", content)
        for member_name, payload in (extra_files or {}).items():
            zf.writestr(member_name, payload)

    return path, png_bytes


def test_registry_resolves_epub_by_suffix_mime_and_sniffing(tmp_path: Path):
    assert docs.supports("novel.epub")
    assert docs.resolve("NOVEL.EPUB").name == "epub"
    assert docs.resolve("novel.epub").extra is None
    assert docs.resolve("blob.bin", mime_type="application/epub+zip").name == "epub"

    # Extensionless EPUB (uncompressed mimetype first -> fast path)
    no_ext, _ = _build_epub(tmp_path / "book_without_suffix")
    assert docs.supports(no_ext)
    assert docs.resolve(no_ext).name == "epub"
    opened = docs.open(no_ext)
    assert isinstance(opened, HTML)
    assert opened.title == "A Tale of Two Chapters"

    # Extensionless EPUB with compressed mimetype -> zip inspection fallback path
    compressed_mime, _ = _build_epub(tmp_path / "book_compressed_mime", compress_mimetype=True)
    assert docs.resolve(compressed_mime).name == "epub"
    opened2 = docs.open(compressed_mime)
    assert isinstance(opened2, HTML)
    assert opened2.title == "A Tale of Two Chapters"


def test_epub_chapters_order_title_inline_assets_and_script_stripping(tmp_path: Path):
    epub_path, png_bytes = _build_epub(tmp_path / "sample.epub")

    doc = docs.open(epub_path)
    assert isinstance(doc, HTML)
    assert doc.title == "A Tale of Two Chapters"
    assert doc.source == epub_path
    assert doc.base is None

    # Title in <title> and chapter order matches spine order.
    assert "<title>A Tale of Two Chapters</title>" in doc.html
    pos_ch1 = doc.html.index("Chapter One")
    pos_ch2 = doc.html.index("Chapter Two")
    assert pos_ch1 < pos_ch2
    assert 'data-chapter="1"' in doc.html
    assert 'data-chapter="2"' in doc.html

    # Relative image inlined as PNG data URI, and CSS inlined with its url(...) rewritten too.
    expected_uri = f"data:image/png;base64,{base64.b64encode(png_bytes).decode('ascii')}"
    assert f'src="{expected_uri}"' in doc.html
    assert "<style>" in doc.html
    assert f'url("{expected_uri}")' in doc.html
    assert "../images/pixel.png" not in doc.html
    assert "../styles/book.css" not in doc.html

    # Scripts, inline event handlers, and javascript: URIs are stripped; surrounding text survives.
    assert "<script" not in doc.html.lower()
    assert "evilHead" not in doc.html
    assert 'alert("xss")' not in doc.html
    assert "onclick" not in doc.html.lower()
    assert "javascript:" not in doc.html.lower()
    assert "First paragraph &amp; intro" in doc.html
    # No XML namespace prefixes like ns0:
    assert "ns0:" not in doc.html


def test_epub_respects_max_pages(tmp_path: Path):
    chapters = [
        (
            f"text/ch{i}.xhtml",
            f'<html xmlns="http://www.w3.org/1999/xhtml"><body><p>ChapterMarker-{i}</p></body></html>',
        )
        for i in range(1, 5)
    ]
    epub_path, _ = _build_epub(tmp_path / "multi.epub", chapters=chapters)

    doc = docs.open(epub_path, max_pages=2)
    assert isinstance(doc, HTML)
    assert "ChapterMarker-1" in doc.html
    assert "ChapterMarker-2" in doc.html
    assert "ChapterMarker-3" not in doc.html
    assert "ChapterMarker-4" not in doc.html

    with pytest.raises(ValueError, match="max_pages"):
        epub_mod.render(epub_path, max_pages=0)


@pytest.mark.parametrize(
    "bad_member",
    [
        "../outside.txt",
        "OEBPS/../../escape.txt",
        "/etc/passwd",
        r"\windows\system.ini",
        "C:/secret.txt",
    ],
)
def test_epub_rejects_path_traversal_and_absolute_members(tmp_path: Path, bad_member: str):
    epub_path, _ = _build_epub(
        tmp_path / "evil.epub",
        extra_files={bad_member: "pwned"},
    )
    with pytest.raises(DocsError):
        docs.open(epub_path)


def test_epub_zip_bomb_respects_max_bytes_on_uncompressed_size(tmp_path: Path):
    # Highly compressible payload: tiny on disk, large uncompressed.
    big_chapter = (
        '<html xmlns="http://www.w3.org/1999/xhtml"><body><p>'
        + ("A" * 200_000)
        + "</p></body></html>"
    )
    epub_path, _ = _build_epub(
        tmp_path / "bomb.epub",
        chapters=[("text/big.xhtml", big_chapter)],
    )
    assert epub_path.stat().st_size < 10_000  # compressed file is tiny

    with pytest.raises(DocumentTooLarge) as exc_info:
        docs.open(epub_path, max_bytes=50_000)
    assert exc_info.value.max_bytes == 50_000
    assert exc_info.value.size > 50_000

    # Disabling max_bytes allows opening it.
    doc = docs.open(epub_path, max_bytes=None)
    assert isinstance(doc, HTML)
    assert "AAAA" in doc.html


def test_epub_invalid_zip_raises_docs_error(tmp_path: Path):
    bad = tmp_path / "corrupt.epub"
    bad.write_bytes(b"not a zip archive")
    with pytest.raises(DocsError):
        docs.open(bad)

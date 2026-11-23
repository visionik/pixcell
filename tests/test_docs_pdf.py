"""Tests for the PDF renderer in pixcell.docs.pdf."""

from __future__ import annotations

import hashlib
import importlib.util
import struct
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image as PILImage

import pixcell.docs as docs
from pixcell.docs import DocsError, MissingExtra, Picture
from pixcell.docs import pdf as pdf_mod

HAS_PYPDFIUM2 = importlib.util.find_spec("pypdfium2") is not None
requires_pdfium = pytest.mark.skipif(not HAS_PYPDFIUM2, reason="pypdfium2 is not installed")

# Standard PDF 1.4 security handler padding string (ISO 32000-1, 7.6.3.3).
_PDF_PADDING = bytes.fromhex(
    "28bf4e5e4e758a4164004e56fffa0108"
    "2e2e00b6d0683e802f0ca9fe6453697a"
)


def _rc4(key: bytes, data: bytes) -> bytes:
    s = list(range(256))
    j = 0
    for i in range(256):
        j = (j + s[i] + key[i % len(key)]) & 0xFF
        s[i], s[j] = s[j], s[i]
    out = bytearray(len(data))
    i = j = 0
    for idx, byte in enumerate(data):
        i = (i + 1) & 0xFF
        j = (j + s[i]) & 0xFF
        s[i], s[j] = s[j], s[i]
        out[idx] = byte ^ s[(s[i] + s[j]) & 0xFF]
    return bytes(out)


def _pad_password(pw: str) -> bytes:
    return (pw.encode("latin-1") + _PDF_PADDING)[:32]


def _make_color_pdf(
    path: Path,
    colours: list[tuple[int, int, int]],
    *,
    size: tuple[int, int] = (120, 60),
) -> Path:
    images = [PILImage.new("RGB", size, colour) for colour in colours]
    images[0].save(
        path,
        "PDF",
        resolution=72.0,
        save_all=True,
        append_images=images[1:],
    )
    return path


def _make_text_pdf(
    path: Path,
    page_texts: list[str],
    *,
    user_password: str | None = None,
    owner_password: str = "owner",
) -> Path:
    """Build a minimal valid multi-page PDF (optionally RC4-40 encrypted) in pure Python."""
    file_id = b"0123456789abcdef"
    enc_key: bytes | None = None
    encrypt_obj: bytes | None = None
    encrypt_obj_num: int | None = None

    # Object layout:
    # 1: Catalog, 2: Pages, 3: Font, then per page (page_obj, content_obj), optional Encrypt obj
    page_obj_nums = [4 + 2 * i for i in range(len(page_texts))]
    if user_password is not None:
        p_val = -44  # standard restricted permissions int32
        o_key = hashlib.md5(_pad_password(owner_password)).digest()[:5]
        o_val = _rc4(o_key, _pad_password(user_password))
        enc_key = hashlib.md5(
            _pad_password(user_password) + o_val + struct.pack("<i", p_val) + file_id
        ).digest()[:5]
        u_val = _rc4(enc_key, _PDF_PADDING)
        encrypt_obj_num = 4 + 2 * len(page_texts)
        encrypt_obj = (
            f"<< /Filter /Standard /V 1 /R 2 /O <{o_val.hex()}> "
            f"/U <{u_val.hex()}> /P {p_val} >>"
        ).encode("ascii")

    objects: dict[int, bytes] = {
        1: b"<< /Type /Catalog /Pages 2 0 R >>",
        2: (
            f"<< /Type /Pages /Kids [{' '.join(f'{n} 0 R' for n in page_obj_nums)}] "
            f"/Count {len(page_texts)} >>"
        ).encode("ascii"),
        3: b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    }

    for i, text in enumerate(page_texts):
        page_num = page_obj_nums[i]
        content_num = page_num + 1
        escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
        stream_data = f"BT /F1 18 Tf 20 50 Td ({escaped}) Tj ET".encode("latin-1")
        if enc_key is not None:
            obj_key = hashlib.md5(
                enc_key + struct.pack("<I", content_num)[:3] + b"\x00\x00"
            ).digest()[:10]
            stream_data = _rc4(obj_key, stream_data)
        objects[page_num] = (
            f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 200 100] "
            f"/Contents {content_num} 0 R /Resources << /Font << /F1 3 0 R >> >> >>"
        ).encode("ascii")
        objects[content_num] = (
            f"<< /Length {len(stream_data)} >>\nstream\n".encode("ascii")
            + stream_data
            + b"\nendstream"
        )

    if encrypt_obj_num is not None and encrypt_obj is not None:
        objects[encrypt_obj_num] = encrypt_obj

    chunks = [b"%PDF-1.4\n"]
    offsets: dict[int, int] = {}
    pos = len(chunks[0])
    for num in sorted(objects):
        offsets[num] = pos
        entry = f"{num} 0 obj\n".encode("ascii") + objects[num] + b"\nendobj\n"
        chunks.append(entry)
        pos += len(entry)

    xref_pos = pos
    total_objs = max(objects) + 1
    xref_lines = [f"xref\n0 {total_objs}\n0000000000 65535 f \n".encode("ascii")]
    for num in range(1, total_objs):
        xref_lines.append(f"{offsets[num]:010d} 00000 n \n".encode("ascii"))
    chunks.extend(xref_lines)

    encrypt_ref = f" /Encrypt {encrypt_obj_num} 0 R" if encrypt_obj_num is not None else ""
    trailer = (
        f"trailer\n<< /Size {total_objs} /Root 1 0 R"
        f"{encrypt_ref} /ID [<{file_id.hex()}> <{file_id.hex()}>] >>\n"
        f"startxref\n{xref_pos}\n%%EOF\n"
    ).encode("ascii")
    chunks.append(trailer)
    path.write_bytes(b"".join(chunks))
    return path


def test_registry_resolves_pdf_by_suffix_mime_and_sniffing(tmp_path: Path):
    assert docs.supports("report.pdf")
    assert docs.resolve("REPORT.PDF").name == "pdf"
    assert docs.resolve("blob.bin", mime_type="application/pdf").name == "pdf"

    no_ext = tmp_path / "downloaded_pdf"
    no_ext.write_bytes(b"%PDF-1.7\n%%EOF\n")
    assert docs.supports(no_ext)
    assert docs.resolve(no_ext).name == "pdf"


def test_missing_extra_when_pypdfium2_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")
    monkeypatch.setitem(sys.modules, "pypdfium2", None)

    with pytest.raises(MissingExtra) as exc_info:
        docs.open(pdf_path)
    assert exc_info.value.extra == "pdf"
    assert 'pip install "pixcell[pdf]"' in str(exc_info.value)

    with pytest.raises(MissingExtra) as direct_exc:
        pdf_mod.render(pdf_path)
    assert direct_exc.value.extra == "pdf"


def test_render_validates_scale_and_max_pages(tmp_path: Path):
    pdf_path = tmp_path / "sample.pdf"
    pdf_path.write_bytes(b"%PDF-1.4\n%%EOF\n")

    with pytest.raises(ValueError, match="scale"):
        pdf_mod.render(pdf_path, scale=0)
    with pytest.raises(ValueError, match="scale"):
        pdf_mod.render(pdf_path, scale=-1.5)
    with pytest.raises(ValueError, match="max_pages"):
        pdf_mod.render(pdf_path, max_pages=0)


@requires_pdfium
def test_pdf_page_count_size_ratio_scale_and_pixel_colour(tmp_path: Path):
    pdf_path = _make_color_pdf(
        tmp_path / "slides.pdf",
        [(220, 30, 30), (30, 200, 40), (30, 40, 220)],
        size=(120, 60),
    )

    with docs.open(pdf_path) as doc:
        assert isinstance(doc, Picture)
        assert doc.page_count == 3
        assert doc.total_pages == 3
        assert doc.truncated is False
        assert doc.source == pdf_path

        # Default scale=2.0 -> 2x of 72dpi -> 240x120 (2:1 aspect ratio)
        p0 = doc.pages[0]
        assert p0.size == (240, 120)
        assert p0.width / p0.height == pytest.approx(2.0)

        r, g, b = p0.convert("RGB").getpixel((p0.width // 2, p0.height // 2))
        assert r > 190 and g < 60 and b < 60

        p1 = doc.pages[1]
        r1, g1, b1 = p1.convert("RGB").getpixel((p1.width // 2, p1.height // 2))
        assert g1 > 170 and r1 < 60 and b1 < 70

        p2 = doc.pages[2]
        r2, g2, b2 = p2.convert("RGB").getpixel((p2.width // 2, p2.height // 2))
        assert b2 > 190 and r2 < 60 and g2 < 70

    # Custom scale=1.0 renders at 72dpi (120x60)
    with docs.open(pdf_path, scale=1.0) as doc1:
        assert isinstance(doc1, Picture)
        assert doc1.image.size == (120, 60)

    # Extensionless PDF opened via %PDF magic-byte sniffing
    sniffed = tmp_path / "unnamed_document"
    sniffed.write_bytes(pdf_path.read_bytes())
    with docs.open(sniffed) as sniffed_doc:
        assert isinstance(sniffed_doc, Picture)
        assert sniffed_doc.page_count == 3


@requires_pdfium
def test_pdf_lazy_rendering(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    import pypdfium2 as pdfium

    pdf_path = _make_color_pdf(
        tmp_path / "lazy.pdf",
        [(200, 0, 0), (0, 200, 0), (0, 0, 200), (200, 200, 0)],
    )

    loaded_pages: list[int] = []
    render_calls = 0
    real_getitem = pdfium.PdfDocument.__getitem__
    real_render = pdfium.PdfPage.render

    def spy_getitem(self, index: int):
        loaded_pages.append(index)
        return real_getitem(self, index)

    def spy_render(self, *args, **kwargs):
        nonlocal render_calls
        render_calls += 1
        return real_render(self, *args, **kwargs)

    monkeypatch.setattr(pdfium.PdfDocument, "__getitem__", spy_getitem)
    monkeypatch.setattr(pdfium.PdfPage, "render", spy_render)

    with docs.open(pdf_path) as doc:
        assert isinstance(doc, Picture)
        assert len(doc) == 4
        assert loaded_pages == []  # nothing loaded or rendered on open
        assert render_calls == 0

        _ = doc.pages[2]
        assert loaded_pages == [2]
        assert render_calls == 1

        _ = doc.pages[0]
        assert loaded_pages == [2, 0]
        assert render_calls == 2


@requires_pdfium
def test_pdf_max_pages(tmp_path: Path):
    pdf_path = _make_color_pdf(
        tmp_path / "long.pdf",
        [(220, 0, 0), (0, 220, 0), (0, 0, 220), (220, 220, 0), (220, 0, 220)],
    )

    with docs.open(pdf_path, max_pages=2) as doc:
        assert isinstance(doc, Picture)
        assert doc.page_count == 2
        assert doc.total_pages == 5
        assert doc.truncated is True
        assert doc.pages[1].size == (240, 120)
        with pytest.raises(IndexError):
            _ = doc.pages[2]
        assert isinstance(doc.pages, pdf_mod.PdfPages)
        with pytest.raises(IndexError):
            doc.pages.page_text(2)


@requires_pdfium
def test_pdf_close_and_image_independence(tmp_path: Path):
    pdf_path = _make_color_pdf(tmp_path / "single.pdf", [(210, 40, 40)])

    with docs.open(pdf_path) as doc:
        assert isinstance(doc, Picture)
        rendered = doc.pages[0]

    # Calling close() again is idempotent.
    doc.close()

    # Previously rendered PIL image remains usable after the PDFium handle is closed.
    r, g, b = rendered.convert("RGB").getpixel((rendered.width // 2, rendered.height // 2))
    assert r > 180 and g < 70 and b < 70

    # Rendering new pages after explicit close fails cleanly.
    with pytest.raises(ValueError, match="closed"):
        _ = doc.pages[0]


@requires_pdfium
def test_pdf_thread_safety(tmp_path: Path):
    colours = [(220, 30, 30), (30, 200, 40), (30, 40, 220), (210, 210, 30)]
    pdf_path = _make_color_pdf(tmp_path / "threaded.pdf", colours, size=(80, 40))

    with docs.open(pdf_path, scale=1.5) as doc:
        assert isinstance(doc, Picture)
        indices = list(range(len(colours))) * 4

        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda idx: (idx, doc.pages[idx]), indices))

        for idx, img in results:
            assert img.size == (120, 60)
            pixel = img.convert("RGB").getpixel((60, 30))
            expected = colours[idx]
            for ch_got, ch_exp in zip(pixel, expected, strict=True):
                assert abs(ch_got - ch_exp) < 30


@requires_pdfium
def test_pdf_page_text(tmp_path: Path):
    pdf_path = _make_text_pdf(tmp_path / "text.pdf", ["Page One Hello", "Page Two World"])

    with docs.open(pdf_path) as doc:
        assert isinstance(doc, Picture)
        assert isinstance(doc.pages, pdf_mod.PdfPages)
        assert "Page One Hello" in doc.pages.page_text(0)
        assert "Page Two World" in doc.pages.page_text(-1)


@requires_pdfium
def test_pdf_password_protected(tmp_path: Path):
    pdf_path = _make_text_pdf(
        tmp_path / "locked.pdf",
        ["Top Secret"],
        user_password="secret",
    )

    with pytest.raises(pdf_mod.PdfPasswordError) as no_pw_exc:
        docs.open(pdf_path)
    assert isinstance(no_pw_exc.value, DocsError)
    assert "password" in str(no_pw_exc.value).lower()

    with pytest.raises(pdf_mod.PdfPasswordError) as wrong_pw_exc:
        docs.open(pdf_path, password="wrong")
    assert "incorrect password" in str(wrong_pw_exc.value).lower()

    with docs.open(pdf_path, password="secret") as doc:
        assert isinstance(doc, Picture)
        assert doc.page_count == 1
        assert doc.image.size == (400, 200)
        assert isinstance(doc.pages, pdf_mod.PdfPages)
        assert "Top Secret" in doc.pages.page_text(0)

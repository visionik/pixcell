"""Tests for the SVG renderer and resvg-py rasterisation helper in pixcell.docs.svg."""

from __future__ import annotations

import importlib.util
import io
import sys
from pathlib import Path

import pytest
from PIL import Image as PILImage

import pixcell.docs as docs
from pixcell.docs import SVG, MissingExtra, Picture
from pixcell.docs import svg as svg_mod

HAS_RESVG = importlib.util.find_spec("resvg_py") is not None
requires_resvg = pytest.mark.skipif(not HAS_RESVG, reason="resvg-py is not installed")

TEXT_SVG = """\
<?xml version="1.0" encoding="UTF-8"?>
<svg xmlns="http://www.w3.org/2000/svg" width="120" height="80" viewBox="0 0 120 80">
  <title>Glyph test</title>
  <rect width="120" height="80" fill="#ffffff"/>
  <text x="20" y="62" font-size="56" font-weight="bold" fill="#000000">H</text>
</svg>
"""


def _count_dark_and_light_pixels(image: PILImage.Image) -> tuple[int, int]:
    rgb = image.convert("RGB")
    raw = rgb.tobytes()
    dark = light = 0
    for offset in range(0, len(raw), 3):
        r, g, b = raw[offset], raw[offset + 1], raw[offset + 2]
        if r < 60 and g < 60 and b < 60:
            dark += 1
        elif r > 220 and g > 220 and b > 220:
            light += 1
    return dark, light


def test_open_svg_returns_svg_document_with_dimensions(tmp_path: Path):
    path = tmp_path / "glyph.svg"
    path.write_text(TEXT_SVG, encoding="utf-8")

    doc = docs.open(path)
    assert isinstance(doc, SVG)
    assert doc.width == 120.0
    assert doc.height == 80.0
    assert doc.title == "Glyph test"
    assert doc.source == path
    assert "<text" in doc.svg


def test_parse_svg_info_viewbox_and_units():
    w, h, title = svg_mod.parse_svg_info(
        '<svg xmlns="http://www.w3.org/2000/svg" width="100%" height="100%" viewBox="0 0 300 150"></svg>'
    )
    assert (w, h, title) == (300.0, 150.0, "")

    w_pt, h_pt, _ = svg_mod.parse_svg_info(
        '<svg xmlns="http://www.w3.org/2000/svg" width="72pt" height="1in"></svg>'
    )
    assert w_pt == pytest.approx(96.0)
    assert h_pt == pytest.approx(96.0)


def test_open_extensionless_svg_via_sniffing(tmp_path: Path):
    path = tmp_path / "vector_graphic"
    path.write_text(TEXT_SVG, encoding="utf-8")

    assert docs.supports(path)
    doc = docs.open(path)
    assert isinstance(doc, SVG)
    assert doc.width == 120.0 and doc.height == 80.0


def test_rasterize_missing_resvg_raises_missing_extra(monkeypatch: pytest.MonkeyPatch):
    monkeypatch.setitem(sys.modules, "resvg_py", None)
    with pytest.raises(MissingExtra) as exc_info:
        svg_mod.rasterize(TEXT_SVG)
    assert exc_info.value.extra == "svg"
    assert 'pip install "pixcell[svg]"' in str(exc_info.value)

    doc = SVG(svg=TEXT_SVG, width=120.0, height=80.0)
    with pytest.raises(MissingExtra):
        doc.to_picture()


def test_rasterize_validates_dpi_and_zoom():
    with pytest.raises(ValueError, match="dpi"):
        svg_mod.rasterize(TEXT_SVG, dpi=0)
    with pytest.raises(ValueError, match="zoom"):
        svg_mod.rasterize(TEXT_SVG, zoom=0)


@requires_resvg
def test_svg_rasterize_renders_text_pixels_and_options(tmp_path: Path):
    png = svg_mod.rasterize(TEXT_SVG, background="#ffffff")
    assert png.startswith(b"\x89PNG\r\n\x1a\n")

    image = PILImage.open(io.BytesIO(png))
    assert image.size == (240, 160)  # default zoom=2
    dark, light = _count_dark_and_light_pixels(image)
    assert dark > 100, f"expected dark text pixels to be drawn, got {dark}"
    assert light > 1000

    # Also test via docs.open -> SVG.to_picture() and docs.open(..., rasterize=True)
    path = tmp_path / "glyph.svg"
    path.write_text(TEXT_SVG, encoding="utf-8")

    doc = docs.open(path)
    assert isinstance(doc, SVG)
    with doc.to_picture(zoom=1.0, background="#ffffff") as pic:
        assert isinstance(pic, Picture)
        assert pic.page_count == 1
        assert pic.image.size == (120, 80)
        dark_pic, _ = _count_dark_and_light_pixels(pic.image)
        assert dark_pic > 40

    with docs.open(path, rasterize=True, zoom=1.5, background="#ffffff") as pic2:
        assert isinstance(pic2, Picture)
        assert pic2.image.size == (180, 120)
        dark_pic2, _ = _count_dark_and_light_pixels(pic2.image)
        assert dark_pic2 > 60


@requires_resvg
def test_rasterize_passes_dpi_96_and_font_options_to_resvg(monkeypatch: pytest.MonkeyPatch):
    import resvg_py

    captured: list[dict[str, object]] = []
    real_svg_to_bytes = resvg_py.svg_to_bytes

    def spy(**kwargs):
        captured.append(kwargs)
        return real_svg_to_bytes(**kwargs)

    monkeypatch.setattr(resvg_py, "svg_to_bytes", spy)

    svg_mod.rasterize(TEXT_SVG)
    assert captured[-1]["dpi"] == 96.0
    assert captured[-1]["zoom"] == 2.0
    assert captured[-1]["sans_serif_family"] == "Arial"
    assert "background" not in captured[-1]

    svg_mod.rasterize(TEXT_SVG, zoom=3.0, background="#112233", font_family="DejaVu Sans")
    assert captured[-1]["dpi"] == 96.0
    assert captured[-1]["zoom"] == 3.0
    assert captured[-1]["background"] == "#112233"
    assert captured[-1]["font_family"] == "DejaVu Sans"
    assert captured[-1]["sans_serif_family"] == "DejaVu Sans"

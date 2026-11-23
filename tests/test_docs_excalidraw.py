"""Tests for the Excalidraw JSON-to-SVG renderer in pixcell.docs.excalidraw."""

from __future__ import annotations

import base64
import importlib.util
import io
import json
import math
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any

import pytest
from PIL import Image as PILImage

import pixcell.docs as docs
from pixcell.docs import SVG, DocsError, MissingExtra, Picture, sniff_mime
from pixcell.docs import excalidraw as excalidraw_mod

HAS_RESVG = importlib.util.find_spec("resvg_py") is not None
requires_resvg = pytest.mark.skipif(not HAS_RESVG, reason="resvg-py is not installed")

EXCALIDRAW_MIME = "application/vnd.excalidraw+json"
SVG_NS = "{http://www.w3.org/2000/svg}"


def _make_png_data_url(color: tuple[int, int, int] = (220, 40, 40)) -> str:
    buf = io.BytesIO()
    PILImage.new("RGB", (8, 8), color).save(buf, format="PNG")
    encoded = base64.b64encode(buf.getvalue()).decode("ascii")
    return f"data:image/png;base64,{encoded}"


def _full_scene(image_data_url: str | None = None) -> dict[str, Any]:
    data_url = image_data_url or _make_png_data_url()
    return {
        "type": "excalidraw",
        "version": 2,
        "source": "https://excalidraw.com",
        "appState": {
            "viewBackgroundColor": "#f8f9fa",
            "name": "Architecture Diagram",
        },
        "files": {
            "img-file-1": {
                "id": "img-file-1",
                "mimeType": "image/png",
                "dataURL": data_url,
            },
            "img-unsafe": {
                "id": "img-unsafe",
                "mimeType": "image/png",
                "dataURL": "https://example.com/nope.png",
            },
        },
        "elements": [
            {
                "id": "rect-1",
                "type": "rectangle",
                "x": 20,
                "y": 30,
                "width": 120,
                "height": 60,
                "angle": 0,
                "strokeColor": "#1e1e1e",
                "backgroundColor": "#a5d8ff",
                "fillStyle": "solid",
                "strokeWidth": 2,
                "strokeStyle": "dashed",
                "opacity": 50,
                "roundness": {"type": 3},
                "isDeleted": False,
            },
            {
                "id": "ellipse-1",
                "type": "ellipse",
                "x": 180,
                "y": 30,
                "width": 80,
                "height": 50,
                "angle": math.pi / 4,
                "strokeColor": "#2f9e44",
                "backgroundColor": "#b2f2bb",
                "fillStyle": "hachure",
                "strokeWidth": 3,
                "strokeStyle": "dotted",
                "opacity": 100,
                "isDeleted": False,
            },
            {
                "id": "diamond-sharp",
                "type": "diamond",
                "x": 20,
                "y": 120,
                "width": 80,
                "height": 60,
                "angle": 0,
                "strokeColor": "#e03131",
                "backgroundColor": "#ffc9c9",
                "fillStyle": "cross-hatch",
                "strokeWidth": 2,
                "strokeStyle": "solid",
                "opacity": 100,
                "roundness": None,
                "isDeleted": False,
            },
            {
                "id": "diamond-round",
                "type": "diamond",
                "x": 120,
                "y": 120,
                "width": 60,
                "height": 60,
                "strokeColor": "#6741d9",
                "backgroundColor": "transparent",
                "strokeWidth": 2,
                "roundness": {"type": 2},
                "isDeleted": False,
            },
            {
                "id": "line-1",
                "type": "line",
                "x": 20,
                "y": 210,
                "width": 100,
                "height": 20,
                "points": [[0, 0], [50, 20], [100, 0]],
                "strokeColor": "#1971c2",
                "backgroundColor": "transparent",
                "strokeWidth": 2,
                "strokeStyle": "solid",
                "opacity": 100,
                "roundness": None,
                "isDeleted": False,
            },
            {
                "id": "arrow-1",
                "type": "arrow",
                "x": 140,
                "y": 210,
                "width": 110,
                "height": 30,
                "points": [[0, 0], [55, 30], [110, 0]],
                "strokeColor": "#1e1e1e",
                "strokeWidth": 2,
                "startArrowhead": "dot",
                "endArrowhead": "triangle",
                "roundness": {"type": 2},
                "isDeleted": False,
            },
            {
                "id": "freedraw-1",
                "type": "freedraw",
                "x": 20,
                "y": 260,
                "width": 90,
                "height": 25,
                "points": [[0, 0], [30, 25], [60, 5], [90, 20]],
                "strokeColor": "#d9480f",
                "strokeWidth": 3,
                "opacity": 80,
                "isDeleted": False,
            },
            {
                "id": "text-1",
                "type": "text",
                "x": 140,
                "y": 255,
                "width": 140,
                "height": 50,
                "text": "Line 1 <A & B>\n\"Line 2\" 'quoted'",
                "fontSize": 18,
                "fontFamily": 3,
                "textAlign": "center",
                "verticalAlign": "top",
                "strokeColor": "#000000",
                "opacity": 100,
                "isDeleted": False,
            },
            {
                "id": "image-1",
                "type": "image",
                "x": 290,
                "y": 30,
                "width": 40,
                "height": 40,
                "fileId": "img-file-1",
                "opacity": 100,
                "isDeleted": False,
            },
            {
                "id": "image-unsafe-ignored",
                "type": "image",
                "x": 340,
                "y": 30,
                "width": 20,
                "height": 20,
                "fileId": "img-unsafe",
                "isDeleted": False,
            },
            {
                "id": "deleted-far-away",
                "type": "rectangle",
                "x": 5000,
                "y": 5000,
                "width": 500,
                "height": 500,
                "strokeColor": "#ff00ff",
                "backgroundColor": "#ff00ff",
                "isDeleted": True,
            },
        ],
    }


def _strip_tag(elem: ET.Element) -> str:
    return elem.tag.rsplit("}", 1)[-1]


def test_registry_resolves_excalidraw_by_suffix_mime_and_sniffing(tmp_path: Path):
    assert docs.supports("diagram.excalidraw")
    assert docs.supports("DIAGRAM.EXCALIDRAW")
    assert docs.supports("diagram.excalidraw.json")

    spec = docs.resolve("diagram.excalidraw")
    assert spec is not None
    assert spec.name == "excalidraw"
    assert spec.extra == "svg"

    assert docs.resolve("blob.bin", mime_type=EXCALIDRAW_MIME).name == "excalidraw"

    # Sniffing small extensionless and .json Excalidraw files
    scene_bytes = json.dumps({"type": "excalidraw", "version": 2, "elements": []}).encode("utf-8")
    assert sniff_mime(scene_bytes) == EXCALIDRAW_MIME

    no_ext = tmp_path / "board_without_ext"
    no_ext.write_bytes(scene_bytes)
    assert docs.supports(no_ext)
    assert docs.resolve(no_ext).name == "excalidraw"

    plain_json_ext = tmp_path / "scene.json"
    plain_json_ext.write_bytes(scene_bytes)
    assert docs.resolve(plain_json_ext).name == "excalidraw"

    # Large file (> 4096 bytes) where "type": "excalidraw" appears at the very end
    big_scene = {
        "elements": [
            {"id": f"r{i}", "type": "rectangle", "x": i * 10, "y": 0, "width": 5, "height": 5}
            for i in range(120)
        ],
        "type": "excalidraw",
    }
    big_path = tmp_path / "big_sorted_board"
    big_path.write_text(json.dumps(big_scene), encoding="utf-8")
    assert big_path.stat().st_size > 4096
    assert docs.resolve(big_path).name == "excalidraw"

    # Non-excalidraw JSON must not match
    other_json = b'{"type": "other", "nested": {"type": "excalidraw"}}'
    assert sniff_mime(other_json) is None
    other_path = tmp_path / "other.json"
    other_path.write_bytes(other_json)
    assert not docs.supports(other_path)


def test_importing_pixcell_docs_does_not_import_excalidraw_or_resvg():
    code = (
        "import sys, pixcell.docs as docs\n"
        "assert docs.supports('board.excalidraw')\n"
        "assert 'pixcell.docs.excalidraw' not in sys.modules\n"
        "assert 'resvg_py' not in sys.modules\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_full_scene_svg_elements_attributes_escaping_and_deleted_skipped(tmp_path: Path):
    data_url = _make_png_data_url()
    scene = _full_scene(data_url)
    path = tmp_path / "arch.excalidraw"
    path.write_text(json.dumps(scene), encoding="utf-8")

    doc = docs.open(path)
    assert isinstance(doc, SVG)
    assert doc.source == path
    assert doc.title == "Architecture Diagram"
    assert doc.width is not None and doc.height is not None

    # Must be strictly well-formed XML
    root = ET.fromstring(doc.svg)
    assert _strip_tag(root) == "svg"

    # Background rect matches viewBackgroundColor and full viewBox
    rects = root.findall(f"{SVG_NS}rect")
    assert len(rects) == 2  # background rect + rectangle element
    bg_rect, shape_rect = rects
    assert bg_rect.attrib["fill"] == "#f8f9fa"

    # Rectangle element attributes: roundness, solid fill, dashed stroke, opacity 50 -> 0.5
    assert shape_rect.attrib["x"] == "20"
    assert shape_rect.attrib["y"] == "30"
    assert shape_rect.attrib["width"] == "120"
    assert shape_rect.attrib["height"] == "60"
    assert shape_rect.attrib["fill"] == "#a5d8ff"
    assert shape_rect.attrib["stroke"] == "#1e1e1e"
    assert shape_rect.attrib["stroke-width"] == "2"
    assert "stroke-dasharray" in shape_rect.attrib
    assert float(shape_rect.attrib["opacity"]) == pytest.approx(0.5)
    assert float(shape_rect.attrib["rx"]) > 0

    # Deleted rectangle (#ff00ff at x=5000) is completely absent and excluded from bbox
    assert "#ff00ff" not in doc.svg
    assert doc.width < 1000 and doc.height < 1000

    # Ellipse: dotted stroke, hachure pattern fill, 45-degree rotation around center (220, 55)
    ellipses = root.findall(f"{SVG_NS}ellipse")
    assert len(ellipses) == 1
    ell = ellipses[0]
    assert ell.attrib["cx"] == "220"
    assert ell.attrib["cy"] == "55"
    assert ell.attrib["rx"] == "40"
    assert ell.attrib["ry"] == "25"
    assert ell.attrib["fill"].startswith("url(#excalidraw-pat-")
    assert "stroke-dasharray" in ell.attrib
    assert ell.attrib["transform"] == "rotate(45 220 55)"

    # Patterns defined in <defs> for hachure and cross-hatch
    patterns = root.findall(f"{SVG_NS}defs/{SVG_NS}pattern")
    assert len(patterns) == 2

    # Sharp diamond -> <polygon> with cross-hatch fill; rounded diamond -> <path>
    polygons = root.findall(f"{SVG_NS}polygon")
    assert len(polygons) == 1
    assert polygons[0].attrib["points"] == "60,120 100,150 60,180 20,150"
    assert polygons[0].attrib["fill"].startswith("url(#excalidraw-pat-")

    # Top-level paths: rounded diamond, line, freedraw
    top_paths = root.findall(f"{SVG_NS}path")
    assert len(top_paths) == 3
    assert any(p.attrib.get("stroke") == "#1971c2" for p in top_paths)  # line
    assert any(
        p.attrib.get("stroke") == "#d9480f" and float(p.attrib.get("opacity", "1")) == pytest.approx(0.8)
        for p in top_paths
    )  # freedraw

    # Arrow grouped in <g> with shaft <path>, start dot <circle>, end triangle <polygon>
    groups = root.findall(f"{SVG_NS}g")
    assert len(groups) == 1
    arrow_group = groups[0]
    assert arrow_group.find(f"{SVG_NS}path") is not None
    assert arrow_group.find(f"{SVG_NS}circle") is not None
    assert arrow_group.find(f"{SVG_NS}polygon") is not None

    # Multi-line text with center alignment, monospace font, and XML-escaped text
    texts = root.findall(f"{SVG_NS}text")
    assert len(texts) == 1
    text_elem = texts[0]
    assert text_elem.attrib["font-size"] == "18"
    assert text_elem.attrib["text-anchor"] == "middle"
    assert text_elem.attrib["font-family"] == "monospace"
    assert float(text_elem.attrib["x"]) == pytest.approx(210.0)  # 140 + 140 / 2
    tspans = text_elem.findall(f"{SVG_NS}tspan")
    assert [t.text for t in tspans] == ["Line 1 <A & B>", '"Line 2" \'quoted\'']
    assert "&lt;A &amp; B&gt;" in doc.svg

    # Embedded image from files map (unsafe https image ignored)
    images = root.findall(f"{SVG_NS}image")
    assert len(images) == 1
    img_elem = images[0]
    assert img_elem.attrib["href"] == data_url
    assert img_elem.attrib["x"] == "290"
    assert img_elem.attrib["y"] == "30"
    assert img_elem.attrib["width"] == "40"
    assert img_elem.attrib["height"] == "40"


def test_bbox_padding_and_rotation_expansion():
    scene = {
        "type": "excalidraw",
        "elements": [
            {
                "type": "rectangle",
                "x": -50,
                "y": 10,
                "width": 100,
                "height": 40,
                "strokeWidth": 0,
            }
        ],
    }
    doc = excalidraw_mod.scene_to_svg(scene, padding=15)
    root = ET.fromstring(doc.svg)
    # Without rotation: x in [-50, 50], y in [10, 50]; with padding=15 -> vx=-65, vy=-5, vw=130, vh=70
    assert doc.width == pytest.approx(130.0)
    assert doc.height == pytest.approx(70.0)
    assert root.attrib["viewBox"] == "-65 -5 130 70"

    # Rotate 90 degrees (pi/2): 100x40 centered at (0, 30) becomes 40x100 -> x in [-20, 20], y in [-20, 80]
    rotated_scene = {
        "type": "excalidraw",
        "elements": [
            {
                "type": "rectangle",
                "x": -50,
                "y": 10,
                "width": 100,
                "height": 40,
                "angle": math.pi / 2,
                "strokeWidth": 0,
            }
        ],
    }
    rot_doc = excalidraw_mod.scene_to_svg(rotated_scene, padding=10)
    rot_root = ET.fromstring(rot_doc.svg)
    assert rot_doc.width == pytest.approx(60.0)
    assert rot_doc.height == pytest.approx(120.0)
    rect_elem = rot_root.find(f"{SVG_NS}rect")
    assert rect_elem is not None
    assert rect_elem.attrib["transform"] == "rotate(90 0 30)"


def test_empty_or_all_deleted_and_transparent_background():
    scene = {
        "type": "excalidraw",
        "appState": {"viewBackgroundColor": "transparent"},
        "elements": [
            {"type": "rectangle", "x": 10, "y": 10, "width": 50, "height": 50, "isDeleted": True},
        ],
    }
    doc = excalidraw_mod.scene_to_svg(scene)
    root = ET.fromstring(doc.svg)
    assert root.findall(f"{SVG_NS}rect") == []
    assert doc.width is not None and doc.width > 0
    assert doc.height is not None and doc.height > 0


def test_svg_output_needs_no_resvg_but_rasterize_raises_missing_extra(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    path = tmp_path / "board.excalidraw"
    path.write_text(json.dumps(_full_scene()), encoding="utf-8")

    monkeypatch.setitem(sys.modules, "resvg_py", None)

    # Opening as SVG works with zero optional dependencies
    doc = docs.open(path)
    assert isinstance(doc, SVG)

    # Rasterising fails with MissingExtra("svg")
    with pytest.raises(MissingExtra) as exc1:
        doc.to_picture()
    assert exc1.value.extra == "svg"
    assert 'pip install "pixcell[svg]"' in str(exc1.value)

    with pytest.raises(MissingExtra) as exc2:
        docs.open(path, rasterize=True)
    assert exc2.value.extra == "svg"


def test_invalid_excalidraw_json_raises_docs_error(tmp_path: Path):
    bad = tmp_path / "broken.excalidraw"
    bad.write_text("{not valid json", encoding="utf-8")
    with pytest.raises(DocsError):
        docs.open(bad)

    non_obj = tmp_path / "array.excalidraw"
    non_obj.write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(DocsError):
        docs.open(non_obj)


@requires_resvg
def test_excalidraw_rasterize_produces_non_blank_image(tmp_path: Path):
    path = tmp_path / "scene.excalidraw"
    path.write_text(json.dumps(_full_scene()), encoding="utf-8")

    doc = docs.open(path)
    assert isinstance(doc, SVG)

    with doc.to_picture(zoom=1.0) as pic:
        assert isinstance(pic, Picture)
        assert pic.page_count == 1
        img = pic.image.convert("RGB")
        assert img.width > 100 and img.height > 100
        # Verify the rasterised image is non-blank and contains multiple drawn colors
        # (background #f8f9fa, red embedded image, dark strokes/text, blue rect fill, etc.)
        colors = img.getcolors(maxcolors=100_000)
        assert colors is not None
        assert len(colors) > 20
        has_red = any(r > 180 and g < 80 and b < 80 for _count, (r, g, b) in colors)
        has_dark = any(r < 50 and g < 50 and b < 50 for _count, (r, g, b) in colors)
        assert has_red, "expected embedded red PNG image pixels to be rendered"
        assert has_dark, "expected dark stroke/text pixels to be rendered"

    # Also verify direct rasterize=True through docs.open()
    with docs.open(path, rasterize=True, zoom=1.5) as pic2:
        assert isinstance(pic2, Picture)
        assert pic2.page_count == 1
        assert pic2.image.width > img.width

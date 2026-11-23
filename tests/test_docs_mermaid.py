"""Tests for the Mermaid renderer in pixcell.docs.mermaid."""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest

import pixcell.docs as docs
from pixcell.docs import SVG, DocsError, MissingExtra, Picture
from pixcell.docs import mermaid as mermaid_mod

HAS_QUICKJS = importlib.util.find_spec("quickjs") is not None
HAS_RESVG = importlib.util.find_spec("resvg_py") is not None

requires_quickjs = pytest.mark.skipif(not HAS_QUICKJS, reason="quickjs-ng is not installed")
requires_resvg = pytest.mark.skipif(not HAS_RESVG, reason="resvg-py is not installed")

FLOWCHART = """\
flowchart LR
  A[Start] -->|calls| B[Finish]
"""

SEQUENCE = """\
sequenceDiagram
  Alice->>Bob: Hello Bob
  Bob-->>Alice: Hi Alice
"""


def test_registry_resolves_mermaid_by_suffix_and_mime():
    assert docs.supports("diagram.mmd")
    assert docs.supports("diagram.mermaid")
    assert docs.resolve("DIAGRAM.MMD").name == "mermaid"
    assert docs.resolve("diagram.mermaid").name == "mermaid"
    assert docs.resolve("diagram.mmd").extra == "mermaid"
    assert docs.resolve("blob.bin", mime_type="text/vnd.mermaid").name == "mermaid"
    assert docs.resolve("blob.bin", mime_type="text/vnd.mermaid; charset=utf-8").name == "mermaid"


def test_importing_pixcell_docs_does_not_import_mermaid_or_quickjs():
    code = (
        "import sys, pixcell.docs as docs\n"
        "assert docs.supports('flow.mmd')\n"
        "assert docs.supports('seq.mermaid')\n"
        "assert 'quickjs' not in sys.modules\n"
        "assert 'pixcell.docs.mermaid' not in sys.modules\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_missing_extra_when_quickjs_absent(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    mmd_path = tmp_path / "flow.mmd"
    mmd_path.write_text(FLOWCHART, encoding="utf-8")
    monkeypatch.setitem(sys.modules, "quickjs", None)

    with pytest.raises(MissingExtra) as open_exc:
        docs.open(mmd_path)
    assert open_exc.value.extra == "mermaid"
    assert 'pip install "pixcell[mermaid]"' in str(open_exc.value)

    with pytest.raises(MissingExtra) as direct_exc:
        mermaid_mod.render_mermaid(FLOWCHART)
    assert direct_exc.value.extra == "mermaid"

    with pytest.raises(MissingExtra) as render_exc:
        mermaid_mod.render(mmd_path)
    assert render_exc.value.extra == "mermaid"


@requires_quickjs
def test_missing_extra_when_resvg_absent_on_rasterize(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    mmd_path = tmp_path / "flow.mmd"
    mmd_path.write_text(FLOWCHART, encoding="utf-8")
    monkeypatch.setitem(sys.modules, "resvg_py", None)

    # SVG output still works without resvg-py
    doc = docs.open(mmd_path)
    assert isinstance(doc, SVG)

    with pytest.raises(MissingExtra) as pic_exc:
        doc.to_picture()
    assert pic_exc.value.extra == "svg"

    with pytest.raises(MissingExtra) as raster_exc:
        docs.open(mmd_path, rasterize=True)
    assert raster_exc.value.extra == "svg"


@requires_quickjs
def test_flowchart_renders_to_svg_with_labels_and_dimensions(tmp_path: Path):
    mmd_path = tmp_path / "flow.mmd"
    mmd_path.write_text(FLOWCHART, encoding="utf-8")

    doc = docs.open(mmd_path)
    assert isinstance(doc, SVG)
    assert doc.source == mmd_path
    assert doc.width is not None and doc.width > 0
    assert doc.height is not None and doc.height > 0
    assert "<svg" in doc.svg
    assert "Start" in doc.svg
    assert "Finish" in doc.svg
    assert "calls" in doc.svg
    assert "var(" not in doc.svg
    assert "--_" not in doc.svg

    # Also test .mermaid suffix and explicit text/vnd.mermaid MIME type
    mermaid_path = tmp_path / "flow.mermaid"
    mermaid_path.write_text(FLOWCHART, encoding="utf-8")
    assert isinstance(docs.open(mermaid_path), SVG)

    blob_path = tmp_path / "diagram_blob"
    blob_path.write_text(FLOWCHART, encoding="utf-8")
    mime_doc = docs.open(blob_path, mime_type="text/vnd.mermaid")
    assert isinstance(mime_doc, SVG)
    assert "Start" in mime_doc.svg


@requires_quickjs
def test_sequence_diagram_renders_to_svg_with_labels(tmp_path: Path):
    seq_path = tmp_path / "chat.mermaid"
    seq_path.write_text(SEQUENCE, encoding="utf-8")

    doc = docs.open(seq_path)
    assert isinstance(doc, SVG)
    assert doc.width is not None and doc.width > 0
    assert doc.height is not None and doc.height > 0
    for expected in ("Alice", "Bob", "Hello Bob", "Hi Alice"):
        assert expected in doc.svg

    direct = mermaid_mod.render_mermaid(SEQUENCE, theme="dark")
    assert isinstance(direct, SVG)
    assert mermaid_mod.DARK.fg in direct.svg
    assert "Hello Bob" in direct.svg


@requires_quickjs
def test_theme_variables_resolve_and_diagram_init_overrides():
    # Resolved DARK theme strips CSS variables and applies palette colours.
    dark_doc = mermaid_mod.render_mermaid("flowchart LR\n  A[one<br/>two] -->|calls| B", mermaid_mod.DARK)
    assert "var(" not in dark_doc.svg
    assert "--_" not in dark_doc.svg
    assert mermaid_mod.DARK.surface in dark_doc.svg

    # Raw SVG option preserves Pele's CSS variables when requested.
    raw_doc = mermaid_mod.render_mermaid(FLOWCHART, resolve_vars=False)
    assert "var(--pele-" in raw_doc.svg

    # Custom theme with series colours applies to charts, while %%{init}%% wins.
    custom = mermaid_mod.MermaidTheme(
        bg="#272822",
        fg="#f8f8f2",
        muted="#a0a09a",
        line="#c8c8c0",
        surface="#383930",
        surface_alt="#49483e",
        border="#75715e",
        accent="#ae81ff",
        series=("#ae81ff", "#66d9ef"),
        critical="#f92672",
    )
    pie = 'pie\n  "A" : 1\n  "B" : 2'
    pie_svg = mermaid_mod.render_mermaid(pie, theme=custom).svg
    assert "#ae81ff" in pie_svg and "#66d9ef" in pie_svg

    own = "%%{init: {'themeVariables': {'pie1': '#123456'}}}%%\n" + pie
    own_svg = mermaid_mod.render_mermaid(own, theme=custom).svg
    assert "#123456" in own_svg.lower()

    with pytest.raises(ValueError, match="Unknown Mermaid theme"):
        mermaid_mod.render_mermaid(FLOWCHART, theme="neon-rainbow")


@requires_quickjs
def test_max_width_reflows_wide_diagram():
    gantt = (
        "gantt\n"
        "  title Plan\n"
        "  dateFormat YYYY-MM-DD\n"
        "  section Work\n"
        "  Build :a1, 2026-11-01, 5d\n"
        "  Ship :after a1, 3d\n"
    )
    wide = mermaid_mod.render_mermaid(gantt)
    narrow = mermaid_mod.render_mermaid(gantt, max_width=400)
    assert narrow.width is not None and wide.width is not None
    assert narrow.width <= 400 < wide.width

    with pytest.raises(ValueError, match="max_width"):
        mermaid_mod.render_mermaid(FLOWCHART, max_width=0)


@requires_quickjs
def test_bad_syntax_and_empty_source_raise_clear_error(tmp_path: Path):
    with pytest.raises(mermaid_mod.MermaidError) as syntax_exc:
        mermaid_mod.render_mermaid("flowchart LR\n  A -->")
    assert isinstance(syntax_exc.value, DocsError)
    msg = str(syntax_exc.value)
    assert "Parse error" in msg or "line 2" in msg
    assert "    at " not in msg

    with pytest.raises(mermaid_mod.MermaidError):
        mermaid_mod.render_mermaid("not a valid mermaid diagram")

    with pytest.raises(mermaid_mod.MermaidError, match="empty"):
        mermaid_mod.render_mermaid("   \n\t  ")

    bad_file = tmp_path / "broken.mmd"
    bad_file.write_text("flowchart LR\n  A -->", encoding="utf-8")
    with pytest.raises(mermaid_mod.MermaidError):
        docs.open(bad_file)


@requires_quickjs
@requires_resvg
def test_mermaid_rasterizes_to_picture_and_draws_labels(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import resvg_py

    captured: list[dict[str, object]] = []
    real_svg_to_bytes = resvg_py.svg_to_bytes

    def spy(**kwargs):
        captured.append(kwargs)
        return real_svg_to_bytes(**kwargs)

    monkeypatch.setattr(resvg_py, "svg_to_bytes", spy)

    # Flowchart -> SVG -> Picture with theme background and visible label pixels
    flow_doc = mermaid_mod.render_mermaid("flowchart LR\n  A[MMMMMMMM]", theme=mermaid_mod.DARK)
    with flow_doc.to_picture(zoom=1.0, background=mermaid_mod.DARK.bg) as pic:
        assert isinstance(pic, Picture)
        assert pic.page_count == 1
        img = pic.image.convert("RGB")
        assert img.size[0] > 0 and img.size[1] > 0
        assert img.getpixel((1, 1)) == (0x1E, 0x1E, 0x1E)
        mid_row = [img.getpixel((x, img.size[1] // 2)) for x in range(img.size[0])]
        text_pixels = set(mid_row) - {(0x1E, 0x1E, 0x1E), (0x2A, 0x2D, 0x33), (0x6B, 0x72, 0x80)}
        assert text_pixels, "expected label glyph pixels to be rendered"

    assert captured[-1]["dpi"] == 96.0
    assert captured[-1]["sans_serif_family"] == "Arial"

    # Sequence diagram opened directly with rasterize=True via docs.open
    seq_path = tmp_path / "seq.mmd"
    seq_path.write_text(SEQUENCE, encoding="utf-8")
    with docs.open(seq_path, theme="dark", rasterize=True, zoom=1.0) as seq_pic:
        assert isinstance(seq_pic, Picture)
        assert seq_pic.page_count == 1
        seq_img = seq_pic.image.convert("RGB")
        assert seq_img.size[0] > 0 and seq_img.size[1] > 0
        assert seq_img.getpixel((1, 1)) == (0x1E, 0x1E, 0x1E)


@requires_quickjs
def test_concurrent_rendering_is_thread_safe():
    diagrams = [
        f"flowchart LR\n  A{i}[Node {i}] --> B{i}[End {i}]"
        for i in range(8)
    ]
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(mermaid_mod.render_mermaid, diagrams))
    assert len(results) == len(diagrams)
    for i, doc in enumerate(results):
        assert isinstance(doc, SVG)
        assert f"Node {i}" in doc.svg
        assert f"End {i}" in doc.svg

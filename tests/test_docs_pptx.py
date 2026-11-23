"""Tests for the PPTX renderer in pixcell.docs.pptx."""

from __future__ import annotations

import importlib.util
import io
import subprocess
import sys
import zipfile
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from PIL import Image as PILImage

import pixcell.docs as docs
from pixcell.docs import SVG, DocsError, DocumentTooLarge, MissingExtra, Picture
from pixcell.docs import pptx as pptx_mod

HAS_PPTX_DEPS = (
    importlib.util.find_spec("quickjs") is not None
    and importlib.util.find_spec("resvg_py") is not None
)
requires_pptx = pytest.mark.skipif(
    not HAS_PPTX_DEPS, reason="quickjs-ng and resvg-py are not installed"
)

PPTX_MIME = "application/vnd.openxmlformats-officedocument.presentationml.presentation"

_THEME_XML = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Office Theme">
  <a:themeElements>
    <a:clrScheme name="Office">
      <a:dk1><a:srgbClr val="000000"/></a:dk1>
      <a:lt1><a:srgbClr val="FFFFFF"/></a:lt1>
      <a:dk2><a:srgbClr val="1F497D"/></a:dk2>
      <a:lt2><a:srgbClr val="EEECE1"/></a:lt2>
      <a:accent1><a:srgbClr val="4F81BD"/></a:accent1>
      <a:accent2><a:srgbClr val="C0504D"/></a:accent2>
      <a:accent3><a:srgbClr val="9BBB59"/></a:accent3>
      <a:accent4><a:srgbClr val="8064A2"/></a:accent4>
      <a:accent5><a:srgbClr val="4BACC6"/></a:accent5>
      <a:accent6><a:srgbClr val="F79646"/></a:accent6>
      <a:hlink><a:srgbClr val="0000FF"/></a:hlink>
      <a:folHlink><a:srgbClr val="800080"/></a:folHlink>
    </a:clrScheme>
    <a:fontScheme name="Office">
      <a:majorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:majorFont>
      <a:minorFont><a:latin typeface="Calibri"/><a:ea typeface=""/><a:cs typeface=""/></a:minorFont>
    </a:fontScheme>
    <a:fmtScheme name="Office">
      <a:fillStyleLst>
        <a:solidFill><a:schemeClr val="phClr"/></a:solidFill>
        <a:solidFill><a:schemeClr val="phClr"/></a:solidFill>
        <a:solidFill><a:schemeClr val="phClr"/></a:solidFill>
      </a:fillStyleLst>
      <a:lnStyleLst>
        <a:ln w="9525"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>
        <a:ln w="25400"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>
        <a:ln w="38100"><a:solidFill><a:schemeClr val="phClr"/></a:solidFill></a:ln>
      </a:lnStyleLst>
      <a:effectStyleLst>
        <a:effectStyle><a:effectLst/></a:effectStyle>
        <a:effectStyle><a:effectLst/></a:effectStyle>
        <a:effectStyle><a:effectLst/></a:effectStyle>
      </a:effectStyleLst>
      <a:bgFillStyleLst>
        <a:solidFill><a:schemeClr val="phClr"/></a:solidFill>
        <a:solidFill><a:schemeClr val="phClr"/></a:solidFill>
        <a:solidFill><a:schemeClr val="phClr"/></a:solidFill>
      </a:bgFillStyleLst>
    </a:fmtScheme>
  </a:themeElements>
</a:theme>
"""

_SLIDE_MASTER_XML = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldMaster
    xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld>
    <p:bg><p:bgPr><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill><a:effectLst/></p:bgPr></p:bg>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr/>
    </p:spTree>
  </p:cSld>
  <p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2"
            accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6"
            hlink="hlink" folHlink="folHlink"/>
  <p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>
</p:sldMaster>
"""

_SLIDE_MASTER_RELS = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/>
</Relationships>
"""

_SLIDE_LAYOUT_XML = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldLayout
    xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
    type="blank">
  <p:cSld name="Blank">
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr/>
    </p:spTree>
  </p:cSld>
</p:sldLayout>
"""

_SLIDE_LAYOUT_RELS = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/>
</Relationships>
"""


def _make_sample_png() -> bytes:
    buf = io.BytesIO()
    PILImage.new("RGB", (120, 80), (20, 180, 60)).save(buf, format="PNG")
    return buf.getvalue()


def _text_slide_xml(heading: str, body: str) -> str:
    return f"""\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld
    xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr/>
      <p:sp>
        <p:nvSpPr><p:cNvPr id="2" name="Title"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>
        <p:spPr>
          <a:xfrm><a:off x="600000" y="500000"/><a:ext cx="10000000" cy="1200000"/></a:xfrm>
          <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
        </p:spPr>
        <p:txBody>
          <a:bodyPr/>
          <a:lstStyle/>
          <a:p>
            <a:r><a:rPr lang="en-US" sz="4400" b="1"><a:solidFill><a:srgbClr val="111111"/></a:solidFill></a:rPr><a:t>{heading}</a:t></a:r>
          </a:p>
          <a:p>
            <a:r><a:rPr lang="en-US" sz="2400"><a:solidFill><a:srgbClr val="333333"/></a:solidFill></a:rPr><a:t>{body}</a:t></a:r>
          </a:p>
        </p:txBody>
      </p:sp>
    </p:spTree>
  </p:cSld>
</p:sld>
"""


def _shape_slide_xml(label: str) -> str:
    return f"""\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld
    xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr/>
      <p:sp>
        <p:nvSpPr><p:cNvPr id="3" name="BlueBox"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
        <p:spPr>
          <a:xfrm><a:off x="1500000" y="1200000"/><a:ext cx="9000000" cy="4000000"/></a:xfrm>
          <a:prstGeom prst="roundRect"><a:avLst/></a:prstGeom>
          <a:solidFill><a:srgbClr val="1E64DC"/></a:solidFill>
          <a:ln w="25400"><a:solidFill><a:srgbClr val="0A2A66"/></a:solidFill></a:ln>
        </p:spPr>
        <p:txBody>
          <a:bodyPr anchor="ctr"/>
          <a:lstStyle/>
          <a:p>
            <a:r><a:rPr lang="en-US" sz="3200" b="1"><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill></a:rPr><a:t>{label}</a:t></a:r>
          </a:p>
        </p:txBody>
      </p:sp>
    </p:spTree>
  </p:cSld>
</p:sld>
"""


def _image_slide_xml(caption: str) -> str:
    return f"""\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld
    xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr/>
      <p:sp>
        <p:nvSpPr><p:cNvPr id="4" name="Caption"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>
        <p:spPr>
          <a:xfrm><a:off x="600000" y="300000"/><a:ext cx="8000000" cy="800000"/></a:xfrm>
          <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
        </p:spPr>
        <p:txBody>
          <a:bodyPr/>
          <a:lstStyle/>
          <a:p>
            <a:r><a:rPr lang="en-US" sz="3200" b="1"><a:solidFill><a:srgbClr val="111111"/></a:solidFill></a:rPr><a:t>{caption}</a:t></a:r>
          </a:p>
        </p:txBody>
      </p:sp>
      <p:pic>
        <p:nvPicPr>
          <p:cNvPr id="5" name="GreenSwatch"/>
          <p:cNvPicPr/>
          <p:nvPr/>
        </p:nvPicPr>
        <p:blipFill>
          <a:blip r:embed="rId2"/>
          <a:stretch><a:fillRect/></a:stretch>
        </p:blipFill>
        <p:spPr>
          <a:xfrm><a:off x="2000000" y="1500000"/><a:ext cx="8000000" cy="4500000"/></a:xfrm>
          <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
        </p:spPr>
      </p:pic>
    </p:spTree>
  </p:cSld>
</p:sld>
"""


def _build_pptx(
    path: Path,
    *,
    title: str = "Quarterly Deck",
    extra_padding: int = 0,
) -> Path:
    """Build a valid 3-slide .pptx (text, shape, embedded PNG image) using stdlib zipfile."""
    slide_xmls = [
        _text_slide_xml("Quarterly Review", "Key milestones for Q4 engineering"),
        _shape_slide_xml("Rounded Shape Slide"),
        _image_slide_xml("Embedded Image Slide"),
    ]
    png_bytes = _make_sample_png()

    ct_overrides = [
        '<Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>',
        '<Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>',
        '<Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>',
        '<Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>',
        '<Override PartName="/docProps/core.xml" ContentType="application/vnd.openxmlformats-package.core-properties+xml"/>',
    ]
    for i in range(1, len(slide_xmls) + 1):
        ct_overrides.append(
            f'<Override PartName="/ppt/slides/slide{i}.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>'
        )
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        '  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>\n'
        '  <Default Extension="xml" ContentType="application/xml"/>\n'
        '  <Default Extension="png" ContentType="image/png"/>\n'
        + "\n".join(f"  {o}" for o in ct_overrides)
        + "\n</Types>\n"
    )

    root_rels = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/package/2006/relationships/metadata/core-properties" Target="docProps/core.xml"/>
</Relationships>
"""

    sld_ids = "\n".join(
        f'    <p:sldId id="{256 + i}" r:id="rId{i + 2}"/>' for i in range(len(slide_xmls))
    )
    presentation_xml = f"""\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation
    xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>
  <p:sldIdLst>
{sld_ids}
  </p:sldIdLst>
  <p:sldSz cx="12192000" cy="6858000"/>
  <p:notesSz cx="6858000" cy="9144000"/>
</p:presentation>
"""

    pres_rels_items = [
        '  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>'
    ]
    for i in range(len(slide_xmls)):
        pres_rels_items.append(
            f'  <Relationship Id="rId{i + 2}" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide{i + 1}.xml"/>'
        )
    pres_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        + "\n".join(pres_rels_items)
        + "\n</Relationships>\n"
    )

    core_xml = f"""\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<cp:coreProperties
    xmlns:cp="http://schemas.openxmlformats.org/package/2006/metadata/core-properties"
    xmlns:dc="http://purl.org/dc/elements/1.1/">
  <dc:title>{title}</dc:title>
</cp:coreProperties>
"""

    slide_rels_plain = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
</Relationships>
"""

    slide_rels_with_img = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/image1.png"/>
</Relationships>
"""

    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("docProps/core.xml", core_xml)
        zf.writestr("ppt/presentation.xml", presentation_xml)
        zf.writestr("ppt/_rels/presentation.xml.rels", pres_rels)
        zf.writestr("ppt/slideMasters/slideMaster1.xml", _SLIDE_MASTER_XML)
        zf.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels", _SLIDE_MASTER_RELS)
        zf.writestr("ppt/slideLayouts/slideLayout1.xml", _SLIDE_LAYOUT_XML)
        zf.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels", _SLIDE_LAYOUT_RELS)
        zf.writestr("ppt/theme/theme1.xml", _THEME_XML)
        zf.writestr("ppt/media/image1.png", png_bytes)
        for i, xml in enumerate(slide_xmls, 1):
            zf.writestr(f"ppt/slides/slide{i}.xml", xml)
            zf.writestr(
                f"ppt/slides/_rels/slide{i}.xml.rels",
                slide_rels_with_img if i == 3 else slide_rels_plain,
            )
        if extra_padding > 0:
            zf.writestr("ppt/padding.txt", "A" * extra_padding)

    return path


def _count_non_white_pixels(image: PILImage.Image) -> int:
    raw = image.convert("RGB").tobytes()
    count = 0
    for i in range(0, len(raw), 3):
        if raw[i] < 245 or raw[i + 1] < 245 or raw[i + 2] < 245:
            count += 1
    return count


def test_importing_pixcell_docs_does_not_import_pptx_backends():
    code = (
        "import sys, pixcell.docs as docs\n"
        "assert docs.supports('slides.pptx')\n"
        "assert 'quickjs' not in sys.modules\n"
        "assert 'resvg_py' not in sys.modules\n"
        "assert 'pixcell.docs.pptx' not in sys.modules\n"
    )
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True, text=True, check=False)
    assert proc.returncode == 0, proc.stderr or proc.stdout


def test_registry_resolves_pptx_by_suffix_mime_and_sniffing(tmp_path: Path):
    assert docs.supports("deck.pptx")
    assert docs.resolve("DECK.PPTX").name == "pptx"
    assert docs.resolve("deck.pptx").extra == "pptx"
    assert docs.resolve("blob.bin", mime_type=PPTX_MIME).name == "pptx"

    no_ext = _build_pptx(tmp_path / "downloaded_presentation")
    assert docs.supports(no_ext)
    assert docs.resolve(no_ext).name == "pptx"


def test_missing_extra_when_quickjs_or_resvg_absent(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    pptx_path = _build_pptx(tmp_path / "sample.pptx")

    # Missing quickjs
    monkeypatch.setitem(sys.modules, "quickjs", None)
    with pytest.raises(MissingExtra) as exc_info:
        docs.open(pptx_path)
    assert exc_info.value.extra == "pptx"
    assert 'pip install "pixcell[pptx]"' in str(exc_info.value)

    with pytest.raises(MissingExtra) as direct_exc:
        pptx_mod.render(pptx_path)
    assert direct_exc.value.extra == "pptx"

    # Restore quickjs, remove resvg_py
    monkeypatch.undo()
    monkeypatch.setitem(sys.modules, "resvg_py", None)
    with pytest.raises(MissingExtra) as resvg_exc:
        docs.open(pptx_path)
    assert resvg_exc.value.extra == "pptx"
    assert 'pip install "pixcell[pptx]"' in str(resvg_exc.value)


def test_render_validates_arguments(tmp_path: Path):
    pptx_path = _build_pptx(tmp_path / "sample.pptx")

    with pytest.raises(ValueError, match="zoom"):
        pptx_mod.render(pptx_path, zoom=0)
    with pytest.raises(ValueError, match="dpi"):
        pptx_mod.render(pptx_path, dpi=0)
    with pytest.raises(ValueError, match="max_bytes"):
        pptx_mod.render(pptx_path, max_bytes=-1)
    with pytest.raises(ValueError, match="max_pages"):
        pptx_mod.render(pptx_path, max_pages=0)


@requires_pptx
def test_pptx_slides_svg_and_non_blank_rasters(tmp_path: Path):
    pptx_path = _build_pptx(tmp_path / "deck.pptx")

    with docs.open(pptx_path, zoom=1.0) as doc:
        assert isinstance(doc, Picture)
        assert doc.page_count == 3
        assert doc.total_pages == 3
        assert doc.truncated is False
        assert doc.title == "Quarterly Deck"
        assert doc.source == pptx_path
        assert isinstance(doc.pages, pptx_mod.PptxPages)
        assert doc.pages.slide_errors == {}

        # Per-slide SVG exposure
        assert len(doc.pages.svgs) == 3
        svg0 = doc.pages.slide_svg(0)
        svg1 = doc.pages.slide_svg(1)
        svg2 = doc.pages.slide_svg(-1)
        assert "<svg" in svg0
        assert "Quarterly Review" in svg0
        assert "Key milestones" in svg0
        assert "Rounded Shape Slide" in svg1
        assert "Embedded Image Slide" in svg2
        assert "data:image/png;base64," in svg2

        slide_doc = doc.pages.slide_document(0)
        assert isinstance(slide_doc, SVG)
        assert slide_doc.svg == svg0
        assert slide_doc.width is not None and slide_doc.width > 0
        assert slide_doc.height is not None and slide_doc.height > 0

        # Slide 1 (text): non-blank raster (verifies dpi=96.0 didn't drop text)
        img0 = doc.pages[0]
        assert img0.width > 500 and img0.height > 300
        assert _count_non_white_pixels(img0) > 200

        # Slide 2 (blue rounded shape): centre pixel is strongly blue
        img1 = doc.pages[1]
        r1, g1, b1 = img1.convert("RGB").getpixel((img1.width // 2, img1.height // 2))
        assert b1 > 180 and r1 < 100

        # Slide 3 (embedded green PNG): centre-lower region is strongly green
        img2 = doc.pages[2]
        r2, g2, b2 = img2.convert("RGB").getpixel((img2.width // 2, int(img2.height * 0.6)))
        assert g2 > 140 and r2 < 80 and b2 < 100

    # Extensionless PPTX opened via OOXML magic-byte sniffing
    sniffed = _build_pptx(tmp_path / "unnamed_deck")
    with docs.open(sniffed, zoom=1.0) as sniffed_doc:
        assert isinstance(sniffed_doc, Picture)
        assert sniffed_doc.page_count == 3


@requires_pptx
def test_pptx_lazy_rasterization_and_resvg_options(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    import resvg_py

    pptx_path = _build_pptx(tmp_path / "lazy.pptx")
    captured: list[dict[str, object]] = []
    real_svg_to_bytes = resvg_py.svg_to_bytes

    def spy_svg_to_bytes(**kwargs):
        captured.append(kwargs)
        return real_svg_to_bytes(**kwargs)

    monkeypatch.setattr(resvg_py, "svg_to_bytes", spy_svg_to_bytes)

    with docs.open(pptx_path, zoom=1.0) as doc:
        assert isinstance(doc, Picture)
        assert doc.page_count == 3
        assert captured == []  # zero rasterisation calls on open()

        # Accessing slide 2 rasterises only slide 2
        _ = doc.pages[2]
        assert len(captured) == 1
        assert captured[0]["dpi"] == 96.0
        assert captured[0]["font_family"] == "Carlito"
        assert captured[0]["sans_serif_family"] == "Arial"

        # Repeated access hits cache
        _ = doc.pages[2]
        assert len(captured) == 1

        # Accessing slide 0 triggers one more rasterisation
        _ = doc.pages[0]
        assert len(captured) == 2


@requires_pptx
def test_pptx_max_pages_and_max_bytes(tmp_path: Path):
    pptx_path = _build_pptx(tmp_path / "capped.pptx")

    with docs.open(pptx_path, max_pages=2, zoom=1.0) as doc:
        assert isinstance(doc, Picture)
        assert doc.page_count == 2
        assert doc.total_pages == 3
        assert doc.truncated is True
        assert isinstance(doc.pages, pptx_mod.PptxPages)
        assert len(doc.pages.svgs) == 2
        assert "Quarterly Review" in doc.pages.slide_svg(0)
        assert "Rounded Shape Slide" in doc.pages.slide_svg(1)
        with pytest.raises(IndexError):
            _ = doc.pages[2]
        with pytest.raises(IndexError):
            doc.pages.slide_svg(2)

    # Uncompressed zip-bomb guard rail via max_bytes
    bomb = _build_pptx(tmp_path / "bomb.pptx", extra_padding=200_000)
    assert bomb.stat().st_size < 15_000
    with pytest.raises(DocumentTooLarge):
        docs.open(bomb, max_bytes=50_000)

    # Invalid archive raises DocsError
    bad = tmp_path / "corrupt.pptx"
    bad.write_bytes(b"not a zip")
    with pytest.raises(DocsError):
        docs.open(bad)


@requires_pptx
def test_pptx_per_slide_failure_produces_placeholder_without_failing_deck(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    pptx_path = _build_pptx(tmp_path / "partial.pptx")
    real_convert = pptx_mod._convert_in_js

    def flaky_convert(pptx_b64: str, options: dict):
        slides = options.get("slides") or []
        if 2 in slides:
            raise pptx_mod._PptxConversionError(
                "ReferenceError: 'document' is not defined (EMF document.createElementNS)"
            )
        return real_convert(pptx_b64, options)

    monkeypatch.setattr(pptx_mod, "_convert_in_js", flaky_convert)

    with docs.open(pptx_path, zoom=1.0) as doc:
        assert isinstance(doc, Picture)
        assert doc.page_count == 3
        assert isinstance(doc.pages, pptx_mod.PptxPages)

        # Slide 1 & 3 rendered normally; slide 2 (index 1) surfaced a placeholder SVG + recorded error
        assert "Quarterly Review" in doc.pages.slide_svg(0)
        assert "Slide 2 could not be rendered" in doc.pages.slide_svg(1)
        assert "document" in doc.pages.slide_svg(1)
        assert "Embedded Image Slide" in doc.pages.slide_svg(2)
        assert list(doc.pages.slide_errors) == [1]
        assert "document" in doc.pages.slide_errors[1]

        # Placeholder slide still rasterises to a valid non-blank image
        placeholder_img = doc.pages[1]
        assert placeholder_img.width > 300 and placeholder_img.height > 150
        assert _count_non_white_pixels(placeholder_img) > 200


@requires_pptx
def test_pptx_thread_safety(tmp_path: Path):
    pptx_path = _build_pptx(tmp_path / "threaded.pptx")

    def open_and_check(_: int) -> tuple[int, str]:
        with docs.open(pptx_path, zoom=0.5) as doc:
            assert isinstance(doc, Picture)
            assert isinstance(doc.pages, pptx_mod.PptxPages)
            img = doc.pages[0]
            assert img.width > 100
            return doc.page_count, doc.pages.slide_svg(0)

    with ThreadPoolExecutor(max_workers=3) as pool:
        results = list(pool.map(open_and_check, range(6)))

    for count, svg_text in results:
        assert count == 3
        assert "Quarterly Review" in svg_text


def _build_custom_pptx(
    path: Path,
    *,
    slide_xml: str,
    slide_rels: str | None = None,
    master_xml: str | None = None,
    extra_parts: dict[str, bytes | str] | None = None,
    extra_ct_defaults: list[tuple[str, str]] | None = None,
    extra_ct_overrides: list[tuple[str, str]] | None = None,
    extra_pres_rels: list[tuple[str, str, str]] | None = None,
) -> Path:
    """Build a single-slide .pptx with optional extra OOXML parts for fidelity tests."""
    ct_defaults = [
        ("rels", "application/vnd.openxmlformats-package.relationships+xml"),
        ("xml", "application/xml"),
        ("png", "image/png"),
        *(extra_ct_defaults or []),
    ]
    ct_overrides = [
        (
            "/ppt/presentation.xml",
            "application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml",
        ),
        (
            "/ppt/slideMasters/slideMaster1.xml",
            "application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml",
        ),
        (
            "/ppt/slideLayouts/slideLayout1.xml",
            "application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml",
        ),
        ("/ppt/theme/theme1.xml", "application/vnd.openxmlformats-officedocument.theme+xml"),
        (
            "/ppt/slides/slide1.xml",
            "application/vnd.openxmlformats-officedocument.presentationml.slide+xml",
        ),
        *(extra_ct_overrides or []),
    ]
    content_types = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">\n'
        + "\n".join(
            f'  <Default Extension="{ext}" ContentType="{ct}"/>' for ext, ct in ct_defaults
        )
        + "\n"
        + "\n".join(
            f'  <Override PartName="{part}" ContentType="{ct}"/>' for part, ct in ct_overrides
        )
        + "\n</Types>\n"
    )
    root_rels = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
</Relationships>
"""
    presentation_xml = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation
    xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>
  <p:sldIdLst><p:sldId id="256" r:id="rId2"/></p:sldIdLst>
  <p:sldSz cx="12192000" cy="6858000"/>
  <p:notesSz cx="6858000" cy="9144000"/>
</p:presentation>
"""
    pres_rels_entries = [
        (
            "rId1",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster",
            "slideMasters/slideMaster1.xml",
        ),
        (
            "rId2",
            "http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide",
            "slides/slide1.xml",
        ),
        *(extra_pres_rels or []),
    ]
    pres_rels = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">\n'
        + "\n".join(
            f'  <Relationship Id="{rid}" Type="{rtype}" Target="{target}"/>'
            for rid, rtype, target in pres_rels_entries
        )
        + "\n</Relationships>\n"
    )
    default_slide_rels = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
</Relationships>
"""
    master_with_txstyles = master_xml or """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldMaster
    xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld>
    <p:bg><p:bgPr><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill><a:effectLst/></p:bgPr></p:bg>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr/>
    </p:spTree>
  </p:cSld>
  <p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2"
            accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6"
            hlink="hlink" folHlink="folHlink"/>
  <p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>
  <p:txStyles>
    <p:otherStyle>
      <a:lvl1pPr algn="l">
        <a:defRPr sz="1800"><a:solidFill><a:schemeClr val="tx1"/></a:solidFill><a:latin typeface="+mn-lt"/></a:defRPr>
      </a:lvl1pPr>
    </p:otherStyle>
  </p:txStyles>
</p:sldMaster>
"""
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("ppt/presentation.xml", presentation_xml)
        zf.writestr("ppt/_rels/presentation.xml.rels", pres_rels)
        zf.writestr("ppt/slideMasters/slideMaster1.xml", master_with_txstyles)
        zf.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels", _SLIDE_MASTER_RELS)
        zf.writestr("ppt/slideLayouts/slideLayout1.xml", _SLIDE_LAYOUT_XML)
        zf.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels", _SLIDE_LAYOUT_RELS)
        zf.writestr("ppt/theme/theme1.xml", _THEME_XML)
        zf.writestr("ppt/slides/slide1.xml", slide_xml)
        zf.writestr("ppt/slides/_rels/slide1.xml.rels", slide_rels or default_slide_rels)
        for part_name, payload in (extra_parts or {}).items():
            zf.writestr(part_name, payload)
    return path


@requires_pptx
def test_pptx_autoshape_fontref_scheme_color_and_tints(tmp_path: Path):
    import re

    slide_xml = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld
    xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr/>
      <p:sp>
        <p:nvSpPr><p:cNvPr id="2" name="DarkBox"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
        <p:spPr>
          <a:xfrm><a:off x="800000" y="800000"/><a:ext cx="4500000" cy="2200000"/></a:xfrm>
          <a:prstGeom prst="roundRect"><a:avLst/></a:prstGeom>
          <a:solidFill><a:schemeClr val="dk2"/></a:solidFill>
        </p:spPr>
        <p:style>
          <a:fontRef idx="minor"><a:schemeClr val="lt1"/></a:fontRef>
        </p:style>
        <p:txBody>
          <a:bodyPr anchor="ctr"/><a:lstStyle/>
          <a:p><a:pPr marL="342900" indent="-285750"><a:buChar char="•"/></a:pPr><a:r><a:t>WhiteOnNavy</a:t></a:r></a:p>
        </p:txBody>
      </p:sp>
      <p:sp>
        <p:nvSpPr><p:cNvPr id="3" name="TintedBox"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
        <p:spPr>
          <a:xfrm><a:off x="6000000" y="800000"/><a:ext cx="4500000" cy="2200000"/></a:xfrm>
          <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
          <a:solidFill><a:srgbClr val="1B263B"/></a:solidFill>
        </p:spPr>
        <p:style>
          <a:fontRef idx="major">
            <a:schemeClr val="accent1"><a:lumMod val="40000"/><a:lumOff val="60000"/></a:schemeClr>
          </a:fontRef>
        </p:style>
        <p:txBody>
          <a:bodyPr anchor="ctr"/><a:lstStyle/>
          <a:p><a:r><a:t>TintedLabel</a:t></a:r></a:p>
        </p:txBody>
      </p:sp>
    </p:spTree>
  </p:cSld>
</p:sld>
"""
    deck = _build_custom_pptx(tmp_path / "fontref.pptx", slide_xml=slide_xml)
    with docs.open(deck, zoom=1.0) as doc:
        assert isinstance(doc.pages, pptx_mod.PptxPages)
        svg = doc.pages.slide_svg(0)
        white_match = re.search(r'<tspan[^>]*fill="([^"]+)"[^>]*>WhiteOnNavy</tspan>', svg)
        assert white_match is not None
        assert white_match.group(1).lower() == "#ffffff"

        bullet_match = re.search(r'<tspan[^>]*fill="([^"]+)"[^>]*>•</tspan>', svg)
        assert bullet_match is not None
        assert bullet_match.group(1).lower() == "#ffffff"

        tint_match = re.search(r'<tspan[^>]*fill="([^"]+)"[^>]*>TintedLabel</tspan>', svg)
        assert tint_match is not None
        tint_hex = tint_match.group(1).lower()
        assert tint_hex not in {"#000000", "#4f81bd"}


@requires_pptx
def test_pptx_bullet_sizing_default_pct_pts_and_pua(tmp_path: Path):
    import re

    slide_xml = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld
    xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr/>
      <p:sp>
        <p:nvSpPr><p:cNvPr id="2" name="Bullets"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>
        <p:spPr>
          <a:xfrm><a:off x="600000" y="600000"/><a:ext cx="10000000" cy="5000000"/></a:xfrm>
          <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
        </p:spPr>
        <p:txBody>
          <a:bodyPr/><a:lstStyle/>
          <a:p>
            <a:pPr marL="457200" indent="-342900"><a:buChar char="•"/></a:pPr>
            <a:r><a:rPr sz="2800"/><a:t>DefaultSizeItem</a:t></a:r>
          </a:p>
          <a:p>
            <a:pPr marL="457200" indent="-342900"><a:buSzPct val="150000"/><a:buChar char="▪"/></a:pPr>
            <a:r><a:rPr sz="2000"/><a:t>PctSizeItem</a:t></a:r>
          </a:p>
          <a:p>
            <a:pPr marL="457200" indent="-342900"><a:buSzPts val="2600"/><a:buChar char="★"/></a:pPr>
            <a:r><a:rPr sz="1800"/><a:t>PtsSizeItem</a:t></a:r>
          </a:p>
          <a:p>
            <a:pPr marL="457200" indent="-342900"><a:buFont typeface="Wingdings"/><a:buChar char="&#xF0FC;"/></a:pPr>
            <a:r><a:rPr sz="2200"/><a:t>PuaCheckItem</a:t></a:r>
          </a:p>
        </p:txBody>
      </p:sp>
    </p:spTree>
  </p:cSld>
</p:sld>
"""
    deck = _build_custom_pptx(tmp_path / "bullets.pptx", slide_xml=slide_xml)
    with docs.open(deck, zoom=1.0) as doc:
        assert isinstance(doc.pages, pptx_mod.PptxPages)
        svg = doc.pages.slide_svg(0)
        assert re.search(r'<tspan[^>]*font-size="28pt"[^>]*>•</tspan>', svg)
        assert re.search(r'<tspan[^>]*font-size="30pt"[^>]*>▪</tspan>', svg)
        assert re.search(r'<tspan[^>]*font-size="26pt"[^>]*>★</tspan>', svg)
        assert re.search(r'<tspan[^>]*font-size="22pt"[^>]*>✓</tspan>', svg)


@requires_pptx
def test_pptx_builtin_table_style_header_banding_and_borders(tmp_path: Path):
    import re

    slide_xml = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld
    xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr/>
      <p:graphicFrame>
        <p:nvGraphicFramePr><p:cNvPr id="4" name="StyledTable"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>
        <p:xfrm><a:off x="1000000" y="1000000"/><a:ext cx="8000000" cy="2400000"/></p:xfrm>
        <a:graphic>
          <a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table">
            <a:tbl>
              <a:tblPr firstRow="1" bandRow="1">
                <a:tableStyleId>{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}</a:tableStyleId>
              </a:tblPr>
              <a:tblGrid><a:gridCol w="4000000"/><a:gridCol w="4000000"/></a:tblGrid>
              <a:tr h="800000">
                <a:tc><a:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>HeaderA</a:t></a:r></a:p></a:txBody><a:tcPr/></a:tc>
                <a:tc><a:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>HeaderB</a:t></a:r></a:p></a:txBody><a:tcPr/></a:tc>
              </a:tr>
              <a:tr h="800000">
                <a:tc><a:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Row1A</a:t></a:r></a:p></a:txBody><a:tcPr/></a:tc>
                <a:tc><a:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Row1B</a:t></a:r></a:p></a:txBody><a:tcPr/></a:tc>
              </a:tr>
              <a:tr h="800000">
                <a:tc><a:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Row2A</a:t></a:r></a:p></a:txBody><a:tcPr/></a:tc>
                <a:tc><a:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Row2B</a:t></a:r></a:p></a:txBody><a:tcPr/></a:tc>
              </a:tr>
            </a:tbl>
          </a:graphicData>
        </a:graphic>
      </p:graphicFrame>
    </p:spTree>
  </p:cSld>
</p:sld>
"""
    empty_table_styles = (
        '<?xml version="1.0" encoding="UTF-8" standalone="yes"?>\n'
        '<a:tblStyleLst xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" '
        'def="{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}"/>\n'
    )
    deck = _build_custom_pptx(
        tmp_path / "table.pptx",
        slide_xml=slide_xml,
        extra_parts={"ppt/tableStyles.xml": empty_table_styles},
        extra_ct_overrides=[
            (
                "/ppt/tableStyles.xml",
                "application/vnd.openxmlformats-officedocument.presentationml.tableStyles+xml",
            )
        ],
        extra_pres_rels=[
            (
                "rId3",
                "http://schemas.openxmlformats.org/officeDocument/2006/relationships/tableStyles",
                "tableStyles.xml",
            )
        ],
    )
    with docs.open(deck, zoom=1.0) as doc:
        assert isinstance(doc.pages, pptx_mod.PptxPages)
        svg = doc.pages.slide_svg(0)
        # Accent 1 (#4f81bd) header fill + bold white header text + two distinct banded fills
        assert 'fill="#4f81bd"' in svg.lower()
        hdr_tspan = re.search(r"<tspan[^>]*>HeaderA</tspan>", svg)
        assert hdr_tspan is not None
        assert 'font-weight="bold"' in hdr_tspan.group(0)
        assert 'fill="#ffffff"' in hdr_tspan.group(0).lower()
        rect_fills = {
            m.lower()
            for m in re.findall(r'<rect[^>]*fill="(#[0-9a-fA-F]{6})"', svg)
            if m.lower() != "#ffffff"
        }
        assert "#4f81bd" in rect_fills
        assert len(rect_fills) >= 3  # header + band1H + band2H


@requires_pptx
def test_pptx_pie_and_doughnut_chart_data_labels(tmp_path: Path):
    slide_xml = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld
    xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
    xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart">
  <p:cSld>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr/>
      <p:graphicFrame>
        <p:nvGraphicFramePr><p:cNvPr id="5" name="Pie"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>
        <p:xfrm><a:off x="500000" y="900000"/><a:ext cx="5200000" cy="4800000"/></p:xfrm>
        <a:graphic>
          <a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart">
            <c:chart r:id="rId2"/>
          </a:graphicData>
        </a:graphic>
      </p:graphicFrame>
      <p:graphicFrame>
        <p:nvGraphicFramePr><p:cNvPr id="6" name="Doughnut"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>
        <p:xfrm><a:off x="6200000" y="900000"/><a:ext cx="5200000" cy="4800000"/></p:xfrm>
        <a:graphic>
          <a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart">
            <c:chart r:id="rId3"/>
          </a:graphicData>
        </a:graphic>
      </p:graphicFrame>
    </p:spTree>
  </p:cSld>
</p:sld>
"""
    pie_xml = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart">
  <c:chart>
    <c:plotArea>
      <c:pieChart>
        <c:ser>
          <c:idx val="0"/>
          <c:cat><c:strRef><c:strCache>
            <c:pt idx="0"><c:v>Search</c:v></c:pt>
            <c:pt idx="1"><c:v>Direct</c:v></c:pt>
          </c:strCache></c:strRef></c:cat>
          <c:val><c:numRef><c:numCache>
            <c:pt idx="0"><c:v>75</c:v></c:pt>
            <c:pt idx="1"><c:v>25</c:v></c:pt>
          </c:numCache></c:numRef></c:val>
        </c:ser>
        <c:dLbls>
          <c:showVal val="0"/>
          <c:showCatName val="1"/>
          <c:showPercent val="1"/>
        </c:dLbls>
      </c:pieChart>
    </c:plotArea>
  </c:chart>
</c:chartSpace>
"""
    doughnut_xml = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart">
  <c:chart>
    <c:plotArea>
      <c:doughnutChart>
        <c:ser>
          <c:idx val="0"/>
          <c:dLbls>
            <c:showVal val="1"/>
            <c:showCatName val="1"/>
            <c:separator>: </c:separator>
          </c:dLbls>
          <c:cat><c:strRef><c:strCache>
            <c:pt idx="0"><c:v>Q1</c:v></c:pt>
            <c:pt idx="1"><c:v>Q2</c:v></c:pt>
          </c:strCache></c:strRef></c:cat>
          <c:val><c:numRef><c:numCache>
            <c:pt idx="0"><c:v>120</c:v></c:pt>
            <c:pt idx="1"><c:v>180</c:v></c:pt>
          </c:numCache></c:numRef></c:val>
        </c:ser>
        <c:holeSize val="50"/>
      </c:doughnutChart>
    </c:plotArea>
  </c:chart>
</c:chartSpace>
"""
    slide_rels = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart" Target="../charts/chart1.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart" Target="../charts/chart2.xml"/>
</Relationships>
"""
    deck = _build_custom_pptx(
        tmp_path / "charts.pptx",
        slide_xml=slide_xml,
        slide_rels=slide_rels,
        extra_parts={
            "ppt/charts/chart1.xml": pie_xml,
            "ppt/charts/chart2.xml": doughnut_xml,
        },
        extra_ct_overrides=[
            (
                "/ppt/charts/chart1.xml",
                "application/vnd.openxmlformats-officedocument.drawingml.chart+xml",
            ),
            (
                "/ppt/charts/chart2.xml",
                "application/vnd.openxmlformats-officedocument.drawingml.chart+xml",
            ),
        ],
    )
    with docs.open(deck, zoom=1.0) as doc:
        assert isinstance(doc.pages, pptx_mod.PptxPages)
        svg = doc.pages.slide_svg(0)
        assert ">Search, 75%</text>" in svg
        assert ">Direct, 25%</text>" in svg
        assert ">Q1: 120</text>" in svg
        assert ">Q2: 180</text>" in svg


@requires_pptx
def test_pptx_emf_wmf_fallbacks_and_per_picture_placeholder(tmp_path: Path):
    import struct

    # Minimal EMF header + unsupported GDI+ record 109 + EOF (cannot convert to vector SVG)
    emf_header = bytearray(108)
    struct.pack_into("<II", emf_header, 0, 1, 108)
    struct.pack_into("<iiii", emf_header, 8, 0, 0, 100, 100)
    struct.pack_into("<iiii", emf_header, 24, 0, 0, 2000, 2000)
    struct.pack_into("<I", emf_header, 40, 0x464D4520)
    struct.pack_into("<II", emf_header, 44, 0x00010000, 144)
    struct.pack_into("<IH", emf_header, 52, 3, 1)
    struct.pack_into("<ii", emf_header, 72, 1024, 768)
    struct.pack_into("<ii", emf_header, 80, 320, 240)
    raw_emf = bytes(emf_header) + struct.pack("<IIII", 109, 16, 0, 0) + struct.pack("<IIIII", 14, 20, 0, 0, 20)

    png_fallback = _make_sample_png()
    svg_fallback = (
        b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 80 80" width="80" height="80">'
        b'<rect width="80" height="80" fill="#2563EB"/></svg>'
    )
    slide_xml = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld
    xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
    xmlns:mc="http://schemas.openxmlformats.org/markup-compatibility/2006"
    xmlns:asvg="http://schemas.microsoft.com/office/drawing/2016/SVG/main">
  <p:cSld>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr/>
      <p:sp>
        <p:nvSpPr><p:cNvPr id="2" name="Heading"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>
        <p:spPr><a:xfrm><a:off x="500000" y="300000"/><a:ext cx="9000000" cy="800000"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>
        <p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Slide Remains Intact</a:t></a:r></a:p></p:txBody>
      </p:sp>
      <mc:AlternateContent>
        <mc:Choice Requires="v">
          <p:pic>
            <p:nvPicPr><p:cNvPr id="10" name="EmfChoice"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>
            <p:blipFill><a:blip r:embed="rId2"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>
            <p:spPr><a:xfrm><a:off x="500000" y="1500000"/><a:ext cx="3000000" cy="2400000"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>
          </p:pic>
        </mc:Choice>
        <mc:Fallback>
          <p:pic>
            <p:nvPicPr><p:cNvPr id="10" name="PngFallback"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>
            <p:blipFill><a:blip r:embed="rId3"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>
            <p:spPr><a:xfrm><a:off x="500000" y="1500000"/><a:ext cx="3000000" cy="2400000"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>
          </p:pic>
        </mc:Fallback>
      </mc:AlternateContent>
      <p:pic>
        <p:nvPicPr><p:cNvPr id="11" name="SvgBlipPic"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>
        <p:blipFill>
          <a:blip r:embed="rId2">
            <a:extLst><a:ext uri="{96DAC541-7B7A-43D3-8B79-37D633B846F1}"><asvg:svgBlip r:embed="rId4"/></a:ext></a:extLst>
          </a:blip>
          <a:stretch><a:fillRect/></a:stretch>
        </p:blipFill>
        <p:spPr><a:xfrm><a:off x="4000000" y="1500000"/><a:ext cx="3000000" cy="2400000"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>
      </p:pic>
      <p:pic>
        <p:nvPicPr><p:cNvPr id="12" name="RawEmfPic"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>
        <p:blipFill><a:blip r:embed="rId2"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>
        <p:spPr><a:xfrm><a:off x="7500000" y="1500000"/><a:ext cx="3000000" cy="2400000"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>
      </p:pic>
    </p:spTree>
  </p:cSld>
</p:sld>
"""
    slide_rels = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/pic.emf"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/pic.png"/>
  <Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/pic.svg"/>
</Relationships>
"""
    deck = _build_custom_pptx(
        tmp_path / "emf.pptx",
        slide_xml=slide_xml,
        slide_rels=slide_rels,
        extra_parts={
            "ppt/media/pic.emf": raw_emf,
            "ppt/media/pic.png": png_fallback,
            "ppt/media/pic.svg": svg_fallback,
        },
        extra_ct_defaults=[
            ("emf", "application/x-msmetafile"),
            ("svg", "image/svg+xml"),
        ],
    )
    with docs.open(deck, zoom=1.0) as doc:
        assert isinstance(doc.pages, pptx_mod.PptxPages)
        assert doc.pages.slide_errors == {}
        svg = doc.pages.slide_svg(0)
        assert "Slide Remains Intact" in svg
        assert "data:image/png;base64," in svg
        assert "data:image/svg+xml;base64," in svg
        assert ">[EMF]</text>" in svg
        img = doc.pages[0]
        assert _count_non_white_pixels(img) > 500


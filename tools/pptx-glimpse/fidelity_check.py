#!/usr/bin/env python3
"""Generate fidelity test decks covering the 5 PPTX rendering gaps and export PNGs.

Usage:
    python tools/pptx-glimpse/fidelity_check.py --prefix before --out-dir /workspace/beta-tasks/pptx-fidelity-png
    python tools/pptx-glimpse/fidelity_check.py --prefix after  --out-dir /workspace/beta-tasks/pptx-fidelity-png
"""

from __future__ import annotations

import argparse
import io
import struct
import zipfile
from pathlib import Path

from PIL import Image, ImageDraw

import pixcell.docs as docs

SLIDE_NAMES = [
    "gap1-autoshape-fontref",
    "gap2-bullet-sizing",
    "gap3-builtin-table-styles",
    "gap4-pie-doughnut-labels",
    "gap5-emf-wmf-pictures",
]

THEME_XML = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:theme xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" name="Office Theme">
  <a:themeElements>
    <a:clrScheme name="Office">
      <a:dk1><a:sysClr val="windowText" lastClr="000000"/></a:dk1>
      <a:lt1><a:sysClr val="window" lastClr="FFFFFF"/></a:lt1>
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

SLIDE_MASTER_XML = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldMaster
    xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld>
    <p:bg><p:bgPr><a:solidFill><a:schemeClr val="bg1"/></a:solidFill><a:effectLst/></p:bgPr></p:bg>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr/>
      <p:sp>
        <p:nvSpPr><p:cNvPr id="2" name="Title Placeholder 1"/><p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>
        <p:spPr>
          <a:xfrm><a:off x="609600" y="274638"/><a:ext cx="10972800" cy="900000"/></a:xfrm>
          <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
        </p:spPr>
        <p:txBody><a:bodyPr anchor="ctr"/><a:lstStyle/><a:p><a:r><a:t>Master Title</a:t></a:r></a:p></p:txBody>
      </p:sp>
      <p:sp>
        <p:nvSpPr><p:cNvPr id="3" name="Text Placeholder 2"/><p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr><p:nvPr><p:ph type="body" idx="1"/></p:nvPr></p:nvSpPr>
        <p:spPr>
          <a:xfrm><a:off x="609600" y="1371600"/><a:ext cx="5200000" cy="5000000"/></a:xfrm>
          <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
        </p:spPr>
        <p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Master Body</a:t></a:r></a:p></p:txBody>
      </p:sp>
    </p:spTree>
  </p:cSld>
  <p:clrMap bg1="lt1" tx1="dk1" bg2="lt2" tx2="dk2" accent1="accent1" accent2="accent2"
            accent3="accent3" accent4="accent4" accent5="accent5" accent6="accent6"
            hlink="hlink" folHlink="folHlink"/>
  <p:sldLayoutIdLst><p:sldLayoutId id="2147483649" r:id="rId1"/></p:sldLayoutIdLst>
  <p:txStyles>
    <p:titleStyle>
      <a:lvl1pPr algn="l"><a:buNone/><a:defRPr sz="3600" b="1"><a:solidFill><a:schemeClr val="dk2"/></a:solidFill><a:latin typeface="+mj-lt"/></a:defRPr></a:lvl1pPr>
    </p:titleStyle>
    <p:bodyStyle>
      <a:lvl1pPr marL="342900" indent="-342900" algn="l">
        <a:spcBef><a:spcPct val="20000"/></a:spcBef>
        <a:buFont typeface="Arial"/>
        <a:buChar char="•"/>
        <a:defRPr sz="2800"><a:solidFill><a:schemeClr val="tx1"/></a:solidFill><a:latin typeface="+mn-lt"/></a:defRPr>
      </a:lvl1pPr>
      <a:lvl2pPr marL="742950" indent="-285750" algn="l">
        <a:spcBef><a:spcPct val="15000"/></a:spcBef>
        <a:buFont typeface="Arial"/>
        <a:buChar char="–"/>
        <a:defRPr sz="2400"><a:solidFill><a:schemeClr val="tx1"/></a:solidFill><a:latin typeface="+mn-lt"/></a:defRPr>
      </a:lvl2pPr>
      <a:lvl3pPr marL="1143000" indent="-228600" algn="l">
        <a:spcBef><a:spcPct val="15000"/></a:spcBef>
        <a:buFont typeface="Arial"/>
        <a:buChar char="•"/>
        <a:defRPr sz="2000"><a:solidFill><a:schemeClr val="tx1"/></a:solidFill><a:latin typeface="+mn-lt"/></a:defRPr>
      </a:lvl3pPr>
    </p:bodyStyle>
    <p:otherStyle>
      <a:lvl1pPr algn="l">
        <a:defRPr sz="1800"><a:solidFill><a:schemeClr val="tx1"/></a:solidFill><a:latin typeface="+mn-lt"/></a:defRPr>
      </a:lvl1pPr>
    </p:otherStyle>
  </p:txStyles>
</p:sldMaster>
"""

SLIDE_LAYOUT_XML = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sldLayout
    xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main"
    type="obj">
  <p:cSld name="Title and Content">
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr/>
      <p:sp>
        <p:nvSpPr><p:cNvPr id="2" name="Title 1"/><p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>
        <p:spPr/>
        <p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:endParaRPr/></a:p></p:txBody>
      </p:sp>
      <p:sp>
        <p:nvSpPr><p:cNvPr id="3" name="Content Placeholder 2"/><p:cNvSpPr><a:spLocks noGrp="1"/></p:cNvSpPr><p:nvPr><p:ph idx="1"/></p:nvPr></p:nvSpPr>
        <p:spPr/>
        <p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:endParaRPr/></a:p></p:txBody>
      </p:sp>
    </p:spTree>
  </p:cSld>
</p:sldLayout>
"""

TABLE_STYLES_EMPTY_XML = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<a:tblStyleLst xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main" def="{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}"/>
"""


def _slide1_autoshape_fontref_xml() -> str:
    return """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:sld
    xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:cSld>
    <p:bg>
      <p:bgPr>
        <a:gradFill>
          <a:gsLst>
            <a:gs pos="0"><a:srgbClr val="102048"/></a:gs>
            <a:gs pos="100000"><a:srgbClr val="4A1259"/></a:gs>
          </a:gsLst>
          <a:lin ang="5400000" scaled="0"/>
        </a:gradFill>
        <a:effectLst/>
      </p:bgPr>
    </p:bg>
    <p:spTree>
      <p:nvGrpSpPr><p:cNvPr id="1" name=""/><p:cNvGrpSpPr/><p:nvPr/></p:nvGrpSpPr>
      <p:grpSpPr/>
      <p:sp>
        <p:nvSpPr><p:cNvPr id="2" name="Heading"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>
        <p:spPr>
          <a:xfrm><a:off x="600000" y="250000"/><a:ext cx="11000000" cy="700000"/></a:xfrm>
          <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
        </p:spPr>
        <p:txBody>
          <a:bodyPr/>
          <a:lstStyle/>
          <a:p><a:r><a:rPr sz="3200" b="1"><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill></a:rPr><a:t>Gap 1: Autoshape a:fontRef Scheme Colours &amp; Tints</a:t></a:r></a:p>
        </p:txBody>
      </p:sp>
      <!-- Row 1: lt1 schemeClr on gradient/solid shapes (matches python-pptx add_shape) -->
      <p:sp>
        <p:nvSpPr><p:cNvPr id="10" name="Rounded"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
        <p:spPr>
          <a:xfrm><a:off x="600000" y="1200000"/><a:ext cx="2500000" cy="2200000"/></a:xfrm>
          <a:prstGeom prst="roundRect"><a:avLst/></a:prstGeom>
          <a:gradFill>
            <a:gsLst><a:gs pos="0"><a:srgbClr val="E65C00"/></a:gs><a:gs pos="100000"><a:srgbClr val="0066CC"/></a:gs></a:gsLst>
            <a:lin ang="2700000"/>
          </a:gradFill>
          <a:ln w="25400"><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill></a:ln>
        </p:spPr>
        <p:style>
          <a:lnRef idx="1"><a:schemeClr val="accent1"/></a:lnRef>
          <a:fillRef idx="3"><a:schemeClr val="accent1"/></a:fillRef>
          <a:effectRef idx="2"><a:schemeClr val="accent1"/></a:effectRef>
          <a:fontRef idx="minor"><a:schemeClr val="lt1"/></a:fontRef>
        </p:style>
        <p:txBody>
          <a:bodyPr anchor="ctr"/><a:lstStyle/>
          <a:p><a:pPr algn="ctr"/><a:r><a:rPr sz="2400" b="1"/><a:t>Rounded (lt1)</a:t></a:r></a:p>
        </p:txBody>
      </p:sp>
      <p:sp>
        <p:nvSpPr><p:cNvPr id="11" name="Oval"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
        <p:spPr>
          <a:xfrm><a:off x="3400000" y="1200000"/><a:ext cx="2500000" cy="2200000"/></a:xfrm>
          <a:prstGeom prst="ellipse"><a:avLst/></a:prstGeom>
          <a:solidFill><a:schemeClr val="dk2"/></a:solidFill>
          <a:ln w="25400"><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill></a:ln>
        </p:spPr>
        <p:style>
          <a:lnRef idx="1"><a:schemeClr val="accent1"/></a:lnRef>
          <a:fillRef idx="3"><a:schemeClr val="accent1"/></a:fillRef>
          <a:effectRef idx="2"><a:schemeClr val="accent1"/></a:effectRef>
          <a:fontRef idx="minor"><a:schemeClr val="lt1"/></a:fontRef>
        </p:style>
        <p:txBody>
          <a:bodyPr anchor="ctr"/><a:lstStyle/>
          <a:p><a:pPr algn="ctr"/><a:r><a:rPr sz="2400" b="1"/><a:t>Oval (lt1)</a:t></a:r></a:p>
        </p:txBody>
      </p:sp>
      <p:sp>
        <p:nvSpPr><p:cNvPr id="12" name="Star"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
        <p:spPr>
          <a:xfrm><a:off x="6200000" y="1200000"/><a:ext cx="2600000" cy="2200000"/></a:xfrm>
          <a:prstGeom prst="star5"><a:avLst/></a:prstGeom>
          <a:solidFill><a:srgbClr val="8B1E3F"/></a:solidFill>
          <a:ln w="25400"><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill></a:ln>
        </p:spPr>
        <p:style>
          <a:fontRef idx="major"><a:schemeClr val="accent6"><a:lumMod val="25000"/><a:lumOff val="75000"/></a:schemeClr></a:fontRef>
        </p:style>
        <p:txBody>
          <a:bodyPr anchor="ctr"/><a:lstStyle/>
          <a:p><a:pPr algn="ctr"/><a:r><a:rPr sz="2200" b="1"/><a:t>Star Tint</a:t></a:r></a:p>
        </p:txBody>
      </p:sp>
      <p:sp>
        <p:nvSpPr><p:cNvPr id="13" name="Arrow"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
        <p:spPr>
          <a:xfrm><a:off x="9100000" y="1200000"/><a:ext cx="2500000" cy="2200000"/></a:xfrm>
          <a:prstGeom prst="rightArrow"><a:avLst/></a:prstGeom>
          <a:solidFill><a:srgbClr val="1B4332"/></a:solidFill>
          <a:ln w="25400"><a:solidFill><a:srgbClr val="FFFFFF"/></a:solidFill></a:ln>
        </p:spPr>
        <p:style>
          <a:fontRef idx="minor"><a:schemeClr val="accent3"><a:lumMod val="30000"/><a:lumOff val="70000"/></a:schemeClr></a:fontRef>
        </p:style>
        <p:txBody>
          <a:bodyPr anchor="ctr"/><a:lstStyle/>
          <a:p><a:pPr algn="ctr"/><a:r><a:rPr sz="2200" b="1"/><a:t>Arrow</a:t></a:r></a:p>
        </p:txBody>
      </p:sp>
      <!-- Row 2: lumMod/lumOff dark shade on light card + bullet color inheritance from fontRef -->
      <p:sp>
        <p:nvSpPr><p:cNvPr id="14" name="LightCard"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
        <p:spPr>
          <a:xfrm><a:off x="600000" y="3900000"/><a:ext cx="5300000" cy="2300000"/></a:xfrm>
          <a:prstGeom prst="roundRect"><a:avLst/></a:prstGeom>
          <a:solidFill><a:srgbClr val="EDF2FB"/></a:solidFill>
        </p:spPr>
        <p:style>
          <a:fontRef idx="major"><a:schemeClr val="accent1"><a:lumMod val="50000"/></a:schemeClr></a:fontRef>
        </p:style>
        <p:txBody>
          <a:bodyPr anchor="ctr"/><a:lstStyle/>
          <a:p><a:pPr algn="ctr"/><a:r><a:rPr sz="2400" b="1"/><a:t>Shaded Accent1 (lumMod 50%)</a:t></a:r></a:p>
          <a:p><a:pPr algn="ctr"/><a:r><a:rPr sz="1800"/><a:t>Major font + dark blue schemeClr</a:t></a:r></a:p>
        </p:txBody>
      </p:sp>
      <p:sp>
        <p:nvSpPr><p:cNvPr id="15" name="DarkBulletCard"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
        <p:spPr>
          <a:xfrm><a:off x="6200000" y="3900000"/><a:ext cx="5400000" cy="2300000"/></a:xfrm>
          <a:prstGeom prst="roundRect"><a:avLst/></a:prstGeom>
          <a:solidFill><a:srgbClr val="1D2D44"/></a:solidFill>
          <a:ln w="19050"><a:solidFill><a:schemeClr val="accent5"/></a:solidFill></a:ln>
        </p:spPr>
        <p:style>
          <a:fontRef idx="minor"><a:schemeClr val="lt1"/></a:fontRef>
        </p:style>
        <p:txBody>
          <a:bodyPr anchor="ctr" lIns="250000"/><a:lstStyle/>
          <a:p><a:pPr marL="342900" indent="-285750"><a:buChar char="•"/></a:pPr><a:r><a:rPr sz="2000"/><a:t>Bullets in dark shape inherit lt1</a:t></a:r></a:p>
          <a:p><a:pPr marL="342900" indent="-285750"><a:buChar char="•"/></a:pPr><a:r><a:rPr sz="2000"/><a:t>Explicit run colour still overrides: </a:t></a:r><a:r><a:rPr sz="2000" b="1"><a:solidFill><a:srgbClr val="FFD166"/></a:solidFill></a:rPr><a:t>Gold</a:t></a:r></a:p>
        </p:txBody>
      </p:sp>
    </p:spTree>
  </p:cSld>
</p:sld>
"""


def _slide2_bullet_sizing_xml() -> str:
    return """\
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
        <p:nvSpPr><p:cNvPr id="2" name="Title 1"/><p:cNvSpPr/><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>
        <p:spPr/>
        <p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Gap 2: Bullet Glyph Sizing (Default, buSzPct, buSzPts)</a:t></a:r></a:p></p:txBody>
      </p:sp>
      <!-- Left placeholder: inherits bodyStyle from SlideMaster (28pt / 24pt / 20pt with no explicit buSz) -->
      <p:sp>
        <p:nvSpPr><p:cNvPr id="3" name="Content Placeholder 2"/><p:cNvSpPr/><p:nvPr><p:ph idx="1"/></p:nvPr></p:nvSpPr>
        <p:spPr/>
        <p:txBody>
          <a:bodyPr/>
          <a:lstStyle/>
          <a:p><a:r><a:t>Default 28pt level-0 bullet (100%)</a:t></a:r></a:p>
          <a:p><a:pPr lvl="1"/><a:r><a:t>Level-1 en-dash at 24pt</a:t></a:r></a:p>
          <a:p><a:pPr lvl="2"/><a:r><a:t>Level-2 bullet at 20pt</a:t></a:r></a:p>
          <a:p><a:r><a:t>Another 28pt primary point</a:t></a:r></a:p>
        </p:txBody>
      </p:sp>
      <!-- Right box: explicit buSzPct, buSzPts, buAutoNum, and Wingdings PUA bullets -->
      <p:sp>
        <p:nvSpPr><p:cNvPr id="4" name="ExplicitBullets"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>
        <p:spPr>
          <a:xfrm><a:off x="6200000" y="1371600"/><a:ext cx="5400000" cy="5000000"/></a:xfrm>
          <a:prstGeom prst="roundRect"><a:avLst/></a:prstGeom>
          <a:solidFill><a:srgbClr val="F8FAFC"/></a:solidFill>
          <a:ln w="12700"><a:solidFill><a:srgbClr val="CBD5E1"/></a:solidFill></a:ln>
        </p:spPr>
        <p:txBody>
          <a:bodyPr lIns="228600" tIns="180000"/>
          <a:lstStyle/>
          <a:p>
            <a:pPr marL="457200" indent="-381000">
              <a:spcBef><a:spcPts val="800"/></a:spcBef>
              <a:buClr><a:srgbClr val="C0504D"/></a:buClr>
              <a:buSzPct val="150000"/>
              <a:buChar char="•"/>
            </a:pPr>
            <a:r><a:rPr sz="2200"/><a:t>buSzPct=150% on 22pt (33pt bullet)</a:t></a:r>
          </a:p>
          <a:p>
            <a:pPr marL="457200" indent="-381000">
              <a:spcBef><a:spcPts val="800"/></a:spcBef>
              <a:buClr><a:srgbClr val="4F81BD"/></a:buClr>
              <a:buSzPct val="75000"/>
              <a:buChar char="▪"/>
            </a:pPr>
            <a:r><a:rPr sz="2200"/><a:t>buSzPct=75% on 22pt (16.5pt square)</a:t></a:r>
          </a:p>
          <a:p>
            <a:pPr marL="457200" indent="-381000">
              <a:spcBef><a:spcPts val="800"/></a:spcBef>
              <a:buClr><a:srgbClr val="2D6A4F"/></a:buClr>
              <a:buSzPts val="3000"/>
              <a:buChar char="★"/>
            </a:pPr>
            <a:r><a:rPr sz="2000"/><a:t>buSzPts=3000 (30pt star on 20pt text)</a:t></a:r>
          </a:p>
          <a:p>
            <a:pPr marL="457200" indent="-381000">
              <a:spcBef><a:spcPts val="800"/></a:spcBef>
              <a:buFont typeface="Wingdings"/>
              <a:buChar char="&#xF0FC;"/>
            </a:pPr>
            <a:r><a:rPr sz="2200"/><a:t>Wingdings PUA check (U+F0FC) at 22pt</a:t></a:r>
          </a:p>
          <a:p>
            <a:pPr marL="457200" indent="-381000">
              <a:spcBef><a:spcPts val="800"/></a:spcBef>
              <a:buAutoNum type="arabicPeriod" startAt="1"/>
            </a:pPr>
            <a:r><a:rPr sz="2200"/><a:t>Numbered auto-bullet at 22pt</a:t></a:r>
          </a:p>
        </p:txBody>
      </p:sp>
    </p:spTree>
  </p:cSld>
</p:sld>
"""


def _make_table_graphic_frame(
    shape_id: int,
    name: str,
    x: int,
    y: int,
    cx: int,
    cy: int,
    style_id: str,
    tbl_attrs: str,
    col_widths: list[int],
    rows: list[list[str]],
    row_height: int = 520000,
) -> str:
    grid_cols = "".join(f'<a:gridCol w="{w}"/>' for w in col_widths)
    tr_list = []
    for row in rows:
        tcs = []
        for idx, val in enumerate(row):
            algn = "l" if idx == 0 else "r"
            tcs.append(
                f"<a:tc><a:txBody><a:bodyPr anchor=\"ctr\"/><a:lstStyle/>"
                f'<a:p><a:pPr algn="{algn}"/><a:r><a:rPr sz="1500"/><a:t>{val}</a:t></a:r></a:p>'
                f"</a:txBody><a:tcPr/></a:tc>"
            )
        tr_list.append(f'<a:tr h="{row_height}">{"".join(tcs)}</a:tr>')
    return f"""\
      <p:graphicFrame>
        <p:nvGraphicFramePr><p:cNvPr id="{shape_id}" name="{name}"/><p:cNvGraphicFramePr><a:graphicFrameLocks noGrp="1"/></p:cNvGraphicFramePr><p:nvPr/></p:nvGraphicFramePr>
        <p:xfrm><a:off x="{x}" y="{y}"/><a:ext cx="{cx}" cy="{cy}"/></p:xfrm>
        <a:graphic>
          <a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/table">
            <a:tbl>
              <a:tblPr {tbl_attrs}><a:tableStyleId>{style_id}</a:tableStyleId></a:tblPr>
              <a:tblGrid>{grid_cols}</a:tblGrid>
              {"".join(tr_list)}
            </a:tbl>
          </a:graphicData>
        </a:graphic>
      </p:graphicFrame>"""


def _slide3_table_styles_xml() -> str:
    main_tbl = _make_table_graphic_frame(
        shape_id=10,
        name="MediumStyle2Accent1",
        x=600000,
        y=1300000,
        cx=6200000,
        cy=3120000,
        style_id="{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}",
        tbl_attrs='firstRow="1" bandRow="1"',
        col_widths=[2200000, 1000000, 1000000, 1000000, 1000000],
        rows=[
            ["Region", "Q1", "Q2", "Q3", "Q4"],
            ["North America", "$120M", "$135M", "$150M", "$171M"],
            ["Europe", "$90M", "$95M", "$101M", "$120M"],
            ["Asia-Pacific", "$70M", "$88M", "$110M", "$140M"],
            ["Latin America", "$30M", "$33M", "$41M", "$44M"],
            ["Total", "$310M", "$351M", "$402M", "$475M"],
        ],
        row_height=500000,
    )
    light_tbl = _make_table_graphic_frame(
        shape_id=11,
        name="LightStyle1Accent2",
        x=7100000,
        y=1300000,
        cx=4500000,
        cy=2300000,
        style_id="{0E3FDE45-AF77-4B5C-9715-49D594BDF05E}",
        tbl_attrs='firstRow="1" bandRow="1" lastRow="1"',
        col_widths=[2100000, 1200000, 1200000],
        rows=[
            ["Light Style 1 (Acc2)", "2025", "2026"],
            ["Search", "45%", "48%"],
            ["Direct", "35%", "34%"],
            ["Total", "80%", "82%"],
        ],
        row_height=520000,
    )
    med3_tbl = _make_table_graphic_frame(
        shape_id=12,
        name="MediumStyle2Accent3",
        x=7100000,
        y=3900000,
        cx=4500000,
        cy=2300000,
        style_id="{F5AB1C69-6EDB-4FF4-983F-18BD219EF322}",
        tbl_attrs='firstRow="1" bandRow="1" firstCol="1"',
        col_widths=[2100000, 1200000, 1200000],
        rows=[
            ["Med Style 2 (Acc3)", "Plan", "Actual"],
            ["Latency p50", "100ms", "90ms"],
            ["Latency p95", "250ms", "210ms"],
            ["Uptime", "99.9%", "99.98%"],
        ],
        row_height=520000,
    )
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
        <p:nvSpPr><p:cNvPr id="2" name="Title 1"/><p:cNvSpPr/><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>
        <p:spPr/>
        <p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Gap 3: Built-in OOXML Table Styles (Theme Colours)</a:t></a:r></a:p></p:txBody>
      </p:sp>
{main_tbl}
{light_tbl}
{med3_tbl}
    </p:spTree>
  </p:cSld>
</p:sld>
"""


PIE_CHART_XML = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"
              xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <c:chart>
    <c:title>
      <c:tx><c:rich><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Traffic Share (Pie + %)</a:t></a:r></a:p></c:rich></c:tx>
    </c:title>
    <c:plotArea>
      <c:pieChart>
        <c:varyColors val="1"/>
        <c:ser>
          <c:idx val="0"/>
          <c:order val="0"/>
          <c:tx><c:v>Traffic</c:v></c:tx>
          <c:cat>
            <c:strRef>
              <c:strCache>
                <c:ptCount val="4"/>
                <c:pt idx="0"><c:v>Search</c:v></c:pt>
                <c:pt idx="1"><c:v>Social</c:v></c:pt>
                <c:pt idx="2"><c:v>Direct</c:v></c:pt>
                <c:pt idx="3"><c:v>Email</c:v></c:pt>
              </c:strCache>
            </c:strRef>
          </c:cat>
          <c:val>
            <c:numRef>
              <c:numCache>
                <c:ptCount val="4"/>
                <c:pt idx="0"><c:v>45</c:v></c:pt>
                <c:pt idx="1"><c:v>25</c:v></c:pt>
                <c:pt idx="2"><c:v>20</c:v></c:pt>
                <c:pt idx="3"><c:v>10</c:v></c:pt>
              </c:numCache>
            </c:numRef>
          </c:val>
        </c:ser>
        <c:dLbls>
          <c:showLegendKey val="0"/>
          <c:showVal val="0"/>
          <c:showCatName val="1"/>
          <c:showSerName val="0"/>
          <c:showPercent val="1"/>
        </c:dLbls>
      </c:pieChart>
    </c:plotArea>
    <c:legend><c:legendPos val="b"/></c:legend>
  </c:chart>
</c:chartSpace>
"""

DOUGHNUT_CHART_XML = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<c:chartSpace xmlns:c="http://schemas.openxmlformats.org/drawingml/2006/chart"
              xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main">
  <c:chart>
    <c:title>
      <c:tx><c:rich><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Regional Revenue (Doughnut + Val)</a:t></a:r></a:p></c:rich></c:tx>
    </c:title>
    <c:plotArea>
      <c:doughnutChart>
        <c:varyColors val="1"/>
        <c:ser>
          <c:idx val="0"/>
          <c:order val="0"/>
          <c:tx><c:v>Revenue</c:v></c:tx>
          <c:dLbls>
            <c:showVal val="1"/>
            <c:showCatName val="1"/>
            <c:showPercent val="0"/>
            <c:separator>: </c:separator>
          </c:dLbls>
          <c:cat>
            <c:strRef>
              <c:strCache>
                <c:ptCount val="4"/>
                <c:pt idx="0"><c:v>Q1</c:v></c:pt>
                <c:pt idx="1"><c:v>Q2</c:v></c:pt>
                <c:pt idx="2"><c:v>Q3</c:v></c:pt>
                <c:pt idx="3"><c:v>Q4</c:v></c:pt>
              </c:strCache>
            </c:strRef>
          </c:cat>
          <c:val>
            <c:numRef>
              <c:numCache>
                <c:ptCount val="4"/>
                <c:pt idx="0"><c:v>120</c:v></c:pt>
                <c:pt idx="1"><c:v>150</c:v></c:pt>
                <c:pt idx="2"><c:v>180</c:v></c:pt>
                <c:pt idx="3"><c:v>210</c:v></c:pt>
              </c:numCache>
            </c:numRef>
          </c:val>
        </c:ser>
        <c:holeSize val="50"/>
      </c:doughnutChart>
    </c:plotArea>
    <c:legend><c:legendPos val="b"/></c:legend>
  </c:chart>
</c:chartSpace>
"""


def _slide4_charts_xml() -> str:
    return """\
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
      <p:sp>
        <p:nvSpPr><p:cNvPr id="2" name="Title 1"/><p:cNvSpPr/><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>
        <p:spPr/>
        <p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Gap 4: Pie &amp; Doughnut Chart Data Labels (c:dLbls)</a:t></a:r></a:p></p:txBody>
      </p:sp>
      <p:graphicFrame>
        <p:nvGraphicFramePr><p:cNvPr id="10" name="PieChart"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>
        <p:xfrm><a:off x="500000" y="1350000"/><a:ext cx="5400000" cy="5000000"/></p:xfrm>
        <a:graphic>
          <a:graphicData uri="http://schemas.openxmlformats.org/drawingml/2006/chart">
            <c:chart r:id="rId2"/>
          </a:graphicData>
        </a:graphic>
      </p:graphicFrame>
      <p:graphicFrame>
        <p:nvGraphicFramePr><p:cNvPr id="11" name="DoughnutChart"/><p:cNvGraphicFramePr/><p:nvPr/></p:nvGraphicFramePr>
        <p:xfrm><a:off x="6300000" y="1350000"/><a:ext cx="5400000" cy="5000000"/></p:xfrm>
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


def _make_fallback_png() -> bytes:
    img = Image.new("RGB", (360, 240), (16, 124, 65))
    draw = ImageDraw.Draw(img)
    draw.rectangle([12, 12, 348, 228], outline=(255, 255, 255), width=6)
    draw.ellipse([110, 45, 250, 185], fill=(255, 214, 10), outline=(255, 255, 255), width=4)
    draw.polygon([(155, 115), (175, 138), (212, 92)], fill=(16, 124, 65))
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return buf.getvalue()


def _make_svg_blip_bytes() -> bytes:
    return (
        b'<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 240 160" width="240" height="160">'
        b'<rect width="240" height="160" rx="16" fill="#1E40AF"/>'
        b'<circle cx="120" cy="80" r="46" fill="#38BDF8"/>'
        b'<polygon points="120,46 132,72 160,72 138,88 146,114 120,98 94,114 102,88 80,72 108,72" fill="#FDE047"/>'
        b"</svg>"
    )


def _make_unsupported_emf_bytes() -> bytes:
    """Build an EMF header + unsupported GDI+ record (record type 109) + EOF."""
    # Minimal 108-byte EMR_HEADER (type=1, size=108) followed by unsupported record type 109 and EMR_EOF (type=14, size=20)
    header = bytearray(108)
    struct.pack_into("<II", header, 0, 1, 108)  # iType=1, nSize=108
    struct.pack_into("<iiii", header, 8, 0, 0, 200, 120)  # rclBounds
    struct.pack_into("<iiii", header, 24, 0, 0, 4000, 2400)  # rclFrame
    struct.pack_into("<I", header, 40, 0x464D4520)  # dSignature = ' EMF'
    struct.pack_into("<II", header, 44, 0x00010000, 108 + 16 + 20)  # nVersion, nBytes
    struct.pack_into("<IH", header, 52, 3, 1)  # nRecords=3, nHandles=1
    struct.pack_into("<ii", header, 72, 1024, 768)  # szlDevice
    struct.pack_into("<ii", header, 80, 320, 240)  # szlMillimeters
    unsupported_rec = struct.pack("<IIII", 109, 16, 0, 0)
    eof_rec = struct.pack("<IIIII", 14, 20, 0, 0, 20)
    return bytes(header) + unsupported_rec + eof_rec


def _slide5_emf_wmf_xml() -> str:
    return """\
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
        <p:nvSpPr><p:cNvPr id="2" name="Title 1"/><p:cNvSpPr/><p:nvPr><p:ph type="title"/></p:nvPr></p:nvSpPr>
        <p:spPr/>
        <p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:r><a:t>Gap 5: EMF/WMF Fallbacks &amp; Per-Picture Placeholder</a:t></a:r></a:p></p:txBody>
      </p:sp>
      <!-- Normal slide content that must always render even when a raw EMF has no fallback -->
      <p:sp>
        <p:nvSpPr><p:cNvPr id="3" name="Banner"/><p:cNvSpPr/><p:nvPr/></p:nvSpPr>
        <p:spPr>
          <a:xfrm><a:off x="600000" y="1250000"/><a:ext cx="11000000" cy="700000"/></a:xfrm>
          <a:prstGeom prst="roundRect"><a:avLst/></a:prstGeom>
          <a:solidFill><a:schemeClr val="accent1"/></a:solidFill>
        </p:spPr>
        <p:style><a:fontRef idx="minor"><a:schemeClr val="lt1"/></a:fontRef></p:style>
        <p:txBody>
          <a:bodyPr anchor="ctr"/><a:lstStyle/>
          <a:p><a:pPr algn="ctr"/><a:r><a:rPr sz="2000" b="1"/><a:t>Slide shapes &amp; text remain intact alongside EMF pictures</a:t></a:r></a:p>
        </p:txBody>
      </p:sp>
      <!-- Case A: mc:AlternateContent with EMF Choice and PNG Fallback -->
      <mc:AlternateContent>
        <mc:Choice Requires="v">
          <p:pic>
            <p:nvPicPr><p:cNvPr id="10" name="AltContentEmf"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>
            <p:blipFill><a:blip r:embed="rId2"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>
            <p:spPr>
              <a:xfrm><a:off x="600000" y="2250000"/><a:ext cx="3300000" cy="2400000"/></a:xfrm>
              <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
            </p:spPr>
          </p:pic>
        </mc:Choice>
        <mc:Fallback>
          <p:pic>
            <p:nvPicPr><p:cNvPr id="10" name="AltContentPng"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>
            <p:blipFill><a:blip r:embed="rId3"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>
            <p:spPr>
              <a:xfrm><a:off x="600000" y="2250000"/><a:ext cx="3300000" cy="2400000"/></a:xfrm>
              <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
            </p:spPr>
          </p:pic>
        </mc:Fallback>
      </mc:AlternateContent>
      <p:sp>
        <p:nvSpPr><p:cNvPr id="20" name="CapA"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>
        <p:spPr><a:xfrm><a:off x="600000" y="4800000"/><a:ext cx="3300000" cy="600000"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>
        <p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:pPr algn="ctr"/><a:r><a:rPr sz="1600" b="1"/><a:t>mc:AlternateContent -&gt; PNG</a:t></a:r></a:p></p:txBody>
      </p:sp>
      <!-- Case B: a:blip with primary EMF and asvg:svgBlip alternate in extLst -->
      <p:pic>
        <p:nvPicPr><p:cNvPr id="11" name="BlipSvgAlt"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>
        <p:blipFill>
          <a:blip r:embed="rId2">
            <a:extLst>
              <a:ext uri="{96DAC541-7B7A-43D3-8B79-37D633B846F1}">
                <asvg:svgBlip r:embed="rId4"/>
              </a:ext>
            </a:extLst>
          </a:blip>
          <a:stretch><a:fillRect/></a:stretch>
        </p:blipFill>
        <p:spPr>
          <a:xfrm><a:off x="4450000" y="2250000"/><a:ext cx="3300000" cy="2400000"/></a:xfrm>
          <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
        </p:spPr>
      </p:pic>
      <p:sp>
        <p:nvSpPr><p:cNvPr id="21" name="CapB"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>
        <p:spPr><a:xfrm><a:off x="4450000" y="4800000"/><a:ext cx="3300000" cy="600000"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>
        <p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:pPr algn="ctr"/><a:r><a:rPr sz="1600" b="1"/><a:t>a:blip extLst -&gt; svgBlip</a:t></a:r></a:p></p:txBody>
      </p:sp>
      <!-- Case C: Raw unsupported EMF with NO fallback -> [EMF] placeholder box -->
      <p:pic>
        <p:nvPicPr><p:cNvPr id="12" name="RawEmfOnly"/><p:cNvPicPr/><p:nvPr/></p:nvPicPr>
        <p:blipFill><a:blip r:embed="rId2"/><a:stretch><a:fillRect/></a:stretch></p:blipFill>
        <p:spPr>
          <a:xfrm><a:off x="8300000" y="2250000"/><a:ext cx="3300000" cy="2400000"/></a:xfrm>
          <a:prstGeom prst="rect"><a:avLst/></a:prstGeom>
        </p:spPr>
      </p:pic>
      <p:sp>
        <p:nvSpPr><p:cNvPr id="22" name="CapC"/><p:cNvSpPr txBox="1"/><p:nvPr/></p:nvSpPr>
        <p:spPr><a:xfrm><a:off x="8300000" y="4800000"/><a:ext cx="3300000" cy="600000"/></a:xfrm><a:prstGeom prst="rect"><a:avLst/></a:prstGeom></p:spPr>
        <p:txBody><a:bodyPr/><a:lstStyle/><a:p><a:pPr algn="ctr"/><a:r><a:rPr sz="1600" b="1"/><a:t>Raw EMF -&gt; [EMF] placeholder</a:t></a:r></a:p></p:txBody>
      </p:sp>
    </p:spTree>
  </p:cSld>
</p:sld>
"""


def build_fidelity_deck(path: Path) -> Path:
    """Write the 5-slide fidelity verification .pptx archive to ``path``."""
    slides = [
        _slide1_autoshape_fontref_xml(),
        _slide2_bullet_sizing_xml(),
        _slide3_table_styles_xml(),
        _slide4_charts_xml(),
        _slide5_emf_wmf_xml(),
    ]

    content_types = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Types xmlns="http://schemas.openxmlformats.org/package/2006/content-types">
  <Default Extension="rels" ContentType="application/vnd.openxmlformats-package.relationships+xml"/>
  <Default Extension="xml" ContentType="application/xml"/>
  <Default Extension="png" ContentType="image/png"/>
  <Default Extension="svg" ContentType="image/svg+xml"/>
  <Default Extension="emf" ContentType="image/emf"/>
  <Override PartName="/ppt/presentation.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.presentation.main+xml"/>
  <Override PartName="/ppt/slideMasters/slideMaster1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideMaster+xml"/>
  <Override PartName="/ppt/slideLayouts/slideLayout1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slideLayout+xml"/>
  <Override PartName="/ppt/theme/theme1.xml" ContentType="application/vnd.openxmlformats-officedocument.theme+xml"/>
  <Override PartName="/ppt/tableStyles.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.tableStyles+xml"/>
  <Override PartName="/ppt/charts/chart1.xml" ContentType="application/vnd.openxmlformats-officedocument.drawingml.chart+xml"/>
  <Override PartName="/ppt/charts/chart2.xml" ContentType="application/vnd.openxmlformats-officedocument.drawingml.chart+xml"/>
  <Override PartName="/ppt/slides/slide1.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>
  <Override PartName="/ppt/slides/slide2.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>
  <Override PartName="/ppt/slides/slide3.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>
  <Override PartName="/ppt/slides/slide4.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>
  <Override PartName="/ppt/slides/slide5.xml" ContentType="application/vnd.openxmlformats-officedocument.presentationml.slide+xml"/>
</Types>
"""

    root_rels = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/officeDocument" Target="ppt/presentation.xml"/>
</Relationships>
"""

    pres_xml = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<p:presentation
    xmlns:a="http://schemas.openxmlformats.org/drawingml/2006/main"
    xmlns:r="http://schemas.openxmlformats.org/officeDocument/2006/relationships"
    xmlns:p="http://schemas.openxmlformats.org/presentationml/2006/main">
  <p:sldMasterIdLst><p:sldMasterId id="2147483648" r:id="rId1"/></p:sldMasterIdLst>
  <p:sldIdLst>
    <p:sldId id="256" r:id="rId2"/>
    <p:sldId id="257" r:id="rId3"/>
    <p:sldId id="258" r:id="rId4"/>
    <p:sldId id="259" r:id="rId5"/>
    <p:sldId id="260" r:id="rId6"/>
  </p:sldIdLst>
  <p:sldSz cx="12192000" cy="6858000"/>
  <p:notesSz cx="6858000" cy="9144000"/>
</p:presentation>
"""

    pres_rels = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="slideMasters/slideMaster1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide1.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide2.xml"/>
  <Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide3.xml"/>
  <Relationship Id="rId5" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide4.xml"/>
  <Relationship Id="rId6" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slide" Target="slides/slide5.xml"/>
  <Relationship Id="rId7" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/tableStyles" Target="tableStyles.xml"/>
</Relationships>
"""

    master_rels = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/theme" Target="../theme/theme1.xml"/>
</Relationships>
"""

    layout_rels = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideMaster" Target="../slideMasters/slideMaster1.xml"/>
</Relationships>
"""

    plain_slide_rels = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
</Relationships>
"""

    chart_slide_rels = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart" Target="../charts/chart1.xml"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/chart" Target="../charts/chart2.xml"/>
</Relationships>
"""

    emf_slide_rels = """\
<?xml version="1.0" encoding="UTF-8" standalone="yes"?>
<Relationships xmlns="http://schemas.openxmlformats.org/package/2006/relationships">
  <Relationship Id="rId1" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/slideLayout" Target="../slideLayouts/slideLayout1.xml"/>
  <Relationship Id="rId2" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/diagram.emf"/>
  <Relationship Id="rId3" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/fallback.png"/>
  <Relationship Id="rId4" Type="http://schemas.openxmlformats.org/officeDocument/2006/relationships/image" Target="../media/vector.svg"/>
</Relationships>
"""

    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", compression=zipfile.ZIP_DEFLATED) as zf:
        zf.writestr("[Content_Types].xml", content_types)
        zf.writestr("_rels/.rels", root_rels)
        zf.writestr("ppt/presentation.xml", pres_xml)
        zf.writestr("ppt/_rels/presentation.xml.rels", pres_rels)
        zf.writestr("ppt/slideMasters/slideMaster1.xml", SLIDE_MASTER_XML)
        zf.writestr("ppt/slideMasters/_rels/slideMaster1.xml.rels", master_rels)
        zf.writestr("ppt/slideLayouts/slideLayout1.xml", SLIDE_LAYOUT_XML)
        zf.writestr("ppt/slideLayouts/_rels/slideLayout1.xml.rels", layout_rels)
        zf.writestr("ppt/theme/theme1.xml", THEME_XML)
        zf.writestr("ppt/tableStyles.xml", TABLE_STYLES_EMPTY_XML)
        zf.writestr("ppt/charts/chart1.xml", PIE_CHART_XML)
        zf.writestr("ppt/charts/chart2.xml", DOUGHNUT_CHART_XML)
        zf.writestr("ppt/media/diagram.emf", _make_unsupported_emf_bytes())
        zf.writestr("ppt/media/fallback.png", _make_fallback_png())
        zf.writestr("ppt/media/vector.svg", _make_svg_blip_bytes())
        for i, xml in enumerate(slides, 1):
            zf.writestr(f"ppt/slides/slide{i}.xml", xml)
            rels = (
                chart_slide_rels
                if i == 4
                else emf_slide_rels
                if i == 5
                else plain_slide_rels
            )
            zf.writestr(f"ppt/slides/_rels/slide{i}.xml.rels", rels)
    return path


def render_fidelity_pngs(
    out_dir: Path,
    *,
    prefix: str,
    zoom: float = 1.5,
) -> list[Path]:
    out_dir.mkdir(parents=True, exist_ok=True)
    deck_path = out_dir / "fidelity-gaps.pptx"
    build_fidelity_deck(deck_path)
    written: list[Path] = []
    with docs.open(deck_path, zoom=zoom) as pic:
        for idx, slug in enumerate(SLIDE_NAMES):
            png_path = out_dir / f"{prefix}-{idx + 1}-{slug}.png"
            pic.pages[idx].save(png_path, format="PNG")
            written.append(png_path)
    return written


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--prefix",
        default="after",
        help="Filename prefix for rendered PNGs (e.g. 'before' or 'after').",
    )
    parser.add_argument(
        "--out-dir",
        type=Path,
        default=Path("/workspace/beta-tasks/pptx-fidelity-png"),
        help="Directory to save rendered PNGs and the generated deck.",
    )
    parser.add_argument("--zoom", type=float, default=1.5)
    args = parser.parse_args()

    paths = render_fidelity_pngs(args.out_dir, prefix=args.prefix, zoom=args.zoom)
    for p in paths:
        print(p)


if __name__ == "__main__":
    main()

# Local `pptx-glimpse` Fidelity Patches

Ordered unified diffs applied to `tools/pptx-glimpse/node_modules/` (`pptx-glimpse@5.3.1` and `@pptx-glimpse/document@0.16.0`) by `tools/pptx-glimpse/build.mjs` before bundling `src/pixcell/docs/vendor/pptx-glimpse.global.min.js`.

Each patch is self-contained, permissive (MIT-compatible), and structured so it can be ported to upstream TypeScript sources cleanly.

## Patch Index

### `01-shape-style-fontref.patch`
- **Gap**: Autoshape text rendered `#000000` black because `<p:style><a:fontRef>` was ignored during shape style parsing, allowing `<p:txStyles><p:otherStyle>` (`tx1`) to win.
- **Packages**: `@pptx-glimpse/document`, `pptx-glimpse` (renderer)
- **Changes**:
  - Parse `<a:fontRef idx="major|minor|none">` and its child colour (`schemeClr`, `srgbClr`, `sysClr`, `prstClr` with `lumMod`/`lumOff`/`satMod`/`satOff`/`tint`/`shade`/`alpha` transforms) in `parseShapeStyle`.
  - Insert `shape.style.fontRef` (with layout/master placeholder fallback) into `buildTextStyleChain` after `listStyle` and before `master.txStyles` / `presentation.defaultTextStyle`, so run/paragraph colours still override `fontRef`, `fontRef` overrides `otherStyle`'s `tx1`, and `otherStyle` still supplies default `fontSize`.
  - Support `<a:buClrTx/>` and default paragraph bullet colour inheritance from the first text run when `<a:buClr>` is omitted.

### `02-bullet-sizing.patch`
- **Gap**: Bullet `<tspan>` elements in native `<text>` mode omitted `font-size` unless `buSzPct` was explicitly set, causing SVG rasterisers to fall back to the default `12pt`/`16px` size; `buSzPts` and `buSzTx` were unparsed; Wingdings/Symbol PUA bullets rendered as missing/fallback glyphs without embedded symbol fonts.
- **Packages**: `@pptx-glimpse/document`, `pptx-glimpse` (adapter + text renderer)
- **Changes**:
  - Parse `<a:buSzPts val="..."/>` (1/100 pt) and `<a:buSzTx/>` alongside `<a:buSzPct/>`, preserving OOXML `EG_TextBulletSize` mutual exclusion across paragraph style inheritance and resolving theme font placeholders (`+mj-lt`, `+mn-lt`) on `<a:buFont>`.
  - Pass `bulletSizePts` through the computed-view renderer adapter and always emit `font-size` on bullet `<tspan>`s (`bulletSizePts * fontScale`, `textFontSizePt * (bulletSizePct / 100000)`, or `textFontSizePt` by default) in both native `<text>` and glyph-path modes.
  - Map common Private Use Area (`U+F000..U+F0FF`) Wingdings/Symbol bullet codepoints (`•`, `▪`, `■`, `□`, `❖`, `➢`, `✓`, `✗`) to Unicode in native `<text>` mode.

### `03-builtin-table-styles.patch`
- **Gap**: Tables referencing built-in OOXML `tableStyleId` GUIDs (such as `{5C22544A-7EE6-4342-B048-85BDC9FD1C3A}`, *Medium Style 2 – Accent 1*) without inline definitions in `ppt/tableStyles.xml` fell back to plain `1pt #000000` grid borders with no fills or header text styling.
- **Packages**: `@pptx-glimpse/document`, `pptx-glimpse` (table renderer)
- **Changes**:
  - Parse `<a:tblPr>` style flags (`firstRow`, `lastRow`, `firstCol`, `lastCol`, `bandRow`, `bandCol`), inline `<a:tblPr><a:tableStyle>`, and package-level `ppt/tableStyles.xml` definitions.
  - Provide a compact theme-resolved built-in OOXML table style catalog covering *No Style*, *Table Grid*, *Themed Style 1–2*, *Light Style 1–3*, *Medium Style 1–4*, and *Dark Style 1–2* (base + *Accent 1–6* GUIDs per `[MS-OE376]`).
  - Cascade `wholeTbl`, `band1V`/`band2V`, `band1H`/`band2H`, `firstCol`/`lastCol`, `firstRow`/`lastRow`, and corner regions for cell fills, outer vs. `insideH`/`insideV` borders, and `tcTxStyle` (`bold`, `italic`, `color`, `fontRef`).
  - Render table fills, width-ordered borders, and text in separate SVG passes so adjacent row fills do not paint over emphasis borders.

### `04-pie-doughnut-data-labels.patch`
- **Gap**: Pie and doughnut charts ignored `<c:dLbls>` / `<c:dLbl>` and rendered no slice data labels.
- **Packages**: `@pptx-glimpse/document`, `pptx-glimpse` (adapter + chart renderer)
- **Changes**:
  - Parse chart-group and series `<c:dLbls>` plus per-point `<c:dLbl>` overrides (`showVal`, `showPercent`, `showCatName`, `showSerName`, `dLblPos`, `separator`, `delete`, custom `<c:tx>`).
  - Render SVG `<text>` labels on pie and doughnut slices with automatic inside/outside placement (`outEnd` and narrow slices placed outside with adjusted radius; inside labels given luminance-contrasted `#FFFFFF` / `#262626` fill).

### `05-metafile-fallbacks-and-placeholder.patch`
- **Gap**: Standard `image/emf` / `image/wmf` / `application/x-msmetafile` content types were either excluded from media parts or mis-normalised to `image/png`, and `<mc:AlternateContent>` / `<a:blip>` raster or SVG alternates were not used when the primary blip was an EMF/WMF.
- **Packages**: `@pptx-glimpse/document`, `pptx-glimpse` (adapter + metafile/image renderer)
- **Changes**:
  - Recognise `image/emf`, `image/wmf`, and `application/x-msmetafile` (plus `.emf`/`.wmf`/`.svg` extension fallbacks) as package media and preserve `image/emf` / `image/wmf` in `normalizeImageMimeType`.
  - Extract alternate blip relationships (`asvg:svgBlip` in `a:blip/a:extLst` and `<mc:AlternateContent>` `Choice`/`Fallback` branches) and prefer non-metafile raster/SVG media when the primary relationship is unresolved or an EMF/WMF.
  - Allow benign `EMR_COMMENT` (`70`) records during EMF validation and guard inline metafile SVG rendering so unsupported metafiles cleanly emit a per-picture `[EMF]`/`[WMF]` placeholder while rendering the rest of the slide.

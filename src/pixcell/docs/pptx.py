"""PPTX renderer using bundled ``pptx-glimpse`` in QuickJS + ``resvg-py``.

``quickjs-ng`` (MIT) and ``resvg-py`` (MIT / MPL-2.0) live behind the ``pixcell[pptx]``
optional extra and are imported lazily only when a ``.pptx`` file is opened.

Conversion architecture
-----------------------
1. Slide markup is converted to per-slide SVG inside a module-level cached
   ``quickjs.Context`` (serialised by ``_JS_LOCK``) running the vendored browser bundle
   of ``pptx-glimpse`` in no-fonts mode (built-in font metrics tables + native ``<text>``).
2. PPTX bytes cross the Python/QuickJS boundary as base64 strings decoded with QuickJS's
   native ``atob()``. Because ``convertPptxToSvg`` returns a ``Promise``, pending jobs are
   drained with ``ctx.execute_pending_job()`` until settled.
3. The returned :class:`~pixcell.docs.Picture` keeps every slide's SVG on
   :class:`PptxPages` (accessible via :meth:`PptxPages.slide_svg`,
   :meth:`PptxPages.slide_document`, and :attr:`PptxPages.svgs`) and rasterises each page
   on demand via :func:`pixcell.docs.svg.rasterize_image` with ``dpi=96.0`` (required
   because ``resvg-py`` defaults to ``dpi=0``, which drops ``pt``-sized slide text) and
   ``font_family="Carlito"`` (which falls back cleanly when Carlito is unavailable).
4. If whole-deck conversion fails (for example, an EMF/WMF picture hitting
   ``document.createElementNS`` without a browser DOM), conversion retries slide-by-slide
   and substitutes a clear placeholder SVG for any failing slide instead of failing the
   whole presentation.
"""

from __future__ import annotations

import base64
import html
import importlib.resources
import json
import logging
import threading
import xml.etree.ElementTree as ET
import zipfile
from collections.abc import Sequence
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pixcell.docs import svg as svg_mod
from pixcell.docs.errors import DocsError, DocumentTooLarge, MissingExtra
from pixcell.docs.types import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_PAGES,
    SVG,
    LazyPages,
    Picture,
)

if TYPE_CHECKING:
    from PIL.Image import Image

logger = logging.getLogger(__name__)

DEFAULT_DPI = svg_mod.DEFAULT_DPI
"""DPI passed to ``resvg-py`` (``96.0``; default ``0`` drops ``pt``-sized slide text)."""

DEFAULT_ZOOM = svg_mod.DEFAULT_ZOOM
"""Default rasterisation scale for slide SVGs."""

DEFAULT_BACKGROUND = "#ffffff"
"""Default slide canvas background colour."""

DEFAULT_FONT_FAMILY = "Carlito"
"""Default font family passed to ``resvg-py`` (metric-compatible with Calibri; falls back cleanly)."""

DEFAULT_SANS_SERIF_FAMILY = svg_mod.DEFAULT_FONT_FAMILY
"""Default generic ``sans-serif`` family passed to ``resvg-py``."""

_DEFAULT_SLIDE_WIDTH = 960.0
_DEFAULT_SLIDE_HEIGHT = 540.0
_EMU_PER_INCH = 914_400.0
_DC_TITLE_TAG = "{http://purl.org/dc/elements/1.1/}title"
_JS_MAX_STACK_BYTES = 8 * 1024 * 1024

_JS_LOCK = threading.Lock()
_JS_CTX: Any | None = None
_BUNDLE_SOURCE: str | None = None


class _PptxConversionError(DocsError):
    """Internal error raised when pptx-glimpse rejects a conversion."""


def _import_quickjs() -> Any:
    try:
        import quickjs
    except ImportError as exc:
        raise MissingExtra(
            "pptx",
            renderer="pptx",
            dependency=getattr(exc, "name", None) or "quickjs",
        ) from exc
    return quickjs


def _import_resvg() -> Any:
    try:
        import resvg_py
    except ImportError as exc:
        raise MissingExtra(
            "pptx",
            renderer="pptx",
            dependency=getattr(exc, "name", None) or "resvg_py",
        ) from exc
    return resvg_py


def _load_bundle_source() -> str:
    global _BUNDLE_SOURCE
    if _BUNDLE_SOURCE is None:
        resource = importlib.resources.files(__package__).joinpath(
            "vendor", "pptx-glimpse.global.min.js"
        )
        _BUNDLE_SOURCE = resource.read_text(encoding="utf-8")
    return _BUNDLE_SOURCE


def _get_context_locked() -> Any:
    """Return the cached ``quickjs.Context`` (caller must hold ``_JS_LOCK``)."""
    global _JS_CTX
    if _JS_CTX is None:
        quickjs = _import_quickjs()
        ctx = quickjs.Context()
        ctx.set_max_stack_size(_JS_MAX_STACK_BYTES)
        ctx.eval("var globalThis = this;")
        ctx.eval(_load_bundle_source())
        _JS_CTX = ctx
    return _JS_CTX


def _convert_in_js(pptx_b64: str, options: dict[str, Any]) -> dict[str, Any]:
    """Run ``pptxGlimpse.start(...)`` in the cached QuickJS context and return the parsed payload."""
    global _JS_CTX
    with _JS_LOCK:
        ctx = _get_context_locked()
        try:
            ctx.set("__pptx", pptx_b64)
            ctx.set("__opts", json.dumps(options))
            ctx.set("__fonts", "[]")
            ctx.eval("pptxGlimpse.start(__pptx, __opts, JSON.parse(__fonts));")
            while ctx.execute_pending_job():
                pass
            raw = ctx.eval("__result")
        except Exception as exc:
            # Reset the cached context if the JS runtime itself threw so future calls start clean.
            _JS_CTX = None
            raise _PptxConversionError(f"QuickJS execution failed: {exc}") from exc
        finally:
            if _JS_CTX is not None:
                try:
                    ctx.set("__pptx", "")
                    ctx.set("__result", None)
                except Exception:
                    _JS_CTX = None

    if raw is None:
        raise _PptxConversionError("pptx-glimpse promise did not settle after draining jobs")

    try:
        result = json.loads(raw)
    except (TypeError, ValueError) as exc:
        raise _PptxConversionError(f"Invalid JSON result from pptx-glimpse: {exc}") from exc

    if not isinstance(result, dict) or not result.get("ok"):
        error_msg = result.get("error") if isinstance(result, dict) else str(result)
        raise _PptxConversionError(str(error_msg or "Unknown pptx-glimpse conversion error"))
    return result


def _inspect_pptx_archive(
    path: Path,
    *,
    max_bytes: int | None,
) -> tuple[int, str, float, float]:
    """Validate archive structure/size and return ``(slide_count, title, width_px, height_px)``."""
    try:
        with zipfile.ZipFile(path, "r") as archive:
            if max_bytes is not None:
                total_uncompressed = sum(max(0, info.file_size) for info in archive.infolist())
                if total_uncompressed > max_bytes:
                    raise DocumentTooLarge(path, size=total_uncompressed, max_bytes=max_bytes)

            names = set(archive.namelist())
            if "ppt/presentation.xml" not in names:
                raise DocsError(f"Invalid PPTX archive {path}: missing ppt/presentation.xml")

            slide_count = 0
            width_px = _DEFAULT_SLIDE_WIDTH
            height_px = _DEFAULT_SLIDE_HEIGHT
            try:
                pres_root = ET.fromstring(archive.read("ppt/presentation.xml"))
            except ET.ParseError as exc:
                raise DocsError(f"Invalid ppt/presentation.xml in {path}: {exc}") from exc

            for elem in pres_root.iter():
                local = elem.tag.rsplit("}", 1)[-1]
                if local == "sldId":
                    slide_count += 1
                elif local == "sldSz":
                    try:
                        cx = int(elem.attrib.get("cx", "0"))
                        cy = int(elem.attrib.get("cy", "0"))
                        if cx > 0 and cy > 0:
                            width_px = cx / _EMU_PER_INCH * 96.0
                            height_px = cy / _EMU_PER_INCH * 96.0
                    except ValueError:
                        pass

            title = ""
            if "docProps/core.xml" in names:
                info = archive.getinfo("docProps/core.xml")
                if info.file_size <= 1_048_576:
                    try:
                        core_root = ET.fromstring(archive.read("docProps/core.xml"))
                        for elem in core_root.iter():
                            if (
                                elem.tag == _DC_TITLE_TAG
                                or elem.tag.rsplit("}", 1)[-1].lower() == "title"
                            ):
                                text = "".join(elem.itertext()).strip()
                                if text:
                                    title = text
                                    break
                    except ET.ParseError:
                        pass
    except zipfile.BadZipFile as exc:
        raise DocsError(f"Invalid PPTX archive {path}: {exc}") from exc

    return slide_count, title, width_px, height_px


def _placeholder_svg(
    slide_number: int,
    error: str,
    *,
    width: float = _DEFAULT_SLIDE_WIDTH,
    height: float = _DEFAULT_SLIDE_HEIGHT,
) -> str:
    """Build a self-contained SVG error card for a slide that failed to render."""
    w = max(320, int(round(width)))
    h = max(180, int(round(height)))
    first_line = (error.strip().splitlines() or ["Rendering error"])[0]
    if len(first_line) > 120:
        first_line = first_line[:117] + "..."
    escaped_error = html.escape(first_line)
    title_text = f"Slide {slide_number} could not be rendered"
    pad = 36
    card_w = w - pad * 2
    card_h = h - pad * 2
    cx = w // 2
    title_y = h // 2 - 12
    detail_y = h // 2 + 24
    return (
        f'<svg xmlns="http://www.w3.org/2000/svg" width="{w}" height="{h}" viewBox="0 0 {w} {h}">'
        f"<title>{html.escape(title_text)}</title>"
        f'<rect width="{w}" height="{h}" fill="#f5f5f5"/>'
        f'<rect x="{pad}" y="{pad}" width="{card_w}" height="{card_h}" rx="12" '
        f'fill="#ffffff" stroke="#c8c8c8" stroke-width="2"/>'
        f'<text x="{cx}" y="{title_y}" text-anchor="middle" '
        f'font-family="Carlito, Arial, sans-serif" font-size="28" font-weight="bold" fill="#222222">'
        f"{html.escape(title_text)}</text>"
        f'<text x="{cx}" y="{detail_y}" text-anchor="middle" '
        f'font-family="Carlito, Arial, sans-serif" font-size="16" fill="#666666">'
        f"{escaped_error}</text>"
        "</svg>"
    )


def _log_diagnostics(path: Path, diagnostics: Sequence[dict[str, Any]]) -> None:
    for diag in diagnostics:
        if not isinstance(diag, dict):
            continue
        severity = diag.get("severity", "info")
        code = diag.get("code", "")
        message = diag.get("message", "")
        slide_no = diag.get("slideNumber")
        prefix = f"slide {slide_no} " if slide_no is not None else ""
        if severity == "error":
            logger.warning("pptx-glimpse (%s %s%s): %s", path, prefix, code, message)
        elif severity == "warning":
            logger.debug("pptx-glimpse (%s %s%s): %s", path, prefix, code, message)


def _convert_slides(
    pptx_b64: str,
    *,
    path: Path,
    slide_numbers: Sequence[int],
    slide_width: float,
    slide_height: float,
) -> tuple[list[str], dict[int, str]]:
    """Convert ``slide_numbers`` (1-based) to SVG strings, isolating per-slide failures."""
    if not slide_numbers:
        return [], {}

    errors: dict[int, str] = {}
    base_opts: dict[str, Any] = {"logLevel": "off"}

    try:
        payload = _convert_in_js(pptx_b64, {**base_opts, "slides": list(slide_numbers)})
    except _PptxConversionError as deck_exc:
        if len(slide_numbers) == 1:
            msg = str(deck_exc)
            logger.warning("Failed to convert PPTX slide %d (%s): %s", slide_numbers[0], path, msg)
            errors[0] = msg
            return [
                _placeholder_svg(slide_numbers[0], msg, width=slide_width, height=slide_height)
            ], errors

        logger.warning(
            "Batch PPTX conversion failed for %s (%s); retrying slide-by-slide",
            path,
            deck_exc,
        )
        svgs: list[str] = []
        for idx, slide_num in enumerate(slide_numbers):
            try:
                single = _convert_in_js(pptx_b64, {**base_opts, "slides": [slide_num]})
                _log_diagnostics(path, single.get("diagnostics") or ())
                slides_out = single.get("slides") or ()
                matched = next(
                    (s.get("svg") for s in slides_out if isinstance(s, dict) and s.get("svg")),
                    None,
                )
                if matched is None:
                    raise _PptxConversionError(f"Slide {slide_num} produced no SVG output")
                svgs.append(str(matched))
            except _PptxConversionError as slide_exc:
                msg = str(slide_exc)
                logger.warning("Failed to convert PPTX slide %d (%s): %s", slide_num, path, msg)
                errors[idx] = msg
                svgs.append(
                    _placeholder_svg(slide_num, msg, width=slide_width, height=slide_height)
                )
        return svgs, errors

    _log_diagnostics(path, payload.get("diagnostics") or ())
    by_number: dict[int, str] = {}
    for entry in payload.get("slides") or ():
        if isinstance(entry, dict) and "slideNumber" in entry and entry.get("svg"):
            by_number[int(entry["slideNumber"])] = str(entry["svg"])

    svgs = []
    for idx, slide_num in enumerate(slide_numbers):
        svg_text = by_number.get(slide_num)
        if svg_text is not None:
            svgs.append(svg_text)
        else:
            msg = f"Slide {slide_num} produced no SVG output"
            logger.warning("PPTX %s: %s", path, msg)
            errors[idx] = msg
            svgs.append(_placeholder_svg(slide_num, msg, width=slide_width, height=slide_height))
    return svgs, errors


class PptxPages(LazyPages):
    """A :class:`LazyPages` sequence of PPTX slides backed by per-slide SVG strings.

    Each slide's SVG is retained and rasterised to a Pillow image via ``resvg-py`` only
    when indexed. Call :meth:`slide_svg`, :meth:`slide_document`, or :attr:`svgs` to
    access the underlying SVG markup directly.
    """

    def __init__(
        self,
        svgs: Sequence[str],
        *,
        source: Path | None = None,
        title: str = "",
        zoom: float = DEFAULT_ZOOM,
        dpi: float = DEFAULT_DPI,
        background: str | None = DEFAULT_BACKGROUND,
        font_family: str | None = DEFAULT_FONT_FAMILY,
        sans_serif_family: str | None = DEFAULT_SANS_SERIF_FAMILY,
        cache: bool = True,
        slide_errors: dict[int, str] | None = None,
        slide_width: float = _DEFAULT_SLIDE_WIDTH,
        slide_height: float = _DEFAULT_SLIDE_HEIGHT,
    ) -> None:
        self._svgs: tuple[str, ...] = tuple(svgs)
        self._source = source
        self._title = title
        self._zoom = float(zoom)
        self._dpi = float(dpi)
        self._background = background
        self._font_family = font_family
        self._sans_serif_family = sans_serif_family
        self._slide_errors: dict[int, str] = dict(slide_errors) if slide_errors else {}
        self._slide_width = float(slide_width)
        self._slide_height = float(slide_height)
        super().__init__(len(self._svgs), self._rasterize_slide, cache=cache)

    def _normalize_index(self, index: int) -> int:
        if index < 0:
            index += self._count
        if not 0 <= index < self._count:
            raise IndexError(f"page index {index} out of range (0..{self._count - 1})")
        return index

    def _rasterize_slide(self, index: int) -> Image:
        markup = self._svgs[index]
        try:
            return svg_mod.rasterize_image(
                markup,
                zoom=self._zoom,
                dpi=self._dpi,
                background=self._background,
                font_family=self._font_family,
                sans_serif_family=self._sans_serif_family,
            )
        except MissingExtra:
            raise
        except Exception as exc:
            msg = str(exc)
            logger.warning(
                "Failed to rasterise PPTX slide %d (%s): %s",
                index + 1,
                self._source,
                msg,
            )
            self._slide_errors[index] = msg
            fallback = _placeholder_svg(
                index + 1,
                msg,
                width=self._slide_width,
                height=self._slide_height,
            )
            return svg_mod.rasterize_image(
                fallback,
                zoom=self._zoom,
                dpi=self._dpi,
                background=self._background,
                font_family=self._font_family,
                sans_serif_family=self._sans_serif_family,
            )

    @property
    def svgs(self) -> tuple[str, ...]:
        """Tuple of raw SVG strings for every exposed slide."""
        return self._svgs

    @property
    def slide_errors(self) -> dict[int, str]:
        """Mapping of 0-based slide indices to error messages for slides that used a placeholder."""
        return dict(self._slide_errors)

    def slide_svg(self, index: int) -> str:
        """Return the raw SVG markup for slide ``index`` (0-based; supports negative indexing)."""
        return self._svgs[self._normalize_index(index)]

    def slide_document(self, index: int) -> SVG:
        """Return slide ``index`` wrapped in an :class:`~pixcell.docs.SVG` document."""
        idx = self._normalize_index(index)
        markup = self._svgs[idx]
        width, height, svg_title = svg_mod.parse_svg_info(markup)
        slide_title = svg_title or (
            f"{self._title} - Slide {idx + 1}" if self._title else f"Slide {idx + 1}"
        )
        return SVG(
            svg=markup,
            width=width,
            height=height,
            title=slide_title,
            source=self._source,
        )

    def capped(self, max_pages: int) -> PptxPages:
        """Return a view limited to at most ``max_pages`` without rasterising any slide."""
        if max_pages < 0:
            raise ValueError("max_pages must be non-negative")
        if self._count <= max_pages:
            return self
        capped_errors = {k: v for k, v in self._slide_errors.items() if k < max_pages}
        return PptxPages(
            self._svgs[:max_pages],
            source=self._source,
            title=self._title,
            zoom=self._zoom,
            dpi=self._dpi,
            background=self._background,
            font_family=self._font_family,
            sans_serif_family=self._sans_serif_family,
            cache=self._cache is not None,
            slide_errors=capped_errors,
            slide_width=self._slide_width,
            slide_height=self._slide_height,
        )


def render(
    path: Path,
    *,
    zoom: float = DEFAULT_ZOOM,
    dpi: float = DEFAULT_DPI,
    background: str | None = DEFAULT_BACKGROUND,
    font_family: str | None = DEFAULT_FONT_FAMILY,
    sans_serif_family: str | None = DEFAULT_SANS_SERIF_FAMILY,
    max_bytes: int | None = DEFAULT_MAX_BYTES,
    max_pages: int | None = DEFAULT_MAX_PAGES,
    cache: bool = True,
) -> Picture:
    """Open a ``.pptx`` file and return a :class:`Picture` with lazy SVG-backed pages.

    Parameters
    ----------
    path:
        Path to the PowerPoint ``.pptx`` file.
    zoom:
        Scale factor passed to ``resvg-py`` when rasterising slides on demand (default ``2.0``).
    dpi:
        DPI passed to ``resvg-py`` (default ``96.0``; must be positive).
    background:
        Background colour passed to ``resvg-py`` (default ``"#ffffff"``).
    font_family:
        Default font family passed to ``resvg-py`` (default ``"Carlito"``, which falls back
        cleanly if Carlito is not installed).
    sans_serif_family:
        Generic ``sans-serif`` family passed to ``resvg-py`` (default ``"Arial"``).
    max_bytes:
        Maximum file size and uncompressed OOXML archive size in bytes (``None`` to disable).
    max_pages:
        Maximum number of slides converted and exposed (``None`` for all slides).
    cache:
        If ``True`` (default), retain rasterised Pillow slide images in memory on
        :class:`PptxPages`.
    """
    if zoom <= 0:
        raise ValueError("zoom must be positive")
    if dpi <= 0:
        raise ValueError("dpi must be positive")
    if max_bytes is not None and max_bytes < 0:
        raise ValueError("max_bytes must be non-negative or None")
    if max_pages is not None and max_pages < 1:
        raise ValueError("max_pages must be at least 1 or None")

    # Validate both optional deps eagerly so MissingExtra("pptx") surfaces on open().
    _import_quickjs()
    _import_resvg()

    file_size = path.stat().st_size
    if max_bytes is not None and file_size > max_bytes:
        raise DocumentTooLarge(path, size=file_size, max_bytes=max_bytes)

    total_pages, title, slide_w, slide_h = _inspect_pptx_archive(path, max_bytes=max_bytes)

    if total_pages == 0:
        return Picture(
            pages=PptxPages(
                (),
                source=path,
                title=title,
                zoom=zoom,
                dpi=dpi,
                background=background,
                font_family=font_family,
                sans_serif_family=sans_serif_family,
                cache=cache,
                slide_width=slide_w,
                slide_height=slide_h,
            ),
            title=title,
            source=path,
            total_pages=0,
        )

    count = min(total_pages, max_pages) if max_pages is not None else total_pages
    pptx_b64 = base64.b64encode(path.read_bytes()).decode("ascii")
    svgs, slide_errors = _convert_slides(
        pptx_b64,
        path=path,
        slide_numbers=range(1, count + 1),
        slide_width=slide_w,
        slide_height=slide_h,
    )

    pages = PptxPages(
        svgs,
        source=path,
        title=title,
        zoom=zoom,
        dpi=dpi,
        background=background,
        font_family=font_family,
        sans_serif_family=sans_serif_family,
        cache=cache,
        slide_errors=slide_errors,
        slide_width=slide_w,
        slide_height=slide_h,
    )
    return Picture(
        pages=pages,
        title=title,
        source=path,
        total_pages=total_pages,
    )

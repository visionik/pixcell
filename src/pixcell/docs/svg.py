"""SVG renderer and shared resvg-py rasterisation helpers for :mod:`pixcell.docs`.

Other SVG-producing renderers (Mermaid, PPTX, Excalidraw) reuse :func:`rasterize`,
:func:`rasterize_image`, and :func:`to_picture` here so the resvg-py defaults (notably
``dpi=96.0`` and a sensible sans-serif font family) live in one place.
"""

from __future__ import annotations

import io
import os
import re
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import TYPE_CHECKING

from pixcell.docs.errors import MissingExtra
from pixcell.docs.types import SVG, LazyPages, Picture

if TYPE_CHECKING:
    from PIL.Image import Image

DEFAULT_DPI = 96.0
"""DPI passed to resvg-py. resvg-py defaults to 0, which silently drops text."""

DEFAULT_ZOOM = 2.0
"""Default rasterisation scale (pixels per SVG unit) for crisp terminal rendering."""

DEFAULT_FONT_FAMILY = "Arial"
"""Default sans-serif family passed to resvg-py."""

_LENGTH_RE = re.compile(r"^\s*([+-]?(?:\d+\.?\d*|\.\d+))\s*([a-z%]*)\s*$", re.IGNORECASE)
_UNIT_TO_PX: dict[str, float] = {
    "": 1.0,
    "px": 1.0,
    "pt": 96.0 / 72.0,
    "pc": 16.0,
    "in": 96.0,
    "cm": 96.0 / 2.54,
    "mm": 96.0 / 25.4,
}


def _import_resvg():
    try:
        import resvg_py
    except ImportError as exc:
        raise MissingExtra(
            "svg",
            renderer="svg",
            dependency=getattr(exc, "name", None) or "resvg_py",
        ) from exc
    return resvg_py


def _parse_length(raw: str | None) -> float | None:
    if not raw:
        return None
    match = _LENGTH_RE.match(raw)
    if not match:
        return None
    value = float(match.group(1))
    unit = match.group(2).lower()
    factor = _UNIT_TO_PX.get(unit)
    if factor is None or value <= 0:
        return None
    return value * factor


def _parse_viewbox(raw: str | None) -> tuple[float | None, float | None]:
    if not raw:
        return None, None
    parts = [part for part in re.split(r"[\s,]+", raw.strip()) if part]
    if len(parts) != 4:
        return None, None
    try:
        width, height = float(parts[2]), float(parts[3])
    except ValueError:
        return None, None
    if width <= 0 or height <= 0:
        return None, None
    return width, height


def parse_svg_info(svg: str) -> tuple[float | None, float | None, str]:
    """Extract ``(width, height, title)`` from an SVG string without external dependencies."""
    try:
        root = ET.fromstring(svg)
    except ET.ParseError:
        return None, None, ""
    tag = root.tag.rsplit("}", 1)[-1].lower()
    if tag != "svg":
        return None, None, ""

    width = _parse_length(root.attrib.get("width"))
    height = _parse_length(root.attrib.get("height"))
    if width is None or height is None:
        vb_width, vb_height = _parse_viewbox(root.attrib.get("viewBox") or root.attrib.get("viewbox"))
        if width is None:
            width = vb_width
        if height is None:
            height = vb_height

    title = ""
    for child in root:
        if child.tag.rsplit("}", 1)[-1].lower() == "title" and child.text:
            title = child.text.strip()
            break
    return width, height, title


def rasterize(
    svg: str,
    *,
    zoom: float = DEFAULT_ZOOM,
    dpi: float = DEFAULT_DPI,
    background: str | None = None,
    font_family: str | None = None,
    sans_serif_family: str | None = None,
    width: int | None = None,
    height: int | None = None,
    resources_dir: str | os.PathLike[str] | None = None,
) -> bytes:
    """Rasterise an SVG string to PNG bytes via ``resvg-py``.

    Always passes a positive ``dpi`` (default ``96.0``, avoiding resvg-py's ``dpi=0``
    default that drops text) and a default ``sans_serif_family`` (``"Arial"`` unless
    overridden via ``font_family`` or ``sans_serif_family``).
    """
    if dpi <= 0:
        raise ValueError("dpi must be positive")
    if zoom <= 0:
        raise ValueError("zoom must be positive")

    resvg_py = _import_resvg()
    kwargs: dict[str, object] = {
        "svg_string": svg,
        "dpi": float(dpi),
        "zoom": float(zoom),
        "sans_serif_family": sans_serif_family or font_family or DEFAULT_FONT_FAMILY,
    }
    if font_family is not None:
        kwargs["font_family"] = font_family
    if background is not None:
        kwargs["background"] = background
    if width is not None:
        kwargs["width"] = width
    if height is not None:
        kwargs["height"] = height
    if resources_dir is not None:
        kwargs["resources_dir"] = os.fspath(resources_dir)
    return bytes(resvg_py.svg_to_bytes(**kwargs))


def rasterize_image(
    svg: str,
    *,
    zoom: float = DEFAULT_ZOOM,
    dpi: float = DEFAULT_DPI,
    background: str | None = None,
    font_family: str | None = None,
    sans_serif_family: str | None = None,
    width: int | None = None,
    height: int | None = None,
    resources_dir: str | os.PathLike[str] | None = None,
) -> Image:
    """Rasterise an SVG string and return a decoded Pillow image."""
    from PIL import Image as PILImage

    png = rasterize(
        svg,
        zoom=zoom,
        dpi=dpi,
        background=background,
        font_family=font_family,
        sans_serif_family=sans_serif_family,
        width=width,
        height=height,
        resources_dir=resources_dir,
    )
    image = PILImage.open(io.BytesIO(png))
    image.load()
    return image


def to_picture(
    svg: str | SVG,
    *,
    zoom: float = DEFAULT_ZOOM,
    dpi: float = DEFAULT_DPI,
    background: str | None = None,
    font_family: str | None = None,
    sans_serif_family: str | None = None,
    width: int | None = None,
    height: int | None = None,
    resources_dir: str | os.PathLike[str] | None = None,
) -> Picture:
    """Wrap an SVG string or :class:`SVG` document in a lazy 1-page :class:`Picture`."""
    # Validate resvg-py eagerly so MissingExtra surfaces immediately rather than on page access.
    _import_resvg()
    if isinstance(svg, SVG):
        markup = svg.svg
        title = svg.title
        source = svg.source
        if resources_dir is None and source is not None:
            resources_dir = source.parent
    else:
        markup = svg
        title = ""
        source = None

    return Picture(
        pages=LazyPages(
            1,
            lambda _: rasterize_image(
                markup,
                zoom=zoom,
                dpi=dpi,
                background=background,
                font_family=font_family,
                sans_serif_family=sans_serif_family,
                width=width,
                height=height,
                resources_dir=resources_dir,
            ),
            cache=True,
        ),
        title=title,
        source=source,
    )


def render(
    path: Path,
    *,
    rasterize: bool = False,
    zoom: float = DEFAULT_ZOOM,
    dpi: float = DEFAULT_DPI,
    background: str | None = None,
    font_family: str | None = None,
    sans_serif_family: str | None = None,
    width: int | None = None,
    height: int | None = None,
    resources_dir: str | os.PathLike[str] | None = None,
) -> SVG | Picture:
    """Open an ``.svg`` file as an :class:`SVG` (or a :class:`Picture` when ``rasterize=True``)."""
    markup = path.read_text(encoding="utf-8-sig")
    svg_width, svg_height, title = parse_svg_info(markup)
    document = SVG(
        svg=markup,
        width=svg_width,
        height=svg_height,
        title=title,
        source=path,
    )
    if rasterize:
        return to_picture(
            document,
            zoom=zoom,
            dpi=dpi,
            background=background,
            font_family=font_family,
            sans_serif_family=sans_serif_family,
            width=width,
            height=height,
            resources_dir=resources_dir if resources_dir is not None else path.parent,
        )
    return document

"""Excalidraw scene (JSON) to clean SVG renderer for :mod:`pixcell.docs`.

Converts Excalidraw scene files (``.excalidraw`` / ``.excalidraw.json`` /
``application/vnd.excalidraw+json``) directly to clean vector :class:`~pixcell.docs.SVG`
markup using only the Python standard library — no external dependencies are required to
produce SVG output.

Design note: this renderer emits crisp geometric SVG primitives rather than simulating
Excalidraw's rough.js hand-drawn wobble. Rasterising the resulting :class:`SVG` to a
:class:`~pixcell.docs.Picture` or PNG bytes reuses :mod:`pixcell.docs.svg` and requires
the ``pixcell[svg]`` extra (``resvg-py``).
"""

from __future__ import annotations

import json
import math
import os
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any
from xml.sax.saxutils import escape as _xml_escape

from pixcell.docs.errors import DocsError
from pixcell.docs.svg import DEFAULT_DPI, DEFAULT_ZOOM, to_picture
from pixcell.docs.types import SVG, Picture

DEFAULT_PADDING = 10.0
"""Default margin (in Excalidraw scene units) added around the computed bounding box."""

_DEFAULT_STROKE_COLOR = "#1e1e1e"
_DEFAULT_STROKE_WIDTH = 2.0
_DEFAULT_FONT_SIZE = 20.0
_DEFAULT_LINE_HEIGHT = 1.25
_ADAPTIVE_ROUNDNESS_CAP = 32.0
_TRANSPARENT_VALUES = frozenset({"", "transparent", "none"})

# Excalidraw numeric fontFamily codes -> generic stacks that resvg resolves reliably.
_FONT_FAMILIES: dict[int, str] = {
    1: "sans-serif",  # Virgil
    2: "sans-serif",  # Helvetica
    3: "monospace",  # Cascadia
    4: "sans-serif",  # Assistant
    5: "sans-serif",  # Excalifont
    6: "sans-serif",  # Nunito
    7: "sans-serif",  # Lilita One
    8: "monospace",  # Comic Shanns
}

_TEXT_ANCHORS: dict[str, str] = {
    "left": "start",
    "center": "middle",
    "right": "end",
}

BBox = tuple[float, float, float, float]


def _fmt(value: float) -> str:
    """Format a finite float compactly without scientific notation or ``-0``."""
    if not math.isfinite(value):
        return "0"
    if abs(value) < 1e-9:
        return "0"
    formatted = f"{value:.4f}".rstrip("0").rstrip(".")
    return "0" if formatted in ("", "-0") else formatted


def _escape_text(value: str) -> str:
    return _xml_escape(value)


def _escape_attr(value: str) -> str:
    return _xml_escape(value, {'"': "&quot;", "'": "&apos;"})


def _num(value: Any, default: float = 0.0) -> float:
    if isinstance(value, bool):
        return default
    if isinstance(value, (int, float)) and math.isfinite(value):
        return float(value)
    return default


def _is_transparent(color: Any) -> bool:
    if not isinstance(color, str):
        return True
    return color.strip().lower() in _TRANSPARENT_VALUES


def _clean_color(color: Any, default: str) -> str:
    if isinstance(color, str) and color.strip():
        return color.strip()
    return default


def _normalize_box(x: float, y: float, width: float, height: float) -> tuple[float, float, float, float]:
    if width < 0:
        x += width
        width = -width
    if height < 0:
        y += height
        height = -height
    return x, y, width, height


def _rotate_point(px: float, py: float, cx: float, cy: float, angle: float) -> tuple[float, float]:
    if abs(angle) <= 1e-9:
        return px, py
    cos_a = math.cos(angle)
    sin_a = math.sin(angle)
    dx = px - cx
    dy = py - cy
    return cx + dx * cos_a - dy * sin_a, cy + dx * sin_a + dy * cos_a


def _bbox_from_points(
    points: Sequence[tuple[float, float]],
    *,
    cx: float = 0.0,
    cy: float = 0.0,
    angle: float = 0.0,
    expand: float = 0.0,
) -> BBox | None:
    if not points:
        return None
    xs: list[float] = []
    ys: list[float] = []
    for px, py in points:
        rx, ry = _rotate_point(px, py, cx, cy, angle)
        xs.append(rx)
        ys.append(ry)
    return (min(xs) - expand, min(ys) - expand, max(xs) + expand, max(ys) + expand)


def _box_corners(x: float, y: float, width: float, height: float) -> list[tuple[float, float]]:
    return [
        (x, y),
        (x + width, y),
        (x + width, y + height),
        (x, y + height),
    ]


def _merge_bboxes(bboxes: Sequence[BBox]) -> BBox | None:
    if not bboxes:
        return None
    return (
        min(b[0] for b in bboxes),
        min(b[1] for b in bboxes),
        max(b[2] for b in bboxes),
        max(b[3] for b in bboxes),
    )


def _corner_radius(roundness: Any, width: float, height: float) -> float:
    if not roundness:
        return 0.0
    min_dim = min(width, height)
    if min_dim <= 0:
        return 0.0
    if isinstance(roundness, Mapping):
        if "value" in roundness and roundness["value"] is not None:
            return max(0.0, min(_num(roundness["value"]), min_dim / 2.0))
        rtype = roundness.get("type")
        if rtype == 3:
            return min(min_dim * 0.25, _ADAPTIVE_ROUNDNESS_CAP)
        return min_dim * 0.25
    if isinstance(roundness, (int, float)) and not isinstance(roundness, bool):
        return max(0.0, min(float(roundness), min_dim / 2.0))
    return min(min_dim * 0.25, _ADAPTIVE_ROUNDNESS_CAP)


def _is_round(element: Mapping[str, Any]) -> bool:
    if element.get("roundness") is not None:
        return True
    return element.get("strokeSharpness") == "round"


def _opacity_attr(element: Mapping[str, Any]) -> str:
    raw = _num(element.get("opacity"), 100.0)
    clamped = max(0.0, min(100.0, raw)) / 100.0
    if clamped >= 1.0 - 1e-6:
        return ""
    return f' opacity="{_fmt(clamped)}"'


def _rotate_attr(angle: float, cx: float, cy: float) -> str:
    if abs(angle) <= 1e-9:
        return ""
    deg = math.degrees(angle)
    return f' transform="rotate({_fmt(deg)} {_fmt(cx)} {_fmt(cy)})"'


def _stroke_attrs(element: Mapping[str, Any]) -> tuple[str, float]:
    stroke_color = _clean_color(element.get("strokeColor"), _DEFAULT_STROKE_COLOR)
    stroke_width = max(0.0, _num(element.get("strokeWidth"), _DEFAULT_STROKE_WIDTH))
    if _is_transparent(stroke_color) or stroke_width <= 0.0:
        return ' stroke="none"', 0.0

    parts = [
        f' stroke="{_escape_attr(stroke_color)}"',
        f' stroke-width="{_fmt(stroke_width)}"',
    ]
    stroke_style = str(element.get("strokeStyle") or "solid").strip().lower()
    if stroke_style == "dashed":
        dash = max(stroke_width * 4.0, 8.0)
        gap = max(stroke_width * 3.0, 6.0)
        parts.append(f' stroke-dasharray="{_fmt(dash)} {_fmt(gap)}"')
    elif stroke_style == "dotted":
        dot = max(stroke_width, 1.5)
        gap = max(stroke_width * 2.5, 4.0)
        parts.append(f' stroke-dasharray="{_fmt(dot)} {_fmt(gap)}"')
        parts.append(' stroke-linecap="round"')
    return "".join(parts), stroke_width


class _PatternRegistry:
    """Collects reusable SVG ``<pattern>`` definitions for hachure/cross-hatch fills."""

    def __init__(self) -> None:
        self._ids: dict[tuple[str, str, float], str] = {}
        self._defs: list[str] = []

    def fill_value(self, element: Mapping[str, Any]) -> str:
        bg = element.get("backgroundColor")
        if _is_transparent(bg):
            return "none"
        color = str(bg).strip()
        fill_style = str(element.get("fillStyle") or "solid").strip().lower()
        if fill_style not in ("hachure", "cross-hatch", "zigzag"):
            return _escape_attr(color)

        kind = "cross-hatch" if fill_style == "cross-hatch" else "hachure"
        stroke_width = max(1.0, _num(element.get("strokeWidth"), _DEFAULT_STROKE_WIDTH) * 0.75)
        step = max(8.0, stroke_width * 5.0)
        key = (kind, color, round(stroke_width, 3))
        pat_id = self._ids.get(key)
        if pat_id is None:
            pat_id = f"excalidraw-pat-{len(self._ids) + 1}"
            self._ids[key] = pat_id
            esc_color = _escape_attr(color)
            lines = [
                f'<line x1="0" y1="0" x2="0" y2="{_fmt(step)}" '
                f'stroke="{esc_color}" stroke-width="{_fmt(stroke_width)}"/>'
            ]
            if kind == "cross-hatch":
                lines.append(
                    f'<line x1="0" y1="0" x2="{_fmt(step)}" y2="0" '
                    f'stroke="{esc_color}" stroke-width="{_fmt(stroke_width)}"/>'
                )
            self._defs.append(
                f'    <pattern id="{pat_id}" width="{_fmt(step)}" height="{_fmt(step)}" '
                f'patternUnits="userSpaceOnUse" patternTransform="rotate(-45)">'
                + "".join(lines)
                + "</pattern>"
            )
        return f"url(#{pat_id})"

    def render_defs(self) -> str:
        if not self._defs:
            return ""
        return "  <defs>\n" + "\n".join(self._defs) + "\n  </defs>"


def _extract_points(element: Mapping[str, Any], x: float, y: float) -> list[tuple[float, float]]:
    raw_points = element.get("points")
    if not isinstance(raw_points, list):
        return []
    result: list[tuple[float, float]] = []
    for pt in raw_points:
        if isinstance(pt, (list, tuple)) and len(pt) >= 2:
            result.append((x + _num(pt[0]), y + _num(pt[1])))
    return result


def _points_closed(points: Sequence[tuple[float, float]]) -> bool:
    if len(points) < 3:
        return False
    dx = points[-1][0] - points[0][0]
    dy = points[-1][1] - points[0][1]
    return math.hypot(dx, dy) <= 1.0


def _polyline_path_d(
    points: Sequence[tuple[float, float]],
    *,
    curved: bool,
    closed: bool,
) -> str:
    if not points:
        return ""
    if len(points) == 1:
        x0, y0 = points[0]
        return f"M {_fmt(x0)} {_fmt(y0)}"
    if not curved or len(points) == 2:
        cmds = [f"M {_fmt(points[0][0])} {_fmt(points[0][1])}"]
        for px, py in points[1:]:
            cmds.append(f"L {_fmt(px)} {_fmt(py)}")
        if closed:
            cmds.append("Z")
        return " ".join(cmds)

    # Smooth quadratic spline through midpoints of consecutive segments.
    cmds = [f"M {_fmt(points[0][0])} {_fmt(points[0][1])}"]
    for idx in range(1, len(points) - 1):
        p_curr = points[idx]
        p_next = points[idx + 1]
        mid_x = (p_curr[0] + p_next[0]) / 2.0
        mid_y = (p_curr[1] + p_next[1]) / 2.0
        cmds.append(f"Q {_fmt(p_curr[0])} {_fmt(p_curr[1])} {_fmt(mid_x)} {_fmt(mid_y)}")
    cmds.append(f"L {_fmt(points[-1][0])} {_fmt(points[-1][1])}")
    if closed:
        cmds.append("Z")
    return " ".join(cmds)


def _render_arrowhead(
    tip: tuple[float, float],
    prev: tuple[float, float],
    kind: str,
    *,
    stroke_color: str,
    stroke_width: float,
    view_bg: str | None,
) -> tuple[str, list[tuple[float, float]]]:
    """Render an Excalidraw arrowhead as explicit SVG geometry at ``tip``."""
    dx = tip[0] - prev[0]
    dy = tip[1] - prev[1]
    seg_len = math.hypot(dx, dy)
    if seg_len <= 1e-6:
        return "", []

    ux = dx / seg_len
    uy = dy / seg_len
    px = -uy
    py = ux

    head_kind = kind.strip().lower()
    size = max(10.0, stroke_width * 4.5 + 6.0)
    if seg_len < size * 2.0:
        size = max(4.0, seg_len * 0.5)

    esc_stroke = _escape_attr(stroke_color)
    sw = _fmt(max(stroke_width, 1.0))
    outline_fill = _escape_attr(view_bg) if view_bg else "#ffffff"

    if head_kind in ("dot", "circle", "circle_outline"):
        r = max(4.0, stroke_width * 2.0)
        cx = tip[0] - ux * r
        cy = tip[1] - uy * r
        fill = outline_fill if head_kind == "circle_outline" else esc_stroke
        markup = (
            f'<circle cx="{_fmt(cx)}" cy="{_fmt(cy)}" r="{_fmt(r)}" '
            f'fill="{fill}" stroke="{esc_stroke}" stroke-width="{sw}"/>'
        )
        pts = [(cx - r, cy - r), (cx + r, cy + r)]
        return markup, pts

    if head_kind == "bar":
        half = size * 0.55
        x1, y1 = tip[0] + px * half, tip[1] + py * half
        x2, y2 = tip[0] - px * half, tip[1] - py * half
        markup = (
            f'<line x1="{_fmt(x1)}" y1="{_fmt(y1)}" x2="{_fmt(x2)}" y2="{_fmt(y2)}" '
            f'stroke="{esc_stroke}" stroke-width="{sw}" stroke-linecap="round"/>'
        )
        return markup, [(x1, y1), (x2, y2)]

    if head_kind in ("diamond", "diamond_outline"):
        half_len = size * 0.65
        half_w = size * 0.42
        mid_x, mid_y = tip[0] - ux * half_len, tip[1] - uy * half_len
        back_x, back_y = tip[0] - ux * (2.0 * half_len), tip[1] - uy * (2.0 * half_len)
        left = (mid_x + px * half_w, mid_y + py * half_w)
        right = (mid_x - px * half_w, mid_y - py * half_w)
        pts = [tip, left, (back_x, back_y), right]
        pts_attr = " ".join(f"{_fmt(a)},{_fmt(b)}" for a, b in pts)
        fill = outline_fill if head_kind == "diamond_outline" else esc_stroke
        markup = (
            f'<polygon points="{pts_attr}" fill="{fill}" '
            f'stroke="{esc_stroke}" stroke-width="{sw}" stroke-linejoin="round"/>'
        )
        return markup, pts

    # Wing coordinates for "arrow", "triangle", "triangle_outline", and fallback kinds.
    wing_angle = math.radians(25.0)
    back = size * math.cos(wing_angle)
    spread = size * math.sin(wing_angle)
    w1 = (tip[0] - ux * back + px * spread, tip[1] - uy * back + py * spread)
    w2 = (tip[0] - ux * back - px * spread, tip[1] - uy * back - py * spread)
    pts = [tip, w1, w2]

    if head_kind in ("triangle", "triangle_outline"):
        pts_attr = " ".join(f"{_fmt(a)},{_fmt(b)}" for a, b in pts)
        fill = outline_fill if head_kind == "triangle_outline" else esc_stroke
        markup = (
            f'<polygon points="{pts_attr}" fill="{fill}" '
            f'stroke="{esc_stroke}" stroke-width="{sw}" stroke-linejoin="round"/>'
        )
        return markup, pts

    d = f"M {_fmt(w1[0])} {_fmt(w1[1])} L {_fmt(tip[0])} {_fmt(tip[1])} L {_fmt(w2[0])} {_fmt(w2[1])}"
    markup = (
        f'<path d="{d}" fill="none" stroke="{esc_stroke}" '
        f'stroke-width="{sw}" stroke-linecap="round" stroke-linejoin="round"/>'
    )
    return markup, pts


def _render_rectangle(
    element: Mapping[str, Any],
    patterns: _PatternRegistry,
) -> tuple[str, BBox | None]:
    x, y, w, h = _normalize_box(
        _num(element.get("x")),
        _num(element.get("y")),
        _num(element.get("width")),
        _num(element.get("height")),
    )
    cx, cy = x + w / 2.0, y + h / 2.0
    angle = _num(element.get("angle"))
    fill = patterns.fill_value(element)
    stroke_attr, sw = _stroke_attrs(element)
    opacity_attr = _opacity_attr(element)
    rot_attr = _rotate_attr(angle, cx, cy)
    r = _corner_radius(element.get("roundness"), w, h)
    round_attr = f' rx="{_fmt(r)}" ry="{_fmt(r)}"' if r > 0.0 else ""

    markup = (
        f'  <rect x="{_fmt(x)}" y="{_fmt(y)}" width="{_fmt(w)}" height="{_fmt(h)}"'
        f'{round_attr} fill="{fill}"{stroke_attr}{opacity_attr}{rot_attr}/>'
    )
    bbox = _bbox_from_points(_box_corners(x, y, w, h), cx=cx, cy=cy, angle=angle, expand=sw / 2.0)
    return markup, bbox


def _render_ellipse(
    element: Mapping[str, Any],
    patterns: _PatternRegistry,
) -> tuple[str, BBox | None]:
    x, y, w, h = _normalize_box(
        _num(element.get("x")),
        _num(element.get("y")),
        _num(element.get("width")),
        _num(element.get("height")),
    )
    cx, cy = x + w / 2.0, y + h / 2.0
    rx, ry = w / 2.0, h / 2.0
    angle = _num(element.get("angle"))
    fill = patterns.fill_value(element)
    stroke_attr, sw = _stroke_attrs(element)
    opacity_attr = _opacity_attr(element)
    rot_attr = _rotate_attr(angle, cx, cy)

    markup = (
        f'  <ellipse cx="{_fmt(cx)}" cy="{_fmt(cy)}" rx="{_fmt(rx)}" ry="{_fmt(ry)}"'
        f' fill="{fill}"{stroke_attr}{opacity_attr}{rot_attr}/>'
    )
    bbox = _bbox_from_points(_box_corners(x, y, w, h), cx=cx, cy=cy, angle=angle, expand=sw / 2.0)
    return markup, bbox


def _render_diamond(
    element: Mapping[str, Any],
    patterns: _PatternRegistry,
) -> tuple[str, BBox | None]:
    x, y, w, h = _normalize_box(
        _num(element.get("x")),
        _num(element.get("y")),
        _num(element.get("width")),
        _num(element.get("height")),
    )
    cx, cy = x + w / 2.0, y + h / 2.0
    angle = _num(element.get("angle"))
    fill = patterns.fill_value(element)
    stroke_attr, sw = _stroke_attrs(element)
    opacity_attr = _opacity_attr(element)
    rot_attr = _rotate_attr(angle, cx, cy)

    vertices = [
        (cx, y),
        (x + w, cy),
        (cx, y + h),
        (x, cy),
    ]
    r = _corner_radius(element.get("roundness"), w, h)
    if r > 0.0 and w > 0.0 and h > 0.0:
        # Round each corner by moving a fraction along each adjacent edge.
        edge_len = math.hypot(w / 2.0, h / 2.0)
        frac = min(0.35, r / edge_len) if edge_len > 1e-6 else 0.0
        cmds: list[str] = []
        for idx in range(4):
            prev_v = vertices[(idx - 1) % 4]
            curr_v = vertices[idx]
            next_v = vertices[(idx + 1) % 4]
            p_in = (
                curr_v[0] + (prev_v[0] - curr_v[0]) * frac,
                curr_v[1] + (prev_v[1] - curr_v[1]) * frac,
            )
            p_out = (
                curr_v[0] + (next_v[0] - curr_v[0]) * frac,
                curr_v[1] + (next_v[1] - curr_v[1]) * frac,
            )
            if idx == 0:
                cmds.append(f"M {_fmt(p_in[0])} {_fmt(p_in[1])}")
            else:
                cmds.append(f"L {_fmt(p_in[0])} {_fmt(p_in[1])}")
            cmds.append(f"Q {_fmt(curr_v[0])} {_fmt(curr_v[1])} {_fmt(p_out[0])} {_fmt(p_out[1])}")
        cmds.append("Z")
        markup = (
            f'  <path d="{" ".join(cmds)}" fill="{fill}"{stroke_attr}'
            f' stroke-linejoin="round"{opacity_attr}{rot_attr}/>'
        )
    else:
        pts_attr = " ".join(f"{_fmt(vx)},{_fmt(vy)}" for vx, vy in vertices)
        markup = (
            f'  <polygon points="{pts_attr}" fill="{fill}"{stroke_attr}'
            f' stroke-linejoin="round"{opacity_attr}{rot_attr}/>'
        )

    bbox = _bbox_from_points(vertices, cx=cx, cy=cy, angle=angle, expand=sw / 2.0)
    return markup, bbox


def _point_center(points: Sequence[tuple[float, float]], x: float, y: float, w: float, h: float) -> tuple[float, float]:
    if not points:
        return x + w / 2.0, y + h / 2.0
    xs = [p[0] for p in points]
    ys = [p[1] for p in points]
    return (min(xs) + max(xs)) / 2.0, (min(ys) + max(ys)) / 2.0


def _render_line_or_arrow(
    element: Mapping[str, Any],
    patterns: _PatternRegistry,
    *,
    is_arrow: bool,
    view_bg: str | None,
) -> tuple[str, BBox | None]:
    x = _num(element.get("x"))
    y = _num(element.get("y"))
    w = _num(element.get("width"))
    h = _num(element.get("height"))
    points = _extract_points(element, x, y)
    if not points:
        return "", None

    cx, cy = _point_center(points, x, y, w, h)
    angle = _num(element.get("angle"))
    closed = (not is_arrow) and _points_closed(points)
    curved = _is_round(element)
    fill = patterns.fill_value(element) if closed else "none"
    stroke_attr, sw = _stroke_attrs(element)
    opacity_attr = _opacity_attr(element)
    rot_attr = _rotate_attr(angle, cx, cy)
    d = _polyline_path_d(points, curved=curved, closed=closed)

    if not is_arrow:
        markup = (
            f'  <path d="{d}" fill="{fill}"{stroke_attr}'
            f' stroke-linecap="round" stroke-linejoin="round"{opacity_attr}{rot_attr}/>'
        )
        bbox = _bbox_from_points(points, cx=cx, cy=cy, angle=angle, expand=sw / 2.0)
        return markup, bbox

    stroke_color = _clean_color(element.get("strokeColor"), _DEFAULT_STROKE_COLOR)
    start_head = element.get("startArrowhead")
    if "endArrowhead" in element:
        end_head = element.get("endArrowhead")
    else:
        end_head = "arrow"

    extra_points: list[tuple[float, float]] = list(points)
    children = [
        f'    <path d="{d}" fill="none"{stroke_attr} stroke-linecap="round" stroke-linejoin="round"/>'
    ]
    if len(points) >= 2 and not _is_transparent(stroke_color):
        if isinstance(start_head, str) and start_head.strip():
            head_svg, head_pts = _render_arrowhead(
                points[0],
                points[1],
                start_head,
                stroke_color=stroke_color,
                stroke_width=sw or _DEFAULT_STROKE_WIDTH,
                view_bg=view_bg,
            )
            if head_svg:
                children.append(f"    {head_svg}")
                extra_points.extend(head_pts)
        if isinstance(end_head, str) and end_head.strip():
            head_svg, head_pts = _render_arrowhead(
                points[-1],
                points[-2],
                end_head,
                stroke_color=stroke_color,
                stroke_width=sw or _DEFAULT_STROKE_WIDTH,
                view_bg=view_bg,
            )
            if head_svg:
                children.append(f"    {head_svg}")
                extra_points.extend(head_pts)

    markup = f"  <g{opacity_attr}{rot_attr}>\n" + "\n".join(children) + "\n  </g>"
    bbox = _bbox_from_points(extra_points, cx=cx, cy=cy, angle=angle, expand=sw / 2.0)
    return markup, bbox


def _render_freedraw(
    element: Mapping[str, Any],
    patterns: _PatternRegistry,
) -> tuple[str, BBox | None]:
    x = _num(element.get("x"))
    y = _num(element.get("y"))
    w = _num(element.get("width"))
    h = _num(element.get("height"))
    points = _extract_points(element, x, y)
    if not points:
        return "", None

    cx, cy = _point_center(points, x, y, w, h)
    angle = _num(element.get("angle"))
    stroke_attr, sw = _stroke_attrs(element)
    stroke_color = _clean_color(element.get("strokeColor"), _DEFAULT_STROKE_COLOR)
    opacity_attr = _opacity_attr(element)
    rot_attr = _rotate_attr(angle, cx, cy)

    if len(points) == 1:
        r = max(sw / 2.0, 1.0)
        px, py = points[0]
        fill_color = "none" if _is_transparent(stroke_color) else _escape_attr(stroke_color)
        markup = (
            f'  <circle cx="{_fmt(px)}" cy="{_fmt(py)}" r="{_fmt(r)}" '
            f'fill="{fill_color}"{opacity_attr}{rot_attr}/>'
        )
        bbox = _bbox_from_points(points, cx=cx, cy=cy, angle=angle, expand=r)
        return markup, bbox

    closed = _points_closed(points)
    fill = patterns.fill_value(element) if closed else "none"
    d = _polyline_path_d(points, curved=True, closed=closed)
    markup = (
        f'  <path d="{d}" fill="{fill}"{stroke_attr}'
        f' stroke-linecap="round" stroke-linejoin="round"{opacity_attr}{rot_attr}/>'
    )
    bbox = _bbox_from_points(points, cx=cx, cy=cy, angle=angle, expand=sw / 2.0)
    return markup, bbox


def _resolve_font_family(raw: Any) -> str:
    if isinstance(raw, int) and not isinstance(raw, bool):
        return _FONT_FAMILIES.get(raw, "sans-serif")
    if isinstance(raw, str) and raw.strip():
        return f"{raw.strip()}, sans-serif"
    return "sans-serif"


def _render_text(element: Mapping[str, Any]) -> tuple[str, BBox | None]:
    raw_text = element.get("text")
    if not isinstance(raw_text, str):
        return "", None
    lines = raw_text.replace("\r\n", "\n").replace("\r", "\n").split("\n")

    x = _num(element.get("x"))
    y = _num(element.get("y"))
    font_size = max(1.0, _num(element.get("fontSize"), _DEFAULT_FONT_SIZE))
    line_height = max(0.5, _num(element.get("lineHeight"), _DEFAULT_LINE_HEIGHT))
    line_step = font_size * line_height
    total_text_h = len(lines) * line_step

    raw_w = _num(element.get("width"))
    raw_h = _num(element.get("height"))
    est_w = max((len(line) for line in lines), default=1) * font_size * 0.6
    w = abs(raw_w) if raw_w != 0.0 else est_w
    h = abs(raw_h) if raw_h != 0.0 else total_text_h
    if raw_w < 0:
        x += raw_w
    if raw_h < 0:
        y += raw_h

    text_align = str(element.get("textAlign") or "left").strip().lower()
    anchor = _TEXT_ANCHORS.get(text_align, "start")
    if text_align == "center":
        tx = x + w / 2.0
    elif text_align == "right":
        tx = x + w
    else:
        tx = x

    vertical_align = str(element.get("verticalAlign") or "top").strip().lower()
    if vertical_align == "middle":
        start_y = y + (h - total_text_h) / 2.0
    elif vertical_align == "bottom":
        start_y = y + (h - total_text_h)
    else:
        start_y = y

    cx, cy = x + w / 2.0, y + h / 2.0
    angle = _num(element.get("angle"))
    color = _clean_color(element.get("strokeColor"), _DEFAULT_STROKE_COLOR)
    fill_attr = ' fill="none"' if _is_transparent(color) else f' fill="{_escape_attr(color)}"'
    font_family = _escape_attr(_resolve_font_family(element.get("fontFamily")))
    opacity_attr = _opacity_attr(element)
    rot_attr = _rotate_attr(angle, cx, cy)

    base_attrs = (
        f' font-family="{font_family}" font-size="{_fmt(font_size)}"'
        f' text-anchor="{anchor}"{fill_attr}{opacity_attr}{rot_attr}'
    )

    if len(lines) == 1:
        baseline_y = start_y + font_size * 0.85
        escaped = _escape_text(lines[0])
        markup = f'  <text x="{_fmt(tx)}" y="{_fmt(baseline_y)}"{base_attrs}>{escaped}</text>'
    else:
        tspans: list[str] = []
        for idx, line in enumerate(lines):
            baseline_y = start_y + idx * line_step + font_size * 0.85
            tspans.append(
                f'    <tspan x="{_fmt(tx)}" y="{_fmt(baseline_y)}">{_escape_text(line)}</tspan>'
            )
        markup = f'  <text x="{_fmt(tx)}" y="{_fmt(start_y + font_size * 0.85)}"{base_attrs}>\n' + "\n".join(tspans) + "\n  </text>"

    bbox = _bbox_from_points(_box_corners(x, y, w, h), cx=cx, cy=cy, angle=angle)
    return markup, bbox


def _render_image(
    element: Mapping[str, Any],
    files: Mapping[str, Any],
) -> tuple[str, BBox | None]:
    file_id = element.get("fileId")
    if not isinstance(file_id, str) or not file_id:
        return "", None
    file_entry = files.get(file_id)
    if not isinstance(file_entry, Mapping):
        return "", None
    data_url = file_entry.get("dataURL")
    if not isinstance(data_url, str) or not data_url.strip().lower().startswith("data:"):
        return "", None

    x, y, w, h = _normalize_box(
        _num(element.get("x")),
        _num(element.get("y")),
        _num(element.get("width")),
        _num(element.get("height")),
    )
    if w <= 0.0 or h <= 0.0:
        return "", None

    cx, cy = x + w / 2.0, y + h / 2.0
    angle = _num(element.get("angle"))
    opacity_attr = _opacity_attr(element)

    transforms: list[str] = []
    if abs(angle) > 1e-9:
        transforms.append(f"rotate({_fmt(math.degrees(angle))} {_fmt(cx)} {_fmt(cy)})")
    scale = element.get("scale")
    if isinstance(scale, (list, tuple)) and len(scale) == 2:
        sx = _num(scale[0], 1.0)
        sy = _num(scale[1], 1.0)
        if abs(sx - 1.0) > 1e-6 or abs(sy - 1.0) > 1e-6:
            transforms.append(
                f"translate({_fmt(cx)} {_fmt(cy)}) scale({_fmt(sx)} {_fmt(sy)}) "
                f"translate({_fmt(-cx)} {_fmt(-cy)})"
            )
    transform_attr = f' transform="{" ".join(transforms)}"' if transforms else ""

    markup = (
        f'  <image href="{_escape_attr(data_url.strip())}" '
        f'x="{_fmt(x)}" y="{_fmt(y)}" width="{_fmt(w)}" height="{_fmt(h)}" '
        f'preserveAspectRatio="none"{opacity_attr}{transform_attr}/>'
    )
    bbox = _bbox_from_points(_box_corners(x, y, w, h), cx=cx, cy=cy, angle=angle)
    return markup, bbox


def scene_to_svg(
    scene: Mapping[str, Any],
    *,
    padding: float = DEFAULT_PADDING,
    source: Path | None = None,
) -> SVG:
    """Convert an Excalidraw scene mapping into a clean :class:`~pixcell.docs.SVG` document.

    Emits crisp geometric SVG shapes (without rough.js hand-drawn distortion) for
    ``rectangle``, ``ellipse``, ``diamond``, ``line``, ``arrow``, ``freedraw``, ``text``,
    and ``image`` elements. Deleted elements (``isDeleted: true``) are skipped.
    """
    if padding < 0:
        raise ValueError("padding must be non-negative")
    if not isinstance(scene, Mapping):
        raise DocsError("Excalidraw scene must be a JSON object")

    raw_elements = scene.get("elements", [])
    if not isinstance(raw_elements, list):
        raise DocsError("Excalidraw scene 'elements' must be a list")

    raw_app_state = scene.get("appState")
    app_state: Mapping[str, Any] = raw_app_state if isinstance(raw_app_state, Mapping) else {}

    raw_files = scene.get("files")
    files: Mapping[str, Any] = raw_files if isinstance(raw_files, Mapping) else {}

    raw_bg = app_state.get("viewBackgroundColor")
    view_bg: str | None = (
        raw_bg.strip()
        if isinstance(raw_bg, str) and not _is_transparent(raw_bg)
        else None
    )

    raw_title = app_state.get("name") or scene.get("name") or ""
    title = raw_title.strip() if isinstance(raw_title, str) else ""

    patterns = _PatternRegistry()
    body_nodes: list[str] = []
    bboxes: list[BBox] = []

    for item in raw_elements:
        if not isinstance(item, Mapping):
            continue
        if item.get("isDeleted"):
            continue

        elem_type = str(item.get("type") or "").strip().lower()
        node = ""
        bbox: BBox | None = None

        if elem_type == "rectangle":
            node, bbox = _render_rectangle(item, patterns)
        elif elem_type == "ellipse":
            node, bbox = _render_ellipse(item, patterns)
        elif elem_type == "diamond":
            node, bbox = _render_diamond(item, patterns)
        elif elem_type == "line":
            node, bbox = _render_line_or_arrow(item, patterns, is_arrow=False, view_bg=view_bg)
        elif elem_type == "arrow":
            node, bbox = _render_line_or_arrow(item, patterns, is_arrow=True, view_bg=view_bg)
        elif elem_type == "freedraw":
            node, bbox = _render_freedraw(item, patterns)
        elif elem_type == "text":
            node, bbox = _render_text(item)
        elif elem_type == "image":
            node, bbox = _render_image(item, files)
        else:
            continue

        if node:
            body_nodes.append(node)
        if bbox is not None:
            bboxes.append(bbox)

    merged = _merge_bboxes(bboxes)
    if merged is None:
        fallback = max(padding * 2.0, 100.0)
        vx, vy, vw, vh = 0.0, 0.0, fallback, fallback
    else:
        vx = merged[0] - padding
        vy = merged[1] - padding
        vw = max(merged[2] - merged[0] + padding * 2.0, 1.0)
        vh = max(merged[3] - merged[1] + padding * 2.0, 1.0)

    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        (
            f'<svg xmlns="http://www.w3.org/2000/svg" '
            f'width="{_fmt(vw)}" height="{_fmt(vh)}" '
            f'viewBox="{_fmt(vx)} {_fmt(vy)} {_fmt(vw)} {_fmt(vh)}">'
        ),
    ]
    if title:
        parts.append(f"  <title>{_escape_text(title)}</title>")
    defs_markup = patterns.render_defs()
    if defs_markup:
        parts.append(defs_markup)
    if view_bg is not None:
        parts.append(
            f'  <rect x="{_fmt(vx)}" y="{_fmt(vy)}" width="{_fmt(vw)}" height="{_fmt(vh)}" '
            f'fill="{_escape_attr(view_bg)}"/>'
        )
    parts.extend(body_nodes)
    parts.append("</svg>")

    markup = "\n".join(parts) + "\n"
    return SVG(svg=markup, width=vw, height=vh, title=title, source=source)


def render(
    path: Path,
    *,
    padding: float = DEFAULT_PADDING,
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
    """Open an Excalidraw scene file as an :class:`SVG` (or :class:`Picture` if ``rasterize=True``)."""
    try:
        raw_text = path.read_text(encoding="utf-8-sig")
        scene = json.loads(raw_text)
    except (OSError, UnicodeDecodeError, json.JSONDecodeError) as exc:
        raise DocsError(f"Failed to parse Excalidraw scene {path}: {exc}") from exc

    document = scene_to_svg(scene, padding=padding, source=path)
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

"""Mermaid diagram renderer using Pele inside QuickJS for :mod:`pixcell.docs`.

Renders Mermaid source with Pele (https://github.com/obsidianmd/pele, MIT; vendored at
``vendor/pele.global.js``) inside ``quickjs-ng`` (MIT), returning an :class:`~pixcell.docs.SVG`
document whose CSS variables are resolved so it rasterises cleanly via :mod:`pixcell.docs.svg`
(``resvg-py``).

Both ``quickjs-ng`` and ``resvg-py`` live behind the ``pixcell[mermaid]`` optional extra and
are imported lazily only when a Mermaid diagram is rendered.
"""

from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any

from pixcell.docs import svg as svg_mod
from pixcell.docs.errors import DocsError, MissingExtra
from pixcell.docs.types import SVG, Picture

BUNDLE = Path(__file__).parent / "vendor" / "pele.global.js"
"""Path to the vendored Pele JavaScript bundle (MIT)."""

FONT = "Arial, 'Liberation Sans', Helvetica, 'DejaVu Sans', sans-serif"
"""Default diagram label font stack.

Pele measures labels with Arial's widths; Liberation Sans shares the same metrics, and
DejaVu Sans is a common Linux fallback.
"""

FONT_MONO = "Menlo, 'DejaVu Sans Mono', monospace"
"""Default monospace font stack for Mermaid diagrams."""

_LOCK = threading.Lock()
"""QuickJS contexts are not thread-safe; serialise all evaluations."""

_VAR_RE = re.compile(r"var\((--[\w-]+)\s*(?:,\s*([^()]*(?:\([^()]*\)[^()]*)*))?\)")
_DEFINITION_RE = re.compile(r"(--[\w-]+)\s*:\s*([^;\"]+)")
_DECLARATION_RE = re.compile(r"(?<=[\";])\s*--[\w-]+\s*:[^;\"]*;?")


class MermaidError(DocsError):
    """Raised when Pele cannot parse or render a Mermaid diagram."""


@dataclass(frozen=True)
class MermaidTheme:
    """Colours and fonts for Pele's ``--pele-*`` CSS custom properties."""

    bg: str
    fg: str
    muted: str
    line: str
    surface: str
    surface_alt: str
    border: str
    accent: str
    series: tuple[str, ...] = ()
    """Chart colours (pie slices, bars, gantt tasks), first to last; Pele's own after these."""
    critical: str = ""
    font: str = FONT
    font_mono: str = FONT_MONO

    def variables(self) -> dict[str, str]:
        """Return the ``--pele-*`` CSS variable mapping for this theme."""
        variables = {
            "--pele-bg": self.bg,
            "--pele-fg": self.fg,
            "--pele-muted": self.muted,
            "--pele-line": self.line,
            "--pele-surface": self.surface,
            "--pele-surface-alt": self.surface_alt,
            "--pele-border": self.border,
            "--pele-accent": self.accent,
            "--pele-font": self.font,
            "--pele-font-mono": self.font_mono,
        }
        for number, colour in enumerate(self.series[:8], 1):
            variables[f"--pele-series-{number}"] = colour
        if self.critical:
            variables["--pele-critical"] = self.critical
        return variables


Palette = MermaidTheme
"""Alias for :class:`MermaidTheme`."""

DARK = MermaidTheme(
    bg="#1e1e1e",
    fg="#e6e6e6",
    muted="#9a9a9a",
    line="#c8c8c8",
    surface="#2a2d33",
    surface_alt="#353a42",
    border="#6b7280",
    accent="#7aa2f7",
)
LIGHT = MermaidTheme(
    bg="#ffffff",
    fg="#1f1f1f",
    muted="#6e6e6e",
    line="#1f1f1f",
    surface="#f3f3f3",
    surface_alt="#e6e6e6",
    border="#8a8a8a",
    accent="#5b7bd5",
)

THEMES: dict[str, MermaidTheme] = {
    "dark": DARK,
    "light": LIGHT,
}


def _import_quickjs() -> Any:
    try:
        import quickjs
    except ImportError as exc:
        raise MissingExtra(
            "mermaid",
            renderer="mermaid",
            dependency=getattr(exc, "name", None) or "quickjs",
        ) from exc
    return quickjs


@lru_cache(maxsize=1)
def _context() -> Any:
    quickjs = _import_quickjs()
    if not BUNDLE.is_file():
        raise DocsError(f"Vendored Pele bundle is missing: {BUNDLE}")
    context = quickjs.Context()
    context.eval("var globalThis = this;")
    context.eval(BUNDLE.read_text(encoding="utf-8"))
    context.eval(
        "function __render(source, options) {"
        "  var r = pele.render(source, options);"
        "  return JSON.stringify({svg: r.svg, width: r.width, height: r.height});"
        "}"
    )
    return context


def _resolve_theme(theme: MermaidTheme | str | None) -> MermaidTheme:
    if theme is None:
        return LIGHT
    if isinstance(theme, MermaidTheme):
        return theme
    if isinstance(theme, str):
        chosen = THEMES.get(theme.strip().lower())
        if chosen is not None:
            return chosen
        raise ValueError(
            f"Unknown Mermaid theme {theme!r}; expected 'light', 'dark', or a MermaidTheme"
        )
    raise TypeError(
        f"theme must be a MermaidTheme, 'light', 'dark', or None, got {type(theme).__name__}"
    )


def resolve_variables(svg: str, variables: dict[str, str]) -> str:
    """Replace every ``var(--x, fallback)`` with its concrete value, as a browser would.

    Pele styles its SVG with CSS custom properties, which ``resvg`` does not interpret and
    which also cause ``resvg`` to stop parsing a ``style`` attribute before subsequent
    declarations (such as ``font-family``).
    """
    values = dict(variables)
    # Pele defines its own short names on the root element (--_bg:var(--pele-bg,#fff)),
    # and a diagram's own %%{init}%% colours as --pele-series-*: those win over the theme.
    for name, value in _DEFINITION_RE.findall(svg):
        values[name] = value.strip()

    def substitute(match: re.Match[str]) -> str:
        name, fallback = match.group(1), match.group(2)
        return values.get(name, fallback.strip() if fallback else "")

    for _ in range(5):  # values can refer to values
        resolved = _VAR_RE.sub(substitute, svg)
        if resolved == svg:
            break
        svg = resolved
        values = {name: _VAR_RE.sub(substitute, value) for name, value in values.items()}
    # Strip the custom property declarations themselves so resvg parses the rest of style="...".
    return _DECLARATION_RE.sub("", svg)


def _format_js_error(exc: Exception) -> str:
    raw = str(exc).strip()
    if not raw:
        return type(exc).__name__
    # Strip minified JS stack frames ("    at ...") while preserving Pele's message and snippet.
    lines = [line for line in raw.splitlines() if not line.lstrip().startswith("at ")]
    cleaned = "\n".join(lines).strip()
    return cleaned or raw


def _render_raw(source: str, *, max_width: int | None = None) -> tuple[str, float, float]:
    """Evaluate Pele on ``source`` and return ``(raw_svg, width, height)``."""
    _import_quickjs()
    if not source or not source.strip():
        raise MermaidError("Mermaid diagram source is empty")
    if max_width is not None and max_width <= 0:
        raise ValueError("max_width must be positive")

    options = {"maxWidth": int(max_width)} if max_width is not None else {}
    call = f"__render({json.dumps(source)}, {json.dumps(options)})"
    with _LOCK:
        context = _context()
        try:
            payload = context.eval(call)
        except Exception as exc:
            if isinstance(exc, DocsError):
                raise
            raise MermaidError(f"Failed to render Mermaid diagram: {_format_js_error(exc)}") from exc

    result = json.loads(payload)
    return str(result["svg"]), float(result["width"]), float(result["height"])


def render_mermaid(
    source: str,
    theme: MermaidTheme | str | None = None,
    *,
    max_width: int | None = None,
    resolve_vars: bool = True,
    title: str = "",
    source_path: Path | None = None,
) -> SVG:
    """Render Mermaid diagram ``source`` to an :class:`~pixcell.docs.SVG` document.

    Parameters
    ----------
    source:
        Mermaid diagram text (e.g. ``flowchart LR\\n  A --> B``).
    theme:
        A :class:`MermaidTheme` instance, ``"light"``, ``"dark"``, or ``None`` (defaults
        to :data:`LIGHT`).
    max_width:
        Optional layout width hint in SVG units; diagrams that support reflow (such as
        gantt, timeline, mindmap, and charts) lay themselves out narrower to fit.
    resolve_vars:
        If ``True`` (the default), resolve Pele's ``--pele-*`` CSS custom properties using
        ``theme`` so the returned SVG rasterises cleanly via ``doc.to_picture()``.
    title:
        Optional document title; if omitted, extracted from any ``<title>`` in the SVG.
    source_path:
        Optional source file path attached to the returned :class:`SVG`.
    """
    active_theme = _resolve_theme(theme)
    raw_svg, width, height = _render_raw(source, max_width=max_width)
    svg_markup = resolve_variables(raw_svg, active_theme.variables()) if resolve_vars else raw_svg
    if not title:
        _, _, title = svg_mod.parse_svg_info(svg_markup)
    return SVG(
        svg=svg_markup,
        width=width,
        height=height,
        title=title,
        source=source_path,
    )


def render(
    path: Path,
    *,
    theme: MermaidTheme | str | None = None,
    max_width: int | None = None,
    resolve_vars: bool = True,
    rasterize: bool = False,
    zoom: float = svg_mod.DEFAULT_ZOOM,
    dpi: float = svg_mod.DEFAULT_DPI,
    background: str | None = None,
    font_family: str | None = None,
    sans_serif_family: str | None = None,
    width: int | None = None,
    height: int | None = None,
) -> SVG | Picture:
    """Open a ``.mmd``/``.mermaid`` file as an :class:`SVG` (or :class:`Picture` if ``rasterize=True``)."""
    _import_quickjs()
    active_theme = _resolve_theme(theme)
    source = path.read_text(encoding="utf-8-sig")
    document = render_mermaid(
        source,
        theme=active_theme,
        max_width=max_width,
        resolve_vars=resolve_vars,
        source_path=path,
    )
    if rasterize:
        return svg_mod.to_picture(
            document,
            zoom=zoom,
            dpi=dpi,
            background=background if background is not None else active_theme.bg,
            font_family=font_family,
            sans_serif_family=sans_serif_family or svg_mod.DEFAULT_FONT_FAMILY,
            width=width,
            height=height,
        )
    return document

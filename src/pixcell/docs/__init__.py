"""Document rendering registry and output types for pixcell.

Call :func:`open` with a path to get a displayable :class:`Picture`, :class:`SVG`, or
:class:`HTML`. Heavy dependencies live behind optional extras (e.g. ``pixcell[svg]``) and
are imported lazily only when a matching document is rendered.
"""

from __future__ import annotations

import os
from collections.abc import Iterable
from typing import Any

from pixcell.docs.errors import DocsError, DocumentTooLarge, MissingExtra, UnsupportedType
from pixcell.docs.registry import (
    Registry,
    Renderer,
    RendererLoader,
    Sniffer,
    default_registry,
    sniff_mime,
)
from pixcell.docs.types import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_PAGES,
    HTML,
    SVG,
    Document,
    LazyPages,
    Picture,
)

__all__ = [
    "DEFAULT_MAX_BYTES",
    "DEFAULT_MAX_PAGES",
    "Document",
    "DocsError",
    "DocumentTooLarge",
    "HTML",
    "LazyPages",
    "MissingExtra",
    "Picture",
    "Registry",
    "Renderer",
    "SVG",
    "UnsupportedType",
    "open",
    "register",
    "renderers",
    "resolve",
    "sniff_mime",
    "supports",
    "unregister",
]


def open(
    path: str | os.PathLike[str],
    *,
    max_bytes: int | None = DEFAULT_MAX_BYTES,
    max_pages: int | None = DEFAULT_MAX_PAGES,
    mime_type: str | None = None,
    **options: Any,
) -> Document:
    """Open ``path`` with the matching renderer and return a :data:`Document`."""
    return default_registry.open(
        path,
        max_bytes=max_bytes,
        max_pages=max_pages,
        mime_type=mime_type,
        **options,
    )


def register(
    renderer: Renderer | str,
    *,
    loader: RendererLoader | None = None,
    suffixes: Iterable[str] = (),
    mime_types: Iterable[str] = (),
    extra: str | None = None,
    sniff: Sniffer | None = None,
    priority: int = 0,
) -> Renderer:
    """Register a document renderer in the default registry."""
    return default_registry.register(
        renderer,
        loader=loader,
        suffixes=suffixes,
        mime_types=mime_types,
        extra=extra,
        sniff=sniff,
        priority=priority,
    )


def unregister(name: str) -> bool:
    """Remove a renderer by name from the default registry."""
    return default_registry.unregister(name)


def renderers() -> tuple[Renderer, ...]:
    """Return all registered renderers in the default registry."""
    return default_registry.renderers()


def resolve(
    path: str | os.PathLike[str],
    *,
    mime_type: str | None = None,
    sniff: bool = True,
) -> Renderer | None:
    """Return the :class:`Renderer` that handles ``path``, or ``None``."""
    return default_registry.resolve(path, mime_type=mime_type, sniff=sniff)


def supports(
    path: str | os.PathLike[str],
    *,
    mime_type: str | None = None,
    sniff: bool = True,
) -> bool:
    """Return True if a registered renderer can handle ``path``."""
    return default_registry.supports(path, mime_type=mime_type, sniff=sniff)

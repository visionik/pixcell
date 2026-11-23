"""Shared output types for :mod:`pixcell.docs` renderers.

Every renderer returns one of three small frozen dataclasses:

- :class:`Picture` — one or more pages as Pillow images (backed by :class:`LazyPages` so
  multi-page documents only render pages on demand).
- :class:`SVG` — native SVG markup with its parsed width and height; can be rasterised to
  a :class:`Picture` or PNG bytes via ``pixcell[svg]``.
- :class:`HTML` — HTML markup plus a base path or URL for relative assets, ready for a
  browser view.
"""

from __future__ import annotations

import os
import threading
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Self, overload

if TYPE_CHECKING:
    from PIL.Image import Image

DEFAULT_MAX_BYTES = 50 * 1024 * 1024
"""Default maximum file size (50 MiB) accepted by :func:`pixcell.docs.open`."""

DEFAULT_MAX_PAGES = 200
"""Default maximum page count exposed by :func:`pixcell.docs.open`."""


class LazyPages(Sequence["Image"]):
    """A read-only sequence of Pillow images rendered on demand by page index.

    Indexing ``pages[n]`` calls ``render(n)`` only when page ``n`` is requested. Pass
    ``cache=True`` to retain rendered pages, and ``close`` if the renderer holds a handle
    (e.g. a PDF document) that should be released when the :class:`Picture` is closed.
    """

    def __init__(
        self,
        count: int,
        render: Callable[[int], Image],
        *,
        cache: bool = False,
        close: Callable[[], None] | None = None,
    ) -> None:
        if count < 0:
            raise ValueError("page count must be non-negative")
        self._count = count
        self._render = render
        self._cache: dict[int, Image] | None = {} if cache else None
        self._close = close
        self._closed = False
        self._lock = threading.Lock()

    def __len__(self) -> int:
        return self._count

    @overload
    def __getitem__(self, index: int) -> Image: ...

    @overload
    def __getitem__(self, index: slice) -> list[Image]: ...

    def __getitem__(self, index: int | slice) -> Image | list[Image]:
        if isinstance(index, slice):
            return [self[i] for i in range(*index.indices(self._count))]
        if index < 0:
            index += self._count
        if not 0 <= index < self._count:
            raise IndexError(f"page index {index} out of range (0..{self._count - 1})")
        if self._cache is not None:
            with self._lock:
                cached = self._cache.get(index)
                if cached is not None:
                    return cached
            image = self._render(index)
            with self._lock:
                self._cache.setdefault(index, image)
                return self._cache[index]
        return self._render(index)

    def capped(self, max_pages: int) -> LazyPages:
        """Return a view limited to at most ``max_pages`` without rendering any page."""
        if max_pages < 0:
            raise ValueError("max_pages must be non-negative")
        if self._count <= max_pages:
            return self
        return LazyPages(
            max_pages,
            self._render,
            cache=self._cache is not None,
            close=self._close,
        )

    def close(self) -> None:
        """Release any underlying document handle and cached pages."""
        with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._cache is not None:
                self._cache.clear()
            closer = self._close
        if closer is not None:
            closer()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    def __repr__(self) -> str:
        return f"LazyPages(count={self._count})"


@dataclass(frozen=True)
class Picture:
    """One or more pages as Pillow images."""

    pages: Sequence[Image]
    title: str = ""
    source: Path | None = None
    total_pages: int | None = None
    """Original page count before ``max_pages`` capping, or ``None`` if uncapped."""

    @property
    def page_count(self) -> int:
        return len(self.pages)

    @property
    def image(self) -> Image:
        """The first page (convenient for single-page pictures)."""
        return self.pages[0]

    @property
    def truncated(self) -> bool:
        """True when ``pages`` was capped below the document's ``total_pages``."""
        return self.total_pages is not None and len(self.pages) < self.total_pages

    def __len__(self) -> int:
        return len(self.pages)

    def close(self) -> None:
        closer = getattr(self.pages, "close", None)
        if callable(closer):
            closer()

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@dataclass(frozen=True)
class SVG:
    """An SVG document returned by renderers whose native output is SVG."""

    svg: str
    width: float | None = None
    height: float | None = None
    title: str = ""
    source: Path | None = None

    def to_png(
        self,
        *,
        zoom: float = 2.0,
        dpi: float = 96.0,
        background: str | None = None,
        font_family: str | None = None,
        sans_serif_family: str | None = None,
        width: int | None = None,
        height: int | None = None,
        resources_dir: str | os.PathLike[str] | None = None,
    ) -> bytes:
        """Rasterise this SVG to PNG bytes using ``pixcell[svg]`` (resvg-py)."""
        from pixcell.docs.svg import rasterize

        if resources_dir is None and self.source is not None:
            resources_dir = self.source.parent
        return rasterize(
            self.svg,
            zoom=zoom,
            dpi=dpi,
            background=background,
            font_family=font_family,
            sans_serif_family=sans_serif_family,
            width=width,
            height=height,
            resources_dir=resources_dir,
        )

    def to_picture(
        self,
        *,
        zoom: float = 2.0,
        dpi: float = 96.0,
        background: str | None = None,
        font_family: str | None = None,
        sans_serif_family: str | None = None,
        width: int | None = None,
        height: int | None = None,
        resources_dir: str | os.PathLike[str] | None = None,
    ) -> Picture:
        """Rasterise this SVG to a 1-page :class:`Picture` using ``pixcell[svg]``."""
        from pixcell.docs.svg import to_picture

        if resources_dir is None and self.source is not None:
            resources_dir = self.source.parent
        return to_picture(
            self,
            zoom=zoom,
            dpi=dpi,
            background=background,
            font_family=font_family,
            sans_serif_family=sans_serif_family,
            width=width,
            height=height,
            resources_dir=resources_dir,
        )

    def close(self) -> None:
        pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


@dataclass(frozen=True)
class HTML:
    """HTML markup with a base path or URL for resolving relative assets."""

    html: str
    base: Path | str | None = None
    title: str = ""
    source: Path | None = None

    @property
    def base_url(self) -> str | None:
        """``base`` as a URL (``file://...`` when ``base`` is a :class:`Path`)."""
        if isinstance(self.base, Path):
            return self.base.resolve().as_uri()
        return self.base

    def close(self) -> None:
        pass

    def __enter__(self) -> Self:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()


Document = Picture | SVG | HTML

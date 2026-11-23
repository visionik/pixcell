"""Renderer registry, cheap magic-byte sniffing, and :func:`open`."""

from __future__ import annotations

import builtins
import importlib
import inspect
import os
import struct
import zipfile
from collections.abc import Callable, Iterable
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any

from pixcell.docs.errors import DocumentTooLarge, MissingExtra, UnsupportedType
from pixcell.docs.types import (
    DEFAULT_MAX_BYTES,
    DEFAULT_MAX_PAGES,
    Document,
    LazyPages,
    Picture,
)

RendererCallable = Callable[..., Document]
RendererLoader = RendererCallable | str
Sniffer = Callable[[bytes, Path], bool]

_SNIFF_BYTES = 4096
_BMP_DIB_SIZES = frozenset({12, 40, 52, 56, 108, 124})
_NETPBM_MAGIC = (b"P1", b"P2", b"P3", b"P4", b"P5", b"P6")
_OOXML_MARKERS: tuple[tuple[str, str], ...] = (
    ("word/document.xml", "application/vnd.openxmlformats-officedocument.wordprocessingml.document"),
    ("xl/workbook.xml", "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"),
    ("ppt/presentation.xml", "application/vnd.openxmlformats-officedocument.presentationml.presentation"),
)


def _normalize_suffix(suffix: str) -> str:
    cleaned = suffix.strip().lower()
    if not cleaned:
        raise ValueError("suffix must not be empty")
    return cleaned if cleaned.startswith(".") else f".{cleaned}"


def _normalize_mime(mime_type: str) -> str:
    # Strip optional parameters like "; charset=utf-8"
    return mime_type.split(";", 1)[0].strip().lower()


@dataclass(frozen=True)
class Renderer:
    """Descriptor for a document renderer registered in :class:`Registry`.

    ``loader`` may be a callable ``(path, **options) -> Document`` or a lazy import string
    ``"package.module:callable"``, so optional dependencies are never imported until a
    matching file is opened.
    """

    name: str
    loader: RendererLoader
    suffixes: tuple[str, ...] = ()
    mime_types: tuple[str, ...] = ()
    extra: str | None = None
    sniff: Sniffer | None = None
    priority: int = 0

    def __post_init__(self) -> None:
        if not self.name:
            raise ValueError("renderer name must not be empty")
        object.__setattr__(
            self, "suffixes", tuple(dict.fromkeys(_normalize_suffix(s) for s in self.suffixes))
        )
        object.__setattr__(
            self, "mime_types", tuple(dict.fromkeys(_normalize_mime(m) for m in self.mime_types))
        )

    def resolve_loader(self) -> RendererCallable:
        if callable(self.loader):
            return self.loader
        module_name, sep, attr = self.loader.partition(":")
        if not sep or not module_name or not attr:
            raise ValueError(
                f"Renderer {self.name!r} loader string must be 'module:callable', got {self.loader!r}"
            )
        module = importlib.import_module(module_name)
        target = getattr(module, attr)
        if not callable(target):
            raise TypeError(f"Renderer {self.name!r} target {self.loader!r} is not callable")
        return target


def _candidate_suffixes(path: Path) -> list[str]:
    """Candidate suffixes for ``path`` from most specific (compound) to least specific.

    For ``diagram.excalidraw.json`` yields ``['.excalidraw.json', '.json']``.
    """
    parts = [s.lower() for s in path.suffixes]
    if not parts:
        return []
    return ["".join(parts[i:]) for i in range(len(parts))]


def _sniff_zip_mime(header: bytes, path: Path | None) -> str:
    # Valid EPUBs store an uncompressed "mimetype" entry first right after the 30-byte header.
    if b"mimetypeapplication/epub+zip" in header[:96]:
        return "application/epub+zip"
    if path is not None:
        try:
            with zipfile.ZipFile(path) as archive:
                names = set(archive.namelist())
                if "mimetype" in names and archive.getinfo("mimetype").file_size <= 256:
                    try:
                        mime = archive.read("mimetype").decode("ascii", errors="ignore").strip()
                        if mime == "application/epub+zip":
                            return "application/epub+zip"
                    except Exception:
                        pass
                for marker, mime in _OOXML_MARKERS:
                    if marker in names:
                        return mime
        except (OSError, zipfile.BadZipFile):
            pass
    return "application/zip"


def sniff_mime(header: bytes, path: Path | None = None) -> str | None:
    """Identify common document and image formats from leading magic bytes."""
    if not header:
        return None
    if header.startswith(b"%PDF-"):
        return "application/pdf"
    if header.startswith(b"\x89PNG\r\n\x1a\n"):
        return "image/png"
    if header.startswith(b"\xff\xd8\xff"):
        return "image/jpeg"
    if header.startswith((b"GIF87a", b"GIF89a")):
        return "image/gif"
    if len(header) >= 12 and header[:4] == b"RIFF" and header[8:12] == b"WEBP":
        return "image/webp"
    if header.startswith((b"II*\x00", b"MM\x00*")):
        return "image/tiff"
    if header.startswith(b"\x00\x00\x01\x00"):
        return "image/x-icon"
    if header.startswith(b"qoif"):
        return "image/qoi"
    if len(header) >= 18 and header[:2] == b"BM":
        dib_size = struct.unpack_from("<I", header, 14)[0]
        if dib_size in _BMP_DIB_SIZES:
            return "image/bmp"
    if len(header) >= 3 and header[:2] in _NETPBM_MAGIC and header[2:3] in b" \t\r\n":
        return "image/x-portable-anymap"
    if header.startswith(b"PK\x03\x04"):
        return _sniff_zip_mime(header, path)

    # Text-based markup: strip UTF-8 BOM and leading ASCII whitespace.
    stripped = header.lstrip(b"\xef\xbb\xbf \t\r\n")
    lower = stripped[:512].lower()
    if lower.startswith(b"<svg"):
        return "image/svg+xml"
    if lower.startswith(b"<?xml") and b"<svg" in stripped.lower():
        return "image/svg+xml"
    if lower.startswith((b"<!doctype html", b"<html")):
        return "text/html"
    return None


def _read_header(path: Path) -> bytes:
    try:
        with builtins.open(path, "rb") as stream:
            return stream.read(_SNIFF_BYTES)
    except OSError:
        return b""


def _call_renderer(
    fn: RendererCallable,
    path: Path,
    *,
    max_bytes: int | None,
    max_pages: int | None,
    options: dict[str, Any],
) -> Document:
    call_kwargs = dict(options)
    try:
        sig = inspect.signature(fn)
    except (TypeError, ValueError):
        sig = None
    if sig is not None:
        accepts_kwargs = any(
            p.kind is inspect.Parameter.VAR_KEYWORD for p in sig.parameters.values()
        )
        if "max_pages" in sig.parameters or accepts_kwargs:
            call_kwargs.setdefault("max_pages", max_pages)
        if "max_bytes" in sig.parameters:
            call_kwargs.setdefault("max_bytes", max_bytes)
    else:
        call_kwargs.setdefault("max_pages", max_pages)
    return fn(path, **call_kwargs)


def _cap_picture(picture: Picture, max_pages: int) -> Picture:
    if len(picture.pages) <= max_pages:
        return picture
    total = picture.total_pages if picture.total_pages is not None else len(picture.pages)
    pages = picture.pages
    if isinstance(pages, LazyPages):
        capped_pages = pages.capped(max_pages)
    else:
        closer = getattr(pages, "close", None)
        capped_pages = LazyPages(
            max_pages,
            lambda i, seq=pages: seq[i],
            close=closer if callable(closer) else None,
        )
    return replace(picture, pages=capped_pages, total_pages=total)


class Registry:
    """Maps file suffixes, MIME types, and magic bytes to lazy document renderers."""

    def __init__(self) -> None:
        self._renderers: list[Renderer] = []

    def register(
        self,
        renderer: Renderer | str,
        *,
        loader: RendererLoader | None = None,
        suffixes: Iterable[str] = (),
        mime_types: Iterable[str] = (),
        extra: str | None = None,
        sniff: Sniffer | None = None,
        priority: int = 0,
    ) -> Renderer:
        """Register a :class:`Renderer` (replacing any existing renderer with the same name)."""
        if isinstance(renderer, str):
            if loader is None:
                raise TypeError("loader is required when registering a renderer by name")
            spec = Renderer(
                name=renderer,
                loader=loader,
                suffixes=tuple(suffixes),
                mime_types=tuple(mime_types),
                extra=extra,
                sniff=sniff,
                priority=priority,
            )
        else:
            spec = renderer
        self._renderers = [existing for existing in self._renderers if existing.name != spec.name]
        self._renderers.append(spec)
        return spec

    def unregister(self, name: str) -> bool:
        """Remove a renderer by name; returns True if one was removed."""
        before = len(self._renderers)
        self._renderers = [existing for existing in self._renderers if existing.name != name]
        return len(self._renderers) != before

    def renderers(self) -> tuple[Renderer, ...]:
        """Return all registered renderers in registration order."""
        return tuple(self._renderers)

    def _ordered(self) -> list[Renderer]:
        # Higher priority wins; for equal priority, later registrations override earlier ones.
        indexed = list(enumerate(self._renderers))
        indexed.sort(key=lambda item: (item[1].priority, item[0]), reverse=True)
        return [spec for _, spec in indexed]

    def _match_mime(self, mime_type: str) -> Renderer | None:
        normalized = _normalize_mime(mime_type)
        for spec in self._ordered():
            if normalized in spec.mime_types:
                return spec
        return None

    def _match_suffix(self, path: Path) -> Renderer | None:
        ordered = self._ordered()
        for suffix in _candidate_suffixes(path):
            for spec in ordered:
                if suffix in spec.suffixes:
                    return spec
        return None

    def _resolve_with_mime(
        self,
        path: Path,
        *,
        mime_type: str | None = None,
        sniff: bool = True,
    ) -> tuple[Renderer | None, str | None]:
        if mime_type:
            normalized = _normalize_mime(mime_type)
            return self._match_mime(normalized), normalized

        matched = self._match_suffix(path)
        if matched is not None:
            return matched, None

        if not sniff or not path.is_file():
            return None, None

        header = _read_header(path)
        sniffed = sniff_mime(header, path)
        if sniffed is not None:
            matched = self._match_mime(sniffed)
            if matched is not None:
                return matched, sniffed

        for spec in self._ordered():
            if spec.sniff is not None:
                try:
                    if spec.sniff(header, path):
                        return spec, sniffed
                except Exception:
                    continue

        return None, sniffed

    def resolve(
        self,
        path: str | os.PathLike[str],
        *,
        mime_type: str | None = None,
        sniff: bool = True,
    ) -> Renderer | None:
        """Return the :class:`Renderer` that would handle ``path``, or ``None``."""
        matched, _ = self._resolve_with_mime(Path(path), mime_type=mime_type, sniff=sniff)
        return matched

    def supports(
        self,
        path: str | os.PathLike[str],
        *,
        mime_type: str | None = None,
        sniff: bool = True,
    ) -> bool:
        """Return True if a registered renderer handles ``path`` (without importing extras)."""
        return self.resolve(path, mime_type=mime_type, sniff=sniff) is not None

    def open(
        self,
        path: str | os.PathLike[str],
        *,
        max_bytes: int | None = DEFAULT_MAX_BYTES,
        max_pages: int | None = DEFAULT_MAX_PAGES,
        mime_type: str | None = None,
        **options: Any,
    ) -> Document:
        """Open ``path`` with the matching renderer and return a displayable :data:`Document`."""
        target = Path(path)
        if max_bytes is not None and max_bytes < 0:
            raise ValueError("max_bytes must be non-negative or None")
        if max_pages is not None and max_pages < 1:
            raise ValueError("max_pages must be at least 1 or None")

        stat = target.stat()  # raises FileNotFoundError if missing
        if target.is_dir():
            raise IsADirectoryError(f"Expected a file, got directory: {target}")
        if max_bytes is not None and stat.st_size > max_bytes:
            raise DocumentTooLarge(target, size=stat.st_size, max_bytes=max_bytes)

        renderer, detected_mime = self._resolve_with_mime(target, mime_type=mime_type, sniff=True)
        if renderer is None:
            raise UnsupportedType(
                target,
                suffix=target.suffix.lower(),
                mime_type=detected_mime,
            )

        try:
            fn = renderer.resolve_loader()
            document = _call_renderer(
                fn,
                target,
                max_bytes=max_bytes,
                max_pages=max_pages,
                options=options,
            )
        except MissingExtra:
            raise
        except ImportError as exc:
            if renderer.extra:
                raise MissingExtra(
                    renderer.extra,
                    renderer=renderer.name,
                    dependency=getattr(exc, "name", None),
                ) from exc
            raise

        if isinstance(document, Picture) and max_pages is not None:
            document = _cap_picture(document, max_pages)
        return document


def _default_registry() -> Registry:
    registry = Registry()
    # Built-in Pillow image renderer (no optional extra; Pillow is a core dependency).
    registry.register(
        Renderer(
            name="image",
            loader="pixcell.docs.image:render",
            suffixes=(
                ".png",
                ".jpg",
                ".jpeg",
                ".jfif",
                ".gif",
                ".webp",
                ".bmp",
                ".tif",
                ".tiff",
                ".ico",
                ".ppm",
                ".pgm",
                ".pbm",
                ".pnm",
                ".qoi",
            ),
            mime_types=(
                "image/png",
                "image/jpeg",
                "image/gif",
                "image/webp",
                "image/bmp",
                "image/tiff",
                "image/x-icon",
                "image/vnd.microsoft.icon",
                "image/x-portable-pixmap",
                "image/x-portable-graymap",
                "image/x-portable-bitmap",
                "image/x-portable-anymap",
                "image/qoi",
            ),
            extra=None,
        )
    )
    # SVG renderer (returns SVG natively; rasterises to Picture with pip install "pixcell[svg]").
    registry.register(
        Renderer(
            name="svg",
            loader="pixcell.docs.svg:render",
            suffixes=(".svg",),
            mime_types=("image/svg+xml",),
            extra="svg",
        )
    )
    # PDF renderer (rasterises pages lazily via pypdfium2 with pip install "pixcell[pdf]").
    registry.register(
        Renderer(
            name="pdf",
            loader="pixcell.docs.pdf:render",
            suffixes=(".pdf",),
            mime_types=("application/pdf",),
            extra="pdf",
        )
    )
    # Built-in EPUB renderer (stdlib zipfile + xml.etree; returns self-contained HTML).
    registry.register(
        Renderer(
            name="epub",
            loader="pixcell.docs.epub:render",
            suffixes=(".epub",),
            mime_types=("application/epub+zip",),
            extra=None,
        )
    )
    # DOCX renderer (converts Word documents to HTML via mammoth with pip install "pixcell[docx]").
    registry.register(
        Renderer(
            name="docx",
            loader="pixcell.docs.docx:render",
            suffixes=(".docx",),
            mime_types=(
                "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
            ),
            extra="docx",
        )
    )
    # PPTX renderer (bundled pptx-glimpse in QuickJS + resvg-py with pip install "pixcell[pptx]").
    registry.register(
        Renderer(
            name="pptx",
            loader="pixcell.docs.pptx:render",
            suffixes=(".pptx",),
            mime_types=(
                "application/vnd.openxmlformats-officedocument.presentationml.presentation",
            ),
            extra="pptx",
        )
    )
    return registry


default_registry = _default_registry()

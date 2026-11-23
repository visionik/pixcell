"""PDF renderer using ``pypdfium2`` for :mod:`pixcell.docs`.

``pypdfium2`` (Apache-2.0 / BSD) lives behind the ``pixcell[pdf]`` optional extra and is
imported lazily only when a PDF is opened.

Handle lifecycle & thread safety
--------------------------------
Each returned :class:`~pixcell.docs.Picture` keeps a single ``PdfDocument`` handle open
for fast on-demand page rendering and text extraction. Because PDFium is not thread-safe,
all PDFium calls across the process are serialised by a module-level re-entrant lock
(``_PDFIUM_LOCK``). Each rendered page is copied out of PDFium's bitmap buffer into an
independent Pillow image before the bitmap and page are closed, so returned images stay
valid even after the document is closed or passed across threads.

Call ``picture.close()`` (or use ``with pixcell.docs.open(path) as doc:``) to release the
underlying PDFium handle promptly; a :func:`weakref.finalize` hook also closes it on
garbage collection if the caller forgets.
"""

from __future__ import annotations

import threading
import weakref
from pathlib import Path
from typing import TYPE_CHECKING, Any

from pixcell.docs.errors import DocsError, MissingExtra
from pixcell.docs.types import DEFAULT_MAX_PAGES, LazyPages, Picture

if TYPE_CHECKING:
    from PIL.Image import Image

DEFAULT_SCALE = 2.0
"""Default PDF render scale (2x PDFium's 72 DPI baseline, i.e. 144 DPI)."""

# PDFium C internals are not thread-safe, even across separate documents.
_PDFIUM_LOCK = threading.RLock()


class PdfPasswordError(DocsError):
    """Raised when an encrypted PDF requires a password or the supplied password is wrong."""

    def __init__(self, path: Path, *, password_provided: bool = False) -> None:
        self.path = path
        self.password_provided = password_provided
        if password_provided:
            message = f"Incorrect password for encrypted PDF: {path}"
        else:
            message = f"PDF is password-protected; pass password=... to open it: {path}"
        super().__init__(message)


def _import_pdfium() -> Any:
    try:
        import pypdfium2 as pdfium
    except ImportError as exc:
        raise MissingExtra(
            "pdf",
            renderer="pdf",
            dependency=getattr(exc, "name", None) or "pypdfium2",
        ) from exc
    return pdfium


def _is_password_error(pdfium: Any, exc: Exception) -> bool:
    err_code = getattr(exc, "err_code", None)
    raw = getattr(pdfium, "raw", None)
    password_err = getattr(raw, "FPDF_ERR_PASSWORD", 4)
    if err_code is not None and err_code == password_err:
        return True
    return "password" in str(exc).lower()


def _close_pdfium_doc(doc: Any) -> None:
    with _PDFIUM_LOCK:
        doc.close()


def _read_title(doc: Any) -> str:
    try:
        raw_title = doc.get_metadata_value("Title")
    except Exception:
        return ""
    return raw_title.strip() if isinstance(raw_title, str) else ""


class _PdfHandle:
    """Thread-safe owner of an open ``pypdfium2.PdfDocument``."""

    def __init__(self, doc: Any, path: Path) -> None:
        self._doc: Any | None = doc
        self.path = path
        self._finalizer = weakref.finalize(self, _close_pdfium_doc, doc)

    def render_page(self, index: int, *, scale: float) -> Image:
        with _PDFIUM_LOCK:
            if self._doc is None:
                raise ValueError(f"PDF document is closed: {self.path}")
            page = self._doc[index]
            try:
                bitmap = page.render(scale=scale)
                try:
                    # Copy pixel data so the Pillow image does not reference PDFium's buffer.
                    return bitmap.to_pil().copy()
                finally:
                    bitmap.close()
            finally:
                page.close()

    def page_text(self, index: int) -> str:
        with _PDFIUM_LOCK:
            if self._doc is None:
                raise ValueError(f"PDF document is closed: {self.path}")
            page = self._doc[index]
            try:
                textpage = page.get_textpage()
                try:
                    return str(textpage.get_text_bounded())
                finally:
                    textpage.close()
            finally:
                page.close()

    def close(self) -> None:
        with _PDFIUM_LOCK:
            doc = self._doc
            if doc is None:
                return
            self._doc = None
            self._finalizer.detach()
            doc.close()


class PdfPages(LazyPages):
    """A :class:`LazyPages` sequence backed by ``pypdfium2`` with optional text extraction."""

    def __init__(
        self,
        count: int,
        handle: _PdfHandle,
        *,
        scale: float = DEFAULT_SCALE,
        cache: bool = False,
    ) -> None:
        self._handle = handle
        self._scale = scale
        super().__init__(
            count,
            lambda i: handle.render_page(i, scale=scale),
            cache=cache,
            close=handle.close,
        )

    def page_text(self, index: int) -> str:
        """Extract text from page ``index`` (0-based; supports negative indexing)."""
        if index < 0:
            index += self._count
        if not 0 <= index < self._count:
            raise IndexError(f"page index {index} out of range (0..{self._count - 1})")
        return self._handle.page_text(index)

    def capped(self, max_pages: int) -> PdfPages:
        """Return a view limited to at most ``max_pages`` without rendering any page."""
        if max_pages < 0:
            raise ValueError("max_pages must be non-negative")
        if self._count <= max_pages:
            return self
        return PdfPages(
            max_pages,
            self._handle,
            scale=self._scale,
            cache=self._cache is not None,
        )


def render(
    path: Path,
    *,
    scale: float = DEFAULT_SCALE,
    password: str | None = None,
    max_pages: int | None = DEFAULT_MAX_PAGES,
    cache: bool = False,
) -> Picture:
    """Open a PDF file with ``pypdfium2`` and return a lazy :class:`Picture`.

    Parameters
    ----------
    path:
        Path to the PDF file.
    scale:
        Render scale relative to 72 DPI (default ``2.0`` -> 144 DPI).
    password:
        Optional password for encrypted PDFs. Raises :class:`PdfPasswordError` if a
        password is required or incorrect.
    max_pages:
        Maximum number of pages exposed in ``Picture.pages`` (``None`` for no cap).
        ``Picture.total_pages`` records the document's full page count.
    cache:
        If ``True``, retain rendered Pillow pages in memory on :class:`PdfPages`.
    """
    if scale <= 0:
        raise ValueError("scale must be positive")
    if max_pages is not None and max_pages < 1:
        raise ValueError("max_pages must be at least 1 or None")

    pdfium = _import_pdfium()

    with _PDFIUM_LOCK:
        try:
            doc = pdfium.PdfDocument(path, password=password)
        except Exception as exc:
            pdfium_error = getattr(pdfium, "PdfiumError", None)
            if pdfium_error is not None and isinstance(exc, pdfium_error):
                if _is_password_error(pdfium, exc):
                    raise PdfPasswordError(path, password_provided=password is not None) from exc
                raise DocsError(f"Failed to open PDF {path}: {exc}") from exc
            raise

        try:
            total_pages = len(doc)
            title = _read_title(doc)
        except Exception:
            doc.close()
            raise

    handle = _PdfHandle(doc, path)
    count = min(total_pages, max_pages) if max_pages is not None else total_pages
    return Picture(
        pages=PdfPages(count, handle, scale=float(scale), cache=cache),
        title=title,
        source=path,
        total_pages=total_pages,
    )

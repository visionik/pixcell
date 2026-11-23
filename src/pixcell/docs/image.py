"""Built-in Pillow image renderer for :mod:`pixcell.docs`."""

from __future__ import annotations

import io
import threading
from pathlib import Path
from typing import TYPE_CHECKING

from pixcell.docs.types import DEFAULT_MAX_PAGES, LazyPages, Picture

if TYPE_CHECKING:
    from PIL.Image import Image


def render(path: Path, *, max_pages: int | None = DEFAULT_MAX_PAGES) -> Picture:
    """Open a raster image with Pillow and return a :class:`Picture`.

    Reads the file into memory so no OS file handle is left open on the returned image.
    Animated formats (GIF, WebP, APNG) remain a single animated Pillow image so
    :mod:`pixcell.textual` can play them; multi-page TIFFs expose each page lazily.
    """
    from PIL import Image as PILImage

    data = path.read_bytes()
    image = PILImage.open(io.BytesIO(data))
    n_frames = getattr(image, "n_frames", 1)

    if image.format == "TIFF" and n_frames > 1:
        count = min(n_frames, max_pages) if max_pages is not None else n_frames
        lock = threading.Lock()

        def render_page(index: int) -> Image:
            with lock:
                image.seek(index)
                return image.copy()

        return Picture(
            pages=LazyPages(count, render_page, close=image.close),
            source=path,
            total_pages=n_frames,
        )

    is_animated = bool(getattr(image, "is_animated", False)) and n_frames > 1

    def load_single(_: int) -> Image:
        if not is_animated:
            image.load()
        return image

    return Picture(
        pages=LazyPages(1, load_single, cache=True, close=image.close),
        source=path,
    )

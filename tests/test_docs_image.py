"""Tests for the built-in Pillow image renderer in pixcell.docs."""

from __future__ import annotations

from pathlib import Path

import pytest
from PIL import Image as PILImage

import pixcell.docs as docs
from pixcell.docs import Picture


@pytest.mark.parametrize(
    ("filename", "fmt"),
    [
        ("sample.png", "PNG"),
        ("sample.jpg", "JPEG"),
        ("sample.webp", "WEBP"),
        ("sample.bmp", "BMP"),
        ("sample.tif", "TIFF"),
    ],
)
def test_builtin_image_renderer_formats(tmp_path: Path, filename: str, fmt: str):
    path = tmp_path / filename
    PILImage.new("RGB", (24, 16), (30, 120, 210)).save(path, format=fmt)

    with docs.open(path) as doc:
        assert isinstance(doc, Picture)
        assert doc.page_count == 1
        assert doc.source == path
        assert doc.truncated is False
        assert doc.image.size == (24, 16)


def test_image_renderer_sniffing_without_extension(tmp_path: Path):
    path = tmp_path / "avatar"
    PILImage.new("RGB", (12, 10), (200, 40, 60)).save(path, format="PNG")

    assert docs.supports(path)
    with docs.open(path) as doc:
        assert isinstance(doc, Picture)
        assert doc.image.size == (12, 10)
        assert doc.image.getpixel((5, 5)) == (200, 40, 60)


def test_animated_gif_preserved_as_single_animated_page(tmp_path: Path):
    path = tmp_path / "anim.gif"
    frames = [
        PILImage.new("RGB", (16, 12), (255, 0, 0)),
        PILImage.new("RGB", (16, 12), (0, 255, 0)),
        PILImage.new("RGB", (16, 12), (0, 0, 255)),
    ]
    frames[0].save(path, format="GIF", save_all=True, append_images=frames[1:], duration=60, loop=0)

    with docs.open(path) as doc:
        assert isinstance(doc, Picture)
        assert doc.page_count == 1
        assert getattr(doc.image, "is_animated", False) is True
        assert getattr(doc.image, "n_frames", 1) == 3


def test_multipage_tiff_is_lazy_and_respects_max_pages(tmp_path: Path):
    path = tmp_path / "scan.tiff"
    colours = [(250, 0, 0), (0, 250, 0), (0, 0, 250), (250, 250, 0)]
    pages = [PILImage.new("RGB", (10, 10), colour) for colour in colours]
    pages[0].save(path, format="TIFF", save_all=True, append_images=pages[1:])

    with docs.open(path) as doc:
        assert isinstance(doc, Picture)
        assert doc.page_count == 4
        assert doc.total_pages == 4
        assert doc.truncated is False
        assert doc.pages[2].getpixel((0, 0)) == (0, 0, 250)
        assert doc.pages[0].getpixel((0, 0)) == (250, 0, 0)

    with docs.open(path, max_pages=2) as capped:
        assert isinstance(capped, Picture)
        assert capped.page_count == 2
        assert capped.total_pages == 4
        assert capped.truncated is True
        assert capped.pages[1].getpixel((0, 0)) == (0, 250, 0)

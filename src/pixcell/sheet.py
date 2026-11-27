"""Pictures on a cell grid, drawn the way the host terminal can move them.

A :class:`Sheet` does not import libghostty and does not know about widgets. The caller
hands it :class:`Placement` records (pixel bytes, the cell rectangle, a crop). ``update``
picks the protocol from :class:`~pixcell.probe.Capabilities` and returns the cell text to
paint plus any escapes to write.

Kitty placeholders and half blocks come back as row text. The host, or Textual, moves
that text with the other cells. iTerm2, Sixel, and Kitty-at-the-cursor come back as
escapes that put the cursor at ``origin + cell``; the caller writes those after the
frame, which is what :class:`pixcell.textual.Image` already does for one picture.
"""

from __future__ import annotations

import io
from dataclasses import dataclass, field, replace

from rich.color import Color
from rich.segment import Segment
from rich.style import Style
from textual.strip import Strip

from pixcell import halfblock, iterm2, kitty, sixel
from pixcell.probe import Capabilities
from pixcell.textual import OVERLAY

_OVERLAY = OVERLAY


@dataclass(frozen=True)
class Placement:
    """One picture on the grid. ``key`` is stable across updates. ``generation`` bumps
    when ``pixels`` changes."""

    key: str
    pixels: bytes
    pixel_format: str
    pixel_width: int
    pixel_height: int
    column: int
    row: int
    columns: int
    rows: int
    source: tuple[int, int, int, int] | None = None
    offset: tuple[int, int] = (0, 0)
    z: int | None = None
    generation: int = 0
    paint: bool = True
    """When false, keep the upload but do not draw. A pane does this for a picture
    it has already frozen into scrollback, so a later update does not delete it."""


@dataclass(frozen=True)
class Drawn:
    """What one ``update`` wants drawn.

    ``rows`` maps a grid row to the strip that covers from column 0 through the last
    picture cell on that row. The caller splices it over the cell text. ``setup`` is
    upload and delete escapes, empty when nothing changed. ``overlay`` is cursor-addressed
    escapes for protocols that do not live in the cells, empty otherwise. ``placements``
    is the input list when the protocol is ``host`` (an outer display draws them) and
    empty otherwise.
    """

    rows: dict[int, Strip]
    setup: str
    overlay: str
    placements: list[Placement] = field(default_factory=list)


@dataclass
class _Sent:
    generation: int
    image_id: int
    columns: int
    rows: int
    source: tuple[int, int, int, int] | None = None
    offset: tuple[int, int] = (0, 0)
    z: int | None = None

    def placed_as(self, placement: Placement) -> bool:
        """Is the host's placement (cells, crop, offset, layer) still ``placement``'s?"""
        return (self.columns, self.rows, self.source, self.offset, self.z) == (
            placement.columns,
            placement.rows,
            placement.source,
            placement.offset,
            placement.z,
        )


class Sheet:
    """The pictures currently on one grid, and what has already been uploaded."""

    def __init__(self, capabilities: Capabilities | None = None) -> None:
        if capabilities is None:
            from pixcell.textual import capabilities as current

            capabilities = current()
        self.capabilities = capabilities
        self._sent: dict[str, _Sent] = {}

    def update(
        self,
        placements: list[Placement],
        *,
        origin: tuple[int, int] | None = None,
        clip: tuple[int, int, int, int] | None = None,
    ) -> Drawn:
        """Return the rows, uploads, and overlay for ``placements``.

        ``origin`` is the screen cell of grid ``(0, 0)``. ``clip`` is the visible screen
        rectangle ``(x, y, width, height)``. Both matter for overlay protocols.
        """
        origin = origin or (0, 0)
        protocol = self.capabilities.protocol
        setup: list[str] = []
        wanted = {placement.key: placement for placement in placements}
        for key in [key for key in self._sent if key not in wanted]:
            setup.append(kitty.delete(self._sent.pop(key).image_id))
        if protocol in ("kitty", "kitty-direct"):
            for placement in placements:
                setup.extend(self._sync_kitty(placement))
        rows = (
            {}
            if protocol in _OVERLAY or protocol in ("host", "off")
            else self._rows(placements, protocol)
        )
        overlay = self._overlay(placements, origin, clip) if protocol in _OVERLAY else ""
        shown = list(placements) if protocol == "host" else []
        return Drawn(rows, "".join(setup), overlay, shown)

    def _sync_kitty(self, placement: Placement) -> list[str]:
        sent = self._sent.get(placement.key)
        if sent is None or sent.generation != placement.generation:
            image_id = sent.image_id if sent is not None else kitty.new_image_id()
            codes = [kitty.delete(image_id)] if sent is not None else []
            codes.append(self._transmit(placement, image_id))
            codes.append(_virtual_place(placement, image_id))
            self._sent[placement.key] = _sent_for(placement, image_id)
            return codes
        if not sent.placed_as(placement):
            # The whole picture is uploaded, so a new crop, offset or layer is a new
            # placement of the same upload.
            self._sent[placement.key] = _sent_for(placement, sent.image_id)
            return [_virtual_place(placement, sent.image_id)]
        return []

    def _transmit(self, placement: Placement, image_id: int) -> str:
        """Upload the whole picture: the placement (``_virtual_place``) applies ``source``,
        so a crop here would crop it twice."""
        fmt = self.capabilities.fastest_format
        whole = replace(placement, source=None)
        chunk_bytes = self.capabilities.chunk_bytes
        if fmt == "png":
            if placement.pixel_format == "png":
                return kitty.transmit(image_id, placement.pixels, chunk_bytes=chunk_bytes)
            return kitty.transmit(image_id, _png(_picture(whole)), chunk_bytes=chunk_bytes)
        compress = fmt == "zlib"
        if placement.pixel_format in ("rgb", "rgba"):
            return kitty.transmit_pixels(
                image_id,
                data=placement.pixels,
                width=placement.pixel_width,
                height=placement.pixel_height,
                alpha=placement.pixel_format == "rgba",
                compress=compress,
                chunk_bytes=chunk_bytes,
            )
        return kitty.transmit_pixels(image_id, _picture(whole), compress=compress, chunk_bytes=chunk_bytes)

    def _rows(self, placements: list[Placement], protocol: str) -> dict[int, Strip]:
        cells: dict[int, dict[int, Segment]] = {}
        for placement in sorted(placements, key=lambda item: (item.z or 0, item.row, item.column)):
            if not placement.paint:
                continue
            sent = self._sent.get(placement.key)
            if protocol == "kitty" and sent is not None:
                _paint_placeholders(cells, placement, sent.image_id)
            elif protocol == "halfblock":
                _paint_half_blocks(cells, placement)
        return {row: _strip(line) for row, line in cells.items()}

    def _overlay(
        self,
        placements: list[Placement],
        origin: tuple[int, int],
        clip: tuple[int, int, int, int] | None,
    ) -> str:
        protocol = self.capabilities.protocol
        cell_width, cell_height = self.capabilities.cell_size or (8, 16)
        codes: list[str] = []
        for placement in sorted(placements, key=lambda item: (item.z or 0, item.row, item.column)):
            if not placement.paint:
                continue
            screen_x = origin[0] + placement.column
            screen_y = origin[1] + placement.row
            visible = _clip_rect(screen_x, screen_y, placement.columns, placement.rows, clip)
            if visible is None:
                continue
            vis_x, vis_y, vis_w, vis_h = visible
            move = f"\x1b[{vis_y + 1};{vis_x + 1}H"
            if protocol == "kitty-direct":
                sent = self._sent.get(placement.key)
                if sent is None:
                    continue
                source = _visible_source(placement, vis_x - screen_x, vis_y - screen_y, vis_w, vis_h)
                codes.append(move + kitty.place_here(sent.image_id, vis_w, vis_h, source))
                continue
            picture = _fit(_picture(placement), (placement.columns * cell_width, placement.rows * cell_height))
            left = (vis_x - screen_x) * cell_width
            top = (vis_y - screen_y) * cell_height
            piece = picture.crop((left, top, left + vis_w * cell_width, top + vis_h * cell_height))
            if protocol == "sixel":
                piece = piece.crop((0, 0, piece.width, piece.height - piece.height % 6))
                if piece.height <= 0:
                    continue
                codes.append(move + sixel.encode(piece))
            else:
                codes.append(move + iterm2.inline(_png(piece), vis_w, vis_h))
        return "".join(codes)


def _sent_for(placement: Placement, image_id: int) -> _Sent:
    return _Sent(
        placement.generation,
        image_id,
        placement.columns,
        placement.rows,
        placement.source,
        placement.offset,
        placement.z,
    )


def _visible_source(
    placement: Placement, left_cells: int, top_cells: int, columns: int, rows: int
) -> tuple[int, int, int, int]:
    """The uploaded image's pixels (``x, y, width, height``) that the visible cells show.

    The picture (its ``source`` crop, or the whole upload) is scaled to fill
    ``placement.columns`` by ``placement.rows`` cells, so cells map back to image pixels
    by that scale, not by the host's cell size.
    """
    base_x, base_y, base_width, base_height = placement.source or (
        0,
        0,
        placement.pixel_width,
        placement.pixel_height,
    )
    across = max(placement.columns, 1)
    down = max(placement.rows, 1)
    left = base_x + left_cells * base_width // across
    top = base_y + top_cells * base_height // down
    right = base_x + (left_cells + columns) * base_width // across
    bottom = base_y + (top_cells + rows) * base_height // down
    return (left, top, max(right - left, 1), max(bottom - top, 1))


def _virtual_place(placement: Placement, image_id: int) -> str:
    return kitty.place(
        image_id,
        placement.columns,
        placement.rows,
        source=placement.source,
        offset=placement.offset,
        z=placement.z,
    )


def _paint_placeholders(cells: dict[int, dict[int, Segment]], placement: Placement, image_id: int) -> None:
    style = Style(color=Color.from_rgb(*kitty.id_colour(image_id)))
    for local in range(placement.rows):
        text = kitty.placeholder_row(image_id, local, placement.columns)
        line = cells.setdefault(placement.row + local, {})
        for index, cell in enumerate(text.split(kitty.PLACEHOLDER)[1:]):
            line[placement.column + index] = Segment(kitty.PLACEHOLDER + cell, style)


def _paint_half_blocks(cells: dict[int, dict[int, Segment]], placement: Placement) -> None:
    picture = _picture(placement)
    for local, line_cells in enumerate(halfblock.cells(picture, placement.columns, placement.rows)):
        line = cells.setdefault(placement.row + local, {})
        for index, (upper, lower) in enumerate(line_cells):
            line[placement.column + index] = Segment(
                halfblock.UPPER_HALF,
                Style(
                    color=Color.from_rgb(*upper) if upper else None,
                    bgcolor=Color.from_rgb(*lower) if lower else None,
                ),
            )


def _strip(line: dict[int, Segment]) -> Strip:
    if not line:
        return Strip.blank(0)
    end = max(line) + 1
    segments: list[Segment] = []
    column = 0
    blanks = 0
    while column < end:
        segment = line.get(column)
        if segment is None:
            blanks += 1
        else:
            if blanks:
                segments.append(Segment(" " * blanks))
                blanks = 0
            segments.append(segment)
        column += 1
    return Strip(segments, end)


def _picture(placement: Placement):
    """The placement's pixels as a Pillow image, cropped to ``source`` when it has one."""
    from PIL import Image

    if placement.pixel_format == "png":
        image = Image.open(io.BytesIO(placement.pixels))
    else:
        mode = "RGBA" if placement.pixel_format == "rgba" else "RGB"
        image = Image.frombytes(mode, (placement.pixel_width, placement.pixel_height), placement.pixels)
    if placement.source is None:
        return image
    left, top, width, height = placement.source
    return image.crop((left, top, left + width, top + height))


def _png(picture) -> bytes:
    buffer = io.BytesIO()
    picture.save(buffer, format="PNG", compress_level=1)
    return buffer.getvalue()


def _fit(picture, box: tuple[int, int]):
    from PIL import Image

    fitted = picture.convert("RGBA").resize(box, Image.Resampling.LANCZOS)
    return fitted


def _clip_rect(
    x: int,
    y: int,
    width: int,
    height: int,
    clip: tuple[int, int, int, int] | None,
) -> tuple[int, int, int, int] | None:
    if clip is None:
        return x, y, width, height
    cx, cy, cw, ch = clip
    left = max(x, cx)
    top = max(y, cy)
    right = min(x + width, cx + cw)
    bottom = min(y + height, cy + ch)
    if right <= left or bottom <= top:
        return None
    return left, top, right - left, bottom - top
